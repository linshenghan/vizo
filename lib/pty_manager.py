"""
PTY Manager for Opus V6 Web Console.

Manages pseudo-terminal sessions for browser-based Claude Code interaction.
Each session wraps a PTY running `claude` in interactive mode, with:
- Ring buffer for reconnection replay
- Idle detection with Opus task awareness
- Orphan process cleanup
"""

import asyncio
import json
import logging
import os
import shutil
import signal
import time
from datetime import datetime
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional
from pathlib import Path

from .mcp_runtime import build_main_session_mcp_config, build_main_session_mcp_env
from .paths import iter_task_dirs

_LIB_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _LIB_DIR.parent


logger = logging.getLogger("pty_manager")

REDIS_KEY = "web_console:sessions"
MAIN_SESSION_STRIP_ENV_PREFIXES = ("ANTHROPIC_", "CODEX_")
MAIN_SESSION_STRIP_ENV_KEYS = {"CLAUDE_CODE_SUBAGENT_MODEL", "OPUS_MAIN_API_KEY", "OPENAI_API_KEY"}
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
CLAUDE_TRANSCRIPT_SCAN_LIMIT = 48
CLAUDE_SESSION_DISCOVERY_MAX_WAIT = 8.0
CLAUDE_SESSION_DISCOVERY_INTERVAL = 0.4


def _load_posix_pty_modules():
    try:
        import fcntl as fcntl_module
        import pty as pty_module
        import struct as struct_module
        import termios as termios_module
    except ModuleNotFoundError as error:
        raise RuntimeError("PTY manager requires a POSIX/WSL runtime") from error
    return fcntl_module, pty_module, struct_module, termios_module


def _build_main_session_mcp_config(
    work_dir: str | None = None,
    confirm_port: int = 9390,
    *,
    session_id: str = "",
    session_dir: str = "",
    runtime_dir: str = "",
    project_root: str = "",
) -> dict:
    """为主会话构建受管 MCP 配置，避免加载用户全量 MCP 导致启动卡死。"""
    try:
        config_data = json.loads((_PROJECT_ROOT / "config.json").read_text(encoding="utf-8"))
    except Exception:
        config_data = {}
    return build_main_session_mcp_config(
        work_dir,
        confirm_port=confirm_port,
        config_data=config_data,
        session_id=session_id,
        session_dir=session_dir,
        runtime_dir=runtime_dir,
        project_root=project_root,
    )


def _find_claude_bin() -> str:
    """查找 claude 可执行文件，覆盖常见安装路径"""
    found = shutil.which('claude')
    if found:
        return found
    # 常见安装路径（npm global、nvm、volta 等）
    candidates = [
        os.path.expanduser("~/.npm-global/bin/claude"),
        os.path.expanduser("~/.local/bin/claude"),
        "/usr/local/bin/claude",
    ]
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return 'claude'  # fallback，依赖子进程 PATH

CLAUDE_BIN = _find_claude_bin()
# Extra PATH entries needed for child processes
EXTRA_PATH = os.path.dirname(CLAUDE_BIN) + ":" + os.path.expanduser("~/.local/bin") + ":" + os.path.expanduser("~/.npm-global/bin")


def _build_main_session_child_env(base_env: dict[str, str], cols: int, rows: int) -> dict[str, str]:
    """为主会话构建干净的子进程环境，避免继承旧 CLI 认证与线程状态。"""
    child_env = {
        k: v for k, v in dict(base_env or {}).items()
        if k not in MAIN_SESSION_STRIP_ENV_KEYS
        and not any(k.startswith(prefix) for prefix in MAIN_SESSION_STRIP_ENV_PREFIXES)
    }
    child_env["TERM"] = "xterm-256color"
    child_env["COLORTERM"] = "truecolor"
    child_env["COLUMNS"] = str(cols)
    child_env["LINES"] = str(rows)
    current_path = child_env.get("PATH", "/usr/bin:/bin")
    child_env["PATH"] = f"{EXTRA_PATH}:{current_path}"
    return child_env


