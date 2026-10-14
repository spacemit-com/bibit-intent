"""H11：appcli 工具在 MCP 路径上的审批回归。

Runtime 的审批策略看的是 MCP ``ToolAnnotations``：``readOnlyHint`` 为假即需要人工审批。
本文件固定三件事：
- `appcli_run` 变更动作 → 非只读 + destructive → 必须审批；
- `appcli_list` / `appcli_verify` → 只读 → 不弹审批；
- 分组过滤下 readonly/admin 的可见性与注解一致（只读工具不会因为分组被当成变更类）。
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

from bibit_intent.mcp_server.server import _allowed_groups, _build_executor, _tool_annotations


def _flags(tool) -> dict:
    """把 ToolAnnotations 归一成 (read_only, destructive)，兼容 mcp 1.x/2.x 字段名。"""
    annotations = _tool_annotations(tool)
    assert annotations is not None, "MCP SDK 缺失时注解会消失，审批策略就失去依据"
    read_only = getattr(
        annotations, "read_only_hint", getattr(annotations, "readOnlyHint", None)
    )
    destructive = getattr(
        annotations, "destructive_hint", getattr(annotations, "destructiveHint", None)
    )
    return {"read_only": read_only, "destructive": destructive}


def test_appcli_run_requires_approval():
    registry, _ = _build_executor()
    flags = _flags(registry.get("appcli_run"))

    assert flags["read_only"] is False
    assert flags["destructive"] is True


@pytest.mark.parametrize("tool_name", ["appcli_list", "appcli_verify"])
def test_read_only_appcli_tools_never_prompt(tool_name):
    registry, _ = _build_executor()
    flags = _flags(registry.get(tool_name))

    assert flags["read_only"] is True
    assert flags["destructive"] is False


def test_group_filter_matches_annotations():
    registry, _ = _build_executor()

    readonly_names = {tool.name for tool in registry.all() if tool.mcp_group() == "readonly"}
    assert {"appcli_list", "appcli_verify"} <= readonly_names
    assert "appcli_run" not in readonly_names
    assert registry.get("appcli_run").mcp_group() == "admin"


def test_appcli_run_schema_exposes_the_documented_parameters():
    """Runtime 侧的模型只能照 schema 调用：参数类型与默认值必须在这里对齐。"""
    registry, _ = _build_executor()
    schema = registry.get("appcli_run").mcp_schema()["function"]
    props = schema["parameters"]["properties"]

    assert props["args"]["type"] == "array"
    assert props["args"]["items"]["type"] == "string"
    assert props["timeout"]["type"] == "integer"
    assert props["project"]["type"] == "string"
    assert props["output"]["type"] == "string"
    assert set(schema["parameters"]["required"]) == {"appcli", "action"}


def test_stdio_listing_carries_the_annotations_runtime_decides_on():
    """跨进程确认一次：Runtime 真的能从 tools/list 拿到这些注解。"""
    pytest.importorskip("mcp.server.fastmcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from pathlib import Path

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "bibit_intent.mcp_server"],
            env={
                **os.environ,
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            },
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                return {tool.name: tool.annotations for tool in listed.tools}

    annotations = asyncio.run(run())
    assert "appcli_run" in annotations and "appcli_verify" in annotations

    def read(annotation, snake, camel):
        value = getattr(annotation, snake, None)
        return getattr(annotation, camel, None) if value is None else value

    run_annotation = annotations["appcli_run"]
    assert read(run_annotation, "read_only_hint", "readOnlyHint") is False
    assert read(run_annotation, "destructive_hint", "destructiveHint") is True

    verify_annotation = annotations["appcli_verify"]
    assert read(verify_annotation, "read_only_hint", "readOnlyHint") is True
    assert read(verify_annotation, "destructive_hint", "destructiveHint") is False


def test_every_confirmed_tool_carries_the_destructive_annotation():
    """confirm 工具必须让 Runtime 看得见：注解丢了，审批就静默消失。

    MCP 侧不再自己拦一道（见 `mcp_confirmation`），审批的唯一依据就是这条注解。
    新增一个 confirm 工具却忘了注解，等于在云端路径上开了一个无审批的变更口子，
    所以这里逐个工具固定下来。
    """
    registry, _ = _build_executor()
    confirmed = [
        tool for tool in registry.all() if bool(tool.system.get("confirm"))
    ]
    assert confirmed, "注册表里应当存在需要确认的工具"

    unprotected = []
    for tool in confirmed:
        flags = _flags(tool)
        if flags["destructive"] is not True or flags["read_only"] is True:
            unprotected.append(tool.name)
    assert unprotected == [], f"这些 confirm 工具没有 destructive 注解：{unprotected}"


def test_mcp_confirmation_delegates_to_the_runtime():
    """确认交给 Runtime：否则任何 confirm 工具在 MCP 上都无法执行。"""
    from bibit_intent.mcp_server.server import mcp_confirmation

    assert mcp_confirmation("执行应用命令行：appcli=libreoffice") is True
    assert mcp_confirmation("安装软件包：pkg=curl") is True


def test_confirmed_tool_reaches_its_handler_over_mcp():
    """回归：K3 实测「已取消：执行产出物 appcli（需确认）」把云端产出路径堵死。"""
    _, executor = _build_executor()
    result = executor.execute(
        "appcli_run", {"appcli": "not-registered", "action": "export_render"}
    )
    assert "需确认" not in result.message
    assert result.ok is False
    assert "未登记的应用" in result.message
