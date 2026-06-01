#!/bin/bash
# ============================================
# Vizo 构建脚本
# 用法: ./build.sh [版本号]
# 示例: ./build.sh 1.0.0
# ============================================
set -e

VERSION="${1:-dev}"
IMAGE_NAME="vizo"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="/tmp/vizo-build-$$"
DIST_DIR="/tmp/vizo-dist-$$"
OUTPUT_DIR="${2:-/tmp}"

echo "=== Vizo 构建 v${VERSION} ==="
echo ""

# Step 1: 创建干净的构建目录
echo "[1/5] 创建构建目录..."
rm -rf "$BUILD_DIR" "$DIST_DIR"
mkdir -p "$BUILD_DIR" "$DIST_DIR/vizo"

# 复制源码（排除开发/个人文件）
rsync -a \
    --exclude='.git' \
    --exclude='.vizo/' \
    --exclude='.opus/' \
    --exclude='.claude/' \
    --exclude='.claude-agent/' \
    --exclude='.serena/' \
    --exclude='.mcp.json' \
    --exclude='logs/' \
    --exclude='tests/' \
    --exclude='.test-opus/' \
    --exclude='*.pyc' \
    --exclude='__pycache__' \
    --exclude='.env' \
    --exclude='.env.*' \
    --exclude='config.json' \
    --exclude='lib/config_v3.json' \
    --exclude='docs/' \
    --exclude='*.md' \
    --exclude='build.sh' \
    --exclude='*Zone.Identifier' \
    "$SRC_DIR/" "$BUILD_DIR/"

# 保留必要的 .md 文件（角色模板）
rsync -a "$SRC_DIR/role_templates/" "$BUILD_DIR/role_templates/" --include='*.md' --exclude='*'

# 复制 agents 目录（内置模块的角色模板）
if [ -d "$SRC_DIR/agents" ]; then
    rsync -a "$SRC_DIR/agents/" "$BUILD_DIR/agents/" --include='*.md' --include='*.json' --include='*/' --exclude='*'
fi

# 生成 .claude/settings.json（工具权限预配置，用户无需手动审批）
mkdir -p "$BUILD_DIR/.claude"
cat > "$BUILD_DIR/.claude/settings.json" << 'CLAUDE_SETTINGS_EOF'
{
  "permissions": {
    "allow": [
      "Bash(*)",
      "Read(*)",
      "Write(*)",
      "Edit(*)",
      "Glob(*)",
      "Grep(*)",
      "WebFetch(*)",
      "WebSearch(*)",
      "NotebookEdit(*)",
      "mcp__serena__*"
    ]
  },
  "enableAllProjectMcpServers": true
}
CLAUDE_SETTINGS_EOF

# 生成 .mcp.json（MCP 服务器预配置）
cat > "$BUILD_DIR/.mcp.json" << 'MCP_EOF'
{
  "mcpServers": {
    "serena": {
      "command": "serena",
      "args": ["start-mcp-server", "--project-from-cwd"]
    },
    "jina": {
      "command": "npx",
      "args": ["-y", "jina-mcp-tools"]
    },
    "playwright": {
      "command": "npx",
      "args": [
        "-y",
        "@playwright/mcp@latest",
        "--headless",
        "--browser",
        "chromium",
        "--no-sandbox",
        "--isolated"
      ]
    }
  }
}
MCP_EOF
cp "$BUILD_DIR/.mcp.json" "$DIST_DIR/vizo/.mcp.json"

# 复制配置模板
cp "$SRC_DIR/.env.example" "$BUILD_DIR/.env.example"
if [ -f "$SRC_DIR/config.json.example" ]; then
    cp "$SRC_DIR/config.json.example" "$BUILD_DIR/config.json.example"
fi

# Step 2: 修补 subprocess 中的 .py 引用
echo "[2/5] 修补文件引用..."

# secretary.py: confirm_server.py → confirm_server.pyc
sed -i 's|"confirm_server\.py"|"confirm_server.pyc"|g' "$BUILD_DIR/secretary.py"

