"""
agent/cn_number.py — 中文数字确定性解析（零依赖、可单测）

用途：把「百分之五十→50、五成→50、一半→50、一百二→120」等口语中的
中文数字表达确定性转成阿拉伯数字，作为原文证据闸门的「规则证据」：
模型输出的数值若等于原文任一中文数字表达，视为有依据直接执行，
而不是当成猜值挂起确认（见 router._keep_evidenced_params）。

覆盖范围（刻意保守，避免误报）：
  - 百分之N（N 为中文数字或阿拉伯数字）→ N，如「百分之百」→100
  - X成 → X*10，如「八成」→80、「6成」→60
  - 半 / 一半 → 50
  - 普通中文数字序列，要求长度 ≥2 或含 十/百/千/万/亿 单位
    （排除「星期五」「一些」里的单个数字汉字）
"""
from __future__ import annotations

import re

_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000, "亿": 100000000}
_UNIT_CHARS = frozenset(_UNITS)

_CN_SEQ_RE = re.compile(r"[零一二两三四五六七八九十百千万亿]+")
_PERCENT_RE = re.compile(r"百分之\s*([零一二两三四五六七八九十百千万亿]+|\d+)")
_CHENG_RE = re.compile(r"([零一二两三四五六七八九]|\d+)成")
_HALF_RE = re.compile(r"(?:一?半)")
_PERCENT_PREFIX_RE = re.compile(r"百分之\s*")


def _expand_colloquial(seq: str) -> str:
    """口语省略：一百二→一百二十、一千八→一千八百、二百五→二百五十。"""
    m = re.search(r"([百千万])([一二两三四五六七八九])$", seq)
    if m and len(seq) > 2:
        unit, d = m.group(1), m.group(2)
        return seq[:-1] + d + {"百": "十", "千": "百", "万": "千"}[unit]
    return seq


def parse_cn_seq(seq: str) -> int | None:
    """
    中文数字序列 → 阿拉伯数字。非法序列返回 None。
    例：五十→50、十五→15、一百二十→120、一千零五十→1050、十万→100000。
    """
    if not seq:
        return None
    seq = _expand_colloquial(seq)
    total = 0        # 已结算的 万/亿 段
    section = 0      # 当前段（万以下累积）
    num = 0          # 待乘单位前的数字
    for ch in seq:
        if ch in _DIGITS:
            num = _DIGITS[ch]
        elif ch in _UNITS:
            u = _UNITS[ch]
            if u < 10000:
                section += (num if num else 1) * u
                num = 0
            else:
                section = (section + num) * u
                if section == 0:
                    section = u   # 单独"万/亿"开头（如"万"/"一亿"之外的裸"万"）
                total += section
                section = 0
                num = 0
        else:
            return None
    return total + section + num


def extract_cn_numbers(text: str) -> list[int]:
    """从文本中提取中文数字表达 → 去重升序的阿拉伯数字列表。"""
    out: list[int] = []
    for m in _CHENG_RE.finditer(text):
        g = m.group(1)
        out.append((int(g) if g.isdigit() else _DIGITS[g]) * 10)  # 八成→80 / 6成→60
    if _HALF_RE.search(text):
        out.append(50)                                 # 半/一半→50
    for m in _PERCENT_RE.finditer(text):               # 百分之五十→50
        seg = m.group(1)
        v = int(seg) if seg.isdigit() else parse_cn_seq(seg)
        if v is not None:
            out.append(v)
    # 普通中文数字：先去掉"百分之"前缀，避免把量词"百"误当数字
    plain = _PERCENT_PREFIX_RE.sub("", text)
    for m in _CN_SEQ_RE.finditer(plain):
        seq = m.group(0)
        if len(seq) >= 2 or any(c in _UNIT_CHARS for c in seq):
            v = parse_cn_seq(seq)
            if v is not None:
                out.append(v)
    return sorted(set(out))
