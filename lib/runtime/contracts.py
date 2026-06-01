from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


RUNTIME_SCOPE_SUBAGENT = "subagent"
RUNTIME_SCOPE_MAIN_SESSION = "main_session"
RUNTIME_FAMILY_CLAUDE_CODE = "claude_code"
RUNTIME_FAMILY_CODEX = "codex"


@dataclass(frozen=True)
class RouteDecision:
    """Resolved runtime choice for a single execution request."""

    scope: str
    runtime_family: str
    adapter_key: str
    requested_model: str
    selected_model: str
    can_resume: bool
    supports_pause: bool
    supports_pause_feedback: bool
    supports_native_resume: bool
    reason: str
    diagnostics: tuple[str, ...] = field(default_factory=tuple)
    display_model: str = ""
    provider_model: str = ""
    connection_id: str = ""
    reason_code: str = ""
    requires_new_session: bool = False
    blocking_issues: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["runtime_kind"] = self.runtime_family
        data["diagnostics"] = list(self.diagnostics)
        data["blocking_issues"] = list(self.blocking_issues)
        data["warnings"] = list(self.warnings)
        return data


@dataclass(frozen=True)
class RuntimeModelDescriptor:
    """Runtime/model capability view exposed to diagnostics consumers."""

    display_model: str
    provider_model: str
    runtime_family: str
    connection_id: str
    access_mode: str
    provider_family: str
    supports_main_session: bool
    supports_subagent: bool
    supports_native_resume: bool

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["runtime_kind"] = self.runtime_family
        return data


@dataclass(frozen=True)
class RuntimeExecutionContext:
    """Execution envelope shared by adapters and event stores."""

    scope: str
    role: str
    task_id: str = ""
    step_name: str = ""
    task_dir: str = ""
    project_root: str = ""
    output_file: str = ""
    resume_token: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RuntimeExecutionMetadata:
    """Persisted execution facts for future resume, diagnostics and audit."""

    scope: str
    runtime_family: str
    adapter_key: str
    role: str
    requested_model: str
    selected_model: str
    status: str
    task_id: str = ""
    step_name: str = ""
    native_session_id: str = ""
    resume_token: str = ""
    resume_allowed: bool = False
    pause_supported: bool = False
    pause_feedback_supported: bool = False
    event_log_path: str = ""
    raw_event_log_path: str = ""
    session_record_path: str = ""
    events_count: int = 0
    raw_event_count: int = 0
    error_code: str = ""
    error_message: str = ""
    diagnostics: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RuntimeSessionRecord:
    """Runtime-native session handle persisted for future resume."""

    session_key: str
    scope: str
    runtime_family: str
    role: str
    task_id: str = ""
    step_name: str = ""
    native_session_id: str = ""
    resume_token: str = ""
    status: str = "active"
    resume_allowed: bool = False
    created_at: str = ""
    updated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MainSessionRecord:
    """Persistent session record for Vizo-owned main session runtime."""

    session_id: str
    name: str
    cwd: str
    status: str
    runtime_family: str
    runtime_kind: str
    display_model: str
    provider_model: str
    connection_id: str
    connection_name: str = ""
    provider_id: str = ""
    provider_family: str = ""
    access_mode: str = ""
    provider_display: str = ""
    created_at: str = ""
    updated_at: str = ""
    last_active_at: str = ""
    current_turn_id: str = ""
    current_turn_status: str = ""
    current_turn_started_at: str = ""
    native_session_id: str = ""
    resume_token: str = ""
    last_error: str = ""
    last_error_code: str = ""
    latest_event_seq: int = 0
    legacy_terminal_session_id: str = ""
    pending_interaction: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_public_dict(self) -> dict[str, Any]:
        data = self.to_dict()
        metadata = dict(data.get("metadata") or {})
        metadata.pop("connection_snapshot", None)
        pending_inputs = metadata.pop("pending_inputs", [])
        public_pending_inputs = []
        if isinstance(pending_inputs, list):
            for index, item in enumerate(pending_inputs):
                if not isinstance(item, dict):
                    continue
                content = str(item.get("content") or "")
                attachments = []
                for attachment in item.get("attachments") or []:
                    if isinstance(attachment, dict):
                        attachments.append(
                            {
                                "id": str(attachment.get("id") or ""),
                                "name": str(attachment.get("name") or attachment.get("filename") or ""),
                                "size": attachment.get("size"),
                                "mime_type": str(attachment.get("mime_type") or attachment.get("type") or ""),
                            }
                        )
                public_pending_inputs.append(
                    {
                        "queue_id": str(item.get("queue_id") or f"pending_index_{index + 1}"),
                        "kind": str(item.get("kind") or "message"),
                        "content": content,
                        "content_preview": content[:240],
                        "attachments": attachments,
                        "requested_at": str(item.get("requested_at") or item.get("created_at") or ""),
                        "queue_position": index + 1,
                    }
                )
        pending_count = len(public_pending_inputs)
        if metadata.pop("pending_model_switch", None):
            pending_count += 1
        data["metadata"] = metadata
        reasoning_effort = str(metadata.get("reasoning_effort") or "").strip()
        if reasoning_effort:
            data["reasoning_effort"] = reasoning_effort
        if not isinstance(data.get("pending_interaction"), dict):
            data["pending_interaction"] = {}
        data["id"] = data["session_id"]
        data["pending_inputs"] = public_pending_inputs
        data["pending_input_count"] = pending_count
        data["has_pending_inputs"] = pending_count > 0
        return data
