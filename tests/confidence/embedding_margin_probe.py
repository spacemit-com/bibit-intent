"""tests/confidence/embedding_margin_probe.py — embedding 候选注入阈值扫描探针。

目的：比较当前 dense-best 门控与 hybrid top1 / top2 margin 门控，在正负样本
      上的误注入率和召回率，为 embedding 软置信方案提供阈值数据。

用法（设备端）：
  .venv/bin/python tests/confidence/embedding_margin_probe.py
  .venv/bin/python tests/confidence/embedding_margin_probe.py --all-regression
  .venv/bin/python tests/confidence/embedding_margin_probe.py --thresholds 0.70,0.72,0.75,0.80

依赖：AI Gateway 的 Embed 模型；只读实验，不执行工具。
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from dataclasses import replace
from bibit_intent.models.backends import local_client, embedding_client
from bibit_intent.pipeline import PipelineSettings
from bibit_intent.routing.embedding import (
    EmbeddingClient,
    EmbeddingRetriever,
    default_index_paths,
    _dot,
    _group_score,
    _norm,
    _tokenize,
)
from regression import collect_cases


LABELED_CASES: tuple[tuple[str, int], ...] = (
    ("屏幕太刺眼了", 1),
    ("怎么查看系统内存", 1),
    ("帮我查一下当前CPU占用", 1),
    ("连不上家里的WiFi了", 1),
    ("讲个笑话", 0),
    ("今天天气怎么样", 0),
    ("给我转100块钱", 0),
    ("给李总发封邮件", 0),
    ("你是猪吗", 0),
    ("格式化磁盘", 0),
    ("怎么结束卡死的进程", 0),
)


def make_embed() -> EmbeddingRetriever:
    return EmbeddingRetriever(embedding_client(replace(PipelineSettings.from_environment(), local_timeout_seconds=60)), default_index_paths())


def features(embed: EmbeddingRetriever, text: str) -> dict | None:
    """计算与 route() 相同的 dense/sparse/hybrid 信号。"""
    vec = embed._client.embed(text)
    if not vec:
        return None
    qnorm = _norm(vec)
    if not qnorm:
        return None
    per_tool: dict[str, list[float]] = {}
    for e in embed._examples:
        if not e.norm:
            e.norm = _norm(e.vec)
        per_tool.setdefault(e.tool, []).append(_dot(vec, e.vec) / (qnorm * e.norm))

    dense = {t: _group_score(v) for t, v in per_tool.items()}
    best_tool = max(dense, key=dense.get)
    dense_best = max(per_tool[best_tool])

    sparse = embed._bm25.scores(_tokenize(text)) if embed._bm25 else {}
    mx = max(sparse.values()) if sparse else 0.0
    sparse_norm = {t: (s / mx if mx > 0 else 0.0) for t, s in sparse.items()}
    hybrid = {
        t: embed._dense_w * dense[t] + (1 - embed._dense_w) * sparse_norm.get(t, 0.0)
        for t in per_tool
    }
    ranked = sorted((t for t in per_tool if t != "ESCALATE"), key=hybrid.get, reverse=True)
    top1 = ranked[0] if ranked else None
    top2 = ranked[1] if len(ranked) > 1 else None
    return {
        "text": text,
        "best_tool": best_tool,
        "dense_best": dense_best,
        "top1": top1,
        "top1_score": hybrid.get(top1, 0.0) if top1 else 0.0,
        "top2_score": hybrid.get(top2, 0.0) if top2 else 0.0,
        "margin": (hybrid.get(top1, 0.0) - hybrid.get(top2, 0.0)) if top1 and top2 else 0.0,
        "ranked": ranked[:5],
    }


def current_gate(feat: dict, threshold: float) -> bool:
    return bool(feat and feat["best_tool"] == feat["top1"] and feat["dense_best"] >= threshold)


def hybrid_gate(feat: dict, threshold: float) -> bool:
    return bool(feat and feat["best_tool"] == feat["top1"] and feat["top1_score"] >= threshold)


def margin_gate(feat: dict, score_threshold: float, margin_threshold: float) -> bool:
    return bool(
        feat
        and feat["best_tool"] == feat["top1"]
        and feat["top1_score"] >= score_threshold
        and feat["margin"] >= margin_threshold
    )


def confusion(items: list[tuple[dict | None, int]], threshold: float, gate) -> dict:
    tp = fp = tn = fn = 0
    for feat, label in items:
        pred = gate(feat, threshold) if feat is not None else False
        if label == 1 and pred:
            tp += 1
        elif label == 1 and not pred:
            fn += 1
        elif label == 0 and pred:
            fp += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": tp, "fp": fp, "tn": tn, "fn": fn, "precision": precision, "recall": recall, "f1": f1}


def print_summary(items: list[tuple[dict | None, int]], thresholds: list[float], margin: float = 0.10) -> None:
    print(f"\nn={len(items)}")
    print(f"{'threshold':>9} | {'current':>32} | {'hybrid_top1':>32} | {'hybrid+margin':>32}")
    print(f"{'':>9} | {'P':>6} {'R':>6} {'F1':>6} {'FP':>4} | {'P':>6} {'R':>6} {'F1':>6} {'FP':>4} | {'P':>6} {'R':>6} {'F1':>6} {'FP':>4}")
    for t in thresholds:
        c = confusion(items, t, current_gate)
        h = confusion(items, t, hybrid_gate)
        m = {
            "tp": sum(1 for f, l in items if l == 1 and margin_gate(f, t, margin)),
            "fp": sum(1 for f, l in items if l == 0 and margin_gate(f, t, margin)),
            "tn": sum(1 for f, l in items if l == 0 and not margin_gate(f, t, margin)),
            "fn": sum(1 for f, l in items if l == 1 and not margin_gate(f, t, margin)),
        }
        for x in (c, h, m):
            x["precision"] = x["tp"] / (x["tp"] + x["fp"]) if x["tp"] + x["fp"] else 0.0
            x["recall"] = x["tp"] / (x["tp"] + x["fn"]) if x["tp"] + x["fn"] else 0.0
            x["f1"] = 2 * x["precision"] * x["recall"] / (x["precision"] + x["recall"]) if x["precision"] + x["recall"] else 0.0
        print(
            f"{t:>9.2f} | "
            f"{c['precision']:>6.3f} {c['recall']:>6.3f} {c['f1']:>6.3f} {c['fp']:>4} | "
            f"{h['precision']:>6.3f} {h['recall']:>6.3f} {h['f1']:>6.3f} {h['fp']:>4} | "
            f"{m['precision']:>6.3f} {m['recall']:>6.3f} {m['f1']:>6.3f} {m['fp']:>4}"
        )


def print_retrieval_summary(
    positives: list[tuple[dict | None, str]],
    negatives: list[tuple[dict | None, str]],
    thresholds: list[float],
    margin: float,
) -> None:
    """按离线回归标签报告：正样本 top1/top3 召回，负样本误注入率。"""
    pos_n = len(positives)
    neg_n = len(negatives)
    top1 = sum(1 for feat, tool in positives if feat and feat["top1"] == tool)
    top3 = sum(1 for feat, tool in positives if feat and tool in feat["ranked"][:3])
    print(f"\npositive n={pos_n}, negative n={neg_n}")
    print(f"retrieval top1={top1}/{pos_n} = {top1 / pos_n:.3f}" if pos_n else "retrieval top1=NA")
    print(f"retrieval top3={top3}/{pos_n} = {top3 / pos_n:.3f}" if pos_n else "retrieval top3=NA")
    print(f"\n{'threshold':>9} | {'current_FPR':>12} | {'hybrid_FPR':>11} | {'margin_FPR':>11} | {'margin_recall':>14}")
    for t in thresholds:
        cur = sum(1 for feat, _ in negatives if current_gate(feat, t)) / neg_n if neg_n else 0.0
        hyb = sum(1 for feat, _ in negatives if hybrid_gate(feat, t)) / neg_n if neg_n else 0.0
        mar = sum(1 for feat, _ in negatives if margin_gate(feat, t, margin)) / neg_n if neg_n else 0.0
        rec = sum(1 for feat, _ in positives if margin_gate(feat, t, margin)) / pos_n if pos_n else 0.0
        print(f"{t:>9.2f} | {cur:>12.3f} | {hyb:>11.3f} | {mar:>11.3f} | {rec:>14.3f}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all-regression", action="store_true", help="接入 tests/regression.py 全量 routing 用例")
    parser.add_argument("--thresholds", type=str, default="0.65,0.70,0.72,0.75,0.78,0.80,0.85")
    parser.add_argument("--margin", type=float, default=0.10)
    args = parser.parse_args()
    thresholds = [float(x) for x in args.thresholds.split(",") if x.strip()]

    embed = make_embed()
    embed.route("初始化")

    if args.all_regression:
        positives = []
        negatives = []
        for case in collect_cases():
            if case.kind != "routing":
                continue
            feat = features(embed, case.input)
            if case.expect_exit == "rule_call" and case.expect_tool:
                positives.append((feat, case.expect_tool))
            elif case.expect_exit == "cloud" or case.expect_empty_candidates:
                negatives.append((feat, case.input))
            else:
                continue
        print(f"embedding margin probe: {len(positives) + len(negatives)} regression/routing cases")
        print_retrieval_summary(positives, negatives, thresholds, args.margin)
    else:
        rows = []
        print(f"{'label':<5} {'输入':<20} {'best_tool':<16} {'dense_best':>7} {'top1':>7} {'top2':>7} {'margin':>7}")
        for text, label in LABELED_CASES:
            feat = features(embed, text)
            if feat is None:
                print(f"{label:<5} {text:<20} NO_EMBED")
                continue
            print(
                f"{label:<5} {text:<20} {feat['best_tool']:<16} {feat['dense_best']:>7.3f} "
                f"{feat['top1_score']:>7.3f} {feat['top2_score']:>7.3f} {feat['margin']:>7.3f}"
            )
            rows.append((feat, label))
        print_summary(rows, thresholds, args.margin)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
