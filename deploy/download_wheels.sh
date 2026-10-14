#!/usr/bin/env bash
# deploy/download_wheels.sh — 在有网络的机器上预下载 riscv64 wheel 包
# 用于设备无网络时的离线安装
#
# 用法（在本机 x86 / arm 上运行，下载 riscv64 wheel）：
#   ./deploy/download_wheels.sh
#
# 输出目录：./bibit-wheels/（可随项目一起 scp 到设备）
#
# 注：pip download 支持跨平台下载，--platform 指定 riscv64 wheel

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
OUTPUT_DIR="$PROJECT_DIR/bibit-wheels"
# 官方 PyPI 镜像（https://git.spacemit.com/archive/pypi，GitLab Package Registry）
INDEX_URL="https://git.spacemit.com/api/v4/projects/33/packages/pypi/simple"

echo "=== 下载 riscv64 Python wheel 包 ==="
echo "目标目录：$OUTPUT_DIR"
echo ""

mkdir -p "$OUTPUT_DIR"

# Python 3.14 在 riscv64 上的 ABI tag：cp314-cp314-linux_riscv64
# 同时包含 py3-none-any（纯 Python 包）和 riscv64 预编译包
pip3 download \
    --dest "$OUTPUT_DIR" \
    -i "$INDEX_URL" \
    --platform linux_riscv64 \
    --python-version 3.14 \
    --implementation cp \
    --abi cp314 \
    --only-binary=:all: \
    -r "$PROJECT_DIR/requirements.txt" \
    2>&1 || {
    echo ""
    echo "⚠  部分包无 riscv64 预编译 wheel，尝试下载纯 Python 包..."
    # 纯 Python 包（无 C 扩展）可用 none-any wheel
    pip3 download \
        --dest "$OUTPUT_DIR" \
        -i "$INDEX_URL" \
        --no-deps \
        --platform any \
        -r "$PROJECT_DIR/requirements.txt" 2>&1 || true
}

echo ""
echo "已下载 wheel 包："
ls -lh "$OUTPUT_DIR"
echo ""
echo "下一步：将 bibit-wheels/ 目录上传到设备"
echo "  scp -r $OUTPUT_DIR root@10.0.91.72:/opt/bibit-wheels"
echo "然后运行 deploy.sh（会自动使用本地缓存）"
