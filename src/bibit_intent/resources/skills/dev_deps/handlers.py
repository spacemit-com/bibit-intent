"""dev_deps Skill 的执行实现：依赖扫描 / 状态 / 计划 / 验证（全部只读）。

执行安装的是 ubuntu_admin 的 dep_apply（confirm: true，走 sudo/venv）。
本文件只读文件与只读命令，不做任何写操作（pip 干跑会写 pip 缓存，不装包）。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from bibit_intent.core import dependency_plan as plan_mod
from bibit_intent.core import dependency_scan as scan_mod
from bibit_intent.core.dependency_audit import read_records
from bibit_intent.core.progress import report_progress
from bibit_intent.core.dependency_plan import (
    build_plan,
    format_plan_message,
    format_verify_message,
    get_plan,
    save_plan,
    verify_plan,
)
from bibit_intent.core.dependency_scan import (
    Requirement,
    check_requirements,
    install_target_python,
    requirements_from_dicts,
    resolve_root,
    scan_project,
)

_DETAIL_ITEMS = 50


@dataclass
class ExecResult:
    ok: bool
    message: str
    detail: str = ""
    escalate: str | None = None


def _scan_and_check(path: str, python: str, limit: int) -> tuple[ExecResult | None, dict, list[Requirement], dict]:
    root, err = resolve_root(path)
    if root is None:
        return ExecResult(False, err), {}, [], {}
    scanned = scan_project(
        root,
        limit=limit,
        on_progress=lambda done, total: report_progress(
            done, total, f"已扫描 {done}/{total} 个文件"
        ),
    )
    requirements = requirements_from_dicts(scanned.get("requirements") or [])
    # 以「安装目标环境」判定，保证 dep_status 与 dep_plan/verify 结论一致
    target_python, _ = install_target_python(root, python)
    statuses = check_requirements(requirements, root=root, python=target_python)
    return None, {"root": root, "scanned": scanned}, requirements, statuses


def _evidence(item: Requirement) -> str:
    parts = []
    for entry in item.evidence[:2]:
        parts.append(f"{entry.file}:{entry.line} `{entry.snippet}`")
    return "；".join(parts)


def dep_scan(path: str = "", limit: int = 200, **_) -> ExecResult:
    """扫描项目，列出代码里用到的依赖（不判断是否已安装、不安装）。"""
    failure, context, requirements, _ = _scan_and_check(path, "", limit)
    if failure is not None:
        return failure
    root: Path = context["root"]
    scanned = context["scanned"]
    if not requirements:
        return ExecResult(
            True,
            f"{root} 里没有发现依赖需求（扫描 {scanned['files_scanned']} 个文件）。"
            "支持 Python import / requirements.txt / pyproject.toml、C/C++ #include、"
            "CMake/Makefile 的 pkg-config、Makefile 的 -l 链接旗标、ctypes 动态库、package.json。",
        )
    kinds: dict[str, int] = {}
    for item in requirements:
        kinds[item.kind] = kinds.get(item.kind, 0) + 1
    summary = "、".join(f"{kind} {count} 项" for kind, count in sorted(kinds.items()))
    lines = [f"{root} 共发现 {len(requirements)} 项依赖需求（{summary}）。"]
    for item in requirements[:_DETAIL_ITEMS]:
        line = f"  - [{item.kind}] {item.name}"
        if item.spec:
            line += f"（{item.spec}）"
        evidence = _evidence(item)
        if evidence:
            line += f" ← {evidence}"
        lines.append(line)
    if scanned.get("truncated") or len(requirements) > _DETAIL_ITEMS:
        lines.append(f"  …（仅显示前 {_DETAIL_ITEMS} 项，共 {scanned.get('total_requirements')} 项）")
    lines.append("下一步：dep_status 查看缺哪些，dep_plan 生成带来源与风险的安装计划。")
    detail = json.dumps(
        {"root": str(root), "requirements": [item.to_dict() for item in requirements[:_DETAIL_ITEMS]]},
        ensure_ascii=False,
    )
    return ExecResult(True, "\n".join(lines), detail)


def dep_status(path: str = "", python: str = "", **_) -> ExecResult:
    """判定项目依赖在本机是否已满足（只读）。"""
    failure, context, requirements, statuses = _scan_and_check(path, python, 200)
    if failure is not None:
        return failure
    root: Path = context["root"]
    if not requirements:
        return ExecResult(True, f"{root} 里没有发现依赖需求。")
    missing = [item for item in requirements if not statuses.get(item.key(), {}).get("satisfied")]
    lines = [f"{root}：{len(requirements)} 项依赖中，满足 {len(requirements) - len(missing)} 项，缺失 {len(missing)} 项。"]
    for item in missing:
        status = statuses.get(item.key(), {})
        line = f"  - [缺] [{item.kind}] {item.name}"
        if status.get("detail"):
            line += f"：{status['detail']}"
        evidence = _evidence(item)
        if evidence:
            line += f" ← {evidence}"
        lines.append(line)
    for item in requirements:
        if statuses.get(item.key(), {}).get("satisfied"):
            lines.append(f"  - [有] [{item.kind}] {item.name}")
    if missing:
        lines.append("下一步：dep_plan 生成安装计划（含来源、体积、风险），确认后再安装。")
    else:
        lines.append("依赖都已满足，无需安装。")
    detail = json.dumps(
        {
            "root": str(root),
            "missing": [
                {"kind": item.kind, "name": item.name,
                 "detail": statuses.get(item.key(), {}).get("detail", "")}
                for item in missing
            ],
        },
        ensure_ascii=False,
    )
    return ExecResult(True, "\n".join(lines), detail)


def dep_plan(
    path: str = "", scope: str = "auto", python: str = "", max_dry_runs: int = 8, **_,
) -> ExecResult:
    """扫描 + 判定 + 干跑，生成可确认的依赖安装计划（不安装）。"""
    scope = (scope or "auto").strip().lower() or "auto"
    if scope not in {"auto", "user", "system"}:
        return ExecResult(False, "scope 只支持 auto / user / system")
    try:
        dry_runs = max(0, min(int(max_dry_runs), 20))
    except (TypeError, ValueError):
        return ExecResult(False, "max_dry_runs 需要是整数")
    failure, context, requirements, statuses = _scan_and_check(path, python, 200)
    if failure is not None:
        return failure
    root: Path = context["root"]
    if not requirements:
        return ExecResult(True, f"{root} 里没有发现依赖需求，无需安装。")
    plan = build_plan(
        root, requirements, statuses, scope=scope, python=python, max_dry_runs=dry_runs,
    )
    save_plan(plan)
    detail = json.dumps(
        {
            "plan_id": plan["plan_id"],
            "project_root": plan["project_root"],
            "scope": plan["scope"],
            "ttl_seconds": plan["ttl_seconds"],
            "summary": plan["summary"],
            "items": [
                {
                    "index": item["index"], "kind": item["kind"], "name": item["name"],
                    "status": item["status"], "channel": item["channel"],
                    "package": item["package"], "version": item["version"],
                    "source": item["source"], "risk": item["risk"],
                    "notes": item["notes"],
                }
                for item in plan["items"]
            ],
        },
        ensure_ascii=False,
    )
    return ExecResult(True, format_plan_message(plan), detail)


def dep_verify(plan_id: str = "", python: str = "", **_) -> ExecResult:
    """对已生成的计划做装后复验（import / pkg-config / 头文件 / 命令）。"""
    plan, err = get_plan(plan_id)
    if plan is None:
        return ExecResult(False, err)
    results = verify_plan(plan, python=python)
    message = format_verify_message(results)
    detail = json.dumps({"plan_id": plan.get("plan_id"), "results": results}, ensure_ascii=False)
    ok = all(item["satisfied"] for item in results) if results else True
    return ExecResult(ok, message, detail)


def dep_history(limit: int = 5, **_) -> ExecResult:
    """查看最近几次 dep_apply 的审计记录（只读），用于回答「上次装了什么」与取回滚命令。"""
    try:
        count = max(1, min(int(limit), 20))
    except (TypeError, ValueError):
        return ExecResult(False, "limit 需要是整数")
    records = read_records(count)
    if not records:
        return ExecResult(True, "还没有依赖安装记录。")
    lines = [f"最近 {len(records)} 次依赖安装记录（新→旧）："]
    for record in records:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(record.get("recorded_at", 0)))
        items = record.get("items") or []
        done = [item for item in items if item.get("ok")]
        verified = [item for item in items if item.get("ok") and item.get("verified")]
        status = "成功" if record.get("ok") else ("部分成功" if done else "未执行")
        lines.append(
            f"  - {stamp} {record.get('plan_id', '')}（{record.get('project_root', '')}）"
            f"：{status}，执行 {len(items)} 项、验证通过 {len(verified)} 项"
        )
        for item in items:
            if item.get("ok"):
                lines.append(f"      [{item.get('label')}] 回滚：{item.get('rollback') or '无'}")
    detail = json.dumps({"records": records}, ensure_ascii=False)
    return ExecResult(True, "\n".join(lines), detail)


_NOTICE_PREFIX = "依赖提醒"
_NOTICE_CHANNELS = {
    scan_mod.KIND_PYTHON: "pip",
    scan_mod.KIND_C_HEADER: "apt",
    scan_mod.KIND_PKGCONFIG: "apt",
    scan_mod.KIND_SHARED_LIB: "apt",
    scan_mod.KIND_COMMAND: "apt",
    scan_mod.KIND_NODE: "npm",
}


def dep_notice(path: str = "", limit: int = 5, **_) -> ExecResult:
    """写完文件后的一次性依赖提示（只读）：只列缺失项与下一步，绝不安装。

    专供 Runtime 的 PostToolUse Hook 调用（见 `docs/tools/dependency_management.md`），
    所以首行必须短、且带固定前缀与「未安装」标记，方便 Hook 判断要不要提示；
    有缺失时故意不给证据行——那是 `dep_status` 的活，这里只做提醒。
    """
    try:
        cap = max(1, min(int(limit), 10))
    except (TypeError, ValueError):
        return ExecResult(False, "limit 需要是整数")
    failure, context, requirements, statuses = _scan_and_check(path, "", 200)
    if failure is not None:
        return failure
    root: Path = context["root"]
    if not requirements:
        return ExecResult(
            True, f"{_NOTICE_PREFIX}【无依赖】：{root} 里没有发现依赖需求。",
            json.dumps({"root": str(root), "notice": "empty", "missing": []}, ensure_ascii=False),
        )
    missing = [item for item in requirements if not statuses.get(item.key(), {}).get("satisfied")]
    if not missing:
        return ExecResult(
            True,
            f"{_NOTICE_PREFIX}【已满足】：{root} 的 {len(requirements)} 项依赖都已满足。",
            json.dumps(
                {"root": str(root), "notice": "satisfied", "missing": []}, ensure_ascii=False
            ),
        )
    shown = missing[:cap]
    line = (
        f"{_NOTICE_PREFIX}【未安装】：{root} 还缺 {len(missing)} 项依赖——"
        + "、".join(f"{item.name}（{_NOTICE_CHANNELS.get(item.kind, 'manual')}）" for item in shown)
    )
    if len(missing) > len(shown):
        line += f"（另有 {len(missing) - len(shown)} 项未列出）"
    line += f"；仅提示，未安装。要安装请先回复「生成依赖计划 {root}」。"
    detail = json.dumps(
        {
            "root": str(root),
            "notice": "missing",
            "missing": [
                {
                    "kind": item.kind,
                    "name": item.name,
                    "channel": _NOTICE_CHANNELS.get(item.kind, "manual"),
                }
                for item in missing
            ],
        },
        ensure_ascii=False,
    )
    return ExecResult(True, line, detail)


HANDLERS = {
    "dep_scan": dep_scan,
    "dep_status": dep_status,
    "dep_plan": dep_plan,
    "dep_verify": dep_verify,
    "dep_history": dep_history,
    "dep_notice": dep_notice,
}
