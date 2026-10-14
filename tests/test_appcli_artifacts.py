"""H04：产物校验规则集。

对应 docs/design/cli_anything_plan.md 的 H04 验收标准：exists / 魔数（PDF、ODF zip、
SVG/XML、PNG、MP4）/ size>0 / 可解析；空文件、截断文件、错格式判失败；
**exit 0 但无产物判失败**（上游 drawio 反例固化为回归）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from types import SimpleNamespace

import pytest
import yaml

from bibit_intent.core.appcli_artifacts import ARTIFACT_RULES, verify_artifact
from bibit_intent.core.appcli_prereq import AppCliReadiness
from bibit_intent.core.appcli_registry import VERIFY_RULES
from bibit_intent.resources import resource_path


# ── 规则与注册表必须一一对应 ─────────────────────────────────────────

def test_every_registered_rule_has_an_implementation():
    assert set(ARTIFACT_RULES) == set(VERIFY_RULES)


# ── 真实格式的正例 ───────────────────────────────────────────────────

def _write(path, payload):
    """写入并返回路径——``Path.write_bytes`` 返回的是字节数，不能直接当路径用。"""
    path.write_bytes(payload)
    return path


def _pdf(path):
    path.write_bytes(b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n")
    return path


def _png(path):
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 40 + b"\x00\x00\x00\x00IEND\xaeB`\x82")
    return path


def _zip(path, names):
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in names:
            archive.writestr(name, payload)
    return path


def _mp4(path):
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32)
    return path


@pytest.mark.parametrize("rule,builder", [
    ("pdf_magic", _pdf),
    ("png_magic", _png),
    ("mp4_magic", _mp4),
    ("office_zip_magic", lambda p: _zip(p, [("content.xml", b"<x/>")])),
    ("odf_zip_magic", lambda p: _zip(p, [("mimetype", b"application/vnd.oasis.opendocument.text")])),
    ("svg_magic", lambda p: _write(p, b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg"/>')),
    ("xml_wellformed", lambda p: _write(p, b"<root><child/></root>")),
    ("exists", lambda p: _write(p, b"x")),
    ("size>0", lambda p: _write(p, b"x")),
])
def test_rule_accepts_a_real_file(tmp_path, rule, builder):
    path = builder(tmp_path / "artifact.bin")
    verdict = verify_artifact(path, [rule])
    assert verdict.ok, verdict.summary()


# ── 空文件 / 错格式 / 截断 ───────────────────────────────────────────

def test_empty_file_fails_even_though_it_exists(tmp_path):
    path = tmp_path / "empty.pdf"
    path.write_bytes(b"")
    verdict = verify_artifact(path, ["exists", "size>0", "pdf_magic"])
    assert not verdict.ok
    assert {item.rule for item in verdict.failed} == {"size>0", "pdf_magic"}


def test_wrong_format_fails_magic_check(tmp_path):
    path = tmp_path / "actually-text.pdf"
    path.write_bytes(b"this is not a pdf at all")
    verdict = verify_artifact(path, ["exists", "size>0", "pdf_magic"])
    assert not verdict.ok
    assert [item.rule for item in verdict.failed] == ["pdf_magic"]


@pytest.mark.parametrize("rule,builder", [
    # 截断：头对了，尾不对
    ("pdf_magic", lambda p: _write(p, b"%PDF-1.7\n1 0 obj\n")),
    ("png_magic", lambda p: _write(p, b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)),
    ("jpeg_magic", lambda p: _write(p, b"\xff\xd8\xff\xe0" + b"\x00" * 20)),
])
def test_truncated_file_fails(tmp_path, rule, builder):
    path = builder(tmp_path / "cut.bin")
    verdict = verify_artifact(path, [rule])
    assert not verdict.ok
    assert "截断" in verdict.failed[0].evidence


def test_truncated_zip_fails(tmp_path):
    path = _zip(tmp_path / "doc.odt", [("mimetype", b"application/vnd.oasis.opendocument.text")])
    payload = path.read_bytes()
    path.write_bytes(payload[: len(payload) // 2])
    verdict = verify_artifact(path, ["odf_zip_magic"])
    assert not verdict.ok


def test_zip_without_mimetype_is_not_odf(tmp_path):
    path = _zip(tmp_path / "doc.odt", [("content.xml", b"<x/>")])
    verdict = verify_artifact(path, ["odf_zip_magic"])
    assert not verdict.ok
    assert "mimetype" in verdict.failed[0].evidence


def test_missing_file_reports_dependents_as_not_run(tmp_path):
    verdict = verify_artifact(tmp_path / "nope.pdf", ["exists", "size>0", "pdf_magic"])
    assert not verdict.ok
    assert verdict.failed[0].rule == "exists"
    assert all("未执行" in item.evidence for item in verdict.failed[1:])


def test_unparseable_xml_fails(tmp_path):
    path = tmp_path / "broken.svg"
    path.write_bytes(b"<svg><unclosed></svg>")
    assert not verify_artifact(path, ["xml_wellformed"]).ok


def test_summary_reports_size_on_success_and_reason_on_failure(tmp_path):
    good = _pdf(tmp_path / "ok.pdf")
    assert "B）" in verify_artifact(good, ["exists", "size>0"]).summary()
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"nope")
    assert "校验未通过" in verify_artifact(bad, ["pdf_magic"]).summary()


# ── 接进 appcli_run：drawio 反例回归 ────────────────────────────────

def _load_handlers():
    path = resource_path("skills") / "appcli" / "handlers.py"
    spec = importlib.util.spec_from_file_location("_test_appcli_artifacts_handlers", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _registry_document():
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
                        "argv": ["export", "render", "{output}"],
                        "params": {"output": {"type": "str", "path": True}},
                        "artifacts": [
                            {"param": "output", "verify": ["exists", "size>0", "pdf_magic"]}
                        ],
                    }
                ],
            }
        ],
    }


@pytest.fixture
def handlers(tmp_path, monkeypatch):
    module = _load_handlers()
    path = tmp_path / "appcli_registry.yaml"
    path.write_text(
        yaml.safe_dump(_registry_document(), allow_unicode=True), encoding="utf-8"
    )
    original = module.AppCliRegistry.load
    monkeypatch.setattr(
        module.AppCliRegistry, "load",
        classmethod(lambda cls, _path=None: original(path)),
    )
    monkeypatch.setattr(module, "check_readiness", lambda spec: AppCliReadiness(True))
    monkeypatch.setenv("BIBIT_INTENT_APPCLI_ROOT", str(tmp_path / "workspace"))
    return module


def _run_with_stdout(handlers, monkeypatch, stdout, *, returncode=0):
    monkeypatch.setattr(
        handlers, "run_bounded_command",
        lambda command, *, timeout, max_output_bytes, env=None: SimpleNamespace(
            returncode=returncode, stdout=stdout, stderr=""
        ),
    )
    return handlers.appcli_run(appcli="demo", action="render", args=["output=out.pdf"])


def test_exit_zero_without_artifact_is_a_failure(handlers, monkeypatch):
    """drawio 反例：缺后端时 export render 返回 exit 0 却不产出文件。"""
    result = _run_with_stdout(
        handlers, monkeypatch, json.dumps({"action": "export_fallback", "note": "backend missing"})
    )
    assert not result.ok
    assert "产物校验未通过" in result.message
    assert "exists: 文件不存在" in result.detail


def test_exit_zero_with_a_corrupt_artifact_is_a_failure(handlers, monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "out.pdf").write_bytes(b"not really a pdf")
    result = _run_with_stdout(handlers, monkeypatch, json.dumps({"ok": True}))
    assert not result.ok
    assert "pdf_magic" in result.detail


def test_exit_zero_with_a_valid_artifact_passes_and_reports_evidence(handlers, monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    pdf = workspace / "out.pdf"
    pdf.write_bytes(b"%PDF-1.7\ntrailer\n%%EOF\n")
    result = _run_with_stdout(handlers, monkeypatch, json.dumps({"ok": True}))
    assert result.ok, result.message
    assert "已生成产物并通过校验" in result.message
    assert f"({pdf.stat().st_size} B)" in result.detail or "B）" in result.detail
    assert "校验：通过" in result.detail
    assert "pdf_magic: PDF 头尾完整" in result.detail


# ── K02：按格式判容器（ODF 精确 mimetype / OOXML 主部件）────────────────
#
# 设备实测（K3/riscv64，soffice 26.2.1.2）产出的 odt/ods/docx/xlsx/pptx 都能过，
# 而「换个后缀的 ZIP」不能——这正是 office_zip_magic（只验 ZIP 可读）缺的那一层。

def _odf(path, mime):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", mime)
        archive.writestr("content.xml", "<x/>")
    return path


def _ooxml(path, part):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(part, "<x/>")
    return path


@pytest.mark.parametrize("rule,builder", [
    ("odt_magic", lambda p: _odf(p, "application/vnd.oasis.opendocument.text")),
    ("ods_magic", lambda p: _odf(p, "application/vnd.oasis.opendocument.spreadsheet")),
    ("docx_magic", lambda p: _ooxml(p, "word/document.xml")),
    ("xlsx_magic", lambda p: _ooxml(p, "xl/workbook.xml")),
    ("pptx_magic", lambda p: _ooxml(p, "ppt/presentation.xml")),
])
def test_container_rule_accepts_the_right_document(tmp_path, rule, builder):
    path = builder(tmp_path / "artifact.bin")
    verdict = verify_artifact(path, ["exists", "size>0", rule])
    assert verdict.ok, verdict.summary()


def test_odf_rule_refuses_the_other_odf_flavour(tmp_path):
    """表格与文字只差 mimetype —— 只验「是 ZIP」会放过换后缀的文件。"""
    path = _odf(tmp_path / "actually-spreadsheet.odt", "application/vnd.oasis.opendocument.spreadsheet")
    verdict = verify_artifact(path, ["odt_magic"])
    assert not verdict.ok
    assert "spreadsheet" in verdict.failed[0].evidence


def test_ooxml_rule_requires_the_main_part(tmp_path):
    """ZIP + [Content_Types].xml 但没有主部件，不算那种文档。"""
    path = _ooxml(tmp_path / "empty.docx", "docProps/app.xml")
    verdict = verify_artifact(path, ["docx_magic"])
    assert not verdict.ok
    assert "word/document.xml" in verdict.failed[0].evidence


def test_ooxml_rule_refuses_a_plain_zip(tmp_path):
    """普通 ZIP（没有 [Content_Types].xml）不能冒充 Office 文档。"""
    path = _zip(tmp_path / "archive.docx", [("readme.txt", b"hi")])
    verdict = verify_artifact(path, ["docx_magic"])
    assert not verdict.ok
    assert "[Content_Types].xml" in verdict.failed[0].evidence
