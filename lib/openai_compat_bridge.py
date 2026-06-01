"""
OpenAI-compatible upstream bridge for Claude Code.

将 Claude / Anthropic Messages API 请求转换为 OpenAI-compatible
chat/completions 请求，并暴露给 Claude Code 主会话与子代理使用。
"""

from __future__ import annotations

import json
import logging
import math
import uuid
from typing import Any
from urllib.parse import quote, urlparse, urlunparse

from aiohttp import ClientSession, ClientTimeout, web

logger = logging.getLogger("openai_bridge")

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
OPENAI_BRIDGE_PREFIX = "/vizo/bridge/openai"
_OPENAI_ENDPOINT_SUFFIXES = (
    "/chat/completions",
    "/responses",
    "/completions",
)
_ALL_CLAUDE_MODEL_ALIASES = {
    "opus": ("opus", "claude-opus-4-7", "claude-opus-4-6"),
    "sonnet": ("sonnet", "claude-sonnet-4-7", "claude-sonnet-4-6", "claude-sonnet-4-5-20250929"),
    "haiku": ("haiku", "claude-haiku-4-7", "claude-haiku-4-5", "claude-haiku-4-5-20251001"),
}
_OPENAI_PROVIDER_DISPLAY_NAMES = {
    "openai": "OpenAI 兼容接口",
    "codex": "Codex 兼容接口",
    "gemini": "Gemini（OpenAI 兼容）",
    "openrouter": "OpenRouter（OpenAI 兼容）",
    "generic": "OpenAI 兼容接口",
}
_OPENAI_NEXT_PLAN_RETRY_STATUSES = frozenset((400, 404, 405, 415, 422, 501))
_OPENAI_NESTED_CLI_HEADER_NAMES = frozenset((
    "x-codex-active-limit",
    "x-codex-limit-reset-seconds",
))
_OPENAI_NESTED_CLI_TEXT_MARKERS = (
    "you are a coding agent running in the codex cli",
    "you are operating in the codex ide assistant mode",
    "running in the codex cli",
)


def _get_confirm_server_port(default: int = 9390) -> int:
    try:
        from lib.config_loader import load_config

        config = load_config()
        port = int(config.get("confirm_server", {}).get("port", default) or default)
        return port if port > 0 else default
    except Exception:
        return default


def build_openai_bridge_base_url(scope: str = "main", model_id: str | None = None,
                                 port: int | None = None) -> str:
    """生成 Claude Code 可直连的本地桥接 Base URL。"""
    use_port = int(port or _get_confirm_server_port())
    if scope == "main":
        return f"http://127.0.0.1:{use_port}{OPENAI_BRIDGE_PREFIX}/main"
    if scope == "external" and model_id:
        encoded = quote(str(model_id).strip(), safe="")
        return f"http://127.0.0.1:{use_port}{OPENAI_BRIDGE_PREFIX}/external/{encoded}"
    raise ValueError(f"unsupported bridge scope: {scope}")


def is_openai_bridge_base_url(base_url: str | None) -> bool:
    """判断 URL 是否为本地 OpenAI→Anthropic 桥接地址。"""
    try:
        parsed = urlparse(str(base_url or "").strip())
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if (parsed.hostname or "").lower() not in ("127.0.0.1", "localhost"):
        return False
    return (parsed.path or "").startswith(OPENAI_BRIDGE_PREFIX + "/")


def normalize_openai_compatible_base_url(base_url: str | None,
                                         default_base_url: str | None = DEFAULT_OPENAI_BASE_URL,
                                         allow_blank: bool = False) -> str:
    """规范化 OpenAI-compatible Base URL，允许用户填写完整 endpoint。"""
    raw = str(base_url or "").strip().rstrip("/")
    if not raw:
        if default_base_url:
            return str(default_base_url).strip().rstrip("/")
        return "" if allow_blank else DEFAULT_OPENAI_BASE_URL
    try:
        parsed = urlparse(raw)
    except Exception:
        return raw
    path = (parsed.path or "").rstrip("/")
    lower_path = path.lower()
    for suffix in _OPENAI_ENDPOINT_SUFFIXES:
        if lower_path.endswith(suffix):
            path = path[:-len(suffix)]
            parsed = parsed._replace(path=path or "")
            return urlunparse(parsed).rstrip("/")
    return raw


def validate_openai_compatible_base_url(base_url: str | None) -> str:
    """校验 OpenAI-compatible Base URL。"""
    raw = str(base_url or "").strip()
    if not raw:
        return ""
    try:
        path = (urlparse(raw).path or "").rstrip("/").lower()
    except Exception:
        return ""
    if not path:
        return ""
    if path.endswith("/messages") or path.endswith("/v1/messages"):
        return "这看起来是 Anthropic Messages 接口地址。OpenAI 兼容接口请填写 /v1 或供应商提供的 OpenAPI 根路径，例如 /codex/v1。"
    if path.endswith("/count_tokens") or path.endswith("/v1/messages/count_tokens"):
        return "OpenAI 兼容接口不需要填写 /count_tokens；请填写根路径，例如 /v1 或供应商提供的原生 OpenAI 入口。"
    return ""


def build_openai_chat_completions_url(base_url: str | None) -> str:
    """构造上游 chat/completions 地址。"""
    root = normalize_openai_compatible_base_url(base_url)
    return root.rstrip("/") + "/chat/completions"


def build_openai_responses_url(base_url: str | None) -> str:
    """构造上游 /responses 地址。"""
    root = normalize_openai_compatible_base_url(base_url)
    return root.rstrip("/") + "/responses"


