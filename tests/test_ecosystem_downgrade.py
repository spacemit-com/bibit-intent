"""pip/npm 安装路径的降级预检回归测试。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from test_ecosystem_install import _load_skill_handlers


@pytest.fixture
def module(monkeypatch):
    handlers = _load_skill_handlers()
    monkeypatch.setattr(handlers, "_deb_architecture_error", lambda file: "")
    monkeypatch.setattr(handlers, "_deb_version_preflight", lambda file, pkg, identity, allow_downgrade=False: (identity.update(package=pkg, version="1.0"), ""))
    return handlers


class Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("requests", ("requests", None)),
        ("requests==2.0", ("requests", "2.0")),
        ("requests[socks]==2.31.0", ("requests", "2.31.0")),
    ],
)
def test_pip_package_spec(module, spec, expected):
    assert module._pip_package_spec(spec) == expected


def test_pip_package_spec_local_wheel(module):
    assert module._pip_package_spec("/tmp/demo_pkg-1.2.3-py3-none-any.whl") == ("demo-pkg", "1.2.3")


def test_pip_target_python_detects_venv(module, tmp_path):
    python = tmp_path / "bin" / "python"
    python.parent.mkdir()
    python.write_text("")
    assert module._pip_target_python(str(tmp_path)) == str(python)


def test_pip_install_uses_venv_interpreter_for_venv_target(module, monkeypatch, tmp_path):
    python = tmp_path / "bin" / "python"
    python.parent.mkdir()
    python.write_text("")
    calls = []
    monkeypatch.setattr(module, "_pip_downgrade_preflight", lambda pkg, target="", allow_downgrade=False, python="": None)
    monkeypatch.setattr(module, "_run", lambda cmd, timeout=600: calls.append(cmd) or module.ExecResult(True, "ok", ""))
    assert module.pip_install(pkg="requests==2.0", target=str(tmp_path)).ok is True
    assert calls[0][0] == str(python)
    assert "--target" not in calls[0]


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("axios", ("axios", None)),
        ("axios@1.7.0", ("axios", "1.7.0")),
        ("@scope/pkg", ("@scope/pkg", None)),
        ("@scope/pkg@2.0.0", ("@scope/pkg", "2.0.0")),
    ],
)
def test_npm_package_spec(module, spec, expected):
    assert module._npm_package_spec(spec) == expected


def test_npm_package_spec_local_tarball(module):
    assert module._npm_package_spec("/tmp/demo-pkg-1.2.3.tgz") == ("demo-pkg", "1.2.3")


@pytest.mark.parametrize(
    ("candidate", "installed", "relation"),
    [
        ("1.0.0", "1.0.0", "eq"),
        ("1.0.0", "1.0.1", "lt"),
        ("2.0.0", "1.0.1", "gt"),
        ("1.0.0-rc.1", "1.0.0", "lt"),
        ("1.0.0-beta.2", "1.0.0-beta.10", "lt"),
        ("1.0.0+build", "1.0.0", "eq"),
    ],
)
def test_semver_relation(module, candidate, installed, relation):
    assert module._semver_relation(candidate, installed) == relation


def test_pip_installed_version_show(module, monkeypatch):
    monkeypatch.setattr(module, "run_command", lambda cmd, timeout=60: Proc(0, "Name: requests\nVersion: 2.31.0\n"))
    assert module._pip_installed_version("python", "requests") == "2.31.0"


def test_pip_installed_version_target(module, monkeypatch):
    payload = json.dumps([{"name": "demo_pkg", "version": "1.0"}, {"name": "other", "version": "2.0"}])
    monkeypatch.setattr(module, "run_command", lambda cmd, timeout=60: Proc(0, payload))
    assert module._pip_installed_version("python", "demo-pkg", target="/tmp/target") == "1.0"


def test_npm_installed_version(module, monkeypatch):
    payload = json.dumps({"dependencies": {"axios": {"version": "1.7.0"}}})
    monkeypatch.setattr(module, "run_command", lambda cmd, timeout=60: Proc(0, payload))
    assert module._npm_installed_version("axios") == "1.7.0"


def test_pip_compare_versions_uses_vendored_packaging(module, monkeypatch):
    calls = []

    def fake_run(command, timeout=60):
        calls.append(command)
        return Proc(0, "lt")

    monkeypatch.setattr(module, "run_command", fake_run)
    assert module._pip_compare_versions("python", "2.0", "3.0") == "lt"
    assert calls[0][:2] == ["python", "-c"]
    assert calls[0][-2:] == ["2.0", "3.0"]


def test_pip_downgrade_preflight_blocks(module, monkeypatch):
    monkeypatch.setattr(module, "_user_pip", lambda: ["/venv/bin/python", "-m", "pip"])
    monkeypatch.setattr(module, "_pip_installed_version", lambda python, name, target="": "3.0")
    monkeypatch.setattr(module, "_pip_compare_versions", lambda python, candidate, installed: "lt")
    monkeypatch.setattr(module, "_downgrade_dialog", lambda *args: None)
    result = module._pip_downgrade_preflight("requests==2.0")
    assert result.ok is False
    assert "允许降级" in result.message and "3.0" in result.message
    assert result.need_param == "allow_downgrade"
    assert result.confirm is True


def test_pip_downgrade_preflight_allows_when_confirmed(module, monkeypatch):
    monkeypatch.setattr(module, "_user_pip", lambda: ["/venv/bin/python", "-m", "pip"])
    monkeypatch.setattr(module, "_pip_installed_version", lambda python, name, target="": "3.0")
    monkeypatch.setattr(module, "_pip_compare_versions", lambda python, candidate, installed: "lt")
    assert module._pip_downgrade_preflight("requests==2.0", allow_downgrade=True) is None
    monkeypatch.setattr(module, "_downgrade_dialog", lambda *args: True)
    assert module._pip_downgrade_preflight("requests==2.0") is None


def test_npm_downgrade_preflight_blocks(module, monkeypatch):
    monkeypatch.setattr(module, "_npm_installed_version", lambda name, target="", global_install=False: "2.0.0")
    monkeypatch.setattr(module, "_downgrade_dialog", lambda *args: None)
    result = module._npm_downgrade_preflight("axios@1.0.0")
    assert result.ok is False
    assert "允许降级" in result.message and "2.0.0" in result.message
    assert result.need_param == "allow_downgrade"
    assert result.confirm is True


def test_npm_downgrade_preflight_allows_upgrade(module, monkeypatch):
    monkeypatch.setattr(module, "_npm_installed_version", lambda name, target="", global_install=False: "1.0.0")
    assert module._npm_downgrade_preflight("axios@2.0.0") is None
    monkeypatch.setattr(module, "_downgrade_dialog", lambda *args: True)
    assert module._npm_downgrade_preflight("axios@1.0.0") is None


def test_pip_install_returns_preflight_error(module, monkeypatch):
    failure = module.ExecResult(False, "降级拦截")
    monkeypatch.setattr(module, "_user_pip", lambda: ["/venv/bin/python", "-m", "pip"])
    monkeypatch.setattr(module, "_pip_downgrade_preflight", lambda pkg, target="", allow_downgrade=False: failure)
    monkeypatch.setattr(module, "_run", lambda *args, **kwargs: pytest.fail("不应安装"))
    result = module.pip_install(pkg="requests==2.0")
    assert result is failure


def test_npm_install_returns_preflight_error(module, monkeypatch):
    failure = module.ExecResult(False, "降级拦截")
    monkeypatch.setattr(module, "_npm_downgrade_preflight", lambda pkg, target="", global_install=False, allow_downgrade=False: failure)
    monkeypatch.setattr(module, "_run", lambda *args, **kwargs: pytest.fail("不应安装"))
    result = module.npm_install(pkg="axios@1.0.0")
    assert result is failure
