"""本地文件检索触发词 MVP 测试：decide_local_intent 直通 + router 转发。"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.routing.intent import IntentAction, decide_local_intent
from bibit_intent.routing.rule_engine import RuleEngine
from bibit_intent.routing.router import Router

_reg = ToolRegistry()
_rule = RuleEngine(_reg)


def _plan(text):
    return decide_local_intent(text, _rule, _reg)


def test_trigger_word_routes_to_file_search():
    for text in (
        "本地搜索一下GTK相关的文档",
        "帮我本地搜索一下周报",
        "本地文档里找一下会议纪要",
        "本地资料搜索GTK",
    ):
        plan = _plan(text)
        assert plan.action is IntentAction.RULE_CALL, text
        assert plan.tool_name == "file_search", text
        assert plan.slots.get("query") == text


def test_no_trigger_word_escalates():
    for text in (
        "帮我找一下GTK相关的文档",
        "帮我找一下桌面上的ppt",
        "搜索AI资讯并生成可视化网页报告",
    ):
        plan = _plan(text)
        assert plan.action is IntentAction.ESCALATE, text


def test_rule_engine_fills_trigger_word_gap():
    """前置层 _LOCAL_SEARCH_RE 要求方位词与检索动词紧邻，漏掉中间隔
    礼貌/修饰词的口语；规则引擎补上，仍命中 file_search（整句透传）。"""
    for text in (
        "电脑上帮我找一下去年的照片",
        "本机里给我搜搜GTK相关的代码",
        "电脑里帮我查一下有什么视频",
    ):
        plan = _plan(text)
        assert plan.action is IntentAction.RULE_CALL, text
        assert plan.tool_name == "file_search", text
        # 走规则引擎（①），而非前置层触发词直通
        assert plan.matched_pattern != "local_search_trigger", text
        # query 不设命名捕获组，slots 为空，由 router 整句透传（与前置层直通一致）
        assert plan.slots.get("query") is None, text


def test_trigger_word_does_not_break_other_intents():
    assert _plan("今天天气怎么样").action is IntentAction.TO_MODEL
    assert _plan("音量调到60").tool_name == "set_volume"
    assert _plan("解释一下量子纠缠").action is IntentAction.ESCALATE


def test_router_forwards_to_filesearch_service():
    r = Router.__new__(Router)
    r._filesearch_port = 18700
    with patch("httpx.post") as fake_post:
        fake_post.return_value.raise_for_status.return_value = None
        fake_post.return_value.json.return_value = {
            "code": 0,
            "data": {
                "results": [
                    {"fileName": "a.md", "filePath": "/d/a.md", "confidence": 1.0},
                    {"fileName": "b.md", "filePath": "/d/b.md", "confidence": 0.5},
                ]
            },
        }
        res = r._local_file_search("GTK")
        assert res.ok is True
        assert "a.md（100%）" in res.message
        assert "b.md（50%）" in res.message
        # 直通回答只回元数据：要明确下一步怎么拿正文（K13），否则停在文件名上
        assert "读取本地文件" in res.message


def test_router_empty_results():
    r = Router.__new__(Router)
    r._filesearch_port = 18700
    with patch("httpx.post") as fake_post:
        fake_post.return_value.raise_for_status.return_value = None
        fake_post.return_value.json.return_value = {"code": 0, "data": {"results": []}}
        res = r._local_file_search("xyz")
        assert res.ok is False
        assert "没有找到" in res.message


def test_router_service_down():
    r = Router.__new__(Router)
    r._filesearch_port = 18700
    with patch("httpx.post", side_effect=ConnectionError("refused")):
        res = r._local_file_search("GTK")
        assert res.ok is False
        assert "服务不可用" in res.message
