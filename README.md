# Bibit Intent

Bibit Intent provides intent routing and tool execution for Bianbu Linux /
SpacemiT RISC-V devices, combining deterministic rules, local inference, and
cloud escalation. See the [English design document](docs/design/design.en.md)
or [Chinese design document](docs/design/design.md) for architecture,
interfaces, configuration, and a suggested learning path.

## Architecture: three-stage routing

```text
User input
   |
   v
1. Rule engine (rule_engine.py)       Frequent commands; offline
   | No match / low confidence
   v
   Semantic retrieval (embedding.py)  Dense cosine similarity + sparse BM25
   |
   v
2. Local model (AI Gateway)           GLM Edge + GBNF; offline
   | ESCALATE / unavailable
   v
3. Cloud model (cloud_model.py)       Complex orchestration; network required
```

Semantic retrieval narrows the candidate tools for local inference; it is not
a separate execution stage. In embedded integrations, Runtime handles cloud
fallback for requests that the local pipeline does not handle.

## Repository layout

| Path | Purpose |
| --- | --- |
| `src/bibit_intent/` | Core Python package |
| `src/bibit_intent/core/` | Registry, sessions, number parsing, and shared utilities |
| `src/bibit_intent/routing/` | Rules, intent routing, semantic retrieval, and question detection |
| `src/bibit_intent/execution/` | Executor, system tools, skill loading, and cancellation |
| `src/bibit_intent/models/` | Local and cloud clients and model management |
| `src/bibit_intent/knowledge/` | BM25 knowledge retrieval and hallucination statistics |
| `src/bibit_intent/pipeline.py` | Runtime-neutral integration API |
| `src/bibit_intent/cli.py` | Standalone CLI |
| `src/bibit_intent/resources/` | Packaged tool declarations, skills, knowledge, and routing corpora |
| `deploy/` | Deployment scripts, configuration examples, and systemd units |
| `docs/` | English and Chinese design documents |

## Get the source

Binary dependencies (`.so` and `.whl` files) are stored using Git LFS. Install
Git LFS before cloning:

```bash
git lfs install
git clone git@github.com:spacemit-com/bibit-intent.git
cd bibit-intent
git lfs pull
```

## Quick start

Use `uv` on an x86_64 development machine. For Bianbu riscv64 devices, follow
[Device deployment](#device-deployment), using the deployment scripts,
`requirements.txt`, pip, and platform-compatible wheels. Do not run `uv sync`
on the target device.

```bash
# 1. Install the package on a development machine.
uv sync

# 2. Run the rule-engine self-test; no model service is required.
uv run bibit-intent --selftest

# 3. Install and configure a compatible AI Gateway on the host running inference.
# Explicitly register and download models. See docs/design/design.en.md.
# Load the deployment configuration for both the CLI and service, if available.
set -a
. /etc/bibit-intent/inference.env
set +a

# 4. Run interactively or send a single request.
uv run bibit-intent
uv run bibit-intent "Set the volume to 60"
```

Alternatively, install with pip:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/bibit-intent --selftest
```

## Tests

The repository includes standalone self-test scripts with `run()` entry points
and pytest cases. Use the entry point appropriate for each test file. For
standalone scripts:

```bash
for f in tests/test_*.py; do uv run --group dev python "$f"; done
```

Tests involving real services or system controls require the corresponding
runtime environment.

## Runtime plugin integration

`bianbu-agent-runtime` runs the same core through its bundled
`lib/bibit_intent/` package. Use the Runtime repository's synchronization script
to copy this repository's `src/bibit_intent/` into the plugin:

```bash
../bianbu-agent-runtime/scripts/sync-bibit-intent-lib.sh
```

The plugin hook uses the runtime-neutral API:

```python
from bibit_intent.pipeline import EmbeddedBibitPipeline, PipelineSettings

pipeline = EmbeddedBibitPipeline.from_package(data_root=..., settings=...)
```

The plugin's bundled `lib/bibit_intent/` is the actual import source on the
device. This repository can also be built as a standalone `bibit-intent` wheel
for installation, testing, or distribution through the Spacemit PyPI mirror
(`git.spacemit.com/archive/pypi`).

## Device deployment

Target platform: SpacemiT K3 / Bianbu, RISC-V 64-bit. Replace the example device
addresses and usernames below with your own.

### Online deployment

Install `rsync` and `sshpass` on the development machine:

```bash
# Ubuntu / Debian
sudo apt install rsync sshpass

# macOS
brew install rsync sshpass
```

With the device reachable over the network, run the script and enter its IP
address, username, and password when prompted:

```bash
./deploy/deploy.sh
```

The script checks SSH connectivity, synchronizes code to `/opt/bibit/`, and
installs dependencies in a virtual environment using `requirements.txt`.
`main.py` adds `src/` to the import path, so building a wheel first is not
required. The script uses the Spacemit PyPI mirror by default and can use the
pre-downloaded `bibit-wheels/` cache when the device is offline. It also sets up
the systemd service configured by the deployment script.

Install and configure AI Gateway separately and explicitly provision models;
installing the Python package does not make models ready for inference.

### Offline deployment

Download riscv64 wheels on a machine with network access, transfer the cache,
and run the deployment script:

```bash
# 1. Download wheels on the development machine.
./deploy/download_wheels.sh

# 2. Transfer the cache to the device.
scp -r bibit-wheels/ root@10.0.91.72:/opt/bibit-wheels

# 3. Deploy using the local dependency cache.
./deploy/deploy.sh
```

### Verify deployment

```bash
ssh bianbu@10.0.91.72

# Rule-engine self-test; no model service is required.
.venv/bin/python /opt/bibit/main.py --selftest

# Full pipeline; requires a compatible AI Gateway and registered models.
.venv/bin/python /opt/bibit/main.py 'Scan Wi-Fi networks'
.venv/bin/python /opt/bibit/main.py 'Set the volume to 60'
```

### Deployment resources

| File | Purpose |
| --- | --- |
| `deploy/deploy.sh` | Main deployment script |
| `deploy/download_wheels.sh` | Download riscv64 wheels for offline installation |
| `deploy/ai-gateway-bibit.yaml` | Example AI Gateway configuration |
| [English design](docs/design/design.en.md) / [Chinese design](docs/design/design.md) | Architecture, Gateway settings, and deployment boundaries |

## Key design decisions

- **Compact local output with GBNF:** grammar constraints reduce prompt overhead
  under the default 512-token context budget. Tool selection and parameters
  still require validation.
- **Bounded generation:** disable thinking output when using models such as
  Qwen3 in a constrained local context.
- **Lightweight edge implementation:** the routing and execution layers do not
  depend on LangChain.
- **Pure Python BM25 retrieval:** reduce native dependencies on the device.
  Measure performance on the actual target hardware.
