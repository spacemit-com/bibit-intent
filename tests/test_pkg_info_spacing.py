import pytest

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.resources import resource_path
from bibit_intent.routing.intent import decide_local_intent
from bibit_intent.routing.rule_engine import RuleEngine


@pytest.mark.parametrize("text,package", [
    ("查看软件包 bash 的信息", "bash"),
    ("查看软件包 bash 信息", "bash"),
    ("查看软件包python3-pip的详情", "python3-pip"),
    ("显示软件包 libc6 的版本", "libc6"),
])
def test_package_metadata_query_stays_local(text, package):
    registry = ToolRegistry(resource_path("tools.yaml"))
    load_skills(resource_path("skills"), registry, {})
    plan = decide_local_intent(text, RuleEngine(registry), registry, embed=None)
    assert plan.action.value == "rule_call"
    assert plan.tool_name == "pkg_info"
    assert plan.slots["pkg"] == package
