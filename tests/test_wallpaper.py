import json
from pathlib import Path

import pytest

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.core.wallpaper_request import parse_wallpaper_request
from bibit_intent.execution import system_tools, wallpaper_helper, wallpaper_tools
from bibit_intent.execution.executor import Executor
from bibit_intent.execution.system_tools import ExecResult
from bibit_intent.routing.router import Router, Stage
from bibit_intent.routing.rule_engine import RuleEngine


@pytest.fixture
def router():
    registry = ToolRegistry()
    calls = []

    def execute(**params):
        calls.append(params)
        return ExecResult(True, "ok")

    return Router(registry, RuleEngine(registry), None,
                  Executor(registry, handlers={"set_wallpaper": execute})), calls


@pytest.mark.parametrize("text,path", [
    ("把/home/bianbu/Pictures/a.png设为壁纸", "/home/bianbu/Pictures/a.png"),
    ("请帮我将 ~/Pictures/a.jpg 设置为桌面壁纸", "~/Pictures/a.jpg"),
    ('将“/home/bianbu/图片/山 水.png”设为壁纸', "/home/bianbu/图片/山 水.png"),
    ("设置壁纸为file:///home/bianbu/Pictures/a.png", "file:///home/bianbu/Pictures/a.png"),
    ("把壁纸换成/home/bianbu/Pictures/a.png", "/home/bianbu/Pictures/a.png"),
    ("把/home/bianbu/Pictures/a.png变成壁纸", "/home/bianbu/Pictures/a.png"),
    ('set wallpaper to "/home/user/a b.png"', "/home/user/a b.png"),
    ("please use ~/Pictures/a.jpg as the desktop background", "~/Pictures/a.jpg"),
])
def test_explicit_path_routes(router, text, path):
    handler, calls = router
    result = handler.handle(text)
    assert result.stage is Stage.RULE and result.tool_name == "set_wallpaper"
    assert result.params == {"path": path} and len(calls) == 1


@pytest.mark.parametrize("text", [
    "切换壁纸", "下一张壁纸", "把某个位置的图片变成壁纸", "把下载文件夹的照片设为壁纸",
    "不要把/home/user/a.png设为壁纸", "怎么把/home/user/a.png设为壁纸？",
    "把/home/user/a.png设为壁纸然后关机", "设置壁纸为/home/user/a.png并关机",
    "set wallpaper to /home/user/a.png and reboot", "set wallpaper to https://example.com/a.png",
    "设置壁纸为a.png", "明天把/home/user/a.png设为壁纸",
])
def test_no_guessing_or_partial_execution(router, text):
    handler, calls = router
    result = handler.handle(text)
    assert result.stage is Stage.CLOUD and result.escalate_reason == "ambiguous_wallpaper_request"
    assert not calls and parse_wallpaper_request(text) is None


