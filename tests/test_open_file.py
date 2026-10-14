"""open_file：本地文件/文档打开应走宿主桌面专用路径。"""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bibit_intent.execution import system_tools


def test_open_file_rejects_missing_path():
    result = system_tools.open_file("")
    assert result.ok is False
    assert "缺少文件路径" in result.message


def test_open_file_rejects_nonexistent_file(tmp_path: Path):
    result = system_tools.open_file(str(tmp_path / "missing.pptx"))
    assert result.ok is False
    assert "文件不存在" in result.message


def test_open_file_uses_host_desktop_for_worker(tmp_path: Path, monkeypatch):
    target = tmp_path / "休假.pptx"
    target.write_text("ppt", encoding="utf-8")
    calls: list[list[str]] = []

    monkeypatch.setenv("BIANBU_RUNTIME_ROLE", "agent-worker")
    monkeypatch.setattr(system_tools, "_host_worker_desktop_launch", lambda: True)
    monkeypatch.setattr(system_tools, "_find_desktop_session_env", lambda: {"DISPLAY": ":0"})
    monkeypatch.setattr(system_tools, "_default_open_argv", lambda file_path: None)
    monkeypatch.setattr(system_tools.shutil, "which", lambda name: "/usr/bin/xdg-open")

    def fake_host(argv, app_name, session_env):
        calls.append(list(argv))
        return system_tools.ExecResult(True, f"已启动应用: {app_name}")

    monkeypatch.setattr(system_tools, "_launch_host_desktop", fake_host)

    result = system_tools.open_file(str(target))
    assert result.ok is True
    assert calls == [["/usr/bin/xdg-open", str(target)]]
    assert "已打开文件" in result.message


def test_open_file_does_not_launch_in_sandbox_worker(tmp_path: Path, monkeypatch):
    target = tmp_path / "休假.pptx"
    target.write_text("ppt", encoding="utf-8")
    monkeypatch.setenv("BIANBU_RUNTIME_ROLE", "agent-worker")
    monkeypatch.setattr(system_tools, "_host_worker_desktop_launch", lambda: False)
    monkeypatch.setattr(system_tools.shutil, "which", lambda name: "/usr/bin/xdg-open")

    result = system_tools.open_file(str(target))
    assert result.ok is False
    assert result.escalate == "needs_host_channel"
