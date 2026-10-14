"""
tests/test_local_model.py — 第二段小模型测试用例
分两部分：
  A. 输出解析（离线，python3 tests/test_local_model.py 直接跑）
  B. 语义推理（需 AI Gateway，须在设备上跑；服务不在线则自动跳过）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bibit_intent.routing.intent import IntentAction, decide_local_intent
from bibit_intent.models.local_model import (
    _SPECIAL_TOKEN_RESERVE,
    _build_system_prompt,
    _fit_prompt,
    _parse_output,
    LocalModelClient,
)
from bibit_intent.routing.rule_engine import RuleEngine
from bibit_intent.core.registry import ToolRegistry

# ── A. 输出解析用例 ───────────────────────────────────────────────────
# (原始输出, 期望action, 期望tool, 期望params, 期望reason)
PARSE_CASES = [
    # 合法 CALL
    ("CALL set_volume level=60", "CALL", "set_volume", {"level": 60}, None),
    ("CALL set_brightness level=30", "CALL", "set_brightness", {"level": 30}, None),
    # 布尔当前解析为字符串（执行层需消化）—— 记录真实行为
    ("CALL mute on=true", "CALL", "mute", {"on": "true"}, None),
    # 前后空白应被容忍
    ("   CALL set_volume level=60  ", "CALL", "set_volume", {"level": 60}, None),
    # 多参数
    ("CALL set_volume level=60 mode=fast", "CALL", "set_volume",
     {"level": 60, "mode": "fast"}, None),
    # 合法 ESCALATE
    ("ESCALATE reason=complex", "ESCALATE", None, None, "complex"),
    # 标签层输出（L3：CALL launch|close 无参数 / 枚举 reason）——_parse_output
    # 与 CALL 参数协议共用，保证 infer_label 的解析可直接复用
    ("CALL launch", "CALL", "launch", {}, None),
    ("CALL close", "CALL", "close", {}, None),
    ("ESCALATE reason=out_of_scope", "ESCALATE", None, None, "out_of_scope"),
    ("ESCALATE reason=missing_param", "ESCALATE", None, None, "missing_param"),
    # 非法输出 → 安全升级
    ("garbage output", "ESCALATE", None, None, "parse_failed"),
    ("", "ESCALATE", None, None, "parse_failed"),
    # 大写工具名（GBNF 只允许小写）→ 升级
    ("CALL Set_Volume level=60", "ESCALATE", None, None, "parse_failed"),
    # 无参工具（回归：曾因 grammar/正则强制 ≥1 参数而误升级，已修复）
    ("CALL reboot", "CALL", "reboot", {}, None),
    ("  CALL reboot  ", "CALL", "reboot", {}, None),
]


# ── B. 语义推理用例（需 AI Gateway；离线时跳过）────────────────────
# 只声明"输入 + 期望路由方向"，实际 tool/params 由模型决定，须设备实跑核对。
# 目的：验证「规则引擎够不着、但小模型应能理解」的场景。
SEMANTIC_CASES = [
    # (输入, 期望action, 期望tool 或 None, 说明)
    ("把声音开大一点",        "CALL",     "adjust_volume", "相对调整已拆分为 adjust_volume"),
    ("屏幕太暗了看不清",      "CALL",     "adjust_brightness","隐含调亮，规则相对亮度直通"),
    ("现在CPU占用高吗",       "CALL",     "get_system_info","CPU占用规则直通"),
    ("帮我写一首关于秋天的诗", "ESCALATE", None,            "端侧能力外，应升级到云端"),
    ("解释一下量子纠缠",      "ESCALATE", None,            "开放知识问答，应升级"),
]


def run_parse():
    passed = failed = 0
    print("== A. 输出解析用例（离线）==")
    for raw, ea, et, ep, er in PARSE_CASES:
        r = _parse_output(raw, 0.0)
        ok = (r.action == ea and r.tool_name == et
              and r.params == ep and r.reason == er)
        passed += ok; failed += (not ok)
        print(f"  {'OK ' if ok else 'XX '}{raw!r:36} -> {r.action} "
              f"tool={r.tool_name} params={r.params} reason={r.reason}")
    print(f"  小计：{passed}/{passed+failed}")
    return failed == 0


def run_prompt():
    """系统提示词结构检查（离线）：防猜指令必须在，且不破坏协议行。"""
    print("\n== 系统提示词（离线）==")
    p = _build_system_prompt("vol(level:0-100)")
    checks = [
        ("协议行", "CALL <工具>" in p),
        ("防猜指令", "禁止猜值" in p),
        ("省略指令", "省略" in p),
        ("升级指令", "ESCALATE" in p),
        (
            "预算内不截断",
            _fit_prompt(
                _build_system_prompt("vol(level:0-100)"),
                "静音",
                512 - 30 - _SPECIAL_TOKEN_RESERVE,
            )
            is not None,
        ),
        (
            "超预算直接升级不截断",
            _fit_prompt(
                _build_system_prompt("vol(level:0-100)"),
                "这是很长的中文输入，" * 30,
                512 - 30 - _SPECIAL_TOKEN_RESERVE,
            )
            is None,
        ),
    ]
    ok = all(ok for _, ok in checks)
    for name, good in checks:
        print(f"  {'OK ' if good else 'XX '}{name}")
    if not ok:
        print("  ---- 当前提示词 ----")
        print(p)
    return ok


def test_english_prompt_uses_english_protocol():
    from bibit_intent.analysis.features import UtteranceAnalysis
    analysis = UtteranceAnalysis(text="set volume to 60", language="en")
    prompt = _build_system_prompt("set_volume(level:0-100)", analysis)
    assert "You are an edge assistant" in prompt
    assert "CALL <tool>" in prompt
    assert "Only use values explicitly stated" in prompt
    assert "你是端侧助手" not in prompt


def run_semantic():
    print("\n== B. 语义推理用例（需 AI Gateway）==")
    # 从配置取 AI Gateway 地址（设备 .env / Gateway 配置），与 main.py 一致
    from bibit_intent.config import load_config
    cfg = load_config()
    from bibit_intent.models.backends import local_client
    client = local_client(cfg)
    if not client.is_available():
        print("  [跳过] AI Gateway 不在线。请在设备上启动后运行：")
        print("         python3 tests/test_local_model.py --semantic")
        for inp, ea, et, note in SEMANTIC_CASES:
            print(f"    · {inp!r:24} 期望 {ea:9} {et or '':16} # {note}")
        return None
    reg = ToolRegistry()
    rule = RuleEngine(reg)
    passed = failed = 0
    for inp, ea, et, note in SEMANTIC_CASES:
        # 用与 router 相同的意图决策（agent.intent），避免复制逻辑：
        plan = decide_local_intent(inp, rule, reg)
        if plan.action is IntentAction.RULE_CALL:
            res = _parse_output(f"CALL {plan.tool_name}", 0.0)   # 规则命中，不进模型
        elif plan.action is IntentAction.ESCALATE:
            res = _parse_output(f"ESCALATE reason={plan.reason}", 0.0)  # 前置升级
        else:
            res = client.infer(inp, plan.tool_list)              # 进第二段模型
        # 模型输出 short/近似名（vol/sysinfo）是设计允许的，用 fuzzy_resolve
        # 规范化（与执行层一致）。
        got_full = reg.fuzzy_resolve(res.tool_name) if res.tool_name else None
        # 有效路由方向：CALL 一个「无法解析的工具」（模型幻觉出端侧没有的
        # 能力，如 write_poem）等价于 ESCALATE —— 都表示端侧兜不住、交上层。
        eff_action = res.action
        if res.action == "CALL" and got_full is None:
            eff_action = "ESCALATE"
        ok = eff_action == ea and (et is None or got_full == et)
        passed += ok; failed += (not ok)
        note_arrow = ""
        if got_full and got_full != res.tool_name:
            note_arrow = f"→{got_full}"
        elif res.action == "CALL" and got_full is None:
            note_arrow = "→(未知工具,视同升级)"
        print(f"  {'OK ' if ok else 'XX '}{inp!r:24} -> {res.action} "
              f"{res.tool_name or ''}{note_arrow}"
              f"  ({res.latency_ms:.0f}ms)  # {note}")
    print(f"  小计：{passed}/{passed+failed}")
    return failed == 0


if __name__ == "__main__":
    ok = run_parse() and run_prompt()
    if "--semantic" in sys.argv:
        run_semantic()
    else:
        run_semantic()  # 离线时自动跳过并打印用例清单
    sys.exit(0 if ok else 1)
