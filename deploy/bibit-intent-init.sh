#!/bin/sh
# bibit-intent-init.sh — bibit-intent.service 启动前的初始化检查（user 级）
#
# 挂在 bibit-intent.service 的 ExecStart 最前面，启动 main.py 前执行：
#   1. 维护 $HOME/.bianbu-agent/mcps/bibit-intent/mcp.json：Runtime 自带的
#      bianbu.bibit.intent 插件已经内置这份定义时，退休用户目录里的历史副本
#      （两个 id 不同，会同时加载）；Runtime 还没有该插件成员时（旧 Runtime）
#      保留原来的写入行为，见下文注释。
set -u

[ "$(id -u)" -ne 0 ] || exit 0
HOME="${HOME:-}"
[ -n "$HOME" ] || exit 0

log() { echo "bibit-intent-init: $*"; }

# ── 1) MCP manifest：插件优先，旧 Runtime 才写用户目录 ────────────────
# Runtime 从 ~/.bianbu-agent/mcps 和 /usr/share/bianbu-agent-runtime 两处发现
# MCP，二者按 id 去重。本包历史上（0.1.14 及更早）把 manifest 装到用户目录，
# 而 Runtime 的 bianbu.bibit.intent 插件内嵌了同一份定义，id 却是
# bibit-intent 对 bianbu.bibit.intent/bibit-intent——插件成员必然带插件前缀，
# 覆盖规则不成立，两份因此同时加载，内置专家的 mcps: ['*'] 展开成两套重名的
# 149 个工具（K3 实测 2026-09-14）。
#
# 插件在时由插件独占分发，用户目录里那份退休；插件不在时（旧 Runtime 还没有
# 这个成员）没有别的来源，保留原来的写入行为——否则升级到本版 bibit 而 Runtime
# 未更新的机器会一个 MCP 都没有。
#
# 退休必须把整个目录挪走，不能只挪 mcp.json：Runtime 要求 MCP 包目录里
# **恰好**只有 mcp.json（`mcp/manager.py::_snapshot_open_package`），
# 留下改名后的备份文件会让整份目录列表报 ValueError。备份放到本包自己的
# 用户目录 ~/.bibit-intent 下（Runtime 不扫描该路径），内容原样保留，
# 需要时可整目录搬回去。
MCP_SRC=/opt/bibit-intent/mcp.json
MCP_DIR="$HOME/.bianbu-agent/mcps/bibit-intent"
RUNTIME_PLUGIN_MCP_ROOT=/usr/share/bianbu-agent-runtime/plugins/bianbu.bibit.intent/mcps
RUNTIME_PLUGIN_MCP=0
for mcp_name in bibit-readonly bibit-control bibit-admin; do
  if [ -f "$RUNTIME_PLUGIN_MCP_ROOT/$mcp_name/mcp.json" ]; then
    RUNTIME_PLUGIN_MCP=1
    break
  fi
done
if [ "$RUNTIME_PLUGIN_MCP" = 1 ]; then
  if [ -f "$MCP_DIR/mcp.json" ]; then
    # 备份目录名必须唯一：`mv a b` 在 b 已存在时会把 a 塞进 b 里面。
    MCP_RETIRED_BASE="$HOME/.bibit-intent/retired-mcp/bibit-intent-$(date '+%Y%m%d%H%M%S')"
    MCP_RETIRED="$MCP_RETIRED_BASE"
    suffix=0
    while [ -e "$MCP_RETIRED" ]; do
      suffix=$((suffix + 1))
      MCP_RETIRED="$MCP_RETIRED_BASE.$suffix"
    done
    if mkdir -p "$(dirname "$MCP_RETIRED")" && mv "$MCP_DIR" "$MCP_RETIRED" 2>/dev/null; then
      log "retired legacy user MCP manifest -> $MCP_RETIRED/mcp.json"
    else
      log "failed to retire legacy MCP manifest at $MCP_DIR"
    fi
  fi
elif [ -f "$MCP_SRC" ]; then
  if [ ! -f "$MCP_DIR/mcp.json" ] || ! cmp -s "$MCP_SRC" "$MCP_DIR/mcp.json"; then
    if mkdir -p "$MCP_DIR" && cp "$MCP_SRC" "$MCP_DIR/mcp.json"; then
      chmod 0644 "$MCP_DIR/mcp.json" 2>/dev/null || true
      log "installed mcp.json -> $MCP_DIR/mcp.json (Runtime ships no Bibit Plugin MCP)"
    else
      log "failed to install mcp.json into $MCP_DIR"
    fi
  fi
fi

if [ -x /opt/bibit-intent/.venv/bin/python ]; then
  PYTHONPATH=/opt/bibit-intent/src /opt/bibit-intent/.venv/bin/python \
    -m bibit_intent.models.gateway_setup --migrate-legacy --cleanup-legacy \
    || log "Gateway model migration pending; originals preserved"
fi
exit 0
