from __future__ import annotations

from pathlib import Path

from vizo_core.agent_runner import AgentError

from ..contracts import (
    RUNTIME_FAMILY_CODEX,
    RuntimeExecutionContext,
    RuntimeExecutionMetadata,
    RuntimeSessionRecord,
)
from ..event_store import RuntimeEventStore, RuntimeSessionStore
from ..events import build_lifecycle_event, build_unified_events, utcnow_iso
from ..errors import wrap_runtime_error
from ..policy import resolve_subagent_route
from ..registry import build_default_runtime_registry


class SubAgentRuntimeController:
    """Unified entry for subagent execution across runtime families."""

    def __init__(self, agent_runner, *, project_root: Path | str | None = None, registry=None) -> None:
        self.agent_runner = agent_runner
        self.project_root = Path(project_root or ".")
        self.registry = registry or build_default_runtime_registry(agent_runner)
        self.session_store = RuntimeSessionStore(self.project_root)

    async def run(
        self,
        *,
        task=None,
        step_name: str | None = None,
        config: dict | None = None,
        on_action=None,
        on_stream_event=None,
        **kwargs,
    ):
        fallback_used = bool(kwargs.pop("_codex_failure_fallback_used", False))
        role = kwargs.get("role", "unknown")
        task_dir = kwargs.get("task_dir")
        resume_session = str(kwargs.get("resume_session") or "")
        config_data = config or self.agent_runner.config or {}
        previous_runtime_family = ""
        if resume_session and task is not None:
            runtime_meta = (
                task.get("runtime", {})
                if isinstance(task, dict)
                else (getattr(task, "metadata", {}) or {}).get("runtime", {})
            ) or {}
            subagents = runtime_meta.get("subagents", {}) if isinstance(runtime_meta, dict) else {}
            current_step = (
                step_name
                or (task.get("current_step", "") if isinstance(task, dict) else getattr(task, "current_step", ""))
                or role
            )
            step_runtime = subagents.get(current_step, {}) if isinstance(subagents, dict) else {}
            if isinstance(step_runtime, dict):
                previous_runtime_family = str(step_runtime.get("runtime_family") or "")
        decision = resolve_subagent_route(
            config=config_data,
            role=role,
            model_override=kwargs.get("model_override"),
            resume_session=resume_session,
            resume_runtime_family=previous_runtime_family,
        )
        if decision.blocking_issues:
            error = AgentError(
                "子代理运行时被阻断: " + ", ".join(decision.blocking_issues),
                error_code="runtime_blocked",
            )
            error.runtime_metadata = {"decision": decision.to_dict()}
            raise error
        context = RuntimeExecutionContext(
            scope=decision.scope,
            role=role,
            task_id=(
                (task.get("id", "") if isinstance(task, dict) else getattr(task, "id", ""))
                or kwargs.get("task_id", "")
            ),
            step_name=(
                step_name
                or (task.get("current_step", "") if isinstance(task, dict) else getattr(task, "current_step", ""))
                or ""
            ),
            task_dir=str(task_dir or ""),
            project_root=str(self.project_root),
            output_file=str(kwargs.get("output_file") or ""),
            resume_token=resume_session,
        )
        event_store = RuntimeEventStore.for_subagent(task_dir, context.step_name, role) if task_dir else None
        started_at = utcnow_iso()
        event_sequence = 1
        raw_event_count = 0

        if event_store:
            event_store.append_unified(
                build_lifecycle_event(
                    sequence=event_sequence,
                    decision=decision,
                    context=context,
                    event_name="route_decision",
                    payload={"decision": decision.to_dict()},
                )
            )
            event_sequence += 1

        def _handle_stream(raw_event: dict) -> None:
            nonlocal event_sequence, raw_event_count
            raw_event_count += 1
            if event_store:
                event_store.append_raw(raw_event)
                unified_events = build_unified_events(
                    raw_event,
                    sequence_start=event_sequence,
                    decision=decision,
                    context=context,
                )
                for item in unified_events:
                    event_store.append_unified(item)
                event_sequence += len(unified_events)
            if on_stream_event:
                on_stream_event(raw_event)

        adapter = self.registry.get_subagent(decision.runtime_family)

        try:
            result = await adapter.run(
                decision=decision,
                execution_context=context,
                on_action=on_action,
                on_stream_event=_handle_stream,
                **kwargs,
            )
        except Exception as error:
            fallback_model = _resolve_codex_failure_fallback_model(
                config=config_data,
                decision=decision,
                error=error,
                resume_session=resume_session,
                fallback_used=fallback_used,
            )
            if fallback_model:
                if event_store:
                    event_store.append_unified(
                        build_lifecycle_event(
                            sequence=event_sequence,
                            decision=decision,
                            context=context,
                            event_name="fallback",
                            payload={
                                "from_runtime": decision.runtime_family,
                                "to_model": fallback_model,
                                "error_code": getattr(error, "error_code", ""),
                                "message": str(error)[:240],
                            },
                        )
                    )
                fallback_kwargs = dict(kwargs)
                fallback_kwargs["model_override"] = fallback_model
                fallback_kwargs["resume_session"] = ""
                fallback_kwargs["_codex_failure_fallback_used"] = True
                return await self.run(
                    task=task,
                    step_name=step_name,
                    config=config_data,
                    on_action=on_action,
                    on_stream_event=on_stream_event,
                    **fallback_kwargs,
                )
            final_event_count = max(event_sequence - 1, 0) + (1 if event_store else 0)
            metadata = self._build_runtime_metadata(
                decision=decision,
                context=context,
                status=_classify_error_status(error),
                requested_model=decision.requested_model,
                selected_model=decision.selected_model,
                event_store=event_store,
                raw_event_count=raw_event_count,
                event_count=final_event_count,
                started_at=started_at,
                finished_at=utcnow_iso(),
                error=error,
            )
            if event_store:
                event_store.append_unified(
                    build_lifecycle_event(
                        sequence=event_sequence,
                        decision=decision,
                        context=context,
                        event_name="error",
                        payload={
                            "message": str(error),
                            "error_code": getattr(error, "error_code", ""),
                            "status": metadata.status,
                        },
                    )
                )
            raise wrap_runtime_error(error, metadata.to_dict()) from error

        native_session_id = ""
        if isinstance(result.data, dict):
            native_session_id = str(result.data.get("session_id", "") or "")

        final_event_count = max(event_sequence - 1, 0) + (1 if event_store else 0)
        metadata = self._build_runtime_metadata(
            decision=decision,
            context=context,
            status="completed",
            requested_model=decision.requested_model,
            selected_model=str(getattr(result, "model", "") or decision.selected_model),
            event_store=event_store,
            raw_event_count=raw_event_count,
            event_count=final_event_count,
            started_at=started_at,
            finished_at=utcnow_iso(),
            native_session_id=native_session_id,
            resume_token=context.resume_token or native_session_id,
        )

        if native_session_id or context.resume_token:
            session_record = RuntimeSessionRecord(
                session_key=self._session_key(context),
                scope=context.scope,
                runtime_family=decision.runtime_family,
                role=context.role,
                task_id=context.task_id,
                step_name=context.step_name,
                native_session_id=native_session_id,
                resume_token=context.resume_token or native_session_id,
                status="completed",
                resume_allowed=bool(native_session_id),
                created_at=started_at,
                updated_at=metadata.finished_at,
                metadata={
                    "requested_model": metadata.requested_model,
                    "selected_model": metadata.selected_model,
                },
            )
            session_path = self.session_store.save(session_record)
            metadata.session_record_path = str(session_path)

        if event_store:
            event_store.append_unified(
                build_lifecycle_event(
                    sequence=event_sequence,
                    decision=decision,
                    context=context,
                    event_name="completed",
                    payload={
                        "session_id": native_session_id,
                        "status": metadata.status,
                        "selected_model": metadata.selected_model,
                    },
                    session_id=native_session_id,
                )
            )

        setattr(result, "runtime_metadata", metadata.to_dict())
        if isinstance(result.data, dict):
            result.data["runtime_metadata"] = metadata.to_dict()
        return result

    @staticmethod
    def _session_key(context: RuntimeExecutionContext) -> str:
        if context.task_id:
            return f"{context.scope}:{context.task_id}:{context.step_name or context.role}"
        return f"{context.scope}:{context.role}:{context.resume_token or 'latest'}"

    @staticmethod
    def _build_runtime_metadata(
        *,
        decision,
        context,
        status: str,
        requested_model: str,
        selected_model: str,
        event_store,
        raw_event_count: int,
        event_count: int,
        started_at: str,
        finished_at: str,
        native_session_id: str = "",
        resume_token: str = "",
        error: Exception | None = None,
    ) -> RuntimeExecutionMetadata:
        return RuntimeExecutionMetadata(
            scope=context.scope,
            runtime_family=decision.runtime_family,
            adapter_key=decision.adapter_key,
            role=context.role,
            task_id=context.task_id,
            step_name=context.step_name,
            requested_model=requested_model,
            selected_model=selected_model,
            native_session_id=native_session_id,
            resume_token=resume_token,
            resume_allowed=bool(native_session_id),
            pause_supported=decision.supports_pause,
            pause_feedback_supported=decision.supports_pause_feedback,
            status=status,
            event_log_path=str(event_store.events_path) if event_store else "",
            raw_event_log_path=str(event_store.raw_events_path) if event_store else "",
            events_count=event_count,
            raw_event_count=raw_event_count,
            error_code=getattr(error, "error_code", "") if error else "",
            error_message=str(error) if error else "",
            diagnostics=list(decision.diagnostics),
            started_at=started_at,
            finished_at=finished_at,
        )