def infer_openai_compatible_provider_family(base_url: str | None,
                                            provider_family: str | None = None) -> str:
    """根据 Base URL 和提示信息推断 OpenAI-compatible 上游家族。"""
    hinted_family = str(provider_family or "").strip().lower()
    root = normalize_openai_compatible_base_url(
        base_url,
        default_base_url="",
        allow_blank=True,
    )
    try:
        parsed = urlparse(root)
    except Exception:
        parsed = urlparse("")
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()

    if hinted_family in ("gemini", "openrouter", "codex"):
        return hinted_family
    if host.endswith("googleapis.com") or "generativelanguage" in host or "vertex" in host or "gemini" in host:
        return "gemini"
    if host.endswith("openrouter.ai"):
        return "openrouter"
    if "/codex" in path or hinted_family == "codex":
        return "codex"
    if host == "api.openai.com" or host.endswith(".openai.com"):
        return "openai"
    if hinted_family not in ("", "openai", "custom", "generic"):
        return hinted_family
    return "generic" if host else "openai"


def infer_openai_provider_capabilities(base_url: str | None,
                                       provider_family: str | None = None) -> dict[str, Any]:
    """返回 OpenAI-compatible 上游的能力画像，供桥接和探测逻辑统一使用。"""
    family = infer_openai_compatible_provider_family(base_url, provider_family=provider_family)
    preferred_endpoint = "responses" if family in ("openai", "codex") else "chat_completions"
    return {
        "provider_family": family,
        "display_name": _OPENAI_PROVIDER_DISPLAY_NAMES.get(
            family,
            _OPENAI_PROVIDER_DISPLAY_NAMES["generic"],
        ),
        "supports_tools": True,
        "supports_stream": True,
        "supports_chat_completions": True,
        "supports_responses_api": family in ("openai", "codex"),
        "supports_reasoning_controls": family in ("openai", "codex", "gemini"),
        "preferred_endpoint": preferred_endpoint,
    }


def should_use_max_completion_tokens(model: str | None) -> bool:
    """判断是否优先使用 max_completion_tokens。"""
    normalized = str(model or "").strip().lower()
    if not normalized:
        return False
    return normalized.startswith("gpt-5") or normalized.startswith("o1") or normalized.startswith("o3") or normalized.startswith("o4")


def apply_openai_token_limit(payload: dict[str, Any], model: str | None,
                             max_tokens: int | None) -> dict[str, Any]:
    """为 OpenAI-compatible payload 写入合适的 token 上限字段。"""
    if max_tokens is None:
        return payload
    if should_use_max_completion_tokens(model):
        payload["max_completion_tokens"] = max_tokens
    else:
        payload["max_tokens"] = max_tokens
    return payload


def build_openai_token_limit_fallback_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    """生成 max_completion_tokens / max_tokens 的回退请求体。"""
    if "max_completion_tokens" in payload and "max_tokens" not in payload:
        alt = dict(payload)
        alt["max_tokens"] = alt.pop("max_completion_tokens")
        return alt
    if "max_tokens" in payload and "max_completion_tokens" not in payload:
        alt = dict(payload)
        alt["max_completion_tokens"] = alt.pop("max_tokens")
        return alt
    return None


def _json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _stringify_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        if value.get("type") == "text":
            return str(value.get("text") or "")
        if "text" in value and isinstance(value.get("text"), str):
            return value["text"]
        return _json_dumps(value)
    if isinstance(value, list):
        parts = []
        for item in value:
            text = _stringify_content(item)
            if text:
                parts.append(text)
        return "\n".join(parts)
    return str(value)


def _system_to_text(system_value: Any) -> str:
    if isinstance(system_value, list):
        parts = []
        for block in system_value:
            if isinstance(block, dict) and block.get("type") == "text":
                text = str(block.get("text") or "")
                if text:
                    parts.append(text)
        return "\n".join(parts)
    return _stringify_content(system_value)


def _image_block_to_part(block: dict[str, Any]) -> dict[str, Any] | None:
    source = block.get("source") or {}
    if source.get("type") == "base64" and source.get("data"):
        media_type = source.get("media_type") or "image/png"
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{source['data']}"},
        }
    url = block.get("url") or source.get("url")
    if url:
        return {"type": "image_url", "image_url": {"url": str(url)}}
    return None


def _build_openai_user_content(parts: list[dict[str, Any]]) -> Any:
    if not parts:
        return ""
    if all(part.get("type") == "text" for part in parts):
        return "".join(str(part.get("text") or "") for part in parts)
    return parts


