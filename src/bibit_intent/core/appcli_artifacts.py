"""core/appcli_artifacts.py — 应用产物校验规则集

设计口径（docs/design/cli_anything_integration.md §5.2 要点 5）：**exit 0 不等于成功**。
上游 drawio 在缺后端时 ``export render`` 返回 exit 0 却什么都不产出，所以产物必须以
「文件本身」为准来判定，规则表如下：

============= ==========================================================
规则           判据
============= ==========================================================
``exists``     存在且是普通文件
``size>0``     非空
``pdf_magic``  ``%PDF-`` 头 + 尾部 ``%%EOF``（缺尾部视为截断）
``png_magic``  PNG 签名 + 尾部 ``IEND``
``jpeg_magic`` ``\\xff\\xd8\\xff`` 头 + ``\\xff\\xd9`` 尾
``mp4_magic``  第 4–8 字节为 ``ftyp``
``odf_zip_magic`` 可读的 ZIP 且含 ``mimetype``（值为 ODF MIME）
``odt_magic`` / ``ods_magic`` ZIP 且 ``mimetype`` 精确等于对应的 ODF MIME
``docx_magic`` / ``xlsx_magic`` / ``pptx_magic`` ZIP + ``[Content_Types].xml`` + 各自的主部件
``office_zip_magic`` 可读的 ZIP（ODF 或 OOXML）
``svg_magic``  ``<svg`` 头（允许 ``<?xml`` 前缀）
``xml_wellformed`` 可被 XML 解析器解析
============= ==========================================================

判定只用文件内容，不看命令输出；每条规则单独给证据（格式、字节数、失败原因），
供 ``appcli_run`` 与 ``appcli_verify``（H05）复用。除 ``exists`` 外的规则在文件
不存在时直接判失败，不做无意义的读取。
"""
from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

__all__ = [
    "ARTIFACT_RULES",
    "ArtifactVerdict",
    "RuleResult",
    "verify_artifact",
]

#: 头部/尾部探测读取的字节数；足够覆盖签名与结束标记，不整文件读入。
_PROBE_BYTES = 4096

_PDF_HEAD = b"%PDF-"
_PDF_TAIL = b"%%EOF"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_TAIL = b"IEND"
_JPEG_HEAD = b"\xff\xd8\xff"
_JPEG_TAIL = b"\xff\xd9"
_ZIP_HEAD = b"PK\x03\x04"
_ODF_MIME_PREFIX = "application/vnd.oasis.opendocument"

#: 「哪种 ODF」由 mimetype 条目精确决定（odf_zip_magic 只验前缀，分不出文字/表格）。
_ODF_MIMES = {
    "odt_magic": "application/vnd.oasis.opendocument.text",
    "ods_magic": "application/vnd.oasis.opendocument.spreadsheet",
}

#: OOXML 的必要部件：只验「是 ZIP」会把换后缀的 ODF 当成 Word，所以按主部件判。
_OOXML_PARTS = {
    "docx_magic": ("word/document.xml",),
    "xlsx_magic": ("xl/workbook.xml",),
    "pptx_magic": ("ppt/presentation.xml",),
}
_OOXML_CONTENT_TYPES = "[Content_Types].xml"


@dataclass(frozen=True)
class RuleResult:
    rule: str
    ok: bool
    evidence: str


@dataclass(frozen=True)
class ArtifactVerdict:
    path: Path
    results: tuple[RuleResult, ...]

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(item.ok for item in self.results)

    @property
    def failed(self) -> tuple[RuleResult, ...]:
        return tuple(item for item in self.results if not item.ok)

    @property
    def size(self) -> int | None:
        try:
            return self.path.stat().st_size
        except OSError:
            return None

    def evidence_lines(self) -> list[str]:
        return [f"{item.rule}: {item.evidence}" for item in self.results]

    def summary(self) -> str:
        """一行结论：通过时给「路径（字节数）」，失败时给失败原因。"""
        if self.ok:
            return f"{self.path}（{self.size} B）"
        reasons = "；".join(f"{item.rule}={item.evidence}" for item in self.failed)
        return f"{self.path} 校验未通过：{reasons}"


