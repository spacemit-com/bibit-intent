"""tests/confidence/soft_gate_online_ab.py — 全量在线语料的 SoftGate A/B 探针。

不执行任何工具，只复现 regression.py 的在线路由判定：
  - shadow：当前线上行为，SoftGate 只记录。
  - on    ：启用 pre/post SoftGate 后，哪些样本会被提前升级。
对比两者差异，重点统计「shadow 正确执行、on 会误升云」的本地正例。

用法（设备端）：
  .venv/bin/python tests/confidence/soft_gate_online_ab.py
  .venv/bin/python tests/confidence/soft_gate_online_ab.py --max-cases 40
  .venv/bin/python tests/confidence/soft_gate_online_ab.py --json /tmp/softgate_online_ab.json

依赖：AI Gateway 的 LLM/Embed 模型。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from dataclasses import replace
from bibit_intent.models.backends import local_client, embedding_client
from bibit_intent.pipeline import PipelineSettings
from bibit_intent.models.local_model import LocalModelClient
from bibit_intent.routing.intent import IntentAction, decide_local_intent
from bibit_intent.routing.rule_engine import RuleEngine
from bibit_intent.routing.soft_gate import (
    SoftGateConfig,
    post_model_escalate,
    pre_model_escalate,
)
from n_probs_calibration import build_production_rule
from regression import _collect_online_cases


def model_resolve(client, text, plan, reg, cfg):
    """调用模型并返回 (model_result, got_tool, pre_blocked, post_blocked, signal)。"""
    pre_blocked = pre_model_escalate(plan, cfg)
    try:
        r = client.infer(
            text, plan.tool_list, analysis=plan.analysis, n_probs=cfg.n_probs
        )
    except TypeError as exc:
        if "n_probs" not in str(exc):
            raise
        r = client.infer(text, plan.tool_list, analysis=plan.analysis)

    if not r.is_call:
        return r, None, pre_blocked, False, signal(plan, cfg)

    if not plan.hint and not plan.candidates:
        return r, None, pre_blocked, False, signal(plan, cfg)

    post_blocked = post_model_escalate(r, cfg)
    fallbacks = (
        [plan.hint]
        if plan.hint
        else plan.candidates
        if len(plan.candidates) == 1
        else []
    )
    got = reg.resolve_with_fallback(r.tool_name, fallbacks)
    return r, got, pre_blocked, post_blocked, signal(plan, cfg)


def signal(plan, cfg):
    """返回 post 组合门控所需的 embedding/候选信号。"""
    route = plan.embed_route
    embed_ok = False
    if plan.hint_source == "agreed":
        embed_ok = True
    elif plan.hint_source == "embed" and route is not None:
        embed_ok = (
            route.top1_score >= cfg.embed_top1_threshold
            and route.margin >= cfg.embed_margin_threshold
        )
    return {
        "hint_source": plan.hint_source,
        "candidate_count": len(plan.candidates),
        "top1_score": route.top1_score if route is not None else 0.0,
        "margin": route.margin if route is not None else 0.0,
        "embed_ok": embed_ok,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-cases", type=int, default=10000)
    parser.add_argument("--json", type=str, default=None)
    parser.add_argument("--lp", type=float, default=-2.2)
    parser.add_argument("--et", type=float, default=0.72)
    parser.add_argument("--margin", type=float, default=0.05)
    parser.add_argument("--n-probs", type=int, default=10)
    args = parser.parse_args()

    cfg = SoftGateConfig(
        mode="on",
        logprob_threshold=args.lp,
        embed_top1_threshold=args.et,
        embed_margin_threshold=args.margin,
        n_probs=args.n_probs,
    )
    reg, rule, embed = build_production_rule()
    client = local_client(replace(PipelineSettings.from_environment(), local_timeout_seconds=60))
    cases = _collect_online_cases()[: args.max_cases]

    shadow_correct = on_correct = 0
    both_wrong = 0
    false_local_escalations: list[dict] = []
    false_local_combined: list[dict] = []
    false_local_pre: list[dict] = []
    pre_hit_cases: list[dict] = []
    both_wrong_cases: list[dict] = []
    records: list[dict] = []
    pre_shadow = post_shadow = 0
    on_combined_correct = 0
    on_pre_correct = 0

    print(
        f"SoftGate online A/B: cases={len(cases)} lp>{args.lp} "
        f"top1>{args.et} margin>{args.margin}\n"
    )
    for c in cases:
        inp, expect = c.input, c.expect
        m = rule.match(inp)

        if m and m.confidence >= RuleEngine.CONFIDENCE_THRESHOLD:
            stage = "rule"
            shadow_got = on_got = m.tool_name
            on_combined_got = m.tool_name
            on_pre_got = m.tool_name
            pre = post = False
            avg_lp = 0.0
            sig = {"hint_source": "", "candidate_count": 0,
                   "top1_score": 0.0, "margin": 0.0, "embed_ok": False}
        else:
            plan = decide_local_intent(inp, rule, reg, embed=embed)
            if plan.action is IntentAction.ESCALATE:
                stage = "escalate"
                shadow_got = on_got = None
                pre = post = False
                avg_lp = 0.0
                sig = {"hint_source": "", "candidate_count": 0,
                       "top1_score": 0.0, "margin": 0.0, "embed_ok": False}
                on_combined_got = None
                on_pre_got = None
            else:
                stage = "model"
                model_result, shadow_got, pre, post, sig = model_resolve(
                    client, inp, plan, reg, cfg
                )
                avg_lp = getattr(model_result, "avg_logprob", 0.0)
                if pre:
                    pre_shadow += 1
                if post:
                    post_shadow += 1
                on_got = None if (pre or post) else shadow_got
                post_combined = post and not sig["embed_ok"]
                on_combined_got = None if (pre or post_combined) else shadow_got
                on_pre_got = None if pre else shadow_got
                if pre:
                    pre_hit_cases.append({
                        "id": c.id, "suite": c.suite, "input": inp,
                        "expect": expect, "shadow_got": shadow_got,
                    })

        shadow_ok = (shadow_got is None) if expect == "ESCALATE" else (shadow_got == expect)
        on_ok = (on_got is None) if expect == "ESCALATE" else (on_got == expect)
        on_combined_ok = (
            (on_combined_got is None)
            if expect == "ESCALATE"
            else (on_combined_got == expect)
        )
        on_pre_ok = (
            (on_pre_got is None)
            if expect == "ESCALATE"
            else (on_pre_got == expect)
        )
        shadow_correct += int(shadow_ok)
        on_correct += int(on_ok)
        on_combined_correct += int(on_combined_ok)
        on_pre_correct += int(on_pre_ok)
        if not shadow_ok and not on_ok:
            both_wrong += 1
            both_wrong_cases.append({
                "id": c.id, "suite": c.suite, "input": inp, "expect": expect,
                "shadow_got": shadow_got, "on_got": on_got, "stage": stage,
            })
        if shadow_ok and not on_ok and expect != "ESCALATE":
            false_local_escalations.append({
                "id": c.id,
                "suite": c.suite,
                "input": inp,
                "expect": expect,
                "shadow_got": shadow_got,
                "on_got": on_got,
                "pre": pre,
                "post": post,
                "avg_logprob": avg_lp,
                **sig,
            })
        if shadow_ok and not on_combined_ok and expect != "ESCALATE":
            false_local_combined.append({
                "id": c.id,
                "suite": c.suite,
                "input": inp,
                "expect": expect,
                "shadow_got": shadow_got,
                "pre": pre,
                "post_combined": post and not sig["embed_ok"],
                **sig,
            })
        if shadow_ok and not on_pre_ok and expect != "ESCALATE":
            false_local_pre.append({
                "id": c.id,
                "suite": c.suite,
                "input": inp,
                "expect": expect,
                "shadow_got": shadow_got,
                "pre": pre,
                **sig,
            })
        records.append({
            "id": c.id,
            "suite": c.suite,
            "input": inp,
            "expect": expect,
            "stage": stage,
            "shadow_got": shadow_got,
            "pre": pre,
            "post": post,
            "avg_logprob": avg_lp,
            **sig,
        })

    total = len(cases)
    print(f"total cases           : {total}")
    print(f"shadow correct        : {shadow_correct}/{total} = {shadow_correct/total:.3f}")
    print(f"on     correct        : {on_correct}/{total} = {on_correct/total:.3f}")
    print(f"on combined correct  : {on_combined_correct}/{total} = {on_combined_correct/total:.3f}")
    print(f"on pre-only correct  : {on_pre_correct}/{total} = {on_pre_correct/total:.3f}")
    print(f"both wrong            : {both_wrong}")
    print(f"shadow pre gate hits  : {pre_shadow}")
    print(f"shadow post gate hits : {post_shadow}")
    print(f"false local escalations (on vs shadow): {len(false_local_escalations)}")
    print(f"false local escalations (combined vs shadow): {len(false_local_combined)}")
    print(f"false local escalations (pre-only vs shadow): {len(false_local_pre)}")
    print(f"pre gate hit cases   : {len(pre_hit_cases)}")

    if pre_hit_cases:
        print("\n== pre gate hit cases ==")
        for r in pre_hit_cases:
            print(f"{r['id']:<20} {r['input']:<24} expect={r['expect']} shadow={r['shadow_got']}")

    if both_wrong_cases:
        print("\n== both wrong (model variance) ==")
        for r in both_wrong_cases:
            print(f"{r['id']:<20} {r['input']:<24} expect={r['expect']} shadow={r['shadow_got']}")

    if false_local_escalations:
        print("\n== false local escalations ==")
        print(f"{'id':<20} {'input':<24} {'expect':<20} {'shadow':<22} {'pre':<4} {'post':<4} {'avg_lp':>8} {'hint':<8} {'top1':>7} {'margin':>7} {'embed_ok':<8}")
        for r in false_local_escalations:
            print(
                f"{r['id']:<20} {r['input']:<24} {r['expect']:<20} "
                f"{str(r['shadow_got']):<22} {str(r['pre']):<4} {str(r['post']):<4} "
                f"{r.get('avg_logprob', 0):>8.3f} {r.get('hint_source') or '-':<8} "
                f"{r.get('top1_score', 0):>7.3f} {r.get('margin', 0):>7.3f} "
                f"{str(r.get('embed_ok', False)):<8}"
            )

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "total": total,
                    "shadow_correct": shadow_correct,
                    "on_correct": on_correct,
                    "on_combined_correct": on_combined_correct,
                    "on_pre_correct": on_pre_correct,
                    "both_wrong": both_wrong,
                    "pre_shadow": pre_shadow,
                    "post_shadow": post_shadow,
                    "false_local_escalations": false_local_escalations,
                    "false_local_combined": false_local_combined,
                    "false_local_pre": false_local_pre,
                    "pre_hit_cases": pre_hit_cases,
                    "both_wrong_cases": both_wrong_cases,
                    "records": records,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nJSON written: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
