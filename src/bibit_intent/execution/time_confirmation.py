from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass

from . import system_tools
from .system_tools import ExecResult, _reply


_CHANGE_LOCK = threading.Lock()
CONFIRMATION_TTL = 180
MAX_PENDING = 128


def ntp_enabled() -> bool:
    result = system_tools._run(["timedatectl", "show", "--property=NTP", "--value"])
    if not result.ok or result.detail.strip() not in ("yes", "no"):
        raise ValueError(result.detail or result.message or "Could not determine NTP state")
    return result.detail.strip() == "yes"


def prepare_time_change(target: str, language: str | None) -> ExecResult:
    with _CHANGE_LOCK:
        try:
            enabled = ntp_enabled()
        except (OSError, ValueError) as error:
            return ExecResult(False, _reply("无法确认自动校时状态，未修改系统时间。",
                                           "Cannot determine automatic time synchronization state; no changes made.", language), str(error))
        if not enabled:
            return system_tools._write_system_time(target, language)
        return time_change_confirmation(target, language)


def time_change_confirmation(target: str, language: str | None) -> ExecResult:
    preservation = (
        _reply("保留执行时的日期", "preserve the date at execution", language) if len(target) == 8 else
        _reply("保留执行时的时分秒", "preserve the clock time at execution", language) if len(target) == 10 else
        _reply("使用系统当前时区", "use the current system timezone", language)
    )
    message = _reply(
        f"关闭 NTP 自动校时并设置为 {target}（{preservation}）？成功后自动校时将保持关闭。",
        f"Disable NTP automatic time synchronization and set {target} ({preservation})? Automatic synchronization will remain disabled after success.", language)
    return ExecResult(False, message, confirmation={"kind": "disable_ntp_and_set_time", "datetime": target,
                                                  "language": language, "description": message})


def _apply_confirmed(target: str, language: str | None) -> ExecResult:
    blocked = system_tools._settings_host_guard(language)
    if blocked is not None:
        return blocked
    with _CHANGE_LOCK:
        disabled = False
        try:
            if ntp_enabled():
                disabled = True
                result = system_tools._run(["timedatectl", "--no-ask-password", "set-ntp", "false"])
                if not result.ok:
                    raise ValueError("Could not confirm NTP disable; time was not set: " + (result.detail or result.message))
                if ntp_enabled():
                    raise ValueError("NTP is still enabled after disabling it")
            result = system_tools._write_system_time(target, language)
            if result.ok:
                result.message += _reply(" 自动校时保持关闭。", " Automatic synchronization remains disabled.", language)
                return result
            failure = result.detail or result.message
        except Exception as error:
            failure = str(error)
        restored = False
        if disabled:
            try:
                restored = system_tools._run(["timedatectl", "--no-ask-password", "set-ntp", "true"]).ok and ntp_enabled()
            except Exception:
                restored = False
        status = (_reply("已恢复自动校时。", "Automatic synchronization was restored.", language) if restored else
                  _reply("自动校时恢复未确认，请检查系统设置。", "Restoring automatic synchronization is unconfirmed; check system settings.", language) if disabled else
                  _reply("未关闭自动校时。", "Automatic synchronization was not disabled by this request.", language))
        return ExecResult(False, _reply("时间设置未成功确认。", "The time change was not confirmed successful.", language) + status, failure)


@dataclass(frozen=True)
class PendingTimeChange:
    session_id: str
    run_id: str
    token: str
    expires: float
    target: str
    language: str | None


class TimeConfirmations:
    def __init__(self):
        self._pending: dict[str, PendingTimeChange] = {}
        self._lock = threading.Lock()

    def create(self, session_id: str, run_id: str, request: dict) -> dict:
        with self._lock:
            now = time.monotonic()
            self._pending = {key: value for key, value in self._pending.items()
                             if value.expires > now and value.session_id != session_id}
            if len(self._pending) >= MAX_PENDING:
                raise ValueError("Too many pending confirmations")
            identifier, token = secrets.token_urlsafe(24), secrets.token_urlsafe(32)
            self._pending[identifier] = PendingTimeChange(session_id, run_id, token, now + CONFIRMATION_TTL,
                                                        request["datetime"], request.get("language"))
        return {"id": identifier, "token": token, "kind": "disable_ntp_and_set_time",
                "datetime": request["datetime"], "description": request["description"], "timeout_seconds": CONFIRMATION_TTL}

    def discard(self, *, session_id: str | None = None, run_id: str | None = None):
        with self._lock:
            self._pending = {key: value for key, value in self._pending.items()
                             if not ((session_id is not None and value.session_id == session_id) or
                                     (run_id is not None and value.run_id == run_id))}

    def resolve(self, identifier: str, token: str, session_id: str, decision: str) -> ExecResult:
        if decision not in ("approve", "reject"):
            raise ValueError("Invalid decision")
        with self._lock:
            pending = self._pending.get(identifier)
            if pending is None or pending.session_id != session_id or not secrets.compare_digest(pending.token, token):
                raise ValueError("Unknown confirmation or invalid identity")
            self._pending.pop(identifier)
            if pending.expires <= time.monotonic():
                raise ValueError("Confirmation expired")
        if decision == "reject":
            return ExecResult(True, _reply("已取消，未修改时间或自动校时设置。",
                                           "Cancelled; time and automatic synchronization were not changed.", pending.language))
        return _apply_confirmed(pending.target, pending.language)
