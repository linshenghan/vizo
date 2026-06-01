#!/usr/bin/env python3
"""Convert markdown to HTML"""

import re
import html
from pathlib import Path

def md_to_html(content):
    """Convert markdown to HTML"""

    # 处理代码块（必须最优先，否则会破坏内容）
    def code_block_replace(match):
        code = html.escape(match.group(1).strip())
        return f'<pre><code>{code}</code></pre>'

    content = re.sub(r'```([\s\S]*?)```', code_block_replace, content)

    # 处理标题
    content = re.sub(r'^## (.*?)$', r'<h2>\1</h2>', content, flags=re.MULTILINE)
    content = re.sub(r'^### (.*?)$', r'<h3>\1</h3>', content, flags=re.MULTILINE)
    content = re.sub(r'^#### (.*?)$', r'<h4>\1</h4>', content, flags=re.MULTILINE)

    # 处理水平线
    content = re.sub(r'^---+$', '<hr>', content, flags=re.MULTILINE)

    # 处理引用块
    content = re.sub(r'^> (.*?)$', r'<blockquote>\1</blockquote>', content, flags=re.MULTILINE)

    # 处理表格
    def convert_table(match):
        text = match.group(0)
        lines = text.strip().split('\n')
        if len(lines) < 3:
            return text

        table_html = '<table>'

        # 头部
        header_line = lines[0]
        header_cells = [cell.strip() for cell in header_line.split('|')[1:-1]]
        table_html += '<thead><tr>'
        for cell in header_cells:
            table_html += f'<th>{html.escape(cell)}</th>'
        table_html += '</tr></thead>'

        # 跳过分隔线，处理数据行
        table_html += '<tbody>'
        for line in lines[2:]:
            if line.strip() and '|' in line:
                cells = [cell.strip() for cell in line.split('|')[1:-1]]
                table_html += '<tr>'
                for cell in cells:
                    table_html += f'<td>{html.escape(cell)}</td>'
                table_html += '</tr>'

        table_html += '</tbody></table>'
        return table_html

    content = re.sub(r'\|.*?\|[\s\S]*?\n\|[-:| ]+\|([\s\S]*?)(?=\n\n|$)', convert_table, content)

    # 处理加粗
    content = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', content)

    # 处理倾斜
    content = re.sub(r'\*(.*?)\*', r'<em>\1</em>', content)

    # 处理内联代码
    content = re.sub(r'`([^`]+)`', r'<code>\1</code>', content)

    # 处理链接
    content = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', content)

    # 处理列表
    lines = content.split('\n')
    new_lines = []
    in_list = False
    in_ol = False

    for line in lines:
        if line.startswith('- '):
            if not in_list:
                new_lines.append('<ul>')
                in_list = True
                in_ol = False
            new_lines.append(f'<li>{line[2:]}</li>')
        elif re.match(r'^\d+\. ', line):
            if not in_ol:
                if in_list:
                    new_lines.append('</ul>')
                new_lines.append('<ol>')
                in_ol = True
                in_list = False
            match = re.match(r'^\d+\. (.*)', line)
            new_lines.append(f'<li>{match.group(1)}</li>')
        else:
            if in_list:
                new_lines.append('</ul>')
                in_list = False
            if in_ol:
                new_lines.append('</ol>')
                in_ol = False
            new_lines.append(line)

    if in_list:
        new_lines.append('</ul>')
    if in_ol:
        new_lines.append('</ol>')

    content = '\n'.join(new_lines)

    # 处理段落（不破坏已有标签）
    content = re.sub(r'^(?!<|<pre|<table|<ul|<ol|$)(.*?)$', r'<p>\1</p>', content, flags=re.MULTILINE)

    return content

# 主程序
markdown_file = Path(__file__).resolve().parent / 'docs' / 'OPUS-V6-使用手册.md'
html_content = md_to_html(markdown_file.read_text(encoding='utf-8'))

html_template = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Opus V6 智能协作系统 — 使用手册 v2.7</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            line-height: 1.8;
            color: #333;
            background: #f5f5f5;
            padding: 20px;
        }

        .container {
            max-width: 900px;
            margin: 0 auto;
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            overflow: hidden;
        }

        header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 40px 30px;
            text-align: center;
        }

        h1 {
            font-size: 2.2em;
            margin-bottom: 10px;
        }

        .subtitle {
            font-size: 0.95em;
            opacity: 0.95;
        }

        main {
            padding: 40px 30px;
        }

        h2 {
            font-size: 1.8em;
            color: #667eea;
            margin: 40px 0 20px 0;
            padding-bottom: 10px;
            border-bottom: 2px solid #f0f0f0;
        }

        h3 {
            font-size: 1.3em;
            color: #555;
            margin: 25px 0 15px 0;
        }

        h4 {
            font-size: 1.1em;
            color: #666;
            margin: 18px 0 10px 0;
        }

        p {
            margin: 12px 0;
        }

        ul, ol {
            margin: 15px 0 15px 30px;
        }

        li {
            margin: 8px 0;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            margin: 15px 0;
            border: 1px solid #ddd;
        }

        th {
            background: #f9f9f9;
            padding: 12px;
            text-align: left;
            font-weight: 600;
            border: 1px solid #ddd;
        }

        td {
            padding: 12px;
            border: 1px solid #ddd;
        }

        tr:nth-child(even) {
            background: #f9f9f9;
        }

        code {
            background: #f4f4f4;
            padding: 2px 6px;
            border-radius: 3px;
            font-family: 'Courier New', monospace;
            color: #c7254e;
        }

        pre {
            background: #f8f8f8;
            padding: 15px;
            border-radius: 6px;
            overflow-x: auto;
            margin: 15px 0;
            border-left: 3px solid #667eea;
        }

        pre code {
            background: none;
            padding: 0;
            color: #333;
        }

        blockquote {
            border-left: 4px solid #667eea;
            padding: 10px 20px;
            background: #f9f9f9;
            margin: 15px 0;
            color: #666;
        }

        hr {
            border: none;
            height: 2px;
            background: #f0f0f0;
            margin: 30px 0;
        }

        a {
            color: #667eea;
            text-decoration: none;
        }

        a:hover {
            text-decoration: underline;
        }

        footer {
            background: #f5f5f5;
            padding: 20px 30px;
            text-align: center;
            font-size: 0.9em;
            color: #999;
            border-top: 1px solid #eee;
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>Opus V6 智能协作系统</h1>
            <div class="subtitle">— 使用手册 v2.7 | 更新日期：2026-02-15</div>
        </header>

        <main>
            {content}
        </main>

        <footer>
            <p>Opus V6 智能协作系统 © 2025-2026 | 基于 Claude Code CLI</p>
            <p>最后更新：2026-02-15</p>
        </footer>
    </div>
</body>
</html>'''

final_html = html_template.replace('{content}', html_content)
Path('/tmp/doc_output.html').write_text(final_html, encoding='utf-8')
print('✅ HTML 转换完成')
