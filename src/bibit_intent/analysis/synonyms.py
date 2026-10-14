"""受控近义词扩展。

默认只加载 resources/synonyms.yaml，避免把 WordNet 等通用词库直接引入端侧。
该解析器只用于候选生成/稀疏匹配的 query expansion，不改写用户原文。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from bibit_intent.resources import resource_path


class SynonymResolver:
    """把命中词条的 canonical 词追加到候选匹配文本中。"""

    def __init__(self, path: str | Path | None = None):
        self._canonical_by_term: dict[str, str] = {}
        raw = Path(path) if path is not None else resource_path("synonyms.yaml")
        try:
            data = yaml.safe_load(raw.read_text(encoding="utf-8")) or {}
        except OSError:
            data = {}
        for group in data.get("groups", []):
            canonical = str(group.get("canonical", "")).strip()
            if not canonical:
                continue
            for term in group.get("terms", []):
                term = str(term).strip()
                if term:
                    self._canonical_by_term[term] = canonical

    def hits(self, text: str) -> list[str]:
        """返回原文本中命中的所有 canonical 词，保持词表顺序。"""
        out: list[str] = []
        seen: set[str] = set()
        for term, canonical in self._canonical_by_term.items():
            if term in text and canonical not in seen:
                seen.add(canonical)
                out.append(canonical)
        return out

    def expand_text(self, text: str) -> str:
        """把命中的 canonical 词追加到原文，供候选生成使用。"""
        extra = self.hits(text)
        if not extra:
            return text
        return f"{text} {' '.join(extra)}"


_DEFAULT_SYNONYM_RESOLVER = SynonymResolver()


def default_synonym_resolver() -> SynonymResolver:
    return _DEFAULT_SYNONYM_RESOLVER
