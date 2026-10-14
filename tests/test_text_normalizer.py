"""text_normalizer 统一归一化入口的单元测试。

通过 monkeypatch 控制 ITN 后端，覆盖：
  - 内置降级路径（等价于 cn_number.extract_cn_numbers）
  - 可选增强路径（小数/英文数字词）
  - 归一化异常时的优雅降级
"""
from __future__ import annotations

import pytest

from bibit_intent.core import text_normalizer as tn
from bibit_intent.core.cn_number import extract_cn_numbers


class _FakeNormalizer:
    def __init__(self, output: str):
        self._output = output

    def normalize(self, text: str) -> str:
        return self._output


def _no_backend(monkeypatch) -> None:
    monkeypatch.setattr(tn, "_itn_backend", lambda: (None, None))


def test_builtin_degrades_to_cn_number(monkeypatch):
    _no_backend(monkeypatch)
    samples = [
        "调节音量到6成",
        "音量调到六成",
        "百分之五十",
        "一半",
        "调节音量到60",
    ]
    for text in samples:
        assert tn.evidence_number_candidates(text) == extract_cn_numbers(text)


def test_builtin_basic_values(monkeypatch):
    _no_backend(monkeypatch)
    assert tn.evidence_number_candidates("调节音量到6成") == [60]
    assert tn.evidence_number_candidates("百分之五十") == [50]
    assert tn.evidence_number_candidates("一半") == [50]


def test_itn_float_enhancement(monkeypatch):
    monkeypatch.setattr(
        tn,
        "_itn_backend",
        lambda: (_FakeNormalizer("2.5平方电线"), _FakeNormalizer("")),
    )
    result = tn.evidence_number_candidates("二点五平方电线")
    assert 2.5 in result
    assert isinstance(next(x for x in result if x == 2.5), float)


def test_itn_english_number_word(monkeypatch):
    monkeypatch.setattr(
        tn,
        "_itn_backend",
        lambda: (_FakeNormalizer(""), _FakeNormalizer("60 percent")),
    )
    assert 60 in tn.evidence_number_candidates("sixty percent")


def test_itn_exception_degrades_gracefully(monkeypatch):
    class _Bad:
        def normalize(self, text: str) -> str:
            raise RuntimeError("boom")

    monkeypatch.setattr(tn, "_itn_backend", lambda: (_Bad(), _Bad()))
    assert tn.evidence_number_candidates("6成") == [60]


def test_sorted_and_unique(monkeypatch):
    monkeypatch.setattr(
        tn,
        "_itn_backend",
        lambda: (_FakeNormalizer("60 60 2.5"), _FakeNormalizer("60")),
    )
    result = tn.evidence_number_candidates("百分之六十")
    assert result == sorted(set(result))
    assert result == [2.5, 60]


def test_partial_degrade_zh_only(monkeypatch):
    monkeypatch.setattr(
        tn,
        "_itn_backend",
        lambda: (_FakeNormalizer("2.5平方电线"), None),
    )
    assert 2.5 in tn.evidence_number_candidates("二点五平方电线")


def test_partial_degrade_en_only(monkeypatch):
    monkeypatch.setattr(
        tn,
        "_itn_backend",
        lambda: (None, _FakeNormalizer("60 percent")),
    )
    assert 60 in tn.evidence_number_candidates("sixty percent")
