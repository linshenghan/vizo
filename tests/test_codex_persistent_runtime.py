import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

from lib.mcp_runtime import build_codex_mcp_config_overrides
from lib.runtime.contracts import (
    MainSessionRecord,
    RUNTIME_FAMILY_CLAUDE_CODE,
    RUNTIME_FAMILY_CODEX,
    RouteDecision,
    RuntimeExecutionContext,
)
from lib.runtime.events import build_unified_events
from lib.runtime.image_artifacts import (
    capture_image_generation_artifact,
    list_image_artifacts,
    scrub_image_generation_event,
)
from lib.runtime.sessions.persistent_runtime import (
    ClaudePersistentSessionRuntime,
    CodexPersistentSessionRuntime,
    RuntimeArtifact,
    _extract_codex_mcp_approval_prompt,
)
from lib.runtime.sessions.codex import CodexMainSessionAdapter
from lib.runtime.sessions.controller import MainSessionController, _materialize_main_session_attachments


def test_codex_mcp_overrides_keep_hyphenated_server_names_unquoted(tmp_path):
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "vizo-router": {"command": "python3", "args": ["lib/vizo_router_mcp_stdio.py"]},
                    "mcp-chrome": {"command": "python3", "args": ["lib/chrome_mcp_stdio.py"]},
                }
            }
        ),
        encoding="utf-8",
    )

    overrides = build_codex_mcp_config_overrides(
        str(tmp_path),
        config_data={"mcp_service_state": {"mcp-chrome": {"enabled": False}}},
    )

    assert any(item.startswith("mcp_servers.vizo-router=") for item in overrides)
    assert not any(item.startswith('mcp_servers."vizo-router"=') for item in overrides)


def test_codex_mcp_overrides_embed_vizo_router_session_env(tmp_path):
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "vizo-router": {"command": "python3", "args": ["lib/vizo_router_mcp_stdio.py"]},
                }
            }
        ),
        encoding="utf-8",
    )
    runtime_dir = tmp_path / ".vizo" / "sessions" / "main" / "ms_test" / "runtime"

    overrides = build_codex_mcp_config_overrides(
        str(tmp_path),
        session_id="ms_test",
        runtime_dir=str(runtime_dir),
        project_root=str(tmp_path),
    )

    router_override = next(item for item in overrides if item.startswith("mcp_servers.vizo-router="))
    assert 'VIZO_MAIN_SESSION_ID="ms_test"' in router_override
    assert f'VIZO_MAIN_SESSION_DIR="{runtime_dir.parent}"' in router_override
    assert f'VIZO_MAIN_SESSION_RUNTIME_DIR="{runtime_dir}"' in router_override
    assert f'VIZO_PROJECT_ROOT="{tmp_path}"' in router_override


def test_codex_mcp_overrides_can_be_limited_to_role_servers(tmp_path):
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "serena": {"command": "serena", "args": ["start-mcp-server"]},
                    "jina": {"command": "jina-mcp"},
                    "playwright": {"command": "npx", "args": ["@playwright/mcp@latest"]},
                }
            }
        ),
        encoding="utf-8",
    )

    overrides = build_codex_mcp_config_overrides(
        str(tmp_path),
        allowed_server_names=["serena"],
    )

    assert len(overrides) == 1
    assert overrides[0].startswith("mcp_servers.serena=")


def test_codex_prompt_write_handles_partial_nonblocking_pty_writes(tmp_path, monkeypatch):
    runtime = CodexPersistentSessionRuntime(
        session_id="ms_test",
        project_root=tmp_path,
        config={
            "main_session_pty_write_chunk_size": 128,
            "main_session_pty_write_timeout": 1,
        },
    )
    runtime.master_fd = 123
    runtime.pid = 0
    written = bytearray()
    attempts = 0

    def fake_write(fd, payload):
        nonlocal attempts
        assert fd == 123
        attempts += 1
        if attempts == 2:
            raise BlockingIOError()
        chunk = bytes(payload[:7])
        written.extend(chunk)
        return len(chunk)

    def fake_select(readable, writable, exceptional, timeout):
        assert readable == []
        assert writable == [123]
        assert exceptional == []
        assert timeout > 0
        return [], writable, []

    monkeypatch.setattr("lib.runtime.sessions.persistent_runtime.os.write", fake_write)
    monkeypatch.setattr("lib.runtime.sessions.persistent_runtime.select.select", fake_select)

    prompt = "中文提示" * 2048
    runtime._send_prompt(prompt)

    expected = ("\x1b[200~" + prompt + "\x1b[201~\r").encode("utf-8")
    assert bytes(written) == expected
    assert attempts > 2


def test_claude_interrupt_turn_sends_escape_then_ctrl_c(tmp_path, monkeypatch):
    runtime = ClaudePersistentSessionRuntime(
        session_id="ms_test",
        project_root=tmp_path,
        config={"main_session_interrupt_ctrl_c_delay": 0},
    )
    runtime.master_fd = 123
    runtime.pid = 0
    written = []

    def fake_write(payload):
        written.append(bytes(payload))

    monkeypatch.setattr(runtime, "_write_master_bytes", fake_write)

    result = asyncio.run(runtime.interrupt_turn())

    assert result["signal"] == "esc_ctrl_c"
    assert written == [b"\x1b", b"\x03"]


