"""
tests/test_launch_app.py — launch_app 的 .desktop 解析回归（全离线）

背景：launch_app 原来只依赖硬编码中文别名，装了什么应用不认识什么应用。
     现在动态扫描 /usr/share/applications、~/.local/share/applications，
     按 Name / Name[zh_CN] / 文件名匹配，并用 Exec 字段启动。
运行： python3 tests/test_launch_app.py  或  pytest tests/test_launch_app.py
"""
import os
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bibit_intent.execution import system_tools as st


def _pkill_call(target: str) -> list[str]:
    """close_app 实际发给 pkill 的 argv（模式锚定到可执行名，见 _pkill_pattern）。"""
    return ["pkill", "-f", st._pkill_pattern(target)]


DESKTOP_SAMPLE = """\
[Desktop Entry]
Type=Application
Name=KCalc
Name[zh_CN]=KCalc 计算器
Exec=kcalc %U
Terminal=false
"""

DESKTOP_HIDDEN = """\
[Desktop Entry]
Type=Application
Name=Hidden App
Exec=hidden-app
NoDisplay=true
"""

DESKTOP_TERMINAL = """\
[Desktop Entry]
Type=Application
Name=Terminal App
Exec=term-app --foo
Terminal=true
"""

DESKTOP_COMPLEX = """\
[Desktop Entry]
Type=Application
Name=mpv Media Player
Name[zh_CN]=mpv 媒体播放器
Exec=mpv --player-operation-mode=pseudo-gui -- %U
"""


def _write(rel: str, content: str, base: Path) -> Path:
    path = base / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_parse_and_exec():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        calc = _write("kcalc.desktop", DESKTOP_SAMPLE, base)
        info = st._parse_desktop_file(str(calc))
        assert info is not None
        assert info["stem"] == "kcalc"
        assert info["names"] == ["KCalc", "KCalc 计算器"]
        assert st._exec_argv(info["exec"]) == ["kcalc"]

        hidden = _write("hidden.desktop", DESKTOP_HIDDEN, base)
        assert st._parse_desktop_file(str(hidden)) is None

        terminal = _write("term.desktop", DESKTOP_TERMINAL, base)
        assert st._parse_desktop_file(str(terminal))["terminal"] is True


def test_desktop_cache_refreshes_after_install(monkeypatch, tmp_path):
    # "安装 X → 打开 X"：长驻进程必须在不重启的前提下看到新装的 .desktop
    appdir = tmp_path / "applications"
    appdir.mkdir()
    monkeypatch.setattr(st, "_desktop_search_dirs", lambda: [str(appdir)])
    monkeypatch.setattr(st, "_DESKTOP_APPS_CACHE", None)
    monkeypatch.setattr(st, "_DESKTOP_APPS_SIGNATURE", None)

    assert st._get_desktop_apps() == []

    _write("wechat.desktop",
           "[Desktop Entry]\nName=WeChat\nName[zh_CN]=微信\nExec=wechat %U\n",
           appdir)
    # 强制目录 mtime 前进，避免粗粒度文件系统把两次扫判定成同一签名
    future = time.time() + 5
    os.utime(appdir, (future, future))

    names = [name for app in st._get_desktop_apps() for name in app["names"]]
    assert "微信" in names
    assert st._match_desktop_app("微信", st._get_desktop_apps())["stem"] == "wechat"


def test_terminal_app_opens_window(monkeypatch):
    desktop = {"stem": "htop", "names": ["Htop"], "exec": "htop --delay 10 %F", "terminal": True}
    monkeypatch.setattr(st, "_get_desktop_apps", lambda: [desktop])
    monkeypatch.setattr(st, "_app_aliases", lambda: {"进程监视器": "htop"})
    monkeypatch.setattr(st, "_find_desktop_session_env", lambda: {"WAYLAND_DISPLAY": "wayland-0"})
    monkeypatch.setattr(st.shutil, "which", lambda name, **kwargs: "/usr/bin/qterminal")
    calls = []
    monkeypatch.setattr(st, "_launch_argv", lambda argv, name, env: calls.append((argv, env)) or st.ExecResult(True, "ok"))
    for name in ("htop", "进程监视器"):
        assert st.launch_app(name).ok
    assert calls == [(["/usr/bin/qterminal", "-e", "htop", "--delay", "10"], {"WAYLAND_DISPLAY": "wayland-0"})] * 2


