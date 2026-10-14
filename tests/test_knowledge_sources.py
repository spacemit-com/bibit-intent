"""K06：云端知识源配置（core/knowledge_sources.py）。

对应 docs/design/k3_smart_office.md §3.3：出厂默认、用户文件、环境变量三层合并；
坏配置加载期即报稳定错误码；缓存按文件 mtime 失效（长驻进程改完配置要生效）。
"""
from __future__ import annotations

import pytest

from bibit_intent.core.knowledge_sources import (
    DEFAULT_CONFIG_FILENAME,
    KnowledgeSourceError,
    default_config_path,
    enabled_sources,
    load_sources,
    resolve_token,
    user_config_path,
)
from bibit_intent.resources import resource_path


def _write(path, sources):
    import yaml

    path.write_text(
        yaml.safe_dump({"sources": sources}, allow_unicode=True), encoding="utf-8"
    )
    return path


def test_packaged_defaults_are_all_off_for_remote(tmp_path, monkeypatch):
    """随包默认：社区知识库预填 RagFlow 测试端点但**关闭**；网络搜索登记为开启。"""
    monkeypatch.setenv("BIBIT_KB_SOURCES_FILE", str(tmp_path / "none.yaml"))
    sources = load_sources()
    community = sources["spacemit_community"]
    assert community.kind == "remote_vector"
    assert community.protocol == "ragflow"
    assert community.enabled is False
    assert community.endpoint.endswith("/ragflow/retrieval")
    assert community.token_env == ""  # 公开接口，仅按 IP 限流
    assert 60 <= community.timeout_seconds <= 120  # 文档建议客户端 60–120s
    assert sources["web"].kind == "web_search"
    assert sources["web"].enabled is True
    assert enabled_sources("remote_vector") == []


def test_user_file_merges_and_overrides_defaults(tmp_path, monkeypatch):
    path = _write(
        tmp_path / "kb.yaml",
        [
            {
                "id": "spacemit_community",
                "kind": "remote_vector",
                "label": "进迭社区知识库",
                "enabled": True,
                "endpoint": "https://kb.example.com/api/search",
                "token_env": "MY_TOKEN",
            },
            {
                "id": "internal_wiki",
                "kind": "remote_vector",
                "label": "内部 Wiki",
                "enabled": True,
                "endpoint": "http://10.0.0.5:8080/search",
            },
        ],
    )
    monkeypatch.setenv("BIBIT_KB_SOURCES_FILE", str(path))
    sources = load_sources()
    # 内置字段保留、用户字段覆盖
    assert sources["spacemit_community"].label_en
    assert sources["spacemit_community"].enabled is True
    assert sources["internal_wiki"].label_en == "内部 Wiki"
    assert [src.id for src in enabled_sources("remote_vector")] == [
        "spacemit_community",
        "internal_wiki",
    ]


def test_env_overrides_single_source(tmp_path, monkeypatch):
    monkeypatch.setenv("BIBIT_KB_SOURCES_FILE", str(tmp_path / "none.yaml"))
    monkeypatch.setenv("BIBIT_KB_SPACEMIT_COMMUNITY_ENABLED", "1")
    monkeypatch.setenv("BIBIT_KB_SPACEMIT_COMMUNITY_ENDPOINT", "https://kb.example.com/s")
    monkeypatch.setenv("BIBIT_KB_SPACEMIT_COMMUNITY_TOKEN_ENV", "KB_TOKEN")
    monkeypatch.setenv("KB_TOKEN", "secret")
    source = load_sources()["spacemit_community"]
    assert source.enabled is True
    assert source.endpoint == "https://kb.example.com/s"
    assert resolve_token(source) == "secret"


def test_env_bool_is_parsed_strictly(tmp_path, monkeypatch):
    monkeypatch.setenv("BIBIT_KB_SOURCES_FILE", str(tmp_path / "none.yaml"))
    monkeypatch.setenv("BIBIT_KB_SPACEMIT_COMMUNITY_ENABLED", "maybe")
    with pytest.raises(KnowledgeSourceError) as excinfo:
        load_sources()
    assert excinfo.value.code == "kb_config_invalid"
    assert "布尔值" in str(excinfo.value)


def test_enabled_remote_source_requires_endpoint(tmp_path, monkeypatch):
    path = _write(
        tmp_path / "kb.yaml",
        [
            {
                "id": "bad",
                "kind": "remote_vector",
                "label": "坏来源",
                "enabled": True,
            }
        ],
    )
    monkeypatch.setenv("BIBIT_KB_SOURCES_FILE", str(path))
    with pytest.raises(KnowledgeSourceError) as excinfo:
        load_sources()
    assert excinfo.value.code == "kb_config_invalid"
    assert "endpoint" in str(excinfo.value)


def test_unknown_field_and_duplicate_id_are_rejected(tmp_path, monkeypatch):
    path = _write(
        tmp_path / "kb.yaml",
        [{"id": "a", "kind": "web_search", "label": "A", "surprise": 1}],
    )
    monkeypatch.setenv("BIBIT_KB_SOURCES_FILE", str(path))
    with pytest.raises(KnowledgeSourceError):
        load_sources()

    path = _write(
        tmp_path / "kb2.yaml",
        [
            {"id": "a", "kind": "web_search", "label": "A"},
            {"id": "a", "kind": "web_search", "label": "A2"},
        ],
    )
    monkeypatch.setenv("BIBIT_KB_SOURCES_FILE", str(path))
    with pytest.raises(KnowledgeSourceError) as excinfo:
        load_sources()
    assert "重复" in str(excinfo.value)


def test_web_search_source_rejects_endpoint(tmp_path, monkeypatch):
    path = _write(
        tmp_path / "kb.yaml",
        [
            {
                "id": "web",
                "kind": "web_search",
                "label": "网络搜索",
                "endpoint": "https://search.example.com",
            }
        ],
    )
    monkeypatch.setenv("BIBIT_KB_SOURCES_FILE", str(path))
    with pytest.raises(KnowledgeSourceError):
        load_sources()


def test_cache_invalidates_on_file_change(tmp_path, monkeypatch):
    path = _write(
        tmp_path / "kb.yaml",
        [
            {
                "id": "spacemit_community",
                "kind": "remote_vector",
                "label": "进迭社区知识库",
                "enabled": False,
            }
        ],
    )
    monkeypatch.setenv("BIBIT_KB_SOURCES_FILE", str(path))
    assert load_sources()["spacemit_community"].enabled is False

    import os
    import time

    _write(
        path,
        [
            {
                "id": "spacemit_community",
                "kind": "remote_vector",
                "label": "进迭社区知识库",
                "enabled": True,
                "endpoint": "https://kb.example.com/s",
            }
        ],
    )
    os.utime(path, (time.time() + 2, time.time() + 2))
    assert load_sources()["spacemit_community"].enabled is True


def test_config_paths(tmp_path, monkeypatch):
    monkeypatch.delenv("BIBIT_KB_SOURCES_FILE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert user_config_path() == tmp_path / ".config" / "bibit-intent" / DEFAULT_CONFIG_FILENAME
    monkeypatch.setenv("BIBIT_KB_SOURCES_FILE", "~/custom.yaml")
    assert user_config_path() == tmp_path / "custom.yaml"
    assert default_config_path() == resource_path(DEFAULT_CONFIG_FILENAME)
