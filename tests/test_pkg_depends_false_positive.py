"""“帮我记录：9月10号…agent os需要…”被 pkg_depends 误判的回归测试。

三层防护：
  1. 规则收紧：依赖查询必须带疑问词/依赖名词，长叙述句不再命中 pkg_depends；
  2. handler 兜底：apt/dpkg 报“包不存在”时返回友好提示并升级云端，
     不再把裸的“命令失败(码100)”抛给用户；
  3. 长句护栏：词数 ≥ 30 且含分隔符的整句直接走云端（reason=long_sentence）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.resources import resource_path
from bibit_intent.routing.intent import (
    IntentAction,
    decide_local_intent,
    is_long_sentence_with_separator,
)
from bibit_intent.routing.rule_engine import RuleEngine

REGISTRY = ToolRegistry(resource_path("tools.yaml"))
load_skills(resource_path("skills"), REGISTRY, {})
RULE = RuleEngine(REGISTRY)

RECORD_SENTENCE = (
    "帮我记录：9月10号有一个演示，agent os需要全部功能集成进来，"
    "9月15转测试，9月24全部功能测试通过"
)


def _load_ubuntu_cmd_handlers():
    path = resource_path("skills", "ubuntu_cmd", "handlers.py")
    spec = importlib.util.spec_from_file_location("ubuntu_cmd_handlers_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module.__name__] = module
    spec.loader.exec_module(module)
    return module


# ── 1) 规则收紧 ──────────────────────────────────────────────────────

def test_record_sentence_not_pkg_depends():
    assert RULE.match_tool(RECORD_SENTENCE, "pkg_depends") is None


def test_depends_queries_still_match():
    cases = {
        "python3依赖哪些包": "python3",
        "查看vim的依赖关系": "vim",
        "vim需要哪些依赖": "vim",
        "python3 装了哪些依赖包": "python3",
        "curl 依赖什么库": "curl",
        "安装 curl 前需要装什么": "curl",
        "git 依赖哪些包": "git",
    }
    for text, want in cases.items():
        m = RULE.match_tool(text, "pkg_depends")
        assert m is not None, f"应命中 pkg_depends: {text}"
        assert m.slots.get("pkg") == want, (text, m.slots)


def test_statement_sentences_not_pkg_depends():
    for text in ("系统依赖gcc才能运行", "这个软件依赖gcc"):
        assert RULE.match_tool(text, "pkg_depends") is None, text


def test_depends_query_still_routes_local():
    plan = decide_local_intent("python3依赖哪些包", RULE, REGISTRY)
    assert plan.action is IntentAction.RULE_CALL
    assert plan.tool_name == "pkg_depends"
    assert plan.slots.get("pkg") == "python3"


# ── 2) handler 兜底：包不存在 → 友好提示 + 升级云端 ──────────────────

def test_pkg_depends_not_found_escalates(monkeypatch):
    module = _load_ubuntu_cmd_handlers()
    monkeypatch.setattr(
        module,
        "_run",
        lambda cmd, timeout=20: module.ExecResult(
            False, "命令失败(码100)", "E: 没有发现匹配的软件包"
        ),
    )
    r = module.pkg_depends("agent")
    assert r.ok is False
    assert r.escalate == "pkg_not_found"
    assert "未找到软件包 agent" in r.message


def test_pkg_depends_english_not_found_escalates(monkeypatch):
    module = _load_ubuntu_cmd_handlers()
    monkeypatch.setattr(
        module,
        "_run",
        lambda cmd, timeout=20: module.ExecResult(
            False, "command failed (code 100)", "E: No packages found"
        ),
    )
    r = module.pkg_depends("agent")
    assert r.escalate == "pkg_not_found"


def test_pkg_files_dpkg_not_installed_escalates(monkeypatch):
    module = _load_ubuntu_cmd_handlers()
    monkeypatch.setattr(
        module,
        "_run",
        lambda cmd, timeout=20: module.ExecResult(
            False, "命令失败(码1)",
            "dpkg-query: package 'vim' is not installed and no information is available",
        ),
    )
    r = module.pkg_files("vim")
    assert r.escalate == "pkg_not_found"


def test_other_failure_not_escalated(monkeypatch):
    module = _load_ubuntu_cmd_handlers()
    monkeypatch.setattr(
        module,
        "_run",
        lambda cmd, timeout=20: module.ExecResult(False, "命令失败(码1)", "E: 锁文件不可用"),
    )
    r = module.pkg_depends("vim")
    assert r.escalate is None
    assert r.message.startswith("命令失败")


# ── 3) 长句护栏 ──────────────────────────────────────────────────────

def test_long_sentence_predicate():
    assert is_long_sentence_with_separator(RECORD_SENTENCE)
    assert not is_long_sentence_with_separator("打开终端和控制中心")
    assert not is_long_sentence_with_separator(
        "帮我记录一下9月10号有个演示agent os需要全部功能集成"
        "9月15转测试9月24全部功能测试通过"
    )


def test_record_sentence_escalates_to_cloud():
    plan = decide_local_intent(RECORD_SENTENCE, RULE, REGISTRY)
    assert plan.action is IntentAction.ESCALATE
    assert plan.reason == "long_sentence"
