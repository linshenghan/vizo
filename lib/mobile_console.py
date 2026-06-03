"""
Mobile Web Console for Vizo.

`/vizo/m` keeps mobile PWA endpoints, then redirects users to the PC Dialogue
Console. Mobile no longer renders a separate Dialogue Console shell.
"""

import logging

from aiohttp import web

logger = logging.getLogger("mobile_console")


class MobileConsoleHandler:
    """Mobile console handler — PWA + redirect routes."""

    def __init__(self, config: dict, password_manager=None):
        self._config = config
        self._password_mgr = password_manager

    def _check_auth(self, request) -> bool:
        """Console access is open; legacy auth guards remain as no-op checks."""
        return True

    async def handle_console(self, request):
        """GET /vizo/m"""
        raise web.HTTPFound("/vizo/console/dialogue")

    async def handle_pwa_manifest(self, request):
        """GET /vizo/m/manifest.json"""
        return web.Response(
            text=PWA_MANIFEST_JSON,
            content_type="application/manifest+json",
        )

    async def handle_pwa_sw(self, request):
        """GET /vizo/m/sw.js"""
        return web.Response(
            text=PWA_SW_JS,
            content_type="application/javascript",
            headers={"Service-Worker-Allowed": "/vizo/m"},
        )


# ============================================================
# Mobile Console SPA HTML
# ============================================================
MOBILE_CONSOLE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="manifest" href="/vizo/m/manifest.json">
<meta name="theme-color" content="#0ea5e9">
<title>Vizo Mobile</title>
<link rel="stylesheet" href="/vizo/static/xterm/xterm.css">
<script src="/vizo/static/xterm/xterm.js"></script>
<script src="/vizo/static/xterm/addon-fit.js"></script>
<script src="/vizo/static/xterm/addon-web-links.js"></script>
<script src="/vizo/static/xterm/addon-canvas.js"></script>
<style>
/* ============================================================
   CSS Variables
   ============================================================ */
:root {
  --bg-primary: #0a0e17;
  --bg-secondary: #111827;
  --bg-tertiary: #1a2332;
  --bg-card: #151e2d;
  --accent: #38bdf8;
  --accent-dim: rgba(56,189,248,0.12);
  --green: #22c55e;
  --green-dim: rgba(34,197,94,0.12);
  --yellow: #eab308;
  --yellow-dim: rgba(234,179,8,0.12);
  --red: #ef4444;
  --red-dim: rgba(239,68,68,0.12);
  --purple: #a78bfa;
  --text-primary: #e2e8f0;
  --text-secondary: #94a3b8;
  --text-muted: #64748b;
  --border: #1e293b;
  --border-subtle: rgba(255,255,255,0.06);
  --safe-area-bottom: env(safe-area-inset-bottom, 0px);
}

/* ============================================================
   Reset & Global
   ============================================================ */
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body {
  height: 100vh;
  height: 100dvh;
  height: -webkit-fill-available;
  overflow: hidden;
  background: var(--bg-primary);
  color: var(--text-primary);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans SC", sans-serif;
  -webkit-tap-highlight-color: transparent;
  user-select: none; -webkit-user-select: none;
}

/* ============================================================
   Layout
   ============================================================ */
#app {
  display: flex; flex-direction: column;
  height: 100vh;
  height: 100dvh;
  height: -webkit-fill-available;
  position: fixed; inset: 0;
}

/* Header */
.m-header {
  height: 48px; min-height: 48px;
  display: flex; align-items: center;
  padding: 0 12px; gap: 8px;
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border-subtle);
  z-index: 10;
}
.m-header-menu {
  width: 36px; height: 36px;
  display: flex; align-items: center; justify-content: center;
  background: none; border: none; color: var(--text-secondary);
  font-size: 1.2rem; cursor: pointer; border-radius: 8px;
}
.m-header-menu:active { color: var(--accent); }
.m-header-brand {
  font-size: 0.95rem; font-weight: 700;
  background: linear-gradient(135deg, var(--accent), var(--purple));
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  flex-shrink: 0;
}
.m-header-title {
  flex: 1; text-align: center;
  font-size: 0.85rem; font-weight: 600;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.m-header-status {
  display: flex; align-items: center; gap: 5px;
  font-size: 0.7rem; color: var(--text-muted); flex-shrink: 0;
}
.m-header-status.connected { color: var(--green); }
.m-header-dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--text-muted);
}
.m-header-status.connected .m-header-dot {
  background: var(--green); box-shadow: 0 0 6px var(--green);
  animation: pulse 2s infinite;
}
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
@keyframes breathe { 0%,100%{box-shadow:0 0 4px var(--green)} 50%{box-shadow:0 0 12px var(--green), 0 0 20px rgba(34,197,94,0.3)} }

/* Notification Bar */
.m-notif {
  display: none;
  padding: 10px 14px;
  font-size: 0.78rem;
  align-items: center; gap: 10px;
  cursor: pointer; flex-shrink: 0;
  transition: background 0.15s;
}
.m-notif:active { opacity: 0.8; }
.m-notif.info { background: var(--green-dim); border-bottom: 1px solid rgba(34,197,94,0.2); display: flex; }
.m-notif.warning { background: var(--yellow-dim); border-bottom: 1px solid rgba(234,179,8,0.2); display: flex; }
.m-notif-icon { font-size: 0.9rem; flex-shrink: 0; }
.m-notif-body { flex: 1; min-width: 0; }
.m-notif-title { font-size: 0.78rem; font-weight: 600; }
.m-notif.info .m-notif-title { color: var(--green); }
.m-notif.warning .m-notif-title { color: var(--yellow); }
.m-notif-desc { font-size: 0.7rem; color: var(--text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-top: 2px; }
.m-notif-text { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.m-notif-arrow { color: var(--text-muted); font-size: 0.9rem; flex-shrink: 0; }
.m-notif-close { background: none; border: none; color: var(--text-muted); font-size: 0.85rem; cursor: pointer; padding: 4px; flex-shrink: 0; }

/* Content Area */
.m-content { flex: 1; overflow: hidden; position: relative; }

/* Tab Bar */
.m-tabbar {
  display: flex;
  background: var(--bg-secondary);
  border-top: 1px solid var(--border-subtle);
  padding-bottom: var(--safe-area-bottom);
  z-index: 10;
}
.m-tab {
  flex: 1; display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  height: 56px;
  background: none; border: none;
  color: var(--text-muted); font-size: 0.68rem;
  cursor: pointer; gap: 3px;
  transition: color 0.2s;
  position: relative; user-select: none;
}
.m-tab.active { color: var(--accent); }
.m-tab-icon { font-size: 1.2rem; line-height: 1; }
.m-tab-badge {
  position: absolute; top: 4px; right: calc(50% - 20px);
  background: var(--red); color: #fff; font-size: 0.55rem;
  min-width: 16px; height: 16px; border-radius: 8px;
  display: none; align-items: center; justify-content: center; font-weight: 700;
}
.m-tab-badge.show { display: flex; }

/* Tab Panels */
.m-panel { display: none; height: 100%; flex-direction: column; position: relative; }
.m-panel.active { display: flex; }

/* ============================================================
   Chat Tab (Terminal)
   ============================================================ */
#terminalContainer {
  flex: 1; overflow: hidden;
  background: var(--bg-primary);
}
#terminalContainer .xterm { height: 100%; }
/* Fix: keep xterm textarea at top of viewport so browser never needs to scroll to show it */
#terminalContainer .xterm-helper-textarea {
  position: fixed !important;
  top: 0 !important;
  left: 0 !important;
  width: 1px !important;
  height: 1px !important;
}

/* Chat panel needs relative positioning for FAB absolute placement */
#panelChat { position: relative; }

/* Terminal scroll-to-bottom FAB — pill style matching live panel */
#termScrollFab {
  position: absolute;
  bottom: 64px;
  left: 50%;
  transform: translateX(-50%);
  display: none;
  padding: 5px 12px;
  background: var(--accent);
  color: #000;
  border: none;
  border-radius: 14px;
  font-size: 0.68rem;
  font-weight: 700;
  cursor: pointer;
  z-index: 10;
  box-shadow: 0 2px 8px rgba(0,0,0,0.3);
}
#termScrollFab:active { opacity: 0.8; }

/* Mobile Input Row — replaces xterm native keyboard input */
.m-input-row {
  display: flex; align-items: flex-end;
  padding: 6px 8px;
  background: var(--bg-secondary);
  border-top: 1px solid var(--border-subtle);
  gap: 6px; flex-shrink: 0;
}
.m-input-wrap {
  flex: 1; min-width: 0;
  background: var(--bg-tertiary);
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
}
#mInput {
  width: 100%; border: none; outline: none;
  background: transparent;
  color: var(--text-primary);
  font-size: 14px; line-height: 1.4;
  font-family: "SF Mono", "Fira Code", Menlo, monospace;
  padding: 8px 10px;
  resize: none;
  max-height: 120px;
  min-height: 36px;
  -webkit-appearance: none;
  autocorrect: off; autocapitalize: off;
}
#mInput::placeholder { color: var(--text-muted); }
.m-send-btn {
  flex-shrink: 0;
  width: 36px; height: 36px;
  border: none; border-radius: 8px;
  background: var(--accent);
  color: #fff;
  font-size: 1.1rem;
  cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  touch-action: manipulation;
}
.m-send-btn:active { opacity: 0.7; transform: scale(0.92); }

/* Shortcut Toolbar */
.m-shortcuts {
  display: flex; align-items: center;
  height: 38px; min-height: 38px;
  padding: 0 6px; gap: 3px;
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border-subtle);
  overflow-x: auto; -webkit-overflow-scrolling: touch;
  scrollbar-width: none; flex-shrink: 0;
}
.m-shortcuts::-webkit-scrollbar { display: none; }
.m-shortcut-btn {
  flex-shrink: 0;
  padding: 4px 9px;
  background: var(--bg-tertiary);
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text-secondary);
  font-size: 0.7rem; font-weight: 600;
  font-family: "SF Mono", "Fira Code", monospace;
  cursor: pointer; white-space: nowrap;
  min-height: 26px; display: flex; align-items: center;
  touch-action: manipulation;
}
.m-shortcut-btn:active { background: var(--accent-dim); color: var(--accent); border-color: var(--accent); transform: scale(0.93); }
.m-sk-sep { width: 1px; height: 18px; background: var(--border); flex-shrink: 0; margin: 0 1px; }

/* ESC Interrupt Overlay — floats over shortcuts bar */
.m-esc-overlay {
  display: none; align-items: center;
  position: absolute; top: 0; left: 0; right: 0;
  height: 38px; z-index: 20;
  padding: 0 12px; gap: 8px;
  background: rgba(239, 68, 68, 0.15);
  backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
  border-bottom: 1px solid rgba(239, 68, 68, 0.3);
}
.m-esc-overlay.visible { display: flex; }
.m-esc-label { flex: 1; font-size: 0.75rem; color: var(--red); font-weight: 500; }
.m-esc-confirm {
  padding: 5px 14px; background: var(--red);
  border: none; border-radius: 6px;
  color: white; font-size: 0.75rem; font-weight: 600;
  cursor: pointer; touch-action: manipulation;
}
.m-esc-confirm:active { opacity: 0.8; }
.m-esc-cancel {
  padding: 5px 10px; background: var(--bg-tertiary);
  border: 1px solid var(--border); border-radius: 6px;
  color: var(--text-secondary); font-size: 0.75rem;
  cursor: pointer; touch-action: manipulation;
}
.m-esc-cancel:active { opacity: 0.7; }

/* Workflow Buttons */
.m-workflows {
  display: flex; align-items: center;
  height: 38px; min-height: 38px;
  padding: 0 6px; gap: 4px;
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border-subtle);
  overflow-x: auto; -webkit-overflow-scrolling: touch;
  scrollbar-width: none; flex-shrink: 0;
}
.m-workflows::-webkit-scrollbar { display: none; }
.m-wf-btn {
  flex-shrink: 0;
  padding: 4px 10px;
  border: none; border-radius: 6px;
  color: #fff; font-size: 0.68rem; font-weight: 600;
  cursor: pointer; white-space: nowrap;
  min-height: 26px; display: flex; align-items: center; gap: 4px;
}
.m-wf-btn:active { opacity: 0.7; transform: scale(0.93); }

/* ============================================================
   Task Tab
   ============================================================ */
