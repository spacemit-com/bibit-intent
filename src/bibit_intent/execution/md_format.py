"""
execution/md_format.py — 把结构化事实渲染成 Markdown 表格

原先 system_tools.py 与 skills/ubuntu_cmd/handlers.py 各有一份 _md_table，
再加 USB/WiFi/蓝牙后会有五份副本，统一收敛到这里。

约定：表格放 ExecResult.detail，message 只留一句话结论（pipeline._format_reply
会拼 message + detail，两边都写会打印两遍）；单元格里的 | 转义成 \\|。
"""
from __future__ import annotations

import re


def code_block(text: str, limit: int | None = 4000) -> str:
    """保留正文空白，用更长围栏隔离原文；预览截断提示不混入代码。"""
    body = text if limit is None else text[:max(0, limit)]
    fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", body)), default=0))
    separator = "" if not body or body.endswith(("\n", "\r")) else "\n"
    block = f"{fence}text\n{body}{separator}{fence}"
    if len(body) < len(text):
        block += f"\n\n…(截断，显示 {len(body)} / {len(text)} 字符)"
    return block


def render_detail(detail: str, detail_format: str = "markdown") -> str:
    """原始文本只在展示边界包围栏，既有 Markdown 保持原样。"""
    if detail_format == "text":
        return code_block(detail, limit=None) if detail else ""
    return detail.strip()


def md_table(headers: list[str], rows: list[list[str]]) -> str:
    """把结构化行渲染成 Markdown 表格（单元格里的 | 转义为 \\|）。"""

    def cell(value: object) -> str:
        return ("" if value is None else str(value)).replace("|", "\\|")

    lines = [
        "| " + " | ".join(cell(h) for h in headers) + " |",
        "|" + "---|" * len(headers),
    ]
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    return "\n".join(lines)
