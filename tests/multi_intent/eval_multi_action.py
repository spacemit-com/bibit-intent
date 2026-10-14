"""tests/multi_intent/eval_multi_action.py — 多动作检测对拍评测（全离线）。

用法：
    .venv/bin/python tests/multi_intent/eval_multi_action.py [corpus.jsonl]

语料为 BlendX/MixATIS 风格的多意图标注：每条含 input + label（是否多动作）。
本脚本只评测"检测"（不评测拆分），输出 P/R/F1 与逐条错误，供离线对拍与回归。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from bibit_intent.routing.multi_action import detect_multi_action

CORPUS = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name(
    "multi_action_corpus.jsonl"
)


def main() -> int:
    rows = [json.loads(line) for line in CORPUS.read_text(encoding="utf-8").splitlines()]
    tp = fp = tn = fn = 0
    errors: list[str] = []
    for row in rows:
        text, label = row["input"], bool(row["label"])
        pred = detect_multi_action(text).is_multi
        if label and pred:
            tp += 1
        elif label and not pred:
            fn += 1
            errors.append(f"FN  {row['id']}  {text!r} (应多动作, 判成单)")
        elif not label and pred:
            fp += 1
            errors.append(f"FP  {row['id']}  {text!r} (应单动作, 判成多)")
        else:
            tn += 1

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    acc = (tp + tn) / len(rows)

    print(f"corpus: {CORPUS}  n={len(rows)}")
    print(f"accuracy : {acc:.3f}")
    print(f"precision: {precision:.3f}")
    print(f"recall   : {recall:.3f}")
    print(f"f1       : {f1:.3f}")
    print(f"tp={tp} fp={fp} tn={tn} fn={fn}")
    if errors:
        print("\nerrors:")
        for e in errors:
            print("  " + e)
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
