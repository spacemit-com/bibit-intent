import json
import subprocess
from types import SimpleNamespace

import pytest

from bibit_intent.execution import system_tools
from bibit_intent import system_update_recovery as recovery


@pytest.mark.parametrize('state,job,removed', [
    ('agent_upgrading', 'current', False),
    ('succeeded', 'previous', False),
    ('succeeded', 'current', True),
])
def test_recovery_preserves_pending_or_unrelated_results(tmp_path, monkeypatch, state, job, removed):
    marker = tmp_path / 'recovery'
    marker.write_text(json.dumps({'job_id': 'current'}))
    monkeypatch.setattr(recovery, '_recovery_marker', lambda uid: marker)
    monkeypatch.setattr(recovery, '_read_state', lambda: (state, {'job_id': job}))
    popups = []
    monkeypatch.setattr(recovery, '_popup', lambda *args, **kwargs: popups.append(args) or True)
    recovery.report_pending_system_update(timeout_seconds=0)
    assert marker.exists() is not removed
    assert bool(popups) is removed


def test_failed_popup_keeps_marker_for_retry(tmp_path, monkeypatch):
    marker = tmp_path / 'recovery'
    marker.write_text(json.dumps({'job_id': 'current'}))
    monkeypatch.setattr(recovery, '_recovery_marker', lambda uid: marker)
    monkeypatch.setattr(recovery, '_read_state', lambda: ('succeeded', {'job_id': 'current'}))
    monkeypatch.setattr(recovery, '_popup', lambda *args, **kwargs: False)
    recovery.report_pending_system_update(timeout_seconds=0)
    assert marker.exists()


@pytest.mark.parametrize('returncode,expected', [
    (0, True),      # 用户点击 OK
    (5, True),      # --timeout 到期自动关闭，仍算已显示
    (-11, False),   # SIGSEGV 崩溃
    (1, False),     # zenity 主动报错/取消
])
def test_popup_treats_timeout_as_shown(monkeypatch, returncode, expected):
    monkeypatch.setattr(system_tools, '_find_desktop_session_env', lambda uid: {'DISPLAY': ':0'})
    monkeypatch.setattr(recovery.os.path, 'exists', lambda path: True)

    def fake_run(arguments, env, capture_output, text, timeout):
        assert arguments[0] == '/usr/bin/zenity'
        return SimpleNamespace(returncode=returncode, stderr='')

    monkeypatch.setattr(recovery.subprocess, 'run', fake_run)
    assert recovery._popup(1000, '系统更新已完成。') is expected
