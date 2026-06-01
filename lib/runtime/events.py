from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .contracts import (
    RUNTIME_FAMILY_CLAUDE_CODE,
    RUNTIME_FAMILY_CODEX,
    RouteDecision,
    RuntimeExecutionContext,
)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_TEST_COMMAND_RE = re.compile(
    r"\b(pytest|unittest|npm\s+(?:run\s+)?test|pnpm\s+(?:run\s+)?test|yarn\s+test|vitest|jest|go\s+test|cargo\s+test|rspec)\b",
    re.IGNORECASE,
)


@dataclass
class UnifiedEvent:
    sequence: int
    timestamp: str
    scope: str
    runtime_family: str
    role: str
    event_type: str
    event_name: str
    payload: dict[str, Any] = field(default_factory=dict)
    task_id: str = ""
    step_name: str = ""
    session_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_lifecycle_event(
    *,
    sequence: int,
    decision: RouteDecision,
    context: RuntimeExecutionContext,
    event_name: str,
    payload: dict[str, Any] | None = None,
    session_id: str = "",
) -> UnifiedEvent:
    return UnifiedEvent(
        sequence=sequence,
        timestamp=utcnow_iso(),
        scope=context.scope,
        runtime_family=decision.runtime_family,
        role=context.role,
        task_id=context.task_id,
        step_name=context.step_name,
        session_id=session_id,
        event_type="lifecycle",
        event_name=event_name,
        payload=payload or {},
    )


def build_claude_unified_events(
    raw_event: dict[str, Any],
    *,
    sequence_start: int,
    decision: RouteDecision,
    context: RuntimeExecutionContext,
) -> list[UnifiedEvent]:
    event_type = raw_event.get("type", "unknown")
    events: list[UnifiedEvent] = []
    next_sequence = sequence_start

    def emit(name: str, payload: dict[str, Any], kind: str, session_id: str = "") -> None:
        nonlocal next_sequence
        events.append(
            UnifiedEvent(
                sequence=next_sequence,
                timestamp=utcnow_iso(),
                scope=context.scope,
                runtime_family=decision.runtime_family,
                role=context.role,
                task_id=context.task_id,
                step_name=context.step_name,
                session_id=session_id,
                event_type=kind,
                event_name=name,
                payload=payload,
            )
        )
        next_sequence += 1

    if event_type == "system" and raw_event.get("subtype") == "init":
        emit(
            "init",
            {
                "model": raw_event.get("model", ""),
                "tools": raw_event.get("tools", []),
                "cwd": raw_event.get("cwd", ""),
            },
            "lifecycle",
            session_id=raw_event.get("session_id", ""),
        )
        return events

    if event_type == "status":
        emit(
            "runtime_status",
            {
                "text": str(raw_event.get("text") or ""),
                "status_kind": str(raw_event.get("subtype") or ""),
            },
            "lifecycle",
            session_id=raw_event.get("session_id", ""),
        )
        return events

    if event_type == "assistant":
        message = raw_event.get("message", {})
        for block in message.get("content", []) if isinstance(message, dict) else []:
            block_type = block.get("type", "")
            if block_type == "tool_use":
                tool_name = str(block.get("name") or "")
                tool_input = block.get("input", {})
                emit(
                    "work_progress",
                    _build_work_progress_payload(
                        tool_name=tool_name,
                        tool_input=tool_input,
                        status="started",
                    ),
                    "work_progress",
                )
            elif block_type == "text":
                emit(
                    "assistant_text",
                    {"text": block.get("text", "")},
                    "message",
                )
            elif block_type == "thinking":
                emit(
                    "thinking",
                    {"text": block.get("thinking", "")},
                    "reasoning",
                )
            else:
                emit(
                    "assistant_block",
                    {"block_type": block_type},
                    "raw",
                )
        return events

    if event_type == "result":
        emit(
            "result",
            {
                "is_error": raw_event.get("is_error", False),
                "stop_reason": raw_event.get("stop_reason", ""),
                "usage": raw_event.get("usage", {}),
                "total_cost_usd": raw_event.get("total_cost_usd", 0.0),
            },
            "lifecycle",
            session_id=raw_event.get("session_id", ""),
        )
        return events

    emit(
        "passthrough",
        {
            "raw_type": event_type,
            "raw_subtype": raw_event.get("subtype", ""),
        },
        "raw",
        session_id=raw_event.get("session_id", ""),
    )
    return events


