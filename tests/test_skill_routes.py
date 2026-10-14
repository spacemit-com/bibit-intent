"""
tests/test_skill_routes.py — Skills YAML 路由回归测试（全离线）

覆盖 skills/*/skill.yaml 注册进 ToolRegistry 后的两类关键行为：
  1. 关键字不被其他工具高置信抢占（尤其是内置 tools.yaml 的
     launch_app/close_app/get_system_info/pkg_search 等宽泛规则）；
  2. 关键字按安全契约召回，缺少对象或不支持的命令不得注入候选；
  3. 代表性规则正例能高置信命中对应工具（含此前修复的冲突场景）。

运行：
  pytest tests/test_skill_routes.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bibit_intent.resources import resource_path
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.core.textmatch import term_hits
from bibit_intent.routing.rule_engine import RuleEngine
from bibit_intent.routing.intent import decide_local_intent
from bibit_intent.execution.skill_loader import load_skills


@pytest.fixture(scope="module")
def registry() -> ToolRegistry:
    reg = ToolRegistry(resource_path("tools.yaml"))
    load_skills(resource_path("skills"), reg, {})
    return reg


@pytest.fixture(scope="module")
def rule(registry: ToolRegistry) -> RuleEngine:
    return RuleEngine(registry)


def _skill_keywords() -> list[tuple[str, str, str]]:
    """返回 (skill 目录名, 工具名, keyword) 三元组。"""
    import yaml

    out: list[tuple[str, str, str]] = []
    skills_dir = resource_path("skills")
    for manifest in sorted(skills_dir.glob("*/skill.yaml")):
        skill = manifest.parent.name
        meta = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        for tool_name, tool_data in (meta.get("tools") or {}).items():
            rule_cfg = (tool_data.get("routes") or {}).get("rule") or {}
            keywords = rule_cfg.get("keywords") if isinstance(rule_cfg, dict) else None
            for kw in keywords or []:
                out.append((skill, tool_name, kw))
    return out


def test_skill_keywords_not_hijacked_by_other_tool(rule: RuleEngine) -> None:
    """任何 skill keyword 都不能被其它工具以高置信规则抢占。"""
    failures: list[str] = []
    for skill, tool_name, kw in _skill_keywords():
        m = rule.match(kw)
        if m is not None and m.tool_name != tool_name and m.confidence >= rule.CONFIDENCE_THRESHOLD:
            failures.append(
                f"[{skill}] {tool_name} keyword={kw!r} -> hijacked by "
                f"{m.tool_name} conf={m.confidence}"
            )
    assert not failures, "\n".join(failures)


CONTRACT_KEYWORD_CASES = [
    ("pkg_clean", "清理缓存", "清理软件缓存"),
    ("pkg_clean", "清缓存", "清理软件缓存"),
    ("pkg_clean", "apt autoclean", "apt clean"),
    ("disk_format", "格式化", "格式化/dev/sdb1"),
    ("disk_format", "mkfs", "mkfs /dev/sdb1"),
    ("disk_format", "抹掉分区", "格式化/dev/sdb1"),
    ("proc_kill", "杀进程", "结束123号进程"),
    ("proc_zombie", "僵尸进程", "清理僵尸进程"),
    ("proc_zombie", "清理僵尸", "清理僵尸进程"),
    ("proc_zombie", "zombie", "清理僵尸进程"),
    ("user_del", "deluser", "删除用户alice"),
    ("user_lock", "usermod -L", "锁定用户alice"),
    ("user_unlock", "usermod -U", "解锁用户alice"),
]


@pytest.mark.parametrize("tool_name,keyword,command", CONTRACT_KEYWORD_CASES)
def test_contract_keywords_require_supported_commands(rule, tool_name, keyword, command):
    assert tool_name not in rule.keyword_candidates(keyword)
    assert rule.match_tool(keyword, tool_name) is None
    match = rule.match(keyword)
    assert match is None or match.confidence < rule.CONFIDENCE_THRESHOLD
    match = rule.match(command)
    assert match is not None
    assert match.tool_name == tool_name
    assert match.confidence >= rule.CONFIDENCE_THRESHOLD
    match = rule.match_tool(command, tool_name)
    assert match is not None
    assert match.tool_name == tool_name


def test_skill_keywords_recall_their_own_tool(rule: RuleEngine, registry: ToolRegistry) -> None:
    """符合安全契约的 skill keyword 应召回所属工具。

    CONTRACT_KEYWORD_CASES 明确列出的裸关键词必须拒绝召回；另以完整命令
    验证对应工具仍然可用，不以生产判定函数生成预期结果。

    声明了 routes.rule.require_object 的**有副作用工具**（usb_reset / wifi_disconnect /
    （usb_reset / wifi_disconnect / eth_up / eth_down）其弱命中受护栏约束：
    句中没有出现该工具的直接宾语时，其裸症状类关键词不得注入该工具。

    原因：关键词弱命中只有 0.5，不触发 RULE_CALL，但候选仍会注入第二段
    小模型，模型可能直接 CALL —— "没反应"会把 usb_reset（会真重置 USB 设备）注入，
    "是不是断网了"会把 wifi_disconnect（会真断 WiFi）注入。

    因此本测试对这类工具断言两件事：
      (a) keyword + 直接宾语 必须能召回自身（保证关键词没写死）；
      (b) 本身不含直接宾语的裸 keyword，单独出现时**不得**召回自身（护栏生效）。
    """
    failures: list[str] = []
    guard_leaks: list[str] = []
    blocked_keywords = {(tool, keyword) for tool, keyword, _ in CONTRACT_KEYWORD_CASES}
    declared_keywords = {(tool, keyword) for _, tool, keyword in _skill_keywords()}
    assert blocked_keywords <= declared_keywords
    for skill, tool_name, kw in _skill_keywords():
        if (tool_name, kw) in blocked_keywords:
            assert tool_name not in rule.keyword_candidates(kw)
            continue
        tool = registry.get(tool_name)
        objs = tool.rule_require_object() if tool is not None else []
        if not objs:
            # 未声明护栏的工具：维持原断言不变。
            if tool_name not in rule.keyword_candidates(kw):
                failures.append(f"[{skill}] {tool_name} keyword={kw!r} 未召回自身")
            continue
        # (a) 补上直接宾语后必须召回（空格分隔：避免 ASCII 词与关键词粘连，
        #     如 "重置USB"+"USB" 拼成 "重置USBUSB" 会因单词边界判定而失配）
        if tool_name not in rule.keyword_candidates(f"{objs[0]} {kw}"):
            failures.append(
                f"[{skill}] {tool_name} keyword={kw!r}+{objs[0]!r} 未召回自身"
            )
        # (b) 关键词自身已含直接宾语 → 单独召回是预期行为；否则单独召回 = 护栏失效
        if not any(term_hits(kw, o) for o in objs) and tool_name in rule.keyword_candidates(kw):
            guard_leaks.append(
                f"[{skill}] {tool_name} 裸 keyword={kw!r} 绕过了 require_object 护栏"
            )
    assert not failures, "\n".join(failures)
    assert not guard_leaks, "\n".join(guard_leaks)


# §4.4 回归保护：有副作用工具的弱关键词不得在缺宾语时注入候选。
# 每项 (输入, 不得出现的候选工具)。改动前实测这些输入都会注入右侧工具。
SIDE_EFFECT_NO_INJECT_CASES: list[tuple[str, str]] = [
    ("是不是断网了", "wifi_disconnect"),   # 用户只是"问"，注入后模型可能真断网
    ("没反应", "usb_reset"),               # 会真执行 usbreset 重置 USB 设备
    ("点啥都没反应", "usb_reset"),
    ("掉线了", "usb_reset"),
    ("不好使了", "usb_reset"),
    ("电脑卡住了", "usb_reset"),
]


def test_side_effect_tool_not_injected_without_direct_object(
    rule: RuleEngine, registry: ToolRegistry
) -> None:
    """§4.4-C：缺直接宾语时，有副作用工具不得被注入候选、更不得被直通执行。"""
    for text, tool_name in SIDE_EFFECT_NO_INJECT_CASES:
        plan = decide_local_intent(text, rule, registry, embed=None)
        assert tool_name not in (plan.candidates or []), (
            f"{text!r} 不应注入 {tool_name}，实际候选={plan.candidates}"
        )
        assert plan.tool_name != tool_name, (
            f"{text!r} 不应直通执行 {tool_name}，实际 tool={plan.tool_name}"
        )


POSITIVE_CASES: list[tuple[str, str]] = [
    ("软件交付评估 nodejs", "software_delivery_assess"),
    ("评估软件 nodejs", "software_delivery_assess"),
    ("assess software delivery nodejs", "software_delivery_assess"),
    # bluetooth
    ("打开蓝牙", "bt_on"),
    ("关闭蓝牙", "bt_off"),
    ("扫描蓝牙设备", "bt_scan"),
    ("连接蓝牙耳机", "bt_connect"),
    ("断开当前蓝牙", "bt_disconnect"),
    ("取消配对耳机", "bt_forget"),
    # ubuntu_admin
    ("安装软件vim", "pkg_install"),
    ("卸载软件vim", "pkg_remove"),
    ("系统更新", "pkg_update"),
    ("清理软件缓存", "pkg_clean"),
    ("结束进程123", "proc_kill"),
    ("开启nat", "net_enable_nat"),
    ("启动ssh服务", "service_start"),
    ("停止ssh服务", "service_stop"),
    ("重启ssh服务", "service_restart"),
    # ubuntu_cmd：内置 tools.yaml 冲突回归
    ("查看系统用户", "user_list"),
    ("显示内存大小", "sys_mem_size"),
    ("查看内存型号", "sys_mem_type"),
    ("cpu占用最高", "proc_top_cpu"),
    ("运行多久", "sys_uptime"),
    ("raid状态", "disk_raid"),
    # wifi
    ("扫描wifi", "wifi_scan"),
    ("搜索网络", "wifi_scan"),
    ("查看wifi状态", "wifi_status"),
    ("当前网络", "wifi_status"),
    # English coverage
    ("turn on bluetooth", "bt_on"),
    ("turn off bluetooth", "bt_off"),
    ("scan bluetooth devices", "bt_scan"),
    ("connect to bluetooth headphones", "bt_connect"),
    ("disconnect the current bluetooth device", "bt_disconnect"),
    ("install vim", "pkg_install"),
    ("remove firefox", "pkg_remove"),
    ("update all packages", "pkg_update"),
    ("clean apt cache", "pkg_clean"),
    ("start ssh service", "service_start"),
    ("stop ssh service", "service_stop"),
    ("restart ssh service", "service_restart"),
    ("list users", "user_list"),
    ("show memory size", "sys_mem_size"),
    ("show system uptime", "sys_uptime"),
    ("scan available wifi", "wifi_scan"),
    ("show wifi status", "wifi_status"),
    ("connect to wifi HomeNet", "wifi_connect"),
    ("disconnect wifi", "wifi_disconnect"),
]


def test_skill_positive_rules(rule: RuleEngine) -> None:
    """代表性输入应高置信命中对应 skill 工具。"""
    failures: list[str] = []
    for text, expected in POSITIVE_CASES:
        m = rule.match(text)
        ok = (
            m is not None
            and m.tool_name == expected
            and m.confidence is not None
            and m.confidence >= rule.CONFIDENCE_THRESHOLD
        )
        if not ok:
            failures.append(
                f"{text!r} -> {m.tool_name if m else None} "
                f"conf={m.confidence if m else None}，期望 {expected}"
            )
    assert not failures, "\n".join(failures)


def test_ascii_keyword_uses_word_boundary(rule: RuleEngine) -> None:
    """英文关键词按单词边界匹配："skill" 不应命中 "kill"。"""
    assert "proc_kill" not in rule.keyword_candidates("看看这个skill")
    assert "proc_kill" in rule.keyword_candidates("kill 1234")


def test_skill_word_not_candidate_for_proc_kill(
    rule: RuleEngine, registry: ToolRegistry
) -> None:
    """"看看这个skill" 不应携带 proc_kill 候选进端侧模型。"""
    plan = decide_local_intent("看看这个skill", rule, registry, embed=None)
    assert "proc_kill" not in plan.candidates


def test_install_non_ascii_escalates_to_cloud(
    rule: RuleEngine, registry: ToolRegistry
) -> None:
    """中文应用名安装不能端侧解析包名，应升级云端（不再误报 dpkg-query 失败）。"""
    for text in ["安装浏览器", "安装微信", "下载安装百度网盘", "装个网易云音乐"]:
        plan = decide_local_intent(text, rule, registry, embed=None)
        assert plan.action.value == "escalate", f"{text!r} 应升级云端，实际 {plan.action.value}"
        assert plan.reason == "package_not_found_in_apt", f"{text!r} reason={plan.reason}"


def test_install_non_ascii_resolves_then_installs(
    rule: RuleEngine, registry: ToolRegistry
) -> None:
    """中文应用名安装：解析器搜到完全匹配包名 → 端侧 pkg_install 直通。"""
    def resolver(name: str) -> str | None:
        return {"浏览器": "chromium", "微信": None}.get(name)

    plan = decide_local_intent(
        "安装浏览器", rule, registry, embed=None, pkg_resolver=resolver
    )
    assert plan.action.value == "rule_call", plan.action.value
    assert plan.tool_name == "pkg_install", plan.tool_name
    assert str(plan.slots.get("pkg")) == "chromium", plan.slots

    miss = decide_local_intent(
        "安装微信", rule, registry, embed=None, pkg_resolver=resolver
    )
    assert miss.action.value == "escalate", miss.action.value
    assert miss.reason == "package_not_found_in_apt", miss.reason
    assert miss.slots == {"pkg": "微信", "operation": "install"}


def test_install_ascii_still_rule_call(
    rule: RuleEngine, registry: ToolRegistry
) -> None:
    """英文包名安装仍走规则直通，不能被中文上云规则误伤。"""
    for text, pkg in [
        ("安装fio", "fio"),
        ("安装软件 vim", "vim"),
        ("帮我安装一下 nginx", "nginx"),
    ]:
        plan = decide_local_intent(text, rule, registry, embed=None)
        assert plan.action.value == "rule_call", f"{text!r} 应规则直通，实际 {plan.action.value}"
        assert plan.tool_name == "pkg_install", f"{text!r} tool={plan.tool_name}"
        assert str(plan.slots.get("pkg")) == pkg, f"{text!r} slots={plan.slots}"


def test_install_ascii_unknown_pkg_escalates(
    rule: RuleEngine, registry: ToolRegistry
) -> None:
    """英文包名安装同样要先搜索验证：不存在的包名（安装fxg）应上云。"""
    def resolver(name: str) -> str | None:
        return name if name in {"fio", "vim", "nginx", "chromium"} else None

    miss = decide_local_intent(
        "安装fxg", rule, registry, embed=None, pkg_resolver=resolver
    )
    assert miss.action.value == "escalate", miss.action.value
    assert miss.reason == "package_not_found_in_apt", miss.reason
    assert miss.slots == {"pkg": "fxg", "operation": "install"}

    hit = decide_local_intent(
        "安装fio", rule, registry, embed=None, pkg_resolver=resolver
    )
    assert hit.action.value == "rule_call", hit.action.value
    assert hit.tool_name == "pkg_install", hit.tool_name
    assert str(hit.slots.get("pkg")) == "fio", hit.slots


def test_install_explicit_version_validates_base_pkg(
    rule: RuleEngine, registry: ToolRegistry
) -> None:
    """name=version 安装只校验基础包名，不把完整版本串送去 apt-cache。"""
    def resolver(name: str) -> str | None:
        return name if name == "bash" else None

    hit = decide_local_intent(
        "安装 bash=0", rule, registry, embed=None, pkg_resolver=resolver
    )
    assert hit.action.value == "rule_call", hit.action.value
    assert hit.tool_name == "pkg_install", hit.tool_name
    assert str(hit.slots.get("pkg")) == "bash=0", hit.slots

    miss = decide_local_intent(
        "安装 fxg=0", rule, registry, embed=None, pkg_resolver=resolver
    )
    assert miss.action.value == "escalate", miss.action.value
    assert miss.reason == "package_not_found_in_apt", miss.reason


def test_remove_ascii_unknown_pkg_escalates(
    rule: RuleEngine, registry: ToolRegistry
) -> None:
    """卸载同样先搜索验证：不存在的包名（卸载fxg）上云，存在的包名端侧卸载。"""
    def resolver(name: str) -> str | None:
        return name if name in {"fio", "vim", "nginx", "chromium"} else None

    miss = decide_local_intent(
        "帮我卸载fxg", rule, registry, embed=None, pkg_resolver=resolver
    )
    assert miss.action.value == "escalate", miss.action.value
    assert miss.reason == "package_not_found_in_apt", miss.reason
    assert miss.slots == {"pkg": "fxg", "operation": "remove"}

    hit = decide_local_intent(
        "帮我卸载fio", rule, registry, embed=None, pkg_resolver=resolver
    )
    assert hit.action.value == "rule_call", hit.action.value
    assert hit.tool_name == "pkg_remove", hit.tool_name
    assert str(hit.slots.get("pkg")) == "fio", hit.slots


def test_multi_pkg_target_escalates_to_cloud(
    rule: RuleEngine, registry: ToolRegistry
) -> None:
    """安装/卸载多个软件包应升级云端拆解，避免只处理第一个包。"""
    for text in ["安装hello和fio", "安装 hello、fio", "卸载firefox和chrome"]:
        plan = decide_local_intent(text, rule, registry, embed=None)
        assert plan.action.value == "escalate", f"{text!r} 应升级云端，实际 {plan.action.value}"
        assert plan.reason == "multi_target", f"{text!r} reason={plan.reason}"

    single = decide_local_intent("安装python3-pip", rule, registry, embed=None)
    assert single.action.value == "rule_call", single.action.value
    assert single.tool_name == "pkg_install", single.tool_name


def test_search_engine_prefix_escalates_to_cloud(
    rule: RuleEngine, registry: ToolRegistry
) -> None:
    """搜索引擎前缀搜索应升云，不能被 pkg_search 误吃。"""
    for text in [
        "百度搜索特朗普在干什么",
        "谷歌搜索量子纠缠",
        "用必应搜索天气",
    ]:
        plan = decide_local_intent(text, rule, registry, embed=None)
        assert plan.action.value == "escalate", f"{text!r} -> {plan.action.value}"
        assert plan.reason == "web_search", f"{text!r} reason={plan.reason}"


def test_pkg_search_rejects_web_question(
    rule: RuleEngine, registry: ToolRegistry
) -> None:
    """不带搜索引擎前缀的网页问题也不应命中 pkg_search。"""
    plan = decide_local_intent("搜索特朗普在干什么", rule, registry, embed=None)
    assert plan.action.value != "rule_call", plan.action.value
    assert plan.tool_name != "pkg_search"


def test_pkg_search_still_accepts_package_queries(
    rule: RuleEngine, registry: ToolRegistry
) -> None:
    """合法软件搜索仍应规则直通。"""
    for text in ["帮我搜一下有没有vlc", "搜索软件库中的文本编辑器"]:
        plan = decide_local_intent(text, rule, registry, embed=None)
        assert plan.action.value == "rule_call", f"{text!r} -> {plan.action.value}"
        assert plan.tool_name == "pkg_search", f"{text!r} -> {plan.tool_name}"
