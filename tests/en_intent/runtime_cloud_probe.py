"""Verify the remote Bianbu Runtime hands English questions to cloud.

The Runtime intentionally returns ``handled=false, route=cloud`` so its Agent
Loop can generate the final answer; this probe validates that hand-off contract.
"""
from __future__ import annotations
import argparse, json, urllib.request

def main() -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--url", default="http://127.0.0.1:8765"); args = ap.parse_args()
    payload = {"prompt": "What is quantum entanglement? Please answer in English.", "session_id": "english-cloud-probe", "timeout_ms": 60000}
    req = urllib.request.Request(args.url.rstrip("/") + "/v1/runs", data=json.dumps(payload).encode(), headers={"Content-Type":"application/json"})
    result = json.load(urllib.request.urlopen(req, timeout=70))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    ok = result.get("handled") is False and result.get("route") == "cloud" and result.get("reason") in {"no_tool_support", "open_question"}
    print("Runtime English cloud handoff:", "PASS" if ok else "FAIL")
    return 0 if ok else 1
if __name__ == "__main__": raise SystemExit(main())
