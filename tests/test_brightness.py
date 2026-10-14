"""亮度工具回归：无物理背光的开发板应明确提示不支持，而不是报权限错误。"""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bibit_intent.execution import system_tools


def test_backlight_power_state_detects_powerdown(tmp_path: Path):
    (tmp_path / "bl_power").write_text("4\n")
    assert system_tools._backlight_is_powered_off(tmp_path) is True


def test_backlight_power_state_accepts_active(tmp_path: Path):
    (tmp_path / "bl_power").write_text("0\n")
    assert system_tools._backlight_is_powered_off(tmp_path) is False


def test_backlight_power_state_missing_is_not_powered_off(tmp_path: Path):
    assert system_tools._backlight_is_powered_off(tmp_path) is False


def test_set_brightness_reports_missing_physical_backlight(tmp_path: Path, monkeypatch):
    (tmp_path / "bl_power").write_text("4\n")
    monkeypatch.setattr(system_tools, "_backlight_state", lambda: (tmp_path, 255, 50))
    monkeypatch.setattr(system_tools, "_write_backlight_level", lambda *args: (_ for _ in ()).throw(AssertionError("should not write")))

    result = system_tools.set_brightness(80)
    assert result.ok is False
    assert "没有可用的背光" in result.message
    assert result.detail == f"bl_power=4: {tmp_path}"


def test_get_brightness_returns_current_level(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(system_tools, "_backlight_state", lambda: (tmp_path, 255, 45))
    monkeypatch.setattr(system_tools, "_backlight_is_powered_off", lambda _bl_dir: False)

    result = system_tools.get_brightness()
    assert result.ok is True
    assert result.message == "当前亮度是 45%"


def test_get_brightness_reports_missing_physical_backlight(tmp_path: Path, monkeypatch):
    (tmp_path / "bl_power").write_text("4\n")
    monkeypatch.setattr(system_tools, "_backlight_state", lambda: (tmp_path, 255, 50))

    result = system_tools.get_brightness()
    assert result.ok is False
    assert "没有可用的背光" in result.message
    assert result.detail == f"bl_power=4: {tmp_path}"


def test_adjust_brightness_reports_missing_physical_backlight(tmp_path: Path, monkeypatch):
    (tmp_path / "bl_power").write_text("4\n")
    monkeypatch.setattr(system_tools, "_backlight_state", lambda: (tmp_path, 255, 50))
    monkeypatch.setattr(system_tools, "_write_backlight_level", lambda *args: (_ for _ in ()).throw(AssertionError("should not write")))

    result = system_tools.adjust_brightness("up", 10)
    assert result.ok is False
    assert "没有可用的背光" in result.message
    assert result.detail == f"bl_power=4: {tmp_path}"
