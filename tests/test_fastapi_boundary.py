import asyncio
import threading

import httpx
import pytest

from bibit_intent.pipeline import PipelineOutcome
from bibit_intent.server import BibitIntentService


class Pipeline:
    registered_tool_count = 1

    def handle(self, prompt, session_id, cancel_event):
        if prompt == 'denied':
            raise PermissionError('private policy detail')
        if prompt == 'partial':
            cancel_event.execution_started.set()
            raise RuntimeError('private operation detail')
        return PipelineOutcome(handled=True, reply='done', route='rule')

    def stats(self):
        return {}


@pytest.mark.asyncio
async def test_cancel_arriving_before_post_prevents_late_execution():
    class MustNotRun(Pipeline):
        def handle(self, *args, **kwargs):
            pytest.fail('cancelled submission executed')

    service = BibitIntentService(MustNotRun())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service.create_app()), base_url='http://test') as client:
        await client.delete('/v1/runs/late-request')
        result = await client.post('/v1/runs', json={'prompt': 'execute', 'session_id': 's', 'run_id': 'late-request'})
        assert result.status_code == 409
        assert result.json()['error']['code'] == 'run_conflict'


@pytest.mark.asyncio
async def test_fastapi_documents_runs_and_preserves_http_contract():
    app = BibitIntentService(Pipeline()).create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        schema = await client.get('/openapi.json')
        assert schema.status_code == 200
        assert '/v1/runs' in schema.json()['paths']
        result = await client.post('/v1/runs', json={'prompt': 'hello', 'session_id': 's'})
        assert result.status_code == 200
        assert result.json()['reply'] == 'done'
        assert (await client.get('/healthz')).json()['registered_tools'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('prompt,reason', [('denied', 'permission_denied'), ('partial', 'edge_execution_uncertain')])
async def test_unsafe_fallbacks_return_handled_results(prompt, reason):
    app = BibitIntentService(Pipeline()).create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/v1/runs', json={'prompt': prompt, 'session_id': 's'})
    body = response.json()
    assert body['handled'] is True
    assert body['reason'] == reason
    assert body['reply']
    assert 'private' not in response.text


@pytest.mark.asyncio
async def test_client_known_run_id_cancels_inflight_execution():
    started = threading.Event()
    stopped = threading.Event()

    class WaitingPipeline(Pipeline):
        def handle(self, prompt, session_id, cancel_event):
            cancel_event.execution_started.set()
            started.set()
            assert cancel_event.wait(3)
            stopped.set()
            raise RuntimeError('cancelled')

    service = BibitIntentService(WaitingPipeline())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service.create_app()), base_url='http://test') as client:
        task = asyncio.create_task(client.post('/v1/runs', json={'prompt': 'wait', 'session_id': 's', 'run_id': 'known-run'}))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            duplicate = await client.post('/v1/runs', json={'prompt': 'wait', 'session_id': 's', 'run_id': 'known-run'})
            assert duplicate.status_code == 409
            assert (await client.delete('/v1/runs/known-run')).status_code == 204
            body = (await task).json()
            assert stopped.is_set()
            assert body['handled'] is True
            assert body['run_id'] == 'known-run'
        finally:
            for control in service._runs.values():
                control.set()
            await task
