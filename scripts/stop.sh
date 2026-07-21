#!/bin/bash
# Hermes Halo 停止脚本

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PID_FILE="$PROJECT_DIR/.pids"

echo "🛑 停止 Hermes Halo..."

stopped=0

if [ -f "$PID_FILE" ]; then
  # shellcheck disable=SC1090
  source "$PID_FILE"
  if [ -n "${PID:-}" ] && kill -0 "$PID" 2>/dev/null; then
    echo "   停止 PID $PID"
    kill "$PID" 2>/dev/null || true
    for i in $(seq 1 20); do
      if ! kill -0 "$PID" 2>/dev/null; then
        stopped=1
        break
      fi
      sleep 0.1
    done
    if kill -0 "$PID" 2>/dev/null; then
      echo "   强制停止 PID $PID"
      kill -9 "$PID" 2>/dev/null || true
    fi
    stopped=1
  fi
  rm -f "$PID_FILE"
fi

# 兜底：清理本项目的 halo_server 进程
PIDS=$(pgrep -f "$PROJECT_DIR/scripts/halo_server.py" || true)
if [ -n "$PIDS" ]; then
  echo "   清理残留进程: $PIDS"
  kill $PIDS 2>/dev/null || true
  sleep 0.5
  PIDS=$(pgrep -f "$PROJECT_DIR/scripts/halo_server.py" || true)
  if [ -n "$PIDS" ]; then
    kill -9 $PIDS 2>/dev/null || true
  fi
  stopped=1
fi

# 兼容旧双进程模型
OLD_HTTP=$(pgrep -f "http.server 8766" || true)
if [ -n "$OLD_HTTP" ]; then
  echo "   清理旧 HTTP 进程: $OLD_HTTP"
  kill $OLD_HTTP 2>/dev/null || true
  sleep 0.2
  kill -9 $OLD_HTTP 2>/dev/null || true
  stopped=1
fi

if [ "$stopped" -eq 1 ]; then
  echo "✅ Hermes Halo 已停止"
else
  echo "ℹ️  未发现运行中的 Hermes Halo 进程"
fi
