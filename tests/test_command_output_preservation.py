"""原始命令输出经过执行器、pipeline 和 HTTP 后仍保持文本语义。"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution import md_format, system_tools
from bibit_intent.execution.executor import Executor
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.execution.system_tools import ExecResult
from bibit_intent.pipeline import EmbeddedBibitPipeline, _format_reply, _readonly_detail
from bibit_intent.resources import resource_path
from bibit_intent.routing.router import Router
from bibit_intent.routing.rule_engine import RuleEngine
from bibit_intent.server import BibitIntentService


RAW = "\n    root  value\n\tchild\n\n        leaf  \n\n"


def load_handlers(skill):
    spec = importlib.util.spec_from_file_location(
        f"output_preservation_{skill}", resource_path(f"skills/{skill}/handlers.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("skill", ["system", "ubuntu_cmd", "ubuntu_admin", "wifi"])
@pytest.mark.parametrize("returncode", [0, 1])
def test_command_capture_preserves_whitespace(monkeypatch, skill, returncode):
    module = system_tools if skill == "system" else load_handlers(skill)
    monkeypatch.setattr(module, "run_command", lambda *a, **kw: subprocess.CompletedProcess(
        a[0], returncode, stdout=RAW, stderr=RAW
    ))
    result = module._run(["example"])
    assert result.detail == RAW
    assert result.detail_format == "text"
    assert result.ok == (returncode == 0)


@pytest.mark.parametrize("via_manager", [False, True])
@pytest.mark.parametrize("returncode,raw", [(0, RAW), (1, RAW), (1, "\n  incorrect password\n\n")])
def test_sudo_output_preserves_whitespace_and_auth_state(monkeypatch, via_manager, returncode, raw):
    module = load_handlers("ubuntu_admin")
    monkeypatch.setattr(module, "_session_askpass_env", lambda: {"SUDO_ASKPASS": "/bin/true"})
    monkeypatch.setattr(module, "_install_askpass_wrapper", lambda *a: None)
    monkeypatch.setattr(module, "_no_new_privs_active", lambda: via_manager)
    monkeypatch.setattr(module, "_usr_mount_read_only", lambda: False)
    monkeypatch.setattr(module, "_authorize_sudo", lambda *a: None)
    withdrawn = []
    calls = []
    monkeypatch.setattr(module, "end_execution_without_side_effect", lambda: withdrawn.append(True))

    def command(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout=raw, stderr=raw)

    monkeypatch.setattr(module, "run_command", command)
    result = module._run_sudo(["example"])
    assert result.detail == raw and result.detail_format == "text"
    assert result.ok == (returncode == 0)
    assert calls[0][0] == ("systemd-run" if via_manager else "sudo")
    assert withdrawn == ([True] if "incorrect password" in raw else [])


def test_installation_and_wifi_failure_wrappers_keep_raw_format(monkeypatch):
    admin = load_handlers("ubuntu_admin")
    raw = "\n  E: Unmet dependencies\n\n"
    result = admin._installation_failure(admin.ExecResult(False, "失败", raw, detail_format="text"))
    assert result.detail == raw and result.detail_format == "text"
    assert "```text\n" + raw + "```" in _format_reply(result)
    wifi = load_handlers("wifi")
    monkeypatch.setattr(wifi, "_find_saved_profile", lambda *a: "example")
    monkeypatch.setattr(wifi, "_radio_state", lambda: ("wlan0", "disconnected"))
    monkeypatch.setattr(wifi, "_run", lambda *a, **kw: wifi.ExecResult(False, "失败", RAW, detail_format="text"))
    result = wifi.wifi_connect(ssid="example")
    assert result.detail == RAW and result.detail_format == "text"


@pytest.mark.parametrize("tool", ["eth_up", "eth_down", "eth_status", "eth_ip"])
def test_ethernet_discovery_errors_remain_raw(monkeypatch, tool):
    monkeypatch.setattr(system_tools, "run_command", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 1, stdout="", stderr=RAW
    ))
    result = getattr(system_tools, tool)()
    assert result.detail == RAW and result.detail_format == "text"
    assert RAW not in result.message


def test_bluetooth_error_keeps_existing_noise_cleanup(monkeypatch):
    module = load_handlers("bluetooth")
    raw = "\x1b[31mFailed to disconnect\x1b[0m\n  Not available\n"
    monkeypatch.setattr(module, "_session", lambda *a, **kw: SimpleNamespace(
        started=True, raw=raw, echoed=frozenset(), error=""
    ))
    result = module.bt_disconnect()
    assert not result.ok and result.detail_format == "text"
    assert result.detail == "Failed to disconnect\nNot available"
    assert "```text\n" + result.detail + "\n```" in _format_reply(result)


@pytest.mark.parametrize("text", [RAW, "", " \t\n\n", "no final newline", "a\r\nb\r\n"])
def test_fence_preserves_body_and_only_adds_required_separator(text):
    block = md_format.code_block(text)
    separator = "" if not text or text.endswith(("\n", "\r")) else "\n"
    assert block == "```text\n" + text + separator + "```"


def test_fence_cannot_be_closed_by_command_output():
    raw = "```\n# title\n`````\n<svg>**$5**</svg>\n~~~\n"
    assert md_format.code_block(raw) == "``````text\n" + raw + "``````"


def test_truncation_note_is_outside_closed_fence():
    raw = "  abc\n" * 1000
    block = md_format.code_block(raw, limit=10)
    assert block.startswith("```text\n" + raw[:10] + "\n```\n\n")
    assert "截断" in block.split("\n```\n\n")[1]
    assert str(len(raw)) in block


def test_raw_reply_keeps_whitespace_and_localizes_only_message():
    result = ExecResult(False, "命令失败(码1)", RAW, detail_format="text")
    assert _format_reply(result, language="en") == (
        "Command failed (code 1)\n\n```text\n" + RAW + "```"
    )
    assert result.detail == RAW


def test_raw_json_is_not_business_metadata():
    raw = json.dumps({"ok": True, "read_only": True, "value": "example"})
    result = ExecResult(True, "查询完成", raw, detail_format="text")
    assert _readonly_detail(result) is None
    assert raw in _format_reply(result)
    assert "```text" in _format_reply(result)


def test_markdown_and_legacy_results_remain_compatible():
    table = "| 网卡 | IP |\n|---|---|\n| eth0 | 127.0.0.1 |"
    assert _format_reply(ExecResult(True, "网络信息", table)) == "网络信息\n" + table
    legacy = SimpleNamespace(ok=False, message="失败", detail="raw error")
    assert _format_reply(legacy) == "失败\nraw error"
    result = ExecResult(True, "只读结果", '{"ok": true, "read_only": true}')
    assert _format_reply(result) == "只读结果"


def test_executor_preserves_skill_format_and_legacy_default():
    registry = ToolRegistry(resource_path("tools.yaml"))
    module = load_handlers("ubuntu_cmd")
    skill = module.ExecResult(True, "完成", RAW, detail_format="text")
    executor = Executor(registry, handlers={"get_system_info": lambda **kw: skill})
    result = executor.execute("get_system_info")
    assert result.detail == RAW and result.detail_format == "text"
    skill = SimpleNamespace(ok=True, message="完成", detail="legacy")
    assert executor.execute("get_system_info").detail_format == "markdown"


def test_read_bundle_formats_raw_without_cutting_fences():
    registry = ToolRegistry(resource_path("tools.yaml"))
    handlers = {}
    load_skills(resource_path("skills"), registry, handlers)
    handlers["sys_kernel"] = lambda **kw: ExecResult(True, "内核", RAW, detail_format="text")
    handlers["sys_version"] = lambda **kw: ExecResult(True, "版本", "|版本|\n|---|\n|1|")
    executor = Executor(registry, handlers=handlers)
    result = executor.execute_read_bundle(["sys_kernel", "sys_version"])
    assert "```text\n" + RAW + "```" in result.detail
    assert "|版本|" in result.detail
    handlers["sys_kernel"] = lambda **kw: ExecResult(
        True, "内核", "```text\n" + "x" * 13000 + "\n```"
    )
    result = executor.execute_read_bundle(["sys_kernel", "sys_version"])
    assert result.detail.count("```") % 2 == 0
    assert "截断" in result.detail


@pytest.mark.parametrize("tool,returncode", [("usb_tree", 0), ("usb_detail", 0), ("usb_tree", 1)])
@pytest.mark.asyncio
async def test_usb_command_through_real_router_executor_pipeline_and_http(monkeypatch, tool, returncode):
    registry = ToolRegistry(resource_path("tools.yaml"))
    handlers = {}
    load_skills(resource_path("skills"), registry, handlers)
    calls = []

    def command(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode,
                                           stdout=RAW if returncode == 0 else "",
                                           stderr=RAW if returncode else "")

    monkeypatch.setitem(handlers[tool].__globals__, "run_command", command)
    executor = Executor(registry, handlers=handlers)
    facade = EmbeddedBibitPipeline(Router(registry, RuleEngine(registry), None, executor))
    app = BibitIntentService(facade).create_app()
    prompt = "查看USB设备树" if tool == "usb_tree" else "USB详细"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/runs", json={"prompt": prompt, "session_id": tool})
    assert response.status_code == 200
    outcome = response.json()
    assert outcome["handled"] and outcome["metadata"]["tool"] == tool
    assert outcome["reply"].count(RAW) == 1
    assert "```text\n" + RAW + "```" in outcome["reply"]
    assert calls == [["lsusb", "-t" if tool == "usb_tree" else "-v"]]
    if returncode:
        assert "命令失败(码1)" in outcome["reply"]


@pytest.mark.parametrize("domain", ["wifi", "bluetooth"])
def test_connect_orchestration_keeps_raw_format(monkeypatch, domain):
    module = load_handlers("connect")
    error = SimpleNamespace(ok=False, message="失败", detail=RAW, detail_format="text")
    wifi = SimpleNamespace(
        visible_ssids=lambda **kw: ["example"] if domain == "wifi" else [],
        saved_profile_name=lambda *a: None,
        wifi_connect=lambda **kw: error,
    )
    bt = SimpleNamespace(
        paired_device_matches=lambda *a: [("AA:BB:CC:DD:EE:FF", "example")] if domain == "bluetooth" else [],
        known_device_matches=lambda *a: [],
        bt_connect=lambda **kw: error,
    )
    monkeypatch.setattr(module, "_sibling", lambda skill: wifi if skill == "wifi" else bt)
    result = module.connect_any(device="example")
    assert result.detail_format == "text" and RAW in result.detail
    assert RAW in _format_reply(result) and "```text" in _format_reply(result)


def test_pip_build_tools_failure_keeps_raw_format(monkeypatch, tmp_path):
    module = load_handlers("ubuntu_admin")
    monkeypatch.setattr(module, "_pending_build_tools", lambda *a: ["gcc"])
    monkeypatch.setattr(module, "_run_sudo", lambda *a, **kw: module.ExecResult(
        False, "失败", RAW, detail_format="text"
    ))
    result, installed = module._install_pip_item({}, tmp_path, position=1, total=1, label="example")
    assert not installed and result.detail == RAW and result.detail_format == "text"
    assert "```text\n" + RAW + "```" in _format_reply(result)


def test_scalar_command_values_stay_single_line_in_tables(monkeypatch):
    module = load_handlers("ubuntu_cmd")
    monkeypatch.setattr(module, "run_command", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, stdout="64\n" if cmd[0] == "getconf" else "example\n", stderr=""
    ))
    assert "| 内核版本 | example |" in module._kernel_version_table()
    assert "| 机器架构 | example |" in module._arch_table()
    assert "| 位数 | 64 bit |" in module._arch_table()
