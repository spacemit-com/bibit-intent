"""截图工具：系统工具薄封装（退出码 → 用户文案）与路由直通。

执行侧不重复实现 grim/slurp/wl-copy：只验证「注入桌面会话环境后调用系统
bianbu-screenshot，并把 0/2/3/4 翻成对应文案」的契约。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution import system_tools as st
from bibit_intent.execution.executor import Executor
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.resources import resource_path
from bibit_intent.routing.intent import IntentAction, decide_local_intent
from bibit_intent.routing.rule_engine import RuleEngine


def _fake_session():
    return {
        "WAYLAND_DISPLAY": "wayland-0",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
    }


@pytest.fixture
def tool_env(monkeypatch):
    """固定会话环境与二进制路径，只观察调用参数与返回文案。"""
    monkeypatch.setattr(st, "_find_desktop_session_env", _fake_session)
    monkeypatch.setattr(st, "_screenshot_binary", lambda: "/usr/bin/bianbu-screenshot")


@pytest.fixture
def runner(monkeypatch):
    """替换真正起进程的 runner，捕获 (binary, mode, env)。"""
    calls: list[tuple[str, str, dict]] = []

    def fake(binary, mode, env):
        calls.append((binary, mode, env))
        return 0, "/home/bianbu/Desktop/screenshot-2026-10-14-171214.png\n", ""

    monkeypatch.setattr(st, "_run_shot_tool", fake)
    return calls


def test_region_mode_is_the_default_and_passes_the_session_env(tool_env, runner):
    """region 模式必须带桌面会话环境，否则 slurp 报 No wayland session 秒退。"""
    result = st.screenshot()

    assert result.ok is True
    binary, mode, env = runner[0]
    assert binary == "/usr/bin/bianbu-screenshot"
    assert mode == "region"
    assert env["WAYLAND_DISPLAY"] == "wayland-0"
    assert "WAYLAND_SOCKET" not in env
    assert "screenshot-2026-10-14-171214.png" in result.message
    assert result.detail_format == "text"


def test_full_mode_is_forwarded(tool_env, runner):
    result = st.screenshot(mode="full")

    assert result.ok is True
    assert runner[0][1] == "full"


def test_unknown_mode_falls_back_to_region(tool_env, runner):
    st.screenshot(mode="weird")

    assert runner[0][1] == "region"


def test_user_cancel_is_reported_as_success_without_a_file(tool_env, monkeypatch):
    """退出码 2 = 用户按 Esc 取消：不产生文件，也不应报失败。"""
    monkeypatch.setattr(st, "_run_shot_tool", lambda *a: (2, "", "screenshot cancelled"))
    result = st.screenshot()

    assert result.ok is True
    assert "取消" in result.message


def test_no_wayland_session_exit_code_names_the_next_action(tool_env, monkeypatch):
    monkeypatch.setattr(st, "_run_shot_tool", lambda *a: (3, "", "no Wayland session found"))
    result = st.screenshot()

    assert result.ok is False
    assert "Wayland" in result.message


def test_missing_dependencies_report_exit_code(tool_env, monkeypatch):
    monkeypatch.setattr(st, "_run_shot_tool", lambda *a: (4, "", "missing dependencies: slurp"))
    result = st.screenshot()

    assert result.ok is False
    assert "grim" in result.message


def test_generic_failure_keeps_stderr_as_detail(tool_env, monkeypatch):
    monkeypatch.setattr(st, "_run_shot_tool", lambda *a: (1, "", "capture failed: /x.png"))
    result = st.screenshot()

    assert result.ok is False
    assert result.detail == "capture failed: /x.png"


def test_selection_timeout_is_reported_as_failure(tool_env, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("bianbu-screenshot", st._SHOT_TIMEOUT)

    monkeypatch.setattr(st, "_run_shot_tool", timeout)
    result = st.screenshot()

    assert result.ok is False
    assert "超时" in result.message


def test_no_desktop_session_fails_before_spawning(monkeypatch):
    """没有桌面会话时不调用二进制（给了环境连不上 Wayland 也是白跑）。"""
    monkeypatch.setattr(st, "_find_desktop_session_env", lambda: None)

    def boom(*args, **kwargs):
        pytest.fail("must not spawn without a desktop session")

    monkeypatch.setattr(st, "_run_shot_tool", boom)
    result = st.screenshot()

    assert result.ok is False
    assert "桌面会话" in result.message


def test_missing_binary_is_reported(monkeypatch):
    monkeypatch.setattr(st, "_find_desktop_session_env", _fake_session)
    monkeypatch.setattr(st, "_screenshot_binary", lambda: None)

    def boom(*args, **kwargs):
        pytest.fail("must not spawn when the tool is missing")

    monkeypatch.setattr(st, "_run_shot_tool", boom)
    result = st.screenshot()

    assert result.ok is False
    assert "bianbu-screenshot" in result.message


def test_english_output_follows_language():
    with patch.object(st, "_find_desktop_session_env", _fake_session), \
         patch.object(st, "_screenshot_binary", lambda: "/usr/bin/bianbu-screenshot"), \
         patch.object(st, "_run_shot_tool", lambda *a: (2, "", "")):
        result = st.screenshot(language="en")
    assert result.message == "Screenshot cancelled."


def test_screenshot_is_registered_and_executes():
    """tools.yaml 声明 + HANDLERS 注册 + MCP 分组，缺一不可。"""
    registry = ToolRegistry(resource_path("tools.yaml"))
    load_skills(resource_path("skills"), registry, dict(st.HANDLERS))
    tool = registry.get("screenshot")
    assert tool is not None
    assert tool.system.get("type") == "shell"
    assert tool.mcp_group() == "control"

    executor = Executor(registry, handlers=dict(st.HANDLERS))
    with patch.object(st, "_find_desktop_session_env", _fake_session), \
         patch.object(st, "_screenshot_binary", lambda: "/usr/bin/bianbu-screenshot"), \
         patch.object(st, "_run_shot_tool", lambda *a: (0, "/d/a.png", "")):
        result = executor.execute("screenshot", {"mode": "full"})
    assert result.ok is True
    assert result.detail_format == "text"


@pytest.fixture(scope="module")
def routing():
    registry = ToolRegistry(resource_path("tools.yaml"))
    load_skills(resource_path("skills"), registry, {})
    return registry, RuleEngine(registry)


def _lexicon_available() -> bool:
    """本机没有 jieba 时分析器降级，decide_local_intent 会先升云（与改动无关）。

    词库是端侧运行时资源，仓库既有探针类用例在开发机上一律因此判红；这里把
    「进模型前决策」的断言按词库可用性跳过，规则层断言仍在任何机器上都执行。
    """
    from bibit_intent.analysis.lexical import lexical_analysis

    return lexical_analysis("截图")[1] is None


needs_lexicon = pytest.mark.skipif(
    not _lexicon_available(), reason="本机缺少端侧词库（jieba），决策层用例在设备上跑"
)


@pytest.mark.parametrize("text,slots", [
    ("截图", {}),
    ("截屏", {}),
    ("屏幕截图", {}),
    ("帮我截个图", {}),
    ("截个图", {}),
    ("帮我截一张图", {}),
    ("截取屏幕", {}),
    ("全屏截图", {"mode": "full"}),
    ("区域截图", {"mode": "region"}),
    ("选区截图", {"mode": "region"}),
])
def test_rule_engine_hits_screenshot_tool(routing, text, slots):
    """规则层整句锚定命中；模式词由 slot_map 补，默认模式由工具声明补。"""
    registry, rule = routing
    match = rule.match(text)
    assert match is not None and match.tool_name == "screenshot", (text, match)
    for key, value in slots.items():
        assert match.slots.get(key) == value, (text, match.slots)


@needs_lexicon
@pytest.mark.parametrize("text", [
    "截图", "截屏", "屏幕截图", "帮我截个图", "截个图",
    "帮我截一张图", "截取屏幕", "全屏截图", "区域截图", "选区截图",
])
def test_rule_call_direct_hit(routing, text):
    """规则直通：整句锚定的截图表达不进模型，直接执行。"""
    registry, rule = routing
    plan = decide_local_intent(text, rule, registry)
    assert plan.action is IntentAction.RULE_CALL, (text, plan.action, plan.tool_name)
    assert plan.tool_name == "screenshot"


@needs_lexicon
def test_full_screen_slot_map_overrides_the_default(routing):
    registry, rule = routing
    plan = decide_local_intent("全屏截图", rule, registry)
    assert plan.slots == {"mode": "full"}


@pytest.mark.parametrize("text", [
    "take a screenshot",
    "take a screenshot of the whole screen",
    "capture the screen",
    "grab a screenshot",
])
def test_english_rule_engine_hit(routing, text):
    registry, rule = routing
    match = rule.match(text)
    assert match is not None and match.tool_name == "screenshot", (text, match)


@needs_lexicon
@pytest.mark.parametrize("text", [
    "take a screenshot",
    "take a screenshot of the whole screen",
    "capture the screen",
    "grab a screenshot",
])
def test_english_rule_call(routing, text):
    registry, rule = routing
    plan = decide_local_intent(text, rule, registry)
    assert plan.action is IntentAction.RULE_CALL, (text, plan.action, plan.reason)
    assert plan.tool_name == "screenshot"


@pytest.mark.parametrize("text", [
    "截图为证",
    "这个截图上写了什么",
    "把截图发给我",
    "截图给我看看",
    "截图的原理是什么",
])
def test_narrative_input_is_not_hijacked(routing, text):
    """「截图为证」「这个截图上写了什么」是叙述/问答句，不得直通执行截图。"""
    registry, rule = routing
    match = rule.match(text)
    assert match is None or match.tool_name != "screenshot", (text, match)
    assert match is None or match.confidence < RuleEngine.CONFIDENCE_THRESHOLD or match.tool_name != "screenshot"


def _fake_tool(path: Path, body: str) -> str:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def test_runner_does_not_block_on_a_daemon_that_inherits_stdout(tmp_path):
    """截图工具内部 setsid 起的 wl-copy 守护会继承 stdout，runner 必须照样及时返回。

    用 cancellation.run_command（输出收在管道里）时，收尾的 communicate() 等不到
    EOF：实测 region 超时后 /v1/runs 一直不返回（35s 已过，请求仍挂在会话里）。
    这个用例起一个真的会 fork 守护的假工具来钉住该行为。
    """
    fake = _fake_tool(
        tmp_path / "bianbu-screenshot",
        'echo "已保存并复制到剪贴板：%s/out.png"\n' % tmp_path
        + "setsid sh -c 'sleep 10' &\n"
        + "exit 0\n",
    )
    started = time.monotonic()
    rc, out, err = st._run_shot_tool(fake, "full", dict(os.environ))
    elapsed = time.monotonic() - started

    assert rc == 0
    assert "out.png" in out
    assert elapsed < 5, f"runner 被守护持有的 stdout 卡住了（{elapsed:.1f}s）"


def test_runner_kills_the_process_group_on_timeout(tmp_path, monkeypatch):
    """超时（选区一直没人拖框）要按进程组回收，不能留下 slurp 之类残留。"""
    fake = _fake_tool(tmp_path / "bianbu-screenshot", "sleep 60 &\nsleep 60\n")
    monkeypatch.setattr(st, "_SHOT_TIMEOUT", 1)

    with pytest.raises(subprocess.TimeoutExpired):
        st._run_shot_tool(fake, "region", dict(os.environ))

    # 假工具与它 fork 出来的后台任务都属于同一个新会话/进程组，应一起被收掉。
    time.sleep(0.3)
    leftovers = subprocess.run(["pgrep", "-f", str(fake)], capture_output=True, text=True).stdout.strip()
    assert leftovers == "", f"超时后仍有残留进程：{leftovers}"


def test_runner_reports_the_exit_code_and_output(tmp_path):
    """正常路径：退出码、stdout、stderr 原样回传（文案翻译在上层）。"""
    fake = _fake_tool(tmp_path / "bianbu-screenshot", 'echo "out"; echo "err" >&2; exit 7\n')
    rc, out, err = st._run_shot_tool(fake, "region", dict(os.environ))
    assert (rc, out.strip(), err.strip()) == (7, "out", "err")


def test_concurrent_capture_is_reported_not_as_failure(tool_env, monkeypatch):
    """系统工具的单实例锁拒绝重复截图时，提示「上一次还在进行中」而不是泛化失败。"""
    monkeypatch.setattr(
        st, "_run_shot_tool",
        lambda *a: (1, "", "上一次截图还在进行中，请先完成或按 Esc 取消"),
    )
    result = st.screenshot()

    assert result.ok is True
    assert "上一次" in result.message
    # detail 会被渲染成代码块，重复同一句话会让用户看到两遍
    assert result.detail == ""


def test_english_concurrent_capture(tool_env, monkeypatch):
    monkeypatch.setattr(
        st, "_run_shot_tool",
        lambda *a: (1, "", "a previous capture is still running, finish it or press Esc"),
    )
    result = st.screenshot(language="en")

    assert result.ok is True
    assert "previous capture" in result.message
