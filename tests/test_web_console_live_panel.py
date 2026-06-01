import types
import sys
import json
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.web_console import WEB_CONSOLE_HTML, WebConsoleHandler


def test_attach_output_artifacts_adds_doc_specific_output_url(tmp_path):
    task_dir = tmp_path / "task-1"
    task_dir.mkdir()
    (task_dir / "00-requirement-analysis.md").write_text("# RA\n", encoding="utf-8")
    (task_dir / "01-prd.md").write_text("# PRD\n", encoding="utf-8")
    (task_dir / "02-design.md").write_text("# Design\n", encoding="utf-8")

    task_data = {
        "task_id": "task-1",
        "steps": [
            {"name": "requirement_analysis", "output_doc": "00-requirement-analysis.md", "preview_url": ""},
            {"name": "pm_prd", "output_doc": "01-prd.md", "preview_url": ""},
            {"name": "architect", "output_doc": "02-design.md", "preview_url": "https://example.test/vizo/preview/old"},
        ],
    }

    handler = WebConsoleHandler.__new__(WebConsoleHandler)
    result = handler._attach_output_artifacts(task_data, task_dir)

    assert result["steps"][0]["preview_url"] == "/vizo/api/tasks/task-1/outputs/00-requirement-analysis.md"
    assert result["steps"][1]["preview_url"] == "/vizo/api/tasks/task-1/outputs/01-prd.md"
    assert result["steps"][1]["output_url"] == "/vizo/api/tasks/task-1/outputs/01-prd.md"
    assert result["steps"][2]["preview_url"] == "/vizo/api/tasks/task-1/outputs/02-design.md"
    assert result["steps"][2]["output_url"] == "/vizo/api/tasks/task-1/outputs/02-design.md"
    assert result["steps"][2]["external_preview_url"] == "https://example.test/vizo/preview/old"
    assert result["steps"][2]["output_available"] is True


def test_normalize_task_live_state_prefers_latest_running_step_row():
    task_data = {
        "status": "running",
        "current_step": "test_round_3",
        "current_role": "qa_engineer",
        "steps": [
            {"name": "test_round_3", "role": "qa_engineer", "status": "completed"},
            {"name": "fix_round_3", "role": "frontend_developer", "status": "running"},
        ],
    }

    result = WebConsoleHandler._normalize_task_live_state(task_data)

    assert result["current_step"] == "fix_round_3"
    assert result["current_role"] == "frontend_developer"


@pytest.mark.asyncio
async def test_attach_pending_confirm_drops_stale_fallback_when_not_waiting():
    handler = WebConsoleHandler.__new__(WebConsoleHandler)

    async def no_web_pending(self, task_id="", request_id=""):
        return "", None

    handler._find_web_pending_confirm = types.MethodType(no_web_pending, handler)
    task_data = {
        "status": "running",
        "pending_confirm": {"request_id": "old", "summary": "PRD 已生成"},
    }

    result = await handler._attach_pending_confirm(task_data, "")

    assert "pending_confirm" not in result


@pytest.mark.asyncio
async def test_attach_pending_confirm_keeps_fallback_while_waiting():
    handler = WebConsoleHandler.__new__(WebConsoleHandler)

    async def no_web_pending(self, task_id="", request_id=""):
        return "", None

    handler._find_web_pending_confirm = types.MethodType(no_web_pending, handler)
    task_data = {
        "status": "waiting_confirm",
        "pending_confirm": {"request_id": "active", "summary": "PRD 已生成"},
    }

    result = await handler._attach_pending_confirm(task_data, "")

    assert result["pending_confirm"]["request_id"] == "active"


@pytest.mark.asyncio
async def test_attach_pending_confirm_drops_resolved_fallback_while_waiting():
    handler = WebConsoleHandler.__new__(WebConsoleHandler)

    async def no_web_pending(self, task_id="", request_id=""):
        return "", None

    async def confirm_has_response(self, request_id=""):
        return request_id == "done"

    handler._find_web_pending_confirm = types.MethodType(no_web_pending, handler)
    handler._confirm_request_has_response = types.MethodType(confirm_has_response, handler)
    task_data = {
        "status": "waiting_confirm",
        "pending_confirm": {"request_id": "done", "summary": "PRD 已生成"},
    }

    result = await handler._attach_pending_confirm(task_data, "task-1")

    assert "pending_confirm" not in result


