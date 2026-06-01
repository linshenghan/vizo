#!/usr/bin/env python3
"""
Web 确认服务 - 轻量级 aiohttp 服务器
Vizo 智能协作系统

用途：为 WxPusher 微信通知提供 Web 确认页面
用户点击微信中的链接即可确认/取消高风险操作

使用:
    python3 confirm_server.py start       # 后台启动
    python3 confirm_server.py start -f    # 前台启动
    python3 confirm_server.py stop        # 停止
    python3 confirm_server.py status      # 查看状态
"""

import os
import re
import sys
import json
import signal

# 确保项目根目录在 sys.path 中，以支持 from lib.xxx 导入
_PROJECT_ROOT_STR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT_STR not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT_STR)
import socket
import asyncio
import argparse
import logging
import urllib.error
import urllib.request
from datetime import datetime
from typing import Optional
from pathlib import Path

from aiohttp import web
import redis.asyncio as aioredis
from lib.confirm_server_runtime import DEFAULT_CONFIRM_SERVER_PORT, resolve_confirm_server_pid_file
from lib.paths import (
    iter_storage_dirs,
    iter_task_dirs,
    read_data_path,
    task_dir as resolve_task_dir,
)
from lib.task_titles import summarize_task_title
from state_manager import check_task_code_changes

_LIB_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _LIB_DIR.parent


# ==================== 辅助函数 ====================


