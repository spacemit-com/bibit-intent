"""K06：knowledge Skill（kb_sources / kb_search，只读）。

对应 docs/design/k3_smart_office.md §3.3。验收口径：
  - 只读、不需要确认；未启用任何远程来源时**拒绝出网**（一次请求都不发）；
  - 只发脱敏后的查询词，不带本地文件内容；契约不符就报稳定错误码，不猜结构。
全部离线：httpx.post 一律被替换成假实现（或「被调用即失败」的哨兵）。
"""
from __future__ import annotations

import importlib.util
import json
import sys

import pytest
import yaml

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.resources import resource_path
from bibit_intent.routing.rule_engine import RuleEngine


def _load_handlers():
    path = resource_path("skills") / "knowledge" / "handlers.py"
    spec = importlib.util.spec_from_file_location("_test_knowledge_handlers", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def handlers():
    return _load_handlers()


@pytest.fixture(scope="module")
def rule() -> RuleEngine:
    registry = ToolRegistry(resource_path("tools.yaml"))
    load_skills(resource_path("skills"), registry, {})
    return RuleEngine(registry)


class _Resp:
    def __init__(self, status=200, payload=None, json_error=False):
        self.status_code = status
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("not json")
        return self._payload


def _configure(tmp_path, monkeypatch, sources):
    path = tmp_path / "knowledge_sources.yaml"
    path.write_text(yaml.safe_dump({"sources": sources}, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("BIBIT_KB_SOURCES_FILE", str(path))
    return path


def _remote(**over):
    data = {
        "id": "spacemit_community",
        "kind": "remote_vector",
        "label": "进迭社区知识库",
        "enabled": True,
        "endpoint": "https://kb.example.com/api/kb/search",
        "token_env": "BIBIT_KB_SPACEMIT_TOKEN",
        "timeout_seconds": 5,
        "top_k": 3,
    }
    data.update(over)
    return data



def _plan(text):
    """跑一遍端侧前置层（与 tests/test_local_file_search.py 同口径）。"""
    from bibit_intent.routing.intent import decide_local_intent

    registry = ToolRegistry(resource_path("tools.yaml"))
    load_skills(resource_path("skills"), registry, {})
    return decide_local_intent(text, RuleEngine(registry), registry, embed=None)

def _code(result) -> str:
    return json.loads(result.detail)["code"]


# ── 接线与路由 ───────────────────────────────────────────────────────

def test_tools_are_registered_readonly_without_confirmation():
    registry = ToolRegistry(resource_path("tools.yaml"))
    handlers: dict = {}
    load_skills(resource_path("skills"), registry, handlers)
    for name in ("kb_sources", "kb_search"):
        tool = registry.get(name)
        assert tool is not None, name
        assert tool.mcp_group() == "readonly", name
        assert not tool.system.get("confirm"), name
        assert name in handlers, name


def test_rule_layer_reaches_both_tools(rule):
    assert rule.match("列出云端知识源").tool_name == "kb_sources"
    assert rule.match("list knowledge sources").tool_name == "kb_sources"
    match = rule.match("在社区知识库里检索 GTK 怎么用")
    assert match.tool_name == "kb_search"
    assert match.slots == {"query": "GTK 怎么用"}


def test_knowledge_tools_do_not_steal_existing_paths(rule):
    assert rule.match("打开终端").tool_name == "launch_app"
    assert rule.match("本地搜索一下周报").tool_name == "file_search"


# ── kb_sources ───────────────────────────────────────────────────────

def test_sources_report_state_and_refusal(tmp_path, monkeypatch, handlers):
    _configure(tmp_path, monkeypatch, [_remote(enabled=False)])
    result = handlers.kb_sources()
    assert result.ok
    assert "spacemit_community" in result.message
    assert "未启用" in result.message
    assert "拒绝出网" in result.message
    payload = json.loads(result.detail)
    assert payload["code"] == "ok"
    ids = {item["id"]: item for item in payload["sources"]}
    assert ids["spacemit_community"]["enabled"] is False
    assert ids["web"]["kind"] == "web_search"


def test_sources_reports_bad_config_with_code(tmp_path, monkeypatch, handlers):
    path = tmp_path / "knowledge_sources.yaml"
    path.write_text("sources:\n  - id: A\n    kind: nope\n", encoding="utf-8")
    monkeypatch.setenv("BIBIT_KB_SOURCES_FILE", str(path))
    result = handlers.kb_sources()
    assert not result.ok
    assert _code(result) == "kb_config_invalid"


# ── kb_search：拒绝出网 ──────────────────────────────────────────────

def test_search_refuses_when_nothing_enabled(tmp_path, monkeypatch, handlers):
    _configure(tmp_path, monkeypatch, [_remote(enabled=False)])

    def boom(*args, **kwargs):  # pragma: no cover - 调用了就是失败
        raise AssertionError("未启用时不得发起任何网络请求")

    import httpx

    monkeypatch.setattr(httpx, "post", boom)
    result = handlers.kb_search("GTK 怎么用")
    assert not result.ok
    assert _code(result) == "kb_disabled"
    assert "不发起任何网络请求" in result.message or "不出网" in result.message


def test_search_refuses_disabled_named_source(tmp_path, monkeypatch, handlers):
    _configure(tmp_path, monkeypatch, [_remote(enabled=False)])

    def boom(*args, **kwargs):  # pragma: no cover
        raise AssertionError("未启用时不得发起任何网络请求")

    import httpx

    monkeypatch.setattr(httpx, "post", boom)
    result = handlers.kb_search("GTK", source="spacemit_community")
    assert not result.ok
    assert _code(result) == "kb_disabled"
    assert "spacemit_community" in result.message


def test_search_without_query_needs_param(handlers):
    result = handlers.kb_search("   ")
    assert not result.ok
    assert result.need_param == "query"
    assert _code(result) == "kb_missing_query"


# ── kb_search：契约与降级 ────────────────────────────────────────────

def test_search_returns_cited_results_and_sanitizes(tmp_path, monkeypatch, handlers):
    _configure(tmp_path, monkeypatch, [_remote()])
    monkeypatch.setenv("BIBIT_KB_SPACEMIT_TOKEN", "s3cret")
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None, verify=None):
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return _Resp(
            payload={
                "results": [
                    {
                        "title": "K3 串口配置",
                        "snippet": "在 /etc/bianbu 下配置 uart…",
                        "url": "https://community.example.com/t/1",
                        "score": 0.83,
                    },
                    {"snippet": "没有 title 的条目应被丢弃"},
                ]
            }
        )

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    result = handlers.kb_search("我的手机号 13812345678 想问串口怎么配")
    assert result.ok, result.message
    assert len(calls) == 1
    assert calls[0]["url"] == "https://kb.example.com/api/kb/search"
    assert calls[0]["headers"]["Authorization"] == "Bearer s3cret"
    assert calls[0]["json"] == {"query": "我的手机号 [手机号] 想问串口怎么配", "top_k": 3}
    assert calls[0]["timeout"] == 5
    assert "K3 串口配置" in result.message
    assert "https://community.example.com/t/1" in result.message
    payload = json.loads(result.detail)
    assert payload["query"].endswith("想问串口怎么配")
    assert [item["title"] for item in payload["results"]] == ["K3 串口配置"]
    assert payload["results"][0]["source"] == "spacemit_community"


def test_search_top_k_override_and_snippet_truncation(tmp_path, monkeypatch, handlers):
    _configure(tmp_path, monkeypatch, [_remote()])
    monkeypatch.setenv("BIBIT_KB_SPACEMIT_TOKEN", "t")

    def fake_post(url, json=None, headers=None, timeout=None, verify=None):
        assert json["top_k"] == 8
        return _Resp(payload={"results": [{"title": "T", "snippet": "x" * 900}]})

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    result = handlers.kb_search("q", top_k=8)
    assert result.ok
    snippet = json.loads(result.detail)["results"][0]["snippet"]
    assert len(snippet) <= handlers.MAX_SNIPPET
    assert snippet.endswith("…")


def test_search_auth_failure_has_stable_code(tmp_path, monkeypatch, handlers):
    _configure(tmp_path, monkeypatch, [_remote()])
    monkeypatch.setenv("BIBIT_KB_SPACEMIT_TOKEN", "bad")
    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(status=401))
    result = handlers.kb_search("q")
    assert not result.ok
    assert _code(result) == "kb_auth_failed"
    assert "不编造" in result.message or "no external evidence" in result.message


def test_search_timeout_and_unreachable(tmp_path, monkeypatch, handlers):
    _configure(tmp_path, monkeypatch, [_remote()])
    monkeypatch.setenv("BIBIT_KB_SPACEMIT_TOKEN", "t")
    import httpx

    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: (_ for _ in ()).throw(httpx.ReadTimeout("slow"))
    )
    assert _code(handlers.kb_search("q")) == "kb_timeout"

    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("refused"))
    )
    assert _code(handlers.kb_search("q")) == "kb_unreachable"


