# English intent test cases

`corpus.jsonl` covers high-confidence English controls, open questions, and safety escalations.

Run the offline probe with:

```bash
python tests/en_intent/probe.py
```

For an on-device model/embedding run, use the existing `tests/ubuntu_cmd/probe.py` flow after switching the remote embedding service to `Qwen3-Embedding-0.6B-Q4_0.gguf`.

Optional Runtime-compatible cloud verification (uses `CHAT_API_KEY` and never executes local tools):

```bash
.venv/bin/python tests/en_intent/cloud_probe.py
```

Remote Bianbu Runtime handoff verification:

```bash
ssh bianbu@10.0.91.72 'python3 -' < tests/en_intent/runtime_cloud_probe.py
```
