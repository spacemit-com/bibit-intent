"""H02：appcli 前置齐备度判定（只读）。"""
from __future__ import annotations

import pytest
from importlib.metadata import PackageNotFoundError

from bibit_intent.core.appcli_prereq import check_readiness
from bibit_intent.core.appcli_registry import AppCliRegistry
from bibit_intent.core.package_state import PackageState


class _State:
    def __init__(self, installed=()):
        self._installed = set(installed)
        self.queried: list[str] = []

    def __call__(self, package):
        self.queried.append(package)
        return PackageState("installed" if package in self._installed else "absent")


def _spec(**requires):
    return AppCliRegistry.load(
        _write(
            {
                "version": 1,
                "appclies": [
                    {
                        "name": "demo",
                        "display": "Demo",
                        "entry_point": "cli-anything-demo",
                        "channel": "artifact-only",
                        "riscv64": "verified",
                        "requires": requires,
                        "actions": [{"name": "run", "argv": ["run"], "params": {}}],
                    }
                ],
            }
        )
    ).require("demo")


def _write(document):
    import pathlib
    import tempfile
    import yaml

    directory = pathlib.Path(tempfile.mkdtemp())
    path = directory / "appcli_registry.yaml"
    path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    return path


def test_ready_when_entry_point_and_requirements_present():
    readiness = check_readiness(
        _spec(apt=["libreoffice-writer"], python=["cli-anything-demo"]),
        which=lambda name: "/usr/bin/cli-anything-demo",
        package_state=_State(["libreoffice-writer"]),
        dist_version=lambda name: "0.1.0",
    )
    assert readiness.ready
    assert readiness.status == "ready"
    assert readiness.missing() == ()


def test_missing_entry_point_is_not_ready():
    readiness = check_readiness(
        _spec(), which=lambda name: None, package_state=_State()
    )
    assert not readiness.ready
    assert readiness.status == "needs_install"
    assert readiness.missing() == ("入口命令未安装",)


def test_missing_apt_and_python_are_reported_in_order():
    readiness = check_readiness(
        _spec(apt=["libreoffice-writer", "drawio"], python=["cli-anything-demo"]),
        which=lambda name: "/usr/bin/cli-anything-demo",
        package_state=_State(["drawio"]),
        dist_version=lambda name: (_ for _ in ()).throw(PackageNotFoundError(name)),
    )
    assert readiness.missing_apt == ("libreoffice-writer",)
    assert readiness.missing_python == ("cli-anything-demo",)
    assert readiness.missing() == ("apt:libreoffice-writer", "python:cli-anything-demo")


def test_unknown_package_state_is_treated_as_missing():
    """dpkg 查询不可用时保守判为缺失，交给依赖计划去核对。"""

    def unknown(package):
        return PackageState("unknown", detail="软件包状态查询不可用或超时")

    readiness = check_readiness(
        _spec(apt=["libreoffice-writer"]),
        which=lambda name: "/usr/bin/cli-anything-demo",
        package_state=unknown,
        dist_version=lambda name: "0.1.0",
    )
    assert not readiness.ready
    assert readiness.missing_apt == ("libreoffice-writer",)


@pytest.mark.parametrize("present", [True, False])
def test_python_requirement_uses_distribution_metadata(present):
    """按分发包名查元数据（导入名是 cli_anything.*，与分发包名不同）。"""
    seen: list[str] = []

    def dist_version(name):
        seen.append(name)
        if not present:
            raise PackageNotFoundError(name)
        return "0.1.0"

    readiness = check_readiness(
        _spec(python=["cli-anything-libreoffice"]),
        which=lambda _: "/usr/bin/cli-anything-demo",
        package_state=_State(),
        dist_version=dist_version,
    )
    assert seen == ["cli-anything-libreoffice"]
    assert (readiness.missing_python == ()) is present
