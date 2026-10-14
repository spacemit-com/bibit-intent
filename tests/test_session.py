"""
tests/test_session.py — 会话级缺参回填单元测试
覆盖 agent/session.py 的 fill/keep/new 三态决策：
  - 补值提取：int / str / bool / 显式 参数=值
  - 纯确认语保持 pending（防死循环有轮次上限）
  - 新意图守卫：规则命中 / 开放问答 / 明确控制 / 疑问词 → 不误填
  - Router 集成：模型 CALL 缺参 → 挂起 → 下一轮回填执行

运行： python3 tests/test_session.py           （纯逻辑部分）
      设备上 .venv/bin/python tests/test_session.py   （含规则/路由集成）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bibit_intent.core.session import SessionState
from bibit_intent.resources import resource_path


def test_package_preview_pattern_pending_distinguishes_new_intent():
    from bibit_intent.core.registry import ToolRegistry
    from bibit_intent.routing.rule_engine import RuleEngine

    rule = RuleEngine(ToolRegistry(resource_path("tools.yaml")))
    session = SessionState()
    session.set_pending(
        "pkg_match_preview", "pattern", "pattern",
        {"scope": "installed", "mode": "regex"}, "",
    )

    assert session.resolve("reboot", rule) == ("new", None)
    assert session.pending is None

    session.set_pending(
        "pkg_match_preview", "pattern", "pattern",
        {"scope": "installed", "mode": "regex"}, "",
    )
    assert session.resolve("pattern=^bash.*$", rule) == ("fill", "^bash.*$")

    session.set_pending(
        "pkg_match_preview", "pattern", "pattern",
        {"scope": "installed", "mode": "glob"}, "",
    )
    assert session.resolve("bash?", rule) == ("fill", "bash?")

# ── 纯逻辑用例（不依赖 pydantic，本机可跑）────────────────────────────
# (pending 参数类型, 输入, 期望动作, 期望值)
FILL_CASES = [
    # int 槽位：数字 / 口语"调到X"
    ("int", "60",        "fill", 60),
    ("int", "调到70",    "fill", 70),
    ("int", "音量调到80", "fill", 80),
    # str 槽位：裸值 / 剥离填充词
    ("str", "python3",          "fill", "python3"),
    ("str", "帮我查一下 vim",    "fill", "vim"),
    ("str", "浏览器",           "fill", "浏览器"),
    ("str", "/usr/bin/ls",      "fill", "/usr/bin/ls"),
    # bool 槽位："是/否"本身就是值（优先于确认语）
    ("bool", "是",     "fill", True),
    ("bool", "不要",   "fill", False),
    # 显式 参数=值
    ("str", "pkg=vim",       "fill", "vim"),
    ("int", "level=55",      "fill", 55),
    ("str", "pkg: python3",  "fill", "python3"),
    # English slot-filling forms
    ("int", "70", "fill", 70),
    ("int", "level=55", "fill", 55),
    ("str", "vim", "fill", "vim"),
    ("str", "package=python3", "fill", "python3"),
    ("str", "/usr/bin/ls", "fill", "/usr/bin/ls"),
    ("bool", "yes", "fill", True),
    ("bool", "no", "fill", False),
]

# 纯确认语 → keep；轮次上限 → new
KEEP_CASES = ["嗯", "好的", "继续", "ok", "对", "okay", "yes", "confirm", "go ahead"]

# 新意图守卫 → new（pending 清空，走正常路由）
NEW_INTENT_CASES = [
    "今天天气怎么样",   # 疑问词
    "怎么打开浏览器",   # 疑问词
    "帮我写一首诗",     # 疑问词? 无 → 待验证 str 误填风险
    "音量调到多少",     # 疑问词
    "reboot",           # 英文系统控制指令
]


def run_pure() -> bool:
    passed = failed = 0

    print("== 补值提取 ==")
    for ptype, text, want_action, want_val in FILL_CASES:
        s = SessionState()
        s.set_pending("pdep" if ptype == "str" else "set_volume",
                      "pkg" if ptype == "str" else ("on" if ptype == "bool" else "level"),
                      "测试参数", {}, "", ptype)
        action, val = s.resolve(text)
        ok = (action, val) == (want_action, want_val)
        passed += ok; failed += (not ok)
        print(f"  {'OK ' if ok else 'XX '}{ptype:4} {text!r:14} -> {action:4} {val!r}"
              + ("" if ok else f"   期望 {want_action}/{want_val!r}"))

    print("\n== 纯确认语（keep，保持 pending） ==")
    for text in KEEP_CASES:
        s = SessionState()
        s.set_pending("pdep", "pkg", "软件包名", {}, "", "str")
        action, _ = s.resolve(text)
        ok = action == "keep" and s.pending is not None
        passed += ok; failed += (not ok)
        print(f"  {'OK ' if ok else 'XX '}{text!r:10} -> {action:4} pending={'保留' if s.pending else '丢失!'}")

    print("\n== 轮次上限（防死循环） ==")
    s = SessionState(max_wait_turns=1)
    s.set_pending("pdep", "pkg", "软件包名", {}, "", "str")
    a1, _ = s.resolve("嗯")
    a2, _ = s.resolve("嗯")
    ok = a1 == "keep" and a2 == "new" and s.pending is None
    passed += ok; failed += (not ok)
    print(f"  {'OK ' if ok else 'XX '}第1轮={a1} 第2轮={a2} pending={'已清空' if s.pending is None else '仍在'}")

    print("\n== 新意图守卫（疑问词不误填） ==")
    for text in NEW_INTENT_CASES:
        s = SessionState()
        s.set_pending("pdep", "pkg", "软件包名", {}, "", "str")
        action, val = s.resolve(text)
        ok = action == "new" and s.pending is None
        passed += ok; failed += (not ok)
        print(f"  {'OK ' if ok else 'XX '}{text!r:12} -> {action:4} val={val!r}"
              + ("" if ok else f"   期望 new，避免把整句填成包名"))

    total = passed + failed
    print(f"\n纯逻辑：{passed}/{total} 通过")
    return failed == 0


# ── 规则/路由集成（需 pydantic；本机缺失时跳过，设备上跑）────────────
def run_integration() -> bool:
    try:
        from bibit_intent.execution.executor import Executor
        from bibit_intent.models.local_model import LocalModelResult
        from bibit_intent.routing.rule_engine import RuleEngine
        from bibit_intent.routing.router import Router, Stage
        from bibit_intent.core.registry import ToolRegistry
        from bibit_intent.execution.system_tools import ExecResult
    except ImportError:
        print("\n（本机无 pydantic，跳过规则/路由集成用例；设备上 .venv 会跑）")
        return True

    passed = failed = 0

    print("\n== 执行层缺参返回 ==")
    reg = ToolRegistry()
    ex = Executor(reg, handlers={})
    r = ex.execute("launch_app", {})
    ok = r.need_param == "app_name"
    passed += ok; failed += (not ok)
    print(f"  {'OK ' if ok else 'XX '}launch_app{{}} -> need_param={r.need_param!r} msg={r.message!r}")

    print("\n== Router：模型 CALL 缺参 → 挂起 → 下一轮回填执行 ==")
    # pkg_depends 等工具来自 skills/ubuntu_cmd，需像 main.py 一样加载 skill
    from pathlib import Path
    from bibit_intent.execution.skill_loader import load_skills
    from bibit_intent.execution.system_tools import HANDLERS as SYSTEM_HANDLERS
    skills_dir = resource_path("skills")
    handlers = dict(SYSTEM_HANDLERS)
    load_skills(skills_dir, reg, handlers)
    calls: list[dict] = []

    class _DuckResult:
        """模拟 skill 自带 ExecResult（无 need_param 字段）→ 执行层须归一化。"""
        def __init__(self, ok, message, detail=""):
            self.ok = ok
            self.message = message
            self.detail = detail

    def fake_pkg_depends(**kw):
        calls.append(kw)
        return _DuckResult(True, "ok")

    rule = RuleEngine(reg)
    executor = Executor(reg, handlers={"pkg_depends": fake_pkg_depends})

    class StubLocal:
        def is_available(self):
            return True
        def infer(self, user_input, tool_list, max_output_tokens=30, **_):
            return LocalModelResult(action="CALL", tool_name="pdep", params={},
                                    raw="CALL pdep")

    router = Router(reg, rule, StubLocal(), executor)
    session = SessionState()

    # 第一轮：规则未命中，模型 CALL pdep 但缺 pkg → 提示补参并挂起
    r1 = router.handle("查一下依赖", session=session)
    ok = r1.exec_result is not None and r1.exec_result.need_param == "pkg" \
        and session.pending is not None and session.pending.tool_name == "pkg_depends"
    passed += ok; failed += (not ok)
    print(f"  {'OK ' if ok else 'XX '}第1轮 need_param={r1.exec_result.need_param if r1.exec_result else None}"
          f" pending={'挂起' if session.pending else '无'}")

    # 第二轮：直接补值 → 自动回填执行
    r2 = router.handle("python3", session=session)
    ok = r2.stage is Stage.SLOT_FILL and r2.exec_result.ok \
        and calls == [{"pkg": "python3"}] and session.pending is None
    passed += ok; failed += (not ok)
    print(f"  {'OK ' if ok else 'XX '}第2轮 stage={r2.stage.value} ok={r2.exec_result.ok}"
          f" calls={calls} pending={'已清空' if session.pending is None else '仍在!'}")

    print("\n== 无状态模式（session=None 不崩溃，缺参只提示） ==")
    r3 = router.handle("查一下依赖")
    ok = r3.exec_result is not None and r3.exec_result.need_param == "pkg"
    passed += ok; failed += (not ok)
    print(f"  {'OK ' if ok else 'XX '}无 session need_param={r3.exec_result.need_param if r3.exec_result else None}")

    print("\n== 新意图守卫（pending 时换话题 → 正常路由） ==")
    session = SessionState()
    router.handle("查一下依赖", session=session)
    # 明确控制指令 → 应清空 pending 走正常路由（规则命中 launch_app）
    r4 = router.handle("打开浏览器", session=session)
    ok = session.pending is None and r4.stage is Stage.RULE and r4.tool_name == "launch_app"
    passed += ok; failed += (not ok)
    print(f"  {'OK ' if ok else 'XX '}打开浏览器 -> stage={r4.stage.value} tool={r4.tool_name}"
          f" pending={'已清空' if session.pending is None else '仍在!'}")

    print("\n== Router：模型推测值（原文无证据）→ 挂起确认 → 确认/新值/取消 ==")
    confirm_calls: list[dict] = []

    def fake_set_volume(**kw):
        confirm_calls.append(kw)
        return _DuckResult(True, "ok")

    class StubGuess:
        def is_available(self):
            return True
        def infer(self, user_input, tool_list, max_output_tokens=30, **_):
            return LocalModelResult(action="CALL", tool_name="set_volume",
                                    params={"level": 50}, raw="CALL set_volume level=50")

    executor_c = Executor(reg, handlers={"set_volume": fake_set_volume})
    router_c = Router(reg, rule, StubGuess(), executor_c)

    # 第一轮：模型猜 level=50，原文无证据 → 挂起确认，不执行
    session = SessionState()
    r5 = router_c.handle("帮我设置音量", session=session)
    ok = r5.stage is Stage.CONFIRM and r5.exec_result.ok is False \
        and session.pending is not None and session.pending.confirm \
        and session.pending.filled == {"level": 50} and confirm_calls == []
    passed += ok; failed += (not ok)
    print(f"  {'OK ' if ok else 'XX '}第1轮 stage={r5.stage.value} "
          f"confirm={'挂起' if session.pending and session.pending.confirm else '无'}"
          f" 未执行={confirm_calls == []}")

    # 第二轮：确认 → 执行模型推测值
    r6 = router_c.handle("确认", session=session)
    ok = r6.exec_result.ok and confirm_calls == [{"level": 50}] \
        and session.pending is None
    passed += ok; failed += (not ok)
    print(f"  {'OK ' if ok else 'XX '}确认 -> calls={confirm_calls}"
          f" pending={'已清空' if session.pending is None else '仍在!'}")

    # 给新值：用户直接给数字 → 用新值执行
    session = SessionState()
    router_c.handle("帮我设置音量", session=session)
    r7 = router_c.handle("60", session=session)
    ok = r7.exec_result.ok and confirm_calls[-1] == {"level": 60} \
        and session.pending is None
    passed += ok; failed += (not ok)
    print(f"  {'OK ' if ok else 'XX '}给新值60 -> calls={confirm_calls[-1]}"
          f" pending={'已清空' if session.pending is None else '仍在!'}")

    # 取消 → 不执行
    session = SessionState()
    router_c.handle("帮我设置音量", session=session)
    r8 = router_c.handle("取消", session=session)
    ok = r8.escalate_reason == "cancelled" and confirm_calls[-1] == {"level": 60} \
        and session.pending is None
    passed += ok; failed += (not ok)
    print(f"  {'OK ' if ok else 'XX '}取消 -> reason={r8.escalate_reason}"
          f" 未执行={confirm_calls[-1] == {'level': 60}}")

    total = passed + failed
    print(f"\n集成：{passed}/{total} 通过")
    return failed == 0


if __name__ == "__main__":
    ok_pure = run_pure()
    ok_int = run_integration()
    sys.exit(0 if (ok_pure and ok_int) else 1)
