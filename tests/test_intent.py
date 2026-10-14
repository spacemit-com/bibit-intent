"""
tests/test_intent.py — 本地意图识别单元测试（全离线）
测试 agent.intent.decide_local_intent() 三级决策逻辑：
  ① 规则高置信命中 → RULE_CALL
  ② 开放知识问答 → ESCALATE
  ③ 否则 → TO_MODEL（附候选与工具清单）
运行： python3 tests/test_intent.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bibit_intent.routing.intent import IntentAction, decide_local_intent
from bibit_intent.routing.rule_engine import RuleEngine
from bibit_intent.routing.question_detector import QuestionDetector
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.routing.embedding import RouteResult

_reg = ToolRegistry()
_rule = RuleEngine(_reg)

# (输入, 期望action, 期望tool_name/reason, 备注)
INTENT_CASES = [
    # ① 规则高置信 → RULE_CALL
    ("静音",           IntentAction.RULE_CALL, "mute",         "精确匹配"),
    ("解除静音",       IntentAction.RULE_CALL, "mute",         "规则引擎取消静音(on:false)"),
    ("重启",           IntentAction.RULE_CALL, "reboot",       "精确匹配"),
    ("关机",           IntentAction.RULE_CALL, "shutdown",     "电源规则直通"),
    ("帮我关机",       IntentAction.RULE_CALL, "shutdown",     "电源规则直通（礼貌前缀）"),
    ("休眠",           IntentAction.RULE_CALL, "sleep",        "电源规则直通"),
    ("待机一下",       IntentAction.RULE_CALL, "sleep",        "电源规则直通（待机）"),
    ("音量调到60",      IntentAction.RULE_CALL, "set_volume",   "正则槽位,高置信"),

    # ② 开放问答 → ESCALATE
    ("解释一下量子纠缠", IntentAction.ESCALATE, "open_question", "开放知识问答"),
    ("什么是黑洞",    IntentAction.ESCALATE, "open_question", "开放知识问答"),
    ("为什么天是蓝的", IntentAction.ESCALATE, "open_question", "开放知识问答"),

    # ③ 进模型 → TO_MODEL（控制指令但规则置信度不够高/未命中）
    # 注：相对方向词（大一点/太暗）已由 slot_map 规则直通，见上方 RULE_CALL 用例
    ("把声音开大一点",   IntentAction.RULE_CALL, "adjust_volume", "相对方向词 adjust_volume（up/10）"),
    ("屏幕太暗了看不清", IntentAction.RULE_CALL, "adjust_brightness", "相对方向词 adjust_brightness（up/10）"),
    ("声音调小一点",     IntentAction.RULE_CALL, "adjust_volume", "相对方向词 adjust_volume（down/10）"),
    ("现在CPU占用高吗", IntentAction.RULE_CALL, "get_system_info", "CPU占用规则直通(只读)"),

    # 边界：陷阱用例——"为什么没声音"是控制意图，slot_map 没声音→80 直通
    ("为什么没声音",   IntentAction.RULE_CALL, "set_volume", "slot_map 没声音→80"),
    # ②b 写作/编程任务 → 升级云端 chat
    ("帮我写个关闭音量的脚本", IntentAction.ESCALATE, "writing_task", "写脚本→云端"),
    ("帮我写一首诗",   IntentAction.ESCALATE, "writing_task", "写诗→云端"),
    # ②c 端侧无能力（音频输出切换）→ 升级云端
    ("把声音输出切换到蓝牙耳机", IntentAction.ESCALATE, "unsupported_control", "无此能力"),
    # 科学问句（L1"是怎么…的"）→ 开放问答升级
    ("声音是怎么传播的", IntentAction.ESCALATE, "open_question", "科学知识问答"),
    # “如何/怎么打开 X”无句尾标记也应升级为开放问答，避免缺 app_name
    ("如何打开文件管理器", IntentAction.ESCALATE, "open_question", "如何打开应用"),
    # 网络搜索/报告生成是多步任务，不能误入 pkg_search
    ("搜索AI资讯并生成可视化网页报告", IntentAction.ESCALATE, "writing_task", "网络搜索/报告任务"),
    ("mute the audio", IntentAction.RULE_CALL, "mute", "English control"),
    ("set volume to 60", IntentAction.RULE_CALL, "set_volume", "English slot rule"),
    ("what is a black hole?", IntentAction.ESCALATE, "open_question", "English question"),
    ("please don't restart the computer", IntentAction.ESCALATE, "negated_action", "English negation"),
    # 纯问候/寒暄 → 升级云端，避免 embedding 误召回系统工具
    ("hi", IntentAction.ESCALATE, "chitchat", "English greeting"),
    ("hello", IntentAction.ESCALATE, "chitchat", "English greeting"),
    ("你好呀", IntentAction.ESCALATE, "chitchat", "Chinese greeting"),
    ("在吗", IntentAction.ESCALATE, "chitchat", "Chinese greeting"),

    # ⓪ 本地文件检索触发词（MVP）→ RULE_CALL file_search（最高优先级直通）
    ("本地搜索一下GTK相关的文档", IntentAction.RULE_CALL, "file_search", "本地搜索触发词直通"),
    ("帮我本地搜索一下周报",      IntentAction.RULE_CALL, "file_search", "本地搜索触发词直通"),
    ("本地文档里找一下会议纪要",  IntentAction.RULE_CALL, "file_search", "本地文档触发词"),
    ("本地资料搜索GTK",           IntentAction.RULE_CALL, "file_search", "本地资料触发词"),
    # 无触发词时"找文档"仍走云端（_FILE_OR_MEDIA_TRAP_RE 拦截）
    ("帮我找一下GTK相关的文档",   IntentAction.ESCALATE, "unsupported_control", "无触发词→陷阱升级云端"),
    ("帮我找一下桌面上的ppt",     IntentAction.ESCALATE, "unsupported_control", "打开文件陷阱→云端"),
    # "打开X文件夹/目录"：交给 open_folder 做本地模糊定位（唯一→直开，多个→选序号）
    ("打开bianbu文件夹",          IntentAction.RULE_CALL, "open_folder", "打开X文件夹→open_folder定位"),
    ("打开bianbu目录",            IntentAction.RULE_CALL, "open_folder", "打开X目录→open_folder定位"),
    # 诊断/检查类复杂任务 → 直接升级云端（勿被 file_search 的"查"字误吃）
    ("帮我检查一下本机的开发环境", IntentAction.ESCALATE, "diagnostic_task", "检查开发环境→云端诊断"),
    ("诊断一下系统环境",           IntentAction.ESCALATE, "diagnostic_task", "诊断环境→云端"),
]

# 无关输入（天气/闲聊/写作等）：不应注入核心工具集，候选应为空。
# 这是对「核心集兜底误判」的回归测试：此前"今天天气怎么样"会被硬塞
# 核心工具集，1B 模型幻觉出 CALL vol 导致误执行。
EMPTY_CANDIDATE_CASES = [
    "今天天气怎么样",
]


# 上下文依赖输入（指代/省略对象/否定撤销）：端侧单轮无历史上下文，
# 统一升级云端（reason=context_dependent），不能误识别为单轮工具执行。
CONTEXT_DEPENDENT_CASES = [
    # 指代宾语（"打开它/那个"不能当 launch_app 的 app_name）
    "打开它", "打开那个", "打开这个", "启动它", "运行它",
    "关闭它", "退出它", "停止它", "把它关掉", "把它最小化",
    # 纯指代
    "它", "那个", "这个",
    # 省略对象的相对方向词（"调大一点"不能猜成 set_volume/set_brightness）
    "调大一点", "调小一点", "再调大", "再调小", "调亮点", "调暗点",
    # 否定撤销（"别重启"不能误当 reboot）
    "别重启", "先别重启", "不要重启", "别关机", "别休眠", "不要待机",
]

# 边界放行：完整单轮指令不应被误判为上下文依赖。
NON_CONTEXT_DEPENDENT_CASES = [
    "打开浏览器", "关闭浏览器", "打开文件管理器",
    "把声音调大一点", "调大音量", "调暗屏幕",
    "最大化终端", "重启", "取消静音", "关机", "休眠",
]


# 问句判别模块接入：attach 带语义锚点（假检索器）的完整判别器后，
# "亮度如何调节"这类无句尾标记的怎么做问句应升级为开放问答（原实现
# 会漏放给 embedding → 1B 猜 level=50 → 证据闸门挂起）。
class _FakeQRetriever:
    def best_match(self, text):
        return ("QUESTION", 0.95)


def run_semantic_wiring():
    rule_q = RuleEngine(_reg)
    rule_q.attach_question_detector(QuestionDetector(_FakeQRetriever()))
    plan = decide_local_intent("亮度如何调节", rule_q, _reg)
    ok = plan.action is IntentAction.ESCALATE and plan.reason == "open_question"
    print(f"  {'OK ' if ok else 'XX '}{'亮度如何调节'!r:12} -> "
          f"{plan.action.value} reason={plan.reason}  # L3 语义层接入")
    return ok


def run_near_miss_wiring():
    """L3b 近命中：不注入候选，模型无工具可猜（candidates 应为空）。"""
    class _FakeNearMissRetriever:
        def best_match(self, text):
            return ("QUESTION", 0.85)   # [0.78, 0.90) 近命中带

    rule_q = RuleEngine(_reg)
    rule_q.attach_question_detector(QuestionDetector(_FakeNearMissRetriever()))
    plan = decide_local_intent("怎么结束卡死的进程", rule_q, _reg)
    ok = plan.action is IntentAction.TO_MODEL and plan.candidates == []
    print(f"  {'OK ' if ok else 'XX '}{'怎么结束卡死的进程'!r:12} -> "
          f"{plan.action.value} candidates={len(plan.candidates)}  # L3b 近命中去候选")
    return ok


def run_embedding_margin_guard():
    """embedding-only 注入必须同时满足 top1_score 与 margin。"""
    class _LowMarginEmbed:
        def route(self, text):
            return RouteResult(
                ranked=["reboot", "camera_close"],
                best=("reboot", 0.83),
                top1="reboot",
                top1_score=0.59,
                top2_score=0.588,
                margin=0.006,
            )

    class _HighMarginEmbed:
        def route(self, text):
            return RouteResult(
                ranked=["set_volume"],
                best=("set_volume", 0.90),
                top1="set_volume",
                top1_score=0.90,
                top2_score=0.80,
                margin=0.10,
            )

    low = decide_local_intent("随便看看", _rule, _reg, embed=_LowMarginEmbed())
    high = decide_local_intent("随便看看", _rule, _reg, embed=_HighMarginEmbed())
    ok = (
        low.action is IntentAction.TO_MODEL
        and low.candidates == []
        and high.candidates == ["set_volume"]
        and high.hint_source == "embed"
    )
    print(
        f"  {'OK ' if ok else 'XX '}embedding margin guard -> "
        f"low_candidates={low.candidates} high_candidates={high.candidates}"
    )
    return ok


def run():
    passed = failed = 0
    print("== 本地意图识别决策 ==")
    for inp, exp_action, exp_key, note in INTENT_CASES:
        plan = decide_local_intent(inp, _rule, _reg)
        # 根据不同 action 检查不同字段
        if exp_action is IntentAction.RULE_CALL:
            ok = plan.action == exp_action and plan.tool_name == exp_key
            detail = plan.tool_name
        elif exp_action is IntentAction.ESCALATE:
            ok = plan.action == exp_action and plan.reason == exp_key
            detail = plan.reason
        else:  # TO_MODEL
            ok = plan.action == exp_action and len(plan.tool_list) > 0
            detail = f"{len(plan.candidates)}候选"

        passed += ok; failed += (not ok)
        print(f"  {'OK ' if ok else 'XX '}{inp!r:16} -> {plan.action.value:12} {detail or '':18} # {note}"
              + ("" if ok else f"  期望{exp_action.value}/{exp_key}"))

    # 无关输入：候选必须为空、工具清单必须为空（不注入任何工具）
    print("\n== 无关输入（候选应为空，不注入工具） ==")
    for inp in EMPTY_CANDIDATE_CASES:
        plan = decide_local_intent(inp, _rule, _reg)
        ok = plan.action is IntentAction.TO_MODEL and plan.candidates == []
        passed += ok; failed += (not ok)
        print(f"  {'OK ' if ok else 'XX '}{inp!r:12} -> {plan.action.value:12} "
              f"{len(plan.candidates)}候选 / 工具清单{'空' if not plan.tool_list else '非空!'}")

    # 上下文依赖输入：必须升级云端（reason=context_dependent），不得误识别
    print("\n== 上下文依赖输入（应升级云端，不误识别） ==")
    for inp in CONTEXT_DEPENDENT_CASES:
        plan = decide_local_intent(inp, _rule, _reg)
        ok = plan.action is IntentAction.ESCALATE and plan.reason == "context_dependent"
        passed += ok; failed += (not ok)
        print(f"  {'OK ' if ok else 'XX '}{inp!r:14} -> {plan.action.value:12} reason={plan.reason}"
              + ("" if ok else "   期望 ESCALATE/context_dependent"))

    # 边界放行：完整单轮指令不应被拦截成 context_dependent
    print("\n== 边界放行（完整单轮指令不应被拦截） ==")
    for inp in NON_CONTEXT_DEPENDENT_CASES:
        plan = decide_local_intent(inp, _rule, _reg)
        ok = not (plan.action is IntentAction.ESCALATE and plan.reason == "context_dependent")
        passed += ok; failed += (not ok)
        print(f"  {'OK ' if ok else 'XX '}{inp!r:14} -> {plan.action.value:12} tool={plan.tool_name}"
              + ("" if ok else "   不应升级为 context_dependent"))

    print("\n== 问句判别模块接入（L3 语义层） ==")
    passed += run_semantic_wiring()
    passed += run_near_miss_wiring()
    passed += run_embedding_margin_guard()

    total = passed + failed
    print(f"\n结果：{passed}/{total} 通过")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
