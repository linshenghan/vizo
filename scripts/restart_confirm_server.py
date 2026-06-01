#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lib.config_loader import load_config
from lib.confirm_server_runtime import resolve_confirm_server_pid_file, resolve_confirm_server_port


def _health_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/health"


def _probe_health(port: int, timeout: float = 2.0) -> dict | None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(_health_url(port), timeout=timeout) as resp:
            payload = resp.read().decode("utf-8")
            return json.loads(payload)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
        return None


def _run_confirm_server_cmd(*args: str, capture_output: bool = True) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, str(PROJECT_ROOT / "lib" / "confirm_server.py"), *args]
    run_kwargs = {
        "cwd": PROJECT_ROOT,
        "text": True,
        "check": False,
    }
    if capture_output:
        run_kwargs["capture_output"] = True
    else:
        # `confirm_server.py start` forks a long-lived child. If we pipe stdout/stderr
        # here, the forked child inherits those pipe fds and keeps them open, which
        # prevents subprocess.run() from seeing EOF and makes this restart script hang
        # even though the service is already healthy.
        run_kwargs["stdin"] = subprocess.DEVNULL
        run_kwargs["stdout"] = subprocess.DEVNULL
        run_kwargs["stderr"] = subprocess.DEVNULL
    return subprocess.run(cmd, **run_kwargs)


def main() -> int:
    parser = argparse.ArgumentParser(description="Restart Vizo confirm_server using the configured runtime port.")
    parser.add_argument("--port", type=int, default=None, help="Override confirm_server port")
    parser.add_argument("--wait-seconds", type=float, default=8.0, help="Health-check wait timeout")
    args = parser.parse_args()

    config = load_config(force_reload=True)
    port = args.port or resolve_confirm_server_port(config)

    stop_proc = _run_confirm_server_cmd("stop")
    _run_confirm_server_cmd("start", "-p", str(port), capture_output=False)

    deadline = time.time() + max(args.wait_seconds, 1.0)
    health = None
    while time.time() < deadline:
        health = _probe_health(port)
        if health:
            break
        time.sleep(0.5)

    pid_file = resolve_confirm_server_pid_file(PROJECT_ROOT)
    pid_text = pid_file.read_text(encoding="utf-8").strip() if pid_file.exists() else ""

    if stop_proc.stdout.strip():
        print(stop_proc.stdout.strip())
    if stop_proc.stderr.strip():
        print(stop_proc.stderr.strip(), file=sys.stderr)
    if not health:
        print(f"confirm_server restart failed health-check: {_health_url(port)}", file=sys.stderr)
        return 1

    print(f"confirm_server restart command issued on port {port}")
    print(f"confirm_server healthy on {_health_url(port)}")
    if pid_text:
        print(f"PID: {pid_text}")
    print(f"Health: {json.dumps(health, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