def test_search_tls_failure_has_its_own_code(tmp_path, monkeypatch, handlers):
    """证书链问题不能报成「不可达」（设备实测：venv 找不到系统 CA 路径）。"""
    _configure(tmp_path, monkeypatch, [_remote()])
    monkeypatch.setenv("BIBIT_KB_SPACEMIT_TOKEN", "t")
    import httpx

    def tls_error(*args, **kwargs):
        raise httpx.ConnectError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
            "unable to get local issuer certificate"
        )

    monkeypatch.setattr(httpx, "post", tls_error)
    result = handlers.kb_search("q")
    assert not result.ok
    assert _code(result) == "kb_tls_failed"
    assert "CERTIFICATE_VERIFY_FAILED" in result.message


def test_search_prefers_system_ca_bundle(tmp_path, monkeypatch, handlers):
    """系统信任库优先于 certifi（设备上企业 CA 只装在系统 bundle 里）。"""
    _configure(tmp_path, monkeypatch, [_remote()])
    monkeypatch.setenv("BIBIT_KB_SPACEMIT_TOKEN", "t")
    fake_bundle = tmp_path / "ca.pem"
    fake_bundle.write_text("dummy", encoding="utf-8")
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None, verify=None):
        seen["verify"] = verify
        return _Resp(payload={"results": [{"title": "T"}]})

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setenv("SSL_CERT_FILE", str(fake_bundle))
    assert handlers.kb_search("q").ok
    assert seen["verify"] == str(fake_bundle)

    # 没有环境变量时用系统 bundle（存在就用它，不存在才退回 certifi）
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    assert handlers.kb_search("q").ok
    assert "cacert.pem" in str(seen["verify"]) or "ca-certificates.crt" in str(
        seen["verify"]
    )


