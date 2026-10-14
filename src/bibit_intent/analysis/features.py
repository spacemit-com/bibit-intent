"""统一的语言分析特征结构。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class UtteranceAnalysis:
    """一条用户输入的浅层语言分析结果。

    字段分三类：
      - 句法/语法：predicates/objects/negation_tokens
      - 篇章/话语：connectors/discourse_relations/is_context_dependent
      - 路由前置：is_multi_action/is_multi_target

    所有字段均有确定性默认值，分析器不可用或降级时调用方仍可安全使用。
    """

    text: str = ""
    language: str = "unknown"

    # 分词与词典词性提示；不是上下文消歧后的词性模型输出
    tokens: tuple[str, ...] = ()
    pos_tags: tuple[str, ...] = ()

    # 篇章/话语
    connectors: tuple[str, ...] = ()
    discourse_relations: tuple[str, ...] = ()

    # 句法/语法
    predicates: tuple[str, ...] = ()
    objects: tuple[str, ...] = ()
    has_negation: bool = False
    negation_tokens: tuple[str, ...] = ()
    is_negated_action: bool = False

    # 路由前置判定（P0 与 decide_local_intent 现有逻辑对齐）
    is_multi_action: bool = False
    is_multi_target: bool = False
    is_context_dependent: bool = False
    is_conditional: bool = False
    lexical_error: str = ""
    context_dependent_reason: str = ""

    analysis_latency_ms: float = 0.0
    debug: dict[str, object] = field(default_factory=dict)