class RingBuffer:
    """Simple ring buffer for terminal output replay on reconnect."""

    MAX_SIZE = 512 * 1024  # 512KB — Claude Code 输出含大量 ANSI 序列，50KB 太小

    def __init__(self):
        self._data = bytearray()

    def write(self, data: bytes):
        self._data.extend(data)
        if len(self._data) > self.MAX_SIZE:
            self._data = self._data[-self.MAX_SIZE:]

    def read_all(self) -> bytes:
        return bytes(self._data)

    def clear(self):
        self._data.clear()


@dataclass
class PTYSession:
    """Represents a single PTY session."""

    id: str
    name: str
    pid: int
    master_fd: int
    cwd: str = ""
    status: str = "running"  # running | suspended | stopped
    created_at: float = field(default_factory=time.time)
    last_input_at: float = field(default_factory=time.time)
    last_output_at: float = field(default_factory=time.time)
    buffer: RingBuffer = field(default_factory=RingBuffer)
    cols: int = 120
    rows: int = 40
    ws_clients: list = field(default_factory=list)  # All connected WebSocket clients (broadcast)
    dialogue_events: list = field(default_factory=list)
    dialogue_seq: int = 0
    native_session_id: str = ""
    resumed_from_session_id: str = ""
    _output_batch: bytearray = field(default_factory=bytearray)
    _flush_handle: Optional[asyncio.TimerHandle] = None
    _reader_task: Optional[asyncio.Task] = None
    _native_session_task: Optional[asyncio.Task] = None


