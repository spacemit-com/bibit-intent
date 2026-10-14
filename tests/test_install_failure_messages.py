from pathlib import Path

import pytest

from test_ecosystem_install import _load_skill_handlers
from bibit_intent.core import package_state


@pytest.fixture(autouse=True)
def absent_package_state(monkeypatch):
    monkeypatch.setattr(package_state, "query_package_state", lambda pkg: package_state.PackageState("absent"))


CASES = [
    ("E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 123 (apt)", "包管理器正被其他进程占用"),
    ("E: 无法获得锁 /var/lib/dpkg/lock-frontend，锁正由进程 123 持有", "包管理器正被其他进程占用"),
    ("dpkg: error: No space left on device", "存储空间不足"),
    ("E: You don't have enough free space in /var/cache/apt/archives/.", "存储空间不足"),
    ("设备上没有空间", "存储空间不足"),
    ("E: Unmet dependencies. Try apt --fix-broken install", "依赖无法满足"),
    ("dpkg: dependency problems prevent configuration of testapp", "依赖无法满足"),
    ("下列软件包有未满足的依赖关系", "依赖无法满足"),
]


@pytest.mark.parametrize("detail,summary", CASES)
@pytest.mark.parametrize("mode", ["package", "apt", "dpkg"])
def test_install_failure_has_actionable_summary(monkeypatch, detail, summary, mode):
    module = _load_skill_handlers()
    calls = []

    def failed_install(*args, **kwargs):
        calls.append(args)
        return module.ExecResult(False, "命令失败(码100)", detail)

    monkeypatch.setattr(module, "_run_sudo", failed_install)
    monkeypatch.setattr(module, "_is_installed", lambda package: False)
    monkeypatch.setattr(module, "_deb_package_name", lambda file: "testapp")
    monkeypatch.setattr(module, "_deb_architecture_error", lambda file: "")
    monkeypatch.setattr(module, "_deb_version_preflight", lambda file, pkg, identity, allow_downgrade=False: (identity.update(package=pkg, version="1.0"), ""))
    monkeypatch.setattr(module, "_apt_simulate_install", lambda file: (True, [], ""))
    monkeypatch.setattr(module, "_deb_unsatisfied_deps", lambda file: [])
    monkeypatch.setattr(module, "_deb_depends", lambda file: [])
    result = module.pkg_install("testapp") if mode == "package" else module._install_deb(Path("test.deb"), mode == "apt")
    assert result.ok is False
    assert summary in result.message
    assert result.detail == detail
    assert result.escalate is None
    assert "未执行安装" not in result.message
    assert len(calls) == 1


@pytest.mark.parametrize("detail", [
    "sudo: no password was provided",
    "sudo: 3 incorrect password attempts",
    "sudo: a password is required",
    "E: Could not open lock file /var/lib/dpkg/lock-frontend - open (13: Permission denied)",
    "unexpected backend error",
])
def test_ambiguous_failure_is_not_reclassified(monkeypatch, detail):
    module = _load_skill_handlers()
    failure = module.ExecResult(False, "命令失败(码1)", detail)
    monkeypatch.setattr(module, "_is_installed", lambda package: False)
    monkeypatch.setattr(module, "_run_sudo", lambda *args, **kwargs: failure)
    assert module.pkg_install("testapp") is failure


@pytest.mark.parametrize("detail,summary", CASES + [("Temporary failure resolving host", "预演失败")])
def test_simulation_failure_is_not_always_dependency_error(monkeypatch, detail, summary):
    module = _load_skill_handlers()
    monkeypatch.setattr(module, "_deb_package_name", lambda file: "testapp")
    monkeypatch.setattr(module, "_deb_architecture_error", lambda file: "")
    monkeypatch.setattr(module, "_deb_version_preflight", lambda file, pkg, identity, allow_downgrade=False: (identity.update(package=pkg, version="1.0"), ""))
    monkeypatch.setattr(module, "_apt_simulate_install", lambda file: (False, [], detail))

    def unexpected(*args, **kwargs):
        pytest.fail("simulation failure must not invoke sudo")

    monkeypatch.setattr(module, "_run_sudo", unexpected)
    result = module._install_deb(Path("test.deb"), True)
    assert summary in result.message
    assert "未执行安装" in result.message
    assert result.detail == detail
    if "resolving" in detail:
        assert "依赖无法满足" not in result.message
