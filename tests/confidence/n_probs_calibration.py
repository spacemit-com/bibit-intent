"""tests/confidence/n_probs_calibration.py — 端侧 n_probs 置信度校准探针。

目的：验证 llama-server 在业务 GBNF grammar 下能否输出 token 级 logprob，
      并观察 CALL / ESCALATE / 缺参 / 域外样本的置信度分布，为软置信门控
      提供阈值校准数据。

用法（设备端）：
  .venv/bin/python tests/confidence/n_probs_calibration.py
  .venv/bin/python tests/confidence/n_probs_calibration.py --all-regression
  .venv/bin/python tests/confidence/n_probs_calibration.py --all-regression --max-cases 30

依赖：AI Gateway 的 LLM/Embed 模型；只读实验，不执行工具。
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
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.resources import resource_path
from bibit_intent.routing.embedding import EmbeddingClient, EmbeddingRetriever, default_index_paths
from bibit_intent.routing.intent import IntentAction, decide_local_intent
from bibit_intent.routing.question_detector import QuestionDetector
from bibit_intent.routing.rule_engine import RuleEngine
from bibit_intent.models.local_model import (
    LocalModelClient,
    _GBNF_GRAMMAR,
    _SPECIAL_TOKEN_RESERVE,
    _build_system_prompt,
    _fit_prompt,
    _parse_output,
)
from regression import collect_cases


LABELED_CASES: tuple[tuple[str, str], ...] = (
    ("把音量调到60", "call"),
    ("音量调到六十", "call"),
    ("帮我设置音量", "missing_param"),
    ("帮我调一下亮度", "missing_param"),
    ("讲个笑话", "out_of_scope"),
    ("今天天气怎么样", "out_of_scope"),
    ("怎么结束卡死的进程", "out_of_scope"),
    ("格式化磁盘", "out_of_scope"),
)


def build_production_rule() -> tuple[ToolRegistry, RuleEngine, EmbeddingRetriever]:
    reg = ToolRegistry(resource_path("tools.yaml"))
    load_skills(resource_path("skills"), reg, {})
    rule = RuleEngine(reg)
    question_embed = EmbeddingRetriever(
        embedding_client(replace(PipelineSettings.from_environment(), local_timeout_seconds=60)), [resource_path("question_corpus.jsonl")]
    )
    rule.attach_question_detector(QuestionDetector(question_embed))
    embed = EmbeddingRetriever(embedding_client(replace(PipelineSettings.from_environment(), local_timeout_seconds=60)), default_index_paths())
    return reg, rule, embed


def run_case(client: LocalModelClient, user_input: str, plan) -> dict:
    """用生产候选清单跑一次真实模型推理，并返回 n_probs 校准指标。"""
    system = _build_system_prompt(plan.tool_list, plan.analysis)
    output_tokens = 30
    budget = max(0, client.context_limit - output_tokens - _SPECIAL_TOKEN_RESERVE)
    prompt = client._fit_prompt_tokens(system, user_input, budget)
    if prompt is None:
        prompt = _fit_prompt(system, user_input, budget)
    if prompt is None:
        return {"skip": "prompt_overflow", "input": user_input}

    payload = {
        "prompt": prompt,
        "grammar": _GBNF_GRAMMAR,
        "n_predict": output_tokens,
        "n_probs": 10,
        "temperature": 0.0,
        "stop": ["\n", "用户：", "User:"],
        "chat_template_kwargs": {"enable_thinking": False},
    }
    raw = client._post("/completion", payload)
    content = raw.get("content", "").strip()
    parsed = _parse_output(content, 0.0)
    probs = raw.get("completion_probabilities") or []

    first = probs[0] if probs else None
    p_call = p_esc = 0.0
    if first:
        merged: dict[str, float] = {}
        chosen = first.get("token")
        chosen_lp = first.get("logprob")
        if chosen is not None and chosen_lp is not None:
            merged[chosen] = max(merged.get(chosen, float("-inf")), chosen_lp)
        for item in first.get("top_logprobs") or []:
            tok, lp = item.get("token"), item.get("logprob")
            if tok is not None and lp is not None:
                merged[tok] = max(merged.get(tok, float("-inf")), lp)
        denom = sum(math.exp(lp) for lp in merged.values())
        if denom > 0:
            p_call = math.exp(merged.get("C", float("-inf"))) / denom if "C" in merged else 0.0
            p_esc = math.exp(merged.get("E", float("-inf"))) / denom if "E" in merged else 0.0

    avg_lp = sum(p.get("logprob") or 0.0 for p in probs) / len(probs) if probs else 0.0
    return {
        "input": user_input,
        "content": content,
        "action": parsed.action,
        "tool": parsed.tool_name,
        "reason": parsed.reason,
        "p_call": p_call,
        "p_esc": p_esc,
        "avg_logprob": avg_lp,
        "candidates": plan.candidates,
        "hint": plan.hint,
        "hint_source": plan.hint_source,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all-regression", action="store_true", help="接入 tests/regression.py 全量 routing 用例")
    parser.add_argument("--max-cases", type=int, default=1000)
    args = parser.parse_args()

    reg, rule, embed = build_production_rule()
    client = local_client(replace(PipelineSettings.from_environment(), local_timeout_seconds=60))

    if args.all_regression:
        cases = [c for c in collect_cases() if c.kind == "routing"]
        print(f"n_probs calibration: scanning {len(cases)} routing cases\n")
        print(f"{'suite/id':<18} {'input':<20} {'exp':<14} {'action':<9} {'target':<22} {'p(CALL)':>7} {'p(ESC)':>7} {'avg_lp':>9}")
        for case in cases[: args.max_cases]:
            plan = decide_local_intent(case.input, rule, reg, embed=embed)
            if plan.action is not IntentAction.TO_MODEL:
                continue
            row = run_case(client, case.input, plan)
            row["suite"] = case.suite
            row["id"] = case.id
            exp = f"{case.expect_exit}"
            if case.expect_empty_candidates:
                exp += "/empty"
            target = row["tool"] or row["reason"] or row["action"]
            print(f"{case.suite+'/'+case.id:<18} {row['input']:<20} {exp:<14} {row['action']:<9} {target:<22} {row['p_call']:>7.3f} {row['p_esc']:>7.3f} {row['avg_logprob']:>9.3f}")
    else:
        print(f"n_probs calibration: {len(LABELED_CASES)} labeled cases\n")
        print(f"{'input':<20} {'期望':<14} {'action':<9} {'target':<22} {'p(CALL)':>7} {'p(ESC)':>7} {'avg_lp':>9}")
        for user_input, expected in LABELED_CASES:
            plan = decide_local_intent(user_input, rule, reg, embed=embed)
            if plan.action is not IntentAction.TO_MODEL:
                print(f"{user_input:<20} {expected:<14} {plan.action.value:<9} {'-':<22}")
                continue
            row = run_case(client, user_input, plan)
            target = row["tool"] or row["reason"] or row["action"]
            print(f"{row['input']:<20} {expected:<14} {row['action']:<9} {target:<22} {row['p_call']:>7.3f} {row['p_esc']:>7.3f} {row['avg_logprob']:>9.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
