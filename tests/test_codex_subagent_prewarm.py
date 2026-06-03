import asyncio

import pytest

from vizo_core.agent_runner import AgentError, AgentTimeoutError
from lib.runtime.subagents.controller import _classify_error_status
from lib.runtime.subagents.codex import (
    CODEX_CLOUD_REQUIREMENTS_ERROR_CODE,
    CODEX_OUTPUT_CONTENT_BEGIN,
    CODEX_OUTPUT_CONTENT_END,
    _PREWARMED_PROFILE_KEYS,
    _append_codex_output_file_guidance,
    _build_codex_env,
    _codex_prewarm_key,
    _ensure_codex_cloud_requirements_prewarmed,
    _extract_codex_action,
    _materialize_codex_output_file,
    _resolve_codex_idle_timeout,
    _resolve_codex_mcp_server_names,
    _resolve_codex_sandbox,
)
from lib.runtime.codex_cli import build_codex_exec_command


def _profile(tmp_path):
    return {
        "use_default_auth": True,
        "codex_home": str(tmp_path / "codex-home"),
        "base_url": "https://chatgpt.com/backend-api",
        "selected_model": "gpt-5.5",
        "requested_model": "__main_session__",
        "wire_api": "responses",
    }


def test_codex_prewarm_retries_cloud_requirements_timeout_then_marks_success(tmp_path):
    class FakeRunner:
        config = {
            "codex_subagent_prewarm_attempts": 2,
            "codex_subagent_prewarm_retry_delay": 0,
        }

        def __init__(self):
            self.calls = 0

        async def _run_subprocess_streaming(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return "", "Error: timed out waiting for cloud requirements after 15s", 1, []
            return (
                '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}',
                "",
                0,
                [{"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}],
            )

    profile = _profile(tmp_path)
    runner = FakeRunner()
    _PREWARMED_PROFILE_KEYS.clear()

    asyncio.run(
        _ensure_codex_cloud_requirements_prewarmed(
            runner,
            role="requirement_analyst",
            profile=profile,
            work_dir=tmp_path,
            runtime_dir=tmp_path,
            live_log=None,
            parent_live_log=None,
        )
    )

    assert runner.calls == 2
    assert _codex_prewarm_key(profile) in _PREWARMED_PROFILE_KEYS


def test_codex_prewarm_exhaustion_uses_preflight_error_code(tmp_path):
    class FakeRunner:
        config = {
            "codex_subagent_prewarm_attempts": 2,
            "codex_subagent_prewarm_retry_delay": 0,
        }

        def __init__(self):
            self.calls = 0

        async def _run_subprocess_streaming(self, *args, **kwargs):
            self.calls += 1
            return "", "Error: timed out waiting for cloud requirements after 15s", 1, []

    runner = FakeRunner()
    _PREWARMED_PROFILE_KEYS.clear()

    with pytest.raises(AgentError) as excinfo:
        asyncio.run(
            _ensure_codex_cloud_requirements_prewarmed(
                runner,
                role="requirement_analyst",
                profile=_profile(tmp_path),
                work_dir=tmp_path,
                runtime_dir=tmp_path,
                live_log=None,
                parent_live_log=None,
            )
        )

    assert runner.calls == 2
    assert excinfo.value.error_code == CODEX_CLOUD_REQUIREMENTS_ERROR_CODE
    assert "云端前置条件预热失败" in str(excinfo.value)


def test_codex_prewarm_timeout_after_runtime_events_does_not_mark_success(tmp_path):
    class FakeRunner:
        config = {
            "codex_subagent_prewarm_attempts": 3,
            "codex_subagent_prewarm_retry_delay": 0,
        }

        def __init__(self):
            self.calls = 0

        async def _run_subprocess_streaming(self, *args, **kwargs):
            self.calls += 1
            raise AgentTimeoutError(
                "空闲超时 (45s)，共收到 2 个事件",
                error_code="E101",
                timeout_type="idle",
                events=[{"type": "thread.started"}, {"type": "turn.started"}],
            )

    profile = _profile(tmp_path)
    runner = FakeRunner()
    _PREWARMED_PROFILE_KEYS.clear()

    with pytest.raises(AgentError) as excinfo:
        asyncio.run(
            _ensure_codex_cloud_requirements_prewarmed(
                runner,
                role="requirement_analyst",
                profile=profile,
                work_dir=tmp_path,
                runtime_dir=tmp_path,
                live_log=None,
                parent_live_log=None,
            )
        )

    assert runner.calls == 3
    assert excinfo.value.error_code == CODEX_CLOUD_REQUIREMENTS_ERROR_CODE
    assert _codex_prewarm_key(profile) not in _PREWARMED_PROFILE_KEYS


def test_codex_cloud_requirements_error_is_not_classified_as_rate_limit():
    error = AgentError("Codex 云端前置条件获取超时", error_code=CODEX_CLOUD_REQUIREMENTS_ERROR_CODE)

    assert _classify_error_status(error) == "preflight_timeout"


def test_codex_item_events_extract_live_actions():
    assert (
        _extract_codex_action(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "我已经确认主题切换入口，接下来写需求分析文件。",
                },
            }
        )
        == "💬 我已经确认主题切换入口，接下来写需求分析文件。"
    )
    assert (
        _extract_codex_action(
            {
                "type": "item.started",
                "item": {
                    "type": "mcp_tool_call",
                    "server": "serena",
                    "tool": "read_memory",
                },
            }
        )
        == "🔧 serena.read_memory"
    )


def test_codex_idle_timeout_can_be_overridden_per_role():
    config = {
        "idle_timeout": {"requirement_analyst": 900},
        "codex_subagent_idle_timeout": {"requirement_analyst": 120},
    }

    assert _resolve_codex_idle_timeout(config, "requirement_analyst") == 120
    assert _resolve_codex_idle_timeout(config, "architect") == 900
    assert _resolve_codex_idle_timeout({}, "product_manager") == 600


def test_codex_prompt_guidance_requires_marked_output_handoff(tmp_path):
    output_file = tmp_path / "00-requirement-analysis.md"
    prompt = _append_codex_output_file_guidance("base prompt", output_file)

    assert str(output_file) in prompt
    assert CODEX_OUTPUT_CONTENT_BEGIN in prompt
    assert CODEX_OUTPUT_CONTENT_END in prompt
    assert "不要调用命令写这个目标文件" in prompt


def test_codex_output_markers_materialize_output_file(tmp_path):
    output_file = tmp_path / "00-requirement-analysis.md"
    message = (
        "analysis complete\n"
        f"{CODEX_OUTPUT_CONTENT_BEGIN}\n"
        "# 需求分析\n\n正文\n"
        f"{CODEX_OUTPUT_CONTENT_END}\n"
        '{"status":"success","summary":"ok","files_changed":[]}'
    )

    assert _materialize_codex_output_file(output_file, message) is True
    assert output_file.read_text(encoding="utf-8") == "# 需求分析\n\n正文\n"


def test_codex_home_relative_path_resolves_against_project_root_for_worktrees(tmp_path):
    project_root = tmp_path / "project"
    worktree = project_root / ".vizo" / "worktrees" / "task-backend"
    profile = {
        "use_default_auth": True,
        "codex_home": ".vizo/codex/connections/conn_test",
    }

    env = _build_codex_env(profile, worktree, project_root=project_root)

    assert env["CODEX_HOME"] == str((project_root / ".vizo/codex/connections/conn_test").resolve())


def test_codex_subagent_sandbox_can_be_overridden_per_role():
    config = {
        "codex_subagent_sandbox": {
            "default": "workspace-write",
            "qa_engineer": "danger-full-access",
        }
    }

    assert _resolve_codex_sandbox(config, "qa_engineer") == "danger-full-access"
    assert _resolve_codex_sandbox(config, "backend_developer") == "workspace-write"
    assert _resolve_codex_sandbox({"codex_subagent_sandbox": "bad-value"}, "qa_engineer") == "workspace-write"


def test_codex_mcp_server_names_follow_role_permissions():
    config = {
        "mcp_permissions": {
            "product_manager": ["serena"],
            "qa_engineer": ["mcp__mcp-chrome", "playwright", "playwright"],
        }
    }

    assert _resolve_codex_mcp_server_names(config, "product_manager") == ["serena"]
    assert _resolve_codex_mcp_server_names(config, "qa_engineer") == ["mcp-chrome", "playwright"]
    assert _resolve_codex_mcp_server_names(config, "unknown_role") == []


def test_codex_exec_command_uses_requested_sandbox(tmp_path):
    cmd = build_codex_exec_command(
        profile={"selected_model": "gpt-5.5"},
        resume_session="",
        last_message_path=tmp_path / "last-message.txt",
        sandbox_mode="danger-full-access",
    )

    assert cmd[cmd.index("--sandbox") + 1] == "danger-full-access"
