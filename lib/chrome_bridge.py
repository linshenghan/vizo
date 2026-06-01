#!/usr/bin/env python3
"""
Chrome Bridge — 持久化 WebSocket 桥接服务

通过 WebSocket 替代 Native Messaging，解决 Chrome Service Worker 空闲断连问题。
集成到 confirm_server 的 aiohttp app，无需额外进程。

路由:
  GET  /vizo/chrome/ws       — Chrome 扩展 WebSocket 连接
  POST /vizo/chrome/mcp      — MCP JSON-RPC 端点（streamable HTTP）
  GET  /vizo/chrome/health    — 健康检查
  GET  /vizo/chrome/connect   — 一键连接引导页
"""

import json
import time
import uuid
import asyncio
import logging
import secrets
import html
from pathlib import Path
from typing import Optional

from aiohttp import web, WSMsgType
from lib.paths import read_data_path, write_data_path

logger = logging.getLogger("chrome_bridge")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_BRIDGE_STATE_FILE = write_data_path("chrome_bridge_state.json", project_root=_PROJECT_ROOT)


def _is_local_host(host: str) -> bool:
    """判断 host 是否指向本机。"""
    value = (host or "").strip().lower()
    if not value:
        return False
    if value.startswith("[::1]"):
        return True
    host_only = value.split(":", 1)[0]
    return host_only in {"127.0.0.1", "localhost", "::1"}


def _default_bridge_state() -> dict:
    return {
        "chrome_connected": False,
        "browser_info": None,
        "cached_tools": 0,
        "connected_at": 0,
        "uptime_seconds": 0.0,
        "pending_requests": 0,
        "stats": {
            "total_requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "reconnect_count": 0,
        },
        "updated_at": 0,
    }


