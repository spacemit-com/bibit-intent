from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from bibit_intent.core.yaml_utils import safe_load
from bibit_intent.resources import resource_path


def test_resource_yaml_matches_pyyaml_safe_load():
    resources = Path(resource_path("."))
    yaml_files = sorted(resources.rglob("*.yaml"))
    assert yaml_files
    for path in yaml_files:
        text = path.read_text(encoding="utf-8")
        assert safe_load(text) == yaml.safe_load(text), path


def test_fast_yaml_loader_rejects_python_object_tags():
    with pytest.raises(yaml.YAMLError):
        safe_load("!!python/object/apply:os.system ['true']")
