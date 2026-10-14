"""tests/confidence/soft_gate_cv.py — 组合软置信门控扩充语料与交叉验证。

目的：用更大的本地控制 / OOS 标注集做 5-fold 交叉验证，避免只凭上一轮 14 条
      小样本得出生效结论。每条模型样本只推理一次，再复用特征做阈值选择。

用法（设备端）：
  .venv/bin/python tests/confidence/soft_gate_cv.py
  .venv/bin/python tests/confidence/soft_gate_cv.py --k 5 --seed 42

依赖：AI Gateway 的 LLM/Embed 模型；只读实验，不执行工具。
"""

from __future__ import annotations

import argparse
import random
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
from soft_gate_probe import embed_ok


LP_THRESHOLDS = (-2.4, -2.2, -2.0, -1.8)
EMBED_THRESHOLDS = (
    (0.72, 0.05),
    (0.75, 0.10),
    (0.78, 0.10),
    (0.80, 0.15),
)


def build_cases() -> list[tuple[str, int, str]]:
    """生成扩充标注集。label=1 表示应上云，0 表示应保留本地模型路径。"""
    local: list[tuple[str, int, str]] = []
    oos: list[tuple[str, int, str]] = []

    for n in (10, 20, 30, 50, 60, 70, 80, 90):
        local.append((f"把音量调到{n}", 0, "volume"))
    for n in (20, 40, 60, 80):
        local.append((f"音量调到{n}", 0, "volume"))
    for n in (20, 30, 50, 70, 80):
        local.append((f"把亮度调到{n}", 0, "brightness"))
    for n in (30, 60, 90):
        local.append((f"亮度调到{n}", 0, "brightness"))

    local.extend([
        ("把声音开大一点", 0, "volume_relative"),
        ("音量拉满", 0, "volume_relative"),
        ("音量最低", 0, "volume_relative"),
        ("屏幕亮一点", 0, "brightness_relative"),
        ("亮度调到最亮", 0, "brightness_relative"),
        ("帮我设置音量", 0, "volume_missing"),
        ("帮我调一下亮度", 0, "brightness_missing"),
        ("连不上家里的WiFi了", 0, "wifi"),
        ("帮我查一下当前CPU占用", 0, "sysinfo"),
        ("打开浏览器", 0, "launch"),
        ("关闭终端", 0, "close"),
        ("把屏幕亮度调到80", 0, "brightness"),
        ("把声音调小一点", 0, "volume_relative"),
        ("调底音量", 0, "noise"),
    ])

    for n in (50, 100, 200):
        oos.append((f"给我转{n}块钱", 1, "transfer"))
    oos.extend([
        ("讲个笑话", 1, "chitchat"),
        ("今天天气怎么样", 1, "weather"),
        ("给李总发封邮件", 1, "email"),
        ("帮我查一下 root 密码", 1, "credential"),
        ("格式化磁盘", 1, "dangerous"),
        ("删除所有文件", 1, "dangerous"),
        ("你是猪吗", 1, "insult"),
        ("帮我写个Python脚本", 1, "writing"),
        ("帮我写周报", 1, "writing"),
        ("推荐一本小说", 1, "chitchat"),
        ("今晚吃什么", 1, "chitchat"),
        ("谁是牛顿", 1, "knowledge"),
        ("翻译一下这段英文", 1, "writing"),
        ("总结一下这篇文章", 1, "writing"),
        ("订一张去北京的机票", 1, "oostask"),
        ("帮我约个会议室", 1, "oostask"),
        ("怎么提高英语", 1, "knowledge"),
        ("说明一下相对论", 1, "knowledge"),
        ("推荐个旅游地点", 1, "chitchat"),
        ("给我讲个故事", 1, "chitchat"),
        ("我要炒股", 1, "oostask"),
        ("帮我做一份PPT", 1, "writing"),
        ("计算一下明天涨跌", 1, "oostask"),
        ("怎么结束卡死的进程", 1, "near_miss_question"),
        ("打开桌面的年度总结报告", 1, "file_open"),
        ("检查一下磁盘里有没有我的照片", 1, "file_search"),
        ("帮我查一下内存里装的什么歌", 1, "file_search"),
        ("把亮度调成彩虹色", 1, "unrepresentable"),
    ])

    return local + oos


