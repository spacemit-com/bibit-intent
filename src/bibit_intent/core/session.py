"""
agent/session.py — 会话级槽位状态（缺参多轮回填）

对照 docs/intent/slot_missing_management.md。

背景：执行层发现工具缺少必填参数时（Executor 返回 need_param），
传统做法是让用户整句重发。本模块把「等待补参」做成会话级状态：

  第一轮  用户："查一下 python3 依赖什么" → 缺 pkg → 记 pending
  第二轮  用户："python3"                 → 自动回填 pkg → 直接执行

业界对照（详见文档）：
  - 传统任务型对话：slot filling + DST 状态跟踪，unfilled/filled 帧
  - LLM function calling：让模型"问而非编"（OpenAI system message、
    Ask-when-Needed）；把提问做成工具（Anthropic AskUserQuestion）
  - 决策层：Expected Regret，低风险给默认值+告知，高风险维持确认

本实现设计要点：
  1. 缺参检测留在确定性层（执行层 _prepare_params），1B 模型只负责填值。
  2. 回填前先判"新意图"：高置信规则命中 / 开放问答 / 明确控制指令 /
     疑问词 → 换话题，清空 pending 走正常路由，避免把新指令误填成参数值。
  3. 纯确认语（"嗯/好的/继续"）→ 保持 pending，再提示一次（有最大轮次
     上限防死循环）。
  4. confirm 工具（危险操作）回填后仍走执行层二次确认。
  5. 多参数工具一次只缺一个：回填后仍缺 → 重新挂起，逐轮补齐。

未传入共享分析结果时使用离线分析器，不调用模型。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from bibit_intent.analysis.features import UtteranceAnalysis

# 明确控制指令特征词（与 agent/intent.py _EXPLICIT_CONTROL_KEYWORDS 保持一致）
_EXPLICIT_CONTROL_KEYWORDS = [
    "打开", "启动", "运行", "开启",
    "关闭", "退出", "停止", "结束",
    "安装", "卸载", "删除", "升级", "重启", "重新启动", "关机", "休眠", "睡眠", "待机", "挂起",
    "reboot", "restart", "shutdown", "hibernate", "suspend", "standby",
]

# 纯确认/继续语：不填槽、不换话题，保持 pending 再提示一次
_CONFIRM_WORDS = {
    "是", "嗯", "好", "好的", "可以", "对", "行", "继续",
    "就这样", "ok", "okay", "yes", "y", "1", "confirm", "go ahead",
}

# 确认语：确认执行「模型推测值」（router 三级证据处置，见 _suspend_confirm）
_CONFIRM_REPLY_WORDS = {
    "确认", "确定", "是", "嗯", "好", "好的", "可以", "对", "行", "继续",
    "就这样", "允许", "允许降级", "确认降级", "ok", "okay", "yes", "y",
}

# 取消语：放弃执行模型推测值，清空确认挂起
_CANCEL_WORDS = {"不", "不用", "不要", "取消", "算了", "不必", "不执行", "no", "n"}

# 疑问词：视为换话题（新意图），不误填成参数值。
# 如 pending pkg 时用户问"今天天气怎么样"→ 不应把整句填成包名。
_QUESTION_WORDS = ("吗", "呢", "怎么样", "怎么", "什么", "为什么", "哪些", "多少", "几", "咋")

# str 参数回填时剥离的常见口语填充前缀
_FILLER_RE = re.compile(
    r"^(请|麻烦|帮我|给我|把|将|设|设置|设成|设为|改成|改为|调|调到|调成|"
    r"用|是|就|为|要|查|看|一下|下|个|叫|名字是|名称是)"
)

# str 回填拒绝的动作动词前缀：pending 时用户说出这类短语视为换话题
# （"帮我写一首诗"不该被填成 pkg=写一首诗）
_ACTION_VERB_RE = re.compile(
    r"^(写|作|讲|介绍|念|唱|画|设计|做|制造|说|推荐|教我|告诉我|读|听|玩|打开|关闭)"
)

# 显式 参数=值 / 参数:值 / 参数是值
_EXPLICIT_VALUE_RE = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*[=:是]\s*(\S.{0,39})")

_PARAM_NAME_ALIASES = {
    "pkg": {"pkg", "package", "package_name"},
}

# 新意图判定阈值（与 agent/intent.py RULE_CONFIDENCE_THRESHOLD 一致）
RULE_CONFIDENCE_THRESHOLD = 0.7

# 等待补参的最大轮次：超限后视为换话题，清空 pending
MAX_WAIT_TURNS = 3


@dataclass
class PendingSlot:
    """一条等待补参的挂起槽位。"""
    tool_name: str
    param_name: str
    param_label: str
    param_type: str = "str"
    filled: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    turn: int = 0
    confirm: bool = False   # True=模型推测值待确认（三级证据处置，非缺参补值）
    validator: str | None = None   # 合法取值正则（与执行层 enum/pattern 同源）
    choices: list[str] = field(default_factory=list)   # N 选一候选（用户可回序号）


class SessionState:
    """会话状态：目前只维护一条 pending 槽位（单帧 DST 简化版）。"""

    def __init__(self, max_wait_turns: int = MAX_WAIT_TURNS):
        self.pending: PendingSlot | None = None
        self._max_wait_turns = max_wait_turns

    def set_pending(
        self,
        tool_name: str,
        param_name: str,
        param_label: str,
        filled: dict[str, Any],
        message: str = "",
        param_type: str = "str",
        validator: str | None = None,
        choices: list[str] | None = None,
    ) -> None:
        self.pending = PendingSlot(
            tool_name=tool_name,
            param_name=param_name,
            param_label=param_label,
            param_type=param_type,
            filled=dict(filled or {}),
            message=message,
            validator=validator,
            choices=list(choices or []),
        )

    def set_pending_confirm(
        self,
        tool_name: str,
        params: dict[str, Any],
        param_name: str,
        param_label: str,
        message: str,
        param_type: str = "int",
    ) -> None:
        """挂起「模型推测值确认」：等用户确认执行 / 给新值 / 取消。"""
        self.pending = PendingSlot(
            tool_name=tool_name,
            param_name=param_name,
            param_label=param_label,
            param_type=param_type,
            filled=dict(params or {}),
            message=message,
            confirm=True,
        )

    def clear_pending(self) -> None:
        self.pending = None

    def resolve(
        self,
        text: str,
        rule=None,
        analysis: UtteranceAnalysis | None = None,
    ) -> tuple[str, Any]:
        """
        决定本轮回填动作。返回 (action, value)：
          "fill" — 提取到参数值 value；pending 保留，由上层执行成功后清除
          "keep" — 纯确认语，仍缺参；pending 保留（超轮次则清空并返回 "new"）
          "new"  — 换话题/新意图，pending 已清空，上层走正常路由
          "ambiguous" — 标量回答无法完整解析，pending 已清空，交给云端
        """
        if self.pending is None:
            return "new", None
        t = text.strip()
        if not t:
            return "keep", None
        if self.pending.confirm:
            return self.resolve_confirm(text, rule, analysis)
        if _unsafe_reply(t, analysis):
            self.clear_pending()
            return "new", None

        # N 选一候选：pending.choices 非空且用户回纯数字时，按 1-based 序号映射。
        # 必须早于确认语判断：否则「1」会被 _CONFIRM_WORDS 当成确认语吞掉，
        # 而不是选中第 1 个候选。
        if self.pending.choices and re.fullmatch(r"\d{1,3}", t):
            idx = int(t) - 1
            if 0 <= idx < len(self.pending.choices):
                return "fill", self.pending.choices[idx]

        if self.pending.tool_name == "pkg_match_preview":
            from bibit_intent.core.package_preview import PREVIEW_MODES, PREVIEW_SCOPES
            key = self.pending.param_name
            value = t.removeprefix(key + "=").removeprefix(key + ":").strip()
            aliases = PREVIEW_SCOPES if key == "scope" else PREVIEW_MODES if key == "mode" else {}
            if value in aliases or value in aliases.values():
                return "fill", aliases.get(value, value)
            if key == "pattern" and (t.startswith("pattern=") or t.startswith("pattern:")):
                return "fill", value

        # bool 槽位："是/否"本身就是值，优先于确认语判断。
        # 但纯数字/百分比这类能直接当合法值的回答（对 ratio 而言 "1" 就是 1.00）
        # 不能被确认语抢先吃掉，否则用户回 "1" 会被反复追问"还差一个参数"。
        if (
            self.pending.param_type != "bool"
            and t.lower() in _CONFIRM_WORDS
            and not (self.pending.validator and _fill_ok(self.pending, t))
        ):
            self.pending.turn += 1
            if self.pending.turn > self._max_wait_turns:
                self.clear_pending()
                return "new", None
            return "keep", None

        # 新意图检测：高置信规则命中 / 开放问答 / 明确控制指令 / 疑问词
        if analysis is not None and (
            analysis.is_multi_action
            or analysis.is_negated_action
            or analysis.is_context_dependent
        ):
            self.clear_pending()
            return "new", None
        if rule is not None:
            rm = rule.match(t, analysis)
            if rm and rm.confidence >= RULE_CONFIDENCE_THRESHOLD:
                self.clear_pending()
                return "new", None
            if rule.is_open_question(t):
                self.clear_pending()
                return "new", None
        if any(kw in t for kw in _EXPLICIT_CONTROL_KEYWORDS):
            self.clear_pending()
            return "new", None
        if any(q in t for q in _QUESTION_WORDS):
            self.clear_pending()
            return "new", None

        if self.pending.tool_name == "pkg_match_preview" and self.pending.param_name == "pattern" and re.fullmatch(r"[\x21-\x7e]{1,128}", t):
            return "fill", t

        ok, value = _extract_value(self.pending, t)
        if ok:
            return "fill", value
        action = "ambiguous" if self.pending.param_type in ("int", "float", "bool") else "new"
        self.clear_pending()
        return action, None

    def resolve_confirm(
        self,
        text: str,
        rule=None,
        analysis: UtteranceAnalysis | None = None,
    ) -> tuple[str, Any]:
        """
        确认挂起决策。返回 (action, value)：
          "confirm" — 确认执行 pending.filled（模型推测值）
          "fill"    — 用户给了新值 value，替换后执行
          "keep"    — 空输入，保持 pending 再提示
          "cancel"  — 用户取消，pending 已清空
          "new"     — 换话题/新意图，pending 已清空，走正常路由
          "ambiguous" — 标量回答无法完整解析，pending 已清空，交给云端
        """
        t = text.strip()
        if not t:
            return "keep", None
        low = t.lower()
        if low in _CANCEL_WORDS:
            self.clear_pending()
            return "cancel", None
        if _unsafe_reply(t, analysis):
            self.clear_pending()
            return "new", None
        if low in _CONFIRM_REPLY_WORDS:
            return "confirm", None
        # 新意图守卫（与 resolve 一致）：规则命中 / 开放问答 / 控制指令 / 疑问词
        if analysis is not None and (
            analysis.is_multi_action
            or analysis.is_negated_action
            or analysis.is_context_dependent
        ):
            self.clear_pending()
            return "new", None
        if rule is not None:
            rm = rule.match(t, analysis)
            if rm and rm.confidence >= RULE_CONFIDENCE_THRESHOLD:
                self.clear_pending()
                return "new", None
            if rule.is_open_question(t):
                self.clear_pending()
                return "new", None
        if any(kw in t for kw in _EXPLICIT_CONTROL_KEYWORDS):
            self.clear_pending()
            return "new", None
        if any(q in t for q in _QUESTION_WORDS):
            self.clear_pending()
            return "new", None
        # 数值槽位：用户直接给新数字 → 替换建议值
        if self.pending.param_type in ("int", "float"):
            ok, value = _numeric_reply(self.pending, t)
            if ok:
                return "fill", value
        action = "ambiguous" if self.pending.param_type in ("int", "float", "bool") else "new"
        self.clear_pending()
        return action, None


def _unsafe_reply(text: str, analysis: UtteranceAnalysis | None) -> bool:
    if analysis is None:
        from bibit_intent.analysis.deterministic import default_analyzer
        analysis = default_analyzer().analyze(text)
    return bool(analysis.lexical_error or analysis.is_conditional or analysis.is_multi_action
                or analysis.is_multi_target or analysis.is_negated_action)


def _numeric_reply(pending: PendingSlot, text: str) -> tuple[bool, Any]:
    names = {pending.param_name, pending.param_label}
    names.update(_PARAM_NAME_ALIASES.get(pending.param_name, ()))
    prefix = '|'.join(re.escape(name) for name in sorted(names, key=len, reverse=True) if name)
    match = re.fullmatch(
        r'\s*(?:(?:请|就|please)\s*)?(?:(?:' + prefix + r')\s*)?'
        r'(?:(?:=|:|：|是|调到|调成|设置为|设为|改为|改成|to)\s*)?'
        r'(?P<number>[+-]?\d+(?:\.\d+)?)(?:\s*[%％])?[。!！]?\s*', text, re.I,
    )
    if not match or (pending.param_type == 'int' and '.' in match['number']):
        return False, None
    value = float(match['number']) if pending.param_type == 'float' else int(match['number'])
    return (True, value) if _fill_ok(pending, value) else (False, None)


def _fill_ok(pending: PendingSlot, value: Any) -> bool:
    """回填值是否落在该参数的合法取值内（enum/pattern 由 router 归一成 validator）。

    不做这一步会把整句当值填进去（"屏幕刷新率up" → mode=屏幕刷新率up），
    下一轮再由执行层报"取值不合法"，用户看到的就是莫名其妙的参数错误。
    """
    if not pending.validator:
        return True
    return bool(re.fullmatch(pending.validator, str(value).strip()))


def _extract_value(pending: PendingSlot, text: str) -> tuple[bool, Any]:
    """从文本中提取缺失参数的值。返回 (是否提取到, 值)。"""
    pname = pending.param_name
    if pending.param_type in ('int', 'float'):
        return _numeric_reply(pending, text)

    # 1) 显式 参数=值 / 参数:值 / 参数是值（最不易歧义）
    m = _EXPLICIT_VALUE_RE.fullmatch(text)
    if m and m.group(1) in _PARAM_NAME_ALIASES.get(pname, {pname}):
        if pending.param_type == "bool":
            return _boolean_reply(m.group(2).strip())
        val = _coerce(pending, m.group(2).strip())
        return (True, val) if _fill_ok(pending, val) else (False, None)

    # 3) bool → 是/否类词（否定优先："不要"含"要"）
    if pending.param_type == "bool":
        return _boolean_reply(text)

    # 4) str → 循环剥离填充词后取剩余文本（"python3"/"浏览器"/"/usr/bin/ls"）
    rest = text
    for _ in range(5):
        nxt = _FILLER_RE.sub("", rest)
        if nxt == rest:
            break
        rest = nxt
    rest = rest.strip(" ，。？！!?、")
    if not rest or len(rest) > 40:
        return False, None
    if _ACTION_VERB_RE.match(rest):
        # 动词开头（写诗/讲故事）→ 更可能是新意图，不当作参数值
        return False, None
    if not _fill_ok(pending, rest):
        return False, None
    return True, rest


def _boolean_reply(text: str) -> tuple[bool, Any]:
    value = text.strip().lower()
    if value in {"不", "否", "不要", "关", "停", "取消", "算了", "no", "false", "off", "n", "0"}:
        return True, False
    if value in {"是", "对", "要", "开", "启用", "允许", "确认", "确定", "可以", "继续", "yes", "ok", "true", "on", "y", "1"}:
        return True, True
    return False, None


def _coerce(pending: PendingSlot, raw: str) -> Any:
    """按参数类型把原始字符串转成目标类型。"""
    if pending.param_type in ("int", "float"):
        m = re.search(r"\d+(?:\.\d+)?", raw)
        if not m:
            return raw
        return float(m.group(0)) if pending.param_type == "float" else int(m.group(0))
    if pending.param_type == "bool":
        return raw.lower() in ("是", "对", "要", "开", "启用", "true", "yes", "y", "1", "on")
    return raw
