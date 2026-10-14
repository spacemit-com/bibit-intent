"""tests/confidence/soft_gate_probe.py — n_probs + embedding margin 组合软置信探针。

目的：在进入端侧模型前，用 embedding 候选信号和模型 logprob 组合判断是否应
      提前 ESCALATE（域外/闲聊/危险 OOS），并评估对本地控制指令的误伤率。

用法（设备端）：
  .venv/bin/python tests/confidence/soft_gate_probe.py

依赖：AI Gateway 的 LLM/Embed 模型；只读实验，不执行工具。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dataclasses import replace
from bibit_intent.models.backends import local_client, embedding_client
from bibit_intent.pipeline import PipelineSettings
from bibit_intent.routing.intent import IntentAction, decide_local_intent
from embedding_margin_probe import features
from n_probs_calibration import build_production_rule, run_case


# label=1 表示应提前上云（OOS/危险输入）；label=0 表示应保留本地模型路径。
LABELED_CASES: tuple[tuple[str, int], ...] = (
    ("把音量调到60", 0),
    ("音量调到六十", 0),
    ("帮我设置音量", 0),
    ("帮我调一下亮度", 0),
    ("屏幕太刺眼了", 0),
    ("连不上家里的WiFi了", 0),
    ("调底音量", 0),
    ("讲个笑话", 1),
    ("今天天气怎么样", 1),
    ("给我转100块钱", 1),
    ("给李总发封邮件", 1),
    ("你是猪吗", 1),
    ("格式化磁盘", 1),
    ("怎么结束卡死的进程", 1),
)

LP_THRESHOLDS = (-2.4, -2.2, -2.0, -1.8)
EMBED_THRESHOLDS = (
    (0.72, 0.05),
    (0.75, 0.10),
    (0.78, 0.10),
    (0.80, 0.15),
)


def embed_ok(feat: dict | None, hint_source: str, e_t: float, e_m: float) -> bool:
    if hint_source == "agreed":
        return True
    if hint_source == "embed" and feat is not None:
        return feat["top1_score"] >= e_t and feat["margin"] >= e_m
    return False


def metrics(records: list[dict], labels: list[int], pred) -> dict:
    tp = fp = tn = fn = 0
    for rec, label in zip(records, labels):
        p = pred(rec)
        if label == 1 and p:
            tp += 1
        elif label == 1 and not p:
            fn += 1
        elif label == 0 and p:
            fp += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": tp, "fp": fp, "tn": tn, "fn": fn, "precision": precision, "recall": recall, "f1": f1}


def print_metric_block(title: str, records: list[dict], labels: list[int], preds: list[tuple[str, callable]]) -> None:
    print(f"\n== {title} ==")
    print(f"{'params':<16} {'P':>6} {'R':>6} {'F1':>6} {'FP':>4} {'FN':>4}")
    for name, pred in preds:
        m = metrics(records, labels, pred)
        print(f"{name:<16} {m['precision']:>6.3f} {m['recall']:>6.3f} {m['f1']:>6.3f} {m['fp']:>4} {m['fn']:>4}")


def main() -> int:
    reg, rule, embed = build_production_rule()
    client = None
    from bibit_intent.models.local_model import LocalModelClient
    client = local_client(replace(PipelineSettings.from_environment(), local_timeout_seconds=60))

    records: list[dict] = []
    labels: list[int] = []

    print(f"{'input':<20} {'label':<5} {'stage':<9} {'hint':<8} {'best':>7} {'margin':>7} {'avg_lp':>8} {'action':<7}")
    for text, label in LABELED_CASES:
        plan = decide_local_intent(text, rule, reg, embed=embed)
        labels.append(label)
        if plan.action is IntentAction.ESCALATE:
            rec = {"stage": "escalate", "label": label, "input": text}
            records.append(rec)
            print(f"{text:<20} {label:<5} {'escalate':<9} {'-':<8} {'-':>7} {'-':>7} {'-':>8} {'-':<7}")
            continue
        if plan.action is IntentAction.RULE_CALL:
            rec = {"stage": "rule", "label": label, "input": text}
            records.append(rec)
            print(f"{text:<20} {label:<5} {'rule':<9} {'-':<8} {'-':>7} {'-':>7} {'-':>8} {'-':<7}")
            continue

        feat = features(embed, text)
        row = run_case(client, text, plan)
        rec = {
            "stage": "model",
            "label": label,
            "input": text,
            "hint_source": plan.hint_source,
            "avg_logprob": row.get("avg_logprob", -10.0),
            "feat": feat,
        }
        records.append(rec)
        print(
            f"{text:<20} {label:<5} {'model':<9} {plan.hint_source or '-':<8} "
            f"{(feat or {}).get('dense_best', 0):>7.3f} {(feat or {}).get('margin', 0):>7.3f} "
            f"{row.get('avg_logprob', 0):>8.3f} {row.get('action', '-'):<7}"
        )

    # 对非模型阶段，预测使用确定性阶段；模型阶段由当前 lambda 组合。
    def lp_pred(t: float):
        def f(rec):
            if rec["stage"] == "escalate":
                return True
            if rec["stage"] == "rule":
                return False
            return rec["avg_logprob"] < t
        return f

    def emb_pred(e_t: float, e_m: float):
        def f(rec):
            if rec["stage"] == "escalate":
                return True
            if rec["stage"] == "rule":
                return False
            return not embed_ok(rec.get("feat"), rec.get("hint_source", ""), e_t, e_m)
        return f

    def both_pred(lp_t: float, e_t: float, e_m: float):
        def f(rec):
            if rec["stage"] == "escalate":
                return True
            if rec["stage"] == "rule":
                return False
            lp_ok = rec["avg_logprob"] >= lp_t
            e_ok = embed_ok(rec.get("feat"), rec.get("hint_source", ""), e_t, e_m)
            return not (lp_ok and e_ok)
        return f

    def either_pred(lp_t: float, e_t: float, e_m: float):
        def f(rec):
            if rec["stage"] == "escalate":
                return True
            if rec["stage"] == "rule":
                return False
            lp_ok = rec["avg_logprob"] >= lp_t
            e_ok = embed_ok(rec.get("feat"), rec.get("hint_source", ""), e_t, e_m)
            return not (lp_ok or e_ok)
        return f

    print_metric_block(
        "logprob only",
        records,
        labels,
        [(f"lp>{t}", lp_pred(t)) for t in LP_THRESHOLDS],
    )
    print_metric_block(
        "embedding only",
        records,
        labels,
        [(f"e>{e_t},m>{e_m}", emb_pred(e_t, e_m)) for e_t, e_m in EMBED_THRESHOLDS],
    )

    both = []
    either = []
    for lp_t in LP_THRESHOLDS:
        for e_t, e_m in EMBED_THRESHOLDS:
            both.append((f"lp>{lp_t},e>{e_t},m>{e_m}", both_pred(lp_t, e_t, e_m)))
            either.append((f"lp>{lp_t},e>{e_t},m>{e_m}", either_pred(lp_t, e_t, e_m)))
    print_metric_block("combined: AND", records, labels, both)
    print_metric_block("combined: OR", records, labels, either)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
