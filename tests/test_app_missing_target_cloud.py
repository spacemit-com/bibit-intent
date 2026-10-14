import pytest

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.core.session import SessionState
from bibit_intent.execution.executor import Executor
from bibit_intent.pipeline import EmbeddedBibitPipeline
from bibit_intent.routing import router as router_module
from bibit_intent.routing.intent import IntentAction, IntentPlan
from bibit_intent.routing.router import Router, Stage
from bibit_intent.routing.rule_engine import RuleEngine


@pytest.fixture
def router():
    registry = ToolRegistry()

    def must_not_execute(**params):
        raise AssertionError("Missing targets must never execute application handlers")

    handlers = {name: must_not_execute for name in ("launch_app", "close_app", "launch_apps", "close_apps")}
    return Router(registry, RuleEngine(registry), None, Executor(registry, handlers=handlers))


@pytest.mark.parametrize("text", [
    "打开应用", "帮我打开一个应用", "能帮我打开应用吗", "启动软件",
    "关闭应用", "退出程序", "打开", "请打开一下应用", "open an app",
    "can you open an application?", "please close the app", "launch",
])
def test_missing_target_returns_unhandled(router, text):
    result = EmbeddedBibitPipeline(router).handle(text, "missing")
    assert not result.handled
    assert result.route == "cloud"
    assert "还差" not in result.reply


@pytest.mark.parametrize("tool", ["launch_app", "close_app", "launch_apps", "close_apps"])
def test_rule_missing_target_is_cloud_not_pending(router, monkeypatch, tool):
    monkeypatch.setattr(router_module, "decide_local_intent", lambda *args, **kwargs: IntentPlan(
        action=IntentAction.RULE_CALL, tool_name=tool, slots={},
    ))
    session = SessionState()
    result = router.handle("应用操作", session)
    assert result.stage is Stage.CLOUD
    assert result.escalate_reason == "missing_app_target"
    assert session.pending is None


@pytest.mark.parametrize("tool", ["launch_app", "close_app", "launch_apps", "close_apps"])
def test_legacy_pending_is_forwarded_without_local_fill(router, tool):
    session = SessionState()
    key = "app_names" if tool.endswith("apps") else "app_name"
    session.set_pending(tool, key, "应用名", {}, "请补充应用名")
    result = router.handle("终端", session)
    assert result.stage is Stage.CLOUD
    assert result.escalate_reason == "missing_app_target"
    assert session.pending is None


def test_bound_missing_target_is_cloud(router, monkeypatch):
    monkeypatch.setattr(router, "_handle", lambda text, session: router._bound_missing_param(
        text, "launch_app", session, 0,
    ))
    session = SessionState()
    result = router.handle("打开应用", session)
    assert result.stage is Stage.CLOUD
    assert session.pending is None


def test_other_tools_keep_existing_local_clarification(router, monkeypatch):
    monkeypatch.setattr(router_module, "decide_local_intent", lambda *args, **kwargs: IntentPlan(
        action=IntentAction.RULE_CALL, tool_name="set_resolution", slots={},
    ))
    session = SessionState()
    result = router.handle("设置分辨率", session)
    assert result.stage is Stage.RULE
    assert result.exec_result.need_param == "mode"
    assert session.pending.tool_name == "set_resolution"


def test_executor_still_reports_schema_errors_for_mcp(router):
    result = router._executor.execute("launch_app", {})
    assert not result.ok and result.need_param == "app_name"


@pytest.mark.parametrize("tool,params", [
    ("launch_app", {"app_name": " "}), ("close_app", {"app_name": ""}),
    ("launch_apps", {"app_names": []}), ("close_apps", {"app_names": " , "}),
])
def test_blank_app_targets_are_missing_before_execution(router, monkeypatch, tool, params):
    monkeypatch.setattr(router_module, "decide_local_intent", lambda *args, **kwargs: IntentPlan(
        action=IntentAction.RULE_CALL, tool_name=tool, slots=params,
    ))
    result = router.handle("应用操作", SessionState())
    assert result.stage is Stage.CLOUD
    assert result.escalate_reason == "missing_app_target"