# lib/confirm_server.py: opus.py → opus.pyc（subprocess 调用）
sed -i 's|/ "opus\.py"|/ "opus.pyc"|g' "$BUILD_DIR/lib/confirm_server.py"

# hooks/task_selection_handler.py: opus.py → opus.pyc
sed -i 's|"opus\.py"|"opus.pyc"|g' "$BUILD_DIR/hooks/task_selection_handler.py"

# orchestrator.py: 进程检测兼容 .pyc
sed -i 's|"opus\.py" in cmdline|"opus" in cmdline|g' "$BUILD_DIR/orchestrator.py"

# orchestrator.py: SERVICE_FILES 常量（改为 .pyc 后缀）
sed -i 's|"lib/confirm_server\.py"|"lib/confirm_server.pyc"|g' "$BUILD_DIR/orchestrator.py"
sed -i 's|"lib/web_console\.py"|"lib/web_console.pyc"|g' "$BUILD_DIR/orchestrator.py"
sed -i 's|"lib/mobile_console\.py"|"lib/mobile_console.pyc"|g' "$BUILD_DIR/orchestrator.py"
sed -i 's|"lib/pty_manager\.py"|"lib/pty_manager.pyc"|g' "$BUILD_DIR/orchestrator.py"
sed -i 's|"lib/preview_server\.py"|"lib/preview_server.pyc"|g' "$BUILD_DIR/orchestrator.py"

# Step 3: 写入版本号
echo "[3/5] 写入版本 ${VERSION}..."
echo "$VERSION" > "$BUILD_DIR/VERSION"

# 移除 .dockerignore 中对 .pyc 的排除（容器内编译后需要保留 .pyc）
sed -i '/\*\.pyc/d' "$BUILD_DIR/.dockerignore"
sed -i '/\*\.pyo/d' "$BUILD_DIR/.dockerignore"

# Step 4: 构建 Docker 镜像（编译在容器内完成，避免 Python 版本不匹配）
echo "[4/5] 构建 Docker 镜像..."
cp "$SRC_DIR/Dockerfile.release" "$BUILD_DIR/Dockerfile"

docker build --network=host \
    -t "${IMAGE_NAME}:${VERSION}" \
    -t "${IMAGE_NAME}:latest" \
    "$BUILD_DIR"

# 清理构建目录
rm -rf "$BUILD_DIR"

# Step 5: 打包发行包（镜像 + 配置 + 启动脚本）
echo "[5/5] 打包发行包..."

# 导出镜像
echo "  导出镜像（约 1-2 分钟）..."
docker save \
    "${IMAGE_NAME}:${VERSION}" \
    "${IMAGE_NAME}:latest" \
    | gzip > "$DIST_DIR/vizo/image.tar.gz"

# 生成 docker-compose.yml
cat > "$DIST_DIR/vizo/docker-compose.yml" << 'COMPOSE_EOF'
version: "3.8"

services:
  vizo:
    image: vizo:latest
    ports:
      - "9390:9390"
    environment:
      - HOME=/home/vizo
      - PATH=/opt/vizo-host/npm-global/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
      - VIZO_HOST_NPM_MOUNT=/opt/vizo-host/npm-global
      - VIZO_RUNTIME_NPM_PREFIX=/usr/local
      - REDIS_HOST=redis
      - REDIS_PORT=6379
    volumes:
      - ${VIZO_HOST_NPM_PREFIX:-./docker/empty-npm-prefix}:/opt/vizo-host/npm-global:ro
      - vizo_data:/app/.vizo
      - vizo_projects:/app/projects
      - ./config.json:/app/config.json
      - ./.mcp.json:/app/.mcp.json
    depends_on:
      - redis
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    volumes:
      - redis_data:/data
    restart: unless-stopped

volumes:
  vizo_data: {}
  vizo_projects: {}
  redis_data: {}
COMPOSE_EOF

