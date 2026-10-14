import importlib.util
from dataclasses import fields
import json
from pathlib import Path

import pytest

from bibit_intent.analysis.features import UtteranceAnalysis
from bibit_intent.config import AgentConfig
from bibit_intent.pipeline import PipelineSettings
from bibit_intent.routing.complexity import Complexity, ComplexityThresholds, classify_complexity


@pytest.mark.parametrize("raw", ["0", "-1", "4097", "nan", "2.5", ""])
def test_bad_environment_fails_explicitly(raw):
    with pytest.raises(ValueError):
        ComplexityThresholds.from_environment({"BIBIT_INTENT_COMPLEXITY_SCORE": raw})


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, 4097])
def test_direct_thresholds_are_validated(value):
    with pytest.raises(ValueError):
        ComplexityThresholds(score=value)


def test_environment_and_dotenv_entrypoints_agree(tmp_path):
    environment = {"BIBIT_INTENT_COMPLEXITY_SCORE": "5", "BIBIT_INTENT_COMPLEXITY_VERB_COUNT": "6"}
    path = tmp_path / ".env"
    path.write_text("\n".join(f"{key}={value}" for key, value in environment.items()))
    expected = ComplexityThresholds(score=5, verb_count=6)
    assert PipelineSettings.from_environment(environment).complexity_thresholds == expected
    assert AgentConfig(_env_file=path).complexity_thresholds == expected


def test_soft_score_changes_but_conditions_do_not():
    analysis = UtteranceAnalysis(tokens=("custom",), pos_tags=("VERB",), predicates=("custom",), connectors=("and",))
    text = "custom, alpha, beta"
    assert classify_complexity(text, analysis=analysis).complexity is Complexity.COMPLEX
    relaxed = ComplexityThresholds(score=3)
    assert classify_complexity(text, analysis=analysis, thresholds=relaxed).complexity is Complexity.SIMPLE
    assert classify_complexity("如果电量充足就安装curl", thresholds=relaxed).escalation_reason == "conditional_intent"


@pytest.mark.parametrize("name", [item.name for item in fields(ComplexityThresholds)])
def test_every_threshold_is_shared_by_both_configuration_entrypoints(name):
    environment = {f"BIBIT_INTENT_COMPLEXITY_{name.upper()}": "9"}
    assert getattr(PipelineSettings.from_environment(environment).complexity_thresholds, name) == 9
    assert getattr(AgentConfig(_env_file=None, **environment).complexity_thresholds, name) == 9


def evaluator():
    path = Path(__file__).resolve().parents[1] / "scripts/evaluate_complexity_corpus.py"
    spec = importlib.util.spec_from_file_location("complexity_evaluator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("rows", [[], [{"text": "hello", "label": None}], [{"text": "", "label": "simple"}],
                                 [{"text": "hello", "label": "simple"}] * 2])
def test_evaluation_rejects_invalid_labels_or_duplicates(tmp_path, rows):
    path = tmp_path / "corpus.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows))
    with pytest.raises(ValueError):
        evaluator().load_rows(path)


def test_confusion_matrix_and_missing_class_are_explicit():
    module = evaluator()
    result = module.metrics([{"label": "complex", "predicted": "simple"}, {"label": "simple", "predicted": "complex"}])
    assert result["fn"] == result["fp"] == 1
    assert result["false_simple_rate"] == result["false_complex_rate"] == 1
    assert module.metrics([])["complex_recall"] is None


def test_serve_uses_dotenv_thresholds(tmp_path, monkeypatch):
    from bibit_intent import cli, pipeline, server

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("BIBIT_INTENT_COMPLEXITY_SCORE=7\n")
    seen = {}

    def fake_package(**kwargs):
        seen["settings"] = kwargs["settings"]
        return object()

    monkeypatch.setattr(pipeline.EmbeddedBibitPipeline, "from_package", fake_package)
    monkeypatch.setattr(server, "run_server", lambda *args, **kwargs: None)
    cli._cmd_serve(["--data-root", str(tmp_path)])
    assert seen["settings"].complexity_thresholds.score == 7


def test_router_snapshots_thresholds_for_pending_and_normal_paths(monkeypatch):
    from bibit_intent.core.registry import ToolRegistry
    from bibit_intent.core.session import SessionState
    from bibit_intent.execution.executor import Executor
    from bibit_intent.routing import router as router_module
    from bibit_intent.routing.intent import IntentAction, IntentPlan
    from bibit_intent.routing.rule_engine import RuleEngine

    registry = ToolRegistry()
    thresholds = ComplexityThresholds(score=7)
    router = router_module.Router(registry, RuleEngine(registry), None, Executor(registry), complexity_thresholds=thresholds)
    monkeypatch.setenv("BIBIT_INTENT_COMPLEXITY_SCORE", "invalid")
    seen = []
    classify = router_module.classify_complexity

    def pending_classifier(*args, **kwargs):
        seen.append(kwargs["thresholds"])
        return classify(*args, **kwargs)

    def normal_classifier(*args, **kwargs):
        seen.append(kwargs["complexity_thresholds"])
        return IntentPlan(action=IntentAction.ESCALATE, reason="complex_intent")

    monkeypatch.setattr(router_module, "classify_complexity", pending_classifier)
    monkeypatch.setattr(router_module, "decide_local_intent", normal_classifier)
    session = SessionState()
    session.set_pending_confirm("set_volume", {"level": 40}, "level", "音量", "确认？")
    router.handle("60，如果电量充足", session)
    router.handle("normal request")
    assert seen == [thresholds, thresholds]
