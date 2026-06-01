"""
Web Console handler for Vizo.

Provides browser-based Claude Code terminal access via:
- Token authentication (Cookie-based)
- xterm.js SPA frontend
- WebSocket ↔ PTY bridge
- Session CRUD API
- Vizo task status API (P1)
"""

import hashlib
import json
import logging
import os
import asyncio
import inspect
import re
from datetime import datetime, timezone
from typing import Callable

from agent_runner import AgentError
from aiohttp import web, WSMsgType
from pathlib import Path
from lib.config_loader import load_config
from lib.dialogue_console import render_dialogue_console_html
from lib.paths import (
    iter_storage_dirs,
    iter_task_dirs,
    task_dir as resolve_task_dir,
    write_data_path,
)
from lib.project_identity import normalize_mainline_project_path, normalize_runtime_path
from lib.runtime.diagnostics import build_runtime_diagnostics_snapshot
from lib.runtime.sessions.controller import MainSessionController
from lib.project_bootstrap import bootstrap_serena_project
from lib.task_titles import summarize_task_title
from state_manager import check_task_code_changes

_LIB_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _LIB_DIR.parent
_HOST_MAINLINE_ROOT = "/opt/vizo-next"
_CONTAINER_MAINLINE_ROOT = "/app"


logger = logging.getLogger("web_console")


# ============================================================
# Login Page HTML
# ============================================================
LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Vizo Web Console - Login</title>
<script>
(function(){try{var t=localStorage.getItem('opus_theme');
if(t==='light')document.documentElement.setAttribute('data-theme','light');
}catch(e){}})();
</script>
<style>
:root {
  --bg-primary: #0a0e17;
  --bg-secondary: #111827;
  --bg-input: #0f1923;
  --text-primary: #e2e8f0;
  --text-muted: #64748b;
  --accent: #38bdf8;
  --purple: #a78bfa;
  --red: #ef4444;
  --border: #1e3a5f;
  --font-size-2xs: 10px;
  --font-size-xs: 11px;
  --font-size-sm: 12px;
  --font-size-md: 13px;
  --font-size-lg: 14px;
  --font-size-xl: 16px;
  --font-size-2xl: 20px;
  --line-height-tight: 1.35;
  --line-height-base: 1.5;
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 20px;
  --space-6: 24px;
  --radius-2xs: 3px;
  --radius-xs: 4px;
  --radius-sm: 6px;
  --radius: 8px;
  --radius-lg: 12px;
  --radius-xl: 16px;
  --radius-pill: 999px;
  --shadow: 0 4px 24px rgba(0,0,0,0.4);
  --transition: 0.25s cubic-bezier(0.4, 0, 0.2, 1);
}
[data-theme="light"] {
  --bg-primary: #ffffff;
  --bg-secondary: #f5f5f7;
  --bg-input: #f0f0f5;
  --text-primary: #1d1d1f;
  --text-muted: #aeaeb2;
  --accent: #0071e3;
  --purple: #7c3aed;
  --red: #ef4444;
  --border: rgba(0,0,0,0.12);
  --shadow: 0 2px 16px rgba(0,0,0,0.08);
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  background: var(--bg-primary);
  color: var(--text-primary);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans SC", sans-serif;
  font-size: var(--font-size-md);
  line-height: var(--line-height-base);
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
}
.auth-box {
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: var(--radius-xl);
  padding: calc(var(--space-6) + var(--space-2));
  width: 400px;
  text-align: center;
  box-shadow: var(--shadow);
}
.auth-logo {
  font-size: var(--font-size-2xl);
  font-weight: 800;
  background: linear-gradient(135deg, var(--accent), var(--purple));
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  margin-bottom: 0.5rem;
}
.auth-subtitle { color: var(--text-muted); margin-bottom: 2rem; font-size: var(--font-size-sm); }
.auth-input {
  width: 100%; padding: 0.8rem 1rem;
  background: var(--bg-input); border: 1px solid var(--border);
  border-radius: var(--radius); color: var(--text-primary);
  font-size: var(--font-size-lg); outline: none; transition: border-color var(--transition);
}
.auth-input:focus { border-color: var(--accent); }
.auth-input::placeholder { color: var(--text-muted); }
.auth-btn {
  width: 100%; padding: 0.8rem;
  background: linear-gradient(135deg, #0369a1, #0284c7);
  border: none; border-radius: var(--radius);
  color: white; font-size: var(--font-size-lg); font-weight: 600;
  cursor: pointer; margin-top: 1rem;
  transition: all var(--transition);
}
.auth-btn:hover { background: linear-gradient(135deg, #0284c7, #0ea5e9); transform: translateY(-1px); }
.auth-error { color: var(--red); font-size: var(--font-size-sm); margin-top: 0.8rem; display: {{error_display}}; }
</style>
</head>
<body>
<div class="auth-box">
  <div class="auth-logo">维造 Vizo</div>
  <div class="auth-subtitle">Web Console &mdash; 新架构隔离环境</div>
  <form method="POST" action="/vizo/console/login">
    <input type="password" class="auth-input" name="token" placeholder="请输入密码..." autocomplete="off" autofocus>
    <button type="submit" class="auth-btn">登录</button>
  </form>
  <div class="auth-error">{{error_message}}</div>
</div>
</body>
</html>"""


# ============================================================
# Main SPA HTML (xterm.js + sidebar + toolbar + live panel)
# ============================================================
WEB_CONSOLE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Vizo Web Console</title>
<script>
(function(){try{var t=localStorage.getItem('opus_theme');
if(t==='light')document.documentElement.setAttribute('data-theme','light');
}catch(e){}})();
</script>
<link rel="stylesheet" href="/vizo/static/xterm/xterm.css">
<link rel="stylesheet" href="/vizo/static/vendor/material-symbols.css">
<script src="/vizo/static/xterm/xterm.js"></script>
<script src="/vizo/static/xterm/addon-fit.js"></script>
<script src="/vizo/static/xterm/addon-web-links.js"></script>
<script src="/vizo/static/xterm/addon-webgl.js"></script>
<style>
:root {
  --bg-primary: #0a0e17;
  --bg-secondary: #111827;
  --bg-tertiary: #1a2332;
  --bg-card: #1e293b;
  --bg-hover: #263348;
  --bg-input: #0f1923;
  --text-primary: #e2e8f0;
  --text-secondary: #94a3b8;
  --text-muted: #64748b;
  --accent: #38bdf8;
  --accent-dim: #0c4a6e;
  --green: #22c55e;
  --green-dim: #064e3b;
  --yellow: #eab308;
  --yellow-dim: #713f12;
  --red: #ef4444;
  --red-dim: #7f1d1d;
  --orange: #f97316;
  --purple: #a78bfa;
  --pink: #f472b6;
  --border: #1e3a5f;
  --border-subtle: #1a2a3e;
  --font-size-2xs: 10px;
  --font-size-xs: 11px;
  --font-size-sm: 12px;
  --font-size-md: 13px;
  --font-size-lg: 14px;
  --font-size-xl: 16px;
  --font-size-2xl: 20px;
  --line-height-tight: 1.35;
  --line-height-base: 1.5;
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 20px;
  --space-6: 24px;
  --space-8: 32px;
  --radius-2xs: 3px;
  --radius-xs: 4px;
  --radius-sm: 6px;
  --radius: 8px;
  --radius-lg: 12px;
  --radius-xl: 16px;
  --radius-pill: 999px;
  --shadow: 0 4px 24px rgba(0,0,0,0.4);
  --toolbar-h: 48px;
  --sidebar-w: 260px;
  --panel-w: 720px;
  --transition: 0.25s cubic-bezier(0.4, 0, 0.2, 1);
}
[data-theme="light"] {
  --bg-primary: #ffffff;
  --bg-secondary: #f5f5f7;
  --bg-tertiary: #ebebf0;
  --bg-card: #ffffff;
  --bg-hover: #e5e5ea;
  --bg-input: #f0f0f5;
  --text-primary: #1d1d1f;
  --text-secondary: #6e6e73;
  --text-muted: #aeaeb2;
  --accent: #0071e3;
  --accent-dim: rgba(0,113,227,0.12);
  --green-dim: rgba(34,197,94,0.15);
  --yellow-dim: rgba(234,179,8,0.15);
  --red-dim: rgba(239,68,68,0.12);
  --purple: #7c3aed;
  --pink: #ec4899;
  --border: rgba(0,0,0,0.12);
  --border-subtle: rgba(0,0,0,0.06);
  --shadow: 0 2px 16px rgba(0,0,0,0.08);
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { background: var(--bg-primary); color: var(--text-primary); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans SC", sans-serif; font-size: var(--font-size-md); line-height: var(--line-height-base); height: 100vh; overflow: hidden; }

/* Layout */
#app { display: flex; height: 100vh; }

/* Sidebar */
.sidebar {
  width: var(--sidebar-w); background: var(--bg-secondary);
  border-right: 1px solid var(--border-subtle);
  display: flex; flex-direction: column; flex-shrink: 0;
  transition: margin-left var(--transition);
}
.sidebar.collapsed { margin-left: calc(var(--sidebar-w) * -1); }
.sidebar-header {
  padding: 1rem 1.2rem; border-bottom: 1px solid var(--border-subtle);
  display: flex; align-items: center; justify-content: space-between;
}
.sidebar-brand {
  font-size: var(--font-size-xl); font-weight: 700;
  background: linear-gradient(135deg, var(--accent), var(--purple));
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
}
.icon-btn {
  background: none; border: none; color: var(--text-muted);
  cursor: pointer; font-size: var(--font-size-xl); padding: var(--space-1);
  border-radius: var(--radius-xs); transition: all var(--transition);
}
.icon-btn:hover { color: var(--text-primary); background: var(--bg-hover); }
.new-session-btn {
  margin: 0.8rem 1rem; padding: 0.6rem;
  background: var(--bg-tertiary); border: 1px dashed var(--border);
  border-radius: var(--radius); color: var(--text-secondary);
  cursor: pointer; font-size: var(--font-size-sm);
  display: flex; align-items: center; justify-content: center; gap: 0.4rem;
  transition: all var(--transition);
}
.new-session-btn:hover { background: var(--bg-hover); border-color: var(--accent); color: var(--accent); }
.session-list { flex: 1; overflow-y: auto; padding: 0.4rem 0.6rem; }
.session-item {
  padding: 0.6rem 0.7rem; border-radius: var(--radius); cursor: pointer;
  margin-bottom: 3px; transition: all var(--transition);
  border: 1px solid transparent; position: relative;
  background: rgba(255,255,255,0.02);
}
.session-item:hover { background: var(--bg-hover); }
.session-item.active { background: var(--accent-dim); border-color: var(--accent); }
.session-name { font-size: var(--font-size-sm); font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: var(--text-primary); margin-bottom: 3px; }
.session-cwd { font-size: var(--font-size-xs); color: var(--text-muted); font-family: monospace; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-bottom: 4px; }
.session-meta { font-size: var(--font-size-xs); color: var(--text-muted); display: flex; align-items: center; gap: 0.5rem; }
.session-status { display: inline-flex; align-items: center; gap: 4px; font-weight: 500; }
.session-status::before { content: ''; width: 6px; height: 6px; border-radius: 50%; display: inline-block; }
.session-status.running { color: #4ade80; }
.session-status.running::before { background: #22c55e; box-shadow: 0 0 6px #22c55e; }
.session-status.suspended { color: #facc15; }
.session-status.suspended::before { background: var(--yellow); }
.session-status.stopped { color: #94a3b8; }
.session-status.stopped::before { background: #64748b; }
.session-meta .session-time { color: var(--text-muted); font-variant-numeric: tabular-nums; }
.session-delete {
  position: absolute; right: 6px; top: 50%; transform: translateY(-50%);
  background: none; border: none; color: var(--text-muted); cursor: pointer;
  font-size: var(--font-size-lg); padding: 2px 4px; border-radius: var(--radius-2xs);
  opacity: 0; transition: opacity var(--transition);
}
.session-item:hover .session-delete { opacity: 1; }
.session-delete:hover { color: var(--red); background: var(--red-dim); }

/* Main Area */
.main-area { flex: 1; display: flex; flex-direction: column; min-width: 0; }

/* Toolbar */
.toolbar {
  height: var(--toolbar-h); background: var(--bg-secondary);
  border-bottom: 1px solid var(--border-subtle);
  display: flex; align-items: center; padding: 0 0.8rem; gap: 0.4rem; flex-shrink: 0;
}
.toolbar-group { display: flex; gap: 0.3rem; align-items: center; padding: 0 0.4rem; }
.toolbar-group + .toolbar-group { border-left: 1px solid var(--border-subtle); padding-left: 0.8rem; }
.toolbar-btn {
  padding: 0.35rem 0.7rem; background: var(--bg-tertiary);
  border: 1px solid var(--border-subtle); border-radius: var(--radius-sm);
  color: var(--text-secondary); font-size: var(--font-size-sm); cursor: pointer;
  display: flex; align-items: center; gap: 0.3rem;
  transition: all var(--transition); white-space: nowrap;
}
.toolbar-btn:hover { background: var(--bg-hover); color: var(--text-primary); border-color: var(--border); }
.toolbar-btn.active { background: var(--accent-dim); color: var(--accent); border-color: var(--accent); }
.toolbar-btn.danger { color: var(--red); }
.toolbar-btn.danger:hover { background: var(--red-dim); border-color: var(--red); }
.toolbar-btn.success { color: var(--green); }
.toolbar-btn.success:hover { background: var(--green-dim); border-color: var(--green); }
.toolbar-group.disabled { opacity: 0.5; pointer-events: none; }
.toolbar-btn.glow-green {
  background: var(--green-dim); border-color: var(--green); color: var(--green);
  animation: glowPulse 1.5s ease-in-out infinite;
}
.toolbar-btn.glow-red {
  background: var(--red-dim); border-color: var(--red); color: var(--red);
  animation: glowPulseRed 1.5s ease-in-out infinite;
}
@keyframes glowPulse { 0%,100% { box-shadow: 0 0 4px rgba(34,197,94,0.3); } 50% { box-shadow: 0 0 12px rgba(34,197,94,0.6); } }
@keyframes glowPulseRed { 0%,100% { box-shadow: 0 0 4px rgba(239,68,68,0.3); } 50% { box-shadow: 0 0 12px rgba(239,68,68,0.6); } }
/* opus-btns removed — controls now in panel */
.toolbar-spacer { flex: 1; }
.connection-status {
  display: flex; align-items: center; gap: 0.4rem;
  font-size: var(--font-size-xs); color: var(--text-muted);
  padding: 0.3rem 0.6rem; background: var(--bg-tertiary); border-radius: var(--radius-lg);
}
.conn-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--green); box-shadow: 0 0 6px var(--green); }
.conn-dot.disconnected { background: var(--red); box-shadow: 0 0 6px var(--red); animation: blink 1s infinite; }
.conn-dot.reconnecting { background: var(--yellow); box-shadow: 0 0 6px var(--yellow); animation: blink 0.5s infinite; }
@keyframes blink { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
/* Chrome Bridge 状态指示器 */
.chrome-status { display:flex;align-items:center;gap:0.35rem;font-size:var(--font-size-xs);color:var(--text-muted);padding:0.25rem 0.55rem;background:var(--bg-tertiary);border-radius:var(--radius-lg);cursor:pointer;position:relative;border:1px solid transparent;transition:border-color .2s }
.chrome-status:hover { border-color:var(--border) }
.chrome-status .chr-dot { width:6px;height:6px;border-radius:50%;background:var(--text-muted);flex-shrink:0 }
.chrome-status.on .chr-dot { background:var(--green);box-shadow:0 0 5px var(--green) }
.chrome-status-popup { display:none;position:absolute;top:100%;right:0;margin-top:6px;background:var(--bg-secondary);border:1px solid var(--border);border-radius:var(--radius-lg);padding:14px;min-width:260px;z-index:999;box-shadow:0 4px 16px rgba(0,0,0,.3);font-size:var(--font-size-sm) }
.chrome-status-popup.show { display:block }
.chrome-status-popup h4 { margin:0 0 8px;font-size:0.82rem;color:var(--text) }
.chrome-status-popup .info-row { display:flex;justify-content:space-between;padding:3px 0;color:var(--text-muted) }
.chrome-status-popup .info-val { color:var(--text) }
.chrome-status-popup .connect-link { display:block;margin-top:10px;color:var(--accent);text-decoration:none;font-size:0.78rem }
.chrome-status-popup .connect-link:hover { text-decoration:underline }
.chrome-status.off .chr-dot { background: var(--text-muted); box-shadow: none; }
.chrome-status.waiting .chr-dot { background: var(--orange, #f59e0b); box-shadow: 0 0 5px color-mix(in srgb, var(--orange, #f59e0b) 60%, transparent); }
.chrome-status.disabled .chr-dot { background: var(--text-muted); opacity: 0.7; }
.panel-toggle-btn {
  padding: 0.35rem 0.7rem; background: var(--bg-tertiary);
  border: 1px solid var(--border-subtle); border-radius: var(--radius-sm);
  color: var(--text-secondary); font-size: 0.8rem; cursor: pointer;
  transition: all var(--transition);
}
.panel-toggle-btn:hover { background: var(--bg-hover); color: var(--accent); }
.panel-toggle-btn.active { background: var(--accent-dim); color: var(--accent); border-color: var(--accent); }
.panel-toggle-btn { position: relative; }
.notif-red-dot {
  position: absolute; top: -3px; right: -3px;
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--red); display: none;
  animation: blink 1s infinite;
}

/* Terminal */
.terminal-container { flex: 1; background: var(--bg-primary); position: relative; overflow: hidden; }
#terminal { width: 100%; height: 100%; }
/*
 * Input Box 模式：终端变为只读输出面板
 * 1. 隐藏光标（ID 选择器覆盖 xterm 动态注入的 !important 样式）
 * 2. 鼠标指针从 I-beam 改为默认箭头
 * 3. 移除聚焦样式
 */
.input-box-mode #terminal .xterm-cursor,
.input-box-mode #terminal .xterm-cursor.xterm-cursor-block,
.input-box-mode #terminal .xterm-cursor.xterm-cursor-outline,
.input-box-mode #terminal .xterm-cursor.xterm-cursor-bar,
.input-box-mode #terminal .xterm-cursor.xterm-cursor-underline {
  background-color: transparent !important;
  color: inherit !important;
  outline: none !important;
  box-shadow: none !important;
  border-bottom-style: none !important;
  animation: none !important;
}
/* 鼠标改为默认箭头（不再是 I-beam 文本光标） */
.input-box-mode #terminal .xterm {
  cursor: default !important;
}
/* 隐藏 xterm 的隐藏输入框（防止意外聚焦） */
.input-box-mode #terminal .xterm-helper-textarea {
  pointer-events: none !important;
}

/* Right Panel */
.right-panel {
  position: fixed;
  top: 0; right: 0; bottom: 0;
  width: var(--panel-w);
  background: var(--bg-secondary);
  border-left: 1px solid var(--border-subtle);
  display: flex; flex-direction: column;
  z-index: 100;
  transform: translateX(100%);
  transition: transform var(--transition);
  box-shadow: -4px 0 24px rgba(0,0,0,0.5);
  min-height: 0;
  overflow: hidden;
}
.right-panel.open { transform: translateX(0); }
/* ===== L1: Panel Header — two-row layout ===== */
.panel-header {
  padding: 0.55rem 0.7rem; border-bottom: 1px solid var(--border-subtle);
  display: flex; flex-direction: column; gap: 0.35rem; flex-shrink: 0;
  position: relative; overflow: visible; z-index: 10;
}
.hdr-row1 { display: flex; align-items: center; gap: 0.4rem; width: 100%; }
.hdr-row2 { display: flex; align-items: center; gap: 0.5rem; width: 100%; padding-top: 0.1rem; }
.status-dot { font-size: 0.72rem; flex-shrink: 0; line-height: 1; display: inline-flex; align-items: center; gap: 0.15rem; padding: 0.1rem 0.35rem; border-radius: 8px; white-space: nowrap; }
.status-dot.running { background: var(--green-dim); color: var(--green); }
.status-dot.completed { background: rgba(34,197,94,0.1); color: var(--green); }
.status-dot.pending { background: rgba(100,116,139,0.1); color: var(--text-muted); }
.status-dot.paused { background: var(--yellow-dim); color: var(--yellow); }
.status-dot.failed { background: var(--red-dim); color: var(--red); }
.status-dot.rolled_back { background: rgba(167,139,250,0.1); color: var(--purple, #a78bfa); }
.status-dot.stale { background: var(--yellow-dim); color: var(--yellow); }
.status-dot.idle { background: var(--red-dim); color: var(--red); }
.status-dot.unknown { background: rgba(100,116,139,0.1); color: var(--text-muted); }
.status-dot.partially-failed { background: var(--yellow-dim); color: var(--yellow); }
.p-title {
  flex: 1; min-width: 0; cursor: pointer; display: flex; align-items: center; gap: 0.35rem;
  position: relative; user-select: none; transition: color var(--transition);
}
.p-title:hover { color: var(--accent); }
.p-title-name { font-size: 0.82rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.p-arrow { font-size: 0.5rem; color: var(--text-muted); transition: transform 0.2s; flex-shrink: 0; }
.p-arrow.open { transform: rotate(180deg); }
.p-timer { font-family: 'JetBrains Mono','Fira Code',monospace; font-size: 0.82rem; color: var(--accent); font-weight: 600; flex-shrink: 0; letter-spacing: 0.02em; }
.p-cost { font-size: 0.78rem; color: var(--yellow); font-weight: 600; flex-shrink: 0; }
.p-ctrl { display: flex; gap: 3px; flex-shrink: 0; margin-left: auto; }
.p-ctrl button { background: none; border: 1px solid var(--border-subtle); cursor: pointer; color: var(--text-muted); font-size: 0.88rem; padding: 2px 6px; border-radius: 3px; transition: all 0.15s; line-height: 1; }
.p-ctrl button:hover { color: var(--text-primary); border-color: var(--border); background: var(--bg-hover); }
.p-ctrl button.warn:hover { color: var(--yellow); border-color: var(--yellow); }
.p-ctrl button.danger:hover { color: var(--red); border-color: var(--red); }
.p-ctrl button.ok { color: var(--accent); border-color: var(--accent); }
/* Task dropdown — direct child of right-panel, positioned absolutely */
.t-dd { display: none; position: absolute; top: 58px; left: 10px; right: 10px; min-width: 0; background: var(--bg-tertiary); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: 0 8px 24px rgba(0,0,0,0.5); z-index: 500; overflow: hidden; max-height: 60vh; overflow-y: auto; }
.t-dd.show { display: block; animation: fadeSlide 0.15s ease; }
.t-dd-item { display: flex; align-items: center; gap: 0.5rem; padding: 0.45rem 0.7rem; cursor: pointer; font-size: 0.82rem; border-bottom: 1px solid var(--border-subtle); transition: background var(--transition); }
.t-dd-item:last-child { border-bottom: none; }
.t-dd-item:hover { background: var(--bg-hover); }
.t-dd-item.active { border-left: 2px solid var(--accent); background: rgba(56,189,248,0.04); }
.t-dd-dot { flex-shrink: 0; font-size: 0.68rem; line-height: 1; display: inline-flex; align-items: center; gap: 0.15rem; padding: 0.1rem 0.3rem; border-radius: 8px; white-space: nowrap; }
.t-dd-dot.running { background: var(--green-dim); color: var(--green); }
.t-dd-dot.completed { background: rgba(34,197,94,0.1); color: var(--green); }
.t-dd-dot.paused { background: var(--yellow-dim); color: var(--yellow); }
.t-dd-dot.failed { background: var(--red-dim); color: var(--red); }
.t-dd-dot.rolled_back { background: rgba(167,139,250,0.1); color: var(--purple, #a78bfa); }
.t-dd-dot.pending { background: rgba(100,116,139,0.1); color: var(--text-muted); }
.t-dd-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.t-dd-cost { color: var(--text-muted); font-size: 0.76rem; flex-shrink: 0; }
/* ── Task Selector trigger button ── */
.ts-trigger {
  background: none; border: 1px solid var(--border-subtle); color: var(--text-muted);
  border-radius: var(--radius-sm); padding: 0.2rem 0.35rem;
  cursor: pointer; display: flex; align-items: center;
  transition: all var(--transition); flex-shrink: 0;
}
.ts-trigger:hover { color: var(--accent); border-color: var(--accent); background: var(--accent-dim); }
/* ── Task Selector Modal (left-right dual panel) ── */
.modal-box.ts-modal { width: 520px; max-width: 92vw; padding: 0; overflow: hidden; }
.ts-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 0.6rem 0.8rem;
}
.ts-header .modal-title { margin-bottom: 0; }
.ts-close {
  background: none; border: none; color: var(--text-muted); font-size: var(--font-size-xl);
  cursor: pointer; padding: 0.2rem 0.4rem; border-radius: var(--radius-sm);
  transition: all var(--transition); line-height: 1;
}
.ts-close:hover { color: var(--text-primary); background: var(--bg-hover); }
.ts-body { display: flex; height: 380px; max-height: 60vh; border-top: 1px solid var(--border-subtle); }
/* Left panel — project list */
.ts-left {
  width: 130px; flex-shrink: 0;
  border-right: 1px solid var(--border-subtle);
  overflow-y: auto; padding: 0.4rem 0;
}
.ts-left::-webkit-scrollbar { width: 3px; }
.ts-left::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
.ts-proj {
  display: flex; flex-direction: column; gap: 1px;
  padding: 0.5rem 0.6rem; cursor: pointer;
  transition: background var(--transition);
  border-left: 2px solid transparent;
}
.ts-proj:hover { background: var(--bg-hover); }
.ts-proj.active {
  background: var(--accent-dim); border-left-color: var(--accent);
}
.ts-proj-row1 {
  display: flex; align-items: center; gap: 0.35rem;
  font-size: 0.78rem; font-weight: 600; color: var(--text-primary);
}
.ts-proj-row2 {
  font-size: 0.68rem; color: var(--text-muted); padding-left: 0.85rem;
}
/* Right panel — task list */
.ts-right { flex: 1; overflow-y: auto; padding: 0.4rem; }
.ts-right::-webkit-scrollbar { width: 3px; }
.ts-right::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
.ts-task {
  display: flex; flex-direction: column; gap: 2px;
  padding: 0.5rem 0.6rem; border-radius: var(--radius-sm);
  cursor: pointer; transition: all var(--transition);
  border-left: 2px solid transparent;
}
.ts-task:hover { background: var(--bg-hover); }
.ts-task.active {
  background: var(--accent-dim); border-left-color: var(--accent);
}
.ts-task-row1 {
  display: flex; align-items: center; gap: 0.4rem;
  font-size: 0.8rem; color: var(--text-primary);
}
.ts-task-row1 .t-dd-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ts-task-row2 {
  display: flex; align-items: center; gap: 0.5rem;
  font-size: 0.7rem; color: var(--text-muted); padding-left: 0.85rem;
}
.ts-task-row2 .ts-status {
  font-size: 0.65rem; padding: 0.1rem 0.3rem; border-radius: 3px;
}
.ts-task-row2 .ts-status.running { background: var(--green-dim); color: var(--green); }
.ts-task-row2 .ts-status.completed { background: rgba(100,116,139,0.15); color: var(--text-muted); }
.ts-task-row2 .ts-status.paused { background: var(--yellow-dim); color: var(--yellow); }
.ts-task-row2 .ts-status.failed { background: var(--red-dim); color: var(--red); }
/* Empty / loading states */
.ts-empty {
  display: flex; align-items: center; justify-content: center;
  height: 100%; color: var(--text-muted); font-size: 0.82rem;
}
/* Live banner */
.live-banner { display: flex; align-items: center; gap: 0.4rem; padding: 0.32rem 0.65rem; cursor: pointer; font-size: 0.7rem; color: var(--green); background: linear-gradient(90deg, rgba(34,197,94,0.08), transparent); border-bottom: 1px solid rgba(34,197,94,0.1); transition: background 0.15s; }
.live-banner:hover { background: linear-gradient(90deg, rgba(34,197,94,0.15), transparent); }
.live-banner-dismiss { border: 1px solid var(--border); border-radius: 4px; background: var(--bg-secondary); color: var(--text-secondary); padding: 0.12rem 0.45rem; cursor: pointer; font-size: 0.68rem; }
.live-banner-dismiss:hover { color: var(--text-primary); background: var(--bg-hover); }
.b-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--green); flex-shrink: 0; animation: blink 1s infinite; }
/* L3: Progress row */
.progress-row {
  display: flex; align-items: center; gap: 0.5rem;
  padding: 0.35rem 0.7rem; border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0; cursor: pointer; transition: background 0.15s;
  position: relative; z-index: 1;
}
.progress-row:hover { background: var(--bg-hover); }
.pr-bar { flex: 1; height: 4px; background: var(--bg-primary); border-radius: 2px; overflow: hidden; }
.pr-fill { height: 100%; background: linear-gradient(90deg, var(--accent), var(--green)); border-radius: 2px; transition: width 0.5s; }
.pr-pct { font-size: 0.78rem; color: var(--text-muted); min-width: 1.8rem; text-align: right; flex-shrink: 0; }
.pr-steps { font-size: 0.78rem; color: var(--green); font-weight: 600; flex-shrink: 0; }
.pr-toggle { font-size: 0.76rem; color: var(--text-muted); display: flex; align-items: center; gap: 2px; flex-shrink: 0; transition: color 0.15s; }
.progress-row:hover .pr-toggle { color: var(--accent); }
.pr-arrow { font-size: 0.5rem; transition: transform 0.3s; display: none; }
.pr-arrow.open { transform: rotate(180deg); }
.pr-toggle { display: none; }
/* L4: Step list (collapsible) */
.step-list { flex-shrink: 0; max-height: 0; overflow: hidden; transition: max-height 0.3s ease; border-bottom: 1px solid transparent; position: relative; z-index: 1; min-height: 0; }
.step-list.open { max-height: 45vh; overflow-y: auto; border-bottom-color: var(--border-subtle); }
.step-list::-webkit-scrollbar { width: 3px; }
.step-list::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
/* Split body: left=step-list, right=log-panels */
.split-body { display: flex; flex: 1 1 auto; min-height: 220px; overflow: hidden; }
.split-body ~ .progress-row, .split-body + .progress-row { display: none !important; }
.split-body .step-list-col { width: 280px; flex: 0 0 280px; min-height: 0; overflow-y: auto; border-right: 1px solid var(--border-subtle); }
.split-body .step-list-col .step-list { max-height: none !important; overflow: visible; border-bottom: none; }
.split-body .step-list-col::-webkit-scrollbar { width: 3px; }
.split-body .step-list-col::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
.split-body .log-panels-col { flex: 1; min-width: 0; display: flex; flex-direction: column; }
@keyframes fadeSlide { from { opacity: 0; transform: translateY(3px); } to { opacity: 1; transform: translateY(0); } }
/* (old panel-content, #tab-live, #task-live-content removed — v5 layout uses right-panel flex directly) */
#no-task-msg { padding: 0.6rem; }

/* Agent cards */
.agent-card {
  background: var(--bg-tertiary); border: 1px solid var(--border-subtle);
  border-radius: var(--radius); margin-bottom: 0.5rem; overflow: hidden;
  transition: border-color var(--transition);
  min-height: 58px; flex-shrink: 0;
}
.agent-card.active { border-color: var(--accent); }
.agent-card-header {
  padding: 0.45rem 0.6rem; display: flex; flex-direction: column; gap: 0.2rem;
  cursor: pointer;
}
.agent-card-row1 { display: flex; align-items: center; justify-content: space-between; }
.agent-card-row2 { display: flex; align-items: center; gap: 0.6rem; padding-left: 28px; font-size: 0.72rem; color: var(--text-muted); }
.agent-role { display: flex; align-items: center; gap: 0.4rem; font-size: 0.78rem; font-weight: 600; min-width: 0; overflow: hidden; }
.agent-role > span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.role-icon { width: 24px; height: 24px; border-radius: 5px; display: flex; align-items: center; justify-content: center; font-size: 0.75rem; flex-shrink: 0; }
.agent-meta { display: flex; gap: 0.5rem; font-size: 0.72rem; color: var(--text-muted); flex-shrink: 0; white-space: nowrap; }
.step-duration[data-running="true"] { color: var(--green); }
.cost-tag.unbillable { font-size: 0.65rem; padding: 0.1rem 0.3rem; border-radius: 8px; background: rgba(100,116,139,0.15); color: var(--text-muted); }
.cost-info-icon { font-size: 0.75rem; color: var(--text-muted); cursor: help; margin-left: 0.2rem; }
.agent-status-badge {
  padding: 0.1rem 0.4rem; border-radius: 10px; font-size: 0.68rem; font-weight: 500; flex-shrink: 0;
}
.badge-running { background: var(--green-dim); color: var(--green); }
.badge-completed { background: var(--accent-dim); color: var(--accent); }
.badge-error { background: var(--red-dim); color: var(--red); }
.badge-waiting { background: rgba(100,116,139,0.2); color: var(--text-muted); }

/* Task overview */
.task-overview {
  background: var(--bg-tertiary); border: 1px solid var(--border-subtle);
  border-radius: var(--radius); padding: 0.8rem; margin-bottom: 0.6rem;
}
.task-title { font-size: 0.85rem; font-weight: 600; margin-bottom: 0.5rem; color: var(--text-primary); }
.task-progress-bar { height: 4px; background: var(--bg-primary); border-radius: 2px; overflow: hidden; margin-bottom: 0.5rem; }
.task-progress-fill { height: 100%; background: linear-gradient(90deg, var(--accent), var(--green)); border-radius: 2px; transition: width 0.5s; }
.task-stats { display: flex; gap: 1rem; font-size: 0.75rem; color: var(--text-muted); }
.task-stat-value { color: var(--text-primary); font-weight: 600; }

.doc-link {
  display: flex; align-items: center; gap: 0.4rem;
  padding: 0.3rem 0.5rem; background: var(--bg-card); border-radius: var(--radius-sm);
  font-size: 0.75rem; color: var(--accent); cursor: pointer; margin-top: 0.4rem;
  transition: background var(--transition); text-decoration: none;
}
.doc-link:hover { background: var(--bg-hover); }

/* No task placeholder */
.no-task {
  flex-direction: column; align-items: center; justify-content: center;
  flex: 1; color: var(--text-muted); font-size: 0.85rem; text-align: center; gap: 0.5rem;
}
.no-task-icon { font-size: var(--font-size-2xl); opacity: 0.3; }

/* Reconnect overlay */
.reconnect-overlay {
  position: fixed; inset: 0; z-index: 900;
  background: rgba(10,14,23,0.85);
  display: none; align-items: center; justify-content: center;
  flex-direction: column; gap: 1rem;
}
.reconnect-overlay.visible { display: flex; }
.reconnect-spinner {
  width: 40px; height: 40px; border: 3px solid var(--border);
  border-top-color: var(--accent); border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.reconnect-text { color: var(--text-secondary); font-size: var(--font-size-lg); }

/* Top Toast Notification (VC-COMP-06) */
.vizo-toast-host {
  position: fixed; top: 16px; left: 50%; transform: translateX(-50%);
  width: calc(100vw - 24px); max-width: 720px; z-index: 10000;
  display: flex; flex-direction: column; gap: 10px; align-items: center;
  pointer-events: none;
}
.vizo-toast {
  --toast-accent: var(--accent);
  --toast-border: rgba(56,189,248,0.24);
  --toast-glow: rgba(56,189,248,0.16);
  --toast-icon-bg: rgba(56,189,248,0.12);
  position: relative; display: grid; grid-template-columns: auto minmax(0,1fr) auto;
  gap: 12px; align-items: center; width: fit-content; max-width: 100%;
  padding: 14px 16px 14px 14px; border-radius: 18px; border: 1px solid var(--toast-border);
  background: linear-gradient(180deg, rgba(9,20,33,0.97) 0%, rgba(6,17,29,0.98) 100%);
  box-shadow: 0 18px 42px rgba(0,0,0,0.28), 0 0 0 1px rgba(255,255,255,0.02), 0 0 24px var(--toast-glow);
  backdrop-filter: blur(10px); pointer-events: auto; overflow: hidden;
  animation: vizo-toast-enter 0.2s ease;
}
.vizo-toast::before {
  content: ""; position: absolute; left: 0; top: 10px; bottom: 10px;
  width: 3px; border-radius: 999px; background: var(--toast-accent);
}
.vizo-toast--info { --toast-accent: #22d3ee; --toast-border: rgba(34,211,238,0.22); --toast-glow: rgba(34,211,238,0.14); --toast-icon-bg: rgba(34,211,238,0.12); }
.vizo-toast--success { --toast-accent: #66d59b; --toast-border: rgba(102,213,155,0.24); --toast-glow: rgba(102,213,155,0.14); --toast-icon-bg: rgba(102,213,155,0.12); }
.vizo-toast--warning { --toast-accent: #fbbf24; --toast-border: rgba(251,191,36,0.24); --toast-glow: rgba(251,191,36,0.12); --toast-icon-bg: rgba(251,191,36,0.12); }
.vizo-toast--error { --toast-accent: #ff7e88; --toast-border: rgba(255,126,136,0.24); --toast-glow: rgba(255,126,136,0.12); --toast-icon-bg: rgba(255,126,136,0.12); }
.vizo-toast__icon-shell {
  width: 28px; height: 28px; border-radius: 999px; display: inline-flex;
  align-items: center; justify-content: center; flex-shrink: 0; align-self: start;
  background: var(--toast-icon-bg); color: var(--toast-accent); margin: 1px 0 0 4px;
}
.vizo-toast__icon { font-size: 18px; line-height: 1; }
.vizo-toast__copy { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.vizo-toast__topline { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; min-width: 0; }
.vizo-toast__title {
  font-size: 13px; font-weight: 800; line-height: 1.35; color: var(--text-primary);
  white-space: normal; overflow-wrap: anywhere;
}
.vizo-toast__badge {
  display: inline-flex; align-items: center; padding: 2px 8px; border-radius: 999px;
  background: var(--toast-icon-bg); color: var(--toast-accent);
  font-size: 9px; font-weight: 800; letter-spacing: 0.08em;
}
.vizo-toast__close {
  width: 28px; height: 28px; border: 0; border-radius: 999px; background: transparent;
  color: var(--text-muted); align-self: center; display: inline-flex; align-items: center;
  justify-content: center; flex-shrink: 0; transition: background var(--transition), color var(--transition);
}
.vizo-toast__close:hover { background: rgba(255,255,255,0.06); color: var(--text-primary); }
.vizo-toast.is-leaving { animation: vizo-toast-exit 0.16s ease forwards; }
[data-theme="light"] .vizo-toast {
  background: linear-gradient(180deg, rgba(255,255,255,0.98) 0%, rgba(245,247,250,0.98) 100%);
  box-shadow: 0 18px 42px rgba(15,23,42,0.16), 0 0 0 1px rgba(15,23,42,0.04), 0 0 24px var(--toast-glow);
}
@keyframes vizo-toast-enter { from { opacity: 0; transform: translateY(-10px); } to { opacity: 1; transform: translateY(0); } }
@keyframes vizo-toast-exit { from { opacity: 1; transform: translateY(0); } to { opacity: 0; transform: translateY(-8px); } }
@media (max-width: 900px) {
  .vizo-toast-host { top: 12px; width: calc(100vw - 16px); max-width: none; }
  .vizo-toast { gap: 10px; max-width: 100%; padding: 12px; }
}

/* Input Area */
.input-area {
  border-top: 1px solid var(--border-subtle);
  display: flex; flex-direction: column;
  background: var(--bg-secondary);
}
.input-row {
  display: flex; gap: 0.6rem; align-items: flex-end;
  padding: 0.6rem 1rem;
}
.input-area.hidden { display: none; }
.input-area.disabled { opacity: 0.5; pointer-events: none; }
.input-wrapper {
  flex: 1; background: var(--bg-input);
  border: 1px solid var(--border); border-radius: var(--radius);
  display: flex; align-items: flex-end;
  transition: border-color var(--transition);
}
.input-wrapper:focus-within { border-color: var(--accent); }
#user-input {
  flex: 1; background: none; border: none;
  padding: 0.6rem 0.8rem; color: var(--text-primary);
  font-family: inherit; font-size: var(--font-size-md); outline: none;
  resize: none; max-height: 120px; line-height: 1.5;
}
#user-input::placeholder { color: var(--text-muted); }
.send-btn {
  padding: 0.5rem 1rem;
  background: linear-gradient(135deg, #0369a1, #0284c7);
  border: none; border-radius: var(--radius);
  color: white; font-size: 0.85rem; cursor: pointer; font-weight: 500;
  transition: all var(--transition); white-space: nowrap;
  flex-shrink: 0;
}
.send-btn:hover { background: linear-gradient(135deg, #0284c7, #0ea5e9); }
/* Input row: hidden by default, shown on mobile */
#input-row { display: none; }

/* Agent Card Body */
.agent-card-body {
  padding: 0 0.8rem 0;
  font-size: 0.78rem; color: var(--text-secondary); line-height: 1.5;
  max-height: 0; overflow: hidden;
  transition: max-height 0.3s ease, padding 0.3s;
}
.agent-card.expanded .agent-card-body { max-height: 400px; padding: 0.4rem 0.8rem 0.8rem; }

/* Sub-task expansion */
.subtask-card { position: relative; }
.subtask-expand-icon {
  font-size: 0.7rem; color: var(--text-muted); margin-left: 0.3rem;
  transition: transform 0.2s;
}
.subtask-inner {
  background: var(--bg-secondary); border-top: 1px solid var(--border-subtle);
  padding: 0.3rem 0;
}
.subtask-inner-row {
  display: flex; align-items: center; gap: 0.5rem;
  padding: 0.35rem 0.8rem 0.35rem 1.2rem;
  cursor: pointer; transition: background var(--transition);
  font-size: 0.8rem;
}
.subtask-inner-row:hover { background: var(--bg-hover); }
.subtask-inner-name {
  flex: 1; font-weight: 500; color: var(--text-primary);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.subtask-inner-meta {
  font-size: 0.75rem; color: var(--text-muted); white-space: nowrap;
}

.agent-action {
  display: flex; align-items: center; gap: 0.4rem;
  padding: 0.25rem 0;
  font-family: "JetBrains Mono", monospace; font-size: 0.72rem;
}
.agent-action-icon { color: var(--accent); }

/* ====== Shortcut Bar ====== */
.cc-shortcuts {
  display: flex; flex-wrap: wrap; gap: 0.3rem; padding: 0.3rem 0.5rem;
  border-bottom: 1px solid var(--border-subtle);
  background: var(--bg-secondary);
}
.cc-btn {
  display: flex; align-items: center; gap: 0.2rem;
  padding: 0.2rem 0.5rem; border: 1px solid var(--border);
  border-radius: 4px; background: transparent; color: var(--text-secondary);
  font-size: 0.72rem; cursor: pointer; white-space: nowrap;
  transition: all 0.15s;
}
.cc-btn:hover { background: var(--bg-tertiary); color: var(--text-primary); }
.cc-btn:active { transform: scale(0.95); }
.cc-key {
  font-size: 0.62rem; padding: 0.05rem 0.3rem;
  background: var(--bg-primary); border-radius: 3px;
  color: var(--text-muted); font-family: monospace;
}
.cc-btn.allow-highlight {
  border-color: var(--green); color: var(--green);
  animation: allowPulse 1.5s infinite;
}
.cc-btn.deny-highlight { border-color: var(--red); color: var(--red); }
@keyframes allowPulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(74,222,128,0.4); }
  50% { box-shadow: 0 0 8px 2px rgba(74,222,128,0.2); }
}

/* ====== Workflow Bar ====== */
.wf-bar {
  display: flex; gap: 0.4rem; padding: 0.3rem 0.5rem 0.4rem;
  border-top: 1px solid var(--border); overflow-x: auto;
  flex-wrap: nowrap;
}
.wf-btn {
  flex: 1 1 0; min-width: 0; padding: 0.3rem 0.5rem;
  border: none; border-radius: 6px; cursor: pointer;
  font-size: 0.75rem; white-space: nowrap;
  color: #fff; transition: opacity 0.15s;
}
.wf-btn:hover { opacity: 0.85; }
.wf-new      { background: #1f6feb; }
.wf-fix      { background: #da3633; }
.wf-refactor { background: #d29922; }
.wf-auto     { background: #238636; }
.wf-embedded { background: #484f58; }
.wf-nondev   { background: #484f58; }
[data-theme="light"] .wf-new      { background: #0969da; }
[data-theme="light"] .wf-fix      { background: #cf222e; }
[data-theme="light"] .wf-refactor { background: #bf8700; }
[data-theme="light"] .wf-auto     { background: #1a7f37; }
[data-theme="light"] .wf-embedded { background: #6e7781; }
[data-theme="light"] .wf-nondev   { background: #6e7781; }

/* ====== Notification System ====== */
.notification-stack {
  position: absolute; bottom: 0; left: 0; right: 0;
  display: flex; flex-direction: column-reverse; gap: 0.4rem;
  padding: 0.5rem; pointer-events: none; z-index: 100;
  max-height: 50%; overflow: hidden;
}
.notif-bar {
  pointer-events: auto; background: var(--bg-secondary);
  border: 1px solid var(--border); border-radius: 8px;
  padding: 0.6rem 0.8rem; font-size: 0.78rem; color: var(--text-primary);
  animation: slideUp 0.3s ease; position: relative;
}
.notif-bar.green  { border-left: 3px solid var(--green); }
.notif-bar.blue   { border-left: 3px solid var(--accent); }
.notif-bar.red    { border-left: 3px solid var(--red); }
.notif-bar.yellow { border-left: 3px solid var(--yellow); }
.notif-bar.orange { border-left: 3px solid var(--orange); }
.notif-header { display: flex; justify-content: space-between; align-items: center; }
.notif-title { font-weight: 600; }
.notif-meta { font-size: 0.7rem; color: var(--text-muted); margin-top: 0.2rem; }
.notif-actions { display: flex; gap: 0.4rem; margin-top: 0.4rem; }
.notif-close {
  position: absolute; top: 0.3rem; right: 0.5rem;
  background: none; border: none; color: var(--text-muted);
  cursor: pointer; font-size: 0.9rem;
}
.notif-dismiss-btn {
  border: 1px solid var(--border); border-radius: 4px; background: var(--bg-card);
  color: var(--text-secondary); padding: 0.2rem 0.5rem; cursor: pointer; font-size: 0.72rem;
}
.notif-dismiss-btn:hover { background: var(--bg-hover); color: var(--text-primary); }

/* ====== Confirm Notification Bar ====== */
.confirm-bar { border-left-width: 3px; border-left-color: var(--accent); }
.confirm-bar .notif-summary { color: var(--text-secondary); margin: 0.3rem 0; font-size: 0.75rem; }
.confirm-bar .notif-doc-link { color: var(--accent); text-decoration: none; font-size: 0.75rem; }
.confirm-bar .notif-doc-link:hover { text-decoration: underline; }
.confirm-bar .notif-context { font-size: 0.68rem; color: var(--text-muted); margin: 0.2rem 0 0.4rem; }
.banner-btn {
  padding: 0.25rem 0.6rem; border-radius: 4px; font-size: 0.72rem;
  cursor: pointer; border: 1px solid; transition: all 0.15s;
}
.banner-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.banner-btn.primary { background: var(--green); color: #000; border-color: var(--green); }
.banner-btn.danger { background: transparent; color: var(--red); border-color: var(--red); }
.banner-btn.feedback-btn { background: transparent; color: var(--yellow); border-color: var(--yellow); }
.feedback-area { display: none; margin-top: 0.4rem; }
.feedback-area.open { display: block; }
.feedback-area textarea {
  width: 100%; min-height: 50px; max-height: 120px;
  background: var(--bg-input); border: 1px solid var(--border);
  border-radius: 4px; color: var(--text-primary);
  padding: 0.4rem; font-size: 0.75rem; resize: vertical;
}
.feedback-submit {
  margin-top: 0.3rem; padding: 0.25rem 0.8rem;
  border-radius: 4px; font-size: 0.72rem; cursor: pointer; border: none;
}
.feedback-submit.yellow { background: var(--yellow); color: #000; }
.banner-btn.warn { background: transparent; color: var(--yellow); border-color: var(--yellow); }
.panel-confirm-area {
  padding: 0.4rem 0.6rem; border-top: 1px solid var(--border);
  background: var(--bg-secondary);
  flex-shrink: 0; max-height: 34vh; overflow-y: auto;
}
.panel-confirm-area .notif-actions { display: flex; gap: 0.4rem; flex-wrap: wrap; }
.panel-confirm-area .feedback-area { margin-top: 0.4rem; }
.confirm-bar.timeout { border-top: 2px solid var(--orange); }
.confirm-bar.timeout .timeout-badge {
  display: inline-block; font-size: 0.65rem; color: var(--orange);
  animation: remindPulse 2s infinite;
}
@keyframes remindPulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
@keyframes slideUp {
  from { transform: translateY(20px); opacity: 0; }
  to { transform: translateY(0); opacity: 1; }
}

/* ====== Panel Controls ====== */
.opus-controls { padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--border-subtle); flex-shrink: 0; max-height: 34vh; overflow-y: auto; }
.ctrl-group { display: flex; gap: 0.4rem; flex-wrap: wrap; }
.ctrl-btn {
  padding: 0.3rem 0.7rem; border-radius: 5px;
  font-size: 0.75rem; cursor: pointer; border: 1px solid;
  transition: all 0.15s;
}
.ctrl-btn.success { background: var(--green); color: #000; border-color: var(--green); }
.ctrl-btn.warn { background: var(--yellow); color: #000; border-color: var(--yellow); }
.ctrl-btn.danger { background: transparent; color: var(--red); border-color: var(--red); }
.ctrl-btn.accent { background: transparent; color: var(--accent); border-color: var(--accent); }
.ctrl-btn.muted { background: transparent; color: var(--text-muted); border-color: var(--border); }
.panel-feedback, .rollback-selector { padding: 0.4rem 0; font-size: 0.75rem; }
.panel-feedback textarea, .rollback-selector textarea {
  width: 100%; min-height: 40px; max-height: 80px;
  background: var(--bg-input); border: 1px solid var(--border);
  border-radius: 4px; color: var(--text-primary);
  padding: 0.3rem; font-size: 0.72rem; resize: vertical; margin-bottom: 0.3rem;
}
.panel-feedback-hint { font-size: 0.68rem; color: var(--text-muted); margin-bottom: 0.3rem; }
.rollback-steps { margin-bottom: 0.4rem; }
.rollback-step-option {
  display: flex; align-items: center; gap: 0.4rem;
  padding: 0.3rem 0; font-size: 0.72rem; cursor: pointer;
}
.rollback-step-option input[type="radio"] { accent-color: var(--accent); }
.rollback-step-doc { color: var(--text-muted); font-size: 0.65rem; }
.rollback-actions { display: flex; gap: 0.4rem; }

/* ====== Enhanced Card States ====== */
.agent-card.card-pending { opacity: 0.5; }
.agent-card.card-running { border-left: 2px solid var(--green); opacity: 1; }
.agent-card.card-completed { opacity: 0.85; }
.agent-card.card-error { border-left: 2px solid var(--red); opacity: 1; }
.agent-card.card-paused { opacity: 1; }
.badge-paused { background: rgba(250,204,21,0.15); color: var(--yellow); }

/* ====== Markdown Preview Drawer ====== */
.md-preview-overlay {
  position: fixed; inset: 0; z-index: 1800;
  background: rgba(0,0,0,0.4); backdrop-filter: blur(2px);
}
.md-preview-drawer {
  position: fixed; top: 0; right: 0; bottom: 0;
  width: 680px; max-width: 90vw; z-index: 1900;
  background: var(--bg-secondary);
  border-left: 1px solid var(--border);
  box-shadow: -4px 0 20px rgba(0,0,0,0.3);
  display: flex; flex-direction: column;
}
.md-preview-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 0.8rem 1rem;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.md-preview-title {
  font-size: 0.9rem; font-weight: 600; color: var(--text-primary);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.md-preview-close {
  background: none; border: none; color: var(--text-muted);
  font-size: var(--font-size-xl); cursor: pointer; padding: 0 0.3rem; line-height: 1;
}
.md-preview-close:hover { color: var(--text-primary); }
.md-preview-body { flex: 1; position: relative; overflow: hidden; }
.md-preview-iframe { width: 100%; height: 100%; border: none; background: var(--bg-primary); }
.md-preview-loading {
  position: absolute; inset: 0;
  display: flex; align-items: center; justify-content: center;
  color: var(--text-muted); font-size: 0.85rem;
}
.md-preview-error {
  position: absolute; inset: 0;
  display: flex; align-items: center; justify-content: center;
  color: var(--red); font-size: 0.85rem;
}

/* 子任务展开 */
.subtask-card { position: relative; }
.subtask-card .agent-card-header { cursor: pointer; }
.subtask-expand-icon {
  font-size: 0.65rem; color: var(--text-muted); margin-left: auto; transition: transform 0.2s;
}
.subtask-inner {
  background: var(--bg-secondary); border-top: 1px solid var(--border-subtle);
  padding: 0.3rem 0;
}
.subtask-inner-row {
  display: flex; align-items: center; gap: 0.5rem;
  padding: 0.35rem 0.8rem 0.35rem 1.4rem; cursor: pointer;
  transition: background 0.15s;
}
.subtask-inner-row:hover { background: var(--bg-tertiary); }
.subtask-inner-name {
  flex: 1; font-size: 0.78rem; color: var(--text-primary);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.subtask-inner-meta {
  font-size: 0.72rem; color: var(--text-muted); white-space: nowrap;
}

.agent-detail-row {
  display: flex; justify-content: space-between;
  padding: 0.15rem 0; font-size: 0.8rem;
  border-bottom: 1px solid var(--border-subtle);
}
.agent-detail-label { color: var(--text-muted); }
.agent-detail-doc { color: var(--accent); text-decoration: none; font-size: 0.8rem; }
.agent-detail-doc:hover { text-decoration: underline; }
.agent-detail-status { color: var(--green); font-size: 0.8rem; margin-top: 0.2rem; }
.agent-detail-error {
  background: rgba(248,113,113,0.1); border: 1px solid var(--red);
  border-radius: 4px; padding: 0.3rem; margin-top: 0.2rem;
  font-size: 0.8rem; color: var(--red);
}

@media (max-width: 1200px) { :root { --panel-w: 600px; } }

/* (drawer CSS removed — replaced by progress-row + step-list above) */

/* ===== F3: 角色标签页 ===== */
.role-tabs-wrap {
  display: flex; align-items: stretch; position: relative;
  border-bottom: 1px solid var(--border-subtle); flex-shrink: 0;
}
.rt-arrow {
  background: var(--bg-tertiary); border: none; color: var(--text-muted);
  cursor: pointer; font-size: var(--font-size-xl); padding: 0 0.5rem; flex-shrink: 0;
  display: flex; align-items: center; transition: all 0.15s; line-height: 1;
}
.rt-arrow:hover { color: var(--text-primary); background: var(--bg-hover); }
.rt-arrow.hidden { visibility: hidden; width: 0; padding: 0; overflow: hidden; }
.role-tabs {
  display: flex; gap: 2px; padding: 0.3rem 0.2rem;
  overflow: hidden; flex: 1;
  scroll-behavior: smooth;
}
.role-tab {
  padding: 0.3rem 0.7rem; border-radius: 4px 4px 0 0;
  font-size: 0.82rem; cursor: pointer; white-space: nowrap;
  display: flex; align-items: center; gap: 4px;\n  color: var(--text-muted); border-bottom: 2px solid transparent;
  transition: all 0.2s;
}
.role-tab:hover { background: rgba(56,189,248,0.05); }
.role-tab.active {
  color: var(--text-primary); background: rgba(56,189,248,0.08);
  border-bottom-color: var(--green);
}
.role-tab.error { color: var(--red); }
.role-tab.error.active {
  background: rgba(239,68,68,0.08);
  border-bottom-color: var(--red);
}
.role-tab.completed { color: var(--text-muted); }
.role-tab.removing { opacity: 0; transition: opacity 0.3s ease; }
.role-tab-dot {
  width: 6px; height: 6px; border-radius: 50%;
  display: inline-block;
}
.role-tab-dot.running { background: var(--green); animation: dotPulse 1.5s infinite; }
.role-tab-dot.error { background: var(--red); }

@keyframes dotPulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.3; }
}

/* ===== L6: Stream Header (merged role-info + log-header) ===== */
.stream-hdr {
  display: flex; align-items: center; justify-content: space-between;
  padding: 0.2rem 0.6rem; font-size: 0.76rem; border-bottom: 1px solid var(--border-subtle); flex-shrink: 0;
}
.sh-left { display: flex; align-items: center; gap: 0.45rem; }
.sh-model { color: var(--purple); }
.sh-time { color: var(--text-secondary); }
.sh-cost { color: var(--yellow); }
.sh-right { display: flex; align-items: center; gap: 0.35rem; }
.live-tag { background: var(--green-dim); color: var(--green); font-size: 0.6rem; padding: 0.06rem 0.3rem; border-radius: 4px; font-weight: 700; letter-spacing: 0.5px; animation: blink 2s infinite; }
.sh-btn { background: none; border: 1px solid var(--border-subtle); color: var(--text-muted); font-size: 0.7rem; padding: 0.1rem 0.35rem; border-radius: 3px; cursor: pointer; transition: all 0.15s; }
.sh-btn:hover { color: var(--text-secondary); border-color: var(--border); }
.sh-btn.highlight { color: var(--accent); border-color: var(--accent); }
.log-body {
  flex: 1; overflow-y: auto; padding: 0.3rem 0.5rem;
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  font-size: 0.78rem; line-height: 1.5;
}
.log-body::-webkit-scrollbar { width: 4px; }
.log-body::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }

/* 日志条目 */
.log-entry {
  padding: 1px 0; animation: logFadeIn 0.3s ease;
  display: flex; gap: 0.4rem; align-items: flex-start;
}
@keyframes logFadeIn {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: translateY(0); }
}
.log-ts { color: var(--text-muted); white-space: nowrap; flex-shrink: 0; }
.log-type { font-weight: 600; white-space: nowrap; flex-shrink: 0; }
.log-type.read { color: #58a6ff; }
.log-type.write { color: #3fb950; }
.log-type.exec { color: #d29922; }
.log-type.think { color: #bc8cff; font-style: italic; }
.log-type.search { color: #f778ba; }
.log-type.output { color: #79c0ff; font-style: italic; }
.log-type.init { color: #56d364; font-weight: 700; }
.log-target {
  color: var(--text-primary); word-break: break-all;
  overflow: hidden; text-overflow: ellipsis;
}
.log-snippet {
  margin: 2px 0 2px 4.5rem; padding: 2px 6px;
  background: var(--bg-primary); border: 1px solid var(--border);
  border-radius: 3px; font-size: 0.65rem; color: var(--text-muted);
  overflow: hidden;
  white-space: pre-wrap; word-break: break-all;
  max-height: 10.5em; /* ~10 lines */
}
.log-snippet.think-snippet, .log-snippet.output-snippet {
  color: var(--text-secondary);
}
.log-empty {
  display: flex; align-items: center; justify-content: center;
  height: 100%; color: var(--text-muted); font-size: 0.78rem;
}
.log-disconnect {
  text-align: center; color: var(--text-muted); font-size: 0.68rem;
  padding: 4px 0; border-top: 1px dashed var(--border-subtle);
  margin: 4px 0;
}

/* Log panels container (multi-panel) */
.log-panels-container {
  flex: 1; overflow-y: auto; display: flex; flex-direction: column;
  min-height: 0;
}
.log-panel {
  display: flex; flex-direction: column; border-bottom: 1px solid var(--border-subtle);
  min-height: 80px; flex-shrink: 0;
}
.panel-header-bar {
  display: flex; align-items: center; gap: 0.5rem;
  padding: 0.3rem 0.6rem; background: var(--bg-tertiary);
  border-bottom: 1px solid var(--border-subtle); flex-shrink: 0;
  font-size: 0.78rem;
}
.panel-role-dot {
  width: 6px; height: 6px; border-radius: 50%; display: inline-block;
}
.panel-role-dot.running { background: var(--green); animation: dotPulse 1.5s infinite; }
.panel-role-dot.completed { background: var(--accent); }
.panel-role-dot.idle { background: var(--text-muted); }
.panel-role-name { font-weight: 600; color: var(--text-primary); }
.panel-model-tag { font-size: 0.68rem; color: var(--text-muted); font-weight: 400; margin-left: 0.3rem; }
.panel-time-info { font-size: 0.68rem; color: var(--green); font-weight: 400; margin-left: auto; margin-right: 0.3rem; }
.panel-body {
  flex: 1; overflow-y: auto; padding: 0.3rem 0.5rem;
  font-family: "JetBrains Mono", monospace; font-size: 0.72rem;
}
.panel-body::-webkit-scrollbar { width: 3px; }
.panel-body::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }

/* 直播全部按钮 */
.btn-live-all {
  background: var(--green-dim); color: var(--green); border: 1px solid var(--green);
  border-radius: 10px; padding: 0.15rem 0.6rem; font-size: 0.76rem; font-weight: 600;
  cursor: pointer; transition: all var(--transition); white-space: nowrap;
}
.btn-live-all:hover { background: var(--green); color: var(--bg-primary); }

/* 日志底部状态行 */
.log-footer {
  display: flex; justify-content: space-between; align-items: center;
  padding: 0.2rem 0.6rem; font-size: 0.76rem;
  border-top: 1px solid var(--border-subtle); flex-shrink: 0;
  color: var(--text-muted);
}
.log-footer-left { display: flex; align-items: center; gap: 4px; }
.log-status-dot {
  width: 6px; height: 6px; border-radius: 50%; display: inline-block;
}
.log-status-dot.running { background: var(--green); animation: dotPulse 1.5s infinite; }
.log-status-dot.completed { background: var(--green); }
.log-status-dot.error { background: var(--red); }
.log-status-dot.finished { background: var(--text-muted); }

/* New Session Modal */
.modal-overlay {
  position: fixed; inset: 0; z-index: 2000;
  background: rgba(0,0,0,0.6); backdrop-filter: blur(4px);
  display: flex; align-items: center; justify-content: center;
}
.modal-box {
  background: var(--bg-secondary); border: 1px solid var(--border);
  border-radius: var(--radius-lg); padding: 1.5rem; width: 420px; max-width: 90vw;
  box-shadow: 0 8px 32px rgba(0,0,0,0.5);
}
.modal-title {
  font-size: var(--font-size-xl); font-weight: 700; margin-bottom: 1rem;
  color: var(--text-primary);
}
.modal-label {
  font-size: 0.78rem; color: var(--text-muted); margin-bottom: 0.4rem;
  font-weight: 600;
}
.project-list {
  margin-bottom: 1rem; max-height: 220px; overflow-y: auto;
}
.project-option {
  display: flex; align-items: center; gap: 0.5rem;
  padding: 0.5rem 0.6rem; border-radius: 6px; cursor: pointer;
  transition: background 0.15s; font-size: 0.82rem;
}
.project-option:hover { background: rgba(56,189,248,0.08); }
.project-option.selected { background: rgba(56,189,248,0.15); border: 1px solid var(--accent); }
.project-option input[type="radio"] { accent-color: var(--accent); flex-shrink: 0; }
.project-option .proj-name { font-weight: 600; color: var(--text-primary); }
.project-option .proj-path { font-size: 0.72rem; color: var(--text-muted); font-family: monospace; }
.project-custom-input {
  width: 100%; padding: 0.45rem 0.6rem; margin-top: 0.3rem;
  background: var(--bg-input); border: 1px solid var(--border);
  border-radius: 6px; color: var(--text-primary); font-size: 0.82rem;
  outline: none; font-family: monospace;
  transition: border-color 0.2s;
}
.project-custom-input:focus { border-color: var(--accent); }
.project-custom-input::placeholder { color: var(--text-muted); }
.modal-input {
  width: 100%; padding: 0.45rem 0.6rem; margin-bottom: 1rem;
  background: var(--bg-input); border: 1px solid var(--border);
  border-radius: 6px; color: var(--text-primary); font-size: 0.82rem;
  outline: none; transition: border-color 0.2s;
}
.modal-input:focus { border-color: var(--accent); }
.modal-input::placeholder { color: var(--text-muted); }
.modal-actions { display: flex; gap: 0.5rem; justify-content: flex-end; }
.modal-btn {
  padding: 0.45rem 1.2rem; border: none; border-radius: 6px;
  font-size: 0.82rem; font-weight: 600; cursor: pointer;
  transition: all 0.2s;
}
.modal-btn-cancel {
  background: transparent; color: var(--text-muted);
  border: 1px solid var(--border);
}
.modal-btn-cancel:hover { background: rgba(255,255,255,0.05); }
.modal-btn-create {
  background: linear-gradient(135deg, #0369a1, #0284c7);
  color: white;
}
.modal-btn-create:hover { background: linear-gradient(135deg, #0284c7, #0ea5e9); }
.session-switch-confirm-modal { width: 560px; max-width: 92vw; }
.session-switch-confirm-copy {
  font-size: 0.86rem; color: var(--text-primary); line-height: 1.65;
  margin-bottom: 1rem;
}
.session-switch-confirm-copy strong { color: var(--accent); }
.session-switch-confirm-lines {
  display: flex; flex-direction: column; gap: 0.45rem;
  margin: 0 0 1rem; padding: 0.8rem 0.9rem;
  border: 1px solid var(--border); border-radius: 8px;
  background: rgba(255,255,255,0.02);
}
.session-switch-confirm-line {
  font-size: 0.8rem; color: var(--text-muted); line-height: 1.55;
}
.session-switch-confirm-target {
  font-size: 0.78rem; color: var(--text-muted); margin-bottom: 0.8rem;
  word-break: break-word;
}
/* Settings Page */
.settings-view {
  flex: 1; display: flex; flex-direction: column; overflow: hidden;
  background: var(--bg-primary);
}
.settings-page-header {
  height: var(--toolbar-h); background: var(--bg-secondary);
  border-bottom: 1px solid var(--border-subtle);
  display: flex; align-items: center; padding: 0 1rem; gap: 0.8rem;
  flex-shrink: 0;
}
.settings-page-header h2 { margin: 0; font-size: 1rem; font-weight: 700; flex: 1; }
.settings-back-btn {
  background: var(--bg-tertiary); border: 1px solid var(--border);
  color: var(--text-secondary); padding: 0.35rem 0.8rem;
  border-radius: var(--radius-sm); cursor: pointer; font-size: 0.82rem;
  transition: all var(--transition);
}
.settings-back-btn:hover { background: var(--bg-hover); color: var(--accent); border-color: var(--accent); }
.settings-page-shell {
  flex: 1; display: flex; overflow: hidden;
}
.settings-sidebar {
  width: 220px; border-right: 1px solid var(--border-subtle);
  background: var(--bg-secondary); padding: 0.8rem; overflow-y: auto;
  display: flex; flex-direction: column; gap: 0.4rem; flex-shrink: 0;
}
.settings-nav-item {
  position: relative;
  width: 100%; text-align: left; padding: 0.8rem 0.9rem;
  background: transparent; border: 1px solid transparent; border-radius: var(--radius-sm);
  color: var(--text-secondary); cursor: pointer; font-size: 0.84rem;
  transition: all var(--transition);
}
.settings-nav-item:hover { background: var(--bg-hover); color: var(--text-primary); }
.settings-nav-item.dirty::after {
  content: ''; position: absolute; top: 50%; right: 0.75rem; transform: translateY(-50%);
  width: 7px; height: 7px; border-radius: 50%; background: var(--orange, #f59e0b);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--orange, #f59e0b) 18%, transparent);
}
.settings-nav-item.active {
  background: var(--accent-dim); color: var(--accent); border-color: color-mix(in srgb, var(--accent) 35%, transparent);
}
.settings-main {
  flex: 1; overflow: hidden; display: flex; flex-direction: column;
}
.settings-panel {
  flex: 1; display: none; flex-direction: column; min-height: 0;
}
.settings-panel.active { display: flex; }
.settings-panel-scroll {
  flex: 1; overflow-y: auto; padding: 1.2rem 1.4rem 1rem;
}
.settings-panel-title {
  font-size: 1.08rem; font-weight: 700; color: var(--text-primary); margin-bottom: 0.35rem;
}
.settings-panel-desc {
  font-size: 0.82rem; color: var(--text-muted); line-height: 1.5; margin-bottom: 1rem;
}
.settings-subsection {
  background: var(--bg-secondary); border: 1px solid var(--border-subtle);
  border-radius: var(--radius); padding: 1rem; margin-bottom: 1rem;
}
.settings-subsection-title {
  font-size: 0.8rem; font-weight: 700; color: var(--text-secondary); margin-bottom: 0.6rem;
}
.settings-page-actions {
  padding: 0.9rem 1.4rem; border-top: 1px solid var(--border-subtle);
  display: flex; align-items: center; justify-content: flex-end; gap: 0.6rem;
  flex-wrap: wrap; background: var(--bg-secondary); flex-shrink: 0;
}
.settings-actions-state {
  margin-right: auto; font-size: 0.78rem; color: var(--text-muted);
}
.settings-actions-state.dirty { color: var(--orange, #f59e0b); }
.settings-actions-state.clean { color: var(--text-muted); }
.settings-btn-secondary {
  padding: 0.55rem 0.95rem; background: none; border: 1px solid var(--border);
  border-radius: var(--radius-sm); color: var(--text-secondary);
  font-size: 0.82rem; cursor: pointer; transition: all var(--transition);
}
.settings-btn-secondary:hover { background: var(--bg-hover); color: var(--text-primary); border-color: var(--text-muted); }
.settings-btn-secondary:disabled {
  opacity: 0.5; cursor: not-allowed; background: none; color: var(--text-muted); border-color: var(--border);
}
.settings-field { margin-bottom: 1rem; }
.settings-field label {
  display: block; font-size: 0.8rem; color: var(--text-muted); margin-bottom: 0.3rem;
}
.settings-field input, .settings-field select {
  width: 100%; padding: 0.6rem 0.8rem; background: var(--bg-input);
  border: 1px solid var(--border); border-radius: 6px; color: var(--text-primary);
  font-size: 0.9rem; outline: none;
}
.settings-field input:focus, .settings-field select:focus { border-color: var(--accent); }
.settings-guide-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 0.7rem; margin: 0.2rem 0 0.9rem;
}
.settings-guide-card {
  border: 1px solid var(--border-subtle);
  background: color-mix(in srgb, var(--bg-secondary) 86%, transparent);
  border-radius: 10px; padding: 0.8rem 0.9rem;
}
.settings-guide-title {
  display: flex; align-items: center; justify-content: space-between; gap: 0.5rem;
  margin-bottom: 0.45rem;
}
.settings-guide-title strong {
  font-size: 0.84rem; color: var(--text-primary);
}
.settings-guide-badge {
  display: inline-flex; align-items: center;
  padding: 0.08rem 0.45rem; border-radius: 999px;
  font-size: 0.66rem; line-height: 1.2;
  border: 1px solid var(--border-subtle); color: var(--text-muted);
}
.settings-guide-badge.ok {
  color: var(--green); border-color: color-mix(in srgb, var(--green) 30%, transparent);
  background: color-mix(in srgb, var(--green) 10%, transparent);
}
.settings-guide-badge.warn {
  color: var(--orange, #f59e0b);
  border-color: color-mix(in srgb, var(--orange, #f59e0b) 30%, transparent);
  background: color-mix(in srgb, var(--orange, #f59e0b) 10%, transparent);
}
.settings-guide-body {
  font-size: 0.78rem; line-height: 1.6; color: var(--text-muted);
}
.settings-guide-body code { color: var(--text-secondary); }
.settings-guide-row {
  display: flex; gap: 0.5rem; margin-top: 0.2rem;
}
.settings-guide-key {
  min-width: 5.2rem; color: var(--text-secondary);
}
.settings-guide-value {
  flex: 1; color: var(--text-muted);
}
.settings-save {
  padding: 0.65rem 1.1rem; background: linear-gradient(135deg, #0369a1, #0284c7);
  border: none; border-radius: 6px; color: white; font-size: 0.9rem; font-weight: 600;
  cursor: pointer; transition: all 0.2s;
}
.settings-save:hover { background: linear-gradient(135deg, #0284c7, #0ea5e9); }
.settings-save:disabled {
  opacity: 0.55; cursor: not-allowed;
  background: linear-gradient(135deg, #164e63, #155e75);
}
.settings-note {
  margin-bottom: 0.8rem; padding: 0.75rem 0.85rem; border-radius: var(--radius-sm);
  font-size: 0.8rem; line-height: 1.5;
}
.settings-note.warn {
  background: color-mix(in srgb, var(--orange, #f59e0b) 10%, transparent);
  border: 1px solid color-mix(in srgb, var(--orange, #f59e0b) 28%, transparent);
  color: var(--orange, #f59e0b);
}
.settings-note.error {
  background: color-mix(in srgb, var(--danger, #ef4444) 10%, transparent);
  border: 1px solid color-mix(in srgb, var(--danger, #ef4444) 28%, transparent);
  color: var(--danger, #ef4444);
}
.settings-note.info {
  background: color-mix(in srgb, var(--accent) 10%, transparent);
  border: 1px solid color-mix(in srgb, var(--accent) 28%, transparent);
  color: var(--text-secondary);
}
.mcp-service-list {
  display: flex; flex-direction: column; gap: 0.65rem;
}
.mcp-service-card {
  border: 1px solid var(--border-subtle);
  background: color-mix(in srgb, var(--bg-secondary) 90%, transparent);
  border-radius: 12px;
  padding: 0.95rem;
}
.mcp-service-head {
  display: flex; align-items: flex-start; justify-content: space-between; gap: 0.8rem;
  margin-bottom: 0.55rem;
}
.mcp-service-title {
  font-size: 0.95rem; font-weight: 700; color: var(--text-primary);
}
.mcp-service-desc {
  margin-top: 0.2rem; font-size: 0.79rem; line-height: 1.55; color: var(--text-muted);
}
.mcp-service-badge {
  display: inline-flex; align-items: center; padding: 0.14rem 0.55rem; border-radius: 999px;
  font-size: 0.7rem; border: 1px solid var(--border-subtle); white-space: nowrap;
}
.mcp-service-badge.ready, .mcp-service-badge.connected {
  color: var(--green); border-color: color-mix(in srgb, var(--green) 30%, transparent);
  background: color-mix(in srgb, var(--green) 10%, transparent);
}
.mcp-service-badge.waiting {
  color: var(--orange, #f59e0b); border-color: color-mix(in srgb, var(--orange, #f59e0b) 30%, transparent);
  background: color-mix(in srgb, var(--orange, #f59e0b) 10%, transparent);
}
.mcp-service-badge.disabled {
  color: var(--text-muted); border-color: var(--border); background: var(--bg-tertiary);
}
.mcp-service-badge.missing, .mcp-service-badge.error {
  color: var(--danger, #ef4444); border-color: color-mix(in srgb, var(--danger, #ef4444) 30%, transparent);
  background: color-mix(in srgb, var(--danger, #ef4444) 10%, transparent);
}
.mcp-service-status-wrap { position: relative; flex-shrink: 0; }
.mcp-service-status-trigger {
  background: none; cursor: pointer;
}
.mcp-service-status-popup {
  display: none; position: absolute; top: calc(100% + 6px); right: 0;
  min-width: 250px; max-width: 320px; z-index: 20;
  background: var(--bg-secondary); border: 1px solid var(--border);
  border-radius: 10px; padding: 0.8rem 0.9rem; box-shadow: 0 4px 16px rgba(0,0,0,.3);
}
.mcp-service-status-popup.show { display: block; }
.mcp-service-status-popup-title {
  font-size: 0.82rem; color: var(--text-primary); font-weight: 700; margin-bottom: 0.45rem;
}
.mcp-service-status-popup-body {
  font-size: 0.76rem; color: var(--text-muted); line-height: 1.55;
}
.mcp-service-status-popup .info-row { display:flex; justify-content:space-between; gap:0.6rem; padding:2px 0; }
.mcp-service-status-popup .info-val { color: var(--text-primary); text-align: right; }
.mcp-service-status-popup .connect-link { display:block; margin-top:8px; color:var(--accent); text-decoration:none; }
.mcp-service-status-popup .connect-link:hover { text-decoration:underline; }
.mcp-service-meta {
  font-size: 0.76rem; color: var(--text-secondary); margin-bottom: 0.35rem;
}
.mcp-service-roles {
  font-size: 0.74rem; color: var(--text-muted); line-height: 1.5; margin-bottom: 0.55rem;
}
.mcp-service-helper {
  font-size: 0.76rem; color: var(--text-muted); line-height: 1.55;
  background: color-mix(in srgb, var(--bg-tertiary) 78%, transparent);
  border-radius: 8px; padding: 0.55rem 0.65rem; margin-bottom: 0.6rem;
}
.mcp-service-actions {
  display: flex; flex-wrap: wrap; gap: 0.45rem;
}
.mcp-service-actions .settings-btn-secondary[data-enabled="0"] {
  border-color: color-mix(in srgb, var(--green) 30%, transparent);
  color: var(--green);
}
.mcp-service-actions .settings-btn-secondary[data-enabled="1"] {
  border-color: color-mix(in srgb, var(--orange, #f59e0b) 30%, transparent);
}
.mcp-add-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 0.8rem;
}
.mcp-add-card {
  border: 1px solid var(--border-subtle);
  background: color-mix(in srgb, var(--bg-secondary) 88%, transparent);
  border-radius: 10px;
  padding: 0.9rem;
}
.mcp-add-title {
  font-size: 0.84rem; font-weight: 700; color: var(--text-primary); margin-bottom: 0.3rem;
}
.mcp-add-desc {
  font-size: 0.76rem; color: var(--text-muted); line-height: 1.55; margin-bottom: 0.7rem;
}
.settings-field textarea {
  width: 100%; min-height: 120px; resize: vertical; padding: 0.7rem 0.8rem;
  background: var(--bg-input); border: 1px solid var(--border); border-radius: 6px;
  color: var(--text-primary); font-size: 0.86rem; outline: none; font-family: inherit;
}
.settings-field textarea:focus { border-color: var(--accent); }
.mcp-role-page-note {
  margin-bottom: 0.75rem;
}
@media (max-width: 900px) {
  .settings-page-shell { flex-direction: column; }
  .settings-sidebar {
    width: auto; border-right: none; border-bottom: 1px solid var(--border-subtle);
    padding: 0.75rem 0.8rem; flex-direction: row; flex-wrap: nowrap;
    overflow-x: auto; overflow-y: hidden;
  }
  .settings-nav-item {
    width: auto; flex: 0 0 auto; white-space: nowrap;
  }
}
@media (max-width: 640px) {
  .settings-page-header { padding: 0 0.8rem; }
  .settings-panel-scroll { padding: 1rem 0.9rem 0.9rem; }
  .settings-page-actions { padding: 0.8rem 0.9rem; justify-content: stretch; }
  .settings-actions-state { width: 100%; margin-right: 0; }
  .settings-page-actions .settings-save,
  .settings-page-actions .settings-btn-secondary { width: 100%; }
  .settings-nav-item { width: auto; }
}
/* Model Config Tab */
.model-table { width: 100%; }
.model-row {
  display: flex; align-items: center; justify-content: space-between;
  padding: 0.5rem 0; border-bottom: 1px solid var(--border);
}
.model-row:last-child { border-bottom: none; }
.model-row { flex-wrap: wrap; }
.mcp-chips-row {
  width: 100%; display: flex; align-items: center; gap: 0.35rem; flex-wrap: wrap;
  padding: 0.3rem 0 0.5rem; border-bottom: 1px solid var(--border);
}
.mcp-chips-label {
  font-size: 0.68rem; color: var(--text-muted); white-space: nowrap; min-width: 28px;
}
.mcp-chip {
  padding: 0.12rem 0.5rem; border-radius: 3px; font-size: 0.71rem; cursor: pointer;
  border: 1px solid var(--border); color: var(--text-muted);
  background: var(--bg-input); transition: all 0.15s; user-select: none;
}
.mcp-chip:hover { border-color: var(--accent); color: var(--text-secondary); }
.mcp-chip.active {
  background: rgba(56,189,248,0.12); border-color: var(--accent); color: var(--accent);
}
.mcp-chip.disabled {
  cursor: not-allowed;
  opacity: 0.65;
  border-style: dashed;
}
.mcp-chip.disabled:hover {
  border-color: var(--border);
  color: var(--text-muted);
}
.mcp-chip.active.disabled {
  background: color-mix(in srgb, var(--orange, #f59e0b) 12%, transparent);
  border-color: color-mix(in srgb, var(--orange, #f59e0b) 28%, transparent);
  color: var(--orange, #f59e0b);
}
.model-role {
  font-size: 0.85rem; color: var(--text-primary); flex: 1;
}
.model-default {
  font-size: 0.7rem; color: var(--text-muted); margin-left: 0.5rem;
}
.model-select {
  padding: 0.4rem 0.6rem; background: var(--bg-input);
  border: 1px solid var(--border); border-radius: 6px;
  color: var(--text-primary); font-size: 0.8rem; outline: none;
  min-width: 180px;
}
.reasoning-select {
  min-width: 120px;
  margin-left: 0.4rem;
}
.model-select:focus { border-color: var(--accent); }
.model-info {
  font-size: 0.75rem; color: var(--text-muted); margin-bottom: 0.8rem;
  padding: 0.5rem 0.8rem; background: rgba(3,105,161,0.1); border-radius: 6px;
}
/* External Models Section */
.ext-section-label {
  font-size: 0.72rem; font-weight: 700; color: var(--text-secondary);
  text-transform: uppercase; letter-spacing: 0.06em;
  padding: 0.7rem 0 0.4rem;
  border-top: 1px solid var(--border-subtle);
  margin-top: 0.8rem;
}
.ext-section-label:first-child { border-top: none; margin-top: 0; padding-top: 0; }
.ext-model-card {
  border: 1px solid var(--border-subtle); border-radius: 6px;
  padding: 0.65rem 0.8rem; margin-bottom: 0.5rem;
  transition: border-color var(--transition);
}
.ext-model-card.configured { border-color: rgba(34,197,94,0.3); }
/* Saved Connection Cards */
.conn-card {
  position: relative;
  display: flex; flex-direction: column; gap: 0.18rem;
  min-width: 148px; max-width: 220px;
  padding: 0.5rem 0.75rem; border-radius: 6px; font-size: 0.75rem;
  cursor: pointer; border: 1px solid var(--border); color: var(--text-muted);
  background: var(--bg-input); transition: all 0.15s;
}
.conn-card:hover { border-color: var(--accent); color: var(--text-secondary); }
.conn-card.active { background: rgba(56,189,248,0.12); border-color: var(--accent); color: var(--accent); font-weight: 600; }
.conn-card-title {
  display: flex; align-items: center; gap: 0.35rem;
  min-width: 0; padding-right: 1rem;
}
.conn-card-state {
  display: inline-flex; align-items: center;
  padding: 0.08rem 0.35rem; border-radius: 999px;
  font-size: 0.62rem; line-height: 1.2;
  color: var(--accent); background: rgba(56,189,248,0.14);
  border: 1px solid rgba(56,189,248,0.26);
}
.conn-card-name {
  font-size: 0.78rem; color: var(--text-primary);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.conn-card.active .conn-card-name { color: var(--accent); }
.conn-card-meta {
  display: flex; align-items: center; gap: 0.35rem; flex-wrap: wrap;
}
.conn-card-badge {
  display: inline-flex; align-items: center;
  padding: 0.08rem 0.38rem; border-radius: 999px;
  font-size: 0.64rem; line-height: 1.2;
  border: 1px solid var(--border-subtle);
  color: var(--text-muted); background: rgba(148,163,184,0.08);
}
.conn-card.active .conn-card-badge {
  color: var(--accent);
  border-color: rgba(56,189,248,0.24);
  background: rgba(56,189,248,0.12);
}
.conn-card-host {
  font-size: 0.65rem; color: var(--text-muted);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.conn-auth-row {
  display: flex; align-items: center; gap: 0.35rem; flex-wrap: wrap;
  margin-top: 0.1rem;
}
.conn-auth-badge {
  display: inline-flex; align-items: center;
  padding: 0.08rem 0.38rem; border-radius: 999px;
  font-size: 0.64rem; line-height: 1.2;
  border: 1px solid var(--border-subtle);
  color: var(--text-muted); background: rgba(148,163,184,0.08);
}
.conn-auth-badge.ready {
  color: var(--green); border-color: rgba(34,197,94,0.26); background: rgba(34,197,94,0.12);
}
.conn-auth-badge.waiting {
  color: var(--warning); border-color: rgba(245,158,11,0.26); background: rgba(245,158,11,0.12);
}
.conn-auth-badge.missing, .conn-auth-badge.error {
  color: var(--danger); border-color: rgba(239,68,68,0.26); background: rgba(239,68,68,0.1);
}
.conn-auth-action {
  border: 1px solid var(--border-subtle);
  background: rgba(148,163,184,0.08);
  color: var(--text-muted);
  border-radius: 999px;
  padding: 0.08rem 0.45rem;
  font-size: 0.64rem;
  cursor: pointer;
}
.conn-auth-action:hover { border-color: var(--accent); color: var(--accent); }
.conn-card .conn-delete {
  display: none; position: absolute; top: 0.35rem; right: 0.45rem;
  color: var(--text-muted); font-size: 0.9rem; line-height: 1;
}
.conn-card:hover .conn-delete { display: inline; }
.conn-card .conn-delete:hover { color: var(--red); }
.connection-auth-modal { width: 560px; max-width: 92vw; }
.connection-auth-modal-actions { flex-wrap: wrap; justify-content: flex-end; }
.auth-status-row {
  display: flex; align-items: center; gap: 0.45rem; flex-wrap: wrap;
  margin-bottom: 0.6rem;
}
.auth-status-badge {
  display: inline-flex; align-items: center;
  padding: 0.12rem 0.48rem; border-radius: 999px;
  font-size: 0.72rem; line-height: 1.2;
  border: 1px solid var(--border-subtle);
  color: var(--text-muted); background: rgba(148,163,184,0.08);
}
.auth-status-badge.ready {
  color: var(--green); border-color: rgba(34,197,94,0.26); background: rgba(34,197,94,0.12);
}
.auth-status-badge.waiting {
  color: var(--warning); border-color: rgba(245,158,11,0.26); background: rgba(245,158,11,0.12);
}
.auth-status-badge.missing, .auth-status-badge.error, .auth-status-badge.idle {
  color: var(--danger); border-color: rgba(239,68,68,0.26); background: rgba(239,68,68,0.1);
}
.auth-device-box {
  margin-top: 0.75rem;
  padding: 0.7rem 0.8rem;
  border-radius: 8px;
  border: 1px solid var(--border-subtle);
  background: rgba(15,23,42,0.18);
}
.auth-device-code {
  font-size: 1rem;
  font-weight: 700;
  letter-spacing: 0.08em;
  color: var(--text-primary);
  margin-top: 0.35rem;
}
/* API Source Label in Role Table */
.api-source-label {
  font-size: 0.65rem; padding: 0.1rem 0.4rem; border-radius: 3px;
  white-space: nowrap; margin-left: 0.4rem;
}
.api-source-label.default { background: rgba(56,189,248,0.1); color: var(--accent); }
.api-source-label.external { background: rgba(251,146,60,0.1); color: #fb923c; }
.ext-model-name {
  display: flex; align-items: center; gap: 0.4rem;
  font-size: 0.85rem; font-weight: 600; color: var(--text-primary);
  margin-bottom: 0.45rem;
}
.ext-dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--text-muted); flex-shrink: 0;
}
.ext-dot.on { background: var(--green); box-shadow: 0 0 4px var(--green); }
.ext-key-display { font-size: 0.78rem; color: var(--text-muted); font-family: monospace; flex: 1; }
.ext-edit-btn { background: none; border: 1px solid var(--border); color: var(--text-muted); border-radius: 3px; padding: 0.1rem 0.4rem; font-size: 0.73rem; cursor: pointer; white-space: nowrap; }
.ext-edit-btn:hover { color: var(--accent); border-color: var(--accent); }
.ext-model-fields { display: flex; flex-direction: column; gap: 0.35rem; }
.ext-field-row { display: flex; align-items: center; gap: 0.4rem; }
.ext-field-label {
  font-size: 0.72rem; color: var(--text-muted); width: 4.5rem; flex-shrink: 0;
}
.ext-field-input {
  flex: 1; padding: 0.3rem 0.5rem;
  background: var(--bg-input); border: 1px solid var(--border);
  border-radius: 4px; color: var(--text-primary);
  font-size: 0.78rem; outline: none; transition: border-color var(--transition);
}
.ext-field-input:focus { border-color: var(--accent); }
.ext-clear-btn {
  padding: 0.25rem 0.5rem;
  background: none; border: 1px solid var(--border);
  border-radius: 4px; color: var(--text-muted);
  font-size: 0.72rem; cursor: pointer; white-space: nowrap;
  transition: all var(--transition);
}
.ext-clear-btn:hover { border-color: var(--red); color: var(--red); }
.ext-key-display {
  flex: 1; font-size: 0.78rem; color: var(--text-muted);
  letter-spacing: 0.03em; padding: 0.28rem 0.4rem;
}
.ext-edit-btn {
  padding: 0.25rem 0.5rem;
  background: none; border: 1px solid var(--border);
  border-radius: 4px; color: var(--accent);
  font-size: 0.72rem; cursor: pointer; white-space: nowrap;
  transition: all var(--transition);
}
.ext-edit-btn:hover { background: var(--accent-dim); }

/* ====== Project Manager ====== */
.sidebar-projects-btn {
  padding: 0.7rem 1rem; background: var(--bg-tertiary);
  border: none; border-top: 1px solid var(--border-subtle);
  color: var(--text-secondary); cursor: pointer; font-size: 0.85rem;
  display: flex; align-items: center; gap: 0.5rem;
  transition: all var(--transition); flex-shrink: 0;
}
.sidebar-projects-btn:hover { background: var(--bg-hover); color: var(--accent); }
.sidebar-agents-btn {
  padding: 0.7rem 1rem; background: var(--bg-tertiary);
  border: none; border-top: 1px solid var(--border-subtle);
  color: var(--text-secondary); cursor: pointer; font-size: 0.85rem;
  display: flex; align-items: center; gap: 0.5rem;
  transition: all var(--transition); flex-shrink: 0;
}
.sidebar-agents-btn:hover { background: var(--bg-hover); color: var(--accent); }

/* === Agent Management Page === */
.agents-view {
  flex: 1; display: flex; flex-direction: column; overflow: hidden;
  background: var(--bg-primary); position: relative;
}
.agents-header {
  height: var(--toolbar-h); background: var(--bg-secondary);
  border-bottom: 1px solid var(--border-subtle);
  display: flex; align-items: center; padding: 0 1rem; gap: 0.8rem;
  flex-shrink: 0;
}
.agents-header h2 { font-size: 1rem; font-weight: 700; flex: 1; }
.agents-back-btn, .agents-create-btn {
  background: var(--bg-tertiary); border: 1px solid var(--border);
  color: var(--text-secondary); padding: 0.35rem 0.8rem;
  border-radius: var(--radius-sm); cursor: pointer; font-size: 0.82rem;
  transition: all var(--transition);
}
.agents-back-btn:hover, .agents-create-btn:hover { background: var(--bg-hover); color: var(--accent); border-color: var(--accent); }
.agents-filter {
  padding: 0.6rem 1rem; display: flex; align-items: center; gap: 0.8rem;
  border-bottom: 1px solid var(--border-subtle); flex-shrink: 0;
}
.agents-search {
  flex: 1; background: var(--bg-input); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 0.4rem 0.7rem;
  color: var(--text-primary); font-size: 0.82rem; outline: none;
  transition: border-color var(--transition);
}
.agents-search:focus { border-color: var(--accent); }
.agents-filter-tags { display: flex; gap: 0.3rem; }
.agents-tag {
  background: var(--bg-tertiary); border: 1px solid var(--border);
  color: var(--text-muted); padding: 0.25rem 0.6rem; border-radius: 12px;
  cursor: pointer; font-size: 0.75rem; transition: all var(--transition);
}
.agents-tag:hover { color: var(--text-secondary); border-color: var(--text-muted); }
.agents-tag.active { background: var(--accent-dim); color: var(--accent); border-color: var(--accent); }
.agents-grid {
  flex: 1; overflow-y: auto; padding: 1rem;
  display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 0.8rem; align-content: start;
}
.agents-card {
  background: var(--bg-secondary); border: 1px solid var(--border-subtle);
  border-radius: var(--radius); padding: 1rem; cursor: pointer;
  transition: all var(--transition); position: relative;
}
.agents-card:hover { border-color: var(--accent); background: var(--bg-hover); }
.agents-card-icon { font-size: var(--font-size-2xl); margin-bottom: 0.5rem; }
.agents-card-name { font-size: 0.9rem; font-weight: 600; color: var(--text-primary); margin-bottom: 0.3rem; }
.agents-card-desc { font-size: 0.78rem; color: var(--text-muted); line-height: 1.4; margin-bottom: 0.5rem; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.agents-card-meta { font-size: 0.72rem; color: var(--text-muted); }
.agents-badge {
  position: absolute; top: 0.6rem; right: 0.6rem;
  font-size: 0.65rem; padding: 1px 6px; border-radius: 8px; font-weight: 600;
}
.agents-badge-builtin { background: rgba(96,165,250,0.15); color: #60a5fa; }
.agents-badge-user { background: rgba(34,197,94,0.15); color: #4ade80; }
.agents-card-menu-btn {
  position: absolute; top: 0.6rem; right: 3.5rem;
  width: 24px; height: 24px; display: flex; align-items: center; justify-content: center;
  border-radius: var(--radius-sm); cursor: pointer; color: var(--text-muted);
  font-size: var(--font-size-lg); transition: all var(--transition); opacity: 0;
}
.agents-card:hover .agents-card-menu-btn { opacity: 1; }
.agents-card-menu-btn:hover { background: var(--bg-tertiary); color: var(--text-primary); }
.agents-menu {
  display: none; position: absolute; top: 2.2rem; right: 3rem;
  background: var(--bg-card); border: 1px solid var(--border);
  border-radius: var(--radius-sm); box-shadow: var(--shadow);
  z-index: 100; min-width: 120px; overflow: hidden;
}
.agents-menu.open { display: block; }
.agents-menu button {
  display: block; width: 100%; padding: 0.5rem 0.8rem;
  background: none; border: none; color: var(--text-secondary);
  font-size: 0.8rem; cursor: pointer; text-align: left;
  transition: background var(--transition);
}
.agents-menu button:hover { background: var(--bg-hover); color: var(--text-primary); }
.agents-menu-danger:hover { color: var(--red) !important; background: var(--red-dim) !important; }
.agents-empty { text-align: center; padding: 3rem 1rem; color: var(--text-muted); font-size: var(--font-size-lg); grid-column: 1 / -1; }

/* Agent Drawer (right slide) */
.agents-drawer {
  position: absolute; top: 0; right: 0; bottom: 0; width: 480px;
  background: var(--bg-secondary); border-left: 1px solid var(--border);
  transform: translateX(100%); transition: transform var(--transition);
  z-index: 200; display: flex; flex-direction: column; overflow: hidden;
}
.agents-drawer.open { transform: translateX(0); }
.agents-drawer-header {
  padding: 1rem; border-bottom: 1px solid var(--border-subtle);
  display: flex; align-items: center; gap: 0.8rem; flex-shrink: 0;
}
.agents-drawer-close {
  background: none; border: none; color: var(--text-muted); cursor: pointer;
  font-size: var(--font-size-xl); padding: 4px; border-radius: var(--radius-sm);
}
.agents-drawer-close:hover { color: var(--text-primary); background: var(--bg-hover); }
.agents-drawer-title { font-size: var(--font-size-xl); font-weight: 700; flex: 1; }
.agents-drawer-actions { display: flex; gap: 0.4rem; }
.agents-drawer-actions button {
  background: var(--bg-tertiary); border: 1px solid var(--border);
  color: var(--text-secondary); padding: 0.3rem 0.6rem;
  border-radius: var(--radius-sm); cursor: pointer; font-size: 0.78rem;
  transition: all var(--transition);
}
.agents-drawer-actions button:hover { background: var(--bg-hover); color: var(--accent); border-color: var(--accent); }
.agents-drawer-body { flex: 1; overflow-y: auto; padding: 1rem; }
.agents-drawer-section { margin-bottom: 1.2rem; }
.agents-drawer-section-title { font-size: 0.75rem; color: var(--text-muted); font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 0.5rem; }
.agents-drawer-info { font-size: 0.85rem; color: var(--text-secondary); line-height: 1.5; }
.agents-wf-card {
  background: var(--bg-tertiary); border-radius: var(--radius-sm);
  padding: 0.7rem; margin-bottom: 0.5rem;
}
.agents-wf-name { font-size: 0.85rem; font-weight: 600; color: var(--text-primary); margin-bottom: 0.3rem; }
.agents-wf-desc { font-size: 0.78rem; color: var(--text-muted); margin-bottom: 0.4rem; }
.agents-wf-steps { font-size: 0.75rem; color: var(--text-muted); }
.agents-step-row {
  display: flex; align-items: center; gap: 0.5rem; padding: 0.3rem 0;
  border-bottom: 1px solid var(--border-subtle);
}
.agents-step-row:last-child { border-bottom: none; }
.agents-step-num { width: 20px; height: 20px; border-radius: 50%; background: var(--accent-dim); color: var(--accent); font-size: 0.68rem; display: flex; align-items: center; justify-content: center; font-weight: 600; flex-shrink: 0; }
.agents-step-name { flex: 1; font-size: 0.78rem; color: var(--text-primary); }
.agents-step-model { font-size: 0.68rem; color: var(--text-muted); background: var(--bg-primary); padding: 1px 5px; border-radius: 4px; }

/* Agent Create/Edit Panel (right slide, wider) */
.agents-panel {
  position: absolute; top: 0; right: 0; bottom: 0; width: 600px;
  background: var(--bg-secondary); border-left: 1px solid var(--border);
  transform: translateX(100%); transition: transform var(--transition);
  z-index: 300; display: flex; flex-direction: column; overflow: hidden;
}
.agents-panel.open { transform: translateX(0); }
.agents-panel-header {
  padding: 1rem; border-bottom: 1px solid var(--border-subtle);
  display: flex; align-items: center; gap: 0.8rem; flex-shrink: 0;
}
.agents-panel-close {
  background: none; border: none; color: var(--text-muted); cursor: pointer;
  font-size: var(--font-size-xl); padding: 4px; border-radius: var(--radius-sm);
}
.agents-panel-close:hover { color: var(--text-primary); background: var(--bg-hover); }
.agents-panel-title { font-size: var(--font-size-xl); font-weight: 700; flex: 1; }
.agents-panel-body { flex: 1; overflow-y: auto; padding: 1rem; }
.agents-panel-footer {
  padding: 0.8rem 1rem; border-top: 1px solid var(--border-subtle);
  display: flex; justify-content: flex-end; gap: 0.5rem; flex-shrink: 0;
}
.agents-panel-footer button {
  padding: 0.45rem 1rem; border-radius: var(--radius-sm);
  cursor: pointer; font-size: 0.85rem; transition: all var(--transition);
}
.agents-btn-primary { background: var(--accent); border: none; color: #fff; font-weight: 600; }
.agents-btn-primary:hover { opacity: 0.9; }
.agents-btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
.agents-btn-secondary { background: none; border: 1px solid var(--border); color: var(--text-secondary); }
.agents-btn-secondary:hover { background: var(--bg-hover); color: var(--text-primary); }

/* Create panel form */
.agents-form-group { margin-bottom: 1rem; }
.agents-form-label { display: block; font-size: 0.8rem; color: var(--text-secondary); margin-bottom: 0.3rem; font-weight: 600; }
.agents-form-input, .agents-form-textarea, .agents-form-select {
  width: 100%; background: var(--bg-input); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 0.5rem 0.7rem;
  color: var(--text-primary); font-size: 0.85rem; outline: none;
  transition: border-color var(--transition); font-family: inherit;
}
.agents-form-input:focus, .agents-form-textarea:focus, .agents-form-select:focus { border-color: var(--accent); }
.agents-form-textarea { resize: vertical; min-height: 80px; }
.agents-form-hint { font-size: 0.72rem; color: var(--text-muted); margin-top: 0.2rem; }
.agents-form-error { font-size: 0.72rem; color: var(--red); margin-top: 0.2rem; }
.agents-form-input.error { border-color: var(--red); }
.agents-tools-grid {
  display: flex; flex-wrap: wrap; gap: 0.4rem 0.8rem; margin-top: 0.3rem;
}
.agents-tool-item {
  display: flex; align-items: center; gap: 0.3rem; font-size: 0.82rem; color: var(--text-primary); cursor: pointer;
}
.agents-tool-item input[type="checkbox"] { accent-color: var(--accent); cursor: pointer; }
.agents-tool-warn {
  display: flex; align-items: center; gap: 0.3rem; margin-top: 0.4rem;
  font-size: 0.72rem; color: var(--orange, #f59e0b); padding: 0.3rem 0.5rem;
  background: color-mix(in srgb, var(--orange, #f59e0b) 10%, transparent);
  border-radius: var(--radius-sm);
}

/* AI generate loading */
.agents-loading {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  padding: 3rem 1rem; color: var(--text-muted);
}
.agents-loading-spinner {
  width: 32px; height: 32px; border: 3px solid var(--border);
  border-top-color: var(--accent); border-radius: 50%;
  animation: agentsSpin 0.8s linear infinite; margin-bottom: 1rem;
}
@keyframes agentsSpin { to { transform: rotate(360deg); } }

/* Preview step list in create panel */
.agents-preview-step {
  display: flex; align-items: center; gap: 0.5rem; padding: 0.5rem;
  background: var(--bg-tertiary); border-radius: var(--radius-sm);
  margin-bottom: 0.4rem;
}
.agents-preview-step-name { flex: 1; font-size: 0.82rem; color: var(--text-primary); }
.agents-preview-step-model {
  font-size: 0.75rem; background: var(--bg-input); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 0.2rem 0.4rem; color: var(--text-secondary);
}

/* Overlay for drawer/panel backdrop */
.agents-overlay {
  position: absolute; top: 0; left: 0; right: 0; bottom: 0;
  background: rgba(0,0,0,0.3); z-index: 150; display: none;
}
.agents-overlay.open { display: block; }

#project-manager-view {
  flex: 1; display: flex; flex-direction: column; overflow: hidden;
  background: var(--bg-primary);
}
.pm-topbar {
  height: var(--toolbar-h); background: var(--bg-secondary);
  border-bottom: 1px solid var(--border-subtle);
  display: flex; align-items: center; padding: 0 1rem; gap: 0.8rem;
  flex-shrink: 0;
}
.pm-topbar-title { font-weight: 700; font-size: 1rem; flex: 1; }
.pm-topbar button {
  background: var(--bg-tertiary); border: 1px solid var(--border);
  color: var(--text-secondary); padding: 0.35rem 0.8rem;
  border-radius: var(--radius-sm); cursor: pointer; font-size: 0.82rem;
  transition: all var(--transition);
}
.pm-topbar button:hover { background: var(--bg-hover); color: var(--accent); border-color: var(--accent); }
.pm-content {
  flex: 1; display: flex; overflow: hidden;
}
.pm-sidebar {
  width: 240px; border-right: 1px solid var(--border-subtle);
  display: flex; flex-direction: column; overflow-y: auto;
  background: var(--bg-secondary); flex-shrink: 0;
}
.pm-project-item {
  padding: 0.7rem 1rem; cursor: pointer; border-left: 3px solid transparent;
  transition: all var(--transition);
}
.pm-project-item:hover { background: var(--bg-hover); }
.pm-project-item.active { background: var(--accent-dim); border-left-color: var(--accent); }
.pm-project-name { font-size: 0.88rem; font-weight: 600; color: var(--text-primary); }
.pm-project-desc { font-size: 0.73rem; color: var(--text-muted); margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pm-sidebar-footer {
  margin-top: auto; padding: 0.6rem 1rem; border-top: 1px solid var(--border-subtle);
}
.pm-delete-btn {
  width: 100%; padding: 0.45rem; background: none; border: 1px solid var(--border);
  border-radius: var(--radius-sm); color: var(--text-muted); cursor: pointer;
  font-size: 0.78rem; transition: all var(--transition);
}
.pm-delete-btn:hover { border-color: var(--red); color: var(--red); background: var(--red-dim); }
.pm-main {
  flex: 1; display: flex; flex-direction: column; overflow: hidden;
}
.pm-breadcrumb {
  padding: 0.6rem 1rem; background: var(--bg-secondary);
  border-bottom: 1px solid var(--border-subtle);
  font-size: 0.8rem; color: var(--text-muted); display: flex; align-items: center; gap: 0.3rem;
  flex-shrink: 0;
}
.pm-breadcrumb span { cursor: pointer; color: var(--text-secondary); transition: color var(--transition); }
.pm-breadcrumb span:hover { color: var(--accent); }
.pm-breadcrumb span.current { color: var(--text-primary); font-weight: 600; cursor: default; }
.pm-breadcrumb .pm-sep { color: var(--text-muted); cursor: default; }
.pm-body {
  flex: 1; overflow-y: auto; padding: 1rem;
}
.pm-empty {
  text-align: center; padding: 3rem 1rem; color: var(--text-muted); font-size: 0.9rem;
}
/* Task card */
.pm-task-card {
  padding: 0.8rem 1rem; border-radius: var(--radius); cursor: pointer;
  border: 1px solid var(--border-subtle); margin-bottom: 0.5rem;
  transition: all var(--transition); background: var(--bg-secondary);
}
.pm-task-card:hover { border-color: var(--accent); background: var(--accent-dim); }
.pm-task-header { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 4px; }
.pm-task-id { font-size: 0.78rem; font-family: monospace; color: var(--text-secondary); font-weight: 600; }
.pm-task-status {
  font-size: 0.68rem; padding: 1px 6px; border-radius: 8px; font-weight: 600;
}
.pm-task-status.running { background: rgba(34,197,94,0.15); color: #4ade80; }
.pm-task-status.paused { background: rgba(250,204,21,0.15); color: #facc15; }
.pm-task-status.completed { background: rgba(96,165,250,0.15); color: #60a5fa; }
.pm-task-status.failed, .pm-task-status.rolled_back { background: rgba(248,113,113,0.15); color: #f87171; }
.pm-task-name { font-size: 0.82rem; color: var(--text-primary); }
.pm-task-cost { font-size: 0.75rem; color: var(--text-muted); margin-top: 2px; }
/* File entry */
.pm-file-entry {
  display: flex; align-items: center; gap: 0.6rem; padding: 0.55rem 0.8rem;
  border-radius: var(--radius-sm); cursor: pointer; transition: all var(--transition);
}
.pm-file-entry:hover { background: var(--bg-hover); }
.pm-file-icon { font-size: 1rem; width: 1.2rem; text-align: center; flex-shrink: 0; }
.pm-file-name { flex: 1; font-size: 0.84rem; color: var(--text-primary); }
.pm-file-meta { font-size: 0.72rem; color: var(--text-muted); font-variant-numeric: tabular-nums; }
/* File preview */
.pm-file-preview {
  padding: 0.5rem 0;
  font-family: "SF Mono", "Fira Code", "Cascadia Code", monospace;
  font-size: 0.78rem; line-height: 1.6; white-space: pre-wrap;
  color: var(--text-secondary);
}
.pm-file-title {
  font-size: 0.85rem; font-weight: 600; margin-bottom: 0.6rem;
  color: var(--text-primary); display: flex; align-items: center; gap: 0.5rem;
}
.pm-file-size { font-size: 0.72rem; color: var(--text-muted); font-weight: 400; }

  /* === 任务展示层优化新增样式 === */
  /* 任务类型标签（直播面板顶部） */
  .task-type-label {
    font-size: 0.85rem; padding: 0.2rem 0.6rem;
    border-radius: 4px; background: var(--accent-dim);
    color: var(--accent); display: inline-block;
    font-weight: 600; margin-top: 0.2rem;
  }
  .task-type-label.error {
    background: var(--red-dim); color: var(--red);
  }
  .task-type-label.info {
    background: var(--blue-dim); color: var(--blue);
  }
  /* 工作流流程图弹窗样式 */
  .wf-diagram-overlay {
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background: rgba(0, 0, 0, 0.6);
    z-index: 10000;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .wf-diagram-modal {
    background: var(--bg-card, #1e293b);
    border-radius: 12px;
    max-width: 480px;
    width: 90%;
    max-height: 80vh;
    overflow-y: auto;
    box-shadow: 0 10px 25px rgba(0, 0, 0, 0.3);
  }
  .wf-diagram-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1rem 1.2rem;
    border-bottom: 1px solid var(--border);
    font-weight: 600;
    font-size: 1rem;
    color: var(--text);
  }
  .wf-close {
    cursor: pointer;
    font-size: var(--font-size-xl);
    opacity: 0.6;
    color: var(--text-dim);
  }
  .wf-close:hover {
    opacity: 1;
    color: var(--red);
  }
  .wf-diagram-body {
    padding: 1.2rem;
    display: flex;
    flex-direction: column;
    align-items: center;
  }
  .wf-node {
    display: flex;
    align-items: center;
    gap: 0.8rem;
    padding: 0.6rem 1rem;
    background: var(--bg-main, #0f172a);
    border-radius: 8px;
    width: 100%;
    max-width: 360px;
    margin-bottom: 0.8rem;
  }
  .wf-icon {
    font-size: var(--font-size-2xl);
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    border-radius: 6px;
  }
  .wf-info {
    flex: 1;
  }
  .wf-role {
    font-weight: 600;
    font-size: 0.95rem;
    color: var(--text, #e2e8f0);
    line-height: 1.4;
  }
  .wf-desc {
    font-size: 0.8rem;
    opacity: 0.6;
    margin-top: 2px;
    color: var(--text-dim);
    line-height: 1.3;
  }
  .wf-arrow {
    font-size: var(--font-size-xl);
    opacity: 0.4;
    margin: 0.3rem 0;
    text-align: center;
    color: var(--accent);
    line-height: 1;
  }
  .ts-task.hub { border-left-color: var(--green); }
  .ts-task.hub.active { border-left-color: var(--green); background: var(--green-dim); }
  .ts-type-badge.hub { background: rgba(34,197,94,0.15); color: var(--green); }
  /* 任务类型徽章（任务切换弹窗） */
  .ts-type-badge {
    font-size: 0.68rem; padding: 1px 6px;
    border-radius: 8px; font-weight: 600;
    background: rgba(96,165,250,0.15); color: #60a5fa;
    margin-left: auto;
  }

  /* ====== Model Config Page ====== */
  .sidebar-models-btn {
    padding: 0.7rem 1rem; background: var(--bg-tertiary);
    border: none; border-top: 1px solid var(--border-subtle);
    color: var(--text-secondary); cursor: pointer; font-size: 0.85rem;
    display: flex; align-items: center; gap: 0.5rem;
    transition: all var(--transition); flex-shrink: 0;
  }
  .sidebar-models-btn:hover { background: var(--bg-hover); color: var(--accent); }
  .mc-view {
    flex: 1; display: flex; flex-direction: column; width: 100%; height: 100%;
    padding: 1.5rem; overflow-y: auto; background: var(--bg-primary);
  }
  .mc-header {
    display: flex; align-items: center; gap: 0.75rem;
    margin-bottom: 1.5rem; padding-bottom: 0.75rem;
    border-bottom: 1px solid var(--border);
  }
  .mc-back-btn {
    background: none; border: none; color: var(--text-primary);
    font-size: 1.25rem; cursor: pointer; padding: 0.25rem 0.5rem; border-radius: 4px;
  }
  .mc-back-btn:hover { background: var(--bg-hover); }
  .mc-title { margin: 0; font-size: 1.25rem; color: var(--text-primary); }
  .mc-section { margin-bottom: 2rem; }
  .mc-section-title { font-size: 1rem; color: var(--text-primary); margin: 0 0 0.5rem; }
  .mc-section-desc { font-size: 0.85rem; color: var(--text-muted); margin: 0 0 1rem; }
  .mc-table { border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
  .mc-table-header {
    display: grid; grid-template-columns: 1fr 1.4fr 0.8fr 0.8fr;
    padding: 0.75rem 1rem; background: var(--bg-secondary);
    font-size: 0.8rem; color: var(--text-muted); font-weight: 600;
  }
  .mc-table-row {
    display: grid; grid-template-columns: 1fr 1.4fr 0.8fr 0.8fr;
    padding: 0.6rem 1rem; border-top: 1px solid var(--border); align-items: center;
  }
  .mc-table-row:hover { background: var(--bg-hover); }
  .mc-role-name { font-size: 0.9rem; color: var(--text-primary); }
  .mc-select {
    width: 100%; max-width: 280px; padding: 0.4rem 0.5rem;
    background: var(--bg-primary); color: var(--text-primary);
    border: 1px solid var(--border); border-radius: 4px; font-size: 0.85rem;
  }
  .mc-default { font-size: 0.8rem; color: var(--text-muted); }
  .mc-modified {
    color: var(--accent); font-size: 0.75rem;
    background: rgba(56, 189, 248, 0.1); padding: 0.1rem 0.4rem; border-radius: 3px;
  }
  .mc-actions {
    display: flex; gap: 0.75rem; justify-content: flex-end; margin-top: 1rem;
  }
  .mc-save-btn {
    padding: 0.5rem 1.5rem; background: var(--accent); color: #fff;
    border: none; border-radius: 6px; cursor: pointer; font-size: 0.9rem;
  }
  .mc-save-btn:hover { opacity: 0.9; }
  .mc-save-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .mc-reset-btn {
    padding: 0.5rem 1rem; background: transparent; color: var(--text-muted);
    border: 1px solid var(--border); border-radius: 6px; cursor: pointer; font-size: 0.85rem;
  }
  .mc-reset-btn:hover { color: var(--text-primary); border-color: var(--text-muted); }
  .mc-ext-card {
    display: flex; align-items: center; gap: 1rem;
    padding: 0.75rem 1rem; border: 1px solid var(--border);
    border-radius: 6px; margin-bottom: 0.5rem;
  }
  .mc-ext-name { font-size: 0.9rem; color: var(--text-primary); flex: 1; }
  .mc-ext-status { font-size: 0.8rem; }
  .mc-configured { color: #22c55e; }
  .mc-not-configured { color: #ef4444; }
  .mc-ext-key { font-size: 0.75rem; color: var(--text-muted); font-family: monospace; }
  .mc-loading, .mc-error, .mc-empty {
    text-align: center; padding: 3rem; color: var(--text-muted); font-size: 0.9rem;
  }
  .mc-error { color: #ef4444; }
</style>
</head>
<body>
<div id="app">
  <!-- Sidebar -->
  <div class="sidebar" id="sidebar">
    <div class="sidebar-header">
      <span class="sidebar-brand">维造 Vizo</span>
      <button class="icon-btn" onclick="toggleSidebar()" title="收起侧栏">&#9776;</button>
    </div>
    <button class="new-session-btn" id="new-session-btn" onclick="createSession()">
      <span>+</span> 新建会话
    </button>
    <div class="session-list" id="session-list"></div>
    <button class="sidebar-projects-btn" onclick="openProjectManager()">&#128193; <span data-i18n="pmSidebarBtn">项目管理</span></button>
    <button class="sidebar-agents-btn" data-action="open-agents-sidebar">&#129302; <span data-i18n="agSidebarBtn">Agent 模块</span></button>
    <button class="sidebar-models-btn" data-action="open-model-config">&#9881; <span data-i18n="mcSidebarBtn">模型配置</span></button>
  </div>

  <!-- Main Area -->
  <div class="main-area">
    <!-- Toolbar -->
    <div class="toolbar">
      <button class="icon-btn" id="sidebar-expand" style="display:none;margin-right:0.2rem" onclick="toggleSidebar()">&#9776;</button>

      <div class="toolbar-spacer"></div>

      <div class="connection-status" id="conn-status">
        <span class="conn-dot" id="conn-dot"></span>
        <span id="conn-text">未连接</span>
      </div>
      <div class="chrome-status" id="chrome-status" onclick="toggleChromePopup()" title="浏览器连接">
        <span class="chr-dot"></span>
        <span id="chrome-status-text">Chrome</span>
        <div class="chrome-status-popup" id="chrome-popup">
          <div id="chrome-popup-disabled" style="display:none">
            <h4>Chrome MCP 已关闭</h4>
            <p style="color:var(--text-muted);line-height:1.5;margin-bottom:8px">浏览器能力已在 MCP 工具中关闭。重新开启后，顶部状态会继续显示连接情况。</p>
            <a class="connect-link" href="#" onclick="event.preventDefault(); document.getElementById('chrome-popup').classList.remove('show'); openSettings('mcp-tools');">打开 MCP 工具页 →</a>
          </div>
          <div id="chrome-popup-disconnected">
            <h4>浏览器未连接</h4>
            <p style="color:var(--text-muted);line-height:1.5;margin-bottom:8px">连接 Chrome 浏览器，解锁截图、导航、表单填写等 AI 能力</p>
            <a class="connect-link" id="chrome-connect-link" href="/vizo/chrome/connect" target="_blank">打开连接引导页 →</a>
          </div>
          <div id="chrome-popup-connected" style="display:none">
            <h4>浏览器已连接</h4>
            <div class="info-row"><span>浏览器</span><span class="info-val" id="chr-browser">-</span></div>
            <div class="info-row"><span>AI 可用能力</span><span class="info-val" id="chr-tools">-</span></div>
            <div class="info-row"><span>连接时长</span><span class="info-val" id="chr-uptime">-</span></div>
            <p style="color:var(--text-muted);font-size:0.7rem;margin-top:6px;line-height:1.4">AI 可用能力 = 截图、点击、导航、表单填写等浏览器操作数</p>
          </div>
        </div>
      </div>
      <button class="panel-toggle-btn" id="panel-toggle" onclick="togglePanel()">直播面板<span class="notif-red-dot" id="panel-red-dot"></span></button>
      <button class="toolbar-btn" id="theme-toggle" onclick="toggleTheme()" title="切换主题" style="font-size:1rem">&#9728;&#65039;</button>
      <button class="toolbar-btn" id="settings-btn" onclick="openSettings()" title="设置" style="font-size:1rem">&#9881;</button>
      <button class="toolbar-btn" id="lang-toggle" onclick="setLang(S.lang==='zh'?'en':'zh')" style="font-size:0.7rem;min-width:auto;padding:0.2rem 0.45rem;font-weight:700">EN</button>
    </div>

    <!-- Project Manager View -->
    <div id="project-manager-view" style="display:none">
      <div class="pm-topbar">
        <button onclick="closeProjectManager()" data-i18n="pmBackToTerminal">&#8592; 返回终端</button>
        <span class="pm-topbar-title" data-i18n="pmTitle">项目管理</span>
        <button onclick="showCreateProjectDialog()" data-i18n="pmNewProject">+ 新建项目</button>
      </div>
      <div class="pm-content">
        <div class="pm-sidebar" id="pm-project-list"></div>
        <div class="pm-main" id="pm-main-area">
          <div class="pm-body"><div class="pm-empty" data-i18n="pmSelectProject">请从左侧选择一个项目</div></div>
        </div>
      </div>
    </div>

    <!-- Agent Management View -->
    <div id="agents-view" style="display:none" class="agents-view">
      <div class="agents-header">
        <button class="agents-back-btn" data-action="close-agents">&#8592;</button>
        <h2 data-i18n="agTitle">Agent 模块</h2>
        <button class="agents-create-btn" data-action="open-create-panel">+ <span data-i18n="agCreate">创建新模块</span></button>
      </div>
      <div class="agents-filter">
        <input class="agents-search" id="agents-search" placeholder="搜索模块..." data-action="search-agents">
        <div class="agents-filter-tags">
          <button class="agents-tag active" data-filter="all" data-action="filter-agents">全部</button>
          <button class="agents-tag" data-filter="_builtin" data-action="filter-agents">官方</button>
          <button class="agents-tag" data-filter="_user" data-action="filter-agents">自建</button>
        </div>
      </div>
      <div class="agents-grid" id="agents-grid"></div>
      <div class="agents-overlay" id="agents-overlay" data-action="close-drawer"></div>
      <div class="agents-drawer" id="agents-drawer">
        <div id="agents-drawer-content"></div>
      </div>
      <div class="agents-panel" id="agents-panel">
        <div id="agents-panel-content"></div>
      </div>
    </div>

    <!-- Model Config View -->
    <div id="model-config-view" style="display:none" class="mc-view">
      <div class="mc-header">
        <button class="mc-back-btn" data-action="close-model-config">&larr;</button>
        <h2 class="mc-title">模型配置</h2>
      </div>
      <div class="mc-content" id="mc-content"></div>
    </div>

    <!-- Settings View -->
    <div id="settings-view" style="display:none" class="settings-view">
      <div class="settings-page-header">
        <button class="settings-back-btn" onclick="closeSettings()">&larr; 返回</button>
        <h2>设置</h2>
      </div>
      <div class="settings-page-shell">
        <div class="settings-sidebar">
          <button class="settings-nav-item active" data-section="main-session" onclick="switchSettingsSection('main-session')">主会话模型配置</button>
          <button class="settings-nav-item" data-section="external-models" onclick="switchSettingsSection('external-models')">外部模型配置</button>
          <button class="settings-nav-item" data-section="role-models" onclick="switchSettingsSection('role-models')">角色配置</button>
          <button class="settings-nav-item" data-section="mcp-tools" onclick="switchSettingsSection('mcp-tools')">MCP 工具</button>
          <button class="settings-nav-item" data-section="network" onclick="switchSettingsSection('network')">网络配置</button>
          <button class="settings-nav-item" data-section="password" onclick="switchSettingsSection('password')">控制台密码</button>
        </div>
        <div class="settings-main">
          <div class="settings-panel active" data-section="main-session">
            <div class="settings-panel-scroll">
              <div class="settings-panel-title">主会话模型配置</div>
              <div class="settings-panel-desc">主会话默认使用这里的连接。角色会话及其内部子代理仍按角色配置生效；这里同时负责 Claude CLI 的安全切模映射。</div>
              <div class="settings-subsection">
                <div id="apikey-status" style="margin-bottom:0.75rem;font-size:0.85rem;color:var(--text-muted)">加载中...</div>
                <div id="saved-connections" style="display:none;margin-bottom:1rem">
                  <div class="settings-subsection-title">快捷连接</div>
                  <div class="model-info" style="margin:0.2rem 0 0.5rem">同一平台可按不同接入方式分别保存，例如“Claude 直转”、“Anthropic 映射”和“OpenAI 桥接”。点击卡片立即切换；如当前页有未保存修改，会先提醒。悬停卡片可查看路由详情。</div>
                  <div id="conn-cards" style="display:flex;gap:0.4rem;flex-wrap:wrap"></div>
                </div>
                <div class="settings-field">
                  <label>接入类型</label>
	                  <select id="new-provider" onchange="onMainProviderChange()">
	                    <option value="claude">Claude</option>
	                    <option value="anthropic_compatible">Anthropic 兼容接口</option>
	                    <option value="openai_compatible">OpenAPI</option>
	                  </select>
                </div>
                <div class="settings-field" id="main-provider-template-field" style="display:none">
                  <label>预设模板</label>
                  <select id="new-provider-template" onchange="onMainProviderTemplateChange()"></select>
                </div>
                <div class="settings-field" id="main-openai-auth-mode-field" style="display:none">
                  <label>授权方式</label>
                  <select id="new-openai-auth-mode" onchange="onMainOpenAIAuthModeChange()">
                    <option value="api_key">API Key</option>
                    <option value="account_login">OpenAI 账号登录</option>
                  </select>
                </div>
                <div class="settings-field" id="main-base-url-field">
                  <label>Base URL</label>
                  <input type="text" id="new-baseurl" placeholder="https://api.anthropic.com（留空使用当前接入默认地址）" autocomplete="off">
                </div>
                <div class="settings-field" id="main-api-key-field">
                  <label>API Key</label>
                  <input type="text" id="new-apikey" placeholder="输入 API Key" autocomplete="off">
                </div>
                <div id="main-openai-auth-panel" class="settings-guide-card" style="display:none;margin:-0.1rem 0 0.9rem"></div>
                <div id="main-model-mapping-fields" style="display:none">
                  <div class="settings-field">
                    <label>Opus 对应模型 ID</label>
                    <input type="text" id="new-default-opus-model" placeholder="例如：glm-5" autocomplete="off">
                  </div>
                  <div class="settings-field">
                    <label>Sonnet 对应模型 ID</label>
                    <input type="text" id="new-default-sonnet-model" placeholder="例如：glm-4.7" autocomplete="off">
                  </div>
                  <div class="settings-field">
                    <label>Haiku 对应模型 ID</label>
                    <input type="text" id="new-default-haiku-model" placeholder="例如：glm-4.5-air" autocomplete="off">
                  </div>
                </div>
              </div>
              <div class="settings-subsection">
                <div class="settings-subsection-title">连接测试与快捷连接</div>
                <div style="display:flex;gap:0.5rem;flex-wrap:wrap;align-items:center">
                  <button id="test-conn-btn" class="settings-btn-secondary" onclick="testConnection()">测试连接</button>
                  <button class="settings-btn-secondary" onclick="saveAsConnection()">保存为快捷连接</button>
                </div>
                <div id="conn-test-result" style="font-size:0.78rem;line-height:1.6;color:var(--text-muted);margin-top:0.65rem"></div>
              </div>
            </div>
            <div class="settings-page-actions">
              <span class="settings-actions-state clean" id="settings-state-main-session">未修改</span>
	              <button id="save-main-session-btn" class="settings-save" onclick="saveMainSessionConfig()">保存默认主连接配置</button>
            </div>
          </div>

          <div class="settings-panel" data-section="external-models">
            <div class="settings-panel-scroll">
              <div class="settings-panel-title">外部模型配置</div>
              <div class="settings-panel-desc">仅当某些角色需要使用独立连接时才需要配置。这里的外部模型同样通过 <code>claude -p</code> 调用：直转 Claude 的平台按 Claude 方式使用，需要模型映射的平台选 Anthropic 兼容接口。</div>
              <div class="settings-note warn">如果给的是 <code>/v1</code> 或平台提供的 <code>/codex/v1</code> 这类 OpenAPI 根路径，请改配到主会话 OpenAPI 直连；外部模型与子代理的本地 bridge 已移除，不再支持这种链路。</div>
              <div class="settings-subsection">
                <div id="ext-models-list"><div style="text-align:center;color:var(--text-muted);padding:1rem">加载中...</div></div>
              </div>
            </div>
            <div class="settings-page-actions">
              <span class="settings-actions-state clean" id="settings-state-external-models">未修改</span>
              <button id="save-external-models-btn" class="settings-save" onclick="saveExternalModelsConfig()">保存外部模型配置</button>
            </div>
          </div>

          <div class="settings-panel" data-section="role-models">
            <div class="settings-panel-scroll">
              <div class="settings-panel-title">角色配置</div>
              <div class="settings-panel-desc">修改后对下一个启动的任务生效。切换到本页时会重新拉取最新模型映射，确保下拉显示与主会话配置一致。MCP 需要先在“MCP 工具”页中开启，才能在这里分配给角色。</div>
              <div class="settings-subsection">
                <div id="role-mcp-disabled-note" class="settings-note warn mcp-role-page-note" style="display:none"></div>
                <div class="model-info">限流时自动降级：Opus → Sonnet → Haiku，无需手动干预。</div>
                <div class="model-table" id="model-table">
                  <div style="text-align:center;color:var(--text-muted);padding:2rem">加载中...</div>
                </div>
              </div>
            </div>
            <div class="settings-page-actions">
              <span class="settings-actions-state clean" id="settings-state-role-models">未修改</span>
              <button id="reset-role-models-btn" class="settings-btn-secondary" onclick="resetSettingsModels()">恢复所有默认值</button>
              <button id="save-role-models-btn" class="settings-save" onclick="saveModels()">保存角色配置</button>
            </div>
          </div>

          <div class="settings-panel" data-section="mcp-tools">
            <div class="settings-panel-scroll">
              <div class="settings-panel-title">MCP 工具</div>
              <div class="settings-panel-desc">管理 AI 可用的工具。先在这里开启，再到角色配置里分配给角色。</div>
              <div id="mcp-tools-overview"></div>
              <div class="settings-subsection">
                <div id="mcp-service-list" class="mcp-service-list">
                  <div style="text-align:center;color:var(--text-muted);padding:1rem">加载中...</div>
                </div>
              </div>
              <div class="settings-subsection">
                <div class="settings-subsection-title">添加工具</div>
                <div class="mcp-add-grid">
                  <div class="mcp-add-card">
                    <div class="mcp-add-title">从官方文档粘贴配置</div>
                    <div class="mcp-add-desc">如果第三方文档给了 MCP 配置 JSON，直接粘贴到这里导入即可。导入后，再到“角色配置”里分配给角色。</div>
                    <div class="settings-field" style="margin-bottom:0.75rem">
                      <label>配置 JSON</label>
                      <textarea id="mcp-import-json" placeholder='例如：{"mcpServers":{"my-mcp":{"command":"npx","args":["-y","some-mcp"]}}}'></textarea>
                    </div>
                    <div style="display:flex;gap:0.5rem;flex-wrap:wrap">
                      <button class="settings-btn-secondary" onclick="importMcpConfig()">导入配置</button>
                    </div>
                  </div>
                  <div class="mcp-add-card">
                    <div class="mcp-add-title">手动添加工具</div>
                    <div class="mcp-add-desc">如果文档只给了名称、命令和参数，可在这里手动填写。参数请一行一个，环境变量按 KEY=VALUE 一行一个填写。</div>
                    <div class="settings-field">
                      <label>工具名称</label>
                      <input type="text" id="mcp-manual-name" placeholder="例如：jina">
                    </div>
                    <div class="settings-field">
                      <label>命令</label>
                      <input type="text" id="mcp-manual-command" placeholder="例如：npx">
                    </div>
                    <div class="settings-field">
                      <label>参数（每行一个）</label>
                      <textarea id="mcp-manual-args" placeholder="-y&#10;jina-mcp-tools"></textarea>
                    </div>
                    <div class="settings-field" style="margin-bottom:0.75rem">
                      <label>环境变量（每行一个）</label>
                      <textarea id="mcp-manual-env" placeholder="API_KEY=your-key"></textarea>
                    </div>
                    <div style="display:flex;gap:0.5rem;flex-wrap:wrap">
                      <button class="settings-btn-secondary" onclick="saveManualMcpService()">添加工具</button>
                    </div>
                  </div>
                </div>
              </div>
            </div>
            <div class="settings-page-actions">
              <span class="settings-actions-state clean" id="settings-state-mcp-tools">开关会立即生效</span>
            </div>
          </div>

          <div class="settings-panel" data-section="network">
            <div class="settings-panel-scroll">
              <div class="settings-panel-title">网络配置</div>
              <div class="settings-panel-desc">配置公网域名后，可生成手机可访问的预览链接。</div>
              <div class="settings-subsection">
                <div id="domain-status" style="margin-bottom:1rem;font-size:0.85rem;color:var(--text-muted)">加载中...</div>
                <div class="settings-field">
                  <label>公网域名</label>
                  <div style="font-size:0.75rem;color:var(--text-muted);margin-bottom:0.3rem">已备案的域名，不需要 http:// 前缀</div>
                  <input type="text" id="domain-input" placeholder="example.com" autocomplete="off">
                </div>
              </div>
            </div>
            <div class="settings-page-actions">
              <span class="settings-actions-state clean" id="settings-state-network">未修改</span>
              <button id="save-network-btn" class="settings-save" onclick="saveDomain()">保存网络配置</button>
            </div>
          </div>

          <div class="settings-panel" data-section="password">
            <div class="settings-panel-scroll">
              <div class="settings-panel-title">控制台密码</div>
              <div class="settings-panel-desc">修改后当前设备不受影响，其他已登录设备需要重新登录。</div>
              <div class="settings-subsection">
                <div class="settings-field">
                  <label>旧密码</label>
                  <input type="password" id="old-password" placeholder="当前密码或访问令牌" autocomplete="current-password">
                </div>
                <div class="settings-field">
                  <label>新密码</label>
                  <input type="password" id="new-password" placeholder="至少 8 位" autocomplete="new-password">
                </div>
                <div class="settings-field" style="margin-bottom:0">
                  <label>确认新密码</label>
                  <input type="password" id="confirm-password" placeholder="再次输入新密码" autocomplete="new-password">
                </div>
              </div>
            </div>
            <div class="settings-page-actions">
              <span class="settings-actions-state clean" id="settings-state-password">未填写</span>
              <button id="save-password-btn" class="settings-save" onclick="changePassword()">修改控制台密码</button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Terminal -->
    <div class="terminal-container">
      <div id="terminal"></div>
      <div class="notification-stack" id="notification-stack"></div>
    </div>

    <!-- Input Area -->
    <div class="input-area" id="input-area">
      <!-- ====== Shortcut Bar ====== -->
      <div class="cc-shortcuts" id="cc-shortcuts">
        <button class="cc-btn" onclick="sendPtyKey('y\r')" id="cc-allow" title="Claude Code 权限确认：允许执行当前操作">
          <span class="cc-icon">&#10003;</span> <span data-i18n="allow">允许</span> <span class="cc-key">y</span>
        </button>
        <button class="cc-btn" onclick="sendPtyKey('n\r')" id="cc-deny" title="Claude Code 权限确认：拒绝执行当前操作">
          <span class="cc-icon">&#10007;</span> <span data-i18n="deny">拒绝</span> <span class="cc-key">n</span>
        </button>
        <button class="cc-btn" onclick="sendPtyKey('\x03')" title="发送 Ctrl+C 中断信号，停止当前正在执行的命令">
          <span class="cc-icon">&#9632;</span> <span data-i18n="interrupt">中断</span> <span class="cc-key">Ctrl+C</span>
        </button>
        <button class="cc-btn" onclick="sendPtyKey('\x1a')" title="发送 Ctrl+Z 撤销信号，撤销上一次操作">
          <span class="cc-icon">&#8617;</span> <span data-i18n="undo">撤销</span> <span class="cc-key">Ctrl+Z</span>
        </button>
        <button class="cc-btn" onclick="sendPtyKey('/commit\r')" title="执行 /commit 命令，提交代码变更">
          <span class="cc-icon">&#10003;</span> <span data-i18n="commit">提交</span> <span class="cc-key">/commit</span>
        </button>
        <button class="cc-btn" onclick="sendPtyKey('/retry\r')" title="执行 /retry 命令，重试上一次失败的操作">
          <span class="cc-icon">&#8635;</span> <span data-i18n="retry">重试</span> <span class="cc-key">/retry</span>
        </button>
        <button class="cc-btn" onclick="sendPtyKey('/compact\r')" title="压缩对话上下文，释放 token 空间，保留关键信息">
          <span class="cc-icon">&#128230;</span> <span data-i18n="compact">压缩</span> <span class="cc-key">/compact</span>
        </button>
        <button class="cc-btn" onclick="sendPtyKey('/clear\r')" title="清除对话上下文，开始全新对话">
          <span class="cc-icon">&#128465;</span> <span data-i18n="clearCtx">清除</span> <span class="cc-key">/clear</span>
        </button>
        <button class="cc-btn" onclick="sendPtyKey('/model\r')" title="切换 Claude 模型（Opus/Sonnet/Haiku）">
          <span class="cc-icon">&#9881;</span> <span data-i18n="switchModel">模型</span> <span class="cc-key">/model</span>
        </button>
        <button class="cc-btn" onclick="sendPtyKey('\x1b')" title="发送 Escape 键，取消当前输入或退出提示">
          <span class="cc-icon">&#9099;</span> <span data-i18n="escape">取消</span> <span class="cc-key">Esc</span>
        </button>
        <button class="cc-btn" onclick="sendPtyKey('/cost\r')" title="查看当前会话的 token 用量和费用明细">
          <span class="cc-icon">&#128176;</span> <span data-i18n="costCmd">费用</span> <span class="cc-key">/cost</span>
        </button>
      </div>
      <!-- ====== Workflow Bar ====== -->
      <div class="wf-bar" id="wf-bar">
        <button class="wf-btn wf-new" onclick="fillWorkflow('new_feature')" title="完整开发流程：需求分析→PRD→架构→开发→测试→部署">
          ✨ <span data-i18n="wfNew">新功能</span>
        </button>
        <button class="wf-btn wf-fix" onclick="fillWorkflow('bug_fix')" title="快速修复流程：定位→修复→测试">
          🐛 <span data-i18n="wfFix">Bug修复</span>
        </button>
        <button class="wf-btn wf-refactor" onclick="fillWorkflow('refactor')" title="重构优化流程：分析→重构→回归测试">
          ♻️ <span data-i18n="wfRefactor">优化重构</span>
        </button>
        <button class="wf-btn wf-auto" onclick="fillWorkflow('auto')" title="AI 自动判定最适合的工作流（推荐）">
          🤖 <span data-i18n="wfAuto">AI自动</span>
        </button>
        <button class="wf-btn wf-embedded" onclick="fillWorkflow('embedded')" title="嵌入式调试流程">
          🔧 <span data-i18n="wfEmbed">嵌入式</span>
        </button>
        <button class="wf-btn wf-nondev" onclick="fillWorkflow('non_dev')" title="非开发任务（文档/咨询等）">
          📝 <span data-i18n="wfNonDev">非开发</span>
        </button>
      </div>
      <!-- Input Row (mobile only) -->
      <div class="input-row" id="input-row">
        <div class="input-wrapper">
          <textarea id="user-input" rows="1" onkeydown="handleInputKey(event)" oninput="autoResizeInput(this)"></textarea>
        </div>
        <button class="send-btn" onclick="sendMessage()" data-i18n="send">发送</button>
      </div>
    </div>
  </div>
  <div class="right-panel" id="right-panel">
    <!-- L1: Header (two-row layout) -->
    <div class="panel-header">
      <div class="hdr-row1">
        <span class="status-dot" id="hdr-dot"></span>
        <div class="p-title" onclick="toggleTaskDropdown(event)">
          <span class="p-title-name" id="hdr-name">Vizo 直播</span>
          <button class="ts-trigger" onclick="openTaskSelector()" title="切换任务">
            <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor">
              <path d="M2 3h8v1.5H2V3zm0 4h10v1.5H2V7zm0 4h6v1.5H2V11z"/>
              <path d="M12 9l3 3-3 3v-2H9v-2h3V9z" opacity="0.7"/>
            </svg>
          </button>
        </div>
        <button class="icon-btn" onclick="togglePanel()" style="font-size:1rem">&times;</button>
      </div>
      <!-- 任务类型标签 -->
      <div class="task-type-label" id="hdr-type" style="display:none"></div>
      <div class="hdr-row2" id="hdr-row2" style="display:none">
        <span class="p-timer" id="hdr-timer">00:00</span>
        <span class="p-cost" id="hdr-cost">$0.00</span><span class="cost-info-icon" id="hdr-cost-info" style="display:none" title=""></span>
        <div class="p-ctrl" id="hdr-ctrl"></div>
      </div>
    </div>
    <!-- L2: Banner (conditional — shows when viewing history while live task runs) -->
    <div id="banner-area"></div>
    <!-- L3: Progress row + step toggle (hidden in split layout) -->
    <div class="progress-row" id="progress-row" onclick="toggleDrawer()" style="display:none">
      <div class="pr-bar"><div class="pr-fill" id="pr-fill" style="width:0%"></div></div>
      <span class="pr-pct" id="pr-pct">0%</span>
      <span class="pr-steps" id="pr-steps">0/0</span>
      <span class="pr-toggle"><span id="pr-text">展开</span> <span class="pr-arrow" id="pr-arrow">&#9660;</span></span>
    </div>
    <!-- L4: Step list (collapsible) -->
    <div class="step-list" id="step-list">
      <div id="agent-cards"></div>
    </div>
    <!-- Controls (opus-controls) — preserved fully -->
    <div class="opus-controls" id="opus-controls" style="display:none">
      <div class="ctrl-group" id="ctrl-running">
        <button class="ctrl-btn warn" onclick="opusPanelAction('pause')">&#9208; <span data-i18n="pause">暂停</span></button>
        <button class="ctrl-btn danger" onclick="opusPanelAction('terminate')">&#9632; <span data-i18n="terminate">终止</span></button>
      </div>
      <div class="ctrl-group" id="ctrl-paused" style="display:none">
        <button class="ctrl-btn success" onclick="toggleResumeInput()" id="ctrl-resume-btn">&#9654; <span data-i18n="resume">继续执行</span></button>
        <button class="ctrl-btn accent" onclick="toggleRollbackSelector()">&#8617; <span data-i18n="rollbackStep">回退步骤</span></button>
        <button class="ctrl-btn danger" onclick="opusPanelAction('terminate')">&#9632; <span data-i18n="terminate">终止</span></button>
      </div>
      <div class="ctrl-group" id="ctrl-done" style="display:none">
        <button class="ctrl-btn accent" onclick="viewTaskReport()">&#128203; <span data-i18n="viewReport">查看报告</span></button>
      </div>
      <div class="panel-feedback" id="panel-resume-area" style="display:none">
        <textarea id="resume-feedback" placeholder=""></textarea>
        <div class="panel-feedback-hint"></div>
        <button class="ctrl-btn success" onclick="doResume()">&#9654; <span data-i18n="resume">继续执行</span></button>
      </div>
      <div class="rollback-selector" id="rollback-selector" style="display:none">
        <div class="rollback-steps" id="rollback-steps"></div>
        <textarea id="rollback-feedback" placeholder=""></textarea>
        <div class="rollback-actions">
          <button class="ctrl-btn warn" onclick="doRollback()">&#8617; <span data-i18n="confirmRollback">确认回退</span></button>
          <button class="ctrl-btn muted" onclick="closeRollbackSelector()"><span data-i18n="cancel">取消</span></button>
        </div>
      </div>
    </div>
    <!-- L5: Role tabs with arrow navigation -->
    <div class="role-tabs-wrap" id="role-tabs-wrap" style="display:none">
      <button class="rt-arrow rt-left" id="rt-left" onclick="scrollRoleTabs(-1)">&lsaquo;</button>
      <div class="role-tabs" id="role-tabs"></div>
      <button class="rt-arrow rt-right" id="rt-right" onclick="scrollRoleTabs(1)">&rsaquo;</button>
    </div>
    <!-- Panel confirm area (dual-instance with notification bar) -->
    <div class="panel-confirm-area" id="panel-confirm-area" style="display:none"></div>
    <!-- L6+L7: Log panels container (multi-panel) -->
    <div class="log-panels-container" id="log-panels-container" style="display:none"></div>
    <!-- L8: Footer -->
    <div class="log-footer" id="log-footer" style="display:none">
      <span class="log-footer-left">
        <button class="btn-live-all" id="btn-live-all" style="display:none" onclick="clearFocus()">&#9679; <span data-i18n="liveAll">直播全部</span></button>
        <span class="log-status-dot" id="log-status-dot"></span>
        <span id="log-footer-role"></span>
        <span id="log-footer-status"></span>
      </span>
      <span class="log-footer-right" id="log-footer-count">0 events</span>
    </div>
    <!-- No task placeholder -->
    <div class="no-task" id="no-task-msg" style="display:flex">
      <div class="no-task-icon">&#128640;</div>
      <div>当前无 Vizo 任务运行</div>
      <div style="font-size:0.75rem">输入 <code style="background:var(--bg-input);padding:2px 6px;border-radius:3px">vizo "任务描述"</code> 启动任务</div>
    </div>
    <!-- Task dropdown removed — replaced by task selector modal -->
  </div>
</div>

<!-- New Session Modal -->
<div class="modal-overlay" id="new-session-modal" style="display:none" onclick="if(event.target===this)closeNewSessionModal()">
  <div class="modal-box">
    <div class="modal-title" id="modal-title">新建会话</div>
    <div class="modal-label" id="modal-label-path">项目路径</div>
    <div class="project-list" id="project-list">
      <div style="color:var(--text-muted);font-size:0.8rem;padding:0.5rem">加载中...</div>
    </div>
    <div class="modal-label" id="modal-label-name">会话名称（可选）</div>
    <input type="text" class="modal-input" id="modal-session-name" placeholder="默认使用项目名">
    <div class="modal-actions">
      <button class="modal-btn modal-btn-cancel" onclick="closeNewSessionModal()">取消</button>
      <button class="modal-btn modal-btn-create" onclick="doCreateSession()">创建</button>
    </div>
  </div>
</div>

<!-- Active Session Hot-Swap Confirm Modal -->
<div class="modal-overlay" id="session-switch-confirm-modal" style="display:none" onclick="if(event.target===this)closeSessionSwitchConfirm(false)">
  <div class="modal-box session-switch-confirm-modal">
    <div class="modal-title" id="session-switch-confirm-title">应用到当前会话？</div>
    <div class="session-switch-confirm-copy" id="session-switch-confirm-copy"></div>
    <div class="session-switch-confirm-target" id="session-switch-confirm-target"></div>
    <div class="session-switch-confirm-lines" id="session-switch-confirm-lines"></div>
    <div class="modal-actions">
      <button class="modal-btn modal-btn-cancel" id="session-switch-confirm-cancel" onclick="closeSessionSwitchConfirm(false)">取消</button>
      <button class="modal-btn modal-btn-create" id="session-switch-confirm-ok" onclick="closeSessionSwitchConfirm(true)">继续切换</button>
    </div>
  </div>
</div>

<!-- Task Selector Modal (left-right dual panel) -->
<div class="modal-overlay" id="task-selector-modal" style="display:none"
     onclick="if(event.target===this)closeTaskSelector()">
  <div class="modal-box ts-modal">
    <div class="ts-header">
      <div class="modal-title" data-i18n="tsTitle">选择任务</div>
      <button class="ts-close" onclick="closeTaskSelector()">&times;</button>
    </div>
    <div class="ts-body">
      <div class="ts-left" id="ts-project-list"></div>
      <div class="ts-right" id="ts-task-list"></div>
    </div>
  </div>
</div>

<!-- Create Project Modal -->
<div class="modal-overlay" id="create-project-modal" style="display:none" onclick="if(event.target===this)closeCreateProjectDialog()">
  <div class="modal-box">
    <div class="modal-title" data-i18n="pmCreateTitle">新建项目</div>
    <div class="modal-label" data-i18n="pmProjectName">项目名称</div>
    <input type="text" class="modal-input" id="new-project-name" placeholder="my-project">
    <div class="modal-label" style="margin-top:0.8rem" data-i18n="pmProjectDesc">项目描述（可选）</div>
    <input type="text" class="modal-input" id="new-project-desc">
    <div class="modal-label" style="margin-top:0.8rem" data-i18n="pmProjectType">项目类型</div>
    <div style="display:flex;gap:0.5rem">
      <label style="display:flex;align-items:center;gap:0.3rem;cursor:pointer;padding:0.4rem 0.8rem;border:1px solid var(--border);border-radius:6px;font-size:0.85rem" id="type-dev-label">
        <input type="radio" name="new-project-type" value="dev" checked onchange="onProjectTypeChange()"> <span data-i18n="pmTypeDev">开发项目</span>
      </label>
      <label style="display:flex;align-items:center;gap:0.3rem;cursor:pointer;padding:0.4rem 0.8rem;border:1px solid var(--border);border-radius:6px;font-size:0.85rem" id="type-hub-label">
        <input type="radio" name="new-project-type" value="hub" onchange="onProjectTypeChange()"> <span data-i18n="pmTypeHub">工作区</span>
      </label>
    </div>
    <div id="default-module-row" style="margin-top:0.8rem;display:none">
      <div class="modal-label" data-i18n="pmDefaultModule">默认模块（可选）</div>
      <select class="modal-input" id="new-project-module" style="padding:0.5rem 0.7rem"></select>
    </div>
    <div class="modal-actions">
      <button class="modal-btn modal-btn-cancel" onclick="closeCreateProjectDialog()" data-i18n="cancel">取消</button>
      <button class="modal-btn modal-btn-create" onclick="doCreateProject()" data-i18n="create">创建</button>
    </div>
  </div>
</div>

<div class="modal-overlay" id="connection-auth-modal" style="display:none" onclick="if(event.target===this)closeConnectionAuthModal()">
  <div class="modal-box connection-auth-modal">
    <div class="modal-title" id="connection-auth-modal-title">OpenAI 账号登录</div>
    <div id="connection-auth-modal-body" class="model-info" style="margin:0 0 0.8rem">加载中...</div>
    <div class="modal-actions connection-auth-modal-actions">
      <button class="modal-btn modal-btn-cancel" onclick="closeConnectionAuthModal()">关闭</button>
      <button class="modal-btn modal-btn-cancel" id="connection-auth-logout-btn" onclick="logoutConnectionAccountAuth()" style="display:none">退出登录</button>
      <button class="modal-btn modal-btn-cancel" id="connection-auth-refresh-btn" onclick="refreshConnectionAuthModalStatus(true)">刷新状态</button>
      <button class="modal-btn modal-btn-create" id="connection-auth-login-btn" onclick="startConnectionAccountLogin()">开始登录</button>
    </div>
  </div>
</div>

<!-- Reconnect Overlay -->
<div class="reconnect-overlay" id="reconnect-overlay">
  <div class="reconnect-spinner"></div>
  <div class="reconnect-text">重连中...</div>
</div>

<!-- Toast -->
<div class="vizo-toast-host" id="toastHost" aria-live="polite" aria-atomic="false"></div>

<script>
// ======================== State ========================
const S = {
  sessions: [],
  activeSessionId: null,
  ws: null,
  term: null,
  fitAddon: null,
  sidebarOpen: true,
  panelOpen: false,
  permissionPending: false,
  opusTask: null,
  reconnectTimer: null,
  reconnectAttempts: 0,
  maxReconnectAttempts: 50,
  pingInterval: null,
  pongTimeout: null,
  lastOutputChunk: '',
  resizeTimer: null,
  // Recent output lines buffer for permission detection
  _outputBuf: '',
  inputBoxVisible: false,
  isConnected: false,
  _sessionSwitchConfirmResolver: null,
  lang: localStorage.getItem('opus_lang') || 'zh',
  // 操作日志状态
  actionLogs: new Map(),       // Map<logKey, Array<ActionEvent>>  key: {role} 或 sub-{N}:{role}
  scrollPositions: new Map(),  // Map<logKey, scrollTop>
  drawerOpen: false,           // 步骤抽屉展开状态
  taskFinished: false,         // 任务是否终态
  lastStepActionTs: 0,         // 最近一次 WebSocket step_action 时间戳（防重复）
  taskTimer: null,             // 任务计时 setInterval
  selectedTaskId: null,        // 任务切换选中
  taskList: [],                // 任务列表缓存
  // 多面板/工作流相关
  focus: null,                 // null | {kind:'step', role} | {kind:'subtask', subId, role}
  expandedSubs: new Set(),     // 展开的子任务 subName 集合
  subTaskCache: new Map(),     // Map<subName, subTask data>
  workflowCache: null,         // buildWorkflow() 缓存，opus_update 时清空
  autoFollowMap: new Map(),    // Map<logKey, boolean> 每面板独立跟随
};

// ======================== i18n ========================
const LANGS = {
  zh: {
    // Sidebar
    collapseSidebar: '收起侧栏',
    newSession: '新建会话',
    noSessions: '暂无会话',
    closeSession: '关闭会话',
    // Toolbar - Basic
    interrupt: '中断',
    undo: '撤销',
    commit: '提交',
    retry: '重试',
    compact: '压缩',
    clearCtx: '清除',
    switchModel: '模型',
    escape: '取消',
    costCmd: '费用',
    // Workflow Bar
    wfNew: '新功能', wfFix: 'Bug修复', wfRefactor: '优化重构',
    wfAuto: 'AI自动', wfEmbed: '嵌入式', wfNonDev: '非开发',
    // Toolbar - Permission
    allow: '允许',
    deny: '拒绝',
    // Toolbar - Vizo
    pause: '暂停',
    terminate: '终止',
    resume: '恢复',
    rollback: '回滚',
    // Toolbar - Mode & Status
    inputBox: '输入框',
    terminal: '终端',
    inputBoxTooltip: '输入框：在下方输入长消息\n终端：直接在终端中输入（Esc 切换）',
    connected: '已连接',
    disconnected: '未连接',
    reconnecting: '重连中...',
    livePanel: '直播面板',
    expand: '展开',
    collapse: '收起',
    liveAll: '直播全部',
    focusLabel: '聚焦',
    // Terminal welcome
    welcomeTitle: 'Vizo Web Console',
    welcomeMsg1: '点击 \x1b[37m+ 新建会话\x1b[90m 开始与 Claude 对话',
    welcomeMsg2: '或从左侧栏选择已有会话',
    welcomeTip1: '底部输入框用于输入消息',
    welcomeTip2: '切换到终端模式可直接键盘操作',
    // Session
    connectingToSession: '正在连接会话...',
    sessionClosedMsg: '会话已关闭，请选择或新建会话。',
    sessionRunningConfirm: '该会话仍在运行，确定关闭？',
    // Toasts
    authFailed: '认证失败，正在跳转到登录页...',
    sessionTakeover: '会话已被另一个窗口接管',
    sessionNotFound: '会话不存在',
    replayedBuffer: '已回放缓冲区内容',
    sessionEnded: '会话已结束',
    processExited: '进程退出',
    reconnectFailed: '多次重连失败',
    maxSessionsReached: '已达最大会话数，请先关闭一个。',
    sessionCreated: '会话已创建',
    createSessionFailed: '创建会话失败',
    sessionClosed: '会话已关闭',
    closeSessionFailed: '关闭会话失败',
    notConnected: '未连接',
    noOpusTask: '未找到 Vizo 任务',
    taskActionSent: '任务{action}指令已发送',
    taskActionFailed: '{action}失败',
    serverError: '服务器错误',
    // Vizo Panel
    opusLive: 'Vizo 直播',
    noOpusTaskRunning: '当前无 Vizo 任务运行',
    startTaskHint: '输入 <code style="background:var(--bg-input);padding:2px 6px;border-radius:3px">vizo "任务描述"</code> 启动任务',
    tabLive: '直播',
    tabOverview: '概览',
    tabDocs: '文档',
    outputDocs: '产出文档',
    noDocsYet: '暂无文档',
    inProgress: '进行中...',
    progress: '进度',
    cost: '费用',
    unbillable: '不计费',
    costInfoTip: '仅统计 Claude 相关模型费用，外部模型不计入',
    status: '状态',
    taskDetails: '任务详情',
    taskId: '任务 ID',
    totalCost: '总费用',
    started: '开始时间',
    model: '模型',
    inputTokens: '输入 Token',
    outputTokens: '输出 Token',
    // Placeholder
    inputPlaceholder: '输入消息或命令...',
    inputDisabledPlaceholder: '请先选择或创建会话',
    send: '发送',
    // Vizo actions
    actionPause: '暂停',
    actionTerminate: '终止',
    actionResume: '恢复',
    actionRollback: '回滚',
    confirmPause: '确定要暂停任务吗？',
    confirmTerminate: '确定要终止任务吗？',
    // Time ago
    justNow: '刚刚',
    mAgo: '{n}分钟前',
    hAgo: '{n}小时前',
    dAgo: '{n}天前',
    // New session modal
    newSessionTitle: '新建会话',
    projectPath: '项目路径',
    customPath: '自定义路径',
    customPathPlaceholder: '输入项目目录路径...',
    sessionName: '会话名称（可选）',
    sessionNamePlaceholder: '默认使用项目名',
    create: '创建',
    cancel: '取消',
    pathNotExist: '路径不存在',
    loadingProjects: '加载中...',
    // Notification system
    agentStarted: '已启动',
    agentCompleted: '已完成',
    agentFailed: '执行失败',
    autopaused: '已自动暂停',
    taskPaused: '任务已暂停',
    taskTerminated: '任务已终止',
    taskCompleted: '任务已完成',
    codeRolledBack: '代码已回滚',
    viewFull: '查看全貌',
    viewDetails: '查看详情',
    openPanel: '打开面板',
    step: '步骤',
    steps: '步骤',
    duration: '耗时',
    analyzing: '正在分析中',
    unknownError: '未知错误',
    // Confirm notification
    confirmRequired: '请确认',
    clickToPreview: '点击预览',
    confirm: '确认',
    feedback: '意见',
    feedbackPlaceholder: '请输入修改意见...',
    submitFeedback: '提交意见',
    confirmed: '已确认，任务继续执行',
    cancelled: '已取消',
    feedbackSent: '已收到修改意见，正在修改...',
    waitedMinutes: '已等待 {n} 分钟',
    // Panel controls
    opusTask: 'Vizo 任务',
    rollbackStep: '回退步骤',
    viewReport: '查看报告',
    confirmRollback: '确认回退',
    resumeFeedbackPlaceholder: '可选：输入修改意见或补充指令（留空则直接继续）',
    resumeHint: '提交后任务将从暂停处继续执行',
    rollbackFeedbackPlaceholder: '可选：输入修改指令（告诉 AI 重做时需要注意什么）',
    selectStep: '请选择回退目标步骤',
    operationFailed: '操作失败',
    networkError: '网络错误，请重试',
    // Live log panel
    realTimeLog: '实时日志',
    follow: '↓ 跟随',
    clearLog: '清除',
    workingOn: '正在工作中',
    taskDone: '任务已完成',
    taskTerminated2: '任务已终止',
    taskRolledBack: '任务已回退',
    taskEnded: '任务已结束',
    waitingForAgent: '等待 {name} 开始工作...',
    events: '{n} events',
    disconnectedLog: '--- 连接中断，部分日志可能丢失 ---',
    expand: '展开',
    collapse: '收起',
    liveBannerText: '有任务正在直播',
    staleStep: '此步骤已被后续重试取代',
    // Status badges
    status_running: '运行中',
    status_completed: '已完成',
    status_error: '失败',
    status_paused: '已暂停',
    status_pending: '待执行',
    statusAbandoned: '已放弃',
    statusPending: '待启动',
    // 任务展示层优化新增
    status_unknown: '未知状态',
    status_rolled_back: '已回退',
    // Task types
    taskType_new_feature: '新功能',
    taskType_bug_fix: 'Bug修复',
    taskType_refactor: '重构',
    taskType_debug_embedded: '嵌入式调试',
    taskType_non_dev: '非开发',
    scaleLarge: '大需求',
    // Project Manager
    pmTitle: '项目管理',
    pmProjectList: '项目列表',
    pmNoProjects: '暂无项目',
    pmCreateHint: '点击右上角「+ 新建项目」创建',
    pmSelectProject: '请从左侧选择一个项目',
    pmLoading: '加载中...',
    pmLoadFailed: '加载失败',
    pmNoTasks: '暂无任务',
    pmNoName: '(无名称)',
    pmEmptyDir: '空目录',
    pmBinaryFile: '二进制文件，无法预览',
    pmContentTruncated: '... (内容过长，已截断)',
    pmBackToTerminal: '← 返回终端',
    pmNewProject: '+ 新建项目',
    pmDeleteProject: '删除项目',
    pmCreateTitle: '新建项目',
    pmProjectName: '项目名称',
    pmProjectDesc: '项目描述（可选）',
    pmProjectDescPlaceholder: '项目简介',
    pmNameRequired: '请输入项目名称',
    pmNameInvalid: '项目名称只允许字母、数字、下划线和连字符',
    pmCreated: '项目 {name} 创建成功',
    pmCreateFailed: '创建失败',
    pmDeleteConfirm: '确定要删除项目 "{name}" 吗？\n\n这将删除项目目录和所有文件，不可恢复。',
    pmDeleted: '项目已删除',
    pmDeleteFailed: '删除失败',
    pmProjectType: '项目类型',
    pmTypeDev: '开发项目',
    pmTypeHub: '工作区',
    pmDefaultModule: '默认模块（可选）',
    pmDefaultModuleNone: '无',
    pmSidebarBtn: '项目管理',
    // Agent Management
    agSidebarBtn: 'Agent 模块',
    agTitle: 'Agent 模块',
    agCreate: '创建新模块',
    agSearchPlaceholder: '搜索模块...',
    agAll: '全部',
    agBuiltin: '官方',
    agUser: '自建',
    agNoAgents: '暂无模块',
    agWorkflows: '个工作流',
    agVersion: '版本',
    agAuthor: '作者',
    agDescription: '描述',
    agWorkflowList: '工作流列表',
    agSteps: '步骤',
    agEdit: '编辑',
    agCopy: '复制为新模块',
    agDelete: '删除',
    agDeleteConfirm: '确定要删除模块 "{name}" 吗？此操作不可恢复。',
    agDeleted: '模块已删除',
    agDeleteFailed: '删除失败',
    agCopied: '模块已复制',
    agCopyFailed: '复制失败',
    agSaved: '模块已保存',
    agSaveFailed: '保存失败',
    agAiTitle: 'AI 辅助创建',
    agAiDesc: '描述你想要的 Agent 功能，AI 将自动生成模块配置',
    agAiPlaceholder: '例如：一个能帮我审查合同、识别风险条款的法律助手...',
    agAiGenerate: '生成模块',
    agAiGenerating: '正在生成...',
    agManualTitle: '手动创建',
    agPreviewTitle: '预览与编辑',
    agModuleId: '模块 ID',
    agModuleName: '模块名称',
    agModuleIcon: '图标',
    agModuleDesc: '模块描述',
    agSave: '保存',
    agCancel: '取消',
    agIdConflict: '模块 ID 已存在',
    agIdInvalid: '只允许字母、数字和下划线',
    agNewId: '新模块 ID',
    agEditTitle: '编辑模块',
    agBuiltinNoEdit: '内置模块不可编辑',
    agBuiltinNoDelete: '内置模块不可删除',
    agToolsLabel: '工具权限',
    agToolsBashWarn: '⚠️ Bash 工具允许执行任意命令，请确认是否需要',
    // Task Selector
    tsTitle: '选择任务',
    tsNoTasks: '暂无任务',
    tsNoProjects: '暂无项目',
    tsRunning: '运行中',
    tsCompleted: '已完成',
    tsPaused: '已暂停',
    tsFailed: '失败',
  },
  en: {
    collapseSidebar: 'Collapse sidebar',
    newSession: 'New Session',
    noSessions: 'No sessions yet',
    closeSession: 'Close session',
    interrupt: 'Interrupt',
    undo: 'Undo',
    commit: 'Commit',
    retry: 'Retry',
    compact: 'Compact',
    clearCtx: 'Clear',
    switchModel: 'Model',
    escape: 'Cancel',
    costCmd: 'Cost',
    wfNew: 'New Feature', wfFix: 'Bug Fix', wfRefactor: 'Refactor',
    wfAuto: 'AI Auto', wfEmbed: 'Embedded', wfNonDev: 'Non-Dev',
    allow: 'Allow',
    deny: 'Deny',
    pause: 'Pause',
    terminate: 'Terminate',
    resume: 'Resume',
    rollback: 'Rollback',
    inputBox: 'Input Box',
    terminal: 'Terminal',
    inputBoxTooltip: 'Input Box: type long messages below\nTerminal: type directly in terminal (Esc to switch)',
    connected: 'Connected',
    disconnected: 'Disconnected',
    reconnecting: 'Reconnecting...',
    livePanel: 'Live Panel',
    expand: 'Expand',
    collapse: 'Collapse',
    liveAll: 'Live All',
    focusLabel: 'Focus',
    welcomeTitle: 'Vizo Web Console',
    welcomeMsg1: 'Click \x1b[37m+ New Session\x1b[90m to start a conversation with Claude',
    welcomeMsg2: 'Or select an existing session from the sidebar',
    welcomeTip1: 'Bottom input box for typing messages',
    welcomeTip2: 'Switch to Terminal mode for direct keyboard control',
    connectingToSession: 'Connecting to session...',
    sessionClosedMsg: 'Session closed. Select or create a new session.',
    sessionRunningConfirm: 'This session is still running. Close it?',
    authFailed: 'Authentication failed. Redirecting to login...',
    sessionTakeover: 'Session taken over by another window',
    sessionNotFound: 'Session not found',
    replayedBuffer: 'Replayed buffered output',
    sessionEnded: 'Session ended',
    processExited: 'Process exited',
    reconnectFailed: 'Failed to reconnect after multiple attempts',
    maxSessionsReached: 'Max sessions reached. Close one first.',
    sessionCreated: 'Session created',
    createSessionFailed: 'Failed to create session',
    sessionClosed: 'Session closed',
    closeSessionFailed: 'Failed to close session',
    notConnected: 'Not connected',
    noOpusTask: 'No Vizo task found',
    taskActionSent: 'Task {action} sent',
    taskActionFailed: 'Failed to {action}',
    serverError: 'Server error',
    opusLive: 'Vizo Live',
    noOpusTaskRunning: 'No Vizo task running',
    startTaskHint: 'Start a task with <code style="background:var(--bg-input);padding:2px 6px;border-radius:3px">vizo "task"</code>',
    tabLive: 'Live',
    tabOverview: 'Overview',
    tabDocs: 'Docs',
    outputDocs: 'Output Documents',
    noDocsYet: 'No documents yet',
    inProgress: 'In progress...',
    progress: 'Progress',
    cost: 'Cost',
    unbillable: 'N/A',
    costInfoTip: 'Only Claude model costs are counted; external models are excluded',
    status: 'Status',
    taskDetails: 'Task Details',
    taskId: 'Task ID',
    totalCost: 'Total Cost',
    started: 'Started',
    model: 'Model',
    inputTokens: 'Input Tokens',
    outputTokens: 'Output Tokens',
    inputPlaceholder: 'Type a message or command...',
    inputDisabledPlaceholder: 'Select or create a session first',
    send: 'Send',
    actionPause: 'pause',
    actionTerminate: 'terminate',
    actionResume: 'resume',
    actionRollback: 'rollback',
    confirmPause: 'Are you sure you want to pause the task?',
    confirmTerminate: 'Are you sure you want to terminate the task?',
    justNow: 'just now',
    mAgo: '{n}m ago',
    hAgo: '{n}h ago',
    dAgo: '{n}d ago',
    newSessionTitle: 'New Session',
    projectPath: 'Project Path',
    customPath: 'Custom path',
    customPathPlaceholder: 'Enter project directory path...',
    sessionName: 'Session Name (optional)',
    sessionNamePlaceholder: 'Defaults to project name',
    create: 'Create',
    cancel: 'Cancel',
    pathNotExist: 'Path does not exist',
    loadingProjects: 'Loading...',
    agentStarted: 'started',
    agentCompleted: 'completed',
    agentFailed: 'failed',
    autopaused: 'auto-paused',
    taskPaused: 'Task paused',
    taskTerminated: 'Task terminated',
    taskCompleted: 'Task completed',
    codeRolledBack: 'Code rolled back',
    viewFull: 'View full',
    viewDetails: 'View details',
    openPanel: 'Open panel',
    step: 'Step',
    steps: 'Steps',
    duration: 'Duration',
    analyzing: 'Analyzing',
    unknownError: 'Unknown error',
    confirmRequired: 'Confirmation required',
    clickToPreview: 'Click to preview',
    confirm: 'Confirm',
    feedback: 'Feedback',
    feedbackPlaceholder: 'Enter your feedback...',
    submitFeedback: 'Submit feedback',
    confirmed: 'Confirmed, task continues',
    cancelled: 'Cancelled',
    feedbackSent: 'Feedback received, revising...',
    waitedMinutes: 'Waited {n} minutes',
    opusTask: 'Vizo Task',
    rollbackStep: 'Rollback step',
    viewReport: 'View report',
    confirmRollback: 'Confirm rollback',
    resumeFeedbackPlaceholder: 'Optional: feedback or instructions (leave empty to continue)',
    resumeHint: 'Task will continue from where it paused',
    rollbackFeedbackPlaceholder: 'Optional: instructions for redo (what should AI focus on)',
    selectStep: 'Please select a target step',
    operationFailed: 'Operation failed',
    networkError: 'Network error, please retry',
    // Live log panel
    realTimeLog: 'Live Log',
    follow: '↓ Follow',
    clearLog: 'Clear',
    workingOn: 'Working',
    taskDone: 'Task completed',
    taskTerminated2: 'Task terminated',
    taskRolledBack: 'Task rolled back',
    taskEnded: 'Task ended',
    waitingForAgent: 'Waiting for {name} to start...',
    events: '{n} events',
    disconnectedLog: '--- Disconnected, some logs may be lost ---',
    expand: 'Expand',
    collapse: 'Collapse',
    liveBannerText: 'Live task running',
    staleStep: 'Superseded by later retry',
    // Status badges
    status_running: 'Running',
    status_completed: 'Completed',
    status_error: 'Error',
    status_paused: 'Paused',
    status_pending: 'Pending',
    statusAbandoned: 'Abandoned',
    statusPending: 'Pending',
    // Task types
    taskType_new_feature: 'New Feature',
    taskType_bug_fix: 'Bug Fix',
    taskType_refactor: 'Refactor',
    taskType_debug_embedded: 'Embedded Debug',
    taskType_non_dev: 'Non-Dev',
    scaleLarge: 'Large',
    // Project Manager
    pmTitle: 'Projects',
    pmProjectList: 'Projects',
    pmNoProjects: 'No projects yet',
    pmCreateHint: 'Click "+ New Project" to create one',
    pmSelectProject: 'Select a project from the left',
    pmLoading: 'Loading...',
    pmLoadFailed: 'Load failed',
    pmNoTasks: 'No tasks',
    pmNoName: '(unnamed)',
    pmEmptyDir: 'Empty directory',
    pmBinaryFile: 'Binary file, cannot preview',
    pmContentTruncated: '... (content truncated)',
    pmBackToTerminal: '← Back to Terminal',
    pmNewProject: '+ New Project',
    pmDeleteProject: 'Delete Project',
    pmCreateTitle: 'New Project',
    pmProjectName: 'Project Name',
    pmProjectDesc: 'Description (optional)',
    pmProjectDescPlaceholder: 'Brief description',
    pmNameRequired: 'Please enter a project name',
    pmNameInvalid: 'Only letters, digits, underscores and hyphens allowed',
    pmCreated: 'Project {name} created',
    pmCreateFailed: 'Failed to create',
    pmDeleteConfirm: 'Delete project "{name}"?\n\nThis will remove the directory and all files permanently.',
    pmDeleted: 'Project deleted',
    pmDeleteFailed: 'Failed to delete',
    pmProjectType: 'Project Type',
    pmTypeDev: 'Development',
    pmTypeHub: 'Workspace',
    pmDefaultModule: 'Default Module (optional)',
    pmDefaultModuleNone: 'None',
    pmSidebarBtn: 'Projects',
    // Agent Management
    agSidebarBtn: 'Agents',
    agTitle: 'Agent Modules',
    agCreate: 'Create Module',
    agSearchPlaceholder: 'Search modules...',
    agAll: 'All',
    agBuiltin: 'Built-in',
    agUser: 'Custom',
    agNoAgents: 'No modules',
    agWorkflows: ' workflows',
    agVersion: 'Version',
    agAuthor: 'Author',
    agDescription: 'Description',
    agWorkflowList: 'Workflows',
    agSteps: 'Steps',
    agEdit: 'Edit',
    agCopy: 'Copy as new',
    agDelete: 'Delete',
    agDeleteConfirm: 'Delete module "{name}"? This cannot be undone.',
    agDeleted: 'Module deleted',
    agDeleteFailed: 'Delete failed',
    agCopied: 'Module copied',
    agCopyFailed: 'Copy failed',
    agSaved: 'Module saved',
    agSaveFailed: 'Save failed',
    agAiTitle: 'AI-Assisted Creation',
    agAiDesc: 'Describe the agent you want, AI will generate the module config',
    agAiPlaceholder: 'e.g. A legal assistant that reviews contracts and identifies risk clauses...',
    agAiGenerate: 'Generate',
    agAiGenerating: 'Generating...',
    agManualTitle: 'Manual Creation',
    agPreviewTitle: 'Preview & Edit',
    agModuleId: 'Module ID',
    agModuleName: 'Module Name',
    agModuleIcon: 'Icon',
    agModuleDesc: 'Description',
    agSave: 'Save',
    agCancel: 'Cancel',
    agIdConflict: 'Module ID already exists',
    agIdInvalid: 'Only letters, numbers and underscores allowed',
    agNewId: 'New Module ID',
    agEditTitle: 'Edit Module',
    agBuiltinNoEdit: 'Built-in modules cannot be edited',
    agBuiltinNoDelete: 'Built-in modules cannot be deleted',
    agToolsLabel: 'Tool Permissions',
    agToolsBashWarn: '⚠️ Bash allows arbitrary command execution, confirm if needed',
    // Task Selector
    tsTitle: 'Select Task',
    tsNoTasks: 'No tasks',
    tsNoProjects: 'No projects',
    tsRunning: 'Running',
    tsCompleted: 'Completed',
    tsPaused: 'Paused',
    tsFailed: 'Failed',
  }
};

function t(key, params) {
  let s = (LANGS[S.lang] || LANGS.zh)[key] || key;
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      s = s.replace('{' + k + '}', v);
    }
  }
  return s;
}

// ======================== Theme ========================
const TERMINAL_THEMES = {
  dark: {
    background: '#0a0e17',
    foreground: '#e2e8f0',
    cursor: 'transparent',
    cursorAccent: 'transparent',
    selectionBackground: 'rgba(56,189,248,0.3)',
    black: '#1a2332', red: '#ef4444', green: '#22c55e', yellow: '#eab308',
    blue: '#38bdf8', magenta: '#a78bfa', cyan: '#06b6d4', white: '#e2e8f0',
    brightBlack: '#64748b', brightRed: '#f87171', brightGreen: '#4ade80',
    brightYellow: '#facc15', brightBlue: '#7dd3fc', brightMagenta: '#c4b5fd',
    brightCyan: '#22d3ee', brightWhite: '#f8fafc',
  },
  light: {
    background: '#ffffff',
    foreground: '#1d1d1f',
    cursor: 'transparent',
    cursorAccent: 'transparent',
    selectionBackground: 'rgba(0,113,227,0.2)',
    black: '#1d1d1f', red: '#d32f2f', green: '#2e7d32', yellow: '#f57f17',
    blue: '#1565c0', magenta: '#7b1fa2', cyan: '#00838f', white: '#fafafa',
    brightBlack: '#757575', brightRed: '#ef5350', brightGreen: '#66bb6a',
    brightYellow: '#ffee58', brightBlue: '#42a5f5', brightMagenta: '#ab47bc',
    brightCyan: '#26c6da', brightWhite: '#ffffff',
  }
};

function getTheme() {
  try { var t = document.documentElement.getAttribute('data-theme'); return t === 'light' ? 'light' : 'dark'; }
  catch(e) { return 'dark'; }
}

function syncTerminalTheme(theme) {
  if (!S.term) return;
  var base = TERMINAL_THEMES[theme] || TERMINAL_THEMES.dark;
  // Preserve current cursor state (input box mode keeps transparent)
  var cur = S.term.options.theme || {};
  S.term.options.theme = Object.assign({}, base, { cursor: cur.cursor || 'transparent', cursorAccent: cur.cursorAccent || 'transparent' });
}

function updateThemeButtonIcon(theme) {
  var btn = document.getElementById('theme-toggle');
  if (btn) btn.innerHTML = theme === 'dark' ? '&#9728;&#65039;' : '&#127769;';
}

function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  syncTerminalTheme(theme);
  updateThemeButtonIcon(theme);
  try { localStorage.setItem('opus_theme', theme); } catch(e) {}
}

function toggleTheme() {
  applyTheme(getTheme() === 'dark' ? 'light' : 'dark');
}

function setLang(lang) {
  S.lang = lang;
  localStorage.setItem('opus_lang', lang);
  // Update lang toggle button
  const btn = document.getElementById('lang-toggle');
  if (btn) btn.textContent = lang === 'zh' ? 'EN' : '中';
  applyLang();
}

function applyLang() {
  // Sidebar
  document.querySelector('#sidebar .icon-btn[onclick*="toggleSidebar"]').title = t('collapseSidebar');
  document.getElementById('new-session-btn').innerHTML = '<span>+</span> ' + t('newSession');
  // Toolbar buttons (by data-i18n attributes)
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const key = el.getAttribute('data-i18n');
    const icon = el.getAttribute('data-icon') || '';
    el.innerHTML = icon ? icon + ' ' + t(key) : t(key);
  });
  document.querySelectorAll('[data-i18n-title]').forEach(el => {
    el.title = t(el.getAttribute('data-i18n-title'));
  });
  // Input mode button (removed, guard for safety)
  const modeBtn = document.getElementById('input-mode-btn');
  if (modeBtn) {
    modeBtn.innerHTML = S.inputBoxVisible ? '&#9000; ' + t('inputBox') : '&#9000; ' + t('terminal');
    modeBtn.title = t('inputBoxTooltip');
  }
  // Connection status
  const connText = document.getElementById('conn-text');
  if (S.isConnected) connText.textContent = t('connected');
  else if (connText.textContent) {
    const dot = document.getElementById('conn-dot');
    if (dot.classList.contains('reconnecting')) connText.textContent = t('reconnecting');
    else connText.textContent = t('disconnected');
  }
  // Live panel
  document.getElementById('panel-toggle').textContent = t('livePanel');
  var hdrName = document.getElementById('hdr-name');
  if (hdrName && !S.opusTask) hdrName.textContent = t('opusLive');
  // No task message
  const noTaskMsg = document.getElementById('no-task-msg');
  if (noTaskMsg) {
    noTaskMsg.innerHTML = '<div class="no-task-icon">&#128640;</div><div>' + t('noOpusTaskRunning') + '</div><div style="font-size:0.75rem">' + t('startTaskHint') + '</div>';
  }
  // Reconnect overlay
  const reconnectText = document.querySelector('.reconnect-text');
  if (reconnectText) reconnectText.textContent = t('reconnecting');
  // Input placeholder & send button
  updateUIState();
  const sendBtn = document.querySelector('.send-btn');
  if (sendBtn) sendBtn.textContent = t('send');
  // Re-render sessions with new language
  renderSessions();
  // Re-render opus panel if task exists
  if (S.opusTask) updateOpusPanel(S.opusTask);
}

// ======================== Terminal ========================
function initTerminal() {
  const currentTheme = TERMINAL_THEMES[getTheme()] || TERMINAL_THEMES.dark;
  S.term = new Terminal({
    cursorBlink: false,  // 默认 Input Box 模式，终端只读
    cursorInactiveStyle: 'none',
    fontSize: 14,
    fontFamily: '"JetBrains Mono", "Fira Code", "Cascadia Code", "SF Mono", Consolas, monospace',
    theme: currentTheme,
    allowProposedApi: true,
    scrollback: 5000,
  });

  S.fitAddon = new FitAddon.FitAddon();
  S.term.loadAddon(S.fitAddon);

  try {
    const webLinksAddon = new WebLinksAddon.WebLinksAddon();
    S.term.loadAddon(webLinksAddon);
  } catch(e) {}

  S.term.open(document.getElementById('terminal'));
  // WebGL renderer — GPU accelerated, significantly faster
  try {
    if (typeof WebglAddon !== 'undefined') {
      const webglAddon = new WebglAddon.WebglAddon();
      webglAddon.onContextLoss(() => {
        // 保存当前 viewport 位置
        var viewport = document.querySelector('#terminal .xterm-viewport');
        var savedScrollTop = viewport ? viewport.scrollTop : -1;
        webglAddon.dispose();
        // context loss 后恢复 viewport 位置
        if (savedScrollTop > 0) {
          requestAnimationFrame(() => {
            var vp = document.querySelector('#terminal .xterm-viewport');
            if (vp) vp.scrollTop = savedScrollTop;
          });
        }
      });
      S.term.loadAddon(webglAddon);
    }
  } catch(e) {}

  // 守卫：WebGL context loss 后恢复 viewport 位置
  requestAnimationFrame(() => {
    var vp = document.querySelector('#terminal .xterm-viewport');
    if (vp) {
      var _lastKnownScroll = 0;
      vp.addEventListener('scroll', () => {
        if (vp.scrollTop > 0) _lastKnownScroll = vp.scrollTop;
      });
    }
  });
  S.fitAddon.fit();

  // Intercept Ctrl+C/Ctrl+V before xterm for clipboard copy/paste
  S.term.attachCustomKeyEventHandler((e) => {
    // Ctrl+C: 有选中文本时复制，否则交给 xterm 发送 SIGINT
    if ((e.ctrlKey || e.metaKey) && e.key === 'c' && e.type === 'keydown') {
      var selection = S.term.getSelection();
      if (selection) {
        navigator.clipboard.writeText(selection).catch(() => {});
        S.term.clearSelection();
        return false;  // 阻止 xterm 处理
      }
      return true;  // 无选中，交给 xterm 发送 Ctrl+C
    }
    // Ctrl+V: 从剪贴板粘贴
    if ((e.ctrlKey || e.metaKey) && e.key === 'v' && e.type === 'keydown') {
      navigator.clipboard.readText().then(text => {
        if (text && S.ws && S.ws.readyState === WebSocket.OPEN) {
          S.ws.send(JSON.stringify({ type: 'input', data: text }));
        }
      }).catch(() => {});
      return false;
    }
    return true;
  });

  // User input -> WebSocket
  S.term.onData(data => {
    if (S.ws && S.ws.readyState === WebSocket.OPEN) {
      S.ws.send(JSON.stringify({ type: 'input', data: data }));
    }
  });

  // Handle terminal resize
  S.term.onResize(({ cols, rows }) => {
    if (S.ws && S.ws.readyState === WebSocket.OPEN) {
      S.ws.send(JSON.stringify({ type: 'resize', cols, rows }));
    }
  });

  // Window resize -> fit terminal (throttled)
  window.addEventListener('resize', () => {
    if (S.resizeTimer) clearTimeout(S.resizeTimer);
    S.resizeTimer = setTimeout(() => {
      if (S.fitAddon) S.fitAddon.fit();
    }, 100);
  });

  S.term.write('\r\n  \x1b[1;36m' + t('welcomeTitle') + '\x1b[0m\r\n\r\n  \x1b[90m→ ' + t('welcomeMsg1') + '\r\n  → ' + t('welcomeMsg2') + '\r\n\r\n  ' + t('welcomeTip1') + '\r\n        ' + t('welcomeTip2') + '\x1b[0m\r\n\r\n');

  // Mobile: show input row, terminal read-only (no keyboard on tap)
  var isMobile = window.matchMedia('(hover: none) and (pointer: coarse)').matches;
  if (isMobile) {
    S.inputBoxVisible = true;
    document.getElementById('input-row').style.display = 'flex';
    document.querySelector('.terminal-container').classList.add('input-box-mode');
    // Prevent virtual keyboard when tapping terminal
    if (S.term.textarea) {
      S.term.textarea.setAttribute('inputmode', 'none');
      S.term.textarea.setAttribute('readonly', '');
    }
    // Prevent xterm from stealing focus on touch
    var termEl = document.getElementById('terminal');
    termEl.addEventListener('touchstart', function(e) {
      // Allow scrolling but prevent focus/keyboard
      if (S.term.textarea) S.term.textarea.blur();
    }, { passive: true });
  } else {
    // Desktop: terminal interactive, auto-focus
    S.term.focus();
  }
}

// ======================== WebSocket ========================
function connectWS(sessionId) {
  if (S.ws) {
    S.ws.onclose = null;
    S.ws.close();
    S.ws = null;
  }

  clearPingInterval();

  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = `${proto}//${location.host}/vizo/console/ws?session_id=${sessionId}`;

  updateConnectionStatus('reconnecting');

  const ws = new WebSocket(url);
  S.ws = ws;

  ws.onopen = () => {
    S.reconnectAttempts = 0;
    S.isConnected = true;
    updateConnectionStatus('connected');
    updateUIState();
    hideReconnectOverlay();
    startPingInterval();

    // Send initial resize
    if (S.fitAddon) {
      S.fitAddon.fit();
      const dims = S.fitAddon.proposeDimensions();
      if (dims) {
        ws.send(JSON.stringify({ type: 'resize', cols: dims.cols, rows: dims.rows }));
      }
    }
  };

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      handleWSMessage(msg);
    } catch (e) {
      console.error('WS message parse error:', e);
    }
  };

  ws.onclose = (event) => {
    S.ws = null;
    S.isConnected = false;
    clearPingInterval();
    updateUIState();

    if (event.code === 4001) {
      showToast(t('authFailed'), 'error');
      setTimeout(() => { location.href = '/vizo/console/login'; }, 2000);
      return;
    }
    if (event.code === 4002) {
      showToast(t('sessionTakeover'), 'error');
      updateConnectionStatus('disconnected');
      return;  // 不自动重连——已被接管
    }
    if (event.code === 4004) {
      showToast(t('sessionNotFound'), 'error');
      updateConnectionStatus('disconnected');
      loadSessions();
      return;
    }

    updateConnectionStatus('disconnected');
    // F5.10: 在日志区插入断线提示
    insertDisconnectMessage();
    scheduleReconnect(sessionId);
  };

  ws.onerror = () => {};
}

// Write batching — accumulate output, flush once per frame
let _writeBuf = '';
let _writeRAF = 0;
function batchWrite(data) {
  _writeBuf += data;
  if (!_writeRAF) {
    _writeRAF = requestAnimationFrame(() => {
      if (S.term && _writeBuf) {
        // 写入前用 xterm buffer API 记录用户滚动偏移（比 DOM scrollTop 更可靠）
        var buf = S.term.buffer.active;
        var linesFromBottom = buf.baseY - buf.viewportY;  // 0 = 在底部
        var chunk = _writeBuf;
        _writeBuf = '';
        _writeRAF = 0;
        // 使用 term.write(data, callback)：回调在 xterm.js 渲染完成后触发
        S.term.write(chunk, () => {
          if (linesFromBottom > 0) {
            // 用户之前不在底部，恢复到相同的「距底部行数」位置
            var newBuf = S.term.buffer.active;
            var target = newBuf.baseY - linesFromBottom;
            if (target < 0) target = 0;
            S.term.scrollToLine(target);
          }
          // 在底部时 xterm.js 自动跟随，无需干预
        });
        return;  // 已在 write callback 中清理，提前返回
      }
      _writeBuf = '';
      _writeRAF = 0;
    });
  }
}

function handleWSMessage(msg) {
  switch (msg.type) {
    case 'output':
      batchWrite(msg.data);
      S.lastOutputChunk = msg.data;
      detectPermissionPrompt(msg.data);
      break;
    case 'replay':
      if (msg.data) {
        batchWrite(msg.data);
        showToast(t('replayedBuffer'), 'info');
      }
      break;
    case 'pong':
      clearPongTimeout();
      break;
    case 'session_ended':
      showToast(t('sessionEnded') + ': ' + (msg.reason || t('processExited')), 'warning');
      updateConnectionStatus('disconnected');
      loadSessions();
      break;
    case 'opus_event':
      handleOpusEvent(msg);
      break;
    case 'takeover':
      showToast(t('sessionTakeover'), 'error');
      break;
    case 'error':
      showToast(msg.message || t('serverError'), 'error');
      break;
  }
}

function scheduleReconnect(sessionId) {
  if (S.reconnectAttempts >= S.maxReconnectAttempts) {
    showToast(t('reconnectFailed'), 'error');
    return;
  }

  S.reconnectAttempts++;
  const delay = Math.min(1000 * Math.pow(1.5, S.reconnectAttempts - 1), 30000);

  updateConnectionStatus('reconnecting');
  if (S.reconnectAttempts > 2) showReconnectOverlay();

  S.reconnectTimer = setTimeout(() => {
    connectWS(sessionId);
  }, delay);
}

// ======================== Heartbeat ========================
function startPingInterval() {
  clearPingInterval();
  S.pingInterval = setInterval(() => {
    if (S.ws && S.ws.readyState === WebSocket.OPEN) {
      S.ws.send(JSON.stringify({ type: 'ping' }));
      // Clear previous pong timeout before setting new one
      clearPongTimeout();
      // Expect pong within 90s (generous margin for mobile/unstable networks)
      S.pongTimeout = setTimeout(() => {
        if (S.ws) {
          S.ws.close();
        }
      }, 90000);
    }
  }, 30000);
}

function clearPingInterval() {
  if (S.pingInterval) { clearInterval(S.pingInterval); S.pingInterval = null; }
  clearPongTimeout();
}

function clearPongTimeout() {
  if (S.pongTimeout) { clearTimeout(S.pongTimeout); S.pongTimeout = null; }
}

// ======================== Permission Detection ========================
function detectPermissionPrompt(data) {
  // Strip ANSI escape sequences for reliable text matching
  const clean = data.replace(/\x1b\[[0-9;]*[a-zA-Z]/g, '');
  const cleanBuf = (S._outputBuf + clean).slice(-500);
  S._outputBuf = cleanBuf;
  // Detect permission prompts: "Allow" + "(y/n)" or "allow this action"
  const hasPrompt = (clean.includes('Allow') && clean.includes('(y/n)')) ||
                    clean.includes('allow this action') ||
                    (cleanBuf.includes('Allow') && cleanBuf.includes('? (y/n)'));
  if (hasPrompt && !S.permissionPending) {
    S.permissionPending = true;
    document.getElementById('cc-allow').classList.add('allow-highlight');
    document.getElementById('cc-deny').classList.add('deny-highlight');
  }
  // Auto-clear permission state when we see output after user responded
  if (S.permissionPending && !hasPrompt && clean.length > 20 && !clean.includes('(y/n)')) {
    clearPermissionState();
  }
}

function clearPermissionState() {
  S.permissionPending = false;
  document.getElementById('cc-allow').classList.remove('allow-highlight');
  document.getElementById('cc-deny').classList.remove('deny-highlight');
}

function updateUIState() {
  const connected = S.isConnected;
  // Input area
  const inputArea = document.getElementById('input-area');
  if (inputArea) inputArea.classList.toggle('disabled', !connected);
  const userInput = document.getElementById('user-input');
  if (userInput) {
    userInput.placeholder = connected ? t('inputPlaceholder') : t('inputDisabledPlaceholder');
    userInput.disabled = !connected;
  }
}

// ======================== Sessions ========================
async function loadSessions() {
  try {
    const resp = await fetch('/vizo/console/api/legacy/sessions');
    if (!resp.ok) throw new Error('Failed to load sessions');
    const data = await resp.json();
    S.sessions = data.sessions || [];
    renderSessions();
  } catch (e) {
    console.error('Load sessions error:', e);
  }
}

function renderSessions() {
  const list = document.getElementById('session-list');
  if (S.sessions.length === 0) {
    list.innerHTML = '<div style="text-align:center;color:var(--text-muted);font-size:0.78rem;padding:1rem">' + t('noSessions') + '</div>';
    return;
  }

  list.innerHTML = S.sessions.map(s => `
    <div class="session-item ${s.id === S.activeSessionId ? 'active' : ''}"
         onclick="switchSession('${s.id}')">
      <div class="session-name">${escapeHtml(s.name)}</div>
      ${s.cwd ? `<div class="session-cwd" title="${escapeHtml(s.cwd)}">${escapeHtml(s.cwd)}</div>` : ''}
      <div class="session-meta">
        <span class="session-status ${s.status}">${s.status}</span>
        <span class="session-time">${formatTime(s.created_at)}</span>
      </div>
      <button class="session-delete" onclick="event.stopPropagation();confirmDeleteSession('${s.id}','${s.status}')" title="${t('closeSession')}">&times;</button>
    </div>
  `).join('');
}

// ======================== New Session Modal ========================
async function createSession() {
  // Open modal instead of creating directly
  const modal = document.getElementById('new-session-modal');
  modal.style.display = '';

  // Update modal labels with current language
  document.getElementById('modal-title').textContent = t('newSessionTitle');
  document.getElementById('modal-label-path').textContent = t('projectPath');
  document.getElementById('modal-label-name').textContent = t('sessionName');
  document.getElementById('modal-session-name').placeholder = t('sessionNamePlaceholder');
  modal.querySelector('.modal-btn-cancel').textContent = t('cancel');
  modal.querySelector('.modal-btn-create').textContent = t('create');

  // Load projects
  const list = document.getElementById('project-list');
  list.innerHTML = '<div style="color:var(--text-muted);font-size:0.8rem;padding:0.5rem">' + t('loadingProjects') + '</div>';

  try {
    const resp = await fetch('/vizo/console/api/projects');
    const data = await resp.json();
    const projects = data.projects || [];

    let html = '';
    projects.forEach((p, i) => {
      html += `
        <label class="project-option${i === 0 ? ' selected' : ''}" onclick="selectProject(this, '${escapeHtml(p.path)}', '${escapeHtml(p.name)}')">
          <input type="radio" name="project" value="${escapeHtml(p.path)}" ${i === 0 ? 'checked' : ''}>
          <div>
            <div class="proj-name">${escapeHtml(p.name)}</div>
            <div class="proj-path">${escapeHtml(p.path)}</div>
          </div>
        </label>`;
    });
    // Custom path option
    html += `
      <label class="project-option" onclick="selectProject(this, '__custom__', '')">
        <input type="radio" name="project" value="__custom__">
        <div style="flex:1">
          <div class="proj-name">${t('customPath')}</div>
          <input type="text" class="project-custom-input" id="custom-path-input"
                 placeholder="${t('customPathPlaceholder')}"
                 onclick="event.stopPropagation(); this.closest('.project-option').querySelector('input[type=radio]').checked=true; selectProject(this.closest('.project-option'), '__custom__', '')">
        </div>
      </label>`;
    list.innerHTML = html;

    // Auto-fill session name with first project
    if (projects.length > 0) {
      document.getElementById('modal-session-name').value = '';
      S._selectedProjectName = projects[0].name;
      S._selectedProjectPath = projects[0].path;
    }
  } catch(e) {
    list.innerHTML = '<div style="color:var(--red);font-size:0.8rem;padding:0.5rem">Failed to load projects</div>';
  }
}

function selectProject(el, path, name) {
  // Update selected style
  document.querySelectorAll('.project-option').forEach(o => o.classList.remove('selected'));
  el.classList.add('selected');
  el.querySelector('input[type=radio]').checked = true;
  S._selectedProjectPath = path;
  S._selectedProjectName = name;

  // Focus custom input if custom selected
  if (path === '__custom__') {
    setTimeout(() => {
      const inp = document.getElementById('custom-path-input');
      if (inp) inp.focus();
    }, 50);
  }
}

function closeNewSessionModal() {
  document.getElementById('new-session-modal').style.display = 'none';
}

function closeSessionSwitchConfirm(confirmed) {
  const modal = document.getElementById('session-switch-confirm-modal');
  if (modal) modal.style.display = 'none';
  const resolver = S._sessionSwitchConfirmResolver;
  S._sessionSwitchConfirmResolver = null;
  if (resolver) resolver(!!confirmed);
}

function confirmSessionHotSwap(options) {
  options = options || {};
  if (!S.activeSessionId) return Promise.resolve(true);
  const modal = document.getElementById('session-switch-confirm-modal');
  const titleEl = document.getElementById('session-switch-confirm-title');
  const copyEl = document.getElementById('session-switch-confirm-copy');
  const targetEl = document.getElementById('session-switch-confirm-target');
  const linesEl = document.getElementById('session-switch-confirm-lines');
  const okBtn = document.getElementById('session-switch-confirm-ok');
  const cancelBtn = document.getElementById('session-switch-confirm-cancel');
  const actionLabel = options.actionLabel || '切换当前会话';
  const targetLabel = options.targetLabel || '';
  const okLabel = options.okLabel || '继续切换';

  titleEl.textContent = options.title || '应用到当前会话？';
  copyEl.innerHTML = '你即将<strong>' + escapeHtml(actionLabel) + '</strong>。系统会保留当前 Web 会话和 Claude 对话上下文，但会重启底层 Claude 进程来清除旧模型状态。';
  targetEl.textContent = targetLabel ? ('目标配置：' + targetLabel) : '';
  linesEl.innerHTML = [
    '切换期间当前输出会短暂中断，通常持续 1 到 3 秒。',
    '如果当前正在生成关键回复，建议等待本轮输出结束后再切换。',
    '若上游恢复失败，当前会话可能仍沿用旧模型，需要你再执行一次 /model。'
  ].map(function(line) {
    return '<div class="session-switch-confirm-line">' + escapeHtml(line) + '</div>';
  }).join('');
  okBtn.textContent = okLabel;
  cancelBtn.textContent = '取消';
  modal.style.display = '';

  return new Promise(function(resolve) {
    S._sessionSwitchConfirmResolver = resolve;
  });
}

async function doCreateSession() {
  let cwd = S._selectedProjectPath || '';
  let name = document.getElementById('modal-session-name').value.trim();

  // Handle custom path
  if (cwd === '__custom__') {
    cwd = (document.getElementById('custom-path-input')?.value || '').trim();
    if (!cwd) {
      showToast(t('customPathPlaceholder'), 'warning');
      return;
    }
  }

  // Use project name as session name if not specified; for custom path use dir basename
  if (!name) {
    if (S._selectedProjectName) {
      name = S._selectedProjectName;
    } else if (cwd) {
      name = cwd.replace(/\/+$/, '').split('/').pop() || cwd;
    }
  }

  closeNewSessionModal();

  try {
    const resp = await fetch('/vizo/console/api/legacy/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: name,
        cwd: cwd,
        cols: S.fitAddon ? S.fitAddon.proposeDimensions()?.cols || 120 : 120,
        rows: S.fitAddon ? S.fitAddon.proposeDimensions()?.rows || 40 : 40,
      }),
    });

    if (resp.status === 409) {
      showToast(t('maxSessionsReached'), 'warning');
      return;
    }
    if (!resp.ok) throw new Error('Create session failed');

    const data = await resp.json();
    showToast(t('sessionCreated'), 'success');
    await loadSessions();
    switchSession(data.session.id);
  } catch (e) {
    showToast(t('createSessionFailed') + ': ' + e.message, 'error');
  }
}

function switchSession(sessionId) {
  if (sessionId === S.activeSessionId) return;

  S.activeSessionId = sessionId;
  localStorage.setItem('opus_last_session', sessionId);
  renderSessions();

  // Clear terminal and show transition
  if (S.term) {
    S.term.clear();
    S.term.reset();
    S.term.write('\r\n  \x1b[36m' + t('connectingToSession') + '\x1b[0m\r\n\r\n');
  }
  clearPermissionState();
  connectWS(sessionId);
}

function confirmDeleteSession(sessionId, status) {
  if (status === 'running') {
    if (!confirm(t('sessionRunningConfirm'))) return;
  }
  deleteSession(sessionId);
}

async function deleteSession(sessionId) {
  try {
    await fetch(`/vizo/console/api/legacy/sessions/${sessionId}`, { method: 'DELETE' });
    if (sessionId === S.activeSessionId) {
      S.activeSessionId = null;
      S.isConnected = false;
      if (S.ws) { S.ws.onclose = null; S.ws.close(); S.ws = null; }
      updateConnectionStatus('disconnected');
      updateUIState();
      if (S.term) {
        S.term.clear();
        S.term.reset();
        S.term.write('\r\n  ' + t('sessionClosedMsg') + '\r\n\r\n');
      }
    }
    await loadSessions();
    showToast(t('sessionClosed'), 'info');
  } catch (e) {
    showToast(t('closeSessionFailed'), 'error');
  }
}

// ======================== Toolbar Actions ========================
function sendPtyKey(data) {
  if (S.ws && S.ws.readyState === WebSocket.OPEN) {
    S.ws.send(JSON.stringify({ type: 'input', data: data }));
    if (data === 'y\r' || data === 'n\r') {
      clearPermissionState();
    }
  }
}

function fillWorkflow(type) {
  if (!S.activeSessionId || !S.ws || S.ws.readyState !== 1) return;
  var cmd;
  if (type === 'auto') {
    cmd = 'opus "';
  } else {
    cmd = 'opus --workflow ' + type + ' "';
  }
  var payload = '\x15' + cmd;
  S.ws.send(JSON.stringify({type: 'input', data: payload}));
}

// ======================== Vizo Panel Actions ========================
async function opusPanelAction(action) {
  if (!S.opusTask) return;
  // Terminate action: show choice modal if has code changes
  if (action === 'terminate') {
    if (S.opusTask.has_code_changes) {
      showTerminateChoiceModal();
    } else {
      if (!confirm(t('confirmTerminate'))) return;
      doTerminateRequest(false);
    }
    return;
  }
  var confirmMsgs = {pause: t('confirmPause'), resume: '确认重试？将从失败步骤继续执行。'};
  var confirmMsg = confirmMsgs[action] || t('confirmTerminate');
  if (!confirm(confirmMsg)) return;
  try {
    const resp = await fetch('/vizo/api/tasks/' + S.opusTask.task_id + '/' + action, {method: 'POST'});
    const data = await resp.json();
    if (data.status === 'ok') {
      showToast(data.message, 'success');
      setTimeout(pollOpusTask, 1500);
    } else {
      showToast(data.message || t('operationFailed'), 'error');
    }
  } catch(e) {
    showToast(t('networkError'), 'error');
  }
}

function showTerminateChoiceModal() {
  var overlay = document.createElement('div');
  overlay.id = 'terminateChoiceOverlay';
  overlay.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.5);z-index:9999;display:flex;align-items:center;justify-content:center;';
  overlay.innerHTML = '<div style="background:var(--bg-secondary,#1e1e2e);border:1px solid var(--border,#333);border-radius:var(--radius-lg);padding:24px;max-width:400px;width:90%;text-align:center;">' +
    '<div style="font-size:var(--font-size-xl);font-weight:600;margin-bottom:8px;">终止任务</div>' +
    '<div style="color:var(--text-secondary,#888);font-size:var(--font-size-lg);margin-bottom:20px;">此任务有代码变更，请选择处理方式：</div>' +
    '<div style="display:flex;flex-direction:column;gap:10px;">' +
    '<button onclick="closeTerminateChoice();doTerminateRequest(true)" style="padding:10px 16px;border-radius:var(--radius);border:none;background:var(--accent,#7c3aed);color:#fff;font-size:var(--font-size-lg);cursor:pointer;">🔄 回滚代码（推荐）</button>' +
    '<button onclick="closeTerminateChoice();doTerminateRequest(false)" style="padding:10px 16px;border-radius:var(--radius);border:1px solid var(--border,#333);background:transparent;color:var(--text-primary,#ccc);font-size:var(--font-size-lg);cursor:pointer;">📌 保留代码</button>' +
    '<button onclick="closeTerminateChoice()" style="padding:8px 16px;border-radius:var(--radius);border:none;background:transparent;color:var(--text-secondary,#888);font-size:var(--font-size-sm);cursor:pointer;">取消</button>' +
    '</div></div>';
  overlay.addEventListener('click', function(e) { if (e.target === overlay) closeTerminateChoice(); });
  document.body.appendChild(overlay);
}

function closeTerminateChoice() {
  var el = document.getElementById('terminateChoiceOverlay');
  if (el) el.remove();
}

async function doTerminateRequest(rollback) {
  if (!S.opusTask) return;
  try {
    const resp = await fetch('/vizo/api/tasks/' + S.opusTask.task_id + '/terminate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({rollback: rollback})
    });
    const data = await resp.json();
    if (data.status === 'ok') {
      showToast(data.message, 'success');
      setTimeout(pollOpusTask, 1500);
    } else {
      showToast(data.message || t('operationFailed'), 'error');
    }
  } catch(e) {
    showToast(t('networkError'), 'error');
  }
}

// ======================== Button Set Configuration ========================
var BUTTON_SET_MAP = {
  doc_review: {
    buttons: [
      {label: '\u2713 确认继续', action: 'y', cls: 'primary'},
      {label: '\u270e 补充修改意见', action: 'f', cls: 'feedback-btn'},
      {label: '\u2717 取消任务', action: 'n', cls: 'danger', confirmRequired: true}
    ]
  },
  doc_review_discussion: {
    buttons: [
      {label: '\u2713 确认继续', action: 'y', cls: 'primary'},
      {label: '\u270e 补充修改意见', action: 'f', cls: 'feedback-btn'},
      {label: '\ud83d\udcac 发起讨论', action: 'd', cls: ''},
      {label: '\u2717 取消任务', action: 'n', cls: 'danger', confirmRequired: true}
    ]
  },
  agent_exception: {
    buttons: [
      {label: '\ud83d\udd04 重试', action: 'y', cls: 'primary'},
      {label: '\u23f8 暂停等待', action: 'n', cls: 'warn'},
      {label: '\ud83d\uded1 终止任务', action: 'terminate', cls: 'danger', confirmRequired: true}
    ]
  },
  deploy_confirm: {
    buttons: [
      {label: '\u2713 确认部署', action: 'y', cls: 'primary'},
      {label: '\u2717 取消', action: 'n', cls: 'danger'}
    ]
  },
  generic_confirm: {
    buttons: [
      {label: '\u2713 确认', action: 'y', cls: 'primary'},
      {label: '\u2717 取消', action: 'n', cls: 'danger'},
      {label: '\u270e 补充意见', action: 'f', cls: 'feedback-btn'}
    ]
  }
};

function getButtonsForStatus(status, pendingConfirm) {
  var hasCompletedSteps = S.opusTask && S.opusTask.steps && S.opusTask.steps.some(function(s) { return s.status === 'completed'; });
  var pausedBtns = [
    {icon: '&#9654;', label: t('resume'), cls: 'ok', action: "toggleResumeInput()"},
  ];
  if (hasCompletedSteps) pausedBtns.push({icon: '&#8617;', label: t('rollbackStep'), cls: '', action: "toggleRollbackSelector()"});
  pausedBtns.push({icon: '&#9632;', label: t('terminate'), cls: 'danger', action: "opusPanelAction('terminate')"});

  var failedBtns = [
    {icon: '&#x1F504;', label: '重试', cls: 'ok', action: "toggleResumeInput()"},
    {icon: '&#9632;', label: t('terminate'), cls: 'danger', action: "opusPanelAction('terminate')"},
  ];

  var map = {
    running: [
      {icon: '&#9208;', label: t('pause'), cls: 'warn', action: "opusPanelAction('pause')"},
      {icon: '&#9632;', label: t('terminate'), cls: 'danger', action: "opusPanelAction('terminate')"},
    ],
    waiting_confirm: [],
    paused: pausedBtns,
    failed: failedBtns,
    completed: [],
    terminated: [],
    rolled_back: [],
  };
  if (status === 'waiting_confirm' && pendingConfirm) {
    var bs = (pendingConfirm.context && pendingConfirm.context.button_set) || 'generic_confirm';
    var bsCfg = BUTTON_SET_MAP[bs] || BUTTON_SET_MAP['generic_confirm'];
    if (bs !== 'generic_confirm' && !BUTTON_SET_MAP[bs]) {
      console.warn('Unknown button_set: ' + bs + ', falling back to generic_confirm');
    }
    var btns = bsCfg.buttons.map(function(btn) {
      var actionStr;
      if (btn.action === 'f') actionStr = "toggleFeedbackArea('notif')";
      else if (btn.action === 'd') actionStr = "doConfirmAction('d')";
      else if (btn.action === 'terminate') actionStr = "doTerminateFromConfirm()";
      else actionStr = "doConfirmAction('" + btn.action + "')";
      if (btn.confirmRequired && btn.action !== 'terminate') {
        actionStr = "confirmThenAction('" + btn.action + "', '" + btn.label.replace(/'/g, "\\'") + "')";
      }
      return {icon: '', label: btn.label, cls: btn.cls || '', action: actionStr};
    });
    return btns;
  }
  return map[status] || [];
}

function setControls(status, pendingConfirm) {
  var controls = document.getElementById('opus-controls');
  // v5: 操作按钮在 header 图标区 (hdr-ctrl)，opus-controls 只用于展开的输入区域
  // 旧版按钮组全部隐藏
  document.getElementById('ctrl-running').style.display = 'none';
  document.getElementById('ctrl-paused').style.display = 'none';
  document.getElementById('ctrl-done').style.display = 'none';

  if (status === 'paused' || status === 'failed') {
    controls.style.display = '';
  } else {
    controls.style.display = 'none';
    document.getElementById('panel-resume-area').style.display = 'none';
    document.getElementById('rollback-selector').style.display = 'none';
  }
  // Header icon buttons (data-driven)
  var hdrCtrl = document.getElementById('hdr-ctrl');
  if (!hdrCtrl) return;
  var btns = getButtonsForStatus(status, pendingConfirm);
  if (btns.length === 0) {
    hdrCtrl.innerHTML = '';
  } else {
    hdrCtrl.innerHTML = btns.map(function(b) {
      return '<button class="' + (b.cls || '') + '" title="' + b.label + '" onclick="' + b.action + '">'
        + b.icon + ' ' + b.label + '</button>';
    }).join('');
  }
  // 确认状态：展示确认通知区；非确认状态：清除残留
  if (status === 'waiting_confirm' && pendingConfirm) {
    showConfirmNotification(pendingConfirm);
  } else {
    clearConfirmNotification();
  }
}

function toggleResumeInput() {
  var area = document.getElementById('panel-resume-area');
  if (area.style.display === 'none') {
    area.style.display = '';
    document.getElementById('resume-feedback').placeholder = t('resumeFeedbackPlaceholder');
    document.querySelector('.panel-feedback-hint').textContent = t('resumeHint');
  } else {
    area.style.display = 'none';
  }
}

async function doResume() {
  if (!S.opusTask) return;
  var feedback = document.getElementById('resume-feedback').value.trim();
  try {
    var body = feedback ? {feedback: feedback} : {};
    var resp = await fetch('/vizo/api/tasks/' + S.opusTask.task_id + '/resume', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    var data = await resp.json();
    if (data.status === 'ok') {
      showToast(data.message, 'success');
      document.getElementById('panel-resume-area').style.display = 'none';
      setTimeout(pollOpusTask, 1500);
    } else {
      showToast(data.message || t('operationFailed'), 'error');
    }
  } catch(e) {
    showToast(t('networkError'), 'error');
  }
}

function toggleRollbackSelector() {
  var sel = document.getElementById('rollback-selector');
  if (sel.style.display !== 'none') { sel.style.display = 'none'; return; }
  sel.style.display = '';
  var steps = (S.opusTask && S.opusTask.steps || []).filter(function(s) { return s.status === 'completed'; });
  var stepsEl = document.getElementById('rollback-steps');
  stepsEl.innerHTML = steps.map(function(s, i) {
    return '<label class="rollback-step-option">' +
      '<input type="radio" name="rollback-target" value="' + escapeHtml(s.name) + '" ' + (i===steps.length-1?'checked':'') + '>' +
      '<span>' + escapeHtml(ROLE_DISPLAY_MAP[s.role] || s.name) + '</span>' +
      (s.output_doc ? '<span class="rollback-step-doc">&mdash; ' + escapeHtml(s.output_doc) + '</span>' : '') +
    '</label>';
  }).join('');
  document.getElementById('rollback-feedback').placeholder = t('rollbackFeedbackPlaceholder');
}

function closeRollbackSelector() {
  document.getElementById('rollback-selector').style.display = 'none';
}

async function doRollback() {
  if (!S.opusTask) return;
  var selected = document.querySelector('input[name="rollback-target"]:checked');
  if (!selected) { showToast(t('selectStep'), 'warning'); return; }
  var targetStep = selected.value;
  var feedback = document.getElementById('rollback-feedback').value.trim();
  try {
    var resp = await fetch('/vizo/api/tasks/' + S.opusTask.task_id + '/rollback', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({target_step: targetStep, feedback: feedback})
    });
    var data = await resp.json();
    if (data.status === 'ok') {
      showToast(data.message, 'success');
      closeRollbackSelector();
      setTimeout(pollOpusTask, 1500);
    } else {
      showToast(data.message || t('operationFailed'), 'error');
    }
  } catch(e) {
    showToast(t('networkError'), 'error');
  }
}

function viewTaskReport() {
  if (S.opusTask) window.open('/vizo/tasks/' + S.opusTask.task_id, '_blank');
}

// ======================== Notification System ========================
var MAX_NOTIFICATIONS = 3;
var _recentNotifs = [];

function showNotification(type, data) {
  // 5秒窗口去重
  var sig = type + '|' + (data.role || '') + '|' + (data.updated_at || '');
  var now = Date.now();
  _recentNotifs = _recentNotifs.filter(function(n) { return now - n.ts < 5000; });
  if (_recentNotifs.some(function(n) { return n.sig === sig; })) return;
  _recentNotifs.push({sig: sig, ts: now});

  var stack = document.getElementById('notification-stack');
  var bar = document.createElement('div');
  bar.className = 'notif-bar';

  var role = data.role || '';
  var roleDisplay = ROLE_DISPLAY_MAP[role] || role;
  var completedSteps = (data.steps || []).filter(function(s) { return s.status === 'completed'; }).length;
  var stepProgress = completedSteps + '/' + (data.steps || []).length;

  switch(type) {
    case 'agent_start':
      bar.classList.add('green');
      bar.innerHTML = buildNotifHTML(
        roleDisplay + ' ' + t('agentStarted'),
        t('step') + ' ' + stepProgress + ' | ' + t('cost') + ': $' + (data.cost_usd||0).toFixed(2),
        [{text: t('openPanel'), action: 'panel'}]
      );
      autoRemove(bar, 5000);
      break;
    case 'agent_complete':
      bar.classList.add('blue');
      var lastStep = (data.steps||[]).filter(function(s){return s.status==='completed'}).pop();
      var doc = lastStep ? lastStep.output_doc : '';
      var dur = lastStep ? formatDuration(lastStep.duration) : '';
      var cst = lastStep ? '$'+lastStep.cost_usd.toFixed(2) : '';
      var previewUrl = lastStep ? (lastStep.preview_url || '') : '';
      var agentDocLink = '';
      if (previewUrl && doc) {
        agentDocLink = '<div class="notif-meta"><a class="notif-doc-link" href="' + escapeHtml(previewUrl) + '" target="_blank">&#128196; ' + escapeHtml(doc) + '</a></div>';
      }
      bar.innerHTML = buildNotifHTML(
        roleDisplay + ' ' + t('agentCompleted'),
        dur + ' | ' + cst,
        [{text: t('viewDetails'), action: 'panel'}],
        false,
        agentDocLink
      );
      autoRemove(bar, 5000);
      break;
    case 'agent_error':
      bar.classList.add('red');
      var errStep = (data.steps||[]).filter(function(s){return s.status==='error'}).pop();
      var errMsg = (errStep && errStep.error) ? errStep.error.substring(0,80) : t('unknownError');
      bar.innerHTML = buildNotifHTML(
        roleDisplay + ' ' + t('agentFailed'),
        errMsg + ' — ' + t('autopaused'),
        [{text: t('viewDetails'), action: 'panel'}],
        true
      );
      break;
    case 'pause':
      bar.classList.add('yellow');
      bar.innerHTML = buildNotifHTML(
        t('taskPaused'),
        t('step') + ' ' + stepProgress + ' | ' + t('cost') + ': $' + (data.cost_usd||0).toFixed(2),
        [{text: t('openPanel'), action: 'panel'}],
        true
      );
      break;
    case 'terminate':
      bar.classList.add('orange');
      bar.innerHTML = buildNotifHTML(
        t('taskTerminated'),
        t('codeRolledBack') + ' | ' + t('cost') + ': $' + (data.cost_usd||0).toFixed(2),
        [], true
      );
      break;
    case 'complete':
      bar.classList.add('blue');
      var totalSteps = (data.steps||[]).length;
      var totalDur = formatDuration((data.steps||[]).reduce(function(s,st) { return s + (st.duration||0); }, 0));
      var totalTokens = (data.steps||[]).reduce(function(s,st) { return s + (st.tokens||0); }, 0);
      bar.innerHTML = buildNotifHTML(
        t('taskCompleted'),
        t('steps') + ': ' + totalSteps + ' | ' + totalDur + ' | $' + (data.cost_usd||0).toFixed(2) + ' | ' + totalTokens.toLocaleString() + ' tokens',
        [{text: t('viewFull'), action: 'panel'}],
        true
      );
      break;
  }
  // 确认通知条始终置底：column-reverse 中 DOM 首子元素在视觉底部，confirmBar 用 prepend 置底
  // 新通知插到 confirmBar 之后（DOM 更高索引 = 视觉更高位置），确保 confirmBar 始终在最底部
  var confirmBar = stack.querySelector('.confirm-bar');
  if (confirmBar) {
    confirmBar.after(bar);
  } else {
    stack.prepend(bar);
  }
  enforceMaxNotifications();
  // 面板关闭时显示红点
  if (!S.panelOpen) {
    var dot = document.getElementById('panel-red-dot');
    if (dot) dot.style.display = '';
  }
}

function buildNotifHTML(title, meta, actions, manualClose, docLink) {
  var html = '<div class="notif-header"><span class="notif-title">' + escapeHtml(title) + '</span></div>';
  if (meta) html += '<div class="notif-meta">' + escapeHtml(meta) + '</div>';
  if (docLink) html += docLink;
  if (actions.length || manualClose) {
    html += '<div class="notif-actions">';
    actions.forEach(function(a) {
      html += '<button class="banner-btn primary" onclick="notifAction(\'' + a.action + '\',this)">' + escapeHtml(a.text) + ' &rarr;</button>';
    });
    if (manualClose) {
      html += '<button class="notif-dismiss-btn" onclick="this.parentElement.parentElement.remove()">隐藏</button>';
    }
    html += '</div>';
  }
  if (manualClose) {
    html += '<button class="notif-close" onclick="this.parentElement.remove()">&times;</button>';
  }
  return html;
}

function autoRemove(bar, ms) {
  setTimeout(function() { if (bar.parentElement) bar.remove(); }, ms);
}

function enforceMaxNotifications() {
  var stack = document.getElementById('notification-stack');
  var bars = stack.querySelectorAll('.notif-bar:not(.confirm-bar)');
  while (bars.length > MAX_NOTIFICATIONS) {
    bars[bars.length - 1].remove();
    bars = stack.querySelectorAll('.notif-bar:not(.confirm-bar)');
  }
}

function notifAction(action, btn) {
  if (action === 'panel') {
    if (!S.panelOpen) togglePanel();
    btn.closest('.notif-bar').remove();
  }
}

// ======================== Confirm Notification ========================
function renderConfirmActions(confirm, source) {
  // source = 'notif' | 'panel'
  var suffix = source;
  var ctx = confirm.context || {};
  var bs = ctx.button_set || 'generic_confirm';
  var bsCfg = BUTTON_SET_MAP[bs] || BUTTON_SET_MAP['generic_confirm'];
  if (!BUTTON_SET_MAP[bs]) {
    console.warn('Unknown button_set: ' + bs + ', falling back to generic_confirm');
  }

  var btnsHTML = bsCfg.buttons.map(function(btn) {
    var onclickStr;
    if (btn.action === 'f') {
      onclickStr = "toggleFeedbackArea('" + suffix + "')";
    } else if (btn.action === 'terminate') {
      onclickStr = "doTerminateFromConfirm()";
    } else if (btn.confirmRequired) {
      onclickStr = "confirmThenAction('" + btn.action + "', '" + btn.label.replace(/'/g, "\\'") + "')";
    } else {
      onclickStr = "doConfirmAction('" + btn.action + "')";
    }
    var cls = 'banner-btn ' + (btn.cls || '');
    return '<button class="' + cls + '" onclick="' + onclickStr + '">' + btn.label + '</button>';
  }).join('');

  var hasFeedback = bsCfg.buttons.some(function(btn) { return btn.action === 'f'; });
  var feedbackHTML = hasFeedback
    ? '<div class="feedback-area" id="confirm-feedback-area-' + suffix + '">' +
        '<textarea id="confirm-feedback-text-' + suffix + '" placeholder="' + t('feedbackPlaceholder') + '"></textarea>' +
        '<button class="feedback-submit yellow" onclick="submitFeedbackFrom(\'' + suffix + '\')">' + t('submitFeedback') + '</button>' +
      '</div>'
    : '';

  return '<div class="notif-actions" id="confirm-actions-' + suffix + '">' + btnsHTML + '</div>' + feedbackHTML;
}

function showConfirmNotification(confirm) {
  // 输入状态保护：如果是同一个确认请求且用户正在输入，跳过重渲染
  if (shouldSkipConfirmRender(confirm)) return;

  document.querySelectorAll('.confirm-bar').forEach(function(el) { el.remove(); });
  var stack = document.getElementById('notification-stack');
  var bar = document.createElement('div');
  bar.className = 'notif-bar confirm-bar';
  bar.dataset.requestId = confirm.request_id;
  bar.dataset.createdAt = Date.now();

  var ctx = confirm.context || {};
  var contextParts = [
    ctx.role_display, ctx.model, ctx.duration,
    ctx.cost_usd ? '$' + ctx.cost_usd.toFixed(2) : '',
    ctx.step_progress ? (t('step') + ' ' + ctx.step_progress) : ''
  ].filter(Boolean);
  var contextStr = contextParts.join(' | ');

  var docLink = '';
  var previewUrl = confirm.preview_url;
  // fallback: pending_confirm 可能没有 preview_url（WebSocket 推送时尚未生成），从最新有产物的步骤取。
  if (!previewUrl && S.opusTask && S.opusTask.steps) {
    for (var si = S.opusTask.steps.length - 1; si >= 0; si--) {
      var step = S.opusTask.steps[si];
      var stepDoc = step.output_doc || '';
      if (!stepDoc) continue;
      previewUrl = step.output_url || step.preview_url || (
        S.opusTask.task_id
          ? '/vizo/api/tasks/' + encodeURIComponent(S.opusTask.task_id) + '/outputs/' + encodeURIComponent(stepDoc)
          : ''
      );
      if (previewUrl) {
        break;
      }
    }
  }
  if (previewUrl) {
    docLink = '<a class="notif-doc-link" href="' + escapeHtml(previewUrl) + '" target="_blank">&#128196; ' + t('clickToPreview') + '</a>';
  }

  bar.innerHTML =
    '<div class="notif-header">' +
      '<span class="notif-title">' + escapeHtml(confirm.title || t('confirmRequired')) + '</span>' +
      '<span class="timeout-badge" id="confirm-timeout" style="display:none"></span>' +
    '</div>' +
    (confirm.summary ? '<div class="notif-summary">' + escapeHtml(confirm.summary) + '</div>' : '') +
    docLink +
    (contextStr ? '<div class="notif-context">' + escapeHtml(contextStr) + '</div>' : '') +
    renderConfirmActions(confirm, 'notif');

  stack.prepend(bar);

  // 同步渲染直播面板中的确认区域
  renderPanelConfirmArea(confirm);
  // 面板关闭时显示红点
  if (!S.panelOpen) {
    var dot = document.getElementById('panel-red-dot');
    if (dot) dot.style.display = '';
  }

  S._confirmTimer = setInterval(function() {
    var elapsed = Math.floor((Date.now() - parseInt(bar.dataset.createdAt)) / 60000);
    if (elapsed >= 3) {
      bar.classList.add('timeout');
      var badge = document.getElementById('confirm-timeout');
      if (badge) {
        badge.style.display = 'inline';
        badge.textContent = t('waitedMinutes', {n: elapsed});
      }
    }
  }, 60000);
}

function toggleFeedbackArea(suffix) {
  // suffix = 'notif' | 'panel'
  var area = document.getElementById('confirm-feedback-area-' + suffix);
  var text = document.getElementById('confirm-feedback-text-' + suffix);
  if (!area || !text) return;
  if (area.classList.contains('open')) {
    area.classList.remove('open'); return;
  }
  area.classList.add('open');
  text.placeholder = t('feedbackPlaceholder');
  text.focus();
}

async function doConfirmAction(action) {
  if (!S.opusTask) return;
  var taskId = S.opusTask.task_id;
  disableAllConfirmButtons(true);
  try {
    var resp = await fetch('/vizo/api/tasks/' + taskId + '/confirm', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: action})
    });
    var data = await resp.json();
    if (data.status === 'ok') {
      markConfirmResolvedLocally();
      var msgMap = {y: t('confirmed'), n: t('cancelled'), d: '已发起讨论'};
      showToast(msgMap[action] || data.message, 'success');
    } else {
      showToast(data.message || t('operationFailed'), 'error');
      disableAllConfirmButtons(false);
    }
  } catch(e) {
    showToast(t('networkError'), 'error');
    disableAllConfirmButtons(false);
  }
}

function disableAllConfirmButtons(disabled) {
  ['notif', 'panel'].forEach(function(suffix) {
    var actions = document.getElementById('confirm-actions-' + suffix);
    if (actions) {
      actions.querySelectorAll('.banner-btn').forEach(function(b) { b.disabled = disabled; });
    }
  });
}

async function doTerminateFromConfirm() {
  if (!S.opusTask) return;
  if (!window.confirm('确定要终止当前任务吗？此操作不可撤回。')) return;
  var taskId = S.opusTask.task_id;
  disableAllConfirmButtons(true);
  try {
    var resp = await fetch('/vizo/api/tasks/' + taskId + '/confirm', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: 'terminate'})
    });
    var data = await resp.json();
    if (data.status === 'ok') {
      markConfirmResolvedLocally();
      showToast(data.message || '任务已终止', 'success');
    } else {
      showToast(data.message || t('operationFailed'), 'error');
      disableAllConfirmButtons(false);
    }
  } catch(e) {
    showToast(t('networkError'), 'error');
    disableAllConfirmButtons(false);
  }
}

function confirmThenAction(action, label) {
  if (!window.confirm('确定要执行「' + label + '」吗？')) return;
  doConfirmAction(action);
}

async function submitFeedbackFrom(suffix) {
  if (!S.opusTask) return;
  var taskId = S.opusTask.task_id;
  var textEl = document.getElementById('confirm-feedback-text-' + suffix);
  if (!textEl) return;
  var text = textEl.value.trim();
  if (!text) return;
  var submitBtn = textEl.parentElement.querySelector('.feedback-submit');
  if (submitBtn) submitBtn.disabled = true;
  try {
    var resp = await fetch('/vizo/api/tasks/' + taskId + '/confirm', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: 'f', feedback: text})
    });
    var data = await resp.json();
    if (data.status === 'ok') {
      markConfirmResolvedLocally();
      showToast(t('feedbackSent'), 'success');
    } else {
      showToast(data.message || t('operationFailed'), 'error');
      if (submitBtn) submitBtn.disabled = false;
    }
  } catch(e) {
    showToast(t('networkError'), 'error');
    if (submitBtn) submitBtn.disabled = false;
  }
}

// 输入状态保护：检查是否应跳过确认区域重渲染
function shouldSkipConfirmRender(newConfirm) {
  var existingBar = document.querySelector('.confirm-bar');
  if (!existingBar) return false;
  if (existingBar.dataset.requestId !== newConfirm.request_id) return false;
  // 检查通知条和面板的输入框状态
  var suffixes = ['notif', 'panel'];
  for (var i = 0; i < suffixes.length; i++) {
    var feedbackArea = document.getElementById('confirm-feedback-area-' + suffixes[i]);
    var textArea = document.getElementById('confirm-feedback-text-' + suffixes[i]);
    if (feedbackArea && textArea && feedbackArea.classList.contains('open') &&
        (textArea.value.trim() || document.activeElement === textArea)) {
      return true;
    }
  }
  return false;
}

// 直播面板中渲染确认区域
function renderPanelConfirmArea(confirm) {
  var container = document.getElementById('panel-confirm-area');
  if (!container) return;
  container.innerHTML = renderConfirmActions(confirm, 'panel');
  container.style.display = '';
}

function clearPanelConfirmArea() {
  var container = document.getElementById('panel-confirm-area');
  if (container) { container.innerHTML = ''; container.style.display = 'none'; }
}

function clearConfirmNotification() {
  document.querySelectorAll('.confirm-bar').forEach(function(el) { el.remove(); });
  clearPanelConfirmArea();
  if (S._confirmTimer) {
    clearInterval(S._confirmTimer);
    S._confirmTimer = null;
  }
}

function markConfirmResolvedLocally() {
  clearConfirmNotification();
  if (S.opusTask) {
    delete S.opusTask.pending_confirm;
    if (S.opusTask.status === 'waiting_confirm') {
      S.opusTask.status = 'running';
    }
    setControls(S.opusTask.status || 'running', null);
  }
  pollOpusTask();
}

// ======================== Vizo Live Panel ========================
var ROLE_DISPLAY_MAP = {
  requirement_analyst: '需求分析师', product_manager: '产品经理',
  project_manager: '项目经理', architect: '架构师',
  backend_developer: '后端工程师', frontend_developer: '前端工程师',
  embedded_engineer: '嵌入式工程师', fix_engineer: '修复工程师',
  qa_engineer: '测试工程师', integration_engineer: '集成工程师',
  devops_engineer: '运维工程师', technical_assessor: '技术评估师',
  code_explorer: '代码分析师', assistant: '助手',
  knowledge_engineer: '知识工程师', knowledge_admin: '知识管理员',
  handoff_extractor: '交接提取', manual_updater: '文档更新',
  merge_resolver: '合并处理',
  developer_backend: '后端工程师', developer_frontend: '前端工程师',
  devops: '运维工程师'
};

// ======================== Workflow Builder ========================

var PIPELINE_TEMPLATES = {
  new_feature: [
    {step: 'requirement_analysis', role: 'requirement_analyst'},
    {step: 'pm_prd', role: 'product_manager'},
    {step: 'architect', role: 'architect'},
    {step: 'qa_test_cases', role: 'qa_engineer'},
    {step: 'backend_dev', role: 'backend_developer'},
    {step: 'frontend_dev', role: 'frontend_developer'},
    {step: 'integration', role: 'integration_engineer'},
    {step: 'test_round_1', role: 'qa_engineer'},
    {step: 'deploy', role: 'devops_engineer'},
    {step: 'knowledge', role: 'knowledge_engineer'},
  ],
  bug_fix: [
    {step: 'requirement_analysis', role: 'requirement_analyst'},
    {step: 'architect', role: 'architect'},
    {step: 'fix', role: 'fix_engineer'},
    {step: 'qa_engineer', role: 'qa_engineer'},
    {step: 'test_round_1', role: 'qa_engineer'},
    {step: 'deploy', role: 'devops_engineer'},
    {step: 'knowledge', role: 'knowledge_engineer'},
  ],
  refactor: [
    {step: 'requirement_analysis', role: 'requirement_analyst'},
    {step: 'architect', role: 'architect'},
    {step: 'refactor', role: 'backend_developer'},
    {step: 'qa_engineer', role: 'qa_engineer'},
    {step: 'test_round_1', role: 'qa_engineer'},
    {step: 'deploy', role: 'devops_engineer'},
    {step: 'knowledge', role: 'knowledge_engineer'},
  ],
  debug_embedded: [
    {step: 'requirement_analysis', role: 'requirement_analyst'},
    {step: 'embedded', role: 'embedded_engineer'},
    {step: 'knowledge', role: 'knowledge_engineer'},
  ],
  non_dev: [
    {step: 'assistant', role: 'assistant'},
  ],
};

// 步骤名称映射：同角色多步骤时区分显示
var STEP_DISPLAY_MAP = {
  requirement_analysis: '需求分析',
  pm_prd: '产品PRD',
  architect: '架构设计',
  design_confirmed: '设计确认',
  qa_test_cases: '编写测试用例',
  backend_dev: '后端开发',
  frontend_dev: '前端开发',
  integration: '集成联调',
  fix: '问题修复',
  refactor: '重构实现',
  qa_engineer: '编写测试',
  test_round_1: '第1轮测试',
  test_round_2: '第2轮测试',
  test_round_3: '第3轮测试',
  fix_round_1: '第1轮修复',
  fix_round_2: '第2轮修复',
  fix_round_3: '第3轮修复',
  deploy: '部署上线',
  knowledge: '知识沉淀',
  embedded: '嵌入式开发',
  assistant: '助手执行',
};

// ======================== 任务展示层优化：状态映射 ========================
var STATUS_MAPPING = {
  pending: {
    text: t('status_pending'),    // "待开始"
    icon: '⏳',
    color: 'gray',
    cssClass: 'pending'
  },
  running: {
    text: t('status_running'),    // "运行中"
    icon: '🔄',
    color: 'blue',
    cssClass: 'running'
  },
  paused: {
    text: t('status_paused'),     // "已暂停"
    icon: '⏸️',
    color: 'yellow',
    cssClass: 'paused'
  },
  completed: {
    text: t('status_completed'),  // "已完成"
    icon: '✅',
    color: 'green',
    cssClass: 'completed'
  },
  failed: {
    text: t('status_error'),      // "已失败"
    icon: '❌',
    color: 'red',
    cssClass: 'failed'
  },
  rolled_back: {
    text: t('status_rolled_back'), // "已回退"
    icon: '↩️',
    color: 'purple',
    cssClass: 'rolled_back'
  },
  terminated: {
    text: t('status_terminated', '已终止'),
    icon: '⛔',
    color: 'red',
    cssClass: 'failed'
  },
  waiting_confirm: {
    text: t('status_waiting_confirm', '待确认'),
    icon: '🔔',
    color: 'yellow',
    cssClass: 'paused'
  },
  partially_failed: {
    text: t('status_partially_failed', '部分失败'),
    icon: '⚠️',
    color: 'yellow',
    cssClass: 'partially-failed'
  }
};

// 获取状态显示信息
function getStatusDisplay(status) {
  return STATUS_MAPPING[status] || {
    text: t('status_unknown'),
    icon: '?',
    color: 'gray',
    cssClass: 'unknown'
  };
}

// ======================== 任务展示层优化：任务类型映射 ========================
var TASK_TYPE_DISPLAY = {
  new_feature: {
    text: '全新功能',
    steps: PIPELINE_TEMPLATES.new_feature.length,
    workflow: PIPELINE_TEMPLATES.new_feature
  },
  bug_fix: {
    text: '缺陷修复',
    steps: PIPELINE_TEMPLATES.bug_fix.length,
    workflow: PIPELINE_TEMPLATES.bug_fix
  },
  refactor: {
    text: '重构优化',
    steps: PIPELINE_TEMPLATES.refactor.length,
    workflow: PIPELINE_TEMPLATES.refactor
  },
  debug_embedded: {
    text: '嵌入式调试',
    steps: PIPELINE_TEMPLATES.debug_embedded.length,
    workflow: PIPELINE_TEMPLATES.debug_embedded
  },
  non_dev: {
    text: '非开发任务',
    steps: 1,
    workflow: PIPELINE_TEMPLATES.non_dev
  }
};

// 获取任务类型显示信息
function getTaskTypeDisplay(taskType) {
  if (!taskType) return null;
  var display = TASK_TYPE_DISPLAY[taskType];
  if (display) {
    return {
      text: display.text,
      type: taskType,
      steps: display.steps
    };
  }
  // 非法值
  return {
    text: '类型错误',
    type: taskType,
    error: true
  };
}

// ======================== 任务展示层优化：辅助函数 ========================
// 格式化任务名称（优先 task_name，降级 description）
function formatTaskName(task, maxLength) {
  if (!task) return '(未命名任务)';

  // 优先使用 task_name
  var name = task.task_name || '';

  // task_name 为空或提取失败时使用 description
  if (!name.trim()) {
    name = task.description || '(未命名任务)';
  }

  // 截断超长名称
  if (maxLength && name.length > maxLength) {
    return name.substring(0, maxLength) + '...';
  }

  return name;
}

// 完整描述（用于悬浮提示）
function getFullDescription(task) {
  return task.description || task.task_name || '';
}

// 从 task_id 提取创建时间戳
function extractCreateTimeFromTaskId(taskId) {
  if (!taskId || taskId.length < 15) return 0;
  var datePart = taskId.substring(0, 4) + '-' +
                 taskId.substring(4, 6) + '-' +
                 taskId.substring(6, 8) + 'T' +
                 taskId.substring(9, 11) + ':' +
                 taskId.substring(11, 13) + ':' +
                 taskId.substring(13, 15);
  return new Date(datePart).getTime();
}

// 复合排序：进行中优先 + 按创建时间倒序
function sortTasks(tasks) {
  return tasks.sort(function(a, b) {
    // 进行中状态集合
    var runningStatus = ['running', 'paused'];

    // 第一梯队：进行中任务
    var aIsRunning = runningStatus.includes(a.status);
    var bIsRunning = runningStatus.includes(b.status);

    if (aIsRunning && !bIsRunning) return -1;  // a 在前
    if (!aIsRunning && bIsRunning) return 1;   // b 在前

    // 第二梯队：按创建时间倒序
    var aTime = extractCreateTimeFromTaskId(a.task_id);
    var bTime = extractCreateTimeFromTaskId(b.task_id);

    // 降序：新任务在前
    return bTime - aTime;
  });
}

// 构建完整工作流（含待执行步骤）
function buildFullWorkflow(task) {
  if (!task) return [];

  var taskType = task.task_type;
  var completedSteps = task.completed_steps || [];
  var currentStep = task.current_step || '';

  // 有子任务的大任务：模板不适用，用动态构建（buildWorkflow 已处理 sub-task: 角色）
  if (task.sub_tasks && task.sub_tasks.length > 0) {
    return buildWorkflow(task);
  }

  // 无 task_type 时：只渲染已执行步骤
  if (!taskType) {
    return buildWorkflow(task);  // 复用现有逻辑
  }

  // non_dev 特殊处理
  if (taskType === 'non_dev') {
    return [{
      kind: 'notice',
      text: '该任务为单步骤执行，无工作流步骤'
    }];
  }

  // 获取工作流模板
  var template = TASK_TYPE_DISPLAY[taskType]?.workflow;
  if (!template) {
    // 非法 task_type：降级处理
    return buildWorkflow(task);
  }

  // 按 step name 汇总实际步骤数据（同名多条时：累加 cost/duration/tokens，取最新成功条目的 model/doc/preview）
  var actualSteps = task.steps || [];
  var stepAgg = {};  // { stepName: { cost_usd, duration, tokens, latest: <last entry>, latestOk: <last completed entry> } }
  actualSteps.forEach(function(s) {
    var key = s.name || '';
    if (!stepAgg[key]) {
      stepAgg[key] = { cost_usd: 0, duration: 0, tokens: 0, latest: null, latestOk: null };
    }
    var agg = stepAgg[key];
    agg.cost_usd += (s.cost_usd || 0);
    agg.duration += (s.duration || 0);
    agg.tokens += (s.tokens || 0);
    agg.latest = s;
    if (s.status === 'completed' || s.status === 'running') {
      agg.latestOk = s;
    }
  });

  // 构建完整步骤列表
  var result = [];
  var lastKind = null;

  template.forEach(function(tmpl, idx) {
    var stepName = tmpl.step;
    var isCompleted = completedSteps.includes(stepName);
    var isCurrent = stepName === currentStep;
    var isSubTask = stepName.startsWith('sub-task:');

    var kind = isSubTask ? 'subtask' : 'step';

    // 分隔符处理（与现有逻辑一致）
    if (lastKind === 'step' && kind === 'subtask') {
      result.push({ kind: 'separator', label: '▼ 子任务执行阶段' });
    } else if (lastKind === 'subtask' && kind === 'step') {
      result.push({ kind: 'separator', label: '▲ 子任务结束' });
    }

    // 从汇总数据匹配
    var agg = stepAgg[stepName];
    var best = agg ? (agg.latestOk || agg.latest) : null;  // 优先取成功条目

    // 确定步骤状态：有实际数据时用最新条目状态
    var status;
    if (agg && agg.latest) {
      status = agg.latest.status;
      // 如果最新是 error 但步骤在 completed_steps 里，说明后续重试成功了
      if (status === 'error' && isCompleted) status = 'completed';
    } else if (isCompleted) {
      status = 'completed';
    } else if (isCurrent) {
      status = 'running';
    } else {
      status = 'pending';
    }

    // 构建步骤对象，合并汇总数据
    var stepObj = {
      kind: kind,
      role: best ? (best.role || tmpl.role) : tmpl.role,
      name: stepName,
      status: status,
      order: idx + 1,
      cost_usd: agg ? agg.cost_usd : 0,
      billable: best ? (best.billable !== undefined ? best.billable : true) : true,
      duration: agg ? agg.duration : 0,
      started_at: best ? (best.started_at || '') : '',
      model: best ? (best.model || '') : '',
      tokens: agg ? agg.tokens : 0,
      output_doc: best ? (best.output_doc || '') : '',
      preview_url: best ? (best.preview_url || '') : '',
      output_url: best ? (best.output_url || '') : '',
      error: (agg && agg.latest && agg.latest.error) ? agg.latest.error : '',
    };

    // 子任务特殊处理
    if (isSubTask) {
      stepObj.subName = stepName.substring('sub-task:'.length);
    }

    result.push(stepObj);
    lastKind = kind;
  });

  // 动态插入 test_round_N / fix_round_N 轮次（模板中没有但 progress.json 中出现的）
  var templateStepNames = new Set(template.map(function(t) { return t.step; }));
  var dynamicRounds = [];
  Object.keys(stepAgg).forEach(function(name) {
    if (!templateStepNames.has(name) && /^(test_round_|fix_round_)\d+$/.test(name)) {
      dynamicRounds.push(name);
    }
  });

  // 按轮次排序：fix_round_1, test_round_2, fix_round_2, test_round_3, ...
  dynamicRounds.sort(function(a, b) {
    var parseRound = function(s) {
      var m = s.match(/(\d+)$/);
      var num = m ? parseInt(m[1]) : 0;
      return s.startsWith('test_') ? num * 10 : num * 10 - 5;
    };
    return parseRound(a) - parseRound(b);
  });

  if (dynamicRounds.length > 0) {
    var deployIdx = result.findIndex(function(r) { return r.name === 'deploy'; });
    if (deployIdx < 0) deployIdx = result.length;

    var insertItems = dynamicRounds.map(function(name, i) {
      var agg = stepAgg[name];
      var best = agg ? (agg.latestOk || agg.latest) : null;
      var isCompleted = completedSteps.includes(name);
      var isCurrent = name === currentStep;
      var status = agg && agg.latest ? agg.latest.status : (isCompleted ? 'completed' : isCurrent ? 'running' : 'pending');
      if (status === 'error' && isCompleted) status = 'completed';
      var role = best ? best.role : (name.startsWith('test_') ? 'qa_engineer' : 'fix_engineer');
      return {
        kind: 'step', role: role, name: name,
        status: status, order: deployIdx + i,
        cost_usd: agg ? agg.cost_usd : 0,
        billable: true,
        duration: agg ? agg.duration : 0,
        started_at: best ? (best.started_at || '') : '',
        model: best ? (best.model || '') : '',
        tokens: agg ? agg.tokens : 0,
        output_doc: best ? (best.output_doc || '') : '',
        preview_url: best ? (best.preview_url || '') : '',
        output_url: best ? (best.output_url || '') : '',
        error: (agg && agg.latest && agg.latest.error) ? agg.latest.error : '',
      };
    });

    // splice 插入到 deploy 之前
    result.splice.apply(result, [deployIdx, 0].concat(insertItems));
  }

  return result;
}

// ======================== 工作流流程图弹窗 ========================
// 显示工作流流程图弹窗
function showWorkflowDiagram(taskType) {
  if (!taskType) return;
  var template = PIPELINE_TEMPLATES[taskType];
  if (!template) return;

  // 构建流程图 HTML（垂直流程图，箭头连接各步骤）
  var stepsHTML = '';
  template.forEach(function(step, idx) {
    var roleDisplay = STEP_DISPLAY_MAP[step.step] || ROLE_DISPLAY_MAP[step.role] || step.role;
    var roleInfo = ROLE_ICONS[step.role] || ROLE_ICONS.default;

    stepsHTML += '<div class="wf-node">' +
      '<div class="wf-icon" style="background:' + roleInfo.bg + ';color:' + roleInfo.color + '">' +
        roleInfo.icon +
      '</div>' +
      '<div class="wf-info">' +
        '<div class="wf-role">' + escapeHtml(roleDisplay) + '</div>' +
        '<div class="wf-desc">' + escapeHtml(step.step || '') + '</div>' +
      '</div>' +
    '</div>';

    // 添加箭头分隔符（最后一个步骤后不加）
    if (idx < template.length - 1) {
      stepsHTML += '<div class="wf-arrow">↓</div>';
    }
  });

  // 获取任务类型显示名称
  var typeDisplay = getTaskTypeDisplay(taskType);
  var title = typeDisplay ? typeDisplay.text + '（' + typeDisplay.type + '）' : taskType;

  // 创建弹窗覆盖层
  var overlay = document.createElement('div');
  overlay.className = 'wf-diagram-overlay';
  overlay.onclick = function(e) {
    // 点击覆盖层外部关闭弹窗
    if (e.target === overlay) {
      overlay.remove();
      document.body.style.overflow = '';
    }
  };

  // 弹窗内容
  overlay.innerHTML =
    '<div class="wf-diagram-modal">' +
      '<div class="wf-diagram-header">' +
        '<span>工作流流程图 — ' + escapeHtml(title) + '</span>' +
        '<span class="wf-close" onclick="var el=this.closest(\'.wf-diagram-overlay\');if(el){el.remove();document.body.style.overflow=\'\';}">✕</span>' +
      '</div>' +
      '<div class="wf-diagram-body">' + stepsHTML + '</div>' +
    '</div>';

  // 添加到 body
  document.body.appendChild(overlay);

  // 阻止页面滚动
  document.body.style.overflow = 'hidden';

  // 弹窗关闭时恢复滚动
  var modal = overlay.querySelector('.wf-diagram-modal');
  if (modal) {
    modal.onmousedown = function(e) {
      e.stopPropagation();
    };
  }
}

function buildWorkflow(task) {
  if (S.workflowCache) return S.workflowCache;
  var steps = task.steps || [];
  var result = [];

  // Pipeline 尾部步骤：一定属于父任务，遇到时退出子任务区域
  var parentOnlySteps = ['deploy', 'knowledge', 'knowledge_review', 'knowledge_extractor'];

  // 遍历原始 steps，用位置跟踪子任务上下文
  // 子任务区域内的普通步骤（如 final_integration_test）归入前一个子任务
  var lastSubContext = null;  // 当前所在的子任务 role
  var subAgg = {};    // sub-task role → { cost_usd, duration, latest, latestOk }
  var stepAgg = {};   // parent step name → { cost_usd, duration, tokens, latest, latestOk }
  var subOrder = [];  // 子任务首次出现顺序
  var stepOrder = []; // 父步骤首次出现顺序（在子任务区域之前和之后）
  var subStarted = false;  // 是否已进入过子任务区域

  steps.forEach(function(step) {
    var role = step.role || '';
    var name = step.name || role || '';
    var isSub = role.indexOf('sub-task:') === 0;

    if (isSub) {
      lastSubContext = role;
      subStarted = true;
      if (!subAgg[role]) {
        subAgg[role] = { cost_usd: 0, duration: 0, latest: null, latestOk: null };
        subOrder.push(role);
      }
      var sa = subAgg[role];
      sa.cost_usd += (step.cost_usd || 0);
      sa.duration += (step.duration || 0);
      sa.latest = step;
      if (step.status === 'completed' || step.status === 'running') sa.latestOk = step;
      return;
    }

    // 子任务区域内的普通步骤
    if (lastSubContext && parentOnlySteps.indexOf(name) < 0) {
      // 归入当前子任务（累加费用）
      var sa2 = subAgg[lastSubContext];
      if (sa2) {
        sa2.cost_usd += (step.cost_usd || 0);
        sa2.duration += (step.duration || 0);
      }
      return;
    }

    // 父任务步骤（子任务区域之前或之后的 deploy/knowledge 等）
    if (parentOnlySteps.indexOf(name) >= 0) {
      lastSubContext = null;  // 退出子任务区域
    }
    if (!stepAgg[name]) {
      stepAgg[name] = { cost_usd: 0, duration: 0, tokens: 0, latest: null, latestOk: null, afterSub: subStarted };
      stepOrder.push(name);
    }
    var pa = stepAgg[name];
    pa.cost_usd += (step.cost_usd || 0);
    pa.duration += (step.duration || 0);
    pa.tokens += (step.tokens || 0);
    pa.latest = step;
    if (step.status === 'completed' || step.status === 'running') pa.latestOk = step;
  });

  // 构建输出：先父任务前置步骤，再子任务区域，再父任务后置步骤
  var lastKind = null;

  // 1. 子任务之前的父步骤
  stepOrder.forEach(function(name) {
    var pa = stepAgg[name];
    if (pa.afterSub) return;  // 子任务之后的步骤，稍后处理
    var best = pa.latestOk || pa.latest;
    result.push({
      kind: 'step', role: best.role, name: best.name,
      status: best.status, cost_usd: pa.cost_usd,
      billable: best.billable !== undefined ? best.billable : true,
      duration: pa.duration, started_at: best.started_at,
      model: best.model || '', tokens: pa.tokens,
      output_doc: best.output_doc || '', preview_url: best.preview_url || '',
      output_url: best.output_url || '',
      error: (pa.latest && pa.latest.error) ? pa.latest.error : '',
      description: best.description || '',
    });
    lastKind = 'step';
  });

  // 2. 子任务区域
  if (subOrder.length > 0) {
    var subDone = subOrder.filter(function(r) { var a = subAgg[r]; return a.latestOk && a.latestOk.status === 'completed'; }).length;
    var totalSubs = (task.sub_tasks && task.sub_tasks.length > 0) ? task.sub_tasks.length : subOrder.length;
    result.push({ kind: 'separator', label: '\u25bc \u5b50\u4efb\u52a1\u6267\u884c\u9636\u6bb5 ' + subDone + '/' + totalSubs });

    subOrder.forEach(function(role) {
      var sa = subAgg[role];
      var best = sa.latestOk || sa.latest;
      var subName = role.substring('sub-task:'.length);
      result.push({
        kind: 'subtask', role: best.role, name: subName, subName: subName,
        status: best.status, cost_usd: sa.cost_usd, duration: sa.duration,
        started_at: best.started_at, model: best.model || '',
      });
    });

    // 补充 pending 子任务
    if (task.sub_tasks && task.sub_tasks.length > 0) {
      var existingSubNames = new Set();
      subOrder.forEach(function(r) { existingSubNames.add(r.substring('sub-task:'.length)); });
      task.sub_tasks.forEach(function(st) {
        if (!existingSubNames.has(st.name)) {
          result.push({
            kind: 'subtask', role: 'sub-task:' + st.name, name: st.name,
            subName: st.name, status: 'pending', cost_usd: 0, duration: 0,
            started_at: '', model: '', error: '',
          });
        }
      });
      // 更新分隔符计数
      var allSubItems = result.filter(function(r) { return r.kind === 'subtask'; });
      var doneSubItems = allSubItems.filter(function(r) { return r.status === 'completed'; });
      result.forEach(function(r) {
        if (r.kind === 'separator' && r.label.indexOf('\u5b50\u4efb\u52a1\u6267\u884c\u9636\u6bb5') >= 0) {
          r.label = '\u25bc \u5b50\u4efb\u52a1\u6267\u884c\u9636\u6bb5 ' + doneSubItems.length + '/' + allSubItems.length;
        }
      });
    }

    result.push({ kind: 'separator', label: '\u25b2 \u5b50\u4efb\u52a1\u7ed3\u675f' });
    lastKind = 'subtask';
  }

  // 3. 子任务之后的父步骤
  stepOrder.forEach(function(name) {
    var pa = stepAgg[name];
    if (!pa.afterSub) return;
    var best = pa.latestOk || pa.latest;
    result.push({
      kind: 'step', role: best.role, name: best.name,
      status: best.status, cost_usd: pa.cost_usd,
      billable: best.billable !== undefined ? best.billable : true,
      duration: pa.duration, started_at: best.started_at,
      model: best.model || '', tokens: pa.tokens,
      output_doc: best.output_doc || '', preview_url: best.preview_url || '',
      output_url: best.output_url || '',
      error: (pa.latest && pa.latest.error) ? pa.latest.error : '',
      description: best.description || '',
    });
  });

  S.workflowCache = result;
  return result;
}

function deduplicateSteps(items) {
  var roleMap = {};
  var result = [];

  items.forEach(function(item) {
    if (item.kind !== 'step') {
      result.push(item);
      return;
    }
    if (roleMap.hasOwnProperty(item.role)) {
      var existing = result[roleMap[item.role]];
      if (!existing.retries) existing.retries = [];
      existing.retries.push({
        status: existing.status,
        cost_usd: existing.cost_usd,
        duration: existing.duration,
        error: existing.error || '',
        started_at: existing.started_at,
      });
      existing.status = item.status;
      existing.cost_usd = (existing.cost_usd || 0) + (item.cost_usd || 0);
      existing.duration = (existing.duration || 0) + (item.duration || 0);
      existing.started_at = item.started_at;
      existing.model = item.model;
      existing.tokens = (existing.tokens || 0) + (item.tokens || 0);
      existing.output_doc = item.output_doc || existing.output_doc;
      existing.preview_url = item.preview_url || existing.preview_url;
      existing.output_url = item.output_url || existing.output_url;
      existing.error = item.error;
    } else {
      roleMap[item.role] = result.length;
      result.push(Object.assign({}, item));
    }
  });

  return result;
}

function calcProgress(task) {
  var wf = buildFullWorkflow(task);
  var done = wf.filter(function(i) {
    if (i.kind === 'separator' || i.kind === 'notice') return false;
    return i.status === 'completed';
  }).length;
  var total = wf.filter(function(i) {
    return i.kind !== 'separator' && i.kind !== 'notice';
  }).length;
  var pct = total > 0 ? Math.round(done / total * 100) : 0;
  return { done: done, total: total, pct: pct };
}

function getRunningPanels() {
  var workflow = buildWorkflow(S.opusTask || {});
  var panels = [];

  workflow.forEach(function(item) {
    if (item.kind === 'step' && item.status === 'running') {
      panels.push({ key: item.name, role: item.role, subId: null, label: STEP_DISPLAY_MAP[item.name] || ROLE_DISPLAY_MAP[item.role] || item.name, model: item.model || '', started_at: item.started_at || '' });
    }
    if (item.kind === 'subtask' && item.status === 'running') {
      var subName = item.subName;
      var cached = S.subTaskCache.get(subName);
      if (cached && cached.steps) {
        cached.steps.forEach(function(subStep) {
          if (subStep.status === 'running') {
            var subIdNum = extractSubId(subName);
            panels.push({
              key: 'sub-' + subIdNum + ':' + (subStep.name || subStep.role),
              role: subStep.role,
              name: subStep.name || subStep.role,
              subId: subIdNum,
              label: item.name + ' \u2014 ' + (ROLE_DISPLAY_MAP[subStep.role] || subStep.role),
              model: subStep.model || '',
              started_at: subStep.started_at || '',
            });
          }
        });
      } else {
        // 子任务缓存未加载时显示子任务级面板
        panels.push({ key: 'sub-task:' + subName, role: 'sub-task:' + subName, subId: null, label: item.name });
      }
    }
  });

  return panels;
}

function extractSubId(subName) {
  // 优先从 task.sub_tasks（来自 state.json）精确匹配
  if (S.opusTask && S.opusTask.sub_tasks) {
    for (var i = 0; i < S.opusTask.sub_tasks.length; i++) {
      var st = S.opusTask.sub_tasks[i];
      if ((st.name || '') === subName || (st.id || '') === subName) {
        return st.id || '';
      }
    }
  }
  // 尝试从 subTaskCache 获取（task_id 格式如 xxx-subS2，或直接有 sub_id 字段）
  var cached = S.subTaskCache.get(subName);
  if (cached) {
    if (cached.sub_id) return cached.sub_id;
    if (cached.task_id) {
      var m = cached.task_id.match(/sub([A-Za-z0-9]+)$/);
      if (m) return m[1];
    }
  }
  return '';
}

function getFocusLogKey(focus) {
  if (!focus) return null;
  if (focus.kind === 'step') return focus.name || focus.role;
  if (focus.kind === 'subtask') return 'sub-' + focus.subId + ':' + (focus.name || focus.role);
  return null;
}

function fetchSubTaskDetails(taskId, subName) {
  var subUrl = '/vizo/console/api/opus/subtask?task_id=' + encodeURIComponent(taskId) +
    '&sub_name=' + encodeURIComponent(subName);
  if (S.selectedProject) subUrl += '&project=' + encodeURIComponent(S.selectedProject);
  fetch(subUrl)
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.found && data.sub_task) {
        var prev = S.subTaskCache.get(subName);
        S.subTaskCache.set(subName, data.sub_task);
        var prevSig = prev ? (prev.steps||[]).map(function(s){return s.name+':'+s.status;}).join(',') : '';
        var newSig = (data.sub_task.steps||[]).map(function(s){return s.name+':'+s.status;}).join(',');
        if (prevSig !== newSig) {
          S.workflowCache = null;
          if (S.opusTask) renderWorkflowCards(S.opusTask);
          // 新 running 步骤自动聚焦
          if (!S.userManualSelect) {
            var prevR = prev ? (prev.steps||[]).filter(function(s){return s.status==='running';}).map(function(s){return s.name;}) : [];
            var ns = data.sub_task.steps||[], sid = data.sub_task.sub_id||'';
            for (var i=0;i<ns.length;i++) {
              if (ns[i].status==='running' && prevR.indexOf(ns[i].name)<0) {
                S.focus={kind:'subtask',name:ns[i].name,role:ns[i].role||ns[i].name,subId:sid};
                S.autoFollowMap.set('sub-'+sid+':'+ns[i].name,true);
                renderLogPanels(); break;
              }
            }
          }
        }
        // 预加载子任务步骤日志
        var sid2=data.sub_task.sub_id||'', pp=S.selectedProject?'&project='+encodeURIComponent(S.selectedProject):'';
        (data.sub_task.steps||[]).forEach(function(st){
          if(st.status==='pending')return;
          var lk='sub-'+sid2+':'+(st.name||st.role);
          if(S.actionLogs.has(lk)&&S.actionLogs.get(lk).length>0)return;
          var sn=st.name||st.role;
          fetch('/vizo/console/api/opus/logs?task_id='+encodeURIComponent(taskId)+'&step_id='+encodeURIComponent(sn)+'&role='+encodeURIComponent(st.role||sn)+'&sub_id='+encodeURIComponent(sid2)+'&run_index=-1'+pp)
            .then(function(r){return r.json();}).then(function(ld){if(ld.actions&&ld.actions.length>0){S.actionLogs.set(lk,ld.actions);renderLogPanels();}}).catch(function(){});
        });
      }
    })
    .catch(function() {});
}

function handleOpusEvent(msg) {
  if (!msg.data) return;

  // step_action 事件：分发到角色日志缓冲，不触发面板更新
  if (msg.data.event === 'step_action') {
    handleStepAction(msg.data);
    return;
  }

  // ★ 过滤：如果用户已选择特定任务，忽略其他任务的事件
  var incomingTaskId = msg.data.task_id || '';
  if (S.selectedTaskId && incomingTaskId && incomingTaskId !== S.selectedTaskId) {
    if (msg.data.status === 'running' || msg.data.status === 'waiting_confirm') {
      showLiveBanner(incomingTaskId, msg.data.task_name || '');
    }
    return;
  }
  if (S.opusTask && S.opusTask.task_id && incomingTaskId && incomingTaskId !== S.opusTask.task_id) {
    if (msg.data.status === 'running' || msg.data.status === 'waiting_confirm') {
      showLiveBanner(incomingTaskId, msg.data.task_name || '');
    }
    return;
  }

  var prev = S.opusTask;
  S.opusTask = msg.data;
  S.workflowCache = null; // 清空工作流缓存，重新构建
  updateOpusPanel(msg.data);

  var evt = msg.data.event;
  var st = msg.data.status || '';

  // agent_start 时自动切换到新步骤（清除聚焦模式），除非用户手动选择了面板
  if (evt === 'agent_start' && !S.userManualSelect) {
    S.focus = null;
    if (S._lastActionKeys) S._lastActionKeys.clear();
    renderLogPanels();
  }

  // Trigger notifications
  if (evt && ['agent_start','agent_complete','agent_error','pause','terminate','complete'].indexOf(evt) >= 0) {
    showNotification(evt, msg.data);
  }

  // Check pending_confirm (skip on error/terminate events to avoid confusion)
  if (msg.data.pending_confirm && ['agent_error','terminate'].indexOf(evt) < 0) {
    showConfirmNotification(msg.data.pending_confirm);
  }
  if (prev && prev.pending_confirm && !msg.data.pending_confirm) {
    clearConfirmNotification();
  }
  // Handle waiting_confirm event explicitly
  if (evt === 'waiting_confirm' && msg.data.pending_confirm) {
    showConfirmNotification(msg.data.pending_confirm);
  }
  // Handle confirm_resolved event — clear confirm UI, restore running controls
  if (evt === 'confirm_resolved') {
    clearConfirmNotification();
  }

  // Terminal states — 用 poll 获取 state.json 权威状态（progress.json 可能未同步 rolled_back 等状态）
  if (['completed','terminated','rolled_back','failed'].indexOf(st) >= 0) {
    var ld = document.getElementById('hdr-dot');
    if (ld) {
      var ldInfo = getStatusDisplay(st);
      ld.className = 'status-dot ' + ldInfo.cssClass;
      ld.textContent = ldInfo.icon + ' ' + ldInfo.text;
    }
    // 延迟 poll 一次，以 state.json 为准修正最终状态
    setTimeout(function() { pollOpusTask(); }, 2000);
    return;
  }

  // Auto-open panel
  if (!S.panelOpen) togglePanel();
}

function taskFingerprint(task) {
  if (!task) return '';
  var steps = task.steps || [];
  // 包含所有步骤的状态，确保中间步骤变化也能检测到
  var stepsSig = steps.map(function(s) { return s.name + ':' + s.status; }).join(',');
  return task.task_id + '|' + task.status + '|' + stepsSig +
    '|' + (task.cost_usd || 0) +
    '|' + (task.pending_confirm ? task.pending_confirm.request_id || '1' : '0') +
    '|' + (task.updated_at || '');
}

async function pollOpusTask() {
  try {
    // If user is viewing a specific historical task, refresh it + check for live banner
    if (S.selectedTaskId) {
      try {
        var selUrl = '/vizo/console/api/opus/current?task_id=' + encodeURIComponent(S.selectedTaskId);
        if (S.selectedProject) selUrl += '&project=' + encodeURIComponent(S.selectedProject);
        var selResp = await fetch(selUrl);
        var selData = await selResp.json();
        if (selData.has_task) {
          var newFp = taskFingerprint(selData.task);
          var oldFp = taskFingerprint(S.opusTask);
          S.opusTask = selData.task;
          if (newFp !== oldFp) updateOpusPanel(selData.task);
          // 独立刷新日志（不依赖 fingerprint）
          var _s1 = selData.task.status;
          if (_s1 === 'running' || _s1 === 'paused' || _s1 === 'waiting_confirm') {
            refreshRunningLogs(S.selectedTaskId, selData.task.steps || []);
          }
        }
      } catch(e2) {}
      // Check if there's a live running task to show banner
      try {
        var liveResp = await fetch('/vizo/console/api/opus/current');
        var liveData = await liveResp.json();
        if (liveData.has_task && liveData.task.task_id !== S.selectedTaskId) {
          showLiveBanner(liveData.task.task_id, liveData.task.task_name);
        } else {
          removeLiveBanner();
        }
      } catch(e3) {}
      return;
    }

    // Normal mode: follow the live running task
    var resp = await fetch('/vizo/console/api/opus/current');
    var data = await resp.json();
    if (data.has_task) {
      var liveTask = data.task;
      // If a different task is now running (the one we were watching completed)
      // Stay on the old task, show banner for the new one
      if (S.opusTask && S.opusTask.task_id && S.opusTask.task_id !== liveTask.task_id) {
        S.selectedTaskId = S.opusTask.task_id;
        showLiveBanner(liveTask.task_id, liveTask.task_name);
        // Refresh old task's final state
        try {
          var oldUrl = '/vizo/console/api/opus/current?task_id=' + encodeURIComponent(S.opusTask.task_id);
          if (S.selectedProject) oldUrl += '&project=' + encodeURIComponent(S.selectedProject);
          var oldResp = await fetch(oldUrl);
          var oldData = await oldResp.json();
          if (oldData.has_task) {
            var oFp = taskFingerprint(oldData.task);
            var oOldFp = taskFingerprint(S.opusTask);
            S.opusTask = oldData.task;
            if (oFp !== oOldFp) updateOpusPanel(oldData.task);
          }
        } catch(e4) {}
        return;
      }
      var newLiveFp = taskFingerprint(liveTask);
      var oldLiveFp = taskFingerprint(S.opusTask);
      S.opusTask = liveTask;
      removeLiveBanner();
      if (newLiveFp !== oldLiveFp) updateOpusPanel(liveTask);
      // 独立刷新日志
      var _s2 = liveTask.status;
      if (_s2 === 'running' || _s2 === 'paused' || _s2 === 'waiting_confirm') {
        refreshRunningLogs(liveTask.task_id, liveTask.steps || []);
      }
      if (liveTask.pending_confirm) {
        showConfirmNotification(liveTask.pending_confirm);
      } else {
        // 确认请求已消失：检查用户是否有未提交的输入
        var existingBar = document.querySelector('.confirm-bar');
        if (existingBar) {
          var hasUnsaved = false;
          ['notif', 'panel'].forEach(function(suffix) {
            var ta = document.getElementById('confirm-feedback-text-' + suffix);
            if (ta && ta.value.trim()) hasUnsaved = true;
          });
          if (hasUnsaved) {
            showToast('该确认请求已被处理，你的意见未提交。', 'warning');
          }
          clearConfirmNotification();
        }
      }
    } else {
      // No running task
      if (S.opusTask && S.opusTask.task_id) {
        // Task we were watching just completed — stay on it
        S.selectedTaskId = S.opusTask.task_id;
        try {
          var finUrl = '/vizo/console/api/opus/current?task_id=' + encodeURIComponent(S.opusTask.task_id);
          if (S.selectedProject) finUrl += '&project=' + encodeURIComponent(S.selectedProject);
          var finResp = await fetch(finUrl);
          var finData = await finResp.json();
          if (finData.has_task) {
            S.opusTask = finData.task;
            updateOpusPanel(finData.task);
          }
        } catch(e5) {}
        return;
      }
      // Truly no task — reset to empty state
      S.opusTask = null;
      document.getElementById('no-task-msg').style.display = 'flex';
      document.getElementById('opus-controls').style.display = 'none';
      var hdrRow2 = document.getElementById('hdr-row2');
      if (hdrRow2) hdrRow2.style.display = 'none';
      var progressRow = document.getElementById('progress-row');
      if (progressRow) progressRow.style.display = 'none';
      var roleTabs = document.getElementById('role-tabs');
      if (roleTabs) roleTabs.style.display = 'none';
      var streamHdr = document.getElementById('stream-hdr');
      if (streamHdr) streamHdr.style.display = 'none';
      var logBody = document.getElementById('log-body');
      if (logBody) { logBody.style.display = 'none'; logBody.innerHTML = '<div class="log-empty">等待任务开始...</div>'; }
      var logFooter = document.getElementById('log-footer');
      if (logFooter) logFooter.style.display = 'none';
      var hdrDot = document.getElementById('hdr-dot');
      if (hdrDot) hdrDot.className = 'status-dot';
      var hdrName = document.getElementById('hdr-name');
      if (hdrName) hdrName.textContent = t('opusLive');
      var hdrTimer = document.getElementById('hdr-timer');
      if (hdrTimer) hdrTimer.textContent = '00:00';
      var hdrCost = document.getElementById('hdr-cost');
      if (hdrCost) hdrCost.textContent = '$0.00';
      var hdrCtrl = document.getElementById('hdr-ctrl');
      if (hdrCtrl) hdrCtrl.innerHTML = '';
      S.actionLogs.clear();
      if (S._lastActionKeys) S._lastActionKeys.clear();
      S.activeRoles = [];
      S.selectedRole = null;
      S.scrollPositions.clear();
      S.taskFinished = false;
      S.userManualSelect = false;
      if (S._roleInfoTimer) { clearInterval(S._roleInfoTimer); S._roleInfoTimer = null; }
      if (S.taskTimer) { clearInterval(S.taskTimer); S.taskTimer = null; }
      S.roleRemoveTimers.forEach(function(tid) { clearTimeout(tid); });
      S.roleRemoveTimers.clear();
    }
  } catch(e) {}
}

// ======================== Timer Formatting ========================
function formatTimerStr(ms) {
  var s = Math.floor(ms / 1000);
  var m = Math.floor(s / 60);
  var h = Math.floor(m / 60);
  m %= 60; s %= 60;
  return (h ? h + ':' : '') + (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
}

// ======================== Task Switcher ========================
async function fetchTaskList() {
  try {
    var resp = await fetch('/vizo/api/tasks');
    var data = await resp.json();
    S.taskList = data.tasks || [];
    updateTaskDropdown();
  } catch(e) {}
}

function toggleTaskDropdown(e) {
  if (e) e.stopPropagation();
  openTaskSelector();
}

// ── Task Selector Modal ──
S.selectedProject = null;

function openTaskSelector() {
  document.getElementById('task-selector-modal').style.display = '';
  fetch('/vizo/api/tasks').then(function(r) { return r.json(); }).then(function(data) {
    S.taskList = data.tasks || [];
    renderTSProjects();
  });
}

function closeTaskSelector() {
  document.getElementById('task-selector-modal').style.display = 'none';
}

function renderTSProjects() {
  var groups = {};
  S.taskList.forEach(function(t) {
    var p = t.project || '(default)';
    if (!groups[p]) groups[p] = { tasks: [], hasRunning: false };
    groups[p].tasks.push(t);
    if (t.status === 'running') groups[p].hasRunning = true;
  });
  // 自动选中：当前任务所在项目 → 否则第一个
  var autoSelect = Object.keys(groups)[0] || null;
  if (S.selectedTaskId) {
    var found = S.taskList.find(function(tk) { return tk.task_id === S.selectedTaskId; });
    if (found) autoSelect = found.project || '(default)';
  }
  if (!S.selectedProject || !groups[S.selectedProject]) S.selectedProject = autoSelect;
  var html = Object.keys(groups).map(function(p) {
    var g = groups[p];
    var isActive = p === S.selectedProject;
    var count = g.tasks.length;
    var dot = g.hasRunning ? '<span class="t-dd-dot running"></span>' : '';
    return '<div class="ts-proj' + (isActive ? ' active' : '') + '" onclick="selectTSProject(\'' + escapeHtml(p) + '\')">'
      + '<div class="ts-proj-row1">' + dot + '<span>' + escapeHtml(p) + '</span></div>'
      + '<div class="ts-proj-row2">' + count + (count === 1 ? ' task' : ' tasks') + '</div>'
      + '</div>';
  }).join('');
  document.getElementById('ts-project-list').innerHTML = html || '<div class="ts-empty">' + t('tsNoProjects') + '</div>';
  renderTSTasks(groups[S.selectedProject] ? groups[S.selectedProject].tasks : []);
}

function selectTSProject(projName) {
  S.selectedProject = projName;
  renderTSProjects();
}

function renderTSTasks(tasks) {
  // 使用新的复合排序
  tasks = sortTasks(tasks.slice());  // 避免修改原数组

  var currentId = S.selectedTaskId || (S.opusTask && S.opusTask.task_id) || '';
  var html = tasks.map(function(tk) {
    var isActive = tk.task_id === currentId;
    var statusInfo = getStatusDisplay(tk.status);  // 统一状态映射
    var name = formatTaskName(tk, 50);             // 格式化任务名称
    var dateStr = formatTaskDate(tk.task_id);
    var taskType = getTaskTypeDisplay(tk.task_type);
    var isHub = tk.task_id && tk.task_id.indexOf('hub-') === 0;
    var hubClass = isHub ? ' hub' : '';
    var hubIcon = isHub ? '\u{1F916} ' : '';
    var typeBadge = '';
    if (isHub && tk.module_id) {
      typeBadge = '<span class="ts-type-badge hub">' + escapeHtml(tk.module_id) + '</span>';
    } else if (taskType) {
      typeBadge = '<span class="ts-type-badge ' + (taskType.error ? 'error' : '') + '">' + taskType.text + '</span>';
    }

    return '<div class="ts-task' + (isActive ? ' active' : '') + hubClass + '" onclick="selectTask(\'' +
      escapeHtml(tk.task_id) + '\',\'' + escapeHtml(tk.project || '') + '\')">' +
      '<div class="ts-task-row1">' +
        '<span class="t-dd-dot ' + statusInfo.cssClass + '">' + statusInfo.icon + ' ' + statusInfo.text + '</span>' +
        '<span class="t-dd-name">' + hubIcon + escapeHtml(name) + '</span>' +
        typeBadge +
      '</div>' +
      '<div class="ts-task-row2">' +
        '<span>' + dateStr + '</span>' +
        '<span>$' + (tk.cost_usd || 0).toFixed(2) + '</span>' +
      '</div>' +
      '</div>';
  }).join('');
  document.getElementById('ts-task-list').innerHTML = html || '<div class="ts-empty">' + t('tsNoTasks') + '</div>';
}

function formatTaskDate(taskId) {
  if (!taskId || taskId.length < 15) return taskId || '';
  return taskId.substring(4,6) + '-' + taskId.substring(6,8) + ' '
       + taskId.substring(9,11) + ':' + taskId.substring(11,13);
}

async function selectTask(taskId, project) {
  closeTaskSelector();

  S.selectedTaskId = taskId;
  S.selectedProject = project || null;
  removeLiveBanner();
  // Reset role state for the new task
  S.actionLogs.clear();
  if (S._lastActionKeys) S._lastActionKeys.clear();
  S.activeRoles = [];
  S.selectedRole = null;
  S.userManualSelect = false;
  S.taskFinished = false;
  S.workflowCache = null;
  S.subTaskCache = new Map();
  S.expandedSubs = new Set();
  S.focus = null;

  try {
    var url = '/vizo/console/api/opus/current?task_id=' + encodeURIComponent(taskId);
    if (project) url += '&project=' + encodeURIComponent(project);
    var resp = await fetch(url);
    var data = await resp.json();
    if (data.has_task) {
      S.opusTask = data.task;
      updateOpusPanel(data.task);
      // Load historical action logs for all roles
      await loadHistoricalLogs(taskId, data.task.steps || []);
    }
  } catch(e) {}
}

async function loadHistoricalLogs(taskId, steps) {
  // Fetch action logs for each non-pending step in parallel
  // 用 step.name 作为 logKey（面板 key），同时传 step.role 用于后端文件 fallback
  var entries = [];
  var seen = {};
  (steps || []).forEach(function(step) {
    var key = step.name || step.role;
    if (step.status !== 'pending' && key && !seen[key]) {
      seen[key] = true;
      entries.push({ name: key, role: step.role || key });
    }
  });
  if (entries.length === 0) return;

  var projParam = S.selectedProject ? '&project=' + encodeURIComponent(S.selectedProject) : '';
  var fetches = entries.map(function(entry) {
    return fetch('/vizo/console/api/opus/logs?task_id=' + encodeURIComponent(taskId) +
      '&step_id=' + encodeURIComponent(entry.name) +
      '&role=' + encodeURIComponent(entry.role) + projParam)
      .then(function(r) { return r.json(); })
      .then(function(data) { return { name: entry.name, actions: data.actions || [] }; })
      .catch(function() { return { name: entry.name, actions: [] }; });
  });

  var results = await Promise.all(fetches);
  results.forEach(function(r) {
    if (r.actions.length > 0) {
      S.actionLogs.set(r.name, r.actions);
    }
  });

  // Re-render log panels with loaded historical data
  renderLogPanels();
  updateLogFooter();
}

async function loadSubtaskStepLogs(subId, role, stepName) {
  var logKey = 'sub-' + subId + ':' + (stepName || role);
  if (S.actionLogs.has(logKey) && S.actionLogs.get(logKey).length > 0) {
    renderLogPanels();
    return;
  }
  var projParam = S.selectedProject ? '&project=' + encodeURIComponent(S.selectedProject) : '';
  var url = '/vizo/console/api/opus/logs?task_id=' + encodeURIComponent(S.selectedTaskId) +
    '&step_id=' + encodeURIComponent(stepName || role) +
    '&role=' + encodeURIComponent(role) +
    '&sub_id=' + encodeURIComponent(subId) + projParam;
  try {
    var resp = await fetch(url);
    var data = await resp.json();
    if (data.actions && data.actions.length > 0) {
      S.actionLogs.set(logKey, data.actions);
    }
  } catch(e) {}
  renderLogPanels();
}

async function refreshRunningLogs(taskId, steps) {
  // WebSocket 活跃时短暂跳过（3秒内有推送则跳过，避免重复）
  if (Date.now() - S.lastStepActionTs < 3000) return;
  var runningEntries = [];
  (steps || []).forEach(function(step) {
    if (step.status === 'running' || step.status === 'paused') {
      var key = step.name || step.role;
      if (key) runningEntries.push({ name: key, role: step.role || key });
    } else if ((step.status === 'completed' || step.status === 'error') && !(S.actionLogs.has(step.name || step.role))) {
      var key = step.name || step.role;
      if (key) runningEntries.push({ name: key, role: step.role || key });
    }
  });
  // 子任务步骤：从缓存中收集
  if (S.subTaskCache) {
    S.subTaskCache.forEach(function(subData) {
      var subId = subData.sub_id || '';
      (subData.steps || []).forEach(function(ss) {
        if (ss.status === 'pending') return;
        var stepName = ss.name || ss.role;
        var logKey = 'sub-' + subId + ':' + stepName;
        if (ss.status === 'running' || ss.status === 'paused') {
          runningEntries.push({ name: logKey, role: ss.role || stepName, subId: subId, stepName: stepName });
        } else if (!S.actionLogs.has(logKey)) {
          runningEntries.push({ name: logKey, role: ss.role || stepName, subId: subId, stepName: stepName });
        }
      });
    });
  }
  if (runningEntries.length === 0) return;
  var projParam = S.selectedProject ? '&project=' + encodeURIComponent(S.selectedProject) : '';
  var fetches = runningEntries.map(function(entry) {
    var url = '/vizo/console/api/opus/logs?task_id=' + encodeURIComponent(taskId) +
      '&step_id=' + encodeURIComponent(entry.stepName || entry.name) +
      '&role=' + encodeURIComponent(entry.role) + '&run_index=-1' + projParam;
    if (entry.subId) url += '&sub_id=' + encodeURIComponent(entry.subId);
    return fetch(url)
      .then(function(r) { return r.json(); })
      .then(function(data) { return { name: entry.name, actions: data.actions || [] }; })
      .catch(function() { return { name: entry.name, actions: [] }; });
  });
  var results = await Promise.all(fetches);
  var changed = false;
  results.forEach(function(r) {
    var existing = S.actionLogs.get(r.name) || [];
    if (r.actions.length > existing.length) {
      S.actionLogs.set(r.name, r.actions);
      changed = true;
    }
  });
  if (changed) { renderLogPanels(); updateLogFooter(); }
}

function showLiveBanner(taskId, taskName) {
  removeLiveBanner();
  var container = document.getElementById('banner-area');
  if (!container) return;
  var name = taskName || taskId || '';
  var truncName = name.length > 30 ? name.substring(0, 30) + '...' : name;
  var banner = document.createElement('div');
  banner.className = 'live-banner';
  banner.id = 'live-task-banner';
  banner.onclick = function() {
    S.selectedTaskId = null;
    S.opusTask = null;
    S.actionLogs.clear();
    if (S._lastActionKeys) S._lastActionKeys.clear();
    S.activeRoles = [];
    S.selectedRole = null;
    S.userManualSelect = false;
    S.taskFinished = false;
    S.workflowCache = null;
    S.subTaskCache = new Map();
    S.expandedSubs = new Set();
    S.focus = null;
    removeLiveBanner();
    pollOpusTask();
  };
  banner.innerHTML = '<span class="b-dot"></span>' +
    '<span style="flex:1">' + t('liveBannerText') + ' — ' + escapeHtml(truncName) + '</span>' +
    '<button class="live-banner-dismiss" type="button" onclick="event.stopPropagation(); removeLiveBanner();">隐藏</button>' +
    '<span style="color:var(--accent);font-size:0.75rem">&#10132;</span>';
  container.appendChild(banner);
}

function removeLiveBanner() {
  var b = document.getElementById('live-task-banner');
  if (b) b.remove();
}

const ROLE_ICONS = {
  requirement_analyst: { icon: '&#128269;', bg: '#1e3a5f', color: 'var(--accent)' },
  product_manager: { icon: '&#128203;', bg: '#1e3a5f', color: 'var(--accent)' },
  architect: { icon: '&#127959;', bg: '#312e81', color: 'var(--purple)' },
  backend_developer: { icon: '&#128187;', bg: '#064e3b', color: 'var(--green)' },
  frontend_developer: { icon: '&#127912;', bg: '#064e3b', color: 'var(--green)' },
  developer_backend: { icon: '&#128187;', bg: '#064e3b', color: 'var(--green)' },
  developer_frontend: { icon: '&#127912;', bg: '#064e3b', color: 'var(--green)' },
  fix_engineer: { icon: '&#128295;', bg: '#064e3b', color: 'var(--green)' },
  qa_engineer: { icon: '&#128270;', bg: '#713f12', color: 'var(--yellow)' },
  integration_engineer: { icon: '&#128279;', bg: '#312e81', color: 'var(--purple)' },
  devops_engineer: { icon: '&#9881;', bg: '#4a044e', color: 'var(--pink)' },
  devops: { icon: '&#9881;', bg: '#4a044e', color: 'var(--pink)' },
  knowledge_engineer: { icon: '&#128218;', bg: '#1e3a5f', color: 'var(--accent)' },
  embedded_engineer: { icon: '&#128268;', bg: '#713f12', color: 'var(--yellow)' },
  assistant: { icon: '&#9733;', bg: '#1e3a5f', color: 'var(--accent)' },
  default: { icon: '&#9733;', bg: '#1e3a5f', color: 'var(--accent)' },
};

function toggleDrawer() {
  // 左右布局下步骤列表常驻可见，不需要折叠
  return;
}

function updateOpusPanel(task) {
  document.getElementById('no-task-msg').style.display = 'none';
  // Show panel content elements
  var progressRow = document.getElementById('progress-row');
  if (progressRow) progressRow.style.display = '';
  var logPanelsContainer = document.getElementById('log-panels-container');
  if (logPanelsContainer) logPanelsContainer.style.display = '';
  var logFooter = document.getElementById('log-footer');
  if (logFooter) logFooter.style.display = '';

  var steps = task.steps || [];
  var prog = calcProgress(task);

  // Stale task detection: if status=running but no update for 30+ minutes
  var effectiveStatus = task.status;
  if (task.status === 'running' && task.updated_at) {
    var updatedMs = new Date(task.updated_at).getTime();
    if (Date.now() - updatedMs > 30 * 60 * 1000) {
      effectiveStatus = 'stale';
    }
  }
  var isRunning = effectiveStatus === 'running' || effectiveStatus === 'waiting_confirm';

  // Build stale step index set
  var staleStepIndices = new Set();
  for (var si = 0; si < steps.length; si++) {
    if (steps[si].status === 'running') {
      for (var sj = si + 1; sj < steps.length; sj++) {
        if (steps[sj].name === steps[si].name) { staleStepIndices.add(si); break; }
      }
    }
  }

  // === L1: Header ===
  var hdrDot = document.getElementById('hdr-dot');
  if (hdrDot) {
    var dotStatus = effectiveStatus === 'stale' ? 'failed' : task.status;
    var statusInfo = getStatusDisplay(dotStatus);
    hdrDot.className = 'status-dot ' + statusInfo.cssClass;
    hdrDot.textContent = statusInfo.icon + ' ' + statusInfo.text;
  }
  // 使用格式化后的任务名称
  var taskName = formatTaskName(task, 60);
  var hdrName = document.getElementById('hdr-name');
  if (hdrName) hdrName.textContent = taskName;
  // L1: 类型标签（使用新的展示逻辑）
  var hdrType = document.getElementById('hdr-type');
  if (hdrType) {
    var taskType = getTaskTypeDisplay(task.task_type);
    if (taskType) {
      var typeText = taskType.text + '（' + taskType.type + '）';
      if (task.scale === 'large') typeText += ' · ' + t('scaleLarge');

      // 使用 innerHTML 支持后续添加的点击事件
      hdrType.innerHTML = typeText;
      hdrType.style.display = '';
      hdrType.style.cursor = 'pointer';  // 添加手型光标

      // 添加点击事件：打开工作流流程图弹窗
      hdrType.onclick = function() {
        showWorkflowDiagram(task.task_type);
      };

      // 设置类型标签的样式类
      if (taskType.error) {
        hdrType.className = 'task-type-label error';
      } else {
        hdrType.className = 'task-type-label info';
      }
    } else {
      hdrType.style.display = 'none';
    }
  }
  var hdrRow2 = document.getElementById('hdr-row2');
  if (hdrRow2) hdrRow2.style.display = '';
  var hdrCost = document.getElementById('hdr-cost');
  if (hdrCost) {
    var totalCost = task.cost_usd || 0;
    // 顶层 cost 可能未汇总（如任务异常退出），从步骤累加作为 fallback
    if (!totalCost && task.steps) {
      task.steps.forEach(function(s) { totalCost += (s.cost_usd || 0); });
    }
    hdrCost.textContent = '$' + totalCost.toFixed(2);
  }
  var hdrCostInfo = document.getElementById('hdr-cost-info');
  if (hdrCostInfo) {
    hdrCostInfo.style.display = '';
    hdrCostInfo.title = t('costInfoTip');
    hdrCostInfo.textContent = '\u24d8';
  }

  // Timer
  if (S.taskTimer) { clearInterval(S.taskTimer); S.taskTimer = null; }
  var hdrTimer = document.getElementById('hdr-timer');
  if (hdrTimer) {
    var updateTimer = function() {
      if (isRunning && task.started_at) {
        var startMs = new Date(task.started_at).getTime();
        var elapsed = Date.now() - startMs;
        hdrTimer.textContent = formatTimerStr(elapsed);
      } else {
        var totalSec = steps.reduce(function(a, s) { return a + (s.duration || 0); }, 0);
        hdrTimer.textContent = formatTimerStr(totalSec * 1000);
      }
    };
    updateTimer();
    if (isRunning) S.taskTimer = setInterval(updateTimer, 1000);
  }

  // Controls — derive display status for waiting_confirm
  var controlStatus = effectiveStatus === 'stale' ? 'terminated' : task.status;
  if (task.pending_confirm && task.pending_confirm.request_id) {
    controlStatus = 'waiting_confirm';
  }
  setControls(controlStatus, task.pending_confirm);

  // === L3: Progress row — hide in split layout ===
  var prRow = document.getElementById('progress-row');
  if (prRow && document.getElementById('split-body')) {
    prRow.style.display = 'none';
  }

  // === L4: Split body layout (step-list left, log-panels right) ===
  if (!document.getElementById('split-body')) {
    var wrapper = document.createElement('div');
    wrapper.id = 'split-body';
    wrapper.className = 'split-body';
    var stepCol = document.createElement('div');
    stepCol.className = 'step-list-col';
    var logCol = document.createElement('div');
    logCol.className = 'log-panels-col';
    var stepList = document.getElementById('step-list');
    var logContainer = document.getElementById('log-panels-container');
    var logFooter = document.getElementById('log-footer');
    if (stepList && logContainer && logFooter) {
      stepCol.appendChild(stepList);
      stepList.classList.add('open');
      logCol.appendChild(logContainer);
      logCol.appendChild(logFooter);
      var controls = document.getElementById('opus-controls');
      if (controls && controls.parentNode) {
        controls.parentNode.insertBefore(wrapper, controls.nextSibling);
      }
      wrapper.appendChild(stepCol);
      wrapper.appendChild(logCol);
    }
  }

  // === L4: Step list (workflow cards) ===
  renderWorkflowCards(task, isRunning, effectiveStatus, steps);

  // === 渲染日志面板 ===
  // 日志为空时，从 JSONL 文件加载历史日志（不论运行中还是已完成）
  // 修复：无 WebSocket 连接时，运行中任务也能通过轮询显示日志
  if (S.actionLogs.size === 0 && task.task_id) {
    loadHistoricalLogs(task.task_id, steps);
  } else if (isRunning && !S.isConnected && task.task_id) {
    // WebSocket 未连接时，增量拉取运行中步骤的日志
    refreshRunningLogs(task.task_id, steps);
  } else {
    renderLogPanels();
  }
  updateLogFooter();
}

function renderWorkflowCards(task, isRunning, effectiveStatus, steps) {
  if (!steps) steps = task.steps || [];
  if (isRunning === undefined) isRunning = task.status === 'running';
  if (!effectiveStatus) effectiveStatus = task.status;
  // 使用新的完整工作流构建逻辑
  var workflow = buildFullWorkflow(task);
  var isHubTask = task.task_id && task.task_id.indexOf('hub-') === 0;
  var cardsHTML = '';
  workflow.forEach(function(item) {
    // 不展示未启动的步骤（hub 任务除外）
    if (item.status === 'pending') {
      if (!isHubTask) return;
      // hub 任务 pending 步骤：灰色简化卡片
      var roleInfo = ROLE_ICONS[item.role] || ROLE_ICONS.default;
      cardsHTML += '<div class="agent-card card-pending">' +
        '<div class="agent-card-header" style="cursor:default;opacity:0.5">' +
          '<div class="agent-card-row1">' +
            '<div class="agent-role">' +
              '<div class="role-icon" style="background:var(--bg-tertiary);color:var(--text-muted)">\u25CB</div>' +
              '<span>' + escapeHtml(item.description || STEP_DISPLAY_MAP[item.name] || ROLE_DISPLAY_MAP[item.role] || item.name) + '</span>' +
            '</div>' +
          '</div>' +
          '<div class="agent-card-row2">' +
            '<span class="agent-status-badge badge-waiting">' + t('status_pending', '\u5F85\u6267\u884C') + '</span>' +
          '</div>' +
        '</div>' +
      '</div>';
      return;
    }

    if (item.kind === 'separator') {
      cardsHTML += '<div class="workflow-separator"><span>' + escapeHtml(item.label) + '</span></div>';
      return;
    }

    if (item.kind === 'notice') {
      cardsHTML += '<div class="workflow-notice">' + escapeHtml(item.text) + '</div>';
      return;
    }

    if (item.kind === 'subtask') {
      var subStatusClass, subBadgeClass;
      switch(item.status) {
        case 'running': subStatusClass = 'card-running'; subBadgeClass = 'badge-running'; break;
        case 'completed': subStatusClass = 'card-completed'; subBadgeClass = 'badge-completed'; break;
        case 'error': subStatusClass = 'card-error'; subBadgeClass = 'badge-error'; break;
        default: subStatusClass = 'card-pending'; subBadgeClass = 'badge-waiting';
      }
      var subTimeStr;
      if (item.status === 'running' && item.started_at) {
        var subElapsed = Math.floor((Date.now() - new Date(item.started_at).getTime()) / 1000);
        subTimeStr = '<span class="step-duration" data-running="true" data-started-at="' + escapeHtml(item.started_at) + '">' + formatDuration(subElapsed) + '</span>';
      } else {
        subTimeStr = item.duration ? formatDuration(item.duration) : '--';
      }
      var subCostStr = item.cost_usd ? '$' + item.cost_usd.toFixed(2) : '--';
      var isExpanded = S.expandedSubs.has(item.subName);

      var isPending = item.status === 'pending';

      cardsHTML += '<div class="agent-card subtask-card ' + subStatusClass + '">' +
        '<div class="agent-card-header"' + (isPending ? '' : ' onclick="toggleSubTask(\'' + escapeHtml(item.subName) + '\')"') + ' style="' + (isPending ? 'cursor:default;opacity:0.6' : '') + '">' +
          '<div class="agent-card-row1">' +
            '<div class="agent-role">' +
              '<div class="role-icon" style="background:#312e81;color:var(--purple)">\u25a3</div>' +
              '<span>' + escapeHtml(item.name) + '</span>' +
            '</div>' +
            '<span class="subtask-expand-icon">' + (isPending ? '' : isExpanded ? '\u25b2' : '\u25bc') + '</span>' +
          '</div>' +
          '<div class="agent-card-row2">' +
            '<span class="agent-status-badge ' + subBadgeClass + '">' + t('status_' + item.status, item.status) + '</span>' +
            '<span>' + subTimeStr + '</span>' +
            '<span style="color:var(--yellow)">' + subCostStr + '</span>' +
          '</div>' +
        '</div>';

      if (isExpanded) {
        var cached = S.subTaskCache.get(item.subName);
        if (cached && cached.steps) {
          cardsHTML += '<div class="subtask-inner">';
          cached.steps.forEach(function(subStep) {
            var ssInfo = ROLE_ICONS[subStep.role] || ROLE_ICONS.default;
            var ssBadge = subStep.status === 'running' ? 'badge-running' :
                          subStep.status === 'completed' ? 'badge-completed' :
                          subStep.status === 'error' ? 'badge-error' : 'badge-waiting';
            var ssTime = subStep.duration ? formatDuration(subStep.duration) : '--';
            var ssCost = subStep.cost_usd ? '$' + subStep.cost_usd.toFixed(2) : '--';
            var subIdNum = extractSubId(item.subName);
            var focusTarget = 'subtask,' + subIdNum + ',' + subStep.role + ',' + (subStep.name || subStep.role);
            cardsHTML += '<div class="subtask-inner-row" onclick="setFocus(\'' + focusTarget + '\')">' +
              '<div class="role-icon" style="background:' + ssInfo.bg + ';color:' + ssInfo.color + ';width:22px;height:22px;font-size:0.7rem">' + ssInfo.icon + '</div>' +
              '<span class="subtask-inner-name">' + escapeHtml(ROLE_DISPLAY_MAP[subStep.role] || subStep.role) + '</span>' +
              '<span class="agent-status-badge ' + ssBadge + '">' + t('status_' + subStep.status, subStep.status) + '</span>' +
              '<span class="subtask-inner-meta">' + ssTime + '</span>' +
              '<span class="subtask-inner-meta" style="color:var(--yellow)">' + ssCost + '</span>' +
            '</div>';
          });
          cardsHTML += '</div>';
        } else {
          cardsHTML += '<div class="subtask-inner"><div class="log-empty">\u52a0\u8f7d\u4e2d...</div></div>';
          // 触发异步加载
          if (S.opusTask && S.opusTask.task_id) {
            fetchSubTaskDetails(S.opusTask.task_id, item.subName);
          }
        }
      }
      cardsHTML += '</div>';
      return;
    }

    // kind === 'step'
    var roleInfo = ROLE_ICONS[item.role] || ROLE_ICONS.default;
    var displayStatus = item.status;
    var statusClass, badgeClass;
    switch(displayStatus) {
      case 'running': statusClass = 'card-running'; badgeClass = 'badge-running'; break;
      case 'completed': statusClass = 'card-completed'; badgeClass = 'badge-completed'; break;
      case 'error': statusClass = 'card-error'; badgeClass = 'badge-error'; break;
      case 'paused': statusClass = 'card-paused'; badgeClass = 'badge-paused'; break;
      default: statusClass = 'card-pending'; badgeClass = 'badge-waiting';
    }
    var isActive = displayStatus === 'running' || displayStatus === 'error' || displayStatus === 'completed';
    var timeStr;
    if (displayStatus === 'running' && item.started_at) {
      var elapsedSec = Math.floor((Date.now() - new Date(item.started_at).getTime()) / 1000);
      timeStr = '<span class="step-duration" data-running="true" data-started-at="' + escapeHtml(item.started_at) + '">' + formatDuration(elapsedSec) + '</span>';
    } else {
      timeStr = item.duration ? formatDuration(item.duration) : '--';
    }
    var costStr = item.cost_usd ? '$' + item.cost_usd.toFixed(2) : '--';
    if (item.billable === false) costStr = '<span class="cost-tag unbillable">' + t('unbillable') + '</span>';

    var detailsHTML = '';
    if (item.model) {
      detailsHTML += '<div class="agent-detail-row"><span class="agent-detail-label">' + t('model') + '</span><span style="color:var(--purple)">' + escapeHtml(item.model) + '</span></div>';
    }
    if (item.tokens) {
      detailsHTML += '<div class="agent-detail-row"><span class="agent-detail-label">Tokens</span><span>' + item.tokens.toLocaleString() + '</span></div>';
    }
    if (item.retries && item.retries.length > 0) {
      detailsHTML += '<div class="agent-detail-row"><span class="agent-detail-label">\u91cd\u8bd5</span><span style="color:var(--yellow)">' + item.retries.length + ' \u6b21</span></div>';
    }
    if (item.output_doc) {
      var itemPreviewUrl = item.output_url || item.preview_url;
      if (itemPreviewUrl) {
        if (isHubTask) {
          // hub 任务：页内预览抽屉
          detailsHTML += '<div class="agent-detail-row">' +
            '<a class="agent-detail-doc md-preview-link" href="javascript:void(0)" ' +
            'data-task-id="' + escapeHtml(task.task_id) + '" ' +
            'data-filename="' + escapeHtml(item.output_doc) + '" ' +
            'data-title="' + escapeHtml(item.description || item.name) + '">' +
            '&#128196; ' + escapeHtml(item.output_doc) + ' &mdash; ' + t('clickToPreview') + '</a></div>';
        } else {
          // dev 任务：保持外链新标签页
          detailsHTML += '<div class="agent-detail-row"><a class="agent-detail-doc" href="' + escapeHtml(itemPreviewUrl) + '" target="_blank">&#128196; ' + escapeHtml(item.output_doc) + ' &mdash; ' + t('clickToPreview') + '</a></div>';
        }
      } else {
        detailsHTML += '<div class="agent-detail-row"><span class="agent-detail-doc">&#128196; ' + escapeHtml(item.output_doc) + '</span></div>';
      }
    }
    if (item.error) {
      detailsHTML += '<div class="agent-detail-error">' + escapeHtml(item.error) + '</div>';
    }

    var focusTarget = 'step,' + item.name + ',' + item.role;
    var stepLabel = STEP_DISPLAY_MAP[item.name] || ROLE_DISPLAY_MAP[item.role] || item.name;
    cardsHTML += '<div class="agent-card ' + statusClass + (isActive ? ' expanded' : '') + '" onclick="setFocus(\'' + focusTarget + '\')">' +
      '<div class="agent-card-header">' +
        '<div class="agent-card-row1">' +
          '<div class="agent-role">' +
            '<div class="role-icon" style="background:' + roleInfo.bg + ';color:' + roleInfo.color + '">' + roleInfo.icon + '</div>' +
            '<span>' + escapeHtml(stepLabel) + '</span>' +
          '</div>' +
        '</div>' +
        '<div class="agent-card-row2">' +
          '<span class="agent-status-badge ' + badgeClass + '">' + t('status_' + item.status, item.status) + '</span>' +
          '<span>' + timeStr + '</span>' +
          '<span style="color:var(--yellow)">' + costStr + '</span>' +
        '</div>' +
      '</div>' +
      '<div class="agent-card-body">' + detailsHTML + '</div>' +
    '</div>';
  });

  document.getElementById('agent-cards').innerHTML = cardsHTML;

  // === 步骤耗时实时刷新 ===
  if (S._stepDurationTimer) { clearInterval(S._stepDurationTimer); S._stepDurationTimer = null; }
  if (isRunning) {
    S._stepDurationTimer = setInterval(function() {
      document.querySelectorAll('.step-duration[data-running="true"]').forEach(function(el) {
        var startedAt = el.dataset.startedAt;
        if (!startedAt) return;
        var diffSec = Math.floor((Date.now() - new Date(startedAt).getTime()) / 1000);
        el.textContent = formatDuration(diffSec);
      });
      document.querySelectorAll('.panel-time-info[data-live="1"]').forEach(function(el) {
        var startedAt = el.dataset.startedAt;
        if (!startedAt) return;
        var st = new Date(startedAt);
        var hh = String(st.getHours()).padStart(2, '0');
        var mm = String(st.getMinutes()).padStart(2, '0');
        var diffSec = Math.floor((Date.now() - st.getTime()) / 1000);
        el.textContent = hh + ':' + mm + ' \u00b7 ' + formatDuration(diffSec);
      });
    }, 1000);
  }

  // === 终态判定 ===
  var isTerminal = ['completed','terminated','rolled_back'].indexOf(task.status) >= 0 || effectiveStatus === 'stale';
  if (isTerminal && !S.taskFinished) {
    S.taskFinished = true;
  }
  if (isRunning && S.taskFinished) {
    S.taskFinished = false;
  }

  // === 异步拉取 running 子任务的内部步骤 ===
  if (isRunning && task.task_id) {
    workflow.forEach(function(item) {
      if (item.kind === 'subtask' && item.status === 'running') {
        if (!S.subTaskCache.has(item.subName)) {
          fetchSubTaskDetails(task.task_id, item.subName);
        }
      }
    });
  }
}

// ======================== Live Log Panel (Multi-Panel) ========================

function handleStepAction(data) {
  var currentTaskId = S.opusTask ? S.opusTask.task_id : null;
  if (data.task_id !== currentTaskId) return;
  if (S.taskFinished) return;
  S.lastStepActionTs = Date.now();

  var stepId = data.step_id;
  var action = data.action;
  if (!action) return;

  // 按 step_id 确定 logKey（sub-{N}:{role} 或 {role}）
  var logKey = stepId;

  // 去重：通过 timestamp+type+target 防止相同事件重复
  var dedupKey = (action.timestamp || '') + '|' + (action.type || '') + '|' + (action.target || '');
  if (action.snippet) dedupKey += '|' + action.snippet.substring(0, 80);
  if (!S._lastActionKeys) S._lastActionKeys = new Map();
  var roleKeys = S._lastActionKeys.get(logKey);
  if (!roleKeys) { roleKeys = []; S._lastActionKeys.set(logKey, roleKeys); }
  if (roleKeys.indexOf(dedupKey) >= 0) return; // 已存在，跳过
  roleKeys.push(dedupKey);
  if (roleKeys.length > 200) roleKeys.splice(0, roleKeys.length - 200);

  var isNewRole = !S.actionLogs.has(logKey);
  if (isNewRole) {
    S.actionLogs.set(logKey, []);
  }
  var logs = S.actionLogs.get(logKey);
  logs.push(action);
  if (logs.length > 500) {
    logs.splice(0, logs.length - 500);
  }

  // 新角色首次出现，触发面板重新渲染以创建对应面板
  if (isNewRole && S.focus === null) {
    renderLogPanels();
  }

  // 多面板模式
  if (S.focus === null) {
    appendLogToPanel(logKey, action);
  } else {
    var focusKey = getFocusLogKey(S.focus);
    if (logKey === focusKey) {
      appendLogToPanel(logKey, action);
    }
  }
  updateLogFooter();
}

function appendLogToPanel(logKey, action) {
  var panelBody = document.querySelector('[data-panel-key="' + logKey + '"] .panel-body');
  if (!panelBody) return;

  var empty = panelBody.querySelector('.log-empty');
  if (empty) empty.remove();

  var entry = document.createElement('div');
  entry.className = 'log-entry';

  var typeLabels = {read:'\u8bfb\u53d6', write:'\u5199\u5165', exec:'\u6267\u884c', think:'\u601d\u8003', search:'\u641c\u7d22', output:'\u8f93\u51fa', init:'\u521d\u59cb\u5316'};
  if (S.lang === 'en') typeLabels = {read:'READ', write:'WRITE', exec:'EXEC', think:'THINK', search:'SEARCH', output:'OUTPUT', init:'INIT'};
  var typeLabel = typeLabels[action.type] || action.type;

  entry.innerHTML =
    '<span class="log-ts">' + escapeHtml(action.timestamp || '') + '</span>' +
    '<span class="log-type ' + (action.type || '') + '">[' + typeLabel + ']</span>' +
    '<span class="log-target">' + escapeHtml(action.target || '') + '</span>';
  panelBody.appendChild(entry);

  if (action.snippet) {
    var snippetText = action.snippet;
    var sLines = snippetText.split('\n');
    if (sLines.length > 10) snippetText = sLines.slice(-10).join('\n');
    var snippetEl = document.createElement('div');
    snippetEl.className = 'log-snippet' + (action.type === 'think' ? ' think-snippet' : action.type === 'output' ? ' output-snippet' : '');
    snippetEl.textContent = snippetText;
    panelBody.appendChild(snippetEl);
  }

  var autoFollow = S.autoFollowMap.get(logKey);
  if (autoFollow !== false) {
    requestAnimationFrame(function() {
      panelBody.scrollTop = panelBody.scrollHeight;
    });
  }
}

function renderLogPanels() {
  var container = document.getElementById('log-panels-container');
  if (!container) return;

  if (S.focus !== null) {
    // 聚焦模式：单面板
    var focusKey = getFocusLogKey(S.focus);
    if (!focusKey) return;

    // 保存当前滚动位置
    var existingBody = container.querySelector('[data-panel-key="' + focusKey + '"] .panel-body');
    var savedScroll = existingBody ? existingBody.scrollTop : -1;
    var savedScrollHeight = existingBody ? existingBody.scrollHeight : 0;

    container.innerHTML = '';
    var focusWf = buildWorkflow(S.opusTask || {});
    var focusMatch = focusWf.filter(function(w) { return w.name === S.focus.name; }).pop();
    var role = S.focus.role || (focusMatch && focusMatch.role) || S.focus.name;
    var panel = createPanelElement({ key: focusKey, role: role, subId: S.focus.subId || null,
      label: STEP_DISPLAY_MAP[S.focus.name] || ROLE_DISPLAY_MAP[role] || S.focus.name,
      model: (focusMatch && focusMatch.model) || '' });
    container.appendChild(panel);
    panel.style.height = '100%';
    if (!S.autoFollowMap.has(focusKey)) S.autoFollowMap.set(focusKey, true);
    renderLogStreamInPanel(focusKey);

    // 恢复滚动位置：autoFollow=false 时恢复到之前的位置
    if (savedScroll >= 0 && S.autoFollowMap.get(focusKey) === false) {
      var newBody = container.querySelector('[data-panel-key="' + focusKey + '"] .panel-body');
      if (newBody) newBody.scrollTop = savedScroll;
    }
    return;
  }

  // 多面板模式
  var panels = getRunningPanels();

  // 无 running 角色时，仅在任务已结束时展示历史日志
  if (panels.length === 0) {
    var taskRunning = S.opusTask && S.opusTask.status === 'running';
    if (!taskRunning) {
      var wfItems = buildWorkflow(S.opusTask || {});
      S.actionLogs.forEach(function(logs, logKey) {
        if (logs.length > 0) {
          var wfMatch = wfItems.filter(function(w) { return w.name === logKey; }).pop();
          var role = wfMatch ? wfMatch.role : logKey;
          panels.push({ key: logKey, role: role, subId: null,
            label: STEP_DISPLAY_MAP[logKey] || ROLE_DISPLAY_MAP[role] || logKey,
            model: (wfMatch && wfMatch.model) || '' });
        }
      });
    }
  }

  if (panels.length === 0) {
    if (container.children.length === 0) {
      container.innerHTML = '<div class="log-empty">\u7b49\u5f85\u89d2\u8272\u542f\u52a8...</div>';
    }
    return;
  }

  var panelHeight;
  if (panels.length <= 3) {
    panelHeight = Math.floor(container.clientHeight / panels.length);
    container.style.overflowY = 'hidden';
  } else {
    panelHeight = Math.max(120, Math.floor(container.clientHeight / 3));
    container.style.overflowY = 'auto';
  }

  // 保存所有面板的滚动位置
  var savedScrolls = {};
  Array.from(container.children).forEach(function(el) {
    var key = el.getAttribute('data-panel-key');
    var body = el.querySelector('.panel-body');
    if (key && body) savedScrolls[key] = body.scrollTop;
  });

  // 增量 DOM 更新
  var existingPanels = {};
  Array.from(container.children).forEach(function(el) {
    var key = el.getAttribute('data-panel-key');
    if (key) existingPanels[key] = el;
  });

  var newKeys = new Set(panels.map(function(p) { return p.key; }));

  // 移除不在新列表中的面板
  Object.keys(existingPanels).forEach(function(key) {
    if (!newKeys.has(key)) {
      existingPanels[key].remove();
      S.autoFollowMap.delete(key);
    }
  });

  // 添加/更新面板
  panels.forEach(function(panel) {
    var el = existingPanels[panel.key];
    if (!el) {
      el = createPanelElement(panel);
      container.appendChild(el);
      if (!S.autoFollowMap.has(panel.key)) {
        S.autoFollowMap.set(panel.key, true);
      }
      renderLogStreamInPanel(panel.key);
    }
    el.style.height = panelHeight + 'px';
    updatePanelHeader(el, panel);
  });

  // 恢复滚动位置
  panels.forEach(function(panel) {
    if (savedScrolls[panel.key] !== undefined) {
      var body = document.querySelector('[data-panel-key="' + panel.key + '"] .panel-body');
      if (body) body.scrollTop = savedScrolls[panel.key];
    }
  });
}

function createPanelElement(panel) {
  var el = document.createElement('div');
  el.className = 'log-panel';
  el.setAttribute('data-panel-key', panel.key);

  var header = document.createElement('div');
  header.className = 'panel-header-bar';
  var isLive = panel.status === 'running' || (!panel.status && getRunningPanels().some(function(p) { return p.key === panel.key; }));
  var dotClass = isLive ? 'running' : 'completed';
  var timeInfo = '';
  if (panel.started_at) {
    var startTime = new Date(panel.started_at);
    var hh = String(startTime.getHours()).padStart(2, '0');
    var mm = String(startTime.getMinutes()).padStart(2, '0');
    var elapsedSec = Math.floor((Date.now() - startTime.getTime()) / 1000);
    var elapsedStr = formatDuration(elapsedSec);
    timeInfo = '<span class="panel-time-info" data-started-at="' + escapeHtml(panel.started_at) + '" data-live="' + (isLive ? '1' : '0') + '">' + hh + ':' + mm + ' \u00b7 ' + elapsedStr + '</span>';
  }
  header.innerHTML = '<span class="panel-role-dot ' + dotClass + '"></span>' +
    '<span class="panel-role-name">' + escapeHtml(panel.label) + '</span>' +
    (panel.model ? '<span class="panel-model-tag">' + escapeHtml(panel.model) + '</span>' : '') +
    timeInfo +
    (isLive ? '<span class="live-tag">LIVE</span>' : '');
  el.appendChild(header);

  var body = document.createElement('div');
  body.className = 'panel-body';
  body.innerHTML = '<div class="log-empty">' + t('waitingForAgent', {name: panel.label}) + '</div>';

  // 滚动检测
  body.addEventListener('scroll', function() {
    var distFromBottom = body.scrollHeight - body.scrollTop - body.clientHeight;
    if (distFromBottom > 50) {
      S.autoFollowMap.set(panel.key, false);
    } else if (distFromBottom <= 10) {
      S.autoFollowMap.set(panel.key, true);
    }
  });

  el.appendChild(body);
  return el;
}

function updatePanelHeader(el, panel) {
  var nameSpan = el.querySelector('.panel-role-name');
  if (nameSpan) nameSpan.textContent = panel.label;
  var modelTag = el.querySelector('.panel-model-tag');
  if (panel.model) {
    if (modelTag) {
      modelTag.textContent = panel.model;
    } else {
      var tag = document.createElement('span');
      tag.className = 'panel-model-tag';
      tag.textContent = panel.model;
      var header = el.querySelector('.panel-header-bar');
      var liveTag = header ? header.querySelector('.live-tag') : null;
      if (liveTag) header.insertBefore(tag, liveTag);
      else if (header) header.appendChild(tag);
    }
  } else if (modelTag) {
    modelTag.remove();
  }
}

function renderLogStreamInPanel(logKey) {
  var panelBody = document.querySelector('[data-panel-key="' + logKey + '"] .panel-body');
  if (!panelBody) return;

  var logs = S.actionLogs.get(logKey) || [];
  if (logs.length === 0) return;

  var fragment = document.createDocumentFragment();
  var typeLabels = {read:'\u8bfb\u53d6', write:'\u5199\u5165', exec:'\u6267\u884c', think:'\u601d\u8003', search:'\u641c\u7d22', output:'\u8f93\u51fa', init:'\u521d\u59cb\u5316'};
  if (S.lang === 'en') typeLabels = {read:'READ', write:'WRITE', exec:'EXEC', think:'THINK', search:'SEARCH', output:'OUTPUT', init:'INIT'};

  logs.forEach(function(action) {
    var entry = document.createElement('div');
    entry.className = 'log-entry';
    entry.style.animation = 'none';
    var typeLabel = typeLabels[action.type] || action.type;
    entry.innerHTML =
      '<span class="log-ts">' + escapeHtml(action.timestamp || '') + '</span>' +
      '<span class="log-type ' + (action.type || '') + '">[' + typeLabel + ']</span>' +
      '<span class="log-target">' + escapeHtml(action.target || '') + '</span>';
    fragment.appendChild(entry);
    if (action.snippet) {
      var snippetText = action.snippet;
      var lines = snippetText.split('\n');
      if (lines.length > 10) snippetText = lines.slice(-10).join('\n');
      var snippetEl = document.createElement('div');
      snippetEl.className = 'log-snippet' + (action.type === 'think' ? ' think-snippet' : action.type === 'output' ? ' output-snippet' : '');
      snippetEl.textContent = snippetText;
      fragment.appendChild(snippetEl);
    }
  });

  // 保存滚动位置（非 autoFollow 时需要恢复）
  var savedScrollTop = panelBody.scrollTop;
  var autoFollow = S.autoFollowMap.get(logKey);

  panelBody.innerHTML = '';
  panelBody.appendChild(fragment);

  if (autoFollow !== false) {
    panelBody.scrollTop = panelBody.scrollHeight;
  } else {
    // 恢复之前的滚动位置
    panelBody.scrollTop = savedScrollTop;
  }
}

function initLogScrollDetection() {
  // Legacy boot path still calls this hook before deep-link setup.
  // Panel-level scroll listeners are attached in createPanelElement(),
  // so the shared init only needs to exist as a compatibility no-op.
}

function setFocus(target) {
  if (!target) { S.focus = null; S.userManualSelect = false; renderLogPanels(); return; }
  var parts = target.split(',');
  if (parts[0] === 'step') {
    S.focus = { kind: 'step', name: parts[1], role: parts[2] || parts[1] };
  } else if (parts[0] === 'subtask') {
    S.focus = { kind: 'subtask', subId: parts[1], role: parts[2], name: parts[3] || parts[2] };
  }
  S.userManualSelect = true;
  // 聚焦时强制滚到最新
  var key = getFocusLogKey(S.focus);
  if (key) S.autoFollowMap.set(key, true);
  // 子任务步骤：按需加载日志
  if (S.focus && S.focus.kind === 'subtask' && key && !S.actionLogs.has(key)) {
    loadSubtaskStepLogs(S.focus.subId, S.focus.role, S.focus.name);
    return; // loadSubtaskStepLogs 完成后自行调 renderLogPanels
  }
  renderLogPanels();
}

function clearFocus() {
  S.focus = null;
  S.userManualSelect = false;
  renderLogPanels();
  updateLogFooter();
}

function toggleSubTask(subName) {
  if (S.expandedSubs.has(subName)) {
    S.expandedSubs.delete(subName);
  } else {
    S.expandedSubs.add(subName);
  }
  S.workflowCache = null;
  // 只重绘工作流卡片区域，不触发日志面板和全局面板重建
  if (S.opusTask) renderWorkflowCards(S.opusTask);
}

function updateLogFooter() {
  var countEl = document.getElementById('log-footer-count');
  var roleEl = document.getElementById('log-footer-role');
  var statusEl = document.getElementById('log-footer-status');
  var dotEl = document.getElementById('log-status-dot');
  var btnLiveAll = document.getElementById('btn-live-all');

  // 聚焦模式：显示「● 直播全部」按钮 + 聚焦角色信息
  if (S.focus !== null) {
    if (btnLiveAll) btnLiveAll.style.display = '';
    if (dotEl) dotEl.style.display = 'none';
    var focusKey = getFocusLogKey(S.focus);
    var focusLabel = ROLE_DISPLAY_MAP[S.focus.role] || S.focus.role;
    var focusLogs = S.actionLogs.get(focusKey) || [];
    if (roleEl) roleEl.textContent = t('focusLabel') + ': ' + focusLabel;
    if (statusEl) statusEl.textContent = '';
    if (countEl) countEl.textContent = focusLogs.length + ' events';
    return;
  }

  // 多面板模式：隐藏按钮
  if (btnLiveAll) btnLiveAll.style.display = 'none';
  if (dotEl) dotEl.style.display = '';

  if (S.taskFinished) {
    var taskStatus = S.opusTask ? S.opusTask.status : 'completed';
    var terminalStatusMap = {
      completed: t('taskDone'),
      terminated: t('taskTerminated2'),
      rolled_back: t('taskRolledBack'),
    };
    if (statusEl) statusEl.textContent = terminalStatusMap[taskStatus] || t('taskEnded');
    if (dotEl) dotEl.className = 'log-status-dot finished';
    if (roleEl) {
      // Show recovery info for rolled_back tasks
      if (taskStatus === 'rolled_back' && S.opusTask && S.opusTask.rollback_info) {
        var tid = S.opusTask.task_id;
        roleEl.innerHTML = '<span style="color:var(--yellow,#fbbf24);font-size:0.8rem;cursor:pointer;" title="点击复制恢复命令" onclick="navigator.clipboard.writeText(\'git cherry-pick opus-snapshot/' + tid + '\').then(function(){showToast(\'已复制\',\'success\')})">恢复: git cherry-pick opus-snapshot/' + tid + '</span>';
      } else {
        roleEl.textContent = '';
      }
    }
    var totalLogs = 0;
    S.actionLogs.forEach(function(logs) { totalLogs += logs.length; });
    if (countEl) countEl.textContent = totalLogs > 0 ? totalLogs + ' events' : '';
    return;
  }

  var panels = getRunningPanels();
  var totalEvents = 0;
  panels.forEach(function(p) {
    var logs = S.actionLogs.get(p.key) || [];
    totalEvents += logs.length;
  });

  if (countEl) countEl.textContent = t('events', {n: totalEvents});
  if (panels.length > 0) {
    if (roleEl) roleEl.textContent = panels.length + ' \u89d2\u8272\u5e76\u884c';
    if (statusEl) statusEl.textContent = t('workingOn');
    if (dotEl) dotEl.className = 'log-status-dot running';
  } else {
    // 历史任务有日志
    var logCount = 0;
    S.actionLogs.forEach(function(logs) { logCount += logs.length; });
    if (logCount > 0) {
      if (roleEl) roleEl.textContent = S.actionLogs.size + ' \u89d2\u8272';
      if (statusEl) statusEl.textContent = '';
      if (dotEl) dotEl.className = 'log-status-dot completed';
      if (countEl) countEl.textContent = logCount + ' events';
    } else {
      if (roleEl) roleEl.textContent = '';
      if (statusEl) statusEl.textContent = '';
      if (dotEl) dotEl.className = 'log-status-dot';
    }
  }
}

function toggleAutoFollow() {
  // 切换所有面板的 autoFollow
  var allTrue = true;
  S.autoFollowMap.forEach(function(v) { if (!v) allTrue = false; });
  var newVal = !allTrue;
  S.autoFollowMap.forEach(function(v, k) { S.autoFollowMap.set(k, newVal); });
  var btn = document.getElementById('btn-follow');
  if (btn) btn.classList.toggle('highlight', !newVal);
  if (newVal) {
    // 滚动所有面板到底部
    document.querySelectorAll('.panel-body').forEach(function(body) {
      body.scrollTop = body.scrollHeight;
    });
  }
}

function clearCurrentLog() {
  if (S.focus) {
    var focusKey = getFocusLogKey(S.focus);
    if (focusKey) {
      S.actionLogs.set(focusKey, []);
      renderLogStreamInPanel(focusKey);
    }
  }
  updateLogFooter();
}

function insertDisconnectMessage() {
  // F5.10: WebSocket 断线时在当前角色的日志区插入灰色提示行
  if (!S.selectedRole) return;
  var body = document.getElementById('log-body');
  if (!body) return;
  var msg = document.createElement('div');
  msg.className = 'log-disconnect';
  msg.textContent = t('disconnectedLog');
  body.appendChild(msg);
  if (S.autoFollow) {
    requestAnimationFrame(function() { body.scrollTop = body.scrollHeight; });
  }
}

function formatDuration(seconds) {
  if (seconds < 60) return Math.round(seconds) + 's';
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}m ${s}s`;
}

function timeAgo(isoStr) {
  if (!isoStr) return '';
  const sec = Math.floor((Date.now() - new Date(isoStr).getTime()) / 1000);
  if (sec < 0 || isNaN(sec)) return '';
  if (sec < 60) return t('justNow');
  if (sec < 3600) return t('mAgo', {n: Math.floor(sec / 60)});
  if (sec < 86400) return t('hAgo', {n: Math.floor(sec / 3600)});
  return t('dAgo', {n: Math.floor(sec / 86400)});
}

function formatTime(isoStr) {
  if (!isoStr) return '';
  try {
    const d = new Date(isoStr);
    return d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
  } catch(e) { return ''; }
}

// ======================== UI Helpers ========================
function toggleSidebar() {
  S.sidebarOpen = !S.sidebarOpen;
  document.getElementById('sidebar').classList.toggle('collapsed', !S.sidebarOpen);
  document.getElementById('sidebar-expand').style.display = S.sidebarOpen ? 'none' : '';
  setTimeout(() => { if (S.fitAddon) S.fitAddon.fit(); }, 300);
}

function togglePanel() {
  S.panelOpen = !S.panelOpen;
  document.getElementById('right-panel').classList.toggle('open', S.panelOpen);
  document.getElementById('panel-toggle').classList.toggle('active', S.panelOpen);
  // 打开面板时清除红点
  if (S.panelOpen) {
    var dot = document.getElementById('panel-red-dot');
    if (dot) dot.style.display = 'none';
  }
}

// ========== Chrome Bridge 状态 ==========
function toggleChromePopup(e) {
  if (e) e.stopPropagation();
  document.getElementById('chrome-popup').classList.toggle('show');
}
document.addEventListener('click', function(e) {
  var p = document.getElementById('chrome-popup');
  if (p && !e.target.closest('#chrome-status')) p.classList.remove('show');
});
function updateChromeStatus() {
  fetch('/vizo/console/api/chrome-status').then(r=>r.json()).then(d => {
    _lastChromeStatus = d;
    var el = document.getElementById('chrome-status');
    var txt = document.getElementById('chrome-status-text');
    var ds = document.getElementById('chrome-popup-disabled');
    var dc = document.getElementById('chrome-popup-disconnected');
    var cn = document.getElementById('chrome-popup-connected');
    el.classList.remove('on', 'waiting', 'disabled');
    if (d.service_enabled === false) {
      el.classList.add('disabled');
      txt.textContent = 'Chrome 已关闭';
      if (ds) ds.style.display = 'block';
      dc.style.display = 'none';
      cn.style.display = 'none';
    } else if (d.chrome_connected) {
      el.classList.add('on');
      txt.textContent = 'Chrome 已连接';
      if (ds) ds.style.display = 'none';
      dc.style.display = 'none'; cn.style.display = 'block';
      document.getElementById('chr-browser').textContent = (d.browser_info && d.browser_info.browser) || 'Chrome';
      document.getElementById('chr-tools').textContent = (d.cached_tools || 0) + ' 个';
      var s = d.uptime_seconds || 0;
      document.getElementById('chr-uptime').textContent = s > 3600 ? Math.floor(s/3600)+'h '+Math.floor((s%3600)/60)+'m' : Math.floor(s/60)+'m';
    } else {
      el.classList.add('waiting');
      txt.textContent = d.service_installed === false ? 'Chrome 未安装' : 'Chrome 待连接';
      if (ds) ds.style.display = 'none';
      dc.style.display = 'block'; cn.style.display = 'none';
    }
    _syncChromeStatusIntoMcpTools(d);
  }).catch(function(){});
}
updateChromeStatus();
setInterval(updateChromeStatus, 15000);
window.addEventListener('focus', function() {
  updateChromeStatus();
  if (_currentSettingsSection === 'mcp-tools') loadMcpTools();
});
document.addEventListener('visibilitychange', function() {
  if (document.visibilityState === 'visible') {
    updateChromeStatus();
    if (_currentSettingsSection === 'mcp-tools') loadMcpTools();
  }
});
window.addEventListener('storage', function(e) {
  if (e.key === 'opus_chrome_bridge_status') {
    updateChromeStatus();
    if (_currentSettingsSection === 'mcp-tools') loadMcpTools();
  }
});
window.addEventListener('message', function(e) {
  if (e.origin !== window.location.origin) return;
  if (e.data && e.data.type === 'opus-chrome-connected') {
    updateChromeStatus();
    if (_currentSettingsSection === 'mcp-tools') loadMcpTools();
  }
});

// switchTab removed — tabs have been consolidated into v5 layout

function updateConnectionStatus(status) {
  const dot = document.getElementById('conn-dot');
  const text = document.getElementById('conn-text');
  dot.className = 'conn-dot';
  if (status === 'connected') {
    text.textContent = t('connected');
  } else if (status === 'disconnected') {
    dot.classList.add('disconnected');
    text.textContent = t('disconnected');
  } else if (status === 'reconnecting') {
    dot.classList.add('reconnecting');
    text.textContent = t('reconnecting');
  }
}

function showReconnectOverlay() {
  document.getElementById('reconnect-overlay').classList.add('visible');
}
function hideReconnectOverlay() {
  document.getElementById('reconnect-overlay').classList.remove('visible');
}

const MAX_ACTIVE_TOASTS = 4;
const TOAST_TONE_META = {
  info: { badge: '提醒', icon: 'notifications', duration: 8000, ariaLive: 'polite' },
  success: { badge: '完成', icon: 'check_circle', duration: 8000, ariaLive: 'polite' },
  warning: { badge: '注意', icon: 'warning', duration: 8000, ariaLive: 'polite' },
  error: { badge: '异常', icon: 'error', duration: 8000, ariaLive: 'assertive' }
};
function dismissToast(node) {
  if (!node || node.dataset.toastClosing === 'true') return;
  node.dataset.toastClosing = 'true';
  if (node._timer) {
    clearTimeout(node._timer);
    node._timer = null;
  }
  node.classList.add('is-leaving');
  setTimeout(() => { if (node.parentNode) node.parentNode.removeChild(node); }, 170);
}
function enforceToastLimit(host) {
  if (!host) return;
  const activeNodes = Array.from(host.querySelectorAll('.vizo-toast'))
    .filter(node => node.dataset.toastClosing !== 'true');
  while (activeNodes.length > MAX_ACTIVE_TOASTS) {
    dismissToast(activeNodes.pop());
  }
}
function showToast(msg, type='info') {
  const host = document.getElementById('toastHost');
  if (!host) return;
  const tone = TOAST_TONE_META[type] ? type : 'info';
  const meta = TOAST_TONE_META[tone];
  const node = document.createElement('article');
  node.className = 'vizo-toast vizo-toast--' + tone;
  node.setAttribute('role', tone === 'error' ? 'alert' : 'status');
  node.setAttribute('aria-live', meta.ariaLive);
  node.innerHTML =
    '<span class="vizo-toast__icon-shell" aria-hidden="true"><span class="material-symbols-outlined vizo-toast__icon">' + meta.icon + '</span></span>' +
    '<div class="vizo-toast__copy"><div class="vizo-toast__topline"><div class="vizo-toast__title">' + escapeHtml(String(msg || '')) + '</div><span class="vizo-toast__badge">' + meta.badge + '</span></div></div>' +
    '<button class="vizo-toast__close" type="button" aria-label="关闭提醒"><span class="material-symbols-outlined" aria-hidden="true">close</span></button>';
  const closeButton = node.querySelector('.vizo-toast__close');
  if (closeButton) closeButton.addEventListener('click', () => dismissToast(node));
  host.prepend(node);
  enforceToastLimit(host);
  node._timer = setTimeout(() => dismissToast(node), meta.duration);
}

function escapeHtml(s) {
  if (!s) return '';
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ======================== Input Box ========================
function toggleInputMode() {
  S.inputBoxVisible = !S.inputBoxVisible;
  // Toggle input row visibility (not the entire input-area which contains shortcuts)
  var inputRow = document.getElementById('input-row');
  if (inputRow) inputRow.style.display = S.inputBoxVisible ? 'flex' : 'none';
  const btn = document.getElementById('input-mode-btn');
  if (btn) {
    btn.classList.toggle('active', S.inputBoxVisible);
    btn.innerHTML = S.inputBoxVisible ? '&#9000; ' + t('inputBox') : '&#9000; ' + t('terminal');
  }

  const termContainer = document.querySelector('.terminal-container');

  // 切换终端交互性
  if (S.term) {
    if (S.inputBoxVisible) {
      // Input Box 模式：终端只读，隐藏光标，移除焦点
      S.term.options.cursorBlink = false;
      S.term.options.cursorInactiveStyle = 'none';
      // 通过 Theme API 从源头让光标透明（xterm.js 会用此颜色重新生成注入的 <style>）
      S.term.options.theme = Object.assign({}, S.term.options.theme, {
        cursor: 'transparent',
        cursorAccent: 'transparent'
      });
      termContainer.classList.add('input-box-mode');
      S.term.blur();
    } else {
      // Terminal 模式：终端可交互，显示光标，自动聚焦
      S.term.options.cursorBlink = true;
      S.term.options.cursorInactiveStyle = 'outline';
      var th = getTheme();
      S.term.options.theme = Object.assign({}, S.term.options.theme, {
        cursor: th === 'dark' ? '#38bdf8' : '#0071e3',
        cursorAccent: th === 'dark' ? '#0a0e17' : '#ffffff'
      });
      termContainer.classList.remove('input-box-mode');
      S.term.focus();
    }
  }

  setTimeout(() => { if (S.fitAddon) S.fitAddon.fit(); }, 50);
}

function handleInputKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  // Auto-resize textarea
  autoResizeInput(e.target);
}

function autoResizeInput(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}

function sendMessage() {
  const input = document.getElementById('user-input');
  if (!input) return;
  const text = input.value;
  if (!text.trim()) return;
  if (S.ws && S.ws.readyState === WebSocket.OPEN) {
    // 先发送文字，再单独发送回车，模拟真实终端打字行为
    // Claude Code 的输入处理需要文字和回车分开到达
    S.ws.send(JSON.stringify({ type: 'input', data: text }));
    setTimeout(() => {
      if (S.ws && S.ws.readyState === WebSocket.OPEN) {
        S.ws.send(JSON.stringify({ type: 'input', data: '\r' }));
      }
    }, 30);
  }
  input.value = '';
  input.style.height = 'auto';
}

// ======================== Init ========================
function applyConsoleDeepLink() {
  const raw = String(window.location.hash || '').replace(/^#/, '').trim();
  if (!raw) return;
  const parts = raw.split('/').filter(Boolean);
  const view = parts[0] || '';
  const section = parts[1] || '';
  if (view === 'settings') {
    openSettings(section || 'main-session');
  } else if (view === 'agents') {
    openAgentsView();
  } else if (view === 'model-config') {
    openModelConfig();
  }
}

document.addEventListener('DOMContentLoaded', () => {
  // Apply saved language (defaults to zh)
  const langBtn = document.getElementById('lang-toggle');
  if (langBtn) langBtn.textContent = S.lang === 'zh' ? 'EN' : '中';
  if (S.lang !== 'zh') applyLang();

  // Apply saved theme (button icon sync)
  updateThemeButtonIcon(getTheme());

  initTerminal();
  updateUIState();  // 初始状态：未连接
  loadSessions().then(() => {
    // Auto-restore last active session
    const lastSessionId = localStorage.getItem('opus_last_session');
    if (lastSessionId && S.sessions.find(s => s.id === lastSessionId && s.status !== 'stopped')) {
      switchSession(lastSessionId);
    }
  });
  // Poll Vizo task status with dynamic interval
  function schedulePoll() {
    var interval = 10000;
    // 有未提交的输入内容时降低轮询频率
    ['notif', 'panel'].forEach(function(suffix) {
      var ta = document.getElementById('confirm-feedback-text-' + suffix);
      if (ta && ta.value.trim()) interval = 120000;
    });
    setTimeout(function() { pollOpusTask().finally(schedulePoll); }, interval);
  }
  pollOpusTask().then(schedulePoll);

  // Init log scroll detection
  initLogScrollDetection();

  window.addEventListener('hashchange', applyConsoleDeepLink);
  window.setTimeout(applyConsoleDeepLink, 0);

  // Global keyboard shortcuts
  document.addEventListener('keydown', (e) => {
    // Escape: close modal first, then switch input mode
    if (e.key === 'Escape') {
      const tsModal = document.getElementById('task-selector-modal');
      if (tsModal && tsModal.style.display !== 'none') {
        closeTaskSelector();
        return;
      }
      const switchConfirmModal = document.getElementById('session-switch-confirm-modal');
      if (switchConfirmModal && switchConfirmModal.style.display !== 'none') {
        closeSessionSwitchConfirm(false);
        return;
      }
      const modal = document.getElementById('new-session-modal');
      if (modal && modal.style.display !== 'none') {
        closeNewSessionModal();
        return;
      }
      const authModal = document.getElementById('connection-auth-modal');
      if (authModal && authModal.style.display !== 'none') {
        closeConnectionAuthModal();
        return;
      }
    }
    // Ctrl+. : Interrupt (Ctrl+C)
    if (e.ctrlKey && e.key === '.') {
      e.preventDefault();
      sendPtyKey('\x03');
      return;
    }
    // Ctrl+K : Clear terminal
    if (e.ctrlKey && e.key === 'k') {
      e.preventDefault();
      if (S.term) S.term.clear();
      return;
    }
    // / key in terminal mode: auto-focus input box for slash commands
    if (e.key === '/' && !e.ctrlKey && !e.metaKey && !e.altKey) {
      const active = document.activeElement;
      const isInTerminal = !active || active === document.body || active.closest('.terminal-container');
      if (isInTerminal) {
        if (S.inputBoxVisible) {
          // Mobile/input-box mode: focus textarea and prefill /
          e.preventDefault();
          var ui = document.getElementById('user-input');
          if (ui) { ui.value = '/'; ui.focus(); }
        } else {
          // Desktop terminal mode: send / directly to PTY
          e.preventDefault();
          sendPtyKey('/');
        }
      }
    }
  });
});

// ======================== Settings ========================
let _currentSettingsSection = 'main-session';
let _settingsReturnView = 'terminal';
let _settingsNeedsReload = true;
const _settingsSectionMeta = {
  'main-session': {buttonId: 'save-main-session-btn', stateId: 'settings-state-main-session', pendingText: '保存中...'},
  'external-models': {buttonId: 'save-external-models-btn', stateId: 'settings-state-external-models', pendingText: '保存中...'},
  'role-models': {buttonId: 'save-role-models-btn', stateId: 'settings-state-role-models', pendingText: '保存中...', secondaryButtonId: 'reset-role-models-btn'},
  'network': {buttonId: 'save-network-btn', stateId: 'settings-state-network', pendingText: '保存中...'},
  'password': {buttonId: 'save-password-btn', stateId: 'settings-state-password', pendingText: '修改中...'}
};

function _sameStringArray(a, b) {
  const aa = (a || []).slice().sort();
  const bb = (b || []).slice().sort();
  if (aa.length !== bb.length) return false;
  for (let i = 0; i < aa.length; i++) {
    if (aa[i] !== bb[i]) return false;
  }
  return true;
}

function _getSettingsActionMeta(section) {
  return _settingsSectionMeta[section] || {};
}

function _getSettingsButton(section) {
  const meta = _getSettingsActionMeta(section);
  return meta.buttonId ? document.getElementById(meta.buttonId) : null;
}

function _getSettingsStateText(section, dirty) {
  if (section === 'password') return dirty ? '待保存密码修改' : '未填写';
  if (section === 'mcp-tools') return '开关会立即生效';
  return dirty ? '有未保存修改' : '未修改';
}

function _isMainSessionDirty() {
  const apikeyInput = document.getElementById('new-apikey');
  const rawKey = apikeyInput?.value.trim() || '';
  const masked = apikeyInput?.dataset.masked || '';
  const providerEl = document.getElementById('new-provider');
  const templateEl = document.getElementById('new-provider-template');
  const authModeEl = document.getElementById('new-openai-auth-mode');
  const baseUrlEl = document.getElementById('new-baseurl');
  const providerGroupId = providerEl?.value || 'claude';
  let modelDefaultsChanged = false;
  if (providerGroupId === 'anthropic_compatible') {
    const modelDefaults = getMainProviderModelDefaults();
    const origDefaults = _getMainProviderOriginalDefaults();
    modelDefaultsChanged = Object.keys(modelDefaults).some(function(k) { return modelDefaults[k] !== origDefaults[k]; });
  }
  return (
    (!!rawKey && rawKey !== masked) ||
    ((providerEl?.value || 'claude') !== (providerEl?.dataset.orig || 'claude')) ||
    ((templateEl?.value || '') !== (templateEl?.dataset.orig || '')) ||
    ((authModeEl?.value || 'api_key') !== (authModeEl?.dataset.orig || 'api_key')) ||
    ((baseUrlEl?.value.trim() || '') !== (baseUrlEl?.dataset.orig || '')) ||
    modelDefaultsChanged
  );
}

function _isExternalModelsDirty() {
  const cards = document.querySelectorAll('#ext-models-list .ext-model-card');
  for (const card of cards) {
    const apiKeyRaw = card.querySelector('.ext-apikey')?.value.trim() || '';
    const apiKeyChanged = !!apiKeyRaw && apiKeyRaw !== '__EXISTING__';
    const burlInput = card.querySelector('.ext-baseurl');
    const cliInput = card.querySelector('.ext-cli-model');
    const baseUrlChanged = (burlInput?.value.trim() || '') !== (burlInput?.dataset.orig || '');
    const cliChanged = (cliInput?.value.trim() || '') !== (cliInput?.dataset.orig || '');
    if (apiKeyChanged || baseUrlChanged || cliChanged) return true;
  }
  return ['new-ext-display', 'new-ext-cli', 'new-ext-url', 'new-ext-key'].some(function(id) {
    return (document.getElementById(id)?.value || '').trim() !== '';
  }) || (document.getElementById('new-ext-access-mode')?.value || 'anthropic_gateway') !== 'anthropic_gateway';
}

function _isRoleModelsDirty() {
  if (!_modelsData || !_modelsData.roles) return false;
  for (const role of _modelsData.roles) {
    const select = document.querySelector('#model-table .model-select[data-role="' + role.role + '"]');
    if (select && select.value !== role.current_model) return true;
    const effortSelect = document.querySelector('#model-table .reasoning-select[data-role="' + role.role + '"]');
    if (effortSelect && effortSelect.value !== (role.current_reasoning_effort || 'inherit')) return true;
  }
  const expected = (_mcpData && _mcpData.role_mcps) ? _mcpData.role_mcps : {};
  const rows = document.querySelectorAll('#model-table .mcp-chips-row');
  for (const row of rows) {
    const role = row.dataset.role;
    const current = Array.from(row.querySelectorAll('.mcp-chip.active')).map(function(chip) { return chip.dataset.mcp; });
    const orig = expected[role] || [];
    if (!_sameStringArray(current, orig)) return true;
  }
  return false;
}

function _isMcpToolsDirty() {
  return false;
}

function _isNetworkDirty() {
  const input = document.getElementById('domain-input');
  if (!input) return false;
  return input.value.trim() !== (input.dataset.orig || '');
}

function _isPasswordDirty() {
  return ['old-password', 'new-password', 'confirm-password'].some(function(id) {
    return (document.getElementById(id)?.value || '').trim() !== '';
  });
}

function _isSettingsSectionDirty(section) {
  if (section === 'main-session') return _isMainSessionDirty();
  if (section === 'external-models') return _isExternalModelsDirty();
  if (section === 'role-models') return _isRoleModelsDirty();
  if (section === 'mcp-tools') return _isMcpToolsDirty();
  if (section === 'network') return _isNetworkDirty();
  if (section === 'password') return _isPasswordDirty();
  return false;
}

function _refreshSettingsSectionState(section) {
  const dirty = _isSettingsSectionDirty(section);
  const meta = _getSettingsActionMeta(section);
  const nav = document.querySelector('.settings-nav-item[data-section="' + section + '"]');
  const stateEl = meta.stateId ? document.getElementById(meta.stateId) : null;
  const saveBtn = _getSettingsButton(section);
  if (nav) nav.classList.toggle('dirty', dirty);
  if (stateEl) {
    stateEl.textContent = _getSettingsStateText(section, dirty);
    stateEl.classList.toggle('dirty', dirty);
    stateEl.classList.toggle('clean', !dirty);
  }
  if (saveBtn && saveBtn.dataset.loading !== '1') {
    saveBtn.disabled = !dirty;
  }
}

function _refreshAllSettingsStates() {
  Object.keys(_settingsSectionMeta).forEach(_refreshSettingsSectionState);
}

function _hasSettingsUnsavedChanges() {
  return Object.keys(_settingsSectionMeta).some(_isSettingsSectionDirty);
}

function _setSettingsActionLoading(section, loading) {
  const meta = _getSettingsActionMeta(section);
  const saveBtn = _getSettingsButton(section);
  const secondaryBtn = meta.secondaryButtonId ? document.getElementById(meta.secondaryButtonId) : null;
  if (saveBtn) {
    if (!saveBtn.dataset.defaultText) saveBtn.dataset.defaultText = saveBtn.textContent;
    saveBtn.dataset.loading = loading ? '1' : '0';
    saveBtn.disabled = !!loading;
    saveBtn.textContent = loading ? (meta.pendingText || '保存中...') : (saveBtn.dataset.defaultText || saveBtn.textContent);
  }
  if (secondaryBtn) secondaryBtn.disabled = !!loading;
  if (!loading) _refreshSettingsSectionState(section);
}

function _saveCurrentSettingsSection() {
  if (_currentSettingsSection === 'main-session') return saveMainSessionConfig();
  if (_currentSettingsSection === 'external-models') return saveExternalModelsConfig();
  if (_currentSettingsSection === 'role-models') return saveModels();
  if (_currentSettingsSection === 'network') return saveDomain();
  if (_currentSettingsSection === 'password') return changePassword();
}

function _reloadSettingsDrafts() {
  Object.keys(_settingsSectionMeta).forEach(function(section) {
    const meta = _getSettingsActionMeta(section);
    const nav = document.querySelector('.settings-nav-item[data-section="' + section + '"]');
    const stateEl = meta.stateId ? document.getElementById(meta.stateId) : null;
    const saveBtn = _getSettingsButton(section);
    if (nav) nav.classList.remove('dirty');
    if (stateEl) {
      stateEl.textContent = _getSettingsStateText(section, false);
      stateEl.classList.remove('dirty');
      stateEl.classList.add('clean');
    }
    if (saveBtn) {
      saveBtn.disabled = true;
      if (saveBtn.dataset.defaultText) saveBtn.textContent = saveBtn.dataset.defaultText;
      saveBtn.dataset.loading = '0';
    }
  });
  _modelsLoaded = false;
  _modelsData = null;
  _mcpData = null;
  _mcpServicesLoaded = false;
  _mcpServicesData = null;
  const modelTable = document.getElementById('model-table');
  if (modelTable) {
    modelTable.innerHTML = '<div style="text-align:center;color:var(--text-muted);padding:2rem">加载中...</div>';
  }
  const mcpList = document.getElementById('mcp-service-list');
  if (mcpList) mcpList.innerHTML = '<div style="text-align:center;color:var(--text-muted);padding:1rem">加载中...</div>';
  ['old-password', 'new-password', 'confirm-password'].forEach(function(id) {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  ['mcp-import-json', 'mcp-manual-name', 'mcp-manual-command', 'mcp-manual-args', 'mcp-manual-env'].forEach(function(id) {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  const connResult = document.getElementById('conn-test-result');
  if (connResult) {
    connResult.textContent = '';
    connResult.innerHTML = '';
  }
  if (document.getElementById('ext-add-form')) {
    toggleExtAddForm(false);
  }
  loadApiKeyStatus();
  loadSavedConnections();
  loadExternalModels();
  loadDomainStatus();
  _refreshSettingsSectionState('password');
}

function _detectCurrentMainView() {
  if (document.getElementById('project-manager-view')?.style.display === 'flex') return 'project-manager';
  if (document.getElementById('agents-view')?.style.display === 'flex') return 'agents';
  if (document.getElementById('model-config-view')?.style.display === 'flex') return 'model-config';
  return 'terminal';
}

function _showMainView(viewName) {
  document.getElementById('settings-view').style.display = 'none';
  if (viewName === 'project-manager') {
    document.getElementById('project-manager-view').style.display = 'flex';
    return;
  }
  if (viewName === 'agents') {
    document.getElementById('agents-view').style.display = 'flex';
    return;
  }
  if (viewName === 'model-config') {
    document.getElementById('model-config-view').style.display = 'flex';
    return;
  }
  document.querySelector('.terminal-container').style.display = '';
  document.getElementById('input-area').style.display = '';
  if (S.term && S.fitAddon) {
    try { S.fitAddon.fit(); } catch(e) {}
  }
}

function openSettings(section) {
  if (document.getElementById('settings-view')?.style.display === 'flex') {
    switchSettingsSection(section || _currentSettingsSection || 'main-session');
    return;
  }
  _settingsReturnView = _detectCurrentMainView();
  document.querySelector('.terminal-container').style.display = 'none';
  document.getElementById('input-area').style.display = 'none';
  document.getElementById('project-manager-view').style.display = 'none';
  document.getElementById('agents-view').style.display = 'none';
  document.getElementById('model-config-view').style.display = 'none';
  document.getElementById('settings-view').style.display = 'flex';
  if (_settingsNeedsReload) {
    _reloadSettingsDrafts();
    _settingsNeedsReload = false;
  }
  switchSettingsSection(section || _currentSettingsSection || 'main-session');
}

function closeSettings() {
  if (_hasSettingsUnsavedChanges() && !window.confirm('当前设置页有未保存的修改，确定返回吗？')) {
    return;
  }
  _settingsNeedsReload = true;
  _modelsLoaded = false;
  _showMainView(_settingsReturnView || 'terminal');
}

async function changePassword() {
  const oldPwd = document.getElementById('old-password').value;
  const newPwd = document.getElementById('new-password').value;
  const confirmPwd = document.getElementById('confirm-password').value;
  if (!oldPwd || !newPwd) { showToast('请填写完整信息', 'error'); return; }
  if (newPwd.length < 8) { showToast('新密码至少 8 位', 'error'); return; }
  if (newPwd !== confirmPwd) { showToast('两次密码不一致', 'error'); return; }
  _setSettingsActionLoading('password', true);
  try {
    const r = await fetch('/vizo/console/api/settings/password', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({old_password: oldPwd, new_password: newPwd})
    });
    const d = await r.json();
    if (d.success) {
      showToast('密码修改成功，其他设备需重新登录', 'success');
      ['old-password','new-password','confirm-password'].forEach(
        id => document.getElementById(id).value = '');
      _refreshSettingsSectionState('password');
    } else {
      showToast(d.error || '修改失败', 'error');
    }
  } catch(e) { showToast('网络错误', 'error'); }
  finally { _setSettingsActionLoading('password', false); }
}

// ======================== Settings Section Switch ========================
function switchSettingsSection(section) {
  _currentSettingsSection = section;
  document.querySelectorAll('.settings-nav-item').forEach(function(btn) {
    btn.classList.toggle('active', btn.dataset.section === section);
  });
  document.querySelectorAll('.settings-panel').forEach(function(panel) {
    panel.classList.toggle('active', panel.dataset.section === section);
  });
  const scrollEl = document.querySelector('.settings-panel.active .settings-panel-scroll');
  if (scrollEl) scrollEl.scrollTop = 0;

  if (section === 'main-session') {
    loadApiKeyStatus();
    loadSavedConnections();
  } else if (section === 'external-models') {
    loadExternalModels();
  } else if (section === 'role-models') {
    _modelsLoaded = false;
    loadModels();
  } else if (section === 'mcp-tools') {
    _mcpServicesLoaded = false;
    loadMcpTools();
  } else if (section === 'network') {
    loadDomainStatus();
  }
  _refreshSettingsSectionState(section);
}

document.addEventListener('input', function(e) {
  const settingsView = document.getElementById('settings-view');
  if (!settingsView || settingsView.style.display !== 'flex') return;
  const panel = e.target.closest('.settings-panel');
  if (!panel) return;
  const section = panel.dataset.section;
  if (section === 'main-session') {
    const result = document.getElementById('conn-test-result');
    if (result) {
      result.textContent = '';
      result.innerHTML = '';
    }
  }
  _refreshSettingsSectionState(section);
});

document.addEventListener('change', function(e) {
  const settingsView = document.getElementById('settings-view');
  if (!settingsView || settingsView.style.display !== 'flex') return;
  const panel = e.target.closest('.settings-panel');
  if (!panel) return;
  _refreshSettingsSectionState(panel.dataset.section);
});

document.addEventListener('click', function(e) {
  const settingsView = document.getElementById('settings-view');
  if (!settingsView || settingsView.style.display !== 'flex') return;
  if (e.target.closest('#model-table .mcp-chip')) {
    setTimeout(function() { _refreshSettingsSectionState('role-models'); }, 0);
  }
});

document.addEventListener('keydown', function(e) {
  const settingsView = document.getElementById('settings-view');
  if (!settingsView || settingsView.style.display !== 'flex') return;
  const key = (e.key || '').toLowerCase();
  if ((e.ctrlKey || e.metaKey) && key === 's') {
    e.preventDefault();
    _saveCurrentSettingsSection();
  }
});

// ======================== Model Config ========================
let _modelsLoaded = false;
let _modelsData = null;
let _mcpData = null;  // {mcps: [{name}], role_mcps: {role: [names]}}
let _mcpServicesLoaded = false;
let _mcpServicesData = null;
let _lastChromeStatus = null;
async function loadModels() {
  const table = document.getElementById('model-table');
  try {
    const [r1, r2] = await Promise.all([
      fetch('/vizo/console/api/settings/models'),
      fetch('/vizo/console/api/settings/mcp-permissions')
    ]);
    if (r1.status === 401) { showToast('请先登录', 'error'); return; }
    _modelsData = await r1.json();
    _mcpData = r2.ok ? await r2.json() : null;
    _modelsLoaded = true;
    _renderModelsTable();
    _refreshSettingsSectionState('role-models');
  } catch(e) {
    table.innerHTML = '<div style="color:var(--danger);padding:1rem">加载失败，请刷新重试</div>';
  }
}

function _renderModelsTable() {
  const table = document.getElementById('model-table');
  const hasMcps = _mcpData && _mcpData.mcps && _mcpData.mcps.length > 0;
  const disabledMcps = hasMcps ? _mcpData.mcps.filter(function(mcp) { return !mcp.enabled; }) : [];
  const disabledNote = document.getElementById('role-mcp-disabled-note');
  if (disabledNote) {
    if (disabledMcps.length) {
      disabledNote.style.display = 'block';
      disabledNote.textContent = '部分 MCP 已在“MCP 工具”页中关闭，因此这里暂不可用：' + disabledMcps.map(function(item) {
        return item.display_name || item.name;
      }).join('、');
    } else {
      disabledNote.style.display = 'none';
      disabledNote.textContent = '';
    }
  }
  let html = '';
  for (const role of _modelsData.roles) {
    html += '<div class="model-row">';
    html += '<span class="model-role">' + role.display_name + '<span class="model-default">(默认: ' + _getModelDisplay(role.default_model) + ')</span></span>';
    html += '<select class="model-select" data-role="' + role.role + '">';
    for (const m of _modelsData.available_models) {
      const sel = m.id === role.current_model ? ' selected' : '';
      html += '<option value="' + m.id + '"' + sel + '>' + m.display + '</option>';
    }
    html += '</select>';
    html += '<select class="reasoning-select model-select" data-role="' + role.role + '" title="思考深度">';
    const efforts = _modelsData.available_reasoning_efforts || [];
    for (const effort of efforts) {
      const sel = effort.id === (role.current_reasoning_effort || 'inherit') ? ' selected' : '';
      html += '<option value="' + escapeHtml(effort.id) + '"' + sel + '>' + escapeHtml(effort.display || effort.id) + '</option>';
    }
    html += '</select>';
    // API 来源指示标签
    const modelMeta = (_modelsData.available_models || []).find(x => x.id === role.current_model) || {};
    const isMainConnection = role.current_model === '__main_session__' ||
      String(role.current_model || '').indexOf('__main_model__:') === 0 ||
      modelMeta.source === 'main_session';
    if (isMainConnection) {
      html += '<span class="api-source-label default">主会话连接</span>';
    } else {
      html += '<span class="api-source-label external">独立连接</span>';
    }
    html += '</div>';
    if (hasMcps) {
      const roleMcps = (_mcpData.role_mcps[role.role] || []);
      html += '<div class="mcp-chips-row" data-role="' + role.role + '">';
      html += '<span class="mcp-chips-label">MCP</span>';
      for (const mcp of _mcpData.mcps) {
        const active = roleMcps.includes(mcp.name) ? ' active' : '';
        const disabled = mcp.enabled ? '' : ' disabled';
        const title = mcp.enabled ? (mcp.status_text || '可用') : '已在 MCP 工具中关闭';
        const label = mcp.display_name || mcp.name;
        const click = mcp.enabled ? ' onclick="toggleRoleMcpChip(this)"' : '';
        html += '<span class="mcp-chip' + active + disabled + '" data-mcp="' + mcp.name + '" data-enabled="' + (mcp.enabled ? '1' : '0') + '" title="' + escapeHtml(title) + '"' + click + '>' + escapeHtml(label) + '</span>';
      }
      html += '</div>';
    }
  }
  table.innerHTML = html;
}

function _getModelDisplay(modelId) {
  if (!_modelsData) return modelId;
  const m = _modelsData.available_models.find(x => x.id === modelId);
  if (!m) return modelId;
  if (/^(opus|sonnet|haiku)（.+）$/.test(m.display)) return m.display;
  return m.display.split('（')[0].trim();
}

function toggleRoleMcpChip(el) {
  if (!el || el.dataset.enabled !== '1') return;
  el.classList.toggle('active');
}

async function saveModels() {
  if (!_isRoleModelsDirty()) {
    _refreshSettingsSectionState('role-models');
    showToast('角色配置没有需要保存的改动', 'success');
    return;
  }
  _setSettingsActionLoading('role-models', true);
  const selects = document.querySelectorAll('#model-table .model-select');
  const overrides = {};
  selects.forEach(s => {
    if (!s.classList.contains('reasoning-select')) overrides[s.dataset.role] = s.value;
  });
  const role_reasoning_efforts = {};
  document.querySelectorAll('#model-table .reasoning-select').forEach(s => {
    role_reasoning_efforts[s.dataset.role] = s.value;
  });

  // 收集 MCP 权限（若有 chips 行）
  const role_mcps = {};
  document.querySelectorAll('#model-table .mcp-chips-row').forEach(row => {
    const role = row.dataset.role;
    role_mcps[role] = [];
    row.querySelectorAll('.mcp-chip.active').forEach(chip => {
      role_mcps[role].push(chip.dataset.mcp);
    });
  });

  try {
    const reqs = [fetch('/vizo/console/api/settings/models', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({model_overrides: overrides, role_reasoning_efforts})
    })];
    if (Object.keys(role_mcps).length > 0) {
      reqs.push(fetch('/vizo/console/api/settings/mcp-permissions', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({role_mcps})
      }));
    }
    const results = await Promise.all(reqs);
    const d = await results[0].json();
    if (d.success) {
      showToast('角色配置已保存，下个任务生效', 'success');
      await loadModels();
    } else {
      showToast(d.error || '保存失败', 'error');
    }
  } catch(e) { showToast('网络错误', 'error'); }
  finally { _setSettingsActionLoading('role-models', false); }
}

function resetSettingsModels() {
  if (!_modelsData || !_modelsData.roles) {
    showToast('角色配置尚未加载完成', 'error');
    return;
  }
  _modelsData.roles.forEach(function(role) {
    const select = document.querySelector('#model-table .model-select[data-role="' + role.role + '"]');
    if (select) select.value = role.default_model;
    const effortSelect = document.querySelector('#model-table .reasoning-select[data-role="' + role.role + '"]');
    if (effortSelect) effortSelect.value = role.default_reasoning_effort || 'inherit';
  });
  _refreshSettingsSectionState('role-models');
  showToast('已恢复默认值，点击“保存角色配置”生效', 'info');
}

// ======================== MCP Tools ========================
function _getRoleDisplayName(roleName) {
  if (_modelsData && _modelsData.roles) {
    const role = _modelsData.roles.find(function(item) { return item.role === roleName; });
    if (role) return role.display_name;
  }
  return roleName;
}

function _findMcpService(name) {
  if (!_mcpServicesData || !_mcpServicesData.services) return null;
  return _mcpServicesData.services.find(function(item) { return item.name === name; }) || null;
}

function _mcpPopupId(name) {
  return 'mcp-status-popup-' + String(name || '').replace(/[^a-zA-Z0-9_-]/g, '_');
}

function _closeMcpStatusPopups(exceptId) {
  document.querySelectorAll('.mcp-service-status-popup.show').forEach(function(el) {
    if (!exceptId || el.id !== exceptId) el.classList.remove('show');
  });
}

function toggleMcpServiceStatusPopup(event, popupId) {
  if (event) event.stopPropagation();
  const popup = document.getElementById(popupId);
  if (!popup) return;
  const shouldShow = !popup.classList.contains('show');
  _closeMcpStatusPopups(shouldShow ? popupId : '');
  popup.classList.toggle('show', shouldShow);
}

document.addEventListener('click', function(e) {
  if (!e.target.closest('.mcp-service-status-wrap')) _closeMcpStatusPopups('');
});

function _getMcpDraftEnabled(service) {
  return !!service.enabled;
}

function _getMcpDraftState(service) {
  const enabled = _getMcpDraftEnabled(service);
  if (!enabled) return {status: 'disabled', text: '已关闭'};
  if (!service.installed) return {status: service.status, text: service.status_text};
  if (service.status === 'disabled') {
    if (service.name === 'mcp-chrome') return {status: 'waiting', text: '等待连接'};
    if (service.name === 'serena') return {status: 'ready', text: '可用'};
    return {status: 'ready', text: '已开启'};
  }
  return {status: service.status, text: service.status_text};
}

function _renderMcpStatusPopup(service, state) {
  let html = '<div class="mcp-service-status-popup-title">' + escapeHtml(service.display_name) + ' · ' + escapeHtml(state.text) + '</div>';
  html += '<div class="mcp-service-status-popup-body">';
  if (service.helper_text) {
    html += '<div style="margin-bottom:0.45rem">' + escapeHtml(service.helper_text) + '</div>';
  }
  html += '<div class="info-row"><span>角色使用</span><span class="info-val">' + escapeHtml(String(service.roles_using_count || 0)) + ' 个</span></div>';
  if (service.name === 'mcp-chrome') {
    const browser = service.browser_info && service.browser_info.browser ? service.browser_info.browser : '-';
    const tools = service.cached_tools || 0;
    const uptime = service.uptime_seconds || 0;
    const uptimeText = uptime > 3600 ? Math.floor(uptime/3600)+'h '+Math.floor((uptime%3600)/60)+'m' : Math.floor(uptime/60)+'m';
    html += '<div class="info-row"><span>浏览器</span><span class="info-val">' + escapeHtml(browser) + '</span></div>';
    html += '<div class="info-row"><span>可用能力</span><span class="info-val">' + escapeHtml(String(tools)) + ' 个</span></div>';
    html += '<div class="info-row"><span>连接时长</span><span class="info-val">' + escapeHtml(uptimeText) + '</span></div>';
    if (_getMcpDraftEnabled(service)) {
      html += '<a class="connect-link" href="/vizo/chrome/connect" target="_blank">打开连接引导页 →</a>';
    } else {
      html += '<a class="connect-link" href="#" onclick="event.preventDefault(); openSettings(\'mcp-tools\');">先到本页重新开启 →</a>';
    }
  } else if (service.action_hint) {
    html += '<div style="margin-top:0.45rem">' + escapeHtml(service.action_hint) + '</div>';
  }
  html += '</div>';
  return html;
}

function _renderMcpServiceCard(service) {
  const state = _getMcpDraftState(service);
  const enabled = _getMcpDraftEnabled(service);
  const locked = !!service.is_locked;
  const roleText = service.roles_using_count > 0
    ? '当前有 ' + service.roles_using_count + ' 个角色正在使用'
    : '当前没有角色使用';
  const roleList = service.roles_using_count > 0
    ? service.roles_using.map(_getRoleDisplayName).join('、')
    : '可到“角色配置”页分配给角色。';
  const toggleDisabled = !service.installed || locked || service.can_toggle === false;
  const toggleText = locked ? '固定开启' : (enabled ? '关闭' : '开启');
  const popupId = _mcpPopupId(service.name);
  let html = '<div class="mcp-service-card">';
  html += '<div class="mcp-service-head">';
  html += '<div><div class="mcp-service-title">' + escapeHtml(service.display_name) + '</div>';
  html += '<div class="mcp-service-desc">' + escapeHtml(service.description || '') + '</div></div>';
  html += '<div class="mcp-service-status-wrap">';
  html += '<button class="mcp-service-badge mcp-service-status-trigger ' + escapeHtml(state.status) + '" type="button" onclick="toggleMcpServiceStatusPopup(event, \'' + escapeHtml(popupId) + '\')">' + escapeHtml(state.text) + '</button>';
  html += '<div class="mcp-service-status-popup" id="' + escapeHtml(popupId) + '">' + _renderMcpStatusPopup(service, state) + '</div>';
  html += '</div>';
  html += '</div>';
  html += '<div class="mcp-service-meta">' + escapeHtml(roleText) + '</div>';
  html += '<div class="mcp-service-roles">' + escapeHtml(roleList) + '</div>';
  if (service.helper_text || service.action_hint) {
  html += '<div class="mcp-service-helper">';
    if (service.helper_text) html += escapeHtml(service.helper_text);
    if (service.action_hint) html += (service.helper_text ? ' ' : '') + escapeHtml(service.action_hint);
    html += '</div>';
  }
  html += '<div class="mcp-service-actions">';
  html += '<button class="settings-btn-secondary mcp-service-toggle" data-name="' + escapeHtml(service.name) + '" data-enabled="' + (enabled ? '1' : '0') + '" data-orig-enabled="' + (service.enabled ? '1' : '0') + '"' + (toggleDisabled ? ' disabled' : '') + ' onclick="toggleMcpServiceDraft(this.dataset.name)">' + escapeHtml(toggleText) + '</button>';
  if (service.can_repair && (state.status === 'missing' || state.status === 'error')) {
    html += '<button class="settings-btn-secondary" data-name="' + escapeHtml(service.name) + '" onclick="repairMcpService(this.dataset.name)">' + (service.name === 'serena' ? '一键修复' : '修复服务') + '</button>';
  }
  if (service.auxiliary_action === 'open_guide') {
    html += '<button class="settings-btn-secondary" onclick="openChromeMcpGuide()">' + escapeHtml(service.auxiliary_label || '打开连接引导') + '</button>';
  }
  html += '</div></div>';
  return html;
}

function renderMcpTools() {
  const overviewEl = document.getElementById('mcp-tools-overview');
  const listEl = document.getElementById('mcp-service-list');
  if (!_mcpServicesData) {
    if (listEl) listEl.innerHTML = '<div style="text-align:center;color:var(--text-muted);padding:1rem">加载失败，请刷新重试</div>';
    return;
  }

  const summary = _mcpServicesData.summary || {};
  const missingLocked = summary.locked_missing || summary.core_missing || [];
  if (overviewEl) {
    let cls = 'settings-note info';
    let text = '当前已发现 ' + (summary.installed_count || 0) + ' 个工具，已开启 ' + (summary.enabled_count || 0) + ' 个。先在这里开启，再到“角色配置”里分配给角色。';
    if (missingLocked.includes('Serena')) {
      cls = 'settings-note error';
      text = '未检测到 Serena。系统记忆依赖它，建议立即点击“一键修复”。';
    } else if (missingLocked.includes('Vizo Router')) {
      cls = 'settings-note error';
      text = '未检测到 Vizo Router。主会话语义路由和交互设计流程依赖它，建议立即点击“修复服务”。';
    }
    overviewEl.className = cls;
    overviewEl.textContent = text;
  }

  if (listEl) {
    listEl.innerHTML = (_mcpServicesData.services || []).map(_renderMcpServiceCard).join('') ||
      '<div style="color:var(--text-muted)">暂未安装 MCP 工具</div>';
  }
}

function _syncChromeStatusIntoMcpTools(status, shouldRender) {
  if (!_mcpServicesData || !status) return;
  ['services'].forEach(function(key) {
    const list = _mcpServicesData[key];
    if (!Array.isArray(list)) return;
    _mcpServicesData[key] = list.map(function(item) {
      if (item.name !== 'mcp-chrome') return item;
      const next = Object.assign({}, item);
      if (typeof status.service_enabled === 'boolean') next.enabled = status.service_enabled;
      if (typeof status.service_installed === 'boolean') next.installed = status.service_installed;
      next.browser_info = status.browser_info || null;
      next.cached_tools = Number(status.cached_tools || 0);
      next.uptime_seconds = Number(status.uptime_seconds || 0);

      if (!next.installed) {
        next.status = 'missing';
        next.status_text = '未安装';
        next.helper_text = '未检测到 Chrome MCP 配置。';
        next.action_hint = '请点击“修复服务”恢复内置配置，再打开连接引导。';
      } else if (!next.enabled) {
        next.status = 'disabled';
        next.status_text = '已关闭';
        next.helper_text = '';
        next.action_hint = '';
      } else if (status.service_status === 'error') {
        next.status = 'error';
        next.status_text = status.service_status_text || '异常';
      } else if (status.chrome_connected) {
        next.status = 'connected';
        next.status_text = '已连接';
        next.helper_text = '浏览器已连接，可直接用于截图、点击、表单填写等操作。';
        next.action_hint = '';
      } else {
        next.status = 'waiting';
        next.status_text = status.service_status_text || '等待连接';
        next.helper_text = '浏览器尚未连接，前端测试能力暂不可用。';
        next.action_hint = '请打开连接引导，在浏览器中完成连接。';
      }
      return next;
    });
  });
  if (shouldRender !== false && _currentSettingsSection === 'mcp-tools') {
    renderMcpTools();
  }
}

async function loadMcpTools() {
  const listEl = document.getElementById('mcp-service-list');
  try {
    const res = await fetch('/vizo/console/api/settings/mcp-services');
    if (res.status === 401) { showToast('请先登录', 'error'); return; }
    _mcpServicesData = await res.json();
    if (_lastChromeStatus) _syncChromeStatusIntoMcpTools(_lastChromeStatus, false);
    _mcpServicesLoaded = true;
    renderMcpTools();
    _refreshSettingsSectionState('mcp-tools');
  } catch (e) {
    if (listEl) listEl.innerHTML = '<div style="color:var(--danger);padding:1rem">加载失败，请刷新重试</div>';
  }
}

async function toggleMcpServiceDraft(name) {
  const service = _findMcpService(name);
  const btn = document.querySelector('.mcp-service-toggle[data-name="' + name + '"]');
  if (!service || !btn || !service.installed) return;
  if (service.is_locked) {
    showToast(service.display_name + ' 是系统内置 MCP，保持开启', 'info');
    return;
  }
  if (service.can_toggle === false) {
    showToast(service.display_name + ' 当前不可切换', 'warning');
    return;
  }
  const current = !!service.enabled;
  const next = !current;
  if (!next && service.roles_using_count > 0) {
    const ok = window.confirm('当前有 ' + service.roles_using_count + ' 个角色正在使用 ' + service.display_name + '。关闭后，这些角色将暂时无法使用该工具。确定继续吗？');
    if (!ok) return;
  }
  btn.disabled = true;
  const oldText = btn.textContent;
  btn.textContent = '处理中...';
  try {
    const res = await fetch('/vizo/console/api/settings/mcp-services/state', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({service_states: {[name]: next}})
    });
    const data = await res.json();
    if (data.success) {
      showToast(service.display_name + (next ? ' 已开启' : ' 已关闭'), 'success');
      _modelsLoaded = false;
      _mcpData = null;
      await loadMcpTools();
      updateChromeStatus();
    } else {
      showToast(data.error || '设置失败', 'error');
    }
  } catch (e) {
    showToast('网络错误', 'error');
  } finally {
    const currentBtn = document.querySelector('.mcp-service-toggle[data-name="' + name + '"]');
    if (currentBtn) {
      currentBtn.disabled = false;
      currentBtn.textContent = currentBtn.dataset.enabled === '1' ? '关闭' : '开启';
    } else {
      btn.disabled = false;
      btn.textContent = oldText;
    }
  }
}

async function repairMcpService(name) {
  try {
    const res = await fetch('/vizo/console/api/settings/mcp-services/repair', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name})
    });
    const data = await res.json();
    if (data.success) {
      showToast(data.message || '修复成功', 'success');
      _modelsLoaded = false;
      _mcpData = null;
      await loadMcpTools();
      updateChromeStatus();
      if (name === 'mcp-chrome') openChromeMcpGuide();
    } else {
      showToast(data.error || '修复失败', 'error');
    }
  } catch (e) {
    showToast('网络错误', 'error');
  }
}

function openChromeMcpGuide() {
  window.open('/vizo/chrome/connect', '_blank');
}

async function importMcpConfig() {
  const raw = document.getElementById('mcp-import-json')?.value || '';
  if (!raw.trim()) {
    showToast('请先粘贴配置 JSON', 'error');
    return;
  }
  try {
    const res = await fetch('/vizo/console/api/settings/mcp-services/import', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({raw_json: raw})
    });
    const data = await res.json();
    if (data.success) {
      showToast(data.message || '导入成功', 'success');
      document.getElementById('mcp-import-json').value = '';
      _modelsLoaded = false;
      _mcpData = null;
      await loadMcpTools();
    } else {
      showToast(data.error || '导入失败', 'error');
    }
  } catch (e) {
    showToast('网络错误', 'error');
  }
}

function _parseManualEnv(text) {
  const env = {};
  (text || '').split(/\r?\n/).forEach(function(line) {
    const raw = line.trim();
    if (!raw) return;
    const idx = raw.indexOf('=');
    if (idx <= 0) return;
    env[raw.slice(0, idx).trim()] = raw.slice(idx + 1).trim();
  });
  return env;
}

async function saveManualMcpService() {
  const name = document.getElementById('mcp-manual-name')?.value.trim() || '';
  const command = document.getElementById('mcp-manual-command')?.value.trim() || '';
  const args = (document.getElementById('mcp-manual-args')?.value || '').split(/\r?\n/).map(function(line) {
    return line.trim();
  }).filter(Boolean);
  const env = _parseManualEnv(document.getElementById('mcp-manual-env')?.value || '');
  if (!name) { showToast('请填写工具名称', 'error'); return; }
  if (!command) { showToast('请填写命令', 'error'); return; }
  try {
    const res = await fetch('/vizo/console/api/settings/mcp-services/manual', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name, command: command, args: args, env: env})
    });
    const data = await res.json();
    if (data.success) {
      showToast(data.message || '添加成功', 'success');
      ['mcp-manual-name', 'mcp-manual-command', 'mcp-manual-args', 'mcp-manual-env'].forEach(function(id) {
        const el = document.getElementById(id);
        if (el) el.value = '';
      });
      _modelsLoaded = false;
      _mcpData = null;
      await loadMcpTools();
    } else {
      showToast(data.error || '添加失败', 'error');
    }
  } catch (e) {
    showToast('网络错误', 'error');
  }
}

// ======================== API Key Config ========================
let _mainProviderOptions = [];
let _currentMainConnectionId = 'main_session';
let _connectionAuthModalState = {
  connectionId: '',
  timer: null,
  payload: null
};
const MAIN_PROVIDER_GROUP_OPTIONS = [
  {id: 'claude', display: 'Claude'},
  {id: 'anthropic_compatible', display: 'Anthropic 兼容接口'},
  {id: 'openai_compatible', display: 'OpenAPI'}
];
const MAIN_PROVIDER_TEMPLATE_OPTIONS = {
  anthropic_compatible: [
    {id: 'gateway', display: '手动填写映射'},
    {id: 'glm', display: 'GLM'},
    {id: 'deepseek', display: 'DeepSeek'},
    {id: 'minimax', display: 'MiniMax'},
    {id: 'mimo', display: 'MiMo'}
  ]
};
async function loadApiKeyStatus() {
  const el = document.getElementById('apikey-status');
  try {
    const r = await fetch('/vizo/console/api/settings/apikey');
    if (r.status === 401) { el.textContent = '请先登录'; return; }
    const d = await r.json();
    _currentMainConnectionId = d.connection_id || 'main_session';
    const apikeyEl = document.getElementById('new-apikey');
    if (d.configured) {
      el.innerHTML = '<span style="color:var(--green)">✓ API Key 已配置</span>';
      apikeyEl.value = d.masked || '';
      apikeyEl.dataset.masked = d.masked || '';
    } else {
      el.innerHTML = '<span style="color:var(--danger)">✗ API Key 未配置</span>';
      apikeyEl.value = '';
      apikeyEl.dataset.masked = '';
    }
    const baseUrlEl = document.getElementById('new-baseurl');
    baseUrlEl.value = d.base_url || '';
    baseUrlEl.dataset.orig = d.base_url || '';
    _mainProviderOptions = d.provider_options || [];
    const uiState = mapInternalProviderToUiState(d.routing_provider_id || 'anthropic');
    renderMainProviderOptions(uiState.groupId || 'claude');
    const providerEl = document.getElementById('new-provider');
    if (providerEl) {
      providerEl.value = uiState.groupId || 'claude';
      providerEl.dataset.orig = uiState.groupId || 'claude';
    }
    const authModeEl = document.getElementById('new-openai-auth-mode');
    if (authModeEl) {
      authModeEl.value = d.auth_mode || 'api_key';
      authModeEl.dataset.orig = d.auth_mode || 'api_key';
    }
    renderMainProviderTemplateOptions(uiState.groupId || 'claude', uiState.templateId || '');
    const templateEl = document.getElementById('new-provider-template');
    if (templateEl) {
      templateEl.value = uiState.templateId || '';
      templateEl.dataset.orig = uiState.templateId || '';
    }
    const fieldMap = {
      'new-default-opus-model': d.default_opus_model || '',
      'new-default-sonnet-model': d.default_sonnet_model || '',
      'new-default-haiku-model': d.default_haiku_model || ''
    };
    Object.keys(fieldMap).forEach(function(inputId) {
      var input = document.getElementById(inputId);
      if (!input) return;
      input.value = fieldMap[inputId];
      input.dataset.orig = fieldMap[inputId];
    });
    updateMainProviderFields(false);
    updateMainOpenAIAuthUi(d);
  } catch(e) { el.textContent = '加载失败'; }
  _refreshSettingsSectionState('main-session');
}

function renderMainOpenAIAuthPanel(data) {
  const panel = document.getElementById('main-openai-auth-panel');
  if (!panel) return;
  const authMode = data?.auth_mode || getSelectedMainOpenAIAuthMode();
  const providerGroupId = document.getElementById('new-provider')?.value || 'claude';
  if (providerGroupId !== 'openai_compatible' || authMode !== 'account_login') {
    panel.style.display = 'none';
    panel.innerHTML = '';
    return;
  }
  panel.style.display = '';
  const authData = Object.assign({auth_mode: 'account_login'}, data || {});
  const meta = getConnectionAuthStatusMeta(authData);
  const connectionId = data?.connection_id || _currentMainConnectionId || 'main_session';
  const canManage = !!data && data.auth_mode === 'account_login' && !!connectionId && connectionId !== 'main_session';
  let html = '<div class="settings-guide-title"><strong>OpenAI 账号登录</strong><span class="settings-guide-badge ok">连接级隔离</span></div>';
  html += '<div class="auth-status-row">';
  html += '<span class="auth-status-badge ' + escapeHtml(meta.stateClass) + '">' + escapeHtml(meta.label) + '</span>';
  if (data?.auth_account_label) {
    html += '<span style="color:var(--text-muted)">账号：' + escapeHtml(data.auth_account_label) + '</span>';
  }
  html += '</div>';
  if (data?.auth_last_verified_at) {
    html += '<div style="color:var(--text-muted)">最近验证：' + escapeHtml(data.auth_last_verified_at) + '</div>';
  }
  if (canManage) {
    html += '<div style="margin-top:0.7rem;display:flex;gap:0.5rem;flex-wrap:wrap">';
    html += '<button class="settings-btn-secondary" onclick="openConnectionAuthModalById(\'' + escapeHtml(connectionId) + '\')">管理登录</button>';
    html += '<button class="settings-btn-secondary" onclick="refreshConnectionAuthStatusInline(\'' + escapeHtml(connectionId) + '\')">刷新状态</button>';
    html += '</div>';
  } else {
    html += '<div style="margin-top:0.65rem;color:var(--text-muted)">账号登录连接需要先保存为快捷连接；只有保存后的这条连接本身已切到“OpenAI 账号登录”模式，才能在这里或连接卡片里继续授权。</div>';
    html += '<div style="margin-top:0.7rem;display:flex;gap:0.5rem;flex-wrap:wrap">';
    html += '<button class="settings-btn-secondary" onclick="saveAccountLoginConnectionAndOpenModal()">保存为快捷连接并登录</button>';
    html += '</div>';
  }
  panel.innerHTML = html;
}

function updateMainOpenAIAuthUi(data) {
  const providerGroupId = document.getElementById('new-provider')?.value || 'claude';
  const authModeField = document.getElementById('main-openai-auth-mode-field');
  const baseUrlField = document.getElementById('main-base-url-field');
  const baseUrlEl = document.getElementById('new-baseurl');
  const apiKeyField = document.getElementById('main-api-key-field');
  const authModeEl = document.getElementById('new-openai-auth-mode');
  const statusEl = document.getElementById('apikey-status');
  if (authModeField) authModeField.style.display = providerGroupId === 'openai_compatible' ? '' : 'none';
  if (providerGroupId === 'openai_compatible' && authModeEl && !authModeEl.value) {
    authModeEl.value = (data && data.auth_mode) || 'api_key';
  }
  const authMode = providerGroupId === 'openai_compatible' ? getSelectedMainOpenAIAuthMode() : '';
  const usingAccountLogin = providerGroupId === 'openai_compatible' && authMode === 'account_login';
  const officialOpenAIBaseUrl = getMainProviderOption('openai')?.default_base_url || 'https://api.openai.com/v1';
  if (baseUrlField) baseUrlField.style.display = usingAccountLogin ? 'none' : '';
  if (baseUrlEl) {
    if (usingAccountLogin) {
      const currentValue = (baseUrlEl.value || '').trim();
      if (currentValue && currentValue !== officialOpenAIBaseUrl && !baseUrlEl.dataset.accountLoginPrev) {
        baseUrlEl.dataset.accountLoginPrev = currentValue;
      }
      baseUrlEl.value = officialOpenAIBaseUrl;
      baseUrlEl.disabled = true;
    } else {
      if (baseUrlEl.disabled) {
        const previousValue = baseUrlEl.dataset.accountLoginPrev || '';
        if (previousValue && (baseUrlEl.value || '').trim() === officialOpenAIBaseUrl) {
          baseUrlEl.value = previousValue;
        }
        delete baseUrlEl.dataset.accountLoginPrev;
      }
      baseUrlEl.disabled = false;
    }
  }
  if (apiKeyField) apiKeyField.style.display = authMode === 'account_login' ? 'none' : '';
  if (statusEl && providerGroupId === 'openai_compatible' && authMode === 'account_login') {
    const meta = getConnectionAuthStatusMeta(Object.assign({auth_mode: 'account_login'}, data || {}));
    statusEl.innerHTML = '<span class="auth-status-badge ' + escapeHtml(meta.stateClass) + '">' + escapeHtml(meta.label) + '</span>'
      + '<span style="margin-left:0.45rem;color:var(--text-muted)">OpenAI 账号登录采用连接级隔离，固定使用官方 OpenAI 入口，不会复用当前 CLI 对话的登录态。</span>';
  } else if (statusEl) {
    const apikeyEl = document.getElementById('new-apikey');
    const hasMasked = !!(apikeyEl?.dataset.masked || '');
    statusEl.innerHTML = hasMasked
      ? '<span style="color:var(--green)">✓ API Key 已配置</span>'
      : '<span style="color:var(--danger)">✗ API Key 未配置</span>';
  }
  renderMainOpenAIAuthPanel(data);
}

function getMainProviderOption(providerId) {
  return (_mainProviderOptions || []).find(function(x) { return x.id === providerId; }) || null;
}

function mapInternalProviderToUiState(providerId) {
  if (providerId === 'openai') {
    return {groupId: 'openai_compatible', templateId: ''};
  }
  if (['glm', 'deepseek', 'minimax', 'mimo', 'gateway'].includes(providerId)) {
    return {groupId: 'anthropic_compatible', templateId: providerId};
  }
  return {groupId: 'claude', templateId: ''};
}

function getSelectedMainProviderTemplateId() {
  return document.getElementById('new-provider-template')?.value || '';
}

function getSelectedMainProviderSaveId() {
  const groupId = document.getElementById('new-provider')?.value || 'claude';
  if (groupId === 'anthropic_compatible') {
    return getSelectedMainProviderTemplateId() || 'gateway';
  }
  if (groupId === 'openai_compatible') {
    return 'openai';
  }
  return 'anthropic';
}

function isAnthropicOfficialBaseUrl(baseUrl) {
  try {
    const host = (new URL(baseUrl || 'https://api.anthropic.com').hostname || '').toLowerCase();
    return host === 'api.anthropic.com' || host.endsWith('.anthropic.com');
  } catch(e) {
    return false;
  }
}

function getEffectiveMainProviderId() {
  const selected = document.getElementById('new-provider')?.value || 'claude';
  const baseUrl = document.getElementById('new-baseurl')?.value.trim() || '';
  if (selected === 'claude') {
    if (baseUrl && !isAnthropicOfficialBaseUrl(baseUrl)) {
      return 'custom';
    }
    return 'anthropic';
  }
  if (selected === 'anthropic_compatible') {
    return getSelectedMainProviderTemplateId() || 'gateway';
  }
  if (selected === 'openai_compatible') {
    return 'openai';
  }
  if (selected === 'anthropic' && baseUrl && !isAnthropicOfficialBaseUrl(baseUrl)) {
    return 'custom';
  }
  return selected;
}

function getMainProviderLabel(providerId) {
  const provider = getMainProviderOption(providerId);
  if (provider) return provider.display;
  if (providerId === 'custom') return 'Claude 中转平台';
  return providerId || 'anthropic';
}

function getConnectionModeLabel(providerId, providerDisplay) {
  if (providerId === 'anthropic') return 'Claude';
  if (providerId === 'custom') return 'Claude 中转';
  if (providerId === 'openai') return 'OpenAPI';
  if (providerId === 'gateway') return 'Anthropic 兼容接口 · 手动映射';
  return 'Anthropic 兼容接口 · ' + (providerDisplay || getMainProviderLabel(providerId));
}

function getConnectionHostLabel(baseUrl) {
  try {
    const hostname = new URL(baseUrl || 'https://api.anthropic.com').hostname || 'anthropic';
    const parts = hostname.replace(/^(api|open)\./, '').split('.').filter(Boolean);
    return parts[0] || hostname;
  } catch(e) {
    return 'anthropic';
  }
}

function getConnectionNameSuggestion(baseUrl, providerId, providerDisplay) {
  const host = getConnectionHostLabel(baseUrl);
  const mode = getConnectionModeLabel(providerId, providerDisplay);
  if (providerId === 'anthropic') return host + ' · Claude 官方';
  if (providerId === 'custom') return host + ' · Claude 中转';
  if (providerId === 'openai') return host + ' · OpenAPI';
  if (providerId === 'gateway') return host + ' · Anthropic 兼容接口';
  return host + ' · ' + mode;
}

function getSelectedMainOpenAIAuthMode() {
  return document.getElementById('new-openai-auth-mode')?.value || 'api_key';
}

function isMainOpenAIAccountLogin() {
  return (document.getElementById('new-provider')?.value || 'claude') === 'openai_compatible'
    && getSelectedMainOpenAIAuthMode() === 'account_login';
}

function getConnectionAuthStatusMeta(connection) {
  const authMode = connection?.auth_mode || '';
  const authStatus = connection?.auth_status || '';
  if (authMode !== 'account_login') {
    return {
      stateClass: connection?.has_key ? 'ready' : 'missing',
      label: connection?.has_key ? 'API Key 已配置' : 'API Key 未配置'
    };
  }
  if (authStatus === 'ready') {
    return {stateClass: 'ready', label: '账号已登录'};
  }
  if (authStatus === 'unknown') {
    return {stateClass: 'waiting', label: '等待授权'};
  }
  if (authStatus === 'expired') {
    return {stateClass: 'error', label: '登录已失效'};
  }
  return {stateClass: 'missing', label: '尚未登录'};
}

function getMainBaseUrlValidationMessage(baseUrl) {
  const raw = (baseUrl || '').trim();
  if (!raw) return '';
  const providerId = document.getElementById('new-provider')?.value || 'claude';
  try {
    const path = (new URL(raw).pathname || '').replace(/\/+$/, '').toLowerCase();
    if (!path) return '';
    if (providerId === 'openai_compatible') {
      if (path.endsWith('/messages') || path.endsWith('/v1/messages') || path.endsWith('/count_tokens')) {
        return '这看起来是 Anthropic Messages 地址。OpenAPI 请填写 /v1 或平台提供的 OpenAPI 根路径，例如 /codex/v1。';
      }
      return '';
    }
    if (path.endsWith('/v1')) {
      return '看起来像 OpenAPI 接口入口（/v1 或 /codex/v1）。如果你要接 OpenAPI，请改选 OpenAPI；Claude / Anthropic 类型仍应填写 /claude、/anthropic 这类根路径。';
    }
    const badSuffixes = [
      '/messages',
      '/responses',
      '/completions',
      '/chat/completions',
      '/v1/messages',
      '/v1/responses',
      '/v1/completions',
      '/v1/chat/completions'
    ];
    if (badSuffixes.some(function(suffix) { return path.endsWith(suffix); })) {
      return '这看起来是 OpenAI / Codex 的具体 endpoint。无论主会话最终走 Codex direct 还是 Claude / Anthropic 兼容链路，都应该填写入口根路径，而不是 /messages、/responses 或 /chat/completions 这类具体地址。';
    }
  } catch(e) {}
  return '';
}

function getExternalBaseUrlValidationMessage(baseUrl, accessMode) {
  const raw = (baseUrl || '').trim();
  if (!raw) return '';
  try {
    const path = (new URL(raw).pathname || '').replace(/\/+$/, '').toLowerCase();
    if (!path) return '';
    if (accessMode === 'openai_compatible') {
      if (path.endsWith('/messages') || path.endsWith('/v1/messages') || path.endsWith('/count_tokens')) {
        return '这看起来是 Anthropic Messages 地址。OpenAI 兼容接口请填写 /v1 或平台提供的 OpenAPI 根路径，例如 /codex/v1。';
      }
      return '';
    }
    if (path.endsWith('/v1')) {
      return '看起来像 OpenAI / Codex 接口入口（/v1）。Anthropic 兼容接口请填写 /claude、/anthropic 这类根路径。';
    }
    const badSuffixes = [
      '/messages',
      '/responses',
      '/completions',
      '/chat/completions',
      '/v1/messages',
      '/v1/responses',
      '/v1/completions',
      '/v1/chat/completions'
    ];
    if (badSuffixes.some(function(suffix) { return path.endsWith(suffix); })) {
      return '这看起来是 OpenAI / Codex 的具体 endpoint。Anthropic 兼容接口请填写根路径，不要直接填 /responses 或 /chat/completions。';
    }
  } catch(e) {}
  return '';
}

function renderMainProviderOptions(selectedId) {
  const el = document.getElementById('new-provider');
  if (!el) return;
  let html = '';
  let hasSelected = false;
  MAIN_PROVIDER_GROUP_OPTIONS.forEach(function(opt) {
    const sel = opt.id === selectedId ? ' selected' : '';
    if (opt.id === selectedId) hasSelected = true;
    html += '<option value="' + escapeHtml(opt.id) + '"' + sel + '>' + escapeHtml(opt.display) + '</option>';
  });
  if (selectedId && !hasSelected) {
    html += '<option value="' + escapeHtml(selectedId) + '" selected>' + escapeHtml(selectedId) + '</option>';
  }
  el.innerHTML = html;
}

function renderMainProviderTemplateOptions(groupId, selectedTemplateId) {
  const wrap = document.getElementById('main-provider-template-field');
  const el = document.getElementById('new-provider-template');
  if (!wrap || !el) return;
  if (groupId !== 'anthropic_compatible') {
    wrap.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  wrap.style.display = '';
  const options = MAIN_PROVIDER_TEMPLATE_OPTIONS[groupId] || [];
  let html = '';
  let hasSelected = false;
  options.forEach(function(opt) {
    const sel = opt.id === selectedTemplateId ? ' selected' : '';
    if (opt.id === selectedTemplateId) hasSelected = true;
    html += '<option value="' + escapeHtml(opt.id) + '"' + sel + '>' + escapeHtml(opt.display) + '</option>';
  });
  if (!hasSelected && options.length) {
    selectedTemplateId = options[0].id;
  }
  el.innerHTML = html;
  if (selectedTemplateId) el.value = selectedTemplateId;
}

function getMainProviderModelDefaults() {
  return {
    default_opus_model: document.getElementById('new-default-opus-model')?.value.trim() || '',
    default_sonnet_model: document.getElementById('new-default-sonnet-model')?.value.trim() || '',
    default_haiku_model: document.getElementById('new-default-haiku-model')?.value.trim() || ''
  };
}

function updateMainProviderFields(usePresetDefaults) {
  const providerGroupId = document.getElementById('new-provider')?.value || 'claude';
  renderMainProviderTemplateOptions(providerGroupId, getSelectedMainProviderTemplateId());
  const providerId = getEffectiveMainProviderId();
  const provider = getMainProviderOption(providerId);
  const baseUrlInput = document.getElementById('new-baseurl');
  const wrap = document.getElementById('main-model-mapping-fields');
  const isGateway = providerId === 'gateway';
  const isOpenAI = providerGroupId === 'openai_compatible';
  const isClaude = providerGroupId === 'claude';
  if (wrap) wrap.style.display = (isClaude || isOpenAI) ? 'none' : '';
  if (baseUrlInput) {
    baseUrlInput.placeholder = isOpenAI
      ? '例如：https://api.openai.com/v1 或 https://code.newcli.com/codex/v1'
      : (isGateway
      ? '例如：https://code.newcli.com/claude（不能填写 /codex/v1）'
      : 'https://api.anthropic.com（留空使用当前接入默认地址）');
  }
  if (provider && usePresetDefaults) {
    const baseUrlEl = document.getElementById('new-baseurl');
    if (baseUrlEl) baseUrlEl.value = provider.default_base_url || '';
    const defaults = provider.default_models || {};
    const fieldMap = {
      'new-default-opus-model': defaults.opus || '',
      'new-default-sonnet-model': defaults.sonnet || '',
      'new-default-haiku-model': defaults.haiku || ''
    };
    Object.keys(fieldMap).forEach(function(inputId) {
      var input = document.getElementById(inputId);
      if (!input) return;
      input.value = (isClaude || isOpenAI) ? '' : fieldMap[inputId];
    });
  }
  updateMainOpenAIAuthUi();
}

function onMainProviderChange() {
  updateMainProviderFields(true);
  resetConnectionTestResult();
  _refreshSettingsSectionState('main-session');
}

function onMainProviderTemplateChange() {
  updateMainProviderFields(true);
  resetConnectionTestResult();
  _refreshSettingsSectionState('main-session');
}

function onMainOpenAIAuthModeChange() {
  updateMainProviderFields(false);
  resetConnectionTestResult();
  _refreshSettingsSectionState('main-session');
}

function resetConnectionTestResult() {
  const result = document.getElementById('conn-test-result');
  if (!result) return;
  result.textContent = '';
  result.innerHTML = '';
}

function buildMainProviderPayload() {
  const providerGroupId = document.getElementById('new-provider')?.value || 'claude';
  const providerId = getSelectedMainProviderSaveId();
  const authMode = providerGroupId === 'openai_compatible' ? getSelectedMainOpenAIAuthMode() : '';
  const defaultBaseUrl = getMainProviderOption(providerId)?.default_base_url || '';
  const baseUrl = providerGroupId === 'openai_compatible' && authMode === 'account_login'
    ? (getMainProviderOption('openai')?.default_base_url || 'https://api.openai.com/v1')
    : (document.getElementById('new-baseurl')?.value.trim() || '');
  const payload = {
    provider_id: providerId,
    ui_provider_group: providerGroupId,
    base_url: baseUrl
  };
  if (providerGroupId === 'anthropic_compatible') {
    Object.assign(payload, getMainProviderModelDefaults());
  }
  if (providerGroupId === 'openai_compatible') {
    payload.auth_mode = authMode;
    if (!payload.base_url && defaultBaseUrl) payload.base_url = defaultBaseUrl;
  }
  return payload;
}

function validateMainProviderPayload(showErrorToast) {
  const payload = buildMainProviderPayload();
  const providerGroupId = document.getElementById('new-provider')?.value || 'claude';
  const baseUrlWarning = getMainBaseUrlValidationMessage(payload.base_url);
  if (baseUrlWarning) {
    if (showErrorToast) showToast(baseUrlWarning, 'error');
    return null;
  }
  if ((providerGroupId === 'anthropic_compatible' || providerGroupId === 'openai_compatible') && !payload.base_url) {
    if (showErrorToast) showToast(providerGroupId === 'openai_compatible' ? 'OpenAPI 必须填写 Base URL' : 'Anthropic 兼容接口必须填写 Base URL', 'error');
    return null;
  }
  if (providerGroupId === 'anthropic_compatible') {
    const hasAny = !!(payload.default_opus_model || payload.default_sonnet_model || payload.default_haiku_model);
    if (!hasAny) {
      if (showErrorToast) showToast('Anthropic 兼容接口至少填写一个模型 ID 映射', 'error');
      return null;
    }
  }
  return payload;
}

// ======================== Connection Test & Profiles ========================
async function testConnection() {
  const btn = document.getElementById('test-conn-btn');
  const result = document.getElementById('conn-test-result');
  const apikeyInput = document.getElementById('new-apikey');
  const rawKey = apikeyInput.value.trim();
  const masked = apikeyInput.dataset.masked || '';
  const payload = validateMainProviderPayload(true);
  if (!payload) return;
  if (payload.auth_mode === 'account_login') {
    result.innerHTML = '<span style="color:var(--warning)">账号登录连接需先保存为快捷连接，再在连接卡片或登录窗口中完成授权。</span>';
    return;
  }
  // 如果用户没改过 key，发 masked 标记让后端用已存的
  const apiKey = (rawKey && rawKey !== masked) ? rawKey : '__USE_STORED__';
  btn.disabled = true;
  result.textContent = '测试中...';
  result.style.color = 'var(--text-muted)';
  try {
    const r = await fetch('/vizo/console/api/settings/test-connection', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(Object.assign(payload, {api_key: apiKey}))
    });
    const d = await r.json();
    if (d.ok) {
      result.innerHTML = '<span style="color:var(--green)">✓ 连接成功 (' + d.latency_ms + 'ms)</span>';
      if (d.validated_models && d.validated_models.length) {
        const lines = d.validated_models.map(function(item) {
          const tiers = (item.tiers || []).join(' / ');
          return tiers + ' → ' + item.model;
        });
        result.innerHTML += '<div style="margin-top:0.35rem;color:var(--text-muted)">已验证模型：' + escapeHtml(lines.join('；')) + '</div>';
      }
      if (d.warning) {
        result.innerHTML += '<div style="margin-top:0.35rem;color:var(--warning)">' + escapeHtml(d.warning) + '</div>';
      }
      if (d.warnings && d.warnings.length) {
        result.innerHTML += '<div style="margin-top:0.35rem;color:var(--warning)">' + escapeHtml(d.warnings.map(function(item) { return item.message || item.code || ''; }).join('；')) + '</div>';
      }
    } else {
      result.innerHTML = '<span style="color:var(--danger)">✗ ' + (d.error || '连接失败') + '</span>';
      if (d.blocking_issues && d.blocking_issues.length) {
        result.innerHTML += '<div style="margin-top:0.35rem;color:var(--danger)">' + escapeHtml(d.blocking_issues.map(function(item) { return item.message || item.code || ''; }).join('；')) + '</div>';
      }
    }
  } catch(e) {
    result.innerHTML = '<span style="color:var(--danger)">✗ 网络错误</span>';
  }
  btn.disabled = false;
}

let _savedConnections = [];
async function loadSavedConnections() {
  try {
    const r = await fetch('/vizo/console/api/settings/connections');
    if (!r.ok) return;
    const d = await r.json();
    _savedConnections = d.connections || [];
    _renderConnectionCards();
  } catch(e) {}
}

async function _applySessionSyncInfo(syncInfo) {
  const newSessionId = syncInfo && syncInfo.new_session_id;
  if (!newSessionId) return false;
  if (S.activeSessionId !== newSessionId) {
    switchSession(newSessionId);
  }
  await loadSessions();
  return true;
}

function _renderConnectionCards() {
  const container = document.getElementById('saved-connections');
  const cards = document.getElementById('conn-cards');
  if (!_savedConnections.length) { container.style.display = 'none'; return; }
  container.style.display = '';
  let html = '';
  for (let i = 0; i < _savedConnections.length; i++) {
    const c = _savedConnections[i];
    const active = c.active ? ' active' : '';
    const host = c.host_label || getConnectionHostLabel(c.base_url);
    const providerDisplay = c.provider_display || host;
    const modeDisplay = c.mode_display || getConnectionModeLabel(c.provider_id, providerDisplay);
    const keyIcon = c.auth_mode === 'account_login' ? ' 👤' : (c.has_key ? ' 🔑' : ' ⚠️');
    const activeTag = c.active ? '<span class="conn-card-state">当前连接</span>' : '';
    const authMeta = getConnectionAuthStatusMeta(c);
    const mainDiag = (c.runtime_diagnostics || {}).main_session || {};
    const route = mainDiag.route_decision || {};
    const runtimeBadge = route.label || route.runtime_family || '';
    const compatCandidate = (c.runtime_candidates || []).find(function(item) {
      return item.category === 'compatibility';
    });
    const blockingText = (mainDiag.blocking_issues || []).map(function(item) { return item.message || item.code || ''; }).join('；');
    const title = (c.base_url || 'https://api.anthropic.com')
      + '\\n模式：' + modeDisplay
      + (c.routing_summary ? '\\n' + c.routing_summary : '')
      + (c.runtime_summary ? '\\n主路径：' + c.runtime_summary : '')
      + (blockingText ? '\\n阻断：' + blockingText : '')
      + (c.auth_mode === 'account_login'
        ? ('\\n账号登录：' + authMeta.label)
        : (c.has_key ? '' : '\\n无 API Key，请重新保存'));
    html += '<span class="conn-card' + active + '" onclick="switchConnection(' + i + ')" title="' + escapeHtml(title) + '">';
    html += '<span class="conn-card-title">' + activeTag + '<span class="conn-card-name">' + escapeHtml(c.name || providerDisplay) + keyIcon + '</span></span>';
    html += '<span class="conn-card-meta"><span class="conn-card-badge">' + escapeHtml(modeDisplay) + '</span>'
      + (runtimeBadge ? '<span class="conn-card-badge">' + escapeHtml(runtimeBadge) + '</span>' : '')
      + (compatCandidate ? '<span class="conn-card-badge">' + escapeHtml((compatCandidate.label || compatCandidate.runtime_kind || '') + ' 兼容保留') + '</span>' : '')
      + '<span class="conn-card-host">' + escapeHtml(host) + '</span></span>';
    if (c.auth_mode === 'account_login') {
      html += '<span class="conn-auth-row">';
      html += '<span class="conn-auth-badge ' + escapeHtml(authMeta.stateClass) + '">' + escapeHtml(authMeta.label) + '</span>';
      html += '<button class="conn-auth-action" onclick="event.stopPropagation();openConnectionAuthModalById(\'' + escapeHtml(c.id || '') + '\')">管理登录</button>';
      html += '</span>';
    }
    html += '<span class="conn-delete" onclick="event.stopPropagation();deleteConnection(' + i + ')">×</span>';
    html += '</span>';
  }
  cards.innerHTML = html;
}

async function switchConnection(idx) {
  const c = _savedConnections[idx];
  if (!c) return;
  const modeDisplay = c.mode_display || getConnectionModeLabel(c.provider_id, c.provider_display);
  const hadActiveSession = !!S.activeSessionId;
  if (c.active) {
    let msg = '当前默认连接已是: ' + (c.name || 'API 连接') + ' · ' + modeDisplay;
    if (hadActiveSession) msg += '；当前运行中的主会话不会被设置页改动。';
    showToast(msg, 'success');
    return;
  }
  if (_isMainSessionDirty() && !confirm('当前主会话配置有未保存修改，切换快捷连接会覆盖这些内容，仍要继续吗？')) {
    return;
  }
  try {
    const r = await fetch('/vizo/console/api/settings/connections', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        action: 'switch',
        index: idx
      })
    });
    const d = await r.json();
    if (r.ok) {
      let msg = '默认连接已切换到: ' + (c.name || 'API 连接') + ' · ' + modeDisplay;
      let level = 'success';
      if (hadActiveSession) {
        msg += '；当前运行中的主会话不会被设置页改动，请在主会话顶部的渠道选择器里切换。';
      }
      if (d.claude_settings_synced === false) {
        msg += '；但 Claude 兼容配置文件暂未写入成功，请稍后重试。';
        level = 'warning';
      }
      showToast(msg, level);
      resetConnectionTestResult();
      loadApiKeyStatus();
      loadSavedConnections();
    } else {
      showToast(d.error || '切换失败', 'error');
    }
  } catch(e) { showToast('切换失败', 'error'); }
}

async function saveAsConnection() {
  return _saveCurrentConnectionAsProfile({});
}

async function _saveCurrentConnectionAsProfile(options) {
  options = options || {};
  const apikeyInput = document.getElementById('new-apikey');
  const rawKey = apikeyInput.value.trim();
  const masked = apikeyInput.dataset.masked || '';
  const payload = validateMainProviderPayload(true);
  if (!payload) return null;
  const apiKey = payload.auth_mode === 'account_login'
    ? ''
    : ((rawKey && rawKey !== masked) ? rawKey : '__USE_STORED__');
  const baseUrl = payload.base_url || (getMainProviderOption(payload.provider_id)?.default_base_url || 'https://api.anthropic.com');
  const modeDisplay = getConnectionModeLabel(payload.provider_id, getMainProviderLabel(payload.provider_id));
  const suggestedName = getConnectionNameSuggestion(baseUrl, payload.provider_id, getMainProviderLabel(payload.provider_id));
  const name = prompt('连接名称（建议区分模式）:', suggestedName);
  if (name === null) return null;
  try {
    const body = {action: 'save', name: name || suggestedName};
    body.api_key = apiKey;
    Object.assign(body, payload);
    const r = await fetch('/vizo/console/api/settings/connections', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    const d = await r.json();
    if (r.ok) {
      const msg = payload.auth_mode === 'account_login'
        ? ((options.openAuthAfterSave ? '连接已保存，正在打开登录窗口：' : '连接已保存：') + (name || suggestedName) + ' · ' + modeDisplay)
        : ('连接已保存：' + (name || suggestedName) + ' · ' + modeDisplay + '，可点击卡片快速切换');
      showToast(msg, 'success');
      await loadSavedConnections();
      return d.saved_connection || null;
    } else {
      showToast(d.error || '保存失败', 'error');
      return null;
    }
  } catch(e) {
    showToast('保存失败', 'error');
    return null;
  }
}

async function saveAccountLoginConnectionAndOpenModal() {
  const savedConnection = await _saveCurrentConnectionAsProfile({openAuthAfterSave: true});
  const connectionId = savedConnection?.id || '';
  if (!connectionId) {
    showToast('保存连接成功，但未拿到连接标识，请刷新后重试。', 'warning');
    return;
  }
  await loadApiKeyStatus();
  await openConnectionAuthModalById(connectionId);
}

async function deleteConnection(idx) {
  try {
    const r = await fetch('/vizo/console/api/settings/connections', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: 'delete', index: idx})
    });
    if (r.ok) loadSavedConnections();
  } catch(e) {}
}

function _clearConnectionAuthModalTimer() {
  if (_connectionAuthModalState.timer) {
    clearTimeout(_connectionAuthModalState.timer);
    _connectionAuthModalState.timer = null;
  }
}

function closeConnectionAuthModal() {
  _clearConnectionAuthModalTimer();
  _connectionAuthModalState.connectionId = '';
  _connectionAuthModalState.payload = null;
  const modal = document.getElementById('connection-auth-modal');
  if (modal) modal.style.display = 'none';
}

function _renderConnectionAuthModalPayload(payload) {
  const body = document.getElementById('connection-auth-modal-body');
  const title = document.getElementById('connection-auth-modal-title');
  const loginBtn = document.getElementById('connection-auth-login-btn');
  const refreshBtn = document.getElementById('connection-auth-refresh-btn');
  const logoutBtn = document.getElementById('connection-auth-logout-btn');
  if (!body || !title || !loginBtn || !refreshBtn || !logoutBtn) return;
  const stateClass = payload?.login_state || getConnectionAuthStatusMeta(payload || {}).stateClass || 'idle';
  const stateLabel = payload?.login_state === 'waiting'
    ? '等待授权'
    : (payload?.auth_status === 'ready'
      ? '账号已登录'
      : (payload?.auth_status === 'expired' ? '登录已失效' : '尚未登录'));
  title.textContent = 'OpenAI 账号登录';
  let html = '<div class="auth-status-row">';
  html += '<span class="auth-status-badge ' + escapeHtml(stateClass) + '">' + escapeHtml(stateLabel) + '</span>';
  if (payload?.auth_account_label) {
    html += '<span style="color:var(--text-muted)">账号：' + escapeHtml(payload.auth_account_label) + '</span>';
  }
  html += '</div>';
  if (payload?.status_message) {
    html += '<div style="color:var(--text-muted)">' + escapeHtml(payload.status_message) + '</div>';
  }
  if (payload?.device_code || payload?.device_url) {
    html += '<div class="auth-device-box">';
    if (payload?.device_code) {
      html += '<div style="color:var(--text-muted)">1. 打开浏览器访问：</div>';
      html += '<div style="margin-top:0.28rem"><a href="' + escapeHtml(payload.device_url || 'https://auth.openai.com/codex/device') + '" target="_blank" rel="noopener noreferrer">' + escapeHtml(payload.device_url || 'https://auth.openai.com/codex/device') + '</a></div>';
      html += '<div style="margin-top:0.7rem;color:var(--text-muted)">2. 输入一次性设备码：</div>';
      html += '<div class="auth-device-code">' + escapeHtml(payload.device_code || '等待生成') + '</div>';
    } else {
      html += '<div style="color:var(--text-muted)">浏览器应已自动打开；如果没有自动打开，请访问下面的登录地址完成 OpenAI 账号授权：</div>';
      html += '<div style="margin-top:0.42rem"><a href="' + escapeHtml(payload.device_url || 'https://auth.openai.com/') + '" target="_blank" rel="noopener noreferrer">' + escapeHtml(payload.device_url || 'https://auth.openai.com/') + '</a></div>';
    }
    html += '</div>';
  }
  if (payload?.error) {
    html += '<div style="margin-top:0.7rem;color:var(--danger)">' + escapeHtml(payload.error) + '</div>';
  }
  body.innerHTML = html;
  loginBtn.textContent = payload?.login_state === 'waiting' ? '重新发起登录' : '开始登录';
  loginBtn.style.display = payload?.auth_status === 'ready' ? 'none' : '';
  logoutBtn.style.display = payload?.auth_status === 'ready' ? '' : 'none';
  refreshBtn.disabled = false;
}

async function openConnectionAuthModalById(connectionId) {
  if (!connectionId || connectionId === 'main_session') {
    showToast('请先把账号登录配置保存为快捷连接，再执行登录。', 'warning');
    return;
  }
  _connectionAuthModalState.connectionId = connectionId;
  _connectionAuthModalState.payload = null;
  const modal = document.getElementById('connection-auth-modal');
  const body = document.getElementById('connection-auth-modal-body');
  if (body) body.innerHTML = '<div style="color:var(--text-muted)">加载中...</div>';
  if (modal) modal.style.display = '';
  await refreshConnectionAuthModalStatus(false);
}

async function refreshConnectionAuthStatusInline(connectionId) {
  try {
    const r = await fetch('/vizo/console/api/settings/connections/' + encodeURIComponent(connectionId) + '/auth/status');
    const d = await r.json();
    if (!r.ok) {
      showToast(d.error || '刷新登录状态失败', 'error');
      return;
    }
    await loadSavedConnections();
    await loadApiKeyStatus();
    showToast('登录状态已刷新：' + (getConnectionAuthStatusMeta(d).label || '完成'), 'success');
  } catch(e) {
    showToast('刷新登录状态失败', 'error');
  }
}

async function refreshConnectionAuthModalStatus(showToastOnError) {
  const connectionId = _connectionAuthModalState.connectionId;
  if (!connectionId) return;
  _clearConnectionAuthModalTimer();
  try {
    const r = await fetch('/vizo/console/api/settings/connections/' + encodeURIComponent(connectionId) + '/auth/status');
    const d = await r.json();
    if (!r.ok) {
      if (showToastOnError) showToast(d.error || '加载登录状态失败', 'error');
      return;
    }
    _connectionAuthModalState.payload = d;
    _renderConnectionAuthModalPayload(d);
    await loadSavedConnections();
    await loadApiKeyStatus();
    if (d.login_state === 'waiting') {
      _connectionAuthModalState.timer = setTimeout(function() {
        refreshConnectionAuthModalStatus(false);
      }, 2000);
    }
  } catch(e) {
    if (showToastOnError) showToast('加载登录状态失败', 'error');
  }
}

async function startConnectionAccountLogin() {
  const connectionId = _connectionAuthModalState.connectionId;
  if (!connectionId) return;
  try {
    const r = await fetch('/vizo/console/api/settings/connections/' + encodeURIComponent(connectionId) + '/auth/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({})
    });
    const d = await r.json();
    if (!r.ok) {
      showToast(d.error || '启动登录失败', 'error');
      return;
    }
    _connectionAuthModalState.payload = d;
    _renderConnectionAuthModalPayload(d);
    await loadSavedConnections();
    await loadApiKeyStatus();
    if (d.login_state === 'waiting') {
      _connectionAuthModalState.timer = setTimeout(function() {
        refreshConnectionAuthModalStatus(false);
      }, 1500);
    }
  } catch(e) {
    showToast('启动登录失败', 'error');
  }
}

async function logoutConnectionAccountAuth() {
  const connectionId = _connectionAuthModalState.connectionId;
  if (!connectionId) return;
  try {
    const r = await fetch('/vizo/console/api/settings/connections/' + encodeURIComponent(connectionId) + '/auth/logout', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({})
    });
    const d = await r.json();
    if (!r.ok) {
      showToast(d.error || '退出登录失败', 'error');
      return;
    }
    _connectionAuthModalState.payload = d;
    _renderConnectionAuthModalPayload(d);
    await loadSavedConnections();
    await loadApiKeyStatus();
    showToast('已退出该连接的 OpenAI 账号登录', 'success');
  } catch(e) {
    showToast('退出登录失败', 'error');
  }
}

// ======================== External Models Config ========================
let _extModelsData = null;
async function loadExternalModels() {
  const el = document.getElementById('ext-models-list');
  try {
    const r = await fetch('/vizo/console/api/settings/external-models');
    if (r.status === 401) { el.innerHTML = '<div style="color:var(--danger)">请先登录</div>'; return; }
    const data = await r.json();
    const models = data.models || data;
    _extModelsData = models;
    let html = '';
    // 渲染预置模型
    for (const m of (models.presets || [])) {
      html += _renderExtCard(m, true);
    }
    // 自定义模型分区
    html += '<div class="ext-section-label" style="margin-top:0.5rem">🔧 自定义模型</div>';
    html += '<div class="model-info">同一平台可添加多个模型。若平台本质是直转 Claude，不需要在这里重复配置；这里只有在角色需要独立连接，且你要接入 Claude 或 Anthropic 兼容模型时才使用。</div>';
    for (const m of (models.customs || [])) {
      html += _renderExtCard(m, false);
    }
    // 添加自定义模型表单（折叠）
    html += '<div id="ext-add-form" style="display:none;border:1px dashed var(--border);border-radius:6px;padding:0.65rem 0.8rem;margin-top:0.4rem">';
    html += '<div class="ext-model-fields">';
    html += '<div class="ext-field-row"><span class="ext-field-label">显示名</span><input class="ext-field-input" id="new-ext-display" type="text" placeholder="GLM-4（可选，不填用模型 ID）" autocomplete="off"></div>';
    html += '<div class="ext-field-row"><span class="ext-field-label">接入方式</span><select class="ext-field-input" id="new-ext-access-mode"><option value="anthropic_gateway">Anthropic 兼容接口（需要模型映射）</option><option value="anthropic_native">Anthropic 兼容接口（平台原生支持）</option></select></div>';
    html += '<div class="ext-field-row"><span class="ext-field-label">模型 ID</span><input class="ext-field-input" id="new-ext-cli" type="text" placeholder="实际模型名，如 GLM-4" autocomplete="off"></div>';
    html += '<div class="ext-field-row"><span class="ext-field-label">Base URL</span><input class="ext-field-input" id="new-ext-url" type="text" placeholder="Anthropic 兼容入口，如 https://.../claude" autocomplete="off"></div>';
    html += '<div class="ext-field-row"><span class="ext-field-label">API Key</span><input class="ext-field-input" id="new-ext-key" type="password" placeholder="sk-..." autocomplete="off"></div>';
    html += '</div>';
    html += '<div style="display:flex;gap:0.5rem;margin-top:0.5rem">';
    html += '<button onclick="addCustomExtModel()" style="flex:1;padding:0.35rem;background:var(--accent-dim);border:1px solid var(--accent);border-radius:4px;color:var(--accent);font-size:0.8rem;cursor:pointer">确认添加</button>';
    html += '<button onclick="toggleExtAddForm(false)" style="padding:0.35rem 0.7rem;background:none;border:1px solid var(--border);border-radius:4px;color:var(--text-muted);font-size:0.8rem;cursor:pointer">取消</button>';
    html += '</div></div>';
    html += '<button onclick="toggleExtAddForm(true)" id="ext-add-btn" style="width:100%;margin-top:0.4rem;padding:0.35rem;background:none;border:1px dashed var(--border);border-radius:4px;color:var(--text-muted);font-size:0.8rem;cursor:pointer;transition:all 0.15s" onmouseover="this.style.borderColor=\'var(--accent)\';this.style.color=\'var(--accent)\'" onmouseout="this.style.borderColor=\'var(--border)\';this.style.color=\'var(--text-muted)\'">＋ 添加自定义模型</button>';
    el.innerHTML = html;
  } catch(e) {
    el.innerHTML = '<div style="color:var(--danger);padding:0.5rem">加载失败</div>';
  }
  _refreshSettingsSectionState('external-models');
}

function _renderExtCard(m, isPreset) {
  const cls = m.configured ? 'ext-model-card configured' : 'ext-model-card';
  const dotCls = m.configured ? 'ext-dot on' : 'ext-dot';
  let html = '<div class="' + cls + '" data-model-id="' + m.id + '" data-access-mode="' + escapeHtml(m.access_mode || '') + '">';
  html += '<div class="ext-model-name"><span class="' + dotCls + '"></span>' + m.display;
  if (m.configured) html += ' <span style="font-size:0.7rem;color:var(--green)">已配置</span>';
  if (!isPreset) html += ' <button class="ext-clear-btn" onclick="clearExtModel(\'' + m.id + '\',this)" style="margin-left:auto">删除</button>';
  html += '</div><div class="ext-model-fields">';
  html += '<div class="model-info" style="margin:0 0 0.35rem">接入方式：' + escapeHtml(m.source_display || 'Anthropic 兼容接口') + '</div>';
  const modelPH = isPreset ? (m.model_placeholder || '') : m.cli_model || '';
  html += '<div class="ext-field-row"><span class="ext-field-label">模型 ID</span><input class="ext-field-input ext-cli-model" type="text" placeholder="' + modelPH + '" value="' + (m.cli_model || '') + '" data-orig="' + (m.cli_model || '') + '" autocomplete="off"></div>';
  const urlPH = isPreset ? m.default_base_url : (m.base_url || '');
  const urlVal = (isPreset && m.base_url === m.default_base_url) ? '' : (m.base_url || '');
  html += '<div class="ext-field-row"><span class="ext-field-label">Base URL</span><input class="ext-field-input ext-baseurl" type="text" placeholder="' + urlPH + '" value="' + urlVal + '" data-orig="' + urlVal + '" autocomplete="off"></div>';
  // API Key: 已配置时用特殊标记值 __EXISTING__，未配置时为空
  html += '<div class="ext-field-row"><span class="ext-field-label">API Key</span>';
  const hasApiKey = m.configured && m.api_key_masked;
  const apiKeyPlaceholder = hasApiKey ? '已配置（修改时覆盖）' : '未配置';
  const apiKeyValue = hasApiKey ? '__EXISTING__' : '';
  html += '<input class="ext-field-input ext-apikey" type="password" placeholder="' + apiKeyPlaceholder + '" value="' + apiKeyValue + '" data-existing="' + (hasApiKey ? '1' : '0') + '" autocomplete="off">';
  if (isPreset && m.configured) html += '<button class="ext-clear-btn" onclick="clearExtModel(\'' + m.id + '\',this)">清除</button>';
  html += '</div></div></div>';
  return html;
}

function toggleExtAddForm(show) {
  document.getElementById('ext-add-form').style.display = show ? '' : 'none';
  document.getElementById('ext-add-btn').style.display = show ? 'none' : '';
  if (!show) {
    ['new-ext-display', 'new-ext-cli', 'new-ext-url', 'new-ext-key'].forEach(function(id) {
      const el = document.getElementById(id);
      if (el) el.value = '';
    });
    const modeEl = document.getElementById('new-ext-access-mode');
    if (modeEl) modeEl.value = 'anthropic_gateway';
    _refreshSettingsSectionState('external-models');
  }
  if (show) document.getElementById('new-ext-cli').focus();
}

async function addCustomExtModel() {
  const display = document.getElementById('new-ext-display').value.trim();
  const accessMode = document.getElementById('new-ext-access-mode').value || 'anthropic_gateway';
  const cliModel = document.getElementById('new-ext-cli').value.trim();
  const baseUrl = document.getElementById('new-ext-url').value.trim();
  const apiKey = document.getElementById('new-ext-key').value.trim();
  if (!cliModel) { showToast('请填写模型 ID', 'error'); return; }
  if (!baseUrl) { showToast('请填写 Base URL', 'error'); return; }
  const baseUrlWarning = getExternalBaseUrlValidationMessage(baseUrl, accessMode);
  if (baseUrlWarning) { showToast(baseUrlWarning, 'error'); return; }
  if (!apiKey) { showToast('请填写 API Key', 'error'); return; }
  // 自动从模型 ID 推导 config key：小写 + 只保留字母/数字/连字符/下划线
  const mid = cliModel.toLowerCase().replace(/[^a-z0-9_-]/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '');
  try {
    const r = await fetch('/vizo/console/api/settings/external-models', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        model_id: mid,
        cli_model: cliModel,
        display: display,
        base_url: baseUrl,
        api_key: apiKey,
        access_mode: accessMode,
        provider_family: accessMode === 'anthropic_gateway' ? 'gateway' : 'custom'
      })
    });
    const d = await r.json();
    if (d.success) {
      showToast('自定义模型已添加', 'success');
      ['new-ext-display', 'new-ext-cli', 'new-ext-url', 'new-ext-key'].forEach(function(id) {
        const el = document.getElementById(id);
        if (el) el.value = '';
      });
      const modeEl = document.getElementById('new-ext-access-mode');
      if (modeEl) modeEl.value = 'anthropic_gateway';
      toggleExtAddForm(false);
      loadExternalModels();
      _modelsLoaded = false;
    } else { showToast(d.error || '添加失败', 'error'); }
  } catch(e) { showToast('网络错误', 'error'); }
}

async function clearExtModel(modelId, btn) {
  btn.disabled = true;
  try {
    const r = await fetch('/vizo/console/api/settings/external-models', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({model_id: modelId, api_key: '__DELETE__', base_url: '', cli_model: ''})
    });
    const d = await r.json();
    if (d.success) {
      showToast('已清除 ' + modelId + ' 配置', 'success');
      loadExternalModels();
      _modelsLoaded = false;
    } else { showToast(d.error || '操作失败', 'error'); }
  } catch(e) { showToast('网络错误', 'error'); }
  btn.disabled = false;
}

function _getMainProviderOriginalDefaults() {
  return {
    default_opus_model: document.getElementById('new-default-opus-model')?.dataset.orig || '',
    default_sonnet_model: document.getElementById('new-default-sonnet-model')?.dataset.orig || '',
    default_haiku_model: document.getElementById('new-default-haiku-model')?.dataset.orig || ''
  };
}

async function saveMainSessionConfig() {
  if (!_isMainSessionDirty()) {
    _refreshSettingsSectionState('main-session');
    showToast('默认主连接配置没有需要保存的改动', 'success');
    return;
  }
  const apikeyInput = document.getElementById('new-apikey');
  const rawKey = apikeyInput.value.trim();
  const masked = apikeyInput.dataset.masked || '';
  const baseUrlInput = document.getElementById('new-baseurl');
  const baseUrl = baseUrlInput.value.trim();

  const payload = validateMainProviderPayload(true);
  if (!payload) return;
  const key = payload.auth_mode === 'account_login' ? '' : ((rawKey && rawKey !== masked) ? rawKey : '');

  _setSettingsActionLoading('main-session', true);
  try {
    const body = Object.assign({}, payload);
    if (key) body.api_key = key;
    const r = await fetch('/vizo/console/api/settings/apikey', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
    });
    const d = await r.json();
    if (!d.success) {
      showToast(d.error || '默认主连接配置保存失败', 'error');
      return;
    }
    if (apikeyInput) apikeyInput.value = '';
    await loadApiKeyStatus();
    await loadSavedConnections();
    _modelsLoaded = false;
    let msg = '默认主连接配置已保存';
    let level = 'success';
    if (S.activeSessionId) {
      msg += '；当前运行中的主会话不会被设置页改动。';
    }
    if (d.claude_settings_synced === false) {
      msg += '；但 Claude 兼容配置文件暂未写入成功，请稍后重试。';
      level = 'warning';
    }
    showToast(msg, level);
  } catch(e) {
    showToast('网络错误', 'error');
  }
  finally { _setSettingsActionLoading('main-session', false); }
}

async function saveExternalModelsConfig() {
  if (!_isExternalModelsDirty()) {
    _refreshSettingsSectionState('external-models');
    showToast('外部模型配置没有需要保存的改动', 'success');
    return;
  }
  const pendingAddForm = ['new-ext-display', 'new-ext-cli', 'new-ext-url', 'new-ext-key'].some(function(id) {
    return (document.getElementById(id)?.value || '').trim() !== '';
  }) || (document.getElementById('new-ext-access-mode')?.value || 'anthropic_gateway') !== 'anthropic_gateway';
  if (pendingAddForm) {
    showToast('有未完成的自定义模型表单，请先添加或取消', 'warning');
    return;
  }
  _setSettingsActionLoading('external-models', true);
  const cards = document.querySelectorAll('#ext-models-list .ext-model-card');
  let extSaved = 0, extErr = null;
  for (const card of cards) {
    const modelId = card.dataset.modelId;
    const apiKeyInput = card.querySelector('.ext-apikey');
    const apiKeyRaw = apiKeyInput ? apiKeyInput.value.trim() : '';
    const apiKey = (apiKeyRaw && apiKeyRaw !== '__EXISTING__') ? apiKeyRaw : '';
    const burlInput = card.querySelector('.ext-baseurl');
    const burl = burlInput ? burlInput.value.trim() : '';
    const burlOrig = burlInput ? (burlInput.dataset.orig || '') : '';
    const cliInput = card.querySelector('.ext-cli-model');
    const cliModel = cliInput ? cliInput.value.trim() : '';
    const cliOrig = cliInput ? (cliInput.dataset.orig || '') : '';
    const accessMode = card.dataset.accessMode || '';
    if (!apiKey && burl === burlOrig && cliModel === cliOrig) continue;
    try {
      const r = await fetch('/vizo/console/api/settings/external-models', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({model_id: modelId, cli_model: cliModel, api_key: apiKey, base_url: burl, access_mode: accessMode})
      });
      const d = await r.json();
      if (d.success) { extSaved++; if (apiKeyInput) apiKeyInput.value = ''; }
      else { extErr = d.error; }
    } catch(e) { extErr = '网络错误'; }
  }

  if (extErr) {
    showToast('外部模型保存失败: ' + extErr, 'error');
    _setSettingsActionLoading('external-models', false);
    return;
  }
  await loadExternalModels();
  _modelsLoaded = false;
  showToast(extSaved + ' 个外部模型已保存', 'success');
  _setSettingsActionLoading('external-models', false);
}

async function saveModelConfig() {
  await saveMainSessionConfig();
  await saveExternalModelsConfig();
}

// ======================== Network Domain ========================
let _domainLoaded = false;
async function loadDomainStatus() {
  const el = document.getElementById('domain-status');
  try {
    const r = await fetch('/vizo/console/api/settings/domain');
    if (r.status === 401) { el.textContent = '请先登录'; return; }
    const d = await r.json();
    _domainLoaded = true;
    let html = '';
    if (d.custom_domain && d.custom_domain !== '') {
      html += '<span style="color:var(--green)">● 已配置域名: ' + d.custom_domain + '</span>';
      html += '<br>有效 URL: ' + d.effective_url;
    } else {
      html += '<span style="color:var(--text-muted)">○ 未配置域名</span>';
    }
    el.innerHTML = html;
    const input = document.getElementById('domain-input');
    input.value = d.custom_domain || '';
    input.dataset.orig = d.custom_domain || '';
  } catch(e) { el.textContent = '加载失败'; }
  _refreshSettingsSectionState('network');
}

async function saveDomain() {
  if (!_isNetworkDirty()) {
    _refreshSettingsSectionState('network');
    showToast('网络配置没有需要保存的改动', 'success');
    return;
  }
  const domain = document.getElementById('domain-input').value.trim();
  // 去除 http:// 前缀
  const cleanDomain = domain.replace(/^https?:\/\//, '');
  // 前端简单校验
  if (cleanDomain && !/^[a-zA-Z0-9][a-zA-Z0-9-]*(\.[a-zA-Z0-9][a-zA-Z0-9-]*)+$/.test(cleanDomain)) {
    showToast('域名格式非法', 'error');
    return;
  }
  _setSettingsActionLoading('network', true);
  try {
    const r = await fetch('/vizo/console/api/settings/domain', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({custom_domain: cleanDomain})
    });
    if (r.status === 401) { showToast('请先登录', 'error'); return; }
    const d = await r.json();
    if (d.success) {
      showToast('保存成功，有效 URL: ' + d.effective_url, 'success');
      await loadDomainStatus();
    } else {
      showToast('保存失败', 'error');
    }
  } catch(e) { showToast('网络错误', 'error'); }
  finally { _setSettingsActionLoading('network', false); }
}

// ======================== Project Manager ========================

var pmState = { selectedProject: null, breadcrumb: [] };

function openProjectManager() {
  document.querySelector('.terminal-container').style.display = 'none';
  document.getElementById('input-area').style.display = 'none';
  document.getElementById('agents-view').style.display = 'none';
  document.getElementById('model-config-view').style.display = 'none';
  document.getElementById('settings-view').style.display = 'none';
  document.getElementById('project-manager-view').style.display = 'flex';
  pmState.selectedProject = null;
  pmState.breadcrumb = [];
  loadPMProjects();
}

function closeProjectManager() {
  document.getElementById('project-manager-view').style.display = 'none';
  document.querySelector('.terminal-container').style.display = '';
  document.getElementById('input-area').style.display = '';
  if (S.term && S.fitAddon) {
    try { S.fitAddon.fit(); } catch(e) {}
  }
}

async function loadPMProjects() {
  try {
    const r = await fetch('/vizo/console/api/projects');
    const d = await r.json();
    renderPMProjectList(d.projects || []);
  } catch(e) {
    document.getElementById('pm-project-list').innerHTML = '<div class="pm-empty">' + t('pmLoadFailed') + '</div>';
  }
}

function renderPMProjectList(projects) {
  var el = document.getElementById('pm-project-list');
  var html = '<div style="padding:0.6rem 1rem 0.3rem;font-size:0.72rem;color:var(--text-muted);font-weight:600;text-transform:uppercase;letter-spacing:0.5px">' + t('pmProjectList') + '</div>';
  if (projects.length === 0) {
    html += '<div class="pm-empty" style="padding:2rem 1rem">' + t('pmNoProjects') + '<br><span style="font-size:0.78rem">' + t('pmCreateHint') + '</span></div>';
  } else {
    projects.forEach(function(p) {
      var cls = (pmState.selectedProject === p.name) ? ' active' : '';
      var typeLabel = '';
      if (p.type === 'hub') {
        typeLabel = '<span style="display:inline-block;font-size:0.65rem;padding:0.1rem 0.4rem;border-radius:3px;background:var(--accent);color:#fff;margin-left:0.4rem;vertical-align:middle">' + t('pmTypeHub') + '</span>';
      }
      html += '<div class="pm-project-item' + cls + '" onclick="selectPMProject(\'' + escHtml(p.name) + '\')">' +
        '<div class="pm-project-name">' + escHtml(p.name) + typeLabel + '</div>' +
        (p.description ? '<div class="pm-project-desc">' + escHtml(p.description) + '</div>' : '') +
        '</div>';
    });
  }
  // Footer with delete button
  if (pmState.selectedProject) {
    html += '<div class="pm-sidebar-footer"><button class="pm-delete-btn" onclick="deletePMProject(\'' + escHtml(pmState.selectedProject) + '\')">&#128465; ' + t('pmDeleteProject') + '</button></div>';
  }
  el.innerHTML = html;
}

function escHtml(s) {
  var d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function selectPMProject(name) {
  pmState.selectedProject = name;
  pmState.breadcrumb = [{label: name, action: function(){ loadPMTasks(name); }}];
  loadPMProjects(); // re-render to highlight
  loadPMTasks(name);
}

async function loadPMTasks(name) {
  pmState.breadcrumb = [{label: name, action: function(){ loadPMTasks(name); }}];
  renderPMBreadcrumb();
  var main = document.getElementById('pm-main-area');
  main.innerHTML = '<div class="pm-breadcrumb" id="pm-breadcrumb"></div><div class="pm-body" id="pm-body"><div class="pm-empty">' + t('pmLoading') + '</div></div>';
  renderPMBreadcrumb();
  try {
    var r = await fetch('/vizo/console/api/projects/' + encodeURIComponent(name) + '/tasks');
    var d = await r.json();
    var tasks = d.tasks || [];
    var body = document.getElementById('pm-body');
    if (tasks.length === 0) {
      body.innerHTML = '<div class="pm-empty">' + t('pmNoTasks') + '</div>';
      return;
    }
    var html = '';
    tasks.forEach(function(tk) {
      var statusCls = tk.status || 'unknown';
      html += '<div class="pm-task-card" onclick="selectPMTask(\'' + escHtml(name) + '\',\'' + escHtml(tk.id) + '\',\'' + escHtml(tk.path || (".vizo/tasks/" + tk.id)) + '\')">' +
        '<div class="pm-task-header">' +
        '<span class="pm-task-id">' + escHtml(tk.id) + '</span>' +
        '<span class="pm-task-status ' + statusCls + '">' + escHtml(tk.status) + '</span>' +
        '</div>' +
        '<div class="pm-task-name">' + escHtml(tk.task_name || t('pmNoName')) + '</div>' +
        '<div class="pm-task-cost">$' + (tk.cost_usd || 0).toFixed(2) + '</div>' +
        '</div>';
    });
    body.innerHTML = html;
  } catch(e) {
    document.getElementById('pm-body').innerHTML = '<div class="pm-empty">' + t('pmLoadFailed') + '</div>';
  }
}

function selectPMTask(name, taskId, taskPath) {
  taskPath = taskPath || ('.vizo/tasks/' + taskId);
  pmState.breadcrumb = [
    {label: name, action: function(){ loadPMTasks(name); }},
    {label: taskId, action: function(){ browsePMDir(name, taskPath); }}
  ];
  browsePMDir(name, taskPath);
}

async function browsePMDir(name, path) {
  // Update breadcrumb for current path
  var baseParts = [
    {label: name, action: function(){ loadPMTasks(name); }}
  ];
  // If path starts with .vizo/tasks/{taskId} or .opus/tasks/{taskId}, add task level
  var m = path.match(/^\.(vizo|opus)\/tasks\/([^\/]+)(\/(.*))?$/);
  if (m) {
    var rootName = m[1];
    var taskId = m[2];
    var subPath = m[4] || '';
    var taskBase = '.' + rootName + '/tasks/' + taskId;
    baseParts.push({label: taskId, action: function(){ browsePMDir(name, taskBase); }});
    if (subPath) {
      var parts = subPath.split('/');
      var cumulative = taskBase;
      parts.forEach(function(p) {
        cumulative += '/' + p;
        var cp = cumulative; // closure
        baseParts.push({label: p, action: function(){ browsePMDir(name, cp); }});
      });
    }
  }
  pmState.breadcrumb = baseParts;

  var main = document.getElementById('pm-main-area');
  main.innerHTML = '<div class="pm-breadcrumb" id="pm-breadcrumb"></div><div class="pm-body" id="pm-body"><div class="pm-empty">' + t('pmLoading') + '</div></div>';
  renderPMBreadcrumb();

  try {
    var r = await fetch('/vizo/console/api/projects/' + encodeURIComponent(name) + '/files?path=' + encodeURIComponent(path));
    var d = await r.json();
    if (d.error) { document.getElementById('pm-body').innerHTML = '<div class="pm-empty">' + escHtml(d.error) + '</div>'; return; }
    if (d.type === 'dir') {
      var entries = d.entries || [];
      var body = document.getElementById('pm-body');
      if (entries.length === 0) {
        body.innerHTML = '<div class="pm-empty">' + t('pmEmptyDir') + '</div>';
        return;
      }
      var html = '';
      entries.forEach(function(e) {
        var icon = e.type === 'dir' ? '&#128193;' : '&#128196;';
        var meta = e.type === 'dir' ? formatDate(e.mtime) : formatSize(e.size);
        var childPath = path + '/' + e.name;
        if (e.type === 'dir') {
          html += '<div class="pm-file-entry" onclick="browsePMDir(\'' + escHtml(name) + '\',\'' + escHtml(childPath) + '\')">';
        } else {
          html += '<div class="pm-file-entry" onclick="previewPMFile(\'' + escHtml(name) + '\',\'' + escHtml(childPath) + '\')">';
        }
        html += '<span class="pm-file-icon">' + icon + '</span>' +
          '<span class="pm-file-name">' + escHtml(e.name) + '</span>' +
          '<span class="pm-file-meta">' + meta + '</span></div>';
      });
      body.innerHTML = html;
    } else if (d.type === 'file') {
      renderPMFilePreview(d);
    }
  } catch(e) {
    document.getElementById('pm-body').innerHTML = '<div class="pm-empty">' + t('pmLoadFailed') + '</div>';
  }
}

async function previewPMFile(name, path) {
  // Update breadcrumb
  var parts = path.split('/');
  var fileName = parts[parts.length - 1];
  var dirPath = parts.slice(0, -1).join('/');
  // Rebuild breadcrumb up to parent dir, then add file
  var baseParts = [{label: name, action: function(){ loadPMTasks(name); }}];
  var m = path.match(/^\.(vizo|opus)\/tasks\/([^\/]+)(\/(.*))?$/);
  if (m) {
    var rootName = m[1];
    var taskId = m[2];
    var taskBase = '.' + rootName + '/tasks/' + taskId;
    baseParts.push({label: taskId, action: function(){ browsePMDir(name, taskBase); }});
    var afterTask = m[4] || '';
    if (afterTask) {
      var segs = afterTask.split('/');
      var cum = taskBase;
      segs.forEach(function(s, i) {
        cum += '/' + s;
        if (i < segs.length - 1) {
          var cp = cum;
          baseParts.push({label: s, action: function(){ browsePMDir(name, cp); }});
        } else {
          baseParts.push({label: s, action: null}); // current file, no action
        }
      });
    }
  }
  pmState.breadcrumb = baseParts;

  var main = document.getElementById('pm-main-area');
  main.innerHTML = '<div class="pm-breadcrumb" id="pm-breadcrumb"></div><div class="pm-body" id="pm-body"><div class="pm-empty">' + t('pmLoading') + '</div></div>';
  renderPMBreadcrumb();

  try {
    var r = await fetch('/vizo/console/api/projects/' + encodeURIComponent(name) + '/files?path=' + encodeURIComponent(path));
    var d = await r.json();
    if (d.error) { document.getElementById('pm-body').innerHTML = '<div class="pm-empty">' + escHtml(d.error) + '</div>'; return; }
    renderPMFilePreview(d);
  } catch(e) {
    document.getElementById('pm-body').innerHTML = '<div class="pm-empty">' + t('pmLoadFailed') + '</div>';
  }
}

function renderPMFilePreview(data) {
  var body = document.getElementById('pm-body');
  if (data.binary) {
    body.innerHTML = '<div class="pm-empty">' + t('pmBinaryFile') + '</div>';
    return;
  }
  var content = data.content || '';
  body.innerHTML = '<div class="pm-file-preview">' + escHtml(content) + '</div>';
}

function renderPMBreadcrumb() {
  var el = document.getElementById('pm-breadcrumb');
  if (!el) return;
  var html = '';
  pmState.breadcrumb.forEach(function(b, i) {
    if (i > 0) html += '<span class="pm-sep"> / </span>';
    if (i === pmState.breadcrumb.length - 1) {
      html += '<span class="current">' + escHtml(b.label) + '</span>';
    } else {
      html += '<span onclick="pmBreadcrumbClick(' + i + ')">' + escHtml(b.label) + '</span>';
    }
  });
  el.innerHTML = html;
}

function pmBreadcrumbClick(index) {
  var item = pmState.breadcrumb[index];
  if (item && item.action) {
    item.action();
  }
}

function formatSize(bytes) {
  if (bytes < 1024) return bytes + 'B';
  if (bytes < 1024*1024) return (bytes/1024).toFixed(1) + 'KB';
  return (bytes/(1024*1024)).toFixed(1) + 'MB';
}

function formatDate(ts) {
  if (!ts) return '';
  var d = new Date(ts * 1000);
  return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0');
}

function showCreateProjectDialog() {
  document.getElementById('new-project-name').value = '';
  document.getElementById('new-project-desc').value = '';
  document.getElementById('new-project-desc').placeholder = t('pmProjectDescPlaceholder');
  // Reset type to dev
  var radios = document.querySelectorAll('input[name="new-project-type"]');
  radios.forEach(function(r){ r.checked = (r.value === 'dev'); });
  document.getElementById('default-module-row').style.display = 'none';
  // Pre-load agents for module dropdown
  loadModuleOptions();
  document.getElementById('create-project-modal').style.display = 'flex';
  document.getElementById('new-project-name').focus();
}

function onProjectTypeChange() {
  var sel = document.querySelector('input[name="new-project-type"]:checked');
  var row = document.getElementById('default-module-row');
  row.style.display = (sel && sel.value === 'hub') ? '' : 'none';
}

async function loadModuleOptions() {
  var select = document.getElementById('new-project-module');
  select.innerHTML = '<option value="">' + t('pmDefaultModuleNone') + '</option>';
  try {
    var r = await fetch('/vizo/console/api/agents');
    var d = await r.json();
    var agents = (Array.isArray(d.data) ? d.data : (d.data && d.data.agents)) || d.agents || [];
    agents.forEach(function(a) {
      var opt = document.createElement('option');
      opt.value = a.id || a.module_id || '';
      opt.textContent = (a.name || a.id || '') + ' (' + (a.id || '') + ')';
      select.appendChild(opt);
    });
  } catch(e) { /* ignore, dropdown will just have "None" */ }
}

function closeCreateProjectDialog() {
  document.getElementById('create-project-modal').style.display = 'none';
}

async function doCreateProject() {
  var name = document.getElementById('new-project-name').value.trim();
  var desc = document.getElementById('new-project-desc').value.trim();
  var typeRadio = document.querySelector('input[name="new-project-type"]:checked');
  var projType = typeRadio ? typeRadio.value : 'dev';
  var defaultModule = '';
  if (projType === 'hub') {
    defaultModule = document.getElementById('new-project-module').value || '';
  }
  if (!name) { showToast(t('pmNameRequired'), 'error'); return; }
  if (!/^[a-zA-Z0-9_-]{1,50}$/.test(name)) {
    showToast(t('pmNameInvalid'), 'error');
    return;
  }
  try {
    var body = {name: name, description: desc};
    if (projType === 'hub') {
      body.type = 'hub';
      if (defaultModule) body.default_module = defaultModule;
    }
    var r = await fetch('/vizo/console/api/projects', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    var d = await r.json();
    if (d.ok) {
      closeCreateProjectDialog();
      showToast(t('pmCreated', {name: name}), 'success');
      await loadPMProjects();
      selectPMProject(name);
    } else {
      showToast(d.error || t('pmCreateFailed'), 'error');
    }
  } catch(e) { showToast(t('networkError'), 'error'); }
}

async function deletePMProject(name) {
  if (!confirm(t('pmDeleteConfirm', {name: name}))) return;
  try {
    var r = await fetch('/vizo/console/api/projects/' + encodeURIComponent(name), {method: 'DELETE'});
    var d = await r.json();
    if (d.ok) {
      showToast(t('pmDeleted'), 'success');
      pmState.selectedProject = null;
      pmState.breadcrumb = [];
      await loadPMProjects();
      document.getElementById('pm-main-area').innerHTML = '<div class="pm-body"><div class="pm-empty">' + t('pmSelectProject') + '</div></div>';
    } else {
      showToast(d.error || t('pmDeleteFailed'), 'error');
    }
  } catch(e) { showToast(t('networkError'), 'error'); }
}

// ======================== Agent Management ========================

var agState = {
  agents: [],
  filter: 'all',
  search: '',
  openMenuId: null,
  currentDetail: null,
  editingId: null,
  previewData: null,
  models: null
};

async function agentApi(url, options) {
  var resp = await fetch(url, Object.assign({
    credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'}
  }, options || {}));
  if (resp.status === 401) {
    window.location.href = '/vizo/console/login';
    return null;
  }
  var data = await resp.json();
  if (!resp.ok) throw new Error(data.error || 'HTTP ' + resp.status);
  return data;
}

function openAgentsView() {
  document.querySelector('.terminal-container').style.display = 'none';
  document.getElementById('input-area').style.display = 'none';
  document.getElementById('project-manager-view').style.display = 'none';
  document.getElementById('model-config-view').style.display = 'none';
  document.getElementById('settings-view').style.display = 'none';
  document.getElementById('agents-view').style.display = 'flex';
  agState.filter = 'all';
  agState.search = '';
  var searchEl = document.getElementById('agents-search');
  if (searchEl) searchEl.value = '';
  loadAgents();
}

function closeAgentsView() {
  document.getElementById('agents-view').style.display = 'none';
  document.querySelector('.terminal-container').style.display = '';
  document.getElementById('input-area').style.display = '';
  closeAgentsDrawer();
  closeAgentsPanel();
  if (S.term && S.fitAddon) {
    try { S.fitAddon.fit(); } catch(e) {}
  }
}

async function loadAgents() {
  try {
    var result = await agentApi('/vizo/console/api/agents');
    if (result && result.success) {
      agState.agents = result.data || [];
      renderAgentsGrid();
    }
  } catch(e) {
    document.getElementById('agents-grid').innerHTML = '<div class="agents-empty">' + t('pmLoadFailed') + '</div>';
  }
}

function renderAgentsGrid() {
  var grid = document.getElementById('agents-grid');
  var list = agState.agents;
  if (agState.filter !== 'all') {
    list = list.filter(function(a) { return a.source === agState.filter; });
  }
  if (agState.search) {
    var q = agState.search.toLowerCase();
    list = list.filter(function(a) {
      return (a.name || '').toLowerCase().indexOf(q) >= 0 ||
             (a.id || '').toLowerCase().indexOf(q) >= 0 ||
             (a.description || '').toLowerCase().indexOf(q) >= 0;
    });
  }
  if (list.length === 0) {
    grid.innerHTML = '<div class="agents-empty">' + t('agNoAgents') + '</div>';
    return;
  }
  grid.innerHTML = list.map(renderAgentCard).join('');
}

function renderAgentCard(agent) {
  var isBuiltin = agent.source === '_builtin';
  var badge = isBuiltin
    ? '<span class="agents-badge agents-badge-builtin">' + t('agBuiltin') + '</span>'
    : '<span class="agents-badge agents-badge-user">' + t('agUser') + '</span>';
  var wfCount = agent.workflow_count || 0;
  var menuItems = '';
  if (!isBuiltin) {
    menuItems += '<button data-action="edit-agent" data-id="' + escHtml(agent.id) + '">' + t('agEdit') + '</button>';
  }
  menuItems += '<button data-action="copy-agent" data-id="' + escHtml(agent.id) + '">' + t('agCopy') + '</button>';
  if (!isBuiltin) {
    menuItems += '<button data-action="delete-agent" data-id="' + escHtml(agent.id) + '" class="agents-menu-danger">' + t('agDelete') + '</button>';
  }
  return '<div class="agents-card" data-action="view-agent" data-id="' + escHtml(agent.id) + '">' +
    badge +
    '<div class="agents-card-icon">' + (agent.icon || '&#129302;') + '</div>' +
    '<div class="agents-card-name">' + escHtml(agent.name || agent.id) + '</div>' +
    '<div class="agents-card-desc">' + escHtml((agent.description || '').substring(0, 80)) + '</div>' +
    '<div class="agents-card-meta">' + wfCount + ' ' + t('agWorkflows') + '</div>' +
    '<div class="agents-card-menu-btn" data-action="toggle-menu" data-id="' + escHtml(agent.id) + '">&#8943;</div>' +
    '<div class="agents-menu" id="agents-menu-' + escHtml(agent.id) + '">' + menuItems + '</div>' +
    '</div>';
}

function toggleAgentMenu(id, e) {
  if (e) { e.stopPropagation(); e.preventDefault(); }
  var menu = document.getElementById('agents-menu-' + id);
  if (!menu) return;
  var wasOpen = menu.classList.contains('open');
  closeAllAgentMenus();
  if (!wasOpen) menu.classList.add('open');
  agState.openMenuId = wasOpen ? null : id;
}

function closeAllAgentMenus() {
  var menus = document.querySelectorAll('.agents-menu.open');
  for (var i = 0; i < menus.length; i++) menus[i].classList.remove('open');
  agState.openMenuId = null;
}

// Detail drawer
function openAgentsDrawer(id) {
  closeAgentsPanel();
  var drawer = document.getElementById('agents-drawer');
  var overlay = document.getElementById('agents-overlay');
  var agent = agState.agents.find(function(a) { return a.id === id; });
  if (!agent) {
    document.getElementById('agents-drawer-content').innerHTML = '<div class="agents-empty">Agent not found</div>';
    drawer.classList.add('open');
    overlay.classList.add('open');
    return;
  }
  agState.currentDetail = agent;
  renderAgentDetail(agent);
  drawer.classList.add('open');
  overlay.classList.add('open');
}

function renderAgentDetail(agent) {
  var isBuiltin = agent.source === '_builtin';
  var actionsHtml = '';
  if (!isBuiltin) {
    actionsHtml += '<button data-action="edit-agent" data-id="' + escHtml(agent.id) + '">' + t('agEdit') + '</button>';
  }
  actionsHtml += '<button data-action="copy-agent" data-id="' + escHtml(agent.id) + '">' + t('agCopy') + '</button>';
  if (!isBuiltin) {
    actionsHtml += '<button data-action="delete-agent" data-id="' + escHtml(agent.id) + '" style="color:var(--red)">' + t('agDelete') + '</button>';
  }

  var wfHtml = '';
  var workflows = agent.workflows || {};
  for (var wfId in workflows) {
    var wf = workflows[wfId];
    var steps = wf.steps || [];
    var stepsHtml = '';
    for (var i = 0; i < steps.length; i++) {
      var s = steps[i];
      stepsHtml += '<div class="agents-step-row">' +
        '<span class="agents-step-num">' + (i + 1) + '</span>' +
        '<span class="agents-step-name">' + escHtml(s.name || s.step || '') + '</span>' +
        '<span class="agents-step-model">' + escHtml(s.model || '') + '</span>' +
        '</div>';
    }
    wfHtml += '<div class="agents-wf-card">' +
      '<div class="agents-wf-name">' + escHtml(wf.name || wfId) + '</div>' +
      '<div class="agents-wf-desc">' + escHtml(wf.description || '') + '</div>' +
      '<div class="agents-wf-steps">' + stepsHtml + '</div>' +
      '</div>';
  }

  document.getElementById('agents-drawer-content').innerHTML =
    '<div class="agents-drawer-header">' +
      '<button class="agents-drawer-close" data-action="close-drawer">&times;</button>' +
      '<span class="agents-drawer-title">' + (agent.icon || '&#129302;') + ' ' + escHtml(agent.name || agent.id) + '</span>' +
      '<div class="agents-drawer-actions">' + actionsHtml + '</div>' +
    '</div>' +
    '<div class="agents-drawer-body">' +
      '<div class="agents-drawer-section">' +
        '<div class="agents-drawer-section-title">' + t('agDescription') + '</div>' +
        '<div class="agents-drawer-info">' + escHtml(agent.description || '') + '</div>' +
      '</div>' +
      '<div class="agents-drawer-section">' +
        '<div class="agents-drawer-section-title">' + t('agVersion') + ' / ' + t('agAuthor') + '</div>' +
        '<div class="agents-drawer-info">v' + escHtml(agent.version || '1.0') + ' &middot; ' + escHtml(agent.author || '-') + '</div>' +
      '</div>' +
      '<div class="agents-drawer-section">' +
        '<div class="agents-drawer-section-title">' + t('agWorkflowList') + '</div>' +
        (wfHtml || '<div class="agents-drawer-info" style="color:var(--text-muted)">-</div>') +
      '</div>' +
    '</div>';
}

function closeAgentsDrawer() {
  document.getElementById('agents-drawer').classList.remove('open');
  document.getElementById('agents-overlay').classList.remove('open');
  agState.currentDetail = null;
}

// Create panel
function openAgentsCreatePanel() {
  closeAgentsDrawer();
  agState.editingId = null;
  agState.previewData = null;
  var panel = document.getElementById('agents-panel');
  renderCreatePanelInput();
  panel.classList.add('open');
}

function renderCreatePanelInput() {
  document.getElementById('agents-panel-content').innerHTML =
    '<div class="agents-panel-header">' +
      '<button class="agents-panel-close" data-action="close-panel">&times;</button>' +
      '<span class="agents-panel-title">' + t('agAiTitle') + '</span>' +
    '</div>' +
    '<div class="agents-panel-body">' +
      '<div class="agents-form-group">' +
        '<div class="agents-form-label" style="margin-bottom:0.5rem">' + t('agAiDesc') + '</div>' +
        '<textarea class="agents-form-textarea" id="agents-ai-input" rows="5" placeholder="' + t('agAiPlaceholder') + '" style="min-height:120px"></textarea>' +
      '</div>' +
    '</div>' +
    '<div class="agents-panel-footer">' +
      '<button class="agents-btn-secondary" data-action="close-panel">' + t('agCancel') + '</button>' +
      '<button class="agents-btn-primary" data-action="generate-agent" id="agents-generate-btn">' + t('agAiGenerate') + '</button>' +
    '</div>';
}

async function generateAgent() {
  var input = document.getElementById('agents-ai-input');
  if (!input) return;
  var desc = input.value.trim();
  if (desc.length < 10) { showToast(t('agAiPlaceholder'), 'error'); return; }
  var btn = document.getElementById('agents-generate-btn');
  if (btn) { btn.disabled = true; btn.textContent = t('agAiGenerating'); }
  document.getElementById('agents-panel-content').innerHTML =
    '<div class="agents-panel-header">' +
      '<button class="agents-panel-close" data-action="close-panel">&times;</button>' +
      '<span class="agents-panel-title">' + t('agAiGenerating') + '</span>' +
    '</div>' +
    '<div class="agents-panel-body"><div class="agents-loading"><div class="agents-loading-spinner"></div>' + t('agAiGenerating') + '</div></div>';
  try {
    var result = await agentApi('/vizo/console/api/agents/generate', {
      method: 'POST',
      body: JSON.stringify({description: desc})
    });
    if (result && result.success && result.data && result.data.module) {
      agState.previewData = result.data.module;
      renderPreviewPanel(result.data.module);
    } else {
      showToast(t('agSaveFailed'), 'error');
      renderCreatePanelInput();
    }
  } catch(e) {
    showToast(e.message, 'error');
    renderCreatePanelInput();
  }
}

function renderPreviewPanel(data) {
  var manifest = data.manifest || {};
  var roles = data.roles || {};
  var wfHtml = '';
  var workflows = manifest.workflows || {};
  for (var wfId in workflows) {
    var wf = workflows[wfId];
    var steps = (wf.stages || wf.steps || []);
    // flatten stages
    var flatSteps = [];
    for (var si = 0; si < steps.length; si++) {
      var st = steps[si];
      if (st.parallel) {
        for (var pi = 0; pi < st.parallel.length; pi++) flatSteps.push(st.parallel[pi]);
      } else {
        flatSteps.push(st);
      }
    }
    var stepsHtml = '';
    for (var i = 0; i < flatSteps.length; i++) {
      var s = flatSteps[i];
      stepsHtml += '<div class="agents-preview-step">' +
        '<span class="agents-step-num">' + (i + 1) + '</span>' +
        '<span class="agents-preview-step-name">' + escHtml(s.name || s.step || '') + '</span>' +
        '<span class="agents-preview-step-model">' + escHtml(s.model || 'sonnet') + '</span>' +
        '</div>';
    }
    wfHtml += '<div class="agents-form-group">' +
      '<div class="agents-form-label">' + escHtml(wf.name || wfId) + '</div>' +
      '<div style="font-size:0.78rem;color:var(--text-muted);margin-bottom:0.4rem">' + escHtml(wf.description || '') + '</div>' +
      stepsHtml +
      '</div>';
  }

  // Collect existing tools from all roles for pre-check
  var allTools = ['Read', 'Write', 'Edit', 'Glob', 'Grep', 'WebSearch', 'WebFetch', 'Bash'];
  var checkedTools = {};
  for (var rn in roles) {
    var rt = roles[rn].tools || [];
    for (var ti = 0; ti < rt.length; ti++) checkedTools[rt[ti]] = true;
  }
  // Also check manifest.roles
  var mRoles = manifest.roles || {};
  for (var rn2 in mRoles) {
    var rt2 = mRoles[rn2].tools || [];
    for (var ti2 = 0; ti2 < rt2.length; ti2++) checkedTools[rt2[ti2]] = true;
  }
  var toolsHtml = '<div class="agents-form-group">' +
    '<label class="agents-form-label">' + t('agToolsLabel') + '</label>' +
    '<div class="agents-tools-grid">';
  for (var tIdx = 0; tIdx < allTools.length; tIdx++) {
    var tn = allTools[tIdx];
    var ck = checkedTools[tn] ? ' checked' : '';
    toolsHtml += '<label class="agents-tool-item"><input type="checkbox" data-tool="' + tn + '"' + ck + '>' + tn + '</label>';
  }
  toolsHtml += '</div><div class="agents-tool-warn" id="agents-bash-warn" style="display:' + (checkedTools['Bash'] ? 'flex' : 'none') + '">' + t('agToolsBashWarn') + '</div></div>';

  document.getElementById('agents-panel-content').innerHTML =
    '<div class="agents-panel-header">' +
      '<button class="agents-panel-close" data-action="close-panel">&times;</button>' +
      '<span class="agents-panel-title">' + t('agPreviewTitle') + '</span>' +
    '</div>' +
    '<div class="agents-panel-body">' +
      '<div class="agents-form-group">' +
        '<label class="agents-form-label">' + t('agModuleId') + '</label>' +
        '<input class="agents-form-input" id="agents-preview-id" value="' + escHtml(manifest.id || '') + '">' +
        '<div class="agents-form-error" id="agents-id-error" style="display:none"></div>' +
      '</div>' +
      '<div class="agents-form-group">' +
        '<label class="agents-form-label">' + t('agModuleName') + '</label>' +
        '<input class="agents-form-input" id="agents-preview-name" value="' + escHtml(manifest.name || '') + '">' +
      '</div>' +
      '<div class="agents-form-group">' +
        '<label class="agents-form-label">' + t('agModuleIcon') + '</label>' +
        '<input class="agents-form-input" id="agents-preview-icon" value="' + escHtml(manifest.icon || '') + '" style="width:60px">' +
      '</div>' +
      '<div class="agents-form-group">' +
        '<label class="agents-form-label">' + t('agModuleDesc') + '</label>' +
        '<textarea class="agents-form-textarea" id="agents-preview-desc" rows="3">' + escHtml(manifest.description || '') + '</textarea>' +
      '</div>' +
      toolsHtml +
      wfHtml +
    '</div>' +
    '<div class="agents-panel-footer">' +
      '<button class="agents-btn-secondary" data-action="back-to-input">' + t('agCancel') + '</button>' +
      '<button class="agents-btn-primary" data-action="save-agent">' + t('agSave') + '</button>' +
    '</div>';

  // ID validation on blur
  var idInput = document.getElementById('agents-preview-id');
  if (idInput) {
    idInput.addEventListener('blur', function() { validateAgentId(this.value.trim()); });
  }
  // Bash warning toggle
  var toolsGrid = document.querySelector('.agents-tools-grid');
  if (toolsGrid) {
    toolsGrid.addEventListener('change', function(e) {
      if (e.target.dataset.tool === 'Bash') {
        var warn = document.getElementById('agents-bash-warn');
        if (warn) warn.style.display = e.target.checked ? 'flex' : 'none';
      }
    });
  }
}

function validateAgentId(id) {
  var errEl = document.getElementById('agents-id-error');
  var inputEl = document.getElementById('agents-preview-id');
  if (!errEl || !inputEl) return true;
  if (!id) { errEl.style.display = 'none'; inputEl.classList.remove('error'); return false; }
  if (!/^[a-zA-Z0-9_]+$/.test(id)) {
    errEl.textContent = t('agIdInvalid');
    errEl.style.display = 'block';
    inputEl.classList.add('error');
    return false;
  }
  var conflict = agState.agents.some(function(a) { return a.id === id; });
  if (conflict && !(agState.editingId && agState.editingId === id)) {
    errEl.textContent = t('agIdConflict');
    errEl.style.display = 'block';
    inputEl.classList.add('error');
    return false;
  }
  errEl.style.display = 'none';
  inputEl.classList.remove('error');
  return true;
}

async function saveAgent() {
  var idEl = document.getElementById('agents-preview-id');
  var nameEl = document.getElementById('agents-preview-name');
  var iconEl = document.getElementById('agents-preview-icon');
  var descEl = document.getElementById('agents-preview-desc');
  if (!idEl || !nameEl) return;
  var moduleId = idEl.value.trim();
  if (!moduleId) { showToast(t('agModuleId'), 'error'); return; }
  var valid = await validateAgentId(moduleId);
  if (!valid) return;

  var manifest, roles;
  if (agState.previewData) {
    manifest = agState.previewData.manifest || {};
    roles = agState.previewData.roles || {};
  } else if (agState.editingId && agState.currentDetail) {
    manifest = JSON.parse(JSON.stringify(agState.currentDetail));
    delete manifest.source;
    roles = agState.currentDetail._roles || {};
  } else {
    return;
  }

  manifest.id = moduleId;
  manifest.name = nameEl.value.trim() || moduleId;
  manifest.icon = iconEl ? iconEl.value.trim() : '';
  manifest.description = descEl ? descEl.value.trim() : '';

  // Collect selected tools from checkboxes
  var selectedTools = [];
  var toolCbs = document.querySelectorAll('.agents-tools-grid input[data-tool]');
  for (var ci = 0; ci < toolCbs.length; ci++) {
    if (toolCbs[ci].checked) selectedTools.push(toolCbs[ci].dataset.tool);
  }
  // Apply to all roles in both roles and manifest.roles
  var applyTools = function(rolesObj) {
    for (var rk in rolesObj) { rolesObj[rk].tools = selectedTools.slice(); }
  };
  if (Object.keys(roles).length) applyTools(roles);
  if (manifest.roles && Object.keys(manifest.roles).length) applyTools(manifest.roles);

  try {
    await agentApi('/vizo/console/api/agents', {
      method: 'POST',
      body: JSON.stringify({manifest: manifest, roles: roles, module_id: moduleId})
    });
    showToast(t('agSaved'), 'success');
    closeAgentsPanel();
    loadAgents();
  } catch(e) {
    showToast(e.message, 'error');
  }
}

function closeAgentsPanel() {
  document.getElementById('agents-panel').classList.remove('open');
  agState.editingId = null;
  agState.previewData = null;
}

// Edit
function openAgentsEditPanel(id) {
  closeAgentsDrawer();
  var agent = agState.agents.find(function(a) { return a.id === id; });
  if (!agent) { showToast('Agent not found', 'error'); return; }
  if (agent.source === '_builtin') {
    showToast(t('agBuiltinNoEdit'), 'error');
    return;
  }
  agState.editingId = id;
  agState.currentDetail = agent;
  agState.previewData = {manifest: JSON.parse(JSON.stringify(agent)), roles: {}};
  var panel = document.getElementById('agents-panel');
  renderPreviewPanel(agState.previewData);
  panel.classList.add('open');
}

// Delete
async function confirmDeleteAgent(id) {
  closeAllAgentMenus();
  var agent = agState.agents.find(function(a) { return a.id === id; });
  if (agent && agent.source === '_builtin') {
    showToast(t('agBuiltinNoDelete'), 'error');
    return;
  }
  var name = agent ? agent.name : id;
  if (!confirm(t('agDeleteConfirm', {name: name}))) return;
  try {
    await agentApi('/vizo/console/api/agents/' + encodeURIComponent(id), {method: 'DELETE'});
    showToast(t('agDeleted'), 'success');
    closeAgentsDrawer();
    loadAgents();
  } catch(e) {
    showToast(e.message || t('agDeleteFailed'), 'error');
  }
}

// Copy
async function copyAgent(id) {
  closeAllAgentMenus();
  var agent = agState.agents.find(function(a) { return a.id === id; });
  if (!agent) { showToast('Agent not found', 'error'); return; }
  var newId = prompt(t('agNewId'), id + '_copy');
  if (!newId) return;
  newId = newId.trim();
  if (!/^[a-zA-Z0-9_]+$/.test(newId)) {
    showToast(t('agIdInvalid'), 'error');
    return;
  }
  if (agState.agents.some(function(a) { return a.id === newId; })) {
    showToast(t('agIdConflict'), 'error');
    return;
  }
  var manifest = JSON.parse(JSON.stringify(agent));
  delete manifest.source;
  delete manifest.workflow_count;
  manifest.id = newId;
  manifest.name = (manifest.name || id) + ' (Copy)';
  try {
    await agentApi('/vizo/console/api/agents', {
      method: 'POST',
      body: JSON.stringify({manifest: manifest, roles: {}, module_id: newId})
    });
    showToast(t('agCopied'), 'success');
    closeAgentsDrawer();
    loadAgents();
  } catch(e) {
    showToast(e.message || t('agCopyFailed'), 'error');
  }
}

// Event delegation for agents-view
(function() {
  var view = document.getElementById('agents-view');
  if (!view) return;
  view.addEventListener('click', function(e) {
    var btn = e.target.closest('[data-action]');
    if (!btn) {
      closeAllAgentMenus();
      return;
    }
    var action = btn.dataset.action;
    var id = btn.dataset.id;
    if (action === 'close-agents') closeAgentsView();
    else if (action === 'open-create-panel') openAgentsCreatePanel();
    else if (action === 'view-agent' && id) {
      if (e.target.closest('.agents-card-menu-btn') || e.target.closest('.agents-menu')) return;
      openAgentsDrawer(id);
    }
    else if (action === 'toggle-menu' && id) toggleAgentMenu(id, e);
    else if (action === 'edit-agent' && id) { closeAllAgentMenus(); openAgentsEditPanel(id); }
    else if (action === 'delete-agent' && id) confirmDeleteAgent(id);
    else if (action === 'copy-agent' && id) copyAgent(id);
    else if (action === 'close-drawer') closeAgentsDrawer();
    else if (action === 'close-panel') closeAgentsPanel();
    else if (action === 'generate-agent') generateAgent();
    else if (action === 'save-agent') saveAgent();
    else if (action === 'back-to-input') renderCreatePanelInput();
    else if (action === 'filter-agents') {
      var filter = btn.dataset.filter;
      agState.filter = filter;
      var tags = view.querySelectorAll('.agents-tag');
      for (var i = 0; i < tags.length; i++) tags[i].classList.remove('active');
      btn.classList.add('active');
      renderAgentsGrid();
    }
  });

  // Search input
  var searchInput = document.getElementById('agents-search');
  if (searchInput) {
    searchInput.addEventListener('input', function() {
      agState.search = this.value.trim();
      renderAgentsGrid();
    });
  }

  // Sidebar button
  var sidebarBtn = document.querySelector('[data-action="open-agents-sidebar"]');
  if (sidebarBtn) {
    sidebarBtn.addEventListener('click', function() { openAgentsView(); });
  }
})();

// ====== Model Config Page ======
var mcState = { roles: [], available: [], reasoning: [], extModels: null, dirty: false };

function openModelConfig() {
  document.querySelector('.terminal-container').style.display = 'none';
  document.getElementById('input-area').style.display = 'none';
  document.getElementById('project-manager-view').style.display = 'none';
  document.getElementById('agents-view').style.display = 'none';
  document.getElementById('settings-view').style.display = 'none';
  document.getElementById('model-config-view').style.display = 'flex';
  loadModelConfig();
}

function closeModelConfig() {
  document.getElementById('model-config-view').style.display = 'none';
  document.querySelector('.terminal-container').style.display = '';
  document.getElementById('input-area').style.display = '';
  if (S.term && S.fitAddon) {
    try { S.fitAddon.fit(); } catch(e) {}
  }
}

async function loadModelConfig() {
  document.getElementById('mc-content').innerHTML = '<div class="mc-loading">加载中...</div>';
  try {
    var results = await Promise.all([
      fetch('/vizo/console/api/settings/models', { credentials: 'include' }).then(function(r) { return r.json(); }),
      fetch('/vizo/console/api/settings/external-models', { credentials: 'include' }).then(function(r) { return r.json(); })
    ]);
    var rolesRes = results[0];
    var extRes = results[1];
    mcState.roles = rolesRes.roles || [];
    mcState.available = rolesRes.available_models || [];
    mcState.reasoning = rolesRes.available_reasoning_efforts || [];
    mcState.extModels = extRes.models || { presets: [], customs: [] };
    mcState.dirty = false;
    renderModelConfig();
  } catch(e) {
    document.getElementById('mc-content').innerHTML =
      '<div class="mc-error">加载失败：' + escHtml(e.message || '网络错误') + '</div>';
  }
}

function renderModelConfig() {
  var html = '';
  // 区域1：角色模型配置
  html += '<div class="mc-section">';
  html += '<h3 class="mc-section-title">系统角色模型配置</h3>';
  html += '<p class="mc-section-desc">为每个角色选择使用的 AI 模型。修改后点击底部保存按钮生效。</p>';
  html += '<div class="mc-table">';
  html += '<div class="mc-table-header"><span>角色</span><span>当前模型</span><span>思考深度</span><span>默认值</span></div>';
  mcState.roles.forEach(function(r) {
    var isDefault = r.current_model === r.default_model && (r.current_reasoning_effort || 'inherit') === (r.default_reasoning_effort || 'inherit');
    var defaultLabel = r.default_model;
    mcState.available.forEach(function(m) {
      if (m.id === r.default_model) defaultLabel = m.display;
    });
    html += '<div class="mc-table-row" data-role="' + escHtml(r.role) + '">';
    html += '<span class="mc-role-name">' + escHtml(r.display_name) + '</span>';
    html += '<span class="mc-role-select"><select class="mc-select" data-role="' + escHtml(r.role) + '">';
    mcState.available.forEach(function(m) {
      var sel = m.id === r.current_model ? ' selected' : '';
      html += '<option value="' + escHtml(m.id) + '"' + sel + '>' + escHtml(m.display) + '</option>';
    });
    html += '</select></span>';
    html += '<span class="mc-role-select"><select class="mc-select mc-reasoning-select" data-role="' + escHtml(r.role) + '">';
    mcState.reasoning.forEach(function(effort) {
      var sel = effort.id === (r.current_reasoning_effort || 'inherit') ? ' selected' : '';
      html += '<option value="' + escHtml(effort.id) + '"' + sel + '>' + escHtml(effort.display || effort.id) + '</option>';
    });
    html += '</select></span>';
    html += '<span class="mc-default">' + escHtml(defaultLabel) + (isDefault ? '' : ' <span class="mc-modified">已修改</span>') + '</span>';
    html += '</div>';
  });
  html += '</div>';
  html += '<div class="mc-actions">';
  html += '<button class="mc-reset-btn" data-action="mc-reset">恢复所有默认值</button>';
  html += '<button class="mc-save-btn" data-action="mc-save">保存配置</button>';
  html += '</div>';
  html += '</div>';
  // 区域2：外部模型配置状态（只读）
  html += renderExternalModels();
  document.getElementById('mc-content').innerHTML = html;
}

function renderExternalModels() {
  var ext = mcState.extModels;
  if (!ext) return '';
  var html = '<div class="mc-section">';
  html += '<h3 class="mc-section-title">外部模型</h3>';
  html += '<p class="mc-section-desc">已配置的外部模型可在上方角色配置中选择使用。</p>';
  var all = (ext.presets || []).concat(ext.customs || []);
  if (all.length === 0) {
    html += '<div class="mc-empty">暂无外部模型配置</div>';
  } else {
    all.forEach(function(m) {
      var statusText = m.configured ? '✅ 已配置' : '❌ 未配置';
      var statusClass = m.configured ? 'mc-configured' : 'mc-not-configured';
      html += '<div class="mc-ext-card">';
      html += '<span class="mc-ext-name">' + escHtml(m.display || m.id) + '</span>';
      html += '<span class="mc-ext-status ' + statusClass + '">' + statusText + '</span>';
      if (m.api_key_masked) {
        html += '<span class="mc-ext-key">Key: ' + escHtml(m.api_key_masked) + '</span>';
      }
      html += '</div>';
    });
  }
  html += '</div>';
  return html;
}

async function saveMcModelConfig() {
  var overrides = {};
  document.querySelectorAll('.mc-select:not(.mc-reasoning-select)').forEach(function(sel) {
    overrides[sel.dataset.role] = sel.value;
  });
  var role_reasoning_efforts = {};
  document.querySelectorAll('.mc-reasoning-select').forEach(function(sel) {
    role_reasoning_efforts[sel.dataset.role] = sel.value;
  });
  var btn = document.querySelector('[data-action="mc-save"]');
  if (btn) { btn.disabled = true; btn.textContent = '保存中...'; }
  try {
    var res = await fetch('/vizo/console/api/settings/models', {
      method: 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model_overrides: overrides, role_reasoning_efforts: role_reasoning_efforts })
    });
    var data = await res.json();
    if (data.success) {
      showToast('模型配置已保存', 'success');
      mcState.dirty = false;
      loadModelConfig();
    } else {
      showToast('保存失败：' + (data.error || '未知错误'), 'error');
    }
  } catch(e) {
    showToast('网络错误', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '保存配置'; }
  }
}

function resetModelConfig() {
  mcState.roles.forEach(function(r) {
    var sel = document.querySelector('.mc-select:not(.mc-reasoning-select)[data-role="' + r.role + '"]');
    if (sel) sel.value = r.default_model;
    var effortSel = document.querySelector('.mc-reasoning-select[data-role="' + r.role + '"]');
    if (effortSel) effortSel.value = r.default_reasoning_effort || 'inherit';
  });
  mcState.dirty = true;
  showToast('已恢复默认值，点击"保存配置"生效', 'info');
}

// Model config event delegation
(function() {
  document.addEventListener('click', function(e) {
    var target = e.target.closest('[data-action]');
    if (!target) return;
    var action = target.dataset.action;
    if (action === 'mc-save') saveMcModelConfig();
    else if (action === 'mc-reset') resetModelConfig();
    else if (action === 'close-model-config') closeModelConfig();
    else if (action === 'open-model-config') openModelConfig();
  });
})();

// ====== Markdown Preview Drawer ======
function openMdPreview(taskId, filename, title) {
  var drawer = document.getElementById('md-preview-drawer');
  var overlay = document.getElementById('md-preview-overlay');
  var iframe = document.getElementById('md-preview-iframe');
  var loading = document.getElementById('md-preview-loading');
  var errEl = document.getElementById('md-preview-error');
  var titleEl = document.getElementById('md-preview-title');

  titleEl.textContent = title || filename;
  loading.style.display = 'flex';
  errEl.style.display = 'none';
  iframe.style.opacity = '0';
  iframe.src = '';

  drawer.style.display = 'flex';
  overlay.style.display = 'block';

  var url = '/vizo/api/tasks/' + encodeURIComponent(taskId) +
            '/outputs/' + encodeURIComponent(filename);
  fetch(url, {method: 'HEAD', credentials: 'same-origin'}).then(function(resp) {
    if (!resp.ok) throw new Error(resp.status);
    iframe.src = url;
    iframe.onload = function() {
      loading.style.display = 'none';
      iframe.style.opacity = '1';
    };
    iframe.onerror = function() {
      loading.style.display = 'none';
      errEl.style.display = 'flex';
    };
  }).catch(function() {
    loading.style.display = 'none';
    errEl.style.display = 'flex';
  });
}

function closeMdPreview() {
  document.getElementById('md-preview-drawer').style.display = 'none';
  document.getElementById('md-preview-overlay').style.display = 'none';
  document.getElementById('md-preview-iframe').src = '';
}

// 事件委托：md-preview-link 点击 + overlay/close 按钮
document.addEventListener('click', function(e) {
  var link = e.target.closest('.md-preview-link');
  if (link) {
    e.preventDefault();
    var taskId = link.dataset.taskId;
    var filename = link.dataset.filename;
    var title = link.dataset.title;
    if (taskId && filename) openMdPreview(taskId, filename, title);
    return;
  }
  if (e.target.id === 'md-preview-overlay' || e.target.id === 'md-preview-close-btn') {
    closeMdPreview();
  }
});

</script>
<!-- Markdown Preview Drawer -->
<div id="md-preview-overlay" class="md-preview-overlay" style="display:none"></div>
<div id="md-preview-drawer" class="md-preview-drawer" style="display:none">
  <div class="md-preview-header">
    <span class="md-preview-title" id="md-preview-title"></span>
    <button class="md-preview-close" id="md-preview-close-btn">&times;</button>
  </div>
  <div class="md-preview-body">
    <iframe id="md-preview-iframe" class="md-preview-iframe"></iframe>
    <div id="md-preview-loading" class="md-preview-loading">\u52A0\u8F7D\u4E2D...</div>
    <div id="md-preview-error" class="md-preview-error" style="display:none">\u6587\u4EF6\u52A0\u8F7D\u5931\u8D25</div>
  </div>
</div>
</body>
</html>"""


class WebConsoleHandler:
    """Handles all Web Console HTTP and WebSocket requests."""

    def __init__(self, pty_manager, config: dict, redis_getter: Callable,
                 password_manager=None):
        self._pty = pty_manager
        self._config = config
        self._get_redis = redis_getter
        self._main_sessions = MainSessionController(config, project_root=_PROJECT_ROOT)
        self._token = config.get("token", "")
        self._token_hash = hashlib.sha256(self._token.encode()).hexdigest() if self._token else ""
        self._cookie_max_age = config.get("cookie_max_age_days", 30) * 86400
        self._password_mgr = password_manager
        self._connection_auth_jobs: dict[str, dict] = {}

    @staticmethod
    def _runtime_error_payload(error: Exception, *, fallback_status: int = 400) -> tuple[dict, int]:
        error_code = str(getattr(error, "error_code", "") or "runtime_error")
        status = fallback_status
        if error_code in {"session_not_found", "pending_input_not_found"}:
            status = 404
        elif error_code == "codex_auth_required":
            status = 401
        elif error_code in {"session_busy", "runtime_mismatch", "interaction_not_pending", "interaction_stale"}:
            status = 409
        elif error_code == "task_resource_blocked":
            status = 503
        elif error_code in {
            "runtime_blocked",
            "cross_family_switch_forbidden",
            "model_not_available",
            "interaction_action_invalid",
            "interaction_text_required",
            "pending_input_action_invalid",
            "pending_input_empty",
        }:
            status = 400
        elif error_code in {"runtime_missing", "interactive_request_unavailable"}:
            status = 412
        return (
            {
                "error": error_code,
                "message": str(error),
                "details": getattr(error, "runtime_metadata", {}) or {},
            },
            status,
        )

    def _check_auth(self, request) -> bool:
        """Verify authentication cookie (bcrypt priority, token fallback)."""
        cookie_val = request.cookies.get("vizo_web_token", "")
        if not cookie_val:
            return False
        # bcrypt 优先
        if self._password_mgr and self._password_mgr.has_password():
            return cookie_val == self._password_mgr.get_cookie_value()
        # 降级: 原有 token 验证
        return cookie_val == self._token_hash and bool(self._token_hash)

    def _resolve_active_session_target_model(self) -> tuple[bool, str, str]:
        """Resolve the safe Claude alias to apply inside the active session."""
        try:
            from pathlib import Path as _Path
            from lib.settings_handler import MAIN_SESSION_DEFAULT_TIER, VALID_MODEL_IDS

            target_model = MAIN_SESSION_DEFAULT_TIER
            settings_path = _Path.home() / ".claude" / "settings.json"
            if settings_path.exists():
                try:
                    settings_data = json.loads(settings_path.read_text(encoding="utf-8"))
                    configured_model = str(settings_data.get("model", "") or "").strip()
                    if configured_model in VALID_MODEL_IDS:
                        target_model = configured_model
                except Exception:
                    pass
        except Exception as e:
            logger.warning("Resolve active session target model failed: %s", e)
            return False, "", "resolve_failed"
        if not target_model:
            return False, "", "empty_target_model"
        return True, target_model, ""

    def _sync_active_session_model(self, session_id: str, resolved_connection: dict | None) -> dict:
        """将当前活跃 PTY 会话切换到安全 alias，避免残留旧 provider 的模型 ID。"""
        result = {"attempted": False, "applied": False}
        session_id = str(session_id or "").strip()
        if not session_id:
            return result
        session = self._pty.get_session(session_id)
        if not session or getattr(session, "status", "") == "stopped":
            result["attempted"] = True
            result["reason"] = "session_not_found"
            return result
        ok, target_model, reason = self._resolve_active_session_target_model()
        result["attempted"] = True
        if not ok:
            result["reason"] = reason
            return result
        result["target_model"] = target_model
        # Claude Code 会话内切模应走 alias（sonnet/opus/haiku），而不是底层真实模型 ID。
        self._pty.write_input(session, f"/model {target_model}\r")
        result["applied"] = True
        result["mode"] = "inplace"
        return result

    async def _apply_active_session_model(self, session_id: str, resolved_connection: dict | None) -> dict:
        """优先通过 Claude 原生 resume/fork 迁移活跃会话，失败时回退到会话内 /model。"""
        session_id = str(session_id or "").strip()
        result = {"attempted": False, "applied": False}
        if not session_id:
            return result

        ok, target_model, reason = self._resolve_active_session_target_model()
        if not ok:
            return {"attempted": True, "applied": False, "reason": reason}

        replace_session = getattr(self._pty, "replace_session", None)
        if callable(replace_session):
            try:
                migration = await replace_session(session_id, target_model=target_model)
            except Exception as e:
                logger.warning("Replace active session model failed: session=%s, %s", session_id, e)
                migration = {"attempted": True, "applied": False, "reason": "replace_failed"}
            migration.setdefault("attempted", True)
            migration.setdefault("target_model", target_model)
            if migration.get("applied"):
                return migration

        sync_result = self._sync_active_session_model(session_id, resolved_connection)
        sync_result.setdefault("target_model", target_model)
        return sync_result

    def _resolve_runtime_context(self, session_id: str | None) -> dict:
        session_id = str(session_id or "").strip()
        if not session_id:
            return {}
        try:
            session = self._main_sessions.get_session(session_id)
        except Exception as e:
            logger.warning("Resolve runtime context failed: session=%s, %s", session_id, e)
            return {}
        if not session:
            return {}
        return {
            "session_id": session_id,
            "runtime_family": str(getattr(session, "runtime_family", "") or ""),
            "display_model": str(getattr(session, "display_model", "") or ""),
        }

    def _build_runtime_diagnostics_payload(
        self,
        *,
        config: dict,
        payload: dict,
        active_session_id: str = "",
        probe_scope: str = "all",
    ) -> dict:
        from lib.settings_handler import resolve_current_main_session_api_key

        runtime_ctx = self._resolve_runtime_context(active_session_id)
        connection_payload = dict(payload or {})
        api_key = str(connection_payload.get("api_key", "") or "").strip()
        if api_key == "__USE_STORED__":
            api_key = resolve_current_main_session_api_key()
        connection_payload["api_key"] = api_key
        if not connection_payload.get("connection_id"):
            connection_payload["connection_id"] = (
                str(connection_payload.get("id") or "")
                or str(
                    config.get("external_models", {})
                    .get("anthropic", {})
                    .get("active_saved_connection_id", "")
                    or "main_session"
                )
            )
        return build_runtime_diagnostics_snapshot(
            config=config,
            connection=connection_payload,
            display_model=str(
                connection_payload.get("display_model")
                or runtime_ctx.get("display_model")
                or ""
            ),
            current_runtime_family=runtime_ctx.get("runtime_family"),
            probe_scope=probe_scope,
        )

    @staticmethod
    def _clean_connection_auth_output(text: str) -> str:
        cleaned = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(text or ""))
        cleaned = cleaned.replace("\r", "")
        return cleaned.strip()

    def _resolve_saved_connection_auth_target(self, connection_id: str) -> dict:
        from lib.settings_handler import ConnectionProfileManager

        connection = ConnectionProfileManager().get_by_id(connection_id)
        return connection

    @staticmethod
    def _resolve_connection_codex_home_path(connection: dict) -> Path:
        codex_home = Path(str(connection.get("codex_home") or "").strip() or ".vizo/codex/connections/main_session")
        if not codex_home.is_absolute():
            codex_home = (_PROJECT_ROOT / codex_home).resolve()
        return codex_home

    @staticmethod
    def _extract_connection_auth_device_info(output: str) -> tuple[str, str]:
        cleaned = WebConsoleHandler._clean_connection_auth_output(output)
        url_match = re.search(r"https://\S+", cleaned)
        code_match = re.search(r"\b[A-Z0-9]{4,}-[A-Z0-9]{4,}\b", cleaned)
        return (
            str(url_match.group(0) if url_match else ""),
            str(code_match.group(0) if code_match else ""),
        )

    @staticmethod
    def _extract_connection_auth_label(status_output: str) -> str:
        cleaned = WebConsoleHandler._clean_connection_auth_output(status_output)
        match = re.search(r"Logged in using\s+(.+)", cleaned)
        return str(match.group(1).strip() if match else "")

    @staticmethod
    def _connection_auth_timestamp() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _build_connection_auth_env(codex_home: Path) -> dict[str, str]:
        env = dict(os.environ)
        for key in list(env.keys()):
            if key.startswith("CODEX_") or key.startswith("ANTHROPIC_"):
                env.pop(key, None)
        env.pop("OPENAI_API_KEY", None)
        env["CODEX_HOME"] = str(codex_home)
        return env

    async def _run_connection_auth_command(self, codex_home: Path, *args: str) -> tuple[int, str]:
        env = self._build_connection_auth_env(codex_home)
        proc = await asyncio.create_subprocess_exec(
            "codex",
            *args,
            cwd=str(_PROJECT_ROOT),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        output = (stdout or b"").decode("utf-8", errors="replace")
        err = (stderr or b"").decode("utf-8", errors="replace")
        combined = output
        if err:
            combined = f"{combined}\n{err}".strip()
        return int(proc.returncode or 0), self._clean_connection_auth_output(combined)

    async def _read_connection_auth_stream(self, connection_id: str, stream) -> None:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            job = self._connection_auth_jobs.get(connection_id)
            if job is None:
                return
            job["output"] = str(job.get("output", "")) + chunk.decode("utf-8", errors="replace")
            device_url, device_code = self._extract_connection_auth_device_info(job["output"])
            if device_url:
                job["device_url"] = device_url
            if device_code:
                job["device_code"] = device_code

    async def _wait_connection_auth_process(self, connection_id: str, process) -> None:
        returncode = await process.wait()
        job = self._connection_auth_jobs.get(connection_id)
        if job is None:
            return
        job["returncode"] = int(returncode)
        job["completed"] = True

    async def _terminate_connection_auth_job(self, connection_id: str) -> None:
        job = self._connection_auth_jobs.get(connection_id)
        if not job:
            return
        process = job.get("process")
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        for task_name in ("stdout_task", "stderr_task", "wait_task"):
            task = job.get(task_name)
            if task:
                task.cancel()
        self._connection_auth_jobs.pop(connection_id, None)

    async def _persist_connection_auth_state(
        self,
        connection_id: str,
        *,
        auth_status: str,
        auth_last_verified_at: str = "",
        auth_account_label: str = "",
    ) -> dict | None:
        from lib.settings_handler import ConnectionProfileManager

        return ConnectionProfileManager().update_auth_state(
            connection_id,
            auth_status=auth_status,
            auth_last_verified_at=auth_last_verified_at,
            auth_account_label=auth_account_label,
        )

    async def _probe_connection_auth_status(self, connection: dict) -> dict[str, str]:
        codex_home = self._resolve_connection_codex_home_path(connection)
        returncode, status_output = await self._run_connection_auth_command(codex_home, "login", "status")
        auth_status = "unknown"
        auth_label = ""
        if returncode == 0 and "Logged in using" in status_output:
            auth_status = "ready"
            auth_label = self._extract_connection_auth_label(status_output)
        elif "Not logged in" in status_output:
            auth_status = "missing"
        return {
            "auth_status": auth_status,
            "status_output": status_output,
            "auth_account_label": auth_label,
        }

    async def _serialize_connection_auth_payload(self, connection: dict) -> dict:
        connection_id = str(connection.get("id") or "")
        status_probe = await self._probe_connection_auth_status(connection)
        auth_status = status_probe["auth_status"]
        auth_account_label = status_probe["auth_account_label"]
        auth_last_verified_at = ""
        login_state = "idle"
        login_error = ""
        job = self._connection_auth_jobs.get(connection_id)
        if auth_status == "ready":
            auth_last_verified_at = self._connection_auth_timestamp()
            await self._persist_connection_auth_state(
                connection_id,
                auth_status="ready",
                auth_last_verified_at=auth_last_verified_at,
                auth_account_label=auth_account_label,
            )
            if job:
                await self._terminate_connection_auth_job(connection_id)
            login_state = "ready"
        elif job:
            login_state = "waiting"
            if job.get("completed") and job.get("returncode") not in (None, 0):
                login_state = "cancelled" if job.get("returncode") == -15 else "error"
                login_error = self._clean_connection_auth_output(job.get("output", ""))
            await self._persist_connection_auth_state(connection_id, auth_status="unknown")
            auth_status = "unknown"
        else:
            await self._persist_connection_auth_state(connection_id, auth_status="missing")
            auth_status = "missing"

        updated = self._resolve_saved_connection_auth_target(connection_id)
        payload = {
            "connection_id": connection_id,
            "auth_mode": str(updated.get("auth_mode") or ""),
            "auth_status": auth_status,
            "auth_last_verified_at": auth_last_verified_at or str(updated.get("auth_last_verified_at") or ""),
            "auth_account_label": auth_account_label or str(updated.get("auth_account_label") or ""),
            "codex_home": str(updated.get("codex_home") or ""),
            "account_login_supported": bool(updated.get("account_login_supported")),
            "login_state": login_state,
            "status_message": status_probe["status_output"],
        }
        if job:
            payload["device_url"] = str(job.get("device_url") or "")
            payload["device_code"] = str(job.get("device_code") or "")
            payload["login_method"] = str(job.get("login_method") or "")
        if login_error:
            payload["error"] = login_error
        return payload

    # -------------------- Login --------------------

    async def handle_login_page(self, request):
        """GET /vizo/console/login — render login page."""
        html = LOGIN_HTML.replace("{{error_display}}", "none").replace("{{error_message}}", "")
        return web.Response(text=html, content_type="text/html")

    async def handle_login(self, request):
        """POST /vizo/console/login — verify token and set cookie."""
        data = await request.post()
        password = data.get("token", "")  # 表单字段名保持 "token"

        # bcrypt 优先
        if self._password_mgr and self._password_mgr.has_password():
            if self._password_mgr.verify_password(password):
                resp = web.HTTPSeeOther("/vizo/console")
                resp.set_cookie(
                    "vizo_web_token",
                    self._password_mgr.get_cookie_value(),
                    httponly=True,
                    path="/vizo",
                    max_age=self._cookie_max_age,
                    samesite="Lax",
                )
                return resp
            html = LOGIN_HTML.replace("{{error_display}}", "block").replace(
                "{{error_message}}", "密码错误，请重试")
            return web.Response(text=html, content_type="text/html")

        # 降级: 原有 token 验证
        if password == self._token and self._token:
            resp = web.HTTPSeeOther("/vizo/console")
            resp.set_cookie(
                "vizo_web_token",
                self._token_hash,
                httponly=True,
                path="/vizo",
                max_age=self._cookie_max_age,
                samesite="Lax",
            )
            return resp

        html = LOGIN_HTML.replace("{{error_display}}", "block").replace(
            "{{error_message}}", "Invalid token, please try again"
        )
        return web.Response(text=html, content_type="text/html")

    # -------------------- Settings API --------------------

    async def handle_settings_password(self, request):
        """POST /vizo/console/api/settings/password — 修改密码"""
        if not self._check_auth(request):
            return web.json_response({"error": "未授权"}, status=401)
        if not self._password_mgr:
            return web.json_response({"error": "密码管理不可用"}, status=500)

        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "请求格式错误"}, status=400)

        old_password = data.get("old_password", "")
        new_password = data.get("new_password", "")
        if not old_password or not new_password:
            return web.json_response({"error": "请填写完整信息"}, status=400)
        if len(new_password) < 8:
            return web.json_response({"error": "新密码至少 8 位"}, status=400)

        # 验证旧密码
        if self._password_mgr.has_password():
            if not self._password_mgr.verify_password(old_password):
                return web.json_response({"error": "旧密码不正确"}, status=400)
        else:
            # 降级: 验证 config token
            if old_password != self._token or not self._token:
                return web.json_response({"error": "旧密码不正确"}, status=400)

        # 写入新密码（asyncio.Lock 保护并发）
        try:
            async with self._password_mgr._lock:
                self._password_mgr.set_password(new_password)
        except RuntimeError as e:
            return web.json_response({"error": str(e)}, status=500)

        return web.json_response({"success": True})

    async def handle_settings_models_get(self, request):
        """GET /vizo/console/api/settings/models — 读取模型配置"""
        if not self._check_auth(request):
            return web.json_response({"error": "未授权"}, status=401)
        from lib.config_loader import load_config
        from lib.settings_handler import ModelConfigManager
        config = load_config()
        mgr = ModelConfigManager()
        return web.json_response(mgr.get_models(config))

    async def handle_settings_models_post(self, request):
        """POST /vizo/console/api/settings/models — 修改模型配置"""
        if not self._check_auth(request):
            return web.json_response({"error": "未授权"}, status=401)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "请求格式错误"}, status=400)

        overrides = data.get("model_overrides")
        if not isinstance(overrides, dict):
            return web.json_response({"error": "缺少 model_overrides"}, status=400)

        from lib.settings_handler import ModelConfigManager
        mgr = ModelConfigManager()
        role_reasoning_efforts = data.get("role_reasoning_efforts")
        err = mgr.update_models(overrides, role_reasoning_efforts=role_reasoning_efforts)
        if err:
            return web.json_response({"success": False, "error": err})
        return web.json_response({"success": True})

    async def handle_settings_apikey_get(self, request):
        """GET /vizo/console/api/settings/apikey — 读取 API Key + Base URL 状态"""
        if not self._check_auth(request):
            return web.json_response({"error": "未授权"}, status=401)
        from lib.config_loader import is_placeholder
        from lib.openai_compat_bridge import is_openai_bridge_base_url
        from lib.settings_handler import (
            extract_managed_main_session_env,
            get_main_session_provider_options,
            resolve_current_main_session_api_key,
            resolve_main_session_connection,
            sync_main_session_to_claude_settings,
            upgrade_legacy_openai_defaults_in_config,
            validate_main_session_base_url_for_access_mode,
            write_config_data,
        )
        import json as _json
        from pathlib import Path as _Path

        from lib.config_loader import load_config
        cfg = load_config()
        migrated, migrated_resolved = upgrade_legacy_openai_defaults_in_config(cfg)
        if migrated:
            write_config_data(cfg)
            cfg = load_config(force_reload=True)
        anthropic_cfg = cfg.get("external_models", {}).get("anthropic", {})
        stored_provider_id = anthropic_cfg.get("provider_id", "")
        if migrated:
            sync_main_session_to_claude_settings(
                api_key=str(anthropic_cfg.get("api_key", "") or ""),
                base_url=str(anthropic_cfg.get("base_url", "") or ""),
                extra_env=anthropic_cfg.get("env", {}) or {},
                resolved_connection=migrated_resolved,
            )

        # 设置页展示“默认主连接配置”，不再把 ~/.claude/settings.json 当成主事实源。
        api_key = str(anthropic_cfg.get("api_key", "") or "").strip()
        base_url = str(anthropic_cfg.get("base_url", "") or "").strip()
        managed_env = extract_managed_main_session_env(anthropic_cfg.get("env", {}))

        settings_path = _Path.home() / ".claude" / "settings.json"
        if (not base_url or not managed_env) and settings_path.exists():
            try:
                s = _json.loads(settings_path.read_text(encoding="utf-8"))
                env = s.get("env", {})
                settings_base_url = env.get("ANTHROPIC_BASE_URL", "")
                settings_env = extract_managed_main_session_env(env)
                if not base_url and not is_openai_bridge_base_url(settings_base_url):
                    base_url = settings_base_url
                if not managed_env:
                    managed_env = settings_env
            except Exception:
                pass

        if not api_key or is_placeholder(api_key):
            fallback_key = resolve_current_main_session_api_key()
            if fallback_key and not is_placeholder(fallback_key):
                api_key = fallback_key

        resolved = resolve_main_session_connection(
            base_url,
            current_env=managed_env,
            stored_provider_id=stored_provider_id,
        )
        model_fields = resolved["model_fields"]
        runtime_diagnostics = self._build_runtime_diagnostics_payload(
            config=cfg,
            payload={
                "connection_id": str(anthropic_cfg.get("active_saved_connection_id", "") or "main_session"),
                "base_url": resolved["base_url"],
                "provider_id": resolved["provider_id"],
                "env": resolved["env"],
                "api_key": api_key,
                "auth_mode": resolved.get("auth_mode", ""),
                "auth_status": resolved.get("auth_status", ""),
                "auth_last_verified_at": resolved.get("auth_last_verified_at", ""),
                "auth_account_label": resolved.get("auth_account_label", ""),
                "codex_home": resolved.get("codex_home", ""),
            },
            probe_scope="all",
        )

        configured = (
            resolved.get("auth_mode") != "account_login"
            and bool(api_key)
            and not is_placeholder(api_key)
        )
        masked = ""
        if configured and len(api_key) > 12:
            masked = api_key[:7] + "..." + api_key[-4:]
        return web.json_response({
            "configured": configured,
            "masked": masked,
            "connection_id": str(anthropic_cfg.get("active_saved_connection_id", "") or "main_session"),
            "base_url": resolved["base_url"],
            "base_url_warning": validate_main_session_base_url_for_access_mode(
                resolved["base_url"], resolved["access_mode"]
            ),
            "routing_provider": resolved["provider_display"],
            "routing_provider_id": resolved["provider_id"],
            "routing_access_mode": resolved["access_mode"],
            "routing_provider_family": resolved["provider_family"],
            "routing_summary": resolved["routing_summary"],
            "routing_models": resolved["routing_models"],
            "auth_mode": resolved.get("auth_mode", ""),
            "auth_status": resolved.get("auth_status", ""),
            "auth_last_verified_at": resolved.get("auth_last_verified_at", ""),
            "auth_account_label": resolved.get("auth_account_label", ""),
            "codex_home": resolved.get("codex_home", ""),
            "account_login_supported": bool(resolved.get("account_login_supported")),
            "provider_options": get_main_session_provider_options(),
            "runtime_diagnostics": runtime_diagnostics,
            **model_fields,
        })

    async def handle_runtime_diagnostics(self, request):
        """GET/POST /vizo/console/api/runtime/diagnostics — 返回 runtime 可用性与当前策略。"""
        if not self._check_auth(request):
            return web.json_response({"error": "未授权"}, status=401)
        from lib.config_loader import load_config
        from lib.openai_compat_bridge import is_openai_bridge_base_url
        from lib.settings_handler import extract_managed_main_session_env
        import json as _json
        from pathlib import Path as _Path

        cfg = load_config(force_reload=True)
        if request.method == "POST":
            try:
                data = await request.json()
            except Exception:
                return web.json_response({"error": "请求格式错误"}, status=400)
            diagnostics = self._build_runtime_diagnostics_payload(
                config=cfg,
                payload=data,
                active_session_id=str(data.get("active_session_id", "") or ""),
                probe_scope=str(data.get("probe_scope", "all") or "all"),
            )
            return web.json_response(diagnostics)

        anthropic_cfg = cfg.get("external_models", {}).get("anthropic", {})
        api_key = str(anthropic_cfg.get("api_key", "") or "").strip()
        base_url = str(anthropic_cfg.get("base_url", "") or "").strip()
        managed_env = extract_managed_main_session_env(anthropic_cfg.get("env", {}))
        settings_path = _Path.home() / ".claude" / "settings.json"
        if (not base_url or not managed_env) and settings_path.exists():
            try:
                settings_data = _json.loads(settings_path.read_text(encoding="utf-8"))
                env = settings_data.get("env", {}) or {}
                settings_base_url = env.get("ANTHROPIC_BASE_URL", "")
                settings_env = extract_managed_main_session_env(env)
                if not base_url and not is_openai_bridge_base_url(settings_base_url):
                    base_url = settings_base_url
                if not managed_env:
                    managed_env = settings_env
            except Exception:
                pass
        if not api_key:
            from lib.settings_handler import resolve_current_main_session_api_key

            api_key = str(resolve_current_main_session_api_key() or "")

        diagnostics = self._build_runtime_diagnostics_payload(
            config=cfg,
            payload={
                "connection_id": str(anthropic_cfg.get("active_saved_connection_id", "") or "main_session"),
                "base_url": base_url,
                "provider_id": anthropic_cfg.get("provider_id", ""),
                "env": managed_env,
                "api_key": api_key,
                "auth_mode": anthropic_cfg.get("auth_mode", ""),
                "auth_status": anthropic_cfg.get("auth_status", ""),
                "auth_last_verified_at": anthropic_cfg.get("auth_last_verified_at", ""),
                "auth_account_label": anthropic_cfg.get("auth_account_label", ""),
                "codex_home": anthropic_cfg.get("codex_home", ""),
            },
            active_session_id=str(request.query.get("active_session_id", "") or ""),
            probe_scope=str(request.query.get("probe_scope", "all") or "all"),
        )
        return web.json_response(diagnostics)

    async def handle_settings_apikey_post(self, request):
        """POST /vizo/console/api/settings/apikey — 修改 API Key / Base URL"""
        if not self._check_auth(request):
            return web.json_response({"error": "未授权"}, status=401)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "请求格式错误"}, status=400)
        api_key = data.get("api_key", "").strip()
        base_url = data.get("base_url", "").strip()
        base_url_present = "base_url" in data
        from lib.settings_handler import (
            MAIN_SESSION_MANAGED_ENV_KEYS,
            MAIN_SESSION_MODEL_ENV_MAP,
            is_openai_access_mode,
            resolve_current_main_session_api_key,
            resolve_main_session_connection,
            _apply_openai_auth_fields,
            validate_main_session_connection_requirements,
            validate_main_session_base_url_for_access_mode,
        )
        if api_key == "__USE_STORED__":
            api_key = resolve_current_main_session_api_key()
        model_keys_present = any(k in data for k in MAIN_SESSION_MODEL_ENV_MAP)
        if not api_key and not base_url_present and not model_keys_present:
            return web.json_response({"success": False, "error": "请至少填写一项"})
        # 写入 .env 持久化并刷新运行时缓存
        from lib.config_loader import load_config, write_env_file
        if api_key:
            write_env_file({
                "OPUS_MAIN_API_KEY": api_key,
                "ANTHROPIC_API_KEY": "",
            })
        config = load_config(force_reload=True)
        current_base_url = config.get("external_models", {}).get("anthropic", {}).get("base_url", "")
        current_provider_id = config.get("external_models", {}).get("anthropic", {}).get("provider_id")
        effective_base_url = base_url if base_url_present else current_base_url
        resolved = resolve_main_session_connection(
            effective_base_url,
            values=data,
            stored_provider_id=current_provider_id,
        )
        base_url_error = validate_main_session_base_url_for_access_mode(
            resolved["base_url"], resolved["access_mode"]
        )
        if base_url_error:
            return web.json_response({"success": False, "error": base_url_error})
        selection_error = validate_main_session_connection_requirements(resolved)
        if selection_error:
            return web.json_response({"success": False, "error": selection_error})
        if (resolved["provider_id"] == "gateway" or is_openai_access_mode(resolved["access_mode"])) and not resolved["base_url"]:
            msg = "OpenAPI 必须填写 Base URL" if is_openai_access_mode(resolved["access_mode"]) else "Anthropic 兼容接口必须填写 Base URL"
            return web.json_response({"success": False, "error": msg})
        # 同步到 config.json（Docker 部署时 .env 不持久化，config.json 通过卷挂载持久化）
        import json as _json
        from lib.paths import CONFIG_FILE
        effective_api_key = api_key
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config_data = _json.load(f)
            if "external_models" not in config_data:
                config_data["external_models"] = {}
            if "anthropic" not in config_data["external_models"]:
                config_data["external_models"]["anthropic"] = {}
            anthropic_cfg = config_data["external_models"]["anthropic"]
            if api_key:
                anthropic_cfg["api_key"] = api_key
            elif str(resolved.get("auth_mode") or "") == "account_login":
                anthropic_cfg.pop("api_key", None)
            anthropic_cfg["base_url"] = resolved["base_url"]
            anthropic_cfg["provider_id"] = resolved["provider_id"]
            anthropic_cfg["access_mode"] = resolved["access_mode"]
            anthropic_cfg["provider_family"] = resolved["provider_family"]
            _apply_openai_auth_fields(anthropic_cfg, resolved)
            anthropic_cfg.pop("active_saved_connection_id", None)
            anthropic_env = anthropic_cfg.setdefault("env", {})
            for env_key in MAIN_SESSION_MANAGED_ENV_KEYS:
                anthropic_env.pop(env_key, None)
            anthropic_env.update(resolved["env"])
            effective_api_key = str(
                anthropic_cfg.get("api_key", "")
                or resolve_current_main_session_api_key()
                or ""
            ).strip()
            with open(str(CONFIG_FILE), 'w', encoding='utf-8') as f:
                _json.dump(config_data, f, indent=2, ensure_ascii=False)
                f.write('\n')
            load_config(force_reload=True)
        except Exception as e:
            logger.warning("Failed to persist API key to config.json: %s", e)
        from lib.settings_handler import sync_main_session_to_claude_settings

        claude_settings_synced = sync_main_session_to_claude_settings(
            api_key=effective_api_key,
            base_url=resolved["base_url"],
            extra_env=resolved["env"],
            resolved_connection=resolved,
        )
        return web.json_response({
            "success": True,
            "session_model_sync": {"attempted": False, "applied": False, "reason": "settings_only"},
            "claude_settings_synced": bool(claude_settings_synced),
        })

    # -------------------- Connection Test & Profiles --------------------

    async def handle_test_connection(self, request):
        """POST /vizo/console/api/settings/test-connection — 测试 API 连接"""
        if not self._check_auth(request):
            return web.json_response({"error": "未授权"}, status=401)
        import aiohttp
        import time as _time
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "请求格式错误"}, status=400)
        from lib.openai_compat_bridge import (
            build_openai_request_plans,
            detect_nested_cli_upstream,
            normalize_openai_response_payload,
            should_try_next_openai_request_plan,
        )
        from lib.settings_handler import (
            is_openai_access_mode,
            resolve_current_main_session_api_key,
            resolve_main_session_connection,
            validate_main_session_connection_requirements,
            validate_main_session_base_url_for_access_mode,
        )
        routing = resolve_main_session_connection(data.get("base_url", ""), values=data)
        diagnostics_payload = self._build_runtime_diagnostics_payload(
            config=self._config,
            payload={
                "connection_id": str(data.get("connection_id") or ""),
                "base_url": data.get("base_url", ""),
                "provider_id": data.get("provider_id", ""),
                "env": data,
                "api_key": data.get("api_key", ""),
                "auth_mode": data.get("auth_mode", ""),
                "auth_status": data.get("auth_status", ""),
                "auth_last_verified_at": data.get("auth_last_verified_at", ""),
                "auth_account_label": data.get("auth_account_label", ""),
                "codex_home": data.get("codex_home", ""),
            },
            active_session_id=str(data.get("active_session_id", "") or ""),
            probe_scope=str(data.get("probe_scope", "all") or "all"),
        )
        base_url = routing["base_url"]
        base_url_error = validate_main_session_base_url_for_access_mode(base_url, routing["access_mode"])
        if base_url_error:
            return web.json_response({
                "ok": False,
                "status": "error",
                "error": base_url_error,
                "runtime_diagnostics": diagnostics_payload,
                "runtime_candidates": diagnostics_payload.get("runtime_candidates", []),
                "blocking_issues": diagnostics_payload.get("blocking_issues", []),
                "warnings": diagnostics_payload.get("warnings", []),
                "resolved_connection": diagnostics_payload.get("resolved_connection", {}),
            })
        selection_error = validate_main_session_connection_requirements(routing)
        if selection_error:
            return web.json_response({
                "ok": False,
                "status": "error",
                "error": selection_error,
                "runtime_diagnostics": diagnostics_payload,
                "runtime_candidates": diagnostics_payload.get("runtime_candidates", []),
                "blocking_issues": diagnostics_payload.get("blocking_issues", []),
                "warnings": diagnostics_payload.get("warnings", []),
                "resolved_connection": diagnostics_payload.get("resolved_connection", {}),
            })
        if (routing["provider_id"] == "gateway" or is_openai_access_mode(routing["access_mode"])) and not base_url:
            msg = "OpenAPI 必须填写 Base URL" if is_openai_access_mode(routing["access_mode"]) else "Anthropic 兼容接口必须填写 Base URL"
            return web.json_response({
                "ok": False,
                "status": "error",
                "error": msg,
                "runtime_diagnostics": diagnostics_payload,
                "runtime_candidates": diagnostics_payload.get("runtime_candidates", []),
                "blocking_issues": diagnostics_payload.get("blocking_issues", []),
                "warnings": diagnostics_payload.get("warnings", []),
                "resolved_connection": diagnostics_payload.get("resolved_connection", {}),
            })
        api_key = data.get("api_key", "")
        model_fields = routing["model_fields"]
        has_explicit_mapping = any(str(v or "").strip() for v in model_fields.values())
        if is_openai_access_mode(routing["access_mode"]):
            test_plan = []
            seen_models = set()
            for tier in ("sonnet", "opus", "haiku"):
                model = routing["routing_models"].get(tier, "").strip()
                if not model:
                    continue
                if model not in seen_models:
                    test_plan.append({"model": model, "tiers": [tier]})
                    seen_models.add(model)
                else:
                    for item in test_plan:
                        if item["model"] == model:
                            item["tiers"].append(tier)
                            break
        elif routing["provider_id"] in ("gateway", "custom") and not has_explicit_mapping:
            from lib.settings_handler import get_main_session_api_model

            test_plan = [{
                "model": get_main_session_api_model(
                    base_url,
                    values=data,
                    prefer="sonnet",
                ),
                "tiers": ["sonnet"],
            }]
        elif routing["provider_id"] in ("gateway", "custom", "openai") or has_explicit_mapping:
            test_plan = []
            seen_models = set()
            for tier in ("sonnet", "opus", "haiku"):
                model = routing["routing_models"].get(tier, "").strip()
                if not model:
                    continue
                if not has_explicit_mapping and routing["provider_id"] in ("gateway", "custom"):
                    from lib.settings_handler import get_main_session_api_model
                    model = get_main_session_api_model(base_url, values=data, prefer=tier)
                if model not in seen_models:
                    test_plan.append({"model": model, "tiers": [tier]})
                    seen_models.add(model)
                else:
                    for item in test_plan:
                        if item["model"] == model:
                            item["tiers"].append(tier)
                            break
        else:
            test_model = (model_fields.get("default_haiku_model") or model_fields.get("default_sonnet_model")
                          or model_fields.get("default_opus_model") or "claude-haiku-4-5-20251001")
            test_plan = [{"model": test_model, "tiers": ["haiku"]}]
        # 如果前端发 __USE_STORED__，从 settings.json 读取已存的 key
        if api_key == "__USE_STORED__":
            api_key = resolve_current_main_session_api_key()
        if not api_key:
            return web.json_response({
                "ok": False,
                "status": "error",
                "error": "API Key 为空",
                "runtime_diagnostics": diagnostics_payload,
                "runtime_candidates": diagnostics_payload.get("runtime_candidates", []),
                "blocking_issues": diagnostics_payload.get("blocking_issues", []),
                "warnings": diagnostics_payload.get("warnings", []),
                "resolved_connection": diagnostics_payload.get("resolved_connection", {}),
            })
        headers = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
        if is_openai_access_mode(routing["access_mode"]):
            headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}
        elif api_key.startswith("sk-ant-"):
            headers["x-api-key"] = api_key
        else:
            headers["Authorization"] = f"Bearer {api_key}"

        def _looks_like_model_error(body_text: str) -> bool:
            text = str(body_text or "").lower()
            if "model" not in text:
                return False
            patterns = (
                "not supported model",
                "unsupported model",
                "selected model",
                "model_not_found",
                "invalid model",
                "not exist",
                "does not exist",
                "no access",
                "have access to it",
                "unknown model",
            )
            return any(p in text for p in patterns)

        def _should_accept_codex_runtime_probe(resp_headers, body_text: str, nested_cli_error: str) -> bool:
            if not nested_cli_error or not is_openai_access_mode(routing["access_mode"]):
                return False
            provider_caps = routing.get("provider_capabilities") or {}
            provider_family = str(provider_caps.get("provider_family") or "").strip().lower()
            route_decision = ((diagnostics_payload.get("main_session") or {}).get("route_decision") or {})
            runtime_family = str(route_decision.get("runtime_family") or "").strip().lower()
            if provider_family != "codex" and runtime_family != "codex":
                return False
            normalized = str(body_text or "").lower()
            return (
                "response.created" in normalized
                or "you are a coding agent running in the codex cli" in normalized
                or "you are operating in the codex ide assistant mode" in normalized
                or "running in the codex cli" in normalized
            )

        def _extract_openai_tool_probe_error(resp_headers, body_text: str) -> str:
            nested_cli_error = detect_nested_cli_upstream(
                resp_headers,
                body_text,
                base_url=base_url,
            )
            if nested_cli_error:
                if not is_openai_access_mode(routing["access_mode"]):
                    return nested_cli_error
            try:
                normalized_payload = normalize_openai_response_payload(json.loads(body_text or "{}"))
            except Exception:
                return "上游返回了非 JSON 响应，无法验证 OpenAPI 工具调用协议"
            choice = ((normalized_payload.get("choices") or [{}])[0] or {})
            message = choice.get("message") or {}
            tool_calls = message.get("tool_calls") or []
            if tool_calls:
                return ""
            preview = str(message.get("content") or body_text or "").strip()
            if preview:
                preview = preview[:120]
                return f"上游未按 OpenAPI 工具协议返回 tool_calls，而是返回普通文本：{preview}"
            return "上游未按 OpenAPI 工具协议返回 tool_calls"

        start = _time.time()
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                validated_models = []
                soft_probe_warning = ""
                openai_target = None
                if is_openai_access_mode(routing["access_mode"]):
                    openai_target = {
                        "upstream_base_url": base_url,
                        "provider_id": routing.get("provider_family") or routing.get("provider_id"),
                        "capabilities": routing.get("provider_capabilities") or {},
                    }
                for item in test_plan:
                    if is_openai_access_mode(routing["access_mode"]):
                        request_plans = build_openai_request_plans(
                            {
                                "model": item["model"],
                                "max_tokens": 1,
                                "stream": False,
                                "messages": [{"role": "user", "content": "hi"}],
                            },
                            item["model"],
                            openai_target or {},
                        )
                        if not request_plans:
                            latency = int((_time.time() - start) * 1000)
                            return web.json_response({
                                "ok": False,
                                "status": "error",
                                "error": f"模型校验失败：{' / '.join(item['tiers'])} → {item['model']}；当前连接未找到可用的 OpenAI 兼容上游能力",
                                "latency_ms": latency,
                                "runtime_diagnostics": diagnostics_payload,
                                "runtime_candidates": diagnostics_payload.get("runtime_candidates", []),
                                "blocking_issues": diagnostics_payload.get("blocking_issues", []),
                                "warnings": diagnostics_payload.get("warnings", []),
                                "resolved_connection": diagnostics_payload.get("resolved_connection", {}),
                            })
                        probe_ok = False
                        probe_status = 0
                        probe_body = ""
                        for plan_index, plan in enumerate(request_plans):
                            active_payload = plan["payload"]
                            resp = await s.post(plan["url"], headers=headers, json=active_payload)
                            if resp.status == 400 and plan.get("fallback_payload") is not None:
                                await resp.release()
                                active_payload = plan["fallback_payload"]
                                resp = await s.post(plan["url"], headers=headers, json=active_payload)
                            probe_status = resp.status
                            probe_body = await resp.text()
                            if probe_status in (200, 201):
                                nested_cli_error = detect_nested_cli_upstream(
                                    resp.headers,
                                    probe_body,
                                    base_url=base_url,
                                )
                                if nested_cli_error:
                                    if _should_accept_codex_runtime_probe(resp.headers, probe_body, nested_cli_error):
                                        soft_probe_warning = (
                                            soft_probe_warning
                                            or "当前 OpenAPI 上游表现为 Codex runtime 端点，已按 Codex 直连 runtime 放行。"
                                        )
                                        probe_ok = True
                                        break
                                probe_ok = True
                                break
                            if should_try_next_openai_request_plan(
                                plan,
                                probe_status,
                                has_next_plan=plan_index + 1 < len(request_plans),
                            ):
                                continue
                            break
                        if not probe_ok:
                            latency = int((_time.time() - start) * 1000)
                            tier_label = " / ".join(item["tiers"])
                            err = probe_body.strip()[:180] or f"HTTP {probe_status}"
                            return web.json_response({
                                "ok": False,
                                "status": "error",
                                "error": f"模型校验失败：{tier_label} → {item['model']}；{err}",
                                "latency_ms": latency,
                                "runtime_diagnostics": diagnostics_payload,
                                "runtime_candidates": diagnostics_payload.get("runtime_candidates", []),
                                "blocking_issues": diagnostics_payload.get("blocking_issues", []),
                                "warnings": diagnostics_payload.get("warnings", []),
                                "resolved_connection": diagnostics_payload.get("resolved_connection", {}),
                            })
                        validated_models.append(item)
                        continue
                    request_url = f"{base_url}/v1/messages"
                    request_json = {
                        "model": item["model"],
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "hi"}],
                    }
                    resp = await s.post(
                        request_url,
                        headers=headers,
                        json=request_json,
                    )
                    body = await resp.text()
                    if resp.status not in (200, 201):
                        if (
                            resp.status == 400
                            and routing["provider_id"] == "custom"
                            and not any(str(v or "").strip() for v in model_fields.values())
                        ):
                            soft_probe_warning = (
                                "当前 Claude 直转平台未返回标准模型探测结果，已跳过严格模型校验；"
                                "保存后请直接新建会话验证实际对话。"
                            )
                            validated_models.append(item)
                            break
                        if resp.status == 400 and routing["provider_id"] not in ("gateway", "custom", "openai") and not _looks_like_model_error(body):
                            pass
                        else:
                            latency = int((_time.time() - start) * 1000)
                            tier_label = " / ".join(item["tiers"])
                            err = body.strip()[:180] or f"HTTP {resp.status}"
                            return web.json_response({
                                "ok": False,
                                "status": "error",
                                "error": f"模型校验失败：{tier_label} → {item['model']}；{err}",
                                "latency_ms": latency,
                                "runtime_diagnostics": diagnostics_payload,
                                "runtime_candidates": diagnostics_payload.get("runtime_candidates", []),
                                "blocking_issues": diagnostics_payload.get("blocking_issues", []),
                                "warnings": diagnostics_payload.get("warnings", []),
                                "resolved_connection": diagnostics_payload.get("resolved_connection", {}),
                            })
                    validated_models.append(item)

                if is_openai_access_mode(routing["access_mode"]) and validated_models and not soft_probe_warning:
                    tool_probe_body = {
                        "model": validated_models[0]["model"],
                        "max_tokens": 32,
                        "stream": False,
                        "messages": [{
                            "role": "user",
                            "content": "Call the function foo with empty JSON arguments.",
                        }],
                        "tools": [{
                            "name": "foo",
                            "description": "OpenAPI connectivity probe",
                            "input_schema": {
                                "type": "object",
                                "properties": {},
                                "additionalProperties": False,
                            },
                        }],
                        "tool_choice": {"type": "tool", "name": "foo"},
                    }
                    tool_request_plans = build_openai_request_plans(
                        tool_probe_body,
                        validated_models[0]["model"],
                        openai_target or {},
                    )
                    tool_probe_ok = False
                    tool_probe_status = 0
                    tool_probe_body_text = ""
                    for plan_index, plan in enumerate(tool_request_plans):
                        active_payload = plan["payload"]
                        resp = await s.post(plan["url"], headers=headers, json=active_payload)
                        if resp.status == 400 and plan.get("fallback_payload") is not None:
                            await resp.release()
                            active_payload = plan["fallback_payload"]
                            resp = await s.post(plan["url"], headers=headers, json=active_payload)
                        tool_probe_status = resp.status
                        tool_probe_body_text = await resp.text()
                        if tool_probe_status in (200, 201):
                            tool_probe_error = _extract_openai_tool_probe_error(
                                resp.headers,
                                tool_probe_body_text,
                            )
                            if not tool_probe_error:
                                tool_probe_ok = True
                                break
                            latency = int((_time.time() - start) * 1000)
                            return web.json_response({
                                "ok": False,
                                "status": "error",
                                "error": f"工具调用校验失败：{tool_probe_error}",
                                "latency_ms": latency,
                                "runtime_diagnostics": diagnostics_payload,
                                "runtime_candidates": diagnostics_payload.get("runtime_candidates", []),
                                "blocking_issues": diagnostics_payload.get("blocking_issues", []),
                                "warnings": diagnostics_payload.get("warnings", []),
                                "resolved_connection": diagnostics_payload.get("resolved_connection", {}),
                            })
                        if should_try_next_openai_request_plan(
                            plan,
                            tool_probe_status,
                            has_next_plan=plan_index + 1 < len(tool_request_plans),
                        ):
                            continue
                        break
                    if not tool_probe_ok:
                        latency = int((_time.time() - start) * 1000)
                        err = tool_probe_body_text.strip()[:180] or f"HTTP {tool_probe_status}"
                        return web.json_response({
                            "ok": False,
                            "status": "error",
                            "error": f"工具调用校验失败：{err}",
                            "latency_ms": latency,
                            "runtime_diagnostics": diagnostics_payload,
                            "runtime_candidates": diagnostics_payload.get("runtime_candidates", []),
                            "blocking_issues": diagnostics_payload.get("blocking_issues", []),
                            "warnings": diagnostics_payload.get("warnings", []),
                            "resolved_connection": diagnostics_payload.get("resolved_connection", {}),
                        })

                latency = int((_time.time() - start) * 1000)
                count_tokens_ok = False
                count_tokens_warning = ""
                if is_openai_access_mode(routing["access_mode"]):
                    count_tokens_ok = True
                else:
                    try:
                        ct_resp = await s.post(
                            f"{base_url}/v1/messages/count_tokens",
                            headers=headers,
                            json={"model": validated_models[0]["model"],
                                  "messages": [{"role": "user", "content": "hi"}]},
                        )
                        count_tokens_ok = ct_resp.status in (200, 201)
                        if not count_tokens_ok:
                            count_tokens_warning = "count_tokens 未验证通过，Claude Code 子代理可能不可用"
                    except Exception:
                        count_tokens_warning = "count_tokens 测试失败，Claude Code 子代理可能不可用"
                payload = {
                    "ok": True,
                    "status": "ok",
                    "latency_ms": latency,
                    "count_tokens_ok": count_tokens_ok,
                    "validated_models": validated_models,
                    "runtime_diagnostics": diagnostics_payload,
                    "runtime_candidates": diagnostics_payload.get("runtime_candidates", []),
                    "blocking_issues": diagnostics_payload.get("blocking_issues", []),
                    "warnings": diagnostics_payload.get("warnings", []),
                    "resolved_connection": diagnostics_payload.get("resolved_connection", {}),
                }
                warnings = [msg for msg in (soft_probe_warning, count_tokens_warning) if msg]
                if warnings:
                    payload["warning"] = "；".join(warnings)
                return web.json_response(payload)
        except Exception as e:
            latency = int((_time.time() - start) * 1000)
            return web.json_response({
                "ok": False,
                "status": "error",
                "error": str(e)[:100],
                "latency_ms": latency,
                "runtime_diagnostics": diagnostics_payload,
                "runtime_candidates": diagnostics_payload.get("runtime_candidates", []),
                "blocking_issues": diagnostics_payload.get("blocking_issues", []),
                "warnings": diagnostics_payload.get("warnings", []),
                "resolved_connection": diagnostics_payload.get("resolved_connection", {}),
            })

    async def handle_saved_connections(self, request):
        """GET/POST /vizo/console/api/settings/connections — 已保存连接管理"""
        if not self._check_auth(request):
            return web.json_response({"error": "未授权"}, status=401)
        from lib.settings_handler import (
            ConnectionProfileManager,
            is_openai_access_mode,
            resolve_main_session_connection,
            validate_main_session_connection_requirements,
            validate_main_session_base_url_for_access_mode,
        )
        mgr = ConnectionProfileManager()
        if request.method == "GET":
            from lib.config_loader import load_config

            cfg = load_config(force_reload=True)
            raw_connections = cfg.get("saved_connections", []) or []
            connections = mgr.get_all()
            by_id = {
                str(item.get("id", "") or ""): item
                for item in raw_connections
                if str(item.get("id", "") or "")
            }
            for index, item in enumerate(connections):
                raw = by_id.get(str(item.get("id", "") or ""))
                if raw is None and 0 <= index < len(raw_connections):
                    raw = raw_connections[index]
                if raw is None:
                    raw = {}
                diagnostics = self._build_runtime_diagnostics_payload(
                    config=cfg,
                    payload={
                        "connection_id": item.get("id", ""),
                        "base_url": raw.get("base_url", item.get("base_url", "")),
                        "provider_id": raw.get("provider_id", item.get("provider_id", "")),
                        "env": raw.get("env", {}),
                        "api_key": raw.get("api_key", ""),
                        "auth_mode": raw.get("auth_mode", item.get("auth_mode", "")),
                        "auth_status": raw.get("auth_status", item.get("auth_status", "")),
                        "auth_last_verified_at": raw.get("auth_last_verified_at", item.get("auth_last_verified_at", "")),
                        "auth_account_label": raw.get("auth_account_label", item.get("auth_account_label", "")),
                        "codex_home": raw.get("codex_home", item.get("codex_home", "")),
                    },
                    probe_scope="all",
                )
                item["runtime_diagnostics"] = diagnostics
                item["runtime_summary"] = diagnostics.get("main_session", {}).get("summary", "")
                item["runtime_candidates"] = diagnostics.get("runtime_candidates", [])
            return web.json_response({"connections": connections})
        # POST
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "请求格式错误"}, status=400)
        action = data.get("action", "")
        session_sync = {"attempted": False, "applied": False, "reason": "settings_only"}
        claude_settings_synced = True
        saved_connection = None
        if action == "save":
            resolved = resolve_main_session_connection(data.get("base_url", ""), values=data)
            base_url_error = validate_main_session_base_url_for_access_mode(
                resolved["base_url"], resolved["access_mode"]
            )
            if base_url_error:
                return web.json_response({"error": base_url_error}, status=400)
            selection_error = validate_main_session_connection_requirements(resolved)
            if selection_error:
                return web.json_response({"error": selection_error}, status=400)
            if (resolved["provider_id"] == "gateway" or is_openai_access_mode(resolved["access_mode"])) and not resolved["base_url"]:
                msg = "OpenAPI 必须填写 Base URL" if is_openai_access_mode(resolved["access_mode"]) else "Anthropic 兼容接口必须填写 Base URL"
                return web.json_response({"error": msg}, status=400)
            saved_connection = mgr.save(
                data.get("name", ""),
                data.get("base_url", ""),
                data.get("api_key", ""),
                extra_env=data,
            )
        elif action == "switch":
            idx = data.get("index", 0)
            from lib.config_loader import load_config

            raw_connections = load_config(force_reload=True).get("saved_connections", []) or []
            if 0 <= idx < len(raw_connections):
                raw_conn = raw_connections[idx]
                resolved = resolve_main_session_connection(
                    raw_conn.get("base_url", ""),
                    values=raw_conn,
                    current_env=raw_conn.get("env", {}),
                    stored_provider_id=raw_conn.get("provider_id"),
                )
                base_url_error = validate_main_session_base_url_for_access_mode(
                    resolved["base_url"], resolved["access_mode"]
                )
                if base_url_error:
                    return web.json_response({"error": base_url_error}, status=400)
                selection_error = validate_main_session_connection_requirements(resolved)
                if selection_error:
                    return web.json_response({"error": selection_error}, status=400)
                if (resolved["provider_id"] == "gateway" or is_openai_access_mode(resolved["access_mode"])) and not resolved["base_url"]:
                    msg = "OpenAPI 必须填写 Base URL" if is_openai_access_mode(resolved["access_mode"]) else "Anthropic 兼容接口必须填写 Base URL"
                    return web.json_response({"error": msg}, status=400)
            conn = mgr.switch(idx)
            claude_settings_synced = bool((conn or {}).get("claude_settings_synced"))
        elif action == "resync_session_model":
            idx = data.get("index", 0)
            from lib.config_loader import load_config

            raw_connections = load_config(force_reload=True).get("saved_connections", []) or []
            if idx < 0 or idx >= len(raw_connections):
                return web.json_response({"error": "连接不存在"}, status=404)
            conn = mgr.switch(idx)
            claude_settings_synced = bool((conn or {}).get("claude_settings_synced"))
        elif action == "delete":
            mgr.delete(data.get("index", 0))
            claude_settings_synced = True
        else:
            return web.json_response({"error": f"未知操作: {action}"}, status=400)
        return web.json_response({
            "success": True,
            "saved_connection": saved_connection if action == "save" else None,
            "session_model_sync": session_sync,
            "claude_settings_synced": bool(claude_settings_synced),
        })

    async def handle_saved_connection_auth_status(self, request):
        """GET /vizo/console/api/settings/connections/{connection_id}/auth/status"""
        if not self._check_auth(request):
            return web.json_response({"error": "未授权"}, status=401)
        connection_id = str(request.match_info.get("connection_id", "") or "")
        connection = self._resolve_saved_connection_auth_target(connection_id)
        if connection is None:
            return web.json_response({"error": "连接不存在"}, status=404)
        if str(connection.get("auth_mode") or "") != "account_login":
            return web.json_response({"error": "当前连接未启用 OpenAPI 账号登录模式"}, status=400)
        if not bool(connection.get("account_login_supported")):
            return web.json_response({"error": "当前连接不支持 OpenAPI 账号登录"}, status=400)
        return web.json_response(await self._serialize_connection_auth_payload(connection))

    async def handle_saved_connection_auth_login(self, request):
        """POST /vizo/console/api/settings/connections/{connection_id}/auth/login"""
        if not self._check_auth(request):
            return web.json_response({"error": "未授权"}, status=401)
        try:
            data = await request.json()
        except Exception:
            data = {}
        connection_id = str(request.match_info.get("connection_id", "") or "")
        connection = self._resolve_saved_connection_auth_target(connection_id)
        if connection is None:
            return web.json_response({"error": "连接不存在"}, status=404)
        if str(connection.get("auth_mode") or "") != "account_login":
            return web.json_response({"error": "当前连接未启用 OpenAPI 账号登录模式"}, status=400)
        if not bool(connection.get("account_login_supported")):
            return web.json_response({"error": "当前连接不支持 OpenAPI 账号登录"}, status=400)

        await self._terminate_connection_auth_job(connection_id)
        codex_home = self._resolve_connection_codex_home_path(connection)
        codex_home.mkdir(parents=True, exist_ok=True)
        env = self._build_connection_auth_env(codex_home)
        login_method = str(data.get("login_method") or "").strip().lower()
        login_args = ["codex", "login"]
        normalized_login_method = "browser"
        if login_method == "device_auth":
            login_args.append("--device-auth")
            normalized_login_method = "device_auth"
        process = await asyncio.create_subprocess_exec(
            *login_args,
            cwd=str(_PROJECT_ROOT),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._connection_auth_jobs[connection_id] = {
            "process": process,
            "output": "",
            "device_url": "",
            "device_code": "",
            "login_method": normalized_login_method,
            "completed": False,
            "returncode": None,
        }
        self._connection_auth_jobs[connection_id]["stdout_task"] = asyncio.create_task(
            self._read_connection_auth_stream(connection_id, process.stdout)
        )
        self._connection_auth_jobs[connection_id]["stderr_task"] = asyncio.create_task(
            self._read_connection_auth_stream(connection_id, process.stderr)
        )
        self._connection_auth_jobs[connection_id]["wait_task"] = asyncio.create_task(
            self._wait_connection_auth_process(connection_id, process)
        )
        await self._persist_connection_auth_state(connection_id, auth_status="unknown")
        await asyncio.sleep(0.2)
        return web.json_response(await self._serialize_connection_auth_payload(connection))

    async def handle_saved_connection_auth_logout(self, request):
        """POST /vizo/console/api/settings/connections/{connection_id}/auth/logout"""
        if not self._check_auth(request):
            return web.json_response({"error": "未授权"}, status=401)
        connection_id = str(request.match_info.get("connection_id", "") or "")
        connection = self._resolve_saved_connection_auth_target(connection_id)
        if connection is None:
            return web.json_response({"error": "连接不存在"}, status=404)
        if str(connection.get("auth_mode") or "") != "account_login":
            return web.json_response({"error": "当前连接未启用 OpenAPI 账号登录模式"}, status=400)

        await self._terminate_connection_auth_job(connection_id)
        codex_home = self._resolve_connection_codex_home_path(connection)
        codex_home.mkdir(parents=True, exist_ok=True)
        await self._run_connection_auth_command(codex_home, "logout")
        await self._persist_connection_auth_state(connection_id, auth_status="missing")
        updated = self._resolve_saved_connection_auth_target(connection_id)
        return web.json_response({
            "connection_id": connection_id,
            "auth_mode": str((updated or {}).get("auth_mode") or ""),
            "auth_status": "missing",
            "auth_last_verified_at": "",
            "auth_account_label": "",
            "codex_home": str((updated or {}).get("codex_home") or ""),
            "login_state": "idle",
            "status_message": "Not logged in",
        })

    # -------------------- External Models Settings --------------------

    async def handle_settings_ext_models_get(self, request):
        """GET /vizo/console/api/settings/external-models — 读取外部模型配置（脱敏）"""
        if not self._check_auth(request):
            return web.json_response({"error": "未授权"}, status=401)
        from lib.config_loader import load_config
        from lib.settings_handler import ExternalModelManager
        cfg = load_config()
        models = ExternalModelManager().get(cfg)
        return web.json_response({"models": models})

    async def handle_settings_ext_models_post(self, request):
        """POST /vizo/console/api/settings/external-models — 更新外部模型配置"""
        if not self._check_auth(request):
            return web.json_response({"error": "未授权"}, status=401)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "请求格式错误"}, status=400)
        model_id = data.get("model_id", "").strip()
        cli_model = data.get("cli_model", "").strip()
        base_url = data.get("base_url", "").strip()
        api_key = data.get("api_key", "").strip()
        display = data.get("display", "").strip()
        access_mode = data.get("access_mode", "").strip()
        provider_family = data.get("provider_family", "").strip()
        if not model_id:
            return web.json_response({"success": False, "error": "缺少 model_id"})
        from lib.settings_handler import (
            ExternalModelManager,
            OPENAPI_EXTERNAL_MODEL_UNSUPPORTED_MESSAGE,
            PRESET_IDS,
            is_openai_access_mode,
            validate_main_session_base_url_for_access_mode,
        )
        if api_key != "__DELETE__" and is_openai_access_mode(access_mode):
            return web.json_response({"success": False, "error": OPENAPI_EXTERNAL_MODEL_UNSUPPORTED_MESSAGE})
        if api_key != "__DELETE__" and base_url and model_id not in PRESET_IDS:
            base_url_error = validate_main_session_base_url_for_access_mode(base_url, access_mode)
            if base_url_error:
                return web.json_response({"success": False, "error": base_url_error})
        err = ExternalModelManager().update(
            model_id,
            cli_model,
            base_url,
            api_key,
            display,
            access_mode=access_mode,
            provider_family=provider_family,
        )
        if err:
            return web.json_response({"success": False, "error": err})
        return web.json_response({"success": True})

    async def handle_settings_mcp_services_get(self, request):
        """GET /vizo/console/api/settings/mcp-services — 读取 MCP 工具总览"""
        if not self._check_auth(request):
            return web.json_response({"error": "未授权"}, status=401)
        from lib.settings_handler import McpServiceManager
        try:
            return web.json_response(McpServiceManager().get_overview())
        except Exception as e:
            logger.exception("Failed to load MCP services")
            return web.json_response({"error": str(e)}, status=500)

    async def handle_settings_mcp_services_state(self, request):
        """POST /vizo/console/api/settings/mcp-services/state — 保存 MCP 工具全局开关"""
        if not self._check_auth(request):
            return web.json_response({"error": "未授权"}, status=401)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "请求格式错误"}, status=400)
        from lib.settings_handler import McpServiceManager
        err = McpServiceManager().update_service_states(data.get("service_states", {}))
        if err:
            return web.json_response({"success": False, "error": err}, status=400)
        return web.json_response({"success": True})

    async def handle_settings_mcp_services_repair(self, request):
        """POST /vizo/console/api/settings/mcp-services/repair — 修复内置 MCP 工具"""
        if not self._check_auth(request):
            return web.json_response({"error": "未授权"}, status=401)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "请求格式错误"}, status=400)
        from lib.settings_handler import McpServiceManager
        ok, message = McpServiceManager().repair_core_service(str(data.get("name", "")).strip())
        if not ok:
            return web.json_response({"success": False, "error": message}, status=400)
        return web.json_response({"success": True, "message": message})

    async def handle_settings_mcp_services_import(self, request):
        """POST /vizo/console/api/settings/mcp-services/import — 导入 MCP 配置 JSON"""
        if not self._check_auth(request):
            return web.json_response({"error": "未授权"}, status=401)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "请求格式错误"}, status=400)
        from lib.settings_handler import McpServiceManager
        ok, message, names = McpServiceManager().import_services(
            data.get("raw_json", ""),
            overwrite=bool(data.get("overwrite")),
        )
        if not ok:
            return web.json_response({"success": False, "error": message}, status=400)
        return web.json_response({"success": True, "message": message, "names": names})

    async def handle_settings_mcp_services_manual(self, request):
        """POST /vizo/console/api/settings/mcp-services/manual — 手动添加 MCP 工具"""
        if not self._check_auth(request):
            return web.json_response({"error": "未授权"}, status=401)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "请求格式错误"}, status=400)
        from lib.settings_handler import McpServiceManager
        ok, message = McpServiceManager().add_manual_service(
            name=data.get("name", ""),
            command=data.get("command", ""),
            args=data.get("args", []) or [],
            env=data.get("env", {}) or {},
            overwrite=bool(data.get("overwrite")),
        )
        if not ok:
            return web.json_response({"success": False, "error": message}, status=400)
        return web.json_response({"success": True, "message": message})

    async def handle_settings_mcp_perms_get(self, request):
        """GET /vizo/console/api/settings/mcp-permissions — 读取角色 MCP 权限"""
        if not self._check_auth(request):
            return web.json_response({"error": "未授权"}, status=401)
        from lib.settings_handler import McpPermissionManager
        try:
            return web.json_response(McpPermissionManager().get())
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def handle_settings_mcp_perms_post(self, request):
        """POST /vizo/console/api/settings/mcp-permissions — 保存角色 MCP 权限"""
        if not self._check_auth(request):
            return web.json_response({"error": "未授权"}, status=401)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "请求格式错误"}, status=400)
        role_mcps = data.get("role_mcps", {})
        from lib.settings_handler import McpPermissionManager
        err = McpPermissionManager().update(role_mcps)
        if err:
            return web.json_response({"success": False, "error": err})
        return web.json_response({"success": True})

    # -------------------- Chrome Bridge Status --------------------

    async def handle_chrome_status(self, request):
        """GET /vizo/console/api/chrome-status — 返回 Chrome Bridge 连接状态"""
        if not self._check_auth(request):
            return web.json_response({"error": "未授权"}, status=401)
        try:
            from lib.settings_handler import McpServiceManager
            return web.json_response(McpServiceManager().get_chrome_status())
        except Exception:
            return web.json_response({
                "service_enabled": True,
                "service_installed": False,
                "service_status": "missing",
                "service_status_text": "未安装",
                "chrome_connected": False,
                "browser_info": None,
                "pending_requests": 0,
                "cached_tools": 0,
                "uptime_seconds": 0,
                "stats": {},
            })

    # -------------------- Domain Settings --------------------

    async def handle_settings_domain_get(self, request):
        """GET /vizo/console/api/settings/domain — 读取域名配置"""
        if not self._check_auth(request):
            return web.json_response({"error": "未授权"}, status=401)
        from lib.config_loader import load_config
        from lib.preview_server import get_base_url
        cfg = load_config()
        custom_domain = cfg.get("custom_domain", "")
        return web.json_response({
            "custom_domain": custom_domain,
            "effective_url": get_base_url(),
        })

    async def handle_settings_domain_post(self, request):
        """POST /vizo/console/api/settings/domain — 保存域名配置"""
        if not self._check_auth(request):
            return web.json_response({"error": "未授权"}, status=401)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "请求格式错误"}, status=400)

        custom_domain = data.get("custom_domain", "").strip()
        # 简单的域名格式校验
        if custom_domain:
            import re
            if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9-]*(\.[a-zA-Z0-9][a-zA-Z0-9-]*)+$', custom_domain):
                return web.json_response({"error": "域名格式非法"}, status=400)

        import json as _json
        from lib.config_loader import load_config
        from lib.preview_server import get_base_url
        from lib.paths import CONFIG_FILE

        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config_data = _json.load(f)

        config_data["custom_domain"] = custom_domain

        with open(str(CONFIG_FILE), 'w', encoding='utf-8') as f:
            _json.dump(config_data, f, indent=2, ensure_ascii=False)
            f.write('\n')
        load_config(force_reload=True)
        return web.json_response({
            "success": True,
            "effective_url": get_base_url(),
        })

    # -------------------- Console SPA --------------------

    async def handle_console(self, request):
        """GET /vizo/console — serve SPA (auth required)."""
        if not self._check_auth(request):
            raise web.HTTPFound("/vizo/console/login")
        return web.Response(
            text=WEB_CONSOLE_HTML, content_type="text/html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

    async def handle_dialogue_console(self, request):
        """GET /vizo/console/dialogue — serve Dialogue Console (auth required)."""
        if not self._check_auth(request):
            raise web.HTTPFound("/vizo/console/login")
        return web.Response(
            text=render_dialogue_console_html(),
            content_type="text/html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @staticmethod
    def _is_local_fixture_request(request) -> bool:
        remote = str(getattr(request, "remote", "") or "")
        host = str(getattr(request, "host", "") or "").split(":", 1)[0].strip("[]").lower()
        if remote in {"127.0.0.1", "::1", "localhost"} or remote.startswith("::ffff:127.0.0.1"):
            return True
        return host in {"127.0.0.1", "::1", "localhost"}

    async def handle_dialogue_fixture(self, request):
        """GET /vizo/testing/fixtures/dialogue — local-only browser validation fixture."""
        if not self._is_local_fixture_request(request):
            return web.json_response({"error": "Forbidden"}, status=403)
        return web.Response(
            text=render_dialogue_console_html(),
            content_type="text/html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    # -------------------- WebSocket --------------------

    async def handle_ws(self, request):
        """GET /vizo/console/ws — WebSocket terminal bridge."""
        # Auth check
        if not self._check_auth(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.close(code=4001, message=b"Unauthorized")
            return ws

        session_id = request.query.get("session_id", "")
        session = self._pty.get_session(session_id)

        if not session:
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.close(code=4004, message=b"Session not found")
            return ws

        # Takeover: 新连接直接接管，踢掉旧连接
        ws = web.WebSocketResponse(heartbeat=None)  # We handle our own heartbeat
        await ws.prepare(request)

        # Detect client type (mobile won't resize PTY)
        client_type = request.query.get("client", "pc")
        is_mobile = client_type == "mobile"

        # Auto-resume suspended session on reconnect
        if session.status == "suspended":
            try:
                import os as _os
                import signal as _signal
                _os.kill(session.pid, _signal.SIGCONT)
                session.status = "running"
                session.last_input_at = __import__('time').time()
                logger.info("Auto-resumed suspended session %s on WS reconnect", session_id)
            except ProcessLookupError:
                session.status = "stopped"

        # Add to broadcast list (multi-client)
        session.ws_clients.append(ws)

        def _ensure_client_resize_state():
            if not hasattr(session, '_client_sizes'):
                session._client_sizes = {}
            if not hasattr(session, '_client_types'):
                session._client_types = {}

        async def _send_pty_size(target_ws):
            try:
                await target_ws.send_json({
                    "type": "pty_size",
                    "cols": session.cols,
                    "rows": session.rows,
                })
            except Exception:
                pass

        async def _broadcast_pty_size():
            pty_size_msg = {
                "type": "pty_size",
                "cols": session.cols,
                "rows": session.rows,
            }
            for client_ws in list(session.ws_clients):
                if not client_ws.closed:
                    try:
                        await client_ws.send_json(pty_size_msg)
                    except Exception:
                        pass

        def _get_resize_target():
            if not hasattr(session, '_client_sizes') or not session._client_sizes:
                return None
            _ensure_client_resize_state()
            pc_sizes = [
                size for client_id, size in session._client_sizes.items()
                if session._client_types.get(client_id) == "pc"
            ]
            # PC 尺寸始终优先；仅当没有 PC 客户端时，才允许移动端接管 PTY 尺寸。
            active_sizes = pc_sizes or [
                size for client_id, size in session._client_sizes.items()
                if session._client_types.get(client_id) == "mobile"
            ]
            if not active_sizes:
                return None
            max_cols = max(max(2, int(cols)) for cols, _ in active_sizes)
            max_rows = max(max(1, int(rows)) for _, rows in active_sizes)
            return max_cols, max_rows

        async def _sync_pty_size(target_ws=None):
            target = _get_resize_target()
            if target:
                cols, rows = target
                if session.cols != cols or session.rows != rows:
                    self._pty.resize(session, cols, rows)
                    await _broadcast_pty_size()
                    return
            if target_ws is not None:
                await _send_pty_size(target_ws)

        # Send replay buffer
        replay_data = session.buffer.read_all()
        if replay_data:
            try:
                await ws.send_json({
                    "type": "replay",
                    "data": replay_data.decode("utf-8", errors="replace"),
                })
            except Exception:
                pass

        # Send actual PTY size so client can match
        await _send_pty_size(ws)

        logger.info("WebSocket connected: session=%s, client=%s", session_id, client_type)

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        msg_type = data.get("type")

                        if msg_type == "input":
                            self._pty.write_input(session, data.get("data", ""))

                        elif msg_type == "resize":
                            try:
                                cols = max(2, int(data.get("cols", 120)))
                                rows = max(1, int(data.get("rows", 40)))
                            except (TypeError, ValueError):
                                continue
                            _ensure_client_resize_state()
                            session._client_sizes[id(ws)] = (cols, rows)
                            session._client_types[id(ws)] = "mobile" if is_mobile else "pc"
                            await _sync_pty_size(target_ws=ws)

                        elif msg_type == "ping":
                            await ws.send_json({"type": "pong"})

                    except json.JSONDecodeError:
                        pass

                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break

        except Exception as e:
            logger.error("WebSocket error: session=%s, %s", session_id, e)

        finally:
            # Remove from broadcast list
            try:
                session.ws_clients.remove(ws)
            except ValueError:
                pass
            # 清理该客户端的尺寸记录，并按剩余客户端重新计算 PTY 尺寸。
            if hasattr(session, '_client_sizes'):
                session._client_sizes.pop(id(ws), None)
            if hasattr(session, '_client_types'):
                session._client_types.pop(id(ws), None)
            await _sync_pty_size()
            logger.info("WebSocket disconnected: session=%s", session_id)

        return ws

    # -------------------- Session API --------------------

    async def handle_list_sessions(self, request):
        """GET /vizo/console/api/sessions — list Vizo-owned main sessions."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        return web.json_response({
            "sessions": self._main_sessions.list_sessions(),
        })

    async def handle_list_dialogue_events(self, request):
        """GET /vizo/console/api/legacy/dialogue/events — list PTY dialogue events."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        session_id = request.query.get("session_id", "")
        session = self._pty.get_session(session_id)
        if not session:
            return web.json_response({"events": []})
        return web.json_response({"events": self._pty.list_dialogue_events(session)})

    async def handle_add_dialogue_event(self, request):
        """POST /vizo/console/api/legacy/dialogue/events — append a PTY dialogue event."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        session_id = str(body.get("session_id", "")).strip()
        session = self._pty.get_session(session_id)
        if not session:
            return web.json_response({"error": "Session not found"}, status=404)

        kind = str(body.get("kind", "note")).strip() or "note"
        label = str(body.get("label", "Event")).strip()[:80] or "Event"
        raw_body = body.get("body", [])
        if isinstance(raw_body, str):
            lines = [raw_body]
        elif isinstance(raw_body, list):
            lines = [str(item) for item in raw_body]
        else:
            lines = [str(raw_body)]
        clean_lines = [line.strip()[:2000] for line in lines if str(line).strip()]
        if not clean_lines:
            return web.json_response({"error": "Event body required"}, status=400)

        meta = str(body.get("meta", "")).strip()[:240]
        event = self._pty.append_dialogue_event(session, kind, label, clean_lines, meta)
        for ws in list(session.ws_clients):
            if ws.closed:
                continue
            try:
                await ws.send_json({"type": "dialogue_event", "event": event})
            except Exception:
                pass
        return web.json_response({"event": event})

    async def handle_create_session(self, request):
        """POST /vizo/console/api/sessions — create a Vizo-owned main session."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        try:
            body = await request.json()
        except Exception:
            body = {}

        try:
            session = await self._main_sessions.create_session(
                name=str(body.get("name", "") or ""),
                cwd=str(body.get("cwd", "") or ""),
                connection_id=str(body.get("connection_id", "") or ""),
                display_model=str(body.get("display_model", "") or ""),
                runtime_override=str(body.get("runtime_override", "") or ""),
            )
        except KeyError:
            return web.json_response(
                {"error": "connection_not_found", "message": "Connection not found"},
                status=404,
            )
        except Exception as e:
            payload, status = self._runtime_error_payload(e, fallback_status=500)
            logger.error("Main session create failed: %s", e)
            return web.json_response(payload, status=status)

        return web.json_response({
            "status": "ok",
            "session": session.to_public_dict(),
        })

    async def handle_delete_session(self, request):
        """DELETE /vizo/console/api/sessions/{session_id}."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        session_id = request.match_info["session_id"]
        try:
            await self._main_sessions.delete_session(session_id)
        except Exception as e:
            payload, status = self._runtime_error_payload(e, fallback_status=400)
            return web.json_response(payload, status=status)
        return web.json_response({"status": "ok"})

    async def handle_close_session(self, request):
        """POST /vizo/console/api/sessions/{session_id}/close."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        session_id = request.match_info["session_id"]
        try:
            session = await self._main_sessions.close_session(session_id)
        except KeyError:
            return web.json_response({"error": "session_not_found", "message": "Session not found"}, status=404)
        except Exception as error:
            payload, status = self._runtime_error_payload(error, fallback_status=400)
            return web.json_response(payload, status=status)
        return web.json_response({
            "status": "ok",
            "message": "会话已关闭，已移入历史会话。",
            "session": session.to_public_dict(),
        })

    async def handle_legacy_list_sessions(self, request):
        """GET /vizo/console/api/legacy/sessions — list legacy PTY sessions."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        return web.json_response({
            "sessions": self._pty.list_sessions(),
            "max_sessions": self._pty._max_sessions,
        })

    async def handle_legacy_create_session(self, request):
        """POST /vizo/console/api/legacy/sessions — create a legacy PTY session."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        try:
            body = await request.json()
        except Exception:
            body = {}

        try:
            session = await self._pty.create_session(
                name=body.get("name", ""),
                cols=body.get("cols", 120),
                rows=body.get("rows", 40),
                cwd=body.get("cwd", ""),
            )
        except Exception as e:
            logger.error("PTY create_session failed: %s", e)
            return web.json_response(
                {"error": "create_failed", "message": str(e)},
                status=500,
            )

        if session is None:
            return web.json_response(
                {"error": "max_sessions", "message": "Maximum sessions reached"},
                status=409,
            )

        return web.json_response({
            "status": "ok",
            "session": {
                "id": session.id,
                "name": session.name,
                "status": session.status,
            },
        })

    async def handle_legacy_delete_session(self, request):
        """DELETE /vizo/console/api/legacy/sessions/{session_id}."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        session_id = request.match_info["session_id"]
        await self._pty.destroy_session(session_id)
        return web.json_response({"status": "ok"})

    async def handle_post_session_message(self, request):
        """POST /vizo/console/api/sessions/{session_id}/messages."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        session_id = request.match_info["session_id"]
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid_json", "message": "Invalid JSON"}, status=400)

        content = str(body.get("content", "") or "").strip()
        if not content:
            return web.json_response({"error": "content_required", "message": "content is required"}, status=400)

        try:
            payload = await self._main_sessions.accept_client_input(
                session_id,
                content=content,
                attachments=body.get("attachments") or [],
                expected_runtime=str(body.get("expected_runtime", "") or ""),
            )
        except KeyError:
            return web.json_response({"error": "session_not_found", "message": "Session not found"}, status=404)
        except Exception as e:
            payload, status = self._runtime_error_payload(e, fallback_status=400)
            return web.json_response(payload, status=status)

        return web.json_response(payload)

    async def handle_interrupt_session_turn(self, request):
        """POST /vizo/console/api/sessions/{session_id}/interrupt."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        session_id = request.match_info["session_id"]
        try:
            body = await request.json()
        except Exception:
            body = {}

        try:
            payload = await self._main_sessions.interrupt_turn(
                session_id,
                mode=str(body.get("mode", "soft") or "soft"),
                drop_pending=bool(body.get("drop_pending", True)),
                expected_runtime=str(body.get("expected_runtime", "") or ""),
            )
        except KeyError:
            return web.json_response({"error": "session_not_found", "message": "Session not found"}, status=404)
        except Exception as e:
            payload, status = self._runtime_error_payload(e, fallback_status=400)
            return web.json_response(payload, status=status)

        return web.json_response(payload)

    async def handle_delete_pending_session_input(self, request):
        """DELETE /vizo/console/api/sessions/{session_id}/pending/{queue_id}."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        session_id = request.match_info["session_id"]
        queue_id = request.match_info["queue_id"]
        try:
            body = await request.json()
        except Exception:
            body = {}

        try:
            payload = await self._main_sessions.delete_pending_input(
                session_id,
                queue_id,
                expected_runtime=str(body.get("expected_runtime", "") or ""),
            )
        except KeyError:
            return web.json_response({"error": "session_not_found", "message": "Session not found"}, status=404)
        except Exception as e:
            payload, status = self._runtime_error_payload(e, fallback_status=400)
            return web.json_response(payload, status=status)

        return web.json_response(payload)

    async def handle_send_pending_session_input_now(self, request):
        """POST /vizo/console/api/sessions/{session_id}/pending/{queue_id}/send-now."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        session_id = request.match_info["session_id"]
        queue_id = request.match_info["queue_id"]
        try:
            body = await request.json()
        except Exception:
            body = {}

        try:
            payload = await self._main_sessions.send_pending_input_now(
                session_id,
                queue_id,
                expected_runtime=str(body.get("expected_runtime", "") or ""),
                interrupt_mode=str(body.get("mode", "soft") or "soft"),
            )
        except KeyError:
            return web.json_response({"error": "session_not_found", "message": "Session not found"}, status=404)
        except Exception as e:
            payload, status = self._runtime_error_payload(e, fallback_status=400)
            return web.json_response(payload, status=status)

        return web.json_response(payload)

    async def handle_post_session_attachment(self, request):
        """POST /vizo/console/api/sessions/{session_id}/attachments."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        session_id = request.match_info["session_id"]
        max_bytes = self._main_sessions.attachment_upload_max_bytes()
        try:
            reader = await request.multipart()
        except Exception:
            return web.json_response({"error": "invalid_multipart", "message": "Invalid multipart body"}, status=400)

        file_field = None
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "file":
                file_field = part
                break
            await part.release()
        if file_field is None:
            return web.json_response({"error": "file_required", "message": "file is required"}, status=400)

        filename = str(file_field.filename or "attachment").strip() or "attachment"
        chunks: list[bytes] = []
        total_size = 0
        try:
            while True:
                chunk = await file_field.read_chunk(size=1024 * 1024)
                if not chunk:
                    break
                total_size += len(chunk)
                if total_size > max_bytes:
                    return web.json_response(
                        {
                            "error": "attachment_too_large",
                            "message": f"附件超过上传上限（{max_bytes // 1024 // 1024} MB）",
                        },
                        status=413,
                    )
                chunks.append(chunk)
            metadata = self._main_sessions.save_client_attachment(
                session_id,
                filename=filename,
                content=b"".join(chunks),
                content_type=str(file_field.headers.get("Content-Type", "") or ""),
            )
        except KeyError:
            return web.json_response({"error": "session_not_found", "message": "Session not found"}, status=404)
        except Exception as e:
            payload, status = self._runtime_error_payload(e, fallback_status=400)
            return web.json_response(payload, status=status)

        return web.json_response({"status": "ok", "attachment": metadata})

    async def handle_get_session_events(self, request):
        """GET /vizo/console/api/sessions/{session_id}/events."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        session_id = request.match_info["session_id"]
        raw_before_seq = str(request.query.get("before_seq", "") or "").strip()
        before_seq = int(raw_before_seq) if raw_before_seq else None
        try:
            payload = self._main_sessions.get_events(
                session_id,
                after_seq=int(request.query.get("after_seq", "0") or 0),
                before_seq=before_seq,
                limit=int(request.query.get("limit", "200") or 200),
            )
        except KeyError:
            return web.json_response({"error": "session_not_found", "message": "Session not found"}, status=404)
        return web.json_response(payload)

    async def handle_get_session_logs(self, request):
        """GET /vizo/console/api/sessions/{session_id}/logs."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        session_id = request.match_info["session_id"]
        try:
            payload = self._main_sessions.get_logs(
                session_id,
                offset=int(request.query.get("offset", "0") or 0),
                limit=int(request.query.get("limit", "65536") or 65536),
            )
        except KeyError:
            return web.json_response({"error": "session_not_found", "message": "Session not found"}, status=404)
        return web.json_response(payload)

    async def handle_get_session_images(self, request):
        """GET /vizo/console/api/sessions/{session_id}/images."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        session_id = request.match_info["session_id"]
        try:
            payload = self._main_sessions.get_images(session_id)
        except KeyError:
            return web.json_response({"error": "session_not_found", "message": "Session not found"}, status=404)
        return web.json_response(payload)

    async def handle_get_session_image(self, request):
        """GET /vizo/console/api/sessions/{session_id}/images/{image_id}."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        session_id = request.match_info["session_id"]
        image_id = request.match_info["image_id"]
        try:
            image_path, mime_type = self._main_sessions.resolve_image_artifact(session_id, image_id)
        except KeyError:
            return web.json_response({"error": "session_not_found", "message": "Session not found"}, status=404)
        except FileNotFoundError:
            return web.json_response({"error": "image_not_found", "message": "Image not found"}, status=404)

        response = web.FileResponse(image_path)
        response.content_type = mime_type
        response.headers["Cache-Control"] = "private, max-age=3600"
        return response

    async def handle_set_session_model(self, request):
        """POST /vizo/console/api/sessions/{session_id}/model."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        session_id = request.match_info["session_id"]
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid_json", "message": "Invalid JSON"}, status=400)

        display_model = str(body.get("display_model", "") or "").strip()
        if not display_model:
            return web.json_response({"error": "display_model_required", "message": "display_model is required"}, status=400)
        reasoning_effort = body.get("reasoning_effort")
        if reasoning_effort is not None:
            reasoning_effort = str(reasoning_effort or "").strip()

        try:
            result = await self._main_sessions.request_model_switch(
                session_id,
                display_model=display_model,
                reasoning_effort=reasoning_effort,
            )
        except KeyError:
            return web.json_response({"error": "session_not_found", "message": "Session not found"}, status=404)
        except Exception as e:
            payload, status = self._runtime_error_payload(e, fallback_status=400)
            return web.json_response(payload, status=status)

        session_payload = result.get("session") or {}
        return web.json_response({
            "status": result.get("status") or "ok",
            "session_id": session_payload.get("id") or session_id,
            "runtime_kind": session_payload.get("runtime_kind") or session_payload.get("runtime_family") or "",
            "display_model": session_payload.get("display_model") or display_model,
            "reasoning_effort": session_payload.get("reasoning_effort") or result.get("reasoning_effort", ""),
            "queued_display_model": result.get("queued_display_model", ""),
            "message": result.get("message", ""),
            "session": session_payload,
            "queued": bool(result.get("queued", False)),
            "applied_to_next_turn": result.get("status") == "model_switch_applied",
            "effective_current_turn": False,
            "effective_next_message": result.get("status") in {"model_switch_applied", "model_switch_queued"},
        })

    async def handle_post_session_interaction(self, request):
        """POST /vizo/console/api/sessions/{session_id}/interaction."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        session_id = request.match_info["session_id"]
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid_json", "message": "Invalid JSON"}, status=400)

        request_id = str(body.get("request_id", "") or "").strip()
        if not request_id:
            return web.json_response(
                {"error": "request_id_required", "message": "request_id is required"},
                status=400,
            )

        action = str(body.get("action", "") or "").strip()
        if not action:
            return web.json_response(
                {"error": "action_required", "message": "action is required"},
                status=400,
            )

        try:
            result = await self._main_sessions.submit_interaction_response(
                session_id,
                request_id=request_id,
                action=action,
                text=str(body.get("text", "") or ""),
                expected_runtime=str(body.get("expected_runtime", "") or ""),
            )
        except KeyError:
            return web.json_response({"error": "session_not_found", "message": "Session not found"}, status=404)
        except Exception as error:
            payload, status = self._runtime_error_payload(error, fallback_status=400)
            return web.json_response(payload, status=status)

        return web.json_response(result)

    async def handle_set_session_connection(self, request):
        """POST /vizo/console/api/sessions/{session_id}/connection."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        session_id = request.match_info["session_id"]
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid_json", "message": "Invalid JSON"}, status=400)

        connection_id = str(body.get("connection_id", "") or "").strip()
        if not connection_id:
            return web.json_response({"error": "connection_id_required", "message": "connection_id is required"}, status=400)

        try:
            session = await self._main_sessions.switch_connection(session_id, connection_id=connection_id)
        except KeyError as error:
            if str(error) == "'connection_not_found'":
                return web.json_response({"error": "connection_not_found", "message": "Connection not found"}, status=404)
            return web.json_response({"error": "session_not_found", "message": "Session not found"}, status=404)
        except Exception as error:
            payload, status = self._runtime_error_payload(error, fallback_status=400)
            return web.json_response(payload, status=status)

        return web.json_response({
            "status": "ok",
            "session_id": session.session_id,
            "connection_id": session.connection_id,
            "connection_name": session.connection_name,
            "runtime_kind": session.runtime_kind,
            "display_model": session.display_model,
            "session": session.to_public_dict(),
            "applied_immediately": True,
        })

    async def handle_session_events_ws(self, request):
        """GET /vizo/console/api/sessions/{session_id}/events/ws — realtime structured events."""
        if not self._check_auth(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.close(code=4001, message=b"Unauthorized")
            return ws

        session_id = request.match_info["session_id"]
        session = self._main_sessions.get_session(session_id)
        if not session:
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.close(code=4004, message=b"Session not found")
            return ws

        ws = web.WebSocketResponse(heartbeat=30.0)
        await ws.prepare(request)
        after_seq = int(request.query.get("after_seq", "0") or 0)
        last_session_updated = ""
        try:
            while True:
                backlog = self._main_sessions.get_events(
                    session_id,
                    after_seq=after_seq,
                    limit=1000,
                )
                for event in backlog.get("events", []):
                    await ws.send_json({"type": "event", "event": event})
                    after_seq = max(after_seq, int(event.get("seq") or 0))
                session_snapshot = backlog.get("session", {})
                session_updated = str(session_snapshot.get("updated_at") or "")
                if session_snapshot and session_updated != last_session_updated:
                    await ws.send_json({"type": "session_snapshot", "session": session_snapshot})
                    last_session_updated = session_updated
                await asyncio.sleep(1.0)
        except Exception as e:
            logger.debug("Session events websocket closed: %s", e)
        finally:
            if not ws.closed:
                await ws.close()
        return ws

    async def handle_list_projects(self, request):
        """GET /vizo/console/api/projects — list registered projects."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        projects = []
        try:
            for name, info in self._load_projects_config().items():
                path = info.get("path", "")
                if path and os.path.isdir(path):
                    proj = {"name": name, "path": path, "description": info.get("description", "")}
                    if info.get("type"):
                        proj["type"] = info["type"]
                    if info.get("default_module"):
                        proj["default_module"] = info["default_module"]
                    projects.append(proj)
        except Exception as e:
            logger.warning("Failed to load projects: %s", e)

        return web.json_response({"projects": projects})

    # -------------------- Project Management API (Phase 2) --------------------

    async def handle_create_project(self, request):
        """POST /vizo/console/api/projects - create a new project."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        name = (data.get("name") or "").strip()
        description = (data.get("description") or "").strip()
        project_type = (data.get("type") or "").strip()  # "hub" or empty (dev)
        default_module = (data.get("default_module") or "").strip()
        requested_path = (data.get("path") or "").strip()

        # Validate name
        import re
        if not name or not re.match(r'^[a-zA-Z0-9_-]{1,50}$', name):
            return web.json_response(
                {"error": "项目名称只允许字母、数字、下划线和连字符，长度 1-50"},
                status=400,
            )

        # Check if project already exists in config
        import json as _json
        from lib.config_loader import load_config
        from lib.paths import CONFIG_FILE

        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config_data = _json.load(f)

        projects = config_data.get("projects") or {}
        if name in projects:
            return web.json_response({"error": f"项目 '{name}' 已存在"}, status=409)

        # Create directory structure
        import subprocess
        projects_base = _PROJECT_ROOT / "projects"
        persisted_projects_base = self._persisted_projects_base()
        projects_base.mkdir(parents=True, exist_ok=True)
        persisted_projects_base.mkdir(parents=True, exist_ok=True)
        if requested_path:
            project_dir = Path(self._normalize_runtime_project_path(name, requested_path)).resolve()
            persisted_project_dir = Path(
                normalize_mainline_project_path(
                    normalize_runtime_path(requested_path),
                    project_name=name,
                )
            ).resolve()
        else:
            project_dir = (projects_base / name).resolve()
            persisted_project_dir = (persisted_projects_base / name).resolve()
        if project_dir.exists() and not project_dir.is_dir():
            return web.json_response({"error": f"项目路径不是目录: {project_dir}"}, status=400)
        if persisted_project_dir.exists() and not persisted_project_dir.is_dir():
            return web.json_response({"error": f"项目路径不是目录: {persisted_project_dir}"}, status=400)
        for existing_name, info in projects.items():
            existing_path = self._normalize_runtime_project_path(
                existing_name,
                (info or {}).get("path", "") if isinstance(info, dict) else "",
            )
            if existing_path and Path(existing_path).resolve() == project_dir:
                return web.json_response(
                    {"error": f"项目路径已被项目 '{existing_name}' 使用"},
                    status=409,
                )
        project_dir.mkdir(parents=True, exist_ok=True)
        write_data_path("tasks", project_root=project_dir).mkdir(parents=True, exist_ok=True)
        (project_dir / ".serena" / "memories").mkdir(parents=True, exist_ok=True)
        if persisted_project_dir.resolve() != project_dir.resolve():
            persisted_project_dir.mkdir(parents=True, exist_ok=True)
            write_data_path("tasks", project_root=persisted_project_dir).mkdir(parents=True, exist_ok=True)
            (persisted_project_dir / ".serena" / "memories").mkdir(parents=True, exist_ok=True)

        normalized_project_type = "hub" if project_type == "hub" else "dev"
        bootstrap_serena_project(
            project_dir,
            project_name=name,
            description=description,
            project_type=normalized_project_type,
        )
        if persisted_project_dir.resolve() != project_dir.resolve():
            bootstrap_serena_project(
                persisted_project_dir,
                project_name=name,
                description=description,
                project_type=normalized_project_type,
            )

        # git init (best effort)
        try:
            subprocess.run(
                ["git", "init"], cwd=str(project_dir),
                capture_output=True, timeout=10,
            )
        except Exception as e:
            logger.warning("git init failed for project %s: %s", name, e)

        # Update config.json atomically
        if "projects" not in config_data:
            config_data["projects"] = {}
        project_entry = {
            "path": str(persisted_project_dir),
            "description": description,
            "serena_project": name,
        }
        if project_type == "hub":
            project_entry["type"] = "hub"
            if default_module:
                project_entry["default_module"] = default_module
        config_data["projects"][name] = project_entry
        with open(str(CONFIG_FILE), 'w', encoding='utf-8') as f:
            _json.dump(config_data, f, indent=2, ensure_ascii=False)
            f.write('\n')
        load_config(force_reload=True)

        return web.json_response({
            "ok": True,
            "project": {"name": name, "path": str(project_dir)},
        })

    async def handle_delete_project(self, request):
        """DELETE /vizo/console/api/projects/{name} - delete a project."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        name = request.match_info["name"]

        import json as _json
        from lib.config_loader import load_config
        from lib.paths import CONFIG_FILE

        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config_data = _json.load(f)

        projects = config_data.get("projects") or {}
        if name not in projects:
            return web.json_response({"error": f"项目 '{name}' 不存在"}, status=404)

        project_path = Path(projects[name].get("path", ""))

        # Check for running/paused tasks
        for task_dir in iter_task_dirs(project_path):
            state_file = task_dir / "state.json"
            if state_file.is_file():
                try:
                    state = json.loads(state_file.read_text("utf-8"))
                    status = state.get("status", "")
                    if status in ("running", "in_progress", "paused"):
                        return web.json_response(
                            {"error": f"项目中有进行中的任务 ({task_dir.name})，请先终止"},
                            status=409,
                        )
                except (json.JSONDecodeError, OSError):
                    continue

        # Remove from config.json
        del config_data["projects"][name]
        with open(str(CONFIG_FILE), 'w', encoding='utf-8') as f:
            _json.dump(config_data, f, indent=2, ensure_ascii=False)
            f.write('\n')
        load_config(force_reload=True)

        # Delete directory
        import shutil
        if project_path.is_dir():
            try:
                shutil.rmtree(str(project_path))
            except OSError as e:
                logger.warning("Failed to remove project dir %s: %s", project_path, e)

        return web.json_response({"ok": True})

    async def handle_project_tasks(self, request):
        """GET /vizo/console/api/projects/{name}/tasks - list tasks in a project."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        name = request.match_info["name"]

        # Get project path from config
        try:
            project_info = self._load_projects_config().get(name)
            if not project_info:
                return web.json_response({"error": f"项目 '{name}' 不存在"}, status=404)
            project_path = Path(project_info["path"])
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

        results = []
        for task_dir in iter_task_dirs(project_path):
            state_file = task_dir / "state.json"
            if not state_file.is_file():
                continue
            try:
                state = json.loads(state_file.read_text("utf-8"))
            except (json.JSONDecodeError, OSError):
                continue

            # Normalize status
            if state.get("status") == "in_progress":
                state["status"] = "running"

            # Read cost — prefer progress.json (includes sub-task costs),
            # fallback to cost.json (direct agent costs only)
            cost_usd = 0
            progress_file = task_dir / "progress.json"
            if progress_file.is_file():
                try:
                    progress = json.loads(progress_file.read_text("utf-8"))
                    cost_usd = progress.get("cost_usd", 0)
                except (json.JSONDecodeError, OSError):
                    pass
            if not cost_usd:
                cost_file = task_dir / "cost.json"
                if cost_file.is_file():
                    try:
                        costs = json.loads(cost_file.read_text("utf-8"))
                        cost_usd = sum(c.get("cost_usd", 0) for c in costs if c.get("billable", True))
                    except (json.JSONDecodeError, OSError):
                        pass

            results.append(self._task_list_payload(
                task_dir,
                state,
                project_path=project_path,
                cost_usd=cost_usd,
            ))

        # Sort: running/paused first, then by created_at descending
        results.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        results.sort(key=lambda x: 0 if x.get("status") in ("running", "paused") else 1)

        return web.json_response({"tasks": results})

    async def handle_all_tasks(self, request):
        """GET /vizo/console/api/tasks — list tasks across all projects."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        results = []
        seen_tasks = set()
        for proj_name, tasks_dir_path in self._all_tasks_dirs():
            try:
                for task_dir in tasks_dir_path.iterdir():
                    if not task_dir.is_dir():
                        continue
                    state_file = task_dir / "state.json"
                    if not state_file.is_file():
                        continue
                    task_key = (proj_name, task_dir.name)
                    if task_key in seen_tasks:
                        continue
                    seen_tasks.add(task_key)
                    try:
                        state = json.loads(state_file.read_text("utf-8"))
                    except (json.JSONDecodeError, OSError):
                        continue

                    if state.get("status") == "in_progress":
                        state["status"] = "running"

                    cost_usd = 0
                    progress_file = task_dir / "progress.json"
                    if progress_file.is_file():
                        try:
                            progress = json.loads(progress_file.read_text("utf-8"))
                            cost_usd = progress.get("cost_usd", 0)
                        except (json.JSONDecodeError, OSError):
                            pass
                    if not cost_usd:
                        cost_file = task_dir / "cost.json"
                        if cost_file.is_file():
                            try:
                                costs = json.loads(cost_file.read_text("utf-8"))
                                cost_usd = sum(c.get("cost_usd", 0) for c in costs if c.get("billable", True))
                            except (json.JSONDecodeError, OSError):
                                pass

                    results.append(self._task_list_payload(
                        task_dir,
                        state,
                        project=proj_name or state.get("project", ""),
                        cost_usd=cost_usd,
                    ))
            except OSError:
                pass

        results.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        results.sort(key=lambda x: 0 if x.get("status") in ("running", "paused") else 1)

        return web.json_response({"tasks": results})

    async def handle_project_files(self, request):
        """GET /vizo/console/api/projects/{name}/files - browse files in a project."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        name = request.match_info["name"]
        rel_path = request.query.get("path", "")

        # Get project path from config
        try:
            project_info = self._load_projects_config().get(name)
            if not project_info:
                return web.json_response({"error": f"项目 '{name}' 不存在"}, status=404)
            project_path = Path(project_info["path"])
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

        # Build target path and security check
        target = (project_path / rel_path).resolve()
        project_real = project_path.resolve()
        if not str(target).startswith(str(project_real)):
            return web.json_response({"error": "路径越界"}, status=403)

        if not target.exists():
            return web.json_response({"error": "路径不存在"}, status=404)

        if target.is_dir():
            entries = []
            try:
                for entry in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name)):
                    stat = entry.stat()
                    entries.append({
                        "name": entry.name,
                        "type": "dir" if entry.is_dir() else "file",
                        "size": stat.st_size if entry.is_file() else 0,
                        "mtime": int(stat.st_mtime),
                    })
            except OSError as e:
                return web.json_response({"error": str(e)}, status=500)
            return web.json_response({"type": "dir", "entries": entries})
        else:
            # File
            stat = target.stat()
            file_size = stat.st_size
            file_name = target.name

            # Try to read as text if small enough
            if file_size <= 1024 * 1024:  # 1MB
                try:
                    content = target.read_text("utf-8")
                    return web.json_response({
                        "type": "file",
                        "name": file_name,
                        "size": file_size,
                        "content": content,
                    })
                except (UnicodeDecodeError, OSError):
                    pass

            # Binary or large file
            return web.json_response({
                "type": "file",
                "name": file_name,
                "size": file_size,
                "binary": True,
            })

    # -------------------- Vizo API (P1) --------------------

    def _find_task_dir(self, task_id, project=None):
        """定位 task_id 所在目录。指定 project 时直接定位，否则遍历所有项目。"""
        try:
            projects = self._load_projects_config()
            if project and project in projects:
                candidate = resolve_task_dir(task_id, project_root=projects[project]["path"])
                if candidate.is_dir():
                    return candidate
            for _, info in projects.items():
                candidate = resolve_task_dir(task_id, project_root=info["path"])
                if candidate.is_dir():
                    return candidate
        except Exception:
            pass
        return resolve_task_dir(task_id, project_root=_PROJECT_ROOT)  # 兜底

    def _all_tasks_dirs(self):
        """返回所有项目的 (project_name, tasks_dir_path) 列表。"""
        tasks_dirs = []
        seen = set()
        try:
            for proj_name, proj_info in self._load_projects_config().items():
                for td in iter_storage_dirs("tasks", project_root=proj_info.get("path", "")):
                    key = str(td)
                    if key in seen:
                        continue
                    seen.add(key)
                    tasks_dirs.append((proj_name, td))
        except Exception:
            pass
        for default_td in iter_storage_dirs("tasks", project_root=_PROJECT_ROOT):
            key = str(default_td)
            if key in seen:
                continue
            seen.add(key)
            tasks_dirs.append(("", default_td))
        return tasks_dirs

    @staticmethod
    def _task_display_title(state: dict | None, task_id: str = "") -> str:
        state = state if isinstance(state, dict) else {}
        return summarize_task_title(
            state.get("task_name")
            or state.get("task_title")
            or state.get("description")
            or state.get("original_request")
            or task_id,
            fallback=task_id or "未命名任务",
        )

    def _task_list_payload(self, task_dir: Path, state: dict, *,
                           project: str = "", project_path: Path | None = None,
                           cost_usd: float = 0) -> dict:
        title = self._task_display_title(state, task_dir.name)
        item = {
            "id": task_dir.name,
            "task_name": title,
            "task_title": title,
            "task_summary": state.get("task_summary") or title,
            "description": state.get("description", ""),
            "task_type": state.get("task_type", ""),
            "status": state.get("status", "unknown"),
            "created_at": state.get("created_at", ""),
            "updated_at": state.get("updated_at", state.get("completed_at", state.get("created_at", ""))),
            "cost_usd": round(cost_usd, 2),
        }
        if project:
            item["project"] = project
        if project_path is not None:
            try:
                item["path"] = str(task_dir.relative_to(project_path)).replace("\\", "/")
            except ValueError:
                item["path"] = str(task_dir)
        return item

    def _decorate_task_display_fields(self, task_data: dict, state: dict | None = None,
                                      task_id: str = "") -> dict:
        if not isinstance(task_data, dict):
            return task_data
        state = state if isinstance(state, dict) else {}
        title_source = (
            task_data.get("task_name")
            or task_data.get("task_title")
            or state.get("task_name")
            or state.get("task_title")
            or task_data.get("description")
            or state.get("description")
            or state.get("original_request")
            or task_id
            or task_data.get("task_id")
            or task_data.get("id")
        )
        title = summarize_task_title(
            title_source,
            fallback=task_id or task_data.get("task_id") or task_data.get("id") or "未命名任务",
        )
        task_data["task_name"] = title
        task_data["task_title"] = title
        task_data.setdefault("task_summary", state.get("task_summary") or title)
        return task_data

    @staticmethod
    def _is_recent_task_timestamp(value: str, *, hours: int = 24) -> bool:
        raw = str(value or "").strip()
        if not raw:
            return False
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return False
        now = datetime.now(parsed.tzinfo) if parsed.tzinfo else datetime.now()
        return 0 <= (now - parsed).total_seconds() <= hours * 3600

    @staticmethod
    def _normalize_runtime_project_path(project_name: str, path: str) -> str:
        value = str(path or "").strip()
        if not value:
            return ""
        expanded = normalize_mainline_project_path(
            normalize_runtime_path(value),
            project_name=project_name,
        )
        runtime_root = str(_PROJECT_ROOT.resolve())
        alias_roots = {_HOST_MAINLINE_ROOT, _CONTAINER_MAINLINE_ROOT, runtime_root}
        lower_name = str(project_name or "").strip().lower()
        if lower_name in {"vizo", "vizo-next", "vizo_next"}:
            for alias in alias_roots:
                if expanded == alias:
                    return runtime_root
        for alias in alias_roots:
            normalized_alias = alias.rstrip("/\\")
            prefixes = {normalized_alias + "/projects/", normalized_alias + os.sep + "projects" + os.sep}
            for prefix in prefixes:
                if expanded.startswith(prefix):
                    relative = expanded[len(prefix):]
                    return str(_PROJECT_ROOT / "projects" / relative)
        return expanded

    @staticmethod
    def _persisted_projects_base() -> Path:
        runtime_root = str(_PROJECT_ROOT.resolve())
        if runtime_root == _CONTAINER_MAINLINE_ROOT:
            return Path(_HOST_MAINLINE_ROOT) / "projects"
        return _PROJECT_ROOT / "projects"

    @staticmethod
    def _load_projects_config() -> dict:
        try:
            config = load_config(force_reload=True)
            projects = {}
            for name, info in (config.get("projects") or {}).items():
                if not isinstance(info, dict):
                    continue
                normalized = dict(info)
                normalized["path"] = WebConsoleHandler._normalize_runtime_project_path(
                    name,
                    normalized.get("path", ""),
                )
                projects[name] = normalized
            return projects
        except Exception:
            return {}

    def _resolve_project_context_for_task_dir(self, task_dir, projects=None) -> dict:
        projects = projects or self._load_projects_config()
        task_path = Path(task_dir).resolve()
        for proj_name, info in projects.items():
            project_root = Path(info.get("path", "")).resolve()
            tasks_root = write_data_path("tasks", project_root=project_root).resolve()
            task_path_str = str(task_path)
            tasks_root_str = str(tasks_root)
            if task_path_str == tasks_root_str or task_path_str.startswith(tasks_root_str + os.sep):
                return {
                    "project": proj_name,
                    "project_name": proj_name,
                    "project_path": str(project_root),
                }
        return {
            "project": "",
            "project_name": "",
            "project_path": "",
        }

    def _decorate_task_with_project_context(self, task_data: dict, task_dir, requested_project: str = "") -> dict:
        if not isinstance(task_data, dict):
            return task_data
        projects = self._load_projects_config()
        project_name = requested_project or str(task_data.get("project") or "")
        project_path = ""
        if project_name and project_name in projects:
            project_path = str(projects[project_name].get("path", "") or "")
        if not project_name or not project_path:
            resolved = self._resolve_project_context_for_task_dir(task_dir, projects=projects)
            project_name = project_name or resolved.get("project_name", "")
            project_path = project_path or resolved.get("project_path", "")
        if project_name:
            task_data["project"] = project_name
            task_data["project_name"] = project_name
        if project_path:
            task_data["project_path"] = project_path
        return task_data

    @staticmethod
    def _build_pending_confirm_payload(request: dict, request_id: str = "") -> dict:
        request_type = str(request.get("type") or "").strip()
        if request_type == "v6_confirm":
            request_type = "confirm_with_feedback"
        return {
            "request_id": request_id or request.get("id", ""),
            "title": (request.get("title") or request.get("message") or "")[:100],
            "summary": request.get("summary", ""),
            "preview_url": request.get("preview_link", "") or request.get("preview_url", ""),
            "context": request.get("context", {}) if isinstance(request.get("context"), dict) else {},
            "type": request_type or "confirm_with_feedback",
        }

    @staticmethod
    def _merge_pending_confirm_payload(primary: dict | None, fallback: dict | None) -> dict | None:
        if not isinstance(primary, dict) and not isinstance(fallback, dict):
            return None
        result = dict(fallback or {})
        for key, value in (primary or {}).items():
            if value not in (None, "", {}):
                result[key] = value
        fallback_ctx = fallback.get("context") if isinstance(fallback, dict) and isinstance(fallback.get("context"), dict) else {}
        primary_ctx = primary.get("context") if isinstance(primary, dict) and isinstance(primary.get("context"), dict) else {}
        merged_ctx = dict(fallback_ctx)
        for key, value in primary_ctx.items():
            if value not in (None, "", []):
                merged_ctx[key] = value
        if merged_ctx:
            result["context"] = merged_ctx
        fallback_summary = str((fallback or {}).get("summary") or "").strip()
        primary_summary = str((primary or {}).get("summary") or "").strip()
        if fallback_summary and primary_summary in {"", "请查看并确认", "请确认"}:
            result["summary"] = fallback_summary
        return result

    async def _get_console_redis_client(self):
        getter = getattr(self, "_get_redis", None)
        if getter is None:
            return None
        try:
            result = getter() if callable(getter) else getter
        except Exception:
            return None
        if inspect.isawaitable(result):
            try:
                result = await result
            except Exception:
                return None
        return result

    @staticmethod
    def _decode_json_payload(raw):
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

    async def _find_web_pending_confirm(self, task_id: str = "", request_id: str = ""):
        redis_client = await self._get_console_redis_client()
        if redis_client is None:
            return "", None

        async def _load_request(candidate_request_id: str):
            if not candidate_request_id:
                return "", None
            try:
                raw = await asyncio.wait_for(
                    redis_client.get(f"pending_request:{candidate_request_id}"),
                    timeout=0.25,
                )
            except Exception:
                return "", None
            data = self._decode_json_payload(raw)
            if not isinstance(data, dict):
                return "", None
            return candidate_request_id, data

        if request_id:
            found_request_id, request_data = await _load_request(request_id)
            if request_data:
                return found_request_id, request_data

        if not task_id:
            return "", None

        async def _scan_matching_request():
            async for raw_key in redis_client.scan_iter(match="pending_request:*"):
                key = raw_key.decode("utf-8") if isinstance(raw_key, bytes) else str(raw_key)
                candidate_request_id = key.split("pending_request:", 1)[-1]
                found_request_id, request_data = await _load_request(candidate_request_id)
                if not request_data:
                    continue
                context = request_data.get("context") if isinstance(request_data.get("context"), dict) else {}
                bound_task_id = str(context.get("task_id") or request_data.get("task_id") or "").strip()
                if bound_task_id == task_id:
                    return found_request_id, request_data
            return "", None

        try:
            return await asyncio.wait_for(_scan_matching_request(), timeout=0.5)
        except Exception:
            return "", None

    async def _confirm_request_has_response(self, request_id: str = "") -> bool:
        request_id = str(request_id or "").strip()
        if not request_id:
            return False
        redis_client = await self._get_console_redis_client()
        if redis_client is not None:
            try:
                existing = await asyncio.wait_for(redis_client.get(f"response:{request_id}"), timeout=0.25)
                if existing:
                    return True
            except Exception:
                pass
        try:
            from lib.confirm_bridge import CONFIRM_DIR
            return (CONFIRM_DIR / f"{request_id}.response.json").exists()
        except Exception:
            return False

    @staticmethod
    def _resolve_task_output_path(task_dir, filename: str):
        filename = str(filename or "").strip()
        if not task_dir or not filename or ".." in filename or "/" in filename or "\\" in filename:
            return None
        task_path = Path(task_dir)
        for candidate in (task_path / filename, task_path / "outputs" / filename):
            if candidate.is_file():
                return candidate
        return None

    @staticmethod
    def _build_task_output_preview_url(task_id: str, filename: str) -> str:
        task_id = str(task_id or "").strip()
        filename = str(filename or "").strip()
        if not task_id or not filename:
            return ""
        return f"/vizo/api/tasks/{task_id}/outputs/{filename}"

    @staticmethod
    def _latest_active_step(steps: list) -> dict | None:
        if not isinstance(steps, list):
            return None
        active_statuses = {"running", "waiting_confirm", "paused"}
        for step in reversed(steps):
            if not isinstance(step, dict):
                continue
            status = str(step.get("status") or "").strip().lower()
            if status in active_statuses:
                return step
        return None

    @staticmethod
    def _normalize_task_live_state(task_data: dict) -> dict:
        if not isinstance(task_data, dict):
            return task_data
        status = str(task_data.get("status") or "").strip().lower()
        if status not in {"running", "waiting_confirm", "paused"}:
            return task_data
        active_step = WebConsoleHandler._latest_active_step(task_data.get("steps"))
        if not active_step:
            return task_data
        active_name = str(active_step.get("name") or "").strip()
        active_role = str(active_step.get("role") or "").strip()
        if active_name:
            task_data["current_step"] = active_name
        if active_role:
            task_data["current_role"] = active_role
        return task_data

    def _attach_output_artifacts(self, task_data: dict, task_dir) -> dict:
        if not isinstance(task_data, dict):
            return task_data
        task_id = str(task_data.get("task_id") or "").strip()
        steps = task_data.get("steps")
        if not isinstance(steps, list):
            return task_data

        for step in steps:
            if not isinstance(step, dict):
                continue
            output_doc = str(step.get("output_doc") or "").strip()
            preview_url = str(step.get("preview_url") or "").strip()
            output_available = bool(preview_url)
            output_path = self._resolve_task_output_path(task_dir, output_doc) if output_doc else None
            if output_path and task_id:
                local_preview_url = self._build_task_output_preview_url(task_id, output_doc)
                if preview_url and preview_url != local_preview_url:
                    step["external_preview_url"] = preview_url
                step["preview_url"] = local_preview_url
                step["output_url"] = local_preview_url
                output_available = True
            elif output_doc and task_id:
                step["output_url"] = self._build_task_output_preview_url(task_id, output_doc)
            step["output_available"] = output_available

        return task_data

    async def _attach_pending_confirm(self, task_data: dict, task_id: str) -> dict:
        if not isinstance(task_data, dict):
            return task_data
        status = str(task_data.get("status") or "").strip().lower()
        if status != "waiting_confirm":
            task_data.pop("pending_confirm", None)
            return task_data
        existing_pending = task_data.get("pending_confirm") if isinstance(task_data.get("pending_confirm"), dict) else None
        existing_request_id = str((existing_pending or {}).get("request_id") or "").strip()
        existing_resolved = await self._confirm_request_has_response(existing_request_id)
        matched_resolved = existing_resolved
        pending_confirm = None

        web_request_id, web_request = ("", None)
        if not existing_resolved:
            web_request_id, web_request = await self._find_web_pending_confirm(task_id=task_id, request_id=existing_request_id)
        if isinstance(web_request, dict):
            matched_resolved = await self._confirm_request_has_response(web_request_id)
            if not matched_resolved:
                pending_confirm = self._build_pending_confirm_payload(web_request, request_id=web_request_id)

        if pending_confirm is None and task_id and not matched_resolved:
            try:
                from lib.confirm_bridge import get_pending
                for request in get_pending():
                    req_task_id = request.get("task_id", "")
                    ctx_task_id = (request.get("context") or {}).get("task_id", "")
                    if task_id in (req_task_id, ctx_task_id):
                        pending_confirm = self._build_pending_confirm_payload(request)
                        break
            except Exception:
                pending_confirm = None

        fallback_pending = (
            existing_pending
            if not matched_resolved and str(task_data.get("status") or "").strip() == "waiting_confirm"
            else None
        )
        merged_pending = self._merge_pending_confirm_payload(pending_confirm, fallback_pending)
        if merged_pending:
            task_data["pending_confirm"] = merged_pending
        elif "pending_confirm" in task_data:
            task_data.pop("pending_confirm", None)
        return task_data

    @staticmethod
    def _format_runtime_family_label(runtime_family: str) -> str:
        value = str(runtime_family or "").strip().lower()
        if value in {"codex", "openai"}:
            return "Codex CLI"
        if value in {"claude_code", "claude", "anthropic"}:
            return "Claude CLI"
        return runtime_family or "子代理"

    @staticmethod
    def _resolve_runtime_record(state: dict | None, step_id: str, role: str):
        if not isinstance(state, dict):
            return None
        metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
        runtime_meta = metadata.get("runtime") if isinstance(metadata.get("runtime"), dict) else {}
        if not isinstance(runtime_meta, dict):
            return None
        subagents = runtime_meta.get("subagents") if isinstance(runtime_meta.get("subagents"), dict) else {}
        for key in (step_id, role):
            if key and isinstance(subagents.get(key), dict):
                return subagents[key]
        for record in subagents.values():
            if not isinstance(record, dict):
                continue
            if step_id and str(record.get("step_name") or "").strip() == step_id:
                return record
            if role and str(record.get("role") or "").strip() == role:
                return record
        last_subagent = runtime_meta.get("last_subagent")
        if isinstance(last_subagent, dict):
            if step_id and str(last_subagent.get("step_name") or "").strip() == step_id:
                return last_subagent
            if role and str(last_subagent.get("role") or "").strip() == role:
                return last_subagent
        return None

    @classmethod
    def _runtime_event_to_action(cls, event: dict):
        if not isinstance(event, dict):
            return None
        event_type = str(event.get("event_type") or "").strip().lower()
        event_name = str(event.get("event_name") or "").strip()
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        timestamp = str(event.get("timestamp") or "").strip()

        if event_type == "message":
            text = str(payload.get("text") or "").strip()
            if not text:
                return None
            return {"type": event_name or "message", "text": text, "timestamp": timestamp}

        if event_type == "lifecycle" and event_name == "route_decision":
            decision = payload.get("decision") if isinstance(payload.get("decision"), dict) else {}
            runtime_label = cls._format_runtime_family_label(
                decision.get("runtime_family") or decision.get("runtime_kind") or ""
            )
            model = str(
                decision.get("selected_model")
                or decision.get("display_model")
                or decision.get("requested_model")
                or ""
            ).strip()
            reason = str(decision.get("reason") or decision.get("reason_code") or "").strip()
            text = f"已路由到 {runtime_label}"
            if model:
                text += f"（{model}）"
            if reason:
                text += f"；{reason}"
            return {"type": "route", "text": text, "timestamp": timestamp}

        if event_type == "lifecycle" and event_name == "init":
            model = str(payload.get("model") or "").strip()
            text = "子代理开始执行"
            if model:
                text += f"（{model}）"
            return {"type": "init", "text": text, "timestamp": timestamp}

        if event_type == "lifecycle" and event_name == "result":
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            total_cost = payload.get("total_cost_usd")
            parts = []
            if input_tokens or output_tokens:
                parts.append(f"输入 {input_tokens:,} / 输出 {output_tokens:,} tokens")
            if total_cost not in (None, ""):
                try:
                    parts.append(f"${float(total_cost):.2f}")
                except (TypeError, ValueError):
                    pass
            text = "执行结果已记录"
            if parts:
                text += "：" + " | ".join(parts)
            return {"type": "result", "text": text, "timestamp": timestamp}

        if event_type == "lifecycle" and event_name in {"completed", "failed"}:
            status = "完成" if event_name == "completed" else "失败"
            return {"type": event_name, "text": f"子代理执行{status}", "timestamp": timestamp}

        return None

    @classmethod
    def _load_runtime_event_actions(cls, event_log_path: Path) -> list:
        actions = []
        if not event_log_path or not event_log_path.is_file():
            return actions
        try:
            with event_log_path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        action = cls._runtime_event_to_action(json.loads(line))
                    except json.JSONDecodeError:
                        action = None
                    if action:
                        actions.append(action)
        except OSError:
            return []
        return actions

    @staticmethod
    def _load_stream_log_actions(log_path: Path) -> list:
        actions = []
        if not log_path or not log_path.is_file():
            return actions
        try:
            with log_path.open(encoding="utf-8") as handle:
                for line in handle:
                    text = line.strip()
                    if not text:
                        continue
                    if text and set(text) == {"━"}:
                        continue
                    if text.startswith("●"):
                        text = text[1:].strip()
                    actions.append({"type": "stream", "text": text})
        except OSError:
            return []
        return actions

    @staticmethod
    def _parse_step_epoch(value: str):
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _raw_role_log_epoch(log_path: Path):
        match = re.search(r"-(\d+)\.log$", log_path.name)
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    @classmethod
    def _step_time_windows(cls, task_dir: Path, step_id: str, role: str) -> list:
        progress_file = task_dir / "progress.json"
        if not progress_file.is_file():
            return []
        try:
            progress = json.loads(progress_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

        windows = []
        for step in progress.get("steps") or []:
            if not isinstance(step, dict):
                continue
            step_name = str(step.get("name") or "").strip()
            step_role = str(step.get("role") or "").strip()
            name_match = step_id and step_name == step_id
            role_as_step_match = step_id and role and step_id == role and step_role == role
            if not name_match and not role_as_step_match:
                continue

            if role and step_role and step_role != role:
                continue

            start = cls._parse_step_epoch(step.get("started_at") or "")
            end = cls._parse_step_epoch(step.get("completed_at") or "")
            try:
                duration = float(step.get("duration") or 0)
            except (TypeError, ValueError):
                duration = 0
            if start is None and end is not None and duration > 0:
                start = end - duration
            if end is None and start is not None and duration > 0:
                end = start + duration
            if start is not None or end is not None:
                windows.append((start, end))
        return windows

    @staticmethod
    def _unique_paths(paths: list[Path]) -> list[Path]:
        seen = set()
        unique = []
        for path in paths:
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            unique.append(path)
        return unique

    @classmethod
    def _select_raw_role_logs(cls, task_dir: Path, step_id: str, role: str) -> list[Path]:
        logs_dir = task_dir / "logs"
        if not logs_dir.is_dir():
            return []

        prefixes = []
        for value in (role, step_id):
            value = str(value or "").strip()
            if value and value not in prefixes:
                prefixes.append(value)

        candidates = []
        for prefix in prefixes:
            candidates.extend(logs_dir.glob(f"{prefix}-*.log"))
        candidates = [
            path for path in cls._unique_paths(candidates)
            if path.is_file() and cls._raw_role_log_epoch(path) is not None
        ]

        windows = cls._step_time_windows(task_dir, step_id, role)
        if not candidates and windows:
            candidates = [
                path for path in logs_dir.glob("*-*.log")
                if path.is_file() and cls._raw_role_log_epoch(path) is not None
            ]

        if not candidates:
            return []

        candidates.sort(key=lambda path: cls._raw_role_log_epoch(path) or 0)
        if not windows:
            return candidates[-1:]

        selected = []
        margin_seconds = 45
        for path in candidates:
            epoch = cls._raw_role_log_epoch(path)
            if epoch is None:
                continue
            for start, end in windows:
                low = (start - margin_seconds) if start is not None else float("-inf")
                high = (end + margin_seconds) if end is not None else float("inf")
                if low <= epoch <= high:
                    selected.append(path)
                    break
        if selected:
            return selected

        anchors = [value for window in windows for value in window if value is not None]
        if not anchors:
            return candidates[-1:]
        nearest = min(
            candidates,
            key=lambda path: min(abs((cls._raw_role_log_epoch(path) or 0) - anchor) for anchor in anchors),
        )
        distance = min(abs((cls._raw_role_log_epoch(nearest) or 0) - anchor) for anchor in anchors)
        return [nearest] if distance <= 3600 else []

    @staticmethod
    def _redact_log_text(text: str) -> str:
        if not text:
            return ""
        text = re.sub(
            r"(?i)([\"']?(?:authorization|cookie|api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret)[\"']?\s*[:=]\s*[\"']?)([^\"'\s,}]+)",
            r"\1[REDACTED]",
            str(text),
        )
        text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", text)
        return text

    @classmethod
    def _extract_log_text_content(cls, value) -> str:
        if isinstance(value, dict):
            content = value.get("content")
            if isinstance(content, list):
                chunks = []
                for item in content:
                    if isinstance(item, dict):
                        text = item.get("text") or item.get("content")
                        if isinstance(text, str) and text.strip():
                            chunks.append(text.strip())
                    elif isinstance(item, str) and item.strip():
                        chunks.append(item.strip())
                if chunks:
                    return "\n\n".join(chunks)

            for key in ("text", "message", "result", "output", "stdout", "stderr"):
                text = value.get(key)
                if isinstance(text, str) and text.strip():
                    return text.strip()

            structured = value.get("structured_content")
            if isinstance(structured, dict):
                text = cls._extract_log_text_content(structured)
                if text:
                    return text

        if isinstance(value, list):
            chunks = []
            for item in value:
                text = cls._extract_log_text_content(item)
                if text:
                    chunks.append(text)
            if chunks:
                return "\n\n".join(chunks)

        return ""

    @classmethod
    def _format_log_mapping(cls, value: dict, max_chars: int = 4000) -> str:
        parts = []
        for key, item in value.items():
            if key == "structured_content" and value.get("content"):
                continue
            if item in (None, ""):
                continue
            if isinstance(item, (str, int, float, bool)):
                item_text = str(item)
            else:
                item_text = cls._extract_log_text_content(item)
                if not item_text:
                    try:
                        item_text = json.dumps(item, ensure_ascii=False, default=str)
                    except (TypeError, ValueError):
                        item_text = str(item)
            item_text = cls._redact_log_text(item_text).strip()
            if item_text:
                parts.append(f"{key}: {item_text}")
        text = "\n".join(parts)
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "\n...（已截断）"
        return text

    @classmethod
    def _compact_log_text(cls, value, max_chars: int = 4000) -> str:
        if value in (None, ""):
            return ""
        text = cls._extract_log_text_content(value)
        if not text and isinstance(value, dict):
            text = cls._format_log_mapping(value, max_chars=max_chars)
        if not text and isinstance(value, (dict, list)):
            try:
                text = json.dumps(value, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                text = str(value)
        elif not text:
            text = str(value)
        text = cls._redact_log_text(text).strip()
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "\n...（已截断）"
        return text

    @classmethod
    def _summarize_mcp_result(cls, server: str, tool: str, arguments, result) -> str:
        server = str(server or "").strip()
        tool = str(tool or "").strip()
        args = arguments if isinstance(arguments, dict) else {}

        if server == "serena" and tool == "activate_project":
            project = str(args.get("project") or "").strip()
            text = cls._extract_log_text_content(result)
            path_match = re.search(r"\bat\s+([^\s]+)\s+is activated", text)
            suffix = f"：{project}" if project else ""
            if path_match:
                suffix += f"（{path_match.group(1)}）"
            return "已激活 Serena 项目" + suffix

        if server == "serena" and tool == "initial_instructions":
            return "已读取 Serena 工具使用说明"

        if server == "serena" and tool == "read_memory":
            memory_name = str(args.get("memory_name") or "").strip()
            text = cls._extract_log_text_content(result)
            size = f"，内容 {len(text):,} 字" if text else ""
            suffix = f"：{memory_name}" if memory_name else ""
            return f"已读取项目记忆{suffix}{size}"

        if server == "serena" and tool == "list_memories":
            text = cls._extract_log_text_content(result)
            count = len(re.findall(r'"[^"]+"', text)) if text else 0
            if count:
                return f"已获取项目记忆列表（约 {count} 项）"
            return "已获取项目记忆列表"

        return cls._compact_log_text(result, max_chars=2500)

    @classmethod
    def _raw_role_event_to_action(cls, event: dict):
        if not isinstance(event, dict):
            return None
        event_type = str(event.get("type") or "").strip()
        timestamp = str(event.get("timestamp") or "").strip()

        if event_type == "thread.started":
            thread_id = str(event.get("thread_id") or "").strip()
            text = "线程已启动"
            if thread_id:
                text += f"：{thread_id}"
            return {"type": "thread", "text": text, "timestamp": timestamp}

        if event_type == "turn.started":
            return {"type": "turn", "text": "开始执行", "timestamp": timestamp}

        if event_type == "turn.completed":
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            parts = []
            if input_tokens or output_tokens:
                parts.append(f"输入 {input_tokens:,} / 输出 {output_tokens:,} tokens")
            text = "执行完成"
            if parts:
                text += "：" + " | ".join(parts)
            return {"type": "result", "text": text, "timestamp": timestamp}

        if event_type != "item.completed":
            return None

        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        item_type = str(item.get("type") or "").strip()
        status = str(item.get("status") or "").strip()

        if item_type == "agent_message":
            text = cls._compact_log_text(item.get("text") or "")
            if text:
                return {"type": "output", "text": text, "timestamp": timestamp}
            return None

        if item_type == "command_execution":
            parts = []
            command = cls._compact_log_text(item.get("command") or "", max_chars=1200)
            if command:
                parts.append(f"命令: {command}")
            if status:
                parts.append(f"状态: {status}")
            if item.get("exit_code") is not None:
                parts.append(f"退出码: {item.get('exit_code')}")
            output = cls._compact_log_text(
                item.get("aggregated_output")
                or item.get("output")
                or item.get("stdout")
                or item.get("stderr")
                or "",
                max_chars=5000,
            )
            if output:
                parts.append(f"输出:\n{output}")
            if item.get("error"):
                parts.append("错误:\n" + cls._compact_log_text(item.get("error"), max_chars=3000))
            if parts:
                return {"type": "exec", "text": "\n".join(parts), "timestamp": timestamp}
            return None

        if item_type == "mcp_tool_call":
            server = str(item.get("server") or "").strip()
            tool = str(item.get("tool") or "").strip()
            name = ".".join(part for part in (server, tool) if part)
            parts = [f"工具: {name or 'mcp_tool_call'}"]
            if status:
                parts.append(f"状态: {status}")
            arguments = cls._compact_log_text(item.get("arguments"), max_chars=2500)
            if arguments:
                parts.append(f"参数: {arguments}")
            if item.get("error"):
                parts.append("错误:\n" + cls._compact_log_text(item.get("error"), max_chars=3000))
            result = cls._summarize_mcp_result(server, tool, item.get("arguments"), item.get("result"))
            if result:
                parts.append(f"结果:\n{result}")
            return {"type": "mcp", "text": "\n".join(parts), "timestamp": timestamp}

        if item_type == "file_change":
            changes = item.get("changes") if isinstance(item.get("changes"), list) else []
            lines = []
            for change in changes:
                if not isinstance(change, dict):
                    continue
                path = str(change.get("path") or "").strip()
                kind = str(change.get("kind") or "change").strip()
                if path:
                    lines.append(f"- {kind}: {path}")
            text = "文件变更"
            if lines:
                text += ":\n" + "\n".join(lines)
            if status:
                text += f"\n状态: {status}"
            return {"type": "write", "text": text, "timestamp": timestamp}

        text = cls._compact_log_text(item)
        return {"type": item_type or "log", "text": text, "timestamp": timestamp} if text else None

    @classmethod
    def _load_raw_role_log_actions(cls, log_path: Path) -> list:
        actions = []
        if not log_path or not log_path.is_file():
            return actions
        try:
            with log_path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        action = cls._raw_role_event_to_action(json.loads(line))
                    except json.JSONDecodeError:
                        action = None
                    if action:
                        actions.append(action)
        except OSError:
            return []
        return actions

    @classmethod
    def _load_raw_role_step_actions(cls, task_base: Path, task_id: str, step_id: str, role: str, sub_id: str) -> list:
        task_dirs = []
        if sub_id:
            task_dirs.extend([task_base / f"sub-{sub_id}", task_base.parent / f"{task_id}-sub{sub_id}"])
        else:
            task_dirs.append(task_base)

        actions = []
        for task_dir in cls._unique_paths(task_dirs):
            for log_path in cls._select_raw_role_logs(task_dir, step_id, role):
                actions.extend(cls._load_raw_role_log_actions(log_path))
        if len(actions) > 500:
            actions = actions[-500:]
        return actions

    def _load_fallback_step_actions(self, task_base: Path, task_id: str, step_id: str, role: str, sub_id: str) -> list:
        state_candidates = []
        stream_candidates = []
        if sub_id:
            for sub_dir in (task_base / f"sub-{sub_id}", task_base.parent / f"{task_id}-sub{sub_id}"):
                state_candidates.append(sub_dir / "state.json")
                stream_candidates.append(sub_dir / "logs" / f"{step_id}.stream.log")
                if role and role != step_id:
                    stream_candidates.append(sub_dir / "logs" / f"{role}.stream.log")
        else:
            state_candidates.append(task_base / "state.json")
            stream_candidates.append(task_base / "logs" / f"{step_id}.stream.log")
            if role and role != step_id:
                stream_candidates.append(task_base / "logs" / f"{role}.stream.log")

        for state_file in state_candidates:
            if not state_file.is_file():
                continue
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            record = self._resolve_runtime_record(state, step_id, role)
            event_log_path = Path(str(record.get("event_log_path") or "")) if isinstance(record, dict) else None
            actions = self._load_runtime_event_actions(event_log_path) if event_log_path else []
            if actions:
                return actions

        raw_actions = self._load_raw_role_step_actions(task_base, task_id, step_id, role, sub_id)
        if raw_actions:
            return raw_actions

        for stream_path in stream_candidates:
            actions = self._load_stream_log_actions(stream_path)
            if actions:
                return actions
        return []

    @staticmethod
    def _attach_runtime_facts(task_data: dict, state: dict | None) -> dict:
        if not isinstance(task_data, dict) or not isinstance(state, dict):
            return task_data
        steps = task_data.get("steps")
        if not isinstance(steps, list) or not steps:
            return task_data

        metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
        runtime_meta = metadata.get("runtime") if isinstance(metadata, dict) else {}
        if not isinstance(runtime_meta, dict):
            return task_data

        subagents = runtime_meta.get("subagents") if isinstance(runtime_meta.get("subagents"), dict) else {}
        last_subagent = runtime_meta.get("last_subagent") if isinstance(runtime_meta.get("last_subagent"), dict) else {}
        current_step = str(task_data.get("current_step") or state.get("current_step") or "").strip()
        current_role = str(task_data.get("current_role") or "").strip()

        def _apply_record(step: dict, record: dict) -> None:
            runtime_family = str(record.get("runtime_family") or "").strip()
            selected_model = str(record.get("selected_model") or record.get("requested_model") or "").strip()
            native_session_id = str(record.get("native_session_id") or "").strip()
            if runtime_family:
                step["runtime_family"] = runtime_family
                step["runtime_kind"] = runtime_family
                step["provider_family"] = runtime_family
            if selected_model:
                step["selected_model"] = selected_model
                step["model"] = selected_model
            if native_session_id:
                step["native_session_id"] = native_session_id

        for step in steps:
            if not isinstance(step, dict):
                continue
            name = str(step.get("name") or "").strip()
            role = str(step.get("role") or "").strip()
            record = None
            for key in (name, role):
                if key and isinstance(subagents.get(key), dict):
                    record = subagents[key]
                    break
            if record is None and last_subagent:
                last_step_name = str(last_subagent.get("step_name") or "").strip()
                last_role = str(last_subagent.get("role") or "").strip()
                status = str(step.get("status") or "").strip().lower()
                if name and name == last_step_name:
                    record = last_subagent
                elif role and role == last_role:
                    record = last_subagent
                elif status in {"running", "waiting_confirm", "paused"} and (
                    (current_step and name == current_step) or
                    (current_role and role == current_role)
                ):
                    record = last_subagent
            if record:
                _apply_record(step, record)

        return task_data

    @staticmethod
    def _build_hub_task_data(state: dict, task_dir) -> dict:
        """从 hub 任务的 state.json + cost.json 构建 progress.json 等效数据。"""
        task_id = state.get("id", "")
        description = state.get("description", "")
        title = summarize_task_title(
            state.get("task_name") or state.get("task_title") or description or state.get("original_request") or task_id,
            fallback=task_id or "未命名任务",
        )
        status = state.get("status", "unknown")
        if status == "in_progress":
            status = "running"

        # 从 cost.json 构建 steps
        steps = []
        cost_file = Path(task_dir) / "cost.json"
        total_cost = 0
        if cost_file.is_file():
            try:
                costs = json.loads(cost_file.read_text("utf-8"))
                for i, c in enumerate(costs):
                    if not c.get("billable", True):
                        continue
                    step_cost = c.get("cost_usd", 0)
                    total_cost += step_cost
                    # 用 completed_steps 中的名称（如果有），否则用 output_file 推导
                    completed = state.get("completed_steps", [])
                    step_name = completed[i] if i < len(completed) else c.get("output_file", "").replace(".md", "")
                    output_file = c.get("output_file", "")
                    # 检查是否有预览链接（先从 cost.json 读取，再生成本地服务链接）
                    preview_url = c.get("preview_url", "")
                    if not preview_url and output_file:
                        out_path = Path(task_dir) / "outputs" / output_file
                        if out_path.is_file():
                            preview_url = f"/vizo/api/tasks/{task_id}/outputs/{output_file}"
                    steps.append({
                        "name": step_name,
                        "role": c.get("role", ""),
                        "status": "completed",
                        "started_at": c.get("timestamp", ""),
                        "completed_at": c.get("timestamp", ""),
                        "cost_usd": round(step_cost, 4),
                        "duration": c.get("duration", 0),
                        "output_doc": output_file,
                        "preview_url": preview_url,
                        "model": c.get("model", ""),
                        "error": "",
                    })
            except (json.JSONDecodeError, OSError):
                pass

        # 如果正在运行但 cost.json 还没有记录，检查 completed_steps
        if not steps and state.get("completed_steps"):
            for step_name in state["completed_steps"]:
                steps.append({
                    "name": step_name, "role": "", "status": "completed",
                    "started_at": "", "completed_at": "", "cost_usd": 0,
                    "duration": 0, "output_doc": "", "preview_url": "",
                    "model": "", "error": "",
                })

        # 从 manifest 补充 pending 步骤
        module_id = state.get("module_id", "")
        workflow_id = state.get("workflow_id", "")
        if module_id and workflow_id:
            try:
                from lib.agent_creator import _flatten_steps
                manifest = None
                for search_dir in [
                    _PROJECT_ROOT / "agents" / "_builtin" / module_id,
                    *(_PROJECT_ROOT / "agents" / "_user").glob(f"*/{module_id}"),
                ]:
                    mf = search_dir / "manifest.json"
                    if mf.is_file():
                        manifest = json.loads(mf.read_text("utf-8"))
                        break

                if manifest:
                    wf = manifest.get("workflows", {}).get(workflow_id, {})
                    all_steps = _flatten_steps(wf)
                    completed_names = {s["name"] for s in steps}

                    # 确定当前运行中步骤
                    current_step_name = None
                    if status in ("running", "in_progress"):
                        for ms in all_steps:
                            if ms["step"] not in completed_names:
                                current_step_name = ms["step"]
                                break

                    # 补充已完成步骤的 description
                    for s in steps:
                        for ms in all_steps:
                            if ms["step"] == s["name"] and not s.get("description"):
                                s["description"] = ms.get("description", "")
                                break

                    # 添加 pending/running 步骤
                    for ms in all_steps:
                        if ms["step"] not in completed_names:
                            output_file = ms.get("output", "")
                            step_status = "running" if ms["step"] == current_step_name else "pending"
                            steps.append({
                                "name": ms["step"],
                                "role": ms.get("role", ""),
                                "status": step_status,
                                "started_at": "", "completed_at": "",
                                "cost_usd": 0, "duration": 0,
                                "output_doc": output_file,
                                "preview_url": "",
                                "model": "", "error": "",
                                "description": ms.get("description", ""),
                            })
            except Exception:
                pass  # manifest 不可用时降级为只展示已完成步骤

        return {
            "task_id": task_id,
            "task_name": title,
            "task_title": title,
            "task_summary": state.get("task_summary") or title,
            "description": description,
            "status": status,
            "current_role": "",
            "current_step": "",
            "completed_steps": state.get("completed_steps", []),
            "total_steps": len(steps),
            "cost_usd": round(total_cost, 2),
            "started_at": state.get("created_at", ""),
            "updated_at": state.get("completed_at", state.get("created_at", "")),
            "steps": steps,
            "task_type": state.get("task_type", ""),
            "scale": "normal",
            "module_id": state.get("module_id", ""),
            "workflow_id": state.get("workflow_id", ""),
            "has_code_changes": False,
        }

    async def handle_opus_current(self, request):
        """GET /vizo/console/api/opus/current — get current or specified Vizo task."""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        # If task_id specified, load that specific task (any status)
        requested_task_id = request.query.get("task_id", "")
        requested_project = request.query.get("project", "")
        if requested_task_id:
            task_dir = self._find_task_dir(requested_task_id, requested_project or None)
            progress_file = task_dir / "progress.json"
            if progress_file.is_file():
                try:
                    task_data = json.loads(progress_file.read_text("utf-8"))
                    state = None
                    # 交叉验证 state.json — 以 state.json 为权威状态源
                    # 但 waiting_confirm 是 progress.json 独有的细粒度状态，state.json 只知道 running
                    state_file = task_dir / "state.json"
                    if state_file.is_file():
                        try:
                            state = json.loads(state_file.read_text("utf-8"))
                            state_status = state.get("status", "")
                            progress_status = task_data.get("status", "")
                            # 终态以 state.json 为准（rolled_back, completed, failed 等）
                            # running 时保留 progress.json 的细粒度（如 waiting_confirm）
                            if state_status and state_status != progress_status:
                                if state_status != "running" or progress_status not in ("running", "waiting_confirm"):
                                    task_data["status"] = state_status
                                    try:
                                        progress_file.write_text(
                                            json.dumps(task_data, ensure_ascii=False, indent=2), "utf-8")
                                    except OSError:
                                        pass
                        except (json.JSONDecodeError, OSError):
                            pass
                    # 补充 has_code_changes 字段：基于 git diff 检测实际代码变更
                    if "has_code_changes" not in task_data:
                        if state is not None:
                            task_data["has_code_changes"] = check_task_code_changes(state)
                        else:
                            sf = task_dir / "state.json"
                            if sf.is_file():
                                try:
                                    st = json.loads(sf.read_text("utf-8"))
                                    task_data["has_code_changes"] = check_task_code_changes(st)
                                    state = st
                                except (json.JSONDecodeError, OSError):
                                    task_data["has_code_changes"] = False
                    # 补充 sub_tasks（state.json 独有，progress.json 没有）
                    if "sub_tasks" not in task_data:
                        if state is None:
                            sf = task_dir / "state.json"
                            if sf.is_file():
                                try:
                                    state = json.loads(sf.read_text("utf-8"))
                                except (json.JSONDecodeError, OSError):
                                    state = None
                        if isinstance(state, dict) and state.get("sub_tasks"):
                            task_data["sub_tasks"] = state["sub_tasks"]
                    task_data = self._decorate_task_display_fields(task_data, state, requested_task_id)
                    task_data = self._attach_runtime_facts(task_data, state)
                    task_data = self._attach_output_artifacts(task_data, task_dir)
                    task_data = self._normalize_task_live_state(task_data)
                    task_data = await self._attach_pending_confirm(task_data, requested_task_id)
                    task_data = self._decorate_task_with_project_context(task_data, task_dir, requested_project)
                    return web.json_response({"has_task": True, "task": task_data})
                except (json.JSONDecodeError, OSError):
                    pass
            # Fallback: no progress.json but state.json exists (hub tasks)
            state_file = task_dir / "state.json"
            if state_file.is_file():
                try:
                    state = json.loads(state_file.read_text("utf-8"))
                    task_data = self._decorate_task_with_project_context(
                        self._build_hub_task_data(state, task_dir),
                        task_dir,
                        requested_project,
                    )
                    task_data = self._decorate_task_display_fields(task_data, state, requested_task_id)
                    task_data = self._attach_runtime_facts(task_data, state)
                    task_data = self._attach_output_artifacts(task_data, task_dir)
                    task_data = self._normalize_task_live_state(task_data)
                    task_data = await self._attach_pending_confirm(task_data, requested_task_id)
                    return web.json_response({"has_task": True, "task": task_data})
                except (json.JSONDecodeError, OSError):
                    pass
            return web.json_response({"has_task": False})

        # No task_id — find the latest running task across all projects
        latest_task = None
        latest_time = ""
        latest_attention_task = None
        latest_attention_time = ""
        attention_statuses = {
            "failed",
            "error",
            "runtime_error",
            "interrupted",
        }

        for proj_name, tasks_dir_path in self._all_tasks_dirs():
            if requested_project and proj_name != requested_project:
                continue
            try:
                for task_dir_entry in tasks_dir_path.iterdir():
                    if not task_dir_entry.is_dir():
                        continue
                    progress_file = task_dir_entry / "progress.json"
                    if progress_file.is_file():
                        try:
                            progress = json.loads(progress_file.read_text("utf-8"))
                            task_status = progress.get("status")
                            if task_status == "running":
                                # 交叉验证 state.json — 防止僵尸 progress（编排器崩溃后未更新）
                                state_file = task_dir_entry / "state.json"
                                if state_file.is_file():
                                    try:
                                        state = json.loads(state_file.read_text("utf-8"))
                                        real_status = state.get("status", "")
                                        if real_status not in ("running", "in_progress"):
                                            progress["status"] = real_status or "completed"
                                            try:
                                                progress_file.write_text(
                                                    json.dumps(progress, ensure_ascii=False, indent=2), "utf-8")
                                            except OSError:
                                                pass
                                            continue
                                    except (json.JSONDecodeError, OSError):
                                        pass
                                # 过期检测：progress 超 10 分钟未更新 且 state.json 也非 running，才标记 stale
                                # 仅凭 updated_at 超时不够——agent 长时间执行时 orchestrator 不会频繁更新 progress
                                updated = progress.get("updated_at", progress.get("started_at", ""))
                                if updated:
                                    try:
                                        from datetime import datetime
                                        last_update = datetime.fromisoformat(updated)
                                        age_seconds = (datetime.now() - last_update).total_seconds()
                                        if age_seconds > 600:
                                            # 再次检查 state.json：如果 orchestrator 仍标记为 running，不标 stale
                                            state_still_running = False
                                            try:
                                                st = json.loads(state_file.read_text("utf-8")) if state_file.is_file() else {}
                                                state_still_running = st.get("status") in ("running", "in_progress")
                                            except (json.JSONDecodeError, OSError):
                                                pass
                                            if not state_still_running:
                                                progress["status"] = "stale"
                                                try:
                                                    progress_file.write_text(
                                                        json.dumps(progress, ensure_ascii=False, indent=2), "utf-8")
                                                except OSError:
                                                    pass
                                                continue
                                    except (ValueError, TypeError):
                                        pass
                                if updated > latest_time:
                                    latest_time = updated
                                    latest_task = self._decorate_task_with_project_context(
                                        progress,
                                        task_dir_entry,
                                        proj_name,
                                    )
                            elif task_status == "waiting_confirm":
                                # waiting_confirm 任务：用户必须响应，跳过 stale 检测
                                updated = progress.get("updated_at", progress.get("started_at", ""))
                                if updated > latest_time:
                                    latest_time = updated
                                    latest_task = self._decorate_task_with_project_context(
                                        progress,
                                        task_dir_entry,
                                        proj_name,
                                    )
                            elif str(task_status or "").lower() in attention_statuses:
                                updated = progress.get("updated_at", progress.get("completed_at", progress.get("started_at", "")))
                                if self._is_recent_task_timestamp(updated) and updated > latest_attention_time:
                                    latest_attention_time = updated
                                    latest_attention_task = self._decorate_task_with_project_context(
                                        progress,
                                        task_dir_entry,
                                        proj_name,
                                    )
                        except (json.JSONDecodeError, OSError):
                            continue
            except OSError:
                pass

        # Also check hub tasks (no progress.json, only state.json)
        for proj_name, tasks_dir_path in self._all_tasks_dirs():
            if requested_project and proj_name != requested_project:
                continue
            try:
                for task_dir_entry in tasks_dir_path.iterdir():
                    if not task_dir_entry.is_dir() or not task_dir_entry.name.startswith("hub-"):
                        continue
                    progress_file = task_dir_entry / "progress.json"
                    if progress_file.is_file():
                        continue  # already handled above
                    state_file = task_dir_entry / "state.json"
                    if not state_file.is_file():
                        continue
                    try:
                        state = json.loads(state_file.read_text("utf-8"))
                        status = str(state.get("status") or "").lower()
                        if status in ("running", "in_progress"):
                            updated = state.get("updated_at") or state.get("created_at", "")
                            if updated > latest_time:
                                latest_time = updated
                                latest_task = self._decorate_task_with_project_context(
                                    self._build_hub_task_data(state, task_dir_entry),
                                    task_dir_entry,
                                    proj_name,
                                )
                        elif status in attention_statuses:
                            updated = state.get("updated_at") or state.get("completed_at") or state.get("created_at", "")
                            if self._is_recent_task_timestamp(updated) and updated > latest_attention_time:
                                latest_attention_time = updated
                                latest_attention_task = self._decorate_task_with_project_context(
                                    self._build_hub_task_data(state, task_dir_entry),
                                    task_dir_entry,
                                    proj_name,
                                )
                    except (json.JSONDecodeError, OSError):
                        continue
            except OSError:
                pass

        if not latest_task and latest_attention_task:
            latest_task = latest_attention_task

        if latest_task:
            # 补充 sub_tasks（state.json 独有，progress.json 没有）
            task_id = latest_task.get("task_id", "")
            task_state = None
            if task_id:
                task_dir = self._find_task_dir(task_id, latest_task.get("project") or None)
                state_file = task_dir / "state.json"
                if state_file.is_file():
                    try:
                        task_state = json.loads(state_file.read_text("utf-8"))
                    except (json.JSONDecodeError, OSError):
                        task_state = None
            if isinstance(task_state, dict) and "sub_tasks" not in latest_task and task_state.get("sub_tasks"):
                latest_task["sub_tasks"] = task_state["sub_tasks"]
            latest_task = self._decorate_task_display_fields(latest_task, task_state, task_id)
            latest_task = self._attach_runtime_facts(latest_task, task_state)
            latest_task = self._attach_output_artifacts(latest_task, task_dir)
            latest_task = self._normalize_task_live_state(latest_task)

            # 查找匹配的待确认请求
            task_id = latest_task.get("task_id", "")
            result = {"has_task": True, "task": latest_task}
            result["task"] = await self._attach_pending_confirm(result["task"], task_id)
            return web.json_response(result)
        else:
            return web.json_response({"has_task": False})

    async def handle_opus_logs(self, request):
        """GET /vizo/console/api/opus/logs — get historical action logs for a task step.

        Optional: run_index=N to get actions for the Nth run only (0-based).
        When a step is retried (resume), multiple runs share the same .actions.jsonl,
        separated by 'init' type actions. run_index selects a specific run segment.
        Default (no run_index): returns the last run segment.
        Use run_index=-1 to get all actions without segmentation.
        """
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        task_id = request.query.get("task_id", "")
        step_id = request.query.get("step_id", "")
        role = request.query.get("role", "")  # fallback: 旧任务文件名是 role
        sub_id = request.query.get("sub_id", "")
        project = request.query.get("project", "")
        run_index_str = request.query.get("run_index", "")
        if not task_id or not step_id:
            return web.json_response({"actions": []})

        task_base = self._find_task_dir(task_id, project or None)
        if sub_id:
            jsonl_path = str(task_base / f"sub-{sub_id}" / "logs" / f"{step_id}.actions.jsonl")
            # fallback: 旧任务用 role 作文件名
            if not os.path.isfile(jsonl_path) and role and role != step_id:
                jsonl_path = str(task_base / f"sub-{sub_id}" / "logs" / f"{role}.actions.jsonl")
            # fallback: 独立子任务目录（子 orchestrator 写入位置）
            if not os.path.isfile(jsonl_path):
                sub_task_dir = task_base.parent / f"{task_id}-sub{sub_id}"
                jsonl_path = str(sub_task_dir / "logs" / f"{step_id}.actions.jsonl")
                if not os.path.isfile(jsonl_path) and role and role != step_id:
                    jsonl_path = str(sub_task_dir / "logs" / f"{role}.actions.jsonl")
        else:
            jsonl_path = str(task_base / "logs" / f"{step_id}.actions.jsonl")
            # fallback: 旧任务用 role 作文件名
            if not os.path.isfile(jsonl_path) and role and role != step_id:
                jsonl_path = str(task_base / "logs" / f"{role}.actions.jsonl")
        if not os.path.isfile(jsonl_path):
            actions = self._load_fallback_step_actions(task_base, task_id, step_id, role, sub_id)
            return web.json_response({"actions": actions, "total_runs": 1})

        actions = []
        try:
            with open(jsonl_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            actions.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except OSError:
            pass

        if not actions:
            fallback_actions = self._load_fallback_step_actions(task_base, task_id, step_id, role, sub_id)
            if fallback_actions:
                return web.json_response({"actions": fallback_actions, "total_runs": 1})

        # 按 init action 分段（多次运行共用同一个 jsonl 文件）
        init_positions = [i for i, a in enumerate(actions) if a.get("type") == "init"]
        total_runs = len(init_positions) if init_positions else 1

        if run_index_str == "-1":
            # 不分段，返回全部
            pass
        elif total_runs > 1:
            if run_index_str:
                try:
                    ri = int(run_index_str)
                except ValueError:
                    ri = total_runs - 1
            else:
                ri = total_runs - 1  # 默认返回最后一次运行
            ri = max(0, min(ri, total_runs - 1))
            seg_start = init_positions[ri]
            seg_end = init_positions[ri + 1] if ri + 1 < total_runs else len(actions)
            actions = actions[seg_start:seg_end]

        # Cap at 500 entries (same as frontend buffer limit)
        if len(actions) > 500:
            actions = actions[-500:]

        return web.json_response({"actions": actions, "total_runs": total_runs})

    async def handle_opus_subtask(self, request):
        """GET /vizo/console/api/opus/subtask — 获取子任务内部步骤详情"""
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        task_id = request.query.get("task_id", "")
        sub_name = request.query.get("sub_name", "")
        project = request.query.get("project", "")
        if not task_id or not sub_name:
            return web.json_response({"found": False})

        # 从 state.json 读取 sub_tasks 列表，匹配子任务目录
        task_base = self._find_task_dir(task_id, project or None)
        state_file = task_base / "state.json"
        if not state_file.is_file():
            return web.json_response({"found": False})

        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return web.json_response({"found": False})

        # 遍历 sub_tasks 找到匹配的子任务
        sub_tasks = state.get("sub_tasks", [])
        matched_sub_dir = None
        matched_sub_id = ""
        for sub in sub_tasks:
            sub_id = sub.get("id", "")
            if sub.get("name", "") == sub_name or str(sub_id) == sub_name:
                matched_sub_dir = task_base / f"sub-{sub_id}"
                matched_sub_id = str(sub_id)
                break

        if not matched_sub_dir or not matched_sub_dir.is_dir():
            return web.json_response({"found": False})

        # 读取子任务 progress.json
        progress_file = matched_sub_dir / "progress.json"
        if not progress_file.is_file():
            return web.json_response({"found": False})

        try:
            progress = json.loads(progress_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return web.json_response({"found": False})

        sub_state = None
        sub_state_file = matched_sub_dir / "state.json"
        if sub_state_file.is_file():
            try:
                sub_state = json.loads(sub_state_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                sub_state = None

        # 计算费用
        cost_usd = sum(s.get("cost_usd", 0) for s in progress.get("steps", []) if s.get("billable", True))
        sub_task_payload = {
            "task_id": progress.get("task_id", ""),
            "name": sub_name,
            "sub_id": matched_sub_id,
            "status": progress.get("status", "running"),
            "current_step": progress.get("current_step", ""),
            "current_role": progress.get("current_role", ""),
            "steps": progress.get("steps", []),
            "cost_usd": round(cost_usd, 2),
        }
        sub_task_payload = self._attach_runtime_facts(sub_task_payload, sub_state)

        return web.json_response({
            "found": True,
            "sub_task": sub_task_payload
        })
