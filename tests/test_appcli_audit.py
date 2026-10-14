"""H06：appcli 执行的审计与指标。

对应 docs/design/cli_anything_plan.md 的 H06 验收标准：复用
``core/dependency_audit.py::record()``，``action="appcli_run"``（含
appcli/action/command/outputs/verify/ok/duration_ms）；``/metrics`` 增加 appcli 段；
**写失败不影响执行结果**。

本文件同时固定两个容易搞混的口径：
- 被白名单/前置/通道/参数拒掉的调用记 ``stage=refused``，**不计入** ``runs``；
- ``failures`` 里的 ``verify_failures`` 专门数「命令 exit 0 但产物不合格」（H04 场景）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from types import SimpleNamespace

import pytest
import yaml

from bibit_intent.core import dependency_audit
from bibit_intent.core import appcli_audit
from bibit_intent.core.appcli_prereq import AppCliReadiness
from bibit_intent.core.appcli_registry import AppCliRegistry
from bibit_intent.resources import resource_path

_ORIGINAL_LOAD = AppCliRegistry.load

_VALID_PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n"
_TRUNCATED_PDF = b"%PDF-1.4\ntrailer but no end marker\n"


@pytest.fixture(autouse=True)
def _clean_metrics():
    appcli_audit.reset_metrics()
    yield
    appcli_audit.reset_metrics()


def _load_handlers():
    path = resource_path("skills") / "appcli" / "handlers.py"
    spec = importlib.util.spec_from_file_location("_test_appcli_audit_handlers", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _document(riscv64="verified"):
    return {
        "version": 1,
        "appclies": [
            {
                "name": "demo",
                "display": "Demo",
                "entry_point": "cli-anything-demo",
                "channel": "artifact-only",
                "riscv64": riscv64,
                "actions": [
                    {
                        "name": "render",
                        "argv": ["export", "render", "{output}"],
                        "params": {"output": {"type": "str", "path": True}},
                        "artifacts": [{"param": "output", "verify": ["exists", "pdf_magic"]}],
                    }
                ],
            }
        ],
    }


@pytest.fixture
def handlers(tmp_path, monkeypatch):
    module = _load_handlers()
    path = tmp_path / "appcli_registry.yaml"
    path.write_text(yaml.safe_dump(_document(), allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(
        module.AppCliRegistry,
        "load",
        classmethod(lambda cls, _path=None: _ORIGINAL_LOAD(path)),
    )
    monkeypatch.setattr(module, "check_readiness", lambda spec: AppCliReadiness(True))
    monkeypatch.setenv("BIBIT_INTENT_APPCLI_ROOT", str(tmp_path / "workspace"))
    return module


def _records():
    return [item for item in dependency_audit.read_records(limit=50) if item.get("action") == "appcli_run"]


# ── 被拒的调用：有审计，但不计入 runs ────────────────────────────────

def test_refused_call_is_audited_but_not_counted_as_a_run(tmp_path, monkeypatch):
    module = _load_handlers()
    # 真实注册表：drawio 不在白名单（riscv64 上 unsupported，设备没有桌面后端）
    monkeypatch.setenv("BIBIT_INTENT_APPCLI_ROOT", str(tmp_path))

    result = module.appcli_run(appcli="drawio", action="export_render", args=["output=o.png"])

    assert not result.ok
    assert "尚未验收" in result.message

    record = _records()[0]
    assert record["stage"] == "refused"
    assert record["reason"] == "appcli_unverified"
    assert record["appcli"] == "drawio"
    assert record["target_action"] == "export_render"
    assert record["ok"] is False
    assert record["command"] == ""

    metrics = appcli_audit.appcli_metrics()
    assert metrics["runs"] == 0
    assert metrics["refusals"] == 1
    assert metrics["failures"] == 0
    assert metrics["by_appcli"]["drawio"] == {"runs": 0, "failures": 0, "refusals": 1}


def test_missing_prerequisite_is_refused_with_its_own_reason(handlers, monkeypatch):
    monkeypatch.setattr(
        handlers,
        "check_readiness",
        lambda spec: AppCliReadiness(False, missing_apt=("libreoffice-writer",)),
    )
    result = handlers.appcli_run(appcli="demo", action="render", args=["output=o.pdf"])

    assert not result.ok
    record = _records()[0]
    assert record["stage"] == "refused"
    assert record["reason"] == "missing_prereq"
    assert appcli_audit.appcli_metrics()["runs"] == 0


def test_host_channel_action_is_refused_with_channel_reason(handlers, monkeypatch):
    # 沙箱判定在 core.appcli_execution 里单独测；这里只固定「通道不符 → escalated +
    # 审计 reason=needs_host_channel，且不计入 runs」这条接线。
    monkeypatch.setattr(handlers, "channel_allowed", lambda channel, **_: False)
    result = handlers.appcli_run(appcli="demo", action="render", args=["output=o.pdf"])

    assert result.escalate == "needs_host_channel"
    record = _records()[0]
    assert record["reason"] == "needs_host_channel"
    assert record["stage"] == "refused"


def test_invalid_args_before_registry_lookup_are_audited(handlers):
    result = handlers.appcli_run(appcli="demo", action="render", args=["nope=1"])

    assert not result.ok
    record = _records()[0]
    assert record["stage"] == "refused"
    assert record["reason"] == "invalid_args"


# ── 执行的调用：命令、产物证据、耗时 ────────────────────────────────

def test_successful_run_audits_command_artifact_and_metrics(handlers, monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def runner(command, *, timeout, max_output_bytes, env=None):
        workspace.joinpath("report.pdf").write_bytes(_VALID_PDF)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"file_size": len(_VALID_PDF)}), stderr="")

    monkeypatch.setattr(handlers, "run_bounded_command", runner)

    result = handlers.appcli_run(appcli="demo", action="render", args=["output=report.pdf"])

    assert result.ok
    record = _records()[0]
    assert record["stage"] == "executed"
    assert record["ok"] is True
    assert record["reason"] == ""
    assert "cli-anything-demo --json export render" in record["command"]
    assert record["duration_ms"] >= 0
    assert record["artifacts"] == 1
    assert record["verify_failures"] == 0
    output = record["outputs"][0]
    assert output["path"].endswith("report.pdf")
    assert output["ok"] is True
    assert {item["rule"] for item in output["rules"]} == {"exists", "pdf_magic"}
    assert all(item["ok"] for item in output["rules"])

    metrics = appcli_audit.appcli_metrics()
    assert metrics["runs"] == 1
    assert metrics["refusals"] == 0
    assert metrics["failures"] == 0
    assert metrics["artifacts"] == 1
    assert metrics["failure_rate"] == 0.0
    assert metrics["by_appcli"]["demo"]["runs"] == 1


def test_verify_failure_is_counted_separately_from_other_failures(handlers, monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def runner(command, *, timeout, max_output_bytes, env=None):
        workspace.joinpath("half.pdf").write_bytes(_TRUNCATED_PDF)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(handlers, "run_bounded_command", runner)

    result = handlers.appcli_run(appcli="demo", action="render", args=["output=half.pdf"])

    assert not result.ok
    record = _records()[0]
    assert record["stage"] == "executed"
    assert record["reason"] == "verify_failed"
    assert record["verify_failures"] == 1
    assert record["artifacts"] == 0
    failed_rules = [item["rule"] for item in record["outputs"][0]["rules"] if not item["ok"]]
    assert failed_rules == ["pdf_magic"]

    metrics = appcli_audit.appcli_metrics()
    assert metrics["runs"] == 1
    assert metrics["failures"] == 1
    assert metrics["verify_failures"] == 1
    assert metrics["verify_failure_rate"] == 1.0


def test_nonzero_exit_is_audited_with_its_reason(handlers, monkeypatch):
    monkeypatch.setattr(
        handlers,
        "run_bounded_command",
        lambda command, **_: SimpleNamespace(returncode=2, stdout="", stderr="boom"),
    )
    result = handlers.appcli_run(appcli="demo", action="render", args=["output=o.pdf"])

    assert not result.ok
    record = _records()[0]
    assert record["reason"] == "exit_nonzero"
    assert appcli_audit.appcli_metrics()["failures"] == 1
    assert appcli_audit.appcli_metrics()["verify_failures"] == 0


# ── 写失败不影响执行结果 ─────────────────────────────────────────────

def test_unwritable_audit_path_does_not_change_the_result(handlers, monkeypatch, tmp_path):
    # 审计路径的父目录是一个普通文件：mkdir 会抛 NotADirectoryError（OSError 子类）。
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("BIBIT_INTENT_DEPENDENCY_AUDIT", str(blocker / "audit.jsonl"))

    monkeypatch.setattr(
        handlers,
        "run_bounded_command",
        lambda command, **_: SimpleNamespace(returncode=0, stdout="{}", stderr=""),
    )
    result = handlers.appcli_run(appcli="demo", action="render", args=["output=o.pdf"])

    # 产物校验会失败（没有文件），但这是执行结果本身，不是审计造成的。
    assert "校验" in result.message or not result.ok
    assert appcli_audit.appcli_metrics()["runs"] == 1


def test_audit_layer_exception_never_propagates(handlers, monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("report.pdf").write_bytes(_VALID_PDF)
    monkeypatch.setattr(
        handlers,
        "run_bounded_command",
        lambda command, **_: SimpleNamespace(
            returncode=0, stdout=json.dumps({"ok": True}), stderr=""
        ),
    )

    def exploding(entry):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(appcli_audit, "record", exploding)

    result = handlers.appcli_run(appcli="demo", action="render", args=["output=report.pdf"])

    assert result.ok, "审计层异常绝不能改变执行结果"
    assert appcli_audit.appcli_metrics()["runs"] == 1


# ── 指标本身 ─────────────────────────────────────────────────────────

def test_metrics_are_deep_copied_and_resettable():
    appcli_audit.record_run(
        appcli="demo", target_action="render", stage="executed", ok=True, artifacts=2
    )
    snapshot = appcli_audit.appcli_metrics()
    snapshot["by_appcli"]["demo"]["runs"] = 999

    assert appcli_audit.appcli_metrics()["by_appcli"]["demo"]["runs"] == 1

    appcli_audit.reset_metrics()
    assert appcli_audit.appcli_metrics()["by_appcli"] == {}


def test_stage_and_counts_are_clamped_for_bad_input():
    entry = appcli_audit.record_run(
        appcli="demo",
        target_action="render",
        stage="whatever",
        ok=False,
        outputs=({"path": "a"},),
        verify_failed=-3,
        artifacts=-1,
        duration_ms=-5,
    )
    assert entry["stage"] == "refused"
    assert entry["verify_failures"] == 0
    assert entry["artifacts"] == 0
    assert entry["duration_ms"] == 0


def test_records_share_the_dependency_audit_file(handlers, tmp_path):
    """appcli 与依赖安装共用一个 JSONL，靠 action 字段区分（便于统一回放）。"""
    dependency_audit.record({"action": "dep_apply", "plan_id": "dep-1"})
    handlers.appcli_run(appcli="demo", action="render", args=["output=o.pdf"])

    actions = [item["action"] for item in dependency_audit.read_records(limit=10)]
    assert actions[:2] == ["appcli_run", "dep_apply"]


def test_pipeline_metrics_include_the_appcli_section():
    from bibit_intent.core.registry import ToolRegistry
    from bibit_intent.execution.executor import Executor
    from bibit_intent.pipeline import EmbeddedBibitPipeline
    from bibit_intent.routing.router import Router
    from bibit_intent.routing.rule_engine import RuleEngine

    registry = ToolRegistry()
    router = Router(registry, RuleEngine(registry), None, Executor(registry, handlers={}))

    stats = EmbeddedBibitPipeline(router).stats()

    appcli = stats["appcli"]
    assert appcli["runs"] == 0
    assert appcli["refusals"] == 0
    assert appcli["failures"] == 0
    assert appcli["verify_failures"] == 0
    assert appcli["artifacts"] == 0
    assert appcli["by_appcli"] == {}
    assert appcli["failure_rate"] is None
