"""Bibit edge-first intent routing package."""

from importlib import import_module

_EXPORTS = {
    "BibitIntentClient": (".client", "BibitIntentClient"),
    "BibitServiceError": (".client", "BibitServiceError"),
    "BM25KnowledgeBase": (".knowledge.knowledge", "BM25KnowledgeBase"),
    "Doc": (".knowledge.knowledge", "Doc"),
    "EdgeDeadlineExceeded": (".execution.cancellation", "EdgeDeadlineExceeded"),
    "EdgeExecutionCancelled": (".execution.cancellation", "EdgeExecutionCancelled"),
    "EdgeExecutionControl": (".execution.cancellation", "EdgeExecutionControl"),
    "EmbeddedBibitPipeline": (".pipeline", "EmbeddedBibitPipeline"),
    "Executor": (".execution.executor", "Executor"),
    "ExecResult": (".execution.system_tools", "ExecResult"),
    "HANDLERS": (".execution.system_tools", "HANDLERS"),
    "LocalModelClient": (".models.local_model", "LocalModelClient"),
    "LocalModelResult": (".models.local_model", "LocalModelResult"),
    "PipelineOutcome": (".pipeline", "PipelineOutcome"),
    "PipelineSettings": (".pipeline", "PipelineSettings"),
    "RouteResult": (".routing.router", "RouteResult"),
    "Router": (".routing.router", "Router"),
    "RuleEngine": (".routing.rule_engine", "RuleEngine"),
    "RuleMatch": (".routing.rule_engine", "RuleMatch"),
    "Stage": (".routing.router", "Stage"),
}


def __getattr__(name: str):
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))

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