def test_controller_interrupt_turn_marks_session_and_clears_pending(tmp_path):
    async def run_check():
        class FakeRuntime:
            def __init__(self):
                self.calls = []

            def is_alive(self):
                return True

            async def interrupt_turn(self, *, force=False):
                self.calls.append(force)
                return {"mode": "force" if force else "soft", "signal": "ctrl_c"}

        async def sleeper():
            await asyncio.sleep(60)

        controller = MainSessionController(
            config={"projects": {}},
            project_root=tmp_path,
            repair_running_sessions=False,
        )
        session = MainSessionRecord(
            session_id="ms_test",
            name="test",
            cwd=str(tmp_path),
            status="running",
            runtime_family=RUNTIME_FAMILY_CODEX,
            runtime_kind=RUNTIME_FAMILY_CODEX,
            display_model="gpt-5.5",
            provider_model="gpt-5.5",
            connection_id="conn_test",
            current_turn_id="turn_test",
            current_turn_status="running",
            metadata={
                "pending_inputs": [{"kind": "message", "content": "queued"}],
                "pending_model_switch": {"display_model": "gpt-5.4"},
            },
        )
        controller.store.save(session)
        runtime = FakeRuntime()
        controller._persistent_runtimes["ms_test"] = runtime
        task = asyncio.create_task(sleeper())
        controller._turn_tasks["ms_test"] = task

        result = await controller.interrupt_turn("ms_test")
        saved = controller.store.load("ms_test")

        assert result["status"] == "interrupted"
        assert result["dropped_pending_count"] == 2
        assert runtime.calls == [False]
        assert task.cancelled()
        assert saved.status == "interrupted"
        assert saved.current_turn_status == "interrupted"
        assert saved.last_error_code == "turn_interrupted"
        assert saved.pending_interaction == {}
        assert not saved.to_public_dict()["has_pending_inputs"]

    asyncio.run(run_check())


def test_controller_send_pending_input_now_interrupts_and_submits(tmp_path, monkeypatch):
    async def run_check():
        class FakeRuntime:
            def __init__(self):
                self.calls = []

            def is_alive(self):
                return True

            async def interrupt_turn(self, *, force=False):
                self.calls.append(force)
                return {"mode": "force" if force else "soft", "signal": "ctrl_c"}

        controller = MainSessionController(
            config={"projects": {}, "main_session_send_now_interrupt_delay": 0},
            project_root=tmp_path,
            repair_running_sessions=False,
        )
        session = MainSessionRecord(
            session_id="ms_test",
            name="test",
            cwd=str(tmp_path),
            status="running",
            runtime_family=RUNTIME_FAMILY_CODEX,
            runtime_kind=RUNTIME_FAMILY_CODEX,
            display_model="gpt-5.5",
            provider_model="gpt-5.5",
            connection_id="conn_test",
            current_turn_id="turn_active",
            current_turn_status="running",
            metadata={
                "pending_inputs": [
                    {"queue_id": "q1", "kind": "message", "content": "send this now", "attachments": []},
                    {"queue_id": "q2", "kind": "message", "content": "keep queued", "attachments": []},
                ],
            },
        )
        controller.store.save(session)
        runtime = FakeRuntime()
        controller._persistent_runtimes["ms_test"] = runtime
        task = asyncio.create_task(asyncio.sleep(60))
        controller._turn_tasks["ms_test"] = task
        submitted = {}

        async def fake_submit_message(
            session_id,
            *,
            content,
            attachments,
            expected_runtime="",
            repair_sessions=True,
        ):
            submitted.update(
                {
                    "session_id": session_id,
                    "content": content,
                    "attachments": attachments,
                    "expected_runtime": expected_runtime,
                }
            )
            saved = controller.store.load(session_id)
            saved.status = "running"
            saved.current_turn_status = "running"
            saved.current_turn_id = "turn_now"
            controller.store.save(saved)
            return "turn_now"

        monkeypatch.setattr(controller, "submit_message", fake_submit_message)

        before = controller.store.load("ms_test").to_public_dict()
        assert before["pending_input_count"] == 2
        assert before["pending_inputs"][0]["queue_id"] == "q1"

        result = await controller.send_pending_input_now("ms_test", "q1", expected_runtime=RUNTIME_FAMILY_CODEX)
        saved = controller.store.load("ms_test")

        assert result["status"] == "pending_input_sent_now"
        assert result["turn_id"] == "turn_now"
        assert runtime.calls == [False]
        assert task.cancelled()
        assert submitted["content"] == "send this now"
        assert submitted["expected_runtime"] == RUNTIME_FAMILY_CODEX
        assert [item["queue_id"] for item in saved.metadata["pending_inputs"]] == ["q2"]
        assert saved.to_public_dict()["pending_inputs"][0]["content"] == "keep queued"

    asyncio.run(run_check())


