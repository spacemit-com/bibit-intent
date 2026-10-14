"""Tests for the read-only system_update_status tool in ubuntu_cmd."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bibit_intent.resources import resource_path


def _load_module():
    path = resource_path("skills", "ubuntu_cmd", "handlers.py")
    spec = importlib.util.spec_from_file_location("system_update_status_handlers", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["system_update_status_handlers"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def module(tmp_path, monkeypatch):
    mod = _load_module()
    monkeypatch.setattr(mod, "_SYSTEM_UPDATE_STATE", tmp_path / "system-update.status")
    monkeypatch.setattr(mod, "_SYSTEM_UPDATE_DETAIL", tmp_path / "system-update.status.json")
    monkeypatch.setattr(
        mod,
        "run_command",
        lambda cmd, timeout=5: SimpleNamespace(stdout="inactive" if cmd[-1].endswith('.timer') else "active"),
    )
    return mod


def test_no_record(module):
    result = module.system_update_status()
    assert result.ok is True
    assert "暂无系统更新任务记录" in result.message


def test_pending_timer_is_not_reported_as_completed(module, monkeypatch):
    monkeypatch.setattr(module, 'run_command', lambda *args, **kwargs: SimpleNamespace(stdout='active'))
    result = module.system_update_status()
    assert result.ok
    assert '等待后台启动' in result.message
    assert '尚未完成升级' in result.message


def test_succeeded_reads_detail(module):
    module._SYSTEM_UPDATE_STATE.write_text("succeeded\n", encoding="utf-8")
    module._SYSTEM_UPDATE_DETAIL.write_text(
        json.dumps({"state": "succeeded", "skipped": {}}),
        encoding="utf-8",
    )
    result = module.system_update_status()
    assert result.ok is True
    assert "系统更新已完成" in result.message
    assert "系统更新已完成" in result.detail
    assert "active" in result.detail


def test_awaiting_confirmation_lists_downgrades(module):
    module._SYSTEM_UPDATE_STATE.write_text("awaiting_confirmation\n", encoding="utf-8")
    module._SYSTEM_UPDATE_DETAIL.write_text(
        json.dumps({"state": "awaiting_confirmation", "downgrades": {"bash": ["5.3-2ubuntu1", "0"]}}),
        encoding="utf-8",
    )
    result = module.system_update_status()
    assert "检测到降级，等待确认" in result.message
    assert "bash：5.3-2ubuntu1 -> 0" in result.detail


def test_failed_reads_error(module):
    module._SYSTEM_UPDATE_STATE.write_text("failed\n", encoding="utf-8")
    module._SYSTEM_UPDATE_DETAIL.write_text(
        json.dumps({"state": "failed", "error": "simulated error"}),
        encoding="utf-8",
    )
    result = module.system_update_status()
    assert "系统更新失败" in result.message
    assert "simulated error" in result.detail
