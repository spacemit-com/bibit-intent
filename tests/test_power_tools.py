"""电源工具（关机/重启/睡眠）回归测试：路由、执行与 polkit 拒绝路径。

背景：设备此前只有 reboot，且 MCP 工具进程跑在无 seat 的 Runtime 系统服务
里，logind 会回 "Interactive authentication required"，电源动作静默失败。
这里锁定：规则层能确定性识别关机/休眠，关机不会误吞"关闭浏览器"，handler
在成功/被拒/硬件不支持三种情况下给出可读结果。
"""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution import system_tools
from bibit_intent.execution.system_tools import ExecResult
from bibit_intent.routing.rule_engine import RuleEngine

_registry = ToolRegistry()
_rule = RuleEngine(_registry)


def _match(text: str) -> str | None:
    m = _rule.match(text)
    return m.tool_name if m else None


def _fake_run(monkeypatch, result: ExecResult | None = None):
    calls: list[list[str]] = []

    def fake(cmd, **kwargs):
        calls.append(list(cmd))
        return result if result is not None else ExecResult(True, "ok")

    monkeypatch.setattr(system_tools, "_run", fake)
    return calls


def test_registry_exposes_power_tools():
    assert _registry.get("shutdown").mcp_group() == "admin"
    assert _registry.get("sleep").mcp_group() == "admin"
    assert _registry.get("shutdown").mcp_schema()["function"]["name"] == "shutdown"


def test_reboot_receipt_is_preserved_but_not_displayed(monkeypatch):
    import json
    from bibit_intent.pipeline import _format_reply

    _fake_run(monkeypatch)
    result = system_tools.reboot()
    assert json.loads(result.detail)["status"] == "accepted"
    assert _format_reply(result) == result.message


def test_shutdown_patterns_route_locally():
    for text in ("关机", "帮我关机", "关机吧", "关闭系统", "关闭电脑", "把电脑关机", "关掉电脑"):
        assert _match(text) == "shutdown", text
    assert _match("shutdown the computer") == "shutdown"
    assert _match("turn off my computer") == "shutdown"


def test_sleep_patterns_route_locally():
    for text in ("休眠", "睡眠", "待机", "挂起", "进入休眠模式", "待机一下"):
        assert _match(text) == "sleep", text
    assert _match("suspend the system") == "sleep"
    assert _match("put the system to sleep") == "sleep"


def test_close_app_keeps_its_own_targets():
    # "关闭 X"里 X 不是整机名时仍归 close_app，不能被 shutdown 抢走。
    assert _match("关闭浏览器") == "close_app"
    assert _match("关闭终端") == "close_app"


def test_shutdown_runs_systemctl_poweroff(monkeypatch):
    calls = _fake_run(monkeypatch)
    r = system_tools.shutdown()
    assert r.ok is True
    assert calls == [["systemctl", "poweroff"]]
    assert "关机" in r.message


def test_reboot_schedules_before_reporting_acceptance(monkeypatch):
    calls = _fake_run(monkeypatch)
    result = system_tools.reboot()
    assert result.ok is True
    assert calls == [["systemctl", "reboot", "--when=+1min"]]
    assert '"status":"accepted"' in result.detail
    assert "一分钟" in result.message


def test_reboot_failure_never_falls_back_to_immediate_reboot(monkeypatch):
    calls = _fake_run(monkeypatch, ExecResult(False, "unsupported or denied"))
    result = system_tools.reboot()
    assert not result.ok
    assert not result.detail
    assert len(calls) == 1


def test_power_action_surfaces_command_failure(monkeypatch):
    _fake_run(monkeypatch, ExecResult(False, "命令失败(码1)", "Failed to connect to bus"))
    r = system_tools.shutdown()
    assert r.ok is False
    assert r.detail == "Failed to connect to bus"


def test_power_action_maps_authentication_failure(monkeypatch):
    _fake_run(monkeypatch, ExecResult(False, "命令失败(码1)", "Interactive authentication required."))
    r = system_tools.shutdown()
    assert r.ok is False
    assert "未执行" in r.message


def test_reboot_preserves_authorization_error(monkeypatch):
    calls = _fake_run(monkeypatch, ExecResult(False, "failed", "Access denied", detail_format="text"))
    result = system_tools.reboot()
    assert not result.ok
    assert "需要授权" in result.message
    assert result.detail == "Access denied"
    assert result.detail_format == "text"
    assert len(calls) == 1


def test_sleep_reports_unsupported_hardware(monkeypatch):
    monkeypatch.setattr(system_tools, "_power_states", lambda: [])
    calls = _fake_run(monkeypatch)
    r = system_tools.sleep()
    assert r.ok is False
    assert "不支持" in r.message
    assert calls == []


def test_sleep_uses_suspend_without_disk(monkeypatch):
    monkeypatch.setattr(system_tools, "_power_states", lambda: ["freeze", "mem"])
    calls = _fake_run(monkeypatch)
    r = system_tools.sleep()
    assert r.ok is True
    assert calls == [["systemctl", "suspend"]]


def test_sleep_prefers_hibernate_when_kernel_supports_it(monkeypatch):
    monkeypatch.setattr(system_tools, "_power_states", lambda: ["freeze", "mem", "disk"])
    calls = _fake_run(monkeypatch)
    r = system_tools.sleep()
    assert r.ok is True
    assert calls == [["systemctl", "hibernate"]]
