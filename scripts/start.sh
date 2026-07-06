#!/bin/bash
# Hermes Halo 启动脚本

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
STATIC_DIR="$PROJECT_DIR/static"
SCRIPTS_DIR="$PROJECT_DIR/scripts"
PID_FILE="$PROJECT_DIR/.pids"

# 默认端口
WS_PORT=${1:-8765}
HTTP_PORT=${2:-8766}

echo "🚀 启动 Hermes Halo..."
echo "   WebSocket 端口: $WS_PORT"
echo "   HTTP 端口: $HTTP_PORT"
echo ""

# 检查是否已在运行
if [ -f "$PID_FILE" ]; then
    echo "⚠️  检测到 PID 文件，可能已在运行"
    echo "   如需重启，请先运行: ./scripts/stop.sh"
    exit 1
fi

# 检查 Python 依赖
PYTHON="python3"
if ! $PYTHON -c "import aiohttp" 2>/dev/null; then
    echo "📦 安装 aiohttp..."
    pip install aiohttp
fi

# 启动 WebSocket 服务器 (后台)
echo "🔌 启动 WebSocket 服务器..."
cd "$SCRIPTS_DIR"
$PYTHON halo_server.py --port "$WS_PORT" &
WS_PID=$!
echo "   WebSocket PID: $WS_PID"

# 启动 HTTP 服务器 (后台)
echo "🌐 启动 HTTP 服务器..."
cd "$STATIC_DIR"
$PYTHON -m http.server "$HTTP_PORT" &
HTTP_PID=$!
echo "   HTTP PID: $HTTP_PID"

# 保存 PID 文件
cat > "$PID_FILE" << EOF
WS_PID=$WS_PID
HTTP_PID=$HTTP_PID
WS_PORT=$WS_PORT
HTTP_PORT=$HTTP_PORT
EOF

# 等待服务器启动
sleep 2

# 检查服务器是否启动成功
if kill -0 $WS_PID 2>/dev/null && kill -0 $HTTP_PID 2>/dev/null; then
    echo ""
    echo "✅ Hermes Halo 已启动!"
    echo ""
    echo "📱 访问地址:"
    echo "   本地: http://localhost:$HTTP_PORT"
    echo "   局域网: http://$(hostname -I | awk '{print $1}'):$HTTP_PORT"
    echo ""
    echo "🔧 WebSocket 地址:"
    echo "   ws://localhost:$WS_PORT"
    echo ""
    echo "📋 管理命令:"
    echo "   查看状态: ./scripts/status.sh"
    echo "   停止服务: ./scripts/stop.sh"
    echo ""
    echo "按 Ctrl+C 停止所有服务"

    # 捕获退出信号
    trap "echo ''; echo '🛑 停止服务...'; rm -f '$PID_FILE'; kill $WS_PID $HTTP_PID 2>/dev/null; exit 0" INT TERM

    # 等待
    wait
else
    echo "❌ 服务启动失败"
    rm -f "$PID_FILE"
    exit 1
fi
