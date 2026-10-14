"""文本命中工具：ASCII 词按单词边界匹配，中文/混排按子串匹配。

背景：关键词/别名预筛此前用裸子串 ``kw in text``，导致英文 "skill" 被
"kill" 命中（skill 末尾就是 kill），把 "看看这个skill" 错路由进 proc_kill。
"""
from __future__ import annotations

import re


def term_hits(text: str, term: str) -> bool:
    """term 是否命中 text。

    ASCII 词（kill/ip/cpu…）用 ASCII 单词边界，防止 "skill" 命中 "kill"、
    "cpuinfo" 命中 "cpu"；中文/混排词仍用普通子串，保留 "看看蓝牙" 这类
    匹配。中文字符不视作 ASCII 单词字符，因此 "我的cpu" / "cpu信息" 仍可命中。
    """
    if not term:
        return False
    if term.isascii():
        return re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])",
            text,
        ) is not None
    return term in text