def test_submit_message_prelogs_user_event_before_runtime_starts(tmp_path, monkeypatch):
    async def run_check():
        controller = MainSessionController(
            config={"projects": {}},
            project_root=tmp_path,
            repair_running_sessions=False,
        )
        session = MainSessionRecord(
            session_id="ms_test",
            name="test",
            cwd=str(tmp_path),
            status="idle",
            runtime_family=RUNTIME_FAMILY_CODEX,
            runtime_kind=RUNTIME_FAMILY_CODEX,
            display_model="gpt-5.5",
            provider_model="gpt-5.5",
            connection_id="conn_test",
        )
        controller.store.save(session)

        async def fake_run_turn_request(**kwargs):
            await asyncio.sleep(60)

        monkeypatch.setattr(controller, "run_turn_request", fake_run_turn_request)

        turn_id = await controller.submit_message(
            "ms_test",
            content="show this immediately",
            attachments=[],
            expected_runtime=RUNTIME_FAMILY_CODEX,
        )
        payload = controller.get_events("ms_test", after_seq=0, limit=10)
        events = payload["events"]
        request = controller.store.load_turn_request("ms_test", turn_id)

        assert [event["event_name"] for event in events[:2]] == ["user_text", "turn_started"]
        assert events[0]["text"] == "show this immediately"
        assert events[0]["payload"]["attachments"] == []
        assert request["events_prelogged"] is True
        assert payload["session"]["latest_event_seq"] >= 2

        task = controller._turn_tasks.pop("ms_test", None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(run_check())


def test_controller_send_pending_input_now_prelogs_selected_message(tmp_path, monkeypatch):
    async def run_check():
        class FakeRuntime:
            def __init__(self):
                self.calls = []

            def is_alive(self):
                return True

            async def interrupt_turn(self, *, force=False):
                self.calls.append(force)
                return {"mode": "force" if force else "soft", "signal": "ctrl_c"}

        controller = MainSessionController(
            config={"projects": {}, "main_session_send_now_interrupt_delay": 0},
            project_root=tmp_path,
            repair_running_sessions=False,
        )
        session = MainSessionRecord(
            session_id="ms_test",
            name="test",
            cwd=str(tmp_path),
            status="running",
            runtime_family=RUNTIME_FAMILY_CODEX,
            runtime_kind=RUNTIME_FAMILY_CODEX,
            display_model="gpt-5.5",
            provider_model="gpt-5.5",
            connection_id="conn_test",
            current_turn_id="turn_active",
            current_turn_status="running",
            metadata={
                "pending_inputs": [
                    {"queue_id": "q1", "kind": "message", "content": "send this now", "attachments": []},
                    {"queue_id": "q2", "kind": "message", "content": "keep queued", "attachments": []},
                ],
            },
        )
        controller.store.save(session)
        runtime = FakeRuntime()
        controller._persistent_runtimes["ms_test"] = runtime
        active_task = asyncio.create_task(asyncio.sleep(60))
        controller._turn_tasks["ms_test"] = active_task

        async def fake_run_turn_request(**kwargs):
            await asyncio.sleep(60)

        monkeypatch.setattr(controller, "run_turn_request", fake_run_turn_request)

        result = await controller.send_pending_input_now("ms_test", "q1", expected_runtime=RUNTIME_FAMILY_CODEX)
        payload = controller.get_events("ms_test", after_seq=0, limit=20)
        user_events = [event for event in payload["events"] if event["event_name"] == "user_text"]
        saved = controller.store.load("ms_test")

        assert result["status"] == "pending_input_sent_now"
        assert runtime.calls == [False]
        assert active_task.cancelled()
        assert user_events[-1]["text"] == "send this now"
        assert result["session"]["latest_event_seq"] >= user_events[-1]["seq"]
        assert [item["queue_id"] for item in saved.metadata["pending_inputs"]] == ["q2"]

        task = controller._turn_tasks.pop("ms_test", None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(run_check())


def test_runtime_thread_started_stream_persists_native_session_id():
    class FakeStore:
        def __init__(self):
            self.saved = []

        def save(self, session):
            self.saved.append(session.to_dict())

    controller = object.__new__(MainSessionController)
    controller.store = FakeStore()
    session = MainSessionRecord(
        session_id="ms_test",
        name="test",
        cwd="/tmp",
        status="running",
        runtime_family=RUNTIME_FAMILY_CODEX,
        runtime_kind=RUNTIME_FAMILY_CODEX,
        display_model="gpt-5.5",
        provider_model="gpt-5.5",
        connection_id="conn_test",
        current_turn_status="running",
    )

    updated = controller._apply_runtime_stream_state(
        session,
        {"type": "thread.started", "thread_id": "thread-test"},
    )

    assert updated.native_session_id == "thread-test"
    assert updated.resume_token == "thread-test"
    assert controller.store.saved[-1]["native_session_id"] == "thread-test"


def test_codex_tui_mcp_approval_prompt_is_parsed():
    prompt = (
        '\x1b]0;[ ! ] Action Required | vizo-next\x07'
        'Allow the vizo-router MCP server to run tool "interaction_design_session"?\n'
        'action: status\nproject_name: vizo\n'
        '› 1. Allow\n'
        '2. Allow for this session\n'
        '3. Always allow\n'
        '4. Cancel\n'
    )

    parsed = _extract_codex_mcp_approval_prompt(prompt)

    assert parsed is not None
    assert parsed["server"] == "vizo-router"
    assert parsed["tool"] == "interaction_design_session"
    assert parsed["request_id"].startswith("mcp_")
    assert parsed["signature"]


def test_codex_tui_mcp_approval_prompt_dedupes_only_while_active(tmp_path):
    async def run_check():
        prompt = (
            'Allow the vizo-router MCP server to run tool "interaction_design_session"?\n'
            'action: status\nproject_name: vizo\n'
            '1. Allow\n'
            '2. Allow for this session\n'
            '3. Always allow\n'
            '4. Cancel\n'
        )
        runtime = CodexPersistentSessionRuntime(
            session_id="ms_test",
            project_root=tmp_path,
            config={"main_session_max_timeout": 1},
        )
        runtime.native_session_id = "thread-test"
        emitted = []
        runtime._stream_event_cb = emitted.append

        runtime._recent_output = prompt
        await runtime._handle_runtime_output_interactions("")
        await runtime._handle_runtime_output_interactions("")

        assert len(emitted) == 1
        first_request_id = emitted[0]["payload"]["request_id"]

        async with runtime._interaction_state_lock:
            runtime._interaction_queue.clear()
        await runtime._handle_runtime_output_interactions("")

        assert len(emitted) == 1

        runtime._recent_output = "continued"
        await runtime._handle_runtime_output_interactions("")
        runtime._recent_output = prompt
        await runtime._handle_runtime_output_interactions("")

        assert len(emitted) == 2
        assert emitted[1]["payload"]["request_id"] != first_request_id

    asyncio.run(run_check())


def test_codex_startup_waits_for_final_prompt_after_mcp_startup(tmp_path):
    runtime = CodexPersistentSessionRuntime(
        session_id="ms_test",
        project_root=tmp_path,
        config={},
    )
    runtime._recent_output = (
        "› Use /skills to list available skills\n"
        "OpenAI Codex (v0.128.0)\n"
        "model: gpt-5.5 high   /model to change\n"
        "directory: /opt/vizo-next\n"
        "Starting MCP servers (4/5): codex_apps"
    )

    assert runtime._codex_mcp_startup_pending()
    assert not runtime._startup_ready_marker_seen()
    assert not runtime._startup_semantic_output_seen()

    runtime._recent_output += "\n› Use /skills to list available skills\n  gpt-5.5 high · /opt/vizo-next\n"

    assert not runtime._codex_mcp_startup_pending()
    assert runtime._startup_ready_marker_seen()


def test_codex_persistent_main_session_uses_configured_sandbox(tmp_path):
    session = SimpleNamespace(
        cwd=str(tmp_path),
        display_model="gpt-5.5",
        provider_model="gpt-5.5",
        resume_token="",
        session_id="ms_test",
        metadata={
            "runtime_dir": str(tmp_path / ".vizo" / "sessions" / "main" / "ms_test" / "runtime"),
            "project_root": str(tmp_path),
        },
    )

    runtime = CodexPersistentSessionRuntime(
        session_id="ms_test",
        project_root=tmp_path,
        config={"codex_main_session_sandbox": "danger-full-access"},
    )
    cmd = runtime._build_command(session=session, connection={"auth_mode": "account_login"})

    assert cmd[cmd.index("--sandbox") + 1] == "danger-full-access"

    default_runtime = CodexPersistentSessionRuntime(
        session_id="ms_test_default",
        project_root=tmp_path,
        config={},
    )
    default_cmd = default_runtime._build_command(session=session, connection={"auth_mode": "account_login"})

    assert default_cmd[default_cmd.index("--sandbox") + 1] == "workspace-write"


def test_codex_exec_main_session_uses_configured_sandbox(tmp_path):
    async def run_check():
        class Runner:
            def __init__(self) -> None:
                self.config = {
                    "codex_main_session_sandbox": "danger-full-access",
                    "confirm_server": {"port": 9390},
                }
                self.project_path = tmp_path
                self.cmd = []

            async def _run_subprocess_streaming(self, cmd, prompt, cwd, **kwargs):
                del prompt, cwd, kwargs
                self.cmd = cmd
                last_message_path = Path(cmd[cmd.index("--output-last-message") + 1])
                last_message_path.parent.mkdir(parents=True, exist_ok=True)
                last_message_path.write_text("done", encoding="utf-8")
                return "", "", 0, []

        runner = Runner()
        adapter = CodexMainSessionAdapter(runner)
        session = SimpleNamespace(
            cwd=str(tmp_path),
            resume_token="",
            session_id="ms_test",
            metadata={
                "runtime_dir": str(tmp_path / ".vizo" / "sessions" / "main" / "ms_test" / "runtime"),
                "last_message_path": str(
                    tmp_path / ".vizo" / "sessions" / "main" / "ms_test" / "runtime" / "last-message.txt"
                ),
                "project_root": str(tmp_path),
            },
        )
        decision = SimpleNamespace(provider_model="gpt-5.5", display_model="gpt-5.5", selected_model="gpt-5.5")

        result = await adapter.run(
            decision=decision,
            session=session,
            turn_id="turn_test",
            prompt="hello",
            connection={
                "base_url": "https://example.invalid/v1",
                "api_key": "test-key",
                "auth_mode": "api_key",
                "connection_id": "conn_test",
            },
        )

        assert result["result"] == "done"
        assert runner.cmd[runner.cmd.index("--sandbox") + 1] == "danger-full-access"

    asyncio.run(run_check())


def test_codex_transcript_polling_uses_byte_offsets_after_non_ascii_prefix(tmp_path):
    async def run_check():
        transcript = tmp_path / "rollout-2026-05-02T03-02-56-thread-1.jsonl"
        non_ascii_prefix = ("\u4e2d\u6587" * 700) + "\n"
        transcript.write_text(non_ascii_prefix, encoding="utf-8")
        byte_offset = transcript.stat().st_size

        events = [
            {
                "timestamp": "2026-05-01T19:03:18.713Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "processing"}],
                },
            },
            {
                "timestamp": "2026-05-01T19:03:27.992Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "last_agent_message": "final diagram",
                },
            },
        ]
        with transcript.open("a", encoding="utf-8") as handle:
            for item in events:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")

        runtime = CodexPersistentSessionRuntime(
            session_id="ms_test",
            project_root=Path("/opt/vizo-next"),
            config={"main_session_max_timeout": 1},
        )
        runtime.pid = os.getpid()
        artifact = RuntimeArtifact(native_session_id="thread-1", transcript_path=transcript)
        session = SimpleNamespace(display_model="gpt-5.5", provider_model="gpt-5.5")
        decision = SimpleNamespace(display_model="gpt-5.5", provider_model="gpt-5.5", selected_model="gpt-5.5")
        streamed = []

        result = await runtime._wait_for_turn_result(
            session=session,
            decision=decision,
            artifact=artifact,
            offset=byte_offset,
            on_stream_event=streamed.append,
        )

        assert result["result"] == "final diagram"
        assert any(item.get("type") == "turn.completed" for item in streamed)

    asyncio.run(run_check())