class PTYManager:
    """Manages PTY sessions lifecycle, output buffering, and idle detection."""

    def __init__(self, config: dict, redis_getter: Callable):
        self._sessions: Dict[str, PTYSession] = {}
        self._config = config
        self._get_redis = redis_getter
        self._max_sessions = config.get("max_sessions", 3)
        self._idle_suspend = config.get("idle_timeout_suspend", 1800)
        self._idle_terminate = config.get("idle_timeout_terminate", 7200)
        self._idle_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def startup(self):
        """Initialize manager: clean orphans, start idle checker, opus subscriber."""
        self._loop = asyncio.get_event_loop()
        await self._cleanup_orphans()
        self._idle_task = asyncio.create_task(self._idle_check_loop())
        self._opus_sub_task = asyncio.create_task(self._opus_pubsub_loop())
        logger.info("PTYManager started (max_sessions=%d)", self._max_sessions)

    async def shutdown(self):
        """Destroy all sessions and stop idle checker."""
        if self._idle_task:
            self._idle_task.cancel()
            try:
                await self._idle_task
            except asyncio.CancelledError:
                pass

        if hasattr(self, '_opus_sub_task') and self._opus_sub_task:
            self._opus_sub_task.cancel()
            try:
                await self._opus_sub_task
            except asyncio.CancelledError:
                pass

        session_ids = list(self._sessions.keys())
        for sid in session_ids:
            await self.destroy_session(sid)
        logger.info("PTYManager shutdown complete")

    async def create_session(self, name: str = "", cols: int = 120, rows: int = 40, cwd: str = "",
                             resume_session_id: str = "", fork_session: bool = False,
                             initial_model: str = "", allow_overflow: bool = False) -> Optional[PTYSession]:
        """Create a new PTY session running claude."""
        if len(self._sessions) >= self._max_sessions and not allow_overflow:
            logger.warning("Max sessions reached (%d)", self._max_sessions)
            return None

        session_id = f"sess_{int(time.time()*1000) % 1000000:06x}"

        if not name:
            name = f"Session {len(self._sessions) + 1}"

        # Validate cwd
        work_dir = cwd if cwd and os.path.isdir(cwd) else None
        project_root = str(Path(work_dir or os.getcwd()).resolve())
        session_state_dir = Path(project_root) / ".vizo" / "sessions" / "pty-main" / session_id
        runtime_state_dir = session_state_dir / "runtime"
        runtime_state_dir.mkdir(parents=True, exist_ok=True)

        confirm_port = int(self._config.get("confirm_server", {}).get("port", 9390))
        try:
            from .config_loader import ensure_main_session_env_file_migrated

            ensure_main_session_env_file_migrated()
        except Exception:
            pass
        mcp_config = _build_main_session_mcp_config(
            work_dir,
            confirm_port=confirm_port,
            session_id=session_id,
            session_dir=str(session_state_dir),
            runtime_dir=str(runtime_state_dir),
            project_root=project_root,
        )
        logger.info("Main session MCP managed load: %s", list(mcp_config.get("mcpServers", {}).keys()))

        # Use pty.fork() which correctly sets up the PTY
        fcntl_module, pty_module, struct_module, termios_module = _load_posix_pty_modules()
        child_pid, master_fd = pty_module.fork()

        if child_pid == 0:
            # === Child process ===
            # Change to requested working directory
            if work_dir:
                try:
                    os.chdir(work_dir)
                except Exception:
                    pass
            # Set terminal size on the slave side (stdin is the slave PTY)
            try:
                winsize = struct_module.pack('HHHH', rows, cols, 0, 0)
                fcntl_module.ioctl(0, termios_module.TIOCSWINSZ, winsize)
            except Exception:
                pass

            # 主会话始终从 ~/.claude/settings.json 读取受管认证；这里清掉继承的 ANTHROPIC_*，
            # 避免 shell / .env 的旧值与 settings.json 冲突。
            child_env = _build_main_session_child_env(os.environ, cols, rows)
            child_env.update(
                build_main_session_mcp_env(
                    session_id=session_id,
                    session_dir=str(session_state_dir),
                    runtime_dir=str(runtime_state_dir),
                    project_root=project_root,
                )
            )
            os.environ.clear()
            os.environ.update(child_env)

            # Execute claude
            try:
                claude_args = [
                    "claude",
                ]
                if resume_session_id:
                    claude_args.extend(["--resume", resume_session_id])
                    if fork_session:
                        claude_args.append("--fork-session")
                if initial_model:
                    claude_args.extend(["--model", initial_model])
                claude_args.extend([
                    "--mcp-config", json.dumps(mcp_config, ensure_ascii=False),
                    "--strict-mcp-config",
                ])
                os.execvp(CLAUDE_BIN, claude_args)
            except Exception:
                os._exit(1)

        # === Parent process ===
        # Set master_fd non-blocking
        flags = fcntl_module.fcntl(master_fd, fcntl_module.F_GETFL)
        fcntl_module.fcntl(master_fd, fcntl_module.F_SETFL, flags | os.O_NONBLOCK)

        session = PTYSession(
            id=session_id,
            name=name,
            pid=child_pid,
            master_fd=master_fd,
            cwd=work_dir or os.getcwd(),
            cols=cols,
            rows=rows,
            resumed_from_session_id=resume_session_id,
        )
        self._sessions[session_id] = session

        # Start output reader
        session._reader_task = asyncio.create_task(
            self._output_reader_loop(session)
        )
        session._native_session_task = asyncio.create_task(
            self._discover_native_session_id(session)
        )

        # Register in Redis
        await self._redis_register(session)

        logger.info(
            "Created session %s (pid=%d, %dx%d)",
            session_id, child_pid, cols, rows,
        )
        return session

    async def destroy_session(self, session_id: str):
        """Destroy a PTY session and clean up resources."""
        session = self._sessions.pop(session_id, None)
        if not session:
            return

        await self._stop_session_runtime(session)

        # Close all WebSocket clients
        for ws in list(session.ws_clients):
            try:
                await ws.send_json({
                    "type": "session_ended",
                    "reason": "Session destroyed",
                })
                await ws.close()
            except Exception:
                pass
        session.ws_clients.clear()

        # Unregister from Redis
        await self._redis_unregister(session_id)

        session.status = "stopped"
        logger.info("Destroyed session %s (pid=%d)", session_id, session.pid)

    def get_session(self, session_id: str) -> Optional[PTYSession]:
        return self._sessions.get(session_id)

    def list_sessions(self) -> list:
        result = []
        for s in self._sessions.values():
            result.append({
                "id": s.id,
                "name": s.name,
                "cwd": s.cwd,
                "status": s.status,
                "created_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%S", time.localtime(s.created_at)
                ),
                "last_active_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%S",
                    time.localtime(max(s.last_input_at, s.last_output_at)),
                ),
                "has_vizo_task": self._has_active_vizo_task(),
            })
        return result

    def write_input(self, session: PTYSession, data: str):
        """Write user input to PTY."""
        try:
            os.write(session.master_fd, data.encode("utf-8"))
            session.last_input_at = time.time()
            # Wake up suspended session on input
            if session.status == "suspended":
                try:
                    os.kill(session.pid, signal.SIGCONT)
                    session.status = "running"
                    logger.info("Resumed suspended session %s on input", session.id)
                except ProcessLookupError:
                    pass
        except OSError as e:
            logger.error("Write to session %s failed: %s", session.id, e)

    def append_dialogue_event(
        self,
        session: PTYSession,
        kind: str,
        label: str,
        body: list[str],
        meta: str = "",
    ) -> dict:
        """Append a shared dialogue event to a PTY session."""
        session.dialogue_seq += 1
        ts = time.time()
        event = {
            "id": f"{session.id}:{session.dialogue_seq}",
            "kind": kind,
            "label": label,
            "body": body,
            "meta": meta,
            "created_ts": ts,
            "time": time.strftime("%H:%M:%S", time.localtime(ts)),
        }
        session.dialogue_events.append(event)
        if len(session.dialogue_events) > 240:
            session.dialogue_events = session.dialogue_events[-240:]
        return event

    def list_dialogue_events(self, session: PTYSession) -> list:
        """Return shared dialogue events for a PTY session."""
        return list(session.dialogue_events)

    def resize(self, session: PTYSession, cols: int, rows: int):
        """Resize PTY window."""
        try:
            fcntl_module, _, struct_module, termios_module = _load_posix_pty_modules()
            winsize = struct_module.pack('HHHH', rows, cols, 0, 0)
            fcntl_module.ioctl(session.master_fd, termios_module.TIOCSWINSZ, winsize)
            session.cols = cols
            session.rows = rows
        except OSError as e:
            logger.error("Resize session %s failed: %s", session.id, e)

    async def replace_session(self, session_id: str, target_model: str = "sonnet") -> dict:
        """Replace an active PTY process in place with a clean Claude process resumed from the same native session."""
        result = {"attempted": False, "applied": False}
        session = self.get_session(session_id)
        if not session or session.status == "stopped":
            result["attempted"] = True
            result["reason"] = "session_not_found"
            return result

        target_model = str(target_model or "").strip() or "sonnet"
        native_session_id = session.native_session_id or self._infer_native_session_id(session)
        if not native_session_id:
            result["attempted"] = True
            result["reason"] = "native_session_unavailable"
            return result

        result["attempted"] = True
        result["target_model"] = target_model
        result["old_session_id"] = session.id
        result["native_session_id"] = native_session_id

        replacement = await self.create_session(
            name=session.name,
            cols=session.cols,
            rows=session.rows,
            cwd=session.cwd,
            resume_session_id=native_session_id,
            fork_session=True,
            initial_model=target_model,
            allow_overflow=True,
        )
        if replacement is None:
            result["reason"] = "create_failed"
            return result

        await asyncio.sleep(1.2)
        if replacement.status == "stopped":
            await self.destroy_session(replacement.id)
            result["reason"] = "replacement_failed"
            return result

        startup_output = replacement.buffer.read_all()
        replacement_native_session_id = replacement.native_session_id

        await self._stop_session_runtime(session)
        await self._stop_session_runtime(replacement, terminate=False, close_fd=False)
        self._sessions.pop(replacement.id, None)
        await self._redis_unregister(replacement.id)

        session.pid = replacement.pid
        session.master_fd = replacement.master_fd
        session.status = "running"
        session.resumed_from_session_id = native_session_id
        session.native_session_id = replacement_native_session_id or ""
        if startup_output:
            session.buffer.write(startup_output)
            session.last_output_at = time.time()
            if session.ws_clients:
                msg = {
                    "type": "output",
                    "data": startup_output.decode("utf-8", errors="replace"),
                }
                for ws in list(session.ws_clients):
                    if ws.closed:
                        continue
                    try:
                        asyncio.ensure_future(ws.send_json(msg))
                    except Exception:
                        pass
        session._reader_task = asyncio.create_task(self._output_reader_loop(session))
        session._native_session_task = asyncio.create_task(self._discover_native_session_id(session))
        await self._redis_register(session)

        result["applied"] = True
        result["mode"] = "migrated"
        result["new_session_id"] = session.id
        return result

    async def _discover_native_session_id(self, session: PTYSession):
        """Best-effort discovery of Claude's native session ID for later hot migration."""
        deadline = time.time() + CLAUDE_SESSION_DISCOVERY_MAX_WAIT
        while session.id in self._sessions and not session.native_session_id and time.time() < deadline:
            native_session_id = self._infer_native_session_id(session)
            if native_session_id:
                session.native_session_id = native_session_id
                logger.info("Mapped PTY session %s -> Claude session %s", session.id, native_session_id)
                return
            try:
                await asyncio.sleep(CLAUDE_SESSION_DISCOVERY_INTERVAL)
            except asyncio.CancelledError:
                return

    def _infer_native_session_id(self, session: PTYSession) -> str:
        """Infer Claude's native session ID from recent transcript files."""
        if session.native_session_id:
            return session.native_session_id
        if not CLAUDE_PROJECTS_DIR.exists():
            return ""

        assigned = {
            s.native_session_id
            for s in self._sessions.values()
            if s.id != session.id and s.native_session_id
        }
        best_match = ""
        best_delta = None

        try:
            candidates = sorted(
                CLAUDE_PROJECTS_DIR.rglob("*.jsonl"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except Exception:
            return ""

        for path in candidates[:CLAUDE_TRANSCRIPT_SCAN_LIMIT]:
            meta = self._read_transcript_start_meta(path)
            if not meta:
                continue
            native_session_id = meta.get("native_session_id", "")
            if (
                not native_session_id
                or native_session_id in assigned
                or meta.get("cwd") != session.cwd
            ):
                continue
            started_at = meta.get("started_at")
            if started_at is None:
                continue
            delta = abs(started_at - session.created_at)
            if delta > 120:
                continue
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best_match = native_session_id

        if best_match:
            session.native_session_id = best_match
        return best_match

    def _read_transcript_start_meta(self, path: Path) -> dict | None:
        """Read the first transcript event and extract Claude session metadata."""
        try:
            with path.open(encoding="utf-8") as f:
                for _ in range(12):
                    line = f.readline()
                    if not line:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except Exception:
                        continue
                    native_session_id = str(payload.get("sessionId", "") or "")
                    cwd = str(payload.get("cwd", "") or "")
                    if not native_session_id and not cwd:
                        continue
                    timestamp = payload.get("timestamp")
                    started_at = None
                    if timestamp:
                        try:
                            started_at = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")).timestamp()
                        except Exception:
                            started_at = None
                    return {
                        "native_session_id": native_session_id or path.stem,
                        "cwd": cwd,
                        "started_at": started_at,
                    }
        except Exception:
            return None
        return None

    async def _stop_session_runtime(self, session: PTYSession, terminate: bool = True,
                                    close_fd: bool = True):
        """Stop PTY readers/process without touching session registry or WebSocket clients."""
        if session._reader_task:
            session._reader_task.cancel()
            try:
                await session._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            session._reader_task = None

        if session._native_session_task:
            session._native_session_task.cancel()
            try:
                await session._native_session_task
            except (asyncio.CancelledError, Exception):
                pass
            session._native_session_task = None

        if session._flush_handle:
            session._flush_handle.cancel()
            session._flush_handle = None

        try:
            self._loop.remove_reader(session.master_fd)
        except Exception:
            pass

        if terminate:
            try:
                os.kill(session.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            else:
                for _ in range(50):
                    try:
                        pid, _ = os.waitpid(session.pid, os.WNOHANG)
                        if pid != 0:
                            break
                    except ChildProcessError:
                        break
                    await asyncio.sleep(0.1)
                else:
                    try:
                        os.kill(session.pid, signal.SIGKILL)
                        os.waitpid(session.pid, 0)
                    except (ProcessLookupError, ChildProcessError):
                        pass

        if close_fd:
            try:
                os.close(session.master_fd)
            except OSError:
                pass

    async def _output_reader_loop(self, session: PTYSession):
        """Read PTY output using asyncio event loop reader."""
        loop = asyncio.get_event_loop()
        output_ready = asyncio.Event()

        def _on_readable():
            output_ready.set()

        try:
            loop.add_reader(session.master_fd, _on_readable)

            while True:
                await output_ready.wait()
                output_ready.clear()

                try:
                    data = os.read(session.master_fd, 65536)
                    if not data:
                        break
                except (OSError, BlockingIOError) as e:
                    if isinstance(e, BlockingIOError):
                        continue
                    break

                session.last_output_at = time.time()
                session.buffer.write(data)
                session._output_batch.extend(data)

                # Schedule batch flush at ~60fps (16ms)
                if not session._flush_handle:
                    session._flush_handle = loop.call_later(
                        0.016, self._flush_output, session
                    )

        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error("Output reader for %s error: %s", session.id, e)
        finally:
            try:
                loop.remove_reader(session.master_fd)
            except Exception:
                pass

            # Mark session as stopped
            if session.id in self._sessions:
                session.status = "stopped"
                for ws in list(session.ws_clients):
                    try:
                        await ws.send_json({
                            "type": "session_ended",
                            "reason": "Process exited",
                        })
                    except Exception:
                        pass
                logger.info("Session %s PTY process ended", session.id)

    def _flush_output(self, session: PTYSession):
        """Flush batched output to WebSocket."""
        session._flush_handle = None
        if not session._output_batch:
            return

        data = bytes(session._output_batch)
        session._output_batch.clear()

        if session.ws_clients:
            msg = {
                "type": "output",
                "data": data.decode("utf-8", errors="replace"),
            }
            for ws in list(session.ws_clients):
                if ws.closed:
                    continue
                try:
                    asyncio.ensure_future(ws.send_json(msg))
                except Exception:
                    pass

    def _has_active_vizo_task(self) -> bool:
        """Check if any Vizo task is in a non-terminal state (running, paused, waiting confirm, etc.)."""
        terminal_statuses = {"completed", "failed", "rolled_back", "cancelled"}
        for task_dir in iter_task_dirs(_PROJECT_ROOT):
            progress_file = task_dir / "progress.json"
            if progress_file.is_file():
                try:
                    with open(progress_file) as f:
                        progress = json.load(f)
                    status = progress.get("status", "")
                    if status and status not in terminal_statuses:
                        return True
                except (json.JSONDecodeError, OSError):
                    continue

        return False

    async def _idle_check_loop(self):
        """Periodically check for idle sessions.
        
        Policy: never terminate sessions automatically.
        - If an Opus task is active (any non-terminal state), skip idle check entirely.
        - Otherwise, suspend idle sessions after idle_timeout_suspend (SIGSTOP).
        - Suspended sessions are auto-resumed on WS reconnect or user input.
        """
        while True:
            try:
                await asyncio.sleep(60)
                now = time.time()
                has_vizo = self._has_active_vizo_task()

                for session in list(self._sessions.values()):
                    if session.status == "stopped":
                        continue

                    # Never touch sessions while Opus tasks are active
                    if has_vizo:
                        continue

                    idle_time = now - max(session.last_input_at, session.last_output_at)

                    # Only suspend, never terminate — user can always reconnect
                    if idle_time > self._idle_suspend and session.status == "running":
                        logger.info(
                            "Session %s idle for %ds, suspending (will resume on reconnect)",
                            session.id, int(idle_time),
                        )
                        try:
                            os.kill(session.pid, signal.SIGSTOP)
                            session.status = "suspended"
                        except ProcessLookupError:
                            session.status = "stopped"

            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error("Idle check error: %s", e)

    async def _cleanup_orphans(self):
        """Clean up orphan sessions from Redis on startup."""
        try:
            redis = await self._get_redis()
            if not redis:
                return

            records = await redis.hgetall(REDIS_KEY)
            if not records:
                return

            cleaned = 0
            for session_id_bytes, data_bytes in records.items():
                session_id = session_id_bytes.decode() if isinstance(session_id_bytes, bytes) else session_id_bytes
                try:
                    info = json.loads(data_bytes)
                    pid = info.get("pid", 0)
                    if pid:
                        try:
                            os.kill(pid, 0)  # Check if alive
                            # Process exists but no local session — kill it
                            logger.info(
                                "Killing orphan PTY pid=%d (session=%s)",
                                pid, session_id,
                            )
                            os.kill(pid, signal.SIGTERM)
                            await asyncio.sleep(0.5)
                            try:
                                os.kill(pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            try:
                                os.waitpid(pid, os.WNOHANG)
                            except ChildProcessError:
                                pass
                        except ProcessLookupError:
                            pass  # Already dead
                except (json.JSONDecodeError, ValueError):
                    pass

                await redis.hdel(REDIS_KEY, session_id)
                cleaned += 1

            if cleaned:
                logger.info("Cleaned %d orphan session records", cleaned)

        except Exception as e:
            logger.error("Orphan cleanup error: %s", e)

    async def _opus_pubsub_loop(self):
        """Subscribe to Redis opus:progress:* and broadcast to all WebSocket clients."""
        import redis.asyncio as aioredis

        redis_config = self._config.get("redis", {})
        host = redis_config.get("host", "127.0.0.1")
        port = redis_config.get("port", 6380)

        while True:
            try:
                sub_redis = aioredis.Redis(host=host, port=port, decode_responses=True)
                pubsub = sub_redis.pubsub()
                await pubsub.psubscribe("opus:progress:*")
                logger.info("Opus PubSub subscriber connected (pattern=opus:progress:*)")

                async for message in pubsub.listen():
                    if message["type"] != "pmessage":
                        continue
                    try:
                        data = json.loads(message["data"])
                        await self._broadcast_opus_event(data)
                    except (json.JSONDecodeError, Exception) as e:
                        logger.debug("Opus PubSub parse error: %s", e)

            except asyncio.CancelledError:
                try:
                    await pubsub.punsubscribe("opus:progress:*")
                    await sub_redis.aclose()
                except Exception:
                    pass
                return
            except Exception as e:
                logger.warning("Opus PubSub error: %s, reconnecting in 5s", e)
                await asyncio.sleep(5)

    async def _broadcast_opus_event(self, data: dict):
        """Send opus_event to all active WebSocket connections."""
        for session in self._sessions.values():
            for ws in list(session.ws_clients):
                if not ws.closed:
                    try:
                        await ws.send_json({"type": "opus_event", "data": data})
                    except Exception:
                        pass

    async def _redis_register(self, session: PTYSession):
        """Register session in Redis."""
        try:
            redis = await self._get_redis()
            if redis:
                data = json.dumps({
                    "id": session.id,
                    "name": session.name,
                    "pid": session.pid,
                    "status": session.status,
                    "created_at": session.created_at,
                })
                await redis.hset(REDIS_KEY, session.id, data)
        except Exception as e:
            logger.error("Redis register error: %s", e)

    async def _redis_unregister(self, session_id: str):
        """Unregister session from Redis."""
        try:
            redis = await self._get_redis()
            if redis:
                await redis.hdel(REDIS_KEY, session_id)
        except Exception as e:
            logger.error("Redis unregister error: %s", e)
