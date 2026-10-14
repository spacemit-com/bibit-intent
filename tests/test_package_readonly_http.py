import json

import httpx
import pytest

from bibit_intent.core import package_preview
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.executor import Executor
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.execution.system_tools import ExecResult
from bibit_intent.pipeline import EmbeddedBibitPipeline, _format_reply
from bibit_intent.resources import resource_path
from bibit_intent.routing.router import Router
from bibit_intent.routing.rule_engine import RuleEngine
from bibit_intent.server import BibitIntentService


@pytest.fixture
def pipeline():
    registry = ToolRegistry(resource_path("tools.yaml"))
    handlers = {}
    load_skills(resource_path("skills"), registry, handlers)
    calls = []
    def preview(**params):
        calls.append(params)
        payload = {"ok": True, "read_only": True, "items": [], **params}
        return ExecResult(True, "只读匹配结果", json.dumps(payload))
    handlers["pkg_match_preview"] = preview
    router = Router(registry, RuleEngine(registry), None, Executor(registry, handlers=handlers))
    return EmbeddedBibitPipeline(router), calls, handlers


@pytest.mark.asyncio
async def test_full_http_keeps_details_out_of_reply(pipeline):
    facade, calls, _ = pipeline
    app = BibitIntentService(facade).create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/runs", json={"prompt": "软件包预览 已安装 正则 ^bash.*$", "session_id": "full"})
    assert response.status_code == 200
    result = response.json()
    assert result["handled"] and result["reply"] == "只读匹配结果"
    assert result["metadata"]["result"]["pattern"] == "^bash.*$"
    assert result["metadata"]["result"]["read_only"] and len(calls) == 1


@pytest.mark.parametrize("text,need", [("软件包预览", "scope"), ("软件包预览 已安装", "mode"), ("软件包预览 已安装 正则", "pattern"), ("软件包预览 regex ^bash.*$", "scope")])
def test_missing_slots_no_model_no_command(pipeline, text, need):
    facade, calls, _ = pipeline
    result = facade.handle(text, "missing")
    assert result.handled and need in result.reply and "如 vim" not in result.reply
    assert not calls


def test_multi_turn_preview(pipeline):
    facade, calls, _ = pipeline
    assert "scope" in facade.handle("软件包预览", "multi").reply
    assert "mode" in facade.handle("scope=installed", "multi").reply
    assert "pattern" in facade.handle("mode=regex", "multi").reply
    assert facade.handle("pattern=^bash.*$", "multi").handled
    assert len(calls) == 1 and calls[0]["pattern"] == "^bash.*$"


def test_chinese_slot_replies_and_glob_question_mark(pipeline):
    facade, calls, _ = pipeline
    assert "scope" in facade.handle("软件包预览", "chinese").reply
    assert "mode" in facade.handle("已安装", "chinese").reply
    assert "pattern" in facade.handle("通配符", "chinese").reply
    facade.handle("bash?", "chinese")
    assert len(calls) == 1 and calls[0]["pattern"] == "bash?"
    assert calls[0]["scope"] == "installed" and calls[0]["mode"] == "glob"


@pytest.mark.parametrize("text,tool,expected", [
    ("软件包候选 bash", "pkg_candidate", {"pkg": "bash"}),
    ("软件包预检 安装 bash", "pkg_operation_preview", {"pkg": "bash", "operation": "install"}),
    ("软件包预检 卸载 bash", "pkg_operation_preview", {"pkg": "bash", "operation": "remove"}),
    ("软件包验证 bash 1:5.0-1", "pkg_verify", {"pkg": "bash", "expected_version": "1:5.0-1"}),
])
def test_new_tools_full_router(pipeline, text, tool, expected):
    facade, _, handlers = pipeline
    calls = []
    def handler(**params):
        calls.append(params)
        return ExecResult(True, "只读结果")
    handlers[tool] = handler
    result = facade.handle(text, "check")
    assert result.handled and result.metadata["tool"] == tool
    assert len(calls) == 1 and all(calls[0][key] == value for key, value in expected.items())


def test_delivery_assessment_route_preserves_structured_readonly_result(pipeline):
    facade, _, handlers = pipeline
    payload = {
        "ok": True,
        "read_only": True,
        "target": "nodejs",
        "diagnosis": "repo_candidate_found",
        "network_accessed": False,
        "write_performed": False,
    }
    handlers["software_delivery_assess"] = lambda **_: ExecResult(
        True, "只读交付评估", json.dumps(payload, ensure_ascii=False)
    )
    result = facade.handle("软件交付评估 nodejs", "delivery")
    assert result.handled and result.metadata["tool"] == "software_delivery_assess"
    assert result.reply == "只读交付评估"
    assert result.metadata["result"]["diagnosis"] == "repo_candidate_found"
    assert result.metadata["result"]["read_only"]


def test_legacy_errors_and_malformed_details_not_hidden():
    assert _format_reply(ExecResult(False, "失败", "raw error")) == "失败\nraw error"
    assert _format_reply(ExecResult(False, "失败", "{bad")) == "失败\n{bad"
    detail = json.dumps({"ok": False, "read_only": True, "diagnostic": "raw error"})
    assert _format_reply(ExecResult(False, "明确错误", detail)) == "明确错误"


def test_preview_parser_preserves_pattern():
    assert package_preview.preview_slots("软件包预览 已安装 正则 ^(foo|bar)[a-z]+$")["pattern"] == "^(foo|bar)[a-z]+$"
    assert package_preview.preview_slots("安装 bash*") is None
