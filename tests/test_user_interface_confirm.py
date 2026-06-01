import asyncio
import builtins
import json

import pytest

import user_interface
from user_interface import UserInterface


class _Live:
    def __init__(self):
        self.is_started = True
        self.calls = []

    def stop(self):
        self.calls.append("stop")
        self.is_started = False

    def start(self):
        self.calls.append("start")
        self.is_started = True


class _Display:
    def __init__(self):
        self._live = _Live()


def test_terminal_confirm_restores_progress_when_input_fails(monkeypatch):
    ui = UserInterface({"ui_mode": "terminal"})
    display = _Display()
    ui._progress_display = display

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def fail_input(_prompt):
        raise KeyboardInterrupt()

    monkeypatch.setattr(builtins, "input", fail_input)

    with pytest.raises(KeyboardInterrupt):
        ui._terminal_confirm("continue?")

    assert display._live.calls == ["stop", "start"]
    assert display._live.is_started is True


def test_upload_preview_writes_backup_and_returns_link_when_redis_fails(tmp_path, monkeypatch):
    preview_file = tmp_path / "result.md"
    preview_file.write_text("# Result\n\nDone.", encoding="utf-8")
    backup_dir = tmp_path / "previews"
    monkeypatch.setattr(user_interface, "PREVIEWS_DIR", backup_dir)

    class FailingRedis:
        async def set(self, *args, **kwargs):
            raise TimeoutError("redis unavailable")

        async def close(self):
            pass

    import redis.asyncio as aioredis

    monkeypatch.setattr(aioredis, "from_url", lambda *args, **kwargs: FailingRedis())

    ui = UserInterface({
        "preview_redis_url": "redis://127.0.0.1:1",
        "wecom": {"callback_server": {"url": "http://127.0.0.1:9390/wecom/callback"}},
    })

    link = asyncio.run(ui._upload_preview(preview_file))

    assert link.startswith("http://127.0.0.1:9390/vizo/preview/")
    preview_id = link.rsplit("/", 1)[1]
    backup_file = backup_dir / f"{preview_id}.json"
    assert backup_file.exists()
    payload = json.loads(backup_file.read_text(encoding="utf-8"))
    assert payload["filename"] == "result.md"
    assert "Done." in payload["html"]
