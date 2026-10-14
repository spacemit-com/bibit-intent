"""云端多 tool_calls 回归：_tool_mode 不再丢后续调用，router 按序聚合执行。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.executor import Executor
from bibit_intent.execution.system_tools import ExecResult
from bibit_intent.resources import resource_path
from bibit_intent.routing.router import Router


def _client():
    from bibit_intent.models.cloud_model import CloudModelClient
    cfg = SimpleNamespace(
        cloud_api_key="test", cloud_base_url="http://localhost", cloud_model="m"
    )
    return CloudModelClient(cfg, ToolRegistry(resource_path("tools.yaml")))


def _resp(*tool_calls):
    msg = SimpleNamespace(tool_calls=tool_calls or None, content="(云端无明确动作)")
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def test_tool_mode_collects_all_tool_calls():
    fake = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kw: _resp(
                SimpleNamespace(function=SimpleNamespace(
                    name="launch_app", arguments='{"app_name": "终端"}')),
                SimpleNamespace(function=SimpleNamespace(
                    name="launch_app", arguments='{"app_name": "设置"}')),
            )
        ))
    )
    out = _client()._tool_mode(fake, "打开终端和设置", language="zh")
    assert out["mode"] == "tool"
    assert len(out["calls"]) == 2
    assert out["tool"] == "launch_app"
    assert out["params"] == {"app_name": "终端"}
    assert out["calls"][1]["params"] == {"app_name": "设置"}


def test_tool_mode_no_calls_falls_back_to_content():
    fake = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: _resp()))
    )
    out = _client()._tool_mode(fake, "你好", language="zh")
    assert out["tool"] is None
    assert out["calls"] == []


class _FakeRule:
    def attach_stats(self, stats):
        pass


class _FakeCloud:
    def __init__(self, calls):
        self._calls = calls

    def handle(self, user_input, reason="", language=None):
        return {"ok": True, "message": "x", "mode": "tool", "calls": self._calls}


def test_router_executes_multi_cloud_calls():
    executed: list[str] = []

    def fake_launch(**kw):
        executed.append(kw["app_name"])
        return ExecResult(True, f"ok:{kw['app_name']}")

    registry = ToolRegistry(resource_path("tools.yaml"))
    executor = Executor(
        registry,
        confirm_callback=lambda _: True,
        handlers={"launch_app": fake_launch},
    )
    cloud = _FakeCloud([
        {"tool": "launch_app", "params": {"app_name": "终端"}},
        {"tool": "launch_app", "params": {"app_name": "设置"}},
    ])
    router = Router(
        registry, rule_engine=_FakeRule(), local_client=None,
        executor=executor, cloud_client=cloud,
    )
    result = router._cloud_fallback("打开终端和设置", "multi", 0.0)
    assert executed == ["终端", "设置"]
    assert result.tool_name is None
    assert "终端" in result.exec_result.message
    assert "设置" in result.exec_result.message
