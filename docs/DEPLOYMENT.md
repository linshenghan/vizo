# Vizo Deployment Guide

This guide covers the practical ways to run Vizo from the public repository.
Keep the GitHub README focused on what the project is and how to start quickly;
deployment details belong in this file under `docs/`.

## Deployment Options

| Option | Use when | Notes |
| --- | --- | --- |
| Docker Compose | You want the fastest complete setup. | Recommended for evaluation and single-host deployment. Starts Vizo and Redis. |
| Local Python process | You are developing Vizo or want direct control over installed runtimes. | Requires Python dependencies, Redis, and AI CLI runtimes on the host. |
| Reverse proxy | You need HTTPS, a domain name, or remote access. | Put Nginx/Caddy/Traefik in front of the Vizo HTTP service. |
| systemd or another supervisor | You are running without Docker in production. | Keep `secretary.py` alive; it supervises the Web service. |

## Prerequisites

Required for all deployments:

- A Linux host or compatible container environment.
- Git.
- A supported Anthropic-compatible API key, usually through `ANTHROPIC_API_KEY`
  or `OPUS_MAIN_API_KEY`.
- A strong Web Console password or token.
- Redis reachable by the Vizo process.

Required only for local-process deployments:

- Python 3.11 or newer.
- Node.js 22.x or a compatible version for installed AI CLI tools.
- The AI CLI runtime you plan to use, available on `PATH`.
- Optional MCP tools, such as Serena and Playwright MCP, if you want those
  integrations outside Docker.

The Docker image installs Python dependencies, Node.js 22.x, Claude Code, Codex,
Playwright MCP, and Chromium inside the container.

## Configuration Model

Vizo reads configuration in this order:

1. `config.json`
2. `.env`
3. selected environment variables

Create local config files from the examples:

```bash
cp config.json.example config.json
cp .env.example .env
```

Do not commit the generated `config.json` or `.env`.

Common environment overrides:

| Variable | Maps to |
| --- | --- |
| `ANTHROPIC_API_KEY` | `external_models.anthropic.api_key` |
| `OPUS_MAIN_API_KEY` | `external_models.anthropic.api_key` |
| `ANTHROPIC_BASE_URL` | `external_models.anthropic.base_url` |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | Anthropic Opus model override |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | Anthropic Sonnet model override |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | Anthropic Haiku model override |
| `DEEPSEEK_API_KEY` | `external_models.deepseek.api_key` |
| `GLM_API_KEY` | `external_models.glm.api_key` |
| `QWEN_API_KEY` | `external_models.qwen.api_key` |
| `WEB_CONSOLE_TOKEN` | `web_console.token` |
| `REDIS_HOST` | `redis.host` |
| `REDIS_PORT` | `redis.port` |
| `CONFIRM_SERVER_HOST` | `confirm_server.host` |
| `CONFIRM_SERVER_PORT` | `confirm_server.port` |
| `CONFIRM_SERVER_USE_TUNNEL` | `confirm_server.use_cloudflare_tunnel` |
| `PUBLIC_BASE_URL` | public preview-link base URL |

Implementation detail: Anthropic-prefixed variables are intentionally read from
`.env` for the main configuration path so stale shell variables from another AI
session do not override the selected connection unexpectedly.

## Docker Compose Deployment

### 1. Prepare The Repository

```bash
git clone <your-vizo-repo-url>
cd vizo-public

cp config.json.example config.json
cp .env.example .env
```

### 2. Set Secrets

Edit `.env`:

```bash
ANTHROPIC_API_KEY=sk-ant-...
VIZO_WEB_CONSOLE_TOKEN=replace-with-a-strong-password
```

Optional but recommended when generating links:

```bash
VIZO_PUBLIC_BASE_URL=http://127.0.0.1:9390
```

Use your real HTTPS URL instead of the local URL when deploying behind a reverse
proxy.

