from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from pathlib import Path

from agent_runner import (
    AgentError,
    AgentRateLimitError,
    AgentResult,
    AgentSignalInterrupt,
    AgentTimeoutError,
)
from lib.runtime.codex_cli import build_codex_exec_command, resolve_codex_automation_sandbox
from lib.mcp_runtime import build_codex_mcp_config_overrides
from lib.settings_handler import resolve_subagent_codex_candidate

from .base import BaseSubagentAdapter

logger = logging.getLogger(__name__)

CODEX_CLOUD_REQUIREMENTS_ERROR_CODE = "E204"
_PREWARMED_PROFILE_KEYS: set[str] = set()
CODEX_OUTPUT_CONTENT_BEGIN = "<<<VIZO_OUTPUT_FILE_CONTENT_BEGIN>>>"
CODEX_OUTPUT_CONTENT_END = "<<<VIZO_OUTPUT_FILE_CONTENT_END>>>"
CODEX_ROLE_IDLE_TIMEOUT_DEFAULTS = {
    "product_manager": 600,
}


class CodexSubagentAdapter(BaseSubagentAdapter):
    runtime_family = "codex"

    def __init__(self, agent_runner) -> None:
        self.agent_runner = agent_runner

    async def run(self, *, decision, execution_context, **kwargs):
        try:
            from lib.config_loader import load_config as _reload_config

            self.agent_runner.config = _reload_config(force_reload=True)
        except Exception:
            logger.debug("Codex 子代理启动前刷新配置失败，继续使用现有配置", exc_info=True)

        role = kwargs.get("role") or execution_context.role
        task_dir = kwargs.get("task_dir")
        output_file = kwargs.get("output_file")
        project = kwargs.get("project") or self.agent_runner.config.get("default_project", "")
        work_dir = kwargs.get("cwd") or self.agent_runner._get_project_path(project)
        prompt_override = kwargs.get("prompt_override")

        if prompt_override:
            prompt = prompt_override
        else:
            prompt = await self.agent_runner._build_prompt(
                role,
                task_dir,
                kwargs.get("input_docs"),
                kwargs.get("memories"),
                output_file,
                project,
                session_recall=kwargs.get("session_recall"),
                work_state_summary=kwargs.get("work_state_summary"),
                template_override=kwargs.get("template_override"),
            )
        prompt = _append_codex_output_file_guidance(prompt, output_file)

        estimated_tokens = self.agent_runner._estimate_tokens(prompt)
        if estimated_tokens > 150_000:
            logger.info("Codex prompt 较大（%s tokens）", estimated_tokens)

        candidate = resolve_subagent_codex_candidate(
            self.agent_runner.config,
            role=role,
            model_override=kwargs.get("model_override"),
        )
        profile = candidate.get("profile")
        if not profile:
            raise AgentError(
                "当前连接不满足 Codex direct 试点条件，请改用 Claude 主链或先修复诊断项",
                error_code="E301",
            )

        task_dir_path = Path(task_dir) if task_dir else None
        runtime_dir = _runtime_work_dir(task_dir_path, execution_context.step_name or role)
        runtime_dir.mkdir(parents=True, exist_ok=True)
        last_message_path = runtime_dir / f"{role}.codex-last-message.txt"
        last_message_path.unlink(missing_ok=True)

        config_overrides = build_codex_mcp_config_overrides(
            str(work_dir),
            confirm_port=int(self.agent_runner.config.get("confirm_server", {}).get("port", 9390)),
            config_data=self.agent_runner.config,
            allowed_server_names=_resolve_codex_mcp_server_names(self.agent_runner.config, role),
        )
        sandbox_mode = _resolve_codex_sandbox(self.agent_runner.config, role)
        cmd = _build_codex_command(
            profile=profile,
            resume_session=str(kwargs.get("resume_session") or ""),
            last_message_path=last_message_path,
            config_overrides=config_overrides,
            sandbox_mode=sandbox_mode,
        )

        live_log = None
        parent_live_log = None
        if task_dir_path:
            live_log_dir = task_dir_path / "logs"
            live_log_dir.mkdir(parents=True, exist_ok=True)
            live_log = str(live_log_dir / "live.log")
            if "/sub-" in str(task_dir_path):
                parent_dir = task_dir_path.parent
                parent_log_dir = parent_dir / "logs"
                if parent_log_dir.exists():
                    parent_live_log = str(parent_log_dir / "live.log")
            startup_line = (
                f"\n{'=' * 60}\n"
                f"[{time.strftime('%H:%M:%S')}] [Codex] {role} ({profile['selected_model']}) 启动\n"
                f"{'=' * 60}\n"
            )
            with open(live_log, "a", encoding="utf-8") as lf:
                lf.write(startup_line)
            if parent_live_log:
                try:
                    with open(parent_live_log, "a", encoding="utf-8") as lf:
                        lf.write(startup_line)
                except Exception:
                    pass

        await _ensure_codex_cloud_requirements_prewarmed(
            self.agent_runner,
            role=role,
            profile=profile,
            work_dir=work_dir,
            runtime_dir=runtime_dir,
            live_log=live_log,
            parent_live_log=parent_live_log,
            config_overrides=config_overrides,
            sandbox_mode=sandbox_mode,
            task_id=kwargs.get("task_id"),
            parent_task_id=kwargs.get("parent_task_id"),
        )

        git_before = await self.agent_runner._git_status(work_dir)
        idle_timeout = _resolve_codex_idle_timeout(self.agent_runner.config, role)
        max_timeout = 3600
        start_time = time.time()

        try:
            stdout_raw, stderr_str, returncode, events = await self.agent_runner._run_subprocess_streaming(
                cmd,
                prompt,
                str(work_dir),
                idle_timeout,
                max_timeout,
                live_log_file=live_log,
                parent_live_log_file=parent_live_log,
                on_action=kwargs.get("on_action") or self.agent_runner._on_action_callback,
                on_stream_event=kwargs.get("on_stream_event") or self.agent_runner._on_stream_event_callback,
                task_id=kwargs.get("task_id"),
                parent_task_id=kwargs.get("parent_task_id"),
                on_paused=kwargs.get("on_paused"),
                on_resumed=kwargs.get("on_resumed"),
                role=role,
                extra_env=_build_codex_env(
                    profile,
                    work_dir,
                    project_root=getattr(self.agent_runner, "project_path", None),
                ),
                action_extractor=_extract_codex_action,
            )
        except AgentTimeoutError as error:
            _record_partial_cost(
                self.agent_runner,
                role=role,
                model=profile["selected_model"],
                task_dir=task_dir_path,
                output_file=output_file,
                duration=time.time() - start_time,
                events=getattr(error, "events", None) or [],
            )
            raise
        except AgentSignalInterrupt as error:
            _record_partial_cost(
                self.agent_runner,
                role=role,
                model=profile["selected_model"],
                task_dir=task_dir_path,
                output_file=output_file,
                duration=time.time() - start_time,
                events=getattr(error, "events", None) or [],
            )
            raise

        duration = time.time() - start_time
        input_tokens, output_tokens, cost_usd = _extract_codex_usage(events)
        self.agent_runner._record_cost(
            role,
            profile["selected_model"],
            input_tokens,
            output_tokens,
            cost_usd,
            task_dir_path,
            duration=duration,
            output_file=str(output_file) if output_file else "",
        )

        if returncode != 0:
            message = _extract_codex_error_message(stderr_str, events, stdout_raw)
            if _is_codex_cloud_requirements_timeout(message):
                _PREWARMED_PROFILE_KEYS.discard(_codex_prewarm_key(profile))
                raise AgentError(
                    f"Codex [{role}] 云端前置条件获取超时: {message[:220]}",
                    error_code=CODEX_CLOUD_REQUIREMENTS_ERROR_CODE,
                    cost_usd=cost_usd,
                )
            if _is_codex_rate_limited(message):
                raise AgentRateLimitError(
                    f"Codex [{role}] 被限流: {message[:240]}",
                    error_code="E201",
                    cost_usd=cost_usd,
                )
            raise AgentError(
                f"Codex [{role}] 执行失败: {message[:300]}",
                error_code="E301",
                cost_usd=cost_usd,
            )

        last_message = _read_last_message(last_message_path) or _extract_codex_message_text(events)
        materialized_output = _materialize_codex_output_file(output_file, last_message)
        data = {
            "session_id": _extract_codex_thread_id(events) or str(kwargs.get("resume_session") or ""),
            "result": last_message,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        }
        agent_json = self.agent_runner._extract_agent_json(last_message) if last_message else None
        if isinstance(agent_json, dict):
            data["agent_data"] = agent_json
            for key, value in agent_json.items():
                if key not in data:
                    data[key] = value
        if materialized_output:
            data["codex_output_materialized"] = True

        git_after = await self.agent_runner._git_status(work_dir)
        changes = self.agent_runner._diff_git_status(git_before, git_after)

        if task_dir_path:
            log_file = task_dir_path / "logs" / f"{role}-{int(time.time())}.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text(stdout_raw, encoding="utf-8")

        return AgentResult(
            success=True,
            data=data,
            raw_output=stdout_raw,
            cost_tokens=input_tokens + output_tokens or estimated_tokens,
            duration=duration,
            exit_code=returncode,
            model=profile["selected_model"],
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            changes=changes,
            timeout_type=None,
            events_count=len(events),
            last_event_time=time.time() if events else 0.0,
        )


