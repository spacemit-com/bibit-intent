"""
agent/embedding.py — 端侧 embedding 语义路由（正式语义路由）

背景：端侧 1B 模型对「规则够不着的口语用例」路由准确率低，根因之一是候选
注入只靠关键词预筛，口语表达（"vim 安装出来的文件放在哪些目录"）与工具
关键词对不上。此前用 bge-small-zh 做纯余弦召回，但仍是「示例级」打分，
且 recall()/best_match() 各调一次 /embedding。

本版本升级为「混合语义路由」（对照业界 Semantic Router 的 dense+sparse 混合）：
  1. 稠密通道（dense）：bge-small-zh 把用户口语编码成 512 维向量，与锚点
     示例做余弦相似度，**按工具分组打分**（组内 max 与均值 0.7/0.3 加权），
     避免各工具锚点数多寡影响排名。
  2. 稀疏通道（sparse）：BM25 用字符 bigram + ASCII 词（纯标准库实现），
     对每个工具的锚点文档打分，弥补小 embedding 对「装了/依赖/病毒」这类
     术语的混淆，语义相近工具（pkg_files/pkg_installed）靠词面区分。
  3. 混合排序：hybrid = 0.75*dense + 0.25*sparse（稀疏归一化到 0~1）。
     route() 返回工具排名；best 仍返回 (工具, 组内最大余弦) 供阈值门控。
  4. 单次查询只调用一次 /embedding（route() 合并原 recall+best_match 两次调用）。

索引锚点 = corpus.jsonl 的 group=rule 用例 + tests/ubuntu_cmd/anchors.jsonl
（每工具 4~5 句口语锚点）。惰性构建并缓存到 *.embed_cache.json
（version 3，多源文件 mtime 校验）。服务不可用/构建失败静默降级：
route() 返回空排名，调用方自然回退到关键词逻辑。

对照 design.md §5.1「动态候选注入」。
本模块只依赖标准库（不引 numpy）。
"""
from __future__ import annotations

import json
import math
import operator
import re
import threading
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from bibit_intent.routing.multi_action import find_connectors, find_verbs

