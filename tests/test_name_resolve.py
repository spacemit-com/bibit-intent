"""
tests/test_name_resolve.py — short/full 工具名解析回归（全离线）
背景：第二段模型输出 short 名（vol/sysinfo），执行层用 full 名索引。
     曾因未转换导致 '现在CPU占用高吗' 模型答对却执行失败。
运行： python3 tests/test_name_resolve.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.executor import Executor

_reg = ToolRegistry()

def test_english_tool_alias_resolution():
    assert _reg.fuzzy_resolve("volume") == "set_volume"
    assert _reg.fuzzy_resolve("brightness") == "set_brightness"
    assert _reg.fuzzy_resolve("system info") == "get_system_info"
    assert _reg.fuzzy_resolve("reboot") == "reboot"

# (输入名, 期望规范化 full name)
RESOLVE_CASES = [
    ("sysinfo",         "get_system_info"),
    ("get_system_info", "get_system_info"),
    ("vol",             "set_volume"),
    ("set_volume",      "set_volume"),
    ("vol_adj",         "adjust_volume"),
    ("adjust_volume",   "adjust_volume"),
    ("bright",          "set_brightness"),
    ("mute",            "mute"),
    ("reboot",          "reboot"),
    ("bogus",           None),
]


def run():
    passed = failed = 0
    print("== short/full 名解析 ==")
    for name, exp in RESOLVE_CASES:
        got = _reg.resolve_name(name)
        ok = got == exp
        passed += ok; failed += (not ok)
        print(f"  {'OK ' if ok else 'XX '}{name!r:18} -> {got}"
              + ("" if ok else f"   期望 {exp}"))

    # 模糊解析：模型输出的近似名/英文别名应落到正确工具，无关词应为 None
    print("\n== 模糊解析（fuzzy_resolve）==")
    FUZZY_CASES = [
        ("volume", "set_volume"), ("sound", "set_volume"),
        ("cpu", "get_system_info"), ("memory", "get_system_info"),
        ("temp", "get_system_info"), ("brightness", "set_brightness"),
        ("screen", "set_brightness"), ("sys_info", "get_system_info"),
        ("set-volume", "set_volume"),
        # 无关输入必须 None，避免误路由
        ("poem", None), ("quantum", None), ("foobar", None), ("", None),
    ]
    for name, exp in FUZZY_CASES:
        got = _reg.fuzzy_resolve(name)
        ok = got == exp
        passed += ok; failed += (not ok)
        print(f"  {'OK ' if ok else 'XX '}{name!r:14} -> {got}"
              + ("" if ok else f"   期望 {exp}"))

    # 执行层用 short 名不应报「未知工具」(用 short 名走到参数/handler 阶段)
    print("\n== 执行层接受 short 名（不报未知工具）==")
    ex = Executor(_reg)
    r = ex.execute("vol", {"level": 50})
    # 不校验真实副作用（本地无 alsa），只确认没有卡在「未知工具」
    ok = "未知工具" not in r.message
    passed += ok; failed += (not ok)
    print(f"  {'OK ' if ok else 'XX '}execute('vol', level=50) -> ok={r.ok} msg={r.message!r}")

    total = passed + failed
    print(f"\n结果：{passed}/{total} 通过")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