def _resolve_confirm_server_log_file() -> str:
    """返回 confirm_server 可写的日志文件路径。"""
    candidate_dirs = (
        _PROJECT_ROOT / "logs",
        _PROJECT_ROOT / ".vizo" / "logs",
    )
    for log_dir in candidate_dirs:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            probe = log_dir / ".confirm_server_write_probe"
            probe.write_text("", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return str(log_dir / "confirm_server.log")
        except Exception:
            continue
    return str((_PROJECT_ROOT / "confirm_server.log").resolve())


# ==================== Agent 创建意图识别 ====================

_AGENT_CREATE_VERBS = r"(?:创建|新建|做一个|设计一个|搭建|帮我(?:创建|新建|做|设计|搭建))"
_AGENT_CREATE_OBJECTS = r"(?:[Aa]gent|AGENT|助手|模块|智能体)"
# 检测模式：动词和对象同时出现
_AGENT_CREATE_DETECT = re.compile(
    rf"(?:{_AGENT_CREATE_VERBS}).*?(?:{_AGENT_CREATE_OBJECTS})"
    rf"|(?:{_AGENT_CREATE_OBJECTS}).*?(?:{_AGENT_CREATE_VERBS})",
    re.IGNORECASE
)
# 剥离模式：仅去除动词和对象关键词本身（不吃中间内容）
_AGENT_CREATE_STRIP = re.compile(
    rf"{_AGENT_CREATE_VERBS}|{_AGENT_CREATE_OBJECTS}",
    re.IGNORECASE
)


def _detect_agent_create_intent(content: str) -> dict | None:
    """检测企微消息中的 Agent 创建意图。

    匹配规则：动词组 ∩ 对象组同时出现（防误触）。
    返回 {"intent": "create_agent", "description": "提取的描述"} 或 None。
    """
    content_stripped = content.strip()
    if not _AGENT_CREATE_DETECT.search(content_stripped):
        return None
    # 提取描述：仅去除动词和对象关键词，保留中间的描述内容
    desc = _AGENT_CREATE_STRIP.sub("", content_stripped).strip()
    # 清理残留的标点、助词
    desc = re.sub(r'^[，。、：:\s一个]+|[，。、：:\s的]+$', '', desc)
    return {
        "intent": "create_agent",
        "description": desc if len(desc) >= 2 else ""
    }


def parse_request_id(request_id: str) -> dict:
    """
    解析 request_id，提取项目名和窗口 ID

    格式: opus-{project}-{window_id}
    示例: opus-xiaozhi-a3f2c1

    返回: {"project": "xiaozhi", "window_id": "a3f2c1", "window_short": "a3f2", "display": "[xiaozhi|a3f2]"}
    """
    parts = request_id.split("-")
    if len(parts) >= 3 and parts[0] == "opus":
        project = parts[1]
        window_id = "-".join(parts[2:])  # 支持窗口 ID 中含有 -
        window_short = window_id[:4]
        return {
            "project": project,
            "window_id": window_id,
            "window_short": window_short,
            "display": f"[{project}|{window_short}]"
        }
    # 旧格式或无法解析，返回原始 ID
    return {
        "project": "",
        "window_id": "",
        "window_short": "",
        "display": request_id
    }


# ==================== HTML 模板 ====================

CONFIRM_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Vizo - 操作确认</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:16px;font-size:13px;line-height:1.5}}
.card{{background:#1e293b;border-radius:12px;padding:24px 20px;max-width:420px;width:100%;box-shadow:0 4px 24px rgba(0,0,0,.3)}}
.header{{text-align:center;margin-bottom:24px}}
.icon{{font-size:36px;margin-bottom:12px}}
.title{{font-size:18px;font-weight:600;color:#f8fafc}}
.rid{{font-size:12px;color:#64748b;font-family:monospace;margin-top:4px}}
.details{{background:#0f172a;border-radius:8px;padding:16px;margin:20px 0;white-space:pre-wrap;font-size:13px;line-height:1.6;border-left:3px solid #f59e0b}}
.buttons{{display:flex;gap:12px;margin-top:24px}}
.btn{{flex:1;padding:12px 14px;border:none;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;transition:opacity .2s}}
.btn:active{{opacity:.8}}
.btn-yes{{background:#22c55e;color:#fff}}
.btn-no{{background:#ef4444;color:#fff}}
.time{{text-align:center;font-size:12px;color:#475569;margin-top:16px}}
.context-card{{background:#0f172a;border-radius:8px;padding:12px 16px;margin:16px 0;border-left:3px solid #22c55e}}
.ctx-item{{display:flex;justify-content:space-between;align-items:center;padding:4px 0;font-size:13px}}
.ctx-label{{color:#64748b;min-width:60px}}
.ctx-value{{color:#e2e8f0;text-align:right;flex:1}}
</style>
</head>
<body>
<div class="card">
<div class="header">
<div class="icon">&#x26A0;&#xFE0F;</div>
<div class="title">{title}</div>
<div class="rid">{rid_display}</div>
</div>
{context_html}
<div class="details">{content}</div>
<form method="POST" action="/vizo/confirm/{request_id}">
<div class="buttons">
<button type="submit" name="action" value="confirm" class="btn btn-yes">&#x2705; 确认执行</button>
<button type="submit" name="action" value="reject" class="btn btn-no">&#x274C; 取消</button>
</div>
</form>
<div class="time">请求时间: {created_at}</div>
</div>
</body>
</html>"""

RESULT_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Vizo - {result_title}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:16px;font-size:13px;line-height:1.5}}
.card{{background:#1e293b;border-radius:12px;padding:24px 20px;max-width:480px;width:100%;box-shadow:0 4px 24px rgba(0,0,0,.3);text-align:center}}
.icon{{font-size:40px;margin-bottom:16px}}
.title{{font-size:18px;font-weight:600;color:#f8fafc;margin-bottom:12px}}
.msg{{font-size:13px;color:#94a3b8;margin-bottom:20px}}
.response-box{{background:#0f172a;border-radius:8px;padding:16px;margin:20px 0;text-align:left;border-left:3px solid #22c55e}}
.response-label{{font-size:12px;color:#64748b;margin-bottom:8px}}
.response-content{{font-size:13px;color:#e2e8f0;white-space:pre-wrap;word-break:break-word}}
.hint{{font-size:12px;color:#475569;margin-top:16px}}
</style>
</head>
<body>
<div class="card">
<div class="icon">{icon}</div>
<div class="title">{result_title}</div>
<div class="msg">{message}</div>
{response_box}
<div class="hint">此页面可安全关闭</div>
</div>
</body>
</html>"""

# 简单结果页面（无响应内容显示）
SIMPLE_RESULT_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Vizo - {result_title}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:16px;font-size:13px;line-height:1.5}}
.card{{background:#1e293b;border-radius:12px;padding:24px 20px;max-width:420px;width:100%;box-shadow:0 4px 24px rgba(0,0,0,.3);text-align:center}}
.icon{{font-size:40px;margin-bottom:16px}}
.title{{font-size:18px;font-weight:600;color:#f8fafc;margin-bottom:12px}}
.msg{{font-size:13px;color:#94a3b8;margin-bottom:20px}}
.hint{{font-size:12px;color:#475569;margin-top:16px}}
</style>
</head>
<body>
<div class="card">
<div class="icon">{icon}</div>
<div class="title">{result_title}</div>
<div class="msg">{message}</div>
<div class="hint">此页面可安全关闭</div>
</div>
</body>
</html>"""

CONFIRM_DISCUSSION_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Vizo - 需求确认</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:16px;font-size:13px;line-height:1.5}}
.card{{background:#1e293b;border-radius:12px;padding:24px 20px;max-width:480px;width:100%;box-shadow:0 4px 24px rgba(0,0,0,.3)}}
.header{{text-align:center;margin-bottom:24px}}
.icon{{font-size:36px;margin-bottom:12px}}
.title{{font-size:18px;font-weight:600;color:#f8fafc}}
.rid{{font-size:12px;color:#64748b;font-family:monospace;margin-top:4px}}
.details{{background:#0f172a;border-radius:8px;padding:16px;margin:20px 0;white-space:pre-wrap;font-size:13px;line-height:1.6;border-left:3px solid #3b82f6;max-height:40vh;overflow-y:auto}}
.preview-link{{display:block;text-align:center;margin:16px 0;padding:12px;background:#1e3a5f;border-radius:8px;color:#60a5fa;text-decoration:none;font-size:13px;transition:background .2s}}
.preview-link:hover{{background:#1e4a7f}}
.buttons{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:20px}}
.btn{{padding:12px 14px;border:none;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;transition:opacity .2s;color:#fff;text-align:center}}
.btn:active{{opacity:.8}}
.btn-confirm{{background:#22c55e}}
.btn-cancel{{background:#ef4444}}
.btn-feedback{{background:#f59e0b;color:#1e293b}}
.btn-discuss{{background:#3b82f6}}
.feedback-area{{display:none;margin-top:16px}}
.feedback-area textarea{{width:100%;min-height:80px;padding:12px;border:2px solid #334155;border-radius:8px;background:#0f172a;color:#e2e8f0;font-size:13px;resize:vertical;font-family:inherit}}
.feedback-area textarea:focus{{outline:none;border-color:#f59e0b}}
.feedback-area textarea::placeholder{{color:#64748b}}
.feedback-submit{{width:100%;margin-top:8px;padding:12px;border:none;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;background:#f59e0b;color:#1e293b}}
.time{{text-align:center;font-size:12px;color:#475569;margin-top:16px}}
.context-card{{background:#0f172a;border-radius:8px;padding:12px 16px;margin:16px 0;border-left:3px solid #22c55e}}
.ctx-item{{display:flex;justify-content:space-between;align-items:center;padding:4px 0;font-size:13px}}
.ctx-label{{color:#64748b;min-width:60px}}
.ctx-value{{color:#e2e8f0;text-align:right;flex:1}}
</style>
</head>
<body>
<div class="card">
<div class="header">
<div class="icon">&#x1F4CB;</div>
<div class="title">{title}</div>
<div class="rid">{rid_display}</div>
</div>
{context_html}
<div class="details">{content}</div>
{preview_html}
<form id="mainForm" method="POST" action="/vizo/confirm/{request_id}">
<input type="hidden" name="action" id="actionField" value="">
<input type="hidden" name="feedback" id="feedbackField" value="">
<div class="buttons">
<button type="button" onclick="submitAction('confirm')" class="btn btn-confirm">&#x2705; 确认继续</button>
<button type="button" onclick="submitAction('cancel')" class="btn btn-cancel">&#x274C; 取消任务</button>
<button type="button" onclick="toggleFeedback()" class="btn btn-feedback">&#x270F;&#xFE0F; 补充意见</button>
<button type="button" onclick="submitAction('discussion')" class="btn btn-discuss">&#x1F4AC; 发起讨论</button>
</div>
</form>
<div class="feedback-area" id="feedbackArea">
<textarea id="feedbackText" placeholder="请输入补充意见或修改建议..."></textarea>
<button type="button" onclick="submitFeedback()" class="feedback-submit">&#x1F4E4; 提交意见</button>
</div>
<div class="time">请求时间: {created_at}</div>
</div>
<script>
function submitAction(action) {{
    document.getElementById('actionField').value = action;
    document.getElementById('mainForm').submit();
}}
function toggleFeedback() {{
    var area = document.getElementById('feedbackArea');
    area.style.display = area.style.display === 'none' ? 'block' : 'none';
    if (area.style.display === 'block') {{
        document.getElementById('feedbackText').focus();
    }}
}}
function submitFeedback() {{
    var text = document.getElementById('feedbackText').value.trim();
    if (!text) {{
        alert('请输入补充意见');
        return;
    }}
    document.getElementById('actionField').value = 'feedback';
    document.getElementById('feedbackField').value = text;
    document.getElementById('mainForm').submit();
}}
</script>
</body>
</html>"""

CONFIRM_UNIFIED_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Vizo - 操作确认</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:16px;font-size:13px;line-height:1.5}}
.card{{background:#1e293b;border-radius:12px;padding:24px 20px;max-width:480px;width:100%;box-shadow:0 4px 24px rgba(0,0,0,.3)}}
.header{{text-align:center;margin-bottom:24px}}
.icon{{font-size:36px;margin-bottom:12px}}
.title{{font-size:18px;font-weight:600;color:#f8fafc}}
.rid{{font-size:12px;color:#64748b;font-family:monospace;margin-top:4px}}
.details{{background:#0f172a;border-radius:8px;padding:16px;margin:20px 0;white-space:pre-wrap;font-size:13px;line-height:1.6;border-left:3px solid #3b82f6;max-height:40vh;overflow-y:auto}}
.preview-link{{display:block;text-align:center;margin:16px 0;padding:12px;background:#1e3a5f;border-radius:8px;color:#60a5fa;text-decoration:none;font-size:13px;transition:background .2s}}
.preview-link:hover{{background:#1e4a7f}}
.buttons{{display:grid;grid-template-columns:repeat(auto-fit, minmax(140px, 1fr));gap:12px;margin-top:20px}}
.btn{{padding:12px 14px;border:none;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;transition:opacity .2s;color:#fff;text-align:center}}
.btn:active{{opacity:.8}}
.btn-confirm{{background:#22c55e}}
.btn-cancel{{background:#ef4444}}
.btn-feedback{{background:#f59e0b;color:#1e293b}}
.btn-discuss{{background:#3b82f6}}
.feedback-area{{display:none;margin-top:16px}}
.feedback-area textarea{{width:100%;min-height:80px;padding:12px;border:2px solid #334155;border-radius:8px;background:#0f172a;color:#e2e8f0;font-size:13px;resize:vertical;font-family:inherit}}
.feedback-area textarea:focus{{outline:none;border-color:#f59e0b}}
.feedback-area textarea::placeholder{{color:#64748b}}
.feedback-submit{{width:100%;margin-top:8px;padding:12px;border:none;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;background:#f59e0b;color:#1e293b}}
.time{{text-align:center;font-size:12px;color:#475569;margin-top:16px}}
.context-card{{background:#0f172a;border-radius:8px;padding:12px 16px;margin:16px 0;border-left:3px solid #22c55e}}
.ctx-item{{display:flex;justify-content:space-between;align-items:center;padding:4px 0;font-size:13px}}
.ctx-label{{color:#64748b;min-width:60px}}
.ctx-value{{color:#e2e8f0;text-align:right;flex:1}}
</style>
</head>
<body>
<div class="card">
<div class="header">
<div class="icon">&#x1F4CB;</div>
<div class="title">{title}</div>
<div class="rid">{rid_display}</div>
</div>
{context_html}
<div class="details">{content}</div>
{preview_html}
<form id="mainForm" method="POST" action="/vizo/confirm/{request_id}">
<input type="hidden" name="action" id="actionField" value="">
<input type="hidden" name="feedback" id="feedbackField" value="">
<div class="buttons">
{buttons_html}
</div>
</form>
<div class="feedback-area" id="feedbackArea">
<textarea id="feedbackText" placeholder="请输入补充意见或修改建议..."></textarea>
<button type="button" onclick="submitFeedback()" class="feedback-submit">&#x1F4E4; 提交意见</button>
</div>
<div class="time">请求时间: {created_at}</div>
</div>
<script>
function submitAction(action) {{
    document.getElementById('actionField').value = action;
    document.getElementById('mainForm').submit();
}}
function toggleFeedback() {{
    var area = document.getElementById('feedbackArea');
    area.style.display = area.style.display === 'none' ? 'block' : 'none';
    if (area.style.display === 'block') {{
        document.getElementById('feedbackText').focus();
    }}
}}
function submitFeedback() {{
    var text = document.getElementById('feedbackText').value.trim();
    if (!text) {{ alert('请输入补充意见'); return; }}
    document.getElementById('actionField').value = 'feedback';
    document.getElementById('feedbackField').value = text;
    document.getElementById('mainForm').submit();
}}
function confirmThenAction(action) {{
    if (confirm('确定要执行此操作吗？')) {{
        submitAction(action);
    }}
}}
</script>
</body>
</html>"""

EXPIRED_PAGE = SIMPLE_RESULT_PAGE.format(
    icon="&#x23F0;",
    result_title="请求已过期或已处理",
    message="此确认链接已失效。可能原因：请求已超时、已被处理、或链接无效。"
)

# 输入页面模板（支持单选/多选）
INPUT_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Vizo - 回复</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:16px;font-size:13px;line-height:1.5}}
.card{{background:#1e293b;border-radius:12px;padding:24px 20px;max-width:520px;width:100%;box-shadow:0 4px 24px rgba(0,0,0,.3)}}
.header{{text-align:center;margin-bottom:24px}}
.icon{{font-size:36px;margin-bottom:12px}}
.title{{font-size:18px;font-weight:600;color:#f8fafc}}
.rid{{font-size:12px;color:#64748b;font-family:monospace;margin-top:4px}}
.details{{background:#0f172a;border-radius:8px;padding:16px;margin:20px 0;font-size:13px;line-height:1.6;border-left:3px solid #3b82f6;max-height:60vh;overflow-y:auto}}
.options{{margin:20px 0}}
.mode-hint{{font-size:12px;color:#64748b;margin-bottom:12px;text-align:center}}
/* 单选模式：点击即提交 */
.option-btn{{width:100%;padding:14px 16px;border:2px solid #334155;border-radius:12px;background:#0f172a;color:#e2e8f0;font-size:13px;cursor:pointer;transition:all .2s;margin-bottom:12px;text-align:left;display:block}}
.option-btn:hover{{border-color:#3b82f6;background:#1e3a5f}}
.option-btn:active{{transform:scale(0.98)}}
/* 多选模式：checkbox */
.checkbox-option{{width:100%;padding:14px 16px;border:2px solid #334155;border-radius:12px;background:#0f172a;margin-bottom:12px;cursor:pointer;transition:all .2s;display:flex;align-items:flex-start;gap:12px}}
.checkbox-option:hover{{border-color:#3b82f6;background:#1e3a5f}}
.checkbox-option.selected{{border-color:#22c55e;background:#14532d}}
.checkbox-option input[type="checkbox"]{{width:20px;height:20px;margin-top:2px;accent-color:#22c55e;cursor:pointer}}
.option-content{{flex:1}}
.option-label{{font-weight:600;margin-bottom:4px}}
.option-desc{{font-size:12px;color:#94a3b8}}
.divider{{display:flex;align-items:center;margin:20px 0;color:#475569;font-size:12px}}
.divider::before,.divider::after{{content:'';flex:1;height:1px;background:#334155}}
.divider::before{{margin-right:12px}}
.divider::after{{margin-left:12px}}
.input-area{{margin:16px 0}}
.input-area textarea{{width:100%;min-height:100px;padding:12px;border:2px solid #334155;border-radius:8px;background:#0f172a;color:#e2e8f0;font-size:13px;resize:vertical;font-family:inherit}}
.input-area textarea:focus{{outline:none;border-color:#3b82f6}}
.input-area textarea::placeholder{{color:#64748b}}
.btn{{width:100%;padding:12px 14px;border:none;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;transition:opacity .2s;background:#3b82f6;color:#fff;margin-top:12px}}
.btn:active{{opacity:.8}}
.btn:disabled{{background:#475569;cursor:not-allowed}}
.btn-submit{{background:#22c55e}}
.time{{text-align:center;font-size:12px;color:#475569;margin-top:16px}}
</style>
</head>
<body>
<div class="card">
<div class="header">
<div class="icon">&#x1F4DD;</div>
<div class="title">{title}</div>
<div class="rid">{rid_display}</div>
</div>
<div class="details">{content}</div>
{options_html}
<div class="time">请求时间: {created_at}</div>
</div>
{script}
</body>
</html>"""

# 单选模式：点击即提交
SINGLE_SELECT_OPTION = """<form method="POST" action="/vizo/input/{request_id}" style="margin:0">
<input type="hidden" name="response" value="{option_value}">
<button type="submit" class="option-btn">
<div class="option-label">{option_label}</div>
<div class="option-desc">{option_desc}</div>
</button>
</form>"""

# 多选模式：checkbox + 提交按钮
MULTI_SELECT_FORM_START = """<form method="POST" action="/vizo/input/{request_id}" id="multiSelectForm">
<div class="options">
<div class="mode-hint">可多选，选择后点击提交</div>"""

MULTI_SELECT_OPTION = """<label class="checkbox-option" onclick="this.classList.toggle('selected')">
<input type="checkbox" name="selected" value="{option_value}">
<div class="option-content">
<div class="option-label">{option_label}</div>
<div class="option-desc">{option_desc}</div>
</div>
</label>"""

MULTI_SELECT_FORM_END = """</div>
<div class="divider">或输入其他内容</div>
<div class="input-area">
<textarea name="other_response" placeholder="输入其他内容（可选）..."></textarea>
</div>
<button type="submit" class="btn btn-submit">&#x2705; 提交选择</button>
</form>"""

# 单选模式的其他输入区域
SINGLE_SELECT_OTHER = """<form method="POST" action="/vizo/input/{request_id}">
<div class="divider">或输入其他内容</div>
<div class="input-area">
<textarea name="response" placeholder="请输入你的回复..."></textarea>
</div>
<button type="submit" class="btn">&#x1F4E4; 提交回复</button>
</form>"""

# 多选模式的 JavaScript
MULTI_SELECT_SCRIPT = """<script>
document.getElementById('multiSelectForm').addEventListener('submit', function(e) {
    var checkboxes = document.querySelectorAll('input[name="selected"]:checked');
    var otherText = document.querySelector('textarea[name="other_response"]').value.trim();
    
    if (checkboxes.length === 0 && !otherText) {
        e.preventDefault();
        alert('请至少选择一个选项或输入其他内容');
        return false;
    }
});
</script>"""

# ==================== 任务控制台常量 ====================

STEP_DISPLAY = {
    "requirement_analysis": "需求分析",
    "pm_prd": "产品设计",
    "architect": "架构设计",
    "design_confirmed": "设计确认",
    "qa_test_cases": "编写测试用例",
    "backend_dev": "后端开发",
    "frontend_dev": "前端开发",
    "backend": "后端开发",
    "integration": "联调测试",
    "test_fix": "测试修复",
    "test_round_1": "第1轮测试",
    "test_round_2": "第2轮测试",
    "test_round_3": "第3轮测试",
    "fix_round_1": "第1轮修复",
    "fix_round_2": "第2轮修复",
    "fix_round_3": "第3轮修复",
    "deploy": "部署上线",
    "knowledge": "知识沉淀",
    "fix": "问题修复",
    "refactor": "代码重构",
    "qa_engineer": "测试验证",
    "embedded": "嵌入式调试",
    "project_planning": "项目规划",
    "final_integration_test": "最终集成测试",
}

STEP_TO_DOC = {
    "requirement_analysis": "00-requirement-analysis.md",
    "pm_prd": "01-prd.md",
    "architect": "02-design.md",
    "qa_test_cases": "03-test-cases.md",
    "backend_dev": "04-backend-result.md",
    "frontend_dev": "05-frontend-result.md",
    "integration": "06-integration-report.md",
    "test_fix": "07-test-report-round-*.md",
    "knowledge": "08-knowledge-updates.md",
}

STATUS_ORDER = {"running": 0, "paused": 1, "completed": 2, "failed": 3, "rolled_back": 4, "terminated": 5, "cancelled": 6}

STATUS_CONFIG = {
    "running":     {"color": "#3b82f6", "text_color": "#60a5fa", "icon": "🟢", "label": "运行中"},
    "paused":      {"color": "#f59e0b", "text_color": "#fb923c", "icon": "🟡", "label": "已暂停"},
    "completed":   {"color": "#22c55e", "text_color": "#4ade80", "icon": "✅", "label": "已完成"},
    "rolled_back": {"color": "#ef4444", "text_color": "#f87171", "icon": "🔴", "label": "已回退"},
}

# ==================== 任务控制台页面模板 ====================

TASK_LIST_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Vizo 任务控制台</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh;padding:16px;font-size:13px;line-height:1.5}}
.container{{max-width:520px;margin:0 auto}}
.header{{text-align:center;padding:20px 0 16px}}
.header h1{{font-size:20px;font-weight:600;color:#f8fafc}}
.group-title{{font-size:14px;font-weight:600;color:#94a3b8;padding:12px 0 8px;display:flex;align-items:center;gap:6px}}
.card{{background:#1e293b;border-radius:12px;padding:14px 16px;margin-bottom:10px;text-decoration:none;display:block;transition:background .2s;cursor:pointer}}
.card:hover{{background:#273549}}
.card-name{{font-size:15px;font-weight:600;color:#f8fafc;margin-bottom:6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.card-meta{{font-size:13px;color:#94a3b8;display:flex;align-items:center;gap:6px;flex-wrap:wrap}}
.card-meta .status{{font-weight:600}}
.card-meta .sep{{color:#475569}}
.empty{{text-align:center;padding:40px 0;color:#64748b;font-size:14px}}
</style>
</head>
<body>
<div class="container">
<div class="header"><h1>📋 Vizo 任务控制台</h1></div>
{cards_html}
</div>
</body>
</html>"""

TASK_DETAIL_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{task_name} - Vizo 任务</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh;padding:16px;font-size:13px;line-height:1.5}}
.container{{max-width:520px;margin:0 auto}}
.back{{display:inline-block;color:#60a5fa;text-decoration:none;font-size:13px;padding:8px 0;margin-bottom:8px}}
.back:hover{{text-decoration:underline}}
.title-card{{background:#1e293b;border-radius:12px;padding:16px;margin-bottom:12px}}
.title-card h1{{font-size:18px;font-weight:600;color:#f8fafc;margin-bottom:8px}}
.title-meta{{font-size:13px;color:#94a3b8;display:flex;flex-wrap:wrap;gap:8px;align-items:center}}
.title-meta .status{{font-weight:600}}
.title-meta .sep{{color:#475569}}
.section{{background:#1e293b;border-radius:12px;padding:16px;margin-bottom:12px}}
.section-title{{font-size:14px;font-weight:600;color:#94a3b8;margin-bottom:12px}}
.step-item{{display:flex;align-items:flex-start;gap:10px;padding:8px 0;border-bottom:1px solid #334155}}
.step-item:last-child{{border-bottom:none}}
.step-icon{{font-size:16px;flex-shrink:0;width:24px;text-align:center;padding-top:1px}}
.step-content{{flex:1;min-width:0}}
.step-name{{font-size:14px;font-weight:500;color:#e2e8f0}}
.step-detail{{font-size:12px;color:#94a3b8;margin-top:2px}}
.step-detail a{{color:#60a5fa;text-decoration:none}}
.step-detail a:hover{{text-decoration:underline}}
@keyframes blink{{0%,100%{{opacity:1}}50%{{opacity:.4}}}}
.blink{{animation:blink 1.5s ease-in-out infinite;color:#60a5fa}}
.actions{{margin-bottom:12px}}
.btn{{border:none;border-radius:8px;height:40px;font-size:13px;font-weight:600;cursor:pointer;width:100%;transition:opacity .2s;display:flex;align-items:center;justify-content:center;gap:6px}}
.btn:active{{opacity:.8}}
.btn:disabled{{opacity:.5;cursor:not-allowed}}
.btn-primary{{background:#3b82f6;color:#fff}}
.btn-success{{background:#22c55e;color:#fff}}
.btn-secondary{{background:#334155;color:#e2e8f0}}
.btn-danger{{background:#ef4444;color:#fff}}
.feedback-area{{width:100%;min-height:80px;padding:12px;border:2px solid #334155;border-radius:8px;background:#0f172a;color:#e2e8f0;font-size:13px;resize:vertical;font-family:inherit;margin-bottom:10px}}
.feedback-area:focus{{outline:none;border-color:#3b82f6}}
.feedback-area::placeholder{{color:#64748b}}
.grid-2{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:8px}}
</style>
</head>
<body>
<div class="container">
<a href="/vizo/tasks" class="back">← 返回列表</a>
<div class="title-card">
<h1>{task_name}</h1>
<div class="title-meta">
<span class="status" style="color:{status_color}">{status_icon} {status_label}</span>
<span class="sep">·</span><span>{duration_str}</span>
<span class="sep">·</span><span id="total-cost">{cost_str}</span>
</div>
</div>
<div class="section">
<div class="section-title">执行步骤</div>
<div id="steps-container">{steps_html}</div>
</div>
{actions_html}
</div>
<script>
var STEP_DISPLAY = {step_display_json};
var taskId = '{task_id}';
var taskStatus = '{task_status}';
var hasCodeChanges = {has_code_changes} === 'true';

function setLoading(btnId, text) {{
    var btn = document.getElementById(btnId);
    if (btn) {{ btn.disabled = true; btn.textContent = text; }}
}}

function apiCall(action, body, btnId, loadingText) {{
    setLoading(btnId, loadingText);
    fetch('/vizo/api/tasks/' + taskId + '/' + action, {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: body ? JSON.stringify(body) : undefined
    }}).then(function(r) {{ return r.json(); }})
    .then(function(data) {{
        if (data.status === 'ok') {{
            // 显示成功提示，延迟刷新等待后端处理
            var btn = document.getElementById(btnId);
            if (btn) {{ btn.textContent = '✓ ' + (data.message || '操作成功'); btn.style.opacity = '0.7'; }}
            setTimeout(function() {{ location.reload(); }}, 3000);
        }} else {{
            alert(data.message || '操作失败');
            location.reload();
        }}
    }}).catch(function() {{
        alert('网络错误，请重试');
        location.reload();
    }});
}}

function doPause() {{
    apiCall('pause', null, 'pauseBtn', '暂停中...');
}}

function doResume() {{
    var feedback = document.getElementById('feedback');
    var body = feedback && feedback.value.trim() ? {{feedback: feedback.value.trim()}} : null;
    apiCall('resume', body, 'resumeBtn', '恢复中...');
}}

function doTerminate() {{
    if (!hasCodeChanges) {{
        // 无代码变更，显示简单确认
        if (!confirm('确认终止此任务？')) return;
        apiCall('terminate', null, 'termBtn', '终止中...');
    }} else {{
        // 有代码变更，显示选择菜单
        showTerminateChoiceModal();
    }}
}}

function showTerminateChoiceModal() {{
    var overlay = document.createElement('div');
    overlay.id = 'terminateChoiceOverlay';
    overlay.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.6);z-index:9999;display:flex;align-items:center;justify-content:center;';
    overlay.innerHTML = '<div style="background:var(--bg-secondary,#1e1e2e);border:1px solid var(--border,#333);border-radius:12px;padding:20px;max-width:420px;width:90%;text-align:center;">' +
        '<div style="font-size:16px;font-weight:600;margin-bottom:8px;">终止任务</div>' +
        '<div style="color:var(--text-secondary,#888);font-size:13px;margin-bottom:20px;">此任务有代码变更，请选择处理方式：</div>' +
        '<div style="display:flex;flex-direction:column;gap:10px;">' +
        '<button onclick="closeTerminateChoice();doTerminateWithRollback()" style="padding:10px 16px;border-radius:8px;border:none;background:var(--accent,#7c3aed);color:#fff;font-size:13px;cursor:pointer;font-weight:500;">🔄 回滚代码（推荐）</button>' +
        '<button onclick="closeTerminateChoice();doTerminateKeepCode()" style="padding:10px 16px;border-radius:8px;border:1px solid var(--border,#333);background:transparent;color:var(--text-primary,#ccc);font-size:13px;cursor:pointer;font-weight:500;">📌 保留代码</button>' +
        '<button onclick="closeTerminateChoice()" style="padding:8px 16px;border-radius:8px;border:none;background:transparent;color:var(--text-secondary,#888);font-size:12px;cursor:pointer;">取消</button>' +
        '</div></div>';
    overlay.addEventListener('click', function(e) {{ if (e.target === overlay) closeTerminateChoice(); }});
    document.body.appendChild(overlay);
}}

function closeTerminateChoice() {{
    var el = document.getElementById('terminateChoiceOverlay');
    if (el) el.remove();
}}

function doTerminateWithRollback() {{
    apiCall('terminate', {{rollback: true}}, 'termBtn', '终止中...');
}}

function doTerminateKeepCode() {{
    apiCall('terminate', {{rollback: false}}, 'termBtn', '终止中...');
}}

if (taskStatus === 'running' || taskStatus === 'paused') {{
    setInterval(function() {{
        fetch('/vizo/api/tasks/' + taskId + '/progress')
        .then(function(r) {{ if (!r.ok) return null; return r.json(); }})
        .then(function(data) {{
            if (!data) return;
            if (data.status && data.status !== taskStatus) {{
                location.reload();
                return;
            }}
            if (data.steps) {{
                var html = '';
                data.steps.forEach(function(step) {{
                    var nameCn = STEP_DISPLAY[step.name] || step.name;
                    var icon = '○', detail = '';
                    if (step.status === 'completed') {{
                        icon = '✅';
                        var parts = [];
                        if (step.preview_url) {{
                            parts.push('<a href="' + step.preview_url + '" style="color:#60a5fa;">' + (step.output_doc || '') + '</a>');
                        }} else if (step.output_doc) {{
                            parts.push('<span style="color:#64748b;">' + step.output_doc + '</span>');
                        }}
                        if (step.cost_usd) parts.push('$' + step.cost_usd.toFixed(2));
                        if (step.duration) {{
                            var m = Math.floor(step.duration / 60);
                            var s = step.duration % 60;
                            parts.push(m > 0 ? m + '分' + s + '秒' : s + '秒');
                        }}
                        detail = parts.join(' · ');
                    }} else if (step.status === 'running') {{
                        icon = '🔄'; detail = '<span class="blink">进行中...</span>';
                    }} else if (step.status === 'paused') {{
                        icon = '🟡'; detail = '<span style="color:#fb923c;">已暂停</span>';
                    }} else if (step.status === 'error') {{
                        icon = '❌'; detail = '<span style="color:#f87171;">执行失败</span>';
                    }}
                    html += '<div class="step-item"><span class="step-icon">' + icon + '</span><div class="step-content"><div class="step-name">' + nameCn + '</div><div class="step-detail">' + detail + '</div></div></div>';
                }});
                document.getElementById('steps-container').innerHTML = html;
            }}
            if (data.cost_usd !== undefined) {{
                document.getElementById('total-cost').textContent = '$' + data.cost_usd.toFixed(2);
            }}
        }}).catch(function() {{}});
    }}, 5000);
}}

window.addEventListener('DOMContentLoaded', function() {{
    var params = new URLSearchParams(window.location.search);
    if (params.get('action') === 'terminate') {{
        doTerminate();
    }}
}});
</script>
</body>
</html>"""

TASK_ROLLBACK_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>回退操作 - {task_name}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh;padding:16px}}
.container{{max-width:520px;margin:0 auto}}
.back{{display:inline-block;color:#60a5fa;text-decoration:none;font-size:13px;padding:8px 0;margin-bottom:8px}}
.back:hover{{text-decoration:underline}}
.section{{background:#1e293b;border-radius:12px;padding:16px;margin-bottom:12px}}
.section h2{{font-size:16px;font-weight:600;color:#f8fafc;margin-bottom:12px}}
.notice{{text-align:center;padding:24px 12px;color:#94a3b8;font-size:13px}}
.notice .btn{{margin-top:16px;display:inline-block;width:auto;padding:0 24px}}
.radio-group{{display:flex;flex-direction:column;gap:8px}}
.radio-item{{display:flex;align-items:center;gap:10px;padding:10px 12px;background:#0f172a;border-radius:8px;cursor:pointer;transition:background .2s}}
.radio-item:hover{{background:#172033}}
.radio-item input[type="radio"]{{accent-color:#3b82f6;width:18px;height:18px;flex-shrink:0}}
.radio-label{{font-size:13px;font-weight:500;color:#e2e8f0}}
.radio-doc{{font-size:12px;color:#64748b;margin-left:auto}}
.warning-text{{font-size:12px;color:#f59e0b;margin:12px 0;padding:8px 12px;background:rgba(245,158,11,.1);border-radius:6px}}
.feedback-area{{width:100%;min-height:80px;padding:12px;border:2px solid #334155;border-radius:8px;background:#0f172a;color:#e2e8f0;font-size:13px;resize:vertical;font-family:inherit;margin-bottom:12px}}
.feedback-area:focus{{outline:none;border-color:#3b82f6}}
.feedback-area::placeholder{{color:#64748b}}
.btn{{border:none;border-radius:8px;height:40px;font-size:13px;font-weight:600;cursor:pointer;width:100%;transition:opacity .2s;display:flex;align-items:center;justify-content:center;gap:6px}}
.btn:active{{opacity:.8}}
.btn:disabled{{opacity:.5;cursor:not-allowed}}
.btn-primary{{background:#3b82f6;color:#fff}}
.btn-secondary{{background:#334155;color:#e2e8f0}}
.grid-2{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
</style>
</head>
<body>
<div class="container">
<a href="/vizo/tasks/{task_id}" class="back">← 返回详情</a>
{content_html}
</div>
<script>
var STEP_DISPLAY = {step_display_json};
var taskId = '{task_id}';

document.querySelectorAll('input[name="target_step"]').forEach(function(radio) {{
    radio.addEventListener('change', function() {{
        document.getElementById('rollbackBtn').disabled = false;
    }});
}});

function doRollback() {{
    var target = document.querySelector('input[name="target_step"]:checked');
    if (!target) return;
    var stepName = STEP_DISPLAY[target.value] || target.value;
    if (!confirm('确认回退到「' + stepName + '」？该步骤及后续步骤将重新执行。')) return;

    var btn = document.getElementById('rollbackBtn');
    btn.disabled = true;
    btn.textContent = '回退中...';

    fetch('/vizo/api/tasks/' + taskId + '/rollback', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{
            target_step: target.value,
            feedback: document.getElementById('feedback').value.trim()
        }})
    }}).then(function(r) {{ return r.json(); }})
    .then(function(data) {{
        if (data.status === 'ok') {{
            location.href = '/vizo/tasks/' + taskId;
        }} else {{
            alert(data.message || '回退失败');
            btn.disabled = false;
            btn.textContent = '↩️ 确认回退';
        }}
    }}).catch(function() {{
        alert('网络错误，请重试');
        btn.disabled = false;
        btn.textContent = '↩️ 确认回退';
    }});
}}
</script>
</body>
</html>"""

# ==================== 确认服务 ====================

class ConfirmServer:
    """轻量级 Web 确认服务"""

    def __init__(self, host="0.0.0.0", port=DEFAULT_CONFIRM_SERVER_PORT,
                 redis_host="127.0.0.1", redis_port=6380):
        self.host = host
        self.port = port
        self.redis_host = redis_host
        self.redis_port = redis_port
        self._redis: Optional[aioredis.Redis] = None
        self._tunnel_proc = None
        self._tunnel_url: Optional[str] = None
        self.logger = logging.getLogger("confirm_server")

        # Password Manager (before Web Console init)
        self._password_mgr = None
        try:
            from lib.settings_handler import PasswordManager
            PasswordManager.check_reset_on_startup(_PROJECT_ROOT)
            self._password_mgr = PasswordManager(_PROJECT_ROOT)
        except Exception as e:
            self.logger.warning("PasswordManager init failed: %s", e)

        # Web Console (conditional initialization)
        self._pty_manager = None
        self._web_console = None
        self._pubsub_task = None
        self._orphan_scanner_task = None
        wc_config = self._load_web_console_config()
        if wc_config and wc_config.get("enabled"):
            from lib.pty_manager import PTYManager
            from lib.web_console import WebConsoleHandler
            self._pty_manager = PTYManager(wc_config, self._get_redis)
            self._web_console = WebConsoleHandler(
                self._pty_manager, wc_config, self._get_redis,
                password_manager=self._password_mgr,
            )
            self.logger.info("Web Console enabled (max_sessions=%d)",
                             wc_config.get("max_sessions", 3))

        # Chrome Bridge (conditional initialization)
        self._chrome_bridge = None
        cb_config = self._load_chrome_bridge_config()
        if cb_config and cb_config.get("enabled", True):
            from lib.chrome_bridge import ChromeBridgeHandler
            self._chrome_bridge = ChromeBridgeHandler(cb_config)
            self.logger.info("Chrome Bridge enabled (token=%s...)",
                             self._chrome_bridge.token[:8] if self._chrome_bridge.token else "none")

        # OpenAI bridge removed: webconsole no longer exposes OpenAI->Anthropic bridge.
        self._openai_bridge = None

    @staticmethod
    def _load_chrome_bridge_config() -> Optional[dict]:
        """Load chrome_bridge config via load_config()."""
        try:
            from lib.config_loader import load_config
            config = load_config()
            return config.get("chrome_bridge")
        except Exception:
            return None

    @staticmethod
    def _load_web_console_config() -> Optional[dict]:
        """Load web_console config via load_config() (supports env var overrides)."""
        try:
            from lib.config_loader import load_config
            config = load_config()
            return config.get("web_console")
        except Exception:
            return None

    def _check_api_auth(self, request) -> bool:
        """验证控制信号 API 的认证（复用 Web Console 的认证机制）。
        防止子代理通过 curl 调用控制 API 导致自身任务被意外终止。
        """
        if self._web_console:
            return self._web_console._check_auth(request)
        # 无 Web Console 时，检查 password manager
        if self._password_mgr and self._password_mgr.has_password():
            cookie_val = request.cookies.get("vizo_web_token", "")
            return cookie_val == self._password_mgr.get_cookie_value()
        # 无密码保护时放行（开发/测试环境）
        return True

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None or self._redis.connection is None:
            self._redis = aioredis.Redis(
                host=self.redis_host, port=self.redis_port,
                decode_responses=True
            )
        return self._redis

    # ==================== 路由处理 ====================

    async def handle_confirm_page(self, request: web.Request) -> web.Response:
        """GET /vizo/confirm/{request_id}（兼容 /confirm）"""
        request_id = request.match_info["request_id"]
        r = await self._get_redis()

        # 检查是否已回复
        existing = await r.get(f"response:{request_id}")
        if existing:
            # 解析已有回复的显示
            display_map = {"Y": ("&#x2705;", "已确认", "确认执行"),
                           "N": ("&#x274C;", "已取消", "取消操作"),
                           "D": ("&#x1F4AC;", "已发起讨论", "发起讨论")}
            icon, title, display = display_map.get(
                existing.split(":")[0] if ":" in existing else existing,
                ("&#x2705;", "已处理", existing))
            response_box = f'''<div class="response-box">
<div class="response-label">你的选择：</div>
<div class="response-content">{self._escape_html(display)}</div>
</div>'''
            return web.Response(text=RESULT_PAGE.format(
                icon=icon, result_title=title,
                message="此请求已于之前处理",
                response_box=response_box
            ), content_type="text/html")

        # 读取待处理请求
        data = await r.get(f"pending_request:{request_id}")
        if not data:
            # F6.1: 尝试从 meta 文件解析 task_id，302 重定向到控制台
            task_id = self._resolve_task_from_request(request_id)
            if task_id:
                raise web.HTTPFound(f"/vizo/tasks/{task_id}")
            return web.Response(text=EXPIRED_PAGE, content_type="text/html")

        info = json.loads(data)
        rid_info = parse_request_id(request_id)
        # 读取 button_set，向后兼容：无 button_set 时从 allow_discussion 推断
        button_set = info.get("button_set",
                              info.get("context", {}).get("button_set", ""))
        if not button_set:
            button_set = "doc_review_discussion" if info.get("allow_discussion") else "generic_confirm"

        # 构建上下文信息卡片 HTML
        context_html = self._build_context_html(info.get("context", {}))

        buttons_html = self._generate_confirm_buttons(button_set)

        preview_link = info.get("preview_link", "")
        preview_html = ""
        if preview_link:
            preview_html = (
                f'<a href="{self._escape_html(preview_link)}" '
                f'target="_blank" class="preview-link">'
                f'&#x1F4C4; 查看完整文档</a>'
            )

        created_at = info.get("created_at", "")
        if isinstance(created_at, (int, float)):
            created_at = datetime.fromtimestamp(created_at).strftime("%Y-%m-%d %H:%M:%S")
        else:
            created_at = str(created_at)[:19]

        page = CONFIRM_UNIFIED_PAGE.format(
            title=info.get("title", "操作确认"),
            request_id=request_id,
            rid_display=rid_info["display"],
            content=self._escape_html(info.get("summary", info.get("content", "请确认"))),
            preview_html=preview_html,
            created_at=created_at,
            context_html=context_html,
            buttons_html=buttons_html,
        )
        return web.Response(text=page, content_type="text/html")

    def _resolve_task_from_request(self, request_id: str) -> str:
        """从 meta 文件解析过期确认请求对应的 task_id"""
        meta_file = read_data_path(
            "confirms",
            "meta",
            f"{request_id}.json",
            project_root=_PROJECT_ROOT,
        )
        if meta_file.exists():
            try:
                data = json.loads(meta_file.read_text("utf-8"))
                return data.get("task_id", "")
            except (json.JSONDecodeError, OSError):
                pass
        return ""

    async def handle_confirm_action(self, request: web.Request) -> web.Response:
        """POST /vizo/confirm/{request_id}（兼容 /confirm）"""
        request_id = request.match_info["request_id"]
        r = await self._get_redis()

        # 防重复
        existing = await r.get(f"response:{request_id}")
        if existing:
            display_map = {"Y": "确认执行", "N": "取消操作", "D": "发起讨论"}
            key = existing.split(":")[0] if ":" in existing else existing
            response_display = display_map.get(key, existing)
            icon = {"Y": "&#x2705;", "N": "&#x274C;", "D": "&#x1F4AC;"}.get(key, "&#x2705;")
            response_box = f'''<div class="response-box">
<div class="response-label">你的回复：</div>
<div class="response-content">{self._escape_html(response_display)}</div>
</div>'''
            return web.Response(text=RESULT_PAGE.format(
                icon=icon,
                result_title="已处理",
                message="此请求已经处理过了",
                response_box=response_box
            ), content_type="text/html")

        # 检查请求是否有效
        pending = await r.get(f"pending_request:{request_id}")
        if not pending:
            return web.Response(text=EXPIRED_PAGE, content_type="text/html")

        # 解析用户选择
        post_data = await request.post()
        action = post_data.get("action", "reject")
        feedback = post_data.get("feedback", "").strip()

        # 映射动作到 Redis 响应值
        if action in ("confirm", "approve", "y", "yes"):
            response_value = "Y"
            icon, title, msg = "&#x2705;", "已确认", "操作已确认执行，将继续处理"
            response_display = "确认执行"
        elif action == "discussion":
            response_value = "discussion"
            icon, title, msg = "&#x1F4AC;", "已发起讨论", "将启动需求方向讨论"
            response_display = "发起讨论"
        elif action == "feedback":
            response_value = f"feedback:{feedback}" if feedback else "Y"
            icon, title, msg = "&#x270F;&#xFE0F;", "意见已提交", "补充意见已发送"
            response_display = f"补充意见：{feedback}" if feedback else "确认执行"
        else:
            response_value = "N"
            icon, title, msg = "&#x274C;", "已取消", "操作已取消"
            response_display = "取消操作"

        # 写入响应
        await r.setex(f"response:{request_id}", 3600, response_value)

        # 如果关联了 bridge 确认请求，同步写入 bridge 响应文件
        try:
            pending_data = json.loads(pending) if pending else {}
            bridge_id = pending_data.get("bridge_request_id")
            if bridge_id:
                from lib.confirm_bridge import respond as bridge_respond
                if response_value == "Y":
                    bridge_respond(bridge_id, "confirm")
                elif response_value == "N":
                    bridge_respond(bridge_id, "cancel")
                elif response_value == "discussion":
                    bridge_respond(bridge_id, "discussion")
                elif response_value.startswith("feedback:"):
                    bridge_respond(bridge_id, "feedback",
                                   response_value.split(":", 1)[1])
                self.logger.info(
                    f"Web 确认已同步到 bridge: {bridge_id} → {response_value}")
        except Exception as e:
            self.logger.warning(f"同步 bridge 响应失败: {e}")

        response_box = f'''<div class="response-box">
<div class="response-label">你的选择：</div>
<div class="response-content">{self._escape_html(response_display)}</div>
</div>'''

        return web.Response(text=RESULT_PAGE.format(
            icon=icon, result_title=title, message=msg, response_box=response_box
        ), content_type="text/html")

    async def handle_health(self, request: web.Request) -> web.Response:
        """GET /vizo/health（兼容 /health）"""
        redis_ok = await self._probe_redis_health()

        return web.json_response({
            "status": "ok",
            "redis": "connected" if redis_ok else "disconnected",
            "tunnel_url": self._tunnel_url or "",
            "time": datetime.now().isoformat()
        })

    async def handle_favicon(self, request: web.Request) -> web.Response:
        """Return an explicit empty favicon so browser probes do not pollute logs."""
        return web.Response(
            status=204,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    async def _probe_redis_health(self, timeout: float = 0.75) -> bool:
        """Check Redis health without letting /health block server restarts."""
        redis = aioredis.Redis(
            host=self.redis_host,
            port=self.redis_port,
            decode_responses=True,
            socket_connect_timeout=timeout,
            socket_timeout=timeout,
        )
        try:
            await asyncio.wait_for(redis.ping(), timeout=timeout + 0.25)
            return True
        except Exception:
            return False
        finally:
            try:
                await redis.aclose()
            except Exception:
                pass

    async def handle_wechat_verify(self, request: web.Request) -> web.Response:
        """GET /{filename}.txt - 微信域名验证文件"""
        filename = request.match_info["filename"]
        r = await self._get_redis()

        # 从 Redis 读取验证内容
        verify_content = await r.get(f"wechat_verify:{filename}")
        if verify_content:
            return web.Response(text=verify_content, content_type="text/plain")

        # 返回 404
        return web.Response(text="Not Found", status=404)

    # ==================== 输入页面处理 ====================

    async def handle_input_page(self, request: web.Request) -> web.Response:
        """GET /vizo/input/{request_id}（兼容 /input）"""
        request_id = request.match_info["request_id"]
        r = await self._get_redis()

        # 检查是否已回复
        existing = await r.get(f"response:{request_id}")
        if existing:
            response_box = f'''<div class="response-box">
<div class="response-label">你的回复：</div>
<div class="response-content">{self._escape_html(existing)}</div>
</div>'''
            return web.Response(text=RESULT_PAGE.format(
                icon="&#x2705;",
                result_title="已回复",
                message=f"此请求已于之前回复",
                response_box=response_box
            ), content_type="text/html")

        # 读取待处理请求
        data = await r.get(f"pending_request:{request_id}")
        if not data:
            return web.Response(text=EXPIRED_PAGE, content_type="text/html")

        info = json.loads(data)
        options = info.get("options", [])
        extra_data = info.get("extra_data", {})
        questions = extra_data.get("questions", [])
        
        # 判断是否为多选模式
        is_multi_select = False
        if questions:
            # 从 questions 中获取 multiSelect 标志
            for q in questions:
                if q.get("multiSelect", False):
                    is_multi_select = True
                    break
        
        # 生成选项 HTML
        options_html = ""
        script = ""
        
        if options:
            if is_multi_select:
                # 多选模式：checkbox + 提交按钮
                options_html = MULTI_SELECT_FORM_START.format(request_id=request_id)
                for opt in options:
                    label = opt.get("label", "")
                    desc = opt.get("description", "")
                    label_safe = self._escape_html(label)
                    desc_safe = self._escape_html(desc)
                    options_html += MULTI_SELECT_OPTION.format(
                        option_value=label,
                        option_label=label_safe,
                        option_desc=desc_safe
                    )
                options_html += MULTI_SELECT_FORM_END
                script = MULTI_SELECT_SCRIPT
            else:
                # 单选模式：点击即提交
                options_html = '<div class="options">'
                options_html += '<div class="mode-hint">点击选项直接提交</div>'
                for opt in options:
                    label = opt.get("label", "")
                    desc = opt.get("description", "")
                    label_safe = self._escape_html(label)
                    desc_safe = self._escape_html(desc)
                    options_html += SINGLE_SELECT_OPTION.format(
                        request_id=request_id,
                        option_value=label,
                        option_label=label_safe,
                        option_desc=desc_safe
                    )
                options_html += '</div>'
                # 单选模式也可以输入其他内容
                options_html += SINGLE_SELECT_OTHER.format(request_id=request_id)
        else:
            # 无选项，只显示输入框
            options_html = f"""<form method="POST" action="/vizo/input/{request_id}">
<div class="input-area">
<textarea name="response" placeholder="请输入你的回复..." required autofocus></textarea>
</div>
<button type="submit" class="btn">&#x1F4E4; 提交回复</button>
</form>"""

        rid_info = parse_request_id(request_id)
        page = INPUT_PAGE.format(
            title=info.get("title", "补充信息"),
            request_id=request_id,
            rid_display=rid_info["display"],
            content=self._render_markdown(info.get("content", "请提供更多信息")),
            options_html=options_html,
            created_at=info.get("created_at", "")[:19],
            script=script
        )
        return web.Response(text=page, content_type="text/html")

    def _escape_html(self, text: str) -> str:
        """转义 HTML 特殊字符"""
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    # action → (form_action_value, css_class) 映射
    _CONFIRM_BTN_MAP = {
        "y": ("confirm", "btn-confirm"),
        "n": ("reject", "btn-cancel"),
        "f": ("_feedback_toggle", "btn-feedback"),
        "d": ("discussion", "btn-discuss"),
        "terminate": ("reject", "btn-cancel"),
    }

    def _generate_confirm_buttons(self, button_set: str) -> str:
        """根据 button_set 配置生成确认页面的按钮 HTML"""
        from user_interface import BUTTON_SET_CONFIGS
        config = BUTTON_SET_CONFIGS.get(button_set, BUTTON_SET_CONFIGS["generic_confirm"])
        parts = []
        for btn in config["buttons"]:
            action = btn["action"]
            label = btn["label"]
            form_action, css_cls = self._CONFIRM_BTN_MAP.get(action, ("confirm", "btn-confirm"))

            if action == "f":
                onclick = "toggleFeedback()"
            elif btn.get("confirm_required"):
                onclick = f"confirmThenAction('{form_action}')"
            else:
                onclick = f"submitAction('{form_action}')"

            parts.append(
                f'<button type="button" onclick="{onclick}" '
                f'class="btn {css_cls}">{label}</button>'
            )
        return "\n".join(parts)

    @staticmethod
    def _button_set_has_feedback(button_set: str) -> bool:
        """检查 button_set 是否包含补充意见按钮"""
        from user_interface import BUTTON_SET_CONFIGS
        config = BUTTON_SET_CONFIGS.get(button_set, BUTTON_SET_CONFIGS["generic_confirm"])
        return any(btn["action"] == "f" for btn in config["buttons"])

    def _build_context_html(self, ctx: dict) -> str:
        """从 context dict 构建 HTML 信息卡片"""
        if not ctx:
            return ""

        items = []
        if ctx.get("task_name"):
            items.append(f'<div class="ctx-item"><span class="ctx-label">📋 任务</span>'
                         f'<span class="ctx-value">{self._escape_html(ctx["task_name"])}</span></div>')

        role_parts = []
        if ctx.get("role_display"):
            role_parts.append(f'🤖 {self._escape_html(ctx["role_display"])}')
        if ctx.get("model"):
            model_short = {
                "claude-opus-4-7": "opus",
                "claude-opus-4-6": "opus",
                "claude-sonnet-4-7": "sonnet",
                "claude-sonnet-4-6": "sonnet",
                "claude-sonnet-4-5-20250929": "sonnet",
                "claude-haiku-4-7": "haiku",
                "claude-haiku-4-5": "haiku",
                "claude-haiku-4-5-20251001": "haiku",
            }.get(ctx["model"], ctx["model"])
            role_parts.append(model_short)
        if role_parts:
            items.append(f'<div class="ctx-item"><span class="ctx-label">角色</span>'
                         f'<span class="ctx-value">{" | ".join(role_parts)}</span></div>')

        # 费用/耗时
        cost_parts = []
        duration = ctx.get("duration", 0)
        if duration > 0:
            m, s = divmod(int(duration), 60)
            cost_parts.append(f'⏱️ {m}分{s}秒' if m else f'⏱️ {s}秒')
        cost_usd = ctx.get("cost_usd", 0)
        if cost_usd > 0:
            cost_parts.append(f'💰 ${cost_usd:.2f}')
        tokens = ctx.get("input_tokens", 0) + ctx.get("output_tokens", 0)
        if tokens > 0:
            cost_parts.append(f'📊 {tokens:,} tokens')
        if cost_parts:
            items.append(f'<div class="ctx-item"><span class="ctx-label">本步骤</span>'
                         f'<span class="ctx-value">{" | ".join(cost_parts)}</span></div>')

        # 任务累计
        total_cost = ctx.get("total_cost_usd", 0)
        completed = ctx.get("completed_steps", [])
        total_steps = ctx.get("total_steps", 0)
        if total_cost > 0 or completed:
            progress = f'{len(completed)}/{total_steps}' if total_steps else f'{len(completed)} 步'
            acc_parts = [f'进度 {progress}']
            if total_cost > 0:
                acc_parts.append(f'累计 ${total_cost:.2f}')
            items.append(f'<div class="ctx-item"><span class="ctx-label">📈 任务</span>'
                         f'<span class="ctx-value">{" | ".join(acc_parts)}</span></div>')

        # 文档路径
        file_path = ctx.get("file_path", "")
        if file_path:
            items.append(f'<div class="ctx-item"><span class="ctx-label">📁 文件</span>'
                         f'<span class="ctx-value" style="font-size:12px;word-break:break-all">'
                         f'{self._escape_html(file_path)}</span></div>')

        if not items:
            return ""

        return (
            '<div class="context-card">'
            + "\n".join(items)
            + '</div>'
        )

    def _render_markdown(self, text: str) -> str:
        """简单 Markdown 渲染（标题、粗体、代码块、列表、分隔线）"""
        import re

        # 先转义 HTML
        text = self._escape_html(text)

        # 代码块 ```...```
        text = re.sub(r'```(\w*)\n(.*?)```', r'<pre style="background:#0f172a;padding:12px;border-radius:8px;overflow-x:auto;font-size:13px;border:1px solid #334155"><code>\2</code></pre>', text, flags=re.DOTALL)

        # 行内代码 `...`
        text = re.sub(r'`([^`]+)`', r'<code style="background:#334155;padding:2px 6px;border-radius:4px;font-size:13px">\1</code>', text)

        # 标题
        text = re.sub(r'^### (.+)$', r'<h3 style="font-size:16px;font-weight:600;margin:16px 0 8px;color:#f8fafc">\1</h3>', text, flags=re.MULTILINE)
        text = re.sub(r'^## (.+)$', r'<h2 style="font-size:18px;font-weight:600;margin:20px 0 10px;color:#f8fafc">\1</h2>', text, flags=re.MULTILINE)
        text = re.sub(r'^# (.+)$', r'<h1 style="font-size:20px;font-weight:700;margin:24px 0 12px;color:#f8fafc">\1</h1>', text, flags=re.MULTILINE)

        # 粗体
        text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)

        # 分隔线
        text = re.sub(r'^---+$', r'<hr style="border:none;border-top:1px solid #334155;margin:16px 0">', text, flags=re.MULTILINE)

        # 列表项
        text = re.sub(r'^- (.+)$', r'<li style="margin-left:20px;margin-bottom:4px">\1</li>', text, flags=re.MULTILINE)
        text = re.sub(r'^\* (.+)$', r'<li style="margin-left:20px;margin-bottom:4px">\1</li>', text, flags=re.MULTILINE)
        text = re.sub(r'^(\d+)\. (.+)$', r'<li style="margin-left:20px;margin-bottom:4px">\1. \2</li>', text, flags=re.MULTILINE)

        # Markdown 链接 [text](url)
        text = re.sub(
            r'\[([^\]]+)\]\((https?://[^\)]+)\)',
            r'<a href="\2" target="_blank" style="color:#60a5fa;text-decoration:underline;word-break:break-all">\1</a>',
            text
        )

        # 纯 URL（不在已有 <a> 标签内的）
        text = re.sub(
            r'(?<!href=")(https?://[^\s<\)]+)',
            r'<a href="\1" target="_blank" style="color:#60a5fa;text-decoration:underline;word-break:break-all">\1</a>',
            text
        )

        # 换行转 <br>（但保留 HTML 标签内的换行）
        lines = text.split('\n')
        result = []
        for line in lines:
            if line.strip().startswith('<') or line.strip() == '':
                result.append(line)
            else:
                result.append(line + '<br>')
        text = '\n'.join(result)

        return text

    async def handle_input_action(self, request: web.Request) -> web.Response:
        """POST /vizo/input/{request_id}（兼容 /input）"""
        request_id = request.match_info["request_id"]
        r = await self._get_redis()

        # 防重复
        existing = await r.get(f"response:{request_id}")
        if existing:
            response_box = f'''<div class="response-box">
<div class="response-label">你的回复：</div>
<div class="response-content">{self._escape_html(existing)}</div>
</div>'''
            return web.Response(text=RESULT_PAGE.format(
                icon="&#x2705;",
                result_title="已提交",
                message="此请求已经处理过了",
                response_box=response_box
            ), content_type="text/html")

        # 检查请求是否有效
        pending = await r.get(f"pending_request:{request_id}")
        if not pending:
            return web.Response(text=EXPIRED_PAGE, content_type="text/html")

        info = json.loads(pending)
        
        # 获取用户输入
        post_data = await request.post()
        
        # 处理多选模式
        selected_options = post_data.getall("selected", [])
        other_response = post_data.get("other_response", "").strip()
        # 处理单选模式
        single_response = post_data.get("response", "").strip()
        
        # 构建最终响应
        response_parts = []
        
        if selected_options:
            # 多选模式：选中的选项
            response_parts.extend(selected_options)
        
        if other_response:
            # 多选模式：其他内容
            response_parts.append(f"Other: {other_response}")
        
        if single_response:
            # 单选模式
            response_parts.append(single_response)
        
        # 合并响应
        if response_parts:
            if len(response_parts) == 1:
                response_text = response_parts[0]
            else:
                # 多个选项用逗号分隔
                response_text = ", ".join(response_parts)
        else:
            response_text = ""
        
        if not response_text:
            # 空输入，重新渲染页面
            # 直接调用 handle_input_page 重新生成页面
            return await self._render_input_page_with_error(request_id, info, r)

        # 写入响应（保留较长时间供 Claude Code 读取）
        await r.setex(f"response:{request_id}", 3600, response_text)  # 1小时
        # 不立即删除 pending_request，让用户可以刷新页面看到"已处理"状态
        
        # 构建响应内容显示框
        response_safe = self._escape_html(response_text)
        response_box = f'''<div class="response-box">
<div class="response-label">你的回复：</div>
<div class="response-content">{response_safe}</div>
</div>'''
        
        return web.Response(text=RESULT_PAGE.format(
            icon="&#x2705;",
            result_title="已提交",
            message="你的回复已发送给 Claude Code",
            response_box=response_box
        ), content_type="text/html")

    async def _render_input_page_with_error(self, request_id: str, info: dict, r) -> web.Response:
        """渲染带错误提示的输入页面"""
        options = info.get("options", [])
        extra_data = info.get("extra_data", {})
        questions = extra_data.get("questions", [])
        
        # 判断是否为多选模式
        is_multi_select = False
        if questions:
            for q in questions:
                if q.get("multiSelect", False):
                    is_multi_select = True
                    break
        
        # 生成选项 HTML（与 handle_input_page 逻辑相同）
        options_html = ""
        script = ""
        
        if options:
            if is_multi_select:
                options_html = MULTI_SELECT_FORM_START.format(request_id=request_id)
                for opt in options:
                    label = opt.get("label", "")
                    desc = opt.get("description", "")
                    label_safe = self._escape_html(label)
                    desc_safe = self._escape_html(desc)
                    options_html += MULTI_SELECT_OPTION.format(
                        option_value=label,
                        option_label=label_safe,
                        option_desc=desc_safe
                    )
                options_html += MULTI_SELECT_FORM_END
                script = MULTI_SELECT_SCRIPT
            else:
                options_html = '<div class="options">'
                options_html += '<div class="mode-hint">点击选项直接提交</div>'
                for opt in options:
                    label = opt.get("label", "")
                    desc = opt.get("description", "")
                    label_safe = self._escape_html(label)
                    desc_safe = self._escape_html(desc)
                    options_html += SINGLE_SELECT_OPTION.format(
                        request_id=request_id,
                        option_value=label,
                        option_label=label_safe,
                        option_desc=desc_safe
                    )
                options_html += '</div>'
                options_html += SINGLE_SELECT_OTHER.format(request_id=request_id)
        else:
            options_html = f"""<form method="POST" action="/vizo/input/{request_id}">
<div class="input-area">
<textarea name="response" placeholder="请输入你的回复..." required autofocus></textarea>
</div>
<button type="submit" class="btn">&#x1F4E4; 提交回复</button>
</form>"""
        
        # 添加错误提示
        error_content = info.get("content", "请提供更多信息") + "\n\n⚠️ 请至少选择一个选项或输入内容"

        rid_info = parse_request_id(request_id)
        page = INPUT_PAGE.format(
            title=info.get("title", "补充信息"),
            request_id=request_id,
            rid_display=rid_info["display"],
            content=self._render_markdown(error_content),
            options_html=options_html,
            created_at=info.get("created_at", "")[:19],
            script=script
        )
        return web.Response(text=page, content_type="text/html")

    # ==================== 多隧道管理 ====================

    async def start_tunnel(self) -> Optional[str]:
        """启动隧道（支持多提供商自动切换），返回公网 URL"""
        # 读取配置（支持环境变量覆盖）
        from lib.config_loader import load_config

        config = load_config()

        cs_config = config.get("confirm_server", {})
        providers_config = cs_config.get("tunnel_providers", {})

        # 按优先级排序启用的提供商
        enabled_providers = []
        for name, cfg in providers_config.items():
            if cfg.get("enabled", False):
                enabled_providers.append((cfg.get("priority", 99), name, cfg))
        enabled_providers.sort(key=lambda x: x[0])

        if not enabled_providers:
            self.logger.warning("没有启用的隧道提供商，尝试使用 cloudflare")
            return await self._start_cloudflared_tunnel()

        # 依次尝试各提供商
        for priority, provider_name, provider_config in enabled_providers:
            self.logger.info(f"尝试启动隧道: {provider_name} (优先级: {priority})")

            url = None
            if provider_name == "natapp":
                url = await self._start_natapp_tunnel(provider_config)
            elif provider_name == "cpolar":
                url = await self._start_cpolar_tunnel(provider_config)
            elif provider_name == "cloudflare":
                url = await self._start_cloudflared_tunnel()

            if url:
                self._current_provider = provider_name
                self.logger.info(f"隧道启动成功: {provider_name} -> {url}")
                return url
            else:
                self.logger.warning(f"隧道启动失败: {provider_name}，尝试下一个")

        self.logger.error("所有隧道提供商都启动失败")
        return None

    async def _start_natapp_tunnel(self, config: dict) -> Optional[str]:
        """启动 natapp 隧道"""
        natapp = os.path.expanduser("~/.local/bin/natapp")
        if not os.path.exists(natapp):
            natapp = "natapp"

        authtoken = config.get("authtoken", "")
        domain = config.get("domain", "")

        if not authtoken:
            self.logger.error("natapp authtoken 未配置")
            return None

        try:
            # 创建不含代理的环境变量
            env = os.environ.copy()
            for proxy_var in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']:
                env.pop(proxy_var, None)

            # natapp 使用 -authtoken 参数
            self._tunnel_proc = await asyncio.create_subprocess_exec(
                natapp, "-authtoken", authtoken,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env
            )

            # natapp 启动后等待并检查输出
            url = None
            for _ in range(30):  # 最多等 30 秒
                try:
                    line = await asyncio.wait_for(
                        self._tunnel_proc.stdout.readline(), timeout=2
                    )
                    if not line:
                        continue
                    text = line.decode().strip()
                    self.logger.info(f"[natapp] {text}")

                    # natapp 输出格式: Tunnel established at http://xxx.natappfree.cc
                    if "natappfree.cc" in text or "Forwarding" in text:
                        # 从配置获取域名（natapp 免费版域名固定）
                        if domain:
                            url = domain if domain.startswith("http") else f"http://{domain}"
                        else:
                            # 尝试从输出解析
                            match = re.search(r'(https?://[a-zA-Z0-9-]+\.natappfree\.cc)', text)
                            if match:
                                url = match.group(1)
                        if url:
                            break
                except asyncio.TimeoutError:
                    # 如果配置了固定域名，直接使用
                    if domain and self._tunnel_proc.returncode is None:
                        url = domain if domain.startswith("http") else f"http://{domain}"
                        break
                    continue

            if url:
                # 转换为 https（如果可用）
                if url.startswith("http://"):
                    url = url.replace("http://", "https://")
                self._tunnel_url = url
                r = await self._get_redis()
                await r.set("confirm_server:tunnel_url", url)
                self.logger.info(f"natapp Tunnel URL: {url}")
                return url
            else:
                self.logger.error("无法获取 natapp Tunnel URL")
                await self.stop_tunnel()
                return None

        except Exception as e:
            self.logger.error(f"启动 natapp Tunnel 失败: {e}")
            import traceback
            traceback.print_exc()
            return None

    async def _start_cpolar_tunnel(self, config: dict) -> Optional[str]:
        """启动 cpolar 隧道"""
        cpolar = os.path.expanduser("~/.local/bin/cpolar")
        if not os.path.exists(cpolar):
            cpolar = "cpolar"

        try:
            # 创建不含代理的环境变量
            env = os.environ.copy()
            for proxy_var in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']:
                env.pop(proxy_var, None)

            # cpolar http <port>
            self._tunnel_proc = await asyncio.create_subprocess_exec(
                cpolar, "http", str(self.port),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env
            )

            # 从输出解析 URL
            url = None
            for _ in range(30):  # 最多等 30 秒
                try:
                    line = await asyncio.wait_for(
                        self._tunnel_proc.stdout.readline(), timeout=2
                    )
                    if not line:
                        continue
                    text = line.decode().strip()
                    self.logger.info(f"[cpolar] {text}")

                    # cpolar 输出格式包含 https://xxx.cpolar.cn 或 https://xxx.cpolar.top
                    match = re.search(r'(https://[a-zA-Z0-9-]+\.(?:cpolar\.cn|cpolar\.top|vip\.cpolar\.cn))', text)
                    if match:
                        url = match.group(1)
                        break
                except asyncio.TimeoutError:
                    continue

            if url:
                self._tunnel_url = url
                r = await self._get_redis()
                await r.set("confirm_server:tunnel_url", url)
                self.logger.info(f"cpolar Tunnel URL: {url}")
                return url
            else:
                self.logger.error("无法获取 cpolar Tunnel URL")
                await self.stop_tunnel()
                return None

        except Exception as e:
            self.logger.error(f"启动 cpolar Tunnel 失败: {e}")
            import traceback
            traceback.print_exc()
            return None

    async def _start_cloudflared_tunnel(self) -> Optional[str]:
        """启动 cloudflared tunnel，返回公网 URL

        优先级：
        1. 检查是否有固定 tunnel（opus-tunnel）在运行，如果是则使用固定域名
        2. 否则启动临时 tunnel
        """
        cloudflared = os.path.expanduser("~/.local/bin/cloudflared")
        if not os.path.exists(cloudflared):
            cloudflared = "cloudflared"

        # 【修复】检查是否有固定 tunnel 在运行
        try:
            import subprocess
            result = subprocess.run(
                ["pgrep", "-f", "cloudflared tunnel run opus-tunnel"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                # opus-tunnel 在运行，尝试从配置获取固定域名
                config_file = os.path.expanduser("~/.cloudflared/config.yml")
                if os.path.exists(config_file):
                    with open(config_file) as f:
                        config_content = f.read()
                    # 简单解析 hostname
                    import re
                    hostname_match = re.search(r'hostname:\s*([^\s]+)', config_content)
                    if hostname_match:
                        fixed_domain = hostname_match.group(1)
                        url = f"https://{fixed_domain}"
                        self.logger.info(f"检测到固定 tunnel opus-tunnel 在运行，使用固定域名: {url}")
                        self._tunnel_url = url
                        r = await self._get_redis()
                        await r.set("confirm_server:tunnel_url", url)
                        return url
        except Exception as e:
            self.logger.debug(f"检查固定 tunnel 时出错: {e}")

        try:
            # 创建不含代理的环境变量（代理会导致 tunnel 连接失败）
            env = os.environ.copy()
            for proxy_var in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']:
                env.pop(proxy_var, None)

            # 使用 http2 协议，避免 QUIC 在某些网络环境下不稳定
            self._tunnel_proc = await asyncio.create_subprocess_exec(
                cloudflared, "tunnel", "--url", f"http://localhost:{self.port}",
                "--protocol", "http2",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,  # 合并 stderr 到 stdout
                env=env
            )

            # 从输出解析 URL
            url = None
            for _ in range(60):  # 最多等 60 秒
                try:
                    line = await asyncio.wait_for(
                        self._tunnel_proc.stdout.readline(), timeout=2
                    )
                    if not line:
                        continue
                    text = line.decode().strip()
                    self.logger.info(f"[cloudflared] {text}")
                    match = re.search(r'(https://[a-zA-Z0-9-]+\.trycloudflare\.com)', text)
                    if match:
                        url = match.group(1)
                        break
                except asyncio.TimeoutError:
                    continue

            if url:
                self._tunnel_url = url
                # 写入 Redis
                r = await self._get_redis()
                await r.set("confirm_server:tunnel_url", url)
                self.logger.info(f"Tunnel URL: {url}")
                return url
            else:
                self.logger.error("无法获取 Tunnel URL")
                return None

        except Exception as e:
            self.logger.error(f"启动 Tunnel 失败: {e}")
            import traceback
            traceback.print_exc()
            return None

    async def stop_tunnel(self):
        """停止 cloudflared tunnel"""
        if self._tunnel_proc:
            self._tunnel_proc.terminate()
            await self._tunnel_proc.wait()
            self._tunnel_proc = None
        # 清理 Redis
        try:
            r = await self._get_redis()
            await r.delete("confirm_server:tunnel_url")
        except Exception:
            pass

    # ==================== 企业微信回调验证 ====================

    async def handle_wecom_callback(self, request: web.Request) -> web.Response:
        """GET /wecom/callback - 企业微信 URL 验证"""
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from WXBizMsgCrypt3 import WXBizMsgCrypt

            # 读取配置
            config_path = str(_PROJECT_ROOT / 'config.json')
            with open(config_path) as f:
                config = json.load(f)

            secrets = config.get("secrets", {})
            wecom_config = config.get("wecom_callback", {})

            corpid = secrets.get("wecom_corpid", "")
            token = wecom_config.get("token", "")
            encoding_aes_key = wecom_config.get("encoding_aes_key", "")

            self.logger.info(f"WeChat Work callback config: corpid={corpid[:6]}..., token={token[:6]}..., aes_key_len={len(encoding_aes_key)}")

            if not all([corpid, token, encoding_aes_key]):
                return web.Response(text="WeChat Work callback not configured", status=500)

            # 获取验证参数
            msg_signature = request.query.get("msg_signature", "")
            timestamp = request.query.get("timestamp", "")
            nonce = request.query.get("nonce", "")
            echostr = request.query.get("echostr", "")

            self.logger.info(f"WeChat Work verification request: sig={msg_signature}, ts={timestamp}, nonce={nonce}, echostr={echostr[:20]}...")

            # 验证并解密
            wxcpt = WXBizMsgCrypt(token, encoding_aes_key, corpid)

            # 调试：手动计算签名对比
            computed_sig = wxcpt._compute_signature(token, timestamp, nonce, echostr)
            self.logger.info(f"WeChat Work signature check: received={msg_signature}, computed={computed_sig}, match={msg_signature == computed_sig}")

            ret, reply_echostr = wxcpt.VerifyURL(msg_signature, timestamp, nonce, echostr)

            if ret == 0:
                self.logger.info("WeChat Work URL verification SUCCESS")
                return web.Response(text=reply_echostr)
            else:
                self.logger.error(f"WeChat Work URL verification FAILED: ret={ret}")
                return web.Response(text=f"Verification failed: {ret}", status=403)

        except Exception as e:
            self.logger.error(f"WeChat Work callback error: {e}")
            import traceback
            traceback.print_exc()
            return web.Response(text=str(e), status=500)

    async def handle_wecom_message(self, request: web.Request) -> web.Response:
        """POST /wecom/callback - 接收企业微信发送的消息"""
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from WXBizMsgCrypt3 import WXBizMsgCrypt

            # 读取配置
            config_path = str(_PROJECT_ROOT / 'config.json')
            with open(config_path) as f:
                config = json.load(f)

            secrets = config.get("secrets", {})
            wecom_config = config.get("wecom_callback", {})

            corpid = secrets.get("wecom_corpid", "")
            token = wecom_config.get("token", "")
            encoding_aes_key = wecom_config.get("encoding_aes_key", "")

            if not all([corpid, token, encoding_aes_key]):
                return web.Response(text="WeChat Work callback not configured", status=500)

            # 获取请求参数
            msg_signature = request.query.get("msg_signature", "")
            timestamp = request.query.get("timestamp", "")
            nonce = request.query.get("nonce", "")

            # 读取请求体
            body = await request.text()
            self.logger.info(f"WeChat Work message received: sig={msg_signature[:8]}...")

            # 解密消息
            wxcpt = WXBizMsgCrypt(token, encoding_aes_key, corpid)
            ret, xml_content = wxcpt.DecryptMsg(body, msg_signature, timestamp, nonce)

            if ret != 0:
                self.logger.error(f"Decrypt message failed: ret={ret}")
                return web.Response(text="success")  # 企微要求返回 success

            # 解析 XML
            import xml.etree.ElementTree as ET
            root = ET.fromstring(xml_content)

            msg_type = root.find("MsgType").text if root.find("MsgType") is not None else ""
            content = root.find("Content").text if root.find("Content") is not None else ""
            from_user = root.find("FromUserName").text if root.find("FromUserName") is not None else ""

            self.logger.info(f"Message from {from_user}: type={msg_type}, content={content[:50] if content else 'N/A'}...")

            # 只处理文本消息
            if msg_type == "text" and content:
                r = await self._get_redis()
                await self._route_wecom_message(r, content, from_user, msg_type)

            return web.Response(text="success")

        except Exception as e:
            self.logger.error(f"WeChat Work message error: {e}")
            import traceback
            traceback.print_exc()
            return web.Response(text="success")  # 企微要求返回 success

    # ==================== 企微消息路由 ====================

    async def _route_wecom_message(self, r, content, from_user, msg_type):
        """路由企微消息到目标会话"""
        import re

        # 1. 获取所有活跃会话（带 PID 存活检测）
        active_sessions = []
        cursor = b'0'
        while True:
            cursor, keys = await r.scan(cursor, match="claude_session:*", count=100)
            for key in keys:
                data_str = await r.get(key)
                if not data_str:
                    continue
                try:
                    data = json.loads(data_str)
                    pid = data.get("pid")
                    # PID 存活检测
                    try:
                        os.kill(pid, 0)
                        session_id = f"{data['project']}:{pid}"
                        active_sessions.append({**data, "session_id": session_id})
                    except (ProcessLookupError, PermissionError):
                        # 进程已死，清理
                        await r.delete(key)
                        await r.delete(f"wecom_session:{data['project']}:{pid}")
                    except (TypeError, ValueError):
                        await r.delete(key)
                except json.JSONDecodeError:
                    await r.delete(key)
            if cursor == b'0' or cursor == 0:
                break

        # 2. 检查 @前缀路由
        prefix_match = re.match(r'^@(\S+)\s+(.+)$', content, re.DOTALL)

        if prefix_match:
            target_name = prefix_match.group(1)
            actual_content = prefix_match.group(2).strip()
            # 查找匹配项目
            targets = [s for s in active_sessions if s["project"] == target_name]
            if len(targets) == 1:
                sid = targets[0]["session_id"]
                await self._push_to_session_queue(r, sid, actual_content, from_user, msg_type)
                await r.set("wecom_default_session", sid, ex=7200)
                return
            elif len(targets) > 1:
                # 同项目多会话，提示用完整 session_id
                await self._notify_ambiguous_sessions(targets, target_name)
                return
            else:
                # 目标不存在
                await self._notify_target_not_found(target_name, active_sessions)
                return

        # 2.5 Agent 创建意图拦截
        agent_create_handled = await self._handle_agent_create_flow(
            r, content, from_user
        )
        if agent_create_handled:
            return  # 已处理，不继续路由

        # 3. 无前缀 — 按会话数量路由
        if len(active_sessions) == 0:
            await self._notify_no_sessions()
            return

        if len(active_sessions) == 1:
            sid = active_sessions[0]["session_id"]
            await self._push_to_session_queue(r, sid, content, from_user, msg_type)
            return

        # 多会话 — 检查默认会话
        default_sid = await r.get("wecom_default_session")
        if default_sid:
            # 验证默认会话仍然存活
            if any(s["session_id"] == default_sid for s in active_sessions):
                await self._push_to_session_queue(r, default_sid, content, from_user, msg_type)
                return
            else:
                # 默认会话已失效，清除
                await r.delete("wecom_default_session")

        # 多会话 + 无默认 → 提示用户选择
        await self._notify_choose_session(active_sessions)

    async def _push_to_session_queue(self, r, session_id, content, from_user, msg_type):
        """写入 per-session 消息队列"""
        message_data = json.dumps({
            "content": content,
            "from_user": from_user,
            "msg_type": msg_type,
            "received_at": datetime.now().isoformat()
        }, ensure_ascii=False)
        queue_key = f"wecom_session:{session_id}"
        await r.lpush(queue_key, message_data)
        await r.ltrim(queue_key, 0, 99)
        await r.expire(queue_key, 86400)
        self.logger.info(f"Message routed to session '{session_id}'")

    async def _notify_no_sessions(self):
        """通知用户无活跃会话"""
        try:
            from wecom_notifier import WeComNotifier
            config_path = _PROJECT_ROOT / 'config.json'
            with open(config_path) as f:
                cfg = json.load(f)
            secrets = cfg.get("secrets", {})
            notifier = WeComNotifier(
                secrets.get("wecom_corpid", ""),
                secrets.get("wecom_agentid", ""),
                secrets.get("wecom_secret", ""),
                secrets.get("wecom_userid", "")
            )
            import asyncio
            asyncio.create_task(notifier.send_text(
                "⚠️ 当前没有活跃的 Claude Code 会话。\n"
                "请先在电脑终端启动 Claude Code。"
            ))
        except Exception as e:
            self.logger.error(f"Failed to send no-sessions notification: {e}")

    async def _notify_choose_session(self, sessions):
        """通知用户选择目标会话"""
        try:
            from wecom_notifier import WeComNotifier
            config_path = _PROJECT_ROOT / 'config.json'
            with open(config_path) as f:
                cfg = json.load(f)
            secrets = cfg.get("secrets", {})
            notifier = WeComNotifier(
                secrets.get("wecom_corpid", ""),
                secrets.get("wecom_agentid", ""),
                secrets.get("wecom_secret", ""),
                secrets.get("wecom_userid", "")
            )
            lines = ["📱 当前有多个活跃的 Claude Code 会话："]
            for s in sessions:
                started = s.get("started_at", "")[:16].replace("T", " ")
                lines.append(f"  • {s['project']}（启动于 {started}）")
            lines.append("")
            lines.append("请用 @项目名 前缀指定目标，例如：")
            lines.append(f"  @{sessions[0]['project']} 你的消息内容")
            lines.append("")
            lines.append("选择后，后续消息会自动发到该会话。")
            import asyncio
            asyncio.create_task(notifier.send_text("\n".join(lines)))
        except Exception as e:
            self.logger.error(f"Failed to send choose-session notification: {e}")

    async def _notify_target_not_found(self, target_name, active_sessions):
        """通知用户目标会话不存在"""
        try:
            from wecom_notifier import WeComNotifier
            config_path = _PROJECT_ROOT / 'config.json'
            with open(config_path) as f:
                cfg = json.load(f)
            secrets = cfg.get("secrets", {})
            notifier = WeComNotifier(
                secrets.get("wecom_corpid", ""),
                secrets.get("wecom_agentid", ""),
                secrets.get("wecom_secret", ""),
                secrets.get("wecom_userid", "")
            )
            lines = [f"⚠️ 会话 @{target_name} 不活跃"]
            if active_sessions:
                lines.append("当前活跃会话：")
                for s in active_sessions[:5]:
                    lines.append(f"  • @{s['project']}")
            else:
                lines.append("当前无活跃会话。")
            import asyncio
            asyncio.create_task(notifier.send_text("\n".join(lines)))
        except Exception as e:
            self.logger.error(f"Failed to send target-not-found notification: {e}")

    async def _notify_ambiguous_sessions(self, targets, target_name):
        """通知用户同项目有多个会话"""
        try:
            from wecom_notifier import WeComNotifier
            config_path = _PROJECT_ROOT / 'config.json'
            with open(config_path) as f:
                cfg = json.load(f)
            secrets = cfg.get("secrets", {})
            notifier = WeComNotifier(
                secrets.get("wecom_corpid", ""),
                secrets.get("wecom_agentid", ""),
                secrets.get("wecom_secret", ""),
                secrets.get("wecom_userid", "")
            )
            lines = [f"⚠️ 项目 @{target_name} 有 {len(targets)} 个活跃会话："]
            for t in targets:
                started = t.get("started_at", "")[:16].replace("T", " ")
                lines.append(f"  • PID {t['pid']}（启动于 {started}）")
            lines.append("请关闭多余会话后重试。")
            import asyncio
            asyncio.create_task(notifier.send_text("\n".join(lines)))
        except Exception as e:
            self.logger.error(f"Failed to send ambiguous-sessions notification: {e}")

    # ==================== 企微消息路由 ====================

    async def _route_wecom_message(self, r, content, from_user, msg_type):
        """路由企微消息到目标会话"""
        import re

        # 1. 获取所有活跃会话（带 PID 存活检测）
        active_sessions = []
        cursor = b'0'
        while True:
            cursor, keys = await r.scan(cursor, match="claude_session:*", count=100)
            for key in keys:
                data_str = await r.get(key)
                if not data_str:
                    continue
                try:
                    data = json.loads(data_str)
                    pid = data.get("pid")
                    # PID 存活检测
                    try:
                        os.kill(pid, 0)
                        session_id = f"{data['project']}:{pid}"
                        active_sessions.append({**data, "session_id": session_id})
                    except (ProcessLookupError, PermissionError):
                        # 进程已死，清理
                        await r.delete(key)
                        await r.delete(f"wecom_session:{data['project']}:{pid}")
                    except (TypeError, ValueError):
                        await r.delete(key)
                except json.JSONDecodeError:
                    await r.delete(key)
            if cursor == b'0' or cursor == 0:
                break

        # 2. 检查 @前缀路由
        prefix_match = re.match(r'^@(\S+)\s+(.+)$', content, re.DOTALL)

        if prefix_match:
            target_name = prefix_match.group(1)
            actual_content = prefix_match.group(2).strip()
            # 查找匹配项目
            targets = [s for s in active_sessions if s["project"] == target_name]
            if len(targets) == 1:
                sid = targets[0]["session_id"]
                await self._push_to_session_queue(r, sid, actual_content, from_user, msg_type)
                await r.set("wecom_default_session", sid, ex=7200)
                return
            elif len(targets) > 1:
                # 同项目多会话，提示用完整 session_id
                await self._notify_ambiguous_sessions(targets, target_name)
                return
            else:
                # 目标不存在
                await self._notify_target_not_found(target_name, active_sessions)
                return

        # 2.5 Agent 创建意图拦截
        agent_create_handled = await self._handle_agent_create_flow(
            r, content, from_user
        )
        if agent_create_handled:
            return  # 已处理，不继续路由

        # 3. 无前缀 — 按会话数量路由
        if len(active_sessions) == 0:
            await self._notify_no_sessions()
            return

        if len(active_sessions) == 1:
            sid = active_sessions[0]["session_id"]
            await self._push_to_session_queue(r, sid, content, from_user, msg_type)
            return

        # 多会话 — 检查默认会话
        default_sid = await r.get("wecom_default_session")
        if default_sid:
            # 验证默认会话仍然存活
            if any(s["session_id"] == default_sid for s in active_sessions):
                await self._push_to_session_queue(r, default_sid, content, from_user, msg_type)
                return
            else:
                # 默认会话已失效，清除
                await r.delete("wecom_default_session")

        # 多会话 + 无默认 → 提示用户选择
        await self._notify_choose_session(active_sessions)

    async def _push_to_session_queue(self, r, session_id, content, from_user, msg_type):
        """写入 per-session 消息队列"""
        message_data = json.dumps({
            "content": content,
            "from_user": from_user,
            "msg_type": msg_type,
            "received_at": datetime.now().isoformat()
        }, ensure_ascii=False)
        queue_key = f"wecom_session:{session_id}"
        await r.lpush(queue_key, message_data)
        await r.ltrim(queue_key, 0, 99)
        await r.expire(queue_key, 86400)
        self.logger.info(f"Message routed to session '{session_id}'")

    async def _notify_no_sessions(self):
        """通知用户无活跃会话"""
        try:
            from wecom_notifier import WeComNotifier
            config_path = _PROJECT_ROOT / 'config.json'
            with open(config_path) as f:
                cfg = json.load(f)
            secrets = cfg.get("secrets", {})
            notifier = WeComNotifier(
                secrets.get("wecom_corpid", ""),
                secrets.get("wecom_agentid", ""),
                secrets.get("wecom_secret", ""),
                secrets.get("wecom_userid", "")
            )
            import asyncio
            asyncio.create_task(notifier.send_text(
                "⚠️ 当前没有活跃的 Claude Code 会话。\n"
                "请先在电脑终端启动 Claude Code。"
            ))
        except Exception as e:
            self.logger.error(f"Failed to send no-sessions notification: {e}")

    async def _send_wecom_text(self, text: str):
        """通过 WeComNotifier 发送文本消息"""
        try:
            from wecom_notifier import WeComNotifier
            config_path = _PROJECT_ROOT / 'config.json'
            with open(config_path) as f:
                cfg = json.load(f)
            secrets = cfg.get("secrets", {})
            notifier = WeComNotifier(
                secrets.get("wecom_corpid", ""),
                secrets.get("wecom_agentid", ""),
                secrets.get("wecom_secret", ""),
                secrets.get("wecom_userid", "")
            )
            await notifier.send_text(text)
        except Exception as e:
            self.logger.error(f"_send_wecom_text error: {e}")

    async def _handle_agent_create_flow(self, r, content, from_user) -> bool:
        """处理 Agent 创建对话流。返回 True 表示已处理，不继续路由。"""
        state_key = f"wecom_agent_create:{from_user}"
        state_raw = await r.get(state_key)

        # 1. 检查是否有进行中的创建流程
        if state_raw:
            state = json.loads(state_raw)
            current = state.get("state")
            content_lower = content.strip().lower()

            # 任何阶段回复"取消"都终止流程
            if content_lower in ("取消", "否", "no", "n"):
                await r.delete(state_key)
                await self._send_wecom_text("已取消 Agent 创建。")
                return True

            if current == "awaiting_confirm":
                if content_lower in ("是", "yes", "y", "好", "确认"):
                    desc = state.get("description", "")
                    if desc:
                        state["state"] = "generating"
                        await r.set(state_key, json.dumps(state, ensure_ascii=False), ex=600)
                        await self._send_wecom_text("🤖 AI 正在设计工作流，预计需要 20~40 秒...")
                        asyncio.create_task(self._do_agent_generate(r, state_key, from_user, desc))
                    else:
                        state["state"] = "awaiting_description"
                        await r.set(state_key, json.dumps(state, ensure_ascii=False), ex=600)
                        await self._send_wecom_text(
                            "好的，请描述您想创建的 Agent（20~300字）\n"
                            "例如：一个帮我分析竞品的助手，输入竞品 URL，输出分析报告"
                        )
                    return True
                else:
                    await self._send_wecom_text("请回复 [是] 开始创建，或 [否] 取消")
                    return True

            elif current == "awaiting_description":
                desc = content.strip()
                if len(desc) < 10:
                    await self._send_wecom_text("描述太简短，请至少输入 10 个字符")
                    return True
                if len(desc) > 500:
                    desc = desc[:500]
                state["state"] = "generating"
                state["description"] = desc
                await r.set(state_key, json.dumps(state, ensure_ascii=False), ex=600)
                await self._send_wecom_text("🤖 AI 正在设计工作流，预计需要 20~40 秒...")
                asyncio.create_task(self._do_agent_generate(r, state_key, from_user, desc))
                return True

            elif current == "generating":
                await self._send_wecom_text("AI 正在生成中，请稍候...")
                return True

            elif current == "awaiting_save":
                if content_lower in ("保存", "确认保存", "确认", "save", "是", "yes", "y"):
                    await self._do_agent_save(r, state_key, state)
                    return True
                elif content_lower in ("重试", "重新生成", "retry"):
                    desc = state.get("description", "")
                    state["state"] = "generating"
                    state["preview"] = None
                    await r.set(state_key, json.dumps(state, ensure_ascii=False), ex=600)
                    await self._send_wecom_text("🤖 正在重新生成...")
                    asyncio.create_task(self._do_agent_generate(r, state_key, from_user, desc))
                    return True
                else:
                    await self._send_wecom_text("请回复 [保存] 确认保存，[重试] 重新生成，或 [取消] 放弃")
                    return True

        # 2. 无进行中流程 — 检测新意图
        intent = _detect_agent_create_intent(content)
        if not intent:
            return False  # 不是创建意图，继续正常路由

        desc = intent["description"]
        state_data = {
            "state": "awaiting_confirm",
            "description": desc,
            "preview": None,
            "created_at": datetime.now().isoformat()
        }
        await r.set(state_key, json.dumps(state_data, ensure_ascii=False), ex=600)

        if desc:
            msg = (
                f"检测到您想创建 Agent 模块。\n"
                f"描述：{desc}\n\n"
                f"回复 [是] 开始创建，[否] 取消"
            )
        else:
            msg = (
                "检测到您想创建 Agent 模块。\n\n"
                "回复 [是] 继续（需补充描述），[否] 取消"
            )
        await self._send_wecom_text(msg)
        return True

    async def _do_agent_generate(self, r, state_key, from_user, description):
        """后台 Task：调用 AI 生成 Agent 并回复结果"""
        try:
            from lib.config_loader import load_config
            from opus import handle_create_agent

            config = load_config()
            result = await handle_create_agent(config, source="web", description=description)

            if not result or result.get("action") == "error":
                error_msg = result.get("message", "生成失败") if result else "生成失败"
                state_data = {
                    "state": "awaiting_save",
                    "description": description,
                    "preview": None,
                    "created_at": datetime.now().isoformat()
                }
                await r.set(state_key, json.dumps(state_data, ensure_ascii=False), ex=600)
                await self._send_wecom_text(f"❌ 生成失败：{error_msg}\n回复 [重试] 重新生成，[取消] 放弃")
                return

            module_data = result.get("module", result)
            manifest = module_data.get("manifest", {})
            roles = module_data.get("roles", {})

            # 构建预览摘要
            name = manifest.get("name", "未命名")
            mid = manifest.get("id", "unknown")
            desc = manifest.get("description", "")
            workflows = manifest.get("workflows", {})
            steps_text = ""
            for wf_name, wf_data in workflows.items():
                steps = wf_data.get("steps", [])
                for i, step in enumerate(steps, 1):
                    step_name = step.get("name", f"步骤{i}")
                    role = step.get("role", "未知角色")
                    steps_text += f"\n{i}. {step_name}（{role}）"

            state_data = {
                "state": "awaiting_save",
                "description": description,
                "preview": {"manifest": manifest, "roles": roles},
                "created_at": datetime.now().isoformat()
            }
            await r.set(state_key, json.dumps(state_data, ensure_ascii=False), ex=600)

            await self._send_wecom_text(
                f"✅ Agent 已生成：\n"
                f"📋 {name}（{mid}）\n"
                f"📝 {desc}\n\n"
                f"工作流步骤：{steps_text}\n\n"
                f"回复 [保存] 确认保存，[重试] 重新生成，[取消] 放弃"
            )

        except Exception as e:
            self.logger.error(f"_do_agent_generate error: {e}")
            state_data = {
                "state": "awaiting_save",
                "description": description,
                "preview": None,
                "created_at": datetime.now().isoformat()
            }
            await r.set(state_key, json.dumps(state_data, ensure_ascii=False), ex=600)
            await self._send_wecom_text(f"❌ 生成异常：{e}\n回复 [重试] 重新生成，[取消] 放弃")

    async def _do_agent_save(self, r, state_key, state):
        """保存 Agent 模块到磁盘"""
        try:
            preview = state.get("preview")
            if not preview:
                await self._send_wecom_text("❌ 无预览数据，请重新生成")
                await r.delete(state_key)
                return

            from lib.agent_creator import (
                validate_manifest, check_id_conflict,
                check_module_limit, write_module_to_disk,
            )
            from lib.config_loader import load_config

            manifest = preview["manifest"]
            roles = preview["roles"]
            config = load_config()

            validation = validate_manifest(manifest)
            if not validation["valid"]:
                await self._send_wecom_text(f"❌ 校验失败：{'; '.join(validation['errors'])}\n回复 [取消] 放弃")
                return

            limit = check_module_limit(config)
            if not limit["allowed"]:
                await self._send_wecom_text(f"❌ 已达模块上限（{limit['max_limit']}个）")
                await r.delete(state_key)
                return

            final_id = manifest.get("id", "")
            conflict = check_id_conflict(final_id)
            if conflict["conflict"] and conflict["conflict_source"] == "_builtin":
                await self._send_wecom_text(f"❌ 模块 ID '{final_id}' 与内置模块冲突，请在 PC 端修改后保存")
                await r.delete(state_key)
                return

            write_module_to_disk(manifest, roles, module_id=final_id)

            # 同步 CLAUDE.md
            try:
                from lib.module_sync import sync_modules_to_claude_md
                sync_modules_to_claude_md(str(_PROJECT_ROOT))
            except Exception:
                pass

            await r.delete(state_key)
            await self._send_wecom_text(
                f"✅ 模块 {manifest.get('name', final_id)} 已保存！\n"
                f"使用方式：vizo \"任务描述\" @{final_id}"
            )

        except Exception as e:
            self.logger.error(f"_do_agent_save error: {e}")
            await self._send_wecom_text(f"❌ 保存失败：{e}")
            await r.delete(state_key)

    async def _notify_choose_session(self, sessions):
        """通知用户选择目标会话"""
        try:
            from wecom_notifier import WeComNotifier
            config_path = _PROJECT_ROOT / 'config.json'
            with open(config_path) as f:
                cfg = json.load(f)
            secrets = cfg.get("secrets", {})
            notifier = WeComNotifier(
                secrets.get("wecom_corpid", ""),
                secrets.get("wecom_agentid", ""),
                secrets.get("wecom_secret", ""),
                secrets.get("wecom_userid", "")
            )
            lines = ["📱 当前有多个活跃的 Claude Code 会话："]
            for s in sessions:
                started = s.get("started_at", "")[:16].replace("T", " ")
                lines.append(f"  • {s['project']}（启动于 {started}）")
            lines.append("")
            lines.append("请用 @项目名 前缀指定目标，例如：")
            lines.append(f"  @{sessions[0]['project']} 你的消息内容")
            lines.append("")
            lines.append("选择后，后续消息会自动发到该会话。")
            import asyncio
            asyncio.create_task(notifier.send_text("\n".join(lines)))
        except Exception as e:
            self.logger.error(f"Failed to send choose-session notification: {e}")

    async def _notify_target_not_found(self, target_name, active_sessions):
        """通知用户目标会话不存在"""
        try:
            from wecom_notifier import WeComNotifier
            config_path = _PROJECT_ROOT / 'config.json'
            with open(config_path) as f:
                cfg = json.load(f)
            secrets = cfg.get("secrets", {})
            notifier = WeComNotifier(
                secrets.get("wecom_corpid", ""),
                secrets.get("wecom_agentid", ""),
                secrets.get("wecom_secret", ""),
                secrets.get("wecom_userid", "")
            )
            lines = [f"⚠️ 会话 @{target_name} 不活跃"]
            if active_sessions:
                lines.append("当前活跃会话：")
                for s in active_sessions[:5]:
                    lines.append(f"  • @{s['project']}")
            else:
                lines.append("当前无活跃会话。")
            import asyncio
            asyncio.create_task(notifier.send_text("\n".join(lines)))
        except Exception as e:
            self.logger.error(f"Failed to send target-not-found notification: {e}")

    async def _notify_ambiguous_sessions(self, targets, target_name):
        """通知用户同项目有多个会话"""
        try:
            from wecom_notifier import WeComNotifier
            config_path = _PROJECT_ROOT / 'config.json'
            with open(config_path) as f:
                cfg = json.load(f)
            secrets = cfg.get("secrets", {})
            notifier = WeComNotifier(
                secrets.get("wecom_corpid", ""),
                secrets.get("wecom_agentid", ""),
                secrets.get("wecom_secret", ""),
                secrets.get("wecom_userid", "")
            )
            lines = [f"⚠️ 项目 @{target_name} 有 {len(targets)} 个活跃会话："]
            for t in targets:
                started = t.get("started_at", "")[:16].replace("T", " ")
                lines.append(f"  • PID {t['pid']}（启动于 {started}）")
            lines.append("请关闭多余会话后重试。")
            import asyncio
            asyncio.create_task(notifier.send_text("\n".join(lines)))
        except Exception as e:
            self.logger.error(f"Failed to send ambiguous-sessions notification: {e}")

    # ==================== 智能机器人回调 ====================

    async def handle_bot_callback(self, request: web.Request) -> web.Response:
        """GET /bot/callback - 智能机器人 URL 验证"""
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from WXBizMsgCrypt3 import WXBizMsgCrypt

            config_path = str(_PROJECT_ROOT / 'config.json')
            with open(config_path) as f:
                config = json.load(f)

            secrets = config.get("secrets", {})
            bot_config = config.get("wecom_bot_callback", {})

            corpid = secrets.get("wecom_corpid", "")
            token = bot_config.get("token", "")
            encoding_aes_key = bot_config.get("encoding_aes_key", "")

            if not all([corpid, token, encoding_aes_key]):
                return web.Response(text="Bot callback not configured", status=500)

            msg_signature = request.query.get("msg_signature", "")
            timestamp = request.query.get("timestamp", "")
            nonce = request.query.get("nonce", "")
            echostr = request.query.get("echostr", "")

            self.logger.info(f"Bot verification: sig={msg_signature[:8] if msg_signature else 'N/A'}..., ts={timestamp}")

            wxcpt = WXBizMsgCrypt(token, encoding_aes_key, corpid)
            ret, reply_echostr = wxcpt.VerifyURL(msg_signature, timestamp, nonce, echostr)

            if ret == 0:
                self.logger.info("Bot URL verification SUCCESS")
                return web.Response(text=reply_echostr)
            else:
                self.logger.error(f"Bot URL verification FAILED: ret={ret}")
                return web.Response(text=f"Verification failed: {ret}", status=403)

        except Exception as e:
            self.logger.error(f"Bot callback error: {e}")
            import traceback
            traceback.print_exc()
            return web.Response(text=str(e), status=500)

    async def handle_bot_message(self, request: web.Request) -> web.Response:
        """POST /bot/callback - 接收智能机器人消息"""
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from WXBizMsgCrypt3 import WXBizMsgCrypt

            config_path = str(_PROJECT_ROOT / 'config.json')
            with open(config_path) as f:
                config = json.load(f)

            secrets = config.get("secrets", {})
            bot_config = config.get("wecom_bot_callback", {})

            corpid = secrets.get("wecom_corpid", "")
            token = bot_config.get("token", "")
            encoding_aes_key = bot_config.get("encoding_aes_key", "")

            if not all([corpid, token, encoding_aes_key]):
                return web.Response(text="Bot callback not configured", status=500)

            msg_signature = request.query.get("msg_signature", "")
            timestamp = request.query.get("timestamp", "")
            nonce = request.query.get("nonce", "")

            body = await request.text()
            self.logger.info(f"Bot message received: sig={msg_signature[:8] if msg_signature else 'N/A'}...")

            wxcpt = WXBizMsgCrypt(token, encoding_aes_key, corpid)
            ret, xml_content = wxcpt.DecryptMsg(body, msg_signature, timestamp, nonce)

            if ret != 0:
                self.logger.error(f"Bot decrypt message failed: ret={ret}")
                return web.Response(text="success")

            import xml.etree.ElementTree as ET
            root = ET.fromstring(xml_content)

            msg_type = root.find("MsgType").text if root.find("MsgType") is not None else ""
            content = root.find("Content").text if root.find("Content") is not None else ""
            from_user = root.find("FromUserName").text if root.find("FromUserName") is not None else ""

            self.logger.info(f"Bot message from {from_user}: type={msg_type}, content={content[:50] if content else 'N/A'}...")

            # 保存到单独的机器人消息队列
            if msg_type == "text" and content:
                r = await self._get_redis()
                message_data = json.dumps({
                    "content": content,
                    "from_user": from_user,
                    "msg_type": msg_type,
                    "source": "bot",
                    "received_at": datetime.now().isoformat()
                }, ensure_ascii=False)

                await r.lpush("wecom_bot_messages", message_data)
                await r.ltrim("wecom_bot_messages", 0, 99)
                await r.expire("wecom_bot_messages", 86400)

                # 也同步到通用消息队列，方便统一处理
                await r.lpush("wecom_messages", message_data)
                await r.ltrim("wecom_messages", 0, 99)

                self.logger.info("Bot message saved to Redis queue")

            return web.Response(text="success")

        except Exception as e:
            self.logger.error(f"Bot message error: {e}")
            import traceback
            traceback.print_exc()
            return web.Response(text="success")

    # ==================== 文档服务 ====================

    def _get_docs_root(self) -> Path:
        """获取文档根目录"""
        return _PROJECT_ROOT / '.test-opus'

    async def handle_doc_json(self, request: web.Request) -> web.Response:
        """GET /vizo/docs/{task_id}/{document} - 返回 JSON 文档"""
        task_id = request.match_info.get("task_id", "")
        document = request.match_info.get("document", "")

        # 构建文件路径
        docs_root = self._get_docs_root()
        doc_path = docs_root / task_id / f"{document}.json"

        self.logger.info(f"Doc request: /vizo/docs/{task_id}/{document}.json")

        # 安全检查：防止目录遍历
        try:
            resolved_path = doc_path.resolve()
            resolved_root = docs_root.resolve()
            if not str(resolved_path).startswith(str(resolved_root)):
                return web.json_response(
                    {"error": "Path traversal detected"},
                    status=403
                )
        except Exception as e:
            self.logger.warning(f"Path resolution error: {e}")
            return web.json_response({"error": "Invalid path"}, status=400)

        # 检查文件是否存在
        if not doc_path.exists():
            self.logger.warning(f"Document not found: {doc_path}")
            return web.json_response(
                {"error": "Document not found"},
                status=404
            )

        # 读取并返回 JSON
        try:
            with open(doc_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            response = web.json_response(data)
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
            return response
        except json.JSONDecodeError as e:
            self.logger.error(f"JSON decode error: {e}")
            return web.json_response({"error": "Invalid JSON format"}, status=400)
        except Exception as e:
            self.logger.error(f"Error reading document: {e}")
            return web.json_response({"error": "Error reading document"}, status=500)

    async def handle_doc_html(self, request: web.Request) -> web.Response:
        """GET /vizo/docs/{task_id}/{document}.html - 返回 HTML 格式的文档"""
        task_id = request.match_info.get("task_id", "")
        document = request.match_info.get("document", "")

        docs_root = self._get_docs_root()
        doc_path = docs_root / task_id / f"{document}.json"

        if not doc_path.exists():
            return web.Response(
                text=f"<h1>文档未找到</h1><p>路径: {doc_path}</p>",
                content_type="text/html",
                status=404
            )

        try:
            with open(doc_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
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
        """GET /vizo/docs/ - 列出所有可用的文档"""
        try:
            docs = {}
            docs_root = self._get_docs_root()

            if docs_root.exists():
                for task_dir in docs_root.iterdir():
                    if task_dir.is_dir():
                        task_id = task_dir.name
                        docs[task_id] = []
                        for doc_file in task_dir.glob("*.json"):
                            doc_name = doc_file.stem
                            docs[task_id].append({
                                "name": doc_name,
                                "url": f"/vizo/docs/{task_id}/{doc_name}.json",
                                "html_url": f"/vizo/docs/{task_id}/{doc_name}.html"
                            })

            response = web.json_response({
                "docs_root": str(docs_root),
                "total_tasks": len(docs),
                "tasks": docs
            })
            response.headers['Access-Control-Allow-Origin'] = '*'
            return response
        except Exception as e:
            self.logger.error(f"Error listing documents: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def handle_cors_options(self, request: web.Request) -> web.Response:
        """处理 CORS OPTIONS 请求"""
        response = web.Response()
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response

    def _json_to_html(self, data: dict, title: str = "Document") -> str:
        """将 JSON 数据转换为 HTML"""
        def escape_html(text: str) -> str:
            return (text.replace('&', '&amp;').replace('<', '&lt;')
                    .replace('>', '&gt;').replace('"', '&quot;'))

        html = ['<!DOCTYPE html><html lang="zh-CN"><head>',
                '<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">',
                f'<title>{escape_html(title)}</title><style>',
                'body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;line-height:1.6;color:#333;background:#f5f5f5;margin:0;padding:20px}',
                '.container{max-width:900px;margin:0 auto;background:#fff;border-radius:8px;padding:30px;box-shadow:0 2px 8px rgba(0,0,0,.1)}',
                'h1{color:#2c3e50;border-bottom:2px solid #3498db;padding-bottom:10px}',
                'h2{color:#34495e;margin-top:30px;padding-top:20px;border-top:1px solid #ecf0f1}',
                'pre{background:#f8f8f8;border:1px solid #ddd;border-radius:4px;padding:12px;overflow-x:auto;font-family:monospace;font-size:13px}',
                'code{background:#f4f4f4;padding:2px 6px;border-radius:3px;font-family:monospace}',
                '.json-value{background:#fafafa;padding:10px;border-left:3px solid #3498db;margin:10px 0;border-radius:3px}',
                '</style></head><body><div class="container">',
                f'<h1>{escape_html(title)}</h1>']

        def render_dict(d, level=0):
            for k, v in d.items():
                html.append(f'<h{min(3, level+2)}>{escape_html(str(k))}</h{min(3, level+2)}>')
                if isinstance(v, dict):
                    render_dict(v, level+1)
                elif isinstance(v, list):
                    render_list(v, level+1)
                else:
                    html.append(f'<div class="json-value"><code>{escape_html(str(v))}</code></div>')

        def render_list(l, level=0):
            html.append('<ul>')
            for item in l:
                if isinstance(item, dict):
                    html.append('<li>')
                    render_dict(item, level+1)
                    html.append('</li>')
                else:
                    html.append(f'<li>{escape_html(str(item))}</li>')
            html.append('</ul>')

        if isinstance(data, dict):
            render_dict(data)
        elif isinstance(data, list):
            render_list(data)

        html.extend(['</div></body></html>'])
        return '\n'.join(html)

    # ==================== 前端预览 ====================

    async def handle_preview_page(self, request: web.Request) -> web.Response:
        """GET /vizo/preview/{preview_id} - 显示前端预览页面"""
        preview_id = request.match_info["preview_id"]
        r = await self._get_redis()

        # 读取预览内容
        data = await r.get(f"preview:{preview_id}")

        # F6.2: Redis 未命中时从磁盘回退读取
        if not data:
            backup_file = read_data_path(
                "previews",
                f"{preview_id}.json",
                project_root=_PROJECT_ROOT,
            )
            if backup_file.exists():
                try:
                    data = backup_file.read_text("utf-8")
                except OSError:
                    pass

        if not data:
            return web.Response(
                text=SIMPLE_RESULT_PAGE.format(
                    icon="&#x26A0;",
                    result_title="预览不存在或已过期",
                    message="此预览链接已失效。"
                ),
                content_type="text/html",
                headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}
            )

        try:
            info = json.loads(data)
            html_content = info.get("html", "")
            title = info.get("title", "预览")

            # 如果存储的是完整 HTML，直接返回
            if html_content.strip().startswith("<!DOCTYPE") or html_content.strip().startswith("<html"):
                return web.Response(
                    text=html_content,
                    content_type="text/html",
                    headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}
                )

            # 否则包装成完整 HTML
            wrapped_html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{self._escape_html(title)}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; padding: 20px; }}
</style>
</head>
<body>
{html_content}
</body>
</html>"""
            return web.Response(
                    text=wrapped_html,
                    content_type="text/html",
                    headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}
                )

        except json.JSONDecodeError:
            # 兼容：直接存储 HTML 字符串的情况
            no_cache_headers = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}
            if data.strip().startswith("<!DOCTYPE") or data.strip().startswith("<html"):
                return web.Response(text=data, content_type="text/html", headers=no_cache_headers)
            return web.Response(text=data, content_type="text/html", headers=no_cache_headers)

    # ==================== 任务控制台：辅助方法 ====================

    def _resolve_task_name(self, state: dict, task_id: str) -> str:
        """解析任务显示名称：task_name → RA 标题 → description → task_id"""
        # 1. state.json 中已有 task_name
        name = state.get("task_name", "")
        if name:
            return summarize_task_title(name, fallback=task_id)
        # 2. 从 RA 输出文件提取标题
        task_dir = state.get("dir", "")
        if task_dir:
            ra_file = os.path.join(task_dir, "00-requirement-analysis.md")
            try:
                if os.path.exists(ra_file):
                    with open(ra_file, encoding="utf-8") as f:
                        first_line = f.readline().strip()
                    # 格式1: "# 需求分析：XXX" 或 "# 需求分析报告：XXX"
                    m = re.match(r"^#\s*需求分析(?:报告)?[：:]\s*(.+)$", first_line)
                    if m:
                        return summarize_task_title(m.group(1).strip(), fallback=task_id)
                    # 格式2: "# XXX — 需求分析(文档/报告)"
                    m = re.match(r'^#\s*(.+?)\s*[—\-]+\s*需求分析', first_line)
                    if m:
                        return summarize_task_title(m.group(1).strip(), fallback=task_id)
            except Exception:
                pass
        # 3. fallback: description 的第一句
        desc = state.get("description", "")
        if desc:
            return summarize_task_title(desc, fallback=task_id)
        # 4. 最后回退到 task_id
        return task_id

    def _calc_duration_str(self, created_at_str: str) -> str:
        """从 created_at 计算到现在的运行时长"""
        try:
            created = datetime.fromisoformat(created_at_str)
            total = int((datetime.now() - created).total_seconds())
            if total < 60:
                return f"{total}秒"
            m = total // 60
            if m < 60:
                return f"{m}分钟"
            h, m = divmod(m, 60)
            return f"{h}小时{m}分钟"
        except (ValueError, TypeError):
            return "未知"

    def _format_step_duration(self, seconds) -> str:
        """格式化步骤耗时"""
        try:
            total = int(seconds)
            m, s = divmod(total, 60)
            if m > 0 and s > 0:
                return f"{m}分{s}秒"
            elif m > 0:
                return f"{m}分钟"
            else:
                return f"{s}秒"
        except (ValueError, TypeError):
            return ""

    async def _load_all_tasks(self) -> list:
        """扫描所有项目的兼容任务目录加载任务"""
        # 收集所有项目的 tasks 目录
        tasks_dirs = []  # [(project_name, tasks_dir_path), ...]
        seen_dirs = set()
        try:
            with open(str(_PROJECT_ROOT / "config.json")) as f:
                cfg = json.load(f)
            for proj_name, proj_info in (cfg.get("projects") or {}).items():
                for td in iter_storage_dirs("tasks", project_root=proj_info.get("path", "")):
                    key = (proj_name, str(td))
                    if key in seen_dirs:
                        continue
                    seen_dirs.add(key)
                    tasks_dirs.append((proj_name, td))
        except Exception:
            pass
        # 兜底：始终包含 _PROJECT_ROOT（无项目配置时仍能看到主项目任务）
        for default_td in iter_storage_dirs("tasks", project_root=_PROJECT_ROOT):
            key = ("", str(default_td))
            if key in seen_dirs:
                continue
            seen_dirs.add(key)
            tasks_dirs.append(("", default_td))

        results = []
        seen_tasks = set()
        for proj_name, tasks_dir in tasks_dirs:
            if not tasks_dir.exists():
                continue
            for task_dir in tasks_dir.iterdir():
                if not task_dir.is_dir():
                    continue
                state_file = task_dir / "state.json"
                if not state_file.exists():
                    continue
                task_key = (proj_name, task_dir.name)
                if task_key in seen_tasks:
                    continue
                seen_tasks.add(task_key)
                try:
                    state = json.loads(state_file.read_text("utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                # 归一化历史状态值
                if state.get("status") == "in_progress":
                    state["status"] = "running"

                # 读取 progress.json（可选，降级为空 dict）
                progress = {}
                progress_file = task_dir / "progress.json"
                if progress_file.exists():
                    try:
                        progress = json.loads(progress_file.read_text("utf-8"))
                    except (json.JSONDecodeError, OSError):
                        pass

                # 费用：优先从 progress.json 读取（含子任务），降级到 cost.json
                cost_usd = progress.get("cost_usd", 0)
                if not cost_usd:
                    cost_file = task_dir / "cost.json"
                    if cost_file.exists():
                        try:
                            costs = json.loads(cost_file.read_text("utf-8"))
                            cost_usd = sum(c.get("cost_usd", 0) for c in costs)
                        except (json.JSONDecodeError, OSError):
                            pass

                results.append({
                    "id": task_dir.name,
                    "project": proj_name,
                    "state": state,
                    "progress": progress,
                    "cost_usd": round(cost_usd, 2),
                })

        # 运行中/暂停优先，其余按时间倒序（Python sort 稳定排序）
        results.sort(key=lambda x: x["state"].get("created_at", ""), reverse=True)
        results.sort(key=lambda x: 0 if x["state"].get("status", "") in ("running", "paused") else 1)
        return results

    async def handle_task_list_json(self, request: web.Request) -> web.Response:
        """GET /vizo/api/tasks - 任务列表 JSON API（供 Web Console 下拉菜单使用）"""
        try:
            tasks = await self._load_all_tasks()
            items = []
            for t in tasks:
                state = t.get("state", {})
                title = self._resolve_task_name(state, t["id"])
                items.append({
                    "task_id": t["id"],
                    "task_name": title,
                    "task_title": title,
                    "task_summary": state.get("task_summary") or title,
                    "status": state.get("status", "unknown"),
                    "cost_usd": t.get("cost_usd", 0),
                    "created_at": state.get("created_at", ""),
                    "project": t.get("project", ""),
                    "task_type": state.get("task_type", ""),
                    "module_id": state.get("module_id", ""),
                })
            return web.json_response({"tasks": items})
        except Exception as e:
            self.logger.error(f"handle_task_list_json error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def _load_task_detail(self, task_id: str) -> dict:
        """加载单个任务的完整信息"""
        if self._web_console:
            task_dir = self._web_console._find_task_dir(task_id)
        else:
            task_dir = resolve_task_dir(task_id, project_root=_PROJECT_ROOT)
        if not task_dir.is_dir():
            return None

        state_file = task_dir / "state.json"
        if not state_file.exists():
            return None

        try:
            state = json.loads(state_file.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        # 归一化历史状态值
        if state.get("status") == "in_progress":
            state["status"] = "running"

        progress = {}
        progress_file = task_dir / "progress.json"
        if progress_file.exists():
            try:
                progress = json.loads(progress_file.read_text("utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        # 费用：优先从 progress.json 读取（含子任务），降级到 cost.json
        cost_usd = progress.get("cost_usd", 0)
        if not cost_usd:
            cost_file = task_dir / "cost.json"
            if cost_file.exists():
                try:
                    costs = json.loads(cost_file.read_text("utf-8"))
                    cost_usd = sum(c.get("cost_usd", 0) for c in costs)
                except (json.JSONDecodeError, OSError):
                    pass

        return {
            "id": task_id,
            "task_dir": str(task_dir),
            "state": state,
            "progress": progress,
            "cost_usd": round(cost_usd, 2),
        }

    def _render_task_card(self, task: dict) -> str:
        """渲染单个任务卡片 HTML"""
        state = task["state"]
        progress = task["progress"]
        task_id = task["id"]
        status = state.get("status", "unknown")
        cfg = STATUS_CONFIG.get(status, {"color": "#475569", "text_color": "#94a3b8", "icon": "○", "label": status})

        name = self._escape_html(self._resolve_task_name(state, task_id))
        duration = self._calc_duration_str(state.get("created_at", ""))
        cost = f"${task['cost_usd']:.2f}"

        # 当前步骤信息
        current_step = ""
        steps = progress.get("steps", [])
        for step in steps:
            if step.get("status") == "running":
                current_step = STEP_DISPLAY.get(step.get("name", ""), step.get("name", ""))
                break
        if not current_step and state.get("current_step"):
            current_step = STEP_DISPLAY.get(state["current_step"], state["current_step"])

        meta_parts = [f'<span class="status" style="color:{cfg["text_color"]}">{cfg["icon"]} {cfg["label"]}</span>']
        if current_step:
            meta_parts.append(f'<span class="sep">·</span><span>{self._escape_html(current_step)}中</span>')
        meta_parts.append(f'<span class="sep">·</span><span>{duration}</span>')
        meta_parts.append(f'<span class="sep">·</span><span>{cost}</span>')

        return f'''<a href="/vizo/tasks/{task_id}" class="card" style="border-left:3px solid {cfg['color']}">
<div class="card-name">{name}</div>
<div class="card-meta">{''.join(meta_parts)}</div>
</a>'''

    def _render_steps_html(self, progress: dict, state: dict) -> str:
        """渲染步骤列表 HTML"""
        steps = progress.get("steps", [])

        # 降级：无 progress.json 时从 state.json 构建
        if not steps and state.get("completed_steps"):
            for step_name in state["completed_steps"]:
                steps.append({"name": step_name, "status": "completed",
                              "cost_usd": 0, "duration": 0, "output_doc": "", "preview_url": ""})

        if not steps:
            return '<div style="color:#64748b;font-size:13px;text-align:center;padding:12px 0">暂无步骤信息</div>'

        html = ""
        for step in steps:
            name_cn = STEP_DISPLAY.get(step.get("name", ""), step.get("name", ""))
            status = step.get("status", "")

            if status == "completed":
                icon = "✅"
                detail_parts = []
                if step.get("preview_url"):
                    doc = self._escape_html(step.get("output_doc", ""))
                    url = self._escape_html(step["preview_url"])
                    detail_parts.append(f'<a href="{url}" style="color:#60a5fa;">{doc}</a>')
                elif step.get("output_doc"):
                    detail_parts.append(f'<span style="color:#64748b;">{self._escape_html(step["output_doc"])}</span>')
                if step.get("cost_usd"):
                    detail_parts.append(f'${step["cost_usd"]:.2f}')
                if step.get("duration"):
                    detail_parts.append(self._format_step_duration(step["duration"]))
                detail = " · ".join(detail_parts)

            elif status == "running":
                icon = "🔄"
                detail = '<span class="blink">进行中...</span>'

            elif status == "paused":
                icon = "🟡"
                detail = '<span style="color:#fb923c;">已暂停</span>'

            elif status == "error":
                icon = "❌"
                detail = '<span style="color:#f87171;">执行失败</span>'

            else:
                icon = "○"
                detail = ""

            html += f'''<div class="step-item">
<span class="step-icon">{icon}</span>
<div class="step-content">
<div class="step-name">{self._escape_html(name_cn)}</div>
<div class="step-detail">{detail}</div>
</div>
</div>'''

        return html

    def _render_actions_html(self, state: dict, task_id: str) -> str:
        """根据任务状态渲染操作按钮区"""
        status = state.get("status", "")
        if status == "running":
            return '''<div class="actions">
<div class="grid-2">
<button onclick="doPause()" class="btn btn-primary" id="pauseBtn">⏸ 暂停</button>
<button onclick="doTerminate()" class="btn btn-danger" id="termBtn">🛑 终止</button>
</div>
</div>'''
        elif status == "paused":
            return f'''<div class="actions">
<textarea id="feedback" class="feedback-area" placeholder="补充信息（选填）：告诉 AI 助手你的修改意见..." rows="3" maxlength="2000"></textarea>
<button onclick="doResume()" class="btn btn-success" id="resumeBtn">▶️ 继续执行</button>
<div class="grid-2">
<button onclick="location.href='/vizo/tasks/{task_id}/rollback'" class="btn btn-secondary">↩️ 回退到指定步骤</button>
<button onclick="doTerminate()" class="btn btn-danger" id="termBtn">🛑 终止</button>
</div>
</div>'''
        elif status == "failed":
            return '''<div class="actions">
<textarea id="feedback" class="feedback-area" placeholder="补充信息（选填）：告诉 AI 助手你的修改意见..." rows="3" maxlength="2000"></textarea>
<div class="grid-2">
<button onclick="doResume()" class="btn btn-success">▶️ 重试</button>
<button onclick="doTerminate()" class="btn btn-danger">🛑 终止</button>
</div>
</div>'''
        return ""

    async def _spawn_opus_resume(self, task_id: str):
        """以 detached 方式 spawn opus --resume 子进程"""
        proc = await asyncio.create_subprocess_exec(
            "python3", str(_PROJECT_ROOT / "opus.py"),
            "--resume", "--task-id", task_id, "--wecom", "--auto-confirm",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        self.logger.info(f"Spawned opus --resume for task {task_id}, pid={proc.pid}")

    # ==================== 任务控制台：页面 handler ====================

    async def handle_task_list(self, request: web.Request) -> web.Response:
        """GET /vizo/tasks - 任务列表页"""
        try:
            tasks = await self._load_all_tasks()

            if not tasks:
                cards_html = '<div class="empty">暂无任务</div>'
            else:
                cards_html = ""
                current_group = None
                for task in tasks:
                    status = task["state"].get("status", "unknown")
                    cfg = STATUS_CONFIG.get(status, {"icon": "○", "label": status})
                    group_key = status
                    if group_key != current_group:
                        current_group = group_key
                        cards_html += f'<div class="group-title">{cfg["icon"]} {cfg["label"]}</div>'
                    cards_html += self._render_task_card(task)

            html = TASK_LIST_PAGE.format(cards_html=cards_html)
            return web.Response(text=html, content_type="text/html",
                                headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"})
        except Exception as e:
            self.logger.error(f"handle_task_list error: {e}")
            return web.Response(
                text=SIMPLE_RESULT_PAGE.format(icon="&#x26A0;", result_title="加载失败", message=str(e)),
                content_type="text/html", status=500)

    async def handle_task_detail(self, request: web.Request) -> web.Response:
        """GET /vizo/tasks/{task_id} - 任务详情页"""
        task_id = request.match_info["task_id"]
        try:
            task = await self._load_task_detail(task_id)
            if not task:
                return web.Response(
                    text=SIMPLE_RESULT_PAGE.format(icon="&#x26A0;", result_title="任务不存在",
                                                   message=f"任务 {self._escape_html(task_id)} 不存在"),
                    content_type="text/html", status=404)

            state = task["state"]
            status = state.get("status", "unknown")
            cfg = STATUS_CONFIG.get(status, {"color": "#475569", "text_color": "#94a3b8", "icon": "○", "label": status})

            step_display_json = json.dumps(STEP_DISPLAY, ensure_ascii=False)
            # 检查任务是否有代码变更（基于 git diff 检测实际代码变更）
            has_code_changes = check_task_code_changes(state)
            html = TASK_DETAIL_PAGE.format(
                task_name=self._escape_html(self._resolve_task_name(state, task_id)),
                status_color=cfg["text_color"],
                status_icon=cfg["icon"],
                status_label=cfg["label"],
                duration_str=self._calc_duration_str(state.get("created_at", "")),
                cost_str=f"${task['cost_usd']:.2f}",
                steps_html=self._render_steps_html(task["progress"], state),
                actions_html=self._render_actions_html(state, task_id),
                step_display_json=step_display_json,
                task_id=task_id,
                task_status=status,
                has_code_changes="true" if has_code_changes else "false",
            )
            return web.Response(text=html, content_type="text/html",
                                headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"})
        except Exception as e:
            self.logger.error(f"handle_task_detail error: {e}")
            return web.Response(
                text=SIMPLE_RESULT_PAGE.format(icon="&#x26A0;", result_title="加载失败", message=str(e)),
                content_type="text/html", status=500)

    async def handle_task_rollback(self, request: web.Request) -> web.Response:
        """GET /vizo/tasks/{task_id}/rollback - 回退操作页"""
        task_id = request.match_info["task_id"]
        try:
            task = await self._load_task_detail(task_id)
            if not task:
                return web.Response(
                    text=SIMPLE_RESULT_PAGE.format(icon="&#x26A0;", result_title="任务不存在",
                                                   message=f"任务 {self._escape_html(task_id)} 不存在"),
                    content_type="text/html", status=404)

            state = task["state"]
            status = state.get("status", "")

            step_display_json = json.dumps(STEP_DISPLAY, ensure_ascii=False)

            if status != "paused":
                content_html = '''<div class="section"><div class="notice">
<p>请先暂停任务再进行回退操作</p>
<button onclick="history.back()" class="btn btn-secondary" style="margin-top:16px;display:inline-block;width:auto;padding:0 24px">返回</button>
</div></div>'''
            else:
                # 构建已完成步骤的 radio 列表
                completed_steps = state.get("completed_steps", [])
                if not completed_steps:
                    content_html = '''<div class="section"><div class="notice">
<p>没有可回退的步骤</p>
<button onclick="history.back()" class="btn btn-secondary" style="margin-top:16px;display:inline-block;width:auto;padding:0 24px">返回</button>
</div></div>'''
                else:
                    radio_html = ""
                    for step_name in completed_steps:
                        name_cn = STEP_DISPLAY.get(step_name, step_name)
                        doc = STEP_TO_DOC.get(step_name, "")
                        if isinstance(doc, list):
                            doc = ", ".join(doc)
                        radio_html += f'''<label class="radio-item">
<input type="radio" name="target_step" value="{self._escape_html(step_name)}">
<span class="radio-label">{self._escape_html(name_cn)}</span>
<span class="radio-doc">{self._escape_html(doc)}</span>
</label>'''

                    content_html = f'''<div class="section">
<h2>↩️ 回退到指定步骤</h2>
<div class="radio-group">{radio_html}</div>
<p class="warning-text">⚠️ 注：回退后该步骤及后续步骤将重新执行，产出文档将被覆盖</p>
<textarea id="feedback" class="feedback-area" placeholder="告诉 AI 重做时需要注意什么..." rows="3"></textarea>
<div class="grid-2">
<button onclick="doRollback()" class="btn btn-primary" id="rollbackBtn" disabled>↩️ 确认回退</button>
<button onclick="history.back()" class="btn btn-secondary">取消</button>
</div>
</div>'''

            html = TASK_ROLLBACK_PAGE.format(
                task_name=self._escape_html(self._resolve_task_name(state, task_id)),
                task_id=task_id,
                step_display_json=step_display_json,
                content_html=content_html,
            )
            return web.Response(text=html, content_type="text/html",
                                headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"})
        except Exception as e:
            self.logger.error(f"handle_task_rollback error: {e}")
            return web.Response(
                text=SIMPLE_RESULT_PAGE.format(icon="&#x26A0;", result_title="加载失败", message=str(e)),
                content_type="text/html", status=500)

    # ==================== 任务控制台：API handler ====================

    async def handle_api_pause(self, request: web.Request) -> web.Response:
        """POST /vizo/api/tasks/{task_id}/pause"""
        if not self._check_api_auth(request):
            return web.json_response({"status": "error", "message": "Unauthorized"}, status=401)
        task_id = request.match_info["task_id"]
        try:
            task = await self._load_task_detail(task_id)
            if not task:
                return web.json_response({"status": "error", "message": "任务不存在"}, status=404)

            status = task["state"].get("status", "")
            if status not in ("running", "in_progress"):
                return web.json_response({"status": "error", "message": "任务状态不允许暂停"}, status=400)

            from lib.control_signals import write_signal
            write_signal(task_id, "pause", source="web")
            return web.json_response({"status": "ok", "message": "暂停信号已发送"})
        except Exception as e:
            self.logger.error(f"handle_api_pause error: {e}")
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def handle_api_resume(self, request: web.Request) -> web.Response:
        """POST /vizo/api/tasks/{task_id}/resume"""
        if not self._check_api_auth(request):
            return web.json_response({"status": "error", "message": "Unauthorized"}, status=401)
        task_id = request.match_info["task_id"]
        try:
            task = await self._load_task_detail(task_id)
            if not task:
                return web.json_response({"status": "error", "message": "任务不存在"}, status=404)

            status = task["state"].get("status", "")
            if status not in ("paused", "failed"):
                return web.json_response({"status": "error", "message": "任务未暂停"}, status=400)

            # 解析可选 feedback
            body = {}
            try:
                body = await request.json()
            except Exception:
                pass
            feedback = body.get("feedback", "").strip() if body else ""

            # 检查编排器是否仍然存活（SIGSTOP 冻结模式 vs 传统暂停）
            task_dir = Path(task["task_dir"])
            frozen_flag = task_dir / "frozen.flag"
            if frozen_flag.exists():
                # 编排器仍在运行，子进程被 SIGSTOP 冻结——写 resume 信号即可
                from lib.control_signals import write_signal
                write_signal(task_id, "resume", source="web", feedback=feedback)
                frozen_flag.unlink(missing_ok=True)
                return web.json_response({"status": "ok", "message": "任务恢复中（进程解冻）"})
            else:
                # 传统暂停（编排器已退出）——spawn 新的 opus --resume
                if feedback:
                    injection_file = task_dir / "user_injection.md"
                    injection_file.write_text(f"# 用户补充信息\n\n{feedback}", encoding="utf-8")

                await self._spawn_opus_resume(task_id)
                return web.json_response({"status": "ok", "message": "任务恢复中"})
        except Exception as e:
            self.logger.error(f"handle_api_resume error: {e}")
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def handle_api_terminate(self, request: web.Request) -> web.Response:
        """POST /vizo/api/tasks/{task_id}/terminate"""
        if not self._check_api_auth(request):
            return web.json_response({"status": "error", "message": "Unauthorized"}, status=401)
        task_id = request.match_info["task_id"]
        try:
            # 解析 rollback 参数（默认 True，向后兼容）
            rollback = False
            if request.content_type == "application/json":
                try:
                    body = await request.json()
                    rollback = body.get("rollback", True)
                except Exception:
                    pass

            task = await self._load_task_detail(task_id)
            if not task:
                return web.json_response({"status": "error", "message": "任务不存在"}, status=404)

            status = task["state"].get("status", "")
            if status in ("running", "in_progress"):
                from lib.control_signals import write_signal
                write_signal(task_id, "terminate", source="web", rollback=rollback)
                return web.json_response({"status": "ok", "message": "终止信号已发送"})
            elif status in ("paused", "failed"):
                cmd = [
                    "python3", str(_PROJECT_ROOT / "opus.py"),
                    "--terminate", "--task-id", task_id,
                    "--yes",
                ]
                if not rollback:
                    cmd.append("--no-rollback")
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode != 0:
                    err_msg = stderr.decode(errors="replace").strip() or "未知错误"
                    self.logger.error(f"terminate 子进程失败 (rc={proc.returncode}): {err_msg}")
                    return web.json_response({"status": "error", "message": f"终止失败: {err_msg}"}, status=500)
                return web.json_response({"status": "ok", "message": "任务已终止"})
            else:
                return web.json_response({"status": "error", "message": "任务已结束"}, status=400)
        except Exception as e:
            self.logger.error(f"handle_api_terminate error: {e}")
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def handle_api_task_output(self, request: web.Request) -> web.Response:
        """GET /vizo/api/tasks/{task_id}/outputs/{filename} — 服务任务产出文件（md→html 渲染）"""
        if not self._check_api_auth(request):
            return web.Response(text="Unauthorized", status=401)
        task_id = request.match_info["task_id"]
        filename = request.match_info["filename"]
        # 安全检查：防止路径穿越
        if ".." in filename or "/" in filename or "\\" in filename:
            return web.Response(text="Invalid filename", status=400)
        # 查找任务目录
        task_dir = None
        output_path = None
        for _, td in (self._web_console._all_tasks_dirs() if self._web_console else []):
            base_dir = td / task_id
            for candidate in (base_dir / filename, base_dir / "outputs" / filename):
                if candidate.is_file():
                    task_dir = base_dir
                    output_path = candidate
                    break
            if output_path is not None:
                break
        if not task_dir:
            # 默认路径
            task_dir = resolve_task_dir(task_id, project_root=_PROJECT_ROOT)
        if output_path is None:
            direct_path = task_dir / filename
            outputs_path = task_dir / "outputs" / filename
            output_path = direct_path if direct_path.is_file() else outputs_path
        if not output_path.is_file():
            return web.Response(text="文件不存在", status=404)
        try:
            content = output_path.read_text("utf-8")
            # .md 文件渲染为 HTML
            if filename.endswith(".md"):
                try:
                    import markdown
                    html_body = markdown.markdown(content, extensions=["tables", "fenced_code"])
                except ImportError:
                    # fallback: 简单的 markdown→html
                    import html as html_mod
                    html_body = "<pre>" + html_mod.escape(content) + "</pre>"
                html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{filename}</title>
<style>html{{background:#1a1a2e;color-scheme:dark;scrollbar-width:thin;scrollbar-color:rgba(123,224,255,.74) transparent}}
body{{max-width:800px;margin:0 auto;padding:20px;font-family:-apple-system,sans-serif;background:#1a1a2e;color:#e0e0e0;line-height:1.6}}
::-webkit-scrollbar{{width:4px;height:4px}}::-webkit-scrollbar-track{{background:transparent}}::-webkit-scrollbar-thumb{{background:rgba(123,224,255,.74);border-radius:4px}}::-webkit-scrollbar-thumb:hover{{background:#7be0ff}}
h1,h2,h3{{color:#a78bfa}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #333;padding:8px;text-align:left}}
th{{background:#2a2a4a}}pre{{background:#0d0d1a;padding:12px;border-radius:6px;overflow-x:auto}}code{{color:#7dd3fc}}
a{{color:#818cf8}}</style></head><body>{html_body}</body></html>"""
                return web.Response(text=html, content_type="text/html")
            else:
                return web.Response(text=content, content_type="text/plain; charset=utf-8")
        except Exception as e:
            self.logger.error(f"handle_api_task_output error: {e}")
            return web.Response(text=str(e), status=500)

    async def handle_api_rollback(self, request: web.Request) -> web.Response:
        """POST /vizo/api/tasks/{task_id}/rollback"""
        if not self._check_api_auth(request):
            return web.json_response({"status": "error", "message": "Unauthorized"}, status=401)
        task_id = request.match_info["task_id"]
        try:
            body = {}
            try:
                body = await request.json()
            except Exception:
                pass

            target_step = body.get("target_step", "").strip() if body else ""
            if not target_step:
                return web.json_response({"status": "error", "message": "缺少 target_step"}, status=400)

            task = await self._load_task_detail(task_id)
            if not task:
                return web.json_response({"status": "error", "message": "任务不存在"}, status=404)

            status = task["state"].get("status", "")
            feedback = body.get("feedback", "").strip() if body else ""

            if status == "running":
                from lib.control_signals import write_signal
                write_signal(task_id, "rollback", source="web", target_step=target_step, feedback=feedback)
                step_cn = STEP_DISPLAY.get(target_step, target_step)
                return web.json_response({"status": "ok", "message": f"回滚信号已发送 → {step_cn}"})
            elif status == "paused":
                if feedback:
                    task_dir = Path(task["task_dir"])
                    injection_file = task_dir / "user_injection.md"
                    injection_file.write_text(f"# 用户补充信息\n\n{feedback}", encoding="utf-8")

                proc = await asyncio.create_subprocess_exec(
                    "python3", str(_PROJECT_ROOT / "opus.py"),
                    "--rollback", "--task-id", task_id, "--step", target_step,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.wait()
                step_cn = STEP_DISPLAY.get(target_step, target_step)
                return web.json_response({"status": "ok", "message": f"已回退到 {step_cn}"})
            else:
                return web.json_response({"status": "error", "message": "任务状态不允许回退"}, status=400)
        except Exception as e:
            self.logger.error(f"handle_api_rollback error: {e}")
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def handle_api_confirm(self, request: web.Request) -> web.Response:
        """POST /vizo/api/tasks/{task_id}/confirm — JSON API for Web Console"""
        task_id = request.match_info["task_id"]

        body = {}
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"status": "error", "message": "Invalid JSON"}, status=400)

        action = body.get("action", "").strip()
        feedback = body.get("feedback", "").strip()

        if action not in ("y", "n", "f", "d", "i", "terminate"):
            return web.json_response(
                {"status": "error", "message": "Invalid action"}, status=400)

        # terminate action: 直接路由到终止逻辑，不走 confirm_bridge
        if action == "terminate":
            rollback = body.get("rollback", False)
            try:
                task = await self._load_task_detail(task_id)
                if not task:
                    return web.json_response(
                        {"status": "error", "message": "任务不存在"}, status=404)
                status = task["state"].get("status", "")
                if status == "running":
                    from lib.control_signals import write_signal
                    write_signal(task_id, "terminate", source="web", rollback=rollback)
                    # 同时响应 confirm_bridge 以解除等待阻塞
                    # agent_exception 场景需发 "terminate" 让 _handle_agent_exception
                    # 走 raise WorkflowError 而非 return "pause"；其他场景发 "cancel" 触发 WorkflowError
                    from lib.confirm_bridge import get_pending, respond as bridge_respond
                    pending = get_pending()
                    for req in pending:
                        req_tid = req.get("task_id", "")
                        ctx_tid = (req.get("context") or {}).get("task_id", "")
                        if task_id in (req_tid, ctx_tid):
                            ctx = req.get("context") or {}
                            bs = ctx.get("button_set", "generic_confirm") if isinstance(ctx, dict) else "generic_confirm"
                            bridge_action = "terminate" if bs == "agent_exception" else "cancel"
                            bridge_respond(req["id"], bridge_action)
                            break
                    return web.json_response(
                        {"status": "ok", "message": "任务已终止"})
                elif status in ("paused", "failed"):
                    cmd = [
                        "python3", str(_PROJECT_ROOT / "opus.py"),
                        "--terminate", "--task-id", task_id,
                        "--yes",
                    ]
                    if not rollback:
                        cmd.append("--no-rollback")
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, stderr = await proc.communicate()
                    if proc.returncode != 0:
                        err_msg = stderr.decode(errors="replace").strip() or "未知错误"
                        self.logger.error(f"terminate(confirm) 子进程失败 (rc={proc.returncode}): {err_msg}")
                        return web.json_response(
                            {"status": "error", "message": f"终止失败: {err_msg}"}, status=500)
                    return web.json_response(
                        {"status": "ok", "message": "任务已终止"})
                else:
                    return web.json_response(
                        {"status": "error", "message": "任务已结束"}, status=400)
            except Exception as e:
                self.logger.error(f"handle_api_confirm terminate error: {e}")
                return web.json_response(
                    {"status": "error", "message": str(e)}, status=500)

        r = await self._get_redis()

        def _decode_pending(raw):
            if raw is None:
                return None
            if isinstance(raw, bytes):
                try:
                    raw = raw.decode("utf-8")
                except UnicodeDecodeError:
                    return None
            if isinstance(raw, str):
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return None
            if isinstance(raw, dict):
                return raw
            return None

        async def _load_web_pending(candidate_request_id: str):
            candidate_request_id = str(candidate_request_id or "").strip()
            if not candidate_request_id:
                return "", None
            raw = await r.get(f"pending_request:{candidate_request_id}")
            data = _decode_pending(raw)
            if not isinstance(data, dict):
                return "", None
            return candidate_request_id, data

        async def _find_web_pending_by_task():
            request_id = str(body.get("request_id", "")).strip()
            found_request_id, pending_data = await _load_web_pending(request_id)
            if pending_data:
                return found_request_id, pending_data
            try:
                async for raw_key in r.scan_iter(match="pending_request:*"):
                    key = raw_key.decode("utf-8") if isinstance(raw_key, bytes) else str(raw_key)
                    found_request_id, pending_data = await _load_web_pending(
                        key.split("pending_request:", 1)[-1]
                    )
                    if not pending_data:
                        continue
                    context = pending_data.get("context") if isinstance(pending_data.get("context"), dict) else {}
                    bound_task_id = str(context.get("task_id") or pending_data.get("task_id") or "").strip()
                    if bound_task_id == task_id:
                        return found_request_id, pending_data
            except Exception as error:
                self.logger.warning("scan pending_request failed for task %s: %s", task_id, error)
            return "", None

        # 从 web pending_request / confirm_bridge 查找匹配 task_id 的待确认请求
        from lib.confirm_bridge import get_pending
        pending = get_pending()
        bridge_target = None
        for req in pending:
            # 匹配 req.task_id 或 context.task_id（后者在 orchestrator 设置了真实 task_id）
            req_tid = req.get("task_id", "")
            ctx_tid = (req.get("context") or {}).get("task_id", "")
            if task_id in (req_tid, ctx_tid):
                bridge_target = req
                break

        web_request_id, web_pending = await _find_web_pending_by_task()
        if not web_request_id and not bridge_target:
            return web.json_response(
                {"status": "error", "message": "无待确认请求"}, status=404)

        # 幂等检查
        request_id = web_request_id or bridge_target["id"]
        existing = await r.get(f"response:{request_id}")
        if existing:
            return web.json_response({"status": "ok", "message": "已处理"})

        # 映射 action → Redis response value
        action_map = {
            "y": "Y",
            "n": "N",
            "f": f"feedback:{feedback}" if feedback else "Y",
            "d": "discussion",
            "i": "interaction_design",
        }
        response_value = action_map.get(action, "Y")
        await r.setex(f"response:{request_id}", 3600, response_value)

        # 同步 bridge 响应文件
        from lib.confirm_bridge import respond as bridge_respond
        bridge_map = {"y": "confirm", "n": "cancel", "d": "discussion", "i": "interaction_design"}
        bridge_request_id = ""
        if isinstance(web_pending, dict):
            bridge_request_id = str(web_pending.get("bridge_request_id") or "").strip()
        if not bridge_request_id and bridge_target:
            bridge_request_id = bridge_target["id"]
        if bridge_request_id:
            if action in bridge_map:
                bridge_respond(bridge_request_id, bridge_map[action])
            elif action == "f" and feedback:
                bridge_respond(bridge_request_id, "feedback", feedback)
            else:
                bridge_respond(bridge_request_id, "confirm")

        msg_map = {
            "y": "已确认", "n": "已取消",
            "f": "意见已提交", "d": "已发起讨论",
        }
        return web.json_response(
            {"status": "ok", "message": msg_map.get(action, "已处理")})

    async def handle_api_progress(self, request: web.Request) -> web.Response:
        """GET /vizo/api/tasks/{task_id}/progress"""
        task_id = request.match_info["task_id"]
        try:
            task_dir = (
                self._web_console._find_task_dir(task_id)
                if self._web_console
                else resolve_task_dir(task_id, project_root=_PROJECT_ROOT)
            )
            progress_file = task_dir / "progress.json"
            if not progress_file.exists():
                return web.json_response({"status": "error", "message": "进度文件不存在"}, status=404)

            data = json.loads(progress_file.read_text("utf-8"))

            # 补充 has_code_changes 字段：基于 git diff 检测实际代码变更
            # 同时用 state.json 的权威字段修正 progress 中可能过时的快照值
            state_file = task_dir / "state.json"
            if state_file.exists():
                try:
                    state = json.loads(state_file.read_text("utf-8"))
                    data["has_code_changes"] = check_task_code_changes(state)
                    # state.json 是 task_type/scale 的权威源，progress.json 可能滞后
                    for key in ("task_type", "scale"):
                        if state.get(key):
                            data[key] = state[key]
                    title = self._resolve_task_name(state, task_id)
                    data["task_name"] = title
                    data["task_title"] = title
                    data.setdefault("task_summary", state.get("task_summary") or title)
                except (json.JSONDecodeError, OSError):
                    data["has_code_changes"] = False
            else:
                data["has_code_changes"] = False

            return web.json_response(data)
        except json.JSONDecodeError:
            return web.json_response({"status": "error", "message": "进度文件格式错误"}, status=500)
        except Exception as e:
            self.logger.error(f"handle_api_progress error: {e}")
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    # ==================== 服务器生命周期 ====================


    # ==================== Agent 模块管理 API ====================

    async def handle_api_agents_list(self, request: web.Request) -> web.Response:
        """GET /vizo/console/api/agents — 列出所有可用 Agent 模块"""
        if not self._check_api_auth(request):
            return web.json_response(
                {"success": False, "data": None, "error": "Unauthorized"}, status=401
            )
        try:
            agents_base = _PROJECT_ROOT / "agents"
            result = []

            # 扫描 _builtin（单层）
            builtin_dir = agents_base / "_builtin"
            if builtin_dir.is_dir():
                for d in sorted(builtin_dir.iterdir()):
                    if not d.is_dir():
                        continue
                    manifest_file = d / "manifest.json"
                    if not manifest_file.exists():
                        continue
                    try:
                        m = json.loads(manifest_file.read_text(encoding="utf-8"))
                        result.append(self._build_agent_summary(m, "_builtin"))
                    except (json.JSONDecodeError, OSError) as e:
                        self.logger.warning("Skip invalid manifest %s: %s", manifest_file, e)

            # 扫描 _user（两层：namespace → module）
            user_dir = agents_base / "_user"
            if user_dir.is_dir():
                for ns_dir in sorted(user_dir.iterdir()):
                    if not ns_dir.is_dir():
                        continue
                    for d in sorted(ns_dir.iterdir()):
                        if not d.is_dir():
                            continue
                        manifest_file = d / "manifest.json"
                        if not manifest_file.exists():
                            continue
                        try:
                            m = json.loads(manifest_file.read_text(encoding="utf-8"))
                            result.append(self._build_agent_summary(m, "_user"))
                        except (json.JSONDecodeError, OSError) as e:
                            self.logger.warning("Skip invalid manifest %s: %s", manifest_file, e)

            return web.json_response({"success": True, "data": result, "error": None})
        except Exception as e:
            self.logger.error(f"handle_api_agents_list error: {e}")
            return web.json_response(
                {"success": False, "data": None, "error": str(e)}, status=500
            )

    @staticmethod
    def _build_agent_summary(manifest: dict, source: str) -> dict:
        """从 manifest 提取模块摘要信息"""
        from lib.agent_creator import _flatten_steps

        workflows_summary = {}
        for wf_id, wf in manifest.get("workflows", {}).items():
            steps = _flatten_steps(wf)
            workflows_summary[wf_id] = {
                "step_count": len(steps),
                "description": wf.get("description", ""),
            }
        return {
            "id": manifest.get("id", ""),
            "name": manifest.get("name", ""),
            "description": manifest.get("description", ""),
            "icon": manifest.get("icon", ""),
            "version": manifest.get("version", "1.0"),
            "author": manifest.get("author", ""),
            "source": source,
            "workflow_count": len(workflows_summary),
            "workflows": workflows_summary,
        }

    async def handle_api_agents_generate(self, request: web.Request) -> web.Response:
        """POST /vizo/console/api/agents/generate — AI 生成模块预览"""
        if not self._check_api_auth(request):
            return web.json_response(
                {"success": False, "data": None, "error": "Unauthorized"}, status=401
            )
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"success": False, "data": None, "error": "无效的 JSON 请求体"},
                status=400,
            )
        raw_desc = body.get("description")
        if "description" not in body:
            return web.json_response(
                {"success": False, "data": None, "error": "缺少必填字段: description"},
                status=400,
            )
        if not isinstance(raw_desc, str):
            return web.json_response(
                {"success": False, "data": None, "error": "description 必须为字符串"},
                status=400,
            )
        description = raw_desc.strip()
        if len(description) < 10:
            return web.json_response(
                {"success": False, "data": None, "error": "描述太简短（<10字符）"},
                status=400,
            )
        if len(description) > 500:
            return web.json_response(
                {"success": False, "data": None, "error": "描述过长（>500字符）"},
                status=400,
            )
        try:
            from lib.config_loader import load_config
            from opus import handle_create_agent

            config = load_config()
            result = await handle_create_agent(
                config, source="web", description=description
            )
            if result and result.get("action") == "error":
                return web.json_response(
                    {"success": False, "data": None, "error": result.get("message", "生成失败")},
                    status=400,
                )
            return web.json_response(
                {"success": True, "data": result, "error": None}
            )
        except Exception as e:
            self.logger.error(f"handle_api_agents_generate error: {e}")
            return web.json_response(
                {"success": False, "data": None, "error": str(e)}, status=500
            )

    async def handle_api_agents_save(self, request: web.Request) -> web.Response:
        """POST /vizo/console/api/agents — 保存模块到磁盘"""
        if not self._check_api_auth(request):
            return web.json_response(
                {"success": False, "data": None, "error": "Unauthorized"}, status=401
            )
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"success": False, "data": None, "error": "无效的 JSON 请求体"},
                status=400,
            )
        manifest = body.get("manifest")
        roles = body.get("roles")
        module_id_override = body.get("module_id")

        if not manifest:
            return web.json_response(
                {"success": False, "data": None, "error": "请求体缺少 manifest 字段"},
                status=400,
            )
        if roles is None:
            return web.json_response(
                {"success": False, "data": None, "error": "请求体缺少 roles 字段"},
                status=400,
            )

        try:
            from lib.agent_creator import (
                validate_manifest,
                check_id_conflict,
                check_module_limit,
                write_module_to_disk,
            )
            from lib.config_loader import load_config

            # 校验 manifest
            validation = validate_manifest(manifest)
            if not validation["valid"]:
                error_msg = "; ".join(validation["errors"])
                return web.json_response(
                    {"success": False, "data": None, "error": error_msg}, status=400
                )

            # 确定 final_id
            final_id = module_id_override or manifest.get("id", "")
            if not final_id:
                return web.json_response(
                    {"success": False, "data": None, "error": "缺少模块 ID"},
                    status=400,
                )

            # 检查 ID 冲突
            conflict = check_id_conflict(final_id)
            if conflict["conflict"]:
                if conflict["conflict_source"] == "_builtin":
                    return web.json_response(
                        {
                            "success": False,
                            "data": None,
                            "error": "模块 ID 与内置模块冲突，请使用 module_id 参数指定新 ID",
                        },
                        status=409,
                    )
                # _user 冲突 → 覆盖更新（允许）

            # 检查数量限制
            config = load_config()
            limit_check = check_module_limit(config)
            if not limit_check["allowed"]:
                return web.json_response(
                    {
                        "success": False,
                        "data": None,
                        "error": f"已达模块上限（{limit_check['max_limit']}个）",
                    },
                    status=400,
                )

            # 写入磁盘
            module_path = write_module_to_disk(manifest, roles, module_id=final_id)

            # 同步到 CLAUDE.md
            try:
                from lib.module_sync import sync_modules_to_claude_md
                sync_modules_to_claude_md(str(_PROJECT_ROOT))
            except Exception:
                pass

            return web.json_response(
                {
                    "success": True,
                    "data": {
                        "action": "saved",
                        "module_id": final_id,
                        "module_path": str(module_path.relative_to(_PROJECT_ROOT)),
                    },
                    "error": None,
                }
            )
        except Exception as e:
            self.logger.error(f"handle_api_agents_save error: {e}")
            return web.json_response(
                {"success": False, "data": None, "error": str(e)}, status=500
            )

    async def handle_api_agents_delete(self, request: web.Request) -> web.Response:
        """DELETE /vizo/console/api/agents/{id} — 删除用户自建模块"""
        if not self._check_api_auth(request):
            return web.json_response(
                {"success": False, "data": None, "error": "Unauthorized"}, status=401
            )
        module_id = request.match_info["id"]

        # 路径穿越防护
        if ".." in module_id or "/" in module_id:
            return web.json_response(
                {"success": False, "data": None, "error": "无效的模块 ID"},
                status=400,
            )

        try:
            import shutil

            agents_base = _PROJECT_ROOT / "agents"

            # 检查是否为 _builtin 模块
            builtin_path = agents_base / "_builtin" / module_id
            if builtin_path.is_dir():
                return web.json_response(
                    {"success": False, "data": None, "error": "内置模块不可删除"},
                    status=403,
                )

            # 在 _user/default/ 中查找
            user_path = agents_base / "_user" / "default" / module_id
            if not user_path.is_dir():
                return web.json_response(
                    {"success": False, "data": None, "error": f"模块不存在: {module_id}"},
                    status=404,
                )

            shutil.rmtree(user_path)

            # 同步到 CLAUDE.md
            try:
                from lib.module_sync import sync_modules_to_claude_md
                sync_modules_to_claude_md(str(_PROJECT_ROOT))
            except Exception:
                pass

            return web.json_response(
                {
                    "success": True,
                    "data": {"module_id": module_id, "action": "deleted"},
                    "error": None,
                }
            )
        except Exception as e:
            self.logger.error(f"handle_api_agents_delete error: {e}")
            return web.json_response(
                {"success": False, "data": None, "error": str(e)}, status=500
            )

    async def handle_api_agents_validate_id(self, request: web.Request) -> web.Response:
        """GET /vizo/console/api/agents/{id}/validate-id — 校验模块 ID 可用性"""
        if not self._check_api_auth(request):
            return web.json_response(
                {"success": False, "data": None, "error": "Unauthorized"}, status=401
            )
        module_id = request.match_info["id"]
        import re
        if not re.match(r'^[a-zA-Z0-9_]+$', module_id):
            return web.json_response({
                "success": True,
                "data": {"valid": False, "conflict": False, "conflict_source": None,
                         "conflict_name": None, "format_error": "只允许字母数字下划线"},
                "error": None,
            })
        try:
            from lib.agent_creator import check_id_conflict
            result = check_id_conflict(module_id)
            return web.json_response({
                "success": True,
                "data": {"valid": not result["conflict"], "conflict": result["conflict"],
                         "conflict_source": result["conflict_source"],
                         "conflict_name": result["conflict_name"], "format_error": None},
                "error": None,
            })
        except Exception as e:
            self.logger.error(f"handle_api_agents_validate_id error: {e}")
            return web.json_response(
                {"success": False, "data": None, "error": str(e)}, status=500
            )

    async def handle_api_models_list(self, request: web.Request) -> web.Response:
        """GET /vizo/console/api/models — 列出可用模型"""
        if not self._check_api_auth(request):
            return web.json_response(
                {"success": False, "data": None, "error": "Unauthorized"}, status=401
            )
        try:
            from lib.config_loader import load_config
            config = load_config()
            ext_models = config.get("external_models", {})
            external = []
            for name, info in ext_models.items():
                if name == "anthropic":
                    continue
                key = info.get("api_key", "")
                configured = bool(key) and not key.startswith("YOUR_")
                external.append({
                    "name": name,
                    "label": info.get("label", name),
                    "configured": configured,
                })
            return web.json_response({
                "success": True,
                "data": {"builtin": ["opus", "sonnet", "haiku"], "external": external},
                "error": None,
            })
        except Exception as e:
            self.logger.error(f"handle_api_models_list error: {e}")
            return web.json_response(
                {"success": False, "data": None, "error": str(e)}, status=500
            )

    async def handle_api_agents_update(self, request: web.Request) -> web.Response:
        """PUT /vizo/console/api/agents/{id} — 更新用户自建模块"""
        if not self._check_api_auth(request):
            return web.json_response(
                {"success": False, "data": None, "error": "Unauthorized"}, status=401
            )
        module_id = request.match_info["id"]
        if ".." in module_id or "/" in module_id:
            return web.json_response(
                {"success": False, "data": None, "error": "无效的模块 ID"}, status=400
            )
        # 仅允许更新 _user 模块
        agents_base = _PROJECT_ROOT / "agents"
        builtin_path = agents_base / "_builtin" / module_id
        if builtin_path.is_dir():
            return web.json_response(
                {"success": False, "data": None, "error": "内置模块不可编辑"}, status=403
            )
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"success": False, "data": None, "error": "无效的 JSON 请求体"}, status=400
            )
        manifest = body.get("manifest")
        roles = body.get("roles")
        if not manifest:
            return web.json_response(
                {"success": False, "data": None, "error": "请求体缺少 manifest 字段"}, status=400
            )
        if roles is None:
            return web.json_response(
                {"success": False, "data": None, "error": "请求体缺少 roles 字段"}, status=400
            )
        try:
            from lib.agent_creator import validate_manifest, write_module_to_disk
            validation = validate_manifest(manifest)
            if not validation["valid"]:
                return web.json_response(
                    {"success": False, "data": None, "error": "; ".join(validation["errors"])},
                    status=400,
                )
            write_module_to_disk(manifest, roles, module_id=module_id)
            try:
                from lib.module_sync import sync_modules_to_claude_md
                sync_modules_to_claude_md(str(_PROJECT_ROOT))
            except Exception:
                pass
            return web.json_response({
                "success": True,
                "data": {"action": "updated", "module_id": module_id},
                "error": None,
            })
        except Exception as e:
            self.logger.error(f"handle_api_agents_update error: {e}")
            return web.json_response(
                {"success": False, "data": None, "error": str(e)}, status=500
            )

    async def run(self, use_tunnel=True):
        """启动完整服务"""
        log_file = _resolve_confirm_server_log_file()
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(name)s] %(message)s",
            datefmt="%H:%M:%S",
            handlers=[
                logging.FileHandler(log_file, encoding="utf-8"),
                logging.StreamHandler(),
            ],
        )

        # Setup Wizard (first_run detection)
        setup_wizard = None
        setup_handler = None
        if self._password_mgr:
            try:
                from lib.config_loader import load_config as _lc
                from lib.settings_handler import SetupWizard, SetupHandler
                _cfg = _lc()
                setup_wizard = SetupWizard(_cfg, self._password_mgr)
                setup_handler = SetupHandler(setup_wizard, self._password_mgr)
            except Exception as e:
                self.logger.warning("SetupWizard init failed: %s", e)

        def _path_prefix(path: str) -> str:
            if path.startswith("/vizo/") or path == "/vizo":
                return "/vizo"
            return ""

        def _versionless_alias_path(path: str) -> str | None:
            if not path.startswith("/vizo/"):
                return None
            return path[len("/vizo"):] or "/"

        def _route_aliases(path: str) -> list[tuple[str, str]]:
            versionless_path = _versionless_alias_path(path)
            if not versionless_path:
                return []
            return [
                (versionless_path, ""),
            ]

        def _rewrite_alias_text(text: str, alias_prefix: str = "") -> str:
            if not text:
                return text
            return text.replace("/vizo/", f"{alias_prefix}/")

        def _rewrite_cookie_path(path: str, alias_prefix: str = "") -> str:
            if path == "/vizo":
                return alias_prefix or "/"
            if path.startswith("/vizo/"):
                suffix = path[len("/vizo"):]
                return f"{alias_prefix}{suffix}" if alias_prefix else (suffix or "/")
            return path

        def _rewrite_set_cookie_header(cookie: str, alias_prefix: str = "") -> str:
            target = alias_prefix or "/"
            cookie = cookie.replace("Path=/vizo/", f"Path={target.rstrip('/')}/", 1)
            cookie = cookie.replace("Path=/vizo;", f"Path={target};", 1)
            if cookie.endswith("Path=/vizo"):
                cookie = cookie[:-8] + f"Path={target}"
            return cookie

        # first_run 中间件：未初始化时拦截 Web Console 路由到引导页
        @web.middleware
        async def first_run_middleware(request, handler):
            prefix = _path_prefix(request.path)
            setup_path = f"{prefix}/console/setup" if prefix else "/console/setup"
            login_paths = {
                "/console/login",
                "/m/login",
                "/vizo/console/login",
                "/vizo/m/login",
            }
            if (
                request.path.startswith("/console/setup")
                or request.path.startswith("/vizo/console/setup")
            ):
                return await handler(request)
            if request.path in login_paths:
                return await handler(request)
            if not any(
                request.path.startswith(prefix)
                for prefix in (
                    "/console",
                    "/m",
                    "/vizo/console",
                    "/vizo/m",
                )
            ):
                return await handler(request)
            if setup_wizard and setup_wizard.is_first_run():
                if request.path.startswith(("/console/api/", "/vizo/console/api/")):
                    return web.json_response({"error": "System not initialized"}, status=403)
                raise web.HTTPFound(setup_path)
            return await handler(request)

        middlewares = [first_run_middleware] if setup_wizard else []
        wc_config = self._load_web_console_config() or {}
        try:
            client_max_size = int(wc_config.get("client_max_size", 64 * 1024 * 1024))
        except (TypeError, ValueError):
            client_max_size = 64 * 1024 * 1024
        app = web.Application(middlewares=middlewares, client_max_size=max(client_max_size, 64 * 1024 * 1024))

        def _rewrite_alias_response(resp: web.StreamResponse, alias_prefix: str = "") -> web.StreamResponse:
            location = resp.headers.get("Location")
            if location:
                resp.headers["Location"] = _rewrite_alias_text(location, alias_prefix)

            swa = resp.headers.get("Service-Worker-Allowed")
            if swa:
                resp.headers["Service-Worker-Allowed"] = _rewrite_alias_text(swa, alias_prefix)

            if hasattr(resp, "cookies"):
                for morsel in resp.cookies.values():
                    cookie_path = morsel["path"] or ""
                    rewritten = _rewrite_cookie_path(cookie_path, alias_prefix)
                    if rewritten != cookie_path:
                        morsel["path"] = rewritten

            cookies = resp.headers.getall("Set-Cookie", [])
            if cookies:
                del resp.headers["Set-Cookie"]
                for cookie in cookies:
                    resp.headers.add("Set-Cookie", _rewrite_set_cookie_header(cookie, alias_prefix))

            if isinstance(resp, web.Response):
                content_type = (resp.content_type or "").lower()
                if content_type.startswith("text/") or content_type in {
                    "application/json",
                    "application/javascript",
                    "application/manifest+json",
                }:
                    try:
                        payload = resp.text
                    except Exception:
                        payload = None
                    if payload:
                        resp.text = _rewrite_alias_text(payload, alias_prefix)
            return resp

        def _wrap_alias_handler(handler, alias_prefix: str = ""):
            async def _wrapped(request):
                try:
                    resp = await handler(request)
                except web.HTTPException as exc:
                    _rewrite_alias_response(exc, alias_prefix)
                    raise
                return _rewrite_alias_response(resp, alias_prefix)
            return _wrapped

        def _add_route(method: str, path: str, handler):
            app.router.add_route(method, path, handler)
            for alias_path, alias_prefix in _route_aliases(path):
                app.router.add_route(method, alias_path, _wrap_alias_handler(handler, alias_prefix))

        def _add_get(path: str, handler):
            app.router.add_get(path, handler)
            for alias_path, alias_prefix in _route_aliases(path):
                app.router.add_get(alias_path, _wrap_alias_handler(handler, alias_prefix))

        def _add_post(path: str, handler):
            _add_route("POST", path, handler)

        def _add_delete(path: str, handler):
            _add_route("DELETE", path, handler)

        def _add_put(path: str, handler):
            _add_route("PUT", path, handler)

        def _add_options(path: str, handler):
            _add_route("OPTIONS", path, handler)

        def _add_static(path: str, directory: str, **kwargs):
            app.router.add_static(path, directory, **kwargs)
            for alias_path, _alias_prefix in _route_aliases(path):
                app.router.add_static(alias_path, directory, **kwargs)

        async def _handle_root(_request):
            raise web.HTTPFound("/vizo/console")

        async def _handle_vizo_root(_request):
            raise web.HTTPFound("/vizo/console")

        app.router.add_get("/", _handle_root)
        app.router.add_get("/favicon.ico", self.handle_favicon)
        app.router.add_get("/vizo", _handle_vizo_root)
        app.router.add_get("/vizo/", _handle_vizo_root)

        _add_get("/vizo/confirm/{request_id}", self.handle_confirm_page)
        _add_post("/vizo/confirm/{request_id}", self.handle_confirm_action)
        _add_get("/vizo/input/{request_id}", self.handle_input_page)
        _add_post("/vizo/input/{request_id}", self.handle_input_action)
        _add_get("/vizo/preview/{preview_id}", self.handle_preview_page)
        _add_get("/vizo/health", self.handle_health)
        _add_get("/{filename}.txt", self.handle_wechat_verify)  # 微信域名验证
        # 企微/Bot 回调路由（仅在企微启用时注册）
        _wecom_on = False
        try:
            with open(str(_PROJECT_ROOT / "config.json")) as _f:
                _wecom_on = json.load(_f).get("wecom", {}).get("enabled", False)
        except Exception:
            pass
        if os.environ.get("WECOM_ENABLED", "").lower() == "true":
            _wecom_on = True

        if _wecom_on:
            _add_get("/wecom/callback", self.handle_wecom_callback)
            _add_post("/wecom/callback", self.handle_wecom_message)
            _add_get("/bot/callback", self.handle_bot_callback)
            _add_post("/bot/callback", self.handle_bot_message)
            logging.info("企微回调路由已注册")
        else:
            logging.info("企微未启用，跳过回调路由注册")
        # 文档服务
        _add_get("/vizo/docs/", self.handle_list_docs)
        _add_get("/vizo/docs/{task_id}/{document}", self.handle_doc_json)
        _add_options("/vizo/docs/{task_id}/{document}", self.handle_cors_options)
        _add_get("/vizo/docs/{task_id}/{document}.html", self.handle_doc_html)
        # 任务控制台 — 页面（rollback 在 task_id 之前注册，避免路由冲突）
        _add_get("/vizo/tasks", self.handle_task_list)
        _add_get("/vizo/api/tasks", self.handle_task_list_json)
        _add_get("/vizo/tasks/{task_id}/rollback", self.handle_task_rollback)
        _add_get("/vizo/tasks/{task_id}", self.handle_task_detail)
        # 任务控制台 — API
        _add_post("/vizo/api/tasks/{task_id}/pause", self.handle_api_pause)
        _add_post("/vizo/api/tasks/{task_id}/resume", self.handle_api_resume)
        _add_post("/vizo/api/tasks/{task_id}/terminate", self.handle_api_terminate)
        _add_post("/vizo/api/tasks/{task_id}/rollback", self.handle_api_rollback)
        _add_get("/vizo/api/tasks/{task_id}/progress", self.handle_api_progress)
        _add_post("/vizo/api/tasks/{task_id}/confirm", self.handle_api_confirm)
        _add_get("/vizo/api/tasks/{task_id}/outputs/{filename}", self.handle_api_task_output)

        # Web Console 路由
        if self._web_console:
            # 静态资源（xterm.js 等）——本地提供避免 CDN 延迟
            static_dir = _LIB_DIR / "static"
            if static_dir.is_dir():
                _add_static("/vizo/static", str(static_dir), show_index=False)
            wc = self._web_console
            from lib.dialogue_console import render_dialogue_workbench_html

            async def _handle_dialogue_agents_workbench(request):
                if not wc._check_auth(request):
                    raise web.HTTPFound("/vizo/console/login")
                return web.Response(
                    text=render_dialogue_workbench_html("agents"),
                    content_type="text/html",
                    headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
                )

            async def _handle_dialogue_settings_workbench(request):
                if not wc._check_auth(request):
                    raise web.HTTPFound("/vizo/console/login")
                section = str(request.query.get("section") or "main-session")
                return web.Response(
                    text=render_dialogue_workbench_html("settings", section=section),
                    content_type="text/html",
                    headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
                )

            _add_get("/vizo/console/login", wc.handle_login_page)
            _add_post("/vizo/console/login", wc.handle_login)
            _add_get("/vizo/console/ws", wc.handle_ws)
            _add_get("/vizo/console/api/sessions", wc.handle_list_sessions)
            _add_post("/vizo/console/api/sessions", wc.handle_create_session)
            _add_delete("/vizo/console/api/sessions/{session_id}", wc.handle_delete_session)
            _add_post("/vizo/console/api/sessions/{session_id}/close", wc.handle_close_session)
            _add_post("/vizo/console/api/sessions/{session_id}/attachments", wc.handle_post_session_attachment)
            _add_post("/vizo/console/api/sessions/{session_id}/messages", wc.handle_post_session_message)
            _add_post("/vizo/console/api/sessions/{session_id}/interrupt", wc.handle_interrupt_session_turn)
            _add_delete("/vizo/console/api/sessions/{session_id}/pending/{queue_id}", wc.handle_delete_pending_session_input)
            _add_post("/vizo/console/api/sessions/{session_id}/pending/{queue_id}/send-now", wc.handle_send_pending_session_input_now)
            _add_get("/vizo/console/api/sessions/{session_id}/events", wc.handle_get_session_events)
            _add_get("/vizo/console/api/sessions/{session_id}/events/ws", wc.handle_session_events_ws)
            _add_get("/vizo/console/api/sessions/{session_id}/logs", wc.handle_get_session_logs)
            _add_get("/vizo/console/api/sessions/{session_id}/images", wc.handle_get_session_images)
            _add_get("/vizo/console/api/sessions/{session_id}/images/{image_id}", wc.handle_get_session_image)
            _add_post("/vizo/console/api/sessions/{session_id}/model", wc.handle_set_session_model)
            _add_post("/vizo/console/api/sessions/{session_id}/interaction", wc.handle_post_session_interaction)
            _add_post("/vizo/console/api/sessions/{session_id}/connection", wc.handle_set_session_connection)
            _add_get("/vizo/console/api/legacy/sessions", wc.handle_legacy_list_sessions)
            _add_post("/vizo/console/api/legacy/sessions", wc.handle_legacy_create_session)
            _add_delete("/vizo/console/api/legacy/sessions/{session_id}", wc.handle_legacy_delete_session)
            _add_get("/vizo/console/api/dialogue/events", wc.handle_list_dialogue_events)
            _add_post("/vizo/console/api/dialogue/events", wc.handle_add_dialogue_event)
            _add_get("/vizo/console/api/legacy/dialogue/events", wc.handle_list_dialogue_events)
            _add_post("/vizo/console/api/legacy/dialogue/events", wc.handle_add_dialogue_event)
            _add_get("/vizo/console/api/opus/current", wc.handle_opus_current)
            _add_get("/vizo/console/api/opus/logs", wc.handle_opus_logs)
            _add_get("/vizo/console/api/opus/subtask", wc.handle_opus_subtask)
            _add_get("/vizo/console/api/projects", wc.handle_list_projects)
            _add_post("/vizo/console/api/projects", wc.handle_create_project)
            _add_delete("/vizo/console/api/projects/{name}", wc.handle_delete_project)
            _add_get("/vizo/console/api/projects/{name}/tasks", wc.handle_project_tasks)
            _add_get("/vizo/console/api/tasks", wc.handle_all_tasks)
            _add_get("/vizo/console/api/projects/{name}/files", wc.handle_project_files)
            _add_post("/vizo/console/api/settings/password", wc.handle_settings_password)
            _add_get("/vizo/console/api/settings/models", wc.handle_settings_models_get)
            _add_post("/vizo/console/api/settings/models", wc.handle_settings_models_post)
            _add_get("/vizo/console/api/settings/apikey", wc.handle_settings_apikey_get)
            _add_post("/vizo/console/api/settings/apikey", wc.handle_settings_apikey_post)
            _add_get("/vizo/console/api/settings/wecom", wc.handle_settings_wecom_get)
            _add_post("/vizo/console/api/settings/wecom", wc.handle_settings_wecom_post)
            _add_get("/vizo/console/api/settings/domain", wc.handle_settings_domain_get)
            _add_post("/vizo/console/api/settings/domain", wc.handle_settings_domain_post)
            _add_get("/vizo/console/api/settings/external-models", wc.handle_settings_ext_models_get)
            _add_post("/vizo/console/api/settings/external-models", wc.handle_settings_ext_models_post)
            _add_route("*", "/vizo/console/api/runtime/diagnostics", wc.handle_runtime_diagnostics)
            _add_post("/vizo/console/api/settings/test-connection", wc.handle_test_connection)
            _add_route("*", "/vizo/console/api/settings/connections", wc.handle_saved_connections)
            _add_post("/vizo/console/api/settings/connections/{connection_id}/auth/login", wc.handle_saved_connection_auth_login)
            _add_get("/vizo/console/api/settings/connections/{connection_id}/auth/status", wc.handle_saved_connection_auth_status)
            _add_post("/vizo/console/api/settings/connections/{connection_id}/auth/logout", wc.handle_saved_connection_auth_logout)
            _add_get("/vizo/console/api/settings/mcp-services", wc.handle_settings_mcp_services_get)
            _add_post("/vizo/console/api/settings/mcp-services/state", wc.handle_settings_mcp_services_state)
            _add_post("/vizo/console/api/settings/mcp-services/repair", wc.handle_settings_mcp_services_repair)
            _add_post("/vizo/console/api/settings/mcp-services/import", wc.handle_settings_mcp_services_import)
            _add_post("/vizo/console/api/settings/mcp-services/manual", wc.handle_settings_mcp_services_manual)
            _add_get("/vizo/console/api/settings/mcp-permissions", wc.handle_settings_mcp_perms_get)
            _add_post("/vizo/console/api/settings/mcp-permissions", wc.handle_settings_mcp_perms_post)
            _add_get("/vizo/console/api/chrome-status", wc.handle_chrome_status)
            # Agent 模块管理 API（Phase 3）
            _add_get("/vizo/console/api/agents", self.handle_api_agents_list)
            _add_post("/vizo/console/api/agents/generate", self.handle_api_agents_generate)
            _add_post("/vizo/console/api/agents", self.handle_api_agents_save)
            _add_delete("/vizo/console/api/agents/{id}", self.handle_api_agents_delete)
            _add_get("/vizo/console/api/agents/{id}/validate-id", self.handle_api_agents_validate_id)
            _add_put("/vizo/console/api/agents/{id}", self.handle_api_agents_update)
            _add_get("/vizo/console/api/models", self.handle_api_models_list)
            # Setup Wizard 路由（first_run 时可用）
            if setup_handler:
                _add_get("/vizo/console/setup", setup_handler.handle_setup_page)
                _add_post("/vizo/console/setup/password", setup_handler.handle_setup_password)
                _add_post("/vizo/console/setup/apikey", setup_handler.handle_setup_apikey)
                _add_post("/vizo/console/setup/complete", setup_handler.handle_setup_complete)
                self.logger.info("Setup Wizard routes registered")
            _add_get("/vizo/console/dialogue/agents", _handle_dialogue_agents_workbench)
            _add_get("/vizo/console/dialogue/settings", _handle_dialogue_settings_workbench)
            _add_get("/vizo/console/dialogue", wc.handle_dialogue_console)
            _add_get("/vizo/testing/fixtures/dialogue", wc.handle_dialogue_fixture)
            # Console SPA 放最后（避免与子路由冲突）
            _add_get("/vizo/console", wc.handle_console)
            self.logger.info("Web Console routes registered")

        # Mobile Console 路由（依赖 Web Console 基础设施）
        self._mobile_console = None
        if self._web_console:
            try:
                from lib.mobile_console import MobileConsoleHandler
                self._mobile_console = MobileConsoleHandler(
                    self._load_web_console_config() or {}, password_manager=self._password_mgr
                )
            except Exception as e:
                self.logger.error("MobileConsoleHandler init failed: %s", e)
        if self._mobile_console:
            mc = self._mobile_console
            _add_get("/vizo/m/manifest.json", mc.handle_pwa_manifest)
            _add_get("/vizo/m/sw.js", mc.handle_pwa_sw)
            _add_get("/vizo/m/login", mc.handle_login_page)
            _add_post("/vizo/m/login", mc.handle_login)
            _add_get("/vizo/m", mc.handle_console)
            self.logger.info("Mobile Console routes registered")

        # Chrome Bridge 路由
        if self._chrome_bridge:
            cb = self._chrome_bridge
            _add_get("/vizo/chrome/ws", cb.handle_chrome_ws)
            _add_post("/vizo/chrome/mcp", cb.handle_mcp_request)
            _add_delete("/vizo/chrome/mcp", cb.handle_mcp_delete)
            _add_get("/vizo/chrome/health", cb.handle_health)
            _add_get("/vizo/chrome/connect", cb.handle_connect_page)
            _add_get("/vizo/chrome/extension.zip", cb.handle_extension_download)
            self.logger.info("Chrome Bridge routes registered")

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()

        self.logger.info(f"确认服务已启动 http://{self.host}:{self.port}")

        # 启动 PTYManager
        if self._pty_manager:
            await self._pty_manager.startup()
            self.logger.info("PTYManager started")

        # 启动 Redis PubSub 订阅（P1: 转发 opus:progress 事件到 WebSocket）
        if self._web_console:
            self._pubsub_task = asyncio.create_task(self._subscribe_opus_progress())

        # 启动孤儿任务扫描器（编排器被杀后自动修复 running → failed）
        self._orphan_scanner_task = asyncio.create_task(self._orphan_task_scanner())

        # 启动 Tunnel
        if use_tunnel:
            url = await self.start_tunnel()
            if url:
                print(f"\n  公网地址: {url}")
                print(f"  确认页面: {url}/vizo/confirm/{{request_id}}")
                print(f"  健康检查: {url}/vizo/health\n")
            else:
                print("\n  Tunnel 启动失败，仅本地可访问\n")

        # 等待退出信号
        stop_event = asyncio.Event()

        def _signal_handler():
            stop_event.set()

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _signal_handler)

        print("  按 Ctrl+C 停止服务\n")
        await stop_event.wait()

        # 清理
        self.logger.info("正在停止...")
        if self._pubsub_task:
            self._pubsub_task.cancel()
            try:
                await self._pubsub_task
            except asyncio.CancelledError:
                pass
        if self._orphan_scanner_task:
            self._orphan_scanner_task.cancel()
            try:
                await self._orphan_scanner_task
            except asyncio.CancelledError:
                pass
        if self._pty_manager:
            await self._pty_manager.shutdown()
        if self._chrome_bridge:
            await self._chrome_bridge.close()
        await self.stop_tunnel()
        await runner.cleanup()
        if self._redis:
            await self._redis.close()
        self.logger.info("已停止")

    async def _subscribe_opus_progress(self):
        """Subscribe to opus:progress:* Redis PubSub and forward to WebSocket clients.
        Automatically reconnects on connection loss with exponential backoff.
        """
        retry_delay = 1  # start with 1 second
        max_retry_delay = 30  # cap at 30 seconds

        while True:
            try:
                redis = await self._get_redis()
                pubsub = redis.pubsub()
                await pubsub.psubscribe("opus:progress:*")
                self.logger.info("Subscribed to opus:progress:* PubSub")
                retry_delay = 1  # reset on successful connection

                async for message in pubsub.listen():
                    if message["type"] != "pmessage":
                        continue
                    try:
                        data = json.loads(message["data"])
                        # Forward to all connected WebSocket sessions
                        if self._pty_manager:
                            for session in self._pty_manager._sessions.values():
                                if session.ws:
                                    try:
                                        await session.ws.send_json({
                                            "type": "opus_event",
                                            "data": data,
                                        })
                                    except Exception:
                                        pass
                    except (json.JSONDecodeError, TypeError):
                        pass

                # listen() exited without error — connection lost
                self.logger.warning("PubSub listen() ended, reconnecting in %ds...", retry_delay)

            except asyncio.CancelledError:
                return
            except Exception as e:
                self.logger.error("PubSub subscriber error: %s, reconnecting in %ds...", e, retry_delay)

            # Wait before reconnecting
            try:
                await asyncio.sleep(retry_delay)
            except asyncio.CancelledError:
                return
            retry_delay = min(retry_delay * 2, max_retry_delay)

    async def _orphan_task_scanner(self):
        """定期扫描兼容任务目录，修复编排器崩溃后遗留的 running 状态任务。

        检测逻辑：status == "running" 但 .lock 文件未被占用 → 编排器已死 → 标记 failed。
        """
        import fcntl

        SCAN_INTERVAL = 30  # seconds

        # 首次启动延迟 10 秒（等编排器充分启动，避免误判刚启动的任务）
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            return

        while True:
            try:
                if self._web_console:
                    tasks_dirs = [td for _, td in self._web_console._all_tasks_dirs()]
                else:
                    tasks_dirs = list(iter_storage_dirs("tasks", project_root=_PROJECT_ROOT))
                for tasks_dir in tasks_dirs:
                    for task_dir in tasks_dir.iterdir():
                        if not task_dir.is_dir():
                            continue
                        try:
                            await self._check_and_fix_orphan(task_dir, fcntl)
                        except Exception as e:
                            self.logger.debug("orphan scan skip %s: %s", task_dir.name, e)
            except asyncio.CancelledError:
                return
            except Exception as e:
                self.logger.error("orphan scanner error: %s", e)

            try:
                await asyncio.sleep(SCAN_INTERVAL)
            except asyncio.CancelledError:
                return

    async def _check_and_fix_orphan(self, task_dir: Path, fcntl_mod):
        """检查单个任务是否为孤儿，如果是则修复状态。"""
        state_file = task_dir / "state.json"
        if not state_file.exists():
            return

        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return

        if state.get("status") != "running":
            return

        # 检查 .lock 文件是否被占用
        lock_file = task_dir / ".lock"
        if not lock_file.exists():
            if self._should_defer_lockless_hub_orphan_check(task_dir, state):
                return
            # 无锁文件 → 编排器从未正常获取锁，视为孤儿
            pass
        else:
            try:
                fd = open(lock_file, "r")
                try:
                    fcntl_mod.flock(fd, fcntl_mod.LOCK_EX | fcntl_mod.LOCK_NB)
                    # 成功获取锁 → 原进程已死，释放锁
                    fcntl_mod.flock(fd, fcntl_mod.LOCK_UN)
                finally:
                    fd.close()
            except (BlockingIOError, OSError):
                # 锁被占用 → 编排器仍活着，跳过
                return

        # 确认为孤儿任务，修复状态
        task_id = task_dir.name
        now = datetime.now().isoformat(timespec="seconds")
        completed_steps = state.get("completed_steps") or []
        if self._is_agent_hub_task_state(task_dir, state) and not completed_steps:
            failure_reason = "后台 runner 进程不存在，未产生步骤产出。"
        else:
            failure_reason = "编排器进程异常退出。"
        self.logger.warning("检测到孤儿任务: %s (running → failed)", task_id)

        # 修复 state.json
        state["status"] = "failed"
        state["failed_at"] = now
        state["last_error"] = failure_reason
        state["error_code"] = "orphan_runner_exit"
        state_file.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        try:
            live_log = task_dir / "logs" / "live.log"
            live_log.parent.mkdir(parents=True, exist_ok=True)
            live_ts = datetime.now().strftime("%H:%M:%S")
            with live_log.open("a", encoding="utf-8") as log_file:
                log_file.write(f"\n[{live_ts}] ❌ 任务已标记失败：{failure_reason}\n")
        except Exception:
            pass

        # 修复 progress.json
        progress_file = task_dir / "progress.json"
        if progress_file.exists():
            try:
                progress = json.loads(progress_file.read_text(encoding="utf-8"))
                if progress.get("status") == "running":
                    progress["status"] = "failed"
                    progress["updated_at"] = now
                    for step in progress.get("steps", []):
                        if step.get("status") == "running":
                            step["status"] = "error"
                            step["completed_at"] = now
                            step.setdefault("error", failure_reason)
                    progress_file.write_text(
                        json.dumps(progress, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
            except (json.JSONDecodeError, OSError):
                pass

        # 通过 Redis 通知前端刷新
        try:
            redis = await self._get_redis()
            await redis.publish(
                f"opus:progress:{task_id}",
                json.dumps({
                    "task_id": task_id,
                    "status": "failed",
                    "event": "orphan_fixed",
                    "message": f"{failure_reason}任务已标记失败",
                    "updated_at": now,
                }, ensure_ascii=False),
            )
        except Exception as e:
            self.logger.debug("orphan fix redis notify failed: %s", e)

    def _should_defer_lockless_hub_orphan_check(self, task_dir: Path, state: dict) -> bool:
        """Avoid false positives while AgentHub tasks are still starting.

        AgentHub tasks historically did not create a .lock file, so a freshly
        created hub task without a lock is ambiguous for a short window.
        """
        if not self._is_agent_hub_task_state(task_dir, state):
            return False

        runner_pid = state.get("runner_pid")
        if runner_pid and self._is_task_runner_process_alive(runner_pid):
            return True

        age = self._task_age_seconds(state)
        return age is not None and age < 120

    @staticmethod
    def _is_agent_hub_task_state(task_dir: Path, state: dict) -> bool:
        return (
            task_dir.name.startswith("hub-")
            or bool(state.get("module_id") and state.get("workflow_id"))
        )

    @staticmethod
    def _task_age_seconds(state: dict) -> float | None:
        created_at = str(state.get("created_at") or "").strip()
        if not created_at:
            return None
        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        now = datetime.now(created.tzinfo) if created.tzinfo else datetime.now()
        return max(0.0, (now - created).total_seconds())

    @staticmethod
    def _is_task_runner_process_alive(pid_value) -> bool:
        try:
            pid = int(pid_value)
        except (TypeError, ValueError):
            return False
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

        cmdline_path = Path(f"/proc/{pid}/cmdline")
        try:
            cmdline = cmdline_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return True
        return "opus.py" in cmdline or "vizo.py" in cmdline


# ==================== 进程管理 ====================


def _resolve_confirm_server_pid_file() -> str:
    """返回 confirm_server 可写的 PID 文件路径。"""
    return str(resolve_confirm_server_pid_file(_PROJECT_ROOT))


PID_FILE = _resolve_confirm_server_pid_file()


def write_pid():
    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def read_pid() -> Optional[int]:
    try:
        with open(PID_FILE) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def is_running() -> bool:
    pid = read_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # PID 被其他用户的进程复用，不是我们的服务
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        return False


def _load_confirm_server_runtime_config() -> tuple[str, int]:
    """Resolve configured host/port for status checks."""
    host = "0.0.0.0"
    port = DEFAULT_CONFIRM_SERVER_PORT
    try:
        from lib.config_loader import load_config

        config = load_config()
        cs_config = config.get("confirm_server", {})
        host = cs_config.get("host", host)
        port = int(cs_config.get("port", port))
    except Exception:
        pass
    return host, port


def _connect_host_for_status(host: str) -> str:
    if host in ("", "0.0.0.0", "::"):
        return "127.0.0.1"
    return host


def _is_port_listening(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection(
            (_connect_host_for_status(host), int(port)),
            timeout=timeout,
        ):
            return True
    except OSError:
        return False


def _probe_confirm_server_health(host: str, port: int,
                                 timeout: float = 0.8) -> dict:
    url = f"http://{_connect_host_for_status(host)}:{int(port)}/vizo/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read(4096).decode("utf-8", errors="replace")
            payload = json.loads(body) if body else {}
            return {
                "ok": response.status == 200
                and isinstance(payload, dict)
                and payload.get("status") == "ok",
                "payload": payload,
                "error": "",
            }
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as e:
        return {"ok": False, "payload": {}, "error": str(e)}


def cmd_start(args):
    """启动服务"""
    if is_running():
        pid = read_pid()
        print(f"确认服务已在运行中 (PID: {pid})")
        return

    # 读取配置（支持环境变量覆盖）
    from lib.config_loader import load_config

    config = load_config()

    cs_config = config.get("confirm_server", {})
    redis_config = config.get("redis", {})

    host = cs_config.get("host", "0.0.0.0")
    port = args.port or cs_config.get("port", DEFAULT_CONFIRM_SERVER_PORT)
    use_tunnel = cs_config.get("use_cloudflare_tunnel", True)

    if args.foreground:
        # 前台运行
        write_pid()
        server = ConfirmServer(
            host=host, port=port,
            redis_host=redis_config.get("host", "127.0.0.1"),
            redis_port=redis_config.get("port", 6380)
        )
        try:
            asyncio.run(server.run(use_tunnel=use_tunnel))
        finally:
            if os.path.exists(PID_FILE):
                os.remove(PID_FILE)
    else:
        # 后台运行
        pid = os.fork()
        if pid > 0:
            # 父进程
            print(f"确认服务已启动 (PID: {pid})")
            print(f"监听: http://{host}:{port}")
            print(f"日志: nohup 模式，使用 opus3-confirm status 查看")
            sys.exit(0)

        # 子进程
        os.setsid()
        write_pid()

        # 重定向输出到日志
        log_file = _resolve_confirm_server_log_file()

        sys.stdout = open(log_file, "a")
        sys.stderr = sys.stdout

        server = ConfirmServer(
            host=host, port=port,
            redis_host=redis_config.get("host", "127.0.0.1"),
            redis_port=redis_config.get("port", 6380)
        )
        try:
            asyncio.run(server.run(use_tunnel=use_tunnel))
        finally:
            if os.path.exists(PID_FILE):
                os.remove(PID_FILE)


def cmd_stop(args):
    """停止服务"""
    pid = read_pid()
    if pid is None or not is_running():
        print("确认服务未运行")
        return

    os.kill(pid, signal.SIGTERM)
    print(f"已发送停止信号 (PID: {pid})")

    # 等待退出
    import time
    for _ in range(10):
        if not is_running():
            break
        time.sleep(0.5)

    if is_running():
        os.kill(pid, signal.SIGKILL)
        print("强制终止")

    if os.path.exists(PID_FILE):
        os.remove(PID_FILE)
    print("确认服务已停止")


def cmd_status(args):
    """查看状态"""
    pid = read_pid()
    running = is_running()
    host, configured_port = _load_confirm_server_runtime_config()
    port = args.port or configured_port
    port_listening = _is_port_listening(host, port)
    health = _probe_confirm_server_health(host, port) if port_listening else {
        "ok": False,
        "payload": {},
        "error": "",
    }

    if running:
        status_label = "运行中"
    elif health["ok"]:
        status_label = "运行中（PID 文件缺失或过期）"
    elif port_listening:
        status_label = "端口已占用（健康检查未通过）"
    else:
        status_label = "未运行"

    print(f"确认服务: {status_label}", end="")
    if running:
        print(f" (PID: {pid})")
    else:
        print()
    print(f"监听检查: http://{_connect_host_for_status(host)}:{port} "
          f"{'已占用' if port_listening else '未监听'}")
    if port_listening:
        print(f"健康检查: {'正常' if health['ok'] else '异常'}")

    # 检查 Redis 中的 tunnel URL
    try:
        import redis as sync_redis
        r = sync_redis.Redis(host="127.0.0.1", port=6380, decode_responses=True)
        url = r.get("confirm_server:tunnel_url")
        if url:
            print(f"公网地址: {url}")
        r.close()
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="Vizo Web 确认服务")
    parser.add_argument("command", nargs="?", default="status",
                        choices=["start", "stop", "status"],
                        help="start/stop/status")
    parser.add_argument("-f", "--foreground", action="store_true",
                        help="前台运行")
    parser.add_argument("-p", "--port", type=int, default=None,
                        help="监听端口")
    args = parser.parse_args()

    if args.command == "start":
        cmd_start(args)
    elif args.command == "stop":
        cmd_stop(args)
    else:
        cmd_status(args)


if __name__ == "__main__":
    main()
