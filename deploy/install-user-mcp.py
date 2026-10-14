#!/usr/bin/python3
"""Install Bibit's manifest and repair only its known parent directories."""

import argparse
import os
from pathlib import Path
import pwd
import secrets
import stat
import sys


def install_manifest(home: Path, uid: int, gid: int, source: Path) -> None:
    if os.geteuid() != 0 or uid < 1000 or gid < 0:
        raise ValueError("requires root and a non-system target account")
    if not home.is_absolute() or home == Path("/") or home.resolve() != home:
        raise ValueError("home must be a real absolute directory without symlinks")
    content = source.read_bytes()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptors = [os.open(home, flags)]
    temporary = None
    parts = (".bianbu-agent", "mcps", "bibit-intent")
    try:
        if os.fstat(descriptors[0]).st_uid != uid:
            raise ValueError("home owner differs from the selected account")
        for part in parts:
            try:
                descriptor = os.open(part, flags, dir_fd=descriptors[-1])
            except FileNotFoundError:
                break
            descriptors.append(descriptor)
            entry = os.fstat(descriptor)
            if entry.st_uid not in (0, uid):
                raise ValueError("MCP parent belongs to another user")
            if entry.st_dev != os.fstat(descriptors[0]).st_dev:
                raise ValueError("MCP path crosses a filesystem boundary")
        if len(descriptors) == 4:
            try:
                entry = os.stat("mcp.json", dir_fd=descriptors[-1], follow_symlinks=False)
            except FileNotFoundError:
                entry = None
            if entry is not None and (
                not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1
                or entry.st_uid not in (0, uid)
            ):
                raise ValueError("existing manifest is linked, special or owned by another user")
        for descriptor in descriptors[1:]:
            if os.fstat(descriptor).st_uid == 0:
                os.fchown(descriptor, uid, gid)
        for part in parts[len(descriptors) - 1:]:
            os.mkdir(part, 0o700, dir_fd=descriptors[-1])
            descriptor = os.open(part, flags, dir_fd=descriptors[-1])
            descriptors.append(descriptor)
            os.fchown(descriptor, uid, gid)
        candidate = ".mcp-install-" + secrets.token_hex(16)
        descriptor = os.open(
            candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=descriptors[-1],
        )
        temporary = candidate
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), 0o644)
            os.fchown(stream.fileno(), uid, gid)
        os.replace(temporary, "mcp.json", src_dir_fd=descriptors[-1], dst_dir_fd=descriptors[-1])
        temporary = None
    finally:
        try:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=descriptors[-1])
                except FileNotFoundError:
                    pass
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True)
    parser.add_argument("--source", type=Path, default=Path("/opt/bibit-intent/mcp.json"))
    args = parser.parse_args()
    try:
        account = pwd.getpwnam(args.user)
        if account.pw_uid == 65534:
            raise ValueError("nobody is not a login account")
        install_manifest(Path(account.pw_dir), account.pw_uid, account.pw_gid, args.source)
    except (KeyError, OSError, ValueError) as exc:
        print(f"bibit-intent: MCP installation failed for {args.user}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
