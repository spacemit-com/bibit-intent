import os
import subprocess
import sys
import threading

import pytest

from bibit_intent.execution.bounded_command import OutputLimitExceeded, run_bounded_command
from bibit_intent.execution.cancellation import EdgeExecutionCancelled, cancellation_scope


def test_bounded_input_output():
    result = run_bounded_command([sys.executable, "-c", "import sys; print(sys.stdin.read()); print('error', file=sys.stderr)"], input="hello", timeout=2)
    assert result.stdout == "hello\n" and result.stderr == "error\n" and result.returncode == 0


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_output_limit_reaps_process(tmp_path, stream):
    marker = tmp_path / "pid"
    code = f"import os,sys; open({str(marker)!r},'w').write(str(os.getpid())); sys.{stream}.write('x'*1000000); sys.{stream}.flush()"
    with pytest.raises(OutputLimitExceeded):
        run_bounded_command([sys.executable, "-c", code], timeout=3, max_output_bytes=1024)
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


def test_timeout_reaps_process(tmp_path):
    marker = tmp_path / "pid"
    code = f"import os,time; open({str(marker)!r},'w').write(str(os.getpid())); time.sleep(30)"
    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded_command([sys.executable, "-c", code], timeout=0.4)
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


def test_cancellation_stops_readonly_process():
    event = threading.Event()
    timer = threading.Timer(0.2, event.set)
    timer.start()
    try:
        with cancellation_scope(event), pytest.raises(EdgeExecutionCancelled):
            run_bounded_command([sys.executable, "-c", "import time; time.sleep(30)"], timeout=3)
    finally:
        timer.cancel()


def test_exited_parent_with_inherited_pipe_is_bounded():
    code = "import os,time; child=os.fork(); time.sleep(30) if child == 0 else None"
    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded_command([sys.executable, "-c", code], timeout=0.3)


def test_exact_limit_and_combined_streams():
    assert run_bounded_command([sys.executable, "-c", "print('abc',end='')"], timeout=2, max_output_bytes=3).stdout == "abc"
    with pytest.raises(OutputLimitExceeded):
        run_bounded_command([sys.executable, "-c", "import sys; print('ab',end=''); print('cd',end='',file=sys.stderr)"], timeout=2, max_output_bytes=3)
