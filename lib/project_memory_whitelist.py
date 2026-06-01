from __future__ import annotations

from pathlib import Path
from typing import Optional

from lib.project_identity import canonicalize_project_name, resolve_project_identity


WHITELIST_MEMORY_NAME = "startup/role-memory-whitelist"
MAIN_SESSION_SCOPE = "main_session"


def resolve_project_root_by_name(
    project: str | None,
    *,
    config: Optional[dict] = None,
    fallback_root: Path | str | None = None,
) -> Path | None:
    """Resolve a configured project root for the given project name."""
    project_name = canonicalize_project_name(project)
    projects = ((config or _load_config()).get("projects") or {})

    for name, info in projects.items():
        if not isinstance(info, dict):
            continue
        resolved_name = resolve_project_identity(name, info.get("path"))
        if project_name and resolved_name != project_name and canonicalize_project_name(name) != project_name:
            continue
        path = str(info.get("path", "") or "").strip()
        if path:
            return Path(path).resolve()

    if fallback_root:
        return Path(fallback_root).resolve()
    return None


def resolve_serena_project_name(project: str | None, *, config: Optional[dict] = None) -> str:
    """Resolve the Serena project alias declared in config.json for a project."""
    project_name = canonicalize_project_name(project)
    projects = ((config or _load_config()).get("projects") or {})

    for name, info in projects.items():
        if not isinstance(info, dict):
            continue
        resolved_name = resolve_project_identity(name, info.get("path"))
        if project_name and resolved_name != project_name and canonicalize_project_name(name) != project_name:
            continue
        serena_project = str(info.get("serena_project", "") or "").strip()
        return serena_project or resolved_name or canonicalize_project_name(name) or str(name)

    return project_name or ""


def load_project_memory_whitelist(project_root: Path | str | None) -> dict[str, list[str]]:
    """Parse the project-level Serena whitelist markdown into section -> memory names."""
    if not project_root:
        return {}

    whitelist_path = (
        Path(project_root)
        / ".serena"
        / "memories"
        / f"{WHITELIST_MEMORY_NAME}.md"
    )
    if not whitelist_path.exists():
        return {}

    whitelist: dict[str, list[str]] = {}
    current_section: str | None = None
    for raw_line in whitelist_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("## "):
            current_section = line[3:].strip().lower()
            whitelist.setdefault(current_section, [])
            continue
        if line.startswith("- ") and current_section:
            value = line[2:].strip().strip("`")
            if value and value not in whitelist[current_section]:
                whitelist[current_section].append(value)

    return whitelist


def resolve_required_memories(whitelist: dict[str, list[str]], scope: str | None = None) -> list[str]:
    """Return de-duplicated required memories for `always + scope`."""
    required: list[str] = []
    normalized_scope = str(scope or "").strip().lower()
    for section in ("always", normalized_scope):
        if not section:
            continue
        for item in whitelist.get(section, []):
            if item not in required:
                required.append(item)
    return required


def _load_config() -> dict:
    from lib.config_loader import load_config

    return load_config()
