from __future__ import annotations

from pathlib import Path

from vizo_core.agent_runner import AgentError, AgentRunner, AgentRateLimitError, AgentTimeoutError
from lib.mcp_runtime import build_codex_mcp_config_overrides, build_main_session_mcp_env
from lib.runtime.codex_cli import resolve_codex_main_session_sandbox
from lib.runtime.sessions.persistent_runtime import resolve_connection_codex_home
from lib.runtime.subagents.codex import (
    _build_codex_command,
    _extract_codex_error_message,
    _extract_codex_message_text,
    _extract_codex_thread_id,
    _extract_codex_usage,
    _is_codex_rate_limited,
    _read_last_message,
)

from .base import BaseMainSessionAdapter


class CodexMainSessionAdapter(BaseMainSessionAdapter):
    runtime_family = "codex"
    supports_persistent = True

    def __init__(self, agent_runner: AgentRunner) -> None:
        self.agent_runner = agent_runner

    async def run(
        self,
        *,
        decision,
        session,
        turn_id: str,
        prompt: str,
        connection: dict,
        on_stream_event=None,
        on_raw_output=None,
    ):
        metadata = dict(getattr(session, "metadata", {}) or {})
        profile = {
            "base_url": connection.get("base_url", ""),
            "api_key": connection.get("api_key", ""),
            "selected_model": decision.provider_model or decision.display_model or decision.selected_model,
            "wire_api": "responses",
            "use_default_auth": str(connection.get("auth_mode") or "").strip() == "account_login",
        }
        runtime_dir = metadata.get("runtime_dir", "")
        if not runtime_dir:
            raise AgentError("主会话缺少 Codex runtime 工作目录", error_code="E301")
        last_message_path = metadata.get("last_message_path", "")
        if not last_message_path:
            raise AgentError("主会话缺少 Codex last-message 路径", error_code="E301")
        project_root = Path(str(metadata.get("project_root") or session.cwd or self.agent_runner.project_path or ".")).resolve()
        runtime_dir = str(metadata.get("runtime_dir") or "")
        codex_home = resolve_connection_codex_home(project_root, connection, fallback_key="main_session")
        codex_home.mkdir(parents=True, exist_ok=True)
        cmd = _build_codex_command(
            profile=profile,
            resume_session=str(session.resume_token or ""),
            last_message_path=Path(last_message_path),
            config_overrides=build_codex_mcp_config_overrides(
                str(session.cwd or ""),
                confirm_port=int(self.agent_runner.config.get("confirm_server", {}).get("port", 9390)),
                config_data=self.agent_runner.config,
                session_id=str(getattr(session, "session_id", "") or ""),
                runtime_dir=runtime_dir,
                project_root=str(project_root),
            ),
            sandbox_mode=resolve_codex_main_session_sandbox(self.agent_runner.config),
        )

        extra_env = {
            "CODEX_HOME": str(codex_home),
            **build_main_session_mcp_env(
                session_id=str(getattr(session, "session_id", "") or ""),
                runtime_dir=runtime_dir,
                project_root=str(metadata.get("project_root") or session.cwd or ""),
            ),
        }
        if not profile["use_default_auth"]:
            extra_env["OPENAI_API_KEY"] = str(connection.get("api_key") or "")

        try:
            stdout_raw, stderr_str, returncode, events = await self.agent_runner._run_subprocess_streaming(
                cmd,
                prompt,
                str(session.cwd or self.agent_runner.project_path or "."),
                idle_timeout=int(self.agent_runner.config.get("main_session_idle_timeout", 900)),
                max_timeout=int(self.agent_runner.config.get("main_session_max_timeout", 3600)),
                on_stream_event=on_stream_event,
                role="assistant",
                extra_env=extra_env,
            )
        except AgentTimeoutError:
            raise

        if on_raw_output and stdout_raw:
            on_raw_output(stdout_raw)

        input_tokens, output_tokens, cost_usd = _extract_codex_usage(events)
        if returncode != 0:
            message = _extract_codex_error_message(stderr_str, events, stdout_raw)
            if _is_codex_rate_limited(message):
                raise AgentRateLimitError(
                    f"Codex 主会话被限流: {message[:240]}",
                    error_code="E201",
                    cost_usd=cost_usd,
                )
            raise AgentError(f"Codex 主会话执行失败: {message[:300]}", error_code="E301", cost_usd=cost_usd)

        last_message = _read_last_message(Path(last_message_path)) or _extract_codex_message_text(events)
        thread_id = _extract_codex_thread_id(events) or str(session.resume_token or "")
        return {
            "native_session_id": thread_id,
            "resume_token": thread_id,
            "selected_model": profile["selected_model"],
            "provider_model": profile["selected_model"],
            "result": last_message,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost_usd,
            },
            "raw_output": stdout_raw,
        }
