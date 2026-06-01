from __future__ import annotations

import logging
import os
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from lib.paths import iter_task_dirs

try:
    import psutil
except ImportError:  # pragma: no cover - 运行环境通常已安装
    psutil = None

try:
    import resource
except ImportError:  # pragma: no cover - 非 POSIX 平台兼容
    resource = None


logger = logging.getLogger(__name__)


DEFAULT_TASK_RESOURCE_GUARD = {
    "enabled": True,
    "dynamic_thresholds": True,
    "min_memory_available_mb": 2048,
    "memory_reserve_ratio": 0.35,
    "memory_per_active_task_mb": 1024,
    "memory_max_reserve_ratio": 0.75,
    "min_disk_free_mb": 1024,
    "min_fd_headroom": 128,
    "fd_per_active_task": 32,
    "min_process_headroom": 16,
    "process_per_active_task": 2,
    "active_task_statuses": ("running", "paused", "in_progress"),
}


@dataclass(frozen=True)
class ResourceIssue:
    code: str
    message: str
    severity: str = "blocking"

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
        }


@dataclass
class TaskResourceDecision:
    allowed: bool
    checks: dict[str, dict] = field(default_factory=dict)
    blocking_issues: tuple[ResourceIssue, ...] = field(default_factory=tuple)
    warnings: tuple[ResourceIssue, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "checks": self.checks,
            "blocking_issues": [item.to_dict() for item in self.blocking_issues],
            "warnings": [item.to_dict() for item in self.warnings],
        }


class TaskAdmissionError(Exception):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "task_resource_blocked",
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.runtime_metadata = details or {}


def _load_guard_config(config: dict | None) -> dict:
    merged = dict(DEFAULT_TASK_RESOURCE_GUARD)
    raw = (config or {}).get("task_resource_guard", {})
    if isinstance(raw, dict):
        for key, value in raw.items():
            if value is not None:
                merged[key] = value
    return merged


def _is_unlimited_limit(value: int | None) -> bool:
    if value is None:
        return True
    infinity = getattr(resource, "RLIM_INFINITY", None)
    return value < 0 or (infinity is not None and value == infinity)


def _get_limit(limit_name: str) -> tuple[int | None, int | None]:
    if resource is None:
        return None, None
    attr_name = {
        "nofile": "RLIMIT_NOFILE",
        "nproc": "RLIMIT_NPROC",
    }.get(limit_name, "")
    if not attr_name or not hasattr(resource, attr_name):
        return None, None
    try:
        return resource.getrlimit(getattr(resource, attr_name))
    except (OSError, ValueError):
        return None, None


def _process_fd_count() -> int:
    if psutil is None:
        raise RuntimeError("psutil_unavailable")
    proc = psutil.Process()
    if not hasattr(proc, "num_fds"):
        raise RuntimeError("num_fds_unsupported")
    return int(proc.num_fds())


def _count_user_processes(uid: int) -> int:
    if psutil is None:
        raise RuntimeError("psutil_unavailable")
    total = 0
    for proc in psutil.process_iter(attrs=["uids"]):
        try:
            uids = proc.info.get("uids")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        real_uid = getattr(uids, "real", None)
        if real_uid is None:
            if isinstance(uids, dict):
                real_uid = uids.get("real")
            elif isinstance(uids, (tuple, list)) and uids:
                real_uid = uids[0]
        if real_uid == uid:
            total += 1
    return total


def _count_active_tasks(
    project_root: Path,
    active_statuses: tuple[str, ...],
) -> int:
    total = 0
    for task_dir in iter_task_dirs(project_root):
        state_file = task_dir / "state.json"
        if not state_file.exists():
            continue
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        status = str(state.get("status", "") or "").strip()
        normalized = "running" if status == "in_progress" else status
        if normalized in active_statuses:
            total += 1
    return total