def convert_anthropic_messages_to_openai(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Anthropic messages -> OpenAI chat/completions messages。"""
    messages: list[dict[str, Any]] = []

    system_text = _system_to_text(body.get("system"))
    if system_text:
        messages.append({"role": "system", "content": system_text})

    for raw_message in body.get("messages") or []:
        role = str(raw_message.get("role") or "user")
        content = raw_message.get("content", "")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            messages.append({"role": role, "content": _stringify_content(content)})
            continue

        if role == "assistant":
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for block in content:
                if not isinstance(block, dict):
                    text = _stringify_content(block)
                    if text:
                        text_parts.append(text)
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    text = str(block.get("text") or "")
                    if text:
                        text_parts.append(text)
                elif block_type == "tool_use":
                    tool_calls.append({
                        "id": block.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                        "type": "function",
                        "function": {
                            "name": block.get("name") or "tool",
                            "arguments": _json_dumps(block.get("input") or {}),
                        },
                    })
                elif block_type in ("thinking", "redacted_thinking"):
                    continue
                else:
                    text = _stringify_content(block)
                    if text:
                        text_parts.append(text)
            message = {"role": "assistant", "content": "".join(text_parts)}
            if tool_calls:
                message["tool_calls"] = tool_calls
            messages.append(message)
            continue

        pending_parts: list[dict[str, Any]] = []

        def flush_user_parts():
            nonlocal pending_parts
            if not pending_parts:
                return
            messages.append({
                "role": "user",
                "content": _build_openai_user_content(pending_parts),
            })
            pending_parts = []

        for block in content:
            if not isinstance(block, dict):
                text = _stringify_content(block)
                if text:
                    pending_parts.append({"type": "text", "text": text})
                continue
            block_type = block.get("type")
            if block_type == "text":
                text = str(block.get("text") or "")
                if text:
                    pending_parts.append({"type": "text", "text": text})
            elif block_type == "image":
                image_part = _image_block_to_part(block)
                if image_part:
                    pending_parts.append(image_part)
            elif block_type == "tool_result":
                flush_user_parts()
                tool_text = _stringify_content(block.get("content") if "content" in block else block)
                messages.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id") or block.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                    "content": tool_text or "",
                })
            elif block_type in ("thinking", "redacted_thinking"):
                continue
            else:
                text = _stringify_content(block)
                if text:
                    pending_parts.append({"type": "text", "text": text})
        flush_user_parts()

    if not messages:
        messages.append({"role": "user", "content": "ping"})
    return messages


def convert_anthropic_tools_to_openai(body: dict[str, Any]) -> list[dict[str, Any]]:
    tools = []
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        tools.append({
            "type": "function",
            "function": {
                "name": tool.get("name") or "tool",
                "description": tool.get("description") or "",
                "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
            },
        })
    return tools


def convert_tool_choice(tool_choice: Any) -> Any:
    if not tool_choice:
        return None
    if isinstance(tool_choice, str):
        if tool_choice in ("auto", "none", "required"):
            return tool_choice
        return None
    if not isinstance(tool_choice, dict):
        return None
    choice_type = str(tool_choice.get("type") or "").strip()
    if choice_type == "auto":
        return "auto"
    if choice_type == "any":
        return "required"
    if choice_type == "tool":
        name = str(tool_choice.get("name") or "").strip()
        if name:
            return {"type": "function", "function": {"name": name}}
    return None


def build_openai_chat_payload(body: dict[str, Any], target_model: str) -> dict[str, Any]:
    payload = {
        "model": target_model,
        "messages": convert_anthropic_messages_to_openai(body),
        "stream": bool(body.get("stream")),
    }
    max_tokens = body.get("max_tokens")
    apply_openai_token_limit(payload, target_model, max_tokens)
    if body.get("temperature") is not None:
        payload["temperature"] = body.get("temperature")
    if body.get("top_p") is not None:
        payload["top_p"] = body.get("top_p")
    stop_sequences = body.get("stop_sequences")
    if stop_sequences:
        payload["stop"] = stop_sequences
    tools = convert_anthropic_tools_to_openai(body)
    if tools:
        payload["tools"] = tools
        tool_choice = convert_tool_choice(body.get("tool_choice"))
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
    return payload


def _build_responses_message_content(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not parts:
        return [{"type": "input_text", "text": ""}]
    if all(part.get("type") == "input_text" for part in parts):
        return [{
            "type": "input_text",
            "text": "".join(str(part.get("text") or "") for part in parts),
        }]
    return parts


def convert_anthropic_messages_to_responses_input(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Anthropic messages -> OpenAI Responses API input items。"""
    items: list[dict[str, Any]] = []

    for raw_message in body.get("messages") or []:
        role = str(raw_message.get("role") or "user")
        content = raw_message.get("content", "")
        if isinstance(content, str):
            items.append({
                "type": "message",
                "role": role,
                "content": [{"type": "input_text", "text": content}],
            })
            continue
        if not isinstance(content, list):
            items.append({
                "type": "message",
                "role": role,
                "content": [{"type": "input_text", "text": _stringify_content(content)}],
            })
            continue

        pending_parts: list[dict[str, Any]] = []

        def flush_message():
            nonlocal pending_parts
            if not pending_parts:
                return
            items.append({
                "type": "message",
                "role": role,
                "content": _build_responses_message_content(pending_parts),
            })
            pending_parts = []

        for block in content:
            if not isinstance(block, dict):
                text = _stringify_content(block)
                if text:
                    pending_parts.append({"type": "input_text", "text": text})
                continue

            block_type = block.get("type")
            if block_type == "text":
                text = str(block.get("text") or "")
                if text:
                    pending_parts.append({"type": "input_text", "text": text})
            elif block_type == "image":
                image_part = _image_block_to_part(block)
                if image_part:
                    image_url = str((image_part.get("image_url") or {}).get("url") or "")
                    if image_url:
                        pending_parts.append({
                            "type": "input_image",
                            "image_url": image_url,
                        })
            elif block_type == "tool_use":
                flush_message()
                items.append({
                    "type": "function_call",
                    "call_id": block.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                    "name": block.get("name") or "tool",
                    "arguments": _json_dumps(block.get("input") or {}),
                })
            elif block_type == "tool_result":
                flush_message()
                tool_text = _stringify_content(block.get("content") if "content" in block else block)
                items.append({
                    "type": "function_call_output",
                    "call_id": block.get("tool_use_id") or block.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                    "output": tool_text or "",
                })
            elif block_type in ("thinking", "redacted_thinking"):
                continue
            else:
                text = _stringify_content(block)
                if text:
                    pending_parts.append({"type": "input_text", "text": text})
        flush_message()

    if not items:
        items.append({
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "ping"}],
        })
    return items


def convert_anthropic_tools_to_responses(body: dict[str, Any]) -> list[dict[str, Any]]:
    tools = []
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        tools.append({
            "type": "function",
            "name": tool.get("name") or "tool",
            "description": tool.get("description") or "",
            "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
        })
    return tools


def convert_responses_tool_choice(tool_choice: Any) -> Any:
    if not tool_choice:
        return None
    if isinstance(tool_choice, str):
        if tool_choice in ("auto", "none", "required"):
            return tool_choice
        return None
    if not isinstance(tool_choice, dict):
        return None
    choice_type = str(tool_choice.get("type") or "").strip()
    if choice_type == "auto":
        return "auto"
    if choice_type == "any":
        return "required"
    if choice_type == "tool":
        name = str(tool_choice.get("name") or "").strip()
        if name:
            return {"type": "function", "name": name}
    return None


def build_openai_responses_payload(body: dict[str, Any], target_model: str) -> dict[str, Any]:
    payload = {
        "model": target_model,
        "input": convert_anthropic_messages_to_responses_input(body),
        "stream": bool(body.get("stream")),
    }
    instructions = _system_to_text(body.get("system"))
    if instructions:
        payload["instructions"] = instructions
    max_tokens = body.get("max_tokens")
    if max_tokens is not None:
        payload["max_output_tokens"] = max_tokens
    if body.get("temperature") is not None:
        payload["temperature"] = body.get("temperature")
    if body.get("top_p") is not None:
        payload["top_p"] = body.get("top_p")
    tools = convert_anthropic_tools_to_responses(body)
    if tools:
        payload["tools"] = tools
        tool_choice = convert_responses_tool_choice(body.get("tool_choice"))
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
    return payload