@pytest.mark.asyncio
async def test_attach_pending_confirm_drops_fallback_when_matching_web_request_resolved():
    handler = WebConsoleHandler.__new__(WebConsoleHandler)

    async def web_pending(self, task_id="", request_id=""):
        return "web-done", {"id": "web-done", "context": {"task_id": task_id}, "summary": "请确认"}

    async def confirm_has_response(self, request_id=""):
        return request_id == "web-done"

    handler._find_web_pending_confirm = types.MethodType(web_pending, handler)
    handler._confirm_request_has_response = types.MethodType(confirm_has_response, handler)
    task_data = {
        "status": "waiting_confirm",
        "pending_confirm": {"request_id": "bridge-id", "summary": "旧确认"},
    }

    result = await handler._attach_pending_confirm(task_data, "task-1")

    assert "pending_confirm" not in result


@pytest.mark.asyncio
async def test_attach_pending_confirm_ignores_stale_web_request_for_terminal_task():
    handler = WebConsoleHandler.__new__(WebConsoleHandler)

    async def web_pending(self, task_id="", request_id=""):
        return "stale-web", {"id": "stale-web", "context": {"task_id": task_id}, "summary": "旧确认"}

    handler._find_web_pending_confirm = types.MethodType(web_pending, handler)
    task_data = {
        "status": "completed",
        "pending_confirm": {"request_id": "old", "summary": "PRD 已生成"},
    }

    result = await handler._attach_pending_confirm(task_data, "task-1")

    assert "pending_confirm" not in result


def test_confirm_success_clears_local_pending_confirm_state():
    assert "function markConfirmResolvedLocally()" in WEB_CONSOLE_HTML
    assert "delete S.opusTask.pending_confirm;" in WEB_CONSOLE_HTML
    assert "S.opusTask.status = 'running';" in WEB_CONSOLE_HTML
    assert "setControls(S.opusTask.status || 'running', null);" in WEB_CONSOLE_HTML
    assert "pollOpusTask();" in WEB_CONSOLE_HTML
    assert WEB_CONSOLE_HTML.count("markConfirmResolvedLocally();") >= 3


def test_live_panel_layout_keeps_steps_scrollable_without_squashing_cards():
    assert ".right-panel" in WEB_CONSOLE_HTML
    assert "overflow: hidden;" in WEB_CONSOLE_HTML
    assert ".split-body { display: flex; flex: 1 1 auto; min-height: 220px; overflow: hidden; }" in WEB_CONSOLE_HTML
    assert ".split-body .step-list-col { width: 280px; flex: 0 0 280px; min-height: 0; overflow-y: auto;" in WEB_CONSOLE_HTML
    assert "min-height: 58px; flex-shrink: 0;" in WEB_CONSOLE_HTML
    assert ".panel-confirm-area" in WEB_CONSOLE_HTML
    assert "flex-shrink: 0; max-height: 34vh; overflow-y: auto;" in WEB_CONSOLE_HTML


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_fallback_step_actions_reads_raw_role_log_before_stream_log(tmp_path):
    task_dir = tmp_path / "task-1"
    logs_dir = task_dir / "logs"
    logs_dir.mkdir(parents=True)
    (task_dir / "progress.json").write_text(json.dumps({
        "steps": [{
            "name": "test_round_1",
            "role": "qa_engineer",
            "started_at": "2026-05-12T12:50:03+00:00",
            "completed_at": "2026-05-12T13:00:04+00:00",
            "duration": 600.5,
        }]
    }), encoding="utf-8")
    (logs_dir / "test_round_1.stream.log").write_text(
        "━━━━━━━━━━━━━ 🤖 测试工程师 (__main_session__) ━━━━━━━━━━━━━\n"
        "✅ 完成 | 耗时 10m00s\n",
        encoding="utf-8",
    )
    _write_jsonl(logs_dir / "qa_engineer-1778590804.log", [
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "开始验证直播面板日志。"}},
        {"type": "item.completed", "item": {
            "type": "mcp_tool_call",
            "server": "local",
            "tool": "lookup",
            "arguments": {"memory_name": "architecture"},
            "result": {"content": [{"text": "ok"}]},
            "status": "completed",
        }},
        {"type": "item.completed", "item": {
            "type": "command_execution",
            "command": "pytest -q",
            "aggregated_output": "1 passed",
            "exit_code": 0,
            "status": "completed",
        }},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}},
    ])

    handler = WebConsoleHandler.__new__(WebConsoleHandler)
    actions = handler._load_fallback_step_actions(task_dir, "task-1", "test_round_1", "qa_engineer", "")

    action_types = [action["type"] for action in actions]
    assert "output" in action_types
    assert "mcp" in action_types
    assert "exec" in action_types
    assert any("开始验证直播面板日志" in action["text"] for action in actions)
    assert any("pytest -q" in action["text"] for action in actions)
    mcp_text = next(action["text"] for action in actions if action["type"] == "mcp")
    assert "参数: memory_name: architecture" in mcp_text
    assert "结果:\nok" in mcp_text
    assert '{"content"' not in mcp_text
    assert not all(action["type"] == "stream" for action in actions)


