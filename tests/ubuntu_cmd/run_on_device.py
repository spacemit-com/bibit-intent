"""
tests/ubuntu_cmd/run_on_device.py — 把准确率探针推到设备并执行

密码从环境变量 BIBIT_DEV_PW 读取，不硬编码、不回显、不落盘。
用法:
  BIBIT_DEV_PW='***' python3 tests/ubuntu_cmd/run_on_device.py [--n 3]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import paramiko

HOST = "10.0.91.72"
USER = "root"
REMOTE_DIR = "/opt/bibit/tests/ubuntu_cmd"
LOCAL_DIR = Path(__file__).parent
FILES = ["corpus.jsonl", "anchors.jsonl", "probe.py"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=1)
    args = ap.parse_args()

    pw = os.environ.get("BIBIT_DEV_PW")
    if not pw:
        print("ERROR: 未设置 BIBIT_DEV_PW 环境变量", file=sys.stderr)
        return 2

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"[连接] {USER}@{HOST} ...")
    cli.connect(HOST, username=USER, password=pw, timeout=15,
                look_for_keys=False, allow_agent=False)

    cli.exec_command(f"mkdir -p {REMOTE_DIR}")[1].channel.recv_exit_status()
    sftp = cli.open_sftp()
    for f in FILES:
        local = LOCAL_DIR / f
        remote = f"{REMOTE_DIR}/{f}"
        sftp.put(str(local), remote)
        rsize = sftp.stat(remote).st_size
        lsize = local.stat().st_size
        ok = "OK" if rsize == lsize else "SIZE_MISMATCH"
        print(f"[上传] {f}  {lsize}B -> {rsize}B  {ok}")
    sftp.close()

    _run(cli, "curl -s -m 5 http://127.0.0.1:18790/healthz || echo NO_SERVER")
    print(f"\n[执行] probe.py --n {args.n}\n" + "-" * 56)
    _run(cli,
         f"set -a; . /etc/bibit-intent/inference.env; set +a; "
         f"cd /opt/bibit-intent && PYTHONPATH=/opt/bibit-intent/src .venv/bin/python {REMOTE_DIR}/probe.py "
         f"--n {args.n}",
         stream=True)
    cli.close()
    return 0


def _run(cli, cmd: str, stream: bool = False) -> None:
    stdin, stdout, stderr = cli.exec_command(cmd, timeout=900, get_pty=True)
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
