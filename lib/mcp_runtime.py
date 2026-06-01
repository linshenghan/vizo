"""
MCP runtime helpers.

提供主会话/子代理共用的 MCP 配置归一化逻辑：
- `serena` 优先走项目内置可执行文件，避免依赖用户全局安装
- `mcp-chrome` 优先走当前 Python 解释器 + 绝对脚本路径
- `playwright` 优先走容器内预装的 `playwright-mcp` 二进制
- `vizo-router` 优先走当前 Python 解释器 + 绝对脚本路径
- Chrome 未连接时可选择性跳过 `mcp-chrome`
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import sys
from pathlib import Path

from lib.chrome_bridge import read_bridge_state

_LIB_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _LIB_DIR.parent
_CONFIG_FILE = _PROJECT_ROOT / "config.json"
_DEFAULT_SERENA_ARGS = ["start-mcp-server", "--project-from-cwd"]
_DEFAULT_PLAYWRIGHT_ARGS = [
    "-y",
    "@playwright/mcp@latest",
    "--headless",
    "--browser",
    "chromium",
    "--no-sandbox",
    "--isolated",
]

MCP_SERVICE_METADATA = {
    "serena": {
        "display_name": "Serena",
        "description": "项目记忆和代码理解依赖它，建议始终开启。",
        "is_core": True,
        "kind": "memory",
    },
    "mcp-chrome": {
        "display_name": "Chrome MCP",
        "description": "网页操作、截图和前端测试依赖它，做前端任务时建议开启。",
        "is_core": True,
        "kind": "browser",
    },
    "vizo-router": {
        "display_name": "Vizo Router",
        "description": "主会话语义路由、能力发现和交互设计流程依赖它，建议始终开启。",
        "is_core": True,
        "kind": "platform",
    },
    "jina": {
        "display_name": "Jina",
        "description": "用于网页搜索与抓取。",
        "is_core": False,
        "kind": "search",
    },
    "playwright": {
        "display_name": "Playwright",
        "description": "用于无头浏览器自动化、页面巡检和端到端交互测试。",
        "is_core": False,
        "kind": "browser",
    },
    "zai": {
        "display_name": "ZAI",
        "description": "第三方扩展能力。",
        "is_core": False,
        "kind": "extension",
    },
    "shadcn-ui": {
        "display_name": "Shadcn UI",
        "description": "用于组件生成与前端搭建辅助。",
        "is_core": False,
        "kind": "frontend",
    },
}
LOCKED_MCP_SERVICE_NAMES = ("serena", "vizo-router")
MAIN_SESSION_MCP_NAMES = ("serena", "mcp-chrome", "playwright", "vizo-router", "jina")


def load_mcp_servers_from(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        servers = data.get("mcpServers", {})
        return servers if isinstance(servers, dict) else {}
    except Exception:
        return {}


def get_mcp_service_metadata(name: str) -> dict:
    meta = MCP_SERVICE_METADATA.get(name, {})
    display_name = meta.get("display_name") or name
    description = meta.get("description") or "已安装的 MCP 工具。"
    return {
        "name": name,
        "display_name": display_name,
        "description": description,
        "is_core": bool(meta.get("is_core")),
        "kind": meta.get("kind") or "generic",
    }


def is_locked_mcp_service(name: str) -> bool:
    return name in LOCKED_MCP_SERVICE_NAMES


def load_mcp_service_state(config_data: dict | None = None) -> dict:
    data = config_data
    if data is None:
        try:
            data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    state = data.get("mcp_service_state", {})
    return state if isinstance(state, dict) else {}


def is_mcp_service_enabled(name: str, config_data: dict | None = None) -> bool:
    if is_locked_mcp_service(name):
        return True
    state = load_mcp_service_state(config_data)
    item = state.get(name)
    if isinstance(item, dict) and "enabled" in item:
        return bool(item.get("enabled"))
    if isinstance(item, bool):
        return item
    return True


def filter_enabled_mcp_servers(servers: dict, config_data: dict | None = None) -> dict:
    filtered: dict = {}
    for name, server in (servers or {}).items():
        if is_mcp_service_enabled(name, config_data):
            filtered[name] = server
    return filtered


def is_chrome_bridge_connected(port: int, timeout: float = 1.5) -> bool:
    del port, timeout
    try:
        return bool(read_bridge_state().get("chrome_connected"))
    except Exception:
        return False


def _main_session_mcp_env(
    *,
    session_id: str = "",
    session_dir: str = "",
    runtime_dir: str = "",
    project_root: str = "",
) -> dict[str, str]:
    env: dict[str, str] = {}
    clean_session_id = str(session_id or "").strip()
    clean_session_dir = str(session_dir or "").strip()
    clean_runtime_dir = str(runtime_dir or "").strip()
    clean_project_root = str(project_root or "").strip()

    if clean_runtime_dir:
        runtime_path = Path(clean_runtime_dir).resolve()
        env["VIZO_MAIN_SESSION_RUNTIME_DIR"] = str(runtime_path)
        clean_session_dir = clean_session_dir or str(runtime_path.parent)

    if clean_session_id:
        env["VIZO_MAIN_SESSION_ID"] = clean_session_id

    if clean_session_dir:
        env["VIZO_MAIN_SESSION_DIR"] = str(Path(clean_session_dir).resolve())

    if clean_project_root:
        env["VIZO_PROJECT_ROOT"] = str(Path(clean_project_root).resolve())

    return env


def normalize_mcp_servers(
    servers: dict,
    *,
    confirm_port: int = 9390,
    drop_unhealthy_chrome: bool = False,
    config_data: dict | None = None,
    managed_env: dict[str, str] | None = None,
) -> dict:
    normalized: dict = {}
    chrome_ok = True
    if drop_unhealthy_chrome:
        chrome_ok = is_chrome_bridge_connected(confirm_port)

    enabled_servers = filter_enabled_mcp_servers(servers, config_data=config_data)

    for name, server in enabled_servers.items():
        if not isinstance(server, dict):
            continue
        if name == "serena":
            normalized[name] = _normalize_serena_server(server)
        elif name == "mcp-chrome":
            if drop_unhealthy_chrome and not chrome_ok:
                continue
            normalized[name] = _normalize_chrome_server(server, confirm_port=confirm_port)
        elif name == "playwright":
            normalized[name] = _normalize_playwright_server(server)
        elif name == "vizo-router":
            normalized[name] = _normalize_vizo_router_server(server, managed_env=managed_env)
        else:
            normalized[name] = copy.deepcopy(server)

    return normalized


def build_main_session_mcp_config(
    work_dir: str | None = None,
    *,
    confirm_port: int = 9390,
    config_data: dict | None = None,
    session_id: str = "",
    session_dir: str = "",
    runtime_dir: str = "",
    project_root: str = "",
) -> dict:
    """Build the managed MCP config shared by main sessions and Codex workers."""
    merged: dict = {}
    candidate_paths: list[Path] = []

    if work_dir:
        candidate_paths.append(Path(work_dir) / ".mcp.json")
    candidate_paths.append(_PROJECT_ROOT / ".mcp.json")
    candidate_paths.append(Path.home() / ".claude" / ".mcp.json")

    seen_paths: set[str] = set()
    for path in candidate_paths:
        resolved = str(path.resolve()) if path.exists() else str(path)
        if resolved in seen_paths or not path.exists():
            continue
        seen_paths.add(resolved)
        servers = load_mcp_servers_from(path)
        for name in MAIN_SESSION_MCP_NAMES:
            if name in servers and name not in merged:
                merged[name] = servers[name]

    loaded_config = config_data
    if loaded_config is None:
        try:
            loaded_config = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            loaded_config = {}

    merged = filter_enabled_mcp_servers(merged, config_data=loaded_config)
    managed_env = _main_session_mcp_env(
        session_id=session_id,
        session_dir=session_dir,
        runtime_dir=runtime_dir,
        project_root=project_root,
    )
    return {
        "mcpServers": normalize_mcp_servers(
            merged,
            confirm_port=confirm_port,
            drop_unhealthy_chrome=True,
            config_data=loaded_config,
            managed_env=managed_env,
        )
    }


def build_codex_mcp_config_overrides(
    work_dir: str | None = None,
    *,
    confirm_port: int = 9390,
    config_data: dict | None = None,
    allowed_server_names: list[str] | tuple[str, ...] | set[str] | None = None,
    session_id: str = "",
    session_dir: str = "",
    runtime_dir: str = "",
    project_root: str = "",
) -> list[str]:
    servers = build_main_session_mcp_config(
        work_dir,
        confirm_port=confirm_port,
        config_data=config_data,
        session_id=session_id,
        session_dir=session_dir,
        runtime_dir=runtime_dir,
        project_root=project_root,
    ).get("mcpServers", {})
    if allowed_server_names is not None:
        allowed = {str(name).strip() for name in allowed_server_names if str(name).strip()}
        servers = {name: server for name, server in servers.items() if name in allowed}
    overrides: list[str] = []
    for name, server in servers.items():
        key = f"mcp_servers.{_toml_path_segment(name)}"
        overrides.append(f"{key}={_to_inline_toml(server)}")
    return overrides


def build_main_session_mcp_env(
    *,
    session_id: str = "",
    session_dir: str = "",
    runtime_dir: str = "",
    project_root: str = "",
) -> dict[str, str]:
    return _main_session_mcp_env(
        session_id=session_id,
        session_dir=session_dir,
        runtime_dir=runtime_dir,
        project_root=project_root,
    )


def _normalize_serena_server(server: dict) -> dict:
    normalized = copy.deepcopy(server)
    packaged_serena = _find_packaged_serena_bin()
    if packaged_serena:
        normalized["command"] = packaged_serena

    args = normalized.get("args")
    if not isinstance(args, list) or not args:
        normalized["args"] = list(_DEFAULT_SERENA_ARGS)

    env = normalized.get("env")
    if not isinstance(env, dict):
        env = {}

    runtime_root = Path.home() / ".vizo"
    cache_root = runtime_root / "cache"
    serena_home = runtime_root / "serena-home"
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
        serena_home.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    env.setdefault("SERENA_HOME", str(serena_home))
    env.setdefault("XDG_CACHE_HOME", str(cache_root))
    env.setdefault("UV_CACHE_DIR", str(cache_root / "uv"))
    normalized["env"] = env
    return normalized


def _normalize_chrome_server(server: dict, *, confirm_port: int) -> dict:
    normalized = copy.deepcopy(server)
    bridge_script = (_PROJECT_ROOT / "lib" / "chrome_mcp_stdio.py").resolve()
    if bridge_script.exists():
        normalized["command"] = sys.executable or normalized.get("command") or "python3"
        normalized["args"] = [str(bridge_script)]

    env = normalized.get("env")
    if not isinstance(env, dict):
        env = {}
    env.setdefault("CHROME_BRIDGE_URL", f"http://127.0.0.1:{confirm_port}/vizo/chrome/mcp")
    normalized["env"] = env
    return normalized


def _normalize_playwright_server(server: dict) -> dict:
    normalized = copy.deepcopy(server)
    command = str(normalized.get("command") or "").strip()
    args = normalized.get("args")
    clean_args = [str(item) for item in args if str(item).strip()] if isinstance(args, list) else []
    if not clean_args:
        clean_args = list(_DEFAULT_PLAYWRIGHT_ARGS)

    command_name = Path(command).name.lower()
    packaged_playwright = _find_playwright_mcp_bin()
    if packaged_playwright and command_name in {"", "npx", "npx.cmd", "playwright-mcp", "playwright-mcp.cmd"}:
        normalized["command"] = packaged_playwright
        normalized["args"] = _strip_playwright_package_args(clean_args)
    else:
        normalized["args"] = clean_args
    return normalized


def _normalize_vizo_router_server(server: dict, *, managed_env: dict[str, str] | None = None) -> dict:
    normalized = copy.deepcopy(server)
    router_script = (_PROJECT_ROOT / "lib" / "vizo_router_mcp_stdio.py").resolve()
    if router_script.exists():
        normalized["command"] = sys.executable or normalized.get("command") or "python3"
        normalized["args"] = [str(router_script)]
    clean_env = {str(k): str(v) for k, v in (managed_env or {}).items() if str(k).startswith("VIZO_") and str(v)}
    if clean_env:
        existing_env = normalized.get("env")
        if not isinstance(existing_env, dict):
            existing_env = {}
        merged_env = {**existing_env, **clean_env}
        normalized["env"] = merged_env
    return normalized


def _find_packaged_serena_bin() -> str | None:
    candidates = [
        _PROJECT_ROOT / ".serena-venv312" / "bin" / "serena",
        _PROJECT_ROOT / ".serena-venv312" / "Scripts" / "serena.exe",
        _PROJECT_ROOT / ".serena-venv312" / "Scripts" / "serena",
        _PROJECT_ROOT / ".serena-venv" / "bin" / "serena",
        _PROJECT_ROOT / ".serena-venv" / "Scripts" / "serena.exe",
        _PROJECT_ROOT / ".serena-venv" / "Scripts" / "serena",
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _find_playwright_mcp_bin() -> str | None:
    for candidate in ("playwright-mcp", "playwright-mcp.cmd"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def _strip_playwright_package_args(args: list[str]) -> list[str]:
    stripped = list(args)
    while stripped and stripped[0] in {"-y", "--yes"}:
        stripped.pop(0)
    if stripped and stripped[0].startswith("@playwright/mcp"):
        stripped.pop(0)
    return stripped


def _toml_path_segment(name: str) -> str:
    value = str(name or "").strip()
    if re.match(r"^[A-Za-z0-9_-]+$", value):
        return value
    return json.dumps(value, ensure_ascii=False)


def _toml_inline_key(name: str) -> str:
    value = str(name or "").strip()
    if re.match(r"^[A-Za-z0-9_-]+$", value):
        return value
    return json.dumps(value, ensure_ascii=False)


def _to_inline_toml(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ",".join(_to_inline_toml(item) for item in value) + "]"
    if isinstance(value, dict):
        parts = [f"{_toml_inline_key(key)}={_to_inline_toml(item)}" for key, item in value.items()]
        return "{" + ",".join(parts) + "}"
    return json.dumps(str(value), ensure_ascii=False)
