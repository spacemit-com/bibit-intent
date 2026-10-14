"""launch/close 窄域标签层（L3）落地测试：确定性抽取 + Router 标签路径。"""

from __future__ import annotations

import pytest

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.executor import Executor
from bibit_intent.execution.system_tools import ExecResult
from bibit_intent.models.local_model import LocalModelResult
from bibit_intent.routing import router as router_module
from bibit_intent.routing.intent import IntentAction, IntentPlan, decide_local_intent
from bibit_intent.routing.router import (
    Router,
    Stage,
    _deterministic_app_name,
)
from bibit_intent.routing.rule_engine import RuleEngine


# ── 确定性抽取（方案 B 落地版）──────────────────────────────────────
# (text, tool_name, expected_name_or_None)
EXTRACT_CASES = [
    ("帮我打开一下记事本", "launch_app", "记事本"),
    ("帮我把浏览器打开", "launch_app", "浏览器"),
    ("帮我把微信启动起来", "launch_app", "微信"),
    ("把在跑的记事本退出去", "close_app", "记事本"),
    ("帮我把视频软件退出一下", "close_app", "视频软件"),
    ("帮我打开一下那个编辑器", "launch_app", "编辑器"),
    ("把正在放的视频软件关一下", "close_app", "视频软件"),
    ("帮我把微信弄出来", "launch_app", "微信"),
    ("打开浏览器", "launch_app", "浏览器"),
    ("启动终端", "launch_app", "终端"),
    ("关闭浏览器", "close_app", "浏览器"),
    ("退出微信", "close_app", "微信"),
    ("帮我启动文件管理器", "launch_app", "文件管理器"),
    ("请打开计算器", "launch_app", "计算器"),
    ("帮我把微信打开一下", "launch_app", "微信"),
    ("把系统设置关掉", "close_app", "系统设置"),
    # 占位/泛化/无动词 → 抽不到，走澄清或升云
    ("帮我把那个应用弄出来", "launch_app", None),
    ("帮我打开一个应用", "launch_app", None),
    ("帮我把那个文件打开", "launch_app", None),
    ("结束那个卡死的应用", "close_app", "卡死的应用"),
    ("帮我退出正在录屏的软件", "close_app", None),
    ("把正在放的视频软件关一下", "close_app", "视频软件"),
    ("打开终端和控制中心", "launch_app", None),
    ("别关了", "close_app", None),
    ("今天天气怎么样", "launch_app", None),
]


@pytest.mark.parametrize("text,tool,expected", EXTRACT_CASES)
def test_deterministic_app_name(text, tool, expected):
    assert _deterministic_app_name(text, tool) == expected


# ── Router 标签层路径 ───────────────────────────────────────────────

class StubLabelLocal:
    def __init__(self, label_result: LocalModelResult):
        self._label = label_result
        self.calls = []

    def is_available(self):
        return True

    def infer_label(self, user_input, max_output_tokens=15):
        self.calls.append(user_input)
        return self._label


def _router_for(handlers, local):
    registry = ToolRegistry()
    return Router(
        registry,
        RuleEngine(registry),
        local,
        Executor(registry, handlers=handlers),
    )


def _plan(candidates, hint=None, hint_source=""):
    return IntentPlan(
        action=IntentAction.TO_MODEL,
        candidates=candidates,
        tool_list="\n".join(candidates),
        hint=hint,
        hint_source=hint_source,
    )


def test_label_launch_direction_with_deterministic_params(monkeypatch):
    captured = {}

    def fake_launch(**kw):
        captured.update(kw)
        return ExecResult(True, f"已启动应用: {kw.get('app_name')}")

    router = _router_for({"launch_app": fake_launch}, StubLabelLocal(
        LocalModelResult(action="CALL", tool_name="launch", raw="CALL launch")))
    monkeypatch.setattr(router_module, "decide_local_intent",
                        lambda *a, **k: _plan(["launch_app"]))

    r = router.handle("帮我把浏览器打开")
    assert r.stage is Stage.LOCAL
    assert r.tool_name == "launch_app"
    assert captured.get("app_name") == "浏览器"
    assert r.debug.get("label_mode") is True


def test_label_close_ba_sentence(monkeypatch):
    captured = {}

    def fake_close(**kw):
        captured.update(kw)
        return ExecResult(True, f"已关闭: {kw.get('app_name')}")

    router = _router_for({"close_app": fake_close}, StubLabelLocal(
        LocalModelResult(action="CALL", tool_name="close", raw="CALL close")))
    monkeypatch.setattr(router_module, "decide_local_intent",
                        lambda *a, **k: _plan(["close_app"]))

    r = router.handle("把在跑的记事本退出去")
    assert r.stage is Stage.LOCAL
    assert r.tool_name == "close_app"
    assert captured.get("app_name") == "记事本"


def test_label_escalate_oos_without_hint_goes_cloud(monkeypatch):
    router = _router_for({}, StubLabelLocal(
        LocalModelResult(action="ESCALATE", reason="out_of_scope",
                         raw="ESCALATE reason=out_of_scope")))
    monkeypatch.setattr(router_module, "decide_local_intent",
                        lambda *a, **k: _plan(["launch_app", "close_app"]))

    r = router.handle("帮我把那个应用弄出来")
    assert r.stage is Stage.CLOUD
    assert r.escalate_reason == "out_of_scope"


