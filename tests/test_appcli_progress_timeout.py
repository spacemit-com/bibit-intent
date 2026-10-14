"""H08：进度、超时与取消。

对应 docs/design/cli_anything_plan.md 的 H08 验收标准：
- 渲染类动作按「准备 → 渲染 → 校验」上报进度（`core/progress.py`；非 MCP 路径自动降级）；
- 默认超时可按 appcli 配置，调用方显式给值优先；
- 超时/取消**有界停止**（进程组已终止），**保留已产出文件路径**，且**不自动重放**。
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
from bibit_intent.execution.cancellation import EdgeExecutionCancelled
from bibit_intent.resources import resource_path

_ORIGINAL_LOAD = AppCliRegistry.load

_VALID_PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n"
_TRUNCATED_PDF = b"%PDF-1.4\ntrailer but no end marker\n"


def _load_handlers():
    path = resource_path("skills") / "appcli" / "handlers.py"
    spec = importlib.util.spec_from_file_location("_test_appcli_progress_handlers", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _document(timeout_seconds=None):
    appcli = {
        "name": "demo",
        "display": "Demo",
        "entry_point": "cli-anything-demo",
        "channel": "artifact-only",
        "riscv64": "verified",
        "actions": [
            {
                "name": "render",
                "argv": ["export", "render", "{output}"],
                "params": {"output": {"type": "str", "path": True}},
                "artifacts": [{"param": "output", "verify": ["exists", "pdf_magic"]}],
            }
        ],
    }
    if timeout_seconds is not None:
        appcli["timeout_seconds"] = timeout_seconds
    return {"version": 1, "appclies": [appcli]}


@pytest.fixture
def build(tmp_path, monkeypatch):
    """返回 (module, 绑定注册表的工厂, 记录到的超时列表)。"""
    module = _load_handlers()
    seen: list[float] = []

    def bind(timeout_seconds=None):
        path = tmp_path / "appcli_registry.yaml"
        path.write_text(
            yaml.safe_dump(_document(timeout_seconds), allow_unicode=True), encoding="utf-8"
        )
        monkeypatch.setattr(
            module.AppCliRegistry,
            "load",
            classmethod(lambda cls, _path=None: _ORIGINAL_LOAD(path)),
        )

    monkeypatch.setattr(module, "check_readiness", lambda spec: AppCliReadiness(True))
    monkeypatch.setenv("BIBIT_INTENT_APPCLI_ROOT", str(tmp_path / "workspace"))
    (tmp_path / "workspace").mkdir()

    def runner(command, *, timeout, max_output_bytes, env=None):
        seen.append(timeout)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True}), stderr="")

    monkeypatch.setattr(module, "run_bounded_command", runner)
    return SimpleNamespace(module=module, bind=bind, seen=seen, root=tmp_path / "workspace")


# ── 默认超时按 appcli 配置 ──────────────────────────────────────────

def test_registry_timeout_is_used_when_the_caller_does_not_specify(build):
    build.bind(timeout_seconds=7)
    build.module.appcli_run(appcli="demo", action="render", args=["output=o.pdf"])
    assert build.seen == [7]


def test_caller_timeout_wins_over_the_registry_value(build):
    build.bind(timeout_seconds=7)
    build.module.appcli_run(appcli="demo", action="render", args=["output=o.pdf"], timeout=3)
    assert build.seen == [3]


def test_zero_timeout_param_means_registry_default(build):
    build.bind()  # 用默认 300
    build.module.appcli_run(appcli="demo", action="render", args=["output=o.pdf"], timeout=0)
    assert build.seen == [300]


def test_none_timeout_param_means_registry_default(build):
    build.bind(timeout_seconds=42)
    build.module.appcli_run(appcli="demo", action="render", args=["output=o.pdf"], timeout=None)
    assert build.seen == [42]


# ── 进度三段 ─────────────────────────────────────────────────────────

def test_progress_reports_prepare_render_and_verify(build, monkeypatch):
    build.bind()
    events: list[tuple] = []
    monkeypatch.setattr(
        build.module, "report_progress",
        lambda position, total, message: events.append((position, total, message)),
    )

    def runner(command, *, timeout, max_output_bytes, env=None):
        (build.root / "o.pdf").write_bytes(_VALID_PDF)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True}), stderr="")

    monkeypatch.setattr(build.module, "run_bounded_command", runner)
    build.module.appcli_run(appcli="demo", action="render", args=["output=o.pdf"])

    assert [event[0] for event in events] == [1, 2, 3]
    assert all(event[1] == 3 for event in events)
    assert events[0][2].startswith("准备：demo/render")
    assert "校验" in events[1][2]
    assert events[2][2] == "校验通过"


def test_progress_marks_the_last_stage_when_verification_fails(build, monkeypatch):
    build.bind()
    events: list[tuple] = []
    monkeypatch.setattr(
        build.module, "report_progress",
        lambda position, total, message: events.append((position, total, message)),
    )

    def runner(command, *, timeout, max_output_bytes, env=None):
        (build.root / "half.pdf").write_bytes(_TRUNCATED_PDF)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(build.module, "run_bounded_command", runner)
    build.module.appcli_run(appcli="demo", action="render", args=["output=half.pdf"])

    assert events[-1] == (3, 3, "校验未通过")


def test_progress_never_breaks_execution_without_an_mcp_bridge(build):
    """没绑进度桥时 report_progress 是 no-op：不抛异常、不改变结果。"""
    from bibit_intent.core.progress import report_progress

    build.bind()
    report_progress(1, 3, "准备")

    result = build.module.appcli_run(appcli="demo", action="render", args=["output=o.pdf"])
    # 没有产物文件，因此产物校验失败；这与进度是否上报无关。
    assert not result.ok


# ── 超时：保留产物路径、不重放 ───────────────────────────────────────

def test_timeout_reports_partial_artifact_and_never_replays(build, monkeypatch):
    build.bind(timeout_seconds=5)
    calls: list[list[str]] = []

    def runner(command, *, timeout, max_output_bytes, env=None):
        calls.append(command)
        (build.root / "half.pdf").write_bytes(_TRUNCATED_PDF)
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(build.module, "run_bounded_command", runner)
    result = build.module.appcli_run(appcli="demo", action="render", args=["output=half.pdf"])

    assert not result.ok
    assert "超时（5s）" in result.message
    assert "不会自动重放" in result.message
    assert len(calls) == 1, "超时后不允许自动重放"
    assert "产物现状（未判定成功）" in result.detail
    assert "half.pdf" in result.detail
    assert "%%EOF" in result.detail, "半成品应给出截断证据，而不是只说超时"


def test_timeout_without_any_output_says_so(build, monkeypatch):
    build.bind()

    def runner(command, *, timeout, max_output_bytes, env=None):
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(build.module, "run_bounded_command", runner)
    result = build.module.appcli_run(appcli="demo", action="render", args=["output=nope.pdf"])

    assert not result.ok
    assert "文件不存在" in result.detail


# ── 取消：有界停止 + 如实回报 ────────────────────────────────────────

def test_cancellation_returns_a_result_instead_of_propagating(build, monkeypatch):
    build.bind()

    def runner(command, *, timeout, max_output_bytes, env=None):
        (build.root / "half.pdf").write_bytes(_TRUNCATED_PDF)
        raise EdgeExecutionCancelled("edge execution cancelled")

    monkeypatch.setattr(build.module, "run_bounded_command", runner)
    result = build.module.appcli_run(appcli="demo", action="render", args=["output=half.pdf"])

    assert not result.ok
    assert result.message.startswith("已取消：")
    assert "不会自动重放" in result.message
    assert "产物现状（未判定成功）" in result.detail
    # 没有副作用可声明（渲染已被终止），不允许 escalate。
    assert result.escalate is None


def test_cancellation_is_audited_as_an_executed_run(build, monkeypatch):
    from bibit_intent.core import dependency_audit, appcli_audit

    appcli_audit.reset_metrics()
    build.bind()
    monkeypatch.setattr(
        build.module, "run_bounded_command",
        lambda command, **_: (_ for _ in ()).throw(EdgeExecutionCancelled("cancelled")),
    )
    build.module.appcli_run(appcli="demo", action="render", args=["output=o.pdf"])

    record = [
        item for item in dependency_audit.read_records(limit=20)
        if item.get("action") == "appcli_run"
    ][0]
    assert record["stage"] == "executed"
    assert record["reason"] == "cancelled"
    assert appcli_audit.appcli_metrics()["runs"] == 1
    appcli_audit.reset_metrics()