@pytest.mark.parametrize("variable,value", [("BIANBU_SANDBOX_ACTIVE", "1"), ("BIANBU_WORKER_EFFECTIVE_MODE", "sandbox")])
def test_host_only(monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    monkeypatch.setattr(wallpaper_tools, "_desktop_processes", lambda: pytest.fail("Must not inspect sandbox desktop"))
    result = wallpaper_tools.set_wallpaper("/tmp/photo.png")
    assert not result.ok and result.escalate == "needs_host_channel"


def test_missing_path():
    result = wallpaper_tools.set_wallpaper()
    assert not result.ok and result.need_param == "path"


@pytest.mark.parametrize("sessions", [[], [(100, 1000), (101, 1001)]])
def test_no_or_multiple_desktops(monkeypatch, sessions):
    monkeypatch.setattr(wallpaper_tools, "_desktop_processes", lambda: sessions)
    assert not wallpaper_tools.set_wallpaper("/tmp/photo.png").ok


@pytest.mark.parametrize("root", [True, False])
def test_helper_runs_as_desktop_owner(monkeypatch, root):
    calls = []
    monkeypatch.setattr(wallpaper_tools.os, "getuid", lambda: 0 if root else 1000)
    monkeypatch.setattr(wallpaper_tools, "_desktop_processes", lambda: [(100, 1000)])
    monkeypatch.setattr(wallpaper_tools.pwd, "getpwuid", lambda owner: type("User", (), {"pw_name": "bianbu"}))

    def execute(command, **kwargs):
        calls.append(command)
        return ExecResult(True, "ok", '{"ok": true, "path": "/tmp/a b.png"}')

    monkeypatch.setattr(system_tools, "_run", execute)
    assert wallpaper_tools.set_wallpaper("/tmp/a b.png").ok
    assert calls[0][-2:] == ["100", "/tmp/a b.png"]
    assert (calls[0][:4] == ["runuser", "-u", "bianbu", "--"]) is root
    assert "-I" in calls[0]


@pytest.mark.parametrize("result", [ExecResult(False, "failed", "timeout"), ExecResult(True, "ok", "invalid json"),
                                    ExecResult(True, "ok", '{"ok": false, "error": "bad image"}')])
def test_failure_not_reported_as_success(monkeypatch, result):
    monkeypatch.setattr(wallpaper_tools.os, "getuid", lambda: 1000)
    monkeypatch.setattr(wallpaper_tools, "_desktop_processes", lambda: [(100, 1000)])
    monkeypatch.setattr(system_tools, "_run", lambda *args, **kwargs: result)
    assert not wallpaper_tools.set_wallpaper("/tmp/photo.png").ok


def test_image_path_variants(tmp_path):
    image = tmp_path / "山 水.png"
    image.touch()
    for value in (str(image), "~/山 水.png", image.as_uri()):
        assert wallpaper_helper._image_path(value, tmp_path) == image


@pytest.mark.parametrize("value", ["photo.png", "https://example.com/a.png", "file://remote/a.png", "file:///tmp/a.png?q=1", "/tmp/a\x00.png"])
def test_invalid_image_paths(tmp_path, value):
    with pytest.raises(ValueError):
        wallpaper_helper._image_path(value, tmp_path)


def test_non_image_and_missing_paths(tmp_path):
    text = tmp_path / "a.txt"
    text.touch()
    with pytest.raises(ValueError):
        wallpaper_helper._image_path(str(text), tmp_path)
    with pytest.raises(FileNotFoundError):
        wallpaper_helper._image_path(str(tmp_path / "missing.png"), tmp_path)


def test_qsettings_fallback_and_escaped_path(tmp_path):
    system = tmp_path / "system/pcmanfm-qt/lxqt"
    system.mkdir(parents=True)
    config = system / "settings.conf"
    config.write_text('[Desktop]\nWallpaper="/tmp/山 水,50%.png"\nWallpaperMode=zoom\n')
    selected, values = wallpaper_helper._settings([tmp_path / "user", tmp_path / "system"], "lxqt")
    assert selected == config and values["Wallpaper"] == "/tmp/山 水,50%.png"
    assert wallpaper_helper._qt_string(r'"/tmp/\x5c71.png"') == "/tmp/山.png"


@pytest.fixture
def helper_backend(monkeypatch, tmp_path):
    config_dir = tmp_path / "config/pcmanfm-qt/lxqt"
    config_dir.mkdir(parents=True)
    config = config_dir / "settings.conf"
    config.write_text("[Desktop]\nWallpaper=/old.png\nWallpaperMode=fit\n")
    image = tmp_path / "image.png"
    image.touch()
    monkeypatch.setattr(wallpaper_helper, "_context", lambda process_id: ({}, tmp_path, tmp_path, [tmp_path / "config"], "lxqt"))
    monkeypatch.setattr(wallpaper_helper, "_validate_image", lambda path: None)
    calls = []

    def call(environment, *arguments):
        calls.append(arguments)
        if "GetConnectionUnixProcessID" in arguments:
            return "u 100"
        config.write_text(f"[Desktop]\nWallpaper={arguments[-2]}\nWallpaperMode={arguments[-1]}\n")
        return ""

    monkeypatch.setattr(wallpaper_helper, "_call", call)
    return config, image, calls


def test_apply_verifies_persisted_wallpaper(helper_backend):
    config, image, calls = helper_backend
    result = wallpaper_helper.apply_wallpaper(100, str(image))
    assert result["ok"] and result["path"] == str(image) and result["previous_path"] == "/old.png"
    assert calls[-1][-4:] == ("setWallpaper", "ss", str(image), "fit")


def test_color_mode_becomes_zoom(helper_backend):
    config, image, calls = helper_backend
    config.write_text("[Desktop]\nWallpaper=/old.png\nWallpaperMode=color\n")
    assert wallpaper_helper.apply_wallpaper(100, str(image))["mode"] == "zoom"


@pytest.mark.parametrize("setting", ["SlideShowInterval=10", "PerScreenWallpaper=true"])
def test_unsupported_modes_not_silently_changed(helper_backend, setting):
    config, image, calls = helper_backend
    with config.open("a") as stream:
        stream.write(setting + "\n")
    with pytest.raises(ValueError):
        wallpaper_helper.apply_wallpaper(100, str(image))
    assert len(calls) == 1


def test_corrupt_image_prevents_dbus_write(helper_backend, monkeypatch):
    config, image, calls = helper_backend

    def invalid(path):
        raise ValueError("corrupt image")

    monkeypatch.setattr(wallpaper_helper, "_validate_image", invalid)
    with pytest.raises(ValueError):
        wallpaper_helper.apply_wallpaper(100, str(image))
    assert not calls


@pytest.mark.parametrize("owner", ["u 100", "u 101"])
def test_no_success_without_saved_config(helper_backend, monkeypatch, owner):
    config, image, calls = helper_backend
    monkeypatch.setattr(wallpaper_helper, "_call", lambda *args: owner)
    with pytest.raises(ValueError):
        wallpaper_helper.apply_wallpaper(100, str(image))


def test_wallpaper_schema():
    registry = ToolRegistry()
    tool = registry.get("set_wallpaper")
    assert tool.params["path"].is_required
    assert tool.system["channel"] == "host"
    assert "set_wallpaper" in system_tools.HANDLERS
