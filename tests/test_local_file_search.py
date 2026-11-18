"""本地文件检索触发词 MVP 测试：decide_local_intent 直通 + router 转发。"""
import sys
from pathlib import Path
from unittest.mock import patch
import json
import pytest

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


@pytest.fixture(autouse=True)
def search_policy(tmp_path, monkeypatch):
    from bibit_intent import file_results
    monkeypatch.setattr(file_results, "load_settings", lambda port=None: {
        "index_dirs": [str(tmp_path)], "exclude_patterns": [],
        "indexable_types": ["md"], "max_depth": 3,
    })


def test_router_forwards_to_filesearch_service(tmp_path):
    from bibit_intent.execution.system_tools import local_file_search
    r = Router.__new__(Router)
    r._filesearch_port = 18700
    for name in ("a.md", "b.md"):
        (tmp_path / name).write_text("正文 " + name)
    with patch("httpx.post") as fake_post:
        fake_post.return_value.raise_for_status.return_value = None
        fake_post.return_value.json.return_value = {
            "code": 0,
            "data": {"results": [
                {"fileName": "a.md", "filePath": str(tmp_path / "a.md"), "confidence": 1.0},
                {"fileName": "b.md", "filePath": str(tmp_path / "b.md"), "confidence": 0.5},
            ], "total": 2},
        }
        res = r._local_file_search("GTK")
        assert res.ok
        assert fake_post.call_args.kwargs["trust_env"] is False
        assert "相对匹配度 100%" in res.message
        assert "按相关度排序，第 1 位最相关" in res.message
        assert "\n\n第 1 位：a.md" in res.message
        assert "\n\n第 2 位：b.md" in res.message
        assert "\n\n可点击文件名" in res.message
        assert "相对匹配度 50%" in res.message
        assert "读取本地文件" in res.message
        assert res.detail == local_file_search("GTK").detail
        assert json.loads(res.detail)["entries"][0]["snippet"] == "正文 a.md"


def test_router_empty_results():
    r = Router.__new__(Router)
    r._filesearch_port = 18700
    with patch("httpx.post") as fake_post:
        fake_post.return_value.raise_for_status.return_value = None
        fake_post.return_value.json.return_value = {"code": 0, "data": {"results": []}}
        res = r._local_file_search("xyz")
        assert not res.ok
        assert "没有找到" in res.message
        assert json.loads(res.detail)["entries"] == []


def test_router_service_down():
    r = Router.__new__(Router)
    r._filesearch_port = 18700
    with patch("httpx.post", side_effect=ConnectionError("refused")):
        res = r._local_file_search("GTK")
        assert not res.ok
        assert "服务不可用" in res.message


def test_business_rejection_is_not_reported_as_service_down():
    """服务活着但业务拒绝（真实设备对 query="*" / "文件" 回 code=1001
    「query 不能为空」）时，必须报"查询被拒绝 + 换更具体的词"，
    不能报"服务不可用"——后者会让模型以为服务挂了，转去开终端扫盘绕路。"""
    from bibit_intent.execution.system_tools import local_file_search

    with patch("httpx.post") as fake_post:
        fake_post.return_value.raise_for_status.return_value = None
        fake_post.return_value.json.return_value = {
            "code": 1001, "message": "query 不能为空", "data": None,
        }
        for bad_query in ("*", "所有格式的文件", "文件"):
            res = local_file_search(bad_query)
            assert not res.ok, bad_query
            assert "服务不可用" not in res.message, bad_query
            assert "query 不能为空" in res.message, bad_query
            # 缺参语义：调用方知道该换词重试，而不是排查服务
            assert res.need_param == "query", bad_query


def test_empty_query_is_missing_param_not_service_failure():
    from bibit_intent.execution.system_tools import local_file_search

    res = local_file_search("")
    assert not res.ok
    assert "服务不可用" not in res.message
    assert res.need_param == "query"


def test_file_search_description_forbids_wildcard_and_enumeration():
    """描述必须写明：关键词检索、拒绝通配符、不能用来枚举全部文件。

    实测事故：用户说「查找所有格式的文件」，云端模型唯一能选的文件工具就是
    file_search，旧描述只说 "Search for files ... find, locate, or search"，
    模型据此传了 {"query": "*"} 让服务端拒绝，又因错误归因被判成服务不可用。
    """
    desc = _reg.get("file_search").mcp_schema()["function"]["description"]
    assert "KEYWORD" in desc
    assert "not a wildcard match" in desc
    assert "not a directory listing" in desc
    # 不许拿占位符硬调（原句 "Do not use this tool to list or count every file"
    # 已改成下面这句：枚举不是禁止，而是换条路，见 offers_both_real_paths）
    assert "Do not pass a placeholder and do not pretend this tool can enumerate" in desc
    # 参数级说明要拼进描述（registry.py 的 hint 通道）
    assert "不要用 * 或「所有文件」这类泛词" in desc