# 生成默认 config.json
cat > "$DIST_DIR/vizo/config.json" << 'CONFIG_EOF'
{
  "_comment": "Vizo 配置文件 — 请修改 token 和 api_key 后再启动",
  "version": "4.0",
  "projects": {
    "workspace": {
      "path": "/app/projects/workspace",
      "type": "dev",
      "description": "默认项目"
    }
  },
  "web_console": {
    "enabled": true,
    "token": "请修改为你的登录密码",
    "max_sessions": 3,
    "idle_timeout_suspend": 1800,
    "idle_timeout_terminate": 7200,
    "cookie_max_age_days": 30
  },
  "redis": {
    "host": "redis",
    "port": 6379,
    "db": 0,
    "password": ""
  },
  "confirm_server": {
    "enabled": true,
    "host": "0.0.0.0",
    "port": 9390
  },
  "wecom": {
    "enabled": false
  },
  "external_models": {
    "anthropic": {
      "api_key": "请修改为你的 Anthropic API Key（sk-ant-开头）",
      "base_url": "https://api.anthropic.com"
    }
  },
  "budget": {
    "daily_limit_usd": 50,
    "warn_threshold": 0.8,
    "stop_threshold": 0.95
  },
  "max_concurrent_agents": 3
}
CONFIG_EOF

# 生成一键启动脚本（macOS / Linux）
cat > "$DIST_DIR/vizo/start.sh" << 'START_EOF'
#!/bin/bash
# ============================================
# Vizo 一键启动脚本
# ============================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Vizo 启动 ==="
echo ""

# 检查 Docker
if ! command -v docker &>/dev/null; then
    echo "错误：未安装 Docker Desktop，请先安装。"
    echo "下载地址：https://www.docker.com/products/docker-desktop/"
    exit 1
fi

if ! docker info &>/dev/null 2>&1; then
    echo "错误：Docker Desktop 未运行，请先启动 Docker Desktop。"
    exit 1
fi

HOST_NPM_PREFIX=""
if command -v npm &>/dev/null; then
    HOST_NPM_PREFIX="$(npm prefix -g 2>/dev/null || true)"
fi

if [ -n "$HOST_NPM_PREFIX" ] && { [ -x "$HOST_NPM_PREFIX/bin/claude" ] || [ -x "$HOST_NPM_PREFIX/bin/codex" ]; }; then
    export VIZO_HOST_NPM_PREFIX="$HOST_NPM_PREFIX"
    echo "检测到宿主机 CLI 安装，优先复用：$VIZO_HOST_NPM_PREFIX"
else
    unset VIZO_HOST_NPM_PREFIX
    echo "未检测到可复用的宿主机 CLI，回退到容器内自带 CLI。"
fi

# 检查配置
if grep -q "请修改" config.json 2>/dev/null; then
    echo "⚠️  首次使用，请先编辑 config.json："
    echo ""
    echo "  1. 将 \"token\" 改为你的登录密码"
    echo "  2. 将 \"api_key\" 改为你的 Anthropic API Key"
    echo ""
    echo "  编辑命令：nano $SCRIPT_DIR/config.json"
    echo "  或用任意文本编辑器打开 config.json"
    echo ""
    read -p "已修改完成？按回车继续，Ctrl+C 取消..."
fi

# 加载镜像（首次）
if ! docker images | grep -q "vizo"; then
    echo "[1/2] 首次运行，加载镜像（约 1-2 分钟）..."
    docker load < "$SCRIPT_DIR/image.tar.gz"
    # docker load 可能不带 latest 标签，手动查找并打标签
    LOADED_TAG=$(docker images vizo --format '{{.Tag}}' | grep -v latest | head -1)
    if [ -n "$LOADED_TAG" ] && [ "$LOADED_TAG" != "latest" ]; then
        docker tag "vizo:$LOADED_TAG" vizo:latest
    fi
    echo "  镜像加载完成。"
else
    echo "[1/2] 镜像已存在，跳过加载。"
fi

# 启动
echo "[2/2] 启动服务..."
docker compose up -d

