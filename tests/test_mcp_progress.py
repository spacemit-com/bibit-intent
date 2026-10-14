"""MCP 进度上报：线程桥 + 工具端上报 + FastMCP Context 注入签名。

对应 docs/design/dependency_management.md M2：Runtime 已经能接收
notifications/progress（见 bianbu-agent-runtime 的
docs/j12-riscv-delivery-runtime-interfaces.md），这里补齐服务端真正上报的那一侧。
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bibit_intent.core import dependency_scan as ds
from bibit_intent.core.progress import progress_bridge, report_progress
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.execution.system_tools import HANDLERS as BUILTIN_HANDLERS
from bibit_intent.mcp_server.server import (
    _CONTEXT_PARAM,
    _invoke_parameters,
    _progress_context_type,
)
from bibit_intent.resources import resource_path


class _FakeContext:
    """占位 Context 类型：注入识别只要求是类且带注解。"""


# ── 进度桥 ──────────────────────────────────────────────────────────

def test_report_progress_without_bridge_is_a_noop():
    # 直接调 handler（HTTP / 规则路径）时没有进度桥，绝不能因此抛异常
    report_progress(1, 2, "no bridge")


def test_report_progress_accepts_unparsable_values():
    report_progress("not-a-number")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_bridge_forwards_progress_from_worker_thread():
    seen: list[tuple[float, float | None, str | None]] = []

    async def emitter(progress, total, message):
        seen.append((progress, total, message))

    async with progress_bridge(emitter):
        await asyncio.to_thread(report_progress, 1, 3, "a")
        await asyncio.to_thread(report_progress, 2, 3, "b")

    assert seen == [(1.0, 3, "a"), (2.0, 3, "b")]


@pytest.mark.asyncio
async def test_bridge_drops_progress_after_close_and_without_emitter():
    seen: list[object] = []
    async with progress_bridge(None):
        report_progress(1)
    async with progress_bridge(lambda p, t, m: seen.append(p) or _coro()):
        report_progress(1)
    report_progress(9)  # 桥已关闭
    assert seen == [1]


async def _coro():
    return None


@pytest.mark.asyncio
async def test_emitter_failure_does_not_reach_the_caller(caplog):
    calls = {"n": 0}

    async def boom(progress, total, message):
        calls["n"] += 1
        raise RuntimeError("transport gone")

    async with progress_bridge(boom):
        # 上报失败不能让正在执行的安装抛异常
        await asyncio.to_thread(report_progress, 1, 1, "x")
        await asyncio.to_thread(report_progress, 2, 2, "y")

    assert calls["n"] == 1


# ── scan_project 的进度回调 ─────────────────────────────────────────

def test_scan_project_reports_progress_and_survives_callback_error(tmp_path):
    for index in range(3):
        (tmp_path / f"m{index}.py").write_text("import six\n", encoding="utf-8")

    seen: list[tuple[int, int]] = []
    result = ds.scan_project(tmp_path, on_progress=lambda done, total: seen.append((done, total)))
    assert seen == [(3, 3)]
    assert result["files_scanned"] == 3

    def boom(done, total):
        raise RuntimeError("no listener")

    assert ds.scan_project(tmp_path, on_progress=boom)["files_scanned"] == 3


# ── FastMCP Context 注入签名 ────────────────────────────────────────

@pytest.fixture(scope="module")
def registry():
    reg = ToolRegistry(resource_path("tools.yaml"))
    load_skills(resource_path("skills"), reg, dict(BUILTIN_HANDLERS))
    return reg


def test_context_param_is_injected_into_the_tool_signature(registry):
    parameters, enabled = _invoke_parameters(registry.get("dep_scan"), _FakeContext)

    assert enabled is True
    assert [p.name for p in parameters] == ["path", "limit", _CONTEXT_PARAM]
    assert parameters[-1].annotation is _FakeContext


def test_context_injection_is_skipped_without_a_context_type(registry):
    parameters, enabled = _invoke_parameters(registry.get("dep_scan"), None)

    assert enabled is False
    assert [p.name for p in parameters] == ["path", "limit"]


def test_context_injection_never_shadows_a_tool_argument():
    from types import SimpleNamespace

    param = SimpleNamespace(type="str", default=None)

    class _Tool:
        params = {_CONTEXT_PARAM: param, "path": param}

    parameters, enabled = _invoke_parameters(_Tool(), _FakeContext)

    # 放弃注入，保留工具自己的同名参数（只出现一次，且不是 Context 注解）
    assert enabled is False
    names = [p.name for p in parameters]
    assert names.count(_CONTEXT_PARAM) == 1
    assert parameters[0].annotation is not _FakeContext


def test_progress_context_type_matches_the_installed_sdk():
    context_type = _progress_context_type()
    if context_type is None:
        pytest.skip("MCP SDK 不可用或不提供上下文注入")
    # 必须是类，注入识别走 issubclass(annotation, Context)
    assert inspect.isclass(context_type)


# ── dep_apply 逐项上报（走线程桥，模拟 MCP 调用路径）────────────────

def _load_admin_handlers():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_test_admin_handlers_progress",
        resource_path("skills") / "ubuntu_admin" / "handlers.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_dep_apply_reports_progress_for_each_item(tmp_path, monkeypatch):
    from bibit_intent.core import dependency_plan as dp

    admin = _load_admin_handlers()
    requirement = ds.Requirement(kind=ds.KIND_C_HEADER, name="openssl/ssl.h")
    statuses = {requirement.key(): {"satisfied": False, "check": "probe", "detail": ""}}
    monkeypatch.setattr(dp, "_apt_facts", lambda pkg: {
        "resolvable": True, "version": "3.0.16", "size_bytes": 1024,
        "removes": [], "will_install": [pkg], "detail": "",
    })
    plan = dp.build_plan(tmp_path, [requirement], statuses)
    dp.save_plan(plan)
    monkeypatch.setattr(
        admin, "_run_sudo",
        lambda cmd, timeout=60, input=None: admin.ExecResult(True, "ok"),
    )
    monkeypatch.setattr(
        admin, "_verify_dep_item", lambda item, root, python: (True, "probe ok")
    )

    seen: list[tuple[float, float | None, str | None]] = []

    async def emitter(progress, total, message):
        seen.append((progress, total, message))

    async with progress_bridge(emitter):
        result = await asyncio.to_thread(
            admin.dep_apply, plan_id=plan["plan_id"], items="1"
        )

    assert result.ok is True
    assert [p for p, _, _ in seen] == [0.0, 0.0, 1.0]
    assert all(total == 1 for _, total, _ in seen)
    assert seen[0][2] == "准备安装 1 项依赖"
    assert "openssl/ssl.h" in seen[1][2]
    assert "已安装并验证" in seen[2][2]
