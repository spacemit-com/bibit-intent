"""tests/confidence/soft_gate_shadow.py — 软门控 shadow mode 评估。

不修改线上决策：对每条本该进入端侧模型的样本，同时计算“如果启用软门控，
是否会提前升级”。输出模型调用节省比例、与标注不一致的跳过明细。

用法（设备端）：
  .venv/bin/python tests/confidence/soft_gate_shadow.py
  .venv/bin/python tests/confidence/soft_gate_shadow.py --lp -2.2 --et 0.72 --margin 0.05

依赖：AI Gateway 的 LLM/Embed 模型；只读实验，不执行工具。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dataclasses import replace
from bibit_intent.models.backends import local_client, embedding_client
from bibit_intent.pipeline import PipelineSettings
from bibit_intent.models.local_model import LocalModelClient
from bibit_intent.routing.intent import IntentAction, decide_local_intent
from embedding_margin_probe import features
from n_probs_calibration import build_production_rule, run_case
from soft_gate_cv import build_cases
from soft_gate_probe import embed_ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lp", type=float, default=-2.2)
    parser.add_argument("--et", type=float, default=0.72)
    parser.add_argument("--margin", type=float, default=0.05)
    args = parser.parse_args()

    reg, rule, embed = build_production_rule()
    client = local_client(replace(PipelineSettings.from_environment(), local_timeout_seconds=60))
    cases = build_cases()

    model_total = 0
    skip_total = 0
    skip_correct_oos = 0
    skip_false_local = 0
    skip_rows: list[dict] = []
    rule_total = 0
    escalate_total = 0

    print(
        f"soft gate shadow: lp>{args.lp}, embed_top1>{args.et}, margin>{args.margin}\n"
    )
    for text, label, group in cases:
        plan = decide_local_intent(text, rule, reg, embed=embed)
        if plan.action is IntentAction.RULE_CALL:
            rule_total += 1
            continue
        if plan.action is IntentAction.ESCALATE:
            escalate_total += 1
            continue

        model_total += 1
        feat = features(embed, text)
        row = run_case(client, text, plan)
        lp_ok = row.get("avg_logprob", -10.0) >= args.lp
        e_ok = embed_ok(feat, plan.hint_source, args.et, args.margin)
        gate_oos = not (lp_ok and e_ok)
        if gate_oos:
            skip_total += 1
            if label == 1:
                skip_correct_oos += 1
            else:
                skip_false_local += 1
            skip_rows.append({
                "input": text,
                "label": label,
                "group": group,
                "hint_source": plan.hint_source,
                "avg_logprob": row.get("avg_logprob", -10.0),
                "top1_score": (feat or {}).get("top1_score", 0.0),
                "margin": (feat or {}).get("margin", 0.0),
                "model_action": row.get("action"),
                "model_tool": row.get("tool"),
            })

    print(f"total cases        : {len(cases)}")
    print(f"rule path          : {rule_total}")
    print(f"deterministic cloud: {escalate_total}")
    print(f"would be model     : {model_total}")
    print(f"shadow skip model  : {skip_total}")
    if model_total:
        print(f"model-call reduction: {skip_total / model_total:.3f} ({skip_total}/{model_total})")
    print(f"skip correct (OOS) : {skip_correct_oos}")
    print(f"skip false (local) : {skip_false_local}")

    if skip_rows:
        print("\n== shadow skip details ==")
        print(f"{'input':<20} {'label':<5} {'hint':<8} {'avg_lp':>8} {'top1':>7} {'margin':>7} {'model_raw':<28}")
        for r in skip_rows:
            target = r["model_tool"] or r["model_action"]
            print(
                f"{r['input']:<20} {r['label']:<5} {r['hint_source'] or '-':<8} "
                f"{r['avg_logprob']:>8.3f} {r['top1_score']:>7.3f} {r['margin']:>7.3f} "
                f"{r['model_action']} {target:<24}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
