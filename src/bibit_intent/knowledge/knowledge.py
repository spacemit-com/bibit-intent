"""
agent/knowledge.py — 端侧知识库（BM25 检索）
纯 Python 零依赖，实测 120 文档 0.94ms/次（tech-selection.md §7.1）。
无需 faiss（riscv64 难编译），亚毫秒级足够端侧使用。

对照 design.md §7.2（分级检索：端侧 BM25 → 云端深度 RAG）
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Doc:
    id: str
    title: str
    content: str


@dataclass
class SearchHit:
    doc: Doc
    score: float


def _tokenize(text: str) -> list[str]:
    """中英文混合分词：英文/数字按词，中文按字。"""
    return re.findall(r"[a-z0-9]+|[一-鿿]", text.lower())


class BM25KnowledgeBase:
    """BM25 检索。索引构建在内存，适合端侧中小规模知识库。"""

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._docs: list[Doc] = []
        self._tokenized: list[list[str]] = []
        self._df: dict[str, int] = {}
        self._idf: dict[str, float] = {}
        self._avgdl: float = 0.0

    # ── 构建 ──────────────────────────────────────────────────────────

    def load_dir(self, kb_dir: str | Path) -> int:
        """
        从目录加载知识库。支持 .md / .txt。
        每个文件是一篇文档，文件名（去扩展名）作为标题。
        返回加载的文档数。
        """
        kb_dir = Path(kb_dir)
        docs = []
        if kb_dir.is_dir():
            for f in sorted(kb_dir.glob("**/*")):
                if f.suffix in (".md", ".txt") and f.is_file():
                    docs.append(Doc(
                        id=str(f.relative_to(kb_dir)),
                        title=f.stem,
                        content=f.read_text(encoding="utf-8"),
                    ))
        self.build(docs)
        return len(docs)

    def build(self, docs: list[Doc]) -> None:
        """构建 BM25 索引。"""
        self._docs = docs
        self._tokenized = [_tokenize(f"{d.title} {d.content}") for d in docs]
        n = len(docs)
        if n == 0:
            return
        self._avgdl = sum(len(t) for t in self._tokenized) / n
        self._df = {}
        for toks in self._tokenized:
            for w in set(toks):
                self._df[w] = self._df.get(w, 0) + 1
        self._idf = {
            w: math.log(1 + (n - df + 0.5) / (df + 0.5))
            for w, df in self._df.items()
        }

    # ── 检索 ──────────────────────────────────────────────────────────

    def search(self, query: str, top_k: int = 3) -> list[SearchHit]:
        if not self._docs:
            return []
        q_tokens = _tokenize(query)
        scored: list[SearchHit] = []
        for i, toks in enumerate(self._tokenized):
            dl = len(toks)
            score = 0.0
            for w in q_tokens:
                idf = self._idf.get(w)
                if idf is None:
                    continue
                tf = toks.count(w)
                denom = tf + self.k1 * (1 - self.b + self.b * dl / self._avgdl)
                score += idf * tf * (self.k1 + 1) / denom
            if score > 0:
                scored.append(SearchHit(doc=self._docs[i], score=score))
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[:top_k]

    def context_snippet(self, query: str, top_k: int = 2, max_chars: int = 300) -> str:
        """
        为第二段/第三段生成可注入的知识片段。
        512T 约束下须严格控制长度（默认 300 字符）。
        """
        hits = self.search(query, top_k=top_k)
        if not hits:
            return ""
        parts = []
        budget = max_chars
        for h in hits:
            chunk = h.doc.content.strip().replace("\n", " ")[:budget]
            parts.append(f"[{h.doc.title}] {chunk}")
            budget -= len(chunk)
            if budget <= 0:
                break
        return "\n".join(parts)
