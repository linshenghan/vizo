from __future__ import annotations

import os
from pathlib import Path

DEFAULT_CONFIRM_SERVER_PORT = 9390


def resolve_confirm_server_pid_file(project_root: Path | None = None) -> Path:
    """Return a writable PID file path for confirm_server."""
    root = (project_root or Path(__file__).resolve().parent.parent).resolve()
    candidates = [
        Path.home() / ".local" / "state" / "vizo" / "confirm_server.pid",
        root / "lib" / "confirm_server.pid",
        Path("/tmp/vizo-confirm_server.pid"),
    ]
    for candidate in candidates:
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            if candidate.exists() and not os.access(candidate, os.W_OK):
                continue
            probe = candidate.parent / ".confirm_server_pid_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return candidate
        except OSError:
            continue
    return Path("/tmp/vizo-confirm_server.pid")


def resolve_confirm_server_port(config: dict | None = None) -> int:
    try:
        return int((config or {}).get("confirm_server", {}).get("port", DEFAULT_CONFIRM_SERVER_PORT))
    except (TypeError, ValueError, AttributeError):
        return DEFAULT_CONFIRM_SERVER_PORT