class EmbeddingClient(ABC):
    """Transport-independent interface used by the vector retriever."""

    def __init__(self, base_url: str, timeout: int = 10):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @abstractmethod
    def is_available(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def embed(self, text: str) -> list[float] | None:
        raise NotImplementedError


@dataclass
class _Example:
    tool: str
    text: str
    vec: list[float]
    norm: float = 0.0  # 预计算 L2 模，避免每次查询重算（约 5 倍提速）


@dataclass
class RouteResult:
    """一次语义路由的输出。ranked=按混合分排名的工具（最多 top_k 个）。"""
    ranked: list[str]
    best: tuple[str, float] | None
    top1: str | None = None
    top1_score: float = 0.0
    top2_score: float = 0.0
    margin: float = 0.0


def _dot(a: list[float], b: list[float]) -> float:
    """点积：map(operator.mul) 走 C 级内建，比 zip 生成器快 ~5 倍（纯标准库）。"""
    return sum(map(operator.mul, a, b))


def _norm(v: list[float]) -> float:
    return math.sqrt(_dot(v, v))


def _cosine(a: list[float], b: list[float], na: float = 0.0, nb: float = 0.0) -> float:
    """
    余弦相似度。na/nb 为预计算模（0 时现算）。
    端点向量由 llama.cpp --embd-normalize 2 归一化，模≈1，但预计算仍稳妥。
    """
    na = na or _norm(a)
    nb = nb or _norm(b)
    return _dot(a, b) / (na * nb) if na and nb else 0.0


# ── 稀疏通道：BM25（纯标准库）────────────────────────────────────────

_ASCII_WORD_RE = re.compile(r"[a-zA-Z0-9]+")


def _tokenize(text: str) -> list[str]:
    """
    中文按字符 bigram 切词（无需分词库），ASCII 字母数字串按整词保留；
    再追加浅层语法特征（动作动词/连接词），让 BM25 稀疏通道区分
    「装了哪些文件」和「哪些文件被依赖」这类词面相近但结构不同的输入。
    例："装了哪些文件 /etc" -> ["装了","了哪","哪些","些文","文件","etc"]
    """
    toks: list[str] = []
    han = "".join(ch for ch in text if "\u4e00" <= ch <= "\u9fff")
    toks.extend(han[i : i + 2] for i in range(len(han) - 1))
    for w in _ASCII_WORD_RE.findall(text.lower()):
        if len(w) >= 2:
            toks.append(w)
    toks.extend(f"verb:{v}" for v in find_verbs(text))
    toks.extend(f"conn:{c}" for c in find_connectors(text))
    return toks


class _BM25:
    """标准 BM25 打分器（k1=1.5, b=0.75）。文档=每个工具的全部锚点文本。"""

    def __init__(self, docs: dict[str, list[str]], k1: float = 1.5, b: float = 0.75):
        self._docs = docs
        self._lens = {t: len(toks) for t, toks in docs.items()}
        self._n = len(docs)
        self._avg = sum(self._lens.values()) / self._n if self._n else 0.0
        self._df: Counter[str] = Counter()
        for toks in docs.values():
            self._df.update(set(toks))
        self._k1, self._b = k1, b

    def scores(self, query_tokens: list[str]) -> dict[str, float]:
        """返回每个工具的词面匹配得分（无词面重叠为 0）。"""
        qf = Counter(query_tokens)
        out: dict[str, float] = {}
        for tool, toks in self._docs.items():
            dl = self._lens[tool]
            s = 0.0
            for tok, c in qf.items():
                n = self._df.get(tok, 0)
                if not n:
                    continue
                tf = sum(1 for t in toks if t == tok)
                if not tf:
                    continue
                idf = math.log(1 + (self._n - n + 0.5) / (n + 0.5))
                denom = tf + self._k1 * (1 - self._b + self._b * dl / self._avg)
                s += idf * c * (tf * (self._k1 + 1)) / denom
            out[tool] = s
        return out


# ── 语义路由器 ────────────────────────────────────────────────────────

# 索引语料的唯一真源（相对项目根）。main.py 与 tests/*/probe.py 都从这里取，
# 保证线上路由与探针测的是同一份索引。每份语料只取 group=rule 的句子做锚点，
# 并自动带上同目录的 anchors.jsonl（不存在则跳过）。新增设备域时改这里。
DEFAULT_INDEXES: tuple[str, ...] = (
    "resources/route-data/ubuntu_cmd.jsonl",
    "resources/route-data/system_control.jsonl",
    "resources/route-data/ubuntu_cmd-en.jsonl",
    "resources/route-data/system_control-en.jsonl",
    "resources/route-data/ubuntu_admin-en.jsonl",
)

# 项目根：agent/embedding.py -> agent -> 根
_PROJECT_ROOT = Path(__file__).resolve().parents[1]  # src/bibit_intent


def default_index_paths(root: str | Path | None = None) -> list[Path]:
    """把 DEFAULT_INDEXES 解析成绝对路径。root 缺省为项目根。"""
    base = Path(root) if root is not None else _PROJECT_ROOT
    return [base / p for p in DEFAULT_INDEXES]


def _group_score(vals: list[float]) -> float:
    """工具分组打分：max 保召回，均值稳方向（0.7/0.3 加权）。"""
    if not vals:
        return 0.0
    mx = max(vals)
    avg = sum(vals) / len(vals)
    return 0.7 * mx + 0.3 * avg


class EmbeddingRetriever:
    """
    口语 → 工具名的混合语义路由器（dense 余弦 + sparse BM25）。

    索引锚点：index_path（可传多份 corpus.jsonl，只取 group=rule 用例）+
    anchor_paths（默认取每份语料同目录的 anchors.jsonl）。
    惰性构建并缓存到首份 corpus 旁的 *.embed_cache.json（version 4，
    按完整路径校验所有源文件 mtime）。服务不可用/缓存缺失/构建失败静默降级。

    索引源的唯一真源是 DEFAULT_INDEXES / default_index_paths()，
    main.py 与各 probe 都从那里取，避免线上线下索引不一致。
    """

    def __init__(
        self,
        client: EmbeddingClient,
        index_path: str | Path | list[str | Path],
        anchor_paths: list[str | Path] | None = None,
        cache_path: str | Path | None = None,
        top_k: int = 5,
        dense_weight: float = 0.75,
    ):
        self._client = client
        # index_path 可传单个路径，也可传多份语料（覆盖多个设备域）。
        # 首个路径决定缓存文件位置；每份语料自动带上同目录的 anchors.jsonl
        # （不存在则在 _build_index 里被 p.exists() 跳过）。
        raw = index_path if isinstance(index_path, (list, tuple)) else [index_path]
        indexes = [Path(p) for p in raw]
        self._index_path = indexes[0]
        paths: list[Path] = []
        for p in indexes:
            paths.append(p)
            paths.append(p.with_name("anchors.jsonl"))
            # Language-specific corpora may provide <stem>-anchors.jsonl.
            paths.append(p.with_name(p.stem + "-anchors.jsonl"))
        paths.extend(Path(p) for p in (anchor_paths or []))
        seen: set[Path] = set()
        self._paths = [p for p in paths if not (p in seen or seen.add(p))]
        self._cache_path = (
            Path(cache_path)
            if cache_path is not None
            else self._index_path.with_name(self._index_path.name + ".embed_cache.json")
        )
        self._top_k = top_k
        self._dense_w = max(0.5, min(1.0, dense_weight))
        self._examples: list[_Example] | None = None  # None = 未构建
        self._bm25: _BM25 | None = None
        self._build_lock = threading.Lock()
        self._building = False

    @property
    def cache_path(self) -> Path:
        return self._cache_path

    # ── 对外接口 ──────────────────────────────────────────────────────

    def start_background_build(self) -> None:
        """后台预热索引；预热期间请求直接使用其余路由信号。"""
        if not self._claim_build():
            return
        threading.Thread(
            target=self._background_build,
            name="bibit-embedding-index",
            daemon=True,
        ).start()

    def route(self, text: str) -> RouteResult:
        """
        一次语义路由（只调一次 /embedding）：
          ranked — 按 hybrid=dense_weight*dense + (1-dense_weight)*sparse
                   排名的工具列表（最多 top_k 个）
          best   — (工具, 组内最大余弦)，供上层阈值门控
        """
        if self._examples is None:
            if not self._claim_build():
                return RouteResult(ranked=[], best=None)
            try:
                self._build_index()
            finally:
                self._finish_build()
        if not self._examples:
            return RouteResult(ranked=[], best=None)
        vec = self._client.embed(text)
        if not vec:
            return RouteResult(ranked=[], best=None)

        # 稠密：按工具分组打分（预计算锚点模，单次查询只算一次查询模）
        qnorm = _norm(vec)
        if not qnorm:
            return RouteResult(ranked=[], best=None)
        per_tool: dict[str, list[float]] = {}
        for e in self._examples:
            if not e.norm:
                e.norm = _norm(e.vec)
            per_tool.setdefault(e.tool, []).append(
                _dot(vec, e.vec) / (qnorm * e.norm)
            )
        dense = {t: _group_score(v) for t, v in per_tool.items()}
        best_tool = max(dense, key=dense.get)
        best_score = max(per_tool[best_tool])  # 与旧 best_match 语义一致

        # 稀疏：BM25 词面匹配（归一化到 0~1）
        sparse = self._bm25.scores(_tokenize(text)) if self._bm25 else {}
        mx = max(sparse.values()) if sparse else 0.0
        sparse_norm = {t: (s / mx if mx > 0 else 0.0) for t, s in sparse.items()}

        # 混合排序
        hybrid = {
            t: self._dense_w * dense[t]
            + (1 - self._dense_w) * sparse_norm.get(t, 0.0)
            for t in per_tool
        }
        ranked = sorted(
            (t for t in per_tool if t != "ESCALATE"),
            key=hybrid.get,
            reverse=True,
        )[: self._top_k]
        top1 = ranked[0] if ranked else None
        top2 = ranked[1] if len(ranked) > 1 else None
        return RouteResult(
            ranked=ranked,
            best=(best_tool, best_score),
            top1=top1,
            top1_score=hybrid.get(top1, 0.0) if top1 else 0.0,
            top2_score=hybrid.get(top2, 0.0) if top2 else 0.0,
            margin=(hybrid.get(top1, 0.0) - hybrid.get(top2, 0.0)) if top1 and top2 else 0.0,
        )

    def recall(self, text: str) -> list[str]:
        """兼容旧接口：返回语义排序的工具名列表（最多 top_k 个）。"""
        return self.route(text).ranked

    def best_match(self, text: str) -> tuple[str, float] | None:
        """兼容旧接口：返回 (最相似工具, 组内最大余弦) 或 None。"""
        return self.route(text).best

    # ── 索引构建（惰性 + 本地缓存）───────────────────────────────

    def _claim_build(self) -> bool:
        with self._build_lock:
            if self._examples is not None or self._building:
                return False
            self._building = True
            return True

    def _finish_build(self) -> None:
        with self._build_lock:
            self._building = False

    def _background_build(self) -> None:
        try:
            self._build_index()
        finally:
            self._finish_build()

    def _build_index(self) -> None:
        self._examples = []
        self._bm25 = None
        if not self._client.is_available():
            return
        cached = self._load_cache()
        if cached is not None:
            self._examples = cached
            self._bm25 = self._build_bm25(cached)
            return
        try:
            rows: list[dict] = []
            for p in self._paths:
                if not p.exists():
                    continue
                rows.extend(
                    json.loads(l)
                    for l in open(p, encoding="utf-8")
                    if l.strip()
                )
        except OSError:
            return
        examples: list[_Example] = []
        for c in rows:
            tool, text = c.get("expect"), c.get("input")
            if not tool or not text or tool == "ESCALATE":
                continue
            if c.get("group") == "model":
                continue  # 测试集不进入索引（corpus 只取 rule 组；anchors 全取）
            vec = self._client.embed(text)
            if vec:
                examples.append(_Example(tool=tool, text=text, vec=vec,
                                         norm=_norm(vec)))
        self._examples = examples
        self._bm25 = self._build_bm25(examples)
        self._save_cache(examples)

    def _build_bm25(self, examples: list[_Example]) -> _BM25 | None:
        docs: dict[str, list[str]] = {}
        for e in examples:
            docs.setdefault(e.tool, []).extend(_tokenize(e.text))
        return _BM25(docs) if docs else None

    def _load_cache(self) -> list[_Example] | None:
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if data.get("version") != 4:
            return None
        if data.get("embedding_identity") != getattr(self._client, "cache_identity", None):
            return None
        try:
            for p in self._paths:
                if not p.exists():
                    continue
                # 用完整路径而非 p.name：多份语料的 anchors.jsonl 同名，
                # 只比文件名会互相冒充，导致改了语料仍误命中旧缓存。
                want = {"name": str(p), "mtime": int(p.stat().st_mtime)}
                if want not in data.get("sources", []):
                    return None
            return [
                _Example(tool=e["tool"], text=e["text"], vec=e["vec"],
                         norm=_norm(e["vec"]))
                for e in data.get("examples", [])
                if e.get("tool") != "ESCALATE"
            ]
        except (KeyError, TypeError):
            return None

    def _save_cache(self, examples: list[_Example]) -> None:
        sources: list[dict] = []
        for p in self._paths:
            try:
                sources.append({"name": str(p), "mtime": int(p.stat().st_mtime)})
            except OSError:
                pass
        data = {
            "version": 4,
            "embedding_identity": getattr(self._client, "cache_identity", None),
            "sources": sources,
            "examples": [
                {"tool": e.tool, "text": e.text, "vec": e.vec}
                for e in examples
            ],
        }
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass
