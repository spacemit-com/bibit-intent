"""tests/regression.py — 意图识别统一回归脚本（批量运行，输出统一 JSON）。

按 docs/evaluation/intent_evaluation_methodology.md §4/§5/§7 落地：
  1. 把散落的离线确定性语料统一成一份批量回归入口；
  2. 给每类语料定义「期望出口」与通过线；
  3. 输出统一 JSON、出口混淆矩阵、OOS/误执行率与失败明细；
  4. `--online` 时把 device 侧在线正例 probe（system_control/ubuntu_cmd）
     按同一 JSON 口径收编，避免指标散落在多个打印输出里。

离线口径：只走 `decide_local_intent(..., embed=None)` 与 `QuestionDetector`/
`detect_multi_action` 的确定性层，不调用 embedding 与端侧模型，秒级可回归。

统一出口标签（方法论 §2 的 L0/L1 层）：
  rule_call —— 规则高置信直通执行
  cloud     —— 确定性前置升级云端（reason 细分在失败明细中保留）
  to_model  —— 进入端侧模型（附候选清单）

用法：
  .venv/bin/python tests/regression.py                    # 离线汇总 + 写 JSON
  .venv/bin/python tests/regression.py --online --n 3     # 加跑 device 在线正例
  .venv/bin/python tests/regression.py --json /tmp/x.json
  .venv/bin/python -m pytest tests/test_intent_eval.py    # pytest 离线批量入口
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
sys.path.insert(0, str(ROOT / "src"))

from dataclasses import replace
from bibit_intent.models.backends import local_client, embedding_client
from bibit_intent.pipeline import PipelineSettings
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.resources import resource_path
from bibit_intent.routing.intent import IntentAction, decide_local_intent
from bibit_intent.routing.multi_action import detect_multi_action
from bibit_intent.routing.question_detector import QuestionDetector
from bibit_intent.routing.rule_engine import RuleEngine

EXIT_LABELS = ("rule_call", "cloud", "to_model")
_ACTION_TO_EXIT = {
    IntentAction.RULE_CALL: "rule_call",
    IntentAction.ESCALATE: "cloud",
    IntentAction.TO_MODEL: "to_model",
}


@dataclass
class Case:
    """统一离线用例。kind 决定用哪条确定性链路判定。"""

    id: str
    suite: str
    kind: str  # routing | multi | question
    input: str
    # routing
    expect_exit: str = "any"  # rule_call | cloud | to_model | not_rule_call | any
    expect_tool: str | None = None
    expect_reason: str | None = None
    expect_empty_candidates: bool = False
    forbid_reason: str | None = None
    # multi
    expect_multi: bool | None = None
    # question
    expect_question: bool | None = None
    expect_layer: str | None = None
    note: str = ""


@dataclass
class Outcome:
    ok: bool
    pred_exit: str | None = None
    pred_tool: str | None = None
    pred_reason: str | None = None
    pred_candidates: list[str] = field(default_factory=list)
    pred_multi: bool | None = None
    pred_question: bool | None = None
    pred_layer: str | None = None
    detail: str = ""


@dataclass
class OnlineCase:
    """在线正例探针用例：expect 为工具短名或 `ESCALATE`。"""

    id: str
    suite: str
    group: str
    input: str
    expect: str


def build_registry() -> ToolRegistry:
    """与 router/pipeline 一致：内置 tools.yaml + skills/。"""
    reg = ToolRegistry(resource_path("tools.yaml"))
    load_skills(resource_path("skills"), reg, {})
    return reg


def _load_module(name: str, path: Path):
    """把非包内的脚本测试模块作为数据源导入，复用其用例常量（单一数据源）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载模块 {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _collect_intent_cases() -> list[Case]:
    mod = _load_module("_reg_intent_cases", TESTS / "test_intent.py")
    cases: list[Case] = []

    for idx, (text, exp_action, exp_key, note) in enumerate(mod.INTENT_CASES):
        if exp_action is IntentAction.RULE_CALL:
            cases.append(Case(
                id=f"intent{idx:03d}", suite="intent", kind="routing", input=text,
                expect_exit="rule_call", expect_tool=exp_key, note=note,
            ))
        elif exp_action is IntentAction.ESCALATE:
            cases.append(Case(
                id=f"intent{idx:03d}", suite="intent", kind="routing", input=text,
                expect_exit="cloud", expect_reason=exp_key, note=note,
            ))
        else:  # TO_MODEL
            cases.append(Case(
                id=f"intent{idx:03d}", suite="intent", kind="routing", input=text,
                expect_exit="to_model", note=note,
            ))

    for idx, text in enumerate(mod.EMPTY_CANDIDATE_CASES):
        cases.append(Case(
            id=f"empty{idx:03d}", suite="intent", kind="routing", input=text,
            expect_exit="to_model", expect_empty_candidates=True,
            note="无关输入不注入候选",
        ))

    for idx, text in enumerate(mod.CONTEXT_DEPENDENT_CASES):
        cases.append(Case(
            id=f"ctx{idx:03d}", suite="intent", kind="routing", input=text,
            expect_exit="cloud", expect_reason="context_dependent",
            note="上下文依赖输入统一上云",
        ))

    for idx, text in enumerate(mod.NON_CONTEXT_DEPENDENT_CASES):
        cases.append(Case(
            id=f"nctx{idx:03d}", suite="intent", kind="routing", input=text,
            expect_exit="any", forbid_reason="context_dependent",
            note="完整单轮指令不被上下文规则误拦截",
        ))
    return cases


def _collect_hard_negative_cases() -> list[Case]:
    corpora = [TESTS / "hard_negatives" / "corpus.jsonl"]
    en = TESTS / "hard_negatives" / "corpus.en.jsonl"
    if en.exists():
        corpora.append(en)
    cases: list[Case] = []
    for corpus in corpora:
      for line in corpus.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        c = json.loads(line)
        expect = c.get("expect", "no_rule_call")
        if expect == "escalate":
            cases.append(Case(
                id=c["id"], suite="hard_negatives", kind="routing", input=c["input"],
                expect_exit="cloud", note=c.get("note", ""),
            ))
        elif expect == "no_candidates":
            cases.append(Case(
                id=c["id"], suite="hard_negatives", kind="routing", input=c["input"],
                expect_exit="not_rule_call", expect_empty_candidates=True,
                note=c.get("note", ""),
            ))
        else:
            cases.append(Case(
                id=c["id"], suite="hard_negatives", kind="routing", input=c["input"],
                expect_exit="not_rule_call", note=c.get("note", ""),
            ))
    return cases


def _collect_multi_cases() -> list[Case]:
    corpora = [TESTS / "multi_intent" / "multi_action_corpus.jsonl"]
    en = TESTS / "multi_intent" / "corpus.en.jsonl"
    if en.exists():
        corpora.append(en)
    detect_cases: list[Case] = []
    route_cases: list[Case] = []
    for corpus in corpora:
      for line in corpus.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        c = json.loads(line)
        # English corpora use a semantic ``expect`` label instead of the
        # legacy boolean ``label`` field used by Chinese multi-action data.
        label = c.get("label")
        if label is None:
            label = c.get("expect") in {"multi_intent", "multi", "true", True}
        detect_cases.append(Case(
            id=c["id"], suite="multi_detect", kind="multi", input=c["input"],
            expect_multi=bool(label), note=c.get("note", ""),
        ))
        if label:
            route_cases.append(Case(
                id=c["id"], suite="multi_route", kind="routing", input=c["input"],
                expect_exit="cloud", note="多动作复合指令必须升级云端",
            ))
    return detect_cases + route_cases


def _collect_question_cases() -> list[Case]:
    mod = _load_module("_reg_question_cases", TESTS / "test_question_detector.py")
    return [
        Case(
            id=f"q{idx:03d}", suite="question", kind="question", input=text,
            expect_question=exp_q, expect_layer=exp_layer,
            note=f"L{exp_layer} 问句判别",
        )
        for idx, (text, exp_q, exp_layer) in enumerate(mod.CASES)
    ]


def collect_cases() -> list[Case]:
    return (
        _collect_intent_cases()
        + _collect_hard_negative_cases()
        + _collect_multi_cases()
        + _collect_question_cases()
    )


def _collect_online_cases() -> list[OnlineCase]:
    cases: list[OnlineCase] = []
    for suite, name in (("system_control", "system_control_online"),
                        ("ubuntu_cmd", "ubuntu_cmd_online")):
        corpus = TESTS / suite / "corpus.jsonl"
        for line in corpus.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            c = json.loads(line)
            cases.append(OnlineCase(
                id=c["id"], suite=name, group=c.get("group", "model"),
                input=c["input"], expect=c["expect"],
            ))
    return cases


def _predict_routing(text: str, rule: RuleEngine, registry: ToolRegistry) -> Outcome:
    plan = decide_local_intent(text, rule, registry, embed=None)
    return Outcome(
        ok=True,
        pred_exit=_ACTION_TO_EXIT[plan.action],
        pred_tool=plan.tool_name,
        pred_reason=plan.reason,
        pred_candidates=list(plan.candidates),
    )


def _predict_multi(text: str) -> Outcome:
    r = detect_multi_action(text)
    return Outcome(ok=True, pred_multi=r.is_multi)


def _predict_question(text: str, det: QuestionDetector) -> Outcome:
    v = det.detect(text)
    return Outcome(ok=True, pred_question=v.is_question, pred_layer=v.layer)


def run_case(case: Case, rule: RuleEngine, registry: ToolRegistry,
             qdet: QuestionDetector) -> Outcome:
    if case.kind == "routing":
        out = _predict_routing(case.input, rule, registry)
        _check_routing(case, out)
        return out
    if case.kind == "multi":
        out = _predict_multi(case.input)
        out.ok = out.pred_multi == case.expect_multi
        out.detail = f"is_multi={out.pred_multi} 期望={case.expect_multi}"
        return out
    out = _predict_question(case.input, qdet)
    ok_q = out.pred_question == case.expect_question
    ok_l = case.expect_layer is None or out.pred_layer == case.expect_layer
    out.ok = bool(ok_q and ok_l)
    out.detail = (f"is_question={out.pred_question} layer={out.pred_layer} "
                  f"期望={case.expect_question}/{case.expect_layer}")
    return out


def _check_routing(case: Case, out: Outcome) -> None:
    if case.forbid_reason is not None:
        bad = out.pred_exit == "cloud" and out.pred_reason == case.forbid_reason
        if bad:
            out.ok = False
            out.detail = f"误命中禁止原因 {case.forbid_reason}"
            return
    exp = case.expect_exit
    if exp == "any":
        out.ok = True
        out.detail = f"出口={out.pred_exit}"
        return
    if exp == "not_rule_call":
        out.ok = out.pred_exit != "rule_call"
        out.detail = (f"出口={out.pred_exit}" if out.ok
                      else f"误直通 rule_call tool={out.pred_tool}")
        if out.ok and case.expect_empty_candidates and out.pred_exit == "to_model":
            if out.pred_candidates:
                out.ok = False
                out.detail = f"候选非空 {out.pred_candidates}"
        return
    # 单一出口
    out.ok = out.pred_exit == exp
    out.detail = f"出口={out.pred_exit}"
    if out.ok and case.expect_tool is not None and out.pred_tool != case.expect_tool:
        out.ok = False
        out.detail = f"工具 {out.pred_tool} != {case.expect_tool}"
    if out.ok and case.expect_reason is not None and out.pred_reason != case.expect_reason:
        out.ok = False
        out.detail = f"原因 {out.pred_reason} != {case.expect_reason}"
    if out.ok and case.expect_empty_candidates and exp == "to_model":
        if out.pred_candidates:
            out.ok = False
            out.detail = f"候选非空 {out.pred_candidates}"


def _macro_f1(confusion: dict[str, dict[str, int]]) -> float:
    labels = EXIT_LABELS
    f1s = []
    for label in labels:
        tp = confusion[label][label]
        fp = sum(confusion[g][label] for g in labels) - tp
        fn = sum(confusion[label][p] for p in labels) - tp
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1s.append(2 * prec * rec / (prec + rec) if prec + rec else 0.0)
    return sum(f1s) / len(f1s)


def evaluate(cases: list[Case]) -> dict:
    registry = build_registry()
    rule = RuleEngine(registry)
    qdet = QuestionDetector()

    results: list[tuple[Case, Outcome]] = []
    for case in cases:
        results.append((case, run_case(case, rule, registry, qdet)))

    suites: dict[str, dict] = {}
    for case, out in results:
        s = suites.setdefault(case.suite, {
            "name": case.suite, "total": 0, "passed": 0, "failed": 0,
        })
        s["total"] += 1
        s["passed"] += int(out.ok)
        s["failed"] += int(not out.ok)

    confusion: dict[str, dict[str, int]] = {
        g: {p: 0 for p in EXIT_LABELS} for g in EXIT_LABELS
    }
    oos_total = oos_correct = 0
    no_rule_total = mis_exec = 0
    exit_counts = {e: 0 for e in EXIT_LABELS}
    for case, out in results:
        if case.kind != "routing":
            continue
        if out.pred_exit in EXIT_LABELS:
            exit_counts[out.pred_exit] += 1
        if case.expect_exit in EXIT_LABELS:
            confusion[case.expect_exit][out.pred_exit] += 1
        if case.expect_exit == "cloud":
            oos_total += 1
            oos_correct += int(out.pred_exit == "cloud")
        if case.expect_exit == "not_rule_call":
            no_rule_total += 1
            mis_exec += int(out.pred_exit == "rule_call")

    failures = [
        {
            "id": case.id, "suite": case.suite, "input": case.input,
            "expect_exit": case.expect_exit, "expect_tool": case.expect_tool,
            "expect_reason": case.expect_reason, "pred_exit": out.pred_exit,
            "pred_tool": out.pred_tool, "pred_reason": out.pred_reason,
            "detail": out.detail, "note": case.note,
        }
        for case, out in results
        if not out.ok
    ]

    total = len(results)
    passed = sum(1 for _, out in results if out.ok)
    total_routing = sum(1 for c, _ in results if c.kind == "routing")

    return {
        "meta": {
            "date": time.strftime("%Y-%m-%d"),
            "methodology": "docs/evaluation/intent_evaluation_methodology.md",
            "mode": "offline",
            "exit_labels": list(EXIT_LABELS),
        },
        "summary": {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "accuracy": passed / total if total else 0.0,
            "macro_f1": _macro_f1(confusion),
            "oos_recall": oos_correct / oos_total if oos_total else None,
            "mis_execution_rate": mis_exec / no_rule_total if no_rule_total else None,
            "exit_rates": {
                e: exit_counts[e] / total_routing if total_routing else 0.0
                for e in EXIT_LABELS
            },
        },
        "suites": [
            {
                **{k: v for k, v in s.items() if k != "name"},
                "name": s["name"],
                "accuracy": s["passed"] / s["total"] if s["total"] else 0.0,
                "threshold": 1.0,
                "pass": s["failed"] == 0,
            }
            for s in suites.values()
        ],
        "confusion_matrix": {
            "labels": list(EXIT_LABELS),
            "matrix": [[confusion[g][p] for p in EXIT_LABELS] for g in EXIT_LABELS],
        },
        "failures": failures,
    }


def _resolve_online(client, text: str, tool_list: str, reg: ToolRegistry, plan):
    """与 router 一致：模型 CALL 失败时按 agreed 强提示 / 唯一候选兜底。"""
    r = client.infer(text, tool_list)
    if not r.is_call:
        return None, r.latency_ms
    if not plan.hint and not plan.candidates:
        return None, r.latency_ms
    fallbacks = (
        [plan.hint] if plan.hint
        else plan.candidates if len(plan.candidates) == 1
        else []
    )
    return reg.resolve_with_fallback(r.tool_name, fallbacks), r.latency_ms


def _pct(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, int(len(sorted_vals) * p))
    return sorted_vals[idx]


def evaluate_online(cases: list[OnlineCase], client, embed, n: int) -> dict:
    reg = build_registry()
    rule = RuleEngine(reg)

    suites: dict[str, dict] = {}
    all_failures: list[dict] = []
    total = passed = 0
    latencies: list[float] = []

    for c in cases:
        inp, expect = c.input, c.expect
        m = rule.match(inp)

        if m and m.confidence >= RuleEngine.CONFIDENCE_THRESHOLD:
            stage, got = "rule", m.tool_name
        else:
            plan = decide_local_intent(inp, rule, reg, embed=embed)
            if plan.action is IntentAction.ESCALATE:
                stage, got = "escalate", None
            else:
                stage = "model"
                votes: list[str | None] = []
                for _ in range(max(1, n)):
                    name, ms = _resolve_online(client, inp, plan.tool_list, reg, plan)
                    latencies.append(ms)
                    if name is None:
                        if plan.hint and plan.hint_source == "agreed":
                            name = plan.hint
                        elif len(plan.candidates) == 1:
                            name = plan.candidates[0]
                    votes.append(name if name else None)
                got = max(set(votes), key=votes.count) if votes else None

        ok = (got is None) if expect == "ESCALATE" else (got == expect)
        total += 1
        passed += int(ok)

        s = suites.setdefault(c.suite, {
            "name": c.suite, "total": 0, "passed": 0, "failed": 0,
            "rule": [0, 0], "model": [0, 0], "escalate": [0, 0],
            "hint_hit": 0, "hint_total": 0,
        })
        s["total"] += 1
        s["passed"] += int(ok)
        s["failed"] += int(not ok)
        s[stage][1] += 1
        s[stage][0] += int(ok)
        if stage == "model" and plan.hint:
            s["hint_total"] += 1
            s["hint_hit"] += int(plan.hint == expect)

        if not ok:
            all_failures.append({
                "id": c.id, "suite": c.suite, "input": inp, "expect": expect,
                "got": got, "stage": stage,
            })

    latencies.sort()
    suite_rows = []
    for s in suites.values():
        suite_rows.append({
            "name": s["name"],
            "total": s["total"],
            "passed": s["passed"],
            "failed": s["failed"],
            "accuracy": s["passed"] / s["total"] if s["total"] else 0.0,
            "rule_acc": s["rule"][0] / s["rule"][1] if s["rule"][1] else None,
            "model_acc": s["model"][0] / s["model"][1] if s["model"][1] else None,
            "escalate_acc": s["escalate"][0] / s["escalate"][1] if s["escalate"][1] else None,
            "hint_hit_rate": s["hint_hit"] / s["hint_total"] if s["hint_total"] else None,
            "threshold": 1.0,
            "pass": s["failed"] == 0,
        })

    return {
        "mode": "online",
        "summary": {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "accuracy": passed / total if total else 0.0,
            "latency_p50_ms": _pct(latencies, 0.5),
            "latency_p90_ms": _pct(latencies, 0.9),
            "latency_mean_ms": sum(latencies) / len(latencies) if latencies else None,
        },
        "suites": suite_rows,
        "failures": all_failures,
    }


def print_report(report: dict) -> None:
    s = report["summary"]
    print("=" * 72)
    print("意图识别统一离线回归")
    print(f"  口径 : {report['meta']['mode']}  {report['meta']['methodology']}")
    print(f"  汇总 : {s['passed']}/{s['total']} = {s['accuracy']:.1%}  "
          f"macro-F1={s['macro_f1']:.3f}")
    if s["oos_recall"] is not None:
        print(f"  OOS 召回(应上云) : {s['oos_recall']:.1%}")
    if s["mis_execution_rate"] is not None:
        print(f"  误执行率(硬负样本) : {s['mis_execution_rate']:.1%}")
    print("  出口分布 : " + "  ".join(
        f"{k}={v:.1%}" for k, v in s["exit_rates"].items()))
    print("\n分语料：")
    for suite in report["suites"]:
        flag = "PASS" if suite["pass"] else "FAIL"
        print(f"  [{flag}] {suite['name']:16s} {suite['passed']}/{suite['total']}"
              f" = {suite['accuracy']:.1%}  (门槛 {suite['threshold']:.0%})")

    print("\n出口混淆矩阵(行=期望, 列=预测)：")
    labels = report["confusion_matrix"]["labels"]
    print("         " + "".join(f"{l:>11s}" for l in labels))
    for g, row in zip(labels, report["confusion_matrix"]["matrix"]):
        print(f"  {g:7s} " + "".join(f"{v:>11d}" for v in row))

    if report["failures"]:
        print(f"\n失败明细({len(report['failures'])}):")
        for f in report["failures"]:
            print(f"  {f['id']} [{f['suite']}] 「{f['input']}」 "
                  f"期望={f['expect_exit'] or f['expect_reason'] or ''} "
                  f"实际={f['pred_exit']}/{f['pred_reason']} :: {f['detail']}")
    else:
        print("\n失败明细：无")

    online = report.get("online")
    if online:
        _print_online(online)
    print("=" * 72)


def _print_online(online: dict) -> None:
    s = online["summary"]
    print("\n在线正例 probe（device 端，进模型）")
    print(f"  汇总 : {s['passed']}/{s['total']} = {s['accuracy']:.1%}  "
          f"延迟 p50={s['latency_p50_ms']}ms p90={s['latency_p90_ms']}ms")
    for suite in online["suites"]:
        flag = "PASS" if suite["pass"] else "FAIL"
        print(f"  [{flag}] {suite['name']:20s} {suite['passed']}/{suite['total']}"
              f" = {suite['accuracy']:.1%}")
        print(f"        rule={_fmt_acc(suite['rule_acc'])} "
              f"model={_fmt_acc(suite['model_acc'])} "
              f"escalate={_fmt_acc(suite['escalate_acc'])} "
              f"hint={_fmt_acc(suite['hint_hit_rate'])}")
    if online["failures"]:
        print(f"\n在线失败明细({len(online['failures'])}):")
        for f in online["failures"]:
            print(f"  {f['id']} [{f['suite']}] 「{f['input']}」 "
                  f"期望={f['expect']} 实际={f['got']} [{f['stage']}]")


def _fmt_acc(v) -> str:
    return f"{v:.1%}" if isinstance(v, float) else "—"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=str(TESTS / "results" / "intent_regression.json"),
                    help="统一 JSON 输出路径（默认 tests/results/intent_regression.json）")
    ap.add_argument("--online", action="store_true",
                    help="加跑 device 端在线正例 probe（system_control/ubuntu_cmd）")
    ap.add_argument("--no-embed", dest="embed", action="store_false",
                    help="关闭 embedding 语义召回（--online 对照实验）")
    ap.set_defaults(embed=True)
    ap.add_argument("--n", type=int, default=1, help="在线模型用例重复次数（取多数票）")
    args = ap.parse_args()

    cases = collect_cases()
    report = evaluate(cases)

    if args.online:
        from bibit_intent.models.local_model import LocalModelClient
        from bibit_intent.routing.embedding import (
            EmbeddingClient,
            EmbeddingRetriever,
            default_index_paths,
        )
        client = local_client(replace(PipelineSettings.from_environment(), local_timeout_seconds=60))
        embed = None
        if args.embed:
            embed = EmbeddingRetriever(
                embedding_client(replace(PipelineSettings.from_environment(), local_timeout_seconds=60)),
                default_index_paths(),
            )
        report["online"] = evaluate_online(
            _collect_online_cases(), client, embed, args.n,
        )

    print_report(report)

    out = Path(args.json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(f"\nJSON 已写入：{out}")

    failed = report["summary"]["failed"]
    if report.get("online"):
        failed += report["online"]["summary"]["failed"]
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
