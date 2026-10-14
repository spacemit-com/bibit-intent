import importlib.util
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest

spec = importlib.util.spec_from_file_location(
    'gateway_readiness', Path(__file__).resolve().parents[1]
    / 'src/bibit_intent/models/gateway_readiness.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_retries_connection_refused(monkeypatch):
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    transport = Mock()
    transport.request.side_effect = [URLError('refused'), [], []]
    module.wait_for_gateway(transport)
    assert transport.request.call_count == 3
    assert transport.request.call_args.args == ('/v1/embed/models',)


def test_timeout_is_bounded(monkeypatch):
    monkeypatch.setattr(module.time, 'monotonic', Mock(side_effect=[0, 0, 31]))
    transport = Mock()
    transport.request.side_effect = URLError('refused')
    with pytest.raises(TimeoutError, match='did not become ready'):
        module.wait_for_gateway(transport)
    assert transport.request.call_count == 1


@pytest.mark.parametrize('code', [401, 403, 404])
def test_permanent_http_error_not_retried(code):
    transport = Mock()
    transport.request.side_effect = HTTPError('url', code, 'error', {}, None)
    with pytest.raises(HTTPError):
        module.wait_for_gateway(transport)
    assert transport.request.call_count == 1


def test_unavailable_retried(monkeypatch):
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    transport = Mock()
    transport.request.side_effect = [HTTPError('url', 503, 'starting', {}, None), [], []]
    module.wait_for_gateway(transport)
    assert transport.request.call_count == 3
