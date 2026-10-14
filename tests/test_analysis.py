"""P0 语言分析层单元测试。

当前只验证零依赖确定性分析器的行为与路由前置规则一致：
  - 多动作
  - 多包目标
  - 上下文依赖
  - 否定标记
"""

from __future__ import annotations

import pytest

from bibit_intent.analysis.deterministic import DeterministicAnalyzer
from bibit_intent.analysis.features import UtteranceAnalysis
from bibit_intent.analysis.synonyms import SynonymResolver
from bibit_intent.models.local_model import _build_system_prompt


@pytest.fixture()
def analyzer() -> DeterministicAnalyzer:
    return DeterministicAnalyzer()


def test_multi_action(analyzer):
    assert analyzer.analyze("打开浏览器并播放三国演义").is_multi_action
    assert analyzer.analyze("查看 CPU 详细信息").is_multi_action is False
    assert analyzer.analyze("播放三国演义").is_multi_action is False
    assert analyzer.analyze("open the browser and play a video").is_multi_action


def test_multi_target(analyzer):
    assert analyzer.analyze("安装hello和fio").is_multi_target
    assert analyzer.analyze("卸载 firefox、chrome").is_multi_target
    assert analyzer.analyze("安装fio").is_multi_target is False
    assert analyzer.analyze("install vim and htop").is_multi_target
    assert analyzer.analyze("remove firefox, chrome, and edge").is_multi_target


def test_context_dependent(analyzer):
    assert analyzer.analyze("打开它").is_context_dependent
    assert analyzer.analyze("别重启").is_context_dependent
    assert analyzer.analyze("打开浏览器").is_context_dependent is False
    assert analyzer.analyze("open it").is_context_dependent
    assert analyzer.analyze("open it").is_context_dependent


def test_negation_tokens(analyzer):
    assert analyzer.analyze("不要关浏览器").has_negation
    assert analyzer.analyze("关浏览器").has_negation is False
    assert analyzer.analyze("帮我不要关浏览器").is_negated_action
    assert analyzer.analyze("关浏览器").is_negated_action is False
    assert analyzer.analyze("do not close the browser").has_negation
    assert analyzer.analyze("please don't restart the computer").is_negated_action


def test_default_result_is_empty():
    result = UtteranceAnalysis()
    assert result.text == ""
    assert result.tokens == ()
    assert result.pos_tags == ()
    assert not result.is_multi_action
    assert not result.is_multi_target
    assert not result.is_context_dependent


def test_synonym_expansion():
    resolver = SynonymResolver()
    assert "音量" in resolver.expand_text("声量调到60")
    assert "浏览器" in resolver.expand_text("打开网页浏览器")
    assert resolver.expand_text("播放三国演义") == "播放三国演义"


def test_local_prompt_structure_hint():
    analysis = UtteranceAnalysis(
        text="帮我不要关浏览器",
        predicates=("关闭",),
        has_negation=True,
        negation_tokens=("不要",),
    )
    prompt = _build_system_prompt("close_app  // 关闭应用", analysis)
    assert "动词=关闭" in prompt
    assert "否定=是" in prompt
