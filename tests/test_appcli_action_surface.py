"""K01–K03：libreoffice 动作面扩充、按格式拆分的导出动作、列表型参数。

对应 docs/design/k3_smart_office.md §2：真实会话里模型想加表格/列表/单元格/幻灯片，
注册表里都没有，于是 `appcli_run` 零调用。这里把「登记了哪些动作、argv 长什么样、
导出动作各自要求什么校验规则」钉住；argv 逐字对齐设备上 `cli-anything-libreoffice --help`
的输出（K3 实测，见设计方案 §2.2）。
"""
from __future__ import annotations

import textwrap

import pytest

from bibit_intent.core.appcli_artifacts import ARTIFACT_RULES
from bibit_intent.core.appcli_registry import (
    MAX_LIST_ITEMS,
    VERIFY_RULES,
    AppCliRegistry,
    AppCliRegistryError,
    load_appcli_registry,
)


@pytest.fixture(scope="module")
def registry() -> AppCliRegistry:
    return load_appcli_registry()


@pytest.fixture(scope="module")
def office(registry):
    return registry.get("libreoffice")


# ── 动作面 ───────────────────────────────────────────────────────────

def test_office_exposes_the_document_actions_the_model_needs(office):
    names = {action.name for action in office.actions}
    expected = {
        "doc_new",
        "writer_add_heading",
        "writer_add_paragraph",
        "writer_add_list",
        "writer_add_table",
        "writer_add_page_break",
        "writer_set_text",
        "writer_remove",
        "writer_list",
        "calc_set_cell",
        "calc_get_cell",
        "calc_add_sheet",
        "calc_rename_sheet",
        "calc_remove_sheet",
        "calc_list_sheets",
        "impress_add_slide",
        "impress_set_content",
        "impress_list_slides",
        "impress_remove_slide",
        "style_apply",
        "style_list",
        "document_json",
        "doc_info",
        "export_presets",
    }
    assert expected <= names, expected - names


@pytest.mark.parametrize("action,argv", [
    ("writer_add_table", ["--project", "{project}", "writer", "add-table", "-r", "{rows}", "-c", "{cols}"]),
    ("writer_add_page_break", ["--project", "{project}", "writer", "add-page-break"]),
    ("writer_set_text", ["--project", "{project}", "writer", "set-text", "{index}", "{text}"]),
    ("calc_add_sheet", ["--project", "{project}", "calc", "add-sheet", "-n", "{name}"]),
    ("calc_rename_sheet", ["--project", "{project}", "calc", "rename-sheet", "{index}", "{name}"]),
    ("impress_add_slide", ["--project", "{project}", "impress", "add-slide", "-t", "{title}", "-c", "{content}"]),
    ("style_apply", ["--project", "{project}", "style", "apply", "{style}", "{content_index}"]),
])
def test_argv_matches_the_device_cli(office, action, argv):
    assert office.action(action).argv == argv


def test_readonly_actions_are_marked_and_produce_nothing(office):
    readonly = {a.name for a in office.actions if a.readonly}
    assert {"writer_list", "calc_get_cell", "calc_list_sheets", "impress_list_slides",
            "style_list", "document_json", "doc_info", "export_presets"} <= readonly
    for name in readonly:
        assert office.action(name).artifacts == [], name


# ── 按格式拆分的导出动作（K02）────────────────────────────────────────

@pytest.mark.parametrize("action,rule,preset", [
    ("export_render", "pdf_magic", "pdf"),
    ("export_docx", "docx_magic", "docx"),
    ("export_odt", "odt_magic", "odt"),
    ("export_xlsx", "xlsx_magic", "xlsx"),
    ("export_ods", "ods_magic", "ods"),
    ("export_pptx", "pptx_magic", "pptx"),
])
def test_each_export_action_carries_its_own_verification(office, action, rule, preset):
    spec = office.action(action)
    # 新增的导出动作把格式写死在 argv 里；遗留的 export_render 仍走 {preset} 占位符
    assert preset in spec.argv or "{preset}" in spec.argv
    artifacts = spec.artifacts
    assert len(artifacts) == 1
    assert artifacts[0].param == "output"
    assert artifacts[0].verify == ["exists", "size>0", rule]
    assert rule in VERIFY_RULES and rule in ARTIFACT_RULES


