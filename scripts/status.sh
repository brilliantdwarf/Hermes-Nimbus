#!/bin/bash
# Hermes Halo 状态检查脚本

echo "📊 Hermes Halo 状态检查"
echo "========================"
echo ""

# 检查 WebSocket 服务器
WS_PID=$(pgrep -f "halo_server.py" || true)
if [ -n "$WS_PID" ]; then
    echo "✅ WebSocket 服务器: 运行中 (PID: $WS_PID)"
else
    echo "❌ WebSocket 服务器: 未运行"
fi

# 检查 HTTP 服务器
HTTP_PID=$(pgrep -f "http.server 8766" || true)
if [ -n "$HTTP_PID" ]; then
    echo "✅ HTTP 服务器: 运行中 (PID: $HTTP_PID)"
else
    echo "❌ HTTP 服务器: 未运行"
fi

echo ""

# 检查端口
echo "🔌 端口检查:"
if lsof -i :8765 >/dev/null 2>&1; then
    echo "   端口 8765 (WebSocket): 已占用"
else
    echo "   端口 8765 (WebSocket): 空闲"
fi

if lsof -i :8766 >/dev/null 2>&1; then
    echo "   端口 8766 (HTTP): 已占用"
else
    echo "   端口 8766 (HTTP): 空闲"
fi

echo ""

# 检查状态文件
STATE_FILE="$HOME/.hermes/hermes_state.txt"
if [ -f "$STATE_FILE" ]; then
    STATE=$(cat "$STATE_FILE")
    echo "📝 当前状态: $STATE"
else
    echo "📝 状态文件: 不存在 (将使用自动检测)"
fi

echo ""

# 访问地址
echo "📱 访问地址:"
echo "   本地: http://localhost:8766"
echo "   局域网: http://$(hostname -I | awk '{print $1}'):8766"
echo ""

# PID 文件
PID_FILE="$HOME/hermes-halo/.pids"
if [ -f "$PID_FILE" ]; then
    echo "📄 PID 文件内容:"
    cat "$PID_FILE"
fi
