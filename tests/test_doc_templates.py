"""K04：内置办公模板的资源加载与动作计划渲染。

对应 docs/design/k3_smart_office.md §4：模板是**声明式骨架**（不是 .docx 模板
文件），加载期严格校验，渲染出的计划只使用登记过的 appcli 动作，导出格式只放行
模板自己声明过的（设备上验收过的）那几种。
"""
from __future__ import annotations

import textwrap

import pytest

from bibit_intent.core.doc_templates import (
    OUTPUT_PLACEHOLDER,
    PROJECT_PLACEHOLDER,
    DocTemplateError,
    load_templates,
    placeholders,
    template_plan,
)


@pytest.fixture(scope="module")
def templates():
    return load_templates()


# ── 资源与 schema ────────────────────────────────────────────────────

def test_ships_five_templates_covering_all_doc_types(templates):
    assert set(templates) == {
        "weekly_report",
        "meeting_minutes",
        "project_plan",
        "data_report",
        "presentation",
    }
    assert {tpl.doc_type for tpl in templates.values()} == {"writer", "calc", "impress"}


def test_every_template_cites_its_open_source_reference(templates):
    """模板声称「参考开源项目」，就必须写清项目、链接与许可证。"""
    for template in templates.values():
        assert template.references, f"{template.id} 没有列出参考项目"
        for ref in template.references:
            assert ref.project and ref.url.startswith("https://")
            assert ref.license and ref.what


def test_every_template_declares_inputs_with_fallbacks_and_export(templates):
    for template in templates.values():
        assert template.ask_at_most <= 2, f"{template.id} 会追问太多"
        assert template.inputs, f"{template.id} 没有声明最少输入"
        assert template.export.default in template.export.allowed
        assert template.export.allowed, f"{template.id} 没有声明可导出格式"


def test_unknown_field_is_rejected(tmp_path):
    (tmp_path / "bad.yaml").write_text(
        textwrap.dedent(
            """
            id: bad
            doc_type: writer
            name: 坏模板
            export: {default: pdf, allowed: [pdf]}
            surprise: true
            sections:
              - {kind: heading, level: 1, text: 标题}
            """
        ),
        encoding="utf-8",
    )
    with pytest.raises(DocTemplateError) as excinfo:
        load_templates(tmp_path)
    assert excinfo.value.code == "template_invalid"
    assert "surprise" in str(excinfo.value)


def test_id_must_match_file_name(tmp_path):
    (tmp_path / "other.yaml").write_text(
        "id: bad\ndoc_type: writer\nname: x\nexport: {default: pdf, allowed: [pdf]}\n"
        "sections: [{kind: heading, level: 1, text: t}]\n",
        encoding="utf-8",
    )
    with pytest.raises(DocTemplateError, match="与文件名不一致"):
        load_templates(tmp_path)


def test_sheet_rows_must_match_header_width(tmp_path):
    (tmp_path / "bad.yaml").write_text(
        "id: bad\ndoc_type: calc\nname: x\nexport: {default: xlsx, allowed: [xlsx]}\n"
        "sheets: [{name: S, header: [a, b], rows: [[\"only-one\"]]}]\n",
        encoding="utf-8",
    )
    with pytest.raises(DocTemplateError) as excinfo:
        load_templates(tmp_path)
    assert "列" in str(excinfo.value)


def test_writer_template_without_sections_is_rejected(tmp_path):
    (tmp_path / "bad.yaml").write_text(
        "id: bad\ndoc_type: writer\nname: x\nexport: {default: pdf, allowed: [pdf]}\n",
        encoding="utf-8",
    )
    with pytest.raises(DocTemplateError, match="必须给出 sections"):
        load_templates(tmp_path)


# ── 计划渲染 ─────────────────────────────────────────────────────────

def test_writer_plan_uses_only_registered_actions(templates):
    template = templates["weekly_report"]
    plan = template_plan(template)
    actions = [step["action"] for step in plan]
    assert actions[0] == "doc_new"
    assert plan[0]["args"] == {"doc_type": "writer", "project": PROJECT_PLACEHOLDER}
    assert "writer_add_heading" in actions
    assert "writer_add_list" in actions
    assert actions[-1] == "export_render"  # pdf 走遗留动作，见 _EXPORT_ACTIONS
    assert plan[-1]["args"]["output"] == OUTPUT_PLACEHOLDER


