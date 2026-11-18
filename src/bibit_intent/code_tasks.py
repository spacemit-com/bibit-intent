"""Coding tasks executed by dsh (DeepSeek Harness) on the device.

dsh comes from the ``deepseek-harness`` package (``/usr/bin/dsh``, a wrapper
around the packaged runtime). It runs outside the runtime sandbox, so the MCP
tool processes cannot start it themselves; they forward here over loopback and
this service owns the subprocess.

Note the protocol change in dsh 0.2.x: the LLM adapter now speaks Anthropic
Messages and appends ``/v1/messages`` to the base URL, so the endpoint is the
``/anthropic`` root rather than the OpenAI-compatible ``/v1`` one the runtime
provider record uses. Device credentials are still read from that record, with
the base URL rewritten here.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DSH_BIN = Path(os.environ.get("BIBIT_CODE_DSH_BIN") or "/usr/bin/dsh")
PROVIDER_YAML = Path(
    "/usr/share/bianbu-agent-runtime/models/custom-bibit/provider.yaml"
)

# dsh 0.2.x speaks Anthropic Messages; the provider record stores the
# OpenAI-compatible root that the runtime agent needs.
ANTHROPIC_ROOT = "/anthropic"

# Keep dsh's own state (sessions, profile) separate from any desktop app, and
# put the scratch area under the same root so one cleanup policy covers both.
DSH_HOME = Path.home() / ".bibit-code" / "dsh-home"
WORKSPACE_ROOT = Path.home() / "bibit-code-workspace"
SPILL_ROOT = DSH_HOME / "spill"

# One dsh run takes tens of seconds; the MCP call returns a task id instead.
TASK_TIMEOUT = 600.0
KEEP_FINISHED = 32


class CodeTaskError(RuntimeError):
    """Raised when a coding task cannot be started."""


@dataclass
class CodeTask:
    task_id: str
    prompt: str
    workspace: Path
    status: str = "running"
    answer: str = ""
    error: str = ""
    files: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    before: dict[str, float] = field(default_factory=dict, repr=False)

    def payload(self) -> dict:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "answer": self.answer,
            "error": self.error,
            "files": self.files,
            "workspace": str(self.workspace),
            "elapsed_s": round((self.finished_at or time.time()) - self.started_at, 1),
        }


def _messages_root(base: str) -> str:
    """Rewrite an OpenAI-compatible root to the Anthropic Messages root.

    The runtime provider record stores ``https://api.deepseek.com/v1`` for the
    agent, but dsh 0.2.x appends ``/v1/messages`` to whatever root it is given,
    so ``/v1`` would produce ``/v1/v1/messages``. An explicit base that already
    points at a Messages root is passed through untouched.
    """
    base = base.rstrip("/")
    if "/anthropic" in base:
        return base
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base + ANTHROPIC_ROOT


def _provider() -> tuple[str, str]:
    """Resolve endpoint and key.

    On a shipped device the runtime already owns a provider record, so read it
    rather than keeping a second copy of the key. Both values can be overridden
    by environment for development checkouts that have no runtime installed.
    """
    base = os.environ.get("BIBIT_CODE_BASE_URL", "").strip()
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if base and key:
        return _messages_root(base), key
    data: dict = {}
    if PROVIDER_YAML.is_file():
        data = yaml.safe_load(PROVIDER_YAML.read_text(encoding="utf-8")) or {}
    base = base or str(data.get("base_url") or "").rstrip("/")
    key = key or str((data.get("parameters") or {}).get("api_key") or "")
    if not base or not key:
        raise CodeTaskError(
            f"缺少模型端点或密钥（{PROVIDER_YAML} 不可读且未设置 "
            "BIBIT_CODE_BASE_URL / DEEPSEEK_API_KEY）"
        )
    return _messages_root(base), key


def _snapshot(root: Path) -> dict[str, float]:
    """Map relative path -> mtime, so we can report what actually changed."""
    out: dict[str, float] = {}
    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        try:
            out[str(path.relative_to(root))] = path.stat().st_mtime
        except OSError:
            continue
    return out


def _changed(before: dict[str, float], after: dict[str, float]) -> list[str]:
    return sorted(n for n, m in after.items() if n not in before or m > before[n])


class CodeTaskRegistry:
    """Track in-flight code tasks; finished ones are kept briefly for polling."""

    def __init__(self) -> None:
        self._tasks: dict[str, CodeTask] = {}
        self._lock = threading.Lock()

    def get(self, task_id: str) -> CodeTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def submit(self, prompt: str, workspace: str = "") -> CodeTask:
        if not (DSH_BIN.is_file() or shutil.which(str(DSH_BIN))):
            raise CodeTaskError(f"找不到 dsh 入口 {DSH_BIN}")
        task_id = uuid.uuid4().hex[:12]
        # A caller that points at an existing project must not get a stray
        # empty run directory next to it; only new tasks get a fresh one.
        root = (
            Path(workspace).expanduser()
            if workspace
            else WORKSPACE_ROOT / "runs" / (time.strftime("%Y%m%d-%H%M%S") + "-" + task_id[:4])
        )
        root.mkdir(parents=True, exist_ok=True)
        task = CodeTask(task_id=task_id, prompt=prompt, workspace=root)
        task.before = _snapshot(root)
        with self._lock:
            finished = [k for k, t in self._tasks.items() if t.status != "running"]
            for key in finished[: max(0, len(finished) - KEEP_FINISHED)]:
                self._tasks.pop(key, None)
            self._tasks[task_id] = task
        threading.Thread(target=self._run, args=(task,), daemon=True).start()
        return task

    def _run(self, task: CodeTask) -> None:
        try:
            base, key = _provider()
            DSH_HOME.mkdir(parents=True, exist_ok=True)
            SPILL_ROOT.mkdir(parents=True, exist_ok=True)
            env = dict(os.environ)
            env.update(
                DSH_HOME=str(DSH_HOME),
                DEEPSEEK_API_KEY=key,
                DEEPSEEK_BASE_URL=base,
                # headless has no approval responder: the default "ask" policy
                # would deny every edit. The workspace boundary is enforced
                # here by pinning cwd, not by dsh itself.
                DSH_PERMISSION_MODE="danger-full-access",
                TMPDIR=str(SPILL_ROOT),
            )
            proc = subprocess.run(
                [str(DSH_BIN), "headless", task.prompt],
                cwd=str(task.workspace),
                env=env,
                capture_output=True,
                text=True,
                timeout=TASK_TIMEOUT,
            )
            task.answer = (proc.stdout or "").strip()
            if proc.returncode != 0 or not task.answer:
                lines = [x for x in (proc.stderr or "").strip().splitlines() if x.strip()]
                task.status = "failed"
                task.error = lines[-1] if lines else f"dsh 退出码 {proc.returncode}"
            else:
                task.status = "done"
        except subprocess.TimeoutExpired:
            task.status = "failed"
            task.error = f"超过 {int(TASK_TIMEOUT)} 秒未完成"
        except Exception as exc:  # noqa: BLE001 - surface any failure to the caller
            task.status = "failed"
            task.error = f"{type(exc).__name__}: {exc}"
        finally:
            task.files = _changed(task.before, _snapshot(task.workspace))
            task.finished_at = time.time()


REGISTRY = CodeTaskRegistry()