def test_file_search_description_offers_both_real_paths():
    """做不到时给出两条真实可行的路，让用户选，而不是拒绝或编造。

    实测事故（K3，改动后复测）：只写「不能枚举、不许编造替代」，模型就答
    「我会用打开文件夹的方式帮你定位」——open_folder 只能打开目录、不能筛选。
    真正的第二选项是 Runtime 的 terminal（config.yaml 的 presets.tools 里
    有 terminal/process/read_file/search_files），它能跑 find；
    但它不在 bibit-intent 的工具面里，所以必须由本描述点名，
    否则模型只看到 file_search 一个选项，只能拒绝或编造。
    """
    desc = _reg.get("file_search").mcp_schema()["function"]["description"]
    # 意图定性：不是不可能，是选错工具
    assert "GENERIC ENUMERATION" in desc
    assert "it is the wrong tool, not an impossible request" in desc
    assert "Offer the two paths that actually work" in desc
    # 路径一：真·枚举走 Runtime terminal，并要求说明代价
    assert "Runtime terminal tool" in desc
    assert "only thing on the device that can walk the filesystem" in desc
    assert "scans paths outside the index" in desc
    assert "find or ls pipeline that groups by extension" in desc
    # 路径二：索引关键词检索及其限制
    assert "relevance search over the indexed directories only" in desc
    assert "unable to prove what exists on the device" in desc
    assert "asks" in desc or "ask for one concrete anchor" in desc
    assert "wait for the next turn" in desc
    # 对比一句，让用户能选
    assert "enumeration is exhaustive but bypasses the index" in desc
    assert "keyword search is fast and index-scoped but needs a term" in desc


def test_file_search_description_still_denies_fake_fallbacks():
    """允许 find ≠ 允许拿 open_folder / disk_dir_usage 冒充枚举。"""
    desc = _reg.get("file_search").mcp_schema()["function"]["description"]
    assert "Never offer open_folder as a way to locate or filter files" in desc
    assert "disk_dir_usage (directory size only)" in desc
    assert "file_info (one known path)" in desc
    assert "never claim a result proves what exists outside the index" in desc


def test_requested_phrasings_in_description_actually_reach_file_search():
    """描述里教用户的措辞必须真能直通 file_search，否则又是在承诺做不到的事。

    实测（decide_local_intent）：方位词是硬条件——
      「本地搜索关于合同的txt」→ file_search
      「搜索关于合同的pdf」（无方位词）→ escalate unsupported_control
    所以描述里举的例子只能取带方位词的形式。
    """
    desc = _reg.get("file_search").mcp_schema()["function"]["description"]
    # 描述必须把方位词规则讲出来，而不是只丢几个例子
    for word in ("本地", "本机", "电脑上", "电脑里", "我电脑"):
        assert word in desc, word
    assert "search verb" in desc
    # 描述里出现的示例措辞逐条回归到路由层
    for text in (
        "本地搜索关于合同的txt",
        "本地搜索关于周报的pdf",
        "本机找一下会议纪要",
        "电脑上查一下关于2026的md",
    ):
        assert text.replace(" ", "") in desc.replace(" ", ""), text
        plan = _plan(text)
        assert plan.action is IntentAction.RULE_CALL, text
        assert plan.tool_name == "file_search", text


def test_description_only_names_indexable_formats():
    """只能承诺索引真能装的格式；mp4/zip 这类不在 indexable_types 里。"""
    desc = _reg.get("file_search").mcp_schema()["function"]["description"]
    for fmt in ("txt", "md", "pdf", "docx", "xlsx", "json"):
        assert fmt in desc, fmt
    # 明确告知索引不含媒体/压缩包，避免又去承诺 mp4/zip
    assert "do not name media or archive types the index does not carry" in desc


def test_open_folder_description_denies_file_lookup():
    """open_folder 必须自己否认"能定位文件"，否则模型会拿它当 find 用。"""
    desc = _reg.get("open_folder").mcp_schema()["function"]["description"]
    assert "cannot find, filter, list or enumerate the files inside it" in desc
    assert "never offer it as a way to locate files by type or name" in desc
