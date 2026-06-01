"""
Global path helpers for Vizo state storage.

`VIZO_HOME` is the project root. The legacy `OPUS_HOME` name is kept as a
compatibility alias because older modules and hooks still import it.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Iterator


DATA_DIR_NAME = ".vizo"
LEGACY_DATA_DIR_NAME = ".opus"


def _default_project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def resolve_project_root(project_root: Path | str | None = None) -> Path:
    if project_root is None:
        return VIZO_HOME
    return Path(project_root)


VIZO_HOME = Path(
    os.environ.get("VIZO_HOME")
    or os.environ.get("OPUS_HOME")
    or str(_default_project_root())
)
OPUS_HOME = VIZO_HOME


def data_dir(project_root: Path | str | None = None) -> Path:
    return resolve_project_root(project_root) / DATA_DIR_NAME


def legacy_data_dir(project_root: Path | str | None = None) -> Path:
    return resolve_project_root(project_root) / LEGACY_DATA_DIR_NAME


def write_data_path(*parts: str, project_root: Path | str | None = None) -> Path:
    return data_dir(project_root).joinpath(*parts)


def _legacy_import_manifest_path(project_root: Path | str | None = None) -> Path:
    return data_dir(project_root) / ".legacy-import-manifest.json"


def _load_legacy_import_manifest(project_root: Path | str | None = None) -> set[str]:
    manifest_path = _legacy_import_manifest_path(project_root)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            return {str(item) for item in payload}
    except Exception:
        pass
    return set()


def get_legacy_import_manifest_scopes(project_root: Path | str | None = None) -> list[str]:
    return sorted(_load_legacy_import_manifest(project_root))


def list_pending_legacy_import_scopes(project_root: Path | str | None = None) -> list[str]:
    root = legacy_data_dir(project_root)
    if not root.is_dir():
        return []

    imported_scopes = _load_legacy_import_manifest(project_root)
    pending: list[str] = []
    try:
        entries = sorted(root.iterdir(), key=lambda entry: entry.name)
    except OSError:
        return []

    for entry in entries:
        scope = f"dir:{entry.name}" if entry.is_dir() else f"file:{entry.name}"
        if scope not in imported_scopes:
            pending.append(scope)
    return pending


def _save_legacy_import_manifest(scopes: set[str], project_root: Path | str | None = None) -> None:
    manifest_path = _legacy_import_manifest_path(project_root)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(sorted(scopes), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _copy_legacy_entry(source: Path, target: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def import_legacy_data_entry(
    scope: str,
    *parts: str,
    project_root: Path | str | None = None,
    is_dir: bool = False,
) -> Path:
    target = write_data_path(*parts, project_root=project_root)
    legacy = legacy_data_dir(project_root).joinpath(*parts)
    scopes = _load_legacy_import_manifest(project_root)
    if scope in scopes:
        return target

    try:
        if legacy.exists():
            if is_dir:
                target.mkdir(parents=True, exist_ok=True)
            _copy_legacy_entry(legacy, target)
    finally:
        scopes.add(scope)
        _save_legacy_import_manifest(scopes, project_root)

    return target


def read_data_path(*parts: str, project_root: Path | str | None = None) -> Path:
    scope = "file:" + "/".join(parts)
    return import_legacy_data_entry(scope, *parts, project_root=project_root)


def iter_storage_dirs(
    name: str,
    project_root: Path | str | None = None,
) -> Iterator[Path]:
    candidate = import_legacy_data_entry(
        f"dir:{name}",
        name,
        project_root=project_root,
        is_dir=True,
    )
    if candidate.is_dir():
        yield candidate


def task_dir(
    task_id: str,
    project_root: Path | str | None = None,
    prefer_existing: bool = True,
) -> Path:
    import_legacy_data_entry("dir:tasks", "tasks", project_root=project_root, is_dir=True)
    preferred = data_dir(project_root) / "tasks" / task_id
    if prefer_existing:
        if preferred.exists():
            return preferred
    return preferred


def task_path(
    task_id: str,
    *parts: str,
    project_root: Path | str | None = None,
    prefer_existing: bool = True,
) -> Path:
    return task_dir(
        task_id,
        project_root=project_root,
        prefer_existing=prefer_existing,
    ).joinpath(*parts)


def iter_task_dirs(project_root: Path | str | None = None) -> Iterator[Path]:
    seen: set[str] = set()
    for root in iter_storage_dirs("tasks", project_root=project_root):
        try:
            entries = sorted(root.iterdir(), key=lambda entry: entry.name, reverse=True)
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir():
                continue
            if entry.name in seen:
                continue
            seen.add(entry.name)
            yield entry


DATA_DIR = data_dir()
LEGACY_DATA_DIR = legacy_data_dir()
TASKS_DIR = DATA_DIR / "tasks"
LEGACY_TASKS_DIR = LEGACY_DATA_DIR / "tasks"
CONFIRMS_DIR = DATA_DIR / "confirms"
LEGACY_CONFIRMS_DIR = LEGACY_DATA_DIR / "confirms"
PREVIEWS_DIR = DATA_DIR / "previews"
LEGACY_PREVIEWS_DIR = LEGACY_DATA_DIR / "previews"
SIGNALS_DIR = DATA_DIR / "signals"
LEGACY_SIGNALS_DIR = LEGACY_DATA_DIR / "signals"
SESSIONS_DIR = DATA_DIR / "sessions"
LEGACY_SESSIONS_DIR = LEGACY_DATA_DIR / "sessions"
WORKTREES_DIR = DATA_DIR / "worktrees"
LEGACY_WORKTREES_DIR = LEGACY_DATA_DIR / "worktrees"
WORK_STATE_FILE = DATA_DIR / "work_state.json"
LEGACY_WORK_STATE_FILE = LEGACY_DATA_DIR / "work_state.json"
LOGS_DIR = VIZO_HOME / "logs"
CONFIG_FILE = VIZO_HOME / "config.json"
ROLE_TEMPLATES_DIR = VIZO_HOME / "role_templates"
HOOKS_DIR = VIZO_HOME / "hooks"
LIB_DIR = VIZO_HOME / "lib"
