"""H07：缺前置时的依赖计划提示。

对应 docs/design/cli_anything_plan.md 的 H07 验收标准：缺 apt/pip 前置时**不执行、不安装**，
返回「缺什么 + 生成依赖计划 <root>」提示；提示口径与 `dep_plan` 的渠道一致
（apt 需 sudo、pip 装用户 venv）。

本文件还固定一条更隐蔽的性质：缺前置是**无副作用**的拒绝，因此绝不能带
``escalate``（否则执行层会把它当成「已产生副作用、不可再转交」，把云端兜底封死）。
"""
from __future__ import annotations

import importlib.util
import sys

import pytest
import yaml

from bibit_intent.core.dependency_plan import channel_label
from bibit_intent.core.appcli_prereq import AppCliReadiness
from bibit_intent.core.appcli_registry import AppCliRegistry
from bibit_intent.resources import resource_path

_ORIGINAL_LOAD = AppCliRegistry.load


def _load_handlers():
    path = resource_path("skills") / "appcli" / "handlers.py"
    spec = importlib.util.spec_from_file_location("_test_appcli_prereq_handlers", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _document():
    return {
        "version": 1,
        "appclies": [
            {
                "name": "demo",
                "display": "Demo",
                "entry_point": "cli-anything-demo",
                "channel": "artifact-only",
                "riscv64": "verified",
                "requires": {"apt": ["libreoffice-writer"], "python": ["cli-anything-demo"]},
                "actions": [{"name": "peek", "argv": ["peek"], "readonly": True}],
            }
        ],
    }


@pytest.fixture
def handlers(tmp_path, monkeypatch):
    module = _load_handlers()
    path = tmp_path / "appcli_registry.yaml"
    path.write_text(yaml.safe_dump(_document(), allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(
        module.AppCliRegistry,
        "load",
        classmethod(lambda cls, _path=None: _ORIGINAL_LOAD(path)),
    )
    monkeypatch.setenv("BIBIT_INTENT_APPCLI_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setattr(
        module,
        "run_bounded_command",
        lambda *a, **k: pytest.fail("缺前置时不允许执行任何命令"),
    )
    return module


def _missing(module, monkeypatch, readiness):
    monkeypatch.setattr(module, "check_readiness", lambda spec: readiness)
    return module.appcli_run(appcli="demo", action="peek")


# ── 缺什么 + 走哪条渠道 ──────────────────────────────────────────────

def test_missing_apt_and_python_are_listed_with_their_channels(handlers, monkeypatch):
    result = _missing(
        handlers,
        monkeypatch,
        AppCliReadiness(
            entry_point_found=False,
            missing_apt=("libreoffice-writer",),
            missing_python=("cli-anything-demo",),
        ),
    )

    assert not result.ok
    assert result.message.splitlines()[0] == "缺少前置，本次未执行：demo"
    assert f"  - 系统包 libreoffice-writer → {channel_label('apt')}" in result.message
    assert f"  - python 包 cli-anything-demo → {channel_label('pip')}" in result.message
    assert "  - 入口命令 cli-anything-demo 不在 PATH" in result.message


def test_channel_wording_matches_the_dependency_plan_card(handlers, monkeypatch):
    result = _missing(
        handlers,
        monkeypatch,
        AppCliReadiness(entry_point_found=False, missing_apt=("libreoffice-writer",)),
    )
    # 「apt 需 sudo」「pip 装用户 venv」这套口径只在 core/dependency_plan.py 维护一份。
    assert channel_label("apt") == "apt（系统包，需 sudo）"
    assert channel_label("apt") in result.message
    assert "sudo" in result.message


def test_entry_point_only_missing_still_explains_the_path_problem(handlers, monkeypatch):
    result = _missing(handlers, monkeypatch, AppCliReadiness(entry_point_found=False))

    assert not result.ok
    assert "入口命令 cli-anything-demo 不在 PATH" in result.message
    assert "系统包" not in result.message
    assert "python 包" not in result.message


# ── 提示指向依赖计划，而不是自己装 ───────────────────────────────────

def test_hint_routes_to_dep_plan_not_to_a_manual_install(handlers, monkeypatch):
    result = _missing(
        handlers,
        monkeypatch,
        AppCliReadiness(entry_point_found=False, missing_python=("cli-anything-demo",)),
    )

    assert "生成依赖计划 <工程目录>" in result.message
    assert "dep_plan" in result.message
    assert "dep_apply" in result.message
    assert "不会自动安装" in result.message


def test_missing_prerequisite_is_a_side_effect_free_refusal(handlers, monkeypatch):
    result = _missing(
        handlers,
        monkeypatch,
        AppCliReadiness(entry_point_found=False, missing_apt=("libreoffice-writer",)),
    )

    # 没有副作用 → 不许 escalate：escalate 会让执行层判定「已部分执行、不可转交」。
    assert result.escalate is None
    assert result.need_param is None
    assert result.detail == ""


# ── 英文 ─────────────────────────────────────────────────────────────

def test_english_prereq_hint_is_localized(handlers, monkeypatch):
    monkeypatch.setattr(
        handlers,
        "check_readiness",
        lambda spec: AppCliReadiness(
            entry_point_found=False,
            missing_apt=("libreoffice-writer",),
            missing_python=("cli-anything-demo",),
        ),
    )
    result = handlers.appcli_run(appcli="demo", action="peek", language="en")

    assert result.message.startswith("Missing prerequisites, nothing was executed: demo")
    assert "system package libreoffice-writer ->" in result.message
    assert "python package cli-anything-demo ->" in result.message
    assert "dependency plan" in result.message
    assert "dep_apply" in result.message
