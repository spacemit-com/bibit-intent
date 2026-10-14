"""
agent/local_model.py — 第二段：端侧 1.7B 模型客户端
职责：保留端侧推理的业务逻辑，网络传输由 AI Gateway 子类提供，
     用 GBNF grammar 强制输出紧凑结构化指令。

对照 design.md §4.2、§5

关键实测结论（来自 tech-selection.md §7）：
  - Qwen3 必须关闭 thinking（enable_thinking:false），否则思考链吃掉全部 budget
  - GBNF grammar 不占 prompt token、100% 保证格式 → 第二段首选
  - Function calling 单个 schema ~165 token，多工具场景在 512T 不可行
  - 输出协议：CALL <tool> <k>=<v> 或 ESCALATE reason=<reason>
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import re
import urllib.error
import json
import math
import time
from dataclasses import dataclass
from typing import Any

from bibit_intent.analysis.features import UtteranceAnalysis
from bibit_intent.core.language import detect_language

# ── 输出协议解析 ─────────────────────────────────────────────────────

@dataclass
class LocalModelResult:
    action: str                     # "CALL" 或 "ESCALATE"
    tool_name: str | None = None    # CALL 时的工具名
    params: dict[str, Any] | None = None  # CALL 时的参数键值对
    reason: str | None = None       # ESCALATE 时的原因
    raw: str = ""                   # 模型原始输出，调试用
    latency_ms: float = 0.0
    avg_logprob: float = 0.0
    p_call: float = 0.0
    p_escalate: float = 0.0

    @property
    def is_call(self) -> bool:
        return self.action == "CALL"

    @property
    def is_escalate(self) -> bool:
        return self.action == "ESCALATE"


# GBNF grammar：强制输出 CALL/ESCALATE 紧凑协议
# 不进入 prompt，由 llama-server 在解码侧约束输出
# 参考：design.md §5.2「端侧精简工具描述格式」
# 收紧点（实测 1B 会在 kv 上编造垃圾键值，如
# "CALL bright level=50 _or_=0 _or_=100..."）：
#   - kv 对上限 {0,5} → {0,2}（工具参数最多 2 个，阻断无限续写键值）
#   - value 从 [a-z0-9_.]+ 收窄为 数字/true/false/纯小写短词(≤10)
#     （"blue"、"no_need_to_change_volume" 等编造值仍属语法允许，
#      但 `_or_` 这类下划线垃圾键、超长续写单词被语法直接禁掉）
_GBNF_GRAMMAR = r"""
root    ::= call-cmd | escalate-cmd
call-cmd    ::= "CALL " tool-name kv-list
escalate-cmd ::= "ESCALATE reason=" word
tool-name   ::= [a-z_]+
kv-list     ::= (" " kv-pair){0,2}
kv-pair     ::= word "=" value
word        ::= [a-z_]{1,12}
value       ::= number | "true" | "false" | [a-z]{1,10}
number      ::= [0-9]+ ("." [0-9]+)?
"""


def build_call_grammar(tool_names: list[str] | None) -> str:
    """按候选工具集构造 CALL/ESCALATE 解码语法。

    - ``tool_names=None``：保持旧宽语法（探针/离线直调兼容）；
    - 空列表：只允许 ``ESCALATE``，从解码侧杜绝「空工具清单下乱猜工具名」
      的幻觉 CALL；
    - 非空列表：把 ``tool-name`` 闭合枚举到候选集合，替代 ``[a-z_]+`` 的
      任意工具名，阻断跨工具错选（如本该 set_volume 却输出 launch_app）。

    规则名一律用连字符（llama-server 不接受下划线规则名），工具名字面量
    保留原下划线不受影响。
    """
    if tool_names is None:
        return _GBNF_GRAMMAR
    valid = [name for name in tool_names if re.fullmatch(r"[a-z_]+", name)]
    if not valid:
        return (
            "root ::= escalate-cmd\n"
            'escalate-cmd ::= "ESCALATE reason=" reason-word\n'
            "reason-word ::= [a-z_]{1,20}\n"
        )
    tool_rule = " | ".join(f'"{name}"' for name in valid)
    return (
        "root ::= call-cmd | escalate-cmd\n"
        'call-cmd ::= "CALL " tool-name kv-list\n'
        f"tool-name ::= {tool_rule}\n"
        'escalate-cmd ::= "ESCALATE reason=" reason-word\n'
        'kv-list ::= (" " kv-pair){0,2}\n'
        'kv-pair ::= word "=" value\n'
        "word ::= [a-z_]{1,20}\n"
        "reason-word ::= [a-z_]{1,20}\n"
        'value ::= number | "true" | "false" | [a-z]{1,10}\n'
        'number ::= [0-9]+ ("." [0-9]+)?\n'
    )

# GBNF grammar（标签层，launch/close 窄域）：只允许两个方向标签与两个升级
# 原因，工具名/reason 全部闭合枚举——1.5B 不存在"名单外乱选工具"的空间。
# 实测（glm_practice_experiment.md §6）：标签层 + 闭合 grammar 后模型只输出
# 方向，参数由 router 从原文确定性抽取（方案 B），猜值/编造被结构性消除。
_LABEL_GBNF_GRAMMAR = r"""
root    ::= call-cmd | escalate-cmd
call-cmd    ::= "CALL " tool-name
escalate-cmd ::= "ESCALATE reason=" reason
tool-name   ::= "launch" | "close"
reason      ::= "out_of_scope" | "missing_param"
"""


# ── per-tool bound grammar（方案 A 生产形态，见 docs/evaluation/
#    glm_practice_experiment.md §9-10）──────────────────────────────
# 方向由确定性通道（规则 / 标签层 / 唯一候选 / agreed 双通道）先拍板后，
# grammar 把工具名锁死为单个工具，模型只负责把原文明确出现的值翻译成
# CALL <tool> <key>=<value>：参数键值闭合到 param_spec 声明值域——枚举→
# 字面量、数值→number-val、文本→[a-z0-9_.]{1,16}。与宽 _GBNF_GRAMMAR 的
# 差异：同域工具错选（vol↔bright）、跨工具垃圾键（orlaunch/oracle）在解码
# 侧没有落点（§8.3.4），猜值由 router 证据闸门在解码后拦截。
# 注意：llama-server 要求规则名不含下划线（否则 400 invalid grammar），
# 一律转连字符；工具名/键以字符串字面量写死，允许保留下划线。
def build_bound_grammar(
    tool_name: str, param_spec: dict[str, dict[str, Any]]
) -> str:
    def rule_id(name: str) -> str:
        return name.replace("_", "-")

    tool_r = rule_id(tool_name)
    lines = [
        f'root    ::= call-{tool_r} | escalate-cmd',
        f'call-{tool_r} ::= "CALL {tool_name}" kv-list?',
        'escalate-cmd ::= "ESCALATE reason=" reason',
        'reason  ::= "missing_param" | "out_of_scope"',
    ]
    if param_spec:
        # 键组合：每个声明键至多出现一次，按声明顺序枚举全部子集，
        # 语法层面杜绝未声明键（_or_/orlaunch 等）与重复键。
        kv_pairs: list[str] = []
        val_rules: list[str] = []
        items = list(param_spec.items())
        for i, (key, spec) in enumerate(items):
            vtype = str(spec.get("type", "str"))
            enum = spec.get("enum") or []
            if vtype in ("int", "float"):
                val_rule = "number-val"
            elif vtype == "bool":
                val_rule = "bool-val"
            elif enum:
                val_rule = f"kv-{i}-val"
                val_rules.append(
                    f"kv-{i}-val ::= "
                    + " | ".join(f'"{v}"' for v in enum)
                )
            else:
                val_rule = f"kv-{i}-val"
                val_rules.append(f"kv-{i}-val ::= [a-z0-9_.]{{1,16}}")
            # 键字面量带前导空格（" level="）：与 "CALL <tool>" 直接拼接，
            # 实测缺空格会输出 "CALL set_volumelevel=60" 这类无法解析的串。
            kv_pairs.append(f'kv-pair-{i} ::= " {key}=" {val_rule}')
        n = len(kv_pairs)
        if n > 6:
            # 理论边界防御（当前本地工具参数数 ≤3）：退化回键枚举列表，
            # 避免子集枚举爆炸。
            alts = " | ".join(f"kv-pair-{i}" for i in range(n))
            lines.append(f"kv-list ::= {alts}")
        else:
            subsets = []
            for mask in range(1, 1 << n):
                subsets.append(
                    " ".join(
                        f"kv-pair-{i}"
                        for i in range(n)
                        if mask & (1 << i)
                    )
                )
            lines.append("kv-list ::= " + " | ".join(subsets))
        lines.extend(kv_pairs)
        lines.extend(val_rules)
    lines.append('number-val ::= [0-9]+ ("." [0-9]+)?')
    lines.append('bool-val ::= "true" | "false"')
    return "\n".join(lines) + "\n"


_SPECIAL_TOKEN_RESERVE = 8


# ── 系统提示（紧凑，实测含 2 个候选工具约 133 token）────────────────
def _build_system_prompt(
    tool_list: str, analysis: UtteranceAnalysis | None = None
) -> str:
    """
    端侧系统提示保持紧凑：实测含 2 个候选工具约 133 token，加用户输入与
    30 token 输出仍远低于 512 上下文。
    格式：角色 + 工具清单 + 参数规则 + 唯一输出规则。
    注意：不要加 few-shot 示例——实测 1B 模型会照抄示例的 CALL 结构和
    工具名（"扫一下…"被带成 CALL launch app=…），严重污染工具选择。
    防猜指令按业界"禁令 + 替代行为 + 区块式规则"写法（Prohibition-First、
    Hermes/xLAM 同思路，见 rule_and_intent_design.md §5.9 与
    slot_missing_management.md §5）：只填用户明确给出的值、未给出省略、
    必填缺失输出 missing_param。1B 在 GBNF 强制输出数字的压力下仍可能
    猜端点/中值（大→100/小→0，见 §5.8），prompt 只是降概率，真正的兜底
    在 router 的原文证据闸门（_keep_evidenced_params）：模型省略参数后
    由执行层走默认值或缺参回填，不盲执行猜值。
    """
    structure_hint = ""
    if analysis is not None:
        parts: list[str] = []
        if analysis.predicates:
            parts.append("动词=" + "/".join(analysis.predicates[:3]))
        if analysis.has_negation:
            parts.append("否定=是")
        if analysis.connectors:
            parts.append("连接词=" + "/".join(analysis.connectors[:2]))
        if parts:
            structure_hint = "\n// 结构: " + "; ".join(parts) + "\n"
    if analysis is not None and detect_language(analysis.text) == "en":
        return (
        "You are an edge assistant. Choose a tool and output exactly one line:\n"
        "CALL <tool> <param>=<value> or ESCALATE reason=<reason>\n"
        "Tools:\n" + f"{tool_list}\n" +
        "Only use values explicitly stated by the user; never guess. "
        "Missing required parameters: ESCALATE reason=missing_param. "
        "Writing, open questions, chit-chat, and complex tasks: ESCALATE reason=out_of_scope."
        )
    return (
        "你是端侧助手。根据用户指令选择工具，只输出一行：\n"
        "CALL <工具> <参数>=<值>  或  ESCALATE reason=<原因>\n"
        "工具：\n"
        f"{tool_list}\n"
        f"{structure_hint}"
        "- 只填用户明确给出的值，未给出的参数省略，禁止猜值；\n"
        "- 必填参数缺失时输出 ESCALATE reason=missing_param。\n"
        "写作、开放知识问答、闲聊或多步复杂任务，"
        "输出 ESCALATE reason=out_of_scope。"
    )


def _confidence_from_probs(probs: list[dict]) -> tuple[float, float, float]:
    """从 llama.cpp completion_probabilities 提取 avg_logprob 与 CALL/ESCALATE 首 token 概率。"""
    if not probs:
        return 0.0, 0.0, 0.0
    avg_lp = sum(p.get("logprob") or 0.0 for p in probs) / len(probs)
    first = probs[0]
    merged: dict[str, float] = {}
    chosen = first.get("token")
    chosen_lp = first.get("logprob")
    if chosen is not None and chosen_lp is not None:
        merged[chosen] = max(merged.get(chosen, float("-inf")), chosen_lp)
    for item in first.get("top_logprobs") or []:
        tok, lp = item.get("token"), item.get("logprob")
        if tok is not None and lp is not None:
            merged[tok] = max(merged.get(tok, float("-inf")), lp)
    denom = sum(math.exp(lp) for lp in merged.values())
    if denom <= 0:
        return avg_lp, 0.0, 0.0
    p_call = math.exp(merged.get("C", float("-inf"))) / denom if "C" in merged else 0.0
    p_esc = math.exp(merged.get("E", float("-inf"))) / denom if "E" in merged else 0.0
    return avg_lp, p_call, p_esc


def _build_label_system_prompt() -> str:
    """
    launch/close 窄域「标签层」系统提示（L3 形态，见 docs/evaluation/
    glm_practice_experiment.md §5-6 与 glm_intent_research.md §2.1/2.3）。

    设计依据（GLM-Edge-1.5B 实测）：
      - 纯文字规则对 1.5B 近乎无效（L1：规则逐字写出"天气/写作→OOS"仍 3/12）；
      - few-shot 边界示例才有效（L3：方向 36/36、OOS 12/12）；few-shot 有效的
        前提是输出空间已被闭合 grammar 锁死（无参数可抄，照抄只剩方向，无害）；
      - 每类 2-3 个代表性示例：倒装（把X打开）、动词前置（退出微信）、
        插入语（退出一下/正在放的…关一下）+ 2 个 OOS 负例。
    与 _build_system_prompt 的差异：这里"不产参数"，应用名一律由 router 的
    _deterministic_app_params 从原文抽取，避免 1.5B 在开放参数上猜值/编造。
    """
    return (
        "你是端侧助手。你的任务只判断用户指令属于哪个工具方向或是否需要升级，"
        "只输出一行：\n"
        "CALL <工具>  或  ESCALATE reason=out_of_scope|missing_param\n"
        "可用工具方向（不填任何参数）：\n"
        "launch  // 打开/启动/运行/开启应用\n"
        "close   // 关闭/退出/停止/结束应用\n"
        "- 用户明确要打开/启动某个应用 → CALL launch；\n"
        "- 用户明确要关闭/退出某个应用 → CALL close；\n"
        "- 写作、天气、闲聊、知识问答、总结、多步复杂任务，或未提到任何应用，"
        "或无法确定方向 → ESCALATE reason=out_of_scope；\n"
        "- 方向已确定但缺少应用名等必要信息 → 仍输出 CALL <工具>，不填参数。\n"
        "示例：\n"
        "用户：帮我把记事本打开\n指令：CALL launch\n"
        "用户：退出微信\n指令：CALL close\n"
        "用户：帮我把视频软件退出一下\n指令：CALL close\n"
        "用户：把正在放的视频软件关一下\n指令：CALL close\n"
        "用户：今天天气怎么样\n指令：ESCALATE reason=out_of_scope\n"
        "用户：帮我写一封邮件\n指令：ESCALATE reason=out_of_scope\n"
    )


def _build_bound_system_prompt(tool_name: str, tool_list: str | None) -> str:
    """
    per-tool bound grammar 的系统提示。

    §9.3 第 4 条「提示词格式敏感」：即使工具已被 grammar 锁死，提示词也要
    保持「根据用户指令选择工具并填值」的风格（列出工具清单），不能改成
    「工具方向已确定、只补一个参数」的填空式——后者会把 1.5B 带进占位模式
    （O1→xxx、O3→latesttag）。因此优先复用 _build_system_prompt（含防猜指令），
    tool_list 为单工具清单时即自然退化为「只填这一个工具的值」。
    """
    if tool_list:
        return _build_system_prompt(tool_list)
    return (
        "你是端侧助手。根据用户指令选择工具并填值，只输出一行：\n"
        f"CALL {tool_name} <参数>=<值>  或  ESCALATE reason=missing_param\n"
        f"工具：{tool_name}\n"
        "- 只填用户明确给出的值，未给出的参数省略，禁止猜值；\n"
        "- 必填参数缺失时输出 ESCALATE reason=missing_param；\n"
        "- 写作、闲聊或域外任务输出 ESCALATE reason=out_of_scope。"
    )


def _fit_prompt(system: str, user_input: str, max_bytes: int) -> str | None:
    """Strict byte budget: never truncate; return None to escalate to cloud."""
    prompt = f"{system}\n\nUser: {user_input}\nCommand: " if "You are an edge assistant" in system else f"{system}\n\n用户：{user_input}\n指令："
    if len(prompt.encode("utf-8")) > max_bytes:
        return None
    return prompt


# ── 本地模型客户端 ───────────────────────────────────────────────────

class LocalModelClient(ABC):
    """
    调用本地 llama-server 的 /completion 端点。
    使用 GBNF grammar 约束输出，不使用 function calling（token 开销太大）。
    """

    def __init__(
        self,
        context_limit: int = 512,
        timeout: int = 30,
    ):
        self.context_limit = context_limit
        self.timeout = timeout

    @abstractmethod
    def is_available(self) -> bool:
        """Check the configured inference transport."""
        raise NotImplementedError

    def infer(
        self,
        user_input: str,
        tool_list: str,
        max_output_tokens: int = 30,
        analysis: UtteranceAnalysis | None = None,
        n_probs: int = 0,
        tool_names: list[str] | None = None,
    ) -> LocalModelResult:
        """
        主推理入口。
        tool_list: local_view() 生成的一行式工具签名清单（见 registry.py）
        tool_names: 候选工具名列表，用于把 grammar 的 tool-name 闭合到候选集；
                    None 保持旧宽语法，空列表只允许 ESCALATE。
        max_output_tokens: CALL/ESCALATE 一行约 10~20 token，留 30 足够
        """
        system = _build_system_prompt(tool_list, analysis)
        output_tokens = max(1, min(int(max_output_tokens), self.context_limit))
        budget = max(0, self.context_limit - output_tokens - _SPECIAL_TOKEN_RESERVE)
        prompt = self._fit_prompt_tokens(system, user_input, budget)
        if prompt is None:
            # /tokenize 不可用或预算本身过小 → 回退 UTF-8 字节保守估算
            prompt = _fit_prompt(system, user_input, budget)
        if prompt is None:
            return LocalModelResult(
                action="ESCALATE",
                reason="context_overflow",
            )

        payload = {
            "prompt": prompt,
            "grammar": build_call_grammar(tool_names),
            "n_predict": output_tokens,
            "temperature": 0.1,     # 低温度保证格式稳定
            "stop": ["\n", "用户：", "User:"],
            # Qwen3 必须关 thinking，否则吃掉全部 budget
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if n_probs > 0:
            payload["n_probs"] = n_probs

        t0 = time.monotonic()
        try:
            raw = self._post("/completion", payload)
        except urllib.error.URLError as e:
            # 服务不可达 → 升级到第三段
            return LocalModelResult(
                action="ESCALATE",
                reason="local_server_unavailable",
                raw=str(e),
                latency_ms=(time.monotonic() - t0) * 1000,
            )

        latency_ms = (time.monotonic() - t0) * 1000
        output = raw.get("content", "").strip()
        result = _parse_output(output, latency_ms)
        if n_probs > 0:
            result.avg_logprob, result.p_call, result.p_escalate = _confidence_from_probs(
                raw.get("completion_probabilities") or []
            )
        return result

    def infer_label(
        self,
        user_input: str,
        max_output_tokens: int = 15,
    ) -> LocalModelResult:
        """
        launch/close 窄域标签层推理：只判方向（CALL launch|close）或升级
        （ESCALATE reason=out_of_scope|missing_param），不输出任何参数。

        与 infer()（CALL 参数协议）的区别：grammar 换成 _LABEL_GBNF_GRAMMAR
        （工具名/reason 闭合枚举），n_predict 更小（方向一行约 3-8 token）。
        中文输入专用（英文直接指令由规则层覆盖，见 router 的调用门）。
        """
        system = _build_label_system_prompt()
        output_tokens = max(1, min(int(max_output_tokens), self.context_limit))
        budget = max(0, self.context_limit - output_tokens - _SPECIAL_TOKEN_RESERVE)
        prompt = self._fit_prompt_tokens(system, user_input, budget)
        if prompt is None:
            prompt = _fit_prompt(system, user_input, budget)
        if prompt is None:
            return LocalModelResult(
                action="ESCALATE",
                reason="context_overflow",
            )

        payload = {
            "prompt": prompt,
            "grammar": _LABEL_GBNF_GRAMMAR,
            "n_predict": output_tokens,
            "temperature": 0.1,
            "stop": ["\n", "用户：", "User:"],
        }
        t0 = time.monotonic()
        try:
            raw = self._post("/completion", payload)
        except urllib.error.URLError as e:
            return LocalModelResult(
                action="ESCALATE",
                reason="local_server_unavailable",
                raw=str(e),
                latency_ms=(time.monotonic() - t0) * 1000,
            )
        latency_ms = (time.monotonic() - t0) * 1000
        output = raw.get("content", "").strip()
        return _parse_output(output, latency_ms)

    def infer_bound(
        self,
        user_input: str,
        tool_name: str,
        tool_list: str | None = None,
        param_spec: dict[str, dict[str, Any]] | None = None,
        max_output_tokens: int = 30,
        analysis: UtteranceAnalysis | None = None,
    ) -> LocalModelResult:
        """
        方向已锁定（规则 / 标签层 / 唯一候选 / agreed 双通道）后的
        per-tool bound grammar 单次生成（方案 A 生产化，见
        glm_practice_experiment.md §9-10）。

        grammar 由 build_bound_grammar 生成：工具名锁死为 tool_name，参数键
        值闭合到 param_spec 声明值域。模型不再拥有「选工具 / 编造键值」的
        自由，只把原文里明确出现的值翻译成 CALL <tool> <key>=<value>；值
        缺失 / 域外走 ESCALATE 分支。真正的兜底仍在 router 证据闸门（值
        必须回原文有依据），本方法不重复二次重试（§9.3.3）。

        param_spec: {参数名: {"type": "int|float|str|bool", "enum": [...],
                              "range": [...]}}，由 router 从 ToolDef.params 派生。
        """
        spec = {k: dict(v) for k, v in (param_spec or {}).items()}
        grammar = build_bound_grammar(tool_name, spec)
        system = _build_bound_system_prompt(tool_name, tool_list)
        output_tokens = max(1, min(int(max_output_tokens), self.context_limit))
        budget = max(0, self.context_limit - output_tokens - _SPECIAL_TOKEN_RESERVE)
        prompt = self._fit_prompt_tokens(system, user_input, budget)
        if prompt is None:
            prompt = _fit_prompt(system, user_input, budget)
        if prompt is None:
            return LocalModelResult(
                action="ESCALATE",
                reason="context_overflow",
            )

        payload = {
            "prompt": prompt,
            "grammar": grammar,
            "n_predict": output_tokens,
            "temperature": 0.1,
            "stop": ["\n", "用户：", "User:"],
            "chat_template_kwargs": {"enable_thinking": False},
        }
        t0 = time.monotonic()
        try:
            raw = self._post("/completion", payload)
        except urllib.error.URLError as e:
            return LocalModelResult(
                action="ESCALATE",
                reason="local_server_unavailable",
                raw=str(e),
                latency_ms=(time.monotonic() - t0) * 1000,
            )
        latency_ms = (time.monotonic() - t0) * 1000
        output = raw.get("content", "").strip()
        return _parse_output(output, latency_ms)

    def _tokenize(self, text: str) -> int | None:
        """用 llama-server 的 /tokenize 端点精确计数；失败返回 None。"""
        try:
            tokens = self._post("/tokenize", {"content": text}).get("tokens")
            if isinstance(tokens, list):
                return len(tokens)
        except Exception:
            return None
        return None

    def _fit_prompt_tokens(
        self, system: str, user_input: str, budget_tokens: int
    ) -> str | None:
        """按真实 token 数判断是否超出预算；超限返回 None 直接升级云端，不截断。"""
        full = f"{system}\n\n用户：{user_input}\n指令："
        n = self._tokenize(full)
        if n is None:
            return None
        if n <= budget_tokens:
            return full
        return None

    @abstractmethod
    def _post(self, path: str, payload: dict) -> dict:
        """Send a native inference request through the configured transport."""
        raise NotImplementedError


# ── 输出解析 ─────────────────────────────────────────────────────────

_CALL_RE = re.compile(
    r"^CALL\s+(?P<tool>[a-z_]+)"
    r"(?:\s+(?P<kvs>[a-z_]+=\S+(?:\s+[a-z_]+=\S+)*))?$"
)
_ESCALATE_RE = re.compile(r"^ESCALATE\s+reason=(?P<reason>[a-z_]+)$")


def _parse_output(raw: str, latency_ms: float) -> LocalModelResult:
    """
    解析模型输出的 CALL/ESCALATE 紧凑协议。
    解析失败也返回 ESCALATE（让路由器安全升级），不抛异常。
    """
    line = raw.strip()

    m = _CALL_RE.match(line)
    if m:
        params = {}
        for kv in (m.group("kvs") or "").split():
            k, _, v = kv.partition("=")
            # 尝试转换数字
            try:
                params[k] = int(v)
            except ValueError:
                try:
                    params[k] = float(v)
                except ValueError:
                    params[k] = v
        return LocalModelResult(
            action="CALL",
            tool_name=m.group("tool"),
            params=params,
            raw=raw,
            latency_ms=latency_ms,
        )

    m = _ESCALATE_RE.match(line)
    if m:
        return LocalModelResult(
            action="ESCALATE",
            reason=m.group("reason"),
            raw=raw,
            latency_ms=latency_ms,
        )

    # 解析失败：格式不合法，安全升级
    return LocalModelResult(
        action="ESCALATE",
        reason="parse_failed",
        raw=raw,
        latency_ms=latency_ms,
    )