def test_codex_empty_task_complete_retries_for_visible_message(tmp_path):
    async def run_check():
        transcript = tmp_path / "rollout-2026-05-28T02-21-37-thread-1.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "turn_empty_message",
                        "last_agent_message": None,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        class RetryRuntime(CodexPersistentSessionRuntime):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.sent_prompts = []

            async def _send_prompt_async(self, prompt: str) -> None:
                self.sent_prompts.append(prompt)
                with transcript.open("a", encoding="utf-8") as handle:
                    for item in (
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "agent_message",
                                "message": "YaDou Windows 镜像记忆在 apps/yadou/build-and-devtools.md",
                            },
                        },
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "task_complete",
                                "turn_id": "turn_retry",
                                "last_agent_message": "YaDou Windows 镜像记忆在 apps/yadou/build-and-devtools.md",
                            },
                        },
                    ):
                        handle.write(json.dumps(item, ensure_ascii=False) + "\n")

        runtime = RetryRuntime(
            session_id="ms_test",
            project_root=Path("/opt/vizo-next"),
            config={"main_session_max_timeout": 1},
        )
        runtime.pid = os.getpid()
        runtime._codex_log_path = tmp_path / "codex-tui.log"
        artifact = RuntimeArtifact(native_session_id="thread-1", transcript_path=transcript)
        session = SimpleNamespace(display_model="gpt-5.5", provider_model="gpt-5.5")
        decision = SimpleNamespace(display_model="gpt-5.5", provider_model="gpt-5.5", selected_model="gpt-5.5")
        streamed = []

        result = await runtime._wait_for_turn_result(
            session=session,
            decision=decision,
            artifact=artifact,
            offset=0,
            on_stream_event=streamed.append,
        )

        assert result["result"] == "YaDou Windows 镜像记忆在 apps/yadou/build-and-devtools.md"
        assert len(runtime.sent_prompts) == 1
        assert "没有返回可展示的 assistant message" in runtime.sent_prompts[0]
        assert any(item.get("type") == "turn.completed" for item in streamed)

    asyncio.run(run_check())