def _parse_tool_call_arguments(raw_arguments: Any) -> dict[str, Any]:
    if isinstance(raw_arguments, dict):
        return raw_arguments
    if isinstance(raw_arguments, str):
        stripped = raw_arguments.strip()
        if not stripped:
            return {}
        try:
            parsed = json.loads(stripped)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except Exception:
            return {"raw": raw_arguments}
    return {"value": raw_arguments}


def _extract_response_text_fragments(value: Any) -> list[str]:
    parts: list[str] = []
    if value is None:
        return parts
    if isinstance(value, str):
        return [value] if value else parts
    if isinstance(value, list):
        for item in value:
            parts.extend(_extract_response_text_fragments(item))
        return parts
    if not isinstance(value, dict):
        text = _stringify_content(value)
        return [text] if text else parts

    block_type = str(value.get("type") or "").strip()
    if block_type in ("output_text", "text", "input_text"):
        text = str(value.get("text") or value.get("delta") or "")
        return [text] if text else parts
    if block_type == "refusal":
        text = str(value.get("refusal") or value.get("text") or "")
        return [text] if text else parts

    for key in ("content", "part", "item", "delta"):
        child = value.get(key)
        if child is not None:
            parts.extend(_extract_response_text_fragments(child))
    return parts


def normalize_openai_response_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """将 Responses API / chat.completions 响应统一归一为 chat.completions 形态。"""
    if not isinstance(payload, dict):
        return {}
    if payload.get("choices"):
        return payload

    response_id = str(payload.get("id") or f"chatcmpl_{uuid.uuid4().hex[:24]}")
    usage = payload.get("usage") or {}
    prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    finish_reason = "stop"
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    for index, item in enumerate(payload.get("output") or []):
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").strip()
        if item_type == "message":
            text_parts.extend(_extract_response_text_fragments(item.get("content")))
        elif item_type == "function_call":
            tool_calls.append({
                "id": item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": item.get("name") or f"tool_{index}",
                    "arguments": str(item.get("arguments") or item.get("input") or "{}"),
                },
            })

    if not text_parts and payload.get("output_text"):
        text_parts.append(str(payload.get("output_text") or ""))

    incomplete_details = payload.get("incomplete_details") or {}
    if tool_calls:
        finish_reason = "tool_calls"
    elif str(incomplete_details.get("reason") or "").strip() in ("max_output_tokens", "max_tokens", "length"):
        finish_reason = "length"

    return {
        "id": response_id,
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "finish_reason": finish_reason,
            "message": {
                "role": "assistant",
                "content": "".join(text_parts),
                **({"tool_calls": tool_calls} if tool_calls else {}),
            },
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    }


def convert_openai_message_to_anthropic_content(message: dict[str, Any]) -> list[dict[str, Any]]:
    content_blocks: list[dict[str, Any]] = []
    content = message.get("content")
    if isinstance(content, str):
        if content:
            content_blocks.append({"type": "text", "text": content})
    elif isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                text = _stringify_content(part)
                if text:
                    content_blocks.append({"type": "text", "text": text})
                continue
            if part.get("type") == "text":
                text = str(part.get("text") or "")
                if text:
                    content_blocks.append({"type": "text", "text": text})
    for tool_call in message.get("tool_calls") or []:
        function = tool_call.get("function") or {}
        content_blocks.append({
            "type": "tool_use",
            "id": tool_call.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
            "name": function.get("name") or "tool",
            "input": _parse_tool_call_arguments(function.get("arguments")),
        })
    if not content_blocks:
        content_blocks.append({"type": "text", "text": ""})
    return content_blocks


def convert_openai_response_to_anthropic(payload: dict[str, Any], target_model: str) -> dict[str, Any]:
    payload = normalize_openai_response_payload(payload)
    choice = ((payload.get("choices") or [{}])[0] or {})
    message = choice.get("message") or {}
    usage = payload.get("usage") or {}
    finish_reason = choice.get("finish_reason")
    content_blocks = convert_openai_message_to_anthropic_content(message)
    if any(block.get("type") == "tool_use" for block in content_blocks):
        stop_reason = "tool_use"
    elif finish_reason in ("length", "max_tokens"):
        stop_reason = "max_tokens"
    else:
        stop_reason = "end_turn"
    return {
        "id": payload.get("id") or f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": target_model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        },
    }


