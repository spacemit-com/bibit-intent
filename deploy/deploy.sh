#!/usr/bin/env bash
# deploy/deploy.sh — 将 bibit 部署到端侧设备并安装 Python 依赖
#
# 部署约定：
#   - bibit_intent 源码部署到 $REMOTE_DIR/src/bibit_intent（默认 /opt/bibit-intent/src/bibit_intent）。
#   - runtime 插件已不再内嵌 lib/，插件 hook.py / console.py 通过
#     BIBIT_INTENT_SRC（默认 /opt/bibit-intent/src）加载这里的 bibit_intent。
#   - 本脚本只同步独立仓库代码；插件壳代码由 bianbu-agent-runtime 仓库维护。
#
# 用法：
#   ./deploy/deploy.sh          # 交互式输入 IP、用户名、密码
#
# 前提：
#   - 本机已安装 rsync
#   - 本机已安装 sshpass（用于密码认证）
#     Ubuntu/Debian: sudo apt install sshpass
#     macOS:         brew install sshpass
#   - 设备已联网（用于 pip 下载），或已提前用 download_wheels.sh 缓存

set -euo pipefail

REMOTE_DIR="/opt/bibit-intent"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== bibit 端侧部署 ==="
echo ""

# ── 交互式输入连接信息 ───────────────────────────────────────────────────
read -rp "设备 IP 地址 [默认 10.0.91.72]: " HOST
HOST="${HOST:-10.0.91.72}"

read -rp "用户名 [默认 root]: " USER
USER="${USER:-root}"

read -rsp "密码: " PASSWORD
echo ""

TARGET="${USER}@${HOST}"

# 检查 sshpass 是否可用
if ! command -v sshpass &>/dev/null; then
    echo ""
    echo "✗ 未找到 sshpass，请先安装："
    echo "    Ubuntu/Debian: sudo apt install sshpass"
    echo "    macOS:         brew install sshpass"
    exit 1
fi

SSH="sshpass -p ${PASSWORD} ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10"
RSYNC_SSH="sshpass -p ${PASSWORD} ssh -o StrictHostKeyChecking=no"
SCP="sshpass -p ${PASSWORD} scp -o StrictHostKeyChecking=no"

echo ""
echo "目标：$TARGET"
echo "远端路径：$REMOTE_DIR"
echo ""

# ── 1. 检查 SSH 连通性 ──────────────────────────────────────────────────
echo "[1/4] 检查 SSH 连通性..."
if ! $SSH "$TARGET" "echo OK" &>/dev/null; then
    echo "✗ 无法连接 $TARGET，请确认："
    echo "  - 设备已开机且在同一网络"
    echo "  - IP 地址、用户名、密码正确"
    exit 1
fi
echo "✓ SSH 连通"

# ── 2. 同步项目代码 ─────────────────────────────────────────────────────
echo ""
echo "[2/4] 同步项目代码..."
rsync -avz --progress \
    -e "$RSYNC_SSH" \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.git' \
    --exclude 'tests/' \
    --exclude 'docs/' \
    --exclude 'deploy/' \
    --exclude '.env' \
    --exclude 'wheels/' \
    "$PROJECT_DIR/" "$TARGET:$REMOTE_DIR/"
echo "✓ 代码同步完成"

# ── 3. 在设备上安装 Python 依赖（含 pynini + WeTextProcessing wheel）──
echo ""
echo "[3/4] 安装 Python 依赖..."
$SCP "$SCRIPT_DIR/install_deps.sh" "$TARGET:$REMOTE_DIR/install_deps.sh"
$SSH "$TARGET" bash <<'REMOTE'
set -euo pipefail
cd "$REMOTE_DIR"

# install_deps.sh 负责：libfst26 / requirements.txt / vendored deps（pynini + WeTextProcessing）
# 需要 root；deploy.sh 默认以 root 登录。若用普通用户，请手动执行：
#   sudo bash "$REMOTE_DIR/install_deps.sh" "$REMOTE_DIR"
if [ "$(id -u)" -ne 0 ]; then
    echo "✗ 依赖安装需 root 权限，请以 root 运行 deploy.sh，或手动执行："
    echo "  sudo bash $REMOTE_DIR/install_deps.sh $REMOTE_DIR"
    exit 1
fi
bash "$REMOTE_DIR/install_deps.sh" "$REMOTE_DIR"
echo "  ✓ 依赖安装完成"
REMOTE

