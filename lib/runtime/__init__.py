"""Runtime abstraction primitives for Vizo multi-backend execution."""

from .contracts import (
    RUNTIME_FAMILY_CLAUDE_CODE,
    RUNTIME_FAMILY_CODEX,
    RUNTIME_SCOPE_MAIN_SESSION,
    RUNTIME_SCOPE_SUBAGENT,
    MainSessionRecord,
    RouteDecision,
    RuntimeModelDescriptor,
    RuntimeExecutionContext,
    RuntimeExecutionMetadata,
    RuntimeSessionRecord,
)
from .diagnostics import build_runtime_diagnostics_snapshot
from .event_store import RuntimeEventStore, RuntimeSessionStore
from .policy import resolve_main_session_route, resolve_subagent_route
from .registry import RuntimeRegistry, build_default_runtime_registry
from .session_store import MainSessionStore

__all__ = [
    "RouteDecision",
    "RuntimeModelDescriptor",
    "RUNTIME_FAMILY_CLAUDE_CODE",
    "RUNTIME_FAMILY_CODEX",
    "RUNTIME_SCOPE_MAIN_SESSION",
    "RUNTIME_SCOPE_SUBAGENT",
    "MainSessionRecord",
    "MainSessionStore",
    "RuntimeExecutionContext",
    "RuntimeExecutionMetadata",
    "RuntimeEventStore",
    "RuntimeRegistry",
    "RuntimeSessionRecord",
    "RuntimeSessionStore",
    "build_default_runtime_registry",
    "build_runtime_diagnostics_snapshot",
    "resolve_main_session_route",
    "resolve_subagent_route",
]
