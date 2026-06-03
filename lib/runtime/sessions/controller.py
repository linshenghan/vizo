from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vizo_core.agent_runner import AgentError, AgentRunner
from lib.config_loader import load_config
from lib.interaction_design_service import InteractionDesignInterception, InteractionDesignService
from lib.project_identity import normalize_runtime_path
from lib.project_memory_whitelist import MAIN_SESSION_SCOPE
from lib.startup_protocol import build_main_session_protocol, resolve_startup_protocol_context
from lib.task_titles import summarize_task_title

from ..contracts import (
    MainSessionRecord,
    RuntimeExecutionContext,
    RUNTIME_FAMILY_CLAUDE_CODE,
    RUNTIME_FAMILY_CODEX,
    RUNTIME_SCOPE_MAIN_SESSION,
)
from ..events import UnifiedEvent, build_lifecycle_event, build_unified_events, utcnow_iso
from ..image_artifacts import (
    IMAGE_ARTIFACT_EVENT_NAME,
    capture_image_generation_artifact,
    is_image_generation_event,
    list_image_artifacts,
    resolve_image_artifact_path,
    scrub_image_generation_event,
)
from ..policy import resolve_main_session_route
from ..registry import build_default_runtime_registry
from ..session_store import MainSessionStore
from .persistent_runtime import (
    TURN_EVENT_TIMESTAMP_SKEW_SECONDS,
    _extract_claude_text,
    _extract_runtime_event_timestamp,
    _resolve_claude_transcript_path,
    build_persistent_runtime,
)


_WORKER_PID_KEY = "current_turn_worker_pid"
_WORKER_STARTED_AT_KEY = "current_turn_worker_started_at"
_WORKER_ERROR_CODE = "worker_interrupted"
_WORKER_BOOT_GRACE_SECONDS = 30
_WORKER_MODULE = "lib.runtime.sessions.worker"
_PENDING_MODEL_SWITCH_KEY = "pending_model_switch"
_PENDING_INPUTS_KEY = "pending_inputs"
_CONTEXT_REPLAY_KEY = "context_replay_after_model_switch"
_CONTROL_COMMAND_PREFIX = "/"
_MODEL_COMMAND = "model"
_MAX_REPLAY_ITEMS = 18
_MAX_REPLAY_CHARS = 480
_MAX_PENDING_INPUTS = 20
_RUNTIME_PID_KEY = "persistent_runtime_pid"
_RUNTIME_STARTED_AT_KEY = "persistent_runtime_started_at"
_RUNTIME_HEARTBEAT_KEY = "persistent_runtime_heartbeat_at"
_RUNTIME_NATIVE_PATH_KEY = "persistent_runtime_transcript_path"
_REASONING_EFFORT_KEY = "reasoning_effort"
_CODEX_REASONING_EFFORTS = {"low", "medium", "high", "xhigh"}
_BACKGROUND_TASK_STATE_NAME = "background-task-state.json"
_PENDING_DESIGN_ADOPTION_KEY = "pending_design_adoption"
_LAST_DECLINED_DESIGN_TASK_KEY = "last_declined_design_task_id"
_BACKGROUND_CONFIRM_REPLIES = ("继续", "开始", "继续吧", "开始吧", "确认", "确定", "同意")
_BACKGROUND_CANCEL_REPLIES = ("取消", "先取消", "不用了", "先不启动", "先别启动")
_DESIGN_ADOPT_REPLIES = (
    "设为默认",
    "设为项目默认",
    "设为默认设计风格",
    "作为默认设计风格",
    "用这套做默认",
    "替换当前默认",
    "采用这套做默认",
)
_DESIGN_KEEP_REPLIES = (
    "保持当前默认",
    "暂不设为默认",
    "先不设为默认",
    "不用设为默认",
    "先保持当前默认",
)
_ATTACHMENT_NAME_MAX_LEN = 120
_DEFAULT_ATTACHMENT_UPLOAD_MAX_BYTES = 50 * 1024 * 1024
_INTERRUPTIBLE_TURN_STATUSES = {"running", "waiting_confirm", "waiting_interaction"}
_INTERRUPT_ERROR_CODE = "turn_interrupted"


