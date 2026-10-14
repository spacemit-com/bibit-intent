#!/bin/sh
set -eu

# 独立 Agent 栈升级入口。必须由 systemd-run --system 拉起，运行在
# bianbu-agent-runtime@.service 的 cgroup 之外；否则升级 Runtime 时停服务会
# 连带杀死正在执行的 apt/dpkg。
#
# 入参：需要升级的 Agent 包名。pkg_update 已先把普通系统包升级完，这里只处理
# bibit-intent / bianbu-agent-runtime / bibit-voice 等 Agent 自身栈。

LOG=${BIBIT_AGENT_UPGRADE_LOG:-/var/log/bibit-agent-upgrade.log}
RUNTIME_UID=${SUDO_UID:-}
# 留出几秒让当前 tool result 先回到前端，再停 Runtime；否则响应还没送达，
# 连接就被关闭，用户仍会看到 EOFError。
STOP_DELAY=${BIBIT_AGENT_UPGRADE_STOP_DELAY:-5}

ALLOW_DOWNGRADES=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --uid)
            [ "$#" -ge 2 ] || exit 2
            RUNTIME_UID=$2
            shift 2
            ;;
        --allow-downgrades)
            ALLOW_DOWNGRADES=1
            shift
            ;;
        *)
            break
            ;;
    esac
done

if [ -z "$RUNTIME_UID" ] && [ "$(id -u)" -ne 0 ]; then
    RUNTIME_UID=$(id -u)
fi
case "$RUNTIME_UID" in
    ''|*[!0-9]*) echo 'A valid initiating user UID is required' >&2; exit 2 ;;
esac
RUNTIME_USER=$(id -nu "$RUNTIME_UID") || exit 2
if [ "$RUNTIME_UID" -eq 0 ]; then
    echo 'Refusing to select root as the desktop Runtime user' >&2
    exit 2
fi
RUNTIME_UNIT=$(systemd-escape --template=bianbu-agent-runtime@.service -- "$RUNTIME_USER")
if [ "$(systemctl show --property=LoadState --value bianbu-agent-runtime.service)" = loaded ]; then
    RUNTIME_UNIT=bianbu-agent-runtime.service
elif [ "$(systemctl show --property=LoadState --value "$RUNTIME_UNIT")" != loaded ]; then
    echo 'No installed Runtime service found' >&2
    exit 2
fi

mkdir -p "$(dirname "$LOG")"

log() {
    echo "[$(date '+%F %T')] $*" >>"$LOG"
}

progress() {
    echo "STATUS $1"
    log "phase=$1"
}

if [ "$#" -eq 0 ]; then
    log "no agent packages specified; nothing to upgrade"
    exit 2
fi

log "agent stack upgrade starting: $*"
progress starting

log "waiting ${STOP_DELAY}s before stopping Runtime"
sleep "$STOP_DELAY"

progress stopping_runtime
if systemctl is-active -q "$RUNTIME_UNIT"; then
    log "stopping $RUNTIME_UNIT"
    systemctl stop "$RUNTIME_UNIT"
fi

progress unholding
log "unholding agent packages: $*"
apt-mark unhold "$@" >>"$LOG" 2>&1 || true

progress upgrading
APT_OPTS="install -y --no-remove --only-upgrade"
if [ "$ALLOW_DOWNGRADES" -eq 1 ]; then
    APT_OPTS="$APT_OPTS --allow-downgrades"
fi

FAILED=0
if ! DEBIAN_FRONTEND=noninteractive apt-get $APT_OPTS "$@" >>"$LOG" 2>&1; then
    FAILED=1
    log "apt-get install failed; attempting dpkg --configure -a"
    DEBIAN_FRONTEND=noninteractive dpkg --configure -a >>"$LOG" 2>&1 || true
    log "re-holding agent packages after failed upgrade"
    apt-mark hold "$@" >>"$LOG" 2>&1 || true
fi

progress restarting_runtime
log "starting $RUNTIME_UNIT"
if ! systemctl start "$RUNTIME_UNIT"; then
    FAILED=1
    log "failed to start $RUNTIME_UNIT"
fi

if [ "$FAILED" -eq 0 ]; then
    progress succeeded
    log "agent stack upgrade finished"
else
    progress failed
    log "agent stack upgrade failed"
    exit 1
fi
