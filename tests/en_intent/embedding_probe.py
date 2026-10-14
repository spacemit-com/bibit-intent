"""Probe Qwen3 embedding service with the English intent corpus."""
from __future__ import annotations
import argparse, json, urllib.request
from pathlib import Path

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=str(Path(__file__).with_name("corpus.jsonl")))
    args = ap.parse_args()
    rows = [json.loads(x) for x in Path(args.corpus).read_text(encoding="utf-8").splitlines() if x.strip()]
    from bibit_intent.models.backends import embedding_client
    from bibit_intent.pipeline import PipelineSettings
    client = embedding_client(PipelineSettings.from_environment())
    dims = set(); failures = 0
    for row in rows:
        try:
            v = client.embed(row["input"]); dims.add(len(v))
            ok = bool(v) and all(isinstance(x, (int, float)) for x in v)
            print(f"{'OK' if ok else 'XX'} {row['id']} dim={len(v)}: {row['input']}")
            failures += not ok
        except Exception as exc:
            print(f"XX {row['id']}: {exc}"); failures += 1
    print(f"Embedding probe: {len(rows)-failures}/{len(rows)} passed; dimensions={sorted(dims)}")
    return 0 if failures == 0 else 1

if __name__ == "__main__": raise SystemExit(main())
