"""Offline bilingual acceptance cases; all handlers/models are test doubles."""
import json
from pathlib import Path
import socket

import pytest

from bibit_intent.analysis import lexical
from bibit_intent.analysis.deterministic import DeterministicAnalyzer
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.executor import Executor
from bibit_intent.routing.complexity import classify_complexity, Complexity
from bibit_intent.routing.intent import decide_local_intent, IntentAction
from bibit_intent.routing.multi_action import find_verbs, find_connectors
from bibit_intent.routing.router import Router, Stage
from bibit_intent.routing.rule_engine import RuleEngine


SIMPLE = [
    '安装 curl', '帮我下载zenow', '打开浏览器', '关闭终端', '查看下载记录',
    '打开安装器', '查看CPU占用', '调节音量到60', '下载 https://example.com/install/and/delete',
    '查看 /tmp/install-and-delete.log', '打开“安装并删除”', '查看USB设备和PCI设备',
    'open Firefox', 'OPEN Firefox', 'please open Firefox', 'download zenow',
    'install curl', 'check cpu details', 'open the download folder', 'set volume to 60',
    'download https://example.com/install/and/delete', 'open "install and delete"',
    'open installation.txt', 'open standard.pdf', '查看 CPU and 内存',
]
COMPLEX = [
    '配置服务然后验证结果', '部署应用并测试接口', '分析日志并评估风险',
    '校对文档然后归档', '把名字dec写入记忆，然后告诉我你用了哪个工具',
    '如果软件没安装就安装', '除非服务已经启动，否则启动服务', '请不要部署服务',
    '下载软件，不要安装', '打开浏览器并frobnicate数据', '为什么需要安装软件',
    '安装curl和wget', '打开它', '先打开终端再查询系统信息',
    'configure the service and verify it', 'deploy the app and test the API',
    'configured the service and verified it', 'downloaded curl then installed it',
    'proofread the document and archive it', 'wrote the report and sent it',
    'if curl is missing install curl', 'unless it is installed install curl',
    'do not download the file', "please don't deploy the service", 'never erase the disk',
    'open the browser then frobnicate data', 'remember dec then tell me which tool you used',
    'install curl and wget', 'OPEN IT', 'why is the service unavailable?',
    '配置 nginx then verify the service', '下载 curl 然后 install it',
    'if 服务没有启动就启动服务', '不要 deploy nginx',
]


@pytest.mark.parametrize('text', SIMPLE)
def test_single_request_not_inflated_by_dictionary(text):
    assert classify_complexity(text).complexity is Complexity.SIMPLE


@pytest.mark.parametrize('text', COMPLEX)
def test_compound_and_unsafe_requests_are_complex(text):
    result = classify_complexity(text)
    assert result.complexity is Complexity.COMPLEX
    assert result.escalation_reason


@pytest.mark.parametrize('text', COMPLEX)
def test_complex_requests_never_execute_or_call_local_model(text):
    registry = ToolRegistry()

    class ForbiddenLocal:
        def is_available(self):
            pytest.fail('complex request reached local model')

    class ForbiddenExecutor(Executor):
        def execute(self, *args, **kwargs):
            pytest.fail('complex request reached executor')

    router = Router(registry, RuleEngine(registry), ForbiddenLocal(), ForbiddenExecutor(registry))
    result = router.handle(text)
    assert result.stage is Stage.CLOUD


@pytest.mark.parametrize(('word', 'lemma'), [
    ('configured', 'configure'), ('configuring', 'configure'), ('configures', 'configure'),
    ('verified', 'verify'), ('verifies', 'verify'), ('verifying', 'verify'),
    ('running', 'run'), ('stopped', 'stop'), ('wrote', 'write'), ('sent', 'send'),
    ('deployed', 'deploy'), ('INSTALLING', 'install'),
])
def test_english_dictionary_morphology(word, lemma):
    _, verbs, exceptions = lexical.dictionaries()
    assert lexical.english_lemma(word, verbs, exceptions) == lemma


def test_no_substring_conjunctions_or_negation():
    assert find_connectors('standard record beforehand') == []
    assert not DeterministicAnalyzer().analyze('open notebook').has_negation
    assert find_verbs('打开开关') == ['打开']
    assert find_verbs('查看下载记录') == ['查看']


