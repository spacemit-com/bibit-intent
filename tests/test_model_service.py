from __future__ import annotations

from collections.abc import Mapping

import httpx
import pytest

from bibit_intent.server import BibitIntentService


class _Pipeline:
    active_session_count = 0
    registered_tool_count = 0

    def stats(self) -> dict[str, object]:
        return {}


class _ModelManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def describe(self) -> Mapping[str, object]:
        return {
            "models": [
                {
                    "model_type": "llm",
                    "name": "bibit-llm",
                    "backend": "ai_gateway",
                    "port": None,
                    "download": {"status": "completed"},
                    "server": {"status": "running", "managed": True},
                }
            ],
            "actions": (
                "models.download",
                "models.cancel",
                "models.start",
                "models.stop",
            ),
        }

    async def invoke(
        self, action: str, payload: Mapping[str, object]
    ) -> Mapping[str, object]:
        if payload.get("model_type") != "llm":
            raise ValueError("model_type must be llm or embed")
        self.calls.append((action, dict(payload)))
        return {"accepted": True, "status": action.removeprefix("models.")}


@pytest.fixture
def manager() -> _ModelManager:
    return _ModelManager()


@pytest.fixture
def app(manager: _ModelManager):
    return BibitIntentService(_Pipeline(), model_manager=manager).create_app()


@pytest.mark.asyncio
async def test_model_descriptor_is_owned_by_bibit_service(app) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/models")

    assert response.status_code == 200
    assert response.json()["models"][0]["name"] == "bibit-llm"
    assert response.json()["actions"] == [
        "models.download",
        "models.cancel",
        "models.start",
        "models.stop",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["download", "cancel", "start", "stop"])
async def test_model_action_maps_to_manager_contract(
    app, manager: _ModelManager, action: str
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(f"/v1/models/llm/{action}", json={})

    assert response.status_code == 200
    assert response.json() == {"accepted": True, "status": action}
    assert manager.calls[-1] == (f"models.{action}", {"model_type": "llm"})


@pytest.mark.asyncio
async def test_invalid_model_and_action_are_rejected(app) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        model = await client.post("/v1/models/rerank/start", json={})
        action = await client.post("/v1/models/llm/delete", json={})

    assert model.status_code == 422
    assert model.json()["error"]["code"] == "invalid_request"
    assert action.status_code == 404
    assert action.json()["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_model_action_rejects_caller_supplied_arguments(app) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/models/llm/start", json={"command": "anything"}
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_model_routes_fail_clearly_without_manager() -> None:
    app = BibitIntentService(_Pipeline()).create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        descriptor = await client.get("/v1/models")
        action = await client.post("/v1/models/llm/start", json={})

    assert descriptor.status_code == 503
    assert descriptor.json()["error"]["code"] == "model_manager_unavailable"
    assert action.status_code == 503
    assert action.json()["error"]["code"] == "model_manager_unavailable"
