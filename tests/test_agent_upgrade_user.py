import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize('uid,name', [('1234', 'alice'), ('2345', 'developer')])
@pytest.mark.parametrize('modern', [True, False])
def test_upgrade_restarts_initiating_user_only(tmp_path, uid, name, modern):
    commands = tmp_path / 'commands'
    scripts = {
        'id': f'echo {name}\n',
        'systemd-escape': 'printf "bianbu-agent-runtime@%s.service\\n" "$3"\n',
        'systemctl': 'echo "$*" >> "$COMMAND_LOG"\ncase "$*" in *LoadState*bianbu-agent-runtime@*) echo loaded ;; esac\n',
        'apt-get': 'exit 0\n',
        'apt-mark': 'exit 0\n',
    }
    for command, body in scripts.items():
        if command == 'systemctl' and modern:
            body = 'echo "$*" >> "$COMMAND_LOG"\ncase "$*" in *LoadState*) echo loaded ;; esac\n'
        path = tmp_path / command
        path.write_text('#!/bin/sh\n' + body)
        path.chmod(0o755)
    script = Path(__file__).resolve().parents[1] / 'deploy/bibit-agent-upgrade.sh'
    env = dict(os.environ, PATH=f'{tmp_path}:/usr/bin:/bin', COMMAND_LOG=str(commands),
               BIBIT_AGENT_UPGRADE_LOG=str(tmp_path / 'upgrade.log'),
               BIBIT_AGENT_UPGRADE_STOP_DELAY='0', BIBIT_RUNTIME_UNIT='wrong.service')
    result = subprocess.run(['/bin/sh', str(script), '--uid', uid, 'bibit-intent'],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    lines = commands.read_text().splitlines()
    unit = 'bianbu-agent-runtime.service' if modern else f'bianbu-agent-runtime@{name}.service'
    assert f'stop {unit}' in lines
    assert f'start {unit}' in lines
    assert all('wrong.service' not in line for line in lines)


@pytest.mark.parametrize('uid', ['0', 'alice', '-1', ''])
def test_invalid_uid_fails_before_package_operations(tmp_path, uid):
    script = Path(__file__).resolve().parents[1] / 'deploy/bibit-agent-upgrade.sh'
    result = subprocess.run(['/bin/sh', str(script), '--uid', uid, 'bibit-intent'],
                            env={'PATH': '/usr/bin:/bin', 'SUDO_UID': '0'},
                            capture_output=True, text=True)
    assert result.returncode != 0
