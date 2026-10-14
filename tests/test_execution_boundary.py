"""escalate 契约回归：本地未执行任何变更时必须撤回 execution_started 标记。

背景：'打开微信' / '关闭微信' / '安装 XXX' 在应用未安装、包名不在源中、无图形
授权框等场景会返回 ``escalate``，由路由层升级云端。若执行层仍留着
``execution_started`` 标记，HTTP 边界（server.uncertain_outcome）会把这次云端
兜底改判成"端侧操作可能已部分执行；为避免重复操作，本次不再转交云端"，
用户既看不到本地原因，也拿不到云端兜底。
"""
from __future__ import annotations

import pytest

import bibit_intent.execution.system_tools as st
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.core.session import SessionState
from bibit_intent.execution.cancellation import (
    cancellation_scope,
    EdgeExecutionControl,
)
from bibit_intent.execution.executor import Executor
from bibit_intent.execution.system_tools import ExecResult
from bibit_intent.routing.router import Router, Stage
from bibit_intent.routing.rule_engine import RuleEngine


def _executor(handler) -> Executor:
    registry = ToolRegistry()
    return Executor(registry, handlers={"launch_app": handler})


def test_escalate_withdraws_execution_started_mark():
    # 应用未安装：没有设备变更 → 不能报告"可能已部分执行"
    executor = _executor(
        lambda **_: ExecResult(False, "应用未找到: 微信", escalate="app_not_found")
    )
    control = EdgeExecutionControl()
    with cancellation_scope(control):
        result = executor.execute("launch_app", {"app_name": "微信"})
    assert result.escalate == "app_not_found"
    assert control.execution_started.is_set() is False


def test_successful_side_effect_keeps_execution_started_mark():
    executor = _executor(lambda **_: ExecResult(True, "已启动应用: 微信"))
    control = EdgeExecutionControl()
    with cancellation_scope(control):
        result = executor.execute("launch_app", {"app_name": "微信"})
    assert result.ok is True
    assert control.execution_started.is_set() is True


def test_mid_execution_failure_keeps_mark_for_uncertain_outcome():
    # 真正"可能已部分执行"：处理器抛异常，标记必须保留，边界才敢判 uncertain
    def boom(**_):
        raise RuntimeError("post-side-effect crash")

    executor = _executor(boom)
    control = EdgeExecutionControl()
    with cancellation_scope(control):
        with pytest.raises(RuntimeError):
            executor.execute("launch_app", {"app_name": "微信"})
    assert control.execution_started.is_set() is True


def test_open_wechat_routes_to_cloud_without_partial_report(monkeypatch):
    # 直接复现线上现象：'打开微信' 未安装 → 规则命中 launch_app → app_not_found
    # 应升级云端，而不是被边界改判成"端侧操作可能已部分执行"。
    monkeypatch.setattr(st, "_find_desktop_session_env", lambda: None)
    monkeypatch.setattr(st, "_get_desktop_apps", lambda: [])
    monkeypatch.setattr(st, "_match_desktop_app", lambda query, apps: None)
    monkeypatch.setattr(st, "_suggest_app_names", lambda query, limit=3: [])
    monkeypatch.setattr(st, "_bare_command_path", lambda name: None)

    registry = ToolRegistry()
    router = Router(registry, RuleEngine(registry), None, Executor(registry))
    control = EdgeExecutionControl()
    with cancellation_scope(control):
        result = router.handle("打开微信", SessionState())

    assert result.stage is Stage.CLOUD
    assert result.escalate_reason == "app_not_found"
    assert control.execution_started.is_set() is False
