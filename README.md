# 端侧 Agent

Bibit Intent 是面向 Bianbu Linux / SpacemiT RISC-V 设备的意图路由与工具执行组件，支持规则、本地模型和云端升级。当前架构、接口、配置及学习路线见 [中文设计文档](docs/design/design.md) / [English design](docs/design/design.en.md)。

## 架构：三段式路由

```
用户输入
   │
   ▼
① 规则引擎 (rule_engine.py)      高频指令，成本≈0，离线
   │ 未命中/低置信
   ▼
② embedding 语义路由 (embedding.py)  dense 余弦 + BM25 稀疏，先路由再走小模型
   │
   ▼
② 端侧模型 (AI Gateway)        GLM Edge + GBNF，离线
   │ ESCALATE / 不可用
   ▼
③ 云端模型 (cloud_model.py)      复杂编排，需联网
```

## 目录结构

| 路径 | 说明 |
|------|------|
| `src/bibit_intent/` | 独立维护的 Python 包（唯一核心源码） |
| `src/bibit_intent/core/` | 注册表、会话、中文数字等核心基础 |
| `src/bibit_intent/routing/` | 路由、规则、意图、语义检索、问句判别 |
| `src/bibit_intent/execution/` | 执行器、系统工具、Skill 加载、取消控制 |
| `src/bibit_intent/models/` | 本地/云端模型客户端、模型管理 |
| `src/bibit_intent/knowledge/` | BM25 知识库、幻觉统计 |
| `src/bibit_intent/pipeline.py` | Runtime 接入门面 |
| `src/bibit_intent/cli.py` | 独立运行 CLI |
| `src/bibit_intent/resources/` | 随包发布的 `tools.yaml`、`skills/`、`kb/`、路由语料 |
| `deploy/` | systemd 服务单元 |
| `docs/` | 中英文设计文档 |

## 获取代码

仓库中的 `.so` 和 `.whl` 二进制依赖使用 Git LFS。克隆前安装 Git LFS，然后执行：

```bash
git lfs install
git clone git@github.com:spacemit-com/bibit-intent.git
cd bibit-intent
git lfs pull
```

## 快速开始

> 说明：`uv` 仅用于开发机（x86_64）；目标设备是 riscv64（Bianbu），
> 部署请走下面的「端侧部署」（`deploy.sh` + `requirements.txt` + pip，
> 使用 Spacemit PyPI 镜像），不要在设备上执行 `uv sync`。

```bash
# 1. 安装为本地可编辑包（开发机，可选 uv）
uv sync

# 2. 规则引擎自检（不需要模型服务）
uv run bibit-intent --selftest

# 3. 在设备 host 安装并配置兼容的 AI Gateway，显式注册/下载模型
# 架构、配置与部署边界见 docs/design/design.md
# CLI 与服务使用同一份配置：
set -a
. /etc/bibit-intent/inference.env
set +a

# 4. 运行 Agent
uv run bibit-intent                 # 交互式
uv run bibit-intent "把音量调到60"   # 单次
```

如果使用 pip：

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/bibit-intent --selftest
```

## 独立测试

仓库里的 `tests/test_*.py` 是可直接执行的离线脚本：

```bash
for f in tests/test_*.py; do uv run --group dev python "$f"; done
```

测试中同时存在 `run()` 自测脚本和 pytest 用例，请按测试文件的入口运行。
涉及真实服务或系统控制的测试需要对应环境。

## 作为 Runtime 插件接入

`bianbu-agent-runtime` 通过插件自带的 `lib/bibit_intent/` 运行同一套核心。
在 runtime 仓库执行同步脚本，把本仓库的 `src/bibit_intent/` 同步过去：

```bash
../bianbu-agent-runtime/scripts/sync-bibit-intent-lib.sh
```

插件 hook 直接使用包提供的运行时中性 API：

```python
from bibit_intent.pipeline import EmbeddedBibitPipeline, PipelineSettings
pipeline = EmbeddedBibitPipeline.from_package(data_root=..., settings=...)
```

runtime 插件目录自带的 `lib/bibit_intent/` 是设备运行时的实际加载来源。
本仓库仍可发布为独立 `bibit-intent` wheel，用于单独安装、测试或后续
走 Spacemit PyPI 镜像（`git.spacemit.com/archive/pypi`）分发。

## 端侧部署

目标平台：SpacemiT K3 / Bianbu，RISC-V 64 位。以下地址仅为部署示例，请替换为自己的设备。

### 在线部署（推荐）

前提：本机已安装 `rsync` 和 `sshpass`：

```bash
# Ubuntu/Debian
sudo apt install rsync sshpass

# macOS
brew install rsync sshpass
```

设备和本机在同一网络时，运行脚本并按提示输入连接信息：

```bash
./deploy/deploy.sh
```

```
设备 IP 地址 [默认 10.0.91.72]: 10.0.91.72
用户名 [默认 root]: root
密码: ****
```

脚本自动完成：
1. SSH 连通检查
2. `rsync` 同步代码到设备 `/opt/bibit/`
3. 设备上用 venv 执行 `pip install -r requirements.txt`（`main.py` 自带 `src/` 路径，无需先构建包）
   （默认走 Spacemit 官方 PyPI 镜像 `git.spacemit.com/archive/pypi`，
    设备无网络时自动使用本机预下载的 `bibit-wheels/` 缓存）
4. 部署并启用 `bibit-intent-llm.service`（systemd 常驻）

### 离线部署

设备无网络时，先在本机预下载 riscv64 wheel 包：

```bash
# 1. 本机预下载（需本机有网）
./deploy/download_wheels.sh

# 2. 将 wheel 缓存传到设备
scp -r bibit-wheels/ root@10.0.91.72:/opt/bibit-wheels

# 3. 正常部署（pip 自动使用本地缓存）
./deploy/deploy.sh
```

### 部署后验证

```bash
ssh bianbu@10.0.91.72

# 规则引擎自检（不依赖模型服务）
.venv/bin/python /opt/bibit/main.py --selftest

# 完整链路（需兼容的 AI Gateway 和已注册模型）
.venv/bin/python /opt/bibit/main.py '扫描wifi'
.venv/bin/python /opt/bibit/main.py '把音量调到60'
```

### deploy/ 目录说明

| 文件 | 说明 |
|------|------|
| `deploy/deploy.sh` | 主部署脚本 |
| `deploy/download_wheels.sh` | 预下载 riscv64 wheel（离线用） |
| `deploy/ai-gateway-bibit.yaml` | AI Gateway 配置示例 |
| `docs/design/design.md` / `design.en.md` | 当前架构、Gateway 配置及部署边界 |

## 关键设计决策

- **第二段用 GBNF grammar 而非 function calling**：512T 预算下 function schema 太贵（单工具 ~165 token）。GBNF 减少提示开销并约束输出格式，工具含义与参数仍需校验。
- **必须关闭 Qwen3 thinking**：否则思考链吃爆 token 预算。
- **端侧全部手写、不用 LangChain**：极致轻量可控；LangChain 仅建议用于云端第三段。
- **知识检索包含纯 Python BM25**：减少端侧依赖；具体性能应在目标设备测量。
