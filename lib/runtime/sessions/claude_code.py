from __future__ import annotations

import json
import shutil

from agent_runner import AgentError, AgentRunner, AgentTimeoutError
from lib.mcp_runtime import build_main_session_mcp_config, build_main_session_mcp_env
from lib.settings_handler import get_main_session_api_model, infer_main_session_display_model

from .base import BaseMainSessionAdapter


class ClaudeMainSessionAdapter(BaseMainSessionAdapter):
    runtime_family = "claude_code"
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
        cli_bin = shutil.which("claude") or "claude"
        cmd = [
            cli_bin,
            "-p",
            "-",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
        ]
        display_model = str(decision.display_model or decision.selected_model or "sonnet")
        if session.resume_token:
            cmd.extend(["--resume", session.resume_token])
        cmd.extend(["--model", display_model])

        metadata = dict(getattr(session, "metadata", {}) or {})
        mcp_config = build_main_session_mcp_config(
            session.cwd or "",
            confirm_port=int(self.agent_runner.config.get("confirm_server", {}).get("port", 9390)),
            config_data=self.agent_runner.config,
            session_id=str(getattr(session, "session_id", "") or ""),
            runtime_dir=str(metadata.get("runtime_dir") or ""),
            project_root=str(metadata.get("project_root") or session.cwd or ""),
        )
        cmd.extend(["--mcp-config", json.dumps(mcp_config, ensure_ascii=False), "--strict-mcp-config"])

        extra_env = dict(connection.get("env", {}) or {})
        extra_env.update(
            build_main_session_mcp_env(
                session_id=str(getattr(session, "session_id", "") or ""),
                runtime_dir=str(metadata.get("runtime_dir") or ""),
                project_root=str(metadata.get("project_root") or session.cwd or ""),
            )
        )
        api_key = str(connection.get("api_key") or "").strip()
        if api_key:
            extra_env["ANTHROPIC_AUTH_TOKEN"] = api_key
            extra_env.pop("ANTHROPIC_API_KEY", None)
        runtime_base_url = str(connection.get("runtime_base_url") or connection.get("base_url") or "").strip()
        if runtime_base_url and runtime_base_url != "https://api.anthropic.com":
            extra_env["ANTHROPIC_BASE_URL"] = runtime_base_url
            extra_env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
        provider_model = str(
            decision.provider_model
            or get_main_session_api_model(
                connection.get("base_url", ""),
                current_env=connection.get("env", {}) or {},
                prefer=decision.display_model or "sonnet",
                stored_provider_id=connection.get("provider_id"),
            )
        ).strip()
        if provider_model:
            extra_env["CLAUDE_CODE_SUBAGENT_MODEL"] = provider_model
            if not provider_model.startswith("claude-"):
                extra_env["ANTHROPIC_MODEL"] = provider_model

        work_dir = str(session.cwd or self.agent_runner.project_path or ".")
        try:
            stdout_raw, stderr_str, returncode, events = await self.agent_runner._run_subprocess_streaming(
                cmd,
                prompt,
                work_dir,
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

        if returncode != 0:
            message = stderr_str.strip() or _extract_claude_result_message(events) or stdout_raw[-400:]
            raise AgentError(f"Claude 主会话执行失败: {message[:300]}", error_code="E301")

        native_session_id = _extract_claude_session_id(events) or session.native_session_id or session.resume_token
        actual_provider_model = str(_extract_claude_result_model(events) or provider_model).strip()
        return {
            "native_session_id": native_session_id,
            "resume_token": native_session_id,
            "selected_model": infer_main_session_display_model(actual_provider_model, fallback=display_model),
            "provider_model": actual_provider_model,
            "result": _extract_claude_result_message(events),
            "usage": _extract_claude_usage(events),
            "raw_output": stdout_raw,
        }


def _extract_claude_session_id(events: list[dict]) -> str:
    for event in events:
        session_id = str(event.get("session_id") or "")
        if session_id:
            return session_id
    return ""


def _extract_claude_result_message(events: list[dict]) -> str:
    chunks: list[str] = []
    for event in events:
        if event.get("type") != "assistant":
            continue
        message = event.get("message", {})
        if not isinstance(message, dict):
            continue
        for block in message.get("content", []) if isinstance(message.get("content"), list) else []:
            if block.get("type") == "text":
                text = str(block.get("text") or "")
                if text:
                    chunks.append(text)
    return "\n".join(chunks).strip()


def _extract_claude_result_model(events: list[dict]) -> str:
    for event in reversed(events):
        if event.get("type") != "assistant":
            continue
        message = event.get("message", {})
        if not isinstance(message, dict):
            continue
        model = str(message.get("model") or "").strip()
        if model:
            return model
    return ""


def _extract_claude_usage(events: list[dict]) -> dict:
    for event in reversed(events):
        if event.get("type") == "result":
            usage = event.get("usage")
            if isinstance(usage, dict):
                return usage
    return {}
