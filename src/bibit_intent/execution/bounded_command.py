from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time

from bibit_intent.execution.cancellation import raise_if_cancelled


class OutputLimitExceeded(RuntimeError):
    pass


def run_bounded_command(
    cmd: list[str], *, timeout: float, env: dict[str, str] | None = None,
    input: str | None = None, max_output_bytes: int = 8_000_000,
) -> subprocess.CompletedProcess[str]:
    if max_output_bytes < 1 or timeout <= 0:
        raise ValueError("positive output limit and timeout required")
    raise_if_cancelled()
    payload = (input or "").encode("utf-8")
    process = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, start_new_session=True,
    )
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    total = 0
    offset = 0
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for name in buffers:
                stream = getattr(process, name)
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            if payload:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            else:
                process.stdin.close()
            while selector.get_map() or process.poll() is None:
                raise_if_cancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(cmd, timeout)
                for key, _ in selector.select(min(0.05, remaining)):
                    if key.data == "stdin":
                        try:
                            offset += os.write(key.fd, payload[offset:offset + 65536])
                        except BrokenPipeError:
                            offset = len(payload)
                        except BlockingIOError:
                            continue
                        if offset >= len(payload):
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                        continue
                    try:
                        chunk = os.read(key.fd, min(65536, max_output_bytes - total + 1))
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    else:
                        total += len(chunk)
                        if total > max_output_bytes:
                            raise OutputLimitExceeded("command output exceeded byte limit")
                        buffers[key.data].extend(chunk)
            return subprocess.CompletedProcess(
                cmd, process.returncode,
                buffers["stdout"].decode("utf-8", errors="replace"),
                buffers["stderr"].decode("utf-8", errors="replace"),
            )
    finally:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                break
        process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
