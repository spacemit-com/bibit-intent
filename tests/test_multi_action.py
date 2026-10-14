"""tests/test_multi_action.py — 多动作检测单元测试（全离线）。

覆盖 detect_multi_action() 的判定逻辑，以及 decide_local_intent() 在
"多动作复合指令"时确定性升级云端（reason=multi_intent）的集成行为。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.routing.intent import IntentAction, decide_local_intent
from bibit_intent.routing.multi_action import (
    detect_multi_action,
    find_connectors,
    find_verbs,
)
from bibit_intent.routing.rule_engine import RuleEngine

_reg = ToolRegistry()
_rule = RuleEngine(_reg)


def test_find_verbs_longest_match():
    # Trie 最长匹配：复合词优先于单字"调"
    assert find_verbs("调节音量到6成") == ["调节"]
    assert find_verbs("调低亮度") == ["调低"]
    # "打开"优先于"开"，"开关"不会被拆成"开"+"关"
    assert find_verbs("打开开关") == ["打开"]


def test_find_connectors():
    assert find_connectors("打开浏览器并播放三国演义") == ["并"]
    assert find_connectors("open browser and play") == ["and"]


def test_multi_action_connector():
    r = detect_multi_action("打开浏览器并播放三国演义")
    assert r.is_multi is True
    assert r.reason == "connector"
    assert r.verbs == ["打开", "播放"]


def test_multi_action_connector_with_colloquial_query_verb():
    # 连接词是主信号：即使后一个子句用的是未被完整动词表收录的口语动词
    # （“查下”的“查”），也应判为复合，而不是被动词表拦下。
    r = detect_multi_action("帮我调音量到80%再查下K3的AI算力")
    assert r.is_multi is True
    assert r.reason == "connector"

    assert detect_multi_action("打开浏览器然后看下天气").is_multi is True
    assert detect_multi_action("调低亮度再看一下内存").is_multi is True
    assert detect_multi_action("先连上WiFi再查一下IP").is_multi is True


def test_multi_action_memory_tell_compound():
    # 「写进记忆 + 告诉我用了哪个工具」：写进/告诉我 都是动作，然后连接 → 复合。
    assert detect_multi_action(
        "请把我的名字 dec 写进长期记忆，然后告诉我你用了哪个记忆工具。"
    ).is_multi is True
    assert detect_multi_action(
        "把我的名字 dec 写进长期记忆，然后告诉我你用了哪个记忆工具。"
    ).is_multi is True


def test_multi_action_action_plus_question_clause():
    # 动作 + 疑问子句也应视为复合升级云端。
    assert detect_multi_action("先调音量到80%再告诉我怎么省电").is_multi is True


def test_single_predicate_multi_object_not_multi_action():
    # 单谓词多宾语仍交给 detect_multi_object_query，不应被连接词路径误判为多动作。
    assert detect_multi_action("查内存和CPU").is_multi is False
    assert detect_multi_action("查看CPU和内存").is_multi is False


def test_multi_action_boundary_no_connector():
    r = detect_multi_action("调低亮度 音量调大")
    assert r.is_multi is True
    assert r.reason == "boundary"

    r = detect_multi_action("音量调到30，亮度调到50")
    assert r.is_multi is True


def test_multi_action_english():
    assert detect_multi_action("open browser and play three kingdoms").is_multi
    assert detect_multi_action("launch Chrome, then mute the sound").is_multi
    assert detect_multi_action("scan for Wi-Fi and connect to HomeNet").is_multi
    assert not detect_multi_action("check cpu details").is_multi


def test_single_action_not_multi():
    for text in [
        "打开浏览器",
        "打开开关",
        "调节音量到6成",
        "查看 CPU 详细信息",
        "显示内存大小",
        "把音量调到60",
        "调低亮度",
        "看看这个skill",
        "看电影",
    ]:
        assert not detect_multi_action(text).is_multi, text


def test_decide_local_intent_escalates_multi_action():
    plan = decide_local_intent("打开浏览器并播放三国演义", _rule, _reg)
    assert plan.action is IntentAction.ESCALATE
    assert plan.reason == "multi_intent"


def test_rule_engine_does_not_partially_match_multi_action():
    text = "打开浏览器并删除用户alice"
    assert detect_multi_action(text).is_multi is True
    assert _rule.match(text) is None
    assert _rule.match_tool(text, "launch_app") is None
    assert _rule.keyword_candidates(text) == []


def test_other_multi_action_forms_are_blocked_before_rules():
    for text in (
        "安装vim然后重启nginx",
        "连接WiFi并启动服务",
        "查看内存并清理缓存",
        "关闭蓝牙、删除用户alice",
        "打开浏览器再删除用户alice",
    ):
        assert detect_multi_action(text).is_multi, text
        assert _rule.match(text) is None, text


def test_audio_output_switch_plus_volume_is_multi_action():
    text = "更改音频输出方式，改为：模拟（输出snd-8326），并调节输出音量为22%"
    assert "更改" in find_verbs(text)
    assert detect_multi_action(text).is_multi is True
    plan = decide_local_intent(text, _rule, _reg)
    assert plan.action is IntentAction.ESCALATE
    assert plan.reason == "multi_intent"


def test_single_audio_output_switch_to_analog_is_rule_call():
    plan = decide_local_intent("音频输出改为模拟", _rule, _reg)
    assert plan.action is IntentAction.RULE_CALL
    assert plan.tool_name == "audio_output_switch"
    assert plan.slots == {"device": "analog"}

def test_audio_output_switch_short_verbs_fill_device():
    # 短式动词（换成/切到/转成/改到/设成）不应被 predicate 过滤误跳过
    for text, device in [
        ("换成耳机", "headphone"),
        ("换成喇叭", "speaker"),
        ("切到喇叭", "speaker"),
        ("切到耳机", "headphone"),
        ("换到耳机", "headphone"),
        ("转成耳机", "headphone"),
        ("改到喇叭", "speaker"),
        ("设成喇叭", "speaker"),
    ]:
        plan = decide_local_intent(text, _rule, _reg)
        assert plan.action is IntentAction.RULE_CALL, text
        assert plan.tool_name == "audio_output_switch", text
        assert plan.slots == {"device": device}, text


def test_audio_output_switch_english_devices_fill_slot():
    # 英文设备词不再只返回空槽位（空槽位会被执行成 auto 误切）
    for text, device in [
        ("switch to headphone", "headphone"),
        ("switch to headphones", "headphone"),
        ("switch to speaker", "speaker"),
        ("switch to speakers", "speaker"),
        ("change to headphone", "headphone"),
        ("change to speaker", "speaker"),
        ("I want to use the speakers", "speaker"),
    ]:
        plan = decide_local_intent(text, _rule, _reg)
        assert plan.action is IntentAction.RULE_CALL, text
        assert plan.tool_name == "audio_output_switch", text
        assert plan.slots == {"device": device}, text


def test_audio_output_switch_howto_questions_escalate():
    # 短式动词的「怎么/如何」问句应升级开放问答，不能被规则直通误执行
    for text in ("怎么切到喇叭", "怎么切到耳机", "怎么换成耳机", "怎么转成喇叭", "如何切到喇叭"):
        plan = decide_local_intent(text, _rule, _reg)
        assert plan.action is IntentAction.ESCALATE, text
        assert plan.reason == "open_question", text


def test_audio_output_switch_short_verbs_hdmi_and_analog():
    # 短式动词也要覆盖 HDMI/DP/显示器/snd 设备，不能只支持喇叭/耳机
    for text, device in [
        ("切到HDMI", "hdmi"),
        ("转成HDMI", "hdmi"),
        ("切到显示器", "hdmi"),
        ("转成显示器", "hdmi"),
        ("切到DP", "dp"),
        ("转成DP", "dp"),
        ("切到snd-8326", "analog"),
        ("转成snd-8326", "analog"),
        ("切到snd-es8326", "analog"),
        ("切到模拟输出", "analog"),
        ("转成模拟输出", "analog"),
        ("切到模拟音频", "analog"),
    ]:
        plan = decide_local_intent(text, _rule, _reg)
        assert plan.action is IntentAction.RULE_CALL, text
        assert plan.tool_name == "audio_output_switch", text
        assert plan.slots == {"device": device}, text


def test_audio_output_switch_bluetooth_wireless_not_executed():
    # 裸「模拟」有歧义（可能是模拟/仿真），不应被当成 analog 直通
    plan = decide_local_intent("切到模拟", _rule, _reg)
    assert not (plan.action is IntentAction.RULE_CALL and plan.tool_name == "audio_output_switch")

    # 蓝牙/无线耳机、音箱不能被当成内置有线设备误执行
    for text in (
        "切到蓝牙耳机", "切到无线耳机", "切换到蓝牙耳机", "换成蓝牙耳机",
        "切到蓝牙音箱", "换成蓝牙喇叭", "换成蓝牙扬声器", "从喇叭切到蓝牙音箱",
    ):
        plan = decide_local_intent(text, _rule, _reg)
        assert not (
            plan.action is IntentAction.RULE_CALL
            and plan.tool_name == "audio_output_switch"
        ), text

    # 蓝牙/无线与设备词之间夹少量修饰词（降噪/头戴式/入耳式…）仍要拦截，
    # 不能被 slot_map 的裸设备关键词误填成内置有线设备
    for text in (
        "换成蓝牙降噪耳机", "换成蓝牙降噪喇叭", "切到无线降噪音箱",
        "切到蓝牙头戴式耳机", "换成无线入耳式耳机",
    ):
        plan = decide_local_intent(text, _rule, _reg)
        assert not (
            plan.action is IntentAction.RULE_CALL
            and plan.tool_name == "audio_output_switch"
        ), text


def test_audio_output_switch_bluetooth_source_to_wired_target_is_local():
    # 「蓝牙耳机」只是源设备时，「切到喇叭」的目标仍是有线设备，应正常直通，
    # 不能被 slot_map 把源设备的蓝牙前缀误套到目标词上
    for text in ("从蓝牙耳机切到喇叭", "蓝牙耳机换成喇叭", "把蓝牙耳机切到音箱"):
        plan = decide_local_intent(text, _rule, _reg)
        assert plan.action is IntentAction.RULE_CALL, text
        assert plan.tool_name == "audio_output_switch", text
        assert plan.slots.get("device") == "speaker", text


def test_audio_output_switch_non_bluetooth_audio_switch_is_local():
    # 「把声音输出切换到 X」只有蓝牙才升级，耳机/HDMI/音箱应本地直通
    for text, device in [
        ("把声音输出切换到耳机", "headphone"),
        ("把输出切换到HDMI", "hdmi"),
        ("把声音输出切换到音箱", "speaker"),
    ]:
        plan = decide_local_intent(text, _rule, _reg)
        assert plan.action is IntentAction.RULE_CALL, text
        assert plan.tool_name == "audio_output_switch", text
        assert plan.slots == {"device": device}, text
    plan = decide_local_intent("把声音输出切换到蓝牙耳机", _rule, _reg)
    assert plan.action is IntentAction.ESCALATE
    assert plan.reason == "unsupported_control"


def test_decide_local_intent_keeps_single_action_rule_call():
    # 单动作仍走规则直通，不升级
    plan = decide_local_intent("打开浏览器", _rule, _reg)
    assert plan.action is IntentAction.RULE_CALL
