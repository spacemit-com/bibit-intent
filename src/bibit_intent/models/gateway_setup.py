"""Request the fixed Gateway preset models and optionally load them."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from urllib.error import URLError
from urllib.parse import urlsplit

from bibit_intent.models.gateway import GatewayTransport
from bibit_intent.models.gateway_bootstrap import ensure_downloaded
from bibit_intent.models.gateway_readiness import wait_for_gateway
from bibit_intent.models.gateway_migration import migrate_models
from bibit_intent.pipeline import PipelineSettings


def register_model(transport: GatewayTransport, domain: str, model: str, path: Path) -> None:
    """Register a custom local_path model; preset models are managed by the Gateway."""
    path = path.expanduser().resolve(strict=True)
    with path.open("rb") as handle:
        if handle.read(4) != b"GGUF":
            raise ValueError(f"Not a GGUF file: {path}")
    rows = transport.request(f"/v1/{domain}/models")
    existing = next((row for row in rows if row["id"] == model), None)
    if existing:
        if (existing["source_type"] != "local_path" or existing.get("api_base_url")
                or existing.get("local_path") != str(path)):
            raise ValueError(f"Model ID {model} already exists with a different source; not overwritten")
    else:
        transport.request(f"/v1/{domain}/models/register", {
            "model": model, "source_type": "local_path", "local_path": str(path),
        })


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true",
                        help="Request Gateway download for the preset models; does not load them")
    parser.add_argument("--migrate-legacy", action="store_true")
    parser.add_argument("--cleanup-legacy", action="store_true")
    parser.add_argument("--legacy-home", type=Path, default=Path.home())
    parser.add_argument("--gateway-root", type=Path, default=None,
                        help="Gateway model storage root (defaults to BIBIT_INTENT_GATEWAY_MODELS_ROOT or ~/.cache/models)")
    parser.add_argument("--legacy-model-dir", type=Path, default=Path("/opt/bibit-intent/models"))
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--load", action="store_true")
    parser.add_argument("--min-available-memory-mib", type=int, default=2048,
                        help="Available RAM guard before loading; not an exact capacity estimate")
    args = parser.parse_args()
    if args.env_file:
        for line in args.env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, separator, value = line.partition("=")
            if not separator or (not name.startswith("BIBIT_INTENT_GATEWAY_") and name != "BIBIT_INTENT_INFERENCE_BACKEND"):
                raise ValueError("Expected Gateway KEY=value environment entries")
            os.environ[name] = value.strip().strip("\"'")
    if args.cleanup_legacy and not args.migrate_legacy:
        parser.error("--cleanup-legacy requires --migrate-legacy")
    config = PipelineSettings.from_environment()
    if urlsplit(config.gateway_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("Run setup on the gateway host using a loopback gateway URL")
    transport = GatewayTransport(config.gateway_url, config.gateway_api_key, timeout=150)
    wait_for_gateway(transport)
    models = {"llm": config.gateway_llm_model, "embed": config.gateway_embed_model}
    if args.migrate_legacy:
        migrate_models(transport, models, legacy_home=args.legacy_home, cleanup=args.cleanup_legacy,
                       legacy_system=args.legacy_model_dir, gateway_root=args.gateway_root)
        if not args.load and not args.download:
            return
    if args.download or args.load:
        for domain, model in models.items():
            ensure_downloaded(transport, domain, model)
    if args.load:
        if args.min_available_memory_mib < 0:
            parser.error("--min-available-memory-mib must be nonnegative")
        memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        available_kib = int(memory["MemAvailable"].split()[0])
        if available_kib < args.min_available_memory_mib * 1024:
            raise RuntimeError("Insufficient available RAM for the configured load guard")
        for domain, model in models.items():
            if not transport.health(domain, model, load=True):
                raise RuntimeError(f"{domain} model is not ready")
            print(f"Ready {domain}: {model}")


if __name__ == "__main__":
    try:
        main()
    except (URLError, ConnectionError, TimeoutError) as exc:
        print(f"Gateway setup deferred: {exc}", file=sys.stderr)
        sys.exit(1)
