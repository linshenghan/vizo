from __future__ import annotations

import os
import shutil
from pathlib import Path


CLI_SOURCE_HOST_REUSE = "host_reuse"
CLI_SOURCE_CONTAINER_BUNDLE = "container_bundle"
CLI_SOURCE_SYSTEM_PATH = "system_path"
CLI_SOURCE_MISSING = "missing"

DEFAULT_HOST_NPM_MOUNT = "/opt/vizo-host/npm-global"
DEFAULT_RUNTIME_NPM_PREFIX = "/usr/local"


def resolve_cli_binary(name: str) -> str | None:
    return shutil.which(name)


def cli_available(name: str) -> bool:
    return resolve_cli_binary(name) is not None


def describe_cli(name: str) -> dict[str, str | bool]:
    path = resolve_cli_binary(name)
    if not path:
        return {
            "available": False,
            "path": "",
            "resolved_path": "",
            "source": CLI_SOURCE_MISSING,
        }

    resolved_path = str(Path(path).resolve())
    return {
        "available": True,
        "path": path,
        "resolved_path": resolved_path,
        "source": _classify_cli_source(Path(resolved_path)),
    }


def _classify_cli_source(path: Path) -> str:
    host_mount = _read_env_path("VIZO_HOST_NPM_MOUNT", DEFAULT_HOST_NPM_MOUNT)
    if host_mount and _is_relative_to(path, host_mount):
        return CLI_SOURCE_HOST_REUSE

    runtime_prefix = _read_env_path("VIZO_RUNTIME_NPM_PREFIX", DEFAULT_RUNTIME_NPM_PREFIX)
    if runtime_prefix and _is_relative_to(path, runtime_prefix):
        return CLI_SOURCE_CONTAINER_BUNDLE

    return CLI_SOURCE_SYSTEM_PATH


def _read_env_path(env_name: str, default: str) -> Path | None:
    raw = str(os.getenv(env_name) or default or "").strip()
    if not raw:
        return None
    return Path(raw).resolve()


def _is_relative_to(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)