def test_label_escalate_single_candidate_deterministic_fallback(monkeypatch):
    captured = {}

    def fake_launch(**kw):
        captured.update(kw)
        return ExecResult(True, "ok")

    router = _router_for({"launch_app": fake_launch}, StubLabelLocal(
        LocalModelResult(action="ESCALATE", reason="out_of_scope",
                         raw="ESCALATE reason=out_of_scope")))
    monkeypatch.setattr(router_module, "decide_local_intent",
                        lambda *a, **k: _plan(["launch_app"], hint="launch_app",
                                              hint_source="agreed"))

    r = router.handle("帮我把浏览器打开")
    assert r.stage is Stage.LOCAL
    assert r.tool_name == "launch_app"
    assert captured.get("app_name") == "浏览器"


def test_label_missing_app_hands_off_to_cloud(monkeypatch):
    captured = {}

    def fake_launch(**kw):
        captured.update(kw)
        return ExecResult(False, "还差一个参数：app_name（应用名）。"
                                "请直接回复应用名，或按「app_name=值」格式补充。",
                          need_param="app_name")

    router = _router_for({"launch_app": fake_launch}, StubLabelLocal(
        LocalModelResult(action="CALL", tool_name="launch", raw="CALL launch")))
    monkeypatch.setattr(router_module, "decide_local_intent",
                        lambda *a, **k: _plan(["launch_app"]))

    r = router.handle("帮我把那个应用弄出来")
    assert r.stage is Stage.CLOUD
    assert r.escalate_reason == "app_target_unresolved"
    assert not captured


def test_label_unresolved_target_hands_off_instead_of_asking(monkeypatch):
    calls = []
    router = _router_for({"launch_app": lambda **params: calls.append(params)}, StubLabelLocal(
        LocalModelResult(action="CALL", tool_name="launch", raw="CALL launch")))
    monkeypatch.setattr(router_module, "decide_local_intent", lambda *args, **kwargs: _plan(["launch_app"]))
    result = router.handle("请替我把终端窗口调出来")
    assert result.stage is Stage.CLOUD
    assert result.escalate_reason == "app_target_unresolved"
    assert calls == []


def test_legacy_stub_without_infer_label_keeps_call_protocol(monkeypatch):
    """旧桩（无 infer_label）不得进入标签层：退化为原 CALL 参数协议。"""
    captured = {}

    class LegacyLocal:
        def is_available(self):
            return True

        def infer(self, user_input, tool_list, analysis=None,
                  max_output_tokens=30, **_kwargs):
            return LocalModelResult(
                action="CALL", tool_name="launch_app",
                params={"app_name": "浏览器"}, raw="CALL launch_app app_name=浏览器")

    def fake_launch(**kw):
        captured.update(kw)
        return ExecResult(True, "ok")

    router = _router_for({"launch_app": fake_launch}, LegacyLocal())
    monkeypatch.setattr(router_module, "decide_local_intent",
                        lambda *a, **k: _plan(["launch_app"]))

    r = router.handle("帮我把浏览器打开")
    assert r.stage is Stage.LOCAL
    assert r.tool_name == "launch_app"
    assert captured.get("app_name") == "浏览器"
    assert r.debug.get("label_mode") is None


def test_label_other_domain_not_triggered(monkeypatch):
    """非 launch/close 候选不触发标签层（如音量 → set_volume）。"""
    calls = []

    class StubVolLocal:
        def is_available(self):
            return True

        def infer_label(self, user_input, max_output_tokens=15):
            calls.append("infer_label")
            return LocalModelResult(action="CALL", tool_name="launch",
                                    raw="CALL launch")

        def infer(self, user_input, tool_list, analysis=None,
                  max_output_tokens=30, **_kwargs):
            return LocalModelResult(action="CALL", tool_name="vol",
                                    params={"level": 60},
                                    raw="CALL vol level=60")

    def fake_vol(**kw):
        return ExecResult(True, "ok")

    router = _router_for({"set_volume": fake_vol}, StubVolLocal())
    monkeypatch.setattr(router_module, "decide_local_intent",
                        lambda *a, **k: _plan(["set_volume"]))
    r = router.handle("把音量调到60")
    assert r.stage is Stage.LOCAL
    assert r.tool_name == "set_volume"
    assert calls == []          # 标签层未被调用


def test_real_decide_no_embed_single_candidate_launch(monkeypatch):
    """真实 decide_local_intent（无 embed）：H445 候选单 launch_app → 标签层。"""
    captured = {}

    def fake_launch(**kw):
        captured.update(kw)
        return ExecResult(True, "ok")

    router = _router_for({"launch_app": fake_launch}, StubLabelLocal(
        LocalModelResult(action="CALL", tool_name="launch", raw="CALL launch")))
    r = router.handle("帮我把浏览器打开")
    assert r.stage is Stage.LOCAL
    assert r.tool_name == "launch_app"
    assert captured.get("app_name") == "浏览器"
    assert r.debug.get("label_mode") is True
