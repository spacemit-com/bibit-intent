import hashlib
from pathlib import Path

import pytest

from bibit_intent.models import gateway_migration as migration
from bibit_intent.models.gateway_artifacts import Artifact


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    content = b"GGUFmigration-test"
    artifact = Artifact("https://example.test/model.gguf", len(content), hashlib.sha256(content).hexdigest())
    monkeypatch.setattr(migration, "ARTIFACTS", {"llm": artifact})
    source = tmp_path / "old/model.gguf"
    source.parent.mkdir()
    source.write_bytes(content)
    directory = tmp_path / "gateway/llm"
    directory.mkdir(parents=True)
    monkeypatch.setenv('BIBIT_INTENT_GATEWAY_MODELS_ROOT', str(directory.parent))

    class Transport:
        base_url = 'http://127.0.0.1:18790'
        rows = []

        def request(self, path, payload=None):
            if path.endswith('/models'):
                return list(self.rows)
            raise AssertionError(path)

    transport = Transport()
    options = {"legacy_home": tmp_path / "home", "legacy_system": source.parent, "cleanup": True}
    return transport, source, directory, options


def test_migrate_then_cleanup_is_idempotent(scenario):
    transport, source, directory, options = scenario
    unrelated = source.parent / "other.gguf"
    unrelated.write_bytes(b"untouched")
    result = migration.migrate_models(transport, {"llm": "glm-edge-1.5b-chat-q4_0"}, **options)
    assert result["cleanup_pending"] == []
    assert not source.exists()
    assert (directory / source.name).read_bytes() == b"GGUFmigration-test"
    assert unrelated.read_bytes() == b"untouched"
    migration.migrate_models(transport, {"llm": "glm-edge-1.5b-chat-q4_0"}, **options)
    assert (directory / source.name).read_bytes() == b"GGUFmigration-test"


def test_migration_failure_keeps_old_file(scenario, monkeypatch):
    transport, source, directory, options = scenario

    def fail_copy(*args, **kwargs):
        raise OSError('disk full')

    monkeypatch.setattr(migration, 'copy_model', fail_copy)
    result = migration.migrate_models(transport, {"llm": "glm-edge-1.5b-chat-q4_0"}, **options)
    assert source.exists()
    assert str(source) in result["cleanup_pending"]
    assert not (directory / source.name).exists()


def test_other_registration_keeps_shared_old_copy(scenario):
    transport, source, directory, options = scenario
    transport.rows = [{"id": "another-app", "source_type": "local_path",
                       "local_path": str(source), "status": "downloaded"}]
    migration.migrate_models(transport, {"llm": "glm-edge-1.5b-chat-q4_0"}, **options)
    assert source.exists()


def test_missing_models_never_download(scenario):
    transport, source, directory, options = scenario
    source.unlink()
    result = migration.migrate_models(transport, {"llm": "glm-edge-1.5b-chat-q4_0"}, **options)
    assert result["migrated"] == []
    assert not any(path.endswith('/download') for path, _ in [(p, None) for p in []])


def test_unregistered_existing_model_is_imported(scenario):
    transport, source, directory, options = scenario
    result = migration.migrate_models(transport, {"llm": "glm-edge-1.5b-chat-q4_0"}, **options)
    assert not source.exists()
    assert (directory / source.name).read_bytes() == b"GGUFmigration-test"
    assert result["migrated"][0]["path"] == str(directory / source.name)


def test_corrupt_source_is_not_deleted(scenario):
    transport, source, directory, options = scenario
    source.write_bytes(b"bad")
    result = migration.migrate_models(transport, {"llm": "glm-edge-1.5b-chat-q4_0"}, **options)
    assert source.read_bytes() == b"bad"
    assert not (directory / source.name).exists()
    assert result["migrated"] == []


def test_storage_is_owned_by_bibit_configuration(tmp_path, monkeypatch):
    class Transport:
        base_url = 'http://127.0.0.1:18790'

    monkeypatch.delenv('BIBIT_INTENT_GATEWAY_MODELS_ROOT', raising=False)
    assert migration.storage_directory(Transport(), 'embed', gateway_root=tmp_path) == tmp_path / '.cache/models/embed'
    monkeypatch.setenv('BIBIT_INTENT_GATEWAY_MODELS_ROOT', str(tmp_path / 'shared'))
    assert migration.storage_directory(Transport(), 'llm') == tmp_path / 'shared/llm'
    monkeypatch.setenv('BIBIT_INTENT_GATEWAY_MODELS_ROOT', 'relative')
    with pytest.raises(ValueError, match='absolute'):
        migration.storage_directory(Transport(), 'llm')
    Transport.base_url = 'https://other-host.example'
    with pytest.raises(ValueError, match='this host'):
        migration.storage_directory(Transport(), 'llm')


def test_deregister_obsolete_models_removes_legacy_ids():
    class Transport:
        base_url = 'http://127.0.0.1:18790'
        calls = []

        def request(self, path, payload=None):
            self.calls.append((path, payload))
            if path.endswith('/models'):
                return [{"id": "bibit-llm", "source_type": "local_path", "status": "loaded"}]
            return {}

    transport = Transport()
    result = migration.deregister_obsolete_models(transport)
    assert result == ["llm:bibit-llm"]
    assert any(path.endswith('/unload') for path, _ in transport.calls)
    assert any(path.endswith('/deregister') for path, _ in transport.calls)


def test_deregister_obsolete_models_noops_without_legacy_ids():
    class Transport:
        base_url = 'http://127.0.0.1:18790'
        calls = []

        def request(self, path, payload=None):
            self.calls.append((path, payload))
            if path.endswith('/models'):
                return [{"id": "glm-edge-1.5b-chat-q4_0", "source_type": "local_url",
                         "status": "loaded"}]
            return {}

    transport = Transport()
    assert migration.deregister_obsolete_models(transport) == []
    assert not any(path.endswith('/deregister') for path, _ in transport.calls)


def test_gateway_root_independent_of_legacy_home(tmp_path, monkeypatch):
    """Gateway 存储根不得复用 legacy_home（调用方 HOME）。

    Gateway 以其它用户运行时，迁移目标应是 Gateway 自己的存储目录，
    而不是旧 Bibit 模型所在（调用方 HOME）的位置。
    """
    content = b"GGUFmigration-test"
    artifact = Artifact("https://example.test/model.gguf", len(content), hashlib.sha256(content).hexdigest())
    monkeypatch.setattr(migration, "ARTIFACTS", {"llm": artifact})
    legacy_home = tmp_path / "caller-home"
    source = legacy_home / ".bibit-intent/models/model.gguf"
    source.parent.mkdir(parents=True)
    source.write_bytes(content)
    gateway_root = tmp_path / "gateway-home"

    class Transport:
        base_url = 'http://127.0.0.1:18790'
        rows = []

        def request(self, path, payload=None):
            if path.endswith('/models'):
                return list(self.rows)
            raise AssertionError(path)

    monkeypatch.delenv('BIBIT_INTENT_GATEWAY_MODELS_ROOT', raising=False)
    result = migration.migrate_models(
        Transport(), {"llm": "glm-edge-1.5b-chat-q4_0"},
        legacy_home=legacy_home, gateway_root=gateway_root,
    )
    assert result["migrated"][0]["path"] == str(gateway_root / ".cache/models/llm/model.gguf")
    assert (gateway_root / ".cache/models/llm/model.gguf").read_bytes() == content
    assert not (legacy_home / ".cache/models/llm/model.gguf").exists()
