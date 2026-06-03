FROM python:3.11-slim

LABEL org.opencontainers.image.title="Vizo" \
      org.opencontainers.image.description="Vizo single-user Docker runtime"

# 系统依赖：git（worktree）、curl（健康检查）、gnupg+ca-certificates（NodeSource 签名验证）
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# 安装 Node.js 22.x LTS（Claude Code CLI / Codex CLI 运行时依赖）
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# 安装 Claude Code CLI / Codex CLI / Playwright MCP，并预装 Chromium 运行时
RUN npm install -g @anthropic-ai/claude-code @openai/codex @playwright/mcp \
    && node "$(npm root -g)/@playwright/mcp/node_modules/playwright/cli.js" install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

ENV VIZO_RUNTIME_NPM_PREFIX=/usr/local
ENV HOME=/home/vizo
ENV OPUS_BRAND_EN=Vizo
ENV OPUS_BRAND_ZH=维造

# 工作目录
WORKDIR /app

# 先复制依赖文件，利用 Docker 层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码（.dockerignore 排除敏感和非必要文件）
COPY . .

# 预置系统集成 MCP，避免新部署用户手动安装
RUN cat > /app/.mcp.json <<'MCP_EOF'
{
  "mcpServers": {
    "serena": {
      "command": "serena",
      "args": ["start-mcp-server", "--project-from-cwd"]
    },
    "mcp-chrome": {
      "command": "python3",
      "args": ["lib/chrome_mcp_stdio.py"]
    },
    "vizo-router": {
      "command": "python3",
      "args": ["lib/vizo_router_mcp_stdio.py"]
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

# 初始化 Git 仓库（state_manager.py 的 worktree 和回滚快照功能需要）
RUN git config --global user.email "vizo@container" \
    && git config --global user.name "Vizo" \
    && git init \
    && git add -A \
    && git commit -m "Docker initial commit" --allow-empty

# 确保运行时目录存在（named volume 挂载前需要目标路径）
RUN mkdir -p /app/.vizo /app/logs /home/vizo

# 暴露 confirm_server 端口
EXPOSE 9390

# 启动入口：secretary 模块管理 confirm_server 生命周期
CMD ["python3", "-m", "vizo_core.secretary"]
