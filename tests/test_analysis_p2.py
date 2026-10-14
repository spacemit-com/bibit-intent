"""P2 接入点测试：RuleEngine 否定守卫、Session 新意图、可选后端降级。"""

from __future__ import annotations

import pytest

from bibit_intent.analysis import backends
from bibit_intent.analysis.deterministic import DeterministicAnalyzer
from bibit_intent.analysis.features import UtteranceAnalysis
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.resources import resource_path
from bibit_intent.core.session import SessionState
from bibit_intent.routing.rule_engine import RuleEngine


@pytest.fixture(scope="module")
def rule_engine():
    registry = ToolRegistry(resource_path("tools.yaml"))
    return RuleEngine(registry), registry


def test_rule_engine_negation_guard(rule_engine):
    rule, _ = rule_engine
    analysis = UtteranceAnalysis(is_negated_action=True)
    assert rule.match("重启", analysis) is None
    assert rule.match("重启").tool_name == "reboot"


def test_rule_engine_english_negation_guard(rule_engine):
    rule, _ = rule_engine
    analysis = UtteranceAnalysis(is_negated_action=True, language="en")
    assert rule.match("restart", analysis) is None
    assert rule.match("restart the computer").tool_name == "reboot"


def test_session_resolve_negated_action_as_new_intent(rule_engine):
    rule, registry = rule_engine
    session = SessionState()
    session.set_pending(
        tool_name="set_volume",
        param_name="level",
        param_label="音量",
        filled={},
        param_type="int",
    )
    analysis = DeterministicAnalyzer().analyze("帮我不要关浏览器")
    action, _ = session.resolve("帮我不要关浏览器", rule, analysis=analysis)
    assert action == "new"
    assert session.pending is None


def test_session_resolve_english_negated_action_as_new_intent(rule_engine):
    rule, _ = rule_engine
    session = SessionState()
    session.set_pending("set_volume", "level", "volume", {}, "", "int")
    analysis = DeterministicAnalyzer().analyze("do not close the browser")
    action, _ = session.resolve("do not close the browser", rule, analysis=analysis)
    assert action == "new"
    assert session.pending is None


def test_optional_backend_falls_back_when_backends_missing(monkeypatch):
    class MissingBackend:
        def __init__(self):
            raise ImportError("optional backend unavailable")

    monkeypatch.setattr(backends, "JiebaAnalyzer", MissingBackend)
    monkeypatch.setattr(backends, "SpacyAnalyzer", MissingBackend)

    analyzer = backends.build_analyzer()
    assert hasattr(analyzer, "analyze")
    assert isinstance(analyzer, DeterministicAnalyzer)
