"""
tools/registry.py — 统一工具注册表
从 config/tools.yaml 加载工具声明，提供三段各自需要的视图：
  - rule_view()   →  规则引擎使用的正则模板列表
  - local_view()  →  端侧模型看到的一行式签名清单（极省 token）
  - mcp_schema()  →  完整 MCP/OpenAI function schema（给云端）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from bibit_intent.core.yaml_utils import safe_load
from bibit_intent.resources import resource_path


# 安装类工具的生态提示（system.mcp.install_family 声明 → 自动注入描述，
# 让云端/模型在 pkg/pip/npm 之间选对安装器，而不是把 Python 包塞给 apt）。
_INSTALL_FAMILY_HINTS = {
    "apt": "Only apt/deb system packages: for Python packages use pip_install, "
           "for Node.js packages use npm_install. Accept multiple package names "
           "in one call and combine every requested apt package into a single "
           "tool call; never emit parallel apt tool calls.",
    "pip": "Python (PyPI) packages only: for system packages use pkg_install, "
           "for Node.js packages use npm_install.",
    "npm": "Node.js (npm registry) packages only: for system packages use "
           "pkg_install, for Python packages use pip_install.",
}

_INSTALL_REQUEST_HINT = (
    "Requires an explicit installation request; download-only requests do not "
    "authorize installation or repair, even if the existing package is incomplete. "
    "Do not use this tool merely to fetch an installer."
)


# ── 数据模型 ─────────────────────────────────────────────────────────

class ParamDef(BaseModel):
    type: str
    range: list[int] | None = None
    enum: list[str] | None = None
    default: Any = None
    required: bool | None = None
    unit: str | None = None
    items: dict[str, Any] | None = None   # type=list 时的元素定义（如 {type: str}）
    pattern: str | None = None            # str 参数的合法取值正则（enum 之外放行绝对值）
    hint: str | None = None               # 取值说明：缺参/报错提示与云端描述共用

    @property
    def is_required(self) -> bool:
        """参数是否必填。

        YAML 中显式声明 ``required: false`` 的参数应优先于 ``default is None``
        的旧推导规则；没有显式声明时保持兼容：无默认值视为必填。
        """
        if self.required is not None:
            return self.required
        return self.default is None


class ToolDef(BaseModel):
    name: str
    display: str
    short: str
    params: dict[str, ParamDef]
    routes: dict[str, Any]
    system: dict[str, Any]
    aliases: list[str] = []          # 英文别名，供第二段模糊解析（fuzzy_resolve）
    covers: list[str] = []           # 多宾语只读查询的对象覆盖词
    display_en: str | None = None
    description_en: str | None = None

    # ── 规则视图 ──────────────────────────────────────────────────────
    def rule_patterns(self) -> list[re.Pattern]:
        """返回编译好的正则列表，用于规则引擎匹配。"""
        r = self.routes.get("rule")
        if not r:
            return []
        return [re.compile(p) for p in r.get("patterns", [])]

    def rule_keywords(self) -> list[str]:
        r = self.routes.get("rule")
        return (r or {}).get("keywords", [])

    def rule_require_object(self) -> list[str]:
        """候选注入护栏（§4.4-C）。

        有副作用的工具（wifi_disconnect / usb_reset / eth_up / eth_down…）
        声明本字段后，其**裸症状类关键词**只有在句中出现这些"直接宾语"之一时，
        才允许作为弱命中候选注入。

        为什么需要：关键词弱命中只有 0.5，低于 RULE_CONFIDENCE_THRESHOLD(0.7)，
        不会触发 RULE_CALL，但候选仍会注入给第二段小模型，模型可能直接 CALL
        ——实测"没反应"注入 usb_reset、"是不是断网了"注入 wifi_disconnect。

        注意：本护栏只作用于「关键词弱命中」路径；精确正则命中不受影响，
        因为正则本身已表达宾语要求。
        """
        r = self.routes.get("rule")
        return (r or {}).get("require_object", []) or []

    # ── 端侧视图 ──────────────────────────────────────────────────────
    def local_signature(self) -> str | None:
        """一行式签名，用于端侧模型的极简工具清单。"""
        return self.routes.get("local")

    # ── 完整 MCP schema ───────────────────────────────────────────────
    def mcp_schema(self) -> dict:
        """生成 OpenAI function calling 格式的完整 schema，供云端使用。"""
        props: dict[str, Any] = {}
        required: list[str] = []
        for pname, p in self.params.items():
            pdef: dict[str, Any] = {}
            if p.type in ("int", "float"):
                # float → number：小数刷新率（59.94）走 integer 会被截断/拒收
                pdef["type"] = "integer" if p.type == "int" else "number"
                if p.range and p.type == "int":
                    pdef["minimum"], pdef["maximum"] = p.range
            elif p.type == "bool":
                pdef["type"] = "boolean"
            elif p.type == "str":
                pdef["type"] = "string"
                if p.enum:
                    pdef["enum"] = p.enum
            elif p.type == "list":
                pdef["type"] = "array"
                item_type = (p.items or {}).get("type", "str")
                pdef["items"] = {
                    "type": "string" if item_type != "int" else "integer"
                }
            if p.is_required:
                required.append(pname)
            props[pname] = pdef
        description = self.display_en or self.description_en or self.display
        # Expose declarative aliases to cloud/tool callers.  These aliases are
        # otherwise only consumed by the local resolver (e.g. launch_app).
        aliases = self.system.get("aliases")
        if aliases:
            pairs = ", ".join(
                f"{alias} -> {target}" for alias, target in aliases.items()
            )
            description = f"{description}. Supported application aliases: {pairs}."
        # Discovery hint: an open-ended entity parameter can be resolved by a
        # read-only discovery tool before the call (e.g. list_apps).
        mcp_cfg = self.system.get("mcp") or {}
        discovery = mcp_cfg.get("discovery")
        # discovery 可以是目标工具名（launch_app → "list_apps"）或只读标记
        # （list_apps 自身声明 discovery: true）；只有字符串工具名才追加提示。
        if isinstance(discovery, str) and discovery.strip():
            description = (
                f"{description}. Use the {discovery.strip()} tool to list available "
                "values before calling this tool."
            )
        install_family = mcp_cfg.get("install_family")
        if self.name in {"pkg_install", "pip_install", "npm_install", "install_local", "dep_apply"}:
            description = f"{description} {_INSTALL_REQUEST_HINT}"
        if self.name in {"pkg_install", "install_local", "dep_apply"}:
            description = f"{description} System package writes run on the host through the authorized user-service path, not inside the agent sandbox."
        if install_family:
            hint = _INSTALL_FAMILY_HINTS.get(str(install_family))
            if hint:
                description = f"{description} {hint}"
        if self.mcp_async():
            description = (
                f"{description} This tool runs asynchronously. It returns a "
                "task_id immediately; call bibit_task_status(task_id) to poll "
                "until the task completes."
            )
        # 参数取值提示：MCP inputSchema 由调用方签名派生，enum 到不了模型，
        # 这里把「能填什么」写进描述，避免模型猜值（如把 Hz 塞进档位参数）。
        param_hints = [
            f"{name}: {p.hint}" for name, p in self.params.items() if p.hint
        ]
        if param_hints:
            description = (
                f"{description} Parameter values — " + "; ".join(param_hints) + "."
            )
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                },
            },
        }


    def mcp_group(self) -> str:
        """工具分组（readonly/control/admin），供 MCP 工具面裁剪与授权映射。

        优先级：system.mcp.group 显式声明 → confirm:true 归 admin → 默认 control。
        readonly 只由显式声明给出（如只读发现/查询工具）。
        """
        mcp_cfg = self.system.get("mcp") or {}
        group = mcp_cfg.get("group")
        if group:
            return str(group)
        if self.system.get("confirm"):
            return "admin"
        return "control"

    def mcp_async(self) -> bool:
        """该工具在 MCP 路径上是否改为异步任务执行。

        只用于耗时明显可能超过 Runtime MCP 客户端超时的工具；调用方应立即
        收到 task_id，并通过 bibit_task_status(task_id) 轮询最终结果。
        """
        mcp_cfg = self.system.get("mcp") or {}
        return bool(mcp_cfg.get("async"))

# ── 注册表 ───────────────────────────────────────────────────────────

class ToolRegistry:
    def __init__(self, config_path: str | Path | None = None):
        if config_path is None:
            config_path = resource_path("tools.yaml")
        raw = safe_load(Path(config_path).read_text(encoding="utf-8"))
        self._tools: dict[str, ToolDef] = {}
        # 核心控制域口语线索缓存（惰性构建，见 core_control_cues）
        self._core_cues: set[str] | None = None
        # short name → full name 反查表：第二段模型输出 short 名（vol/sysinfo），
        # 需转回 full 名（set_volume/get_system_info）才能被执行层索引。
        self._short2full: dict[str, str] = {}
        # 工具来源标记：{工具名: skill 名}，None 表示内置工具
        self._tool_source: dict[str, str | None] = {}
        for name, data in raw["tools"].items():
            self.register_tool(name, data, source=None)

    def register_tool(
        self, name: str, data: dict, source: str | None = None
    ) -> ToolDef:
        """
        注册单个工具。内置工具(source=None)与 Skill 工具共用此入口，
        保证 skill 工具与内置工具行为一致(规则/端侧/云端视图、名字解析)。

        source: 工具来源。None=内置(config/tools.yaml)，否则为 skill 名。
        """
        data = dict(data)
        data["name"] = name
        tool = ToolDef(**data)
        self._tools[name] = tool
        if tool.short:
            self._short2full[tool.short] = name
        self._tool_source[name] = source
        return tool

    def get(self, name: str) -> ToolDef | None:
        """按 full name 或 short name 查工具，两者皆可。"""
        if name in self._tools:
            return self._tools[name]
        full = self._short2full.get(name)
        return self._tools.get(full) if full else None

    def app_aliases(self) -> dict[str, str]:
        """Return launch_app aliases from the declarative tool definition."""
        tool = self._tools.get("launch_app")
        aliases = tool.system.get("aliases", {}) if tool else {}
        return {str(key).casefold(): str(value) for key, value in aliases.items()}

    def resolve_name(self, name: str) -> str | None:
        """把任意名字（short/full）规范化为 full name；查不到返回 None。"""
        if name in self._tools:
            return name
        return self._short2full.get(name)

    def fuzzy_resolve(self, name: str, cutoff: float = 0.6) -> str | None:
        """
        模糊解析工具名。用于第二段：让小模型先自由输出一个大致工具名，
        再在解析层三级兜底匹配到真实工具（design.md §5：容错优先）。

        匹配顺序：
          1. 精确（full / short）
          2. 别名（tools.yaml 的 keywords，英文子串双向包含）
          3. 近似（difflib 对 full+short+英文别名做相似度匹配）
        查不到返回 None（路由器据此升级）。
        """
        if not name:
            return None
        n = name.strip().lower().replace("-", "_")

        # 1. 精确
        exact = self.resolve_name(n) or self.resolve_name(name.strip())
        if exact:
            return exact

        def _ascii_terms(tool) -> list[str]:
            terms = [tool.name.lower(), tool.short.lower()]
            terms += [a.lower() for a in tool.aliases]
            terms += [kw.lower() for kw in tool.rule_keywords() if kw.isascii()]
            return [t for t in terms if t]

        # 2. 别名子串（英文，如 "volume" ⊂ "set_volume"、"cpu" ∈ aliases）
        for full, tool in self._tools.items():
            for c in _ascii_terms(tool):
                if c in n or n in c:
                    return full

        # 3. difflib 近似匹配
        import difflib
        vocab: dict[str, str] = {}
        for full, tool in self._tools.items():
            for c in _ascii_terms(tool):
                vocab[c] = full
        hit = difflib.get_close_matches(n, list(vocab), n=1, cutoff=cutoff)
        return vocab[hit[0]] if hit else None

    def resolve_with_fallback(
        self, name: str, fallbacks: list[str] | None = None
    ) -> str | None:
        """
        容错解析端侧输出（design.md §5「容错优先」）：
          1. 正常 fuzzy_resolve（short/full/别名/近似）
          2. 失败 → 按 embedding 强提示 / 唯一候选兜底
        端侧 1B 常把口语里的软件名当工具名输出（如 "CALL vim"），
        此时按上下文候选兜底，避免无谓升级到云端。
        """
        hit = self.fuzzy_resolve(name)
        if hit:
            return hit
        for fb in fallbacks or []:
            full = self.resolve_name(fb)
            if full:
                return full
        return None

    def all(self) -> list[ToolDef]:
        return list(self._tools.values())

    def local_tool_list(
        self,
        candidates: list[str] | None = None,
        hints: bool = True,
        top_hint: str | None = None,
    ) -> str:
        """
        生成端侧模型看到的极简工具清单。
        candidates 为 None 时返回全部，否则只返回候选子集。
        hints=True 时在注释里附中文触发线索（复用 rule 的 keywords），
          帮助 1.7B 把口语/间接表达（"开大声音"）桥接到工具，减少误升级。
        top_hint：embedding 语义路由给出的「最可能工具」，在清单首行用
          注释强提示，帮小模型在口语用例里快速锁定目标工具。
        这是 design.md §5.1 「动态候选注入」的实现。
        """
        # 空列表表示「不注入任何工具」（区别于 None=全部），
        # 供无关输入使用：模型看到空清单应输出 ESCALATE out_of_scope
        if candidates is None:
            tools = list(self._tools.values())
        else:
            tools = [self._tools[n] for n in candidates]
        lines = []
        if top_hint:
            t = self._tools.get(top_hint)
            if t and t.local_signature():
                lines.append(f"// 最可能是: {t.short or t.name}，请优先选择它")
        for t in tools:
            sig = t.local_signature()
            if not sig:
                continue
            note = t.display
            if hints:
                # 触发线索优先取 aliases 里的中文口语词（如"太暗/大声"），
                # 不足再补 rule keywords。控制在 4 个以内以省 token。
                cn = [a for a in t.aliases if not a.isascii()]
                cn += [k for k in t.rule_keywords()
                       if not k.isascii() and k not in cn]
                if cn:
                    note = "/".join(cn[:4])
            lines.append(f"{sig}  // {note}")
        return "\n".join(lines)

    def local_tool_names(
        self, candidates: list[str] | None = None
    ) -> list[str]:
        """返回会出现在端侧工具清单里的工具名（与 local_tool_list 对齐）。

        用于把端侧解码 grammar 的 tool-name 闭合到候选集合：只枚举有
        local_signature 的工具，避免 grammar 允许模型输出清单里根本不存在的
        工具名（如云端专属工具）。
        """
        if candidates is None:
            tools = list(self._tools.values())
        else:
            tools = [self._tools[n] for n in candidates]
        return [t.name for t in tools if t.local_signature()]

    def mcp_schemas(self, candidates: list[str] | None = None) -> list[dict]:
        tools = [self._tools[n] for n in (candidates or self._tools)]
        return [t.mcp_schema() for t in tools]

    def core_tools(self) -> list[str]:
        """
        核心控制工具集：关键词预筛无命中时注入这一小集（而非全量），
        减少干扰项。

        只含**内置工具**(source=None)，排除：
          - Skill 工具（source=skill名）——skill 工具靠关键词命中才注入，
            不进兜底核心集，避免 skill 越多、核心集越膨胀、模型越易误判。
        对照 design.md §5.1「动态候选注入」的兜底策略。
        """
        return [
            n for n in self._tools
            if self._tool_source.get(n) is None
        ]

    def core_control_cues(self) -> set[str]:
        """
        核心控制域的口语触发线索：内置工具（core_tools）的别名 + 规则关键词。
        用于 decide_local_intent 在「关键词无命中」时判断是否值得注入核心
        工具集——只对确实涉及核心控制的输入兜底（如"太暗/看不清/占用"），
        无关输入（天气/闲聊/写作）不硬塞工具，避免 1B 模型在无关工具里
        幻觉 CALL（如"今天天气怎么样"被误判成 set_volume）。
        """
        if self._core_cues is None:
            cues: set[str] = set()
            for n in self.core_tools():
                t = self._tools[n]
                cues.update(t.aliases)
                cues.update(t.rule_keywords())
            self._core_cues = cues
        return self._core_cues

    def get_bt_device_context(self) -> str:
        """
        获取已知蓝牙设备列表，供云端模型注入上下文（用于填 bt_connect 的 device）。

        只给**设备名**并按名字去重（理由见 bluetooth/handlers.known_device_labels）：
        BLE 设备的随机地址每次重新配对都会变，把历史地址摆给模型只会让它去连一个
        已经失效的地址（实测报 "not available"/"le-connection-abort-by-local"）。

        注意取 handlers 的方式：skills/ 目录不是 Python 包，不能按
        `bibit_intent.resources.skills.*` import（会 ModuleNotFoundError），
        skill_loader 是以 `_skill_<skill名>` 把模块放进 sys.modules 的。
        """
        try:
            source = self._tool_source.get("bt_connect")
            module = sys.modules.get(f"_skill_{source}") if source else None
            labels = module.known_device_labels() if module else []
            if not labels:
                return ""
            return ("已知蓝牙设备（直接按名称调用 bt_connect，不要使用历史 MAC 地址）："
                    + "、".join(labels))
        except Exception:  # noqa: BLE001
            return ""
