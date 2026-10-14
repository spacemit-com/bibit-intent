import pytest

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.resources import resource_path
from bibit_intent.routing.rule_engine import RuleEngine


@pytest.mark.parametrize("text,level", [
    ("把音量调到60", 60),
    ("将声音设为75", 75),
    ("把音量调到六成", 60),
    ("把音量调到百分之六十", 60),
])
def test_volume_ba_extracts_explicit_level(text, level):
    rule = RuleEngine(ToolRegistry(resource_path("tools.yaml")))
    result = rule.match(text)
    assert result is not None
    assert result.tool_name == "set_volume"
    assert result.slots == {"level": level}
    assert result.confidence >= rule.CONFIDENCE_THRESHOLD


@pytest.mark.parametrize("text", [
    "把麦克风音量调到60",
    "把输入音量调到60",
    "把话筒声音设为75",
])
def test_volume_ba_does_not_match_input_device(text):
    rule = RuleEngine(ToolRegistry(resource_path("tools.yaml")))
    assert rule.match_tool(text, "set_volume") is None
