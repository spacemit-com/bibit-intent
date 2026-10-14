"""
tests/hallucination_probe.py — 幻觉检测真机探针（P0，对照指南 7.4）

走完整 Router.handle() 链路（规则 → embedding 候选 → 端侧模型 → 参数证据闸门），
通过 Router.stats（agent/hallucination.py 统一检测结果模型）输出量化幻觉数据：
  - 疑似幻觉事件数 / 率（工具名层 L1 + 参数层 L2）
  - 处置分布（升级 / 挂起确认 / 钳制 / 丢弃 / 有证据放行）
  - 幻觉 Top 工具
执行层全部用 dry-run 桩（不改真实系统状态，危险操作也不会触发）。

用法（设备 /opt/bibit 下，AI Gateway）：
  .venv/bin/python tests/hallucination_probe.py
  .venv/bin/python tests/hallucination_probe.py --group halluc   # 只跑某组
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dataclasses import replace
from bibit_intent.models.backends import local_client, embedding_client
from bibit_intent.pipeline import PipelineSettings
from bibit_intent.routing.embedding import (
    EmbeddingClient,
    EmbeddingRetriever,
    default_index_paths,
)
from bibit_intent.execution.executor import Executor
from bibit_intent.models.local_model import LocalModelClient
from bibit_intent.routing.router import Router, Stage
from bibit_intent.routing.rule_engine import RuleEngine
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.resources import resource_path
from bibit_intent.execution.system_tools import ExecResult

# 语料分组：rule=规则直通(不应幻觉) / fuzzy=模糊单意图(模型路径) /
#          halluc=无关输入(无候选CALL拦截) / unsupported=明确控制但无工具 /
#          oob=数值越界(钳制) / model_normal=正常口语模型用例(有候选)
GROUPS: dict[str, list[str]] = {
    "rule": [
        "把音量调到60",
        "音量调到百分之百",
        "亮度开到一半",
        "关闭WiFi",
    ],
    "fuzzy": [
        "帮我设置音量",
        "把声音开大一点",
        "声音小一点",
        "屏幕太暗了",
        "音量拉满",
        "太刺眼了",
    ],
    "halluc": [
        "今天天气怎么样",
        "今天星期几",
        "帮我写一首诗",
        "介绍一下你自己",
    ],
    "unsupported": [
        "请给我打开浏览器",
        "打开浏览器",
        "帮我打开微信",
    ],
    "oob": [
        "音量调到200",
        "亮度调到150",
    ],
    "model_normal": [
        "vim这个软件给我看看它装了什么",
        "搜索一下python的文档",
        "把亮度调到60",
    ],
}


def build() -> tuple[Router, dict[str, list[str]]]:
    reg = ToolRegistry(resource_path("tools.yaml"))
    handlers = {}
    load_skills(resource_path("skills"), reg, handlers)

    # dry-run 桩：任何工具执行都返回成功，不真正改系统
    def _stub(name: str):
        return lambda **kw: ExecResult(True, f"[dry-run] {name}")
    stub = {t.name: _stub(t.name) for t in reg.all()}

    rule = RuleEngine(reg)
    local = local_client(replace(PipelineSettings.from_environment(), local_timeout_seconds=60))
    executor = Executor(reg, handlers=stub)
    embed = EmbeddingRetriever(
        embedding_client(replace(PipelineSettings.from_environment(), local_timeout_seconds=60)),
        default_index_paths(),
    )
    router = Router(reg, rule, local, executor, kb=None, cloud_client=None, embed=embed)
    return router, GROUPS


def _brief(r) -> str:
    """把 RouteResult 压成一行摘要。"""
    st = r.stage.value
    tool = r.tool_name or "—"
    if r.stage is Stage.CONFIRM and r.params:
        return f"{st:<8} {tool:<16} 推测值={r.params}"
    if r.stage is Stage.CLOUD:
        return f"{st:<8} {'—':<16} escalate={r.escalate_reason}"
    return f"{st:<8} {tool:<16}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", default=None, help="只跑某组（rule/fuzzy/halluc/...）")
    args = ap.parse_args()

    router, groups = build()
    names = [args.group] if args.group else list(groups)
    need_model = any(g != "rule" for g in names)
    if need_model and not router._local.is_available():
        print("ERROR: AI Gateway LLM 不可用", file=sys.stderr)
        return 2

    grand = 0  # 全部疑似幻觉事件
    grand_req = 0

    for g in names:
        router.stats.clear()
        cases = groups[g]
        print(f"\n{'='*88}\n[组] {g}（{len(cases)} 条）\n{'='*88}")
        for inp in cases:
            r = router.handle(inp, session=None)
            print(f"  {inp:<22} → {_brief(r)}")
        print(f"\n{router.stats.summary()}")
        grand += router.stats.hallucinated_events
        grand_req += len(cases)

    print("\n" + "=" * 88)
    print(f"[汇总] 全部 {grand_req} 条请求，疑似幻觉事件 {grand}"
          f"（{grand * 100.0 / max(grand_req, 1):.1f}%）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
