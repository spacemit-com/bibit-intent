"""knowledge Skill（云端知识源，只读）的执行实现。

口径见 ``docs/design/k3_smart_office.md`` §3.3：

- 本地资料永远走 ``file_search``（bibit-filesearch），**先本地后云端**；
- 云端来源由**用户指定**（``~/.config/bibit-intent/knowledge_sources.yaml``），
  ``kb_sources`` 只报状态，``kb_search`` 只查**已启用**的 ``remote_vector`` 来源；
- **未启用就不出网**：没有任何可用来源时明确拒绝，不静默换来源、不代网搜索；
- 只发**脱敏后的查询词**（``models.cloud_model.sanitize``），绝不发本地文件内容；
- 结果一律带 ``title``/``url`` 出处，供文档的「资料来源」段引用；
  没查到就说没查到——宁缺勿造（``knowledge/hallucination.py`` 的口径）。

``kind=web_search`` 的来源由 Runtime 的 ``web_search`` 执行，本模块只登记它开没开。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlparse

from bibit_intent.core.knowledge_sources import (
    KnowledgeSource,
    KnowledgeSourceError,
    load_sources,
    resolve_token,
    user_config_path,
)
from bibit_intent.models.cloud_model import sanitize

#: 单条片段的截断长度（云端返回可能很长，进 prompt 要限长）。
MAX_SNIPPET = 400

#: 一次检索最多回几条（跨来源合并后）。
MAX_RESULTS = 12

_KIND_LABELS = {
    "remote_vector": ("远程向量检索", "remote vector search"),
    "web_search": ("网络搜索", "web search"),
}


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


def _fail(
    code: str,
    zh: str,
    en: str,
    language: str | None = None,
    *,
    need_param: str | None = None,
    extra: dict | None = None,
) -> ExecResult:
    # ok + read_only 是执行层的「只读回包」约定：pipeline.py::_format_reply
    # 见到它就不再拼 detail，否则这份 JSON 会原样出现在用户看到的回答里。
    payload: dict = {"ok": False, "read_only": True, "code": code}
    if extra:
        payload.update(extra)
    return ExecResult(
        False,
        _reply(zh, en, language),
        detail=json.dumps(payload, ensure_ascii=False),
        need_param=need_param,
    )


def _ok(code: str, message: str, extra: dict | None = None) -> ExecResult:
    payload: dict = {"ok": True, "read_only": True, "code": code}
    if extra:
        payload.update(extra)
    return ExecResult(True, message, detail=json.dumps(payload, ensure_ascii=False))


def _kind_label(source: KnowledgeSource, language: str | None = None) -> str:
    zh, en = _KIND_LABELS.get(source.kind, (source.kind, source.kind))
    return _reply(zh, en, language)


def _host(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    return parsed.netloc or endpoint


def _config_hint() -> str:
    return str(user_config_path())


def kb_sources(language: str | None = None, **_) -> ExecResult:
    """列出用户指定的云端知识源与启用状态（只读，不发任何请求）。"""
    try:
        sources = load_sources()
    except KnowledgeSourceError as exc:
        return _fail(
            exc.code,
            f"知识源配置有误（{exc.code}）：{exc}",
            f"Invalid knowledge-source config ({exc.code}): {exc}",
            language,
            extra={"config": _config_hint()},
        )

    title = _reply(
        "云端知识源（用户可指定；本地文件请用 file_search）：",
        "Cloud knowledge sources (user-configured; use file_search for local files):",
        language,
    )
    lines = [title]
    remote_enabled = 0
    for index, source in enumerate(sources.values(), 1):
        state = _reply("已启用", "enabled", language) if source.enabled else _reply(
            "未启用", "disabled", language
        )
        label = source.label_en if str(language or "").lower().startswith("en") else source.label
        lines.append(f"{index}. {source.id}｜{label}（{_kind_label(source, language)}）｜{state}")
        if source.kind == "remote_vector":
            endpoint = source.endpoint or _reply("未配置", "not set", language)
            token_state = _reply("未设置", "not set", language)
            if source.token_env:
                token_state = (
                    source.token_env
                    if resolve_token(source)
                    else f"{source.token_env}（{token_state}）"
                )
            else:
                token_state = _reply("不需要", "not required", language)
            lines.append(
                _reply("   端点：", "   endpoint: ", language)
                + endpoint
                + _reply("｜令牌：", " | token: ", language)
                + token_state
            )
            if source.enabled:
                remote_enabled += 1
        if source.description:
            lines.append("   " + source.description)
    lines.append(
        _reply("配置文件：", "Config file: ", language)
        + _config_hint()
        + _reply(
            "（不存在时用随包默认）",
            " (packaged defaults apply when it is missing)",
            language,
        )
    )
    lines.append(
        _reply(
            "环境变量覆盖：BIBIT_KB_<ID>_ENABLED / _ENDPOINT / _TOKEN_ENV",
            "Env overrides: BIBIT_KB_<ID>_ENABLED / _ENDPOINT / _TOKEN_ENV",
            language,
        )
    )
    if remote_enabled:
        lines.append(
            _reply(
                f"kb_search 现在可用（{remote_enabled} 个远程来源已启用），只会把脱敏后的查询词发出去。",
                f"kb_search is available ({remote_enabled} remote source(s) enabled); it only sends the sanitized query text.",
                language,
            )
        )
    else:
        lines.append(
            _reply(
                "kb_search 现在会拒绝出网：没有已启用的远程来源。填好 endpoint 并把 enabled 改成 true 后用 kb_search。",
                "kb_search will refuse to make a network call: no remote source is enabled. Set an endpoint and flip enabled, then use kb_search.",
                language,
            )
        )
    return _ok(
        "ok",
        "\n".join(lines),
        {
            "config": _config_hint(),
            "sources": [
                {
                    "id": src.id,
                    "kind": src.kind,
                    "label": src.label,
                    "enabled": src.enabled,
                    "endpoint": _host(src.endpoint) if src.endpoint else "",
                    "token_env": src.token_env,
                    "token_present": bool(resolve_token(src)),
                }
                for src in sources.values()
            ],
        },
    )


def _clean_snippet(raw: object) -> str:
    text = " ".join(str(raw or "").split())
    if len(text) > MAX_SNIPPET:
        text = text[: MAX_SNIPPET - 1] + "…"
    return text


def _to_score(raw: object) -> float | None:
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _parse_results(body: object, limit: int) -> list[dict] | None:
    """generic 协议：``{"results": [{title, snippet, url, score}]}``。

    格式不符返回 ``None``（不猜结构）。
    """
    if not isinstance(body, dict):
        return None
    raw_results = body.get("results")
    if not isinstance(raw_results, list):
        return None
    results: list[dict] = []
    for item in raw_results[:limit]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        results.append(
            {
                "title": title,
                "snippet": _clean_snippet(item.get("snippet")),
                "url": str(item.get("url") or "").strip(),
                "score": _to_score(item.get("score")),
            }
        )
    return results


def _strip_highlight(text: str) -> str:
    import re

    return re.sub(r"</?em>", "", text)


def _parse_ragflow(
    body: object, limit: int
) -> tuple[list[dict] | None, dict | None, dict]:
    """进迭社区知识库（RagFlow 转发接口）的响应解析。

    契约见 ``docs/spec/RagFlow-检索接口对接文档.md`` §5：HTTP 200 + ``code=0``
    才算成功，``data.chunks[]`` 里取 ``content``/``document_keyword``/``similarity``；
    ``code != 0`` 是业务错误（读 ``message``），不猜结构。
    """
    if not isinstance(body, dict):
        return None, {"code": "kb_bad_response", "message": "响应不是 JSON 对象"}, {}
    if "code" not in body:
        return None, {"code": "kb_bad_response", "message": "响应缺少 code 字段"}, {}
    code = body.get("code")
    if code not in (0, "0"):
        return None, {
            "code": "kb_business_error",
            "message": f"RagFlow code={code}：{str(body.get('message') or '').strip()}",
        }, {}
    data = body.get("data")
    if not isinstance(data, dict):
        return None, {"code": "kb_bad_response", "message": "成功响应缺少 data 对象"}, {}
    chunks = data.get("chunks")
    if not isinstance(chunks, list):
        return None, {"code": "kb_bad_response", "message": "data 里没有 chunks 数组"}, {}
    results: list[dict] = []
    for item in chunks[:limit]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("document_keyword") or "").strip()
        document_id = str(item.get("document_id") or "").strip()
        if not title:
            title = document_id or _reply("未命名片段", "untitled chunk", None)
        snippet = str(item.get("content") or "").strip()
        if not snippet:
            snippet = _strip_highlight(str(item.get("highlight") or ""))
        results.append(
            {
                "title": title,
                "snippet": _clean_snippet(snippet),
                "url": str(item.get("url") or "").strip(),
                "score": _to_score(item.get("similarity")),
                "document_id": document_id,
            }
        )
    total = data.get("total")
    try:
        total = int(total) if total is not None else None
    except (TypeError, ValueError):
        total = None
    return results, None, {"total": total}


def _verify_option():
    """HTTPS 校验用的 CA 包：系统信任库**优先**，certifi 兜底。

    设备实测（进迭社区知识库走的是内网 CA `dc.com-CA`）：

    - 默认 `ssl.get_default_verify_paths()` 指向 `/usr/lib/ssl/cert.pem`，但这个
      venv 里的 OpenSSL 3.5.5 用它校验会报 `CERTIFICATE_VERIFY_FAILED`；
    - `certifi` 的 CA 包同样**不含**企业 CA，也不能用；
    - `/etc/ssl/certs/ca-certificates.crt`（系统 `ca-certificates` 包，本仓
      Depends 里就有）能验通。

    所以顺序是：`SSL_CERT_FILE` → 系统 bundle → certifi → 默认。目的只有一个：
    别把「证书链没配对」报成「接口不可达」（0.1.37 前就是这样，害得排查方向跑偏）。
    """
    import os
    from pathlib import Path

    candidates = [os.environ.get("SSL_CERT_FILE"), "/etc/ssl/certs/ca-certificates.crt"]
    for item in candidates:
        if item and Path(item).is_file():
            return item
    try:
        import certifi

        return certifi.where()
    except Exception:  # noqa: BLE001 - 没有 certifi 就交给默认校验
        return True


def _request_body(source: KnowledgeSource, query: str, limit: int) -> dict:
    """按协议构造请求体（只发查询词与调参，绝不带本地文件内容）。"""
    if source.protocol == "ragflow":
        # docs/spec/RagFlow-检索接口对接文档.md §3：question/page/page_size/…；
        # dataset_ids 由服务端强制指定，对接方不传。
        return {
            "question": query,
            "page": 1,
            "page_size": int(limit),
            "similarity_threshold": float(source.similarity_threshold),
            "vector_similarity_weight": float(source.vector_similarity_weight),
        }
    return {"query": query, "top_k": int(limit)}


def _query_source(
    source: KnowledgeSource, query: str, top_k: int
) -> tuple[list[dict], dict | None, dict]:
    """向一个来源发一次检索；返回（结果, 单个错误, 附加信息）三者之一。"""
    limit = int(top_k or source.top_k)
    extra: dict = {"protocol": source.protocol}
    token = resolve_token(source)
    if source.token_env and not token:
        return [], {
            "source": source.id,
            "code": "kb_no_token",
            "message": f"{source.token_env} 未设置",
        }, extra
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        import httpx

        resp = httpx.post(
            source.endpoint,
            json=_request_body(source, query, limit),
            headers=headers,
            timeout=float(source.timeout_seconds),
            verify=_verify_option(),
        )
    except Exception as exc:  # noqa: BLE001 - 出网失败一律降级为「无外部依据」
        name = type(exc).__name__
        text = str(exc)
        if "Timeout" in name:
            code = "kb_timeout"
        elif "CERTIFICATE_VERIFY_FAILED" in text or "SSLCertVerification" in name:
            # TLS 不是「对方不可达」：证书链没配好时要说清楚，别让用户去查网络。
            code = "kb_tls_failed"
        else:
            code = "kb_unreachable"
        return [], {"source": source.id, "code": code, "message": f"{name}: {text[:200]}"}, extra
    if resp.status_code == 429:
        return [], {
            "source": source.id,
            "code": "kb_rate_limited",
            "message": "HTTP 429（限流；文档口径：每 IP 每秒 100 次，需退避重试）",
        }, extra
    if resp.status_code in (401, 403):
        return [], {
            "source": source.id,
            "code": "kb_auth_failed",
            "message": f"HTTP {resp.status_code}",
        }, extra
    if resp.status_code >= 400:
        return [], {
            "source": source.id,
            "code": "kb_http_error",
            "message": f"HTTP {resp.status_code}",
        }, extra
    try:
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        return [], {
            "source": source.id,
            "code": "kb_bad_response",
            "message": f"JSON 解析失败（{type(exc).__name__}）",
        }, extra
    if source.protocol == "ragflow":
        results, problem, ragflow_extra = _parse_ragflow(body, limit)
        if problem is not None:
            return [], {"source": source.id, **problem}, extra
        return results or [], None, {**extra, **ragflow_extra}
    results = _parse_results(body, limit)
    if results is None:
        return [], {
            "source": source.id,
            "code": "kb_bad_response",
            "message": "响应缺少 results 数组",
        }, extra
    return results, None, extra


def kb_search(
    query: str = "",
    source: str = "",
    top_k: int = 0,
    language: str | None = None,
    **_,
) -> ExecResult:
    """向已启用的云端远程来源检索（只发脱敏查询词，未启用则拒绝出网）。"""
    text = sanitize(" ".join(str(query or "").split()))
    if not text:
        return _fail(
            "kb_missing_query",
            "检索知识库需要查询内容。",
            "A knowledge-base search needs a query.",
            language,
            need_param="query",
        )
    try:
        sources = load_sources()
    except KnowledgeSourceError as exc:
        return _fail(
            exc.code,
            f"知识源配置有误（{exc.code}）：{exc}",
            f"Invalid knowledge-source config ({exc.code}): {exc}",
            language,
            extra={"config": _config_hint()},
        )

    if source:
        picked_source = sources.get(source.strip())
        if picked_source is None:
            known = "、".join(sources) or _reply("（无）", "(none)", language)
            return _fail(
                "kb_unknown_source",
                f"没有名为 {source!r} 的知识源。已配置：{known}",
                f"No knowledge source named {source!r}. Configured: {known}",
                language,
            )
        if picked_source.kind == "web_search":
            return _fail(
                "kb_delegated",
                "网络搜索由 Runtime 的 web_search 执行，kb_search 不代它出网。"
                "领域内/公司内的问题请启用 remote_vector 知识源。",
                "Web search runs through Runtime's web_search; kb_search does not send it. "
                "Enable a remote_vector source for domain or company knowledge.",
                language,
                extra={"config": _config_hint()},
            )
        if not picked_source.enabled:
            return _fail(
                "kb_disabled",
                f"知识源 {picked_source.id}（{picked_source.label}）未启用，本次不发起任何网络请求。"
                f"在 {_config_hint()} 里填好 endpoint 并把 enabled 改成 true，"
                f"或设 {_env_hint(picked_source)}。",
                f"Knowledge source {picked_source.id} ({picked_source.label}) is disabled; "
                f"no network request was made. Set an endpoint and enable it in {_config_hint()}, "
                f"or set {_env_hint(picked_source)}.",
                language,
                extra={"config": _config_hint()},
            )
        picked = [picked_source]
    else:
        picked = [
            src
            for src in sources.values()
            if src.enabled and src.kind == "remote_vector"
        ]

    if not picked:
        web_enabled = [
            src for src in sources.values() if src.enabled and src.kind == "web_search"
        ]
        extra = {"config": _config_hint()}
        if web_enabled:
            extra["web_search_enabled"] = True
        return _fail(
            "kb_disabled",
            "没有已启用的远程知识源，本次不发起任何网络请求（未启用就不出网）。"
            + ("网络搜索已启用，可改用 Runtime 的 web_search。" if web_enabled else "")
            + f"启用方式：编辑 {_config_hint()}，或设 BIBIT_KB_<ID>_ENABLED=1 与 BIBIT_KB_<ID>_ENDPOINT。"
            + "本地资料请先用 file_search。",
            "No enabled remote knowledge source, so no network request was made (disabled means no egress)."
            + (" Web search is enabled; use Runtime's web_search instead." if web_enabled else "")
            + f" Enable one in {_config_hint()}, or set BIBIT_KB_<ID>_ENABLED=1 and BIBIT_KB_<ID>_ENDPOINT."
            " For local material, use file_search first.",
            language,
            extra=extra,
        )

    collected: list[dict] = []
    errors: list[dict] = []
    totals: dict[str, int] = {}
    for item in picked:
        results, error, extra = _query_source(item, text, top_k)
        if error:
            errors.append(error)
            continue
        if extra.get("total") is not None:
            totals[item.id] = extra["total"]
        for entry in results:
            collected.append({**entry, "source": item.id, "source_label": item.label})

    if errors and not collected:
        first = errors[0]
        detail = _reply(f"（{first['source']}：{first['message']}）", f" ({first['source']}: {first['message']})", language)
        return _fail(
            first["code"],
            f"云端知识源检索失败{detail}，本次按「无外部依据」处理，不编造内容。",
            f"Cloud knowledge-source search failed{detail}; treat it as no external evidence and do not invent facts.",
            language,
            extra={"errors": errors, "config": _config_hint()},
        )

    collected = collected[:MAX_RESULTS]
    lines = [
        _reply("云端知识源检索结果：", "Cloud knowledge-source results:", language),
    ]
    for index, item in enumerate(collected, 1):
        score = (
            _reply("，相关性 ", ", score ", language) + f"{item['score']:.2f}"
            if item["score"] is not None
            else ""
        )
        doc_id = item.get("document_id") or ""
        where = item["source_label"] + (
            f"，{_reply('文档 ID', 'doc', language)} {doc_id}" if doc_id else ""
        )
        lines.append(
            f"{index}. {item['title']}（{_reply('来源', 'source', language)}：{where}{score}）"
        )
        if item["snippet"]:
            lines.append("   " + item["snippet"])
        if item["url"]:
            lines.append("   " + item["url"])
    if totals:
        lines.append(
            _reply(
                "命中：" + "；".join(f"{sid} {count} 个片段" for sid, count in totals.items()),
                "Hits: " + "; ".join(f"{sid} {count} chunks" for sid, count in totals.items()),
                language,
            )
        )
    if errors:
        lines.append(
            _reply("部分来源失败：", "Some sources failed: ", language)
            + "；".join(f"{e['source']}（{e['code']}）" for e in errors)
        )
    if not collected:
        lines.append(
            _reply(
                "已启用的来源里没有相关内容；没有依据的部分不要编造。",
                "Nothing relevant in the enabled sources; do not fabricate what has no evidence.",
                language,
            )
        )
    lines.append(
        _reply(
            "引用时带上 title 与 URL，并写进文档的「资料来源」段；宁缺勿造。",
            "Cite title and URL in the document's Sources section; prefer omission over invention.",
            language,
        )
    )
    return _ok(
        "ok",
        "\n".join(lines),
        {
            "query": text,
            "sanitized": text != str(query or "").strip(),
            "results": collected,
            "errors": errors,
            "totals": totals,
            "sources": [
                {"id": src.id, "label": src.label, "protocol": src.protocol} for src in picked
            ],
        },
    )


def _env_hint(source: KnowledgeSource) -> str:
    prefix = "BIBIT_KB_" + source.id.upper()
    return f"{prefix}_ENABLED=1 与 {prefix}_ENDPOINT"


HANDLERS = {
    "kb_sources": kb_sources,
    "kb_search": kb_search,
}


# ── 本地取文：file_search 只给元数据，正文要单独读 ─────────────────────
# bibit-filesearch 0.1.7 的 /api/search 只回元数据（没有 summary/正文，也没有
# 内容接口；DB 里的 chunks.text 未对外）。所以「本地资料」的完整路径是
# file_search（找路径）→ file_read（读正文，本工具）→ 落笔并标注来源。
# 允许读的目录**就是** filesearch 的 index_dirs（同一条信任边界），
# 二进制/超大文件明确拒绝，绝不猜内容。

#: 单次读文大小上限（字节）；超过就截断并说明。
DEFAULT_READ_BYTES = 64 * 1024
MAX_READ_BYTES = 256 * 1024

#: 进 prompt 的正文字符上限（detail 不再复制一份，避免双倍 token）。
MAX_READ_CHARS = 8000

#: 明确无法取文的容器格式（不是纯文本，也没有可用的抽取器）。
_BINARY_HINT = (
    "这是二进制/容器文件（docx/pdf/…），本机当前没有取文接口："
    "bibit-filesearch 0.1.7 只回元数据。不要凭文件名猜内容——"
    "要么换一份纯文本/markdown 资料，要么让用户贴正文。"
)

#: 同上的英文版（语言参数为 en 时用）。
_BINARY_HINT_EN = (
    "This is a binary/container file (docx/pdf/...). There is no text-extraction "
    "endpoint on this device: bibit-filesearch 0.1.7 returns metadata only. Do not guess "
    "the content from the file name - use a plain-text/markdown source or ask the user "
    "to paste the text."
)


def _filesearch_port() -> int:
    """与 execution/system_tools.py:_filesearch_port 同口径（环境变量优先）。"""
    import os

    env_port = os.environ.get("BIBIT_FILESEARCH_PORT")
    if env_port:
        return int(env_port)
    return 18700 if os.getuid() < 1000 else 18700 + (os.getuid() - 1000)


def file_read(path: str = "", max_bytes: int = 0, language: str | None = None, **_) -> ExecResult:
    """读取当前授权下的正文，所有文件结果入口共用相同边界。"""
    from bibit_intent.file_results import FileResultError, authorized_open, extract_text

    raw_path = str(path or "").strip()
    if not raw_path:
        return _fail("file_missing_path", "读取文件需要给出绝对路径。", "An absolute file path is required.", language, need_param="path")
    if not raw_path.startswith("/"):
        return _fail("file_path_relative", "请给出绝对路径。", "Give an absolute file path.", language)
    try:
        limit = max(1, min(int(max_bytes or DEFAULT_READ_BYTES), MAX_READ_BYTES))
        with authorized_open(raw_path) as (fd, resolved, info):
            text, truncated = extract_text(fd, resolved, info.st_size, max_chars=MAX_READ_CHARS, max_bytes=limit)
        header = f"{resolved}（{info.st_size} 字节{'，已截断' if truncated else ''}）："
        if str(language or "").lower().startswith("en"):
            header = f"{resolved} ({info.st_size} bytes{', truncated' if truncated else ''}):"
        return _ok("file_text", header + "\n" + text, {"path": str(resolved), "size": info.st_size, "truncated": truncated})
    except (FileResultError, ValueError) as exc:
        return _fail("file_unavailable", str(exc), "The file is unavailable, unauthorized or its format is unsupported.", language)

HANDLERS["file_read"] = file_read
