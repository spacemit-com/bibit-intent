from __future__ import annotations

import json

import httpx

from bibit_intent.client import BibitIntentClient, BibitServiceError
from bibit_intent.execution.cancellation import EdgeExecutionControl
from bibit_intent.pipeline import PipelineOutcome
from bibit_intent.server import BibitIntentService


class FakePipeline:
    def __init__(self) -> None:
        self.active_session_count = 0
        self.registered_tool_count = 3
        self.cleared: list[str] = []

    def handle(self, text: str, session_id: str, cancel_event=None) -> PipelineOutcome:
        if text == "boom":
            raise RuntimeError("boom")
        return PipelineOutcome(
            handled=True,
            reply=f"ok:{text}",
            route="rule",
            reason="",
            metadata={"tool": "fake"},
        )

    def clear_session(self, session_id: str) -> bool:
        if session_id == "missing":
            return False
        self.cleared.append(session_id)
        return True


def _mock_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if request.method == "POST" and path == "/v1/runs":
        payload = json.loads(request.content)
        prompt = payload.get("prompt")
        if not prompt:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": "invalid_request",
                        "message": "prompt is required",
                    }
                },
            )
        if prompt == "boom":
            return httpx.Response(
                200,
                json={
                    "run_id": "r",
                    "handled": False,
                    "reply": "",
                    "route": "cloud",
                    "reason": "edge_error",
                    "metadata": {"error_type": "RuntimeError"},
                },
            )
        return httpx.Response(
            200,
            json={
                "run_id": "r",
                "handled": True,
                "reply": f"ok:{prompt}",
                "route": "rule",
                "reason": "",
                "metadata": {"tool": "fake"},
            },
        )
    if request.method == "DELETE" and path.startswith("/v1/runs/"):
        return httpx.Response(204)
    if request.method == "DELETE" and path.startswith("/v1/sessions/"):
        return httpx.Response(204)
    if request.method == "GET" and path == "/healthz":
        return httpx.Response(
            200, json={"status": "ok", "registered_tools": 3}
        )
    return httpx.Response(404)


def _client() -> BibitIntentClient:
    return BibitIntentClient(
        base_url="http://testserver",
        transport=httpx.MockTransport(_mock_handler),
    )


def test_client_run_roundtrip() -> None:
    outcome = _client().run("打开文件", "s1")
    assert outcome.handled is True
    assert outcome.reply == "ok:打开文件"
    assert outcome.route == "rule"
    assert outcome.metadata.get("tool") == "fake"


def test_client_run_falls_back_to_cloud_on_error() -> None:
    outcome = _client().run("boom", "s1")
    assert outcome.handled is False
    assert outcome.route == "cloud"
    assert outcome.reason == "edge_error"
    assert outcome.metadata.get("error_type") == "RuntimeError"


def test_client_run_requires_prompt() -> None:
    try:
        _client().run("", "s1")
    except BibitServiceError as exc:
        assert "prompt is required" in str(exc)
    else:
        raise AssertionError("expected BibitServiceError")


def test_client_health_and_session_cancel() -> None:
    client = _client()
    assert client.cancel("r") is True
    assert client.clear_session("s1") is True
    assert client.health()["status"] == "ok"


def test_service_run_sync_ok() -> None:
    service = BibitIntentService(FakePipeline())
    outcome = service.run_sync("打开文件", "s1", EdgeExecutionControl())
    assert outcome.handled is True
    assert outcome.reply == "ok:打开文件"


def test_service_run_sync_error() -> None:
    service = BibitIntentService(FakePipeline())
    outcome = service.run_sync("boom", "s1", EdgeExecutionControl())
    assert outcome.handled is False
    assert outcome.route == "cloud"
    assert outcome.reason == "edge_error"
    assert outcome.metadata.get("error_type") == "RuntimeError"


def test_service_cancel_register() -> None:
    service = BibitIntentService(FakePipeline())
    control = EdgeExecutionControl()
    service.register_run("r1", control)
    assert service.cancel("r1") is True
    assert control.is_set() is True
    assert service.cancel("r1") is True
    service.unregister_run("r1")
    assert service.cancel("r1") is False
