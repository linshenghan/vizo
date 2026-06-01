from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from lib.paths import write_data_path

from .contracts import MainSessionRecord
from .event_store import RuntimeEventStore, _safe_name


class MainSessionStore:
    """Persistent store for Vizo-owned main sessions."""

    def __init__(self, project_root: Path | str | None = None):
        self.root = write_data_path("sessions", "main", project_root=project_root)
        self.root.mkdir(parents=True, exist_ok=True)

    def session_dir(self, session_id: str) -> Path:
        return self.root / _safe_name(session_id)

    def session_file(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "session.json"

    def event_store(self, session_id: str) -> RuntimeEventStore:
        return RuntimeEventStore.for_main_session(self.session_dir(session_id))

    def raw_log_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "raw.log"

    def turn_requests_dir(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "turns"

    def turn_request_path(self, session_id: str, turn_id: str) -> Path:
        return self.turn_requests_dir(session_id) / f"{_safe_name(turn_id)}.json"

    def save(self, record: MainSessionRecord) -> Path:
        session_dir = self.session_dir(record.session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        path = self.session_file(record.session_id)
        path.write_text(
            json.dumps(record.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return path

    def load(self, session_id: str) -> MainSessionRecord | None:
        path = self.session_file(session_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return MainSessionRecord(**data)

    def list(self) -> list[MainSessionRecord]:
        records: list[MainSessionRecord] = []
        for item in sorted(self.root.glob("*/session.json"), reverse=True):
            try:
                data = json.loads(item.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            try:
                records.append(MainSessionRecord(**data))
            except TypeError:
                continue
        records.sort(
            key=lambda record: (
                record.updated_at or record.created_at or "",
                record.session_id,
            ),
            reverse=True,
        )
        return records

    def delete(self, session_id: str) -> None:
        shutil.rmtree(self.session_dir(session_id), ignore_errors=True)

    def save_turn_request(
        self,
        session_id: str,
        turn_id: str,
        payload: dict[str, Any],
    ) -> Path:
        path = self.turn_request_path(session_id, turn_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    def load_turn_request(self, session_id: str, turn_id: str) -> dict[str, Any] | None:
        path = self.turn_request_path(session_id, turn_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return data if isinstance(data, dict) else None

    def append_raw_log(self, session_id: str, text: str) -> Path:
        path = self.raw_log_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def read_raw_log(self, session_id: str, *, offset: int = 0, limit: int = 65536) -> dict:
        path = self.raw_log_path(session_id)
        if not path.exists():
            return {
                "offset": max(0, int(offset)),
                "next_offset": max(0, int(offset)),
                "content": "",
                "path": str(path),
            }

        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 1_000_000))
        with path.open("r", encoding="utf-8") as handle:
            handle.seek(offset)
            content = handle.read(limit)
            next_offset = handle.tell()
        return {
            "offset": offset,
            "next_offset": next_offset,
            "content": content,
            "path": str(path),
        }

    def read_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        before_seq: int | None = None,
        limit: int = 200,
    ) -> list[dict]:
        limit = max(1, min(int(limit), 1000))
        items = RuntimeEventStore.iter_lines(self.event_store(session_id).events_path)
        if before_seq is not None and int(before_seq) > 0:
            result = [item for item in items if int(item.get("sequence") or 0) < int(before_seq)]
            return result[-limit:]
        result = [item for item in items if int(item.get("sequence") or 0) > int(after_seq)]
        return result[:limit]
