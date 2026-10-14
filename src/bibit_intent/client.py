"""Thin HTTP client for the bibit-intent service (unidirectional RPC).

This module is imported by the runtime plugin's BEFORE_RUN hook. It speaks the
``/v1`` protocol implemented in ``server.py``.
"""
from __future__ import annotations

from typing import Any, Mapping

import httpx

from .pipeline import PipelineOutcome

DEFAULT_BASE_URL = "http://127.0.0.1:8765"
DEFAULT_TIMEOUT_SECONDS = 120.0


class BibitServiceError(RuntimeError):
    """Raised when the service returns an error or cannot be reached."""


class BibitIntentClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = (base_url or "").rstrip("/")
        self._timeout = float(timeout)
        self._transport = transport

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self._base_url,
            timeout=self._timeout,
            transport=self._transport,
        )

    def run(
        self,
        prompt: str,
        session_id: str,
        *,
        timeout_ms: int = 90000,
    ) -> PipelineOutcome:
        with self._client() as client:
            response = client.post(
                "/v1/runs",
                json={
                    "session_id": session_id,
                    "prompt": prompt,
                    "timeout_ms": timeout_ms,
                    "stream": False,
                },
            )
        return self._parse_run(response)

    def cancel(self, run_id: str) -> bool:
        with self._client() as client:
            response = client.delete(f"/v1/runs/{run_id}")
        if response.status_code == 204:
            return True
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return False

    def clear_session(self, session_id: str) -> bool:
        with self._client() as client:
            response = client.delete(f"/v1/sessions/{session_id}")
        if response.status_code == 204:
            return True
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return False

    def health(self) -> Mapping[str, Any]:
        with self._client() as client:
            response = client.get("/healthz")
            response.raise_for_status()
            return dict(response.json())

    @staticmethod
    def _parse_run(response: httpx.Response) -> PipelineOutcome:
        if response.status_code >= 400:
            detail = ""
            try:
                payload = response.json()
                detail = payload.get("error", {}).get("message", "")
            except Exception:
                detail = response.text
            raise BibitServiceError(
                f"bibit-intent service error {response.status_code}: "
                f"{detail or response.text}"
            )
        payload = response.json()
        return PipelineOutcome(
            handled=bool(payload.get("handled", False)),
            reply=str(payload.get("reply", "")),
            route=str(payload.get("route", "")),
            reason=str(payload.get("reason", "")),
            metadata=dict(payload.get("metadata", {}) or {}),
        )
