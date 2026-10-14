"""core/knowledge_sources.py — 用户指定的云端知识源（配置 + 校验）

设计与边界见 ``docs/design/k3_smart_office.md`` §3.3：本地资料永远走
``bibit-filesearch``（``file_search``）；**云端**来源由用户指定——例如进迭社区
知识库（远程向量检索）或网络搜索。本模块只管「有哪些来源、是否启用、怎么连」，
不做任何出网请求（真正发请求的是 ``skills/knowledge/handlers.py``）。

三层配置，后者覆盖前者（按 ``id`` 合并）：

1. 随包默认：``resources/knowledge_sources.yaml``（出厂**全部关闭**，
   不替用户把内容发出去）；
2. 用户配置：``~/.config/bibit-intent/knowledge_sources.yaml``
   （前端「知识库」开关列表与运维都改这份；``BIBIT_KB_SOURCES_FILE`` 可改路径）；
3. 环境变量：``BIBIT_KB_<ID>_ENABLED`` / ``_ENDPOINT`` / ``_TOKEN_ENV``
   （部署侧 drop-in 用，避免为了开一个开关去改 YAML）。

缓存按「文件 mtime」失效：边缘服务与 MCP server 都是长驻进程，用户改完配置
不重启也要生效（对照 K05/K06 的同类问题）。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from bibit_intent.resources import resource_path

__all__ = [
    "DEFAULT_CONFIG_FILENAME",
    "KnowledgeSource",
    "KnowledgeSourceError",
    "default_config_path",
    "enabled_sources",
    "load_sources",
    "resolve_token",
]

#: 云端知识源的用户配置文件（出厂默认内容随包在 resources/ 下）。
DEFAULT_CONFIG_FILENAME = "knowledge_sources.yaml"

#: 改配置文件路径的环境变量（测试与多用户部署用）。
CONFIG_PATH_ENV = "BIBIT_KB_SOURCES_FILE"

#: remote_vector 之外，kind=web_search 的来源由 Runtime 的 web_search 执行：
#: 本仓只登记它「开没开」，绝不代它出网。
KINDS = ("remote_vector", "web_search")

#: remote_vector 的对接协议：
#:   - ``ragflow`` — 进迭社区知识库的 RagFlow 检索转发接口，见
#:     ``docs/spec/RagFlow-检索接口对接文档.md``（POST /ragflow/retrieval，
#:     请求 question/page/page_size/…，响应 data.chunks[]，HTTP 200 + code≠0 是业务错误）；
#:   - ``generic`` — 最小契约 ``{query, top_k} → {results:[{title,snippet,url,score}]}``，
#:     供自建/其它厂商的知识库使用。
PROTOCOLS = ("ragflow", "generic")

_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_ENV_TOKEN_ENV = "_TOKEN_ENV"
_ENV_ENABLED = "_ENABLED"
_ENV_ENDPOINT = "_ENDPOINT"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class KnowledgeSourceError(ValueError):
    """配置读取/校验失败。``code`` 供工具层映射成稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def __str__(self) -> str:  # pragma: no cover - 便于日志阅读
        return f"[{self.code}] {super().__str__()}"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KnowledgeSource(_StrictModel):
    """一个云端知识源。

    ``enabled`` 为真才可能被 ``kb_search`` 使用；``token_env`` 只存**环境变量名**，
    令牌本身不进配置文件、不进日志。
    """

    id: str
    kind: str = "remote_vector"
    protocol: str = "generic"
    label: str
    label_en: str = ""
    enabled: bool = False
    endpoint: str = ""
    token_env: str = ""
    #: RagFlow 官方建议客户端超时 60–120s（服务端响应超时 120s）。
    timeout_seconds: float = 20.0
    #: 返回条数：generic 是 top_k，ragflow 映射成 page_size。
    top_k: int = 5
    #: ragflow 协议的检索调参（默认值与服务端一致）。
    similarity_threshold: float = 0.2
    vector_similarity_weight: float = 0.3
    description: str = ""

    @model_validator(mode="after")
    def _check(self) -> "KnowledgeSource":
        if not _ID_RE.match(self.id or ""):
            raise ValueError("id 必须是小写字母开头、仅含小写字母/数字/下划线的标识符")
        if self.kind not in KINDS:
            raise ValueError(f"kind 必须是 {'/'.join(KINDS)}")
        if self.protocol not in PROTOCOLS:
            raise ValueError(f"protocol 必须是 {'/'.join(PROTOCOLS)}")
        if not self.label:
            raise ValueError("label 不能为空")
        if self.kind == "web_search":
            if self.endpoint:
                raise ValueError("web_search 由 Runtime 的 web_search 执行，不能配 endpoint")
            if self.token_env:
                raise ValueError("web_search 不需要 token_env")
            if self.protocol != "generic":
                raise ValueError("web_search 没有 protocol")
        elif self.enabled and not self.endpoint.startswith(("http://", "https://")):
            raise ValueError("enabled 的 remote_vector 来源必须给出 http(s) endpoint")
        if self.endpoint and not self.endpoint.startswith(("http://", "https://")):
            raise ValueError("endpoint 必须是 http(s) 地址")
        if not 1.0 <= float(self.timeout_seconds) <= 180.0:
            raise ValueError("timeout_seconds 必须在 1–180 之间")
        if not 1 <= int(self.top_k) <= 30:
            raise ValueError("top_k 必须在 1–30 之间")
        if not 0.0 <= float(self.similarity_threshold) <= 1.0:
            raise ValueError("similarity_threshold 必须在 0–1 之间")
        if not 0.0 <= float(self.vector_similarity_weight) <= 1.0:
            raise ValueError("vector_similarity_weight 必须在 0–1 之间")
        if not self.label_en:
            self.label_en = self.label
        return self


