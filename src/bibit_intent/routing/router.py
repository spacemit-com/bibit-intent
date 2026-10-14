"""
agent/router.py — 三段式路由器（核心）
串联：规则引擎 → 端侧模型 → 云端模型

路由逻辑（对照 design.md §4）：
  第一段（规则）：高置信命中 → 直接执行，成本≈0
  第二段（端侧）：低置信/未命中 → llama-server + GBNF，离线可用
  第三段（云端）：ESCALATE/端侧不可用 → 云端大模型（需联网）
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from bibit_intent.analysis.deterministic import default_analyzer
from bibit_intent.core.language import detect_language
from bibit_intent.core.app_request import normalize_app_request
from bibit_intent.routing.multi_action import detect_multi_action
from bibit_intent.routing.complexity import classify_complexity, Complexity, ComplexityThresholds
from bibit_intent.core.text_normalizer import evidence_number_candidates
from bibit_intent.execution.executor import Executor
from bibit_intent.routing.intent import (
    IntentAction,
    _cloud_reachable,
    decide_local_intent,
    is_network_fault_diagnosis,
)
from bibit_intent.routing.soft_gate import (
    SoftGateConfig,
    post_model_escalate,
    pre_model_escalate,
)
from bibit_intent.knowledge.knowledge import BM25KnowledgeBase
from bibit_intent.models.local_model import LocalModelClient
from bibit_intent.routing.rule_engine import RuleEngine
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.system_tools import ExecResult, resolve_multi_app_names
from bibit_intent.knowledge.hallucination import (
    DetectionKind,
    Disposition,
    HallucinationRecord,
    HallucinationStats,
)


class Stage(str, Enum):
    RULE = "rule"
    LOCAL = "local"
    CLOUD = "cloud"
    SLOT_FILL = "slot_fill"   # 会话级缺参回填（确定性直通，见 agent/session.py）
    CONFIRM = "confirm"       # 模型推测值待确认（三级证据处置，见 _suspend_confirm）


@dataclass
class RouteResult:
    stage: Stage
    tool_name: str | None
    params: dict[str, Any]
    exec_result: ExecResult | None
    escalate_reason: str | None = None
    latency_ms: float = 0.0
    debug: dict = field(default_factory=dict)


@dataclass
class EvidenceResult:
    """三级证据判定结果：kept=有依据可直接执行；uncertain=模型推测需确认。"""
    kept: dict[str, Any]
    uncertain: dict[str, Any]
    dropped: list[str] = field(default_factory=list)  # 未定义键（执行层会忽略）


def _normalize_evidence_text(text: str) -> str:
    """全角→半角（数字/字母/符号），用于原文证据匹配前的归一化。"""
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


_LAUNCH_APP_VERB_RE = re.compile(
    r"^(?:请帮我|请给我|帮我|给我|请|麻烦)?\s*(?:打开|启动|运行|开启)\s*(.+?)\s*$"
)

_MULTI_LAUNCH_APP_RE = re.compile(
    r"^(?:请帮我|请给我|帮我|给我|请|麻烦)?\s*打开\s*(.+?)\s*(?:和|以及|并且|同时)\s*(.+?)\s*$"
)


# 单应用工具 → (对象动词, 批量工具)。规则层把"打开A和B"确定性改写成批量调用，
# 避免整串被当成一个应用名 → app_not_found 上云 → 云端没有真正调用工具却复述成功。
_MULTI_APP_TOOL_ROUTES: dict[str, tuple[str, str]] = {
    "launch_app": ("打开|启动|运行|开启", "launch_apps"),
    "close_app": ("关闭|退出|停止|结束", "close_apps"),
}
_MULTI_APP_OBJECT_RE = {
    tool_name: re.compile(
        r"^(?:请帮我|请给我|帮我|给我|请|麻烦)?\s*(?P<verb>%s)\s*(?P<obj>.+?)\s*$"
        % verbs
    )
    for tool_name, (verbs, _target) in _MULTI_APP_TOOL_ROUTES.items()
}


def _multi_app_route(user_input: str, tool_name: str) -> tuple[str, list[str]] | None:
    """规则层命中单应用工具时，尝试确定性拆成批量工具调用。

    返回 ``(批量工具名, 应用名列表)``；拆不出 2 个可解析的真实应用返回 None，
    保持原有单应用路径与上云行为不变。
    """
    spec = _MULTI_APP_TOOL_ROUTES.get(tool_name)
    if spec is None:
        return None
    _verbs, target_tool = spec
    match = _MULTI_APP_OBJECT_RE[tool_name].match((user_input or "").strip())
    if not match:
        return None
    obj = match.group("obj").strip(" ？！。.!，,、")
    names = resolve_multi_app_names(obj)
    if names is None:
        return None
    return target_tool, names


def _extract_launch_app_name(text: str) -> str | None:
    """启动类指令兜底：剥离动词，把剩余文本当作应用名。

    端侧 GBNF 的 value 只允许 ASCII/数字，模型无法输出中文应用名；
    规则负向前瞻又可能漏掉合法应用名（如"文件浏览器"）。此时从原文
    确定性回抽，交给 launch_app 的别名映射兜底。
    """
    text = (text or "").strip()
    multi = _MULTI_LAUNCH_APP_RE.match(text)
    if multi:
        # Executor accepts one app per call; preserve both names for cloud/slot handling.
        return None
    m = _LAUNCH_APP_VERB_RE.match(text)
    if not m:
        return None
    name = m.group(1).strip(" ？！。.!，,、")
    return name or None


# ── launch/close 窄域标签层（L3 实验落地，见 glm_practice_experiment.md §6）──
# 候选收窄到 launch/close 时，端侧模型只判方向（CALL launch|close，不产参数），
# 应用名由 _deterministic_app_name 从原文抽取（方案 B：倒装/插入语/把字句），
# 再交执行层别名/.desktop 解析；抽不到则缺参澄清或升级云端，绝不本地猜名。
_APP_CONTROL_LABEL_TOOLS = frozenset({"launch_app", "close_app"})
_APP_TARGET_TOOLS = _APP_CONTROL_LABEL_TOOLS | {"launch_apps", "close_apps"}
_APP_LABEL_DIRECTION = {"launch": "launch_app", "close": "close_app"}

_APP_POLITE_RE = re.compile(
    r"^(?:请帮我|帮帮我|麻烦帮我|帮我|请给我|给我|麻烦你|麻烦|请|帮我个忙)\s*"
)
# 把字句：把/将 + 对象 + 方向动词（动词含插入语尾缀如"一下/起来/掉"）
_APP_BA_RE = re.compile(
    r"^(?:把|将)\s*(?P<obj>.+?)\s*"
    r"(?P<verb>打开|启动|运行|开启|弄出来|调出来|唤出|关闭|退出|退出去|停止|"
    r"结束|杀掉|终止|关了|关掉|关一下|关)\s*(?:一下|掉|起来|上去|下来|了)?$"
)
# 动词在前：打开/启动/… + 应用名（允许"打开一下记事本"这类插入语）
_APP_VERB_FIRST_RE = re.compile(
    r"^(?P<verb>打开|启动|运行|开启|关闭|退出|停止|结束)"
    r"\s*(?:一下)?\s*(?P<obj>.+?)\s*$"
)
# 应用名对象前后修饰语（在跑的/正在放的/那个/一下/起来…，剥离后才是核心名）
_APP_OBJ_PREFIX_NOISE_RE = re.compile(
    r"^(?:在跑的|正在放的|正在播放的|正在录屏的|正在运行的|正在用的|正在|"
    r"现在|当前|那个|这个|哪个|这些|那些|一个)\s*"
)
_APP_OBJ_SUFFIX_NOISE_RE = re.compile(r"(?:一下|起来|出来|上去|下来|了|掉)$")
# 指代占位 / 泛化名词：不能当作应用名执行（应澄清或升云）
_APP_PLACEHOLDER_RE = re.compile(
    r"^(?:一个|那个|这个|哪个|某个)(?:应用|软件|程序)$|^应用$|^软件$|^程序$|"
    r"^(?:文件|文档|文件夹|网页|网址)$"
)
_APP_CONJ_RE = re.compile(r"[和以及与、,，及]")


def _deterministic_app_name(user_input: str, tool_name: str) -> str | None:
    """方案 B 落地：从原文确定性抽取 launch/close 的应用名。

    覆盖直陈（打开浏览器）、把字句倒装（把浏览器打开）、插入语
    （打开一下记事本/在跑的记事本退出去/正在放的视频软件关一下）、
    口语动词（把微信弄出来）等句式；返回 None 表示无法确定应用名
    （缺参/指代占位/多应用串），由执行层缺参澄清或路由器升级云端，
    绝不本地猜名执行。
    """
    if detect_multi_action(user_input or "").is_multi:
        return None
    text = normalize_app_request(user_input or "").strip()
    text = _APP_POLITE_RE.sub("", text).strip(" ？！。.!，,、")
    if not text:
        return None
    obj: str | None = None
    ba = _APP_BA_RE.match(text)
    if ba:
        obj = ba.group("obj")
    else:
        vf = _APP_VERB_FIRST_RE.match(text)
        if vf:
            obj = vf.group("obj")
    if obj is None:
        return None
    name = obj.strip(" ？！。.!，,、")
    # 剥离对象前后修饰（至多 3 轮，防误伤应用名内含词）
    for _ in range(3):
        prev = name
        name = _APP_OBJ_PREFIX_NOISE_RE.sub("", name)
        name = _APP_OBJ_SUFFIX_NOISE_RE.sub("", name)
        name = name.strip(" ？！。.!，,、")
        if name == prev:
            break
    if not name or _APP_PLACEHOLDER_RE.fullmatch(name):
        return None
    if _APP_CONJ_RE.search(name):
        return None
    return name


def _keep_evidenced_params(
    user_input: str,
    params: dict[str, Any],
    tool,
    strict_str: bool = False,
) -> EvidenceResult:
    """
    参数原文证据闸门（三级证据）：数值参数按「原文证据」分级，防 1B 猜值。
    背景：实测端侧 1B 会把缺失数值猜成区间端点/中值（大→100/小→0，
    见 rule_and_intent_design.md §5.8）。prompt 防猜指令是软的、不可靠，
    这里在「执行前」做确定性判定（匹配前先做全角→半角归一化）：
      - kept：非数值（str/bool）原样保留；数值的值在归一化后的原文
        中出现（字面证据），或等于原文中文数字表达（规则证据，如
        「百分之五十」→50、「五成」→50，见 agent/cn_number.py）
        → 直接执行。
      - uncertain：数值既无字面证据也无中文数字证据（模型语义推测）
        → 不静默丢弃也不盲执行，交由会话层反问确认
        （见 router._suspend_confirm / session.resolve_confirm）。

    方案 A 扩展（glm_practice_experiment.md §9-10）：
      - 枚举 str 参数与数值同等对待：值必须字面出现在原文，或由该工具
        规则 slot_map 锚词（扩展→extend 等）映射而来，否则视为模型猜值
        （E4「屏幕显示模式帮我改一下」→ mode=mirror 属无锚点猜值）→ uncertain；
      - strict_str=True（per-tool bound 路径）：开放文本参数（query/pkg/app）
        同样要求字面锚点——bound grammar 下模型只能「复述」原文 token，
        输出原文没有的词就是编造（§9 M1：指代模糊猜 app=comicchat → 校验不过）。
    """
    if not params or tool is None:
        return EvidenceResult(kept=dict(params or {}), uncertain={}, dropped=[])
    ev_text = _normalize_evidence_text(user_input)
    cn_vals = set(evidence_number_candidates(user_input))
    kept: dict[str, Any] = {}
    uncertain: dict[str, Any] = {}
    dropped: list[str] = []
    for k, v in params.items():
        pdef = tool.params.get(k)
        if pdef is None:
            dropped.append(k)  # 未知键：执行层本就会忽略，这里直接丢弃
            continue
        if pdef.type in ("int", "float") and str(v) not in ev_text and v not in cn_vals:
            uncertain[k] = v
        elif pdef.enum and not _enum_or_str_value_evidenced(user_input, tool, k, v):
            uncertain[k] = v
        elif strict_str and pdef.type == "str" and not _enum_or_str_value_evidenced(
            user_input, tool, k, v
        ):
            uncertain[k] = v
        else:
            kept[k] = v
    return EvidenceResult(kept=kept, uncertain=uncertain, dropped=dropped)


def _slot_anchors_for_value(tool, key: str, value: Any) -> list[str]:
    """规则 slot_map 中能映射出该参数值的锚词列表（值由确定性规则赋予）。"""
    if tool is None:
        return []
    rule_cfg = (tool.routes or {}).get("rule") or {}
    sm = rule_cfg.get("slot_map") or {}
    return [
        anchor
        for anchor, mapping in sm.items()
        if mapping.get(key) == value
    ]


def _enum_or_str_value_evidenced(
    user_input: str, tool, key: str, value: Any
) -> bool:
    """
    文本 / 枚举参数值的原文证据：字面出现（归一化后忽略大小写），或该值由
    规则 slot_map 锚词映射而来且锚词在原文中（扩展→extend、调大→80 等）。
    两者都没有 → 模型/规则给出的值在原文无依据，属猜值/误赋，禁止执行。
    """
    ev_text = _normalize_evidence_text(user_input).casefold()
    if str(value).strip().casefold() in ev_text:
        return True
    return any(anchor in user_input for anchor in _slot_anchors_for_value(tool, key, value))


_QUERY_FILLER_RE = re.compile(
    r"(?:请帮我|帮帮我|麻烦帮我|帮我|请给我|给我|麻烦|帮我个忙|请|"
    r"打开|启动|运行|开启|关闭|退出|设置|设置一下|调一下|调|"
    r"搜索|搜一下|搜|查找|找一下|找找|找|查一下|查|看看|查看|列出|"
    r"本地文件|应用|软件|程序|文件|文档|资料|目录|内容|信息|依赖|"
    r"那个|一个|一下|的|了|吧)"
)


def _has_cjk_entity(text: str) -> bool:
    """剥离功能词后是否仍剩 ≥2 个有效中文字符（开放文本参数的值证据）。"""
    leftover = _QUERY_FILLER_RE.sub("", text or "").strip()
    cjk = [ch for ch in leftover if "\u4e00" <= ch <= "\u9fff"]
    return len(cjk) >= 2


def _tool_has_value_evidence(user_input: str, tool) -> bool:
    """
    方案 A 参数证据预检（bound 路径）：原文是否含该工具任一参数的值证据。

      - int/float：中文数字候选或 slot_map 相对方向词（调大/太暗）在原文；
      - 枚举：枚举字面量或 slot_map 锚词在原文；
      - bool：参数自带开/关语义（mute on 等），不强制要求原文显式值；
      - 开放文本（query/pkg/app…）：ASCII token 或剥离功能词后的中文实体
        在原文。
    没有任何参数的值证据 → 真缺参句：0 模型调用，直接缺参澄清（§9：缺参
    句 6/6 走 gate，模型根本不参与，避免 1.5B 猜默认/域内值）。
    """
    if tool is None:
        return False
    ev_text = _normalize_evidence_text(user_input)
    num_cands = set(evidence_number_candidates(user_input))
    rule_cfg = (tool.routes or {}).get("rule") or {}
    sm = rule_cfg.get("slot_map") or {}
    for key, pdef in tool.params.items():
        if pdef.type in ("int", "float"):
            if num_cands or re.search(r"[0-9]+(?:\.[0-9]+)?", user_input):
                return True
            if any(
                key in mapping and anchor in user_input
                for anchor, mapping in sm.items()
            ):
                return True
        elif pdef.enum:
            if any(v.casefold() in ev_text.casefold() for v in pdef.enum):
                return True
            if any(
                anchor in user_input
                and mapping.get(key) in pdef.enum
                for anchor, mapping in sm.items()
            ):
                return True
        elif pdef.type == "bool":
            return True
        else:
            if re.search(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,}", user_input):
                return True
            if _has_cjk_entity(user_input):
                return True
    return False


def _first_fillable_param(tool) -> str | None:
    """缺参澄清优先问必填参数（无默认值），否则问第一个声明参数。"""
    if tool is None or not tool.params:
        return None
    for key, pdef in tool.params.items():
        if pdef.is_required:
            return key
    return next(iter(tool.params))


def _param_validator(pdef) -> str | None:
    """把参数取值约束归一成一条正则，供会话层校验回填值。

    与执行层 _value_allowed 同源（enum ∪ pattern）；两者都没声明时返回 None
    （自由文本参数不做限制）。
    """
    if pdef is None:
        return None
    parts: list[str] = []
    if pdef.enum:
        parts.append("|".join(re.escape(str(v)) for v in pdef.enum))
    if pdef.pattern:
        parts.append(pdef.pattern)
    if not parts:
        return None
    return "(?:" + "|".join(parts) + ")"


class Router:
    """
    三段式路由器。
    cloud_client: 第三段客户端，None 时离线降级（返回 ESCALATE 结果而不崩溃）。
    """

    RULE_CONFIDENCE_THRESHOLD = 0.7

    def __init__(
        self,
        registry: ToolRegistry,
        rule_engine: RuleEngine,
        local_client: LocalModelClient,
        executor: Executor,
        kb: BM25KnowledgeBase | None = None,
        cloud_client: Any | None = None,
        embed: Any | None = None,
        stats: HallucinationStats | None = None,
        pkg_resolver: Callable[[str], str | None] | None = None,
        soft_gate_config: SoftGateConfig | None = None,
        complexity_thresholds: ComplexityThresholds | None = None,
    ):
        self._registry = registry
        self._rule = rule_engine
        self._local = local_client
        self._executor = executor
        self._kb = kb
        self._cloud = cloud_client
        self._embed = embed
        self._stats = stats if stats is not None else HallucinationStats()
        self._pkg_resolver = pkg_resolver
        self._complexity_thresholds = complexity_thresholds or ComplexityThresholds.from_environment()
        self._soft_gate = soft_gate_config if soft_gate_config is not None else SoftGateConfig()
        # bibit-filesearch 服务端口：环境变量优先，否则按 uid 派生（对齐服务端 _port_for_uid）
        import os

        env_port = os.environ.get("BIBIT_FILESEARCH_PORT")
        self._filesearch_port = (
            int(env_port)
            if env_port
            else (18700 if os.getuid() < 1000 else 18700 + (os.getuid() - 1000))
        )
        # 执行层共享同一统计器：越界钳制/枚举拒绝也计入幻觉报告
        executor.attach_stats(self._stats)
        rule_engine.attach_stats(self._stats)

    @property
    def stats(self) -> HallucinationStats:
        """幻觉检测统计（P0：统一检测结果模型，见 agent/hallucination.py）。"""
        return self._stats

    def _record_evidence(
        self, ev: EvidenceResult, tool_name: str, user_input: str
    ) -> None:
        """把参数证据闸门结果写入统计（P0 埋点，不改变决策逻辑）。"""
        td = self._registry.get(tool_name)
        num_types = ("int", "float")
        for k, v in ev.uncertain.items():
            self._stats.record(HallucinationRecord(
                kind=DetectionKind.PARAMETER,
                disposition=Disposition.CONFIRMED,
                tool=tool_name, input=user_input, param=k, value=v,
                detail="no_literal_evidence",
            ))
        for k in ev.dropped:
            self._stats.record(HallucinationRecord(
                kind=DetectionKind.PARAMETER,
                disposition=Disposition.DROPPED,
                tool=tool_name, input=user_input, param=k,
                detail="undefined_param",
            ))
        if td is not None:
            for k, v in ev.kept.items():
                pdef = td.params.get(k)
                if pdef is not None and pdef.type in num_types:
                    self._stats.record(HallucinationRecord(
                        kind=DetectionKind.PARAMETER,
                        disposition=Disposition.EXECUTED,
                        tool=tool_name, input=user_input, param=k, value=v,
                        detail="literal_or_rule_evidence",
                    ))

    # ── 主入口 ────────────────────────────────────────────────────────

    def handle(self, user_input: str, session=None) -> RouteResult:
        t0 = time.monotonic()
        if session is not None and session.pending is not None:
            if session.pending.tool_name in _APP_TARGET_TOOLS and not session.pending.confirm:
                session.clear_pending()
                if not re.search(r"打开|启动|关闭|退出|查询|检查|安装|卸载|\b(?:open|launch|close|quit)\b", user_input, re.I):
                    self._stats.total_requests += 1
                    return self._cloud_fallback(user_input, "missing_app_target", t0)
        result = self._handle(user_input, session)
        if (
            result.tool_name in _APP_TARGET_TOOLS
            and result.exec_result is not None
            and result.exec_result.need_param
        ):
            if session is not None and session.pending is not None:
                if session.pending.tool_name == result.tool_name and not session.pending.confirm:
                    session.clear_pending()
            return self._cloud_fallback(
                user_input, "missing_app_target", t0,
                debug={"local_tool": result.tool_name, "need_param": result.exec_result.need_param},
            )
        return result

    def _handle(self, user_input: str, session=None) -> RouteResult:
        """
        主入口。
        session: 可选的会话状态（agent.session.SessionState）。传入后支持
        缺参多轮回填：上一轮工具缺必填参数时挂起 pending，本轮直接补值
        执行；不传则保持无状态（单次命令模式）。
        """
        import time
        t0 = time.monotonic()
        self._stats.total_requests += 1
        analysis = default_analyzer().analyze(user_input)

        # ── 0. 会话级缺参回填：上一轮缺必填参数，本轮先尝试补值 ──
        if session is not None and session.pending is not None:
            complexity = classify_complexity(user_input, analysis=analysis, thresholds=self._complexity_thresholds)
            if complexity.complexity is Complexity.COMPLEX and complexity.escalation_reason != "insufficient_evidence":
                session.clear_pending()
                return self._cloud_fallback(user_input, complexity.escalation_reason, t0, debug={
                    "complexity": {"type": complexity.complexity.value, "score": complexity.score,
                                   "features": dict(complexity.features), "reasons": list(complexity.reasons)},
                    "pending_cancelled": True,
                })
            if session.pending.confirm:
                # 模型推测值确认挂起：确认/给新值/取消/换话题
                r = self._run_confirm(session, user_input, t0, analysis)
                if r is not None:
                    return r
            else:
                action, value = session.resolve(
                    user_input, self._rule, analysis=analysis
                )
                if action == "ambiguous":
                    return self._cloud_fallback(user_input, "ambiguous_slot_reply", t0)
                if action == "fill":
                    return self._run_slot_fill(session, value, t0)
                if action == "keep":
                    p = session.pending
                    # 优先复用挂起时执行层给的提示（含可选取值），比重新拼一句
                    # 泛化的"还差一个参数"更具体
                    msg = p.message or (
                        f"还差一个参数：{p.param_name}（{p.param_label}）。"
                        f"请直接回复{p.param_label}，或按「{p.param_name}=值」格式补充。"
                    )
                    return RouteResult(
                        stage=Stage.SLOT_FILL,
                        tool_name=p.tool_name,
                        params={},
                        exec_result=ExecResult(
                            False,
                            msg,
                            need_param=p.param_name,
                        ),
                        latency_ms=(time.monotonic() - t0) * 1000,
                        debug={"slot_wait_turn": p.turn},
                    )
                # action == "new"：pending 已清空，走正常路由

        # ── 进模型前的确定性意图决策（规则命中 / 开放问答 / 进模型）──────
        # 这段逻辑抽到 agent.intent，router 与测试共用，避免重复。
        plan = decide_local_intent(
            user_input,
            self._rule,
            self._registry,
            embed=self._embed,
            pkg_resolver=self._pkg_resolver,
            complexity_thresholds=self._complexity_thresholds,
        )

        # ① 第一段：规则引擎高置信命中 → 直接执行
        if plan.action is IntentAction.RULE_CALL:
            if plan.tool_name == "__read_bundle__":
                result = self._executor.execute_read_bundle(
                    list(plan.slots.get("tools") or []),
                    language=detect_language(user_input),
                )
                return RouteResult(
                    stage=Stage.RULE,
                    tool_name="__read_bundle__",
                    params=plan.slots,
                    exec_result=result,
                    latency_ms=(time.monotonic() - t0) * 1000,
                    debug={"pattern": plan.matched_pattern},
                )
            # 方案 A 规则层防御：枚举槽位若在原文找不到锚点（字面值或 slot_map
            # 锚词），视为规则误赋默认值（如"显示模式帮我改一下"被挂成 extend），
            # 删掉该槽 → 执行层报缺必填参数澄清，不盲执行默认枚举。
            slots = self._rule_evidenced_slots(user_input, plan.tool_name, plan.slots)
            tool_name = plan.tool_name
            multi_debug: dict[str, object] = {}
            # "打开终端和浏览器"这类多应用串：规则层直接拆成 launch_apps，
            # 不再整串交给单应用工具后 app_not_found 上云（云端只会复述成功）。
            multi = _multi_app_route(user_input, tool_name)
            if multi is not None:
                tool_name, app_names = multi
                slots = {"app_names": app_names}
                multi_debug = {"multi_app_split": app_names}
            result = self._executor.execute(tool_name, slots, language=detect_language(user_input))
            if getattr(result, "escalate", None):
                return self._cloud_fallback(
                    user_input,
                    result.escalate,
                    t0,
                    debug={
                        "pattern": plan.matched_pattern,
                        "local_tool": tool_name,
                        **multi_debug,
                    },
                )
            # file_search 走独立服务转发（触发词直通），不走 executor
            rule_tool = self._registry.get(tool_name)
            if rule_tool and rule_tool.system.get("type") == "filesearch":
                result = self._local_file_search(
                    str(slots.get("query") or user_input).strip()
                )
            # 非 filesearch 工具直接复用上面的执行结果；
            # 重复执行会让有副作用的工具（如 close_app）第二次误报"未找到"。
            if result.need_param:
                self._suspend(session, tool_name, result, slots)
            return RouteResult(
                stage=Stage.RULE,
                tool_name=tool_name,
                params=slots,
                exec_result=result,
                latency_ms=(time.monotonic() - t0) * 1000,
                debug={"pattern": plan.matched_pattern, **multi_debug},
            )

        # ② 开放问答前置判断 → 直接升级到第三段
        if plan.action is IntentAction.ESCALATE:
            debug = {"intent_slots": plan.slots} if plan.slots else {}
            if plan.analysis is not None and "complexity" in plan.analysis.debug:
                debug["complexity"] = plan.analysis.debug["complexity"]
            return self._cloud_fallback(user_input, plan.reason or "escalated", t0, debug=debug)

        # PreModelGate：只使用 embedding/候选信号，可真正避免无意义模型调用。
        if self._soft_gate.mode != "off" and pre_model_escalate(plan, self._soft_gate):
            if self._soft_gate.mode == "on":
                self._stats.soft_gate_pre_on += 1
                return self._cloud_fallback(
                    user_input, "soft_gate_oos", t0,
                    debug={"soft_gate": "pre_model"},
                )
            # shadow：记录继续走原路径，不改变现有行为。
            self._stats.soft_gate_pre_shadow += 1

        # ── 第二段：端侧模型 ──────────────────────────────────────────
        if not self._local.is_available():
            # llama-server 不可用 → 直接升级
            return self._cloud_fallback(user_input, "local_unavailable", t0)

        candidates = plan.candidates
        tool_list = plan.tool_list

        # ── launch/close 窄域 → 标签层协议（L3）─────────────────────
        # 1.5B 在开放参数上会猜值/编造（param_hallucination_experiment.md），
        # 因此凡候选收窄到 launch/close 的指令不进 CALL 参数协议：模型只判
        # 方向，应用名走 _deterministic_app_params 原文确定性抽取。英文直接
        # 指令由规则层覆盖，不进标签层。
        if (
            candidates
            and all(c in _APP_CONTROL_LABEL_TOOLS for c in candidates)
            and getattr(self._local, "infer_label", None) is not None
            and detect_language(user_input) != "en"
        ):
            return self._route_app_control_label(user_input, plan, session, t0)

        # ── 方向已锁定（唯一候选 / agreed 双通道）→ per-tool bound grammar ──
        # 方案 A 生产化（glm_practice_experiment.md §9-10，§8.3.4）：方向由
        # 确定性通道拍板后，模型只负责把原文明确出现的值翻译成 CALL <tool>
        # <key>=<value>，工具名/键/值域被 grammar 锁死——同域错选（vol↔bright）
        # 与跨工具垃圾键（orlaunch/oracle）在解码侧没有落点。真缺参句由
        # _route_bound_locked 的证据预检直接澄清（0 模型调用），不让 1.5B
        # 猜默认/域内值。launch/close 窄域继续走上面的标签层分支。
        locked_tool: str | None = None
        if plan.hint and plan.hint_source == "agreed":
            locked_tool = plan.hint
        elif len(candidates) == 1:
            locked_tool = candidates[0]
        if (
            locked_tool
            and locked_tool not in _APP_CONTROL_LABEL_TOOLS
            and getattr(self._local, "infer_bound", None) is not None
        ):
            r = self._route_bound_locked(user_input, locked_tool, plan, session, t0)
            if r is not None:
                return r

        # 若涉及知识库查询，注入相关知识片段
        kb_context = ""
        if self._kb and candidates and "query_knowledge" in candidates:
            kb_context = self._kb.context_snippet(user_input)

        full_input = user_input
        if kb_context:
            full_input = f"[背景知识]\n{kb_context}\n\n[用户问题]\n{user_input}"

        try:
            local_r = self._local.infer(
                full_input,
                tool_list,
                analysis=plan.analysis,
                n_probs=self._soft_gate.n_probs,
                tool_names=self._registry.local_tool_names(candidates),
            )
        except TypeError as exc:
            if "n_probs" not in str(exc):
                raise
            local_r = self._local.infer(
                full_input,
                tool_list,
                analysis=plan.analysis,
                tool_names=self._registry.local_tool_names(candidates),
            )
        self._stats.model_requests += 1

        if local_r.is_call:
            # PostModelGate：低 logprob 的 CALL 在证据闸门之前提前升级。
            if self._soft_gate.post_mode != "off" and post_model_escalate(
                local_r, self._soft_gate
            ):
                if self._soft_gate.post_mode == "on":
                    self._stats.soft_gate_post_on += 1
                    self._stats.record(HallucinationRecord(
                        kind=DetectionKind.TOOL_NAME,
                        disposition=Disposition.ESCALATED,
                        tool=local_r.tool_name,
                        input=user_input,
                        detail="soft_gate_oos",
                    ))
                    return self._cloud_fallback(
                        user_input, "soft_gate_oos", t0,
                        debug={"local_raw": local_r.raw,
                               "avg_logprob": local_r.avg_logprob},
                    )
                # shadow：继续走原路径。
                self._stats.soft_gate_post_shadow += 1

            self._stats.model_calls += 1
            # 无任何确定性支撑（无关键词候选、无 embedding 强提示）时，
            # 模型 CALL 视为幻觉：不执行、直接升级。此时 tool_list 为空，
            # 模型输出 CALL 必然无依据（如"今天天气怎么样"→vol），
            # 防止别名/模糊解析把它当合法调用执行。
            if not plan.hint and not plan.candidates:
                self._stats.record(HallucinationRecord(
                    kind=DetectionKind.TOOL_NAME,
                    disposition=Disposition.ESCALATED,
                    tool=local_r.tool_name,
                    input=user_input,
                    detail="no_tool_support",
                ))
                return self._cloud_fallback(
                    user_input, "no_tool_support", t0,
                    debug={"local_raw": local_r.raw,
                           "hallucinated_tool": local_r.tool_name},
                )
            # 模型 CALL 了一个端侧不存在的工具（如幻觉出的 write_poem）
            # → 先按 embedding 强提示 / 唯一候选容错兜底（端侧 1B 常把
            #   口语里的软件名当工具名，如 "CALL vim"）；兜底不了再升级。
            fallbacks = (
                [plan.hint] if plan.hint
                else plan.candidates if len(plan.candidates) == 1
                else []
            )
            # 强提示与模型 CALL 冲突 → 动作域歧义：不执行、直接升级。
            # hint 是 embedding（±关键词双通道一致）高置信给出的方向，模型却
            # 指向另一个**能解析出来**的工具，说明两边在各自猜域，谁也压不过
            # 谁；执行任何一侧都可能误调用。实测：「连接LOGI M240 B」hint=
            # bt_connect，1.5B 输出 CALL wconn（wifi_connect），旧逻辑采纳了
            # 模型的选择并回复「请告诉我要连接的 WiFi 名称和密码」。
            # 这种情况交云端用真实工具消歧（扫描 + 判别性反问）比本地赌一侧
            # 可靠，调研见 docs/intent-ambiguity-survey.md §2.2 / §4-1。
            # 注意：模型 CALL 了端侧不存在的名字（如 "CALL vim"）**不算**冲突，
            # 那是名字解析问题，继续走下面的 fallbacks 兜底到 hint。
            model_tool = (
                self._registry.fuzzy_resolve(local_r.tool_name)
                if local_r.tool_name else None
            )
            if plan.hint and model_tool and model_tool != plan.hint:
                self._stats.record(HallucinationRecord(
                    kind=DetectionKind.TOOL_NAME,
                    disposition=Disposition.ESCALATED,
                    tool=model_tool,
                    input=user_input,
                    detail="hint_conflict",
                ))
                return self._cloud_fallback(
                    user_input, "ambiguous_tool", t0,
                    debug={"local_raw": local_r.raw,
                           "hint": plan.hint,
                           "hint_source": plan.hint_source,
                           "model_tool": model_tool},
                )
            tool = self._registry.resolve_with_fallback(
                local_r.tool_name, fallbacks
            )
            if tool is None:
                self._stats.record(HallucinationRecord(
                    kind=DetectionKind.TOOL_NAME,
                    disposition=Disposition.ESCALATED,
                    tool=local_r.tool_name,
                    input=user_input,
                    detail="unknown_tool",
                ))
                return self._cloud_fallback(
                    user_input, "unknown_tool", t0,
                    debug={"local_raw": local_r.raw,
                           "hallucinated_tool": local_r.tool_name},
                )
            raw_params = local_r.params or {}
            params = dict(raw_params)
            if tool != local_r.tool_name:
                self._stats.record(HallucinationRecord(
                    kind=DetectionKind.TOOL_NAME,
                    disposition=Disposition.FALLBACK,
                    tool=tool,
                    input=user_input,
                    detail=f"resolve_from_{local_r.tool_name}",
                ))
                # 兜底命中：模型参数是幻觉，只保留目标工具声明的参数
                td = self._registry.get(tool)
                if td:
                    params = {
                        k: v for k, v in params.items() if k in td.params
                    }
            ev = _keep_evidenced_params(
                user_input, params, self._registry.get(tool)
            )
            self._record_evidence(ev, tool, user_input)
            if ev.uncertain:
                return self._suspend_confirm(session, tool, ev, t0)
            params = ev.kept
            # 模型给了参数但全部是幻觉（未定义键/兜底丢弃）→ 不允许拿
            # 空参或默认值静默执行：先从原文确定性回抽，抽不到就升级云端。
            if raw_params and not params:
                recovered = self._deterministic_params(user_input, tool)
                if recovered:
                    params = recovered
                else:
                    self._stats.record(HallucinationRecord(
                        kind=DetectionKind.PARAMETER,
                        disposition=Disposition.ESCALATED,
                        tool=tool,
                        input=user_input,
                        detail="all_model_params_dropped",
                    ))
                    return self._cloud_fallback(
                        user_input, "hallucinated_params", t0,
                        debug={"local_raw": local_r.raw,
                               "hallucinated_params": raw_params},
                    )
            result = self._execute_local_tool(tool, params, user_input)
            if result is None:
                return self._cloud_fallback(user_input, "knowledge_miss", t0)
            if getattr(result, "escalate", None):
                return self._cloud_fallback(
                    user_input,
                    result.escalate,
                    t0,
                    debug={
                        "raw": local_r.raw,
                        "model_latency_ms": local_r.latency_ms,
                        "fallback": tool != local_r.tool_name,
                        "local_tool": tool,
                    },
                )
            if result.need_param:
                self._suspend(session, tool, result, params)
            return RouteResult(
                stage=Stage.LOCAL,
                tool_name=tool,
                params=params,
                exec_result=result,
                latency_ms=(time.monotonic() - t0) * 1000,
                debug={"raw": local_r.raw, "model_latency_ms": local_r.latency_ms,
                       "fallback": tool != local_r.tool_name},
            )

        # 第二段判断需要升级；但确定性通道高置信时容错兜底：
        #   ① embedding+关键词双通道一致（agreed 强提示，实测 8/8 正确）
        #   ② 候选唯一（无二义）
        # 即「先 embedding 语义路由拍板工具，小模型只负责翻译参数，
        # 翻车时由确定性通道兜底」，而不是无谓升级云端。
        single = len(plan.candidates) == 1
        if (plan.hint and plan.hint_source == "agreed") or single:
            if local_r.reason != "local_server_unavailable":
                tool = plan.hint or plan.candidates[0]
                td = self._registry.get(tool)
                params: dict[str, Any] = {}
                if td:
                    params = {
                        k: v for k, v in (local_r.params or {}).items()
                        if k in td.params
                    }
                    if not params:
                        m = self._rule.match_tool(user_input, tool)
                        if m:
                            params = m.slots
                ev = _keep_evidenced_params(user_input, params, td)
                self._record_evidence(ev, tool, user_input)
                if ev.uncertain:
                    return self._suspend_confirm(session, tool, ev, t0)
                params = ev.kept
                result = self._execute_local_tool(tool, params, user_input)
                if result is None:
                    return self._cloud_fallback(user_input, "knowledge_miss", t0)
                if getattr(result, "escalate", None):
                    return self._cloud_fallback(
                        user_input,
                        result.escalate,
                        t0,
                        debug={
                            "raw": local_r.raw,
                            "model_latency_ms": local_r.latency_ms,
                            "override": "escalate_hint",
                            "local_tool": tool,
                        },
                    )
                if result.need_param:
                    self._suspend(session, tool, result, params)
                return RouteResult(
                    stage=Stage.LOCAL,
                    tool_name=tool,
                    params=params,
                    exec_result=result,
                    latency_ms=(time.monotonic() - t0) * 1000,
                    debug={"raw": local_r.raw,
                           "model_latency_ms": local_r.latency_ms,
                           "override": "escalate_hint"},
                )

        if local_r.reason == "parse_failed":
            self._stats.record(HallucinationRecord(
                kind=DetectionKind.TOOL_NAME,
                disposition=Disposition.ESCALATED,
                tool=None,
                input=user_input,
                detail="parse_failed",
            ))
        return self._cloud_fallback(
            user_input, local_r.reason or "escalated", t0,
            debug={"local_raw": local_r.raw}
        )

    def _deterministic_app_params(
        self, user_input: str, tool_name: str
    ) -> dict[str, Any]:
        """标签层（launch/close）参数回抽：只回抽 app_name，其余不猜。"""
        name = _deterministic_app_name(user_input, tool_name)
        if not name:
            return {}
        return {"app_name": name}

    def _route_app_control_label(
        self, user_input: str, plan: IntentPlan, session, t0
    ) -> RouteResult:
        """
        launch/close 窄域标签层路径（L3 实验结论落地）。

        输出协议：CALL launch|close（无参数）或 ESCALATE reason=out_of_scope|
        missing_param，grammar 闭合枚举。方向确定后应用名一律由
        _deterministic_app_params 抽取，交给执行层别名/.desktop 解析：
          抽到 → 执行（别名/桌面命中）或升云（未找到+近似候选）；
          抽不到（缺参/占位/多应用）→ 执行层缺参澄清或升级。
        """
        import time
        label_r = self._local.infer_label(user_input)
        self._stats.model_requests += 1

        if label_r.is_call:
            self._stats.model_calls += 1
            tool = _APP_LABEL_DIRECTION.get(label_r.tool_name or "")
            if tool is None:
                # 闭合 grammar 外不应出现；防御性升级
                self._stats.record(HallucinationRecord(
                    kind=DetectionKind.TOOL_NAME,
                    disposition=Disposition.ESCALATED,
                    tool=label_r.tool_name,
                    input=user_input,
                    detail="label_unknown_direction",
                ))
                return self._cloud_fallback(
                    user_input, "unknown_tool", t0,
                    debug={"label_raw": label_r.raw},
                )
        else:
            # ESCALATE：方向未定。与旧 CALL 协议一致，仅在 embedding+关键词
            # 双通道一致（agreed）或唯一候选时做确定性兜底，避免模型一次误
            # 升级把强证据指令送云端。
            if (plan.hint and plan.hint_source == "agreed") or len(plan.candidates) == 1:
                tool = plan.hint or plan.candidates[0]
            else:
                if label_r.reason == "parse_failed":
                    self._stats.record(HallucinationRecord(
                        kind=DetectionKind.TOOL_NAME,
                        disposition=Disposition.ESCALATED,
                        tool=None,
                        input=user_input,
                        detail="parse_failed",
                    ))
                return self._cloud_fallback(
                    user_input, label_r.reason or "escalated", t0,
                    debug={"label_raw": label_r.raw,
                           "model_latency_ms": label_r.latency_ms},
                )

        params = self._deterministic_app_params(user_input, tool)
        if not params:
            return self._cloud_fallback(user_input, "app_target_unresolved", t0)
        result = self._execute_local_tool(tool, params, user_input)
        if result is None:
            return self._cloud_fallback(user_input, "knowledge_miss", t0)
        if getattr(result, "escalate", None):
            return self._cloud_fallback(
                user_input,
                result.escalate,
                t0,
                debug={"label_raw": label_r.raw,
                       "model_latency_ms": label_r.latency_ms,
                       "local_tool": tool,
                       "params": params},
            )
        if result.need_param:
            self._suspend(session, tool, result, params)
        return RouteResult(
            stage=Stage.LOCAL,
            tool_name=tool,
            params=params,
            exec_result=result,
            latency_ms=(time.monotonic() - t0) * 1000,
            debug={"label_raw": label_r.raw,
                   "model_latency_ms": label_r.latency_ms,
                   "label_mode": True},
        )

    def _rule_evidenced_slots(
        self,
        user_input: str,
        tool_name: str,
        slots: dict[str, Any],
    ) -> dict[str, Any]:
        """RULE_CALL 前清理无原文锚点的枚举槽位（方案 A 规则层防御）。"""
        tool = self._registry.get(tool_name)
        if tool is None or not slots:
            return dict(slots)
        out = dict(slots)
        for key, value in list(out.items()):
            pdef = tool.params.get(key)
            if pdef is not None and pdef.enum and not _enum_or_str_value_evidenced(
                user_input, tool, key, value
            ):
                self._stats.record(HallucinationRecord(
                    kind=DetectionKind.PARAMETER,
                    disposition=Disposition.DROPPED,
                    tool=tool_name, input=user_input, param=key, value=value,
                    detail="rule_enum_without_anchor",
                ))
                del out[key]
        return out

    # ── 方案 A：方向已锁定 → per-tool bound grammar ──────────────────
    def _route_bound_locked(
        self,
        user_input: str,
        tool_name: str,
        plan: IntentPlan,
        session,
        t0: float,
    ) -> RouteResult | None:
        """
        方案 A 生产路径（glm_practice_experiment.md §9-10）：方向由确定性通道
        （规则 / 唯一候选 / agreed 双通道）锁定后，模型只负责把原文明确出现的
        值翻译成 CALL <tool> <key>=<value>（grammar per-tool 绑定）。

        顺序：
          1. 规则确定性抽取（slot_map / 正则命名组，值自带原文锚点）→ 直接执行；
          2. 值证据预检：原文无任何参数值证据（真缺参句）→ 0 模型调用，
             直接缺参澄清（§9：缺参句 6/6 走 gate，模型不参与）；
          3. 有证据 → bound grammar 单次生成 → 值过证据闸门（数值/枚举/开放
             文本都要回原文有依据）→ 执行 / 确认 / 澄清。不二次重试（§9.3.3：
             重试只会再猜一次）。
        """
        tool = self._registry.get(tool_name)
        if tool is None:
            return None
        # 无参数工具：方向已锁定且没有值可补，直接执行（与 RULE_CALL 语义一致）
        if not tool.params:
            return self._finish_bound_locked(
                user_input, tool_name, {}, session, t0
            )

        # 1) 确定性参数（规则 slot_map / 正则命名组）优先，不调用模型
        m = self._rule.match_tool(user_input, tool_name)
        if m is not None and m.slots:
            return self._finish_bound_locked(
                user_input, tool_name, m.slots, session, t0,
                deterministic=True,
            )

        # 2) 真缺参预检：没有任何值证据就不请模型猜（§8/§9：1.5B 缺参必猜）
        if not _tool_has_value_evidence(user_input, tool):
            return self._bound_missing_param(
                user_input, tool_name, session, t0,
                debug={"bound": "no_value_evidence"},
            )

        # 3) bound grammar 单次生成
        param_spec = {
            key: {
                "type": pdef.type,
                "enum": pdef.enum,
                "range": pdef.range,
            }
            for key, pdef in tool.params.items()
        }
        tool_list = self._registry.local_tool_list([tool_name])
        bound_r = self._local.infer_bound(
            user_input,
            tool_name,
            tool_list=tool_list,
            param_spec=param_spec,
            analysis=plan.analysis,
        )
        self._stats.model_requests += 1

        if not bound_r.is_call:
            if bound_r.reason == "local_server_unavailable":
                return self._cloud_fallback(user_input, "local_unavailable", t0)
            if bound_r.reason == "context_overflow":
                return self._cloud_fallback(user_input, "context_overflow", t0)
            # 模型 ESCALATE：方向已锁定但模型认为值缺失/域外 → 规则确定性回抽
            m = self._rule.match_tool(user_input, tool_name)
            if m is not None and m.slots:
                return self._finish_bound_locked(
                    user_input, tool_name, m.slots, session, t0,
                    deterministic=True,
                )
            if bound_r.reason == "out_of_scope":
                return self._cloud_fallback(
                    user_input, "out_of_scope", t0,
                    debug={"bound_raw": bound_r.raw},
                )
            # 证据预检已确认值在场，模型仍升级 → 模型漏值（§9 值在场 100% 命中，
            # 属稀有失败）。不二次重试也不重复追问（用户已给出值），直接升云。
            return self._cloud_fallback(
                user_input, bound_r.reason or "escalated", t0,
                debug={"bound": "model_escalate_with_evidence",
                       "bound_raw": bound_r.raw},
            )

        # CALL：参数过证据闸门（strict_str：开放文本同样要求原文字面锚点）
        self._stats.model_calls += 1
        td = self._registry.get(tool_name)
        params = {
            key: value
            for key, value in (bound_r.params or {}).items()
            if td is not None and key in td.params
        }
        if not params:
            # 语法允许省略参数，但模型没给任何值 → 规则回抽，回抽不到澄清
            m = self._rule.match_tool(user_input, tool_name)
            if m is not None and m.slots:
                return self._finish_bound_locked(
                    user_input, tool_name, m.slots, session, t0,
                    deterministic=True,
                )
            return self._cloud_fallback(
                user_input, "missing_param", t0,
                debug={"bound": "model_call_without_value",
                       "bound_raw": bound_r.raw},
            )
        ev = _keep_evidenced_params(user_input, params, td, strict_str=True)
        self._record_evidence(ev, tool_name, user_input)
        if ev.uncertain:
            # 无锚点猜值：开放式文本猜值属编造（§9 M1 → 升云），封闭数值/枚举
            # 走会话确认（给用户纠正机会），都不盲执行。
            uncertain_types = {
                key: td.params.get(key).type
                for key in ev.uncertain
                if td is not None and td.params.get(key) is not None
            }
            if any(t == "str" for t in uncertain_types.values()):
                self._stats.record(HallucinationRecord(
                    kind=DetectionKind.PARAMETER,
                    disposition=Disposition.ESCALATED,
                    tool=tool_name,
                    input=user_input,
                    detail="bound_open_text_guess",
                ))
                return self._cloud_fallback(
                    user_input, "hallucinated_params", t0,
                    debug={"bound_raw": bound_r.raw,
                           "bound_params": ev.uncertain},
                )
            return self._suspend_confirm(session, tool_name, ev, t0)
        params = ev.kept
        return self._finish_bound_locked(
            user_input, tool_name, params, session, t0,
            bound_r=bound_r,
        )

    def _finish_bound_locked(
        self,
        user_input: str,
        tool_name: str,
        params: dict[str, Any],
        session,
        t0: float,
        deterministic: bool = False,
        bound_r=None,
    ) -> RouteResult:
        """bound 路径收尾：执行工具并返回 RouteResult（与标签层同构）。"""
        result = self._execute_local_tool(tool_name, params, user_input)
        if result is None:
            return self._cloud_fallback(user_input, "knowledge_miss", t0)
        if getattr(result, "escalate", None):
            return self._cloud_fallback(
                user_input,
                result.escalate,
                t0,
                debug={
                    "raw": getattr(bound_r, "raw", ""),
                    "model_latency_ms": getattr(bound_r, "latency_ms", 0),
                    "local_tool": tool_name,
                    "params": params,
                    "bound": True,
                },
            )
        if result.need_param:
            self._suspend(session, tool_name, result, params)
        debug = {
            "bound": True,
            "bound_deterministic": deterministic,
            "raw": getattr(bound_r, "raw", ""),
            "model_latency_ms": getattr(bound_r, "latency_ms", 0),
        }
        return RouteResult(
            stage=Stage.LOCAL,
            tool_name=tool_name,
            params=params,
            exec_result=result,
            latency_ms=(time.monotonic() - t0) * 1000,
            debug=debug,
        )

    def _bound_missing_param(
        self,
        user_input: str,
        tool_name: str,
        session,
        t0: float,
        debug: dict | None = None,
    ) -> RouteResult:
        """
        方案 A 缺参澄清出口：真缺参句不执行、不拿默认值静默执行、不确认猜值，
        直接问用户参数值（多轮回填由 session pending 承载）。
        """
        tool = self._registry.get(tool_name)
        param = _first_fillable_param(tool)
        label = self._executor.param_label(tool_name, param or "")
        sample = self._executor.param_sample(tool_name, param or "")
        hint = f"（{sample}）" if sample else ""
        msg = (
            f"还差一个参数：{param}（{label}）。{hint}"
            f"请直接回复{label}，或按「{param}=值」格式补充。"
        )
        result = ExecResult(False, msg, need_param=param)
        if session is not None:
            self._suspend(session, tool_name, result, {})
        d = dict(debug or {})
        d["bound"] = d.get("bound", "missing_param")
        d["need_param"] = param
        return RouteResult(
            stage=Stage.LOCAL,
            tool_name=tool_name,
            params={},
            exec_result=result,
            latency_ms=(time.monotonic() - t0) * 1000,
            debug=d,
        )

    def _deterministic_params(self, user_input: str, tool_name: str) -> dict[str, Any]:
        """模型参数全幻觉时，从原文确定性回抽参数。

        顺序：先按该工具规则模式补抽；抽不到且目标是 launch_app 时，
        用动词剥离兜底取 app_name（中文应用名模型无法表达）。
        """
        m = self._rule.match_tool(user_input, tool_name)
        if m and m.slots:
            return m.slots
        if tool_name == "launch_app":
            name = _extract_launch_app_name(user_input)
            if name:
                return {"app_name": name}
        return {}

    def _execute_local_tool(
        self,
        tool_name: str,
        params: dict[str, Any],
        user_input: str,
    ) -> ExecResult | None:
        from bibit_intent.core.command_contracts import (
            command_contract_allows,
            side_effect_contract_allows,
        )

        if not command_contract_allows(tool_name, user_input):
            return ExecResult(False, "请求不符合本地操作契约", escalate="unverified_command")
        if not side_effect_contract_allows(tool_name, user_input):
            return ExecResult(False, "请求未明确要求执行该设备操作", escalate="unverified_command")
        tool = self._registry.get(tool_name)
        if tool and tool.system.get("type") == "knowledge_base":
            if self._kb is None:
                return None
            query = str(params.get("query") or user_input).strip()
            snippet = self._kb.context_snippet(query)
            if not snippet:
                return None
            return ExecResult(True, snippet)
        if tool and tool.system.get("type") == "filesearch":
            return self._local_file_search(
                str(params.get("query") or user_input).strip()
            )
        return self._executor.execute(tool_name, params, language=detect_language(user_input))

    def _local_file_search(self, query: str) -> ExecResult | None:
        """本地文件检索：转发到 bibit-filesearch 服务（:18700 段），返回文件列表。"""
        if not query:
            return ExecResult(False, "本地搜索需要查询内容。")
        try:
            import httpx

            resp = httpx.post(
                f"http://127.0.0.1:{self._filesearch_port}/api/search",
                json={"query": query, "page": 1, "pageSize": 10},
                timeout=15.0,
            )
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:  # noqa: BLE001 - 服务不可用降级
            return ExecResult(
                False,
                f"本地文件检索服务不可用（{type(exc).__name__}），"
                "请确认 bibit-filesearch 已启动。",
            )
        data = body.get("data") or {}
        results = data.get("results") or []
        if not results:
            return ExecResult(False, "本地没有找到相关文件。")
        lines = ["本地检索结果："]
        for i, item in enumerate(results[:10], 1):
            conf = round(float(item.get("confidence") or 0) * 100)
            lines.append(
                f"{i}. {item.get('fileName') or ''}（{conf}%）"
                f" — {item.get('filePath') or ''}"
            )
        # 直通回答只给文件名（filesearch 只回元数据）。明确告诉用户/模型下一步
        # 怎么拿正文——否则「本地资料」就停在文件名上（K13）。
        lines.append(
            "如需在桌面打开该文件，请调用 open_file(path=<文件路径>)，"
            "不要用 terminal 直接运行 libreoffice/xdg-open。"
        )
        lines.append(
            "需要正文时：说「读取本地文件 <路径>」（只读索引目录里的文本文件，"
            "docx/pdf 暂时只能给文件名）。"
        )
        return ExecResult(True, "\n".join(lines))

    # ── 会话级缺参回填 ──────────────────────────────────────────────

    def _run_slot_fill(self, session, value, t0) -> RouteResult:
        """回填挂起槽位并执行。仍缺参（多参数工具）则重新挂起逐轮补齐。"""
        import time
        p = session.pending
        filled = dict(p.filled)
        filled[p.param_name] = value
        result = self._executor.execute(p.tool_name, filled)
        if result.need_param:
            # 多参数工具：补一个后仍缺 → 重新挂起，下一轮继续补
            self._suspend(session, p.tool_name, result, filled)
            return RouteResult(
                stage=Stage.SLOT_FILL,
                tool_name=p.tool_name,
                params=filled,
                exec_result=result,
                latency_ms=(time.monotonic() - t0) * 1000,
                debug={"slot_fill": p.param_name, "still_missing": result.need_param},
            )
        session.clear_pending()
        return RouteResult(
            stage=Stage.SLOT_FILL,
            tool_name=p.tool_name,
            params=filled,
            exec_result=result,
            latency_ms=(time.monotonic() - t0) * 1000,
            debug={"slot_fill": p.param_name},
        )

    def _suspend(self, session, tool_name: str, result, filled: dict) -> None:
        """执行层缺参 → 挂起会话级 pending，等下一轮补值。session 为 None 时忽略。"""
        if session is None:
            return
        tool = self._registry.get(tool_name)
        param_type = "str"
        pdef = None
        if tool and result.need_param in tool.params:
            pdef = tool.params[result.need_param]
            param_type = pdef.type
        label = self._executor.param_label(tool_name, result.need_param)
        if getattr(result, "confirm", False):
            confirm_params = dict(filled)
            if result.need_param:
                confirm_params[result.need_param] = True
            session.set_pending_confirm(
                tool_name,
                confirm_params,
                result.need_param,
                label,
                result.message,
                param_type,
            )
            return
        session.set_pending(
            tool_name, result.need_param, label, filled, result.message, param_type,
            validator=_param_validator(pdef),
            choices=getattr(result, "choices", None),
        )

    def _suspend_confirm(
        self, session, tool_name: str, ev: EvidenceResult, t0: float
    ) -> RouteResult:
        """
        三级证据处置：模型推测值（原文无字面证据）→ 不执行、不静默默认，
        挂起会话级确认，等用户「确认」或给新值。无 session 时只返回确认请求。
        """
        merged = {**ev.kept, **ev.uncertain}
        key = next(iter(ev.uncertain), "")
        tool = self._registry.get(tool_name)
        label = self._executor.param_label(tool_name, key) if key else ""
        value_desc = ", ".join(f"{k}={v}" for k, v in ev.uncertain.items())
        display = tool.display if tool else tool_name
        message = (
            f"端侧推断「{display}」参数 {value_desc}"
            f"（原文未明确{('，' + label) if label else ''}）。"
            f"回复「确认」执行，或直接给新值。"
        )
        if session is not None:
            param_type = "str"
            if tool and key in tool.params:
                param_type = tool.params[key].type
            session.set_pending_confirm(
                tool_name, merged, key, label, message, param_type
            )
            return RouteResult(
                stage=Stage.CONFIRM,
                tool_name=tool_name,
                params=merged,
                exec_result=ExecResult(False, message, "等待确认"),
                latency_ms=(time.monotonic() - t0) * 1000,
                debug={"confirm": list(ev.uncertain.keys())},
            )
        # 无状态模式（session=None）：不盲执行，直接返回确认请求
        return RouteResult(
            stage=Stage.CONFIRM,
            tool_name=tool_name,
            params=merged,
            exec_result=ExecResult(False, message, "需要确认"),
            latency_ms=(time.monotonic() - t0) * 1000,
            debug={"confirm": list(ev.uncertain.keys())},
        )

    def _run_confirm(
        self,
        session,
        user_input: str,
        t0: float,
        analysis=None,
    ) -> RouteResult | None:
        """
        确认挂起决策：确认执行 / 用户给新值 / 取消 / 换话题。
        返回 None 表示 pending 已清空且应走正常路由（换话题）。
        """
        p = session.pending
        action, value = session.resolve_confirm(
            user_input, self._rule, analysis=analysis
        )
        if action == "ambiguous":
            return self._cloud_fallback(user_input, "ambiguous_slot_reply", t0)
        if action in ("confirm", "fill"):
            filled = dict(p.filled)
            if action == "fill":
                filled[p.param_name] = value
            session.clear_pending()
            result = self._executor.execute(p.tool_name, filled, language=detect_language(user_input))
            if result.need_param:
                self._suspend(session, p.tool_name, result, filled)
            return RouteResult(
                stage=Stage.CONFIRM,
                tool_name=p.tool_name,
                params=filled,
                exec_result=result,
                latency_ms=(time.monotonic() - t0) * 1000,
                debug={"confirm_action": action},
            )
        if action == "keep":
            return RouteResult(
                stage=Stage.CONFIRM,
                tool_name=p.tool_name,
                params=dict(p.filled),
                exec_result=ExecResult(False, p.message, "等待确认"),
                latency_ms=(time.monotonic() - t0) * 1000,
                debug={"confirm_action": "keep"},
            )
        if action == "cancel":
            session.clear_pending()
            return RouteResult(
                stage=Stage.CONFIRM,
                tool_name=None,
                params={},
                exec_result=ExecResult(False, "已取消", "用户取消执行"),
                escalate_reason="cancelled",
                latency_ms=(time.monotonic() - t0) * 1000,
                debug={"confirm_action": "cancel"},
            )
        # action == "new"：pending 已被 session 清空，走正常路由
        return None

    # ── 第三段 ────────────────────────────────────────────────────────

    def _local_diagnosis_fallback(
        self,
        user_input: str,
        reason: str,
        t0: float,
        debug: dict,
        *,
        cloud_hint: str = "现在连不上云端",
    ) -> RouteResult | None:
        """云端不可用时，网络故障类请求用本地 net_diagnose 兜底并直接答复。

        只在两处调用：无云端客户端（离线部署）、云端调用抛异常。

        判定有两个条件，缺一不可：
          1. 输入本身是网络故障类表达（同 ⓪e3 的正则）—— 因此也覆盖预检门
             之外的升级分支（如 reason=context_dependent）；
          2. **云端确实不可达**（复用预检门的 _cloud_reachable，带 30s 熔断，
             所以这次调用在门刚探过的情况下几乎零成本）。

        第 2 条必须有：bibit-intent 在 serve 模式下 `cloud_client` 恒为 None（云端升级由
        runtime 负责，见 pipeline.py），只看"没有云端客户端"会在**在线**时也把网络故障
        请求截胡——用户拿到"现在连不上云端…"却同屏看到"✅ 云端可达"。加可达性判断后，
        在线返回 None 维持升级行为，离线才本地作答。

        返回 Stage.RULE：本地已经拿到可答复结果，pipeline 会置 handled=True，
        runtime hook 据此用 direct_reply 直接回给用户（不再转云端）。
        兜底本身失败（工具未注册/执行异常/无结论）时返回 None，由调用方回落原话术。

        cloud_hint：兜底话术的前半句。无云端客户端时要说清"未配置"而不是
        "连不上"——否则会和下方"✅ 云端可达"的探测事实自相矛盾（§7.3 降级不撒谎）。
        """
        if reason in {
            "multi_intent", "multi_target", "conditional_intent", "negated_action",
            "complex_intent", "lexicon_unavailable", "input_too_long", "insufficient_evidence",
            "ambiguous_slot_reply",
        }:
            return None
        if not is_network_fault_diagnosis(user_input):
            return None
        if _cloud_reachable():
            # 云端可达：不属于"离线降级"，交回原升级链路（runtime 处理云端）
            return None
        try:
            result = self._executor.execute("net_diagnose", {}, language=detect_language(user_input))
        except Exception:  # noqa: BLE001 —— 兜底失败不应影响主链路
            return None
        if not result.ok or not str(result.message or "").strip():
            return None
        return RouteResult(
            stage=Stage.RULE,
            tool_name="net_diagnose",
            params={},
            exec_result=ExecResult(
                True,
                f"{cloud_hint}，我用本地检查了一遍：\n{result.message}",
                result.detail or "",
            ),
            escalate_reason=reason,
            latency_ms=(time.monotonic() - t0) * 1000,
            debug={**debug, "local_fallback": "net_diagnose"},
        )

    def _cloud_fallback(
        self, user_input: str, reason: str, t0: float, debug: dict | None = None
    ) -> RouteResult:
        import time
        d = dict(debug or {})
        d["escalate_reason"] = reason

        if self._cloud is None:
            # 无云端客户端（离线部署）：网络故障类请求用本地诊断兜底并直接答复，
            # 其余请求返回明确的升级结果而不崩溃
            local_fallback = self._local_diagnosis_fallback(
                user_input,
                reason,
                t0,
                d,
                cloud_hint="现在连不上云端（本机未配置云端客户端）",
            )
            if local_fallback is not None:
                return local_fallback
            return RouteResult(
                stage=Stage.CLOUD,
                tool_name=None,
                params={},
                exec_result=ExecResult(
                    False,
                    "现在连不上云端，暂时无法处理这个请求",
                    "云端客户端未配置（离线部署），本地无对应能力",
                ),
                escalate_reason=reason,
                latency_ms=(time.monotonic() - t0) * 1000,
                debug=d,
            )

        # 调用云端客户端（接口由 cloud_client 实现）
        try:
            if reason == "multi_intent":
                user_input += "\n相对音量调整使用 adjust_volume(direction, amount=10)；相对亮度调整使用 adjust_brightness(direction, amount=10)；不要丢失默认参数。"
            cloud_result = self._cloud.handle(user_input, reason=reason, language=detect_language(user_input))

            # 云端双模式处理：对话模式返回 content,工具模式返回 message
            mode = cloud_result.get("mode", "tool")
            if mode == "chat":
                # 对话生成模式：直接返回生成的文本
                message = cloud_result.get("content", "(云端无回复)")
            else:
                # 工具调用模式：返回工具调用信息
                message = cloud_result.get("message", "")

            # 多工具调用序列（云端一次返回多个 tool_calls）：按序执行并聚合。
            # 单调用保持原路径（tool/params 交下游执行）。
            calls = cloud_result.get("calls") or []
            if len(calls) > 1:
                results: list[str] = []
                for call in calls:
                    tool = call.get("tool")
                    try:
                        er = self._executor.execute(tool, call.get("params") or {}, language=detect_language(user_input))
                        results.append(f"{tool}: {er.message}")
                    except Exception as exc:  # noqa: BLE001
                        results.append(f"{tool}: 执行失败 {exc}")
                message = "\n".join(results) or message
                return RouteResult(
                    stage=Stage.CLOUD,
                    tool_name=None,
                    params={},
                    exec_result=ExecResult(True, message),
                    latency_ms=(time.monotonic() - t0) * 1000,
                    debug=d,
                )

            return RouteResult(
                stage=Stage.CLOUD,
                tool_name=cloud_result.get("tool"),
                params=cloud_result.get("params", {}),
                exec_result=ExecResult(
                    cloud_result.get("ok", False),
                    message,
                ),
                latency_ms=(time.monotonic() - t0) * 1000,
                debug=d,
            )
        except Exception as e:
            # 云端调用失败：**且云端确实不可达**时，网络故障类请求用本地诊断兜底
            # 并直接答复；其余保持原话术，异常原文只留在 detail（不暴露给用户）。
            local_fallback = self._local_diagnosis_fallback(user_input, reason, t0, d)
            if local_fallback is not None:
                return local_fallback
            return RouteResult(
                stage=Stage.CLOUD,
                tool_name=None,
                params={},
                exec_result=ExecResult(False, "云端不可用，暂时无法处理这个请求", f"云端调用失败：{e}"),
                escalate_reason=reason,
                latency_ms=(time.monotonic() - t0) * 1000,
                debug=d,
            )
