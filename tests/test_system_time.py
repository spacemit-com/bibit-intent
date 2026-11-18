import pytest

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.core.system_time import parse_system_time_request
from bibit_intent.execution import system_tools
from bibit_intent.execution.executor import Executor
from bibit_intent.execution.system_tools import ExecResult
from bibit_intent.routing.router import Router, Stage
from bibit_intent.routing.rule_engine import RuleEngine


@pytest.fixture(autouse=True)
def automatic_sync_disabled(monkeypatch):
    from bibit_intent.execution import time_confirmation

    monkeypatch.setattr(time_confirmation, "ntp_enabled", lambda: False)


@pytest.fixture
def time_router():
    registry = ToolRegistry()
    calls = []

    def record(**params):
        calls.append(params)
        return ExecResult(True, "ok")

    return Router(registry, RuleEngine(registry), None, Executor(
        registry, handlers={"set_system_time": record})), calls


@pytest.mark.parametrize("text", [
    "把系统时间设置为2026-10-21 14:30:00",
    "请帮我将电脑时间调到2026-10-21 14:30",
    "set the system time to 2026-10-21T14:30:00",
    "设置系统时间为2026-10-21 14:30",
    "把系统时间设置为2026年10月21日14点30分",
])
def test_explicit_time_executes(time_router, text):
    router, calls = time_router
    result = router.handle(text)
    assert result.stage is Stage.RULE
    assert result.params == {"datetime": "2026-10-21 14:30:00"}
    assert len(calls) == 1


@pytest.mark.parametrize("text", [
    "把系统时间调到下午三点", "设置系统时间", "把系统时间调快十分钟",
    "把系统时间设置为2026-02-30 14:30", "set system time to tomorrow",
    "把系统时间设置为2026-10-21 25:30", "修改一下时间",
    "把系统时间设置为2026-10-21 14:30并关机",
    "不要把系统时间设置为2026-10-21 14:30",
    "怎么把系统时间设置为2026-10-21 14:30？",
    "把系统时间设置为2026-10-21 14:30 UTC",
    "不要把时间设置为15点30分", "怎么把时间设置为15点30分？",
    "把时间设置为15点30分并关机", "把时间设置为25点30分",
    "把时间设置为下午三点", "把闹钟时间设置为15点30分",
])
def test_ambiguous_time_goes_to_cloud_without_execution(time_router, text):
    router, calls = time_router
    result = router.handle(text)
    assert result.stage is Stage.CLOUD
    assert result.escalate_reason == "ambiguous_system_time"
    assert calls == []


@pytest.mark.parametrize("target", ["tomorrow", "2026-02-30 12:00", "2026-10-21 14:30; reboot", "25:30", "2026-02-30", "10-23", "14:30:60"])
def test_handler_rejects_invalid_time(monkeypatch, target):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid time must not execute a command")

    monkeypatch.setattr(system_tools, "_run", forbidden)
    assert not system_tools.set_system_time(target).ok


@pytest.mark.parametrize("ok", [True, False])
def test_handler_runs_only_set_time(monkeypatch, ok):
    calls = []

    def record(cmd):
        calls.append(cmd)
        return ExecResult(ok, "ok" if ok else "failed", "NTP enabled" if not ok else "")

    monkeypatch.setattr(system_tools, "_run", record)
    result = system_tools.set_system_time("2026-10-21T14:30", language="en")
    assert result.ok is ok
    assert calls == [["timedatectl", "--no-ask-password", "set-time", "2026-10-21 14:30:00"]]
    if not ok:
        assert result.detail == "NTP enabled"


def test_schema_exposes_time_tool():
    registry = ToolRegistry()
    assert registry.get("set_system_time").params["datetime"].is_required
    assert "set_system_time" in system_tools.HANDLERS


@pytest.mark.parametrize("text,target", [
    ("把系统日期设置为2026年10月23日", "2026-10-23"),
    ("设置系统日期为2028-02-29", "2028-02-29"),
    ("把系统时间设置为14:30:25", "14:30:25"),
    ("设置系统时间为14:30", "14:30:00"),
    ("把时间设置为15点30分", "15:30:00"),
    ("设置时间为15:30", "15:30:00"),
    ("请帮我将时间调到15时30分", "15:30:00"),
    ("把系统时间设为0点5分", "00:05:00"),
    ("设置电脑时间为23时59分59秒", "23:59:59"),
    ("set system date to 2026-10-23", "2026-10-23"),
    ("set system time to 14:30", "14:30:00"),
])
def test_partial_request_keeps_missing_parts_unresolved(time_router, text, target):
    router, calls = time_router
    result = router.handle(text)
    assert result.stage is Stage.RULE and result.params == {"datetime": target}
    assert len(calls) == 1


