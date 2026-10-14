"""
tests/test_hard_negatives.py — 硬负样本评测（全离线，无模型依赖）

覆盖 C8 域外/关键词陷阱与 C9 危险/越权/戏弄类反例，断言"绝不静默直通"。
判断标准与 docs/evaluation/negative_sample_evaluation.md 一致：
  - escalate      : 必须在进模型前被确定性前置规则升级云端
  - no_rule_call  : 不能直接 RULE_CALL（云端/拒绝/澄清均可）
  - no_candidates : 进模型时也不能注入任何工具候选（防 1B 幻觉 CALL）

语料来源：docs/intent/intent_negative_cases.md §2 C8/C9 与 §4 回归清单。
运行：
  .venv/bin/python -m pytest tests/test_hard_negatives.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.resources import resource_path
from bibit_intent.routing.intent import IntentAction, decide_local_intent
from bibit_intent.routing.rule_engine import RuleEngine

_CORPUS = Path(__file__).with_name("hard_negatives") / "corpus.jsonl"
_CORPUS_EN = Path(__file__).with_name("hard_negatives") / "corpus.en.jsonl"


@pytest.fixture(scope="module")
def registry() -> ToolRegistry:
    reg = ToolRegistry(resource_path("tools.yaml"))
    load_skills(resource_path("skills"), reg, {})
    return reg


@pytest.fixture(scope="module")
def rule(registry: ToolRegistry) -> RuleEngine:
    return RuleEngine(registry)


def _cases() -> list[dict]:
    cases: list[dict] = []
    for corpus in (_CORPUS, _CORPUS_EN):
        cases.extend(
            json.loads(line)
            for line in corpus.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    return cases


@pytest.mark.parametrize("case", _cases(), ids=lambda c: f"{c['id']}:{c['input']}")
def test_hard_negative_not_direct_rule_call(rule: RuleEngine, registry: ToolRegistry, case: dict) -> None:
    # cloud_probe 显式注入「云端可达」：本文件断言的契约是"在线时该升级云端"，
    # 不能让离线预检门（§5.2 失败→本地 net_diagnose 直通）随测试机网络状态漂移。
    # 离线兜底契约另见 tests/test_net_diagnosis_offline.py。
    plan = decide_local_intent(case["input"], rule, registry, embed=None, cloud_probe=lambda: True)
    expected = case.get("expect", "no_rule_call")

    if expected in {"escalate", "open_question", "writing_task", "negated_action", "ambiguous_control"}:
        # English corpus uses semantic labels; all must be kept off direct rule execution.
        if expected != "escalate":
            assert plan.action is not IntentAction.RULE_CALL, (
                f"{case['input']} must not route directly, got {plan.action.value}"
            )
            return
        assert plan.action is IntentAction.ESCALATE, (
            f"{case['input']} 期望 escalate，实际 {plan.action.value} "
            f"tool={plan.tool_name} reason={plan.reason}"
        )
    elif expected == "no_candidates":
        assert plan.action is not IntentAction.RULE_CALL, (
            f"{case['input']} 不得直通，实际 {plan.action.value} tool={plan.tool_name}"
        )
        assert not plan.candidates, (
            f"{case['input']} 不应注入候选，实际 {plan.candidates}"
        )
    else:  # no_rule_call
        assert plan.action is not IntentAction.RULE_CALL, (
            f"{case['input']} 不得直通，实际 {plan.action.value} tool={plan.tool_name}"
        )
