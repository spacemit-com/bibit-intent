from __future__ import annotations

import httpx
import pytest

from bibit_intent.execution import system_tools
from bibit_intent.execution import time_confirmation as confirmation
from bibit_intent.server import BibitIntentService


@pytest.fixture
def commands(monkeypatch):
    calls = []
    state = {"ntp": True, "disable_ok": True, "write_ok": True, "restore_ok": True}

    def run(command):
        calls.append(command)
        if command[:2] == ["timedatectl", "show"]:
            return system_tools.ExecResult(True, "ok", "yes" if state["ntp"] else "no")
        if command[2] == "set-ntp":
            enabling = command[3] == "true"
            if not state["restore_ok" if enabling else "disable_ok"]:
                return system_tools.ExecResult(False, "permission denied")
            state["ntp"] = enabling
            return system_tools.ExecResult(True, "ok")
        assert command[2] == "set-time"
        return system_tools.ExecResult(state["write_ok"], "ok" if state["write_ok"] else "set-time failed")

    monkeypatch.setattr(system_tools, "_run", run)
    return calls, state


def pending(store):
    result = system_tools.set_system_time("14:30")
    assert not result.ok and result.confirmation is not None
    return store.create("session", "run", result.confirmation)


def resolve(store, ticket, decision="approve", session="session"):
    return store.resolve(ticket["id"], ticket["token"], session, decision)


def test_ntp_only_read_before_approval(commands):
    calls, state = commands
    result = system_tools.set_system_time("14:30", confirmed=True)
    assert result.confirmation["datetime"] == "14:30:00"
    assert len(calls) == 1 and calls[0][1] == "show" and state["ntp"]


def test_ntp_off_no_confirmation(commands):
    calls, state = commands
    state["ntp"] = False
    result = system_tools.set_system_time("2026-10-22")
    assert result.ok and result.confirmation is None
    assert calls[-1][2] == "set-time" and len(calls) == 2


def test_approve_disables_once_and_sets_time(commands):
    calls, state = commands
    store = confirmation.TimeConfirmations()
    ticket = pending(store)
    assert resolve(store, ticket).ok
    assert not state["ntp"]
    assert [command[2:] for command in calls if command[1] == "--no-ask-password"][0] == ["set-ntp", "false"]
    assert calls[-1][2] == "set-time"
    count = len(calls)
    with pytest.raises(ValueError):
        resolve(store, ticket)
    assert len(calls) == count


def test_reject_never_changes_device(commands):
    calls, state = commands
    store = confirmation.TimeConfirmations()
    assert resolve(store, pending(store), "reject").ok
    assert len(calls) == 1 and state["ntp"]


def test_wrong_session_or_token_not_accepted(commands):
    store = confirmation.TimeConfirmations()
    ticket = pending(store)
    with pytest.raises(ValueError):
        resolve(store, ticket, session="other")
    with pytest.raises(ValueError):
        store.resolve(ticket["id"], "wrong", "session", "approve")
    assert resolve(store, ticket, "reject").ok


def test_timeout_and_superseding_request(commands, monkeypatch):
    store = confirmation.TimeConfirmations()
    ticket = pending(store)
    newer = pending(store)
    with pytest.raises(ValueError):
        resolve(store, ticket)
    monkeypatch.setattr(confirmation.time, "monotonic", lambda: float("inf"))
    with pytest.raises(ValueError):
        resolve(store, newer)
    assert all(command[1] == "show" for command in commands[0])


@pytest.mark.parametrize("by", ["run_id", "session_id"])
def test_cancellation_invalidates_ticket(commands, by):
    store = confirmation.TimeConfirmations()
    ticket = pending(store)
    store.discard(**{by: "run" if by == "run_id" else "session"})
    with pytest.raises(ValueError):
        resolve(store, ticket)


def test_permission_failure_does_not_set_time(commands):
    calls, state = commands
    state["disable_ok"] = False
    store = confirmation.TimeConfirmations()
    assert not resolve(store, pending(store)).ok
    assert state["ntp"] and not any(command[2] == "set-time" for command in calls)


@pytest.mark.parametrize("restore", [True, False])
def test_time_failure_restores_ntp_or_reports_failure(commands, restore):
    calls, state = commands
    state.update(write_ok=False, restore_ok=restore)
    store = confirmation.TimeConfirmations()
    result = resolve(store, pending(store))
    assert not result.ok and state["ntp"] is restore
    assert ("已恢复" if restore else "恢复未确认") in result.message


def test_state_is_rechecked_after_wait(commands):
    calls, state = commands
    store = confirmation.TimeConfirmations()
    ticket = pending(store)
    state["ntp"] = False
    assert resolve(store, ticket).ok
    assert not any(command[2] == "set-ntp" for command in calls)


