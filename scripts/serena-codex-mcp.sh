#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${ROOT_DIR}/.serena-venv312"
MCP_BRIDGE_BIN="${VENV_DIR}/bin/python"
MCP_BRIDGE_SCRIPT="${ROOT_DIR}/scripts/serena-codex-memory-mcp.py"
SERENA_HOME_DIR="${ROOT_DIR}/.serena-codex-home"
LOG_DIR="${ROOT_DIR}/.serena/logs"
LOG_FILE="${LOG_DIR}/codex-mcp.stderr.log"

if [[ ! -x "${MCP_BRIDGE_BIN}" ]]; then
  echo "Codex Serena venv python is not available at ${MCP_BRIDGE_BIN}" >&2
  exit 1
fi

if [[ ! -f "${MCP_BRIDGE_SCRIPT}" ]]; then
  echo "Codex Serena memory bridge is missing at ${MCP_BRIDGE_SCRIPT}" >&2
  exit 1
fi

mkdir -p "${LOG_DIR}"
mkdir -p "${SERENA_HOME_DIR}"
export SERENA_HOME="${SERENA_HOME_DIR}"

exec "${MCP_BRIDGE_BIN}" "${MCP_BRIDGE_SCRIPT}" 2>>"${LOG_FILE}"