The public `.env.example` also shows `WEB_CONSOLE_TOKEN`. That variable is used
by local Python runs. The default Docker Compose file reads
`VIZO_WEB_CONSOLE_TOKEN` and injects it into the container as
`WEB_CONSOLE_TOKEN`.

### 3. Align The Web Port

The default `docker-compose.yml` publishes host port `9390` to container port
`9390`. Make sure `config.json` also uses `9390`:

```json
{
  "confirm_server": {
    "host": "0.0.0.0",
    "port": 9390
  }
}
```

The Compose file already injects Redis settings for the container:

```yaml
REDIS_HOST: vizo-redis
REDIS_PORT: "6379"
```

### 4. Start The Stack

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f vizo
```

Open the console:

```text
http://127.0.0.1:9390/vizo/console
```

Check health:

```bash
curl -fsS http://127.0.0.1:9390/vizo/health
```

### 5. Stop Or Upgrade

Stop:

```bash
docker compose down
```

Upgrade after pulling new code:

```bash
git pull
docker compose up -d --build
```

The Compose file uses named volumes for `.vizo`, logs, the container home
directory, and Redis data. Removing volumes deletes persisted runtime state:

```bash
docker compose down -v
```

## Local Python Deployment

### 1. Install Python Dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Install Runtime Tools

Install the AI CLI runtime or runtimes you plan to use. For example:

```bash
npm install -g @anthropic-ai/claude-code @openai/codex
```

Install optional MCP tools as needed. At minimum, `.mcp.json.example` shows the
Serena MCP shape expected by the public repo:

```json
{
  "mcpServers": {
    "serena": {
      "command": "serena",
      "args": ["start-mcp-server", "--project-from-cwd"]
    }
  }
}
```

Copy it only when you actually want to use it locally:

```bash
cp .mcp.json.example .mcp.json
```

### 3. Start Redis

Use a system Redis service, or run one with Docker:

```bash
docker run -d --name vizo-redis -p 6379:6379 redis:7-alpine
```

Set Redis in `.env` if your `config.json` uses a different default:

```bash
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
```

### 4. Configure Vizo

```bash
cp config.json.example config.json
cp .env.example .env
```

Edit `.env`:

```bash
ANTHROPIC_API_KEY=sk-ant-...
WEB_CONSOLE_TOKEN=replace-with-a-strong-password
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
```

Edit `config.json`:

```json
{
  "confirm_server": {
    "host": "0.0.0.0",
    "port": 9390
  }
}
```

### 5. Start Vizo

For a foreground service:

```bash
python3 secretary.py
```

For confirm_server only:

```bash
python3 lib/confirm_server.py start -f -p 9390
```

Use the CLI in another shell:

```bash
./vizo --chat "summarize this repository"
./vizo "fix the failing login test"
```

## systemd User Service

Use a supervisor if you run Vizo directly on a host. This example uses a systemd
user service:

```ini
[Unit]
Description=Vizo secretary
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/vizo-public
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/vizo-public/.venv/bin/python /opt/vizo-public/secretary.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

Install it:

```bash
mkdir -p ~/.config/systemd/user
cp vizo-secretary.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now vizo-secretary
systemctl --user status vizo-secretary
```

Replace `/opt/vizo-public` with your actual checkout path.

## Reverse Proxy And HTTPS

Do not expose the Web Console over an untrusted network without TLS and a strong
password.

Example Nginx server block:

```nginx
server {
    listen 443 ssl http2;
    server_name vizo.example.com;

    ssl_certificate /etc/letsencrypt/live/vizo.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/vizo.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:9390;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
    }
}
```

Then set the public URL:

```bash
PUBLIC_BASE_URL=https://vizo.example.com
```

Or set `custom_domain` in `config.json` to `vizo.example.com`.

## Important Routes

