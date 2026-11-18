"""本地搜索的卡片正文与模型可见文本必须分开。"""
import json
from unittest.mock import patch

import pytest

from bibit_intent import file_results
from bibit_intent.execution.system_tools import local_file_search
from bibit_intent.mcp_server.server import create_server
from bibit_intent.pipeline import _format_reply, _readonly_detail


@pytest.fixture
def search(tmp_path, monkeypatch):
    path = tmp_path / "private.txt"
    path.write_text("SYNTHETIC_PRIVATE_BODY_72814")
    monkeypatch.setattr(file_results, "load_settings", lambda port=None: {
        "index_dirs": [str(tmp_path)], "exclude_patterns": [],
        "indexable_types": ["txt"], "max_depth": 3,
    })
    with patch("httpx.post") as post:
        post.return_value.raise_for_status.return_value = None
        post.return_value.json.return_value = {
            "code": 0, "data": {"results": [{"filePath": str(path)}]},
        }
        yield


def test_direct_search_keeps_body_only_in_structured_card_metadata(search):
    result = local_file_search("probe")
    card = _readonly_detail(result)
    assert card is not None
    assert card["entries"][0]["snippet"] == "SYNTHETIC_PRIVATE_BODY_72814"
    assert "SYNTHETIC_PRIVATE_BODY_72814" not in _format_reply(result)
    assert "bibit.file-results" not in _format_reply(result)


@pytest.mark.asyncio
async def test_mcp_search_keeps_body_in_private_metadata_not_model_content(search):
    result = await create_server().call_tool("file_search", {"query": "probe"})
    assert "SYNTHETIC_PRIVATE_BODY_72814" not in json.dumps(
        result.model_dump(by_alias=True, exclude={"meta"}), ensure_ascii=False,
    )
    assert result.meta["bibit.file-results"]["entries"][0]["snippet"] == "SYNTHETIC_PRIVATE_BODY_72814"
    assert result.isError is False
    payload = json.loads(result.content[0].text)
    assert payload["ok"] is True
    assert json.loads(payload["detail"])["entries"][0]["snippet"] is None


@pytest.mark.parametrize("raw", [
    "bad json", "[]", '{"ok": true, "detail": "unknown"}',
    json.dumps({"ok": True, "detail": json.dumps({
        "schema": "bibit.file-results.v1", "entries": ["SYNTHETIC_PRIVATE_BODY_72814"],
    })}),
    json.dumps({"ok": False, "message": "无结果", "detail": "SYNTHETIC_PRIVATE_BODY_72814"}),
])
def test_invalid_or_failed_mcp_search_never_releases_private_detail(raw):
    from mcp.types import TextContent
    from bibit_intent.mcp_server.server import _file_search_ui_result

    result = _file_search_ui_result([TextContent(type="text", text=raw)])
    assert result.isError is True
    assert not result.meta
    assert "SYNTHETIC_PRIVATE_BODY_72814" not in result.model_dump_json(by_alias=True)
