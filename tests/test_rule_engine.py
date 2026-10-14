"""
tests/test_rule_engine.py — 第一段规则引擎测试用例（全离线，可直接运行）
运行： python3 tests/test_rule_engine.py
覆盖维度：精确词 / 槽位抽取 / 边界钳制 / 句式变体 / 置信度 / 升级(None) / 候选预筛
所有期望值均已实测核实。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.routing.rule_engine import RuleEngine
from bibit_intent.resources import resource_path
from bibit_intent.execution.skill_loader import load_skills

_registry = ToolRegistry(resource_path("tools.yaml"))
load_skills(resource_path("skills"), _registry, {})
_engine = RuleEngine(_registry)


# ── 用例表：(输入, 期望工具, 期望槽位, 期望置信度) ─────────────────────
# tool=None 表示应返回 None（未命中，升级到第二段）
MATCH_CASES = [
    # English parity coverage
    ("mute the sound",   "mute",        {"on": True},  1.0),
    ("unmute audio",     "mute",        {"on": False}, 1.0),
    ("restart the computer", "reboot",  {},            1.0),
    ("set volume to 60", "set_volume",  {"level": 60}, 1.0),
    ("set brightness to 40 percent", "set_brightness", {"level": 40}, 1.0),
    ("show system uptime", "sys_uptime", {}, 1.0),
    ("show disk usage", "disk_usage", {}, 1.0),
    ("list running processes", "proc_list", {}, 1.0),
    # A. 精确词表（O(1)，置信度 1.0）
    ("静音",           "mute",        {"on": True},   1.0),
    ("取消静音",       "mute",        {"on": False},  1.0),
    ("关闭静音",       "mute",        {"on": False},  1.0),
    ("解除静音",       "mute",        {"on": False},  1.0),
    ("重启",           "reboot",      {},             1.0),
    ("重新启动",       "reboot",      {},             1.0),

    # B. 槽位抽取（数字正确抽取 —— 曾经的 bug，回归测试）
    ("音量调到60",     "set_volume",  {"level": 60},  1.0),
    ("亮度调到30",     "set_brightness", {"level": 30}, 1.0),
    ("音量设为75",     "set_volume",  {"level": 75},  1.0),
    ("声音调大到90",   "set_volume",  {"level": 90},  1.0),
    # 中文数字档位（cn_number 解析）：六成→60 / 百分之六十→60
    ("音量调到百分之六十", "set_volume", {"level": 60}, 1.0),
    ("声音有点小，帮我调到六成吧", "set_volume", {"level": 60}, 0.85),
    # 成数回归：阿拉伯 6成 / 调在前 六成 都应解析为 60（防抓成 6 或 0.6）
    ("调节音量到6成",   "set_volume",  {"level": 60},  1.0),
    ("音量调到6成",     "set_volume",  {"level": 60},  1.0),
    ("调节音量到六成",  "set_volume",  {"level": 60},  1.0),
    ("把亮度调到六成", "set_brightness", {"level": 60}, 0.85),

    # C. 边界与钳制
    ("音量调到0",      "set_volume",  {"level": 0},   1.0),
    ("音量调到100",    "set_volume",  {"level": 100}, 1.0),
    ("音量调到150",    "set_volume",  {"level": 100}, 1.0),   # 超上限 → 钳到 100
    # 音量只读查询：应命中 get_volume，而不是误落到 set_volume/adjust_volume
    ("现在音量是多少", "get_vol_status", {}, 1.0),
    ("当前音量是多少", "get_vol_status", {}, 1.0),
    ("音量现在是多少", "get_vol_status", {}, 1.0),
    ("音量是多少",     "get_vol_status", {}, 1.0),
    ("查看音量",       "get_vol_status", {}, 1.0),
    # 亮度只读查询：应命中 get_display_light，而不是误落到 set_brightness/adjust_brightness
    ("现在亮度是多少", "get_display_light", {}, 1.0),
    ("当前亮度是多少", "get_display_light", {}, 1.0),
    ("亮度现在是多少", "get_display_light", {}, 1.0),
    ("查看亮度",       "get_display_light", {}, 1.0),
    ("把声音开大一点", "adjust_volume", {"direction": "up", "amount": 10}, 1.0),
    ("调高音量",       "adjust_volume", {"direction": "up", "amount": 10}, 1.0),
    ("声音调小一点",   "adjust_volume", {"direction": "down", "amount": 10}, 1.0),
    ("把音量调低",     "adjust_volume", {"direction": "down", "amount": 10}, 1.0),
    ("turn the volume up", "adjust_volume", {"direction": "up", "amount": 10}, 1.0),
    ("turn it down",   "adjust_volume", {"direction": "down", "amount": 10}, 1.0),

    # D. 部分匹配（有前缀"把" → 非全匹配 → 置信度 0.85）
    ("把音量调到60",   "set_volume",  {"level": 60},  0.85),

    # E. 关键词命中但无槽位（低置信 0.5，路由器据此升级）
    ("音量有点大",     "set_volume",  {},             0.5),
    # slot_map 补值："为什么没声音"是控制意图 → 没声音→80（防模型猜 0）
    ("为什么没声音",   "set_volume",  {"level": 80},  0.85),

    # F. 应升级到第二段（返回 None）
    ("帮我写封邮件",   None,          None,           None),
    ("屏幕太暗了",     "adjust_brightness", {"direction": "up", "amount": 10}, 0.85),
    ("亮度调亮一点",   "adjust_brightness", {"direction": "up", "amount": 10}, 1.0),
    ("屏幕亮度调暗一点", "adjust_brightness", {"direction": "down", "amount": 10}, 1.0),
    ("讲个笑话",       None,          None,           None),

    # G. 窗口管理（wlrctl/labwc）
    ("窗口列表",       "window_list",    {},             1.0),
    ("应用列表",       "window_list",    {},             1.0),
    ("所有窗口",       "window_list",    {},             1.0),
    ("有哪些窗口",     "window_list",    {},             1.0),
    ("查看窗口列表",   "window_list",    {},             1.0),
    ("切换到终端",     "window_activate", {"target": "终端"}, 1.0),
    ("激活浏览器窗口", "window_activate", {"target": "浏览器窗口"}, 1.0),
    ("最大化终端",     "window_maximize", {"target": "终端"}, 1.0),
    ("把文件管理器最小化", "window_minimize", {"target": "文件管理器"}, 1.0),

    # H. 口语覆盖回归：口语说法必须命中规则层，不要落到 1B 去猜工具。
    ("把屏幕竖起来",   "rotate_screen", {"angle": 90}, 1.0),
    ("把屏幕横过来",   "rotate_screen", {"angle": 0},  1.0),
    # 静音："关掉声音/别出声" → mute
    ("把声音关掉",     "mute", {}, 1.0),
    ("把声音关掉别出声", "mute", {}, 1.0),
    # 输出设备切换：裸动词"切"、"从X切到Y" → audio_output_switch
    ("切到喇叭",       "audio_output_switch", {"device": "speaker"}, 1.0),
    ("声音从耳机切到喇叭", "audio_output_switch", {"device": "speaker"}, 0.85),
    ("音频输出改为snd-es8326", "audio_output_switch", {"device": "analog"}, 0.85),
    ("音频输出切换到snd-8326", "audio_output_switch", {"device": "analog"}, 0.85),
    # 分辨率抱怨式：\"太低/太高\"之前升级云端后被误选 sys_resolution（查），规则层定死为改
    ("分辨率太低了",   "set_resolution", {"mode": "up"}, 0.85),
    ("屏幕分辨率太低了字都糊了", "set_resolution", {"mode": "up"}, 0.85),

    # I. WiFi 连接口语回归："连接<SSID>的wifi，密码是<密码>" 要命中规则，
    # 且"是/为"不能被吃进密码。
    ("连接JDSK的wifi，密码是SpaceT20211102", "wifi_connect", {"ssid": "JDSK", "password": "SpaceT20211102"}, 1.0),
    ("连接JDSK的wifi，密码12345678", "wifi_connect", {"ssid": "JDSK", "password": "12345678"}, 1.0),
    ("连接JDSK密码是12345678", "wifi_connect", {"ssid": "JDSK", "password": "12345678"}, 1.0),
    ("连接wifi JDSK 密码是 12345678", "wifi_connect", {"ssid": "JDSK", "password": "12345678"}, 1.0),
    ("连接 JDSK 密码为 12345678", "wifi_connect", {"ssid": "JDSK", "password": "12345678"}, 1.0),
    ("连上JDSK的wifi密码12345678", "wifi_connect", {"ssid": "JDSK", "password": "12345678"}, 1.0),
    ("帮我连一下JDSK的wifi密码12345678", "wifi_connect", {"ssid": "JDSK", "password": "12345678"}, 1.0),
    ("连接无线网络JDSK密码12345678", "wifi_connect", {"ssid": "JDSK", "password": "12345678"}, 1.0),
    ("连接 JDSK，密码 12345678", "wifi_connect", {"ssid": "JDSK", "password": "12345678"}, 1.0),
    # 仅 SSID + 类别词后置（无密码）
    ("连接JDSK的wifi", "wifi_connect", {"ssid": "JDSK"}, 1.0),
    ("连上JDSK的wifi", "wifi_connect", {"ssid": "JDSK"}, 1.0),
]

# ── 候选预筛用例：(输入, 期望候选列表) ────────────────────────────────
CANDIDATE_CASES = [
    ("音量有点大", ["set_volume"]),
    ("现在音量是多少", ["get_vol_status", "set_volume"]),
    ("现在亮度是多少", ["get_display_light", "set_brightness", "adjust_brightness"]),
    ("调高音量",   ["set_volume", "adjust_volume"]),
    ("屏幕太暗",   ["set_brightness", "adjust_brightness"]),
    ("讲个笑话",   []),
]


# ── 开放问答前置判断：(输入, 是否应判为开放问答) ─────────────────────
# 关键是控制指令绝不能误伤（尤其"为什么没声音"是控制意图）。
OPEN_QUESTION_CASES = [
    ("解释一下量子纠缠",  True),
    ("什么是黑洞",        True),
    ("为什么天是蓝的",    True),
    ("介绍一下唐朝历史",  True),
    ("讲讲相对论",        True),
    # 控制指令 / 非开放问答 → False
    ("把声音开大一点",    False),
    ("屏幕太暗了看不清",  False),
    ("现在CPU占用高吗",   False),
    ("为什么没声音",      False),   # 陷阱：控制意图，不应误判
    ("音量调到60",        False),
    ("讲个笑话",          False),   # 闲聊非知识问答，不由此判断拦
]


def run():
    passed = failed = 0
    print("== 规则引擎匹配用例 ==")
    for text, exp_tool, exp_slots, exp_conf in MATCH_CASES:
        m = _engine.match(text)
        got_tool = m.tool_name if m else None
        got_slots = m.slots if m else None
        got_conf = m.confidence if m else None
        ok = got_tool == exp_tool
        if exp_slots is not None:
            ok = ok and got_slots == exp_slots
        if exp_conf is not None:
            ok = ok and abs((got_conf or 0) - exp_conf) < 1e-6
        tag = "OK " if ok else "XX "
        passed += ok; failed += (not ok)
        detail = f"{got_tool} {got_slots} conf={got_conf}" if m else "None"
        print(f"  {tag}{text!r:16} -> {detail}")
        if not ok:
            print(f"      期望: {exp_tool} {exp_slots} conf={exp_conf}")

    print("\n== 候选预筛用例 ==")
    for text, exp in CANDIDATE_CASES:
        got = _engine.keyword_candidates(text)
        ok = got == exp
        passed += ok; failed += (not ok)
        print(f"  {'OK ' if ok else 'XX '}{text!r:12} -> {got}"
              + ("" if ok else f"   期望 {exp}"))

    print("\n== 开放问答前置判断 ==")
    for text, exp in OPEN_QUESTION_CASES:
        got = _engine.is_open_question(text)
        ok = got == exp
        passed += ok; failed += (not ok)
        print(f"  {'OK ' if ok else 'XX '}{text!r:16} -> {got}"
              + ("" if ok else f"   期望 {exp}"))

    total = passed + failed
    print(f"\n结果：{passed}/{total} 通过")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
