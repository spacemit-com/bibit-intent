#!/usr/bin/python3
"""Host-owned APT update, with explicit consent bound to a simulated transaction."""
import argparse
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid


STATE = Path('/var/lib/bibit-intent/system-update.status')
JOB_ID = uuid.uuid4().hex
RECOVERY_DELEGATED = False
APT = ['apt-get', '-o', 'DPkg::Lock::Timeout=120']
GUI_KEYS = ('DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY')
INSTALL = re.compile(r'^Inst ([a-z0-9][a-z0-9+.:\-]*)(?: \[([^\]]+)\])? \((\S+)')
SELF_PACKAGES = frozenset({
    'agent-power-manager',
    'bi-bit',
    'bianbu-agent-memory',
    'bianbu-agent-memoryd',
    'bianbu-agent-runtime',
    'bianbu-agent-securityd',
    'bibit-agenthub-cli',
    'bibit-agenthub-plugin',
    'bibit-filesearch',
    'bibit-filesearch-plugin',
    'bibit-intent',
    'bibit-memory-plugin',
    'bibit-runtime-plugin',
    'bibit-voice',
})
AGENT_UPGRADE_SCRIPT = '/opt/bibit-intent/bibit-agent-upgrade.sh'
RECOVERY_MARKER_TEMPLATE = '/run/user/{uid}/bibit-intent/system-update.recovery'


def recovery_marker(uid):
    return Path(RECOVERY_MARKER_TEMPLATE.format(uid=uid))


def _prepare_recovery_directory(marker, uid, gid):
    parent = marker.parent.parent
    owner = parent.lstat()
    if not stat.S_ISDIR(owner.st_mode) or owner.st_uid != uid:
        raise PermissionError(f'Invalid user runtime directory: {parent}')
    directory = marker.parent
    try:
        directory.mkdir(mode=0o700)
        info = directory.lstat()
    except FileExistsError:
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, uid):
            raise PermissionError(f'Invalid recovery directory: {directory}')
    if info.st_uid != uid:
        os.chown(directory, uid, gid, follow_symlinks=False)
    directory.chmod(0o700)
    lock = marker.with_suffix('.lock')
    try:
        info = lock.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISREG(info.st_mode) and info.st_uid == 0:
        lock.unlink()


def _write_recovery_marker(uid):
    """进入自研组件阶段前写入恢复标记。

    Runtime 即将重启，当前 MCP 会话会被切断；bibit-intent 的 MCP 在 Runtime
    恢复后重新拉起时读到该标记，负责弹出最终成功/失败结果。标记放在 /run/user
    下，root（本脚本）和登录用户（MCP 侧清理）都能读写，且登出即自动清理。
    """
    global RECOVERY_DELEGATED
    RECOVERY_DELEGATED = False
    marker = recovery_marker(uid)
    try:
        account = pwd.getpwuid(uid)
        _prepare_recovery_directory(marker, uid, account.pw_gid)
        subprocess.run(
            ['/usr/bin/python3', '-c',
             'import os,pathlib,sys,tempfile; p=pathlib.Path(sys.argv[1]); '
             'fd,name=tempfile.mkstemp(prefix=".recovery-",dir=p.parent); '
             'os.fchmod(fd,0o600); '
             'os.write(fd,sys.argv[2].encode()); os.fsync(fd); os.close(fd); '
             'os.replace(name,p)',
             str(marker), json.dumps({'pending': True, 'job_id': JOB_ID})],
            user=uid, group=account.pw_gid, extra_groups=[], timeout=5, check=True,
        )
        RECOVERY_DELEGATED = True
    except (OSError, KeyError, subprocess.SubprocessError) as error:
        print(f'Recovery marker unavailable ({error}); coordinator will display the result', flush=True)

def run(command, capture=False):
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.STDOUT if capture else None, check=False)
    if result.returncode:
        raise RuntimeError(f'Command failed ({result.returncode}): {command!r}\n{result.stdout or ""}')
    return result.stdout or ''


def status(value, **details):
    temporary = STATE.with_suffix('.tmp')
    temporary.write_text(value + '\n')
    temporary.chmod(0o644)
    temporary.replace(STATE)
    detail = STATE.with_suffix('.json')
    temporary = detail.with_suffix('.tmp')
    temporary.write_text(json.dumps({'state': value, 'job_id': JOB_ID, **details}, ensure_ascii=False) + '\n')
    temporary.chmod(0o644)
    temporary.replace(detail)
    print(value, details, flush=True)