def test_search_bad_response_is_reported(tmp_path, monkeypatch, handlers):
    _configure(tmp_path, monkeypatch, [_remote()])
    monkeypatch.setenv("BIBIT_KB_SPACEMIT_TOKEN", "t")
    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(payload={"items": []}))
    assert _code(handlers.kb_search("q")) == "kb_bad_response"

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(json_error=True))
    assert _code(handlers.kb_search("q")) == "kb_bad_response"


def test_search_missing_token_is_reported(tmp_path, monkeypatch, handlers):
    _configure(tmp_path, monkeypatch, [_remote()])
    monkeypatch.delenv("BIBIT_KB_SPACEMIT_TOKEN", raising=False)

    def boom(*args, **kwargs):  # pragma: no cover
        raise AssertionError("缺令牌不得发起请求")

    import httpx

    monkeypatch.setattr(httpx, "post", boom)
    result = handlers.kb_search("q")
    assert not result.ok
    assert _code(result) == "kb_no_token"


def test_search_rejects_web_source_and_unknown_source(tmp_path, monkeypatch, handlers):
    _configure(tmp_path, monkeypatch, [_remote()])
    delegated = handlers.kb_search("q", source="web")
    assert not delegated.ok
    assert _code(delegated) == "kb_delegated"

    unknown = handlers.kb_search("q", source="nope")
    assert not unknown.ok
    assert _code(unknown) == "kb_unknown_source"
    assert "spacemit_community" in unknown.message


def test_search_merges_multiple_sources(tmp_path, monkeypatch, handlers):
    _configure(
        tmp_path,
        monkeypatch,
        [
            _remote(),
            _remote(
                id="internal_wiki",
                label="内部 Wiki",
                endpoint="http://10.0.0.5:8080/search",
                token_env="",
            ),
        ],
    )
    monkeypatch.setenv("BIBIT_KB_SPACEMIT_TOKEN", "t")

    def fake_post(url, json=None, headers=None, timeout=None, verify=None):
        if "10.0.0.5" in url:
            return _Resp(payload={"results": [{"title": "内部条目", "url": "http://w/1"}]})
        return _Resp(payload={"results": [{"title": "社区条目", "url": "https://c/1"}]})

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    result = handlers.kb_search("q")
    assert result.ok
    payload = json.loads(result.detail)
    assert {item["source"] for item in payload["results"]} == {
        "spacemit_community",
        "internal_wiki",
    }


