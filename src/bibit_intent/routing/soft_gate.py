"""soft_gate.py — 软置信门控（SoftGate）。

基于端侧实验 `docs/intent/soft_confidence_gate_plan.md`：
  - PreModelGate 只使用 embedding 候选信号，不调用模型。
  - PostModelGate 使用模型 `n_probs` 的平均 logprob，拒绝低置信 CALL。

默认配置为 shadow，只记录、不改变现有路由；切到 on 后才会真正升级。
"""

from __future__ import annotations

from dataclasses import dataclass

from bibit_intent.routing.intent import IntentAction, IntentPlan


@dataclass(frozen=True)
class SoftGateConfig:
    mode: str = "shadow"          # off / shadow / on
    post_mode: str = "shadow"     # off / shadow / on，控制后置 logprob 门控
    logprob_threshold: float = -2.2
    embed_top1_threshold: float = 0.72
    embed_margin_threshold: float = 0.05
    n_probs: int = 10


def pre_model_escalate(plan: IntentPlan, config: SoftGateConfig) -> bool:
    """模型调用前是否应直接升级。只依据 embedding / 候选信号。"""
    if plan.action is not IntentAction.TO_MODEL:
        return False

    if plan.hint_source == "agreed":
        return False

    route = plan.embed_route
    if plan.hint_source == "embed" and route is not None:
        return not (
            route.top1_score >= config.embed_top1_threshold
            and route.margin >= config.embed_margin_threshold
        )

    if plan.candidates:
        return False

    return True


def post_model_escalate(
    model_result,
    config: SoftGateConfig,
) -> bool:
    """模型已输出后，是否应因低 logprob 升级。"""
    return model_result.avg_logprob < config.logprob_threshold
