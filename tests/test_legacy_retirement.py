from pathlib import Path
import os
import socket
import subprocess

import pytest


@pytest.fixture
def migration(tmp_path):
    root = Path(__file__).resolve().parents[1]
    home = tmp_path / "home/bibit"
    home.mkdir(parents=True)
    log = tmp_path / "commands.log"
    binaries = tmp_path / "bin"
    binaries.mkdir()
    commands = {
        "id": "echo 0",
        "getent": f"echo 'bibit:x:1000:1000:Bibit:{home}:/bin/sh'",
        "systemctl": f'''echo "$*" >> '{log}'
case "$*" in
    *show*) echo loaded ;;
    *stop*) [ "${{FAIL_STOP:-0}}" != 1 ] ;;
esac''',
        "runuser": 'shift 3; exec "$@"',
    }
    for name, body in commands.items():
        executable = binaries / name
        executable.write_text("#!/bin/sh\n" + body + "\n")
        executable.chmod(0o755)
    runtime = tmp_path / "run/user/1000"
    runtime.mkdir(parents=True)
    (tmp_path / "run/systemd/system").mkdir(parents=True)
    bus = socket.socket(socket.AF_UNIX)
    bus.bind(str(runtime / "bus"))
    source = (root / "deploy/retire-legacy-models.sh").read_text()
    for prefix in ("/var/lib/", "/etc/", "/usr/lib/", "/opt/", "/run/"):
        source = source.replace(prefix, str(tmp_path) + prefix)
    script = tmp_path / "retire.sh"
    script.write_text(source)
    env = {**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}"}
    yield tmp_path, home, script, env, log
    bus.close()


def test_retirement_preserves_models_and_other_services(migration):
    root, home, script, env, log = migration
    for directory in (root / "etc/systemd/system", root / "etc/systemd/user",
                      root / "usr/lib/systemd/user", home / ".config/systemd/user"):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "bibit-intent-llm.service").write_text("legacy")
        (directory / "bibit-intent-embed.service.d").mkdir()
        (directory / "bibit-intent-embed.service.d/custom.conf").write_text("custom")
        wants = directory / "default.target.wants"
        wants.mkdir()
        (wants / "bibit-intent-llm.service").symlink_to("/missing/legacy")
        (directory / "memory.service").write_text("keep")
    model = root / "opt/bibit-intent/models/existing.gguf"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"GGUFkeep")
    old_script = model.parent.parent / "download_llm_model.sh"
    old_script.write_text("legacy downloader")
    bytecode = root / "opt/bibit-intent/src/bibit_intent/models/__pycache__/model_manager.cpython-314.pyc"
    bytecode.parent.mkdir(parents=True)
    bytecode.write_bytes(b"legacy bytecode")
    for unused in range(2):
        result = subprocess.run(["sh", str(script)], env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    assert model.read_bytes() == b"GGUFkeep"
    assert not old_script.exists()
    assert not bytecode.exists()
    assert not (home / ".config/systemd/user/bibit-intent-llm.service").exists()
    assert not (home / ".config/systemd/user/default.target.wants/bibit-intent-llm.service").is_symlink()
    assert (home / ".config/systemd/user/memory.service").read_text() == "keep"
    backups = root / "var/lib/bibit-intent/retired-legacy"
    assert list(backups.rglob("custom.conf"))
    assert list(backups.rglob("download_llm_model.sh"))
    assert list(backups.rglob("model_manager.cpython-314.pyc"))
    actions = log.read_text()
    assert "--user stop bibit-intent-llm.service" in actions
    assert "stop bibit-intent-embed.service" in actions
    assert "memory" not in actions and "spacemit-ai-gateway" not in actions


def test_stop_failure_aborts_retirement(migration):
    root, home, script, env, log = migration
    unit = root / "etc/systemd/system/bibit-intent-llm.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("legacy")
    result = subprocess.run(["sh", str(script)], env={**env, "FAIL_STOP": "1"}, capture_output=True)
    assert result.returncode != 0
    assert unit.read_text() == "legacy"
