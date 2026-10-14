"""appcli Skill（应用命令行）的执行实现。

三个工具都以「注册表为唯一事实来源、绝不抛异常给调用方」为口径：

- ``appcli_list``（H02，只读）：这台设备上哪些应用可用、缺什么、验收状态；
- ``appcli_run``（H03/H04/H06，需确认）：按登记参数渲染 argv 执行，以产物为准判定成功，
  并把每次调用（含被拒的）落一条审计、累计指标；
- ``appcli_verify``（H05，只读）：不执行命令，只对已有产物逐条跑校验规则并给证据。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from bibit_intent.core import appcli_audit
from bibit_intent.core.dependency_plan import channel_label
from bibit_intent.core.appcli_artifacts import verify_artifact
from bibit_intent.core.appcli_execution import (
    DEFAULT_TIMEOUT_SECONDS,
    AppCliPathError,
    channel_allowed,
    appcli_root,
    resolve_in_workspace,
)
from bibit_intent.core.appcli_prereq import check_readiness
from bibit_intent.core.appcli_wheels import source_note
from bibit_intent.core.appcli_registry import AppCliRegistry, AppCliRegistryError
from bibit_intent.core.progress import report_progress
from bibit_intent.execution.bounded_command import (
    OutputLimitExceeded,
    run_bounded_command,
)
from bibit_intent.execution.cancellation import EdgeExecutionCancelled

#: args 字符串形式的分隔符（执行层把字符串转 list 时用的是同一组）。
_ARGS_SEPARATORS = re.compile(r"[,，、;；\s]+")

#: 单次执行的输出与详情上限，避免把大段渲染日志灌进会话。
_MAX_OUTPUT_BYTES = 8_000_000
_MAX_DETAIL_CHARS = 2_000


@dataclass
class ExecResult:
    """与 tools.system.ExecResult 字段对齐（执行层会做归一化）。"""

    ok: bool
    message: str
    detail: str = ""
    need_param: str | None = None
    escalate: str | None = None


def _reply(zh: str, en: str, language: str | None = None) -> str:
    return en if str(language or "").lower().startswith("en") else zh


def appcli_list(category: str = "", language: str | None = None, **_) -> ExecResult:
    """列出已登记的应用及其前置齐备度、验收状态、通道与动作。只读。

    category 非空时按注册表里的 category 精确过滤。
    """
    try:
        registry = AppCliRegistry.load()
    except AppCliRegistryError as exc:
        return ExecResult(
            False,
            _reply(
                f"应用注册表不可用：{exc}",
                f"AppCli registry is unavailable: {exc}",
                language,
            ),
        )

    wanted = (category or "").strip()
    specs = [spec for spec in registry.specs() if not wanted or spec.category == wanted]
    if not specs:
        return ExecResult(
            True,
            _reply(
                f"没有匹配 category={wanted} 的应用" if wanted else "没有登记任何应用",
                f"No app matches category={wanted}" if wanted else "No app is registered",
                language,
            ),
        )

    lines: list[str] = []
    ready_count = 0
    for spec in specs:
        readiness = check_readiness(spec)
        ready_count += int(readiness.ready)
        lines.append(
            f"{spec.name} | {spec.category or '-'} | channel={spec.channel} | "
            f"riscv64={spec.riscv64} | {readiness.status}"
        )
        lines.append(f"  能力: {spec.display}")
        lines.append(f"  动作: {_describe_actions(spec)}")
        missing = readiness.missing()
        if missing:
            lines.append(f"  缺少: {', '.join(missing)}")

    return ExecResult(
        True,
        _reply(
            f"已登记应用 {len(specs)} 个（{ready_count} 个前置齐备，"
            f"{len(registry.verified())} 个已通过 riscv64 验收）：",
            f"{len(specs)} apps registered ({ready_count} ready, "
            f"{len(registry.verified())} riscv64-verified):",
            language,
        ),
        "\n".join(lines),
    )


def _describe_actions(spec) -> str:
    """动作清单；需要审批的动作加 ``*``，只读动作单独列出。"""
    confirm = [a.name for a in spec.actions if not a.readonly]
    readonly = [a.name for a in spec.actions if a.readonly]
    parts: list[str] = []
    if confirm:
        parts.append(", ".join(f"{name}*" for name in confirm))
    if readonly:
        parts.append(f"只读: {', '.join(readonly)}")
    return "；".join(parts) if parts else "(无)"


def _parse_args(raw: object) -> tuple[dict[str, str], str]:
    """把 ``["project=x.json", "output=out.pdf"]`` 解析成键值字典。

    只接受 ``key=value`` 形式（第一个 ``=`` 处切分）：键必须非空且不重复。
    值里允许出现 ``=``；不允许空值——空值按缺失处理，交给注册表的必填校验。

    另外容错 ``[{"key": "text", "value": "…"}]`` 这种「把参数表当结构化对象」的形态：
    K3 真实会话里模型第一次就是这么传的（``args`` 列表里放字典），原本会被判成
    参数错误、白费一轮往返。
    """
    if raw is None or raw == "" or raw == []:
        return {}, ""
    if isinstance(raw, str):
        parts = [item for item in _ARGS_SEPARATORS.split(raw) if item]
    elif isinstance(raw, (list, tuple)):
        parts = []
        for item in raw:
            if isinstance(item, Mapping) and "key" in item:
                parts.append(f"{item['key']}={item.get('value', '')}")
            else:
                parts.append(str(item))
    else:
        return {}, "args 应为 key=value 列表"

    parsed: dict[str, str] = {}
    for item in parts:
        key, separator, value = item.partition("=")
        key = key.strip()
        if not separator or not key:
            return {}, f"args 项 {item!r} 不是 key=value 形式"
        if key in parsed:
            return {}, f"args 里 {key!r} 重复"
        parsed[key] = value.strip()
    return parsed, ""


def _resolve_path_args(spec, action_name: str, values: dict[str, str]) -> tuple[dict[str, str], str]:
    """把标了 path 的参数解析成工作区内的绝对路径；返回 (值, 错误)。"""
    resolved = dict(values)
    for key, param in spec.action(action_name).params.items():
        if not param.path or key not in resolved:
            continue
        try:
            resolved[key] = str(resolve_in_workspace(resolved[key]))
        except AppCliPathError as exc:
            return {}, str(exc)
    return resolved, ""


def _has_subcommand(argv: list[str]) -> bool:
    """argv 里必须有非选项 token（子命令）。

    没有子命令时部分应用会进入 REPL（drawio/libreoffice 实测），那会挂住会话，
    因此宁可判为参数错误，也不允许这种调用发出。
    """
    return any(not token.startswith("-") for token in argv)


def _json_summary(stdout: str, *, allow_list: bool = False) -> tuple[object | None, str]:
    """强制 ``--json`` 的解析：整段必须是 JSON，不猜、不从噪声里抠。

    ``allow_list`` 只给**只读**动作开：上游的列举类只读命令（``writer list``、
    ``calc list-sheets``、``impress list-slides``、``style list``）顶层就是数组，
    要求对象会把它们一律判成「输出异常」（K3 实测）。写动作仍然只认对象信封——
    ``ok`` 之类的信封字段只在对象里。
    """
    text = (stdout or "").strip()
    if not text:
        return None, "空输出"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"{exc.msg}（第 {exc.lineno} 行）"
    if isinstance(payload, dict):
        return payload, ""
    if allow_list and isinstance(payload, list):
        return payload, ""
    return None, f"JSON 顶层不是对象（{type(payload).__name__}）"


def _merge_path_aliases(
    spec, action: str, values: dict[str, str], project: str, output: str
) -> tuple[dict[str, str], str]:
    """把 ``project``/``output`` 快捷参数并入 args；返回 (值, 错误)。

    规则层要用正则命名组接住用户说的文件路径，而 ``args`` 是列表参数抽不了，
    所以给登记表里约定俗成的两个路径参数各开一个同名入口（两者都指向工作区内的
    路径）。动作没声明该参数时明确报错，不静默忽略；同一个参数同时从 args 和
    快捷入口给值也算冲突——两条路都通，就必须有一处说了算，否则谁覆盖谁靠巧合。
    """
    declared = spec.action(action).params
    merged = dict(values)
    for key, raw in (("project", project), ("output", output)):
        text = str(raw or "").strip()
        if not text:
            continue
        if key not in declared:
            known = ", ".join(sorted(declared)) or "(无)"
            return {}, f"{spec.name}/{action} 没有参数 {key!r}；可用参数：{known}"
        if key in merged:
            return {}, f"参数 {key!r} 不能同时用 args 与快捷参数传"
        merged[key] = text
    return merged, ""


def _effective_timeout(spec, timeout: object) -> int:
    """解析本次执行的超时：调用方没给（0/None/非法）就用注册表里该应用的值。

    注册表默认值（`timeout_seconds`，渲染类 300s）是「这类动作通常要多久」的知识；
    工具参数显式给出时以调用方为准，因为它更清楚这一次要渲染什么。
    """
    try:
        requested = int(timeout)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        requested = 0
    if requested <= 0:
        requested = int(getattr(spec, "timeout_seconds", 0) or DEFAULT_TIMEOUT_SECONDS)
    return max(1, requested)


def _interrupted_detail(spec, action: str, values: dict, shown: str, note: str) -> str:
    """超时/取消后的详情：命令行 + 说明 + 该动作声明的产物现状。

    进程组已被有界停止（见 ``execution/bounded_command.py`` 的 finally），这里只如实
    报告文件层面的现状：写出来了就给出路径与校验结论（通常是「尾部没有 %%EOF」这类
    截断证据），没写出来就说没写出来。**不自动重放**：半成品加上重放会得到两份互相
    覆盖的文件，由用户决定下一步。
    """
    lines = [shown, note]
    rules = spec.artifact_rules(action)
    if not rules:
        lines.append("该动作未声明产物，没有需要保留的文件。")
    for rule in rules:
        verdict = verify_artifact(Path(values[rule.param]), rule.verify)
        lines.append(f"产物现状（未判定成功）：{verdict.summary()}")
    return _truncate("\n".join(lines), _MAX_DETAIL_CHARS)


def _prereq_reply(spec, readiness, language: str | None) -> str:
    """缺前置时的回复：缺什么 + 各自走哪条渠道 + 依赖计划提示（H07）。

    与 `dep_plan` 共用渠道口径（`core.dependency_plan.channel_label`）：apt 项需 sudo、
    pip 项装进用户 venv——提示里的说法必须和用户真去跑计划时看到的一致，否则用户会以为
    可以自己 `pip install` 到系统环境。

    应用自己的 wheel（`cli-anything-*`）额外标出来源（`core.appcli_wheels.source_note`）：
    上游的官方装法是 `pip install git+github`，端侧不允许这种来源，用户必须看到「内部源」
    或设备上的离线 wheel 目录，才不会去照着上游 README 拉源码。

    这里**不**安装任何东西，也**不**转交云端：缺前置是可修的本地状态，安装要走
    `dep_plan`（只读干跑）→ 用户确认 → `dep_apply` 的既有审批链路。
    """
    zh = [f"缺少前置，本次未执行：{spec.name}"]
    en = [f"Missing prerequisites, nothing was executed: {spec.name}"]
    for name in readiness.missing_apt:
        zh.append(f"  - 系统包 {name} → {channel_label('apt')}")
        en.append(f"  - system package {name} -> {channel_label('apt')}")
    for name in readiness.missing_python:
        source = source_note(name)
        suffix = f"（{source}）" if source else ""
        zh.append(f"  - python 包 {name} → {channel_label('pip')}{suffix}")
        en.append(
            f"  - python package {name} -> {channel_label('pip')}"
            + (f" ({source})" if source else "")
        )
    if not readiness.entry_point_found:
        zh.append(f"  - 入口命令 {spec.entry_point} 不在 PATH（随上面的包一起装）")
        en.append(
            f"  - entry point {spec.entry_point} is not on PATH (installed with the package above)"
        )
    zh.append(
        "本工具不会自动安装：先用「生成依赖计划 <工程目录>」（dep_plan，只读干跑）看来源、"
        "体积与风险，确认计划后再 dep_apply。"
    )
    en.append(
        'This tool never installs anything: build a plan first with "dependency plan '
        '<project root>" (dep_plan, a read-only dry run) to see sources, sizes and risks, '
        "then confirm it with dep_apply."
    )
    return _reply("\n".join(zh), "\n".join(en), language)


def _refused(result: ExecResult, reason: str) -> tuple[ExecResult, dict]:
    """未执行就被拒：审计里 stage=refused，不计入 runs。"""
    return result, {"stage": "refused", "reason": reason, "ok": False}


def _command_env(spec) -> dict[str, str] | None:
    """应用声明的环境变量叠在继承环境之上；没声明就返回 None（保持原行为）。

    设备上实测的必要性：headless 的 riscv64 设备没有 DISPLAY，LibreOffice 默认去初始化
    Qt xcb 插件，`--headless` 也照旧 —— 直接 exit 134、一个产物都不写。注册表里的
    `env: {SAL_USE_VCLPLUGIN: svp}` 让它走无头 VCL，同一台设备立刻产出真 PDF。
    """
    if not spec.env:
        return None
    env = dict(os.environ)
    env.update(spec.env)
    return env


def _executed(
    result: ExecResult,
    reason: str,
    *,
    command: str,
    outputs: list[dict] | None = None,
    verify_failed: int = 0,
    artifacts: int = 0,
) -> tuple[ExecResult, dict]:
    """已发起命令：审计里 stage=executed，计入 runs。"""
    return result, {
        "stage": "executed",
        "reason": reason,
        "ok": result.ok,
        "command": command,
        "outputs": list(outputs or []),
        "verify_failed": verify_failed,
        "artifacts": artifacts,
    }


def _verdict_records(verdicts) -> list[dict]:
    """把产物校验结果压成审计字段（路径 + 逐条规则结论与证据）。"""
    return [
        {
            "path": str(verdict.path),
            "ok": verdict.ok,
            "rules": [
                {"rule": item.rule, "ok": item.ok, "evidence": item.evidence}
                for item in verdict.results
            ],
        }
        for verdict in verdicts
    ]


def appcli_run(
    appcli: str = "",
    action: str = "",
    args: object = None,
    project: str = "",
    output: str = "",
    timeout: int = 0,
    language: str | None = None,
    **_,
) -> ExecResult:
    """执行一个已登记且已通过设备验收的应用动作。变更类动作需确认。

    args 用 ``key=value`` 列表传参（键必须在该动作的 params 里登记，不接受自由 argv）。
    ``project``/``output`` 是路径快捷参数（等价于对应的 args 项，便于规则层用命名组捕获）。
    ``timeout`` 传 0 表示用注册表里该应用的 ``timeout_seconds``（渲染类默认 300s）。

    调用**一定**会留下一条审计记录（H06）：被白名单/前置/通道/参数拒的记
    ``stage=refused``，真正发起命令的记 ``stage=executed``，两者在指标里分开计数；
    审计写失败只丢审计，不改变这里返回的结果。
    """
    started = time.monotonic()
    result, info = _run_appcli_action(appcli, action, args, project, output, timeout, language)
    appcli_audit.record_run(
        appcli=(appcli or "").strip(),
        target_action=(action or "").strip(),
        duration_ms=int((time.monotonic() - started) * 1000),
        **info,
    )
    return result


def _run_appcli_action(
    appcli: str,
    action: str,
    args: object,
    project: str,
    output: str,
    timeout: int,
    language: str | None,
) -> tuple[ExecResult, dict]:
    """appcli_run 的执行主体：只负责做事并给出（结果，审计信息）。"""
    values, error = _parse_args(args)
    if error:
        return _refused(
            ExecResult(False, _reply(f"参数错误：{error}", f"Invalid parameter: {error}", language)),
            "invalid_args",
        )

    try:
        registry = AppCliRegistry.load()
        spec = registry.require_verified(appcli)
        spec.action(action)
    except AppCliRegistryError as exc:
        return _refused(
            ExecResult(False, _reply(f"无法执行：{exc}", f"Cannot run: {exc}", language)),
            getattr(exc, "code", "registry_error"),
        )

    values, error = _merge_path_aliases(spec, action, values, project, output)
    if error:
        return _refused(
            ExecResult(False, _reply(f"参数错误：{error}", f"Invalid parameter: {error}", language)),
            "invalid_args",
        )

    readiness = check_readiness(spec)
    if not readiness.ready:
        return _refused(ExecResult(False, _prereq_reply(spec, readiness, language)), "missing_prereq")

    if not channel_allowed(spec.channel):
        return _refused(
            ExecResult(
                False,
                _reply(
                    f"{spec.name} 需要在宿主桌面执行（channel=host），当前在沙箱内，本次未执行，"
                    f"已转交宿主处理。",
                    f"{spec.name} must run on the host desktop (channel=host); this session is "
                    f"sandboxed, so nothing was executed and the request was handed to the host.",
                    language,
                ),
                escalate="needs_host_channel",
            ),
            "needs_host_channel",
        )

    try:
        # 先解析出最终值（补默认值 + 校验），再改写路径，再渲染：
        # 产物校验要按同一份值定位文件，不能各算一次。
        values = spec.resolve_values(action, values)
    except AppCliRegistryError as exc:
        return _refused(
            ExecResult(False, _reply(f"参数错误：{exc}", f"Invalid parameter: {exc}", language)),
            getattr(exc, "code", "invalid_args"),
        )

    values, error = _resolve_path_args(spec, action, values)
    if error:
        return _refused(
            ExecResult(
                False,
                _reply(f"参数错误：{error}", f"Invalid parameter: {error}", language),
            ),
            "invalid_args",
        )

    try:
        argv = spec.render_argv(action, values)
    except AppCliRegistryError as exc:
        return _refused(
            ExecResult(False, _reply(f"参数错误：{exc}", f"Invalid parameter: {exc}", language)),
            getattr(exc, "code", "invalid_args"),
        )

    if not _has_subcommand(argv):
        return _refused(
            ExecResult(
                False,
                _reply(
                    "参数错误：该动作没有子命令，会进入交互式 REPL，本工具不允许。",
                    "Invalid parameter: the action has no subcommand and would enter an interactive "
                    "REPL, which this tool does not allow.",
                    language,
                ),
            ),
            "invalid_args",
        )

    # --json 是全局选项，必须紧跟入口命令、位于子命令之前（Click 约束）。
    command = [spec.entry_point, "--json", *argv]
    shown = f"$ {' '.join(command)}"
    if any(param.path for param in spec.action(action).params.values()):
        appcli_root().mkdir(parents=True, exist_ok=True)

    limit = _effective_timeout(spec, timeout)
    # 进度三段（准备 → 渲染 → 校验）。绑定在 MCP 上时转成 notifications/progress；
    # 直接调用 handler（测试、HTTP 边界）时 report_progress 是 no-op，不改变行为。
    report_progress(1, 3, f"准备：{spec.name}/{action}")
    try:
        completed = run_bounded_command(
            command,
            timeout=limit,
            env=_command_env(spec),
            max_output_bytes=_MAX_OUTPUT_BYTES,
        )
    except EdgeExecutionCancelled:
        report_progress(3, 3, "已取消")
        return _executed(
            ExecResult(
                False,
                _reply(
                    f"已取消：{spec.name}/{action} 的渲染已停止，不会自动重放。",
                    f"Cancelled: {spec.name}/{action} was stopped and will not be replayed.",
                    language,
                ),
                detail=_interrupted_detail(
                    spec, action, values, shown,
                    "渲染进程组已终止（取消）；产物现状如下。",
                ),
            ),
            "cancelled",
            command=shown,
        )
    except OutputLimitExceeded:
        return _executed(
            ExecResult(
                False,
                _reply(
                    f"应用输出超过上限，本次不判定成功：{spec.name}/{action}",
                    f"AppCli output exceeded the limit; not treated as success: {spec.name}/{action}",
                    language,
                ),
                detail=shown,
            ),
            "output_limit",
            command=shown,
        )
    except FileNotFoundError:
        return _executed(
            ExecResult(
                False,
                _reply(
                    f"应用入口命令不存在：{spec.entry_point}",
                    f"AppCli entry point not found: {spec.entry_point}",
                    language,
                ),
            ),
            "entry_point_missing",
            command=shown,
        )
    except subprocess.TimeoutExpired:
        report_progress(3, 3, "超时，已停止")
        return _executed(
            ExecResult(
                False,
                _reply(
                    f"应用执行超时（{limit}s）：{spec.name}/{action}；已停止渲染，"
                    f"不会自动重放，产物现状见详情。",
                    f"AppCli timed out after {limit}s: {spec.name}/{action}; the render was "
                    f"stopped and will not be replayed, see the detail for any partial artifact.",
                    language,
                ),
                detail=_interrupted_detail(
                    spec, action, values, shown,
                    f"渲染进程组已在 {limit}s 后终止（超时）；产物现状如下。",
                ),
            ),
            "timeout",
            command=shown,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _executed(
            ExecResult(
                False,
                _reply(
                    f"应用执行失败：{exc}",
                    f"AppCli execution failed: {exc}",
                    language,
                ),
                detail=shown,
            ),
            "exec_failed",
            command=shown,
        )

    if completed.returncode != 0:
        return _executed(
            ExecResult(
                False,
                _reply(
                    f"appcli 执行失败（exit {completed.returncode}）：{spec.name}/{action}",
                    f"AppCli failed (exit {completed.returncode}): {spec.name}/{action}",
                    language,
                ),
                detail="\n".join([shown, _tail(completed.stderr)]).strip(),
            ),
            "exit_nonzero",
            command=shown,
        )

    report_progress(2, 3, f"渲染结束，正在校验：{spec.name}/{action}")
    payload, reason = _json_summary(
        completed.stdout, allow_list=bool(spec.action(action).readonly)
    )
    if payload is None:
        return _executed(
            ExecResult(
                False,
                _reply(
                    f"应用输出异常（exit 0，但不是 JSON）：{spec.name}/{action}",
                    f"AppCli output is malformed (exit 0 but not JSON): {spec.name}/{action}",
                    language,
                ),
                detail="\n".join(
                    [shown, f"原因：{reason}", _tail(completed.stdout or completed.stderr)]
                ).strip(),
            ),
            "malformed_json",
            command=shown,
        )

    # 产物为准（H04）：命令 exit 0 只是前提，声明了校验规则就必须全部通过。
    verdicts = [
        verify_artifact(Path(values[rule.param]), rule.verify)
        for rule in spec.artifact_rules(action)
    ]
    failed = [verdict for verdict in verdicts if not verdict.ok]

    lines = [shown, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)]
    if verdicts:
        lines.append("产物：" + "；".join(verdict.summary() for verdict in verdicts))
        lines.append("校验：" + ("通过" if not failed else "未通过"))
        for verdict in verdicts:
            lines.extend(f"  {item}" for item in verdict.evidence_lines())

    report_progress(3, 3, "校验未通过" if failed else "校验通过")
    if failed:
        return _executed(
            ExecResult(
                False,
                _reply(
                    f"产物校验未通过（命令 exit 0，但产物不合格）：{spec.name}/{action}",
                    f"Artifact verification failed (the command exited 0 but the artifact is not "
                    f"acceptable): {spec.name}/{action}",
                    language,
                ),
                _truncate("\n".join(lines), _MAX_DETAIL_CHARS),
            ),
            "verify_failed",
            command=shown,
            outputs=_verdict_records(verdicts),
            verify_failed=len(failed),
        )

    if verdicts:
        headline = _reply(
            f"已生成产物并通过校验：{spec.name}/{action}",
            f"Artifact produced and verified: {spec.name}/{action}",
            language,
        )
    else:
        headline = _reply(
            f"应用执行完成（exit 0，该动作未声明产物校验规则）：{spec.name}/{action}",
            f"AppCli command finished (exit 0; this action declares no artifact "
            f"verification): {spec.name}/{action}",
            language,
        )
    return _executed(
        ExecResult(True, headline, _truncate("\n".join(lines), _MAX_DETAIL_CHARS)),
        "",
        command=shown,
        outputs=_verdict_records(verdicts),
        artifacts=len(verdicts),
    )


def _tail(text: str, limit: int = _MAX_DETAIL_CHARS) -> str:
    payload = (text or "").strip()
    if len(payload) <= limit:
        return payload
    return payload[-limit:]


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n…（已截断）"



def appcli_verify(
    appcli: str = "",
    action: str = "",
    path: str = "",
    artifact: str = "",
    language: str | None = None,
    **_,
) -> ExecResult:
    """对一个产物文件逐条跑该动作声明的校验规则，给出证据。只读。

    用于装后复验与事后复核：不执行应用命令，也不要求该应用已通过设备验收
    （白名单约束的是**执行**，复核一个未验收应用的产物是合理需求）。

    校验对象是产物文件本身，规则来自注册表里 ``artifacts[].verify``；一条规则绑定
    一个 ``param``（哪个参数指向产物）。当前每个动作只声明一个产物参数，因此默认
    按该动作的全部规则校验 ``path``；一旦将来某个动作声明了多个产物参数，就必须用
    ``artifact`` 指明校验哪一个，否则报错而不是猜。

    文件不存在时要区分两种原因：路径不在工作区内（路径错，调用问题）vs 工作区内
    合法但没写出文件（未产出，应用/命令问题）。
    """
    try:
        registry = AppCliRegistry.load()
        spec = registry.require(appcli)
        spec.action(action)
    except AppCliRegistryError as exc:
        return ExecResult(False, _reply(f"无法校验：{exc}", f"Cannot verify: {exc}", language))

    rules = spec.artifact_rules(action)
    if not rules:
        return ExecResult(
            False,
            _reply(
                f"该动作未声明产物校验规则：{spec.name}/{action}",
                f"This action declares no artifact verification rules: {spec.name}/{action}",
                language,
            ),
        )

    targets = sorted({rule.param for rule in rules})
    wanted = (artifact or "").strip()
    if wanted:
        if wanted not in targets:
            return ExecResult(
                False,
                _reply(
                    f"该动作没有产物参数 {wanted!r}；可选：{', '.join(targets)}",
                    f"This action has no artifact parameter {wanted!r}; "
                    f"available: {', '.join(targets)}",
                    language,
                ),
            )
        rules = tuple(rule for rule in rules if rule.param == wanted)
    elif len(targets) > 1:
        return ExecResult(
            False,
            _reply(
                f"该动作声明了多个产物参数（{', '.join(targets)}），请用 artifact 指明校验哪一个",
                f"This action declares several artifact parameters ({', '.join(targets)}); "
                f"name the one to verify via artifact",
                language,
            ),
        )

    try:
        target = resolve_in_workspace(path)
    except AppCliPathError as exc:
        return ExecResult(
            False,
            _reply(f"路径错误：{exc}", f"Invalid path: {exc}", language),
        )

    if not target.exists():
        return ExecResult(
            False,
            _reply(
                f"未产出：{target} 不存在。该路径在工作区内是合法的，说明命令没有写出文件。",
                f"Not produced: {target} does not exist. The path is valid inside the "
                f"workspace, so the command did not write a file.",
                language,
            ),
        )

    verdicts = [verify_artifact(target, rule.verify) for rule in rules]
    failed = [verdict for verdict in verdicts if not verdict.ok]
    checks = sum(len(rule.verify) for rule in rules)

    lines = [f"文件：{target}", f"（按 {spec.name}/{action} 的 {checks} 条规则校验）"]
    lines.extend(f"  {item}" for verdict in verdicts for item in verdict.evidence_lines())

    return ExecResult(
        not failed,
        _reply(
            f"校验未通过：{target}" if failed else f"校验通过：{target}",
            f"Verification failed: {target}" if failed else f"Verification passed: {target}",
            language,
        ),
        "\n".join(lines),
    )


HANDLERS = {
    "appcli_list": appcli_list,
    "appcli_run": appcli_run,
    "appcli_verify": appcli_verify,
}