# ── 探针 ─────────────────────────────────────────────────────────────

def _head(path: Path, size: int = _PROBE_BYTES) -> bytes:
    with path.open("rb") as handle:
        return handle.read(size)


def _tail(path: Path, size: int = _PROBE_BYTES) -> bytes:
    with path.open("rb") as handle:
        try:
            handle.seek(-size, 2)
        except OSError:
            handle.seek(0)
        return handle.read(size)


def _stat_size(path: Path) -> int:
    return path.stat().st_size


# ── 规则实现 ─────────────────────────────────────────────────────────

def _rule_exists(path: Path) -> tuple[bool, str]:
    if path.is_file():
        return True, "文件存在"
    if path.exists():
        return False, "存在但不是普通文件"
    return False, "文件不存在"


def _rule_size(path: Path) -> tuple[bool, str]:
    size = _stat_size(path)
    if size > 0:
        return True, f"{size} B"
    return False, "空文件"


def _rule_pdf(path: Path) -> tuple[bool, str]:
    if not _head(path).startswith(_PDF_HEAD):
        return False, f"缺少 {_PDF_HEAD.decode()} 头（不是 PDF）"
    if _PDF_TAIL not in _tail(path):
        return False, f"尾部没有 {_PDF_TAIL.decode()}（文件可能被截断）"
    return True, "PDF 头尾完整"


def _rule_png(path: Path) -> tuple[bool, str]:
    if not _head(path).startswith(_PNG_SIGNATURE):
        return False, "PNG 签名不匹配"
    if _PNG_TAIL not in _tail(path):
        return False, "尾部没有 IEND（文件可能被截断）"
    return True, "PNG 签名与结束块完整"


def _rule_jpeg(path: Path) -> tuple[bool, str]:
    if not _head(path).startswith(_JPEG_HEAD):
        return False, "JPEG 签名不匹配"
    if not _tail(path).rstrip().endswith(_JPEG_TAIL):
        return False, "尾部没有 EOI 标记（文件可能被截断）"
    return True, "JPEG 头尾完整"


def _rule_mp4(path: Path) -> tuple[bool, str]:
    header = _head(path)
    if len(header) < 8 or header[4:8] != b"ftyp":
        return False, "缺少 ftyp box（不是 MP4）"
    return True, "ftyp box 正常"


def _open_zip(path: Path) -> zipfile.ZipFile:
    return zipfile.ZipFile(path)


def _rule_odf_zip(path: Path) -> tuple[bool, str]:
    if not _head(path).startswith(_ZIP_HEAD):
        return False, "不是 ZIP 容器（不是 ODF）"
    try:
        with _open_zip(path) as archive:
            names = archive.namelist()
            if "mimetype" not in names:
                return False, "ZIP 里没有 mimetype 条目（不是 ODF）"
            mime = archive.read("mimetype")[:80].decode("ascii", "replace").strip()
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        return False, f"ZIP 无法读取（{exc}）"
    if not mime.startswith(_ODF_MIME_PREFIX):
        return False, f"mimetype={mime!r} 不是 ODF"
    return True, f"{mime} 容器正常"


def _rule_office_zip(path: Path) -> tuple[bool, str]:
    if not _head(path).startswith(_ZIP_HEAD):
        return False, "不是 ZIP 容器（不是 ODF/OOXML）"
    try:
        with _open_zip(path) as archive:
            count = len(archive.namelist())
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        return False, f"ZIP 无法读取（{exc}）"
    return True, f"ZIP 目录可读（{count} 个条目）"


