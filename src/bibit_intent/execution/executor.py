"""
agent/executor.py — 执行层
职责：把（工具名 + 参数）落地为真实系统操作。
  - 参数补全（用 tools.yaml 的默认值填缺失槽位）
  - 参数校验（范围/枚举/类型）
  - 危险操作二次确认
  - 统一返回 ExecResult

对照 design.md §4（控制系统）+ §12（安全）
"""
from __future__ import annotations

import inspect
import re
from typing import Any, Callable
from bibit_intent.core.package_target import (
    is_exact_package,
    split_exact_install_specs,
    split_exact_packages,
)

from bibit_intent.execution.cancellation import (
    begin_execution,
    end_execution_without_side_effect,
    raise_if_cancelled,
)
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.system_tools import HANDLERS, ExecResult
from bibit_intent.execution.md_format import render_detail
from bibit_intent.core.localization import localize_reply
from bibit_intent.knowledge.hallucination import (
    DetectionKind,
    Disposition,
    HallucinationRecord,
    HallucinationStats,
)


# 常见参数的中文提示词（缺参澄清时展示给用户）
_PARAM_CN = {
    "pkg": "软件包名",
    "query": "搜索关键词",
    "port": "端口号",
    "path": "文件或目录路径",
    "dir": "目录路径",
    "user": "用户名",
    "app_name": "应用名",
    "target": "窗口名或应用名",
    "filter": "筛选关键字",
    "level": "数值",
    "direction": "方向",
    "ssid": "WiFi 网络名",
    "device": "设备名",
    "mode": "档位或目标值",
    "rate": "刷新率（Hz）",
    "size": "分辨率（如 1920x1080）",
    "ratio": "缩放比例",
    "angle": "旋转角度",
}

# 缺参样例（按 工具+参数 / 参数 ）——比按类型兜底的"如 vim"准确
_PARAM_SAMPLE = {
    "rate": "如 50 / 59.94",
    "size": "如 1920x1080",
    "ratio": "如 1.25（也可说 1 / 1.5 / 125%）",
    "angle": "如 90",
}
_PARAM_SAMPLE_BY_TOOL = {
    ("set_refresh_rate", "mode"): "如 up/down/max/min，或直接给 Hz（如 50 / 59.94）",
    ("set_resolution", "mode"): "如 up/down/max/min，或直接给 WxH（如 1920x1080）",
}

_READ_ONLY_BUILTINS = frozenset(
    {"get_system_info", "get_vol_status", "get_display_light", "display_modes",
     "query_knowledge", "eth_status", "eth_ip",
     "window_list", "list_apps", "file_search"}
)
_READ_ONLY_WIFI = frozenset({"wifi_scan", "wifi_status", "wifi_ip"})


def _value_allowed(pdef, val: str) -> bool:
    """str 参数取值校验：enum 与 pattern 任一通过即合法。

    pattern 用来放行 enum 之外的绝对值（如 mode 既收 up/down，也收 50 /
    1920x1080），两者都没声明时不校验，保持既有行为。
    """
    if pdef.enum and val in pdef.enum:
        return True
    if pdef.pattern and re.fullmatch(pdef.pattern, val):
        return True
    return not pdef.enum and not pdef.pattern


def _value_options(pdef) -> str:
    """校验失败时给用户/模型看的合法取值说明。"""
    if pdef.hint:
        return pdef.hint
    parts: list[str] = []
    if pdef.enum:
        parts.append("/".join(str(v) for v in pdef.enum))
    if pdef.pattern:
        parts.append(f"匹配 {pdef.pattern}")
    return " 或 ".join(parts) or "取值不合法"


def _normalize_envelope_params(tool, params: dict[str, Any]) -> dict[str, Any]:
    """把模型常见的 OpenAI 信封误用展开回真实工具参数。

    部分推理模型（如 DeepSeek）会把 function-calling 响应里的信封字段
    ``arguments`` 直接当作参数名输出（例如 ``{"arguments": "终端"}``）。
    工具本身没有 arguments 参数时：对象值合并回参数表；字符串值映射到
    唯一必填参数，避免报出“还差一个参数：app_name”这类误导性缺参。
    """
    if "arguments" not in params or "arguments" in tool.params:
        return params
    inner = params.pop("arguments")
    if isinstance(inner, dict):
        for key, value in inner.items():
            params.setdefault(str(key), value)
        return params
    if isinstance(inner, str) and inner.strip():
        required = [name for name, pdef in tool.params.items() if pdef.is_required]
        if len(required) == 1:
            params[required[0]] = inner
    return params


