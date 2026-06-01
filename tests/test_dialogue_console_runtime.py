from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "lib/templates/dialogue_console/pc.html"
RUNTIME = ROOT / "lib/templates/dialogue_console/runtime.js"
LIVE_PANEL = ROOT / "lib/templates/dialogue_console/live-panel-component.js"
CONFIRM_SERVER = ROOT / "lib/confirm_server.py"


def test_dialogue_has_scroll_bottom_action_and_session_scoped_composer_drafts():
    html = TEMPLATE.read_text(encoding="utf-8")
    runtime = RUNTIME.read_text(encoding="utf-8")
    assert 'id="scrollTimelineBottomBtn"' in html
    assert "滚动到最新消息" in html
    assert "arrow_downward" in html
    assert "timeline-bottom-btn.visible" in html
    assert "stream-toolbar-spacer" not in html
    assert ">到底部<" not in html
    assert 'const COMPOSER_DRAFT_KEY_PREFIX = "vizo.dialogue.composerDraft.";' in runtime
    assert "function persistComposerDraft(sessionId)" in runtime
    assert "function restoreComposerDraft(sessionId)" in runtime
    assert "function updateTimelineBottomButton(node)" in runtime
    assert "if (previousSessionId && previousSessionId !== sessionId) persistComposerDraft(previousSessionId);" in runtime
    assert 'bindIfPresent("scrollTimelineBottomBtn", "click", () =>' in runtime


def test_dialogue_has_main_session_interrupt_action():
    html = TEMPLATE.read_text(encoding="utf-8")
    runtime = RUNTIME.read_text(encoding="utf-8")
    confirm_server = CONFIRM_SERVER.read_text(encoding="utf-8")
    assert ".send-btn.interrupt-mode" in html
    assert "/vizo/static/icons/material-design/outlined/stop.svg" in runtime
    assert '"/api/sessions/" + encodeURIComponent(sessionId) + "/interrupt"' in runtime
    assert "async function interruptActiveSession(mode)" in runtime
    assert "function sessionIsInterruptible(session)" in runtime
    assert 'bindIfPresent("sendBtn", "click", (event) =>' in runtime
    assert 'eventName === "interrupted"' in runtime
    assert 'await refreshSessionEvents(sessionId);' in runtime
    assert "_add_post(\"/vizo/console/api/sessions/{session_id}/interrupt\"" in confirm_server


def test_dialogue_has_pending_message_queue_controls():
    html = TEMPLATE.read_text(encoding="utf-8")
    runtime = RUNTIME.read_text(encoding="utf-8")
    confirm_server = CONFIRM_SERVER.read_text(encoding="utf-8")
    assert 'id="composerQueue"' in html
    assert ".composer-queue-item" in html
    assert "function renderComposerQueue()" in runtime
    assert "function sendPendingInputNow(queueId)" in runtime
    assert "function deletePendingInput(queueId)" in runtime
    assert "async function refreshSessionEvents(sessionId)" in runtime
    assert 'data-pending-send-now="' in runtime
    assert 'data-pending-delete="' in runtime
    assert "composerHasSendableContent(input)" in runtime
    assert "发送到队列" in runtime
    assert "/pending/\" + encodeURIComponent(queueId) + \"/send-now" in runtime
    assert "await refreshSessionEvents(session.id)" in runtime
    assert "_add_delete(\"/vizo/console/api/sessions/{session_id}/pending/{queue_id}\"" in confirm_server
    assert "_add_post(\"/vizo/console/api/sessions/{session_id}/pending/{queue_id}/send-now\"" in confirm_server