def _classify_error_status(error: Exception) -> str:
    action = getattr(error, "action", "")
    if action in {"pause", "rollback", "terminate"}:
        return "interrupted"
    timeout_type = getattr(error, "timeout_type", "")
    if timeout_type:
        return "timeout"
    error_code = getattr(error, "error_code", "")
    if error_code in {"E201", "E202"}:
        return "rate_limited"
    if error_code == "E204":
        return "preflight_timeout"
    if error_code == "E305":
        return "runtime_reconnect_failed"
    return "error"


def _resolve_codex_failure_fallback_model(
    *,
    config: dict,
    decision,
    error: Exception,
    resume_session: str,
    fallback_used: bool,
) -> str:
    if fallback_used or resume_session:
        return ""
    if decision.runtime_family != RUNTIME_FAMILY_CODEX:
        return ""
    if getattr(error, "error_code", "") not in {"E101", "E204", "E305"}:
        return ""

    account_login_fallback = str(config.get("codex_subagent_account_login_fallback_model") or "").strip()
    generic_fallback = str(config.get("codex_subagent_failure_fallback_model") or "").strip()
    fallback_model = account_login_fallback or generic_fallback
    if not fallback_model:
        return ""

    if account_login_fallback:
        main_cfg = ((config.get("external_models") or {}).get("anthropic") or {})
        if str(main_cfg.get("auth_mode") or "").strip() != "account_login":
            return ""

    if fallback_model in {str(decision.requested_model or ""), str(decision.selected_model or "")}:
        return ""
    return fallback_model
