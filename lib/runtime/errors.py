from __future__ import annotations

from typing import Any

from agent_runner import (
    AgentError,
    AgentRateLimitError,
    AgentSignalInterrupt,
    AgentTimeoutError,
)


class RuntimeErrorMixin:
    """Attach runtime metadata without breaking legacy exception handling."""

    runtime_metadata: dict[str, Any]

    def _attach_runtime_metadata(self, runtime_metadata: dict[str, Any]) -> None:
        self.runtime_metadata = runtime_metadata
        self.runtime_family = runtime_metadata.get("runtime_family", "")
        self.runtime_status = runtime_metadata.get("status", "")


class RuntimeAgentError(RuntimeErrorMixin, AgentError):
    def __init__(self, message: str, *, runtime_metadata: dict[str, Any], error_code: str = "E301", cost_usd: float = 0.0):
        super().__init__(message, error_code=error_code, cost_usd=cost_usd)
        self._attach_runtime_metadata(runtime_metadata)


class RuntimeRateLimitError(RuntimeErrorMixin, AgentRateLimitError):
    def __init__(self, message: str, *, runtime_metadata: dict[str, Any], error_code: str = "E201", cost_usd: float = 0.0):
        super().__init__(message, error_code=error_code, cost_usd=cost_usd)
        self._attach_runtime_metadata(runtime_metadata)


class RuntimeTimeoutError(RuntimeErrorMixin, AgentTimeoutError):
    def __init__(
        self,
        message: str,
        *,
        runtime_metadata: dict[str, Any],
        error_code: str = "E101",
        timeout_type: str = "idle",
        events: list | None = None,
        cost_usd: float = 0.0,
    ):
        super().__init__(
            message,
            error_code=error_code,
            timeout_type=timeout_type,
            events=events,
            cost_usd=cost_usd,
        )
        self._attach_runtime_metadata(runtime_metadata)


class RuntimeSignalInterrupt(RuntimeErrorMixin, AgentSignalInterrupt):
    def __init__(
        self,
        message: str,
        *,
        runtime_metadata: dict[str, Any],
        signal: dict | None = None,
        cost_usd: float = 0.0,
        events: list | None = None,
    ):
        super().__init__(message, signal=signal, cost_usd=cost_usd, events=events)
        self._attach_runtime_metadata(runtime_metadata)


def wrap_runtime_error(error: Exception, runtime_metadata: dict[str, Any]) -> Exception:
    if hasattr(error, "runtime_metadata"):
        return error

    if isinstance(error, AgentSignalInterrupt):
        return RuntimeSignalInterrupt(
            str(error),
            runtime_metadata=runtime_metadata,
            signal=getattr(error, "signal", None),
            cost_usd=getattr(error, "cost_usd", 0.0),
            events=getattr(error, "events", None),
        )

    if isinstance(error, AgentTimeoutError):
        return RuntimeTimeoutError(
            str(error),
            runtime_metadata=runtime_metadata,
            error_code=getattr(error, "error_code", "E101"),
            timeout_type=getattr(error, "timeout_type", "idle"),
            events=getattr(error, "events", None),
            cost_usd=getattr(error, "cost_usd", 0.0),
        )

    if isinstance(error, AgentRateLimitError):
        return RuntimeRateLimitError(
            str(error),
            runtime_metadata=runtime_metadata,
            error_code=getattr(error, "error_code", "E201"),
            cost_usd=getattr(error, "cost_usd", 0.0),
        )

    if isinstance(error, AgentError):
        return RuntimeAgentError(
            str(error),
            runtime_metadata=runtime_metadata,
            error_code=getattr(error, "error_code", "E301"),
            cost_usd=getattr(error, "cost_usd", 0.0),
        )

    setattr(error, "runtime_metadata", runtime_metadata)
    return error
