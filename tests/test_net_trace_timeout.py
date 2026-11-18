import importlib.util
import subprocess
import sys

import pytest

from bibit_intent.execution.cancellation import run_command
from bibit_intent.resources import resource_path


@pytest.fixture
def handlers():
    spec = importlib.util.spec_from_file_location("net_trace_test_handlers", resource_path("skills/ubuntu_cmd/handlers.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


@pytest.mark.parametrize("output", [b"1: gateway\n", "1: gateway\n", None])
def test_timeout_preserves_output_without_claiming_success(handlers, monkeypatch, output):
    def timeout(command, **kwargs):
        assert command == ["tracepath", "example.com"]
        assert kwargs["timeout"] == 8
        raise subprocess.TimeoutExpired(command, 8, output=output, stderr=b"no reply")
    monkeypatch.setattr(handlers, "run_command", timeout)
    result = handlers.net_trace("example.com")
    assert not result.ok
    assert "部分输出" in result.message
    assert "no reply" in result.detail
    if output:
        assert "gateway" in result.detail


def test_real_subprocess_partial_output_survives_timeout(handlers, monkeypatch):
    def slow_trace(command, **kwargs):
        return run_command([sys.executable, "-c", "import time; print('1: gateway', flush=True); time.sleep(10)"], timeout=0.2)
    monkeypatch.setattr(handlers, "run_command", slow_trace)
    result = handlers.net_trace("example.com")
    assert not result.ok and "1: gateway" in result.detail


def test_nonzero_exit_preserves_stdout_and_stderr(handlers, monkeypatch):
    monkeypatch.setattr(handlers, "run_command", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "1: gateway", "unreachable"))
    result = handlers.net_trace("example.com")
    assert not result.ok
    assert "gateway" in result.detail and "unreachable" in result.detail