class MainSessionController:
    """Turn-based controller for Vizo-owned main sessions."""

    def __init__(
        self,
        config: dict | None = None,
        *,
        project_root: Path | str | None = None,
        registry=None,
        repair_running_sessions: bool = True,
    ) -> None:
        self.project_root = Path(project_root or ".")
        self._config_loader = load_config
        self.agent_runner = AgentRunner(config or self._config_loader())
        self.interaction_design = InteractionDesignService(agent_runner=self.agent_runner)
        self.registry = registry or build_default_runtime_registry(self.agent_runner)
        self.store = MainSessionStore(self.project_root)
        self._turn_tasks: dict[str, asyncio.Task] = {}
        self._event_subscribers: dict[str, set[asyncio.Queue]] = {}
        self._persistent_runtimes: dict[str, Any] = {}
        self._runtime_housekeeping_task: asyncio.Task | None = None
        self._background_launch_tasks: set[asyncio.Task] = set()
        if repair_running_sessions:
            self._repair_orphaned_running_sessions()

    def _reload_config(self) -> dict:
        self.agent_runner.config = self._config_loader(force_reload=True)
        self.interaction_design.agent_runner = self.agent_runner
        return self.agent_runner.config

    def _resolve_connection(self, *, config: dict, connection_id: str = "") -> dict:
        connection_id = str(connection_id or "").strip()
        if connection_id:
            for item in config.get("saved_connections", []) or []:
                if str(item.get("id") or "") != connection_id:
                    continue
                return {
                    "connection_id": connection_id,
                    "name": str(item.get("name") or ""),
                    "base_url": str(item.get("base_url") or ""),
                    "api_key": str(item.get("api_key") or ""),
                    "provider_id": str(item.get("provider_id") or ""),
                    "access_mode": str(item.get("access_mode") or ""),
                    "provider_family": str(item.get("provider_family") or ""),
                    "auth_mode": str(item.get("auth_mode") or ""),
                    "auth_status": str(item.get("auth_status") or ""),
                    "auth_last_verified_at": str(item.get("auth_last_verified_at") or ""),
                    "auth_account_label": str(item.get("auth_account_label") or ""),
                    "codex_home": str(item.get("codex_home") or ""),
                    "env": dict(item.get("env", {}) or {}),
                }
            raise KeyError("connection_not_found")

        anthropic = (config.get("external_models", {}) or {}).get("anthropic", {}) or {}
        return {
            "connection_id": str(anthropic.get("active_saved_connection_id") or "main_session"),
            "name": "当前主连接",
            "base_url": str(anthropic.get("base_url") or ""),
            "api_key": str(anthropic.get("api_key") or ""),
            "provider_id": str(anthropic.get("provider_id") or ""),
            "access_mode": str(anthropic.get("access_mode") or ""),
            "provider_family": str(anthropic.get("provider_family") or ""),
            "auth_mode": str(anthropic.get("auth_mode") or ""),
            "auth_status": str(anthropic.get("auth_status") or ""),
            "auth_last_verified_at": str(anthropic.get("auth_last_verified_at") or ""),
            "auth_account_label": str(anthropic.get("auth_account_label") or ""),
            "codex_home": str(anthropic.get("codex_home") or ""),
            "env": dict(anthropic.get("env", {}) or {}),
        }

    @staticmethod
    def _normalize_reply(text: str) -> str:
        return str(text or "").strip().lower()

    def _background_task_state_path(self, session_id: str) -> Path:
        return self.store.session_dir(session_id) / _BACKGROUND_TASK_STATE_NAME

    def _background_task_launch_log_path(self, session_id: str) -> Path:
        return self.store.session_dir(session_id) / "background-task-launch.log"

    def _load_background_task_state(self, session_id: str) -> dict[str, Any]:
        path = self._background_task_state_path(session_id)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_background_task_state(self, session_id: str, payload: dict[str, Any]) -> None:
        path = self._background_task_state_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    @staticmethod
    def _append_background_launch_log(path: Path, message: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with path.open("a", encoding="utf-8") as log_file:
                log_file.write(f"[{ts}] {message.rstrip()}\n")
        except Exception:
            pass

    def _track_background_task_process(self, session_id: str, proc: subprocess.Popen, launch_log: Path) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._wait_background_task_process(session_id, proc, launch_log))
        self._background_launch_tasks.add(task)
        task.add_done_callback(self._background_launch_tasks.discard)

    async def _wait_background_task_process(
        self,
        session_id: str,
        proc: subprocess.Popen,
        launch_log: Path,
    ) -> None:
        try:
            returncode = await asyncio.to_thread(proc.wait)
        except Exception as error:
            self._append_background_launch_log(
                launch_log,
                f"failed waiting for background task pid={getattr(proc, 'pid', '?')}: {error}",
            )
            return

        self._append_background_launch_log(
            launch_log,
            f"background task pid={proc.pid} exited with returncode={returncode}",
        )
        state = self._load_background_task_state(session_id)
        launch = dict(state.get("last_launch") or {})
        try:
            launch_pid = int(launch.get("pid") or -1)
        except (TypeError, ValueError):
            launch_pid = -1
        if launch_pid != int(proc.pid):
            return
        launch["process_running"] = False
        launch["process_returncode"] = int(returncode)
        launch["process_exited_at"] = utcnow_iso()
        launch["runtime_status"] = "exited" if returncode == 0 else "failed"
        launch["is_active"] = False
        state["last_launch"] = launch
        state["updated_at"] = utcnow_iso()
        self._save_background_task_state(session_id, state)

    def _build_background_task_command(
        self,
        *,
        pending: dict[str, Any],
        project_name: str,
        config: dict[str, Any],
    ) -> list[str]:
        request = str(pending.get("request") or "").strip()
        target_kind = str(pending.get("target_kind") or "").strip()
        module_id = str(pending.get("module_id") or "").strip()
        if not request:
            raise ValueError("缺少 request。")
        command = [sys.executable, str(self.project_root / "opus.py")]
        if target_kind == "development":
            command.extend([request, "--dev"])
        elif target_kind == "agent_hub":
            if not module_id:
                raise ValueError("target_kind=agent_hub 时必须提供 module_id。")
            command.append(f"{request} @{module_id}")
        else:
            raise ValueError(f"Unsupported target_kind: {target_kind}")
        project_cfg = ((config.get("projects") or {}).get(project_name) or {}) if project_name else {}
        if project_name and isinstance(project_cfg, dict) and project_cfg:
            command.extend(["--project", project_name])
        return command

    @staticmethod
    def _format_background_task_prepare_notice(pending: dict[str, Any], *, replaced_existing: bool) -> str:
        target_label = str(pending.get("target_label") or "后台任务").strip()
        request = str(pending.get("request") or "").strip()
        reason = str(pending.get("reason") or "").strip()
        task_title = summarize_task_title(
            pending.get("task_title") or pending.get("task_summary") or request or target_label,
            fallback=target_label,
        )
        lines = ["系统确认请求："]
        if replaced_existing:
            lines.append("已用新的后台任务提案替换上一条待确认请求。")
        lines.extend(
            [
                f"将通过「{target_label}」完成《{task_title}》，是否继续？",
                "这会启动正式任务，并进入直播、暂停/恢复和知识沉淀等任务生命周期。",
            ]
        )
        if reason:
            lines.append(f"原因：{reason}")
        if request:
            lines.append(f"任务标题：{task_title}")
        lines.extend(
            [
                "如需继续，请直接回复“继续”或“开始”。",
                "如需取消，请直接回复“取消”。",
            ]
        )
        return "\n".join(lines).strip()

    @staticmethod
    def _format_background_task_confirm_message(launch: dict[str, Any]) -> str:
        target_label = str(launch.get("target_label") or "后台任务").strip()
        task_title = summarize_task_title(
            launch.get("task_title") or launch.get("task_summary") or launch.get("request") or target_label,
            fallback=target_label,
        )
        return (
            f"已在后台启动「{target_label}」：《{task_title}》。\n"
            "接下来它会进入正式任务生命周期；你可以继续留在主会话沟通，或者去任务面板查看进度。"
        )

    @staticmethod
    def _format_background_task_cancel_message() -> str:
        return "已取消当前后台任务提案，主会话不会启动任何正式任务。我们继续在当前会话沟通。"

    @staticmethod
    def _format_design_adoption_notice(candidate: dict[str, Any], *, replace_existing: bool) -> str:
        label = str(candidate.get("label") or candidate.get("system_id") or "未命名方案").strip()
        summary = str(candidate.get("summary") or "").strip()
        if replace_existing:
            lines = [
                "系统确认请求：",
                f"交互设计师已产出可采纳方案《{label}》。",
                "如果你确认采用它，将替换当前项目的默认设计风格。",
            ]
        else:
            lines = [
                "系统确认请求：",
                f"交互设计师已产出可采纳方案《{label}》。",
                "是否将它设为本项目的默认设计风格？",
            ]
        if summary:
            lines.append(f"方案摘要：{summary}")
        lines.extend(
            [
                "如需采纳，请直接回复“设为默认设计风格”。",
                "如暂不采纳，请直接回复“保持当前默认”或“暂不设为默认”。",
            ]
        )
        return "\n".join(lines).strip()

    def _handle_pending_background_task_reply(
        self,
        *,
        session: MainSessionRecord,
        content: str,
        config: dict[str, Any],
    ) -> str | None:
        normalized = self._normalize_reply(content)
        if not normalized:
            return None
        state = self._load_background_task_state(session.session_id)
        pending = dict(state.get("pending") or {})
        if not pending:
            return None
        task_title = summarize_task_title(
            pending.get("task_title") or pending.get("task_summary") or pending.get("request") or pending.get("target_label"),
            fallback=str(pending.get("target_label") or "后台任务"),
        )
        pending["task_title"] = task_title
        pending["task_summary"] = task_title
        if normalized in _BACKGROUND_CANCEL_REPLIES:
            state.pop("pending", None)
            state["updated_at"] = utcnow_iso()
            self._save_background_task_state(session.session_id, state)
            return self._format_background_task_cancel_message()
        if normalized not in _BACKGROUND_CONFIRM_REPLIES and not any(
            normalized.startswith(prefix) for prefix in ("继续", "开始")
        ):
            return None
        project_root = Path(
            (session.metadata or {}).get("project_root")
            or session.cwd
            or self.project_root
        )
        project_name = str((session.metadata or {}).get("project_name") or "").strip()
        launch_log = self._background_task_launch_log_path(session.session_id)
        log_handle = None
        try:
            command = self._build_background_task_command(
                pending=pending,
                project_name=project_name,
                config=config,
            )
            self._append_background_launch_log(
                launch_log,
                f"starting background task target={pending.get('target_label') or '后台任务'} cwd={project_root}",
            )
            log_handle = launch_log.open("ab")
            child_env = dict(os.environ)
            session_dir = self.store.session_dir(session.session_id)
            child_env.update({
                "VIZO_MAIN_SESSION_ID": session.session_id,
                "VIZO_MAIN_SESSION_DIR": str(session_dir),
                "VIZO_MAIN_SESSION_RUNTIME_DIR": str(session_dir / "runtime"),
                "VIZO_PROJECT_ROOT": str(project_root),
            })
            proc = subprocess.Popen(
                command,
                cwd=str(project_root),
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                env=child_env,
                start_new_session=True,
            )
        except Exception as error:
            return f"后台任务启动失败：{error}"
        finally:
            if log_handle:
                try:
                    log_handle.close()
                except Exception:
                    pass
        self._append_background_launch_log(launch_log, f"spawned background task pid={proc.pid}")
        self._track_background_task_process(session.session_id, proc, launch_log)
        launch = {
            **pending,
            "launched_at": utcnow_iso(),
            "pid": int(proc.pid),
            "command": command,
            "cwd": str(project_root),
            "launch_log": str(launch_log),
            "process_running": True,
            "is_active": True,
            "runtime_status": "active",
        }
        state["last_launch"] = launch
        state.pop("pending", None)
        state["updated_at"] = utcnow_iso()
        self._save_background_task_state(session.session_id, state)
        return self._format_background_task_confirm_message(launch)

    def _handle_pending_design_adoption_reply(
        self,
        *,
        session: MainSessionRecord,
        project_root: Path,
        content: str,
    ) -> str | None:
        normalized = self._normalize_reply(content)
        if not normalized:
            return None
        pending = dict((session.metadata or {}).get(_PENDING_DESIGN_ADOPTION_KEY) or {})
        if not pending:
            return None
        task_id = str(pending.get("task_id") or "").strip()
        if any(keyword in normalized for keyword in _DESIGN_KEEP_REPLIES):
            session.metadata.pop(_PENDING_DESIGN_ADOPTION_KEY, None)
            if task_id:
                session.metadata[_LAST_DECLINED_DESIGN_TASK_KEY] = task_id
            return "已保留当前项目默认设计风格，这次交互设计结果不会自动写入项目记忆。之后如果你想采用它，再告诉我即可。"
        if not any(keyword in normalized for keyword in _DESIGN_ADOPT_REPLIES):
            return None
        candidate = self.interaction_design.load_latest_task_design(project_root, task_id=task_id)
        session.metadata.pop(_PENDING_DESIGN_ADOPTION_KEY, None)
        if not candidate:
            return "之前可采纳的交互设计结果已不可用，请先重新查看任务结果。"
        adopted = self.interaction_design.load_adopted_design(project_root)
        adopted_source = dict(adopted.get("source") or {}) if isinstance(adopted, dict) else {}
        if adopted and str(adopted_source.get("task_id") or "").strip() == task_id:
            return "这套设计已经是当前项目的默认设计风格，无需重复采纳。"
        replace_existing = bool(adopted)
        adopted_candidate = self.interaction_design.adopt_design(
            project_root=project_root,
            candidate=candidate,
            replace_existing=replace_existing,
        )
        if session.metadata.get(_LAST_DECLINED_DESIGN_TASK_KEY) == task_id:
            session.metadata.pop(_LAST_DECLINED_DESIGN_TASK_KEY, None)
        return self.interaction_design._format_adopted_message(
            adopted_candidate,
            replace_existing=replace_existing,
        )

    def _maybe_prepare_design_adoption_notice(
        self,
        *,
        session: MainSessionRecord,
        project_root: Path,
    ) -> str | None:
        candidate = self.interaction_design.load_latest_task_design(project_root)
        if not candidate:
            return None
        source = dict(candidate.get("source") or {})
        task_id = str(source.get("task_id") or "").strip()
        if not task_id:
            return None
        adopted = self.interaction_design.load_adopted_design(project_root)
        adopted_source = dict(adopted.get("source") or {}) if isinstance(adopted, dict) else {}
        if adopted and str(adopted_source.get("task_id") or "").strip() == task_id:
            session.metadata.pop(_PENDING_DESIGN_ADOPTION_KEY, None)
            return None
        pending = dict((session.metadata or {}).get(_PENDING_DESIGN_ADOPTION_KEY) or {})
        if str(pending.get("task_id") or "").strip() == task_id:
            return None
        if str((session.metadata or {}).get(_LAST_DECLINED_DESIGN_TASK_KEY) or "").strip() == task_id:
            return None
        session.metadata[_PENDING_DESIGN_ADOPTION_KEY] = {
            "task_id": task_id,
            "label": str(candidate.get("label") or ""),
            "replace_existing": bool(adopted),
            "prompted_at": utcnow_iso(),
        }
        return self._format_design_adoption_notice(candidate, replace_existing=bool(adopted))

    def _collect_post_turn_notices(
        self,
        *,
        session: MainSessionRecord,
        session_id: str,
        project_root: Path,
        background_pending_before: str = "",
    ) -> list[str]:
        state = self._load_background_task_state(session_id)
        pending = dict(state.get("pending") or {})
        proposal_id = str(pending.get("proposal_id") or "").strip()
        if pending:
            if proposal_id and proposal_id != background_pending_before:
                return [
                    self._format_background_task_prepare_notice(
                        pending,
                        replaced_existing=bool(background_pending_before),
                    )
                ]
            return []
        design_notice = self._maybe_prepare_design_adoption_notice(
            session=session,
            project_root=project_root,
        )
        return [design_notice] if design_notice else []

    def list_sessions(self) -> list[dict]:
        self._repair_orphaned_running_sessions()
        return [record.to_public_dict() for record in self.store.list()]

    def get_session(self, session_id: str) -> MainSessionRecord | None:
        self._repair_orphaned_running_sessions()
        return self.store.load(session_id)

    def subscribe(self, session_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._event_subscribers.setdefault(session_id, set()).add(queue)
        return queue

    def unsubscribe(self, session_id: str, queue: asyncio.Queue) -> None:
        subscribers = self._event_subscribers.get(session_id)
        if not subscribers:
            return
        subscribers.discard(queue)
        if not subscribers:
            self._event_subscribers.pop(session_id, None)

    async def create_session(
        self,
        *,
        name: str = "",
        cwd: str = "",
        connection_id: str = "",
        display_model: str = "",
        runtime_override: str = "",
    ) -> MainSessionRecord:
        config = self._reload_config()
        resolved_cwd = normalize_runtime_path(cwd or self.project_root)
        from lib.settings_handler import resolve_main_session_connection

        startup_context = resolve_startup_protocol_context(
            cwd=resolved_cwd,
            scope=MAIN_SESSION_SCOPE,
            config=config,
            fallback_root=self.project_root,
        )
        connection = self._resolve_connection(config=config, connection_id=connection_id)
        resolved_connection = resolve_main_session_connection(
            connection.get("base_url", ""),
            values=connection,
            current_env=connection.get("env", {}) or {},
            stored_provider_id=connection.get("provider_id"),
        )
        decision = resolve_main_session_route(
            connection=connection,
            config=config,
            display_model=display_model or "",
            runtime_override=runtime_override or "",
        )
        if decision.blocking_issues:
            error = AgentError(
                "主会话运行时被阻断: " + ", ".join(decision.blocking_issues),
                error_code="runtime_blocked",
            )
            error.runtime_metadata = {"decision": decision.to_dict()}
            raise error

        session_id = "ms_" + uuid.uuid4().hex[:12]
        ts = utcnow_iso()
        session_dir = self.store.session_dir(session_id)
        runtime_dir = session_dir / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        record = MainSessionRecord(
            session_id=session_id,
            name=name or Path(resolved_cwd or ".").name or session_id,
            cwd=resolved_cwd,
            status="idle",
            runtime_family=decision.runtime_family,
            runtime_kind=decision.runtime_family,
            display_model=decision.display_model or decision.selected_model,
            provider_model=decision.provider_model or decision.selected_model,
            connection_id=connection.get("connection_id", ""),
            connection_name=connection.get("name", ""),
            provider_id=connection.get("provider_id", ""),
            provider_family=str(resolved_connection.get("provider_family") or ""),
            access_mode=str(resolved_connection.get("access_mode") or ""),
            provider_display=str(resolved_connection.get("provider_display") or ""),
            created_at=ts,
            updated_at=ts,
            last_active_at=ts,
            metadata={
                "decision": decision.to_dict(),
                "connection_snapshot": connection,
                "runtime_dir": str(runtime_dir),
                "last_message_path": str(runtime_dir / "last-message.txt"),
                "project_name": startup_context.project_name,
                "project_root": startup_context.project_root,
                "serena_project": startup_context.serena_project,
                "supports_pause_feedback": decision.supports_pause_feedback,
                "warnings": list(decision.warnings),
            },
        )
        if decision.runtime_family == RUNTIME_FAMILY_CODEX:
            record.metadata[_REASONING_EFFORT_KEY] = "medium"
        self.store.save(record)
        return record

    async def delete_session(self, session_id: str) -> None:
        session = self.store.load(session_id)
        if session and session.status == "running":
            worker_pid = _coerce_pid((session.metadata or {}).get(_WORKER_PID_KEY))
            if session_id in self._turn_tasks or (worker_pid and _main_session_worker_alive(worker_pid)):
                raise AgentError("运行中的主会话不能删除，请等待当前 turn 完成", error_code="session_running")
        task = self._turn_tasks.pop(session_id, None)
        if task:
            task.cancel()
        await self._shutdown_persistent_runtime(session_id)
        self.store.delete(session_id)

    async def close_session(self, session_id: str) -> MainSessionRecord:
        self._repair_orphaned_running_sessions()
        session = self.store.load(session_id)
        if not session:
            raise KeyError("session_not_found")
        worker_pid = _coerce_pid((session.metadata or {}).get(_WORKER_PID_KEY))
        if self._session_is_busy(session_id, session) or (worker_pid and _main_session_worker_alive(worker_pid)):
            raise AgentError("运行中的主会话不能关闭，请等待当前 turn 完成", error_code="session_running")
        await self._shutdown_persistent_runtime(session_id)
        session = self.store.load(session_id) or session
        now = utcnow_iso()
        session.status = "closed"
        session.current_turn_id = ""
        session.current_turn_status = "closed"
        session.current_turn_started_at = ""
        session.last_error = ""
        session.last_error_code = ""
        session.updated_at = now
        session.last_active_at = now
        session.metadata.pop(_WORKER_PID_KEY, None)
        session.metadata.pop(_WORKER_STARTED_AT_KEY, None)
        session.metadata.pop(_PENDING_INPUTS_KEY, None)
        session.metadata.pop(_PENDING_MODEL_SWITCH_KEY, None)
        session.metadata["closed_at"] = now
        self.store.save(session)
        return session

    async def interrupt_turn(
        self,
        session_id: str,
        *,
        mode: str = "soft",
        drop_pending: bool = True,
        expected_runtime: str = "",
    ) -> dict[str, Any]:
        self._repair_orphaned_running_sessions()
        session = self.store.load(session_id)
        if not session:
            raise KeyError("session_not_found")
        if expected_runtime and expected_runtime != session.runtime_family:
            raise AgentError("前端会话 runtime 已过期", error_code="runtime_mismatch")

        mode = "force" if str(mode or "").strip().lower() == "force" else "soft"
        worker_pid = _coerce_pid((session.metadata or {}).get(_WORKER_PID_KEY))
        worker_alive = bool(worker_pid and _main_session_worker_alive(worker_pid))
        runtime = self._persistent_runtimes.get(session_id)
        runtime_available = runtime is not None
        task = self._turn_tasks.get(session_id)
        turn_status = str(session.current_turn_status or session.status or "").strip()
        interruptible = (
            turn_status in _INTERRUPTIBLE_TURN_STATUSES
            or session.status == "running"
            or task is not None
            or worker_alive
            or runtime_available
        )
        if not interruptible:
            return {
                "status": "not_running",
                "session_id": session_id,
                "message": "当前会话没有正在执行的 turn。",
                "session": session.to_public_dict(),
            }

        interrupt_result: dict[str, Any] = {}
        if runtime is not None:
            if mode == "force":
                self._persistent_runtimes.pop(session_id, None)
            interrupt_result = await runtime.interrupt_turn(force=mode == "force")
        elif worker_alive:
            interrupt_result = await self._interrupt_worker_process(worker_pid, force=mode == "force")

        task = self._turn_tasks.pop(session_id, None)
        if task:
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass

        session = self.store.load(session_id) or session
        dropped_pending_count = self._discard_pending_inputs(session) if drop_pending else 0
        now = utcnow_iso()
        session.status = "interrupted"
        session.current_turn_status = "interrupted"
        session.pending_interaction = {}
        session.updated_at = now
        session.last_active_at = now
        session.last_error = "用户已中断当前主会话 turn。"
        session.last_error_code = _INTERRUPT_ERROR_CODE
        session.metadata.pop(_WORKER_PID_KEY, None)
        session.metadata.pop(_WORKER_STARTED_AT_KEY, None)
        session.metadata.pop("last_turn_error", None)
        if mode == "force" or bool(interrupt_result.get("runtime_stopped")):
            self._clear_persistent_runtime_metadata(session)

        sequence = int(session.latest_event_seq or 0) + 1
        self._append_session_events(
            session,
            [
                {
                    "sequence": sequence,
                    "timestamp": now,
                    "scope": RUNTIME_SCOPE_MAIN_SESSION,
                    "runtime_family": session.runtime_family,
                    "role": "user",
                    "event_type": "lifecycle",
                    "event_name": "interrupt_requested",
                    "payload": {
                        "turn_id": session.current_turn_id,
                        "mode": mode,
                        "drop_pending": bool(drop_pending),
                    },
                    "task_id": "",
                    "step_name": session.current_turn_id or "interrupt",
                    "session_id": session_id,
                },
                {
                    "sequence": sequence + 1,
                    "timestamp": now,
                    "scope": RUNTIME_SCOPE_MAIN_SESSION,
                    "runtime_family": session.runtime_family,
                    "role": "system",
                    "event_type": "lifecycle",
                    "event_name": "interrupted",
                    "payload": {
                        "turn_id": session.current_turn_id,
                        "mode": mode,
                        "dropped_pending_count": dropped_pending_count,
                        "runtime": interrupt_result,
                    },
                    "task_id": "",
                    "step_name": session.current_turn_id or "interrupt",
                    "session_id": session_id,
                },
            ],
        )
        self.store.save(session)
        return {
            "status": "interrupted",
            "session_id": session_id,
            "mode": mode,
            "dropped_pending_count": dropped_pending_count,
            "message": "已中断当前主会话 turn。",
            "session": session.to_public_dict(),
            "runtime": interrupt_result,
        }

    async def delete_pending_input(
        self,
        session_id: str,
        queue_id: str,
        *,
        expected_runtime: str = "",
    ) -> dict[str, Any]:
        session = self.store.load(session_id)
        if not session:
            raise KeyError("session_not_found")
        if expected_runtime and expected_runtime != session.runtime_family:
            raise AgentError("前端会话 runtime 已过期", error_code="runtime_mismatch")
        queue = self._pending_inputs(session)
        index = self._pending_input_index(queue, queue_id)
        if index < 0:
            raise AgentError("队列消息不存在或已被处理。", error_code="pending_input_not_found")
        removed = dict(queue.pop(index))
        self._store_pending_inputs(session, queue)
        session.updated_at = utcnow_iso()
        session.last_active_at = session.updated_at
        self.store.save(session)
        return {
            "status": "pending_input_deleted",
            "session_id": session_id,
            "queue_id": str(removed.get("queue_id") or queue_id),
            "message": "已删除队列消息。",
            "session": session.to_public_dict(),
        }

    async def send_pending_input_now(
        self,
        session_id: str,
        queue_id: str,
        *,
        expected_runtime: str = "",
        interrupt_mode: str = "soft",
    ) -> dict[str, Any]:
        session = self.store.load(session_id)
        if not session:
            raise KeyError("session_not_found")
        if expected_runtime and expected_runtime != session.runtime_family:
            raise AgentError("前端会话 runtime 已过期", error_code="runtime_mismatch")
        queue = self._pending_inputs(session)
        index = self._pending_input_index(queue, queue_id)
        if index < 0:
            raise AgentError("队列消息不存在或已被处理。", error_code="pending_input_not_found")
        selected = dict(queue[index])
        if selected.get("kind") != "message":
            raise AgentError("当前队列项不是消息，不能立即发送。", error_code="pending_input_action_invalid")

        if self._session_is_busy(session_id, session):
            await self.interrupt_turn(
                session_id,
                mode=interrupt_mode,
                drop_pending=False,
                expected_runtime=expected_runtime,
            )
            delay = max(float((self.agent_runner.config or {}).get("main_session_send_now_interrupt_delay", 0.45)), 0.0)
            if delay > 0:
                await asyncio.sleep(delay)

        session = self.store.load(session_id) or session
        queue = self._pending_inputs(session)
        index = self._pending_input_index(queue, queue_id)
        if index >= 0:
            selected = dict(queue.pop(index))
            self._store_pending_inputs(session, queue)
            session.updated_at = utcnow_iso()
            session.last_active_at = session.updated_at
            self.store.save(session)

        content = str(selected.get("content") or "").strip()
        attachments = list(selected.get("attachments") or [])
        if not content and attachments:
            content = "请阅读附加文件。"
        if not content:
            raise AgentError("队列消息内容为空，无法发送。", error_code="pending_input_empty")

        turn_id = await self.submit_message(
            session_id,
            content=content,
            attachments=attachments,
            expected_runtime=expected_runtime,
            repair_sessions=False,
        )
        session = self.store.load(session_id)
        return {
            "status": "pending_input_sent_now",
            "session_id": session_id,
            "turn_id": turn_id,
            "queue_id": str(selected.get("queue_id") or queue_id),
            "message": "已打断当前回复并立即发送队列消息。",
            "session": session.to_public_dict() if session else {},
        }

    async def switch_model(self, session_id: str, *, display_model: str) -> MainSessionRecord:
        result = await self.request_model_switch(
            session_id,
            display_model=display_model,
            raw_command=f"/model {display_model}",
        )
        session = self.store.load(session_id)
        if not session:
            raise KeyError("session_not_found")
        return session

    async def request_model_switch(
        self,
        session_id: str,
        *,
        display_model: str,
        reasoning_effort: str | None = None,
        raw_command: str = "",
        expected_runtime: str = "",
    ) -> dict[str, Any]:
        session = self.store.load(session_id)
        if not session:
            raise KeyError("session_not_found")
        if expected_runtime and expected_runtime != session.runtime_family:
            raise AgentError("前端会话 runtime 已过期", error_code="runtime_mismatch")
        self._discard_stale_pending_inputs(session_id, session)
        config = self._reload_config()
        connection = dict((session.metadata or {}).get("connection_snapshot") or {})
        decision = resolve_main_session_route(
            connection=connection,
            config=config,
            display_model=display_model,
            current_runtime_family=session.runtime_family,
        )
        if decision.blocking_issues:
            error = AgentError(
                "主会话切模被阻断: " + ", ".join(decision.blocking_issues),
                error_code=decision.blocking_issues[0],
            )
            error.runtime_metadata = {"decision": decision.to_dict(), "session_id": session_id}
            raise error
        target_model = decision.display_model or decision.selected_model
        target_reasoning_effort = self._target_reasoning_effort(
            session,
            reasoning_effort=reasoning_effort,
            target_runtime_family=decision.runtime_family,
        )
        current_reasoning_effort = self._session_reasoning_effort(session)
        request_text = raw_command or self._format_model_switch_request_text(
            session,
            target_model=target_model,
            target_reasoning_effort=target_reasoning_effort,
            current_reasoning_effort=current_reasoning_effort,
        )
        if (
            target_model == session.display_model
            and target_reasoning_effort == current_reasoning_effort
            and not session.metadata.get(_PENDING_MODEL_SWITCH_KEY)
            and not self._pending_inputs(session)
        ):
            label = self._format_model_switch_compact_label(
                target_model=target_model,
                target_reasoning_effort=target_reasoning_effort,
            )
            message = f"当前已是{label}"
            self._append_control_exchange(
                session,
                user_text=request_text,
                assistant_text=message,
            )
            session.updated_at = utcnow_iso()
            session.last_active_at = session.updated_at
            self.store.save(session)
            return {
                "status": "model_switch_noop",
                "command": _MODEL_COMMAND,
                "queued": False,
                "display_model": session.display_model,
                "reasoning_effort": current_reasoning_effort,
                "message": message,
                "session": session.to_public_dict(),
            }

        if self._session_accepts_control_command(session_id, session):
            label = self._format_model_switch_compact_label(
                target_model=target_model,
                target_reasoning_effort=target_reasoning_effort,
            )
            queued_message = f"已记录切换为{label}，轮次完成后生效。"
            return self._enqueue_pending_input(
                session,
                {
                    "kind": "model_switch",
                    "display_model": target_model,
                    "provider_model": decision.provider_model or session.provider_model or target_model,
                    "reasoning_effort": target_reasoning_effort,
                    "requested_at": utcnow_iso(),
                    "raw_command": request_text,
                    "decision": decision.to_dict(),
                },
                status="model_switch_queued",
                message=queued_message,
                extra_payload={
                    "command": _MODEL_COMMAND,
                    "queued_display_model": target_model,
                    "display_model": session.display_model,
                    "reasoning_effort": current_reasoning_effort,
                },
            )

        label = self._format_model_switch_compact_label(
            target_model=target_model,
            target_reasoning_effort=target_reasoning_effort,
        )
        message = f"成功切换为{label}"
        self._apply_model_switch(
            session,
            target_model=target_model,
            provider_model=decision.provider_model or session.provider_model or target_model,
            reasoning_effort=target_reasoning_effort,
            decision_data=decision.to_dict(),
            raw_command=request_text,
            assistant_text=message,
        )
        await self._shutdown_persistent_runtime(session_id)
        return {
            "status": "model_switch_applied",
            "command": _MODEL_COMMAND,
            "queued": False,
            "display_model": session.display_model,
            "reasoning_effort": self._session_reasoning_effort(session),
            "message": message,
            "session": session.to_public_dict(),
        }

    async def submit_interaction_response(
        self,
        session_id: str,
        *,
        request_id: str,
        action: str,
        text: str = "",
        expected_runtime: str = "",
    ) -> dict[str, Any]:
        self._repair_orphaned_running_sessions()
        session = self.store.load(session_id)
        if not session:
            raise KeyError("session_not_found")
        if expected_runtime and expected_runtime != session.runtime_family:
            raise AgentError("前端会话 runtime 已过期", error_code="runtime_mismatch")

        pending = self._normalize_pending_interaction(session.pending_interaction)
        if not pending:
            raise AgentError("当前会话没有待处理的交互请求。", error_code="interaction_not_pending")

        current_request_id = str(pending.get("request_id") or "").strip()
        request_id = str(request_id or "").strip()
        if not current_request_id or request_id != current_request_id:
            raise AgentError("当前交互请求已变化，请刷新后重试。", error_code="interaction_stale")

        action = str(action or "").strip()
        available_actions = {
            str(item).strip()
            for item in (pending.get("available_actions") or [])
            if str(item).strip()
        }
        if available_actions and action not in available_actions:
            raise AgentError("当前交互请求不支持该操作。", error_code="interaction_action_invalid")

        text = str(text or "")
        text_required_actions = {
            str(item).strip()
            for item in (pending.get("text_required_actions") or [])
            if str(item).strip()
        }
        if not text_required_actions and pending.get("requires_text") and available_actions:
            text_required_actions = set(available_actions)
        if action in text_required_actions and not text.strip():
            raise AgentError("当前交互请求要求填写文本说明。", error_code="interaction_text_required")

        runtime = self._persistent_runtimes.get(session_id)
        if runtime is None:
            raise AgentError(
                "当前主会话 runtime 暂不支持交互请求。",
                error_code="interactive_request_unavailable",
            )

        runtime_result = await runtime.submit_interaction_response(
            request_id=request_id,
            action=action,
            text=text,
        )
        if not isinstance(runtime_result, dict):
            runtime_result = {}

        next_pending = self._normalize_pending_interaction(runtime_result.get("pending_interaction"))
        next_turn_status = str(runtime_result.get("current_turn_status") or "").strip()
        message = str(runtime_result.get("message") or self._interaction_action_message(action)).strip()

        session = self.store.load(session_id) or session
        session.pending_interaction = next_pending
        if next_turn_status:
            session.current_turn_status = next_turn_status
        elif next_pending:
            session.current_turn_status = "waiting_interaction"
        elif str(session.current_turn_status or "").strip() in {"waiting_interaction", "waiting_confirm"}:
            session.current_turn_status = "running"
        if next_pending or session.current_turn_status in {"running", "waiting_interaction", "waiting_confirm"}:
            session.status = "running"
        session.updated_at = utcnow_iso()
        session.last_active_at = session.updated_at
        self._append_session_events(
            session,
            [
                {
                    "sequence": int(session.latest_event_seq or 0) + 1,
                    "timestamp": session.updated_at,
                    "scope": RUNTIME_SCOPE_MAIN_SESSION,
                    "runtime_family": session.runtime_family,
                    "role": "user",
                    "event_type": "lifecycle",
                    "event_name": "interaction_response_submitted",
                    "payload": {
                        "request_id": request_id,
                        "action": action,
                        "text": text.strip(),
                    },
                    "task_id": "",
                    "step_name": "interaction",
                    "session_id": session.session_id,
                }
            ],
        )
        self.store.save(session)
        return {
            "status": "interaction_response_submitted",
            "session_id": session_id,
            "request_id": request_id,
            "action": action,
            "message": message,
            "session": session.to_public_dict(),
        }

    async def accept_client_input(
        self,
        session_id: str,
        *,
        content: str,
        attachments: list[Any] | None = None,
        expected_runtime: str = "",
    ) -> dict[str, Any]:
        attachments = list(attachments or [])
        self._repair_orphaned_running_sessions()
        session = self.store.load(session_id)
        if not session:
            raise KeyError("session_not_found")
        if expected_runtime and expected_runtime != session.runtime_family:
            raise AgentError("前端会话 runtime 已过期", error_code="runtime_mismatch")
        self._discard_stale_pending_inputs(session_id, session)
        command = _parse_control_command(content)
        if command:
            if attachments:
                raise AgentError("控制命令不支持附加文件", error_code="control_command_attachments_forbidden")
            if command["name"] == _MODEL_COMMAND:
                return await self.request_model_switch(
                    session_id,
                    display_model=command["argument"],
                    raw_command=content,
                    expected_runtime=expected_runtime,
                )

        if self._session_is_busy(session_id, session):
            return self._enqueue_pending_input(
                session,
                {
                    "kind": "message",
                    "content": content,
                    "attachments": attachments,
                    "requested_at": utcnow_iso(),
                },
                status="message_queued",
                message="已加入队列，当前轮结束后会自动继续处理。",
            )

        turn_id = await self.submit_message(
            session_id,
            content=content,
            attachments=attachments,
            expected_runtime=expected_runtime,
        )
        session = self.get_session(session_id)
        return {
            "status": "accepted",
            "turn_id": turn_id,
            "session_id": session_id,
            "runtime_kind": session.runtime_kind if session else "",
        }

    async def switch_connection(self, session_id: str, *, connection_id: str) -> MainSessionRecord:
        session = self.store.load(session_id)
        if not session:
            raise KeyError("session_not_found")
        config = self._reload_config()
        connection = self._resolve_connection(config=config, connection_id=connection_id)
        decision = resolve_main_session_route(
            connection=connection,
            config=config,
            display_model=session.display_model,
            current_runtime_family=session.runtime_family,
        )
        if decision.blocking_issues:
            error = AgentError(
                "主会话切渠道被阻断: " + ", ".join(decision.blocking_issues),
                error_code=decision.blocking_issues[0],
            )
            error.runtime_metadata = {"decision": decision.to_dict(), "session_id": session_id}
            raise error
        from lib.settings_handler import resolve_main_session_connection

        resolved_connection = resolve_main_session_connection(
            connection.get("base_url", ""),
            values=connection,
            current_env=connection.get("env", {}) or {},
            stored_provider_id=connection.get("provider_id"),
        )
        session.connection_id = connection.get("connection_id", "")
        session.connection_name = connection.get("name", "")
        session.provider_id = connection.get("provider_id", "")
        session.provider_family = str(resolved_connection.get("provider_family") or "")
        session.provider_display = str(resolved_connection.get("provider_display") or "")
        session.access_mode = str(resolved_connection.get("access_mode") or "")
        session.display_model = decision.display_model or decision.selected_model
        session.provider_model = decision.provider_model or session.provider_model
        if decision.runtime_family == RUNTIME_FAMILY_CODEX:
            session.metadata[_REASONING_EFFORT_KEY] = "medium"
        else:
            session.metadata.pop(_REASONING_EFFORT_KEY, None)
        session.updated_at = utcnow_iso()
        session.last_active_at = session.updated_at
        session.metadata["decision"] = decision.to_dict()
        session.metadata["connection_snapshot"] = connection
        session.resume_token = ""
        session.native_session_id = ""
        session.metadata[_CONTEXT_REPLAY_KEY] = {
            "target_model": session.display_model,
            "reasoning_effort": self._session_reasoning_effort(session),
            "applied_at": session.updated_at,
            "reason": "connection_switch",
        }
        self.store.save(session)
        await self._shutdown_persistent_runtime(session_id)
        return session

    async def submit_message(
        self,
        session_id: str,
        *,
        content: str,
        attachments: list[Any] | None = None,
        expected_runtime: str = "",
        repair_sessions: bool = True,
    ) -> str:
        config = self._reload_config()
        if repair_sessions:
            self._repair_orphaned_running_sessions()
        session = self.store.load(session_id)
        if not session:
            raise KeyError("session_not_found")
        if self._session_is_busy(session_id, session):
            raise AgentError("当前会话已有运行中的 turn", error_code="session_busy")
        if expected_runtime and expected_runtime != session.runtime_family:
            raise AgentError("前端会话 runtime 已过期", error_code="runtime_mismatch")

        turn_id = "turn_" + uuid.uuid4().hex[:10]
        now = utcnow_iso()
        session.status = "running"
        session.current_turn_id = turn_id
        session.current_turn_status = "running"
        session.current_turn_started_at = now
        session.updated_at = now
        session.last_active_at = now
        session.last_error = ""
        session.last_error_code = ""
        session.metadata.pop(_WORKER_PID_KEY, None)
        session.metadata.pop(_WORKER_STARTED_AT_KEY, None)
        session.metadata.pop("last_turn_error", None)
        request_attachments = list(attachments or [])
        self._append_turn_start_events(
            session,
            turn_id=turn_id,
            content=content,
            attachments=request_attachments,
            config=config,
        )
        self.store.save(session)
        self.store.save_turn_request(
            session_id,
            turn_id,
            {
                "session_id": session_id,
                "turn_id": turn_id,
                "content": content,
                "attachments": request_attachments,
                "created_at": now,
                "events_prelogged": True,
            },
        )

        if self._persistent_runtime_enabled(config) and self._session_supports_persistent_runtime(session):
            task = asyncio.create_task(
                self.run_turn_request(
                    session_id=session_id,
                    turn_id=turn_id,
                )
            )
            self._turn_tasks[session_id] = task
            return turn_id

        if self._detached_workers_enabled(config):
            try:
                worker_pid = await self._spawn_turn_worker(session_id=session_id, turn_id=turn_id)
            except Exception as error:
                session = self.store.load(session_id) or session
                session.status = "error"
                session.current_turn_status = "failed"
                session.last_error = str(error)
                session.last_error_code = "worker_spawn_failed"
                session.updated_at = utcnow_iso()
                session.last_active_at = session.updated_at
                self.store.save(session)
                raise AgentError(
                    f"主会话 worker 启动失败: {str(error)[:200]}",
                    error_code="worker_spawn_failed",
                ) from error

            session = self.store.load(session_id) or session
            session.metadata[_WORKER_PID_KEY] = worker_pid
            session.metadata[_WORKER_STARTED_AT_KEY] = utcnow_iso()
            session.updated_at = utcnow_iso()
            session.last_active_at = session.updated_at
            self.store.save(session)
            return turn_id

        task = asyncio.create_task(
            self.run_turn_request(
                session_id=session_id,
                turn_id=turn_id,
            )
            )
        self._turn_tasks[session_id] = task
        return turn_id

    def attachment_upload_max_bytes(self) -> int:
        config = self.agent_runner.config if isinstance(self.agent_runner.config, dict) else {}
        raw = config.get("main_session_attachment_upload_max_bytes", _DEFAULT_ATTACHMENT_UPLOAD_MAX_BYTES)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = _DEFAULT_ATTACHMENT_UPLOAD_MAX_BYTES
        return max(1, min(value, 512 * 1024 * 1024))

    def save_client_attachment(
        self,
        session_id: str,
        *,
        filename: str,
        content: bytes,
        content_type: str = "",
    ) -> dict[str, Any]:
        session = self.store.load(session_id)
        if not session:
            raise KeyError("session_not_found")
        data = bytes(content or b"")
        max_bytes = self.attachment_upload_max_bytes()
        if len(data) > max_bytes:
            raise AgentError(
                f"附件超过上传上限（{_format_bytes(max_bytes)}）",
                error_code="attachment_too_large",
            )
        attachment_id = "att_" + uuid.uuid4().hex[:12]
        display_name = str(filename or "").strip() or "attachment"
        attachment_dir = self.store.session_dir(session_id) / "attachments" / attachment_id
        attachment_dir.mkdir(parents=True, exist_ok=True)
        target = attachment_dir / _safe_attachment_file_name(display_name, 0)
        target.write_bytes(data)
        metadata = {
            "id": attachment_id,
            "name": display_name,
            "size": len(data),
            "type": str(content_type or "").strip(),
            "path": str(target),
            "uploaded_at": utcnow_iso(),
        }
        (attachment_dir / "metadata.json").write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return metadata

    def get_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        before_seq: int | None = None,
        limit: int = 200,
    ) -> dict:
        session = self.store.load(session_id)
        if not session:
            raise KeyError("session_not_found")
        if int(after_seq) <= 0:
            session = self._backfill_image_events(session)
        items = self.store.read_events(session_id, after_seq=after_seq, before_seq=before_seq, limit=limit)
        events = [self._public_event(session_id, item) for item in items]
        event_seqs = [int(event.get("seq") or 0) for event in events]
        next_after_seq = max([int(after_seq)] + event_seqs) if event_seqs else int(after_seq)
        oldest_seq = min(event_seqs) if event_seqs else int(before_seq or after_seq or 0)
        return {
            "session": session.to_public_dict(),
            "events": events,
            "next_after_seq": next_after_seq,
            "oldest_seq": oldest_seq,
            "has_more_before": bool(oldest_seq and oldest_seq > 1),
        }

    def get_logs(self, session_id: str, *, offset: int = 0, limit: int = 65536) -> dict:
        session = self.store.load(session_id)
        if not session:
            raise KeyError("session_not_found")
        payload = self.store.read_raw_log(session_id, offset=offset, limit=limit)
        payload["session_id"] = session.session_id
        payload["runtime_kind"] = session.runtime_kind
        return payload

    def _backfill_image_events(self, session: MainSessionRecord) -> MainSessionRecord:
        session_id = session.session_id
        images = list_image_artifacts(
            session_dir=self.store.session_dir(session_id),
            session_id=session_id,
            backfill_from_raw_events=True,
        )
        if not images:
            return session

        existing_events = self.store.read_events(session_id, after_seq=0, limit=1000)
        existing_image_ids = {
            str((item.get("payload") or {}).get("id") or "")
            for item in existing_events
            if str(item.get("event_name") or "") == IMAGE_ARTIFACT_EVENT_NAME and isinstance(item.get("payload"), dict)
        }
        next_sequence = max(
            [int(session.latest_event_seq or 0)]
            + [int(item.get("sequence") or 0) for item in existing_events]
        ) + 1
        event_store = self.store.event_store(session_id)
        added = False
        for image in sorted(images, key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or ""))):
            image_id = str(image.get("id") or "")
            if not image_id or image_id in existing_image_ids:
                continue
            event_dict = {
                "sequence": next_sequence,
                "timestamp": str(image.get("created_at") or utcnow_iso()),
                "scope": RUNTIME_SCOPE_MAIN_SESSION,
                "runtime_family": session.runtime_family,
                "role": "assistant",
                "event_type": "message",
                "event_name": IMAGE_ARTIFACT_EVENT_NAME,
                "payload": image,
                "task_id": "",
                "step_name": str(image.get("turn_id") or ""),
                "session_id": session_id,
            }
            event_store.append_unified(_dict_to_unified_event(event_dict))
            session.latest_event_seq = max(session.latest_event_seq, next_sequence)
            existing_image_ids.add(image_id)
            next_sequence += 1
            added = True
        if added:
            session.updated_at = utcnow_iso()
            self.store.save(session)
        return session

    def get_images(self, session_id: str) -> dict:
        session = self.store.load(session_id)
        if not session:
            raise KeyError("session_not_found")
        images = list_image_artifacts(
            session_dir=self.store.session_dir(session_id),
            session_id=session_id,
            backfill_from_raw_events=True,
        )
        return {
            "session": session.to_public_dict(),
            "images": images,
        }

    def resolve_image_artifact(self, session_id: str, image_id: str) -> tuple[Path, str]:
        session = self.store.load(session_id)
        if not session:
            raise KeyError("session_not_found")
        return resolve_image_artifact_path(
            session_dir=self.store.session_dir(session_id),
            image_id=image_id,
        )

    async def run_turn_request(self, *, session_id: str, turn_id: str) -> None:
        session = self.store.load(session_id)
        if session:
            session.metadata[_WORKER_PID_KEY] = os.getpid()
            session.metadata[_WORKER_STARTED_AT_KEY] = utcnow_iso()
            session.updated_at = utcnow_iso()
            session.last_active_at = session.updated_at
            self.store.save(session)
        request = self.store.load_turn_request(session_id, turn_id)
        if not request:
            raise AgentError("主会话缺少 turn request，无法继续执行", error_code="turn_request_missing")
        await self._run_turn(
            session_id=session_id,
            turn_id=turn_id,
            content=str(request.get("content") or ""),
            attachments=list(request.get("attachments") or []),
            events_prelogged=bool(request.get("events_prelogged")),
        )

    async def _run_turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        content: str,
        attachments: list[Any],
        events_prelogged: bool = False,
    ) -> None:
        session = self.store.load(session_id)
        if not session:
            return

        config = self._reload_config()
        self.agent_runner.config = config
        connection = dict((session.metadata or {}).get("connection_snapshot") or {})
        decision = resolve_main_session_route(
            connection=connection,
            config=config,
            display_model=session.display_model,
            current_runtime_family=session.runtime_family,
        )
        context = RuntimeExecutionContext(
            scope=RUNTIME_SCOPE_MAIN_SESSION,
            role="assistant",
            step_name=turn_id,
            project_root=str(self.project_root),
            resume_token=session.resume_token,
        )
        event_store = self.store.event_store(session_id)
        event_sequence = int(session.latest_event_seq or 0) + 1
        used_context_replay = bool((session.metadata or {}).get(_CONTEXT_REPLAY_KEY))
        background_state_before = self._load_background_task_state(session_id)
        background_pending_before = str(
            ((background_state_before.get("pending") or {}).get("proposal_id") or "")
        ).strip()

        def append_event(event_dict: dict[str, Any]) -> None:
            nonlocal event_sequence, session
            event_store.append_unified(_dict_to_unified_event(event_dict))
            self.store.append_raw_log(session_id, json.dumps(event_dict, ensure_ascii=False) + "\n")
            session.latest_event_seq = max(session.latest_event_seq, int(event_dict.get("sequence") or 0))
            self._publish_event(session_id, self._public_event(session_id, event_dict))

        def append_assistant_text(text: str) -> None:
            nonlocal event_sequence
            append_event(
                {
                    "sequence": event_sequence,
                    "timestamp": utcnow_iso(),
                    "scope": RUNTIME_SCOPE_MAIN_SESSION,
                    "runtime_family": session.runtime_family,
                    "role": "assistant",
                    "event_type": "message",
                    "event_name": "assistant_text",
                    "payload": {"text": text},
                    "task_id": "",
                    "step_name": turn_id,
                    "session_id": session_id,
                }
            )
            event_sequence += 1

        def complete_turn_with_text(text: str, *, extra_notices: list[str] | None = None) -> None:
            nonlocal event_sequence, session
            session.status = "idle"
            session.current_turn_status = "completed"
            session.updated_at = utcnow_iso()
            session.last_active_at = session.updated_at
            session.last_error = ""
            session.last_error_code = ""
            session.metadata.pop(_WORKER_PID_KEY, None)
            session.metadata.pop(_WORKER_STARTED_AT_KEY, None)
            session.metadata["last_result"] = text
            append_assistant_text(text)
            for notice in extra_notices or []:
                if str(notice or "").strip():
                    append_assistant_text(str(notice).strip())
            append_event(
                build_lifecycle_event(
                    sequence=event_sequence,
                    decision=decision,
                    context=context,
                    event_name="completed",
                    payload={
                        "turn_id": turn_id,
                        "selected_model": session.display_model,
                        "native_session_id": session.native_session_id,
                    },
                    session_id=session_id,
                ).to_dict()
            )

        if not events_prelogged:
            append_event(
                {
                    "sequence": event_sequence,
                    "timestamp": utcnow_iso(),
                    "scope": RUNTIME_SCOPE_MAIN_SESSION,
                    "runtime_family": session.runtime_family,
                    "role": "user",
                    "event_type": "message",
                    "event_name": "user_text",
                    "payload": {"text": content, "attachments": attachments},
                    "task_id": "",
                    "step_name": turn_id,
                    "session_id": session_id,
                }
            )
            event_sequence += 1
            append_event(
                build_lifecycle_event(
                    sequence=event_sequence,
                    decision=decision,
                    context=context,
                    event_name="turn_started",
                    payload={"turn_id": turn_id},
                    session_id=session_id,
                ).to_dict()
            )
            event_sequence += 1

        design_root = Path(
            (session.metadata or {}).get("project_root")
            or session.cwd
            or self.project_root
        )
        direct_reply = self._handle_pending_background_task_reply(
            session=session,
            content=content,
            config=config,
        )
        if direct_reply is None:
            direct_reply = self._handle_pending_design_adoption_reply(
                session=session,
                project_root=design_root,
                content=content,
            )
        if direct_reply is not None:
            complete_turn_with_text(direct_reply)
            self.store.save(session)
            await self._continue_pending_inputs(session_id)
            self._turn_tasks.pop(session_id, None)
            return

        interception = InteractionDesignInterception()
        if bool(config.get("interaction_design_main_session_intercept", False)):
            interception = await self.interaction_design.maybe_intercept(
                session=session,
                session_dir=self.store.session_dir(session_id),
                project_root=design_root,
                content=content,
                attachments=attachments,
            )
        if interception.session_changed:
            session.updated_at = utcnow_iso()
            session.last_active_at = session.updated_at
            self.store.save(session)

        if interception.handled and not interception.continue_to_runtime:
            complete_turn_with_text(
                interception.assistant_text,
                extra_notices=self._collect_post_turn_notices(
                    session=session,
                    session_id=session_id,
                    project_root=design_root,
                    background_pending_before=background_pending_before,
                ),
            )
            self.store.save(session)
            await self._continue_pending_inputs(session_id)
            self._turn_tasks.pop(session_id, None)
            return

        persistent_runtime = self._persistent_runtimes.get(session_id)
        persistent_alive = bool(persistent_runtime and persistent_runtime.is_alive())
        prompt_attachments = _materialize_main_session_attachments(
            self.store.session_dir(session_id),
            turn_id,
            attachments,
        )
        prompt = self._build_turn_prompt(
            session,
            content=content,
            attachments=prompt_attachments,
            design_context=interception.design_context,
            include_startup_protocol=not persistent_alive or used_context_replay,
        )

        def handle_stream(raw_event: dict) -> None:
            nonlocal event_sequence, session
            image_artifact = capture_image_generation_artifact(
                session_dir=self.store.session_dir(session_id),
                session_id=session_id,
                turn_id=turn_id,
                raw_event=raw_event,
                requested_prompt=content,
            )
            raw_for_log = scrub_image_generation_event(raw_event) if is_image_generation_event(raw_event) else raw_event
            self.store.append_raw_log(session_id, json.dumps(raw_for_log, ensure_ascii=False) + "\n")
            event_store.append_raw(raw_for_log)
            session = self._apply_runtime_stream_state(session, raw_for_log)
            if image_artifact:
                append_event(
                    {
                        "sequence": event_sequence,
                        "timestamp": utcnow_iso(),
                        "scope": RUNTIME_SCOPE_MAIN_SESSION,
                        "runtime_family": session.runtime_family,
                        "role": "assistant",
                        "event_type": "message",
                        "event_name": IMAGE_ARTIFACT_EVENT_NAME,
                        "payload": image_artifact,
                        "task_id": "",
                        "step_name": turn_id,
                        "session_id": session_id,
                    }
                )
                event_sequence += 1
                return
            unified_events = build_unified_events(
                raw_for_log,
                sequence_start=event_sequence,
                decision=decision,
                context=context,
            )
            for item in unified_events:
                append_event(item.to_dict())
            event_sequence += len(unified_events)

        try:
            if self._persistent_runtime_enabled(config) and self._session_supports_persistent_runtime(session):
                runtime = await self._ensure_persistent_runtime(
                    session=session,
                    connection=connection,
                    config=config,
                )
                result = await runtime.run_turn(
                    session=session,
                    decision=decision,
                    prompt=prompt,
                    connection=connection,
                    on_stream_event=handle_stream,
                )
            else:
                adapter = self.registry.get_session(session.runtime_family)
                result = await adapter.run(
                    decision=decision,
                    session=session,
                    turn_id=turn_id,
                    prompt=prompt,
                    connection=connection,
                    on_stream_event=handle_stream,
                    on_raw_output=lambda text: self.store.append_raw_log(session_id, text),
                )
        except Exception as error:
            session = self.store.load(session_id) or session
            error_message = str(error)
            error_code = getattr(error, "error_code", "")
            pending_at_failure = self._normalize_pending_interaction(session.pending_interaction)
            dropped_pending_count = self._discard_pending_inputs(session)
            visible_error_message = self._format_turn_error_message(
                error_message,
                dropped_pending_count=dropped_pending_count,
            )
            if pending_at_failure and error_code in {"E102", "E103"}:
                pending_label = str(
                    pending_at_failure.get("summary")
                    or pending_at_failure.get("command")
                    or pending_at_failure.get("tool_name")
                    or pending_at_failure.get("request_id")
                    or ""
                ).strip()
                if pending_label:
                    visible_error_message = f"{visible_error_message}（卡在交互确认：{pending_label}）"
            session.status = "error"
            session.current_turn_status = "failed"
            session.pending_interaction = {}
            session.last_error = visible_error_message
            session.last_error_code = error_code
            session.updated_at = utcnow_iso()
            session.last_active_at = session.updated_at
            session.metadata.pop(_WORKER_PID_KEY, None)
            session.metadata.pop(_WORKER_STARTED_AT_KEY, None)
            session.metadata["last_turn_error"] = error_message
            if pending_at_failure:
                session.metadata["last_pending_interaction"] = {
                    **pending_at_failure,
                    "status": "expired",
                    "expired_at": session.updated_at,
                }
            runtime = self._persistent_runtimes.get(session_id)
            if runtime:
                await self._shutdown_persistent_runtime(session_id)
                self._clear_persistent_runtime_metadata(session)
            append_event(
                build_lifecycle_event(
                    sequence=event_sequence,
                    decision=decision,
                    context=context,
                    event_name="error",
                    payload={
                        "message": visible_error_message,
                        "error_code": error_code,
                    },
                    session_id=session_id,
                ).to_dict()
            )
            event_sequence += 1
            self.store.save(session)
            return
        finally:
            self._turn_tasks.pop(session_id, None)

        session = self.store.load(session_id) or session
        session.status = "idle"
        session.current_turn_status = "completed"
        session.pending_interaction = {}
        session.updated_at = utcnow_iso()
        session.last_active_at = session.updated_at
        session.resume_token = str(result.get("resume_token") or session.resume_token or "")
        session.native_session_id = str(result.get("native_session_id") or session.native_session_id or "")
        session.provider_model = str(result.get("provider_model") or session.provider_model or "")
        session.display_model = str(result.get("selected_model") or session.display_model or "")
        session.last_error = ""
        session.last_error_code = ""
        session.metadata.pop(_WORKER_PID_KEY, None)
        session.metadata.pop(_WORKER_STARTED_AT_KEY, None)
        session.metadata["last_result"] = str(result.get("result") or "")
        session.metadata["last_usage"] = dict(result.get("usage") or {})
        session.metadata[_RUNTIME_HEARTBEAT_KEY] = utcnow_iso()
        runtime = self._persistent_runtimes.get(session_id)
        if runtime and runtime.is_alive():
            runtime_meta = runtime.snapshot_metadata()
            for key, value in runtime_meta.items():
                if value:
                    session.metadata[key] = value
        if used_context_replay:
            session.metadata.pop(_CONTEXT_REPLAY_KEY, None)
        if result.get("_append_assistant_text") and str(result.get("result") or "").strip():
            append_assistant_text(str(result.get("result") or ""))
        for notice in self._collect_post_turn_notices(
            session=session,
            session_id=session_id,
            project_root=design_root,
            background_pending_before=background_pending_before,
        ):
            if str(notice or "").strip():
                append_assistant_text(str(notice).strip())
        append_event(
            build_lifecycle_event(
                sequence=event_sequence,
                decision=decision,
                context=context,
                event_name="completed",
                payload={
                    "turn_id": turn_id,
                    "selected_model": session.display_model,
                    "native_session_id": session.native_session_id,
                },
                session_id=session_id,
            ).to_dict()
        )
        self.store.save(session)
        await self._continue_pending_inputs(session_id)

    def _repair_orphaned_running_sessions(self) -> None:
        now = datetime.now(timezone.utc)
        for session in self.store.list():
            if session.session_id not in self._persistent_runtimes:
                runtime_cleaned = self._terminate_orphaned_persistent_runtime(session)
                if runtime_cleaned and session.status != "running":
                    self.store.save(session)
            if session.status != "running":
                dropped_pending_count = self._discard_stale_pending_inputs(session.session_id, session)
                if dropped_pending_count and (
                    session.status in {"error", "interrupted"}
                    or session.current_turn_status in {"failed", "interrupted"}
                ):
                    session.last_error = self._format_turn_error_message(
                        session.last_error or "主会话存在上一轮遗留的排队输入，已停止自动继续。",
                        dropped_pending_count=dropped_pending_count,
                    )
                    session.updated_at = utcnow_iso()
                    session.last_active_at = session.updated_at
                    self.store.save(session)
                continue
            if session.session_id in self._turn_tasks:
                continue
            worker_pid = _coerce_pid((session.metadata or {}).get(_WORKER_PID_KEY))
            if worker_pid and _main_session_worker_alive(worker_pid):
                continue
            if not worker_pid and _started_recently(session.current_turn_started_at, now=now):
                continue
            if self._recover_completed_claude_turn_from_transcript(session):
                continue
            dropped_pending_count = self._discard_pending_inputs(session)
            session.status = "interrupted"
            session.current_turn_status = "interrupted"
            interruption_message = (
                session.last_error
                or "主会话正在执行的 turn 因服务重启或 worker 退出而中断，可重新激活该会话继续发送消息。"
            )
            session.last_error = self._format_turn_error_message(
                interruption_message,
                dropped_pending_count=dropped_pending_count,
            )
            session.last_error_code = session.last_error_code or _WORKER_ERROR_CODE
            session.pending_interaction = {}
            session.updated_at = utcnow_iso()
            session.last_active_at = session.updated_at
            session.metadata.pop(_WORKER_PID_KEY, None)
            session.metadata.pop(_WORKER_STARTED_AT_KEY, None)
            self._clear_persistent_runtime_metadata(session)
            self.store.save(session)

    def _recover_completed_claude_turn_from_transcript(self, session: MainSessionRecord) -> bool:
        if session.runtime_family != RUNTIME_FAMILY_CLAUDE_CODE:
            return False
        native_session_id = str(session.native_session_id or session.resume_token or "").strip()
        if not native_session_id:
            return False
        transcript_path_text = str((session.metadata or {}).get(_RUNTIME_NATIVE_PATH_KEY) or "").strip()
        transcript_path = Path(transcript_path_text) if transcript_path_text else Path()
        if not transcript_path_text or not transcript_path.exists():
            transcript_path = _resolve_claude_transcript_path(native_session_id)
        if not transcript_path.exists():
            return False

        event_store = self.store.event_store(session.session_id)
        recorded_message_keys: set[str] = set()
        for item in event_store.iter_lines(event_store.raw_events_path):
            raw_event = item.get("event") if isinstance(item.get("event"), dict) else {}
            if not isinstance(raw_event, dict) or str(raw_event.get("type") or "") != "assistant":
                continue
            message = raw_event.get("message", {}) if isinstance(raw_event.get("message"), dict) else {}
            recorded_message_keys.add(_claude_message_event_key(message))

        config = self.agent_runner.config if isinstance(self.agent_runner.config, dict) else {}
        connection = dict((session.metadata or {}).get("connection_snapshot") or {})
        decision = resolve_main_session_route(
            connection=connection,
            config=config,
            display_model=session.display_model,
            current_runtime_family=session.runtime_family,
        )
        context = RuntimeExecutionContext(
            scope=RUNTIME_SCOPE_MAIN_SESSION,
            role="assistant",
            step_name=session.current_turn_id,
            project_root=str(self.project_root),
            resume_token=session.resume_token,
        )
        next_sequence = int(session.latest_event_seq or 0) + 1
        completed_message: dict[str, Any] | None = None
        appended_events: list[dict[str, Any]] = []
        turn_started_at = _parse_iso_timestamp(session.current_turn_started_at)

        try:
            handle = transcript_path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            return False
        with handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(payload.get("sessionId") or "") != native_session_id:
                    continue
                payload_timestamp = _extract_runtime_event_timestamp(payload)
                if (
                    turn_started_at is not None
                    and payload_timestamp is not None
                    and payload_timestamp < (turn_started_at - TURN_EVENT_TIMESTAMP_SKEW_SECONDS)
                ):
                    continue
                if str(payload.get("type") or "") != "assistant":
                    continue
                message = payload.get("message", {}) if isinstance(payload.get("message"), dict) else {}
                message_key = _claude_message_event_key(message)
                if message_key in recorded_message_keys:
                    continue
                raw_event = {"type": "assistant", "message": message}
                self.store.append_raw_log(session.session_id, json.dumps(raw_event, ensure_ascii=False) + "\n")
                event_store.append_raw(raw_event)
                for item in build_unified_events(
                    raw_event,
                    sequence_start=next_sequence,
                    decision=decision,
                    context=context,
                ):
                    event_dict = item.to_dict()
                    event_dict["session_id"] = event_dict.get("session_id") or session.session_id
                    event_dict["step_name"] = event_dict.get("step_name") or session.current_turn_id
                    appended_events.append(event_dict)
                    next_sequence += 1
                recorded_message_keys.add(message_key)
                if str(message.get("stop_reason") or "") == "end_turn" and _extract_claude_text(message):
                    completed_message = message
                    break

        if not completed_message:
            self._append_session_events(session, appended_events)
            self.store.save(session)
            return False

        usage = dict(completed_message.get("usage") or {})
        provider_model = str(
            completed_message.get("model")
            or session.provider_model
            or decision.provider_model
            or decision.selected_model
            or ""
        ).strip()
        assistant_text = _extract_claude_text(completed_message)
        session.status = "idle"
        session.current_turn_status = "completed"
        session.pending_interaction = {}
        session.updated_at = utcnow_iso()
        session.last_active_at = session.updated_at
        session.native_session_id = native_session_id
        session.resume_token = native_session_id
        session.provider_model = provider_model or session.provider_model
        session.display_model = session.display_model or decision.display_model or decision.selected_model
        session.last_error = ""
        session.last_error_code = ""
        session.metadata.pop(_WORKER_PID_KEY, None)
        session.metadata.pop(_WORKER_STARTED_AT_KEY, None)
        session.metadata["last_result"] = assistant_text
        session.metadata["last_usage"] = usage
        session.metadata[_RUNTIME_HEARTBEAT_KEY] = utcnow_iso()
        self._clear_persistent_runtime_metadata(session)
        appended_events.append(
            build_lifecycle_event(
                sequence=next_sequence,
                decision=decision,
                context=context,
                event_name="completed",
                payload={
                    "turn_id": session.current_turn_id,
                    "selected_model": session.display_model,
                    "native_session_id": session.native_session_id,
                    "recovered": True,
                },
                session_id=session.session_id,
            ).to_dict()
        )
        self._append_session_events(session, appended_events)
        self.store.save(session)
        return True

    @staticmethod
    def _detached_workers_enabled(config: dict | None = None) -> bool:
        if config is None:
            return True
        return bool(config.get("main_session_detached_worker", True))

    @staticmethod
    def _persistent_runtime_enabled(config: dict | None = None) -> bool:
        if config is None:
            return True
        return bool(config.get("main_session_persistent_runtime", True))

    async def _spawn_turn_worker(self, *, session_id: str, turn_id: str) -> int:
        env = dict(os.environ)
        env["VIZO_HOME"] = str(self.project_root)
        env["OPUS_HOME"] = str(self.project_root)
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            _WORKER_MODULE,
            "--project-root",
            str(self.project_root),
            "--session-id",
            session_id,
            "--turn-id",
            turn_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(self.project_root),
            env=env,
            start_new_session=True,
        )
        return int(proc.pid)

    def _publish_event(self, session_id: str, payload: dict) -> None:
        for queue in list(self._event_subscribers.get(session_id, set())):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(payload)
                except asyncio.QueueFull:
                    pass

    @staticmethod
    def _pending_inputs(session: MainSessionRecord) -> list[dict[str, Any]]:
        items = (session.metadata or {}).get(_PENDING_INPUTS_KEY)
        if not isinstance(items, list):
            return []
        return [dict(item) for item in items if isinstance(item, dict)]

    def _store_pending_inputs(self, session: MainSessionRecord, items: list[dict[str, Any]]) -> None:
        if items:
            session.metadata[_PENDING_INPUTS_KEY] = items
        else:
            session.metadata.pop(_PENDING_INPUTS_KEY, None)

    @staticmethod
    def _pending_input_index(queue: list[dict[str, Any]], queue_id: str) -> int:
        target = str(queue_id or "").strip()
        if not target:
            return -1
        for index, item in enumerate(queue):
            current = str((item or {}).get("queue_id") or "").strip()
            fallback = f"pending_index_{index + 1}"
            if target in {current, fallback}:
                return index
        return -1

    def _discard_pending_inputs(self, session: MainSessionRecord) -> int:
        pending_count = len(self._pending_inputs(session))
        self._store_pending_inputs(session, [])
        if (session.metadata or {}).get(_PENDING_MODEL_SWITCH_KEY):
            session.metadata.pop(_PENDING_MODEL_SWITCH_KEY, None)
            pending_count += 1
        return pending_count

    def _discard_stale_pending_inputs(self, session_id: str, session: MainSessionRecord) -> int:
        if self._session_is_busy(session_id, session):
            return 0
        pending_count = self._discard_pending_inputs(session)
        if pending_count <= 0:
            return 0
        session.updated_at = utcnow_iso()
        session.last_active_at = session.updated_at
        self.store.save(session)
        return pending_count

    @staticmethod
    def _format_turn_error_message(message: str, *, dropped_pending_count: int = 0) -> str:
        base = str(message or "").strip() or "主会话执行失败。"
        if dropped_pending_count <= 0:
            return base
        separator = "" if base.endswith(("。", "！", "？", ".", "!", "?")) else "。"
        return (
            f"{base}{separator}后续 {dropped_pending_count} 条排队输入未执行，"
            "请确认问题后重新发送。"
        )

    def _enqueue_pending_input(
        self,
        session: MainSessionRecord,
        payload: dict[str, Any],
        *,
        status: str,
        message: str,
        extra_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        queue = self._pending_inputs(session)
        if len(queue) >= _MAX_PENDING_INPUTS:
            raise AgentError(
                "当前会话待处理输入过多，请等待当前队列消费后再发送。",
                error_code="session_queue_full",
            )
        item = dict(payload)
        item.setdefault("queue_id", "pending_" + uuid.uuid4().hex[:10])
        queue.append(item)
        self._store_pending_inputs(session, queue)
        session.updated_at = utcnow_iso()
        session.last_active_at = session.updated_at
        self.store.save(session)
        result = {
            "status": status,
            "queued": True,
            "queue_position": len(queue),
            "message": message,
            "session": session.to_public_dict(),
        }
        if extra_payload:
            result.update(extra_payload)
        return result

    def _session_reasoning_effort(self, session: MainSessionRecord) -> str:
        if session.runtime_family != RUNTIME_FAMILY_CODEX:
            return ""
        effort = str((session.metadata or {}).get(_REASONING_EFFORT_KEY) or "medium").strip()
        return effort if effort in _CODEX_REASONING_EFFORTS else "medium"

    def _target_reasoning_effort(
        self,
        session: MainSessionRecord,
        *,
        reasoning_effort: str | None,
        target_runtime_family: str,
    ) -> str:
        if target_runtime_family != RUNTIME_FAMILY_CODEX:
            return ""
        if reasoning_effort is None:
            return self._session_reasoning_effort(session)
        effort = str(reasoning_effort or "").strip()
        if not effort:
            return self._session_reasoning_effort(session)
        if effort not in _CODEX_REASONING_EFFORTS:
            raise AgentError(
                "Codex thinking depth must be one of low, medium, high, xhigh",
                error_code="invalid_reasoning_effort",
            )
        return effort

    def _store_reasoning_effort(self, session: MainSessionRecord, reasoning_effort: str) -> None:
        if session.runtime_family != RUNTIME_FAMILY_CODEX:
            session.metadata.pop(_REASONING_EFFORT_KEY, None)
            return
        effort = str(reasoning_effort or "").strip()
        session.metadata[_REASONING_EFFORT_KEY] = effort if effort in _CODEX_REASONING_EFFORTS else "medium"

    @staticmethod
    def _format_model_switch_target_label(
        *,
        current_model: str,
        target_model: str,
        current_reasoning_effort: str,
        target_reasoning_effort: str,
        include_unchanged_model: bool = False,
        include_unchanged_reasoning: bool = False,
    ) -> str:
        current_model = str(current_model or "").strip()
        target_model = str(target_model or "").strip()
        current_reasoning_effort = str(current_reasoning_effort or "").strip()
        target_reasoning_effort = str(target_reasoning_effort or "").strip()
        changed: list[str] = []
        if target_model and (target_model != current_model or include_unchanged_model):
            changed.append(f"模型 {target_model}")
        if target_reasoning_effort and (
            target_reasoning_effort != current_reasoning_effort or include_unchanged_reasoning
        ):
            changed.append(f"Codex 思考深度 {target_reasoning_effort}")
        if changed:
            return " 和 ".join(changed)
        if target_model and target_reasoning_effort:
            return f"模型 {target_model}、Codex 思考深度 {target_reasoning_effort}"
        if target_model:
            return f"模型 {target_model}"
        if target_reasoning_effort:
            return f"Codex 思考深度 {target_reasoning_effort}"
        return "当前模型设置"

    @staticmethod
    def _format_model_switch_compact_label(
        *,
        target_model: str,
        target_reasoning_effort: str,
    ) -> str:
        target_model = str(target_model or "").strip()
        target_reasoning_effort = str(target_reasoning_effort or "").strip()
        if target_model and target_reasoning_effort:
            return f"{target_model}·{target_reasoning_effort}"
        return target_model or target_reasoning_effort or "当前模型设置"

    def _format_model_switch_request_text(
        self,
        session: MainSessionRecord,
        *,
        target_model: str,
        target_reasoning_effort: str,
        current_reasoning_effort: str,
    ) -> str:
        label = self._format_model_switch_compact_label(
            target_model=target_model,
            target_reasoning_effort=target_reasoning_effort,
        )
        return f"切换模型 {label}"

    def _session_is_busy(self, session_id: str, session: MainSessionRecord) -> bool:
        turn_status = str(session.current_turn_status or session.status or "")
        return (
            session.status == "running"
            or session_id in self._turn_tasks
            or turn_status in {"running", "waiting_confirm", "waiting_interaction"}
        )

    def _session_accepts_control_command(self, session_id: str, session: MainSessionRecord) -> bool:
        return self._session_is_busy(session_id, session) or session_id in self._turn_tasks

    def _session_supports_persistent_runtime(self, session: MainSessionRecord) -> bool:
        try:
            adapter = self.registry.get_session(session.runtime_family)
        except Exception:
            return False
        return bool(getattr(adapter, "supports_persistent", False))

    async def _ensure_persistent_runtime(self, *, session: MainSessionRecord, connection: dict, config: dict) -> Any:
        runtime = self._persistent_runtimes.get(session.session_id)
        if runtime and (runtime.is_alive() or (getattr(runtime, "pid", 0) <= 0 and not getattr(runtime, "started_at", 0))):
            return runtime
        if runtime:
            await self._shutdown_persistent_runtime(session.session_id)
        runtime = build_persistent_runtime(
            runtime_family=session.runtime_family,
            session_id=session.session_id,
            project_root=self.project_root,
            config=config,
            raw_output_cb=lambda text: self.store.append_raw_log(session.session_id, text),
        )
        self._persistent_runtimes[session.session_id] = runtime
        self._ensure_runtime_housekeeping_task()
        return runtime

    @staticmethod
    def _signal_pid_group(pid: int, sig: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.killpg(os.getpgid(pid), sig)
            return True
        except (ProcessLookupError, OSError):
            try:
                os.kill(pid, sig)
                return True
            except (ProcessLookupError, OSError):
                return False

    async def _interrupt_worker_process(self, pid: int, *, force: bool = False) -> dict[str, Any]:
        first_signal = signal.SIGTERM if force else signal.SIGINT
        sent = self._signal_pid_group(pid, first_signal)
        killed = False
        if force and sent:
            for _ in range(10):
                if not _pid_alive(pid):
                    break
                await asyncio.sleep(0.1)
            if _pid_alive(pid):
                killed = self._signal_pid_group(pid, signal.SIGKILL)
        return {
            "mode": "force" if force else "soft",
            "signal": "sigterm" if force else "sigint",
            "pid": pid,
            "sent": sent,
            "killed": killed,
        }

    async def _shutdown_persistent_runtime(self, session_id: str) -> None:
        runtime = self._persistent_runtimes.pop(session_id, None)
        if runtime:
            await runtime.shutdown()
        session = self.store.load(session_id)
        if not session:
            return
        self._clear_persistent_runtime_metadata(session)
        session.updated_at = utcnow_iso()
        session.last_active_at = session.updated_at
        self.store.save(session)

    def _ensure_runtime_housekeeping_task(self) -> None:
        if self._runtime_housekeeping_task and not self._runtime_housekeeping_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._runtime_housekeeping_task = loop.create_task(self._runtime_housekeeping_loop())

    async def _runtime_housekeeping_loop(self) -> None:
        while True:
            try:
                config = self._reload_config()
                interval = int(config.get("main_session_persistent_housekeeping_interval", 60))
                await asyncio.sleep(max(interval, 1))
                config = self._reload_config()
                await self._reap_idle_persistent_runtimes(config=config)
            except asyncio.CancelledError:
                return
            except Exception:
                continue

    async def _reap_idle_persistent_runtimes(self, *, config: dict | None = None) -> None:
        config = config or self._reload_config()
        idle_limit = int(config.get("main_session_persistent_idle_terminate", 1800))
        now = datetime.now(timezone.utc)
        for session_id, runtime in list(self._persistent_runtimes.items()):
            session = self.store.load(session_id)
            if not session:
                await self._shutdown_persistent_runtime(session_id)
                continue
            if session.status == "running" or session_id in self._turn_tasks:
                continue
            if self._pending_inputs(session):
                continue
            if not runtime.is_alive():
                await self._shutdown_persistent_runtime(session_id)
                continue
            if not session.last_active_at:
                continue
            try:
                last_active = datetime.fromisoformat(str(session.last_active_at).replace("Z", "+00:00"))
            except ValueError:
                continue
            if last_active.tzinfo is None:
                last_active = last_active.replace(tzinfo=timezone.utc)
            if (now - last_active).total_seconds() >= idle_limit:
                await self._shutdown_persistent_runtime(session_id)

    def _append_control_exchange(self, session: MainSessionRecord, *, user_text: str, assistant_text: str) -> None:
        events: list[dict[str, Any]] = []
        next_sequence = int(session.latest_event_seq or 0) + 1
        now = utcnow_iso()
        events.append(
            {
                "sequence": next_sequence,
                "timestamp": now,
                "scope": RUNTIME_SCOPE_MAIN_SESSION,
                "runtime_family": session.runtime_family,
                "role": "user",
                "event_type": "message",
                "event_name": "user_text",
                "payload": {"text": user_text, "attachments": []},
                "task_id": "",
                "step_name": "control",
                "session_id": session.session_id,
            }
        )
        next_sequence += 1
        events.append(
            {
                "sequence": next_sequence,
                "timestamp": utcnow_iso(),
                "scope": RUNTIME_SCOPE_MAIN_SESSION,
                "runtime_family": session.runtime_family,
                "role": "assistant",
                "event_type": "message",
                "event_name": "assistant_text",
                "payload": {"text": assistant_text},
                "task_id": "",
                "step_name": "control",
                "session_id": session.session_id,
            }
        )
        self._append_session_events(session, events)

    def _append_turn_start_events(
        self,
        session: MainSessionRecord,
        *,
        turn_id: str,
        content: str,
        attachments: list[Any],
        config: dict[str, Any] | None = None,
    ) -> None:
        config = config if isinstance(config, dict) else self.agent_runner.config
        connection = dict((session.metadata or {}).get("connection_snapshot") or {})
        decision = resolve_main_session_route(
            connection=connection,
            config=config if isinstance(config, dict) else {},
            display_model=session.display_model,
            current_runtime_family=session.runtime_family,
        )
        context = RuntimeExecutionContext(
            scope=RUNTIME_SCOPE_MAIN_SESSION,
            role="assistant",
            step_name=turn_id,
            project_root=str(self.project_root),
            resume_token=session.resume_token,
        )
        next_sequence = int(session.latest_event_seq or 0) + 1
        now = utcnow_iso()
        self._append_session_events(
            session,
            [
                {
                    "sequence": next_sequence,
                    "timestamp": now,
                    "scope": RUNTIME_SCOPE_MAIN_SESSION,
                    "runtime_family": session.runtime_family,
                    "role": "user",
                    "event_type": "message",
                    "event_name": "user_text",
                    "payload": {"text": content, "attachments": attachments},
                    "task_id": "",
                    "step_name": turn_id,
                    "session_id": session.session_id,
                },
                build_lifecycle_event(
                    sequence=next_sequence + 1,
                    decision=decision,
                    context=context,
                    event_name="turn_started",
                    payload={"turn_id": turn_id},
                    session_id=session.session_id,
                ).to_dict(),
            ],
        )

    @staticmethod
    def _normalize_pending_interaction(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        request_id = str(payload.get("request_id") or "").strip()
        if not request_id:
            return {}

        result = dict(payload)
        result["request_id"] = request_id

        interaction_kind = str(payload.get("interaction_kind") or "").strip()
        if interaction_kind:
            result["interaction_kind"] = interaction_kind

        options: list[dict[str, Any]] = []
        available_actions: list[str] = []
        text_required_actions: list[str] = []
        seen_actions: set[str] = set()
        seen_text_actions: set[str] = set()

        for item in payload.get("options") or []:
            if isinstance(item, dict):
                action = str(item.get("id") or item.get("action") or "").strip()
                if not action or action in seen_actions:
                    continue
                seen_actions.add(action)
                requires_text = bool(item.get("requires_text"))
                option = dict(item)
                option["id"] = action
                option["requires_text"] = requires_text
                options.append(option)
                available_actions.append(action)
                if requires_text and action not in seen_text_actions:
                    seen_text_actions.add(action)
                    text_required_actions.append(action)
            else:
                action = str(item or "").strip()
                if not action or action in seen_actions:
                    continue
                seen_actions.add(action)
                options.append({"id": action, "requires_text": False})
                available_actions.append(action)

        for item in payload.get("available_actions") or []:
            action = str(item or "").strip()
            if not action or action in seen_actions:
                continue
            seen_actions.add(action)
            options.append({"id": action, "requires_text": False})
            available_actions.append(action)

        for item in payload.get("text_required_actions") or []:
            action = str(item or "").strip()
            if not action or action in seen_text_actions:
                continue
            seen_text_actions.add(action)
            text_required_actions.append(action)

        result["options"] = options
        result["available_actions"] = available_actions
        result["text_required_actions"] = text_required_actions
        result["requires_text"] = bool(payload.get("requires_text"))
        return result

    def _apply_runtime_stream_state(
        self,
        session: MainSessionRecord,
        raw_event: dict[str, Any],
    ) -> MainSessionRecord:
        event_type = str(raw_event.get("type") or "").strip()
        if event_type in {"thread.started", "turn.started"}:
            native_session_id = str(raw_event.get("thread_id") or "").strip()
            if native_session_id:
                session.native_session_id = native_session_id
                session.resume_token = native_session_id
                session.status = "running"
                session.current_turn_status = session.current_turn_status or "running"
                session.updated_at = utcnow_iso()
                session.last_active_at = session.updated_at
                self.store.save(session)
            return session
        if event_type == "system" and str(raw_event.get("subtype") or "").strip() == "init":
            native_session_id = str(raw_event.get("session_id") or "").strip()
            if native_session_id:
                session.native_session_id = native_session_id
                session.resume_token = native_session_id
                session.status = "running"
                session.current_turn_status = session.current_turn_status or "running"
                session.updated_at = utcnow_iso()
                session.last_active_at = session.updated_at
                self.store.save(session)
            return session
        if event_type != "interaction_requested":
            return session
        pending = self._normalize_pending_interaction(raw_event.get("payload"))
        if not pending:
            return session
        session.pending_interaction = pending
        session.status = "running"
        session.current_turn_status = "waiting_interaction"
        session.updated_at = utcnow_iso()
        session.last_active_at = session.updated_at
        self.store.save(session)
        return session

    @staticmethod
    def _interaction_action_message(action: str) -> str:
        action = str(action or "").strip()
        if action == "approve_once":
            return "已提交本次交互请求的确认结果，主会话继续运行。"
        if action == "approve_prefix":
            return "已提交同前缀放行结果，主会话继续运行。"
        if action == "approve_session":
            return "已提交本会话放行结果，主会话继续运行。"
        if action == "approve_always":
            return "已提交长期放行结果，主会话继续运行。"
        if action == "deny":
            return "已提交拒绝结果。"
        if action == "deny_with_feedback":
            return "已提交拒绝结果，并附带文本说明。"
        return "交互请求已提交。"

    @staticmethod
    def _clear_persistent_runtime_metadata(session: MainSessionRecord) -> None:
        session.metadata.pop(_RUNTIME_PID_KEY, None)
        session.metadata.pop(_RUNTIME_STARTED_AT_KEY, None)
        session.metadata.pop(_RUNTIME_HEARTBEAT_KEY, None)
        session.metadata.pop(_RUNTIME_NATIVE_PATH_KEY, None)

    def _terminate_orphaned_persistent_runtime(self, session: MainSessionRecord) -> bool:
        runtime_pid = _coerce_pid((session.metadata or {}).get(_RUNTIME_PID_KEY))
        if not runtime_pid:
            return False
        if _pid_alive(runtime_pid):
            try:
                os.kill(runtime_pid, signal.SIGTERM)
            except OSError:
                pass
        self._clear_persistent_runtime_metadata(session)
        return True

    def _append_session_events(self, session: MainSessionRecord, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        event_store = self.store.event_store(session.session_id)
        for event_dict in events:
            event_store.append_unified(_dict_to_unified_event(event_dict))
            self.store.append_raw_log(session.session_id, json.dumps(event_dict, ensure_ascii=False) + "\n")
            session.latest_event_seq = max(session.latest_event_seq, int(event_dict.get("sequence") or 0))
            self._publish_event(session.session_id, self._public_event(session.session_id, event_dict))

    def _apply_model_switch(
        self,
        session: MainSessionRecord,
        *,
        target_model: str,
        provider_model: str,
        reasoning_effort: str,
        decision_data: dict[str, Any] | None,
        raw_command: str,
        assistant_text: str,
    ) -> None:
        session.display_model = target_model
        session.provider_model = provider_model or session.provider_model or target_model
        self._store_reasoning_effort(session, reasoning_effort)
        session.resume_token = ""
        session.native_session_id = ""
        session.updated_at = utcnow_iso()
        session.last_active_at = session.updated_at
        if decision_data:
            session.metadata["decision"] = decision_data
        session.metadata.pop(_PENDING_MODEL_SWITCH_KEY, None)
        session.metadata[_CONTEXT_REPLAY_KEY] = {
            "target_model": target_model,
            "reasoning_effort": self._session_reasoning_effort(session),
            "applied_at": session.updated_at,
        }
        self._append_control_exchange(session, user_text=raw_command, assistant_text=assistant_text)
        self.store.save(session)

    async def _continue_pending_inputs(self, session_id: str) -> None:
        while True:
            session = self.store.load(session_id)
            if not session:
                return
            queue = self._pending_inputs(session)
            if not queue and (session.metadata or {}).get(_PENDING_MODEL_SWITCH_KEY):
                pending = dict((session.metadata or {}).get(_PENDING_MODEL_SWITCH_KEY) or {})
                queue = [
                    {
                        "kind": "model_switch",
                        "display_model": str(pending.get("display_model") or "").strip(),
                        "provider_model": str(pending.get("provider_model") or ""),
                        "reasoning_effort": str(pending.get("reasoning_effort") or ""),
                        "raw_command": str(pending.get("raw_command") or ""),
                        "decision": pending.get("decision") if isinstance(pending.get("decision"), dict) else None,
                    }
                ]
                session.metadata.pop(_PENDING_MODEL_SWITCH_KEY, None)
            if not queue:
                return

            current = dict(queue.pop(0))
            self._store_pending_inputs(session, queue)
            session.updated_at = utcnow_iso()
            session.last_active_at = session.updated_at
            self.store.save(session)

            if current.get("kind") == "model_switch":
                target_model = str(current.get("display_model") or "").strip()
                if not target_model:
                    continue
                current_reasoning_effort = self._session_reasoning_effort(session)
                target_reasoning_effort = str(current.get("reasoning_effort") or current_reasoning_effort)
                label = self._format_model_switch_compact_label(
                    target_model=target_model,
                    target_reasoning_effort=target_reasoning_effort,
                )
                self._apply_model_switch(
                    session,
                    target_model=target_model,
                    provider_model=str(current.get("provider_model") or session.provider_model or target_model),
                    reasoning_effort=target_reasoning_effort,
                    decision_data=current.get("decision") if isinstance(current.get("decision"), dict) else None,
                    raw_command=str(current.get("raw_command") or f"/model {target_model}"),
                    assistant_text=f"成功切换为{label}",
                )
                await self._shutdown_persistent_runtime(session_id)
                continue

            if current.get("kind") == "message":
                await self.submit_message(
                    session_id,
                    content=str(current.get("content") or ""),
                    attachments=list(current.get("attachments") or []),
                )
                return

    def _build_turn_prompt(
        self,
        session: MainSessionRecord,
        *,
        content: str,
        attachments: list[str],
        design_context: str = "",
        include_startup_protocol: bool = True,
    ) -> str:
        prompt = _build_main_session_prompt(content, attachments)
        current_input_block = "[当前用户输入]\n" + prompt
        if str(design_context or "").strip():
            current_input_block = (
                "[Vizo 交互设计上下文]\n"
                + str(design_context).strip()
                + "\n\n"
                + current_input_block
            )
        prompt = current_input_block
        if include_startup_protocol:
            startup_context = resolve_startup_protocol_context(
                project=str((session.metadata or {}).get("project_name") or ""),
                cwd=str(session.cwd or self.project_root),
                scope=MAIN_SESSION_SCOPE,
                config=self.agent_runner.config,
                fallback_root=(session.metadata or {}).get("project_root") or self.project_root,
            )
            protocol = build_main_session_protocol(startup_context, repo_root=self.project_root)
            if protocol:
                prompt = protocol + "\n\n" + current_input_block
        replay = dict((session.metadata or {}).get(_CONTEXT_REPLAY_KEY) or {})
        if not replay:
            return prompt
        history = self._build_replay_history(session.session_id)
        if not history:
            return prompt
        target_model = str(replay.get("target_model") or session.display_model or "").strip()
        lines = [
            "你正在继续一个已有的 Vizo 主会话。",
            "由于用户刚执行了 /model 切换，本轮不会复用旧的 CLI native session。",
        ]
        if target_model:
            lines.append(f"当前目标模型：{target_model}")
        reasoning_effort = str(replay.get("reasoning_effort") or "").strip()
        if reasoning_effort:
            lines.append(f"当前 Codex 思考深度：{reasoning_effort}")
        lines.append("以下内容来自 unified events 的最近会话事实，请在此基础上继续：")
        lines.extend(history)
        lines.append("下面开始处理本轮新的用户输入。")
        return "[Vizo 会话上下文恢复]\n" + "\n".join(lines) + "\n\n[Vizo 新输入]\n" + prompt

    def _build_replay_history(self, session_id: str) -> list[str]:
        items = self.store.read_events(session_id, after_seq=0, limit=1000)
        relevant: list[str] = []
        for item in items:
            if str(item.get("step_name") or "") == "control":
                continue
            event_name = str(item.get("event_name") or "")
            payload = item.get("payload", {}) if isinstance(item.get("payload"), dict) else {}
            if event_name == "user_text":
                text = str(payload.get("text") or "").strip()
                if not text or text.startswith("/model "):
                    continue
                relevant.append("[User] " + _truncate_for_replay(text))
            elif event_name == "assistant_text":
                text = str(payload.get("text") or "").strip()
                if not text or text.startswith("已切换到 ") or text.startswith("当前已经在使用 "):
                    continue
                relevant.append("[Assistant] " + _truncate_for_replay(text))
            elif event_name == IMAGE_ARTIFACT_EVENT_NAME:
                text = str(payload.get("revised_prompt") or payload.get("prompt") or "").strip()
                if text:
                    relevant.append("[AssistantImage] " + _truncate_for_replay(text))
            elif event_name == "code_edit":
                text = str(payload.get("summary") or payload.get("diff") or "").strip()
                if text:
                    relevant.append("[CodeEdit] " + _truncate_for_replay(text))
            elif event_name == "runtime_error":
                text = str(payload.get("message") or "").strip()
                if text:
                    relevant.append("[Error] " + _truncate_for_replay(text))
        return relevant[-_MAX_REPLAY_ITEMS:]

    @staticmethod
    def _public_event(session_id: str, event: dict[str, Any]) -> dict[str, Any]:
        payload = event.get("payload", {}) if isinstance(event.get("payload"), dict) else {}
        seq = int(event.get("sequence") or 0)
        event_name = str(event.get("event_name") or "")
        event_type = str(event.get("event_type") or "")
        text = str(payload.get("text") or "")
        kind = event_name or event_type or "event"
        role = str(event.get("role") or "")
        if event_name == "user_text":
            role = "user"
        elif event_name in {"assistant_text", "thinking", IMAGE_ARTIFACT_EVENT_NAME}:
            role = "assistant"
        elif event_name in {"tool_call", "tool_result", "work_progress"}:
            role = "tool"
        return {
            "event_id": f"evt_{session_id}_{seq:06d}",
            "session_id": session_id,
            "seq": seq,
            "kind": kind,
            "runtime_kind": event.get("runtime_family", ""),
            "runtime_family": event.get("runtime_family", ""),
            "role": role,
            "text": text,
            "payload": payload,
            "event_type": event_type,
            "event_name": event_name,
            "ts": event.get("timestamp", ""),
        }


def _build_main_session_prompt(content: str, attachments: list[str]) -> str:
    prompt = str(content or "").strip()
    if not attachments:
        return prompt
    lines = [prompt, "", "附加文件："]
    lines.extend(f"- {item}" for item in attachments if str(item).strip())
    return "\n".join(lines).strip()


def _attachment_meta(value: str) -> dict[str, str]:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    meta: dict[str, str] = {}
    for line in text.splitlines()[:16]:
        key, separator, raw_value = line.partition("：")
        if not separator:
            continue
        key = key.strip()
        if key in {"文件", "大小", "类型"}:
            meta[key] = raw_value.strip()
    return meta


def _format_bytes(value: int) -> str:
    size = float(max(0, int(value or 0)))
    units = ["B", "KB", "MB", "GB"]
    unit_index = 0
    while size >= 1024 and unit_index < len(units) - 1:
        size = size / 1024
        unit_index += 1
    if unit_index == 0:
        return f"{int(size)} {units[unit_index]}"
    return f"{size:.1f} {units[unit_index]}"


def _attachment_prompt_reference(item: dict[str, Any]) -> str:
    name = str(item.get("name") or item.get("filename") or item.get("id") or "附件").strip()
    path = str(item.get("path") or item.get("local_path") or "").strip()
    size = item.get("size")
    file_type = str(item.get("type") or item.get("mime_type") or "").strip()
    details = [f"文件：{name}"]
    if path:
        details.append(f"路径：{path}")
    if size not in (None, ""):
        try:
            details.append(f"大小：{_format_bytes(int(size))}")
        except (TypeError, ValueError):
            details.append(f"大小：{size}")
    if file_type:
        details.append(f"类型：{file_type}")
    if path:
        details.append("附件内容已保存到上述路径，请按需读取该文件。")
    return "；".join(details)


def _safe_attachment_file_name(name: str, index: int) -> str:
    raw = str(name or "").strip() or f"attachment-{index + 1}.txt"
    cleaned = re.sub(r"[\\/:\*\?\"<>\|\x00-\x1f]+", "-", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-")
    if not cleaned:
        cleaned = f"attachment-{index + 1}.txt"
    if len(cleaned) > _ATTACHMENT_NAME_MAX_LEN:
        suffix = "".join(Path(cleaned).suffixes)
        stem = cleaned[: max(20, _ATTACHMENT_NAME_MAX_LEN - len(suffix))].rstrip(" .-")
        cleaned = (stem or f"attachment-{index + 1}") + suffix
    return f"{index + 1:02d}-{cleaned}"


def _materialize_main_session_attachments(
    session_dir: Path,
    turn_id: str,
    attachments: list[Any],
) -> list[str]:
    prepared: list[str] = []
    attachment_dir = Path(session_dir) / "runtime" / "attachments" / _safe_attachment_file_name(turn_id, 0)
    for index, item in enumerate(attachments or []):
        if isinstance(item, dict):
            reference = _attachment_prompt_reference(item).strip()
            if reference:
                prepared.append(reference)
            continue
        text = str(item or "").strip()
        if not text:
            continue
        meta = _attachment_meta(text)
        name = meta.get("文件") or f"attachment-{index + 1}.txt"
        size = meta.get("大小") or ""
        file_type = meta.get("类型") or ""
        try:
            attachment_dir.mkdir(parents=True, exist_ok=True)
            target = attachment_dir / _safe_attachment_file_name(name, index)
            target.write_text(text + "\n", encoding="utf-8")
            details = [f"文件：{name}", f"路径：{target}"]
            if size:
                details.append(f"大小：{size}")
            if file_type:
                details.append(f"类型：{file_type}")
            details.append("附件内容已保存到上述路径，请按需读取该文件。")
            prepared.append("；".join(details))
        except OSError:
            prepared.append(text)
    return prepared


def _parse_control_command(content: str) -> dict[str, str] | None:
    text = str(content or "").strip()
    if not text.startswith(_CONTROL_COMMAND_PREFIX):
        return None
    body = text[len(_CONTROL_COMMAND_PREFIX):].strip()
    if not body:
        raise AgentError("控制命令不能为空", error_code="control_command_invalid")
    command_name, _, argument = body.partition(" ")
    command_name = command_name.strip().lower()
    argument = argument.strip()
    if command_name == _MODEL_COMMAND:
        if not argument:
            raise AgentError("用法：/model 模型ID", error_code="control_command_invalid")
        return {"name": _MODEL_COMMAND, "argument": argument}
    return None


def _truncate_for_replay(value: str) -> str:
    text = " ".join(str(value or "").split()).strip()
    if len(text) <= _MAX_REPLAY_CHARS:
        return text
    return text[:_MAX_REPLAY_CHARS].rstrip() + "..."


def _dict_to_unified_event(data: dict[str, Any]):
    return UnifiedEvent(
        sequence=int(data.get("sequence") or 0),
        timestamp=str(data.get("timestamp") or ""),
        scope=str(data.get("scope") or ""),
        runtime_family=str(data.get("runtime_family") or ""),
        role=str(data.get("role") or ""),
        event_type=str(data.get("event_type") or ""),
        event_name=str(data.get("event_name") or ""),
        payload=dict(data.get("payload", {}) or {}),
        task_id=str(data.get("task_id") or ""),
        step_name=str(data.get("step_name") or ""),
        session_id=str(data.get("session_id") or ""),
    )


def _coerce_pid(value: Any) -> int:
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return 0
    return pid if pid > 0 else 0


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _claude_message_event_key(message: dict[str, Any]) -> str:
    content = message.get("content", []) if isinstance(message.get("content"), list) else []
    try:
        content_key = json.dumps(content, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        content_key = str(content)
    return "\x00".join(
        [
            str(message.get("id") or ""),
            str(message.get("stop_reason") or ""),
            content_key,
        ]
    )


def _parse_iso_timestamp(value: str) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _process_cmdline(pid: int) -> str | None:
    proc_path = Path("/proc") / str(pid) / "cmdline"
    if not proc_path.exists():
        return None
    try:
        data = proc_path.read_bytes()
    except (OSError, PermissionError):
        return None
    return data.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()


def _main_session_worker_alive(pid: int) -> bool:
    if not _pid_alive(pid):
        return False
    cmdline = _process_cmdline(pid)
    if cmdline is None:
        return True
    if not cmdline:
        return False
    return _WORKER_MODULE in cmdline or "lib/runtime/sessions/worker.py" in cmdline


def _started_recently(started_at: str, *, now: datetime | None = None) -> bool:
    if not started_at:
        return False
    now = now or datetime.now(timezone.utc)
    try:
        started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (now - started).total_seconds() <= _WORKER_BOOT_GRACE_SECONDS
