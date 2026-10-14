"""Thread-local progress reporting for long-running MCP tasks.

Handlers run in worker threads started by ``_AsyncTaskRegistry``; this module
associates each thread with a ``task_id`` so a handler can publish coarse
phase/progress updates without knowing anything about MCP. ``bibit_task_status``
then merges the latest snapshot into its polling response.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class ProgressSnapshot:
    percent: float | None
    message: str
    phase: str = ""


_LOCAL = threading.local()
_REPORTS: dict[str, ProgressSnapshot] = {}
_LOCK = threading.Lock()

_PERCENT_RE = re.compile(r"\b(\d{1,3})\s*%")
_PROGRESS_LINE_RE = re.compile(r"Progress:\s*\[\s*(\d+)\s*%\]")


def set_current_task(task_id: str) -> None:
    _LOCAL.task_id = task_id


def clear_current_task() -> None:
    _LOCAL.task_id = None


def report(percent: float | None, message: str, *, phase: str = "") -> None:
    """Publish a progress update for the current thread's task, if any."""
    task_id = getattr(_LOCAL, "task_id", None)
    if not task_id:
        return
    if percent is not None:
        percent = min(100.0, max(0.0, float(percent)))
    with _LOCK:
        _REPORTS[task_id] = ProgressSnapshot(percent=percent, message=str(message), phase=str(phase))


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def observe_output(stdout: str | bytes | None, stderr: str | bytes | None) -> None:
    """Extract a useful progress line from subprocess output and publish it.

    apt/dpkg/curl all print different progress formats. We don't try to parse
    them exactly; we prefer an explicit percent and otherwise keep the last
    non-empty line so the user sees what the installer is doing.
    """
    task_id = getattr(_LOCAL, "task_id", None)
    if not task_id:
        return
    stdout = _text(stdout)
    stderr = _text(stderr)
    text = f"{stderr}\n{stdout}" if stderr and stdout else (stderr or stdout)
    if not text:
        return

    matches = _PERCENT_RE.findall(text)
    if matches:
        percent = int(matches[-1])
        progress_line = _last_nonempty_line(text)
        report(percent, progress_line or f"下载中 {percent}%", phase="installing")
        return

    progress_matches = _PROGRESS_LINE_RE.findall(text)
    if progress_matches:
        percent = int(progress_matches[-1])
        report(percent, f"正在配置软件包 {percent}%", phase="installing")
        return

    line = _last_nonempty_line(text)
    if line:
        report(None, line, phase="installing")


def snapshot(task_id: str) -> ProgressSnapshot | None:
    with _LOCK:
        return _REPORTS.get(task_id)


def forget(task_id: str) -> None:
    with _LOCK:
        _REPORTS.pop(task_id, None)


def _last_nonempty_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line:
            return line
    return ""
