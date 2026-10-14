"""Compatibility entry points for the supported model-free analyzer."""
from bibit_intent.analysis.deterministic import DeterministicAnalyzer


class JiebaAnalyzer(DeterministicAnalyzer):
    """Legacy name; now uses static dictionaries and preserves all safety flags."""


class SpacyAnalyzer:
    """Retired model-backed adapter: fail explicitly instead of loading a model."""

    def __init__(self, model: str = "zh_core_web_sm"):
        raise ValueError("Model-backed analysis is unsupported; use DeterministicAnalyzer")


def build_analyzer() -> DeterministicAnalyzer:
    return DeterministicAnalyzer()
