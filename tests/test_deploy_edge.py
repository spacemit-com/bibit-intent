import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest


spec = importlib.util.spec_from_file_location("deploy_edge", Path(__file__).parents[1] / "scripts" / "deploy_edge.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


@pytest.fixture
def release(tmp_path, monkeypatch):
    device = tmp_path / "device"
    repo = Path(__file__).parents[1]
    for name in deploy.FILES:
        path = deploy.source(device) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((deploy.source(repo) / name).read_bytes())
    added = deploy.source(device) / "core/package_checks.py"
    added.unlink()
    baseline = deploy.snapshot(device)
    bundle = tmp_path / "bundle.tgz"
    deploy.build(repo, baseline, bundle)
    events = []
    monkeypatch.setattr(deploy, "service", lambda user, action: events.append(action))
    monkeypatch.setattr(deploy, "healthy", lambda: None)
    monkeypatch.setattr(deploy, "smoke", lambda: None)
    return device, bundle, baseline, events


def test_apply_and_restore_verified_files(release):
    device, bundle, baseline, events = release
    backup = deploy.apply(device, bundle, "bianbu")
    assert deploy.snapshot(device) == deploy.read_bundle(bundle)[0]["files"]
    assert events == ["stop", "start"]
    deploy.restore(device, backup)
    assert deploy.snapshot(device) == baseline


def test_baseline_drift_blocks_before_service_change(release):
    device, bundle, _, events = release
    (deploy.source(device) / "pipeline.py").write_text("changed = True\n")
    with pytest.raises(ValueError, match="changed since baseline"):
        deploy.apply(device, bundle, "bianbu")
    assert events == []


def test_health_failure_rolls_back(release, monkeypatch):
    device, bundle, baseline, events = release
    monkeypatch.setattr(deploy, "smoke", lambda: (_ for _ in ()).throw(RuntimeError("smoke failed")))
    with pytest.raises(RuntimeError, match="smoke failed"):
        deploy.apply(device, bundle, "bianbu")
    assert deploy.snapshot(device) == baseline
    assert events == ["stop", "start", "stop", "start"]


def test_rollback_refuses_later_edits(release):
    device, bundle, _, _ = release
    backup = deploy.apply(device, bundle, "bianbu")
    (deploy.source(device) / "pipeline.py").write_text("later = True\n")
    with pytest.raises(ValueError, match="subsequent change"):
        deploy.restore(device, backup)


@pytest.mark.parametrize("name", ["../escape.py", "/tmp/escape.py"])
def test_archive_traversal_rejected(tmp_path, name):
    bundle = tmp_path / "bad.tgz"
    with tarfile.open(bundle, "w:gz") as archive:
        member = tarfile.TarInfo(name)
        member.size = 1
        archive.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="unsafe"):
        deploy.read_bundle(bundle)


def test_symlink_destination_rejected(release, tmp_path):
    device, bundle, _, events = release
    path = deploy.source(device) / "pipeline.py"
    path.unlink()
    path.symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="symlink"):
        deploy.apply(device, bundle, "bianbu")
    assert not events


def test_apply_restores_missing_runtime_module(tmp_path, monkeypatch):
    """An older device missing a newly deployed module must be importable after apply."""
    repo = Path(__file__).parents[1]
    device = tmp_path / "old-device"
    for name in deploy.FILES:
        path = deploy.source(device) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((deploy.source(repo) / name).read_bytes())
    (deploy.source(device) / "core/package_target.py").unlink()

    baseline = deploy.snapshot(device)
    bundle = tmp_path / "bundle.tgz"
    deploy.build(repo, baseline, bundle)
    monkeypatch.setattr(deploy, "service", lambda user, action: None)
    monkeypatch.setattr(deploy, "healthy", lambda: None)
    monkeypatch.setattr(deploy, "smoke", lambda: None)

    deploy.apply(device, bundle, "bianbu")
    env = {**os.environ, "PYTHONPATH": str(deploy.source(device).parent)}
    probe = subprocess.run(
        [sys.executable, "-c", "from bibit_intent.core.package_target import is_exact_package; assert is_exact_package('g++')"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr


def test_apply_deploys_a_loadable_appcli_skill(tmp_path, monkeypatch):
    """appcli 技能（H01–H07/H15）落到设备后必须真的能加载：注册表 + 锁定表 + handlers。"""
    repo = Path(__file__).parents[1]
    device = tmp_path / "old-device"
    for name in deploy.FILES:
        path = deploy.source(device) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((deploy.source(repo) / name).read_bytes())
    # 老设备上这些文件还不存在：部署必须能把它们补上（而不是只在已有文件上原地替换）。
    for name in (
        "core/appcli_registry.py", "resources/appcli_registry.yaml",
        "resources/appcli_wheels.yaml", "resources/skills/appcli/skill.yaml",
    ):
        (deploy.source(device) / name).unlink()

    baseline = deploy.snapshot(device)
    bundle = tmp_path / "bundle.tgz"
    deploy.build(repo, baseline, bundle)
    monkeypatch.setattr(deploy, "service", lambda user, action: None)
    monkeypatch.setattr(deploy, "healthy", lambda: None)
    monkeypatch.setattr(deploy, "smoke", lambda: None)

    deploy.apply(device, bundle, "bianbu")
    env = {**os.environ, "PYTHONPATH": str(deploy.source(device).parent)}
    probe = subprocess.run(
        [
            sys.executable, "-c",
            "from bibit_intent.resources import resource_path\n"
            "from bibit_intent.core.registry import ToolRegistry\n"
            "from bibit_intent.core.appcli_registry import AppCliRegistry\n"
            "from bibit_intent.core.appcli_wheels import load_lock\n"
            "from bibit_intent.execution.skill_loader import load_skills\n"
            "registry = ToolRegistry(resource_path('tools.yaml'))\n"
            "loaded = load_skills(resource_path('skills'), registry, {})\n"
            "names = {skill.name for skill in loaded}\n"
            "assert 'appcli' in names, names\n"
            "assert AppCliRegistry.load().specs()\n"
            "assert load_lock().packages\n"
            "print('appcli ok')",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    assert "appcli ok" in probe.stdout
