"""端侧多动作检测（离线词典与结构规则，不调用模型）。

目的：一句话含多个动作（"打开浏览器并播放三国演义"）时，规则层在进模型前
**确定性升级云端**，避免被 launch_app 等单工具规则误吃、或 1B 模型把两件事
塞进一个 CALL（见 docs/intent/multi_intent_analysis.md）。

策略与调研结论一致：**不拆分、只检测**；命中复合信号即返回
``ESCALATE reason=multi_intent``，由云端拆成动作序列。

信号（三层，由强到弱）：
  1. 动作动词数 ≥ 2 且存在连接词；
  2. 动作动词数 ≥ 2 且动作之间被标点/空白分隔；
  3. 动作动词数 ≥ 3（即使无连接词，也大概率是复合指令）。

正常路径使用共享词典分析及子句约束，支持英文词形；词库故障时，
底层辅助函数保留 Trie 匹配兼容性，但统一路由会明确升级云端。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from bibit_intent.analysis.domain_lexicon import ACTION_VERBS, CONNECTORS
from bibit_intent.analysis.lexical import lexical_analysis, mask_literals

# 宽松单字动词：只在「连接词已把句子切成多个子句」时，用于判断某个子句是否
# 是动作子句（布尔，不计数）。这里只收歧义低的口语动词；「开/关/存/装/卸/
# 发/设」等易撞名词（开关/内存/存储/装置/发现/设备）或已由双字词
# （打开/关闭/保存/安装/卸载/发送/设置）覆盖，故不放进来，优先不误伤单意图。
LENIENT_SINGLE_VERBS = frozenset(
    "查看听试测搜找写读改换删说讲告问记"
)


class _TrieNode:
    __slots__ = ("children", "end")

    def __init__(self) -> None:
        self.children: dict[str, _TrieNode] = {}
        self.end = False


class _Trie:
    """极小 Trie：按字符插入，支持从左到右返回所有最长匹配。"""

    def __init__(self, words: list[str]) -> None:
        self.root = _TrieNode()
        for word in words:
            node = self.root
            for ch in word:
                node = node.children.setdefault(ch, _TrieNode())
            node.end = True

    def find_all(self, text: str) -> list[tuple[int, int, str]]:
        """返回 [(start, end, word), ...]，end 为半开区间；重叠时保留最长。"""
        out: list[tuple[int, int, str]] = []
        n = len(text)
        i = 0
        while i < n:
            node = self.root
            j = i
            last_end: int | None = None
            while j < n and text[j] in node.children:
                node = node.children[text[j]]
                j += 1
                if node.end:
                    last_end = j
            if last_end is not None:
                out.append((i, last_end, text[i:last_end]))
                i = last_end  # 跳过已匹配区间，避免重叠计数
            else:
                i += 1
        return out


_VERB_TRIE = _Trie(ACTION_VERBS)
_CONNECTOR_TRIE = _Trie(CONNECTORS)

_TERMINAL_ACTION_CHAIN = re.compile(
    r"(?:打开|启动|开启)\s*(?:一个)?(?:终端|命令行|控制台|qterminal)"
    r"(?:窗口)?\s*(?:进行|以便|用于|来|去)?\s*"
    r"(?:查询|查看|检查|运行|执行|输入|显示|查一下|查(?!询|看|一下|找))"
    r"\s*(?!\s|器|工具|管理器)\S+",
    re.I,
)

_TERMINAL_NOMINAL_ACTION = re.compile(
    r"(?:打开|启动|开启)\s*(?:一个)?(?:终端|命令行|控制台|qterminal)"
    r"(?:窗口)?\s*(?:进行|做)\s*[^，,。！？!?;；\n]{1,40}?"
    r"(?:查询|检查|检测|诊断)(?:一下)?(?:吗|么|好吗|可以吗)?\s*[？?！!。]*$",
    re.I,
)

# 动作间的天然边界：标点或空白
_BOUNDARY_RE = re.compile(r"[\s，,、；;。.!！?？]")

# 子句级疑问信号：连接词切出的某个子句是提问时，也应视为「多意图/复杂意图」信号。
_CLAUSE_QUESTION_RE = re.compile(
    r"(为什么|为啥|为何|怎么样|怎么办|怎么|如何|怎样|多少|哪些|哪个|哪一个|"
    r"什么|啥|咋|哪|几|吗|呢|是不是|能不能|能否|是否)"
)


@dataclass
class MultiActionResult:
    is_multi: bool
    verbs: list[str] = field(default_factory=list)
    connectors: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class MultiObjectResult:
    """单谓词 + 多个并列宾语/信息项。"""

    is_multi: bool
    predicate: str = ""
    objects: list[str] = field(default_factory=list)


_READ_PREDICATE_RE = re.compile(
    r"^(?:请|麻烦|帮我|请帮我|给我)?\s*"
    r"(?P<predicate>查看|查询|查一下|查|看一下|看下|看看|显示|列出|获取|"
    r"show|view|check|get|display|list)\s*(?:一下|下)?\s*",
    re.I,
)
# 并列宾语分隔符：支持单个、连续和混合标点（A、B、C / A,,B / A、B，C）。
_OBJECT_SPLIT_RE = re.compile(
    r"\s*(?:以及|和|及|与|跟|and|,|，|、|;|；)\s*",
    re.I,
)
_OBJECT_TRAIL_RE = re.compile(r"[\s。.!！?？]+$")


def _is_ascii_bounded(text: str, start: int, end: int, word: str) -> bool:
    """英文动词要求左右是单词边界，避免 "open" 命中 "opening" / "reopen"。"""
    if not word.isascii():
        return True
    left_ok = start == 0 or not (text[start - 1].isalnum() or text[start - 1] == "_")
    right_ok = end == len(text) or not (text[end].isalnum() or text[end] == "_")
    return left_ok and right_ok


def find_verbs(text: str) -> list[str]:
    """返回经过词典、领域词和子句位置约束的动作词列表。"""
    return [word for _, _, word in _verb_spans(text)]


def _verb_spans(text: str) -> list[tuple[int, int, str]]:
    tokens, error = lexical_analysis(text)
    if not error:
        result = []
        clause_has_predicate = False
        at_clause_head = True
        for token in tokens:
            if token.pos == 'CCONJ' or token.text in ',，;；。.!?！？':
                clause_has_predicate = False
                at_clause_head = True
                continue
            if token.pos != 'VERB' or (len(token.text) < 2 and token.text not in ACTION_VERBS):
                if token.pos not in {'ADV', 'DET'} and token.text not in {'我', '你', 'me', 'to'}:
                    at_clause_head = False
                continue
            if token.lemma in ACTION_VERBS or (not clause_has_predicate and at_clause_head):
                result.append((token.start, token.end, token.text.lower()))
                clause_has_predicate = True
            at_clause_head = False
        return result
    visible = mask_literals(text).lower()
    return [(start, end, word) for start, end, word in _VERB_TRIE.find_all(visible)
            if _is_ascii_bounded(visible, start, end, word)]


def _connector_spans(text: str) -> list[tuple[int, int, str]]:
    """返回连接词区间 [(start, end, word), ...]，按文本顺序。

    英文连接词按词边界过滤，避免 "or" 命中 "for/record"、"and" 命中 "hand" 等
    子串误切；中文连接词不受词边界约束（中文无空格分词）。
    """
    tokens, error = lexical_analysis(text)
    if not error:
        return [(token.start, token.end, token.text.lower()) for token in tokens if token.pos == 'CCONJ']
    text = mask_literals(text).lower()
    spans: list[tuple[int, int, str]] = []
    for start, end, word in _CONNECTOR_TRIE.find_all(text):
        if not word.isascii() or _is_ascii_bounded(text, start, end, word):
            spans.append((start, end, word))
    return spans


def find_connectors(text: str) -> list[str]:
    """返回文本中的连接词列表。"""
    return [word for _, _, word in _connector_spans(text)]


def _split_clauses(text: str, spans: list[tuple[int, int, str]]) -> list[str]:
    """按连接词区间切分子句，去掉只含标点/空白的空子句。"""
    parts: list[str] = []
    cursor = 0
    for start, end, _ in spans:
        parts.append(text[cursor:start])
        cursor = end
    parts.append(text[cursor:])
    return [part.strip() for part in parts if part.strip()]


def _clause_has_action(clause: str) -> bool:
    """判断一个子句是否含动作/疑问信号。

    已收录动词（find_verbs）优先；否则允许安全的口语单字动词兜底。这里只做
    布尔判断，不做动词计数，避免「查内存」里「内存」的「存」被误当成第二个动作。
    疑问词也计为信号：像「先调音量再告诉我怎么省电」这类「动作+提问」复合句
    同样需要升级云端，而不是被端侧单工具误吃。
    """
    if find_verbs(clause):
        return True
    if _CLAUSE_QUESTION_RE.search(clause):
        return True
    return any(ch in LENIENT_SINGLE_VERBS for ch in clause)

def detect_multi_action(text: str) -> MultiActionResult:
    """检测一句话是否含多个动作，命中则建议升级云端。

    连接词是主结构信号：先按连接词切子句，只要有 ≥2 个子句含动作就判复合，
    不再要求每个动作都命中封闭动词表。无连接词时退回保守的动词计数/边界逻辑。
    """
    text = mask_literals(text.strip())
    verbs = find_verbs(text)
    connector_spans = _connector_spans(text)
    connectors = [word for _, _, word in connector_spans]
    if _TERMINAL_ACTION_CHAIN.search(text) or _TERMINAL_NOMINAL_ACTION.search(text):
        return MultiActionResult(
            True, verbs=verbs, connectors=connectors, reason="terminal_action_chain"
        )
    if connector_spans:
        clauses = _split_clauses(text, connector_spans)
        if sum(1 for clause in clauses if _clause_has_action(clause)) >= 2:
            return MultiActionResult(
                True, verbs=verbs, connectors=connectors, reason="connector"
            )
        if verbs and len(clauses) >= 2 and any(connector in {
            '并', '并且', '然后', '顺便', '接着', '随后', '再', 'then', 'also', 'after', 'before',
        } for connector in connectors):
            return MultiActionResult(True, verbs=verbs, connectors=connectors,
                                     reason="unresolved_action_clause")
        if verbs and 'and' in connectors and not _READ_PREDICATE_RE.match(text):
            for clause in clauses[1:]:
                tokens, error = lexical_analysis(clause)
                words = [token for token in tokens if token.pos != 'PUNCT']
                if not error and len(words) >= 2 and words[0].pos == 'X':
                    return MultiActionResult(True, verbs=verbs, connectors=connectors,
                                             reason="unresolved_action_clause")
    if len(verbs) < 2:
        return MultiActionResult(False, verbs=verbs, connectors=connectors)

    if len(verbs) >= 3:
        return MultiActionResult(
            True, verbs=verbs, connectors=connectors, reason="many_verbs"
        )

    # 两个动词、无连接词：判断二者之间是否被标点/空白隔开。
    spans = [(start, end) for start, end, _ in _verb_spans(text)]
    if len(spans) >= 2:
        first_end = min(e for _, e in spans)
        last_start = max(s for s, _ in spans)
        between = text[first_end:last_start]
        if between and _BOUNDARY_RE.search(between):
            return MultiActionResult(
                True, verbs=verbs, connectors=connectors, reason="boundary"
            )

    return MultiActionResult(False, verbs=verbs, connectors=connectors)


def detect_multi_object_query(text: str) -> MultiObjectResult:
    """识别“查看 A、B、C”这类单谓词多宾语只读查询。

    多动作检测只数动词，因此「查看系统版本、内核版本、CPU、内存」只看到
    一个“查看”，会被规则层第一个命中的 sys_* 工具局部吃下。这里补一层
    浅层并列结构解析：剥掉句首只读谓词，对剩余部分按标点/连词切分；
    宾语组 >= 2 才返回 multi。
    """
    text = (text or "").strip()
    if detect_multi_action(text).is_multi:
        return MultiObjectResult(False)
    m = _READ_PREDICATE_RE.match(text)
    if not m:
        return MultiObjectResult(False)
    remainder = _OBJECT_TRAIL_RE.sub("", text[m.end():]).strip()
    if not remainder:
        return MultiObjectResult(False)
    objects = [part.strip() for part in _OBJECT_SPLIT_RE.split(remainder) if part.strip()]
    if len(objects) < 2:
        return MultiObjectResult(False)
    return MultiObjectResult(True, predicate=m.group("predicate"), objects=objects)
