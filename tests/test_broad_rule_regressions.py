import pytest

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.core.request_semantics import non_command_reason
from bibit_intent.execution.executor import Executor
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.resources import resource_path
from bibit_intent.routing.intent import IntentAction, decide_local_intent
from bibit_intent.routing.rule_engine import RuleEngine
from bibit_intent.routing.router import Router
from bibit_intent.pipeline import EmbeddedBibitPipeline


NONCOMMAND_CASES = [
    ("B01", "不要搜索本地文件"),
    ("B01", "不要本地搜索文档"),
    ("B01", "本地文件是什么"),
    ("B01", "本地搜索的原理是什么"),
    ("B01", "在本地搜索文件有风险吗"),
    ("B01", "帮我写一个搜索本地文件的脚本"),
    ("B01", "搜索本地文件然后发邮件"),
    ("B01", "本机有哪些软件"),
    ("B01", "本机有没有网络"),
    ("B02", "如何安装vim"),
    ("B02", "安装vim失败了"),
    ("B02", "已经安装vim了"),
    ("B02", "如果需要就安装vim"),
    ("B02", "如何卸载vim"),
    ("B02", "卸载vim会有什么影响"),
    ("B02", "如何重启nginx服务"),
    ("B02", "昨天重启nginx服务失败了"),
    ("B02", "如果出错就重启nginx服务"),
    ("B02", "如何停止nginx服务"),
    ("B02", "如何删除用户alice"),
    ("B02", "如何格式化/dev/sdb1"),
    ("B02", "如何杀死进程1234"),
    ("B02", "清理缓存是什么意思"),
    ("B02", "为什么需要清理缓存"),
    ("B02", "如果磁盘满了就清理缓存"),
    ("B03", "屏幕太暗是什么原因"),
    ("B03", "屏幕太暗会伤眼睛吗"),
    ("B03", "把声音调大有什么作用"),
    ("B04", "我还是无法访问，我的ip在10.0.90.153"),
    ("B04", "我的IP是10.0.90.153"),
    ("B04", "网络地址是什么"),
    ("B04", "屏幕亮度50是什么意思"),
    ("B05", "如何安装python库requests"),
    ("B05", "安装python库requests失败了"),
    ("B05", "如何安装test.deb"),
]


@pytest.fixture(scope="module")
def routing():
    registry = ToolRegistry()
    load_skills(resource_path("skills"), registry, {})
    return registry, RuleEngine(registry)


@pytest.mark.parametrize("text", [
    pytest.param(
        text,
        id=f"{issue}-{index:02d}",
    )
    for index, (issue, text) in enumerate(NONCOMMAND_CASES, 1)
])
def test_noncommands_escalate_without_tool_candidates(routing, text):
    registry, rule = routing

    class RejectEmbedding:
        def __getattr__(self, name):
            raise AssertionError("Noncommands must not request embeddings")

    def reject_package_resolution(*args):
        raise AssertionError("Noncommands must not resolve package candidates")

    plan = decide_local_intent(
        text, rule, registry, embed=RejectEmbedding(), pkg_resolver=reject_package_resolution,
    )
    assert plan.action is IntentAction.ESCALATE, (
        text, plan.action, plan.tool_name, plan.candidates, plan.matched_pattern,
    )
    assert plan.tool_name is None
    assert not plan.candidates


def test_explicit_query_after_failure_keeps_requested_tool(routing):
    registry, rule = routing
    plan = decide_local_intent("无法访问，帮我查一下本机IP", rule, registry)
    assert plan.action is IntentAction.RULE_CALL
    assert plan.tool_name == "net_ip"


