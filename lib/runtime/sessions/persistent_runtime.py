from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import select
import signal
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from agent_runner import AgentError, AgentRateLimitError
from lib.mcp_runtime import (
    build_codex_mcp_config_overrides,
    build_main_session_mcp_config,
    build_main_session_mcp_env,
)
from lib.project_identity import normalize_runtime_path
from lib.runtime.codex_cli import build_codex_interactive_automation_args, resolve_codex_main_session_sandbox
from lib.settings_handler import infer_main_session_display_model

from ..events import _extract_codex_reasoning_text, utcnow_iso

logger = logging.getLogger("persistent_main_session_runtime")

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
CLAUDE_TRANSCRIPT_SCAN_LIMIT = 48
CLAUDE_SESSION_DISCOVERY_MAX_WAIT = 8.0
CLAUDE_SESSION_DISCOVERY_INTERVAL = 0.4

CODEX_TRANSCRIPT_SCAN_LIMIT = 64
CODEX_SESSION_DISCOVERY_MAX_WAIT = 20.0
CODEX_SESSION_DISCOVERY_INTERVAL = 0.4
DEFAULT_CODEX_HOME = Path.home() / ".codex"
CODEX_SESSIONS_DIR = DEFAULT_CODEX_HOME / "sessions"

TURN_POLL_INTERVAL = 0.2
STARTUP_GRACE_SECONDS = 1.2
STARTUP_READY_BUFFER_LIMIT = 16384
CLAUDE_THINKING_END_TURN_GRACE_SECONDS = 15.0
CLAUDE_THINKING_ONLY_RETRY_PROMPT = (
    "上一条回复只有思考过程，没有可展示正文。请直接用中文给出最终答复，"
    "不要输出思考过程或英文分析。"
)
CODEX_EMPTY_MESSAGE_RETRY_PROMPT = (
    "上一轮 Codex CLI 没有返回可展示的 assistant message。请直接用中文回答上一条用户问题，"
    "如果需要可以继续查看本地文件，但不要只结束回合。"
)
BRACKETED_PASTE_START = "\x1b[200~"
BRACKETED_PASTE_END = "\x1b[201~"
PTY_WRITE_CHUNK_SIZE = 4096
PTY_WRITE_TIMEOUT_SECONDS = 30.0
RUNTIME_PROGRESS_BUFFER_LIMIT = 4096
TURN_EVENT_TIMESTAMP_SKEW_SECONDS = 2.0

ANSI_OSC_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")
ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

_RUNTIME_PID_KEY = "persistent_runtime_pid"
_RUNTIME_STARTED_AT_KEY = "persistent_runtime_started_at"
_RUNTIME_HEARTBEAT_KEY = "persistent_runtime_heartbeat_at"
_RUNTIME_NATIVE_PATH_KEY = "persistent_runtime_transcript_path"


def _same_runtime_path(left: str | None, right: str | None) -> bool:
    return bool(left and right) and normalize_runtime_path(left) == normalize_runtime_path(right)


def _transcript_path_known(path: Path | None) -> bool:
    return bool(path and str(path) not in {"", "."})


def _sanitize_runtime_segment(raw: str | None, fallback: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in str(raw or "").strip())
    cleaned = cleaned.strip(".-")
    return cleaned or fallback


def _strip_ansi_runtime_text(text: str) -> str:
    without_osc = ANSI_OSC_RE.sub("", str(text or ""))
    return ANSI_ESCAPE_RE.sub("", without_osc).replace("\x00", "")


def _extract_codex_mcp_approval_prompt(text: str) -> dict[str, str] | None:
    plain = _strip_ansi_runtime_text(text).replace("\r", "\n")
    compact_chars: list[str] = []
    compact_to_plain: list[int] = []
    for index, char in enumerate(plain):
        if char.isspace():
            continue
        compact_chars.append(char)
        compact_to_plain.append(index)
    compact = "".join(compact_chars)
    if "1.Allow" not in compact or "2.Allowforthissession" not in compact:
        return None
    prompt_pattern = re.compile(
        r"Allowthe(?P<server>[A-Za-z0-9_.-]+)MCPservertoruntool[\"“]?(?P<tool>[A-Za-z0-9_.-]+)[\"”]?\?",
    )
    match = None
    option_start = -1
    for candidate in prompt_pattern.finditer(compact):
        suffix = compact[candidate.end():]
        candidate_option_start = suffix.find("1.Allow")
        if candidate_option_start < 0:
            continue
        if "2.Allowforthissession" not in suffix[candidate_option_start:]:
            continue
        match = candidate
        option_start = candidate_option_start
    if not match:
        return None
    server = match.group("server").strip()
    tool = match.group("tool").strip()
    args = compact[match.end(): match.end() + option_start]
    signature = hashlib.sha1(f"{server}:{tool}:{args[:240]}".encode("utf-8")).hexdigest()[:16]
    request_id = f"mcp_{signature}"
    detail_start = compact_to_plain[match.start()] if compact_to_plain else 0
    detail_end_compact = min(len(compact_to_plain) - 1, match.end() + option_start + 1)
    detail_end = compact_to_plain[detail_end_compact] + 1 if detail_end_compact >= 0 else len(plain)
    detail_window = plain[detail_start:detail_end]
    detail_lines = [line.strip() for line in detail_window.splitlines() if line.strip()]
    details = "\n".join(detail_lines[-12:])[-1200:]
    return {
        "request_id": request_id,
        "signature": signature,
        "server": server,
        "tool": tool,
        "details": details,
    }


def _extract_claude_permission_prompt(text: str) -> dict[str, Any] | None:
    plain = _strip_ansi_runtime_text(text).replace("\r", "\n")
    tail = plain[-5000:]
    if not tail.strip():
        return None
    lowered = tail.lower()

    def visible_lines(window: str) -> list[str]:
        lines: list[str] = []
        for raw_line in window.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            lines.append(line)
        return lines

    def infer_command(lines: list[str]) -> str:
        ignored_prefixes = (
            "allow",
            "do you want",
            "bash command",
            "mcp tool",
            "tool use",
            "1.",
            "2.",
            "3.",
            "4.",
            ">",
            "❯",
        )
        ignored_exact = {"╭", "╰", "│", "┌", "└", "─", "━"}
        for line in reversed(lines):
            normalized = line.strip("│┃╭╮╰╯┌┐└┘─━ ")
            normalized = re.sub(r"^[›❯>]\s*", "", normalized).strip()
            if not normalized:
                continue
            lowered_line = normalized.lower()
            if any(lowered_line.startswith(prefix) for prefix in ignored_prefixes):
                continue
            if set(normalized) <= ignored_exact:
                continue
            return normalized[:500]
        return ""

    def build_result(
        *,
        style: str,
        marker_index: int,
        detail_end: int | None,
        available_actions: list[str],
        response_keys: dict[str, str],
    ) -> dict[str, Any]:
        window_start = max(0, marker_index - 1600)
        window_end = min(len(tail), detail_end if detail_end is not None else marker_index + 1200)
        lines = visible_lines(tail[window_start:window_end])
        details = "\n".join(lines[-14:])[-1400:]
        command = infer_command(lines)
        signature_basis = f"{style}:{command}:{details}"
        signature = hashlib.sha1(signature_basis.encode("utf-8")).hexdigest()[:16]
        return {
            "request_id": f"claude_perm_{signature}",
            "signature": signature,
            "prompt_style": style,
            "details": details,
            "command": command,
            "available_actions": available_actions,
            "response_keys": response_keys,
        }

    numbered_index = lowered.rfind("do you want to proceed?")
    if numbered_index >= 0:
        option_tail = lowered[numbered_index:]
        if re.search(r"(?:^|\s)1\.\s*yes\b", option_tail) and re.search(
            r"(?:^|\s)3\.\s*no\b", option_tail
        ):
            return build_result(
                style="numbered",
                marker_index=numbered_index,
                detail_end=len(tail),
                available_actions=["approve_once", "approve_prefix", "deny"],
                response_keys={"approve_once": "1\r", "approve_prefix": "2\r", "deny": "3\r"},
            )

    yn_start = -1
    yn_end = -1
    for candidate in re.finditer(r"allow[\s\S]{0,700}?\?\s*\(y/n\)", tail, flags=re.IGNORECASE):
        yn_start = candidate.start()
        yn_end = candidate.end()
    if yn_start < 0 and "allow this action" in lowered:
        allow_index = lowered.rfind("allow this action")
        if allow_index >= 0:
            fallback_match = re.search(r"allow this action[\s\S]{0,700}", tail[allow_index:], flags=re.IGNORECASE)
            if fallback_match:
                yn_start = allow_index + fallback_match.start()
                yn_end = allow_index + fallback_match.end()
    if yn_start >= 0:
        return build_result(
            style="yn",
            marker_index=yn_start,
            detail_end=yn_end,
            available_actions=["approve_once", "deny"],
            response_keys={"approve_once": "y\r", "deny": "n\r"},
        )

    return None


def _extract_claude_runtime_progress_events(text: str) -> list[dict[str, str]]:
    normalized = _strip_ansi_runtime_text(text).replace("\r", "\n")
    events: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    baked_pattern = re.compile(r"\bBaked for \d+s\b")
    for raw_line in normalized.splitlines():
        line = " ".join(raw_line.split()).strip()
        if not line:
            continue
        subtype = ""
        payload = ""
        if "SessionStart:startup says:" in line:
            payload = line.split("SessionStart:startup says:", 1)[1].strip()
            subtype = "startup_hint"
        elif line.startswith("running "):
            payload = line
            subtype = "runtime_progress"
        else:
            baked_match = baked_pattern.search(line)
            if baked_match:
                payload = baked_match.group(0).strip()
                subtype = "runtime_progress"
        if not payload or not subtype:
            continue
        key = (subtype, payload)
        if key in seen:
            continue
        seen.add(key)
        events.append({"subtype": subtype, "text": payload})
    return events


def resolve_connection_codex_home(project_root: Path, connection: dict, *, fallback_key: str = "main_session") -> Path:
    raw_path = str(connection.get("codex_home") or "").strip()
    if raw_path:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = (Path(project_root) / path).resolve()
        return path

    connection_id = _sanitize_runtime_segment(str(connection.get("connection_id") or ""), fallback_key)
    return (Path(project_root) / ".vizo" / "codex" / "connections" / connection_id).resolve()


