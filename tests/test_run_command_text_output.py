from __future__ import annotations

import subprocess
import sys

import pytest

from bibit_intent.execution import progress
from bibit_intent.execution.cancellation import run_command


def test_run_command_streams_text_when_communicate_times_out():
    # Python 3.14 exposes TimeoutExpired.stdout/stderr as bytes even for a
    # text=True Popen. run_command must normalize them before invoking
    # on_output, otherwise regex-based progress observers throw
    # ``TypeError: cannot use a string pattern on a bytes-like object``.
    seen: list[tuple[str, str]] = []
    code = "import sys,time; sys.stdout.write('downloading 42%\\n'); sys.stdout.flush(); time.sleep(30)"
    with pytest.raises(subprocess.TimeoutExpired):
        run_command(
            [sys.executable, "-c", code],
            timeout=0.4,
            on_output=lambda stdout, stderr: seen.append((stdout, stderr)),
        )

    assert seen
    for stdout, stderr in seen:
        assert isinstance(stdout, str)
        assert isinstance(stderr, str)
    assert "downloading 42%" in "".join(stdout for stdout, _ in seen)


def test_observe_output_accepts_bytes_as_a_defensive_fallback():
    task_id = "bytes-output-task"
    progress.set_current_task(task_id)
    try:
        progress.observe_output(b"downloading 50%", b"Progress: [75%]")
        snapshot = progress.snapshot(task_id)
        assert snapshot is not None
        assert snapshot.percent is not None
    finally:
        progress.clear_current_task()
        progress.forget(task_id)
