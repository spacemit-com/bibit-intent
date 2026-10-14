"""core/doc_templates.py — 内置办公模板（声明式骨架 → appcli 动作计划）

设计与取舍见 ``docs/design/k3_smart_office.md`` §4：模板**不是** .docx/.odt 模板
文件（riscv64 上要额外渲染器，也无法表达「先检索再落笔」这类流程），而是一份
声明式的骨架：章节顺序、每节写什么、要几张表、导出什么格式、值得先查哪些知识源。
``template_get`` 把它渲染成**登记过的 appcli 动作序列**交给模型，执行与产物校验
仍走既有闭环（``core/appcli_registry`` + ``core/appcli_artifacts``）。

本模块是纯只读的资源加载与计划渲染：
- 加载期严格校验（未知字段即错误），坏模板不让进工具层；
- 渲染出的计划里，**内容占位符**（如 ``{period}``）保持原样交给模型填写，
  **路径占位符**统一是 ``<project>`` / ``<output>``，由调用方替换成工作区内路径。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from bibit_intent.resources import resource_path

__all__ = [
    "DOC_TYPES",
    "PROJECT_PLACEHOLDER",
    "OUTPUT_PLACEHOLDER",
    "DocTemplate",
    "DocTemplateError",
    "load_templates",
    "template_plan",
]

#: 模板面向的文档类型（与 appcli ``doc_new`` 的 doc_type 枚举一致）。
DOC_TYPES = ("writer", "calc", "impress")

#: 计划里的路径占位符：调用方按 ``core.appcli_execution.appcli_root()`` 替换。
PROJECT_PLACEHOLDER = "<project>"
OUTPUT_PLACEHOLDER = "<output>"

#: 导出预设 → 已登记的导出动作。只有 pdf 走遗留的 ``export_render``（规则层的
#: slot_map 指向它），其余每种格式各有自己的动作与校验规则（K02）。
_EXPORT_ACTIONS = {"pdf": "export_render"}

#: 章节种类 → writer 动作。列表之外的 kind 在加载期即被拒绝。
_WRITER_ACTIONS = {
    "heading": "writer_add_heading",
    "paragraph": "writer_add_paragraph",
    "list": "writer_add_list",
    "table": "writer_add_table",
    "page_break": "writer_add_page_break",
}


class DocTemplateError(ValueError):
    """模板加载或使用失败。``code`` 供工具层映射成稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def __str__(self) -> str:  # pragma: no cover - 便于日志阅读
        return f"[{self.code}] {super().__str__()}"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TemplateInput(_StrictModel):
    key: str
    label: str
    example: str = ""
    #: 用户没给这项信息时用的默认值——「先出草稿、不追问」靠它落地。
    fallback: str = ""


class TemplateReference(_StrictModel):
    """借鉴的开源项目：写在模板里是为了让「为什么这样排」可追溯。"""

    project: str
    url: str = ""
    license: str = ""
    what: str = ""


class TemplateKnowledge(_StrictModel):
    local: list[str] = Field(default_factory=list)
    cloud: list[str] = Field(default_factory=list)


class TemplateExport(_StrictModel):
    default: str
    allowed: list[str]


class Section(_StrictModel):
    kind: str
    level: int = 0
    text: str = ""
    style: str = ""
    items: list[str] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    rows: int = 0
    guidance: str = ""

    @model_validator(mode="after")
    def _check(self) -> "Section":
        if self.kind not in _WRITER_ACTIONS:
            raise ValueError(
                f"未知章节种类 {self.kind!r}；可用：{', '.join(sorted(_WRITER_ACTIONS))}"
            )
        if self.kind == "heading" and not 1 <= self.level <= 10:
            raise ValueError("heading 的 level 必须是 1–10")
        if self.kind == "list" and self.style not in ("bullet", "number"):
            raise ValueError("list 的 style 必须是 bullet 或 number")
        if self.kind == "table":
            if not self.columns:
                raise ValueError("table 必须给出 columns")
            if not 1 <= self.rows <= 50:
                raise ValueError("table 的 rows 必须在 1–50")
        return self


class Sheet(_StrictModel):
    name: str
    header: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    guidance: str = ""
    columns_width: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "Sheet":
        if not self.name.strip():
            raise ValueError("工作表名不能为空")
        width = len(self.header)
        for row in self.rows:
            if len(row) != width:
                raise ValueError(
                    f"工作表 {self.name!r} 的行有 {len(row)} 列，表头是 {width} 列"
                )
        return self


