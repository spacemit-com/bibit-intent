"""方案 A 生产化测试（glm_practice_experiment.md §9-10）：
per-tool bound grammar 生成、证据闸门扩展（枚举/开放文本锚点）、
方向锁定 bound 路径（值在场补参执行 / 真缺参 0 模型调用直接澄清）、
RULE_CALL 枚举无锚点不盲执行。

全部离线：本地规则 + stub 模型（不连 llama-server）。
"""

from __future__ import annotations

import pytest

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.core.session import SessionState
from bibit_intent.execution.executor import Executor
from bibit_intent.execution.system_tools import ExecResult
from bibit_intent.models.local_model import (
    LocalModelResult,
    _parse_output,
    build_call_grammar,
    build_bound_grammar,
)
from bibit_intent.routing import router as router_module
from bibit_intent.routing.intent import IntentAction, IntentPlan
from bibit_intent.routing.router import Router, Stage, _keep_evidenced_params
from bibit_intent.routing.rule_engine import RuleEngine


# ── per-tool bound grammar 生成 ────────────────────────────────────


def test_bound_grammar_numeric_locks_tool_and_uses_hyphen_rules():
    g = build_bound_grammar("set_volume", {"level": {"type": "int"}})
    assert '"CALL set_volume"' in g
    assert "number-val" in g
    # llama-server 400：规则名不允许下划线
    for line in g.splitlines():
        name = line.split("::=")[0].strip()
        if name:
            assert "_" not in name, name


def test_bound_grammar_enum_literals_single_key():
    g = build_bound_grammar(
        "set_display_mode",
        {"mode": {"type": "str", "enum": ["extend", "mirror", "primary", "secondary"]}},
    )
    assert '" mode="' in g
    assert '"extend"' in g and '"mirror"' in g
    assert g.count('" mode="') == 1


def test_bound_grammar_empty_spec_allows_plain_call():
    g = build_bound_grammar("window_list", {})
    assert '"CALL window_list"' in g
    assert "kv-pair-" not in g


def test_bound_grammar_multi_key_subsets_no_garbage_keys():
    g = build_bound_grammar("move_win", {"x": {"type": "int"}, "y": {"type": "int"}})
    assert '" x="' in g and '" y="' in g
    assert "kv-pair-0 kv-pair-1" in g  # 双键组合存在
    assert "orlaunch" not in g and "oracle" not in g


def test_bound_grammar_dotted_enum_literals():
    g = build_bound_grammar(
        "set_aspect_ratio",
        {"ratio": {"type": "str", "enum": ["1.00", "1.25", "1.50", "1.75", "2.00"]}},
    )
    assert '"1.25"' in g and '"2.00"' in g


def test_call_grammar_none_keeps_wide_grammar():
    g = build_call_grammar(None)
    assert "tool-name   ::= [a-z_]+" in g


def test_call_grammar_empty_allows_only_escalate():
    g = build_call_grammar([])
    assert "root ::= escalate-cmd" in g
    assert "call-cmd" not in g


def test_call_grammar_enumerates_candidate_tools():
    g = build_call_grammar(["set_volume", "pkg_install"])
    assert '"set_volume" | "pkg_install"' in g
    assert "[a-z_]+" not in g  # 不再允许任意工具名


def test_parse_bound_output_tool_with_underscore():
    r = _parse_output("CALL set_aspect_ratio ratio=1.25", 0.0)
    assert r.action == "CALL"
    assert r.tool_name == "set_aspect_ratio"
    assert r.params == {"ratio": 1.25}


# ── 证据闸门扩展：枚举/开放文本必须有原文锚点 ─────────────────────────


def _tool(name):
    return ToolRegistry().get(name)


def test_enum_guess_without_anchor_is_uncertain():
    ev = _keep_evidenced_params(
        "屏幕显示模式帮我改一下", {"mode": "mirror"}, _tool("set_display_mode")
    )
    assert ev.kept == {}
    assert ev.uncertain == {"mode": "mirror"}


