"""Lightweight language detection for routing."""
from __future__ import annotations
import re

def detect_language(text: str) -> str:
    t = text or ""
    cjk_count = len(re.findall(r"[\u3400-\u9fff]", t))
    en_count = len(re.findall(r"[A-Za-z]+", t))
    has_cjk = cjk_count > 0
    has_en = en_count > 0
    if has_cjk and has_en:
        # Mixed utterances follow the majority language. English is counted by
        # words, Chinese by CJK characters, which is stable for short commands.
        return "zh" if cjk_count >= en_count else "en"
    if has_cjk: return "zh"
    if has_en: return "en"
    return "unknown"
