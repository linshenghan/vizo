#!/usr/bin/env python3
"""
Chrome MCP Stdio Bridge — Python 替代 Node.js mcp-server-stdio.js

从 stdin 读取 JSON-RPC → POST 到 Chrome Bridge HTTP 端点 → 响应写回 stdout。
零外部依赖，仅用标准库。

环境变量:
  CHROME_BRIDGE_URL — 覆盖默认的 http://127.0.0.1:9390/vizo/chrome/mcp
"""

import os
import sys
import json
import urllib.request
import urllib.error
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from lib.http_utils import urlopen_safely

BRIDGE_URL = os.environ.get("CHROME_BRIDGE_URL", "http://127.0.0.1:9390/vizo/chrome/mcp")
_session_id = None


def send_request(data: dict) -> dict | None:
    """发送 JSON-RPC 请求到 Chrome Bridge HTTP 端点。"""
    global _session_id

    body = json.dumps(data).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if _session_id:
        headers["Mcp-Session-Id"] = _session_id

    req = urllib.request.Request(BRIDGE_URL, data=body, headers=headers, method="POST")

    try:
        with urlopen_safely(req, timeout=180) as resp:
            # 捕获 session id
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                _session_id = sid

            if resp.status == 204:
                return None

            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(error_body)
        except json.JSONDecodeError:
            return {
                "jsonrpc": "2.0",
                "id": data.get("id"),
                "error": {"code": -32000, "message": f"HTTP {e.code}: {error_body[:200]}"},
            }
    except urllib.error.URLError as e:
        return {
            "jsonrpc": "2.0",
            "id": data.get("id"),
            "error": {"code": -32000, "message": f"Connection failed: {e.reason}"},
        }
    except Exception as e:
        return {
            "jsonrpc": "2.0",
            "id": data.get("id"),
            "error": {"code": -32603, "message": str(e)},
        }


def main():
    """主循环：从 stdin 读取 JSON-RPC，转发到 Bridge，写回 stdout。"""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            sys.stderr.write(f"Invalid JSON: {line[:100]}\n")
            continue

        result = send_request(request)

        if result is not None:
            sys.stdout.write(json.dumps(result) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
