import pytest

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.executor import Executor
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.execution.system_tools import ExecResult
from bibit_intent.resources import resource_path
from bibit_intent.routing.rule_engine import RuleEngine
from bibit_intent.routing.intent import decide_local_intent
from test_ecosystem_install import _load_skill_handlers


@pytest.mark.parametrize("target", ["libfoo*", "^libfoo.*$", "lib(foo|bar)", "libfoo?", "[abc]", "--purge"])
@pytest.mark.parametrize("tool", ["pkg_install", "pkg_remove"])
def test_pattern_never_queries_or_executes(monkeypatch, target, tool):
    module = _load_skill_handlers()
    def forbidden(*args, **kwargs):
        pytest.fail("must not query, confirm or execute")
    monkeypatch.setattr(module, "run_command", forbidden)
    monkeypatch.setattr(module, "_run_sudo", forbidden)
    result = getattr(module, tool)(target)
    assert not result.ok and "完整准确" in result.message
    registry = ToolRegistry(resource_path("tools.yaml"))
    load_skills(resource_path("skills"), registry, {})
    result = Executor(registry, confirm_callback=forbidden, handlers={tool: forbidden}).execute(tool, {"pkg": target})
    assert not result.ok and "未执行" in result.message


def test_executor_accepts_explicit_install_version():
    registry = ToolRegistry(resource_path("tools.yaml"))
    load_skills(resource_path("skills"), registry, {})
    calls = []

    def install(**params):
        calls.append(params)
        return ExecResult(True, "mock installed")

    result = Executor(
        registry,
        confirm_callback=lambda action: True,
        handlers={"pkg_install": install},
    ).execute("pkg_install", {"pkg": "git=1:2.34.1-1"})
    assert result.ok is True
    assert calls == [{"pkg": "git=1:2.34.1-1"}]


@pytest.mark.parametrize("text,target", [("安装libfoo*", "libfoo*"), ("卸载 ^libfoo.*$", "^libfoo.*$"), ("apt install libfoo?", "libfoo?")])
def test_rule_preserves_pattern(text, target):
    registry = ToolRegistry(resource_path("tools.yaml"))
    load_skills(resource_path("skills"), registry, {})
    result = RuleEngine(registry).match(text)
    assert result.slots == {"pkg": target}
    assert result.matched_pattern == "unsupported_package_pattern"


@pytest.mark.parametrize("text", ["把libfoo*卸载", "安装 /tmp/app*.deb", "安装libfoo*", "卸载 ^libfoo.*$"])
def test_intent_does_not_truncate_or_resolve_pattern(text):
    registry = ToolRegistry(resource_path("tools.yaml"))
    load_skills(resource_path("skills"), registry, {})
    def forbidden(*args):
        pytest.fail("must not resolve pattern to a single package")
    plan = decide_local_intent(text, RuleEngine(registry), registry, pkg_resolver=forbidden)
    assert plan.action.value == "rule_call"
    assert "*" in plan.slots["pkg"]
    result = Executor(registry, confirm_callback=forbidden).execute(plan.tool_name, plan.slots)
    assert not result.ok and "未执行" in result.message
