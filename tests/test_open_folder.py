"""tests/test_open_folder.py — open_folder 目录打开的路由与执行回归（全离线）。

覆盖第一期：
  - 「打开桌面/文档/下载/图片/音乐/视频/主目录/根目录」等知名位置 → open_folder
  - 英文别名与显式绝对路径 / ~/… 路径 → open_folder
  - 模糊目录名与「打开文件」仍升级云端，不误开目录
  - resolve_directory 的 XDG 别名解析
  - open_folder handler 的路径校验与 GUI argv 构造（mock，不真弹窗）
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.routing.intent import IntentAction, decide_local_intent
from bibit_intent.routing.rule_engine import RuleEngine
from bibit_intent.execution import system_tools as st
from bibit_intent.core.desktop_locations import resolve_desktop_shortcut, resolve_directory

_reg = ToolRegistry()
_rule = RuleEngine(_reg)


def _plan(text):
    return decide_local_intent(text, _rule, _reg)


def test_open_folder_known_locations_route_to_open_folder():
    for text in (
        "打开桌面", "打开文档", "打开下载", "打开主目录", "打开根目录",
        "帮我打开下载", "打开下载目录", "打开桌面文件夹", "打开文档目录",
    ):
        plan = _plan(text)
        assert plan.action is IntentAction.RULE_CALL, text
        assert plan.tool_name == "open_folder", text


def test_open_folder_english_alias_and_explicit_path():
    for text in (
        "打开 Downloads", "open Downloads", "打开 desktop", "open downloads",
        "打开 ~/Downloads", "打开 /home/bianbu/workspace/bibit-intent",
    ):
        plan = _plan(text)
        assert plan.action is IntentAction.RULE_CALL, text
        assert plan.tool_name == "open_folder", text


def test_open_folder_file_open_still_escalates():
    # 「打开桌面的XX报告」属于文件打开，不属于目录打开，仍升级云端
    plan = _plan("打开桌面的年度总结报告")
    assert plan.action is IntentAction.ESCALATE, plan.action
    assert plan.reason == "unsupported_control", plan.reason


def test_open_folder_does_not_hijack_apps_or_trash():
    # 「打开回收站」仍走 launch_app 特殊位置，「打开文件管理器」仍是应用
    plan = _plan("打开回收站")
    assert plan.action is IntentAction.RULE_CALL
    assert plan.tool_name == "launch_app"
    plan = _plan("打开文件管理器")
    assert plan.action is IntentAction.RULE_CALL
    assert plan.tool_name == "launch_app"


def test_resolve_directory_home_and_root():
    assert resolve_directory("主目录") == str(Path.home())
    assert resolve_directory("主文件夹") == str(Path.home())
    assert resolve_directory("home") == str(Path.home())
    assert resolve_directory("根目录") == "/"
    assert resolve_directory("根文件系统") == "/"
    assert resolve_directory("root") == "/"
    assert resolve_directory("不认识的位置") is None


def test_resolve_directory_xdg_alias(monkeypatch):
    monkeypatch.setattr(
        "bibit_intent.core.desktop_locations._xdg_user_dir",
        lambda key: f"/home/tester/{key.lower()}",
    )
    assert resolve_directory("桌面") == "/home/tester/desktop"
    assert resolve_directory("文档") == "/home/tester/documents"
    assert resolve_directory("下载") == "/home/tester/download"


def test_open_folder_rejects_missing_relative_and_file_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "_dir_locate_roots", lambda: [tmp_path])
    # 空 → 缺参；相对/不存在/文件 → 本地无法确认，升级云端
    r = st.open_folder("")
    assert not r.ok and r.need_param == "path"
    r = st.open_folder("relative/dir")
    assert not r.ok and r.escalate == "dir_not_found"
    # 不存在的目录
    r = st.open_folder(str(tmp_path / "nope"))
    assert not r.ok and "不存在" in r.message
    assert r.escalate == "dir_not_found"
    # 路径指向普通文件
    f = tmp_path / "file.txt"
    f.write_text("x")
    r = st.open_folder(str(f))
    assert not r.ok and "不是目录" in r.message
    assert r.escalate == "dir_not_found"


def test_open_folder_builds_gio_argv(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(st.shutil, "which", lambda name: "/usr/bin/gio" if name == "gio" else None)
    monkeypatch.setattr(st, "_launch_desktop_argv", lambda argv, label, lang, zh, en: captured.update(argv=argv, label=label) or st.ExecResult(True, zh))

    r = st.open_folder(str(tmp_path))
    assert r.ok
    assert captured["argv"] == ["/usr/bin/gio", "open", str(tmp_path)]
    assert captured["label"].startswith("目录 ")


def test_open_folder_opens_xdg_alias_path(monkeypatch, tmp_path):
    # 别名经 resolve_directory 展开为真实目录后再打开
    captured = {}
    monkeypatch.setattr(
        "bibit_intent.core.desktop_locations._xdg_user_dir",
        lambda key: str(tmp_path / key.lower()),
    )
    (tmp_path / "desktop").mkdir()
    monkeypatch.setattr(st.shutil, "which", lambda name: "/usr/bin/gio" if name == "gio" else None)
    monkeypatch.setattr(st, "_launch_desktop_argv", lambda argv, label, lang, zh, en: captured.update(argv=argv) or st.ExecResult(True, zh))

    r = st.open_folder("桌面")
    assert r.ok
    assert captured["argv"][2] == str(tmp_path / "desktop")


# ── 第二期：模糊名称目录定位 ──────────────────────────────────────────

def test_open_folder_fuzzy_name_routes_to_open_folder():
    for text in ("打开 bianbu 文件夹", "打开源码目录", "打开 workspace 目录"):
        plan = _plan(text)
        assert plan.action is IntentAction.RULE_CALL, text
        assert plan.tool_name == "open_folder", text
        assert plan.slots.get("path"), text


def test_open_folder_fuzzy_name_does_not_hijack_file_open():
    # 「打开 XX 文件/打开桌面的XX报告」不属于目录打开，仍交给云端/模型
    plan = _plan("打开桌面的年度总结报告")
    assert plan.action is IntentAction.ESCALATE
    assert plan.reason == "unsupported_control"


def test_locate_directories_scoped_and_ranked(monkeypatch, tmp_path):
    # 把定位根目录收到 tmp_path，验证名称子串匹配 + 短路径优先
    monkeypatch.setattr(st, "_dir_locate_roots", lambda: [tmp_path])
    (tmp_path / "workspace").mkdir()
    (tmp_path / "deep").mkdir()
    (tmp_path / "deep" / "my-workspace").mkdir()
    (tmp_path / "unrelated").mkdir()

    matches = st.locate_directories("workspace")
    assert matches == [str(tmp_path / "workspace"), str(tmp_path / "deep" / "my-workspace")]


def test_locate_directories_empty_for_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "_dir_locate_roots", lambda: [tmp_path])
    (tmp_path / "a").mkdir()
    assert st.locate_directories("does-not-exist") == []


def test_open_folder_multiple_matches_returns_choices(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "locate_directories", lambda name, limit=10, roots=None: [str(tmp_path / "a"), str(tmp_path / "b")])
    r = st.open_folder("some-name")
    assert not r.ok
    assert r.need_param == "path"
    assert r.choices == [str(tmp_path / "a"), str(tmp_path / "b")]


def test_open_folder_single_match_opens(monkeypatch, tmp_path):
    target = tmp_path / "only"
    target.mkdir()
    captured = {}
    monkeypatch.setattr(st, "locate_directories", lambda name, limit=10, roots=None: [str(target)])
    monkeypatch.setattr(st.shutil, "which", lambda name: "/usr/bin/gio" if name == "gio" else None)
    monkeypatch.setattr(st, "_launch_desktop_argv", lambda argv, label, lang, zh, en: captured.update(argv=argv) or st.ExecResult(True, zh))

    r = st.open_folder("only")
    assert r.ok
    assert captured["argv"][2] == str(target)


def test_open_folder_no_match_reports_missing(monkeypatch):
    # 模糊定位零命中：本地不臆断，升级云端确认
    monkeypatch.setattr(st, "locate_directories", lambda name, limit=10, roots=None: [])
    r = st.open_folder("nowhere")
    assert not r.ok
    assert "没有找到" in r.message
    assert r.escalate == "dir_not_found"


def test_open_folder_no_match_escalates_to_cloud(monkeypatch):
    """端到端：模糊定位零命中时整条路由升级云端，而不是本地报「没有找到」。"""
    from bibit_intent.execution.executor import Executor
    from bibit_intent.routing.router import Router, Stage

    registry = ToolRegistry()
    rule = RuleEngine(registry)
    executor = Executor(registry)
    router = Router(registry, rule, None, executor)

    monkeypatch.setattr(st, "locate_directories", lambda name, limit=10, roots=None: [])

    result = router.handle("打开 nowhere 文件夹")
    assert result.stage is Stage.CLOUD
    assert result.escalate_reason == "dir_not_found"


def test_open_folder_sandbox_escalates_to_host(monkeypatch):
    # 沙箱内宿主文件系统不可见：不要做目录定位/存在性检查（会误报「没有找到」），
    # 直接升级到 host 通道，与 _launch_desktop_argv 的沙箱桥接语义一致。
    monkeypatch.setattr(st, "is_sandboxed", lambda: True)
    r = st.open_folder("桌面")
    assert not r.ok
    assert r.escalate == "needs_host_channel"


# ── 第三期：位置限定词 + 桌面占位快捷方式 ─────────────────────────────

def test_open_folder_location_routes_with_location_slot():
    # 「打开桌面的 bianbu 文件夹」把位置和名称拆开，而不是粘成「桌面的bianbu」
    for text in ("打开桌面的bianbu文件夹", "打开下载的bianbu文件夹", "打开文档里的源码目录"):
        plan = _plan(text)
        assert plan.action is IntentAction.RULE_CALL, text
        assert plan.tool_name == "open_folder", text
        assert plan.slots.get("location"), text
        assert plan.slots.get("path"), text


def test_resolve_desktop_shortcut_hardcoded():
    # user-home/computer/trash-can 占位快捷方式；用户名动态取当前主目录名，
    # chromium 等应用不在此列
    assert resolve_desktop_shortcut(Path.home().name) == str(Path.home())
    assert resolve_desktop_shortcut("计算机") == "computer:///"
    assert resolve_desktop_shortcut("回收站") == "trash:///"
    assert resolve_desktop_shortcut("chromium") is None
    other = "bianbu" if Path.home().name != "bianbu" else "another-user"
    assert resolve_desktop_shortcut(other) is None


def test_open_folder_location_shortcut_opens_home(monkeypatch):
    captured = {}
    monkeypatch.setattr(st.shutil, "which", lambda name: "/usr/bin/gio" if name == "gio" else None)
    monkeypatch.setattr(st, "_launch_desktop_argv",
                        lambda argv, label, lang, zh, en: captured.update(argv=argv) or st.ExecResult(True, zh))
    r = st.open_folder(Path.home().name, location="桌面")
    assert r.ok
    assert captured["argv"] == ["/usr/bin/gio", "open", str(Path.home())]


def test_open_folder_location_shortcut_opens_uri(monkeypatch):
    captured = {}
    monkeypatch.setattr(st.shutil, "which", lambda name: "/usr/bin/gio" if name == "gio" else None)
    monkeypatch.setattr(st, "_launch_desktop_argv",
                        lambda argv, label, lang, zh, en: captured.update(argv=argv) or st.ExecResult(True, zh))
    r = st.open_folder("计算机", location="桌面")
    assert r.ok
    assert captured["argv"] == ["/usr/bin/gio", "open", "computer:///"]


def test_open_folder_location_scoped_locate(tmp_path):
    # 位置限定词把定位收到该目录内，而不是全 home 扫
    base = tmp_path / "desktop"
    base.mkdir()
    (base / "my-src").mkdir()
    (tmp_path / "other-src").mkdir()
    matches = st.locate_directories("src", roots=[base])
    assert matches == [str(base / "my-src")]


def test_session_resolves_choice_by_index():
    from bibit_intent.core.session import SessionState
    s = SessionState()
    s.set_pending("open_folder", "path", "目录", {}, "请选择",
                  param_type="str", choices=["/a", "/b", "/c"])
    assert s.resolve("1") == ("fill", "/a")
    assert s.resolve("3") == ("fill", "/c")
    assert s.resolve("2") == ("fill", "/b")


def test_open_folder_router_suspends_then_fills_by_index(monkeypatch, tmp_path):
    """端到端：模糊名称 → 多候选挂起 → 用户回序号 → 打开对应目录。"""
    from bibit_intent.execution.executor import Executor
    from bibit_intent.routing.router import Router
    from bibit_intent.core.session import SessionState

    registry = ToolRegistry()
    rule = RuleEngine(registry)
    executor = Executor(registry)
    router = Router(registry, rule, None, executor)

    a = tmp_path / "aaa-workspace"
    b = tmp_path / "bbb-workspace"
    a.mkdir()
    b.mkdir()
    monkeypatch.setattr(st, "locate_directories", lambda name, limit=10, roots=None: [str(a), str(b)])
    monkeypatch.setattr(st.shutil, "which", lambda name: "/usr/bin/gio" if name == "gio" else None)
    opened = {}
    monkeypatch.setattr(st, "_launch_desktop_argv",
                        lambda argv, label, lang, zh, en: opened.update(argv=argv) or st.ExecResult(True, zh))

    session = SessionState()
    router.handle("打开 workspace 文件夹", session)
    assert session.pending is not None
    assert session.pending.tool_name == "open_folder"
    assert session.pending.choices == [str(a), str(b)]

    router.handle("2", session)
    assert opened["argv"][2] == str(b)
    assert session.pending is None


# ── 第四期：FileManager1 优先，绕过默认 handler 错配 ──────────────────

def test_file_manager1_show_folders_builds_busctl_argv(monkeypatch, tmp_path):
    """FileManager1 通过 busctl 调 ShowFolders，路径转 file:// URI。"""
    monkeypatch.setattr(st.shutil, "which", lambda name: "/usr/bin/busctl" if name == "busctl" else None)
    calls = {}
    monkeypatch.setattr(st.subprocess, "run",
                        lambda cmd, **kw: calls.update(cmd=cmd) or type("P", (), {"returncode": 0})())
    monkeypatch.setattr(st, "_find_desktop_session_env", lambda: None)

    target = tmp_path / "my dir"
    target.mkdir()
    assert st._file_manager1_show_folders(str(target), None) is True
    assert calls["cmd"][0] == "/usr/bin/busctl"
    assert "org.freedesktop.FileManager1" in calls["cmd"]
    assert calls["cmd"][-5] == "ShowFolders"
    assert calls["cmd"][-4] == "ass"
    assert calls["cmd"][-3] == "1"
    assert calls["cmd"][-2] == target.as_uri()
    assert calls["cmd"][-1] == ""


