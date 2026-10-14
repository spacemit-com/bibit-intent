import json
import subprocess
from types import SimpleNamespace

import pytest

from bibit_intent.core import package_preview
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.execution.executor import Executor
from bibit_intent.execution.system_tools import ExecResult
from bibit_intent.resources import resource_path
from bibit_intent.routing.intent import decide_local_intent
from bibit_intent.routing.rule_engine import RuleEngine
from bibit_intent.routing.router import Router


@pytest.fixture
def backend(monkeypatch):
    calls = []
    def command(args, **kwargs):
        calls.append(args)
        if args[0] == "dpkg-query":
            return SimpleNamespace(returncode=0, stdout="libfoo\tii \t1.0\nlibfoo-dev:riscv64\thi \t2.0\nlibfoo-old\trc \t0.9\nlibfoo-broken\tiU \t1.0\n", stderr="")
        if args[0] == "apt-cache":
            return SimpleNamespace(returncode=0, stdout="libfoo\nlibfoo-dev\nlibfoo-new\nlibfoo\nbash\n", stderr="")
        assert args[1:3] == ["-I", "-c"]
        return subprocess.run(args, input=kwargs["input"], text=True, capture_output=True, timeout=kwargs["timeout"])
    monkeypatch.setattr(package_preview, "run_command", command)
    return calls


@pytest.mark.parametrize("mode,pattern", [("glob", "libfoo*"), ("regex", "^libfoo.*$")])
def test_available_preview(backend, mode, pattern):
    result = package_preview.preview_packages(pattern, mode, "available")
    assert result["ok"] and result["read_only"]
    assert [item["package"] for item in result["items"]] == ["libfoo", "libfoo-dev", "libfoo-new"]
    assert result["items"][1]["local_states"][0]["held"]
    assert result["items"][2]["local_states"][0]["status"] == "absent"
    assert "不代表可安装" in result["message"]
    assert not result["truncated"]
    assert backend[1] == ["apt-cache", "pkgnames"]


def test_installed_scope_excludes_config_only(backend):
    result = package_preview.preview_packages("libfoo*", "glob", "installed")
    assert [item["package"] for item in result["items"]] == ["libfoo", "libfoo-broken", "libfoo-dev:riscv64"]
    assert not any(command[0] == "apt-cache" for command in backend)


def test_modes_are_not_interchangeable(backend):
    glob = package_preview.preview_packages("libfoo*", "glob", "available")
    regex = package_preview.preview_packages("libfoo*", "regex", "available")
    assert glob["returned_count"] == 3 and regex["returned_count"] == 1


def test_limit_and_no_match(backend):
    result = package_preview.preview_packages("*", "glob", "available", 1)
    assert result["returned_count"] == 1 and result["truncated"]
    result = package_preview.preview_packages("none*", "glob", "available")
    assert result["ok"] and result["items"] == [] and not result["truncated"]


def test_invalid_regex(backend):
    result = package_preview.preview_packages("[", "regex", "available")
    assert result["error"] == "invalid_pattern"


@pytest.mark.parametrize("pattern,mode,scope,limit", [
    ("", "glob", "available", 20), ("a" * 129, "regex", "installed", 20),
    ("*", "guess", "available", 20), ("*", "glob", "remove", 20),
    ("*", "glob", "installed", 51), ("*", "glob", "installed", True),
])
def test_validation_before_query(monkeypatch, pattern, mode, scope, limit):
    monkeypatch.setattr(package_preview, "run_command", lambda *args, **kwargs: pytest.fail("no query allowed"))
    assert not package_preview.preview_packages(pattern, mode, scope, limit)["ok"]


