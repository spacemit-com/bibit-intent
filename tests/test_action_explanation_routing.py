import pytest

from bibit_intent.core.request_semantics import is_action_explanation, non_command_reason
from bibit_intent.mcp_server.server import _build_executor
from bibit_intent.routing.intent import IntentAction, decide_local_intent
from bibit_intent.routing.question_detector import QuestionDetector
from bibit_intent.routing.rule_engine import RuleEngine
from bibit_intent.execution.executor import Executor
from bibit_intent.pipeline import EmbeddedBibitPipeline
from bibit_intent.routing.router import Router


@pytest.fixture(scope="module")
def routing():
    registry, _ = _build_executor()
    return registry, RuleEngine(registry)


QUESTIONS = [
    "你知道我密码吗，为什么你能安装file2md app",
    "你知道我密码吗，为什么你能安装 file2md app？",
    "为什么你能安装file2md app",
    "为啥你不用密码就能安装file2md",
    "你为何可以直接安装file2md",
    "你为什么卸载了vim",
    "刚才为什么删除了report.pdf",
    "为什么你能访问我的文件",
    "你怎么在没有密码的情况下安装了file2md",
    "你凭什么关闭浏览器",
    "你是怎么拿到安装权限的",
    "你知道我的密码吗",
    "你保存了我的密码吗",
    "安装file2md是否需要密码",
    "你为什么能重启系统",
    "为什么下载需要管理员权限",
    "为什么我让你下载file2md，你却安装了它",
    "解释一下为什么能安装file2md，然后帮我安装vim",
    "帮我安装vim，再解释为什么你不需要密码",
    "Do you know my password, why can you install file2md app?",
    "Why could you install file2md without my password",
    "Why did you uninstall vim",
    "How were you able to install file2md",
    "How did you delete my file",
    "How come you can restart the system",
    "Do you have permission to install file2md?",
    "Do you know my password? Install file2md only after explaining this.",
    "Have you saved my password?",
    "你知道我的密码吗？先解释一下，再安装file2md",
    "你是不是用我的密码安装了file2md",
    "为什么不用sudo就能安装file2md",
    "你为什么执行了rm命令",
    "为啥你把音量设置为80",
    "为什么你关闭了防火墙",
]


@pytest.mark.parametrize("text", QUESTIONS)
def test_explanation_never_becomes_rule_call_or_tool_candidate(routing, text):
    registry, rule = routing

    def forbidden(*args, **kwargs):
        pytest.fail("explanation must not query package candidates or invoke models")

    assert non_command_reason(text) == "open_question"
    assert QuestionDetector().is_open_question(text)
    assert rule.match(text) is None
    assert rule.match_tool(text, "pkg_install") is None
    assert rule.match_tool(text, "pkg_remove") is None
    assert rule.keyword_candidates(text) == []
    class RejectEmbedding:
        def __getattr__(self, name):
            pytest.fail("explanation must not request embedding candidates")

    plan = decide_local_intent(text, rule, registry, pkg_resolver=forbidden, embed=RejectEmbedding())
    assert plan.action is IntentAction.ESCALATE
    assert plan.reason == "open_question"
    assert plan.tool_name is None
    assert plan.candidates == []


@pytest.mark.parametrize("text", QUESTIONS)
def test_pipeline_hands_explanation_to_context_without_execution(routing, text):
    registry, rule = routing

    def forbidden(*args, **kwargs):
        pytest.fail("explanation must not execute, ask installation confirmation, or call the local model")

    class RejectModel:
        infer = forbidden

    executor = Executor(registry, handlers={tool.name: forbidden for tool in registry.all()},
                        confirm_callback=forbidden)
    pipeline = EmbeddedBibitPipeline(Router(registry, rule, RejectModel(), executor))
    result = pipeline.handle(text, "explanation-regression")
    assert not result.handled
    assert result.route == "cloud"
    assert result.reason == "open_question"