def test_file_manager1_show_folders_keeps_gvfs_uri(monkeypatch):
    monkeypatch.setattr(st.shutil, "which", lambda name: "/usr/bin/busctl" if name == "busctl" else None)
    calls = {}
    monkeypatch.setattr(st.subprocess, "run",
                        lambda cmd, **kw: calls.update(cmd=cmd) or type("P", (), {"returncode": 0})())
    monkeypatch.setattr(st, "_find_desktop_session_env", lambda: None)
    assert st._file_manager1_show_folders("computer:///", None) is True
    assert "computer:///" in calls["cmd"]


def test_open_folder_prefers_file_manager1(monkeypatch, tmp_path):
    """FileManager1 成功时直接返回，不再走 gio open（避免 codium 抢 handler）。"""
    target = tmp_path / "d"
    target.mkdir()
    monkeypatch.setattr(st, "_file_manager1_show_folders", lambda t, env: True)
    launched = {}
    monkeypatch.setattr(st, "_launch_desktop_argv",
                        lambda argv, label, lang, zh, en: launched.update(argv=argv) or st.ExecResult(True, zh))

    r = st.open_folder(str(target))
    assert r.ok
    assert "已打开文件夹" in r.message
    assert launched == {}


def test_open_folder_falls_back_to_gio_when_no_file_manager1(monkeypatch, tmp_path):
    target = tmp_path / "d"
    target.mkdir()
    monkeypatch.setattr(st, "_file_manager1_show_folders", lambda t, env: False)
    monkeypatch.setattr(st.shutil, "which", lambda name: "/usr/bin/gio" if name == "gio" else None)
    captured = {}
    monkeypatch.setattr(st, "_launch_desktop_argv",
                        lambda argv, label, lang, zh, en: captured.update(argv=argv) or st.ExecResult(True, zh))

    r = st.open_folder(str(target))
    assert r.ok
    assert captured["argv"] == ["/usr/bin/gio", "open", str(target)]


@pytest.mark.parametrize("text,name", [
    ("打开桌面的计算机", "计算机"),
    ("打开桌面上的计算机", "计算机"),
    ("打开桌面的回收站", "回收站"),
    ("打开桌面上的回收站", "回收站"),
])
def test_open_folder_desktop_shortcut_without_folder_suffix(text, name):
    # 「打开桌面(上)的计算机/回收站」对应桌面 computer/trash-can 占位快捷方式，
    # 即使名称后没有「文件夹/目录」也应路由到 open_folder 本地打开。
    plan = _plan(text)
    assert plan.action is IntentAction.RULE_CALL, text
    assert plan.tool_name == "open_folder", text
    assert plan.slots == {"path": name, "location": "桌面"}, text


def test_open_folder_desktop_shortcut_non_shortcut_still_escalates():
    # 无「文件夹/目录」后缀且名称不是快捷方式 → 仍升级云端，不被误开目录。
    for text in ("打开桌面的年度总结报告", "打开桌面的说明书"):
        plan = _plan(text)
        assert plan.action is IntentAction.ESCALATE, text


# ── 第五期：两层嵌套目录定位（A 文件夹里面的 B 文件夹） ──────────────

def test_open_folder_nested_routes_with_base_and_name():
    for text in (
        "打开bianbu文件夹里面的Documents文件夹",
        "打开bianbu文件夹里面的Documents",
        "打开home里的Documents文件夹",
    ):
        plan = _plan(text)
        assert plan.action is IntentAction.RULE_CALL, text
        assert plan.tool_name == "open_folder", text
        assert plan.slots.get("path"), text
        assert plan.slots.get("location"), text


def test_open_folder_nested_base_resolves_home_shortcut(monkeypatch):
    # location 是当前主目录名（如 bianbu）时解析成主目录，再在其中定位目标
    base = Path.home()
    monkeypatch.setattr(st, "_resolve_scoped_base", lambda loc: base)
    monkeypatch.setattr(st, "locate_directories",
                        lambda name, limit=10, roots=None: [str(base / name)])
    monkeypatch.setattr(st, "resolve_directory", lambda name: None)
    opened = {}
    monkeypatch.setattr(st, "_open_dir_path",
                        lambda dir_path, language: opened.update(path=str(dir_path)) or st.ExecResult(True, "ok"))

    r = st.open_folder("src", location="whatever")
    assert r.ok
    assert opened["path"] == str(base / "src")


def test_locate_directories_preserves_non_utf8_filenames(tmp_path):
    target = tmp_path / "report-\udc82"
    target.mkdir()
    assert st.locate_directories("report", roots=[tmp_path]) == [str(target)]
