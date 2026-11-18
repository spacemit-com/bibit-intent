"""HTTP service exposing the embedded Bibit pipeline as a local RPC.

The design is intentionally unidirectional: the runtime plugin (client)
initiates every call and this process only answers. There are no
server-to-client callbacks. See docs/design/bibit-intent-service-decoupling-design.md.
"""
from __future__ import annotations

from collections.abc import Mapping
import logging
import ipaddress
from pathlib import Path
import re
import threading
import time
import uuid
from typing import Any

import anyio
from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .execution.cancellation import (
    EdgeDeadlineExceeded,
    EdgeExecutionCancelled,
    EdgeExecutionControl,
)
from .models.gateway import GatewayModelManager
from .models.backends import model_manager as create_model_manager
from .pipeline import EmbeddedBibitPipeline, PipelineOutcome, PipelineSettings
from .code_tasks import REGISTRY as CODE_TASKS, CodeTask

DEFAULT_PORT = 8765
DEFAULT_TIMEOUT_MS = 90000
MAX_TIMEOUT_MS = 3_600_000
MAX_CANCELLED_RUNS = 4096

logger = logging.getLogger(__name__)


def _error(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _outcome_payload(run_id: str, outcome: PipelineOutcome) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "handled": bool(outcome.handled),
        "reply": str(outcome.reply),
        "route": str(outcome.route),
        "reason": str(outcome.reason),
        "metadata": _jsonable(dict(outcome.metadata or {})),
    }


