"""Runtime-neutral facade for the embedded Bibit edge pipeline."""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
import os
import json
from pathlib import Path
from threading import BoundedSemaphore, Lock
from typing import Any

from .execution.cancellation import cancellation_scope, raise_if_cancelled
from .resources import resource_path
from .routing.embedding import EmbeddingRetriever
from .execution.executor import Executor
from .knowledge.knowledge import BM25KnowledgeBase
from .models.backends import local_client, embedding_client, validate_backend
from .routing.question_detector import QuestionDetector
from .routing.pkg_resolver import resolve_install_pkg
from .core.registry import ToolRegistry
from .routing.router import Router, Stage
from .routing.soft_gate import SoftGateConfig
from .routing.complexity import ComplexityThresholds
from .routing.rule_engine import RuleEngine
from .core.session import SessionState
from .core.language import detect_language
from .core.localization import localize_reply
from .execution.skill_loader import load_skills
from .execution.system_tools import HANDLERS
from .execution.md_format import render_detail


_DEFAULT_CONTEXT_LIMIT = 512
_DEFAULT_LOCAL_TIMEOUT_SECONDS = 20
_DEFAULT_MAX_SESSIONS = 256


@dataclass(frozen=True, slots=True)
class PipelineOutcome:
    handled: bool
    reply: str = ""
    route: str = ""
    reason: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PipelineSettings:
    context_limit: int = _DEFAULT_CONTEXT_LIMIT
    local_timeout_seconds: int = _DEFAULT_LOCAL_TIMEOUT_SECONDS
    max_sessions: int = _DEFAULT_MAX_SESSIONS
    soft_gate_mode: str = "shadow"
    soft_gate_post_mode: str = "shadow"
    soft_gate_logprob_threshold: float = -2.2
    soft_gate_embed_top1_threshold: float = 0.72
    soft_gate_embed_margin_threshold: float = 0.05
    soft_gate_n_probs: int = 10
    inference_backend: str = "ai_gateway"
    gateway_url: str = "http://127.0.0.1:18790"
    gateway_llm_model: str = "glm-edge-1.5b-chat-q4_0"
    gateway_embed_model: str = "qwen3-embedding-0.6b-q4_0"
    gateway_api_key: str = field(default="", repr=False)
    gateway_embed_revision: str = "1"
    complexity_thresholds: ComplexityThresholds = field(default_factory=ComplexityThresholds)

    def __post_init__(self) -> None:
        validate_backend(self)

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> "PipelineSettings":
        values = os.environ if environ is None else environ

        def integer(name: str, default: int) -> int:
            raw = str(values.get(name, default)).strip()
            try:
                value = int(raw)
            except ValueError as exc:
                raise ValueError(f"{name} must be an integer") from exc
            if value < 1:
                raise ValueError(f"{name} must be positive")
            return value

        def number(name: str, default: float) -> float:
            raw = str(values.get(name, default)).strip()
            try:
                return float(raw)
            except ValueError as exc:
                raise ValueError(f"{name} must be a number") from exc

        return cls(
            complexity_thresholds=ComplexityThresholds.from_environment(values),
            inference_backend=values.get("BIBIT_INTENT_INFERENCE_BACKEND", "ai_gateway"),
            gateway_url=values.get("BIBIT_INTENT_GATEWAY_URL", "http://127.0.0.1:18790"),
            gateway_llm_model=values.get("BIBIT_INTENT_GATEWAY_LLM_MODEL", "glm-edge-1.5b-chat-q4_0"),
            gateway_embed_model=values.get("BIBIT_INTENT_GATEWAY_EMBED_MODEL", "qwen3-embedding-0.6b-q4_0"),
            gateway_api_key=values.get("BIBIT_INTENT_GATEWAY_API_KEY", ""),
            gateway_embed_revision=values.get("BIBIT_INTENT_GATEWAY_EMBED_REVISION", "1"),
            context_limit=integer(
                "BIBIT_INTENT_CONTEXT_LIMIT", _DEFAULT_CONTEXT_LIMIT
            ),
            local_timeout_seconds=integer(
                "BIBIT_INTENT_LOCAL_TIMEOUT_SECONDS",
                _DEFAULT_LOCAL_TIMEOUT_SECONDS,
            ),
            max_sessions=integer("BIBIT_INTENT_MAX_SESSIONS", _DEFAULT_MAX_SESSIONS),
            soft_gate_mode=str(
                values.get("BIBIT_INTENT_SOFT_GATE_MODE", "shadow")
            ).strip().lower(),
            soft_gate_post_mode=str(
                values.get("BIBIT_INTENT_SOFT_GATE_POST_MODE", "shadow")
            ).strip().lower(),
            soft_gate_logprob_threshold=number(
                "BIBIT_INTENT_SOFT_GATE_LOGPROB_THRESHOLD", -2.2
            ),
            soft_gate_embed_top1_threshold=number(
                "BIBIT_INTENT_SOFT_GATE_EMBED_TOP1_THRESHOLD", 0.72
            ),
            soft_gate_embed_margin_threshold=number(
                "BIBIT_INTENT_SOFT_GATE_EMBED_MARGIN_THRESHOLD", 0.05
            ),
            soft_gate_n_probs=integer(
                "BIBIT_INTENT_SOFT_GATE_N_PROBS", 10
            ),
        )