def test_vim_desktop_opens_in_terminal(monkeypatch, tmp_path):
    desktop_path = _write("vim.desktop", "[Desktop Entry]\nName=Vim\nExec=vim %F\nTerminal=true\n", tmp_path)
    desktop = st._parse_desktop_file(str(desktop_path))
    assert desktop is not None
    monkeypatch.setattr(st, "_get_desktop_apps", lambda: [desktop])
    monkeypatch.setattr(st, "_app_aliases", lambda: {})
    monkeypatch.setattr(st, "_find_desktop_session_env", lambda: {"DISPLAY": ":0"})
    monkeypatch.setattr(st.shutil, "which", lambda *args, **kwargs: "/usr/bin/qterminal")
    calls = []

    def fake_launch(argv, app_name, session_env):
        calls.append(argv)
        return st.ExecResult(True, "ok")

    monkeypatch.setattr(st, "_launch_argv", fake_launch)
    assert st.launch_app("vim").ok
    assert calls == [["/usr/bin/qterminal", "-e", "vim"]]
    desktop["exec"] = 'vim "/tmp/file with spaces.txt" %F'
    assert st.launch_app("Vim").ok
    assert calls[-1] == ["/usr/bin/qterminal", "-e", "vim", "/tmp/file with spaces.txt"]


def test_terminal_app_never_falls_back_to_background(monkeypatch):
    monkeypatch.setattr(st, "_get_desktop_apps", lambda: [
        {"stem": "htop", "names": ["Htop"], "exec": "htop", "terminal": True},
    ])
    monkeypatch.setattr(st, "_app_aliases", lambda: {})
    monkeypatch.setattr(st, "_find_desktop_session_env", lambda: {"DISPLAY": ":0"})
    monkeypatch.setattr(st.shutil, "which", lambda *args, **kwargs: None)
    calls = []
    monkeypatch.setattr(st, "_launch_argv", lambda *args: calls.append(args) or st.ExecResult(False, "failed"))
    assert "终端模拟器" in st.launch_app("htop").message
    assert not calls
    monkeypatch.setattr(st.shutil, "which", lambda *args, **kwargs: "/usr/bin/qterminal")
    assert not st.launch_app("htop").ok
    assert len(calls) == 1
    monkeypatch.setattr(st, "_find_desktop_session_env", lambda: None)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert "桌面会话" in st.launch_app("htop").message
    assert len(calls) == 1


def test_launch_apps_aggregates_and_splits(monkeypatch):
    calls: list[str] = []

    def fake_launch(name: str, language: str | None = None) -> st.ExecResult:
        calls.append(name)
        if name == "bad":
            return st.ExecResult(False, f"应用未找到: {name}")
        return st.ExecResult(True, f"已启动应用: {name}")

    monkeypatch.setattr(st, "launch_app", fake_launch)

    r = st.launch_apps(["qterminal", "bad", "chromium"])
    assert r.ok is True
    assert calls == ["qterminal", "bad", "chromium"]
    assert "qterminal" in r.detail and "bad" in r.detail

    calls.clear()
    r2 = st.launch_apps("qterminal、chromium")
    assert calls == ["qterminal", "chromium"]
    assert r2.ok is True

    calls.clear()
    r3 = st.launch_apps(["bad"])
    assert r3.ok is False

    r4 = st.launch_apps([])
    assert r4.ok is False
    assert "缺少" in r4.message

    # 中文连词：片段都能被本地别名解析才拆
    calls.clear()
    r5 = st.launch_apps("终端和控制中心")
    assert r5.ok is True
    assert calls == ["终端", "控制中心"]

    # 片段不可解析（可能是应用名自带"和"）→ 整串当单个应用名，不误拆
    calls.clear()
    r6 = st.launch_apps("神秘应用甲和神秘应用乙")
    assert r6.ok is True
    assert calls == ["神秘应用甲和神秘应用乙"]