class Slide(_StrictModel):
    title: str
    bullets: list[str] = Field(default_factory=list)
    guidance: str = ""


class DocTemplate(_StrictModel):
    id: str
    version: int = 1
    doc_type: str
    name: str
    name_en: str = ""
    when_to_use: str = ""
    when_to_use_en: str = ""
    deliverable: list[str] = Field(default_factory=list)
    ask_at_most: int = 2
    inputs: list[TemplateInput] = Field(default_factory=list)
    knowledge: TemplateKnowledge = Field(default_factory=TemplateKnowledge)
    sections: list[Section] = Field(default_factory=list)
    sheets: list[Sheet] = Field(default_factory=list)
    slides: list[Slide] = Field(default_factory=list)
    export: TemplateExport
    references: list[TemplateReference] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "DocTemplate":
        if self.doc_type not in DOC_TYPES:
            raise ValueError(f"doc_type 必须是 {DOC_TYPES} 之一")
        if not self.id.replace("_", "").isalnum():
            raise ValueError("id 只能是字母、数字与下划线")
        required = {
            "writer": bool(self.sections),
            "calc": bool(self.sheets),
            "impress": bool(self.slides),
        }[self.doc_type]
        if not required:
            raise ValueError(
                f"doc_type={self.doc_type} 的模板必须给出 "
                f"{'sections' if self.doc_type == 'writer' else 'sheets' if self.doc_type == 'calc' else 'slides'}"
            )
        if self.export.default not in self.export.allowed:
            raise ValueError("export.default 必须在 export.allowed 里")
        if not 0 <= self.ask_at_most <= 5:
            raise ValueError("ask_at_most 必须在 0–5")
        keys = [item.key for item in self.inputs]
        if len(set(keys)) != len(keys):
            raise ValueError(f"inputs 的 key 重复：{keys}")
        return self


def _load_file(path: Path) -> DocTemplate:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise DocTemplateError("template_invalid", f"{path.name} 读取失败：{exc}") from exc
    if not isinstance(raw, dict):
        raise DocTemplateError("template_invalid", f"{path.name} 顶层必须是映射")
    try:
        template = DocTemplate.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first.get("loc", ())) or "(根)"
        raise DocTemplateError(
            "template_invalid", f"{path.name} 字段 {where}：{first.get('msg')}"
        ) from exc
    if template.id != path.stem:
        raise DocTemplateError(
            "template_invalid", f"{path.name} 的 id={template.id!r} 与文件名不一致"
        )
    return template


def load_templates(directory: str | Path | None = None) -> dict[str, DocTemplate]:
    """加载目录下全部 ``*.yaml``；id 与文件名必须一致，重复即报错。"""
    base = Path(directory) if directory is not None else resource_path("templates")
    if not base.is_dir():
        raise DocTemplateError("template_invalid", f"模板目录不存在：{base}")
    templates: dict[str, DocTemplate] = {}
    for path in sorted(base.glob("*.yaml")):
        template = _load_file(path)
        if template.id in templates:
            raise DocTemplateError("template_invalid", f"模板 id 重复：{template.id}")
        templates[template.id] = template
    if not templates:
        raise DocTemplateError("template_invalid", f"模板目录里没有模板：{base}")
    return templates


def _cell_type(value: str) -> str:
    """数字落成 float 单元格，其余按字符串——纯文本表格不会算错数。"""
    text = str(value or "").strip()
    if not text:
        return "string"
    try:
        float(text.replace(",", ""))
    except ValueError:
        return "string"
    return "float"


def _cell_ref(column: int, row: int) -> str:
    """0 基的行列 → A1 引用（本模板最多用到 26 列，够用且不必处理 AA）。"""
    if not 0 <= column < 26:
        raise DocTemplateError("template_invalid", f"列数超出模板支持范围：{column + 1}")
    return f"{chr(ord('A') + column)}{row}"


