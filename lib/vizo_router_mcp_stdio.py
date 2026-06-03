#!/usr/bin/env python3
"""Vizo Router MCP stdio server.

为主会话提供平台级结构化能力发现、语义路由和交互设计状态工具。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, unquote, urlparse
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from vizo_core.agent_router import AgentRouter
from vizo_core.agent_runner import AgentRunner
from lib.config_loader import load_config
from lib.interaction_design_service import InteractionDesignService
from lib.runtime.events import utcnow_iso
from lib.startup_protocol import resolve_startup_protocol_context
from lib.task_titles import summarize_task_title

LATEST_PROTOCOL_VERSION = "2025-06-18"
RESOURCE_SCHEME = "vizo-capability"
STATE_FILE_NAME = "interaction-design-state.json"
BACKGROUND_TASK_STATE_NAME = "background-task-state.json"
TERMINAL_BACKGROUND_TASK_STATUSES = {"completed", "failed", "error", "cancelled", "canceled", "timeout", "stopped"}


def send(
    result_id: int | str | None,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": result_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result or {}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _tool_result(
    text: str,
    *,
    structured: dict[str, Any] | None = None,
    is_error: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }
    if structured is not None:
        result["structuredContent"] = structured
    return result


class VizoRouterAdapter:
    def __init__(self) -> None:
        self._tool_payloads = [
            {
                "name": "list_agent_capabilities",
                "description": "列出当前 Vizo 仓库可用的内置/用户 Agent 能力与工作流，替代旧的文本文档副本。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "include_workflows": {
                            "type": "boolean",
                            "description": "是否展开每个模块的 workflow 明细。默认 true。",
                        }
                    },
                },
            },
            {
                "name": "route_agent_request",
                "description": "根据用户原始请求和当前项目上下文返回结构化路由结果。适用于 Agent 模块选择，而不是页面直接生成。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "request": {
                            "type": "string",
                            "description": "用户的原始请求。",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "可选，覆盖当前检测到的项目名。",
                        },
                    },
                    "required": ["request"],
                },
            },
            {
                "name": "interaction_design_session",
                "description": (
                    "交互设计会话工具。用于候选视觉系统推荐、选择预览、读取当前状态、"
                    "显式采纳为项目默认，以及清空预览态。显式采纳既可来自主会话 preview，也可来自最近完成的正式交互设计任务。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["status", "propose", "select_preview", "adopt", "reset"],
                            "description": "要执行的交互设计动作。",
                        },
                        "request": {
                            "type": "string",
                            "description": "当 action=propose 时，填写用户原始需求。",
                        },
                        "candidate_id": {
                            "type": "string",
                            "description": "当 action=select_preview 或 adopt 时，填写候选方案 ID，如 A/B/C。",
                        },
                        "task_id": {
                            "type": "string",
                            "description": "当 action=adopt 且需要显式采纳某个正式交互设计任务结果时，可传入 task_id。",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "可选，覆盖当前检测到的项目名。",
                        },
                    },
                    "required": ["action"],
                },
            },
            {
                "name": "background_task_session",
                "description": (
                    "主会话后台任务提案工具。用于准备后台任务提案、读取待确认状态、"
                    "在用户明确同意后启动正式任务，或取消当前提案。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["status", "prepare", "confirm", "cancel"],
                            "description": "要执行的后台任务动作。",
                        },
                        "request": {
                            "type": "string",
                            "description": "当 action=prepare 时，填写用户原始需求。",
                        },
                        "target_kind": {
                            "type": "string",
                            "enum": ["development", "agent_hub"],
                            "description": "后台任务目标类型。development 表示开发工作流；agent_hub 表示某个 AgentHub 模块。",
                        },
                        "target_label": {
                            "type": "string",
                            "description": "用户可见的委派对象名称，如“交互设计师智能体”或“开发工作流”。",
                        },
                        "module_id": {
                            "type": "string",
                            "description": "当 target_kind=agent_hub 时，填写模块 ID。",
                        },
                        "reason": {
                            "type": "string",
                            "description": "可选，向用户解释为什么要委派到后台任务。",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "可选，覆盖当前检测到的项目名。",
                        },
                    },
                    "required": ["action"],
                },
            },
        ]

    def _load_config(self) -> dict[str, Any]:
        return load_config(force_reload=False) or {}

    def _build_project_context(self, project_name: str = ""):
        config = self._load_config()
        cwd = str(Path.cwd())
        return resolve_startup_protocol_context(
            project=project_name or None,
            cwd=cwd,
            scope="main_session",
            config=config,
            fallback_root=_PROJECT_ROOT,
        )

    def _project_config(self, context) -> dict[str, Any]:
        config = self._load_config()
        projects = (config.get("projects") or {}) if isinstance(config, dict) else {}
        project_cfg = projects.get(context.project_name, {})
        return project_cfg if isinstance(project_cfg, dict) else {}

    def _router(self) -> AgentRouter:
        return AgentRouter(self._load_config())

    def _design_service(self) -> InteractionDesignService:
        return InteractionDesignService(agent_runner=AgentRunner(self._load_config()))

    def _session_state_dir(self, context) -> Path:
        explicit_dir = str(os.environ.get("VIZO_MAIN_SESSION_DIR") or "").strip()
        if explicit_dir:
            base = Path(explicit_dir)
        else:
            runtime_dir = str(os.environ.get("VIZO_MAIN_SESSION_RUNTIME_DIR") or "").strip()
            if runtime_dir:
                base = Path(runtime_dir).resolve().parent
            else:
                session_id = str(os.environ.get("VIZO_MAIN_SESSION_ID") or "_tool_fallback").strip() or "_tool_fallback"
                base = Path(context.project_root) / ".vizo" / "sessions" / "main" / session_id
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _session_state_path(self, context) -> Path:
        return self._session_state_dir(context) / STATE_FILE_NAME

    def _background_task_state_path(self, context) -> Path:
        return self._session_state_dir(context) / BACKGROUND_TASK_STATE_NAME

    def _background_task_launch_log_path(self, context) -> Path:
        return self._session_state_dir(context) / "background-task-launch.log"

    @staticmethod
    def _append_background_launch_log(path: Path, message: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with path.open("a", encoding="utf-8") as log_file:
                log_file.write(f"[{ts}] {message.rstrip()}\n")
        except Exception:
            pass

    def _track_background_task_process(self, context, proc: subprocess.Popen, launch_log: Path) -> None:
        def _wait() -> None:
            try:
                returncode = proc.wait()
            except Exception as exc:
                self._append_background_launch_log(
                    launch_log,
                    f"failed waiting for background task pid={getattr(proc, 'pid', '?')}: {exc}",
                )
                return
            self._append_background_launch_log(
                launch_log,
                f"background task pid={proc.pid} exited with returncode={returncode}",
            )
            state = self._load_background_task_state(context)
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
            self._save_background_task_state(context, state)

        threading.Thread(target=_wait, daemon=True).start()

    def _load_interaction_state(self, context) -> dict[str, Any]:
        path = self._session_state_path(context)
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_interaction_state(self, context, payload: dict[str, Any]) -> Path:
        path = self._session_state_path(context)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    def _load_background_task_state(self, context) -> dict[str, Any]:
        path = self._background_task_state_path(context)
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_background_task_state(self, context, payload: dict[str, Any]) -> Path:
        path = self._background_task_state_path(context)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    def _list_modules(self, *, include_workflows: bool = True) -> list[dict[str, Any]]:
        modules = self._router()._load_available_modules()
        result: list[dict[str, Any]] = []
        for item in modules:
            manifest = item.get("manifest", {}) if isinstance(item.get("manifest"), dict) else {}
            workflows = manifest.get("workflows", {}) if isinstance(manifest.get("workflows"), dict) else {}
            row = {
                "module_id": str(item.get("id") or manifest.get("id") or ""),
                "name": str(manifest.get("name") or ""),
                "description": str(manifest.get("description") or ""),
                "source": str(item.get("source") or ""),
                "dir": str(item.get("dir") or ""),
            }
            if include_workflows:
                row["workflows"] = [
                    {
                        "workflow_id": wf_id,
                        "name": str((wf or {}).get("name") or wf_id),
                        "description": str((wf or {}).get("description") or ""),
                    }
                    for wf_id, wf in workflows.items()
                ]
            result.append(row)
        return result

    def list_tools(self) -> dict[str, Any]:
        return {"tools": self._tool_payloads}

    def _capability_index_uri(self) -> str:
        return f"{RESOURCE_SCHEME}://catalog/index"

    def _module_uri(self, module_id: str) -> str:
        return f"{RESOURCE_SCHEME}://module/{quote(module_id, safe='')}"

    def resources_list(self) -> dict[str, Any]:
        modules = self._list_modules(include_workflows=True)
        resources = [
            {
                "name": "__index__",
                "uri": self._capability_index_uri(),
                "description": "Index of Vizo agent capabilities.",
                "mimeType": "application/json",
            }
        ]
        for module in modules:
            resources.append(
                {
                    "name": module["module_id"],
                    "uri": self._module_uri(module["module_id"]),
                    "description": module["description"],
                    "mimeType": "application/json",
                }
            )
        return {"resources": resources}

    def resources_read(self, uri: str) -> dict[str, Any]:
        parsed = urlparse(uri)
        if parsed.scheme != RESOURCE_SCHEME:
            raise ValueError(f"Unsupported resource URI: {uri}")
        if parsed.netloc == "catalog" and parsed.path == "/index":
            modules = self._list_modules(include_workflows=True)
            return {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "application/json",
                        "text": json.dumps({"modules": modules}, ensure_ascii=False, indent=2),
                    }
                ]
            }
        if parsed.netloc == "module":
            module_id = unquote(parsed.path.lstrip("/"))
            modules = self._router()._load_available_modules()
            for item in modules:
                if str(item.get("id") or "") != module_id:
                    continue
                manifest = item.get("manifest", {}) if isinstance(item.get("manifest"), dict) else {}
                payload = {
                    "module_id": module_id,
                    "source": item.get("source"),
                    "dir": item.get("dir"),
                    "manifest": manifest,
                }
                return {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "application/json",
                            "text": json.dumps(payload, ensure_ascii=False, indent=2),
                        }
                    ]
                }
            raise FileNotFoundError(f"Unknown module: {module_id}")
        raise ValueError(f"Unsupported resource URI: {uri}")

    def call_tool(self, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        args = arguments or {}
        if name == "list_agent_capabilities":
            include_workflows = bool(args.get("include_workflows", True))
            modules = self._list_modules(include_workflows=include_workflows)
            text_lines = ["当前可用 Agent 能力："]
            for module in modules:
                text_lines.append(f"- {module['module_id']} | {module['name']}：{module['description']}")
            return _tool_result(
                "\n".join(text_lines),
                structured={"modules": modules},
            )

        if name == "route_agent_request":
            request = str(args.get("request") or "").strip()
            if not request:
                return _tool_result("缺少 request。", is_error=True)
            context = self._build_project_context(str(args.get("project_name") or "").strip())
            result = asyncio.run(self._router().route(request, self._project_config(context)))
            if result.get("error"):
                modules = self._list_modules(include_workflows=True)
                result = dict(result)
                result["available_modules"] = modules
            return _tool_result(
                json.dumps(result, ensure_ascii=False, indent=2),
                structured=result,
                is_error=bool(result.get("error")),
            )

        if name == "interaction_design_session":
            return self._call_interaction_design(args)

        if name == "background_task_session":
            return self._call_background_task_session(args)

        return _tool_result(f"Unknown tool: {name}", is_error=True)

    def _match_candidate(self, candidate_id: str, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
        target = str(candidate_id or "").strip()
        if not target:
            return None
        for item in candidates:
            if str(item.get("id") or "").strip().lower() == target.lower():
                return dict(item)
        return None

    def _interaction_status_payload(
        self,
        *,
        service: InteractionDesignService,
        project_root: Path,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        adopted = service.load_adopted_design(project_root)
        task_candidate = service.load_latest_task_design(project_root)
        preview = dict(state.get("preview") or {})
        candidate_round = dict(state.get("candidate_round") or {})
        payload: dict[str, Any] = {
            "has_preview": bool(preview),
            "has_candidate_round": bool(candidate_round),
            "has_adopted_design": bool(adopted),
            "has_adoptable_task_design": bool(task_candidate),
            "preview": preview,
            "candidate_round": candidate_round,
            "adopted": adopted,
            "adoptable_task_design": task_candidate,
        }
        if preview.get("candidate"):
            payload["preview_design_context"] = service.build_runtime_context(preview["candidate"], mode="preview")
        if adopted:
            payload["adopted_design_context"] = service.build_runtime_context(adopted, mode="adopted")
        return payload

    def _call_interaction_design(self, args: dict[str, Any]) -> dict[str, Any]:
        action = str(args.get("action") or "").strip()
        context = self._build_project_context(str(args.get("project_name") or "").strip())
        project_root = Path(context.project_root)
        service = self._design_service()
        state = self._load_interaction_state(context)
        state.setdefault("version", 1)
        state.setdefault("updated_at", utcnow_iso())

        if action == "status":
            payload = self._interaction_status_payload(service=service, project_root=project_root, state=state)
            return _tool_result(
                json.dumps(payload, ensure_ascii=False, indent=2),
                structured=payload,
            )

        if action == "reset":
            state.pop("preview", None)
            state.pop("candidate_round", None)
            state["updated_at"] = utcnow_iso()
            self._save_interaction_state(context, state)
            return _tool_result(
                "已清空当前主会话的交互设计预览态和候选轮次。",
                structured=self._interaction_status_payload(service=service, project_root=project_root, state=state),
            )

        if action == "propose":
            request = str(args.get("request") or "").strip()
            if not request:
                return _tool_result("action=propose 时必须提供 request。", is_error=True)
            adopted = service.load_adopted_design(project_root)
            session_stub = SimpleNamespace(
                metadata={
                    "project_name": context.project_name,
                    "project_root": context.project_root,
                }
            )
            session_dir = self._session_state_path(context).parent
            candidates = asyncio.run(
                service.generate_candidates(
                    session=session_stub,
                    session_dir=session_dir,
                    project_root=project_root,
                    content=request,
                    adopted=adopted,
                    replace_existing=bool(adopted),
                )
            )
            state["candidate_round"] = {
                "created_at": utcnow_iso(),
                "replace_existing": bool(adopted),
                "source_request": request,
                "candidates": candidates,
            }
            state.pop("preview", None)
            state["updated_at"] = utcnow_iso()
            self._save_interaction_state(context, state)
            payload = self._interaction_status_payload(service=service, project_root=project_root, state=state)
            return _tool_result(
                service._format_candidates_message(candidates, replace_existing=bool(adopted)),
                structured=payload,
            )

        candidate_round = dict(state.get("candidate_round") or {})
        preview = dict(state.get("preview") or {})
        candidate_id = str(args.get("candidate_id") or "").strip()
        task_id = str(args.get("task_id") or "").strip()

        if action == "select_preview":
            selection = self._match_candidate(candidate_id, list(candidate_round.get("candidates") or []))
            if not selection:
                return _tool_result("当前没有可用候选，或 candidate_id 无效。", is_error=True)
            state["preview"] = {
                "selected_at": utcnow_iso(),
                "replace_existing": bool(candidate_round.get("replace_existing")),
                "candidate": selection,
            }
            state.pop("candidate_round", None)
            state["updated_at"] = utcnow_iso()
            self._save_interaction_state(context, state)
            payload = self._interaction_status_payload(service=service, project_root=project_root, state=state)
            return _tool_result(
                service._format_preview_ready_message(selection),
                structured=payload,
            )

        if action == "adopt":
            selection = None
            replace_existing = False
            adopted = service.load_adopted_design(project_root)
            if candidate_id:
                selection = self._match_candidate(candidate_id, list(candidate_round.get("candidates") or []))
                replace_existing = bool(candidate_round.get("replace_existing"))
            if selection is None and preview.get("candidate"):
                selection = dict(preview.get("candidate") or {})
                replace_existing = bool(preview.get("replace_existing"))
            if selection is None:
                selection = service.load_latest_task_design(project_root, task_id=task_id)
                replace_existing = bool(adopted)
            if not selection:
                return _tool_result("没有可采纳的候选、预览态方案或正式交互设计任务结果。", is_error=True)
            adopted = service.adopt_design(
                project_root=project_root,
                candidate=selection,
                replace_existing=replace_existing,
            )
            state.pop("preview", None)
            state.pop("candidate_round", None)
            state["updated_at"] = utcnow_iso()
            self._save_interaction_state(context, state)
            payload = self._interaction_status_payload(service=service, project_root=project_root, state=state)
            payload["adopted"] = adopted
            payload["adopted_design_context"] = service.build_runtime_context(adopted, mode="adopted")
            return _tool_result(
                service._format_adopted_message(adopted, replace_existing=replace_existing),
                structured=payload,
            )

        return _tool_result(f"Unsupported action: {action}", is_error=True)

    def _find_module(self, module_id: str) -> dict[str, Any] | None:
        target = str(module_id or "").strip()
        if not target:
            return None
        for item in self._list_modules(include_workflows=True):
            if str(item.get("module_id") or "").strip() == target:
                return item
        return None

    @staticmethod
    def _parse_utc_datetime(value: Any) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _pid_matches_command(pid: int, command: list[Any]) -> bool:
        if not command:
            return True
        cmdline_path = Path("/proc") / str(pid) / "cmdline"
        if not cmdline_path.exists():
            return True
        try:
            cmdline = cmdline_path.read_bytes().replace(b"\x00", b" ").decode("utf-8", "ignore")
        except Exception:
            return True
        expected = [str(item) for item in command[:2] if str(item or "").strip()]
        return all(Path(item).name in cmdline or item in cmdline for item in expected)

    @classmethod
    def _is_launch_process_running(cls, launch: dict[str, Any]) -> bool:
        try:
            pid = int(launch.get("pid") or 0)
        except (TypeError, ValueError):
            return False
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        command = launch.get("command") if isinstance(launch.get("command"), list) else []
        return cls._pid_matches_command(pid, command)

    def _find_launch_task_state(self, *, context, launch: dict[str, Any]) -> dict[str, Any]:
        project_root = Path(
            str(launch.get("project_root") or launch.get("cwd") or getattr(context, "project_root", "") or _PROJECT_ROOT)
        )
        tasks_root = project_root / ".vizo" / "tasks"
        if not tasks_root.exists():
            return {}
        module_id = str(launch.get("module_id") or "").strip()
        request = str(launch.get("request") or "").strip()
        launched_at = self._parse_utc_datetime(launch.get("launched_at"))
        candidates: list[tuple[float, dict[str, Any], Path]] = []
        for state_path in tasks_root.glob("hub-*/state.json"):
            try:
                task_state = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(task_state, dict):
                continue
            if module_id and str(task_state.get("module_id") or "").strip() != module_id:
                continue
            description = str(task_state.get("description") or task_state.get("original_request") or "").strip()
            if request and description and description != request:
                continue
            try:
                mtime = datetime.fromtimestamp(state_path.stat().st_mtime, tz=timezone.utc)
            except Exception:
                mtime = None
            if launched_at and mtime and abs((mtime - launched_at).total_seconds()) > 24 * 3600:
                continue
            score = abs((mtime - launched_at).total_seconds()) if launched_at and mtime else 0.0
            candidates.append((score, task_state, state_path))
        if not candidates:
            return {}
        candidates.sort(key=lambda item: item[0])
        task_state = dict(candidates[0][1])
        task_state.setdefault("state_path", str(candidates[0][2]))
        return task_state

    def _annotate_last_launch(self, *, context, launch: dict[str, Any]) -> dict[str, Any]:
        if not launch:
            return {}
        annotated = dict(launch)
        process_running = self._is_launch_process_running(annotated)
        task_state = self._find_launch_task_state(context=context, launch=annotated)
        task_status = str(task_state.get("status") or "").strip().lower()
        if task_state:
            annotated["task_id"] = str(task_state.get("id") or annotated.get("task_id") or "")
            annotated["task_status"] = task_status or str(task_state.get("status") or "").strip()
            annotated["task_state_path"] = str(task_state.get("state_path") or "")
        terminal_task = bool(task_status and task_status in TERMINAL_BACKGROUND_TASK_STATUSES)
        if terminal_task:
            is_active = False
            stale_reason = f"task_{task_status}"
        elif process_running:
            is_active = True
            stale_reason = ""
        elif task_status:
            is_active = False
            stale_reason = "process_exited"
        else:
            is_active = False
            stale_reason = "process_not_found"
        annotated["process_running"] = process_running
        annotated["is_active"] = is_active
        annotated["runtime_status"] = "active" if is_active else "stale"
        if stale_reason:
            annotated["stale_reason"] = stale_reason
        else:
            annotated.pop("stale_reason", None)
        return annotated

    def _background_task_status_payload(self, state: dict[str, Any], *, context) -> dict[str, Any]:
        pending = dict(state.get("pending") or {})
        last_launch = self._annotate_last_launch(context=context, launch=dict(state.get("last_launch") or {}))
        return {
            "has_pending_task": bool(pending),
            "pending_task": pending,
            "last_launch": last_launch,
            "awaiting_user_confirmation": bool(pending),
        }

    @staticmethod
    def _format_background_task_prepare_message(pending: dict[str, Any], *, replaced_existing: bool) -> str:
        target_label = str(pending.get("target_label") or "后台任务").strip()
        request = str(pending.get("request") or "").strip()
        reason = str(pending.get("reason") or "").strip()
        task_title = summarize_task_title(
            pending.get("task_title") or pending.get("task_summary") or request or target_label,
            fallback=target_label,
        )
        lines = []
        if replaced_existing:
            lines.append("我已经用新的后台任务提案替换了上一条待确认请求。")
        lines.extend(
            [
                f"我准备在后台使用「{target_label}」完成《{task_title}》，是否继续？",
                "这会启动一个正式任务，并进入直播、暂停/恢复和知识沉淀等任务生命周期。",
            ]
        )
        if reason:
            lines.append(f"原因：{reason}")
        if request:
            lines.append(f"任务标题：{task_title}")
        lines.extend(
            [
                "如果继续，请直接回复“继续”或“开始”。",
                "如果取消，请直接回复“取消”。",
                "如果你想先再聊聊，也可以继续提问，我不会自动启动任务。",
            ]
        )
        return "\n".join(lines).strip()

    @staticmethod
    def _format_background_task_cancel_message(had_pending: bool) -> str:
        if had_pending:
            return "已取消当前后台任务提案，主会话不会启动任何正式任务。我们继续在当前会话沟通。"
        return "当前没有待取消的后台任务提案。"

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
    def _format_background_task_status_message(payload: dict[str, Any]) -> str:
        pending = dict(payload.get("pending_task") or {})
        last_launch = dict(payload.get("last_launch") or {})
        if pending:
            return (
                f"当前有一个待确认的后台任务提案：{pending.get('target_label') or '后台任务'}。\n"
                "如需继续，请让用户明确回复“继续”或“开始”；如需取消，请回复“取消”。"
            )
        if last_launch:
            if not bool(last_launch.get("is_active")):
                task_status = str(last_launch.get("task_status") or "").strip()
                stale_reason = str(last_launch.get("stale_reason") or "stale").strip()
                detail = f"任务状态：{task_status}" if task_status else f"原因：{stale_reason}"
                return (
                    f"最近一次后台任务「{last_launch.get('target_label') or '后台任务'}」已经不在运行（{detail}）。\n"
                    "当前没有待确认的后台任务提案；如果用户要重试，需要先创建新的后台任务提案，再等待用户确认。"
                )
            return (
                f"最近一次已启动的后台任务：{last_launch.get('target_label') or '后台任务'}。\n"
                "如果用户现在只是继续聊天，不需要重新发起。"
            )
        return "当前没有待确认的后台任务提案。"

    def _build_background_task_command(self, *, context, pending: dict[str, Any]) -> list[str]:
        request = str(pending.get("request") or "").strip()
        target_kind = str(pending.get("target_kind") or "").strip()
        module_id = str(pending.get("module_id") or "").strip()
        if not request:
            raise ValueError("缺少 request。")
        command = [sys.executable, str(_PROJECT_ROOT / "opus.py")]
        if target_kind == "development":
            command.extend([request, "--dev"])
        elif target_kind == "agent_hub":
            if not module_id:
                raise ValueError("target_kind=agent_hub 时必须提供 module_id。")
            command.append(f"{request} @{module_id}")
        else:
            raise ValueError(f"Unsupported target_kind: {target_kind}")
        project_cfg = self._project_config(context)
        if context.project_name and isinstance(project_cfg, dict) and project_cfg:
            command.extend(["--project", context.project_name])
        return command

    def _call_background_task_session(self, args: dict[str, Any]) -> dict[str, Any]:
        action = str(args.get("action") or "").strip()
        context = self._build_project_context(str(args.get("project_name") or "").strip())
        state = self._load_background_task_state(context)
        state.setdefault("version", 1)
        state.setdefault("updated_at", utcnow_iso())

        if action == "status":
            payload = self._background_task_status_payload(state, context=context)
            if dict(state.get("last_launch") or {}) != dict(payload.get("last_launch") or {}):
                state["last_launch"] = dict(payload.get("last_launch") or {})
                state["updated_at"] = utcnow_iso()
                self._save_background_task_state(context, state)
            return _tool_result(
                self._format_background_task_status_message(payload),
                structured=payload,
            )

        if action == "prepare":
            request = str(args.get("request") or "").strip()
            target_kind = str(args.get("target_kind") or "").strip()
            module_id = str(args.get("module_id") or "").strip()
            reason = str(args.get("reason") or "").strip()
            if not request:
                return _tool_result("action=prepare 时必须提供 request。", is_error=True)
            if target_kind not in {"development", "agent_hub"}:
                return _tool_result("action=prepare 时必须提供有效的 target_kind。", is_error=True)

            module = None
            if target_kind == "agent_hub":
                if not module_id:
                    return _tool_result("target_kind=agent_hub 时必须提供 module_id。", is_error=True)
                module = self._find_module(module_id)
                if not module:
                    return _tool_result(f"未找到模块：{module_id}", is_error=True)

            target_label = str(args.get("target_label") or "").strip()
            if not target_label:
                if target_kind == "development":
                    target_label = "开发工作流"
                elif module:
                    target_label = str(module.get("name") or module_id or "AgentHub 模块").strip()
            task_title = summarize_task_title(request, fallback=target_label)

            replaced_existing = bool(state.get("pending"))
            pending = {
                "proposal_id": f"bg_{uuid.uuid4().hex[:10]}",
                "created_at": utcnow_iso(),
                "target_kind": target_kind,
                "target_label": target_label,
                "task_title": task_title,
                "task_summary": task_title,
                "request": request,
                "reason": reason,
                "module_id": module_id,
                "project_name": context.project_name,
                "project_root": context.project_root,
            }
            state["pending"] = pending
            state["updated_at"] = utcnow_iso()
            self._save_background_task_state(context, state)
            payload = self._background_task_status_payload(state, context=context)
            return _tool_result(
                self._format_background_task_prepare_message(pending, replaced_existing=replaced_existing),
                structured=payload,
            )

        if action == "cancel":
            had_pending = bool(state.get("pending"))
            state.pop("pending", None)
            state["updated_at"] = utcnow_iso()
            self._save_background_task_state(context, state)
            payload = self._background_task_status_payload(state, context=context)
            return _tool_result(
                self._format_background_task_cancel_message(had_pending),
                structured=payload,
            )

        if action == "confirm":
            pending = dict(state.get("pending") or {})
            if not pending:
                return _tool_result("当前没有待确认的后台任务提案。", is_error=True)
            launch_log = self._background_task_launch_log_path(context)
            log_handle = None
            try:
                command = self._build_background_task_command(context=context, pending=pending)
                self._append_background_launch_log(
                    launch_log,
                    f"starting background task target={pending.get('target_label') or '后台任务'} cwd={Path(context.project_root)}",
                )
                log_handle = launch_log.open("ab")
                child_env = dict(os.environ)
                child_env.setdefault("VIZO_PROJECT_ROOT", str(Path(context.project_root)))
                session_dir = self._session_state_dir(context)
                child_env.setdefault("VIZO_MAIN_SESSION_DIR", str(session_dir))
                child_env.setdefault("VIZO_MAIN_SESSION_ID", session_dir.name)
                proc = subprocess.Popen(
                    command,
                    cwd=str(Path(context.project_root)),
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    env=child_env,
                    start_new_session=True,
                )
            except Exception as exc:
                return _tool_result(f"后台任务启动失败：{exc}", is_error=True)
            finally:
                if log_handle:
                    try:
                        log_handle.close()
                    except Exception:
                        pass

            self._append_background_launch_log(launch_log, f"spawned background task pid={proc.pid}")
            self._track_background_task_process(context, proc, launch_log)
            launch = {
                **pending,
                "launched_at": utcnow_iso(),
                "pid": int(proc.pid),
                "command": command,
                "cwd": str(Path(context.project_root)),
                "launch_log": str(launch_log),
                "process_running": True,
                "is_active": True,
                "runtime_status": "active",
            }
            state["last_launch"] = launch
            state.pop("pending", None)
            state["updated_at"] = utcnow_iso()
            self._save_background_task_state(context, state)
            payload = self._background_task_status_payload(state, context=context)
            return _tool_result(
                self._format_background_task_confirm_message(launch),
                structured=payload,
            )

        return _tool_result(f"Unsupported action: {action}", is_error=True)


ADAPTER = VizoRouterAdapter()


def handle_request(message: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    method = message.get("method")
    params = message.get("params") or {}

    if method == "initialize":
        requested_version = params.get("protocolVersion") or LATEST_PROTOCOL_VERSION
        return (
            {
                "protocolVersion": requested_version,
                "capabilities": {
                    "logging": {},
                    "resources": {"listChanged": False, "subscribe": False},
                    "prompts": {"listChanged": False},
                    "tools": {"listChanged": False},
                },
                "serverInfo": {
                    "name": "vizo-router",
                    "version": "1.0.0",
                },
                "instructions": (
                    "Use this server for Vizo capability discovery, module routing, and interaction design session flow. "
                    "Prefer it over guessing from static prompt text."
                ),
            },
            None,
        )

    if method == "ping":
        return ({}, None)

    if method == "tools/list":
        return (ADAPTER.list_tools(), None)

    if method == "tools/call":
        return (ADAPTER.call_tool(params.get("name", ""), params.get("arguments")), None)

    if method == "resources/list":
        return (ADAPTER.resources_list(), None)

    if method == "resources/read":
        try:
            return (ADAPTER.resources_read(str(params.get("uri") or "")), None)
        except FileNotFoundError as exc:
            return (None, {"code": -32001, "message": str(exc)})
        except ValueError as exc:
            return (None, {"code": -32602, "message": str(exc)})

    if method == "resources/templates/list":
        return (
            {
                "resourceTemplates": [
                    {
                        "name": "vizo-module",
                        "uriTemplate": f"{RESOURCE_SCHEME}://module" + "/{module_id}",
                        "description": "Read a Vizo agent/module manifest by module_id.",
                        "mimeType": "application/json",
                    }
                ]
            },
            None,
        )

    if method == "prompts/list":
        return ({"prompts": []}, None)

    if method == "logging/setLevel":
        return ({}, None)

    if method in {"notifications/initialized", "notifications/cancelled"}:
        return (None, None)

    return (None, {"code": -32601, "message": f"Method not found: {method}"})


def main() -> int:
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            send(None, error={"code": -32700, "message": f"Invalid JSON: {exc}"})
            continue

        request_id = message.get("id")
        try:
            result, error = handle_request(message)
        except Exception as exc:  # pragma: no cover
            traceback.print_exc(file=sys.stderr)
            result = None
            error = {"code": -32000, "message": f"{exc.__class__.__name__}: {exc}"}

        if request_id is None:
            continue
        send(request_id, result=result, error=error)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
