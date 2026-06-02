#!/usr/bin/env python3
"""
Opus 智能协作系统 v3.0
"""

from .orchestrator import (
    Orchestrator,
    get_orchestrator,
    smart_process,
    execute_tasks,
    recall,
    TaskStatus,
    AtomicTask,
    OrchestratorResult
)

from .model_router import (
    ModelRouter,
    get_router,
    smart_call,
    TaskType,
    ModelResponse,
    ModelProvider
)

from .cache_manager import (
    CacheManager,
    get_cache_manager,
    cache_get,
    cache_set
)

# memory_system 已在 v2.5 重构中删除，改用 Serena MCP 直接操作
# 保留 stub 函数供向后兼容性
class MemorySystem:
    """Stub - v2.5 已删除，改用 Serena MCP"""
    pass

def get_memory_system():
    """Stub - v2.5 已删除，改用 Serena MCP"""
    raise NotImplementedError("v2.5 已删除 memory_system，请使用 Serena MCP 的 mcp__serena__* 工具")

def recall_project(*args, **kwargs):
    """Stub - v2.5 已删除"""
    raise NotImplementedError("v2.5 已删除 memory_system，请使用 Serena MCP")

def save_session(*args, **kwargs):
    """Stub - v2.5 已删除"""
    raise NotImplementedError("v2.5 已删除 memory_system，请使用 Serena MCP")

from .notification import (
    NotificationManager,
    get_notification_manager,
    NotificationType
)

from .project_session import (
    ProjectSession,
    start_project_session,
    get_current_session,
    end_current_session,
    detect_project,
    detect_project_from_path,
    auto_start_session_if_needed,
    project_task,
    project_decision,
    project_pending,
    project_end,
    KNOWN_PROJECTS
)

__version__ = "3.0.1"
__all__ = [
    # Orchestrator
    "Orchestrator",
    "get_orchestrator",
    "smart_process",
    "execute_tasks",
    "recall",
    "TaskStatus",
    "AtomicTask",
    "OrchestratorResult",

    # Router
    "ModelRouter",
    "get_router",
    "smart_call",
    "TaskType",
    "ModelResponse",
    "ModelProvider",

    # Cache
    "CacheManager",
    "get_cache_manager",
    "cache_get",
    "cache_set",

    # Memory (v2.5 已删除，下列为 stub 保留向后兼容)
    "MemorySystem",
    "get_memory_system",
    "recall_project",
    "save_session",

    # Notification
    "NotificationManager",
    "get_notification_manager",
    "NotificationType",

    # Project Session
    "ProjectSession",
    "start_project_session",
    "get_current_session",
    "end_current_session",
    "detect_project",
    "detect_project_from_path",
    "auto_start_session_if_needed",
    "project_task",
    "project_decision",
    "project_pending",
    "project_end",
    "KNOWN_PROJECTS"
]