def test_dialogue_hides_internal_thinking_noise_and_avoids_duplicate_thinking_labels():
    runtime = RUNTIME.read_text(encoding="utf-8")
    live_panel = LIVE_PANEL.read_text(encoding="utf-8")
    assert "compact.length <= 1200" in runtime
    assert "function isCodexProgressNarration(raw, payload, text)" in runtime
    assert 'mappedEventName = "runtime_status";' in runtime
    assert 'if (payload.status_kind === "commentary") return null;' in runtime
    assert '"work_progress"' in runtime
    assert 'eventName === "work_progress"' in runtime
    assert 'kind = "work";' in runtime
    assert "segments: []" in live_panel
    assert 'renderAssistantProcessSummary(!!live)' in live_panel
    assert "function isAssistantProcessExpanded(processKey, live, steps)" in live_panel
    assert 'item.kind === "work"' in live_panel
    assert 'return item.kind === "tool";' in live_panel
    assert 'if (step.kind === "work") return step.badge || "工作";' in live_panel
    assert "state.timelineAutoCollapsedProcessCards.add(processKey)" in live_panel
    assert "return !!live;" in live_panel
    assert "执行过程" in live_panel
    assert "Vizo 正在整理执行动作" in live_panel
    assert "Vizo 正在整理思考与工具调用" not in live_panel
    assert 'const showKindLabel = !(step && (step.kind === "thinking" || step.kind === "progress"));' in live_panel
    assert "(showKindLabel ? ('<span class=\"ev-process-step-kind\">'" in live_panel
    assert "completedAssistantProcessCards: new Set()" in runtime
    assert "timelineAutoCollapsedProcessCards: new Set()" in runtime
    assert "state.timelineAutoCollapsedProcessCards.delete(cardKey);" in runtime
    assert "state.timelineCardExpansion.clear();" not in runtime


def test_dialogue_live_panel_layout_prevents_step_card_squashing():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert ".task-monitor-shell{height:100%;min-height:0;overflow:hidden;display:flex;flex-direction:column;" in html
    assert ".task-monitor-header{padding:20px 18px 16px;border-bottom:1px solid var(--border);display:flex;flex-direction:column;gap:16px;flex-shrink:0;" in html
    assert ".task-monitor-stream{flex:1 1 auto;min-height:0;overflow:auto;padding:14px;display:flex;flex-direction:column;gap:14px}" in html
    assert ".task-monitor-step-card,.task-monitor-subtask,.task-monitor-cluster{border:1px solid var(--border);border-radius:16px;background:rgba(255,255,255,.03);overflow:hidden;flex:0 0 auto}" in html
    assert ".task-monitor-step-card{min-height:64px}" in html
    assert ".task-monitor-reminder{margin:0 14px 14px;padding:14px;border-radius:16px;border:1px solid rgba(255,255,255,.08);background:rgba(255,255,255,.03);display:flex;flex-direction:column;gap:12px;box-shadow:0 10px 24px rgba(0,0,0,.16);flex:0 0 auto;max-height:min(260px,34vh);overflow:auto}" in html


def test_dialogue_live_panel_logs_wrap_and_step_details_animate_both_directions():
    html = TEMPLATE.read_text(encoding="utf-8")
    live_panel = LIVE_PANEL.read_text(encoding="utf-8")
    assert ".task-monitor-log-box{display:flex;flex-direction:column;gap:6px;max-height:220px;overflow-y:auto;overflow-x:hidden;" in html
    assert ".task-monitor-log-text{white-space:pre-wrap;color:var(--code-text);min-width:0;max-width:100%;flex:1;overflow-wrap:anywhere;word-break:break-word}" in html
    assert ".task-monitor-collapse.is-opening{animation:task-monitor-expand-down" in html
    assert ".task-monitor-collapse.is-collapsing{pointer-events:none;animation:task-monitor-collapse-up" in html
    assert "@keyframes task-monitor-expand-down{from{grid-template-rows:0fr;opacity:0}to{grid-template-rows:1fr;opacity:1}}" in html
    assert "@keyframes task-monitor-collapse-up{from{grid-template-rows:1fr;opacity:1}to{grid-template-rows:0fr;opacity:0}}" in html
    assert "function markLivePanelCardOpening(cardKey)" in live_panel
    assert "function markLivePanelCardCollapsing(cardKey)" in live_panel
    assert "renderLivePanelCollapse(cacheKey, expanded" in live_panel
    assert "renderLivePanelCollapse(" in live_panel


def test_dialogue_live_panel_prefers_local_task_output_url_over_legacy_preview():
    live_panel = LIVE_PANEL.read_text(encoding="utf-8")
    assert 'const outputUrl = String(item.output_url || "").trim();' in live_panel
    assert "const resolvedPreviewUrl = outputUrl || previewUrl;" in live_panel
    assert 'data-output-url="\' + escapeHtml(resolvedPreviewUrl) + \'"' in live_panel
    assert 'pending.output_url || pending.preview_url' in live_panel


