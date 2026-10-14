"""
tests/perf/bench_pipeline.py — 链路性能基准
规则引擎 → 混合语义路由(embedding) → 端侧小钢炮(1B)模型

在设备上实测（AI Gateway）：
  ① 规则引擎      rule.match / decide_local_intent（规则命中路径）
  ② 混合语义路由  embed.route()（/embedding 调用 + 余弦分组 + BM25 + 融合），
                  并拆出 embed 调用耗时与本地计算耗时
  ③ 端侧模型      local.infer()（TO_MODEL 用例）
  模型全链路      decide_local_intent + infer
  端到端 Router   Router.handle() 跑一组安全只读查询（含工具执行）

用法（设备 /opt/bibit 下）：
  .venv/bin/python tests/perf/bench_pipeline.py
  .venv/bin/python tests/perf/bench_pipeline.py --cold   # 含索引冷启动构建
  .venv/bin/python tests/perf/bench_pipeline.py --n 3    # 模型每条重复 3 次
  .venv/bin/python tests/perf/bench_pipeline.py --no-e2e # 跳过端到端 Router

数据与解读见 docs/design/performance.md。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from dataclasses import replace
from bibit_intent.models.backends import local_client, embedding_client
from bibit_intent.pipeline import PipelineSettings
from bibit_intent.routing.embedding import EmbeddingClient, EmbeddingRetriever
from bibit_intent.routing.intent import IntentAction, decide_local_intent
from bibit_intent.models.local_model import LocalModelClient
from bibit_intent.routing.rule_engine import RuleEngine
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.resources import resource_path


def build_registry() -> ToolRegistry:
    reg = ToolRegistry()
    load_skills(resource_path("skills"), reg, {})
    return reg


def pct(vals: list[float], p: float) -> float:
    vals = sorted(vals)
    if not vals:
        return 0.0
    k = max(0, min(len(vals) - 1, int(round(p / 100 * (len(vals) - 1)))))
    return vals[k]


def fmt(name: str, ms: list[float]) -> str:
    if not ms:
        return f"{name:<22} (无样本)"
    return (f"{name:<22} n={len(ms):<3} 均值={statistics.mean(ms):7.1f}ms "
            f"中位={pct(ms, 50):7.1f}ms p95={pct(ms, 95):7.1f}ms "
            f"max={max(ms):7.1f}ms")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=str(ROOT / "tests" / "ubuntu_cmd" / "corpus.jsonl"))
    ap.add_argument("--n", type=int, default=1, help="模型用例重复次数")
    ap.add_argument("--cold", action="store_true", help="先删缓存，测索引冷启动构建")
    ap.add_argument("--no-e2e", dest="e2e", action="store_false",
                    help="跳过端到端 Router 实测")
    args = ap.parse_args()

    reg = build_registry()
    rule = RuleEngine(reg)
    client = local_client(replace(PipelineSettings.from_environment(), local_timeout_seconds=60))
    embed = EmbeddingRetriever(
        embedding_client(replace(PipelineSettings.from_environment(), local_timeout_seconds=60)), Path(args.corpus)
    )

    cases = [json.loads(l) for l in
             open(args.corpus, encoding="utf-8") if l.strip()]
    rule_cases = [c for c in cases if c["group"] == "rule"]
    model_cases = [c for c in cases if c["group"] == "model"]

    print("=" * 62)
    print("链路性能基准：规则 → 混合语义路由 → 端侧模型")
    print(f"语料 {len(cases)} 条（rule {len(rule_cases)} / model {len(model_cases)}）")
    print(f"llama-server configured AI Gateway / llama-embed configured AI Gateway / --n {args.n}")
    print("=" * 62)

    # ── 冷启动：embedding 索引构建 ────────────────────────────────
    if args.cold:
        cache = Path(args.corpus).with_name(
            Path(args.corpus).name + ".embed_cache.json"
        )
        if cache.exists():
            cache.unlink()
            print(f"[冷启动] 已删除缓存 {cache.name}")
    t0 = time.perf_counter()
    _ = embed.route("构建预热")
    build_ms = (time.perf_counter() - t0) * 1000
    if args.cold:
        print(f"[冷启动] 索引构建（含预热 embed）: {build_ms:.0f}ms")
    else:
        print(f"[预热]   首次 route（缓存加载）: {build_ms:.0f}ms")

    # ── ① 规则引擎 ───────────────────────────────────────────────
    t_match, t_decide_rule = [], []
    for c in rule_cases:
        t0 = time.perf_counter(); rule.match(c["input"]); t_match.append((time.perf_counter()-t0)*1000)
        t0 = time.perf_counter(); decide_local_intent(c["input"], rule, reg); t_decide_rule.append((time.perf_counter()-t0)*1000)
    print(fmt("[① 规则] rule.match", t_match))
    print(fmt("[① 规则] 决策(命中即返)", t_decide_rule))

    # ── ② 混合语义路由 ───────────────────────────────────────────
    t_route, t_embed_call, t_embed_local = [], [], []
    emb_client = embedding_client(replace(PipelineSettings.from_environment(), local_timeout_seconds=60))
    for c in model_cases:
        q = c["input"]
        t0 = time.perf_counter(); embed.route(q); total = (time.perf_counter()-t0)*1000
        t0 = time.perf_counter(); emb_client.embed(q); emb = (time.perf_counter()-t0)*1000
        t_route.append(total); t_embed_call.append(emb); t_embed_local.append(total-emb)
    print(fmt("[② 语义路由] route() 总耗时", t_route))
    print(fmt("[② 语义路由] 其中 /embedding", t_embed_call))
    print(fmt("[② 语义路由] 其中本地计算(BM25+融合)", t_embed_local))

    # ── ③ 端侧模型 + 模型全链路 ──────────────────────────────────
    t_infer, t_decide_model, t_chain = [], [], []
    stage_cnt = {"rule": 0, "escalate": 0, "model": 0}
    for c in model_cases:
        q = c["input"]
        t0 = time.perf_counter()
        plan = decide_local_intent(q, rule, reg, embed=embed)
        dt_decide = (time.perf_counter()-t0)*1000
        t_decide_model.append(dt_decide)
        if plan.action is IntentAction.RULE_CALL:
            stage_cnt["rule"] += 1; continue
        if plan.action is IntentAction.ESCALATE:
            stage_cnt["escalate"] += 1; continue
        stage_cnt["model"] += 1
        for _ in range(args.n):
            t0 = time.perf_counter()
            r = client.infer(q, plan.tool_list)
            dt_infer = (time.perf_counter()-t0)*1000
            t_infer.append(dt_infer)
            t_chain.append(dt_decide + dt_infer)
    print(fmt("[③ 端侧模型] infer()", t_infer))
    print(fmt("[③ 全链路] decide+infer", t_chain))
    print(f"    模型用例去向: 规则抢答 {stage_cnt['rule']} / 前置升级 {stage_cnt['escalate']} / 进模型 {stage_cnt['model']}")

    # ── 端到端 Router（安全只读查询，含工具执行）────────────────────
    if args.e2e:
        from bibit_intent.config import load_config
        from bibit_intent.cli import build_router
        router = build_router(load_config())
        e2e = [
            ("查看当前进程", "rule"),
            ("内存还剩多少", "rule"),
            ("当前ip地址是多少", "rule"),
            ("看看ssh有没有在跑", "model"),
            ("var这个目录很占地方", "model"),
            ("机器上挂了几块盘", "model"),
            ("查一下系统版本", "rule"),
        ]
        print("\n[端到端 Router.handle]（含工具执行，安全只读）")
        for q, want in e2e:
            t0 = time.perf_counter()
            r = router.handle(q)
            dt = (time.perf_counter()-t0)*1000
            mark = "✓" if r.stage.value == want else "?"
            print(f"  {mark} {q!r:<16} -> {r.stage.value:6} {dt:8.1f}ms  "
                  f"(tool={r.tool_name}) {('msg='+(r.exec_result.message or '')[:28]) if r.exec_result else ''}")
    print("\n完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
