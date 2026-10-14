"""ubuntu_admin 在 no_new_privs 进程里的提权回归测试。

背景：端侧 bianbu-agent-runtime@.service 设了 NoNewPrivileges=true，Control、
它拉起的 Worker、以及 Worker 拉起的 bibit-intent MCP 子进程都继承该标志。
内核在 no_new_privs 置位时忽略 setuid 位，`sudo -A` 只会打印
``The "no new privileges" flag is set``，于是 MCP 路径下的 pkg_install /
dep_apply / install_local 等所有需要 root 的工具必然失败（实测：设备上
control pid 与 worker pid 的 NoNewPrivs 都是 1）。

修复：_run_sudo 检测到 no_new_privs 时，把单次「授权 + 执行命令」交给
systemd --user 的 transient unit（不继承该标志）执行；授权框与密码仍由用户
在桌面输入，行为与普通路径一致。sudo-rs 的 `sudo -A -v` 不会可靠缓存时间戳，
因此不先 `-v` 再执行，避免同一操作弹两次密码框。
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bibit_intent.execution.cancellation import EdgeExecutionCancelled
from bibit_intent.resources import resource_path


def _load_ubuntu_admin_handlers():
    path = resource_path("skills", "ubuntu_admin", "handlers.py")
    spec = importlib.util.spec_from_file_location("ubuntu_admin_handlers_nnp_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module.__name__] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def module(monkeypatch):
    mod = _load_ubuntu_admin_handlers()
    # 有一个真实桌面会话，才能走到执行分支。
    monkeypatch.setattr(mod, "_find_desktop_session_env", lambda: {"DISPLAY": ":0"})
    monkeypatch.setenv("SUDO_ASKPASS", "/bin/true")
    monkeypatch.setattr(
        mod, "_install_askpass_wrapper", lambda env, real: "/tmp/askpass-wrapper.sh"
    )
    monkeypatch.setattr(mod, "run_command", lambda *a, **k: None)
    return mod


def _status_file(tmp_path: Path, value: str) -> str:
    path = tmp_path / "status"
    path.write_text(f"Name:\tpython\nNoNewPrivs:\t{value}\n", encoding="utf-8")
    return str(path)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("0", False)],
)
def test_no_new_privs_active_reads_proc_status(module, tmp_path, value, expected):
    module._PROC_STATUS_PATH = _status_file(tmp_path, value)
    assert module._no_new_privs_active() is expected


def test_no_new_privs_active_missing_flag_is_false(module, tmp_path):
    path = tmp_path / "status"
    path.write_text("Name:\tpython\n", encoding="utf-8")
    module._PROC_STATUS_PATH = str(path)
    assert module._no_new_privs_active() is False


def test_no_new_privs_active_unreadable_path_is_false(module, tmp_path):
    module._PROC_STATUS_PATH = str(tmp_path / "missing")
    assert module._no_new_privs_active() is False


def test_user_manager_env_fills_runtime_dir_and_bus(module):
    module.os.environ.pop("XDG_RUNTIME_DIR", None)
    env = module._user_manager_env({})
    assert env["XDG_RUNTIME_DIR"] == f"/run/user/{module.os.getuid()}"
    assert env["DBUS_SESSION_BUS_ADDRESS"].startswith("unix:path=")


def test_user_manager_env_keeps_existing_values(module):
    env = module._user_manager_env(
        {"XDG_RUNTIME_DIR": "/run/user/42", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/bus"}
    )
    assert env["XDG_RUNTIME_DIR"] == "/run/user/42"
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/tmp/bus"


def test_run_sudo_uses_user_manager_when_no_new_privs(module):
    """NNP 置位 → 不直连 sudo，而是把授权 + 命令交给 systemd-run。"""
    module._no_new_privs_active = lambda: True
    calls = []

    def fake_run_command(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        return subprocess.CompletedProcess(cmd, 0, "done", "")

    module.run_command = fake_run_command
    target = ["apt-get", "install", "-y", "--", "cowsay"]
    r = module._run_sudo(target, timeout=600)

    assert r.ok is True and r.detail == "done"
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[0] == "systemd-run" and "--user" in argv
    assert "--wait" in argv and "--pipe" in argv
    assert any(item.startswith("--unit=bibit-sudo-") for item in argv)
    assert any(item.startswith("--setenv=SUDO_ASKPASS=") for item in argv)
    # 授权与目标命令都只在这个 unit 的 shim 里出现，没有裸 sudo 调用。
    assert argv[-len(target) :] == target
    assert argv[-len(target) - 4 : -len(target)] == [
        "sh", "/usr/bin/env", "TERM=dumb", "DEBIAN_FRONTEND=noninteractive",
    ]
    shim = argv[-len(target) - 5]
    assert shim == 'exec sudo -A "$@"\n'
    assert kwargs["timeout"] == 600
    assert kwargs["env"]["XDG_RUNTIME_DIR"]


def test_usr_mount_read_only_detects_readonly_mount(module, tmp_path, monkeypatch):
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "25 1 8:3 / / rw,noatime - ext4 /dev/sda3 rw\n"
        "26 25 8:3 /usr /usr ro,noatime - ext4 /dev/sda3 rw\n",
        encoding="utf-8",
    )
    contents = mountinfo.read_text()
    monkeypatch.setattr(module.Path, "read_text", lambda self, *a, **k: contents)
    assert module._usr_mount_read_only() is True


def test_usr_mount_read_only_treats_writable_root_as_writable(module, tmp_path, monkeypatch):
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "25 1 8:3 / / rw,noatime - ext4 /dev/sda3 rw\n",
        encoding="utf-8",
    )
    contents = mountinfo.read_text()
    monkeypatch.setattr(module.Path, "read_text", lambda self, *a, **k: contents)
    assert module._usr_mount_read_only() is False


def test_run_sudo_uses_user_manager_when_usr_is_readonly(module):
    module._no_new_privs_active = lambda: False
    module._usr_mount_read_only = lambda: True
    calls = []

    def fake_run_command(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "done", "")

    module.run_command = fake_run_command
    target = ["apt-get", "install", "-y", "--", "cowsay"]
    r = module._run_sudo(target, timeout=600)

    assert r.ok is True and r.detail == "done"
    assert len(calls) == 1
    assert calls[0][0] == "systemd-run"


def test_run_sudo_user_manager_auth_failure_marks_not_executed(module, monkeypatch):
    """授权失败从 stderr 识别：目标命令从未启动，撤回「已进入执行」标记。"""
    module._no_new_privs_active = lambda: True
    withdrawn = []
    monkeypatch.setattr(module, "end_execution_without_side_effect", lambda: withdrawn.append(True))
    module.run_command = lambda cmd, **kwargs: subprocess.CompletedProcess(
        cmd, 1, "", "sudo: 1 incorrect password attempt"
    )
    r = module._run_sudo(["apt-get", "install", "-y", "--", "cowsay"])
    assert r.ok is False
    assert r.message == "授权未完成，操作未执行"
    assert "incorrect password" in r.detail
    assert withdrawn == [True]


def test_run_sudo_user_manager_stops_unit_on_cancel(module):
    module._no_new_privs_active = lambda: True
    stopped = []
    module._stop_user_manager_unit = lambda unit, env: stopped.append(unit)

    def fake_run_command(cmd, **kwargs):
        raise EdgeExecutionCancelled("cancelled")

    module.run_command = fake_run_command
    with pytest.raises(EdgeExecutionCancelled):
        module._run_sudo(["apt-get", "install", "-y", "--", "cowsay"])
    assert len(stopped) == 1 and stopped[0].startswith("bibit-sudo-")


def test_run_sudo_user_manager_stops_unit_on_timeout(module):
    module._no_new_privs_active = lambda: True
    stopped = []
    module._stop_user_manager_unit = lambda unit, env: stopped.append(unit)

    def fake_run_command(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 60)

    module.run_command = fake_run_command
    r = module._run_sudo(["apt-get", "install", "-y", "--", "cowsay"], timeout=60)
    assert r.ok is False and "超时" in r.message
    assert len(stopped) == 1


def test_package_commands_use_host_even_without_sandbox_flags(module):
    """系统包操作始终走 host，不依赖 NNP 或只读挂载探测。"""
    module._no_new_privs_active = lambda: False
    module._usr_mount_read_only = lambda: False
    calls = []

    def fake_run_command(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    module.run_command = fake_run_command
    r = module._run_sudo(["apt-get", "install", "-y", "--", "cowsay"])
    assert r.ok is True
    assert len(calls) == 1
    assert calls[0][0] == "systemd-run"
    assert calls[0][-8:] == [
        "/usr/bin/env", "TERM=dumb", "DEBIAN_FRONTEND=noninteractive",
        "apt-get", "install", "-y", "--", "cowsay",
    ]


@pytest.mark.parametrize("no_new_privs", [False, True])
@pytest.mark.parametrize("command", ["apt", "apt-get", "dpkg", "/usr/bin/apt-get"])
def test_package_environment_is_set_after_sudo(module, monkeypatch, no_new_privs, command):
    monkeypatch.setenv("TERM", "qterminal")
    monkeypatch.setenv("DEBIAN_FRONTEND", "dialog")
    module._no_new_privs_active = lambda: no_new_privs
    module._usr_mount_read_only = lambda: False
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    module.run_command = fake_run
    target = [command, "--version"]
    assert module._run_sudo(target).ok
    expected = ["/usr/bin/env", "TERM=dumb", "DEBIAN_FRONTEND=noninteractive", *target]
    assert len(calls) == 1 and calls[0][0] == "systemd-run"
    assert calls[-1][-len(expected):] == expected
    assert target == [command, "--version"]
    assert module.os.environ["TERM"] == "qterminal"
    assert module.os.environ["DEBIAN_FRONTEND"] == "dialog"


@pytest.mark.parametrize("no_new_privs", [False, True])
def test_other_sudo_commands_keep_environment(module, no_new_privs):
    module._no_new_privs_active = lambda: no_new_privs
    module._usr_mount_read_only = lambda: False
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    module.run_command = fake_run
    target = ["npm", "install", "-g", "axios"]
    assert module._run_sudo(target).ok
    assert calls[-1][-len(target):] == target
    assert "/usr/bin/env" not in calls[-1]


def test_stop_user_manager_unit_swallows_cleanup_errors(module):
    def boom(cmd, **kwargs):
        raise FileNotFoundError("systemctl missing")

    module.run_command = boom
    module._stop_user_manager_unit("bibit-sudo-1-abcd", {})  # 不抛异常


@pytest.mark.parametrize("failure", [FileNotFoundError("missing systemd-run"), RuntimeError("manager unavailable")])
def test_host_package_failure_never_falls_back_to_direct_sudo(module, failure):
    module._no_new_privs_active = lambda: False
    module._usr_mount_read_only = lambda: False
    calls = []

    def unavailable(command, **kwargs):
        calls.append(command)
        raise failure

    module.run_command = unavailable
    if isinstance(failure, RuntimeError):
        with pytest.raises(RuntimeError, match="manager unavailable"):
            module._run_sudo(["apt-get", "install", "zenow"])
    else:
        assert not module._run_sudo(["apt-get", "install", "zenow"]).ok
    assert len(calls) == 1 and calls[0][0] == "systemd-run"
