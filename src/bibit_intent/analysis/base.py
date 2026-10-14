"""分析器接口。"""

from __future__ import annotations

from typing import Any, Protocol

from bibit_intent.analysis.features import UtteranceAnalysis


class Analyzer(Protocol):
    """文本分析器协议。

    当前只要求 analyze()，context 保留给后续篇章分析（如 session.pending）。
    实现必须保证异常时返回默认 UtteranceAnalysis，不能中断主路由。
    """

    def analyze(
        self, text: str, context: dict[str, Any] | None = None
    ) -> UtteranceAnalysis:
        ...