def test_unknown_ntp_state_fails_closed(monkeypatch):
    monkeypatch.setattr(system_tools, "_run", lambda command: system_tools.ExecResult(True, "ok", "unknown"))
    result = system_tools.set_system_time("14:30")
    assert not result.ok and result.confirmation is None


def test_sandbox_cannot_consume_approval(commands, monkeypatch):
    calls, state = commands
    store = confirmation.TimeConfirmations()
    ticket = pending(store)
    monkeypatch.setenv("BIANBU_SANDBOX_ACTIVE", "1")
    assert resolve(store, ticket).escalate == "needs_host_channel"
    assert len(calls) == 1


def test_completion_resolves_partial_fields_after_approval(commands, monkeypatch):
    from datetime import datetime, timezone

    class Clock:
        @staticmethod
        def now():
            return Clock()

        def astimezone(self):
            return datetime(2027, 1, 1, 0, 0, 9, tzinfo=timezone.utc)

    store = confirmation.TimeConfirmations()
    ticket = pending(store)
    monkeypatch.setattr(system_tools, "system_datetime", Clock)
    assert resolve(store, ticket).ok
    assert commands[0][-1][-1] == "2027-01-01 14:30:00"


def test_concurrent_decisions_execute_at_most_once(commands):
    from concurrent.futures import ThreadPoolExecutor

    store = confirmation.TimeConfirmations()
    ticket = pending(store)

    def decide():
        try:
            return resolve(store, ticket).ok
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: decide(), range(2)))
    assert results.count(True) == 1
    assert sum(command[2] == "set-time" for command in commands[0]) == 1


@pytest.mark.asyncio
async def test_remote_confirmation_endpoint_rejected():
    service = BibitIntentService(object())
    transport = httpx.ASGITransport(app=service.create_app(), client=("192.0.2.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post("/v1/settings/time/confirmation", json={})).status_code == 403


@pytest.mark.asyncio
async def test_http_ticket_is_bound_and_single_use(commands):
    from bibit_intent.core.registry import ToolRegistry
    from bibit_intent.execution.executor import Executor
    from bibit_intent.pipeline import EmbeddedBibitPipeline
    from bibit_intent.routing.router import Router
    from bibit_intent.routing.rule_engine import RuleEngine

    registry = ToolRegistry()
    pipeline = EmbeddedBibitPipeline(Router(registry, RuleEngine(registry), None, Executor(registry)))
    service = BibitIntentService(pipeline)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service.create_app()), base_url="http://test") as client:
        response = await client.post("/v1/runs", json={"prompt": "设置系统时间为14:30", "session_id": "session"})
        ticket = response.json()["confirmation"]
        assert "time_confirmation" not in response.json()["metadata"]
        payload = {"id": ticket["id"], "session_id": "session", "decision": "approve"}
        assert (await client.post("/v1/settings/time/confirmation", json=payload)).status_code == 409
        headers = {"X-Bibit-Confirmation-Token": ticket["token"]}
        result = await client.post("/v1/settings/time/confirmation", json=payload, headers=headers)
        assert result.json()["ok"]
        assert (await client.post("/v1/settings/time/confirmation", json=payload, headers=headers)).status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("ntp", [True, False])
async def test_cloud_prepare_never_writes_and_reject_is_safe(commands, ntp):
    calls, state = commands
    state["ntp"] = ntp
    service = BibitIntentService(None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service.create_app()), base_url="http://test") as client:
        response = await client.post("/v1/settings/time/prepare", json={
            "datetime": "15:30", "session_id": "cloud-session", "run_id": "cloud-run"})
        assert response.status_code == 200
        ticket = response.json()
        assert ticket["datetime"] == "15:30:00"
        assert ticket["kind"] == "disable_ntp_and_set_time"
        assert calls == []
        response = await client.post("/v1/settings/time/confirmation", json={
            "id": ticket["id"], "session_id": "cloud-session", "decision": "reject"},
            headers={"X-Bibit-Confirmation-Token": ticket["token"]})
        assert response.json()["ok"]
        assert calls == [] and state["ntp"] == ntp


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["tomorrow", "25:30", "15:30; reboot", ""])
async def test_cloud_prepare_invalid_target(commands, target):
    service = BibitIntentService(None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service.create_app()), base_url="http://test") as client:
        response = await client.post("/v1/settings/time/prepare", json={
            "datetime": target, "session_id": "session", "run_id": "run"})
    assert response.status_code == 400
    assert commands[0] == []


@pytest.mark.asyncio
async def test_cloud_prepare_refuses_remote_client(commands):
    service = BibitIntentService(None)
    transport = httpx.ASGITransport(app=service.create_app(), client=("192.0.2.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post("/v1/settings/time/prepare", json={})).status_code == 403
    assert commands[0] == []
