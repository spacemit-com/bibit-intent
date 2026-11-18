# Native riscv64 Build Guide

[English (primary)](BUILDING.md) | [简体中文](BUILDING.zh-CN.md)

Build `bibit-intent` and its Debian package directly on a SpacemiT K3 running Bianbu Desktop (`riscv64`). This guide does not cover cross-compilation.

## 1. Requirements

Use the latest maintained source. The Debian package version comes from `debian/changelog`; the Python project version in `pyproject.toml` is separate.

| Dependency | Purpose |
| --- | --- |
| `debhelper`, `dpkg-dev`, `python3.14` | Debian packaging; `debhelper` provides the required compatibility level 13 |
| `python3.14-venv` | An isolated Python environment with pip |
| `libfst26` | OpenFst shared libraries for the bundled Pynini module |
| `git`, `git-lfs`, `ca-certificates` | Source checkout and HTTPS downloads |

Rust, Node and Yarn are not required to package this component. The source project declares Python >=3.11, but the Debian package and bundled native wheel require Python 3.14 on riscv64. Use Python 3.14 for the steps below.

## 2. Get the source

Clone the public repository with Git LFS installed to retrieve bundled binary dependencies:

```bash
git lfs install
git clone https://github.com/spacemit-com/bibit-intent.git
cd bibit-intent
git switch main
git pull --ff-only origin main
git lfs pull
git status --porcelain
git rev-parse HEAD
```

Save local changes before updating an existing checkout. Use the latest maintained version rather than checking out a historical commit. Record the actual commit when reporting build issues.

To connect remotely, run `whoami` (or `id -un`) and `ip addr` (or `ifconfig`, if installed) in the K3 desktop user's terminal. Use the active Ethernet/Wi-Fi interface's `inet` address, without its `/prefix`; do not use loopback or a container address. Run the following on the connecting computer, replacing both placeholders:

```text
ssh <device-user>@<device-ip>
```

Do not use your other computer's username. If `whoami` reports `root` after `sudo -i` or `su`, return to the ordinary desktop user's terminal. SSH requires a reachable device, an enabled SSH server and an account allowed to log in.

## 3. Install build and self-test dependencies

```bash
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  debhelper dpkg-dev python3.14 python3.14-venv libfst26 \
  git git-lfs ca-certificates
```

Update the package index before installing dependencies. If venv creation reports `ensurepip is not available`, install `python3.14-venv` and recreate the environment.

Pynini and WeTextProcessing are bundled under `src/bibit_intent/deps/`; the wheels in `wheels/` are their regeneration sources. Normal packaging does not require rebuilding Pynini or installing `libfst-dev`. Full runtime dependencies are listed in `debian/control`; not all are required just to build the DEB.

## 4. Install the Python project and run self-checks

Use venv and pip on the device, not `uv sync`:

```bash
python3.14 -m venv .venv
PIP_INDEX_URL=https://git.spacemit.com/api/v4/projects/33/packages/pypi/simple
.venv/bin/python -m pip install --index-url "$PIP_INDEX_URL" --timeout 120 --retries 2 -r requirements.txt
.venv/bin/python -m pip install --index-url "$PIP_INDEX_URL" --timeout 120 --retries 2 -e .
.venv/bin/python -m pip check
.venv/bin/bibit-intent --selftest
PYTHONPATH="$PWD/src/bibit_intent/deps/pynini" \
  .venv/bin/python -c 'import pynini; print(pynini.__version__)'
```

This uses the SpaceMiT private PyPI index without changing system Python or global pip settings. Editable installation also reads `pyproject.toml`, including dependencies such as `psutil`; installing only `requirements.txt` does not install the project itself. Match native wheels to the Python ABI and riscv64 architecture. Never put access tokens into source files or commits.

The rule self-test does not require Gateway models and does not execute device actions. Check Pynini separately: a passing rule self-test does not prove that its shared libraries can load. If importing Pynini reports missing `libfst*.so.26`, check that `libfst26` is installed.

## 5. Build the Debian package

From the repository root:

```bash
dpkg-checkbuilddeps
dpkg-buildpackage -us -uc -b
```

The package copies Python source, bundled dependencies, deployment scripts, user service units and policy files; it has no native compilation step. The DEB is written to the parent directory as `bibit-intent_<version>_riscv64.deb`.

The current packaging may warn that `${shlibs:Depends}` is undefined because shlibs scanning is disabled. Check the command's exit status and the generated package; separately verify native runtime libraries rather than treating package creation as a runtime test.

Python dependencies in `/opt/bibit-intent/.venv` are installed by `postinst`, not by DEB construction. Installation therefore needs access to the configured PyPI index or an appropriately prepared dependency cache.

## 6. Install the package

**Read section 7 before installing: installation changes Gateway configuration and may restart services.** APT resolves runtime dependencies, including `deepseek-harness`, Filesearch, Gateway and system tools. Do not force installation while ignoring dependencies.

Replace the filename below with the generated DEB path. Run these commands from the repository root:

```bash
sudo apt-get update
sudo apt install ../bibit-intent_<version>_riscv64.deb
systemctl --user status bibit-intent.service
```

The placeholder `<version>` must be replaced before executing the command. An optional `apt-get -s install <deb-path>` checks dependency resolution without executing package installation scripts.

## 7. Installation effects

- `postinst` creates `/opt/bibit-intent/.venv` and installs `requirements.txt` from the SpaceMiT private PyPI index.
- It updates Gateway's `embed.default_args` in `/opt/spacemit-ai-gateway/configs/base.yaml`, creates a `.pre-bibit-embedding` backup when changing the configuration, and may restart `spacemit-ai-gateway.service`.
- It manages legacy MCP manifests and may restart an active Runtime and logged-in users' Intent services.
- The package enables a user service through `default.target.wants`. That service wants `bibit-intent-models.service`, which may initialize models through Gateway.

There is no installation opt-out switch for the Gateway changes. If Gateway must remain unchanged, do not install the DEB; use the isolated source environment for rule self-checks only. Save ongoing work before installing.

## 8. Configuration and post-install checks

| Item | Location / default |
| --- | --- |
| Application | `/opt/bibit-intent` |
| Inference configuration | `/etc/bibit-intent/inference.env` |
| MCP manifest | `/opt/bibit-intent/mcp.json` |
| User services | `bibit-intent.service`, `bibit-intent-models.service` |
| Intent HTTP endpoint | `127.0.0.1:8765` |
| Gateway endpoint | `127.0.0.1:18790` |
| Default LLM | `glm-edge-1.5b-chat-q4_0` |
| Default embedding model | `qwen3-embedding-0.6b-q4_0` |

Gateway is a separate dependency, not built by this repository. Follow its deployment requirements for runtime and model preparation. Run user-service commands as the ordinary desktop user, not root (`ConditionUser=!root`).

```bash
/opt/bibit-intent/.venv/bin/python /opt/bibit-intent/main.py --selftest
systemctl --user status bibit-intent.service
systemctl --user status bibit-intent-models.service
curl -fsS http://127.0.0.1:8765/healthz
```

Expect the rule self-test to pass, the Intent service to be running, and the health endpoint to return HTTP 200. A healthy HTTP endpoint does not prove models or device tools work: verify model readiness, then test the required read-only requests and MCP workflow. Test settings changes only when you intend to change the device state.
