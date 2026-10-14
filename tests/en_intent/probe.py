"""Offline English intent probe: deterministic rules and escalation guards."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.resources import resource_path
from bibit_intent.routing.rule_engine import RuleEngine
from bibit_intent.routing.intent import decide_local_intent, IntentAction

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=str(Path(__file__).with_name("corpus.jsonl")))
    args = ap.parse_args()
    reg = ToolRegistry(); load_skills(resource_path("skills"), reg, {})
    rule = RuleEngine(reg)
    rows = [json.loads(x) for x in Path(args.corpus).read_text(encoding="utf-8").splitlines() if x.strip()]
    ok = 0
    for row in rows:
        if row.get("group") == "anchor":
            print(f"ANCHOR {row['id']}: {row['input']} -> {row['expect']}")
            continue
        plan = decide_local_intent(row["input"], rule, reg)
        if plan.action is IntentAction.RULE_CALL:
            got = plan.tool_name
        elif plan.action is IntentAction.ESCALATE:
            got = plan.reason
        else:
            got = "model"
        passed = got == row["expect"]
        ok += passed
        print(f"{'OK' if passed else 'XX'} {row['id']}: {row['input']} -> {got} (expect {row['expect']})")
    total = sum(1 for r in rows if r.get("group") != "anchor")
    print(f"English probe: {ok}/{total} ({ok/total:.1%})")
    return 0 if ok == total else 1

if __name__ == "__main__":
    raise SystemExit(main())
