"""H05：只读的 appcli_verify 工具。

对应 docs/design/cli_anything_plan.md 的 H05 验收标准：对指定产物逐条跑校验规则并
给出证据（格式/字节数/路径）；文件不存在时要说清是「未产出」还是「路径错」。

与 appcli_run 的边界（本文件用用例固定）：
- 不执行任何命令，因此不受 ``riscv64: verified`` 白名单与前置齐备度约束——白名单管的是
  执行，复核一个未验收 appcli 的产物是合理需求；
- 只读、免确认，但**不是**无条件通过：动作没声明产物规则、路径越界、文件没写出来
  都必须失败，且失败原因互不混淆。
"""
from __future__ import annotations

import importlib.util
import sys

import pytest
import yaml

from bibit_intent.core.appcli_registry import AppCliRegistry
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.resources import resource_path

#: 与 test_appcli_run 同因：换注册表必须从原始 classmethod 出发重新绑定。
_ORIGINAL_LOAD = AppCliRegistry.load

_VALID_PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n"
_VALID_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16 + b"IEND\xaeB`\x82"


def _load_handlers():
    path = resource_path("skills") / "appcli" / "handlers.py"
    spec = importlib.util.spec_from_file_location("_test_appcli_verify_handlers", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def handlers(tmp_path, monkeypatch):
    """真实注册表（libreoffice 未验收、channel=host）+ 临时工作区。"""
    module = _load_handlers()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("BIBIT_INTENT_APPCLI_ROOT", str(workspace))
    monkeypatch.setattr(
        module,
        "check_readiness",
        lambda spec: pytest.fail("appcli_verify 不应查询前置状态：它不执行命令"),
    )
    module.workspace = workspace  # 便于用例定位工作区
    return module


def _bind_registry(monkeypatch, module, document, tmp_path):
    path = tmp_path / "custom_registry.yaml"
    path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(
        module.AppCliRegistry,
        "load",
        classmethod(lambda cls, _path=None: _ORIGINAL_LOAD(path)),
    )


def _two_artifact_document():
    return {
        "version": 1,
        "appclies": [
            {
                "name": "multi",
                "display": "Multi",
                "entry_point": "cli-anything-multi",
                "channel": "artifact-only",
                "riscv64": "unverified",
                "actions": [
                    {
                        "name": "export_render",
                        "argv": ["export", "render", "{output}", "{poster}"],
                        "params": {
                            "output": {"type": "str", "path": True},
                            "poster": {"type": "str", "path": True},
                        },
                        "artifacts": [
                            {"param": "output", "verify": ["exists", "pdf_magic"]},
                            {"param": "poster", "verify": ["exists", "png_magic"]},
                        ],
                    }
                ],
            }
        ],
    }


# ── Skill / 工具接线 ─────────────────────────────────────────────────

def test_tool_is_registered_as_readonly_and_needs_no_confirmation():
    registry = ToolRegistry(resource_path("tools.yaml"))
    handlers: dict = {}
    load_skills(resource_path("skills"), registry, handlers)

    tool = registry.get("appcli_verify")
    assert tool.mcp_group() == "readonly"
    assert not tool.system.get("confirm")
    assert "appcli_verify" in handlers


def test_skill_declares_all_three_tools():
    registry = ToolRegistry(resource_path("tools.yaml"))
    loaded = load_skills(resource_path("skills"), registry, {})

    skill = next(item for item in loaded if item.name == "appcli")
    assert skill.tool_names == ["appcli_list", "appcli_run", "appcli_verify"]


# ── 通过 ─────────────────────────────────────────────────────────────

def test_passing_artifact_reports_path_and_per_rule_evidence(handlers):
    handlers.workspace.joinpath("report.pdf").write_bytes(_VALID_PDF)

    result = handlers.appcli_verify(appcli="libreoffice", action="export_render", path="report.pdf")

    assert result.ok
    assert result.message == f"校验通过：{handlers.workspace / 'report.pdf'}"
    assert f"文件：{handlers.workspace / 'report.pdf'}" in result.detail
    assert "（按 libreoffice/export_render 的 3 条规则校验）" in result.detail
    assert "exists: 文件存在" in result.detail
    assert "size>0: " in result.detail
    assert "pdf_magic: PDF 头尾完整" in result.detail


def test_absolute_path_inside_workspace_is_accepted(handlers):
    target = handlers.workspace / "nested" / "out.pdf"
    target.parent.mkdir()
    target.write_bytes(_VALID_PDF)

    result = handlers.appcli_verify(
        appcli="libreoffice", action="export_render", path=str(target)
    )
    assert result.ok


# ── 失败：产物不合格 ─────────────────────────────────────────────────

def test_truncated_pdf_fails_with_reason_and_evidence(handlers):
    handlers.workspace.joinpath("half.pdf").write_bytes(b"%PDF-1.4\ntrailer but no end marker\n")

    result = handlers.appcli_verify(appcli="libreoffice", action="export_render", path="half.pdf")

    assert not result.ok
    assert result.message.startswith("校验未通过：")
    assert "尾部没有 %%EOF（文件可能被截断）" in result.detail
    # 未通过时也要给出全部证据，而不只是失败项
    assert "exists: 文件存在" in result.detail


def test_wrong_format_fails_on_magic_rule(handlers):
    handlers.workspace.joinpath("actually.png").write_bytes(_VALID_PNG)

    result = handlers.appcli_verify(
        appcli="libreoffice", action="export_render", path="actually.png"
    )
    assert not result.ok
    assert "缺少 %PDF- 头（不是 PDF）" in result.detail


# ── 失败：路径错 vs 未产出 ───────────────────────────────────────────

def test_missing_file_inside_workspace_is_reported_as_not_produced(handlers):
    result = handlers.appcli_verify(appcli="libreoffice", action="export_render", path="nope.pdf")

    assert not result.ok
    assert result.message.startswith("未产出：")
    assert "路径在工作区内是合法的" in result.message
    assert "路径错误" not in result.message


def test_path_outside_workspace_is_reported_as_invalid_path(handlers):
    result = handlers.appcli_verify(
        appcli="libreoffice", action="export_render", path="/etc/passwd"
    )

    assert not result.ok
    assert result.message.startswith("路径错误：")
    assert "未产出" not in result.message


def test_relative_escape_is_rejected_as_invalid_path(handlers):
    outside = handlers.workspace.parent / "escape.pdf"
    outside.write_bytes(_VALID_PDF)

    result = handlers.appcli_verify(
        appcli="libreoffice", action="export_render", path="../escape.pdf"
    )
    assert not result.ok
    assert result.message.startswith("路径错误：")


def test_empty_path_is_rejected(handlers):
    result = handlers.appcli_verify(appcli="libreoffice", action="export_render", path="")
    assert not result.ok
    assert "路径错误" in result.message


# ── 失败：动作/注册表/参数不成立 ─────────────────────────────────────

def test_action_without_artifact_rules_fails_instead_of_passing(handlers):
    result = handlers.appcli_verify(appcli="libreoffice", action="doc_info", path="project")

    assert not result.ok
    assert "该动作未声明产物校验规则：libreoffice/doc_info" in result.message


def test_unknown_appcli_and_action_are_reported_not_raised(handlers):
    unknown_appcli = handlers.appcli_verify(appcli="nope", action="export_render", path="a.pdf")
    assert not unknown_appcli.ok
    assert "未登记的应用" in unknown_appcli.message

    unknown_action = handlers.appcli_verify(appcli="libreoffice", action="nope", path="a.pdf")
    assert not unknown_action.ok
    assert "没有动作" in unknown_action.message


def test_registry_failure_is_reported_not_raised(handlers, monkeypatch):
    from bibit_intent.core.appcli_registry import AppCliRegistryError

    def broken(*args, **kwargs):
        raise AppCliRegistryError("registry_unavailable", "appcli 注册表不存在：/nope")

    monkeypatch.setattr(AppCliRegistry, "load", broken)
    result = handlers.appcli_verify(appcli="libreoffice", action="export_render", path="a.pdf")

    assert not result.ok
    assert "无法校验" in result.message
    assert "registry_unavailable" in result.message


# ── 未验收 / 缺前置也允许复核（白名单只管执行） ──────────────────────

def test_unverified_appcli_artifact_can_still_be_verified(handlers):
    registry = AppCliRegistry.load()
    spec = registry.require("drawio")
    assert not spec.verified, "本用例假定 drawio 没有进白名单（riscv64 unsupported）"

    handlers.workspace.joinpath("diagram.png").write_bytes(_VALID_PNG)
    result = handlers.appcli_verify(
        appcli="drawio", action="export_render", path="diagram.png"
    )

    # 未进白名单 + 前置未装，都不阻止复核一个已有文件（白名单只管执行）。
    assert result.ok


# ── 多产物参数：必须显式指明 artifact ────────────────────────────────

def test_multiple_artifact_params_require_explicit_artifact(handlers, monkeypatch, tmp_path):
    _bind_registry(monkeypatch, handlers, _two_artifact_document(), tmp_path)
    handlers.workspace.joinpath("poster.png").write_bytes(_VALID_PNG)

    result = handlers.appcli_verify(appcli="multi", action="export_render", path="poster.png")

    assert not result.ok
    assert "声明了多个产物参数（output, poster）" in result.message


def test_artifact_param_selects_its_own_rules(handlers, monkeypatch, tmp_path):
    _bind_registry(monkeypatch, handlers, _two_artifact_document(), tmp_path)
    handlers.workspace.joinpath("poster.png").write_bytes(_VALID_PNG)

    as_pdf = handlers.appcli_verify(
        appcli="multi", action="export_render", path="poster.png", artifact="output"
    )
    assert not as_pdf.ok
    assert "缺少 %PDF- 头" in as_pdf.detail

    as_png = handlers.appcli_verify(
        appcli="multi", action="export_render", path="poster.png", artifact="poster"
    )
    assert as_png.ok
    assert "（按 multi/export_render 的 2 条规则校验）" in as_png.detail


def test_unknown_artifact_param_lists_the_available_ones(handlers, monkeypatch, tmp_path):
    _bind_registry(monkeypatch, handlers, _two_artifact_document(), tmp_path)

    result = handlers.appcli_verify(
        appcli="multi", action="export_render", path="x.pdf", artifact="nope"
    )
    assert not result.ok
    assert "没有产物参数 'nope'" in result.message
    assert "output, poster" in result.message


# ── 英文输出 ─────────────────────────────────────────────────────────

def test_english_language_returns_english_message(handlers):
    handlers.workspace.joinpath("report.pdf").write_bytes(_VALID_PDF)

    passed = handlers.appcli_verify(
        appcli="libreoffice", action="export_render", path="report.pdf", language="en-US"
    )
    assert passed.message.startswith("Verification passed: ")

    failed = handlers.appcli_verify(
        appcli="libreoffice", action="export_render", path="missing.pdf", language="en-US"
    )
    assert failed.message.startswith("Not produced: ")