def test_split_app_names_conservative():
    # 标点直接拆；空白只有每段都可解析才拆（不拆散 "file manager"）
    assert st.split_app_names("终端, 文件管理器") == ["终端", "文件管理器"]
    assert st.split_app_names("终端\n控制中心") == ["终端", "控制中心"]
    assert st.split_app_names("终端、设置；vim") == ["终端", "设置", "vim"]
    assert st.split_app_names("qterminal browser") == ["qterminal", "browser"]
    assert st.split_app_names("file manager") == ["file manager"]
    # 中文连词：全部可解析才拆
    assert st.split_app_names("终端和控制中心") == ["终端", "控制中心"]
    assert st.split_app_names("设置、终端以及控制中心") == ["设置", "终端", "控制中心"]
    # and（大小写不敏感）
    assert st.split_app_names("file manager and terminal") == ["file manager", "terminal"]
    # 不可解析的连词串保持整串，不误拆
    assert st.split_app_names("神秘应用甲和神秘应用乙") == ["神秘应用甲和神秘应用乙"]
    assert st.split_app_names("") == []


def test_exec_argv_strips_field_codes():
    assert st._exec_argv("/usr/bin/chromium %U") == ["/usr/bin/chromium"]
    assert st._exec_argv("lximage-qt %F") == ["lximage-qt"]
    assert st._exec_argv("mpv --player-operation-mode=pseudo-gui -- %U") == \
        ["mpv", "--player-operation-mode=pseudo-gui", "--"]
    assert st._exec_argv('sh -c "echo hi" %U') == ["sh", "-c", "echo hi"]


def test_match_priority():
    apps = [
        {"id": "org.kde.kcalc.desktop", "stem": "org.kde.kcalc",
         "names": ["KCalc", "KCalc 计算器"], "exec": "kcalc"},
        {"id": "chromium.desktop", "stem": "chromium",
         "names": ["Chromium Web Browser", "Chromium 网页浏览器"],
         "exec": "/usr/bin/chromium %U"},
    ]
    # 精确 Name[zh_CN] 命中
    assert st._match_desktop_app("Chromium 网页浏览器", apps)["stem"] == "chromium"
    # 文件名命中
    assert st._match_desktop_app("org.kde.kcalc", apps)["stem"] == "org.kde.kcalc"
    # 包含命中（中文名内）
    assert st._match_desktop_app("计算器", apps)["stem"] == "org.kde.kcalc"
    # 精确优先于包含
    assert st._match_desktop_app("KCalc", apps)["stem"] == "org.kde.kcalc"
    # English display name and case-insensitive substring matching
    assert st._match_desktop_app("Chromium Web Browser", apps)["stem"] == "chromium"
    assert st._match_desktop_app("chromium", apps)["stem"] == "chromium"
    # 无匹配
    assert st._match_desktop_app("不存在", apps) is None


def test_match_desktop_app_rejects_single_char_substring():
    # 单字/泛化词的包含命中会打开或关闭错误的应用（"打开 e"→Chromium，
    # "关闭 器"→计算器），这类兜底必须拒绝；精确命中不受影响。
    apps = [
        {"id": "kcalc.desktop", "stem": "kcalc",
         "names": ["KCalc", "KCalc 计算器"], "exec": "kcalc %U"},
        {"id": "chromium.desktop", "stem": "chromium",
         "names": ["Chromium", "Chromium Web Browser"], "exec": "chromium %U"},
    ]
    assert st._match_desktop_app("器", apps) is None
    assert st._match_desktop_app("计", apps) is None
    assert st._match_desktop_app("a", apps) is None
    assert st._match_desktop_app("e", apps) is None
    # 多字包含命中仍然保留
    assert st._match_desktop_app("计算器", apps)["stem"] == "kcalc"
    # 单个字符的正式应用名（精确）不受“至少 2 字”限制
    apps.append({"id": "x.desktop", "stem": "x", "names": ["X"], "exec": "x"})
    assert st._match_desktop_app("X", apps)["stem"] == "x"


def test_match_desktop_app_prefers_shortest_contains_name():
    # 多个包含命中时取最短名称，避免依赖目录扫描顺序而随机命中
    apps = [
        {"id": "long.desktop", "stem": "long", "names": ["Very Long Edit Tool"], "exec": "long"},
        {"id": "short.desktop", "stem": "short", "names": ["Text Edit"], "exec": "short"},
    ]
    assert st._match_desktop_app("edit", apps)["stem"] == "short"


