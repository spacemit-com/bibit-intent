"""Cooperative cancellation for edge-side subprocess execution."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from queue import Empty, Queue
from typing import Callable, Iterator


class EdgeExecutionCancelled(RuntimeError):
    """Raised after an in-flight edge subprocess has been stopped and reaped."""


class EdgeDeadlineExceeded(RuntimeError):
    """Raised before execution when the Hook deadline has too little budget."""


@dataclass(slots=True)
class EdgeExecutionControl:
    deadline: float | None = None
    _cancelled: threading.Event = field(default_factory=threading.Event)
    execution_started: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def set(self) -> None:
        with self._lock:
            self._cancelled.set()

    def is_set(self) -> bool:
        return self._cancelled.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._cancelled.wait(timeout)

    def begin_execution(self, minimum_budget_seconds: float) -> None:
        with self._lock:
            if self._cancelled.is_set():
                raise EdgeExecutionCancelled("edge execution cancelled")
            if (
                self.deadline is not None
                and self.deadline - time.monotonic() < minimum_budget_seconds
            ):
                raise EdgeDeadlineExceeded("insufficient edge execution budget")
            self.execution_started.set()


_LOCAL = threading.local()


@contextmanager
def cancellation_scope(cancel_event: threading.Event | None) -> Iterator[None]:
    previous = getattr(_LOCAL, "cancel_event", None)
    _LOCAL.cancel_event = cancel_event
    try:
        yield
    finally:
        _LOCAL.cancel_event = previous


def raise_if_cancelled() -> None:
    event = getattr(_LOCAL, "cancel_event", None)
    if event is not None and event.is_set():
        raise EdgeExecutionCancelled("edge execution cancelled")


def remaining_budget() -> float | None:
    """本轮执行的剩余时间预算（秒）；无截止时间或非控制对象时返回 None。

    handler 里的长时间等待（等用户在授权框上点「允许」）必须按这个预算收敛，
    否则会把工具调用拖过 hook deadline，被上层判成 edge_deadline_exceeded。
    """
    control = getattr(_LOCAL, "cancel_event", None)
    deadline = getattr(control, "deadline", None)
    if deadline is None:
        return None
    return deadline - time.monotonic()


def begin_execution(minimum_budget_seconds: float) -> None:
    """Atomically gate execution on cancellation/deadline and mark it started."""
    control = getattr(_LOCAL, "cancel_event", None)
    if isinstance(control, EdgeExecutionControl):
        control.begin_execution(minimum_budget_seconds)
        return
    raise_if_cancelled()
    deadline = getattr(control, "deadline", None)
    if deadline is not None and deadline - time.monotonic() < minimum_budget_seconds:
        raise EdgeDeadlineExceeded("insufficient edge execution budget")
    started = getattr(control, "execution_started", None)
    if started is not None:
        started.set()


def end_execution_without_side_effect() -> None:
    """Withdraw the "execution started" mark after a handler proved it did nothing.

    Tools that return ``escalate`` (app not found, no GUI askpass, package not
    found, ...) have by contract performed no device change.  Leaving the mark
    set would make the HTTP boundary report "possibly partially executed" and
    swallow the cloud fallback the tool explicitly asked for.
    """
    control = getattr(_LOCAL, "cancel_event", None)
    started = getattr(control, "execution_started", None)
    if started is not None:
        started.clear()


def interruptible_call(function, *, deadline: float | None = None):
    """Allow cancellation and an optional total deadline for blocking I/O."""
    event = getattr(_LOCAL, "cancel_event", None)
    if event is None and deadline is None:
        return function()
    result: Queue[tuple[bool, object]] = Queue(maxsize=1)

    def invoke() -> None:
        try:
            result.put((True, function()))
        except BaseException as exc:
            result.put((False, exc))

    def check_budget():
        raise_if_cancelled()
        if deadline is not None and time.monotonic() >= deadline:
            raise EdgeDeadlineExceeded("edge I/O deadline exceeded")

    check_budget()
    threading.Thread(target=invoke, name="bibit-interruptible-io", daemon=True).start()
    while True:
        check_budget()
        try:
            ok, value = result.get(timeout=0.05)
        except Empty:
            continue
        check_budget()
        if ok:
            return value
        if isinstance(value, BaseException):
            raise value
        raise RuntimeError("interruptible call returned an invalid exception")


def _stop_process_group(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=0.5)


def _text_output(value: str | bytes | None) -> str:
    """Normalize subprocess output to ``str``.

    Python 3.14's ``TimeoutExpired.stdout``/``stderr`` can still carry ``bytes``
    even when the process was created with ``text=True``; ``None`` is also
    possible for an empty/unread stream.  Keep the text-mode contract stable so
    ``on_output`` callbacks never receive a mixed bytes/str pair.
    """
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def run_command(
    cmd: list[str],
    *,
    timeout: float | None,
    env: dict[str, str] | None = None,
    input: str | None = None,
    on_output: Callable[[str, str], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command and promptly terminate its process group on cancellation."""
    raise_if_cancelled()
    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if input is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    deadline = None if timeout is None else time.monotonic() + float(timeout)
    first_communicate = True
    stdout_seen = 0
    stderr_seen = 0

    def emit_new_output(stdout: str, stderr: str) -> None:
        nonlocal stdout_seen, stderr_seen
        new_stdout = stdout[stdout_seen:] if len(stdout) > stdout_seen else ""
        new_stderr = stderr[stderr_seen:] if len(stderr) > stderr_seen else ""
        stdout_seen = len(stdout)
        stderr_seen = len(stderr)
        if on_output is not None and (new_stdout or new_stderr):
            on_output(new_stdout, new_stderr)

    while True:
        try:
            raise_if_cancelled()
        except EdgeExecutionCancelled:
            _stop_process_group(process)
            process.communicate()
            raise
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            _stop_process_group(process)
            stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(
                cmd,
                timeout,
                output=_text_output(stdout),
                stderr=_text_output(stderr),
            )
        try:
            stdout, stderr = process.communicate(
                input=input if first_communicate else None,
                timeout=0.1 if remaining is None else min(0.1, remaining),
            )
        except subprocess.TimeoutExpired as exc:
            stdout = _text_output(exc.stdout)
            stderr = _text_output(exc.stderr)
            emit_new_output(stdout, stderr)
            first_communicate = False
            continue
        emit_new_output(stdout, stderr)
        return subprocess.CompletedProcess(
            cmd,
            process.returncode,
            stdout,
            stderr,
        )
