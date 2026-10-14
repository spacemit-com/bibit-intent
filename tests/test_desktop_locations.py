import pytest

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution import system_tools
from bibit_intent.routing.intent import IntentAction, decide_local_intent
from bibit_intent.routing.rule_engine import RuleEngine


@pytest.mark.parametrize("text,name,uri", [
    ("打开trash", "trash", "trash:///"),
    ("打开垃圾箱", "垃圾箱", "trash:///"),
    ("帮我打开回收站", "回收站", "trash:///"),
    ("open Trash", "Trash", "trash:///"),
    ("打开Computer", "Computer", "computer:///"),
    ("打开我的电脑", "我的电脑", "computer:///"),
    ("please open my computer", "my computer", "computer:///"),
])
def test_location_routes_and_launches_locally(monkeypatch, text, name, uri):
    registry = ToolRegistry()
    plan = decide_local_intent(text, RuleEngine(registry), registry)
    assert plan.action is IntentAction.RULE_CALL
    assert plan.tool_name == "launch_app"
    assert plan.slots == {"app_name": name}
    monkeypatch.setattr(system_tools, "_find_desktop_session_env", lambda: {"DISPLAY": ":0"})
    monkeypatch.setattr(system_tools.shutil, "which", lambda n: "/usr/bin/gio" if n == "gio" else None)
    calls = []

    def fake_launch(argv, label, language, zh, en):
        calls.append(argv)
        return system_tools.ExecResult(True, zh)

    monkeypatch.setattr(system_tools, "_launch_desktop_argv", fake_launch)
    assert system_tools.launch_app(**plan.slots).ok
    assert calls == [["/usr/bin/gio", "open", uri]]


@pytest.mark.parametrize("text", [
    "打开我的电脑里的报告", "打开下载文件夹", "不要打开垃圾箱", "清空垃圾箱",
    "打开垃圾箱并删除所有文件",
])
def test_other_requests_do_not_use_location_shortcut(text):
    registry = ToolRegistry()
    plan = decide_local_intent(text, RuleEngine(registry), registry)
    assert plan.matched_pattern != "desktop_location"


def test_location_launch_failure_does_not_fall_back(monkeypatch):
    # gio 不存在时回退 xdg-open；两者都缺失时明确失败，不得再落到 .desktop 应用解析。
    monkeypatch.setattr(system_tools, "_find_desktop_session_env", lambda: None)
    monkeypatch.setattr(system_tools.shutil, "which", lambda n: None)
    calls = []

    def fake_launch(argv, label, language, zh, en):
        calls.append(argv)
        return system_tools.ExecResult(False, "命令不存在")

    monkeypatch.setattr(system_tools, "_launch_desktop_argv", fake_launch)
    assert not system_tools.launch_app("trash").ok
    assert calls == []


def test_location_launch_falls_back_to_xdg_open(monkeypatch):
    # gio 缺失时经 xdg-open 分发到默认文件管理器，不硬编码 pcmanfm-qt。
    monkeypatch.setattr(system_tools, "_find_desktop_session_env", lambda: {"DISPLAY": ":0"})
    monkeypatch.setattr(
        system_tools.shutil, "which",
        lambda n: "/usr/bin/xdg-open" if n == "xdg-open" else None,
    )
    calls = []

    def fake_launch(argv, label, language, zh, en):
        calls.append(argv)
        return system_tools.ExecResult(True, zh)

    monkeypatch.setattr(system_tools, "_launch_desktop_argv", fake_launch)
    assert system_tools.launch_app("trash").ok
    assert calls == [["/usr/bin/xdg-open", "trash:///"]]


def test_location_launch_prefers_file_manager1(monkeypatch):
    # gvfs 虚拟位置（trash:/// computer:///）优先走 FileManager1，避免 gio open
    # 按默认 handler 分发（端侧 inode/directory 被 codium 抢走）。
    monkeypatch.setattr(system_tools, "_find_desktop_session_env", lambda: {"DISPLAY": ":0"})
    monkeypatch.setattr(system_tools, "_file_manager1_show_folders", lambda uri, env: True)
    launched = []
    monkeypatch.setattr(system_tools, "_launch_desktop_argv",
                        lambda argv, label, lang, zh, en: launched.append(argv) or system_tools.ExecResult(True, zh))

    r = system_tools.launch_app("回收站")
    assert r.ok
    assert "已打开" in r.message
    assert launched == []


def test_location_launch_falls_back_to_gio_when_no_file_manager1(monkeypatch):
    monkeypatch.setattr(system_tools, "_find_desktop_session_env", lambda: {"DISPLAY": ":0"})
    monkeypatch.setattr(system_tools, "_file_manager1_show_folders", lambda uri, env: False)
    monkeypatch.setattr(system_tools.shutil, "which", lambda n: "/usr/bin/gio" if n == "gio" else None)
    launched = []
    monkeypatch.setattr(system_tools, "_launch_desktop_argv",
                        lambda argv, label, lang, zh, en: launched.append(argv) or system_tools.ExecResult(True, zh))

    r = system_tools.launch_app("计算机")
    assert r.ok
    assert launched == [["/usr/bin/gio", "open", "computer:///"]]
