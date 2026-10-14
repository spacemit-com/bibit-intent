from types import SimpleNamespace
from unittest.mock import Mock
import os
import subprocess

import pytest

from bibit_intent.execution import system_tools as st


@pytest.fixture(autouse=True)
def clean_process_environment(monkeypatch):
    # A failed mock assertion must never print developer credentials.
    for key in list(st.os.environ):
        monkeypatch.delenv(key)


def host_worker(monkeypatch):
    monkeypatch.setenv("BIANBU_RUNTIME_ROLE", "agent-worker")
    monkeypatch.setenv("BIANBU_WORKER_EFFECTIVE_MODE", "host")
    monkeypatch.setenv("BIANBU_SANDBOX_ACTIVE", "0")


def test_host_launch_uses_user_service_with_account_home_and_minimal_env(monkeypatch):
    host_worker(monkeypatch)
    monkeypatch.setenv("HOME", "/container/home")
    monkeypatch.setenv("TEST_SECRET", "do-not-forward")
    monkeypatch.setattr(st.os, "getuid", lambda: 1000)
    import pwd
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir="/home/desktop"))
    run = Mock(return_value=SimpleNamespace(returncode=0, stderr=""))
    monkeypatch.setattr(st.subprocess, "run", run)
    direct = Mock()
    monkeypatch.setattr(st.subprocess, "Popen", direct)
    seen = {"count": 0}

    def fake_pids(name):
        if name != "qterminal":
            return set()
        seen["count"] += 1
        return {1234} if seen["count"] > 1 else set()

    monkeypatch.setattr(st, "_process_pids", fake_pids)
    result = st._launch_argv(["qterminal", "literal;$(touch nope)"], "终端", {
        "DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0", "HOME": "/wrong",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "XDG_CURRENT_DESKTOP": "LXQt:labwc:wlroots",
        "LC_ALL": "zh_CN.UTF-8", "TEST_SECRET": "secret",
    })
    assert result.ok
    direct.assert_not_called()
    command = run.call_args.args[0]
    assert command[0] == "/usr/bin/systemd-run"
    assert "--user" in command and "--scope" not in command
    assert "--service-type=exec" in command
    assert "--working-directory=/home/desktop" in command
    assert command[command.index("--") + 1:] == ["qterminal", "literal;$(touch nope)"]
    assert "--setenv=DISPLAY=:0" in command
    assert "--setenv=XDG_RUNTIME_DIR=/run/user/1000" in command
    assert "--setenv=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus" in command
    assert "--setenv=XDG_CURRENT_DESKTOP=LXQt:labwc:wlroots" in command
    assert "--setenv=LC_ALL=zh_CN.UTF-8" in command
    env = run.call_args.kwargs["env"]
    assert env["HOME"] == "/home/desktop"
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/1000/bus"
    assert "TEST_SECRET" not in env
    assert all("TEST_SECRET" not in item for item in command)


@pytest.mark.parametrize("mode,active", [("sandbox", "1"), ("host", "1"), ("", "0")])
def test_only_explicit_host_uses_desktop_service(monkeypatch, mode, active):
    host_worker(monkeypatch)
    monkeypatch.setenv("BIANBU_WORKER_EFFECTIVE_MODE", mode)
    monkeypatch.setenv("BIANBU_SANDBOX_ACTIVE", active)
    direct = Mock()
    monkeypatch.setattr(st.subprocess, "Popen", direct)
    manager = Mock()
    monkeypatch.setattr(st.subprocess, "run", manager)
    seen = {"count": 0}

    def fake_pids(name):
        seen["count"] += 1
        return {1234} if seen["count"] > 1 else set()

    monkeypatch.setattr(st, "_process_pids", fake_pids)
    assert st.launch_app("终端", effective_mode="host", env={"BIANBU_SANDBOX_ACTIVE": "0"}).ok
    direct.assert_called_once()
    manager.assert_not_called()


def test_host_without_supervisor_marker_fails_without_launch(monkeypatch):
    host_worker(monkeypatch)
    monkeypatch.delenv("BIANBU_SANDBOX_ACTIVE")
    direct = Mock()
    monkeypatch.setattr(st.subprocess, "Popen", direct)
    manager = Mock()
    monkeypatch.setattr(st.subprocess, "run", manager)
    result = st.launch_app("终端")
    assert not result.ok and "Runtime" in result.message
    direct.assert_not_called()
    manager.assert_not_called()


