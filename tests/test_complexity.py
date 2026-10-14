from bibit_intent.routing.complexity import Complexity, ComplexityThresholds, classify_complexity


def test_short_single_action_stays_on_edge():
    result = classify_complexity("下载 zenow")
    assert result.complexity is Complexity.SIMPLE
    assert result.route == "edge"


def test_complexity_thresholds_are_configurable_without_changing_hard_guards(monkeypatch):
    thresholds = ComplexityThresholds(score=4, long_cjk_chars=100, long_english_words=100,
                                      punctuation_count=10, verb_count=10, question_count=10)
    assert classify_complexity("配置服务然后验证结果", thresholds=thresholds).complexity is Complexity.COMPLEX
    monkeypatch.setenv("BIBIT_INTENT_COMPLEXITY_SCORE", "4")
    assert ComplexityThresholds.from_environment().score == 4
    assert classify_complexity("如果条件满足就执行", thresholds=thresholds).complexity is Complexity.COMPLEX


def test_chinese_multi_action_goes_to_cloud():
    result = classify_complexity("请下载 zenow，并安装它，然后配置并测试服务，可以吗？")
    assert result.complexity is Complexity.COMPLEX
    assert result.route == "cloud"
    assert result.features["verb_count"] >= 2
    assert result.escalation_reason == "multi_intent"


def test_english_multi_action_goes_to_cloud():
    result = classify_complexity("Find the log, summarize it, and upload the report; also explain the errors.")
    assert result.complexity is Complexity.COMPLEX
    assert result.features["conjunction_count"] >= 1


def test_external_upos_provider_is_supported():
    result = classify_complexity("do this and that", pos=[("do", "VERB"), ("this", "PRON"), ("and", "CCONJ"), ("that", "PRON")])
    assert result.features["verb_count"] == 1
    assert result.complexity is Complexity.SIMPLE


def test_result_is_explainable():
    result = classify_complexity("请查日志，修复服务，再验证结果")
    assert result.reasons
    assert result.features["conjunction_count"] > 0
    assert len(result.tokens) == len(result.upos)


def test_shared_analysis_is_reused():
    from bibit_intent.analysis.features import UtteranceAnalysis
    analysis = UtteranceAnalysis(is_multi_action=True, predicates=("custom",))
    result = classify_complexity("custom", analysis=analysis)
    assert result.escalation_reason == "multi_intent"
    assert result.features["verb_count"] == 1


def test_word_boundaries_and_external_token_alignment():
    assert classify_complexity("download standard").features["conjunction_count"] == 0
    result = classify_complexity("打开浏览器", pos=[("打开", "VERB"), ("浏览器", "NOUN")])
    assert result.tokens == ("打开", "浏览器")
    assert result.upos == ("VERB", "NOUN")


def test_question_detection_is_shared():
    from bibit_intent.routing.question_detector import count_question_words
    assert count_question_words("为什么以及如何？ what and why?") == 4
    assert count_question_words("this download somewhere") == 0
    assert classify_complexity("为什么需要安装软件").escalation_reason == "open_question"


def test_decision_gate_prevents_partial_execution():
    from bibit_intent.core.registry import ToolRegistry
    from bibit_intent.routing.rule_engine import RuleEngine
    from bibit_intent.routing.intent import decide_local_intent, IntentAction
    registry = ToolRegistry()
    rule = RuleEngine(registry)
    for text in (
        "请把我的名字 dec 写进长期记忆，然后告诉我你用了哪个记忆工具。",
        "open browser and play music",
    ):
        plan = decide_local_intent(text, rule, registry)
        assert plan.action == IntentAction.ESCALATE
        assert plan.reason == "multi_intent"
        assert plan.analysis.debug["complexity"]["type"] == "complex"


def test_weak_complexity_gate_runs_before_model_recall():
    from bibit_intent.core.registry import ToolRegistry
    from bibit_intent.routing.rule_engine import RuleEngine
    from bibit_intent.routing.intent import decide_local_intent, IntentAction
    registry = ToolRegistry()
    plan = decide_local_intent(
        "alpha beta gamma delta epsilon zeta, eta theta, iota kappa lambda "
        "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda",
        RuleEngine(registry), registry,
    )
    assert plan.action == IntentAction.ESCALATE
    assert plan.reason == "complex_intent"
