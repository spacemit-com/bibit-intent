from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bibit_intent.execution.system_tools import ExecResult
from bibit_intent.mcp_server.server import (
    _AsyncTaskRegistry,
    _async_lock_key,
    _build_executor,
)


class _FakeExecutor:
    def __init__(self, result: ExecResult | None = None, *, exc: Exception | None = None):
        self.result = result or ExecResult(True, "ok")
        self.exc = exc
        self.calls = 0

    def execute(self, tool_name: str, arguments: dict) -> ExecResult:
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.result


def test_high_latency_install_tools_are_marked_async():
    registry, _ = _build_executor()

    assert registry.get("pkg_install").mcp_async() is True
    assert registry.get("pkg_remove").mcp_async() is True
    assert registry.get("pkg_update").mcp_async() is False
    assert "bibit_task_status(task_id)" not in registry.get("pkg_update").mcp_schema()["function"]["description"]
    assert registry.get("sys_uptime").mcp_async() is False
    schema = registry.get("pkg_install").mcp_schema()["function"]
    assert "bibit_task_status(task_id)" in schema["description"]


def test_async_lock_key_groups_package_managers():
    registry, _ = _build_executor()

    assert _async_lock_key(registry.get("pkg_install")) == "apt"
    assert _async_lock_key(registry.get("dep_apply")) == "apt"
    assert _async_lock_key(registry.get("pip_install")) == "pip"
    assert _async_lock_key(registry.get("npm_install")) == "npm"
    assert _async_lock_key(registry.get("install_local")) == "apt"


@pytest.mark.asyncio
async def test_upgrade_mcp_returns_submission_without_polling(monkeypatch):
    from bibit_intent.mcp_server import server

    definitions, _ = _build_executor()
    executor = _FakeExecutor(ExecResult(True, "系统更新任务已提交"))
    registry = _AsyncTaskRegistry()
    monkeypatch.setattr(server, "_build_executor", lambda: (definitions, executor))
    monkeypatch.setattr(server, "_tools_for_groups", lambda *args: [definitions.get("pkg_update")])
    monkeypatch.setattr(server, "_AsyncTaskRegistry", lambda: registry)
    mcp = server.create_server()
    result = json.loads(await mcp._tool_manager.get_tool("pkg_update").fn(confirm=True))
    assert result["message"] == "系统更新任务已提交"
    assert "task_id" not in result
    assert not registry._tasks
    assert executor.calls == 1


@pytest.mark.asyncio
async def test_async_task_registry_returns_task_id_then_final_result(monkeypatch):
    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    executor = _FakeExecutor(ExecResult(True, "pkg installed", "detail"))
    registry = _AsyncTaskRegistry()

    task_id = registry.start(executor, "pkg_install", {"pkg": "iperf3"})
    running = registry.status(task_id)

    assert running["task_id"] == task_id
    assert running["status"] == "running"
    assert running["ok"] is True

    while not registry._tasks[task_id].done():
        await asyncio.sleep(0)

    completed = registry.status(task_id)
    assert completed["status"] == "completed"
    assert completed["ok"] is True
    assert completed["message"] == "pkg installed"
    assert completed["detail"] == "detail"
    assert executor.calls == 1