def template_plan(
    template: DocTemplate,
    *,
    preset: str = "",
    project: str = PROJECT_PLACEHOLDER,
    output: str = OUTPUT_PLACEHOLDER,
) -> list[dict[str, Any]]:
    """把模板渲染成 appcli 动作序列（writer/calc/impress 各一套）。

    ``preset`` 为空时用模板的 ``export.default``；不在 ``export.allowed`` 里则报错
    ——只放行设备上验收过的格式（见设计方案 §2.3）。
    """
    target = (preset or template.export.default).strip()
    if target not in template.export.allowed:
        raise DocTemplateError(
            "template_invalid",
            f"模板 {template.id} 不支持导出 {target!r}；可用：{', '.join(template.export.allowed)}",
        )
    steps: list[dict[str, Any]] = [
        {"action": "doc_new", "args": {"doc_type": template.doc_type, "project": project}}
    ]

    if template.doc_type == "writer":
        for index, section in enumerate(template.sections):
            if section.kind == "heading":
                steps.append(
                    {
                        "action": "writer_add_heading",
                        "args": {"project": project, "text": section.text, "level": section.level},
                    }
                )
            elif section.kind == "paragraph":
                steps.append(
                    {
                        "action": "writer_add_paragraph",
                        "args": {"project": project, "text": section.text},
                    }
                )
            elif section.kind == "list":
                steps.append(
                    {
                        "action": "writer_add_list",
                        "args": {
                            "project": project,
                            "items": list(section.items),
                            "style": section.style or "bullet",
                        },
                    }
                )
            elif section.kind == "table":
                steps.append(
                    {
                        "action": "writer_add_table",
                        "args": {
                            "project": project,
                            "rows": section.rows,
                            "cols": len(section.columns),
                        },
                    }
                )
            elif section.kind == "page_break":
                steps.append({"action": "writer_add_page_break", "args": {"project": project}})
            # 段落/表格之后的文字填充靠后续 writer_set_text（内容由模型决定），
            # 这里不改写计划：模板只保证结构，内容交给模型。
            del index
    elif template.doc_type == "calc":
        for index, sheet in enumerate(template.sheets):
            if index == 0:
                steps.append(
                    {
                        "action": "calc_rename_sheet",
                        "args": {"project": project, "index": 0, "name": sheet.name},
                    }
                )
            else:
                steps.append(
                    {"action": "calc_add_sheet", "args": {"project": project, "name": sheet.name}}
                )
            for column, title in enumerate(sheet.header):
                steps.append(
                    {
                        "action": "calc_set_cell",
                        "args": {
                            "project": project,
                            "ref": _cell_ref(column, 1),
                            "value": title,
                            "cell_type": "string",
                            "sheet": index,
                        },
                    }
                )
            for offset, row in enumerate(sheet.rows):
                for column, value in enumerate(row):
                    steps.append(
                        {
                            "action": "calc_set_cell",
                            "args": {
                                "project": project,
                                "ref": _cell_ref(column, offset + 2),
                                "value": value,
                                "cell_type": _cell_type(value),
                                "sheet": index,
                            },
                        }
                    )
    else:  # impress
        for slide in template.slides:
            steps.append(
                {
                    "action": "impress_add_slide",
                    "args": {
                        "project": project,
                        "title": slide.title,
                        # 值里不允许换行（注册表口径），要点用中文分号连接。
                        "content": "；".join(slide.bullets),
                    },
                }
            )

    steps.append(
        {
            "action": _EXPORT_ACTIONS.get(target, f"export_{target}"),
            "args": {"project": project, "output": output},
        }
    )
    return steps


def placeholders(template: DocTemplate) -> list[str]:
    """模板文本里出现过的 ``{name}`` 占位符（模型需要填的就是这些）。"""
    import re

    found: dict[str, None] = {}
    for text in _template_texts(template):
        for name in re.findall(r"\{([a-z0-9_]+)\}", text):
            found.setdefault(name, None)
    return list(found)


def _template_texts(template: DocTemplate) -> list[str]:
    texts: list[str] = []
    for section in template.sections:
        texts.append(section.text)
        texts.extend(section.items)
    for sheet in template.sheets:
        texts.extend(sheet.header)
        for row in sheet.rows:
            texts.extend(row)
    for slide in template.slides:
        texts.append(slide.title)
        texts.extend(slide.bullets)
    return texts


def dump(template: DocTemplate) -> dict[str, Any]:
    """工具层用的完整模板视图（含引用与计划），字段名对模型友好。"""
    return {
        "id": template.id,
        "doc_type": template.doc_type,
        "name": template.name,
        "name_en": template.name_en,
        "when_to_use": template.when_to_use,
        "when_to_use_en": template.when_to_use_en,
        "deliverable": list(template.deliverable),
        "ask_at_most": template.ask_at_most,
        "inputs": [item.model_dump() for item in template.inputs],
        "knowledge": template.knowledge.model_dump(),
        "references": [item.model_dump() for item in template.references],
        "export": template.export.model_dump(),
        "placeholders": placeholders(template),
        "sections": [item.model_dump() for item in template.sections],
        "sheets": [item.model_dump() for item in template.sheets],
        "slides": [item.model_dump() for item in template.slides],
    }