def test_timeout_is_reported(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("matcher", 2)
    monkeypatch.setattr(package_preview, "run_command", timeout)
    assert package_preview.preview_packages("(a+)+$", "regex", "available")["error"] == "preview_timeout"


def test_pathological_regex_is_stopped_in_child(monkeypatch):
    def command(args, **kwargs):
        if args[0] == "dpkg-query":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[0] == "apt-cache":
            return SimpleNamespace(returncode=0, stdout="a" * 80 + "!\n" + "a" * 80 + "b\n", stderr="")
        return subprocess.run(args, input=kwargs["input"], text=True, capture_output=True, timeout=kwargs["timeout"])
    monkeypatch.setattr(package_preview, "run_command", command)
    assert package_preview.preview_packages("(a+)+$", "regex", "available")["error"] == "preview_timeout"


def test_bad_state_not_reported_absent(monkeypatch):
    monkeypatch.setattr(package_preview, "run_command", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="bad data", stderr=""))
    assert package_preview.preview_packages("*", "glob", "installed")["error"] == "state_query_failed"


def test_preview_registered_and_routed(backend):
    registry = ToolRegistry(resource_path("tools.yaml"))
    handlers = {}
    load_skills(resource_path("skills"), registry, handlers)
    tool = registry.get("pkg_match_preview")
    assert tool.mcp_group() == "readonly" and not tool.system.get("confirm")
    plan = decide_local_intent("软件包预览 available regex ^libfoo.*$", RuleEngine(registry), registry)
    assert plan.tool_name == "pkg_match_preview"
    result = handlers[plan.tool_name](**plan.slots)
    assert result.ok and json.loads(result.detail)["returned_count"] == 3
    assert "libfoo-dev:riscv64 / 已安装 / 版本 2.0 / 已锁定（hold）" in result.message
    assert "libfoo-new：未安装" in result.message


@pytest.mark.parametrize("scope,expected_scope", [("软件源", "available"), ("已安装", "installed")])
@pytest.mark.parametrize("mode,expected_mode,pattern", [("通配符", "glob", "libfoo*"), ("正则", "regex", "^libfoo.*$"), ("正则表达式", "regex", "libfoo.*")])
def test_chinese_preview_routes(scope, expected_scope, mode, expected_mode, pattern):
    registry = ToolRegistry(resource_path("tools.yaml"))
    load_skills(resource_path("skills"), registry, {})
    plan = decide_local_intent(f"软件包预览 {scope} {mode} {pattern}", RuleEngine(registry), registry)
    assert plan.tool_name == "pkg_match_preview"
    assert plan.slots == {"scope": expected_scope, "mode": expected_mode, "pattern": pattern}
    calls = []
    def handler(**kwargs):
        calls.append(kwargs)
        return ExecResult(True, "只读测试")
    router = Router(registry, RuleEngine(registry), None, Executor(registry, handlers={"pkg_match_preview": handler}))
    result = router.handle(f"软件包预览 {scope} {mode} {pattern}")
    assert result.exec_result.ok
    assert len(calls) == 1
    assert all(calls[0][key] == value for key, value in plan.slots.items())


def test_preview_display_preserves_all_architectures():
    preview = {
        "ok": True, "message": "只读预览", "scope": "available", "mode": "glob",
        "items": [{"package": "libfoo", "local_states": [
            {"package": "libfoo:riscv64", "status": "installed", "version": "1.0", "held": True},
            {"package": "libfoo:arm64", "status": "incomplete", "version": "2.0", "held": False},
        ]}],
    }
    message = package_preview.format_preview_message(preview)
    assert "libfoo:riscv64 / 已安装 / 版本 1.0 / 已锁定（hold）" in message
    assert "libfoo:arm64 / 未配置完成或状态异常 / 版本 2.0" in message
    assert "不构成安装或卸载授权" in message


def test_preview_display_empty_truncated_and_failure(backend):
    empty = package_preview.preview_packages("none*", "glob", "available")
    assert "请检查范围和表达式" in package_preview.format_preview_message(empty)
    limited = package_preview.preview_packages("*", "glob", "available", 1)
    assert "已截断" in package_preview.format_preview_message(limited)
    failed = package_preview.preview_packages("[", "regex", "installed")
    assert package_preview.format_preview_message(failed) == failed["message"]