def test_export_actions_only_take_paths(office):
    for name in ("export_docx", "export_odt", "export_xlsx", "export_ods", "export_pptx"):
        params = office.action(name).params
        assert set(params) == {"project", "output"}
        assert all(param.path for param in params.values())


# ── 列表型参数（K03）────────────────────────────────────────────────

def test_list_param_renders_repeated_flags(office):
    argv = office.render_argv(
        "writer_add_list", {"project": "p.json", "items": ["第一条", "第二条"]}
    )
    assert argv == [
        "--project", "p.json", "writer", "add-list",
        "-i", "第一条", "-i", "第二条", "--style", "bullet",
    ]


def test_list_param_accepts_separator_and_json_forms(office):
    args = {"project": "p.json"}
    assert office.render_argv("writer_add_list", {**args, "items": "甲|乙"}) == [
        "--project", "p.json", "writer", "add-list", "-i", "甲", "-i", "乙", "--style", "bullet",
    ]
    assert office.render_argv("writer_add_list", {**args, "items": '["甲","乙"]'}) == [
        "--project", "p.json", "writer", "add-list", "-i", "甲", "-i", "乙", "--style", "bullet",
    ]


def test_list_param_style_enum_is_enforced(office):
    with pytest.raises(AppCliRegistryError, match="bullet, number"):
        office.render_argv(
            "writer_add_list", {"project": "p.json", "items": ["x"], "style": "dash"}
        )


@pytest.mark.parametrize("items,message", [
    ([], "至少要有一项"),
    ([""], "不能为空"),
    (["ok", "bad\nline"], "换行"),
    ([str(i) for i in range(MAX_LIST_ITEMS + 1)], "最多"),
])
def test_list_param_rejects_bad_values(office, items, message):
    with pytest.raises(AppCliRegistryError, match=message):
        office.render_argv("writer_add_list", {"project": "p.json", "items": items})


def test_list_param_is_required(office):
    with pytest.raises(AppCliRegistryError, match="缺少必填参数 'items'"):
        office.render_argv("writer_add_list", {"project": "p.json"})


# ── 注册表对列表参数的约束 ───────────────────────────────────────────

def _registry_yaml(tmp_path, argv: str, param_type: str = "list"):
    path = tmp_path / "reg.yaml"
    path.write_text(
        textwrap.dedent(
            f"""
            version: 1
            appclies:
              - name: demo
                display: 演示
                entry_point: demo-cli
                category: office
                riscv64: verified
                channel: artifact-only
                actions:
                  - name: tags
                    argv: {argv}
                    params:
                      tags: {{type: {param_type}, default: []}}
            """
        ),
        encoding="utf-8",
    )
    return path


def test_list_param_must_be_written_as_a_repeated_flag(tmp_path):
    path = _registry_yaml(tmp_path, '["--tags", "{tags}"]')
    with pytest.raises(AppCliRegistryError, match="没写成"):
        load_appcli_registry(path)


def test_list_param_needs_a_literal_flag_in_front(tmp_path):
    path = _registry_yaml(tmp_path, '["{tags...}"]')
    with pytest.raises(AppCliRegistryError, match="前一个词必须是字面开关"):
        load_appcli_registry(path)


def test_repeat_syntax_is_rejected_for_scalar_params(tmp_path):
    path = _registry_yaml(tmp_path, '["--tags", "{tags...}"]', param_type="str")
    with pytest.raises(AppCliRegistryError, match="不是列表参数"):
        load_appcli_registry(path)


def test_unknown_param_type_is_rejected(tmp_path):
    path = _registry_yaml(tmp_path, '["--tags", "{tags...}"]', param_type="tuple")
    with pytest.raises(AppCliRegistryError, match="不在"):
        load_appcli_registry(path)