| URL | Purpose |
| --- | --- |
| `/vizo/console` | Desktop Web Console. |
| `/vizo/console/setup` | First-run setup wizard. |
| `/vizo/m` | Mobile Console. |
| `/vizo/tasks` | Task list. |
| `/vizo/confirm/{request_id}` | Human approval page. |
| `/vizo/input/{request_id}` | Human feedback page. |
| `/vizo/preview/{preview_id}` | Output preview. |
| `/vizo/docs/` | Generated document listing. |
| `/vizo/chrome/connect` | Chrome bridge connection page. |
| `/vizo/health` | Health check. |

## Health Checks

Container status:

```bash
docker compose ps
docker compose logs --tail=100 vizo
docker compose logs --tail=100 vizo-redis
```

HTTP health:

```bash
curl -fsS http://127.0.0.1:9390/vizo/health
```

Confirm server process status in local mode:

```bash
python3 lib/confirm_server.py status
```

Redis:

```bash
redis-cli -h 127.0.0.1 -p 6379 ping
```

Run a smoke command:

```bash
./vizo --chat "say ok"
```

## Logs And State

Common runtime paths:

| Path | Content |
| --- | --- |
| `logs/` | Service logs. |
| `.vizo/` | Vizo runtime state for newer components. |
| `.opus/` | Historical task/session state path used by compatibility code. |
| `.mcp.json` | Local MCP server configuration. |
| `config.json` | Local runtime configuration. |
| `.env` | Local secret and environment overrides. |

Docker Compose persists runtime data in named volumes:

- `vizo_data`
- `vizo_logs`
- `vizo_home`
- `vizo_redis_data`

## Security Checklist

- Set a strong `WEB_CONSOLE_TOKEN` or initialize a strong password through the
  setup wizard.
- Keep `config.json`, `.env`, `.mcp.json`, logs, task state, and local memories
  out of Git.
- If you build a Docker image on a machine that already has `config.json`, avoid
  publishing that image to a public registry unless you have verified no secrets
  were copied into the image layers.
- Use TLS and a reverse proxy before exposing Vizo outside localhost.
- Prefer private networking or VPN access for the Web Console.
- Rotate API keys if they were ever written into logs, images, screenshots, or
  public issue threads.

## Troubleshooting

### Browser Cannot Open The Console

Check the port first:

```bash
docker compose ps
curl -v http://127.0.0.1:9390/vizo/health
```

If Docker is running but the port is closed, confirm that `config.json` uses
`confirm_server.port = 9390` for the default Compose file.

### Redis Connection Fails

For Docker Compose:

```bash
docker compose logs --tail=100 vizo-redis
docker compose exec vizo-redis redis-cli ping
```

For local mode:

```bash
redis-cli -h 127.0.0.1 -p 6379 ping
```

Then verify `REDIS_HOST` and `REDIS_PORT` in `.env`.

### Setup Wizard Keeps Reappearing

The setup wizard appears when Vizo cannot find a usable Web Console password or
API key. Check:

```bash
grep -E 'WEB_CONSOLE_TOKEN|VIZO_WEB_CONSOLE_TOKEN|ANTHROPIC_API_KEY|OPUS_MAIN_API_KEY' .env
```

Also confirm that persistent volumes were not removed with `docker compose down
-v`.

### AI Runtime Is Not Found

Inside Docker:

```bash
docker compose exec vizo which claude
docker compose exec vizo which codex
```

Local mode:

```bash
which claude
which codex
```

Install the runtime you plan to use and make sure the service process can see it
on `PATH`.

### Generated Links Use The Wrong Host

Set:

```bash
PUBLIC_BASE_URL=https://your-real-host.example.com
```

In Docker Compose, set `VIZO_PUBLIC_BASE_URL` in `.env`; the Compose file passes
it to the container as `PUBLIC_BASE_URL`.

### Public Sync Removes The Deployment Guide

Public sync is whitelist based. Keep this file listed in
`PUBLIC_SYNC_MANIFEST.txt`:

```text
docs/DEPLOYMENT.md
```
