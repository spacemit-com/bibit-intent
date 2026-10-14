"""ubuntu_admin sudo GUI askpass 无图形会话回归测试。

背景：agent 服务常驻 systemd（user 级）或跑在 SSH 等无图形环境，环境里的
DISPLAY/WAYLAND_DISPLAY 可能是残留（缺 XAUTHORITY/XDG_RUNTIME_DIR），卸载/
安装等需要 root 的操作走 _run_sudo 时会拉起 Qt 版 ssh-askpass，连不上显示
会话时打一串 qt.qpa 报错且授权框弹不出来。
修复：_session_askpass_env 从真实桌面会话进程读环境（system_tools 既有做法），
找不到可用会话时不启动 askpass，返回明确错误并置 escalate 升级云端。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bibit_intent.resources import resource_path
from bibit_intent.execution.cancellation import (
    EdgeExecutionCancelled,
    EdgeExecutionControl,
    cancellation_scope,
)


def _load_ubuntu_admin_handlers():
    path = resource_path("skills", "ubuntu_admin", "handlers.py")
    spec = importlib.util.spec_from_file_location("ubuntu_admin_handlers_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module.__name__] = module
    spec.loader.exec_module(module)
    return module


def test_no_gui_skips_sudo_askpass(monkeypatch):
    """无桌面会话且无显示变量 → 不启动 askpass，干净报错 + escalate。"""
    module = _load_ubuntu_admin_handlers()
    monkeypatch.setattr(module, "_find_desktop_session_env", lambda: None)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    calls = []
    monkeypatch.setattr(
        module, "run_command",
        lambda *args, **kwargs: calls.append((args, kwargs)) or None,
    )
    r = module._run_sudo(["apt-get", "remove", "-y", "bibit-intent"])
    assert calls == []
    assert r.ok is False
    assert r.escalate == "no_gui_askpass"
    assert "图形" in r.message


def test_stale_display_without_auth_skips_sudo(monkeypatch, tmp_path):
    """环境残留 DISPLAY 但找不到会话进程、X11 又无 XAUTHORITY → 不放行。"""
    module = _load_ubuntu_admin_handlers()
    monkeypatch.setattr(module, "_find_desktop_session_env", lambda: None)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("XAUTHORITY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # tmp_path 下无 .Xauthority
    calls = []
    monkeypatch.setattr(module, "run_command", lambda *a, **k: calls.append(1))
    r = module._run_sudo(["apt-get", "remove", "-y", "vim"])
    assert calls == []
    assert r.ok is False
    assert r.escalate == "no_gui_askpass"


def test_stale_wayland_socket_missing_skips_sudo(monkeypatch, tmp_path):
    """环境残留 WAYLAND_DISPLAY（绝对路径）但 socket 不存在 → 不放行。"""
    module = _load_ubuntu_admin_handlers()
    monkeypatch.setattr(module, "_find_desktop_session_env", lambda: None)
    sock = str(tmp_path / "wayland-0")
    monkeypatch.setenv("WAYLAND_DISPLAY", sock)
    monkeypatch.delenv("DISPLAY", raising=False)
    calls = []
    monkeypatch.setattr(module, "run_command", lambda *a, **k: calls.append(1))
    r = module._run_sudo(["apt-get", "remove", "-y", "vim"])
    assert calls == []
    assert r.ok is False
    assert r.escalate == "no_gui_askpass"


def test_session_found_runs_sudo_with_session_env(monkeypatch, tmp_path):
    """真实桌面会话存在 → 注入会话环境、装 askpass 包装脚本并执行 sudo -A。

    sudo -A 会按 env_reset 清空 askpass 环境（QT_*/主题/显示变量全丢），
    SUDO_ASKPASS 必须指向包装脚本，脚本先恢复会话/主题环境再 exec askpass。
    主题变量只存在于会话进程环境里（systemd user 服务不带），必须整体继承
    会话环境（而不是只挑显示相关几个 key）。
    """
    module = _load_ubuntu_admin_handlers()
    monkeypatch.setattr(
        module,
        "_find_desktop_session_env",
        lambda: {
            "XDG_RUNTIME_DIR": str(tmp_path),
            "WAYLAND_DISPLAY": "wayland-0",
            "HOME": "/home/bianbu",
            "QT_QPA_PLATFORMTHEME": "lxqt",
            "QT_PLATFORM_PLUGIN": "lxqt",
            "LXQT_SESSION_CONFIG": "session",
            "XDG_CURRENT_DESKTOP": "LXQt:labwc:wlroots",
        },
    )
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("SUDO_ASKPASS", "/bin/true")

    class Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    calls = []
    monkeypatch.setattr(module, "run_command", lambda *a, **k: calls.append(k) or Proc())
    r = module._run_sudo(["apt-get", "remove", "-y", "vim"])
    assert r.ok is True
    env = calls[0]["env"]
    wrapper = Path(tmp_path) / "bibit-askpass" / "askpass-wrapper.sh"
    assert env.get("SUDO_ASKPASS") == str(wrapper)
    assert env.get("WAYLAND_DISPLAY") == "wayland-0"
    assert env.get("XDG_RUNTIME_DIR") == str(tmp_path)
    assert wrapper.is_file()
    text = wrapper.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh")
    assert "exec '/usr/bin/true'" in text
    env_text = (Path(tmp_path) / "bibit-askpass" / "env").read_text(encoding="utf-8")
    assert "export QT_QPA_PLATFORMTHEME=" in env_text
    assert "export QT_PLATFORM_PLUGIN=" in env_text
    assert "export LXQT_SESSION_CONFIG=" in env_text
    assert "export XDG_CURRENT_DESKTOP=" in env_text
    assert "export WAYLAND_DISPLAY=" in env_text


def test_gui_present_but_askpass_missing(monkeypatch):
    module = _load_ubuntu_admin_handlers()
    monkeypatch.setattr(
        module,
        "_find_desktop_session_env",
        lambda: {"XDG_RUNTIME_DIR": "/run/user/1000", "WAYLAND_DISPLAY": "wayland-0"},
    )
    monkeypatch.setenv("SUDO_ASKPASS", "/nonexistent-askpass")
    calls = []
    monkeypatch.setattr(module, "run_command", lambda *a, **k: calls.append(1))
    r = module._run_sudo(["apt-get", "remove", "-y", "vim"])
    assert calls == []
    assert r.escalate == "no_gui_askpass"


def _stub_gui_session(monkeypatch, module, tmp_path):
    monkeypatch.setattr(
        module,
        "_find_desktop_session_env",
        lambda: {"XDG_RUNTIME_DIR": str(tmp_path), "WAYLAND_DISPLAY": "wayland-0"},
    )
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("SUDO_ASKPASS", "/bin/true")


def test_cancel_host_package_operation_preserves_uncertain_state(monkeypatch, tmp_path):
    """host unit 已发起时不能假定取消发生在授权前，停止 unit 并保留不确定状态。"""
    module = _load_ubuntu_admin_handlers()
    _stub_gui_session(monkeypatch, module, tmp_path)
    control = EdgeExecutionControl()
    control.execution_started.set()
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        raise EdgeExecutionCancelled("edge execution cancelled")

    monkeypatch.setattr(module, "run_command", fake_run)
    with cancellation_scope(control):
        with pytest.raises(EdgeExecutionCancelled):
            module._run_sudo(["apt-get", "install", "-y", "--", "cowsay"], timeout=600)

    assert calls[0][0] == "systemd-run"
    assert calls[1][:3] == ["systemctl", "--user", "stop"]
    assert control.execution_started.is_set() is True


def test_authorization_denied_skips_target_command(monkeypatch, tmp_path):
    module = _load_ubuntu_admin_handlers()
    _stub_gui_session(monkeypatch, module, tmp_path)
    control = EdgeExecutionControl()
    control.execution_started.set()
    calls: list[list[str]] = []

    class Denied:
        returncode = 1
        stdout = ""
        stderr = "sudo: 1 incorrect password attempt"

    monkeypatch.setattr(
        module, "run_command",
        lambda cmd, **kwargs: calls.append(list(cmd)) or Denied(),
    )
    with cancellation_scope(control):
        r = module._run_sudo(["apt-get", "install", "-y", "--", "cowsay"], timeout=600)

    assert r.ok is False
    assert "未执行" in r.message
    assert len(calls) == 1 and calls[0][0] == "systemd-run"
    assert calls[0][-5:] == ["apt-get", "install", "-y", "--", "cowsay"]
    assert control.execution_started.is_set() is False


def test_authorization_timeout_reports_not_executed(monkeypatch, tmp_path):
    module = _load_ubuntu_admin_handlers()
    _stub_gui_session(monkeypatch, module, tmp_path)
    control = EdgeExecutionControl()
    control.execution_started.set()
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        raise module.subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1))

    monkeypatch.setattr(module, "run_command", fake_run)
    with cancellation_scope(control):
        r = module._run_sudo(["apt-get", "install", "-y", "--", "cowsay"], timeout=600)

    assert r.ok is False
    assert "超时" in r.message
    assert calls[0][0] == "systemd-run"
    assert calls[1][:3] == ["systemctl", "--user", "stop"]
    # 目标命令已经发起，超时可能留下部分副作用，执行标记必须保持置位。
    assert control.execution_started.is_set() is True


def test_cancel_after_authorization_still_propagates(monkeypatch, tmp_path):
    """授权已完成后的取消仍按"可能已改动"处理（标记保持置位）。"""
    module = _load_ubuntu_admin_handlers()
    _stub_gui_session(monkeypatch, module, tmp_path)
    control = EdgeExecutionControl()
    control.execution_started.set()
    calls: list[list[str]] = []

    class Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if list(cmd) == ["sudo", "-n", "true"]:
            return Ok()
        raise EdgeExecutionCancelled("edge execution cancelled")

    monkeypatch.setattr(module, "run_command", fake_run)
    with cancellation_scope(control):
        with pytest.raises(EdgeExecutionCancelled):
            module._run_sudo(["apt-get", "install", "-y", "--", "cowsay"], timeout=600)

    assert calls[0][0] == "systemd-run"
    assert calls[0][-8:] == [
        "/usr/bin/env", "TERM=dumb", "DEBIAN_FRONTEND=noninteractive",
        "apt-get", "install", "-y", "--", "cowsay",
    ]
    assert calls[1][:3] == ["systemctl", "--user", "stop"]
    assert control.execution_started.is_set() is True
