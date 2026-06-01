from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .controller import MainSessionController


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one detached Vizo main-session turn.")
    parser.add_argument("--project-root", required=True, help="Project root containing .vizo state")
    parser.add_argument("--session-id", required=True, help="Main session id")
    parser.add_argument("--turn-id", required=True, help="Turn id to process")
    return parser.parse_args()


async def _run() -> int:
    args = parse_args()
    controller = MainSessionController(
        project_root=Path(args.project_root).resolve(),
        repair_running_sessions=False,
    )
    await controller.run_turn_request(session_id=args.session_id, turn_id=args.turn_id)
    return 0


def main() -> int:
    try:
        return asyncio.run(_run())
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
