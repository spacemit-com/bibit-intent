"""Code task handler: forward to the intent service, which runs dsh itself.

The MCP tool processes run inside the runtime sandbox: they cannot see the
dsh package (/usr/bin/dsh, from deepseek-harness) and their /home/agent is a
read-only bind, so spawning dsh there fails. They forward over loopback
instead; the service process runs outside the sandbox and owns the subprocess.

The tool is declared ``mcp.async: true``, so the MCP layer returns a task id at
once and the model polls ``bibit_task_status``. One dsh run takes tens of
seconds, well past the runtime's MCP call timeout, so this handler also caps its
own wait and hands back a resumable message if the task is still running.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Literal

SERVICE = os.environ.get("BIBIT_INTENT_BASE_URL", "http://127.0.0.1:8765")
POLL_INTERVAL = 2.0
# Stays under the MCP async wait so the model gets a real answer, not a timeout.
WAIT_BUDGET = 540.0


@dataclass
class ExecResult:
    ok: bool
    message: str
    detail: str = ""
    detail_format: Literal["text", "markdown"] = "markdown"


def _request(path: str, payload: dict | None, timeout: float) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        SERVICE + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def code_task(task: str = "", workspace: str = "", **_) -> ExecResult:
    """让 dsh 在受限工作区里完成一个编码任务并回报改动。"""
    task = (task or "").strip()
    if not task:
        return ExecResult(False, "请说明要完成的编码任务。")
    try:
        started = _request(
            "/v1/code-tasks", {"task": task, "workspace": workspace}, 20.0
        )
        task_id = str((started.get("data") or {}).get("task_id") or "")
    except urllib.error.HTTPError as exc:
        return ExecResult(False, f"代码任务提交失败：HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001 - report, never crash the tool call
        return ExecResult(False, f"代码任务提交失败：{type(exc).__name__}: {exc}")
    if not task_id:
        return ExecResult(False, "代码任务提交失败：未返回任务号")

    deadline = time.monotonic() + WAIT_BUDGET
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
        try:
            data = _request(f"/v1/code-tasks/{task_id}", None, 15.0).get("data") or {}
        except Exception:  # noqa: BLE001 - a transient read error is not fatal
            continue
        if data.get("status") == "running":
            continue
        detail = f"工作区：{data.get('workspace')}"
        files = data.get("files") or []
        if files:
            detail += "\n改动文件：" + "、".join(files[:10])
        if data.get("status") == "done":
            return ExecResult(True, str(data.get("answer") or "").strip(), detail)
        return ExecResult(
            False, f"代码任务未完成：{data.get('error') or '未知原因'}", detail
        )
    return ExecResult(
        False,
        "代码任务还在跑，可以稍后再问一次结果。",
        f"任务号：{task_id}",
    )


HANDLERS = {"code_task": code_task}
