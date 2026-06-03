import asyncio
import json

from vizo_core.agent_runner import AgentError, AgentResult, AgentTimeoutError
from lib.runtime.contracts import RUNTIME_FAMILY_CLAUDE_CODE, RUNTIME_FAMILY_CODEX
from lib.runtime.subagents.controller import SubAgentRuntimeController


class _FailingCodexAdapter:
    def __init__(self, error):
        self.error = error

    async def run(self, **kwargs):
        raise self.error


class _SuccessfulClaudeAdapter:
    def __init__(self):
        self.calls = []

    async def run(self, *, decision, execution_context, **kwargs):
        self.calls.append((decision, kwargs))
        assert decision.runtime_family == RUNTIME_FAMILY_CLAUDE_CODE
        assert kwargs["model_override"] == "glm"
        return AgentResult(
            success=True,
            data={"status": "success"},
            raw_output="ok",
            cost_tokens=0,
            duration=0.0,
            exit_code=0,
            model="glm",
        )


class _Registry:
    def __init__(self, claude_adapter, codex_error):
        self.claude_adapter = claude_adapter
        self.codex_error = codex_error

    def get_subagent(self, runtime_family):
        if runtime_family == RUNTIME_FAMILY_CODEX:
            return _FailingCodexAdapter(self.codex_error)
        if runtime_family == RUNTIME_FAMILY_CLAUDE_CODE:
            return self.claude_adapter
        raise AssertionError(runtime_family)


class _Runner:
    def __init__(self, config):
        self.config = config


def _build_config():
    return {
        "codex_subagent_account_login_fallback_model": "glm",
        "external_models": {
            "anthropic": {
                "provider_id": "openai",
                "base_url": "https://api.openai.com/v1",
                "auth_mode": "account_login",
                "auth_status": "ready",
                "codex_home": ".vizo/codex/connections/test",
                "env": {},
            },
            "glm": {
                "provider_id": "glm",
                "access_mode": "anthropic_native",
                "provider_family": "glm",
                "base_url": "https://example.invalid",
                "cli_model": "glm-5",
            },
        },
    }


def _run_fallback_case(tmp_path, monkeypatch, codex_error):
    session_dir = tmp_path / ".vizo" / "sessions" / "main" / "ms_test"
    session_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text(
        json.dumps(
            {
                "session_id": "ms_test",
                "display_model": "gpt-5.5",
                "provider_model": "gpt-5.5",
                "runtime_family": "codex",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("VIZO_MAIN_SESSION_DIR", str(session_dir))
    monkeypatch.setattr("lib.runtime.cli_discovery.cli_available", lambda name: name == "codex")

    config = _build_config()
    claude_adapter = _SuccessfulClaudeAdapter()
    controller = SubAgentRuntimeController(
        _Runner(config),
        project_root=tmp_path,
        registry=_Registry(claude_adapter, codex_error),
    )

    result = asyncio.run(
        controller.run(
            role="requirement_analyst",
            task_dir=None,
            input_docs={},
            output_file=None,
            project="vizo",
        )
    )

    assert result.success is True
    assert len(claude_adapter.calls) == 1


def test_codex_account_login_preflight_failure_falls_back_to_configured_model(tmp_path, monkeypatch):
    _run_fallback_case(
        tmp_path,
        monkeypatch,
        AgentError("Codex 云端前置条件预热失败", error_code="E204"),
    )


def test_codex_account_login_idle_timeout_falls_back_to_configured_model(tmp_path, monkeypatch):
    _run_fallback_case(
        tmp_path,
        monkeypatch,
        AgentTimeoutError("空闲超时 (120s)，共收到 8 个事件", error_code="E101", timeout_type="idle"),
    )
