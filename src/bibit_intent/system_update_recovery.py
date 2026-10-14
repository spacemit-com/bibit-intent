"""系统更新自升级阶段完成后的恢复弹框。

自研组件（bibit-intent / bianbu-agent-runtime 等）升级会重启 Runtime，当前 MCP
会话随之被切断，无法把最终结果原样回给前端。脱离式更新脚本在进入该阶段前会写入
一个恢复标记，Runtime 恢复后重新拉起的 bibit-intent MCP 在启动时读取该标记，等
到状态文件到达终态后弹出成功/失败结果框，再清理标记。

标记放在 /run/user/<uid> 下：root（脱离式脚本）与登录用户（MCP 侧）都能读写，且
用户登出后自动失效，避免残留导致下次误报。
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import subprocess
import time
from pathlib import Path


_STATUS = Path("/var/lib/bibit-intent/system-update.status")
_DETAIL = Path("/var/lib/bibit-intent/system-update.json")
_TERMINAL = {"succeeded", "succeeded_with_skips", "cancelled", "failed"}
logger = logging.getLogger(__name__)

_MESSAGES = {
    "succeeded": "系统更新已完成。",
    "succeeded_with_skips": "其他软件包更新已完成；已跳过需要降级的软件包。",
    "cancelled": "已取消系统更新，未安装或降级任何软件包。",
    "failed": "系统更新未完成。请查看 /var/log/bibit-system-update.log",
}


def _recovery_marker(uid: int | None = None) -> Path:
    if uid is None:
        uid = os.getuid()
    return Path(f"/run/user/{uid}/bibit-intent/system-update.recovery")


def _read_state() -> tuple[str, dict]:
    try:
        state = _STATUS.read_text(encoding="utf-8").strip()
    except OSError:
        state = ""
    detail: dict = {}
    try:
        value = json.loads(_DETAIL.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            detail = value
    except (OSError, ValueError):
        pass
    return state, detail


def _popup(uid: int, text: str, *, error: bool = False) -> bool:
    """Return whether the result dialog was actually acknowledged."""
    try:
        from bibit_intent.execution.system_tools import _find_desktop_session_env
        env = _find_desktop_session_env(uid)
    except Exception:
        logger.exception('Failed to discover the desktop session for the upgrade result')
        env = None
    if not env:
        logger.warning('Upgrade result dialog deferred: desktop session unavailable')
        return False
    zenity = "/usr/bin/zenity"
    if not os.path.exists(zenity):
        logger.warning('Upgrade result dialog deferred: zenity unavailable')
        return False
    arguments = [
        zenity,
        "--error" if error else "--info",
        "--title=系统更新",
        "--timeout=120",
        "--text=" + text,
    ]
    try:
        result = subprocess.run(arguments, env=env, capture_output=True, text=True, timeout=130)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning('Upgrade result dialog failed: %s', exc)
        return False
    # zenity --info --timeout=120 在用户点 OK 时返回 0，在超时自动关闭时返回
    # 5（timeout）；两者都表示对话框已经成功显示过，只有负数返回码（进程被
    # 信号杀死，如 SIGSEGV）或 zenity 主动报错才视为失败。否则超时会把正常
    # 显示过的结果框误判成失败，导致恢复标记残留、下次重启又重复弹框。
    if result.returncode not in (0, 5):
        logger.warning('Upgrade result dialog exited with %s: %s', result.returncode,
                       result.stderr.strip())
        return False
    return True


def report_pending_system_update(timeout_seconds: int = 90) -> None:
    """一次性恢复检查：有恢复标记就等终态、弹结果框并清标记。

    Runtime 恢复后会同时拉起多个 MCP worker，本函数用非阻塞 flock 保证只有
    一个 worker 真正弹框，其余直接返回。作为后台守护线程调用，不阻塞 MCP 启动。
    """
    uid = os.getuid()
    marker = _recovery_marker(uid)
    if not marker.exists():
        return
    try:
        job_id = json.loads(marker.read_text()).get('job_id')
    except (OSError, ValueError, AttributeError):
        return
    lock = marker.with_suffix(".lock")
    try:
        lock_file = open(lock, "w")
    except OSError:
        return
    with lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return
        try:
            deadline = time.monotonic() + timeout_seconds
            state, detail = _read_state()
            while state not in _TERMINAL and time.monotonic() < deadline:
                time.sleep(1)
                state, detail = _read_state()
            if state in _TERMINAL:
                if job_id and detail.get('job_id') != job_id:
                    return
                if state == "failed":
                    error_text = str(detail.get("error") or "").strip()
                    text = _MESSAGES["failed"] + (f"\n{error_text}" if error_text else "")
                    shown = _popup(uid, text, error=True)
                else:
                    shown = _popup(uid, _MESSAGES.get(state, state))
                if not shown:
                    return
            else:
                return
            try:
                marker.unlink()
            except OSError:
                pass
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