def convert_openai_stream_events_to_response(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """将 OpenAI SSE 事件归并为一个 chat/completions 响应。"""
    response_id = ""
    prompt_tokens = 0
    completion_tokens = 0
    finish_reason = ""
    text_parts: list[str] = []
    tool_states: dict[str, dict[str, Any]] = {}
    next_tool_order = 0
    saw_standard_chunk = False
    saw_meaningful_chunk = False
    saw_responses_chunk = False
    saw_nonstandard_chunk = False

    def ensure_tool_state(key: str, raw_id: str = "", raw_name: str = "") -> dict[str, Any]:
        nonlocal next_tool_order
        tool_state = tool_states.get(key)
        if tool_state is None:
            tool_state = {
                "order": next_tool_order,
                "id": raw_id or f"call_{uuid.uuid4().hex[:24]}",
                "name": raw_name or "tool",
                "arguments_parts": [],
            }
            tool_states[key] = tool_state
            next_tool_order += 1
        if raw_id:
            tool_state["id"] = raw_id
        if raw_name:
            tool_state["name"] = raw_name
        return tool_state

    def append_tool_call(raw_tool: dict[str, Any]):
        tool_index = raw_tool.get("index")
        tool_key = str(tool_index) if tool_index is not None else str(raw_tool.get("id") or len(tool_states))
        function = raw_tool.get("function") or {}
        tool_state = ensure_tool_state(
            tool_key,
            raw_id=str(raw_tool.get("id") or ""),
            raw_name=str(function.get("name") or ""),
        )
        arguments = function.get("arguments")
        if arguments is not None:
            text = _stringify_content(arguments)
            if text:
                tool_state["arguments_parts"].append(text)

    def append_responses_tool(payload: dict[str, Any]):
        item = payload.get("item") or {}
        tool_key = str(
            payload.get("item_id")
            or item.get("call_id")
            or item.get("id")
            or payload.get("call_id")
            or payload.get("output_index")
            or len(tool_states)
        )
        tool_state = ensure_tool_state(
            tool_key,
            raw_id=str(item.get("call_id") or payload.get("call_id") or item.get("id") or ""),
            raw_name=str(item.get("name") or payload.get("name") or ""),
        )
        arguments = payload.get("delta")
        if arguments is None:
            arguments = payload.get("arguments")
        if arguments is None:
            arguments = item.get("arguments") or item.get("input")
        if arguments is not None:
            text = _stringify_content(arguments)
            if text:
                tool_state["arguments_parts"].append(text)

    for payload in events:
        if not isinstance(payload, dict):
            continue
        if payload.get("error"):
            raise ValueError(_extract_upstream_error_text(payload))
        response_id = str(
            payload.get("id")
            or payload.get("response_id")
            or (payload.get("response") or {}).get("id")
            or response_id
        )
        usage = payload.get("usage") or {}
        if usage:
            prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or prompt_tokens)
            completion_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or completion_tokens)

        response_meta = payload.get("response") or {}
        response_usage = response_meta.get("usage") or {}
        if response_usage:
            prompt_tokens = int(response_usage.get("prompt_tokens") or response_usage.get("input_tokens") or prompt_tokens)
            completion_tokens = int(response_usage.get("completion_tokens") or response_usage.get("output_tokens") or completion_tokens)

        event_type = str(payload.get("type") or payload.get("phase") or "").strip()
        if event_type.startswith("response."):
            saw_responses_chunk = True
            if event_type == "response.output_text.delta":
                delta_text = "".join(_extract_response_text_fragments(payload.get("delta")))
                if not delta_text:
                    delta_text = "".join(_extract_response_text_fragments(payload))
                if delta_text:
                    text_parts.append(delta_text)
                    saw_meaningful_chunk = True
                continue
            if event_type in (
                "response.output_item.added",
                "response.output_item.done",
                "response.function_call_arguments.delta",
                "response.function_call_arguments.done",
            ):
                item = payload.get("item") or {}
                if item.get("type") == "function_call" or event_type.startswith("response.function_call_arguments"):
                    append_responses_tool(payload)
                    saw_meaningful_chunk = True
                    continue
            if event_type in ("response.completed", "response.incomplete", "response.failed"):
                incomplete_details = response_meta.get("incomplete_details") or payload.get("incomplete_details") or {}
                if tool_states:
                    finish_reason = "tool_calls"
                elif str(incomplete_details.get("reason") or "").strip() in ("max_output_tokens", "max_tokens", "length"):
                    finish_reason = "length"
                else:
                    finish_reason = "stop"
                saw_meaningful_chunk = saw_meaningful_chunk or bool(text_parts or tool_states)
                continue

        choices = payload.get("choices") or []
        if not choices:
            if payload.get("content") or payload.get("response") or payload.get("phase") or payload.get("status") or payload.get("type"):
                saw_nonstandard_chunk = True
            continue

        saw_standard_chunk = True
        choice = (choices[0] or {})
        message = choice.get("message") or {}
        delta = choice.get("delta") or {}

        current_finish_reason = str(choice.get("finish_reason") or "").strip()
        if current_finish_reason:
            finish_reason = current_finish_reason
            saw_meaningful_chunk = True

        if message:
            message_text = _stringify_content(message.get("content"))
            if message_text:
                text_parts.append(message_text)
                saw_meaningful_chunk = True
            for raw_tool in message.get("tool_calls") or []:
                append_tool_call(raw_tool)
                saw_meaningful_chunk = True

        if delta:
            delta_text = _stringify_content(delta.get("content"))
            if delta_text:
                text_parts.append(delta_text)
                saw_meaningful_chunk = True
            for raw_tool in delta.get("tool_calls") or []:
                append_tool_call(raw_tool)
                saw_meaningful_chunk = True

    if not saw_standard_chunk and not saw_responses_chunk:
        return None

    tool_calls = []
    for tool_state in sorted(tool_states.values(), key=lambda item: item["order"]):
        tool_calls.append({
            "id": tool_state["id"],
            "type": "function",
            "function": {
                "name": tool_state["name"],
                "arguments": "".join(tool_state["arguments_parts"]),
            },
        })

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text_parts),
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    if not message["content"] and not tool_calls and saw_nonstandard_chunk and not saw_responses_chunk:
        return None
    if not message["content"] and not tool_calls and not saw_meaningful_chunk:
        return None

    if not finish_reason:
        finish_reason = "tool_calls" if tool_calls else "stop"

    return {
        "id": response_id or f"chatcmpl_{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "finish_reason": finish_reason,
            "message": message,
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    }


def _extract_token_from_request(request: web.Request) -> str:
    auth = str(request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    x_api_key = str(request.headers.get("x-api-key") or "").strip()
    if x_api_key:
        return x_api_key
    return ""


def _estimate_input_tokens(body: dict[str, Any]) -> int:
    text = _json_dumps({
        "system": body.get("system"),
        "messages": body.get("messages"),
        "tools": body.get("tools"),
    })
    return max(1, int(math.ceil(len(text) / 4.0)))


def _anthropic_error_payload(message: str, error_type: str = "api_error") -> dict[str, Any]:
    return {
        "type": "error",
        "error": {
            "type": error_type,
            "message": message,
        },
    }


def _extract_upstream_error_text(payload: Any) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("type") or _json_dumps(error))
        if error:
            return str(error)
        message = payload.get("message")
        if message:
            return str(message)
        return _json_dumps(payload)
    if isinstance(payload, str):
        return payload
    return str(payload)