def test_claude_transcript_polling_uses_byte_offsets_and_waits_for_text_after_thinking(tmp_path):
    async def run_check():
        transcript = tmp_path / "thread-1.jsonl"
        non_ascii_prefix = ("中文" * 700) + "\n"
        transcript.write_text(non_ascii_prefix, encoding="utf-8")
        byte_offset = transcript.stat().st_size

        thinking_message = {
            "id": "msg_thinking",
            "type": "message",
            "role": "assistant",
            "model": "deepseek-v4-pro",
            "content": [{"type": "thinking", "thinking": "Let me summarize the changes."}],
            "stop_reason": "end_turn",
            "usage": {"output_tokens": 12},
        }
        text_message = {
            "id": "msg_thinking",
            "type": "message",
            "role": "assistant",
            "model": "deepseek-v4-pro",
            "content": [{"type": "text", "text": "最终中文回答"}],
            "stop_reason": "end_turn",
            "usage": {"output_tokens": 8},
        }
        with transcript.open("a", encoding="utf-8") as handle:
            for message in (thinking_message, text_message):
                handle.write(
                    json.dumps(
                        {
                            "sessionId": "thread-1",
                            "type": "assistant",
                            "message": message,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

        runtime = ClaudePersistentSessionRuntime(
            session_id="ms_test",
            project_root=tmp_path,
            config={
                "main_session_max_timeout": 1,
                "main_session_claude_thinking_end_turn_grace_seconds": 15,
            },
        )
        runtime.pid = os.getpid()
        artifact = RuntimeArtifact(native_session_id="thread-1", transcript_path=transcript)
        session = SimpleNamespace(display_model="opus", provider_model="deepseek-v4-pro")
        decision = SimpleNamespace(display_model="opus", provider_model="deepseek-v4-pro", selected_model="opus")
        streamed = []

        result = await runtime._wait_for_turn_result(
            session=session,
            decision=decision,
            artifact=artifact,
            offset=byte_offset,
            on_stream_event=streamed.append,
        )

        assert result["result"] == "最终中文回答"
        assert result["usage"] == {"output_tokens": 8}
        assistant_events = [item for item in streamed if item.get("type") == "assistant"]
        assert [item["message"]["id"] for item in assistant_events] == ["msg_thinking", "msg_thinking"]
        assert streamed[-1]["type"] == "result"

    asyncio.run(run_check())


def test_claude_thinking_only_end_turn_falls_back_on_turn_duration(tmp_path):
    async def run_check():
        transcript = tmp_path / "thread-1.jsonl"
        thinking_message = {
            "id": "msg_thinking",
            "type": "message",
            "role": "assistant",
            "model": "deepseek-v4-pro",
            "content": [{"type": "thinking", "thinking": "No visible answer."}],
            "stop_reason": "end_turn",
            "usage": {"output_tokens": 3},
        }
        transcript.write_text(
            "\n".join(
                [
                    json.dumps(
                        {"sessionId": "thread-1", "type": "assistant", "message": thinking_message},
                        ensure_ascii=False,
                    ),
                    json.dumps({"sessionId": "thread-1", "type": "system", "subtype": "turn_duration"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        runtime = ClaudePersistentSessionRuntime(
            session_id="ms_test",
            project_root=tmp_path,
            config={
                "main_session_max_timeout": 1,
                "main_session_claude_thinking_end_turn_grace_seconds": 15,
            },
        )
        runtime.pid = os.getpid()
        artifact = RuntimeArtifact(native_session_id="thread-1", transcript_path=transcript)
        session = SimpleNamespace(display_model="opus", provider_model="deepseek-v4-pro")
        decision = SimpleNamespace(display_model="opus", provider_model="deepseek-v4-pro", selected_model="opus")
        streamed = []

        result = await runtime._wait_for_turn_result(
            session=session,
            decision=decision,
            artifact=artifact,
            offset=0,
            on_stream_event=streamed.append,
        )

        assert result["result"] == ""
        assert result["usage"] == {"output_tokens": 3}
        assert streamed[-1]["type"] == "result"

    asyncio.run(run_check())


def test_claude_deepseek_thinking_only_end_turn_retries_for_visible_text(tmp_path):
    async def run_check():
        transcript = tmp_path / "thread-1.jsonl"
        thinking_message = {
            "id": "msg_thinking",
            "type": "message",
            "role": "assistant",
            "model": "deepseek-v4-pro",
            "content": [{"type": "thinking", "thinking": "The user is greeting me."}],
            "stop_reason": "end_turn",
            "usage": {"output_tokens": 3},
        }
        transcript.write_text(
            "\n".join(
                [
                    json.dumps(
                        {"sessionId": "thread-1", "type": "assistant", "message": thinking_message},
                        ensure_ascii=False,
                    ),
                    json.dumps({"sessionId": "thread-1", "type": "system", "subtype": "turn_duration"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        retry_prompts = []

        class RetryingClaudeRuntime(ClaudePersistentSessionRuntime):
            async def _send_prompt_async(self, prompt: str) -> None:
                retry_prompts.append(prompt)
                text_message = {
                    "id": "msg_text",
                    "type": "message",
                    "role": "assistant",
                    "model": "deepseek-v4-pro",
                    "content": [{"type": "text", "text": "你好，我在。"}],
                    "stop_reason": "end_turn",
                    "usage": {"output_tokens": 5},
                }
                with transcript.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {"sessionId": "thread-1", "type": "assistant", "message": text_message},
                            ensure_ascii=False,
                        )
                        + "\n"
                    )

        runtime = RetryingClaudeRuntime(
            session_id="ms_test",
            project_root=tmp_path,
            config={
                "main_session_max_timeout": 1,
                "main_session_claude_thinking_end_turn_grace_seconds": 15,
            },
        )
        runtime.pid = os.getpid()
        artifact = RuntimeArtifact(native_session_id="thread-1", transcript_path=transcript)
        session = SimpleNamespace(display_model="opus", provider_model="deepseek-v4-pro")
        decision = SimpleNamespace(display_model="opus", provider_model="deepseek-v4-pro", selected_model="opus")
        streamed = []

        result = await runtime._wait_for_turn_result(
            session=session,
            decision=decision,
            artifact=artifact,
            offset=0,
            on_stream_event=streamed.append,
        )

        assert retry_prompts
        assert "不要输出思考过程" in retry_prompts[0]
        assert result["result"] == "你好，我在。"
        assert result["usage"] == {"output_tokens": 5}

    asyncio.run(run_check())


def test_controller_repairs_running_claude_session_from_missed_text_end_turn(tmp_path):
    transcript = tmp_path / "thread-1.jsonl"
    thinking_message = {
        "id": "msg_thinking",
        "type": "message",
        "role": "assistant",
        "model": "deepseek-v4-pro",
        "content": [{"type": "thinking", "thinking": "Preparing final answer."}],
        "stop_reason": "end_turn",
        "usage": {"output_tokens": 9},
    }
    text_message = {
        "id": "msg_thinking",
        "type": "message",
        "role": "assistant",
        "model": "deepseek-v4-pro",
        "content": [{"type": "text", "text": "做完了"}],
        "stop_reason": "end_turn",
        "usage": {"output_tokens": 4},
    }
    later_message = {
        "id": "msg_later",
        "type": "message",
        "role": "assistant",
        "model": "deepseek-v4-pro",
        "content": [{"type": "text", "text": "不应补入"}],
        "stop_reason": "end_turn",
        "usage": {"output_tokens": 2},
    }
    transcript.write_text(
        "\n".join(
            json.dumps(
                {
                    "sessionId": "thread-1",
                    "type": "assistant",
                    "message": message,
                },
                ensure_ascii=False,
            )
            for message in (thinking_message, text_message, later_message)
        )
        + "\n",
        encoding="utf-8",
    )

    controller = MainSessionController(
        config={"main_session_persistent_runtime": True},
        project_root=tmp_path,
        repair_running_sessions=False,
    )
    session = MainSessionRecord(
        session_id="ms_test",
        name="test",
        cwd=str(tmp_path),
        status="running",
        runtime_family=RUNTIME_FAMILY_CLAUDE_CODE,
        runtime_kind=RUNTIME_FAMILY_CLAUDE_CODE,
        display_model="opus",
        provider_model="deepseek-v4-pro",
        connection_id="conn_test",
        current_turn_id="turn_test",
        current_turn_status="running",
        current_turn_started_at="2020-01-01T00:00:00+00:00",
        native_session_id="thread-1",
        resume_token="thread-1",
        latest_event_seq=3,
        metadata={
            "persistent_runtime_transcript_path": str(transcript),
            "current_turn_worker_pid": os.getpid(),
            "connection_snapshot": {
                "connection_id": "conn_test",
                "base_url": "https://api.deepseek.com/anthropic",
                "api_key": "test-key",
                "auth_mode": "api_key",
            },
        },
    )
    controller.store.save(session)
    controller.store.event_store("ms_test").append_raw({"type": "assistant", "message": thinking_message})

    controller._repair_orphaned_running_sessions()

    saved = controller.store.load("ms_test")
    assert saved is not None
    assert saved.status == "idle"
    assert saved.current_turn_status == "completed"
    assert saved.metadata["last_result"] == "做完了"
    assert saved.metadata["last_usage"] == {"output_tokens": 4}
    events = controller.store.read_events("ms_test", after_seq=0, limit=20)
    assert any(item.get("event_name") == "assistant_text" and item.get("payload", {}).get("text") == "做完了" for item in events)
    assert any(
        item.get("event_name") == "assistant_text"
        and item.get("payload", {}).get("text") == "做完了"
        and item.get("session_id") == "ms_test"
        and item.get("step_name") == "turn_test"
        for item in events
    )
    assert not any(
        item.get("event_name") == "assistant_text" and item.get("payload", {}).get("text") == "不应补入"
        for item in events
    )
    assert any(item.get("event_name") == "completed" and item.get("payload", {}).get("recovered") is True for item in events)


def test_controller_appends_missed_claude_partial_events_before_interrupt(tmp_path):
    transcript = tmp_path / "thread-1.jsonl"
    text_message = {
        "id": "msg_partial",
        "type": "message",
        "role": "assistant",
        "model": "deepseek-v4-pro",
        "content": [{"type": "text", "text": "先只提交我改的文件。"}],
        "stop_reason": "tool_use",
        "usage": {"output_tokens": 12},
    }
    transcript.write_text(
        json.dumps(
            {
                "timestamp": "2026-05-27T17:43:34.555Z",
                "sessionId": "thread-1",
                "type": "assistant",
                "message": text_message,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    controller = MainSessionController(
        config={"main_session_persistent_runtime": True},
        project_root=tmp_path,
        repair_running_sessions=False,
    )
    session = MainSessionRecord(
        session_id="ms_test",
        name="test",
        cwd=str(tmp_path),
        status="running",
        runtime_family=RUNTIME_FAMILY_CLAUDE_CODE,
        runtime_kind=RUNTIME_FAMILY_CLAUDE_CODE,
        display_model="opus",
        provider_model="deepseek-v4-pro",
        connection_id="conn_test",
        current_turn_id="turn_test",
        current_turn_status="running",
        current_turn_started_at="2026-05-27T17:43:05+00:00",
        native_session_id="thread-1",
        resume_token="thread-1",
        latest_event_seq=2,
        metadata={
            "persistent_runtime_transcript_path": str(transcript),
            "current_turn_worker_pid": os.getpid(),
            "connection_snapshot": {
                "connection_id": "conn_test",
                "base_url": "https://api.deepseek.com/anthropic",
                "api_key": "test-key",
                "auth_mode": "api_key",
            },
        },
    )
    controller.store.save(session)

    controller._repair_orphaned_running_sessions()

    saved = controller.store.load("ms_test")
    assert saved is not None
    assert saved.status == "interrupted"
    events = controller.store.read_events("ms_test", after_seq=0, limit=20)
    assert any(
        item.get("event_name") == "assistant_text"
        and item.get("payload", {}).get("text") == "先只提交我改的文件。"
        for item in events
    )


def test_codex_pending_interaction_pauses_main_turn_timeout(tmp_path):
    async def run_check():
        transcript = tmp_path / "rollout-2026-05-02T03-02-56-thread-1.jsonl"
        transcript.write_text("", encoding="utf-8")

        runtime = CodexPersistentSessionRuntime(
            session_id="ms_test",
            project_root=Path("/opt/vizo-next"),
            config={
                "main_session_max_timeout": 1,
                "main_session_interaction_timeout": 3,
            },
        )
        runtime.pid = os.getpid()
        artifact = RuntimeArtifact(native_session_id="thread-1", transcript_path=transcript)
        session = SimpleNamespace(display_model="gpt-5.5", provider_model="gpt-5.5")
        decision = SimpleNamespace(display_model="gpt-5.5", provider_model="gpt-5.5", selected_model="gpt-5.5")

        async def simulate_prompt_and_resume():
            await asyncio.sleep(0.1)
            await runtime._enqueue_interaction_request(
                request={
                    "request_id": "mcp_wait_1",
                    "interaction_kind": "mcp_tool_approval",
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
                            "type": "event_msg",
                            "payload": {
                                "type": "task_complete",
                                "last_agent_message": "continued after approval",
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


def test_codex_commentary_messages_are_runtime_status_events():
    commentary_text = "\u6211\u6b63\u5728\u6574\u7406\u7ed3\u6784\u56fe\u3002"
    decision = RouteDecision(
        scope="main_session",
        runtime_family=RUNTIME_FAMILY_CODEX,
        adapter_key="codex",
        requested_model="gpt-5.5",
        selected_model="gpt-5.5",
        can_resume=True,
        supports_pause=False,
        supports_pause_feedback=False,
        supports_native_resume=True,
        reason="test",
    )
    context = RuntimeExecutionContext(scope="main_session", role="assistant", step_name="turn_test")

    events = build_unified_events(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "commentary",
                "content": [{"type": "output_text", "text": commentary_text}],
            },
        },
        sequence_start=1,
        decision=decision,
        context=context,
    )

    assert [item.event_name for item in events] == ["runtime_status"]
    assert events[0].event_type == "lifecycle"
    assert events[0].payload["text"] == commentary_text
    assert events[0].payload["status_kind"] == "commentary"


def test_codex_exec_command_tool_call_becomes_work_progress():
    decision = RouteDecision(
        scope="main_session",
        runtime_family=RUNTIME_FAMILY_CODEX,
        adapter_key="codex",
        requested_model="gpt-5.5",
        selected_model="gpt-5.5",
        can_resume=True,
        supports_pause=False,
        supports_pause_feedback=False,
        supports_native_resume=True,
        reason="test",
    )
    context = RuntimeExecutionContext(scope="main_session", role="assistant", step_name="turn_test")

    events = build_unified_events(
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "call_test",
                "arguments": json.dumps({"cmd": "pytest -q tests/test_claude_persistent_runtime.py"}),
            },
        },
        sequence_start=1,
        decision=decision,
        context=context,
    )

    assert [item.event_name for item in events] == ["work_progress"]
    payload = events[0].payload
    assert payload["kind"] == "test"
    assert payload["status"] == "started"
    assert payload["title"] == "运行测试"
    assert payload["command"] == "pytest -q tests/test_claude_persistent_runtime.py"


def test_codex_command_completion_becomes_failed_work_progress():
    decision = RouteDecision(
        scope="main_session",
        runtime_family=RUNTIME_FAMILY_CODEX,
        adapter_key="codex",
        requested_model="gpt-5.5",
        selected_model="gpt-5.5",
        can_resume=True,
        supports_pause=False,
        supports_pause_feedback=False,
        supports_native_resume=True,
        reason="test",
    )
    context = RuntimeExecutionContext(scope="main_session", role="assistant", step_name="turn_test")

    events = build_unified_events(
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "pytest -q tests/test_vizo.py",
                "status": "failed",
                "exit_code": 1,
                "aggregated_output": "FAILED tests/test_vizo.py::test_case",
            },
        },
        sequence_start=1,
        decision=decision,
        context=context,
    )

    assert [item.event_name for item in events] == ["work_progress"]
    payload = events[0].payload
    assert payload["kind"] == "test"
    assert payload["status"] == "failed"
    assert payload["title"] == "运行测试失败"
    assert payload["exit_code"] == 1
    assert "FAILED tests/test_vizo.py::test_case" in payload["output"]


def test_claude_tool_use_becomes_work_progress():
    decision = RouteDecision(
        scope="main_session",
        runtime_family=RUNTIME_FAMILY_CLAUDE_CODE,
        adapter_key="claude_code",
        requested_model="sonnet",
        selected_model="sonnet",
        can_resume=True,
        supports_pause=False,
        supports_pause_feedback=False,
        supports_native_resume=True,
        reason="test",
    )
    context = RuntimeExecutionContext(scope="main_session", role="assistant", step_name="turn_test")

    events = build_unified_events(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "lib/runtime/events.py"},
                    }
                ]
            },
        },
        sequence_start=1,
        decision=decision,
        context=context,
    )

    assert [item.event_name for item in events] == ["work_progress"]
    payload = events[0].payload
    assert payload["kind"] == "read"
    assert payload["status"] == "started"
    assert payload["title"] == "读取文件"
    assert payload["detail"] == "lib/runtime/events.py"


def test_codex_image_generation_events_are_saved_as_session_artifacts(tmp_path):
    image_base64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    raw_event = {
        "type": "response_item",
        "payload": {
            "type": "image_generation_call",
            "id": "ig_test",
            "status": "generating",
            "revised_prompt": "A single tree on a white background.",
            "result": image_base64,
        },
    }

    artifact = capture_image_generation_artifact(
        session_dir=tmp_path,
        session_id="ms_img",
        turn_id="turn_img",
        raw_event=raw_event,
        requested_prompt="generate a tree",
    )

    assert artifact is not None
    assert artifact["status"] == "completed"
    assert artifact["mime_type"] == "image/png"
    assert artifact["url"].startswith("/vizo/console/api/sessions/ms_img/images/")
    assert (tmp_path / "artifacts" / "images" / f"{artifact['id']}.png").exists()

    listed = list_image_artifacts(session_dir=tmp_path, session_id="ms_img", backfill_from_raw_events=False)
    assert [item["id"] for item in listed] == [artifact["id"]]

    scrubbed = scrub_image_generation_event(raw_event)
    assert scrubbed["payload"]["result"] == ""
    assert scrubbed["payload"]["result_omitted"] is True


def test_codex_image_generation_events_do_not_become_passthrough_events():
    decision = RouteDecision(
        scope="main_session",
        runtime_family=RUNTIME_FAMILY_CODEX,
        adapter_key="codex",
        requested_model="gpt-5.5",
        selected_model="gpt-5.5",
        can_resume=True,
        supports_pause=False,
        supports_pause_feedback=False,
        supports_native_resume=True,
        reason="test",
    )
    context = RuntimeExecutionContext(scope="main_session", role="assistant", step_name="turn_test")

    events = build_unified_events(
        {
            "type": "response_item",
            "payload": {
                "type": "image_generation_call",
                "id": "ig_test",
                "status": "generating",
            },
        },
        sequence_start=1,
        decision=decision,
        context=context,
    )

    assert events == []


def test_main_session_uploaded_attachment_prompt_references_file_without_inlining(tmp_path):
    attachment_path = tmp_path / "attachments" / "prd.md"
    attachment_path.parent.mkdir()
    attachment_path.write_text("full attachment body", encoding="utf-8")

    prompt_items = _materialize_main_session_attachments(
        tmp_path,
        "turn_uploaded",
        [
            {
                "id": "att_test",
                "name": "prd.md",
                "size": attachment_path.stat().st_size,
                "type": "text/markdown",
                "path": str(attachment_path),
            }
        ],
    )

    assert len(prompt_items) == 1
    assert "prd.md" in prompt_items[0]
    assert str(attachment_path) in prompt_items[0]
    assert "full attachment body" not in prompt_items[0]