def _rule_odf_exact(mime: str):
    """生成「ZIP + mimetype 精确等于某个 ODF MIME」的规则。"""

    def check(path: Path) -> tuple[bool, str]:
        if not _head(path).startswith(_ZIP_HEAD):
            return False, "不是 ZIP 容器（不是 ODF）"
        try:
            with _open_zip(path) as archive:
                if "mimetype" not in archive.namelist():
                    return False, "ZIP 里没有 mimetype 条目（不是 ODF）"
                found = archive.read("mimetype")[:80].decode("ascii", "replace").strip()
        except (zipfile.BadZipFile, OSError, ValueError) as exc:
            return False, f"ZIP 无法读取（{exc}）"
        if found != mime:
            return False, f"mimetype={found!r}，期望 {mime!r}"
        return True, f"{mime} 容器正常"

    return check


def _rule_ooxml(parts: tuple[str, ...]):
    """生成「ZIP + [Content_Types].xml + 主部件」的规则（docx/xlsx/pptx）。"""

    def check(path: Path) -> tuple[bool, str]:
        if not _head(path).startswith(_ZIP_HEAD):
            return False, "不是 ZIP 容器（不是 OOXML）"
        try:
            with _open_zip(path) as archive:
                names = set(archive.namelist())
        except (zipfile.BadZipFile, OSError, ValueError) as exc:
            return False, f"ZIP 无法读取（{exc}）"
        if _OOXML_CONTENT_TYPES not in names:
            return False, f"缺少 {_OOXML_CONTENT_TYPES}（不是 OOXML 包）"
        missing = [part for part in parts if part not in names]
        if missing:
            return False, f"缺少主部件 {', '.join(missing)}"
        return True, f"{', '.join(parts)} 存在，OOXML 容器正常"

    return check


def _rule_svg(path: Path) -> tuple[bool, str]:
    head = _head(path).lstrip(b"\xef\xbb\xbf \t\r\n")
    if head.startswith(b"<svg"):
        return True, "SVG 根元素正常"
    if head.startswith(b"<?xml") and b"<svg" in head:
        return True, "带 XML 声明的 SVG"
    return False, "没有找到 <svg 根元素"


def _rule_xml(path: Path) -> tuple[bool, str]:
    try:
        ElementTree.parse(path)
    except (ElementTree.ParseError, OSError, ValueError) as exc:
        return False, f"XML 解析失败（{exc}）"
    return True, "XML 可解析"


#: 规则名 → 实现。名字与 core.appcli_registry.VERIFY_RULES 一一对应。
ARTIFACT_RULES = {
    "exists": _rule_exists,
    "size>0": _rule_size,
    "pdf_magic": _rule_pdf,
    "png_magic": _rule_png,
    "jpeg_magic": _rule_jpeg,
    "mp4_magic": _rule_mp4,
    "odf_zip_magic": _rule_odf_zip,
    "office_zip_magic": _rule_office_zip,
    **{name: _rule_odf_exact(mime) for name, mime in _ODF_MIMES.items()},
    **{name: _rule_ooxml(part) for name, part in _OOXML_PARTS.items()},
    "svg_magic": _rule_svg,
    "xml_wellformed": _rule_xml,
}


def verify_artifact(path: Path, rules) -> ArtifactVerdict:
    """逐条校验产物；未知规则名按失败处理（注册表已在加载期拦过）。"""
    target = Path(path)
    results: list[RuleResult] = []
    # exists 先跑，且失败时后续规则不再读取文件（给同样的原因，避免误导）。
    ordered = sorted(rules, key=lambda name: 0 if name == "exists" else 1)
    for name in ordered:
        check = ARTIFACT_RULES.get(name)
        if check is None:
            results.append(RuleResult(name, False, "未知规则"))
            continue
        if results and not results[0].ok and name != "exists":
            results.append(RuleResult(name, False, "文件不存在，未执行"))
            continue
        try:
            ok, evidence = check(target)
        except OSError as exc:
            ok, evidence = False, f"读取失败（{exc}）"
        results.append(RuleResult(name, ok, evidence))
    return ArtifactVerdict(target, tuple(results))