@pytest.mark.parametrize("failure", [FileNotFoundError(), subprocess.TimeoutExpired("systemd-run", 10), subprocess.CalledProcessError(1, "systemd-run", stderr="bus unavailable")])
def test_host_service_failure_is_clear_and_never_falls_back(monkeypatch, failure):
    host_worker(monkeypatch)
    monkeypatch.setattr(st, "_find_desktop_session_env", lambda: {"DISPLAY": ":0"})
    manager = Mock(side_effect=failure)
    monkeypatch.setattr(st.subprocess, "run", manager)
    direct = Mock()
    monkeypatch.setattr(st.subprocess, "Popen", direct)
    result = st.launch_app("终端")
    assert not result.ok
    assert "桌面" in result.message
    manager.assert_called_once()
    direct.assert_not_called()

def test_session_setenv_forwards_session_identity_but_not_secrets():
    args = st._session_setenv_args({
        "DISPLAY": ":0",
        "WAYLAND_DISPLAY": "wayland-0",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "DESKTOP_SESSION": "bianbu-lite",
        "LANG": "zh_CN.UTF-8",
        "LC_CTYPE": "zh_CN.UTF-8",
        "TEST_SECRET": "do-not-forward",
    })
    assert "--setenv=XDG_RUNTIME_DIR=/run/user/1000" in args
    assert "--setenv=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus" in args
    assert "--setenv=DESKTOP_SESSION=bianbu-lite" in args
    assert "--setenv=LANG=zh_CN.UTF-8" in args
    assert "--setenv=LC_CTYPE=zh_CN.UTF-8" in args
    assert all("TEST_SECRET" not in item for item in args)


def test_expected_process_name_skips_wrappers():
    assert st._expected_process_name(["/usr/bin/chromium", "--flag"]) == "chromium"
    assert st._expected_process_name(["env", "QT_QPA_PLATFORM=wayland", "/usr/bin/qterminal"]) == "qterminal"
    assert st._expected_process_name(["sh", "-c", "exec /usr/bin/xterm -e vim"]) == "xterm"
    assert st._expected_process_name([]) is None


def test_process_name_running_handles_rewritten_cmdline(monkeypatch, tmp_path):
    # Chromium rewrites its cmdline via prctl: the whole command line becomes a
    # single space-separated argv[0] with no NUL separators.
    proc = tmp_path / "1234"
    proc.mkdir()
    (proc / "cmdline").write_bytes(
        b"/usr/lib/chromium/chromium --ozone-platform=wayland --enable-gpu"
    )
    monkeypatch.setattr(st.glob, "glob", lambda pattern: [str(proc)])
    assert st._process_name_running("chromium")
    assert not st._process_name_running("qterminal")


def test_process_name_running_falls_back_to_exe_link(monkeypatch, tmp_path):
    proc = tmp_path / "4321"
    proc.mkdir()
    (proc / "cmdline").write_bytes(b"")
    binary = tmp_path / "usr" / "bin" / "qterminal"
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    (proc / "exe").symlink_to(binary)
    monkeypatch.setattr(st.glob, "glob", lambda pattern: [str(proc)])
    assert st._process_name_running("qterminal")
    assert not st._process_name_running("chromium")


def test_verify_launch_fails_when_process_never_appears(monkeypatch):
    monkeypatch.setattr(st, "_LAUNCH_VERIFY_TIMEOUT", 0.0)
    monkeypatch.setattr(st, "_process_name_running", lambda name: False)
    result = st._verify_launch(["/usr/bin/chromium"], "浏览器")
    assert not result.ok
    assert result.attempted is True
    assert "浏览器" in result.message
    assert result.escalate is None


def test_verify_launch_passes_when_process_running(monkeypatch):
    monkeypatch.setattr(st, "_process_name_running", lambda name: name == "qterminal")
    result = st._verify_launch(["/usr/bin/qterminal"], "终端")
    assert result.ok and result.attempted is True


def test_verify_launch_rejects_existing_process_without_new_pid(monkeypatch):
    monkeypatch.setattr(st, "_LAUNCH_VERIFY_TIMEOUT", 0.0)
    monkeypatch.setattr(st, "_process_pids", lambda name: {10})
    result = st._verify_launch(["/usr/bin/chromium"], "浏览器", before_pids={10})
    assert not result.ok
    assert result.attempted is True
    assert "浏览器" in result.message


