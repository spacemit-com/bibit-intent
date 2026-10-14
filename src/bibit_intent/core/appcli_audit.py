"""core/appcli_audit.py — 应用执行的审计与指标

设计见 docs/design/cli_anything_integration.md §5.6、任务 H06。

审计：每次 ``appcli_run`` 调用落一条 JSONL，复用 ``core/dependency_audit.record()``
（同一个文件、同一套轮转），用 ``action="appcli_run"`` 与依赖安装记录区分回放。
写失败只丢审计、绝不影响执行结果本身（连 ``record()`` 之外的异常也兜住）。

指标：进程内计数，供 ``/metrics`` 观测，重启归零。**口径必须先说清楚，否则数字没法用**：

============= ==========================================================
``runs``       真正发起命令的次数（Popen 之前被拒的不算执行）
``refusals``   未执行就被拒的次数（未验收/缺前置/通道不符/参数错/注册表坏）
``failures``   ``runs`` 里失败的次数（exit≠0、超时、非 JSON、产物不合格、启动失败）
``verify_failures`` 其中「命令 exit 0 但产物校验未通过」的次数（H04 的核心场景）
``artifacts``  累计**校验通过**的产物文件数
``by_appcli`` 按应用名分组：``runs`` / ``failures`` / ``refusals``
============= ==========================================================

``refusals`` 单独计数是有意的：把「引擎正确拒绝了不该执行的请求」和「引擎执行了但做坏了」
混成一个失败率，会让指标既看不出安全问题、也看不出质量问题。
"""
from __future__ import annotations

import copy
import threading

from bibit_intent.core.dependency_audit import record

__all__ = [
    "AUDIT_ACTION",
    "appcli_metrics",
    "record_run",
    "reset_metrics",
]

#: 审计记录里的 action 值：与依赖安装（dep_apply）共用文件但不混淆。
AUDIT_ACTION = "appcli_run"

#: ``stage`` 取值：executed = 真的发起了命令；refused = 在发起之前就被拒。
STAGES = ("executed", "refused")

_STATS: dict = {
    "runs": 0,
    "refusals": 0,
    "failures": 0,
    "verify_failures": 0,
    "artifacts": 0,
    "by_appcli": {},
}
_STATS_LOCK = threading.Lock()


def _bucket(counts: dict, name: str) -> dict:
    return counts.setdefault(name, {"runs": 0, "failures": 0, "refusals": 0})


def _count(entry: dict) -> None:
    """把一条审计记录累加进进程内指标（与写盘解耦，写盘失败也要计数）。"""
    appcli = str(entry.get("appcli") or "")
    executed = entry.get("stage") == "executed"
    with _STATS_LOCK:
        if executed:
            _STATS["runs"] += 1
            _STATS["failures"] += 0 if entry.get("ok") else 1
            _STATS["verify_failures"] += int(entry.get("verify_failures") or 0)
            _STATS["artifacts"] += int(entry.get("artifacts") or 0)
        else:
            _STATS["refusals"] += 1
        if appcli:
            bucket = _bucket(_STATS["by_appcli"], appcli)
            if executed:
                bucket["runs"] += 1
                bucket["failures"] += 0 if entry.get("ok") else 1
            else:
                bucket["refusals"] += 1


def record_run(
    *,
    appcli: str,
    target_action: str,
    stage: str,
    ok: bool = False,
    reason: str = "",
    command: str = "",
    duration_ms: int = 0,
    outputs: list[dict] | None = None,
    verify_failed: int = 0,
    artifacts: int = 0,
) -> dict:
    """落一条 appcli_run 审计并累计指标，返回落盘内容。

    ``reason`` 是机器可读的原因码（空串表示成功）：``invalid_args`` /
    ``appcli_unverified`` / ``missing_prereq`` / ``needs_host_channel`` /
    ``timeout`` / ``exit_nonzero`` / ``malformed_json`` / ``verify_failed`` 等。
    ``command`` 是给用户看过的同一条命令行，``outputs`` 是逐条产物校验证据。
    """
    entry = {
        "action": AUDIT_ACTION,
        "appcli": appcli,
        "target_action": target_action,
        "stage": stage if stage in STAGES else "refused",
        "ok": bool(ok),
        "reason": reason,
        "command": command,
        "duration_ms": max(0, int(duration_ms)),
        "outputs": copy.deepcopy(list(outputs or [])),
        "verify_failures": max(0, int(verify_failed)),
        "artifacts": max(0, int(artifacts)),
    }
    _count(entry)
    try:
        # record() 自身已吞掉 OSError；这里再兜一层，让「审计写失败不影响执行结果」是
        # **任何**异常下的性质，而不只是在 IO 异常下成立。
        return record(entry)
    except Exception:  # noqa: BLE001 - 审计是次要路径，绝不能反过来改变执行结果
        return entry


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def appcli_metrics() -> dict[str, object]:
    """应用段的运行指标（``/metrics`` 用），返回深拷贝，调用方改不到内部状态。"""
    with _STATS_LOCK:
        snapshot = copy.deepcopy(_STATS)
    return {
        **snapshot,
        "failure_rate": _rate(snapshot["failures"], snapshot["runs"]),
        "verify_failure_rate": _rate(snapshot["verify_failures"], snapshot["runs"]),
    }


def reset_metrics() -> None:
    """清空进程内指标（测试用）。"""
    with _STATS_LOCK:
        for key in _STATS:
            _STATS[key] = {} if key == "by_appcli" else 0
