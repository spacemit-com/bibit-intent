"""
agent/intent.py — 本地意图识别（进模型前的确定性决策）

把「第二段模型推理之前」的所有确定性判定收敛到一处，供 router 与测试共用，
消除重复。本模块不执行工具、不调用模型，只输出一个「意图计划」IntentPlan，
告诉调用方下一步该怎么走：

  RULE_CALL  —— 规则引擎高置信命中，直接执行工具（不进模型）
  ESCALATE   —— 开放知识问答等，直接升级到第三段（不进模型）
  TO_MODEL   —— 以上都不是，进第二段模型；附好候选工具清单

对照 design.md §4（路由）、§5.1（动态候选注入）。
"""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from bibit_intent.analysis.base import Analyzer
from bibit_intent.analysis.deterministic import default_analyzer
from bibit_intent.analysis.features import UtteranceAnalysis
from bibit_intent.analysis.synonyms import (
    SynonymResolver,
    default_synonym_resolver,
)
from bibit_intent.routing.embedding import EmbeddingRetriever
from bibit_intent.routing.multi_action import detect_multi_object_query
from bibit_intent.routing.pkg_resolver import detect_ecosystem
from bibit_intent.routing.polysemy import escalate_reason
from bibit_intent.routing.complexity import classify_complexity, Complexity, ComplexityThresholds
from bibit_intent.analysis.lexical import MAX_INPUT, instruction_search
from bibit_intent.routing.rule_engine import RuleEngine
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.core.textmatch import term_hits
from bibit_intent.core.language import detect_language
from bibit_intent.core.package_target import package_pattern_request, split_exact_install_specs
from bibit_intent.core.desktop_locations import DESKTOP_LOCATIONS, resolve_desktop_shortcut
from bibit_intent.core.app_request import missing_app_target_request, normalize_app_request
from bibit_intent.core.request_semantics import (
    file_search_object_allowed,
    is_capability_question,
    non_command_reason,
)

# ── 云端连通性熔断器（模块级状态） ──
_circuit_breaker_state = {
    "last_check_time": 0.0,
    "last_result": True,  # True=可达，False=不可达
    "window_seconds": 30,  # 熔断窗口：30秒内复用上次结果
}


