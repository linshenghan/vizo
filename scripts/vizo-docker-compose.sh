#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

HOST_NPM_PREFIX=""
if command -v npm >/dev/null 2>&1; then
  HOST_NPM_PREFIX="$(npm prefix -g 2>/dev/null || true)"
fi

if [[ -n "${HOST_NPM_PREFIX}" ]] && [[ -x "${HOST_NPM_PREFIX}/bin/claude" || -x "${HOST_NPM_PREFIX}/bin/codex" ]]; then
  export VIZO_HOST_NPM_PREFIX="${HOST_NPM_PREFIX}"
  echo "检测到宿主机 CLI 安装，优先复用：${VIZO_HOST_NPM_PREFIX}"
else
  unset VIZO_HOST_NPM_PREFIX || true
  echo "未检测到可复用的宿主机 CLI，回退到容器内自带 CLI。"
fi

if [[ $# -eq 0 ]]; then
  set -- up -d
fi

exec docker compose -f "${ROOT_DIR}/docker-compose.vizo.yml" "$@"
