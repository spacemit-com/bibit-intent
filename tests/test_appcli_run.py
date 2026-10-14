"""H03：appcli_run 的骨架与通道判定。

对应 docs/design/cli_anything_plan.md 的 H03 验收标准：结构化 args（非自由 argv）、
强制 --json、禁 REPL、输出路径越界拒绝、channel: host 在沙箱内返回
needs_host_channel 且不执行、confirm: true。

真正的产物校验（exit 0 ≠ 成功）在 H04，本文件只覆盖「命令层」契约。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

from bibit_intent.core.appcli_prereq import AppCliReadiness
from bibit_intent.core.appcli_registry import AppCliRegistry
from bibit_intent.resources import resource_path

#: 未打补丁的原始加载器：多个用例要换注册表文件，必须从它出发重新绑定，
#: 否则第二次 patch 会基于「已经指向固定文件」的 classmethod，换不动。
_ORIGINAL_LOAD = AppCliRegistry.load


def _load_handlers():
    path = resource_path("skills") / "appcli" / "handlers.py"
    spec = importlib.util.spec_from_file_location("_test_appcli_run_handlers", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _document(**overrides):
    appcli = {
        "name": "demo",
        "display": "Demo",
        "entry_point": "cli-anything-demo",
        "channel": "artifact-only",
        "riscv64": "verified",
        "actions": [
            {
                "name": "render",
                "argv": ["--project", "{project}", "export", "render", "{output}", "-p", "{preset}"],
                "params": {
                    "project": {"type": "str", "path": True},
                    "output": {"type": "str", "path": True},
                    "preset": {"type": "str", "default": "pdf"},
                },
            },
            {"name": "peek", "argv": ["peek"], "params": {}, "readonly": True},
        ],
    }
    appcli.update(overrides)
    return {"version": 1, "appclies": [appcli]}


def _bind_registry(monkeypatch, module, document, tmp_path, filename="appcli_registry.yaml"):
    path = tmp_path / filename
    path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(
        module.AppCliRegistry, "load",
        classmethod(lambda cls, _path=None: _ORIGINAL_LOAD(path)),
    )
    return path


@pytest.fixture
def handlers(tmp_path, monkeypatch):
    module = _load_handlers()
    _bind_registry(monkeypatch, module, _document(), tmp_path)
    monkeypatch.setattr(module, "check_readiness", lambda spec: AppCliReadiness(True))
    monkeypatch.setenv("BIBIT_INTENT_APPCLI_ROOT", str(tmp_path / "workspace"))
    return module


@pytest.fixture
def ran(handlers, monkeypatch):
    """捕获真正发出的命令行，并返回可控的 appcli 输出。"""
    calls: list[list[str]] = []
    envs: list[dict[str, str] | None] = []
    outcome = {"returncode": 0, "stdout": json.dumps({"ok": True, "file_size": 12}), "stderr": ""}

    def fake_run(command, *, timeout, max_output_bytes, env=None):
        calls.append(command)
        envs.append(env)
        return SimpleNamespace(**outcome)

    monkeypatch.setattr(handlers, "run_bounded_command", fake_run)
    return SimpleNamespace(module=handlers, calls=calls, envs=envs, outcome=outcome)


# ── 命令行构造 ───────────────────────────────────────────────────────

def test_command_forces_json_after_entry_point(ran, tmp_path):
    result = ran.module.appcli_run(
        appcli="demo", action="render", args=["project=p.json", "output=out.pdf"]
    )
    assert result.ok, result.message
    assert ran.calls == [[
        "cli-anything-demo", "--json", "--project",
        str(tmp_path / "workspace" / "p.json"),
        "export", "render",
        str(tmp_path / "workspace" / "out.pdf"),
        "-p", "pdf",
    ]]


def test_args_accept_key_value_objects(ran, tmp_path):
    """``[{"key": .., "value": ..}]`` 也要能跑。

    K3 真实会话里模型第一次就是这么传的（把参数表当结构化对象），原本一律判成
    「args 项不是 key=value 形式」，白费一轮往返。
    """
    result = ran.module.appcli_run(
        appcli="demo",
        action="render",
        args=[{"key": "project", "value": "p.json"}, {"key": "output", "value": "o.pdf"}],
    )
    assert result.ok, result.message
    assert str(tmp_path / "workspace" / "o.pdf") in ran.calls[0]


def test_relative_paths_resolve_into_the_workspace(ran, tmp_path):
    ran.module.appcli_run(
        appcli="demo", action="render", args=["project=sub/p.json", "output=o.pdf"]
    )
    command = ran.calls[0]
    assert str(tmp_path / "workspace" / "sub" / "p.json") in command
    assert str(tmp_path / "workspace" / "o.pdf") in command


def test_args_accept_an_equals_inside_the_value(ran):
    """值里允许出现 =，只在第一个 = 处切分。"""
    result = ran.module.appcli_run(
        appcli="demo", action="render", args="project=a=b.json,output=o.pdf"
    )
    assert result.ok, result.message
    assert any("a=b.json" in token for token in ran.calls[0])
    assert any(token.endswith("o.pdf") for token in ran.calls[0])


def test_non_path_actions_are_not_rewritten(ran):
    result = ran.module.appcli_run(appcli="demo", action="peek")
    assert result.ok
    assert ran.calls == [["cli-anything-demo", "--json", "peek"]]


# ── 参数拒绝 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("args", ["project", "=value", "project=a.json,=b.pdf", "  "])
def test_malformed_args_are_rejected(ran, args):
    result = ran.module.appcli_run(appcli="demo", action="render", args=args)
    assert not result.ok, result.message
    assert "参数错误" in result.message
    assert ran.calls == []


def test_valid_args_are_accepted(ran):
    assert ran.module.appcli_run(
        appcli="demo", action="render", args="project=a.json,output=b.pdf"
    ).ok
    # 执行层把字符串转 list 时已经滤掉分隔符产生的空段，这里保持一致（宽容分隔符）。
    assert ran.module.appcli_run(
        appcli="demo", action="render", args="project=a.json,,output=b.pdf"
    ).ok


def test_duplicate_arg_key_is_rejected(ran):
    result = ran.module.appcli_run(
        appcli="demo", action="render", args=["project=a.json", "project=b.json"]
    )
    assert not result.ok
    assert "重复" in result.message
    assert ran.calls == []


def test_undeclared_arg_key_is_rejected_without_executing(ran):
    result = ran.module.appcli_run(
        appcli="demo", action="render", args=["project=a.json", "evil=1"]
    )
    assert not result.ok
    assert "evil" in result.message
    assert ran.calls == []


def test_unknown_appcli_or_action_is_rejected(ran):
    assert not ran.module.appcli_run(appcli="nope", action="render").ok
    assert not ran.module.appcli_run(appcli="demo", action="nope").ok
    assert ran.calls == []


def test_unverified_appcli_is_refused(handlers, monkeypatch, tmp_path):
    _bind_registry(monkeypatch, handlers, _document(riscv64="unverified"), tmp_path, "u.yaml")
    monkeypatch.setattr(handlers, "run_bounded_command", lambda *a, **k: pytest.fail("must not run"))
    result = handlers.appcli_run(appcli="demo", action="peek")
    assert not result.ok
    assert "尚未验收" in result.message


# ── 路径越界 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["/etc/passwd", "../escape.pdf", "sub/../../escape.pdf"])
def test_paths_outside_the_workspace_are_refused(ran, bad):
    result = ran.module.appcli_run(
        appcli="demo", action="render", args=["project=p.json", f"output={bad}"]
    )
    assert not result.ok
    assert "工作区" in result.message
    assert ran.calls == []


def test_symlink_escaping_the_workspace_is_refused(ran, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "link.pdf").symlink_to(outside / "target.pdf")
    result = ran.module.appcli_run(
        appcli="demo", action="render", args=["project=p.json", "output=link.pdf"]
    )
    assert not result.ok
    assert "工作区" in result.message
    assert ran.calls == []


# ── 通道判定 ─────────────────────────────────────────────────────────

def test_host_channel_in_sandbox_escalates_without_executing(handlers, monkeypatch, tmp_path):
    _bind_registry(monkeypatch, handlers, _document(channel="host"), tmp_path, "host.yaml")
    monkeypatch.setenv("BIANBU_SANDBOX_ACTIVE", "1")
    monkeypatch.setattr(handlers, "run_bounded_command", lambda *a, **k: pytest.fail("must not run"))

    result = handlers.appcli_run(appcli="demo", action="peek")
    assert not result.ok
    assert result.escalate == "needs_host_channel"
    assert "宿主" in result.message


def test_host_channel_is_allowed_on_the_host(handlers, monkeypatch, tmp_path):
    _bind_registry(monkeypatch, handlers, _document(channel="host"), tmp_path, "host.yaml")
    monkeypatch.delenv("BIANBU_SANDBOX_ACTIVE", raising=False)
    monkeypatch.setattr(
        handlers, "run_bounded_command",
        lambda command, *, timeout, max_output_bytes, env=None: SimpleNamespace(
            returncode=0, stdout=json.dumps({"ok": True}), stderr=""
        ),
    )
    assert handlers.appcli_run(appcli="demo", action="peek").ok


# ── 前置与执行结果 ───────────────────────────────────────────────────

def test_missing_prerequisites_skip_execution(ran, monkeypatch):
    monkeypatch.setattr(
        ran.module, "check_readiness",
        lambda spec: AppCliReadiness(
            entry_point_found=False, missing_apt=("cli-anything-demo",)
        ),
    )
    result = ran.module.appcli_run(appcli="demo", action="peek")
    assert not result.ok
    assert "缺少前置" in result.message
    assert "不会自动安装" in result.message
    assert ran.calls == []


def test_non_zero_exit_is_a_failure_with_stderr_tail(ran):
    ran.outcome.update(returncode=3, stdout="", stderr="boom: no such project")
    result = ran.module.appcli_run(appcli="demo", action="peek")
    assert not result.ok
    assert "exit 3" in result.message
    assert "boom" in result.detail


def test_exit_zero_without_json_is_reported_as_malformed(ran):
    ran.outcome.update(returncode=0, stdout="export_fallback: backend missing", stderr="")
    result = ran.module.appcli_run(appcli="demo", action="peek")
    assert not result.ok
    assert "输出异常" in result.message
    assert "export_fallback" in result.detail


@pytest.mark.parametrize("payload", ["\"text\"", "null", "1"])
def test_non_object_json_is_malformed(ran, payload):
    """写动作只认对象信封（``ok`` 等字段只在对象里）。"""
    ran.outcome.update(stdout=payload)
    result = ran.module.appcli_run(
        appcli="demo", action="render", args=["project=p.json", "output=o.pdf"]
    )
    assert not result.ok
    assert "输出异常" in result.message


def test_write_action_rejects_a_top_level_list(ran):
    """数组信封给写动作同样不认——产物结论只能来自对象信封。"""
    ran.outcome.update(stdout="[1, 2]")
    result = ran.module.appcli_run(
        appcli="demo", action="render", args=["project=p.json", "output=o.pdf"]
    )
    assert not result.ok
    assert "输出异常" in result.message


def test_readonly_action_accepts_a_top_level_list(ran):
    """K3 实测：``writer list`` / ``calc list-sheets`` 这类只读命令顶层就是数组。

    要求对象会把它们一律判成「输出异常」，于是登记了也用不了。只读动作放开数组，
    写动作不放——这是「输出必须是 JSON」与「必须带结论信封」的差别。
    """
    ran.outcome.update(stdout=json.dumps([{"index": 0, "name": "Sheet1"}]))
    result = ran.module.appcli_run(appcli="demo", action="peek")
    assert result.ok, result.message
    assert "Sheet1" in result.detail


def test_success_reports_the_json_summary(ran):
    result = ran.module.appcli_run(appcli="demo", action="peek")
    assert result.ok
    assert "exit 0" in result.message
    assert "$ cli-anything-demo --json peek" in result.detail
    assert '"file_size": 12' in result.detail


def test_timeout_is_reported(ran, monkeypatch):
    def boom(command, *, timeout, max_output_bytes, env=None):
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(ran.module, "run_bounded_command", boom)
    result = ran.module.appcli_run(appcli="demo", action="peek", timeout=5)
    assert not result.ok
    assert "超时" in result.message


def test_output_limit_is_reported(ran, monkeypatch):
    from bibit_intent.execution.bounded_command import OutputLimitExceeded

    def boom(command, *, timeout, max_output_bytes, env=None):
        raise OutputLimitExceeded()

    monkeypatch.setattr(ran.module, "run_bounded_command", boom)
    result = ran.module.appcli_run(appcli="demo", action="peek")
    assert not result.ok
    assert "上限" in result.message


def test_missing_entry_point_is_reported(ran, monkeypatch):
    def boom(command, *, timeout, max_output_bytes, env=None):
        raise FileNotFoundError

    monkeypatch.setattr(ran.module, "run_bounded_command", boom)
    result = ran.module.appcli_run(appcli="demo", action="peek")
    assert not result.ok
    assert "入口命令不存在" in result.message


def test_workspace_directory_is_created_before_path_actions(ran, tmp_path):
    ran.module.appcli_run(
        appcli="demo", action="render", args=["project=p.json", "output=o.pdf"]
    )
    assert (tmp_path / "workspace").is_dir()


def test_workspace_is_not_created_for_non_path_actions(ran, tmp_path):
    ran.module.appcli_run(appcli="demo", action="peek")
    assert not (tmp_path / "workspace").exists()


# ── 工具声明 ─────────────────────────────────────────────────────────

def test_run_tool_requires_confirmation_and_declares_discovery():
    from bibit_intent.core.registry import ToolRegistry
    from bibit_intent.execution.skill_loader import load_skills

    registry = ToolRegistry(resource_path("tools.yaml"))
    load_skills(resource_path("skills"), registry, {})
    tool = registry.get("appcli_run")
    assert tool.system.get("confirm")
    assert tool.mcp_group() == "admin"
    assert tool.system["mcp"]["discovery"] == "appcli_list"

    schema = tool.mcp_schema()["function"]
    assert schema["parameters"]["required"] == ["appcli", "action"]
    assert schema["parameters"]["properties"]["args"]["type"] == "array"
    assert "appcli_list" in schema["description"]


# ── 执行环境变量（H16）────────────────────────────────────────────────

def test_appcli_env_is_applied_on_top_of_the_process_environment(ran, monkeypatch, tmp_path):
    """注册表声明的 env 必须在继承环境之上覆盖，而不是替换整个环境。"""
    _bind_registry(
        monkeypatch, ran.module,
        _document(env={"SAL_USE_VCLPLUGIN": "svp"}), tmp_path, "env.yaml",
    )
    monkeypatch.setenv("BIBIT_TEST_INHERITED", "1")

    ran.module.appcli_run(appcli="demo", action="peek")

    env = ran.envs[0]
    assert env["SAL_USE_VCLPLUGIN"] == "svp"
    assert env["BIBIT_TEST_INHERITED"] == "1"


def test_appcli_without_env_keeps_the_inherited_environment(ran):
    """没声明 env 的 appcli 保持原行为：不传 env，由 Popen 继承进程环境。"""
    ran.module.appcli_run(appcli="demo", action="peek")

    assert ran.envs[0] is None
