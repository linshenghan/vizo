"""Helpers for user-visible Vizo web paths."""

from __future__ import annotations


PRIMARY_WEB_PREFIX = "/vizo"


def _join(path_suffix: str) -> str:
    suffix = path_suffix if path_suffix.startswith("/") else f"/{path_suffix}"
    return f"{PRIMARY_WEB_PREFIX}{suffix}"


def task_list_path() -> str:
    return _join("/tasks")


def task_detail_path(task_id: str) -> str:
    return _join(f"/tasks/{task_id}")


def confirm_path(request_id: str) -> str:
    return _join(f"/confirm/{request_id}")


def input_path(request_id: str) -> str:
    return _join(f"/input/{request_id}")


def preview_path(preview_id: str) -> str:
    return _join(f"/preview/{preview_id}")


def absolute_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + path
