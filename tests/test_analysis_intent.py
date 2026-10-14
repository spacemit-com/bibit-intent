"""语言分析层与 decide_local_intent 的集成测试。"""

from __future__ import annotations

import pytest

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.routing.intent import IntentAction, decide_local_intent
from bibit_intent.routing.rule_engine import RuleEngine


@pytest.fixture(scope="module")
def rule_engine():
    registry = ToolRegistry()
    return RuleEngine(registry), registry


def test_synonym_candidate_recall_for_oov_volume(rule_engine):
    rule, registry = rule_engine
    plan = decide_local_intent("声量调到60", rule, registry)
    assert plan.action is IntentAction.TO_MODEL
    assert "set_volume" in plan.candidates


def test_mid_sentence_negation_escalates(rule_engine):
    rule, registry = rule_engine
    plan = decide_local_intent("帮我不要关浏览器", rule, registry)
    assert plan.action is IntentAction.ESCALATE
    assert plan.reason == "negated_action"
    plan = decide_local_intent("please don't close the browser", *rule_engine)
    assert plan.action is IntentAction.ESCALATE and plan.reason == "negated_action"


def test_leading_negation_keeps_context_dependent(rule_engine):
    rule, registry = rule_engine
    plan = decide_local_intent("不要关浏览器", rule, registry)
    assert plan.action is IntentAction.ESCALATE
    assert plan.reason == "context_dependent"
    plan = decide_local_intent("open it", *rule_engine)
    assert plan.action is IntentAction.ESCALATE and plan.reason == "context_dependent"

def test_english_open_question_escalates(rule_engine):
    rule, registry = rule_engine
    plan = decide_local_intent("What is quantum entanglement?", rule, registry)
    assert plan.action is IntentAction.ESCALATE and plan.reason == "open_question"