echo ""
echo "============================================"
echo "  Vizo 启动成功！"
echo ""
echo "  打开浏览器访问："
echo "  http://localhost:9390/vizo/console"
echo "============================================"
START_EOF
chmod +x "$DIST_DIR/vizo/start.sh"

# 生成一键启动脚本（Windows）
cat > "$DIST_DIR/vizo/start.bat" << 'BAT_EOF'
@echo off
chcp 65001 >nul
title Vizo

echo === Vizo 启动 ===
echo.

REM 检查 Docker
docker info >nul 2>&1
if errorlevel 1 (
    echo 错误：Docker Desktop 未运行，请先启动 Docker Desktop。
    pause
    exit /b 1
)

set "VIZO_HOST_NPM_PREFIX="
for /f "usebackq delims=" %%i in (`npm prefix -g 2^>nul`) do set "HOST_NPM_PREFIX=%%i"
if defined HOST_NPM_PREFIX (
    if exist "%HOST_NPM_PREFIX%\bin\claude" set "VIZO_HOST_NPM_PREFIX=%HOST_NPM_PREFIX%"
    if exist "%HOST_NPM_PREFIX%\bin\codex" set "VIZO_HOST_NPM_PREFIX=%HOST_NPM_PREFIX%"
)
if defined VIZO_HOST_NPM_PREFIX (
    echo 检测到宿主机 CLI 安装，优先复用：%VIZO_HOST_NPM_PREFIX%
) else (
    echo 未检测到可复用的宿主机 CLI，回退到容器内自带 CLI。
)

REM 加载镜像（首次）
docker images | findstr "vizo" >nul 2>&1
if errorlevel 1 (
    echo [1/2] 首次运行，加载镜像（约 1-2 分钟）...
    docker load -i "%~dp0image.tar.gz"
    REM 查找并打 latest 标签
    for /f "tokens=2" %%i in ('docker images vizo --format "{{.Tag}}" ^| findstr /v latest') do (
        docker tag vizo:%%i vizo:latest
    )
    echo   镜像加载完成。
) else (
    echo [1/2] 镜像已存在，跳过加载。
)

REM 启动
echo [2/2] 启动服务...
cd /d "%~dp0"
docker compose up -d

echo.
echo ============================================
echo   Vizo 启动成功！
echo.
echo   打开浏览器访问：
echo   http://localhost:9390/vizo/console
echo ============================================
echo.
pause
BAT_EOF

# 生成停止脚本
cat > "$DIST_DIR/vizo/stop.sh" << 'STOP_EOF'
#!/bin/bash
cd "$(dirname "$0")"
docker compose down
echo "Vizo 已停止。"
STOP_EOF
chmod +x "$DIST_DIR/vizo/stop.sh"

cat > "$DIST_DIR/vizo/stop.bat" << 'STOPBAT_EOF'
@echo off
chcp 65001 >nul
cd /d "%~dp0"
docker compose down
echo Vizo 已停止。
pause
STOPBAT_EOF

# 打包为 zip
cd "$DIST_DIR"
tar czf "$OUTPUT_DIR/vizo-${VERSION}.tar.gz" vizo/

# 清理
rm -rf "$DIST_DIR"

echo ""
echo "============================================"
echo "  构建完成！"
echo ""
echo "  发行包: $OUTPUT_DIR/vizo-${VERSION}.tar.gz"
echo ""
echo "  包内容:"
echo "    vizo/"
echo "    ├── image.tar.gz        # Docker 镜像"
echo "    ├── docker-compose.yml  # 编排配置"
echo "    ├── config.json         # 系统配置（用户需修改）"
echo "    ├── start.sh            # macOS/Linux 一键启动"
echo "    ├── start.bat           # Windows 一键启动"
echo "    ├── stop.sh             # macOS/Linux 停止"
echo "    └── stop.bat            # Windows 停止"
echo ""
echo "  用户使用方式:"
echo "    1. 解压到任意目录"
echo "    2. 编辑 config.json（设置密码和 API Key）"
echo "    3. 双击 start.sh 或 start.bat"
echo "    4. 浏览器打开 http://localhost:9390/vizo/console"
echo "============================================"
