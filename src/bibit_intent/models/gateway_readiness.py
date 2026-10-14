"""Bounded readiness checks for Gateway startup, without loading a model."""
import time
from urllib.error import HTTPError, URLError


def wait_for_gateway(transport, timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        try:
            for domain in ('llm', 'embed'):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Gateway model API readiness timed out')
                rows = transport.request(f'/v1/{domain}/models', timeout=min(3, remaining))
                if not isinstance(rows, list):
                    raise ValueError(f'Invalid Gateway {domain} model list')
            return
        except HTTPError as exc:
            if exc.code not in (502, 503, 504):
                raise
            error = exc
        except (URLError, ConnectionError, TimeoutError) as exc:
            error = exc
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Gateway model APIs did not become ready; retry initialization later') from error
        time.sleep(min(1, remaining))
