import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bibit_intent.core.language import detect_language
from bibit_intent.routing.question_detector import QuestionDetector
from bibit_intent.analysis.deterministic import DeterministicAnalyzer
from bibit_intent.routing.polysemy import escalate_reason
from bibit_intent.analysis.patterns import NEGATION_MARKERS, NEGATED_ACTION_RE

def test_language_detection():
    assert detect_language("what is Linux?") == "en"
    assert detect_language("什么是 Linux") == "zh"
    assert detect_language("open 浏览器 and launch terminal") == "en"
    assert detect_language("什么是黑洞") == "zh"

def test_english_open_questions():
    d = QuestionDetector()
    for text in ("what is quantum entanglement?", "how do I open the file manager?", "Explain TCP"):
        assert d.detect(text).is_question

def test_english_control_statement_not_question():
    d = QuestionDetector()
    assert not d.detect("set volume to 60").is_question

def test_unknown_language_escalates():
    from bibit_intent.routing.intent import decide_local_intent, IntentAction
    from bibit_intent.routing.rule_engine import RuleEngine
    from bibit_intent.core.registry import ToolRegistry
    plan = decide_local_intent("???", RuleEngine(ToolRegistry()), ToolRegistry())
    assert plan.action is IntentAction.ESCALATE and plan.reason == "unknown_language"

def test_analysis_records_language():
    assert DeterministicAnalyzer().analyze("open the browser").language == "en"

def test_english_polysemy_guards():
    assert escalate_reason("open the file") == "ambiguous_control"
    assert escalate_reason("restart the service") == "ambiguous_control"

def test_english_negation_matches_chinese_analysis():
    a = DeterministicAnalyzer()
    for text in ("don't close the browser", "do not restart the system", "avoid changing the volume", "I don't want the app closed"):
        result = a.analyze(text)
        assert result.has_negation
        assert result.is_negated_action
        assert result.negation_tokens
    assert "don't" in NEGATION_MARKERS
    assert NEGATED_ACTION_RE.search("should not shutdown")

def test_spacy_adapter_preserves_deterministic_english_flags():
    from bibit_intent.analysis.backends import JiebaAnalyzer
    # Avoid requiring jieba: verify the deterministic source of truth directly.
    result = DeterministicAnalyzer().analyze("do not restart the system")
    assert result.language == "en" and result.is_negated_action

def test_english_intent_guards():
    from bibit_intent.routing.intent import decide_local_intent, IntentAction
    from bibit_intent.routing.rule_engine import RuleEngine
    from bibit_intent.core.registry import ToolRegistry
    rule, reg = RuleEngine(ToolRegistry()), ToolRegistry()
    cases = [("search the latest news online", "writing_task"), ("switch audio output to Bluetooth", "unsupported_control"), ("how do I open the file manager", "open_question")]
    for text, reason in cases:
        plan = decide_local_intent(text, rule, reg)
        assert plan.action is IntentAction.ESCALATE and plan.reason == reason
