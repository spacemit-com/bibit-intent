"""Update state-machine tests; no host package changes."""
import importlib.util
import io
import json
import os
import stat
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('failed', [False, True])
def test_stale_marker_does_not_suppress_result(coordinator, monkeypatch, tmp_path, failed):
    marker = tmp_path / 'stale.recovery'
    marker.write_text('{"pending": true}')
    monkeypatch.setattr(coordinator, 'recovery_marker', lambda uid: marker)
    monkeypatch.setattr(coordinator.sys, 'argv', ['update', '--uid', '1000'])
    notifications = []
    monkeypatch.setattr(coordinator, 'dialog', lambda *args: notifications.append(args))

    def update(uid, session):
        if failed:
            raise RuntimeError('upgrade failed')
        coordinator.status('succeeded')

    monkeypatch.setattr(coordinator, 'update', update)
    if failed:
        with pytest.raises(RuntimeError):
            coordinator.main()
    else:
        coordinator.main()
    assert len(notifications) == 1
    assert ('--error' if failed else '--info') in notifications[0][2]


def test_failed_marker_write_does_not_delegate(coordinator, monkeypatch):
    coordinator.RECOVERY_DELEGATED = True
    monkeypatch.setattr(coordinator.pwd, 'getpwuid', lambda uid: SimpleNamespace(pw_gid=1000))
    monkeypatch.setattr(coordinator, '_prepare_recovery_directory', lambda *args: None)

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(coordinator.subprocess, 'run', fail)
    coordinator._write_recovery_marker(1000)
    assert coordinator.RECOVERY_DELEGATED is False


def test_root_owned_stale_recovery_directory_is_repaired(coordinator, monkeypatch, tmp_path):
    uid = os.getuid()
    real_run = subprocess.run

    def run_as_current_user(*args, **kwargs):
        for key in ('user', 'group', 'extra_groups'):
            kwargs.pop(key, None)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(coordinator.subprocess, 'run', run_as_current_user)
    directory = tmp_path / 'bibit-intent'
    directory.mkdir()
    (directory / 'system-update.recovery').write_text('{"pending": true}')
    (directory / 'system-update.lock').write_text('stale')
    marker = directory / 'system-update.recovery'
    monkeypatch.setattr(coordinator, 'recovery_marker', lambda _uid: marker)
    coordinator._write_recovery_marker(uid)
    assert coordinator.RECOVERY_DELEGATED
    assert json.loads(marker.read_text())['job_id'] == coordinator.JOB_ID
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_unsafe_recovery_directory_falls_back_to_coordinator(coordinator, monkeypatch, tmp_path):
    marker = tmp_path / 'bibit-intent' / 'system-update.recovery'
    marker.parent.symlink_to(tmp_path, target_is_directory=True)
    monkeypatch.setattr(coordinator, 'recovery_marker', lambda _uid: marker)
    coordinator._write_recovery_marker(os.getuid())
    assert not coordinator.RECOVERY_DELEGATED


def test_status_is_readable_and_job_identity_survives_phases(coordinator):
    previous = os.umask(0o077)
    try:
        coordinator.status('agent_upgrading')
        first = json.loads(coordinator.STATE.with_suffix('.json').read_text())
        coordinator.status('succeeded')
    finally:
        os.umask(previous)
    last = json.loads(coordinator.STATE.with_suffix('.json').read_text())
    assert first['job_id'] == last['job_id'] == coordinator.JOB_ID
    assert stat.S_IMODE(coordinator.STATE.with_suffix('.json').stat().st_mode) == 0o644


