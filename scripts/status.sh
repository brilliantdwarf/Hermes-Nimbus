#!/bin/bash
# Hermes Nimbus 状态检查脚本

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PID_FILE="$PROJECT_DIR/.pids"
PORT=8765
HOST=127.0.0.1

if [ -f "$PID_FILE" ]; then
  # shellcheck disable=SC1090
  source "$PID_FILE"
  PORT="${PORT:-8765}"
fi

echo "📊 Hermes Nimbus 状态检查"
echo "========================"
echo ""

PIDS=$(pgrep -f "$PROJECT_DIR/scripts/halo_server.py" || true)
if [ -n "$PIDS" ]; then
  echo "✅ 服务进程: 运行中 (PID: $PIDS)"
else
  echo "❌ 服务进程: 未运行"
fi

if [ -f "$PID_FILE" ]; then
  echo "📄 PID 文件:"
  cat "$PID_FILE"
  echo ""
fi

echo "🔌 端口检查:"
if command -v lsof >/dev/null 2>&1 && lsof -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "   端口 $PORT: 已监听"
  lsof -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | sed 's/^/   /'
else
  if ss -ltn "( sport = :$PORT )" 2>/dev/null | rg -q ":$PORT"; then
    echo "   端口 $PORT: 已监听"
  else
    echo "   端口 $PORT: 空闲"
  fi
fi

echo ""
echo "🩺 健康检查:"
if curl -fsS "http://$HOST:$PORT/api/health" >/tmp/hermes-nimbus-health.json 2>/dev/null; then
  echo "   ✅ http://$HOST:$PORT/api/health"
  if command -v jq >/dev/null 2>&1; then
    jq . /tmp/hermes-nimbus-health.json | sed 's/^/   /'
  else
    cat /tmp/hermes-nimbus-health.json | sed 's/^/   /'
    echo ""
  fi
else
  echo "   ❌ 无法访问 http://$HOST:$PORT/api/health"
fi

echo ""
echo "📱 访问地址:"
echo "   本地: http://localhost:$PORT"
LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
if [ -n "${LOCAL_IP:-}" ]; then
  echo "   局域网: http://$LOCAL_IP:$PORT"
fi
echo "   WebSocket: ws://localhost:$PORT/ws"
echo "   状态 API: http://localhost:$PORT/api/states"
