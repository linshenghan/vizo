from __future__ import annotations

from typing import Callable

from .contracts import RUNTIME_FAMILY_CLAUDE_CODE, RUNTIME_FAMILY_CODEX
from .sessions.base import BaseMainSessionAdapter
from .sessions.claude_code import ClaudeMainSessionAdapter
from .sessions.codex import CodexMainSessionAdapter
from .subagents.base import BaseSubagentAdapter
from .subagents.claude_code import ClaudeSubagentAdapter
from .subagents.codex import CodexSubagentAdapter


class RuntimeRegistry:
    """Adapter lookup keyed by runtime family and execution scope."""

    def __init__(self) -> None:
        self._subagent_factories: dict[str, Callable[[], BaseSubagentAdapter]] = {}
        self._session_factories: dict[str, Callable[[], BaseMainSessionAdapter]] = {}

    def register_subagent(self, runtime_family: str, factory: Callable[[], BaseSubagentAdapter]) -> None:
        self._subagent_factories[runtime_family] = factory

    def get_subagent(self, runtime_family: str) -> BaseSubagentAdapter:
        try:
            return self._subagent_factories[runtime_family]()
        except KeyError as exc:
            raise KeyError(f"未注册的 subagent runtime adapter: {runtime_family}") from exc

    def register_session(self, runtime_family: str, factory: Callable[[], BaseMainSessionAdapter]) -> None:
        self._session_factories[runtime_family] = factory

    def get_session(self, runtime_family: str) -> BaseMainSessionAdapter:
        try:
            return self._session_factories[runtime_family]()
        except KeyError as exc:
            raise KeyError(f"未注册的 main session runtime adapter: {runtime_family}") from exc


def build_default_runtime_registry(agent_runner) -> RuntimeRegistry:
    registry = RuntimeRegistry()
    registry.register_subagent(
        RUNTIME_FAMILY_CLAUDE_CODE,
        lambda: ClaudeSubagentAdapter(agent_runner),
    )
    registry.register_subagent(
        RUNTIME_FAMILY_CODEX,
        lambda: CodexSubagentAdapter(agent_runner),
    )
    registry.register_session(
        RUNTIME_FAMILY_CLAUDE_CODE,
        lambda: ClaudeMainSessionAdapter(agent_runner),
    )
    registry.register_session(
        RUNTIME_FAMILY_CODEX,
        lambda: CodexMainSessionAdapter(agent_runner),
    )
    return registry
