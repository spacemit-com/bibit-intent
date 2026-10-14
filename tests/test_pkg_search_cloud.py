"""pkg_search 无匹配包转云端的执行层测试。"""

from __future__ import annotations

import importlib.util
import sys

from bibit_intent.execution.executor import Executor
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.resources import resource_path


def _load_ubuntu_cmd_handlers():
    path = resource_path("skills", "ubuntu_cmd", "handlers.py")
    spec = importlib.util.spec_from_file_location("ubuntu_cmd_handlers_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module.__name__] = module
    spec.loader.exec_module(module)
    return module


def test_pkg_search_empty_result_signals_escalate(monkeypatch):
    module = _load_ubuntu_cmd_handlers()
    monkeypatch.setattr(
        module,
        "_run",
        lambda cmd, timeout=20: module.ExecResult(True, "ok", ""),
    )
    result = module.pkg_search("特朗普在干什么")
    assert result.ok is False
    assert result.escalate == "no_pkg_match"


def test_pkg_search_english_query_empty_result_signals_escalate(monkeypatch):
    module = _load_ubuntu_cmd_handlers()
    monkeypatch.setattr(module, "_run", lambda cmd, timeout=20: module.ExecResult(True, "ok", ""))
    result = module.pkg_search("a package for quantum video editor")
    assert result.ok is False
    assert result.escalate == "no_pkg_match"


def test_pkg_search_match_stays_local(monkeypatch):
    module = _load_ubuntu_cmd_handlers()
    monkeypatch.setattr(
        module,
        "_run",
        lambda cmd, timeout=20: module.ExecResult(True, "ok", "vlc - Video player"),
    )
    result = module.pkg_search("vlc")
    assert result.ok is True
    assert result.escalate is None


def test_pkg_search_english_match_stays_local(monkeypatch):
    module = _load_ubuntu_cmd_handlers()
    monkeypatch.setattr(module, "_run", lambda cmd, timeout=20: module.ExecResult(True, "ok", "vim - enhanced vi editor"))
    result = module.pkg_search("vim editor")
    assert result.ok is True
    assert result.escalate is None


def test_executor_normalizes_escalate_flag(monkeypatch):
    registry = ToolRegistry()
    load_skills(resource_path("skills"), registry, {})

    def fake_pkg_search(**kwargs):
        return _load_ubuntu_cmd_handlers().ExecResult(
            False, "未找到匹配的软件包", "", escalate="no_pkg_match"
        )

    executor = Executor(
        registry,
        confirm_callback=lambda action: True,
        handlers={"pkg_search": fake_pkg_search},
    )
    result = executor.execute("pkg_search", {"query": "特朗普在干什么"})
    assert result.escalate == "no_pkg_match"
