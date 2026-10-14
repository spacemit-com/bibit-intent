"""
tests/anti_guess_probe.py — 缺参防猜真机探针（已配置 AI Gateway）

背景：端侧 1B 在 GBNF 强制输出数字的压力下，会把缺失数值猜成区间
      端点/中值（大→100/小→0，见 rule_and_intent_design.md §5.8）。
      本探针走 router 同款决策链路：
        规则 → embedding 候选 → 端侧模型 → 原文证据闸门（三级证据）
      只报告「最终会传给执行层的参数」（kept 直接执行 / uncertain
      挂起确认），不真正执行系统命令。

判定：
  - rule    规则直通（slot_map 已确定性补值，视为安全）
  - model   端侧模型 CALL：同时记录 模型原始参数(raw) 与 闸门后参数(final)，
            闸门把「原文有依据」的数值放进 final（直接执行），
            「模型推测」的数值放进 待确认（真机链路挂起反问，不盲执行）；
            若 final 里出现原文无依据的数值 = 闸门失效（FAIL）
  - escalate/无候选：不执行，视为安全

用法（在设备 /opt/bibit 下跑）：
  .venv/bin/python tests/anti_guess_probe.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dataclasses import replace
from bibit_intent.models.backends import local_client, embedding_client
from bibit_intent.pipeline import PipelineSettings
from bibit_intent.routing.embedding import (
    EmbeddingClient,
    EmbeddingRetriever,
    default_index_paths,
)
from bibit_intent.core.cn_number import extract_cn_numbers
from bibit_intent.routing.intent import IntentAction, decide_local_intent
from bibit_intent.models.local_model import LocalModelClient
from bibit_intent.routing.router import _keep_evidenced_params
from bibit_intent.routing.rule_engine import RuleEngine
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.resources import resource_path

# 缺参/方向词口语：用户原文里没有数字，模型不应猜数值；
# 相对方向词与极值词（大一点/拉满/最亮/最低）由 slot_map 确定性定值，
# 走 rule 直通，不算猜值（见 config/tools.yaml slot_map）。
CASES = [
    "把声音开大一点",
    "声音小一点",
    "帮我设置音量",
    "音量拉满",
    "音量百分之百",
    "音量调到百分之百",
    "音量调到五十",
    "亮度开一半",
    "屏幕亮一点",
    "帮我调一下亮度",
    "亮度调到最亮",
    "音量最低",
    "把亮度调到30",     # 有依据数值：应保留 30
    "音量调到60",       # 有依据数值：应保留 60
]


def build_registry() -> ToolRegistry:
    reg = ToolRegistry()
    load_skills(resource_path("skills"), reg, {})
    return reg


def main() -> int:
    reg = build_registry()
    rule = RuleEngine(reg)
    client = local_client(replace(PipelineSettings.from_environment(), local_timeout_seconds=60))
    embed = EmbeddingRetriever(
        embedding_client(replace(PipelineSettings.from_environment(), local_timeout_seconds=60)),
        default_index_paths(),
    )

    print(f"语料 {len(CASES)} 条，AI Gateway LLM，AI Gateway Embed\n")
    print(f"{'输入':<16} {'阶段':<7} {'工具':<16} {'raw参数':<18} {'执行参数':<14} 结论")
    print("-" * 90)

    fails = 0
    for inp in CASES:
        plan = decide_local_intent(inp, rule, reg, embed=embed)

        if plan.action is IntentAction.RULE_CALL:
            # 规则直通：slot_map 命中是确定性设计值（大→80），不算猜值；
            # 正则抽取的数字必须真实出现在原文（否则异常）。
            td = reg.get(plan.tool_name)
            sm = ((td.routes or {}).get("rule") or {}).get("slot_map") or {}
            # 正则路径 + slot_map 补值（如"亮度调到最亮"→100）：
            # 只要 slot_map 关键词命中原文，数值视为确定性设计值。
            slot_map_covered = any(key in inp for key in sm)
            bad = False if (plan.matched_pattern.startswith("slot_map:")
                            or slot_map_covered) else any(
                str(v) not in inp
                for k, v in plan.slots.items()
                if td is not None
                and td.params.get(k, type("", (), {"type": ""})).type
                in ("int", "float")
            )
            if bad:
                fails += 1
            print(f"{inp:<16} {'rule':<7} {plan.tool_name:<16} "
                  f"{str(plan.slots):<18} {str(plan.slots):<14} "
                  f"{'FAIL' if bad else 'OK'}")
            continue

        if plan.action is IntentAction.ESCALATE:
            print(f"{inp:<16} {'escalate':<7} {'—':<16} {'—':<18} {'—':<14} OK")
            continue

        r = client.infer(inp, plan.tool_list)
        if not r.is_call:
            print(f"{inp:<16} {'model':<7} {'—':<16} {r.raw:<18} {'—':<14} "
                  f"OK(升级/{r.action})")
            continue
        if not plan.hint and not plan.candidates:
            print(f"{inp:<16} {'model':<7} {'—':<16} {r.raw:<18} {'—':<14} "
                  f"OK(无候选拦截)")
            continue

        fallbacks = (
            [plan.hint] if plan.hint
            else plan.candidates if len(plan.candidates) == 1
            else []
        )
        tool = reg.resolve_with_fallback(r.tool_name, fallbacks)
        if tool is None:
            print(f"{inp:<16} {'model':<7} {'—':<16} {r.raw:<18} {'—':<14} "
                  f"OK(未知工具升级)")
            continue

        raw = dict(r.params or {})
        td = reg.get(tool)
        if tool != r.tool_name and td:
            raw = {k: v for k, v in raw.items() if k in td.params}
        ev = _keep_evidenced_params(inp, raw, td)
        final = ev.kept

        # 判定：final（直接执行）里不能有「原文无依据」的数值；
        # uncertain（模型推测值）→ 真机链路挂起确认，不算盲执行。
        cn_vals = set(extract_cn_numbers(inp))
        bad = any(
            td is not None and pdef.type in ("int", "float")
            and str(v) not in inp and v not in cn_vals
            for k, v in final.items()
            for pdef in [td.params.get(k, type("", (), {"type": ""}))]
        )
        if bad:
            fails += 1
        note = f"待确认={ev.uncertain}" if ev.uncertain else ""
        print(f"{inp:<16} {'model':<7} {tool:<16} {str(raw):<18} {str(final):<14} "
              f"{'FAIL' if bad else 'OK'}{' ' + note if note else ''}")

    print("-" * 90)
    print(f"失败：{fails}/{len(CASES)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
