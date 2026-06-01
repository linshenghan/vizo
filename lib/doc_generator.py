#!/usr/bin/env python3
"""
文档生成器 - Markdown 转 HTML 预览
Opus 智能协作系统 v4.0

功能：
1. 将 Markdown 转为美观的 HTML 文档
2. 自动创建预览并返回链接
3. 支持本地转换（0 token）或 ModelScope 转换

用法:
    # 本地转换（推荐，0 token）
    python3 doc_generator.py "文档标题" -f input.md --local

    # 使用 ModelScope 转换（更美观，消耗 token）
    python3 doc_generator.py "文档标题" -f input.md

    # 从 stdin 读取
    cat input.md | python3 doc_generator.py "文档标题" --local
"""

import os
import re
import sys
import json
import asyncio
import argparse
import subprocess
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _LIB_DIR.parent


sys.path.insert(0, str(Path(__file__).resolve().parent))

from model_router import get_router, TaskType


def convert_local_paths_to_preview_urls(content: str) -> str:
    """
    检测文档中的本地文件路径，将其转换为预览 URL

    支持的格式：
    - Markdown 链接: [文本](/path/to/file.md)
    - 纯文本路径: `/path/to/file.md`
    - 代码块中的路径不转换

    Args:
        content: Markdown 内容

    Returns:
        转换后的内容
    """
    # 提取代码块，避免转换代码块中的路径
    code_blocks = []
    def save_code_block(match):
        code_blocks.append(match.group(0))
        return f'__CODE_BLOCK_{len(code_blocks) - 1}__'

    # 保存代码块
    content_safe = re.sub(r'```[\s\S]*?```', save_code_block, content)
    content_safe = re.sub(r'`[^`]+`', save_code_block, content_safe)

    # 查找本地文件路径的 Markdown 链接
    # 格式: [文本](/home/... 或 ~/...)
    local_path_pattern = r'\[([^\]]+)\]\(((?:/home/|~/|/opt/)[^\)]+\.(?:md|txt|py|json|yaml|yml|html|css|js))\)'

    # 收集需要转换的文件
    files_to_convert = {}

    def collect_files(match):
        link_text = match.group(1)
        file_path = match.group(2)
        # 展开 ~
        if file_path.startswith('~'):
            file_path = os.path.expanduser(file_path)

        if os.path.exists(file_path):
            files_to_convert[file_path] = link_text
        return match.group(0)  # 先不替换，只收集

    re.sub(local_path_pattern, collect_files, content_safe)

    # 为每个文件创建预览
    path_to_url = {}
    preview_script = str(_LIB_DIR / 'preview_server.py')

    for file_path, link_text in files_to_convert.items():
        try:
            # 读取文件内容
            with open(file_path, 'r', encoding='utf-8') as f:
                file_content = f.read()

            # 获取文件名作为标题
            file_name = os.path.basename(file_path)

            # 如果是 Markdown 文件，转换为 HTML
            if file_path.endswith('.md'):
                html_content = simple_markdown_to_html(file_content)
                from datetime import datetime
                html = HTML_TEMPLATE.format(
                    title=file_name,
                    content=html_content,
                    timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                )
            else:
                # 非 Markdown 文件，包装成代码块
                from datetime import datetime
                escaped_content = file_content.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                html_content = f'<pre><code>{escaped_content}</code></pre>'
                html = HTML_TEMPLATE.format(
                    title=file_name,
                    content=html_content,
                    timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                )

            # 创建预览
            result = subprocess.run(
                ["python3", preview_script, "create", file_name],
                input=html,
                capture_output=True,
                text=True
            )

            if result.returncode == 0:
                # 解析返回的 JSON 获取 URL
                try:
                    preview_info = json.loads(result.stdout.strip())
                    preview_url = preview_info.get('url', '')
                    if preview_url:
                        path_to_url[file_path] = preview_url
                        print(f"[doc_generator] 关联文档已创建预览: {file_name} -> {preview_url}", file=sys.stderr)
                except json.JSONDecodeError:
                    # 可能是旧格式，直接返回 URL
                    preview_url = result.stdout.strip()
                    if preview_url.startswith('http'):
                        path_to_url[file_path] = preview_url
                        print(f"[doc_generator] 关联文档已创建预览: {file_name} -> {preview_url}", file=sys.stderr)
        except Exception as e:
            print(f"[doc_generator] 创建关联文档预览失败 {file_path}: {e}", file=sys.stderr)

    # 替换内容中的本地路径为预览 URL
    def replace_path(match):
        link_text = match.group(1)
        file_path = match.group(2)
        if file_path.startswith('~'):
            file_path = os.path.expanduser(file_path)

        if file_path in path_to_url:
            return f'[{link_text}]({path_to_url[file_path]})'
        return match.group(0)

    content_safe = re.sub(local_path_pattern, replace_path, content_safe)

    # 恢复代码块
    for i, block in enumerate(code_blocks):
        content_safe = content_safe.replace(f'__CODE_BLOCK_{i}__', block)

    return content_safe


