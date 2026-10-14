"""K04：templates Skill（template_list / template_get，只读）。

对应 docs/design/k3_smart_office.md §4.4：模型先 list 再 get，拿到骨架、最少输入、
知识源提示与动作计划；两个工具都是只读、不需要确认，也不执行任何命令。
"""
from __future__ import annotations

import importlib.util
import json
import sys

import pytest

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.resources import resource_path
from bibit_intent.routing.rule_engine import RuleEngine


def _load_handlers():
    path = resource_path("skills") / "templates" / "handlers.py"
    spec = importlib.util.spec_from_file_location("_test_template_handlers", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def handlers():
    return _load_handlers()


@pytest.fixture(scope="module")
def rule() -> RuleEngine:
    registry = ToolRegistry(resource_path("tools.yaml"))
    load_skills(resource_path("skills"), registry, {})
    return RuleEngine(registry)


# ── 接线 ─────────────────────────────────────────────────────────────

def test_tools_are_registered_readonly_without_confirmation():
    registry = ToolRegistry(resource_path("tools.yaml"))
    handlers: dict = {}
    load_skills(resource_path("skills"), registry, handlers)
    for name in ("template_list", "template_get"):
        tool = registry.get(name)
        assert tool is not None, name
        assert tool.mcp_group() == "readonly", name
        assert not tool.system.get("confirm"), name
        assert name in handlers, name


def test_skill_declares_its_tools():
    registry = ToolRegistry(resource_path("tools.yaml"))
    loaded = load_skills(resource_path("skills"), registry, {})
    skill = next(item for item in loaded if item.name == "templates")
    assert skill.tool_names == ["template_list", "template_get"]


def test_rule_layer_can_reach_both_tools(rule):
    assert rule.match("列出办公模板").tool_name == "template_list"
    assert rule.match("有哪些模板").tool_name == "template_list"
    match = rule.match("取模板 weekly_report")
    assert match.tool_name == "template_get"
    assert match.slots == {"template": "weekly_report"}


def test_templates_do_not_steal_launch_app(rule):
    """模板关键词不能把「打开应用」抢走（launch_app 是另一条路径）。"""
    assert rule.match("打开终端").tool_name == "launch_app"


# ── 输出 ─────────────────────────────────────────────────────────────

def test_list_reports_all_five_templates(handlers):
    result = handlers.template_list()
    assert result.ok
    assert "5 个" in result.message
    for name in ("weekly_report", "meeting_minutes", "project_plan", "data_report", "presentation"):
        assert name in result.message


def test_list_filters_by_doc_type_and_rejects_unknown(handlers):
    only_calc = handlers.template_list(doc_type="calc")
    assert only_calc.ok
    assert "project_plan" in only_calc.message and "data_report" in only_calc.message
    assert "weekly_report" not in only_calc.message

    bad = handlers.template_list(doc_type="pdf")
    assert not bad.ok
    assert "参数错误" in bad.message


def test_list_and_get_support_english(handlers):
    listed = handlers.template_list(language="en")
    assert "built-in office templates" in listed.message
    got = handlers.template_get("weekly_report", language="en")
    assert got.ok
    assert "Template weekly_report" in got.message
    assert "Fill in:" in got.message


def test_get_requires_a_name(handlers):
    result = handlers.template_get()
    assert not result.ok
    assert result.need_param == "template"
    assert "weekly_report" in result.message


def test_get_unknown_template_lists_alternatives(handlers):
    result = handlers.template_get("nope")
    assert not result.ok
    assert "project_plan" in result.message


def test_get_returns_skeleton_inputs_knowledge_and_plan(handlers):
    result = handlers.template_get("weekly_report")
    assert result.ok
    payload = json.loads(result.detail)
    assert payload["id"] == "weekly_report"
    assert payload["export"]["default"] == "pdf"
    assert payload["export"]["target"] == "pdf"
    assert {item["key"] for item in payload["inputs"]} == {"owner", "period", "team"}
    assert payload["knowledge"]["local"] == ["file_search"]
    assert payload["placeholders"]
    actions = [step["action"] for step in payload["plan"]]
    assert actions[0] == "doc_new" and actions[-1] == "export_render"
    assert payload["plan_note"]["execute_with"].startswith("appcli_run")


def test_get_supports_preset_override_and_refuses_others(handlers):
    ok = handlers.template_get("weekly_report", preset="docx")
    assert ok.ok
    assert json.loads(ok.detail)["plan"][-1]["action"] == "export_docx"

    bad = handlers.template_get("weekly_report", preset="xlsx")
    assert not bad.ok
    assert "参数错误" in bad.message