@pytest.mark.parametrize("text,expected_tool,expected_slots", [
    ("查询本机IP", "net_ip", {}),
    ("查询ip", "net_ip", {}),
    ("打开终端", "launch_app", {"app_name": "终端"}),
    ("帮我安装vim", "pkg_install", {"pkg": "vim"}),
    ("卸载vim", "pkg_remove", {"pkg": "vim"}),
    ("帮我重启nginx服务", "service_restart", {"name": "nginx"}),
    ("本地搜索周报", "file_search", {"query": "本地搜索周报"}),
        ("屏幕太暗了", "adjust_brightness", {"direction": "up", "amount": 10}),
])
def test_explicit_requests_keep_local_fast_path(routing, text, expected_tool, expected_slots):
    registry, rule = routing
    plan = decide_local_intent(text, rule, registry)
    assert plan.action is IntentAction.RULE_CALL
    assert plan.tool_name == expected_tool
    assert plan.slots == expected_slots


@pytest.mark.parametrize("text", [
    "检查系统安全隐患",
    "检查有没有后门",
    "rootkit 扫描",
    "系统安全扫描",
    "扫一下 rootkit",
])
def test_security_scan_escalates_to_cloud(routing, text):
    registry, rule = routing
    plan = decide_local_intent(text, rule, registry)
    assert plan.action is IntentAction.ESCALATE
    assert plan.reason == "security_scan"
    assert plan.tool_name is None


@pytest.mark.parametrize("text", [
    "不要安装vim", "帮我安装vim和git", "能帮我打开终端进行 IP 查询吗",
    "终端查看IP的原理是什么", "我说的是查询ip，不是安装软件",
])
def test_existing_guards_still_escalate(routing, text):
    registry, rule = routing
    assert decide_local_intent(text, rule, registry).action is IntentAction.ESCALATE


@pytest.mark.parametrize("text", [
    "本地搜索failed.log", "本地搜索 if.log", "本地搜索网络故障日志",
    "帮我在本机搜索程序崩溃日志", "本地搜索网络配置文件",
])
def test_filename_and_topic_words_are_not_diagnostics(routing, text):
    registry, rule = routing
    assert non_command_reason(text) is None
    plan = decide_local_intent(text, rule, registry)
    assert plan.action is IntentAction.RULE_CALL
    assert plan.tool_name == "file_search"


@pytest.mark.parametrize("text,tool", [
    ("请告诉我如何重启nginx服务", "service_restart"),
    ("你知道怎么删除用户alice吗", "user_del"),
    ("how to install vim", "pkg_install"),
    ("install vim if needed", "pkg_install"),
    ("yesterday I installed vim", "pkg_install"),
    ("installation of vim failed", "pkg_install"),
])
def test_noncommand_guard_also_blocks_direct_slot_matching(routing, text, tool):
    registry, rule = routing
    assert rule.match(text) is None
    assert rule.match_tool(text, tool) is None
    assert decide_local_intent(text, rule, registry).action is IntentAction.ESCALATE


@pytest.mark.parametrize("text", [
    "我还是无法访问，我的ip在10.0.90.153",
    "如何安装vim", "不要搜索本地文件", "安装python库requests失败了",
    "如果出错就重启nginx服务", "屏幕太暗会伤眼睛吗",
])
def test_pipeline_never_executes_noncommands(routing, text):
    registry, rule = routing

    def reject_execution(*args, **kwargs):
        raise AssertionError("Noncommands must not execute a tool or call a local model")

    class RejectModel:
        infer = reject_execution

    executor = Executor(registry, handlers={tool.name: reject_execution for tool in registry.all()})
    pipeline = EmbeddedBibitPipeline(Router(registry, rule, RejectModel(), executor))
    outcome = pipeline.handle(text, "noncommand-test")
    assert not outcome.handled
    assert outcome.route == "cloud"


def test_unrelated_search_candidate_cannot_override_explicit_ip_query(routing):
    registry, rule = routing
    text = "无法访问，帮我查一下本机IP"
    assert rule.match_tool(text, "file_search") is None
    assert "file_search" not in rule.keyword_candidates(text)
    assert decide_local_intent(text, rule, registry).tool_name == "net_ip"