def parse_plan(output):
    plan = {}
    for line in output.splitlines():
        if line.startswith('Remv '):
            raise RuntimeError('Update would remove packages; refusing')
        if line.startswith('Inst '):
            match = INSTALL.match(line)
            if not match:
                raise RuntimeError(f'Unrecognized APT transaction: {line}')
            name, old, new = match.groups()
            if name in plan:
                raise RuntimeError(f'Duplicate package in APT transaction: {name}')
            plan[name] = [old or '-', new]
    return plan


def base_name(name):
    return name.partition(':')[0]


def split_plan(plan):
    """按包名把事务拆成普通系统包与 bibit 自身组件两阶段。"""
    normal = {}
    agent = {}
    for name, versions in plan.items():
        target = agent if base_name(name) in SELF_PACKAGES else normal
        target[name] = versions
    return normal, agent


def unhold_self_packages():
    """解除上次升级失败后被重新 hold 的自研组件包。

    失败路径里 ``bibit-agent-upgrade.sh`` 会 ``apt-mark hold`` 相关包；若不先
    unhold，``apt-get --simulate upgrade`` 不会列出被 hold 的包，重试会得到空
    计划并误报成功。这里只解除当前处于 hold 状态的自研组件，避免误动其它包。
    """
    held = {base_name(name) for name in run(['apt-mark', 'showhold'], True).split()}
    targets = sorted(held & SELF_PACKAGES)
    if targets:
        run(['apt-mark', 'unhold', *targets])
    return targets


def recover_interrupted_dpkg():
    """恢复上次升级中断遗留的半配置包。

    普通包阶段没有独立的恢复逻辑；若上一次在 apt/dpkg 中途被中断，会残留
    unpacked-but-unconfigured 的包，直接 install 可能失败。这里先做一次标准
    恢复；失败不阻断本次更新，交由后续 apt 报出真实错误。
    """
    try:
        run(['dpkg', '--configure', '-a'])
    except RuntimeError:
        pass


def simulate(options):
    return parse_plan(run(APT + options + ['--simulate', '--no-remove', 'upgrade'], True))


def downgrades(plan):
    result = {}
    for name, (old, new) in plan.items():
        if old == '-':
            continue
        code = subprocess.run(['dpkg', '--compare-versions', new, 'lt', old]).returncode
        if code not in (0, 1):
            raise RuntimeError(f'Invalid package version: {name}')
        if code == 0:
            result[name] = [old, new]
    return result


def _dialog_env(uid, session):
    """组装降权后的桌面会话环境；不可用返回 None。"""
    if uid <= 0 or not any(session.get(key) for key in GUI_KEYS[:2]):
        return None
    account = pwd.getpwuid(uid)
    env = {'PATH': '/usr/bin:/bin', 'HOME': account.pw_dir,
           'USER': account.pw_name, 'LOGNAME': account.pw_name, 'LANG': 'zh_CN.UTF-8',
           'XDG_RUNTIME_DIR': f'/run/user/{uid}',
           'DBUS_SESSION_BUS_ADDRESS': f'unix:path=/run/user/{uid}/bus',
           'GSK_RENDERER': 'gl', 'GDK_GL': 'gles'}
    env.update({key: session[key] for key in GUI_KEYS if session.get(key)})
    return env, account


def dialog(uid, session, arguments):
    """Drop privileges before loading any desktop libraries or user session data."""
    prepared = _dialog_env(uid, session)
    if prepared is None:
        print('System update dialog unavailable: no desktop session', flush=True)
        return None
    env, account = prepared
    try:
        result = subprocess.run(['/usr/bin/zenity', *arguments], env=env,
                                user=uid, group=account.pw_gid, extra_groups=[],
                                cwd=account.pw_dir, text=True, capture_output=True, timeout=630)
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f'System update dialog failed: {error}', flush=True)
        return None
    if result.returncode != 0:
        print(f'System update dialog exited with {result.returncode}: {result.stderr.strip()}', flush=True)
        return None
    return result.stdout.strip()


def confirm(uid, session, packages):
    rows = [f'{name}: {old} -> {new}' for name, (old, new) in sorted(packages.items())]
    text = ('以下软件包需要降级，请选择处理方式。\n\n' + '\n'.join(rows))
    choice = dialog(uid, session, [
        '--list', '--radiolist', '--title=系统更新：检测到软件包降级', '--text=' + text,
        '--width=760', '--height=520', '--timeout=600', '--ok-label=确认', '--cancel-label=取消更新',
        '--column=选择', '--column=处理方式',
        'TRUE', '取消本次更新', 'FALSE', '跳过降级并更新其他软件包', 'FALSE', '允许以上降级并继续',
    ])
    return {'允许以上降级并继续': 'allow', '跳过降级并更新其他软件包': 'skip'}.get(choice, 'cancel')


