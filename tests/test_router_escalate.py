"""Router 本地模型执行路径收到 escalate 时应转云端。"""

from __future__ import annotations

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.executor import Executor
from bibit_intent.execution.system_tools import ExecResult
from bibit_intent.models.local_model import LocalModelResult
from bibit_intent.routing import router as router_module
from bibit_intent.routing.intent import IntentAction, IntentPlan
from bibit_intent.routing.router import Router, Stage
from bibit_intent.routing.rule_engine import RuleEngine


def test_router_local_tool_escalate_goes_cloud(monkeypatch):
    registry = ToolRegistry()

    def fake_launch_app(**kwargs):
        return ExecResult(False, "应用未找到", escalate="app_not_found")

    executor = Executor(registry, handlers={"launch_app": fake_launch_app})
    rule = RuleEngine(registry)

    class StubLocal:
        def is_available(self):
            return True

        def infer(self, user_input, tool_list, analysis=None, max_output_tokens=30, **_kwargs):
            return LocalModelResult(
                action="CALL",
                tool_name="launch_app",
                params={"app_name": "不存在"},
                raw="CALL launch_app app_name=不存在",
            )

    router = Router(registry, rule, StubLocal(), executor)
    plan = IntentPlan(
        action=IntentAction.TO_MODEL,
        candidates=["launch_app"],
        tool_list="launch_app(app_name)",
        hint="launch_app",
        hint_source="agreed",
    )
    monkeypatch.setattr(router_module, "decide_local_intent", lambda *a, **k: plan)

    result = router.handle("打开不存在")
    assert result.stage is Stage.CLOUD
    assert result.escalate_reason == "app_not_found"


def test_router_english_open_question_goes_cloud():
    registry = ToolRegistry()
    router = Router(registry, RuleEngine(registry), None, Executor(registry))
    result = router.handle("What is quantum entanglement?")
    assert result.stage is Stage.CLOUD
    assert result.escalate_reason == "open_question"


def test_router_english_negated_command_is_not_executed():
    registry = ToolRegistry()
    router = Router(registry, RuleEngine(registry), None, Executor(registry))
    result = router.handle("don't close the browser")
    assert result.stage is Stage.CLOUD
    assert result.escalate_reason in {"negated_action", "out_of_scope"}


def test_router_english_rule_command_stays_local():
    registry = ToolRegistry()
    router = Router(registry, RuleEngine(registry), None, Executor(registry))
    result = router.handle("set volume to 60")
    assert result.stage is Stage.RULE
    assert result.tool_name == "set_volume"


def test_router_rule_tool_is_executed_once(monkeypatch):
    # 回归：RULE_CALL 若重复执行，close_app 第二次会误报"未找到"。
    registry = ToolRegistry()
    calls: list[dict] = []

    def fake_close_app(**kwargs):
        calls.append(kwargs)
        return ExecResult(True, "已关闭应用")

    executor = Executor(registry, handlers={"close_app": fake_close_app})
    router = Router(registry, RuleEngine(registry), None, executor)
    result = router.handle("关闭控制中心")
    assert result.stage is Stage.RULE
    assert result.tool_name == "close_app"
    assert calls == [{"app_name": "控制中心"}]


def test_router_close_window_phrases_not_routed_to_close_app():
    # 负向前瞻与 launch_app 对齐后，"关闭所有窗口/应用列表"不再被 close_app
    # 当成应用名吞掉（本地无匹配规则 → 无模型时升级云端，由 window/云端语义处理）。
    registry = ToolRegistry()

    class OfflineLocal:
        def is_available(self):
            return False

    router = Router(registry, RuleEngine(registry), OfflineLocal(), Executor(registry))
    for text in ("关闭所有窗口", "关闭应用列表", "关闭蓝牙"):
        result = router.handle(text)
        assert not (result.stage is Stage.RULE and result.tool_name == "close_app")


def test_router_close_camera_goes_to_camera_tool():
    # "关闭相机"交给 camera_close（负向前瞻排除 相机），不是 close_app
    registry = ToolRegistry()
    router = Router(registry, RuleEngine(registry), None, Executor(registry))
    result = router.handle("关闭相机")
    assert result.stage is Stage.RULE
    assert result.tool_name == "camera_close"


def test_router_close_single_app_still_local():
    # "关闭控制中心/终端"仍是 close_app 的本地规则职责
    registry = ToolRegistry()

    def fake_close_app(**kwargs):
        return ExecResult(True, "已关闭应用")

    executor = Executor(registry, handlers={"close_app": fake_close_app})
    router = Router(registry, RuleEngine(registry), None, executor)
    for text in ("关闭控制中心", "关闭终端", "关闭文件管理器"):
        result = router.handle(text)
        assert result.stage is Stage.RULE
        assert result.tool_name == "close_app"


def test_router_multi_launch_app_splits_locally():
    # 回归："打开终端和浏览器"原来整串丢给 launch_app → app_not_found 上云，
    # 云端没调用工具却复述成功。现在规则层直接拆成 launch_apps。
    registry = ToolRegistry()
    calls: list[dict] = []

    def fake_launch_apps(**kwargs):
        calls.append(kwargs)
        return ExecResult(True, "已启动 2 个应用")

    executor = Executor(registry, handlers={"launch_apps": fake_launch_apps})
    router = Router(registry, RuleEngine(registry), None, executor)
    result = router.handle("打开终端和浏览器")
    assert result.stage is Stage.RULE
    assert result.tool_name == "launch_apps"
    assert result.params == {"app_names": ["终端", "浏览器"]}
    assert calls == [{"app_names": ["终端", "浏览器"]}]
    assert result.debug.get("multi_app_split") == ["终端", "浏览器"]


def test_router_multi_close_app_splits_locally():
    registry = ToolRegistry()
    calls: list[dict] = []

    def fake_close_apps(**kwargs):
        calls.append(kwargs)
        return ExecResult(True, "已关闭 2 个应用")

    executor = Executor(registry, handlers={"close_apps": fake_close_apps})
    router = Router(registry, RuleEngine(registry), None, executor)
    result = router.handle("关闭终端和浏览器")
    assert result.stage is Stage.RULE
    assert result.tool_name == "close_apps"
    assert result.params == {"app_names": ["终端", "浏览器"]}
    assert calls == [{"app_names": ["终端", "浏览器"]}]


def test_router_single_app_still_uses_launch_app(monkeypatch):
    registry = ToolRegistry()
    calls: list[dict] = []

    def fake_launch_app(**kwargs):
        calls.append(kwargs)
        return ExecResult(True, "已启动应用")

    executor = Executor(registry, handlers={"launch_app": fake_launch_app})
    router = Router(registry, RuleEngine(registry), None, executor)
    result = router.handle("打开终端")
    assert result.stage is Stage.RULE
    assert result.tool_name == "launch_app"
    assert calls == [{"app_name": "终端"}]