def _pid_is_zombie(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        stat_text = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    _, _, tail = stat_text.partition(") ")
    state = tail[:1]
    return state == "Z"


def _load_posix_runtime_modules() -> tuple[Any, Any, Any, Any]:
    try:
        import fcntl as fcntl_module
        import pty as pty_module
        import struct as struct_module
        import termios as termios_module
    except ModuleNotFoundError as error:
        raise AgentError(
            "主会话常驻 runtime 仅支持 POSIX/WSL 环境",
            error_code="persistent_runtime_unsupported_platform",
        ) from error
    return fcntl_module, pty_module, struct_module, termios_module


def _load_pty_manager_helpers() -> tuple[str, Callable[[dict[str, str], int, int], dict[str, str]]]:
    try:
        from lib.pty_manager import CLAUDE_BIN, _build_main_session_child_env
    except ModuleNotFoundError as error:
        raise AgentError(
            "主会话常驻 runtime 依赖 PTY 环境，当前平台无法加载",
            error_code="persistent_runtime_unsupported_platform",
        ) from error
    return CLAUDE_BIN, _build_main_session_child_env


@dataclass
class RuntimeArtifact:
    native_session_id: str
    transcript_path: Path
    started_at: float | None = None


class BasePersistentSessionRuntime:
    runtime_family = ""

    def __init__(
        self,
        *,
        session_id: str,
        project_root: Path,
        config: dict,
        raw_output_cb: Callable[[str], None] | None = None,
    ) -> None:
        self.session_id = session_id
        self.project_root = Path(project_root)
        self.config = config
        self.raw_output_cb = raw_output_cb
        self.pid = 0
        self.master_fd = -1
        self.started_at = 0.0
        self.last_output_at = 0.0
        self.native_session_id = ""
        self.transcript_path = Path()
        self._first_output_at = 0.0
        self._recent_output = ""
        self._reader_task: asyncio.Task | None = None
        self._artifact_task: asyncio.Task | None = None
        self._turn_lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._startup_input_attempts = 0
        self._startup_prompt = ""
        self._startup_prompt_sent_at = 0.0
        self._startup_prompt_output_checkpoint = 0.0
        self._startup_prompt_retry_count = 0
        self._startup_log_mtime_before = 0.0
        self._codex_history_path = Path()
        self._stream_event_cb: Callable[[dict[str, Any]], None] | None = None
        self._runtime_progress_buffer = ""
        self._runtime_progress_seen: set[tuple[str, str]] = set()

    def is_alive(self) -> bool:
        if self.pid <= 0:
            return False
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        if _pid_is_zombie(self.pid):
            return False
        return True

    async def start(self, *, session, connection: dict) -> None:
        if self.is_alive():
            return
        self._loop = asyncio.get_running_loop()
        self.native_session_id = ""
        self.transcript_path = Path()
        self._reset_startup_prompt_state()
        self._prepare_start(session=session, connection=connection)
        fcntl_module, pty_module, _, _ = _load_posix_runtime_modules()
        child_pid, master_fd = pty_module.fork()
        if child_pid == 0:
            self._exec_child(session=session, connection=connection)
            os._exit(1)

        self.pid = child_pid
        self.master_fd = master_fd
        self.started_at = time.time()
        self.last_output_at = self.started_at
        self._first_output_at = 0.0
        self._recent_output = ""
        self._startup_input_attempts = 0

        flags = fcntl_module.fcntl(master_fd, fcntl_module.F_GETFL)
        fcntl_module.fcntl(master_fd, fcntl_module.F_SETFL, flags | os.O_NONBLOCK)

        self._reader_task = asyncio.create_task(self._reader_loop())
        self._artifact_task = asyncio.create_task(self._discover_artifact_loop(session.cwd))
        await asyncio.sleep(STARTUP_GRACE_SECONDS)

    async def shutdown(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self._artifact_task:
            self._artifact_task.cancel()
            try:
                await self._artifact_task
            except (asyncio.CancelledError, Exception):
                pass
            self._artifact_task = None
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            else:
                for _ in range(40):
                    if not self.is_alive():
                        break
                    await asyncio.sleep(0.1)
                if self.is_alive():
                    try:
                        os.kill(self.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        if self.master_fd >= 0:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
        self.pid = 0
        self.master_fd = -1
        self.native_session_id = ""
        self.transcript_path = Path()
        self._reset_startup_prompt_state()

    async def interrupt_turn(self, *, force: bool = False) -> dict[str, Any]:
        if force:
            await self.shutdown()
            return {"mode": "force", "runtime_stopped": True}
        if self.master_fd < 0:
            raise AgentError("主会话常驻 runtime 尚未启动", error_code="persistent_runtime_unavailable")
        self._write_master_bytes(b"\x03")
        return {"mode": "soft", "signal": "ctrl_c", "runtime_stopped": False}

    async def run_turn(
        self,
        *,
        session,
        decision,
        prompt: str,
        connection: dict,
        on_stream_event=None,
    ) -> dict[str, Any]:
        async with self._turn_lock:
            self._stream_event_cb = on_stream_event
            self._runtime_progress_buffer = ""
            self._runtime_progress_seen.clear()
            try:
                await self.start(session=session, connection=connection)
                offset = 0
                prompt_sent_at: float | None = None
                artifact_known_before_turn = bool(self.native_session_id) and _transcript_path_known(self.transcript_path)
                prompt_sent_before_artifact = self._should_send_prompt_before_artifact() and not artifact_known_before_turn
                if prompt_sent_before_artifact:
                    await self._wait_until_ready_before_first_prompt()
                    prompt_sent_at = time.time()
                    await self._send_prompt_async(prompt)
                    self._record_startup_prompt(prompt)
                artifact = await self._wait_for_artifact(session.cwd)
                if not prompt_sent_before_artifact and artifact.transcript_path.exists():
                    offset = artifact.transcript_path.stat().st_size
                if not artifact_known_before_turn:
                    self._emit_init_event(session, decision, on_stream_event)
                self._emit_turn_started_event(artifact.native_session_id, on_stream_event)
                if not prompt_sent_before_artifact:
                    prompt_sent_at = time.time()
                    await self._send_prompt_async(prompt)
                return await self._wait_for_turn_result(
                    session=session,
                    decision=decision,
                    artifact=artifact,
                    offset=offset,
                    turn_started_at=prompt_sent_at,
                    on_stream_event=on_stream_event,
                )
            finally:
                self._stream_event_cb = None
                self._runtime_progress_buffer = ""
                self._runtime_progress_seen.clear()

    async def submit_interaction_response(
        self,
        *,
        request_id: str,
        action: str,
        text: str = "",
    ) -> dict[str, Any]:
        del request_id, action, text
        raise AgentError(
            "当前主会话 runtime 暂不支持交互请求。",
            error_code="interactive_request_unavailable",
        )

    async def _reader_loop(self) -> None:
        assert self._loop is not None
        output_ready = asyncio.Event()

        def _on_readable() -> None:
            output_ready.set()

        try:
            self._loop.add_reader(self.master_fd, _on_readable)
            while True:
                await output_ready.wait()
                output_ready.clear()
                try:
                    data = os.read(self.master_fd, 65536)
                    if not data:
                        break
                except (OSError, BlockingIOError) as error:
                    if isinstance(error, BlockingIOError):
                        continue
                    break
                text = data.decode("utf-8", errors="replace")
                self._remember_output(text)
                self._emit_runtime_progress_from_output(text)
                await self._handle_runtime_output_interactions(text)
                if self.raw_output_cb:
                    self.raw_output_cb(text)
        except asyncio.CancelledError:
            return
        finally:
            try:
                self._loop.remove_reader(self.master_fd)
            except Exception:
                pass

    def _send_prompt(self, prompt: str) -> None:
        if self.master_fd < 0:
            raise AgentError("主会话常驻 runtime 尚未启动", error_code="persistent_runtime_unavailable")
        payload = BRACKETED_PASTE_START + str(prompt or "") + BRACKETED_PASTE_END + "\r"
        self._write_master_bytes(payload.encode("utf-8"))

    def _write_master_bytes(self, payload: bytes | bytearray | memoryview) -> None:
        if self.master_fd < 0:
            raise AgentError("主会话常驻 runtime 尚未启动", error_code="persistent_runtime_unavailable")
        data = memoryview(bytes(payload))
        total = len(data)
        if total <= 0:
            return
        chunk_size = max(int(self.config.get("main_session_pty_write_chunk_size", PTY_WRITE_CHUNK_SIZE)), 1)
        timeout = max(float(self.config.get("main_session_pty_write_timeout", PTY_WRITE_TIMEOUT_SECONDS)), 1.0)
        deadline = time.time() + timeout
        offset = 0
        while offset < total:
            if self.pid and not self.is_alive():
                raise AgentError("主会话常驻 runtime 已退出", error_code="persistent_runtime_exited")
            try:
                end = min(offset + chunk_size, total)
                written = os.write(self.master_fd, data[offset:end])
                if written <= 0:
                    raise BlockingIOError()
                offset += written
                continue
            except InterruptedError:
                continue
            except BlockingIOError:
                pass
            except OSError as error:
                raise AgentError(
                    "主会话常驻 runtime 写入失败",
                    error_code="persistent_runtime_write_failed",
                ) from error

            remaining = deadline - time.time()
            if remaining <= 0:
                raise AgentError(
                    "主会话常驻 runtime 写入 prompt 超时",
                    error_code="persistent_runtime_write_timeout",
                )
            try:
                select.select([], [self.master_fd], [], min(0.1, remaining))
            except (InterruptedError, BlockingIOError):
                continue
            except OSError:
                time.sleep(min(0.05, remaining))

    def _emit_runtime_progress_from_output(self, text: str) -> None:
        if self.runtime_family != "claude_code" or not self._stream_event_cb:
            return
        plain = _strip_ansi_runtime_text(text).replace("\r", "\n")
        if not plain.strip():
            return
        self._runtime_progress_buffer = (self._runtime_progress_buffer + "\n" + plain)[-RUNTIME_PROGRESS_BUFFER_LIMIT:]
        for item in _extract_claude_runtime_progress_events(self._runtime_progress_buffer):
            key = (str(item.get("subtype") or ""), str(item.get("text") or ""))
            if not all(key) or key in self._runtime_progress_seen:
                continue
            self._runtime_progress_seen.add(key)
            try:
                self._stream_event_cb(
                    {
                        "type": "status",
                        "subtype": key[0],
                        "text": key[1],
                        "session_id": self.native_session_id,
                    }
                )
            except Exception:
                logger.debug("emit runtime progress failed", exc_info=True)

    async def _handle_runtime_output_interactions(self, text: str) -> None:
        del text
        return

    async def _send_prompt_async(self, prompt: str) -> None:
        await asyncio.to_thread(self._send_prompt, prompt)

    def _send_startup_input(self, text: str) -> None:
        if self.master_fd < 0:
            raise AgentError("主会话常驻 runtime 尚未启动", error_code="persistent_runtime_unavailable")
        self._write_master_bytes(str(text or "").encode("utf-8"))

    async def _discover_artifact_loop(self, cwd: str) -> None:
        deadline = time.time() + self._discovery_timeout
        while self.is_alive() and not self.native_session_id and time.time() < deadline:
            artifact = self._infer_artifact(cwd)
            if artifact:
                self.native_session_id = artifact.native_session_id
                self.transcript_path = artifact.transcript_path
                return
            try:
                await asyncio.sleep(self._discovery_interval)
            except asyncio.CancelledError:
                return

    async def _wait_for_artifact(self, cwd: str) -> RuntimeArtifact:
        deadline = time.time() + self._discovery_timeout
        while time.time() < deadline:
            if self.pid and not self.is_alive():
                raise AgentError("主会话常驻 runtime 已退出", error_code="persistent_runtime_exited")
            if self.native_session_id and _transcript_path_known(self.transcript_path):
                self._reset_startup_prompt_state()
                return RuntimeArtifact(
                    native_session_id=self.native_session_id,
                    transcript_path=self.transcript_path,
                    started_at=self.started_at,
                )
            artifact = self._infer_artifact(cwd)
            if artifact:
                self.native_session_id = artifact.native_session_id
                self.transcript_path = artifact.transcript_path
                self._reset_startup_prompt_state()
                return artifact
            self._maybe_retry_startup_prompt()
            await asyncio.sleep(self._discovery_interval)
        self._reset_startup_prompt_state()
        raise AgentError("无法识别主会话常驻 runtime 的 native session", error_code="persistent_runtime_unavailable")

    def _remember_output(self, text: str) -> None:
        now = time.time()
        self.last_output_at = now
        if not self._first_output_at:
            self._first_output_at = now
        if text:
            self._recent_output = (self._recent_output + text)[-STARTUP_READY_BUFFER_LIMIT:]

    def _record_startup_prompt(self, prompt: str) -> None:
        self._startup_prompt = str(prompt or "")
        self._startup_prompt_sent_at = time.time()
        self._startup_prompt_output_checkpoint = float(self.last_output_at or 0.0)
        self._startup_prompt_retry_count = 0

    def _reset_startup_prompt_state(self) -> None:
        self._startup_prompt = ""
        self._startup_prompt_sent_at = 0.0
        self._startup_prompt_output_checkpoint = 0.0
        self._startup_prompt_retry_count = 0
        self._startup_log_mtime_before = 0.0

    @property
    def _discovery_timeout(self) -> float:
        return 8.0

    @property
    def _discovery_interval(self) -> float:
        return 0.4

    def snapshot_metadata(self) -> dict[str, Any]:
        started_at = ""
        if self.pid and self.started_at:
            started_at = datetime.utcfromtimestamp(self.started_at).isoformat() + "Z"
        transcript_path = ""
        if self.transcript_path:
            raw_path = str(self.transcript_path)
            if raw_path != ".":
                transcript_path = raw_path
        return {
            _RUNTIME_PID_KEY: int(self.pid or 0),
            _RUNTIME_STARTED_AT_KEY: started_at,
            _RUNTIME_HEARTBEAT_KEY: datetime.utcnow().isoformat() + "Z",
            _RUNTIME_NATIVE_PATH_KEY: transcript_path,
        }

    def _emit_init_event(self, session, decision, on_stream_event) -> None:
        if not on_stream_event:
            return

    def _emit_turn_started_event(self, native_session_id: str, on_stream_event) -> None:
        if not on_stream_event:
            return

    def _should_send_prompt_before_artifact(self) -> bool:
        return False

    async def _wait_until_ready_before_first_prompt(self) -> None:
        return

    def _prepare_start(self, *, session, connection: dict) -> None:
        return

    def _maybe_retry_startup_prompt(self) -> None:
        return

    def _startup_log_recently_active(self) -> bool:
        return False

    def _exec_child(self, *, session, connection: dict) -> None:
        fcntl_module, _, struct_module, termios_module = _load_posix_runtime_modules()
        _, build_main_session_child_env = _load_pty_manager_helpers()
        cols = 120
        rows = 40
        if session.cwd:
            try:
                os.chdir(session.cwd)
            except Exception:
                pass
        try:
            winsize = struct_module.pack("HHHH", rows, cols, 0, 0)
            fcntl_module.ioctl(0, termios_module.TIOCSWINSZ, winsize)
        except Exception:
            pass
        child_env = build_main_session_child_env(os.environ, cols, rows)
        child_env.update(self._build_child_env(session=session, connection=connection))
        os.environ.clear()
        os.environ.update(child_env)
        argv = self._build_command(session=session, connection=connection)
        os.execvp(argv[0], argv)

    async def _wait_for_turn_result(
        self,
        *,
        session,
        decision,
        artifact: RuntimeArtifact,
        offset: int,
        turn_started_at: float | None = None,
        on_stream_event=None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def _infer_artifact(self, cwd: str) -> RuntimeArtifact | None:
        raise NotImplementedError

    def _build_command(self, *, session, connection: dict) -> list[str]:
        raise NotImplementedError

    def _build_child_env(self, *, session, connection: dict) -> dict[str, str]:
        raise NotImplementedError


class ClaudePersistentSessionRuntime(BasePersistentSessionRuntime):
    runtime_family = "claude_code"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._interaction_queue: list[dict[str, Any]] = []
        self._interaction_seen_call_ids: set[str] = set()
        self._interaction_state_lock = asyncio.Lock()
        self._interaction_resume_event = asyncio.Event()
        self._claude_permission_active_signature = ""
        self._claude_permission_active_request_id = ""
        self._claude_permission_counter = 0

    def _reset_interaction_state(self) -> None:
        self._interaction_queue = []
        self._interaction_seen_call_ids = set()
        self._interaction_resume_event = asyncio.Event()
        self._claude_permission_active_signature = ""
        self._claude_permission_active_request_id = ""
        self._claude_permission_counter = 0

    @staticmethod
    def _permission_options(available_actions: list[str]) -> list[dict[str, Any]]:
        labels = {
            "approve_once": "允许一次",
            "approve_prefix": "允许同类",
            "deny": "拒绝",
        }
        return [
            {"id": action, "label": labels.get(action, action), "kind": "button"}
            for action in available_actions
        ]

    def _refresh_interaction_queue_locked(self) -> None:
        total = len(self._interaction_queue)
        for index, item in enumerate(self._interaction_queue, start=1):
            item["queue_index"] = index
            item["queue_total"] = total

    def _current_interaction_locked(self) -> dict[str, Any]:
        if not self._interaction_queue:
            return {}
        return dict(self._interaction_queue[0])

    async def _enqueue_interaction_request(
        self,
        *,
        request: dict[str, Any],
        native_session_id: str,
        on_stream_event=None,
    ) -> None:
        request_id = str(request.get("request_id") or "").strip()
        if not request_id:
            return
        async with self._interaction_state_lock:
            if request_id in self._interaction_seen_call_ids:
                return
            self._interaction_seen_call_ids.add(request_id)
            self._interaction_queue.append(dict(request))
            self._refresh_interaction_queue_locked()
            pending = self._current_interaction_locked()
        if on_stream_event and pending:
            on_stream_event(
                {
                    "type": "interaction_requested",
                    "thread_id": native_session_id,
                    "payload": pending,
                }
            )

    async def _has_pending_interaction(self) -> bool:
        async with self._interaction_state_lock:
            return bool(self._interaction_queue)

    async def _wait_for_interaction_resume(self, deadline: float) -> bool:
        while time.time() < deadline:
            if not self.is_alive():
                raise AgentError("Claude 主会话常驻 runtime 已退出", error_code="persistent_runtime_exited")
            try:
                timeout = min(TURN_POLL_INTERVAL, max(deadline - time.time(), 0.01))
                await asyncio.wait_for(self._interaction_resume_event.wait(), timeout=timeout)
                self._interaction_resume_event.clear()
                return True
            except asyncio.TimeoutError:
                continue
        return False

    async def _handle_runtime_output_interactions(self, text: str) -> None:
        del text
        prompt = _extract_claude_permission_prompt(self._recent_output)
        if not prompt:
            if not await self._has_pending_interaction():
                self._claude_permission_active_signature = ""
                self._claude_permission_active_request_id = ""
            return
        signature = str(prompt.get("signature") or prompt.get("request_id") or "").strip()
        if signature and signature == self._claude_permission_active_signature:
            return
        self._claude_permission_counter += 1
        if signature:
            self._claude_permission_active_signature = signature
        request_id = f"claude_perm_{signature}_{self._claude_permission_counter}" if signature else prompt["request_id"]
        self._claude_permission_active_request_id = request_id
        available_actions = [
            str(item).strip()
            for item in (prompt.get("available_actions") or ["approve_once", "deny"])
            if str(item).strip()
        ]
        command = str(prompt.get("command") or "").strip()
        request = {
            "request_id": request_id,
            "turn_id": self.native_session_id or self.session_id,
            "runtime_family": self.runtime_family,
            "interaction_kind": "approval",
            "title": "允许 Claude 执行操作",
            "summary": f"Claude CLI 请求允许执行：{command}" if command else "Claude CLI 正在等待权限确认。",
            "details": str(prompt.get("details") or ""),
            "command": command,
            "options": self._permission_options(available_actions),
            "available_actions": available_actions,
            "text_required_actions": [],
            "requires_text": False,
            "status": "pending",
            "source": "claude_tui",
            "prompt_style": str(prompt.get("prompt_style") or ""),
            "response_keys": dict(prompt.get("response_keys") or {}),
            "created_at": utcnow_iso(),
        }
        await self._enqueue_interaction_request(
            request=request,
            native_session_id=self.native_session_id or self.session_id,
            on_stream_event=self._stream_event_cb,
        )

    async def submit_interaction_response(
        self,
        *,
        request_id: str,
        action: str,
        text: str = "",
    ) -> dict[str, Any]:
        del text
        action = str(action or "").strip()
        request_id = str(request_id or "").strip()
        if not self.is_alive():
            raise AgentError("Claude 主会话常驻 runtime 已退出", error_code="persistent_runtime_exited")
        if self.master_fd < 0:
            raise AgentError("当前主会话 runtime 暂不支持交互请求。", error_code="interactive_request_unavailable")
        async with self._interaction_state_lock:
            current = self._current_interaction_locked()
            current_request_id = str(current.get("request_id") or "").strip()
            if not current_request_id or current_request_id != request_id:
                raise AgentError("当前交互请求已变化，请刷新后重试。", error_code="interaction_stale")
            response_keys = dict(current.get("response_keys") or {})
            if not response_keys:
                if current.get("prompt_style") == "numbered":
                    response_keys = {"approve_once": "1\r", "approve_prefix": "2\r", "deny": "3\r"}
                else:
                    response_keys = {"approve_once": "y\r", "deny": "n\r"}
            if action not in response_keys:
                raise AgentError("当前交互请求不支持该操作。", error_code="interaction_action_invalid")
            self._write_master_bytes(str(response_keys[action]).encode("utf-8"))
            if action == "deny":
                message = "已拒绝本次操作，主会话继续运行。"
            elif action == "approve_prefix":
                message = "已允许同类操作，主会话继续运行。"
            else:
                message = "已允许本次操作，主会话继续运行。"
            self._interaction_queue.pop(0)
            self._refresh_interaction_queue_locked()
            next_pending = self._current_interaction_locked()
            self._interaction_resume_event.set()
        return {
            "message": message,
            "current_turn_status": "waiting_interaction" if next_pending else "running",
            "pending_interaction": next_pending,
        }

    @property
    def _discovery_timeout(self) -> float:
        return CLAUDE_SESSION_DISCOVERY_MAX_WAIT

    @property
    def _discovery_interval(self) -> float:
        return CLAUDE_SESSION_DISCOVERY_INTERVAL

    def _should_send_prompt_before_artifact(self) -> bool:
        # Claude only starts writing its transcript after the first user message
        # is submitted, so waiting for an artifact before sending the first
        # prompt deadlocks brand-new runtimes.
        return True

    async def _wait_until_ready_before_first_prompt(self) -> None:
        quiet_period = max(float(self.config.get("main_session_claude_startup_quiet_period", 0.25)), 0.05)
        min_wait = max(float(self.config.get("main_session_claude_startup_min_wait", 1.5)), STARTUP_GRACE_SECONDS)
        ready_timeout = max(float(self.config.get("main_session_claude_startup_ready_timeout", 6.0)), min_wait)
        hard_timeout = max(
            float(self.config.get("main_session_claude_startup_force_prompt_timeout", 12.0)),
            ready_timeout,
        )
        started = time.time()
        ready_deadline = started + ready_timeout
        hard_deadline = started + hard_timeout
        while time.time() < hard_deadline:
            if not self.is_alive():
                raise AgentError("Claude 主会话常驻 runtime 已退出", error_code="persistent_runtime_exited")
            now = time.time()
            if (
                self._claude_startup_ready_marker_seen()
                and self._first_output_at
                and (now - self.started_at) >= min_wait
                and (now - self.last_output_at) >= quiet_period
            ):
                return
            if now >= ready_deadline:
                if not self._first_output_at:
                    return
                if (now - self.last_output_at) >= quiet_period:
                    return
            await asyncio.sleep(0.05)

    def _claude_startup_ready_marker_seen(self) -> bool:
        text = self._recent_output
        return any(
            marker in text
            for marker in (
                "SessionStart:startup says",
                "Welcome back!",
                "Welcome to Claude Code",
                "/resume",
                "INSERT",
                "❯",
            )
        )

    def _send_prompt(self, prompt: str) -> None:
        if self.master_fd < 0:
            raise AgentError("主会话常驻 runtime 尚未启动", error_code="persistent_runtime_unavailable")
        # Keep a synchronous fallback for tests and unexpected direct callers.
        self._recent_output = ""
        self._write_master_bytes(b"a")
        self._write_master_bytes(b"\x15")
        self._write_master_bytes(str(prompt or "").encode("utf-8"))
        self._write_master_bytes(b"\r")

    async def _send_prompt_async(self, prompt: str) -> None:
        if self.master_fd < 0:
            raise AgentError("主会话常驻 runtime 尚未启动", error_code="persistent_runtime_unavailable")
        prefix_delay = max(float(self.config.get("main_session_claude_input_prefix_delay", 0.1)), 0.0)
        settle_delay = max(float(self.config.get("main_session_claude_input_settle_delay", 0.05)), 0.0)
        paste_submit_wait = max(float(self.config.get("main_session_claude_paste_submit_wait", 0.35)), settle_delay)
        paste_ack_timeout = max(float(self.config.get("main_session_claude_paste_ack_timeout", 1.2)), paste_submit_wait)
        self._recent_output = ""
        self._write_master_bytes(b"a")
        if prefix_delay > 0:
            await asyncio.sleep(prefix_delay)
        self._write_master_bytes(b"\x15")
        if settle_delay > 0:
            await asyncio.sleep(settle_delay)
        output_checkpoint = float(self.last_output_at or 0.0)
        await asyncio.to_thread(self._write_master_bytes, str(prompt or "").encode("utf-8"))
        deadline = time.time() + paste_ack_timeout
        while time.time() < deadline:
            if self.last_output_at > (output_checkpoint + 1e-6):
                if "[Pasted text" in self._recent_output or "Pasting text" in self._recent_output:
                    break
            await asyncio.sleep(0.05)
        if paste_submit_wait > 0:
            await asyncio.sleep(paste_submit_wait)
        self._write_master_bytes(b"\r")

    async def interrupt_turn(self, *, force: bool = False) -> dict[str, Any]:
        async with self._interaction_state_lock:
            self._interaction_queue.clear()
            self._claude_permission_active_signature = ""
            self._claude_permission_active_request_id = ""
            self._interaction_resume_event.set()
        if force:
            return await super().interrupt_turn(force=True)
        if self.master_fd < 0:
            raise AgentError("主会话常驻 runtime 尚未启动", error_code="persistent_runtime_unavailable")
        delay = max(float(self.config.get("main_session_interrupt_ctrl_c_delay", 0.2)), 0.0)
        self._write_master_bytes(b"\x1b")
        if delay > 0:
            await asyncio.sleep(delay)
        self._write_master_bytes(b"\x03")
        return {"mode": "soft", "signal": "esc_ctrl_c", "runtime_stopped": False}

    def _prepare_start(self, *, session, connection: dict) -> None:
        resume_token = str(getattr(session, "resume_token", "") or "").strip()
        if resume_token:
            resume_path = _resolve_claude_transcript_path(resume_token)
            if _transcript_path_known(resume_path):
                self.native_session_id = resume_token
                self.transcript_path = resume_path

    def _build_command(self, *, session, connection: dict) -> list[str]:
        claude_bin, _ = _load_pty_manager_helpers()
        command = [claude_bin]
        display_model = str(getattr(session, "display_model", "") or "sonnet")
        if session.resume_token:
            command.extend(["--resume", str(session.resume_token)])
        command.extend(["--model", display_model])
        metadata = dict(getattr(session, "metadata", {}) or {})
        mcp_config = build_main_session_mcp_config(
            str(session.cwd or ""),
            confirm_port=int(self.config.get("confirm_server", {}).get("port", 9390)),
            config_data=self.config,
            session_id=str(getattr(session, "session_id", "") or ""),
            runtime_dir=str(metadata.get("runtime_dir") or ""),
            project_root=str(metadata.get("project_root") or session.cwd or ""),
        )
        command.extend(["--mcp-config", json.dumps(mcp_config, ensure_ascii=False), "--strict-mcp-config"])
        return command

    def _build_child_env(self, *, session, connection: dict) -> dict[str, str]:
        metadata = dict(getattr(session, "metadata", {}) or {})
        env = dict(connection.get("env", {}) or {})
        env.update(
            build_main_session_mcp_env(
                session_id=str(getattr(session, "session_id", "") or ""),
                runtime_dir=str(metadata.get("runtime_dir") or ""),
                project_root=str(metadata.get("project_root") or session.cwd or ""),
            )
        )
        api_key = str(connection.get("api_key") or "").strip()
        if api_key:
            env["ANTHROPIC_AUTH_TOKEN"] = api_key
            env.pop("ANTHROPIC_API_KEY", None)
        runtime_base_url = str(connection.get("runtime_base_url") or connection.get("base_url") or "").strip()
        if runtime_base_url and runtime_base_url != "https://api.anthropic.com":
            env["ANTHROPIC_BASE_URL"] = runtime_base_url
            env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
        return env

    def _infer_artifact(self, cwd: str) -> RuntimeArtifact | None:
        if not CLAUDE_PROJECTS_DIR.exists():
            return None
        best_path: Path | None = None
        best_id = ""
        best_delta: float | None = None
        try:
            candidates = sorted(
                CLAUDE_PROJECTS_DIR.rglob("*.jsonl"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except Exception:
            return None

        for path in candidates[:CLAUDE_TRANSCRIPT_SCAN_LIMIT]:
            meta = _read_claude_transcript_start_meta(path)
            if not meta:
                continue
            native_session_id = str(meta.get("native_session_id") or "")
            started_at = meta.get("started_at")
            transcript_cwd = str(meta.get("cwd") or "")
            if not native_session_id or not _same_runtime_path(transcript_cwd, cwd):
                continue
            if started_at is None:
                continue
            delta = abs(float(started_at) - float(self.started_at))
            if delta > 120:
                continue
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best_path = path
                best_id = native_session_id
        if not best_path or not best_id:
            return None
        return RuntimeArtifact(native_session_id=best_id, transcript_path=best_path, started_at=self.started_at)

    def _emit_init_event(self, session, decision, on_stream_event) -> None:
        if not on_stream_event:
            return
        on_stream_event(
            {
                "type": "system",
                "subtype": "init",
                "session_id": self.native_session_id,
                "model": str(session.display_model or decision.display_model or decision.selected_model or "sonnet"),
            }
        )

    async def _wait_for_turn_result(
        self,
        *,
        session,
        decision,
        artifact: RuntimeArtifact,
        offset: int,
        turn_started_at: float | None = None,
        on_stream_event=None,
    ) -> dict[str, Any]:
        del turn_started_at
        path = artifact.transcript_path
        deadline = time.time() + int(self.config.get("main_session_max_timeout", 3600))
        read_offset = offset
        partial = b""
        pending_end_turn_message: dict[str, Any] | None = None
        pending_end_turn_seen_at = 0.0
        thinking_only_retry_sent = False
        try:
            thinking_grace_seconds = max(
                float(
                    self.config.get(
                        "main_session_claude_thinking_end_turn_grace_seconds",
                        CLAUDE_THINKING_END_TURN_GRACE_SECONDS,
                    )
                ),
                0.0,
            )
        except (TypeError, ValueError):
            thinking_grace_seconds = CLAUDE_THINKING_END_TURN_GRACE_SECONDS

        def finish_with_message(message: dict[str, Any]) -> dict[str, Any]:
            actual_provider_model = str(
                message.get("model")
                or session.provider_model
                or decision.provider_model
                or decision.selected_model
                or ""
            ).strip()
            usage = dict(message.get("usage") or {})
            if on_stream_event:
                on_stream_event(
                    {
                        "type": "result",
                        "session_id": artifact.native_session_id,
                        "usage": usage,
                        "stop_reason": "end_turn",
                    }
                )
            return {
                "native_session_id": artifact.native_session_id,
                "resume_token": artifact.native_session_id,
                "selected_model": infer_main_session_display_model(
                    actual_provider_model,
                    fallback=str(session.display_model or decision.display_model or decision.selected_model or ""),
                ),
                "provider_model": actual_provider_model,
                "result": _extract_claude_text(message),
                "usage": usage,
                "raw_output": "",
            }

        async def retry_thinking_only_response() -> bool:
            nonlocal pending_end_turn_message, pending_end_turn_seen_at, thinking_only_retry_sent
            if thinking_only_retry_sent:
                return False
            if not _should_retry_claude_thinking_only(session=session, decision=decision):
                return False
            if not _extract_claude_thinking(pending_end_turn_message or {}):
                return False
            try:
                await self._send_prompt_async(CLAUDE_THINKING_ONLY_RETRY_PROMPT)
            except AgentError:
                return False
            thinking_only_retry_sent = True
            pending_end_turn_message = None
            pending_end_turn_seen_at = 0.0
            return True

        self._reset_interaction_state()
        interaction_timeout = int(self.config.get("main_session_interaction_timeout", 86400))
        await self._handle_runtime_output_interactions("")
        try:
            while time.time() < deadline:
                if not self.is_alive():
                    raise AgentError("Claude 主会话常驻 runtime 已退出", error_code="persistent_runtime_exited")
                if path.exists():
                    file_size = path.stat().st_size
                    if file_size < read_offset:
                        read_offset = 0
                        partial = b""
                    if file_size > read_offset:
                        with path.open("rb") as handle:
                            handle.seek(read_offset)
                            data = handle.read()
                        read_offset += len(data)
                        chunk = partial + data
                        parts = chunk.split(b"\n")
                        if chunk.endswith(b"\n"):
                            complete_lines = parts[:-1]
                            partial = b""
                        else:
                            complete_lines = parts[:-1]
                            partial = parts[-1] if parts else chunk
                        for line in complete_lines:
                            decoded_line = line.rstrip(b"\r").decode("utf-8", errors="replace").strip()
                            if not decoded_line:
                                continue
                            try:
                                payload = json.loads(decoded_line)
                            except json.JSONDecodeError:
                                continue
                            if str(payload.get("sessionId") or "") != artifact.native_session_id:
                                continue
                            payload_type = str(payload.get("type") or "")
                            if payload_type == "system" and pending_end_turn_message and str(
                                payload.get("subtype") or ""
                            ) == "turn_duration":
                                if await retry_thinking_only_response():
                                    continue
                                return finish_with_message(pending_end_turn_message)
                            if payload_type != "assistant":
                                continue
                            message = payload.get("message", {}) if isinstance(payload.get("message"), dict) else {}
                            if on_stream_event:
                                on_stream_event({"type": "assistant", "message": message})
                            if str(message.get("stop_reason") or "") != "end_turn":
                                continue
                            if _extract_claude_text(message):
                                return finish_with_message(message)
                            pending_end_turn_message = message
                            pending_end_turn_seen_at = pending_end_turn_seen_at or time.time()
                    if (
                        pending_end_turn_message
                        and thinking_grace_seconds <= max(0.0, time.time() - pending_end_turn_seen_at)
                    ):
                        if await retry_thinking_only_response():
                            await asyncio.sleep(TURN_POLL_INTERVAL)
                            continue
                        return finish_with_message(pending_end_turn_message)
                if await self._has_pending_interaction():
                    wait_started_at = time.time()
                    interaction_deadline = (
                        wait_started_at + max(1, interaction_timeout)
                        if interaction_timeout > 0
                        else float("inf")
                    )
                    resumed = await self._wait_for_interaction_resume(interaction_deadline)
                    deadline += max(0.0, time.time() - wait_started_at)
                    if not resumed and await self._has_pending_interaction():
                        raise AgentError("Claude 主会话等待交互确认超时", error_code="E103")
                    continue
                await asyncio.sleep(TURN_POLL_INTERVAL)
        finally:
            self._reset_interaction_state()
        if pending_end_turn_message:
            return finish_with_message(pending_end_turn_message)
        raise AgentError("Claude 主会话常驻 runtime 等待结果超时", error_code="E102")


class CodexPersistentSessionRuntime(BasePersistentSessionRuntime):
    runtime_family = "codex"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._codex_home = DEFAULT_CODEX_HOME
        self._codex_sessions_dir = CODEX_SESSIONS_DIR
        self._codex_log_path = DEFAULT_CODEX_HOME / "log" / "codex-tui.log"
        self._interaction_queue: list[dict[str, Any]] = []
        self._interaction_seen_call_ids: set[str] = set()
        self._interaction_state_lock = asyncio.Lock()
        self._interaction_resume_event = asyncio.Event()
        self._mcp_approval_active_signature = ""
        self._mcp_approval_active_request_id = ""
        self._mcp_approval_counter = 0

    def _reset_interaction_state(self) -> None:
        self._interaction_queue = []
        self._interaction_seen_call_ids = set()
        self._interaction_resume_event = asyncio.Event()
        self._mcp_approval_active_signature = ""
        self._mcp_approval_active_request_id = ""
        self._mcp_approval_counter = 0

    def _interaction_options(self, *, allow_prefix: bool) -> list[dict[str, Any]]:
        options = [
            {"id": "approve_once", "label": "允许一次", "kind": "button"},
        ]
        if allow_prefix:
            options.append({"id": "approve_prefix", "label": "允许同前缀", "kind": "button"})
        options.append({"id": "deny", "label": "拒绝", "kind": "button"})
        return options

    @staticmethod
    def _mcp_approval_options() -> list[dict[str, Any]]:
        return [
            {"id": "approve_once", "label": "允许一次", "kind": "button"},
            {"id": "approve_session", "label": "本会话允许", "kind": "button"},
            {"id": "approve_always", "label": "总是允许", "kind": "button"},
            {"id": "deny", "label": "取消", "kind": "button"},
        ]

    async def _handle_runtime_output_interactions(self, text: str) -> None:
        del text
        prompt = _extract_codex_mcp_approval_prompt(self._recent_output)
        if not prompt:
            if not await self._has_pending_interaction():
                self._mcp_approval_active_signature = ""
                self._mcp_approval_active_request_id = ""
            return
        signature = str(prompt.get("signature") or prompt.get("request_id") or "").strip()
        if signature and signature == self._mcp_approval_active_signature:
            return
        self._mcp_approval_counter += 1
        if signature:
            self._mcp_approval_active_signature = signature
        request_id = f"mcp_{signature}_{self._mcp_approval_counter}" if signature else prompt["request_id"]
        self._mcp_approval_active_request_id = request_id
        server = prompt["server"]
        tool = prompt["tool"]
        request = {
            "request_id": request_id,
            "turn_id": self.native_session_id or self.session_id,
            "runtime_family": self.runtime_family,
            "interaction_kind": "mcp_tool_approval",
            "title": "允许 MCP 工具调用",
            "summary": f"Codex 请求允许 {server} MCP 运行 {tool}。",
            "details": prompt.get("details") or "",
            "command": f"{server}.{tool}",
            "tool_name": tool,
            "mcp_server": server,
            "options": self._mcp_approval_options(),
            "available_actions": ["approve_once", "approve_session", "approve_always", "deny"],
            "text_required_actions": [],
            "requires_text": False,
            "status": "pending",
            "source": "codex_tui",
            "created_at": utcnow_iso(),
        }
        await self._enqueue_interaction_request(
            request=request,
            native_session_id=self.native_session_id or self.session_id,
            on_stream_event=self._stream_event_cb,
        )

    def _parse_interaction_request(
        self,
        *,
        payload: dict[str, Any],
        native_session_id: str,
    ) -> dict[str, Any] | None:
        payload_type = str(payload.get("type") or "")
        if payload_type not in {"function_call", "custom_tool_call"}:
            return None
        call_id = str(payload.get("call_id") or "").strip()
        if not call_id:
            return None
        raw_arguments = payload.get("arguments")
        if not isinstance(raw_arguments, str) or not raw_arguments.strip():
            return None
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError:
            return None
        if not isinstance(arguments, dict):
            return None
        if str(arguments.get("sandbox_permissions") or "").strip() != "require_escalated":
            return None
        command = str(arguments.get("cmd") or "").strip()
        justification = str(arguments.get("justification") or "").strip()
        prefix_rule = [
            str(item).strip()
            for item in (arguments.get("prefix_rule") or [])
            if str(item).strip()
        ]
        available_actions = ["approve_once", "deny"]
        if prefix_rule:
            available_actions.insert(1, "approve_prefix")
        return {
            "request_id": call_id,
            "turn_id": native_session_id,
            "runtime_family": self.runtime_family,
            "interaction_kind": "approval",
            "title": "允许执行工具命令",
            "summary": justification or "主会话需要确认是否允许执行该命令。",
            "details": justification,
            "command": command,
            "prefix_rule": prefix_rule,
            "tool_name": str(payload.get("name") or ""),
            "call_id": call_id,
            "options": self._interaction_options(allow_prefix=bool(prefix_rule)),
            "available_actions": available_actions,
            "text_required_actions": [],
            "requires_text": False,
            "status": "pending",
            "source": "runtime",
            "created_at": utcnow_iso(),
        }

    def _refresh_interaction_queue_locked(self) -> None:
        total = len(self._interaction_queue)
        for index, item in enumerate(self._interaction_queue, start=1):
            item["queue_index"] = index
            item["queue_total"] = total

    def _current_interaction_locked(self) -> dict[str, Any]:
        if not self._interaction_queue:
            return {}
        return dict(self._interaction_queue[0])

    async def _enqueue_interaction_request(
        self,
        *,
        request: dict[str, Any],
        native_session_id: str,
        on_stream_event=None,
    ) -> None:
        request_id = str(request.get("request_id") or "").strip()
        if not request_id:
            return
        async with self._interaction_state_lock:
            if request_id in self._interaction_seen_call_ids:
                return
            self._interaction_seen_call_ids.add(request_id)
            self._interaction_queue.append(dict(request))
            self._refresh_interaction_queue_locked()
            pending = self._current_interaction_locked()
        if on_stream_event and pending:
            on_stream_event(
                {
                    "type": "interaction_requested",
                    "thread_id": native_session_id,
                    "payload": pending,
                }
            )

    async def _has_pending_interaction(self) -> bool:
        async with self._interaction_state_lock:
            return bool(self._interaction_queue)

    async def _wait_for_interaction_resume(self, deadline: float) -> bool:
        while time.time() < deadline:
            if not self.is_alive():
                raise AgentError("Codex 主会话常驻 runtime 已退出", error_code="persistent_runtime_exited")
            try:
                timeout = min(TURN_POLL_INTERVAL, max(deadline - time.time(), 0.01))
                await asyncio.wait_for(self._interaction_resume_event.wait(), timeout=timeout)
                self._interaction_resume_event.clear()
                return True
            except asyncio.TimeoutError:
                continue
        return False

    async def submit_interaction_response(
        self,
        *,
        request_id: str,
        action: str,
        text: str = "",
    ) -> dict[str, Any]:
        del text
        action = str(action or "").strip()
        request_id = str(request_id or "").strip()
        if not self.is_alive():
            raise AgentError("Codex 主会话常驻 runtime 已退出", error_code="persistent_runtime_exited")
        if self.master_fd < 0:
            raise AgentError("当前主会话 runtime 暂不支持交互请求。", error_code="interactive_request_unavailable")
        async with self._interaction_state_lock:
            current = self._current_interaction_locked()
            current_request_id = str(current.get("request_id") or "").strip()
            if not current_request_id or current_request_id != request_id:
                raise AgentError("当前交互请求已变化，请刷新后重试。", error_code="interaction_stale")
            if action == "approve_once":
                payload = b"1\r" if current.get("interaction_kind") == "mcp_tool_approval" else b"y\r"
                message = "已允许本次工具调用，主会话继续运行。"
            elif action == "approve_prefix" and "approve_prefix" in (current.get("available_actions") or []):
                payload = b"p\r"
                message = "已允许当前前缀工具调用，主会话继续运行。"
            elif action == "approve_session" and "approve_session" in (current.get("available_actions") or []):
                payload = b"2\r"
                message = "已允许本会话内的同类 MCP 工具调用，主会话继续运行。"
            elif action == "approve_always" and "approve_always" in (current.get("available_actions") or []):
                payload = b"3\r"
                message = "已长期允许该 MCP 工具调用，主会话继续运行。"
            elif action == "deny":
                payload = b"4\r" if current.get("interaction_kind") == "mcp_tool_approval" else b"\x1b"
                message = "已拒绝本次工具调用，主会话继续运行。"
            else:
                raise AgentError("当前交互请求不支持该操作。", error_code="interaction_action_invalid")
            self._write_master_bytes(payload)
            self._interaction_queue.pop(0)
            self._refresh_interaction_queue_locked()
            next_pending = self._current_interaction_locked()
            self._interaction_resume_event.set()
        return {
            "message": message,
            "current_turn_status": "waiting_interaction" if next_pending else "running",
            "pending_interaction": next_pending,
        }

    async def interrupt_turn(self, *, force: bool = False) -> dict[str, Any]:
        if force:
            return await super().interrupt_turn(force=True)
        async with self._interaction_state_lock:
            self._interaction_queue.clear()
            self._mcp_approval_active_signature = ""
            self._mcp_approval_active_request_id = ""
            self._interaction_resume_event.set()
        return await super().interrupt_turn(force=False)

    @property
    def _discovery_timeout(self) -> float:
        return CODEX_SESSION_DISCOVERY_MAX_WAIT

    @property
    def _discovery_interval(self) -> float:
        return CODEX_SESSION_DISCOVERY_INTERVAL

    def _should_send_prompt_before_artifact(self) -> bool:
        # Codex only creates its native session transcript after the first user
        # message is submitted, so waiting for an artifact before sending the
        # first prompt deadlocks brand-new runtimes.
        return True

    async def _wait_until_ready_before_first_prompt(self) -> None:
        quiet_period = max(float(self.config.get("main_session_codex_startup_quiet_period", 0.35)), 0.05)
        min_wait = max(float(self.config.get("main_session_codex_startup_min_wait", 1.5)), STARTUP_GRACE_SECONDS)
        soft_wait = max(float(self.config.get("main_session_codex_startup_ready_timeout", 15.0)), min_wait)
        hard_wait = max(
            float(self.config.get("main_session_codex_startup_force_prompt_timeout", 60.0)),
            soft_wait,
        )
        started = time.time()
        soft_deadline = started + soft_wait
        hard_deadline = started + hard_wait
        while time.time() < hard_deadline:
            if not self.is_alive():
                raise AgentError("Codex 主会话常驻 runtime 已退出", error_code="persistent_runtime_exited")
            if self._directory_trust_prompt_visible():
                if (
                    self._startup_input_attempts < 2
                    and (
                        self._startup_input_attempts == 0
                        or (time.time() - self.last_output_at) >= 0.6
                    )
                ):
                    self._send_startup_input("\r")
                    self._startup_input_attempts += 1
                await asyncio.sleep(0.05)
                continue
            now = time.time()
            if (
                self._startup_ready_marker_seen()
                and self._first_output_at
                and (now - self.started_at) >= min_wait
                and (now - self.last_output_at) >= quiet_period
            ):
                return
            if (
                self._first_output_at
                and self._startup_semantic_output_seen()
                and (now - self.started_at) >= max(min_wait * 2, 2.5)
                and (now - self.last_output_at) >= quiet_period
            ):
                return
            if (
                now >= soft_deadline
                and not self._codex_mcp_startup_pending()
                and not self._startup_log_recently_active()
            ):
                return
            await asyncio.sleep(0.05)

    def _directory_trust_prompt_visible(self) -> bool:
        text = self._recent_output
        return (
            "Do you trust the contents of this directory?" in text
            and "Press enter to continue" in text
        )

    def _codex_startup_plain_text(self) -> str:
        return _strip_ansi_runtime_text(self._recent_output).replace("\r", "\n")

    def _codex_mcp_startup_pending(self) -> bool:
        text = self._codex_startup_plain_text()
        last_prompt = text.rfind("›")
        last_mcp_start = max(
            text.rfind("Starting MCP servers"),
            text.rfind("Booting MCP server"),
        )
        return last_mcp_start >= 0 and last_mcp_start > last_prompt

    def _startup_ready_marker_seen(self) -> bool:
        text = self._codex_startup_plain_text()
        if "OpenAI Codex" not in text:
            return False
        last_prompt = text.rfind("›")
        if last_prompt < 0 or last_prompt < text.rfind("OpenAI Codex"):
            return False
        if self._codex_mcp_startup_pending():
            return False
        prompt_tail = text[last_prompt:]
        return "Use /skills" in prompt_tail and " · " in prompt_tail

    def _startup_semantic_output_seen(self) -> bool:
        if self._codex_mcp_startup_pending():
            return False
        text = self._recent_output
        return any(
            marker in text
            for marker in (
                "OpenAI Codex",
                "You are in ",
                "Do you trust the contents of this directory?",
                "directory:",
                "/model",
                "100% left",
            )
        )

    def _maybe_retry_startup_prompt(self) -> None:
        transcript_known = _transcript_path_known(self.transcript_path)
        if self.native_session_id or transcript_known or self.master_fd < 0:
            return
        if not self._startup_prompt or self._startup_prompt_sent_at <= 0:
            return
        max_retries = max(int(self.config.get("main_session_codex_startup_retry_limit", 2)), 0)
        if self._startup_prompt_retry_count >= max_retries:
            return
        now = time.time()
        silent_retry_after = max(float(self.config.get("main_session_codex_startup_silent_retry_after", 1.5)), 0.2)
        activity_retry_after = max(
            float(self.config.get("main_session_codex_startup_retry_after", 12.0)),
            silent_retry_after,
        )
        since_send = now - self._startup_prompt_sent_at
        saw_output_after_send = self.last_output_at > (self._startup_prompt_output_checkpoint + 1e-6)
        if not saw_output_after_send and since_send < silent_retry_after:
            return
        if saw_output_after_send and since_send < activity_retry_after:
            return
        self._send_prompt(self._startup_prompt)
        self._startup_prompt_retry_count += 1
        self._startup_prompt_sent_at = now
        self._startup_prompt_output_checkpoint = float(self.last_output_at or 0.0)

    def _startup_log_recently_active(self) -> bool:
        try:
            mtime = self._codex_log_path.stat().st_mtime
        except OSError:
            return False
        baseline = max(float(self._startup_log_mtime_before or 0.0), float(self.started_at or 0.0))
        if mtime <= baseline:
            return False
        # Account-login bootstrap can stall on sparse network-bound startup work
        # for tens of seconds without emitting PTY-ready markers. Keep waiting
        # while the connection-scoped Codex log is still advancing.
        recent_window = max(float(self.config.get("main_session_codex_startup_log_recent_window", 25.0)), 0.5)
        return (time.time() - mtime) <= recent_window

    def _prepare_start(self, *, session, connection: dict) -> None:
        self._codex_home = resolve_connection_codex_home(
            self.project_root,
            connection,
            fallback_key="main_session",
        )
        self._codex_sessions_dir = self._codex_home / "sessions"
        self._codex_history_path = self._codex_home / "history.jsonl"
        self._codex_log_path = self._codex_home / "log" / "codex-tui.log"
        try:
            self._startup_log_mtime_before = self._codex_log_path.stat().st_mtime
        except OSError:
            self._startup_log_mtime_before = 0.0
        self._codex_sessions_dir.mkdir(parents=True, exist_ok=True)
        self._known_session_transcripts: set[Path] = set()
        try:
            for path in self._codex_sessions_dir.rglob("*.jsonl"):
                self._known_session_transcripts.add(path.resolve())
        except Exception:
            self._known_session_transcripts = set()
        resume_token = str(getattr(session, "resume_token", "") or "").strip()
        if resume_token:
            resume_path = self._resolve_codex_transcript_path(resume_token)
            if _transcript_path_known(resume_path):
                self.native_session_id = resume_token
                self.transcript_path = resume_path

    def _build_command(self, *, session, connection: dict) -> list[str]:
        profile = {
            "base_url": connection.get("base_url", ""),
            "selected_model": session.provider_model or session.display_model,
            "wire_api": "responses",
            "use_default_auth": str(connection.get("auth_mode") or "").strip() == "account_login",
        }
        metadata = dict(getattr(session, "metadata", {}) or {})
        reasoning_effort = str(metadata.get("reasoning_effort") or "medium").strip()
        if reasoning_effort not in {"low", "medium", "high", "xhigh"}:
            reasoning_effort = "medium"
        overrides = build_codex_mcp_config_overrides(
            str(session.cwd or ""),
            confirm_port=int(self.config.get("confirm_server", {}).get("port", 9390)),
            config_data=self.config,
            session_id=str(getattr(session, "session_id", "") or ""),
            runtime_dir=str(metadata.get("runtime_dir") or ""),
            project_root=str(metadata.get("project_root") or session.cwd or ""),
        )
        args = ["codex"]
        if session.resume_token:
            args.extend(["resume", str(session.resume_token)])
        args.extend(["--model", str(profile["selected_model"])])
        args.extend(["--enable", "image_generation"])
        args.extend(build_codex_interactive_automation_args(resolve_codex_main_session_sandbox(self.config)))
        args.append("--no-alt-screen")
        args.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
        if not profile["use_default_auth"]:
            provider_name = "vizo_runtime"
            provider_config = (
                f'model_providers.{provider_name}='
                f'{{name="{provider_name}",base_url="{profile["base_url"]}",wire_api="{profile.get("wire_api", "responses")}",env_key="OPENAI_API_KEY"}}'
            )
            args.extend(["-c", f"model_provider={provider_name}", "-c", provider_config])
        for item in overrides:
            args.extend(["-c", item])
        return args

    def _build_child_env(self, *, session, connection: dict) -> dict[str, str]:
        metadata = dict(getattr(session, "metadata", {}) or {})
        env = {
            "CODEX_HOME": str(self._codex_home),
            **build_main_session_mcp_env(
                session_id=str(getattr(session, "session_id", "") or ""),
                runtime_dir=str(metadata.get("runtime_dir") or ""),
                project_root=str(metadata.get("project_root") or session.cwd or ""),
            ),
        }
        if str(connection.get("auth_mode") or "").strip() != "account_login":
            env["OPENAI_API_KEY"] = str(connection.get("api_key") or "")
        return env

    def _predict_codex_transcript_path(self, *, native_session_id: str, timestamp: float) -> Path:
        dt = datetime.fromtimestamp(float(timestamp))
        filename = f"rollout-{dt.strftime('%Y-%m-%dT%H-%M-%S')}-{native_session_id}.jsonl"
        return self._codex_sessions_dir / dt.strftime("%Y") / dt.strftime("%m") / dt.strftime("%d") / filename

    def _resolve_codex_transcript_path(self, native_session_id: str, fallback: Path | None = None) -> Path:
        fallback = fallback or Path()
        if fallback and str(fallback) != "." and fallback.exists():
            return fallback
        if not native_session_id or not self._codex_sessions_dir.exists():
            return fallback
        try:
            candidates = sorted(
                self._codex_sessions_dir.rglob(f"*{native_session_id}.jsonl"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except Exception:
            return fallback
        return candidates[0] if candidates else fallback

    def _infer_codex_artifact_from_history(self) -> RuntimeArtifact | None:
        if not self._startup_prompt or not self._codex_history_path.exists():
            return None
        try:
            lines = self._codex_history_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        for line in reversed(lines[-128:]):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            native_session_id = str(payload.get("session_id") or "").strip()
            prompt_text = str(payload.get("text") or "")
            timestamp = payload.get("ts")
            if not native_session_id or prompt_text != self._startup_prompt:
                continue
            try:
                started_at_ts = float(timestamp)
            except (TypeError, ValueError):
                continue
            if started_at_ts < float(self.started_at) - 5.0:
                continue
            if abs(started_at_ts - float(self.started_at)) > 300.0:
                continue
            transcript_path = self._resolve_codex_transcript_path(
                native_session_id,
                fallback=self._predict_codex_transcript_path(
                    native_session_id=native_session_id,
                    timestamp=started_at_ts,
                ),
            )
            return RuntimeArtifact(
                native_session_id=native_session_id,
                transcript_path=transcript_path,
                started_at=self.started_at,
            )
        return None

    def _infer_artifact(self, cwd: str) -> RuntimeArtifact | None:
        if not self._codex_sessions_dir.exists():
            return self._infer_codex_artifact_from_history()
        best_path: Path | None = None
        best_id = ""
        best_delta: float | None = None
        try:
            candidates = sorted(
                self._codex_sessions_dir.rglob("*.jsonl"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except Exception:
            candidates = []
        for path in candidates[:CODEX_TRANSCRIPT_SCAN_LIMIT]:
            try:
                resolved_path = path.resolve()
            except OSError:
                continue
            if resolved_path in self._known_session_transcripts:
                continue
            meta = _read_codex_session_meta(path)
            if not meta:
                continue
            native_session_id = str(meta.get("native_session_id") or "")
            started_at = meta.get("started_at")
            transcript_cwd = str(meta.get("cwd") or "")
            if not native_session_id or not _same_runtime_path(transcript_cwd, cwd):
                continue
            if started_at is None:
                continue
            started_at_ts = float(started_at)
            if started_at_ts < float(self.started_at) - 5.0:
                continue
            delta = abs(started_at_ts - float(self.started_at))
            if delta > 120:
                continue
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best_path = path
                best_id = native_session_id
        if best_path and best_id:
            return RuntimeArtifact(native_session_id=best_id, transcript_path=best_path, started_at=self.started_at)
        return self._infer_codex_artifact_from_history()

    def _emit_init_event(self, session, decision, on_stream_event) -> None:
        if not on_stream_event:
            return
        on_stream_event(
            {
                "type": "thread.started",
                "thread_id": self.native_session_id,
            }
        )

    def _emit_turn_started_event(self, native_session_id: str, on_stream_event) -> None:
        if on_stream_event:
            on_stream_event({"type": "turn.started", "thread_id": native_session_id})

    async def _wait_for_turn_result(
        self,
        *,
        session,
        decision,
        artifact: RuntimeArtifact,
        offset: int,
        turn_started_at: float | None = None,
        on_stream_event=None,
    ) -> dict[str, Any]:
        self._reset_interaction_state()
        path = self._resolve_codex_transcript_path(artifact.native_session_id, fallback=artifact.transcript_path)
        artifact.transcript_path = path
        turn_timeout = max(1, int(self.config.get("main_session_max_timeout", 3600)))
        interaction_timeout = int(self.config.get("main_session_interaction_timeout", 86400))
        deadline = time.time() + turn_timeout
        read_offset = offset
        partial = ""
        last_usage: dict[str, Any] = {}
        last_message = ""
        last_error_message = ""
        last_error_code = ""
        last_error_info = ""
        empty_message_retry_sent = False

        async def retry_empty_agent_message() -> bool:
            nonlocal empty_message_retry_sent, last_message
            if empty_message_retry_sent:
                return False
            try:
                await self._send_prompt_async(CODEX_EMPTY_MESSAGE_RETRY_PROMPT)
            except AgentError:
                return False
            empty_message_retry_sent = True
            last_message = ""
            return True

        try:
            while time.time() < deadline:
                if not self.is_alive():
                    raise AgentError("Codex 主会话常驻 runtime 已退出", error_code="persistent_runtime_exited")
                resolved_path = self._resolve_codex_transcript_path(artifact.native_session_id, fallback=path)
                if resolved_path != path:
                    path = resolved_path
                    artifact.transcript_path = resolved_path
                if path.exists():
                    file_size = path.stat().st_size
                    if file_size < read_offset:
                        read_offset = 0
                        partial = ""
                    if file_size > read_offset:
                        with path.open("rb") as handle:
                            handle.seek(read_offset)
                            data = handle.read()
                        read_offset += len(data)
                        chunk = partial + data.decode("utf-8", errors="replace")
                        lines = chunk.splitlines()
                        if chunk and not chunk.endswith("\n"):
                            partial = lines.pop() if lines else chunk
                        else:
                            partial = ""
                        for line in lines:
                            try:
                                payload = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            payload_timestamp = _extract_runtime_event_timestamp(payload)
                            if (
                                turn_started_at is not None
                                and payload_timestamp is not None
                                and payload_timestamp < (turn_started_at - TURN_EVENT_TIMESTAMP_SKEW_SECONDS)
                            ):
                                continue
                            if payload.get("type") == "response_item":
                                if on_stream_event:
                                    on_stream_event(payload)
                                inner = payload.get("payload", {}) if isinstance(payload.get("payload"), dict) else {}
                                interaction_request = self._parse_interaction_request(
                                    payload=inner,
                                    native_session_id=artifact.native_session_id,
                                )
                                if interaction_request:
                                    await self._enqueue_interaction_request(
                                        request=interaction_request,
                                        native_session_id=artifact.native_session_id,
                                        on_stream_event=on_stream_event,
                                    )
                                if (
                                    str(inner.get("type") or "") == "message"
                                    and str(inner.get("role") or "") == "assistant"
                                ):
                                    message_text = _extract_codex_message_text(payload)
                                    if message_text:
                                        last_message = message_text
                                continue
                            if payload.get("type") != "event_msg":
                                continue
                            inner = payload.get("payload", {}) if isinstance(payload.get("payload"), dict) else {}
                            inner_type = str(inner.get("type") or "")
                            if inner_type == "token_count":
                                info = inner.get("info", {}) if isinstance(inner.get("info"), dict) else {}
                                last_usage = dict(info.get("last_token_usage") or {})
                                continue
                            if inner_type == "agent_message":
                                last_message = str(inner.get("message") or last_message or "")
                                continue
                            if inner_type == "error":
                                message = str(inner.get("message") or "").strip()
                                if message:
                                    last_error_message = message
                                last_error_info = str(inner.get("codex_error_info") or last_error_info or "")
                                last_error_code = _codex_transcript_error_code(
                                    message=last_error_message,
                                    error_info=last_error_info,
                                    fallback=str(inner.get("error_code") or last_error_code or ""),
                                )
                                continue
                            if inner_type != "task_complete":
                                continue
                            final_text = str(inner.get("last_agent_message") or last_message or "")
                            turn_id = str(inner.get("turn_id") or "").strip()
                            if not final_text and last_error_message:
                                last_error_message = _format_codex_runtime_error_message(
                                    last_error_message,
                                    error_info=last_error_info,
                                )
                                if last_error_code == "E201":
                                    raise AgentRateLimitError(last_error_message, error_code=last_error_code)
                                raise AgentError(last_error_message, error_code=last_error_code or "E301")
                            if not final_text:
                                log_error_message = self._read_recent_codex_turn_error(
                                    turn_id=turn_id,
                                    since=turn_started_at,
                                )
                                if log_error_message:
                                    log_error_code = _codex_transcript_error_code(
                                        message=log_error_message,
                                        error_info=last_error_info,
                                        fallback=last_error_code,
                                    )
                                    log_error_message = _format_codex_runtime_error_message(
                                        log_error_message,
                                        error_info=last_error_info,
                                    )
                                    if log_error_code == "E201":
                                        raise AgentRateLimitError(log_error_message, error_code=log_error_code)
                                    raise AgentError(log_error_message, error_code=log_error_code)
                                if await retry_empty_agent_message():
                                    continue
                                raise AgentError(
                                    "Codex 主会话完成但没有返回 assistant message。请检查 Codex 认证状态或运行日志。",
                                    error_code="empty_agent_message",
                                )
                            if on_stream_event:
                                on_stream_event(
                                    {
                                        "type": "turn.completed",
                                        "thread_id": artifact.native_session_id,
                                        "usage": {
                                            "input_tokens": int(last_usage.get("input_tokens") or 0),
                                            "output_tokens": int(last_usage.get("output_tokens") or 0),
                                            "reasoning_output_tokens": int(last_usage.get("reasoning_output_tokens") or 0),
                                        },
                                    }
                                )
                            return {
                                "native_session_id": artifact.native_session_id,
                                "resume_token": artifact.native_session_id,
                                "selected_model": str(session.display_model or decision.display_model or decision.selected_model or ""),
                                "provider_model": str(session.provider_model or decision.provider_model or decision.selected_model or ""),
                                "result": final_text,
                                "usage": last_usage,
                                "raw_output": "",
                            }
                if await self._has_pending_interaction():
                    wait_started_at = time.time()
                    interaction_deadline = (
                        wait_started_at + max(1, interaction_timeout)
                        if interaction_timeout > 0
                        else float("inf")
                    )
                    resumed = await self._wait_for_interaction_resume(interaction_deadline)
                    deadline += max(0.0, time.time() - wait_started_at)
                    if not resumed and await self._has_pending_interaction():
                        raise AgentError("Codex 主会话等待交互确认超时", error_code="E103")
                    continue
                await asyncio.sleep(TURN_POLL_INTERVAL)
        finally:
            self._reset_interaction_state()
        raise AgentError("Codex 主会话常驻 runtime 等待结果超时", error_code="E102")

    def _read_recent_codex_turn_error(self, *, turn_id: str = "", since: float | None = None) -> str:
        path = self._codex_log_path
        if not path.exists():
            return ""
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(size - 262_144, 0), os.SEEK_SET)
                text = handle.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

        best_message = ""
        for raw_line in text.splitlines():
            if "Turn error:" not in raw_line:
                continue
            if turn_id and f"turn.id={turn_id}" not in raw_line:
                continue
            if since is not None:
                timestamp_text = raw_line.split(" ", 1)[0].strip()
                try:
                    timestamp = datetime.fromisoformat(timestamp_text.replace("Z", "+00:00")).timestamp()
                except Exception:
                    timestamp = None
                if timestamp is not None and timestamp < (since - TURN_EVENT_TIMESTAMP_SKEW_SECONDS):
                    continue
            message = raw_line.split("Turn error:", 1)[1].strip()
            if message:
                best_message = message
        return best_message


def build_persistent_runtime(
    *,
    runtime_family: str,
    session_id: str,
    project_root: Path,
    config: dict,
    raw_output_cb: Callable[[str], None] | None = None,
) -> BasePersistentSessionRuntime:
    if runtime_family == "claude_code":
        return ClaudePersistentSessionRuntime(
            session_id=session_id,
            project_root=project_root,
            config=config,
            raw_output_cb=raw_output_cb,
        )
    if runtime_family == "codex":
        return CodexPersistentSessionRuntime(
            session_id=session_id,
            project_root=project_root,
            config=config,
            raw_output_cb=raw_output_cb,
        )
    raise AgentError(f"不支持的主会话常驻 runtime: {runtime_family}", error_code="persistent_runtime_unsupported")


def _extract_claude_text(message: dict[str, Any]) -> str:
    content = message.get("content", []) if isinstance(message.get("content"), list) else []
    lines: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "") != "text":
            continue
        text = str(item.get("text") or "").strip()
        if text:
            lines.append(text)
    return "\n".join(lines).strip()


def _extract_claude_thinking(message: dict[str, Any]) -> str:
    content = message.get("content", []) if isinstance(message.get("content"), list) else []
    lines: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "") not in {"thinking", "redacted_thinking"}:
            continue
        text = str(item.get("thinking") or item.get("text") or "").strip()
        if text:
            lines.append(text)
    return "\n".join(lines).strip()


def _should_retry_claude_thinking_only(*, session, decision) -> bool:
    values = (
        getattr(session, "provider_model", ""),
        getattr(decision, "provider_model", ""),
        getattr(decision, "selected_model", ""),
    )
    return any("deepseek" in str(value or "").lower() for value in values)


def _extract_codex_message_text(payload: dict[str, Any]) -> str:
    inner = payload.get("payload", {}) if isinstance(payload.get("payload"), dict) else {}
    payload_type = str(inner.get("type") or "")
    if payload_type == "reasoning":
        return _extract_codex_reasoning_text(inner)
    if payload_type != "message":
        return ""
    parts: list[str] = []
    for block in inner.get("content", []) if isinstance(inner.get("content"), list) else []:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "")
        if block_type not in {"output_text", "text"}:
            continue
        text = str(block.get("text") or "").strip()
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _extract_runtime_event_timestamp(payload: dict[str, Any]) -> float | None:
    raw_timestamp = payload.get("timestamp")
    if not raw_timestamp:
        return None
    try:
        return datetime.fromisoformat(str(raw_timestamp).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _is_codex_rate_limited_message(message: str, error_info: str = "") -> bool:
    info = str(error_info or "").strip().lower()
    if info in {"usage_limit_exceeded", "rate_limit_exceeded"}:
        return True
    lowered = str(message or "").lower()
    return any(
        keyword in lowered
        for keyword in (
            "rate limit",
            "429",
            "overloaded",
            "too many requests",
            "limit reached",
            "usage limit",
            "capacity",
        )
    )


def _is_codex_auth_error_message(message: str, error_info: str = "") -> bool:
    lowered = f"{message or ''}\n{error_info or ''}".lower()
    return any(
        keyword in lowered
        for keyword in (
            "token_expired",
            "401 unauthorized",
            "access token",
            "refresh token",
            "please log out and sign in again",
            "authentication",
        )
    )


def _format_codex_runtime_error_message(message: str, *, error_info: str = "") -> str:
    message = str(message or "").strip()
    if _is_codex_auth_error_message(message, error_info):
        return f"Codex 认证失败：{message}。请重新登录 Codex/OpenAI 账号后再试。"
    return message


def _codex_transcript_error_code(*, message: str, error_info: str = "", fallback: str = "") -> str:
    if _is_codex_auth_error_message(message, error_info):
        return "codex_auth_required"
    if _is_codex_rate_limited_message(message, error_info):
        return "E201"
    return str(fallback or "E301")


def _read_claude_transcript_start_meta(path: Path) -> dict[str, Any] | None:
    native_session_id = ""
    cwd = ""
    started_at = None
    try:
        with path.open(encoding="utf-8") as handle:
            for _ in range(12):
                line = handle.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except Exception:
                    continue
                native_session_id = native_session_id or str(payload.get("sessionId", "") or "")
                cwd = cwd or str(payload.get("cwd", "") or "")
                timestamp = payload.get("timestamp")
                if timestamp:
                    try:
                        started_at = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")).timestamp()
                    except Exception:
                        pass
                if native_session_id and cwd and started_at is not None:
                    break
    except Exception:
        return None
    if not native_session_id and not cwd:
        return None
    if started_at is None:
        try:
            started_at = path.stat().st_mtime
        except OSError:
            started_at = None
    return {
        "native_session_id": native_session_id or path.stem,
        "cwd": cwd,
        "started_at": started_at,
    }


def _resolve_claude_transcript_path(native_session_id: str) -> Path:
    if not native_session_id or not CLAUDE_PROJECTS_DIR.exists():
        return Path()
    try:
        candidates = sorted(
            CLAUDE_PROJECTS_DIR.rglob(f"{native_session_id}.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except Exception:
        return Path()
    return candidates[0] if candidates else Path()


def _read_codex_session_meta(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as handle:
            first_line = handle.readline().strip()
        if not first_line:
            return None
        payload = json.loads(first_line)
    except Exception:
        return None
    if str(payload.get("type") or "") != "session_meta":
        return None
    meta = payload.get("payload", {}) if isinstance(payload.get("payload"), dict) else {}
    timestamp = str(meta.get("timestamp") or payload.get("timestamp") or "")
    started_at = None
    if timestamp:
        try:
            started_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
        except Exception:
            started_at = None
    return {
        "native_session_id": str(meta.get("id") or path.stem),
        "cwd": str(meta.get("cwd") or ""),
        "started_at": started_at,
    }
