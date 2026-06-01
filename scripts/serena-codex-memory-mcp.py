#!/usr/bin/env python3
"""Codex-specific stdio MCP adapter for Serena."""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

import anyio


SCRIPT_HOME = Path(__file__).resolve().parent.parent
SERENA_HOME_DIR = SCRIPT_HOME / ".serena-codex-home"
GLOBAL_MEMORY_FILE = Path("/home/linshengbing/.codex/memories/global-startup.md")
LATEST_PROTOCOL_VERSION = "2025-06-18"
RESOURCE_SCHEME = "serena-memory"

os.environ.setdefault("SERENA_HOME", str(SERENA_HOME_DIR))

from serena.agent import SerenaAgent
from serena.cli import find_project_root
from serena.config.context_mode import SerenaAgentContext
from serena.config.serena_config import SerenaConfig
from serena.mcp import SerenaMCPFactory


def send(result_id: int | str | None, result: dict[str, Any] | None = None, error: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": result_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result or {}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def list_memory_files(memories_dir: Path) -> list[tuple[str, Path]]:
    if not memories_dir.is_dir():
        return []

    memories: list[tuple[str, Path]] = []
    for memory_file in sorted(memories_dir.rglob("*.md")):
        relative = memory_file.relative_to(memories_dir).as_posix()
        name = relative[:-3] if relative.endswith(".md") else relative
        memories.append((name, memory_file))
    return memories


def memory_uri(project_name: str, name: str) -> str:
    return f"{RESOURCE_SCHEME}://{project_name}/{quote(name, safe='/')}"


def parse_memory_name(project_name: str, uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme != RESOURCE_SCHEME or parsed.netloc != project_name:
        raise ValueError(f"Unsupported resource URI: {uri}")

    name = unquote(parsed.path.lstrip("/"))
    if not name:
        raise ValueError(f"Missing memory name in URI: {uri}")
    return name


def get_memory_map(memories_dir: Path) -> dict[str, Path]:
    return {name: path for name, path in list_memory_files(memories_dir)}


def resource_list(project_name: str, memories_dir: Path, index_uri: str) -> list[dict[str, Any]]:
    resources = [
        {
            "name": "__index__",
            "uri": index_uri,
            "description": f"Index of Serena memories available for the active {project_name} project.",
            "mimeType": "text/plain",
        }
    ]

    for name, path in list_memory_files(memories_dir):
        resources.append(
            {
                "name": name,
                "uri": memory_uri(project_name, name),
                "description": f"Serena memory '{name}' from the active {project_name} project.",
                "mimeType": "text/markdown",
                "size": path.stat().st_size,
            }
        )
    return resources


def read_resource(project_name: str, memories_dir: Path, index_uri: str, uri: str) -> dict[str, Any]:
    if uri == index_uri:
        memory_names = [name for name, _ in list_memory_files(memories_dir)]
        text = "\n".join(memory_names)
        return {
            "contents": [
                {
                    "uri": index_uri,
                    "mimeType": "text/plain",
                    "text": text,
                }
            ]
        }

    memory_name = parse_memory_name(project_name, uri)
    memory_map = get_memory_map(memories_dir)
    memory_path = memory_map.get(memory_name)
    if memory_path is None:
        raise FileNotFoundError(f"Unknown Serena memory: {memory_name}")

    return {
        "contents": [
            {
                "uri": uri,
                "mimeType": "text/markdown",
                "text": memory_path.read_text(encoding="utf-8"),
            }
        ]
    }


class SerenaCodexAdapter:
    def __init__(self) -> None:
        self._agent: SerenaAgent | None = None
        self._instructions = ""
        self._tools: dict[str, Any] = {}
        self._tool_payloads: list[dict[str, Any]] = []
        self._project_root: Path | None = None
        self._project_name = ""
        self._memories_dir: Path | None = None

    def _detect_project_root(self) -> Path:
        detected = find_project_root()
        if detected is None:
            return Path.cwd().resolve()
        return Path(detected).resolve()

    def ensure_ready(self) -> None:
        if self._agent is not None:
            return

        project_root = self._detect_project_root()
        project_name = project_root.name or "workspace"

        config = SerenaConfig.from_config_file()
        config.web_dashboard = False
        config.web_dashboard_open_on_launch = False
        config.gui_log_window = False

        context = SerenaAgentContext.load("codex")
        agent = SerenaAgent(project=str(project_root), serena_config=config, context=context)

        tool_payloads: list[dict[str, Any]] = []
        tools_by_name: dict[str, Any] = {}
        for tool in agent.get_exposed_tool_instances():
            mcp_tool = SerenaMCPFactory.make_mcp_tool(tool, openai_tool_compatible=True)
            tool_payload: dict[str, Any] = {
                "name": mcp_tool.name,
                "description": mcp_tool.description,
                "inputSchema": mcp_tool.parameters,
            }
            if mcp_tool.title:
                tool_payload["title"] = mcp_tool.title
            if mcp_tool.annotations is not None:
                tool_payload["annotations"] = mcp_tool.annotations.model_dump(by_alias=True, exclude_none=True)
            if mcp_tool.icons is not None:
                tool_payload["icons"] = [icon.model_dump(by_alias=True, exclude_none=True) for icon in mcp_tool.icons]
            if mcp_tool.meta is not None:
                tool_payload["_meta"] = mcp_tool.meta
            tool_payloads.append(tool_payload)
            tools_by_name[mcp_tool.name] = mcp_tool

        self._agent = agent
        self._project_root = project_root
        self._project_name = project_name
        self._memories_dir = project_root / ".serena" / "memories"
        self._instructions = agent.create_system_prompt()
        self._tool_payloads = sorted(tool_payloads, key=lambda item: item["name"])
        self._tools = tools_by_name

    async def _run_tool_async(self, mcp_tool: Any, arguments: dict[str, Any]) -> str:
        return await mcp_tool.fn_metadata.call_fn_with_arg_validation(
            mcp_tool.fn,
            mcp_tool.is_async,
            arguments,
            None,
        )

    def list_tools(self) -> dict[str, Any]:
        self.ensure_ready()
        return {"tools": self._tool_payloads}

    def call_tool(self, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        self.ensure_ready()
        mcp_tool = self._tools.get(name)
        if mcp_tool is None:
            message = f"Unknown tool: {name}"
            return {"content": [{"type": "text", "text": message}], "isError": True}

        try:
            result = anyio.run(self._run_tool_async, mcp_tool, arguments or {})
        except Exception as exc:  # pragma: no cover - defensive path for tool adapter failures
            message = f"Error executing tool: {exc.__class__.__name__} - {exc}"
            traceback.print_exc(file=sys.stderr)
            return {"content": [{"type": "text", "text": message}], "isError": True}

        is_error = isinstance(result, str) and result.startswith("Error")
        return {
            "content": [{"type": "text", "text": result}],
            "isError": is_error,
        }

    @property
    def instructions(self) -> str:
        self.ensure_ready()
        parts: list[str] = []
        if GLOBAL_MEMORY_FILE.is_file():
            parts.append(GLOBAL_MEMORY_FILE.read_text(encoding="utf-8").strip())
        parts.append(self._instructions)
        return "\n\n".join(part for part in parts if part)

    @property
    def project_name(self) -> str:
        self.ensure_ready()
        return self._project_name

    @property
    def memories_dir(self) -> Path:
        self.ensure_ready()
        assert self._memories_dir is not None
        return self._memories_dir

    @property
    def index_uri(self) -> str:
        return f"{RESOURCE_SCHEME}://{self.project_name}/__index__"


ADAPTER = SerenaCodexAdapter()


def handle_request(message: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    method = message.get("method")
    params = message.get("params") or {}

    if method == "initialize":
        requested_version = params.get("protocolVersion") or LATEST_PROTOCOL_VERSION
        return (
            {
                "protocolVersion": requested_version,
                "capabilities": {
                    "logging": {},
                    "resources": {"listChanged": False, "subscribe": False},
                    "prompts": {"listChanged": False},
                    "tools": {"listChanged": False},
                },
                "serverInfo": {
                    "name": "serena-codex-bridge",
                    "version": "1.0.0",
                },
                "instructions": ADAPTER.instructions,
            },
            None,
        )

    if method == "ping":
        return ({}, None)

    if method == "tools/list":
        return (ADAPTER.list_tools(), None)

    if method == "tools/call":
        return (ADAPTER.call_tool(params.get("name", ""), params.get("arguments")), None)

    if method == "resources/list":
        return ({"resources": resource_list(ADAPTER.project_name, ADAPTER.memories_dir, ADAPTER.index_uri)}, None)

    if method == "resources/templates/list":
        return (
            {
                "resourceTemplates": [
                    {
                        "name": "serena-memory",
                        "uriTemplate": f"{RESOURCE_SCHEME}://{ADAPTER.project_name}" + "/{memory_path}",
                        "description": "Read a Serena memory by its path, for example project-overview or architecture/web-console.",
                        "mimeType": "text/markdown",
                    }
                ]
            },
            None,
        )

    if method == "resources/read":
        try:
            return (read_resource(ADAPTER.project_name, ADAPTER.memories_dir, ADAPTER.index_uri, params.get("uri", "")), None)
        except FileNotFoundError as exc:
            return (None, {"code": -32001, "message": str(exc)})
        except ValueError as exc:
            return (None, {"code": -32602, "message": str(exc)})

    if method == "prompts/list":
        return ({"prompts": []}, None)

    if method == "logging/setLevel":
        return ({}, None)

    if method in {"notifications/initialized", "notifications/cancelled"}:
        return (None, None)

    return (None, {"code": -32601, "message": f"Method not found: {method}"})


def main() -> int:
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue

        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            send(None, error={"code": -32700, "message": f"Invalid JSON: {exc}"})
            continue

        request_id = message.get("id")
        try:
            result, error = handle_request(message)
        except Exception as exc:  # pragma: no cover - defensive path for unexpected bridge failures
            traceback.print_exc(file=sys.stderr)
            result = None
            error = {"code": -32000, "message": f"{exc.__class__.__name__}: {exc}"}

        if request_id is None:
            continue

        send(request_id, result=result, error=error)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
