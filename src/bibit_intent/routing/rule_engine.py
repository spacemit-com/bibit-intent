"""
agent/rule_engine.py — 第一段：规则引擎
职责：用近乎零成本的方式处理高频系统控制指令。
  匹配优先级：精确词 → 正则+槽位 → 关键词预筛
  命中时返回 RuleMatch(tool_name, slots, confidence)
  未命中返回 None → 路由到第二段

对照 design.md §4.1
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from bibit_intent.core.registry import ToolDef, ToolRegistry
from bibit_intent.core.command_contracts import (
    COMMAND_CONTRACTS, command_contract_allows, domain_anchor_allows, open_admin_request,
)
from bibit_intent.core.request_semantics import (
    file_search_object_allowed, is_action_explanation, write_action_targets, non_command_reason,
)
from bibit_intent.core.textmatch import term_hits
from bibit_intent.core.package_target import package_pattern_request
from bibit_intent.routing.multi_action import detect_multi_action, find_verbs
from bibit_intent.analysis.domain_lexicon import ACTION_VERBS
from bibit_intent.analysis.lexical import instruction_search, mask_literals
from bibit_intent.routing.question_detector import QuestionDetector
from bibit_intent.core.text_normalizer import evidence_number_candidates
from bibit_intent.knowledge.hallucination import (
    DetectionKind,
    Disposition,
    HallucinationRecord,
    HallucinationStats,
)


# 短式切换/改设单字动词词根：find_verbs 的词典 trie 只收录多字动作词，
# 会把正则交替组里的裸单字动词（(?:切|换|转)、(切换|换|改|设)）漏掉，
# 导致 analysis.predicates（换成/切到/转成/改到/设成）与 _pattern_verbs
# 交集为空，整工具被 predicate 过滤误跳过。截 同理：截图工具的 (?:截取|截)
# 交替组里「截」是裸单字动词。
_SHORT_VERB_ROOTS = frozenset("切换转改设截")
# 词库在这些正则上下文里给不出 VERB 词性、但确实是该工具动作词的多字词根。
# 例：截图工具写 `(?:截取|截)(?:屏幕|全屏)` 时，find_verbs 对「截取」返回空，
# 「帮我截取屏幕」就会被 predicates 门误过滤掉（实测 predicate=('截取',)）。
_SHORT_CN_VERB_ROOTS = ("截取", "抓取", "截屏", "截图")
# 英文短动词词根（take/snap/…）：find_verbs 只收词典里的多字动词，正则交替组
# 里裸的英文单字动词会被漏掉，导致 _pattern_verb_roots 认不出该工具的动作动词，
# 混合中英 pattern 的工具（如 take_photo）对英文输入被谓词门直接过滤掉。
# grab 与 take/snap 同类（grab a screenshot），词库里也没有。
_SHORT_EN_VERB_ROOTS = frozenset({"take", "snap", "grab"})
_VERB_PARTICLE_TAIL = "到成为回"


def _verb_root(verb: str) -> str:
    """把方向/结果复合动词归一到词根：换成→换、切到→切、改为→改。"""
    if len(verb) > 1 and verb[-1] in _VERB_PARTICLE_TAIL:
        return verb[:-1]
    return verb


def _pattern_verb_roots(patterns: list) -> set[str]:
    """提取工具规则里的动作动词词根，供 analysis.predicates 过滤使用。

    find_verbs 只认识词典里的多字动词，漏掉两类：
      1. 正则交替组里的裸单字动词（换/切/转/改/设）；
      2. 未被词典收录的英文切换动词（change）。
    这里在 find_verbs 基础上补齐这两类，并把复合动词（换成/切到）归一到词根，
    与 analysis.predicates 的归一形式对齐。
    """
    roots: set[str] = set()
    for pat in patterns:
        s = pat.pattern
        for verb in find_verbs(s):
            roots.add(verb)
            roots.add(_verb_root(verb))
        for cn in _SHORT_CN_VERB_ROOTS:
            if re.search(rf"[|(:]{re.escape(cn)}(?=[|)])", s):
                roots.add(cn)
        for ch in _SHORT_VERB_ROOTS:
            if re.search(rf"[|(:]{re.escape(ch)}(?=[|)])", s):
                roots.add(ch)
        for en in _SHORT_EN_VERB_ROOTS:
            if re.search(rf"[|(:]{re.escape(en)}(?=[|)])", s):
                roots.add(en)
        if re.search(r"[|(]change[|)]", s):
            roots.add("change")
    return roots


# 否定前缀与设备词之间的「有界间隔」：允许「蓝牙降噪耳机/无线头戴式音箱」
# 这类设备词前面带 0-4 个非切换动词字符（降噪/头戴式/入耳式…），但一旦
# 出现切换动词（切/换/转/改/设 + 到/成/为/回）就说明「蓝牙耳机」只是源设备，
# 「切到喇叭」的目标才是待填槽位，此时不得拦截（例：从蓝牙耳机切到喇叭→speaker）。
_SLOT_NEG_GAP_RE = r"[^切换转改设到成为回]{0,4}"


def _slot_map_neg_prefixed(text: str, key: str, neg_terms: list[str]) -> bool:
    """key 是否被否定前缀（蓝牙/无线）修饰，即「蓝牙/无线 + 设备词」。

    用于拦截「蓝牙耳机/无线音箱」被内置有线设备（headphone/speaker）误吃：
    只有设备词前面紧跟蓝牙/无线（可夹少量修饰词，但不能跨越切换动词）才拦截，
    避免「蓝牙连接后换到喇叭」「从蓝牙耳机切到喇叭」这类句中蓝牙只是源设备的场景被误伤。
    """
    if not neg_terms:
        return False
    return any(re.search(re.escape(neg) + _SLOT_NEG_GAP_RE + re.escape(key), text) for neg in neg_terms)


# 高危 admin 工具的封闭宾语白名单：裸动词关键词（重启/关机/睡眠…）只有在
# 「动词后无宾语，或宾语是设备/系统/模式类词」时才允许关键词弱命中，防止
# 「重启网络服务」「关闭防火墙」这类写动作被「重启/关机」子串误注入 reboot/
# shutdown。只约束关键词兜底（0.5 置信），不影响精确正则命中；正则本身可能
# 不完整（缺礼貌前缀/倒装/语气词），所以这里不做 fullmatch，只拦「动词 + 陌生宾语」。
_ADMIN_CLOSED_OBJECT: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "reboot": (("重启", "重新启动"), ("设备", "系统", "电脑", "机器", "主机")),
    "shutdown": (("关机",), ("设备", "系统", "电脑", "机器", "主机")),
    "sleep": (("睡眠", "休眠", "待机", "挂起"), ("设备", "系统", "电脑", "机器", "主机", "模式")),
}


def _admin_closed_object_violation(text: str, tool_name: str) -> bool:
    """动词后跟了非白名单宾语 → 关键词兜底应拦截。"""
    verbs, objects = _ADMIN_CLOSED_OBJECT.get(tool_name, ((), ()))
    if not verbs:
        return False
    for verb in verbs:
        for m in re.finditer(re.escape(verb), text):
            tail = text[m.end():].strip()
            # 跳过动词与宾语之间/之后的语气词，再剥句末标点空白
            tail = re.sub(r"^(?:一下|下|吧)\s*", "", tail)
            tail = re.sub(r"(?:一下|下|吧|了|啊|呀|呢|哦|嘛|吗)+[。！!？?、，,；;]*\s*$", "", tail)
            if not tail:
                continue
            if any(re.fullmatch(re.escape(o), tail) for o in objects):
                continue
            return True
    return False


# launch_app/close_app 的「非应用宾语」黑名单：这些是系统功能/服务/设置/对象，
# 不是可打开或关闭的应用名。应用名是开放集合，无法用封闭白名单，只能枚举排除
# 高频误命中的非应用词。作用于关键词兜底与正则负向前瞻两层。
_NON_APP_OBJECTS = (
    "防火墙", "自动更新", "录音", "定位", "脚本", "日志",
    "备份", "同步", "代理", "进程",
)

# 英文非应用宾语：与中文黑名单对称，再补高频英文误命中词。
# 刻意不含 file/document（"file manager"/"document viewer" 是合法应用名，
# 由 tools.yaml 负向前瞻的 `$` 锚点 + app 后缀豁免在规则层精确处理），
# 避免 keyword_candidates 把「打开文件管理器」这类合法应用误伤。
_NON_APP_OBJECTS_EN = (
    "benchmarks?", "scripts?", "tests?", "backups?", "updates?", "upgrades?",
    "diagnostic(?:s)?", "commands?", "tasks?", "jobs?", "services?",
    "process(?:es)?", "logs?", "syncs?", "prox(?:y|ies)", "firewalls?",
    "recordings?", "locations?", "bluetooth", "wi-?fi", "cameras?",
    "microphones?", "mics?", "speakers?", "headphones?", "ethernet", "nics?",
    "musics?", "videos?", "audios?", "songs?", "movies?", "playlists?",
    "lids?", "doors?", "books?", "pages?", "tabs?", "curtains?",
    "drawers?", "cabinets?", "blinds?", "deals?", "box(?:es)?",
)

_EN_NON_APP_OBJECT_RE = re.compile(
    rf"(?:{'|'.join(_NON_APP_OBJECTS_EN)})(?=\s|$|[，,。.；;！!？?])",
    re.I,
)

# 应用控制工具的裸动词/名词关键词：它们后面跟黑名单宾语时不应兜底召回。
_APP_CONTROL_VERBS: dict[str, tuple[str, ...]] = {
    "launch_app": ("打开", "启动", "运行", "开启", "应用", "程序", "open", "launch", "start", "run"),
    "close_app": ("关闭", "退出", "停止", "结束", "应用", "程序", "close", "quit", "exit", "stop", "kill"),
}


def _non_app_object_violation(text: str, tool_name: str) -> bool:
    """launch/close 的裸关键词后面跟了非应用宾语 → 关键词兜底应拦截。"""
    verbs = _APP_CONTROL_VERBS.get(tool_name, ())
    if not verbs:
        return False
    for verb in verbs:
        # 英文动词按 ASCII 单词边界 + 大小写不敏感匹配，避免
        # open→opening、run→running、kill→skill 这类子串误命中。
        if verb.isascii():
            matches = re.finditer(rf"\b{re.escape(verb)}\b", text, re.I)
        else:
            matches = re.finditer(re.escape(verb), text)
        for m in matches:
            # 「重新启动」是重启语义，不是 launch 的「启动」
            if verb == "启动" and m.start() >= 2 and text[m.start() - 2:m.start()] == "重新":
                return True
            tail = text[m.end():].strip()
            tail = re.sub(r"^(?:一下|下|吧)\s*", "", tail)
            if verb.isascii():
                tail = re.sub(r"^(?:the|an?)\s+", "", tail, flags=re.I)
            if not tail:
                continue
            for obj in _NON_APP_OBJECTS:
                # 词边界：只拦「裸功能词」（后面不是汉字），不误伤「录音机」、
                # 「脚本编辑器」「进程管理器」这类以黑名单词开头的应用名。
                if re.match(rf"{re.escape(obj)}(?![一-龥])", tail):
                    return True
            # 英文非应用宾语：词边界 + 结束锚点，不误伤 "file manager" 这类复合应用名
            if _EN_NON_APP_OBJECT_RE.match(tail):
                return True
            # 宾语以「服务」结尾（定位服务/网络服务）→ 非应用
            if re.search(r"服务[。！!？?、，,；;\s]*$", tail):
                return True
    return False


def _matches_closed_contract(text: str) -> bool:
    """文本是否命中某个封闭命令文法（user_add/user_del/pkg_update…）。

    这些是白名单里的单条确定性命令，即使动作词检测把它误判成多动作
    （如「删除账号 test」把用户名 test 当成第二个动词），也不应升级云端。
    """
    return any(contract.fullmatch(text.strip()) for contract in COMMAND_CONTRACTS.values())


@dataclass
class RuleMatch:
    tool_name: str
    slots: dict[str, Any]           # 从文本中抽取的槽位值
    confidence: float               # 0~1，规则引擎固定给 1.0
    matched_pattern: str = ""       # 调试用：命中的规则描述


# 精确词映射：特定短语 → (工具名, 槽位)
# 优先级最高，O(1) 查找
_EXACT_MAP: dict[str, tuple[str, dict]] = {
    "静音":     ("mute",   {"on": True}),
    "取消静音": ("mute",   {"on": False}),
    "关闭静音": ("mute",   {"on": False}),
    "重启":     ("reboot", {}),
    "重新启动": ("reboot", {}),
    "mute the sound": ("mute", {"on": True}),
    "mute sound": ("mute", {"on": True}),
    "mute the audio": ("mute", {"on": True}),
    "unmute": ("mute", {"on": False}),
    "turn off mute": ("mute", {"on": False}),
    "restart the computer": ("reboot", {}),
    "restart the system": ("reboot", {}),
    "reboot the system": ("reboot", {}),
    "lock the screen": ("lock_screen", {}),
}


def _blocks_rule_shortcut(multi: Any, text: str) -> bool:
    """Return true only for reliable multi-action structure signals.

    ``boundary`` is intentionally accepted for Chinese punctuation or when
    both verbs are dictionary actions.  The detector's conservative fallback
    may otherwise treat an English filename (``export report.docx``) as a
    second verb and break unrelated file rules.
    """
    if not multi.is_multi:
        return False
    if multi.reason in {"connector", "unresolved_action_clause", "many_verbs"}:
        return True
    return multi.reason == "boundary" and (
        # Do not treat filename dots or ordinary sentence whitespace as an
        # action boundary (for example ``export report.docx``).
        bool(re.search(r"[，,、；;。！!?？]", text))
        or all(word in ACTION_VERBS for word in multi.verbs)
    )


class RuleEngine:
    """
    规则引擎：三层匹配。
    1. 精确词表（O(1)）
    2. 正则 + 槽位抽取（来自 tools.yaml routes.rule.patterns）
    3. 关键词预筛（快速判断"可能涉及哪些工具"，无命中才升级到第二段）
    """

    CONFIDENCE_THRESHOLD = 0.7

    def __init__(
        self,
        registry: ToolRegistry,
        stats: HallucinationStats | None = None,
    ):
        self._registry = registry
        self._stats = stats if stats is not None else HallucinationStats()
        # 问句判别：默认 L1/L2 确定性层（离线可用）；main.py 会 attach
        # 带语义锚点（L3）的完整判别器。
        self._question = QuestionDetector()
        # 预编译：{ tool_name: [(pattern, compiled_re), ...] }
        self._patterns: dict[str, list[tuple[str, re.Pattern]]] = {}
        # 每个工具所有 pattern 中出现过的动作动词，供 analysis.predicates 过滤
        self._pattern_verbs: dict[str, set[str]] = {}
        # 预编译：{ tool_name: [keyword, ...] }
        self._keywords: dict[str, list[str]] = {}
        # 候选注入护栏：{ tool_name: [直接宾语, ...] }（见 docs §4.4-C）
        self._require_object: dict[str, list[str]] = {}
        for tool in registry.all():
            pats = tool.rule_patterns()
            if pats:
                self._patterns[tool.name] = [
                    (p.pattern, p) for p in pats
                ]
                self._pattern_verbs[tool.name] = _pattern_verb_roots(pats)
            kws = tool.rule_keywords()
            if kws:
                self._keywords[tool.name] = kws
            objs = tool.rule_require_object()
            if objs:
                self._require_object[tool.name] = objs

    @property
    def stats(self) -> HallucinationStats:
        """规则层统计（P0 埋点，不改变决策逻辑）。"""
        return self._stats

    def attach_stats(self, stats: HallucinationStats) -> None:
        """与路由器共享统计器：槽位抽取钳制计入同一份幻觉报告。"""
        self._stats = stats

    def attach_question_detector(self, detector: QuestionDetector) -> None:
        """注入完整问句判别器（L1/L2 确定性 + L3 语义锚点）。"""
        self._question = detector

    def is_near_miss_question(self, text: str) -> bool:
        """
        L3b 近命中：语义上非常像开放问答但差一点没达硬阈值
        （余弦落在 [阈值-0.12, 阈值) 区间）。
        供意图决策"不注入候选"，让 1B 模型无工具可猜，只能升级。
        """
        return self._question.is_near_miss(text)

    # ── 公开接口 ──────────────────────────────────────────────────────

    def match(
        self, text: str, analysis: Any | None = None
    ) -> RuleMatch | None:
        """
        尝试匹配输入文本。
        返回 RuleMatch（命中）或 None（未命中，需升级到第二段）。
        """
        text = text.strip()
        if non_command_reason(text):
            return None

        # A rule is a single closed command.  Multi-action requests must be
        # handled as a whole by the cloud/model path; otherwise the first
        # matching verb (for example launch_app in "open ... and delete ...")
        # can execute while the remaining action is silently discarded.
        multi = detect_multi_action(text)
        if _blocks_rule_shortcut(multi, text) and not _matches_closed_contract(text):
            return None

        # P2：analysis 标记为否定动作时，规则层直接放弃匹配，
        # 避免"帮我不要关浏览器"被 close_app 等正则误执行。
        if analysis is not None and analysis.is_negated_action:
            return None

        normalized = text.rstrip("。！？!? \t\r\n")
        if normalized != text:
            candidate = self._exact_match(normalized) or self._pattern_match(normalized, analysis)
            if candidate and candidate.tool_name in {
                "get_vol_status", "get_display_light", "adjust_volume",
                "adjust_brightness", "set_volume", "set_brightness", "mute",
                "lock_screen", "reboot", "shutdown",
            }:
                text = normalized

        # 1. 精确词表
        pattern_request = package_pattern_request(text)
        if pattern_request:
            tool_name, target = pattern_request
            return RuleMatch(tool_name, {"pkg": target}, 1.0, "unsupported_package_pattern")
        m = self._exact_match(text)
        if m:
            return m

        # 2. 正则 + 槽位
        m = self._pattern_match(text, analysis)
        if m and m.confidence >= self.CONFIDENCE_THRESHOLD:
            tool = self._registry.get(m.tool_name)
            if tool is not None and tool.mcp_group() == "readonly" and self._readonly_tool_blocked(tool, text):
                return None  # 写动作句不应命中只读工具
            # slot_map 补齐正则未捕获的槽位（如相对方向词的值）
            self._apply_slot_map(m.tool_name, text, m.slots, require_anchor=False)
            return m

        # 3. 关键词预筛：命中说明「很可能涉及」但没有精确槽位
        #    此处返回低置信度 match 让路由器决定是否直接走第二段
        m = self._keyword_match(text)
        if m:
            if self._apply_slot_map(m.tool_name, text, m.slots):
                # 相对方向词（"开大/调小/太暗"）由 slot_map 确定性补值
                # → 升级为高置信直通，不再交给 1B 模型猜边界值（100/0）
                m.confidence = 0.85
                m.matched_pattern = f"slot_map:{m.tool_name}"
            return m  # 未命中 slot_map 则保持低置信，交路由器升级

        return None

    def _keyword_allowed(self, tool_name: str, text: str) -> bool:
        """有副作用工具的弱命中护栏（§4.4-C）。

        工具声明了 require_object 时，只有在句中出现其直接宾语之一，
        才允许进入「关键词弱命中」路径。没有声明则一律放行。

        只读工具（mcp.group=readonly）在句子表达改写/替换/编译等写动作时
        也一律不注入：它们只负责查询对象状态，不应把「替换kernel」这类
        写动作句吸附成「查看内核版本」的候选（裸 kernel/内核 关键词过宽）。

        只作用于关键词预筛（0.5 置信），不影响精确正则命中。
        """
        if not command_contract_allows(tool_name, text) and not domain_anchor_allows(tool_name, text):
            return False
        # 高危 admin 工具：裸动词后面跟了陌生宾语（「重启网络服务」）时，
        # 关键词兜底不召回，交给云端澄清；「重启系统」「帮我重启」仍放行。
        if _admin_closed_object_violation(text, tool_name):
            return False
        if _non_app_object_violation(text, tool_name):
            return False
        tool = self._registry.get(tool_name)
        if tool is not None and tool.mcp_group() == "readonly" and self._readonly_tool_blocked(tool, text):
            return False
        objs = self._require_object.get(tool_name)
        if not objs:
            return True
        return any(term_hits(text, obj) for obj in objs)

    _CLAUSE_SPLIT_RE = re.compile(
        r"[，,。；;！!？?\n]+"
        r"|然后呢|接下来|再然后|最后再|下一步|并且|而且|然后|接着|随后|之后|同时|顺便|以及|还有|另外|最后|再|先"
        r"|\b(?:then|and|after|before|also)\b",
        re.I,
    )

    _CJK = re.compile(r"[\u4e00-\u9fff]")

    def _readonly_object_terms(self, tool: ToolDef) -> list[str]:
        """只读工具的可改写对象：优先 covers，否则取含中文的对象类关键词。

        命令式关键词（netstat -rn / ip route / free -m 等）不含中文，不视作对象。
        """
        covers = [c for c in tool.covers if c.strip()]
        if covers:
            return covers
        return [k for k in tool.rule_keywords() if self._CJK.search(k)]

    def _readonly_tool_blocked(self, tool: ToolDef, text: str) -> bool:
        """只读工具在「其对象被写动作针对」时拦截（关键词与正则两层共用）。

        不能对整句做子串命中：`查看替换后的内核版本` 里的「替换」是定语而不是
        谓语，`查看内核版本，再替换壁纸` 里的「替换」针对的是壁纸而不是内核，
        整句拦截都会把 sys_kernel 从候选里误删。因此按子句切分，并要求写动词
        直接以该对象为宾语（见 write_action_targets 的「的」边界）。
        """
        terms = self._readonly_object_terms(tool)
        if not terms:
            return False
        for clause in self._CLAUSE_SPLIT_RE.split(text):
            clause = clause.strip()
            if not clause:
                continue
            if any(write_action_targets(clause, term) for term in terms):
                return True
        return False

    def keyword_candidates(self, text: str) -> list[str]:
        """
        根据关键词快速预筛候选工具名列表。
        用于第二段「动态候选注入」：只把相关工具签名注入 prompt。
        """
        multi = detect_multi_action(text)
        if _blocks_rule_shortcut(multi, text):
            return []
        text = mask_literals(text)
        if is_action_explanation(text):
            return []
        hits: list[str] = []
        for tool_name, kws in self._keywords.items():
            if tool_name == "file_search" and not file_search_object_allowed(text):
                continue
            if not self._keyword_allowed(tool_name, text):
                continue
            if any(term_hits(text, kw) for kw in kws):
                hits.append(tool_name)
        return hits

    def match_tool(self, text: str, tool_name: str) -> RuleMatch | None:
        """
        只对该工具的正则模式做匹配 + 槽位抽取。
        用于 embedding 语义路由容错兜底：embedding 选出工具后，
        用该工具的规则模式补抽参数；抽不到再由上层提示用户补充。
        """
        multi = detect_multi_action(text)
        if (non_command_reason(text)
                or _blocks_rule_shortcut(multi, text)
                or not command_contract_allows(tool_name, text)):
            return None
        if tool_name == "file_search" and not file_search_object_allowed(text):
            return None
        if tool_name in {"pkg_install", "pkg_remove"} and package_pattern_request(text):
            return None
        compiled = self._patterns.get(tool_name)
        if not compiled:
            return None
        tool_def = self._registry.get(tool_name)
        for pat_str, pat in compiled:
            m = instruction_search(pat, text)
            if not m:
                continue
            slots = self._extract_slots(m.groupdict(), tool_def, tool_name)
            self._apply_slot_map(tool_name, text, slots, require_anchor=False)
            confidence = 1.0 if m.group(0) == text.strip() else 0.85
            return RuleMatch(
                tool_name=tool_name,
                slots=slots,
                confidence=confidence,
                matched_pattern=pat_str,
            )
        return None

    def is_open_question(self, text: str) -> bool:
        """
        开放知识问答前置判断：在第二段模型推理之前，用确定性规则识别
        “解释/什么是/为什么…”这类开放问答，直接升级到第三段，避免 1B
        把它误吸附到只读工具（如 sysinfo）。

        实现委托给问句判别模块（agent/question_detector.py）：
          L1 强信号正则；L2 疑问词+句尾标记组合特征；
          L3 语义锚点（需 attach_question_detector 注入 embedding）。
        设计要点：只抓明确的开放问答，绝不误伤控制指令
        （如“为什么没声音”是控制意图，不应命中）与本地可答的状态查询
        （“现在CPU占用高吗”）。若已能被规则引擎匹配到工具，调用方应
        先走 match()，此判断仅用于规则未命中之后。
        """
        return self._question.is_open_question(text)

    # ── 内部方法 ──────────────────────────────────────────────────────

    def _exact_match(self, text: str) -> RuleMatch | None:
        result = _EXACT_MAP.get(text)
        if result:
            tool_name, slots = result
            return RuleMatch(
                tool_name=tool_name,
                slots=slots,
                confidence=1.0,
                matched_pattern=f"exact:{text}",
            )
        return None

    def _pattern_match(
        self, text: str, analysis: Any | None = None
    ) -> RuleMatch | None:
        best: RuleMatch | None = None
        for tool_name, compiled in self._patterns.items():
            if not command_contract_allows(tool_name, text):
                continue
            if tool_name == "file_search" and not file_search_object_allowed(text):
                continue
            if analysis is not None and analysis.predicates:
                tool_verbs = self._pattern_verbs.get(tool_name, set())
                predicate_roots = set(analysis.predicates) | {_verb_root(p) for p in analysis.predicates}
                if tool_name not in COMMAND_CONTRACTS and tool_verbs and not (tool_verbs & predicate_roots):
                    continue
            tool_def = self._registry.get(tool_name)
            for pat_str, pat in compiled:
                m = instruction_search(pat, text)
                if not m:
                    continue
                # 抽取槽位：命名捕获组 → 参数值，并做类型转换
                slots = self._extract_slots(m.groupdict(), tool_def, tool_name)
                confidence = 1.0 if m.group(0) == text.strip() else 0.85
                hit = RuleMatch(
                    tool_name=tool_name,
                    slots=slots,
                    confidence=confidence,
                    matched_pattern=pat_str,
                )
                if best is None or hit.confidence > best.confidence or (
                    hit.confidence == best.confidence and tool_name in COMMAND_CONTRACTS
                    and best.tool_name not in COMMAND_CONTRACTS
                ):
                    best = hit
        return best

    def rejected_command_contract(self, text: str) -> bool:
        # Only reject an open admin verb with no domain evidence. A request
        # with a valid domain may still go through the local model; execution
        # is checked again by command_contract_allows in Router.
        return open_admin_request(text)

    def matches_closed_contract(self, text: str) -> bool:
        """输入是否整句命中某个封闭命令文法（user_add/user_del/pkg_update…）。

        供 intent 决策层在 multi_intent 等升级判断之前复用：封闭命令
        文法已把动词与宾语都锁死，即使动作词检测把用户名/包名误判成
        第二个动词（如「删除账号 test」里的 test），也不应升级云端。
        """
        return _matches_closed_contract(text)

    def _keyword_match(self, text: str) -> RuleMatch | None:
        text = mask_literals(text)
        for tool_name, kws in self._keywords.items():
            if tool_name == "file_search" and not file_search_object_allowed(text):
                continue
            if not self._keyword_allowed(tool_name, text):
                continue
            if any(term_hits(text, kw) for kw in kws):
                return RuleMatch(
                    tool_name=tool_name,
                    slots={},
                    confidence=0.5,      # 低置信，仅供预筛
                    matched_pattern=f"keyword:{tool_name}",
                )
        return None

    def _apply_slot_map(self, tool_name: str, text: str, slots: dict, require_anchor: bool = True) -> bool:
        """
        按 tools.yaml routes.rule.slot_map 补齐槽位（键为文本子串）。
        返回是否命中。命中后调用方可提升关键词命中的置信度（0.5→0.85）。

        用途：相对方向词（"把声音开大一点"）文本里没有数字，1B 模型又
        不可靠（实测 大→100、小→0，加提示仍不稳定），由确定性规则直接
        给适中值（大→80、小→20…）。slot_map 此前在配置里但从未被消费，
        本方法让它真正生效。
        """
        tool = self._registry.get(tool_name)
        if not tool:
            return False
        rule_cfg = (tool.routes or {}).get("rule") or {}
        sm = rule_cfg.get("slot_map") or {}
        anchor = rule_cfg.get("slot_map_anchor") or []
        neg_lookbehind = rule_cfg.get("slot_map_neg_lookbehind") or []
        text = mask_literals(text)
        # 宾语锚定：slot_map 的裸相对词（"太暗"/"最亮"/"dim"）必须搭配该工具的
        # 有效宾语（屏幕/亮度/音量…）才允许提升置信度，否则「房间太暗」「灯泡最亮」
        # 会被误执行为屏幕亮度调节。
        if anchor and require_anchor and not any(term_hits(text, a) for a in anchor):
            return False
        hit = False
        for key, mapping in sm.items():
            output_format = tool_name == "appcli_run" and key in {"pdf", "png"} and str(slots.get("output", "")).lower().endswith("." + key)
            # ASCII key 用词边界匹配（避免 "dp" 误命中 "headphone"、"up" 误命中
            # "backup"、"ram" 误命中 "program"），中文 key 保持子串匹配。
            if (term_hits(text, key) or output_format) and not _slot_map_neg_prefixed(text, key, neg_lookbehind):
                for k, v in mapping.items():
                    if k not in slots:
                        slots[k] = v
                hit = True
        return hit

    def _extract_slots(
        self,
        groups: dict[str, str | None],
        tool_def,
        tool_name: str | None = None,
    ) -> dict[str, Any]:
        """把正则命名捕获组转换为工具参数类型。"""
        slots: dict[str, Any] = {}
        if tool_def is None:
            return slots
        for pname, pdef in tool_def.params.items():
            raw = groups.get(pname)
            if raw is None:
                continue
            try:
                if pdef.type in ("int", "float"):
                    try:
                        val = float(raw) if pdef.type == "float" else int(raw)
                    except ValueError:
                        # 中文数字档位（六成→60/百分之六十→60/半→50）
                        vals = evidence_number_candidates(raw)
                        if not vals:
                            continue
                        val = vals[-1]
                    if pdef.range:
                        if not (pdef.range[0] <= val <= pdef.range[1]):
                            self._stats.record(HallucinationRecord(
                                kind=DetectionKind.PARAMETER,
                                disposition=Disposition.CLAMPED,
                                tool=tool_name, param=pname, value=val,
                                detail=(
                                    f"clamp_to_{pdef.range[0]}~{pdef.range[1]}"
                                ),
                            ))
                        val = max(pdef.range[0], min(pdef.range[1], val))
                    slots[pname] = val
                elif pdef.type == "bool":
                    slots[pname] = raw.lower() in ("true", "1", "yes", "on")
                else:
                    slots[pname] = raw
            except (ValueError, TypeError):
                pass  # 抽取失败跳过，执行层会用默认值补全
        return slots
