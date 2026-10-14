"""
tests/ubuntu_cmd/probe.py — Ubuntu 命令 Skill 路由准确率探针

对照真实三段式管线（agent.router 同款决策）：
  ① 规则引擎（高置信直接执行）
  ② 开放问答前置 → 升级
  ③ 端侧模型（候选工具由 keyword_candidates 预筛注入）

指标：
  - rule_acc    规则引擎命中且工具正确的比例（含前置升级正确）
  - model_acc   端侧模型路由正确的比例（--n 取多数票）
  - overall_acc 综合准确率（规则+模型+升级全算）

用法（在设备 /opt/bibit 下跑，已配置 AI Gateway）：
  .venv/bin/python tests/ubuntu_cmd/probe.py
  .venv/bin/python tests/ubuntu_cmd/probe.py --n 3
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]   # tests/ubuntu_cmd -> 项目根
sys.path.insert(0, str(ROOT / "src"))

from dataclasses import replace
from bibit_intent.models.backends import local_client, embedding_client
from bibit_intent.pipeline import PipelineSettings
from bibit_intent.routing.intent import IntentAction, decide_local_intent
from bibit_intent.routing.embedding import (
    EmbeddingClient,
    EmbeddingRetriever,
    default_index_paths,
)
from bibit_intent.models.local_model import LocalModelClient
from bibit_intent.routing.rule_engine import RuleEngine
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.resources import resource_path


def build_registry() -> ToolRegistry:
    """加载内置工具 + skills/ 下全部 skill（与 main.py 一致）。"""
    reg = ToolRegistry()
    load_skills(resource_path("skills"), reg, {})
    return reg


def resolve(client: LocalModelClient, user_input: str, tool_list: str,
            reg: ToolRegistry, plan):
    """
    调用端侧模型，返回 (规范化工具名 or None, 原始输出, 耗时ms)。
    与 router 一致：模型 CALL 了无法解析的工具名时，按 embedding 强提示 /
    唯一候选容错兜底（端侧 1B 常把口语里的软件名当工具名，如 "CALL vim"）。
    """
    r = client.infer(user_input, tool_list)
    if not r.is_call:
        return None, r.raw, r.latency_ms
    # 与 router 一致：无关键词候选且无 embedding 强提示时，
    # 模型 CALL 视为幻觉（视为升级），不做别名/兜底解析执行
    if not plan.hint and not plan.candidates:
        return None, r.raw, r.latency_ms
    fallbacks = (
        [plan.hint] if plan.hint
        else plan.candidates if len(plan.candidates) == 1
        else []
    )
    return reg.resolve_with_fallback(r.tool_name, fallbacks), r.raw, r.latency_ms


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=str(Path(__file__).with_name("corpus.jsonl")))
    ap.add_argument("--no-embed", dest="embed", action="store_false",
                    help="关闭 embedding 语义召回（与旧行为对照）")
    ap.set_defaults(embed=True)
    ap.add_argument("--n", type=int, default=1, help="模型用例重复次数（取多数票）")
    args = ap.parse_args()

    reg = build_registry()
    rule = RuleEngine(reg)
    client = local_client(replace(PipelineSettings.from_environment(), local_timeout_seconds=60))
    embed = None
    if args.embed:
        # 索引源固定用 DEFAULT_INDEXES（与 main.py 一致）；--corpus 只决定
        # 跑哪些用例，不影响索引，否则探针测的索引和线上不是一份。
        embed = EmbeddingRetriever(
            embedding_client(replace(PipelineSettings.from_environment(), local_timeout_seconds=60)),
            default_index_paths(),
        )

    cases = [json.loads(l) for l in
             open(args.corpus, encoding="utf-8") if l.strip()]
    print(f"语料 {len(cases)} 条，llama-server: configured AI Gateway，"
          f"embedding: {'开' if embed else '关'}，模型重复 --n {args.n}\n")

    # 统计: {stage: [correct, total]}
    stat = {"rule": [0, 0], "model": [0, 0], "escalate": [0, 0]}
    fails: list[str] = []
    model_latency: list[float] = []
    rule_expected_model = 0   # 标注 model 却被规则抢答（影响"端侧模型使用率"）
    hint_hit = hint_total = 0  # embedding 强提示命中统计

    for c in cases:
        inp, expect = c["input"], c["expect"]
        m = rule.match(inp)

        # ── ① 规则引擎 ────────────────────────────────────────────
        if m and m.confidence >= RuleEngine.CONFIDENCE_THRESHOLD:
            stage, got = "rule", m.tool_name
            if c["group"] == "model":
                rule_expected_model += 1
        else:
            # ── ② 开放问答前置 / ③ 端侧模型 ─────────────────────
            plan = decide_local_intent(inp, rule, reg, embed=embed)
            if plan.action is IntentAction.ESCALATE:
                stage, got = "escalate", None
            else:
                stage = "model"
                if plan.hint:
                    hint_total += 1
                    if plan.hint == expect:
                        hint_hit += 1
                votes: list[str | None] = []
                for _ in range(max(1, args.n)):
                    name, raw, ms = resolve(
                        client, inp, plan.tool_list, reg, plan
                    )
                    model_latency.append(ms)
                    # 与 router 一致：小模型误判升级时，
                    # agreed 强提示 / 唯一候选 → 确定性通道兜底
                    if name is None:
                        if plan.hint and plan.hint_source == "agreed":
                            name = plan.hint
                        elif len(plan.candidates) == 1:
                            name = plan.candidates[0]
                    # 端侧输出 short/近似名 → 模糊解析规范化；无法解析视同升级
                    votes.append(name if name else None)
                got = max(set(votes), key=votes.count) if votes else None

        # ── 判定 ──────────────────────────────────────────────────
        if expect == "ESCALATE":
            ok = got is None
        else:
            ok = got == expect
        stat[stage][1] += 1
        stat[stage][0] += ok
        mark = "OK " if ok else "XX "
        arrow = got or ("ESCALATE" if stage == "escalate" else "(升级)")
        if not ok:
            fails.append(f"  {mark}{inp!r} -> {arrow}  期望 {expect}  [{stage}]")
            print(f"  {mark}[{stage}] {inp!r} -> {arrow}  期望 {expect}")
        else:
            print(f"  {mark}[{stage}] {inp!r} -> {arrow}")

    # ── 汇总 ──────────────────────────────────────────────────────
    print("\n" + "=" * 56)
    n_all = sum(v[1] for v in stat.values())
    ok_all = sum(v[0] for v in stat.values())
    for st in ("rule", "model", "escalate"):
        ok, total = stat[st]
        acc = ok / total * 100 if total else 0
        print(f"  {st:<8} 准确率 {acc:5.1f}%   ({ok}/{total})")
    print(f"  {'overall':<8} 准确率 {ok_all / n_all * 100:5.1f}%   ({ok_all}/{n_all})")
    if model_latency:
        print(f"  模型平均耗时 {sum(model_latency) / len(model_latency):.0f}ms")
    print(f"  规则抢答了 {rule_expected_model} 条标注为 model 的用例")
    if hint_total:
        print(f"  embedding 强提示命中 {hint_hit}/{hint_total} 条模型用例")
    if fails:
        print("\n失败明细：")
        for f in fails:
            print(f)
    return 0 if ok_all == n_all else 1


if __name__ == "__main__":
    sys.exit(main())
