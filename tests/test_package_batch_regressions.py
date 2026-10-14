import pytest

from bibit_intent.core import package_checks, package_state
from test_pkg_admin_handlers import _load_handlers


@pytest.mark.parametrize("target", ["g++", "gcc g++", "g++:riscv64"])
def test_install_preserves_verified_suffix_package(monkeypatch, target):
    module = _load_handlers()
    names = target.split()
    monkeypatch.setattr(package_state, "query_package_state", lambda name: package_state.PackageState("absent"))
    monkeypatch.setattr(package_checks, "package_candidate", lambda name: {"ok": True, "candidate": "4:14.2-1"})
    monkeypatch.setattr(module, "_is_installed", lambda name: True)
    monkeypatch.setattr(module, "_installed_version", lambda name: "4:14.2-1")
    calls = []
    monkeypatch.setattr(module, "_run_sudo", lambda command, **kwargs: calls.append(command) or module.ExecResult(True, "ok"))

    assert module.pkg_install(target).ok
    expected = [name if name == "gcc" else name + "=4:14.2-1" for name in names]
    assert calls == [["apt-get", "install", "-y", "--no-remove", "--", *expected]]


def test_batch_install_rejects_unverified_suffix_before_any_write(monkeypatch):
    module = _load_handlers()
    monkeypatch.setattr(package_state, "query_package_state", lambda name: package_state.PackageState("absent"))
    monkeypatch.setattr(package_checks, "package_candidate", lambda name: {"ok": False})
    monkeypatch.setattr(module, "_run_sudo", lambda *args, **kwargs: pytest.fail("must not execute"))
    assert not module.pkg_install("gcc git-").ok


@pytest.mark.parametrize("target", ["g++", "gcc g++", "g++:riscv64"])
def test_remove_pins_verified_suffix_package(monkeypatch, target):
    module = _load_handlers()
    states = {name: package_state.PackageState("installed", "4:14.2-1") for name in target.split()}
    monkeypatch.setattr(package_state, "query_package_state", states.__getitem__)
    calls = []

    def remove(command, **kwargs):
        calls.append(command)
        states.update({name: package_state.PackageState("absent") for name in states})
        return module.ExecResult(True, "ok")

    monkeypatch.setattr(module, "_run_sudo", remove)
    assert module.pkg_remove(target).ok
    expected = [name if name == "gcc" else name + "=4:14.2-1" for name in target.split()]
    assert calls == [["apt-get", "remove", "-y", "--", *expected]]


def test_remove_suffix_without_version_does_not_execute(monkeypatch):
    module = _load_handlers()
    monkeypatch.setattr(package_state, "query_package_state", lambda name: package_state.PackageState("installed"))
    monkeypatch.setattr(module, "_run_sudo", lambda *args, **kwargs: pytest.fail("must not execute"))
    assert not module.pkg_remove("gcc g++").ok


@pytest.mark.parametrize("status,ok,note", [
    ("absent", True, "已卸载"),
    ("config_only", True, "已卸载"),
    ("installed", False, "仍处于已安装状态"),
    ("incomplete", False, "仍处于异常状态"),
    ("unknown", False, "无法确认卸载结果"),
])
def test_remove_requires_verified_final_state(monkeypatch, status, ok, note):
    module = _load_handlers()
    states = iter([package_state.PackageState("incomplete", "1.0"), package_state.PackageState(status)])
    monkeypatch.setattr(package_state, "query_package_state", lambda name: next(states))
    monkeypatch.setattr(module, "_is_installed", lambda name: pytest.fail("boolean state cannot verify removal"))
    monkeypatch.setattr(module, "_run_sudo", lambda *args, **kwargs: module.ExecResult(True, "ok"))
    result = module.pkg_remove("iperf3")
    assert result.ok is ok
    assert note in result.message


@pytest.mark.parametrize("failure_stage", [0, 1, None])
def test_mixed_removal_reports_partial_results_and_preserves_details(monkeypatch, failure_stage):
    module = _load_handlers()
    states = {
        "vim": package_state.PackageState("installed", "9.0"),
        "iperf3": package_state.PackageState("incomplete", "3.20"),
        "missing": package_state.PackageState("absent"),
    }
    monkeypatch.setattr(package_state, "query_package_state", states.__getitem__)
    calls = []

    def remove(command, **kwargs):
        stage = len(calls)
        calls.append(command)
        if stage == 0:
            states["vim"] = package_state.PackageState("absent")
        elif failure_stage != stage:
            states["iperf3"] = package_state.PackageState("config_only")
        return module.ExecResult(stage != failure_stage, f"stage {stage}", f"detail {stage}")

    monkeypatch.setattr(module, "_run_sudo", remove)
    result = module.pkg_remove("vim iperf3 missing")
    assert result.ok is (failure_stage is None)
    assert "vim" in result.message and "已卸载" in result.message
    assert "missing 原本未安装" in result.message
    assert "detail 0" in result.detail
    if failure_stage == 0:
        assert len(calls) == 1
        assert "后续卸载步骤未执行" in result.message
    else:
        assert len(calls) == 2
        assert "detail 1" in result.detail
    if failure_stage is not None:
        assert "仍处于异常状态，未完成卸载：iperf3" in result.message


def test_remove_preserves_authorization_failure(monkeypatch):
    module = _load_handlers()
    monkeypatch.setattr(package_state, "query_package_state", lambda name: package_state.PackageState("installed", "1"))
    monkeypatch.setattr(module, "_run_sudo", lambda *args, **kwargs: module.ExecResult(False, "授权失败", escalate="no_gui_askpass"))
    result = module.pkg_remove("vim")
    assert not result.ok and result.escalate == "no_gui_askpass"
    assert "授权失败" in result.message


def test_batch_install_does_not_suggest_wrong_package(monkeypatch):
    module = _load_handlers()
    monkeypatch.setattr(package_state, "query_package_state", lambda name: package_state.PackageState("absent"))
    monkeypatch.setattr(module, "_run_sudo", lambda *args, **kwargs: module.ExecResult(False, "命令失败", "E: Unable to locate package missing"))
    monkeypatch.setattr(module, "suggest_install_packages", lambda name: pytest.fail("must not guess the failing target"))
    result = module.pkg_install("vim missing")
    assert not result.ok and "missing" in result.detail
