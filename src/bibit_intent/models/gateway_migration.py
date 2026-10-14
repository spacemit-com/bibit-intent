"""Move legacy Bibit model files into the Gateway preset storage directory.

Model IDs follow the Gateway presets (`local_url`), so migration only relocates
and verifies files; the Gateway auto-syncs preset state from the file on disk.
"""
import os
from pathlib import Path
import shutil
import tempfile
from urllib.parse import urlsplit

from .gateway_artifacts import ARTIFACTS, RESERVE, verify


OBSOLETE_MODEL_IDS = {"llm": "bibit-llm", "embed": "bibit-embed"}


def storage_directory(transport, domain: str, *, gateway_root: Path | None = None) -> Path:
    if domain not in ARTIFACTS:
        raise ValueError("Unknown Bibit model domain")
    if urlsplit(transport.base_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Model provisioning requires a Gateway on this host")
    root = Path(os.environ.get("BIBIT_INTENT_GATEWAY_MODELS_ROOT", str((gateway_root or Path.home()) / ".cache/models")))
    if not root.is_absolute():
        raise ValueError("BIBIT_INTENT_GATEWAY_MODELS_ROOT must be absolute")
    return root / domain


def copy_model(source: Path, destination: Path, artifact) -> None:
    verify(source, artifact)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise ValueError("Model destination must not be a symbolic link")
    if destination.exists():
        verify(destination, artifact)
        return
    if shutil.disk_usage(destination.parent).free < artifact.size + RESERVE:
        raise OSError("Insufficient disk space for model migration")
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".bibit-migrate-") as temporary:
        with source.open("rb") as original:
            shutil.copyfileobj(original, temporary, length=1024 * 1024)
        temporary.flush()
        os.fsync(temporary.fileno())
        verify(Path(temporary.name), artifact)
        os.chmod(temporary.name, 0o644)
        try:
            os.link(temporary.name, destination)
        except FileExistsError:
            verify(destination, artifact)


def deregister_obsolete_models(transport) -> list[str]:
    """Unload and deregister the legacy bibit-* registrations superseded by presets."""
    retired = []
    for domain, obsolete in OBSOLETE_MODEL_IDS.items():
        rows = transport.request(f"/v1/{domain}/models")
        if not any(row["id"] == obsolete for row in rows):
            continue
        try:
            transport.request(f"/v1/{domain}/models/unload", {"model": obsolete})
        except (OSError, ValueError):
            pass
        transport.request(f"/v1/{domain}/models/deregister", {"model": obsolete})
        retired.append(f"{domain}:{obsolete}")
        print(f"{domain}: deregistered obsolete model {obsolete}")
    return retired


def migrate_models(transport, models: dict[str, str], *, legacy_home: Path,
                   gateway_root: Path | None = None,
                   cleanup: bool = False, legacy_system: Path = Path("/opt/bibit-intent/models")) -> dict:
    # `legacy_home` 只用于定位旧的 Bibit 模型副本；Gateway 的实际存储目录由
    # `gateway_root`（或 `BIBIT_INTENT_GATEWAY_MODELS_ROOT`）单独决定，不能
    # 拿调用方 HOME 当 Gateway 的存储根（Gateway 以其它用户运行时两者不等）。
    directories = {domain: storage_directory(transport, domain, gateway_root=gateway_root) for domain in models}
    roots = (legacy_home / ".bibit-intent/models", legacy_system,
             legacy_home / ".local/share/bibit-intent/models")
    migrated = []
    pending = []
    for domain, model in models.items():
        artifact = ARTIFACTS[domain]
        filename = artifact.url.rsplit("/", 1)[-1]
        destination = directories[domain] / filename
        if destination.is_file():
            try:
                verify(destination, artifact)
                migrated.append({"domain": domain, "model": model, "path": str(destination)})
                print(f"{domain}: verified Gateway model at {destination}")
            except ValueError as exc:
                print(f"{domain}: existing destination preserved (not overwritten): {exc}")
            continue
        source = next((path for path in (root / filename for root in roots) if path.is_file()), None)
        if source is None:
            print(f"{domain}: no existing model; Gateway download is required")
            continue
        try:
            verify(source, artifact)
        except ValueError as exc:
            print(f"{domain}: legacy model invalid and preserved: {exc}")
            continue
        if source.resolve() == destination.resolve():
            migrated.append({"domain": domain, "model": model, "path": str(destination)})
            print(f"{domain}: legacy model already at Gateway storage {destination}")
            continue
        try:
            copy_model(source, destination, artifact)
        except (OSError, ValueError) as exc:
            pending.append(str(source))
            print(f"{domain}: migration pending; original preserved: {exc}")
            continue
        migrated.append({"domain": domain, "model": model, "path": str(destination)})
        print(f"{domain}: migrated legacy model to {destination}")

    if cleanup:
        references = {
            str(Path(row["local_path"]).resolve())
            for domain in ("llm", "embed", "rerank", "vlm")
            for row in transport.request(f"/v1/{domain}/models")
            if row.get("local_path")
        }
        for item in migrated:
            artifact = ARTIFACTS[item["domain"]]
            destination = Path(item["path"])
            verify(destination, artifact)
            for root in roots:
                source = root / destination.name
                if (not source.is_file() or source.is_symlink()
                        or str(source.resolve()) in references):
                    continue
                try:
                    verify(source, artifact)
                    source.unlink()
                    print(f"Removed verified legacy copy: {source}")
                except (OSError, ValueError) as exc:
                    pending.append(str(source))
                    print(f"Legacy copy preserved: {source}: {exc}")
    return {"migrated": migrated, "cleanup_pending": pending}
