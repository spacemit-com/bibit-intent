"""tests/test_intent_eval.py — 意图识别统一离线回归的 pytest 批量入口。

把 `tests/regression.py` 聚合出的全部离线用例参数化，`pytest` 一键回归。
判断口径、出口标签与指标见 docs/evaluation/intent_evaluation_methodology.md §4/§5。
"""
from __future__ import annotations

import pytest

from regression import (
    build_registry,
    collect_cases,
    run_case,
    RuleEngine,
    QuestionDetector,
)

_registry = build_registry()
_rule = RuleEngine(_registry)
_qdet = QuestionDetector()
_CASES = collect_cases()


@pytest.mark.parametrize("case", _CASES, ids=lambda c: f"{c.suite}/{c.id}:{c.input}")
def test_intent_regression_case(case) -> None:
    out = run_case(case, _rule, _registry, _qdet)
    expected = (
        case.expect_exit
        or case.expect_reason
        or (f"is_multi={case.expect_multi}" if case.expect_multi is not None else "")
        or f"is_question={case.expect_question}"
    )
    assert out.ok, (
        f"「{case.input}」期望 {expected}，实际 {out.detail}"
    )
