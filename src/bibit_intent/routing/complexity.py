"""Model-free complexity gate sharing the existing utterance analysis."""
from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum
import re
from typing import Mapping, Sequence
import os

from bibit_intent.analysis.features import UtteranceAnalysis
from bibit_intent.routing.question_detector import QuestionDetector, count_question_words
from bibit_intent.analysis.lexical import mask_literals


class Complexity(str, Enum):
    SIMPLE = "simple"
    COMPLEX = "complex"


@dataclass(frozen=True)
class ComplexityResult:
    complexity: Complexity
    score: int
    features: Mapping[str, int | bool]
    tokens: tuple[str, ...]
    upos: tuple[str, ...]
    reasons: tuple[str, ...]
    escalation_reason: str = ""

    @property
    def route(self) -> str:
        return "cloud" if self.complexity is Complexity.COMPLEX else "edge"


@dataclass(frozen=True)
class ComplexityThresholds:
    """Runtime-tunable soft signals; hard safety signals remain unconditional."""
    long_cjk_chars: int = 30
    long_english_words: int = 20
    conjunction_count: int = 1
    punctuation_count: int = 2
    verb_count: int = 3
    question_count: int = 2
    score: int = 2

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if type(value) is not int or not 1 <= value <= 4096:
                raise ValueError(f"complexity {item.name} must be an integer in [1, 4096]")

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "ComplexityThresholds":
        values = os.environ if environ is None else environ
        resolved = {}
        for item in fields(cls):
            name = f"BIBIT_INTENT_COMPLEXITY_{item.name.upper()}"
            try:
                resolved[item.name] = int(values.get(name, item.default))
            except (ValueError, TypeError) as error:
                raise ValueError(f"{name} must be an integer") from error
        return cls(**resolved)


def classify_complexity(
    text: str, *, analysis: UtteranceAnalysis | None = None,
    pos: Sequence[tuple[str, str]] | None = None,
    thresholds: ComplexityThresholds | None = None,
) -> ComplexityResult:
    """Reuse deterministic analysis; simple never grants execution permission."""
    thresholds = thresholds or ComplexityThresholds.from_environment()
    if analysis is None:
        from bibit_intent.analysis.deterministic import default_analyzer
        analysis = default_analyzer().analyze(text)
    if analysis.lexical_error:
        return ComplexityResult(Complexity.COMPLEX, 0, {"character_count": len(text)},
                                (), (), (analysis.lexical_error,), analysis.lexical_error)
    if pos is not None:
        tokens = tuple(token for token, _ in pos)
        tags = tuple(tag for _, tag in pos)
        verbs = sum(tag in {"VERB", "AUX"} for tag in tags)
        connectors = sum(tag in {"CCONJ", "SCONJ"} for tag in tags)
    else:
        tokens, tags = analysis.tokens, analysis.pos_tags
        verbs, connectors = len(analysis.predicates), len(analysis.connectors)
    questions = count_question_words(text)
    visible = mask_literals(text)
    punctuation = len(re.findall(r"[,，;；]", visible))
    features = {
        "character_count": len(text), "token_count": len(tokens),
        "verb_count": verbs, "conjunction_count": connectors,
        "question_count": questions, "comma_semicolon_count": punctuation,
    }
    signals = (
        (len(re.findall(r"[\u3400-\u9fff]", visible)) > thresholds.long_cjk_chars
         or len(re.findall(r"\b[A-Za-z]+\b", visible)) > thresholds.long_english_words, 1, "long-request"),
        (connectors >= thresholds.conjunction_count, 1, "has-conjunction"),
        (punctuation >= thresholds.punctuation_count, 1, "punctuation-threshold"),
        (verbs >= thresholds.verb_count, 2, "verbs-threshold"),
        (questions >= thresholds.question_count, 1, "questions-threshold"),
        (verbs >= 2 and (connectors > 0 or punctuation > 0), 1, "multiple-actions"),
    )
    score = sum(weight for active, weight, _ in signals if active)
    reasons = tuple(reason for active, _, reason in signals if active)
    question = QuestionDetector().detect(text)
    features["open_question"] = question.is_question
    hard_signals = (
        (analysis.is_conditional, "conditional_intent"),
        (analysis.is_multi_action, "multi_intent"),
        (analysis.is_multi_target, "multi_target"),
        (analysis.is_context_dependent, "context_dependent"),
        (analysis.is_negated_action, "negated_action"),
        (question.is_question, "open_question"),
    )
    escalation = next((reason for active, reason in hard_signals if active), "")
    if not escalation and score >= thresholds.score:
        escalation = "complex_intent"
    if not escalation and not verbs and not questions and not any(tag == 'NOUN' for tag in tags):
        escalation = "insufficient_evidence"
    if escalation:
        reasons += (escalation,)
    return ComplexityResult(
        Complexity.COMPLEX if escalation else Complexity.SIMPLE,
        score, features, tokens, tags, reasons, escalation,
    )