def _extract_openai_response_text(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return str(payload)

    parts: list[str] = []
    normalized = normalize_openai_response_payload(payload)
    choice = ((normalized.get("choices") or [{}])[0] or {})
    message = choice.get("message") or {}
    content = message.get("content")
    if isinstance(content, str) and content:
        parts.append(content)
    elif isinstance(content, list):
        parts.extend(_extract_response_text_fragments(content))
    if payload.get("output_text"):
        parts.append(str(payload.get("output_text") or ""))
    return "\n".join(part for part in parts if part)


def build_nested_cli_upstream_error(base_url: str | None = None) -> str:
    root = normalize_openai_compatible_base_url(
        base_url,
        default_base_url="",
        allow_blank=True,
    )
    try:
        path = (urlparse(root).path or "").lower()
    except Exception:
        path = ""
    path_hint = ""
    if "/codex/" in path:
        path_hint = " 这类情况常见于把 /codex/v1 之类的 CLI 代理地址误当成普通模型 API。"
    return (
        "当前上游看起来是另一个 CLI agent 端点，不是原生 OpenAI 模型接口。"
        "它会注入自己的代理指令，并忽略 Claude Code 下发的 MCP / 工具定义，"
        "所以主会话只会口头回答、不会真正调用工具。"
        f"{path_hint} 请改用供应商提供的原生 OpenAI /v1 根路径。"
    )


def detect_nested_cli_upstream(headers: Any = None, payload: Any = None,
                               base_url: str | None = None) -> str:
    response_text = _extract_openai_response_text(payload).strip()
    if not response_text:
        return ""
    normalized_text = response_text.lower()
    if any(marker in normalized_text for marker in _OPENAI_NESTED_CLI_TEXT_MARKERS):
        return build_nested_cli_upstream_error(base_url)
    return ""


async def _iter_sse_payloads(resp) -> Any:
    buffer = ""
    async for chunk in resp.content.iter_any():
        buffer += chunk.decode("utf-8", errors="ignore")
        normalized = buffer.replace("\r\n", "\n")
        while "\n\n" in normalized:
            raw_event, normalized = normalized.split("\n\n", 1)
            data_lines = []
            for line in raw_event.split("\n"):
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            if data_lines:
                yield "\n".join(data_lines)
        buffer = normalized
    final_event = buffer.replace("\r\n", "\n").strip()
    if final_event:
        data_lines = []
        for line in final_event.split("\n"):
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if data_lines:
            yield "\n".join(data_lines)


def build_openai_request_plans(body: dict[str, Any], target_model: str,
                               target: dict[str, Any]) -> list[dict[str, Any]]:
    """按 provider capabilities 构造上游请求方案，支持 responses 优先 + chat 回退。"""
    capabilities = target.get("capabilities") or infer_openai_provider_capabilities(
        target.get("upstream_base_url"),
        provider_family=target.get("provider_id"),
    )
    plans: list[dict[str, Any]] = []

    if capabilities.get("supports_responses_api") and capabilities.get("preferred_endpoint") == "responses":
        plans.append({
            "api": "responses",
            "url": build_openai_responses_url(target["upstream_base_url"]),
            "payload": build_openai_responses_payload(body, target_model),
            "fallback_payload": None,
        })

    if capabilities.get("supports_chat_completions"):
        chat_payload = build_openai_chat_payload(body, target_model)
        plans.append({
            "api": "chat_completions",
            "url": build_openai_chat_completions_url(target["upstream_base_url"]),
            "payload": chat_payload,
            "fallback_payload": build_openai_token_limit_fallback_payload(chat_payload),
        })

    if capabilities.get("supports_responses_api") and not plans:
        plans.append({
            "api": "responses",
            "url": build_openai_responses_url(target["upstream_base_url"]),
            "payload": build_openai_responses_payload(body, target_model),
            "fallback_payload": None,
        })

    return plans


def should_try_next_openai_request_plan(plan: dict[str, Any], status: int,
                                        has_next_plan: bool = True) -> bool:
    """判断当前失败是否应回退到下一个 OpenAI-compatible request plan。"""
    if not has_next_plan:
        return False
    return plan.get("api") == "responses" and int(status or 0) in _OPENAI_NEXT_PLAN_RETRY_STATUSES


class OpenAICompatBridgeHandler:
    """将 Anthropic Messages API 代理到 OpenAI-compatible 上游。"""

    def _load_target(self, scope: str, model_id: str | None = None) -> dict[str, Any] | None:
        from lib.config_loader import load_config
        from lib.settings_handler import infer_external_model_metadata, resolve_main_session_connection

        config = load_config()
        ext = config.get("external_models", {})

        if scope == "main":
            anthropic_cfg = ext.get("anthropic", {})
            resolved = resolve_main_session_connection(
                anthropic_cfg.get("base_url", ""),
                current_env=anthropic_cfg.get("env", {}),
                stored_provider_id=anthropic_cfg.get("provider_id"),
            )
            if resolved.get("access_mode") != "openai_compatible":
                return None
            capabilities = infer_openai_provider_capabilities(
                resolved.get("base_url", ""),
                provider_family=resolved.get("provider_family"),
            )
            return {
                "scope": "main",
                "upstream_base_url": normalize_openai_compatible_base_url(resolved.get("base_url", "")),
                "routing_models": resolved.get("routing_models", {}),
                "provider_id": capabilities.get("provider_family") or resolved.get("provider_family", "openai"),
                "provider_display": capabilities.get("display_name") or resolved.get("provider_display", "OpenAI 兼容接口"),
                "capabilities": capabilities,
            }

        if scope == "external" and model_id:
            cfg = ext.get(model_id, {})
            if not cfg:
                return None
            meta = infer_external_model_metadata(model_id, cfg)
            if meta.get("access_mode") != "openai_compatible":
                return None
            upstream_base_url = normalize_openai_compatible_base_url(
                cfg.get("base_url") or cfg.get("env", {}).get("ANTHROPIC_BASE_URL") or ""
            )
            capabilities = infer_openai_provider_capabilities(
                upstream_base_url,
                provider_family=meta.get("provider_family"),
            )
            return {
                "scope": "external",
                "model_id": model_id,
                "upstream_base_url": upstream_base_url,
                "model": cfg.get("cli_model") or model_id,
                "provider_id": capabilities.get("provider_family") or meta.get("provider_family") or "openai",
                "provider_display": cfg.get("display") or capabilities.get("display_name") or cfg.get("cli_model") or model_id,
                "capabilities": capabilities,
            }
        return None

    @staticmethod
    def _resolve_upstream_model(requested_model: str, target: dict[str, Any]) -> str:
        model = str(requested_model or "").strip()
        if target.get("scope") == "external":
            if not model:
                return target["model"]
            for aliases in _ALL_CLAUDE_MODEL_ALIASES.values():
                if model in aliases:
                    return target["model"]
            return model

        routing_models = target.get("routing_models", {})
        alias_map: dict[str, str] = {}
        for tier, aliases in _ALL_CLAUDE_MODEL_ALIASES.items():
            routed = routing_models.get(tier) or ""
            if not routed:
                continue
            for alias in aliases:
                alias_map[alias] = routed
        if model and model in alias_map:
            return alias_map[model]
        if model:
            return model
        return routing_models.get("sonnet") or routing_models.get("opus") or routing_models.get("haiku") or "gpt-5.5"

    @staticmethod
    async def _send_sse(stream: web.StreamResponse, event: str, payload: dict[str, Any]):
        data = _json_dumps(payload)
        await stream.write(f"event: {event}\ndata: {data}\n\n".encode("utf-8"))

    async def _stream_from_json_payload(self, request: web.Request, payload: dict[str, Any],
                                        target_model: str) -> web.StreamResponse:
        anthropic_payload = convert_openai_response_to_anthropic(payload, target_model)
        content_blocks = anthropic_payload.get("content") or []
        stream = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        await stream.prepare(request)
        await self._send_sse(stream, "message_start", {
            "type": "message_start",
            "message": {
                "id": anthropic_payload["id"],
                "type": "message",
                "role": "assistant",
                "model": anthropic_payload["model"],
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": anthropic_payload["usage"].get("input_tokens", 0),
                    "output_tokens": 0,
                },
            },
        })
        for index, block in enumerate(content_blocks):
            if block.get("type") == "tool_use":
                await self._send_sse(stream, "content_block_start", {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {
                        "type": "tool_use",
                        "id": block.get("id"),
                        "name": block.get("name"),
                        "input": {},
                    },
                })
                await self._send_sse(stream, "content_block_delta", {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": _json_dumps(block.get("input") or {}),
                    },
                })
            else:
                await self._send_sse(stream, "content_block_start", {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "text", "text": ""},
                })
                await self._send_sse(stream, "content_block_delta", {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "text_delta", "text": block.get("text") or ""},
                })
            await self._send_sse(stream, "content_block_stop", {
                "type": "content_block_stop",
                "index": index,
            })
        await self._send_sse(stream, "message_delta", {
            "type": "message_delta",
            "delta": {
                "stop_reason": anthropic_payload.get("stop_reason"),
                "stop_sequence": None,
            },
            "usage": {
                "output_tokens": anthropic_payload["usage"].get("output_tokens", 0),
            },
        })
        await self._send_sse(stream, "message_stop", {"type": "message_stop"})
        await stream.write_eof()
        return stream

    async def _stream_chat_completion(self, request: web.Request, upstream_resp,
                                      target_model: str) -> web.StreamResponse:
        response_payload = await self._read_sse_response_payload(upstream_resp)
        return await self._stream_from_json_payload(request, response_payload, target_model)

    async def _read_sse_response_payload(self, upstream_resp) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        async for payload_str in _iter_sse_payloads(upstream_resp):
            if payload_str == "[DONE]":
                break
            try:
                payload = json.loads(payload_str)
            except Exception:
                continue
            if isinstance(payload, dict):
                events.append(payload)
        response_payload = convert_openai_stream_events_to_response(events)
        if response_payload is None:
            raise ValueError("unsupported_openai_stream_shape")
        return response_payload

    async def _handle_messages(self, request: web.Request, scope: str,
                               model_id: str | None = None) -> web.StreamResponse | web.Response:
        target = self._load_target(scope, model_id)
        if not target:
            return web.json_response(
                _anthropic_error_payload("当前连接未启用 OpenAI 兼容接口桥接", "invalid_request_error"),
                status=404,
            )
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                _anthropic_error_payload("请求格式错误", "invalid_request_error"),
                status=400,
            )
        api_key = _extract_token_from_request(request)
        if not api_key:
            return web.json_response(
                _anthropic_error_payload("缺少 API Key", "authentication_error"),
                status=401,
            )
        requested_model = body.get("model") or ""
        target_model = self._resolve_upstream_model(requested_model, target)
        request_plans = build_openai_request_plans(body, target_model, target)
        if not request_plans:
            return web.json_response(
                _anthropic_error_payload("当前连接未找到可用的 OpenAI 兼容上游能力", "invalid_request_error"),
                status=502,
            )
        timeout = ClientTimeout(total=None, sock_connect=20, sock_read=None)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        logger.info("OpenAI bridge -> %s (%s)", target.get("provider_display"), target_model)
        try:
            async with ClientSession(timeout=timeout) as session:
                for plan_index, plan in enumerate(request_plans):
                    active_payload = plan["payload"]
                    upstream_url = plan["url"]
                    resp = await session.post(upstream_url, headers=headers, json=active_payload)
                    if resp.status == 400 and plan.get("fallback_payload") is not None:
                        await resp.release()
                        active_payload = plan["fallback_payload"]
                        resp = await session.post(upstream_url, headers=headers, json=active_payload)
                    async with resp:
                        if resp.status not in (200, 201):
                            try:
                                payload = await resp.json(content_type=None)
                            except Exception:
                                payload = await resp.text()
                            message = _extract_upstream_error_text(payload)
                            if should_try_next_openai_request_plan(
                                plan,
                                resp.status,
                                has_next_plan=plan_index + 1 < len(request_plans),
                            ):
                                logger.info("OpenAI bridge fallback responses -> chat/completions: %s", message[:180])
                                continue
                            status = 429 if resp.status == 429 else resp.status
                            return web.json_response(
                                _anthropic_error_payload(message, "invalid_request_error" if status < 500 else "api_error"),
                                status=status,
                            )
                        wants_stream = bool(body.get("stream"))
                        content_type = str(resp.headers.get("Content-Type") or "").lower()
                        is_json_response = "json" in content_type
                        is_sse_response = "text/event-stream" in content_type or content_type.startswith("text/plain")
                        fallback_to_nonstream = False
                        if wants_stream and not is_json_response:
                            try:
                                payload = await self._read_sse_response_payload(resp)
                                nested_cli_error = detect_nested_cli_upstream(
                                    resp.headers,
                                    payload,
                                    base_url=target.get("upstream_base_url"),
                                )
                                if nested_cli_error:
                                    logger.warning("OpenAI bridge rejected nested CLI upstream: %s", nested_cli_error)
                                    return web.json_response(
                                        _anthropic_error_payload(nested_cli_error, "invalid_request_error"),
                                        status=502,
                                    )
                                return await self._stream_from_json_payload(request, payload, target_model)
                            except Exception as exc:
                                logger.warning("OpenAI bridge stream parse failed, retrying non-stream: %s", exc)
                                fallback_to_nonstream = True
                        if not fallback_to_nonstream:
                            if is_json_response:
                                try:
                                    payload = await resp.json(content_type=None)
                                except Exception as exc:
                                    if wants_stream:
                                        logger.warning("OpenAI bridge JSON decode failed, retrying non-stream: %s", exc)
                                        fallback_to_nonstream = True
                                    else:
                                        raise
                            elif is_sse_response:
                                payload = await self._read_sse_response_payload(resp)
                            else:
                                try:
                                    payload = await resp.json(content_type=None)
                                except Exception as exc:
                                    if wants_stream:
                                        logger.warning("OpenAI bridge non-json response decode failed, retrying non-stream: %s", exc)
                                        fallback_to_nonstream = True
                                    else:
                                        raise
                        if not fallback_to_nonstream:
                            nested_cli_error = detect_nested_cli_upstream(
                                resp.headers,
                                payload,
                                base_url=target.get("upstream_base_url"),
                            )
                            if nested_cli_error:
                                logger.warning("OpenAI bridge rejected nested CLI upstream: %s", nested_cli_error)
                                return web.json_response(
                                    _anthropic_error_payload(nested_cli_error, "invalid_request_error"),
                                    status=502,
                                )
                            if wants_stream:
                                return await self._stream_from_json_payload(request, payload, target_model)
                            return web.json_response(convert_openai_response_to_anthropic(payload, target_model))
                    if wants_stream:
                        nonstream_payload = dict(active_payload)
                        nonstream_payload["stream"] = False
                        nonstream_fallback_payload = None
                        if plan["api"] == "chat_completions":
                            nonstream_fallback_payload = build_openai_token_limit_fallback_payload(nonstream_payload)
                        retry_resp = await session.post(upstream_url, headers=headers, json=nonstream_payload)
                        if retry_resp.status == 400 and nonstream_fallback_payload is not None:
                            await retry_resp.release()
                            retry_resp = await session.post(upstream_url, headers=headers, json=nonstream_fallback_payload)
                        async with retry_resp:
                            if retry_resp.status not in (200, 201):
                                try:
                                    retry_payload = await retry_resp.json(content_type=None)
                                except Exception:
                                    retry_payload = await retry_resp.text()
                                message = _extract_upstream_error_text(retry_payload)
                                if should_try_next_openai_request_plan(
                                    plan,
                                    retry_resp.status,
                                    has_next_plan=plan_index + 1 < len(request_plans),
                                ):
                                    logger.info("OpenAI bridge fallback responses -> chat/completions after non-stream retry: %s", message[:180])
                                    continue
                                status = 429 if retry_resp.status == 429 else retry_resp.status
                                return web.json_response(
                                    _anthropic_error_payload(message, "invalid_request_error" if status < 500 else "api_error"),
                                    status=status,
                                )
                            payload = await retry_resp.json(content_type=None)
                            nested_cli_error = detect_nested_cli_upstream(
                                retry_resp.headers,
                                payload,
                                base_url=target.get("upstream_base_url"),
                            )
                            if nested_cli_error:
                                logger.warning("OpenAI bridge rejected nested CLI upstream after retry: %s", nested_cli_error)
                                return web.json_response(
                                    _anthropic_error_payload(nested_cli_error, "invalid_request_error"),
                                    status=502,
                                )
                            return await self._stream_from_json_payload(request, payload, target_model)
        except Exception as exc:
            logger.warning("OpenAI bridge request failed: %s", exc)
            return web.json_response(
                _anthropic_error_payload(str(exc)[:240], "api_error"),
                status=502,
            )

    async def _handle_count_tokens(self, request: web.Request, scope: str,
                                   model_id: str | None = None) -> web.Response:
        target = self._load_target(scope, model_id)
        if not target:
            return web.json_response(
                _anthropic_error_payload("当前连接未启用 OpenAI 兼容接口桥接", "invalid_request_error"),
                status=404,
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        return web.json_response({"input_tokens": _estimate_input_tokens(body)})

    async def handle_main_messages(self, request: web.Request):
        return await self._handle_messages(request, "main")

    async def handle_main_count_tokens(self, request: web.Request):
        return await self._handle_count_tokens(request, "main")

    async def handle_external_messages(self, request: web.Request):
        return await self._handle_messages(request, "external", request.match_info.get("model_id"))

    async def handle_external_count_tokens(self, request: web.Request):
        return await self._handle_count_tokens(request, "external", request.match_info.get("model_id"))
