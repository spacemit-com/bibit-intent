import re

import pytest

from bibit_intent.analysis.backends import JiebaAnalyzer, SpacyAnalyzer
from bibit_intent.analysis.lexical import instruction_search
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.core.session import SessionState
from bibit_intent.execution.executor import Executor
from bibit_intent.routing.complexity import classify_complexity, Complexity
from bibit_intent.routing.router import Router, Stage
from bibit_intent.routing.rule_engine import RuleEngine


@pytest.mark.parametrize("confirm", [False, True])
@pytest.mark.parametrize("text", [
    "60，如果电量充足", "如果电量充足就设为60", "60 provided there is enough power",
    "60，然后frobnicate数据", "60 frobnicate", "open Firefox and frobnicate data",
    "60，前提是电量充足", "60 and restart the service", "60" + "啊" * 5000,
])
def test_pending_never_executes_partial_reply(confirm, text):
    registry = ToolRegistry()

    class ForbiddenLocal:
        def is_available(self):
            pytest.fail("unsafe reply reached local model")

    class ForbiddenExecutor(Executor):
        def execute(self, *args, **kwargs):
            pytest.fail("unsafe reply reached executor")

    router = Router(registry, RuleEngine(registry), ForbiddenLocal(), ForbiddenExecutor(registry))
    session = SessionState()
    if confirm:
        session.set_pending_confirm("set_volume", {"level": 40}, "level", "音量", "确认？")
    else:
        session.set_pending("set_volume", "level", "音量", {}, param_type="int")
    result = router.handle(text, session)
    assert result.stage is Stage.CLOUD
    assert session.pending is None


@pytest.mark.parametrize("text,value", [("60", 60), ("调到70", 70), ("音量调到80", 80), ("level=55", 55)])
def test_complete_numeric_reply_still_fills(text, value):
    session = SessionState()
    session.set_pending("set_volume", "level", "音量", {}, param_type="int")
    assert session.resolve(text) == ("fill", value)


@pytest.mark.parametrize("text", ["on=yes frobnicate", "on=unknown", "yes frobnicate"])
def test_boolean_reply_requires_complete_value(text):
    session = SessionState()
    session.set_pending("toggle", "on", "开关", {}, param_type="bool")
    assert session.resolve(text)[0] != "fill"


@pytest.mark.parametrize("text", [
    "安装curl，前提是磁盘空间足够", "install curl provided there is enough disk space",
    "install curl only when there is enough space", "open Firefox and frobnicate data",
    "frobnicate 123",
])
def test_missing_structure_is_now_complex(text):
    assert classify_complexity(text).complexity is Complexity.COMPLEX


def test_literal_cannot_supply_instruction_but_can_supply_operand():
    pattern = re.compile(r"安装(?P<pkg>.+)")
    assert instruction_search(pattern, '把文件"安装并删除.txt"打开') is None
    pattern = re.compile(r"安装\s+(?P<path>.+)")
    assert instruction_search(pattern, "安装 /tmp/app.deb")["path"] == "/tmp/app.deb"


def test_legacy_backend_cannot_load_a_model():
    assert JiebaAnalyzer().analyze("安装curl，前提是磁盘空间足够").is_conditional
    with pytest.raises(ValueError, match="Model-backed analysis is unsupported"):
        SpacyAnalyzer()


@pytest.mark.parametrize("text,action", [("确认", "confirm"), ("取消", "cancel"), ("60", "fill")])
def test_confirmation_still_accepts_complete_replies(text, action):
    session = SessionState()
    session.set_pending_confirm("set_volume", {"level": 40}, "level", "音量", "确认？")
    assert session.resolve_confirm(text)[0] == action


def test_float_value_remains_numeric():
    session = SessionState()
    session.set_pending("scale", "ratio", "比例", {}, param_type="float", validator=r"\d+(?:\.\d+)?")
    assert session.resolve("1") == ("fill", 1.0)
    assert session.resolve("0.5") == ("fill", 0.5)


def test_quoted_filename_cannot_match_package_install():
    from bibit_intent.execution.skill_loader import load_skills
    from bibit_intent.resources import resource_path

    registry = ToolRegistry()
    load_skills(resource_path("skills"), registry, {})
    rule = RuleEngine(registry)
    text = '把文件"安装并删除.txt"打开'
    assert rule.match_tool(text, "pkg_install") is None
    assert "pkg_install" not in rule.keyword_candidates(text)


@pytest.mark.parametrize("confirm", [False, True])
def test_missing_dictionary_cancels_pending(monkeypatch, confirm):
    from bibit_intent.analysis import lexical

    def unavailable():
        raise OSError("missing dictionary")

    monkeypatch.setattr(lexical, "dictionaries", unavailable)
    registry = ToolRegistry()
    router = Router(registry, RuleEngine(registry), None, Executor(registry))
    session = SessionState()
    if confirm:
        session.set_pending_confirm("set_volume", {"level": 40}, "level", "音量", "确认？")
    else:
        session.set_pending("set_volume", "level", "音量", {}, param_type="int")
    result = router.handle("60", session)
    assert result.stage is Stage.CLOUD
    assert result.escalate_reason == "lexicon_unavailable"
    assert session.pending is None
