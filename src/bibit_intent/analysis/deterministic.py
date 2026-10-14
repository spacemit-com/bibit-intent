"""离线词典和结构规则组成的确定性分析器，不加载训练模型。"""

from __future__ import annotations

import time
from typing import Any

from bibit_intent.analysis.features import UtteranceAnalysis
from bibit_intent.analysis.patterns import (
    CONTEXT_DEPENDENT_RE,
    MULTI_PKG_TARGET_RE,
    NEGATED_ACTION_RE,
    NEGATION_MARKERS,
)
from bibit_intent.routing.multi_action import detect_multi_action
from bibit_intent.core.language import detect_language
from bibit_intent.analysis.lexical import lexical_analysis, structural_flags, mask_literals, negation_markers


class DeterministicAnalyzer:
    """现有规则的确定性分析实现。

    设计目标不是完整句法/依存解析，而是先把现有多动作、上下文依赖、否定
    等规则收敛成统一特征，作为后续接入真正 parser 时的接口与基线。
    """

    def analyze(
        self, text: str, context: dict[str, Any] | None = None
    ) -> UtteranceAnalysis:
        t0 = time.monotonic()
        text = (text or "").strip()
        if not text:
            return UtteranceAnalysis(text=text)

        tokens, lexical_error = lexical_analysis(text)
        if lexical_error:
            return UtteranceAnalysis(text=text, language=detect_language(text[:4096]),
                                     lexical_error=lexical_error)
        multi = detect_multi_action(text)
        conditional, negated = structural_flags(text, tokens)
        visible = mask_literals(text).lower()
        ctx = CONTEXT_DEPENDENT_RE.search(visible)
        neg_tokens = negation_markers(text, NEGATION_MARKERS)
        predicates = tuple(multi.verbs)
        is_negated_action = bool(NEGATED_ACTION_RE.search(mask_literals(text))) or negated

        result = UtteranceAnalysis(
            text=text,
            language=detect_language(text),
            tokens=tuple(token.text for token in tokens),
            pos_tags=tuple(token.pos for token in tokens),
            is_conditional=conditional,
            lexical_error=lexical_error,
            connectors=tuple(multi.connectors),
            predicates=predicates,
            has_negation=bool(neg_tokens),
            negation_tokens=neg_tokens,
            is_negated_action=is_negated_action,
            is_multi_action=multi.is_multi,
            is_multi_target=bool(MULTI_PKG_TARGET_RE.search(visible)),
            is_context_dependent=ctx is not None,
            context_dependent_reason="context_dependent" if ctx else "",
            analysis_latency_ms=(time.monotonic() - t0) * 1000,
            debug={
                "multi_action_reason": multi.reason,
                "multi_action_count": len(multi.verbs),
                "lexical_backend": "jieba-dictionary+wordnet-3.0",
            },
        )
        return result


_DEFAULT_ANALYZER = DeterministicAnalyzer()


def default_analyzer() -> DeterministicAnalyzer:
    """返回进程级共享的默认分析器。"""
    return _DEFAULT_ANALYZER
