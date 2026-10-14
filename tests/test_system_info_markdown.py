"""get_system_info 的 Markdown 格式化与本地路由回归测试。"""

from __future__ import annotations

import pytest

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.system_tools import get_system_info
from bibit_intent.routing.rule_engine import RuleEngine


@pytest.fixture(scope="module")
def rule():
    return RuleEngine(ToolRegistry())


@pytest.mark.parametrize(
    ("text", "expected_item"),
    [
        ("查看cpu", "cpu"),
        ("查看CPU", "cpu"),
        ("View CPU", "cpu"),
        ("Show Processor", "cpu"),
        ("查看内存", "memory"),
        ("查看内存使用率", "memory"),
        ("Show Memory", "memory"),
        ("查看cpu温度", "temp"),
        ("查看CPU温度", "temp"),
        ("View CPU Temperature", "temp"),
        ("查看cpu和cpu温度", "cpu_temp"),
        ("查看CPU和CPU温度", "cpu_temp"),
        ("View CPU and CPU temperature", "cpu_temp"),
        ("查看温度", "temp"),
        ("查看系统状态", "all"),
    ],
)
def test_system_info_local_slots(rule, text, expected_item):
    match = rule.match(text)
    assert match is not None
    assert match.tool_name == "get_system_info"
    assert match.slots.get("item") == expected_item


@pytest.mark.parametrize("item", ["cpu", "memory", "temp", "disk", "all"])
def test_get_system_info_returns_markdown_table(item):
    result = get_system_info(item=item)
    assert result.ok is True
    assert "|" in result.detail
    assert "---" in result.detail
    assert result.detail.count("\n") >= 3
