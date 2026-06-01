"""
Settings handler for Vizo Web Console.

Provides PasswordManager for bcrypt-based password storage
with 5-second memory cache, atomic file writes, and graceful
fallback when bcrypt is unavailable.
"""

import os
import time
import asyncio
import hashlib
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from lib.chrome_bridge import read_bridge_state
from lib.openai_compat_bridge import (
    infer_openai_provider_capabilities,
    is_openai_bridge_base_url,
    normalize_openai_compatible_base_url,
    validate_openai_compatible_base_url,
)
from lib.mcp_runtime import (
    LOCKED_MCP_SERVICE_NAMES,
    get_mcp_service_metadata,
    is_chrome_bridge_connected,
    is_locked_mcp_service,
    is_mcp_service_enabled,
    load_mcp_service_state,
    _find_packaged_serena_bin,
    _find_playwright_mcp_bin,
)
from lib.paths import read_data_path, write_data_path

try:
    import bcrypt
except ImportError:
    bcrypt = None  # 降级：bcrypt 不可用时仅支持 token 认证

logger = logging.getLogger("settings")


CODEX_SUPPORTED_PROVIDER_FAMILIES = {"openai", "codex"}
OPENAPI_EXTERNAL_MODEL_UNSUPPORTED_MESSAGE = (
    "OpenAPI 外部模型桥接已移除；外部模型仅支持 Anthropic 兼容接口。"
    "如需 OpenAPI，请改用主会话 OpenAPI 直连或 Codex direct 子代理。"
)
OPENAPI_AUTH_MODE_API_KEY = "api_key"
OPENAPI_AUTH_MODE_ACCOUNT_LOGIN = "account_login"
OPENAPI_AUTH_STATUS_UNKNOWN = "unknown"
OPENAPI_AUTH_STATUS_READY = "ready"
OPENAPI_AUTH_STATUS_MISSING = "missing"
OPENAPI_AUTH_STATUS_EXPIRED = "expired"
OPENAPI_VALID_AUTH_MODES = {OPENAPI_AUTH_MODE_API_KEY, OPENAPI_AUTH_MODE_ACCOUNT_LOGIN}
OPENAPI_VALID_AUTH_STATUSES = {
    OPENAPI_AUTH_STATUS_UNKNOWN,
    OPENAPI_AUTH_STATUS_READY,
    OPENAPI_AUTH_STATUS_MISSING,
    OPENAPI_AUTH_STATUS_EXPIRED,
}


class PasswordManager:
    """Web Console 密码管理：bcrypt 哈希存储 + 5s 内存缓存 + 原子写入"""

    HASH_FILE = "web_console_password.hash"
    CACHE_TTL = 5.0
    BCRYPT_ROUNDS = 12

    def __init__(self, project_root: Path):
        self._project_root = project_root
        self._cached_hash: Optional[bytes] = None
        self._cache_mtime: float = 0.0
        self._cache_time: float = 0.0
        self._lock = asyncio.Lock()

    def _hash_path(self) -> Path:
        return write_data_path(self.HASH_FILE, project_root=self._project_root)

    def has_password(self) -> bool:
        """Check if a bcrypt hash file exists."""
        return self._hash_path().exists()

    def get_stored_hash(self) -> Optional[bytes]:
        """读取 hash 文件（5s 内存缓存 + mtime 校验）"""
        path = read_data_path(self.HASH_FILE, project_root=self._project_root)
        if not path.exists():
            self._cached_hash = None
            return None
        now = time.time()
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return self._cached_hash
        if (self._cached_hash is not None
                and now - self._cache_time < self.CACHE_TTL
                and mtime == self._cache_mtime):
            return self._cached_hash
        try:
            with open(path, 'rb') as f:
                hash_bytes = f.read().strip()
        except OSError:
            return self._cached_hash
        self._cached_hash = hash_bytes
        self._cache_mtime = mtime
        self._cache_time = now
        return hash_bytes

    def verify_password(self, password: str) -> bool:
        """Verify password against stored bcrypt hash."""
        if not bcrypt:
            return False
        stored = self.get_stored_hash()
        if not stored:
            return False
        try:
            return bcrypt.checkpw(password.encode('utf-8'), stored)
        except (ValueError, TypeError):
            return False

    def set_password(self, password: str) -> bytes:
        """原子写入新密码 hash，返回 hash bytes"""
        if not bcrypt:
            raise RuntimeError("bcrypt not installed")
        new_hash = bcrypt.hashpw(
            password.encode('utf-8'),
            bcrypt.gensalt(rounds=self.BCRYPT_ROUNDS)
        )
        path = self._hash_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        with open(tmp, 'wb') as f:
            f.write(new_hash + b'\n')
        os.rename(str(tmp), str(path))
        os.chmod(str(path), 0o600)
        # Update cache
        self._cached_hash = new_hash
        self._cache_mtime = path.stat().st_mtime
        self._cache_time = time.time()
        return new_hash

    def get_cookie_value(self) -> str:
        """bcrypt hash 对应的 cookie 值：sha256(hash_bytes)"""
        stored = self.get_stored_hash()
        if stored:
            return hashlib.sha256(stored).hexdigest()
        return ""

    @staticmethod
    def check_reset_on_startup(project_root: Path):
        """启动时: WEB_CONSOLE_TOKEN 环境变量 + 无 hash 文件 → 设置初始密码

        逻辑：
        - hash 文件不存在 + TOKEN 存在 → 创建初始密码（首次部署）
        - hash 文件已存在 → 跳过（用户已设密码，不覆盖）

        如需强制重置，删除 .vizo/web_console_password.hash 后重启。
        """
        if not bcrypt:
            return
        token = os.environ.get('WEB_CONSOLE_TOKEN', '')
        if not token:
            return
        hash_path = write_data_path("web_console_password.hash", project_root=project_root)
        if hash_path.exists():
            # 用户已有密码，不覆盖
            return
        new_hash = bcrypt.hashpw(token.encode('utf-8'), bcrypt.gensalt(rounds=12))
        hash_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = hash_path.with_suffix('.tmp')
        with open(tmp, 'wb') as f:
            f.write(new_hash + b'\n')
        os.rename(str(tmp), str(hash_path))
        os.chmod(str(hash_path), 0o600)
        logger.info("已通过 WEB_CONSOLE_TOKEN 设置初始密码")


import json
from pathlib import Path as _Path

_CONFIG_FILE = _Path(__file__).resolve().parent.parent / "config.json"