def test_fallback_step_actions_selects_raw_role_log_by_step_time_window(tmp_path):
    task_dir = tmp_path / "task-1"
    logs_dir = task_dir / "logs"
    logs_dir.mkdir(parents=True)
    (task_dir / "progress.json").write_text(json.dumps({
        "steps": [{
            "name": "test_round_1",
            "role": "qa_engineer",
            "started_at": "2026-05-12T12:50:03+00:00",
            "completed_at": "2026-05-12T13:00:04+00:00",
            "duration": 600.5,
        }]
    }), encoding="utf-8")
    _write_jsonl(logs_dir / "qa_engineer-1778586539.log", [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "错误的旧日志"}},
    ])
    _write_jsonl(logs_dir / "qa_engineer-1778590804.log", [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "正确的测试轮次日志"}},
    ])

    handler = WebConsoleHandler.__new__(WebConsoleHandler)
    actions = handler._load_fallback_step_actions(task_dir, "task-1", "test_round_1", "qa_engineer", "")

    text = "\n".join(action["text"] for action in actions)
    assert "正确的测试轮次日志" in text
    assert "错误的旧日志" not in text


def test_raw_role_mcp_result_prefers_content_text_over_json_wrappers():
    action = WebConsoleHandler._raw_role_event_to_action({
        "type": "item.completed",
        "item": {
            "type": "mcp_tool_call",
            "server": "docs",
            "tool": "fetch",
            "arguments": {"memory_name": "startup/role-memory-whitelist"},
            "result": {
                "content": [{
                    "type": "text",
                    "text": "# 项目记忆白名单\n\n这是项目级唯一的必读 Serena memory 清单。",
                }],
                "structured_content": {
                    "result": "# 项目记忆白名单\n\n这是项目级唯一的必读 Serena memory 清单。",
                },
            },
            "status": "completed",
        },
    })

    assert action["type"] == "mcp"
    assert "参数: memory_name: startup/role-memory-whitelist" in action["text"]
    assert "结果:\n# 项目记忆白名单" in action["text"]
    assert '{"content"' not in action["text"]
    assert '"structured_content"' not in action["text"]


def test_raw_role_serena_memory_result_is_summarized():
    action = WebConsoleHandler._raw_role_event_to_action({
        "type": "item.completed",
        "item": {
            "type": "mcp_tool_call",
            "server": "serena",
            "tool": "read_memory",
            "arguments": {"memory_name": "startup/role-memory-whitelist"},
            "result": {
                "content": [{
                    "type": "text",
                    "text": "# 项目记忆白名单\n\n这是项目级唯一的必读 Serena memory 清单。",
                }],
            },
            "status": "completed",
        },
    })

    assert action["type"] == "mcp"
    assert "已读取项目记忆：startup/role-memory-whitelist" in action["text"]
    assert "内容 " in action["text"]
    assert "# 项目记忆白名单" not in action["text"]
    assert '{"content"' not in action["text"]