def evaluate_task_creation_resources(
    project_root: str | Path,
    config: dict | None = None,
) -> TaskResourceDecision:
    guard = _load_guard_config(config)
    project_path = Path(project_root or ".").resolve()
    checks: dict[str, dict] = {"project_root": str(project_path)}
    blocking: list[ResourceIssue] = []
    warnings: list[ResourceIssue] = []
    active_statuses_raw = guard.get("active_task_statuses", ("running", "paused", "in_progress"))
    active_statuses = tuple(str(item) for item in active_statuses_raw if str(item).strip())
    active_task_count = _count_active_tasks(project_path, active_statuses)

    if not guard.get("enabled", True):
        checks["guard"] = {"enabled": False}
        return TaskResourceDecision(
            allowed=True,
            checks=checks,
            warnings=tuple(warnings),
        )

    checks["guard"] = {"enabled": True, "thresholds": guard}
    checks["activity"] = {
        "active_task_count": active_task_count,
        "active_statuses": list(active_statuses),
    }

    if psutil is None:
        warnings.append(
            ResourceIssue(
                code="psutil_unavailable",
                message="psutil 未安装，无法执行完整资源预检",
                severity="warning",
            )
        )
    else:
        try:
            memory = psutil.virtual_memory()
            total_mb = int(memory.total / (1024 * 1024))
            available_mb = int(memory.available / (1024 * 1024))
            threshold_mb = int(guard.get("min_memory_available_mb", 2048))
            dynamic_enabled = bool(guard.get("dynamic_thresholds", True))
            dynamic_memory = {
                "enabled": dynamic_enabled,
                "base_floor_mb": threshold_mb,
            }
            if dynamic_enabled:
                reserve_ratio = float(guard.get("memory_reserve_ratio", 0.35))
                per_active_task_mb = int(guard.get("memory_per_active_task_mb", 1024))
                max_reserve_ratio = float(guard.get("memory_max_reserve_ratio", 0.75))
                ratio_reserve_mb = int(total_mb * reserve_ratio)
                threshold_mb = max(threshold_mb, ratio_reserve_mb)
                threshold_mb += active_task_count * per_active_task_mb
                if max_reserve_ratio > 0:
                    threshold_mb = min(threshold_mb, int(total_mb * max_reserve_ratio))
                threshold_mb = min(threshold_mb, total_mb)
                dynamic_memory.update({
                    "reserve_ratio": reserve_ratio,
                    "ratio_reserve_mb": ratio_reserve_mb,
                    "per_active_task_mb": per_active_task_mb,
                    "max_reserve_ratio": max_reserve_ratio,
                })
            checks["memory"] = {
                "total_mb": total_mb,
                "available_mb": available_mb,
                "threshold_mb": threshold_mb,
                "percent": round(float(memory.percent), 1),
                "dynamic": dynamic_memory,
            }
            if available_mb < threshold_mb:
                blocking.append(
                    ResourceIssue(
                        code="memory_available_low",
                        message=(
                            f"可用内存仅 {available_mb}MB，低于任务创建阈值 {threshold_mb}MB"
                            f"（总内存 {total_mb}MB，活跃任务 {active_task_count} 个）"
                        ),
                    )
                )
        except Exception as exc:
            warnings.append(
                ResourceIssue(
                    code="memory_probe_failed",
                    message=f"内存资源预检失败: {exc}",
                    severity="warning",
                )
            )

    try:
        disk = shutil.disk_usage(project_path)
        free_mb = int(disk.free / (1024 * 1024))
        total_mb = int(disk.total / (1024 * 1024)) or 1
        threshold_mb = int(guard.get("min_disk_free_mb", 1024))
        checks["disk"] = {
            "path": str(project_path),
            "free_mb": free_mb,
            "threshold_mb": threshold_mb,
            "percent_used": round(((disk.total - disk.free) / disk.total) * 100, 1)
            if disk.total > 0
            else 0.0,
            "total_mb": total_mb,
        }
        if free_mb < threshold_mb:
            blocking.append(
                ResourceIssue(
                    code="disk_free_low",
                    message=f"项目盘剩余空间仅 {free_mb}MB，低于任务创建阈值 {threshold_mb}MB",
                )
            )
    except OSError as exc:
        warnings.append(
            ResourceIssue(
                code="disk_probe_failed",
                message=f"磁盘资源预检失败: {exc}",
                severity="warning",
            )
        )

    fd_soft, fd_hard = _get_limit("nofile")
    if _is_unlimited_limit(fd_soft):
        checks["file_descriptors"] = {
            "supported": False,
            "reason": "unlimited_or_unsupported",
            "soft_limit": fd_soft,
            "hard_limit": fd_hard,
        }
    else:
        try:
            fd_used = _process_fd_count()
            fd_soft_limit = int(fd_soft or 0)
            fd_remaining = max(fd_soft_limit - fd_used, 0)
            threshold = int(guard.get("min_fd_headroom", 128))
            if guard.get("dynamic_thresholds", True):
                threshold += active_task_count * int(guard.get("fd_per_active_task", 32))
            checks["file_descriptors"] = {
                "supported": True,
                "used": fd_used,
                "soft_limit": fd_soft_limit,
                "hard_limit": fd_hard,
                "remaining": fd_remaining,
                "threshold": threshold,
                "base_threshold": int(guard.get("min_fd_headroom", 128)),
                "dynamic_extra": active_task_count * int(guard.get("fd_per_active_task", 32))
                if guard.get("dynamic_thresholds", True)
                else 0,
            }
            if fd_remaining < threshold:
                blocking.append(
                    ResourceIssue(
                        code="fd_headroom_low",
                        message=f"文件句柄余量仅 {fd_remaining}，低于任务创建阈值 {threshold}",
                    )
                )
        except Exception as exc:
            warnings.append(
                ResourceIssue(
                    code="fd_probe_failed",
                    message=f"文件句柄预检失败: {exc}",
                    severity="warning",
                )
            )

    proc_soft, proc_hard = _get_limit("nproc")
    if _is_unlimited_limit(proc_soft):
        checks["processes"] = {
            "supported": False,
            "reason": "unlimited_or_unsupported",
            "soft_limit": proc_soft,
            "hard_limit": proc_hard,
        }
    else:
        uid = os.getuid() if hasattr(os, "getuid") else None
        if uid is None:
            warnings.append(
                ResourceIssue(
                    code="process_probe_unsupported",
                    message="当前平台不支持进程数预检",
                    severity="warning",
                )
            )
        else:
            try:
                proc_used = _count_user_processes(uid)
                proc_soft_limit = int(proc_soft or 0)
                proc_remaining = max(proc_soft_limit - proc_used, 0)
                threshold = int(guard.get("min_process_headroom", 16))
                if guard.get("dynamic_thresholds", True):
                    threshold += active_task_count * int(guard.get("process_per_active_task", 2))
                checks["processes"] = {
                    "supported": True,
                    "uid": uid,
                    "used": proc_used,
                    "soft_limit": proc_soft_limit,
                    "hard_limit": proc_hard,
                    "remaining": proc_remaining,
                    "threshold": threshold,
                    "base_threshold": int(guard.get("min_process_headroom", 16)),
                    "dynamic_extra": active_task_count * int(guard.get("process_per_active_task", 2))
                    if guard.get("dynamic_thresholds", True)
                    else 0,
                }
                if proc_remaining < threshold:
                    blocking.append(
                        ResourceIssue(
                            code="process_headroom_low",
                            message=f"可用进程配额仅 {proc_remaining}，低于任务创建阈值 {threshold}",
                        )
                    )
            except Exception as exc:
                warnings.append(
                    ResourceIssue(
                        code="process_probe_failed",
                        message=f"进程数预检失败: {exc}",
                        severity="warning",
                    )
                )

    return TaskResourceDecision(
        allowed=not blocking,
        checks=checks,
        blocking_issues=tuple(blocking),
        warnings=tuple(warnings),
    )


