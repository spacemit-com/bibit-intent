"""Initialize the fixed Bibit models via the shared Gateway download and load APIs."""
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote

from .gateway import GatewayTransport
from .gateway_artifacts import MODEL_LOAD_ARGS
from .gateway_migration import deregister_obsolete_models, migrate_models
from .gateway_readiness import wait_for_gateway
from ..pipeline import PipelineSettings


_DOWNLOADED_STATES = {"downloaded", "loading", "loaded"}


def _row(transport: GatewayTransport, domain: str, model: str) -> dict | None:
    rows = transport.request(f"/v1/{domain}/models")
    return next((row for row in rows if row["id"] == model), None)


def _request_download(transport: GatewayTransport, domain: str, model: str) -> None:
    transport.request(f"/v1/{domain}/models/{quote(model, safe='')}/download", method="POST")


def ensure_downloaded(transport: GatewayTransport, domain: str, model: str,
                      *, timeout: float = 3600.0) -> None:
    """Request the Gateway download for a local_url preset and wait for completion.

    The Gateway resets a failed download back to ``available`` after its async
    worker removes the temp file, and its model listing is what the polling loop
    below reads.  Without re-requesting, a first boot without a network stays in
    an endless poll even after the link comes up.  Keep re-requesting on
    ``available`` and treat a connection drop as retryable so the unit only
    gives up (and lets systemd back off) once the overall deadline is reached.
    """
    current = _row(transport, domain, model)
    if current is None:
        raise ValueError(f"{model}: Gateway preset model not found")
    if current.get("source_type") != "local_url" or current.get("api_base_url"):
        raise ValueError(f"{model}: Gateway must manage this model as local_url")
    status = current.get("status")
    if status in _DOWNLOADED_STATES:
        return
    deadline = time.monotonic() + timeout
    request_download = status != "downloading"
    while time.monotonic() < deadline:
        try:
            if request_download:
                _request_download(transport, domain, model)
                request_download = False
            current = _row(transport, domain, model)
        except (URLError, ConnectionError, TimeoutError) as exc:
            # Gateway restarting or network still coming up: keep the retryable
            # window alive, then request the download again on the next pass.
            error = exc
        else:
            error = None
            if current is None:
                raise ValueError(f"{model}: Gateway preset disappeared during download")
            status = current.get("status")
            if status == "error":
                raise RuntimeError(f"{model}: Gateway download failed")
            if status in _DOWNLOADED_STATES:
                return
            if status in {"available", "idle", "unregistered"}:
                request_download = True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # Normal polls keep a responsive progress loop, but a download re-request
        # or a dropped connection means the network/Gateway is not ready yet;
        # back off so a first boot without a link does not hammer the endpoint.
        time.sleep(min(15 if (request_download or error is not None) else 2, remaining))
    if error is not None:
        raise TimeoutError(f"{model}: Gateway download not ready ({error})") from error
    raise TimeoutError(f"{model}: Gateway download timed out")


def bootstrap():
    config = PipelineSettings.from_environment()
    transport = GatewayTransport(config.gateway_url, config.gateway_api_key, timeout=150)
    models = {"llm": config.gateway_llm_model, "embed": config.gateway_embed_model}
    wait_for_gateway(transport)
    try:
        for domain in models:
            transport.request(f"/v1/{domain}/models")
    except HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError(
                "Gateway model management API is unavailable; check the configured URL and installed service"
            ) from exc
        raise
    migrate_models(transport, models, legacy_home=Path.home(), cleanup=False)
    deregister_obsolete_models(transport)
    for domain, model in models.items():
        ensure_downloaded(transport, domain, model)
        memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        if int(memory["MemAvailable"].split()[0]) < 2048 * 1024:
            raise RuntimeError("Insufficient available RAM to load Bibit models (2 GiB guard)")
        if not any(row["id"] == model and row.get("status") == "loaded"
                   for row in transport.request(f"/v1/{domain}/models")):
            transport.request(f"/v1/{domain}/models/load", {
                "model": model, "extra_args": list(MODEL_LOAD_ARGS[domain]),
            })
        print(f"Provisioned {domain}: {model}; inference readiness must be checked separately", flush=True)


if __name__ == "__main__":
    bootstrap()
