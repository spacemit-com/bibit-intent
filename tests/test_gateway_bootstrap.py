from pathlib import Path
from unittest.mock import Mock

import pytest

from urllib.error import URLError

from bibit_intent.models import gateway_bootstrap as bootstrap


def _transport(rows):
    class Transport:
        base_url = 'http://127.0.0.1:18790'
        calls = []

        def __init__(self):
            self.rows = list(rows)

        def request(self, path, payload=None, method=None, timeout=None):
            self.calls.append((path, payload, method))
            if path.endswith('/models'):
                return list(self.rows)
            if path.endswith('/download'):
                self.rows[0]['status'] = 'downloaded'
                return {'status': 'downloading'}
            if path.endswith('/load'):
                self.rows[0]['status'] = 'loaded'
                return {'status': 'loaded'}
            raise AssertionError(path)

    return Transport()


def test_ensure_downloaded_skips_when_already_downloaded():
    transport = _transport([{'id': 'glm-edge-1.5b-chat-q4_0', 'source_type': 'local_url',
                             'status': 'downloaded'}])
    bootstrap.ensure_downloaded(transport, 'llm', 'glm-edge-1.5b-chat-q4_0')
    assert not any(path.endswith('/download') for path, _, _ in transport.calls)


def test_ensure_downloaded_requests_gateway_download():
    transport = _transport([{'id': 'qwen3-embedding-0.6b-q4_0', 'source_type': 'local_url',
                             'status': 'available'}])
    bootstrap.ensure_downloaded(transport, 'embed', 'qwen3-embedding-0.6b-q4_0')
    assert ('/v1/embed/models/qwen3-embedding-0.6b-q4_0/download', None, 'POST') in transport.calls


def test_ensure_downloaded_rejects_non_local_url():
    transport = _transport([{'id': 'glm-edge-1.5b-chat-q4_0', 'source_type': 'local_path',
                             'status': 'downloaded'}])
    with pytest.raises(ValueError, match='local_url'):
        bootstrap.ensure_downloaded(transport, 'llm', 'glm-edge-1.5b-chat-q4_0')


def test_ensure_downloaded_rejects_missing_preset():
    transport = _transport([])
    with pytest.raises(ValueError, match='not found'):
        bootstrap.ensure_downloaded(transport, 'llm', 'glm-edge-1.5b-chat-q4_0')


def test_ensure_downloaded_raises_on_error():
    transport = _transport([{'id': 'glm-edge-1.5b-chat-q4_0', 'source_type': 'local_url',
                             'status': 'available'}])
    original = transport.request

    def failing(path, payload=None, method=None, timeout=None):
        if path.endswith('/models'):
            return [{'id': 'glm-edge-1.5b-chat-q4_0', 'source_type': 'local_url',
                     'status': 'error'}]
        return original(path, payload, method, timeout)

    transport.request = failing
    with pytest.raises(RuntimeError, match='failed'):
        bootstrap.ensure_downloaded(transport, 'llm', 'glm-edge-1.5b-chat-q4_0')


def test_ensure_downloaded_re_requests_when_gateway_resets_to_available(monkeypatch):
    states = ['available', 'downloading', 'available', 'downloading', 'downloaded']

    class Transport:
        base_url = 'http://127.0.0.1:18790'
        calls = []

        def request(self, path, payload=None, method=None, timeout=None):
            self.calls.append((path, method))
            if path.endswith('/models'):
                return [{'id': 'glm-edge-1.5b-chat-q4_0', 'source_type': 'local_url',
                         'status': states.pop(0)}]
            if path.endswith('/download'):
                return {'status': 'downloading'}
            raise AssertionError(path)

    transport = Transport()
    monkeypatch.setattr(bootstrap.time, 'sleep', lambda _: None)
    monkeypatch.setattr(bootstrap.time, 'monotonic', lambda: 0.0)
    bootstrap.ensure_downloaded(transport, 'llm', 'glm-edge-1.5b-chat-q4_0')
    downloads = [path for path, method in transport.calls
                 if path.endswith('/download') and method == 'POST']
    assert len(downloads) == 2


def test_ensure_downloaded_retries_transient_connection_errors(monkeypatch):
    ticks = [0]

    class Transport:
        base_url = 'http://127.0.0.1:18790'
        calls = 0

        def request(self, path, payload=None, method=None, timeout=None):
            if path.endswith('/models'):
                self.calls += 1
                if self.calls == 1:
                    return [{'id': 'glm-edge-1.5b-chat-q4_0', 'source_type': 'local_url',
                             'status': 'available'}]
                if self.calls < 4:
                    raise URLError('connection refused')
                return [{'id': 'glm-edge-1.5b-chat-q4_0', 'source_type': 'local_url',
                         'status': 'downloaded'}]
            if path.endswith('/download'):
                return {'status': 'downloading'}
            raise AssertionError(path)

    def fake_monotonic():
        ticks[0] += 1
        return ticks[0]

    transport = Transport()
    monkeypatch.setattr(bootstrap.time, 'sleep', lambda _: None)
    monkeypatch.setattr(bootstrap.time, 'monotonic', fake_monotonic)
    bootstrap.ensure_downloaded(transport, 'llm', 'glm-edge-1.5b-chat-q4_0')
    assert transport.calls == 4

def test_bootstrap_uses_only_model_management_api(monkeypatch):
    class Transport:
        base_url = 'http://127.0.0.1:18790'

        def request(self, path, payload=None, method=None, timeout=None):
            if path in {'/v1/llm/models', '/v1/embed/models'}:
                return [{'id': 'glm-edge-1.5b-chat-q4_0', 'source_type': 'local_url',
                         'status': 'downloaded'},
                        {'id': 'qwen3-embedding-0.6b-q4_0', 'source_type': 'local_url',
                         'status': 'downloaded'}]
            if path in {'/v1/llm/models/load', '/v1/embed/models/load'}:
                assert payload['model'] in {'glm-edge-1.5b-chat-q4_0', 'qwen3-embedding-0.6b-q4_0'}
                if payload['model'] == 'qwen3-embedding-0.6b-q4_0':
                    assert '--cache-ram' in payload['extra_args']
                return {}
            raise AssertionError(path)

    monkeypatch.setattr(bootstrap, 'GatewayTransport', lambda *a, **kw: Transport())
    monkeypatch.setattr(bootstrap, 'migrate_models', Mock())
    monkeypatch.setattr(Path, 'read_text', lambda *a, **kw: 'MemAvailable: 8388608 kB\n')
    monkeypatch.setattr(bootstrap, 'wait_for_gateway', Mock())
    bootstrap.bootstrap()


def test_package_starts_bootstrap_without_blocking_bibit():
    root = Path(__file__).resolve().parents[1]
    unit = (root / 'deploy/bibit-intent.service').read_text()
    assert 'Wants=bibit-intent-models.service' in unit
    assert 'After=bibit-intent-models.service' not in unit
    bootstrap_unit = (root / 'deploy/bibit-intent-models.service').read_text()
    assert 'Restart=on-failure' in bootstrap_unit
    assert 'gateway_bootstrap' in bootstrap_unit
    control = (root / 'debian/control').read_text()
    assert 'spacemit-ai-gateway,' in control
    assert 'bibit-ai-gateway-native-v1' not in control
    rules = (root / 'debian/rules').read_text()
    assert 'etc/bibit-intent/inference.env' in rules
    assert 'bibit-intent-models.service' in rules
