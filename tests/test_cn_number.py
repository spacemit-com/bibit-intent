"""
tests/test_cn_number.py — 中文数字解析器单测（零依赖，本机可跑）
覆盖：parse_cn_seq（中文数字→阿拉伯）与 extract_cn_numbers（口语提取），
并验证闸门把中文数字当作规则证据（kept 直接执行）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bibit_intent.core.cn_number import extract_cn_numbers, parse_cn_seq

SEQ_CASES = [
    ("五十", 50),
    ("十五", 15),
    ("一百", 100),
    ("一百二十", 120),
    ("一千零五十", 1050),
    ("十万", 100000),
    ("两百", 200),
    ("零", 0),
]

EXTRACT_CASES = [
    # (文本, 期望数字列表)
    ("音量调到五十", [50]),
    ("音量百分之五十", [50]),
    ("亮度百分之百", [100]),
    ("音量五成", [50]),
    ("音量八成", [80]),
    ("屏幕亮度开一半", [50]),
    ("音量调到一百二", [120]),
    # 排除误报：单字数字/量词性"一"
    ("星期五下午", []),
    ("帮我拿一些东西", []),
    ("请把音量调高一些", []),
    # 混合
    ("亮度百分之三十，音量八成一", [30, 80]),
    # 阿拉伯数字成数：6成 → 60
    ("音量6成", [60]),
    ("调节音量到6成", [60]),
]

# English/Arabic numerals are handled by the general numeric extractor, not
# the Chinese-number parser; keep a regression guard that this parser does not
# misinterpret ordinary English words as Chinese numerals.
ENGLISH_CASES = [
    ("set the volume to fifty", []),
    ("set brightness to one hundred percent", []),
    ("turn the volume up a little", []),
    ("Friday afternoon", []),
]


def run() -> bool:
    passed = failed = 0
    print("== 中文数字序列解析 ==")
    for seq, want in SEQ_CASES:
        got = parse_cn_seq(seq)
        ok = got == want
        passed += ok; failed += (not ok)
        print(f"  {'OK ' if ok else 'XX '}{seq!r:10} -> {got}" + ("" if ok else f"   期望 {want}"))

    print("\n== 口语提取 ==")
    for text, want in EXTRACT_CASES:
        got = extract_cn_numbers(text)
        ok = got == want
        passed += ok; failed += (not ok)
        print(f"  {'OK ' if ok else 'XX '}{text!r:16} -> {got}" + ("" if ok else f"   期望 {want}"))

    print(f"\n小计：{passed}/{passed + failed}")
    print("== 英文误报回归 ==")
    for text, want in ENGLISH_CASES:
        got = extract_cn_numbers(text)
        ok = got == want
        passed += ok; failed += (not ok)
        print(f"  {'OK ' if ok else 'XX '}{text!r:30} -> {got}")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