def test_verify_launch_accepts_new_pid_after_launch(monkeypatch):
    monkeypatch.setattr(st, "_LAUNCH_VERIFY_TIMEOUT", 0.0)
    monkeypatch.setattr(st, "_process_pids", lambda name: {11})
    result = st._verify_launch(["/usr/bin/chromium"], "浏览器", before_pids={10})
    assert result.ok and result.attempted is True


def test_find_desktop_session_env_merges_same_session(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    compositor = tmp_path / "p1"
    compositor.mkdir()
    (compositor / "cmdline").write_bytes(b"labwc\0")
    (compositor / "environ").write_bytes(
        b"XDG_RUNTIME_DIR=" + str(runtime).encode()
        + b"\0WAYLAND_DISPLAY=wayland-0\0LANG=zh_CN.UTF-8\0"
    )
    xwayland = tmp_path / "p2"
    xwayland.mkdir()
    (xwayland / "cmdline").write_bytes(b"Xwayland\0")
    (xwayland / "environ").write_bytes(
        b"XDG_RUNTIME_DIR=" + str(runtime).encode()
        + b"\0DISPLAY=:0\0XAUTHORITY=/home/bianbu/.Xauthority\0"
    )
    monkeypatch.setattr(st.glob, "glob", lambda pattern: [str(compositor), str(xwayland)])
    env = st._find_desktop_session_env(uid=os.getuid())
    assert env is not None
    assert env["WAYLAND_DISPLAY"] == "wayland-0"
    assert env["LANG"] == "zh_CN.UTF-8"
    assert env["DISPLAY"] == ":0"
    assert env["XAUTHORITY"] == "/home/bianbu/.Xauthority"
    assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={runtime}/bus"


def test_find_desktop_session_env_drops_fd_bound_wayland_socket(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    compositor = tmp_path / "p1"
    compositor.mkdir()
    (compositor / "cmdline").write_bytes(b"labwc\0")
    (compositor / "environ").write_bytes(
        b"XDG_RUNTIME_DIR=" + str(runtime).encode() + b"\0WAYLAND_DISPLAY=wayland-0\0"
    )
    xwayland = tmp_path / "p2"
    xwayland.mkdir()
    (xwayland / "cmdline").write_bytes(b"Xwayland\0")
    # Xwayland 带着已连接 fd 的编号，只对 Xwayland 自己有效
    (xwayland / "environ").write_bytes(
        b"XDG_RUNTIME_DIR=" + str(runtime).encode() + b"\0DISPLAY=:0\0WAYLAND_SOCKET=74\0"
    )
    monkeypatch.setattr(st.glob, "glob", lambda pattern: [str(compositor), str(xwayland)])

    env = st._find_desktop_session_env(uid=os.getuid())

    assert env["WAYLAND_DISPLAY"] == "wayland-0"
    assert "WAYLAND_SOCKET" not in env


def test_gui_child_env_strips_stale_wayland_socket(monkeypatch):
    monkeypatch.setenv("WAYLAND_SOCKET", "74")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-9")

    env = st._gui_child_env({"WAYLAND_DISPLAY": "wayland-0"})

    assert "WAYLAND_SOCKET" not in env
    assert env["WAYLAND_DISPLAY"] == "wayland-0"


def test_launch_app_reports_verify_failure_not_app_not_found(monkeypatch):
    monkeypatch.setenv("BIANBU_RUNTIME_ROLE", "agent-worker")
    monkeypatch.setenv("BIANBU_WORKER_EFFECTIVE_MODE", "host")
    monkeypatch.setenv("BIANBU_SANDBOX_ACTIVE", "0")
    monkeypatch.setattr(st, "_find_desktop_session_env", lambda: {"DISPLAY": ":0"})
    monkeypatch.setattr(st.subprocess, "run", Mock(return_value=SimpleNamespace(returncode=0, stderr="")))
    monkeypatch.setattr(st, "_LAUNCH_VERIFY_TIMEOUT", 0.0)
    monkeypatch.setattr(st, "_process_pids", lambda name: set())
    result = st.launch_app("终端")
    assert not result.ok
    assert "未检测到" in result.message
    assert result.escalate is None
