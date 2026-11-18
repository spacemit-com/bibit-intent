"""templates Skill（办公模板，只读）的执行实现。

两个工具都以「模板是资源、计划是数据」为口径：

- ``template_list``：这台设备内置了哪些办公模板、各自适合什么请求；
- ``template_get``：某个模板的骨架 + 最少输入（带 fallback）+ 值得先查的知识源 +
  一串待执行的 appcli 动作（``<project>``/``<output>`` 由调用方替换成工作区路径）。

**不执行任何命令、不写文件**：真正的执行走 ``appcli_run``（需确认 + 产物校验），
这样模板层无法绕过既有白名单与校验闭环。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from bibit_intent.core.doc_templates import (
    OUTPUT_PLACEHOLDER,
    PROJECT_PLACEHOLDER,
    DocTemplateError,
    DocTemplate,
    dump,
    load_templates,
    template_plan,
)


@dataclass
class ExecResult:
    """与 tools.system.ExecResult 字段对齐（执行层会做归一化）。"""

    ok: bool
    message: str
    detail: str = ""
    need_param: str | None = None
    escalate: str | None = None


def _reply(zh: str, en: str, language: str | None = None) -> str:
    return en if str(language or "").lower().startswith("en") else zh


#: 知识源的说人话版本（模板里只写 id，回复里要能看懂）。
_KNOWLEDGE_LABELS = {
    "file_search": ("本地文件检索（file_search → 读正文再落笔）", "local file search (file_search, then read the file)"),
    "web_search": ("网络搜索（Runtime web_search）", "web search (Runtime web_search)"),
}

_EXPORT_LABELS = {
    "pdf": "PDF",
    "docx": "Word (.docx)",
    "odt": "ODF 文本文档 (.odt)",
    "xlsx": "Excel (.xlsx)",
    "ods": "ODF 表格 (.ods)",
    "pptx": "PowerPoint (.pptx)",
}


def _load(language: str | None):
    try:
        return load_templates(), None
    except DocTemplateError as exc:
        return None, ExecResult(
            False,
            _reply(
                f"模板资源不可用：{exc}",
                f"Template resources are unavailable: {exc}",
                language,
            ),
        )


def _doc_type_label(doc_type: str, language: str | None) -> str:
    zh = {"writer": "文档", "calc": "表格", "impress": "演示"}
    en = {"writer": "document", "calc": "spreadsheet", "impress": "slides"}
    return (en if str(language or "").lower().startswith("en") else zh).get(doc_type, doc_type)


def template_list(doc_type: str = "", language: str | None = None, **_) -> ExecResult:
    """列出内置办公模板（只读）。``doc_type`` 非空时按 writer/calc/impress 过滤。"""
    templates, error = _load(language)
    if error:
        return error
    wanted = (doc_type or "").strip().lower()
    if wanted and wanted not in ("writer", "calc", "impress"):
        return ExecResult(
            False,
            _reply(
                f"参数错误：doc_type 只能是 writer/calc/impress，收到 {doc_type!r}",
                f"Invalid parameter: doc_type must be writer/calc/impress, got {doc_type!r}",
                language,
            ),
        )
    items = [tpl for tpl in templates.values() if not wanted or tpl.doc_type == wanted]
    if not items:
        return ExecResult(
            False,
            _reply(
                f"没有 doc_type={wanted} 的模板。",
                f"No template with doc_type={wanted}.",
                language,
            ),
        )
    en = str(language or "").lower().startswith("en")
    lines = [
        _reply(
            f"内置办公模板 {len(items)} 个（用 template_get 取骨架与动作计划）：",
            f"{len(items)} built-in office templates (use template_get for the skeleton and action plan):",
            language,
        )
    ]
    for tpl in items:
        name = tpl.name_en if en and tpl.name_en else tpl.name
        when = tpl.when_to_use_en if en and tpl.when_to_use_en else tpl.when_to_use
        deliverable = "、".join(
            _EXPORT_LABELS.get(item, item) for item in tpl.deliverable
        )
        lines.append(
            f"- {tpl.id}｜{name}｜{_doc_type_label(tpl.doc_type, language)}｜"
            f"{_reply('交付', 'deliverable', language)}：{deliverable}"
        )
        if when:
            lines.append(f"  {_reply('适用', 'use when', language)}：{when}")
    return ExecResult(True, "\n".join(lines))


def _knowledge_lines(template: DocTemplate, language: str | None) -> list[str]:
    en = str(language or "").lower().startswith("en")
    lines: list[str] = []
    for source in template.knowledge.local:
        label = _KNOWLEDGE_LABELS.get(source)
        lines.append(
            _reply("本地：", "local: ", language)
            + (label[1] if en else label[0] if label else source)
        )
    for source in template.knowledge.cloud:
        label = _KNOWLEDGE_LABELS.get(source)
        lines.append(
            _reply("云端：", "cloud: ", language)
            + (label[1] if en else label[0] if label else source)
        )
    if not lines:
        lines.append(_reply("无需外部来源。", "No external source needed.", language))
    return lines


def template_get(
    template: str = "", preset: str = "", language: str | None = None, **_
) -> ExecResult:
    """取一个模板的骨架与动作计划（只读）。

    ``preset`` 留空用模板默认导出格式；只放行该模板声明过的格式。
    """
    templates, error = _load(language)
    if error:
        return error
    name = (template or "").strip()
    if not name:
        known = "、".join(templates)
        return ExecResult(
            False,
            _reply(
                f"缺少模板名。可用模板：{known}",
                f"Missing template name. Available: {known}",
                language,
            ),
            need_param="template",
        )
    tpl = templates.get(name)
    if tpl is None:
        known = "、".join(templates)
        return ExecResult(
            False,
            _reply(
                f"没有名为 {name!r} 的模板。可用模板：{known}",
                f"No template named {name!r}. Available: {known}",
                language,
            ),
        )
    try:
        steps = template_plan(tpl, preset=preset)
    except DocTemplateError as exc:
        return ExecResult(
            False,
            _reply(
                f"参数错误：{exc}。可用格式：{'、'.join(tpl.export.allowed)}",
                f"Invalid parameter: {exc}. Allowed: {'、'.join(tpl.export.allowed)}",
                language,
            ),
        )

    payload = dump(tpl)
    # 只读回包约定（见 pipeline.py::_format_reply）：带这组键，执行层就只回
    # message，不会把整份计划 JSON 拼进用户看到的回答。
    payload["ok"] = True
    payload["read_only"] = True
    payload["export"]["target"] = (preset or tpl.export.default).strip()
    payload["plan"] = steps
    payload["plan_note"] = {
        "project": f"{PROJECT_PLACEHOLDER} 替换成工作区内的工程 JSON 路径",
        "output": f"{OUTPUT_PLACEHOLDER} 替换成工作区内的产物路径（扩展名与导出格式一致）",
        "execute_with": "appcli_run（appcli=libreoffice，action/args 见 plan）",
        "unknown_state": "模板里的空 items/空单元格是示例位，由你按真实内容填写或省略",
    }
    en = str(language or "").lower().startswith("en")
    header = [
        _reply(f"模板 {tpl.id}｜{tpl.name}", f"Template {tpl.id} | {tpl.name_en or tpl.name}", language),
        _reply(
            f"适用：{tpl.when_to_use}",
            f"Use when: {tpl.when_to_use_en or tpl.when_to_use}",
            language,
        ),
        _reply(
            f"导出：默认 {payload['export']['target']}，可选 {'、'.join(tpl.export.allowed)}"
            f"；动作 {len(steps)} 步（计划见 detail.plan）",
            f"Export: default {payload['export']['target']}, allowed {'、'.join(tpl.export.allowed)};"
            f" {len(steps)} action steps (see detail.plan)",
            language,
        ),
        _reply(
            f"先出草稿：最多问 {tpl.ask_at_most} 个问题，缺的信息用 fallback/TODO 占位",
            f"Draft first: ask at most {tpl.ask_at_most} questions; use fallbacks/TODO for what is missing",
            language,
        ),
    ]
    fill = "、".join("{" + key + "}" for key in payload["placeholders"]) or _reply(
        "无", "none", language
    )
    header.append(_reply(f"需要填写：{fill}", f"Fill in: {fill}", language))
    knowledge = _knowledge_lines(tpl, language)
    header.append(_reply("知识源：", "Knowledge:", language) + "；".join(knowledge))
    return ExecResult(True, "\n".join(header), detail=json.dumps(payload, ensure_ascii=False))


HANDLERS = {
    "template_list": template_list,
    "template_get": template_get,
}