def _format_preflight_snapshot(
    project_root: str | Path,
    decision: TaskResourceDecision,
) -> str:
    checks = decision.checks or {}
    activity = checks.get("activity", {}) or {}
    memory = checks.get("memory", {}) or {}
    disk = checks.get("disk", {}) or {}
    fds = checks.get("file_descriptors", {}) or {}
    processes = checks.get("processes", {}) or {}
    blocking_codes = [item.code for item in decision.blocking_issues]
    warning_codes = [item.code for item in decision.warnings]
    return (
        f"project={Path(project_root).resolve()} "
        f"active_tasks={activity.get('active_task_count', '?')} "
        f"mem_available_mb={memory.get('available_mb', '?')} "
        f"mem_threshold_mb={memory.get('threshold_mb', '?')} "
        f"mem_total_mb={memory.get('total_mb', '?')} "
        f"disk_free_mb={disk.get('free_mb', '?')} "
        f"disk_threshold_mb={disk.get('threshold_mb', '?')} "
        f"fd_remaining={fds.get('remaining', '?')} "
        f"fd_threshold={fds.get('threshold', '?')} "
        f"proc_remaining={processes.get('remaining', '?')} "
        f"proc_threshold={processes.get('threshold', '?')} "
        f"blocking={blocking_codes or '-'} "
        f"warnings={warning_codes or '-'}"
    )


def assert_task_creation_allowed(
    project_root: str | Path,
    config: dict | None = None,
) -> TaskResourceDecision:
    decision = evaluate_task_creation_resources(project_root, config=config)
    snapshot = _format_preflight_snapshot(project_root, decision)
    if decision.allowed:
        logger.info("任务创建资源预检通过: %s", snapshot)
        return decision
    summary = "；".join(item.message for item in decision.blocking_issues)
    logger.warning("任务创建资源预检阻断: %s summary=%s", snapshot, summary)
    raise TaskAdmissionError(
        f"当前机器资源不足，已阻止创建新任务：{summary}",
        details=decision.to_dict(),
    )