def skip_options(directory, packages):
    # Specific pins precede the existing policy; no persistent hold is installed.
    configuration = run(['apt-config', 'shell', 'PREFERENCES', 'Dir::Etc::preferences/f'], True)
    entries = shlex.split(configuration)
    original = Path(entries[0].split('=', 1)[1]) if entries else Path('/etc/apt/preferences')
    pins = ''.join(f'Package: {name}\nPin: version {versions[0]}\nPin-Priority: 1001\n\n'
                   for name, versions in packages.items())
    preferences = directory / 'preferences'
    preferences.write_text(pins + (original.read_text() if original.is_file() else ''))
    return ['-o', f'Dir::Etc::preferences={preferences}']


def guard(stream, approved):
    """Reject solver changes under APT's locks, retaining multiarch identities."""
    protocol = stream.readline().strip()
    if protocol not in ('VERSION 2', 'VERSION 3'):
        raise RuntimeError('Missing APT transaction protocol')
    for line in stream:
        if not line.strip():
            break
    for line in stream:
        fields = line.split()
        if len(fields) != (9 if protocol == 'VERSION 3' else 5):
            raise RuntimeError(f'Invalid APT hook row: {line}')
        if protocol == 'VERSION 3':
            name, old, old_arch, old_multi, comparison, new, arch, multi, action = fields
            qualified = f'{name}:{arch}'
            if qualified in approved:
                name = qualified
        else:
            name, old, comparison, new, action = fields
        if action == '**REMOVE**':
            raise RuntimeError('Package removal is not authorized')
        if action == '**CONFIGURE**' and old == new:
            continue
        versions = approved.get(name)
        if versions != [old, new]:
            raise RuntimeError(f'Package plan changed after confirmation: {name} {old} -> {new}')


_AGENT_PHASE_PERCENT = {
    'starting': 5,
    'stopping_runtime': 20,
    'unholding': 30,
    'upgrading': 55,
    'restarting_runtime': 85,
    'succeeded': 100,
    'failed': 100,
}


def run_agent_upgrade(uid, session, packages, decision):
    """宿主侧执行 bibit 组件升级，用 zenity 显示进度；失败抛异常。"""
    status('agent_upgrading', plan=packages, decision=decision)
    _write_recovery_marker(uid)
    command = [AGENT_UPGRADE_SCRIPT, '--uid', str(uid)]
    if decision == 'allow':
        command.append('--allow-downgrades')
    command.extend(sorted({base_name(name) for name in packages}))

    progress = None
    prepared = _dialog_env(uid, session)
    if prepared is not None:
        env, account = prepared
        try:
            progress = subprocess.Popen(
                ['/usr/bin/zenity', '--progress', '--title=升级 Bibit 组件',
                 '--text=正在准备升级 Bibit 组件…', '--auto-close', '--no-cancel'],
                env=env, user=uid, group=account.pw_gid, extra_groups=[],
                cwd=account.pw_dir, stdin=subprocess.PIPE, text=True,
            )
        except (OSError, ValueError):
            progress = None

    run_env = os.environ.copy()
    run_env['PATH'] = '/usr/sbin:/usr/bin:/sbin:/bin'
    run_env['TERM'] = 'dumb'
    run_env['DEBIAN_FRONTEND'] = 'noninteractive'
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, env=run_env)
    progress_alive = progress is not None and progress.stdin is not None
    try:
        for line in process.stdout:
            line = line.rstrip('\n')
            print(line, flush=True)
            if line.startswith('STATUS '):
                phase = line[len('STATUS '):].strip()
                percent = _AGENT_PHASE_PERCENT.get(phase, 50)
                if progress_alive:
                    try:
                        progress.stdin.write(f'{percent}\n')
                        progress.stdin.flush()
                    except (BrokenPipeError, OSError):
                        progress_alive = False
                        try:
                            progress.terminate()
                        except OSError:
                            pass
        code = process.wait()
    finally:
        if progress is not None:
            try:
                if progress.stdin is not None:
                    progress.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            try:
                progress.wait(timeout=10)
            except subprocess.TimeoutExpired:
                progress.terminate()

    if code != 0:
        raise RuntimeError('Agent 组件升级失败，请查看 /var/log/bibit-agent-upgrade.log')


