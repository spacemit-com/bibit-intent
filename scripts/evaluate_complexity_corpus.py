"""Evaluate labelled requests without invoking models or tools."""
import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import platform
import resource
import time

from bibit_intent.routing.complexity import ComplexityThresholds, classify_complexity


def load_rows(path):
    rows = []
    identities = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        row = json.loads(line)
        if not isinstance(row.get("text"), str) or not row["text"].strip():
            raise ValueError(f"line {line_number}: nonempty text required")
        if row.get("label") not in {"simple", "complex"}:
            raise ValueError(f"line {line_number}: simple/complex label required")
        identity = row.get("id", row["text"])
        if identity in identities:
            raise ValueError(f"line {line_number}: duplicate id/text")
        identities.add(identity)
        rows.append(row)
    if not rows:
        raise ValueError("empty corpus")
    return rows


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def metrics(rows):
    counts = Counter()
    for row in rows:
        actual, predicted = row["label"], row["predicted"]
        key = ("tp" if actual == predicted == "complex" else "tn" if actual == predicted == "simple"
               else "fp" if predicted == "complex" else "fn")
        counts[key] += 1
    return {
        "count": len(rows), **{key: counts[key] for key in ("tp", "tn", "fp", "fn")},
        "accuracy": ratio(counts["tp"] + counts["tn"], len(rows)),
        "complex_precision": ratio(counts["tp"], counts["tp"] + counts["fp"]),
        "complex_recall": ratio(counts["tp"], counts["tp"] + counts["fn"]),
        "false_simple_rate": ratio(counts["fn"], counts["tp"] + counts["fn"]),
        "false_complex_rate": ratio(counts["fp"], counts["tn"] + counts["fp"]),
    }


def evaluate(rows, thresholds, repeats=5):
    results = []
    before_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    started = time.perf_counter_ns()
    classify_complexity(rows[0]["text"], thresholds=thresholds)
    cold_ms = (time.perf_counter_ns() - started) / 1_000_000
    latencies = []
    for repeat in range(repeats):
        for row in rows:
            started = time.perf_counter_ns()
            result = classify_complexity(row["text"], thresholds=thresholds)
            latencies.append((time.perf_counter_ns() - started) / 1_000_000)
            if repeat == 0:
                results.append({**row, "predicted": result.complexity.value,
                                "reason": result.escalation_reason, "score": result.score})
    latencies.sort()
    report = {
        "scope": "classifier only; simple is not evidence of local tool execution",
        "metrics": metrics(results),
        "by_language": {language: metrics([row for row in results if row.get("language", "unspecified") == language])
                        for language in sorted({row.get("language", "unspecified") for row in results})},
        "thresholds": asdict(thresholds),
        "annotation_status": dict(Counter(row.get("annotation_status", "unspecified") for row in rows)),
        "platform": platform.platform(), "python": platform.python_version(),
        "performance": {"cold_ms": cold_ms, "warm_p50_ms": latencies[len(latencies) // 2],
                        "warm_p95_ms": latencies[min(len(latencies) - 1, int(len(latencies) * .95))],
                        "calls": len(latencies), "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                        "peak_rss_delta_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before_rss},
        "mismatches": [row for row in results if row["predicted"] != row["label"]],
        "prediction_sha256": hashlib.sha256(json.dumps(results, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
    }
    return report

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    report = evaluate(load_rows(args.corpus), ComplexityThresholds.from_environment(), args.repeats)
    report["corpus_sha256"] = hashlib.sha256(args.corpus.read_bytes()).hexdigest()
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
