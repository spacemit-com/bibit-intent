from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from bibit_intent.core import package_state
from test_ecosystem_install import _load_skill_handlers


@pytest.mark.parametrize("abbreviation,status,held", [
    ("ii ", "installed", False), ("hi ", "installed", True),
    ("iU ", "incomplete", False), ("iF ", "incomplete", False),
    ("iHR", "incomplete", False), ("rc ", "config_only", False),
    ("un ", "absent", False),
])
def test_dpkg_state(monkeypatch, abbreviation, status, held):
    monkeypatch.setattr(package_state, "run_command", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=f"{abbreviation}\t1:2.0-1\n", stderr=""))
    result = package_state.query_package_state("testapp")
    assert result.status == status and result.held is held


@pytest.mark.parametrize("returncode,stdout,stderr,status", [
    (1, "", "dpkg-query: no packages found matching testapp\n", "absent"),
    (2, "", "database is unreadable", "unknown"),
    (1, "", "permission denied", "unknown"),
    (0, "", "", "unknown"),
    (0, "ii \t1.0\nii \t2.0\n", "", "unknown"),
    (0, "garbage", "", "unknown"),
])
def test_query_failure_is_not_absence(monkeypatch, returncode, stdout, stderr, status):
    monkeypatch.setattr(package_state, "run_command", lambda *args, **kwargs: SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr))
    assert package_state.query_package_state("testapp").status == status


@pytest.mark.parametrize("error", [FileNotFoundError(), subprocess.TimeoutExpired("dpkg-query", 15)])
def test_query_unavailable(monkeypatch, error):
    def unavailable(*args, **kwargs):
        raise error
    monkeypatch.setattr(package_state, "run_command", unavailable)
    assert package_state.query_package_state("testapp").status == "unknown"


@pytest.mark.parametrize("package", ["*", "-a", "bash vim", "", "bash;id"])
def test_query_requires_single_package(monkeypatch, package):
    monkeypatch.setattr(package_state, "run_command", lambda *args, **kwargs: pytest.fail("invalid query must not execute"))
    assert package_state.query_package_state(package).status == "unknown"


def test_query_uses_stable_locale(monkeypatch):
    def query(args, **kwargs):
        assert kwargs["env"]["LC_ALL"] == "C"
        assert args[-1] == "testapp:riscv64"
        return SimpleNamespace(returncode=0, stdout="hi \t1:2.0-1\n", stderr="")
    monkeypatch.setattr(package_state, "run_command", query)
    assert package_state.query_package_state("testapp:riscv64").version == "1:2.0-1"


def test_install_stops_on_unknown_state(monkeypatch):
    module = _load_skill_handlers()
    monkeypatch.setattr(package_state, "query_package_state", lambda pkg: package_state.PackageState("unknown"))
    monkeypatch.setattr(module, "_run_sudo", lambda *args, **kwargs: pytest.fail("must not install"))
    assert not module.pkg_install("testapp").ok


@pytest.mark.parametrize("status_text,status,reinstreq,configure", [
    (
        "Package: bash\nStatus: install ok installed\n\n",
        "healthy", (), False,
    ),
    (
        "Package: zenow\nStatus: install reinstreq half-installed\n\n",
        "interrupted", ("zenow",), False,
    ),
    (
        "Package: yumeet\nStatus: install ok half-configured\n\n",
        "interrupted", (), True,
    ),
    (
        "Package: libc-bin\nStatus: install ok installed\nTriggers-Pending: ldconfig\n\n",
        "interrupted", (), True,
    ),
])
def test_dpkg_audit_states(monkeypatch, status_text, status, reinstreq, configure):
    monkeypatch.setattr(package_state, "_read_dpkg_status_file", lambda: status_text)
    audit = package_state.query_dpkg_audit()
    assert audit.status == status
    assert audit.reinstreq == reinstreq
    assert audit.configure is configure


def test_dpkg_audit_unavailable(monkeypatch):
    def unreadable():
        raise OSError("permission denied")
    monkeypatch.setattr(package_state, "_read_dpkg_status_file", unreadable)
    audit = package_state.query_dpkg_audit()
    assert audit.status == "unknown"
    assert "无法读取 dpkg 状态数据库" in audit.detail


def test_pkg_install_reinstalls_reinstreq_package(monkeypatch):
    module = _load_skill_handlers()
    monkeypatch.setattr(
        package_state, "query_package_state",
        lambda pkg: package_state.PackageState("incomplete", "0.0.184", detail="iHR"),
    )
    audits = iter([
        package_state.DpkgAudit(
            "interrupted", "zenow 需要重新安装", reinstreq=("zenow",)
        ),
        package_state.DpkgAudit("healthy"),
    ])
    monkeypatch.setattr(package_state, "query_dpkg_audit", lambda: next(audits))
    calls = []

    def install(command, **kwargs):
        calls.append(command)
        return module.ExecResult(True, "ok")

    monkeypatch.setattr(module, "_run_sudo", install)
    monkeypatch.setattr(module, "_is_installed", lambda name: True)
    monkeypatch.setattr(module, "_installed_version", lambda name: "0.0.184")
    assert module.pkg_install("zenow").ok
    assert len(calls) == 1
    assert calls[0][:2] == ["/bin/sh", "-c"]
    script = calls[0][2]
    assert "apt-get install -y --no-remove --reinstall -- zenow" in script
    assert "dpkg --configure -a" not in script