def read_bridge_state() -> dict:
    """读取 Chrome Bridge 状态快照。"""
    try:
        state_file = read_data_path("chrome_bridge_state.json", project_root=_PROJECT_ROOT)
        raw = json.loads(state_file.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raw = {}
    except Exception:
        raw = {}

    state = _default_bridge_state()
    state.update(raw)
    connected_at = float(state.get("connected_at") or 0)
    if state.get("chrome_connected") and connected_at > 0:
        state["uptime_seconds"] = round(max(0.0, time.time() - connected_at), 1)
    else:
        state["chrome_connected"] = False
        state["uptime_seconds"] = 0.0
        state["connected_at"] = 0
    state["cached_tools"] = int(state.get("cached_tools") or 0)
    state["pending_requests"] = int(state.get("pending_requests") or 0)
    state["stats"] = state.get("stats") if isinstance(state.get("stats"), dict) else _default_bridge_state()["stats"]
    return state


def write_bridge_state(payload: dict):
    """写入 Chrome Bridge 状态快照。"""
    state = _default_bridge_state()
    state.update(payload or {})
    connected_at = float(state.get("connected_at") or 0)
    if state.get("chrome_connected") and connected_at > 0:
        state["uptime_seconds"] = round(max(0.0, time.time() - connected_at), 1)
    else:
        state["chrome_connected"] = False
        state["connected_at"] = 0
        state["uptime_seconds"] = 0.0
    state["cached_tools"] = int(state.get("cached_tools") or 0)
    state["pending_requests"] = int(state.get("pending_requests") or 0)
    state["updated_at"] = time.time()
    try:
        _BRIDGE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _BRIDGE_STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(_BRIDGE_STATE_FILE)
    except Exception as e:
        logger.warning("Failed to persist chrome bridge state: %s", e)

# ==================== Chrome Bridge Handler ====================

class ChromeBridgeHandler:
    """Chrome Bridge 核心处理器，管理 WebSocket 连接和 MCP 请求转发。"""

    def __init__(self, config: dict):
        self.config = config
        self.token = config.get("token", "")
        self.request_timeout = config.get("request_timeout", 120)
        self.ws_heartbeat = config.get("ws_heartbeat_interval", 30)

        # Chrome WebSocket 连接（同一时间只允许一个浏览器）
        self._chrome_ws: Optional[web.WebSocketResponse] = None
        self._chrome_info: dict = {}  # 浏览器信息
        self._connected_at: float = 0

        # 请求-响应匹配
        self._pending_requests: dict[str, asyncio.Future] = {}

        # 缓存的工具列表
        self._cached_tools: list = []

        # MCP session 管理
        self._mcp_sessions: dict[str, dict] = {}

        # 统计
        self._stats = {
            "total_requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "reconnect_count": 0,
        }

        # 首次启动时自动生成 token
        if not self.token:
            self.token = secrets.token_urlsafe(24)
            self._save_token(self.token)
            logger.info("Chrome Bridge: auto-generated token")
        self._persist_state()

    @staticmethod
    def _save_token(token: str):
        """将自动生成的 token 写回 config.json。"""
        config_path = _PROJECT_ROOT / "config.json"
        try:
            raw = config_path.read_text("utf-8")
            cfg = json.loads(raw)
            if "chrome_bridge" not in cfg:
                cfg["chrome_bridge"] = {}
            cfg["chrome_bridge"]["token"] = token
            config_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", "utf-8")
        except Exception as e:
            logger.warning("Failed to save chrome_bridge token: %s", e)

    # -------------------- 认证 --------------------

    def _verify_token(self, request: web.Request) -> bool:
        """验证请求中的 token。"""
        t = request.query.get("token") or request.headers.get("X-Chrome-Bridge-Token", "")
        return secrets.compare_digest(t, self.token)

    def _persist_state(self):
        write_bridge_state({
            "chrome_connected": self._chrome_ws is not None and not self._chrome_ws.closed,
            "browser_info": self._chrome_info if self._chrome_ws is not None and not self._chrome_ws.closed else None,
            "cached_tools": len(self._cached_tools),
            "connected_at": self._connected_at,
            "pending_requests": len(self._pending_requests),
            "stats": self._stats,
        })

    # -------------------- WebSocket (Chrome 扩展) --------------------

    async def handle_chrome_ws(self, request: web.Request) -> web.WebSocketResponse:
        """GET /vizo/chrome/ws — Chrome 扩展 WebSocket 连接入口。"""
        if not self._verify_token(request):
            raise web.HTTPForbidden(text="Invalid token")

        ws = web.WebSocketResponse(heartbeat=self.ws_heartbeat)
        await ws.prepare(request)
        logger.info("Chrome extension connected from %s", request.remote)

        # Takeover: 新连接替代旧连接
        if self._chrome_ws is not None and not self._chrome_ws.closed:
            logger.info("Takeover: closing previous Chrome connection")
            await self._chrome_ws.close(code=4001, message=b"Replaced by new connection")
            self._stats["reconnect_count"] += 1

        self._chrome_ws = ws
        self._connected_at = time.time()
        self._chrome_info = {}
        self._persist_state()

        # 异步请求工具列表（在消息循环中处理响应）
        async def _fetch_tools():
            await asyncio.sleep(0.3)  # 等消息循环启动
            try:
                result = await self._send_to_chrome("list_tools", {}, timeout=30)
                if result and result.get("status") == "success":
                    self._cached_tools = result.get("data", {}).get("tools", [])
                    self._persist_state()
                    logger.info("Cached %d Chrome tools", len(self._cached_tools))
            except Exception as e:
                logger.warning("Failed to fetch tool list on connect: %s", e)

        fetch_task = asyncio.create_task(_fetch_tools())

        # 消息循环
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        await self._handle_chrome_message(data)
                    except json.JSONDecodeError:
                        logger.warning("Invalid JSON from Chrome: %s", msg.data[:200])
                elif msg.type == WSMsgType.ERROR:
                    logger.error("Chrome WS error: %s", ws.exception())
        finally:
            fetch_task.cancel()
            logger.info("Chrome extension disconnected")
            if self._chrome_ws is ws:
                self._chrome_ws = None
                self._chrome_info = {}
                self._connected_at = 0
                self._cached_tools = []
                # 清理所有待处理请求
                for req_id, future in list(self._pending_requests.items()):
                    if not future.done():
                        future.set_exception(ConnectionError("Chrome disconnected"))
                self._pending_requests.clear()
                self._persist_state()

        return ws

    async def _handle_chrome_message(self, data: dict):
        """处理来自 Chrome 扩展的消息。"""
        resp_id = data.get("responseToRequestId")
        msg_type = data.get("type", "")

        if resp_id and resp_id in self._pending_requests:
            # 响应匹配
            future = self._pending_requests.pop(resp_id)
            if not future.done():
                future.set_result(data.get("payload", {}))
            return

        # 主动推送消息处理
        if msg_type == "browser_info":
            self._chrome_info = data.get("payload", {})
            self._persist_state()
            logger.info("Browser info: %s", self._chrome_info.get("browser", "unknown"))
        elif msg_type == "tools_update":
            self._cached_tools = data.get("payload", {}).get("tools", [])
            self._persist_state()
            logger.info("Tools updated: %d tools", len(self._cached_tools))

    # -------------------- 发送到 Chrome --------------------

    async def _send_to_chrome(self, msg_type: str, payload: dict,
                              timeout: Optional[float] = None) -> dict:
        """通过 WebSocket 发送消息到 Chrome，等待响应。"""
        if self._chrome_ws is None or self._chrome_ws.closed:
            raise ConnectionError("Chrome not connected")

        request_id = str(uuid.uuid4())
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_requests[request_id] = future

        message = {
            "type": msg_type,
            "requestId": request_id,
            "payload": payload,
        }

        try:
            await self._chrome_ws.send_json(message)
        except Exception as e:
            self._pending_requests.pop(request_id, None)
            raise ConnectionError(f"Failed to send to Chrome: {e}") from e

        effective_timeout = timeout or self.request_timeout
        try:
            return await asyncio.wait_for(future, timeout=effective_timeout)
        except asyncio.TimeoutError:
            self._pending_requests.pop(request_id, None)
            raise TimeoutError(f"Chrome did not respond within {effective_timeout}s")

    # -------------------- MCP HTTP 端点 --------------------

    async def handle_mcp_request(self, request: web.Request) -> web.Response:
        """POST /vizo/chrome/mcp — MCP JSON-RPC 端点。"""
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}},
                status=400,
            )

        method = body.get("method", "")
        req_id = body.get("id")
        params = body.get("params", {})

        self._stats["total_requests"] += 1
        self._persist_state()

        try:
            if method == "initialize":
                result = await self._mcp_initialize(request, params)
            elif method == "notifications/initialized":
                # Client notification, no response needed
                return web.Response(status=204)
            elif method == "tools/list":
                result = await self._mcp_tools_list()
            elif method == "tools/call":
                result = await self._mcp_tools_call(params)
            else:
                return web.json_response(
                    {"jsonrpc": "2.0", "id": req_id,
                     "error": {"code": -32601, "message": f"Unknown method: {method}"}},
                    status=400,
                )

            self._stats["successful_requests"] += 1
            self._persist_state()
            resp = {"jsonrpc": "2.0", "id": req_id, "result": result}
            response = web.json_response(resp)

            # 添加 session header
            session_id = request.headers.get("Mcp-Session-Id")
            if session_id:
                response.headers["Mcp-Session-Id"] = session_id

            return response

        except ConnectionError as e:
            self._stats["failed_requests"] += 1
            self._persist_state()
            return web.json_response(
                {"jsonrpc": "2.0", "id": req_id,
                 "error": {"code": -32000, "message": f"Chrome not connected: {e}"}},
                status=503,
            )
        except TimeoutError as e:
            self._stats["failed_requests"] += 1
            self._persist_state()
            return web.json_response(
                {"jsonrpc": "2.0", "id": req_id,
                 "error": {"code": -32000, "message": str(e)}},
                status=504,
            )
        except Exception as e:
            self._stats["failed_requests"] += 1
            self._persist_state()
            logger.exception("MCP request failed: %s", e)
            return web.json_response(
                {"jsonrpc": "2.0", "id": req_id,
                 "error": {"code": -32603, "message": f"Internal error: {e}"}},
                status=500,
            )

    async def handle_mcp_delete(self, request: web.Request) -> web.Response:
        """DELETE /vizo/chrome/mcp — MCP session 清理。"""
        session_id = request.headers.get("Mcp-Session-Id")
        if session_id and session_id in self._mcp_sessions:
            del self._mcp_sessions[session_id]
        return web.Response(status=204)

    async def _mcp_initialize(self, request: web.Request, params: dict) -> dict:
        """处理 MCP initialize 请求。"""
        session_id = str(uuid.uuid4())
        self._mcp_sessions[session_id] = {
            "created_at": time.time(),
            "client_info": params.get("clientInfo", {}),
        }

        return {
            "protocolVersion": "2025-03-26",
            "capabilities": {
                "tools": {"listChanged": True},
            },
            "serverInfo": {
                "name": "opus-chrome-bridge",
                "version": "1.0.0",
            },
            "_meta": {"sessionId": session_id},
        }

    async def _mcp_tools_list(self) -> dict:
        """返回缓存的工具列表。"""
        if not self._cached_tools and self._chrome_ws and not self._chrome_ws.closed:
            # 尝试刷新
            try:
                result = await self._send_to_chrome("list_tools", {}, timeout=15)
                if result and result.get("status") == "success":
                    self._cached_tools = result.get("data", {}).get("tools", [])
            except Exception:
                pass

        return {"tools": self._cached_tools}

    async def _mcp_tools_call(self, params: dict) -> dict:
        """转发工具调用到 Chrome。"""
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        result = await self._send_to_chrome("call_tool", {
            "name": tool_name,
            "args": arguments,
        })

        if result.get("status") == "success":
            data = result.get("data", {})
            # 扩展可能返回 content 数组，需检查其中是否有 base64 图片需要转换
            if "content" in data:
                converted = []
                for block in data["content"]:
                    if block.get("type") == "text":
                        try:
                            inner = json.loads(block["text"])
                            b64 = inner.get("base64Data") or inner.get("base64")
                            if b64:
                                mime = inner.get("mimeType", "image/jpeg")
                                converted.append({"type": "image", "data": b64, "mimeType": mime})
                                # 附加非图片字段作为摘要
                                summary = {k: v for k, v in inner.items()
                                           if k not in ("base64Data", "base64", "mimeType") and v is not None}
                                if summary:
                                    converted.append({"type": "text", "text": json.dumps(summary, ensure_ascii=False)})
                                continue
                        except (json.JSONDecodeError, TypeError):
                            pass
                    converted.append(block)
                return {"content": converted}
            # 顶层含 base64Data 的情况（兜底）
            base64_data = data.get("base64Data") or data.get("base64")
            if base64_data:
                mime = data.get("mimeType", "image/jpeg")
                summary = {k: v for k, v in data.items()
                           if k not in ("base64Data", "base64", "mimeType") and v is not None}
                content = [{"type": "image", "data": base64_data, "mimeType": mime}]
                if summary:
                    content.append({"type": "text", "text": json.dumps(summary, ensure_ascii=False)})
                return {"content": content}
                return {"content": content}
            return {
                "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
            }
        else:
            error_msg = result.get("error", result.get("message", "Tool call failed"))
            return {
                "content": [{"type": "text", "text": f"Error: {error_msg}"}],
                "isError": True,
            }

    # -------------------- 健康检查 --------------------

    async def handle_health(self, request: web.Request) -> web.Response:
        """GET /vizo/chrome/health — 状态查询。"""
        connected = self._chrome_ws is not None and not self._chrome_ws.closed
        uptime = time.time() - self._connected_at if connected else 0

        return web.json_response({
            "chrome_connected": connected,
            "browser_info": self._chrome_info if connected else None,
            "pending_requests": len(self._pending_requests),
            "cached_tools": len(self._cached_tools),
            "uptime_seconds": round(uptime, 1),
            "stats": self._stats,
        })

    # -------------------- 一键连接引导页 --------------------

    async def handle_connect_page(self, request: web.Request) -> web.Response:
        """GET /vizo/chrome/connect — 连接引导页。"""
        ws_url = self._get_bridge_url(request)
        current_access_url = self._get_current_access_bridge_url(request)
        connected = self._chrome_ws is not None and not self._chrome_ws.closed
        extra_url_block = ""
        if current_access_url and current_access_url != ws_url:
            extra_url_block = (
                '<div class="hint" style="margin-top:10px;border-left-color:#38bdf8">'
                '<strong>当前访问地址：</strong>'
                '如果当前浏览器访问的是另一台机器上的 Opus，请改用这条地址，而不是本机地址。'
                '<div class="url-wrap" style="margin-top:8px">'
                f'<span class="url-text" id="wsUrlRemote">{html.escape(current_access_url)}</span>'
                '<button class="copy-btn" onclick="copyUrl(\'wsUrlRemote\', this)">复制</button>'
                '</div></div>'
            )

        html = _CONNECT_PAGE_HTML.format(
            ws_url=ws_url,
            extra_url_block=extra_url_block,
            connected="true" if connected else "false",
            browser_info=json.dumps(self._chrome_info, ensure_ascii=False),
            tools_count=len(self._cached_tools),
        )
        return web.Response(text=html, content_type="text/html")

    async def handle_extension_download(self, request: web.Request) -> web.Response:
        """GET /vizo/chrome/extension.zip — 下载预构建的 Chrome 扩展包。"""
        import pathlib
        zip_path = pathlib.Path(__file__).parent.parent / "assets" / "chrome-mcp-extension.zip"
        if not zip_path.exists():
            return web.Response(text="Extension package not found", status=404)
        return web.FileResponse(
            zip_path,
            headers={"Content-Disposition": "attachment; filename=chrome-mcp-extension.zip"},
        )

    def _get_bridge_url(self, request: web.Request) -> str:
        """根据请求来源自动生成最佳连接地址。"""
        from lib.config_loader import load_config
        config = load_config()
        port = config.get("confirm_server", {}).get("port", 9390)

        # Chrome MCP 默认按“浏览器和 Opus 在同一台机器”设计。
        # 因此连接引导优先给出 localhost 地址，避免被自定义域名/代理误导。
        return f"ws://127.0.0.1:{port}/vizo/chrome/ws?token={self.token}"

    def _get_current_access_bridge_url(self, request: web.Request) -> str:
        """返回“当前浏览器访问地址”对应的 WebSocket URL，供远程访问时手动选择。"""
        forwarded_host = (
            request.headers.get("X-Forwarded-Host")
            or request.headers.get("X-Original-Host")
            or request.host
            or ""
        ).strip()
        if not forwarded_host or _is_local_host(forwarded_host):
            return ""

        forwarded_proto = (
            request.headers.get("X-Forwarded-Proto")
            or request.headers.get("X-Original-Proto")
            or request.scheme
            or "http"
        ).strip().lower()
        scheme = "wss" if forwarded_proto == "https" else "ws"
        return f"{scheme}://{forwarded_host}/vizo/chrome/ws?token={self.token}"

    # -------------------- 清理 --------------------

    async def close(self):
        """关闭所有连接。"""
        if self._chrome_ws and not self._chrome_ws.closed:
            await self._chrome_ws.close(code=1001, message=b"Server shutting down")
        for req_id, future in self._pending_requests.items():
            if not future.done():
                future.set_exception(ConnectionError("Server shutting down"))
        self._pending_requests.clear()
        self._mcp_sessions.clear()


