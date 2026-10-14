"""
routing/pkg_resolver.py — 安装对象的包名解析

用于「安装中文应用名」场景：先通过本地别名映射 / .desktop 中文名把对象
转成英文候选包名，再用 `apt-cache search --names-only` 搜索并做包名完全
匹配。能完全匹配到候选包名 → 返回该包名（端侧直接 apt-get install）；
搜不到 → 返回 None（由路由层升级云端）。

本模块只做只读搜索与解析，不执行安装。
"""
from __future__ import annotations

import difflib
import re
import subprocess

from bibit_intent.execution.cancellation import run_command
from bibit_intent.execution.system_tools import (
    _app_aliases,
    _get_desktop_apps,
    _match_desktop_app,
)


# ── 生态识别（pip/npm vs apt）────────────────────────────────────────
# 安装对象属于 python/node 生态时绝不该走 apt-cache：这里只在“安装/卸载
# 动词 + 生态提示词”同时出现时判定，避免 “安装 python3-pip” 这类 apt 包名
# 被误判（“python” 是包名的一部分，而不是生态提示词）。

_INSTALL_VERB_RE = re.compile(
    r"(?:安装|下载安装|装(?:个|一下|一个)?|install|uninstall|remove|卸载|移除|卸掉|卸)",
    re.I,
)
_PIP_COMMAND_RE = re.compile(
    r"\b(?:pip|pip3)\s+(?:install|uninstall|安装|卸载|装|卸)\b", re.I
)
_NPM_COMMAND_RE = re.compile(
    r"\b(?:npm|pnpm|yarn)\s+(?:install|i|uninstall|remove|安装|卸载|装|卸)\b",
    re.I,
)
_PIP_HINT_RE = re.compile(r"(?:python|pip)\s*(?:库|模块|包)", re.I)
_NPM_HINT_RE = re.compile(r"(?:npm|node|nodejs|前端)\s*(?:包|依赖)", re.I)
_PIP_BARE_RE = re.compile(r"装(?:个|一下|一个)?\s*(?:库|模块)", re.I)
_NPM_BARE_RE = re.compile(r"装(?:个|一下|一个)?\s*(?:依赖)", re.I)
# 依赖计划执行（"安装依赖 dep-<hex>"）里的"装依赖"不是 npm 裸安装意图，
# 含 plan_id 的句子一律不判生态，交给 routing/intent.py 的 dep_apply 分支。
_DEP_PLAN_RE = re.compile(r"\bdep-[0-9a-f]{6,}\b", re.I)


def detect_ecosystem(text: str) -> str | None:
    """识别安装意图属于哪个生态：返回 "pip" / "npm"，未知返回 None。

    只识别明确的生态安装表达（“pip install requests”“装 python 库 requests”
    “npm 装 axios”“装个库”“装个依赖”）；“安装 vim”“安装 python3-pip”
    这类 apt 包名不会命中，继续走 apt 解析。
    """
    t = (text or "").strip()
    if not t or not _INSTALL_VERB_RE.search(t):
        return None
    if _DEP_PLAN_RE.search(t):
        return None
    if _PIP_COMMAND_RE.search(t) or _PIP_HINT_RE.search(t) or _PIP_BARE_RE.search(t):
        return "pip"
    if _NPM_COMMAND_RE.search(t) or _NPM_HINT_RE.search(t) or _NPM_BARE_RE.search(t):
        return "npm"
    return None


def _candidate_packages(name: str) -> list[str]:
    """把用户口中的安装对象转成英文候选包名（去重、保序）。"""
    raw = (name or "").strip()
    if not raw:
        return []

    candidates: list[str] = []

    # ① 内置中文别名（"浏览器"→chromium、"文件管理器"→pcmanfm-qt 等）
    mapped = _app_aliases().get(raw.casefold())
    if mapped:
        candidates.append(mapped)

    # ② .desktop 中文名 → 文件名 stem（通常等于可执行名/包名）
    desktop = _match_desktop_app(raw, _get_desktop_apps())
    if desktop:
        stem = (desktop.get("stem") or "").strip()
        if stem:
            candidates.append(stem)

    # ③ 对象本身已是 ASCII（如用户直接说 chromium），保留原名做精确匹配
    if raw.isascii():
        candidates.append(raw)

    seen: set[str] = set()
    out: list[str] = []
    for cand in candidates:
        cand = cand.strip()
        if cand and cand not in seen:
            seen.add(cand)
            out.append(cand)
    return out


def _apt_exact_match(package: str, timeout: float = 20) -> bool:
    """用 apt-cache search --names-only 精确匹配包名（^package$）。"""
    pattern = "^" + re.escape(package) + "$"
    try:
        p = run_command(
            ["apt-cache", "search", "--names-only", "--", pattern],
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0 and bool(p.stdout.strip())


def resolve_install_pkg(name: str, timeout: float = 20) -> str | None:
    """
    解析可安装的包名：本地映射/.desktop 得到候选包名后，逐个用
    apt-cache search --names-only 做完全匹配；命中返回包名，否则 None。
    """
    for cand in _candidate_packages(name):
        if _apt_exact_match(cand, timeout=timeout):
            return cand
    return None


def suggest_install_packages(
    name: str, limit: int = 3, timeout: float = 15, names: list[str] | None = None
) -> list[str]:
    """apt 包名的只读近似候选，用于“没有找到软件包 X”的本地提示。

    与 close_app 的近似建议同一语义：只提示、绝不安装。默认用
    `apt-cache pkgnames` 拿全量包名做 difflib 相似度排序；`names` 参数仅供
    测试注入语料，避免测试依赖 apt。找不到近似返回 []。
    """
    raw = (name or "").strip().lower()
    if not raw:
        return []
    if names is None:
        try:
            p = run_command(["apt-cache", "pkgnames"], timeout=timeout)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []
        if p.returncode != 0:
            return []
        names = p.stdout.splitlines()
    scored: list[tuple[float, str]] = []
    seen: set[str] = set()
    for cand in names:
        cand = (cand or "").strip().lower()
        if not cand or cand == raw or cand in seen:
            continue
        seen.add(cand)
        sim = difflib.SequenceMatcher(None, raw, cand).ratio()
        if sim >= 0.5:
            scored.append((sim, cand))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [cand for _, cand in scored[:limit]]