def _runtime_work_dir(task_dir: Path | None, step_name: str) -> Path:
    if task_dir is None:
        return Path("/tmp") / "vizo-codex-runtime"
    return task_dir / "runtime" / "subagents" / (step_name or "unknown")


def _append_codex_output_file_guidance(prompt: str, output_file) -> str:
    if not output_file:
        return prompt
    return (
        f"{prompt}\n\n"
        "# Codex CLI 输出文件交接要求\n"
        f"- 当前运行在 Codex CLI 子代理中，最终文档目标路径是 `{output_file}`。\n"
        "- 不要调用命令写这个目标文件，也不要只说明“接下来写入文件”。\n"
        "- 在最终回复中把完整 Markdown 文档放在下面两个独占行标记之间，Vizo 运行时会负责写入目标文件：\n"
        f"{CODEX_OUTPUT_CONTENT_BEGIN}\n"
        "<这里放完整 Markdown 文档内容>\n"
        f"{CODEX_OUTPUT_CONTENT_END}\n"
        "- 标记结束后，再输出最终 JSON 摘要："
        '{"status":"success","summary":"...","files_changed":["'
        f'{output_file}"]}}。\n'
    )


def _extract_codex_output_file_content(message: str | None) -> str | None:
    if not message:
        return None
    start = message.find(CODEX_OUTPUT_CONTENT_BEGIN)
    if start < 0:
        return None
    content_start = start + len(CODEX_OUTPUT_CONTENT_BEGIN)
    end = message.find(CODEX_OUTPUT_CONTENT_END, content_start)
    if end < 0:
        return None
    content = message[content_start:end].strip()
    return content or None