@pytest.mark.parametrize("text", [
    "重启网络服务",
    "重启进程",
    "重启一下网络服务",
    "帮我重启网络服务",
    "重新启动网络服务",
    "关机网络服务",
    "睡眠不好",
    "挂起进程",
])
def test_admin_broad_verb_keyword_rejects_foreign_object(routing, text):
    """reboot/shutdown/sleep 的裸动词关键词不得误注入陌生宾语。

    「重启网络服务」不能被「重启」子串兜底注入 reboot（整机重启），
    「睡眠不好」不能被「睡眠」注入 sleep。这些写动作应交对应服务工具或升云。
    """
    registry, rule = routing
    m = rule.match(text)
    assert (m is None) or (m.tool_name not in {"reboot", "shutdown", "sleep"})
    assert "reboot" not in rule.keyword_candidates(text)
    assert "shutdown" not in rule.keyword_candidates(text)
    assert "sleep" not in rule.keyword_candidates(text)


@pytest.mark.parametrize("text,expected", [
    ("重启", "reboot"),
    ("重启系统", "reboot"),
    ("帮我重启", "reboot"),
    ("重启一下设备", "reboot"),
    ("重启机器", "reboot"),
    ("把机器重启了", "reboot"),
    ("重新启动设备", "reboot"),
    ("关机", "shutdown"),
    ("帮我关机", "shutdown"),
    ("睡眠", "sleep"),
    ("进入睡眠模式", "sleep"),
    ("待机", "sleep"),
])
def test_admin_verb_keyword_still_recalls_device_command(routing, text, expected):
    """合法设备级指令仍召回 reboot/shutdown/sleep。"""
    registry, rule = routing
    assert rule.match(text).tool_name == expected


@pytest.mark.parametrize("text", [
    "关闭防火墙",
    "关闭自动更新",
    "停止录音",
    "运行脚本",
    "启动进程",
])
def test_app_control_regex_rejects_non_app_object(routing, text):
    """launch/close 的正则不得把系统功能/服务/设置当成应用名直通执行。"""
    registry, rule = routing
    m = rule.match(text)
    assert (m is None) or (m.tool_name not in {"launch_app", "close_app"})


@pytest.mark.parametrize("text", [
    "重新启动网络服务",
    "查看程序日志",
    "开启定位服务",
])
def test_app_control_keyword_rejects_non_app_object(routing, text):
    """launch/close 的裸动词/名词关键词不误注入非应用语境。"""
    registry, rule = routing
    assert "launch_app" not in rule.keyword_candidates(text)
    assert "close_app" not in rule.keyword_candidates(text)


@pytest.mark.parametrize("text", [
    "打开录音机",
    "打开脚本编辑器",
    "打开进程管理器",
    "打开日志查看器",
])
def test_app_control_word_boundary_keeps_app_suffix(routing, text):
    """黑名单按词边界排除，不误伤以黑名单词开头的应用名。"""
    registry, rule = routing
    assert rule.match(text).tool_name == "launch_app"


@pytest.mark.parametrize("text,tool", [
    ("安装防火墙", "pkg_files"),
    ("卸载防火墙", "pkg_remove"),
    ("依赖关系", "pkg_depends"),
    ("转码视频", "text_convert"),
    ("编码格式", "text_convert"),
    ("转成PDF", "text_convert"),
    ("层级结构", "usb_tree"),
    ("网络拓扑", "usb_tree"),
    ("父子进程", "usb_tree"),
    ("相机权限", "camera_open"),
    ("相机权限", "camera_close"),
    ("静音模式", "set_volume"),
    ("连回服务器", "wifi_connect"),
    ("挂载点", "disk_mount"),
    ("温度过高", "get_system_info"),
])
def test_bare_word_keyword_rejects_foreign_context(routing, text, tool):
    """裸词关键词（安装/卸载/依赖/转码/层级/相机/挂载/温度…）不得在陌生语境注入候选。"""
    registry, rule = routing
    assert tool not in rule.keyword_candidates(text)