def test_writer_plan_keeps_content_placeholders_for_the_model(templates):
    plan = template_plan(templates["weekly_report"])
    assert plan[1]["args"]["text"] == "{period} 工作汇报"
    assert {key for key in placeholders(templates["weekly_report"])} >= {"period", "owner"}


def test_list_section_renders_items_and_style(templates):
    plan = template_plan(templates["meeting_minutes"])
    lists = [step for step in plan if step["action"] == "writer_add_list"]
    assert lists and all(step["args"]["style"] in ("bullet", "number") for step in lists)
    assert all(isinstance(step["args"]["items"], list) for step in lists)


def test_writer_templates_avoid_tables_because_cells_cannot_be_filled(templates):
    """内置 Writer 模板一律不用表格。

    上游 appcli 只有 ``writer add-table``（建空表），没有填格动作，``set-text``
    对 ``table`` 直接报错（K3 实测）。用了表格就会交付一张空表，真实会话里模型还会
    为此翻了十几个工具调用去找「怎么填」。需要表格的场景走 Calc 模板
    （``project_plan`` / ``data_report``），它们的单元格是能写的。
    """
    for template in templates.values():
        if template.doc_type != "writer":
            continue
        kinds = {section.kind for section in template.sections}
        assert "table" not in kinds, f"{template.id} 用了无法填内容的 Writer 表格"


def test_table_section_still_renders_for_templates_that_want_an_empty_grid(tmp_path):
    """schema 仍然支持 table（有人就要一张空表手填），只是内置模板不用。"""
    (tmp_path / "grid.yaml").write_text(
        textwrap.dedent(
            """
            id: grid
            doc_type: writer
            name: 空表格
            export: {default: pdf, allowed: [pdf]}
            sections:
              - {kind: heading, level: 1, text: 表}
              - {kind: table, columns: [a, b, c, d], rows: 3}
            """
        ),
        encoding="utf-8",
    )
    plan = template_plan(load_templates(tmp_path)["grid"])
    table = next(step for step in plan if step["action"] == "writer_add_table")
    assert table["args"] == {"project": PROJECT_PLACEHOLDER, "rows": 3, "cols": 4}


def test_calc_plan_renames_first_sheet_and_uses_a1_refs(templates):
    plan = template_plan(templates["project_plan"])
    actions = [step["action"] for step in plan]
    assert actions[:2] == ["doc_new", "calc_rename_sheet"]
    assert plan[1]["args"] == {"project": PROJECT_PLACEHOLDER, "index": 0, "name": "计划"}
    assert actions.count("calc_add_sheet") == 1  # 第二张表才需要新增
    cells = [step for step in plan if step["action"] == "calc_set_cell"]
    assert cells[0]["args"]["ref"] == "A1"
    assert cells[0]["args"]["sheet"] == 0
    # 数字（进度 0）落成 float 单元格，文本落成 string
    progress = next(step for step in cells if step["args"]["ref"] == "F2")
    assert progress["args"]["cell_type"] == "float"
    assert next(step for step in cells if step["args"]["ref"] == "A2")["args"]["cell_type"] == "string"


def test_impress_plan_joins_bullets_into_content(templates):
    plan = template_plan(templates["presentation"])
    slides = [step for step in plan if step["action"] == "impress_add_slide"]
    assert slides
    cover = slides[0]
    assert cover["args"]["title"] == "{topic}"
    # 值里不允许换行（注册表口径），要点用分号连接
    assert cover["args"]["content"] == "汇报人：{owner}；{date}"


def test_export_preset_override_is_limited_to_declared_formats(templates):
    plan = template_plan(templates["weekly_report"], preset="docx")
    assert plan[-1]["action"] == "export_docx"
    with pytest.raises(DocTemplateError, match="不支持导出"):
        template_plan(templates["weekly_report"], preset="xlsx")


def test_plan_only_uses_registered_appcli_actions(templates):
    """模板的每一步都必须是注册表里真有的动作。

    这条守卫抓的是「模板写了一个不存在的动作名」这类静默失效——例如 pdf 预设
    指向没有登记的 ``export_pdf``，模型照计划跑就会一路失败。
    """
    from bibit_intent.core.appcli_registry import load_appcli_registry

    registered = {action.name for action in load_appcli_registry().get("libreoffice").actions}
    for template in templates.values():
        for preset in template.export.allowed:
            plan = template_plan(template, preset=preset)
            unknown = {step["action"] for step in plan} - registered
            assert not unknown, f"{template.id}/{preset} 用了未登记的动作：{unknown}"