def test_search_partial_failure_still_returns_results(tmp_path, monkeypatch, handlers):
    _configure(
        tmp_path,
        monkeypatch,
        [_remote(), _remote(id="internal_wiki", label="内部 Wiki", endpoint="http://10.0.0.5/s")],
    )
    monkeypatch.setenv("BIBIT_KB_SPACEMIT_TOKEN", "t")

    def fake_post(url, json=None, headers=None, timeout=None, verify=None):
        if "10.0.0.5" in url:
            return _Resp(status=500)
        return _Resp(payload={"results": [{"title": "社区条目"}]})

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    result = handlers.kb_search("q")
    assert result.ok
    assert "部分来源失败" in result.message
    assert json.loads(result.detail)["errors"][0]["source"] == "internal_wiki"


def test_search_empty_results_says_so_without_inventing(tmp_path, monkeypatch, handlers):
    _configure(tmp_path, monkeypatch, [_remote()])
    monkeypatch.setenv("BIBIT_KB_SPACEMIT_TOKEN", "t")
    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(payload={"results": []}))
    result = handlers.kb_search("q")
    assert result.ok
    assert "不要编造" in result.message
    assert json.loads(result.detail)["results"] == []


def test_search_supports_english(tmp_path, monkeypatch, handlers):
    _configure(tmp_path, monkeypatch, [_remote()])
    monkeypatch.setenv("BIBIT_KB_SPACEMIT_TOKEN", "t")
    import httpx

    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: _Resp(payload={"results": [{"title": "T"}]})
    )
    result = handlers.kb_search("q", language="en")
    assert result.ok
    assert "Cloud knowledge-source results" in result.message


# ── file_read：本地取文（file_search 只回元数据）────────────────────

def _no_service(monkeypatch):
    """让 /api/settings 探不到：退回默认索引目录（$HOME/Desktop、$HOME/Documents）。"""
    import httpx

    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("no service"))
    )


def test_file_read_is_registered_readonly():
    registry = ToolRegistry(resource_path("tools.yaml"))
    handlers: dict = {}
    load_skills(resource_path("skills"), registry, handlers)
    tool = registry.get("file_read")
    assert tool is not None
    assert tool.mcp_group() == "readonly"
    assert not tool.system.get("confirm")
    assert "file_read" in handlers


def test_file_read_rule_carries_path(rule):
    match = rule.match("读取本地文件 /home/bianbu/Desktop/a.md")
    assert match.tool_name == "file_read"
    assert match.slots == {"path": "/home/bianbu/Desktop/a.md"}


def test_file_read_reads_text_inside_index_dir(tmp_path, monkeypatch, handlers):
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    _no_service(monkeypatch)
    target = home / "Desktop" / "notes.md"
    target.write_text("# 周报\n本周完成了 A、B。\n", encoding="utf-8")
    result = handlers.file_read(str(target))
    assert result.ok, result.message
    assert "本周完成了 A、B。" in result.message
    payload = json.loads(result.detail)
    assert payload["code"] == "file_text"
    assert payload["path"] == str(target)


def test_file_read_uses_filesearch_index_dirs(tmp_path, monkeypatch, handlers):
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    index_dir = tmp_path / "idx"
    index_dir.mkdir()
    monkeypatch.setenv("HOME", str(home))
    import httpx

    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **k: _Resp(payload={"data": {"index_dirs": [str(index_dir)]}}),
    )
    inside = index_dir / "a.txt"
    inside.write_text("indexed", encoding="utf-8")
    assert handlers.file_read(str(inside)).ok

    outside = home / "Desktop" / "b.txt"
    outside.write_text("not indexed", encoding="utf-8")
    result = handlers.file_read(str(outside))
    assert not result.ok
    assert _code(result) == "file_outside_index_dirs"


