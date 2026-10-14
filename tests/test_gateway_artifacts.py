import hashlib
import io
from types import SimpleNamespace

import pytest

from bibit_intent.models import gateway_artifacts as artifacts


@pytest.mark.parametrize("resume,ignored", [(False, False), (True, False), (True, True)])
def test_verified_download(tmp_path, monkeypatch, resume, ignored):
    content = b"GGUFtest-model"
    artifact = artifacts.Artifact("https://example.test/model.gguf", len(content), hashlib.sha256(content).hexdigest())
    if resume:
        (tmp_path / "model.gguf.part").write_bytes(content[:4])
    offset = 4 if resume and not ignored else 0
    response = io.BytesIO(content[offset:])
    response.status = 206 if offset else 200
    response.headers = {"Content-Length": str(len(content) - offset)}
    if offset:
        response.headers["Content-Range"] = f"bytes {offset}-{len(content)-1}/{len(content)}"
    monkeypatch.setattr(artifacts, "build_opener", lambda *_: SimpleNamespace(open=lambda *args, **kwargs: response))
    destination = artifacts.download_artifact(artifact, tmp_path, progress=lambda *_: None)
    assert destination.read_bytes() == content
    assert not (tmp_path / "model.gguf.part").exists()
    assert artifacts.download_artifact(artifact, tmp_path) == destination


def test_bad_hash_never_promoted(tmp_path):
    artifact = artifacts.Artifact("https://example.test/model.gguf", 4, "0" * 64)
    (tmp_path / "model.gguf.part").write_bytes(b"GGUF")
    with pytest.raises(ValueError, match="SHA256"):
        artifacts.download_artifact(artifact, tmp_path)
    assert not (tmp_path / "model.gguf").exists()
    assert not (tmp_path / "model.gguf.part").exists()


def test_disk_preflight(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts.shutil, "disk_usage", lambda *_: SimpleNamespace(free=0))
    with pytest.raises(OSError, match="disk space"):
        artifacts.download_artifact(artifacts.ARTIFACTS["embed"], tmp_path)


def test_cancelled_download_preserves_partial_without_network(tmp_path, monkeypatch):
    artifact = artifacts.ARTIFACTS['embed']
    partial = tmp_path / (artifact.url.rsplit('/', 1)[-1] + '.part')
    partial.write_bytes(b'GGUF')
    opener = lambda *args: pytest.fail('Cancelled download must not access network')
    monkeypatch.setattr(artifacts, 'build_opener', opener)
    with pytest.raises(InterruptedError):
        artifacts.download_artifact(artifact, tmp_path, cancelled=lambda: True)
    assert partial.read_bytes() == b'GGUF'


def test_reject_insecure_source(tmp_path):
    with pytest.raises(ValueError, match="HTTPS"):
        artifacts.download_artifact(artifacts.Artifact("http://example.test/model", 4, ""), tmp_path)


def test_invalid_range_preserves_partial(tmp_path, monkeypatch):
    (tmp_path / "model.gguf.part").write_bytes(b"GG")
    response = io.BytesIO(b"UF")
    response.status = 206
    response.headers = {"Content-Range": "bytes 0-1/4"}
    monkeypatch.setattr(artifacts, "build_opener", lambda *_: SimpleNamespace(open=lambda *args, **kwargs: response))
    with pytest.raises(ValueError, match="Content-Range"):
        artifacts.download_artifact(artifacts.Artifact("https://example.test/model.gguf", 4, ""), tmp_path)
    assert (tmp_path / "model.gguf.part").read_bytes() == b"GG"


def test_interruption_retains_partial(tmp_path, monkeypatch):
    content = b"GGUFtest"
    response = io.BytesIO(content)
    response.status = 200
    response.headers = {}
    monkeypatch.setattr(artifacts, "build_opener", lambda *_: SimpleNamespace(open=lambda *args, **kwargs: response))

    def interrupt(*args):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        artifacts.download_artifact(artifacts.Artifact("https://example.test/model.gguf", len(content), ""), tmp_path, interrupt)
    assert (tmp_path / "model.gguf.part").read_bytes() == content
    assert not (tmp_path / "model.gguf").exists()
