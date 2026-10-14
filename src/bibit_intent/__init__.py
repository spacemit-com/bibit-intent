"""Bibit edge-first intent routing package."""

from .client import BibitIntentClient, BibitServiceError
from .execution.cancellation import (
    EdgeDeadlineExceeded,
    EdgeExecutionCancelled,
    EdgeExecutionControl,
)
from .execution.executor import Executor
from .execution.system_tools import ExecResult, HANDLERS
from .knowledge.knowledge import BM25KnowledgeBase, Doc
from .models.local_model import LocalModelClient, LocalModelResult
from .pipeline import EmbeddedBibitPipeline, PipelineOutcome, PipelineSettings
from .routing.router import RouteResult, Router, Stage
from .routing.rule_engine import RuleEngine, RuleMatch

__all__ = [
    "BibitIntentClient",
    "BibitServiceError",
    "BM25KnowledgeBase",
    "Doc",
    "EdgeDeadlineExceeded",
    "EdgeExecutionCancelled",
    "EdgeExecutionControl",
    "EmbeddedBibitPipeline",
    "Executor",
    "ExecResult",
    "HANDLERS",
    "LocalModelClient",
    "LocalModelResult",
    "PipelineOutcome",
    "PipelineSettings",
    "RouteResult",
    "Router",
    "RuleEngine",
    "RuleMatch",
    "Stage",
]