def test_file_read_refuses_outside_binary_and_oversize(tmp_path, monkeypatch, handlers):
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    _no_service(monkeypatch)

    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    assert _code(handlers.file_read(str(outside))) == "file_outside_index_dirs"

    binary = home / "Desktop" / "a.docx"
    binary.write_bytes(b"PK\x03\x04\x00\x00binary")
    result = handlers.file_read(str(binary))
    assert result.ok
    assert _code(result) == "file_binary"
    assert "不要凭文件名猜内容" in result.message

    pdf = home / "Desktop" / "b.pdf"
    pdf.write_bytes(b"%PDF-1.7\n...")
    assert _code(handlers.file_read(str(pdf))) == "file_binary"


def test_file_read_path_argument_errors(tmp_path, monkeypatch, handlers):
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    _no_service(monkeypatch)

    missing = handlers.file_read("")
    assert missing.need_param == "path"
    assert _code(missing) == "file_missing_path"

    assert _code(handlers.file_read("relative.txt")) == "file_path_relative"

    gone = handlers.file_read(str(home / "Desktop" / "nope.txt"))
    assert not gone.ok
    assert _code(gone) == "file_not_found"


def test_file_read_truncates_by_max_bytes(tmp_path, monkeypatch, handlers):
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    _no_service(monkeypatch)
    target = home / "Desktop" / "big.txt"
    target.write_text("x" * 5000, encoding="utf-8")
    result = handlers.file_read(str(target), max_bytes=100)
    assert result.ok
    assert "已截断" in result.message
    assert "x" * 100 in result.message
    assert "x" * 101 not in result.message


# ── RagFlow 协议（进迭社区知识库，docs/spec/RagFlow-检索接口对接文档.md）──

def _ragflow(**over):
    data = _remote(
        protocol="ragflow",
        endpoint="https://developer.bianbu.xyz/api-server/ragflow/retrieval",
        token_env="",
        timeout_seconds=60,
        top_k=10,
        similarity_threshold=0.2,
        vector_similarity_weight=0.3,
        **over,
    )
    return data


def test_ragflow_request_shape_and_citation(tmp_path, monkeypatch, handlers):
    _configure(tmp_path, monkeypatch, [_ragflow()])
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None, verify=None):
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return _Resp(
            payload={
                "code": 0,
                "data": {
                    "chunks": [
                        {
                            "id": "d784",
                            "content": "UART 外设支持……",
                            "document_id": "5c59",
                            "document_keyword": "uart.md",
                            "similarity": 0.9669,
                        }
                    ],
                    "doc_aggs": [{"count": 1, "doc_id": "5c59", "doc_name": "uart.md"}],
                    "total": 1,
                },
            }
        )

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    result = handlers.kb_search("K1 芯片的 UART 外设如何配置")
    assert result.ok, result.message
    # 文档 §3：question/page/page_size/…；dataset_ids 不传
    assert calls[0]["json"] == {
        "question": "K1 芯片的 UART 外设如何配置",
        "page": 1,
        "page_size": 10,
        "similarity_threshold": 0.2,
        "vector_similarity_weight": 0.3,
    }
    assert "dataset_ids" not in calls[0]["json"]
    assert calls[0]["timeout"] == 60
    assert "Authorization" not in calls[0]["headers"]
    # 引用带出处：文档名 + 文档 ID
    assert "uart.md" in result.message
    assert "5c59" in result.message
    assert "UART 外设支持" in result.message
    payload = json.loads(result.detail)
    assert payload["results"][0]["title"] == "uart.md"
    assert payload["results"][0]["document_id"] == "5c59"
    assert payload["totals"] == {"spacemit_community": 1}
    assert payload["sources"][0]["protocol"] == "ragflow"


def test_ragflow_business_error_is_not_a_success(tmp_path, monkeypatch, handlers):
    """HTTP 200 + code!=0 是业务错误（文档 §5.2/§6.2），不能当成检索结果。"""
    _configure(tmp_path, monkeypatch, [_ragflow()])
    import httpx

    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _Resp(payload={"code": 102, "message": "`datasets` is required."}),
    )
    result = handlers.kb_search("什么是 RISC-V")
    assert not result.ok
    assert _code(result) == "kb_business_error"
    assert "datasets" in result.message


