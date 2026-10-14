"""
tests/question_probe.py — 问句判别模块设备侧探针（需 AI Gateway Embed）
运行： cd /opt/bibit && .venv/bin/python tests/question_probe.py

验证完整链路：规则引擎(attach 完整判别器) → decide_local_intent，
覆盖 L1/L2 确定性层与 L3 语义锚点（真实 embedding 服务）。
阈值 0.90 依据设备实测校准（见 docs/intent/rule_and_intent_design.md §2.3）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dataclasses import replace
from bibit_intent.models.backends import local_client, embedding_client
from bibit_intent.pipeline import PipelineSettings
from bibit_intent.routing.embedding import EmbeddingClient, EmbeddingRetriever
from bibit_intent.routing.intent import decide_local_intent
from bibit_intent.routing.question_detector import QuestionDetector
from bibit_intent.routing.rule_engine import RuleEngine
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.resources import resource_path


def run() -> bool:
    reg = ToolRegistry(resource_path("tools.yaml"))
    rule = RuleEngine(reg)
    rule.attach_question_detector(
        QuestionDetector(
            EmbeddingRetriever(
                embedding_client(replace(PipelineSettings.from_environment(), local_timeout_seconds=60)),
                [resource_path("question_corpus.jsonl")],
            )
        )
    )

    # (输入, 期望 action.value)
    cases = [
        # 开放问答/怎么做 → 升级
        ("亮度如何调节",      "escalate"),
        ("怎么调节音量",      "escalate"),
        ("怎么打开浏览器",    "escalate"),
        ("怎么结束卡死的进程",  "escalate"),
        ("为什么天是蓝的",    "escalate"),
        ("怎么打开终端呢",    "escalate"),
        # 控制指令/状态查询 → 本地
        ("把声音开大一点",    "rule_call"),
        ("音量调到60",       "rule_call"),
        ("打开浏览器",       "rule_call"),     # launch_app 规则直通
        ("怎么把声音调大",    "rule_call"),     # slot_map 直通，不被问句判别抢走
        ("为什么没声音",      "rule_call"),    # slot_map 没声音→80 直通
        ("现在CPU占用高吗",   "rule_call"),    # CPU占用规则直通（只读）
        ("今天天气怎么样",    "to_model"),
        ("声音是怎么传播的",   "escalate"),    # L1"是怎么…的"→开放问答
        ("帮我写个关闭音量的脚本", "escalate"), # 写作任务→云端
        ("把声音输出切换到蓝牙耳机", "escalate"), # 能力缺失→云端
    ]
    ok = 0
    for text, exp in cases:
        plan = decide_local_intent(text, rule, reg)
        good = plan.action.value == exp
        ok += good
        mark = "OK " if good else "XX "
        print(mark + repr(text).ljust(16) + " -> " + plan.action.value.ljust(10)
              + "(期望 " + exp + ")")
    print("\n结果：%d/%d 通过" % (ok, len(cases)))
    return ok == len(cases)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
