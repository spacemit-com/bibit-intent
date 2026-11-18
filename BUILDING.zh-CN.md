# 原生 riscv64 构建指南

[English（主版本）](BUILDING.md) | [简体中文](BUILDING.zh-CN.md)

本文介绍如何在运行 Bianbu Desktop 的 SpacemiT K3（`riscv64`）上原生构建 `bibit-intent` 及其 Debian 包，不涉及交叉编译。

## 1. 环境要求

使用最新维护版本的源码。DEB 版本以 `debian/changelog` 为准，`pyproject.toml` 中的 Python 项目版本独立维护。

| 依赖 | 用途 |
| --- | --- |
| `debhelper`、`dpkg-dev`、`python3.14` | Debian 打包；`debhelper` 提供所需的兼容级别 13 |
| `python3.14-venv` | 创建包含 pip 的独立 Python 环境 |
| `libfst26` | 内置 Pynini 所需的 OpenFst 动态库 |
| `git`、`git-lfs`、`ca-certificates` | 源码获取与 HTTPS 下载 |

本组件打包不需要 Rust、Node 或 Yarn。源码声明 Python >=3.11，但 Debian 包和内置原生 wheel 要求 riscv64 上的 Python 3.14，以下步骤使用 Python 3.14。

## 2. 获取源码

安装 Git LFS 后克隆公开仓库，以获取内置二进制依赖：

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

更新已有工作区前先保存本地修改。使用最新维护版本，无需检出历史提交；反馈构建问题时记录实际 commit。

远程连接前，在 K3 桌面用户的终端执行 `whoami`（或 `id -un`）和 `ip addr`（或已安装的 `ifconfig`）。选择已连接有线或无线网卡的 `inet` 地址，去掉 `/前缀长度`，不要使用回环或容器地址。在连接设备的另一台电脑上执行以下命令，替换两个占位内容：

```text
ssh <设备用户名>@<设备IP>
```

不要使用另一台电脑的用户名。若通过 `sudo -i` 或 `su` 切换后 `whoami` 显示 `root`，请回到普通桌面用户终端。SSH 还要求设备网络可达、SSH 服务已启用且账号允许登录。

## 3. 安装构建和自检依赖

```bash
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  debhelper dpkg-dev python3.14 python3.14-venv libfst26 \
  git git-lfs ca-certificates
```

安装依赖前先更新软件包索引。若创建 venv 提示 `ensurepip is not available`，请安装 `python3.14-venv` 后重新创建环境。

Pynini 和 WeTextProcessing 随源码放在 `src/bibit_intent/deps/`，`wheels/` 用于重新生成这些依赖。普通打包无需重编 Pynini 或安装 `libfst-dev`。完整运行依赖以 `debian/control` 为准，不必为了生成 DEB 预先安装全部运行组件。

## 4. 安装 Python 项目并自检

端侧使用 venv 和 pip，不使用 `uv sync`：

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

以上使用 SpaceMiT 私有 PyPI 源，不修改系统 Python 或全局 pip 配置。editable 安装还会读取 `pyproject.toml`（包括 `psutil` 等依赖），仅安装 `requirements.txt` 不等于安装项目本身。原生 wheel 应匹配 Python ABI 和 riscv64 架构，不要将访问令牌写入源码或提交。

规则自检不需要 Gateway 模型，也不会执行设备操作。Pynini 要单独检查：规则自检通过不代表动态库可以加载。若导入时报缺少 `libfst*.so.26`，请检查是否安装了 `libfst26`。

## 5. 构建 Debian 包

在仓库根目录执行：

```bash
dpkg-checkbuilddeps
dpkg-buildpackage -us -uc -b
```

本包复制 Python 源码、内置依赖、部署脚本、用户服务和策略文件，没有原生编译步骤。DEB 输出到上一级目录，名称为 `bibit-intent_<版本>_riscv64.deb`。

由于关闭 shlibs 扫描，当前打包可能提示 `${shlibs:Depends}` 未定义。请检查命令退出状态和产物，并单独检查原生运行库，不要把生成 DEB 等同于运行测试通过。

`/opt/bibit-intent/.venv` 的 Python 依赖由 `postinst` 安装，而非在 DEB 构建阶段安装。因此部署时需要访问配置的 PyPI 源，或提前准备相应依赖缓存。

## 6. 安装软件包

**安装前请先阅读第 7 节：安装会修改 Gateway 配置，并可能重启服务。** 使用 APT 处理 `deepseek-harness`、Filesearch、Gateway 和系统工具等运行依赖，不要强制忽略依赖安装。

将下方文件名替换为生成的 DEB 路径，在仓库根目录执行：

```bash
sudo apt-get update
sudo apt install ../bibit-intent_<版本>_riscv64.deb
systemctl --user status bibit-intent.service
```

执行前必须替换 `<版本>`。也可先用 `apt-get -s install <DEB路径>` 模拟依赖解析，该命令不会执行软件包安装脚本。

## 7. 安装对现有配置和服务的影响

- `postinst` 创建 `/opt/bibit-intent/.venv`，从 SpaceMiT 私有 PyPI 源安装 `requirements.txt`。
- 更新 `/opt/spacemit-ai-gateway/configs/base.yaml` 中的 `embed.default_args`，修改配置时创建 `.pre-bibit-embedding` 备份，并可能重启 `spacemit-ai-gateway.service`。
- 管理旧 MCP manifest，并可能重启已运行的 Runtime 和已登录用户的 Intent 服务。
- 通过 `default.target.wants` 启用用户服务；该服务关联的 `bibit-intent-models.service` 可能通过 Gateway 初始化模型。

目前没有跳过 Gateway 配置变更的安装开关。如需保持 Gateway 不变，不要安装 DEB，仅在独立源码环境进行规则自检。安装前请保存正在进行的工作。

## 8. 配置与安装后检查

| 项目 | 路径或默认值 |
| --- | --- |
| 应用目录 | `/opt/bibit-intent` |
| 推理配置 | `/etc/bibit-intent/inference.env` |
| MCP manifest | `/opt/bibit-intent/mcp.json` |
| 用户服务 | `bibit-intent.service`、`bibit-intent-models.service` |
| Intent HTTP 端点 | `127.0.0.1:8765` |
| Gateway 端点 | `127.0.0.1:18790` |
| 默认 LLM | `glm-edge-1.5b-chat-q4_0` |
| 默认 embedding 模型 | `qwen3-embedding-0.6b-q4_0` |

Gateway 是独立依赖，不由本仓构建。请按其部署要求准备运行时和模型。用户服务命令须在普通桌面用户终端执行，而非 root（`ConditionUser=!root`）。

```bash
/opt/bibit-intent/.venv/bin/python /opt/bibit-intent/main.py --selftest
systemctl --user status bibit-intent.service
systemctl --user status bibit-intent-models.service
curl -fsS http://127.0.0.1:8765/healthz
```

预期规则自检通过、Intent 服务运行、健康端点返回 HTTP 200。HTTP 健康不等于模型或设备工具可用：还需确认模型就绪，再验证所需只读请求和 MCP 流程。只有确实希望改变设备状态时，才测试设置操作。