def _materialize_codex_output_file(output_file, message: str | None) -> bool:
    if not output_file:
        return False
    content = _extract_codex_output_file_content(message)
    if content is None:
        return False
    path = Path(output_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")
    return True


def _build_codex_command(
    *,
    profile: dict,
    resume_session: str,
    last_message_path: Path,
    config_overrides: list[str] | None = None,
    sandbox_mode: str | None = None,
) -> list[str]:
    codex_config_args: list[str] = []
    for override in config_overrides or []:
        codex_config_args.extend(["-c", override])
    provider_args = _build_codex_provider_args(profile)
    return build_codex_exec_command(
        profile=profile,
        resume_session=resume_session,
        last_message_path=last_message_path,
        provider_args=provider_args,
        config_args=codex_config_args,
        sandbox_mode=sandbox_mode,
    )


def _build_codex_provider_args(profile: dict) -> list[str]:
    if bool(profile.get("use_default_auth")):
        return []
    provider_name = "vizo_runtime"
    auth_env_key = str(profile.get("auth_env_key") or "OPENAI_API_KEY")
    provider_config = (
        f'model_providers.{provider_name}='
        f'{{name="{provider_name}",base_url="{profile["base_url"]}",'
        f'wire_api="{profile.get("wire_api", "responses")}",env_key="{auth_env_key}"}}'
    )
    return [
        "-c",
        f"model_provider={provider_name}",
        "-c",
        provider_config,
    ]


def _build_codex_env(
    profile: dict,
    work_dir: str | Path,
    *,
    project_root: str | Path | None = None,
) -> dict[str, str]:
    env: dict[str, str] = {}
    codex_home = str(profile.get("codex_home") or "").strip()
    if codex_home:
        codex_home_path = Path(codex_home).expanduser()
        if not codex_home_path.is_absolute():
            base_dir = Path(project_root) if project_root else Path(work_dir)
            codex_home_path = base_dir / codex_home_path
        codex_home_path = codex_home_path.resolve()
        env["CODEX_HOME"] = str(codex_home_path)
    api_key = str(profile.get("api_key") or "").strip()
    if api_key and not bool(profile.get("use_default_auth")):
        env["OPENAI_API_KEY"] = api_key
    return env


async def _ensure_codex_cloud_requirements_prewarmed(
    agent_runner,
    *,
    role: str,
    profile: dict,
    work_dir: str | Path,
    runtime_dir: Path,
    live_log: str | None,
    parent_live_log: str | None,
    config_overrides: list[str] | None = None,
    sandbox_mode: str | None = None,
    task_id: str | None = None,
    parent_task_id: str | None = None,
) -> None:
    """Warm Codex account-login cloud requirements before the real role prompt."""
    if not bool(profile.get("use_default_auth")):
        return

    config = getattr(agent_runner, "config", {}) or {}
    if config.get("codex_subagent_prewarm_enabled", True) is False:
        return

    profile_key = _codex_prewarm_key(profile)
    if profile_key in _PREWARMED_PROFILE_KEYS:
        return

    attempts = _coerce_int(
        config.get("codex_subagent_prewarm_attempts"),
        default=3,
        minimum=1,
        maximum=8,
    )
    idle_timeout = _coerce_int(
        config.get("codex_subagent_prewarm_idle_timeout"),
        default=45,
        minimum=10,
        maximum=300,
    )
    max_timeout = _coerce_int(
        config.get("codex_subagent_prewarm_max_timeout"),
        default=max(60, idle_timeout + 15),
        minimum=idle_timeout,
        maximum=600,
    )
    retry_delay = _coerce_int(
        config.get("codex_subagent_prewarm_retry_delay"),
        default=8,
        minimum=0,
        maximum=120,
    )

    prewarm_last_message = runtime_dir / "codex-prewarm-last-message.txt"
    prompt = (
        "你是 Vizo 的 Codex 子代理预热步骤。"
        "不要调用工具，不要读取文件，不要修改文件，只输出 ok。"
    )
    last_error = ""

    for attempt in range(1, attempts + 1):
        prewarm_last_message.unlink(missing_ok=True)
        _append_live_log(
            live_log,
            parent_live_log,
            f"[{time.strftime('%H:%M:%S')}] Codex 云端前置条件预热 "
            f"({attempt}/{attempts})\n",
        )
        cmd = _build_codex_command(
            profile=profile,
            resume_session="",
            last_message_path=prewarm_last_message,
            config_overrides=config_overrides or [],
            sandbox_mode=sandbox_mode,
        )

        try:
            stdout_raw, stderr_str, returncode, events = await agent_runner._run_subprocess_streaming(
                cmd,
                prompt,
                str(work_dir),
                idle_timeout,
                max_timeout,
                live_log_file=live_log,
                parent_live_log_file=parent_live_log,
                on_action=None,
                on_stream_event=None,
                task_id=task_id,
                parent_task_id=parent_task_id,
                role=f"{role}:prewarm",
                extra_env=_build_codex_env(
                    profile,
                    work_dir,
                    project_root=getattr(agent_runner, "project_path", None),
                ),
                action_extractor=_extract_codex_action,
            )
        except AgentTimeoutError as error:
            last_error = str(error)
            if attempt < attempts:
                await asyncio.sleep(retry_delay)
                continue
            raise AgentError(
                f"Codex [{role}] 云端前置条件预热超时: {last_error[:220]}",
                error_code=CODEX_CLOUD_REQUIREMENTS_ERROR_CODE,
            ) from error

        if returncode == 0 and _codex_prewarm_has_expected_output(prewarm_last_message, events):
            _PREWARMED_PROFILE_KEYS.add(profile_key)
            _append_live_log(
                live_log,
                parent_live_log,
                f"[{time.strftime('%H:%M:%S')}] Codex 云端前置条件预热完成\n",
            )
            return
        if returncode == 0:
            last_error = "预热进程已退出，但未收到期望的 ok 响应"
            if attempt < attempts:
                await asyncio.sleep(retry_delay)
                continue
            raise AgentError(
                f"Codex [{role}] 云端前置条件预热未完成: {last_error}",
                error_code=CODEX_CLOUD_REQUIREMENTS_ERROR_CODE,
            )

        message = _extract_codex_error_message(stderr_str, events, stdout_raw)
        last_error = message
        if _is_codex_cloud_requirements_timeout(message):
            if attempt < attempts:
                await asyncio.sleep(retry_delay)
                continue
            raise AgentError(
                f"Codex [{role}] 云端前置条件预热失败: {message[:220]}",
                error_code=CODEX_CLOUD_REQUIREMENTS_ERROR_CODE,
            )
        if _is_codex_rate_limited(message):
            raise AgentRateLimitError(
                f"Codex [{role}] 预热时被限流: {message[:240]}",
                error_code="E201",
            )
        raise AgentError(
            f"Codex [{role}] 预热失败: {message[:260]}",
            error_code="E301",
        )

    raise AgentError(
        f"Codex [{role}] 云端前置条件预热失败: {last_error[:220]}",
        error_code=CODEX_CLOUD_REQUIREMENTS_ERROR_CODE,
    )


def _codex_prewarm_key(profile: dict) -> str:
    raw = "|".join(
        str(profile.get(key) or "")
        for key in (
            "codex_home",
            "base_url",
            "selected_model",
            "requested_model",
            "wire_api",
            "use_default_auth",
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _codex_prewarm_has_expected_output(last_message_path: Path, events: list[dict]) -> bool:
    message = _read_last_message(last_message_path) or _extract_codex_message_text(events)
    normalized = str(message or "").strip().strip("`'\"").strip().lower()
    return normalized == "ok"


def _append_live_log(live_log: str | None, parent_live_log: str | None, line: str) -> None:
    for path in (live_log, parent_live_log):
        if not path:
            continue
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line)
        except Exception:
            pass


def _coerce_int(value, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _resolve_codex_idle_timeout(config: dict, role: str) -> int:
    role_default = CODEX_ROLE_IDLE_TIMEOUT_DEFAULTS.get(role, 900)
    default_timeout = _coerce_int(
        (config.get("idle_timeout") or {}).get(role) if isinstance(config.get("idle_timeout"), dict) else None,
        default=role_default,
        minimum=30,
        maximum=3600,
    )
    override = config.get("codex_subagent_idle_timeout")
    if isinstance(override, dict):
        return _coerce_int(
            override.get(role) if role in override else override.get("*"),
            default=default_timeout,
            minimum=30,
            maximum=3600,
        )
    if override is not None:
        return _coerce_int(override, default=default_timeout, minimum=30, maximum=3600)
    return default_timeout


def _resolve_codex_sandbox(config: dict, role: str) -> str:
    override = config.get("codex_subagent_sandbox")
    selected = None
    if isinstance(override, dict):
        selected = override.get(role) or override.get("*") or override.get("default")
    elif override is not None:
        selected = override
    return resolve_codex_automation_sandbox(str(selected).strip() if selected is not None else None)


def _resolve_codex_mcp_server_names(config: dict, role: str) -> list[str]:
    permissions = config.get("mcp_permissions")
    if not isinstance(permissions, dict):
        return []
    raw_names = permissions.get(role)
    if raw_names is None:
        raw_names = permissions.get("*") or permissions.get("default")
    if not isinstance(raw_names, (list, tuple, set)):
        return []
    names: list[str] = []
    for item in raw_names:
        name = str(item or "").strip()
        if not name:
            continue
        if name.startswith("mcp__"):
            name = name.removeprefix("mcp__")
        if name not in names:
            names.append(name)
    return names


def _shorten_action_text(text: object, *, limit: int = 120) -> str:
    collapsed = " ".join(str(text or "").split())
    if not collapsed:
        return ""
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "..."


def _extract_codex_item_action(item: dict, *, completed: bool) -> str:
    item_type = str(item.get("type") or "")
    if item_type == "agent_message":
        text = _shorten_action_text(item.get("text"))
        return f"💬 {text}" if text else ""
    if item_type == "mcp_tool_call":
        server = str(item.get("server") or item.get("server_name") or "").strip()
        tool = str(item.get("tool") or item.get("name") or "").strip()
        if server and tool:
            return f"🔧 {server}.{tool}"
        return f"🔧 {tool or server or 'MCP 工具调用'}"
    if item_type in {"function_call", "custom_tool_call"}:
        name = str(item.get("name") or item.get("tool_name") or item_type)
        return f"🔧 {name}"
    if item_type == "command_execution":
        command = item.get("command") or item.get("cmd") or item.get("input") or ""
        text = _shorten_action_text(command, limit=100)
        return f"🖥️ {text}" if text else "🖥️ 执行命令"
    if item_type == "web_search_call":
        action = item.get("action", {}) if isinstance(item.get("action"), dict) else {}
        query = _shorten_action_text(action.get("query"), limit=80)
        return f"WebSearch: {query}" if query else "WebSearch"
    if item_type == "reasoning":
        return "思考中"
    if completed:
        return ""
    return _shorten_action_text(item_type)


def _extract_codex_action(event: dict) -> str:
    event_type = str(event.get("type") or "")
    if event_type == "thread.started":
        return "🚀 Codex 线程已启动"
    if event_type == "turn.started":
        return "🧠 Codex 开始执行"
    if event_type in {"item.started", "item.completed"}:
        item = event.get("item", {}) if isinstance(event.get("item"), dict) else {}
        return _extract_codex_item_action(item, completed=event_type == "item.completed")
    if event_type == "response_item":
        payload = event.get("payload", {}) if isinstance(event.get("payload"), dict) else {}
        payload_type = str(payload.get("type") or "")
        if payload_type in {"function_call", "custom_tool_call"}:
            return f"{payload.get('name', payload_type)}"
        if payload_type == "web_search_call":
            action = payload.get("action", {}) if isinstance(payload.get("action"), dict) else {}
            return f"WebSearch: {str(action.get('query') or '')[:60]}"
        if payload_type == "reasoning":
            return "思考中"
    if event_type in {"error", "turn.failed"}:
        return "⚠️ Codex 执行异常"
    return ""


def _extract_codex_thread_id(events: list[dict]) -> str:
    for event in events:
        if event.get("type") == "thread.started":
            thread_id = str(event.get("thread_id") or "")
            if thread_id:
                return thread_id
    return ""


def _extract_codex_message_text(events: list[dict]) -> str:
    parts: list[str] = []
    for event in events:
        event_type = event.get("type")
        if event_type == "item.completed":
            item = event.get("item", {}) if isinstance(event.get("item"), dict) else {}
            if item.get("type") == "agent_message":
                text = str(item.get("text") or "")
                if text:
                    parts.append(text)
            continue
        if event_type != "response_item":
            continue
        payload = event.get("payload", {}) if isinstance(event.get("payload"), dict) else {}
        if payload.get("type") != "message":
            continue
        for block in payload.get("content", []) if isinstance(payload.get("content"), list) else []:
            text = str(block.get("text") or "")
            if text:
                parts.append(text)
    return "\n".join(part for part in parts if part).strip()


def _extract_codex_usage(events: list[dict]) -> tuple[int, int, float]:
    for event in reversed(events):
        usage = _coerce_usage(event)
        if usage is None:
            continue
        input_tokens = int(usage.get("input_tokens") or usage.get("inputTokens") or 0)
        output_tokens = int(usage.get("output_tokens") or usage.get("outputTokens") or 0)
        cost_usd = float(
            usage.get("cost_usd")
            or usage.get("costUSD")
            or usage.get("total_cost_usd")
            or 0.0
        )
        return input_tokens, output_tokens, cost_usd
    return 0, 0, 0.0


def _coerce_usage(event: dict) -> dict | None:
    direct_usage = event.get("usage")
    if isinstance(direct_usage, dict):
        return direct_usage
    payload = event.get("payload", {}) if isinstance(event.get("payload"), dict) else {}
    payload_usage = payload.get("usage")
    if isinstance(payload_usage, dict):
        return payload_usage
    response = event.get("response", {}) if isinstance(event.get("response"), dict) else {}
    response_usage = response.get("usage")
    if isinstance(response_usage, dict):
        return response_usage
    return None


def _read_last_message(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _extract_codex_error_message(stderr_str: str, events: list[dict], stdout_raw: str) -> str:
    messages: list[str] = []
    for event in events:
        if event.get("type") == "error":
            messages.append(str(event.get("message") or ""))
        elif event.get("type") == "turn.failed":
            error_payload = event.get("error", {}) if isinstance(event.get("error"), dict) else {}
            messages.append(str(error_payload.get("message") or ""))
    combined = " ".join(part for part in [stderr_str, " ".join(messages), stdout_raw[:500]] if part)
    return combined.strip() or "unknown codex error"


def _is_codex_rate_limited(message: str) -> bool:
    lowered = str(message or "").lower()
    return any(
        keyword in lowered
        for keyword in (
            "rate limit",
            "429",
            "overloaded",
            "too many requests",
            "limit reached",
            "capacity",
        )
    )


def _is_codex_cloud_requirements_timeout(message: str) -> bool:
    lowered = str(message or "").lower()
    return (
        "timed out waiting for cloud requirements" in lowered
        or "timed out waiting for cloud requirement" in lowered
    )


def _record_partial_cost(agent_runner, *, role: str, model: str, task_dir: Path | None, output_file, duration: float, events: list[dict]) -> None:
    if task_dir is None or not events:
        return
    input_tokens, output_tokens, cost_usd = _extract_codex_usage(events)
    if input_tokens <= 0 and output_tokens <= 0 and cost_usd <= 0:
        return
    agent_runner._record_cost(
        role,
        model,
        input_tokens,
        output_tokens,
        cost_usd,
        task_dir,
        duration=duration,
        output_file=str(output_file) if output_file else "",
    )