@pytest.mark.parametrize("text", [
    "把系统日期设置为23号", "设置系统日期为2026-02-29", "设置系统日期为14:30",
    "把系统时间设置为14:30然后关机", "不要设置系统日期为2026-10-23",
    "设置系统时间为下午三点", "设置系统时间为14:30 UTC", "设置系统时间为24:00",
])
def test_ambiguous_partial_requests_never_execute(time_router, text):
    router, calls = time_router
    assert router.handle(text).stage is Stage.CLOUD
    assert calls == []


@pytest.mark.parametrize("value,target", [
    ("2028-02-29", "2028-02-29 23:59:58"),
    ("14:30", "2026-12-31 14:30:00"),
    ("00:00:01", "2026-12-31 00:00:01"),
])
def test_partial_setting_reads_execution_time_and_never_changes_ntp(monkeypatch, value, target):
    from datetime import datetime, timedelta, timezone

    class Clock:
        @staticmethod
        def now():
            return Clock()

        def astimezone(self):
            return datetime(2026, 12, 31, 23, 59, 58, tzinfo=timezone(timedelta(hours=8)))

    calls = []
    monkeypatch.setattr(system_tools, "system_datetime", Clock)

    def run(command):
        calls.append(command)
        return ExecResult(True, "ok")

    monkeypatch.setattr(system_tools, "_run", run)
    result = system_tools.set_system_time(value)
    assert result.ok and target in result.message
    assert calls == [["timedatectl", "--no-ask-password", "set-time", target]]


def test_schedule_is_not_system_time_request():
    from bibit_intent.core.system_time import is_system_time_request

    assert not is_system_time_request("修改会议时间到明天下午三点")
    assert parse_system_time_request("设置一个下午三点的提醒") is None


@pytest.mark.parametrize("text", [
    "现在几点", "现在几点了？", "今天几号", "今天是几月几日？",
    "查看系统时间", "请帮我查询一下当前日期", "查询时区", "当前系统时间",
    "what time is it?", "What is the current time?", "show system time",
    "please get the current date", "what's the system timezone?",
])
def test_time_query_routes_to_read_only_tool(text):
    registry = ToolRegistry()
    calls = []

    def read(**params):
        calls.append(params)
        return ExecResult(True, "time")

    def forbidden(**params):
        pytest.fail("A query must never change system time")

    router = Router(registry, RuleEngine(registry), None, Executor(
        registry, handlers={"get_system_time": read, "set_system_time": forbidden}))
    result = router.handle(text)
    assert result.stage is Stage.RULE
    assert result.tool_name == "get_system_time"
    assert result.params == {}
    assert len(calls) == 1


@pytest.mark.parametrize("text", [
    "伦敦现在几点", "what time is it in London?", "查看系统时间然后关机",
    "不要查看系统时间", "把系统时间设置为2026-10-21 14:30", "明天下午提醒我",
])
def test_query_match_does_not_swallow_other_requests(text):
    from bibit_intent.core.system_time import is_system_time_query

    assert not is_system_time_query(text)


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("hours", [-7, 8])
def test_read_time_includes_date_zone_and_offset(monkeypatch, language, hours):
    from datetime import datetime, timedelta, timezone

    zone = timezone(timedelta(hours=hours), name="TestZone")
    fixed = datetime(2026, 10, 21, 14, 30, 5, tzinfo=zone)

    class Clock:
        @staticmethod
        def now():
            return LocalClock()

    class LocalClock:
        @staticmethod
        def astimezone():
            return fixed

    monkeypatch.setattr(system_tools, "system_datetime", Clock)

    def forbidden(*args, **kwargs):
        pytest.fail("Reading the clock must not execute commands")

    monkeypatch.setattr(system_tools, "_run", forbidden)
    result = system_tools.get_system_time(language=language)
    assert result.ok
    assert "2026-10-21 14:30:05" in result.message
    assert "TestZone" in result.message
    assert ("UTC-07:00" if hours == -7 else "UTC+08:00") in result.message
    assert ("未验证" if language == "zh" else "not been verified") in result.detail


def test_query_tool_is_read_only_and_exposed():
    from bibit_intent.execution.executor import _may_change_device

    tool = ToolRegistry().get("get_system_time")
    assert tool.params == {}
    assert not _may_change_device(tool)
    assert "get_system_time" in system_tools.HANDLERS