def test_dialogue_task_output_preview_is_clean_scrollable_and_resizable():
    html = TEMPLATE.read_text(encoding="utf-8")
    runtime = RUNTIME.read_text(encoding="utf-8")
    confirm_server = CONFIRM_SERVER.read_text(encoding="utf-8")
    assert ".preview-panel.open{width:var(--preview-panel-w,400px);min-width:260px;" in html
    assert ".preview-panel.resizing{transition:none}" in html
    assert 'id="openPreviewBrowserBtn"' in html
    assert "open_in_new" in html
    assert ".preview-head .icon-btn{width:24px;height:24px;border-radius:6px;flex:0 0 auto}" in html
    assert ".preview-head .material-symbols-outlined{font-size:16px;line-height:1}" in html
    assert '<span class="material-symbols-outlined" aria-hidden="true">close</span>' in html
    assert ".preview-body{flex:1;overflow:auto;" in html
    assert "scrollbar-width:thin;scrollbar-color:var(--scrollbar-thumb) transparent" in html
    assert ".preview-body::-webkit-scrollbar-thumb{background:var(--scrollbar-thumb);border-radius:4px}" in html
    assert ".preview-document{max-width:760px;width:100%;margin:0 auto;" in html
    assert ".preview-document-loading{height:100%;display:flex;" in html
    assert "body.preview-resizing{cursor:col-resize;user-select:none}" in html
    assert 'const PREVIEW_PANEL_WIDTH_KEY = "vizo.dialogue.previewPanelWidth";' in runtime
    assert "function beginPreviewPanelResize(event)" in runtime
    assert "function cleanupPreviewPanelResize()" in runtime
    assert "function openPreviewInBrowser()" in runtime
    assert 'bindIfPresent("openPreviewBrowserBtn", "click", openPreviewInBrowser);' in runtime
    assert 'bindIfPresent("splitHandle", "pointerdown", beginPreviewPanelResize);' in runtime
    assert "openBrowserBtn.hidden = !state.preview.url;" in runtime
    assert 'document.querySelector(".preview-frame")' in runtime
    assert 'frame.getAttribute("src") || frame.src' in runtime
    assert 'window.open(url, "_blank", "noopener,noreferrer")' in runtime
    assert 'state.preview.kind === "task_output"' in runtime
    assert '? ""' in runtime
    assert "if (previewLang) previewLang.hidden = !previewLangText;" in runtime
    assert '? "artifact"' not in runtime
    assert "function renderInlineTaskOutputPreview(preview, token)" in runtime
    assert 'fetch(url, { credentials: "same-origin" })' in runtime
    assert 'class="preview-document"' in runtime
    assert 'sanitizePreviewDocumentHtml(text)' in runtime
    assert "function renderPreviewUrlFallback(target, preview)" in runtime
    assert 'document.body.classList.remove("preview-resizing");' in runtime
    assert 'window.addEventListener("blur", finish);' in runtime
    assert 'document.addEventListener("mouseup", finish);' in runtime
    assert '<div class="preview-content preview-content-embedded">' in runtime
    assert '<iframe class="preview-frame" src="' in runtime
    assert '<strong class="preview-title">\' + escapeHtml(state.preview.title' not in runtime
    assert '<div class="preview-meta">任务产出预览' not in runtime
    assert "scrollbar-color:rgba(123,224,255,.74) transparent" in confirm_server
    assert "::-webkit-scrollbar-thumb{{background:rgba(123,224,255,.74);border-radius:4px}}" in confirm_server


def test_dialogue_confirm_success_clears_local_pending_confirm_state():
    live_panel = LIVE_PANEL.read_text(encoding="utf-8")
    assert "function markConfirmResolvedLocally(taskId, requestId)" in live_panel
    assert "delete task.pending_confirm;" in live_panel
    assert 'task.status = "running";' in live_panel
    assert "markConfirmResolvedLocally(taskId, requestId);" in live_panel
    assert "if (typeof renderTaskSurfaces === \"function\") renderTaskSurfaces();" in live_panel