def _may_change_device(tool) -> bool:
    if tool.name in _READ_ONLY_BUILTINS:
        return False
    if tool.system.get("skill") == "ubuntu_cmd":
        return False
    if tool.system.get("skill") == "wifi" and tool.name in _READ_ONLY_WIFI:
        return False
    # 知识源 Skill 全是只读：不写文件、不改设备状态，只有一次网络读
    # （kb_search）或一次本地读（file_read）。如果按「可能改设备」处理，一来
    # begin_execution 会给它套 40s 最小预算——RagFlow 检索超时口径是 60s，预算
    # 不够就直接 EdgeDeadlineExceeded，明明能查却查不了；二来只读失败后的云端
    # 兜底会被改判成「端侧可能已部分执行」，用户看到误导文案。
    if tool.system.get("skill") == "knowledge":
        return False
    return True


class Executor:
    def __init__(
        self,
        registry: ToolRegistry,
        confirm_callback: Callable[[str], bool] | None = None,
        handlers: dict[str, Callable] | None = None,
        stats: HallucinationStats | None = None,
    ):
        """
        confirm_callback: 危险操作的确认回调，返回 True 才执行。
                          None 时默认拒绝所有危险操作（安全默认）。
        handlers: 工具名 → 执行函数的分发表。None 时用内置 HANDLERS。
                  传入合并了 skill handlers 的表可支持 Skill 工具执行。
        """
        self._registry = registry
        self._confirm = confirm_callback
        self._handlers = handlers if handlers is not None else HANDLERS
        self._stats = stats if stats is not None else HallucinationStats()

    @property
    def stats(self) -> HallucinationStats:
        """参数校验统计（P0 埋点，不改变决策逻辑）。"""
        return self._stats

    def attach_stats(self, stats: HallucinationStats) -> None:
        """与路由器共享同一个统计器，钳制/拒绝事件计入同一份报告。"""
        self._stats = stats

    def execute(
        self,
        tool_name: str,
        params: dict[str, Any] | None = None,
        language: str | None = None,
    ) -> ExecResult:
        raise_if_cancelled()
        params = dict(params or {})
        language = str(
            language
            or params.pop("language", params.pop("lang", ""))
            or ""
        )

        def result(ok: bool, message: str, **kwargs: Any) -> ExecResult:
            return ExecResult(ok, localize_reply(message, language), **kwargs)
        # 规范化名字：第二段模型可能输出 short 名（vol/sysinfo）或近似名
        # （volume/sys_info），统一模糊解析成 full 名，保证 registry.get
        # 与 HANDLERS 都能命中。
        full_name = self._registry.fuzzy_resolve(tool_name)
        if full_name is None:
            return result(False, f"未知工具：{tool_name}")
        tool_name = full_name
        tool = self._registry.get(tool_name)
        # 参数信封解包要在 _prepare_params 之前：见 _normalize_envelope_params。
        params = _normalize_envelope_params(tool, params)
        if tool_name == "pkg_match_preview":
            from bibit_intent.core.package_preview import PREVIEW_MODES, PREVIEW_SCOPES
            for key, aliases in [("scope", PREVIEW_SCOPES), ("mode", PREVIEW_MODES)]:
                if isinstance(params.get(key), str):
                    params[key] = aliases.get(params[key], params[key])
        if tool_name in {"pkg_install", "pkg_remove"} and params.get("pkg"):
            target = str(params["pkg"]).strip()
            valid = (
                is_exact_package(target)
                or split_exact_packages(target) is not None
                or (tool_name == "pkg_install" and split_exact_install_specs(target) is not None)
            )
            if not valid:
                return result(False, "安装和卸载仅支持一个或多个完整准确的包名；正则、通配符需要先解析并确认清单，本次未执行操作。")

        # 知识库查询不在此处执行（由路由器交给 knowledge 模块）
        if tool.system.get("type") == "knowledge_base":
            return result(False, "知识库查询应由路由器处理，不走执行层")

        # 1. 参数补全 + 校验（缺必填参数 → 提示用户补充，而非普通失败）
        ok, filled, err, need = self._prepare_params(tool, params)
        if not ok:
            if need:
                if tool_name == "pkg_match_preview":
                    hints = {
                        "scope": "请指定预览范围：available（软件源）或 installed（已安装）。",
                        "mode": "请指定匹配方式：glob（通配符）或 regex（正则）。",
                        "pattern": "请提供完整包名匹配表达式：glob 例如 bash*，regex 例如 ^bash.*$。",
                    }
                    return result(False, hints.get(need, "请补充预览参数。") + f"可回复 {need}=值，或重新输入完整预览命令；不会安装或卸载。", need_param=need)
                label = _PARAM_CN.get(need, "必填参数")
                sample = self.param_sample(tool_name, need)
                hint = f"（{sample}）" if sample else ""
                # 多轮缺参回填：提示直接补值，会话层会记 pending（见 agent/session.py）
                return result(
                    False,
                    f"还差一个参数：{need}（{label}）。{hint}"
                    f"请直接回复{label}，或按「{need}=值」格式补充。",
                    need_param=need,
                )
            return result(False, f"参数错误：{err}")

        # 2. 危险操作二次确认（提示带上参数，避免"确认执行「安装软件」"看不见 pkg=m）
        if tool.system.get("confirm"):
            action = tool.display
            if filled:
                action += "：" + " ".join(f"{k}={v}" for k, v in filled.items())
            if self._confirm is None or not self._confirm(action):
                return result(False, f"已取消：{tool.display}（需确认）")

        # 3. 分发执行（含 skill 工具，见 skill_loader 合并的 handlers）
        handler = self._handlers.get(tool_name)
        if handler is None:
            return result(False, f"工具 {tool_name} 无执行实现")
        if _may_change_device(tool):
            begin_execution(minimum_budget_seconds=40)
        else:
            raise_if_cancelled()
        try:
            sig_params = inspect.signature(handler).parameters
            accepts_language = "language" in sig_params
        except (TypeError, ValueError):
            accepts_language = False
        if accepts_language:
            res = handler(**filled, language=language)
        else:
            res = handler(**filled)
        # 归一化返回值：skills 自带局部 ExecResult（duck-typed，无
        # need_param 字段），统一转成 tools.system.ExecResult，
        # 保证上层（router 的缺参挂起）只面对一个类。
        if not isinstance(res, ExecResult):
            res = ExecResult(
                bool(res.ok),
                str(res.message),
                str(getattr(res, "detail", "") or ""),
                getattr(res, "need_param", None),
                getattr(res, "escalate", None),
                detail_format=getattr(res, "detail_format", "markdown"),
                confirm=getattr(res, "confirm", False),
            )
        if res.escalate:
            # escalate 契约 = 本地未执行任何变更；撤回"已进入执行"标记，
            # 否则 server 边界会把云端兜底改判成 edge_execution_uncertain。
            end_execution_without_side_effect()
        res.message = localize_reply(res.message, language)
        return res

    def execute_read_bundle(
        self,
        tool_names: list[str],
        language: str | None = None,
    ) -> ExecResult:
        """执行一组只读工具并把结果聚合为 Markdown。

        仅用于本地确定性路由的内部 bundle，不注册为 MCP 工具。每个子工具
        都要求执行层判定为不会改设备状态；任一工具不满足即整体拒绝。
        """
        names = [str(n) for n in (tool_names or []) if str(n).strip()]
        if not names:
            return ExecResult(False, "只读聚合缺少工具清单")
        if len(names) > 6:
            return ExecResult(False, "只读聚合最多支持 6 个工具")
        sections: list[str] = []
        messages: list[str] = []
        for raw_name in names:
            full_name = self._registry.fuzzy_resolve(raw_name)
            if full_name is None:
                return ExecResult(False, f"只读聚合包含未知工具：{raw_name}")
            tool = self._registry.get(full_name)
            if tool is None or tool.system.get("confirm") or _may_change_device(tool):
                return ExecResult(False, f"只读聚合拒绝非只读工具：{full_name}")
            sub = self.execute(full_name, {}, language=language)
            title = tool.display or full_name
            body = render_detail(sub.detail, sub.detail_format) if sub.detail else sub.message
            section = f"## {title}\n\n{body}"
            # 按完整 section 限制展示长度，不能从中间切断代码围栏或表格。
            if len("\n\n".join([*sections, section])) > 12000:
                sections.append("…(内容过长，已截断；其余结果请单独查询)")
                break
            sections.append(section)
            messages.append(sub.message)
        detail = "\n\n".join(sections)
        return ExecResult(True, "；".join(messages) or "只读查询结果过长", detail)

    def param_label(self, tool_name: str, param_name: str) -> str:
        """返回参数的中文提示语（缺参澄清/回填提示用）。"""
        tool = self._registry.get(tool_name)
        label = _PARAM_CN.get(param_name, param_name)
        if tool and param_name in tool.params:
            pdef = tool.params[param_name]
            if pdef.type == "int" and pdef.range:
                # 用连接号而非 ~：~ 会被前端 Markdown 渲染器配对成删除线
                label = f"{label}（{pdef.range[0]}–{pdef.range[1]}）"
        return label

    def param_sample(self, tool_name: str, param_name: str) -> str:
        """
        缺参样例：工具+参数 → 参数 hint → enum → 参数名 → 类型 依次兜底。

        原来按类型统一给"如 vim"，对 mode/rate 这类参数是误导（用户看到
        "mode（如 vim）"完全无从下手）。
        """
        fixed = _PARAM_SAMPLE_BY_TOOL.get((tool_name, param_name))
        if fixed:
            return fixed
        tool = self._registry.get(tool_name)
        pdef = tool.params.get(param_name) if tool else None
        if pdef is not None and pdef.hint:
            return pdef.hint
        if pdef is not None and pdef.enum:
            return "如 " + "/".join(pdef.enum)
        fixed = _PARAM_SAMPLE.get(param_name)
        if fixed:
            return fixed
        return {"int": "如 60", "bool": "是/否", "str": "如 vim"}.get(
            pdef.type if pdef else "str", ""
        )

    def _prepare_params(
        self, tool, params: dict[str, Any]
    ) -> tuple[bool, dict[str, Any], str, str | None]:
        """
        补全默认值并校验类型/范围/枚举。
        返回 (ok, filled, err, need_param)；need_param 非空表示缺必填参数。
        """
        filled: dict[str, Any] = {}
        for pname, pdef in tool.params.items():
            if pname in params and params[pname] is not None:
                val = params[pname]
            elif pdef.default is not None:
                val = pdef.default
                self._stats.record(HallucinationRecord(
                    kind=DetectionKind.PARAMETER,
                    disposition=Disposition.EXECUTED,
                    tool=tool.name, param=pname, value=val,
                    detail="default_value",
                ))
            elif not pdef.is_required:
                continue
            else:
                return False, {}, f"缺少必填参数 {pname}", pname

            # 类型转换与校验
            try:
                if pdef.type in ("int", "float"):
                    # float：小数刷新率（59.94/29.97）直接过 float()，int 会截断成错值
                    val = float(val) if pdef.type == "float" else int(val)
                    if pdef.range and not (pdef.range[0] <= val <= pdef.range[1]):
                        # 越界则钳制，不报错（更友好）
                        self._stats.record(HallucinationRecord(
                            kind=DetectionKind.PARAMETER,
                            disposition=Disposition.CLAMPED,
                            tool=tool.name, param=pname, value=val,
                            detail=f"clamp_to_{pdef.range[0]}~{pdef.range[1]}",
                        ))
                        val = max(pdef.range[0], min(pdef.range[1], val))
                    if pdef.type == "float" and pdef.pattern \
                            and not re.fullmatch(pdef.pattern, f"{val:g}"):
                        return False, {}, f"{pname} 取值不合法：{_value_options(pdef)}", None
                elif pdef.type == "bool":
                    val = bool(val) if isinstance(val, bool) else \
                          str(val).lower() in ("true", "1", "yes", "on")
                elif pdef.type == "list":
                    if isinstance(val, str):
                        val = [v.strip() for v in re.split(r"[,，、;；\s]+", val) if v.strip()]
                    elif isinstance(val, (list, tuple)):
                        val = list(val)
                    else:
                        return False, {}, f"{pname} 应为列表", None
                    if (pdef.items or {}).get("type") == "int":
                        try:
                            val = [int(v) for v in val]
                        except (ValueError, TypeError):
                            return False, {}, f"{pname} 应为整数列表", None
                    else:
                        val = [str(v) for v in val]
                elif pdef.type == "str":
                    val = str(val)
                    if not _value_allowed(pdef, val):
                        self._stats.record(HallucinationRecord(
                            kind=DetectionKind.PARAMETER,
                            disposition=Disposition.REJECTED,
                            tool=tool.name, param=pname, value=val,
                            detail=f"not_in_enum_{pdef.enum}",
                        ))
                        # 必须返回 4 元组：execute() 按 (ok, filled, err, need)
                        # 解包，少一个会抛 ValueError（枚举越界即崩）。
                        return False, {}, f"{pname} 取值不合法：{_value_options(pdef)}", None
            except (ValueError, TypeError):
                return False, {}, f"{pname} 类型转换失败：{val!r}", None

            if (
                tool.name in {"launch_app", "close_app", "launch_apps", "close_apps"}
                and pname in {"app_name", "app_names"}
                and (not val or (isinstance(val, str) and not val.strip()))
            ):
                return False, {}, f"缺少必填参数 {pname}", pname
            filled[pname] = val
        return True, filled, "", None
