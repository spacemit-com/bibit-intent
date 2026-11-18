import json

import pytest

from bibit_intent.core.locale_request import is_locale_request, parse_locale_request
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution import locale_tools, system_tools
from bibit_intent.execution.executor import Executor, _may_change_device
from bibit_intent.execution.system_tools import ExecResult
from bibit_intent.routing.router import Router, Stage
from bibit_intent.routing.rule_engine import RuleEngine


@pytest.fixture
def backend(monkeypatch):
    calls = []

    def run(command):
        calls.append(command)
        if command == ["locale", "-a"]:
            return ExecResult(True, "ok", "C\nC.utf8\nen_US.utf8\nzh_CN.utf8\nen_GB.utf8\nfr_FR.utf8\n")
        if command[0] == "busctl":
            return ExecResult(True, "ok", 'as 3 "LANG=en_US.UTF-8" "LC_NUMERIC=en_GB.UTF-8" "LC_TIME=en_GB.UTF-8"')
        return ExecResult(True, "ok")

    monkeypatch.setattr(locale_tools, "_run", run)
    return calls


def test_read_is_global_even_when_personal_config_exists(backend, monkeypatch, tmp_path):
    (tmp_path / "lxqt").mkdir()
    (tmp_path / "lxqt/lxqt-config-locale.conf").write_text("[Formats]\nLANG=zh_CN.UTF-8\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    detail = json.loads(locale_tools.get_locale_settings().detail)
    assert detail["scope"] == "system" and detail["channel"] == "host"
    assert detail["language"] == "en_US.UTF-8" and detail["region"] == "en_GB.UTF-8"


def test_list_options(backend):
    detail = json.loads(locale_tools.list_locale_options().detail)
    assert set(detail["languages"]) == {"zh_CN", "en_US"}
    assert detail["regions"] == ["en_GB", "en_US", "fr_FR", "zh_CN"]
    assert detail["region_names"]["en_GB"] == "英国"


@pytest.mark.parametrize("locale", ["zh_CN.UTF8", "zh_CN.UTF-8", "zh_CN.utf8", "zh_CN"])
def test_friendly_chinese_labels_preserve_codes(monkeypatch, locale):
    monkeypatch.setattr(locale_tools, "_system_values", lambda: {"LANG": locale})
    result = locale_tools.get_locale_settings(language="zh")
    assert result.message == "系统语言：简体中文；区域：中国大陆。"
    detail = json.loads(result.detail)
    assert detail["language"] == detail["region"] == locale
    assert detail["language_name"] == "简体中文" and detail["region_name"] == "中国大陆"


def test_language_and_region_independent_labels(backend):
    result = locale_tools.get_locale_settings(language="en")
    assert result.message == "System language: English; region: United Kingdom."
    assert locale_tools.get_locale_settings(language="zh").message == "系统语言：英文；区域：英国。"


@pytest.mark.parametrize("value,kind,expected", [
    ("", "region", "未配置"), ("C.UTF-8", "region", "标准默认格式"),
    ("xx_XX.UTF-8", "language", "xx_XX.UTF-8"), ("xx_XX.UTF-8", "region", "xx_XX.UTF-8"),
    ("zh_HK.utf8", "region", "中国香港"), ("en_SG.utf8", "region", "新加坡"),
])
def test_label_fallback(value, kind, expected):
    assert locale_tools._locale_label(value, kind, "zh") == expected


@pytest.mark.parametrize("function,target", [
    (locale_tools.set_system_language, "fr_FR"), (locale_tools.set_system_language, "zh_TW"),
    (locale_tools.set_system_region, "de_DE"), (locale_tools.set_system_region, "en_US; reboot"),
])
def test_invalid_target_never_writes(backend, function, target):
    assert not function(target).ok
    assert backend == [["locale", "-a"]]


@pytest.mark.parametrize("target", ["简体中文", "zh_CN", "zh_CN.UTF-8"])
def test_default_language_write_is_global_and_preserves_region(backend, target):
    assert locale_tools.set_system_language(target).ok
    command = backend[-1]
    assert command[:3] == ["localectl", "--no-ask-password", "set-locale"]
    values = dict(value.split("=", 1) for value in command[3:])
    assert values["LANG"] == values["LC_MESSAGES"] == values["LC_CTYPE"] == "zh_CN.utf8"
    assert values["LC_TIME"] == values["LC_MONETARY"] == "en_GB.UTF-8"


def test_default_region_write_is_global_and_preserves_language(backend):
    assert locale_tools.set_system_region("中国").ok
    values = dict(value.split("=", 1) for value in backend[-1][3:])
    assert values["LC_NUMERIC"] == values["LC_TIME"] == "zh_CN.utf8"
    assert values["LANG"] == values["LC_MESSAGES"] == "en_US.UTF-8"


def test_no_personal_fallback_on_permission_denied(backend, monkeypatch):
    original = locale_tools._run
    monkeypatch.setattr(locale_tools, "_run", lambda command: ExecResult(False, "denied", "Authorization denied")
                        if command[0] == "localectl" else original(command))
    result = locale_tools.set_system_language("英文")
    assert not result.ok and result.detail == "Authorization denied"


@pytest.mark.parametrize("function,params", [
    (locale_tools.get_locale_settings, {}),
    (locale_tools.set_system_language, {"locale": "英文"}),
    (locale_tools.set_system_region, {"locale": "中国"}),
])
def test_user_scope_is_rejected_before_commands(backend, function, params):
    assert not function(scope="user", **params).ok
    assert backend == []


@pytest.mark.parametrize("function,params", [
    (system_tools.get_system_time, {}), (system_tools.set_system_time, {"datetime": "2026-10-21 14:30"}),
    (locale_tools.get_locale_settings, {}), (locale_tools.list_locale_options, {}),
    (locale_tools.set_system_language, {"locale": "英文"}),
    (locale_tools.set_system_region, {"locale": "中国"}),
])
@pytest.mark.parametrize("variable,value", [("BIANBU_SANDBOX_ACTIVE", "1"), ("BIANBU_WORKER_EFFECTIVE_MODE", "sandbox")])
def test_sandbox_never_executes_global_settings(monkeypatch, function, params, variable, value):
    monkeypatch.setenv(variable, value)

    def forbidden(*args, **kwargs):
        pytest.fail("Sandbox must not read or modify global settings locally")

    monkeypatch.setattr(locale_tools, "_run", forbidden)
    monkeypatch.setattr(system_tools, "_run", forbidden)
    result = function(**params)
    assert not result.ok and result.escalate == "needs_host_channel"


@pytest.mark.parametrize("text,tool,params", [
    ("查看系统语言", "get_locale_settings", {"scope": "system"}),
    ("当前系统语言是什么", "get_locale_settings", {"scope": "system"}),
    ("查询系统默认区域", "get_locale_settings", {"scope": "system"}),
    ("查看可用区域", "list_locale_options", {}),
    ("把系统语言切换为英文", "set_system_language", {"locale": "英文", "scope": "system"}),
    ("设置系统默认语言为简体中文", "set_system_language", {"locale": "简体中文", "scope": "system"}),
    ("将区域改成美国", "set_system_region", {"locale": "美国", "scope": "system"}),
    ("set system region to en_GB", "set_system_region", {"locale": "en_GB", "scope": "system"}),
])
def test_requests_route_to_global_tools(text, tool, params):
    calls = []

    def record(**kwargs):
        calls.append(kwargs)
        return ExecResult(True, "ok")

    registry = ToolRegistry()
    router = Router(registry, RuleEngine(registry), None, Executor(registry, handlers={tool: record}))
    result = router.handle(text)
    assert result.stage is Stage.RULE and result.tool_name == tool and result.params == params
    assert len(calls) == 1


@pytest.mark.parametrize("text", ["不要修改系统语言", "系统语言改一下", "把系统语言切换为英文然后关机",
                                 "系统区域设置成合适的", "怎么修改系统语言", "设置个人语言为英文"])
def test_ambiguous_or_personal_request_is_not_global_execution(text):
    registry = ToolRegistry()
    router = Router(registry, RuleEngine(registry), None, Executor(registry, handlers={}))
    result = router.handle(text)
    assert result.stage is Stage.CLOUD and result.escalate_reason == "ambiguous_locale_request"


@pytest.mark.parametrize("text", ["用英文回答我", "翻译成中文", "介绍中国区域经济", "截图选定区域"])
def test_unrelated_mentions_are_not_commands(text):
    assert parse_locale_request(text) is None and not is_locale_request(text)


def test_schema_and_handler_registration():
    registry = ToolRegistry()
    for name in (*locale_tools.HANDLERS, "get_system_time", "set_system_time"):
        tool = registry.get(name)
        assert tool.system["channel"] == "host" and name in system_tools.HANDLERS
        assert _may_change_device(tool) is name.startswith("set_")
        if "scope" in tool.params:
            assert tool.params["scope"].default == "system" and tool.params["scope"].enum == ["system"]
