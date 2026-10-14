"""Pinned host-side artifacts for explicit Gateway provisioning."""
from __future__ import annotations

from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import shutil
from urllib.request import HTTPRedirectHandler, Request, build_opener


@dataclass(frozen=True)
class Artifact:
    url: str
    size: int
    sha256: str


ARTIFACTS = {
    "llm": Artifact(
        "https://archive.spacemit.com/spacemit-ai/model_zoo/llm/glm-edge-1.5b-chat-q4_0.gguf",
        931268992, "db55499d77d3f41c0947a6b4901f6d0f4fba798c5acd88ad2e42c133e6be186d",
    ),
    "embed": Artifact(
        "https://archive.spacemit.com/spacemit-ai/model_zoo/embed/Qwen3-Embedding-0.6B-Q4_0.gguf",
        381925344, "d64c751c09425ab44bcf4a2f224c98ebfb789a6caada1e7a5e19a325268ca899",
    ),
}
RESERVE = 256 * 1024 * 1024
MODEL_LOAD_ARGS = {
    "llm": ("-c", "8192", "-t", "7", "--jinja"),
    "embed": ("--embedding", "--pooling", "mean", "--embd-normalize", "2",
              "-c", "2048", "-t", "4", "--cache-ram", "0"),
}


class SecureRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        if not newurl.startswith("https://"):
            raise ValueError("Model redirects must use HTTPS")
        return super().redirect_request(request, response, code, message, headers, newurl)


def verify(path: Path, artifact: Artifact) -> None:
    if path.stat().st_size != artifact.size:
        raise ValueError(f"Model size mismatch: {path}")
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if digest != artifact.sha256:
        raise ValueError(f"Model SHA256 mismatch: {path}")


def download_artifact(artifact: Artifact, directory: Path, progress=print, cancelled=lambda: False) -> Path:
    """Resume a pinned file; cancellation preserves partial data, never registers it."""
    if not artifact.url.startswith("https://"):
        raise ValueError("Model downloads require HTTPS")
    directory = directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / artifact.url.rsplit("/", 1)[-1]
    partial = destination.with_suffix(destination.suffix + ".part")
    lock = destination.with_suffix(destination.suffix + ".lock")
    if any(path.is_symlink() for path in (destination, partial, lock)):
        raise ValueError("Model artifact paths must not be symbolic links")
    with lock.open("a+b") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if cancelled():
            raise InterruptedError('Model download cancelled')
        if destination.exists():
            verify(destination, artifact)
            return destination
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > artifact.size:
            partial.unlink()
            offset = 0
        if shutil.disk_usage(directory).free < artifact.size - offset + RESERVE:
            raise OSError("Insufficient disk space for model and 256 MiB reserve")
        with partial.open("ab"):
            pass
        if offset < artifact.size:
            headers = {"Accept-Encoding": "identity"}
            if offset:
                headers["Range"] = f"bytes={offset}-"
            with build_opener(SecureRedirect()).open(Request(artifact.url, headers=headers), timeout=60) as response:
                if response.status == 206:
                    expected = f"bytes {offset}-{artifact.size - 1}/{artifact.size}"
                    if response.headers.get("Content-Range") != expected:
                        raise ValueError("Invalid model Content-Range")
                elif response.status == 200:
                    offset = 0
                else:
                    raise ValueError(f"Unexpected download status: {response.status}")
                if response.headers.get("Content-Encoding", "identity") != "identity":
                    raise ValueError("Unexpected model content encoding")
                length = response.headers.get("Content-Length")
                if length is not None and int(length) != artifact.size - offset:
                    raise ValueError("Unexpected model Content-Length")
                if shutil.disk_usage(directory).free < artifact.size - offset + RESERVE:
                    raise OSError("Insufficient disk space")
                with partial.open("ab" if offset else "wb") as handle:
                    reported = -1
                    while chunk := response.read(1024 * 1024):
                        if cancelled():
                            raise InterruptedError('Model download cancelled')
                        if offset + len(chunk) > artifact.size:
                            raise ValueError("Model exceeds pinned size")
                        if shutil.disk_usage(directory).free < len(chunk) + RESERVE:
                            raise OSError("Disk reserve reached")
                        handle.write(chunk)
                        offset += len(chunk)
                        percent = offset * 100 // artifact.size
                        if percent != reported:
                            progress(f"{destination.name}: {offset}/{artifact.size} ({percent}%)")
                            reported = percent
                    handle.flush()
                    os.fsync(handle.fileno())
        try:
            verify(partial, artifact)
        except ValueError:
            if partial.stat().st_size == artifact.size:
                partial.unlink()
            raise
        if cancelled():
            raise InterruptedError('Model download cancelled')
        os.replace(partial, destination)
        progress(f"Verified SHA256: {destination.name}")
        return destination
