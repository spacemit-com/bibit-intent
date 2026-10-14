"""可插拔文本归一化后端（证据数值候选生成）。

内置始终使用零依赖的 `cn_number.extract_cn_numbers`；可选加载
WeTextProcessing 的中/英文 ITN 作为增强，覆盖数字/小数/日期/货币等
更宽的口语表达。

依赖加载优先级：
  1. 项目内 vendored 依赖 `deps/pynini` + `deps/wetext`（无需 pip 安装）；
  2. 系统 site-packages 中已安装的 WeTextProcessing/Pynini；
  3. 都不存在则降级，仅使用内置解析。
中英文后端互相独立：任一加载失败只降级该语言，不影响另一语言与主链路。
"""
from __future__ import annotations

import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

from .cn_number import extract_cn_numbers

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_EN_NUMBERS = {"zero":0,"one":1,"two":2,"three":3,"four":4,"five":5,"six":6,"seven":7,"eight":8,"nine":9,"ten":10,"twenty":20,"thirty":30,"forty":40,"fifty":50,"sixty":60,"seventy":70,"eighty":80,"ninety":90,"hundred":100,"half":50,"quarter":25}

# bibit_intent 包目录：src/bibit_intent/core/text_normalizer.py -> parents[1]
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _vendor_paths() -> list[Path]:
    """vendored 依赖目录：src/bibit_intent/deps/pynini 与 deps/wetext（由 wheels 解包而来）。"""
    return [
        _PACKAGE_ROOT / "deps" / "pynini",
        _PACKAGE_ROOT / "deps" / "wetext",
    ]


def _ensure_vendor_on_path() -> None:
    """把 deps/ 下可用的 vendored 目录插入 sys.path（幂等）。"""
    for path in _vendor_paths():
        if path.is_dir() and str(path) not in sys.path:
            sys.path.insert(0, str(path))


@lru_cache(maxsize=1)
def _itn_backend() -> tuple[Any | None, Any | None]:
    """惰性加载中文/英文 ITN 归一化器，分别降级；返回 (zh, en)。"""
    _ensure_vendor_on_path()
    zh = en = None
    try:
        from itn.chinese.inverse_normalizer import InverseNormalizer
        zh = InverseNormalizer()
    except Exception:
        pass
    try:
        from itn.english.inverse_normalizer import (
            InverseNormalizer as EnInverseNormalizer,
        )
        en = EnInverseNormalizer()
    except Exception:
        pass
    return zh, en


def _numbers_from_normalized(text: str) -> list[int | float]:
    """从 ITN 归一化后的文本抽取数值候选（整数保留 int，小数保留 float）。"""
    out: list[int | float] = []
    for m in _NUMBER_RE.finditer(text):
        token = m.group(0)
        try:
            out.append(float(token) if "." in token else int(token))
        except ValueError:
            continue
    return out


def evidence_number_candidates(text: str) -> list[int | float]:
    """从文本提取可作为参数证据的阿拉伯数候选（去重、升序；整数为 int，小数为 float）。

    结果由两部分组成：
      1. 内置 `extract_cn_numbers`（中文数字/成数/百分比，始终可用）；
      2. 可选 WeTextProcessing ITN 归一化后再抽取的数值（覆盖小数与英文数字词等）。
    """
    vals: list[int | float] = list(extract_cn_numbers(text))
    words = re.findall(r"[A-Za-z]+", text.lower())
    for w in words:
        if w in _EN_NUMBERS:
            vals.append(_EN_NUMBERS[w])
    zh, en = _itn_backend()
    if zh is None and en is None:
        return sorted(set(vals))
    try:
        if zh is not None:
            zh_text = zh.normalize(text)
            vals.extend(_numbers_from_normalized(zh_text))
            # 归一化后的书面文本仍可能含中文数字，再走一遍内置解析
            vals.extend(extract_cn_numbers(zh_text))
        if en is not None:
            en_text = en.normalize(text)
            vals.extend(_numbers_from_normalized(en_text))
    except Exception:
        pass
    return sorted(set(vals))
