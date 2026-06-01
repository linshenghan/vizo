"""
统一配置加载器 — 单一入口读取 config.json

提供缓存的配置读取和便捷方法。
lib/ 内部模块暂不直接导入此模块（因 lib/__init__.py 链式导入限制），
但 hooks/ 和根目录模块可以使用。
"""
import json
import os
from pathlib import Path

from lib.project_identity import (
    MAINLINE_PROJECT,
    normalize_mainline_project_path,
    resolve_project_identity,
)

_LIB_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _LIB_DIR.parent
CONFIG_FILE = _PROJECT_ROOT / 'config.json'
ENV_FILE = _PROJECT_ROOT / '.env'

_cached_config = None
MAIN_SESSION_API_KEY_ENV_VARS = ("OPUS_MAIN_API_KEY", "ANTHROPIC_API_KEY")


def _read_env_file() -> dict:
    """读取 .env 文件，返回 key→value 字典（忽略注释和空行）。"""
    result = {}
    if not ENV_FILE.exists():
        return result
    for line in ENV_FILE.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' in line:
            k, v = line.split('=', 1)
            result[k.strip()] = v.strip()
    return result


def write_env_file(updates: dict):
    """将 updates 中的 key=value 写入 .env 文件，同时更新 os.environ 并重置配置缓存。"""
    global _cached_config
    existing = _read_env_file()
    for k, v in updates.items():
        if v is None or v == "":
            existing.pop(k, None)
        else:
            existing[k] = v
    lines = [f"{k}={v}" for k, v in existing.items() if v is not None and v != ""]
    ENV_FILE.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    for k, v in updates.items():
        if v is None or v == "":
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    _cached_config = None


def ensure_main_session_env_file_migrated() -> bool:
    """将主会话旧 ANTHROPIC_API_KEY 迁移为 OPUS_MAIN_API_KEY，并移除冲突源。"""
    global _cached_config
    existing = _read_env_file()
    legacy_key = str(existing.get("ANTHROPIC_API_KEY", "") or "").strip()
    internal_key = str(existing.get("OPUS_MAIN_API_KEY", "") or "").strip()
    if not legacy_key:
        return False

    if not internal_key:
        existing["OPUS_MAIN_API_KEY"] = legacy_key
        os.environ["OPUS_MAIN_API_KEY"] = legacy_key
    existing.pop("ANTHROPIC_API_KEY", None)
    os.environ.pop("ANTHROPIC_API_KEY", None)

    lines = [f"{k}={v}" for k, v in existing.items() if v is not None and v != ""]
    ENV_FILE.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    _cached_config = None
    return True


def read_main_session_api_key_from_env_file() -> str:
    """读取主会话 API Key，优先 Opus 自有变量，兼容旧 ANTHROPIC_API_KEY。"""
    env_vals = _read_env_file()
    for env_var in MAIN_SESSION_API_KEY_ENV_VARS:
        val = str(env_vals.get(env_var, "") or "").strip()
        if val:
            return val
    return ""

# 环境变量 → config 嵌套路径映射
ENV_VAR_MAPPING = {
    "ANTHROPIC_API_KEY":   (("external_models", "anthropic", "api_key"), str),
    "OPUS_MAIN_API_KEY":   (("external_models", "anthropic", "api_key"), str),
    "ANTHROPIC_BASE_URL":  (("external_models", "anthropic", "base_url"), str),
    "ANTHROPIC_DEFAULT_OPUS_MODEL":   (("external_models", "anthropic", "env", "ANTHROPIC_DEFAULT_OPUS_MODEL"), str),
    "ANTHROPIC_DEFAULT_SONNET_MODEL": (("external_models", "anthropic", "env", "ANTHROPIC_DEFAULT_SONNET_MODEL"), str),
    "ANTHROPIC_DEFAULT_HAIKU_MODEL":  (("external_models", "anthropic", "env", "ANTHROPIC_DEFAULT_HAIKU_MODEL"), str),
    "DEEPSEEK_API_KEY":    (("external_models", "deepseek", "api_key"), str),
    "GLM_API_KEY":         (("external_models", "glm", "api_key"), str),
    "QWEN_API_KEY":        (("external_models", "qwen", "api_key"), str),
    "WEB_CONSOLE_TOKEN":   (("web_console", "token"), str),
    "REDIS_HOST":          (("redis", "host"), str),
    "REDIS_PORT":          (("redis", "port"), int),
    "CONFIRM_SERVER_HOST": (("confirm_server", "host"), str),
    "CONFIRM_SERVER_PORT": (("confirm_server", "port"), int),
    "CONFIRM_SERVER_USE_TUNNEL": (
        ("confirm_server", "use_cloudflare_tunnel"),
        lambda v: v.lower() == "true",
    ),
    # --- secrets ---
    "WXPUSHER_APP_TOKEN":      (("secrets", "wxpusher_app_token"), str),
    "MODELSCOPE_API_KEY":      (("secrets", "modelscope_api_key"), str),
    "GITHUB_TOKEN":            (("secrets", "github_token"), str),
}


