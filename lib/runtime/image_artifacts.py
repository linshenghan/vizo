from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .events import utcnow_iso


IMAGE_ARTIFACT_EVENT_NAME = "assistant_image"

_ARTIFACT_DIR_NAME = "images"
_SAFE_NAME_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")
_SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


def capture_image_generation_artifact(
    *,
    session_dir: Path,
    session_id: str,
    turn_id: str,
    raw_event: dict[str, Any],
    requested_prompt: str = "",
) -> dict[str, Any] | None:
    payload = extract_image_generation_payload(raw_event)
    if not payload:
        return None

    result = str(payload.get("result") or "").strip()
    if not result:
        return None

    image_bytes = _decode_base64_image(result)
    if not image_bytes:
        return None

    digest = hashlib.sha256(image_bytes).hexdigest()
    source_id = str(payload.get("id") or payload.get("call_id") or "").strip()
    prefix = _safe_name(source_id or "image")[:72]
    image_id = f"{prefix}-{digest[:12]}"
    extension, mime_type = _detect_image_type(image_bytes)
    root = _image_artifact_dir(session_dir)
    root.mkdir(parents=True, exist_ok=True)
    image_path = root / f"{image_id}.{extension}"
    metadata_path = root / f"{image_id}.json"

    if not image_path.exists():
        image_path.write_bytes(image_bytes)

    created_at = utcnow_iso()
    if metadata_path.exists():
        try:
            existing = json.loads(metadata_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                created_at = str(existing.get("created_at") or created_at)
        except (json.JSONDecodeError, OSError):
            pass

    metadata = {
        "id": image_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "source": "codex_image_generation",
        "source_id": source_id,
        "status": _coerce_completed_status(payload),
        "prompt": _truncate_text(str(payload.get("prompt") or requested_prompt or ""), 4000),
        "revised_prompt": _truncate_text(str(payload.get("revised_prompt") or ""), 4000),
        "mime_type": mime_type,
        "extension": extension,
        "size": len(image_bytes),
        "sha256": digest,
        "created_at": created_at,
    }
    metadata["url"] = build_image_artifact_url(session_id, image_id)
    metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return metadata


def extract_image_generation_payload(raw_event: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(raw_event, dict):
        return None
    event_type = str(raw_event.get("type") or "")
    payload = raw_event.get("payload", {})
    payload = payload if isinstance(payload, dict) else {}
    payload_type = str(payload.get("type") or "")

    if event_type == "response_item" and payload_type == "image_generation_call":
        return payload
    if event_type == "event_msg" and payload_type in {"image_generation_call", "image_generation_end"}:
        return payload
    if event_type in {"image_generation_call", "image_generation_end"}:
        return raw_event
    return None


def is_image_generation_event(raw_event: dict[str, Any]) -> bool:
    return extract_image_generation_payload(raw_event) is not None


def scrub_image_generation_event(raw_event: dict[str, Any]) -> dict[str, Any]:
    payload = extract_image_generation_payload(raw_event)
    if not payload or "result" not in payload:
        return raw_event

    cleaned = copy.deepcopy(raw_event)
    cleaned_payload = extract_image_generation_payload(cleaned)
    if cleaned_payload is not None:
        result = str(cleaned_payload.get("result") or "")
        cleaned_payload["result"] = ""
        cleaned_payload["result_omitted"] = True
        cleaned_payload["result_omitted_chars"] = len(result)
    return cleaned


def list_image_artifacts(
    *,
    session_dir: Path,
    session_id: str,
    backfill_from_raw_events: bool = True,
) -> list[dict[str, Any]]:
    if backfill_from_raw_events:
        backfill_image_artifacts_from_raw_events(session_dir=session_dir, session_id=session_id)

    root = _image_artifact_dir(session_dir)
    if not root.exists():
        return []

    images: list[dict[str, Any]] = []
    for path in root.glob("*.json"):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(item, dict):
            continue
        image_id = str(item.get("id") or path.stem)
        if not _is_safe_artifact_id(image_id):
            continue
        extension = str(item.get("extension") or "png").strip().lstrip(".") or "png"
        if not (root / f"{image_id}.{extension}").exists():
            continue
        item["id"] = image_id
        item["url"] = build_image_artifact_url(session_id, image_id)
        images.append(item)

    images.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")), reverse=True)
    return images


def backfill_image_artifacts_from_raw_events(*, session_dir: Path, session_id: str) -> list[dict[str, Any]]:
    raw_events_path = Path(session_dir) / "raw-events.jsonl"
    if not raw_events_path.exists():
        return []

    captured: list[dict[str, Any]] = []
    try:
        with raw_events_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    container = json.loads(line)
                except json.JSONDecodeError:
                    continue
                raw_event = container.get("event") if isinstance(container, dict) else None
                if not isinstance(raw_event, dict) and isinstance(container, dict):
                    raw_event = container
                if not isinstance(raw_event, dict):
                    continue
                image = capture_image_generation_artifact(
                    session_dir=session_dir,
                    session_id=session_id,
                    turn_id=str(raw_event.get("turn_id") or "backfill"),
                    raw_event=raw_event,
                )
                if image:
                    captured.append(image)
    except OSError:
        return captured
    return captured


def resolve_image_artifact_path(*, session_dir: Path, image_id: str) -> tuple[Path, str]:
    safe_id = str(image_id or "").strip()
    if not _is_safe_artifact_id(safe_id):
        raise FileNotFoundError("image_not_found")

    root = _image_artifact_dir(session_dir)
    metadata_path = root / f"{safe_id}.json"
    if not metadata_path.exists():
        raise FileNotFoundError("image_not_found")

    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}

    extension = str(metadata.get("extension") or "png").strip().lstrip(".") or "png"
    mime_type = str(metadata.get("mime_type") or _mime_for_extension(extension))
    image_path = root / f"{safe_id}.{extension}"
    if not image_path.exists():
        raise FileNotFoundError("image_not_found")
    return image_path, mime_type


def build_image_artifact_url(session_id: str, image_id: str) -> str:
    return (
        "/vizo/console/api/sessions/"
        + quote(str(session_id or ""), safe="")
        + "/images/"
        + quote(str(image_id or ""), safe="")
    )


def _image_artifact_dir(session_dir: Path) -> Path:
    return Path(session_dir) / "artifacts" / _ARTIFACT_DIR_NAME


def _safe_name(value: str) -> str:
    cleaned = _SAFE_NAME_PATTERN.sub("-", str(value or ""))
    cleaned = cleaned.strip(".-")
    return cleaned or "image"


def _is_safe_artifact_id(value: str) -> bool:
    text = str(value or "")
    return bool(text and _SAFE_ID_PATTERN.fullmatch(text) and "/" not in text and "\\" not in text and ".." not in text)


def _decode_base64_image(value: str) -> bytes:
    text = str(value or "").strip()
    if text.startswith("data:") and "," in text:
        text = text.split(",", 1)[1]
    text = "".join(text.split())
    if not text:
        return b""
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return b""


def _detect_image_type(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "gif", "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return "png", "image/png"


def _mime_for_extension(extension: str) -> str:
    ext = str(extension or "").lower().lstrip(".")
    if ext in {"jpg", "jpeg"}:
        return "image/jpeg"
    if ext == "gif":
        return "image/gif"
    if ext == "webp":
        return "image/webp"
    return "image/png"


def _coerce_completed_status(payload: dict[str, Any]) -> str:
    if payload.get("result"):
        return "completed"
    return str(payload.get("status") or "").strip() or "completed"


def _truncate_text(value: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."
