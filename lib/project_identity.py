"""
Shared project identity helpers for Vizo mainline.

These helpers centralize:
- mainline project canonical naming (`vizo`)
- legacy mainline identity normalization (`opus-v6`)
- cwd -> project detection
- legacy mainline path normalization
"""

from __future__ import annotations

import json
import os
import posixpath
import re
from pathlib import Path

from lib.paths import CONFIG_FILE, VIZO_HOME


MAINLINE_PROJECT = "vizo"
MAINLINE_ALIASES = {
    "vizo",
    "vizo-next",
    "vizo_next",
}
LEGACY_MAINLINE_IDENTIFIERS = {
    "opus-v6",
    "opus_v6",
}
XIAOZHI_ALIASES = {
    "xiaozhi",
    "xiaozhi-server",
    "xiaozhi_server",
    "xiaozhi-esp32-server",
}
LEGACY_MAINLINE_ROOT = "/opt/opus-v6"
CONTAINER_MAINLINE_ROOT = "/app"
LEGACY_MAINLINE_PROJECTS_PREFIX = f"{LEGACY_MAINLINE_ROOT}/projects/"
_POSIX_RUNTIME_ROOT_PREFIXES = ("/opt", "/home", "/tmp", "/app")
_WSL_UNC_PATH_RE = re.compile(r"^\\\\wsl(?:\.localhost)?\\[^\\]+\\(.+)$", re.IGNORECASE)
_WINDOWSIZED_UNIX_PATH_RE = re.compile(
    r"([A-Za-z]:[\\/](?:opt|home|tmp)(?:[\\/].*)?)$",
    re.IGNORECASE,
)


def normalize_runtime_path(path: str | None) -> str:
    value = str(path or "").strip()
    if not value:
        return ""
    unc_match = _WSL_UNC_PATH_RE.match(value)
    if unc_match:
        return posixpath.normpath("/" + unc_match.group(1).replace("\\", "/"))
    drive_match = _WINDOWSIZED_UNIX_PATH_RE.search(value)
    if drive_match:
        suffix = drive_match.group(1)[2:].replace("\\", "/")
        return posixpath.normpath("/" + suffix.lstrip("/"))
    normalized_slashes = value.replace("\\", "/")
    if any(normalized_slashes == prefix or normalized_slashes.startswith(prefix + "/") for prefix in _POSIX_RUNTIME_ROOT_PREFIXES):
        return posixpath.normpath(normalized_slashes)
    return os.path.abspath(os.path.expanduser(value))


def _normalize_path_value(path: str | None) -> str:
    return normalize_runtime_path(path)


def is_mainline_root_path(path: str | None) -> bool:
    normalized = _normalize_path_value(path)
    return normalized in {
        normalize_runtime_path(str(VIZO_HOME.resolve())),
        normalize_runtime_path(LEGACY_MAINLINE_ROOT),
        normalize_runtime_path(CONTAINER_MAINLINE_ROOT),
    }


def canonicalize_project_name(name: str | None) -> str:
    value = str(name or "").strip()
    if not value:
        return ""
    lower = value.lower()
    if lower in MAINLINE_ALIASES or lower in LEGACY_MAINLINE_IDENTIFIERS:
        return MAINLINE_PROJECT
    if lower in XIAOZHI_ALIASES:
        return "xiaozhi"
    return value


def normalize_mainline_project_path(path: str | None, *, project_name: str | None = None) -> str:
    value = str(path or "").strip()
    if not value:
        return ""
    expanded = _normalize_path_value(value)
    if expanded.startswith(LEGACY_MAINLINE_PROJECTS_PREFIX):
        return str(VIZO_HOME / "projects" / expanded[len(LEGACY_MAINLINE_PROJECTS_PREFIX) :])
    project = canonicalize_project_name(project_name)
    if project == MAINLINE_PROJECT:
        if is_mainline_root_path(expanded):
            return str(VIZO_HOME)
    return expanded


def resolve_project_identity(name: str | None, path: str | None = None) -> str:
    raw_name = str(name or "").strip()
    canonical_name = canonicalize_project_name(raw_name)
    normalized_path = normalize_mainline_project_path(path, project_name=raw_name)
    if canonical_name != MAINLINE_PROJECT:
        return canonical_name or raw_name
    if not raw_name:
        return MAINLINE_PROJECT if is_mainline_root_path(normalized_path) else ""
    if raw_name.lower() in MAINLINE_ALIASES:
        return MAINLINE_PROJECT
    if is_mainline_root_path(normalized_path):
        return MAINLINE_PROJECT
    return raw_name


def build_project_path_map() -> dict[str, str]:
    project_map: dict[str, str] = {
        "/opt/xiaozhi-server": "xiaozhi",
        "/opt/xiaozhi": "xiaozhi",
        str(VIZO_HOME): MAINLINE_PROJECT,
    }

    try:
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        config = {}

    for name, info in (config.get("projects") or {}).items():
        if not isinstance(info, dict):
            continue
        path = normalize_mainline_project_path(info.get("path", ""), project_name=name)
        normalized_name = resolve_project_identity(name, path)
        if path:
            project_map[path] = normalized_name or name

    return project_map


def detect_project(cwd: str | None = None) -> str:
    current = normalize_runtime_path(cwd or os.getcwd())
    project_map = build_project_path_map()

    for path, project in sorted(project_map.items(), key=lambda item: len(item[0]), reverse=True):
        expanded = normalize_runtime_path(path)
        normalized_prefix = expanded.rstrip("/\\")
        if (
            current == expanded
            or current.startswith(normalized_prefix + "/")
            or current.startswith(normalized_prefix + os.sep)
        ):
            return project

    current_name = os.path.basename(current).lower()
    if current_name in MAINLINE_ALIASES:
        return MAINLINE_PROJECT
    lowered = current.lower()
    if "xiaozhi" in lowered:
        return "xiaozhi"

    return os.path.basename(current) or "default"
