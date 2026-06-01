#!/usr/bin/env python3
"""Agent 执行器：启动 claude -p 子进程并管理生命周期"""

import asyncio
import json
import hashlib
import os
import re
import signal
import subprocess
import time
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from lib.mcp_runtime import is_chrome_bridge_connected, is_mcp_service_enabled, normalize_mcp_servers
from lib.project_memory_whitelist import (
    WHITELIST_MEMORY_NAME,
    load_project_memory_whitelist,
    resolve_project_root_by_name,
    resolve_required_memories,
)
from lib.startup_protocol import build_role_startup_instructions, resolve_startup_protocol_context

logger = logging.getLogger(__name__)

MAIN_SESSION_MODEL_ID = "__main_session__"
MAIN_SESSION_MODEL_PREFIX = "__main_model__:"
LEGACY_ROLE_MODEL_IDS = {"opus", "sonnet", "haiku"}
VALID_REASONING_EFFORTS = {"inherit", "low", "medium", "high", "xhigh"}


def _is_codex_reconnect_exhausted_event(event: dict) -> bool:
    """Detect Codex CLI reconnect loops that have already exhausted retries."""
    if not isinstance(event, dict) or event.get("type") != "error":
        return False
    message = str(event.get("message") or "")
    lowered = message.lower()
    if "reconnecting" not in lowered or "timeout waiting for child process to exit" not in lowered:
        return False
    match = re.search(r"reconnecting\.\.\.\s*(\d+)\s*/\s*(\d+)", message, re.IGNORECASE)
    if not match:
        return "5/5" in message
    try:
        current = int(match.group(1))
        total = int(match.group(2))
    except ValueError:
        return False
    return total > 0 and current >= total


@dataclass
class AgentResult:
    """Agent 执行结果"""
    success: bool
    data: dict
    raw_output: str
    cost_tokens: int
    duration: float
    exit_code: int
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    changes: list = field(default_factory=list)
    timeout_type: Optional[str] = None
    events_count: int = 0
    last_event_time: float = 0.0


class AgentTimeoutError(Exception):
    def __init__(self, message: str, error_code: str = "E101", timeout_type: str = "idle",
                 events: list = None, cost_usd: float = 0.0):
        super().__init__(message)
        self.error_code = error_code
        self.timeout_type = timeout_type
        self.events = events or []
        self.cost_usd = cost_usd


class AgentError(Exception):
    def __init__(self, message: str, error_code: str = "E301", cost_usd: float = 0.0):
        super().__init__(message)
        self.error_code = error_code
        self.cost_usd = cost_usd


class AgentRateLimitError(Exception):
    def __init__(self, message: str, error_code: str = "E201", cost_usd: float = 0.0):
        super().__init__(message)
        self.error_code = error_code
        self.cost_usd = cost_usd


class AgentSignalInterrupt(Exception):
    """子代理被控制信号中断（暂停/终止/回滚），可在执行过程中秒级响应"""
    def __init__(self, message: str, signal: dict = None, cost_usd: float = 0.0,
                 events: list = None):
        super().__init__(message)
        self.signal = signal or {}
        self.action = self.signal.get("action", "unknown")
        self.cost_usd = cost_usd
        self.events = events