# ==================== 一键连接引导页 HTML ====================

_CONNECT_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Opus Chrome Bridge - 连接浏览器</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:16px;font-size:13px;line-height:1.5}}
.container{{max-width:580px;width:100%}}
.card{{background:#1e293b;border-radius:12px;padding:24px 20px;box-shadow:0 4px 24px rgba(0,0,0,.3);margin-bottom:16px}}
.header{{text-align:center;margin-bottom:24px}}
.header h1{{font-size:18px;font-weight:600;color:#f8fafc;margin-bottom:8px}}
.header p{{color:#94a3b8;font-size:13px}}
.status{{display:flex;align-items:center;gap:8px;padding:12px 16px;border-radius:8px;margin-bottom:20px;font-size:13px;transition:all .3s}}
.status.connected{{background:#052e16;border:1px solid #166534}}
.status.disconnected{{background:#1c1917;border:1px solid #44403c}}
.dot{{width:10px;height:10px;border-radius:50%;flex-shrink:0}}
.dot.on{{background:#22c55e;box-shadow:0 0 8px #22c55e80}}
.dot.off{{background:#78716c}}
/* URL box with inline copy button */
.url-wrap{{display:flex;align-items:center;background:#0f172a;border:1px solid #334155;border-radius:8px;transition:border-color .2s}}
.url-wrap:hover{{border-color:#38bdf8}}
.url-text{{flex:1;padding:10px 12px;font-family:monospace;font-size:11.5px;word-break:break-all;color:#38bdf8;line-height:1.4;min-height:40px;user-select:all}}
.copy-btn{{flex-shrink:0;margin:4px 6px;padding:6px 12px;background:#334155;color:#cbd5e1;border:none;border-radius:6px;font-size:11px;font-weight:500;cursor:pointer;white-space:nowrap;transition:background .2s}}
.copy-btn:hover{{background:#475569;color:#f8fafc}}
.copy-btn.copied{{background:#166534;color:#22c55e}}
.steps{{counter-reset:step}}
.step{{display:flex;gap:12px;margin-bottom:24px;align-items:flex-start}}
.step-num{{background:#334155;color:#e2e8f0;width:24px;height:24px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:600;flex-shrink:0;margin-top:1px}}
.step-num.done{{background:#166534;color:#22c55e}}
.step-content{{flex:1}}
.step-content p{{font-size:13px;color:#cbd5e1;line-height:1.6}}
.sub-steps{{font-size:12px;color:#94a3b8;margin-top:8px;line-height:1.7;padding:12px 14px;background:#0f172a;border-radius:8px;border-left:3px solid #334155}}
.sub-steps ol{{margin:0 0 0 16px;padding:0}}
.sub-steps li{{margin-bottom:5px}}
.sub-steps li strong{{color:#cbd5e1}}
.btn{{display:inline-block;padding:10px 16px;border:none;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;text-align:center;transition:all .2s;text-decoration:none;color:#fff}}
.btn:hover{{opacity:.9}}
.btn-download{{background:#3b82f6}}
.hint{{font-size:12px;color:#64748b;margin-top:16px;padding:12px;background:#0f172a;border-radius:8px;border-left:3px solid #f59e0b;line-height:1.5}}
.connected-info{{text-align:center;padding:16px 0}}
.connected-info .check{{font-size:36px;margin-bottom:12px;color:#22c55e}}
.connected-info h2{{font-size:18px;font-weight:600;color:#22c55e;margin-bottom:10px}}
.connected-info .detail{{color:#94a3b8;font-size:13px;margin-bottom:4px}}
.connected-info .explain{{color:#64748b;font-size:12px;margin-top:12px;line-height:1.5}}
.waiting-hint{{text-align:center;margin-top:16px;padding:12px;background:#0c1a33;border:1px solid #1e3a5f;border-radius:8px;font-size:12px;color:#60a5fa}}
.waiting-hint .spinner{{display:inline-block;width:14px;height:14px;border:2px solid #60a5fa40;border-top-color:#60a5fa;border-radius:50%;animation:spin 1s linear infinite;vertical-align:middle;margin-right:6px}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}
</style>
</head>
<body>
<div class="container">
  <div class="card">
    <div class="header">
      <h1>Opus Chrome Bridge</h1>
      <p>连接你的浏览器，让 AI 能看到网页、点击按钮、填写表单</p>
    </div>

    <div id="statusBar" class="status disconnected">
      <span id="statusDot" class="dot off"></span>
      <span id="statusText">检测中...</span>
    </div>

    <!-- ===== 未连接：安装 + 配置指引 ===== -->
    <div id="connectSection">
      <div class="steps">

        <!-- Step 1: 下载并安装扩展 -->
        <div class="step">
          <div class="step-num" id="step1Num">1</div>
          <div class="step-content">
            <p><strong>安装 Chrome 扩展</strong><span style="color:#64748b;font-size:12px;margin-left:6px">（已安装可跳过）</span></p>
            <div class="sub-steps" style="margin-top:8px">
              <ol>
                <li><a href="/vizo/chrome/extension.zip" class="btn btn-download" download style="margin:4px 0 8px">下载扩展安装包</a></li>
                <li>将下载的 <strong>chrome-mcp-extension.zip</strong> 解压到任意文件夹</li>
                <li>在 Chrome 地址栏输入 <strong>chrome://extensions</strong> 并回车</li>
                <li>打开页面右上角的 <strong>「开发者模式」</strong> 开关</li>
                <li>点击左上角 <strong>「加载已解压的扩展程序」</strong></li>
                <li>选择刚才解压出来的文件夹（包含 manifest.json 的那个）</li>
                <li>安装成功后，点击地址栏右侧的拼图图标，将 <strong>Chrome MCP Server</strong> 固定到工具栏</li>
              </ol>
            </div>
          </div>
        </div>

        <!-- Step 2: 复制连接地址 -->
        <div class="step">
          <div class="step-num" id="step2Num">2</div>
          <div class="step-content">
            <p><strong>复制连接地址</strong><span style="color:#64748b;font-size:12px;margin-left:6px">（默认先用本机地址）</span></p>
            <div class="url-wrap" style="margin-top:8px">
              <span class="url-text" id="wsUrl">{ws_url}</span>
              <button class="copy-btn" id="copyBtn" onclick="copyUrl('wsUrl', this)">复制</button>
            </div>
            {extra_url_block}
          </div>
        </div>

        <!-- Step 3: 粘贴到扩展 -->
        <div class="step">
          <div class="step-num" id="step3Num">3</div>
          <div class="step-content">
            <p><strong>粘贴到扩展并连接</strong></p>
            <div class="sub-steps" style="margin-top:8px">
              <ol>
                <li>点击浏览器工具栏的 <strong>Chrome MCP Server</strong> 扩展图标</li>
                <li>找到 <strong>「WebSocket Bridge URL」</strong> 输入框</li>
                <li>将上一步复制的地址粘贴进去</li>
                <li>点击 <strong>「Connect」</strong> 按钮</li>
                <li>看到状态变为 <strong style="color:#22c55e">Connected</strong> 且本页自动切到“浏览器已连接”，才表示真正连接成功</li>
              </ol>
            </div>
          </div>
        </div>
      </div>

      <div id="waitingHint" class="waiting-hint">
        <span class="spinner"></span>等待浏览器连接...完成上述步骤后此页面会自动更新
      </div>

      <div class="hint">
        <strong>提示：</strong>如果插件里显示“已连接，服务未启动”，通常表示地址已经保存，但 WebSocket 还没有真正连通。
        这时请优先检查：1）浏览器和 Opus 是否在同一台机器；2）是否选错了连接地址；3）本页是否仍然停留在“等待浏览器连接”。
      </div>
    </div>

    <!-- ===== 已连接：状态展示 ===== -->
    <div id="connectedSection" style="display:none">
      <div class="connected-info">
        <div class="check">&#10003;</div>
        <h2>浏览器已连接</h2>
        <p class="detail" id="browserDetail"></p>
        <p class="detail">AI 可操控能力：<strong><span id="toolsCount">{tools_count}</span></strong> 项</p>
        <p class="explain">
          包括截图、页面导航、元素点击、表单填写、网络请求监控等。<br>
          AI 代理在执行前端任务时会自动使用这些能力。
        </p>
      </div>
    </div>
  </div>
</div>

<script>
const wsUrl = "{ws_url}";
const initialConnected = {connected};
const browserInfo = {browser_info};

// 初始化
if (initialConnected) {{
  showConnected();
}} else {{
  document.getElementById("statusText").textContent = "未连接";
  pollConnection(300);
}}

// 轮询连接状态（页面加载后持续检测，直到连上）
function pollConnection(maxRetries) {{
  let retries = 0;
  const interval = setInterval(async () => {{
    retries++;
    if (retries > maxRetries) {{
      clearInterval(interval);
      document.getElementById("waitingHint").innerHTML =
        "等待超时。完成扩展配置后请 <a href='' style='color:#38bdf8'>刷新此页面</a>。";
      return;
    }}
    try {{
      const resp = await fetch("/vizo/chrome/health");
      const data = await resp.json();
      if (data.chrome_connected) {{
        clearInterval(interval);
        showConnected(data);
      }}
    }} catch(e) {{}}
  }}, 2000);
}}

function showConnected(data) {{
  document.getElementById("statusBar").className = "status connected";
  document.getElementById("statusDot").className = "dot on";
  document.getElementById("statusText").textContent = "已连接";
  document.getElementById("connectSection").style.display = "none";
  document.getElementById("connectedSection").style.display = "block";
  try {{
    localStorage.setItem("opus_chrome_bridge_status", JSON.stringify({{ connected: true, ts: Date.now() }}));
  }} catch (e) {{}}
  try {{
    if (window.opener && window.opener !== window) {{
      window.opener.postMessage({{ type: "opus-chrome-connected", ts: Date.now() }}, window.location.origin);
    }}
  }} catch (e) {{}}
  if (data && data.browser_info && data.browser_info.browser) {{
    document.getElementById("browserDetail").textContent =
      data.browser_info.browser +
      (data.browser_info.platform ? " · " + data.browser_info.platform : "");
  }}
  if (data && data.cached_tools) {{
    document.getElementById("toolsCount").textContent = data.cached_tools;
  }}
}}

function copyUrl(targetId, btn) {{
  const text = document.getElementById(targetId).textContent;
  navigator.clipboard.writeText(text).then(() => {{
    btn.textContent = "已复制 &#10003;";
    btn.classList.add("copied");
    // 标记步骤 2 已完成
    document.getElementById("step2Num").classList.add("done");
    document.getElementById("step2Num").textContent = "\\u2713";
    setTimeout(() => {{ btn.textContent = "复制"; btn.classList.remove("copied"); }}, 2000);
  }});
}}
</script>
</body>
</html>"""
