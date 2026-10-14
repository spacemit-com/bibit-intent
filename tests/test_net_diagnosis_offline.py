"""
tests/test_net_diagnosis_offline.py — 离线网络诊断降级回归（全离线）

对应 docs/intent/offline_network_diagnosis_plan.md §5：
  · §5.2 预检门：云端可达 → ESCALATE(net_diagnosis)（与改动前逐字节一致）
                 云端不可达 → RULE_CALL net_diagnose（复用规则直通，不新增动作类型）
  · §5.3 router 兜底：无云端客户端 / 云端调用抛异常时，网络故障类请求返回
                 Stage.RULE + 本地诊断结论（handled=True，Runtime 用 direct_reply
                 直接回复用户），非网络故障类请求保持原话术
  · 边界：§4.5 J 组误判边界不得回退（网卡属性 / 应用域 / 知识问答 / 状态查询）

预检门用 cloud_probe 注入（decide_local_intent(..., cloud_probe=...)），
router 侧用 monkeypatch 替换 decide_local_intent，因此本文件不依赖真实网络。

运行： .venv/bin/python -m pytest tests/test_net_diagnosis_offline.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.executor import Executor
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.execution.system_tools import ExecResult
from bibit_intent.resources import resource_path
from bibit_intent.routing import router as router_module
from bibit_intent.routing.intent import IntentAction, IntentPlan, decide_local_intent
from bibit_intent.routing.router import Router, Stage
from bibit_intent.routing.rule_engine import RuleEngine


# ── 预检门（§5.2）─────────────────────────────────────────────────────────

# 网络故障陈述（应进门）：标准说法 + 常见口语
_FAULT_INPUTS = [
    "上不了网了",
    "网页打不开",
    "DNS 解析不了",
    "网络断了",
    "网络无法连接",
]

# ⚠ 已知分支顺序：这几条口语在分析层被标为 is_context_dependent，其分支
#   早于 ⓪e3（intent.py ⓪c1），因此 reason 是 context_dependent 而非
#   net_diagnosis，不经过 §5.2 预检门。改动前行为就是这样（上云），
#   本方案不动它；离线时的本地兜底由 router._local_diagnosis_fallback
#   的正则判定覆盖（见文件末尾 router 用例）。
_CONTEXT_DEPENDENT_FAULT_INPUTS = ["网络连不上", "无法访问网络"]

# J 组误判边界（不得被吃成诊断直通）
_BOUNDARY_INPUTS = [
    "网卡驱动是什么版本",
    "网卡型号是多少",
    "微信打不开了",
    "USB设备连不上",
    "网络协议是什么",
    "以太网协议是什么",
    "网络状态如何",
]


@pytest.fixture(scope="module")
def routing() -> tuple[ToolRegistry, RuleEngine]:
    reg = ToolRegistry(resource_path("tools.yaml"))
    load_skills(resource_path("skills"), reg, {})
    return reg, RuleEngine(reg)


@pytest.mark.parametrize("text", _FAULT_INPUTS)
def test_gate_online_escalates_to_cloud(routing, text: str) -> None:
    """云端可达：维持改动前行为，直接升级云端。"""
    reg, rule = routing
    plan = decide_local_intent(text, rule, reg, cloud_probe=lambda: True)
    assert plan.action is IntentAction.ESCALATE, plan
    assert plan.reason == "net_diagnosis", plan


@pytest.mark.parametrize("text", _FAULT_INPUTS)
def test_gate_offline_falls_back_to_local_tool(routing, text: str) -> None:
    """云端不可达：直通本地 net_diagnose（RULE_CALL，不进模型、不上云）。"""
    reg, rule = routing
    plan = decide_local_intent(text, rule, reg, cloud_probe=lambda: False)
    assert plan.action is IntentAction.RULE_CALL, plan
    assert plan.tool_name == "net_diagnose", plan
    assert plan.confidence == 1.0, plan
    assert plan.matched_pattern == "offline_net_diagnosis", plan


@pytest.mark.parametrize("text", _BOUNDARY_INPUTS)
def test_boundaries_never_direct_call_net_diagnose(routing, text: str) -> None:
    """J 组边界：无论云端可达与否，都不得被判成离线诊断直通。"""
    reg, rule = routing
    for reachable in (True, False):
        plan = decide_local_intent(text, rule, reg, cloud_probe=lambda: reachable)
        assert plan.tool_name != "net_diagnose", (text, reachable, plan)
        assert plan.matched_pattern != "offline_net_diagnosis", (text, reachable, plan)


def test_explicit_ip_query_is_not_hijacked_when_offline(routing) -> None:
    """显式查本机 IP 的请求仍归 net_ip（既有排除项），离线也不改判。"""
    reg, rule = routing
    plan = decide_local_intent(
        "无线连不上了，查一下本机ip", rule, reg, cloud_probe=lambda: False
    )
    assert plan.tool_name == "net_ip", plan


@pytest.mark.parametrize("text", _CONTEXT_DEPENDENT_FAULT_INPUTS)
def test_context_dependent_faults_keep_their_branch(routing, text: str) -> None:
    """分支顺序未变：这类口语走 is_context_dependent（早于 ⓪e3），仍升级云端。

    离线兜底不靠预检门，靠 router._local_diagnosis_fallback 的正则判定。
    """
    reg, rule = routing
    for reachable in (True, False):
        plan = decide_local_intent(text, rule, reg, cloud_probe=lambda: reachable)
        assert plan.action is IntentAction.ESCALATE, (text, plan)
        assert plan.reason == "context_dependent", (text, plan)


# ── router 兜底（§5.3）─────────────────────────────────────────────────────

_DIAGNOSE_SUMMARY = "❌ 断在本机到网关：本地网络问题，检查网线/WiFi连接"


def _executor(registry: ToolRegistry, handler=None) -> Executor:
    def default_handler(**_):
        return ExecResult(True, _DIAGNOSE_SUMMARY, "✅ 网关可达（10.0.0.1）")

    return Executor(registry, handlers={"net_diagnose": handler or default_handler})


def _plan(reason: str, text: str) -> IntentPlan:
    return IntentPlan(action=IntentAction.ESCALATE, reason=reason)


def _offline(monkeypatch) -> None:
    """让兜底里的可达性判断走"云端不可达"（不依赖测试机真实网络）。"""
    monkeypatch.setattr(router_module, "_cloud_reachable", lambda *a, **k: False)


@pytest.mark.parametrize("reason", ["net_diagnosis", "context_dependent"])
def test_router_offline_returns_local_diagnosis_as_reply(routing, monkeypatch, reason) -> None:
    """无云端客户端：网络故障类请求 → Stage.RULE + 本地结论（Runtime 直接回复）。"""
    reg, rule = routing
    monkeypatch.setattr(
        router_module, "decide_local_intent", lambda *a, **k: _plan(reason, "x")
    )
    _offline(monkeypatch)
    router = Router(reg, rule, None, _executor(reg), cloud_client=None)

    result = router.handle("网络连不上")
    assert result.stage is Stage.RULE, result
    assert result.tool_name == "net_diagnose"
    assert result.exec_result.ok is True
    # 无云端客户端场景：话术要说清"未配置"，免得和下面"云端可达"的探测事实打架
    assert result.exec_result.message.startswith(
        "现在连不上云端（本机未配置云端客户端），我用本地检查了一遍："
    )
    assert _DIAGNOSE_SUMMARY in result.exec_result.message
    assert result.exec_result.detail == "✅ 网关可达（10.0.0.1）"
    assert result.escalate_reason == reason
    assert result.debug.get("local_fallback") == "net_diagnose"


def test_router_cloud_exception_returns_local_diagnosis(routing, monkeypatch) -> None:
    """云端客户端存在但调用抛异常（断网典型表现）：同一兜底生效，不暴露异常原文。"""

    class BrokenCloud:
        def handle(self, *a, **k):
            raise ConnectionError("no route to host")

    reg, rule = routing
    monkeypatch.setattr(
        router_module, "decide_local_intent", lambda *a, **k: _plan("net_diagnosis", "x")
    )
    _offline(monkeypatch)
    router = Router(reg, rule, None, _executor(reg), cloud_client=BrokenCloud())

    result = router.handle("上不了网了")
    assert result.stage is Stage.RULE, result
    assert result.exec_result.ok is True
    assert "no route to host" not in result.exec_result.message
    assert "no route to host" not in result.exec_result.detail


def test_router_offline_non_network_request_keeps_old_text(routing, monkeypatch) -> None:
    """非网络故障类请求：保持原有"连不上云端"话术，不被本地诊断劫持。"""
    reg, rule = routing
    monkeypatch.setattr(
        router_module, "decide_local_intent", lambda *a, **k: _plan("writing_task", "x")
    )
    router = Router(reg, rule, None, _executor(reg), cloud_client=None)

    result = router.handle("帮我写一首诗")
    assert result.stage is Stage.CLOUD, result
    assert result.tool_name is None
    assert result.exec_result.ok is False
    assert result.exec_result.message == "现在连不上云端，暂时无法处理这个请求"


def test_router_fallback_gives_up_when_tool_fails(routing, monkeypatch) -> None:
    """兜底工具本身失败（ok=False）时不撒谎：回落原话术，不返回诊断结论。"""
    reg, rule = routing
    monkeypatch.setattr(
        router_module, "decide_local_intent", lambda *a, **k: _plan("net_diagnosis", "x")
    )
    _offline(monkeypatch)
    executor = _executor(
        reg, handler=lambda **_: ExecResult(False, "命令超时：ip route", "")
    )
    router = Router(reg, rule, None, executor, cloud_client=None)

    result = router.handle("上不了网了")
    assert result.stage is Stage.CLOUD, result
    assert result.exec_result.ok is False
    assert result.exec_result.message == "现在连不上云端，暂时无法处理这个请求"


def test_router_fallback_does_not_fire_when_cloud_reachable(routing, monkeypatch) -> None:
    """**板测补的正例**：云端可达时不截胡。

    serve 模式下 Router.cloud_client 恒为 None，若只看"没有云端客户端"就兜底，
    在线的网络故障请求会被本地诊断吞掉（用户看到"现在连不上云端…"，
    却同屏看到"✅ 云端可达"，且云端 agent 的答案拿不到）。
    正确行为：交回原升级链路（Stage.CLOUD，handled=False，runtime 去问云端）。
    """
    reg, rule = routing
    monkeypatch.setattr(
        router_module, "decide_local_intent", lambda *a, **k: _plan("net_diagnosis", "x")
    )
    monkeypatch.setattr(router_module, "_cloud_reachable", lambda *a, **k: True)
    executor = _executor(reg)
    router = Router(reg, rule, None, executor, cloud_client=None)

    result = router.handle("网卡了")
    assert result.stage is Stage.CLOUD, result
    assert result.tool_name is None
    assert result.exec_result.ok is False
    assert result.debug.get("local_fallback") is None


def test_router_cloud_exception_message_when_cloud_reachable(routing, monkeypatch) -> None:
    """可达但调用失败：话术不得断言"连不上云端"（异常原文只进 detail）。"""

    class BrokenCloud:
        def handle(self, *a, **k):
            raise RuntimeError("boom-500")

    reg, rule = routing
    monkeypatch.setattr(
        router_module, "decide_local_intent", lambda *a, **k: _plan("net_diagnosis", "x")
    )
    monkeypatch.setattr(router_module, "_cloud_reachable", lambda *a, **k: True)
    router = Router(reg, rule, None, _executor(reg), cloud_client=BrokenCloud())

    result = router.handle("上不了网了")
    assert result.stage is Stage.CLOUD, result
    assert result.exec_result.message == "云端不可用，暂时无法处理这个请求"
    assert "boom-500" not in result.exec_result.message
    assert "boom-500" in result.exec_result.detail


# ── §4.5 档 1：症状型 / 英文 / 独立故障词 / 泛化求助 ────────────────────────

# 这些表达在扩写前全部落在 to_model（端侧 1B 乱挑工具或无工具），
# 扩写后应确定性识别为网络故障诊断。
_SYMPTOM_INPUTS = [
    "网卡了",
    "网卡得不行，帮我看看",
    "网卡得很",
    "网络好卡",
    "网页加载很慢",
    "上网很慢",
    "网页一直转圈",
    "网线没反应",
    "wifi很慢",
    "网页感叹号",
    "no internet",
    "can't connect to wifi",
    "wifi is down",
    "network is not working",
    "I have no internet",
    "网络怎么了",
    "帮我看看网络怎么了",
]

# 独立故障词（无网络对象共现）：原本会掉进 wifi_disconnect 的关键词弱命中，
# 现在改为诊断直通 —— 顺手消掉一处"危险候选"（§4.4）。
_STANDALONE_FAULT_INPUTS = ["断网了", "是不是断网了", "没网了"]


@pytest.mark.parametrize("text", _SYMPTOM_INPUTS + _STANDALONE_FAULT_INPUTS)
def test_symptom_and_standalone_faults_escalate_when_online(routing, text: str) -> None:
    """§4.5 档 1：在线时升级云端（与其它故障表达同一路径）。"""
    reg, rule = routing
    plan = decide_local_intent(text, rule, reg, cloud_probe=lambda: True)
    assert plan.action is IntentAction.ESCALATE, (text, plan)
    assert plan.reason == "net_diagnosis", (text, plan)


@pytest.mark.parametrize("text", _SYMPTOM_INPUTS + _STANDALONE_FAULT_INPUTS)
def test_symptom_and_standalone_faults_direct_call_when_offline(routing, text: str) -> None:
    """§4.5 档 1：离线时直通本地诊断。"""
    reg, rule = routing
    plan = decide_local_intent(text, rule, reg, cloud_probe=lambda: False)
    assert plan.action is IntentAction.RULE_CALL, (text, plan)
    assert plan.tool_name == "net_diagnose", (text, plan)
    assert plan.matched_pattern == "offline_net_diagnosis", (text, plan)


# 硬件/状态查询：都含"网"+"卡"两个字面，绝不能被症状词吃掉。
# 实测这些是 _KARD 放宽后最先回归的一批（"网卡的带宽是多少"/"启用有线网卡"…）。
_HARDWARE_QUERY_INPUTS = [
    "网卡驱动是什么版本",
    "网卡型号是多少",
    "网卡速率是多少",
    "网卡的带宽是多少",
    "网卡是不是坏了怎么判断",
    "网卡正常吗",
    "查询有线网卡状态",
    "获取网卡IP",
    "查看网卡MAC地址",
    "网卡 ip 地址",
    "网卡 mac 地址",
    "网卡硬件地址是多少",
    "启用有线网卡",
    "禁用有线网卡",
    "切断有线网卡",
    "停掉有线网卡连接",
    "网线插上了帮我把网卡跑起来",
    "USB外设掉线了帮我弄回来",
    "网络状态如何",
    "看看有线网络通不通",
    "我现在网络通不通",
    "网速正常吗看看连接",
    "系统卡不卡",
    "感觉屏幕有点卡把刷新率调高点",
]


@pytest.mark.parametrize("text", _HARDWARE_QUERY_INPUTS)
def test_hardware_and_status_queries_not_taken_by_diagnosis(routing, text: str) -> None:
    """这些既不是故障诊断，也不得被离线诊断吃掉（在线/离线都不许）。"""
    reg, rule = routing
    for reachable in (True, False):
        plan = decide_local_intent(text, rule, reg, cloud_probe=lambda: reachable)
        assert plan.tool_name != "net_diagnose", (text, reachable, plan)
        assert plan.reason != "net_diagnosis", (text, reachable, plan)


# ── §4.6 「能否上网」类疑问：只修离线 ──────────────────────────────────────

_CONNECTIVITY_QUESTION_INPUTS = [
    "能上网吗",
    "现在有网吗",
    "网络正常吗",
    "能不能上网",
    "可以正常上网吗",
    "看看这里能不能上网",
]


@pytest.mark.parametrize("text", _CONNECTIVITY_QUESTION_INPUTS)
def test_connectivity_question_online_keeps_existing_branch(routing, text: str) -> None:
    """在线时逐字节维持现状：绝不能变成诊断（"看看这里能不能上网"仍是 wifi_scan）。"""
    reg, rule = routing
    plan = decide_local_intent(text, rule, reg, cloud_probe=lambda: True)
    assert plan.reason != "net_diagnosis", (text, plan)
    assert plan.matched_pattern != "offline_net_connectivity_question", (text, plan)


@pytest.mark.parametrize("text", _CONNECTIVITY_QUESTION_INPUTS)
def test_connectivity_question_offline_goes_local_diagnosis(routing, text: str) -> None:
    """离线时才转本地诊断（§4.6 拍板：只修离线）。"""
    reg, rule = routing
    plan = decide_local_intent(text, rule, reg, cloud_probe=lambda: False)
    assert plan.action is IntentAction.RULE_CALL, (text, plan)
    assert plan.tool_name == "net_diagnose", (text, plan)
    assert plan.matched_pattern == "offline_net_connectivity_question", (text, plan)


def test_connectivity_question_never_fires_on_disconnect_commands(routing) -> None:
    """反例：断网/开关网卡指令里也有"网"，不得被连接性疑问句式吃掉。"""
    reg, rule = routing
    for text in ["断开网络连接", "关闭wifi", "开有线电视", "关掉有线电视", "帮我切断网络连接"]:
        for reachable in (True, False):
            plan = decide_local_intent(text, rule, reg, cloud_probe=lambda: reachable)
            assert plan.matched_pattern != "offline_net_connectivity_question", (text, plan)
            assert plan.tool_name != "net_diagnose", (text, reachable, plan)
