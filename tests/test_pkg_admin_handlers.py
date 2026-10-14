"""S3 软件安装与更新 handler 行为回归测试。

覆盖本轮优化：
  - _is_installed 改为单包 dpkg-query（不再全量 apt list --installed）；
  - pkg_install / pkg_remove 装/卸后自动回读验证并汇总；
  - pkg_restore_selections 必须走 sudo + stdin（dpkg --set-selections 写
    /var/lib/dpkg/status，普通用户无权限）。
"""
from __future__ import annotations

import importlib.util
import sys
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bibit_intent.resources import resource_path
from bibit_intent.core import package_state


@pytest.fixture(autouse=True)
def absent_package_state(monkeypatch):
    monkeypatch.setattr(package_state, "query_package_state", lambda pkg: package_state.PackageState("absent"))


def _load_handlers():
    spec = importlib.util.spec_from_file_location(
        "_test_pkg_admin_handlers",
        resource_path("skills", "ubuntu_admin", "handlers.py"),
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module.__name__] = module
    spec.loader.exec_module(module)
    return module


class Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_is_installed_uses_single_dpkg_query(monkeypatch):
    module = _load_handlers()
    calls = []
    monkeypatch.setattr(
        module,
        "run_command",
        lambda *a, **k: calls.append((a, k)) or Proc(0, "ii"),
    )
    assert module._is_installed("vim") is True
    assert calls[0][0] == (["dpkg-query", "-W", "-f=${db:Status-Abbrev}", "vim"],)
    assert calls[0][1] == {"timeout": 15}


def test_is_installed_false_on_rc_or_missing(monkeypatch):
    module = _load_handlers()
    monkeypatch.setattr(module, "run_command", lambda *a, **k: Proc(0, "rc"))
    assert module._is_installed("vim") is False
    monkeypatch.setattr(module, "run_command", lambda *a, **k: Proc(1, ""))
    assert module._is_installed("vim") is False


def test_installed_version(monkeypatch):
    module = _load_handlers()
    monkeypatch.setattr(module, "run_command", lambda *a, **k: Proc(0, "1.2.3"))
    assert module._installed_version("vim") == "1.2.3"
    monkeypatch.setattr(module, "run_command", lambda *a, **k: Proc(1, "no packages found"))
    assert module._installed_version("vim") == ""