def test_enum_value_literal_in_text_is_kept():
    ev = _keep_evidenced_params(
        "switch to extend display mode", {"mode": "extend"}, _tool("set_display_mode")
    )
    assert ev.kept == {"mode": "extend"}
    assert ev.uncertain == {}


def test_enum_value_via_slot_anchor_is_kept():
    ev = _keep_evidenced_params(
        "把显示器设成扩展模式", {"mode": "extend"}, _tool("set_display_mode")
    )
    assert ev.kept == {"mode": "extend"}


def test_open_str_guess_rejected_in_strict_mode():
    ev = _keep_evidenced_params(
        "搜一下containerd的文档",
        {"query": "myfile"},
        _tool("file_search"),
        strict_str=True,
    )
    assert ev.kept == {}
    assert ev.uncertain == {"query": "myfile"}


def test_open_str_literal_kept_in_strict_mode():
    ev = _keep_evidenced_params(
        "搜一下containerd的文档",
        {"query": "containerd"},
        _tool("file_search"),
        strict_str=True,
    )
    assert ev.kept == {"query": "containerd"}


# ── Router：方向锁定 → bound 路径 ──────────────────────────────────


class StubLocal:
    """bound/wide 双 stub：未被调用的入口触发断言，显式暴露路径选择。"""

    def __init__(self, bound=None, wide=None):
        self._bound = bound
        self._wide = wide
        self.bound_calls: list[tuple[str, str]] = []
        self.wide_calls: list[str] = []

    def is_available(self):
        return True

    def infer(self, user_input, tool_list, analysis=None, max_output_tokens=30, **_kwargs):
        self.wide_calls.append(user_input)
        if self._wide is None:
            raise AssertionError(f"意外走了宽 CALL 路径：{user_input!r}")
        return self._wide

    def infer_bound(
        self,
        user_input,
        tool_name,
        tool_list=None,
        param_spec=None,
        max_output_tokens=30,
        analysis=None,
    ):
        self.bound_calls.append((user_input, tool_name))
        if self._bound is None:
            raise AssertionError(f"意外走了 bound 路径：{user_input!r}")
        return self._bound


def _router(local, handlers):
    registry = ToolRegistry()
    return Router(
        registry,
        RuleEngine(registry),
        local,
        Executor(registry, handlers=handlers),
    )


def test_router_bound_value_present_executes_locked_tool():
    calls = []

    def fake_set_volume(**kw):
        calls.append(kw)
        return ExecResult(True, "ok")

    local = StubLocal(
        bound=LocalModelResult(
            action="CALL", tool_name="set_volume", params={"level": 60}
        )
    )
    r = _router(local, {"set_volume": fake_set_volume}).handle("帮我把音量调到60")
    assert r.stage is Stage.LOCAL
    assert r.tool_name == "set_volume"
    assert r.params == {"level": 60}
    assert calls == [{"level": 60}]
    assert len(local.bound_calls) == 1
    assert local.wide_calls == []  # 工具被锁死后不再给模型重新选工具的自由


def test_router_bound_missing_param_no_model_call(monkeypatch):
    calls = []

    def fake_set_volume(**kw):
        calls.append(kw)
        return ExecResult(True, "ok")

    local = StubLocal()  # bound/wide 都未提供 → 任一模型调用都会失败
    r = _router(local, {"set_volume": fake_set_volume}).handle("帮我把音量调一下")
    assert r.exec_result is not None
    assert r.exec_result.ok is False
    assert r.exec_result.need_param == "level"
    assert calls == []
    assert local.bound_calls == [] and local.wide_calls == []


def test_router_bound_enum_missing_no_model_call():
    local = StubLocal()
    r = _router(local, {}).handle("帮我调一下屏幕缩放比例")
    assert r.exec_result is not None
    assert r.exec_result.need_param == "ratio"
    assert local.bound_calls == [] and local.wide_calls == []


