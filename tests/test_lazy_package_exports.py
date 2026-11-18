from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_mcp_import_does_not_load_pipeline_model_or_router():
    source_root = Path(__file__).resolve().parents[1] / "src"
    env = {**os.environ, "PYTHONPATH": str(source_root)}
    script = """
import sys
import bibit_intent

assert "bibit_intent.pipeline" not in sys.modules
assert "bibit_intent.models.local_model" not in sys.modules
assert "bibit_intent.routing.router" not in sys.modules

import bibit_intent.mcp_server.server

assert "bibit_intent.pipeline" not in sys.modules
assert "bibit_intent.models.local_model" not in sys.modules
assert "bibit_intent.routing.router" not in sys.modules
"""
    subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )


def test_public_package_exports_remain_available():
    source_root = Path(__file__).resolve().parents[1] / "src"
    env = {**os.environ, "PYTHONPATH": str(source_root)}
    script = """
import bibit_intent
namespace = {}
exec("from bibit_intent import *", namespace)
assert set(bibit_intent.__all__) <= namespace.keys()
"""
    subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )
