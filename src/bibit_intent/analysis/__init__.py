"""共享离线词典、结构规则和路由特征；默认分析器不调用训练模型。"""

from bibit_intent.analysis.base import Analyzer
from bibit_intent.analysis.features import UtteranceAnalysis
from bibit_intent.analysis.synonyms import SynonymResolver

__all__ = [
    "Analyzer",
    "DeterministicAnalyzer",
    "JiebaAnalyzer",
    "SynonymResolver",
    "UtteranceAnalysis",
]


def __getattr__(name):
    if name == "JiebaAnalyzer":
        from bibit_intent.analysis.backends import JiebaAnalyzer
        return JiebaAnalyzer
    if name == "DeterministicAnalyzer":
        from bibit_intent.analysis.deterministic import DeterministicAnalyzer
        return DeterministicAnalyzer
    raise AttributeError(name)