def _readonly_detail(exec_result: Any | None) -> dict | None:
    if getattr(exec_result, "detail_format", "markdown") == "text":
        return None
    try:
        payload = json.loads(getattr(exec_result, "detail", "") or "")
    except (ValueError, TypeError):
        return None
    if isinstance(payload, dict) and payload.get("read_only") is True and type(payload.get("ok")) is bool:
        return payload
    return None


def _format_reply(exec_result: Any | None, language: str = "") -> str:
    if exec_result is None:
        return ""
    message = localize_reply(str(getattr(exec_result, "message", "") or "").strip(), language)
    detail_format = getattr(exec_result, "detail_format", "markdown")
    detail = render_detail(str(getattr(exec_result, "detail", "") or ""), detail_format)
    if message and _readonly_detail(exec_result) is not None:
        return message
    separator = "\n\n" if detail_format == "text" else "\n"
    return separator.join(part for part in (message, detail) if part)


@dataclass(slots=True)
class _SessionEntry:
    state: SessionState = field(default_factory=SessionState)
    lock: Lock = field(default_factory=Lock)
    in_flight: int = 0


class EmbeddedBibitPipeline:
    """Run Bibit locally while keeping Runtime cloud fallback outside the plugin."""

    def __init__(
        self,
        router: Any,
        max_sessions: int = 256,
        registered_tool_count: int = 0,
        embedding_cache_path: Path | None = None,
        question_embedding_cache_path: Path | None = None,
    ) -> None:
        if max_sessions < 1:
            raise ValueError("max_sessions must be positive")
        self._router = router
        self._max_sessions = int(max_sessions)
        self._registered_tool_count = int(registered_tool_count)
        self._embedding_cache_path = embedding_cache_path
        self._question_embedding_cache_path = question_embedding_cache_path
        self._sessions: OrderedDict[str, _SessionEntry] = OrderedDict()
        self._sessions_lock = Lock()
        self._session_slots = BoundedSemaphore(self._max_sessions)

    @property
    def active_session_count(self) -> int:
        with self._sessions_lock:
            return len(self._sessions)

    def stats(self) -> dict[str, object]:
        """供 /metrics 端点读取的运行时统计，不改变路由行为。"""
        from .core.dependency_plan import dependency_metrics
        from .core.appcli_audit import appcli_metrics

        s = self._router.stats
        return {
            "total_requests": s.total_requests,
            "model_requests": s.model_requests,
            "model_calls": s.model_calls,
            "soft_gate_pre_shadow": s.soft_gate_pre_shadow,
            "soft_gate_post_shadow": s.soft_gate_post_shadow,
            "soft_gate_pre_on": s.soft_gate_pre_on,
            "soft_gate_post_on": s.soft_gate_post_on,
            "soft_gate_shadow_total": s.soft_gate_shadow_total,
            "hallucinated_events": s.hallucinated_events,
            "guard_blocked": s.guard_blocked,
            "active_sessions": self.active_session_count,
            "registered_tools": self._registered_tool_count,
            "dependency_plan": dependency_metrics(),
            "appcli": appcli_metrics(),
        }

    @property
    def registered_tool_count(self) -> int:
        return self._registered_tool_count

    @property
    def embedding_cache_path(self) -> Path | None:
        return self._embedding_cache_path

    @property
    def question_embedding_cache_path(self) -> Path | None:
        return self._question_embedding_cache_path

    @classmethod
    def from_package(
        cls,
        *,
        data_root: str | Path,
        settings: PipelineSettings | None = None,
    ) -> "EmbeddedBibitPipeline":
        private_data_root = Path(data_root)
        config = settings or PipelineSettings.from_environment()
        resources = resource_path()
        registry = ToolRegistry(resource_path("tools.yaml"))
        handlers = dict(HANDLERS)
        load_skills(resource_path("skills"), registry, handlers)

        rules = RuleEngine(registry)
        local = local_client(config)
        embed = EmbeddingRetriever(
            embedding_client(config),
            [
                resource_path("route-data", "system_control.jsonl"),
                resource_path("route-data", "ubuntu_cmd.jsonl"),
                resource_path("route-data", "system_control-en.jsonl"),
                resource_path("route-data", "ubuntu_cmd-en.jsonl"),
                resource_path("route-data", "ubuntu_admin-en.jsonl"),
            ],
            anchor_paths=[resource_path("route-data", "ubuntu_cmd-anchors.jsonl"), resource_path("route-data", "ubuntu_cmd-en-anchors.jsonl"), resource_path("route-data", "system_control-en-anchors.jsonl")],
            cache_path=private_data_root / "embedding-index.json",
        )
        embed.start_background_build()
        question_embed = EmbeddingRetriever(
            embedding_client(config),
            [resource_path("question_corpus.jsonl"), resource_path("question_corpus.en.jsonl")],
            cache_path=private_data_root / "question-embedding-index.json",
        )
        question_embed.start_background_build()
        rules.attach_question_detector(QuestionDetector(question_embed))
        knowledge = BM25KnowledgeBase()
        knowledge.load_dir(resource_path("kb"))
        executor = Executor(
            registry,
            confirm_callback=lambda _action: True,
            handlers=handlers,
        )
        router = Router(
            registry,
            rules,
            local,
            executor,
            kb=knowledge,
            cloud_client=None,
            embed=embed,
            pkg_resolver=resolve_install_pkg,
            complexity_thresholds=config.complexity_thresholds,
            soft_gate_config=SoftGateConfig(
                mode=config.soft_gate_mode,
                post_mode=config.soft_gate_post_mode,
                logprob_threshold=config.soft_gate_logprob_threshold,
                embed_top1_threshold=config.soft_gate_embed_top1_threshold,
                embed_margin_threshold=config.soft_gate_embed_margin_threshold,
                n_probs=config.soft_gate_n_probs,
            ),
        )
        return cls(
            router,
            max_sessions=config.max_sessions,
            registered_tool_count=len(registry.all()),
            embedding_cache_path=embed.cache_path,
            question_embedding_cache_path=question_embed.cache_path,
        )

    def handle(
        self,
        text: str,
        session_id: str,
        cancel_event: Any | None = None,
    ) -> PipelineOutcome:
        with cancellation_scope(cancel_event):
            while not self._session_slots.acquire(timeout=0.05):
                raise_if_cancelled()
            try:
                return self._handle(text, session_id)
            finally:
                self._session_slots.release()

    def _handle(self, text: str, session_id: str) -> PipelineOutcome:
        normalized_session_id = str(session_id).strip()
        if not normalized_session_id:
            raise ValueError("session_id must not be empty")

        with self._sessions_lock:
            entry = self._sessions.get(normalized_session_id)
            if entry is None:
                entry = _SessionEntry()
                self._sessions[normalized_session_id] = entry
            else:
                self._sessions.move_to_end(normalized_session_id)
            entry.in_flight += 1
            self._evict_inactive_sessions_locked()

        entry_acquired = False
        try:
            while not entry.lock.acquire(timeout=0.05):
                raise_if_cancelled()
            entry_acquired = True
            from .file_results import SUMMARY_PREFIX, summarize
            if str(text).startswith(SUMMARY_PREFIX):
                summary = summarize(str(text)[len(SUMMARY_PREFIX):], getattr(self._router, "_local", None))
                return PipelineOutcome(handled=True, reply=summary.message, route="local", reason="file_summary",
                                       metadata={"route": "local", "tool": "file_summary", "ok": summary.ok})
            result = self._router.handle(str(text), session=entry.state)
        finally:
            with self._sessions_lock:
                entry.in_flight -= 1
                current = self._sessions.get(normalized_session_id)
                if current is entry:
                    if entry.state.pending is None and entry.in_flight == 0:
                        self._sessions.pop(normalized_session_id, None)
                    else:
                        self._sessions.move_to_end(normalized_session_id)
                self._evict_inactive_sessions_locked()
            if entry_acquired:
                entry.lock.release()

        route = result.stage.value
        reason = str(result.escalate_reason or "")
        metadata: dict[str, object] = {
            "route": route,
            "latency_ms": round(float(result.latency_ms), 3),
        }
        if result.tool_name:
            metadata["tool"] = result.tool_name
        execution = result.exec_result
        if execution is not None:
            if getattr(execution, "unverified", False):
                metadata["task_status"] = "unverified"
            elif execution.confirmation is not None or execution.confirm:
                metadata["task_status"] = "awaiting_confirmation"
            elif execution.need_param is not None or execution.choices:
                metadata["task_status"] = "awaiting_input"
            else:
                metadata["task_status"] = "succeeded" if execution.ok else "failed"
        confirmation = getattr(result.exec_result, "confirmation", None)
        if confirmation is not None:
            metadata["time_confirmation"] = confirmation
        structured = _readonly_detail(result.exec_result)
        if structured is not None:
            metadata["result"] = structured
        if reason:
            metadata["reason"] = reason

        if result.stage is Stage.CLOUD:
            return PipelineOutcome(
                handled=False,
                route=route,
                reason=reason,
                metadata=metadata,
            )
        return PipelineOutcome(
            handled=True,
            reply=_format_reply(result.exec_result, language=detect_language(str(text))),
            route=route,
            reason=reason,
            metadata=metadata,
        )

    def clear_session(self, session_id: str) -> bool:
        """Remove an idle session and its pending slot/confirm state.

        Returns ``True`` when the session existed and was removed, and
        ``False`` when it did not exist or currently has an in-flight request.
        """
        normalized_session_id = str(session_id).strip()
        if not normalized_session_id:
            return False
        with self._sessions_lock:
            entry = self._sessions.get(normalized_session_id)
            if entry is None:
                return False
            if entry.in_flight > 0:
                return False
            self._sessions.pop(normalized_session_id, None)
            return True

    def _evict_inactive_sessions_locked(self) -> None:
        while len(self._sessions) > self._max_sessions:
            victim = next(
                (
                    session_id
                    for session_id, entry in self._sessions.items()
                    if entry.in_flight == 0
                ),
                None,
            )
            if victim is None:
                return
            self._sessions.pop(victim, None)
