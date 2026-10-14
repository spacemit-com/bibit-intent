"""一词多义守卫回归测试。"""

from __future__ import annotations

import pytest

from bibit_intent.resources import resource_path
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.routing.intent import IntentAction, decide_local_intent
from bibit_intent.routing.rule_engine import RuleEngine


@pytest.fixture(scope="module")
def context():
    registry = ToolRegistry(resource_path("tools.yaml"))
    load_skills(resource_path("skills"), registry, {})
    rule = RuleEngine(registry)
    return registry, rule


def _plan(text, context):
    registry, rule = context
    return decide_local_intent(text, rule, registry, embed=None, pkg_resolver=None)


def test_ambiguous_restart_escalates(context):
    for text in ("重启浏览器", "重新启动浏览器", "重启文件管理器"):
        plan = _plan(text, context)
        assert plan.action is IntentAction.ESCALATE
        assert plan.reason == "ambiguous_restart"


def test_english_polysemy_escalates_ambiguous_objects(context):
    for text in ("restart the service", "stop the process", "open the file"):
        plan = _plan(text, context)
        assert plan.action is IntentAction.ESCALATE
        assert plan.reason in {"ambiguous_control", "ambiguous_restart", "unsupported_control"}


def test_device_restart_stays_local(context):
    for text in ("重启", "重启设备", "重启系统", "重新启动设备"):
        plan = _plan(text, context)
        assert plan.action is IntentAction.RULE_CALL
        assert plan.tool_name == "reboot"


def test_named_service_restart_stays_local(context):
    plan = _plan("重启ssh服务", context)
    assert plan.action is IntentAction.RULE_CALL
    assert plan.tool_name == "service_restart"


def test_bare_service_or_process_action_escalates(context):
    for text in ("重启服务", "停止服务", "启动服务", "结束进程", "关闭进程"):
        plan = _plan(text, context)
        assert plan.action is IntentAction.ESCALATE
        assert plan.reason == "ambiguous_control"


def test_ambiguous_open_object_escalates(context):
    # "打开wifi" 已由 wifi_on 规则直通（见 test_wifi_radio_switch_stays_local）；
    # 裸"无线/网络"仍是一词多义（可能是设置项/网络服务），继续交模型判断。
    for text in ("打开进程", "打开文件", "打开服务", "打开网络", "打开无线"):
        plan = _plan(text, context)
        assert plan.action is IntentAction.ESCALATE
        assert plan.reason == "ambiguous_control"


def test_wifi_radio_switch_stays_local(context):
    """开/关 WiFi 网卡：规则直通到 wifi_on / wifi_off（不是断开连接的 wifi_disconnect）。"""
    for text, tool in (("打开wifi", "wifi_on"), ("打开WiFi", "wifi_on"),
                       ("打开无线网", "wifi_on"), ("开启无线网络", "wifi_on"),
                       ("关闭WiFi", "wifi_off"), ("关掉无线网", "wifi_off")):
        plan = _plan(text, context)
        assert plan.action is IntentAction.RULE_CALL, text
        assert plan.tool_name == tool, text
    # "断开"语义仍归 wifi_disconnect（radio 不动）
    for text in ("断开WiFi", "把WiFi切断"):
        plan = _plan(text, context)
        assert plan.action is IntentAction.RULE_CALL, text
        assert plan.tool_name == "wifi_disconnect", text


def test_valid_launch_app_stays_local(context):
    for text in ("打开文件管理器", "打开WiFi设置", "打开浏览器"):
        plan = _plan(text, context)
        assert plan.action is IntentAction.RULE_CALL
        assert plan.tool_name == "launch_app"


def test_external_screen_routes_to_display_mode(context):
    plan = _plan("打开外接屏", context)
    assert plan.action is IntentAction.RULE_CALL
    assert plan.tool_name == "set_display_mode"
    assert plan.slots.get("mode") == "extend"


def test_connect_candidates_are_object_anchored(context):
    registry, rule = context
    candidates = rule.keyword_candidates("连上wifi")
    assert "bt_connect" not in candidates
    assert "wifi_connect" in candidates


def test_generic_memory_query_does_not_recall_sysinfo(context):
    registry, rule = context
    assert "get_system_info" not in rule.keyword_candidates("查看内存")


def test_english_non_app_objects_do_not_inject_launch_app(context):
    """英文 launch 动词 + 非应用宾语不得把 launch_app 注入候选（关键词兜底路径）。

    "run the benchmark/script/backup/update/diagnostics" 此前会被 open/run 关键词
    弱命中，把 launch_app 作为唯一候选塞给 1B，模型可能幻觉 CALL launch_app(benchmark)。
    """
    registry, rule = context
    for text in (
        "run the benchmark", "run the script", "run the backup",
        "run the update", "run the diagnostics", "open the music",
        "start the process", "launch the service",
    ):
        assert "launch_app" not in rule.keyword_candidates(text), text
        plan = _plan(text, context)
        assert plan.action is not IntentAction.RULE_CALL, text


def test_english_real_apps_still_resolve(context):
    registry, rule = context
    for text in ("open firefox", "run firefox", "launch the terminal", "close browser"):
        plan = _plan(text, context)
        assert plan.action is IntentAction.RULE_CALL, text
        assert plan.tool_name in {"launch_app", "close_app"}, text


def test_english_physical_objects_do_not_trigger_app_control(context):
    """英文 open/close 动词 + 物理对象不得误触发 launch_app/close_app。

    应用名是开放集合，无法封闭白名单；这里用负向前瞻排除高频物理对象
    （door/lid/box/book/page/tab/curtain/drawer/cabinet/blinds/deal），
    避免 "open the door"/"close the lid" 被当成打开/关闭应用。
    """
    for text in (
        "open the door", "open the lid", "open the box", "open the book",
        "open the page", "open the tab", "open the curtain",
        "close the lid", "close the door", "close the deal",
        "close the tab", "close the page", "close the box", "close the book",
    ):
        plan = _plan(text, context)
        assert plan.action is not IntentAction.RULE_CALL, text


def test_english_bare_relative_words_do_not_misexecute(context):
    """英文裸相对词（louder/quieter/turn up/turn down/dim 等）不得高置信误执行。

    这些词通过 slot_map 的 `key in text` 子串匹配曾被提升到 0.85 直接 RULE_CALL，
    导致 "turn up the heat"/"turn down the music"/"turn up the mic"/"speak louder"
    被误执行为主音量/亮度调节。移除英文裸词 slot_map 后应升级云端澄清。
    """
    for text in (
        "turn up the heat", "turn down the music", "turn up the mic",
        "turn down the mic", "speak louder", "speak quieter",
        "the lights are too dim", "brighten the image", "the room is too dim",
        "dim the lights", "this photo is too dim",
    ):
        plan = _plan(text, context)
        assert plan.action is not IntentAction.RULE_CALL, text


def test_chinese_brightness_slot_map_requires_screen_object(context):
    """亮度工具的 slot_map 裸词（太暗/太亮/最亮/最暗）必须锚定屏幕/亮度宾语。

    「房间太暗」「灯泡最亮」「灯太亮」此前会被「太暗/最亮」裸子串命中并提升到
    0.85 直接 RULE_CALL，误把屏幕亮度改了。加了 slot_map_anchor 后，只有句子
    出现屏幕/亮度/背光/显示器/画面等宾语时才允许提升置信度。
    """
    for text in (
        "房间太暗", "照片太暗", "灯光太暗", "灯太亮",
        "灯泡最亮", "灯光最亮", "照片最亮", "灯泡最暗",
        "太暗了", "太亮了", "最亮", "最暗",
    ):
        plan = _plan(text, context)
        assert plan.action is not IntentAction.RULE_CALL, text


def test_chinese_brightness_screen_object_still_resolves(context):
    """含屏幕/亮度宾语的亮度 slot_map 仍应直通。"""
    for text, tool in (
        ("屏幕太暗", "adjust_brightness"),
        ("把屏幕调暗", "adjust_brightness"),
        ("画面太暗", "adjust_brightness"),
        ("屏幕太亮", "adjust_brightness"),
        ("屏幕最亮", "set_brightness"),
        ("把屏幕调到最亮", "set_brightness"),
        ("亮度最亮", "set_brightness"),
        ("把屏幕调到最暗", "set_brightness"),
    ):
        plan = _plan(text, context)
        assert plan.action is IntentAction.RULE_CALL, text
        assert plan.tool_name == tool, text


def test_audio_output_switch_ascii_device_keys_use_word_boundaries(context):
    """音频输出设备的 ASCII slot_map 词须按词边界匹配，避免 'dp' 误命中 'headphone'。

    'switch to headphone' 曾被 slot_map 的裸 'dp' 子串命中（'heaDPhone'），
    把输出设备错误切到 DP 而不是耳机。词边界匹配后不得再解析出 device=dp。
    """
    for text in ("switch to headphone", "switch to headphones"):
        plan = _plan(text, context)
        assert plan.action is IntentAction.RULE_CALL, text
        assert plan.tool_name == "audio_output_switch", text
        assert plan.slots.get("device") != "dp", text


def test_audio_output_switch_requires_switch_action(context):
    """输出设备 slot_map 须锚定切换动作，诊断/疑问句不得误切换设备。

    'is the hdmi output working' / 'the hdmi output is broken' 此前被 'output' 关键词
    + 'hdmi' slot_map 提升到 0.85 直接切设备；加了 slot_map_anchor 后应升级云端。
    """
    for text in (
        "the hdmi output is broken", "is the hdmi output working",
        "hdmi output", "check the headphone",
    ):
        plan = _plan(text, context)
        assert plan.action is not IntentAction.RULE_CALL, text


def test_service_status_does_not_treat_article_as_name(context):
    """泛指的 "the service" 不得把冠词 the 误当作服务名。

    "is the service running" / "check the service status" 此前被英文 pattern 的
    可选冠词 + 开放 name 捕获组解析成 name="the"，查询名为 the 的不存在服务。
    """
    for text in ("is the service running", "check the service status", "show the service status"):
        plan = _plan(text, context)
        assert not (plan.action is IntentAction.RULE_CALL and plan.slots.get("name") == "the"), text


def test_service_status_named_service_still_resolves(context):
    for text in ("is the ssh service running", "is ssh service running",
                 "check ssh service status", "show ssh service status"):
        plan = _plan(text, context)
        assert plan.action is IntentAction.RULE_CALL, text
        assert plan.tool_name == "service_status", text
        assert plan.slots.get("name") == "ssh", text
