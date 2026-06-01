#!/usr/bin/env python3
"""
JSON 到 HTML 转换服务
为企业微信消息提供美化的 HTML 文档
支持响应式设计，适配移动端
"""

import json
from pathlib import Path
from datetime import datetime


class JsonToHtmlConverter:
    """JSON 文档转 HTML"""

    HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Roboto", sans-serif;
            line-height: 1.6;
            color: #333;
            background: #f5f5f5;
            padding: 20px;
        }}

        .container {{
            max-width: 800px;
            margin: 0 auto;
            background: white;
            border-radius: 12px;
            padding: 30px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }}

        .header {{
            border-bottom: 3px solid #3b82f6;
            padding-bottom: 20px;
            margin-bottom: 30px;
        }}

        h1 {{
            color: #1f2937;
            font-size: 28px;
            margin-bottom: 10px;
        }}

        .meta {{
            font-size: 12px;
            color: #6b7280;
        }}

        h2 {{
            color: #374151;
            font-size: 20px;
            margin-top: 25px;
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 1px solid #e5e7eb;
        }}

        h3 {{
            color: #4b5563;
            font-size: 16px;
            margin-top: 15px;
            margin-bottom: 10px;
        }}

        p {{
            margin: 10px 0;
            color: #4b5563;
        }}

        ul, ol {{
            margin: 15px 0 15px 20px;
            color: #4b5563;
        }}

        li {{
            margin: 8px 0;
        }}

        .section {{
            background: #f9fafb;
            border-left: 4px solid #3b82f6;
            padding: 15px;
            margin: 20px 0;
            border-radius: 4px;
        }}

        .section h3 {{
            color: #3b82f6;
            margin-top: 0;
        }}

        .key-value {{
            display: grid;
            grid-template-columns: auto 1fr;
            gap: 15px;
            margin: 10px 0;
        }}

        .key {{
            font-weight: 600;
            color: #1f2937;
            min-width: 150px;
        }}

        .value {{
            color: #4b5563;
        }}

        .badge {{
            display: inline-block;
            background: #dbeafe;
            color: #1e40af;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 600;
            margin-right: 8px;
            margin-bottom: 8px;
        }}

        .status-high {{
            background: #dcfce7;
            color: #166534;
        }}

        .status-medium {{
            background: #fef3c7;
            color: #92400e;
        }}

        .status-low {{
            background: #fee2e2;
            color: #991b1b;
        }}

        .footer {{
            margin-top: 40px;
            padding-top: 20px;
            border-top: 1px solid #e5e7eb;
            font-size: 12px;
            color: #6b7280;
            text-align: center;
        }}

        code {{
            background: #f3f4f6;
            padding: 2px 6px;
            border-radius: 3px;
            font-family: "Monaco", "Courier New", monospace;
            font-size: 13px;
        }}

        pre {{
            background: #1f2937;
            color: #f3f4f6;
            padding: 15px;
            border-radius: 6px;
            overflow-x: auto;
            margin: 15px 0;
            font-size: 12px;
        }}

        @media (max-width: 600px) {{
            .container {{
                padding: 15px;
            }}

            h1 {{
                font-size: 22px;
            }}

            h2 {{
                font-size: 16px;
            }}

            .key-value {{
                grid-template-columns: 1fr;
            }}

            .key {{
                font-weight: 600;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>{title}</h1>
            <div class="meta">
                <p>📅 生成时间: {timestamp}</p>
                <p>🔖 任务ID: {task_id}</p>
            </div>
        </div>

        {content}

        <div class="footer">
            <p>这是由 OPUS V6 自动生成的文档</p>
            <p>如有问题，请在企业微信中反馈</p>
        </div>
    </div>
</body>
</html>"""

    @staticmethod
    def convert(data: dict, title: str, task_id: str) -> str:
        """将 JSON 转换为 HTML"""
        content_html = JsonToHtmlConverter._render_dict(data)
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        html = JsonToHtmlConverter.HTML_TEMPLATE.format(
            title=title,
            timestamp=timestamp,
            task_id=task_id,
            content=content_html
        )

        return html

    @staticmethod
    def _render_dict(data: dict, level: int = 0) -> str:
        """递归渲染字典"""
        html_parts = []

        for key, value in data.items():
            # 跳过某些字段
            if key in ['timestamp', 'phase']:
                continue

            # 将 snake_case 转换为易读格式
            display_key = key.replace('_', ' ').title()

            if isinstance(value, dict):
                html_parts.append(f"<h2>{display_key}</h2>")
                html_parts.append(JsonToHtmlConverter._render_dict(value, level + 1))

            elif isinstance(value, list):
                if not value:
                    continue
                html_parts.append(f"<h3>{display_key}</h3>")
                html_parts.append("<ul>")
                for item in value:
                    if isinstance(item, dict):
                        html_parts.append(f"<li>")
                        html_parts.append(f"<div class='section'>")
                        html_parts.append(JsonToHtmlConverter._render_dict(item, level + 2))
                        html_parts.append("</div>")
                        html_parts.append("</li>")
                    else:
                        html_parts.append(f"<li>{JsonToHtmlConverter._escape_html(str(item))}</li>")
                html_parts.append("</ul>")

            elif isinstance(value, str):
                # 检查是否是状态字符串
                if key.endswith('clarity') or key.endswith('feasibility') or key.endswith('risk'):
                    status_class = ''
                    if '高' in value or 'High' in value:
                        status_class = 'status-high'
                    elif '中' in value or 'Medium' in value:
                        status_class = 'status-medium'
                    elif '低' in value or 'Low' in value:
                        status_class = 'status-low'

                    html_parts.append(f"""<div class='key-value'>
                        <div class='key'>{display_key}:</div>
                        <div class='value'><span class='badge {status_class}'>{JsonToHtmlConverter._escape_html(value)}</span></div>
                    </div>""")
                else:
                    html_parts.append(f"""<div class='key-value'>
                        <div class='key'>{display_key}:</div>
                        <div class='value'>{JsonToHtmlConverter._escape_html(value)}</div>
                    </div>""")

            else:
                html_parts.append(f"""<div class='key-value'>
                    <div class='key'>{display_key}:</div>
                    <div class='value'>{JsonToHtmlConverter._escape_html(str(value))}</div>
                </div>""")

        return "\n".join(html_parts)

    @staticmethod
    def _escape_html(text: str) -> str:
        """转义 HTML 特殊字符"""
        return (text
                .replace('&', '&amp;')
                .replace('<', '&lt;')
                .replace('>', '&gt;')
                .replace('"', '&quot;')
                .replace("'", '&#39;'))


def convert_json_to_html(task_id: str, doc_name: str):
    """从文件读取 JSON 并转换为 HTML"""
    json_path = Path(f".test-opus/{task_id}/{doc_name}.json")
    html_path = Path(f".test-opus/{task_id}/{doc_name}.html")

    if not json_path.exists():
        return False

    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # 从数据中提取标题
        phase = data.get('phase', '文档')
        title_map = {
            '需求分析': '需求分析报告',
            'requirement_analysis': '需求分析报告',
            'PRD': '产品需求文档',
            '技术设计': '技术设计文档',
        }
        title = title_map.get(phase, phase)

        # 转换为 HTML
        html_content = JsonToHtmlConverter.convert(data, title, task_id)

        # 保存 HTML
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(html_content)

        return True
    except Exception as e:
        print(f"转换失败: {e}")
        return False


if __name__ == "__main__":
    # 测试
    import sys
    if len(sys.argv) >= 3:
        task_id = sys.argv[1]
        doc_name = sys.argv[2]
        if convert_json_to_html(task_id, doc_name):
            print(f"✅ 已转换: .test-opus/{task_id}/{doc_name}.html")
        else:
            print(f"❌ 转换失败")