def update(uid, session):
    recover_interrupted_dpkg()
    status('refreshing')
    run(APT + ['-o', 'APT::Update::Error-Mode=any', 'update'])
    status('checking')
    unhold_self_packages()
    plan = simulate([])
    lower = downgrades(plan)
    decision = 'normal'
    if lower:
        status('awaiting_confirmation', downgrades=lower)
        decision = confirm(uid, session, lower)
        if decision == 'cancel':
            status('cancelled', downgrades=lower)
            return
    normal, agent = split_plan(plan)
    lower_normal, lower_agent = split_plan(lower)
    if decision == 'skip':
        normal = {name: versions for name, versions in normal.items() if name not in lower_normal}
        agent = {name: versions for name, versions in agent.items() if name not in lower_agent}
        skipped = lower
    else:
        skipped = {}
    with tempfile.TemporaryDirectory(prefix='bibit-system-update-', dir='/run') as work:
        directory = Path(work)
        options = skip_options(directory, lower_normal) if decision == 'skip' else []
        current = simulate(options)
        current_normal, _ = split_plan(current)
        if decision != 'skip':
            if current != plan:
                raise RuntimeError('Package plan changed while awaiting confirmation; run update again')
        else:
            if downgrades(current_normal) or any(name in current_normal for name in lower_normal):
                raise RuntimeError('Cannot skip downgrades without changing protected packages')
            if current_normal != normal:
                raise RuntimeError('Package plan changed while awaiting confirmation; run update again')

        if current_normal:
            # Snapshot the hook because the subsequent agent phase may replace
            # bibit-intent itself; this normal-package transaction stays guarded.
            hook = directory / 'guard.py'
            shutil.copyfile(__file__, hook)
            approved = directory / 'approved.json'
            # Guard 的批准范围覆盖整个已确认事务（normal + agent），而不是只有
            # normal 包：normal 阶段执行 `--only-upgrade` 时，apt 会因依赖关系把
            # agent 包一并卷入同一事务（如 bibit-voice 依赖 libsherpa-onnx-c-api，
            # 只升级库会被迫同时升级 bibit-voice）。这些包本来就在用户确认的升级计划
            # 里，只是设计成第二阶段升级，误判成“计划变化”会让整个更新失败。版本仍
            # 被逐行严格校验，真正的计划外新包/版本漂移依然会被拒绝。
            approved.write_text(json.dumps(current))
            hook_command = f'/usr/bin/python3 {hook} --guard {approved}'
            options += ['-o', f'DPkg::Pre-Install-Pkgs::={hook_command}',
                        '-o', 'DPkg::Tools::Options::/usr/bin/python3::Version=3',
                        '-o', 'DPkg::Tools::Options::/usr/bin/python3::InfoFD=0',
                        '-o', 'APT::Get::allow-downgrades=false']
            if decision == 'allow':
                options += ['--allow-downgrades']
            status('updating', plan=current_normal, decision=decision, skipped=skipped)
            run(APT + options + ['install', '--no-remove', '-y', '--only-upgrade', *sorted(current_normal)])

        if not agent:
            status('succeeded_with_skips' if decision == 'skip' else 'succeeded', skipped=lower if decision == 'skip' else {})
            return

        status('system_succeeded', plan=agent, decision=decision, skipped=skipped)
        run_agent_upgrade(uid, session, agent, decision)
        status('succeeded_with_skips' if decision == 'skip' else 'succeeded', skipped=lower if decision == 'skip' else {})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--uid', type=int, default=0)
    parser.add_argument('--session', default='{}')
    parser.add_argument('--guard', type=Path)
    args = parser.parse_args()
    if args.guard:
        guard(sys.stdin, json.loads(args.guard.read_text()))
        return
    session = json.loads(args.session)
    try:
        update(args.uid, session)
    except Exception as error:
        status('failed', error=str(error))
        if not RECOVERY_DELEGATED:
            dialog(args.uid, session, ['--error', '--title=系统更新失败', '--timeout=120',
                                      '--text=系统更新未完成。请查看 /var/log/bibit-system-update.log\n' + str(error)[-2000:]])
        raise
    else:
        if not RECOVERY_DELEGATED:
            result = STATE.read_text().strip()
            message = {'succeeded': '系统更新已完成。', 'succeeded_with_skips': '其他软件包更新已完成；已跳过需要降级的软件包。',
                       'cancelled': '已取消系统更新，未安装或降级任何软件包。'}.get(result, result)
            dialog(args.uid, session, ['--info', '--title=系统更新', '--timeout=60', '--text=' + message])


if __name__ == '__main__':
    main()
