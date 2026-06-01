from __future__ import annotations

import json
import re
from pathlib import Path

from lib.paths import write_data_path

from .contracts import RuntimeSessionRecord
from .events import UnifiedEvent, utcnow_iso


_SAFE_NAME_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_name(value: str) -> str:
    cleaned = _SAFE_NAME_PATTERN.sub("-", value or "")
    cleaned = cleaned.strip("-")
    return cleaned or "default"


class RuntimeEventStore:
    """Persist unified events and raw runtime events beside task artifacts."""

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.base_dir / "events.jsonl"
        self.raw_events_path = self.base_dir / "raw-events.jsonl"

    @classmethod
    def for_subagent(cls, task_dir: Path | str, step_name: str, role: str) -> "RuntimeEventStore":
        task_root = Path(task_dir)
        store_name = _safe_name(step_name or role)
        return cls(task_root / "runtime" / "subagents" / store_name)

    @classmethod
    def for_main_session(cls, session_dir: Path | str) -> "RuntimeEventStore":
        return cls(Path(session_dir))

    def append_unified(self, event: UnifiedEvent) -> None:
        self._append_line(self.events_path, event.to_dict())

    def append_raw(self, raw_event: dict) -> None:
        payload = {
            "timestamp": utcnow_iso(),
            "event": raw_event,
        }
        self._append_line(self.raw_events_path, payload)

    @staticmethod
    def _append_line(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    @staticmethod
    def iter_lines(path: Path) -> list[dict]:
        if not path.exists():
            return []
        items: list[dict] = []
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        items.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return []
        return items


class RuntimeSessionStore:
    """Shared runtime session registry skeleton for future multi-runtime resume."""

    def __init__(self, project_root: Path | str | None = None):
        self.root = write_data_path("sessions", "runtime", project_root=project_root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, record: RuntimeSessionRecord) -> Path:
        filename = _safe_name(record.session_key) + ".json"
        path = self.root / filename
        data = record.to_dict()
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def load(self, session_key: str) -> RuntimeSessionRecord | None:
        path = self.root / (_safe_name(session_key) + ".json")
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return RuntimeSessionRecord(**data)
