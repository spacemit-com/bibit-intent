"""依赖安装审计：每次 dep_apply 落一条 JSONL 记录。

方案 §4.4 要求「每次 dep_apply 落一条记录（plan_id、条目、命令、过程输出摘要、
验证结果），供 Runtime 会话回放与安全清单核对」。审计写在用户数据目录下的
``dependency-audit.jsonl``：写失败只丢审计，绝不影响安装结果本身。

记录只追加、不原地改写；超过 ``_MAX_BYTES`` 时轮转一代（``.1``），避免端侧
磁盘被长期日志吃掉。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

AUDIT_ENV = "BIBIT_INTENT_DEPENDENCY_AUDIT"
_MAX_BYTES = 2_000_000
_ITEM_FIELDS = (
    "index",
    "label",
    "ok",
    "verified",
    "message",
    "evidence",
    "rollback",
    "build_tools",
)


def audit_path() -> Path:
    """审计文件路径；测试/部署可用 ``BIBIT_INTENT_DEPENDENCY_AUDIT`` 覆盖。"""
    override = os.environ.get(AUDIT_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "bibit-intent" / "dependency-audit.jsonl"


def _rotate(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size >= _MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        # 轮转失败不阻塞写入：继续追加，最多是文件再大一点。
        pass


def record(entry: dict) -> dict:
    """追加一条审计记录并返回落盘内容（写失败也返回构造好的记录）。"""
    payload = {"recorded_at": time.time(), **entry}
    path = audit_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate(path)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return payload


def record_apply(
    *,
    plan: dict,
    results: list[dict],
    note: str = "",
    overall: bool = False,
) -> dict:
    """把一次 dep_apply 的结果落成审计记录。"""
    return record({
        "action": "dep_apply",
        "plan_id": str(plan.get("plan_id") or ""),
        "project_root": str(plan.get("project_root") or ""),
        "files_hash": str(plan.get("files_hash") or ""),
        "scope": str(plan.get("scope") or ""),
        "ok": bool(overall),
        "note": note,
        "items": [
            {key: item.get(key) for key in _ITEM_FIELDS if key in item}
            for item in results
        ],
    })


def read_records(limit: int = 20, *, path: Path | None = None) -> list[dict]:
    """读取最近 ``limit`` 条审计记录（新→旧），坏行跳过。"""
    target = path or audit_path()
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict] = []
    for line in lines[-max(1, int(limit)):]:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    records.reverse()
    return records
