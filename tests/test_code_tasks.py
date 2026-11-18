"""code_tasks 的端到端行为：用一个假 dsh 入口替身跑通子进程/产物/错误路径。

真 dsh 由 deepseek-harness 包提供（/usr/bin/dsh），开发机没有，所以这里把
DSH_BIN 指向一个可执行脚本，它按约定写文件、打印答案、按任务文本模拟失败。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

# dsh 0.2.x 的调用约定：<dsh> headless <task>
FAKE_DSH = """\
#!/usr/bin/env python3
import os, sys, pathlib

args = sys.argv[1:]
if len(args) < 2 or args[0] != "headless":
    sys.stderr.write("unexpected args\\n")
    sys.exit(2)
task = args[1]
if not os.environ.get("DEEPSEEK_API_KEY") or not os.environ.get("DEEPSEEK_BASE_URL"):
    sys.stderr.write("dsh: MISSING_CREDENTIAL\\n")
    sys.exit(1)
if "FAIL" in task:
    sys.stderr.write("dsh: SERVER: provider exploded\\n")
    sys.exit(1)
pathlib.Path("out.txt").write_text("task=" + task + "\\n", encoding="utf-8")
sys.stdout.write("DONE " + os.environ["DSH_HOME"] + " " + os.environ["TMPDIR"] + "\\n")
"""


@pytest.fixture()
def fake_dsh(tmp_path, monkeypatch):
    entry = tmp_path / "fakedsh" / "dsh"
    entry.parent.mkdir(parents=True)
    entry.write_text(FAKE_DSH, encoding="utf-8")
    entry.chmod(0o755)
    monkeypatch.setenv("BIBIT_CODE_DSH_BIN", str(entry))
    monkeypatch.setenv("BIBIT_CODE_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    import importlib

    from bibit_intent import code_tasks

    importlib.reload(code_tasks)
    yield code_tasks
    importlib.reload(code_tasks)


def _wait(task, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if task.status != "running":
            return task
        time.sleep(0.1)
    raise AssertionError("task did not finish")


def test_existing_workspace_is_used_and_changes_are_reported(fake_dsh, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    task = fake_dsh.REGISTRY.submit("写个文件", str(project))
    _wait(task)

    assert task.status == "done", task.error
    assert task.workspace == project
    assert (project / "out.txt").is_file()
    assert task.files == ["out.txt"]
    assert task.answer.startswith("DONE ")
    # dsh 的状态目录与溢出目录都必须落到专用根下，不能留在 /tmp
    assert str(fake_dsh.DSH_HOME) in task.answer
    assert str(fake_dsh.SPILL_ROOT) in task.answer


def test_no_workspace_creates_a_run_directory_under_runs(fake_dsh):
    task = fake_dsh.REGISTRY.submit("随便写点什么")
    _wait(task)

    assert task.status == "done", task.error
    assert task.workspace.parent.name == "runs"
    assert task.workspace.parent.parent == fake_dsh.WORKSPACE_ROOT


def test_failure_surfaces_the_last_stderr_line(fake_dsh, tmp_path):
    task = fake_dsh.REGISTRY.submit("请 FAIL", str(tmp_path))
    _wait(task)

    assert task.status == "failed"
    assert "provider exploded" in task.error


def test_missing_credentials_fail_loudly(fake_dsh, monkeypatch, tmp_path):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("BIBIT_CODE_BASE_URL", raising=False)
    monkeypatch.setattr(fake_dsh, "PROVIDER_YAML", Path("/nonexistent/provider.yaml"))
    task = fake_dsh.REGISTRY.submit("写个文件", str(tmp_path))
    _wait(task)

    assert task.status == "failed"
    assert "缺少模型端点或密钥" in task.error


def test_submit_rejects_a_missing_dsh_entry(fake_dsh, monkeypatch, tmp_path):
    monkeypatch.setattr(fake_dsh, "DSH_BIN", Path("/nonexistent/dsh"))
    with pytest.raises(fake_dsh.CodeTaskError):
        fake_dsh.REGISTRY.submit("写个文件", str(tmp_path))


@pytest.mark.parametrize(
    "given,expected",
    [
        # runtime provider 记录的是 OpenAI 兼容根，dsh 0.2.x 需要 Messages 根
        ("https://api.deepseek.com/v1", "https://api.deepseek.com/anthropic"),
        ("https://api.deepseek.com/v1/", "https://api.deepseek.com/anthropic"),
        ("https://api.deepseek.com", "https://api.deepseek.com/anthropic"),
        # 已经是 Messages 根就原样透传，不重复拼接
        ("https://api.deepseek.com/anthropic", "https://api.deepseek.com/anthropic"),
        ("https://api.deepseek.com/anthropic/", "https://api.deepseek.com/anthropic"),
        # 自建网关同样按 OpenAI 根重写
        ("http://10.0.0.5:8000/v1", "http://10.0.0.5:8000/anthropic"),
    ],
)
def test_messages_root_rewrites_openai_base(fake_dsh, given, expected):
    assert fake_dsh._messages_root(given) == expected
