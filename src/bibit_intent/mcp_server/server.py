"""MCP stdio adapter for the existing Registry/Executor stack.

This module is intentionally isolated: importing it does not alter the normal
Router/Pipeline behavior.  The MCP surface delegates every call to Executor.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import inspect
import re
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from bibit_intent.core.progress import progress_bridge
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution import progress as task_progress
from bibit_intent.execution.executor import Executor
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.execution.system_tools import ExecResult, HANDLERS, split_app_names
from bibit_intent.resources import resource_path


def _unwrap_envelope(tool_params, arguments):
    """把 OpenAI 信封误用展开回工具真实参数（供 MCP call_tool 前置）。

    部分推理模型（如 DeepSeek）会把 function-calling 响应里的信封字段
    ``arguments`` 直接当作参数名输出（例如 ``{"arguments": "终端"}``），
    而工具 schema 里并没有叫 ``arguments`` 的属性。这里在 FastMCP 校验
    之前把信封拆回真实参数：dict 值直接合并；字符串值映射到唯一的必填
    参数，若该参数是 list 类型则按分隔符拆成列表（launch_apps 场景）。
    """
    args = dict(arguments or {})
    if "arguments" not in args:
        return args
    if "arguments" in tool_params:
        return args
    inner = args.pop("arguments")
    if isinstance(inner, Mapping):
        for key, value in inner.items():
            args.setdefault(str(key), value)
        return args
    if isinstance(inner, str) and inner.strip():
        # 唯一必填参数优先；没有必填参数时退到第一个声明的参数（YAML 顺序里
        # 主参数在首位，如 bt_connect 的 device / wifi_connect 的 ssid）。
        # 少了这个退路，把 device/ssid 声明成 required: false 之后，
        # {"arguments": "LOGI M240 B"} 这种信封会直接把值丢掉。
        targets = [
            name for name, pdef in tool_params.items() if pdef.is_required
        ]
        if not targets and tool_params:
            first = next(iter(tool_params))
            if tool_params[first].type == "str":
                targets = [first]
        if len(targets) == 1:
            name = targets[0]
            pdef = tool_params[name]
            if pdef.type == "list":
                items_type = (pdef.items or {}).get("type", "str")
                parts = split_app_names(inner)
                if items_type == "int":
                    converted: list[Any] = []
                    for part in parts:
                        try:
                            converted.append(int(part))
                        except ValueError:
                            converted.append(part)
                    args[name] = converted
                else:
                    args[name] = parts
            else:
                args[name] = inner
    return args


def mcp_confirmation(action: str) -> bool:
    """MCP 路径上的「需确认」判定：交由 Runtime 的审批承担。

    每个 ``confirm: true`` 的工具都会在 ``_tool_annotations`` 里带上
    ``destructiveHint``，Runtime 把它映射成 ``requires_approval``，执行前用
    HITL 弹审批；用户批准之后才会调到这里。MCP 没有把「已批准」回传的通道，
    所以这里再拒一次就是一道**永远无法满足**的闸门：``appcli_run``、关机、
    休眠等需要确认的工具会一律返回「已取消：…（需确认）」，云端路径永远产不出
    产物（K3/10.0.91.72 实测，见 docs/tools/appcli.md）。

    因此这里只回 True：审批的**唯一**权威是 Runtime 的注解驱动审批。为了让这个
    前提可验证，``tests/test_appcli_mcp_approval.py`` 固定「所有 confirm 工具都
    必须带 destructive 注解」，注解丢了测试就会失败。
    """
    del action
    return True


def _build_executor() -> tuple[ToolRegistry, Executor]:
    registry = ToolRegistry(resource_path("tools.yaml"))
    handlers = dict(HANDLERS)
    load_skills(
        resource_path("skills"),
        registry,
        handlers,
        groups=_allowed_groups(),
    )
    return registry, Executor(
        registry,
        handlers=handlers,
        confirm_callback=mcp_confirmation,
    )


def _tool_annotations(tool):
    """MCP ToolAnnotations：只读/破坏性提示，供 Runtime 审批策略使用。

    mcp 1.x 的字段是驼峰（readOnlyHint），2.x 改成蛇形（read_only_hint）；
    端侧固定 1.19，开发 venv 可能装到 2.0，这里两种都试一遍，失败就不带注解。
    """
    try:
        from mcp.types import ToolAnnotations
    except ImportError:  # pragma: no cover - mcp 未安装时不影响工具注册
        return None
    read_only = tool.mcp_group().casefold() == "readonly"
    destructive = bool(tool.system.get("confirm"))
    for kwargs in (
        {"readOnlyHint": read_only, "destructiveHint": destructive, "idempotentHint": False},
        {"read_only_hint": read_only, "destructive_hint": destructive, "idempotent_hint": False},
    ):
        try:
            return ToolAnnotations(**kwargs)
        except Exception:
            continue
    return None


# 注入到工具签名里的 Context 形参名。工具参数里不会出现这个名字；FastMCP 靠
# 注解类型（而不是名字）识别注入项，并把它排除在 inputSchema 之外。
_CONTEXT_PARAM = "mcp_ctx"


def _progress_context_type() -> type | None:
    """返回与 create_server 所用 SDK 同源的 Context 类型，取不到返回 None。

    MCP 1.x 把 FastMCP/Context 放在 ``mcp.server.fastmcp``，2.x 改名成
    ``mcp.server.mcpserver``。两者必须成对取用：Context 的注入识别是
    ``issubclass(annotation, Context)``，混用不同版本的类会识别失败。
    """
    for module_name in ("mcp.server.fastmcp", "mcp.server.mcpserver"):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        context = getattr(module, "Context", None)
        server = getattr(module, "FastMCP", None) or getattr(module, "MCPServer", None)
        if isinstance(context, type) and server is not None:
            return context
    return None


def _context_emitter(report) -> Callable[[float, float | None, str | None], Awaitable[None]]:
    """把 MCP Context.report_progress 适配成进度桥的 emitter。"""

    async def emit(progress: float, total: float | None, message: str | None) -> None:
        await report(progress, total, message)

    return emit


def _tool_payload_failed(result) -> bool:
    """工具结果信封里的 ``ok`` 是不是 False（取不到就当没失败）。"""

    blocks = (
        result
        if isinstance(result, (list, tuple))
        else getattr(result, "content", None)
    )
    if not isinstance(blocks, (list, tuple)):
        return False
    for block in blocks:
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            return False
        return isinstance(payload, dict) and payload.get("ok") is False
    return False


def _file_search_ui_result(result):
    """正文仅放 MCP 私有展示元数据，普通工具内容不得包含文件片段。"""
    from mcp.types import CallToolResult, TextContent

    blocks = getattr(result, "content", None) or result
    if isinstance(blocks, tuple) and blocks and isinstance(blocks[0], list):
        blocks = blocks[0]
    private = None
    try:
        payload = json.loads(blocks[0].text)
        detail = json.loads(payload.get("detail") or "null")
        if payload.get("ok") is True and isinstance(detail, dict) and detail.get("schema") == "bibit.file-results.v1":
            safe = {**detail, "entries": [
                {**entry, "snippet": None, "snippet_source": None}
                for entry in detail["entries"]
            ]}
            private = detail
            payload = {**payload, "detail": json.dumps(safe, ensure_ascii=False)}
        elif payload.get("ok") is not False:
            raise ValueError("unknown file search result")
        else:
            payload = {**payload, "detail": ""}
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        payload = {"ok": False, "message": "文件检索结果无效，请重新搜索。", "detail": ""}
    fields = CallToolResult.model_fields
    return CallToolResult(**{
        "content": [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        "isError" if "isError" in fields else "is_error": payload.get("ok") is not True,
        "_meta": {"bibit.file-results": private} if private is not None else None,
    })


def _mark_tool_failure(result):
    """把 ``ok: false`` 的工具结果标成 MCP ``isError``。

    ``ok`` 是执行层自己的结论，``isError`` 才是 MCP 协议层的「这次调用没成功」。
    以前只有前者，Runtime 看到的永远是「调用成功」：after_tool Hook 的
    「独立复核未通过」会和工具自己的「参数错误」拼在一起，模型把两件事读成因果
    （K3 实测，见 docs/tools/appcli.md §10.2）。标上以后 Runtime 才能把失败调用
    排除在事后复核之外。

    内容保持不变（协议允许 isError 带 content），模型看到的 JSON 一字不改。
    1.x 与 2.x 的字段名不同（``isError`` / ``is_error``），按模型字段挑一个；
    构造失败或 SDK 缺失时原样返回，绝不因为标记失败而改变工具可用性。
    """

    if not _tool_payload_failed(result):
        return result
    try:
        from mcp.types import CallToolResult
    except ImportError:  # pragma: no cover - mcp 未安装时不影响工具注册
        return result
    fields = getattr(CallToolResult, "model_fields", None) or {}
    payload: dict[str, Any] = {
        "content": list(getattr(result, "content", None) or result),
        "isError" if "isError" in fields else "is_error": True,
    }
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if structured is not None:
        payload[
            "structuredContent" if "structuredContent" in fields else "structured_content"
        ] = structured
    try:
        return CallToolResult(**payload)
    except Exception:  # noqa: BLE001 - 标记失败不影响工具调用本身
        return result


class _AsyncTaskRegistry:
    """MCP 进程内的后台任务表。

    仅给 ``mcp.async: true`` 的耗时工具使用：调用时立即返回 task_id，执行体
    在 asyncio task 中继续跑，客户端通过 ``bibit_task_status`` 轮询结果。
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._meta: dict[str, tuple[str, float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def start(
        self,
        executor: Executor,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        lock_key: str = "default",
    ) -> str:
        task_id = uuid.uuid4().hex
        task = asyncio.create_task(
            self._execute(executor, tool_name, arguments, lock_key, task_id)
        )
        self._tasks[task_id] = task
        self._meta[task_id] = (tool_name, time.monotonic())
        self._prune()
        return task_id

    def status(self, task_id: str) -> dict[str, Any]:
        task = self._tasks.get(task_id)
        if task is None:
            return {
                "ok": False,
                "task_id": task_id,
                "status": "unknown",
                "message": "未知任务 ID",
            }
        if not task.done():
            tool_name, _ = self._meta[task_id]
            progress = task_progress.snapshot(task_id)
            return {
                "ok": True,
                "task_id": task_id,
                "status": "running",
                "tool": tool_name,
                "message": "任务仍在执行",
                "progress": (
                    {
                        "percent": progress.percent,
                        "message": progress.message,
                        "phase": progress.phase,
                    }
                    if progress is not None
                    else {"percent": None, "message": "任务仍在执行", "phase": "running"}
                ),
            }
        try:
            result = task.result()
        except asyncio.CancelledError:
            return {
                "ok": False,
                "task_id": task_id,
                "status": "cancelled",
                "message": "任务已取消",
            }
        except Exception as exc:  # noqa: BLE001 - 轮询结果不能因任务异常而中断
            return {
                "ok": False,
                "task_id": task_id,
                "status": "failed",
                "message": f"任务异常：{type(exc).__name__}: {exc}",
            }
        return {
            "task_id": task_id,
            "status": "completed",
            "ok": bool(result.ok),
            "message": str(result.message),
            "detail": str(result.detail or ""),
            "need_param": result.need_param,
            "escalate": result.escalate,
            "progress": {
                "percent": 100 if result.ok else None,
                "message": str(result.message),
                "phase": "done" if result.ok else "failed",
            },
        }

    async def _execute(
        self,
        executor: Executor,
        tool_name: str,
        arguments: dict[str, Any],
        lock_key: str,
        task_id: str,
    ) -> ExecResult:
        """锁覆盖整个执行线程；取消等待不能提前放行同资源的后续命令。"""
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            try:
                def run_with_progress():
                    task_progress.set_current_task(task_id)
                    try:
                        return executor.execute(tool_name, arguments)
                    finally:
                        task_progress.clear_current_task()

                worker = asyncio.create_task(asyncio.to_thread(run_with_progress))
                cancelled = False
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        cancelled = True
                if cancelled:
                    raise asyncio.CancelledError
                return worker.result()
            except Exception as exc:  # noqa: BLE001 - 保持任务可查询，不让异常挂起状态
                return ExecResult(
                    False,
                    f"异步任务异常：{type(exc).__name__}: {exc}",
                    "",
                )

    def _prune(self) -> None:
        """丢弃已完成且超过 1 小时的任务，避免长时间运行的 MCP 服务无限增长。"""
        cutoff = time.monotonic() - 3600
        stale = [
            task_id
            for task_id, (_, started_at) in self._meta.items()
            if started_at < cutoff and self._tasks[task_id].done()
        ]
        for task_id in stale:
            self._tasks.pop(task_id, None)
            self._meta.pop(task_id, None)
            task_progress.forget(task_id)


_APT_ASYNC_TOOLS = frozenset({
    "pkg_install",
    "pkg_remove",
    "pkg_update",
    "pkg_clean",
    "pkg_autoremove",
    "pkg_restore_selections",
    "dep_apply",
    "install_local",
})
_PIP_ASYNC_TOOLS = frozenset({"pip_install", "pip_uninstall"})
_NPM_ASYNC_TOOLS = frozenset({"npm_install", "npm_uninstall"})


def _async_lock_key(tool) -> str:
    """耗时工具按底层包管理器串行，避免 apt/dpkg 锁或固定 askpass 文件竞争。"""
    if tool.name in _APT_ASYNC_TOOLS:
        return "apt"
    if tool.name in _PIP_ASYNC_TOOLS:
        return "pip"
    if tool.name in _NPM_ASYNC_TOOLS:
        return "npm"
    return f"tool:{tool.name}"


def create_server():
    """Create a FastMCP server with all registered tools."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - exercised in packaging env
        raise RuntimeError("MCP server requires the 'mcp' package") from exc

    class _EnvelopeAwareMCP(FastMCP):
        """tools/call 进入 schema 校验前先做 OpenAI 信封解包。"""

        def __init__(self, *args, params_by_tool=None, **kwargs):
            super().__init__(*args, **kwargs)
            self._params_by_tool = params_by_tool or {}

        async def call_tool(self, name, arguments):
            params = self._params_by_tool.get(name) or {}
            result = await super().call_tool(name, _unwrap_envelope(params, arguments))
            if name == "file_search":
                return _file_search_ui_result(result)
            return _mark_tool_failure(result)

    registry, executor = _build_executor()
    tools = list(_tools_for_groups(registry, _allowed_groups()))
    # 取不到 Context（SDK 缺失或版本不提供注入）时不加形参，进度自动关闭，
    # 工具行为与接入进度前完全一致。
    context_type = _progress_context_type()
    mcp = _EnvelopeAwareMCP(
        "bibit-intent",
        params_by_tool={tool.name: tool.params for tool in tools},
    )
    async_registry = _AsyncTaskRegistry()
    for tool in tools:
        schema = tool.mcp_schema()["function"]
        description = schema.get("description", tool.name)

        def make_invoke(
            tool_name: str,
            tool_description: str,
            has_params: bool,
            progress_enabled: bool = False,
            async_enabled: bool = False,
            lock_key: str = "default",
        ):
            async def invoke(**arguments: Any) -> str:
                # 注入的 Context 只用于上报进度，绝不能进 handler 参数
                context = arguments.pop(_CONTEXT_PARAM, None) if progress_enabled else None
                report = getattr(context, "report_progress", None)
                emitter = _context_emitter(report) if callable(report) else None
                async with progress_bridge(emitter):
                    if async_enabled:
                        task_id = async_registry.start(
                            executor,
                            tool_name,
                            arguments,
                            lock_key=lock_key,
                        )
                        payload = {
                            "ok": True,
                            "message": (
                                "Task started. Call bibit_task_status(task_id) "
                                f"with task_id={task_id} to poll for completion."
                            ),
                            "detail": "",
                            "need_param": False,
                            "escalate": None,
                            "task_id": task_id,
                            "status": "running",
                        }
                        return json.dumps(payload, ensure_ascii=False)
                    result = await async_registry._execute(
                        executor, tool_name, arguments, lock_key, uuid.uuid4().hex
                    )
                payload = {
                    "ok": bool(result.ok),
                    "message": str(result.message),
                    "detail": str(result.detail or ""),
                    "need_param": result.need_param,
                    "escalate": result.escalate,
                }
                if result.confirmation is not None:
                    payload["confirmation"] = result.confirmation
                return json.dumps(payload, ensure_ascii=False)
            invoke.__name__ = tool_name
            invoke.__doc__ = tool_description
            invoke.__annotations__ = {"return": str}
            return invoke

        # FastMCP's decorator registers each callable as one MCP tool.
        parameters, progress_enabled = _invoke_parameters(tool, context_type)
        invoke = make_invoke(
            tool.name,
            description,
            bool(tool.params),
            progress_enabled,
            async_enabled=tool.mcp_async(),
            lock_key=_async_lock_key(tool),
        )
        # FastMCP derives inputSchema from the callable signature.  Replace the
        # generic **kwargs signature with the registry's concrete parameters so
        # no-argument tools (e.g. sys_uptime) accept an empty object, defaulted
        # params stay optional, and types reach the model instead of "Any".
        invoke.__signature__ = inspect.Signature(parameters)
        if progress_enabled:
            invoke.__annotations__[_CONTEXT_PARAM] = context_type
        annotations = _tool_annotations(tool)
        try:
            mcp.tool(name=tool.name, description=description, annotations=annotations)(invoke)
        except TypeError:  # 老版本 SDK 不认识 annotations 参数
            mcp.tool(name=tool.name, description=description)(invoke)

    if any(tool.mcp_async() for tool in tools):
        @mcp.tool(
            name="bibit_task_status",
            description=(
                "Poll a long-running bibit-intent task. Use this after an "
                "async tool returns a task_id. Returns status and, when done, "
                "the tool result."
            ),
        )
        async def bibit_task_status(task_id: str) -> str:
            return json.dumps(async_registry.status(task_id), ensure_ascii=False)

    return mcp


_PARAM_ANNOTATIONS = {"str": str, "int": int, "float": float, "bool": bool, "list": list}


def _parameter_specs(tool):
    """把 ToolDef 参数转成 inspect.Parameter 列表（含类型与默认值）。

    FastMCP 会把 ``inspect.Parameter.empty`` 当成必填参数写进 inputSchema，
    因此 ``required: false`` 但未声明 default 的参数（例如 set_volume 的
    direction）不能继续用 empty，否则模型只传 level 会被校验器拒绝。
    """
    parameters = []
    for pname, pdef in tool.params.items():
        is_required = getattr(pdef, "is_required", None)
        if is_required is None:
            is_required = pdef.default is None
        if is_required:
            default = inspect.Parameter.empty
        elif pdef.default is not None:
            default = pdef.default
        else:
            default = None
        parameters.append(inspect.Parameter(
            pname,
            inspect.Parameter.KEYWORD_ONLY,
            annotation=_PARAM_ANNOTATIONS.get(pdef.type, Any),
            default=default,
        ))
    return parameters


def _invoke_parameters(tool, context_type):
    """工具的可调用签名参数，以及是否注入 Context。

    FastMCP 从签名推导 inputSchema，并且只把带 Context 注解的形参当作注入项、
    从 schema 里排除。工具自己若声明了同名参数，就放弃这条工具的进度注入，
    而不是静默覆盖它的参数。
    """
    parameters = list(_parameter_specs(tool))
    progress_enabled = context_type is not None and _CONTEXT_PARAM not in tool.params
    if progress_enabled:
        parameters.append(inspect.Parameter(
            _CONTEXT_PARAM,
            inspect.Parameter.KEYWORD_ONLY,
            annotation=context_type,
        ))
    return parameters, progress_enabled


def _allowed_groups() -> set[str] | None:
    """BIBIT_MCP_GROUPS=readonly,control 时只暴露指定分组；未设置则全部。"""
    raw = os.environ.get("BIBIT_MCP_GROUPS", "").strip()
    if not raw:
        return None
    return {g.strip().casefold() for g in raw.split(",") if g.strip()}


def _tools_for_groups(registry, allowed: set[str] | None):
    if allowed is None:
        return list(registry.all())
    return [t for t in registry.all() if t.mcp_group().casefold() in allowed]


def main() -> None:
    from bibit_intent.system_update_recovery import report_pending_system_update

    # 自研组件升级会重启 Runtime 并切断当前会话；恢复后由最先抢到锁的 worker
    # 弹最终结果框。放守护线程，避免阻塞 MCP 启动或让其它 worker 卡住。
    threading.Thread(target=report_pending_system_update, daemon=True).start()
    create_server().run(transport=os.environ.get("BIBIT_MCP_TRANSPORT", "stdio"))


if __name__ == "__main__":
    main()
