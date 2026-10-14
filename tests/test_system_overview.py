"""Generic multi-object read-query routing and output regression tests."""

from __future__ import annotations

import pytest

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.executor import Executor
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.execution.system_tools import ExecResult
from bibit_intent.resources import resource_path
from bibit_intent.routing.intent import (
    IntentAction,
    _resolve_multi_object_read,
    decide_local_intent,
)
from bibit_intent.routing.multi_action import detect_multi_object_query
from bibit_intent.routing.rule_engine import RuleEngine


@pytest.fixture(scope="module")
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    load_skills(resource_path("skills"), reg, {})
    return reg


@pytest.fixture(scope="module")
def rule(registry: ToolRegistry) -> RuleEngine:
    return RuleEngine(registry)


@pytest.mark.parametrize(
    ("text", "expected_objects"),
    [
        (
            "查看系统版本、内核版本、CPU型号和核心数、内存大小、开机时长",
            ["系统版本", "内核版本", "CPU型号", "核心数", "内存大小", "开机时长"],
        ),
        ("查看系统版本,,内核版本", ["系统版本", "内核版本"]),
        ("查看系统版本、内核版本，内存大小", ["系统版本", "内核版本", "内存大小"]),
        ("查看网卡、IP和DNS", ["网卡", "IP", "DNS"]),
        ("查看系统版本和内核版本以及内存大小", ["系统版本", "内核版本", "内存大小"]),
    ],
)
def test_detect_multi_object_query_handles_separator_variants(text, expected_objects):
    result = detect_multi_object_query(text)
    assert result.is_multi is True
    assert result.objects == expected_objects


@pytest.mark.parametrize(
    ("objects", "expected_tools"),
    [
        (["系统版本", "内核版本"], {"sys_version", "sys_kernel"}),
        (["CPU型号", "核心数"], {"sys_cpu"}),
        (["USB设备", "PCI设备"], {"sys_pci", "usb_list"}),
        (["网卡", "IP", "DNS"], {"net_ip"}),
        (["内存型号", "大小"], None),
    ],
)
def test_resolve_multi_object_read_is_minimal_and_rejects_incomplete(
    registry, objects, expected_tools
):
    resolved = _resolve_multi_object_read(objects, registry)
    if expected_tools is None:
        assert resolved is None
    else:
        assert resolved is not None
        assert set(resolved) == expected_tools


@pytest.mark.parametrize(
    ("text", "expected_tools"),
    [
        (
            "查看系统版本、内核版本、CPU型号和核心数、内存大小、开机时长",
            {"sys_version", "sys_kernel", "sys_cpu", "sys_mem_size", "sys_uptime"},
        ),
        ("查看系统版本和内核版本", {"sys_version", "sys_kernel"}),
        ("查看USB设备和PCI设备", {"sys_pci", "usb_list"}),
    ],
)
def test_multi_object_read_queries_route_to_read_bundle(rule, registry, text, expected_tools):
    plan = decide_local_intent(text, rule, registry, embed=None)
    assert plan.action is IntentAction.RULE_CALL
    assert plan.tool_name == "__read_bundle__"
    assert set(plan.slots["tools"]) == expected_tools


@pytest.mark.parametrize(
    ("text", "expected_tool"),
    [
        ("查看CPU型号和核心数", "sys_cpu"),
        ("查看系统版本", "sys_version"),
        ("查看内核版本", "sys_kernel"),
        ("查看内存大小", "sys_mem_size"),
        ("查看系统状态", "get_system_info"),
    ],
)
def test_single_or_specific_system_info_queries_are_not_hijacked(rule, registry, text, expected_tool):
    plan = decide_local_intent(text, rule, registry, embed=None)
    assert plan.action is IntentAction.RULE_CALL
    assert plan.tool_name == expected_tool


def test_ambiguous_multi_object_read_escalates(rule, registry):
    plan = decide_local_intent("查看内存型号和大小", rule, registry, embed=None)
    assert plan.action is IntentAction.ESCALATE
    assert plan.reason == "multi_object_read"


def test_execute_read_bundle_accepts_only_readonly_tools(registry):
    handlers = {
        "sys_kernel": lambda **_: ExecResult(True, "kernel ok", "kernel detail"),
        "sys_version": lambda **_: ExecResult(True, "version ok", "version detail"),
    }
    executor = Executor(registry, handlers=handlers)

    result = executor.execute_read_bundle(["sys_kernel", "sys_version"], language="zh")
    assert result.ok is True
    assert "kernel detail" in result.detail
    assert "version detail" in result.detail

    rejected = executor.execute_read_bundle(["launch_app"], language="zh")
    assert rejected.ok is False
    assert "非只读" in rejected.message


def test_sys_overview_handler_returns_all_sections():
    handlers: dict = {}
    reg = ToolRegistry()
    load_skills(resource_path("skills"), reg, handlers)
    result = handlers["sys_overview"]()
    assert result.ok is True
    assert "系统概览" in result.detail
    assert "系统版本" in result.detail
    assert "内核版本" in result.detail
    assert "CPU" in result.detail
    assert "内存" in result.detail
    assert "系统位数" in result.detail
    assert "磁盘" in result.detail
    assert "温度" in result.detail
    assert "开机时长" in result.detail
    assert "|" in result.detail
    assert "---" in result.detail