def build_codex_unified_events(
    raw_event: dict[str, Any],
    *,
    sequence_start: int,
    decision: RouteDecision,
    context: RuntimeExecutionContext,
) -> list[UnifiedEvent]:
    event_type = str(raw_event.get("type") or "unknown")
    events: list[UnifiedEvent] = []
    next_sequence = sequence_start

    def emit(name: str, payload: dict[str, Any], kind: str, session_id: str = "") -> None:
        nonlocal next_sequence
        events.append(
            UnifiedEvent(
                sequence=next_sequence,
                timestamp=utcnow_iso(),
                scope=context.scope,
                runtime_family=decision.runtime_family,
                role=context.role,
                task_id=context.task_id,
                step_name=context.step_name,
                session_id=session_id,
                event_type=kind,
                event_name=name,
                payload=payload,
            )
        )
        next_sequence += 1

    if event_type == "thread.started":
        thread_id = str(raw_event.get("thread_id") or "")
        emit(
            "init",
            {"thread_id": thread_id},
            "lifecycle",
            session_id=thread_id,
        )
        return events

    if event_type == "turn.started":
        emit("turn_started", {}, "lifecycle")
        return events

    if event_type == "interaction_requested":
        emit(
            "interaction_requested",
            dict(raw_event.get("payload") or {}),
            "lifecycle",
            session_id=str(raw_event.get("thread_id") or ""),
        )
        return events

    if event_type == "turn.completed":
        emit(
            "result",
            {
                "usage": raw_event.get("usage", {}),
                "status": "completed",
            },
            "lifecycle",
            session_id=str(raw_event.get("thread_id") or ""),
        )
        return events

    if event_type == "turn.failed":
        error_payload = raw_event.get("error", {}) or {}
        emit(
            "result",
            {
                "is_error": True,
                "message": error_payload.get("message", ""),
                "status": "failed",
            },
            "lifecycle",
            session_id=str(raw_event.get("thread_id") or ""),
        )
        return events

    if event_type == "error":
        emit(
            "runtime_error",
            {"message": raw_event.get("message", "")},
            "error",
            session_id=str(raw_event.get("thread_id") or ""),
        )
        return events

    if event_type in {"image_generation_call", "image_generation_end"}:
        return events

    if event_type == "response_item":
        payload = raw_event.get("payload", {}) if isinstance(raw_event.get("payload"), dict) else {}
        payload_type = str(payload.get("type") or "")
        if payload_type == "message":
            if str(payload.get("role") or "") != "assistant":
                return events
            is_commentary = str(payload.get("phase") or "") == "commentary"
            event_name = "runtime_status" if is_commentary else "assistant_text"
            event_kind = "lifecycle" if is_commentary else "message"
            for block in payload.get("content", []) if isinstance(payload.get("content"), list) else []:
                block_type = str(block.get("type") or "")
                text = str(block.get("text") or "")
                if not text and block_type == "output_text":
                    text = str(block.get("text") or "")
                if text:
                    emit(
                        event_name,
                        {"text": text, **({"status_kind": "commentary"} if is_commentary else {})},
                        event_kind,
                    )
                else:
                    emit("assistant_block", {"block_type": block_type}, "raw")
            return events
        if payload_type == "reasoning":
            summary_text = _extract_codex_reasoning_text(payload)
            if summary_text:
                emit("thinking", {"text": summary_text}, "reasoning")
            return events
        if payload_type in {"function_call", "custom_tool_call"}:
            tool_input = _normalize_codex_tool_input(payload)
            emit(
                "work_progress",
                _build_work_progress_payload(
                    tool_name=str(payload.get("name") or ""),
                    tool_input=tool_input,
                    status="started",
                    call_id=str(payload.get("call_id") or ""),
                ),
                "work_progress",
            )
            return events
        if payload_type == "web_search_call":
            action = payload.get("action", {}) if isinstance(payload.get("action"), dict) else {}
            emit(
                "work_progress",
                _build_work_progress_payload(
                    tool_name="web_search",
                    tool_input=action,
                    status="started",
                ),
                "work_progress",
            )
            return events
        if payload_type in {"function_call_output", "custom_tool_call_output"}:
            emit(
                "work_progress",
                _build_work_progress_payload(
                    tool_name=str(payload.get("name") or ""),
                    tool_input={"call_id": payload.get("call_id", "")},
                    status="completed",
                    call_id=str(payload.get("call_id") or ""),
                    output=str(payload.get("output") or ""),
                ),
                "work_progress",
            )
            return events
        if payload_type == "image_generation_call":
            return events

    if event_type == "event_msg":
        payload = raw_event.get("payload", {}) if isinstance(raw_event.get("payload"), dict) else {}
        payload_type = str(payload.get("type") or "")
        if payload_type == "agent_message":
            text = str(payload.get("message") or "")
            if text:
                if str(payload.get("phase") or "") == "commentary":
                    emit("runtime_status", {"text": text, "status_kind": "commentary"}, "lifecycle")
                else:
                    emit("assistant_text", {"text": text}, "message")
            return events
        if payload_type in {"image_generation_call", "image_generation_end"}:
            return events

    if event_type in {"item.started", "item.completed"}:
        item = raw_event.get("item", {}) if isinstance(raw_event.get("item"), dict) else {}
        item_type = str(item.get("type") or "")
        if item_type == "agent_message":
            text = _extract_codex_item_text(item)
            if text:
                emit("assistant_text", {"text": text}, "message")
                return events
        if item_type == "command_execution":
            command = str(item.get("command") or "")
            if event_type == "item.started":
                emit(
                    "work_progress",
                    _build_work_progress_payload(
                        tool_name="command_execution",
                        tool_input={"command": command},
                        status="started",
                        command=command,
                    ),
                    "work_progress",
                )
                return events
            command_status = "failed" if str(item.get("status") or "").lower() in {"failed", "error"} else "completed"
            exit_code = item.get("exit_code")
            if exit_code not in {None, "", 0, "0"}:
                command_status = "failed"
            emit(
                "work_progress",
                _build_work_progress_payload(
                    tool_name="command_execution",
                    tool_input={"command": command},
                    status=command_status,
                    command=command,
                    exit_code=exit_code,
                    output=str(item.get("aggregated_output") or ""),
                ),
                "work_progress",
            )
            return events
        if item_type == "mcp_tool_call":
            tool_name = _codex_tool_label(item)
            if event_type == "item.started":
                emit(
                    "work_progress",
                    _build_work_progress_payload(
                        tool_name=tool_name,
                        tool_input=item.get("arguments") or {},
                        status="started",
                    ),
                    "work_progress",
                )
                return events
            tool_status = "failed" if str(item.get("status") or "").lower() in {"failed", "error"} else "completed"
            emit(
                "work_progress",
                _build_work_progress_payload(
                    tool_name=tool_name,
                    tool_input=item.get("arguments") or {},
                    status=tool_status,
                    output=_extract_codex_item_result_text(item),
                ),
                "work_progress",
            )
            return events
        if item_type == "error":
            emit(
                "runtime_error",
                {"message": str(item.get("message") or "")},
                "error",
            )
            return events

    if event_type.endswith(".delta") or event_type.endswith(".done"):
        text = str(raw_event.get("text") or raw_event.get("delta") or "")
        if text:
            kind = "reasoning" if "reason" in event_type else "message"
            name = "thinking" if kind == "reasoning" else "assistant_text"
            emit(name, {"text": text}, kind)
            return events

    emit(
        "passthrough",
        {
            "raw_type": event_type,
            "raw_subtype": str(raw_event.get("subtype") or ""),
        },
        "raw",
        session_id=str(raw_event.get("thread_id") or ""),
    )
    return events


