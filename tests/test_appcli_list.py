"""H02：只读的 appcli_list 工具。

对应 docs/design/cli_anything_plan.md 的 H02 验收标准：列出 appcli 的名称、
能力、前置齐备度、riscv64 状态、channel、需要审批的动作；未装前置显示
needs_install；注册表不可用时报错不崩。
"""
from __future__ import annotations

import importlib.util
import sys

import pytest

from bibit_intent.core.appcli_prereq import AppCliReadiness
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.resources import resource_path


def _load_handlers():
    path = resource_path("skills") / "appcli" / "handlers.py"
    spec = importlib.util.spec_from_file_location("_test_appcli_handlers", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def handlers():
    return _load_handlers()


def _readiness(ready: bool):
    if ready:
        return AppCliReadiness(entry_point_found=True)
    return AppCliReadiness(
        entry_point_found=False,
        missing_apt=("libreoffice-writer",),
        missing_python=("cli-anything-libreoffice",),
    )


@pytest.fixture
def all_ready(handlers, monkeypatch):
    monkeypatch.setattr(handlers, "check_readiness", lambda spec: _readiness(True))
    return handlers


@pytest.fixture
def none_ready(handlers, monkeypatch):
    monkeypatch.setattr(handlers, "check_readiness", lambda spec: _readiness(False))
    return handlers


# ── 注册表与工具接线 ─────────────────────────────────────────────────

def test_tool_is_registered_as_readonly_and_needs_no_confirmation():
    registry = ToolRegistry(resource_path("tools.yaml"))
    handlers: dict = {}
    load_skills(resource_path("skills"), registry, handlers)
    tool = registry.get("appcli_list")
    assert tool.mcp_group() == "readonly"
    assert not tool.system.get("confirm")
    assert "appcli_list" in handlers


def test_skill_declares_its_tools():
    registry = ToolRegistry(resource_path("tools.yaml"))
    loaded = load_skills(resource_path("skills"), registry, {})

    skill = next(item for item in loaded if item.name == "appcli")
    assert skill.tool_names == ["appcli_list", "appcli_run", "appcli_verify"]


# ── 输出内容 ─────────────────────────────────────────────────────────

def test_lists_name_status_channel_and_actions(none_ready):
    result = none_ready.appcli_list()
    assert result.ok
    assert "已登记应用 2 个" in result.message
    assert "0 个前置齐备" in result.message
    assert "1 个已通过 riscv64 验收" in result.message

    detail = result.detail
    assert "libreoffice | office | channel=host | riscv64=verified | needs_install" in detail
    assert "drawio | diagrams | channel=artifact-only | riscv64=unsupported | needs_install" in detail
    # 需要审批的动作带 *，只读动作单独列出（K01 扩充后只读动作变多）
    assert "doc_new*" in detail
    readonly: set[str] = set()
    for line in detail.splitlines():
        if "只读:" in line:
            readonly |= {item.strip() for item in line.split("只读:", 1)[1].split(",")}
    assert {"doc_info", "export_presets", "writer_list", "calc_list_sheets",
            "impress_list_slides", "style_list", "document_json"} <= readonly
    assert "只读: project_info" in detail
    # 缺什么要写清楚
    assert "缺少: apt:libreoffice-writer, python:cli-anything-libreoffice" in detail


def test_ready_appcli_reports_ready_without_missing_section(all_ready):
    result = all_ready.appcli_list()
    assert "2 个前置齐备" in result.message
    assert "needs_install" not in result.detail
    assert "缺少:" not in result.detail


# ── 过滤与空结果 ─────────────────────────────────────────────────────

def test_category_filter_selects_matching_appcli(all_ready):
    result = all_ready.appcli_list(category="diagrams")
    assert result.ok
    assert "drawio" in result.detail
    assert "libreoffice" not in result.detail
    assert "已登记应用 1 个" in result.message


def test_unknown_category_reports_empty_instead_of_failing(all_ready):
    result = all_ready.appcli_list(category="nonexistent")
    assert result.ok
    assert result.detail == ""
    assert "没有匹配 category=nonexistent 的应用" in result.message


# ── 注册表不可用 ─────────────────────────────────────────────────────

def test_registry_failure_is_reported_not_raised(handlers, monkeypatch):
    from bibit_intent.core.appcli_registry import AppCliRegistry, AppCliRegistryError

    def broken(*args, **kwargs):
        raise AppCliRegistryError("registry_unavailable", "appcli 注册表不存在：/nope")

    monkeypatch.setattr(AppCliRegistry, "load", broken)
    result = handlers.appcli_list()
    assert not result.ok
    assert "应用注册表不可用" in result.message
    assert "registry_unavailable" in result.message


# ── 英文输出 ─────────────────────────────────────────────────────────

def test_english_message_is_localized(all_ready):
    from bibit_intent.core.localization import localize_reply

    result = all_ready.appcli_list()
    english = localize_reply(result.message, "en")
    assert english == "2 apps registered (2 ready, 1 riscv64-verified):"
