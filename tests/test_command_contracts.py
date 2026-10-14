import pytest

from bibit_intent.core.command_contracts import command_contract_allows, domain_anchor_allows
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.executor import Executor
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.resources import resource_path
from bibit_intent.routing.complexity import Complexity, classify_complexity
from bibit_intent.routing.intent import IntentAction, decide_local_intent
from bibit_intent.routing.rule_engine import RuleEngine
from bibit_intent.routing.router import Router, Stage


@pytest.fixture(scope="module")
def routing():
    registry = ToolRegistry()
    load_skills(resource_path("skills"), registry, {})
    return registry, RuleEngine(registry)


NEGATIVE_CASES = [
    ("pkg_update", "更新：是每周一、三18:10–19:10 有瑜伽课"),
    ("pkg_update", "更新我的课程安排"),
    ("pkg_update", "更新客户资料"),
    ("pkg_update", "更新"),
    ("pkg_update", "更新软件的说明文档"),
    ("pkg_clean", "清理浏览器缓存"),
    ("pkg_clean", "清理缓存"),
    ("proc_zombie", "有僵尸进程"),
    ("user_del", "删除用户alice的聊天记录"),
    ("user_add", "创建用户alice的说明文档"),
    ("disk_format", "格式化/dev/sdb1的操作记录"),
    ("proc_kill", "结束2026年的课程"),
    ("user_del", "delete user alice's chat history"),
    ("pkg_update", "update the system documentation"),
]


@pytest.mark.parametrize("tool,text", NEGATIVE_CASES)
def test_open_objects_never_enter_local_execution(routing, tool, text):
    registry, rule = routing
    assert not command_contract_allows(tool, text)
    assert rule.match_tool(text, tool) is None
    assert tool not in rule.keyword_candidates(text)
    plan = decide_local_intent(text, rule, registry)
    assert plan.action is not IntentAction.RULE_CALL

    def reject_execution(**params):
        pytest.fail("No system handler may run for an unverified request")

    executor = Executor(registry, handlers={tool: reject_execution})
    class OfflineLocal:
        def is_available(self):
            return False

    router = Router(registry, rule, OfflineLocal(), executor)
    assert router.handle(text).stage is Stage.CLOUD
    result = router._execute_local_tool(tool, {}, text)
    assert result.escalate == "unverified_command"


@pytest.mark.parametrize("tool,text", [
    ("pkg_update", "更新系统"),
    ("pkg_update", "请帮我更新一下系统"),
    ("pkg_update", "系统更新"),
    ("pkg_update", "更新所有软件包"),
    ("pkg_update", "update the package lists"),
    ("pkg_clean", "清理软件缓存"),
    ("pkg_clean", "clean the apt cache"),
    ("proc_zombie", "清理僵尸进程"),
    ("proc_kill", "结束123号进程"),
    ("disk_format", "格式化/dev/sdb1"),
    ("user_del", "删除用户alice"),
    ("user_add", "创建用户alice"),
])
def test_closed_commands_remain_local(routing, tool, text):
    registry, rule = routing
    assert command_contract_allows(tool, text)
    plan = decide_local_intent(text, rule, registry)
    assert plan.action is IntentAction.RULE_CALL
    assert plan.tool_name == tool


@pytest.mark.parametrize("text", [
    "帮我把系统的软件都升级到最新版本",
    "请升级已安装的软件包",
    "清理 apt 的下载缓存",
])
def test_known_domain_variants_remain_available_to_local_model(routing, text):
    registry, rule = routing
    tool = "pkg_clean" if "缓存" in text else "pkg_update"
    assert not command_contract_allows(tool, text)
    assert domain_anchor_allows(tool, text)
    plan = decide_local_intent(text, rule, registry)
    assert plan.action is not IntentAction.RULE_CALL


@pytest.mark.parametrize("text", [
    "更新：是每周一、三18:10–19:10 有瑜伽课",
    "补充：明天课程取消",
    "Correction: yoga is on Monday",
])
def test_context_corrections_are_complex(text):
    result = classify_complexity(text)
    assert result.complexity is Complexity.COMPLEX
    assert result.escalation_reason == "context_dependent"


@pytest.mark.parametrize("suffix", ["的聊天记录", "的说明文档", "的操作记录", "之后通知我", "并打开浏览器"])
@pytest.mark.parametrize("tool,text", [
    ("pkg_update", "更新系统"),
    ("user_del", "删除用户alice"),
    ("disk_format", "格式化/dev/sdb1"),
])
def test_unexplained_suffixes_fail_closed(routing, tool, text, suffix):
    registry, rule = routing
    request = text + suffix
    assert not command_contract_allows(tool, request)
    assert decide_local_intent(request, rule, registry).action is IntentAction.ESCALATE