class AgentRunner:
    """Agent 启动器"""

    ROLE_MEMORY_WHITELIST = WHITELIST_MEMORY_NAME

    DEFAULT_MODELS = {
        "architect": MAIN_SESSION_MODEL_ID,
        "backend_developer": MAIN_SESSION_MODEL_ID,
        "frontend_developer": MAIN_SESSION_MODEL_ID,
        "fix_engineer": MAIN_SESSION_MODEL_ID,
        "embedded_engineer": MAIN_SESSION_MODEL_ID,
        "requirement_analyst": MAIN_SESSION_MODEL_ID,
        "product_manager": MAIN_SESSION_MODEL_ID,
        "project_manager": MAIN_SESSION_MODEL_ID,
        "qa_engineer": MAIN_SESSION_MODEL_ID,
        "integration_engineer": MAIN_SESSION_MODEL_ID,
        "devops_engineer": MAIN_SESSION_MODEL_ID,
        "interaction_designer": MAIN_SESSION_MODEL_ID,
        "technical_assessor": MAIN_SESSION_MODEL_ID,
        "code_explorer": MAIN_SESSION_MODEL_ID,
        "assistant": MAIN_SESSION_MODEL_ID,
        "knowledge_engineer": MAIN_SESSION_MODEL_ID,
        "knowledge_admin": MAIN_SESSION_MODEL_ID,
        "handoff_extractor": MAIN_SESSION_MODEL_ID,
        "manual_updater": MAIN_SESSION_MODEL_ID,
        "merge_resolver": MAIN_SESSION_MODEL_ID,
    }

    FALLBACK_MODELS = {
        "opus": "sonnet",
        "glm-5": "sonnet",   # GLM-5 失败 → 降级到 Anthropic Sonnet
        "sonnet": "haiku",
        "haiku": None,
    }

    DEFAULT_TIMEOUTS = {
        role: 900 for role in DEFAULT_MODELS
    }

    ALLOWED_TOOLS = {
        "requirement_analyst": ["Read", "Glob", "Grep", "WebFetch", "WebSearch", "mcp__serena"],
        "product_manager": ["Read", "Glob", "Grep", "WebFetch", "WebSearch", "mcp__serena"],
        "architect": ["Read", "Glob", "Grep", "WebFetch", "mcp__serena"],
        "backend_developer": ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "mcp__serena"],
        "frontend_developer": ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "mcp__serena"],
        "interaction_designer": ["Read", "Glob", "Grep", "mcp__serena"],
        "embedded_engineer": ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "mcp__serena"],
        "qa_engineer": ["Read", "Bash", "Glob", "Grep"],
        "integration_engineer": ["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        "fix_engineer": ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "mcp__serena"],
        "devops_engineer": ["Read", "Bash"],
        "knowledge_engineer": ["Read", "Write"],
        "knowledge_admin": ["Read", "Write", "mcp__serena"],
        "code_explorer": ["Read", "Glob", "Grep", "mcp__serena"],
        "assistant": ["Read", "mcp__serena"],
        "project_manager": ["Read", "Glob", "Grep"],
        "handoff_extractor": ["Read", "Glob", "Grep"],
        "manual_updater": ["Read", "Write", "Glob"],
        "merge_resolver": ["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        "technical_assessor": ["Read", "Glob", "Grep", "mcp__serena"],
    }

    MODEL_PRICING = {
        "opus": {"input": 5.0, "output": 25.0},
        "sonnet": {"input": 3.0, "output": 15.0},
        "glm-5": {"input": 2.0, "output": 10.0},
        "haiku": {"input": 1.0, "output": 5.0},
        "minimax-m2-5": {"input": 0.7, "output": 2.8},
        "deepseek-chat": {"input": 0.27, "output": 1.10},
        "qwen3-max": {"input": 1.6, "output": 6.4},
    }

    # 不需要工具的纯文本角色（限流降级时可切换外部模型）
    TEXT_ONLY_ROLES = {
        "requirement_analyst", "product_manager", "architect",
        "knowledge_engineer", "assistant", "code_explorer", "project_manager",
        "interaction_designer",
        "handoff_extractor", "manual_updater",
    }

    # 外部备选模型（限流时降级使用）
    EXTERNAL_MODELS = {
        "deepseek": {"label": "DeepSeek V3", "model": "deepseek-chat"},
    }

    RATE_LIMIT_MAX_RETRIES = 3

    DOC_TEMPLATE_PATTERN = re.compile(r'role_templates/docs/(\S+\.md)')

    # （已废弃：asyncio.Lock 无法跨进程互斥，改用 lib.settings_lock 文件锁）

    def __init__(self, config: dict):
        self.config = config
        self.project_path = Path(
            config.get("projects", {})
            .get(config.get("default_project", ""), {})
            .get("path", ".")
        )
        # 动作回调（由 Orchestrator 设置，用于更新进度显示）
        self._on_action_callback = None
        # 流式事件回调（由 Orchestrator 设置，用于 StreamRenderer）
        self._on_stream_event_callback = None

    # 磁盘备份路径：进程崩溃后下次启动可从此文件恢复 settings.json
    _SETTINGS_BACKUP_PATH = Path.home() / ".claude" / "settings.json.opus_backup"

    @staticmethod
    def _restore_settings(path: Path, backup: str | None):
        """恢复 ~/.claude/settings.json 到备份内容（或删除若原来不存在）"""
        try:
            if backup is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(backup, encoding="utf-8")
        except Exception as e:
            logger.warning(f"恢复 settings.json 失败: {e}")
        # 清理磁盘备份
        try:
            AgentRunner._SETTINGS_BACKUP_PATH.unlink(missing_ok=True)
        except Exception:
            pass

    @staticmethod
    def _recover_settings_if_needed():
        """启动时检查：如果上次进程崩溃导致 settings.json 未恢复，从磁盘备份恢复"""
        backup_path = AgentRunner._SETTINGS_BACKUP_PATH
        if not backup_path.exists():
            return
        # 获取跨进程文件锁，防止恢复时与其他进程的写入冲突
        from lib.settings_lock import acquire_sync, release as release_lock
        lock_fd = None
        try:
            lock_fd = acquire_sync(timeout=5)
        except TimeoutError:
            logger.warning("恢复 settings.json 时获取文件锁超时，跳过恢复")
            return
        try:
            settings_path = Path.home() / ".claude" / "settings.json"
            backup_content = backup_path.read_text(encoding="utf-8")
            # 验证 JSON 合法性
            json.loads(backup_content)
            settings_path.write_text(backup_content, encoding="utf-8")
            backup_path.unlink(missing_ok=True)
            logger.warning("检测到上次 settings.json 未恢复（进程可能异常退出），已从磁盘备份恢复")
        except Exception as e:
            logger.error(f"从磁盘备份恢复 settings.json 失败: {e}")
            # 备份文件损坏，删除避免反复触发
            backup_path.unlink(missing_ok=True)
        finally:
            release_lock(lock_fd)

    @staticmethod
    def _load_main_session_profile() -> dict:
        """读取启动后台任务的主会话模型快照。"""
        candidates: list[Path] = []
        explicit_dir = str(os.environ.get("VIZO_MAIN_SESSION_DIR") or "").strip()
        if explicit_dir:
            candidates.append(Path(explicit_dir) / "session.json")
        session_id = str(os.environ.get("VIZO_MAIN_SESSION_ID") or "").strip()
        project_root = Path(str(os.environ.get("VIZO_PROJECT_ROOT") or ".")).resolve()
        if session_id:
            candidates.append(project_root / ".vizo" / "sessions" / "main" / session_id / "session.json")
        runtime_dir = str(os.environ.get("VIZO_MAIN_SESSION_RUNTIME_DIR") or "").strip()
        if runtime_dir:
            candidates.append(Path(runtime_dir).resolve().parent / "session.json")

        for path in candidates:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict):
                metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
                reasoning = str(metadata.get("reasoning_effort") or "").strip()
                return {
                    "session_id": data.get("session_id", ""),
                    "display_model": data.get("display_model", ""),
                    "provider_model": data.get("provider_model", ""),
                    "runtime_family": data.get("runtime_family", ""),
                    "reasoning_effort": reasoning,
                }
        return {}

    @staticmethod
    def _unwrap_main_session_model_choice(model: str) -> str:
        model = str(model or "").strip()
        if model.startswith(MAIN_SESSION_MODEL_PREFIX):
            return model[len(MAIN_SESSION_MODEL_PREFIX):].strip()
        return ""

    @staticmethod
    def _claude_cli_alias_for_main_model(model: str) -> str:
        model = str(model or "").strip()
        if model in {"opus", "sonnet", "haiku"}:
            return model
        if model.startswith("claude-opus-"):
            return "opus"
        if model.startswith("claude-sonnet-"):
            return "sonnet"
        if model.startswith("claude-haiku-"):
            return "haiku"
        return ""

    def _resolve_default_main_session_model(self, prefer: str | None = None) -> str:
        try:
            from lib.settings_handler import MAIN_SESSION_DEFAULT_TIER, get_main_session_api_model

            prefer = prefer if prefer in LEGACY_ROLE_MODEL_IDS else MAIN_SESSION_DEFAULT_TIER
            main_api = self.config.get("external_models", {}).get("anthropic", {}) or {}
            return get_main_session_api_model(
                main_api.get("base_url", ""),
                current_env=main_api.get("env", {}),
                prefer=prefer,
                stored_provider_id=main_api.get("provider_id"),
            )
        except Exception:
            return "sonnet"

    def _resolve_role_reasoning_effort(
        self,
        role: str,
        *,
        override: str | None = None,
        main_session_profile: dict | None = None,
    ) -> str:
        effort = str(override or "").strip()
        if not effort:
            effort = str((self.config.get("role_reasoning_efforts", {}) or {}).get(role) or "inherit").strip()
        if effort not in VALID_REASONING_EFFORTS:
            effort = "inherit"
        if effort == "inherit":
            inherited = str((main_session_profile or self._load_main_session_profile()).get("reasoning_effort") or "").strip()
            return inherited if inherited in (VALID_REASONING_EFFORTS - {"inherit"}) else "medium"
        return effort

    async def run(self, role, task_dir, input_docs, memories=None,
                  output_file=None, project=None, cwd=None, timeout=None,
                  _retry_count=0, _downgraded_model=None,
                  session_recall=None, work_state_summary=None,
                  on_action=None, on_stream_event=None, resume_session=None,
                  task_id=None, on_paused=None, on_resumed=None,
                  parent_task_id=None,       # 子任务执行时的父任务 ID（轮询父信号用）
                  model_override=None,       # AgentHub: 直接指定模型（最高优先级）
                  reasoning_effort_override=None,  # AgentHub: 角色思考深度（inherit/low/medium/high/xhigh）
                  tools_override=None,       # AgentHub: 直接指定工具列表
                  template_override=None,    # AgentHub: 角色模板路径覆盖
                  prompt_override=None,      # AgentHub: 直接传入完整 prompt，跳过 _build_prompt()
                  ):
        """启动 claude -p 子进程执行 Agent 任务"""
        # 0. 热加载配置（任务运行中用户可能修改了模型/API 配置）
        from lib.config_loader import load_config as _reload_config
        self.config = _reload_config(force_reload=True)

        # 1. 构建 Prompt（prompt_override 由 AgentHub 直接提供，跳过 _build_prompt()）
        if prompt_override is not None:
            prompt = prompt_override
        else:
            prompt = await self._build_prompt(role, task_dir, input_docs, memories, output_file, project,
                                              session_recall=session_recall, work_state_summary=work_state_summary,
                                              template_override=template_override)

        # 2. 检查 prompt 大小
        estimated_tokens = self._estimate_tokens(prompt)
        if estimated_tokens > 150_000:
            logger.info(f"Prompt 较大（{estimated_tokens} tokens）")

        # 3. 确定模型（支持限流时降级覆盖）
        uses_main_session_connection = False
        selected_main_provider_model = ""
        preferred_main_tier = ""
        main_session_profile: dict = {}
        if _downgraded_model:
            model = _downgraded_model
        elif model_override:
            model = model_override
        else:
            model = (self.config.get("model_overrides", {}).get(role)
                     or self.DEFAULT_MODELS.get(role, MAIN_SESSION_MODEL_ID))
        if model == MAIN_SESSION_MODEL_ID:
            uses_main_session_connection = True
            main_session_profile = self._load_main_session_profile()
            selected_main_provider_model = (
                str(main_session_profile.get("provider_model") or "").strip()
                or str(main_session_profile.get("display_model") or "").strip()
                or self._resolve_default_main_session_model()
            )
            model = selected_main_provider_model
        else:
            explicit_main_model = self._unwrap_main_session_model_choice(model)
            if explicit_main_model:
                uses_main_session_connection = True
                main_session_profile = self._load_main_session_profile()
                selected_main_provider_model = explicit_main_model
                preferred_main_tier = self._claude_cli_alias_for_main_model(explicit_main_model)
                model = selected_main_provider_model
            elif model in LEGACY_ROLE_MODEL_IDS:
                # 兼容旧配置：旧 tier 不再是角色可选项，但已保存配置仍按主会话映射解析。
                uses_main_session_connection = True
                main_session_profile = self._load_main_session_profile()
                preferred_main_tier = model
                selected_main_provider_model = self._resolve_default_main_session_model(prefer=model)
                model = selected_main_provider_model
        reasoning_effort = self._resolve_role_reasoning_effort(
            role,
            override=reasoning_effort_override,
            main_session_profile=main_session_profile,
        )
        fallback = self.FALLBACK_MODELS.get(model)
        idle_timeout = self.config.get("idle_timeout", {}).get(role, 900)
        max_timeout = 3600

        # 3.5 外部模型解析：提取 CLI 模型名和额外环境变量
        ext_config = None if uses_main_session_connection else self.config.get("external_models", {}).get(model)
        if ext_config:
            from lib.settings_handler import (
                OPENAPI_EXTERNAL_MODEL_UNSUPPORTED_MESSAGE,
                infer_external_model_metadata,
            )

            meta = infer_external_model_metadata(model, ext_config)
            if meta.get("access_mode") == "openai_compatible":
                raise AgentError(
                    OPENAPI_EXTERNAL_MODEL_UNSUPPORTED_MESSAGE,
                    error_code="runtime_blocked",
                )
            cli_model = ext_config["cli_model"]
            extra_env = dict(ext_config.get("env", {}))  # 复制避免污染 config 缓存
            # API key：同时设置 ANTHROPIC_API_KEY 和 ANTHROPIC_AUTH_TOKEN（ZhipuAI 等需要后者）
            # 优先级：config env → config top-level api_key → .env 预置模型 key
            api_key_val = extra_env.get("ANTHROPIC_API_KEY") or ext_config.get("api_key", "")
            if not api_key_val:
                from lib.settings_handler import _read_api_key_for_model
                api_key_val = _read_api_key_for_model(model, ext_config)
            if api_key_val:
                extra_env["ANTHROPIC_API_KEY"] = api_key_val
                extra_env["ANTHROPIC_AUTH_TOKEN"] = api_key_val
            from lib.settings_handler import get_external_model_runtime_base_url
            base_url_val = get_external_model_runtime_base_url(model, ext_config)
            if base_url_val:
                extra_env["ANTHROPIC_BASE_URL"] = base_url_val
            # 非 claude-* 模型名：直接传给 --model，同时设置 ANTHROPIC_MODEL 供 Claude Code 读取
            # 不做 claude-sonnet-4-5 映射——Qwen/GLM 直接接收真实模型名（如 qwen3-max-2026-01-23）
            if cli_model and not cli_model.startswith("claude-"):
                extra_env["ANTHROPIC_MODEL"] = cli_model
            if cli_model:
                extra_env["CLAUDE_CODE_SUBAGENT_MODEL"] = cli_model
            # 禁用向 Anthropic 官方发出的遥测/认证请求，避免第三方端点下 521 错误
            extra_env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
            extra_env.setdefault("API_TIMEOUT_MS", "600000")
            logger.info(f"Agent [{role}] 使用外部模型 {ext_config.get('label', model)}")
        else:
            cli_model = (
                self._claude_cli_alias_for_main_model(selected_main_provider_model)
                if uses_main_session_connection
                else ""
            ) or model
            # 显式读取默认 API 配置，不依赖 settings.json（避免并发污染）
            _main_api = self.config.get("external_models", {}).get("anthropic", {})
            extra_env = dict(_main_api.get("env", {}))
            _main_key = _main_api.get("api_key", "")
            # fallback 从 .env 文件读取（不从 os.environ，避免继承旧值）
            if not _main_key:
                from lib.config_loader import read_main_session_api_key_from_env_file
                _main_key = read_main_session_api_key_from_env_file()
            _main_url = _main_api.get("base_url", "")
            if _main_key:
                extra_env["ANTHROPIC_API_KEY"] = _main_key
                extra_env["ANTHROPIC_AUTH_TOKEN"] = _main_key
            try:
                from lib.settings_handler import resolve_main_session_connection
                _main_resolved = resolve_main_session_connection(
                    _main_url,
                    current_env=extra_env,
                    stored_provider_id=_main_api.get("provider_id"),
                )
            except Exception:
                _main_resolved = {"runtime_base_url": _main_url}
            runtime_base_url = _main_resolved.get("runtime_base_url", _main_url)
            if runtime_base_url and runtime_base_url != "https://api.anthropic.com":
                extra_env["ANTHROPIC_BASE_URL"] = runtime_base_url
                extra_env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
            try:
                from lib.settings_handler import get_main_session_api_model
                if uses_main_session_connection:
                    provider_model = selected_main_provider_model or get_main_session_api_model(
                        _main_url,
                        current_env=extra_env,
                        prefer=preferred_main_tier or "sonnet",
                        stored_provider_id=_main_api.get("provider_id"),
                    )
                    if provider_model:
                        extra_env["CLAUDE_CODE_SUBAGENT_MODEL"] = provider_model
                        if not provider_model.startswith("claude-"):
                            extra_env["ANTHROPIC_MODEL"] = provider_model
                else:
                    extra_env["CLAUDE_CODE_SUBAGENT_MODEL"] = get_main_session_api_model(
                        _main_url,
                        current_env=extra_env,
                        prefer=model,
                        stored_provider_id=_main_api.get("provider_id"),
                    )
            except Exception:
                pass
            if extra_env:
                logger.info(f"Agent [{role}] 使用默认 API (base_url={_main_url[:40] if _main_url else 'default'})")
        extra_env["VIZO_ROLE_REASONING_EFFORT"] = reasoning_effort

        # 4. 构建命令
        cmd = ["claude", "-p", "-",
               "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
        if resume_session:
            cmd.extend(["--resume", resume_session])
            # resume 时不指定 model/fallback，沿用上一轮会话配置
        else:
            cmd.extend(["--model", cli_model])
            if not ext_config and fallback:  # 外部模型不传 --fallback-model
                cmd.extend(["--fallback-model", fallback])
        # tools_override 完全替代默认工具查找（AgentHub 模块自带工具列表）
        if tools_override:
            allowed_tools = list(tools_override)
        else:
            allowed_tools = list(self.ALLOWED_TOOLS.get(role) or [])
        # QA 工程师：仅涉及前端的任务才启用 Chrome DevTools MCP
        if role == "qa_engineer" and input_docs:
            _fe_keywords = ["frontend", "前端", "web", "页面", "ui", "css", "html",
                            "web_console", "浏览器", "界面", "样式", "布局"]
            _check_text = ""
            for k, v in (input_docs.items() if isinstance(input_docs, dict) else []):
                _check_text += f" {k} {v}" if isinstance(v, str) and len(v) < 500 else f" {k}"
                # 如果值是文件路径，读取文件内容的前 500 字符检查
                if isinstance(v, str) and len(v) < 200 and "\n" not in v and task_dir:
                    fp = Path(task_dir) / v
                    if fp.exists():
                        try:
                            _check_text += " " + fp.read_text(encoding="utf-8")[:500]
                        except Exception:
                            pass
            if any(kw in _check_text.lower() for kw in _fe_keywords):
                allowed_tools.append("mcp__mcp-chrome")
                logger.info("QA 任务涉及前端，已启用 Chrome MCP")
            else:
                logger.info("QA 任务不涉及前端，跳过 Chrome MCP")
        # 应用 config.json 中的 MCP 权限覆盖（优先级高于代码默认值和 QA 条件逻辑）
        mcp_permissions = self.config.get("mcp_permissions", {})
        if role in mcp_permissions:
            allowed_tools = [t for t in allowed_tools if not t.startswith("mcp__")]
            for mcp_name in mcp_permissions[role]:
                allowed_tools.append(f"mcp__{mcp_name}")
        allowed_tools = [
            t for t in allowed_tools
            if not t.startswith("mcp__") or is_mcp_service_enabled(t.removeprefix("mcp__"), self.config)
        ]
        if allowed_tools:
            cmd.extend(["--allowedTools", ",".join(allowed_tools)])

        # 按角色 MCP 权限构建精简 MCP 配置，避免加载不需要的 MCP server 导致启动卡死
        role_mcp_names = mcp_permissions.get(role, [])
        role_mcp_names = [m for m in role_mcp_names if is_mcp_service_enabled(m, self.config)]

        # Chrome Bridge 动态感知：未连接时移除 mcp-chrome，避免子代理超时
        if "mcp-chrome" in role_mcp_names:
            chrome_ok = await self._check_chrome_bridge_health()
            if not chrome_ok:
                role_mcp_names = [m for m in role_mcp_names if m != "mcp-chrome"]
                # 同时从 allowed_tools 中移除
                allowed_tools = [t for t in allowed_tools if t != "mcp__mcp-chrome"]
                if allowed_tools:
                    # 重新构建 --allowedTools（前面已 extend 过，需替换）
                    for i, a in enumerate(cmd):
                        if a == "--allowedTools" and i + 1 < len(cmd):
                            cmd[i + 1] = ",".join(allowed_tools)
                            break
                logger.info(f"Agent [{role}] Chrome 未连接，已移除 mcp-chrome")
            else:
                logger.info(f"Agent [{role}] Chrome 已连接，加载 mcp-chrome")
        mcp_json_path = self._get_project_path(project or self.config.get("default_project", ""))
        mcp_json_file = Path(mcp_json_path) / ".mcp.json" if mcp_json_path else None
        confirm_port = int(self.config.get("confirm_server", {}).get("port", 9390))
        if mcp_json_file and mcp_json_file.exists():
            try:
                if role_mcp_names:
                    # 角色有明确的 MCP 配置：只加载指定的 server
                    all_mcp = json.loads(mcp_json_file.read_text("utf-8")).get("mcpServers", {})
                    role_mcp = {name: all_mcp[name] for name in role_mcp_names if name in all_mcp}
                    role_mcp = normalize_mcp_servers(role_mcp, confirm_port=confirm_port, config_data=self.config)
                    role_mcp_config = json.dumps({"mcpServers": role_mcp})
                    cmd.extend(["--mcp-config", role_mcp_config, "--strict-mcp-config"])
                    logger.info(f"Agent [{role}] MCP 精简加载: {list(role_mcp.keys())}")
                else:
                    # 角色无 MCP 配置：默认只加载 serena（防止加载全部 server 导致卡死）
                    all_mcp = json.loads(mcp_json_file.read_text("utf-8")).get("mcpServers", {})
                    default_mcp = {k: v for k, v in all_mcp.items() if k == "serena"} if "serena" in all_mcp else {}
                    default_mcp = normalize_mcp_servers(default_mcp, confirm_port=confirm_port, config_data=self.config)
                    role_mcp_config = json.dumps({"mcpServers": default_mcp})
                    cmd.extend(["--mcp-config", role_mcp_config, "--strict-mcp-config"])
                    logger.info(f"Agent [{role}] 无 MCP 配置，降级加载: {list(default_mcp.keys())}")
            except Exception as e:
                logger.warning(f"构建角色 MCP 配置失败: {e}，降级为仅加载 serena")
                # 降级为仅 serena，而非不传 --mcp-config（那会导致全量加载卡死）
                try:
                    all_mcp = json.loads(mcp_json_file.read_text("utf-8")).get("mcpServers", {})
                    fallback_mcp = {k: v for k, v in all_mcp.items() if k == "serena"} if "serena" in all_mcp else {}
                    fallback_mcp = normalize_mcp_servers(fallback_mcp, confirm_port=confirm_port, config_data=self.config)
                    cmd.extend(["--mcp-config", json.dumps({"mcpServers": fallback_mcp}), "--strict-mcp-config"])
                except Exception:
                    cmd.extend(["--mcp-config", json.dumps({"mcpServers": {}}), "--strict-mcp-config"])

        # 预算限制（已移除：opus 模型读文件即烧完 $2 预算，导致开发者没写代码就被终止）
        # max_budget = self.config.get("agent_max_budget_usd", {}).get(role, 2.0)
        # cmd.extend(["--max-budget-usd", str(max_budget)])

        work_dir = cwd or self._get_project_path(project or self.config.get("default_project", ""))

        # 5. 执行前记录 Git 状态
        git_before = await self._git_status(work_dir)

        # 6. 执行 Agent
        logger.info(
            f"启动 Agent [{role}] model={model} reasoning={reasoning_effort} "
            f"idle_timeout={idle_timeout}s max_timeout={max_timeout}s tokens≈{estimated_tokens}"
        )
        start_time = time.time()

        # 实时日志文件
        live_log = None
        parent_live_log = None
        if task_dir:
            task_dir_path = Path(task_dir)
            live_log_dir = task_dir_path / "logs"
            live_log_dir.mkdir(parents=True, exist_ok=True)
            live_log = str(live_log_dir / "live.log")
            # 子任务日志同步到父任务 live.log（浏览器终端监控用）
            if "/sub-" in str(task_dir_path):
                parent_dir = task_dir_path.parent  # sub-N 的父目录是主任务目录
                parent_log_dir = parent_dir / "logs"
                if parent_log_dir.exists():
                    parent_live_log = str(parent_log_dir / "live.log")
            # 写入角色启动标记
            startup_line = (
                f"\n{'='*60}\n"
                f"[{time.strftime('%H:%M:%S')}] 🤖 {role} ({model} · {reasoning_effort}) 启动\n"
                f"{'='*60}\n"
            )
            with open(live_log, "a", encoding="utf-8") as lf:
                lf.write(startup_line)
            if parent_live_log:
                try:
                    with open(parent_live_log, "a", encoding="utf-8") as lf:
                        lf.write(startup_line)
                except Exception:
                    pass

        try:
            stdout_raw, stderr_str, returncode, events = await self._run_subprocess_streaming(
                cmd, prompt, str(work_dir), idle_timeout, max_timeout,
                live_log_file=live_log,
                parent_live_log_file=parent_live_log,
                on_action=on_action or self._on_action_callback,
                on_stream_event=on_stream_event or self._on_stream_event_callback,
                task_id=task_id,
                parent_task_id=parent_task_id,
                on_paused=on_paused,
                on_resumed=on_resumed,
                role=role,
                extra_env=extra_env,
            )
        except AgentTimeoutError as e:
            # 超时也要记录已消耗的费用
            duration = time.time() - start_time
            task_dir_path = Path(task_dir) if task_dir else None
            if e.events and task_dir_path:
                in_tok, out_tok, cost = self._extract_cost_from_events(e.events, model)
                if in_tok > 0 or out_tok > 0 or cost > 0:
                    self._record_cost(role, model, in_tok, out_tok, cost, task_dir_path,
                                      duration=duration, output_file=str(output_file) if output_file else "")
                    e.cost_usd = cost
                    logger.info(f"Agent [{role}] 超时前费用已记录: ${cost:.4f} ({in_tok}+{out_tok} tokens)")
            raise
        except AgentSignalInterrupt as e:
            # 信号中断也要记录已消耗的费用（终止/回滚时进程被 kill）
            duration = time.time() - start_time
            task_dir_path = Path(task_dir) if task_dir else None
            if e.events and task_dir_path:
                in_tok, out_tok, cost = self._extract_cost_from_events(e.events, model)
                if in_tok > 0 or out_tok > 0 or cost > 0:
                    self._record_cost(role, model, in_tok, out_tok, cost, task_dir_path,
                                      duration=duration, output_file=str(output_file) if output_file else "")
                    e.cost_usd = cost
                    logger.info(f"Agent [{role}] 信号中断前费用已记录: ${cost:.4f} ({in_tok}+{out_tok} tokens)")
            raise

        duration = time.time() - start_time

        # 7. 检查退出码（区分限流和其他错误）
        if returncode != 0:
            combined_error = (stderr_str + " " + stdout_raw[:1000]).lower()
            logger.error(f"Agent [{role}] 退出码 {returncode}: {stderr_str[:500] or stdout_raw[:200]}")

            # 失败也要记录已消耗的费用
            task_dir_path = Path(task_dir) if task_dir else None
            if events and task_dir_path:
                in_tok, out_tok, cost = self._extract_cost_from_events(events, model)
                if in_tok > 0 or out_tok > 0 or cost > 0:
                    self._record_cost(role, model, in_tok, out_tok, cost, task_dir_path,
                                      duration=duration, output_file=str(output_file) if output_file else "")
                    logger.info(f"Agent [{role}] 失败前费用已记录: ${cost:.4f} ({in_tok}+{out_tok} tokens)")

            # 限流/过载检测（同时检查 stderr 和 stdout）
            if any(kw in combined_error for kw in [
                "rate limit", "429", "overloaded", "负载已经达到上限",
                "capacity", "too many requests", "500", "503",
                "service unavailable", "temporarily unavailable", "服务不可用",
            ]):
                error_code = "E202" if any(
                    kw in combined_error
                    for kw in [
                        "overloaded", "500", "503", "capacity",
                        "service unavailable", "temporarily unavailable", "服务不可用",
                    ]
                ) else "E201"

                if _retry_count < self.RATE_LIMIT_MAX_RETRIES:
                    # 优先立即降级到下一级模型（不等待）
                    next_model = self.FALLBACK_MODELS.get(model)
                    if next_model:
                        logger.warning(
                            f"Agent [{role}] 模型 {model} 被限流，"
                            f"立即降级到 {next_model}（{_retry_count + 1}/{self.RATE_LIMIT_MAX_RETRIES}）"
                        )
                        return await self.run(role, task_dir, input_docs, memories,
                                              output_file, project, cwd, timeout,
                                              _retry_count=_retry_count + 1,
                                              _downgraded_model=next_model,
                                              session_recall=session_recall,
                                              work_state_summary=work_state_summary,
                                              on_action=on_action,
                                              on_stream_event=on_stream_event,
                                              resume_session=resume_session,
                                              task_id=task_id,
                                              tools_override=tools_override,
                                              template_override=template_override,
                                              prompt_override=prompt_override,
                                              parent_task_id=parent_task_id)

                    else:
                        # 已是最低级模型，等待后重试
                        wait = 30 * (2 ** max(0, _retry_count - 1))
                        logger.warning(
                            f"Agent [{role}] 最低级模型 {model} 也被限流，"
                            f"等待 {wait}s 后重试（{_retry_count + 1}/{self.RATE_LIMIT_MAX_RETRIES}）"
                        )
                        await asyncio.sleep(wait)
                        return await self.run(role, task_dir, input_docs, memories,
                                              output_file, project, cwd, timeout,
                                              _retry_count=_retry_count + 1,
                                              _downgraded_model=_downgraded_model,
                                              session_recall=session_recall,
                                              work_state_summary=work_state_summary,
                                              on_action=on_action,
                                              on_stream_event=on_stream_event,
                                              resume_session=resume_session,
                                              task_id=task_id,
                                              tools_override=tools_override,
                                              template_override=template_override,
                                              prompt_override=prompt_override,
                                              parent_task_id=parent_task_id)


                raise AgentRateLimitError(
                    f"Agent [{role}] {self.RATE_LIMIT_MAX_RETRIES}次重试后仍被限流（链路: {model}）",
                    error_code=error_code
                )

            # 外部模型不可用 → 立即降级（不重试同一模型）
            if ext_config and any(kw in combined_error for kw in [
                "issue with the selected model", "may not exist",
                "not have access", "model not found", "invalid model",
            ]):
                next_model = self.FALLBACK_MODELS.get(model)
                if next_model:
                    logger.warning(
                        f"Agent [{role}] 外部模型 {model} 不可用，立即降级到 {next_model}"
                    )
                    return await self.run(role, task_dir, input_docs, memories,
                                          output_file, project, cwd, timeout,
                                          _retry_count=_retry_count,
                                          _downgraded_model=next_model,
                                          session_recall=session_recall,
                                          work_state_summary=work_state_summary,
                                          on_action=on_action,
                                          on_stream_event=on_stream_event,
                                          resume_session=resume_session,
                                          task_id=task_id,
                                          tools_override=tools_override,
                                          template_override=template_override,
                                          prompt_override=prompt_override)


            # 普通错误重试一次
            if _retry_count < 1:
                logger.info(f"Agent [{role}] 重试第 {_retry_count + 1} 次")
                return await self.run(role, task_dir, input_docs, memories,
                                      output_file, project, cwd, timeout,
                                      _retry_count=_retry_count + 1,
                                      _downgraded_model=_downgraded_model,
                                      session_recall=session_recall,
                                      work_state_summary=work_state_summary,
                                      on_action=on_action,
                                      on_stream_event=on_stream_event,
                                      resume_session=resume_session,
                                      task_id=task_id,
                                      tools_override=tools_override,
                                      template_override=template_override,
                                      prompt_override=prompt_override)

            # 提取已记录的费用（上面已 _record_cost）
            _err_cost = 0.0
            if events:
                _, _, _err_cost = self._extract_cost_from_events(events, model)
            raise AgentError(f"Agent [{role}] 执行失败: {stderr_str[:300] or stdout_raw[:300]}",
                             error_code="E301", cost_usd=_err_cost)

        # 8. 解析输出（优先从 events 提取，fallback 到 _parse_json_output）
        result_event = None
        for evt in reversed(events):
            if evt.get("type") == "result":
                result_event = evt
                break

        if result_event:
            data = result_event
            # 从 result 事件提取 agent 业务 JSON
            result_text = result_event.get("result", "")
            if isinstance(result_text, str) and result_text:
                agent_json = self._extract_agent_json(result_text)
                if agent_json and isinstance(agent_json, dict):
                    data["agent_data"] = agent_json
                    cli_keys = {"type", "subtype", "result", "is_error", "usage",
                                "modelUsage", "session_id", "total_cost_usd",
                                "duration_ms", "duration_api_ms", "num_turns",
                                "stop_reason", "agent_data", "permission_denials", "uuid"}
                    for k, v in agent_json.items():
                        if k not in cli_keys:
                            data[k] = v

            # 补充：如果最终 JSON 缺少 task_type/scale，从中间输出的分类 JSON 提取
            if "task_type" not in data or "scale" not in data:
                for evt in events:
                    if evt.get("type") == "assistant":
                        msg = evt.get("message", {})
                        content_blocks = msg.get("content", [])
                        for block in content_blocks:
                            if block.get("type") == "text":
                                mid_json = self._extract_agent_json(block.get("text", ""))
                                if mid_json and mid_json.get("status") == "classification":
                                    if "task_type" not in data and "task_type" in mid_json:
                                        data["task_type"] = mid_json["task_type"]
                                    if "scale" not in data and "scale" in mid_json:
                                        data["scale"] = mid_json["scale"]
                                    if "upgrade_needed" not in data and "upgrade_needed" in mid_json:
                                        data["upgrade_needed"] = mid_json["upgrade_needed"]
                                    break
                    if "task_type" in data and "scale" in data:
                        break
        else:
            # fallback：拼接 stdout 按旧方式解析
            data = self._parse_json_output(stdout_raw)

        # 9. 提取 token 用量
        input_tokens = 0
        output_tokens = 0
        cost_usd = 0.0
        if isinstance(data, dict):
            usage = data.get("usage", {})
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            model_usage = data.get("modelUsage", {})
            for model_info in model_usage.values():
                cost_usd += model_info.get("costUSD", 0.0)

        # 10. 记录费用
        task_dir_path = Path(task_dir) if task_dir else None
        self._record_cost(role, model, input_tokens, output_tokens, cost_usd, task_dir_path,
                          duration=duration, output_file=str(output_file) if output_file else "")

        # 11. Git 变更追踪
        git_after = await self._git_status(work_dir)
        changes = self._diff_git_status(git_before, git_after)

        # 12. 保存日志
        if task_dir_path:
            log_file = task_dir_path / "logs" / f"{role}-{int(time.time())}.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text(stdout_raw, encoding="utf-8")

        # 13. 构建结果（新增 3 个字段）
        last_evt_time = 0.0
        if events:
            last_evt_time = time.time()  # 最后一个事件的接收时间近似为当前
        return AgentResult(
            success=True,
            data=data,
            raw_output=stdout_raw,
            cost_tokens=input_tokens + output_tokens or estimated_tokens,
            duration=duration,
            exit_code=returncode,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            changes=changes,
            timeout_type=None,
            events_count=len(events),
            last_event_time=last_evt_time,
        )


    async def _stream_stderr(self, proc, stderr_lines: list, mcp_failures: list = None):
        """后台读取子进程 stderr，实时转发 OPUS_CONFIRM_PENDING 标记，检测 MCP 失败"""
        import sys
        # MCP 错误关键词（Claude CLI stderr 中的典型 MCP 失败信息）
        _MCP_ERROR_KEYWORDS = [
            "mcp server", "mcp connection", "failed to start",
            "connection refused", "econnrefused", "server disconnected",
            "spawn error", "npx error", "mcp error", "server failed",
            "could not connect", "timed out waiting for mcp",
        ]
        while True:
            try:
                line = await proc.stderr.readline()
            except Exception:
                break
            if not line:
                break
            line_str = line.decode("utf-8", errors="replace").strip()
            if line_str:
                stderr_lines.append(line_str)
                if "OPUS_CONFIRM_PENDING" in line_str:
                    print(line_str, file=sys.stderr, flush=True)
                # 检测 MCP 服务器启动/连接失败
                if mcp_failures is not None:
                    line_lower = line_str.lower()
                    if any(kw in line_lower for kw in _MCP_ERROR_KEYWORDS):
                        mcp_failures.append(line_str)
                        logger.warning(f"MCP 工具异常: {line_str}")

    async def _graceful_kill(self, proc: asyncio.subprocess.Process) -> None:
        """优雅终止子进程组：SIGTERM → 5s → SIGKILL"""
        try:
            pgid = os.getpgid(proc.pid)
        except (AttributeError, ProcessLookupError, OSError):
            pgid = None
        try:
            if pgid:
                os.killpg(pgid, signal.SIGTERM)
            else:
                proc.terminate()  # SIGTERM
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            if pgid:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
            else:
                proc.kill()  # SIGKILL
            await proc.wait()
        except ProcessLookupError:
            try:
                await proc.wait()
            except Exception:
                pass

    @staticmethod
    def _signal_process_group(proc: asyncio.subprocess.Process, sig: int) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (AttributeError, ProcessLookupError, OSError):
            os.kill(proc.pid, sig)

    @staticmethod
    def _save_pause_feedback(task_id: str, feedback: str):
        """保存暂停期间的用户反馈，供 pre_tool_dispatcher hook 注入到会话"""
        try:
            import redis
            r = redis.Redis(host="127.0.0.1", port=6380, decode_responses=True,
                            socket_timeout=2, socket_connect_timeout=2)
            r.set(f"pause_feedback:{task_id}", feedback, ex=3600)
            r.close()
        except Exception:
            pass  # 非关键路径

    @staticmethod
    def _extract_action(event: dict) -> str:
        """从流式事件中提取可读的动作描述"""
        event_type = event.get("type", "")

        if event_type == "assistant":
            msg = event.get("message", {})
            content = msg.get("content", []) if isinstance(msg, dict) else []
            tools = [c.get("name", "") for c in content if c.get("type") == "tool_use"]
            text_parts = [c.get("text", "") for c in content if c.get("type") == "text"]
            if tools:
                # 提取工具调用的简短描述
                tool_details = []
                for c in content:
                    if c.get("type") == "tool_use":
                        inp = c.get("input", {})
                        name = c.get("name", "")
                        if name == "Read":
                            tool_details.append(f"Read {inp.get('file_path', '')[-50:]}")
                        elif name == "Edit":
                            tool_details.append(f"Edit {inp.get('file_path', '')[-50:]}")
                        elif name == "Write":
                            tool_details.append(f"Write {inp.get('file_path', '')[-50:]}")
                        elif name == "Bash":
                            cmd = inp.get("command", "")[:60]
                            tool_details.append(f"Bash: {cmd}")
                        elif name == "Grep":
                            tool_details.append(f"Grep: {inp.get('pattern', '')[:40]}")
                        elif name == "Glob":
                            tool_details.append(f"Glob: {inp.get('pattern', '')[:40]}")
                        elif name == "Task":
                            tool_details.append(f"Task: {inp.get('description', '')[:40]}")
                        else:
                            tool_details.append(name)
                return " | ".join(tool_details)
            # 文本输出不作为动作显示（避免原始代码/JSON 出现在进度表中）
        elif event_type == "system" and event.get("subtype") == "init":
            model = event.get("model", "")
            return f"🚀 初始化 model={model}"

        return ""

    async def _run_subprocess_streaming(
        self, cmd: list, prompt: str, work_dir: str,
        idle_timeout: int = 120, max_timeout: int = 3600,
        live_log_file: str = None, parent_live_log_file: str = None,
        on_action=None,
        on_stream_event=None, task_id: str = None,
        parent_task_id: str = None,
        on_paused=None, on_resumed=None,
        role: str = None, extra_env: dict = None,
        action_extractor=None,
    ) -> tuple:
        """流式执行子进程，基于事件间隔做心跳检测

        Args:
            live_log_file: 实时日志文件路径（逐行写入，供 tail -f 观察）
            parent_live_log_file: 父任务日志文件路径（子任务时同步写入）
            on_action: 回调函数 on_action(action_str)，通知当前子代理动作
            task_id: 任务 ID，用于轮询控制信号（暂停/终止可秒级响应）
            on_paused: async 回调，进程被 SIGSTOP 冻结时调用（推卡片、更新进度）
            on_resumed: async 回调，进程被 SIGCONT 恢复时调用（更新状态）

        Returns:
            tuple[stdout_accumulated, stderr, returncode, events]
        """
        # 移除 CLAUDECODE 环境变量，注入 OPUS_TASK_ID 供 hook 读取暂停反馈
        # 清除继承的 ANTHROPIC_/CODEX_ 与默认 OPENAI_API_KEY，避免当前终端
        # 的 CLI 认证、线程状态和旧 key 污染子进程。
        managed_claude_envs = {
            "CLAUDE_CODE_SUBAGENT_MODEL",
            "CLAUDE_CODE_DISABLE_1M_CONTEXT",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        }
        clean_env = {
            k: v for k, v in os.environ.items()
            if k != "CLAUDECODE"
            and not k.startswith("ANTHROPIC_")
            and not k.startswith("CODEX_")
            and k != "OPENAI_API_KEY"
            and k not in managed_claude_envs
        }
        if task_id:
            clean_env["OPUS_TASK_ID"] = task_id
        if role:
            clean_env["OPUS_AGENT_ROLE"] = role
        if extra_env:
            clean_env.update(extra_env)

        # 🔑 外部模型：临时将 ~/.claude/settings.json 替换为对应模型的认证配置
        # 原因：Claude Code 读取 settings.json 的 env 段时会覆盖 OS env，
        #       导致代理认证取代外部模型认证，即使 clean_env 中已有正确的值。
        # 用跨进程文件锁（fcntl.flock）防止与 confirm_server 中的
        # sync_main_session_to_claude_settings 并发修改同一文件。
        _user_settings_path = Path.home() / ".claude" / "settings.json"
        _settings_backup = None
        _settings_modified = False
        _flock_fd = None  # 跨进程文件锁 fd

        # 🛡️ 崩溃恢复检查：上次进程异常退出可能留下未恢复的 settings.json
        self._recover_settings_if_needed()

        if extra_env and ("ANTHROPIC_AUTH_TOKEN" in extra_env or "ANTHROPIC_API_KEY" in extra_env):
            # 获取跨进程文件锁 — 所有检查和写入都在锁内完成（消除 TOCTOU）
            from lib.settings_lock import acquire_async, release as release_flock
            try:
                _flock_fd = await acquire_async(timeout=10)
            except TimeoutError as e:
                logger.warning(f"获取 settings.json 文件锁超时: {e}，将跳过替换直接启动")
                _flock_fd = None

            if _flock_fd is not None:
                try:
                    # 🔍 锁内比对：凭证一致则跳过替换
                    _need_replace = False
                    try:
                        if _user_settings_path.exists():
                            _current = json.loads(_user_settings_path.read_text(encoding="utf-8"))
                            _current_env = _current.get("env", {})
                            _new_token = extra_env.get("ANTHROPIC_AUTH_TOKEN") or extra_env.get("ANTHROPIC_API_KEY", "")
                            _cur_token = _current_env.get("ANTHROPIC_AUTH_TOKEN") or _current_env.get("ANTHROPIC_API_KEY", "")
                            _new_url = extra_env.get("ANTHROPIC_BASE_URL", "")
                            _cur_url = _current_env.get("ANTHROPIC_BASE_URL", "")
                            if _new_token != _cur_token or _new_url != _cur_url:
                                _need_replace = True
                            else:
                                logger.info(f"settings.json 凭证一致，跳过替换 (base_url={_cur_url[:30] if _cur_url else 'default'})")
                        else:
                            _need_replace = True
                    except Exception:
                        _need_replace = True

                    if _need_replace:
                        # 备份原文件到内存 + 磁盘（双保险）
                        if _user_settings_path.exists():
                            _settings_backup = _user_settings_path.read_text(encoding="utf-8")
                            orig = json.loads(_settings_backup)
                            self._SETTINGS_BACKUP_PATH.write_text(_settings_backup, encoding="utf-8")
                        else:
                            _settings_backup = None
                            orig = {}
                        # 构建临时 settings：只替换 env 段，保留其他配置（hooks等）
                        tmp_settings = dict(orig)
                        tmp_settings["env"] = {
                            k: v for k, v in extra_env.items()
                            if k.startswith("ANTHROPIC_") or k in (
                                "API_TIMEOUT_MS",
                                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
                                "CLAUDE_CODE_DISABLE_1M_CONTEXT",
                                "CLAUDE_CODE_SUBAGENT_MODEL",
                            )
                        }
                        _user_settings_path.write_text(json.dumps(tmp_settings, indent=2), encoding="utf-8")
                        _settings_modified = True
                        logger.info(f"外部模型：已临时替换 settings.json (ANTHROPIC_BASE_URL={extra_env.get('ANTHROPIC_BASE_URL', '?')[:30]})")
                    else:
                        # 凭证一致，不需要替换，立即释放锁
                        release_flock(_flock_fd)
                        _flock_fd = None
                except Exception as e:
                    logger.warning(f"无法修改 settings.json，将继续尝试: {e}")
                    self._SETTINGS_BACKUP_PATH.unlink(missing_ok=True)
                    release_flock(_flock_fd)
                    _flock_fd = None

        # 🛡️ CWD 预验证：启动前检查工作目录是否存在
        work_dir_path = Path(str(work_dir))
        if not work_dir_path.exists():
            if _settings_modified:
                self._restore_settings(_user_settings_path, _settings_backup)
            if _flock_fd is not None:
                from lib.settings_lock import release as release_flock
                release_flock(_flock_fd)
                _flock_fd = None
            raise AgentError(
                f"工作目录不存在: {work_dir}（可能 worktree 已被清理）",
                error_code="E301",
            )
        if not work_dir_path.is_dir():
            if _settings_modified:
                self._restore_settings(_user_settings_path, _settings_backup)
            if _flock_fd is not None:
                from lib.settings_lock import release as release_flock
                release_flock(_flock_fd)
                _flock_fd = None
            raise AgentError(
                f"工作路径不是目录: {work_dir}",
                error_code="E301",
            )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(work_dir),
                env=clean_env,
                limit=10 * 1024 * 1024,  # 10MB — 大型工具调用结果可达数 MB
                start_new_session=True,
            )
        except Exception:
            if _settings_modified:
                self._restore_settings(_user_settings_path, _settings_backup)
                _settings_modified = False
                logger.info("外部模型：子进程启动失败，已恢复原 settings.json")
            if _flock_fd is not None:
                from lib.settings_lock import release as release_flock
                release_flock(_flock_fd)
                _flock_fd = None
            raise

        # 写入 prompt 并关闭 stdin
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
        except Exception:
            await self._graceful_kill(proc)
            if _settings_modified:
                self._restore_settings(_user_settings_path, _settings_backup)
                _settings_modified = False
                logger.info("外部模型：写入 prompt 失败，已恢复原 settings.json")
            if _flock_fd is not None:
                from lib.settings_lock import release as release_flock
                release_flock(_flock_fd)
                _flock_fd = None
            raise

        # 【新增】后台读取 stderr（实时转发确认标记）
        stderr_lines = []
        mcp_failures = []
        stderr_task = asyncio.create_task(
            self._stream_stderr(proc, stderr_lines, mcp_failures)
        )

        events = []
        stdout_lines = []
        start_time = time.time()
        last_event_time = start_time  # 心跳起始=进程启动时刻
        _last_live_log_time = start_time  # 直播日志心跳计时
        timeout_type = None
        runtime_failure_message = ""

        try:
            while True:
                # 快速检查子进程是否已退出
                if proc.returncode is not None:
                    logger.warning(f"子进程已退出(code={proc.returncode})，停止读取")
                    break

                now = time.time()
                elapsed = now - start_time

                # 检查 max_timeout
                if elapsed >= max_timeout:
                    timeout_type = "max"
                    logger.warning(f"绝对超时 ({max_timeout}s)，终止进程")
                    break

                # 计算本次 readline 的等待上限
                remaining_idle = idle_timeout - (now - last_event_time)
                remaining_max = max_timeout - elapsed
                wait_time = max(0.1, min(remaining_idle, remaining_max))

                # 有 task_id 时缩短等待，以便每 3 秒轮询控制信号
                SIGNAL_POLL_INTERVAL = 3
                if task_id:
                    wait_time = min(wait_time, SIGNAL_POLL_INTERVAL)

                try:
                    line = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=wait_time
                    )
                except ValueError:
                    # StreamReader 行超长（不应发生，limit 已设 10MB）
                    logger.warning("readline 行超长，跳过当前缓冲区")
                    try:
                        await asyncio.wait_for(
                            proc.stdout.read(65536), timeout=2
                        )
                    except Exception:
                        pass
                    continue
                except asyncio.TimeoutError:
                    # 先检查子进程是否已退出（管道未正常关闭的异常场景）
                    if proc.returncode is not None:
                        logger.warning(
                            f"子进程已退出(code={proc.returncode})但stdout未关闭，强制结束读取"
                        )
                        break
                    # 二次检查：asyncio child watcher 可能未及时更新 returncode
                    # 通过 os.kill(pid, 0) 直接检查进程是否存活
                    try:
                        os.kill(proc.pid, 0)
                    except ProcessLookupError:
                        logger.warning(
                            f"子进程 pid={proc.pid} 已退出但 returncode 未更新，强制结束读取"
                        )
                        break

                    # 轮询控制信号（暂停/终止/回滚可秒级响应）
                    if task_id:
                        from lib.control_signals import read_signal
                        ctrl_signal = read_signal(task_id)
                        # 子任务执行时，同时检查父任务信号（用户在前端操作的是父任务 ID）
                        if not ctrl_signal and parent_task_id:
                            ctrl_signal = read_signal(parent_task_id)
                        if ctrl_signal:
                            action = ctrl_signal.get("action")

                            if action == "pause":
                                # ——— 真正的暂停：冻结进程，等待用户决策 ———
                                try:
                                    self._signal_process_group(proc, signal.SIGSTOP)
                                except OSError:
                                    break  # 进程已退出
                                logger.info(f"子代理进程 PID={proc.pid} 已暂停 (SIGSTOP)")

                                # 通知编排器（推卡片、更新进度）
                                if on_paused:
                                    try:
                                        await on_paused(ctrl_signal)
                                    except Exception as cb_err:
                                        logger.error(f"on_paused 回调执行失败: {cb_err}")

                                # 等待用户决策：继续 or 回退/终止
                                while True:
                                    await asyncio.sleep(SIGNAL_POLL_INTERVAL)
                                    # 检查进程是否仍然存活（防止 OOM kill 后无限等待）
                                    try:
                                        os.kill(proc.pid, 0)
                                    except ProcessLookupError:
                                        logger.error(f"暂停期间子代理进程 PID={proc.pid} 已异常退出")
                                        raise AgentSignalInterrupt(
                                            "子代理在暂停期间被系统杀死（可能是 OOM），请 resume 重跑",
                                            signal={"action": "terminate", "reason": "process_died_during_pause"},
                                            events=events,
                                        )
                                    except OSError:
                                        pass  # 信号发送权限问题，忽略
                                    next_sig = read_signal(task_id)
                                    if not next_sig and parent_task_id:
                                        next_sig = read_signal(parent_task_id)
                                    if not next_sig:
                                        continue
                                    next_action = next_sig.get("action")

                                    if next_action == "resume":
                                        # 分支 1：继续——先写反馈再解冻，避免竞态
                                        # （解冻后子代理可能立即触发工具调用，
                                        #   hook 需要在此之前读到 Redis 中的反馈）
                                        feedback = next_sig.get("feedback", "")
                                        if feedback:
                                            self._save_pause_feedback(task_id, feedback)
                                        try:
                                            self._signal_process_group(proc, signal.SIGCONT)
                                        except OSError:
                                            break  # 暂停期间进程异常退出
                                        logger.info(f"子代理进程 PID={proc.pid} 已恢复 (SIGCONT)")
                                        last_event_time = time.time()  # 重置空闲计时
                                        # 通知编排器恢复
                                        if on_resumed:
                                            await on_resumed(next_sig)
                                        break

                                    elif next_action in ("rollback", "terminate"):
                                        # 分支 2：回退/终止——解冻后杀死
                                        try:
                                            self._signal_process_group(proc, signal.SIGCONT)
                                        except OSError:
                                            pass
                                        await self._graceful_kill(proc)
                                        raise AgentSignalInterrupt(
                                            f"暂停后收到{next_action}信号，终止子代理",
                                            signal=next_sig,
                                            events=events,
                                        )
                                continue  # 恢复后继续 readline 循环

                            elif action in ("terminate", "rollback"):
                                # 直接终止/回滚（未经暂停）
                                logger.info(f"子代理执行中收到 {action} 信号，终止子进程")
                                await self._graceful_kill(proc)
                                raise AgentSignalInterrupt(
                                    f"收到{action}信号，终止子代理",
                                    signal=ctrl_signal,
                                    events=events,
                                )

                    # 空闲心跳：30 秒内无直播日志 → 写入心跳行
                    HEARTBEAT_INTERVAL = 30
                    if live_log_file:
                        since_last_log = time.time() - _last_live_log_time
                        if since_last_log >= HEARTBEAT_INTERVAL:
                            try:
                                ts = time.strftime("%H:%M:%S")
                                elapsed_min = int((time.time() - start_time) / 60)
                                heartbeat_line = f"[{ts}] ● AI 助手仍在工作中（已运行 {elapsed_min} 分钟）\n"
                                with open(live_log_file, "a", encoding="utf-8") as lf:
                                    lf.write(heartbeat_line)
                                if parent_live_log_file:
                                    with open(parent_live_log_file, "a", encoding="utf-8") as lf:
                                        lf.write(heartbeat_line)
                                _last_live_log_time = time.time()
                            except Exception:
                                pass

                    # 检查子进程是否处于异常停止状态（T/Tl）
                    # 场景：PTY 会话断开、额度耗尽导致 claude 卡住等
                    try:
                        stat_path = f"/proc/{proc.pid}/status"
                        with open(stat_path) as sf:
                            for sline in sf:
                                if sline.startswith("State:"):
                                    pstate = sline.split()[1]
                                    if pstate in ("T", "t"):  # stopped / traced
                                        idle_since = time.time() - last_event_time
                                        if idle_since > 120:  # 停止超过 2 分钟才判定异常
                                            logger.warning(
                                                f"子进程 pid={proc.pid} 处于停止状态 ({pstate})，"
                                                f"已无输出 {idle_since:.0f}s，终止进程"
                                            )
                                            timeout_type = "stuck"
                                            break
                                    break
                    except (FileNotFoundError, OSError):
                        pass  # 进程已退出或无权读取
                    if timeout_type == "stuck":
                        break

                    # 检查是否为真正的空闲/绝对超时
                    now2 = time.time()
                    if now2 - last_event_time >= idle_timeout:
                        timeout_type = "idle"
                        logger.warning(f"空闲超时 ({idle_timeout}s)，终止进程")
                        break
                    elif now2 - start_time >= max_timeout:
                        timeout_type = "max"
                        logger.warning(f"绝对超时 ({max_timeout}s)，终止进程")
                        break
                    # 信号轮询间隔到期，继续读取
                    continue

                if not line:  # EOF
                    break

                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                # 🛡️ 收到任何 stdout 数据就重置空闲计时（含 extended thinking 心跳）
                last_event_time = time.time()

                stdout_lines.append(line_str)

                # 单行大小保护：超过 1MB 截断
                if len(line_str) > 1_048_576:
                    logger.warning(f"单行 JSON 超过 1MB ({len(line_str)} bytes)，已截断")
                    line_str = line_str[:1_048_576]

                # 尝试解析 JSON
                try:
                    event = json.loads(line_str)
                    events.append(event)
                    if _is_codex_reconnect_exhausted_event(event):
                        runtime_failure_message = str(event.get("message") or "")
                        logger.warning(
                            "Codex CLI 重连已耗尽，提前终止子进程: %s",
                            runtime_failure_message[:200],
                        )
                        break

                    # 流式渲染回调
                    if on_stream_event:
                        try:
                            on_stream_event(event)
                        except Exception:
                            pass

                    event_type = event.get("type", "unknown")
                    elapsed_now = time.time() - start_time
                    idle_now = time.time() - last_event_time

                    logger.debug(
                        f"Agent heartbeat: event_type={event_type}, "
                        f"elapsed={elapsed_now:.1f}s, idle={idle_now:.1f}s"
                    )

                    # 提取 session_id
                    if event_type == "system" and event.get("subtype") == "init":
                        session_id = event.get("session_id", "")
                        if session_id:
                            logger.info(f"Agent session_id: {session_id}")

                    # 实时日志 + 动作回调
                    extractor = action_extractor or self._extract_action
                    action_str = extractor(event)
                    if live_log_file and action_str:
                        try:
                            ts = time.strftime("%H:%M:%S")
                            log_line = f"[{ts}] {action_str}\n"
                            with open(live_log_file, "a", encoding="utf-8") as lf:
                                lf.write(log_line)
                            # 子任务活动同步到父任务 live.log
                            if parent_live_log_file:
                                with open(parent_live_log_file, "a", encoding="utf-8") as lf:
                                    lf.write(log_line)
                            _last_live_log_time = time.time()
                        except Exception:
                            pass
                    if on_action and action_str:
                        try:
                            on_action(action_str)
                        except Exception:
                            pass

                except (json.JSONDecodeError, TypeError):
                    if on_stream_event:
                        try:
                            on_stream_event({"type": "_raw_line", "content": line_str[:200]})
                        except Exception:
                            pass
                    logger.warning(f"非 JSON 行（skip）: {line_str[:100]}")

        finally:
            if timeout_type or runtime_failure_message:
                await self._graceful_kill(proc)
                # drain 剩余 stdout
                try:
                    remaining = await asyncio.wait_for(
                        proc.stdout.read(65536), timeout=2
                    )
                    if remaining:
                        for extra_line in remaining.decode("utf-8", errors="replace").split("\n"):
                            extra_line = extra_line.strip()
                            if extra_line:
                                stdout_lines.append(extra_line)
                                try:
                                    events.append(json.loads(extra_line))
                                except (json.JSONDecodeError, TypeError):
                                    pass
                except (asyncio.TimeoutError, Exception):
                    pass
            else:
                # 正常结束，等待进程退出
                await proc.wait()
            # Claude Code 2.1+ 可能在 init 后才读取 settings/env 发起 API 请求。
            # 因此外部模型 settings 必须保持到子进程结束，不能在启动 2s 后恢复。
            if _settings_modified:
                self._restore_settings(_user_settings_path, _settings_backup)
                _settings_modified = False
                logger.info("外部模型：子进程结束，已恢复原 settings.json")
            if _flock_fd is not None:
                from lib.settings_lock import release as release_flock
                release_flock(_flock_fd)
                _flock_fd = None

        # 【修改】等待 stderr 后台读取完成
        try:
            await asyncio.wait_for(stderr_task, timeout=5)
        except (asyncio.TimeoutError, Exception):
            stderr_task.cancel()
        stderr_str = "\n".join(stderr_lines)

        # 【新增】MCP 工具失败时写入 live_log 通知用户（不终止任务）
        if mcp_failures and live_log_file:
            mcp_warn = (
                f"\n{'!'*60}\n"
                f"[⚠️ MCP 工具异常] 以下工具可能不可用：\n"
            )
            for f_line in mcp_failures[:10]:
                mcp_warn += f"  - {f_line[:200]}\n"
            mcp_warn += f"{'!'*60}\n"
            try:
                with open(live_log_file, "a", encoding="utf-8") as lf:
                    lf.write(mcp_warn)
                if parent_live_log_file:
                    with open(parent_live_log_file, "a", encoding="utf-8") as lf:
                        lf.write(mcp_warn)
            except Exception:
                pass
            logger.warning(f"检测到 {len(mcp_failures)} 条 MCP 异常，已写入 live_log")

        returncode = proc.returncode if proc.returncode is not None else -1

        # 如果是超时，抛出异常（携带 events 以便上层提取费用）
        if runtime_failure_message:
            raise AgentError(
                f"Codex CLI 重连失败: {runtime_failure_message[:220]}",
                error_code="E305",
            )
        if timeout_type == "idle":
            raise AgentTimeoutError(
                f"空闲超时 ({idle_timeout}s)，共收到 {len(events)} 个事件",
                error_code="E101", timeout_type="idle", events=events
            )
        elif timeout_type == "max":
            raise AgentTimeoutError(
                f"绝对超时 ({max_timeout}s)，共收到 {len(events)} 个事件",
                error_code="E102", timeout_type="max", events=events
            )
        elif timeout_type == "stuck":
            raise AgentTimeoutError(
                f"子进程停止状态僵死，共收到 {len(events)} 个事件",
                error_code="E103", timeout_type="stuck", events=events
            )

        stdout_accumulated = "\n".join(stdout_lines)
        return stdout_accumulated, stderr_str, returncode, events

    async def _build_prompt(self, role, task_dir, input_docs, memories, output_file, project,
                           session_recall=None, work_state_summary=None,
                           template_override=None):
        """构建完整 Prompt = 角色模板 + 项目记忆 + 输入文档 + 输出要求"""
        # 角色模板（template_override 用于 AgentHub 从模块目录加载模板）
        if template_override:
            template_path = Path(template_override)
        else:
            template_path = Path(__file__).parent / "role_templates" / f"{role}.md"
        if template_path.exists():
            role_template = template_path.read_text(encoding="utf-8")
        else:
            role_template = f"# 角色：{role}\n\n你是一名专业的 {role}，请根据以下信息完成任务。"
            logger.warning(f"角色模板不存在: {template_path}")

        # 项目知识索引（轻量索引，子代理按需读取全文）
        proj = project or self.config.get("default_project", "")
        memories_text = self._build_memory_index(proj, role)
        startup_context = resolve_startup_protocol_context(
            project=proj,
            cwd=str(self._get_project_path(proj)),
            scope=role,
            config=self.config,
            fallback_root=self.project_path,
        )
        startup_text = build_role_startup_instructions(startup_context, role=role)

        # 会话回忆和工作状态
        context_text = ""
        if session_recall:
            context_text += f"\n## 近期会话回忆\n{session_recall}\n"
        if work_state_summary:
            context_text += f"\n## 当前工作状态\n{work_state_summary}\n"

        # 输入文档
        docs_text = ""
        if task_dir and input_docs:
            task_dir_path = Path(task_dir)
            for doc_name, doc_val in input_docs.items():
                if isinstance(doc_val, str) and not doc_val.startswith("{"):
                    # 判断是文件路径还是内容：
                    # 1. 短字符串（<200 字符）且无换行
                    # 2. 看起来像文件名（含扩展名或路径分隔符）
                    # 3. 长度不超过 Linux 文件名限制（255 字节）
                    looks_like_path = (
                        len(doc_val) < 200
                        and "\n" not in doc_val
                        and ("." in doc_val or "/" in doc_val)
                        and len(doc_val.encode("utf-8")) <= 255
                    )
                    if looks_like_path:
                        doc_path = task_dir_path / doc_val
                        if doc_path.exists():
                            docs_text += f"\n## 输入文档：{doc_name}\n{doc_path.read_text(encoding='utf-8')}\n"
                        else:
                            docs_text += f"\n## 输入数据：{doc_name}\n{doc_val}\n"
                    else:
                        docs_text += f"\n## 输入数据：{doc_name}\n{doc_val}\n"
                else:
                    docs_text += f"\n## 输入数据：{doc_name}\n{doc_val}\n"

            # 设计文档哈希校验
            if "design" in input_docs:
                design_val = input_docs["design"]
                if isinstance(design_val, str) and not design_val.startswith("{"):
                    is_design_path = (
                        len(design_val) < 200
                        and "\n" not in design_val
                        and ("." in design_val or "/" in design_val)
                        and len(design_val.encode("utf-8")) <= 255
                    )
                    if is_design_path:
                        design_path = task_dir_path / design_val
                        if design_path.exists():
                            doc_hash = self._compute_doc_hash(design_path)
                            docs_text += f"\n> 你基于的设计文档版本哈希: {doc_hash}\n"
                            docs_text += "> 如果发现实际代码与设计文档不一致，以设计文档为准。\n"

        # 文档模板注入（追加到 docs_text 末尾，截断时优先截断模板）
        doc_template_text = self._load_referenced_doc_templates(role_template)
        if doc_template_text:
            docs_text = docs_text + doc_template_text

        # 输出要求
        output_instruction = ""
        if output_file:
            output_instruction = f"""
# 输出要求
1. 将你的工作成果写入文件：{output_file}
2. 完成后，在最终回复中输出 JSON 格式的摘要
3. JSON 格式：{{"status": "success", "summary": "...", "files_changed": [...]}}
"""
        operational_guidance = self._build_role_operational_guidance(role, task_dir)

        # 保存分段（供优先级截断使用）
        self._prompt_segments = {
            "role_template": role_template,         # priority 1: 不可截断
            "startup_text": startup_text,           # priority 1: 不可截断
            "output_instruction": output_instruction,  # priority 1: 不可截断
            "operational_guidance": operational_guidance,  # priority 1: 不可截断
            "memories_text": memories_text,          # priority 2: 最后截断
            "context_text": context_text,            # priority 3: 次后截断
            "docs_text": docs_text,                  # priority 4: 优先截断
        }

        # 语言指令：根据需求原始语言决定
        lang = self._detect_input_language(docs_text, memories_text)
        if lang == "en":
            lang_instruction = (
                "\n# Language\n"
                "Use English for all communication and documentation.\n"
                "Keep the following in their original form: "
                "code, JSON keys, enum values (e.g. status/task_type/scale), "
                "variable names, file paths, commands, error messages, URLs.\n"
            )
        else:
            lang_instruction = (
                "\n# 语言要求\n"
                "用中文进行说明和沟通。以下内容必须保持原样不变：\n"
                "代码、JSON key、JSON 中的枚举值（如 status/task_type/scale 的值）、"
                "变量名、文件路径、命令行、错误信息、URL。\n"
            )

        knowledge_guide = (
            "\n## 项目知识使用指南\n"
            "上方索引列出了所有可用的项目知识条目。\n"
            "- 如存在“当前角色必读条目”，必须先按上方启动步骤读取这些条目\n"
            "- 使用 `mcp__serena__read_memory` 按需读取与当前任务相关的条目\n"
            "- 使用 `mcp__serena__list_memories` 可查看完整列表\n"
            "- 不要一次读取所有条目，按需读取以节省上下文\n"
        )
        return (
            f"{role_template}\n\n---\n\n"
            f"{startup_text}\n\n---\n\n"
            f"# 项目知识库\n{memories_text or '（无项目知识索引）'}\n"
            f"{knowledge_guide}\n"
            f"{operational_guidance}\n"
            f"{context_text}\n\n---\n\n"
            f"# 本次任务的相关文档\n{docs_text or '（无前置文档）'}\n\n---\n\n"
            f"{output_instruction}"
            f"{lang_instruction}"
        )

    @staticmethod
    def _build_role_operational_guidance(role: str, task_dir) -> str:
        task_dir_text = str(task_dir) if task_dir else "当前传入的 task_dir"
        guidance = (
            "\n## 检索与路径约束\n"
            f"- Vizo 工作流任务目录以本次传入的 task_dir 为准：`{task_dir_text}`；如需查历史任务，使用当前项目 `.vizo/tasks/`，不要查询 `.opus/tasks/` 旧路径\n"
            "- 使用 `rg`/Grep 搜索时默认排除生成日志和子代理运行事件，避免把巨型事件日志拉回上下文；建议参数："
            "`--glob '!.vizo/tasks/**/runtime/subagents/**' --glob '!**/raw-events*.jsonl' --glob '!**/events.jsonl' --glob '!**/logs/**' --glob '!**/*.log'`\n"
            "- 对 JS/HTML/CSS/静态资源/模板文件，优先用 `rg` 定位后定向读取相关行；Python-only Serena 环境中不要优先调用 `mcp__serena__get_symbols_overview` 分析这些前端或静态文件\n"
        )
        if role == "knowledge_admin":
            guidance += (
                "\n## knowledge_admin 记忆写入报告要求\n"
                "- 如果直接通过文件补丁或写文件修改 `.serena/memories/**`，最终 JSON 的 `files_changed` 必须包含实际变更的 memory 文件路径\n"
                "- 如果 `mcp__serena__edit_memory` 或其他 Serena 记忆写入 API 失败，摘要必须明确说明失败原因和后续采用的写入方式；不要把直接文件补丁伪装成 API 写入成功\n"
            )
        return guidance

    @staticmethod
    def _detect_input_language(docs_text: str, memories_text: str = "") -> str:
        """根据输入文档中的中文字符比例检测语言。
        只采样前 2000 字符，避免对大文档耗时。
        Returns: 'zh' or 'en'
        """
        sample = (docs_text or "")[:2000]
        if not sample.strip():
            sample = (memories_text or "")[:1000]
        if not sample.strip():
            return "zh"  # 默认中文
        # 统计 CJK 字符数量
        cjk_count = sum(1 for c in sample if '\u4e00' <= c <= '\u9fff')
        total_alpha = sum(1 for c in sample if c.isalpha())
        if total_alpha == 0:
            return "zh"
        return "zh" if cjk_count / total_alpha > 0.1 else "en"

    def _load_referenced_doc_templates(self, role_template_text: str) -> str:
        """扫描角色模板中的文档模板引用，读取并拼接为 docs_text"""
        matches = self.DOC_TEMPLATE_PATTERN.findall(role_template_text)
        if not matches:
            return ""

        base_dir = Path(__file__).parent
        doc_texts = []
        seen = set()

        for filename in matches:
            if filename in seen:
                continue
            seen.add(filename)

            doc_path = base_dir / "role_templates" / "docs" / filename
            if doc_path.exists():
                content = doc_path.read_text(encoding="utf-8")
                doc_texts.append(f"\n# 参考文档模板：{filename}\n\n{content}\n\n---\n")
                logger.info(f"注入文档模板: {filename}")
            else:
                logger.warning(f"文档模板不存在: {doc_path}")

        return "".join(doc_texts)

    def _parse_json_output(self, raw: str) -> dict:
        """健壮的 JSON 解析

        claude -p --output-format json 输出结构:
          {"type":"result", "result":"...agent 文本（含 JSON）...", "usage":{...}, ...}

        Agent 的业务 JSON（all_passed, failures, task_type 等）嵌在 result 文本中。
        本方法解析顶层 CLI JSON 后，从 result 文本中提取业务 JSON 并合并。
        """
        # 第一步：解析顶层 JSON
        data = None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            # 非标准 JSON，尝试花括号匹配
            data = self._find_first_json(raw)

        if not data:
            logger.warning("无法解析 Agent JSON 输出，使用原始文本")
            return {"status": "unknown", "raw": raw[:500]}

        # 第二步：从 result 文本中提取 Agent 业务 JSON
        result_text = data.get("result", "")
        if isinstance(result_text, str) and result_text:
            agent_json = self._extract_agent_json(result_text)
            if agent_json and isinstance(agent_json, dict):
                # 保留完整的 agent 业务数据
                data["agent_data"] = agent_json
                # 合并业务 key 到顶层（不覆盖 CLI 元数据）
                cli_keys = {"type", "subtype", "result", "is_error", "usage",
                            "modelUsage", "session_id", "total_cost_usd",
                            "duration_ms", "duration_api_ms", "num_turns",
                            "stop_reason", "agent_data", "permission_denials",
                            "uuid"}
                for k, v in agent_json.items():
                    if k not in cli_keys:
                        data[k] = v

        return data

    def _extract_agent_json(self, text: str) -> dict | None:
        """从 Agent 输出文本中提取最后一个 JSON 对象

        优先匹配 ```json ... ``` 代码块（取最后一个），
        回退到从文本末尾向前搜索 {...} 结构。
        """
        # 策略1：找最后一个 ```json ... ``` 代码块
        json_blocks = re.findall(r'```json\s*\n(.*?)\n\s*```', text, re.DOTALL)
        for block in reversed(json_blocks):
            try:
                obj = json.loads(block.strip())
                if isinstance(obj, dict):
                    return obj
            except (json.JSONDecodeError, TypeError):
                continue

        # 策略2：从末尾向前找最后一个完整的 {...}
        last_brace = text.rfind('}')
        if last_brace < 0:
            return None

        # 从最后的 } 向前找配对的 {
        depth = 0
        for i in range(last_brace, -1, -1):
            if text[i] == '}':
                depth += 1
            elif text[i] == '{':
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[i:last_brace + 1])
                        if isinstance(obj, dict):
                            return obj
                    except (json.JSONDecodeError, TypeError):
                        return None
        return None

    def _find_first_json(self, raw: str) -> dict | None:
        """从原始文本中找到第一个完整 JSON 对象"""
        brace_depth = 0
        start = -1
        for i, c in enumerate(raw):
            if c == '{':
                if brace_depth == 0:
                    start = i
                brace_depth += 1
            elif c == '}':
                brace_depth -= 1
                if brace_depth == 0 and start >= 0:
                    try:
                        return json.loads(raw[start:i + 1])
                    except json.JSONDecodeError:
                        start = -1
        return None

    def _estimate_tokens(self, text: str) -> int:
        """中文友好的 token 估算"""
        chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        other_chars = len(text) - chinese_chars
        return int(chinese_chars * 1.5 + other_chars * 0.25)

    def _truncate_prompt(self, prompt: str, target: int = 180_000) -> str:
        """按优先级截断 Prompt：docs_text → context_text → memories_text，保护 role_template 和 output_instruction"""
        current = self._estimate_tokens(prompt)
        if current <= target:
            return prompt

        segments = getattr(self, '_prompt_segments', None)
        if not segments:
            # fallback: 无分段信息时用旧逻辑
            ratio = target / current
            cut_point = int(len(prompt) * ratio * 0.95)
            return prompt[:cut_point] + "\n\n[... 内容因过长被截断 ...]"

        # 按优先级从低到高截断：docs(4) → context(3) → memories(2)
        truncation_order = ["docs_text", "context_text", "memories_text"]
        truncated = dict(segments)

        for key in truncation_order:
            if current <= target:
                break
            seg_tokens = self._estimate_tokens(truncated[key])
            if seg_tokens == 0:
                continue
            excess = current - target
            if seg_tokens <= excess:
                # 整段截断
                truncated[key] = f"[... {key} 内容过长已截断 ...]"
                current -= seg_tokens
            else:
                # 部分截断
                keep_ratio = 1 - (excess / seg_tokens)
                keep_chars = int(len(truncated[key]) * keep_ratio * 0.9)
                truncated[key] = truncated[key][:keep_chars] + f"\n\n[... {key} 剩余内容已截断 ...]"
                current = target  # 近似达标

        knowledge_guide = (
            "\n## 项目知识使用指南\n"
            "上方索引列出了所有可用的项目知识条目。\n"
            "- 使用 `mcp__serena__read_memory` 按需读取与当前任务相关的条目\n"
            "- 使用 `mcp__serena__list_memories` 可查看完整列表\n"
            "- 不要一次读取所有条目，按需读取以节省上下文\n"
        )
        operational_guidance = truncated.get(
            "operational_guidance",
            self._build_role_operational_guidance("", ""),
        )

        return (
            f"{truncated['role_template']}\n\n---\n\n"
            f"{truncated.get('startup_text', '')}\n\n---\n\n"
            f"# 项目知识库\n{truncated['memories_text'] or '（无项目知识索引）'}\n"
            f"{knowledge_guide}\n"
            f"{operational_guidance}\n"
            f"{truncated['context_text']}\n\n---\n\n"
            f"# 本次任务的相关文档\n{truncated['docs_text'] or '（无前置文档）'}\n\n---\n\n"
            f"{truncated['output_instruction']}"
        )

    def _get_project_path(self, project: str) -> Path:
        """根据项目名称获取项目根目录路径"""
        resolved = resolve_project_root_by_name(
            project,
            config=self.config,
            fallback_root=self.project_path,
        )
        return resolved or self.project_path  # fallback 到默认项目

    async def _read_serena_memory(self, project: str, memory_name: str) -> str:
        """直接从磁盘读取 Serena 记忆文件

        根据 project 参数定位对应项目的 .serena/memories/ 目录。
        """
        project_path = self._get_project_path(project)
        memory_path = project_path / ".serena" / "memories" / f"{memory_name}.md"
        try:
            if memory_path.exists():
                content = memory_path.read_text(encoding="utf-8").strip()
                if content:
                    logger.info(f"读取记忆 {memory_name}: {len(content)} 字符")
                    return content
            logger.warning(f"记忆文件不存在: {memory_path}")
            return ""
        except OSError as e:
            logger.warning(f"读取记忆 {memory_name} 异常: {e}")
            return ""

    def _build_memory_index(self, project: str, role: Optional[str] = None) -> str:
        """生成项目记忆索引：标题 + 二级标题列表。"""
        project_path = self._get_project_path(project)
        if not project_path:
            return ""
        memories_dir = Path(project_path) / ".serena" / "memories"
        if not memories_dir.exists():
            return ""

        whitelist = load_project_memory_whitelist(project_path)
        required_names = resolve_required_memories(whitelist, role)

        entries = []
        available_names: set[str] = set()
        for md_file in sorted(memories_dir.rglob("*.md")):
            name = str(md_file.relative_to(memories_dir))[:-3]  # 去掉 .md
            available_names.add(name)
            lines = md_file.read_text(encoding="utf-8").strip().split("\n")
            title = ""
            sections = []
            for line in lines:
                if line.startswith("# ") and not line.startswith("## "):
                    title = line.lstrip("# ").strip()
                elif line.startswith("## "):
                    sections.append(line.lstrip("# ").strip())

            summary = title or name
            if sections:
                shown = sections[:8]
                summary += "（" + "、".join(shown) + "）"
                if len(sections) > 8:
                    summary += f" 等 {len(sections)} 个条目"
            entries.append(f"- **{name}** — {summary}")

        if not entries:
            return ""

        required_names = [name for name in required_names if name in available_names]

        header = "以下是本项目的知识库条目索引。需要时使用 mcp__serena__read_memory 按名称读取完整内容。\n\n"
        if required_names:
            required_lines = "\n".join(f"- **{name}**" for name in required_names)
            header += (
                f"## 当前角色必读条目（{role}）\n"
                f"{required_lines}\n\n"
            )

        return header + "\n".join(entries)

    def _compute_doc_hash(self, doc_path: Path) -> str:
        """计算文件内容 MD5 哈希（前8位）"""
        content = doc_path.read_text(encoding="utf-8")
        return hashlib.md5(content.encode()).hexdigest()[:8]


    def _extract_cost_from_events(self, events: list, model: str) -> tuple:
        """从 events 中提取费用信息（用于失败/超时场景）。

        Returns:
            (input_tokens, output_tokens, cost_usd)
        """
        input_tokens = 0
        output_tokens = 0
        cost_usd = 0.0
        for evt in reversed(events):
            if evt.get("type") == "result":
                usage = evt.get("usage", {})
                input_tokens = usage.get("input_tokens", 0)
                output_tokens = usage.get("output_tokens", 0)
                model_usage = evt.get("modelUsage", {})
                for model_info in model_usage.values():
                    cost_usd += model_info.get("costUSD", 0.0)
                break
        # 如果 result 事件没有费用，用 MODEL_PRICING 估算
        pricing = self.MODEL_PRICING.get(model)
        if pricing and cost_usd == 0.0 and (input_tokens > 0 or output_tokens > 0):
            cost_usd = (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000
        return input_tokens, output_tokens, cost_usd

    def _record_cost(self, role, model, input_tokens, output_tokens, cost_usd, task_dir,
                     duration=0.0, output_file=""):
        """记录费用到 task_dir/cost.json"""
        if not task_dir:
            return
        task_dir = Path(task_dir)
        task_dir.mkdir(parents=True, exist_ok=True)

        # 如果 claude CLI 没返回 cost_usd，用 MODEL_PRICING 估算
        pricing = self.MODEL_PRICING.get(model)
        if pricing and cost_usd == 0.0 and (input_tokens > 0 or output_tokens > 0):
            cost_usd = (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000

        # 判断是否计费模型
        # 非外部模型（无 ext_config）走 Anthropic 官方 API → 始终计费
        # 外部模型：检查 cli_model 是否匹配计费前缀（如转发回 Anthropic 的代理）
        ext_config = self.config.get("external_models", {}).get(model)
        if ext_config:
            billing_prefixes = self.config.get("billing_model_prefixes", ["claude-"])
            cli_model = ext_config.get("cli_model", model)
            billable = any(cli_model.startswith(p) for p in billing_prefixes)
        else:
            billable = True  # Anthropic 自有模型简写（sonnet/opus/haiku）→ 始终计费

        cost_file = task_dir / "cost.json"
        try:
            costs = json.loads(cost_file.read_text(encoding="utf-8")) if cost_file.exists() else []
        except (json.JSONDecodeError, ValueError):
            logger.warning(f"cost.json 损坏，重新创建: {cost_file}")
            costs = []
        costs.append({
            "role": role,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": round(cost_usd, 6),
            "billable": billable,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "duration": round(duration, 1),
            "output_file": Path(output_file).name if output_file else "",
        })
        cost_file.write_text(json.dumps(costs, indent=2, ensure_ascii=False), encoding="utf-8")

    async def _check_chrome_bridge_health(self) -> bool:
        """检查 Chrome Bridge 是否已连接浏览器。"""
        port = int(self.config.get("confirm_server", {}).get("port", 9390))
        return is_chrome_bridge_connected(port, timeout=2)

    async def _git_status(self, cwd: Path) -> set:
        """获取 Git 变更文件集合（非 Git 目录返回空集）"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "diff", "--name-only", "HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
            )
            stdout, _ = await proc.communicate()
            output = stdout.decode().strip()
            return set(output.split('\n')) if output else set()
        except (FileNotFoundError, OSError):
            return set()

    def _diff_git_status(self, before: set, after: set) -> list:
        """计算 Agent 执行前后的文件变更"""
        return sorted(after - before)

    async def run_with_external_model(self, role: str, prompt: str,
                                       model_key: str, output_file=None,
                                       task_dir=None) -> "AgentResult":
        """使用外部模型执行（限流降级时备选）

        注意：外部模型无法使用 Claude Code 工具，只能产出纯文本。
        适合纯分析/设计类角色，不适合需要写代码的角色。
        """
        model_info = self.EXTERNAL_MODELS.get(model_key)
        if not model_info:
            raise AgentError(f"未知的外部模型: {model_key}")

        start_time = time.time()
        logger.info(f"使用外部模型 [{model_info['label']}] 执行 [{role}]")

        try:
            import openai
            client = openai.AsyncOpenAI(
                api_key=self.config.get("external_api_keys", {}).get(model_key, ""),
                base_url=model_info.get("base_url", "https://api.deepseek.com"),
            )
            response = await client.chat.completions.create(
                model=model_info["model"],
                messages=[{"role": "user", "content": prompt}],
                max_tokens=8192,
            )
            raw_output = response.choices[0].message.content or ""
            input_tokens = response.usage.prompt_tokens if response.usage else 0
            output_tokens = response.usage.completion_tokens if response.usage else 0
        except Exception as e:
            raise AgentError(f"外部模型 [{model_key}] 调用失败: {e}")

        duration = time.time() - start_time

        # 保存输出到文件
        if output_file:
            output_path = Path(output_file)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(raw_output, encoding="utf-8")

        return AgentResult(
            success=True,
            data={"status": "success", "model": model_key, "summary": raw_output[:200]},
            raw_output=raw_output,
            cost_tokens=input_tokens + output_tokens,
            duration=duration,
            exit_code=0,
            model=model_info["model"],
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
