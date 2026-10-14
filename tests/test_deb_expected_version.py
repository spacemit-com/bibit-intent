from pathlib import Path
from types import SimpleNamespace

import pytest

from bibit_intent.core import package_state
from test_ecosystem_install import _load_skill_handlers


@pytest.mark.parametrize("deps", [True, False])
@pytest.mark.parametrize("verified", [True, False])
def test_deb_checks_preflight_version_after_command(monkeypatch, deps, verified):
    module = _load_skill_handlers()
    monkeypatch.setattr(module, "_deb_package_name", lambda file: "testapp")
    monkeypatch.setattr(module, "_deb_architecture_error", lambda file: "")
    monkeypatch.setattr(package_state, "query_package_state", lambda pkg: package_state.PackageState("absent"))
    monkeypatch.setattr(module, "run_command", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="Version: 1:2.0-1\nArchitecture: riscv64\n"))
    monkeypatch.setattr(module, "_apt_simulate_install", lambda file: (True, [], ""))
    monkeypatch.setattr(module, "_deb_unsatisfied_deps", lambda file: [])
    monkeypatch.setattr(module, "_deb_depends", lambda file: [])
    monkeypatch.setattr(module, "_is_installed", lambda pkg: True)
    monkeypatch.setattr(module, "_installed_version", lambda pkg: "1:2.0-1")
    events = []
    def install(*args, **kwargs):
        events.append("install")
        return module.ExecResult(True, "finished", "installation log")
    def verify(pkg, version):
        events.append((pkg, version))
        return {"ok": verified, "message": "目标版本不匹配"}
    monkeypatch.setattr(module, "_run_sudo", install)
    monkeypatch.setattr(module, "verify_package", verify)
    result = module._install_deb(Path("test.deb"), deps)
    assert result.ok is verified
    assert events == ["install", ("testapp:riscv64", "1:2.0-1")]
    assert result.detail == "installation log"
    if not verified:
        assert "未通过核验" in result.message
