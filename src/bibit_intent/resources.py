"""Resource access for the packaged bibit-intent runtime data."""
from pathlib import Path

_RESOURCE_ROOT = Path(__file__).resolve().parent / "resources"


def resource_path(*parts: str) -> Path:
    return _RESOURCE_ROOT.joinpath(*parts)
