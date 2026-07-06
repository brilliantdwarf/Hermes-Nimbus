#!/bin/bash
# Hermes Halo 停止脚本

echo "🛑 停止 Hermes Halo..."

# 查找并停止 WebSocket 服务器
WS_PIDS=$(pgrep -f "halo_server.py" || true)
if [ -n "$WS_PIDS" ]; then
    echo "   停止 WebSocket 服务器 (PIDs: $WS_PIDS)"
    kill $WS_PIDS 2>/dev/null || true
fi

# 查找并停止 HTTP 服务器
HTTP_PIDS=$(pgrep -f "http.server 8766" || true)
if [ -n "$HTTP_PIDS" ]; then
    echo "   停止 HTTP 服务器 (PIDs: $HTTP_PIDS)"
    kill $HTTP_PIDS 2>/dev/null || true
fi

# 等待进程停止
sleep 1

# 强制杀死残留进程
pkill -9 -f "halo_server.py" 2>/dev/null || true
pkill -9 -f "http.server 8766" 2>/dev/null || true

echo "✅ Hermes Halo 已停止"