def test_launch_app_not_found_signals_escalate(monkeypatch):
    monkeypatch.setattr(st, "_find_desktop_session_env", lambda: None)
    monkeypatch.setattr(st, "_get_desktop_apps", lambda: [])
    monkeypatch.setattr(st, "_match_desktop_app", lambda query, apps: None)
    monkeypatch.setattr(
        st,
        "_launch_argv",
        lambda *args, **kwargs: st.ExecResult(False, "启动失败"),
    )
    result = st.launch_app("不存在")
    assert result.ok is False
    assert result.escalate == "app_not_found"


def test_launch_app_suggests_near_miss_without_launching(monkeypatch):
    monkeypatch.setattr(st, "_find_desktop_session_env", lambda: None)
    apps = [
        {"id": "chromium.desktop", "stem": "chromium",
         "names": ["Chromium", "Chromium Web Browser"], "exec": "chromium %U"},
        {"id": "wechat.desktop", "stem": "wechat",
         "names": ["WeChat", "微信"], "exec": "wechat"},
    ]
    monkeypatch.setattr(st, "_get_desktop_apps", lambda: apps)
    launched: list[list[str]] = []

    def fake_launch(argv, app_name, session_env=None):
        launched.append(list(argv))
        return st.ExecResult(False, "启动失败")

    monkeypatch.setattr(st, "_launch_argv", fake_launch)
    # 裸命令回退先经 _bare_command_path 解析：不存在则不启动、不盲跑裸词
    monkeypatch.setattr(st, "_bare_command_path", lambda name: None)
    monkeypatch.setattr(st, "_run", lambda *a, **k: st.ExecResult(False, "not found"))

    # 英文近似：chrome → 只做只读建议，绝不启动原始名或候选名
    r = st.launch_app("chrome")
    assert r.ok is False
    assert r.escalate is None
    assert "chromium" in r.message.casefold()

    # 中文近似：微迅 → 建议 微信
    r2 = st.launch_app("微迅")
    assert r2.ok is False
    assert r2.escalate is None
    assert "微信" in r2.message
    # 原始名与候选名都没有被启动
    assert launched == []
    assert not any(
        any(c in argv0.casefold() for c in ("chromium", "wechat", "微信"))
        for argv0 in (item[0] for item in launched)
    )


def test_launch_app_multi_name_still_escalates(monkeypatch):
    """“打开终端和控制中心”整串：不做近似建议，保持升级云端由模型拆分。"""
    monkeypatch.setattr(st, "_find_desktop_session_env", lambda: None)
    monkeypatch.setattr(st, "_get_desktop_apps", lambda: [])
    monkeypatch.setattr(
        st, "_launch_argv", lambda *a, **k: st.ExecResult(False, "启动失败")
    )
    monkeypatch.setattr(st, "_run", lambda *a, **k: st.ExecResult(False, "not found"))
    r = st.launch_app("终端和控制中心")
    assert r.ok is False
    assert r.escalate == "app_not_found"
    assert "相近的应用" not in r.message


def test_common_chinese_app_aliases_resolve_to_real_commands():
    assert st._app_aliases()["终端"] == "qterminal"
    assert st._app_aliases()["设置"] == "bianbu-control-center-shell"
    assert st._app_aliases()["系统设置"] == "bianbu-control-center-shell"
    assert st._app_aliases()["bianbu-control-center"] == "bianbu-control-center-shell"
    assert st._app_command("qterminal") == ["qterminal"]


def test_common_english_app_aliases_resolve_without_ambiguity():
    assert st._app_aliases()["terminal"] == "qterminal"
    assert st._app_aliases()["settings"] == "bianbu-control-center-shell"
    assert st._app_aliases()["system settings"] == "bianbu-control-center-shell"
    assert st._app_aliases()["file manager"] == "pcmanfm-qt"


def test_launch_app_english_name_uses_exec(monkeypatch):
    apps = [{"id": "firefox.desktop", "stem": "firefox",
             "names": ["Firefox Web Browser"], "exec": "firefox %U"}]
    monkeypatch.setattr(st, "_find_desktop_session_env", lambda: {})
    monkeypatch.setattr(st, "_get_desktop_apps", lambda: apps)
    monkeypatch.setattr(st, "_match_desktop_app", lambda query, items: items[0])
    called = {}
    monkeypatch.setattr(st, "_launch_argv", lambda argv, *args, **kwargs: (called.update(argv=argv) or st.ExecResult(True, "ok")))
    result = st.launch_app("Firefox Web Browser")
    assert result.ok is True
    assert called["argv"] == ["firefox"]


