"""
agent/hallucination.py — 幻觉检测的统一结果模型与统计（P0）

对照 appcli 指南 7.4（HallucinationDetectionEngine）：当前检测逻辑分散在
router（工具名/参数证据闸门）、executor（范围/枚举校验）、registry（模糊
解析）三处，这里收敛为统一的记录类型 + 聚合统计，用于量化幻觉率。

设计口径：
  - DetectionKind   检测层级：工具名(L1) / 参数(L2) / 事实(L3，预留) / 执行(L4，预留)
  - Disposition     处置方式：放行 / 挂起确认 / 升级 / 容错兜底 / 钳制 / 拒绝 / 丢弃
  - HallucinationStats 聚合：请求数、模型 CALL 数、各类型/处置计数、按工具分布，
    并提供 summary() 输出可读的量化报告。

本模块只做「记录与统计」，不改变任何路由/执行决策逻辑（零行为影响）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DetectionKind(str, Enum):
    """检测层级（对应指南 7.4 的分层检测）。"""
    TOOL_NAME = "tool_name"   # L1 工具名幻觉
    PARAMETER = "parameter"   # L2 参数幻觉（未定义键/越界/枚举/猜值）
    FACT = "fact"             # L3 事实幻觉（预留：API 端点/权限真实性）
    EXECUTION = "execution"   # L4 执行后验证（预留：目标状态核对）


class Disposition(str, Enum):
    """处置方式。"""
    EXECUTED = "executed"     # 有原文证据，直接执行（检测通过，安全）
    CONFIRMED = "confirmed"   # 推测值挂起确认（不盲执行，见 _suspend_confirm）
    ESCALATED = "escalated"   # 升级云端（无候选 CALL / 未知工具 / 无支撑）
    FALLBACK = "fallback"     # 工具名容错兜底命中（模糊/embedding 兜底）
    CLAMPED = "clamped"       # 数值越界钳制到范围端点
    REJECTED = "rejected"     # 枚举/类型校验拒绝
    DROPPED = "dropped"       # 未知参数键丢弃


@dataclass
class HallucinationRecord:
    """单条检测事件。"""
    kind: DetectionKind
    disposition: Disposition
    tool: str | None
    input: str = ""
    param: str | None = None
    value: Any = None
    detail: str = ""


# 视为「疑似幻觉」的处置（用于统计幻觉率分子）
_GUARD_DISPOSITIONS = {
    Disposition.CONFIRMED,
    Disposition.ESCALATED,
    Disposition.CLAMPED,
    Disposition.REJECTED,
    Disposition.DROPPED,
}


@dataclass
class HallucinationStats:
    """聚合统计器。Router/Executor 埋点写入，探针/上层读取。"""

    _records: list[HallucinationRecord] = field(default_factory=list)
    total_requests: int = 0   # 进入 Router.handle 的请求数（分母）
    model_requests: int = 0   # 端侧模型推理次数
    model_calls: int = 0      # 模型输出 CALL 次数（幻觉检测的主要作用面）
    soft_gate_pre_shadow: int = 0    # shadow 模式：前置门控会升级的请求数
    soft_gate_post_shadow: int = 0   # shadow 模式：后置门控会升级的 CALL 数
    soft_gate_pre_on: int = 0        # on 模式：前置门控实际升级的请求数
    soft_gate_post_on: int = 0       # on 模式：后置门控实际升级的 CALL 数

    # ── 写入 ─────────────────────────────────────────────────────────

    def record(self, rec: HallucinationRecord) -> None:
        self._records.append(rec)

    def clear(self) -> None:
        self._records.clear()
        self.total_requests = 0
        self.model_requests = 0
        self.model_calls = 0
        self.soft_gate_pre_shadow = 0
        self.soft_gate_post_shadow = 0
        self.soft_gate_pre_on = 0
        self.soft_gate_post_on = 0

    # ── 只读聚合 ─────────────────────────────────────────────────────

    @property
    def records(self) -> list[HallucinationRecord]:
        return list(self._records)

    def counts(self) -> dict:
        """按 (kind, disposition) 计数的平铺 dict，便于程序化读取。"""
        c: dict[str, int] = {}
        for r in self._records:
            key = f"{r.kind.value}:{r.disposition.value}"
            c[key] = c.get(key, 0) + 1
        return c

    def by_kind(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for r in self._records:
            c[r.kind.value] = c.get(r.kind.value, 0) + 1
        return c

    def by_disposition(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for r in self._records:
            c[r.disposition.value] = c.get(r.disposition.value, 0) + 1
        return c

    def by_tool(self, kinds: set[str] | None = None) -> dict[str, int]:
        """疑似幻觉事件按工具分布（用于找出最容易幻觉的工具）。"""
        c: dict[str, int] = {}
        for r in self._records:
            if r.disposition not in _GUARD_DISPOSITIONS:
                continue
            if kinds and r.kind.value not in kinds:
                continue
            t = r.tool or "(none)"
            c[t] = c.get(t, 0) + 1
        return c

    # ── 量化指标 ─────────────────────────────────────────────────────

    @property
    def hallucinated_events(self) -> int:
        """疑似幻觉事件总数（闸门拦截 + 钳制/拒绝/丢弃）。"""
        return sum(
            1 for r in self._records if r.disposition in _GUARD_DISPOSITIONS
        )

    @property
    def guard_blocked(self) -> int:
        """被闸门阻止盲执行的事件（升级 + 挂起确认）。"""
        return sum(
            1 for r in self._records
            if r.disposition in (Disposition.ESCALATED, Disposition.CONFIRMED)
        )

    @property
    def executed_events(self) -> int:
        """有证据放行执行的事件（检测通过）。"""
        return sum(
            1 for r in self._records if r.disposition is Disposition.EXECUTED
        )

    @property
    def soft_gate_shadow_total(self) -> int:
        """shadow 模式下会被 SoftGate 拦截的总数。"""
        return self.soft_gate_pre_shadow + self.soft_gate_post_shadow

    def summary(self) -> str:
        """输出可读的量化报告。"""
        lines: list[str] = []
        n_req = self.total_requests or 1
        n_mreq = self.model_requests or 1
        n_call = self.model_calls or 1
        n_halluc = self.hallucinated_events
        n_block = self.guard_blocked

        lines.append("== 幻觉检测统计 ==")
        lines.append(f"请求总数        {self.total_requests}")
        lines.append(f"端侧推理次数    {self.model_requests}")
        lines.append(f"模型 CALL 数     {self.model_calls}")
        lines.append("")
        lines.append(f"疑似幻觉事件    {n_halluc}"
                     f"  （占请求 {n_halluc*100.0/n_req:.1f}%"
                     f"，占端侧推理 {n_halluc*100.0/n_mreq:.1f}%）")
        lines.append(f"闸门阻止盲执行  {n_block}"
                     f"  （升级 {self.by_disposition().get('escalated', 0)}"
                     f" + 挂起确认 {self.by_disposition().get('confirmed', 0)}）")
        lines.append(f"有证据放行执行  {self.executed_events}")
        lines.append("")
        lines.append("按类型：" + ", ".join(
            f"{k}={v}" for k, v in sorted(self.by_kind().items())))
        lines.append("按处置：" + ", ".join(
            f"{k}={v}" for k, v in sorted(self.by_disposition().items())))
        lines.append("SoftGate shadow：" + ", ".join([
            f"pre={self.soft_gate_pre_shadow}",
            f"post={self.soft_gate_post_shadow}",
            f"total={self.soft_gate_shadow_total}",
        ]))
        lines.append("SoftGate on    ：" + ", ".join([
            f"pre={self.soft_gate_pre_on}",
            f"post={self.soft_gate_post_on}",
            f"total={self.soft_gate_pre_on + self.soft_gate_post_on}",
        ]))
        lines.append("疑似幻觉 Top 工具：" + ", ".join(
            f"{k}={v}" for k, v in sorted(
                self.by_tool().items(), key=lambda kv: -kv[1])[:5]) or "无")
        return "\n".join(lines)