.m-task-list {
  flex: 1; overflow-y: auto; -webkit-overflow-scrolling: touch;
  padding: 8px;
}
.m-task-group-label {
  font-size: 0.7rem; color: var(--text-muted);
  padding: 8px 4px 4px; text-transform: uppercase; letter-spacing: 0.5px;
}
.m-task-card {
  background: var(--bg-card);
  border: 1px solid var(--border-subtle);
  border-radius: 12px;
  padding: 14px 16px;
  margin-bottom: 10px;
  cursor: pointer;
  transition: border-color 0.2s, transform 0.1s;
}
.m-task-card:active { transform: scale(0.98); border-color: var(--accent); }
.m-task-card-header {
  display: flex; align-items: center; gap: 8px; margin-bottom: 6px;
}
.m-task-status-dot {
  width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
}
.m-task-status-dot.running { background: var(--green); animation: pulse 1.5s infinite; }
.m-task-status-dot.waiting_confirm { background: var(--yellow); animation: pulse 1.5s infinite; }
.m-task-status-dot.paused { background: var(--yellow); }
.m-task-status-dot.completed { background: var(--green); }
.m-task-status-dot.rolled_back { background: var(--red); }
.m-task-status-dot.failed { background: var(--red); }
.m-task-status-dot.partially-failed { background: var(--yellow); }
.m-task-status-dot.in_progress { background: var(--green); animation: pulse 1.5s infinite; }
.m-task-card-name {
  flex: 1; font-size: 0.88rem; font-weight: 600;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.tc-tag { font-size: 0.62rem; padding: 2px 8px; border-radius: 6px; font-weight: 600; flex-shrink: 0; }
.tc-tag.running, .tc-tag.in_progress { background: var(--green-dim); color: var(--green); }
.tc-tag.waiting_confirm { background: var(--yellow-dim); color: var(--yellow); }
.tc-tag.completed { background: var(--accent-dim); color: var(--accent); }
.tc-tag.failed { background: var(--red-dim); color: var(--red); }
.tc-tag.rolled_back { background: var(--red-dim); color: var(--red); }
.tc-tag.paused { background: var(--yellow-dim); color: var(--yellow); }
.tc-tag.partially-failed { background: var(--yellow-dim); color: var(--yellow); }
.m-task-card-meta {
  display: flex; gap: 10px; flex-wrap: wrap;
  font-size: 0.72rem; color: var(--text-muted); margin-bottom: 8px;
}
.m-task-progress {
  height: 4px; background: var(--bg-tertiary);
  border-radius: 2px; margin-bottom: 10px; overflow: hidden;
}
.m-task-progress-bar {
  height: 100%; border-radius: 2px;
  background: linear-gradient(90deg, var(--accent), var(--green));
  transition: width 0.5s;
}
.m-task-progress-bar.failed { background: var(--red); }
.tc-footer { display: flex; align-items: center; gap: 8px; }
.tc-wf { display: flex; gap: 3px; flex: 1; overflow-x: auto; }
.tc-wf::-webkit-scrollbar { display: none; }
.wf-step { font-size: 0.6rem; padding: 2px 6px; border-radius: 5px; white-space: nowrap; flex-shrink: 0; }
.wf-step.done { background: var(--green-dim); color: var(--green); }
.wf-step.current { background: var(--accent-dim); color: var(--accent); border: 1px solid var(--accent); }
.wf-step.pending { background: rgba(255,255,255,.04); color: var(--text-muted); }
.wf-step.failed { background: var(--red-dim); color: var(--red); }
.tc-live-btn { padding: 5px 12px; border-radius: 8px; font-size: 0.7rem; font-weight: 600; cursor: pointer; border: none; flex-shrink: 0; display: flex; align-items: center; gap: 5px; }
.tc-live-btn.live { background: var(--green); color: #fff; animation: breathe 2s infinite; }
.tc-live-btn.replay { background: var(--bg-tertiary); color: var(--text-secondary); border: 1px solid var(--border); }
.tc-live-btn:active { opacity: .7; }
.tc-live-dot { width: 6px; height: 6px; border-radius: 50%; background: #fff; animation: pulse 1s infinite; }
.m-task-empty {
  text-align: center; color: var(--text-muted);
  padding: 3rem 1rem; font-size: 0.85rem;
}

/* ============================================================
   Session Drawer
   ============================================================ */
.m-drawer-backdrop {
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.5);
  z-index: 300; opacity: 0;
  pointer-events: none;
  transition: opacity 0.3s;
}
.m-drawer-backdrop.visible { opacity: 1; pointer-events: auto; }
.m-drawer {
  position: fixed; top: 0; left: 0; bottom: 0;
  width: 280px; max-width: 80vw;
  background: var(--bg-secondary);
  z-index: 301;
  transform: translateX(-100%);
  transition: transform 0.3s ease;
  display: flex; flex-direction: column;
}
.m-drawer.open { transform: translateX(0); }
.m-drawer-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 16px; border-bottom: 1px solid var(--border);
}
.m-drawer-title { font-size: 1rem; font-weight: 600; }
.m-drawer-close {
  background: none; border: none; color: var(--text-muted);
  font-size: 1.2rem; cursor: pointer;
}
.m-drawer-list {
  flex: 1; overflow-y: auto; padding: 8px;
}
.m-session-item {
  display: flex; align-items: center;
  padding: 10px 12px; border-radius: 8px;
  cursor: pointer; gap: 8px;
  margin-bottom: 4px;
}
.m-session-item:active, .m-session-item.active { background: var(--bg-tertiary); }
.m-session-dot {
  width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
}
.m-session-dot.alive { background: var(--green); }
.m-session-dot.dead { background: var(--text-muted); }
.m-session-name { flex: 1; font-size: 0.85rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.m-session-del {
  background: none; border: none; color: var(--red);
  font-size: 0.8rem; cursor: pointer; padding: 4px;
  opacity: 0.6;
}
.m-session-del:active { opacity: 1; }
.m-drawer-footer {
  padding: 12px; border-top: 1px solid var(--border);
}
.m-drawer-new-btn {
  width: 100%; padding: 10px;
  background: var(--accent); color: #000;
  border: none; border-radius: 8px;
  font-size: 0.85rem; font-weight: 600;
  cursor: pointer;
}
.m-drawer-new-btn:active { opacity: 0.8; }

/* ============================================================
   Bottom Sheet (New Session)
   ============================================================ */
.m-sheet-backdrop {
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.5);
  z-index: 350; opacity: 0;
  pointer-events: none;
  transition: opacity 0.3s;
}
.m-sheet-backdrop.visible { opacity: 1; pointer-events: auto; }
.m-sheet {
  position: fixed; left: 0; right: 0; bottom: 0;
  background: var(--bg-secondary);
  border-top-left-radius: 16px; border-top-right-radius: 16px;
  z-index: 351;
  transform: translateY(100%);
  transition: transform 0.3s ease;
  padding: 20px 16px calc(16px + var(--safe-area-bottom));
}
.m-sheet.open { transform: translateY(0); }
.m-sheet-handle {
  width: 36px; height: 4px; background: var(--text-muted);
  border-radius: 2px; margin: 0 auto 16px;
}
.m-sheet-title { font-size: 1rem; font-weight: 600; margin-bottom: 16px; }
.m-sheet-label { font-size: 0.8rem; color: var(--text-muted); margin-bottom: 4px; }
.m-sheet-input {
  width: 100%; padding: 10px 12px;
  background: var(--bg-primary); border: 1px solid var(--border);
  border-radius: 8px; color: var(--text-primary);
  font-size: 0.9rem; outline: none; margin-bottom: 12px;
  -webkit-appearance: none;
}
.m-sheet-input:focus { border-color: var(--accent); }
.m-sheet-select {
  width: 100%; padding: 10px 12px;
  background: var(--bg-primary); border: 1px solid var(--border);
  border-radius: 8px; color: var(--text-primary);
  font-size: 0.9rem; outline: none; margin-bottom: 16px;
  -webkit-appearance: none;
}
.m-sheet-submit {
  width: 100%; padding: 12px;
  background: var(--accent); color: #000;
  border: none; border-radius: 8px;
  font-size: 0.9rem; font-weight: 600; cursor: pointer;
}
.m-sheet-submit:active { opacity: 0.8; }

/* ============================================================
   Task Detail Overlay
   ============================================================ */
.m-overlay {
  position: fixed; inset: 0;
  background: var(--bg-primary);
  z-index: 200;
  transform: translateY(100%);
  transition: transform 0.3s ease;
  display: flex; flex-direction: column;
  overflow: hidden;
}
.m-overlay.open { transform: translateY(0); }
.m-overlay-header {
  height: 48px; min-height: 48px;
  display: flex; align-items: center;
  padding: 0 12px; gap: 8px;
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border);
}
.m-overlay-back {
  background: none; border: none; color: var(--accent);
  font-size: 1rem; cursor: pointer; padding: 4px 8px;
}
.m-overlay-title {
  flex: 1; font-size: 0.9rem; font-weight: 600;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.m-overlay-body {
  flex: 1; overflow-y: auto; -webkit-overflow-scrolling: touch;
  padding: 12px;
}

/* Task Detail Content */
/* Detail sections (grid layout) */
.d-section { margin-bottom: 16px; }
.d-section-title { font-size: 0.72rem; color: var(--text-muted); font-weight: 600; margin-bottom: 8px; text-transform: uppercase; letter-spacing: .3px; }
.d-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.d-item { background: var(--bg-card); border: 1px solid var(--border-subtle); border-radius: 8px; padding: 10px 12px; }
.d-label { font-size: 0.65rem; color: var(--text-muted); margin-bottom: 2px; }
.d-value { font-size: 0.85rem; font-weight: 600; }
.m-detail-actions {
  display: flex; gap: 8px; padding: 10px 14px; flex-wrap: wrap;
  border-top: 1px solid var(--border-subtle);
  background: var(--bg-secondary); flex-shrink: 0;
  position: sticky; bottom: 0;
}
.m-detail-btn {
  flex: 1; padding: 11px;
  border: 1px solid var(--border); border-radius: 10px;
  background: var(--bg-tertiary);
  font-size: 0.82rem; font-weight: 600;
  cursor: pointer; color: var(--text-primary); text-align: center;
}
.m-detail-btn:active { opacity: 0.7; }
.m-detail-btn:disabled { opacity: 0.4; cursor: not-allowed; }
.m-detail-btn.pause { color: var(--yellow); border-color: var(--yellow); }
.m-detail-btn.terminate { color: var(--red); border-color: var(--red); }
.m-detail-btn.resume { background: var(--green); color: #fff; border-color: var(--green); }
.m-detail-btn.approve { background: var(--green); color: #000; border-color: var(--green); }
.m-detail-btn.reject { color: var(--red); border-color: var(--red); }
.m-detail-btn.live { background: var(--accent); color: #fff; border-color: var(--accent); }

/* Workflow detail steps */
.wf-detail-step { display: flex; gap: 10px; padding: 10px 12px; background: var(--bg-card); border: 1px solid var(--border-subtle); border-radius: 10px; margin-bottom: 6px; }
.wf-detail-step.done { border-left: 3px solid var(--green); }
.wf-detail-step.current { border-left: 3px solid var(--accent); background: var(--accent-dim); }
.wf-detail-step.pending { border-left: 3px solid var(--border); opacity: .6; }
.wf-detail-step.failed { border-left: 3px solid var(--red); }
.wf-icon { font-size: 0.85rem; flex-shrink: 0; margin-top: 2px; }
.wf-info { flex: 1; min-width: 0; }
.wf-name { font-size: 0.82rem; font-weight: 600; margin-bottom: 4px; }
.wf-meta { display: flex; gap: 8px; flex-wrap: wrap; font-size: 0.68rem; color: var(--text-muted); }
.wf-meta span { display: flex; align-items: center; gap: 3px; }

/* ============================================================
   Live Overlay
   ============================================================ */
.m-live-task-card { background: var(--bg-card); border: 1px solid var(--border-subtle); border-radius: 12px; padding: 12px 14px; margin-bottom: 12px; }
.m-ltc-row { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.m-ltc-row:last-child { margin-bottom: 0; }
.m-ltc-name { font-size: 0.88rem; font-weight: 600; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.m-ltc-meta { display: flex; gap: 10px; font-size: 0.7rem; color: var(--text-muted); flex-wrap: wrap; }

.m-live-panels {
  flex: 1; overflow-y: auto; -webkit-overflow-scrolling: touch;
  padding: 0 14px 14px; display: flex; flex-direction: column; gap: 8px;
}
.m-live-panels::-webkit-scrollbar { display: none; }
.lp {
  background: var(--bg-card); border: 1px solid var(--border-subtle);
  border-radius: 12px; overflow: hidden;
  display: flex; flex-direction: column; flex-shrink: 0;
  position: relative;
}
.lp.expanded { flex-shrink: 0; }
.lp-sub-group {
  padding: 5px 14px; font-size: 0.72rem; font-weight: 600;
  color: var(--accent); display: flex; align-items: center; gap: 5px;
  margin: 6px 0 2px;
}
.lp-sub-group .sub-badge {
  background: var(--accent); color: var(--bg); border-radius: 6px;
  padding: 1px 6px; font-size: 0.65rem; font-weight: 700;
}
.lp.lp-sub-indent { margin-left: 10px; }
.lp-header {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 14px; cursor: pointer; flex-shrink: 0;
  border-bottom: 1px solid var(--border-subtle);
}
.lp-header:active { background: var(--bg-tertiary); }
.lp-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.lp-dot.active { background: var(--green); animation: pulse 1.5s infinite; }
.lp-dot.done { background: var(--accent); }
.lp-dot.idle { background: var(--text-muted); }
.lp-name { font-size: 0.82rem; font-weight: 600; }
.lp-model { font-size: 0.68rem; color: var(--text-muted); }
.lp-time { font-size: 0.68rem; color: var(--green); margin-left: auto; }
.lp-expand-icon { color: var(--text-muted); font-size: 0.8rem; transition: transform 0.2s; }
.lp.expanded .lp-expand-icon { transform: rotate(180deg); }
.lp-body {
  overflow: hidden;
  font-family: 'SF Mono','Fira Code',monospace;
  font-size: 0.72rem; line-height: 1.6; color: var(--text-secondary);
  padding: 0; height: 0; opacity: 0;
  transition: height 0.3s cubic-bezier(0.4,0,0.2,1), padding 0.3s cubic-bezier(0.4,0,0.2,1), opacity 0.25s ease;
}
.lp-body::-webkit-scrollbar { width: 2px; }
.lp-body::-webkit-scrollbar-thumb { background: var(--border); border-radius: 1px; }
.lp.expanded .lp-body { padding: 10px 14px; opacity: 1; }
.lp.collapsed .lp-body { height: 0; opacity: 0; padding-top: 0; padding-bottom: 0; }

/* File browser & doc links */
.wf-doc { margin-top: 5px; }
.wf-doc-link { font-size: 0.7rem; color: var(--accent); text-decoration: underline; cursor: pointer; margin-right: 8px; }
.wf-doc-link:active { opacity: 0.7; }
.fb-list { }
.fb-item { display: flex; align-items: center; gap: 10px; padding: 10px 12px; border-bottom: 1px solid var(--border-subtle); cursor: pointer; }
.fb-item:active { background: var(--bg-tertiary); }
.fb-icon { font-size: 1rem; flex-shrink: 0; }
.fb-name { font-size: 0.82rem; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fb-size { font-size: 0.68rem; color: var(--text-muted); flex-shrink: 0; }
.fb-preview { background: var(--bg-card); border: 1px solid var(--border-subtle); border-radius: 8px; padding: 12px; font-family: "SF Mono","Fira Code",monospace; font-size: 0.72rem; line-height: 1.6; color: var(--text-secondary); white-space: pre-wrap; word-break: break-all; }


.m-log-entry {
  display: flex; gap: 6px;
  font-size: 0.72rem; font-family: 'SF Mono','Fira Code',monospace;
  padding: 2px 0; line-height: 1.4; align-items: flex-start;
}
.m-log-ts { color: var(--text-muted); white-space: nowrap; flex-shrink: 0; }
.m-log-type { font-weight: 600; white-space: nowrap; flex-shrink: 0; }
.m-log-type.read { color: #58a6ff; }
.m-log-type.write { color: #3fb950; }
.m-log-type.exec { color: #d29922; }
.m-log-type.think { color: #bc8cff; font-style: italic; }
.m-log-type.search { color: #f778ba; }
.m-log-type.output { color: #79c0ff; font-style: italic; }
.m-log-type.init { color: #56d364; font-weight: 700; }
.m-log-target { color: var(--text-primary); word-break: break-all; overflow: hidden; text-overflow: ellipsis; }
.m-log-snippet {
  margin: 2px 0 4px 3.2rem; padding: 3px 6px;
  background: rgba(0,0,0,0.3); border: 1px solid var(--border);
  border-radius: 4px; font-size: 0.65rem; color: var(--text-muted);
  font-family: 'SF Mono','Fira Code',monospace;
  white-space: pre-wrap; word-break: break-all;
  max-height: 4.5em; overflow: hidden;
  line-height: 1.5;
}
.m-log-snippet.think-snippet, .m-log-snippet.output-snippet {
  color: var(--text-secondary);
}



/* Per-panel scroll-to-bottom FAB */
.lp-scroll-fab {
  position: sticky; bottom: 8px; align-self: flex-end;
  display: none; padding: 5px 12px; margin-right: 4px;
  background: var(--accent); color: #000;
  border: none; border-radius: 14px;
  font-size: 0.68rem; font-weight: 700;
  cursor: pointer; z-index: 5;
  box-shadow: 0 2px 8px rgba(0,0,0,0.3);
}
.lp-scroll-fab:active { opacity: 0.8; }

/* ============================================================
   File Preview Overlay
   ============================================================ */
.m-file-preview {
  z-index: 250;
}
.m-file-content {
  font-family: 'Menlo', 'Monaco', 'Courier New', monospace;
  font-size: 0.75rem;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--text-primary);
  background: var(--bg-card);
  border-radius: 8px;
  padding: 12px;
  user-select: text; -webkit-user-select: text;
}

/* ============================================================
   Confirm Modal
   ============================================================ */
.m-confirm-backdrop {
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.6);
  z-index: 400;
  display: none; align-items: center; justify-content: center;
  padding: 16px;
}
.m-confirm-backdrop.visible { display: flex; }
.m-confirm-box {
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 16px;
  width: 100%; max-width: 380px;
  padding: 20px; max-height: 80vh;
  overflow-y: auto; position: relative;
}
.m-confirm-title {
  font-size: 1rem; font-weight: 600; margin-bottom: 8px;
}
.m-confirm-summary {
  font-size: 0.8rem; color: var(--text-muted);
  margin-bottom: 16px; line-height: 1.5;
  white-space: pre-wrap;
}
.m-confirm-actions { display: flex; flex-direction: column; gap: 8px; }
.m-confirm-btn {
  width: 100%; padding: 12px;
  border: none; border-radius: 8px;
  font-size: 0.85rem; font-weight: 600;
  cursor: pointer;
}
.m-confirm-btn:active { opacity: 0.8; }
.m-confirm-btn.approve { background: var(--green); color: #000; }
.m-confirm-btn.reject { background: var(--red); color: #fff; }
.m-confirm-btn.feedback { background: var(--bg-tertiary); color: var(--text-primary); border: 1px solid var(--border); }
.m-confirm-feedback {
  display: none; margin-top: 8px;
}
.m-confirm-textarea {
  width: 100%; height: 80px; padding: 10px;
  background: var(--bg-primary); border: 1px solid var(--border);
  border-radius: 8px; color: var(--text-primary);
  font-size: 0.85rem; resize: none; outline: none;
  margin-bottom: 8px;
}
.m-confirm-textarea:focus { border-color: var(--accent); }
.m-confirm-send {
  width: 100%; padding: 10px;
  background: var(--accent); color: #000;
  border: none; border-radius: 8px;
  font-size: 0.85rem; font-weight: 600;
  cursor: pointer;
}

/* ============================================================
   Toast
   ============================================================ */
.m-toast {
  position: fixed; top: 60px; left: 50%; transform: translateX(-50%);
  background: var(--bg-tertiary); color: var(--text-primary);
  padding: 8px 20px; border-radius: 20px;
  font-size: 0.8rem; z-index: 999;
  opacity: 0; transition: opacity 0.3s;
  pointer-events: none;
  border: 1px solid var(--border);
}
.m-toast.visible { opacity: 1; }

/* Copy Overlay — shows terminal text as natively selectable on mobile */
.m-copy-overlay {
  display: none; position: fixed; inset: 0; z-index: 900;
  background: rgba(0,0,0,0.7);
  backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
  flex-direction: column;
}
.m-copy-overlay.visible { display: flex; }
.m-copy-toolbar {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 16px; background: var(--bg-secondary);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.m-copy-toolbar span { color: var(--text-muted); font-size: 0.85rem; flex: 1; }
.m-copy-toolbar button {
  border: none; border-radius: 6px; font-size: 0.8rem;
  cursor: pointer; white-space: nowrap; height: 36px;
  display: flex; align-items: center; justify-content: center;
}
.m-copy-toolbar .m-btn-selectcopy {
  background: var(--accent); color: #fff;
  padding: 0 14px; font-size: 0.75rem; flex-shrink: 0;
}
.m-copy-toolbar .m-btn-close {
  background: var(--bg-tertiary); color: var(--text-primary);
  padding: 0 16px; margin-left: 4px;
}
.m-copy-body {
  flex: 1; overflow: auto; -webkit-overflow-scrolling: touch;
  padding: 12px;
  /* Isolate selection so it cannot extend outside overlay */
  contain: content;
}
.m-copy-body pre {
  margin: 0; white-space: pre; overflow-x: auto;
  color: var(--text-primary); font-size: 12px; line-height: 1.5;
  font-family: Menlo, Monaco, "Courier New", monospace;
  user-select: text; -webkit-user-select: text;
}

/* ====== Model Config (mmc-*) ====== */
.mmc-overlay {
  position: fixed; top: 0; left: 0; right: 0; bottom: 0;
  background: var(--bg-primary); z-index: 200;
  display: flex; flex-direction: column;
}
.mmc-header {
  display: flex; align-items: center; gap: 0.5rem;
  padding: 0.75rem 1rem; border-bottom: 1px solid var(--border);
  background: var(--bg-secondary);
}
.mmc-back {
  background: none; border: none; color: var(--text-primary);
  font-size: 1.25rem; padding: 0.25rem; cursor: pointer;
  min-width: 44px; min-height: 44px; display: flex; align-items: center;
}
.mmc-header-title { font-size: 1rem; font-weight: 600; color: var(--text-primary); }
.mmc-list { flex: 1; overflow-y: auto; padding: 0.5rem 0; }
.mmc-item {
  display: flex; justify-content: space-between; align-items: center;
  padding: 0.85rem 1rem; border-bottom: 1px solid var(--border);
  cursor: pointer; min-height: 44px;
}
.mmc-item:active { background: var(--bg-hover); }
.mmc-item-left { display: flex; flex-direction: column; gap: 0.15rem; }
.mmc-item-name { font-size: 0.9rem; color: var(--text-primary); }
.mmc-item-badge {
  font-size: 0.7rem; color: var(--accent);
  background: rgba(56, 189, 248, 0.1); padding: 0.1rem 0.3rem;
  border-radius: 3px; width: fit-content;
}
.mmc-item-right { display: flex; align-items: center; gap: 0.5rem; }
.mmc-item-model {
  font-size: 0.8rem; color: var(--text-muted);
  max-width: 150px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.mmc-item-arrow { color: var(--text-muted); font-size: 1.2rem; }
.mmc-loading { text-align: center; padding: 3rem; color: var(--text-muted); }
.mmc-picker-backdrop {
  position: fixed; top: 0; left: 0; right: 0; bottom: 0;
  background: rgba(0,0,0,0.5); z-index: 300; display: none;
}
.mmc-picker-sheet {
  position: fixed; bottom: 0; left: 0; right: 0;
  background: var(--bg-secondary); border-radius: 12px 12px 0 0;
  padding: 1rem 0; z-index: 301; display: none;
  transform: translateY(100%); transition: transform 0.3s ease;
  max-height: 70vh; overflow-y: auto;
}
.mmc-picker-sheet.mmc-picker-visible { transform: translateY(0); }
.mmc-picker-title {
  padding: 0.5rem 1rem 0.75rem; font-size: 0.9rem; font-weight: 600;
  color: var(--text-primary); border-bottom: 1px solid var(--border);
}
.mmc-picker-option {
  padding: 0.85rem 1rem; display: flex; justify-content: space-between;
  align-items: center; min-height: 44px; cursor: pointer;
  color: var(--text-primary); font-size: 0.9rem;
}
.mmc-picker-option:active { background: var(--bg-hover); }
.mmc-picker-selected { color: var(--accent); }
.mmc-picker-cancel {
  text-align: center; padding: 0.85rem; margin-top: 0.5rem;
  border-top: 1px solid var(--border); color: var(--text-muted);
  cursor: pointer; min-height: 44px; font-size: 0.9rem;
}
.mmc-entry {
  display: flex; align-items: center; gap: 0.75rem;
  padding: 0.85rem 1rem; border-bottom: 1px solid var(--border);
  cursor: pointer; min-height: 44px;
}
.mmc-entry:active { background: var(--bg-hover); }
.mmc-entry-icon { font-size: 1.1rem; }
.mmc-entry-label { flex: 1; font-size: 0.9rem; color: var(--text-primary); }
.mmc-entry-arrow { color: var(--text-muted); font-size: 1.2rem; }

/* ============================================================
   "我的" Tab — prefix .me-
   ============================================================ */
.me-page { padding: 12px; overflow-y: auto; height: 100%; -webkit-overflow-scrolling: touch; }
.me-section { margin-bottom: 16px; }
.me-section-title {
  font-size: 0.72rem; color: var(--text-muted); font-weight: 600;
  padding: 8px 4px 6px; text-transform: uppercase; letter-spacing: 0.3px;
}
.me-item {
  display: flex; align-items: center; gap: 12px;
  padding: 14px 16px; min-height: 48px;
  background: var(--bg-card); border: 1px solid var(--border-subtle);
  border-radius: 10px; margin-bottom: 4px; cursor: pointer;
  transition: background 0.15s;
}
.me-item:active { background: var(--bg-tertiary); }
.me-item.disabled { opacity: 0.4; pointer-events: none; }
.me-item-icon { font-size: 1.1rem; flex-shrink: 0; width: 24px; text-align: center; }
.me-item-label { flex: 1; font-size: 0.88rem; font-weight: 500; }
.me-item-meta { font-size: 0.72rem; color: var(--text-muted); }
.me-item-arrow { color: var(--text-muted); font-size: 1rem; }

/* ============================================================
   Agent 管理 — prefix .ma-
   ============================================================ */
.ma-add-btn {
  background: none; border: none; color: var(--accent);
  font-size: 1.5rem; cursor: pointer; padding: 4px 8px; line-height: 1;
  min-width: 44px; min-height: 44px; display: flex; align-items: center; justify-content: center;
}
.ma-group-title {
  font-size: 0.72rem; color: var(--text-muted); font-weight: 600;
  padding: 12px 4px 6px; text-transform: uppercase; letter-spacing: 0.3px;
}
.ma-card {
  display: flex; align-items: center; gap: 12px;
  padding: 14px 16px; min-height: 48px;
  background: var(--bg-card); border: 1px solid var(--border-subtle);
  border-radius: 12px; margin-bottom: 8px; cursor: pointer;
  transition: border-color 0.2s;
}
.ma-card:active { border-color: var(--accent); }
.ma-card-icon { font-size: 1.5rem; flex-shrink: 0; width: 36px; text-align: center; }
.ma-card-body { flex: 1; min-width: 0; }
.ma-card-name { font-size: 0.88rem; font-weight: 600; margin-bottom: 2px; }
.ma-card-desc {
  font-size: 0.72rem; color: var(--text-secondary);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ma-card-del {
  background: none; border: none; color: var(--red); font-size: 1rem;
  cursor: pointer; padding: 8px; opacity: 0.6;
  min-width: 44px; min-height: 44px; display: flex; align-items: center; justify-content: center;
}
.ma-card-del:active { opacity: 1; }
.ma-empty { text-align: center; padding: 4rem 2rem; }
.ma-empty-icon { font-size: 3rem; margin-bottom: 12px; }
.ma-empty-text { font-size: 0.88rem; color: var(--text-muted); margin-bottom: 20px; }
.ma-empty-btn {
  padding: 12px 24px; background: var(--accent); color: #000;
  border: none; border-radius: 10px; font-size: 0.88rem; font-weight: 600;
  cursor: pointer; min-height: 44px;
}
.ma-empty-btn:active { opacity: 0.8; }

/* Agent 创建流程 */
.ma-create { padding: 20px 4px; }
.ma-create-hint { font-size: 0.85rem; color: var(--text-secondary); margin-bottom: 16px; }
.ma-create-input {
  width: 100%; min-height: 160px; padding: 12px; resize: none;
  background: var(--bg-card); border: 1px solid var(--border);
  border-radius: 10px; color: var(--text-primary);
  font-size: 0.88rem; line-height: 1.5; outline: none;
  -webkit-appearance: none;
}
.ma-create-input:focus { border-color: var(--accent); }
.ma-create-counter { text-align: right; font-size: 0.7rem; color: var(--text-muted); margin: 6px 0 16px; }
.ma-create-btn {
  width: 100%; padding: 14px; min-height: 48px;
  background: var(--accent); color: #000;
  border: none; border-radius: 10px;
  font-size: 0.92rem; font-weight: 600; cursor: pointer;
}
.ma-create-btn:active { opacity: 0.8; }
.ma-create-btn:disabled { opacity: 0.4; cursor: not-allowed; }

/* Loading */
.ma-loading { text-align: center; padding: 6rem 2rem; }
.ma-loading-spinner {
  width: 40px; height: 40px; margin: 0 auto 20px;
  border: 3px solid var(--border); border-top-color: var(--accent);
  border-radius: 50%; animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.ma-loading-text { font-size: 0.92rem; font-weight: 600; margin-bottom: 8px; }
.ma-loading-hint { font-size: 0.78rem; color: var(--text-muted); }

/* Preview */
.ma-preview { padding: 12px 4px; }
.ma-field { margin-bottom: 14px; }
.ma-field-label { display: block; font-size: 0.75rem; color: var(--text-muted); margin-bottom: 4px; }
.ma-field-input {
  width: 100%; padding: 10px 12px;
  background: var(--bg-card); border: 1px solid var(--border);
  border-radius: 8px; color: var(--text-primary);
  font-size: 0.88rem; outline: none; -webkit-appearance: none;
}
.ma-field-input:focus { border-color: var(--accent); }
.ma-field-textarea { min-height: 60px; resize: none; font-family: inherit; }
.ma-field-error { font-size: 0.72rem; color: var(--red); margin-top: 4px; display: none; }
.ma-steps-title { font-size: 0.78rem; color: var(--text-muted); font-weight: 600; margin: 16px 0 8px; }
.ma-step-item {
  display: flex; gap: 10px; align-items: center;
  padding: 10px 12px; background: var(--bg-card);
  border: 1px solid var(--border-subtle); border-radius: 8px;
  margin-bottom: 6px;
}
.ma-step-num {
  width: 24px; height: 24px; border-radius: 50%;
  background: var(--accent-dim); color: var(--accent);
  display: flex; align-items: center; justify-content: center;
  font-size: 0.72rem; font-weight: 700; flex-shrink: 0;
}
.ma-step-body { flex: 1; min-width: 0; }
.ma-step-name { font-size: 0.82rem; font-weight: 600; }
.ma-step-role { font-size: 0.68rem; color: var(--text-muted); }
.ma-preview-actions {
  display: flex; gap: 10px; margin-top: 20px;
  padding-bottom: calc(20px + var(--safe-area-bottom));
}
.ma-preview-btn {
  flex: 1; padding: 14px; min-height: 48px;
  border: none; border-radius: 10px;
  font-size: 0.88rem; font-weight: 600; cursor: pointer;
}
.ma-preview-btn.primary { background: var(--accent); color: #000; flex: 1.5; }
.ma-preview-btn.secondary { background: var(--bg-tertiary); color: var(--text-secondary); border: 1px solid var(--border); }
.ma-preview-btn:active { opacity: 0.8; }
.ma-preview-btn:disabled { opacity: 0.4; cursor: not-allowed; }

/* Hub 任务标识 */
.m-task-card.hub { border-left: 3px solid var(--green); }
.ma-hub-badge { font-size: 0.85rem; flex-shrink: 0; }
.ma-hub-module {
  font-size: 0.62rem; padding: 2px 8px; border-radius: 6px;
  background: var(--green-dim); color: var(--green); font-weight: 600;
}

/* Tab FAB */
.m-tab-fab { flex: 0.6; color: var(--accent); font-size: 1.4rem; }
</style>
</head>
<body>

<div id="app">
  <!-- Header -->
  <div class="m-header">
    <button class="m-header-menu" id="menuBtn" onclick="toggleDrawer(true)">&#9776;</button>
    <span class="m-header-brand">维造 Vizo</span>
    <div class="m-header-title" id="headerTitle">Vizo</div>
    <div class="m-header-status" id="headerStatus"><span class="m-header-dot"></span> <span id="headerStatusText"></span></div>
    <button style="background:none;border:none;color:var(--text-secondary);font-size:1.1rem;padding:6px;min-width:44px;min-height:44px;cursor:pointer" data-action="open-mobile-model-config" title="模型配置">&#9881;</button>
  </div>

  <!-- Notification Bar -->
  <div class="m-notif" id="notifBar" onclick="handleNotifClick()">
    <span class="m-notif-text" id="notifText"></span>
    <span>&#8250;</span>
  </div>

  <!-- Content -->
  <div class="m-content">
    <!-- Chat Panel -->
    <div class="m-panel active" id="panelChat">
      <!-- ESC Interrupt Overlay (top) -->
      <div class="m-esc-overlay" id="escOverlay">
        <span class="m-esc-label">⚡ 打断当前任务？</span>
        <button class="m-esc-cancel" onclick="hideEscOverlay(true)">取消</button>
        <button class="m-esc-confirm" onclick="confirmEscInterrupt()">打断</button>
      </div>
      <div class="m-shortcuts" id="shortcutBar"></div>
      <div class="m-workflows" id="workflowBar"></div>
      <div id="terminalContainer"></div>
      <button id="termScrollFab" onclick="termScrollToBottom()">&#8595; 底部</button>
      <div class="m-input-row" id="mInputRow">
        <div class="m-input-wrap">
          <textarea id="mInput" rows="1" placeholder="输入命令..."
            autocorrect="off" autocapitalize="off" autocomplete="off" spellcheck="false"></textarea>
        </div>
        <button class="m-send-btn" onclick="doSend()">&#9654;</button>
      </div>
    </div>

    <!-- Task Panel -->
    <div class="m-panel" id="panelTask">
      <div class="m-task-list" id="taskList"></div>
    </div>

    <!-- Me Panel -->
    <div class="m-panel" id="panelMe">
      <div class="me-page">
        <div class="me-section">
          <div class="me-section-title">智能体管理</div>
          <div class="me-item" data-action="openAgents">
            <span class="me-item-icon">&#129302;</span>
            <span class="me-item-label">Agent 模块</span>
            <span class="me-item-meta" id="meAgentCount"></span>
            <span class="me-item-arrow">&#8250;</span>
          </div>
        </div>
        <div class="me-section">
          <div class="me-section-title">系统配置</div>
          <div class="me-item disabled">
            <span class="me-item-icon">&#9881;</span>
            <span class="me-item-label">模型配置</span>
            <span class="me-item-meta">P2</span>
            <span class="me-item-arrow">&#8250;</span>
          </div>
          <div class="me-item disabled">
            <span class="me-item-icon">&#127760;</span>
            <span class="me-item-label">网络配置</span>
            <span class="me-item-meta">P2</span>
            <span class="me-item-arrow">&#8250;</span>
          </div>
        </div>
        <div class="me-section">
          <div class="me-section-title">关于</div>
          <div class="me-item disabled">
            <span class="me-item-icon">&#8505;</span>
            <span class="me-item-label">关于维造 Vizo</span>
            <span class="me-item-arrow">&#8250;</span>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Tab Bar -->
  <div class="m-tabbar">
    <button class="m-tab active" data-panel="panelChat" data-action="switchTab">
      <span class="m-tab-icon">&#128172;</span>
      <span>终端</span>
    </button>
    <button class="m-tab" data-panel="panelTask" data-action="switchTab">
      <span class="m-tab-icon">&#128203;</span>
      <span>任务</span>
    </button>
    <button class="m-tab m-tab-fab" data-action="openNewSession">
      <span class="m-tab-icon">&#10133;</span>
    </button>
    <button class="m-tab" data-panel="panelMe" data-action="switchTab">
      <span class="m-tab-icon">&#128100;</span>
      <span>我的</span>
    </button>
  </div>
</div>

<!-- Session Drawer -->
<div class="m-drawer-backdrop" id="drawerBackdrop" onclick="toggleDrawer(false)"></div>
<div class="m-drawer" id="drawer">
  <div class="m-drawer-header">
    <span class="m-drawer-title">会话</span>
    <button class="m-drawer-close" onclick="toggleDrawer(false)">&times;</button>
  </div>
  <div class="m-drawer-list" id="sessionList"></div>
  <div class="m-drawer-footer">
    <button class="m-drawer-new-btn" onclick="openNewSessionSheet()">+ 新建会话</button>
  </div>
</div>

<!-- New Session Bottom Sheet -->
<div class="m-sheet-backdrop" id="sheetBackdrop" onclick="closeSheet()"></div>
<div class="m-sheet" id="newSessionSheet">
  <div class="m-sheet-handle"></div>
  <div class="m-sheet-title">新建会话</div>
  <div class="m-sheet-label">会话名称</div>
  <input class="m-sheet-input" id="newSessionName" placeholder="可选">
  <div class="m-sheet-label">项目</div>
  <select class="m-sheet-select" id="newSessionProject"></select>
  <button class="m-sheet-submit" onclick="createSession()">创建</button>
</div>

<!-- Task Detail Overlay -->
<div class="m-overlay" id="taskDetailOverlay">
  <div class="m-overlay-header">
    <button class="m-overlay-back" onclick="closeOverlay('taskDetailOverlay')">&larr;</button>
    <div class="m-overlay-title" id="detailTitle">任务详情</div>
  </div>
  <div class="m-overlay-body" id="detailBody"></div>
  <div class="m-detail-actions" id="detailActions"></div>
</div>

<!-- Live Overlay -->
<div class="m-overlay" id="liveOverlay">
  <div class="m-overlay-header">
    <button class="m-overlay-back" onclick="closeOverlay('liveOverlay')">&larr;</button>
    <div class="m-overlay-title" id="liveTitle">&#128308; 直播</div>
  </div>
  <div class="m-overlay-body" id="liveBody" style="padding:14px 0 0; display:flex; flex-direction:column; overflow:hidden;"></div>
</div>

<!-- File Preview Overlay -->
<div class="m-overlay m-file-preview" id="fileOverlay">
  <div class="m-overlay-header">
    <button class="m-overlay-back" onclick="closeOverlay('fileOverlay')">&larr;</button>
    <div class="m-overlay-title" id="fileTitle">文件预览</div>
  </div>
  <div class="m-overlay-body" id="fileBody"></div>
</div>

<!-- Agent Overlay -->
<div class="m-overlay" id="agentOverlay">
  <div class="m-overlay-header">
    <button class="m-overlay-back" data-action="closeAgentOverlay">&#8249;</button>
    <div class="m-overlay-title" id="agentOverlayTitle">Agent 模块</div>
    <button class="ma-add-btn" data-action="startAgentCreate" id="maAddBtn">+</button>
  </div>
  <div class="m-overlay-body" id="agentOverlayBody"></div>
</div>

<!-- Model Config Overlay -->
<div id="mobileModelConfig" class="mmc-overlay" style="display:none">
  <div class="mmc-header">
    <button class="mmc-back" data-action="close-mobile-model-config">&larr;</button>
    <span class="mmc-header-title">模型配置</span>
  </div>
  <div class="mmc-list" id="mmc-list"></div>
</div>

<!-- Action Confirm Modal -->
<div class="m-confirm-backdrop" id="actionConfirmModal">
  <div class="m-confirm-box" style="text-align:center;">
    <div class="m-confirm-title" id="actionConfirmMsg" style="margin-bottom:16px;"></div>
    <div class="m-confirm-actions" style="flex-direction:row;">
      <button class="m-confirm-btn feedback" id="actionConfirmCancel" onclick="closeActionConfirmModal()" style="flex:1;">取消</button>
      <button class="m-confirm-btn approve" id="actionConfirmOk" style="flex:1;">确认</button>
    </div>
  </div>
</div>

<!-- Resume/Retry Modal with feedback -->
<div class="m-confirm-backdrop" id="resumeModal">
  <div class="m-confirm-box">
    <div class="m-confirm-title" id="resumeModalTitle" style="margin-bottom:8px;"></div>
    <textarea id="resumeFeedbackInput" placeholder="可选：输入补充意见或修改指令（留空则直接继续）" style="width:100%;padding:8px;background:var(--bg-primary);border:1px solid var(--border);border-radius:8px;color:var(--text-primary);font-size:0.82rem;resize:none;height:80px;outline:none;margin-bottom:12px;"></textarea>
    <div class="m-confirm-actions" style="flex-direction:row;">
      <button class="m-confirm-btn feedback" onclick="closeResumeModal()" style="flex:1;">取消</button>
      <button class="m-confirm-btn approve" id="resumeModalOk" style="flex:1;">恢复</button>
    </div>
  </div>
</div>

<!-- Terminate Choice Modal -->
<div class="m-confirm-backdrop" id="terminateChoiceModal">
  <div class="m-confirm-box" style="text-align:center;">
    <div class="m-confirm-title" style="margin-bottom:8px;">终止任务</div>
    <div style="color:var(--text-secondary);font-size:0.82rem;margin-bottom:16px;">此任务有代码变更，请选择处理方式：</div>
    <div class="m-confirm-actions" style="flex-direction:column;gap:10px;">
      <button class="m-confirm-btn approve" id="terminateRollbackBtn" style="width:100%;">🔄 回滚代码（推荐）</button>
      <button class="m-confirm-btn feedback" id="terminateKeepBtn" style="width:100%;">📌 保留代码</button>
      <button class="m-confirm-btn" onclick="closeTerminateChoiceModal()" style="width:100%;background:var(--bg-primary);color:var(--text-secondary);">取消</button>
    </div>
  </div>
</div>

<!-- Rollback Step Sheet -->
<div class="m-confirm-backdrop" id="rollbackSheet">
  <div class="m-confirm-box" style="max-height:70vh;overflow-y:auto;">
    <div class="m-confirm-title">回退到指定步骤</div>
    <div id="rollbackStepList" style="margin:12px 0;"></div>
    <textarea id="rollbackFeedback" placeholder="可选：输入修改指令（告诉 AI 重做时注意什么）" style="width:100%;padding:8px;background:var(--bg-primary);border:1px solid var(--border);border-radius:8px;color:var(--text-primary);font-size:0.82rem;resize:none;height:60px;outline:none;margin-bottom:12px;"></textarea>
    <div class="m-confirm-actions" style="flex-direction:row;">
      <button class="m-confirm-btn feedback" onclick="closeRollbackSheet()" style="flex:1;">取消</button>
      <button class="m-confirm-btn approve" onclick="doRollback()" style="flex:1;">确认回退</button>
    </div>
  </div>
</div>

<!-- Toast -->
<div class="m-toast" id="toast"></div>

<!-- Copy Overlay -->
<div class="m-copy-overlay" id="copyOverlay">
  <div class="m-copy-toolbar">
    <span>长按选择文字</span>
    <button class="m-btn-selectcopy" onclick="selectAndCopy()">全选复制</button>
    <button class="m-btn-close" onclick="closeCopyOverlay()">关闭</button>
  </div>
  <div class="m-copy-body"><pre id="copyContent"></pre></div>
</div>

<script>
/* ============================================================
   Global State
   ============================================================ */
var ws = null;
var term = null;
var fitAddon = null;
var currentSessionId = localStorage.getItem('opus_m_session') || '';
var currentProject = (function() {
  var p = localStorage.getItem('opus_m_project') || '';
  // Fix stale full-path values like "/opt/vizo-next" → "vizo-next"
  if (p.indexOf('/') !== -1) { p = p.split('/').filter(Boolean).pop() || p; localStorage.setItem('opus_m_project', p); }
  return p;
})();
var reconnectDelay = 1000;
var MAX_RECONNECT_DELAY = 30000;
var reconnectTimer = null;
var pingTimer = null;
var pongTimer = null;
var pollTimer = null;
var hasActiveTask = false;
var currentTaskData = null;
var pendingConfirmTaskId = null;
var livePolling = null;
var liveTaskId = null;
var elapsedTimers = {};

/* Step / Role display name maps (from PC console) */
var STEP_DISPLAY_MAP = {
  requirement_analysis: '需求分析', pm_prd: '产品PRD', architect: '架构设计',
  design_confirmed: '设计确认', qa_test_cases: '编写测试用例',
  backend_dev: '后端开发', frontend_dev: '前端开发', integration: '集成联调',
  fix: '问题修复', refactor: '重构实现', qa_engineer: '编写测试',
  test_round_1: '第1轮测试', test_round_2: '第2轮测试', test_round_3: '第3轮测试',
  fix_round_1: '第1轮修复', fix_round_2: '第2轮修复', fix_round_3: '第3轮修复',
  deploy: '部署上线', knowledge: '知识沉淀', embedded: '嵌入式开发', assistant: '助手执行'
};
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
  merge_resolver: '合并处理', developer_backend: '后端工程师',
  developer_frontend: '前端工程师', devops: '运维工程师'
};
function stepDisplayName(step) {
  return STEP_DISPLAY_MAP[step.name] || ROLE_DISPLAY_MAP[step.role] || step.name || step.role || '';
}

/* ============================================================
   API Helper
   ============================================================ */
async function apiCall(url, options) {
  options = options || {};
  try {
    var resp = await fetch(url, options);
    if (resp.status === 401) {
      showToast('请求未授权');
      return null;
    }
    if (!resp.ok) {
      var err = await resp.json().catch(function() { return {error:'请求失败'}; });
      showToast(err.error || '请求失败 (' + resp.status + ')');
      return null;
    }
    return await resp.json();
  } catch(e) {
    if (e && e.name === 'AbortError') throw e;
    showToast('网络连接失败');
    return null;
  }
}

/* ============================================================
   Toast
   ============================================================ */
var toastTimer = null;
function showToast(msg) {
  var el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(function() { el.classList.remove('visible'); }, 2500);
}

/* ============================================================
   Tab Switching
   ============================================================ */
function switchTab(btn) {
  document.querySelectorAll('.m-tab').forEach(function(t) { t.classList.remove('active'); });
  document.querySelectorAll('.m-panel').forEach(function(p) { p.classList.remove('active'); });
  btn.classList.add('active');
  var panel = document.getElementById(btn.dataset.panel);
  if (panel) panel.classList.add('active');
  if (btn.dataset.panel === 'panelChat' && fitAddon) {
    setTimeout(function() {
      var requested = requestPtyResize(false);
      if (!requested && _ptyCols) {
        scaleFontToFit();
      } else if (!requested) {
        fitAddon.fit();
      }
    }, 50);
  }
  if (btn.dataset.panel === 'panelTask') {
    pollTasks();
  }
  if (btn.dataset.panel === 'panelMe') {
    updateMeTab();
  }
}

/* ============================================================
   Terminal (xterm.js)
   ============================================================ */
function initTerminal() {
  if (typeof Terminal === 'undefined') {
    document.getElementById('terminalContainer').innerHTML =
      '<div style="padding:2rem;text-align:center;color:var(--text-muted);">终端加载失败，请检查网络连接后刷新页面</div>';
    return;
  }
  term = new Terminal({
    fontSize: 12,
    fontFamily: 'Menlo, Monaco, "Courier New", monospace',
    theme: { background: '#0a0e17', foreground: '#e2e8f0', cursor: '#38bdf8' },
    cursorBlink: true,
    allowProposedApi: true,
    scrollback: 500,
    fastScrollSensitivity: 5,
    smoothScrollDuration: 0,
  });
  fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  if (typeof WebLinksAddon !== 'undefined') {
    term.loadAddon(new WebLinksAddon.WebLinksAddon());
  }
  term.open(document.getElementById('terminalContainer'));
  try {
    if (typeof CanvasAddon !== 'undefined') {
      term.loadAddon(new CanvasAddon.CanvasAddon());
    }
  } catch(e) {}
  fitAddon.fit();

  // Ctrl+C: copy selection if text is selected, otherwise send SIGINT
  term.attachCustomKeyEventHandler(function(ev) {
    if (ev.type === 'keydown' && ev.key === 'c' && (ev.ctrlKey || ev.metaKey)) {
      var sel = term.getSelection();
      if (sel) {
        navigator.clipboard.writeText(sel).then(function() {
          showToast('已复制');
          term.clearSelection();
        });
        return false;  // prevent xterm from processing
      }
    }
    return true;
  });

  // Long-press on terminal opens copy overlay (mobile)
  (function() {
    var container = document.getElementById('terminalContainer');
    var lpTimer = null;
    container.addEventListener('touchstart', function(e) {
      lpTimer = setTimeout(function() {
        lpTimer = null;
        openCopyOverlay();
      }, 600);
    }, {passive: true});
    container.addEventListener('touchend', function() { clearTimeout(lpTimer); });
    container.addEventListener('touchmove', function() { clearTimeout(lpTimer); });
  })();

  // Disable xterm native keyboard on mobile — use custom input row instead
  var _xt = document.querySelector('#terminalContainer .xterm-helper-textarea');
  if (_xt) {
    _xt.setAttribute('inputmode', 'none');
    _xt.setAttribute('readonly', 'readonly');
  }

  // Send input to PTY via WebSocket
  term.onData(function(data) {
    if (data === '\x1b') {
      // Physical keyboard ESC: already sent to PTY (can't prevent), show overlay
      var now = Date.now();
      if (now - _lastEscSendTime < 200) return; // 200ms debounce
      _lastEscSendTime = now;
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({type:'input', data:data}));
      }
      _escSentToPty = true;
      _inShortcutsMode = true;
      showEscOverlay();
      return;
    }
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({type:'input', data:data}));
    }
  });

  // Scroll-to-bottom FAB: track xterm viewport scroll
  term.onScroll(function() {
    updateTermFab();
  });
  // Mobile touch scroll: xterm onScroll may not fire reliably on touch,
  // so also listen on the viewport element directly
  var vp = term.element && term.element.querySelector('.xterm-viewport');
  if (vp) {
    vp.addEventListener('scroll', updateTermFab, {passive: true});
  }

  // Load sessions and auto-connect
  loadSessionsAndConnect();
}

/* ============================================================
   Copy Overlay — extract terminal buffer as selectable text
   ============================================================ */
function getTerminalText() {
  if (!term) return '';
  var buf = term.buffer.active;
  var lines = [];
  for (var i = 0; i < buf.length; i++) {
    var line = buf.getLine(i);
    if (line) lines.push(line.translateToString(true));
  }
  // Trim trailing empty lines
  while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
  return lines.join('\n');
}

/* Selection guard — prevent mobile browser from selecting all content
   when user scrolls (drags scrollbar thumb).
   Strategy: lock selection changes for 150ms after any scroll event;
   restore the pre-scroll selection if browser mutates it during the lock. */
var _copySelLen = 0;
var _copyPrevRange = null;
var _copySelLocked = false;
var _copySelLockTimer = null;
var _copyRestoringSelection = false;

(function() {
  var copyBody = document.querySelector('.m-copy-body');
  if (!copyBody) return;

  copyBody.addEventListener('scroll', function() {
    // Snapshot selection before the browser can corrupt it
    if (!_copyRestoringSelection) {
      var sel = window.getSelection();
      if (sel.rangeCount > 0) {
        _copyPrevRange = sel.getRangeAt(0).cloneRange();
        _copySelLen = sel.toString().length;
      }
    }
    // Lock selection changes while scrolling + 150ms after it stops
    _copySelLocked = true;
    clearTimeout(_copySelLockTimer);
    _copySelLockTimer = setTimeout(function() { _copySelLocked = false; }, 150);
  }, {passive: true});

  document.addEventListener('selectionchange', function() {
    if (_copyRestoringSelection) return;
    var overlay = document.getElementById('copyOverlay');
    if (!overlay || !overlay.classList.contains('visible')) return;

    var sel = window.getSelection();

    // During scroll lock: if selection changed, restore pre-scroll snapshot
    if (_copySelLocked && _copyPrevRange) {
      _copyRestoringSelection = true;
      sel.removeAllRanges();
      sel.addRange(_copyPrevRange.cloneRange());
      _copyRestoringSelection = false;
      return;
    }

    // Outside lock: update snapshot
    var len = sel.toString().length;
    if (sel.rangeCount > 0 && len > 0) {
      _copyPrevRange = sel.getRangeAt(0).cloneRange();
    }
    _copySelLen = len;
  });
})();

function openCopyOverlay() {
  var text = getTerminalText();
  if (!text.trim()) { showToast('终端无内容'); return; }
  document.getElementById('copyContent').textContent = text;
  var overlay = document.getElementById('copyOverlay');
  overlay.classList.add('visible');
  // Prevent background scroll while overlay is open
  document.body.style.overflow = 'hidden';
  // Reset selection-guard state
  _copySelLen = 0;
  _copyLastScrollT = 0;
  _copyPrevRange = null;
}

function closeCopyOverlay() {
  document.getElementById('copyOverlay').classList.remove('visible');
  document.body.style.overflow = '';
  window.getSelection().removeAllRanges();
}

function copyAllTermText() {
  var text = document.getElementById('copyContent').textContent;
  navigator.clipboard.writeText(text).then(function() {
    showToast('已复制全部内容');
    closeCopyOverlay();
  }).catch(function() {
    selectAllTermText();
    showToast('已全选，请手动复制');
  });
}

function selectAllTermText() {
  var range = document.createRange();
  range.selectNodeContents(document.getElementById('copyContent'));
  var sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
  showToast('已全选');
}

function selectAndCopy() {
  selectAllTermText();
  copyAllTermText();
}

/* ============================================================
   Terminal scroll-to-bottom FAB
   ============================================================ */
var _termFabRAF = 0;

function updateTermFab() {
  if (_termFabRAF) return;
  _termFabRAF = requestAnimationFrame(function() {
    _termFabRAF = 0;
    var fab = document.getElementById('termScrollFab');
    if (!fab || !term) return;
    var buf = term.buffer.active;
    var atBottom = buf.viewportY >= buf.baseY;
    fab.style.display = atBottom ? 'none' : 'block';
  });
}

function termScrollToBottom() {
  if (term) term.scrollToBottom();
  var fab = document.getElementById('termScrollFab');
  if (fab) fab.style.display = 'none';
}

/* ============================================================
   PTY size matching — adjust fontSize so PTY cols fit screen width
   ============================================================ */
var _ptyCols = 0;
var _ptyRows = 0;
var _baseFontSize = 12;
var _lastResizeReqCols = 0;
var _lastResizeReqRows = 0;

function applyPtySize(cols, rows) {
  _ptyCols = cols;
  _ptyRows = rows;
  if (!term) return;
  scaleFontToFit();
}

function getMeasuredCellSize() {
  try {
    var dims = term && term._core && term._core._renderService &&
      term._core._renderService.dimensions && term._core._renderService.dimensions.css;
    var cell = dims && dims.cell;
    if (cell && cell.width > 0 && cell.height > 0) {
      return {width: cell.width, height: cell.height};
    }
  } catch(e) {}
  return null;
}

function getDesiredMobilePtySize() {
  if (!term) return null;
  var container = document.getElementById('terminalContainer');
  if (!container) return null;
  var cell = getMeasuredCellSize();
  if (!cell) return null;
  var currentFontSize = term.options.fontSize || _baseFontSize;
  if (!(currentFontSize > 0)) return null;
  var scale = _baseFontSize / currentFontSize;
  var baseCellWidth = cell.width * scale;
  var baseCellHeight = cell.height * scale;
  if (!(baseCellWidth > 0) || !(baseCellHeight > 0)) return null;
  var cols = Math.max(2, Math.floor((container.clientWidth - 2) / baseCellWidth));
  var rows = Math.max(1, Math.floor((container.clientHeight - 2) / baseCellHeight));
  if (!isFinite(cols) || !isFinite(rows)) return null;
  return {cols: cols, rows: rows};
}

function requestPtyResize(force) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return false;
  var dims = getDesiredMobilePtySize();
  if (!dims) return false;
  if (!force && dims.cols === _lastResizeReqCols && dims.rows === _lastResizeReqRows) {
    return false;
  }
  _lastResizeReqCols = dims.cols;
  _lastResizeReqRows = dims.rows;
  try {
    ws.send(JSON.stringify({type:'resize', cols:dims.cols, rows:dims.rows}));
    return true;
  } catch(e) {}
  return false;
}

function scaleFontToFit() {
  if (!term || !_ptyCols) return;
  var container = document.getElementById('terminalContainer');
  var containerWidth = container.clientWidth;
  if (containerWidth <= 0) return;

  // 使用 xterm 实际测量的字符宽度来反推 fontSize，避免横竖屏切换时
  // 因硬编码字符比例而把字体缩得过小。
  var targetFontSize;
  var cell = getMeasuredCellSize();
  if (cell && cell.width > 0) {
    var currentFontSize = term.options.fontSize || _baseFontSize;
    targetFontSize = currentFontSize * (containerWidth - 2) / (_ptyCols * cell.width);
  } else {
    var charRatio = 0.602;
    targetFontSize = (containerWidth - 2) / (_ptyCols * charRatio);
  }
  // 限制范围：最小 6px 避免不可读，最大不超过原始值
  targetFontSize = Math.max(6, Math.min(_baseFontSize, Math.floor(targetFontSize * 10) / 10));

  if (Math.abs(term.options.fontSize - targetFontSize) > 0.3) {
    term.options.fontSize = targetFontSize;
    // 用 fitAddon 计算新 fontSize 下能显示的 rows（但保持 PTY 的 cols）
    if (fitAddon) fitAddon.fit();
    // fitAddon.fit() 可能改了 cols，强制恢复到 PTY cols
    if (term.cols !== _ptyCols) {
      var fitRows = term.rows;  // 保留 fit 计算的 rows
      term.resize(_ptyCols, fitRows);
    }
  }
}

/* ============================================================
   Write batching — accumulate output, flush once per frame
   ============================================================ */
var _writeBuf = '';
var _writeRAF = 0;
function batchWrite(data) {
  _writeBuf += data;
  if (!_writeRAF) {
    _writeRAF = requestAnimationFrame(function() {
      if (term && _writeBuf) {
        // 写入前用 xterm buffer API 记录用户滚动偏移（比 DOM scrollTop 更可靠）
        var buf = term.buffer.active;
        var linesFromBottom = buf.baseY - buf.viewportY;  // 0 = 在底部
        var chunk = _writeBuf;
        _writeBuf = '';
        _writeRAF = 0;
        // 使用 term.write(data, callback)：回调在 xterm.js 渲染完成后触发
        term.write(chunk, function() {
          if (linesFromBottom > 0) {
            // 用户之前不在底部，恢复到相同的「距底部行数」位置
            var newBuf = term.buffer.active;
            var target = newBuf.baseY - linesFromBottom;
            if (target < 0) target = 0;
            term.scrollToLine(target);
          }
          // 在底部时 xterm.js 自动跟随，无需干预
          updateTermFab();
        });
        return;  // 已在 write callback 中清理，提前返回
      }
      _writeBuf = '';
      _writeRAF = 0;
      updateTermFab();
    });
  }
}

/* ============================================================
   WebSocket
   ============================================================ */
function connectWS(sessionId) {
  if (ws) {
    ws.onclose = null;
    ws.close();
  }
  clearTimeout(reconnectTimer);
  clearInterval(pingTimer);
  if (pongTimer) { clearTimeout(pongTimer); pongTimer = null; }

  var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(proto + '//' + location.host + '/vizo/console/ws?session_id=' + sessionId + '&client=mobile');

  ws.onopen = function() {
    reconnectDelay = 1000;
    updateConnectionStatus(true);
    // 移动端在连接后主动上报当前屏幕可承载的 PTY 尺寸；
    // 服务端会在无 PC 客户端时真正缩放 PTY，有 PC 时仍保持 PC 优先。
    setTimeout(function() { requestPtyResize(true); }, 60);
    setTimeout(function() { requestPtyResize(true); }, 260);
    pingTimer = setInterval(function() {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({type:'ping'}));
        // Expect pong within 90s
        if (pongTimer) clearTimeout(pongTimer);
        pongTimer = setTimeout(function() {
          if (ws) ws.close();
        }, 90000);
      }
    }, 25000);
  };

  ws.onmessage = function(e) {
    try {
      var msg = JSON.parse(e.data);
      if (msg.type === 'output' || msg.type === 'replay') {
        if (term) batchWrite(msg.data);
      } else if (msg.type === 'pty_size') {
        if (term && msg.cols && msg.rows) {
          applyPtySize(msg.cols, msg.rows);
        }
      } else if (msg.type === 'pong') {
        if (pongTimer) { clearTimeout(pongTimer); pongTimer = null; }
      } else if (msg.type === 'takeover') {
        showToast('会话已被其他窗口接管');
        clearTimeout(reconnectTimer);
        currentSessionId = null;
      }
    } catch(err) {}
  };

  ws.onclose = function() {
    updateConnectionStatus(false);
    clearInterval(pingTimer);
    if (pongTimer) { clearTimeout(pongTimer); pongTimer = null; }
    scheduleReconnect();
  };

  ws.onerror = function() {
    updateConnectionStatus(false);
  };
}

function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  if (!currentSessionId) return;
  reconnectTimer = setTimeout(function() {
    reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY);
    connectWS(currentSessionId);
  }, reconnectDelay);
}

function updateConnectionStatus(connected) {
  var el = document.getElementById('headerStatus');
  el.className = 'm-header-status' + (connected ? ' connected' : '');
  document.getElementById('headerStatusText').textContent = connected ? '已连接' : '';
}

// Visibility change reconnect
document.addEventListener('visibilitychange', function() {
  if (document.visibilityState === 'visible') {
    if (currentSessionId && (!ws || ws.readyState !== WebSocket.OPEN)) {
      reconnectDelay = 1000;
      connectWS(currentSessionId);
    }
    // Refresh task data
    if (document.querySelector('.m-tab[data-panel="panelTask"]').classList.contains('active')) {
      pollTasks();
    }
    // Resume live panel polling if open
    if (liveTaskId && document.getElementById('liveOverlay').classList.contains('open')) {
      clearInterval(livePolling);
      loadLivePanels(liveTaskId);
      livePolling = setInterval(function() { loadLivePanels(liveTaskId); }, 5000);
    }
  }
});
window.addEventListener('pageshow', function(e) {
  if (e.persisted && currentSessionId) connectWS(currentSessionId);
});

function sendInput(data) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({type:'input', data:data}));
  } else {
    showToast('终端未连接');
  }
}

/* Send command text then Enter — text and \r must arrive as SEPARATE
   WebSocket messages (Claude Code's input handler requires this).
   After ESC: sends 'a' + Ctrl+U prefix to exit shortcuts menu.
   Normal mode: sends Ctrl+A + Ctrl+K to clear any existing/suggested text,
   then the actual command. */
function sendAndExecute(text) {
  var wasInShortcutsMode = _inShortcutsMode;
  // Clear ESC overlay state
  _inShortcutsMode = false;
  _escSentToPty = false;
  clearTimeout(_escOverlayTimer);
  _escOverlayTimer = null;
  var overlayEl = document.getElementById('escOverlay');
  if (overlayEl) overlayEl.classList.remove('visible');

  if (wasInShortcutsMode) {
    // Exit shortcuts mode: 'a' triggers mode-switch (consumed), Ctrl+U clears buffer
    sendInput('a');
    setTimeout(function() {
      sendInput('\x15'); // Ctrl+U
      setTimeout(function() {
        sendInput(text);
        setTimeout(function() {
          sendInput('\r');
          if (term) term.scrollToBottom();
        }, 50);
      }, 50);
    }, 100);
  } else {
    // Normal mode: Ctrl+C discards current line (including ghost/autocomplete text),
    // giving us a clean empty prompt. Then type and submit.
    sendInput('\x03'); // Ctrl+C — discard line + ghost text
    setTimeout(function() {
      sendInput(text);
      setTimeout(function() {
        sendInput('\r');
        if (term) term.scrollToBottom();
      }, 50);
    }, 150);
  }
}

/* Mobile custom input — bypass xterm textarea to avoid IME/diff bugs */
function doSend() {
  var el = document.getElementById('mInput');
  var text = el.value;
  if (!text) { sendInput('\r'); return; }
  el.value = '';
  el.style.height = 'auto';
  sendAndExecute(text);
}
// Auto-resize textarea
(function(){
  var el = document.getElementById('mInput');
  if (!el) return;
  el.addEventListener('input', function(){
    this.style.height = 'auto';
    this.style.height = Math.min(this.scrollHeight, 120) + 'px';
  });
  // Enter = send (Shift+Enter = newline)
  el.addEventListener('keydown', function(e){
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      doSend();
    }
  });
})();

/* ============================================================
   Virtual Keyboard Handling
   ============================================================ */
var resizeTimer = null;
function handleViewportResize() {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(function() {
    var requested = requestPtyResize(false);
    if (!requested && _ptyCols) {
      scaleFontToFit();
    } else if (!requested && fitAddon) {
      fitAddon.fit();
    }
    if (term) term.scrollToBottom();
    updateTermFab();
  }, 160);
}
if (window.visualViewport) {
  window.visualViewport.addEventListener('resize', handleViewportResize);
}
if ('virtualKeyboard' in navigator) {
  navigator.virtualKeyboard.overlaysContent = true;
  navigator.virtualKeyboard.addEventListener('geometrychange', handleViewportResize);
}
window.addEventListener('resize', handleViewportResize);

/* ============================================================
   Shortcuts & Workflows
   ============================================================ */
var _lastEscSendTime = 0;   // ESC debounce — 200ms prevents accidental double-tap
var _inShortcutsMode = false; // true after ESC actually sent to PTY (keyboard ESC)
var _escOverlayTimer = null;  // auto-dismiss timer for ESC interrupt overlay
var _escSentToPty = false;    // true if ESC was already sent (keyboard), false if pending (button)

function showEscOverlay() {
  var el = document.getElementById('escOverlay');
  if (el) el.classList.add('visible');
  clearTimeout(_escOverlayTimer);
  _escOverlayTimer = setTimeout(function() {
    hideEscOverlay(true); // auto-dismiss after 5s
  }, 5000);
}

function hideEscOverlay(sendCancel) {
  clearTimeout(_escOverlayTimer);
  _escOverlayTimer = null;
  var el = document.getElementById('escOverlay');
  if (el) el.classList.remove('visible');
  // No ESC was sent to PTY (button mode), so cancel just hides the overlay.
  // For keyboard ESC (_escSentToPty=true), send ESC to exit shortcuts menu.
  if (sendCancel && _escSentToPty) sendInput('\x1b');
  _inShortcutsMode = false;
  _escSentToPty = false;
}

function confirmEscInterrupt() {
  clearTimeout(_escOverlayTimer);
  _escOverlayTimer = null;
  var el = document.getElementById('escOverlay');
  if (el) el.classList.remove('visible');
  _escSentToPty = false;
  // Keep _inShortcutsMode = true: after interrupt, Claude Code returns to a
  // prompt that behaves like shortcuts mode (plain text input is ignored).
  // The next sendAndExecute will use 'a' prefix to enter text-input mode.
  _inShortcutsMode = true;
  // ESC (trigger interrupt prompt) + Ctrl+C (force interrupt) — covers all states
  sendInput('\x1b');
  setTimeout(function() { sendInput('\x03'); }, 200);
}

var SHORTCUTS = [
  {label:'ESC', data:'\x1b'},
  {label:'Tab', data:'\t'},
  {label:'\u25b2', data:'\x1b[A'},
  {label:'\u25bc', data:'\x1b[B'},
  {sep:true},
  {label:'^C', data:'\x03', style:'color:var(--red)'},
  {label:'^L', data:'\x0c'},
  {sep:true},
  {label:'/compact', data:'/compact\r'},
  {label:'/clear', data:'/clear\r'},
  {label:'/cost', data:'/cost\r'},
  {label:'/status', data:'/status\r'},
  {label:'/memory', data:'/memory\r'},
  {label:'/vim', data:'/vim\r'},
  {sep:true},
  {label:'Opus', data:'/model opus\r', style:'color:var(--purple)'},
  {label:'Sonnet', data:'/model sonnet\r', style:'color:var(--accent)'},
  {label:'Haiku', data:'/model haiku\r', style:'color:var(--green)'},
  {label:'/model', data:'/model\r'},
  {sep:true},
  {label:'Yes', data:'y\r'},
  {label:'No', data:'n\r'},
];

var WORKFLOWS = [
  {label:'\ud83e\udd16 AI自动分流', color:'#238636', cmd:'opus "'},
  {label:'\u2728 新功能', color:'#1f6feb', cmd:'opus --workflow new_feature "'},
  {label:'\ud83d\udc1b Bug修复', color:'#da3633', cmd:'opus --workflow bug_fix "'},
  {label:'\u267b\ufe0f 优化重构', color:'#d29922', cmd:'opus --workflow refactor "'},
  {label:'\ud83d\udd27 嵌入式', color:'#484f58', cmd:'opus --workflow embedded "'},
  {label:'\ud83d\udcdd 非开发', color:'#484f58', cmd:'opus --workflow non_dev "'},
];

function buildShortcuts() {
  var bar = document.getElementById('shortcutBar');
  SHORTCUTS.forEach(function(s) {
    if (s.sep) {
      var sep = document.createElement('span');
      sep.className = 'm-sk-sep';
      bar.appendChild(sep);
      return;
    }
    var btn = document.createElement('button');
    btn.className = 'm-shortcut-btn';
    btn.textContent = s.label;
    if (s.style) btn.setAttribute('style', s.style);
    btn.onclick = function() {
      // ESC button: only show overlay for confirmation, do NOT send ESC yet
      if (s.data === '\x1b') {
        var now = Date.now();
        if (now - _lastEscSendTime < 200) return; // 200ms debounce
        _lastEscSendTime = now;
        _escSentToPty = false;
        _inShortcutsMode = false;
        showEscOverlay();
        return;
      }
      // Ctrl+C: direct interrupt — clear ESC state, hide overlay
      if (s.data === '\x03') {
        hideEscOverlay(false);
        sendInput(s.data);
        return;
      }
      // Command shortcuts (ending with \r): send text first, then Enter
      if (s.data.length > 1 && s.data.charAt(s.data.length - 1) === '\r') {
        var cmd = s.data.slice(0, -1);  // strip trailing \r
        sendAndExecute(cmd);
        return;
      }
      // Raw key shortcuts (Tab, arrows, etc.): send directly, hide overlay
      hideEscOverlay(false);
      sendInput(s.data);
    };
    bar.appendChild(btn);
  });
}

function buildWorkflows() {
  var bar = document.getElementById('workflowBar');
  WORKFLOWS.forEach(function(w) {
    var btn = document.createElement('button');
    btn.className = 'm-wf-btn';
    btn.textContent = w.label;
    btn.style.background = w.color;
    btn.onclick = function() {
      var el = document.getElementById('mInput');
      el.value = w.cmd;
      el.focus();
      // Trigger auto-resize
      el.style.height = 'auto';
      el.style.height = Math.min(el.scrollHeight, 120) + 'px';
      // Place cursor at end so user can type description
      el.setSelectionRange(el.value.length, el.value.length);
      // Switch to chat tab
      switchTab(document.querySelector('.m-tab[data-panel="panelChat"]'));
    };
    bar.appendChild(btn);
  });
}

/* ============================================================
   Session Management
   ============================================================ */
async function loadSessionsAndConnect() {
  var data = await apiCall('/vizo/console/api/sessions');
  if (!data) return;
  var sessions = data.sessions || [];

  if (currentSessionId) {
    var found = sessions.find(function(s) { return s.id === currentSessionId; });
    if (found) {
      updateHeaderForSession(found);
      connectWS(currentSessionId);
      return;
    }
  }

  // Find first alive session or any session
  var alive = sessions.find(function(s) { return s.status !== 'stopped'; });
  if (alive) {
    setCurrentSession(alive);
    connectWS(alive.id);
  } else if (sessions.length > 0) {
    setCurrentSession(sessions[0]);
    connectWS(sessions[0].id);
  } else {
    document.getElementById('headerTitle').textContent = '(无会话)';
  }
}

function extractProjectName(session) {
  // API expects project name, not full project path
  var cwd = session.cwd || '';
  if (cwd) return cwd.split('/').filter(Boolean).pop() || cwd;
  return '';
}

function setCurrentSession(session) {
  currentSessionId = session.id;
  currentProject = extractProjectName(session);
  localStorage.setItem('opus_m_session', currentSessionId);
  localStorage.setItem('opus_m_project', currentProject);
  updateHeaderForSession(session);
}

function updateHeaderForSession(session) {
  var title = session.name || session.id || 'Vizo';
  document.getElementById('headerTitle').textContent = title;
}

function toggleDrawer(show) {
  document.getElementById('drawer').classList.toggle('open', show);
  document.getElementById('drawerBackdrop').classList.toggle('visible', show);
  if (show) loadDrawerSessions();
}

async function loadDrawerSessions() {
  var data = await apiCall('/vizo/console/api/sessions');
  if (!data) return;
  var list = document.getElementById('sessionList');
  var sessions = data.sessions || [];
  list.innerHTML = '';
  if (sessions.length === 0) {
    list.innerHTML = '<div style="padding:2rem;text-align:center;color:var(--text-muted);font-size:0.85rem;">暂无会话</div>';
    return;
  }
  sessions.forEach(function(s) {
    var item = document.createElement('div');
    item.className = 'm-session-item' + (s.id === currentSessionId ? ' active' : '');
    item.innerHTML = '<div class="m-session-dot ' + (s.status !== 'stopped' ? 'alive' : 'dead') + '"></div>' +
      '<div class="m-session-name">' + escHtml(s.name || s.id) + '</div>' +
      '<button class="m-session-del" data-sid="' + escHtml(s.id) + '">&times;</button>';
    item.querySelector('.m-session-name').onclick = function() {
      setCurrentSession(s);
      if (term) term.clear();
      connectWS(s.id);
      toggleDrawer(false);
    };
    item.querySelector('.m-session-del').onclick = function(e) {
      e.stopPropagation();
      deleteSession(s.id);
    };
    list.appendChild(item);
  });
}

async function deleteSession(sid) {
  if (!confirm('确认删除此会话？此操作不可恢复。')) return;
  await apiCall('/vizo/console/api/sessions/' + sid, {method:'DELETE'});
  if (sid === currentSessionId) {
    currentSessionId = '';
    localStorage.removeItem('opus_m_session');
    if (ws) { ws.onclose = null; ws.close(); ws = null; }
    if (term) term.clear();
  }
  loadDrawerSessions();
}

function openNewSessionSheet() {
  toggleDrawer(false);
  loadProjectsForSheet();
  document.getElementById('sheetBackdrop').classList.add('visible');
  document.getElementById('newSessionSheet').classList.add('open');
}

function closeSheet() {
  document.getElementById('sheetBackdrop').classList.remove('visible');
  document.getElementById('newSessionSheet').classList.remove('open');
}

async function loadProjectsForSheet() {
  var data = await apiCall('/vizo/console/api/projects');
  if (!data) return;
  var sel = document.getElementById('newSessionProject');
  sel.innerHTML = '';
  (data.projects || []).forEach(function(p) {
    var opt = document.createElement('option');
    opt.value = p.name;
    opt.textContent = p.name;
    opt.setAttribute('data-path', p.path || '');
    if (p.name === currentProject) opt.selected = true;
    sel.appendChild(opt);
  });
}

async function createSession() {
  var name = document.getElementById('newSessionName').value.trim();
  var project = document.getElementById('newSessionProject').value;
  if (!project) { showToast('请选择项目'); return; }
  // Find project path from cached list
  var projPath = '';
  var projSel = document.getElementById('newSessionProject');
  var selOpt = projSel.options[projSel.selectedIndex];
  if (selOpt) projPath = selOpt.getAttribute('data-path') || '';
  if (!name) name = project;
  var cols = 80, rows = 24;
  var mobileDims = getDesiredMobilePtySize();
  if (mobileDims) {
    cols = mobileDims.cols || cols;
    rows = mobileDims.rows || rows;
  } else if (fitAddon) {
    try { var dims = fitAddon.proposeDimensions(); if (dims) { cols = dims.cols || 80; rows = dims.rows || 24; } } catch(e) {}
  }
  var data = await apiCall('/vizo/console/api/sessions', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name: name, cwd: projPath, cols: cols, rows: rows})
  });
  if (data && data.session && data.session.id) {
    currentSessionId = data.session.id;
    currentProject = project;
    localStorage.setItem('opus_m_session', currentSessionId);
    localStorage.setItem('opus_m_project', currentProject);
    document.getElementById('headerTitle').textContent = name || data.session.id;
    if (term) term.clear();
    connectWS(data.session.id);
    closeSheet();
    showToast('会话已创建');
  }
}

/* ============================================================
   Task Polling
   ============================================================ */
async function pollTasks() {
  clearTimeout(pollTimer);
  if (!currentProject) {
    // Try to get project from sessions
    var sessData = await apiCall('/vizo/console/api/sessions');
    if (sessData && sessData.sessions && sessData.sessions.length > 0) {
      var cwd = sessData.sessions[0].cwd || '';
      currentProject = cwd ? cwd.split('/').filter(Boolean).pop() || cwd : '';
    }
  }

  try {
    // Use project-specific API if available, otherwise global
    var tasksResp;
    if (currentProject) {
      tasksResp = await apiCall('/vizo/console/api/projects/' + encodeURIComponent(currentProject) + '/tasks');
    } else {
      tasksResp = await apiCall('/vizo/console/api/tasks');
    }
    var currentResp = currentProject
      ? await apiCall('/vizo/console/api/opus/current?project=' + encodeURIComponent(currentProject))
      : await apiCall('/vizo/console/api/opus/current');

    // Merge current task step data into task list for enrichment
    var tasks = tasksResp ? (tasksResp.tasks || []) : [];
    if (currentResp && currentResp.task) {
      var ct = currentResp.task;
      for (var i = 0; i < tasks.length; i++) {
        if (tasks[i].id === ct.task_id) {
          tasks[i]._steps = ct.steps || [];
          tasks[i]._completed_steps = ct.completed_steps || [];
          tasks[i]._total_steps = ct.total_steps || 0;
          tasks[i]._current_step = ct.current_step || '';
          break;
        }
      }
      updateNotifBar(currentResp);
      checkPendingConfirm(currentResp);
    }

    renderTaskList(tasks);

    hasActiveTask = tasks.some(function(t) {
      return ['running','waiting_confirm','in_progress','paused'].includes(t.status);
    });
  } catch(e) {}

  var interval = hasActiveTask ? 3000 : 10000;
  pollTimer = setTimeout(pollTasks, interval);
}

function renderTaskList(tasks) {
  var list = document.getElementById('taskList');
  if (!tasks || tasks.length === 0) {
    list.innerHTML = '<div class="m-task-empty">暂无任务</div>';
    return;
  }

  var active = tasks.filter(function(t) {
    return ['running','waiting_confirm','in_progress','paused'].includes(t.status);
  });
  var done = tasks.filter(function(t) {
    return ['completed','rolled_back','failed','terminated'].includes(t.status);
  });

  var html = '';
  if (active.length > 0) {
    html += '<div class="m-task-group-label">进行中</div>';
    active.forEach(function(t) { html += renderTaskCard(t); });
  }
  if (done.length > 0) {
    html += '<div class="m-task-group-label">历史任务</div>';
    done.forEach(function(t) { html += renderTaskCard(t); });
  }
  list.innerHTML = html;

  // Bind click events
  list.querySelectorAll('.m-task-card').forEach(function(card) {
    card.onclick = function() { openTaskDetail(card.dataset.taskId); };
  });
}

function getTaskTitle(t) {
  if (t.task_title && t.task_title.trim()) {
    return t.task_title.trim().replace(/^#+\\s*/, '').substring(0, 20);
  }
  if (t.task_name && t.task_name.trim()) {
    var name = t.task_name.trim().split('\\n')[0];
    return name.replace(/^#+\\s*/, '').substring(0, 20);
  }
  if (t.description && t.description.trim()) {
    var line = t.description.trim().split('\\n')[0];
    return line.replace(/^#+\\s*/, '').substring(0, 20);
  }
  return t.id || '';
}

function renderTaskCard(t) {
  var statusClass = t.status || 'pending';
  var statusLabels = {
    running: '执行中', in_progress: '执行中', waiting_confirm: '待确认',
    paused: '已暂停', completed: '已完成', rolled_back: '已回滚', failed: '已失败', terminated: '已终止', partially_failed: '部分失败'
  };
  var isActive = ['running','waiting_confirm','in_progress','paused'].includes(t.status);

  // Progress from merged data
  var steps = t._steps || [];
  var completedCount = t._completed_steps ? t._completed_steps.length : 0;
  var totalCount = steps.length || t._total_steps || 0;
  var pct = totalCount > 0 ? Math.round(completedCount / totalCount * 100) : (t.status === 'completed' ? 100 : 0);

  // Meta with emoji icons
  var timeStr = t.created_at ? new Date(t.created_at).toLocaleString('zh-CN', {hour12:false, year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'}) : '';
  var costStr = t.cost_usd ? ('$' + Number(t.cost_usd).toFixed(2)) : '';
  var typeStr = t.task_type || '';

  var isHub = (t.id && t.id.startsWith('hub-')) || t.task_type === 'hub';
  var html = '<div class="m-task-card' + (isHub ? ' hub' : '') + '" data-task-id="' + escHtml(t.id || '') + '">';

  // Header: dot + name + tag
  html += '<div class="m-task-card-header">';
  if (isHub) html += '<span class="ma-hub-badge">&#129302;</span>';
  html += '<div class="m-task-status-dot ' + statusClass + '"></div>';
  html += '<div class="m-task-card-name">' + escHtml(getTaskTitle(t)) + '</div>';
  html += '<span class="tc-tag ' + statusClass + '">' + (statusLabels[t.status] || t.status || '') + '</span>';
  html += '</div>';

  // Meta: emoji icons
  html += '<div class="m-task-card-meta">';
  if (timeStr) html += '<span>&#128337; ' + timeStr + '</span>';
  if (isHub && t.module_name) html += '<span class="ma-hub-module">[' + escHtml(t.module_name) + ']</span>';
  if (typeStr && !isHub) html += '<span>&#129302; ' + escHtml(typeStr) + '</span>';
  if (costStr) html += '<span>&#128176; ' + costStr + '</span>';
  html += '</div>';

  // Footer: live / replay button
  if (isActive || t.status === 'completed' || t.status === 'failed' || t.status === 'rolled_back') {
    html += '<div class="tc-footer">';
    if (isActive) {
      html += '<button class="tc-live-btn live" onclick="event.stopPropagation();openLive(\'' + escHtml(t.id || '') + '\')"><span class="tc-live-dot"></span> 直播中</button>';
    } else {
      html += '<button class="tc-live-btn replay" onclick="event.stopPropagation();openLive(\'' + escHtml(t.id || '') + '\')">▶ 回放</button>';
    }
    html += '</div>';
  }

  html += '</div>';
  return html;
}

/* ============================================================
   Notification Bar
   ============================================================ */
function updateNotifBar(currentData) {
  var bar = document.getElementById('notifBar');
  var text = document.getElementById('notifText');
  if (!currentData || !currentData.has_task) {
    bar.style.display = 'none';
    bar.className = 'm-notif';
    return;
  }
  var task = currentData.task || {};
  if (task.pending_confirm && task.status === 'waiting_confirm') {
    bar.className = 'm-notif warning';
    text.textContent = '待确认: ' + (task.pending_confirm.title || task.pending_confirm.summary || '请处理');
    bar.style.display = 'flex';
  } else if (['running','waiting_confirm','in_progress'].includes(task.status)) {
    bar.className = 'm-notif info';
    text.textContent = '运行中: ' + getTaskTitle({task_title: task.task_title, task_name: task.task_name, description: task.description, id: task.task_id});
    bar.style.display = 'flex';
  } else {
    bar.style.display = 'none';
    bar.className = 'm-notif';
  }
}

function handleNotifClick() {
  if (pendingConfirmTaskId) {
    openTaskDetail(pendingConfirmTaskId);
  } else if (currentTaskData && currentTaskData.task) {
    openTaskDetail(currentTaskData.task.task_id);
  }
}

/* ============================================================
   Confirm Handling
   ============================================================ */
function checkPendingConfirm(currentData) {
  if (!currentData || !currentData.has_task) {
    pendingConfirmTaskId = null;
    return;
  }
  var task = currentData.task || {};
  currentTaskData = currentData;
  if (task.pending_confirm && task.status === 'waiting_confirm') {
    pendingConfirmTaskId = task.task_id;
    // 不弹弹窗，通知栏点击直接进任务详情
  } else {
    pendingConfirmTaskId = null;
  }
}

function showDetailFeedback() {
  var area = document.getElementById('detailFeedbackArea');
  if (area) {
    area.style.display = area.style.display === 'none' ? 'block' : 'none';
    if (area.style.display === 'block') {
      var ta = document.getElementById('feedbackText');
      if (ta) ta.focus();
    }
  }
}

async function doConfirm(action) {
  if (!pendingConfirmTaskId) return;
  // Map to backend-expected values: y, n, f, d, terminate
  var actionMap = {approve:'y', reject:'n', feedback:'f', discussion:'d', interaction_design:'i'};
  var mappedAction = actionMap[action] || action;
  var body = {action: mappedAction};
  if (mappedAction === 'f') {
    var msg = document.getElementById('feedbackText').value.trim();
    if (!msg) { showToast('请输入意见'); return; }
    body.feedback = msg;
  }
  await apiCall('/vizo/api/tasks/' + pendingConfirmTaskId + '/confirm', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  var confirmedTaskId = pendingConfirmTaskId;
  pendingConfirmTaskId = null;
  var toastMsg = mappedAction === 'y' ? '已确认' : mappedAction === 'n' ? '已取消' : mappedAction === 'f' ? '意见已提交' : mappedAction === 'd' ? '已发起讨论' : mappedAction === 'i' ? '已发起交互设计' : '已处理';
  showToast(toastMsg);
  // 等待状态变化后刷新详情页和列表（与暂停/终止一致）
  var newStatus = await waitForStatusChange(confirmedTaskId, 'waiting_confirm', 10000);
  if (newStatus) {
    showToast(newStatus === 'running' ? '任务已继续' : toastMsg);
  }
  openTaskDetail(confirmedTaskId);
  pollTasks();
}

/* ============================================================
   Task Detail Overlay
   ============================================================ */
function opusCurrentUrl(taskId) {
  var url = '/vizo/console/api/opus/current?task_id=' + encodeURIComponent(taskId);
  if (currentProject) url += '&project=' + encodeURIComponent(currentProject);
  return url;
}

async function openTaskDetail(taskId) {
  if (!taskId) return;
  var overlay = document.getElementById('taskDetailOverlay');
  overlay.classList.add('open');
  document.getElementById('detailTitle').textContent = '加载中...';
  document.getElementById('detailBody').innerHTML = '';
  document.getElementById('detailActions').innerHTML = '';

  var data = await apiCall(opusCurrentUrl(taskId));
  if (!data || !data.task) {
    document.getElementById('detailTitle').textContent = '任务详情';
    document.getElementById('detailBody').innerHTML = '<div class="m-task-empty">无法加载任务信息</div>';
    return;
  }

  var task = data.task;
  var title = getTaskTitle({task_name: task.task_name, description: task.description, id: taskId});
  document.getElementById('detailTitle').textContent = title;

  var statusMap = {
    running: '运行中', in_progress: '运行中', waiting_confirm: '等待确认',
    paused: '已暂停', completed: '已完成', rolled_back: '已回滚', failed: '失败', terminated: '已终止'
  };
  var statusColor = {
    running: 'var(--green)', in_progress: 'var(--green)', waiting_confirm: 'var(--yellow)',
    paused: 'var(--yellow)', completed: 'var(--green)', rolled_back: 'var(--red)', failed: 'var(--red)', terminated: 'var(--text-secondary)'
  };
  var isRunning = ['running','in_progress','waiting_confirm'].includes(task.status);
  var isPaused = task.status === 'paused';
  var isFailed = task.status === 'failed';

  var steps = (task.steps || []).filter(function(s) {
    // 过滤崩溃残留：error 且无耗时、无产出
    if (s.status === 'error' && !s.duration && !s.output_doc) return false;
    return true;
  });
  var elapsed = formatElapsed(task.started_at, (isRunning || isPaused) ? null : task.updated_at);

  // Section: Basic Info (d-grid 2-col)
  var html = '<div class="d-section">';
  html += '<div class="d-section-title">基本信息</div>';
  html += '<div class="d-grid">';
  html += '<div class="d-item"><div class="d-label">任务 ID</div><div class="d-value" style="font-size:0.72rem;font-family:monospace">' + escHtml(taskId) + '</div></div>';
  html += '<div class="d-item"><div class="d-label">状态</div><div class="d-value" style="color:' + (statusColor[task.status] || 'inherit') + '">' + (statusMap[task.status] || task.status) + '</div></div>';
  if (elapsed) html += '<div class="d-item"><div class="d-label">总耗时</div><div class="d-value" id="detailElapsed">' + elapsed + '</div></div>';
  html += '<div class="d-item"><div class="d-label">总费用</div><div class="d-value">' + (task.cost_usd !== undefined ? '$' + Number(task.cost_usd).toFixed(2) : '-') + '</div></div>';
  if (task.task_type) html += '<div class="d-item"><div class="d-label">工作流</div><div class="d-value">' + escHtml(task.task_type) + '</div></div>';
  html += '</div></div>';

  // Section: Rollback Recovery Info (only for rolled_back status)
  if (task.status === 'rolled_back' && task.rollback_info) {
    html += '<div class="d-section">';
    html += '<div class="d-section-title" style="color:var(--yellow);">恢复代码</div>';
    html += '<div style="padding:8px 12px;background:var(--bg-primary);border-radius:8px;font-size:0.78rem;">';
    html += '<div style="color:var(--text-secondary);margin-bottom:6px;">代码已回滚，如需恢复：</div>';
    html += '<div style="font-family:monospace;color:var(--accent);word-break:break-all;margin-bottom:4px;">git cherry-pick opus-snapshot/' + escHtml(taskId) + '</div>';
    html += '<div style="color:var(--text-secondary);margin-top:6px;">单文件恢复：</div>';
    html += '<div style="font-family:monospace;color:var(--accent);word-break:break-all;">git checkout opus-snapshot/' + escHtml(taskId) + ' -- 文件路径</div>';
    html += '</div></div>';
  }

  // Section: Workflow Steps (with colored left borders)
  if (steps.length > 0) {
    // 位置感知：子任务区域内的普通步骤归入子任务
    var parentOnlySteps = ['deploy', 'knowledge', 'knowledge_review', 'knowledge_extractor'];
    var lastSub = null;
    var subStarted = false;
    var subAgg = {};   // subRole → { cost, dur, latest, latestOk, children[] }
    var subOrder = [];
    var preSteps = [];  // 子任务前的父步骤
    var postSteps = []; // 子任务后的父步骤

    steps.forEach(function(step) {
      var role = step.role || '';
      var name = step.name || role || '';
      var isSub = role.indexOf('sub-task:') === 0;

      if (isSub) {
        lastSub = role;
        subStarted = true;
        if (!subAgg[role]) {
          subAgg[role] = { cost: 0, dur: 0, latest: null, latestOk: null };
          subOrder.push(role);
        }
        var sa = subAgg[role];
        sa.cost += (step.cost_usd || 0);
        sa.dur += (step.duration || 0);
        sa.latest = step;
        if (step.status === 'completed' || step.status === 'running') sa.latestOk = step;
        return;
      }

      if (lastSub && parentOnlySteps.indexOf(name) < 0) {
        // 归入当前子任务
        var sa2 = subAgg[lastSub];
        if (sa2) { sa2.cost += (step.cost_usd || 0); sa2.dur += (step.duration || 0); }
        return;
      }

      if (parentOnlySteps.indexOf(name) >= 0) lastSub = null;
      // 聚合同名父步骤
      var target = subStarted ? postSteps : preSteps;
      var existing = target.find(function(e) { return e._aggKey === name; });
      if (!existing) {
        step._aggKey = name;
        step._aggCost = step.cost_usd || 0;
        step._aggDur = step.duration || 0;
        target.push(step);
      } else {
        existing._aggCost += (step.cost_usd || 0);
        existing._aggDur += (step.duration || 0);
        if (step.status === 'completed' || step.status === 'running') {
          existing.status = step.status;
          existing.duration = existing._aggDur;
          existing.cost_usd = existing._aggCost;
          if (step.output_doc) existing.output_doc = step.output_doc;
          if (step.preview_url) existing.preview_url = step.preview_url;
          if (step.model) existing.model = step.model;
        }
      }
    });

    html += '<div class="d-section">';
    html += '<div class="d-section-title">工作流步骤</div>';

    function renderStep(step) {
      var iconMap = {completed:'✓', running:'▶', pending:'○', failed:'✗', error:'✗', skipped:'⊘'};
      var colorMap = {completed:'var(--green)', running:'var(--accent)', failed:'var(--red)', error:'var(--red)', pending:'var(--text-muted)'};
      var stepStatus = step.status || 'pending';
      var cls = stepStatus === 'completed' ? 'done' : stepStatus === 'running' ? 'current' : (stepStatus === 'failed' || stepStatus === 'error') ? 'failed' : 'pending';
      var icon = iconMap[stepStatus] || '○';
      var iconColor = colorMap[stepStatus] || 'var(--text-muted)';
      var cost = step._aggCost || step.cost_usd || 0;
      var dur = step._aggDur || step.duration || 0;

      var h = '<div class="wf-detail-step ' + cls + '">';
      h += '<span class="wf-icon" style="color:' + iconColor + '">' + icon + '</span>';
      h += '<div class="wf-info">';
      h += '<div class="wf-name"' + (stepStatus === 'running' ? ' style="color:var(--accent)"' : '') + '>' + escHtml(stepDisplayName(step)) + '</div>';
      h += '<div class="wf-meta">';
      if (dur) h += '<span>&#128337; ' + formatDuration(dur) + '</span>';
      if (step.model) h += '<span>&#129302; ' + escHtml(step.model) + '</span>';
      if (cost) h += '<span>&#128176; $' + Number(cost).toFixed(2) + '</span>';
      h += '</div>';
      if (step.output_doc) {
        h += '<div class="wf-doc">';
        if (step.preview_url) {
          h += '<a class="wf-doc-link" href="' + escHtml(step.preview_url) + '" target="_blank">&#128196; ' + escHtml(step.output_doc) + '</a>';
        } else {
          h += '<span class="wf-doc-link" onclick="previewFile(\'' + escHtml(currentProject) + '\',\'.vizo/tasks/' + escHtml(taskId) + '/' + escHtml(step.output_doc) + '\')">&#128196; ' + escHtml(step.output_doc) + '</span>';
        }
        h += '</div>';
      }
      h += '</div></div>';
      return h;
    }

    // 前置步骤
    preSteps.forEach(function(step) { html += renderStep(step); });

    // 子任务区域
    if (subOrder.length > 0) {
      var subDone = subOrder.filter(function(r) { var a = subAgg[r]; return a.latestOk && a.latestOk.status === 'completed'; }).length;
      var subTasks = task.sub_tasks || [];
      var totalSubs = subTasks.length > 0 ? subTasks.length : subOrder.length;
      html += '<div class="d-section-title" style="margin-top:8px;font-size:0.72rem;color:var(--accent)">▼ 子任务 ' + subDone + '/' + totalSubs + '</div>';

      subOrder.forEach(function(role) {
        var sa = subAgg[role];
        var best = sa.latestOk || sa.latest;
        var subName = role.substring('sub-task:'.length);
        html += renderStep({name: subName, role: role, status: best.status, cost_usd: sa.cost, _aggCost: sa.cost, duration: sa.dur, _aggDur: sa.dur, model: best.model || '', output_doc: best.output_doc || '', preview_url: best.preview_url || ''});
      });

      // pending 子任务
      if (subTasks.length > 0) {
        var existingSubNames = {};
        subOrder.forEach(function(r) { existingSubNames[r.substring('sub-task:'.length)] = true; });
        subTasks.forEach(function(st) {
          if (!existingSubNames[st.name]) {
            html += renderStep({name: st.name, role: 'sub-task:' + st.name, status: 'pending'});
          }
        });
      }
    }

    // 后置步骤
    if (postSteps.length > 0 && subOrder.length > 0) {
      html += '<div class="d-section-title" style="margin-top:8px;font-size:0.72rem;color:var(--text-secondary)">▲ 后续步骤</div>';
    }
    postSteps.forEach(function(step) { html += renderStep(step); });

    html += '</div>';
  }

  // Section: Task Files
  var taskDir = '.vizo/tasks/' + taskId;
  html += '<div class="d-section">';
  html += '<div class="d-section-title">任务文件 <span style="font-size:0.65rem;color:var(--text-muted);font-weight:400;text-transform:none;letter-spacing:0">' + escHtml(taskDir) + '/</span></div>';
  html += '<div class="fb-list" id="taskFileList"><div style="padding:12px;color:var(--text-muted);font-size:0.75rem;">加载中...</div></div>';
  html += '</div>';

  document.getElementById('detailBody').innerHTML = html;

  // Start dynamic elapsed timer for active tasks
  stopElapsedTimer('detail');
  if ((isRunning || isPaused) && task.started_at) {
    startElapsedTimer('detail', task.started_at, 'detailElapsed');
  }

  // Load task files asynchronously
  loadTaskFiles(currentProject, taskDir);

  // Action buttons (bottom bar)
  var actHtml = '';

  // Check for pending confirm — only when actually waiting (not paused/other stale states)
  if (task.pending_confirm && task.status === 'waiting_confirm') {
    var pc = task.pending_confirm;
    var buttonSet = (pc.context && pc.context.button_set) || 'generic_confirm';
    var BUTTON_SETS = {
      doc_review: [
        {label:'✓ 确认继续', action:'y', cls:'approve'},
        {label:'✎ 补充意见', action:'f', cls:'feedback'},
        {label:'✗ 取消', action:'n', cls:'reject', confirmMsg:'确认取消此任务阶段？'}
      ],
      doc_review_discussion: [
        {label:'✓ 确认继续', action:'y', cls:'approve'},
        {label:'✎ 补充意见', action:'f', cls:'feedback'},
        {label:'💬 发起讨论', action:'d', cls:'feedback'},
        {label:'✗ 取消', action:'n', cls:'reject', confirmMsg:'确认取消此任务阶段？'}
      ],
      agent_exception: [
        {label:'🔄 重试', action:'y', cls:'resume'},
        {label:'⏸ 暂停', action:'n', cls:'pause'},
        {label:'🛑 终止', action:'terminate', cls:'terminate'}
      ],
      deploy_confirm: [
        {label:'✓ 确认部署', action:'y', cls:'approve'},
        {label:'✗ 取消', action:'n', cls:'reject'}
      ],
      generic_confirm: [
        {label:'✓ 确认', action:'y', cls:'approve'},
        {label:'✎ 补充意见', action:'f', cls:'feedback'},
        {label:'✗ 取消', action:'n', cls:'reject'}
      ],
      doc_review_ra_design: [
        {label:'✓ 确认继续', action:'y', cls:'approve'},
        {label:'✎ 补充意见', action:'f', cls:'feedback'},
        {label:'💬 发起讨论', action:'d', cls:'feedback'},
        {label:'🎨 交互设计', action:'i', cls:'feedback'},
        {label:'✗ 取消', action:'n', cls:'reject', confirmMsg:'确认取消此任务阶段？'}
      ],
      doc_review_pm_design: [
        {label:'✓ 确认继续', action:'y', cls:'approve'},
        {label:'✎ 补充意见', action:'f', cls:'feedback'},
        {label:'🎨 交互设计', action:'i', cls:'feedback'},
        {label:'✗ 取消', action:'n', cls:'reject', confirmMsg:'确认取消此任务阶段？'}
      ]
    };
    var buttons = BUTTON_SETS[buttonSet] || BUTTON_SETS.generic_confirm;

    // Live button — always available for viewing logs
    actHtml += '<button class="m-detail-btn live" onclick="openLive(\'' + escHtml(taskId) + '\')">&#128308; 直播</button>';

    // Show doc link if available
    if (pc.preview_url) {
      actHtml += '<a href="' + escHtml(pc.preview_url) + '" target="_blank" class="m-detail-btn live" style="text-decoration:none;text-align:center;">&#128196; 查看文档</a>';
    }

    buttons.forEach(function(btn) {
      if (btn.action === 'f') {
        actHtml += '<button class="m-detail-btn ' + btn.cls + '" onclick="showDetailFeedback()">' + btn.label + '</button>';
      } else if (btn.confirmMsg) {
        actHtml += '<button class="m-detail-btn ' + btn.cls + '" onclick="showActionConfirmModal(\'' + escHtml(btn.confirmMsg) + '\', function(){ doConfirm(\'' + btn.action + '\'); })">' + btn.label + '</button>';
      } else {
        actHtml += '<button class="m-detail-btn ' + btn.cls + '" onclick="doConfirm(\'' + btn.action + '\')">' + btn.label + '</button>';
      }
    });

    // Feedback input area (hidden by default)
    actHtml += '<div id="detailFeedbackArea" style="display:none;width:100%;"><div style="display:flex;gap:8px;width:100%;"><textarea id="feedbackText" placeholder="请输入修改意见..." style="flex:1;padding:8px;background:var(--bg-primary);border:1px solid var(--border);border-radius:8px;color:var(--text-primary);font-size:0.82rem;resize:none;height:40px;outline:none;"></textarea><button class="m-detail-btn resume" style="flex:none;width:60px;" onclick="doConfirm(\'f\')">提交</button></div></div>';
  } else if (isRunning) {
    actHtml += '<button class="m-detail-btn live" onclick="openLive(\'' + escHtml(taskId) + '\')">&#128308; 直播</button>';
    actHtml += '<button class="m-detail-btn pause" onclick="showActionConfirmModal(\'确认暂停此任务？\', function(){ doPauseTask(\'' + escHtml(taskId) + '\'); })">暂停</button>';
    actHtml += '<button class="m-detail-btn terminate" onclick="showTerminateChoiceModal(\''+escHtml(taskId)+'\', '+(task.has_code_changes ? 'true' : 'false')+')">终止</button>';
  } else if (isPaused) {
    actHtml += '<button class="m-detail-btn live" onclick="openLive(\'' + escHtml(taskId) + '\')">&#128308; 直播</button>';
    var completedSteps = (steps || []).filter(function(s) { return s.status === 'completed'; });
    if (completedSteps.length > 0) {
      actHtml += '<button class="m-detail-btn" onclick="showRollbackSheet(\'' + escHtml(taskId) + '\',' + escHtml(JSON.stringify(steps)) + ')">&#8617; 回退步骤</button>';
    }
    actHtml += '<button class="m-detail-btn resume" onclick="showResumeModal(\'' + escHtml(taskId) + '\', \'恢复执行\')">恢复</button>';
    actHtml += '<button class="m-detail-btn terminate" onclick="showTerminateChoiceModal(\''+escHtml(taskId)+'\', '+(task.has_code_changes ? 'true' : 'false')+')">终止</button>';
  } else if (isFailed) {
    actHtml += '<button class="m-detail-btn live" onclick="openLive(\'' + escHtml(taskId) + '\')">&#9654; 查看日志</button>';
    actHtml += '<button class="m-detail-btn resume" onclick="showResumeModal(\'' + escHtml(taskId) + '\', \'重试任务\')">重试</button>';
    actHtml += '<button class="m-detail-btn terminate" onclick="showTerminateChoiceModal(\''+escHtml(taskId)+'\', '+(task.has_code_changes ? 'true' : 'false')+')">终止</button>';
  } else {
    actHtml += '<button class="m-detail-btn live" onclick="openLive(\'' + escHtml(taskId) + '\')">&#9654; 查看日志</button>';
  }
  document.getElementById('detailActions').innerHTML = actHtml;
}

var _actionConfirmCallback = null;

function showActionConfirmModal(msg, callback) {
  document.getElementById('actionConfirmMsg').textContent = msg;
  _actionConfirmCallback = callback;
  document.getElementById('actionConfirmOk').onclick = function() {
    var cb = _actionConfirmCallback;
    closeActionConfirmModal();
    if (cb) cb();
  };
  document.getElementById('actionConfirmModal').classList.add('visible');
}

function closeActionConfirmModal() {
  document.getElementById('actionConfirmModal').classList.remove('visible');
  _actionConfirmCallback = null;
}

var _resumeModalTaskId = null;
function showResumeModal(taskId, title) {
  _resumeModalTaskId = taskId;
  document.getElementById('resumeModalTitle').textContent = title;
  document.getElementById('resumeFeedbackInput').value = '';
  document.getElementById('resumeModalOk').onclick = function() {
    var feedback = document.getElementById('resumeFeedbackInput').value.trim();
    closeResumeModal();
    doResumeTask(taskId, feedback);
  };
  document.getElementById('resumeModal').classList.add('visible');
  // 自动聚焦输入框
  setTimeout(function() { document.getElementById('resumeFeedbackInput').focus(); }, 100);
}

function closeResumeModal() {
  document.getElementById('resumeModal').classList.remove('visible');
  _resumeModalTaskId = null;
}

var _terminateTaskId = null;

function showTerminateChoiceModal(taskId, hasCodeChanges) {
  _terminateTaskId = taskId;
  if (!hasCodeChanges) {
    // 无代码变更，直接简单确认
    showActionConfirmModal('确认终止此任务？', function(){ doTerminateTask(taskId, false); });
    return;
  }
  // 有代码变更，弹出选择框
  document.getElementById('terminateRollbackBtn').onclick = function() {
    closeTerminateChoiceModal();
    doTerminateTask(taskId, true);
  };
  document.getElementById('terminateKeepBtn').onclick = function() {
    closeTerminateChoiceModal();
    doTerminateTask(taskId, false);
  };
  document.getElementById('terminateChoiceModal').classList.add('visible');
}

function closeTerminateChoiceModal() {
  document.getElementById('terminateChoiceModal').classList.remove('visible');
  _terminateTaskId = null;
}

var _rollbackTaskId = null;
var STEP_NAMES = {
  requirement_analysis: '需求分析', pm_prd: '产品 PRD', interaction_design: '交互设计',
  architect: '架构设计', backend_dev: '后端开发', frontend_dev: '前端开发',
  integration: '集成联调', test_round_1: '测试', deployment: '部署'
};

function showRollbackSheet(taskId, steps) {
  _rollbackTaskId = taskId;
  var completed = (steps || []).filter(function(s) { return s.status === 'completed'; });
  var listEl = document.getElementById('rollbackStepList');
  listEl.innerHTML = completed.map(function(s, i) {
    var name = STEP_NAMES[s.name] || s.name;
    return '<label style="display:flex;align-items:center;gap:8px;padding:10px 0;border-bottom:1px solid var(--border);font-size:0.85rem;color:var(--text-primary);">' +
      '<input type="radio" name="rollback-target-m" value="' + escHtml(s.name) + '"' + (i === completed.length - 1 ? ' checked' : '') +
      ' style="accent-color:var(--accent);">' +
      '<span>' + escHtml(name) + '</span>' +
      (s.output_doc ? '<span style="color:var(--text-muted);font-size:0.72rem;margin-left:auto;">' + escHtml(s.output_doc) + '</span>' : '') +
      '</label>';
  }).join('');
  document.getElementById('rollbackFeedback').value = '';
  document.getElementById('rollbackSheet').classList.add('visible');
}

function closeRollbackSheet() {
  document.getElementById('rollbackSheet').classList.remove('visible');
  _rollbackTaskId = null;
}

async function doRollback() {
  if (!_rollbackTaskId) return;
  var selected = document.querySelector('input[name="rollback-target-m"]:checked');
  if (!selected) { showToast('请选择回退步骤'); return; }
  var feedback = document.getElementById('rollbackFeedback').value.trim();
  var body = {target_step: selected.value};
  if (feedback) body.feedback = feedback;
  try {
    var resp = await apiCall('/vizo/api/tasks/' + _rollbackTaskId + '/rollback', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    if (resp && resp.status === 'ok') {
      showToast(resp.message || '已回退', 'success');
      closeRollbackSheet();
      var newStatus = await waitForStatusChange(_rollbackTaskId, 'running', 10000);
      openTaskDetail(_rollbackTaskId);
      pollTasks();
    } else {
      showToast((resp && resp.message) || '回退失败', 'error');
    }
  } catch(e) {
    showToast('网络错误', 'error');
  }
}

async function waitForStatusChange(taskId, oldStatus, maxWait) {
  var start = Date.now();
  while (Date.now() - start < (maxWait || 10000)) {
    await new Promise(function(r) { setTimeout(r, 1500); });
    var data = await apiCall(opusCurrentUrl(taskId));
    if (data && data.task && data.task.status !== oldStatus) {
      return data.task.status;
    }
  }
  return null;
}

async function doPauseTask(taskId) {
  var resp = await apiCall('/vizo/api/tasks/' + taskId + '/pause', {method:'POST'});
  if (!resp) return;
  showToast('暂停中...');
  var newStatus = await waitForStatusChange(taskId, 'running', 10000);
  if (newStatus) {
    showToast('已暂停');
  } else {
    showToast('操作可能需要更长时间，请稍后刷新');
  }
  openTaskDetail(taskId);
  pollTasks();
}

async function doTerminateTask(taskId, rollback) {
  var body = rollback !== undefined ? JSON.stringify({rollback: rollback}) : '{}';
  var resp = await apiCall('/vizo/api/tasks/' + taskId + '/terminate', {method:'POST', headers:{'Content-Type':'application/json'}, body: body});
  if (!resp) return;
  showToast('终止中...');
  var newStatus = await waitForStatusChange(taskId, 'running', 10000);
  if (newStatus) {
    showToast('已终止');
  } else {
    showToast('操作可能需要更长时间，请稍后刷新');
  }
  openTaskDetail(taskId);
  pollTasks();
}

async function doResumeTask(taskId, feedback) {
  var opts = {method:'POST'};
  if (feedback) {
    opts.headers = {'Content-Type': 'application/json'};
    opts.body = JSON.stringify({feedback: feedback});
  }
  var resp = await apiCall('/vizo/api/tasks/' + taskId + '/resume', opts);
  if (!resp) return;
  showToast('恢复中...');
  var newStatus = await waitForStatusChange(taskId, 'paused', 10000) || await waitForStatusChange(taskId, 'failed', 5000);
  if (newStatus) {
    showToast('已恢复执行');
  } else {
    showToast('操作可能需要更长时间，请稍后刷新');
  }
  openTaskDetail(taskId);
  pollTasks();
}

async function doRetryTask(taskId, feedback) {
  var opts = {method:'POST'};
  if (feedback) {
    opts.headers = {'Content-Type': 'application/json'};
    opts.body = JSON.stringify({feedback: feedback});
  }
  var resp = await apiCall('/vizo/api/tasks/' + taskId + '/resume', opts);
  if (!resp) return;
  showToast('重试中...');
  var newStatus = await waitForStatusChange(taskId, 'failed', 15000);
  if (newStatus) {
    showToast('已开始重试');
  } else {
    showToast('操作可能需要更长时间，请稍后刷新');
  }
  openTaskDetail(taskId);
  pollTasks();
}

async function doTerminateFailedTask(taskId) {
  var resp = await apiCall('/vizo/api/tasks/' + taskId + '/terminate', {method:'POST'});
  if (!resp) return;
  showToast('终止中...');
  var newStatus = await waitForStatusChange(taskId, 'failed', 15000);
  if (newStatus) {
    showToast('已终止');
  } else {
    showToast('操作可能需要更长时间，请稍后刷新');
  }
  openTaskDetail(taskId);
  pollTasks();
}

/* ============================================================
   Live Overlay
   ============================================================ */
async function openLive(taskId) {
  closeOverlay('taskDetailOverlay');
  liveTaskId = taskId;
  var overlay = document.getElementById('liveOverlay');
  overlay.classList.add('open');
  document.getElementById('liveTitle').innerHTML = '&#128308; 直播';
  document.getElementById('liveBody').innerHTML = '<div class="m-task-empty">加载中...</div>';

  // Stop previous polling
  clearInterval(livePolling);

  await loadLivePanels(taskId);

  // Auto-refresh for active tasks
  var taskIsActive = ['running','waiting_confirm','in_progress','paused'].includes(
    (currentTaskData && currentTaskData.task && currentTaskData.task.status) || '');
  if (taskIsActive) {
    livePolling = setInterval(function() { loadLivePanels(taskId); }, 5000);
  }
}

async function loadLivePanels(taskId) {
  var data = await apiCall(opusCurrentUrl(taskId));
  if (!data || !data.task) {
    document.getElementById('liveBody').innerHTML = '<div class="m-task-empty">无法加载</div>';
    return;
  }

  var task = data.task;
  var rawSteps = task.steps || [];
  
  // 步骤去重：合并同名步骤，记录重试次数和总费用
  var stepMap = {};
  var stepOrder = [];
  rawSteps.forEach(function(step) {
    var key = (step.name || '') + '|' + (step.role || '');
    if (!stepMap[key]) {
      stepMap[key] = {
        step: Object.assign({}, step),
        retryCount: 0,
        totalCost: step.cost_usd || 0,
        totalDuration: step.duration || 0,
        lastStatus: step.status
      };
      stepOrder.push(key);
    } else {
      var agg = stepMap[key];
      agg.retryCount++;
      agg.totalCost += (step.cost_usd || 0);
      agg.totalDuration += (step.duration || 0);
      // 保留最新状态（running > error > completed）
      if (step.status === 'running' || (step.status === 'error' && agg.lastStatus !== 'running')) {
        agg.lastStatus = step.status;
        agg.step = Object.assign({}, step);
      }
    }
  });
  // 转换为步骤数组，附加聚合信息
  var steps = stepOrder.map(function(key) {
    var agg = stepMap[key];
    var s = agg.step;
    s._aggCost = agg.totalCost;
    s._aggDur = agg.totalDuration;
    s._retryCount = agg.retryCount;
    s._lastStatus = agg.lastStatus;
    return s;
  });

  // 预取所有子任务详情（按名称索引）
  var subTaskDetails = {};
  var subTasks = task.sub_tasks || [];
  for (var si = 0; si < subTasks.length; si++) {
    var subName = subTasks[si].name || subTasks[si].id || '';
    if (!subName) continue;
    var subData = await apiCall('/vizo/console/api/opus/subtask?task_id=' + taskId + '&sub_name=' + encodeURIComponent(subName) + '&project=' + encodeURIComponent(currentProject));
    if (subData && subData.found && subData.sub_task && subData.sub_task.steps) {
      var rawSubSteps = subData.sub_task.steps.filter(function(s) { return s.billable !== false; });
      var subId = subData.sub_task.sub_id || subTasks[si].id || '';
      rawSubSteps.forEach(function(ss) { ss._sub_name = subName; ss._sub_id = subId; });
      
      // 子任务步骤去重：同名同角色的步骤只保留最后一个
      var subStepMap = {};
      var subStepOrder = [];
      rawSubSteps.forEach(function(ss) {
        var key = (ss.name || '') + '|' + (ss.role || '');
        if (!subStepMap[key]) {
          subStepMap[key] = {
            step: Object.assign({}, ss),
            retryCount: 0,
            totalCost: ss.cost_usd || 0,
            totalDuration: ss.duration || 0,
            lastStatus: ss.status
          };
          subStepOrder.push(key);
        } else {
          var agg = subStepMap[key];
          agg.retryCount++;
          agg.totalCost += (ss.cost_usd || 0);
          agg.totalDuration += (ss.duration || 0);
          // 保留最新状态（running > error > completed）
          if (ss.status === 'running' || (ss.status === 'error' && agg.lastStatus !== 'running')) {
            agg.lastStatus = ss.status;
            agg.step = Object.assign({}, ss);
          }
        }
      });
      
      // 转换为步骤数组，附加聚合信息
      var subSteps = subStepOrder.map(function(key) {
        var agg = subStepMap[key];
        var s = agg.step;
        s._aggCost = agg.totalCost;
        s._aggDur = agg.totalDuration;
        s._retryCount = agg.retryCount;
        s._lastStatus = agg.lastStatus;
        return s;
      });
      
      subTaskDetails[subName] = subSteps;
    }
  }

  // 按 progress.json 原始顺序构建步骤列表：
  // - 子任务占位步骤 → 替换为子任务内部步骤（保留位置）
  // - 夹在子任务占位步骤之间的普通步骤（如 final_integration_test）→ 归入前一个子任务分组
  // - 子任务区域开始前的普通步骤 → 保留为父任务步骤
  var insertedSubs = {};
  var hasSubTasks = subTasks.length > 0;
  var lastSubContext = null;
  var lastSubId = null;
  // 收集归入子任务的父步骤，稍后合并排序
  var subExtraSteps = {}; // subName → [steps]
  // 先对 rawSteps 进行去重过滤
  var dedupedRawSteps = [];
  var seenKeys = {};
  rawSteps.forEach(function(s) {
    if (s.billable === false) return;
    // 过滤崩溃残留：error 且无耗时、无产出的步骤
    if (s.status === 'error' && !s.duration && !s.output_doc) return;
    // 去重：同名同角色的步骤只保留最后一个
    var key = (s.name || '') + '|' + (s.role || '');
    if (seenKeys[key] !== undefined) {
      // 更新已有步骤的位置，保留最新状态
      dedupedRawSteps[seenKeys[key]] = s;
    } else {
      seenKeys[key] = dedupedRawSteps.length;
      dedupedRawSteps.push(s);
    }
  });
  var finalSteps = [];
  dedupedRawSteps.forEach(function(s) {
    // 子任务占位步骤 → 替换为子任务内部步骤
    if (s.role && s.role.indexOf('sub-task:') === 0) {
      var sName = s.role.substring('sub-task:'.length);
      lastSubContext = sName;
      // 查找对应的 sub_id
      for (var si2 = 0; si2 < subTasks.length; si2++) {
        if ((subTasks[si2].name || subTasks[si2].id || '') === sName) {
          lastSubId = subTaskDetails[sName] && subTaskDetails[sName].length > 0 ? subTaskDetails[sName][0]._sub_id : (subTasks[si2].id || '');
          break;
        }
      }
      if (!insertedSubs[sName] && subTaskDetails[sName]) {
        insertedSubs[sName] = true;
        // 不立即 concat，先收集，稍后和归入的步骤合并排序
        if (!subExtraSteps[sName]) subExtraSteps[sName] = [];
        // 用占位标记在 steps 中标出子任务位置
        steps.push({_placeholder: true, _sub_name: sName, _sub_id: lastSubId});
      }
      return;
    }
    // 在子任务区域内的普通步骤 → 归入当前子任务分组
    // 但部署/知识沉淀等 pipeline 尾部步骤一定属于父任务
    var parentOnlySteps = ['deploy', 'knowledge', 'knowledge_review', 'knowledge_extractor'];
    if (hasSubTasks && lastSubContext) {
      if (parentOnlySteps.indexOf(s.name) >= 0) {
        lastSubContext = null;
        lastSubId = null;
      } else {
        s._sub_name = lastSubContext;
        s._sub_id = lastSubId || '';
        if (!subExtraSteps[lastSubContext]) subExtraSteps[lastSubContext] = [];
        subExtraSteps[lastSubContext].push(s);
        return; // 不直接 push 到 steps，稍后展开
      }
    }
    steps.push(s);
  });

  // 展开子任务占位：将子任务内部步骤 + 归入的父步骤合并，按 started_at 排序
  var finalSteps = [];
  var processedSubSteps = {}; // 跟踪已处理的子任务步骤，避免重复
  steps.forEach(function(s) {
    if (s._placeholder) {
      var sName = s._sub_name;
      // 避免重复处理同一个子任务
      if (processedSubSteps[sName]) return;
      processedSubSteps[sName] = true;
      
      var merged = [];
      if (subTaskDetails[sName]) merged = merged.concat(subTaskDetails[sName]);
      if (subExtraSteps[sName]) merged = merged.concat(subExtraSteps[sName]);
      // 按 started_at 排序
      merged.sort(function(a, b) {
        return (a.started_at || '').localeCompare(b.started_at || '');
      });
      finalSteps = finalSteps.concat(merged);
    } else {
      finalSteps.push(s);
    }
  });
  steps = finalSteps;

  if (steps.length === 0) {
    document.getElementById('liveBody').innerHTML = '<div class="m-task-empty">暂无日志</div>';
    return;
  }

  var container = document.getElementById('liveBody');
  var isActive = ['running','waiting_confirm','in_progress','paused'].includes(task.status);
  var statusLabels = {
    running: '执行中', in_progress: '执行中', waiting_confirm: '待确认',
    paused: '已暂停', completed: '已完成', rolled_back: '已回滚', failed: '已失败', partially_failed: '部分失败'
  };
  var costStr = task.cost_usd ? '$' + Number(task.cost_usd).toFixed(2) : '';
  var elapsed = formatElapsed(task.started_at, isActive ? null : task.updated_at);

  // Check if we can do incremental update
  var existingPanels = container.querySelector('.m-live-panels');
  var canIncremental = existingPanels && container.querySelector('.m-live-task-card');

  if (!canIncremental) {
    // --- FULL RENDER (first load) ---
    if (!isActive) {
      document.getElementById('liveTitle').innerHTML = '&#9654; 直播回放';
    }

    var html = '<div style="padding:0 14px;flex-shrink:0;">';
    html += '<div class="m-live-task-card">';
    html += '<div class="m-ltc-row">';
    html += '<span class="m-task-status-dot ' + (task.status || '') + '" id="liveSummaryDot" style="width:8px;height:8px"></span>';
    html += '<span class="m-ltc-name" id="liveSummaryName">' + escHtml(getTaskTitle({task_title: task.task_title, task_name: task.task_name, description: task.description, id: taskId})) + '</span>';
    html += '<span class="tc-tag ' + (task.status || '') + '" id="liveSummaryTag">' + (statusLabels[task.status] || task.status || '') + '</span>';
    html += '</div>';
    html += '<div class="m-ltc-row"><div class="m-ltc-meta">';
    if (elapsed) html += '<span id="liveElapsed">&#128337; ' + elapsed + '</span>';
    if (costStr) html += '<span id="liveCost">&#128176; ' + costStr + '</span>';
    html += '</div></div>';
    html += '</div></div>';

    // Find the active (running) step index; -1 if none
    var activeStepIdx = -1;
    if (isActive) {
      for (var ai = steps.length - 1; ai >= 0; ai--) {
        if (steps[ai].status === 'running') { activeStepIdx = ai; break; }
      }
    }

    var lastSubName = null;
    html += '<div class="m-live-panels">';
    for (var i = 0; i < steps.length; i++) {
      var step = steps[i];
      var panelId = 'lp-' + i + '-' + (step.name || step.role || 'step');
      var isExpanded = (i === activeStepIdx);
      var cls = isExpanded ? 'expanded' : 'collapsed';
      if (hasSubTasks && step._sub_name && step._sub_name !== lastSubName) {
        lastSubName = step._sub_name;
        html += '<div class="lp-sub-group"><span class="sub-badge">' + escHtml(step._sub_id || '') + '</span>' + escHtml(step._sub_name) + '</div>';
      }
      if (hasSubTasks && step._sub_id) cls += ' lp-sub-indent';
      html += buildPanelHtml(step, panelId, cls, taskId);
    }
    html += '</div>';
    container.innerHTML = html;
    // Set dynamic height for the expanded panel after DOM render
    if (activeStepIdx >= 0) {
      var activePanelId = 'lp-' + activeStepIdx + '-' + (steps[activeStepIdx].name || steps[activeStepIdx].role || 'step');
      setTimeout(function() {
        resizeLPBody(activePanelId);
        // Scroll the active panel into view on first load
        var activeEl = document.getElementById(activePanelId);
        if (activeEl) activeEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }, 50);
    }
  } else {
    // --- INCREMENTAL UPDATE ---
    // Update title
    if (!isActive) {
      document.getElementById('liveTitle').innerHTML = '&#9654; 直播回放';
    }

    // Update summary card fields
    var dotEl = document.getElementById('liveSummaryDot');
    if (dotEl) dotEl.className = 'm-task-status-dot ' + (task.status || '');
    var tagEl = document.getElementById('liveSummaryTag');
    if (tagEl) {
      tagEl.className = 'tc-tag ' + (task.status || '');
      tagEl.textContent = statusLabels[task.status] || task.status || '';
    }
    var costEl = document.getElementById('liveCost');
    if (costEl) costEl.textContent = '\uD83D\uDCB0 ' + costStr;
    // elapsed is handled by timer, but update static value too
    var elapsedEl = document.getElementById('liveElapsed');
    if (elapsedEl && !isActive) elapsedEl.textContent = '\uD83D\uDD51 ' + elapsed;

    // Update existing panels + append new ones
    var panelsContainer = existingPanels;
    var existingCount = panelsContainer.querySelectorAll('.lp').length;

    for (var i = 0; i < steps.length; i++) {
      var step = steps[i];
      var panelId = 'lp-' + i + '-' + (step.name || step.role || 'step');
      var panelEl = document.getElementById(panelId);

      if (panelEl) {
        // Update existing panel header (dot + time)
        var dot = panelEl.querySelector('.lp-dot');
        if (dot) {
          var dotCls = step.status === 'running' ? 'active' : step.status === 'completed' ? 'done' : 'idle';
          dot.className = 'lp-dot ' + dotCls;
        }
        var timeEl = panelEl.querySelector('.lp-time');
        if (timeEl) {
          var timeStr = '';
          if (step.status === 'completed' && step.duration) timeStr = '✓ ' + formatDuration(step.duration);
          else if (step.status === 'running') timeStr = '运行中...';
          else if (step.status === 'pending') timeStr = '等待中';
          else if (step.duration) timeStr = formatDuration(step.duration);
          timeEl.textContent = timeStr;
        }
        // 恢复任务时，步骤从 failed/completed 变回 running，自动展开并刷新日志
        // 但不干扰用户已手动展开的其他面板
        if (step.status === 'running' && panelEl.classList.contains('collapsed') && !panelEl._userCollapsed) {
          panelEl.classList.remove('collapsed');
          panelEl.classList.add('expanded');
          loadPanelLogs(taskId, step, panelId);
          setTimeout(function() {
            resizeAllLPBodies();
            panelEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
          }, 50);
        }
      } else {
        // Append new panel — only expand if it's currently running
        var isRunningStep = (step.status === 'running');
        var tempDiv = document.createElement('div');
        tempDiv.innerHTML = buildPanelHtml(step, panelId, isRunningStep ? 'expanded' : 'collapsed', taskId);
        var newPanel = tempDiv.firstElementChild;
        panelsContainer.appendChild(newPanel);
        // Auto-load logs for running step
        if (isRunningStep) {
          loadPanelLogs(taskId, step, panelId);
          setTimeout(function() {
            resizeAllLPBodies();
            newPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
          }, 50);
        }
      }
    }
  }

  // Start dynamic elapsed timer for live panel
  stopElapsedTimer('live');
  if (isActive && task.started_at) {
    startElapsedTimer('live', task.started_at, 'liveElapsed');
  }

  // Event delegation for panel expand/collapse
  if (!container._lpDelegated) {
    container._lpDelegated = true;
    container.addEventListener('click', function(e) {
      var el = e.target;
      while (el && el !== container) {
        if (el.classList.contains('lp-header')) {
          var pid = el.getAttribute('data-panel-id');
          if (pid) toggleLP(pid);
          return;
        }
        el = el.parentElement;
      }
    });
  }

  // Load logs for expanded panels (skip completed steps that already have logs)
  for (var j = 0; j < steps.length; j++) {
    var s = steps[j];
    var pid = 'lp-' + j + '-' + (s.name || s.role || 'step');
    var panel = document.getElementById(pid);
    if (panel && panel.classList.contains('expanded')) {
      // 已完成步骤且已有日志内容，跳过重复加载
      var panelBody = document.getElementById('lp-body-' + pid);
      if (s.status === 'completed' && panelBody && panelBody.querySelector('.m-log-entry')) continue;
      loadPanelLogs(taskId, s, pid);
    }
  }

}

function buildPanelHtml(step, panelId, cls, taskId) {
  var dotCls = step.status === 'running' ? 'active' : step.status === 'completed' ? 'done' : 'idle';
  var timeStr = '';
  if (step.status === 'completed' && step.duration) timeStr = '✓ ' + formatDuration(step.duration);
  else if (step.status === 'running') timeStr = '运行中...';
  else if (step.status === 'pending') timeStr = '等待中';
  else if (step.duration) timeStr = formatDuration(step.duration);

  // 重试次数显示
  var retryStr = '';
  if (step._retryCount && step._retryCount > 0) {
    retryStr = ' <span style="color:var(--yellow);font-size:0.68rem;">(重试' + step._retryCount + '次)</span>';
  }

  var html = '<div class="lp ' + cls + '" id="' + escHtml(panelId) + '" data-task-id="' + escHtml(taskId) + '" data-step-name="' + escHtml(step.name || '') + '" data-role="' + escHtml(step.role || '') + '"' + (step._sub_id ? ' data-sub-id="' + escHtml(step._sub_id) + '"' : '') + '>';
  html += '<div class="lp-header" data-panel-id="' + escHtml(panelId) + '">';
  html += '<span class="lp-dot ' + dotCls + '"></span>';
  html += '<span class="lp-name">' + escHtml(stepDisplayName(step)) + retryStr + '</span>';
  if (step.model) html += '<span class="lp-model">' + escHtml(step.model) + '</span>';
  html += '<span class="lp-time">' + escHtml(timeStr) + '</span>';
  html += '<span class="lp-expand-icon">&#9660;</span>';
  html += '</div>';
  html += '<div class="lp-body" id="lp-body-' + escHtml(panelId) + '">';
  html += '<button class="lp-scroll-fab" id="lp-fab-' + escHtml(panelId) + '" onclick="event.stopPropagation();panelScrollToBottom(\'' + escHtml(panelId) + '\')">&#8595; 底部</button>';
  html += '</div>';
  html += '</div>';
  return html;
}

async function loadPanelLogs(taskId, step, stepId) {
  var url = '/vizo/console/api/opus/logs?task_id=' + taskId;
  var sid = step.step_id || step.name || '';
  if (sid) url += '&step_id=' + encodeURIComponent(sid);
  if (step.role) url += '&role=' + encodeURIComponent(step.role);
  if (step._sub_id) url += '&sub_id=' + encodeURIComponent(step._sub_id);

  // 计算该面板是同名步骤的第几次运行，传 run_index 让后端分段返回
  var panelEl = document.getElementById(stepId);
  if (panelEl) {
    var allPanels = panelEl.parentElement ? panelEl.parentElement.querySelectorAll('.lp') : [];
    var runIdx = 0;
    for (var pi = 0; pi < allPanels.length; pi++) {
      if (allPanels[pi].id === stepId) break;
      if ((allPanels[pi].dataset.stepName || '') === (step.name || '') &&
          (allPanels[pi].dataset.role || '') === (step.role || '')) {
        runIdx++;
      }
    }
    url += '&run_index=' + runIdx;
  }

  var data = await apiCall(url);
  if (!data) return;

  var actions = data.actions || [];
  var body = document.getElementById('lp-body-' + stepId);
  if (!body) return;

  if (actions.length === 0) {
    var fab0 = document.getElementById('lp-fab-' + stepId);
    body.innerHTML = '<div style="color:var(--text-muted);font-size:0.75rem;padding:8px;">暂无日志</div>';
    if (fab0) body.appendChild(fab0);
    return;
  }

  // Render last 100 actions (web console style)
  var visible = actions.slice(-100);
  var html = '';
  var typeLabels = {read:'读取', write:'写入', exec:'执行', think:'思考', search:'搜索', output:'输出', init:'初始化'};
  visible.forEach(function(a) {
    var ts = a.timestamp || '';
    var typeLabel = typeLabels[a.type] || a.type || '';
    var typeCls = a.type || '';
    var target = a.target || '';

    html += '<div class="m-log-entry">';
    html += '<span class="m-log-ts">' + escHtml(ts) + '</span>';
    html += '<span class="m-log-type ' + typeCls + '">[' + escHtml(typeLabel) + ']</span>';
    html += '<span class="m-log-target">' + escHtml(target) + '</span>';
    html += '</div>';
    if (a.snippet) {
      var snippetCls = 'm-log-snippet' + (a.type === 'think' ? ' think-snippet' : a.type === 'output' ? ' output-snippet' : '');
      html += '<div class="' + snippetCls + '">' + escHtml(a.snippet) + '</div>';
    }
  });
  // Preserve FAB button, replace only log content
  var fab = document.getElementById('lp-fab-' + stepId);
  body.innerHTML = html;
  if (fab) body.appendChild(fab);

  // Per-panel auto-scroll with FAB
  if (!body.dataset.autoScroll) body.dataset.autoScroll = 'true';
  if (!body._scrollListenerAdded) {
    body._scrollListenerAdded = true;
    body.addEventListener('scroll', function() {
      var isAtBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 50;
      body.dataset.autoScroll = isAtBottom ? 'true' : 'false';
      var fab = document.getElementById('lp-fab-' + stepId);
      if (fab) fab.style.display = isAtBottom ? 'none' : 'block';
    });
  }
  if (body.dataset.autoScroll === 'true') {
    body.scrollTop = body.scrollHeight;
  }
}

function toggleLP(panelId) {
  var el = document.getElementById(panelId);
  if (!el) return;
  var body = el.querySelector('.lp-body');

  if (el.classList.contains('expanded')) {
    // === COLLAPSING with animation ===
    el._userCollapsed = true;
    el._userExpanded = false;
    if (body) {
      body.style.overflow = 'hidden';
      body.style.height = body.offsetHeight + 'px';
      void body.offsetHeight; // force reflow
      body.style.height = '0px';
      body.style.opacity = '0';
      body.style.paddingTop = '0';
      body.style.paddingBottom = '0';
    }
    setTimeout(function() {
      el.classList.remove('expanded');
      el.classList.add('collapsed');
      if (body) {
        body.style.height = '';
        body.style.opacity = '';
        body.style.overflow = '';
        body.style.paddingTop = '';
        body.style.paddingBottom = '';
      }
      resizeAllLPBodies();
    }, 320);
  } else {
    // === EXPANDING with animation ===
    el._userExpanded = true;
    el._userCollapsed = false;
    el.classList.remove('collapsed');
    el.classList.add('expanded');
    // Start from zero
    if (body) {
      body.style.height = '0px';
      body.style.opacity = '0';
      body.style.overflow = 'hidden';
      void body.offsetHeight; // force reflow
    }
    // Load logs if body has no log entries yet
    if (body && !body.querySelector('.m-log-entry')) {
      var fab = document.getElementById('lp-fab-' + panelId);
      body.innerHTML = '<div style="color:var(--text-muted);font-size:0.75rem;padding:8px;">加载中...</div>';
      if (fab) body.appendChild(fab);
      var taskId = el.dataset.taskId;
      var stepName = el.dataset.stepName;
      var role = el.dataset.role;
      var subId = el.dataset.subId || '';
      if (taskId) {
        loadPanelLogs(taskId, {name: stepName || undefined, role: role || undefined, _sub_id: subId || undefined}, panelId);
      }
    }
    // Animate to target height
    requestAnimationFrame(function() {
      resizeAllLPBodies(); // sets body.style.height = Xpx
      if (body) body.style.opacity = '1';
      // Enable scroll after animation completes
      setTimeout(function() {
        if (body && el.classList.contains('expanded')) {
          body.style.overflow = '';
          body.style.overflowY = 'auto';
          body.style.opacity = '';
        }
      }, 320);
    });
  }
}

// Distribute available height equally among all expanded panel bodies
function resizeAllLPBodies() {
  var container = document.querySelector('.m-live-panels');
  if (!container) return;
  var expanded = container.querySelectorAll('.lp.expanded');
  var expandedCount = expanded.length;
  if (expandedCount === 0) return;

  // Viewport-based calculation
  var overlayHeader = document.querySelector('#liveOverlay .m-overlay-header');
  var summaryCard = container.previousElementSibling;
  var liveBody = document.getElementById('liveBody');
  var overlayHeaderH = overlayHeader ? overlayHeader.offsetHeight : 48;
  var summaryCardH = summaryCard ? summaryCard.offsetHeight : 0;
  var liveBodyPadTop = liveBody ? parseFloat(getComputedStyle(liveBody).paddingTop) : 14;
  var gap = parseFloat(getComputedStyle(container).gap) || 8;

  // Sum expanded panel header heights
  var expandedHeadersH = 0;
  for (var i = 0; i < expanded.length; i++) {
    var h = expanded[i].querySelector('.lp-header');
    expandedHeadersH += h ? h.offsetHeight : 42;
  }

  // Reserve one collapsed panel height at bottom as visual hint
  var oneCollapsedH = 50;

  // Available = viewport - overlay header - summary card - padding
  //           - expanded headers - reserved bottom - gaps
  var availH = window.innerHeight - overlayHeaderH - summaryCardH - liveBodyPadTop
               - expandedHeadersH - oneCollapsedH
               - gap * (expandedCount + 1);
  var bodyH = Math.max(Math.floor(availH / expandedCount), 150);

  for (var i = 0; i < expanded.length; i++) {
    var body = expanded[i].querySelector('.lp-body');
    if (body) body.style.height = bodyH + 'px';
  }
}

// Alias for initial render
function resizeLPBody(panelId) { resizeAllLPBodies(); }

/* ============================================================
   File Preview
   ============================================================ */
async function loadTaskFiles(project, dirPath) {
  var container = document.getElementById('taskFileList');
  if (!container) return;
  var data = await apiCall('/vizo/console/api/projects/' + encodeURIComponent(project) + '/files?path=' + encodeURIComponent(dirPath));
  if (!data || data.type !== 'dir' || !data.entries) {
    container.innerHTML = '<div style="padding:12px;color:var(--text-muted);font-size:0.75rem;">无法加载文件列表</div>';
    return;
  }
  var entries = data.entries.filter(function(e) { return e.name !== '.lock'; });
  if (entries.length === 0) {
    container.innerHTML = '<div style="padding:12px;color:var(--text-muted);font-size:0.75rem;">暂无文件</div>';
    return;
  }
  var html = '';
  entries.forEach(function(e) {
    var icon = e.type === 'dir' ? '\uD83D\uDCC1' : '\uD83D\uDCC4';
    var sizeStr = e.type === 'dir' ? '' : formatFileSize(e.size || 0);
    var subPath = dirPath + '/' + e.name;
    html += '<div class="fb-item" onclick="previewFile(\'' + escHtml(project) + '\',\'' + escHtml(subPath) + '\')">';
    html += '<span class="fb-icon">' + icon + '</span><span class="fb-name">' + escHtml(e.name) + '</span><span class="fb-size">' + sizeStr + '</span></div>';
  });
  container.innerHTML = html;
}

async function previewFile(project, filePath) {
  var overlay = document.getElementById('fileOverlay');
  overlay.classList.add('open');
  document.getElementById('fileTitle').textContent = filePath.split('/').pop();
  document.getElementById('fileBody').innerHTML = '<div class="m-task-empty">加载中...</div>';

  var data = await apiCall('/vizo/console/api/projects/' + encodeURIComponent(project) + '/files?path=' + encodeURIComponent(filePath));
  if (!data) {
    document.getElementById('fileBody').innerHTML = '<div class="m-task-empty">加载失败</div>';
    return;
  }

  if (data.binary) {
    document.getElementById('fileBody').innerHTML = '<div class="m-task-empty">二进制文件，无法预览</div>';
    return;
  }

  if (data.type === 'dir') {
    var html = '<div class="fb-list">';
    (data.entries || []).forEach(function(e) {
      var icon = e.type === 'dir' ? '\uD83D\uDCC1' : '\uD83D\uDCC4';
      var sizeStr = e.type === 'dir' ? (e.size || '') : formatFileSize(e.size || 0);
      var subPath = filePath ? filePath + '/' + e.name : e.name;
      if (e.type === 'dir') {
        html += '<div class="fb-item" onclick="previewFile(\'' + escHtml(project) + '\',\'' + escHtml(subPath) + '\')">';
      } else {
        html += '<div class="fb-item" onclick="previewFile(\'' + escHtml(project) + '\',\'' + escHtml(subPath) + '\')">';
      }
      html += '<span class="fb-icon">' + icon + '</span><span class="fb-name">' + escHtml(e.name) + '</span><span class="fb-size">' + sizeStr + '</span></div>';
    });
    html += '</div>';
    document.getElementById('fileTitle').textContent = filePath.split('/').pop() || '任务文件';
    document.getElementById('fileBody').innerHTML = html;
    return;
  }

  var content = data.content || '';
  document.getElementById('fileBody').innerHTML = '<div class="fb-preview">' + escHtml(content) + '</div>';
}

function formatFileSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

/* ============================================================
   Overlay Management
   ============================================================ */
function closeOverlay(id) {
  document.getElementById(id).classList.remove('open');
  if (id === 'liveOverlay') {
    clearInterval(livePolling);
    livePolling = null;
    liveTaskId = null;
    stopElapsedTimer('live');
  }
  if (id === 'taskDetailOverlay') {
    stopElapsedTimer('detail');
  }
}

/* Per-panel scroll-to-bottom */
function panelScrollToBottom(panelId) {
  var body = document.getElementById('lp-body-' + panelId);
  if (body) {
    body.scrollTo({top: body.scrollHeight, behavior: 'smooth'});
    body.dataset.autoScroll = 'true';
    var fab = document.getElementById('lp-fab-' + panelId);
    if (fab) fab.style.display = 'none';
  }
}

/* ============================================================
   Utilities
   ============================================================ */
function escHtml(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function formatDuration(sec) {
  if (!sec || sec < 0) return '';
  sec = Math.round(sec);
  if (sec < 60) return sec + 's';
  var m = Math.floor(sec / 60);
  var s = sec % 60;
  if (m < 60) return m + 'm' + (s > 0 ? s + 's' : '');
  var h = Math.floor(m / 60);
  m = m % 60;
  return h + 'h' + (m > 0 ? m + 'm' : '');
}

function formatElapsed(startedAt, endedAt) {
  if (!startedAt) return '';
  var end = endedAt ? new Date(endedAt) : new Date();
  var diffSec = Math.max(0, Math.round((end - new Date(startedAt)) / 1000));
  return formatDuration(diffSec);
}

function startElapsedTimer(key, startedAt, elementId) {
  stopElapsedTimer(key);
  if (!startedAt) return;
  var prefix = '';
  var el = document.getElementById(elementId);
  if (el) {
    var text = el.textContent;
    // Preserve emoji prefix like "🕐 "
    var match = text.match(/^[^\d]*/);
    if (match) prefix = match[0];
  }
  elapsedTimers[key] = setInterval(function() {
    var el = document.getElementById(elementId);
    if (!el) { stopElapsedTimer(key); return; }
    el.textContent = prefix + formatElapsed(startedAt);
  }, 1000);
}

function stopElapsedTimer(key) {
  if (elapsedTimers[key]) {
    clearInterval(elapsedTimers[key]);
    delete elapsedTimers[key];
  }
}

function stopAllElapsedTimers() {
  Object.keys(elapsedTimers).forEach(function(k) { clearInterval(elapsedTimers[k]); });
  elapsedTimers = {};
}

// PWA Service Worker
if ('serviceWorker' in navigator) {
  var isSecure = location.protocol === 'https:' || location.hostname === 'localhost';
  if (isSecure) {
    navigator.serviceWorker.register('/vizo/m/sw.js', { scope: '/vizo/m' })
      .catch(function(e) { console.warn('[PWA] SW register failed:', e); });
  }
}

/* ============================================================
   Init
   ============================================================ */
window.addEventListener('load', function() {
  buildShortcuts();
  buildWorkflows();
  initTerminal();
  pollTasks();
});

// ====== Model Config (mmc) ======
var mmcState = { roles: [], available: [] };

async function openMobileModelConfig() {
  document.getElementById('mobileModelConfig').style.display = '';
  document.getElementById('mmc-list').innerHTML = '<div class="mmc-loading">加载中...</div>';
  try {
    var res = await fetch('/vizo/console/api/settings/models', { credentials: 'include' });
    var data = await res.json();
    mmcState.roles = data.roles || [];
    mmcState.available = data.available_models || [];
    renderMobileModelConfig();
  } catch(e) {
    document.getElementById('mmc-list').innerHTML = '<div class="mmc-loading">加载失败</div>';
  }
}

function closeMobileModelConfig() {
  document.getElementById('mobileModelConfig').style.display = 'none';
}

function renderMobileModelConfig() {
  var html = '';
  mmcState.roles.forEach(function(r) {
    var modelLabel = '';
    mmcState.available.forEach(function(m) {
      if (m.id === r.current_model) modelLabel = m.display;
    });
    if (!modelLabel) modelLabel = r.current_model;
    var modified = r.current_model !== r.default_model;
    html += '<div class="mmc-item" data-role="' + escHtml(r.role) + '" data-action="mmc-change-model">';
    html += '<div class="mmc-item-left">';
    html += '<span class="mmc-item-name">' + escHtml(r.display_name) + '</span>';
    if (modified) html += '<span class="mmc-item-badge">已修改</span>';
    html += '</div>';
    html += '<div class="mmc-item-right">';
    html += '<span class="mmc-item-model">' + escHtml(modelLabel) + '</span>';
    html += '<span class="mmc-item-arrow">&#8250;</span>';
    html += '</div>';
    html += '</div>';
  });
  document.getElementById('mmc-list').innerHTML = html;
}

function showMobileModelPicker(role) {
  var r = mmcState.roles.find(function(x) { return x.role === role; });
  if (!r) return;
  var html = '<div class="mmc-picker-title">' + escHtml(r.display_name) + ' — 选择模型</div>';
  mmcState.available.forEach(function(m) {
    var checked = m.id === r.current_model ? ' mmc-picker-selected' : '';
    html += '<div class="mmc-picker-option' + checked + '" data-model="' + escHtml(m.id) + '" data-role="' + escHtml(role) + '" data-action="mmc-select-model">';
    html += '<span>' + escHtml(m.display) + '</span>';
    if (m.id === r.current_model) html += '<span>✓</span>';
    html += '</div>';
  });
  html += '<div class="mmc-picker-cancel" data-action="mmc-cancel-picker">取消</div>';

  var sheet = document.getElementById('mmcPickerSheet');
  if (!sheet) {
    var backdrop = document.createElement('div');
    backdrop.id = 'mmcPickerBackdrop';
    backdrop.className = 'mmc-picker-backdrop';
    backdrop.setAttribute('data-action', 'mmc-cancel-picker');
    document.body.appendChild(backdrop);
    sheet = document.createElement('div');
    sheet.id = 'mmcPickerSheet';
    sheet.className = 'mmc-picker-sheet';
    document.body.appendChild(sheet);
  }
  sheet.innerHTML = html;
  sheet.style.display = 'block';
  document.getElementById('mmcPickerBackdrop').style.display = 'block';
  requestAnimationFrame(function() {
    sheet.classList.add('mmc-picker-visible');
  });
}

function closeMobileModelPicker() {
  var sheet = document.getElementById('mmcPickerSheet');
  var backdrop = document.getElementById('mmcPickerBackdrop');
  if (sheet) { sheet.classList.remove('mmc-picker-visible'); sheet.style.display = 'none'; }
  if (backdrop) backdrop.style.display = 'none';
}

async function saveMobileModelChange(role, modelId) {
  closeMobileModelPicker();
  var overrides = {};
  mmcState.roles.forEach(function(r) {
    overrides[r.role] = r.role === role ? modelId : r.current_model;
  });
  try {
    var res = await fetch('/vizo/console/api/settings/models', {
      method: 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model_overrides: overrides })
    });
    var data = await res.json();
    if (data.success) {
      openMobileModelConfig();
    }
  } catch(e) { /* 静默失败，用户可再次尝试 */ }
}

/* ============================================================
   "我的" Tab + Agent 管理
   ============================================================ */
var maAgents = [];
var maCreateStep = 0;
var maPreviewData = null;
var maCreateDesc = '';

function updateMeTab() {
  var el = document.getElementById('meAgentCount');
  if (el && maAgents.length) el.textContent = maAgents.length + ' 个模块';
}

function openAgentOverlay() {
  document.getElementById('agentOverlay').classList.add('open');
  document.getElementById('agentOverlayTitle').textContent = 'Agent 模块';
  document.getElementById('maAddBtn').style.display = '';
  maCreateStep = 0;
  loadAgentList();
}

function closeAgentOverlay() {
  document.getElementById('agentOverlay').classList.remove('open');
  maCreateStep = 0;
  maPreviewData = null;
}

async function loadAgentList() {
  var body = document.getElementById('agentOverlayBody');
  body.innerHTML = '<div class="ma-loading"><div class="ma-loading-spinner"></div></div>';
  try {
    var resp = await apiCall('/vizo/console/api/agents');
    if (resp && resp.success) {
      maAgents = resp.data || [];
      body.innerHTML = renderAgentList(maAgents);
      var el = document.getElementById('meAgentCount');
      if (el) el.textContent = maAgents.length + ' 个模块';
    } else {
      body.innerHTML = '<div class="ma-empty"><div class="ma-empty-text">加载失败</div></div>';
    }
  } catch(e) {
    body.innerHTML = '<div class="ma-empty"><div class="ma-empty-text">网络错误</div></div>';
  }
}

function renderAgentList(agents) {
  if (!agents || agents.length === 0) {
    return '<div class="ma-empty">'
      + '<div class="ma-empty-icon">&#129302;</div>'
      + '<div class="ma-empty-text">还没有自定义 Agent</div>'
      + '<button class="ma-empty-btn" data-action="startAgentCreate">创建第一个</button>'
      + '</div>';
  }
  var builtin = agents.filter(function(a) { return a.source === '_builtin'; });
  var user = agents.filter(function(a) { return a.source === '_user'; });
  var html = '';
  if (builtin.length) {
    html += '<div class="ma-group-title">官方模块</div>';
    builtin.forEach(function(a) { html += renderAgentCard(a); });
  }
  if (user.length) {
    html += '<div class="ma-group-title">自建模块</div>';
    user.forEach(function(a) { html += renderAgentCard(a); });
  }
  return html;
}

function renderAgentCard(a) {
  var html = '<div class="ma-card" data-action="agentCardTap" data-agent-id="' + escHtml(a.id) + '">';
  html += '<div class="ma-card-icon">' + (a.icon || '&#129302;') + '</div>';
  html += '<div class="ma-card-body">';
  html += '<div class="ma-card-name">' + escHtml(a.name || a.id) + '</div>';
  html += '<div class="ma-card-desc">' + escHtml((a.description || '').substring(0, 60)) + '</div>';
  html += '</div>';
  if (a.source === '_user') {
    html += '<button class="ma-card-del" data-action="deleteAgent" data-agent-id="' + escHtml(a.id) + '">&#128465;</button>';
  }
  html += '</div>';
  return html;
}

function startAgentCreate() {
  maCreateStep = 1;
  maCreateDesc = '';
  maPreviewData = null;
  document.getElementById('agentOverlayTitle').textContent = '创建 Agent';
  document.getElementById('maAddBtn').style.display = 'none';
  document.getElementById('agentOverlayBody').innerHTML = renderCreateStep1();
  var textarea = document.getElementById('maDescInput');
  if (textarea) {
    textarea.addEventListener('input', function() {
      document.getElementById('maCharCount').textContent = this.value.length;
    });
  }
}

function renderCreateStep1() {
  return '<div class="ma-create">'
    + '<div class="ma-create-hint">描述你想要的 Agent，越详细越好</div>'
    + '<textarea class="ma-create-input" id="maDescInput" rows="8"'
    + ' placeholder="例如：一个帮我写周报的助手，能根据本周的工作记录自动生成结构化周报"></textarea>'
    + '<div class="ma-create-counter"><span id="maCharCount">0</span>/300</div>'
    + '<button class="ma-create-btn" data-action="generateAgent">生成模块</button>'
    + '</div>';
}

function renderCreateStep2() {
  return '<div class="ma-loading">'
    + '<div class="ma-loading-spinner"></div>'
    + '<div class="ma-loading-text">AI 正在设计工作流…</div>'
    + '<div class="ma-loading-hint">约 20~40 秒</div>'
    + '</div>';
}

function renderCreateStep3(preview) {
  var html = '<div class="ma-preview">';
  html += '<div class="ma-field"><label class="ma-field-label">模块名称</label>';
  html += '<input class="ma-field-input" id="maNameInput" value="' + escHtml(preview.name || '') + '"></div>';
  html += '<div class="ma-field"><label class="ma-field-label">模块 ID</label>';
  html += '<input class="ma-field-input" id="maIdInput" value="' + escHtml(preview.id || '') + '">';
  html += '<div class="ma-field-error" id="maIdError"></div></div>';
  html += '<div class="ma-field"><label class="ma-field-label">描述</label>';
  html += '<textarea class="ma-field-input ma-field-textarea" id="maDescEdit">' + escHtml(preview.description || '') + '</textarea></div>';
  html += '<div class="ma-steps-title">工作流步骤</div>';
  var steps = _extractSteps(preview);
  steps.forEach(function(s, i) {
    html += '<div class="ma-step-item">';
    html += '<span class="ma-step-num">' + (i + 1) + '</span>';
    html += '<div class="ma-step-body">';
    html += '<div class="ma-step-name">' + escHtml(s.name || '步骤 ' + (i + 1)) + '</div>';
    html += '<div class="ma-step-role">' + escHtml(s.role || '') + '</div>';
    html += '</div></div>';
  });
  html += '<div class="ma-preview-actions">';
  html += '<button class="ma-preview-btn secondary" data-action="regenerateAgent">重新生成</button>';
  html += '<button class="ma-preview-btn primary" data-action="saveAgent">保存模块</button>';
  html += '</div>';
  html += '</div>';
  return html;
}

function _extractSteps(manifest) {
  var workflows = manifest.workflows || {};
  for (var wfId in workflows) {
    var wf = workflows[wfId];
    if (wf.steps) return wf.steps;
    if (wf.stages) {
      var flat = [];
      wf.stages.forEach(function(stage) {
        (stage.steps || []).forEach(function(s) { flat.push(s); });
      });
      if (flat.length) return flat;
    }
  }
  return manifest.steps || [];
}

async function generateAgent() {
  var input = document.getElementById('maDescInput');
  if (!input) return;
  var desc = input.value.trim();
  if (!desc) { showToast('请输入描述'); return; }
  maCreateDesc = desc;
  maCreateStep = 2;
  document.getElementById('agentOverlayBody').innerHTML = renderCreateStep2();

  var controller = new AbortController();
  var timeoutId = setTimeout(function() { controller.abort(); }, 120000);
  try {
    var resp = await apiCall('/vizo/console/api/agents/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ description: desc }),
      signal: controller.signal
    });
    clearTimeout(timeoutId);
    if (resp && resp.success && resp.data && resp.data.module) {
      maPreviewData = resp.data.module;
      maCreateStep = 3;
      document.getElementById('agentOverlayBody').innerHTML = renderCreateStep3(resp.data.module.manifest);
    } else {
      showCreateError(resp && resp.error ? resp.error : '生成失败，请重试');
    }
  } catch(e) {
    clearTimeout(timeoutId);
    showCreateError(e.name === 'AbortError' ? '生成超时（>120秒），请重试' : '网络错误，请重试');
  }
}

function showCreateError(msg) {
  maCreateStep = 1;
  document.getElementById('agentOverlayBody').innerHTML = renderCreateStep1();
  var textarea = document.getElementById('maDescInput');
  if (textarea) {
    textarea.value = maCreateDesc;
    textarea.addEventListener('input', function() {
      document.getElementById('maCharCount').textContent = this.value.length;
    });
    document.getElementById('maCharCount').textContent = maCreateDesc.length;
  }
  showToast(msg);
}

async function saveAgent() {
  var nameInput = document.getElementById('maNameInput');
  var idInput = document.getElementById('maIdInput');
  var descEdit = document.getElementById('maDescEdit');
  if (!nameInput || !idInput) return;

  var name = nameInput.value.trim();
  var id = idInput.value.trim();
  var desc = descEdit ? descEdit.value.trim() : '';

  if (!name) { showToast('请填写模块名称'); return; }
  if (!id) { showToast('请填写模块 ID'); return; }
  if (!/^[a-zA-Z0-9_]+$/.test(id)) {
    showToast('ID 只能包含字母、数字和下划线');
    return;
  }

  var conflict = maAgents.find(function(a) { return a.id === id; });
  if (conflict) {
    var errEl = document.getElementById('maIdError');
    if (errEl) { errEl.textContent = '此 ID 已被使用'; errEl.style.display = 'block'; }
    return;
  }

  var updatedManifest = Object.assign({}, maPreviewData.manifest, { name: name, id: id, description: desc });
  var saveData = { manifest: updatedManifest, roles: maPreviewData.roles, module_id: id };
  var btn = document.querySelector('[data-action="saveAgent"]');
  if (btn) { btn.disabled = true; btn.textContent = '保存中…'; }

  try {
    var resp = await apiCall('/vizo/console/api/agents', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(saveData)
    });
    if (resp && resp.success) {
      showToast('创建成功');
      maCreateStep = 0;
      document.getElementById('agentOverlayTitle').textContent = 'Agent 模块';
      document.getElementById('maAddBtn').style.display = '';
      loadAgentList();
    } else {
      showToast(resp && resp.error ? resp.error : '保存失败');
      if (btn) { btn.disabled = false; btn.textContent = '保存模块'; }
    }
  } catch(e) {
    showToast('网络错误');
    if (btn) { btn.disabled = false; btn.textContent = '保存模块'; }
  }
}

async function deleteAgent(agentId) {
  if (!confirm('确定要删除此模块吗？')) return;
  try {
    var resp = await apiCall('/vizo/console/api/agents/' + encodeURIComponent(agentId), { method: 'DELETE' });
    if (resp && resp.success) {
      showToast('已删除');
      loadAgentList();
    } else {
      showToast(resp && resp.error ? resp.error : '删除失败');
    }
  } catch(e) {
    showToast('网络错误');
  }
}

function regenerateAgent() {
  maCreateStep = 2;
  document.getElementById('agentOverlayBody').innerHTML = renderCreateStep2();
  var controller = new AbortController();
  var timeoutId = setTimeout(function() { controller.abort(); }, 120000);
  apiCall('/vizo/console/api/agents/generate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ description: maCreateDesc }),
    signal: controller.signal
  }).then(function(resp) {
    clearTimeout(timeoutId);
    if (resp && resp.success && resp.data && resp.data.module) {
      maPreviewData = resp.data.module;
      maCreateStep = 3;
      document.getElementById('agentOverlayBody').innerHTML = renderCreateStep3(resp.data.module.manifest);
    } else {
      showCreateError(resp && resp.error ? resp.error : '生成失败，请重试');
    }
  }).catch(function(e) {
    clearTimeout(timeoutId);
    showCreateError(e.name === 'AbortError' ? '生成超时，请重试' : '网络错误');
  });
}

// Unified event delegation (Tab, Agent, Model config)
document.addEventListener('click', function(e) {
  var target = e.target.closest('[data-action]');
  if (!target) return;
  var action = target.dataset.action;

  // Tab switching
  if (action === 'switchTab') { switchTab(target); return; }
  if (action === 'openNewSession') { openNewSessionSheet(); return; }

  // Agent management
  if (action === 'openAgents') { openAgentOverlay(); return; }
  if (action === 'closeAgentOverlay') {
    if (maCreateStep > 0) {
      maCreateStep = 0;
      document.getElementById('agentOverlayTitle').textContent = 'Agent 模块';
      document.getElementById('maAddBtn').style.display = '';
      loadAgentList();
    } else {
      closeAgentOverlay();
    }
    return;
  }
  if (action === 'startAgentCreate') { startAgentCreate(); return; }
  if (action === 'generateAgent') { generateAgent(); return; }
  if (action === 'regenerateAgent') { regenerateAgent(); return; }
  if (action === 'saveAgent') { saveAgent(); return; }
  if (action === 'deleteAgent') {
    e.stopPropagation();
    deleteAgent(target.dataset.agentId);
    return;
  }
  if (action === 'agentCardTap') {
    var agentId = target.dataset.agentId;
    var agent = maAgents.find(function(a) { return a.id === agentId; });
    if (agent) showToast('使用命令: opus "任务" @' + agent.id);
    return;
  }

  // Model config
  if (action === 'open-mobile-model-config') openMobileModelConfig();
  else if (action === 'close-mobile-model-config') closeMobileModelConfig();
  else if (action === 'mmc-change-model') showMobileModelPicker(target.dataset.role);
  else if (action === 'mmc-select-model') saveMobileModelChange(target.dataset.role, target.dataset.model);
  else if (action === 'mmc-cancel-picker') closeMobileModelPicker();
});
</script>
</body>
</html>"""


# ============================================================
# PWA Manifest JSON
# ============================================================
_PWA_ICON_SVG_B64 = (
    "data:image/svg+xml;base64,"
    "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA1MTIgNTEyIj48cmVjdCB3aWR0aD0iNTEyIiBoZWlnaHQ9IjUxMiIgcng9IjgwIiBmaWxsPSIjMGEwZTE3Ii8+"
    "PHRleHQgeD0iMjU2IiB5PSIyODAiIHRleHQtYW5jaG9yPSJtaWRkbGUiIGZvbnQtZmFtaWx5PSJzeXN0ZW0tdWksLWFwcGxlLXN5c3RlbSxzYW5zLXNlcmlmIiBmb250LXNpemU9IjIyMCIgZm9udC13ZWlnaHQ9IjcwMCIgZmlsbD0iIzM4YmRmOCI+TzY8L3RleHQ+"
    "PHRleHQgeD0iMjU2IiB5PSIzODAiIHRleHQtYW5jaG9yPSJtaWRkbGUiIGZvbnQtZmFtaWx5PSJzeXN0ZW0tdWksLWFwcGxlLXN5c3RlbSxzYW5zLXNlcmlmIiBmb250LXNpemU9IjYwIiBmb250LXdlaWdodD0iNDAwIiBmaWxsPSIjOTRhM2I4Ij5PUFVTPC90ZXh0Pjwvc3ZnPgo="
)

PWA_MANIFEST_JSON = """{
  "name": "Vizo",
  "short_name": "Vizo",
  "start_url": "/vizo/m",
  "display": "standalone",
  "theme_color": "#0ea5e9",
  "background_color": "#0a0e17",
  "scope": "/vizo/m",
  "icons": [
    {
      "src": "%s",
      "sizes": "192x192",
      "type": "image/svg+xml",
      "purpose": "any"
    },
    {
      "src": "%s",
      "sizes": "512x512",
      "type": "image/svg+xml",
      "purpose": "any"
    }
  ]
}""" % (_PWA_ICON_SVG_B64, _PWA_ICON_SVG_B64)


# ============================================================
# PWA Service Worker Script
# ============================================================
PWA_SW_JS = """var CACHE_NAME = 'opus-pwa-v1';
var PRE_CACHE_URLS = [
  '/vizo/m',
  '/vizo/static/xterm/xterm.css',
  '/vizo/static/xterm/xterm.js',
  '/vizo/static/xterm/addon-fit.js',
  '/vizo/static/xterm/addon-web-links.js',
  '/vizo/static/xterm/addon-canvas.js'
];

self.addEventListener('install', function(event) {
  event.waitUntil(
    caches.open(CACHE_NAME).then(function(cache) {
      return cache.addAll(PRE_CACHE_URLS).catch(function(err) {
        console.warn('[SW] Pre-cache failed (CDN unreachable?):', err);
      });
    })
  );
});

self.addEventListener('activate', function(event) {
  event.waitUntil(
    caches.keys().then(function(names) {
      return Promise.all(
        names.filter(function(n) { return n !== CACHE_NAME; })
             .map(function(n) { return caches.delete(n); })
      );
    })
  );
});

self.addEventListener('fetch', function(event) {
  event.respondWith(
    fetch(event.request).then(function(response) {
      var clone = response.clone();
      caches.open(CACHE_NAME).then(function(cache) {
        cache.put(event.request, clone);
      });
      return response;
    }).catch(function() {
      return caches.match(event.request).then(function(cached) {
        return cached || new Response('Offline', { status: 504, statusText: 'Offline' });
      });
    })
  );
});
"""