def test_close_app_uses_alias_target_same_as_launch(monkeypatch):
    # "关闭控制中心" 与 "打开控制中心" 解析到同一个目标
    monkeypatch.setattr(st, "_match_desktop_app", lambda query, apps: None)
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=3, **kwargs):
        calls.append(list(cmd))
        return st.ExecResult(cmd[2] == st._pkill_pattern("bianbu-control-center-shell"), "ok")

    monkeypatch.setattr(st, "_run", fake_run)
    r = st.close_app("控制中心")
    assert r.ok is True
    assert "已关闭应用" in r.message
    assert calls == [_pkill_call("bianbu-control-center-shell")]


def test_close_app_resolves_desktop_exec(monkeypatch):
    apps = [{"id": "kcalc.desktop", "stem": "kcalc",
             "names": ["KCalc", "KCalc 计算器"], "exec": "kcalc %U"}]
    monkeypatch.setattr(st, "_get_desktop_apps", lambda: apps)
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=3, **kwargs):
        calls.append(list(cmd))
        return st.ExecResult(cmd[2] == st._pkill_pattern("kcalc"), "ok")

    monkeypatch.setattr(st, "_run", fake_run)
    r = st.close_app("计算器")
    assert r.ok is True
    assert "已关闭应用" in r.message
    assert calls == [_pkill_call("kcalc")]


def test_close_app_never_pkills_non_ascii_raw_name(monkeypatch):
    # 真机事故：命令行里含"关闭微信"字样的 SSH bash 被 `pkill -f 微信` 子串误杀。
    # 非 ASCII 原样名不再直接 pkill，且不会报告"已关闭应用"。
    monkeypatch.setattr(st, "_app_aliases", lambda: {})
    monkeypatch.setattr(st, "_get_desktop_apps", lambda: [])
    monkeypatch.setattr(st, "_match_desktop_app", lambda query, apps: None)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        st, "_run",
        lambda cmd, timeout=3, **kw: calls.append(list(cmd)) or st.ExecResult(False, "no match"),
    )
    r = st.close_app("微信")
    assert calls == []
    assert r.ok is False
    assert r.escalate == "app_not_found"
    assert "已关闭" not in r.message


def test_close_app_escalates_when_no_process_matches(monkeypatch):
    monkeypatch.setattr(st, "_app_aliases", lambda: {})
    monkeypatch.setattr(st, "_get_desktop_apps", lambda: [])
    monkeypatch.setattr(st, "_match_desktop_app", lambda query, apps: None)
    # 原样名兜底只对 PATH 中真实存在的可执行文件生效，这里模拟可解析目标
    monkeypatch.setattr(st, "_raw_close_target", lambda name: name)
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=3, **kwargs):
        calls.append(list(cmd))
        return st.ExecResult(False, "no match")

    monkeypatch.setattr(st, "_run", fake_run)
    r = st.close_app("chrome")
    assert calls == [_pkill_call("chrome")]
    assert r.ok is False
    assert r.escalate == "app_not_found"
    assert "未找到运行中的应用" in r.message


def test_close_app_suggests_near_miss_without_killing(monkeypatch):
    monkeypatch.setattr(st, "_app_aliases", lambda: {})
    monkeypatch.setattr(st, "_match_desktop_app", lambda query, apps: None)
    apps = [
        {"id": "chromium.desktop", "stem": "chromium",
         "names": ["Chromium", "Chromium Web Browser"], "exec": "chromium %U"},
        {"id": "wechat.desktop", "stem": "wechat",
         "names": ["WeChat", "微信"], "exec": "wechat"},
    ]
    monkeypatch.setattr(st, "_get_desktop_apps", lambda: apps)
    monkeypatch.setattr(
        st, "_raw_close_target", lambda name: name if name.isascii() else None
    )
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=3, **kwargs):
        calls.append(list(cmd))
        return st.ExecResult(False, "no match")

    monkeypatch.setattr(st, "_run", fake_run)

    # 英文近似：close chrome（未装）→ 提示 chromium，且绝未尝试关闭候选
    r = st.close_app("chrome")
    assert r.ok is False
    assert r.escalate is None
    assert "chromium" in r.message.casefold()
    assert calls == [_pkill_call("chrome")]

    # 中文近似：微迅 → 建议 微信（非 ASCII 名不做原样 pkill 兜底，防子串误杀）
    r2 = st.close_app("微迅")
    assert r2.ok is False
    assert r2.escalate is None
    assert "微信" in r2.message
    assert calls == [_pkill_call("chrome")]