def build_unified_events(
    raw_event: dict[str, Any],
    *,
    sequence_start: int,
    decision: RouteDecision,
    context: RuntimeExecutionContext,
) -> list[UnifiedEvent]:
    if decision.runtime_family == RUNTIME_FAMILY_CLAUDE_CODE:
        return build_claude_unified_events(
            raw_event,
            sequence_start=sequence_start,
            decision=decision,
            context=context,
        )
    if decision.runtime_family == RUNTIME_FAMILY_CODEX:
        return build_codex_unified_events(
            raw_event,
            sequence_start=sequence_start,
            decision=decision,
            context=context,
        )
    return [
        build_lifecycle_event(
            sequence=sequence_start,
            decision=decision,
            context=context,
            event_name="passthrough",
            payload={"raw_event": raw_event},
            session_id=str(raw_event.get("session_id") or raw_event.get("thread_id") or ""),
        )
    ]


def _coerce_tool_input(tool_input: Any) -> dict[str, Any]:
    if isinstance(tool_input, dict):
        return tool_input
    return {"value": tool_input}


def _tool_input_text(tool_input: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list) and value:
            return " ".join(str(item).strip() for item in value if str(item).strip())
    return ""


def _tool_display_name(tool_name: str) -> str:
    raw = str(tool_name or "").strip()
    if not raw:
        return "工具"
    return raw.rsplit(".", 1)[-1].split("__")[-1] or raw