# ── 4. 部署 bibit-intent systemd user 服务 ──────────
echo ""
echo "[4/4] 部署 bibit-intent systemd user 服务（整机单实例、非 root 用户运行）..."
$SCP "$SCRIPT_DIR/retire-legacy-models.sh" "$TARGET:$REMOTE_DIR/retire-legacy-models.sh"
$SCP "$SCRIPT_DIR/bibit-intent-init.sh" "$TARGET:$REMOTE_DIR/bibit-intent-init.sh"
$SCP "$SCRIPT_DIR/bibit-agent-upgrade.sh" "$TARGET:$REMOTE_DIR/bibit-agent-upgrade.sh"
$SCP "$SCRIPT_DIR/bibit-intent.service" "$TARGET:/etc/systemd/user/bibit-intent.service"
$SCP "$SCRIPT_DIR/bibit-intent-models.service" "$TARGET:/etc/systemd/user/bibit-intent-models.service"
$SSH "$TARGET" bash <<'REMOTE'
set -e
/bin/sh /opt/bibit-intent/retire-legacy-models.sh
systemctl daemon-reload
APP_USER=""
ALL_USERS=""
SEAT_USERS=""
if command -v loginctl >/dev/null 2>&1; then
    ALL_USERS=$(loginctl list-sessions --no-legend 2>/dev/null | awk 'NF >= 3 {print $3}' | sort -u)
    SEAT_USERS=$(loginctl list-sessions --no-legend 2>/dev/null | awk 'NF >= 4 && $4 != "-" {print $3}' | sort -u)
fi
for u in $SEAT_USERS $ALL_USERS; do
    [ "$u" = "root" ] && continue
    uid=$(id -u "$u" 2>/dev/null) || continue
    [ -d "/run/user/$uid" ] || continue
    APP_USER="$u"
    break
done
if [ -z "$APP_USER" ]; then
    echo "  ⚠ 未找到已登录的非 root 用户，跳过 bibit 服务 enable/start"
    exit 0
fi
APP_UID=$(id -u "$APP_USER")
runuser -u "$APP_USER" -- env XDG_RUNTIME_DIR="/run/user/$APP_UID" \
    systemctl --user daemon-reload >/dev/null 2>&1 || true
runuser -u "$APP_USER" -- env XDG_RUNTIME_DIR="/run/user/$APP_UID"     systemctl --user stop bibit-intent.service >/dev/null 2>&1 || true
APP_HOME=$(getent passwd "$APP_USER" | cut -d: -f6)
if [ -f /etc/bibit-intent/inference.env ] && [ -n "$APP_HOME" ]; then
    PYTHONPATH=/opt/bibit-intent/src /opt/bibit-intent/.venv/bin/python         -m bibit_intent.models.gateway_setup --migrate-legacy --cleanup-legacy         --legacy-home "$APP_HOME" --env-file /etc/bibit-intent/inference.env         || echo "Gateway migration pending; original models preserved"
fi
for svc in bibit-intent; do
    runuser -u "$APP_USER" -- env XDG_RUNTIME_DIR="/run/user/$APP_UID" \
        systemctl --user enable "$svc" >/dev/null 2>&1 || true
    if ! runuser -u "$APP_USER" -- env XDG_RUNTIME_DIR="/run/user/$APP_UID" \
        systemctl --user is-active --quiet "$svc"; then
        if runuser -u "$APP_USER" -- env XDG_RUNTIME_DIR="/run/user/$APP_UID" \
            systemctl --user start "$svc" 2>/dev/null; then
            echo "  ✓ $svc 已启动"
        else
            echo "  ⚠ $svc 启动失败，见 journalctl --user -u $svc"
        fi
    else
        echo "  ✓ $svc 已在运行（跳过重启）"
    fi
done
for u in $ALL_USERS; do
    [ "$u" = "$APP_USER" ] && continue
    [ "$u" = "root" ] && continue
    uid=$(id -u "$u" 2>/dev/null) || continue
    [ -d "/run/user/$uid" ] || continue
    runuser -u "$u" -- env XDG_RUNTIME_DIR="/run/user/$uid" \
        systemctl --user disable bibit-intent >/dev/null 2>&1 || true
done
REMOTE

echo ""
echo "=== 部署完成 ==="
echo ""
echo "验证命令（在设备上执行）："
echo "  .venv/bin/python $REMOTE_DIR/main.py --selftest"
echo "  .venv/bin/python $REMOTE_DIR/main.py '扫描wifi'"
echo "  .venv/bin/python $REMOTE_DIR/main.py serve --data-root $REMOTE_DIR --bind 127.0.0.1:8765"
echo ""
echo "插件加载约定（在 runtime 中）："
echo "  BIBIT_INTENT_SRC=$REMOTE_DIR/src  # 默认值，插件从此处 import bibit_intent"

echo "AI Gateway must be installed/configured separately. Provision models explicitly; see docs/design/design.md."
