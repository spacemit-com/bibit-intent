"""语言分析层与路由集成边界用例。"""

from __future__ import annotations

import pytest

from bibit_intent.analysis.deterministic import DeterministicAnalyzer
from bibit_intent.analysis.synonyms import SynonymResolver
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.core.session import SessionState
from bibit_intent.routing.intent import IntentAction, decide_local_intent
from bibit_intent.routing.rule_engine import RuleEngine


@pytest.fixture(scope="module")
def analyzer():
    return DeterministicAnalyzer()


@pytest.fixture(scope="module")
def rule_engine():
    registry = ToolRegistry()
    return RuleEngine(registry), registry


@pytest.mark.parametrize(
    "text",
    [
        "打开浏览器并播放三国演义",
        "先打开浏览器，然后播放三国演义",
        "搜索并安装软件",
        "下载、安装、配置软件",
        "open the browser and play a video",
        "search for updates and install the package",
    ],
)
def test_multi_action_true(analyzer, text):
    assert analyzer.analyze(text).is_multi_action


@pytest.mark.parametrize(
    "text",
    [
        "打开浏览器",
        "查看CPU信息",
        "今天天气怎么样",
        "开关",
        "open the browser",
        "show CPU information",
    ],
)
def test_multi_action_false(analyzer, text):
    assert analyzer.analyze(text).is_multi_action is False


@pytest.mark.parametrize(
    "text",
    [
        "安装hello和fio",
        "卸载 firefox、chrome",
        "安装 a、b、c",
        "install vim and htop",
        "remove firefox, chrome, edge",
    ],
)
def test_multi_target_true(analyzer, text):
    assert analyzer.analyze(text).is_multi_target


@pytest.mark.parametrize(
    "text",
    [
        "安装fio",
        "卸载 firefox chrome",
        "播放三国演义",
        "install vim",
    ],
)
def test_multi_target_false(analyzer, text):
    assert analyzer.analyze(text).is_multi_target is False


@pytest.mark.parametrize(
    "text",
    [
        "打开它",
        "把它关掉",
        "调大一点",
        "别重启",
        "open it",
        "open it",
    ],
)
def test_context_dependent_true(analyzer, text):
    assert analyzer.analyze(text).is_context_dependent


@pytest.mark.parametrize(
    "text",
    [
        "打开浏览器",
        "调大音量",
        "把声音调大一点",
        "open the browser",
        "increase the volume",
    ],
)
def test_context_dependent_false(analyzer, text):
    assert analyzer.analyze(text).is_context_dependent is False


@pytest.mark.parametrize(
    "text",
    [
        "帮我不要关浏览器",
        "请不要关闭浏览器",
        "不用重启",
        "please do not close the browser",
        "don't restart the computer",
    ],
)
def test_negated_action_true(analyzer, text):
    assert analyzer.analyze(text).is_negated_action


@pytest.mark.parametrize(
    "text",
    [
        "关浏览器",
        "没有声音",
        "今天天气怎么样",
        "what is the weather today",
    ],
)
def test_negated_action_false(analyzer, text):
    assert analyzer.analyze(text).is_negated_action is False


@pytest.mark.parametrize(
    ("text", "canonical"),
    [
        ("声量调到60", "音量"),
        ("打开网页浏览器", "浏览器"),
        ("中央处理器占用高", "CPU"),
        ("运行内存不够了", "内存"),
    ],
)
def test_synonym_hits(text, canonical):
    resolver = SynonymResolver()
    assert canonical in resolver.hits(text)
    assert canonical in resolver.expand_text(text)


def test_synonym_no_expansion_when_unknown():
    resolver = SynonymResolver()
    assert resolver.expand_text("播放三国演义") == "播放三国演义"


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("打开浏览器并播放三国演义", "multi_intent"),
        ("安装hello和fio", "multi_target"),
        ("不要关浏览器", "context_dependent"),
        ("帮我不要关浏览器", "negated_action"),
        ("open the browser and play a video", "multi_intent"),
        ("install vim and htop", "multi_target"),
        ("do not close the browser", "negated_action"),
    ],
)
def test_decide_local_intent_escalations(rule_engine, text, reason):
    rule, registry = rule_engine
    plan = decide_local_intent(text, rule, registry)
    assert plan.action is IntentAction.ESCALATE
    assert plan.reason == reason


def test_decide_local_intent_synonym_candidate(rule_engine):
    rule, registry = rule_engine
    plan = decide_local_intent("声量调到60", rule, registry)
    assert plan.action is IntentAction.TO_MODEL
    assert "set_volume" in plan.candidates


def test_session_resolve_multi_action_as_new_intent(rule_engine):
    rule, registry = rule_engine
    session = SessionState()
    session.set_pending("set_volume", "level", "音量", {}, param_type="int")
    analysis = DeterministicAnalyzer().analyze("打开浏览器并播放三国演义")
    action, _ = session.resolve("打开浏览器并播放三国演义", rule, analysis=analysis)
    assert action == "new"
    assert session.pending is None


def test_session_resolve_still_fills_int_without_analysis(rule_engine):
    rule, registry = rule_engine
    session = SessionState()
    session.set_pending("set_volume", "level", "音量", {}, param_type="int")
    action, value = session.resolve("60", rule)
    assert action == "fill"
    assert value == 60
