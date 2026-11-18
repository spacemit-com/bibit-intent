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
| `BUILDING.md` / `BUILDING.zh-CN.md` | Native K3 build, installation, and service checks |

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

For native K3 builds, dependency installation, DEB packaging, and service
checks, follow the [English build guide](BUILDING.md) or
[简体中文构建指南](BUILDING.zh-CN.md). Read the installation effects before
installing: package installation updates Gateway configuration and may restart
services. The Debian package and bundled native dependencies require Python
3.14 on riscv64.

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
at the following index:

```text
https://git.spacemit.com/api/v4/projects/33/packages/pypi/simple
```

To install dependencies in the project virtual environment:

```bash
.venv/bin/python -m pip install --index-url https://git.spacemit.com/api/v4/projects/33/packages/pypi/simple -r requirements.txt
```

## Device deployment

Target platform: SpacemiT K3 / Bianbu, RISC-V 64-bit. Replace the example device
addresses and usernames below with your own.

### Find the device username and IP address

Run these commands in the ordinary desktop user's terminal **on the K3**:

```bash
whoami
ip addr
```

Use the username returned by `whoami` (or `id -un`) and the active Ethernet or
Wi-Fi interface's IPv4 `inet` address, without the `/prefix`. Do not use
loopback, container addresses, or the username on your development computer.
If you switched to root with `sudo -i` or `su`, return to the desktop user's
terminal first. `ifconfig` is also suitable if installed.

Connect from another computer, replacing both placeholders:

```text
ssh <device-user>@<device-ip>
```

The device must be reachable, with SSH enabled and the account allowed to log
in. Recheck its IP after reconnecting to the network. Ordinary users can use
`sudo` to install the DEB as described in the build guide; root SSH is not
required for that workflow.

### Online deployment

Install `rsync` and `sshpass` on the development machine:

```bash
# Ubuntu / Debian
sudo apt install rsync sshpass

# macOS
brew install rsync sshpass
```

The source deployment script writes to `/opt` and system service directories
and requires remote root privileges; it does not automatically use `sudo` for
ordinary users. Use it only on devices where root SSH is enabled and authorized.
Otherwise, follow the native DEB build and installation guide.

With the device reachable, run the script and explicitly enter your device's
IP address, username, and password. Override the preset test address:

```bash
./deploy/deploy.sh
```

The script checks SSH connectivity, synchronizes code to `/opt/bibit-intent/`, and
installs dependencies in a virtual environment using `requirements.txt`.
`main.py` adds `src/` to the import path, so building a wheel first is not
required. The script uses the Spacemit PyPI mirror by default and can use the
pre-downloaded `bibit-wheels/` cache when the device is offline. It also sets up
the desktop user’s `bibit-intent.service` and associated
`bibit-intent-models.service`.

Install and configure AI Gateway separately and explicitly provision models;
installing the Python package does not make models ready for inference.

### Offline deployment

Download riscv64 wheels on a machine with network access, transfer the cache,
and run the deployment script:

```bash
# 1. Download wheels on the development machine.
./deploy/download_wheels.sh

# 2. Transfer the cache to the device.
scp -r bibit-wheels/ root@<device-ip>:/opt/bibit-wheels

# 3. Deploy using the local dependency cache.
./deploy/deploy.sh
```

### Verify deployment

```bash
ssh <device-user>@<device-ip>

# Rule-engine self-test; no model service is required.
/opt/bibit-intent/.venv/bin/python /opt/bibit-intent/main.py --selftest

# Full pipeline; requires a compatible AI Gateway and registered models.
/opt/bibit-intent/.venv/bin/python /opt/bibit-intent/main.py 'Scan Wi-Fi networks'
/opt/bibit-intent/.venv/bin/python /opt/bibit-intent/main.py 'Set the volume to 60'
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

## File search and local summaries

File search returns structured `bibit.file-results.v1` data in
`ExecResult.detail` and MCP `_meta["bibit.file-results"]`, alongside the existing
text interface. Result counts cover the search service's Top-K candidates;
relative match scores are not probabilities of correctness. Restricted file
actions use the local `/v1/file-actions` endpoint and current index authorization,
with fixed file descriptors passed to the desktop portal. The system needs
`python3-gi`, `xdg-desktop-portal`, `poppler-utils`, and `util-linux`.

Content extraction supports UTF-8 text, text-layer PDFs, and DOCX files up to
16 MiB. Summaries process at most the first 12,000 characters and indicate
truncation. DOCX extraction preserves document order, paragraphs, line breaks,
and tabs, with bounded output and cancellation checks. XML parsing retains its
4 MiB limit; individual ZIP reads and XML parsing are not interruptible.
Malformed compressed DOCX content fails explicitly without discarding other
search results.

File content snippets are stripped from ordinary replies and MCP tool text;
cards receive their display data through structured results. The pipeline
routes `BIBIT_FILE_SUMMARY_V1` references directly to local model completion.
The model selects original passages, and the backend verifies that each passage
occurs in the extracted text, including when merging long-document results.
Output retains the source language and is labeled as excerpts. Unsupported
rewrites, invalid output, or unavailable services fail explicitly. This does
not guarantee coverage of every key point or verify the source's truthfulness.

Keeping file contents out of cloud requests requires compatible GUI, Runtime,
and Intent versions with the corresponding routing and failure handling. Card
support alone does not establish that guarantee. Validate the desktop portal,
real model, and end-to-end Runtime integration on the target device.

For focused regressions in an existing environment with pytest installed:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest -p no:cacheprovider \
  tests/test_file_results.py tests/test_file_search_privacy.py \
  tests/test_local_file_search.py tests/test_execution_boundary.py \
  tests/test_bounded_command.py -q
```

Run the complete pytest suite with `.venv/bin/python -m pytest -q`. Some tests
need additional dependencies or device services; passing local tests does not
replace target-device validation.
