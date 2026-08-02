#!/bin/bash
# Hermes Nimbus 启动脚本（HTTP + WebSocket 同端口）
# 支持自动发现 Hermes Profile，无需手动配置

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PID_FILE="$PROJECT_DIR/.pids"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/halo_server.log"
CONFIG_PATH="${HERMES_NIMBUS_CONFIG:-${HERMES_HALO_CONFIG:-$HOME/.hermes/hermes-nimbus/config.json}}"
PYTHON="${HERMES_NIMBUS_PYTHON:-${HERMES_HALO_PYTHON:-$(command -v python3)}}"
PORT="${1:-8765}"
HOST="${2:-127.0.0.1}"
LAN_HOST="${3:-}"
ALLOWED_CLIENT="${4:-}"

if { [ -n "$LAN_HOST" ] && [ -z "$ALLOWED_CLIENT" ]; } || \
   { [ -z "$LAN_HOST" ] && [ -n "$ALLOWED_CLIENT" ]; }; then
  echo "❌ 局域网监听地址和允许客户端必须同时指定"
  exit 1
fi

mkdir -p "$LOG_DIR"

echo "🚀 启动 Hermes Nimbus..."
echo "   本机地址: http://$HOST:$PORT"
if [ -n "$LAN_HOST" ]; then
  echo "   局域网地址: http://$LAN_HOST:$PORT（仅允许 $ALLOWED_CLIENT）"
fi
echo "   配置: $CONFIG_PATH"
echo ""

if [ -f "$PID_FILE" ]; then
  # shellcheck disable=SC1090
  source "$PID_FILE"
  if [ -n "${PID:-}" ] && kill -0 "$PID" 2>/dev/null; then
    echo "⚠️  已在运行 (PID: $PID)"
    echo "   如需重启，请先运行: ./scripts/stop.sh"
    exit 1
  fi
  rm -f "$PID_FILE"
fi

if pgrep -f "$PROJECT_DIR/scripts/halo_server.py" >/dev/null 2>&1; then
  echo "⚠️  检测到已有 halo_server 进程，请先运行: ./scripts/stop.sh"
  exit 1
fi

if command -v lsof >/dev/null 2>&1 && lsof -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "❌ 端口 $PORT 已被占用"
  lsof -iTCP:"$PORT" -sTCP:LISTEN || true
  exit 1
fi

if ! "$PYTHON" -c "import aiohttp" 2>/dev/null; then
  echo "📦 安装 aiohttp..."
  "$PYTHON" -m pip install aiohttp
fi

echo "🔌 启动服务..."
cd "$SCRIPT_DIR"
SERVER_ARGS=(
  --host "$HOST"
  --port "$PORT"
  --config "$CONFIG_PATH"
)
if [ -n "$LAN_HOST" ]; then
  SERVER_ARGS+=(--lan-host "$LAN_HOST" --allow-client "$ALLOWED_CLIENT")
fi
nohup "$PYTHON" halo_server.py "${SERVER_ARGS[@]}" >>"$LOG_FILE" 2>&1 &
PID=$!

cat > "$PID_FILE" <<EOF
PID=$PID
PORT=$PORT
HOST=$HOST
CONFIG_PATH=$CONFIG_PATH
LOG_FILE=$LOG_FILE
EOF

# 等待健康检查
for i in $(seq 1 20); do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "❌ 进程已退出，日志: $LOG_FILE"
    tail -n 40 "$LOG_FILE" || true
    rm -f "$PID_FILE"
    exit 1
  fi
  if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    echo ""
    echo "✅ Hermes Nimbus 已启动!"
    echo "   PID: $PID"
    echo "   页面: http://127.0.0.1:$PORT"
    if [ -n "$LAN_HOST" ]; then
      echo "   局域网: http://$LAN_HOST:$PORT（仅允许 $ALLOWED_CLIENT）"
    fi
    echo "   健康检查: http://127.0.0.1:$PORT/api/health"
    echo "   WebSocket: ws://127.0.0.1:$PORT/ws"
    echo "   日志: $LOG_FILE"
    echo ""
    echo "📋 管理命令:"
    echo "   查看状态: ./scripts/status.sh"
    echo "   停止服务: ./scripts/stop.sh"
    exit 0
  fi
  sleep 0.25
done

echo "❌ 启动超时，日志: $LOG_FILE"
tail -n 40 "$LOG_FILE" || true
exit 1