def test_dictionary_sources_and_license():
    chinese, verbs, exceptions = lexical.dictionaries()
    assert len(chinese) > 300000
    assert len(verbs) > 10000
    assert exceptions['wrote'] == ['write']
    directory = Path(lexical.__file__).resolve().parents[1] / 'resources/lexicons'
    assert 'Princeton University' in (directory / 'WORDNET-LICENSE.txt').read_text()
    data = json.loads((directory / 'wordnet-verbs.json').read_text())
    assert data['version'] == 'WordNet-3.0'


def test_default_backend_does_not_use_models_or_network(monkeypatch):
    from bibit_intent.analysis import backends

    def forbidden(*args, **kwargs):
        pytest.fail('network or model-backed analyzer invoked')

    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(backends, 'JiebaAnalyzer', forbidden)
    monkeypatch.setattr(backends, 'SpacyAnalyzer', forbidden)
    analyzer = backends.build_analyzer()
    result = analyzer.analyze('校对文档然后归档')
    assert result.is_multi_action
    assert len(result.tokens) == len(result.pos_tags)


@pytest.mark.parametrize('error', [FileNotFoundError, ValueError, KeyError])
def test_missing_dictionary_fails_closed_before_rule_execution(monkeypatch, error):
    def unavailable():
        raise error('missing or invalid dictionary')

    monkeypatch.setattr(lexical, 'dictionaries', unavailable)
    registry = ToolRegistry()
    result = decide_local_intent('set volume to 60', RuleEngine(registry), registry)
    assert result.action is IntentAction.ESCALATE
    assert result.reason == 'lexicon_unavailable'


def test_oversized_input_is_bounded():
    result = classify_complexity('打开浏览器' * 2000)
    assert result.escalation_reason == 'input_too_long'


def test_unrecognized_input_is_not_assumed_simple():
    for text in ('', 'frobnicate glorp', '🫠🫠'):
        assert classify_complexity(text).escalation_reason == 'insufficient_evidence'


def test_known_local_query_preserves_rule_execution():
    from bibit_intent.execution.skill_loader import load_skills
    from bibit_intent.resources import resource_path
    registry = ToolRegistry()
    load_skills(resource_path('skills'), registry, {})
    for text in ('set volume to 60', '把音量调到60', '查看系统版本和内核版本', '查看USB设备和PCI设备'):
        plan = decide_local_intent(text, RuleEngine(registry), registry)
        assert plan.action is IntentAction.RULE_CALL, text


def test_question_words_in_literals_not_counted():
    assert lexical.question_word_count('打开 "what why 什么 为什么"') == 0
    assert lexical.question_word_count('为什么失败，如何修复？ what failed and why?') == 4


@pytest.mark.parametrize('text', [
    '下载 https://example.com/app.tar.gz,然后安装',
    'download https://example.com/app.zip;then install curl',
    '读取 /tmp/a.txt;然后删除文件',
])
def test_literal_mask_does_not_swallow_following_action(text):
    assert classify_complexity(text).escalation_reason == 'multi_intent'


@pytest.mark.parametrize('reason', [
    'multi_intent', 'multi_target', 'conditional_intent', 'negated_action',
    'complex_intent', 'lexicon_unavailable', 'input_too_long', 'insufficient_evidence',
])
def test_offline_fallback_cannot_bypass_structural_guard(monkeypatch, reason):
    from bibit_intent.routing import router as router_module
    import time

    class ForbiddenExecutor(Executor):
        def execute(self, *args, **kwargs):
            pytest.fail('offline fallback bypassed complexity guard')

    monkeypatch.setattr(router_module, 'is_network_fault_diagnosis', lambda text: True)
    monkeypatch.setattr(router_module, '_cloud_reachable', lambda: False)
    registry = ToolRegistry()
    router = Router(registry, RuleEngine(registry), None, ForbiddenExecutor(registry))
    result = router._cloud_fallback('如果无法上网，就重启系统', reason, time.monotonic())
    assert result.stage is Stage.CLOUD
    assert result.escalate_reason == reason
