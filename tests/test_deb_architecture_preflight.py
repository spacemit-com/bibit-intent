from pathlib import Path
from types import SimpleNamespace

import pytest

from test_ecosystem_install import _load_skill_handlers


@pytest.mark.parametrize("package,native,foreign,allowed", [
    ("riscv64", "riscv64", "", True),
    ("all", "riscv64", "", True),
    ("amd64", "riscv64", "", False),
    ("arm64", "riscv64", "arm64\n", True),
    ("", "riscv64", "", False),
])
def test_architecture_preflight(monkeypatch, package, native, foreign, allowed):
    module = _load_skill_handlers()
    outputs = iter([package, native, foreign])
    monkeypatch.setattr(module, "run_command", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=next(outputs)))
    assert (module._deb_architecture_error(Path("test.deb")) == "") is allowed


@pytest.mark.parametrize("deps", [True, False])
def test_incompatible_package_never_reaches_install(monkeypatch, deps):
    module = _load_skill_handlers()
    monkeypatch.setattr(module, "_deb_package_name", lambda file: "testapp")
    monkeypatch.setattr(module, "_deb_architecture_error", lambda file: "架构不匹配，未执行安装")

    def unexpected(*args, **kwargs):
        pytest.fail("must not simulate or install incompatible package")

    monkeypatch.setattr(module, "_run_sudo", unexpected)
    monkeypatch.setattr(module, "_apt_simulate_install", unexpected)
    assert not module._install_deb(Path("test.deb"), deps).ok


def test_missing_architecture_tool(monkeypatch):
    module = _load_skill_handlers()

    def unavailable(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(module, "run_command", unavailable)
    assert "未执行安装" in module._deb_architecture_error(Path("test.deb"))
