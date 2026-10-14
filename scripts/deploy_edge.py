from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import pwd
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import uuid


FILES = (
    "core/app_request.py", "core/desktop_locations.py", "execution/system_tools.py",
    # 拍照走独立 helper（系统 python3 + AT-SPI，venv 无 PyGObject）：hotfix 只带
    # system_tools.py 而不带它时，take_photo 会在端侧报 helper_missing。
    "execution/camera_capture.py",
    "routing/multi_action.py", "routing/router.py",
    "core/request_semantics.py", "core/package_preview.py", "core/package_checks.py", "core/package_state.py", "core/package_target.py", "core/session.py",
    "execution/bounded_command.py", "execution/cancellation.py", "execution/progress.py",
    "execution/executor.py", "pipeline.py",
    "routing/intent.py", "resources/skills/ubuntu_cmd/handlers.py",
    "resources/skills/ubuntu_cmd/skill.yaml", "resources/skills/ubuntu_admin/handlers.py",
    "routing/rule_engine.py", "mcp_server/server.py", "resources/skills/ubuntu_admin/skill.yaml", "resources/tools.yaml",
    # mask_literals 的路径字面量停止位（CJK 连接词不再被吞）依赖 lexical 层同步。
    "analysis/lexical.py",
    # 英文只读命令（show/list/display/get/trace/mount…）的动作动词表，供规则引擎
    # 的谓词门（_pattern_verbs）识别英文句式，须随 rule_engine 同步到端侧。
    "analysis/domain_lexicon.py",
    # Gateway 模型存储根不得复用调用方 HOME（gateway_root 与 legacy_home 解耦），
    # 迁移/初始化逻辑须随模型供给代码同步到端侧。
    "models/gateway_migration.py", "models/gateway_setup.py", "models/gateway_bootstrap.py",
    # wifi_connect 的裸词护栏（require_object）依赖 wifi skill 表同步到端侧。
    "resources/skills/wifi/skill.yaml",
    # CLI-Anything appcli（H01–H07/H13/H15）：注册表与锁定表是数据，其余是执行/校验/
    # 审计与只读提示。dependency_plan 一并带上是因为缺前置提示要用它的 channel_label
    # 口径（老设备上没有这个函数，缺了会让整个 appcli 技能加载失败）。
    "core/dependency_plan.py",
    "core/appcli_registry.py", "core/appcli_prereq.py", "core/appcli_artifacts.py",
    "core/appcli_execution.py", "core/appcli_audit.py", "core/appcli_wheels.py",
    "resources/appcli_registry.yaml", "resources/appcli_wheels.yaml",
    "resources/skills/appcli/handlers.py", "resources/skills/appcli/skill.yaml",
)
MAX_BUNDLE_BYTES = 16_000_000


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source(root: Path) -> Path:
    return root / "src" / "bibit_intent"


def safe_path(base: Path, name: str) -> Path:
    if name not in FILES:
        raise ValueError(f"unexpected deployment file: {name}")
    path = base / name
    if path.resolve() != base.resolve() / name:
        raise ValueError(f"symlink or redirected path: {name}")
    return path


def snapshot(root: Path) -> dict:
    base = source(root)
    if not base.is_dir():
        raise ValueError("source directory missing")
    return {name: digest(path.read_bytes()) if path.exists() else None for name in FILES for path in [safe_path(base, name)]}


def validate_files(contents: dict[str, bytes]) -> None:
    for name, data in contents.items():
        if name.endswith(".py"):
            ast.parse(data.decode("utf-8"), filename=name)
        elif name.endswith(".yaml"):
            import yaml
            config = yaml.safe_load(data)
            if not isinstance(config, dict):
                raise ValueError("invalid yaml configuration")
            # 技能与工具表必须声明 tools；注册表/锁定表是纯数据，只要求是映射。
            if name.endswith("skill.yaml") or name.endswith("tools.yaml"):
                if not isinstance(config.get("tools"), dict):
                    raise ValueError("invalid skill configuration")
            elif "version" not in config:
                raise ValueError("invalid data configuration")


def build(root: Path, previous: dict, output: Path) -> None:
    if set(previous) != set(FILES):
        raise ValueError("baseline must contain the exact deployment manifest")
    contents = {name: safe_path(source(root), name).read_bytes() for name in FILES}
    validate_files(contents)
    manifest = {"format": 1, "previous": previous, "files": {name: digest(data) for name, data in contents.items()}}
    with tarfile.open(output, "w:gz") as archive:
        for name, data in {"manifest.json": json.dumps(manifest).encode(), **contents}.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))


def read_bundle(bundle: Path) -> tuple[dict, dict[str, bytes]]:
    contents = {}
    total = 0
    with tarfile.open(bundle, "r:gz") as archive:
        for member in archive:
            if member.name not in {*FILES, "manifest.json"} or member.name in contents or not member.isfile():
                raise ValueError("unsafe or duplicate archive member")
            total += member.size
            if total > MAX_BUNDLE_BYTES:
                raise ValueError("bundle exceeds size limit")
            contents[member.name] = archive.extractfile(member).read()
    manifest = json.loads(contents.pop("manifest.json"))
    if manifest.get("format") != 1 or set(contents) != set(FILES) or set(manifest["previous"]) != set(FILES):
        raise ValueError("incomplete manifest")
    if manifest["files"] != {name: digest(data) for name, data in contents.items()}:
        raise ValueError("bundle checksum mismatch")
    validate_files(contents)
    return manifest, contents


