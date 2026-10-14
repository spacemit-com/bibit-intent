"""
tests/multi_intent/run_on_device.py — 通过 paramiko 把探针推到设备并执行

密码从环境变量 BIBIT_DEV_PW 读取，不硬编码、不回显、不落盘。
用法:
  BIBIT_DEV_PW='***' python3 tests/multi_intent/run_on_device.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import paramiko

HOST = "10.0.91.72"
USER = "root"
REMOTE_DIR = "/tmp/mi"
LOCAL_DIR = Path(__file__).parent
FILES = ["corpus.jsonl", "probe.py"]


def main() -> int:
    pw = os.environ.get("BIBIT_DEV_PW")
    if not pw:
        print("ERROR: 未设置 BIBIT_DEV_PW 环境变量", file=sys.stderr)
        return 2

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"[连接] {USER}@{HOST} ...")
    cli.connect(HOST, username=USER, password=pw, timeout=15,
                look_for_keys=False, allow_agent=False)

    # 1. 建目录 + 上传
    cli.exec_command(f"mkdir -p {REMOTE_DIR}")[1].channel.recv_exit_status()
    sftp = cli.open_sftp()
    for f in FILES:
        local = LOCAL_DIR / f
        remote = f"{REMOTE_DIR}/{f}"
        sftp.put(str(local), remote)
        # 校验大小一致（不信任 stdout，比对字节数）
        rsize = sftp.stat(remote).st_size
        lsize = local.stat().st_size
        ok = "✓" if rsize == lsize else "✗大小不符!"
        print(f"[上传] {f}  {lsize}B -> {rsize}B  {ok}")
    sftp.close()

    # 2. 先确认 llama-server 在线
    _run(cli, "curl -s http://127.0.0.1:18790/healthz || echo NO_SERVER")

    # 3. 跑探针（每条 2 次）
    print("\n[执行] probe.py --n 2\n" + "-" * 60)
    _run(cli, f"set -a; . /etc/bibit-intent/inference.env; set +a; "
         f"cd {REMOTE_DIR} && PYTHONPATH=/opt/bibit-intent/src /opt/bibit-intent/.venv/bin/python probe.py --n 2", stream=True)

    cli.close()
    return 0


def _run(cli, cmd: str, stream: bool = False) -> None:
    stdin, stdout, stderr = cli.exec_command(cmd, timeout=600, get_pty=True)
    if stream:
        for line in iter(stdout.readline, ""):
            sys.stdout.write(line)
            sys.stdout.flush()
    else:
        out = stdout.read().decode(errors="replace").strip()
        if out:
            print(out)
    err = stderr.read().decode(errors="replace").strip()
    if err:
        print("[stderr]", err, file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
