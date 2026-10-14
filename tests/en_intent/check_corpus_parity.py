"""Report parity between Chinese and English route corpora by intent."""
from __future__ import annotations
import collections, json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "src/bibit_intent/resources/route-data"
def load(name):
    return [json.loads(x) for x in (ROOT/name).read_text(encoding="utf-8").splitlines() if x.strip()]
def main() -> int:
    zh = collections.Counter(r.get("expect") for r in load("system_control.jsonl"))
    en = collections.Counter(r.get("expect") for r in load("system_control-en.jsonl"))
    print("intent,zh,en,missing")
    for intent in sorted(set(zh)|set(en)):
        print(f"{intent},{zh[intent]},{en[intent]},{max(0, zh[intent]-en[intent])}")
    return 0
if __name__ == "__main__": raise SystemExit(main())