def test_close_app_installed_but_not_running_escalates(monkeypatch):
    # "设置"装了但没在运行：同一目标的别名（系统设置）不应重复建议，
    # 无其他近似 → 保持升级云端说明“未在运行”
    monkeypatch.setattr(st, "_app_aliases", lambda: {
        "设置": "bianbu-control-center-shell",
        "系统设置": "bianbu-control-center-shell",
    })
    monkeypatch.setattr(st, "_get_desktop_apps", lambda: [])
    monkeypatch.setattr(st, "_match_desktop_app", lambda query, apps: None)
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=3, **kwargs):
        calls.append(list(cmd))
        return st.ExecResult(False, "no match")

    monkeypatch.setattr(st, "_run", fake_run)
    r = st.close_app("设置")
    assert r.ok is False
    assert r.escalate == "app_not_found"
    assert "未找到运行中的应用" in r.message
    assert "相近的应用" not in r.message
    # 只按别名解析到的真实可执行名关闭；非 ASCII 的“设置”不做原样 pkill 兜底
    assert calls == [_pkill_call("bianbu-control-center-shell")]


def test_close_app_multi_name_still_escalates_without_suggest(monkeypatch):
    # “关闭终端和控制中心”整串：不做近似建议，保持升级云端由模型拆分
    monkeypatch.setattr(st, "_app_aliases", lambda: {})
    monkeypatch.setattr(st, "_get_desktop_apps", lambda: [])
    monkeypatch.setattr(st, "_match_desktop_app", lambda query, apps: None)
    monkeypatch.setattr(st, "_run", lambda *a, **k: st.ExecResult(False, "no match"))
    r = st.close_app("终端和控制中心")
    assert r.ok is False
    assert r.escalate == "app_not_found"
    assert "相近的应用" not in r.message


def test_close_app_escapes_regex_metachars_in_pkill_target(monkeypatch):
    # .desktop 文件名/命令里的 "." 等元字符必须按字面转义，不能当 ERE 通配符
    monkeypatch.setattr(st, "_app_aliases", lambda: {"测试应用": "some.app.bin"})
    monkeypatch.setattr(st, "_match_desktop_app", lambda query, apps: None)
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=3, **kwargs):
        calls.append(list(cmd))
        return st.ExecResult(True, "ok")

    monkeypatch.setattr(st, "_run", fake_run)
    r = st.close_app("测试应用")
    assert r.ok is True
    assert calls == [_pkill_call("some.app.bin")]
    # "." 必须按字面转义：模式不能把 some.app.bin 当成 someXappXbin 通配匹配
    pattern = calls[0][2]
    assert r"some\.app\.bin" in pattern
    assert re.search(pattern, "someXappXbin") is None


def test_pkill_pattern_anchors_to_executable_name():
    # 关闭 code 不能误杀 codium；也不能杀掉命令行参数里恰好含目标名的进程
    pattern = st._pkill_pattern("code")
    assert re.search(pattern, "code") is not None
    assert re.search(pattern, "/usr/bin/code --flag") is not None
    assert re.search(pattern, "codium --flag") is None
    assert re.search(pattern, "vim code-notes.txt") is None
    # 解释器脚本型目标：python3 /path/app.py 只匹配脚本 token
    assert re.search(st._pkill_pattern("app.py"), "python3 /opt/app.py") is not None


def test_desktop_close_target_skips_wrappers_and_inline_commands():
    assert st._desktop_close_target("/usr/bin/featherpad %U") == "featherpad"
    assert st._desktop_close_target(
        "env BAMF_DESKTOP_FILE_HINT=/usr/share/applications/chromium.desktop "
        "/usr/bin/chromium %U"
    ) == "chromium"
    assert st._desktop_close_target(
        'dbus-run-session -- bash -c "exec /usr/bin/xterm -e vim"'
    ) == "xterm"
    assert st._desktop_close_target('sh -c "exec /usr/bin/xterm -e vim"') == "xterm"
    assert st._desktop_close_target("env") is None
    assert st._desktop_close_target("") is None


