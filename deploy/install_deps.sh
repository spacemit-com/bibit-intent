#!/usr/bin/env bash
# deploy/install_deps.sh — 在设备上安装 bibit-intent 运行依赖
#
# 安装内容：
#   1. Python venv（如不存在）
#   2. 系统依赖 libfst26（pynini vendored .so 所需的 OpenFst 运行时库）
#   3. requirements.txt 常规依赖
#
# 说明：
#   - WeTextProcessing 与 pynini 不打入 site-packages，而是以 vendored 形式
#     解包在 src/bibit_intent/deps/pynini、src/bibit_intent/deps/wetext 中，运行时由
#     src/bibit_intent/core/text_normalizer.py 自动加载，无需 pip 安装。
#   - src/bibit_intent/deps/ 缺失时，bibit 核心路由不受影响，仅中英文 ITN 归一化增强不可用。
#   - 若要重新生成 deps/，见 wheels/README.md。
#
# 用法（在设备上，需 root）：
#   sudo ./deploy/install_deps.sh [项目根目录]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${1:-$(dirname "$SCRIPT_DIR")}"
cd "$PROJECT_DIR"

if [ "$(id -u)" -ne 0 ]; then
    echo "✗ 请以 root 运行：sudo $0" >&2
    exit 1
fi

SPACEMIT_INDEX="https://git.spacemit.com/api/v4/projects/33/packages/pypi/simple"
WHEEL_CACHE="/opt/bibit-wheels"

# 1. venv
if [ ! -x .venv/bin/python ]; then
    echo "==> 创建 venv ..."
    python3 -m venv .venv 2>/dev/null || {
        apt-get update
        apt-get install -y python3.14-venv
        python3 -m venv .venv
    }
fi
PIP="$(pwd)/.venv/bin/pip"

# 2. 系统依赖：libfst26（提供 libfst.so.26 系列）
if ! ldconfig -p 2>/dev/null | grep -q 'libfst\.so\.26'; then
    echo "==> 安装 libfst26 ..."
    apt-get update
    apt-get install -y libfst26
else
    echo "==> libfst26 已存在，跳过"
fi

# 3. 常规依赖（requirements.txt）
echo "==> 安装 requirements.txt ..."
if [ -d "$WHEEL_CACHE" ]; then
    "$PIP" install --no-index --find-links "$WHEEL_CACHE" -r requirements.txt
else
    "$PIP" install -i "$SPACEMIT_INDEX" -r requirements.txt
fi

# 4. 校验 vendored 依赖
echo "==> 校验 vendored 依赖 src/bibit_intent/deps/ ..."
if [ -d src/bibit_intent/deps/pynini ] && [ -d src/bibit_intent/deps/wetext ]; then
    echo "    ✓ src/bibit_intent/deps/pynini 与 deps/wetext 已就绪（运行时自动加载）"
else
    echo "    ⚠ src/bibit_intent/deps/ 缺失，中英文 ITN 归一化增强不可用（核心路由不受影响）"
fi

echo ""
echo "✓ 全部依赖安装完成"
echo "验证：.venv/bin/python -c \"import bibit_intent.core.text_normalizer as t; print(t.evidence_number_candidates('sixty percent'))\""