def test_pkg_install_repairs_interrupted_dpkg(monkeypatch):
    module = _load_skill_handlers()
    monkeypatch.setattr(package_state, "query_package_state", lambda pkg: package_state.PackageState("absent"))
    audits = iter([package_state.DpkgAudit("interrupted", "unpacked"), package_state.DpkgAudit("healthy")])
    monkeypatch.setattr(package_state, "query_dpkg_audit", lambda: next(audits))
    calls = []

    def install(command, **kwargs):
        calls.append(command)
        return module.ExecResult(True, "ok")

    monkeypatch.setattr(module, "_run_sudo", install)
    monkeypatch.setattr(module, "_is_installed", lambda name: True)
    monkeypatch.setattr(module, "_installed_version", lambda name: "1.0")
    assert module.pkg_install("testapp").ok
    # dpkg 修复与 apt 安装必须合并进同一个 sudo 会话，只弹一次密码框。
    assert len(calls) == 1
    assert calls[0][:2] == ["/bin/sh", "-c"]
    script = calls[0][2]
    assert "dpkg --configure -a" in script
    assert "apt-get install -y --no-remove -- testapp" in script


def test_pkg_install_blocks_when_dpkg_audit_unavailable(monkeypatch):
    module = _load_skill_handlers()
    monkeypatch.setattr(package_state, "query_package_state", lambda pkg: package_state.PackageState("absent"))
    monkeypatch.setattr(package_state, "query_dpkg_audit", lambda: package_state.DpkgAudit("unknown", "audit down"))
    monkeypatch.setattr(module, "_run_sudo", lambda *args, **kwargs: pytest.fail("must not run with unknown audit"))
    result = module.pkg_install("testapp")
    assert not result.ok and "无法检查 dpkg" in result.message and result.detail == "audit down"


def test_pkg_install_blocks_when_repair_leaves_interrupted(monkeypatch):
    module = _load_skill_handlers()
    monkeypatch.setattr(package_state, "query_package_state", lambda pkg: package_state.PackageState("absent"))
    audits = iter([
        package_state.DpkgAudit("interrupted", "before"),
        package_state.DpkgAudit("interrupted", "sm-sdk Reinst-required"),
    ])
    monkeypatch.setattr(package_state, "query_dpkg_audit", lambda: next(audits))
    monkeypatch.setattr(module, "_run_sudo", lambda *args, **kwargs: module.ExecResult(True, "ok"))
    result = module.pkg_install("testapp")
    assert not result.ok
    assert "仍存在未修复的严重异常包" in result.message
    assert "sm-sdk Reinst-required" in result.detail


@pytest.mark.parametrize("relation,held,ok,note", [
    ("eq", False, True, "相同版本"), ("eq", True, True, "相同版本"),
    ("lt", False, False, "降级"), ("gt", True, False, "锁定"),
    ("gt", False, None, "版本升级"),
])
def test_deb_version_relation(monkeypatch, relation, held, ok, note):
    module = _load_skill_handlers()
    monkeypatch.setattr(module, "_downgrade_dialog", lambda *args: None)
    targets = []
    def query(target):
        targets.append(target)
        return package_state.PackageState("installed", "1:2.0-1", held)
    monkeypatch.setattr(package_state, "query_package_state", query)
    def command(args, **kwargs):
        if args[0] == "dpkg-deb":
            return SimpleNamespace(returncode=0, stdout="Version: 1:3.0-1\nArchitecture: riscv64\n")
        return SimpleNamespace(returncode=0 if args[3] == relation else 1, stdout="")
    monkeypatch.setattr(module, "run_command", command)
    result, change = module._deb_version_preflight(Path("test.deb"), "testapp")
    assert targets == ["testapp:riscv64"]
    if ok is None:
        assert result is None and note in change
    else:
        assert result.ok is ok and note in result.message


@pytest.mark.parametrize("deps", [True, False])
def test_same_version_skips_simulation_and_install(monkeypatch, deps):
    module = _load_skill_handlers()
    monkeypatch.setattr(module, "_deb_package_name", lambda file: "testapp")
    monkeypatch.setattr(module, "_deb_architecture_error", lambda file: "")
    monkeypatch.setattr(package_state, "query_package_state", lambda pkg: package_state.PackageState("installed", "1.0"))
    monkeypatch.setattr(module, "run_command", lambda args, **kwargs: SimpleNamespace(returncode=0, stdout="Version: 1.0\nArchitecture: all\n"))
    monkeypatch.setattr(module, "_run_sudo", lambda *args, **kwargs: pytest.fail("must not install"))
    monkeypatch.setattr(module, "_apt_simulate_install", lambda *args: pytest.fail("must not simulate"))
    assert module._install_deb(Path("test.deb"), deps).ok


def test_version_comparison_failure_blocks(monkeypatch):
    module = _load_skill_handlers()
    monkeypatch.setattr(package_state, "query_package_state", lambda pkg: package_state.PackageState("installed", "1.0"))
    def command(args, **kwargs):
        return SimpleNamespace(returncode=0 if args[0] == "dpkg-deb" else 2, stdout="Version: 2.0\nArchitecture: all\n")
    monkeypatch.setattr(module, "run_command", command)
    result, note = module._deb_version_preflight(Path("test.deb"), "testapp")
    assert not result.ok and "比较失败" in result.message and not note


@pytest.mark.parametrize("status", ["incomplete", "unknown"])
def test_deb_bad_state_blocks(monkeypatch, status):
    module = _load_skill_handlers()
    monkeypatch.setattr(package_state, "query_package_state", lambda pkg: package_state.PackageState(status))
    monkeypatch.setattr(module, "run_command", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="Version: 2.0\nArchitecture: all\n"))
    result, note = module._deb_version_preflight(Path("test.deb"), "testapp")
    assert not result.ok and "未执行安装" in result.message and not note