@pytest.mark.asyncio
async def test_async_task_registry_serializes_same_lock_key(monkeypatch):
    active = 0
    max_active = 0

    async def fake_to_thread(func, *args, **kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    registry = _AsyncTaskRegistry()
    first = registry.start(
        _FakeExecutor(ExecResult(True, "first")),
        "pkg_install",
        {"pkg": "iperf3"},
        lock_key="apt",
    )
    second = registry.start(
        _FakeExecutor(ExecResult(True, "second")),
        "pkg_update",
        {},
        lock_key="apt",
    )

    await asyncio.gather(registry._tasks[first], registry._tasks[second])

    assert max_active == 1
    assert registry.status(first)["message"] == "first"
    assert registry.status(second)["message"] == "second"


@pytest.mark.asyncio
async def test_async_task_registry_keeps_exceptions_queryable(monkeypatch):
    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    registry = _AsyncTaskRegistry()
    task_id = registry.start(
        _FakeExecutor(exc=RuntimeError("boom")),
        "pkg_install",
        {"pkg": "iperf3"},
    )

    while not registry._tasks[task_id].done():
        await asyncio.sleep(0)

    failed = registry.status(task_id)
    assert failed["status"] == "completed"
    assert failed["ok"] is False
    assert "RuntimeError" in failed["message"]


def test_async_task_registry_reports_unknown_task():
    registry = _AsyncTaskRegistry()
    status = registry.status("missing")

    assert status["ok"] is False
    assert status["status"] == "unknown"


@pytest.mark.asyncio
async def test_cancellation_does_not_release_lock_before_worker_finishes():
    registry = _AsyncTaskRegistry()
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    second = _FakeExecutor()

    class BlockingExecutor:
        def execute(self, tool_name, arguments):
            loop.call_soon_threadsafe(started.set)
            if not release.wait(5):
                raise TimeoutError("test worker was not released")
            return ExecResult(True, "done")

    first_id = registry.start(BlockingExecutor(), "pkg_install", {}, lock_key="apt")
    second_id = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        registry._tasks[first_id].cancel()
        await asyncio.sleep(0)
        registry._tasks[first_id].cancel()
        second_id = registry.start(second, "pkg_remove", {}, lock_key="apt")
        await asyncio.sleep(0.02)
        assert second.calls == 0
        assert not registry._tasks[first_id].done()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*registry._tasks.values(), return_exceptions=True), 3)
    assert registry.status(first_id)["status"] == "cancelled"
    assert second_id is not None and registry.status(second_id)["ok"]


@pytest.mark.asyncio
async def test_registered_tools_use_execution_resource_locks(monkeypatch):
    pytest.importorskip("mcp.server.fastmcp")
    from bibit_intent.mcp_server import server

    definitions, _ = _build_executor()
    registry = _AsyncTaskRegistry()
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    calls = []

    class BlockingExecutor:
        def execute(self, tool_name, arguments):
            calls.append(tool_name)
            if tool_name == "pkg_install":
                loop.call_soon_threadsafe(started.set)
                if not release.wait(5):
                    raise TimeoutError("test worker was not released")
            return ExecResult(True, tool_name)

    selected = [definitions.get(name) for name in (
        "pkg_install", "pkg_remove", "install_local", "pkg_clean", "pip_install", "sys_uptime",
    )]
    monkeypatch.setattr(server, "_build_executor", lambda: (definitions, BlockingExecutor()))
    monkeypatch.setattr(server, "_tools_for_groups", lambda *args: selected)
    monkeypatch.setattr(server, "_AsyncTaskRegistry", lambda: registry)
    mcp = server.create_server()

    async def invoke(name, **arguments):
        return json.loads(await mcp._tool_manager.get_tool(name).fn(**arguments))

    clean = None
    try:
        first = await invoke("pkg_install", pkg="vim")
        await asyncio.wait_for(started.wait(), 2)
        assert first["status"] == "running"
        await invoke("pkg_remove", pkg="vim")
        await invoke("install_local", file="/tmp/test.deb")
        clean = asyncio.create_task(invoke("pkg_clean"))
        pip = await invoke("pip_install", pkg="pytest")
        await asyncio.wait_for(registry._tasks[pip["task_id"]], 2)
        assert (await asyncio.wait_for(invoke("sys_uptime"), 2))["ok"]
        assert "pip_install" in calls and "sys_uptime" in calls
        assert "pkg_remove" not in calls and "install_local" not in calls
        assert not clean.done()
        assert (await invoke("bibit_task_status", task_id=first["task_id"]))["status"] == "running"
    finally:
        release.set()
        pending = list(registry._tasks.values())
        if clean is not None:
            pending.append(clean)
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 3)
    assert {"pkg_remove", "install_local", "pkg_clean"} <= set(calls)