@pytest.fixture
def coordinator(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / 'deploy/bibit-system-update.py'
    spec = importlib.util.spec_from_file_location('system_update', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'STATE', tmp_path / 'status')
    original = module.tempfile.TemporaryDirectory
    monkeypatch.setattr(module.tempfile, 'TemporaryDirectory', lambda **kw: original(dir=tmp_path))
    return module


@pytest.mark.parametrize('decision', ['allow', 'skip', 'cancel', 'normal'])
def test_update_choices(coordinator, monkeypatch, decision):
    m = coordinator
    lower = {'demo': ['2.0', '1.0']}
    upgrade = {'other': ['1.0', '2.0']}
    initial = upgrade if decision == 'normal' else {**lower, **upgrade}
    calls = []
    states = []
    original = m.status

    def record(value, **details):
        states.append(value)
        original(value, **details)

    monkeypatch.setattr(m, 'status', record)
    monkeypatch.setattr(m, 'run', lambda cmd, capture=False: calls.append(cmd) or '')
    monkeypatch.setattr(m, 'simulate', lambda options: upgrade if options else initial)
    monkeypatch.setattr(m, 'recover_interrupted_dpkg', lambda: None)
    monkeypatch.setattr(m, 'unhold_self_packages', lambda: [])
    monkeypatch.setattr(m, 'confirm', lambda *args: decision)
    monkeypatch.setattr(m, 'skip_options', lambda *args: ['-o', 'skip=true'])
    m.update(1000, {})
    assert calls[0][-1] == 'update'
    assert states[:2] == ['refreshing', 'checking']
    assert ('awaiting_confirmation' in states) == (decision != 'normal')
    if decision == 'cancel':
        assert len(calls) == 1
        assert states[-1] == 'cancelled'
    else:
        assert len(calls) == 2
        command = calls[-1]
        assert 'install' in command
        assert ('--allow-downgrades' in command) == (decision == 'allow')
        assert '--no-remove' in command
        assert any('DPkg::Pre-Install-Pkgs' in arg for arg in command)
        assert states[-1] == ('succeeded_with_skips' if decision == 'skip' else 'succeeded')


def test_plan_changed_after_confirmation(coordinator, monkeypatch):
    m = coordinator
    plans = iter([{'demo': ['2', '1']}, {'demo': ['2', '0.5']}])
    calls = []
    monkeypatch.setattr(m, 'run', lambda cmd, *a: calls.append(cmd))
    monkeypatch.setattr(m, 'simulate', lambda options: next(plans))
    monkeypatch.setattr(m, 'recover_interrupted_dpkg', lambda: None)
    monkeypatch.setattr(m, 'unhold_self_packages', lambda: [])
    monkeypatch.setattr(m, 'confirm', lambda *args: 'allow')
    with pytest.raises(RuntimeError, match='plan changed'):
        m.update(1000, {})
    assert len(calls) == 1


@pytest.mark.parametrize('unsafe', [{'demo': ['2', '1']}, {'other': ['3', '1']}])
def test_skip_rejects_remaining_downgrades(coordinator, monkeypatch, unsafe):
    m = coordinator
    plans = iter([{'demo': ['2', '1']}, unsafe])
    monkeypatch.setattr(m, 'run', lambda *args: '')
    monkeypatch.setattr(m, 'simulate', lambda options: next(plans))
    monkeypatch.setattr(m, 'recover_interrupted_dpkg', lambda: None)
    monkeypatch.setattr(m, 'unhold_self_packages', lambda: [])
    monkeypatch.setattr(m, 'confirm', lambda *args: 'skip')
    monkeypatch.setattr(m, 'skip_options', lambda *args: [])
    with pytest.raises(RuntimeError, match='Cannot skip'):
        m.update(1000, {})


def test_refresh_failure_stops_update(coordinator, monkeypatch):
    def fail(*args):
        raise RuntimeError('refresh failed')
    monkeypatch.setattr(coordinator, 'run', fail)
    monkeypatch.setattr(coordinator, 'simulate', lambda *args: pytest.fail('must not solve'))
    with pytest.raises(RuntimeError, match='refresh failed'):
        coordinator.update(1000, {})


def test_parse_and_debian_versions(coordinator):
    plan = coordinator.parse_plan('Inst demo:arm64 [1:2.0-1] (1:1.9-1 repo [arm64])\n'
                                  'Inst other (3.0 repo [arm64])\nConf other (3.0 repo [arm64])')
    assert coordinator.downgrades(plan) == {'demo:arm64': ['1:2.0-1', '1:1.9-1']}
    assert coordinator.downgrades({'demo': ['1.0~rc1', '1.0']}) == {}
    for text in ('Remv demo [1]', 'Inst INVALID'):
        with pytest.raises(RuntimeError):
            coordinator.parse_plan(text)


def test_split_plan_separates_self_packages(coordinator):
    plan = {
        'vim': ['1', '2'],
        'bibit-intent:riscv64': ['1', '2'],
        'bianbu-agent-runtime': ['1', '2'],
    }
    normal, agent = coordinator.split_plan(plan)
    assert normal == {'vim': ['1', '2']}
    assert agent == {'bibit-intent:riscv64': ['1', '2'], 'bianbu-agent-runtime': ['1', '2']}


def test_unhold_self_packages_only_targets_held_self_packages(coordinator, monkeypatch):
    m = coordinator
    calls = []

    def fake_run(cmd, capture=False):
        calls.append(cmd)
        if cmd[:2] == ['apt-mark', 'showhold']:
            return 'bibit-intent\nbianbu-agent-runtime\nsome-other-package\n'
        return ''

    monkeypatch.setattr(m, 'run', fake_run)
    targets = m.unhold_self_packages()
    assert targets == ['bianbu-agent-runtime', 'bibit-intent']
    assert calls == [
        ['apt-mark', 'showhold'],
        ['apt-mark', 'unhold', 'bianbu-agent-runtime', 'bibit-intent'],
    ]


def test_unhold_self_packages_noop_when_nothing_held(coordinator, monkeypatch):
    m = coordinator
    calls = []
    monkeypatch.setattr(m, 'run', lambda cmd, capture=False: calls.append(cmd) or '')
    assert m.unhold_self_packages() == []
    assert calls == [['apt-mark', 'showhold']]


def test_recover_interrupted_dpkg_runs_configure(coordinator, monkeypatch):
    m = coordinator
    calls = []
    monkeypatch.setattr(m, 'run', lambda cmd, capture=False: calls.append(cmd) or '')
    m.recover_interrupted_dpkg()
    assert calls == [['dpkg', '--configure', '-a']]


def test_recover_interrupted_dpkg_swallows_failure(coordinator, monkeypatch):
    m = coordinator
    def fail(cmd, capture=False):
        raise RuntimeError('dpkg broken')
    monkeypatch.setattr(m, 'run', fail)
    m.recover_interrupted_dpkg()


def test_update_runs_agent_phase_after_normal_packages(coordinator, monkeypatch):
    m = coordinator
    plan = {'vim': ['1', '2'], 'bibit-intent': ['1', '2']}
    calls = []
    states = []
    original = m.status

    def record(value, **details):
        states.append(value)
        original(value, **details)

    agent_calls = []
    monkeypatch.setattr(m, 'status', record)
    monkeypatch.setattr(m, 'run', lambda cmd, capture=False: calls.append(cmd) or '')
    monkeypatch.setattr(m, 'simulate', lambda options: plan)
    monkeypatch.setattr(m, 'recover_interrupted_dpkg', lambda: None)
    monkeypatch.setattr(m, 'unhold_self_packages', lambda: [])
    monkeypatch.setattr(m, 'confirm', lambda *args: 'normal')
    monkeypatch.setattr(m, 'skip_options', lambda *args: [])
    monkeypatch.setattr(m, 'run_agent_upgrade', lambda *args: agent_calls.append(args))

    m.update(1000, {})

    assert states[:2] == ['refreshing', 'checking']
    assert 'updating' in states
    assert 'system_succeeded' in states
    assert states[-1] == 'succeeded'
    assert agent_calls == [(1000, {}, {'bibit-intent': ['1', '2']}, 'normal')]


def test_agent_upgrade_ignores_closed_progress_window(coordinator, monkeypatch):
    m = coordinator
    monkeypatch.setattr(m, '_write_recovery_marker', lambda uid: None)

    class BrokenStream:
        def write(self, value):
            raise BrokenPipeError()

        def flush(self):
            pass

        def close(self):
            pass

    process = SimpleNamespace(
        stdin=BrokenStream(),
        stdout=io.StringIO('STATUS starting\nSTATUS upgrading\n'),
        wait=lambda timeout=10: 0,
        terminate=lambda: None,
    )

    def fake_popen(command, **kwargs):
        if command[0] == m.AGENT_UPGRADE_SCRIPT:
            assert command[1:3] == ['--uid', '1000']
        return process

    monkeypatch.setattr(m, '_dialog_env', lambda uid, session: ({}, SimpleNamespace(pw_gid=100, pw_dir='/tmp')))
    monkeypatch.setattr(m.subprocess, 'Popen', fake_popen)

    m.run_agent_upgrade(1000, {'DISPLAY': ':0'}, {'bibit-intent': ['1', '2']}, 'normal')


@pytest.mark.parametrize('choice', [None, '', 'unexpected', '取消本次更新'])
def test_confirmation_fails_closed(coordinator, monkeypatch, choice):
    monkeypatch.setattr(coordinator, 'dialog', lambda *args: choice)
    assert coordinator.confirm(1000, {}, {'demo': ['2', '1']}) == 'cancel'


def test_dialog_missing_desktop_or_root_does_not_spawn(coordinator, monkeypatch):
    monkeypatch.setattr(coordinator.subprocess, 'run', lambda *args, **kw: pytest.fail('must not spawn'))
    assert coordinator.dialog(0, {'DISPLAY': ':0'}, []) is None
    assert coordinator.dialog(1000, {}, []) is None


@pytest.mark.parametrize('row', [
    'demo 2 > 1 /cache/demo.deb',
    'demo 2 > 1 **CONFIGURE**',
])
def test_guard_approved(coordinator, row):
    coordinator.guard(io.StringIO('VERSION 2\nconfig=value\n\n' + row + '\n'), {'demo': ['2', '1']})


def test_guard_v3_arch_all_matches_bare_approved_name(coordinator):
    coordinator.guard(
        io.StringIO('VERSION 3\nconfig=value\n\ndemo 2 all none > 1 all none /cache/demo.deb\n'),
        {'demo': ['2', '1']},
    )


@pytest.mark.parametrize('text', [
    '/cache/demo.deb\n',
    'VERSION 2\n\ndemo 2 > 0.5 /cache/demo.deb\n',
    'VERSION 2\n\ndemo 2 > - **REMOVE**\n',
    'VERSION 2\n\nother 2 > 1 /cache/other.deb\n',
    'VERSION 2\n\nbad row\n',
])
def test_guard_rejects_changed_transaction(coordinator, text):
    with pytest.raises(RuntimeError):
        coordinator.guard(io.StringIO(text), {'demo': ['2', '1']})


def test_skip_pins_are_temporary(coordinator, monkeypatch, tmp_path):
    original = tmp_path / 'original'
    original.write_text('Package: *\nPin: release a=stable\nPin-Priority: 700\n')
    monkeypatch.setattr(coordinator, 'run', lambda *args: f"PREFERENCES='{original}'\n")
    options = coordinator.skip_options(tmp_path, {'demo': ['2', '1']})
    text = (tmp_path / 'preferences').read_text()
    assert 'Pin: version 2\nPin-Priority: 1001' in text
    assert text.endswith(original.read_text())
    assert options == ['-o', f'Dir::Etc::preferences={tmp_path / "preferences"}']


def test_shell_delegates_to_coordinator():
    path = Path(__file__).resolve().parents[1] / 'deploy/bibit-system-update.sh'
    subprocess.run(['/bin/sh', '-n', str(path)], check=True)
    source = path.read_text()
    assert '--allow-downgrades' not in source
    assert 'exec /usr/bin/python3 /opt/bibit-intent/deploy/bibit-system-update.py "$@"' in source


def test_agent_upgrade_script_emits_status_and_fails_on_error():
    path = Path(__file__).resolve().parents[1] / 'deploy/bibit-agent-upgrade.sh'
    subprocess.run(['/bin/sh', '-n', str(path)], check=True)
    source = path.read_text()
    assert 'STATUS ' in source
    assert '--allow-downgrades' in source
    assert 'exit 1' in source


def test_guard_approves_whole_transaction_not_just_normal(coordinator, monkeypatch, tmp_path):
    """normal 阶段被依赖卷入的 agent 包也必须处于 guard 的批准范围内。

    libsherpa-onnx-c-api（普通包）与 bibit-voice（自研组件）存在硬依赖，单独
    ``apt-get install --only-upgrade libsherpa-onnx-c-api`` 会被迫同时升级
    bibit-voice。guard 若只批准 normal 包，就会把这次“计划外”的 bibit-voice 升级
    误判成计划变化而使更新失败。approved 必须覆盖整个已确认事务（normal + agent）。
    """
    m = coordinator
    plan = {
        'libsherpa-onnx-c-api': ['0.1.4', '0.1.5'],
        'bibit-voice': ['0.1.4', '0.1.5'],
    }
    approved_seen = []

    def fake_run(command, capture=False):
        if 'install' in command:
            approved = next(tmp_path.rglob('approved.json'))
            approved_seen.append(json.loads(approved.read_text()))
        return ''

    monkeypatch.setattr(m, 'run', fake_run)
    monkeypatch.setattr(m, 'simulate', lambda options: plan)
    monkeypatch.setattr(m, 'recover_interrupted_dpkg', lambda: None)
    monkeypatch.setattr(m, 'unhold_self_packages', lambda: [])
    monkeypatch.setattr(m, 'confirm', lambda *args: 'normal')
    monkeypatch.setattr(m, 'run_agent_upgrade', lambda *args: None)

    m.update(1000, {})
    assert approved_seen == [plan]