def check(root: Path, bundle: Path) -> tuple[dict, dict[str, bytes]]:
    manifest, contents = read_bundle(bundle)
    if snapshot(root) != manifest["previous"]:
        raise ValueError("device changed since baseline; refusing overwrite")
    return manifest, contents


@contextmanager
def deploy_lock(root: Path):
    descriptor = os.open(root / ".bibit-deploy.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def replace(path: Path, data: bytes, attributes: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.stat() if path.exists() else path.parent.stat()
    descriptor, temporary = tempfile.mkstemp(prefix=".bibit-new-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), attributes["mode"] if attributes else (original.st_mode & 0o777 if path.exists() else 0o644))
            if os.geteuid() == 0:
                os.fchown(stream.fileno(), attributes["uid"] if attributes else original.st_uid, attributes["gid"] if attributes else original.st_gid)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def service(user: str, action: str) -> None:
    uid = pwd.getpwnam(user).pw_uid
    subprocess.run(["runuser", "-u", user, "--", "env", f"XDG_RUNTIME_DIR=/run/user/{uid}", f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus", "systemctl", "--user", action, "bibit-intent.service"], check=True, timeout=45)


def healthy() -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8765/healthz", timeout=2) as response:
                if json.load(response).get("status") == "ok":
                    return
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    raise RuntimeError("Bibit health check failed")


def smoke() -> None:
    request = urllib.request.Request(
        "http://127.0.0.1:8765/v1/runs",
        data=json.dumps({"prompt": "软件包预览 已安装 正则 ^bash.*$", "session_id": "deploy-" + uuid.uuid4().hex, "timeout_ms": 60000}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=65) as response:
        result = json.load(response)
    detail = result.get("metadata", {}).get("result", {})
    if not result.get("handled") or not detail.get("ok") or detail.get("read_only") is not True or '"read_only"' in result.get("reply", ""):
        raise RuntimeError("read-only preview smoke test failed")


def restore(root: Path, backup: Path) -> None:
    manifest = json.loads((backup / "manifest.json").read_text())
    current = snapshot(root)
    for name in FILES:
        if current[name] not in {manifest["files"][name], manifest["previous"][name]}:
            raise ValueError(f"subsequent change detected: {name}; refusing rollback")
        if manifest["previous"][name] is not None:
            if digest(safe_path(backup, name).read_bytes()) != manifest["previous"][name]:
                raise ValueError("backup checksum mismatch")
    for name in FILES:
        path = safe_path(source(root), name)
        if manifest["previous"][name] is None:
            path.unlink(missing_ok=True)
        else:
            replace(path, safe_path(backup, name).read_bytes(), manifest["attributes"][name])
    if snapshot(root) != manifest["previous"]:
        raise RuntimeError("rollback verification failed")


def apply(root: Path, bundle: Path, user: str) -> Path:
    with deploy_lock(root):
        manifest, contents = check(root, bundle)
        backup = Path(tempfile.mkdtemp(prefix="edge-backup-", dir=root))
        manifest["attributes"] = {}
        for name in FILES:
            path = safe_path(source(root), name)
            if path.exists():
                attributes = path.stat()
                manifest["attributes"][name] = {"mode": attributes.st_mode & 0o777, "uid": attributes.st_uid, "gid": attributes.st_gid}
                target = backup / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
        (backup / "manifest.json").write_text(json.dumps(manifest, indent=2))
        print(f"BACKUP={backup}", flush=True)
        stopped = False
        try:
            service(user, "stop")
            stopped = True
            for name, data in contents.items():
                replace(safe_path(source(root), name), data)
            if snapshot(root) != manifest["files"]:
                raise RuntimeError("deployed checksum mismatch")
            service(user, "start")
            healthy()
            smoke()
        except BaseException:
            if stopped:
                service(user, "stop")
                restore(root, backup)
                service(user, "start")
                healthy()
                print("ROLLBACK_OK", flush=True)
            raise
        return backup


def main() -> None:
    parser = argparse.ArgumentParser(description="Bibit fixed-manifest deployment; does not install packages or manage Runtime services")
    commands = parser.add_subparsers(dest="command", required=True)
    for action in ("snapshot", "build", "check", "apply", "rollback"):
        child = commands.add_parser(action)
        child.add_argument("--root", type=Path, required=True)
        if action in {"snapshot", "build"}:
            child.add_argument("--output", type=Path, required=True)
        if action == "build":
            child.add_argument("--baseline", type=Path, required=True)
        if action in {"check", "apply"}:
            child.add_argument("--bundle", type=Path, required=True)
        if action in {"apply", "rollback"}:
            child.add_argument("--user", default="bianbu")
        if action == "rollback":
            child.add_argument("--backup", type=Path, required=True)
    args = parser.parse_args()
    if args.command in {"apply", "rollback"} and os.geteuid() != 0:
        parser.error("apply/rollback require root; other commands are read-only for device sources")
    if args.command == "snapshot":
        args.output.write_text(json.dumps(snapshot(args.root), indent=2))
    elif args.command == "build":
        build(args.root, json.loads(args.baseline.read_text()), args.output)
    elif args.command == "check":
        check(args.root, args.bundle)
        print("CHECK_OK")
    elif args.command == "apply":
        print("DEPLOY_OK", apply(args.root, args.bundle, args.user))
    else:
        with deploy_lock(args.root):
            service(args.user, "stop")
            try:
                restore(args.root, args.backup)
            finally:
                service(args.user, "start")
            healthy()
            print("ROLLBACK_OK")


if __name__ == "__main__":
    main()