@pytest.mark.parametrize("text,tool", [
    ("帮我安装file2md app", "pkg_install"),
    ("请安装vim", "pkg_install"),
    ("你能帮我安装vim吗", "pkg_install"),
    ("可以帮我安装file2md吗", "pkg_install"),
    ("卸载vim", "pkg_remove"),
    ("打开浏览器", "launch_app"),
    ("install file2md", "pkg_install"),
    ("please install vim", "pkg_install"),
    ("uninstall vim", "pkg_remove"),
])
def test_actual_commands_keep_existing_rule_routes(routing, text, tool):
    registry, rule = routing
    assert non_command_reason(text) is None
    plan = decide_local_intent(text, rule, registry)
    assert plan.action is IntentAction.RULE_CALL
    assert plan.tool_name == tool


@pytest.mark.parametrize("text", ["为什么没声音", "现在CPU占用高吗", "把声音开大一点", "安装why", "安装how"])
def test_unrelated_existing_control_and_query_semantics_remain(routing, text):
    registry, rule = routing
    assert non_command_reason(text) is None
    assert decide_local_intent(text, rule, registry).action is IntentAction.RULE_CALL


@pytest.mark.parametrize("text,tool", [("装的是什么", "pkg_info"), ("是什么文件", "file_info")])
def test_existing_readonly_keyword_recall_is_not_blanket_disabled(routing, text, tool):
    _, rule = routing
    assert not is_action_explanation(text)
    assert tool in rule.keyword_candidates(text)


@pytest.mark.parametrize("text", [
    "bianbu下如何替换kernel",
    "如何替换内核",
    "怎么替换内核",
    "如何编译内核",
    "如何降级内核",
])
def test_how_to_mutate_object_escalates_as_open_question(routing, text):
    registry, rule = routing
    assert non_command_reason(text) == "open_question"
    assert rule.keyword_candidates(text) == []
    plan = decide_local_intent(text, rule, registry)
    assert plan.action is IntentAction.ESCALATE
    assert plan.reason == "open_question"
    assert plan.candidates == []


@pytest.mark.parametrize("text", [
    "替换kernel",
    "帮我替换kernel",
    "更换kernel",
    "编译kernel",
    "降级kernel",
    "重装kernel",
    "把kernel替换了",
])
def test_readonly_kernel_tool_not_injected_for_mutation(routing, text):
    _, rule = routing
    assert "sys_kernel" not in rule.keyword_candidates(text)
    rm = rule.match(text)
    assert rm is None or rm.tool_name != "sys_kernel"


@pytest.mark.parametrize("text", ["查看替换后的内核版本", "查看修改过的内核信息"])
def test_attributive_mutation_is_still_read_query(routing, text):
    _, rule = routing
    assert "sys_kernel" in rule.keyword_candidates(text)


@pytest.mark.parametrize("text", ["查看内核版本", "内核信息", "当前kernel"])
def test_readonly_kernel_queries_still_route(routing, text):
    _, rule = routing
    assert "sys_kernel" in rule.keyword_candidates(text)


@pytest.mark.parametrize("text", ["切换kernel", "切换显示模式", "切换音频输出"])
def test_bare_switch_keyword_not_injected_for_non_window_objects(routing, text):
    _, rule = routing
    assert "window_activate" not in rule.keyword_candidates(text)


@pytest.mark.parametrize("text", ["切换到浏览器", "切换窗口", "激活浏览器"])
def test_switch_window_objects_still_inject_window_activate(routing, text):
    _, rule = routing
    assert "window_activate" in rule.keyword_candidates(text)


@pytest.mark.parametrize("text", [
    "Can you reset my password?", "Could you change the permissions on report.txt?",
    "Can you install vim?", "Could you help me install file2md?",
    "请修改我的密码", "帮我设置文件权限", "查询file2md是否已安装",
])
def test_polite_actions_and_status_queries_are_not_authority_explanations(text):
    assert not is_action_explanation(text)
    assert non_command_reason(text) is None