# HTML 文档模板
HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            line-height: 1.6;
            color: #333;
            background: #f5f5f5;
            padding: 20px;
        }}
        .container {{
            max-width: 800px;
            margin: 0 auto;
            background: white;
            padding: 30px;
            border-radius: 10px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
        h1 {{ color: #2c3e50; margin-bottom: 20px; border-bottom: 2px solid #3498db; padding-bottom: 10px; }}
        h2 {{ color: #34495e; margin: 25px 0 15px; }}
        h3 {{ color: #7f8c8d; margin: 20px 0 10px; }}
        p {{ margin: 10px 0; }}
        code {{
            background: #f8f9fa;
            padding: 2px 6px;
            border-radius: 4px;
            font-family: "SFMono-Regular", Consolas, monospace;
            font-size: 0.9em;
        }}
        pre {{
            background: #2d3436;
            color: #dfe6e9;
            padding: 15px;
            border-radius: 8px;
            overflow-x: auto;
            margin: 15px 0;
        }}
        pre code {{ background: transparent; color: inherit; padding: 0; }}
        ul, ol {{ margin: 10px 0 10px 25px; }}
        li {{ margin: 5px 0; }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin: 15px 0;
        }}
        th, td {{
            border: 1px solid #ddd;
            padding: 10px;
            text-align: left;
        }}
        th {{ background: #f8f9fa; }}
        blockquote {{
            border-left: 4px solid #3498db;
            margin: 15px 0;
            padding: 10px 20px;
            background: #f8f9fa;
            font-style: italic;
        }}
        .timestamp {{
            color: #999;
            font-size: 0.85em;
            text-align: right;
            margin-top: 30px;
            padding-top: 15px;
            border-top: 1px solid #eee;
        }}
    </style>
</head>
<body>
    <div class="container">
        {content}
        <div class="timestamp">生成时间: {timestamp}</div>
    </div>
</body>
</html>'''


FORMATTING_PROMPT = '''请将以下内容转换为格式良好的 HTML 文档内容（只需要 body 内的内容，不需要完整 HTML 结构）。

要求：
1. 使用语义化 HTML 标签（h1, h2, h3, p, ul, ol, code, pre, table 等）
2. 代码块用 <pre><code>...</code></pre> 包裹
3. 行内代码用 <code>...</code> 包裹
4. 保持内容完整，不要省略任何信息
5. 表格使用 <table> 标签
6. 直接输出 HTML 内容，不要加任何解释

原始内容：
---
{content}
---

直接输出转换后的 HTML 内容：'''


async def format_with_modelscope(content: str, max_retries: int = 3) -> str:
    """使用 ModelScope 格式化内容为 HTML

    Args:
        content: 要格式化的内容
        max_retries: 最大重试次数
    """
    router = get_router()

    messages = [
        {"role": "user", "content": FORMATTING_PROMPT.format(content=content)}
    ]

    last_error = ""
    for attempt in range(max_retries):
        if attempt > 0:
            print(f"[doc_generator] 重试第 {attempt + 1} 次...", file=sys.stderr)
            await asyncio.sleep(2)  # 等待 2 秒后重试

        response = await router.route_and_call(TaskType.TEXT, messages, max_tokens=16384)

        if response.success:
            await router.close()
            if response.fallback_used:
                print(f"[doc_generator] 使用备用模型: {response.model}", file=sys.stderr)
            else:
                print(f"[doc_generator] 使用模型: {response.model}", file=sys.stderr)
            return response.content.strip()

        last_error = response.error
        print(f"[doc_generator] 调用失败 (尝试 {attempt + 1}/{max_retries}): {response.error}", file=sys.stderr)

    await router.close()

    # 所有重试都失败，使用简单的 Markdown 转 HTML
    print(f"[警告] ModelScope 调用失败，使用本地备用格式化: {last_error}", file=sys.stderr)
    return simple_markdown_to_html(content)


def simple_markdown_to_html(content: str) -> str:
    """本地 Markdown 转 HTML（0 token，推荐用于日报等）"""
    import re

    html = content

    # 代码块（必须先处理，避免内部内容被其他规则影响）
    def replace_code_block(match):
        lang = match.group(1) or ''
        code = match.group(2)
        # 转义 HTML 特殊字符
        code = code.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        return f'<pre><code class="{lang}">{code}</code></pre>'

    html = re.sub(r'```(\w*)\n(.*?)\n```', replace_code_block, html, flags=re.DOTALL)

    # 表格
    def replace_table(match):
        lines = match.group(0).strip().split('\n')
        if len(lines) < 2:
            return match.group(0)

        result = ['<table>']
        for i, line in enumerate(lines):
            if '---' in line and '|' in line:
                continue  # 跳过分隔行
            cells = [c.strip() for c in line.strip('|').split('|')]
            tag = 'th' if i == 0 else 'td'
            row = ''.join(f'<{tag}>{c}</{tag}>' for c in cells)
            result.append(f'<tr>{row}</tr>')
        result.append('</table>')
        return '\n'.join(result)

    html = re.sub(r'(\|.+\|[\n\r]+)+', replace_table, html)

    # 标题
    html = re.sub(r'^#### (.+)$', r'<h4>\1</h4>', html, flags=re.MULTILINE)
    html = re.sub(r'^### (.+)$', r'<h3>\1</h3>', html, flags=re.MULTILINE)
    html = re.sub(r'^## (.+)$', r'<h2>\1</h2>', html, flags=re.MULTILINE)
    html = re.sub(r'^# (.+)$', r'<h1>\1</h1>', html, flags=re.MULTILINE)

    # 行内代码
    html = re.sub(r'`([^`]+)`', r'<code>\1</code>', html)

    # 粗体和斜体
    html = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', html)
    html = re.sub(r'\*(.+?)\*', r'<em>\1</em>', html)

    # 链接
    html = re.sub(r'\[([^\]]+)\]\(([^\)]+)\)', r'<a href="\2" target="_blank">\1</a>', html)

    # 列表处理
    lines = html.split('\n')
    result = []
    in_ul = False
    in_ol = False

    for line in lines:
        stripped = line.strip()

        # 无序列表
        if stripped.startswith('- ') or stripped.startswith('* '):
            if not in_ul:
                if in_ol:
                    result.append('</ol>')
                    in_ol = False
                result.append('<ul>')
                in_ul = True
            result.append(f'<li>{stripped[2:]}</li>')
        # 有序列表
        elif re.match(r'^\d+\. ', stripped):
            if not in_ol:
                if in_ul:
                    result.append('</ul>')
                    in_ul = False
                result.append('<ol>')
                in_ol = True
            ol_pattern = r'^\d+\. '
            li_content = re.sub(ol_pattern, '', stripped)
            result.append(f'<li>{li_content}</li>')
        else:
            if in_ul:
                result.append('</ul>')
                in_ul = False
            if in_ol:
                result.append('</ol>')
                in_ol = False

            # 普通段落（跳过已处理的 HTML 标签和空行）
            if stripped and not stripped.startswith('<'):
                result.append(f'<p>{line}</p>')
            else:
                result.append(line)

    if in_ul:
        result.append('</ul>')
    if in_ol:
        result.append('</ol>')

    return '\n'.join(result)


async def generate_document(title: str, content: str, use_local: bool = True) -> str:
    """生成文档并创建预览

    Args:
        title: 文档标题
        content: 文档内容（Markdown 或纯文本）
        use_local: 使用本地转换（True，0 token）或 ModelScope（False）

    Returns:
        预览 URL
    """
    from datetime import datetime

    # 先处理本地文件路径，转换为预览 URL
    print(f"[doc_generator] 检查并转换本地文件链接...", file=sys.stderr)
    content = convert_local_paths_to_preview_urls(content)

    # 格式化内容
    if use_local:
        print(f"[doc_generator] 使用本地转换（0 token）...", file=sys.stderr)
        formatted_content = simple_markdown_to_html(content)
    else:
        print(f"[doc_generator] 使用 ModelScope 格式化...", file=sys.stderr)
        formatted_content = await format_with_modelscope(content)

    # 生成完整 HTML
    html = HTML_TEMPLATE.format(
        title=title,
        content=formatted_content,
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )

    # 创建预览
    preview_script = str(_LIB_DIR / 'preview_server.py')
    result = subprocess.run(
        ["python3", preview_script, "create", title],
        input=html,
        capture_output=True,
        text=True
    )

    if result.returncode == 0:
        preview_url = result.stdout.strip()
        print(f"[doc_generator] 预览已创建: {preview_url}", file=sys.stderr)
        return preview_url
    else:
        print(f"[doc_generator] 创建预览失败: {result.stderr}", file=sys.stderr)
        return ""


def main():
    parser = argparse.ArgumentParser(description="文档生成器 - Markdown 转 HTML 预览")
    parser.add_argument("title", help="文档标题")
    parser.add_argument("-f", "--file", help="从文件读取内容")
    parser.add_argument("-c", "--content", help="直接传入内容")
    parser.add_argument("--local", action="store_true", default=True,
                        help="使用本地转换（默认，0 token）")
    parser.add_argument("--modelscope", action="store_true",
                        help="使用 ModelScope 转换（更美观，消耗 token）")
    args = parser.parse_args()

    # 获取内容
    if args.content:
        content = args.content
    elif args.file:
        with open(args.file) as f:
            content = f.read()
    elif not sys.stdin.isatty():
        content = sys.stdin.read()
    else:
        print("错误: 请通过 -c、-f 或 stdin 提供内容", file=sys.stderr)
        sys.exit(1)

    # 决定使用哪种转换方式
    use_local = not args.modelscope

    # 生成文档
    preview_url = asyncio.run(generate_document(args.title, content, use_local))

    if preview_url:
        print(preview_url)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
