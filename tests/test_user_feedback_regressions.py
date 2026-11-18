from types import SimpleNamespace

import pytest

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution import locale_tools, system_tools
from bibit_intent.execution.system_tools import ExecResult
from bibit_intent.pipeline import EmbeddedBibitPipeline, _format_reply
from bibit_intent.resources import resource_path
from bibit_intent.routing.rule_engine import RuleEngine
from bibit_intent.routing.router import Stage


@pytest.mark.parametrize("text,tool", [
    ("查看当前音量", "get_vol_status"),
    ("查看当前屏幕亮度", "get_display_light"),
    ("把系统音量调大一点", "adjust_volume"),
    ("把系统音量调小一点", "adjust_volume"),
    ("把屏幕亮度调高一点", "adjust_brightness"),
    ("把屏幕亮度调低一点", "adjust_brightness"),
    ("静音", "mute"),
])
@pytest.mark.parametrize("suffix", ["。", "！", "？", "!", "?", "？！  "])
def test_terminal_punctuation_keeps_tool_and_slots(text, tool, suffix):
    engine = RuleEngine(ToolRegistry(resource_path("tools.yaml")))
    plain = engine.match(text)
    punctuated = engine.match(text + suffix)
    assert plain.tool_name == punctuated.tool_name == tool
    assert plain.slots == punctuated.slots
    assert plain.confidence == punctuated.confidence


@pytest.mark.parametrize("execution,status", [
    (ExecResult(True, "成功"), "succeeded"),
    (ExecResult(False, "失败"), "failed"),
    (ExecResult(False, "需要参数", need_param="level"), "awaiting_input"),
    (ExecResult(False, "确认", confirmation={"kind": "test"}), "awaiting_confirmation"),
    (ExecResult(False, "尚未确认", unverified=True), "unverified"),
])
def test_pipeline_preserves_task_status(execution, status):
    result = SimpleNamespace(stage=Stage.RULE, escalate_reason="", latency_ms=0,
                             tool_name="set_volume", exec_result=execution)
    router = SimpleNamespace(handle=lambda *args, **kwargs: result)
    outcome = EmbeddedBibitPipeline(router).handle("测试", "test")
    assert outcome.metadata["task_status"] == status


def test_locale_query_hides_internal_values(monkeypatch):
    monkeypatch.setattr(system_tools, "_settings_host_guard", lambda _: None)
    monkeypatch.setattr(locale_tools, "_read", lambda _: ({"LANG": "zh_CN.UTF-8"}, "zh_CN.UTF-8", "zh_CN.UTF-8"))
    result = locale_tools.get_locale_settings()
    reply = _format_reply(result)
    assert "简体中文" in reply
    assert "configured_values" not in reply and "```" not in reply
    assert "configured_values" in result.detail


def test_clock_note_is_not_a_code_block(monkeypatch):
    monkeypatch.setattr(system_tools, "_settings_host_guard", lambda _: None)
    reply = _format_reply(system_tools.get_system_time())
    assert "未验证网络校时状态" in reply and "```" not in reply


def test_available_locales_have_readable_summary(monkeypatch):
    monkeypatch.setattr(system_tools, "_settings_host_guard", lambda _: None)
    monkeypatch.setattr(locale_tools, "_installed", lambda: {"zh_CN": "zh_CN.utf8", "en_US": "en_US.utf8"})
    result = locale_tools.list_locale_options()
    reply = _format_reply(result)
    assert "可用语言" in reply and "简体中文" in reply and "可用区域" in reply
    assert "installed_locales" not in reply and "```" not in reply


def test_delayed_launch_is_verified_within_bounded_window(monkeypatch):
    times = iter([0.0, 0.0, 4.0])
    processes = iter([set(), set(), {11}])
    monkeypatch.setattr(system_tools.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(system_tools.time, "sleep", lambda _: None)
    monkeypatch.setattr(system_tools, "_process_pids", lambda _: next(processes))
    result = system_tools._verify_launch(["/usr/bin/chromium"], "浏览器", before_pids={10})
    assert result.ok and not result.unverified
