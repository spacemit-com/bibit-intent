"""Fast safe YAML loading with a pure-Python fallback."""

from typing import Any

import yaml

_SAFE_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def safe_load(stream: str) -> Any:
    return yaml.load(stream, Loader=_SAFE_LOADER)