def _deep_set(d: dict, path: tuple, value):
    """递归设置嵌套字典值，中间节点不存在时自动创建空 dict。"""
    for key in path[:-1]:
        d = d.setdefault(key, {})
    d[path[-1]] = value


def _write_config_file(config: dict):
    tmp_path = CONFIG_FILE.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(CONFIG_FILE)


def _normalize_projects_config(config: dict) -> bool:
    projects = config.get("projects")
    if not isinstance(projects, dict):
        return False

    changed = False
    normalized_projects = {}

    for name, info in projects.items():
        target_name = name
        if not isinstance(info, dict):
            normalized_projects[name] = info
            continue

        normalized_info = dict(info)
        path = str(normalized_info.get("path", "") or "").strip()
        normalized_path = normalize_mainline_project_path(path, project_name=name)
        if normalized_path and normalized_path != path:
            normalized_info["path"] = normalized_path
            changed = True
        target_name = resolve_project_identity(name, normalized_info.get("path", path)) or name
        if target_name == MAINLINE_PROJECT and normalized_info.get("serena_project") != MAINLINE_PROJECT:
            normalized_info["serena_project"] = MAINLINE_PROJECT
            changed = True
        if target_name != name:
            changed = True

        existing = normalized_projects.get(target_name)
        if existing is None:
            normalized_projects[target_name] = normalized_info
            continue

        merged = dict(existing)
        for key, value in normalized_info.items():
            if key in {"path", "serena_project"} and value:
                merged[key] = value
            elif key not in merged or merged[key] in ("", None, {}):
                merged[key] = value
        normalized_projects[target_name] = merged

    if normalized_projects != projects:
        config["projects"] = normalized_projects
        changed = True

    return changed


def load_config(force_reload=False):
    """加载 config.json 并缓存，支持环境变量覆盖。

    Args:
        force_reload: 强制重新读取文件（忽略缓存）

    Returns:
        完整的配置字典
    """
    global _cached_config
    if _cached_config is not None and not force_reload:
        return _cached_config
    ensure_main_session_env_file_migrated()
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        config = json.load(f)
    if _normalize_projects_config(config):
        _write_config_file(config)

    # .env 文件变量注入 os.environ（OS 已有的不覆盖）
    for k, v in _read_env_file().items():
        if not os.environ.get(k):
            os.environ[k] = v

    # 环境变量覆盖：非空值覆盖对应 config 路径
    # 注意：ANTHROPIC_ 开头的变量只从 .env 文件读取，不从 os.environ 继承
    # 原因：os.environ 可能包含 PTY/claude 主会话的旧值（如切换连接后残留的旧 BASE_URL），
    #       这些值会错误覆盖 config.json 中的正确配置
    env_from_file = _read_env_file()
    for env_var, (path, converter) in ENV_VAR_MAPPING.items():
        if env_var.startswith("ANTHROPIC_"):
            val = env_from_file.get(env_var, "")
        else:
            val = os.environ.get(env_var, "")
        if val:
            try:
                _deep_set(config, path, converter(val))
            except (ValueError, TypeError):
                pass  # 类型转换失败时保留 config.json 原值

    _cached_config = config
    return config


def get_redis_config():
    """返回 config["redis"]，默认 {}"""
    return load_config().get('redis', {})


def get_secrets():
    """返回 config["secrets"]，默认 {}"""
    return load_config().get('secrets', {})


def is_placeholder(value) -> bool:
    """判断配置值是否为占位符（未配置的状态）"""
    if not value:
        return True
    if isinstance(value, str):
        if value.startswith("YOUR_"):
            return True
        if "请修改" in value or "请填写" in value:
            return True
    return False
