import asyncio
import json
import os
from types import SimpleNamespace

from lib.runtime.sessions.persistent_runtime import (
    ClaudePersistentSessionRuntime,
    RuntimeArtifact,
    _extract_claude_permission_prompt,
    _read_claude_transcript_start_meta,
)


def test_claude_permission_prompt_parser_supports_yn_prompt():
    prompt = _extract_claude_permission_prompt(
        "\x1b[?25l"
        "Bash command\n"
        "python -m pytest tests/test_claude_persistent_runtime.py\n"
        "Allow this action? (y/n)\n"
    )

    assert prompt
    assert prompt["prompt_style"] == "yn"
    assert prompt["available_actions"] == ["approve_once", "deny"]
    assert prompt["response_keys"] == {"approve_once": "y\r", "deny": "n\r"}
    assert prompt["command"] == "python -m pytest tests/test_claude_persistent_runtime.py"


def test_claude_permission_prompt_parser_supports_numbered_prompt():
    prompt = _extract_claude_permission_prompt(
        "Bash command\n"
        "npm run lint\n"
        "Do you want to proceed?\n"
        "❯ 1. Yes\n"
        "  2. Yes, and don't ask again for npm commands in this project\n"
        "  3. No, and tell Claude what to do differently\n"
    )

    assert prompt
    assert prompt["prompt_style"] == "numbered"
    assert prompt["available_actions"] == ["approve_once", "approve_prefix", "deny"]
    assert prompt["response_keys"] == {"approve_once": "1\r", "approve_prefix": "2\r", "deny": "3\r"}
    assert prompt["command"] == "npm run lint"


def test_claude_runtime_emits_permission_interaction_and_submits_response(tmp_path, monkeypatch):
    async def run_check():
        runtime = ClaudePersistentSessionRuntime(
            session_id="ms_test",
            project_root=tmp_path,
            config={},
        )
        runtime.pid = os.getpid()
        runtime.master_fd = 123
        runtime.native_session_id = "thread-1"
        runtime._stream_event_cb = emitted.append
        runtime._recent_output = (
            "Bash command\n"
            "npm run lint\n"
            "Do you want to proceed?\n"
            "❯ 1. Yes\n"
            "  2. Yes, and don't ask again for npm commands in this project\n"
            "  3. No, and tell Claude what to do differently\n"
        )

        await runtime._handle_runtime_output_interactions("")

        assert len(emitted) == 1
        event = emitted[0]
        assert event["type"] == "interaction_requested"
        assert event["thread_id"] == "thread-1"
        payload = event["payload"]
        assert payload["runtime_family"] == "claude_code"
        assert payload["interaction_kind"] == "approval"
        assert payload["available_actions"] == ["approve_once", "approve_prefix", "deny"]

        result = await runtime.submit_interaction_response(
            request_id=payload["request_id"],
            action="approve_prefix",
        )

        assert written == [b"2\r"]
        assert result["current_turn_status"] == "running"
        assert result["pending_interaction"] == {}

    emitted = []
    written = []

    def fake_write(payload):
        written.append(bytes(payload))

    monkeypatch.setattr(
        ClaudePersistentSessionRuntime,
        "_write_master_bytes",
        lambda self, payload: fake_write(payload),
    )

    asyncio.run(run_check())


def test_claude_pending_interaction_pauses_main_turn_timeout(tmp_path):
    async def run_check():
        transcript = tmp_path / "thread-1.jsonl"
        transcript.write_text("", encoding="utf-8")

        runtime = ClaudePersistentSessionRuntime(
            session_id="ms_test",
            project_root=tmp_path,
            config={
                "main_session_max_timeout": 1,
                "main_session_interaction_timeout": 3,
            },
        )
        runtime.pid = os.getpid()
        artifact = RuntimeArtifact(native_session_id="thread-1", transcript_path=transcript)
        session = SimpleNamespace(display_model="sonnet", provider_model="claude-sonnet-4-5")
        decision = SimpleNamespace(display_model="sonnet", provider_model="claude-sonnet-4-5", selected_model="sonnet")

        async def simulate_prompt_and_resume():
            await asyncio.sleep(0.1)
            await runtime._enqueue_interaction_request(
                request={
                    "request_id": "claude_wait_1",
                    "interaction_kind": "approval",
                    "summary": "需要确认",
                    "status": "pending",
                },
                native_session_id="thread-1",
            )
            await asyncio.sleep(1.2)
            async with runtime._interaction_state_lock:
                runtime._interaction_queue.clear()
            runtime._interaction_resume_event.set()
            with transcript.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "sessionId": "thread-1",
                            "type": "assistant",
                            "message": {
                                "model": "claude-sonnet-4-5",
                                "content": [{"type": "text", "text": "continued after approval"}],
                                "stop_reason": "end_turn",
                                "usage": {"output_tokens": 4},
                            },
                        }
                    )
                    + "\n"
                )

        simulator = asyncio.create_task(simulate_prompt_and_resume())
        result = await runtime._wait_for_turn_result(
            session=session,
            decision=decision,
            artifact=artifact,
            offset=0,
        )
        await simulator

        assert result["result"] == "continued after approval"

    asyncio.run(run_check())


def test_claude_transcript_meta_skips_permission_mode_preamble(tmp_path):
    transcript = tmp_path / "03cd24f6-47c5-4745-b9c7-6a36ca13cbe7.jsonl"
    transcript.write_text(
        "\n".join(
            [
                '{"type":"permission-mode","permissionMode":"bypassPermissions","sessionId":"03cd24f6-47c5-4745-b9c7-6a36ca13cbe7"}',
                '{"type":"file-history-snapshot","messageId":"m1"}',
                (
                    '{"type":"user","cwd":"/home/user/project",'
                    '"sessionId":"03cd24f6-47c5-4745-b9c7-6a36ca13cbe7",'
                    '"timestamp":"2026-05-27T15:25:41.972Z"}'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    meta = _read_claude_transcript_start_meta(transcript)

    assert meta
    assert meta["native_session_id"] == "03cd24f6-47c5-4745-b9c7-6a36ca13cbe7"
    assert meta["cwd"] == "/home/user/project"
    assert meta["started_at"] is not None
