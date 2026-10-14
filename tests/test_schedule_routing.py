"""
tests/test_schedule_routing.py — 日程 Skill 路由回归测试（全离线）

锁定 schedule skill（lxqt-schedule 联动）在规则引擎层的行为：
  1. 关键词/代表句能高置信命中 schedule_add / schedule_remove / schedule_list；
  2. 纯陈述（不带"提醒/日程/闹钟/备忘"锚点）不注入日程工具；
  3. 删除类句子的 id 槽位正确抽取；
  4. handlers.py 的时间解析 parse_when 对典型自然语言时间成立。

运行： pytest tests/test_schedule_routing.py -q
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bibit_intent.resources import resource_path
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.routing.rule_engine import RuleEngine
from bibit_intent.execution.skill_loader import load_skills


@pytest.fixture(scope="module")
def registry() -> ToolRegistry:
    reg = ToolRegistry(resource_path("tools.yaml"))
    load_skills(resource_path("skills"), reg, {})
    return reg


@pytest.fixture(scope="module")
def rule(registry: ToolRegistry) -> RuleEngine:
    return RuleEngine(registry)


@pytest.fixture(scope="module")
def parse_when():
    """加载 schedule skill 的 handlers.py，取出 parse_when 纯函数。"""
    handler = (
        resource_path("skills")
        / "schedule"
        / "handlers.py"
    )
    spec = importlib.util.spec_from_file_location("_test_schedule_handlers", handler)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module.__name__] = module
    spec.loader.exec_module(module)
    return module.parse_when


# (输入, 期望工具)
ROUTE_CASES = [
    ("创建日程", "schedule_add"),
    ("新建日程", "schedule_add"),
    ("提醒我明天上午九点开会", "schedule_add"),
    ("帮我创建一个日程", "schedule_add"),
    ("定个提醒", "schedule_add"),
    ("删除日程", "schedule_remove"),
    ("取消提醒", "schedule_remove"),
    ("取消明天下午三点的会议", "schedule_remove"),
    ("删除明天下午三点的会议", "schedule_remove"),
    ("关掉闹钟", "schedule_remove"),
    ("查看日程", "schedule_list"),
    ("我的日程有哪些", "schedule_list"),
    ("查看提醒", "schedule_list"),
    ("我的提醒有哪些", "schedule_list"),
]


@pytest.mark.parametrize("text,tool", ROUTE_CASES)
def test_schedule_routes_to_own_tool(rule: RuleEngine, text: str, tool: str) -> None:
    m = rule.match(text)
    assert m is not None, f"{text!r} 未命中任何工具"
    assert m.tool_name == tool, f"{text!r} -> {m.tool_name}（期望 {tool}）"
    assert m.confidence >= rule.CONFIDENCE_THRESHOLD


def test_schedule_keywords_not_hijacked(rule: RuleEngine) -> None:
    """schedule 工具的关键词不能被其它工具以高置信抢占。"""
    import yaml

    manifest = resource_path("skills") / "schedule" / "skill.yaml"
    meta = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    for tool_name, tool_data in (meta.get("tools") or {}).items():
        rule_cfg = (tool_data.get("routes") or {}).get("rule") or {}
        for kw in rule_cfg.get("keywords") or []:
            m = rule.match(kw)
            assert m is None or m.tool_name == tool_name or m.confidence < rule.CONFIDENCE_THRESHOLD, (
                f"keyword={kw!r} 被 {m.tool_name} conf={m.confidence} 抢占"
            )


def test_schedule_update_routes(rule: RuleEngine) -> None:
    """修改/调整日程意图应命中 schedule_update。"""
    for text in [
        "修改日程id 3 改到明天下午四点",
        "把id 3的日程改到明天下午四点",
        "修改我的日程",
        "调整提醒",
        "更改日程",
    ]:
        m = rule.match(text)
        assert m is not None, f"{text!r} 未命中任何工具"
        assert m.tool_name == "schedule_update", f"{text!r} -> {m.tool_name}"
        assert m.confidence >= rule.CONFIDENCE_THRESHOLD


def test_schedule_update_extracts_id_and_when(rule: RuleEngine) -> None:
    m = rule.match("修改日程id 3 改到明天下午四点")
    assert m is not None
    assert m.tool_name == "schedule_update"
    assert str(m.slots.get("id")) == "3"
    assert "明天下午四点" in str(m.slots.get("when", ""))


def test_schedule_update_not_claim_other_update(rule: RuleEngine) -> None:
    """其它“修改/调整”类指令不应被 schedule_update 抢占。"""
    for text in [
        "修改用户密码",
        "调整音量到50",
        "修改wifi密码",
        "更新软件",
    ]:
        m = rule.match(text)
        assert m is None or m.tool_name != "schedule_update" or m.confidence < rule.CONFIDENCE_THRESHOLD, (
            f"{text!r} 被 schedule_update 抢占：{m.tool_name if m else None} conf={m.confidence if m else None}"
        )


def test_schedule_add_rejects_bare_statement(rule: RuleEngine) -> None:
    """纯陈述"明天开会"不带提醒/日程锚点，不应被 schedule_add 规则吞掉。"""
    m = rule.match("明天开会")
    assert m is None or m.tool_name != "schedule_add"


def test_schedule_remove_extracts_id(rule: RuleEngine) -> None:
    m = rule.match("删除提醒id 3")
    assert m is not None
    assert m.tool_name == "schedule_remove"
    assert str(m.slots.get("id")) == "3"


def test_schedule_remove_extracts_meeting_title(rule: RuleEngine) -> None:
    m = rule.match("取消明天下午三点的会议")
    assert m is not None
    assert m.tool_name == "schedule_remove"
    assert m.confidence >= rule.CONFIDENCE_THRESHOLD
    assert "明天下午三点" in str(m.slots.get("title", ""))


def _load_handlers(module_name: str):
    handler = resource_path("skills") / "schedule" / "handlers.py"
    spec = importlib.util.spec_from_file_location(module_name, handler)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module.__name__] = module
    spec.loader.exec_module(module)
    return module


def test_schedule_remove_multiple_matches_asks_for_id(monkeypatch) -> None:
    module = _load_handlers("_test_schedule_handlers_rm_multi")
    fake = {
        "schedules": [
            {"id": 1, "title": "下午三点的会议", "due_epoch": 100},
            {"id": 2, "title": "下午三点的周会", "due_epoch": 200},
        ]
    }
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=10):
        calls.append(cmd)
        if cmd[:2] == [module._CTL, "list"]:
            return module.ExecResult(True, "ok", json.dumps(fake), detail_format="text")
        return module.ExecResult(True, "ok", "", detail_format="text")

    monkeypatch.setattr(module, "_run", fake_run)
    res = module.schedule_remove(title="下午三点")

    assert res.ok is False
    assert "匹配" in res.message
    assert "#1" in res.detail and "#2" in res.detail
    assert not any(cmd[:2] == [module._CTL, "remove"] for cmd in calls)


def test_schedule_remove_exact_match_prefers_id(monkeypatch) -> None:
    module = _load_handlers("_test_schedule_handlers_rm_exact")
    fake = {
        "schedules": [
            {"id": 1, "title": "会议", "due_epoch": 100},
            {"id": 2, "title": "项目会议", "due_epoch": 200},
        ]
    }
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=10):
        calls.append(cmd)
        if cmd[:2] == [module._CTL, "list"]:
            return module.ExecResult(True, "ok", json.dumps(fake), detail_format="text")
        return module.ExecResult(True, "ok", "", detail_format="text")

    monkeypatch.setattr(module, "_run", fake_run)
    res = module.schedule_remove(title="会议")

    assert res.ok is True
    remove_calls = [cmd for cmd in calls if cmd[:2] == [module._CTL, "remove"]]
    assert remove_calls == [[module._CTL, "remove", "1"]]


def test_schedule_update_repeat_only(monkeypatch) -> None:
    module = _load_handlers("_test_schedule_handlers_upd_repeat")
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=10):
        calls.append(cmd)
        return module.ExecResult(True, "ok", "", detail_format="text")

    monkeypatch.setattr(module, "_run", fake_run)
    res = module.schedule_update(id="3", repeat="weekly")

    assert res.ok is True
    assert calls == [[module._CTL, "update", "3", "--repeat", "weekly"]]


def test_schedule_update_clear_repeat(monkeypatch) -> None:
    module = _load_handlers("_test_schedule_handlers_upd_clear")
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=10):
        calls.append(cmd)
        return module.ExecResult(True, "ok", "", detail_format="text")

    monkeypatch.setattr(module, "_run", fake_run)
    res = module.schedule_update(id="3", clear_repeat=True)

    assert res.ok is True
    assert calls == [[module._CTL, "update", "3", "--clear-repeat"]]


def test_schedule_update_repeat_conflict() -> None:
    module = _load_handlers("_test_schedule_handlers_upd_conflict")
    res = module.schedule_update(id="3", repeat="weekly", clear_repeat=True)

    assert res.ok is False
    assert "不能同时" in res.message


def test_schedule_update_by_title(monkeypatch) -> None:
    module = _load_handlers("_test_schedule_handlers_upd_title")
    fake = {"schedules": [{"id": 7, "title": "会议", "due_epoch": 100}]}
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=10):
        calls.append(cmd)
        if cmd[:2] == [module._CTL, "list"]:
            return module.ExecResult(True, "ok", json.dumps(fake), detail_format="text")
        return module.ExecResult(True, "ok", "", detail_format="text")

    monkeypatch.setattr(module, "_run", fake_run)
    res = module.schedule_update(title="会议", when="明天下午四点")

    assert res.ok is True
    update_calls = [c for c in calls if c[:2] == [module._CTL, "update"]]
    assert update_calls and update_calls[0][2] == "7"


def test_schedule_update_multiple_titles_asks_id(monkeypatch) -> None:
    module = _load_handlers("_test_schedule_handlers_upd_multi")
    fake = {
        "schedules": [
            {"id": 1, "title": "下午三点的会议", "due_epoch": 100},
            {"id": 2, "title": "下午三点的周会", "due_epoch": 200},
        ]
    }
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=10):
        calls.append(cmd)
        if cmd[:2] == [module._CTL, "list"]:
            return module.ExecResult(True, "ok", json.dumps(fake), detail_format="text")
        return module.ExecResult(True, "ok", "", detail_format="text")

    monkeypatch.setattr(module, "_run", fake_run)
    res = module.schedule_update(title="下午三点", when="明天")

    assert res.ok is False
    assert "匹配" in res.message
    assert "#1" in res.detail and "#2" in res.detail
    assert not any(c[:2] == [module._CTL, "update"] for c in calls)


def test_schedule_update_echoes_changes(monkeypatch) -> None:
    module = _load_handlers("_test_schedule_handlers_upd_echo")

    def fake_run(cmd, timeout=10):
        return module.ExecResult(True, "ok", "", detail_format="text")

    monkeypatch.setattr(module, "_run", fake_run)
    res = module.schedule_update(id="3", when="明天下午四点", repeat="weekly")

    assert res.ok is True
    assert "时间 →" in res.detail
    assert "每周" in res.detail


def test_schedule_list_extracts_date(rule: RuleEngine) -> None:
    for text, expected in [
        ("查看明天的日程", "明天"),
        ("查看后天的日程", "后天"),
        ("查看今天的日程", "今天"),
        ("我明天有什么安排", "明天"),
        ("明天有什么日程", "明天"),
        ("查看周一的日程", "周一"),
    ]:
        m = rule.match(text)
        assert m is not None, f"{text!r} 未命中"
        assert m.tool_name == "schedule_list", f"{text!r} -> {m.tool_name}"
        assert str(m.slots.get("when", "")) == expected, f"{text!r} when={m.slots}!"


def test_date_from_when_relative() -> None:
    module = _load_handlers("_test_schedule_handlers_date")
    now = datetime(2026, 10, 10, 12, 0, 0)
    assert module._date_from_when("明天", now) == datetime(2026, 10, 11).date()
    assert module._date_from_when("后天", now) == datetime(2026, 10, 12).date()
    assert module._date_from_when("今天", now) == datetime(2026, 10, 10).date()
    assert module._date_from_when("", now) is None


def test_schedule_list_filters_by_when(monkeypatch) -> None:
    module = _load_handlers("_test_schedule_handlers_list_when")
    target = datetime(2026, 10, 11).date()
    fake = {
        "schedules": [
            {"id": 1, "title": "上午十点周会", "due_epoch": int(datetime(2026, 10, 11, 10, 0).timestamp()), "repeat": ""},
            {"id": 2, "title": "后天评审会", "due_epoch": int(datetime(2026, 10, 12, 10, 0).timestamp()), "repeat": ""},
        ]
    }

    def fake_run(cmd, timeout=10):
        return module.ExecResult(True, "ok", json.dumps(fake), detail_format="text")

    monkeypatch.setattr(module, "_run", fake_run)
    monkeypatch.setattr(module, "_date_from_when", lambda when, now=None: target)
    res = module.schedule_list(when="明天")

    assert res.ok is True
    assert "#1" in res.detail
    assert "#2" not in res.detail


# parse_when 相对时间用例用固定基准，避免时区/当天边界抖动
_PARSE_CASES = [
    ("明天下午三点", datetime(2026, 10, 8, 10, 0, 0), None),   # 明天 15:00
    ("十分钟后", datetime(2026, 10, 8, 10, 0, 0), None),       # +10min
    ("半小时后", datetime(2026, 10, 8, 10, 0, 0), None),       # +30min
    ("2026-10-09 15:00", datetime(2026, 10, 8, 10, 0, 0), None),
    ("明天 08:30", datetime(2026, 10, 8, 10, 0, 0), None),
    ("现在", datetime(2026, 10, 8, 10, 0, 0), None),
    ("", datetime(2026, 10, 8, 10, 0, 0), None),               # 空 → None
]


@pytest.mark.parametrize("text,now,_expected", _PARSE_CASES)
def test_parse_when_returns_timestamp(parse_when, text: str, now: datetime, _expected) -> None:
    ts = parse_when(text, now)
    if text == "":
        assert ts is None
        return
    assert isinstance(ts, int) and ts > 0, f"{text!r} 应解析出时间戳，实际 {ts!r}"


def test_parse_when_relative_minutes(parse_when) -> None:
    now = datetime(2026, 10, 8, 10, 0, 0)
    assert parse_when("十分钟后", now) == int((now.replace(hour=10, minute=10)).timestamp())


def test_parse_when_tomorrow_afternoon(parse_when) -> None:
    now = datetime(2026, 10, 8, 10, 0, 0)
    expected = datetime(2026, 10, 9, 15, 0, 0)
    assert parse_when("明天下午三点", now) == int(expected.timestamp())


_NORMALIZE_CASES = [
    ("明天下午三点的会议", "下午三点的会议"),
    ("后天开会", "开会"),
    ("今天提交报告", "提交报告"),
    ("大后天体检", "体检"),
    ("明后天整理资料", "整理资料"),
    ("明日下午四点评审", "下午四点评审"),
    ("周五例会", "周五例会"),  # 无相对词，保持不变
    ("开会", "开会"),
]


@pytest.mark.parametrize("title,expected", _NORMALIZE_CASES)
def test_normalize_title_strips_relative_date(title: str, expected: str) -> None:
    handler = resource_path("skills") / "schedule" / "handlers.py"
    spec = importlib.util.spec_from_file_location("_test_schedule_handlers_norm", handler)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module.__name__] = module
    spec.loader.exec_module(module)

    assert module._normalize_title(title) == expected



# (输入, 不应被抢占的 schedule 工具)
CONFUSED_CASES = [
    # 创建类混淆：纯陈述 / 其他系统控制动作不应被 schedule_add 抢走
    ("明天开会", "schedule_add"),
    ("查看明天天气", "schedule_add"),
    ("打开日程应用", "schedule_add"),
    ("打开提醒应用", "schedule_add"),
    ("闹钟声音小一点", "schedule_add"),
    ("设置wifi", "schedule_add"),
    # 删除类混淆：删除用户/软件、关闭蓝牙/静音等不应被 schedule_remove 抢走
    ("删除用户test", "schedule_remove"),
    ("删除软件vim", "schedule_remove"),
    ("关闭蓝牙", "schedule_remove"),
    ("删除会议记录", "schedule_remove"),
    ("取消配对蓝牙", "schedule_remove"),
    ("取消静音", "schedule_remove"),
    ("关掉无线网络", "schedule_remove"),
    # 查看类混淆：日历应用/通知中心/会议记录等不应被 schedule_list 抢走
    ("查看日历", "schedule_list"),
    ("打开日历", "schedule_list"),
    ("显示通知", "schedule_list"),
    ("查看会议记录", "schedule_list"),
    ("查一下待办", "schedule_list"),
]


@pytest.mark.parametrize("text,not_tool", CONFUSED_CASES)
def test_schedule_not_claim_confusable(rule: RuleEngine, text: str, not_tool: str) -> None:
    """混淆句不能以高置信被日程工具抢占。"""
    m = rule.match(text)
    assert m is None or m.tool_name != not_tool or m.confidence < rule.CONFIDENCE_THRESHOLD, (
        f"{text!r} 被日程工具 {not_tool} 抢占：{m.tool_name} conf={m.confidence}"
    )


# ── 范围查询 / 时间冲突 / 日程冲突标记 ───────────────────────────────

def test_schedule_list_extracts_range(rule: RuleEngine) -> None:
    for text, expected in [
        ("查看本周的日程", "本周"),
        ("查看下个月的日程", "下个月"),
        ("查看未来三天的日程", "未来三天"),
        ("我下周有什么日程", "下周"),
        ("查看从10号到12号的日程", "从10号到12号"),
        ("我下个月有哪些日程", "下个月"),
    ]:
        m = rule.match(text)
        assert m is not None, f"{text!r} 未命中"
        assert m.tool_name == "schedule_list", f"{text!r} -> {m.tool_name}"
        assert str(m.slots.get("range", "")) == expected, f"{text!r} range={m.slots}"


def test_range_from_when_windows() -> None:
    module = _load_handlers("_test_schedule_handlers_range_win")
    now = datetime(2026, 10, 10, 12, 0, 0)

    start, end = module._range_from_when("本周", now)
    assert start == int(datetime(2026, 10, 5).timestamp())
    assert end == int(datetime(2026, 10, 12).timestamp())

    start, end = module._range_from_when("未来三天", now)
    assert start == int(now.timestamp())
    assert end == int((now + timedelta(days=3)).timestamp())

    start, end = module._range_from_when("从10号到12号", now)
    assert start == int(datetime(2026, 10, 10).timestamp())
    assert end == int(datetime(2026, 10, 13).timestamp())

    assert module._range_from_when("随便一个词", now) is None


def test_schedule_list_filters_by_range(monkeypatch) -> None:
    module = _load_handlers("_test_schedule_handlers_list_range")
    fake = {
        "schedules": [
            {"id": 1, "title": "本周周会", "due_epoch": int(datetime(2026, 10, 6, 10, 0).timestamp()), "repeat": ""},
            {"id": 2, "title": "下周评审", "due_epoch": int(datetime(2026, 10, 13, 10, 0).timestamp()), "repeat": ""},
        ]
    }

    def fake_run(cmd, timeout=10):
        return module.ExecResult(True, "ok", json.dumps(fake), detail_format="text")

    monkeypatch.setattr(module, "_run", fake_run)
    monkeypatch.setattr(
        module, "_range_from_when",
        lambda when, now=None: (int(datetime(2026, 10, 5).timestamp()),
                                int(datetime(2026, 10, 12).timestamp())),
    )
    res = module.schedule_list(range="本周")

    assert res.ok is True
    assert "#1" in res.detail
    assert "#2" not in res.detail


def test_schedule_list_conflict_marker(monkeypatch) -> None:
    module = _load_handlers("_test_schedule_handlers_list_conflict")
    fake = {
        "schedules": [
            {"id": 1, "title": "评审会", "due_epoch": 100, "repeat": "", "conflict": True},
            {"id": 2, "title": "周会", "due_epoch": 200, "repeat": "", "conflict": False},
        ]
    }

    def fake_run(cmd, timeout=10):
        return module.ExecResult(True, "ok", json.dumps(fake), detail_format="text")

    monkeypatch.setattr(module, "_run", fake_run)
    res = module.schedule_list()

    assert res.ok is True
    assert "[冲突] 评审会" in res.detail
    assert "[冲突] 周会" not in res.detail


def test_resolve_epoch_prefers_title_time() -> None:
    module = _load_handlers("_test_schedule_handlers_resolve")
    now = datetime(2026, 10, 10, 12, 0, 0)

    # title 说明天下午三点，when 被误填为 16:00
    epoch, adjusted = module._resolve_epoch("16:00", "明天下午三点评审会", now)
    assert adjusted is True
    assert epoch == int(datetime(2026, 10, 11, 15, 0).timestamp())

    # title 只有钟点、when 带日期：保留 when 的日期，取 title 的钟点
    epoch, adjusted = module._resolve_epoch("明天16:00", "下午三点评审会", now)
    assert adjusted is True
    assert epoch == int(datetime(2026, 10, 11, 15, 0).timestamp())

    # 两者一致时不调整
    epoch, adjusted = module._resolve_epoch("明天下午三点", "明天下午三点评审会", now)
    assert adjusted is False
    assert epoch == int(datetime(2026, 10, 11, 15, 0).timestamp())
