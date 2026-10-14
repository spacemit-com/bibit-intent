"""H09：路由、意图分工与路径快捷参数。

对应 docs/design/cli_anything_plan.md 的 H09 验收标准：登记 3～5 个高价值动作规则，
「打开 X」仍走 `launch_app`、「产出物」走 `appcli_run`，两条路径互不抢占。

为什么需要 `project`/`output` 快捷参数：规则层用正则命名组接住用户说的文件路径，
而 ``args`` 是列表参数、命名组抽不了它（见 routing/rule_engine.py::_extract_slots）。
登记表约定渲染类动作的源工程参数叫 ``project``、目的文件叫 ``output``，快捷参数就是
这两个名字的同名入口，等价于 args 里的对应项。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from types import SimpleNamespace

import pytest
import yaml

from bibit_intent.core.appcli_prereq import AppCliReadiness
from bibit_intent.core.appcli_registry import AppCliRegistry
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.resources import resource_path
from bibit_intent.routing.rule_engine import RuleEngine

_ORIGINAL_LOAD = AppCliRegistry.load


@pytest.fixture(scope="module")
def rule() -> RuleEngine:
    registry = ToolRegistry(resource_path("tools.yaml"))
    load_skills(resource_path("skills"), registry, {})
    return RuleEngine(registry)


def _match(rule: RuleEngine, text: str) -> tuple[str, dict]:
    found = rule.match(text)
    assert found is not None, f"{text!r} 没有命中任何工具"
    return found.tool_name, found.slots


# ── 两条路径互不抢占 ────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text",
    ["打开终端", "打开微信", "打开 gimp", "启动计算器", "open terminal"],
)
def test_opening_an_application_still_goes_to_launch_app(rule, text):
    tool_name, slots = _match(rule, text)
    assert tool_name == "launch_app", f"{text!r} 被 {tool_name} 抢走了"
    assert slots.get("app_name")


@pytest.mark.parametrize(
    "text",
    [
        "把 report.docx 导出成 out.pdf",
        "把 arch.xml 导出成 out.png",
        "看看 report.docx 的文档信息",
        "列出导出预设",
    ],
)
def test_artifact_requests_go_to_appcli_run(rule, text):
    tool_name, _ = _match(rule, text)
    assert tool_name == "appcli_run", f"{text!r} 被 {tool_name} 抢走了"


def test_verification_prompt_goes_to_the_readonly_verify_tool(rule):
    """H13 的 Hook 用这条 prompt 复核产物，短语必须原样命中只读工具。"""
    tool_name, slots = _match(rule, "校验产出物 libreoffice export_render build/out.pdf")

    assert tool_name == "appcli_verify"
    assert slots == {
        "appcli": "libreoffice",
        "action": "export_render",
        "path": "build/out.pdf",
    }


# ── 规则层把 appcli/动作/路径都定死 ────────────────────────────────

def test_pdf_export_rule_pins_appcli_action_and_paths(rule):
    tool_name, slots = _match(rule, "把 report.docx 导出成 out.pdf")

    assert tool_name == "appcli_run"
    assert slots == {
        "project": "report.docx",
        "output": "out.pdf",
        "appcli": "libreoffice",
        "action": "export_render",
    }


def test_png_export_rule_chooses_the_diagram_appcli(rule):
    _, slots = _match(rule, "把 arch.xml 导出成 out.png")
    assert slots["appcli"] == "drawio"
    assert slots["action"] == "export_render"
    assert slots["output"].endswith(".png")


def test_english_export_rule(rule):
    _, slots = _match(rule, "export report.docx to out.pdf")
    assert slots["appcli"] == "libreoffice"
    assert slots["project"] == "report.docx"


def test_read_only_rules_pin_the_read_only_actions(rule):
    _, doc = _match(rule, "看看 report.docx 的文档信息")
    assert (doc["appcli"], doc["action"]) == ("libreoffice", "doc_info")

    _, diagram = _match(rule, "看看 arch.xml 的工程信息")
    assert (diagram["appcli"], diagram["action"]) == ("drawio", "project_info")

    _, presets = _match(rule, "列出导出预设")
    assert (presets["appcli"], presets["action"]) == ("libreoffice", "export_presets")


def test_bare_pdf_keyword_still_prefers_appcli_run(rule):
    """「生成pdf」这类没有文件信息的说法只定动作，不编造路径。"""
    tool_name, slots = _match(rule, "生成pdf")
    assert tool_name == "appcli_run"
    assert (slots["appcli"], slots["action"]) == ("libreoffice", "export_render")
    assert "project" not in slots and "output" not in slots


# ── 快捷参数进入 args ───────────────────────────────────────────────

def _load_handlers():
    path = resource_path("skills") / "appcli" / "handlers.py"
    spec = importlib.util.spec_from_file_location("_test_appcli_routing_handlers", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _document():
    return {
        "version": 1,
        "appclies": [
            {
                "name": "demo",
                "display": "Demo",
                "entry_point": "cli-anything-demo",
                "channel": "artifact-only",
                "riscv64": "verified",
                "actions": [
                    {
                        "name": "render",
                        "argv": [
                            "--project", "{project}", "export", "render", "{output}", "-p", "{preset}",
                        ],
                        "params": {
                            "project": {"type": "str", "path": True},
                            "output": {"type": "str", "path": True},
                            "preset": {"type": "str", "default": "pdf"},
                        },
                    },
                    {"name": "peek", "argv": ["peek"], "params": {}, "readonly": True},
                ],
            }
        ],
    }


@pytest.fixture
def handlers(tmp_path, monkeypatch):
    module = _load_handlers()
    path = tmp_path / "appcli_registry.yaml"
    path.write_text(yaml.safe_dump(_document(), allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(
        module.AppCliRegistry,
        "load",
        classmethod(lambda cls, _path=None: _ORIGINAL_LOAD(path)),
    )
    monkeypatch.setattr(module, "check_readiness", lambda spec: AppCliReadiness(True))
    monkeypatch.setenv("BIBIT_INTENT_APPCLI_ROOT", str(tmp_path / "workspace"))

    calls: list[list[str]] = []

    def fake_run(command, *, timeout, max_output_bytes, env=None):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True}), stderr="")

    monkeypatch.setattr(module, "run_bounded_command", fake_run)
    module.calls = calls
    module.root = tmp_path / "workspace"
    return module


def test_path_shortcuts_produce_the_same_command_as_args(handlers):
    with_aliases = handlers.appcli_run(
        appcli="demo", action="render", project="p.json", output="o.pdf"
    )
    alias_call = list(handlers.calls)
    handlers.calls.clear()

    with_args = handlers.appcli_run(
        appcli="demo", action="render", args=["project=p.json", "output=o.pdf"]
    )

    assert alias_call == handlers.calls
    assert alias_call, "快捷参数必须真的执行到命令行"
    assert str(handlers.root / "p.json") in alias_call[0]
    assert str(handlers.root / "o.pdf") in alias_call[0]
    # 两条入口的命令行与结果口径必须完全一致（该动作未声明产物规则，因此判成功）。
    assert with_aliases.ok and with_args.ok
    assert with_aliases.message == with_args.message


def test_shortcut_for_an_action_without_that_param_is_rejected(handlers):
    result = handlers.appcli_run(appcli="demo", action="peek", project="p.json")

    assert not result.ok
    assert "没有参数 'project'" in result.message
    assert "可用参数：(无)" in result.message


def test_shortcut_and_args_conflict_is_rejected(handlers):
    result = handlers.appcli_run(
        appcli="demo", action="render", args=["project=a.json"], project="b.json"
    )

    assert not result.ok
    assert "不能同时用 args 与快捷参数传" in result.message


def test_rule_without_paths_fails_instead_of_inventing_files(handlers):
    result = handlers.appcli_run(appcli="demo", action="render")

    assert not result.ok
    # 缺哪个必填参数由注册表报（参数顺序由 YAML 决定），关键是「报错而不是编路径」。
    assert "缺少必填参数" in result.message
    assert "demo/render" in result.message