def test_desktop_close_target_maps_libreoffice_wrapper_to_soffice_bin():
    assert st._desktop_close_target("libreoffice --writer %U") == "soffice.bin"
    assert st._expected_process_name(["libreoffice", "--writer"]) == "soffice.bin"


def test_match_desktop_app_strips_trailing_app_classifier(monkeypatch):
    apps = [
        {
            "id": "libreoffice-writer.desktop",
            "stem": "libreoffice-writer",
            "names": ["LibreOffice Writer"],
            "exec": "libreoffice --writer %U",
        }
    ]
    assert st._match_desktop_app("LibreOffice Writer应用", apps)["stem"] == "libreoffice-writer"


def test_close_app_libreoffice_writer_with_app_suffix(monkeypatch):
    monkeypatch.setattr(st, "_app_aliases", lambda: {})
    monkeypatch.setattr(
        st,
        "_get_desktop_apps",
        lambda: [
            {
                "id": "libreoffice-writer.desktop",
                "stem": "libreoffice-writer",
                "names": ["LibreOffice Writer"],
                "exec": "libreoffice --writer %U",
            }
        ],
    )
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=3, **kwargs):
        calls.append(list(cmd))
        return st.ExecResult(True, "ok")

    monkeypatch.setattr(st, "_run", fake_run)
    r = st.close_app("LibreOffice Writer应用")
    assert r.ok is True
    assert calls == [_pkill_call("soffice.bin")]


def test_close_app_uses_real_process_name_for_libreoffice(monkeypatch):
    monkeypatch.setattr(st, "_app_aliases", lambda: {})
    monkeypatch.setattr(st, "_get_desktop_apps", lambda: [])
    monkeypatch.setattr(
        st,
        "_match_desktop_app",
        lambda query, apps: {
            "exec": "libreoffice --writer %U",
        },
    )
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=3, **kwargs):
        calls.append(list(cmd))
        return st.ExecResult(True, "ok")

    monkeypatch.setattr(st, "_run", fake_run)
    r = st.close_app("LibreOffice Writer")
    assert r.ok is True
    assert calls == [_pkill_call("soffice.bin")]


def test_close_apps_aggregates_and_splits(monkeypatch):
    calls: list[str] = []

    def fake_close(name: str) -> st.ExecResult:
        calls.append(name)
        if name == "bad":
            return st.ExecResult(False, f"未找到运行中的应用: {name}", escalate="app_not_found")
        return st.ExecResult(True, f"已关闭应用: {name}")

    monkeypatch.setattr(st, "close_app", fake_close)

    r = st.close_apps(["终端", "bad", "控制中心"])
    assert r.ok is True
    assert calls == ["终端", "bad", "控制中心"]
    assert "终端" in r.detail and "bad" in r.detail

    # 标点分隔 + 中文连词：片段都可被本地别名解析才拆
    calls.clear()
    r2 = st.close_apps("终端、控制中心")
    assert r2.ok is True
    assert calls == ["终端", "控制中心"]

    calls.clear()
    r3 = st.close_apps("终端和控制中心")
    assert r3.ok is True
    assert calls == ["终端", "控制中心"]

    # 片段不可解析（可能是应用名自带"和"）→ 整串当单个应用名，不误拆
    calls.clear()
    r4 = st.close_apps("神秘应用甲和神秘应用乙")
    assert r4.ok is True
    assert calls == ["神秘应用甲和神秘应用乙"]

    r5 = st.close_apps(["bad"])
    assert r5.ok is False
    assert "关闭失败" in r5.message

    r6 = st.close_apps([])
    assert r6.ok is False
    assert "缺少" in r6.message

    # language=en 时聚合文案走英文（与 launch_apps 对称）
    calls.clear()
    r7 = st.close_apps(["终端"], language="en")
    assert r7.ok is True
    assert r7.message == "Closed 1 application(s)"


def run():
    import traceback
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
                print(f"  OK  {name}")
            except Exception:
                failed += 1
                print(f"  XX  {name}")
                traceback.print_exc()
    total = passed + failed
    print(f"\n结果：{passed}/{total} 通过")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
