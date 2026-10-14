"""
tests/test_question_detector.py — 问句判别模块单元测试
运行： python3 tests/test_question_detector.py

覆盖三层判别：
  L1 强信号正则、L2 组合特征（无 embedding，完全离线）；
  L3 语义锚点用假检索器验证门控与阈值（真检索器在设备上校准）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bibit_intent.routing.question_detector import QuestionDetector


# 无 embedding 的判别器：只测 L1/L2
_det = QuestionDetector()

# (输入, 期望是否开放问答, 期望命中层)
CASES = [
    # ── L1 强信号 ──────────────────────────────────────────────
    ("解释一下量子纠缠",  True, "strong"),
    ("什么是黑洞",        True, "strong"),
    ("为什么天是蓝的",    True, "strong"),
    ("介绍一下唐朝历史",  True, "strong"),
    ("讲讲相对论",        True, "strong"),
    ("如何理解相对论",    True, "strong"),
    ("What is quantum entanglement?", True, "strong"),
    ("How does gravity work?", True, "feature"),
    ("Why is the sky blue?", True, "strong"),
    ("Explain relativity", True, "strong"),
    ("声音是怎么传播的",  True, "strong"),   # "是怎么…的"科学问句
    ("音量是怎么调的",    True, "strong"),
    # ── L2 组合特征：疑问词 + 句尾标记 ─────────────────────────
    ("怎么打开终端呢",    True, "feature"),
    ("为什么没声音呢",    True, "feature"),   # 有句尾标记的 why 是问句
    ("音量怎么调啊",      True, "feature"),
    ("如何设置静态IP呢",  True, "feature"),
    ("系统怎么更新呢",    True, "feature"),
    ("现在几点了呢",      True, "feature"),   # 口语疑问词"几"
    ("咋回事呢",          True, "feature"),   # 口语疑问词"咋"
    ("这是啥情况呢",      True, "feature"),   # 口语疑问词"啥"
    ("你干嘛呢",          True, "feature"),   # 口语疑问词"干嘛"
    ("该选哪个方案呢",    True, "feature"),   # 口语疑问词"哪"
    ("How do I open the terminal?", True, "strong"),
    ("Where are my downloads?", True, "feature"),
    # ── 控制指令 / 状态查询 → 不判为开放问答 ────────────────────
    ("把声音开大一点",    False, "none"),
    ("屏幕太暗了看不清",  False, "none"),
    ("现在CPU占用高吗",   False, "none"),     # 无疑问词，状态查询本地可答
    ("为什么没声音",      False, "none"),     # 陷阱：无句尾标记，控制意图
    ("音量调到60",        False, "none"),
    ("讲个笑话",          False, "none"),     # 闲聊非知识问答
    ("今天天气怎么样",    False, "none"),
    ("今天星期几",        False, "none"),     # 无句尾标记，L3 无命中(无检索器)
    ("increase the volume", False, "none"),
    ("set brightness to 40", False, "none"),
    # ── L1/L2 不命中，需 L3 语义层（本测试无 embedding，保持 False）─
    ("亮度如何调节",      False, "none"),
    ("怎么调节音量",      False, "none"),
    ("如何打开终端",      False, "none"),
]


class _FakeRetriever:
    """固定返回 best 的假检索器，用于验证 L3 逻辑。"""

    def __init__(self, best):
        self._best = best

    def best_match(self, text):
        return self._best


def _check_semantic_layer():
    """L3：疑问词门控 + 阈值 + QUESTION 工具校验。"""
    # 命中：余弦 ≥ 阈值
    det = QuestionDetector(_FakeRetriever(("QUESTION", 0.91)))
    v = det.detect("亮度如何调节")
    assert v.is_question and v.layer == "semantic", v
    # 阈值不足 → 不判
    det = QuestionDetector(_FakeRetriever(("QUESTION", 0.50)))
    assert not det.is_open_question("亮度如何调节")
    # 检索器最佳命中不是 QUESTION（如工具锚点）→ 不判
    det = QuestionDetector(_FakeRetriever(("set_volume", 0.95)))
    assert not det.is_open_question("亮度如何调节")
    # 门控：无疑问词的输入不触发 /embedding（假检索器不应被调用）
    calls = []

    class _CountingRetriever(_FakeRetriever):
        def best_match(self, text):
            calls.append(text)
            return ("QUESTION", 0.99)

    det = QuestionDetector(_CountingRetriever(("QUESTION", 0.99)))
    assert not det.is_open_question("把声音开大一点")
    assert calls == []


def _check_near_miss():
    """L3b 近命中带：[阈值-0.12, 阈值)。"""
    # 0.85 ∈ [0.78, 0.90) → 近命中（但不判为开放问答）
    det = QuestionDetector(_FakeRetriever(("QUESTION", 0.85)))
    assert det.is_near_miss("亮度如何调节")
    assert not det.is_open_question("亮度如何调节")
    # 硬命中（≥0.90）→ 不是近命中
    det = QuestionDetector(_FakeRetriever(("QUESTION", 0.91)))
    assert not det.is_near_miss("亮度如何调节")
    # 低分（<0.78）→ 不是近命中
    det = QuestionDetector(_FakeRetriever(("QUESTION", 0.60)))
    assert not det.is_near_miss("亮度如何调节")
    # 门控：无疑问词输入不触发语义计算（假检索器不应被调用）
    calls = []

    class _CountingRetriever(_FakeRetriever):
        def best_match(self, text):
            calls.append(text)
            return ("QUESTION", 0.85)

    det = QuestionDetector(_CountingRetriever(("QUESTION", 0.85)))
    assert not det.is_near_miss("把声音开大一点")
    assert calls == []


def run():
    passed = failed = 0
    for text, exp, exp_layer in CASES:
        v = _det.detect(text)
        ok = v.is_question == exp and v.layer == exp_layer
        passed += ok
        failed += (not ok)
        print(f"  {'OK ' if ok else 'XX '}{text!r:14} -> "
              f"is_question={v.is_question} layer={v.layer}"
              + ("" if ok else f"   期望 {exp}/{exp_layer}"))
    _check_semantic_layer()
    print("  OK  L3 语义层（假检索器）")
    _check_near_miss()
    print("  OK  L3b 近命中带（假检索器）")
    total = passed + failed
    print(f"\n结果：{passed}/{total} 通过")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