@pytest.mark.parametrize("text,tool", [
    ("卸载这个软件", "pkg_remove"),
    ("转码这个文件", "text_convert"),
    ("USB层级", "usb_tree"),
    ("连回wifi", "wifi_connect"),
    ("挂载U盘", "disk_mount"),
    ("温度多少", "get_system_info"),
])
def test_bare_word_keyword_recalls_with_domain_anchor(routing, text, tool):
    """裸词与域锚点同现时仍召回所属工具（护栏不是一刀切删词）。"""
    registry, rule = routing
    assert tool in rule.keyword_candidates(text)


@pytest.mark.parametrize("text,tool,slots", [
    ("挂载/dev/sdb1到/mnt/c", "disk_mount", {"dev": "/dev/sdb1", "path": "/mnt/c"}),
    ("查看/etc/passwd这个文件的类型", "file_info", {"path": "/etc/passwd"}),
    ("看看/bin/ls这个文件属于哪个软件包", "pkg_owns_file", {"file": "/bin/ls"}),
    ("卸载/mnt/c", "disk_umount", {"target": "/mnt/c"}),
])
def test_path_literal_stops_at_cjk_connector(routing, text, tool, slots):
    """mask_literals 不得把路径后的中文连接词（到/这个/属于）吞进字面量。"""
    registry, rule = routing
    m = rule.match(text)
    assert m is not None and m.tool_name == tool
    for key, value in slots.items():
        assert m.slots.get(key) == value


@pytest.mark.parametrize("text,tool,expected_slots", [
    ("删除账号 test", "user_del", {"name": "test"}),
    ("delete user test", "user_del", {"name": "test"}),
    ("show package files for curl", "pkg_files", {"pkg": "curl"}),
    ("show info for openssh-client", "pkg_info", {"pkg": "openssh-client"}),
    ("show file info /etc/passwd", "file_info", {"path": "/etc/passwd"}),
    ("查看openssh-client这个包的信息", "pkg_info", {"pkg": "openssh-client"}),
])
def test_closed_grammar_beats_complexity_escalation(routing, text, tool, expected_slots):
    """封闭命令文法在 multi_intent 升级判断之前优先走规则直通。

    「删除账号 test」里的 test 会被动作词检测误判成第二个动词、英文
    read-only 句式（show/for）也曾被复杂度门拉进云端。这些整句命中的
    封闭文法应在本层确定性路由，而不是升云拆解。
    """
    registry, rule = routing
    plan = decide_local_intent(text, rule, registry)
    assert plan.action is IntentAction.RULE_CALL
    assert plan.tool_name == tool
    for key, value in expected_slots.items():
        assert plan.slots.get(key) == value


@pytest.mark.parametrize("text,tool", [
    ("拍照", "take_photo"),
    ("照相", "take_photo"),
    ("自拍", "take_photo"),
    ("拍个照", "take_photo"),
    ("我想拍照", "take_photo"),
    ("想自拍", "take_photo"),
    ("take a photo", "take_photo"),
    ("take a picture", "take_photo"),
    ("snap a photo", "take_photo"),
    ("I want to take a photo", "take_photo"),
    ("不拍照了", "camera_close"),
    ("不拍照", "camera_close"),
    ("拍照关掉相机", "camera_close"),
    ("关闭相机", "camera_close"),
    ("相机关掉", "camera_close"),
])
def test_camera_bare_photo_opens_instead_of_closes(routing, text, tool):
    """裸「拍照/照相」/英文 take a photo 都是拍照动作，命中 take_photo；不能命中
    camera_close 的宽松「(?:不)?(?:拍照|照相)(?:关掉)?(相机)?」被误当成关闭相机。

    英文形式依赖 _pattern_verb_roots 认出 pattern 交替组里裸的 take/snap：漏掉时
    analysis.predicates=('take',) 与工具的动词集无交集，谓词门会把英文输入整个
    过滤掉（实测 'take a photo' 静默升级云端）。"""
    registry, rule = routing
    plan = decide_local_intent(text, rule, registry)
    assert plan.action is IntentAction.RULE_CALL
    assert plan.tool_name == tool