class KnowledgeSourcesFile(_StrictModel):
    sources: list[KnowledgeSource] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "KnowledgeSourcesFile":
        seen: set[str] = set()
        for source in self.sources:
            if source.id in seen:
                raise ValueError(f"来源 id 重复：{source.id}")
            seen.add(source.id)
        return self


def default_config_path() -> Path:
    """随包的出厂默认配置（只读，用户改的是 ``user_config_path``）。"""
    return resource_path(DEFAULT_CONFIG_FILENAME)


def user_config_path() -> Path:
    """用户配置文件路径（``BIBIT_KB_SOURCES_FILE`` 可覆盖）。"""
    override = os.environ.get(CONFIG_PATH_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "bibit-intent" / DEFAULT_CONFIG_FILENAME


def _env_prefix(source_id: str) -> str:
    return "BIBIT_KB_" + re.sub(r"[^A-Za-z0-9]", "_", source_id.upper())


def _parse_bool(raw: str, env_name: str) -> bool:
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise KnowledgeSourceError(
        "kb_config_invalid", f"环境变量 {env_name}={raw!r} 不是布尔值（1/0、true/false）"
    )


def _apply_env(source: KnowledgeSource) -> KnowledgeSource:
    """环境变量覆盖（``BIBIT_KB_<ID>_*``）：部署侧开关不必改 YAML。"""
    prefix = _env_prefix(source.id)
    updates: dict[str, Any] = {}
    raw_enabled = os.environ.get(prefix + _ENV_ENABLED)
    if raw_enabled is not None and raw_enabled.strip():
        updates["enabled"] = _parse_bool(raw_enabled, prefix + _ENV_ENABLED)
    raw_endpoint = os.environ.get(prefix + _ENV_ENDPOINT)
    if raw_endpoint is not None and raw_endpoint.strip():
        updates["endpoint"] = raw_endpoint.strip()
    raw_token_env = os.environ.get(prefix + _ENV_TOKEN_ENV)
    if raw_token_env is not None and raw_token_env.strip():
        updates["token_env"] = raw_token_env.strip()
    if not updates:
        return source
    merged = source.model_copy(update=updates)
    # 复跑一次校验：环境变量把 endpoint 补上/打开开关后仍要合法。
    try:
        return KnowledgeSource.model_validate(merged.model_dump())
    except ValidationError as exc:
        raise KnowledgeSourceError(
            "kb_config_invalid", f"来源 {source.id} 的环境变量覆盖非法：{exc.errors()[0]['msg']}"
        ) from exc


def _read_file(path: Path, *, required: bool) -> list[KnowledgeSource]:
    if not path.is_file():
        if required:
            raise KnowledgeSourceError("kb_config_invalid", f"配置文件不存在：{path}")
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise KnowledgeSourceError(
            "kb_config_invalid", f"配置文件读取失败：{path}（{type(exc).__name__}）"
        ) from exc
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise KnowledgeSourceError(
            "kb_config_invalid", f"配置文件顶层必须是 mapping：{path}"
        )
    try:
        return KnowledgeSourcesFile.model_validate(raw).sources
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first.get("loc", ())) or "sources"
        raise KnowledgeSourceError(
            "kb_config_invalid", f"{path} 的 {where} 非法：{first['msg']}"
        ) from exc


#: (内置配置 mtime, 用户配置路径, 用户配置 mtime) → 合并后的来源
_CACHE: tuple[tuple[float, str, float], dict[str, KnowledgeSource]] | None = None


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return -1.0


def load_sources(path: Path | None = None) -> dict[str, KnowledgeSource]:
    """加载并合并云端知识源，返回 ``{id: KnowledgeSource}``（保持文件顺序）。

    参数 ``path`` 显式给出时只用该文件（测试/工具用），不做内置合并。
    """
    global _CACHE

    user_path = Path(path).expanduser() if path is not None else user_config_path()
    builtin = default_config_path() if path is None else None
    key = (
        _mtime(builtin) if builtin is not None else -1.0,
        str(user_path),
        _mtime(user_path),
    )
    if _CACHE is not None and _CACHE[0] == key:
        return {sid: src.model_copy(deep=True) for sid, src in _CACHE[1].items()}

    merged: dict[str, KnowledgeSource] = {}
    if builtin is not None:
        for source in _read_file(builtin, required=False):
            merged[source.id] = source
    for source in _read_file(user_path, required=False):
        merged[source.id] = source

    result = {sid: _apply_env(src) for sid, src in merged.items()}
    _CACHE = (key, result)
    return {sid: src.model_copy(deep=True) for sid, src in result.items()}


def enabled_sources(
    kind: str = "", path: Path | None = None
) -> list[KnowledgeSource]:
    """已启用的来源（可按 kind 过滤）。``kind`` 只接受 :data:`KINDS` 里的值。"""
    if kind and kind not in KINDS:
        raise KnowledgeSourceError("kb_config_invalid", f"未知 kind：{kind}")
    sources = load_sources(path)
    return [
        source
        for source in sources.values()
        if source.enabled and (not kind or source.kind == kind)
    ]


def resolve_token(source: KnowledgeSource) -> str:
    """读取来源的令牌（只读环境变量，绝不落盘/回显）。"""
    if not source.token_env:
        return ""
    return os.environ.get(source.token_env, "").strip()
