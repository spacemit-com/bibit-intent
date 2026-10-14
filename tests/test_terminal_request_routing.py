import pytest

from bibit_intent.core.app_request import normalize_app_request
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.executor import Executor
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.execution.system_tools import ExecResult
from bibit_intent.pipeline import EmbeddedBibitPipeline
from bibit_intent.resources import resource_path
from bibit_intent.routing.intent import IntentAction, decide_local_intent
from bibit_intent.routing.multi_action import detect_multi_action
from bibit_intent.routing.router import Router, _deterministic_app_name
from bibit_intent.routing.rule_engine import RuleEngine


@pytest.fixture
def services():
    registry = ToolRegistry()
    load_skills(resource_path("skills"), registry, {})
    calls = []

    def launch(**params):
        calls.append(("launch_app", params))
        return ExecResult(True, "已打开终端")

    def query(**params):
        calls.append(("net_ip", params))
        return ExecResult(True, "IP 查询结果")

    rule = RuleEngine(registry)
    router = Router(registry, rule, None, Executor(registry, handlers={"launch_app": launch, "net_ip": query}))
    return registry, rule, EmbeddedBibitPipeline(router), calls


@pytest.mark.parametrize("text", [
    "你能帮我打开终端进行查询ip吗", "打开终端进行查询ip", "打开终端查询ip",
    "打开终端然后查询ip", "帮我打开终端并查询IP地址", "打开终端，查询IP地址",
    "打开终端运行ip addr", "打开终端查一下ip", "打开终端来执行ip addr",
    "打开命令行查询IP地址", "启动qterminal运行ip addr",
    "你能帮我打开终端进行ip查询吗", "打开终端进行ip查询",
    "打开终端进行 IP 地址查询", "启动终端做网络诊断",
    "打开终端进行内存检查", "打开终端进行磁盘检测",
])
def test_terminal_chain_hands_off_without_local_execution(services, text):
    registry, rule, pipeline, calls = services
    assert detect_multi_action(text).is_multi
    assert _deterministic_app_name(text, "launch_app") is None
    outcome = pipeline.handle(text, "chain")
    assert not outcome.handled
    assert outcome.reason == "multi_intent"
    assert calls == []


@pytest.mark.parametrize("text", [
    "你能帮我打开终端吗", "您能帮我打开终端吗？", "能不能帮我打开终端？",
    "可以帮我打开终端吗", "你能打开终端吗", "打开终端",
])
def test_polite_terminal_request_needs_no_model_or_clarification(services, text):
    registry, rule, pipeline, calls = services
    assert _deterministic_app_name(text, "launch_app") == "终端"
    outcome = pipeline.handle(text, "single")
    assert outcome.handled and outcome.route == "rule"
    assert calls == [("launch_app", {"app_name": "终端"})]


@pytest.mark.parametrize("text", [
    "查询ip", "查一下ip", "查询 IP", "查看IP地址", "帮我查询ip",
    "请帮我查询本机IP地址", "查看本地ip", "查询我的ip地址是多少？",
])
def test_simple_ip_request_uses_local_readonly_tool(services, text):
    registry, rule, pipeline, calls = services
    outcome = pipeline.handle(text, "ip")
    assert outcome.handled and outcome.route == "rule"
    assert calls == [("net_ip", {})]


@pytest.mark.parametrize("text", [
    "打开终端查看器", "打开查询工具", "打开运行管理器", "启动终端",
])
def test_application_names_are_not_terminal_chains(text):
    assert not detect_multi_action(text).is_multi


@pytest.mark.parametrize("text", [
    "在终端里查询IP地址", "查询公网ip", "查询无线ip", "查询有线ip",
    "查询ip归属地", "不要查询ip", "查询ip并发送给别人",
])
def test_generic_ip_rule_does_not_swallow_other_tasks(services, text):
    registry, rule, pipeline, calls = services
    plan = decide_local_intent(text, rule, registry)
    assert not (plan.action is IntentAction.RULE_CALL and plan.tool_name == "net_ip")


@pytest.mark.parametrize("text", [
    "你能帮我不要打开终端吗", "为什么不能打开终端", "你能解释打开终端的作用吗",
])
def test_nonrequests_are_not_normalized(text):
    assert normalize_app_request(text) == text


def test_nominal_terminal_request_replaces_pending_app(services):
    from bibit_intent.core.session import SessionState
    from bibit_intent.routing.router import Stage

    registry, rule, pipeline, calls = services
    session = SessionState()
    session.set_pending("launch_app", "app_name", "应用名", {}, "请补充应用名")
    router = Router(registry, rule, None, Executor(registry, handlers={}))
    result = router.handle("你能帮我打开终端进行ip查询吗", session=session)
    assert result.stage is Stage.CLOUD
    assert result.escalate_reason == "multi_intent"
    assert session.pending is None


@pytest.mark.parametrize("text", ["打开终端进行查询工具", "打开终端进行IP查询器"])
def test_nominal_application_names_are_not_action_chains(text):
    assert not detect_multi_action(text).is_multi