def test_ragflow_rate_limit_and_bad_data(tmp_path, monkeypatch, handlers):
    _configure(tmp_path, monkeypatch, [_ragflow()])
    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(status=429))
    result = handlers.kb_search("q")
    assert not result.ok
    assert _code(result) == "kb_rate_limited"
    assert "退避" in result.message

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(payload={"code": 0, "data": {}}))
    assert _code(handlers.kb_search("q")) == "kb_bad_response"

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(payload={"code": 0}))
    assert _code(handlers.kb_search("q")) == "kb_bad_response"


def test_ragflow_highlight_fallback_and_document_id_title(tmp_path, monkeypatch, handlers):
    _configure(tmp_path, monkeypatch, [_ragflow()])
    import httpx

    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _Resp(
            payload={
                "code": 0,
                "data": {
                    "chunks": [
                        {
                            "content": "",
                            "highlight": "<em>UART</em> 外设支持……",
                            "document_id": "5c59",
                        }
                    ],
                    "total": 1,
                },
            }
        ),
    )
    result = handlers.kb_search("uart")
    assert result.ok
    payload = json.loads(result.detail)
    assert payload["results"][0]["title"] == "5c59"
    assert payload["results"][0]["snippet"] == "UART 外设支持……"


# ── 端侧直通：显式「读这个文件」──────────────���─────────────────────
# 实测动机（K13）：方位词+名词（"本地文件"）会命中 _LOCAL_SEARCH_RE，于是
# 「读取本地文件 /path」被判成 file_search 直通，用户只拿到文件名。显式读文件
# 必须早于该触发词；路径后面还有别的要求时留给云端加工。

def test_explicit_read_routes_to_file_read(rule):
    plan = _plan("读取本地文件 /home/bianbu/Desktop/kb_uart_notes.txt")
    assert plan.action.value == "rule_call"
    assert plan.tool_name == "file_read"
    assert plan.slots == {"path": "/home/bianbu/Desktop/kb_uart_notes.txt"}

    en = _plan("read local file /home/bianbu/Desktop/a.txt")
    assert en.tool_name == "file_read"
    assert en.slots == {"path": "/home/bianbu/Desktop/a.txt"}


def test_explicit_read_with_extra_instruction_escalates(rule):
    plan = _plan("读取本地文件 /home/bianbu/Desktop/kb_uart_notes.txt，然后总结要点")
    assert plan.action.value == "escalate"
    assert plan.reason == "file_read_with_instruction"


def test_explicit_read_does_not_break_local_search(rule):
    plan = _plan("本地搜索一下周报")
    assert plan.tool_name == "file_search"


def test_knowledge_tools_are_not_device_changes(rule):
    """只读 Skill 不能被当成「可能改设备」：否则慢检索被 40s 预算掐死、
    只读失败后的云端兜底还会被改判成 uncertain。"""
    from bibit_intent.execution.executor import _may_change_device

    registry = ToolRegistry(resource_path("tools.yaml"))
    load_skills(resource_path("skills"), registry, {})
    for name in ("file_read", "kb_sources", "kb_search"):
        assert _may_change_device(registry.get(name)) is False, name


# ── 办公产出任务不能被 file_search 直通吃掉 ───────────────────────────
# 实测（K13 回归）：prompt 里出现「本地资料/周报」时，_LOCAL_SEARCH_RE 的
# 「方位词+名词」分支会先命中，写作请求只回了文件名列表，模型根本没跑。

def test_office_deliverable_beats_the_local_search_fast_path(rule):
    plan = _plan(
        "帮我写这周的周报，主题是 K1 UART 调试：先用本地资料（桌面上的 "
        "kb_uart_notes.txt 和 上周周报.md），再查社区知识库补充 UART 配置要点，"
        "产出 PDF 和 DOCX"
    )
    assert plan.action.value == "escalate"
    assert plan.reason == "office_deliverable"


def test_search_wording_still_uses_the_fast_path(rule):
    assert _plan("本地搜索一下周报").tool_name == "file_search"
    assert _plan("帮我本地找一下会议纪要").tool_name == "file_search"


def test_script_writing_task_keeps_its_own_reason(rule):
    """写脚本走既有的 _WRITING_TASK_RE，别被办公产出任务的正则抢走。"""
    plan = _plan("帮我写一个搜索本地文件的脚本")
    assert plan.action.value == "escalate"
    assert plan.reason == "writing_task"
