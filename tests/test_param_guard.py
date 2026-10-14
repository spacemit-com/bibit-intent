"""
tests/test_param_guard.py — 参数「原文证据闸门」测试（全离线）

背景：端侧 1B 实测会把缺失数值猜成区间端点/中值（大→100/小→0），
      prompt 防猜指令是软的、不可靠。router 在执行前调用
      _keep_evidenced_params 做三级证据判定（匹配前全角→半角归一化）：
        - kept（字面证据/非数值）→ 直接执行；
        - uncertain（模型语义推测、原文无字面证据，如"百分之五十"→50）
          → 挂起确认，不静默丢弃也不盲执行（见 router._suspend_confirm）。

运行： python3 tests/test_param_guard.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bibit_intent.routing.router import _keep_evidenced_params
from bibit_intent.core.registry import ToolRegistry

_reg = ToolRegistry()

def test_english_literal_parameter_evidence():
    tool = _reg.get("set_volume")
    ev = _keep_evidenced_params("set volume to 60", {"level": 60}, tool)
    assert ev.kept == {"level": 60}
    assert ev.uncertain == {}

def test_english_guessed_parameter_requires_confirmation():
    tool = _reg.get("set_brightness")
    ev = _keep_evidenced_params("make the screen brighter", {"level": 80}, tool)
    assert ev.kept == {}
    assert ev.uncertain == {"level": 80}

# (用户输入, 模型输出参数, 期望kept, 期望uncertain, 工具名)
CASES = [
    # 数值在原文有依据 → 保留
    ("音量调到60",      {"level": 60},  {"level": 60}, {}, "set_volume"),
    ("把亮度调到30",    {"level": 30},  {"level": 30}, {}, "set_brightness"),
    ("音量调到100",     {"level": 100}, {"level": 100}, {}, "set_volume"),
    # 全角归一化：全角数字/百分号 → 归一化后命中
    ("音量调到６０",     {"level": 60},  {"level": 60}, {}, "set_volume"),
    ("音量调到１００％", {"level": 100}, {"level": 100}, {}, "set_volume"),
    # 中文数字（规则证据，agent/cn_number.py）→ kept 直接执行
    ("音量调到五十",     {"level": 50},  {"level": 50}, {}, "set_volume"),
    ("音量百分之五十",   {"level": 50},  {"level": 50}, {}, "set_volume"),
    ("音量五成",         {"level": 50},  {"level": 50}, {}, "set_volume"),
    ("屏幕亮度开一半",   {"level": 50},  {"level": 50}, {}, "set_brightness"),
    # 数值在原文无字面/中文数字证据（模型猜的端点/中值）→ uncertain 待确认
    ("把声音开大一点",   {"level": 100}, {}, {"level": 100}, "set_volume"),
    ("声音小一点",       {"level": 0},   {}, {"level": 0}, "set_volume"),
    ("音量拉满",         {"level": 100}, {}, {"level": 100}, "set_volume"),
    ("帮我设置音量",     {"level": 50},  {}, {"level": 50}, "set_volume"),
    ("屏幕太暗了看不清", {"level": 100}, {}, {"level": 100}, "set_brightness"),
    # 混合：有依据的保留、推测的进 uncertain、未知键丢弃
    ("音量调到60",       {"level": 60, "bogus": "x"}, {"level": 60}, {}, "set_volume"),
    ("把声音开大一点",   {"level": 100, "bogus": "x"}, {}, {"level": 100}, "set_volume"),
    # 非数值参数不受影响（bool/str 由执行层类型/枚举校验）
    ("静音",            {"on": "true"}, {"on": "true"}, {}, "mute"),
]


def run():
    passed = failed = 0
    print("== 参数原文证据闸门 ==")
    for inp, params, exp_kept, exp_uncertain, tool_name in CASES:
        tool = _reg.get(tool_name)
        ev = _keep_evidenced_params(inp, dict(params), tool)
        ok = ev.kept == exp_kept and ev.uncertain == exp_uncertain
        passed += ok; failed += (not ok)
        print(f"  {'OK ' if ok else 'XX '}{inp!r:16} params={params} "
              f"-> kept={ev.kept} uncertain={ev.uncertain}"
              + ("" if ok else f"   期望 kept={exp_kept} uncertain={exp_uncertain}"))
    print(f"  小计：{passed}/{passed+failed}")
    return failed == 0


if __name__ == "__main__":
    ok = run()
    sys.exit(0 if ok else 1)