def test_pkg_install_skips_when_already_installed(monkeypatch):
    module = _load_handlers()
    monkeypatch.setattr(module, "_is_installed", lambda pkg: True)
    monkeypatch.setattr(package_state, "query_package_state", lambda pkg: package_state.PackageState("installed", "9.0"))
    monkeypatch.setattr(module, "_run_sudo", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应 sudo")))
    r = module.pkg_install(pkg="vim")
    assert r.ok is True
    assert "已安装" in r.message


def test_pkg_install_success_with_post_verify(monkeypatch):
    module = _load_handlers()
    states = iter([True])
    calls = []

    def fake_installed(pkg):
        return next(states)

    def fake_sudo(cmd, timeout=600, input=None, on_output=None):
        calls.append(cmd)
        return module.ExecResult(True, "ok", "apt detail")

    monkeypatch.setattr(module, "_is_installed", fake_installed)
    monkeypatch.setattr(module, "_installed_version", lambda pkg: "9.0")
    monkeypatch.setattr(module, "_run_sudo", fake_sudo)
    r = module.pkg_install(pkg="vim")
    assert r.ok is True
    assert calls == [["apt-get", "install", "-y", "--no-remove", "--", "vim"]]
    assert "vim（版本 9.0） 安装完成" in r.message


def test_pkg_install_ok_but_not_detected(monkeypatch):
    module = _load_handlers()
    monkeypatch.setattr(module, "_is_installed", lambda pkg: False)
    monkeypatch.setattr(module, "_run_sudo", lambda cmd, timeout=600, input=None, on_output=None: module.ExecResult(True, "ok", ""))
    r = module.pkg_install(pkg="vim")
    assert r.ok is False
    assert "未检测到" in r.message


def test_pkg_install_blocks_explicit_downgrade_without_confirmation(monkeypatch):
    module = _load_handlers()
    monkeypatch.setattr(package_state, "query_package_state", lambda pkg: package_state.PackageState("installed", "3.0"))
    monkeypatch.setattr(module, "_dpkg_version_relation", lambda candidate, installed: "lt")
    monkeypatch.setattr(module, "_downgrade_dialog", lambda *args: None)
    monkeypatch.setattr(module, "_run_sudo", lambda *args, **kwargs: pytest.fail("未确认前不应执行 apt"))
    result = module.pkg_install(pkg="demo=2.0")
    assert result.ok is False
    assert result.need_param == "allow_downgrade"
    assert result.confirm is True
    assert "允许降级" in result.message


def test_pkg_install_allows_explicit_downgrade_and_passes_flag(monkeypatch):
    module = _load_handlers()
    calls = []
    monkeypatch.setattr(package_state, "query_package_state", lambda pkg: package_state.PackageState("installed", "3.0"))
    monkeypatch.setattr(module, "_dpkg_repair_plan", lambda: (None, None))
    monkeypatch.setattr(module, "_dpkg_version_relation", lambda candidate, installed: "eq" if candidate == installed else "lt")
    monkeypatch.setattr(module, "_installed_version", lambda pkg: "2.0")
    monkeypatch.setattr(
        module,
        "_run_sudo",
        lambda cmd, timeout=600, input=None, on_output=None: calls.append(cmd) or module.ExecResult(True, "ok", ""),
    )
    result = module.pkg_install(pkg="demo=2.0", allow_downgrade=True)
    assert result.ok is True
    assert calls[0] == ["apt-get", "install", "-y", "--no-remove", "--allow-downgrades", "--", "demo=2.0"]
    assert "demo（版本 2.0） 安装完成" in result.message
    assert "3.0 → 2.0" in result.message


def test_pkg_install_batches_multiple_packages_into_single_apt(monkeypatch):
    module = _load_handlers()
    calls = []
    monkeypatch.setattr(module, "_is_installed", lambda pkg: True)
    monkeypatch.setattr(module, "_installed_version", lambda pkg: "")
    monkeypatch.setattr(
        module,
        "_run_sudo",
        lambda cmd, timeout=600, input=None, on_output=None: calls.append(cmd) or module.ExecResult(True, "ok", ""),
    )
    r = module.pkg_install(pkg="stress-ng iperf3")
    assert r.ok is True
    assert calls == [
        ["apt-get", "install", "-y", "--no-remove", "--", "stress-ng", "iperf3"]
    ]
    assert "stress-ng、iperf3 安装完成" in r.message


def test_pkg_install_recovers_incomplete_with_single_fix_broken_command(monkeypatch):
    module = _load_handlers()
    states = {
        "iperf3": package_state.PackageState("incomplete", "3.20-2", detail="iF "),
        "stress-ng": package_state.PackageState("absent"),
    }
    calls = []
    monkeypatch.setattr(package_state, "query_package_state", lambda pkg: states[pkg])
    monkeypatch.setattr(module, "_is_installed", lambda pkg: True)
    monkeypatch.setattr(module, "_installed_version", lambda pkg: "")
    monkeypatch.setattr(
        module,
        "_run_sudo",
        lambda cmd, timeout=600, input=None, on_output=None: calls.append(cmd) or module.ExecResult(True, "ok", ""),
    )
    r = module.pkg_install(pkg="iperf3 stress-ng")
    assert r.ok is True
    assert calls == [
        [
            "apt-get",
            "install",
            "-y",
            "--no-remove",
            "--fix-broken",
            "--",
            "iperf3",
            "stress-ng",
        ]
    ]
    assert "已恢复半安装状态" in r.message


def test_pkg_remove_success_verified(monkeypatch):
    module = _load_handlers()
    calls = []
    states = iter([package_state.PackageState("installed", "9.0"), package_state.PackageState("config_only")])
    monkeypatch.setattr(package_state, "query_package_state", lambda pkg: next(states))
    monkeypatch.setattr(module, "_is_installed", lambda pkg: False)
    monkeypatch.setattr(
        module,
        "_run_sudo",
        lambda cmd, timeout=600, input=None: calls.append(cmd) or module.ExecResult(True, "ok", ""),
    )
    r = module.pkg_remove(pkg="vim")
    assert r.ok is True
    assert calls == [["apt-get", "remove", "-y", "--", "vim"]]
    assert "vim 已卸载" in r.message


def test_pkg_remove_forces_half_installed_package(monkeypatch):
    module = _load_handlers()
    calls = []
    states = iter([package_state.PackageState("incomplete", "3.20-2", detail="iF "), package_state.PackageState("absent")])
    monkeypatch.setattr(
        package_state,
        "query_package_state",
        lambda pkg: next(states),
    )
    monkeypatch.setattr(module, "_is_installed", lambda pkg: False)
    monkeypatch.setattr(
        module,
        "_run_sudo",
        lambda cmd, timeout=600, input=None: calls.append(cmd) or module.ExecResult(True, "ok", ""),
    )
    r = module.pkg_remove(pkg="iperf3")
    assert r.ok is True
    assert calls == [["dpkg", "--remove", "--force-remove-reinstreq", "--", "iperf3"]]
    assert "iperf3 已卸载" in r.message


def test_pkg_remove_unknown_state_blocks_sudo(monkeypatch):
    module = _load_handlers()
    monkeypatch.setattr(
        package_state,
        "query_package_state",
        lambda pkg: package_state.PackageState("unknown", detail="database is unreadable"),
    )
    monkeypatch.setattr(module, "_run_sudo", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应 sudo")))
    r = module.pkg_remove(pkg="vim")
    assert r.ok is False
    assert "状态查询失败" in r.message


def test_pkg_install_blocks_apt_action_suffix_when_exact_candidate_is_unknown(monkeypatch):
    module = _load_handlers()
    monkeypatch.setattr(module, "_is_installed", lambda pkg: False)
    monkeypatch.setattr(module, "apt_install_target", lambda pkg: None)
    monkeypatch.setattr(module, "_run_sudo", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应执行")))
    result = module.pkg_install(pkg="git-")
    assert not result.ok and "操作后缀" in result.message


def test_pkg_remove_ok_but_still_installed(monkeypatch):
    module = _load_handlers()
    monkeypatch.setattr(package_state, "query_package_state", lambda pkg: package_state.PackageState("installed", "9.0"))
    monkeypatch.setattr(module, "_is_installed", lambda pkg: True)
    monkeypatch.setattr(module, "_run_sudo", lambda cmd, timeout=600, input=None: module.ExecResult(True, "ok", ""))
    r = module.pkg_remove(pkg="vim")
    assert r.ok is False
    assert "仍处于已安装状态" in r.message


def test_restore_selections_uses_sudo_with_stdin(monkeypatch, tmp_path):
    module = _load_handlers()
    f = tmp_path / "selections.txt"
    f.write_text("vim install\nhtop install\n", encoding="utf-8")
    calls = []

    def fake_sudo(cmd, timeout=120, input=None):
        calls.append((cmd, input))
        return module.ExecResult(True, "ok", "")

    monkeypatch.setattr(module, "_run_sudo", fake_sudo)
    r = module.pkg_restore_selections(file=str(f))
    assert r.ok is True
    assert calls == [(["dpkg", "--set-selections"], "vim install\nhtop install\n")]
    assert "dselect-upgrade" in r.message


def test_restore_selections_read_error(monkeypatch, tmp_path):
    module = _load_handlers()
    monkeypatch.setattr(module, "_run_sudo", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应 sudo")))
    r = module.pkg_restore_selections(file=str(tmp_path / "missing.txt"))
    assert r.ok is False
    assert "读取备份文件失败" in r.message


def test_restore_selections_sudo_failure_propagates(monkeypatch, tmp_path):
    module = _load_handlers()
    f = tmp_path / "selections.txt"
    f.write_text("vim install\n", encoding="utf-8")
    monkeypatch.setattr(
        module,
        "_run_sudo",
        lambda cmd, timeout=120, input=None: module.ExecResult(False, "命令失败(码1)", "denied"),
    )
    r = module.pkg_restore_selections(file=str(f))
    assert r.ok is False
    assert "命令失败" in r.message


def test_pkg_install_apt_not_found_returns_near_miss(monkeypatch):
    module = _load_handlers()
    monkeypatch.setattr(module, "_is_installed", lambda pkg: False)
    monkeypatch.setattr(
        module,
        "_run_sudo",
        lambda cmd, timeout=600, input=None, on_output=None: module.ExecResult(
            False, "命令失败(码100)", "E: 无法定位软件包 vims"
        ),
    )
    monkeypatch.setattr(module, "suggest_install_packages", lambda pkg: ["vim", "vim-tiny"])
    r = module.pkg_install(pkg="vims")
    assert r.ok is False
    assert "没有找到软件包" in r.message and "vim" in r.message
    assert "相近的软件包" in r.message and "vim-tiny" in r.message


def test_pkg_remove_apt_not_found_returns_near_miss(monkeypatch):
    module = _load_handlers()
    # 卸载路径只有在 dpkg 认为已装、apt 却报找不到包时才触发兜底提示
    monkeypatch.setattr(package_state, "query_package_state", lambda pkg: package_state.PackageState("installed", "9.0"))
    monkeypatch.setattr(module, "_is_installed", lambda pkg: True)
    monkeypatch.setattr(
        module,
        "_run_sudo",
        lambda cmd, timeout=600, input=None, on_output=None: module.ExecResult(
            False, "命令失败(码100)", "E: 无法定位软件包 vims"
        ),
    )
    monkeypatch.setattr(module, "suggest_install_packages", lambda pkg: ["vim"])
    r = module.pkg_remove(pkg="vims")
    assert r.ok is False
    assert "没有找到软件包" in r.message and "如需卸载" in r.message


def test_pkg_install_other_apt_error_keeps_original(monkeypatch):
    module = _load_handlers()
    monkeypatch.setattr(module, "_is_installed", lambda pkg: False)
    called = []
    monkeypatch.setattr(
        module,
        "suggest_install_packages",
        lambda pkg: called.append(pkg) or ["vim"],
    )
    monkeypatch.setattr(
        module,
        "_run_sudo",
        lambda cmd, timeout=600, input=None, on_output=None: module.ExecResult(
            False, "命令失败(码100)", "dpkg 被中断，您必须手动运行 sudo dpkg --configure -a"
        ),
    )
    r = module.pkg_install(pkg="vim")
    assert r.ok is False
    assert r.message == "命令失败(码100)"
    assert called == []


def test_pkg_install_apt_not_found_without_suggestions(monkeypatch):
    module = _load_handlers()
    monkeypatch.setattr(module, "_is_installed", lambda pkg: False)
    monkeypatch.setattr(module, "suggest_install_packages", lambda pkg: [])
    monkeypatch.setattr(
        module,
        "_run_sudo",
        lambda cmd, timeout=600, input=None, on_output=None: module.ExecResult(
            False, "命令失败(码100)", "E: 无法定位软件包 vims"
        ),
    )
    r = module.pkg_install(pkg="vims")
    assert r.ok is False
    assert r.message == "命令失败(码100)"


def test_upgradable_packages_parses_inst_lines_and_strips_arch(monkeypatch):
    module = _load_handlers()
    output = "\n".join([
        "Reading package lists...",
        "Inst linux-image:riscv64 [1] (2 localhost)",
        "Inst bibit-intent:riscv64 [0.1.44] (0.1.46 localhost)",
        "Conf linux-image (2 localhost)",
    ])
    monkeypatch.setattr(module, "run_command", lambda *a, **k: Proc(0, output))
    assert module._upgradable_packages() == ["bibit-intent", "linux-image"]


def test_upgradable_packages_returns_none_on_failure(monkeypatch):
    module = _load_handlers()
    monkeypatch.setattr(module, "run_command", lambda *a, **k: Proc(100, ""))
    assert module._upgradable_packages() is None


def test_pkg_update_returns_after_submission_without_waiting(monkeypatch):
    module = _load_handlers()
    calls = []
    timeouts = []
    monkeypatch.setattr(module, '_session_askpass_env', lambda: {'DISPLAY': ':0'})
    monkeypatch.setattr(module.os, 'getuid', lambda: 1000)
    monkeypatch.setattr(module, '_apt_dpkg_in_flight', lambda: None)

    def fake_sudo(cmd, timeout=60, input=None, on_output=None):
        calls.append(list(cmd))
        timeouts.append(timeout)
        return module.ExecResult(True, "ok", "")

    monkeypatch.setattr(module, "_upgradable_packages", lambda: ["bibit-intent", "vim", "linux-image"])
    monkeypatch.setattr(module, "_run_sudo", fake_sudo)
    monkeypatch.setattr(module, "Path", lambda _: type("Script", (), {"is_file": lambda self: True})())
    monkeypatch.setattr(module, "_schedule_agent_upgrade", lambda packages: module.ExecResult(True, "scheduled bibit-intent"))
    monkeypatch.setattr(module, "_wait_system_update", lambda: pytest.fail("Must not wait across Runtime restart"))

    result = module.pkg_update(confirm=True)
    assert result.ok is True
    assert len(calls) == 1
    assert calls[0][:2] == ["/bin/sh", "-c"]
    assert timeouts == [None]
    launch = calls[0][2]
    assert "rm -f /var/lib/bibit-intent/system-update.status /var/lib/bibit-intent/system-update.json" in launch
    assert "systemd-run --system --no-block --collect" in launch
    assert "--on-active=10s --timer-property=RemainAfterElapse=no" in launch
    assert "--unit=bibit-system-update.service" in launch
    assert "/opt/bibit-intent/deploy/bibit-system-update.sh" in launch
    assert "--uid 1000" in launch
    assert '{"DISPLAY": ":0"}' in launch
    assert "任务已提交" in result.message
    assert "不表示升级已完成" in result.message


def test_pkg_update_skips_when_apt_dpkg_in_flight(monkeypatch):
    module = _load_handlers()
    monkeypatch.setattr(
        module, "_apt_dpkg_in_flight",
        lambda: "安装任务 bibit-sudo-9999.service 仍在运行",
    )
    monkeypatch.setattr(
        module, "Path",
        lambda _: type("Script", (), {"is_file": lambda self: True})(),
    )
    result = module.pkg_update()
    assert result.ok is True
    assert "仍在运行" in result.message
    assert "未启动系统更新" in result.message


def test_pkg_update_returns_update_failure_without_upgrading(monkeypatch):
    module = _load_handlers()
    monkeypatch.setattr(module, '_session_askpass_env', lambda: None)
    monkeypatch.setattr(module, "Path", lambda _: type("Script", (), {"is_file": lambda self: True})())
    monkeypatch.setattr(module, "_run_sudo", lambda cmd, timeout=60, input=None, on_output=None: module.ExecResult(False, "更新失败", "denied"))
    result = module.pkg_update()
    assert result.ok is False
    assert "更新失败" in result.message


def test_wait_system_update_returns_terminal_state(monkeypatch):
    module = _load_handlers()
    monkeypatch.setattr(module, "_read_system_update_state", lambda: ("succeeded", {}))
    monkeypatch.setattr(module, "_publish_system_update_progress", lambda state: None)
    assert module._wait_system_update(timeout_seconds=1) == ("succeeded", {})


def test_system_update_result_maps_terminal_states(monkeypatch):
    module = _load_handlers()
    assert module._system_update_result("succeeded", {}).message == "系统更新已完成"
    assert module._system_update_result("succeeded_with_skips", {}).ok is True
    failed = module._system_update_result("failed", {"error": "boom"})
    assert failed.ok is False
    assert "系统更新失败" in failed.message
    assert "boom" in failed.detail


def test_schedule_agent_upgrade_uses_detached_system_unit(monkeypatch):
    module = _load_handlers()
    calls = []
    monkeypatch.setattr(module, "Path", lambda path: type("AlwaysExists", (), {"exists": lambda self: True})())

    def fake_sudo(cmd, timeout=60, input=None, on_output=None):
        calls.append(list(cmd))
        return module.ExecResult(True, "ok", "")

    monkeypatch.setattr(module, "_run_sudo", fake_sudo)
    result = module._schedule_agent_upgrade(["bibit-intent", "bianbu-agent-runtime"])
    assert result.ok is True
    assert "bibit-intent" in result.message and "bianbu-agent-runtime" in result.message
    assert calls and calls[0][0] == "systemd-run"
    assert "--system" in calls[0] and "--no-block" in calls[0]
    assert calls[0][calls[0].index('--uid') + 1] == str(module.os.getuid())
    assert set(calls[0][-2:]) == {"bibit-intent", "bianbu-agent-runtime"}