def confusion(preds: list[int], labels: list[int]) -> dict:
    tp = fp = tn = fn = 0
    for p, y in zip(preds, labels):
        if y == 1 and p == 1:
            tp += 1
        elif y == 1 and p == 0:
            fn += 1
        elif y == 0 and p == 1:
            fp += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": tp, "fp": fp, "tn": tn, "fn": fn, "precision": precision, "recall": recall, "f1": f1}


def predict(records: list[dict], indices: list[int], lp_t: float, e_t: float, e_m: float) -> list[int]:
    out = []
    for idx in indices:
        rec = records[idx]
        if rec["stage"] == "escalate":
            out.append(1)
        elif rec["stage"] == "rule":
            out.append(0)
        else:
            lp_ok = rec["avg_logprob"] >= lp_t
            e_ok = embed_ok(rec.get("feat"), rec.get("hint_source", ""), e_t, e_m)
            out.append(0 if (lp_ok and e_ok) else 1)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    reg, rule, embed = build_production_rule()
    client = local_client(replace(PipelineSettings.from_environment(), local_timeout_seconds=60))
    cases = build_cases()
    records: list[dict] = []
    labels: list[int] = []

    print(f"collecting features for {len(cases)} cases...\n")
    for text, label, group in cases:
        labels.append(label)
        plan = decide_local_intent(text, rule, reg, embed=embed)
        if plan.action is IntentAction.ESCALATE:
            records.append({"stage": "escalate", "input": text, "group": group, "label": label})
        elif plan.action is IntentAction.RULE_CALL:
            records.append({"stage": "rule", "input": text, "group": group, "label": label})
        else:
            feat = features(embed, text)
            row = run_case(client, text, plan)
            records.append({
                "stage": "model",
                "input": text,
                "group": group,
                "label": label,
                "hint_source": plan.hint_source,
                "avg_logprob": row.get("avg_logprob", -10.0),
                "feat": feat,
            })

    pos_idx = [i for i, y in enumerate(labels) if y == 1]
    neg_idx = [i for i, y in enumerate(labels) if y == 0]
    rng = random.Random(args.seed)
    rng.shuffle(pos_idx)
    rng.shuffle(neg_idx)
    k = max(2, min(args.k, len(pos_idx), len(neg_idx)))

    folds: list[tuple[list[int], list[int]]] = []
    for i in range(k):
        test = (
            pos_idx[i::k] + neg_idx[i::k]
        )
        test_set = set(test)
        train = [j for j in range(len(labels)) if j not in test_set]
        folds.append((train, test))

    print(f"5-fold CV: {len(labels)} cases, k={k}\n")
    fold_metrics = []
    for fold, (train, test) in enumerate(folds, 1):
        best = None
        best_f1 = -1.0
        for lp_t in LP_THRESHOLDS:
            for e_t, e_m in EMBED_THRESHOLDS:
                preds = predict(records, train, lp_t, e_t, e_m)
                m = confusion(preds, [labels[i] for i in train])
                if m["f1"] > best_f1 or (m["f1"] == best_f1 and best and m["fp"] < best["train"]["fp"]):
                    best_f1 = m["f1"]
                    best = {"lp_t": lp_t, "e_t": e_t, "e_m": e_m, "train": m}
        test_preds = predict(records, test, best["lp_t"], best["e_t"], best["e_m"])
        test_labels = [labels[i] for i in test]
        test_m = confusion(test_preds, test_labels)
        fold_metrics.append(test_m)
        print(
            f"fold {fold}: params lp>{best['lp_t']},e>{best['e_t']},m>{best['e_m']} | "
            f"train P={best['train']['precision']:.3f} R={best['train']['recall']:.3f} "
            f"F1={best['train']['f1']:.3f} | test P={test_m['precision']:.3f} "
            f"R={test_m['recall']:.3f} F1={test_m['f1']:.3f} FP={test_m['fp']} FN={test_m['fn']}"
        )

    avg = {k: sum(m[k] for m in fold_metrics) / len(fold_metrics) for k in ("precision", "recall", "f1")}
    avg_fp = sum(m["fp"] for m in fold_metrics) / len(fold_metrics)
    avg_fn = sum(m["fn"] for m in fold_metrics) / len(fold_metrics)
    print("\n== CV summary ==")
    print(f"precision: {avg['precision']:.3f}")
    print(f"recall   : {avg['recall']:.3f}")
    print(f"f1       : {avg['f1']:.3f}")
    print(f"avg FP   : {avg_fp:.2f}")
    print(f"avg FN   : {avg_fn:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
