"""锁屏：会话环境清理与「真的锁上」校验。"""
from types import SimpleNamespace
from unittest.mock import Mock

from bibit_intent.execution import system_tools as st


def _fake_session():
    return {"WAYLAND_DISPLAY": "wayland-0", "XDG_RUNTIME_DIR": "/run/user/1000"}


def test_lock_screen_reports_success_only_when_locker_alive(monkeypatch):
    monkeypatch.setattr(st, "_find_desktop_session_env", _fake_session)
    monkeypatch.setattr(st.subprocess, "Popen", Mock(return_value=SimpleNamespace(pid=1)))
    monkeypatch.setattr(st, "_process_name_running", lambda name: name == "gtklock")

    result = st.lock_screen()

    assert result.ok is True
    assert result.message == "已锁屏"


def test_lock_screen_reports_failure_when_locker_exits(monkeypatch):
    monkeypatch.setattr(st, "_find_desktop_session_env", _fake_session)
    monkeypatch.setattr(st.subprocess, "Popen", Mock(return_value=SimpleNamespace(pid=1)))
    monkeypatch.setattr(st, "_process_name_running", lambda name: False)
    monkeypatch.setattr(st, "_LOCK_VERIFY_TIMEOUT", 0.0)

    result = st.lock_screen()

    assert result.ok is False
    assert "锁屏未生效" in result.message
    assert result.escalate is None


def test_lock_screen_strips_stale_wayland_socket(monkeypatch):
    monkeypatch.setenv("WAYLAND_SOCKET", "74")
    session = dict(_fake_session(), WAYLAND_SOCKET="74")
    monkeypatch.setattr(st, "_find_desktop_session_env", lambda: session)
    fake_popen = Mock(return_value=SimpleNamespace(pid=1))
    monkeypatch.setattr(st.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(st, "_process_name_running", lambda name: True)

    st.lock_screen()

    _, kwargs = fake_popen.call_args
    # fd 编号只在持有它的进程里有效，绝不能透传给锁屏子进程
    assert "WAYLAND_SOCKET" not in kwargs["env"]
    assert kwargs["env"]["WAYLAND_DISPLAY"] == "wayland-0"


def test_lock_screen_reports_missing_command(monkeypatch):
    monkeypatch.setattr(st, "_find_desktop_session_env", _fake_session)
    monkeypatch.setattr(st.subprocess, "Popen", Mock(side_effect=FileNotFoundError()))

    result = st.lock_screen()

    assert result.ok is False
    assert "bianbu-gtklock" in result.message


def test_lock_screen_fails_without_desktop_session(monkeypatch):
    monkeypatch.setattr(st, "_find_desktop_session_env", lambda: None)

    result = st.lock_screen()

    assert result.ok is False
    assert "桌面会话" in result.message