def _command_from_tool(tool_name: str, tool_input: dict[str, Any], explicit_command: str = "") -> str:
    command = str(explicit_command or "").strip()
    if command:
        return command
    value = tool_input.get("cmd")
    if isinstance(value, list):
        return " ".join(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, str):
        return value.strip()
    return _tool_input_text(tool_input, ("command", "shell_command"))


def _classify_work_progress(tool_name: str, tool_input: dict[str, Any], command: str) -> tuple[str, str]:
    lowered_name = str(tool_name or "").lower()
    lowered_command = str(command or "").lower()
    target = _tool_input_text(
        tool_input,
        (
            "relative_path",
            "path",
            "file_path",
            "file",
            "filename",
            "uri",
            "query",
            "substring_pattern",
            "regex",
            "pattern",
            "q",
        ),
    )
    if command:
        if _TEST_COMMAND_RE.search(command):
            return "test", command
        if re.search(r"\b(rg|grep|find)\b", lowered_command):
            return "search", command
        if re.search(r"\b(cat|sed|awk|head|tail|nl|ls|tree|git\s+(show|diff|status|log))\b", lowered_command):
            return "read", command
        return "command", command
    if any(token in lowered_name for token in ("apply_patch", "replace_symbol", "insert_", "rename_symbol")):
        return "edit", target
    if any(token in lowered_name for token in ("safe_delete", "delete_symbol")):
        return "edit", target
    if any(token in lowered_name for token in ("search", "find", "grep", "rg")):
        return "search", target
    if any(token in lowered_name for token in ("read", "open", "fetch", "get_symbol", "get_diagnostics", "overview")):
        return "read", target
    if "web_search" in lowered_name or "web.run" in lowered_name:
        return "web", target
    if any(token in lowered_name for token in ("test", "pytest", "vitest", "jest")):
        return "test", target
    return "tool", target or _tool_display_name(tool_name)


def _work_progress_title(kind: str, status: str) -> str:
    verb = {
        "read": "读取文件",
        "search": "搜索代码",
        "edit": "修改代码",
        "command": "执行命令",
        "test": "运行测试",
        "web": "检索资料",
        "tool": "调用工具",
    }.get(kind, "处理任务")
    if status == "completed":
        return verb + "完成"
    if status == "failed":
        return verb + "失败"
    return verb


def _work_progress_status_label(status: str) -> str:
    return {
        "started": "开始",
        "running": "进行中",
        "completed": "完成",
        "failed": "失败",
    }.get(status, status or "进行中")


