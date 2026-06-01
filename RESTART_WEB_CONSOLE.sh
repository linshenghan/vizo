#!/bin/bash
# 快速重启浏览器终端服务

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 使用方法：bash "$SCRIPT_DIR/RESTART_WEB_CONSOLE.sh"

# 检查是否已在运行
EXISTING_PID=$(pgrep -f "confirm_server")
if [ -n "$EXISTING_PID" ]; then
    echo "浏览器终端服务已在运行 (PID: $EXISTING_PID)"
    echo "是否要重启？"
    read -p "按 Enter 确认重启，或 Ctrl+C 取消: "
    if [ $? -ne 0 ]; then
        echo "已取消"
        exit 0
    fi
fi

# 停止现有服务
pkill -f "confirm_server" 2>/dev/null
sleep 2

# 启动服务
nohup python3.11 "$SCRIPT_DIR/lib/confirm_server.py" start -f > /tmp/confirm_server.log 2>&1 &

NEW_PID=$!
echo "浏览器终端服务已重启 (新 PID: $NEW_PID)"
echo ""
echo "=== 快捷命令 ==="
echo "查看日志:   tail -f /tmp/confirm_server.log"
echo "停止服务:   pkill -f confirm_server"
echo "再次重启:   bash $SCRIPT_DIR/RESTART_WEB_CONSOLE.sh"
