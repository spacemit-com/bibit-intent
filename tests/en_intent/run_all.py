"""Run the deterministic English rule and embedding probes."""
from __future__ import annotations
import subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
def main() -> int:
    for name in ("probe.py", "embedding_probe.py"):
        result = subprocess.run([sys.executable, str(ROOT / "tests/en_intent" / name)], cwd=ROOT)
        if result.returncode:
            return result.returncode
    return 0
if __name__ == "__main__": raise SystemExit(main())