def sync_main_session_to_claude_settings(api_key: str = "", base_url: str = "",
                                         extra_env: Optional[dict[str, str]] = None,
                                         resolved_connection: Optional[dict] = None):
    """将主会话模型配置同步到 ~/.claude/settings.json env 段。

    Claude Code 主会话（PTY 中的 claude 进程）从 ~/.claude/settings.json 的 env 段
    读取 ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL。此函数确保设置面板和首次引导
    的改动同步到该文件，使新会话立即生效。

    使用跨进程文件锁（fcntl.flock）避免与 agent_runner 的临时替换产生竞争。
    """
    from lib.settings_lock import acquire_sync, release as release_lock
    settings_path = _Path.home() / ".claude" / "settings.json"
    lock_fd = None
    try:
        lock_fd = acquire_sync(timeout=5)
    except TimeoutError:
        logger.warning("sync_main_session: 获取 settings.json 文件锁超时，跳过同步")
        return False
    try:
        if settings_path.exists():
            try:
                data = json.loads(settings_path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        else:
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            data = {}
        env = data.setdefault("env", {})
        if resolved_connection is None:
            resolved_connection = resolve_main_session_connection(base_url, current_env=extra_env)
        if is_openai_access_mode(resolved_connection.get("access_mode")):
            for settings_key in MAIN_SESSION_MANAGED_SETTINGS_KEYS:
                data.pop(settings_key, None)
            for env_key in MAIN_SESSION_MANAGED_ENV_KEYS:
                env.pop(env_key, None)
            env.pop("ANTHROPIC_BASE_URL", None)
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
            env.pop("ANTHROPIC_API_KEY", None)
            settings_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            return True
        runtime_base_url = resolved_connection.get("runtime_base_url", base_url)
        if api_key:
            env["ANTHROPIC_AUTH_TOKEN"] = api_key
            env.pop("ANTHROPIC_API_KEY", None)  # 避免两个 key 共存
        if runtime_base_url:
            env["ANTHROPIC_BASE_URL"] = runtime_base_url
        for settings_key in MAIN_SESSION_MANAGED_SETTINGS_KEYS:
            data.pop(settings_key, None)
        if extra_env is not None:
            for env_key in MAIN_SESSION_MANAGED_ENV_KEYS:
                env.pop(env_key, None)
            for env_key, env_val in extract_managed_main_session_env(extra_env).items():
                env[env_key] = env_val
        settings_payload = build_main_session_claude_settings_payload(resolved_connection)
        for settings_key in MAIN_SESSION_MANAGED_SETTINGS_KEYS:
            if settings_key in settings_payload:
                data[settings_key] = settings_payload[settings_key]
        settings_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return True
    finally:
        release_lock(lock_fd)


def resolve_current_main_session_api_key() -> str:
    """读取当前主会话实际应使用的 API Key，过滤掉 __USE_STORED__ 这类哨兵值。"""
    settings_path = _Path.home() / ".claude" / "settings.json"
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            env = data.get("env", {}) if isinstance(data, dict) else {}
            key = str(env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or "").strip()
            if key and key != "__USE_STORED__":
                return key
        except Exception:
            pass

    try:
        from lib.config_loader import (
            ensure_main_session_env_file_migrated,
            load_config,
            read_main_session_api_key_from_env_file,
        )

        ensure_main_session_env_file_migrated()
        key = str(read_main_session_api_key_from_env_file() or "").strip()
        if key and key != "__USE_STORED__":
            return key

        cfg = load_config()
        key = str(cfg.get("external_models", {}).get("anthropic", {}).get("api_key", "") or "").strip()
        if key and key != "__USE_STORED__":
            return key
    except Exception:
        pass
    return ""

# 角色列表及显示顺序（按工作流顺序，与 PRD 一致）
ROLE_DISPLAY = [
    ("requirement_analyst", "需求分析师"),
    ("product_manager", "产品经理"),
    ("architect", "架构师"),
    ("frontend_developer", "前端开发者"),
    ("backend_developer", "后端开发者"),
    ("integration_engineer", "集成工程师"),
    ("qa_engineer", "测试工程师"),
    ("devops_engineer", "部署工程师"),
    ("knowledge_engineer", "知识沉淀师"),
    ("fix_engineer", "修复工程师"),
    ("refactor_engineer", "重构工程师"),
    ("code_reviewer", "代码审查员"),
    ("technical_assessor", "技术评估员"),
    ("embedded_engineer", "嵌入式工程师"),
    ("project_manager", "项目经理"),
    ("assistant", "通用助手"),
    ("handoff_extractor", "交接摘要提取器"),
    ("interaction_design.design_analyst", "交互设计 · 需求分析师"),
    ("interaction_design.interaction_designer", "交互设计 · 交互设计师"),
    ("interaction_design.design_system_curator", "交互设计 · 风格策展师"),
    ("interaction_design.prototype_designer", "交互设计 · 原型设计师"),
]

MAIN_SESSION_MODEL_ID = "__main_session__"
MAIN_SESSION_MODEL_PREFIX = "__main_model__:"
LEGACY_ROLE_MODEL_IDS = {"opus", "sonnet", "haiku"}

MAIN_SESSION_GPT_MODELS = [
    {"model": "gpt-5.5", "display": "GPT-5.5"},
    {"model": "gpt-5.4", "display": "GPT-5.4"},
    {"model": "gpt-5.4-mini", "display": "GPT-5.4 Mini"},
    {"model": "gpt-5.3-codex", "display": "GPT-5.3 Codex"},
    {"model": "gpt-5.3-codex-spark", "display": "GPT-5.3 Codex Spark"},
    {"model": "gpt-5.2", "display": "GPT-5.2"},
]

MAIN_SESSION_CLAUDE_MODELS = [
    {"model": "claude-opus-4-7", "display": "Claude Opus 4.7"},
    {"model": "claude-sonnet-4-7", "display": "Claude Sonnet 4.7"},
    {"model": "claude-haiku-4-7", "display": "Claude Haiku 4.7"},
]

AVAILABLE_MODELS = [
    {"id": MAIN_SESSION_MODEL_ID, "display": "继承主会话模型"},
    {"id": "opus", "display": "Claude Opus 4.7（最强）"},
    {"id": "sonnet", "display": "Claude Sonnet 4.7（均衡）"},
    {"id": "haiku", "display": "Claude Haiku 4.7（快速/低成本）"},
]

VALID_MODEL_IDS = {m["id"] for m in AVAILABLE_MODELS}
VALID_REASONING_EFFORTS = {"inherit", "low", "medium", "high", "xhigh"}
AVAILABLE_REASONING_EFFORTS = [
    {"id": "inherit", "display": "继承主会话"},
    {"id": "low", "display": "低"},
    {"id": "medium", "display": "中"},
    {"id": "high", "display": "高"},
    {"id": "xhigh", "display": "极高"},
]

LEGACY_TIER_DISPLAY = {
    "opus": "Claude Opus 4.7",
    "sonnet": "Claude Sonnet 4.7",
    "haiku": "Claude Haiku 4.7",
}

OFFICIAL_API_MODEL_IDS = {
    "opus": "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-7",
    "haiku": "claude-haiku-4-7",
}

OPENAI_COMPAT_DEFAULT_MODEL_ID = "gpt-5.5"
LEGACY_OPENAI_COMPAT_DEFAULT_MODELS = {
    "default_opus_model": "gpt-5.4",
    "default_sonnet_model": "gpt-5-mini",
    "default_haiku_model": "gpt-5-nano",
}

CLAUDE_MODEL_OVERRIDE_IDS = {
    "opus": (
        "claude-opus-4-7",
        "claude-opus-4-6",
    ),
    "sonnet": (
        "claude-sonnet-4-7",
        "claude-sonnet-4-6",
        "claude-sonnet-4-5-20250929",
    ),
    "haiku": (
        "claude-haiku-4-7",
        "claude-haiku-4-5",
        "claude-haiku-4-5-20251001",
    ),
}

MAIN_SESSION_MODEL_ENV_MAP = {
    "default_opus_model": "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "default_sonnet_model": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "default_haiku_model": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
}

MAIN_SESSION_DEFAULT_TIER = "sonnet"
MAIN_SESSION_MANAGED_SETTINGS_KEYS = {"model", "availableModels", "modelOverrides"}

MAIN_SESSION_PROVIDER_PRESETS = [
    {
        "id": "anthropic",
        "display": "Claude",
        "default_base_url": "https://api.anthropic.com",
        "host_suffixes": ("api.anthropic.com",),
        "access_mode": "anthropic_native",
        "provider_family": "anthropic",
        "env": {},
    },
    {
        "id": "glm",
        "display": "GLM",
        "default_base_url": "https://open.bigmodel.cn/api/anthropic",
        "host_suffixes": ("api.z.ai", "open.bigmodel.cn", "bigmodel.cn"),
        "access_mode": "anthropic_native",
        "provider_family": "glm",
        "env": {
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-4.7",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "glm-4.5-air",
            "API_TIMEOUT_MS": "3000000",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_1M_CONTEXT": "1",
        },
    },
    {
        "id": "deepseek",
        "display": "DeepSeek",
        "default_base_url": "https://api.deepseek.com/anthropic",
        "host_suffixes": ("api.deepseek.com", "deepseek.com"),
        "access_mode": "anthropic_native",
        "provider_family": "deepseek",
        "env": {
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-v4-pro",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-v4-pro",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-v4-flash",
            "ANTHROPIC_MODEL": "deepseek-v4-pro",
            "ANTHROPIC_SMALL_FAST_MODEL": "deepseek-v4-flash",
            "API_TIMEOUT_MS": "600000",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_1M_CONTEXT": "1",
        },
    },
    {
        "id": "qwen",
        "display": "Qwen",
        "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "host_suffixes": ("dashscope.aliyuncs.com",),
        "access_mode": "anthropic_native",
        "provider_family": "qwen",
        "env": {
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "qwen3-max",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "qwen3-max",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "qwen3-max",
            "ANTHROPIC_MODEL": "qwen3-max",
            "ANTHROPIC_SMALL_FAST_MODEL": "qwen3-max",
            "API_TIMEOUT_MS": "3000000",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_1M_CONTEXT": "1",
        },
    },
    {
        "id": "minimax",
        "display": "MiniMax",
        "default_base_url": "https://api.minimaxi.com/anthropic",
        "host_suffixes": ("api.minimaxi.com", "minimaxi.com"),
        "access_mode": "anthropic_native",
        "provider_family": "minimax",
        "env": {
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "MiniMax-M2.7",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "MiniMax-M2.7",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "MiniMax-M2.7",
            "ANTHROPIC_MODEL": "MiniMax-M2.7",
            "ANTHROPIC_SMALL_FAST_MODEL": "MiniMax-M2.7",
            "API_TIMEOUT_MS": "3000000",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_1M_CONTEXT": "1",
        },
    },
    {
        "id": "mimo",
        "display": "MiMo",
        "default_base_url": "https://api.xiaomimimo.com/anthropic",
        "host_suffixes": ("api.xiaomimimo.com", "xiaomimimo.com"),
        "access_mode": "anthropic_native",
        "provider_family": "mimo",
        "env": {
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "mimo-v2-pro",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "mimo-v2-pro",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "mimo-v2-pro",
            "ANTHROPIC_MODEL": "mimo-v2-pro",
            "ANTHROPIC_SMALL_FAST_MODEL": "mimo-v2-pro",
            "API_TIMEOUT_MS": "3000000",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_1M_CONTEXT": "1",
        },
    },
    {
        "id": "openai",
        "display": "OpenAPI",
        "default_base_url": "https://api.openai.com/v1",
        "host_suffixes": ("api.openai.com",),
        "access_mode": "openai_compatible",
        "provider_family": "openai",
        "env": {
            "API_TIMEOUT_MS": "3000000",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_1M_CONTEXT": "1",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": OPENAI_COMPAT_DEFAULT_MODEL_ID,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": OPENAI_COMPAT_DEFAULT_MODEL_ID,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": OPENAI_COMPAT_DEFAULT_MODEL_ID,
        },
    },
    {
        "id": "gateway",
        "display": "Anthropic 兼容接口（映射 GPT / Gemini 等）",
        "default_base_url": "",
        "host_suffixes": (),
        "access_mode": "anthropic_gateway",
        "provider_family": "gateway",
        "env": {
            "API_TIMEOUT_MS": "3000000",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_1M_CONTEXT": "1",
        },
    },
]

MAIN_SESSION_PROVIDER_SELECT_IDS = ("anthropic", "deepseek", "minimax", "mimo", "glm", "openai", "gateway")

MAIN_SESSION_EXTRA_ENV_KEYS = {
    "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "API_TIMEOUT_MS",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "CLAUDE_CODE_DISABLE_1M_CONTEXT",
}

MAIN_SESSION_MANAGED_ENV_KEYS = set(MAIN_SESSION_MODEL_ENV_MAP.values()) | MAIN_SESSION_EXTRA_ENV_KEYS

# 预置外部模型（Anthropic 接口兼容）
PRESET_EXTERNAL_MODELS = [
    {
        "id": "glm",
        "display": "智谱 AI（GLM）",
        "default_base_url": "https://open.bigmodel.cn/api/anthropic",
        "model_placeholder": "GLM-5",
        "access_mode": "anthropic_native",
        "provider_family": "glm",
        "required_env": {
            "API_TIMEOUT_MS": "3000000",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_1M_CONTEXT": "1",
        },
    },
    {
        "id": "deepseek",
        "display": "DeepSeek",
        "default_base_url": "https://api.deepseek.com/anthropic",
        "model_placeholder": "deepseek-v4-pro",
        "access_mode": "anthropic_native",
        "provider_family": "deepseek",
        "required_env": {
            "API_TIMEOUT_MS": "3000000",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_1M_CONTEXT": "1",
        },
    },
    {
        "id": "qwen",
        "display": "阿里云（Qwen）",
        "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model_placeholder": "qwen-max",
        "access_mode": "anthropic_native",
        "provider_family": "qwen",
        "required_env": {
            "API_TIMEOUT_MS": "3000000",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_1M_CONTEXT": "1",
        },
    },
    {
        "id": "minimax",
        "display": "MiniMax",
        "default_base_url": "https://api.minimaxi.com/anthropic",
        "model_placeholder": "MiniMax-M2.7",
        "access_mode": "anthropic_native",
        "provider_family": "minimax",
        "required_env": {
            "API_TIMEOUT_MS": "3000000",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_1M_CONTEXT": "1",
        },
    },
    {
        "id": "mimo",
        "display": "MiMo",
        "default_base_url": "https://api.xiaomimimo.com/anthropic",
        "model_placeholder": "mimo-v2-pro",
        "access_mode": "anthropic_native",
        "provider_family": "mimo",
        "required_env": {
            "API_TIMEOUT_MS": "3000000",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_1M_CONTEXT": "1",
        },
    },
]

PRESET_IDS = {p["id"] for p in PRESET_EXTERNAL_MODELS}

# 预置模型对应的环境变量名（api_key 写 .env 而非 config.json 明文）
PROVIDER_ENV_VAR_MAP = {
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"),
    "deepseek":  ("DEEPSEEK_API_KEY",  None),
    "glm":       ("GLM_API_KEY",       None),
    "qwen":      ("QWEN_API_KEY",      None),
    "minimax":   ("MINIMAX_API_KEY",   None),
    "mimo":      ("MIMO_API_KEY",      None),
}


def normalize_main_session_model_envs(values: dict | None) -> dict[str, str]:
    """将前端字段转换为 settings.json 可写入的环境变量映射。"""
    result = {}
    values = values or {}
    for field, env_key in MAIN_SESSION_MODEL_ENV_MAP.items():
        val = str(values.get(field, "") or "").strip()
        if val:
            result[env_key] = val
    return result


def extract_main_session_model_fields(env: dict | None) -> dict[str, str]:
    """从 env 中提取主会话默认模型映射字段。"""
    env = env or {}
    return {field: str(env.get(env_key, "") or "") for field, env_key in MAIN_SESSION_MODEL_ENV_MAP.items()}


def normalize_openai_compat_default_models(provider_id: str | None, env: dict | None) -> dict[str, str]:
    """将旧版 OpenAI 默认三档模型收敛为单一稳定默认值。"""
    env = dict(env or {})
    if str(provider_id or "").strip() != "openai":
        return env
    current = extract_main_session_model_fields(env)
    if current != LEGACY_OPENAI_COMPAT_DEFAULT_MODELS:
        return env
    for field, env_key in MAIN_SESSION_MODEL_ENV_MAP.items():
        env[env_key] = OPENAI_COMPAT_DEFAULT_MODEL_ID
    return env


def _replace_managed_env(target: dict | None, managed_env: dict[str, str]) -> dict:
    """用新的受管 env 替换目标字典中的受管字段。"""
    result = dict(target or {})
    for env_key in MAIN_SESSION_MANAGED_ENV_KEYS:
        result.pop(env_key, None)
    result.update(managed_env)
    return result


def upgrade_legacy_openai_defaults_in_config(config_data: dict | None) -> tuple[bool, dict | None]:
    """将配置中的旧版 OpenAI 默认映射升级为单一稳定默认值。"""
    config_data = config_data or {}
    changed = False
    active_resolved = None

    for conn in config_data.get("saved_connections", []) or []:
        resolved = resolve_main_session_connection(
            conn.get("base_url", ""),
            values=conn,
            current_env=conn.get("env", {}),
            stored_provider_id=conn.get("provider_id"),
        )
        managed_env = extract_managed_main_session_env(resolved.get("env", {}))
        normalized_env = _replace_managed_env(conn.get("env", {}), managed_env)
        if (
            extract_managed_main_session_env(conn.get("env", {})) != managed_env
            or conn.get("base_url", "") != resolved["base_url"]
            or conn.get("provider_id", "") != resolved["provider_id"]
            or conn.get("access_mode", "") != resolved["access_mode"]
            or conn.get("provider_family", "") != resolved["provider_family"]
            or conn.get("auth_mode", "") != resolved["auth_mode"]
            or conn.get("auth_status", "") != resolved["auth_status"]
            or conn.get("codex_home", "") != resolved["codex_home"]
            or conn.get("auth_last_verified_at", "") != resolved["auth_last_verified_at"]
            or conn.get("auth_account_label", "") != resolved["auth_account_label"]
        ):
            conn["base_url"] = resolved["base_url"]
            conn["provider_id"] = resolved["provider_id"]
            conn["access_mode"] = resolved["access_mode"]
            conn["provider_family"] = resolved["provider_family"]
            _apply_openai_auth_fields(conn, resolved)
            if normalized_env:
                conn["env"] = normalized_env
            else:
                conn.pop("env", None)
            changed = True

    anthropic = config_data.setdefault("external_models", {}).setdefault("anthropic", {})
    if anthropic:
        active_resolved = resolve_main_session_connection(
            anthropic.get("base_url", ""),
            values=anthropic,
            current_env=anthropic.get("env", {}),
            stored_provider_id=anthropic.get("provider_id"),
        )
        managed_env = extract_managed_main_session_env(active_resolved.get("env", {}))
        normalized_env = _replace_managed_env(anthropic.get("env", {}), managed_env)
        if (
            extract_managed_main_session_env(anthropic.get("env", {})) != managed_env
            or anthropic.get("base_url", "") != active_resolved["base_url"]
            or anthropic.get("provider_id", "") != active_resolved["provider_id"]
            or anthropic.get("access_mode", "") != active_resolved["access_mode"]
            or anthropic.get("provider_family", "") != active_resolved["provider_family"]
            or anthropic.get("auth_mode", "") != active_resolved["auth_mode"]
            or anthropic.get("auth_status", "") != active_resolved["auth_status"]
            or anthropic.get("codex_home", "") != active_resolved["codex_home"]
            or anthropic.get("auth_last_verified_at", "") != active_resolved["auth_last_verified_at"]
            or anthropic.get("auth_account_label", "") != active_resolved["auth_account_label"]
        ):
            anthropic["base_url"] = active_resolved["base_url"]
            anthropic["provider_id"] = active_resolved["provider_id"]
            anthropic["access_mode"] = active_resolved["access_mode"]
            anthropic["provider_family"] = active_resolved["provider_family"]
            anthropic["env"] = normalized_env
            _apply_openai_auth_fields(anthropic, active_resolved)
            changed = True

    return changed, active_resolved


def write_config_data(config_data: dict):
    """原子写入 config.json 并刷新配置缓存。"""
    tmp = str(_CONFIG_FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(config_data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, str(_CONFIG_FILE))
    from lib.config_loader import load_config
    load_config(force_reload=True)


def normalize_main_session_base_url(base_url: str | None) -> str:
    """规范化主会话 Base URL；空值回退到 Anthropic 官方地址。"""
    val = str(base_url or "").strip().rstrip("/")
    return val or "https://api.anthropic.com"


def is_openai_access_mode(access_mode: str | None) -> bool:
    """判断是否应通过本地 OpenAI 兼容桥接。"""
    return str(access_mode or "").strip() == "openai_compatible"


def _normalize_provider_base_url(base_url: str | None, default_base_url: str | None = None,
                                 allow_blank: bool = False,
                                 access_mode: str | None = None) -> str:
    """按 provider 规则规范化 Base URL。"""
    if is_openai_access_mode(access_mode):
        return normalize_openai_compatible_base_url(
            base_url,
            default_base_url=default_base_url,
            allow_blank=allow_blank,
        )
    val = str(base_url or "").strip().rstrip("/")
    if val:
        return val
    if default_base_url:
        return str(default_base_url).strip().rstrip("/")
    if allow_blank:
        return ""
    return "https://api.anthropic.com"


def _extract_base_url_host(base_url: str | None) -> str:
    """提取 Base URL 的主机名。"""
    try:
        return (urlparse(str(base_url or "").strip()).hostname or "").lower()
    except Exception:
        return ""


def _base_url_matches_preset(base_url: str | None, preset: dict | None) -> bool:
    """判断 Base URL 是否匹配某个 provider 预设域名。"""
    if not preset:
        return False
    host = _extract_base_url_host(base_url)
    if not host:
        return False
    for suffix in preset.get("host_suffixes", ()):
        suffix = str(suffix or "").lower()
        if not suffix:
            continue
        if host == suffix or host.endswith("." + suffix):
            return True
    return False


def _detect_concrete_main_session_provider(base_url: str | None) -> dict | None:
    """Return a vendor preset for known Anthropic-compatible hosts."""
    for preset in MAIN_SESSION_PROVIDER_PRESETS:
        if preset["id"] in {"anthropic", "gateway", "openai"}:
            continue
        if _base_url_matches_preset(base_url, preset):
            return preset
    return None


def validate_main_session_base_url(base_url: str | None) -> str:
    """校验 Claude / Anthropic 类型主连接的 Base URL。"""
    val = str(base_url or "").strip()
    if not val:
        return ""
    try:
        path = (urlparse(val).path or "").rstrip("/").lower()
    except Exception:
        return ""
    if not path:
        return ""
    if path.endswith("/v1"):
        return "看起来像 OpenAPI 接口入口（/v1 或 /codex/v1）。如果你要接 OpenAPI，请把接入方式切到 OpenAPI；Claude / Anthropic 类型仍应填写 /claude、/anthropic 这类根路径。"
    endpoint_suffixes = (
        "/messages",
        "/responses",
        "/completions",
        "/chat/completions",
        "/v1/messages",
        "/v1/responses",
        "/v1/completions",
        "/v1/chat/completions",
    )
    if any(path.endswith(suffix) for suffix in endpoint_suffixes):
        return "这看起来是 OpenAI / Codex 的具体 endpoint。无论主会话最终走 Codex direct 还是 Claude / Anthropic 兼容链路，都应该填写入口根路径，而不是 /messages、/responses 或 /chat/completions 这类具体地址。"
    return ""


def validate_main_session_base_url_for_access_mode(base_url: str | None,
                                                   access_mode: str | None = None) -> str:
    """按接入方式校验主会话 Base URL。"""
    if is_openai_access_mode(access_mode):
        return validate_openai_compatible_base_url(base_url).replace("OpenAI 兼容接口", "OpenAPI")
    return validate_main_session_base_url(base_url)


def looks_like_claude_relay_base_url(base_url: str | None) -> bool:
    """判断 Base URL 是否更像 Claude 中转地址。"""
    try:
        path = (urlparse(str(base_url or "").strip()).path or "").rstrip("/").lower()
    except Exception:
        return False
    return path == "/claude" or path.startswith("/claude/")


def validate_main_session_connection_requirements(resolved_connection: dict | None) -> str:
    """校验主会话连接的模型映射与接入类型是否匹配。"""
    resolved_connection = resolved_connection or {}
    provider_id = str(resolved_connection.get("provider_id") or "").strip()
    access_mode = str(resolved_connection.get("access_mode") or "").strip()
    model_fields = resolved_connection.get("model_fields") or {}
    has_any_mapping = any(str(val or "").strip() for val in model_fields.values())
    if provider_id == "gateway" and not has_any_mapping:
        return "Anthropic 兼容接口至少填写一个模型 ID 映射。"
    if is_openai_access_mode(access_mode):
        caps = resolved_connection.get("provider_capabilities") or {}
        provider_family = str(
            caps.get("provider_family")
            or resolved_connection.get("provider_family")
            or ""
        ).strip()
        if provider_family in {"gemini", "openrouter"}:
            return "OpenAPI 仅支持 OpenAI 官方 API 或 OpenAI / Codex 中转入口，不支持 Gemini / OpenRouter 这类兼容接口。"
        auth_mode = str(resolved_connection.get("auth_mode") or "").strip()
        if auth_mode == OPENAPI_AUTH_MODE_ACCOUNT_LOGIN and not supports_openai_account_login(resolved_connection):
            return "OpenAPI 账号登录目前仅支持 OpenAI 官方 API，请改用 API Key。"
    return ""


def build_main_session_runtime_base_url(resolved_connection: dict | None) -> str:
    """返回 Claude Code 运行时真正要访问的 Base URL。"""
    resolved_connection = resolved_connection or {}
    base_url = str(resolved_connection.get("base_url") or "").strip()
    if is_openai_access_mode(resolved_connection.get("access_mode")):
        return ""
    return base_url


def get_external_model_stored_base_url(model_id: str, cfg: dict | None,
                                       default_base_url: str = "") -> str:
    """读取外部模型配置里展示给用户的上游 Base URL。"""
    cfg = cfg or {}
    env = cfg.get("env", {}) or {}
    if cfg.get("base_url"):
        return str(cfg.get("base_url") or "").strip()
    if env.get("ANTHROPIC_BASE_URL"):
        return str(env.get("ANTHROPIC_BASE_URL") or "").strip()
    preset = next((p for p in PRESET_EXTERNAL_MODELS if p["id"] == model_id), None)
    if preset and preset.get("default_base_url"):
        return str(preset["default_base_url"]).strip()
    return str(default_base_url or "").strip()


def get_external_model_runtime_base_url(model_id: str, cfg: dict | None,
                                        default_base_url: str = "") -> str:
    """返回外部模型在 Claude Code 运行时应使用的 Base URL。"""
    cfg = cfg or {}
    stored_base_url = get_external_model_stored_base_url(model_id, cfg, default_base_url=default_base_url)
    meta = infer_external_model_metadata(model_id, cfg)
    if is_openai_access_mode(meta.get("access_mode")):
        return ""
    return stored_base_url


def _connection_host_label(base_url: str | None) -> str:
    """提取快捷连接的简短主机标签。"""
    host = _extract_base_url_host(base_url)
    if not host:
        return "anthropic"
    host = host.split(":", 1)[0]
    parts = [part for part in host.split(".") if part]
    while parts and parts[0] in {"api", "open"}:
        parts.pop(0)
    return parts[0] if parts else host


def _new_saved_connection_id() -> str:
    """生成快捷连接的稳定唯一 ID。"""
    return "conn_" + uuid.uuid4().hex[:12]


def get_main_session_provider_preset(provider_id: str | None) -> dict | None:
    """按 provider_id 获取主会话 provider 预设。"""
    provider_id = str(provider_id or "").strip().lower()
    if not provider_id:
        return None
    return next((p for p in MAIN_SESSION_PROVIDER_PRESETS if p["id"] == provider_id), None)


def get_main_session_provider_options() -> list[dict]:
    """返回前端主会话 provider 下拉选项。"""
    options = []
    for provider_id in MAIN_SESSION_PROVIDER_SELECT_IDS:
        preset = get_main_session_provider_preset(provider_id)
        if not preset:
            continue
        model_fields = extract_main_session_model_fields(preset.get("env", {}))
        options.append({
            "id": preset["id"],
            "display": preset["display"],
            "default_base_url": preset["default_base_url"],
            "access_mode": preset.get("access_mode", "anthropic_native"),
            "provider_family": preset.get("provider_family", preset["id"]),
            "supports_account_login": bool(
                preset["id"] == "openai"
                and preset.get("access_mode") == "openai_compatible"
                and preset.get("provider_family") == "openai"
            ),
            "default_models": {
                "opus": model_fields.get("default_opus_model", ""),
                "sonnet": model_fields.get("default_sonnet_model", ""),
                "haiku": model_fields.get("default_haiku_model", ""),
            },
        })
    return options


def extract_managed_main_session_env(env: dict | None) -> dict[str, str]:
    """提取由 WebConsole 管理的主会话 env 子集。"""
    result = {}
    for key, val in (env or {}).items():
        sval = str(val or "").strip()
        if key in MAIN_SESSION_MANAGED_ENV_KEYS and sval:
            result[key] = sval
    return result


def _fill_routing_models(provider_id: str, model_fields: dict[str, str]) -> dict[str, str]:
    """将 tier 映射补齐；非 Claude 连接若只填了一个模型，则其余 tier 复用该模型。"""
    by_tier = {
        "opus": model_fields.get("default_opus_model", ""),
        "sonnet": model_fields.get("default_sonnet_model", ""),
        "haiku": model_fields.get("default_haiku_model", ""),
    }
    if provider_id == "anthropic":
        return {tier: by_tier[tier] or tier for tier in ("opus", "sonnet", "haiku")}
    shared = next((by_tier[tier] for tier in ("opus", "sonnet", "haiku") if by_tier[tier]), "")
    if shared:
        return {tier: by_tier[tier] or shared for tier in ("opus", "sonnet", "haiku")}
    return {tier: by_tier[tier] or tier for tier in ("opus", "sonnet", "haiku")}


def build_main_session_model_overrides(routing_models: dict[str, str]) -> dict[str, str]:
    """为 Claude Code CLI 生成安全的 modelOverrides，避免切换到 claude-* 原始 ID。"""
    overrides = {}
    for tier, actual_model in (routing_models or {}).items():
        if not actual_model or actual_model == tier:
            continue
        for official_id in CLAUDE_MODEL_OVERRIDE_IDS.get(tier, ()):
            overrides[official_id] = actual_model
    return overrides


def infer_main_session_display_model(provider_model: str | None, fallback: str = "") -> str:
    """根据 Claude 实际返回的 provider model 反推主会话 tier 标识。"""
    candidate = str(provider_model or "").strip()
    if candidate in VALID_MODEL_IDS:
        return candidate
    for tier, model_ids in CLAUDE_MODEL_OVERRIDE_IDS.items():
        if candidate in model_ids:
            return tier
    fallback_value = str(fallback or "").strip()
    if fallback_value in VALID_MODEL_IDS:
        return fallback_value
    return MAIN_SESSION_DEFAULT_TIER


def build_main_session_claude_settings_payload(resolved_connection: dict | None,
                                               preferred_tier: str = MAIN_SESSION_DEFAULT_TIER) -> dict:
    """生成需要写入 ~/.claude/settings.json 的受管配置。"""
    resolved_connection = resolved_connection or {}
    env = extract_managed_main_session_env(resolved_connection.get("env", {}))
    payload = {"env": env}
    provider_id = resolved_connection.get("provider_id", "anthropic")
    model_fields = resolved_connection.get("model_fields") or extract_main_session_model_fields(env)
    has_custom_mapping = any(model_fields.values())
    payload["model"] = preferred_tier if preferred_tier in VALID_MODEL_IDS else MAIN_SESSION_DEFAULT_TIER
    payload["availableModels"] = [item["id"] for item in AVAILABLE_MODELS]
    # 即使是官方 Claude / Claude 中转（保留官方别名），也要显式写回 tier 列表，
    # 避免 Claude CLI 继承上一个 OpenAI 兼容连接残留的 gpt-* 选项。
    if provider_id == "anthropic" or (provider_id == "custom" and not has_custom_mapping):
        return payload
    model_overrides = build_main_session_model_overrides(resolved_connection.get("routing_models", {}))
    if model_overrides:
        payload["modelOverrides"] = model_overrides
    return payload


def describe_connection_mode(provider_id: str | None, provider_display: str | None = None) -> str:
    """返回快捷连接展示用的中文模式标签。"""
    provider_id = str(provider_id or "").strip()
    if provider_id == "anthropic":
        return "Claude"
    if provider_id == "custom":
        return "Claude 中转"
    if provider_id == "openai":
        return "OpenAPI"
    if provider_id == "gateway":
        return "Anthropic 兼容接口 · 手动映射"
    display = str(provider_display or provider_id or "Claude")
    return f"Anthropic 兼容接口 · {display}"


def _sanitize_connection_segment(raw: str | None, fallback: str = "main_session") -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in str(raw or "").strip())
    cleaned = cleaned.strip(".-")
    return cleaned or fallback


def build_connection_codex_home(connection_id: str | None, fallback: str = "main_session") -> str:
    clean_id = _sanitize_connection_segment(connection_id, fallback)
    return (Path(".vizo") / "codex" / "connections" / clean_id).as_posix()


def normalize_openai_auth_mode(
    auth_mode: str | None,
    *,
    access_mode: str | None,
) -> str:
    if not is_openai_access_mode(access_mode):
        return ""
    mode = str(auth_mode or "").strip().lower()
    if mode in OPENAPI_VALID_AUTH_MODES:
        return mode
    return OPENAPI_AUTH_MODE_API_KEY


def normalize_openai_auth_status(
    auth_status: str | None,
    *,
    auth_mode: str,
    api_key: str = "",
) -> str:
    if not auth_mode:
        return ""
    if auth_mode == OPENAPI_AUTH_MODE_API_KEY:
        return OPENAPI_AUTH_STATUS_READY if str(api_key or "").strip() else OPENAPI_AUTH_STATUS_MISSING
    status = str(auth_status or "").strip().lower()
    if status in OPENAPI_VALID_AUTH_STATUSES:
        return status
    return OPENAPI_AUTH_STATUS_UNKNOWN


def supports_openai_account_login(resolved_connection: dict | None) -> bool:
    resolved_connection = resolved_connection or {}
    if not is_openai_access_mode(resolved_connection.get("access_mode")):
        return False
    if str(resolved_connection.get("provider_id") or "").strip() != "openai":
        return False
    caps = resolved_connection.get("provider_capabilities") or {}
    provider_family = str(
        caps.get("provider_family")
        or resolved_connection.get("provider_family")
        or ""
    ).strip()
    return provider_family == "openai"


def _apply_openai_auth_fields(target: dict, resolved_connection: dict) -> None:
    """将 OpenAPI 认证字段回填到配置对象；非 OpenAPI 连接则清理遗留字段。"""
    if resolved_connection.get("auth_mode"):
        target["auth_mode"] = resolved_connection["auth_mode"]
        target["auth_status"] = resolved_connection["auth_status"]
        target["codex_home"] = resolved_connection["codex_home"]
        target["auth_last_verified_at"] = resolved_connection.get("auth_last_verified_at", "")
        target["auth_account_label"] = resolved_connection.get("auth_account_label", "")
        return
    target.pop("auth_mode", None)
    target.pop("auth_status", None)
    target.pop("codex_home", None)
    target.pop("auth_last_verified_at", None)
    target.pop("auth_account_label", None)


def infer_external_model_metadata(model_id: str, cfg: dict | None) -> dict[str, str]:
    """推断外部模型的接入方式与展示来源。"""
    cfg = cfg or {}
    preset = next((p for p in PRESET_EXTERNAL_MODELS if p["id"] == model_id), None)
    base_url = get_external_model_stored_base_url(model_id, cfg)
    host_meta = detect_main_session_provider(base_url) if base_url else None
    access_mode = str(cfg.get("access_mode") or "").strip()
    provider_family = str(cfg.get("provider_family") or "").strip()

    if preset:
        access_mode = access_mode or preset.get("access_mode", "anthropic_native")
        provider_family = provider_family or preset.get("provider_family", preset["id"])
        source_display = preset["display"]
    else:
        access_mode = access_mode or (host_meta or {}).get("access_mode") or "anthropic_gateway"
        provider_family = provider_family or (host_meta or {}).get("provider_family") or "custom"
        if access_mode == "openai_compatible":
            provider_caps = infer_openai_provider_capabilities(base_url, provider_family=provider_family)
            provider_family = provider_caps["provider_family"]
            source_display = provider_caps["display_name"]
        elif access_mode == "anthropic_gateway":
            source_display = "Anthropic 兼容接口"
        elif host_meta and host_meta.get("id") not in ("anthropic", "custom", "gateway"):
            source_display = host_meta["display"]
        else:
            source_display = "Anthropic 兼容接口"

    return {
        "access_mode": access_mode,
        "provider_family": provider_family,
        "source_display": source_display,
    }


def detect_main_session_provider(base_url: str | None, provider_id: str | None = None) -> dict:
    """根据 Base URL 推断主会话 provider。"""
    preset = get_main_session_provider_preset(provider_id)
    if preset and preset["id"] == "gateway":
        concrete_preset = _detect_concrete_main_session_provider(base_url)
        if concrete_preset:
            preset = concrete_preset
    if preset:
        default_base_url = preset.get("default_base_url", "")
        normalized = _normalize_provider_base_url(
            base_url,
            default_base_url=default_base_url,
            allow_blank=not bool(default_base_url),
            access_mode=preset.get("access_mode"),
        )
        # Anthropic 官方 provider 仅用于官方域名；第三方 Claude 代理应归为 custom。
        if preset["id"] != "anthropic" or not base_url or _base_url_matches_preset(normalized, preset):
            return {
                "id": preset["id"],
                "display": preset["display"],
                "base_url": normalized,
                "default_base_url": default_base_url,
                "access_mode": preset.get("access_mode", "anthropic_native"),
                "provider_family": preset.get("provider_family", preset["id"]),
                "env": dict(preset["env"]),
            }

    normalized = _normalize_provider_base_url(base_url)
    host = _extract_base_url_host(normalized)
    for preset in MAIN_SESSION_PROVIDER_PRESETS:
        for suffix in preset["host_suffixes"]:
            suffix = suffix.lower()
            if host == suffix or host.endswith("." + suffix):
                return {
                    "id": preset["id"],
                    "display": preset["display"],
                    "base_url": normalized,
                    "default_base_url": preset["default_base_url"],
                    "access_mode": preset.get("access_mode", "anthropic_native"),
                    "provider_family": preset.get("provider_family", preset["id"]),
                    "env": dict(preset["env"]),
                }
    return {
        "id": "custom",
        "display": "Claude 中转平台",
        "base_url": normalized,
        "default_base_url": normalized,
        "access_mode": "anthropic_native",
        "provider_family": "custom",
        "env": {},
    }


def resolve_main_session_connection(base_url: str | None, values: dict | None = None,
                                    current_env: dict | None = None,
                                    stored_provider_id: str | None = None) -> dict:
    """解析主会话连接：自动识别 provider，并生成受管 env。"""
    values = values or {}
    if current_env is None and isinstance(values.get("env"), dict):
        current_env = values.get("env")
    provider_hint = values.get("provider_id") or stored_provider_id
    provider = detect_main_session_provider(base_url, provider_id=provider_hint)
    managed_env = dict(provider["env"])
    managed_env.update(extract_managed_main_session_env(current_env))

    # 兼容旧调用方：若显式传入 default_* 字段，则覆盖自动映射。
    manual_model_env = normalize_main_session_model_envs(values)
    if manual_model_env:
        managed_env.update(manual_model_env)
    managed_env = normalize_openai_compat_default_models(provider["id"], managed_env)

    model_fields = extract_main_session_model_fields(managed_env)
    routing_models = _fill_routing_models(provider["id"], model_fields)
    provider_capabilities = {}
    if provider.get("access_mode") == "openai_compatible":
        provider_capabilities = infer_openai_provider_capabilities(
            provider["base_url"],
            provider_family=provider.get("provider_family"),
        )
    connection_id = str(values.get("connection_id") or values.get("id") or "").strip()
    api_key = str(values.get("api_key") or "").strip()
    auth_mode = normalize_openai_auth_mode(
        values.get("auth_mode"),
        access_mode=provider.get("access_mode"),
    )
    auth_status = normalize_openai_auth_status(
        values.get("auth_status"),
        auth_mode=auth_mode,
        api_key=api_key,
    )
    auth_last_verified_at = str(values.get("auth_last_verified_at") or "").strip() if auth_mode else ""
    auth_account_label = str(values.get("auth_account_label") or "").strip() if auth_mode else ""
    codex_home = ""
    if is_openai_access_mode(provider.get("access_mode")):
        codex_home = (
            str(values.get("codex_home") or "").strip()
            or build_connection_codex_home(connection_id, fallback="main_session")
        )
    account_login_supported = supports_openai_account_login(
        {
            "provider_id": provider["id"],
            "access_mode": provider.get("access_mode", "anthropic_native"),
            "provider_family": provider.get("provider_family", provider["id"]),
            "provider_capabilities": provider_capabilities,
        }
    )
    runtime_base_url = build_main_session_runtime_base_url({
        "base_url": provider["base_url"],
        "access_mode": provider.get("access_mode"),
    })
    if provider["id"] == "anthropic":
        summary = "Claude 官方接口，无需额外模型 ID 映射。"
    elif provider["id"] == "openai":
        summary = (
            f"通过 OpenAPI 直连接入：Opus → {routing_models['opus']}，"
            f"Sonnet → {routing_models['sonnet']}，"
            f"Haiku → {routing_models['haiku']}。"
        )
    elif provider["id"] == "gateway":
        summary = (
            f"通过 Anthropic 兼容接口接入：Opus → {routing_models['opus']}，"
            f"Sonnet → {routing_models['sonnet']}，"
            f"Haiku → {routing_models['haiku']}。"
        )
    elif provider["id"] == "custom":
        if any(model_fields.values()):
            summary = (
                f"当前 Claude 中转平台映射：Opus → {routing_models['opus']}，"
                f"Sonnet → {routing_models['sonnet']}，"
                f"Haiku → {routing_models['haiku']}。"
            )
        else:
            summary = "Claude 中转平台默认保留 Claude 官方模型别名。"
    else:
        summary = (
            f"已识别为 {provider['display']}，自动映射："
            f"Opus → {routing_models['opus']}，"
            f"Sonnet → {routing_models['sonnet']}，"
            f"Haiku → {routing_models['haiku']}。"
        )

    return {
        "provider_id": provider["id"],
        "provider_display": provider["display"],
        "base_url": provider["base_url"],
        "default_base_url": provider.get("default_base_url", provider["base_url"]),
        "runtime_base_url": runtime_base_url,
        "access_mode": provider.get("access_mode", "anthropic_native"),
        "provider_family": provider.get("provider_family", provider["id"]),
        "provider_capabilities": provider_capabilities,
        "env": managed_env,
        "model_fields": model_fields,
        "routing_models": routing_models,
        "routing_summary": summary,
        "connection_id": connection_id,
        "auth_mode": auth_mode,
        "auth_status": auth_status,
        "auth_last_verified_at": auth_last_verified_at,
        "auth_account_label": auth_account_label,
        "codex_home": codex_home,
        "account_login_supported": account_login_supported,
    }


def get_main_session_api_model(base_url: str | None, values: dict | None = None,
                               current_env: dict | None = None, prefer: str = "opus",
                               stored_provider_id: str | None = None) -> str:
    """返回原始 Anthropic Messages API 可用的模型名。"""
    prefer = prefer if prefer in OFFICIAL_API_MODEL_IDS else "opus"
    if is_openai_bridge_base_url(base_url):
        try:
            from lib.config_loader import load_config

            config = load_config()
            anthropic_cfg = config.get("external_models", {}).get("anthropic", {})
            base_url = anthropic_cfg.get("base_url", "")
            current_env = anthropic_cfg.get("env", {}) or current_env
            stored_provider_id = anthropic_cfg.get("provider_id") or stored_provider_id
        except Exception:
            pass
    resolved = resolve_main_session_connection(
        base_url,
        values=values,
        current_env=current_env,
        stored_provider_id=stored_provider_id,
    )
    model_fields = resolved["model_fields"]
    field_by_tier = {
        "opus": "default_opus_model",
        "sonnet": "default_sonnet_model",
        "haiku": "default_haiku_model",
    }
    explicit_model = model_fields.get(field_by_tier[prefer], "")
    if explicit_model:
        return explicit_model
    if resolved["provider_id"] == "anthropic":
        return OFFICIAL_API_MODEL_IDS[prefer]
    if resolved["provider_id"] == "custom" and not any(model_fields.values()):
        return OFFICIAL_API_MODEL_IDS[prefer]
    routed_model = resolved["routing_models"].get(prefer, "")
    return routed_model or OFFICIAL_API_MODEL_IDS[prefer]


def get_subagent_codex_candidate_roles() -> tuple[str, ...]:
    """返回会随 OpenAI/Codex 主会话进入 Codex runtime 评估的角色。"""
    return tuple(DEFAULT_MODELS.keys())


def get_subagent_codex_pilot_roles() -> tuple[str, ...]:
    """兼容旧诊断调用；Codex 子代理已从试点白名单切换为按连接能力选择。"""
    return get_subagent_codex_candidate_roles()


def resolve_subagent_requested_model(config: dict | None, role: str, model_override: str | None = None) -> str:
    """解析子代理请求模型，保持与角色默认映射一致。"""
    config = config or {}
    if model_override:
        return str(model_override).strip()
    return str(
        config.get("model_overrides", {}).get(role)
        or DEFAULT_MODELS.get(role, "sonnet")
    ).strip()


def _read_main_session_direct_api_key(config: dict | None) -> str:
    """读取主会话 OpenAI-compatible 直连所需的 API Key。"""
    config = config or {}
    main_cfg = config.get("external_models", {}).get("anthropic", {}) or {}
    env = main_cfg.get("env", {}) or {}
    key = str(
        env.get("ANTHROPIC_API_KEY")
        or env.get("OPENAI_API_KEY")
        or main_cfg.get("api_key")
        or ""
    ).strip()
    if key:
        return key
    try:
        from lib.config_loader import read_main_session_api_key_from_env_file

        return str(read_main_session_api_key_from_env_file() or "").strip()
    except Exception:
        return ""


def _read_main_session_model_snapshot() -> str:
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
        if not isinstance(data, dict):
            continue
        model = str(data.get("provider_model") or data.get("display_model") or "").strip()
        if model:
            return model
    return ""


def resolve_subagent_codex_candidate(
    config: dict | None,
    *,
    role: str,
    model_override: str | None = None,
) -> dict[str, Any]:
    """解析当前子代理是否具备 Codex direct 试点条件。

    返回结果供 runtime policy 和 Codex adapter 共用。`profile` 中可能包含
    敏感认证信息，只能在服务端内部短链使用，不能写入持久化元数据。
    """

    from lib.runtime.cli_discovery import cli_available

    config = config or {}
    diagnostics: list[str] = []
    requested_model = resolve_subagent_requested_model(config, role, model_override=model_override)
    selected_model = requested_model
    profile: dict[str, Any] | None = None
    provider_caps: dict[str, Any] = {}
    provider_family = ""
    provider_display = ""
    access_mode = ""
    source = ""
    base_url = ""
    api_key = ""
    auth_mode = ""
    auth_status = ""
    codex_home = ""
    use_default_auth = False

    diagnostics.append("codex_role_evaluated")
    ext_models = config.get("external_models", {}) or {}
    ext_cfg = ext_models.get(requested_model) or {}

    if requested_model in ext_models and requested_model != "anthropic":
        source = "external_model"
        diagnostics.append("codex_candidate_from_external_model")
        meta = infer_external_model_metadata(requested_model, ext_cfg)
        access_mode = str(meta.get("access_mode") or "").strip()
        if access_mode == "openai_compatible":
            diagnostics.append("openai_external_model_bridge_removed")
            return {
                "requested_model": requested_model,
                "selected_model": selected_model,
                "profile": None,
                "diagnostics": tuple(diagnostics),
                "blocking_issues": ("openai_external_model_bridge_removed",),
                "source": source,
                "provider_family": "",
                "provider_display": "",
                "access_mode": access_mode,
            }
        if meta.get("access_mode") != "openai_compatible":
            diagnostics.append("codex_external_model_not_openai_compatible")
            return {
                "requested_model": requested_model,
                "selected_model": selected_model,
                "profile": None,
                "diagnostics": tuple(diagnostics),
                "source": source,
                "provider_family": "",
                "provider_display": "",
                "access_mode": access_mode,
            }
        base_url = normalize_openai_compatible_base_url(
            ext_cfg.get("base_url") or ext_cfg.get("env", {}).get("ANTHROPIC_BASE_URL") or ""
        )
        provider_caps = infer_openai_provider_capabilities(
            base_url,
            provider_family=meta.get("provider_family"),
        )
        provider_family = str(provider_caps.get("provider_family") or "").strip()
        provider_display = str(
            provider_caps.get("display_name") or provider_family or "OpenAI-compatible"
        ).strip()
        selected_model = str(ext_cfg.get("cli_model") or requested_model).strip()
        api_key = str(
            ext_cfg.get("api_key")
            or ext_cfg.get("env", {}).get("ANTHROPIC_API_KEY")
            or ext_cfg.get("env", {}).get("OPENAI_API_KEY")
            or ""
        ).strip()
    else:
        source = "main_session"
        diagnostics.append("codex_candidate_from_main_session")
        main_cfg = ext_models.get("anthropic", {}) or {}
        resolved = resolve_main_session_connection(
            main_cfg.get("base_url", ""),
            values=main_cfg,
            current_env=main_cfg.get("env", {}),
            stored_provider_id=main_cfg.get("provider_id"),
        )
        access_mode = str(resolved.get("access_mode") or "").strip()
        if resolved.get("access_mode") != "openai_compatible":
            diagnostics.append("codex_main_session_not_openai_compatible")
            return {
                "requested_model": requested_model,
                "selected_model": selected_model,
                "profile": None,
                "diagnostics": tuple(diagnostics),
                "source": source,
                "provider_family": "",
                "provider_display": "",
                "access_mode": access_mode,
            }
        base_url = normalize_openai_compatible_base_url(resolved.get("base_url", ""))
        provider_caps = resolved.get("provider_capabilities") or infer_openai_provider_capabilities(
            base_url,
            provider_family=resolved.get("provider_family"),
        )
        provider_family = str(provider_caps.get("provider_family") or "").strip()
        provider_display = str(
            provider_caps.get("display_name")
            or resolved.get("provider_display")
            or provider_family
            or "OpenAI-compatible"
        ).strip()
        auth_mode = str(resolved.get("auth_mode") or "").strip()
        auth_status = str(resolved.get("auth_status") or "").strip()
        codex_home = str(resolved.get("codex_home") or "").strip()
        explicit_main_model = unwrap_main_session_role_model_id(requested_model)
        if explicit_main_model:
            selected_model = explicit_main_model
        elif requested_model == MAIN_SESSION_MODEL_ID:
            selected_model = _read_main_session_model_snapshot() or get_main_session_api_model(
                main_cfg.get("base_url", ""),
                values=main_cfg,
                current_env=main_cfg.get("env", {}),
                prefer=MAIN_SESSION_DEFAULT_TIER,
                stored_provider_id=main_cfg.get("provider_id"),
            )
        else:
            selected_model = get_main_session_api_model(
                main_cfg.get("base_url", ""),
                values=main_cfg,
                current_env=main_cfg.get("env", {}),
                prefer=requested_model,
                stored_provider_id=main_cfg.get("provider_id"),
            )
        if auth_mode == OPENAPI_AUTH_MODE_ACCOUNT_LOGIN:
            if not bool(resolved.get("account_login_supported")):
                diagnostics.append("codex_account_login_unsupported")
            elif auth_status != OPENAPI_AUTH_STATUS_READY:
                diagnostics.append("codex_account_login_missing")
            else:
                use_default_auth = True
        else:
            api_key = _read_main_session_direct_api_key(config)

    if is_openai_bridge_base_url(base_url):
        diagnostics.append("codex_direct_requires_upstream_base_url")
        base_url = ""
    if not base_url:
        diagnostics.append("codex_base_url_missing")
    if provider_family not in CODEX_SUPPORTED_PROVIDER_FAMILIES:
        diagnostics.append("codex_provider_family_unsupported")
    if not provider_caps.get("supports_responses_api"):
        diagnostics.append("codex_responses_api_required")
    if not selected_model:
        diagnostics.append("codex_selected_model_missing")
    if not api_key and not use_default_auth:
        diagnostics.append("codex_api_key_missing")
    if not cli_available("codex"):
        diagnostics.append("codex_cli_missing")

    ready = not any(
        item in diagnostics
        for item in (
            "codex_direct_requires_upstream_base_url",
            "codex_base_url_missing",
            "codex_provider_family_unsupported",
            "codex_responses_api_required",
            "codex_selected_model_missing",
            "codex_account_login_unsupported",
            "codex_account_login_missing",
            "codex_api_key_missing",
            "codex_cli_missing",
        )
    )

    if ready:
        diagnostics.append("codex_direct_profile_ready")
        profile = {
            "source": source,
            "provider_family": provider_family,
            "provider_display": provider_display or provider_family or "OpenAI-compatible",
            "base_url": base_url,
            "api_key": api_key,
            "selected_model": selected_model,
            "requested_model": requested_model,
            "wire_api": "responses",
            "use_default_auth": use_default_auth,
            "codex_home": codex_home,
        }

    return {
        "requested_model": requested_model,
        "selected_model": selected_model,
        "profile": profile,
        "diagnostics": tuple(diagnostics),
        "source": source,
        "provider_family": provider_family,
        "provider_display": provider_display,
        "access_mode": access_mode,
    }

def _mask_key(key: str) -> str:
    """脱敏：保留前 8 位 + 后 4 位"""
    if len(key) <= 12:
        return key[:4] + "••••"
    return key[:8] + "••••" + key[-4:]


def _read_api_key_for_model(mid: str, cfg: dict) -> str:
    """读取外部模型的 API Key：
    优先 env.ANTHROPIC_API_KEY / top-level api_key，
    预置模型还需从 .env 文件读取（PROVIDER_ENV_VAR_MAP）。
    """
    key = cfg.get("env", {}).get("ANTHROPIC_API_KEY", "") or cfg.get("api_key", "")
    if not key and mid in PROVIDER_ENV_VAR_MAP:
        env_var = PROVIDER_ENV_VAR_MAP[mid][0]
        try:
            from lib.config_loader import _read_env_file
            env_vals = _read_env_file()
            key = env_vals.get(env_var, "")
        except Exception:
            pass
    return key

# 默认模型映射（从 agent_runner.DEFAULT_MODELS 同步，保持一致）
DEFAULT_MODELS = {
    "requirement_analyst": MAIN_SESSION_MODEL_ID,
    "product_manager": MAIN_SESSION_MODEL_ID,
    "architect": MAIN_SESSION_MODEL_ID,
    "frontend_developer": MAIN_SESSION_MODEL_ID,
    "backend_developer": MAIN_SESSION_MODEL_ID,
    "qa_engineer": MAIN_SESSION_MODEL_ID,
    "devops_engineer": MAIN_SESSION_MODEL_ID,
    "knowledge_engineer": MAIN_SESSION_MODEL_ID,
    "code_reviewer": MAIN_SESSION_MODEL_ID,
    "refactor_engineer": MAIN_SESSION_MODEL_ID,
    "handoff_extractor": MAIN_SESSION_MODEL_ID,
    "interaction_design.design_analyst": MAIN_SESSION_MODEL_ID,
    "interaction_design.interaction_designer": MAIN_SESSION_MODEL_ID,
    "interaction_design.design_system_curator": MAIN_SESSION_MODEL_ID,
    "interaction_design.prototype_designer": MAIN_SESSION_MODEL_ID,
}


def make_main_session_role_model_id(model: str) -> str:
    """角色配置中用于表示“主会话连接上的某个具体模型”的稳定 ID。"""
    return f"{MAIN_SESSION_MODEL_PREFIX}{str(model or '').strip()}"


def unwrap_main_session_role_model_id(model_id: str) -> str:
    """从角色模型 ID 中取出实际 provider model。"""
    model_id = str(model_id or "").strip()
    if model_id.startswith(MAIN_SESSION_MODEL_PREFIX):
        return model_id[len(MAIN_SESSION_MODEL_PREFIX):].strip()
    return ""


def _append_main_role_model(options: list[dict], seen: set[str], model: str, display: str) -> None:
    model = str(model or "").strip()
    if not model or model in seen:
        return
    seen.add(model)
    options.append({
        "id": make_main_session_role_model_id(model),
        "display": f"{display or model}（主会话连接）",
        "source": "main_session",
        "provider_model": model,
    })


def build_role_model_options(config: dict) -> list[dict]:
    """生成角色配置可选模型；不再把 opus/sonnet/haiku 旧 tier 别名暴露给角色。"""
    config = config or {}
    available = [{"id": MAIN_SESSION_MODEL_ID, "display": "继承主会话模型", "source": "main_session"}]
    anthropic = (config.get("external_models", {}) or {}).get("anthropic", {}) or {}
    main_route = resolve_main_session_connection(
        anthropic.get("base_url", ""),
        current_env=anthropic.get("env", {}),
        stored_provider_id=anthropic.get("provider_id"),
    )
    provider_id = str(main_route.get("provider_id") or "").strip()
    provider_family = str(main_route.get("provider_family") or "").strip()
    access_mode = str(main_route.get("access_mode") or "").strip()
    model_fields = main_route.get("model_fields") or {}
    has_custom_mapping = any(str(v or "").strip() for v in model_fields.values())

    seen: set[str] = set()
    if provider_id == "openai" or provider_family in {"openai", "codex"} or is_openai_access_mode(access_mode):
        for item in MAIN_SESSION_GPT_MODELS:
            _append_main_role_model(available, seen, item["model"], item["display"])
    elif provider_id == "anthropic" or provider_family == "anthropic" or (provider_id == "custom" and not has_custom_mapping):
        for item in MAIN_SESSION_CLAUDE_MODELS:
            _append_main_role_model(available, seen, item["model"], item["display"])
    else:
        routing_models = main_route.get("routing_models") or {}
        for tier in ("opus", "sonnet", "haiku"):
            model = str(routing_models.get(tier) or "").strip()
            if model and model != tier:
                _append_main_role_model(available, seen, model, model)

    ext = config.get("external_models", {}) or {}
    skip = {"anthropic"}
    for mid, cfg in ext.items():
        if mid in skip:
            continue
        meta = infer_external_model_metadata(mid, cfg)
        if is_openai_access_mode(meta.get("access_mode")):
            continue
        cli_model = cfg.get("cli_model", "")
        api_key = _read_api_key_for_model(mid, cfg)
        if not (cli_model and api_key):
            continue
        preset = next((p for p in PRESET_EXTERNAL_MODELS if p["id"] == mid), None)
        if preset:
            label = f"{cli_model}（{preset['display']}）"
        else:
            display = cfg.get("display", cli_model)
            suffix = meta["source_display"]
            label = display if display != cli_model else cli_model
            if suffix:
                label = f"{label}（{suffix}）"
        available.append({"id": mid, "display": label, "source": "external"})
    return available


def valid_role_model_ids(config: dict) -> set[str]:
    return {item["id"] for item in build_role_model_options(config)}


class ModelConfigManager:
    """角色模型配置读写"""

    def get_models(self, config: dict) -> dict:
        """获取所有角色的当前模型配置（含已配置的外部模型）"""
        overrides = config.get("model_overrides", {})
        reasoning_overrides = config.get("role_reasoning_efforts", {})
        available = build_role_model_options(config)
        available_ids = {item["id"] for item in available}
        roles = []
        for role, display_name in ROLE_DISPLAY:
            default = DEFAULT_MODELS.get(role, MAIN_SESSION_MODEL_ID)
            current = overrides.get(role, default)
            if current in LEGACY_ROLE_MODEL_IDS or current not in available_ids:
                current = default
            current_reasoning = str(reasoning_overrides.get(role) or "inherit").strip()
            if current_reasoning not in VALID_REASONING_EFFORTS:
                current_reasoning = "inherit"
            roles.append({
                "role": role,
                "display_name": display_name,
                "current_model": current,
                "default_model": default,
                "current_reasoning_effort": current_reasoning,
                "default_reasoning_effort": "inherit",
            })
        return {
            "roles": roles,
            "available_models": available,
            "available_reasoning_efforts": AVAILABLE_REASONING_EFFORTS,
        }

    def update_models(
        self,
        model_overrides: dict,
        role_reasoning_efforts: dict | None = None,
    ) -> str | None:
        """
        更新 model_overrides 和可选的 role_reasoning_efforts。

        Args:
            model_overrides: 角色→模型短名映射

        Returns:
            None 表示成功，否则返回错误信息
        """
        # 1. 验证模型值合法（包含已配置的外部模型）
        try:
            with open(_CONFIG_FILE, 'r', encoding='utf-8') as f:
                cur_config = json.load(f)
        except Exception:
            cur_config = {}
        valid_ids = valid_role_model_ids(cur_config)
        for role, model in model_overrides.items():
            if model not in valid_ids:
                return f"无效的模型: {model}"
        cleaned_reasoning = None
        if role_reasoning_efforts is not None:
            if not isinstance(role_reasoning_efforts, dict):
                return "role_reasoning_efforts 必须是对象"
            cleaned_reasoning = {}
            for role, effort in role_reasoning_efforts.items():
                effort = str(effort or "inherit").strip()
                if effort not in VALID_REASONING_EFFORTS:
                    return f"无效的思考深度: {effort}"
                if effort != "inherit":
                    cleaned_reasoning[str(role)] = effort

        # 2. 过滤掉等于默认值的条目（减少 config.json 噪声）
        cleaned = {}
        for role, model in model_overrides.items():
            default = DEFAULT_MODELS.get(role, MAIN_SESSION_MODEL_ID)
            if model != default:
                cleaned[role] = model

        # 3. 原子写入 config.json
        with open(_CONFIG_FILE, 'r', encoding='utf-8') as f:
            config_data = json.load(f)
        config_data["model_overrides"] = cleaned
        if cleaned_reasoning is not None:
            config_data["role_reasoning_efforts"] = cleaned_reasoning
        tmp = str(_CONFIG_FILE) + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(config_data, f, indent=2, ensure_ascii=False)
            f.write('\n')
        os.replace(tmp, str(_CONFIG_FILE))

        # 4. 刷新运行时缓存
        from lib.config_loader import load_config
        load_config(force_reload=True)
        return None



class ExternalModelManager:
    """外部模型（Anthropic 接口兼容）配置读写"""

    _SKIP_KEYS = {"anthropic"}  # config.json 中非外部模型的保留 key

    def get(self, config: dict) -> dict:
        """返回预置模型状态 + 自定义模型列表（API Key 脱敏）"""
        ext = config.get("external_models", {})
        # 预置模型
        presets = []
        for preset in PRESET_EXTERNAL_MODELS:
            mid = preset["id"]
            cfg = ext.get(mid, {})
            env = cfg.get("env", {})
            api_key = _read_api_key_for_model(mid, cfg)
            base_url = get_external_model_stored_base_url(mid, cfg, default_base_url=preset["default_base_url"])
            cli_model = cfg.get("cli_model", "")
            meta = infer_external_model_metadata(mid, cfg)
            presets.append({
                "id": mid,
                "display": preset["display"],
                "base_url": base_url,
                "default_base_url": preset["default_base_url"],
                "model_placeholder": preset["model_placeholder"],
                "cli_model": cli_model,
                "configured": bool(api_key and cli_model),
                "api_key_masked": _mask_key(api_key) if api_key else "",
                "access_mode": meta["access_mode"],
                "provider_family": meta["provider_family"],
                "source_display": meta["source_display"],
            })
        # 自定义模型（config 中不属于预置、也不属于保留 key 的条目）
        skip = PRESET_IDS | self._SKIP_KEYS
        customs = []
        for mid, cfg in ext.items():
            if mid in skip:
                continue
            api_key = _read_api_key_for_model(mid, cfg)
            base_url = get_external_model_stored_base_url(mid, cfg)
            cli_model = cfg.get("cli_model", mid)
            display = cfg.get("display", cli_model)
            meta = infer_external_model_metadata(mid, cfg)
            customs.append({
                "id": mid,
                "display": display,
                "base_url": base_url,
                "cli_model": cli_model,
                "configured": bool(api_key and cli_model),
                "api_key_masked": _mask_key(api_key) if api_key else "",
                "access_mode": meta["access_mode"],
                "provider_family": meta["provider_family"],
                "source_display": meta["source_display"],
            })
        return {"presets": presets, "customs": customs}

    def update(self, model_id: str, cli_model: str, base_url: str, api_key: str,
               display: str = "", access_mode: str = "", provider_family: str = "") -> str | None:
        """
        新增或更新一个外部模型（预置或自定义）。
        - model_id: config key，预置用固定 ID，自定义可任意（字母/数字/连字符/下划线）
        - cli_model: 实际传给 --model 的模型名
        - api_key == '__DELETE__' 表示删除整条配置
        Returns None 表示成功，否则返回错误信息。
        """
        import re
        if model_id in self._SKIP_KEYS:
            return f"保留 ID，不可修改: {model_id}"
        if model_id not in PRESET_IDS:
            if not re.match(r'^[a-zA-Z0-9_-]+$', model_id):
                return "自定义模型 ID 只能包含字母、数字、连字符和下划线"
        with open(_CONFIG_FILE, 'r', encoding='utf-8') as f:
            config_data = json.load(f)
        ext = config_data.setdefault("external_models", {})
        if api_key == "__DELETE__":
            ext.pop(model_id, None)
        else:
            candidate = dict(ext.get(model_id, {}) or {})
            candidate["env"] = dict(candidate.get("env", {}) or {})
            if cli_model:
                candidate["cli_model"] = cli_model
            candidate["base_url"] = base_url.strip()
            if display and model_id not in PRESET_IDS:
                candidate["display"] = display
            if access_mode and model_id not in PRESET_IDS:
                candidate["access_mode"] = access_mode
            if provider_family and model_id not in PRESET_IDS:
                candidate["provider_family"] = provider_family
            if is_openai_access_mode(infer_external_model_metadata(model_id, candidate).get("access_mode")):
                return OPENAPI_EXTERNAL_MODEL_UNSUPPORTED_MESSAGE
            entry = ext.setdefault(model_id, {"env": {}})
            entry.setdefault("env", {})
            if cli_model:
                entry["cli_model"] = cli_model
            entry["base_url"] = base_url.strip()
            if display and model_id not in PRESET_IDS:
                entry["display"] = display
            if access_mode and model_id not in PRESET_IDS:
                entry["access_mode"] = access_mode
            if provider_family and model_id not in PRESET_IDS:
                entry["provider_family"] = provider_family
            effective_access_mode = entry.get("access_mode", "") or access_mode
            if model_id not in PRESET_IDS and is_openai_access_mode(effective_access_mode):
                provider_caps = infer_openai_provider_capabilities(
                    base_url,
                    provider_family=entry.get("provider_family") or provider_family,
                )
                entry["access_mode"] = "openai_compatible"
                entry["provider_family"] = provider_caps["provider_family"]
            if base_url and not is_openai_access_mode(effective_access_mode):
                entry["env"]["ANTHROPIC_BASE_URL"] = base_url
            elif is_openai_access_mode(effective_access_mode):
                entry["env"].pop("ANTHROPIC_BASE_URL", None)
            elif model_id in PRESET_IDS and "ANTHROPIC_BASE_URL" not in entry["env"]:
                # 预置模型：若 base_url 未提交且 env 中还没有，注入 default_base_url
                preset = next((p for p in PRESET_EXTERNAL_MODELS if p["id"] == model_id), None)
                if preset:
                    entry["env"]["ANTHROPIC_BASE_URL"] = preset["default_base_url"]
            if model_id in PRESET_IDS:
                preset = next((p for p in PRESET_EXTERNAL_MODELS if p["id"] == model_id), None)
                if preset:
                    entry["access_mode"] = preset.get("access_mode", "anthropic_native")
                    entry["provider_family"] = preset.get("provider_family", model_id)
            elif "access_mode" not in entry or "provider_family" not in entry:
                inferred = infer_external_model_metadata(model_id, entry)
                entry.setdefault("access_mode", inferred["access_mode"])
                entry.setdefault("provider_family", inferred["provider_family"])
            # 预置模型：始终注入 required_env（不被 UI 展示的隐式必要项）
            if model_id in PRESET_IDS:
                preset = next((p for p in PRESET_EXTERNAL_MODELS if p["id"] == model_id), None)
                if preset:
                    for k, v in preset.get("required_env", {}).items():
                        entry["env"].setdefault(k, v)
            if api_key:
                if model_id in PROVIDER_ENV_VAR_MAP:
                    env_var_key = PROVIDER_ENV_VAR_MAP[model_id][0]
                    from lib.config_loader import write_env_file
                    write_env_file({env_var_key: api_key})
                    # 同时写入 config.json 供 agent_runner 直接读取
                    entry["api_key"] = api_key
                else:
                    entry["env"]["ANTHROPIC_API_KEY"] = api_key
        # 原子写入
        tmp = str(_CONFIG_FILE) + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(config_data, f, indent=2, ensure_ascii=False)
            f.write('\n')
        os.replace(tmp, str(_CONFIG_FILE))
        from lib.config_loader import load_config
        load_config(force_reload=True)
        return None


class ConnectionProfileManager:
    """已保存的 API 连接配置管理（快速切换）"""

    def get_all(self) -> list:
        """读取所有已保存的连接（api_key 脱敏后返回）"""
        with open(_CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
        conns = config.get("saved_connections", [])
        # 标记当前活跃连接
        anthropic = config.get("external_models", {}).get("anthropic", {})
        current_resolved = resolve_main_session_connection(
            anthropic.get("base_url", ""),
            values=anthropic,
            current_env=anthropic.get("env", {}),
            stored_provider_id=anthropic.get("provider_id"),
        )
        current_url = normalize_main_session_base_url(current_resolved.get("base_url", ""))
        current_env = current_resolved["env"]
        current_provider_id = current_resolved["provider_id"]
        current_api_key = str(anthropic.get("api_key", "") or "")
        current_auth_mode = str(current_resolved.get("auth_mode") or "")
        current_codex_home = str(current_resolved.get("codex_home") or "")
        active_saved_connection_id = str(anthropic.get("active_saved_connection_id", "") or "")
        has_active_saved_connection = any(
            str(c.get("id", "") or "") == active_saved_connection_id
            for c in conns
        )
        result = []
        fallback_active_indexes = []
        for idx, c in enumerate(conns):
            resolved = resolve_main_session_connection(
                c.get("base_url", ""),
                values=c,
                current_env=c.get("env", {}),
                stored_provider_id=c.get("provider_id"),
            )
            conn_env = resolved["env"]
            conn_id = str(c.get("id", "") or "")
            fallback_active = (
                normalize_main_session_base_url(c.get("base_url", "")) == current_url
                and conn_env == current_env
                and (c.get("provider_id", "") or resolved["provider_id"]) == current_provider_id
                and str(c.get("api_key", "") or "") == current_api_key
                and str(resolved.get("auth_mode") or "") == current_auth_mode
                and str(resolved.get("codex_home") or "") == current_codex_home
            )
            if fallback_active:
                fallback_active_indexes.append(idx)
            item = {
                "id": conn_id,
                "name": c.get("name", ""),
                "base_url": c.get("base_url", ""),
                "active": False,
                "has_key": bool(c.get("api_key")),
                "provider_id": c.get("provider_id", "") or resolved["provider_id"],
                "provider_display": resolved["provider_display"],
                "mode_display": describe_connection_mode(
                    c.get("provider_id", "") or resolved["provider_id"],
                    resolved["provider_display"],
                ),
                "host_label": _connection_host_label(c.get("base_url", "")),
                "routing_summary": resolved["routing_summary"],
                "auth_mode": resolved.get("auth_mode", ""),
                "auth_status": resolved.get("auth_status", ""),
                "auth_last_verified_at": resolved.get("auth_last_verified_at", ""),
                "auth_account_label": resolved.get("auth_account_label", ""),
                "codex_home": resolved.get("codex_home", ""),
                "account_login_supported": bool(resolved.get("account_login_supported")),
            }
            result.append(item)
        fallback_active_idx = fallback_active_indexes[-1] if fallback_active_indexes else -1
        for idx, item in enumerate(result):
            if has_active_saved_connection:
                item["active"] = bool(item.get("id")) and item["id"] == active_saved_connection_id
            else:
                item["active"] = idx == fallback_active_idx
        return result

    def get_by_id(self, connection_id: str) -> dict | None:
        """读取单条保存连接及其解析后的展示字段。"""
        connection_id = str(connection_id or "").strip()
        if not connection_id:
            return None
        for item in self.get_all():
            if str(item.get("id", "") or "") == connection_id:
                return item
        return None

    def update_auth_state(
        self,
        connection_id: str,
        *,
        auth_status: str,
        auth_last_verified_at: str = "",
        auth_account_label: str = "",
    ) -> dict | None:
        """更新保存连接及当前激活主连接的 OpenAPI 登录状态。"""
        connection_id = str(connection_id or "").strip()
        if not connection_id:
            return None
        with open(_CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
        conns = config.get("saved_connections", []) or []
        target = None
        for conn in conns:
            if str(conn.get("id", "") or "") == connection_id:
                target = conn
                break
        if target is None:
            return None

        update_values = dict(target)
        update_values["auth_status"] = auth_status
        update_values["auth_last_verified_at"] = auth_last_verified_at
        update_values["auth_account_label"] = auth_account_label
        resolved = resolve_main_session_connection(
            target.get("base_url", ""),
            values=update_values,
            current_env=target.get("env", {}),
            stored_provider_id=target.get("provider_id"),
        )
        _apply_openai_auth_fields(target, resolved)

        anthropic = config.setdefault("external_models", {}).setdefault("anthropic", {})
        is_active = str(anthropic.get("active_saved_connection_id", "") or "") == connection_id
        if is_active:
            _apply_openai_auth_fields(anthropic, resolved)
        config["saved_connections"] = conns
        self._write(config)
        return {
            "id": connection_id,
            "active": is_active,
            "resolved_connection": resolved,
        }

    def save(self, name: str, base_url: str, api_key: str = "", extra_env: dict | None = None) -> dict:
        """保存当前连接为快捷配置（api_key='__USE_STORED__' 时自动读取当前生效的 key）"""
        if api_key == "__USE_STORED__":
            api_key = resolve_current_main_session_api_key()
        with open(_CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
        conns = config.setdefault("saved_connections", [])
        connection_values = dict(extra_env or {})
        resolved = resolve_main_session_connection(base_url, values=connection_values)
        normalized_env = resolved["env"]
        base_url = resolved["base_url"]
        preserved_id = ""
        # 去重：仅当 base_url、api_key、默认模型映射都相同时才视为重复
        filtered_conns = []
        for c in conns:
            is_duplicate = (
                normalize_main_session_base_url(c.get("base_url")) == base_url
                and c.get("api_key", "") == api_key
                and (c.get("provider_id", "") or resolved["provider_id"]) == resolved["provider_id"]
                and str(c.get("auth_mode", "") or resolved["auth_mode"]) == resolved["auth_mode"]
                and resolve_main_session_connection(
                    c.get("base_url", ""),
                    values=c,
                    current_env=c.get("env", {}),
                    stored_provider_id=c.get("provider_id"),
                )["env"] == normalized_env
            )
            if is_duplicate and not preserved_id:
                preserved_id = str(c.get("id", "") or "")
                continue
            filtered_conns.append(c)
        conns = filtered_conns
        connection_id = preserved_id or str(connection_values.get("id") or "") or _new_saved_connection_id()
        connection_values["connection_id"] = connection_id
        connection_values["id"] = connection_id
        resolved = resolve_main_session_connection(base_url, values=connection_values)
        normalized_env = resolved["env"]
        base_url = resolved["base_url"]
        conn_item = {
            "id": connection_id,
            "name": name,
            "base_url": base_url,
        }
        if api_key:
            conn_item["api_key"] = api_key
        if normalized_env:
            conn_item["env"] = normalized_env
        conn_item["provider_id"] = resolved["provider_id"]
        conn_item["access_mode"] = resolved["access_mode"]
        conn_item["provider_family"] = resolved["provider_family"]
        _apply_openai_auth_fields(conn_item, resolved)
        conns.append(conn_item)
        config["saved_connections"] = conns
        self._write(config)
        return {
            "id": connection_id,
            "name": name,
            "base_url": base_url,
            "provider_id": resolved["provider_id"],
            "auth_mode": resolved.get("auth_mode", ""),
            "auth_status": resolved.get("auth_status", ""),
            "codex_home": resolved.get("codex_home", ""),
        }

    def switch(self, idx: int) -> dict | None:
        """切换默认主连接，并同步 Claude 兼容 settings。"""
        with open(_CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
        conns = config.get("saved_connections", [])
        if idx < 0 or idx >= len(conns):
            return None
        conn = conns[idx]
        conn_id = str(conn.get("id", "") or "")
        if not conn_id:
            conn_id = _new_saved_connection_id()
            conn["id"] = conn_id
        resolved = resolve_main_session_connection(
            conn.get("base_url", ""),
            values=conn,
            current_env=conn.get("env", {}),
            stored_provider_id=conn.get("provider_id"),
        )
        base_url = resolved["base_url"]
        api_key = conn.get("api_key", "")
        conn_env = resolved["env"]

        # 更新 config.json 的 anthropic 段
        anthropic = config.setdefault("external_models", {}).setdefault("anthropic", {})
        if base_url:
            anthropic["base_url"] = base_url
        if api_key:
            anthropic["api_key"] = api_key
        else:
            anthropic.pop("api_key", None)
        anthropic["provider_id"] = resolved["provider_id"]
        anthropic["access_mode"] = resolved["access_mode"]
        anthropic["provider_family"] = resolved["provider_family"]
        _apply_openai_auth_fields(anthropic, resolved)
        anthropic["active_saved_connection_id"] = conn_id
        anthropic_env = anthropic.setdefault("env", {})
        for env_key in MAIN_SESSION_MANAGED_ENV_KEYS:
            anthropic_env.pop(env_key, None)
        anthropic_env.update(conn_env)
        config["saved_connections"] = conns
        self._write(config)

        # 同步到 .env 持久化
        try:
            from lib.config_loader import write_env_file

            write_env_file({
                "OPUS_MAIN_API_KEY": api_key,
                "ANTHROPIC_API_KEY": "",
            })
        except Exception:
            pass

        effective_api_key = str(
            config.get("external_models", {})
            .get("anthropic", {})
            .get("api_key", "")
            or api_key
            or ""
        ).strip()
        claude_settings_synced = sync_main_session_to_claude_settings(
            api_key=effective_api_key,
            base_url=base_url,
            extra_env=conn_env,
            resolved_connection=resolved,
        )

        return {
            "connection_id": conn_id,
            "base_url": base_url,
            "api_key": api_key,
            "extra_env": conn_env,
            "resolved_connection": resolved,
            "claude_settings_synced": bool(claude_settings_synced),
        }

    def delete(self, idx: int):
        """删除指定连接"""
        with open(_CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
        conns = config.get("saved_connections", [])
        if 0 <= idx < len(conns):
            removed = conns.pop(idx)
            config["saved_connections"] = conns
            anthropic = config.get("external_models", {}).get("anthropic", {})
            removed_id = str(removed.get("id", "") or "")
            if removed_id and anthropic.get("active_saved_connection_id") == removed_id:
                anthropic.pop("active_saved_connection_id", None)
            self._write(config)

    @staticmethod
    def _write(config: dict):
        tmp = str(_CONFIG_FILE) + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
            f.write('\n')
        os.replace(tmp, str(_CONFIG_FILE))
        from lib.config_loader import load_config
        load_config(force_reload=True)


DEFAULT_ROLE_MCP_PERMISSIONS: dict[str, list[str]] = {
    "requirement_analyst": ["serena"],
    "product_manager":     ["serena"],
    "architect":           ["serena"],
    "frontend_developer":  ["serena", "playwright"],
    "backend_developer":   ["serena"],
    "qa_engineer":         ["playwright"],
    "devops_engineer":     [],
    "knowledge_engineer":  [],
    "code_reviewer":       [],
    "refactor_engineer":   [],
    "handoff_extractor":   [],
    "embedded_engineer":   ["serena"],
    "integration_engineer":[],
    "fix_engineer":        ["serena"],
    "knowledge_admin":     ["serena"],
    "code_explorer":       ["serena"],
    "assistant":           ["serena"],
    "project_manager":     [],
    "manual_updater":      [],
    "merge_resolver":      [],
    "technical_assessor":  ["serena"],
}


class McpServiceManager:
    """MCP 工具全局状态、修复与导入管理。"""

    MCP_JSON_PATH = _Path(__file__).resolve().parent.parent / ".mcp.json"
    CHROME_EXTENSION_ZIP = _Path(__file__).resolve().parent.parent / "assets" / "chrome-mcp-extension.zip"
    CHROME_BRIDGE_SCRIPT = _Path(__file__).resolve().parent / "chrome_mcp_stdio.py"
    VIZO_ROUTER_SCRIPT = _Path(__file__).resolve().parent / "vizo_router_mcp_stdio.py"
    BUILTIN_SERVICES = ("serena", "vizo-router", "mcp-chrome", "playwright")
    CORE_SERVICES = ("serena", "mcp-chrome", "vizo-router")
    BUILTIN_SERVICE_SPECS = {
        "serena": {
            "command": "serena",
            "args": ["start-mcp-server", "--project-from-cwd"],
        },
        "mcp-chrome": {
            "command": "python3",
            "args": ["lib/chrome_mcp_stdio.py"],
        },
        "vizo-router": {
            "command": "python3",
            "args": ["lib/vizo_router_mcp_stdio.py"],
        },
        "playwright": {
            "command": "npx",
            "args": [
                "-y",
                "@playwright/mcp@latest",
                "--headless",
                "--browser",
                "chromium",
                "--no-sandbox",
                "--isolated",
            ],
        },
    }

    def _read_mcp_file(self) -> dict:
        if not self.MCP_JSON_PATH.exists():
            return {"mcpServers": {}}
        try:
            data = json.loads(self.MCP_JSON_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        servers = data.get("mcpServers", {})
        if not isinstance(servers, dict):
            servers = {}
        data["mcpServers"] = servers
        return data

    def _write_mcp_file(self, data: dict):
        self.MCP_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(self.MCP_JSON_PATH) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, str(self.MCP_JSON_PATH))

    def _load_config_data(self) -> dict:
        try:
            return json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _build_role_usage(self, config_data: dict, names: set[str]) -> dict[str, list[str]]:
        saved = config_data.get("mcp_permissions", {})
        role_usage = {name: [] for name in names}
        for role, default_list in DEFAULT_ROLE_MCP_PERMISSIONS.items():
            current = saved.get(role, default_list)
            if not isinstance(current, list):
                continue
            for name in current:
                if name in role_usage and role not in role_usage[name]:
                    role_usage[name].append(role)
        return role_usage

    def _read_chrome_health(self, config_data: dict) -> dict:
        return read_bridge_state()

    def _playwright_runtime_available(self) -> bool:
        return bool(_find_playwright_mcp_bin() or shutil.which("npx"))

    def get_chrome_status(self) -> dict:
        config_data = self._load_config_data()
        overview = self.get_overview(config_data=config_data)
        chrome = next((item for item in overview["services"] if item["name"] == "mcp-chrome"), None)
        health = {}
        if chrome and chrome["enabled"]:
            health = self._read_chrome_health(config_data)
        return {
            "service_enabled": bool(chrome and chrome["enabled"]),
            "service_installed": bool(chrome and chrome["installed"]),
            "service_status": chrome["status"] if chrome else "missing",
            "service_status_text": chrome["status_text"] if chrome else "未安装",
            "chrome_connected": bool(health.get("chrome_connected")),
            "browser_info": health.get("browser_info"),
            "cached_tools": int(health.get("cached_tools") or 0),
            "uptime_seconds": float(health.get("uptime_seconds") or 0),
            "pending_requests": int(health.get("pending_requests") or 0),
            "stats": health.get("stats") or {},
        }

    def get_overview(self, config_data: dict | None = None) -> dict:
        if config_data is None:
            config_data = self._load_config_data()
        mcp_data = self._read_mcp_file()
        installed_servers = mcp_data.get("mcpServers", {})
        installed_names = set(installed_servers.keys())
        all_names = set(installed_names)
        all_names.update(self.BUILTIN_SERVICES)
        service_state = load_mcp_service_state(config_data)
        role_usage = self._build_role_usage(config_data, all_names)
        chrome_health = self._read_chrome_health(config_data)
        chrome_connected = bool(chrome_health.get("chrome_connected"))
        serena_runtime_ok = bool(_find_packaged_serena_bin())
        playwright_runtime_ok = self._playwright_runtime_available()

        services = []
        locked_order = {name: index for index, name in enumerate(LOCKED_MCP_SERVICE_NAMES)}

        def _service_sort_key(item: str) -> tuple:
            meta = get_mcp_service_metadata(item)
            if item in locked_order:
                return (0, locked_order[item], meta["display_name"].lower(), item)
            return (1, meta["display_name"].lower(), item)

        for name in sorted(all_names, key=_service_sort_key):
            meta = get_mcp_service_metadata(name)
            installed = name in installed_names
            locked = is_locked_mcp_service(name)
            enabled = is_mcp_service_enabled(name, config_data)
            roles_using = role_usage.get(name, [])
            status = "ready"
            status_text = "可用"
            action_hint = ""
            helper_text = ""
            can_repair = name in self.BUILTIN_SERVICES
            auxiliary_action = ""
            auxiliary_label = ""

            if name == "serena":
                if not installed:
                    status = "missing"
                    status_text = "未安装"
                    helper_text = "系统记忆依赖 Serena，建议立即修复。"
                    action_hint = "请点击“一键修复”恢复内置配置。"
                elif not enabled:
                    status = "disabled"
                    status_text = "已关闭"
                elif not serena_runtime_ok:
                    status = "error"
                    status_text = "异常"
                    helper_text = "内置 Serena 运行时缺失，无法正常启动。"
                    action_hint = "请点击“一键修复”恢复内置配置；若仍失败，请重新安装 Opus。"
                else:
                    status = "ready"
                    status_text = "可用"
            elif name == "mcp-chrome":
                auxiliary_action = "open_guide"
                auxiliary_label = "打开连接引导"
                if not installed:
                    status = "missing"
                    status_text = "未安装"
                    helper_text = "未检测到 Chrome MCP 配置。"
                    action_hint = "请点击“修复服务”恢复内置配置，再打开连接引导。"
                elif not enabled:
                    status = "disabled"
                    status_text = "已关闭"
                elif not self.CHROME_BRIDGE_SCRIPT.exists() or not self.CHROME_EXTENSION_ZIP.exists():
                    status = "error"
                    status_text = "异常"
                    helper_text = "Chrome MCP 内置文件缺失。"
                    action_hint = "请点击“修复服务”；若仍失败，请重新安装 Opus。"
                elif chrome_connected:
                    status = "connected"
                    status_text = "已连接"
                    helper_text = "浏览器已连接，可直接用于截图、点击、表单填写等操作。"
                else:
                    status = "waiting"
                    status_text = "等待连接"
                    helper_text = "浏览器尚未连接，前端测试能力暂不可用。"
                    action_hint = "请打开连接引导，在浏览器中完成连接。"
            elif name == "vizo-router":
                if not installed:
                    status = "missing"
                    status_text = "未安装"
                    helper_text = "主会话语义路由和交互设计工具当前不可用。"
                    action_hint = "请点击“一键修复”恢复内置配置。"
                elif not enabled:
                    status = "disabled"
                    status_text = "已关闭"
                elif not self.VIZO_ROUTER_SCRIPT.exists():
                    status = "error"
                    status_text = "异常"
                    helper_text = "Vizo Router 服务端脚本缺失。"
                    action_hint = "请点击“一键修复”；若仍失败，请重新安装 Opus。"
                else:
                    status = "ready"
                    status_text = "可用"
            elif name == "playwright":
                if not installed:
                    status = "missing"
                    status_text = "未安装"
                    helper_text = "未检测到 Playwright MCP 预置配置。"
                    action_hint = "请点击“修复服务”恢复内置配置。"
                elif not enabled:
                    status = "disabled"
                    status_text = "已关闭"
                elif not playwright_runtime_ok:
                    status = "error"
                    status_text = "异常"
                    helper_text = "当前环境缺少 Playwright MCP 运行时。"
                    action_hint = "请重新安装包含 Playwright MCP 的 Vizo 镜像，或确认 Node.js / npx 可用。"
                else:
                    status = "ready"
                    status_text = "可用"
                    helper_text = "可用于无头浏览器自动化、页面巡检和端到端交互测试。"
            else:
                if not installed:
                    status = "missing"
                    status_text = "未安装"
                    helper_text = "当前项目未安装这个 MCP。"
                elif not enabled:
                    status = "disabled"
                    status_text = "已关闭"
                else:
                    status = "ready"
                    status_text = "已开启"

            services.append({
                "name": name,
                "display_name": meta["display_name"],
                "description": meta["description"],
                "is_core": meta["is_core"],
                "is_locked": locked,
                "can_toggle": bool(installed and not locked),
                "installed": installed,
                "enabled": enabled,
                "status": status,
                "status_text": status_text,
                "roles_using": roles_using,
                "roles_using_count": len(roles_using),
                "can_repair": can_repair,
                "action_hint": action_hint,
                "helper_text": helper_text,
                "auxiliary_action": auxiliary_action,
                "auxiliary_label": auxiliary_label,
                "browser_info": chrome_health.get("browser_info") if name == "mcp-chrome" else None,
                "cached_tools": int(chrome_health.get("cached_tools") or 0) if name == "mcp-chrome" else 0,
                "uptime_seconds": float(chrome_health.get("uptime_seconds") or 0) if name == "mcp-chrome" else 0,
            })

        installed_count = sum(1 for item in services if item["installed"])
        enabled_count = sum(1 for item in services if item["installed"] and item["enabled"])
        core_missing = [item["display_name"] for item in services if item["is_core"] and not item["installed"]]
        locked_missing = [item["display_name"] for item in services if item["is_locked"] and not item["installed"]]
        return {
            "summary": {
                "installed_count": installed_count,
                "enabled_count": enabled_count,
                "core_missing": core_missing,
                "locked_missing": locked_missing,
            },
            "services": services,
            "install_help": {
                "import_title": "从官方文档粘贴配置",
                "import_body": "如果第三方文档给了 MCP 配置 JSON，可以直接粘贴到这里导入。",
                "manual_title": "手动添加工具",
                "manual_body": "如果文档只给了命令和参数，可手动填写名称、命令、参数和环境变量。",
            },
            "service_state": service_state,
        }

    def update_service_states(self, service_states: dict) -> str | None:
        if not isinstance(service_states, dict):
            return "缺少 service_states"
        config_data = self._load_config_data()
        state = config_data.setdefault("mcp_service_state", {})
        known_names = set(self._read_mcp_file().get("mcpServers", {}).keys()) | set(self.BUILTIN_SERVICES)
        for name, enabled in service_states.items():
            if name not in known_names:
                return f"未知 MCP：{name}"
            if is_locked_mcp_service(name) and not bool(enabled):
                display_name = get_mcp_service_metadata(name)["display_name"]
                return f"{display_name} 是系统内置 MCP，不能关闭"
            state[name] = {"enabled": bool(enabled)}
        for name in LOCKED_MCP_SERVICE_NAMES:
            if name in known_names:
                state[name] = {"enabled": True}
        write_config_data(config_data)
        return None

    def repair_core_service(self, name: str) -> tuple[bool, str]:
        if name not in self.BUILTIN_SERVICES:
            return False, "只支持修复内置 MCP"

        if name == "serena" and not _find_packaged_serena_bin():
            return False, "当前安装包中的 Serena 运行时不完整，请重新安装 Opus。"
        if name == "mcp-chrome":
            if not self.CHROME_BRIDGE_SCRIPT.exists():
                return False, "Chrome MCP 服务端脚本缺失，请重新安装 Opus。"
            if not self.CHROME_EXTENSION_ZIP.exists():
                return False, "Chrome MCP 扩展包缺失，请重新安装 Opus。"
        if name == "vizo-router" and not self.VIZO_ROUTER_SCRIPT.exists():
            return False, "Vizo Router 服务端脚本缺失，请重新安装 Opus。"
        if name == "playwright" and not self._playwright_runtime_available():
            return False, "当前环境缺少 Playwright MCP 运行时，请重新安装包含 Playwright MCP 的 Vizo 镜像。"

        data = self._read_mcp_file()
        servers = data.setdefault("mcpServers", {})
        servers[name] = json.loads(json.dumps(self.BUILTIN_SERVICE_SPECS[name]))
        self._write_mcp_file(data)

        config_data = self._load_config_data()
        state = config_data.setdefault("mcp_service_state", {})
        state[name] = {"enabled": True}
        write_config_data(config_data)
        if name == "serena":
            return True, "Serena 已恢复并重新启用。"
        if name == "vizo-router":
            return True, "Vizo Router 已恢复并重新启用。"
        if name == "playwright":
            return True, "Playwright MCP 已恢复并重新启用。"
        return True, "Chrome MCP 服务已恢复，请继续打开连接引导完成浏览器侧连接。"

    def import_services(self, raw_text: str, overwrite: bool = False) -> tuple[bool, str, list[str]]:
        text = str(raw_text or "").strip()
        if not text:
            return False, "请先粘贴配置 JSON", []
        try:
            payload = json.loads(text)
        except Exception:
            return False, "配置 JSON 解析失败", []

        if isinstance(payload, dict) and isinstance(payload.get("mcpServers"), dict):
            new_services = payload["mcpServers"]
        elif isinstance(payload, dict) and payload.get("name") and payload.get("command"):
            new_services = {
                str(payload["name"]).strip(): {
                    "command": str(payload["command"]).strip(),
                    "args": payload.get("args", []) or [],
                    "env": payload.get("env", {}) or {},
                }
            }
        else:
            return False, "请粘贴标准 MCP 配置 JSON", []

        data = self._read_mcp_file()
        servers = data.setdefault("mcpServers", {})
        imported = []
        for name, spec in new_services.items():
            clean_name = str(name or "").strip()
            if not clean_name:
                return False, "MCP 名称不能为空", []
            if clean_name in servers and not overwrite:
                return False, f"MCP {clean_name} 已存在", []
            if not isinstance(spec, dict) or not str(spec.get("command", "")).strip():
                return False, f"MCP {clean_name} 缺少 command", []
            normalized = {"command": str(spec.get("command", "")).strip()}
            args = spec.get("args", [])
            env = spec.get("env", {})
            if args:
                if not isinstance(args, list):
                    return False, f"MCP {clean_name} 的 args 必须是数组", []
                normalized["args"] = args
            if env:
                if not isinstance(env, dict):
                    return False, f"MCP {clean_name} 的 env 必须是对象", []
                normalized["env"] = env
            servers[clean_name] = normalized
            imported.append(clean_name)

        self._write_mcp_file(data)
        config_data = self._load_config_data()
        state = config_data.setdefault("mcp_service_state", {})
        for name in imported:
            state[name] = {"enabled": True}
        write_config_data(config_data)
        return True, f"已导入 {len(imported)} 个 MCP", imported

    def add_manual_service(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        overwrite: bool = False,
    ) -> tuple[bool, str]:
        service_name = str(name or "").strip()
        command_text = str(command or "").strip()
        if not service_name:
            return False, "请填写 MCP 名称"
        if not command_text:
            return False, "请填写命令"

        payload = {
            "mcpServers": {
                service_name: {
                    "command": command_text,
                    "args": [str(item) for item in (args or []) if str(item).strip()],
                    "env": {str(k): str(v) for k, v in (env or {}).items() if str(k).strip()},
                }
            }
        }
        ok, message, _ = self.import_services(json.dumps(payload, ensure_ascii=False), overwrite=overwrite)
        return ok, message


class McpPermissionManager:
    """每个角色可使用的 MCP 服务器权限读写"""

    MCP_JSON_PATH = _Path(__file__).resolve().parent.parent / ".mcp.json"

    # 从 agent_runner.py ALLOWED_TOOLS 提取的默认值（仅 mcp__ 前缀，去前缀后）
    DEFAULT_MCP_PERMISSIONS = DEFAULT_ROLE_MCP_PERMISSIONS

    def get_installed_mcps(self) -> list[str]:
        """从 .mcp.json 读取已安装的 MCP 服务器名列表"""
        if not self.MCP_JSON_PATH.exists():
            return []
        try:
            data = json.loads(self.MCP_JSON_PATH.read_text(encoding="utf-8"))
            return list(data.get("mcpServers", {}).keys())
        except Exception:
            return []

    def get(self) -> dict:
        """
        返回:
        - mcps: 已安装 MCP 列表 [{"name": "serena"}, ...]
        - role_mcps: 每个角色当前的 MCP 权限 {"requirement_analyst": ["serena"], ...}
        """
        installed = self.get_installed_mcps()
        installed_set = set(installed)

        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
        saved = config.get("mcp_permissions", {})

        role_mcps: dict[str, list[str]] = {}
        for role, default_list in self.DEFAULT_MCP_PERMISSIONS.items():
            current = saved.get(role, default_list)
            # 过滤掉 .mcp.json 中已删除的 MCP，避免传入无效工具名
            role_mcps[role] = [m for m in current if m in installed_set]

        service_map = {
            item["name"]: item for item in McpServiceManager().get_overview(config_data=config).get("services", [])
        }
        mcps = []
        for name in installed:
            service = service_map.get(name, {})
            meta = get_mcp_service_metadata(name)
            mcps.append({
                "name": name,
                "display_name": meta["display_name"],
                "enabled": bool(service.get("enabled", True)),
                "status": service.get("status", "ready"),
                "status_text": service.get("status_text", "可用"),
                "is_core": meta["is_core"],
            })

        return {"mcps": mcps, "role_mcps": role_mcps}

    def update(self, role_mcps: dict) -> str | None:
        """
        保存角色 MCP 权限到 config.json。
        Returns None 表示成功，否则返回错误信息。
        """
        installed = set(self.get_installed_mcps())
        for role, mcps in role_mcps.items():
            invalid = [m for m in mcps if m not in installed]
            if invalid:
                return f"MCP {invalid} 未在 .mcp.json 中安装"

        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            config_data = json.load(f)
        # 合并而非替换：保留前端未显示角色的已有配置
        existing = config_data.get("mcp_permissions", {})
        existing.update(role_mcps)
        config_data["mcp_permissions"] = existing
        tmp = str(_CONFIG_FILE) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        import os
        os.replace(tmp, str(_CONFIG_FILE))
        try:
            from lib.config_loader import load_config
            load_config(force_reload=True)
        except Exception:
            pass
        return None

INITIALIZED_FILE = _Path(
    write_data_path("initialized", project_root=_Path(__file__).resolve().parent.parent)
)


class SetupWizard:
    """首次运行引导：检测是否需要引导 + 管理引导完成状态"""

    def __init__(self, config: dict, password_manager: PasswordManager):
        self._config = config
        self._password_mgr = password_manager

    def is_first_run(self) -> bool:
        """首次运行检测"""
        # 1. 已初始化标记存在 → 非首次
        if INITIALIZED_FILE.exists():
            return False
        # 2. 密码已设置 AND API Key 已配置 → 非首次
        has_password = self._password_mgr.has_password()
        has_api_key = self._has_api_key()
        if has_password and has_api_key:
            return False
        return True

    def _has_api_key(self) -> bool:
        """检查 API Key 是否已配置"""
        from lib.config_loader import load_config, is_placeholder
        cfg = load_config()
        api_key = cfg.get("external_models", {}).get("anthropic", {}).get("api_key", "")
        return bool(api_key) and not is_placeholder(api_key)

    def needs_password_step(self) -> bool:
        """是否需要密码设置步骤"""
        return not self._password_mgr.has_password()

    def complete(self):
        """标记引导完成"""
        INITIALIZED_FILE.parent.mkdir(parents=True, exist_ok=True)
        INITIALIZED_FILE.write_text("", encoding="utf-8")


SETUP_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Vizo - 首次设置</title>
<style>
:root {
  --bg-primary: #0a0e17;
  --bg-secondary: #111827;
  --bg-input: #0f1923;
  --text-primary: #e2e8f0;
  --text-muted: #64748b;
  --accent: #38bdf8;
  --purple: #a78bfa;
  --green: #22c55e;
  --red: #ef4444;
  --border: #1e3a5f;
  --radius: 8px;
  --shadow: 0 4px 24px rgba(0,0,0,0.4);
  --transition: 0.25s cubic-bezier(0.4, 0, 0.2, 1);
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  background: var(--bg-primary);
  color: var(--text-primary);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans SC", sans-serif;
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
}
.setup-box {
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 2.5rem;
  width: 480px;
  max-width: 95vw;
  box-shadow: var(--shadow);
}
.setup-logo {
  font-size: 2.2rem;
  font-weight: 800;
  background: linear-gradient(135deg, var(--accent), var(--purple));
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  text-align: center;
  margin-bottom: 0.3rem;
}
.setup-subtitle { color: var(--text-muted); text-align: center; margin-bottom: 1.5rem; font-size: 0.9rem; }
.steps { display: flex; justify-content: center; gap: 0.5rem; margin-bottom: 2rem; }
.step-dot {
  width: 10px; height: 10px; border-radius: 50%;
  background: var(--border); transition: background var(--transition);
}
.step-dot.active { background: var(--accent); }
.step-dot.done { background: var(--green); }
.step-panel { display: none; }
.step-panel.active { display: block; }
.step-title { font-size: 1.1rem; font-weight: 600; margin-bottom: 0.4rem; }
.step-desc { color: var(--text-muted); font-size: 0.85rem; margin-bottom: 1.2rem; }
.field { margin-bottom: 1rem; }
.field label { display: block; font-size: 0.85rem; color: var(--text-muted); margin-bottom: 0.3rem; }
.field input {
  width: 100%; padding: 0.7rem 0.8rem;
  background: var(--bg-input); border: 1px solid var(--border);
  border-radius: var(--radius); color: var(--text-primary);
  font-size: 0.95rem; outline: none; transition: border-color var(--transition);
}
.field input:focus { border-color: var(--accent); }
.field input::placeholder { color: var(--text-muted); }
.btn-row { display: flex; gap: 0.8rem; margin-top: 1.2rem; }
.btn {
  flex: 1; padding: 0.7rem;
  border: none; border-radius: var(--radius);
  font-size: 0.95rem; font-weight: 600;
  cursor: pointer; transition: all var(--transition);
}
.btn-primary {
  background: linear-gradient(135deg, #0369a1, #0284c7);
  color: white;
}
.btn-primary:hover { background: linear-gradient(135deg, #0284c7, #0ea5e9); }
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-secondary {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--text-muted);
}
.btn-secondary:hover { border-color: var(--accent); color: var(--text-primary); }
.toast {
  position: fixed; top: 1rem; right: 1rem;
  padding: 0.8rem 1.2rem; border-radius: var(--radius);
  font-size: 0.85rem; z-index: 9999;
  transition: opacity var(--transition); opacity: 0; pointer-events: none;
}
.toast.show { opacity: 1; pointer-events: auto; }
.toast.success { background: #065f46; color: #a7f3d0; }
.toast.error { background: #7f1d1d; color: #fca5a5; }
.skip-link {
  display: block; text-align: center; margin-top: 1rem;
  color: var(--text-muted); font-size: 0.8rem; cursor: pointer;
  text-decoration: none;
}
.skip-link:hover { color: var(--accent); }
</style>
</head>
<body>
<div class="setup-box">
  <div class="setup-logo">维造 Vizo</div>
  <div class="setup-subtitle">首次设置向导</div>
  <div class="steps">
    <div class="step-dot active" id="dot-0"></div>
    <div class="step-dot" id="dot-1"></div>
    <div class="step-dot" id="dot-2"></div>
  </div>

  <!-- Step 1: Password -->
  <div class="step-panel active" id="step-0">
    <div class="step-title">设置管理密码</div>
    <div class="step-desc">此密码用于登录 Web Console，至少 8 位。</div>
    <div class="field">
      <label>密码</label>
      <input type="password" id="setup-pwd" placeholder="输入密码 (至少 8 位)" autocomplete="new-password">
    </div>
    <div class="field">
      <label>确认密码</label>
      <input type="password" id="setup-pwd2" placeholder="再次输入密码">
    </div>
    <div class="btn-row">
      <button class="btn btn-primary" onclick="setupPassword()">下一步</button>
    </div>
  </div>

  <!-- Step 2: API Key -->
  <div class="step-panel" id="step-1">
    <div class="step-title">配置 API 连接</div>
    <div class="step-desc">配置主会话使用的 API，支持 Claude 官方、Anthropic 兼容接口和 OpenAPI 入口。</div>
    <div class="field">
      <label>API Key</label>
      <input type="password" id="setup-apikey" placeholder="输入 API Key" autocomplete="off">
    </div>
    <div class="field">
      <label>Base URL</label>
      <input type="text" id="setup-baseurl" value="https://api.anthropic.com" autocomplete="off">
    </div>
    <div class="btn-row">
      <button class="btn btn-secondary" onclick="prevStep()">上一步</button>
      <button class="btn btn-primary" onclick="setupApiKey()">下一步</button>
    </div>
    <a class="skip-link" onclick="nextStep()">跳过，稍后配置</a>
  </div>

  <!-- Step 3: Done -->
  <div class="step-panel" id="step-2">
    <div class="step-title">设置完成</div>
    <div class="step-desc">你已完成基础配置，可以进入控制台继续使用 Vizo。</div>
    <div class="btn-row">
      <button class="btn btn-primary" onclick="finishSetup()">进入控制台</button>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>
<script>
let _step = {{initial_step}};
const TOTAL = 3;

function showStep(n) {
  _step = n;
  for (let i = 0; i < TOTAL; i++) {
    document.getElementById('step-' + i).classList.toggle('active', i === n);
    const dot = document.getElementById('dot-' + i);
    dot.classList.toggle('active', i === n);
    dot.classList.toggle('done', i < n);
  }
}

function nextStep() { if (_step < TOTAL - 1) showStep(_step + 1); }
function prevStep() { if (_step > 0) showStep(_step - 1); }

function toast(msg, type) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast show ' + type;
  setTimeout(() => el.classList.remove('show'), 3000);
}

async function setupPassword() {
  const p1 = document.getElementById('setup-pwd').value;
  const p2 = document.getElementById('setup-pwd2').value;
  if (p1.length < 8) { toast('密码至少 8 位', 'error'); return; }
  if (p1 !== p2) { toast('两次密码不一致', 'error'); return; }
  try {
    const r = await fetch('/vizo/console/setup/password', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({password: p1})
    });
    const d = await r.json();
    if (d.success) { toast('密码设置成功', 'success'); nextStep(); }
    else { toast(d.error || '设置失败', 'error'); }
  } catch(e) { toast('网络错误', 'error'); }
}

async function setupApiKey() {
  const key = document.getElementById('setup-apikey').value.trim();
  const baseUrl = document.getElementById('setup-baseurl').value.trim();
  if (!key) { toast('请输入 API Key', 'error'); return; }
  try {
    const body = {api_key: key};
    if (baseUrl && baseUrl !== 'https://api.anthropic.com') body.base_url = baseUrl;
    const r = await fetch('/vizo/console/setup/apikey', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    const d = await r.json();
    if (d.success) { toast('API 配置已保存', 'success'); nextStep(); }
    else { toast(d.error || '保存失败', 'error'); }
  } catch(e) { toast('网络错误', 'error'); }
}

async function finishSetup() {
  try {
    const r = await fetch('/vizo/console/setup/complete', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'}
    });
    const d = await r.json();
    if (d.success) {
      window.location.href = d.redirect || '/vizo/console';
    } else { toast(d.error || '完成失败', 'error'); }
  } catch(e) { toast('网络错误', 'error'); }
}

showStep(_step);
</script>
</body>
</html>"""


class SetupHandler:
    """首次运行引导页的 HTTP 请求处理器"""

    def __init__(self, setup_wizard: SetupWizard, password_manager: PasswordManager):
        self._wizard = setup_wizard
        self._password_mgr = password_manager

    async def handle_setup_page(self, request):
        """GET /vizo/console/setup — 引导页"""
        from aiohttp import web
        if not self._wizard.is_first_run():
            raise web.HTTPFound("/vizo/console")
        initial_step = 0 if self._wizard.needs_password_step() else 1
        html = SETUP_HTML.replace("{{initial_step}}", str(initial_step))
        return web.Response(text=html, content_type="text/html")

    async def handle_setup_password(self, request):
        """POST /vizo/console/setup/password — 设置初始密码"""
        from aiohttp import web
        if not self._wizard.is_first_run():
            return web.json_response({"success": False, "error": "系统已初始化"})
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "请求格式错误"})
        password = data.get("password", "")
        if len(password) < 8:
            return web.json_response({"success": False, "error": "密码长度不能少于 8 位"})
        try:
            self._password_mgr.set_password(password)
        except RuntimeError as e:
            return web.json_response({"success": False, "error": str(e)})
        return web.json_response({"success": True})

    async def handle_setup_apikey(self, request):
        """POST /vizo/console/setup/apikey — 设置 API Key + Base URL"""
        from aiohttp import web
        if not self._wizard.is_first_run():
            return web.json_response({"success": False, "error": "系统已初始化"})
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "请求格式错误"})
        api_key = data.get("api_key", "").strip()
        base_url = data.get("base_url", "").strip()
        if not api_key:
            return web.json_response({"success": False, "error": "请输入 API Key"})
        resolved = resolve_main_session_connection(base_url, values=data)
        base_url_error = validate_main_session_base_url_for_access_mode(
            resolved["base_url"], resolved["access_mode"]
        )
        if base_url_error:
            return web.json_response({"success": False, "error": base_url_error})
        if (resolved["provider_id"] == "gateway" or is_openai_access_mode(resolved["access_mode"])) and not resolved["base_url"]:
            msg = "OpenAPI 必须填写 Base URL" if is_openai_access_mode(resolved["access_mode"]) else "Anthropic 兼容接口必须填写 Base URL"
            return web.json_response({"success": False, "error": msg})
        # 写入 .env 持久化，同时更新运行时缓存
        from lib.config_loader import write_env_file, load_config
        write_env_file({
            "OPUS_MAIN_API_KEY": api_key,
            "ANTHROPIC_API_KEY": "",
        })
        load_config(force_reload=True)
        with open(_CONFIG_FILE, 'r', encoding='utf-8') as f:
            config_data = json.load(f)
        anthropic_cfg = config_data.setdefault("external_models", {}).setdefault("anthropic", {})
        anthropic_cfg["api_key"] = api_key
        anthropic_cfg["base_url"] = resolved["base_url"]
        anthropic_cfg["provider_id"] = resolved["provider_id"]
        anthropic_cfg["access_mode"] = resolved["access_mode"]
        anthropic_cfg["provider_family"] = resolved["provider_family"]
        anthropic_cfg.pop("active_saved_connection_id", None)
        anthropic_env = anthropic_cfg.setdefault("env", {})
        for env_key in MAIN_SESSION_MANAGED_ENV_KEYS:
            anthropic_env.pop(env_key, None)
        anthropic_env.update(resolved["env"])
        tmp = str(_CONFIG_FILE) + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(config_data, f, indent=2, ensure_ascii=False)
            f.write('\n')
        os.replace(tmp, str(_CONFIG_FILE))
        load_config(force_reload=True)
        # 同步到 ~/.claude/settings.json，使新建主会话立即生效
        sync_main_session_to_claude_settings(
            api_key=api_key,
            base_url=resolved["base_url"],
            extra_env=resolved["env"],
            resolved_connection=resolved,
        )
        return web.json_response({"success": True})

    async def handle_setup_complete(self, request):
        """POST /vizo/console/setup/complete — 完成引导"""
        from aiohttp import web
        if not self._wizard.is_first_run():
            return web.json_response({"success": False, "error": "系统已初始化"})
        self._wizard.complete()
        resp_data = {"success": True, "redirect": "/vizo/console"}
        # 如果密码已设置，返回登录 Cookie
        if self._password_mgr.has_password():
            cookie_val = self._password_mgr.get_cookie_value()
            if cookie_val:
                resp = web.json_response(resp_data)
                resp.set_cookie(
                    "vizo_web_token", cookie_val,
                    httponly=True, path="/vizo",
                    max_age=30 * 86400, samesite="Lax",
                )
                return resp
        return web.json_response(resp_data)
