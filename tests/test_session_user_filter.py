"""桌面会话环境的多用户过滤回归测试。

sudo -A 拉起授权框的进程属于当前用户，会话环境必须取自当前用户的桌面会话，
不能把其它用户（不同 XDG_RUNTIME_DIR / 显示）的环境注入进来；否则授权框会
弹到错误的会话。root（uid 0）系统级 agent 无自有桌面会话，允许任意会话。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bibit_intent.execution.system_tools import (  # noqa: E402
    _session_belongs_to_user,
    _session_env_owner_uid,
)


def test_runtime_dir_owner_uid():
    assert _session_env_owner_uid({"XDG_RUNTIME_DIR": "/run/user/1000"}) == 1000
    assert _session_env_owner_uid({"XDG_RUNTIME_DIR": "/run/user/1001"}) == 1001
    assert _session_env_owner_uid({}) is None
    assert _session_env_owner_uid({"XDG_RUNTIME_DIR": "/tmp/run"}) is None


def test_session_belongs_to_same_user():
    env = {"XDG_RUNTIME_DIR": "/run/user/1000", "WAYLAND_DISPLAY": "wayland-0"}
    assert _session_belongs_to_user(env, proc_uid=1000, uid=1000) is True


def test_other_user_session_rejected_by_runtime_dir():
    env = {"XDG_RUNTIME_DIR": "/run/user/1001", "WAYLAND_DISPLAY": "wayland-1"}
    assert _session_belongs_to_user(env, proc_uid=1000, uid=1000) is False


def test_root_process_without_runtime_dir_rejected_for_user():
    # Xorg 常以 root 启动且无用户 XDG_RUNTIME_DIR：反推不出时看进程属主
    env = {"DISPLAY": ":1"}
    assert _session_belongs_to_user(env, proc_uid=0, uid=1000) is False
    assert _session_belongs_to_user(env, proc_uid=1000, uid=1000) is True


def test_root_uid_accepts_any_session():
    env = {"XDG_RUNTIME_DIR": "/run/user/1001", "DISPLAY": ":0"}
    assert _session_belongs_to_user(env, proc_uid=1001, uid=0) is True
    assert _session_belongs_to_user({}, proc_uid=0, uid=0) is True