def _build_work_progress_payload(
    *,
    tool_name: str,
    tool_input: Any,
    status: str,
    call_id: str = "",
    command: str = "",
    exit_code: Any = None,
    output: str = "",
) -> dict[str, Any]:
    input_dict = _coerce_tool_input(tool_input)
    command_text = _command_from_tool(tool_name, input_dict, explicit_command=command)
    kind, target = _classify_work_progress(tool_name, input_dict, command_text)
    normalized_status = str(status or "running").strip() or "running"
    title = _work_progress_title(kind, normalized_status)
    summary_target = target or command_text or _tool_display_name(tool_name)
    if normalized_status == "completed":
        summary = f"{title}：{summary_target}" if summary_target else title
    elif normalized_status == "failed":
        summary = f"{title}：{summary_target}" if summary_target else title
    else:
        summary = f"{title}：{summary_target}" if summary_target else title
    payload = {
        "kind": kind,
        "kind_label": {
            "read": "读取",
            "search": "搜索",
            "edit": "修改",
            "command": "命令",
            "test": "测试",
            "web": "检索",
            "tool": "工具",
        }.get(kind, "过程"),
        "status": normalized_status,
        "status_label": _work_progress_status_label(normalized_status),
        "title": title,
        "summary": _truncate_text(summary, limit=240),
        "detail": _truncate_text(str(target or ""), limit=500),
        "command": _truncate_text(command_text, limit=500),
        "tool_name": str(tool_name or ""),
        "tool_display_name": _tool_display_name(tool_name),
        "tool_input": _summarize_tool_input(input_dict),
        "call_id": str(call_id or ""),
        "output": _truncate_text(str(output or ""), limit=800),
    }
    if exit_code is not None:
        payload["exit_code"] = exit_code
    return payload


def _summarize_tool_input(tool_input: Any) -> dict[str, Any]:
    if not isinstance(tool_input, dict):
        return {"value": str(tool_input)[:200]}

    summary: dict[str, Any] = {}
    for key, value in tool_input.items():
        if isinstance(value, str):
            summary[key] = value[:200]
        elif isinstance(value, (int, float, bool)) or value is None:
            summary[key] = value
        elif isinstance(value, list):
            summary[key] = f"list[{len(value)}]"
        elif isinstance(value, dict):
            summary[key] = f"dict[{len(value)}]"
        else:
            summary[key] = str(value)[:200]
    return summary


def _normalize_codex_tool_input(payload: dict[str, Any]) -> Any:
    arguments = payload.get("arguments")
    if isinstance(arguments, str) and arguments.strip():
        try:
            return json.loads(arguments)
        except Exception:
            return {"arguments": arguments[:200]}
    if isinstance(payload.get("input"), str):
        return {"input": str(payload.get("input") or "")[:200]}
    if isinstance(payload.get("action"), dict):
        return payload.get("action")
    return {"call_id": payload.get("call_id", "")}


def _extract_codex_reasoning_text(payload: dict[str, Any]) -> str:
    for key in ("text", "delta", "content"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:2400]
    summary = payload.get("summary", [])
    if isinstance(summary, list):
        parts: list[str] = []
        for item in summary:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("summary") or ""))
        text = " ".join(part.strip() for part in parts if part and part.strip())
        if text:
            return text[:2400]
    content = payload.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
            elif isinstance(item, dict):
                piece = str(item.get("text") or item.get("content") or item.get("summary") or "").strip()
                if piece:
                    parts.append(piece)
        joined = "\n".join(parts).strip()
        if joined:
            return joined[:2400]
    return ""


def _extract_codex_item_text(item: dict[str, Any]) -> str:
    text = str(item.get("text") or "").strip()
    if text:
        return text
    content = item.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_text = str(block.get("text") or block.get("content") or "").strip()
            if block_text:
                parts.append(block_text)
        joined = "\n".join(parts).strip()
        if joined:
            return joined[:1000]
    return ""


def _extract_codex_item_result_text(item: dict[str, Any]) -> str:
    error = str(item.get("error") or "").strip()
    if error:
        return error
    result = item.get("result")
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_text = str(block.get("text") or "").strip()
                if block_text:
                    parts.append(block_text)
            joined = "\n".join(parts).strip()
            if joined:
                return joined
        serialized = json.dumps(result, ensure_ascii=False)
        if serialized and serialized != "{}":
            return serialized
    return str(item.get("message") or "").strip()


def _codex_tool_label(item: dict[str, Any]) -> str:
    server = str(item.get("server") or "").strip()
    tool = str(item.get("tool") or item.get("name") or "").strip()
    if server and tool:
        return f"{server}.{tool}"
    return tool or "tool"


def _truncate_text(value: str, *, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."