def _cloud_reachable(cloud_base_url: str | None = None) -> bool:
    """探测云端服务可达性（带熔断）。

    窗口内直接返回上次结果，避免断网期间每轮对话都吞一次超时。

    Args:
        cloud_base_url: 云端地址，None 时从环境变量读取

    Returns:
        True=可达，False=不可达
    """
    now = time.monotonic()
    last_check = _circuit_breaker_state["last_check_time"]
    window = _circuit_breaker_state["window_seconds"]

    # 熔断窗口内，复用上次结果
    if now - last_check < window:
        return _circuit_breaker_state["last_result"]

    # 窗口外，真实探测
    if cloud_base_url is None:
        # 从环境变量读取，避免导入 config.py（测试环境可能没装 pydantic_settings）
        import os
        cloud_base_url = os.getenv("CLOUD_BASE_URL", "https://api.rvcompute.com:60000/v1")

    # 提取 host:port（去掉 https:// 和 /v1 等路径）
    m = re.search(r"https?://([^/]+)", cloud_base_url)
    if not m:
        # 解析失败，保守返回 False
        _circuit_breaker_state["last_check_time"] = now
        _circuit_breaker_state["last_result"] = False
        return False

    host_port = m.group(1)

    # 用 curl 探测（2秒超时）
    try:
        result = subprocess.run(
            ["curl", "-sI", "-m", "2", f"https://{host_port}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=3,
            check=False,
        )
        reachable = result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        reachable = False

    # 记录结果
    _circuit_breaker_state["last_check_time"] = now
    _circuit_breaker_state["last_result"] = reachable
    return reachable


# 明确控制指令的特征词（打开/启动/关闭应用等）
_EXPLICIT_CONTROL_KEYWORDS = [
    "打开", "启动", "运行", "开启",
    "关闭", "退出", "停止", "结束",
    "安装", "卸载", "删除", "升级", "重启", "重新启动", "关机", "休眠", "睡眠", "待机", "挂起",
    "open", "launch", "start", "run", "close", "quit", "stop", "install", "uninstall", "remove", "upgrade", "reboot", "restart", "shutdown", "hibernate", "suspend", "standby",
]

# 写作/编程任务（端侧无此能力，升级云端 chat 生成）
_WRITING_TASK_RE = re.compile(
    r"(写|编|生成)[^。！？!?\n]{0,64}(脚本|代码|程序|函数|诗|文章)|"
    r"\b(?:write|create|generate)\b.{0,20}\b(?:poem|article|script|code|program|function)\b",
    re.I,
)

# 办公产出任务（产出动作 + 文档品类）不要被 file_search 直通吃掉；纯检索不受影响。
_OFFICE_DELIVERABLE_RE = re.compile(
    r"(?:帮我|给我|请|替我)?\s*(?:写|撰写|起草|产出|生成|整理成|整理|汇总成|汇总|编排|做)"
    r"\s*(?:一?份|一?个|一下|下)?\s*"
    r"(?:周报|日报|月报|工作汇报|汇报材料|项目计划|排期表|会议纪要|纪要|数据报表|报表|"
    r"方案|演示|幻灯片|pptx?|PPT|表格|文档|说明书)"
    r"|(?:写|起草|生成|整理|汇总)\s*(?:一?份|一?个|一下|下)?\s*(?:关于|有关)?[^，。；]{0,20}"
    r"(?:周报|日报|月报|汇报|会议纪要|排期|演示|幻灯片|表格|报表)",
    re.I,
)

# 本地文件检索触发词：字面词直接直通 file_search，跳过 LLM/embedding，须排在陷阱正则之前；
# 网络语境（"互联网上找本地资料"）由 _is_network_context 处理，正则不做零宽断言。
# 显式「读这个文件」规则必须早于 _LOCAL_SEARCH_RE，否则「读取本地文件 /path」会被判成
# file_search 只回文件名；路径后还有别的要求时升级云端，由模型调 file_read 再加工。
_FILE_READ_RE = re.compile(
    r"^(?:读取|读一下|读读|打开看看|看一下)\s*(?:一下)?\s*(?:本地|本机)?\s*文件\s*[:：]?\s*"
    r"(?P<path>/[^\s，。；：！？、“”\"'）】)]+)"
    r"|^(?:read|open)\s+(?:the\s+)?(?:local\s+)?file\s*[:：]?\s*"
    r"(?P<path_en>/[^\s，。；：！？、“”\"'）】)]+)",
    re.I,
)

_LOCAL_SEARCH_RE = re.compile(
    # 中文：方位词+检索动词 / 检索动词+方位词 / 存在询问 / 方位词+名词
    # 「查」单独成词才算检索动词；排除「检查/核查/审查/复查/排查/清查」等
    # 复合动词（这些是诊断/审查动作，不是文件检索，例：「帮我检查一下本机
    # 的开发环境」应上云端，而非直通 file_search）。
    r"(?:本地|本地里|本机|我本机|电脑上|电脑里|我电脑|我电脑上)\s*"
    r"(?:搜索|查找|找一?下|找找|(?<![检核审复排清])查一?下|(?<![检核审复排清])查查|(?<![检核审复排清])查|检索|搜|看看|翻一下)"
    r"|(?:搜索|查找|找一?下|找找|(?<![检核审复排清])查一?下|(?<![检核审复排清])查查|(?<![检核审复排清])查|检索|搜)\s*(?:一下)?\s*"
    r"(?:本地|本地里|本机|电脑上|电脑里|我电脑)"
    # 存在性询问
    r"|(?:本地|本地里|本机|电脑上|电脑里|我电脑)\s*(?:上|里)?\s*(?:有哪些|有没有|有什么|都有哪些)"
    # 方位词+名词（排除创作语境与应用/工具名，如「文件管理器」不劫持）
    r"|(?:本地|本机|电脑)\s*(?:文档|文件|资料|内容|笔记|周报|图片|pdf|视频)(?!\s*(?:报告|文章|网页|页面|可视化|图表|管理器|浏览器|查看器|编辑器|设置|管理|系统))"
    # 英文：search/find/look up + local/on this machine/on my computer + file/doc
    # 方位词可在动词后（search files on this machine）也可在动词前（on my computer search files）
    # 组合 1: search files on this machine → 动词 + (files…) + 方位词
    r"|\b(?:search|find|look for|look up|browse)\b"
    r"(?:\s+\w+){0,8}\s+"
    r"(?:files?|documents?|docs?|notes?|pdfs?|videos?|images?|photos?)\b"
    r"(?:\s+\w+){0,8}\s+(?:local|on\s+(?:this|my)\s+(?:machine|computer|device|pc)|on\s+disk)\b"
    # 组合 2: search local files → 动词 + local + 名词
    r"|\b(?:search|find|look for|look up|browse)\b.{0,12}"
    r"\b(?:local|on\s+(?:this|my)\s+(?:machine|computer|device|pc)|on\s+disk)\b"
    r".{0,16}\b(?:files?|documents?|docs?|notes?|pdfs?|videos?|images?|photos?)\b"
    # 组合 3: local files search / on my computer search files → 方位词 + 动词 + 名词
    r"|\b(?:local|on\s+(?:this|my)\s+(?:machine|computer|device|pc))\b"
    r".{0,12}\b(?:search|find|look for|look up|browse)\b"
    r".{0,16}\b(?:files?|documents?|docs?|notes?|pdfs?|videos?|images?|photos?)\b",
    re.I,
)


def _is_network_context(text: str) -> bool:
    """句中出现「网络词 + 检索动词」→ 是网络搜索意图，不是本地文件检索。

    如"互联网上找本地资料""网络上搜索本地的新闻"应升级云端网络搜索。
    不影响"本地网络配置""我的电脑网络设置"（网络后是名词，无检索动词）。
    """
    return bool(
        re.search(
            r"(?:互联网|网络|网上|全网|网页)[^。!?]{0,12}(?:搜|找|查|检索|翻|看)",
            text,
        )
    )


# 网络搜索 / 报告生成等多步任务（端侧无此能力，先于规则引擎升级云端）
_WEB_SEARCH_REPORT_RE = re.compile(
    r"(?:搜索|搜一?下|查找|查一下|找找|检索).{0,12}(?:资讯|新闻|网页|报告|文章|资料|教程|全网|互联网|实时|最新)"
    r"|(?:生成|制作|创建|写).{0,12}(?:网页|报告|页面|可视化|图表|文章)"
    r"|\b(?:search|look up|find|browse)\b.{0,20}\b(?:latest news|news|articles|reports|web pages|online)\b"
    r"|\b(?:generate|create|make|write)\b.{0,20}\b(?:web page|report|dashboard|chart|visualization)\b",
    re.I,
)

# 搜索引擎前缀搜索（"百度搜索特朗普在干什么"）→ 端侧无网页搜索能力，
# 先于规则引擎升级云端；否则会被 pkg_search 从"搜索"处误吃成包名查询。
_SEARCH_ENGINE_PREFIX_RE = re.compile(
    r"(?:用|在|上)?(?:百度|谷歌|必应|搜狗|bing|google|baidu|搜索引擎)"
    r"\s*(?:一下|下)?\s*(?:搜索|搜|查|查询|检索)"
    r"|\b(?:use|on)\s+(?:google|bing|baidu|duckduckgo)\s+(?:to\s+)?(?:search|look up|find)\b"
    # 英文：搜索引擎名直接做动词（google search X / bing search X）
    r"|\b(?:google|bing|baidu|duckduckgo)\s+(?:search|look up|find|browse)\b",
    re.I,
)

# 纯问候/寒暄语：短输入且无控制意图，直接升级云端，避免被 embedding
# 误召回系统工具（如 "hi" → camera_close）或进入端侧模型。
_GREETING_RE = re.compile(
    r"^(?:hi|hello|hey|howdy|good\s+(?:morning|afternoon|evening)|"
    r"what'?s\s+up|how\s+are\s+you(?:today)?|"
    r"你好|您好|在吗|在不在|早上好|下午好|晚上好|嗨)"
    r"(?:[啊呀哦喔]|[!！。？?~～\s])*$",
    re.I,
)

# 蓝牙/无线音频输出切换（audio_output_switch 只支持有线/内置设备，蓝牙需升级云端；
# 避免「把声音输出切换到蓝牙耳机」被误切成有线耳机或 window_activate 窗口切换）
_AUDIO_SWITCH_RE = re.compile(
    r"(声音|音频|输出).{0,6}(切换|设置|连接|转到).{0,8}(蓝牙|bluetooth)"
    r"|\b(?:switch|change|route)\b.{0,12}\b(?:audio|sound|output)\b.{0,12}\bbluetooth\b",
    re.I,
)

# 打开知名位置目录 / 显式目录路径 → open_folder 直通（第一期）。
# 这些是确定的目录打开，不是 launch_app 能打开的应用，也不是模糊目录名；
# 模糊目录名（如「打开 bianbu 文件夹」）仍由下方 _FILE_OR_MEDIA_TRAP_RE
# 升级云端。显式路径按目录处理，open_folder 内部用 is_dir 兜底，路径指向
# 普通文件时会拒绝执行而不是误开。
_OPEN_FOLDER_RE = re.compile(
    r"(?:(?:帮我)?(?:打开|启动)\s*|(?:please\s+)?(?:open|launch)\s+(?:the\s+)?)"
    r"(?:"
    r"(?P<loc>桌面|文档|下载|主目录|主文件夹|家目录|根目录|根文件系统"
    r"|desktop|documents?|downloads?|home|root)"
    r"|(?P<path>(?:~|/)[^\s，,。；;！!？?]*)"
    r")(?:\s*(?:文件夹|目录))?"
    r"[。！!]?$",
    re.I,
)

# 打开「位置 + 的/里/里面 + 名称」文件夹（「打开桌面的 bianbu 文件夹」）→
# open_folder 的 scoped 定位：位置限定词解析成基目录，名称在该目录内定位，
# 或命中桌面占位快捷方式（user-home/computer/trash）。放在 _OPEN_FOLDER_RE
# 之后、_OPEN_FOLDER_FUZZY_RE 之前，避免被后者把「桌面的bianbu」粘成一个名。
_OPEN_FOLDER_LOCATION_RE = re.compile(
    r"(?:(?:帮我)?(?:打开|启动)\s*|(?:please\s+)?(?:open|launch)\s+(?:the\s+)?)"
    r"(?P<loc>桌面|文档|下载|主目录|主文件夹|家目录|根目录|根文件系统"
    r"|desktop|documents?|downloads?|home|root)"
    r"(?:里面的|下面的|内的|下的|里的|上的|的|里|中|内)"
    r"(?P<name>[^\s，,。；;！!？?]{1,64}?)\s*(?:(?P<folder>文件夹|目录))?[。！!]?$",
    re.I,
)

# 两层嵌套目录（「打开 bianbu 文件夹里面的 Documents 文件夹」）→ open_folder
# scoped 定位：先解析中间目录 base（XDG 别名 / 主目录名 / 任意目录名），再在
# base 内定位目标 name。放在 _OPEN_FOLDER_LOCATION_RE 之后、模糊定位之前，
# 避免「bianbu文件夹里面的Documents」被模糊定位粘成单个名称或被 launch_app 误吃。
_OPEN_FOLDER_NESTED_RE = re.compile(
    r"(?:(?:帮我)?(?:打开|启动)\s*|(?:please\s+)?(?:open|launch)\s+(?:the\s+)?)"
    r"(?P<base>[^\s，,。；;！!？?]{1,64}?)\s*(?:文件夹|目录)"
    r"(?:里面的|下面的|内的|下的|里的|上的|的|里|中|内)"
    r"(?P<name>[^\s，,。；;！!？?]{1,64}?)\s*(?:文件夹|目录)?[。！!]?$",
    re.I,
)

# 打开模糊名称目录（「打开 bianbu 文件夹 / 打开源码目录」）→ open_folder 定位。
# 名称是「文件夹/目录」之前的非空白串（不含空格的多词名称交给云端）；第一期
# 的 XDG 别名/显式路径由上方 _OPEN_FOLDER_RE 先匹配，不会落到这里。
_OPEN_FOLDER_FUZZY_RE = re.compile(
    r"(?:(?:帮我)?(?:打开|启动)\s*|(?:please\s+)?(?:open|launch)\s+(?:the\s+)?)"
    r"(?P<name>[^\s，,。；;！!？?]{1,64}?)\s*(?:文件夹|目录)[。！!]?$",
    re.I,
)

# 文件/文档打开与磁盘/内存内容检索陷阱（ID-OOS / 关键词陷阱）——
# "打开桌面的年度总结报告"端侧无文件路径解析，不能交给 launch_app；
# "检查磁盘里有没有照片/内存里装了什么歌"命中磁盘/内存关键词但意图域外，
# 必须升级云端，不能直通 disk_check/get_system_info 造成误执行。
# "帮我找一下GTK相关的文档"这类**泛文档检索**（无"打开/磁盘/内存"等
# 动作与载体）也升级云端——端侧无文件检索能力（除"本地搜索"触发词直通
# file_search，见 _LOCAL_SEARCH_RE，已在 ⓪ 提前拦截）。
_FILE_OR_MEDIA_TRAP_RE = re.compile(
    # 打开 + 路径/文档/文件对象
    r"(?:打开|启动)\s*(?:桌面|文档|下载|文件夹|目录|我的|这份|那个|这个)"
    # 打开 + <对象名> + 文件夹/目录（"打开bianbu文件夹"）——文件夹/目录不是应用，
    # launch_app 无法打开，须升级云端；"文件夹/目录"须是对象尾词，避免误伤
    # "打开目录软件"/"打开文件管理器"这类应用名。
    r"|(?:打开|启动)\s*.{0,12}(?:文件夹|目录)(?!\S)"
    # 打开 + 文件扩展名（xxx.pdf / xxx.docx）
    r"|(?:打开|启动).{0,12}\.[A-Za-z0-9]{1,5}\b"
    # 检索 + 磁盘/内存/存储 + 内容谓词（有没有/装的/存的/放的）
    r"|(?:检查|查一下|看看|看下|找找|找一下).{0,10}(?:磁盘|硬盘|内存|存储)"
    r".{0,12}(?:有没有|装了|有什么|存的|放的|装的|找到)"
    r"|\b(?:open|launch)\b.{0,20}\b(?:desktop|documents?|downloads?|folder|directory|file)\b"
    r"|\b(?:check|search|look for|find)\b.{0,20}\b(?:files?|photos?|songs?)\b.{0,12}\b(?:on disk|in memory|on storage)\b"
    # 泛文档检索（找/搜/查/检索 + 文档/文件/资料/笔记/周报/纪要/报告/ppt）
    r"|(?:找|搜|查|检索|找找|找一下).{0,10}(?:文档|文件|资料|笔记|周报|纪要|报告|ppt|PPT|pptx|PDF|pdf)",
    re.I,
)

# 诊断/检查类复杂任务（"帮我检查一下本机的开发环境"/"诊断一下系统环境"）→
# 端侧无此能力：需要多步诊断 + 推理分析，且无对应单工具。直接升级云端，
# 避免 1B 模型幻觉，也避免"检查"里的"查"字被文件检索正则误吃。
_DIAGNOSTIC_TASK_RE = re.compile(
    r"(?:检查|检测|诊断|排查).{0,8}(?:开发环境|运行环境|系统环境|编译环境|依赖环境|环境)",
    re.I,
)

# 安全扫描（rootkit/后门/安全隐患）已从端侧移除 rkhunter 能力，统一升级云端。
# 放在规则引擎之前，避免“检查系统安全隐患”被其它只读工具候选误吃。
_SECURITY_SCAN_RE = re.compile(
    r"(?:检查|检测|扫描|查|查一下|扫一下).{0,8}(?:安全隐患|rootkit|木马|后门|系统安全)"
    r"|(?:rootkit|后门|木马|安全隐患).{0,8}(?:检查|检测|扫描|查)"
    r"|系统安全(?:体检|扫描|检查)",
    re.I,
)

# 网络故障诊断（"上不了网"/"网页打不开"/"无法访问"/"DNS 解析不了"/"网络连不上"）→
# 端侧无诊断能力；更糟的是这类口语在规则层落空后，会掉进 net_ip/net_conn 的
# 关键词预筛，被注入为**唯一候选**，端侧 1B 直接 CALL（用户要的是诊断，
# 系统回一个本机 IP，且完全不上云）。必须在规则引擎之前确定性升级云端。
# 判定：故障谓词与网络对象在 8 字内共现（前后任一方向），避免"USB设备连不上"被误吃。
_NET_FAULT_PRED = (
    r"(?:上不了|上不去|连不上|连接不上|连不了|无法连|不能连|打不开|无法打开|"
    r"访问不了|访问不上|无法访问|不能访问|解析不了|解析失败|"
    # "通不通"/"断了吗" 是**状态查询**（"看看有线网络通不通"→eth_status，
    # "WiFi断了吗"→wifi_status），不是故障陈述，必须排除，否则会把状态查询升级上云。
    r"(?<!通)不通(?!\s*[吗么？?])|断网了|断了(?!\s*[吗么？?])|"
    r"不好使|不好用|没网|掉线|有问题|出问题|异常|故障)"
)
_NET_OBJECT = (
    r"(?:网卡|网络|网线|网口|网页|页面|网站|互联网|宽带|热点|路由|"
    r"wifi|WiFi|WIFI|wi-fi|无线|有线|以太网|dns|DNS|dhcp|DHCP|网)"
)
# 谓词本身隐含网络域（整句没有网络名词也能判定），单独一条备选。
# 不再收"登录"——"让这个用户无法登录"是账号域，不是网络故障。
# (?<!能) 必须："看看这里能不能上网"里的"不能上网"是**能否上网**的疑问，
# 不是故障陈述（该句归 wifi_scan 的口语直通规则）。
_NET_HARD_FAULT = r"(?:无法|(?<!能)不能|没法)(?:访问|上网|联网|连接|连|打开)"

# 症状型谓词：卡/慢/转圈 + 无响应/视觉线索。
# "卡"最危险：_NET_OBJECT 含裸字"网"，而"网卡"是名词——前面是"网"时只有后随 了/得/死/爆
# 才算动词（"网卡了"）；前面不是"网"（网络卡/网速卡/网页卡）直接算症状词。
_KARD = r"(?:(?<!网)卡|卡(?=了|得|死|爆))"
_NET_SYMPTOM_PRED = (
    r"(?:" + _KARD + r"|慢|转圈|缓冲中|缓冲|加载不出|刷不出|发不出|打不通|"
    r"没反应|无响应|不动|断断续续|时好时坏|感叹号|叹号|"
    r"挂了|不太对|不正常|不对劲)"
)

# 英文故障谓词：现有 re.I 只让**已有中文**谓词大小写不敏感，英文表达整体漏判。
# 自带网络对象，作为独立备选（不要求共现）。
_NET_FAULT_PRED_EN = (
    r"(?:no\s+internet|no\s+network|no\s+connection|not\s+connected|"
    r"can'?t\s+connect|cannot\s+connect|can\s+not\s+connect|"
    r"(?:the\s+)?(?:internet|network|wifi|wi-fi)\s+(?:is\s+)?"
    r"(?:down|not\s+working|unreachable|offline|dead))"
)

# 强故障词：字面本身已定性为网络故障，不需要网络对象共现。
# 覆盖"是不是断网了"/"没网了"这类**无对象**的陈述与疑问 ——
# 它们原本会掉进 wifi_disconnect 的关键词弱命中（0.50 → 注入危险候选，
# 见 §4.4），本方案把这类表达换成诊断直通，等于顺手消掉一处危险候选。
# ⚠ 不收裸"掉线了"：实测"USB外设掉线了帮我弄回来"会被误吃（外设域）；
#   带对象的"网络掉线了/无线掉线了"由 _NET_FAULT_PRED 的 掉线 覆盖。
_NET_STANDALONE_FAULT = r"(?:断网了|断网了吗|没网了|网络断了|网络没了)"

# 泛化求助句式：两种形态 —— ①「动词 + 网络 + 怎么了/有问题」②只留主谓
# （"网络怎么了"）。两者都**句尾锚定**，避免吃掉"网络状态如何"（状态查询）
# 与"网络延迟高怎么回事"（知识问答）。
_NET_VAGUE_HELP = (
    r"(?:(?:看看|看下|看一下|检查|查一下|检测|诊断|帮我看看)\s*(?:一下)?\s*(?:我?的)?\s*"
    r"(?:网络连接|网络|网速|网)\s*"
    r"(?:怎么了|怎么回事|有没有问题|有问题吗|是不是有问题)[。！!？?]*$)"
    r"|(?:(?:网络连接|网络|网速)\s*"
    r"(?:怎么了|怎么回事|有没有问题|有问题吗|是不是有问题)[。！!？?]*$)"
)

# ⚠ 拼接时必须给每条备选各自套 (?:…)：_NET_FAULT_PRED 内部含顶层 `|`，
# 直接拼 ".{0,8}" 会让第一个分支退化成"只认谓词、不要求对象共现"
# ——实测导致"USB设备连不上…"/"微信打不开了…"被误升级。
#
# ⚠ 每行末尾必须显式写 `+`：相邻两行只有在**都是字符串字面量**时才能隐式拼接，
#   而行尾是变量名（如 `r"|" + _NET_HARD_FAULT`）再接下一行字符串就是语法错误
#   （"Perhaps you forgot a comma?"）。
_NET_DIAGNOSIS_RE = re.compile(
    r"(?:" + _NET_FAULT_PRED + r".{0,8}" + _NET_OBJECT + r")" +
    r"|(?:" + _NET_OBJECT + r".{0,8}" + _NET_FAULT_PRED + r")" +
    r"|(?:" + _NET_SYMPTOM_PRED + r".{0,8}" + _NET_OBJECT + r")" +
    r"|(?:" + _NET_OBJECT + r".{0,8}" + _NET_SYMPTOM_PRED + r")" +
    r"|" + _NET_HARD_FAULT +
    r"|" + _NET_FAULT_PRED_EN +
    r"|" + _NET_STANDALONE_FAULT +
    r"|" + _NET_VAGUE_HELP,
    re.I,
)

# 「能否上网」类疑问（§4.6 拍板：**只修离线**）。
# 云端可达 → 完全维持现状（wifi_scan / wifi_status / 1B 的既有归属不变）；
# 云端不可达 → 转本地 net_diagnose，让用户拿到一个确定结论。
# 刻意不收"通不通 / 通吗"：那是 eth_status / wifi_status 的既有状态查询
# （H165~H171），不能因为离线就改归属。
_NET_CONNECTIVITY_QUESTION_RE = re.compile(
    r"(?:能不能|能否|可不可以|可以|能)\s*(?:正常\s*)?(?:上网|联网|连网|连上|用网)"
    r"|(?:(?:现在|当前|这会儿|这里)\s*)?有\s*(?:网|网络)\s*(?:吗|么|嘛)"
    r"|(?:网络|网线|宽带|无线|wifi|wi-fi)\s*(?:正常|好使|好用)\s*(?:吗|么|嘛|不)",
    re.I,
)


def is_network_fault_diagnosis(user_input: str) -> bool:
    """输入是否属于「网络故障诊断」（与 ⓪e3 预检门同一判定）。

    抽成公开函数供 router 的离线兜底复用：云端不可达时，无论请求因哪个
    reason 升级（net_diagnosis / context_dependent / no_tool_support …），
    只要是网络故障类表达，都应该能用本地 net_diagnose 作答。
    """
    return bool(_NET_DIAGNOSIS_RE.search(user_input or ""))


# 网络知识问答（"以太网协议是什么"/"wifi6和wifi5区别"/"网络延迟高怎么办"）→
# 本地工具一个都答不了，注入候选只会诱发错调用，直接上云。
# 注意：不能只依赖 QuestionDetector —— 实测它认"什么是以太网"，但不认
# "以太网协议是什么"/"wifi6和wifi5区别"这类后缀式/无句尾标记的句式（覆盖面不够）。
# 判定必须是「网络术语 + 强知识性谓词」：不能用宽泛的"是什么"，否则会把
# "有线网分配的地址是什么"(→eth_ip)、"网卡地址是什么"(→net_mac)、"路由表是什么"
# (→net_route) 这类本地查询误升级。
_NET_KNOWLEDGE_RE = re.compile(
    r"(?:以太网|ethernet|wifi|WiFi|WIFI|wi-fi|无线网|有线网|网络|局域网|广域网|"
    r"子网|网卡|路由|交换机|光纤|带宽|网速|延迟|丢包|dns|DNS|dhcp|DHCP|nat|NAT|协议)"
    r".{0,12}"
    r"(?:协议|标准|规范|原理|历史|由来|发展史|区别|差别|差异|优缺点|"
    r"哪个好|哪个快|哪个更|怎么考证|怎么入门|怎么学习|怎么学|怎么理解|"
    r"怎么连接|怎么连|如何连接|怎么设置|如何设置|怎么配置|如何配置|"
    r"科普|介绍一下|是什么意思|是什么原理|怎么办|怎么解决|怎么处理|如何解决|"
    r"原因|为什么)"
    r"|(?:什么是|什么叫)\s*"
    r"(?:以太网|tcp|TCP|udp|UDP|http|HTTP|https|HTTPS|dhcp|DHCP|dns|DNS|nat|NAT|"
    r"子网|局域网|广域网|网关|路由|交换机|网卡|协议)"
    # 疑问词前置语序："怎么连接网络"（anchor 语料 expect=query_knowledge）——
    # 上面的主体+谓词顺序抓不到，单独放一条；对象必须是网络域词，避免
    # "怎么连接蓝牙耳机"被误升级。
    r"|(?:怎么|如何)\s*(?:连接|连上|接入|设置|配置|使用)\s*.{0,6}"
    r"(?:网络|wifi|WiFi|无线|有线网|有线|以太网|网卡)",
    re.I,
)

# 工具无法表达的参数（"把亮度调成彩虹色"）→ 升级云端澄清，避免 1B 幻觉数字
_UNREPRESENTABLE_SLOT_RE = re.compile(
    r"(?:亮度|声音|音量).{0,6}(?:彩虹|彩色|颜色|红色|蓝色|绿色|粉色|紫色|黄色|黑色|白色)"
    r"|\b(?:volume|sound|brightness|screen)\b.{0,10}\b(?:rainbow|color|red|blue|green|pink|purple|yellow|black|white)\b",
    re.I,
)

# 安装对象不是纯英文包名（如"安装浏览器"/"安装微信"）→ 规则引擎只能解析
# 英文包名；规则未命中的中文应用名安装先走包名解析（pkg_resolver），搜到
# 完全匹配的包名则端侧安装，否则升级云端，避免 1B 模型误判为 dpkg-query。
_INSTALL_NONASCII_PKG_RE = re.compile(
    r"(?:安装|下载安装|装(?:个|一下|一个)?)\s*(?P<name>[^\x00-\x7F].*)"
    r"|\b(?:install|download and install)\s+(?P<en_name>[^\x00-\x7F].*)",
    re.I,
)

# 本地文件安装（"安装test.deb"/"安装 /tmp/foo.deb"/"install ./app.deb"）→
# 规则引擎前直通 install_local。pkg_install 的通用"装"正则会把文件名的
# .deb 尾巴当 apt 包名（apt-cache 校验必失败，误升级云端）；带 .deb/.whl/
# .tgz 尾巴的对象是明确的本地文件意图，不拆不猜，文件是否在当前目录、
# 依赖怎么装由 install_local handler 在拿到路径后决定。
# 目录类表述一并抽取 dir 参数："在 ~/workspace 目录安装 hello.deb"、
# "把 hello.deb 安装到 ~/workspace"，避免文件被错误解析到进程当前目录。
_LOCAL_FILE_TOKEN = r"[\w+./~\-]+\.(?:deb|whl|tgz|tar\.gz)"
_LOCAL_FILE_INSTALL_PATTERNS = (
    # ① 在 <目录> 目录/文件夹/路径(里/中/下) 安装 <文件>
    re.compile(
        r"在\s*(?P<dir>[^，,。;；\s]+?)\s*(?:目录|文件夹|路径)?\s*(?:里|中|下)?\s*"
        r"(?:安装|装(?:个|一下|一个)?|下载安装)\s*(?:本地)?\s*(?:软件|文件|包)?\s*"
        r"(?P<path>" + _LOCAL_FILE_TOKEN + r")",
        re.I,
    ),
    # ② 把/将 <文件> 安装到 <目录>
    re.compile(
        r"(?:(?:把|将)\s*)?(?P<path>" + _LOCAL_FILE_TOKEN + r")\s*(?:安装|装)"
        r"(?:到|至)\s*(?P<dir>[^，,。;；\s]+)",
        re.I,
    ),
    # ③ 安装 <文件> 到 <目录>
    re.compile(
        r"(?:安装|装(?:个|一下|一个)?|下载安装)\s*(?:本地)?\s*(?:软件|文件|包)?\s*"
        r"(?P<path>" + _LOCAL_FILE_TOKEN + r")\s*(?:到|至)\s*(?P<dir>[^，,。;；\s]+)",
        re.I,
    ),
    # ④ 纯文件名/完整路径（不指定目录）
    re.compile(
        r"(?:安装|装(?:个|一下|一个)?|下载安装)\s*(?:本地)?\s*(?:软件|文件|包)?\s*"
        r"(?P<path>" + _LOCAL_FILE_TOKEN + r")",
        re.I,
    ),
    # ⑤ install / install local <file>（英文）
    re.compile(
        r"\b(?:install(?:\s+local)?)\s+(?P<en_path>" + _LOCAL_FILE_TOKEN + r")",
        re.I,
    ),
)


def _local_file_install_slots(text: str) -> dict[str, str] | None:
    """从安装本地文件的句子中抽 {path, dir?}；命中返回 slots，否则 None。

    dir 原样透传给 install_local（handler 会做口语清理与存在性校验），
    抽不到 dir 就让 handler 按进程当前目录解析；仍找不到时由 handler
    直接返回明确提示（不做本地多轮回填）。
    """
    for pat in _LOCAL_FILE_INSTALL_PATTERNS:
        m = instruction_search(pat, text)
        if not m:
            continue
        d = m.groupdict()
        path = (d.get("path") or d.get("en_path") or "").strip()
        if not path:
            continue
        slots: dict[str, str] = {"path": path}
        if d.get("dir"):
            slots["dir"] = d["dir"].strip()
        return slots
    return None

# 依赖计划执行（"安装依赖 dep-<12hex> 2,3"）：plan_id 是强指纹，
# 命中即直通 dep_apply，抽不出则交给下游规则引擎/云端。
_DEP_APPLY_RE = re.compile(
    r"(?:按计划)?安装依赖\s+(?P<plan_id>dep-[0-9a-f]{6,})"
    r"(?:\s+(?P<items>[0-9,，\s]+))?",
    re.I,
)
_DEP_APPLY_EN_RE = re.compile(
    r"\binstall\s+dependencies\s+(?P<plan_id>dep-[0-9a-f]{6,})"
    r"(?:\s+(?P<items>[0-9,\s]+))?",
    re.I,
)


def _dep_apply_slots(text: str) -> dict[str, str] | None:
    """从"安装依赖 dep-…  2,3"抽 {plan_id, items?}；未命中返回 None。"""
    for pat in (_DEP_APPLY_RE, _DEP_APPLY_EN_RE):
        m = pat.search(text or "")
        if not m:
            continue
        slots = {"plan_id": (m.group("plan_id") or "").strip().lower()}
        items = (m.group("items") or "").strip()
        if items:
            slots["items"] = items
        return slots
    return None


# 生态安装包名抽取（pip/npm）：detect_ecosystem 命中后抽包名；
# 抽不出（“装个库”没给包名）→ 升级云端，在线解析依赖网络。
_ECOSYSTEM_PKG_RE = {
    "pip": re.compile(
        r"(?:pip|pip3)\s+(?:install|uninstall|安装|卸载|装|卸)\s+"
        r"(?P<pkg>[A-Za-z0-9_\-\[\].=@]+)"
        r"|(?:安装|装|装个|装一下|下载安装|卸载|卸掉|移除)\s*"
        r"(?:python|pip)\s*(?:库|模块|包)\s*[:：]?\s*(?P<pkg2>[A-Za-z0-9_\-\[\].=@]+)"
        r"|装(?:个|一下|一个)?\s*(?:库|模块)\s*[:：]?\s*(?P<pkg3>[A-Za-z0-9_\-\[\].=@]+)",
        re.I,
    ),
    "npm": re.compile(
        r"(?:npm|pnpm|yarn)\s+(?:install|i|uninstall|remove|安装|卸载|装|卸)\s+"
        r"(?P<pkg>[A-Za-z0-9_\-@/.]+)"
        r"|(?:安装|装|装个|装一下|下载安装|卸载|卸掉|移除)\s*"
        r"(?:npm|node|nodejs|前端)\s*(?:包|依赖)?\s*[:：]?\s*(?P<pkg2>[A-Za-z0-9_\-@/.]+)"
        r"|装(?:个|一下|一个)?\s*(?:依赖)\s*[:：]?\s*(?P<pkg3>[A-Za-z0-9_\-@/.]+)",
        re.I,
    ),
}

_ECOSYSTEM_UNINSTALL_RE = re.compile(
    r"(?:卸载|移除|卸掉|卸|uninstall|remove)", re.I
)

# “如何/怎么打开浏览器”这类“怎么操作”问句，即使无句尾问号/呢，也应升级云端回答；
# 规则能直通的“打开浏览器”已在 ① 命中，不会走到这里。
_HOW_TO_OPEN_RE = re.compile(
    r"(如何|怎么|怎样|咋)\s*(打开|启动|运行|开启)"
    r"|\b(?:how|what is the best way)\b.{0,10}\b(?:open|launch|start|run)\b",
    re.I,
)


def _cover_match_score(cover: str, obj: str) -> int:
    """计算单个 cover 与宾语短语的匹配强度。

    只接受两种确定性命中，避免把宽泛的「内存」错误覆盖到「内存型号」：
      - 完全相等（100）；
      - 英文词根后跟非字母数字边界（80），用于 cpu/pci/usb 等前缀。
    中文只做精确匹配，不做任意子串匹配。
    """
    cover_norm = (cover or "").casefold()
    obj_norm = (obj or "").casefold()
    if not cover_norm or not obj_norm:
        return 0
    if cover_norm == obj_norm:
        return 100
    if cover_norm.isascii() and obj_norm.startswith(cover_norm):
        tail = obj_norm[len(cover_norm):]
        # 只把 ASCII 字母/数字/下划线视为不完整词根；中文等后续字符允许
        # （cpu型号、pci设备、usb设备）。
        boundary_bad = bool(tail) and tail[0].isascii() and (tail[0].isalnum() or tail[0] == "_")
        if not boundary_bad:
            return 80
    return 0


def _resolve_multi_object_read(objects: list[str], registry: ToolRegistry) -> list[str] | None:
    """把多宾语只读查询解析成最小只读工具集；覆盖不完整返回 None。

    每个宾语只接受该宾语在全部只读工具中的最高强度覆盖，低强度子串命中
    不参与集合覆盖，避免「内存大小/内存型号」被一个含「内存」的宽泛工具吞掉。
    同等覆盖增益时优先选择 covers 更少、语义更具体的工具。
    """
    if not objects:
        return None

    tool_scores: dict[str, dict[str, int]] = {}
    best_scores: dict[str, int] = {}
    for tool in registry.all():
        if tool.name == "read_bundle":
            continue
        if tool.system.get("confirm"):
            continue
        # 只允许执行层确认不会改设备状态的工具参与聚合。
        if tool.mcp_group() != "readonly" and tool.system.get("skill") != "ubuntu_cmd":
            continue
        scores: dict[str, int] = {}
        for obj in objects:
            score = max((_cover_match_score(cover, obj) for cover in tool.covers), default=0)
            if score:
                scores[obj] = score
        if not scores:
            continue
        tool_scores[tool.name] = scores
        for obj, score in scores.items():
            best_scores[obj] = max(best_scores.get(obj, 0), score)

    candidates: dict[str, set[str]] = {}
    for name, scores in tool_scores.items():
        covered = {
            obj for obj, score in scores.items()
            if score == best_scores.get(obj, 0)
        }
        if covered:
            candidates[name] = covered
    if not candidates:
        return None

    uncovered = set(objects)
    selected: list[str] = []
    while uncovered and len(selected) < 6:
        best: str | None = None
        best_gain: set[str] = set()
        best_score_sum = -1
        best_specificity = 10**9
        for name, covered in candidates.items():
            if name in selected:
                continue
            gain = covered & uncovered
            if not gain:
                continue
            tool = registry.get(name)
            score_sum = sum(tool_scores[name][obj] for obj in gain)
            specificity = len(tool.covers) if tool else 0
            # 优先级：覆盖宾语数最多 → 覆盖强度最高 → covers 范围最窄。
            better = (
                len(gain),
                score_sum,
                -specificity,
            ) > (
                len(best_gain),
                best_score_sum,
                -best_specificity,
            )
            if best is None or better:
                best = name
                best_gain = gain
                best_score_sum = score_sum
                best_specificity = specificity
        if best is None or not best_gain:
            return None
        selected.append(best)
        uncovered -= best_gain
    if uncovered:
        return None
    return selected
# 长复合句护栏（进规则引擎前）：
#   词数 ≥ 阈值 且 含分隔符（逗号/句号/分号/冒号/顿号等）→ 整句大概率是
#   记录/日程/叙述/复合任务，端侧规则与 1B 模型只看到局部片段极易误判
#   （例："agent os需要全部功能集成进来"曾被 pkg_depends 宽松正则当成包依赖
#   查询并执行 apt-cache depends，报"命令失败(码100)"）。这类输入直接升级
#   云端，由云端模型做整句意图理解与动作拆解。
_LONG_SENTENCE_MIN_TOKENS = 30
_SENTENCE_SEPARATOR_RE = re.compile(r"[，,。;；:：!！?？、]")


def _sentence_token_count(text: str) -> int:
    """零依赖词数近似：英文/数字单词各算 1，每个汉字算 1（不做分词）。"""
    return len(re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]", text or ""))


def is_long_sentence_with_separator(
    text: str, min_tokens: int = _LONG_SENTENCE_MIN_TOKENS
) -> bool:
    return bool(_SENTENCE_SEPARATOR_RE.search(text or "")) and (
        _sentence_token_count(text) >= min_tokens
    )


def _is_explicit_control(text: str) -> bool:
    """
    判断是否为明确的控制指令（如打开应用、关闭应用、安装软件）。
    当用户明确表达控制意图但端侧无对应工具时,应升级到云端而非
    强行从核心工具集里选一个,避免误判。

    示例:
      "打开浏览器" → True (明确控制)
      "音量太大"   → False (虽是控制,但不够"明确",可尝试核心集)
      "介绍量子纠缠" → False (非控制)
    """
    return any(kw in text for kw in _EXPLICIT_CONTROL_KEYWORDS)


class IntentAction(str, Enum):
    RULE_CALL = "rule_call"   # 规则命中 → 直接执行
    ESCALATE = "escalate"     # 前置升级（开放问答等）→ 第三段
    TO_MODEL = "to_model"     # 进第二段模型


@dataclass
class IntentPlan:
    action: IntentAction
    # RULE_CALL 时：
    tool_name: str | None = None
    slots: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    matched_pattern: str = ""
    # ESCALATE 时：
    reason: str | None = None
    # TO_MODEL 时：进模型所需的候选与工具清单
    candidates: list[str] = field(default_factory=list)
    tool_list: str = ""
    # TO_MODEL 时：embedding 语义路由给出的「最可能工具」强提示（可为 None）
    hint: str | None = None
    # 强提示来源：agreed=关键词+embedding双通道一致（高置信，可容错兜底）
    #              embed=仅 embedding（低置信，只作提示不做兜底）
    hint_source: str = ""
    # TO_MODEL 时：embedding 路由结果（供 SoftGate 使用）
    embed_route: Any | None = None
    # 当前输入的语言分析结果（供 Router 注入端侧模型结构提示）
    analysis: UtteranceAnalysis | None = None


# 规则命中判定的置信度阈值（与 router 保持一致）
RULE_CONFIDENCE_THRESHOLD = 0.7
# embedding 语义路由的置信度阈值（余弦相似度，仅关键词缺失时用于提示）
EMBED_CONFIDENCE_THRESHOLD = 0.72
# embedding-only 注入还必须要求 top1 与 top2 的 margin 足够，防止短输入
# 被错误地召回无关工具（hi/hello 的 margin 只有 0.001~0.006）。
EMBED_MARGIN_THRESHOLD = 0.05


def decide_local_intent(
    user_input: str,
    rule: RuleEngine,
    registry: ToolRegistry,
    embed: EmbeddingRetriever | None = None,
    pkg_resolver: Any | None = None,
    analyzer: Analyzer | None = None,
    synonyms: SynonymResolver | None = None,
    cloud_probe: Callable[[], bool] | None = None,
    complexity_thresholds: ComplexityThresholds | None = None,
) -> IntentPlan:
    """
    进模型前的确定性意图决策。三级：
      ① 规则引擎高置信命中 → RULE_CALL
      ② 开放知识问答 → ESCALATE（reason=open_question）
    ③ 否则 → TO_MODEL，并按「候选预筛，空则按核心域线索兜底，
       无关输入不注入工具」构造工具清单
    """

    from bibit_intent.core.package_preview import preview_slots

    if len(user_input) > MAX_INPUT:
        return IntentPlan(action=IntentAction.ESCALATE, reason="input_too_long")
    user_input = normalize_app_request(user_input)
    non_command = non_command_reason(user_input)
    if non_command:
        return IntentPlan(action=IntentAction.ESCALATE, reason=non_command)

    # ⓪e0 打开知名位置目录 / 显式目录路径 → open_folder 直通。
    #     提前到 analysis/complexity 之前：英文「打开 Downloads」会被
    #     确定性分析把 Downloads 误判成动词 download → multi_intent，
    #     中文「打开桌面」无此问题，但统一前移能一并绕开该误判；同时仍早于
    #     下方 _FILE_OR_MEDIA_TRAP_RE，避免「打开桌面 / 打开 /home/xxx」
    #     被当成泛文件打开升级云端。显式路径指向普通文件时由 open_folder
    #     内部 is_dir 兜底拒绝，不会误执行。
    open_dir = _OPEN_FOLDER_RE.match(user_input.strip())
    if open_dir:
        target = (open_dir.group("loc") or open_dir.group("path") or "").strip()
        return IntentPlan(
            action=IntentAction.RULE_CALL, tool_name="open_folder",
            slots={"path": target}, confidence=1.0,
            matched_pattern="open_folder",
        )

    # 第二期扩展：打开「位置+的/里+名称」文件夹，位置与名称分开传给 open_folder
    # 做 scoped 定位（位置→基目录，名称→基目录内的目录或桌面占位快捷方式）。
    open_dir = _OPEN_FOLDER_LOCATION_RE.match(user_input.strip())
    if open_dir:
        loc = (open_dir.group("loc") or "").strip()
        name = (open_dir.group("name") or "").strip()
        # 带「文件夹/目录」后缀的是明确目录打开；无后缀时仅当名称命中桌面占位
        # 快捷方式（计算机/回收站/当前用户名，对应 computer/trash-can/user-home
        # 的 .desktop）才本地打开，否则（如「打开桌面的年度总结报告」）继续
        # 下方规则升级云端。
        if open_dir.group("folder") or resolve_desktop_shortcut(name):
            return IntentPlan(
                action=IntentAction.RULE_CALL, tool_name="open_folder",
                slots={"path": name, "location": loc}, confidence=0.95,
                matched_pattern="open_folder_location",
            )

    # 两层嵌套目录（「打开 bianbu 文件夹里面的 Documents 文件夹」）→
    # open_folder 的 scoped 定位：中间目录为 base（location），目标为 path。
    nested = _OPEN_FOLDER_NESTED_RE.match(user_input.strip())
    if nested:
        base = (nested.group("base") or "").strip()
        name = (nested.group("name") or "").strip()
        return IntentPlan(
            action=IntentAction.RULE_CALL, tool_name="open_folder",
            slots={"path": name, "location": base}, confidence=0.9,
            matched_pattern="open_folder_nested",
        )

    # 第二期：打开模糊名称目录（「打开 bianbu 文件夹」）也走 open_folder，
    # 由 open_folder 内部定位（唯一→直开；多个→挂起选序号；零→提示）。
    open_dir = _OPEN_FOLDER_FUZZY_RE.match(user_input.strip())
    if open_dir:
        name = (open_dir.group("name") or "").strip()
        return IntentPlan(
            action=IntentAction.RULE_CALL, tool_name="open_folder",
            slots={"path": name}, confidence=0.9,
            matched_pattern="open_folder_fuzzy",
        )

    if rule.rejected_command_contract(user_input):
        return IntentPlan(action=IntentAction.ESCALATE, reason="unverified_command")
    if instruction_search(_WRITING_TASK_RE, user_input):
        return IntentPlan(action=IntentAction.ESCALATE, reason="writing_task")
    if missing_app_target_request(user_input):
        return IntentPlan(action=IntentAction.ESCALATE, reason="missing_app_target")
    preview = preview_slots(user_input)
    inspection = re.fullmatch(r"软件包预检\s+(install|remove|安装|卸载)\s+(\S+)", user_input.strip())
    if inspection:
        return IntentPlan(
            action=IntentAction.RULE_CALL, tool_name="pkg_operation_preview",
            slots={"operation": {"安装": "install", "卸载": "remove"}.get(inspection[1], inspection[1]), "pkg": inspection[2]},
            confidence=1.0, matched_pattern="package_operation_preview",
        )
    if preview is not None:
        return IntentPlan(
            action=IntentAction.RULE_CALL, tool_name="pkg_match_preview",
            slots=preview,
            confidence=1.0, matched_pattern="package_preview",
        )
    # ⓪a 搜索引擎前缀搜索先升级云端，避免被 pkg_search 误吃。
    if instruction_search(_SEARCH_ENGINE_PREFIX_RE, user_input):
        return IntentPlan(action=IntentAction.ESCALATE, reason="web_search")
    analysis = (analyzer or default_analyzer()).analyze(user_input)
    synonym_resolver = synonyms or default_synonym_resolver()
    candidate_text = synonym_resolver.expand_text(user_input)

    if analysis.language == "unknown":
        return IntentPlan(action=IntentAction.ESCALATE, reason="unknown_language", analysis=analysis)

    # A capability question must never be converted into a tool execution
    # candidate merely because its technical noun is close to a route anchor.
    if is_capability_question(user_input):
        return IntentPlan(
            action=IntentAction.ESCALATE,
            reason="capability_question",
            analysis=analysis,
        )

    # ⓪g 纯问候/寒暄：无控制意图，不注入候选、不进模型。
    if instruction_search(_GREETING_RE, user_input):
        return IntentPlan(action=IntentAction.ESCALATE, reason="chitchat", analysis=analysis)

    # ⓪ 网络搜索 / 报告生成等多步任务先升级云端，避免被 pkg_search 等本地规则误吃
    if instruction_search(_WEB_SEARCH_REPORT_RE, user_input):
        return IntentPlan(action=IntentAction.ESCALATE, reason="writing_task")

    # ⓪b 多动作复合指令（"打开浏览器并播放三国演义"）→ 升级云端拆分。
    #     必须在规则引擎之前：否则 launch_app 的 ^...$ 会把整句误吃成单个应用名，
    #     1B 模型也易把两件事塞进一个 CALL（见 docs/intent/multi_intent_analysis.md）。
    complexity = classify_complexity(user_input, analysis=analysis, thresholds=complexity_thresholds)
    analysis.debug["complexity"] = {
        "type": complexity.complexity.value,
        "score": complexity.score,
        "features": dict(complexity.features),
        "reasons": list(complexity.reasons),
    }
    if analysis.language == "en" and complexity.features.get("open_question"):
        return IntentPlan(action=IntentAction.ESCALATE, reason="open_question", analysis=analysis)
    if complexity.escalation_reason in {"conditional_intent", "lexicon_unavailable", "input_too_long"}:
        return IntentPlan(action=IntentAction.ESCALATE,
                          reason=complexity.escalation_reason, analysis=analysis)
    if complexity.escalation_reason == "multi_intent" and not rule.matches_closed_contract(user_input):
        return IntentPlan(action=IntentAction.ESCALATE, reason="multi_intent", analysis=analysis)

    # ⓪b2 安装/卸载多个软件包（"安装hello和fio"）→ 端侧单工具只能处理一个
    #     包名，升级云端拆解，避免只安装第一个包。
    if analysis.is_multi_target:
        return IntentPlan(action=IntentAction.ESCALATE, reason="multi_target", analysis=analysis)

    # ⓪b3 单谓词多宾语只读查询（"查看系统版本、内核版本、CPU、内存"）→
    #      先尝试用 covers 解析成最小只读工具集；覆盖完整则本地聚合，否则升云。
    multi_object = detect_multi_object_query(user_input)
    if multi_object.is_multi:
        tool_names = _resolve_multi_object_read(multi_object.objects, registry)
        if not tool_names:
            return IntentPlan(action=IntentAction.ESCALATE, reason="multi_object_read")
        if len(tool_names) == 1:
            return IntentPlan(
                action=IntentAction.RULE_CALL,
                tool_name=tool_names[0],
                slots={},
                confidence=1.0,
                matched_pattern="multi_object_read",
            )
        return IntentPlan(
            action=IntentAction.RULE_CALL,
            tool_name="__read_bundle__",
            slots={"tools": tool_names},
            confidence=1.0,
            matched_pattern="multi_object_read_bundle",
        )

    # ⓪c 上下文依赖输入（指代/省略对象/否定撤销）→ 升级云端。
    #     端侧单轮无历史上下文，解析不了"它/那个/调大一点"这类需要上文的输入；
    #     必须在规则引擎之前拦截，否则"打开它"会被 launch_app 直通、
    #     "别重启"会命中 reboot 关键词。
    if analysis.is_context_dependent:
        return IntentPlan(action=IntentAction.ESCALATE, reason="context_dependent", analysis=analysis)

    # ⓪c2 否定动作（"帮我不要关浏览器"）→ 端侧单轮无法安全处理否定域，
    #      升级云端澄清；前置正则只覆盖句首否定，这里补充句中否定。
    if analysis.is_negated_action:
        return IntentPlan(action=IntentAction.ESCALATE, reason="negated_action", analysis=analysis)

    # ⓪a0 显式读某个文件：纯读取在端侧直通（file_read，只读索引目录里的文本）；
    #       路径后面还有别的要求时升级云端，由模型读文件再按要求加工。
    read_match = _FILE_READ_RE.match(user_input)
    if read_match:
        path = read_match.group("path") or read_match.group("path_en") or ""
        tail = user_input[read_match.end():].strip(" \t，,。.、；;：:!！?？~～")
        if not tail:
            return IntentPlan(
                action=IntentAction.RULE_CALL, tool_name="file_read",
                slots={"path": path}, confidence=1.0,
                matched_pattern="file_read_trigger",
            )
        return IntentPlan(action=IntentAction.ESCALATE, reason="file_read_with_instruction")

    # ⓪c3 写作/产出任务：优先于 file_search 直通（否则「帮我写这周的周报，先用本地
    # 资料…」只会拿到文件名列表）。
    if instruction_search(_OFFICE_DELIVERABLE_RE, user_input):
        return IntentPlan(action=IntentAction.ESCALATE, reason="office_deliverable")

    local_search = bool(instruction_search(_LOCAL_SEARCH_RE, user_input)) and not _is_network_context(user_input)
    nonfile_search = local_search and not file_search_object_allowed(user_input)
    if local_search and not nonfile_search:
        return IntentPlan(
            action=IntentAction.RULE_CALL, tool_name="file_search",
            slots={"query": user_input}, confidence=1.0,
            matched_pattern="local_search_trigger",
        )

    # ⓪d 音频输出切换（"把声音输出切换到蓝牙耳机"）→ 端侧无此工具，先于规则引擎升级，
    #     否则 window_activate 的"切换到…"规则会把它误吃成窗口切换（原在 ②c，
    #     窗口管理工具加入后失效，故提前到规则引擎之前）。
    if instruction_search(_AUDIO_SWITCH_RE, user_input):
        return IntentPlan(action=IntentAction.ESCALATE, reason="unsupported_control")

    # ⓪e 文件/文档打开与磁盘/内存内容检索陷阱（ID-OOS / 关键词陷阱）→ 升级云端，
    #     避免"打开桌面的年度总结报告"被 launch_app 直通、
    #     "检查磁盘里有没有照片"被 disk_check 直通。
    location_request = re.fullmatch(
        r"(?:(?:帮我)?(?:打开|启动)\s*|(?:please\s+)?(?:open|launch)\s+(?:the\s+)?)(.+?)[。！!]?",
        user_input.strip(), re.I,
    )
    if location_request and location_request[1].strip().casefold() in DESKTOP_LOCATIONS:
        return IntentPlan(
            action=IntentAction.RULE_CALL, tool_name="launch_app",
            slots={"app_name": location_request[1].strip()},
            confidence=1.0, matched_pattern="desktop_location",
        )

    if instruction_search(_FILE_OR_MEDIA_TRAP_RE, user_input):
        return IntentPlan(action=IntentAction.ESCALATE, reason="unsupported_control")

    # ⓪e2 诊断/检查类复杂任务（"帮我检查一下本机的开发环境"）→ 升级云端。
    #     需要多步诊断 + 推理分析，端侧无对应单工具，避免 1B 幻觉或误路由。
    if instruction_search(_DIAGNOSTIC_TASK_RE, user_input):
        return IntentPlan(action=IntentAction.ESCALATE, reason="diagnostic_task")

    if instruction_search(_SECURITY_SCAN_RE, user_input):
        return IntentPlan(action=IntentAction.ESCALATE, reason="security_scan")

    # ⓪e3 网络故障诊断（"上不了网"/"网页打不开"/"无法访问"/"DNS 解析不了"）→
    #     端侧无诊断能力；且这类口语在规则层落空后会掉进 net_ip/net_conn 的
    #     关键词预筛，被注入为唯一候选、被 1B 直接 CALL ——
    #     实测"我还是无法访问，我的ip在10.0.91.153"候选=['net_ip']，
    #     用户要诊断，系统去查本机 IP，且完全不上云。必须前置升级。
    explicit_ip_query_after_diagnosis = bool(
        re.search(
            r"(?:查|查询|查看|显示|检查)\s*(?:一下\s*)?(?:本机|本地|我的)?\s*ip(?:地址)?",
            user_input,
            re.I,
        )
    )
    if instruction_search(_NET_DIAGNOSIS_RE, user_input) and not explicit_ip_query_after_diagnosis:
        # 预检门（§5.2）：先探云端是否可达（带熔断，见 _cloud_reachable）。
        # 可达 → 逐字节维持原行为，升级云端；不可达 → 本地兜底 net_diagnose，
        # 复用现有 RULE_CALL 直通机制，不新增动作类型、不改路由框架。
        # cloud_probe 可注入，便于离线单测不依赖真实网络。
        probe = cloud_probe or _cloud_reachable
        if probe():
            return IntentPlan(action=IntentAction.ESCALATE, reason="net_diagnosis", analysis=analysis)
        return IntentPlan(
            action=IntentAction.RULE_CALL,
            tool_name="net_diagnose",
            confidence=1.0,
            matched_pattern="offline_net_diagnosis",
            analysis=analysis,
        )

    # ⓪e3b 「能否上网」类疑问（§4.6 拍板：**只修离线**）。
    #     云端可达 → 此处**不做任何事**，维持既有归属（"看看这里能不能上网"
    #     → wifi_scan 规则直通，"网络正常吗" 由 1B 处理）；
    #     云端不可达 → 转本地 net_diagnose，让用户拿到一个确定结论。
    #     这样有网路径逐字节不变，离线时才多一条出路。
    if instruction_search(_NET_CONNECTIVITY_QUESTION_RE, user_input):
        probe = cloud_probe or _cloud_reachable
        if not probe():
            return IntentPlan(
                action=IntentAction.RULE_CALL,
                tool_name="net_diagnose",
                confidence=1.0,
                matched_pattern="offline_net_connectivity_question",
                analysis=analysis,
            )

    # ⓪e4 网络知识问答（"以太网协议是什么"/"wifi6和wifi5区别"/"网络延迟高怎么办"）→
    #     本地工具一个都答不了，注入候选只会诱发错调用，直接上云。
    if instruction_search(_NET_KNOWLEDGE_RE, user_input):
        return IntentPlan(action=IntentAction.ESCALATE, reason="net_knowledge", analysis=analysis)

    # ⓪f 工具无法表达的参数（"把亮度调成彩虹色"）→ 升级云端澄清，避免 1B 幻觉数字
    if instruction_search(_UNREPRESENTABLE_SLOT_RE, user_input):
        return IntentPlan(action=IntentAction.ESCALATE, reason="unsupported_control")

    # ⓪g 一词多义守卫：对象缺失或跨工具歧义时先升级云端澄清，
    #     避免 reboot/launch_app/close_app 等宽泛规则或候选污染误执行。
    polysemy_reason = escalate_reason(user_input)
    if polysemy_reason:
        return IntentPlan(action=IntentAction.ESCALATE, reason=polysemy_reason)

    # ⓪h- 依赖计划执行（"安装依赖 dep-xxxx"）→ 规则引擎前直通 dep_apply。
    #     必须早于 detect_ecosystem：_NPM_BARE_RE 会把"安装依赖 dep-…"里的
    #     "装依赖"当成 npm 裸安装意图，从而误路由到 npm_install。
    dep_apply_slots = _dep_apply_slots(user_input)
    if dep_apply_slots:
        return IntentPlan(
            action=IntentAction.RULE_CALL,
            tool_name="dep_apply",
            slots=dep_apply_slots,
            confidence=1.0,
            matched_pattern="dependency_plan_apply",
        )

    # ⓪h 生态安装意图（“装 python 库 requests”/“npm install axios”）→ 先于
    #     规则引擎路由到 pip/npm 工具，避免被 pkg_install 的通用“装”规则误吃；
    #     抽不出包名（“装个库”没给名字）→ 升级云端在线解析，不再走 apt-cache。
    ecosystem = detect_ecosystem(user_input)
    if ecosystem:
        m = instruction_search(_ECOSYSTEM_PKG_RE[ecosystem], user_input)
        if m:
            pkg = next((v for v in m.groupdict().values() if v), "")
            pkg = (pkg or "").strip()
            if pkg:
                action = (
                    "uninstall" if instruction_search(_ECOSYSTEM_UNINSTALL_RE, user_input)
                    else "install"
                )
                return IntentPlan(
                    action=IntentAction.RULE_CALL,
                    tool_name=f"{ecosystem}_{action}",
                    slots={"pkg": pkg},
                    confidence=1.0,
                    matched_pattern=f"ecosystem:{ecosystem}_{action}",
                )
        return IntentPlan(action=IntentAction.ESCALATE, reason="ecosystem_online")

    # ⓪i 长复合句（词数 ≥ 30 且含分隔符）→ 直接走云端。放在规则引擎之前，
    #     避免 pkg_depends 等局部正则把长叙述句的某个片段误当包查询执行。
    if is_long_sentence_with_separator(user_input):
        return IntentPlan(
            action=IntentAction.ESCALATE,
            reason="long_sentence",
            analysis=analysis,
        )

    # ⓪j 本地文件安装（"安装test.deb"/"install ./app.deb"/"在 ~/workspace
    #     目录安装 hello.deb"/"把 hello.deb 安装到 ~/workspace"）→ 规则引擎
    #     前直通 install_local：文件是否在当前目录、依赖怎么装由 handler
    #     决定；避免 pkg_install 的通用"装"正则把 ".deb" 当 apt 包名导致
    #     误升级云端，目录表述抽成 dir 一并下发。
    pattern_request = package_pattern_request(user_input)
    if pattern_request:
        tool_name, target = pattern_request
        return IntentPlan(
            action=IntentAction.RULE_CALL,
            tool_name=tool_name,
            slots={"pkg": target},
            confidence=1.0,
            matched_pattern="unsupported_package_pattern",
        )
    local_slots = _local_file_install_slots(user_input)
    if local_slots:
        return IntentPlan(
            action=IntentAction.RULE_CALL,
            tool_name="install_local",
            slots=local_slots,
            confidence=1.0,
            matched_pattern="local_file_install",
        )

    # ① 规则引擎
    relative_direction = (
        "up" if re.search(r"调高|调大|提高|增大|开大|调亮", user_input)
        else "down" if re.search(r"调低|调小|降低|减小|调暗", user_input)
        else None
    )
    if relative_direction and re.search(r"麦克风|话筒|输入音量|录音音量", user_input):
        return IntentPlan(
            action=IntentAction.RULE_CALL,
            tool_name="audio_input_volume",
            slots={"direction": relative_direction, "amount": 10},
            confidence=1.0,
            matched_pattern="relative_input_volume",
        )
    rm = rule.match(user_input, analysis)
    if rm and rm.confidence >= RULE_CONFIDENCE_THRESHOLD:
        # 安装/卸载规则命中后仍需 apt-cache search 完全匹配验证：
        # "安装fxg"/"卸载fxg"这类不存在的英文包名应上云，而不是直接弹密码框执行。
        if rm.tool_name in ("pkg_install", "pkg_remove") and pkg_resolver is not None and rm.matched_pattern != "unsupported_package_pattern":
            pkg = str(rm.slots.get("pkg") or "").strip()
            install_specs = split_exact_install_specs(pkg) if rm.tool_name == "pkg_install" else None
            validation_names = [name for name, _ in install_specs] if install_specs else [pkg]
            if not pkg or any(not name or not pkg_resolver(name) for name in validation_names):
                return IntentPlan(
                    action=IntentAction.ESCALATE,
                    reason="package_not_found_in_apt",
                    slots={"pkg": pkg, "operation": "install" if rm.tool_name == "pkg_install" else "remove"},
                )
        slots = dict(rm.slots)
        return IntentPlan(
            action=IntentAction.RULE_CALL,
            tool_name=rm.tool_name,
            slots=slots,
            confidence=rm.confidence,
            matched_pattern=rm.matched_pattern,
        )

    if nonfile_search:
        return IntentPlan(action=IntentAction.ESCALATE, reason="non_file_query")

    # ①b 安装非英文包名（"安装浏览器"/"安装微信"）→ 先搜索解析（apt-cache search
    #     完全匹配），搜到就返回 RULE_CALL(pkg_install)，搜不到再升级云端。
    install = instruction_search(_INSTALL_NONASCII_PKG_RE, user_input)
    if install:
        pkg_name = install.groupdict().get("name") or install.groupdict().get("en_name") or ""
        if pkg_resolver is not None:
            pkg = pkg_resolver(pkg_name.strip())
            if pkg:
                return IntentPlan(
                    action=IntentAction.RULE_CALL,
                    tool_name="pkg_install",
                    slots={"pkg": pkg},
                    confidence=1.0,
                    matched_pattern="install_name_resolve",
                )
        return IntentPlan(
            action=IntentAction.ESCALATE,
            reason="package_not_found_in_apt",
            slots={"pkg": pkg_name.strip(), "operation": "install"},
        )

    # ② 开放问答前置判断
    if complexity.escalation_reason == "open_question" or rule.is_open_question(user_input):
        return IntentPlan(action=IntentAction.ESCALATE, reason="open_question", analysis=analysis)

    # ②b 写作/编程任务（写脚本/代码/诗）→ 升级云端 chat 生成
    if instruction_search(_WRITING_TASK_RE, user_input):
        return IntentPlan(action=IntentAction.ESCALATE, reason="writing_task")

    # ②d “如何/怎么打开 X” → 开放问答升级，避免进入 launch_app 后缺 app_name
    if instruction_search(_HOW_TO_OPEN_RE, user_input):
        return IntentPlan(action=IntentAction.ESCALATE, reason="open_question")

    # ③ 进模型：先 embedding 语义路由（一次 /embedding 调用，混合打分），
    #    再交小模型决策。route() 同时给出排名与 best，合并原两次调用。
    if complexity.complexity is Complexity.COMPLEX:
        return IntentPlan(action=IntentAction.ESCALATE,
                          reason=complexity.escalation_reason, analysis=analysis)
    candidates = rule.keyword_candidates(candidate_text)
    recalled: list[str] = []
    hit: tuple[str, float] | None = None
    route_result = None
    if embed is not None:
        try:
            route_result = embed.route(user_input)
            recalled = route_result.ranked
            hit = route_result.best
        except Exception:
            recalled, hit, route_result = [], None, None

    top_hint: str | None = None
    hint_source = ""
    if candidates:
        # 双通道一致才给强提示：embedding 也认为最可能的工具排在首位，
        # 避免 embedding 单独判断（曾把依赖方向/文件类型混淆）误导小模型。
        agreed = [t for t in recalled if t in candidates]
        if agreed:
            top_hint = agreed[0]
            hint_source = "agreed"
            candidates = [top_hint] + [t for t in candidates if t != top_hint]
    elif recalled:
        # 关键词够不着 → embedding 语义召回独立供给候选。
        # 仅当 embedding 置信足够高时才注入（否则会把无关工具塞给模型，
        # 如"写诗"被召回成磁盘工具导致误 CALL）；低置信回退原核心工具集。
        if (
            route_result is not None
            and route_result.top1 == recalled[0]
            and route_result.top1_score >= EMBED_CONFIDENCE_THRESHOLD
            and route_result.margin >= EMBED_MARGIN_THRESHOLD
        ):
            candidates = recalled
            top_hint = recalled[0]
            hint_source = "embed"
    if not candidates:
        # 候选为空但用户明确表达控制意图(如"打开xx应用")→ 端侧无此能力,升级
        # 避免硬塞核心工具集导致模型乱猜(如"打开浏览器"被误判成 set_volume)
        if _is_explicit_control(user_input):
            return IntentPlan(action=IntentAction.ESCALATE, reason="unsupported_control")
        # 仅当输入确实涉及核心控制域（命中内置工具的中文口语线索，如
        # "太暗/看不清/占用"）时才注入核心工具集；无关输入（天气/闲聊/
        # 写作等）不硬塞工具，交模型判 out_of_scope 升级，避免 1B 模型
        # 在无关工具里幻觉 CALL（如"今天天气怎么样"被当成 set_volume）。
        if any(term_hits(candidate_text, cue) for cue in registry.core_control_cues()):
            candidates = registry.core_tools()
        else:
            candidates = []
    # 问句近命中（L3b）：含疑问词且与问句原型相似但差一点没达硬阈值
    # （如语义锚点没兜住的"怎么结束卡死的进程"）→ 不注入候选，让 1B
    # 无工具可猜，只能 out_of_scope 升级（防疑问句上幻觉 CALL）。
    # 注意：规则引擎能直通的"怎么把声音调大"等控制指令在 ① 已被拦截，
    # 到不了这里；"为什么没声音"(0.58) 也不在近命中带。
    if candidates and rule.is_near_miss_question(user_input):
        candidates = []
    tool_list = registry.local_tool_list(candidates)
    return IntentPlan(
        action=IntentAction.TO_MODEL,
        candidates=candidates,
        tool_list=tool_list,
        hint=top_hint,
        hint_source=hint_source,
        embed_route=route_result,
        analysis=analysis,
    )
