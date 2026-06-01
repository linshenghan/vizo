#!/usr/bin/env python3
"""
文档服务器 - 提供测试生成的JSON文档访问
支持 /docs/{task_id}/{document}.json 端点
支持 CORS 跨域访问
"""

import json
import os
import sys
import asyncio
import argparse
import logging
from pathlib import Path
from typing import Optional
from datetime import datetime

from aiohttp import web

_LIB_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _LIB_DIR.parent



class DocServer:
    """轻量级文档服务器"""

    def __init__(self, host="0.0.0.0", port=9381, docs_root=None):
        self.host = host
        self.port = port
        # 文档根目录：优先使用传入的，否则使用 _PROJECT_ROOT/.test-opus
        self.docs_root = Path(docs_root or str(_PROJECT_ROOT / ".test-opus"))
        self.logger = logging.getLogger("doc_server")

    async def handle_doc_json(self, request: web.Request) -> web.Response:
        """GET /docs/{task_id}/{document} - 返回 JSON 文档"""
        task_id = request.match_info.get("task_id", "")
        document = request.match_info.get("document", "")

        # 构建文件路径
        doc_path = self.docs_root / task_id / f"{document}.json"

        self.logger.info(f"Request: /docs/{task_id}/{document}.json -> {doc_path}")

        # 安全检查：防止目录遍历
        try:
            # 规范化路径
            resolved_path = doc_path.resolve()
            resolved_root = self.docs_root.resolve()

            # 确保文件在 docs_root 内
            if not str(resolved_path).startswith(str(resolved_root)):
                return web.json_response(
                    {"error": "Path traversal detected"},
                    status=403
                )
        except Exception as e:
            self.logger.warning(f"Path resolution error: {e}")
            return web.json_response(
                {"error": "Invalid path"},
                status=400
            )

        # 检查文件是否存在
        if not doc_path.exists():
            self.logger.warning(f"Document not found: {doc_path}")
            return web.json_response(
                {"error": "Document not found", "path": str(doc_path)},
                status=404
            )

        # 读取并返回 JSON
        try:
            with open(doc_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # 返回 JSON，支持 CORS
            response = web.json_response(data)
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
            return response

        except json.JSONDecodeError as e:
            self.logger.error(f"JSON decode error in {doc_path}: {e}")
            return web.json_response(
                {"error": "Invalid JSON format", "details": str(e)},
                status=400
            )
        except Exception as e:
            self.logger.error(f"Error reading document: {e}")
            return web.json_response(
                {"error": "Error reading document", "details": str(e)},
                status=500
            )

    async def handle_doc_html(self, request: web.Request) -> web.Response:
        """GET /docs/{task_id}/{document}.html - 返回 HTML 格式的文档"""
        task_id = request.match_info.get("task_id", "")
        document = request.match_info.get("document", "")

        # 先读取 JSON
        doc_path = self.docs_root / task_id / f"{document}.json"

        if not doc_path.exists():
            return web.Response(
                text=f"<h1>文档未找到</h1><p>路径: {doc_path}</p>",
                content_type="text/html",
                status=404
            )

        try:
            with open(doc_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # 转换为 HTML
            html = self._json_to_html(data, document)

            response = web.Response(text=html, content_type="text/html")
            response.headers['Access-Control-Allow-Origin'] = '*'
            return response

        except Exception as e:
            self.logger.error(f"Error converting document to HTML: {e}")
            return web.Response(
                text=f"<h1>错误</h1><p>{str(e)}</p>",
                content_type="text/html",
                status=500
            )

    async def handle_list_docs(self, request: web.Request) -> web.Response:
        """GET /docs/ - 列出所有可用的文档"""
        try:
            docs = {}

            if self.docs_root.exists():
                for task_dir in self.docs_root.iterdir():
                    if task_dir.is_dir():
                        task_id = task_dir.name
                        docs[task_id] = []

                        for doc_file in task_dir.glob("*.json"):
                            doc_name = doc_file.stem
                            docs[task_id].append({
                                "name": doc_name,
                                "url": f"/docs/{task_id}/{doc_name}.json",
                                "html_url": f"/docs/{task_id}/{doc_name}.html"
                            })

            response = web.json_response({
                "docs_root": str(self.docs_root),
                "total_tasks": len(docs),
                "tasks": docs
            })
            response.headers['Access-Control-Allow-Origin'] = '*'
            return response

        except Exception as e:
            self.logger.error(f"Error listing documents: {e}")
            return web.json_response(
                {"error": str(e)},
                status=500
            )

    async def handle_options(self, request: web.Request) -> web.Response:
        """处理 CORS OPTIONS 请求"""
        response = web.Response()
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response

    async def handle_health(self, request: web.Request) -> web.Response:
        """GET /health - 健康检查"""
        return web.json_response({
            "status": "ok",
            "service": "doc_server",
            "docs_root": str(self.docs_root),
            "time": datetime.now().isoformat()
        })

    def _json_to_html(self, data: dict, title: str = "Document") -> str:
        """将 JSON 数据转换为 HTML"""
        html_parts = [
            '<!DOCTYPE html>',
            '<html lang="zh-CN">',
            '<head>',
            '<meta charset="UTF-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1.0">',
            f'<title>{self._escape_html(title)}</title>',
            '<style>',
            '''
body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    line-height: 1.6;
    color: #333;
    background: #f5f5f5;
    margin: 0;
    padding: 20px;
}
.container {
    max-width: 900px;
    margin: 0 auto;
    background: white;
    border-radius: 8px;
    padding: 30px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.1);
}
h1 {
    color: #2c3e50;
    border-bottom: 2px solid #3498db;
    padding-bottom: 10px;
}
h2 {
    color: #34495e;
    margin-top: 30px;
    padding-top: 20px;
    border-top: 1px solid #ecf0f1;
}
h3 {
    color: #7f8c8d;
    margin-top: 20px;
}
pre {
    background: #f8f8f8;
    border: 1px solid #ddd;
    border-radius: 4px;
    padding: 12px;
    overflow-x: auto;
    font-family: "Monaco", "Courier New", monospace;
    font-size: 13px;
}
code {
    background: #f4f4f4;
    padding: 2px 6px;
    border-radius: 3px;
    font-family: "Monaco", "Courier New", monospace;
    font-size: 0.9em;
}
.json-value {
    background: #fafafa;
    padding: 10px;
    border-left: 3px solid #3498db;
    margin: 10px 0;
    border-radius: 3px;
}
.timestamp {
    color: #7f8c8d;
    font-size: 0.9em;
    margin-top: 20px;
    padding-top: 20px;
    border-top: 1px solid #ecf0f1;
}
table {
    width: 100%;
    border-collapse: collapse;
    margin: 15px 0;
}
th, td {
    padding: 10px;
    text-align: left;
    border-bottom: 1px solid #ecf0f1;
}
th {
    background: #f8f9fa;
    font-weight: 600;
}
ul, ol {
    margin: 10px 0;
    padding-left: 20px;
}
li {
    margin: 5px 0;
}
.status-ok {
    color: #27ae60;
    font-weight: 600;
}
.status-error {
    color: #e74c3c;
    font-weight: 600;
}
            ''',
            '</style>',
            '</head>',
            '<body>',
            '<div class="container">'
        ]

        # 标题
        html_parts.append(f'<h1>{self._escape_html(title)}</h1>')

        # 内容
        if isinstance(data, dict):
            self._render_dict(data, html_parts)
        elif isinstance(data, list):
            self._render_list(data, html_parts)
        else:
            html_parts.append(f'<pre>{self._escape_html(str(data))}</pre>')

        html_parts.extend([
            '</div>',
            '</body>',
            '</html>'
        ])

        return '\n'.join(html_parts)

    def _render_dict(self, data: dict, html_parts: list, level: int = 0):
        """递归渲染字典"""
        for key, value in data.items():
            html_parts.append(f'<h{min(3, level + 2)}>{self._escape_html(str(key))}</h{min(3, level + 2)}>')

            if isinstance(value, dict):
                self._render_dict(value, html_parts, level + 1)
            elif isinstance(value, list):
                self._render_list(value, html_parts, level + 1)
            elif isinstance(value, str) and value.startswith('```'):
                # 代码块
                html_parts.append(f'<pre>{self._escape_html(value)}</pre>')
            else:
                html_parts.append(f'<div class="json-value"><code>{self._escape_html(str(value))}</code></div>')

    def _render_list(self, data: list, html_parts: list, level: int = 0):
        """递归渲染列表"""
        html_parts.append('<ul>')
        for item in data:
            if isinstance(item, (dict, list)):
                html_parts.append('<li>')
                if isinstance(item, dict):
                    self._render_dict(item, html_parts, level + 1)
                else:
                    self._render_list(item, html_parts, level + 1)
                html_parts.append('</li>')
            else:
                html_parts.append(f'<li>{self._escape_html(str(item))}</li>')
        html_parts.append('</ul>')

    def _escape_html(self, text: str) -> str:
        """转义 HTML 特殊字符"""
        return (text
                .replace('&', '&amp;')
                .replace('<', '&lt;')
                .replace('>', '&gt;')
                .replace('"', '&quot;')
                .replace("'", '&#39;'))

    async def run(self):
        """启动服务"""
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(name)s] %(message)s",
            datefmt="%H:%M:%S"
        )

        self.logger.info(f"文档服务器启动，根目录: {self.docs_root}")

        app = web.Application()

        # 路由
        app.router.add_get("/docs/", self.handle_list_docs)
        app.router.add_get("/docs/{task_id}/{document}", self.handle_doc_json)
        app.router.add_options("/docs/{task_id}/{document}", self.handle_options)
        app.router.add_get("/docs/{task_id}/{document}.html", self.handle_doc_html)
        app.router.add_get("/health", self.handle_health)

        # 提供静态目录浏览（可选）
        app.router.add_static('/test-opus', str(self.docs_root))

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()

        self.logger.info(f"文档服务已启动 http://{self.host}:{self.port}")
        self.logger.info(f"  JSON 文档: http://{self.host}:{self.port}/docs/{{task_id}}/{{document}}.json")
        self.logger.info(f"  HTML 文档: http://{self.host}:{self.port}/docs/{{task_id}}/{{document}}.html")
        self.logger.info(f"  文档列表: http://{self.host}:{self.port}/docs/")
        self.logger.info(f"  健康检查: http://{self.host}:{self.port}/health")

        # 等待退出
        try:
            await asyncio.Event().wait()
        except KeyboardInterrupt:
            self.logger.info("接收到中断信号，正在停止...")
        finally:
            await runner.cleanup()
            self.logger.info("已停止")


def main():
    parser = argparse.ArgumentParser(description="OPUS V6 文档服务器")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=9381, help="监听端口")
    parser.add_argument("--docs-root", default=str(_PROJECT_ROOT / ".test-opus"),
                        help="文档根目录")

    args = parser.parse_args()

    server = DocServer(
        host=args.host,
        port=args.port,
        docs_root=args.docs_root
    )

    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
