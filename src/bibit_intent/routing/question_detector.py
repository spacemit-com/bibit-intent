"""
agent/question_detector.py — 问句判别模块（开放知识问答三层判别）

背景（对照 docs/intent/rule_and_intent_design.md §4.1.2）：
早期 is_open_question 只靠一条窄正则，漏掉"亮度如何调节"这类无句尾
语气词的口语问句：规则未命中 → embedding 把它召回成 set_brightness →
1B 猜 level=50 → 证据闸门挂起确认（3548ms，误入控制链路）。

本模块把"开放知识问答/怎么做"判别独立成三层，命中即停：
  L1 强信号正则：解释/什么是/为什么…是/会/要/能/如何理解 等明确开放问答
  L2 组合特征：疑问词 ∧ 句尾问句标记（呢/啊/呀/？/?）
      —— 必须两者同时出现：既拦下"为什么没声音"（无标记，控制意图），
         也放走"现在CPU占用高吗"（无疑问词，状态查询本地可答）。
  L3 语义锚点：疑问词门控 + 与问句原型（data/question_corpus.jsonl）的
       余弦相似度 ≥ 阈值。覆盖 L1/L2 漏掉的"亮度如何调节/音量怎么调/
       怎么打开终端"（无句尾标记）。复用 EmbeddingRetriever 的索引、
       磁盘缓存与 BM25 基建，一次 /embedding；服务不可用静默降级。
  L3b 近命中带：余弦在 [阈值-0.12, 阈值) 之间 → 不是开放问答硬命中，
       但已非常接近问句 → 供意图决策"不注入候选"，让模型无工具可猜，
       只能 out_of_scope 升级（防 1B 在疑问句上幻觉 CALL，见 §2.3）。

判别对象是"开放知识问答"，不是所有疑问句；调用方保证先走 rule.match()
（规则能高置信直通的指令不进入本模块）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from bibit_intent.core.request_semantics import is_action_explanation
from bibit_intent.analysis.lexical import question_word_count, mask_literals


# L1 强信号：明确开放知识问答句式（沿用原 is_open_question 的正则）。
# 补充「告诉我/说说/说一下/回答一下」这类要求助手作答的口语请求。规则引擎
# 能高置信直通的控制指令（如「告诉我支持的刷新率」）会先于本判定被
# rule.match 拦截，因此这里不会误伤本地状态查询。
_STRONG_RE = re.compile(
    r"(解释|介绍|讲讲|讲一?讲|科普|阐述|的原理|是什么原理"
    r"|什么是|为什么.{0,8}(是|会|要|能)|如何理解|是怎么.{0,12}?的"
    r"|告诉我(?:一下|下)?|说说|说一下|回答(?:一下)?)"
)

# 祈使 + 疑问混合句：像「写进记忆，然后告诉我你用了哪个工具」这类以句号
# 结尾、疑问词不在句尾的复合句，L2（疑问词+句尾标记）抓不到，这里按
# 「作答动词 ∧ 疑问词」双向兜底识别为开放问答升级云端。
_REQUEST_ANSWER_RE = re.compile(
    r"(?:告诉|说说|讲讲|说一下|回答|问问|问一下).{0,12}"
    r"(?:哪个|哪些|哪一个|什么|为什么|为啥|怎么|如何|怎样|多少|几|哪|吗|呢)"
    r"|(?:哪个|哪些|哪一个|什么|为什么|为啥|怎么|如何|怎样|多少|几|哪)"
    r".{0,12}(?:告诉|说说|讲讲|说一下|回答|问问|问一下)"
)

# 疑问词（L2 组合特征与 L3 门控共用；"几/啥/干嘛/咋/哪"为口语疑问词）
_WH_WORD_RE = re.compile(
    r"(为什么|为啥|为何|怎么样|怎么办|怎么|如何|怎样|干嘛|哪些|什么|多少"
    r"|啥|咋|哪|几)"
)

# L2 组合特征：疑问词 + 句尾问句标记（呢/啊/呀/？/?）
# "为什么没声音"（无标记）是控制意图不命中；"为什么没声音呢"是问句命中。
_L2_RE = re.compile(
    r"(为什么|为啥|为何|怎么样|怎么办|怎么|如何|怎样|干嘛|哪些|什么|多少"
    r"|啥|咋|哪|几)"
    r"[^，。！!]{0,14}[呢啊呀？?]"
)

_EN_STRONG_RE = re.compile(
    r"\b(?:what\s+is|what\s+are|how\s+do\s+i|how\s+can\s+i|why\s+is|why\s+are|"
    r"who\s+is|when\s+did|where\s+is|explain|can\s+you\s+explain)\b", re.I)
_EN_L2_RE = re.compile(
    r"\b(?:what|why|how|who|when|where|which|can|could|does|do|is|are)\b[^.!]{0,120}\?", re.I)


@dataclass
class QuestionVerdict:
    """判别结果：is_question 为是否开放知识问答；layer 标记命中的层。"""
    is_question: bool
    layer: str            # strong / feature / semantic / none
    score: float = 0.0    # semantic 层的最大余弦相似度
    reason: str = ""      # 调试用：命中详情


def count_question_words(text: str) -> int:
    """Count non-overlapping Chinese WH words and bounded English WH words."""
    return question_word_count(text)


class QuestionDetector:
    """
    问句判别器（三层，命中即停）。

    retriever: 指向问句原型语料的 EmbeddingRetriever 实例（L3 用）。
               None 时只有 L1/L2 确定性层，完全离线可用。
    threshold: L3 语义层阈值（与问句原型的最大余弦）。
    """

    def __init__(self, retriever=None, threshold: float = 0.90):
        self._retriever = retriever
        self._threshold = threshold
        # L3b 近命中带宽度：余弦落在 [threshold-width, threshold) 视为近命中
        self._near_miss_width = 0.12

    def _semantic_score(self, text: str) -> float:
        """与问句原型的最大余弦（疑问词门控 + QUESTION 校验；无信号返回 0）。"""
        if self._retriever is None or not _WH_WORD_RE.search(text.strip()):
            return 0.0
        try:
            best = self._retriever.best_match(text)
        except Exception:
            return 0.0
        if best and best[0] == "QUESTION":
            return best[1]
        return 0.0

    def is_near_miss(self, text: str) -> bool:
        """
        L3b 近命中：余弦落在 [阈值-width, 阈值) 区间。
        语义上"非常像问句但差一点没达硬阈值"，供意图决策不注入候选，
        让 1B 无工具可猜（疑问句上幻觉 CALL 的主要场景）。
        """
        score = self._semantic_score(text)
        return self._threshold - self._near_miss_width <= score < self._threshold

    def detect(self, text: str) -> QuestionVerdict:
        t = mask_literals(text).strip()
        if not t:
            return QuestionVerdict(False, "none")

        # L1 强信号：明确开放问答句式，零成本
        if _STRONG_RE.search(t) or _REQUEST_ANSWER_RE.search(t):
            return QuestionVerdict(True, "strong")

        if _EN_STRONG_RE.search(t):
            return QuestionVerdict(True, "strong", reason="english")

        # L2 组合特征：疑问词 ∧ 句尾标记（避免单凭疑问词误伤控制指令）
        if _L2_RE.search(t):
            return QuestionVerdict(True, "feature")

        if _EN_L2_RE.search(t):
            return QuestionVerdict(True, "feature", reason="english")

        if is_action_explanation(t):
            return QuestionVerdict(True, "strong", reason="action_explanation")

        # L3 语义锚点：只有含疑问词才值得多花一次 /embedding
        # （"把声音开大一点"等无疑问词输入直接跳过，不产生额外调用）
        score = self._semantic_score(t)
        if score >= self._threshold:
            return QuestionVerdict(
                True, "semantic", score=score,
                reason=f"cosine={score:.3f}>={self._threshold}",
            )
        return QuestionVerdict(False, "none")

    def is_open_question(self, text: str) -> bool:
        """兼容旧接口：是否应作为开放知识问答升级。"""
        return self.detect(text).is_question