class BibitIntentService:
    """HTTP/JSON wrapper around :class:`EmbeddedBibitPipeline`."""

    def __init__(
        self,
        pipeline: EmbeddedBibitPipeline,
        *,
        model_manager: GatewayModelManager | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.model_manager = model_manager
        self.started_at = time.monotonic()
        self._runs: dict[str, EdgeExecutionControl] = {}
        self._cancelled_runs: dict[str, float] = {}
        self._runs_lock = threading.Lock()
        from .execution.time_confirmation import TimeConfirmations

        self._time_confirmations = TimeConfirmations()

    def create_app(self) -> FastAPI:
        app = FastAPI(title="Bibit Intent", version="1.0.0")
        app.add_api_route("/v1/file-actions", self.handle_file_action, methods=["POST"])
        app.add_api_route("/v1/runs", self.handle_run, methods=["POST"])
        app.add_api_route("/v1/settings/time/confirmation", self.resolve_time_confirmation, methods=["POST"])
        app.add_api_route("/v1/settings/time/prepare", self.prepare_time_confirmation, methods=["POST"])
        app.add_api_route("/v1/runs/{run_id}", self.cancel_run, methods=["DELETE"])
        app.add_api_route("/v1/code-tasks", self.submit_code_task, methods=["POST"])
        app.add_api_route("/v1/code-tasks/{task_id}", self.code_task_status, methods=["GET"])
        app.add_api_route("/v1/sessions/{session_id}", self.clear_session, methods=["DELETE"])
        app.add_api_route("/v1/models", self.describe_models, methods=["GET"])
        app.add_api_route(
            "/v1/models/{model_type}/{action}",
            self.invoke_model_action,
            methods=["POST"],
        )
        app.add_api_route("/metrics", self.metrics, methods=["GET"])
        app.add_api_route("/healthz", self.healthz, methods=["GET"])
        app.state.service = self
        return app

    async def handle_file_action(self, request: Request) -> JSONResponse:
        from .file_results import FileResultError
        from .file_portal import file_action

        if request.headers.get("origin") or request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            return JSONResponse({"ok": False, "message": "只接受本地应用的 JSON 请求。"}, status_code=403)
        body = await request.body()
        if len(body) > 16384:
            return JSONResponse({"ok": False, "message": "文件动作请求过大。"}, status_code=413)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise FileResultError("文件动作请求无效。")
            result = await anyio.to_thread.run_sync(file_action, payload)
            return JSONResponse(result)
        except (FileResultError, ValueError) as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=422)

    def _model_manager_or_error(self) -> GatewayModelManager | JSONResponse:
        if self.model_manager is None:
            return JSONResponse(
                _error(
                    "model_manager_unavailable",
                    "Bibit model manager is unavailable",
                ),
                status_code=503,
            )
        return self.model_manager

    async def describe_models(self) -> JSONResponse:
        manager = self._model_manager_or_error()
        if isinstance(manager, JSONResponse):
            return manager
        try:
            value = await manager.describe()
        except Exception:
            logger.exception("Bibit model descriptor failed")
            return JSONResponse(
                _error("model_manager_failed", "Bibit model manager failed"),
                status_code=503,
            )
        if not isinstance(value, Mapping):
            return JSONResponse(
                _error("invalid_response", "Bibit model descriptor is invalid"),
                status_code=502,
            )
        return JSONResponse(_jsonable(dict(value)))

    async def invoke_model_action(self, request: Request) -> JSONResponse:
        manager = self._model_manager_or_error()
        if isinstance(manager, JSONResponse):
            return manager
        body = await request.body()
        if body:
            try:
                payload = await request.json()
            except Exception:
                payload = None
            if not isinstance(payload, Mapping) or payload:
                return JSONResponse(
                    _error(
                        "invalid_request",
                        "model actions do not accept request arguments",
                    ),
                    status_code=422,
                )
        model_type = str(request.path_params.get("model_type") or "")
        action = str(request.path_params.get("action") or "")
        if action not in {"download", "cancel", "start", "stop"}:
            return JSONResponse(
                _error("not_found", "unknown model action"), status_code=404
            )
        try:
            value = await manager.invoke(
                f"models.{action}", {"model_type": model_type}
            )
        except ValueError as exc:
            return JSONResponse(
                _error("invalid_request", str(exc)), status_code=422
            )
        except KeyError:
            return JSONResponse(
                _error("not_found", "unknown model action"), status_code=404
            )
        except Exception:
            logger.exception(
                "Bibit model action failed: model_type=%s action=%s",
                model_type,
                action,
            )
            return JSONResponse(
                _error("model_manager_failed", "Bibit model manager failed"),
                status_code=503,
            )
        if not isinstance(value, Mapping):
            return JSONResponse(
                _error("invalid_response", "Bibit model action result is invalid"),
                status_code=502,
            )
        return JSONResponse(_jsonable(dict(value)))

    def register_run(self, run_id: str, control: EdgeExecutionControl) -> None:
        with self._runs_lock:
            self._prune_cancelled_runs()
            if run_id in self._runs or run_id in self._cancelled_runs:
                raise ValueError("run_id is already active or cancelled")
            self._runs[run_id] = control

    def _prune_cancelled_runs(self) -> None:
        # Called under _runs_lock. Keep cancelled IDs for the maximum request
        # lifetime so a delayed POST cannot overtake an earlier DELETE.
        now = time.monotonic()
        self._cancelled_runs = {
            key: expiry for key, expiry in self._cancelled_runs.items()
            if expiry > now
        }

    def unregister_run(self, run_id: str) -> None:
        with self._runs_lock:
            self._runs.pop(run_id, None)

    def cancel(self, run_id: str) -> bool:
        with self._runs_lock:
            self._prune_cancelled_runs()
            control = self._runs.get(run_id)
            if control is not None:
                control.set()
            if run_id not in self._cancelled_runs and len(self._cancelled_runs) >= MAX_CANCELLED_RUNS:
                raise RuntimeError("cancellation registry is full")
            self._cancelled_runs[run_id] = time.monotonic() + MAX_TIMEOUT_MS / 1000.0
        if control is None:
            return False
        return True

    def run_sync(
        self,
        prompt: str,
        session_id: str,
        control: EdgeExecutionControl,
    ) -> PipelineOutcome:
        try:
            outcome = self.pipeline.handle(prompt, session_id, cancel_event=control)
            if control.execution_started.is_set() and not outcome.handled:
                return self.uncertain_outcome()
            return outcome
        except PermissionError:
            return PipelineOutcome(
                handled=True, reply="端侧权限策略拒绝了本次操作。",
                route="local", reason="permission_denied",
            )
        except EdgeExecutionCancelled:
            if control.execution_started.is_set():
                return self.uncertain_outcome()
            return PipelineOutcome(
                handled=False, route="local", reason="edge_execution_cancelled"
            )
        except EdgeDeadlineExceeded:
            if control.execution_started.is_set():
                return self.uncertain_outcome()
            return PipelineOutcome(
                handled=False, route="cloud", reason="edge_deadline_exceeded"
            )
        except Exception as exc:  # noqa: BLE001 - boundary converts to fallback
            if control.execution_started.is_set():
                return self.uncertain_outcome()
            return PipelineOutcome(
                handled=False,
                route="cloud",
                reason="edge_error",
                metadata={"error_type": type(exc).__name__},
            )

    @staticmethod
    def uncertain_outcome() -> PipelineOutcome:
        return PipelineOutcome(
            handled=True, route="local", reason="edge_execution_uncertain",
            reply="端侧操作可能已部分执行；为避免重复操作，本次不再转交云端。",
        )

    async def handle_run(self, request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(
                _error("invalid_json", "request body must be valid JSON"),
                status_code=400,
            )
        if not isinstance(payload, dict):
            return JSONResponse(
                _error("invalid_request", "request body must be a JSON object"),
                status_code=400,
            )

        prompt = str(payload.get("prompt") or "").strip()
        session_id = str(payload.get("session_id") or "").strip()
        if not prompt:
            return JSONResponse(
                _error("invalid_request", "prompt is required"), status_code=400
            )
        if not session_id:
            return JSONResponse(
                _error("invalid_request", "session_id is required"), status_code=400
            )

        timeout_ms = payload.get("timeout_ms", DEFAULT_TIMEOUT_MS)
        try:
            timeout_ms = int(timeout_ms)
        except (TypeError, ValueError):
            return JSONResponse(
                _error("invalid_request", "timeout_ms must be an integer"),
                status_code=400,
            )
        timeout_ms = max(1, min(timeout_ms, MAX_TIMEOUT_MS))

        if bool(payload.get("stream", False)):
            return JSONResponse(
                _error("not_implemented", "stream is not implemented yet"),
                status_code=501,
            )

        run_id = payload.get("run_id", uuid.uuid4().hex)
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
            return JSONResponse(_error("invalid_request", "invalid run_id"), status_code=400)
        control = EdgeExecutionControl(
            deadline=time.monotonic() + timeout_ms / 1000.0
        )
        try:
            self.register_run(run_id, control)
        except ValueError:
            return JSONResponse(_error("run_conflict", "run_id is active or cancelled"), status_code=409)
        try:
            self._time_confirmations.discard(session_id=session_id)
            outcome = await anyio.to_thread.run_sync(
                self.run_sync, prompt, session_id, control
            )
        finally:
            self.unregister_run(run_id)
        response = _outcome_payload(run_id, outcome)
        confirmation = response["metadata"].pop("time_confirmation", None)
        if confirmation is not None:
            with self._runs_lock:
                if control.is_set() or run_id in self._cancelled_runs:
                    response["reply"] = "时间修改已取消，未关闭自动校时。"
                else:
                    response["confirmation"] = self._time_confirmations.create(session_id, run_id, confirmation)
        return JSONResponse(response)

    async def prepare_time_confirmation(self, request: Request) -> Response:
        from .core.system_time import normalize_system_datetime
        from .execution.time_confirmation import time_change_confirmation

        try:
            if request.client is None or not ipaddress.ip_address(request.client.host).is_loopback:
                return JSONResponse(_error("forbidden", "Confirmation requires a local Runtime connection"), status_code=403)
            if len(await request.body()) > 4096:
                raise ValueError("Request too large")
            payload = await request.json()
            if not isinstance(payload, dict) or set(payload) != {"datetime", "session_id", "run_id"}:
                raise ValueError("Invalid confirmation request")
            if not all(isinstance(value, str) and 0 < len(value) <= 256 for value in payload.values()):
                raise ValueError("Invalid confirmation values")
            target = normalize_system_datetime(payload["datetime"])
            if target is None:
                raise ValueError("Invalid datetime")
            result = time_change_confirmation(target, None)
            ticket = self._time_confirmations.create(payload["session_id"], payload["run_id"], result.confirmation)
            return JSONResponse(ticket)
        except (ValueError, TypeError):
            return JSONResponse(_error("invalid_confirmation", "Invalid time confirmation request"), status_code=400)

    async def resolve_time_confirmation(self, request: Request) -> Response:
        try:
            if request.client is None or not ipaddress.ip_address(request.client.host).is_loopback:
                return JSONResponse(_error("forbidden", "Confirmation requires a local Runtime connection"), status_code=403)
            if len(await request.body()) > 4096:
                raise ValueError("Request too large")
            payload = await request.json()
            if not isinstance(payload, dict) or set(payload) != {"id", "session_id", "decision"}:
                raise ValueError("Invalid confirmation request")
            if not all(isinstance(value, str) and value for value in payload.values()):
                raise ValueError("Invalid confirmation values")
            token = request.headers.get("X-Bibit-Confirmation-Token", "")
            result = await anyio.to_thread.run_sync(self._time_confirmations.resolve, payload["id"], token,
                                                   payload["session_id"], payload["decision"])
            return JSONResponse({"ok": result.ok, "reply": result.message, "detail": result.detail})
        except (ValueError, TypeError):
            return JSONResponse(_error("invalid_confirmation", "Confirmation is invalid, expired or already consumed"), status_code=409)

    async def cancel_run(self, request: Request) -> Response:
        run_id = request.path_params["run_id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
            return JSONResponse(_error("invalid_request", "invalid run_id"), status_code=400)
        try:
            cancelled = self.cancel(run_id)
            self._time_confirmations.discard(run_id=run_id)
        except RuntimeError:
            return JSONResponse(_error("unavailable", "cancellation registry is full"), status_code=503)
        if cancelled:
            return Response(status_code=204)
        return JSONResponse(_error("not_found", "run not found"), status_code=404)

    async def clear_session(self, request: Request) -> Response:
        session_id = request.path_params["session_id"]
        self._time_confirmations.discard(session_id=session_id)
        try:
            removed = await anyio.to_thread.run_sync(
                self.pipeline.clear_session, session_id
            )
        except AttributeError:
            return JSONResponse(
                _error(
                    "not_implemented",
                    "pipeline does not support session clearing",
                ),
                status_code=501,
            )
        if removed:
            return Response(status_code=204)
        return JSONResponse(_error("not_found", "session not found"), status_code=404)

    async def submit_code_task(self, request: Request) -> JSONResponse:
        """Submit a coding task; dsh runs it here, outside the runtime sandbox."""
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(
                _error("invalid_json", "request body must be valid JSON"),
                status_code=400,
            )
        if not isinstance(payload, dict):
            return JSONResponse(
                _error("invalid_request", "request body must be a JSON object"),
                status_code=400,
            )
        prompt = str(payload.get("task") or "").strip()
        workspace = str(payload.get("workspace") or "").strip()
        if not prompt:
            return JSONResponse(
                _error("invalid_request", "task is required"), status_code=400
            )
        try:
            task = CODE_TASKS.submit(prompt, workspace)
        except Exception as exc:  # noqa: BLE001 - report why it could not start
            return JSONResponse(
                _error("code_task_unavailable", f"{type(exc).__name__}: {exc}"),
                status_code=503,
            )
        return JSONResponse({"data": task.payload()}, status_code=202)

    async def code_task_status(self, request: Request) -> JSONResponse:
        task: CodeTask | None = CODE_TASKS.get(request.path_params["task_id"])
        if task is None:
            return JSONResponse(
                _error("not_found", "code task not found"), status_code=404
            )
        return JSONResponse({"data": task.payload()})

    async def healthz(self, request: Request) -> JSONResponse:
        pipeline = self.pipeline
        return JSONResponse(
            {
                "status": "ok",
                "active_sessions": int(
                    getattr(pipeline, "active_session_count", 0)
                ),
                "registered_tools": int(
                    getattr(pipeline, "registered_tool_count", 0)
                ),
                "uptime_seconds": round(time.monotonic() - self.started_at, 3),
            }
        )

    async def metrics(self, request: Request) -> JSONResponse:
        return JSONResponse(self.pipeline.stats())


def run_server(
    pipeline: EmbeddedBibitPipeline,
    *,
    data_root: str | Path,
    socket: str | None = None,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    log_level: str = "info",
    settings: PipelineSettings | None = None,
) -> None:
    import uvicorn

    service = BibitIntentService(
        pipeline,
        model_manager=create_model_manager(settings or PipelineSettings.from_environment()),
    )
    app = service.create_app()
    if socket:
        uvicorn.run(app, uds=socket, log_level=log_level)
    else:
        uvicorn.run(app, host=host, port=port, log_level=log_level)
