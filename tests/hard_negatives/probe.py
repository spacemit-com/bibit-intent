"""
tests/hard_negatives/probe.py — 硬负样本评测探针（全离线，可独立运行）

与 test_hard_negatives.py 同一判据；单独运行便于在设备 /opt/bibit 下快速回归：
  .venv/bin/python tests/hard_negatives/probe.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.resources import resource_path
from bibit_intent.routing.intent import IntentAction, decide_local_intent
from bibit_intent.routing.rule_engine import RuleEngine


def main() -> int:
    reg = ToolRegistry(resource_path("tools.yaml"))
    load_skills(resource_path("skills"), reg, {})
    rule = RuleEngine(reg)

    corpus = Path(__file__).with_name("corpus.jsonl")
    cases = [
        json.loads(line)
        for line in corpus.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    passed = failed = 0
    for case in cases:
        # 与 test_hard_negatives.py 一致：注入"云端可达"，使结果不随测试机网络漂移
        plan = decide_local_intent(
            case["input"], rule, reg, embed=None, cloud_probe=lambda: True
        )
        expected = case.get("expect", "no_rule_call")
        if expected == "escalate":
            ok = plan.action is IntentAction.ESCALATE
        elif expected == "no_candidates":
            ok = plan.action is not IntentAction.RULE_CALL and not plan.candidates
        else:
            ok = plan.action is not IntentAction.RULE_CALL

        passed += bool(ok)
        failed += (not ok)
        detail = (
            f"{plan.action.value} tool={plan.tool_name or '-'} "
            f"reason={plan.reason or '-'} candidates={plan.candidates}"
        )
        mark = "OK " if ok else "XX "
        print(f"  {mark}{case['id']:5s} {case['input']!r:28} -> {detail}")

    print(f"\n硬负样本评测: {passed}/{len(cases)} 通过，{failed} 失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