def test_router_wide_enum_guess_not_executed(monkeypatch):
    calls = []

    def fake_display(**kw):
        calls.append(kw)
        return ExecResult(True, "ok")

    local = StubLocal(
        wide=LocalModelResult(
            action="CALL", tool_name="set_display_mode", params={"mode": "mirror"}
        )
    )
    monkeypatch.setattr(router_module, "decide_local_intent", lambda *a, **kw: IntentPlan(
        action=IntentAction.TO_MODEL, candidates=["set_display_mode", "set_resolution"],
    ))
    r = _router(local, {"set_display_mode": fake_display}).handle(
        "屏幕显示模式帮我改一下"
    )
    assert r.stage is Stage.CONFIRM
    assert calls == []  # 无锚点枚举猜值不执行
    assert len(local.wide_calls) == 1


def test_router_bound_missing_sets_session_pending_then_fill_executes():
    calls = []

    def fake_set_volume(**kw):
        calls.append(kw)
        return ExecResult(True, "ok")

    local = StubLocal()
    router = _router(local, {"set_volume": fake_set_volume})
    session = SessionState()
    r1 = router.handle("帮我把音量调一下", session=session)
    assert r1.exec_result.need_param == "level"
    assert session.pending is not None
    r2 = router.handle("60", session=session)
    assert r2.stage is Stage.SLOT_FILL
    assert calls == [{"level": 60}]


# ── Router：RULE_CALL 枚举锚点守卫 ─────────────────────────────────


def _rule_plan(tool_name, slots):
    return IntentPlan(
        action=IntentAction.RULE_CALL,
        tool_name=tool_name,
        slots=slots,
        confidence=1.0,
        matched_pattern="test",
    )


def test_rule_call_enum_without_anchor_not_executed(monkeypatch):
    calls = []

    def fake_display(**kw):
        calls.append(kw)
        return ExecResult(True, "ok")

    router = _router(StubLocal(), {"set_display_mode": fake_display})
    monkeypatch.setattr(
        router_module,
        "decide_local_intent",
        lambda *a, **k: _rule_plan("set_display_mode", {"mode": "extend"}),
    )
    r = router.handle("屏幕显示模式帮我改一下")
    assert r.exec_result is not None
    assert r.exec_result.need_param == "mode"  # 锚点缺失 → 缺参澄清而非默认 extend
    assert calls == []


def test_rule_call_enum_with_anchor_still_executes(monkeypatch):
    calls = []

    def fake_display(**kw):
        calls.append(kw)
        return ExecResult(True, "ok")

    router = _router(StubLocal(), {"set_display_mode": fake_display})
    monkeypatch.setattr(
        router_module,
        "decide_local_intent",
        lambda *a, **k: _rule_plan("set_display_mode", {"mode": "extend"}),
    )
    r = router.handle("把显示器设成扩展模式")
    assert r.stage is Stage.RULE
    assert r.params == {"mode": "extend"}
    assert calls == [{"mode": "extend"}]


def test_router_bound_open_text_guess_escalates(monkeypatch):
    calls = []

    def fake_file_search(**kw):
        calls.append(kw)
        return ExecResult(True, "ok")

    local = StubLocal(
        bound=LocalModelResult(
            action="CALL", tool_name="file_search", params={"query": "python3"}
        )
    )
    router = _router(local, {"file_search": fake_file_search})
    plan = IntentPlan(
        action=IntentAction.TO_MODEL,
        candidates=["file_search"],
        tool_list="file_search(query)",
    )
    monkeypatch.setattr(router_module, "decide_local_intent", lambda *a, **k: plan)
    r = router.handle("搜一下containerd的文档")
    # 原文有 ascii 证据（containerd）→ 走 bound；模型猜 python3 无锚点 → 升云不执行
    assert r.stage is Stage.CLOUD
    assert r.escalate_reason == "hallucinated_params"
    assert calls == []
    assert len(local.bound_calls) == 1
