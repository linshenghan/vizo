    const ROUTE_BASE = "/vizo/console";
    const IS_MOBILE_MODE = document.body.dataset.mode === "mobile";
    const STORAGE_KEY = "vizo.dialogue.session";
    const COMPOSER_DRAFT_KEY_PREFIX = "vizo.dialogue.composerDraft.";
    const LIVE_PANEL_VISIBILITY_KEY = "vizo.dialogue.livePanelVisible";
    const PREVIEW_PANEL_WIDTH_KEY = "vizo.dialogue.previewPanelWidth";
    const MAX_TIMELINE_EVENTS = 5000;
    const SESSION_EVENTS_PAGE_LIMIT = 400;
    const CREATE_PROVIDER_OPTIONS = [
      { value: "claude_code", label: "Claude code", description: "使用 Claude Code CLI 作为执行底座" },
      { value: "codex", label: "Codex CLI", description: "使用 Codex CLI 作为执行底座" }
    ];
    const CREATE_MODEL_OPTIONS = {
      claude_code: [
        { value: "opus", label: "Claude Opus 4.7", description: "最强推理，适合复杂研发任务" },
        { value: "sonnet", label: "Claude Sonnet 4.7", description: "速度与质量均衡，适合日常开发" },
        { value: "haiku", label: "Claude Haiku 4.7", description: "响应更快，适合轻量任务与试跑" }
      ],
      codex: [
        { value: "gpt-5.5", label: "gpt-5.5", description: "最高质量推理，适合复杂编码与规划" },
        { value: "gpt-5.4", label: "gpt-5.4", description: "高质量推理，适合复杂编码与规划" },
        { value: "gpt-5.4-mini", label: "gpt-5.4-mini", description: "更低成本，适合快速试跑与验证" },
        { value: "gpt-5.3-codex", label: "gpt-5.3-codex", description: "Codex 专用模型，适合日常编码" },
        { value: "gpt-5.3-codex-spark", label: "gpt-5.3-codex-spark", description: "快速模型，适合轻量任务与验证" },
        { value: "gpt-5.2", label: "gpt-5.2", description: "兼容模型，适合保守回退" }
      ]
    };
    const CREATE_PROJECTS_BASE = "/opt/vizo-next/projects";
    const MAINLINE_PROJECT_NAME = "vizo";
    const TASK_ACTIONS = {
      running: [
        { action: "pause", label: "暂停", cls: "warn" },
        { action: "terminate", label: "终止", cls: "danger" }
      ],
      waiting_confirm: [
        { action: "pause", label: "暂停", cls: "warn" },
        { action: "terminate", label: "终止", cls: "danger" }
      ],
      paused: [
        { action: "resume", label: "恢复", cls: "" },
        { action: "terminate", label: "终止", cls: "danger" }
      ],
      failed: [
        { action: "resume", label: "继续执行", cls: "" },
        { action: "terminate", label: "终止", cls: "danger" }
      ]
    };
    const CONFIRM_BUTTONS = {
      generic_confirm: [
        { action: "y", label: "确认", cls: "" },
        { action: "n", label: "取消", cls: "" },
        { action: "f", label: "补充意见", cls: "warn" }
      ],
      rollback_review: [
        { action: "y", label: "继续回滚", cls: "" },
        { action: "d", label: "继续讨论", cls: "" },
        { action: "f", label: "补充意见", cls: "warn" }
      ],
      deploy_confirm: [
        { action: "y", label: "继续部署", cls: "" },
        { action: "n", label: "暂不部署", cls: "" },
        { action: "f", label: "补充意见", cls: "warn" }
      ],
      agent_exception: [
        { action: "y", label: "继续执行", cls: "" },
        { action: "terminate", label: "终止任务", cls: "danger" },
        { action: "f", label: "补充意见", cls: "warn" }
      ]
    };
    const TIMELINE_VISIBLE_EVENT_NAMES = new Set([
      "user_text",
      "assistant_text",
      "assistant_image",
      "thinking",
      "interaction_requested",
      "runtime_status",
      "work_progress",
      "tool_call",
      "tool_result",
      "runtime_error",
      "error",
      "code_edit",
      "interrupt_requested",
      "interrupted"
    ]);
    const INTERNAL_THINKING_PATTERNS = [
      /\bthe user said\b/i,
      /\blet me\b/i,
      /\bi should\b/i,
      /\bi need to\b/i,
      /\bi will\b/i,
      /\bi'll\b/i,
      /\bcheck my memory\b/i,
      /\brespond appropriately\b/i
    ];
    const DIALOGUE_FILE_EXTENSIONS = new Set([
      "c", "cc", "conf", "cpp", "cs", "css", "csv", "dockerfile", "env", "gif", "go", "h", "hpp", "html",
      "ico", "ini", "java", "jpeg", "jpg", "js", "json", "jsonl", "jsx",
      "log", "lua", "md", "mjs", "pdf", "png", "py", "rb", "rs", "scss", "sh", "sql", "svg", "swift", "toml",
      "ts", "tsx", "txt", "vue", "webp", "xml", "yaml", "yml"
    ]);
    const DIALOGUE_SPECIAL_FILE_NAMES = new Set([
      ".env", ".gitignore", "agents.md", "dockerfile", "license", "makefile", "readme", "readme.md"
    ]);

    function readStoredBoolean(key, fallbackValue) {
      try {
        const raw = localStorage.getItem(key);
        if (raw === "true") return true;
        if (raw === "false") return false;
      } catch (error) {}
      return fallbackValue;
    }

    const state = {
      leftTab: "sessions",
      mobileTab: "dialogue",
      mobileDialogueView: "stream",
      sessions: [],
      activeSessionId: localStorage.getItem(STORAGE_KEY) || "",
      activeSession: null,
      projects: [],
      connections: [],
      events: [],
      eventOrderCounter: 0,
      nextAfterSeq: 0,
      oldestEventSeq: 0,
      hasMoreSessionEventsBefore: false,
      loadingOlderSessionEvents: false,
      timelineRenderInitialized: false,
      timelineSeenRenderKeys: new Set(),
      timelineFreshRenderKeys: new Set(),
      timelineFreshCleanupHandle: null,
      timelineRenderFrame: null,
      timelineFollowBottomFrame: null,
      timelineFollowBottomUntil: 0,
      timelineStickToBottom: true,
      ws: null,
      reconnectHandle: null,
      reconnectDelay: 1200,
      currentProjectName: "",
      fileBrowserPath: "",
      fileEntries: [],
      fileTreeCache: new Map(),
      expandedDirs: new Set(),
      timelineCardExpansion: new Map(),
      completedAssistantProcessCards: new Set(),
      timelineAutoCollapsedProcessCards: new Set(),
      expandedTimelineBodies: new Set(),
      preview: null,
      sessionImages: [],
      imageViewer: null,
      imageViewerTransform: null,
      currentTask: null,
      taskDetail: null,
      tasks: [],
      selectedLiveNode: null,
      manualLiveSelection: false,
      expandedLiveSteps: new Map(),
      expandedLiveSubtasks: new Set(),
      liveLogs: new Map(),
      subTasks: new Map(),
      confirmDraftsByRequestId: new Map(),
      confirmEditorsByRequestId: new Set(),
      liveTaskSwitcherOpen: false,
      taskRefreshHandle: null,
      taskRefreshPromise: null,
      taskRefreshPromiseGeneration: null,
      taskRefreshGeneration: 0,
      rawLogContent: "",
      rawLogOffset: 0,
      rawLogHandle: null,
      sheetKind: "",
      livePanelVisible: readStoredBoolean(LIVE_PANEL_VISIBILITY_KEY, true),
      createRuntimeFamily: "",
      createConnectionId: "",
      projectsRefreshPromise: null,
      connectionsRefreshPromise: null,
      embeddedView: "",
      embeddedSection: "",
      embeddedFrameReady: false,
      composerAttachments: [],
      interruptingSessions: new Map(),
      interruptRenderHandle: null
    };
    let livePanel = null;
    let taskActionModalResolver = null;
    let taskActionModalOptions = [];
    const dialogueDropdowns = new Map();
    let activeDialogueDropdownId = "";
    let dialogueDropdownsBound = false;

    function $(id) {
      return document.getElementById(id);
    }

    function setText(id, value) {
      const node = $(id);
      if (node) node.textContent = value;
    }

    function setHtml(id, value) {
      const node = $(id);
      if (node) node.innerHTML = value;
    }

    function bindIfPresent(id, eventName, handler) {
      const node = $(id);
      if (node) node.addEventListener(eventName, handler);
    }

    function clampPreviewPanelWidth(width) {
      const raw = Number(width || 0);
      const workspace = $("workspaceShell");
      const workspaceWidth = workspace ? workspace.getBoundingClientRect().width : window.innerWidth;
      const maxWidth = Math.max(320, Math.min(760, Math.floor((workspaceWidth || window.innerWidth || 1000) * 0.72)));
      return Math.max(260, Math.min(maxWidth, Number.isFinite(raw) && raw > 0 ? raw : 400));
    }

    function readPreviewPanelWidth() {
      try {
        return clampPreviewPanelWidth(Number(localStorage.getItem(PREVIEW_PANEL_WIDTH_KEY) || 400));
      } catch (error) {
        return clampPreviewPanelWidth(400);
      }
    }

    function applyPreviewPanelWidth(width, options) {
      const panel = $("previewPanel");
      const resolved = clampPreviewPanelWidth(width);
      if (panel) panel.style.setProperty("--preview-panel-w", resolved + "px");
      if (options && options.persist) {
        try {
          localStorage.setItem(PREVIEW_PANEL_WIDTH_KEY, String(Math.round(resolved)));
        } catch (error) {}
      }
      return resolved;
    }

    function cleanupPreviewPanelResize() {
      const resizeState = state.previewResizeState;
      if (resizeState && resizeState.onMove) {
        window.removeEventListener("pointermove", resizeState.onMove);
        window.removeEventListener("pointerup", resizeState.finish);
        window.removeEventListener("pointercancel", resizeState.finish);
        window.removeEventListener("blur", resizeState.finish);
        document.removeEventListener("mouseup", resizeState.finish);
      }
      state.previewResizeState = null;
      const panel = $("previewPanel");
      const handle = $("splitHandle");
      if (panel) panel.classList.remove("resizing");
      if (handle) handle.classList.remove("resizing");
      document.body.classList.remove("preview-resizing");
    }

    function beginPreviewPanelResize(event) {
      const panel = $("previewPanel");
      const handle = $("splitHandle");
      if (!panel || !handle || !panel.classList.contains("open")) return;
      event.preventDefault();
      cleanupPreviewPanelResize();
      const startX = Number(event.clientX || 0);
      const startWidth = panel.getBoundingClientRect().width || readPreviewPanelWidth();
      panel.classList.add("resizing");
      handle.classList.add("resizing");
      document.body.classList.add("preview-resizing");
      if (handle.setPointerCapture && event.pointerId !== undefined) {
        try {
          handle.setPointerCapture(event.pointerId);
        } catch (error) {}
      }
      const onMove = (moveEvent) => {
        if (moveEvent && typeof moveEvent.preventDefault === "function") moveEvent.preventDefault();
        applyPreviewPanelWidth(startWidth + Number(moveEvent.clientX || 0) - startX);
      };
      const finish = (upEvent) => {
        if (handle.releasePointerCapture && upEvent && upEvent.pointerId !== undefined) {
          try {
            handle.releasePointerCapture(upEvent.pointerId);
          } catch (error) {}
        }
        applyPreviewPanelWidth(panel.getBoundingClientRect().width, { persist: true });
        cleanupPreviewPanelResize();
      };
      state.previewResizeState = { onMove, finish };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", finish);
      window.addEventListener("pointercancel", finish);
      window.addEventListener("blur", finish);
      document.addEventListener("mouseup", finish);
    }

    function openPreviewInBrowser() {
      const frame = document.querySelector(".preview-frame");
      const inlined = document.querySelector("[data-preview-source-url]");
      const url = state.preview && state.preview.url
        ? String(state.preview.url)
        : (
            inlined
              ? String(inlined.getAttribute("data-preview-source-url") || "")
              : (frame ? String(frame.getAttribute("src") || frame.src || "") : "")
          );
      if (!url) {
        toast("当前预览没有可打开的浏览器地址。", "error");
        return;
      }
      const opened = window.open(url, "_blank", "noopener,noreferrer");
      if (!opened) toast("浏览器阻止了新标签页打开，请检查弹窗设置。", "error");
    }

    function isSameOriginPreviewUrl(url) {
      try {
        return new URL(String(url || ""), window.location.origin).origin === window.location.origin;
      } catch (error) {
        return false;
      }
    }

    function sanitizePreviewDocumentHtml(htmlText) {
      const parser = new DOMParser();
      const doc = parser.parseFromString(String(htmlText || ""), "text/html");
      const root = doc.body || doc.documentElement;
      root.querySelectorAll("script,style,link,iframe,object,embed").forEach((node) => node.remove());
      root.querySelectorAll("*").forEach((node) => {
        Array.from(node.attributes || []).forEach((attr) => {
          const name = String(attr.name || "").toLowerCase();
          const value = String(attr.value || "");
          if (name.startsWith("on")) node.removeAttribute(attr.name);
          if ((name === "href" || name === "src") && /^\s*javascript:/i.test(value)) {
            node.removeAttribute(attr.name);
          }
        });
        if (node.tagName === "A") {
          node.setAttribute("target", "_blank");
          node.setAttribute("rel", "noreferrer");
        }
      });
      return root.innerHTML || "";
    }

    function renderPreviewUrlFallback(target, preview) {
      const url = String((preview && preview.url) || "");
      target.innerHTML =
        '<div class="preview-content preview-content-embedded">' +
          '<iframe class="preview-frame" src="' + escapeHtml(url) + '" title="' + escapeHtml((preview && (preview.title || preview.name)) || "任务产出预览") + '" loading="lazy"></iframe>' +
        "</div>";
    }

    function renderInlineTaskOutputPreview(preview, token) {
      const target = $("previewPane");
      if (!target || !preview || !preview.url) return;
      const url = String(preview.url);
      target.innerHTML =
        '<div class="preview-document-loading" data-preview-source-url="' + escapeHtml(url) + '">正在加载文档...</div>';
      fetch(url, { credentials: "same-origin" }).then(async (response) => {
        if (!response.ok) throw new Error("preview_fetch_failed_" + response.status);
        const contentType = String(response.headers.get("content-type") || "").toLowerCase();
        const text = await response.text();
        if (state.previewRenderToken !== token) return;
        if (contentType.includes("html") || /^\s*<!doctype html/i.test(text) || /^\s*<html[\s>]/i.test(text)) {
          target.innerHTML =
            '<article class="preview-document" data-preview-source-url="' + escapeHtml(url) + '">' +
              sanitizePreviewDocumentHtml(text) +
            "</article>";
          return;
        }
        target.innerHTML =
          '<article class="preview-document" data-preview-source-url="' + escapeHtml(url) + '">' +
            '<pre>' + escapeHtml(text) + "</pre>" +
          "</article>";
      }).catch(() => {
        if (state.previewRenderToken !== token) return;
        renderPreviewUrlFallback(target, preview);
      });
    }

    function escapeHtml(value) {
      return String(value || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }

    function formatFileSize(bytes) {
      const size = Number(bytes || 0);
      if (!Number.isFinite(size) || size <= 0) return "0 B";
      const units = ["B", "KB", "MB", "GB"];
      let value = size;
      let unitIndex = 0;
      while (value >= 1024 && unitIndex < units.length - 1) {
        value = value / 1024;
        unitIndex += 1;
      }
      return (unitIndex === 0 ? String(Math.round(value)) : value.toFixed(value >= 10 ? 1 : 2)) + " " + units[unitIndex];
    }

    function fileLooksTextual(file) {
      const name = String((file && file.name) || "").toLowerCase();
      const type = String((file && file.type) || "").toLowerCase();
      if (type.startsWith("text/")) return true;
      return /\.(md|txt|json|jsonl|yaml|yml|csv|log|py|js|ts|tsx|jsx|vue|css|scss|html|xml|sql|sh|toml|ini|env|dockerfile)$/i.test(name);
    }

    function attachmentPromptText(item) {
      const lines = [
        "文件：" + item.name,
        "大小：" + formatFileSize(item.size),
        item.type ? ("类型：" + item.type) : ""
      ].filter(Boolean);
      if (item.error) {
        lines.push("读取状态：" + item.error);
      } else if (item.content) {
        lines.push("内容" + (item.truncated ? "（已截断）" : "") + "：");
        lines.push("```");
        lines.push(item.content);
        lines.push("```");
      } else {
        lines.push("内容：未读取，可能是二进制文件。");
      }
      return lines.join("\n");
    }

    function composerAttachmentPayload() {
      return (state.composerAttachments || [])
        .filter((item) => item && item.uploadStatus === "uploaded" && item.path)
        .map((item) => ({
          id: item.attachmentId || item.id || "",
          name: item.name || "附件",
          size: Number(item.size || 0),
          type: item.type || "",
          path: item.path || "",
          uploaded_at: item.uploadedAt || ""
        }));
    }

    function htmlAttr(name, value) {
      if (value === null || value === undefined || value === "") return "";
      return " " + name + '="' + escapeHtml(value) + '"';
    }

    const MAX_ACTIVE_TOASTS = 4;
    const TOAST_TONE_META = {
      info: { badge: "提醒", icon: "notifications", duration: 8000, ariaLive: "polite" },
      success: { badge: "完成", icon: "check_circle", duration: 8000, ariaLive: "polite" },
      warning: { badge: "注意", icon: "warning", duration: 8000, ariaLive: "polite" },
      error: { badge: "异常", icon: "error", duration: 8000, ariaLive: "assertive" }
    };

    function dismissToast(node) {
      if (!node || node.dataset.toastClosing === "true") return;
      node.dataset.toastClosing = "true";
      if (node._timer) {
        window.clearTimeout(node._timer);
        node._timer = null;
      }
      node.classList.add("is-leaving");
      window.setTimeout(() => {
        if (node.parentNode) node.parentNode.removeChild(node);
      }, 170);
    }

    function enforceToastLimit(host) {
      if (!host) return;
      const activeNodes = Array.from(host.querySelectorAll(".vizo-toast"))
        .filter((node) => node.dataset.toastClosing !== "true");
      while (activeNodes.length > MAX_ACTIVE_TOASTS) {
        dismissToast(activeNodes.pop());
      }
    }

    function toast(message, tone) {
      const host = $("toastHost");
      if (!host) return;
      const resolvedTone = TOAST_TONE_META[tone] ? tone : "info";
      const meta = TOAST_TONE_META[resolvedTone];
      const node = document.createElement("article");
      node.className = "vizo-toast vizo-toast--" + resolvedTone;
      node.setAttribute("role", resolvedTone === "error" ? "alert" : "status");
      node.setAttribute("aria-live", meta.ariaLive);
      node.innerHTML =
        '<span class="vizo-toast__icon-shell" aria-hidden="true"><span class="material-symbols-outlined vizo-toast__icon">' + meta.icon + '</span></span>' +
        '<div class="vizo-toast__copy"><div class="vizo-toast__topline"><div class="vizo-toast__title">' + escapeHtml(String(message || "")) + '</div><span class="vizo-toast__badge">' + meta.badge + '</span></div></div>' +
        '<button class="vizo-toast__close" type="button" aria-label="关闭提醒"><span class="material-symbols-outlined" aria-hidden="true">close</span></button>';
      const closeButton = node.querySelector(".vizo-toast__close");
      if (closeButton) closeButton.addEventListener("click", () => dismissToast(node));
      host.prepend(node);
      enforceToastLimit(host);
      node._timer = window.setTimeout(() => dismissToast(node), meta.duration);
    }

    function buildWorkbenchInjectedRules() {
      const pageBg = "#06111d";
      return [
        "html,body{height:100%;overflow:hidden!important;background:" + pageBg + "!important;}",
        "#sidebar,.toolbar{display:none!important;}",
        "#input-area,.reconnect-overlay,#new-session-modal,#task-selector-modal,#create-project-modal,.md-preview-overlay,.md-preview-drawer{display:none!important;}",
        "#app,.main-area{height:100%!important;min-height:0!important;background:" + pageBg + "!important;}",
        ".main-area{width:100%!important;flex:1 1 auto!important;}",
        "#settings-view,#agents-view,#project-manager-view,#model-config-view,.terminal-container{height:100%!important;min-height:0!important;}"
      ];
    }

    async function api(path, options) {
      const response = await fetch(path, {
        credentials: "include",
        ...(options || {})
      });
      if (!response.ok) {
        let payload = null;
        try {
          payload = await response.json();
        } catch (error) {}
        throw new Error((payload && (payload.message || payload.error)) || "Request failed");
      }
      return response.json();
    }

    const MODEL_SETTING_SEPARATOR = "|";

    function encodeModelSettingValue(model, reasoningEffort) {
      return String(model || "").trim() + MODEL_SETTING_SEPARATOR + String(reasoningEffort || "").trim();
    }

    function decodeModelSettingValue(value) {
      const text = String(value || "");
      const index = text.indexOf(MODEL_SETTING_SEPARATOR);
      if (index < 0) return { model: text.trim(), reasoningEffort: "" };
      return {
        model: text.slice(0, index).trim(),
        reasoningEffort: text.slice(index + MODEL_SETTING_SEPARATOR.length).trim()
      };
    }

    function isModelSettingsCascaderSelect(select) {
      return !!select && select.dataset && select.dataset.cascader === "model-settings";
    }

    function ensureDialogueDropdown(select) {
      if (!select) return null;
      const root = select.closest(".vizo-select");
      if (!root) return null;
      const selectId = select.id || root.dataset.selectId || "";
      if (!selectId) return null;
      root.dataset.selectId = selectId;
      let entry = dialogueDropdowns.get(selectId);
      if (entry) return entry;
      const trigger = document.createElement("button");
      trigger.type = "button";
      trigger.className = "vizo-select-trigger";
      trigger.setAttribute("aria-haspopup", "listbox");
      trigger.setAttribute("aria-expanded", "false");
      trigger.setAttribute("aria-controls", selectId + "-dropdown-menu");
      trigger.innerHTML =
        '<span class="vizo-select-trigger-copy">' +
          '<span class="vizo-select-trigger-title"></span>' +
          '<span class="vizo-select-trigger-desc"></span>' +
        "</span>" +
        '<span class="vizo-select-trigger-caret" aria-hidden="true">' +
          '<img class="vizo-select-caret-icon" src="/vizo/static/icons/material-design/outlined/keyboard_arrow_down.svg" alt="" aria-hidden="true">' +
        "</span>";
      const menu = document.createElement("div");
      menu.className = "vizo-select-menu";
      menu.id = selectId + "-dropdown-menu";
      menu.setAttribute("role", "listbox");
      menu.setAttribute("aria-label", select.getAttribute("title") || selectId);
      root.appendChild(trigger);
      root.appendChild(menu);
      entry = { selectId, select, root, trigger, menu };
      dialogueDropdowns.set(selectId, entry);
      trigger.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        toggleDialogueDropdown(selectId);
      });
      trigger.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " " || event.key === "ArrowDown") {
          event.preventDefault();
          openDialogueDropdown(selectId, { focusSelected: true });
          return;
        }
        if (event.key === "ArrowUp") {
          event.preventDefault();
          openDialogueDropdown(selectId, { focusLast: true });
          return;
        }
        if (event.key === "Escape") {
          event.preventDefault();
          closeDialogueDropdown(selectId);
        }
      });
      menu.addEventListener("click", (event) => {
        if (isModelSettingsCascaderSelect(select)) {
          handleModelSettingsCascaderClick(entry, event);
          return;
        }
        const optionButton = event.target.closest("[data-dropdown-value]");
        if (!optionButton || optionButton.disabled) return;
        const nextValue = optionButton.getAttribute("data-dropdown-value") || "";
        if (select.value !== nextValue) {
          select.value = nextValue;
          select.dispatchEvent(new Event("change", { bubbles: true }));
        }
        syncDialogueDropdown(selectId);
        closeDialogueDropdown(selectId, { restoreFocus: true });
      });
      menu.addEventListener("mouseover", (event) => {
        if (!isModelSettingsCascaderSelect(select)) return;
        handleModelSettingsCascaderHover(entry, event);
      });
      menu.addEventListener("keydown", (event) => {
        if (isModelSettingsCascaderSelect(select)) {
          handleModelSettingsCascaderKeydown(entry, event);
          return;
        }
        const options = Array.from(menu.querySelectorAll("[data-dropdown-value]:not([disabled])"));
        if (!options.length) {
          if (event.key === "Escape") {
            event.preventDefault();
            closeDialogueDropdown(selectId, { restoreFocus: true });
          }
          return;
        }
        const currentIndex = options.indexOf(document.activeElement);
        if (event.key === "Escape") {
          event.preventDefault();
          closeDialogueDropdown(selectId, { restoreFocus: true });
          return;
        }
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          const step = event.key === "ArrowDown" ? 1 : -1;
          const baseIndex = currentIndex >= 0 ? currentIndex : (step > 0 ? -1 : 0);
          const nextIndex = (baseIndex + step + options.length) % options.length;
          options[nextIndex].focus();
          return;
        }
        if (event.key === "Home") {
          event.preventDefault();
          options[0].focus();
          return;
        }
        if (event.key === "End") {
          event.preventDefault();
          options[options.length - 1].focus();
        }
      });
      select.addEventListener("change", () => syncDialogueDropdown(selectId));
      return entry;
    }

    function dropdownOptionMeta(option) {
      if (!option) {
        return { value: "", label: "暂无可选项", description: "", badge: "", disabled: true };
      }
      return {
        value: String(option.value || ""),
        label: String(option.textContent || option.label || "").trim() || "未命名选项",
        description: String(option.getAttribute("data-description") || option.getAttribute("data-meta") || "").trim(),
        badge: String(option.getAttribute("data-badge") || "").trim(),
        disabled: !!option.disabled
      };
    }

    function syncDialogueDropdown(target) {
      const selectId = typeof target === "string" ? target : String(target && target.id || "");
      if (!selectId) return;
      const entry = ensureDialogueDropdown(typeof target === "string" ? $(target) : target);
      if (!entry) return;
      const options = Array.from(entry.select.options || []);
      const selectedOption = entry.select.selectedOptions && entry.select.selectedOptions[0]
        ? entry.select.selectedOptions[0]
        : (options.find((option) => option.value === entry.select.value) || options[0] || null);
      const selectedMeta = dropdownOptionMeta(selectedOption);
      const titleNode = entry.trigger.querySelector(".vizo-select-trigger-title");
      const descNode = entry.trigger.querySelector(".vizo-select-trigger-desc");
      if (titleNode) titleNode.textContent = selectedMeta.label;
      if (descNode) descNode.textContent = selectedMeta.description;
      entry.trigger.disabled = !!entry.select.disabled;
      entry.trigger.setAttribute("aria-expanded", activeDialogueDropdownId === selectId ? "true" : "false");
      entry.menu.classList.toggle("vizo-select-cascader-menu", isModelSettingsCascaderSelect(entry.select));
      if (!options.length) {
        entry.menu.innerHTML = '<div class="vizo-select-empty">当前还没有可选项。</div>';
      } else if (isModelSettingsCascaderSelect(entry.select)) {
        renderModelSettingsCascaderMenu(entry, selectedOption);
      } else {
        entry.menu.innerHTML = options.map((option) => {
          const meta = dropdownOptionMeta(option);
          return (
            '<button class="vizo-select-option" type="button" role="option"' +
              htmlAttr("data-dropdown-value", meta.value) +
              htmlAttr("aria-selected", option === selectedOption ? "true" : "false") +
              (meta.disabled ? " disabled" : "") +
            ">" +
              '<span class="vizo-select-option-state" aria-hidden="true"></span>' +
              '<span class="vizo-select-option-copy">' +
                '<span class="vizo-select-option-top">' +
                  (meta.badge ? ('<span class="vizo-select-option-badge">' + escapeHtml(meta.badge) + "</span>") : "") +
                  '<span class="vizo-select-option-title">' + escapeHtml(meta.label) + "</span>" +
                "</span>" +
                (meta.description ? ('<span class="vizo-select-option-desc">' + escapeHtml(meta.description) + "</span>") : "") +
              "</span>" +
            "</button>"
          );
        }).join("");
      }
      if (activeDialogueDropdownId === selectId) {
        if (entry.trigger.disabled) {
          closeDialogueDropdown(selectId);
          return;
        }
        positionDialogueDropdown(entry);
      } else {
        entry.root.classList.remove("open");
        entry.root.classList.remove("drop-up");
      }
    }

    function modelSettingOptionMeta(option) {
      const decoded = decodeModelSettingValue(option ? option.value : "");
      const model = String((option && option.getAttribute("data-model")) || decoded.model || "").trim();
      const effort = String((option && option.getAttribute("data-reasoning-effort")) || decoded.reasoningEffort || "").trim();
      return {
        value: String((option && option.value) || encodeModelSettingValue(model, effort)),
        model,
        effort,
        modelLabel: String((option && option.getAttribute("data-model-label")) || model || "未命名模型").trim(),
        modelDescription: String((option && option.getAttribute("data-model-description")) || "").trim(),
        effortLabel: String((option && option.getAttribute("data-effort-label")) || effort || "").trim(),
        effortDescription: String((option && option.getAttribute("data-effort-description")) || "").trim(),
        badge: String((option && option.getAttribute("data-badge")) || "").trim(),
        disabled: !!(option && option.disabled)
      };
    }

    function modelSettingOptionsFromSelect(select) {
      return Array.from((select && select.options) || []).map(modelSettingOptionMeta).filter((item) => item.model);
    }

    function uniqueModelSettingModels(items) {
      const seen = new Set();
      const models = [];
      (items || []).forEach((item) => {
        if (!item.model || seen.has(item.model)) return;
        seen.add(item.model);
        models.push(item);
      });
      return models;
    }

    function findModelSettingOption(items, model, effort, isCodex) {
      const enabled = (items || []).filter((item) => item.model === model && !item.disabled);
      if (!enabled.length) return null;
      if (!isCodex) return enabled.find((item) => !item.effort) || enabled[0];
      const normalizedEffort = String(effort || "");
      return enabled.find((item) => String(item.effort || "") === normalizedEffort) || enabled.find((item) => item.effort) || enabled[0];
    }

    function resetModelSettingsCascaderDraft(entry, selectedOption) {
      if (!entry || !isModelSettingsCascaderSelect(entry.select)) return;
      if (!selectedOption) {
        entry.modelSettingsDraft = { model: "", effort: "" };
        return;
      }
      const selected = modelSettingOptionMeta(selectedOption);
      entry.modelSettingsDraft = { model: selected.model, effort: selected.effort };
    }

    function previewModelSettingsCascaderModel(entry, nextModel, options) {
      if (!entry || entry.select.dataset.runtimeFamily !== "codex") return false;
      nextModel = String(nextModel || "").trim();
      if (!nextModel) return false;
      const selected = modelSettingOptionMeta(entry.select.selectedOptions && entry.select.selectedOptions[0] ? entry.select.selectedOptions[0] : entry.select.options[0]);
      const currentEffort = String((entry.modelSettingsDraft && entry.modelSettingsDraft.effort) || selected.effort || "").trim();
      const items = modelSettingOptionsFromSelect(entry.select);
      const nextItem = findModelSettingOption(items, nextModel, currentEffort, true);
      if (!nextItem) return false;
      const currentDraft = entry.modelSettingsDraft || {};
      if (currentDraft.model === nextItem.model && currentDraft.effort === nextItem.effort) return true;
      entry.modelSettingsDraft = {
        model: nextItem.model,
        effort: nextItem.effort
      };
      renderModelSettingsCascaderMenu(entry, entry.select.selectedOptions && entry.select.selectedOptions[0] ? entry.select.selectedOptions[0] : entry.select.options[0]);
      positionDialogueDropdown(entry);
      if (options && options.focusEffort) {
        const nextFocus = entry.menu.querySelector('[data-cascader-action="effort"][aria-selected="true"]')
          || entry.menu.querySelector('[data-cascader-action="effort"]:not([disabled])');
        if (nextFocus) nextFocus.focus();
      }
      return true;
    }

    function handleModelSettingsCascaderHover(entry, event) {
      const button = event.target.closest('[data-cascader-action="model"]');
      if (!button || button.disabled || !entry.menu.contains(button)) return;
      if (event.relatedTarget && button.contains(event.relatedTarget)) return;
      previewModelSettingsCascaderModel(entry, button.getAttribute("data-cascader-model"), { focusEffort: false });
    }

    function commitModelSettingSelection(entry, model, effort, options) {
      const items = modelSettingOptionsFromSelect(entry.select);
      const isCodex = entry.select.dataset.runtimeFamily === "codex";
      const nextItem = findModelSettingOption(items, model, isCodex ? effort : "", isCodex);
      if (!nextItem) return;
      const nextValue = nextItem.value || encodeModelSettingValue(nextItem.model, isCodex ? nextItem.effort : "");
      const option = Array.from(entry.select.options || []).find((item) => item.value === nextValue && !item.disabled);
      if (!option) return;
      if (entry.select.value !== nextValue) {
        entry.select.value = nextValue;
        entry.select.dispatchEvent(new Event("change", { bubbles: true }));
      }
      resetModelSettingsCascaderDraft(entry, option);
      syncDialogueDropdown(entry.select);
      if (options && options.close) {
        closeDialogueDropdown(entry.selectId, { restoreFocus: true });
      } else if (activeDialogueDropdownId === entry.selectId) {
        positionDialogueDropdown(entry);
      }
    }

    function renderModelSettingsCascaderMenu(entry, selectedOption) {
      const items = modelSettingOptionsFromSelect(entry.select);
      if (!items.length) {
        entry.menu.innerHTML = '<div class="vizo-select-empty">当前还没有可选项。</div>';
        return;
      }
      const selected = modelSettingOptionMeta(selectedOption || entry.select.options[0]);
      const isCodex = entry.select.dataset.runtimeFamily === "codex";
      const draft = isCodex && entry.modelSettingsDraft && entry.modelSettingsDraft.model ? entry.modelSettingsDraft : selected;
      const hasDraftModel = !!(isCodex && entry.modelSettingsDraft && entry.modelSettingsDraft.model);
      const activeItem = isCodex
        ? (hasDraftModel ? findModelSettingOption(items, draft.model, draft.effort, true) : null)
        : (findModelSettingOption(items, selected.model, selected.effort, false) || items.find((item) => !item.disabled) || items[0]);
      const activeModel = activeItem ? activeItem.model : "";
      const activeEffort = isCodex && activeItem ? String(activeItem.effort || "") : "";
      const effortItems = isCodex ? items.filter((item) => item.model === activeModel && item.effort) : [];
      const modelButtons = uniqueModelSettingModels(items).map((item) => {
        const selectedModel = isCodex ? (hasDraftModel && item.model === activeModel) : item.model === activeModel;
        const optionItem = findModelSettingOption(items, item.model, activeEffort, isCodex) || item;
        const optionValue = optionItem.value || encodeModelSettingValue(optionItem.model, isCodex ? optionItem.effort : "");
        return (
          '<button class="vizo-select-option" type="button" role="option"' +
            htmlAttr("data-dropdown-value", optionValue) +
            htmlAttr("data-cascader-action", "model") +
            htmlAttr("data-cascader-model", item.model) +
            htmlAttr("aria-selected", selectedModel ? "true" : "false") +
            (item.disabled ? " disabled" : "") +
          ">" +
            '<span class="vizo-select-option-state" aria-hidden="true"></span>' +
            '<span class="vizo-select-option-copy">' +
              '<span class="vizo-select-option-top">' +
                (item.badge ? ('<span class="vizo-select-option-badge">' + escapeHtml(item.badge) + "</span>") : "") +
                '<span class="vizo-select-option-title">' + escapeHtml(item.modelLabel) + "</span>" +
              "</span>" +
              (item.modelDescription ? ('<span class="vizo-select-option-desc">' + escapeHtml(item.modelDescription) + "</span>") : "") +
            "</span>" +
          "</button>"
        );
      }).join("");
      const effortButtons = effortItems.map((item) => {
        return (
          '<button class="vizo-select-option" type="button" role="option"' +
            htmlAttr("data-dropdown-value", encodeModelSettingValue(activeModel, item.effort)) +
            htmlAttr("data-cascader-action", "effort") +
            htmlAttr("data-cascader-effort", item.effort) +
            htmlAttr("aria-selected", "false") +
            (item.disabled ? " disabled" : "") +
          ">" +
            '<span class="vizo-select-option-state" aria-hidden="true"></span>' +
            '<span class="vizo-select-option-copy">' +
              '<span class="vizo-select-option-top">' +
                '<span class="vizo-select-option-title">' + escapeHtml(item.effortLabel || item.effort) + "</span>" +
              "</span>" +
              (item.effortDescription ? ('<span class="vizo-select-option-desc">' + escapeHtml(item.effortDescription) + "</span>") : "") +
            "</span>" +
          "</button>"
        );
      }).join("");
      entry.menu.innerHTML =
        '<div class="vizo-select-cascader' + (isCodex && hasDraftModel ? "" : " single") + '">' +
          '<div class="vizo-select-cascader-column">' +
            '<div class="vizo-select-cascader-heading">模型</div>' +
            modelButtons +
          "</div>" +
          (isCodex && hasDraftModel ? (
            '<div class="vizo-select-cascader-column">' +
              '<div class="vizo-select-cascader-heading">思考</div>' +
              (effortButtons || '<div class="vizo-select-empty">当前模型没有思考深度选项。</div>') +
            "</div>"
          ) : "") +
        "</div>";
    }

    function handleModelSettingsCascaderClick(entry, event) {
      const button = event.target.closest("[data-cascader-action]");
      if (!button || button.disabled) return;
      event.preventDefault();
      event.stopPropagation();
      const action = button.getAttribute("data-cascader-action") || "";
      const selected = modelSettingOptionMeta(entry.select.selectedOptions && entry.select.selectedOptions[0] ? entry.select.selectedOptions[0] : entry.select.options[0]);
      if (action === "model") {
        const nextModel = String(button.getAttribute("data-cascader-model") || "").trim();
        if (!nextModel) return;
        const isCodex = entry.select.dataset.runtimeFamily === "codex";
        if (!isCodex) {
          commitModelSettingSelection(entry, nextModel, "", { close: true });
          return;
        }
        previewModelSettingsCascaderModel(entry, nextModel, { focusEffort: true });
        return;
      }
      if (action === "effort") {
        const nextEffort = String(button.getAttribute("data-cascader-effort") || "").trim();
        const draftModel = String((entry.modelSettingsDraft && entry.modelSettingsDraft.model) || selected.model || "").trim();
        commitModelSettingSelection(entry, draftModel, nextEffort, { close: true });
        return;
      }
    }

    function handleModelSettingsCascaderKeydown(entry, event) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeDialogueDropdown(entry.selectId, { restoreFocus: true });
        return;
      }
      const focusable = Array.from(entry.menu.querySelectorAll("[data-cascader-action]:not([disabled])"));
      if (!focusable.length) return;
      const currentIndex = focusable.indexOf(document.activeElement);
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const step = event.key === "ArrowDown" ? 1 : -1;
        const baseIndex = currentIndex >= 0 ? currentIndex : (step > 0 ? -1 : 0);
        focusable[(baseIndex + step + focusable.length) % focusable.length].focus();
        return;
      }
      if (event.key === "Home") {
        event.preventDefault();
        focusable[0].focus();
        return;
      }
      if (event.key === "End") {
        event.preventDefault();
        focusable[focusable.length - 1].focus();
      }
    }

    function positionDialogueDropdown(entry) {
      if (!entry) return;
      const rootRect = entry.root.getBoundingClientRect();
      const minWidth = Number(entry.root.dataset.menuMinWidth || "0");
      const cascader = entry.menu.querySelector(".vizo-select-cascader");
      const panelWidths = String(entry.root.dataset.cascaderPanelWidths || "")
        .split(",")
        .map((item) => Number(String(item || "").trim()))
        .filter((item) => item > 0);
      let desiredWidth = Math.max(Math.round(rootRect.width), minWidth || 0);
      if (cascader && panelWidths.length) {
        const panelCount = entry.menu.querySelectorAll(".vizo-select-cascader-column").length || 1;
        const cascaderWidth = panelWidths.slice(0, panelCount).reduce((sum, width) => sum + width, 0)
          + Math.max(0, panelCount - 1) * 6;
        desiredWidth = Math.max(cascaderWidth, minWidth || 0);
      }
      entry.menu.style.width = desiredWidth > 0 ? desiredWidth + "px" : "";
      const viewportPadding = 12;
      const spaceBelow = Math.max(120, Math.floor(window.innerHeight - rootRect.bottom - viewportPadding));
      const spaceAbove = Math.max(120, Math.floor(rootRect.top - viewportPadding));
      const openUp = spaceBelow < 180 && spaceAbove > spaceBelow;
      entry.root.classList.toggle("drop-up", openUp);
      entry.menu.style.maxHeight = Math.min(openUp ? spaceAbove : spaceBelow, 280) + "px";
    }

    function focusDialogueDropdownOption(entry, preferLast) {
      if (!entry) return;
      const options = Array.from(entry.menu.querySelectorAll("[data-dropdown-value]:not([disabled])"));
      if (!options.length) return;
      const selected = options.find((node) => node.getAttribute("aria-selected") === "true");
      const target = preferLast ? (selected || options[options.length - 1]) : (selected || options[0]);
      target.focus();
    }

    function closeDialogueDropdown(target, options) {
      const selectId = typeof target === "string" && target ? target : activeDialogueDropdownId;
      if (!selectId) return;
      const entry = dialogueDropdowns.get(selectId);
      if (!entry) return;
      entry.root.classList.remove("open");
      entry.root.classList.remove("drop-up");
      entry.trigger.setAttribute("aria-expanded", "false");
      if (activeDialogueDropdownId === selectId) activeDialogueDropdownId = "";
      if (options && options.restoreFocus) entry.trigger.focus();
    }

    function openDialogueDropdown(selectId, options) {
      const entry = ensureDialogueDropdown($(selectId));
      if (!entry || entry.trigger.disabled) return;
      entry.select.dispatchEvent(new CustomEvent("vizo-select:beforeopen", { bubbles: true }));
      if (isModelSettingsCascaderSelect(entry.select)) {
        resetModelSettingsCascaderDraft(entry);
      }
      syncDialogueDropdown(selectId);
      if (activeDialogueDropdownId && activeDialogueDropdownId !== selectId) {
        closeDialogueDropdown(activeDialogueDropdownId);
      }
      activeDialogueDropdownId = selectId;
      positionDialogueDropdown(entry);
      entry.root.classList.add("open");
      entry.trigger.setAttribute("aria-expanded", "true");
      if (options && options.focusLast) {
        focusDialogueDropdownOption(entry, true);
      } else if (options && options.focusSelected) {
        focusDialogueDropdownOption(entry, false);
      }
    }

    function toggleDialogueDropdown(selectId) {
      if (activeDialogueDropdownId === selectId) {
        closeDialogueDropdown(selectId);
        return;
      }
      openDialogueDropdown(selectId);
    }

    function syncDialogueDropdowns() {
      document.querySelectorAll(".vizo-select select").forEach((select) => {
        ensureDialogueDropdown(select);
        syncDialogueDropdown(select);
      });
      if (dialogueDropdownsBound) return;
      dialogueDropdownsBound = true;
      document.addEventListener("click", (event) => {
        if (!activeDialogueDropdownId) return;
        const entry = dialogueDropdowns.get(activeDialogueDropdownId);
        if (!entry) return;
        if (!entry.root.contains(event.target)) closeDialogueDropdown(activeDialogueDropdownId);
      });
      document.addEventListener("keydown", (event) => {
        if (event.key !== "Escape" || !activeDialogueDropdownId) return;
        closeDialogueDropdown(activeDialogueDropdownId, { restoreFocus: true });
      });
      window.addEventListener("resize", () => closeDialogueDropdown(activeDialogueDropdownId));
      document.querySelectorAll("[data-select-target]").forEach((label) => {
        label.addEventListener("click", (event) => {
          event.preventDefault();
          event.stopPropagation();
          const selectId = label.getAttribute("data-select-target") || "";
          if (!selectId) return;
          openDialogueDropdown(selectId);
        });
      });
    }

    function basename(path) {
      const normalized = String(path || "").replace(/\/+$/g, "");
      return normalized.split("/").filter(Boolean).pop() || normalized || "-";
    }

    function normalizePath(path) {
      return String(path || "").replace(/^\/+|\/+$/g, "");
    }

    function joinPath(base, name) {
      const prefix = normalizePath(base);
      const tail = normalizePath(name);
      if (!prefix) return tail;
      if (!tail) return prefix;
      return prefix + "/" + tail;
    }

    function normalizeProjectRoot(path) {
      return String(path || "").replace(/\/+$/g, "");
    }

    function parseDialogueLocalPathToken(value) {
      let raw = String(value || "").trim();
      if (!raw) return null;
      raw = raw.replace(/^file:\/\//i, "");
      try {
        raw = decodeURIComponent(raw);
      } catch (error) {}
      const locationMatch = raw.match(/^(.+?)(?::(\d+)(?::(\d+))?)$/);
      const path = locationMatch ? locationMatch[1] : raw;
      return {
        raw: String(value || "").trim(),
        path: String(path || "").trim(),
        line: locationMatch ? Number(locationMatch[2] || 0) : 0,
        column: locationMatch ? Number(locationMatch[3] || 0) : 0
      };
    }

    function dialoguePathBaseName(path) {
      return basename(String(path || "").replace(/\\/g, "/"));
    }

    function dialoguePathLooksLikeFile(path) {
      const name = dialoguePathBaseName(path);
      if (!name) return false;
      if (DIALOGUE_SPECIAL_FILE_NAMES.has(name.toLowerCase())) return true;
      const dotIndex = name.lastIndexOf(".");
      if (dotIndex < 0) return false;
      return DIALOGUE_FILE_EXTENSIONS.has(name.slice(dotIndex + 1).toLowerCase());
    }

    function projectForAbsolutePath(path) {
      const cleanPath = normalizeProjectRoot(path);
      if (!cleanPath.startsWith("/")) return null;
      let match = null;
      state.projects.forEach((project) => {
        const root = normalizeProjectRoot(project && project.path);
        if (!root) return;
        if (cleanPath === root || cleanPath.startsWith(root + "/")) {
          if (!match || root.length > normalizeProjectRoot(match.path).length) match = project;
        }
      });
      return match;
    }

    function projectRelativePath(absolutePath, project) {
      const root = normalizeProjectRoot(project && project.path);
      const path = normalizeProjectRoot(absolutePath);
      if (!root || !path) return "";
      if (path === root) return "";
      if (!path.startsWith(root + "/")) return "";
      return normalizePath(path.slice(root.length + 1));
    }

    function isDialogueLocalPathCandidate(value) {
      const parsed = parseDialogueLocalPathToken(value);
      if (!parsed || !parsed.path) return false;
      const path = parsed.path.replace(/\\/g, "/");
      if (/^[a-z][a-z0-9+.-]*:\/\//i.test(path)) return false;
      if (path.startsWith("/")) {
        return !!projectForAbsolutePath(path) && dialoguePathLooksLikeFile(path);
      }
      if (path.startsWith("~/")) return false;
      if (path.startsWith("./") || path.startsWith("../")) return dialoguePathLooksLikeFile(path);
      if (path.includes("/")) return dialoguePathLooksLikeFile(path);
      return dialoguePathLooksLikeFile(path);
    }

    function resolveDialogueLocalFileTarget(value) {
      const parsed = parseDialogueLocalPathToken(value);
      if (!parsed || !parsed.path) return null;
      const path = parsed.path.replace(/\\/g, "/");
      let project = null;
      let relPath = "";
      if (path.startsWith("/")) {
        project = projectForAbsolutePath(path);
        relPath = projectRelativePath(path, project);
      } else {
        project = currentProject();
        relPath = normalizePath(path.replace(/^\.\//, ""));
      }
      if (!project || !relPath || !dialoguePathLooksLikeFile(relPath)) return null;
      return {
        project,
        path: relPath,
        line: parsed.line,
        column: parsed.column,
        displayPath: path
      };
    }

    function dialogueLocalPathExtension(path) {
      const name = dialoguePathBaseName(path);
      const dotIndex = name.lastIndexOf(".");
      return dotIndex >= 0 ? name.slice(dotIndex + 1).toLowerCase() : "";
    }

    function adjacentDialoguePathSuffix(element) {
      if (!element) return "";
      let node = element.nextSibling || null;
      let text = "";
      while (node && text.length < 16) {
        if (node.nodeType !== 3) break;
        text += node.nodeValue || "";
        node = node.nextSibling || null;
      }
      const match = text.match(/^[A-Za-z0-9]+/);
      return match ? match[0] : "";
    }

    function repairDialogueLocalPathToken(value, sourceElement) {
      const parsed = parseDialogueLocalPathToken(value);
      if (!parsed || !parsed.path || !sourceElement) return value;
      const ext = dialogueLocalPathExtension(parsed.path);
      if (!ext) return value;
      const adjacentSuffix = adjacentDialoguePathSuffix(sourceElement).toLowerCase();
      if (!adjacentSuffix) return value;
      const candidates = Array.from(DIALOGUE_FILE_EXTENSIONS)
        .filter((candidate) => (
          candidate.length > ext.length &&
          candidate.startsWith(ext) &&
          adjacentSuffix.startsWith(candidate.slice(ext.length))
        ))
        .sort((left, right) => right.length - left.length || left.localeCompare(right));
      const replacementExt = candidates[0] || "";
      if (!replacementExt) return value;
      const path = parsed.path.replace(/\\/g, "/");
      const repairedPath = path.slice(0, path.length - ext.length) + replacementExt;
      const locationSuffix = parsed.line
        ? (":" + String(parsed.line) + (parsed.column ? (":" + String(parsed.column)) : ""))
        : "";
      return repairedPath + locationSuffix;
    }

    function isoTime(value) {
      if (!value) return "";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return "";
      return date.toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit"
      });
    }

    function formatDuration(seconds) {
      const total = Math.max(0, Math.round(Number(seconds || 0)));
      const hour = Math.floor(total / 3600);
      const minute = Math.floor((total % 3600) / 60);
      const second = total % 60;
      if (hour > 0) return hour + "h " + String(minute).padStart(2, "0") + "m";
      if (minute > 0) return minute + "m " + String(second).padStart(2, "0") + "s";
      return second + "s";
    }

    function formatRelativeTime(value) {
      if (!value) return "";
      const target = new Date(value);
      if (Number.isNaN(target.getTime())) return "";
      const diffMs = Math.max(0, Date.now() - target.getTime());
      const minute = Math.floor(diffMs / 60000);
      const hour = Math.floor(diffMs / 3600000);
      const day = Math.floor(diffMs / 86400000);
      if (minute < 1) return "刚刚";
      if (minute < 60) return minute + "分钟";
      if (hour < 24) return hour + "小时";
      if (day < 7) return day + "天";
      return formatHistoryDate(value);
    }

    function formatHistoryDate(value) {
      if (!value) return "";
      const target = new Date(value);
      if (Number.isNaN(target.getTime())) return "";
      return (target.getMonth() + 1) + "月" + target.getDate() + "日";
    }

    function formatCurrency(value) {
      const num = Number(value || 0);
      if (!num) return "$0.00";
      return "$" + num.toFixed(2);
    }

    function parseTimeMs(value) {
      if (!value) return 0;
      const date = new Date(value);
      return Number.isNaN(date.getTime()) ? 0 : date.getTime();
    }

    function sumStepDurations(task) {
      return (task && task.steps ? task.steps : []).reduce((total, step) => {
        const seconds = Number(step && (step.duration_seconds || step.duration || 0));
        if (Number.isFinite(seconds) && seconds > 0) return total + seconds;
        const millis = Number(step && step.duration_ms);
        if (Number.isFinite(millis) && millis > 0) return total + (millis / 1000);
        return total;
      }, 0);
    }

    function isTaskPollingActive(task) {
      const status = String((task && task.status) || "").toLowerCase();
      return status === "running" || status === "waiting_confirm" || status === "paused";
    }

    function resolveDisplayTask() {
      return state.taskDetail || state.currentTask || null;
    }

    function resolveLiveSwitchState() {
      const liveTask = state.currentTask;
      const displayTask = resolveDisplayTask();
      if (!liveTask || !displayTask) return null;
      if (!liveTask.task_id || !displayTask.task_id) return null;
      if (liveTask.task_id === displayTask.task_id) return null;
      return { liveTask, displayTask };
    }

    function computeTaskElapsed(task) {
      if (!task) return 0;
      const startedAtMs = parseTimeMs(task.started_at || task.created_at || "");
      if (startedAtMs > 0 && isTaskPollingActive(task)) {
        return Math.max(0, (Date.now() - startedAtMs) / 1000);
      }
      const stepSeconds = sumStepDurations(task);
      if (stepSeconds > 0) return stepSeconds;
      const finishedAtMs = parseTimeMs(task.completed_at || task.finished_at || task.updated_at || "");
      if (startedAtMs > 0 && finishedAtMs > startedAtMs) {
        return Math.max(0, (finishedAtMs - startedAtMs) / 1000);
      }
      const directSeconds = Number(task.elapsed_seconds || 0);
      return Number.isFinite(directSeconds) && directSeconds > 0 ? directSeconds : 0;
    }

    function resolveActionTask(placement) {
      if (placement === "banner") return state.currentTask || resolveDisplayTask() || null;
      return resolveDisplayTask() || state.currentTask || null;
    }

    function resolveKnownTask(taskId, placement) {
      if (taskId) {
        const candidates = [state.taskDetail, state.currentTask, resolveDisplayTask()];
        for (const candidate of candidates) {
          if (candidate && candidate.task_id === taskId) return candidate;
        }
      }
      return resolveActionTask(placement);
    }

    function resolveTaskActionButtons(task) {
      const status = String((task && task.status) || "").toLowerCase();
      return TASK_ACTIONS[status] || [];
    }

    function resolveConfirmButtons(task) {
      const buttonSet = String((((task && task.pending_confirm) || {}).context || {}).button_set || "generic_confirm");
      return CONFIRM_BUTTONS[buttonSet] || CONFIRM_BUTTONS.generic_confirm;
    }

    function getConfirmDraft(requestId) {
      if (!requestId) return "";
      return state.confirmDraftsByRequestId.get(String(requestId)) || "";
    }

    function isConfirmEditorOpen(requestId) {
      if (!requestId) return false;
      return state.confirmEditorsByRequestId.has(String(requestId));
    }

    function openConfirmEditor(requestId) {
      const key = String(requestId || "");
      if (!key) return;
      state.confirmEditorsByRequestId.add(key);
    }

    function closeConfirmEditor(requestId, options) {
      const key = String(requestId || "");
      if (!key) return;
      state.confirmEditorsByRequestId.delete(key);
      if (options && options.clearDraft) clearConfirmDraft(key);
    }

    function setConfirmDraft(requestId, value) {
      const key = String(requestId || "");
      if (!key) return;
      const draft = String(value || "");
      if (draft.trim()) state.confirmDraftsByRequestId.set(key, draft);
      else state.confirmDraftsByRequestId.delete(key);
    }

    function clearConfirmDraft(requestId) {
      const key = String(requestId || "");
      if (!key) return;
      state.confirmDraftsByRequestId.delete(key);
    }

    function renderTaskControls(task, placement) {
      const taskId = String((task && task.task_id) || "");
      if (!taskId) return "";
      const buttons = resolveTaskActionButtons(task);
      if (!buttons.length) return "";
      return '<div class="task-control-group">' + buttons.map((button) => {
        return '<button class="btn ' + (button.cls || "") + '" type="button" data-task-action="' + escapeHtml(button.action) +
          '" data-task-id="' + escapeHtml(taskId) +
          '" data-task-placement="' + escapeHtml(placement || "panel") + '">' +
          escapeHtml(button.label) + "</button>";
      }).join("") + "</div>";
    }

    function renderConfirmControls(task, placement, options) {
      const pendingConfirm = task && task.pending_confirm;
      const taskId = String((task && task.task_id) || "");
      if (!pendingConfirm || !taskId) return "";
      const requestId = String(pendingConfirm.request_id || "");
      const draftKey = requestId || taskId;
      const editorKey = [placement || "panel", draftKey].join("::");
      const editorOpen = isConfirmEditorOpen(editorKey);
      const buttons = resolveConfirmButtons(task);
      const feedbackDraft = getConfirmDraft(draftKey);
      const includeSummary = !options || options.includeSummary !== false;
      const summary = pendingConfirm.summary || pendingConfirm.title || "";
      const summaryHtml = includeSummary && summary
        ? '<div class="task-control-note">' + escapeHtml(summary) + "</div>"
        : "";
      const buttonHtml = '<div class="task-control-group">' + buttons.map((button) => {
        if (button.action === "f") {
          return '<button class="btn ' + (button.cls || "") + (editorOpen ? " active" : "") + '" type="button" data-confirm-action="' + escapeHtml(button.action) +
            '" data-task-id="' + escapeHtml(taskId) +
            '" data-request-id="' + escapeHtml(requestId) +
            '" data-confirm-placement="' + escapeHtml(placement || "panel") +
            '" data-editor-key="' + escapeHtml(editorKey) +
            '" data-draft-key="' + escapeHtml(draftKey) + '">' +
            escapeHtml(button.label) + "</button>";
        }
        return '<button class="btn ' + (button.cls || "") + '" type="button" data-confirm-action="' + escapeHtml(button.action) +
          '" data-task-id="' + escapeHtml(taskId) +
          '" data-request-id="' + escapeHtml(requestId) +
          '" data-confirm-placement="' + escapeHtml(placement || "panel") + '">' +
          escapeHtml(button.label) + "</button>";
      }).join("") + "</div>";
      const feedbackHtml = buttons.some((button) => button.action === "f") && editorOpen
        ? (
            '<div class="task-confirm-feedback">' +
              '<textarea rows="3" data-confirm-feedback="' + escapeHtml(draftKey) + '" placeholder="输入补充意见后再提交给任务。">' +
                escapeHtml(feedbackDraft) +
              "</textarea>" +
              '<div class="task-confirm-feedback-actions">' +
                '<button class="btn-sm" type="button" data-confirm-cancel="1" data-task-id="' + escapeHtml(taskId) +
                  '" data-request-id="' + escapeHtml(requestId) +
                  '" data-confirm-placement="' + escapeHtml(placement || "panel") +
                  '" data-editor-key="' + escapeHtml(editorKey) +
                  '" data-draft-key="' + escapeHtml(draftKey) + '">取消</button>' +
                '<button class="btn-sm primary" type="button" data-confirm-submit="1" data-task-id="' + escapeHtml(taskId) +
                  '" data-request-id="' + escapeHtml(requestId) + '" data-confirm-placement="' + escapeHtml(placement || "panel") +
                  '" data-editor-key="' + escapeHtml(editorKey) +
                  '" data-draft-key="' + escapeHtml(draftKey) + '">提交</button>' +
              "</div>" +
            "</div>"
          )
        : "";
      return summaryHtml + buttonHtml + feedbackHtml;
    }

    function renderTaskControlNote(task) {
      const status = String((task && task.status) || "").toLowerCase();
      if (status === "waiting_confirm" && !(task && task.pending_confirm)) {
        return "当前任务已进入等待确认，但 confirm bridge 暂无匹配请求，暂不显示确认按钮。";
      }
      return "当前任务当前没有可执行的控制动作。";
    }

    function renderTaskActionSection(task, placement, options) {
      if (!task) return "";
      const taskControls = renderTaskControls(task, placement);
      const confirmControls = renderConfirmControls(task, placement, options);
      if (!taskControls && !confirmControls) {
        return '<div class="task-control-note">' + escapeHtml(renderTaskControlNote(task)) + "</div>";
      }
      return (
        '<div class="task-control-stack">' +
          (taskControls
            ? '<div class="task-control-section"><span class="task-control-label">任务操作</span>' + taskControls + "</div>"
            : "") +
          (confirmControls
            ? '<div class="task-control-section"><span class="task-control-label">确认操作</span>' + confirmControls + "</div>"
            : "") +
        "</div>"
      );
    }

    function nextTaskRefreshDelay() {
      if (document.visibilityState !== "visible") return 10000;
      return isTaskPollingActive(state.currentTask) || isTaskPollingActive(resolveDisplayTask()) ? 1800 : 8000;
    }

    function formatStatus(status) {
      const mapping = {
        idle: "空闲",
        running: "运行中",
        waiting_confirm: "等待确认",
        waiting_interaction: "等待交互",
        paused: "已暂停",
        failed: "失败",
        error: "异常",
        interrupted: "已中断",
        completed: "已完成",
        closed: "已关闭",
        terminated: "已终止"
      };
      return mapping[status] || status || "未知";
    }

    function inferRuntimeFamilyFromModel(model) {
      const value = String(model || "").trim();
      if (!value) return "";
      if (/^(gpt-|codex|o[134])|codex/i.test(value)) return "Codex";
      if (/(claude|sonnet|opus|haiku)/i.test(value)) return "Claude";
      return "";
    }

    function formatStepRuntimeLabel(step) {
      const runtimeValue = String(
        (step && (step.runtime_family || step.runtime_kind || step.provider_family || "")) || ""
      ).trim().toLowerCase();
      if (runtimeValue === "codex" || runtimeValue === "openai") return "Codex";
      if (runtimeValue === "claude_code" || runtimeValue === "claude" || runtimeValue === "anthropic") return "Claude";
      return inferRuntimeFamilyFromModel(step && step.model);
    }

    function formatStepModelLabel(step) {
      return String((step && step.model) || "").trim();
    }

    function renderStepMetaBadges(step) {
      const runtimeLabel = formatStepRuntimeLabel(step);
      const modelLabel = formatStepModelLabel(step);
      if (!runtimeLabel && !modelLabel) return "";
      return '<span class="step-meta-tags">' +
        (runtimeLabel
          ? '<span class="step-meta-tag runtime-' + escapeHtml(runtimeLabel.toLowerCase()) + '">' + escapeHtml(runtimeLabel) + "</span>"
          : "") +
        (modelLabel
          ? '<span class="step-meta-tag">' + escapeHtml(modelLabel) + "</span>"
          : "") +
      "</span>";
    }

    function resolveSelectedLiveNodeData(task, node) {
      if (!task || !node || node.taskId !== task.task_id) return null;
      if (node.type === "substep") {
        const cachedSubTask = findCachedSubTask(task, node.subtaskName || "");
        if (!cachedSubTask || !cachedSubTask.steps) return null;
        return cachedSubTask.steps.find((step) => (step.name || step.role || "") === node.id) || null;
      }
      return (task.steps || []).find((step) => (step.name || step.role || "") === node.id) || null;
    }

    function sessionStatus(session) {
      const turnStatus = String((session && session.current_turn_status) || "").trim();
      const baseStatus = String((session && session.status) || "").trim();
      if (turnStatus === "completed") return baseStatus || "idle";
      if (turnStatus === "failed" && baseStatus === "error") return "error";
      return turnStatus || baseStatus || "idle";
    }

    function sessionIsInterruptible(session) {
      return ["running", "waiting_confirm", "waiting_interaction"].includes(sessionStatus(session));
    }

    function composerHasSendableContent(input) {
      const text = input ? String(input.value || "").trim() : "";
      return !!text || (state.composerAttachments || []).length > 0;
    }

    function eventCountForSession(session) {
      return Number((session && session.latest_event_seq) || 0);
    }

    function resetFileTree() {
      state.fileTreeCache.clear();
      state.expandedDirs.clear();
      state.fileBrowserPath = "";
      state.fileEntries = [];
    }

    function currentProject() {
      return state.projects.find((item) => item.name === state.currentProjectName) || null;
    }

    function resolveProjectName(cwd) {
      const current = String(cwd || "");
      let match = "";
      state.projects.forEach((project) => {
        if (!current || !project.path) return;
        if (current === project.path || current.startsWith(project.path + "/")) {
          if (!match || project.path.length > (state.projects.find((item) => item.name === match) || {path: ""}).path.length) {
            match = project.name;
          }
        }
      });
      return match;
    }

    function sortSessions(items) {
      return (items || []).slice().sort((left, right) => {
        const rightTime = new Date(right.updated_at || right.created_at || 0).getTime();
        const leftTime = new Date(left.updated_at || left.created_at || 0).getTime();
        return rightTime - leftTime;
      });
    }

    function splitSessions(items) {
      const ordered = sortSessions(items);
      const active = [];
      const history = [];
      const hotStatuses = new Set(["running", "waiting_confirm", "waiting_interaction", "paused", "idle"]);
      ordered.forEach((session) => {
        if (hotStatuses.has(sessionStatus(session))) active.push(session);
        else history.push(session);
      });
      return { active, history };
    }

    function preferredSessionId() {
      const groups = splitSessions(state.sessions);
      return groups.active[0] ? String(groups.active[0].id || "") : "";
    }

    function sessionCanClose(session) {
      const status = sessionStatus(session);
      return status !== "running" && status !== "waiting_confirm" && status !== "waiting_interaction";
    }

    function sessionCanDelete(session) {
      return sessionCanClose(session);
    }

    function connectionSupportsFamily(connection, family) {
      const candidates = Array.isArray(connection && connection.runtime_candidates) ? connection.runtime_candidates : [];
      if (candidates.length) {
        return candidates.some((candidate) => (
          candidate &&
          candidate.scope === "main_session" &&
          candidate.runtime_kind === family &&
          candidate.supported
        ));
      }
      if (family === "codex") {
        const display = String((connection && connection.provider_display) || "").toLowerCase();
        const providerId = String((connection && connection.provider_id) || "").toLowerCase();
        return display.includes("openai") || providerId === "openai" || providerId === "codex";
      }
      return true;
    }

    function createProviderOptions() {
      const supported = CREATE_PROVIDER_OPTIONS.filter((option) => {
        return state.connections.some((connection) => connectionSupportsFamily(connection, option.value));
      });
      return supported.length ? supported : CREATE_PROVIDER_OPTIONS.slice();
    }

    function connectionsForFamily(family) {
      return state.connections.filter((connection) => connectionSupportsFamily(connection, family));
    }

    function connectionAuthMode(connection) {
      return String((connection && connection.auth_mode) || "").trim();
    }

    function connectionAuthStatus(connection) {
      return String((connection && connection.auth_status) || "").trim();
    }

    function isAccountLoginConnection(connection) {
      return connectionAuthMode(connection) === "account_login";
    }

    function isConnectionReadyForMainSession(connection) {
      if (!connection) return false;
      if (!isAccountLoginConnection(connection)) return true;
      return connectionAuthStatus(connection) === "ready";
    }

    function selectableConnectionsForFamily(family) {
      return connectionsForFamily(family).filter((connection) => isConnectionReadyForMainSession(connection));
    }

    function connectionAuthStatusBadge(connection) {
      if (!isAccountLoginConnection(connection)) return "";
      const status = connectionAuthStatus(connection);
      if (status === "ready") return "账号已登录";
      if (status === "expired") return "登录失效";
      if (status === "unknown") return "等待登录";
      return "需登录";
    }

    function connectionAuthStatusDescription(connection) {
      if (!isAccountLoginConnection(connection)) return "";
      const status = connectionAuthStatus(connection);
      if (status === "ready") {
        const label = String((connection && connection.auth_account_label) || "").trim();
        return label ? ("OpenAI 账号登录 · " + label) : "OpenAI 账号登录已就绪";
      }
      if (status === "expired") return "OpenAI 账号登录已失效，请先重新登录";
      if (status === "unknown") return "OpenAI 账号登录状态待确认";
      return "需先完成 OpenAI 账号登录";
    }

    function connectionSelectionDescription(connection) {
      return [
        connectionOptionDescription(connection),
        connectionAuthStatusDescription(connection)
      ].filter(Boolean).join(" · ");
    }

    function connectionBlockedToastMessage(connection) {
      if (!connection) return "所选连接当前不可用。";
      if (isAccountLoginConnection(connection)) {
        if (connectionAuthStatus(connection) === "expired") {
          return "所选 OpenAI 账号连接的登录已失效，请先在设置中重新登录。";
        }
        return "所选 OpenAI 账号连接尚未完成登录，请先在设置中完成账号登录。";
      }
      return "所选连接当前不可用。";
    }

    function preferredCreateModel(family, connection) {
      if (family === "claude_code") {
        if (state.activeSession && state.activeSession.runtime_family === family && state.activeSession.display_model) {
          return state.activeSession.display_model;
        }
        return "sonnet";
      }
      const candidate = (Array.isArray(connection && connection.runtime_candidates) ? connection.runtime_candidates : []).find((item) => {
        return item && item.scope === "main_session" && item.runtime_kind === family && item.supported;
      });
      return String((candidate && candidate.display_model) || "gpt-5.5");
    }

    function sessionRuntimeFamily(session) {
      return String((session && (session.runtime_family || session.runtime_kind)) || "").trim();
    }

    function isCodexSession(session) {
      return sessionRuntimeFamily(session) === "codex";
    }

    function currentReasoningEffort(session) {
      if (!isCodexSession(session)) return "";
      const metadata = (session && session.metadata && typeof session.metadata === "object") ? session.metadata : {};
      const effort = String((session && session.reasoning_effort) || metadata.reasoning_effort || "medium").trim();
      return ["low", "medium", "high", "xhigh"].includes(effort) ? effort : "medium";
    }

    function sessionModelOptions(session) {
      const family = sessionRuntimeFamily(session);
      const base = family === "claude_code"
        ? [
            { value: "opus", label: "opus", description: "Claude Code Opus 4.7", badge: "Claude" },
            { value: "sonnet", label: "sonnet", description: "Claude Code Sonnet 4.7", badge: "Claude" },
            { value: "haiku", label: "haiku", description: "Claude Code Haiku 4.7", badge: "Claude" }
          ]
        : [
            { value: "gpt-5.5", label: "gpt-5.5", description: "Codex 主力模型", badge: "Codex" },
            { value: "gpt-5.4", label: "gpt-5.4", description: "Codex 通用模型", badge: "Codex" },
            { value: "gpt-5.4-mini", label: "gpt-5.4-mini", description: "Codex 轻量模型", badge: "Codex" },
            { value: "gpt-5.3-codex", label: "gpt-5.3-codex", description: "Codex 专用模型", badge: "Codex" },
            { value: "gpt-5.3-codex-spark", label: "gpt-5.3-codex-spark", description: "Codex 快速模型", badge: "Codex" },
            { value: "gpt-5.2", label: "gpt-5.2", description: "Codex 兼容模型", badge: "Codex" }
          ];
      const current = String((session && session.display_model) || "").trim();
      const options = base.slice();
      if (current && !options.some((option) => option.value === current)) {
        options.unshift({ value: current, label: current, description: "当前会话模型", badge: "当前" });
      }
      return options;
    }

    function reasoningEffortOptions() {
      return [
        { value: "low", label: "low", description: "低思考深度" },
        { value: "medium", label: "medium", description: "默认思考深度" },
        { value: "high", label: "high", description: "高思考深度" },
        { value: "xhigh", label: "xhigh", description: "超高思考深度" }
      ];
    }

    function currentModelSettingsValue(session) {
      if (!session) return "";
      return encodeModelSettingValue(
        String(session.display_model || "").trim(),
        isCodexSession(session) ? currentReasoningEffort(session) : ""
      );
    }

    function modelSettingsSelectOptions(session) {
      const models = sessionModelOptions(session);
      if (!isCodexSession(session)) {
        return models.map((model) => ({
          value: encodeModelSettingValue(model.value, ""),
          label: model.label || model.value,
          description: model.description || "",
          badge: model.badge || "",
          modelValue: model.value,
          modelLabel: model.label || model.value,
          modelDescription: model.description || "",
          effortValue: "",
          effortLabel: "",
          effortDescription: ""
        }));
      }
      const efforts = reasoningEffortOptions();
      const options = [];
      models.forEach((model) => {
        efforts.forEach((effort) => {
          options.push({
            value: encodeModelSettingValue(model.value, effort.value),
            label: (model.label || model.value) + " · " + effort.label,
            description: [model.description || "", effort.description || ""].filter(Boolean).join(" / "),
            badge: model.badge || "",
            modelValue: model.value,
            modelLabel: model.label || model.value,
            modelDescription: model.description || "",
            effortValue: effort.value,
            effortLabel: effort.label,
            effortDescription: effort.description || ""
          });
        });
      });
      return options;
    }

    function sanitizeThinkingSummary(value) {
      const raw = String(value || "").replace(/\u0000/g, "").trim();
      if (!raw) return "";
      const compact = raw.replace(/\s+/g, " ").trim();
      if (!compact) return "";
      if (INTERNAL_THINKING_PATTERNS.some((pattern) => pattern.test(compact)) && compact.length <= 1200) return "";
      return raw.length > 2400 ? raw.slice(0, 2400).trimEnd() + "..." : raw;
    }

    function isCodexProgressNarration(raw, payload, text) {
      const runtime = String((raw && (raw.runtime_family || raw.runtime_kind)) || "").toLowerCase();
      const compact = String(text || "").replace(/\s+/g, " ").trim();
      if (payload && String(payload.phase || "") === "commentary") return true;
      if (runtime !== "codex" || !compact || compact.length > 260) return false;
      return (
        /^let me\b/i.test(compact) ||
        /^now\b.*\b(add|adjust|check|create|fix|implement|inspect|read|run|start|update|verify)\b/i.test(compact) ||
        /^i(?:'ll| will| need to| should| can now)\b/i.test(compact)
      );
    }

    function mapEvent(raw) {
      if (!raw || !raw.event_id) return null;
      const eventName = String(raw.event_name || raw.kind || "");
      if (!TIMELINE_VISIBLE_EVENT_NAMES.has(eventName)) return null;
      const payload = raw.payload || {};
      const text = String(raw.text || payload.text || "");
      let mappedEventName = eventName;
      let kind = "control";
      let title = eventName || "event";
      let badge = raw.runtime_kind || raw.runtime_family || "";
      let body = [];
      let image = null;
      let attachments = [];
      if (eventName === "user_text") {
        kind = "user";
        title = "用户输入";
        body = [text];
        attachments = Array.isArray(payload.attachments)
          ? payload.attachments.map((item) => {
              if (item && typeof item === "object") return item;
              return String(item || "").trim();
            }).filter(Boolean)
          : [];
      } else if (eventName === "assistant_text") {
        if (isCodexProgressNarration(raw, payload, text)) {
          mappedEventName = "runtime_status";
          kind = "control";
          title = "执行进展";
          body = [text];
        } else {
          kind = "assistant";
          title = "助手输出";
          body = [text];
        }
      } else if (eventName === "assistant_image") {
        image = normalizeSessionImage(payload, {
          id: raw.event_id,
          timestamp: raw.ts || raw.timestamp || "",
          seq: Number(raw.seq || 0)
        });
        if (!image) return null;
        kind = "image";
        title = "生成图片";
        badge = payload.mime_type || "image";
        body = [payload.revised_prompt || payload.prompt || "已生成图片"];
      } else if (eventName === "thinking") {
        const summary = sanitizeThinkingSummary(text);
        if (!summary) return null;
        kind = "thinking";
        title = "思考摘要";
        body = [summary];
      } else if (eventName === "interaction_requested") {
        kind = "control";
        title = "等待确认";
        badge = payload.interaction_kind || badge;
        body = [
          payload.summary || "当前会话正在等待你的选择。",
          payload.command || payload.tool_name || "",
          payload.details || ""
        ];
      } else if (eventName === "runtime_status") {
        kind = "control";
        title = payload.status_kind === "startup_hint"
          ? "启动提示"
          : (payload.status_kind === "commentary" || payload.status_kind === "progress" ? "执行进展" : "运行状态");
        body = [payload.text || text];
      } else if (eventName === "work_progress") {
        kind = "work";
        title = payload.summary || payload.title || "工作过程";
        badge = payload.kind_label || payload.status_label || payload.status || badge;
        body = [
          payload.command || "",
          payload.detail && payload.detail !== payload.command ? payload.detail : "",
          payload.output || "",
          payload.exit_code !== undefined && payload.exit_code !== null && payload.exit_code !== ""
            ? ("exit " + String(payload.exit_code))
            : ""
        ];
      } else if (eventName === "interrupt_requested") {
        kind = "work";
        title = "正在中断";
        badge = "状态";
        body = [payload.mode === "force" ? "正在强制停止当前回复。" : "正在停止当前回复。"];
      } else if (eventName === "interrupted") {
        kind = "work";
        title = "已中断";
        badge = "状态";
        body = ["当前回复已停止。"];
      } else if (eventName === "tool_call") {
        kind = "tool";
        title = payload.tool_name ? "工具调用 · " + payload.tool_name : "工具调用";
        body = [JSON.stringify(payload.tool_input || payload, null, 2)];
      } else if (eventName === "tool_result") {
        kind = "tool";
        title = payload.tool_name ? "工具结果 · " + payload.tool_name : "工具结果";
        body = [JSON.stringify(payload, null, 2)];
      } else if (eventName === "error" || eventName === "runtime_error") {
        kind = "control";
        title = "Runtime Error";
        body = [payload.message || text || "主会话执行失败。"];
      } else if (eventName === "code_edit") {
        kind = "tool";
        title = "代码修改";
        body = [payload.diff || payload.summary || text || JSON.stringify(payload, null, 2)];
      }
      body = body.map((line) => String(line || "").trim()).filter(Boolean);
      if (!body.length) return null;
      return {
        id: raw.event_id,
        seq: Number(raw.seq || 0),
        timestamp: String(raw.ts || raw.timestamp || ""),
        kind,
        title,
        badge,
        time: isoTime(raw.ts || ""),
        body,
        attachments,
        image,
        event_name: mappedEventName,
        role: String(raw.role || "")
      };
    }

    function eventSortTime(item) {
      const value = Date.parse(String((item && item.timestamp) || ""));
      return Number.isFinite(value) ? value : 0;
    }

    function compareTimelineEvents(left, right) {
      const leftTime = eventSortTime(left);
      const rightTime = eventSortTime(right);
      if (leftTime !== rightTime) return leftTime - rightTime;
      const leftSeq = Number((left && left.seq) || 0);
      const rightSeq = Number((right && right.seq) || 0);
      if (leftSeq !== rightSeq) return leftSeq - rightSeq;
      return Number((left && left.order) || 0) - Number((right && right.order) || 0);
    }

    function normalizeSessionImage(image, fallback) {
      const raw = image || {};
      const fallbackData = fallback || {};
      const id = String(raw.id || fallbackData.id || "").trim();
      const url = String(raw.url || fallbackData.url || "").trim();
      if (!id || !url) return null;
      return {
        id,
        url,
        session_id: String(raw.session_id || state.activeSessionId || ""),
        turn_id: String(raw.turn_id || ""),
        source: String(raw.source || ""),
        source_id: String(raw.source_id || ""),
        prompt: String(raw.prompt || ""),
        revised_prompt: String(raw.revised_prompt || ""),
        mime_type: String(raw.mime_type || "image/png"),
        extension: String(raw.extension || ""),
        size: Number(raw.size || 0),
        created_at: String(raw.created_at || fallbackData.timestamp || ""),
        seq: Number(raw.seq || fallbackData.seq || 0)
      };
    }

    function compareSessionImages(left, right) {
      const leftTime = Date.parse(String((left && left.created_at) || ""));
      const rightTime = Date.parse(String((right && right.created_at) || ""));
      const normalizedLeftTime = Number.isFinite(leftTime) ? leftTime : 0;
      const normalizedRightTime = Number.isFinite(rightTime) ? rightTime : 0;
      if (normalizedLeftTime !== normalizedRightTime) return normalizedRightTime - normalizedLeftTime;
      return Number((right && right.seq) || 0) - Number((left && left.seq) || 0);
    }

    function mergeSessionImages(images, options) {
      const byId = new Map((options && options.replace ? [] : state.sessionImages).map((item) => [item.id, item]));
      (images || []).forEach((image) => {
        const normalized = normalizeSessionImage(image);
        if (!normalized) return;
        byId.set(normalized.id, Object.assign({}, byId.get(normalized.id) || {}, normalized));
      });
      state.sessionImages = Array.from(byId.values()).sort(compareSessionImages);
      return state.sessionImages;
    }

    function sessionImageTitle(image) {
      return String((image && (image.revised_prompt || image.prompt)) || "生成图片").replace(/\s+/g, " ").trim();
    }

    function renderSessionImageRail() {
      const target = $("sessionImageRail");
      if (!target) return;
      const images = Array.isArray(state.sessionImages) ? state.sessionImages : [];
      const shell = target.closest(".stream-shell");
      if (shell) shell.classList.toggle("has-image-rail", images.length > 0);
      target.classList.toggle("open", images.length > 0);
      if (!images.length) {
        target.innerHTML = "";
        return;
      }
      const items = images.slice(0, 80).map((image) => {
        const title = sessionImageTitle(image);
        return (
          '<button class="session-image-thumb" type="button" data-session-image-open="' + escapeHtml(image.id || "") + '" title="' + escapeHtml(title || "查看图片") + '">' +
            '<img src="' + escapeHtml(image.url || "") + '" alt="' + escapeHtml(title || "生成图片") + '" loading="lazy">' +
          "</button>"
        );
      }).join("");
      target.innerHTML =
        '<div class="session-image-rail-inner">' +
          '<div class="session-image-rail-head"><span>图片</span><span class="session-image-rail-count">' + escapeHtml(String(images.length)) + "</span></div>" +
          '<div class="session-image-rail-list">' + items + "</div>" +
        "</div>";
    }

    function mergeEvents(rawEvents) {
      const byId = new Map(state.events.map((item) => [item.id, item]));
      const imageEvents = [];
      (rawEvents || []).forEach((raw) => {
        state.nextAfterSeq = Math.max(state.nextAfterSeq, Number(((raw && raw.seq) || 0)));
        const mapped = mapEvent(raw);
        if (!mapped) return;
        const existing = byId.get(mapped.id);
        mapped.order = existing && Number.isFinite(Number(existing.order))
          ? Number(existing.order)
          : state.eventOrderCounter++;
        byId.set(mapped.id, mapped);
        if (mapped.kind === "image" && mapped.image) imageEvents.push(mapped.image);
      });
      if (imageEvents.length) mergeSessionImages(imageEvents);
      state.events = Array.from(byId.values())
        .sort(compareTimelineEvents)
        .slice(-MAX_TIMELINE_EVENTS);
      updateLoadedEventSeqBounds();
      return imageEvents.length > 0;
    }

    function updateLoadedEventSeqBounds() {
      const seqs = state.events
        .map((item) => Number((item && item.seq) || 0))
        .filter((seq) => Number.isFinite(seq) && seq > 0);
      if (!seqs.length) {
        state.oldestEventSeq = 0;
        return;
      }
      state.oldestEventSeq = Math.min(...seqs);
      state.nextAfterSeq = Math.max(state.nextAfterSeq, ...seqs);
    }

    function updateSessionEventsPaging(payload) {
      const oldestSeq = Number((payload && payload.oldest_seq) || 0);
      if (Number.isFinite(oldestSeq) && oldestSeq > 0) {
        state.oldestEventSeq = state.oldestEventSeq
          ? Math.min(state.oldestEventSeq, oldestSeq)
          : oldestSeq;
      }
      if (payload && Object.prototype.hasOwnProperty.call(payload, "has_more_before")) {
        state.hasMoreSessionEventsBefore = !!payload.has_more_before;
      } else {
        state.hasMoreSessionEventsBefore = !!(state.oldestEventSeq && state.oldestEventSeq > 1);
      }
    }

    function sessionEventsUrl(sessionId, params) {
      const query = new URLSearchParams();
      Object.entries(params || {}).forEach(([key, value]) => {
        if (value === null || typeof value === "undefined" || value === "") return;
        query.set(key, String(value));
      });
      return ROUTE_BASE + "/api/sessions/" + encodeURIComponent(sessionId) + "/events?" + query.toString();
    }

    async function loadProjects() {
      const payload = await api(ROUTE_BASE + "/api/projects");
      state.projects = (payload.projects || []).slice().sort((left, right) => {
        const leftName = String((left && left.name) || "");
        const rightName = String((right && right.name) || "");
        const leftMain = leftName === MAINLINE_PROJECT_NAME;
        const rightMain = rightName === MAINLINE_PROJECT_NAME;
        if (leftMain !== rightMain) return leftMain ? -1 : 1;
        return leftName.localeCompare(rightName);
      });
    }

    async function refreshCreateProjectOptionsFromServer(preferredPath) {
      const selectedPath = String(preferredPath || "");
      if (!state.projectsRefreshPromise) {
        state.projectsRefreshPromise = loadProjects().finally(() => {
          state.projectsRefreshPromise = null;
        });
      }
      await state.projectsRefreshPromise;
      renderCreateProjectOptions(selectedPath);
    }

    async function refreshCreateConnectionsFromServer() {
      if (!state.connectionsRefreshPromise) {
        state.connectionsRefreshPromise = loadConnections().finally(() => {
          state.connectionsRefreshPromise = null;
        });
      }
      await state.connectionsRefreshPromise;
    }

    async function loadConnections() {
      try {
        const payload = await api(ROUTE_BASE + "/api/settings/connections");
        state.connections = payload.connections || [];
      } catch (error) {
        state.connections = [];
      }
    }

    async function loadSessions() {
      const payload = await api(ROUTE_BASE + "/api/sessions");
      state.sessions = sortSessions(payload.sessions || []);
      if (!state.activeSessionId || !state.sessions.some((item) => item.id === state.activeSessionId)) {
        state.activeSessionId = preferredSessionId();
      }
      state.activeSession = state.sessions.find((item) => item.id === state.activeSessionId) || null;
      if (state.activeSessionId) localStorage.setItem(STORAGE_KEY, state.activeSessionId);
      else localStorage.removeItem(STORAGE_KEY);
    }

    async function loadSessionEvents(sessionId) {
      if (!sessionId) {
        state.events = [];
        state.eventOrderCounter = 0;
        state.nextAfterSeq = 0;
        state.oldestEventSeq = 0;
        state.hasMoreSessionEventsBefore = false;
        state.loadingOlderSessionEvents = false;
        state.sessionImages = [];
        resetTimelineUiState();
        return;
      }
      state.events = [];
      state.eventOrderCounter = 0;
      state.nextAfterSeq = 0;
      state.oldestEventSeq = 0;
      state.hasMoreSessionEventsBefore = false;
      state.loadingOlderSessionEvents = false;
      state.sessionImages = [];
      resetTimelineUiState();
      const session = state.activeSession || state.sessions.find((item) => item.id === sessionId) || null;
      const latestSeq = Number((session && session.latest_event_seq) || 0);
      const eventParams = { limit: SESSION_EVENTS_PAGE_LIMIT };
      if (latestSeq > 0) eventParams.before_seq = latestSeq + 1;
      const payload = await api(sessionEventsUrl(sessionId, eventParams));
      mergeEvents(payload.events || []);
      updateSessionEventsPaging(payload);
      if (payload.session) mergeSessionSnapshot(payload.session);
    }

    async function refreshSessionEvents(sessionId) {
      if (!sessionId) return;
      if (!state.events.length || !state.nextAfterSeq) {
        await loadSessionEvents(sessionId);
        return;
      }
      const afterSeq = Number(state.nextAfterSeq || 0);
      const payload = await api(sessionEventsUrl(sessionId, {
        after_seq: afterSeq,
        limit: SESSION_EVENTS_PAGE_LIMIT
      }));
      if (state.activeSessionId && state.activeSessionId !== sessionId) return;
      mergeEvents(payload.events || []);
      updateSessionEventsPaging(payload);
      if (payload.session) mergeSessionSnapshot(payload.session);
    }

    async function loadOlderSessionEvents() {
      const sessionId = state.activeSessionId;
      if (!sessionId || state.loadingOlderSessionEvents || !state.hasMoreSessionEventsBefore) return;
      const beforeSeq = Number(state.oldestEventSeq || 0);
      if (!beforeSeq || beforeSeq <= 1) {
        state.hasMoreSessionEventsBefore = false;
        return;
      }
      const timeline = $("timeline");
      const previousHeight = timeline ? timeline.scrollHeight : 0;
      const previousTop = timeline ? timeline.scrollTop : 0;
      state.loadingOlderSessionEvents = true;
      cancelTimelineFollowBottom();
      state.timelineStickToBottom = false;
      try {
        const payload = await api(sessionEventsUrl(sessionId, {
          before_seq: beforeSeq,
          limit: SESSION_EVENTS_PAGE_LIMIT
        }));
        if (state.activeSessionId !== sessionId) return;
        if (!(payload.events || []).length) {
          state.hasMoreSessionEventsBefore = false;
          return;
        }
        mergeEvents(payload.events || []);
        updateSessionEventsPaging(payload);
        if (payload.session) mergeSessionSnapshot(payload.session);
        renderTimeline();
        window.requestAnimationFrame(() => {
          if (!timeline) return;
          const heightDelta = Math.max(0, timeline.scrollHeight - previousHeight);
          timeline.scrollTop = Math.max(0, previousTop + heightDelta);
          state.timelineStickToBottom = false;
        });
      } finally {
        state.loadingOlderSessionEvents = false;
      }
    }

    async function loadSessionImages(sessionId) {
      if (!sessionId) {
        state.sessionImages = [];
        return;
      }
      const payload = await api(ROUTE_BASE + "/api/sessions/" + encodeURIComponent(sessionId) + "/images");
      mergeSessionImages(payload.images || [], { replace: true });
      if (payload.session) mergeSessionSnapshot(payload.session);
    }

    function mergeSessionSnapshot(session) {
      const index = state.sessions.findIndex((item) => item.id === session.id);
      if (index >= 0) state.sessions[index] = session;
      else state.sessions.unshift(session);
      state.sessions = sortSessions(state.sessions);
      if (state.activeSessionId === session.id) state.activeSession = session;
    }

    function resetTimelineUiState() {
      if (state.timelineRenderFrame) {
        window.cancelAnimationFrame(state.timelineRenderFrame);
        state.timelineRenderFrame = null;
      }
      cancelTimelineFollowBottom();
      if (state.timelineFreshCleanupHandle) {
        window.clearTimeout(state.timelineFreshCleanupHandle);
        state.timelineFreshCleanupHandle = null;
      }
      state.expandedTimelineBodies.clear();
      state.timelineSeenRenderKeys.clear();
      state.timelineFreshRenderKeys.clear();
      state.timelineRenderInitialized = false;
      state.timelineStickToBottom = true;
    }

    function cancelTimelineFollowBottom() {
      if (state.timelineFollowBottomFrame) {
        window.cancelAnimationFrame(state.timelineFollowBottomFrame);
        state.timelineFollowBottomFrame = null;
      }
      state.timelineFollowBottomUntil = 0;
    }

    function scrollTimelineToBottomNow() {
      const timeline = $("timeline");
      if (!timeline) return;
      timeline.scrollTop = Math.max(0, timeline.scrollHeight - timeline.clientHeight);
      state.timelineStickToBottom = true;
      updateTimelineBottomButton(timeline);
    }

    function resizeComposerInput(input) {
      if (!input) return;
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight, 100) + "px";
    }

    function composerDraftKey(sessionId) {
      const normalized = String(sessionId || "").trim();
      if (!normalized) return "";
      return COMPOSER_DRAFT_KEY_PREFIX + encodeURIComponent(normalized);
    }

    function persistComposerDraft(sessionId) {
      const input = $("composerInput");
      const key = composerDraftKey(sessionId || state.activeSessionId);
      if (!input || !key) return;
      try {
        const value = String(input.value || "");
        if (value) localStorage.setItem(key, value);
        else localStorage.removeItem(key);
      } catch (error) {}
    }

    function restoreComposerDraft(sessionId) {
      const input = $("composerInput");
      const key = composerDraftKey(sessionId || state.activeSessionId);
      if (!input) return;
      let value = "";
      if (key) {
        try {
          value = localStorage.getItem(key) || "";
        } catch (error) {}
      }
      input.value = value;
      resizeComposerInput(input);
    }

    function clearComposerDraft(sessionId) {
      const key = composerDraftKey(sessionId || state.activeSessionId);
      if (!key) return;
      try {
        localStorage.removeItem(key);
      } catch (error) {}
    }

    function requestTimelineFollowBottom(durationMs) {
      const duration = Math.max(250, Number(durationMs || 1400));
      state.timelineFollowBottomUntil = Math.max(
        Number(state.timelineFollowBottomUntil || 0),
        Date.now() + duration
      );
      state.timelineStickToBottom = true;
      if (state.timelineFollowBottomFrame) return;
      const tick = () => {
        state.timelineFollowBottomFrame = null;
        if (Date.now() > Number(state.timelineFollowBottomUntil || 0)) {
          state.timelineFollowBottomUntil = 0;
          return;
        }
        scrollTimelineToBottomNow();
        state.timelineFollowBottomFrame = window.requestAnimationFrame(tick);
      };
      state.timelineFollowBottomFrame = window.requestAnimationFrame(tick);
    }

    function scheduleTimelineRender() {
      if (state.timelineRenderFrame) return;
      state.timelineRenderFrame = window.requestAnimationFrame(() => {
        state.timelineRenderFrame = null;
        renderTimeline();
      });
    }

    function connectionStateClass() {
      return state.ws && state.ws.readyState === WebSocket.OPEN ? "connected" : "idle";
    }

    function closeSessionStream() {
      if (state.ws) {
        state.ws.onclose = null;
        state.ws.close();
        state.ws = null;
      }
      if (state.reconnectHandle) {
        clearTimeout(state.reconnectHandle);
        state.reconnectHandle = null;
      }
    }

    function connectSessionStream(sessionId) {
      if (!sessionId) return;
      closeSessionStream();
      const protocol = location.protocol === "https:" ? "wss:" : "ws:";
      const url = protocol + "//" + location.host +
        ROUTE_BASE + "/api/sessions/" + encodeURIComponent(sessionId) + "/events/ws?after_seq=" + encodeURIComponent(state.nextAfterSeq);
      const ws = new WebSocket(url);
      state.ws = ws;
      renderIdentity();
      ws.onopen = () => {
        state.reconnectDelay = 1200;
        renderIdentity();
      };
      ws.onmessage = (event) => {
        try {
          const message = JSON.parse(event.data);
          if (message.type === "event" && message.event) {
            const hasImageEvent = mergeEvents([message.event]);
            scheduleTimelineRender();
            if (hasImageEvent) renderSessionImageRail();
            scheduleTaskRefresh(250);
          } else if (message.type === "session_snapshot" && message.session) {
            mergeSessionSnapshot(message.session);
            renderSessionSurfaces({ includeTimeline: true, includeLivePanel: true });
          }
        } catch (error) {}
      };
      ws.onclose = () => {
        state.ws = null;
        renderIdentity();
        if (!state.activeSessionId) return;
        state.reconnectHandle = window.setTimeout(() => {
          connectSessionStream(state.activeSessionId);
        }, state.reconnectDelay);
        state.reconnectDelay = Math.min(state.reconnectDelay * 1.6, 8000);
      };
    }

    async function switchSession(sessionId) {
      if (!sessionId) return;
      const previousSessionId = state.activeSessionId || "";
      if (previousSessionId && previousSessionId !== sessionId) persistComposerDraft(previousSessionId);
      state.activeSessionId = sessionId;
      localStorage.setItem(STORAGE_KEY, sessionId);
      state.activeSession = state.sessions.find((item) => item.id === sessionId) || null;
      state.currentProjectName = resolveProjectName(state.activeSession ? state.activeSession.cwd : "");
      resetFileTree();
      state.preview = null;
      state.imageViewer = null;
      resetImageViewerTransform();
      resetLivePanelState();
      state.rawLogContent = "";
      state.rawLogOffset = 0;
      state.composerAttachments = [];
      const taskRefresh = refreshTaskState().catch(() => {});
      await loadSessionEvents(sessionId);
      await loadSessionImages(sessionId).catch(() => {});
      if (state.currentProjectName) {
        await loadFileBrowser("");
      } else {
        resetFileTree();
      }
      requestTimelineFollowBottom(1800);
      renderAll();
      restoreComposerDraft(sessionId);
      requestTimelineFollowBottom(1800);
      connectSessionStream(sessionId);
      await taskRefresh;
      scheduleTaskRefresh(nextTaskRefreshDelay());
      if (state.timelineStickToBottom !== false) requestTimelineFollowBottom(700);
      if ($("rawDrawer").classList.contains("open")) {
        await refreshRawLog(true);
      }
    }

    async function createProjectIfNeeded() {
      const nameNode = $("newProjectNameInput");
      const pathNode = $("newProjectPathInput");
      const descNode = $("newProjectDescInput");
      const name = nameNode ? nameNode.value.trim() : "";
      if (!name) return null;
      const description = descNode ? descNode.value.trim() : "";
      const path = pathNode ? normalizeProjectRoot(pathNode.value.trim()) : "";
      const body = { name, description };
      if (path) body.path = path;
      const payload = await api(ROUTE_BASE + "/api/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      await loadProjects();
      return payload.project;
    }

    async function createSession() {
      const projectField = $("projectSelect");
      const modelField = $("modelInput");
      const connectionField = $("connectionSelect");
      let projectPath = projectField ? projectField.value : "";
      const createdProject = await createProjectIfNeeded();
      if (createdProject && createdProject.path) projectPath = createdProject.path;
      if (!projectPath) {
        toast("请先选择项目或新建项目。", "error");
        return;
      }
      const sessionName = $("sessionNameInput").value.trim();
      const connectionId = connectionField ? (connectionField.value || state.createConnectionId || "") : (state.createConnectionId || "");
      const family = state.createRuntimeFamily || "claude_code";
      const selectedConnection = connectionsForFamily(family).find((item) => connectionOptionId(item) === connectionId) || null;
      if (selectedConnection && !isConnectionReadyForMainSession(selectedConnection)) {
        toast(connectionBlockedToastMessage(selectedConnection), "error");
        return;
      }
      const displayModel = modelField ? String(modelField.value || "").trim() : "";
      const payload = await api(ROUTE_BASE + "/api/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: sessionName,
          cwd: projectPath,
          connection_id: connectionId,
          display_model: displayModel
        })
      });
      closeModal();
      $("sessionNameInput").value = "";
      if ($("newProjectNameInput")) $("newProjectNameInput").value = "";
      if ($("newProjectPathInput")) $("newProjectPathInput").value = "";
      if ($("newProjectDescInput")) $("newProjectDescInput").value = "";
      if ($("newProjectInput")) $("newProjectInput").classList.remove("show");
      if ($("projectSelect")) $("projectSelect").value = "";
      if (modelField) modelField.value = "";
      state.createConnectionId = "";
      await loadSessions();
      await switchSession((payload.session || {}).id || state.activeSessionId);
      toast("主会话已创建。", "success");
    }

    function clearActiveSessionSelection() {
      closeSessionStream();
      state.activeSessionId = "";
      state.activeSession = null;
      state.currentProjectName = "";
      state.events = [];
      state.eventOrderCounter = 0;
      state.nextAfterSeq = 0;
      state.oldestEventSeq = 0;
      state.hasMoreSessionEventsBefore = false;
      state.loadingOlderSessionEvents = false;
      state.sessionImages = [];
      resetTimelineUiState();
      state.preview = null;
      state.imageViewer = null;
      resetImageViewerTransform();
      state.rawLogContent = "";
      state.rawLogOffset = 0;
      resetFileTree();
      resetLivePanelState();
      localStorage.removeItem(STORAGE_KEY);
    }

    async function requestCloseSession(sessionId) {
      const session = state.sessions.find((item) => item.id === sessionId) || null;
      if (!session) return;
      if (!sessionCanClose(session)) {
        toast("当前会话正在执行，暂时不能关闭。", "error");
        return;
      }
      const decision = await requestTaskActionConfirmation({
        title: "关闭会话",
        subtitle: String(session.name || session.id || ""),
        message: "关闭后会停止该会话的常驻 runtime，并把它移入历史会话。后续仍可从历史会话继续查看或再次发送消息。",
        actions: [
          { label: "取消", cls: "ghost-btn", result: null },
          { label: "确认关闭", cls: "btn warn", result: { confirmed: true } }
        ]
      });
      if (!decision || decision.confirmed !== true) return;
      await closeSession(sessionId);
    }

    async function closeSession(sessionId) {
      const wasActive = sessionId === state.activeSessionId;
      if (wasActive) clearActiveSessionSelection();
      const payload = await api(ROUTE_BASE + "/api/sessions/" + encodeURIComponent(sessionId) + "/close", {
        method: "POST"
      });
      await loadSessions();
      if (state.activeSessionId) {
        await switchSession(state.activeSessionId);
      } else {
        renderAll();
        await refreshTaskState().catch(() => {});
      }
      toast((payload && payload.message) || "会话已关闭，已移入历史会话。", "success");
    }

    async function requestDeleteHistorySession(sessionId) {
      const session = state.sessions.find((item) => item.id === sessionId) || null;
      if (!session) return;
      if (!sessionCanDelete(session)) {
        toast("当前会话正在执行，暂时不能删除。", "error");
        return;
      }
      const sessionName = String(session.name || session.id || "");
      const projectPath = String(session.cwd || ((session.metadata || {}).project_root) || "-");
      const decision = await requestTaskActionConfirmation({
        title: "删除历史会话",
        subtitle: "",
        message: "历史会话：" + sessionName + "\n项目路径：" + projectPath + "\n\n删除后将永久移除该历史会话的对话记录、运行日志和生成图片，此操作无法恢复。",
        actions: [
          { label: "取消", cls: "ghost-btn", result: null },
          { label: "确认删除", cls: "btn danger", result: { confirmed: true } }
        ]
      });
      if (!decision || decision.confirmed !== true) return;
      await deleteSession(sessionId);
    }

    async function deleteSession(sessionId) {
      const wasActive = sessionId === state.activeSessionId;
      if (wasActive) clearActiveSessionSelection();
      await api(ROUTE_BASE + "/api/sessions/" + encodeURIComponent(sessionId), {
        method: "DELETE"
      });
      await loadSessions();
      if (state.activeSessionId) {
        await switchSession(state.activeSessionId);
      } else {
        renderAll();
        await refreshTaskState().catch(() => {});
      }
      toast("历史会话已删除。", "success");
    }

    function connectionOptionId(connection) {
      return String((connection && (connection.id || connection.connection_id)) || "");
    }

    function connectionOptionLabel(connection) {
      return String(
        (connection && (connection.name || connection.connection_name || connection.provider_display || connection.host_label))
        || "当前主连接"
      );
    }

    function connectionOptionDescription(connection) {
      return [
        connection && connection.provider_display,
        connection && connection.host_label
      ].filter(Boolean).join(" · ");
    }

    function sessionChannelOptions(session) {
      if (!session) return [];
      const family = String(session.runtime_family || session.runtime_kind || "");
      const options = connectionsForFamily(family).map((connection) => {
        return {
          id: connectionOptionId(connection),
          name: connection.name || "未命名连接",
          provider_display: connection.provider_display || "",
          host_label: connection.host_label || "",
          description: connectionSelectionDescription(connection),
          badge: connectionAuthStatusBadge(connection),
          disabled: !isConnectionReadyForMainSession(connection)
        };
      }).filter((connection) => connection.id);
      const currentId = String(session.connection_id || "");
      if (currentId && !options.some((connection) => connection.id === currentId)) {
        options.unshift({
          id: currentId,
          name: session.connection_name || "当前主连接",
          provider_display: session.provider_display || "",
          host_label: "",
          description: connectionOptionDescription(session),
          badge: "",
          disabled: false
        });
      }
      return options;
    }

    function renderChannelSelect() {
      const select = $("channelSelect");
      if (!select) return;
      const session = state.activeSession;
      if (!session) {
        select.innerHTML = '<option value="">未选择会话</option>';
        select.value = "";
        select.disabled = true;
        syncDialogueDropdown(select);
        return;
      }
      const options = sessionChannelOptions(session);
      if (!options.length) {
        select.innerHTML = '<option value="">当前没有可切换渠道</option>';
        select.value = "";
        select.disabled = true;
        syncDialogueDropdown(select);
        return;
      }
      select.innerHTML = options.map((connection) => {
        return '<option value="' + escapeHtml(connection.id) + '"' +
          htmlAttr("data-description", connection.description || connectionOptionDescription(connection)) +
          htmlAttr("data-badge", connection.badge || "") +
          (connection.disabled ? " disabled" : "") +
          '>' + escapeHtml(connectionOptionLabel(connection)) + "</option>";
      }).join("");
      select.value = String(session.connection_id || options[0].id || "");
      select.disabled = options.length <= 1 && !options[0].disabled;
      select.title = "切换当前会话渠道";
      syncDialogueDropdown(select);
    }

    function renderModelSettingsSelect() {
      const modelSelect = $("modelSettingsSelect");
      if (!modelSelect) return;
      const session = state.activeSession;
      if (!session) {
        modelSelect.innerHTML = '<option value="">未选择会话</option>';
        modelSelect.value = "";
        modelSelect.disabled = true;
        modelSelect.dataset.runtimeFamily = "";
        syncDialogueDropdown(modelSelect);
        return;
      }
      modelSelect.dataset.runtimeFamily = sessionRuntimeFamily(session);
      const options = modelSettingsSelectOptions(session);
      modelSelect.innerHTML = options.map((option) => {
        return '<option value="' + escapeHtml(option.value) + '"' +
          htmlAttr("data-model", option.modelValue || "") +
          htmlAttr("data-model-label", option.modelLabel || "") +
          htmlAttr("data-model-description", option.modelDescription || "") +
          htmlAttr("data-reasoning-effort", option.effortValue || "") +
          htmlAttr("data-effort-label", option.effortLabel || "") +
          htmlAttr("data-effort-description", option.effortDescription || "") +
          htmlAttr("data-description", option.description || "") +
          htmlAttr("data-badge", option.badge || "") +
          '>' + escapeHtml(option.label || option.value) + "</option>";
      }).join("");
      const currentValue = currentModelSettingsValue(session);
      modelSelect.value = options.some((option) => option.value === currentValue)
        ? currentValue
        : String((options[0] && options[0].value) || "");
      modelSelect.disabled = !options.length;
      modelSelect.title = isCodexSession(session) ? "切换当前会话模型和 Codex 思考深度" : "切换当前会话模型";
      syncDialogueDropdown(modelSelect);
    }

    async function switchChannel() {
      const select = $("channelSelect");
      const session = state.activeSession;
      if (!select || !session) return;
      const nextConnectionId = String(select.value || "").trim();
      if (!nextConnectionId || nextConnectionId === String(session.connection_id || "")) return;
      const nextConnection = connectionsForFamily(String(session.runtime_family || session.runtime_kind || ""))
        .find((item) => connectionOptionId(item) === nextConnectionId) || null;
      if (nextConnection && !isConnectionReadyForMainSession(nextConnection)) {
        select.value = String(session.connection_id || "");
        syncDialogueDropdown(select);
        toast(connectionBlockedToastMessage(nextConnection), "error");
        return;
      }
      const previousConnectionId = String(session.connection_id || "");
      try {
        const payload = await api(ROUTE_BASE + "/api/sessions/" + encodeURIComponent(session.id) + "/connection", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ connection_id: nextConnectionId })
        });
        if (payload.session) mergeSessionSnapshot(payload.session);
        await loadConnections();
        state.activeSession = state.sessions.find((item) => item.id === state.activeSessionId) || state.activeSession;
        renderAll();
        toast("渠道切换已立即生效。", "success");
      } catch (error) {
        select.value = previousConnectionId;
        throw error;
      }
    }

    async function applyModelSelection(nextModel, nextReasoningEffort) {
      const session = state.activeSession;
      if (!session) return;
      nextModel = String(nextModel || "").trim();
      if (!nextModel) return;
      nextReasoningEffort = isCodexSession(session)
        ? String(nextReasoningEffort || currentReasoningEffort(session) || "medium").trim()
        : "";
      if (nextModel === String(session.display_model || "") && nextReasoningEffort === currentReasoningEffort(session)) return;
      const payload = await api(ROUTE_BASE + "/api/sessions/" + encodeURIComponent(session.id) + "/model", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          display_model: nextModel,
          reasoning_effort: nextReasoningEffort || undefined
        })
      });
      if (payload && payload.session) {
        mergeSessionSnapshot(payload.session);
      } else {
        await loadSessions();
      }
      state.activeSession = state.sessions.find((item) => item.id === state.activeSessionId) || state.activeSession;
      renderAll();
    }

    async function switchModelSettings() {
      const session = state.activeSession;
      const modelSelect = $("modelSettingsSelect");
      if (!session || !modelSelect) return;
      const previousValue = currentModelSettingsValue(session);
      const next = decodeModelSettingValue(modelSelect.value);
      try {
        await applyModelSelection(next.model, isCodexSession(session) ? next.reasoningEffort : "");
      } catch (error) {
        modelSelect.value = previousValue;
        syncDialogueDropdown(modelSelect);
        toast(error.message, "error");
      }
    }

    async function addComposerFiles(fileList) {
      const session = state.activeSession;
      if (!session) {
        toast("请先创建或切换到一个主会话。", "error");
        return;
      }
      const remainingSlots = Math.max(0, 8 - (state.composerAttachments || []).length);
      const files = Array.from(fileList || []).slice(0, remainingSlots);
      if (!files.length) return;
      const nextItems = files.map((file) => ({
        id: String(Date.now()) + "-" + Math.random().toString(16).slice(2),
        attachmentId: "",
        name: String(file.name || "未命名文件"),
        size: Number(file.size || 0),
        type: String(file.type || ""),
        path: "",
        uploadedAt: "",
        uploadStatus: "uploading",
        error: "",
        file
      }));
      state.composerAttachments = (state.composerAttachments || []).concat(nextItems).slice(0, 8);
      renderComposer();
      for (const item of nextItems) {
        try {
          const payload = await uploadComposerFile(session.id, item.file);
          const attachment = (payload && payload.attachment) || {};
          item.attachmentId = String(attachment.id || item.id || "");
          item.name = String(attachment.name || item.name || "未命名文件");
          item.size = Number(attachment.size || item.size || 0);
          item.type = String(attachment.type || item.type || "");
          item.path = String(attachment.path || "");
          item.uploadedAt = String(attachment.uploaded_at || "");
          item.uploadStatus = "uploaded";
          item.error = "";
        } catch (error) {
          item.uploadStatus = "error";
          item.error = error.message || "上传失败";
        } finally {
          delete item.file;
          renderComposer();
        }
      }
    }

    async function uploadComposerFile(sessionId, file) {
      const form = new FormData();
      form.append("file", file, file.name || "attachment");
      return api(ROUTE_BASE + "/api/sessions/" + encodeURIComponent(sessionId) + "/attachments", {
        method: "POST",
        body: form
      });
    }

    function removeComposerAttachment(id) {
      state.composerAttachments = (state.composerAttachments || []).filter((item) => item.id !== id);
      renderComposer();
    }

    function renderComposerAttachments() {
      const host = $("composerFiles");
      if (!host) return;
      const items = state.composerAttachments || [];
      host.hidden = !items.length;
      host.innerHTML = items.map((item) => {
        const statusText = item.uploadStatus === "uploading"
          ? " · 上传中"
          : (item.uploadStatus === "error" ? (" · " + (item.error || "上传失败")) : "");
        return (
          '<span class="composer-file-chip" title="' + escapeHtml(item.name) + '">' +
            '<span class="composer-file-icon" aria-hidden="true">' +
              '<img class="composer-file-icon-img" src="/vizo/static/icons/material-design/outlined/insert_drive_file.svg" alt="" aria-hidden="true">' +
            "</span>" +
            '<span class="composer-file-main">' +
              '<span class="composer-file-name">' + escapeHtml(item.name) + "</span>" +
              '<span class="composer-file-meta">' + escapeHtml(formatFileSize(item.size) + statusText) + "</span>" +
            "</span>" +
            '<button class="composer-file-remove" type="button" data-composer-file-remove="' + escapeHtml(item.id) + '" aria-label="移除文件 ' + escapeHtml(item.name) + '">' +
              '<img class="composer-file-remove-icon" src="/vizo/static/icons/material-design/outlined/close.svg" alt="" aria-hidden="true">' +
            "</button>" +
          "</span>"
        );
      }).join("");
    }

    async function sendMessage() {
      const session = state.activeSession;
      if (!session) {
        toast("请先创建或切换到一个主会话。", "error");
        return;
      }
      const input = $("composerInput");
      let content = input.value.trim();
      const pendingAttachments = (state.composerAttachments || []).filter((item) => item.uploadStatus === "uploading");
      if (pendingAttachments.length) {
        toast("附件还在上传，请稍后发送。", "error");
        return;
      }
      const failedAttachments = (state.composerAttachments || []).filter((item) => item.uploadStatus === "error");
      if (failedAttachments.length) {
        toast("请先移除上传失败的附件。", "error");
        return;
      }
      const attachments = composerAttachmentPayload();
      if (!content && attachments.length) content = "请阅读附加文件。";
      if (!content) return;
      if (attachments.length && content.startsWith("/")) {
        toast("控制命令不能携带附加文件。", "error");
        return;
      }
      const payload = await api(ROUTE_BASE + "/api/sessions/" + encodeURIComponent(session.id) + "/messages", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          content,
          attachments,
          expected_runtime: session.runtime_family || session.runtime_kind || ""
        })
      });
      input.value = "";
      clearComposerDraft(session.id);
      resizeComposerInput(input);
      state.composerAttachments = [];
      renderComposer();
      if (payload && payload.session) mergeSessionSnapshot(payload.session);
      if (payload && payload.message) {
        toast(
          payload.message,
          payload.status === "accepted" || payload.status === "model_switch_applied" ? "success" : ""
        );
      }
      if (payload && payload.status !== "accepted") {
        await loadSessions();
        renderAll();
      }
      scheduleTaskRefresh(150);
    }

    function interruptModeForSession(session) {
      if (!session || !sessionIsInterruptible(session)) return "";
      const id = String(session.id || session.session_id || "");
      const entry = state.interruptingSessions.get(id);
      if (entry && Date.now() - Number(entry.startedAt || 0) >= 3500) return "force";
      return "soft";
    }

    function scheduleInterruptRenderRefresh() {
      if (state.interruptRenderHandle) window.clearTimeout(state.interruptRenderHandle);
      state.interruptRenderHandle = window.setTimeout(() => {
        state.interruptRenderHandle = null;
        renderComposer();
      }, 3600);
    }

    async function interruptActiveSession(mode) {
      const session = state.activeSession;
      if (!session) {
        toast("请先创建或切换到一个主会话。", "error");
        return;
      }
      if (!sessionIsInterruptible(session)) return;
      const sessionId = String(session.id || session.session_id || "");
      const existing = state.interruptingSessions.get(sessionId);
      const now = Date.now();
      const requestedMode = mode || interruptModeForSession(session) || "soft";
      if (existing && requestedMode !== "force" && now - Number(existing.lastRequestAt || 0) < 1200) return;
      state.interruptingSessions.set(sessionId, {
        startedAt: existing ? existing.startedAt : now,
        lastRequestAt: now
      });
      renderComposer();
      scheduleInterruptRenderRefresh();
      try {
        const payload = await api(ROUTE_BASE + "/api/sessions/" + encodeURIComponent(sessionId) + "/interrupt", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            mode: requestedMode,
            drop_pending: true,
            expected_runtime: session.runtime_family || session.runtime_kind || ""
          })
        });
        if (payload && payload.session) mergeSessionSnapshot(payload.session);
        if (payload && payload.message) toast(payload.message, payload.status === "interrupted" ? "success" : "");
        try {
          await refreshSessionEvents(sessionId);
        } catch (error) {}
        await loadSessions();
        renderAll();
      } finally {
        state.interruptingSessions.delete(sessionId);
        renderComposer();
      }
    }

    function pendingMessageItems(session) {
      const items = Array.isArray(session && session.pending_inputs) ? session.pending_inputs : [];
      return items.filter((item) => item && (item.kind || "message") === "message");
    }

    function pendingInputText(item) {
      const text = String((item && (item.content_preview || item.content)) || "").trim();
      const attachments = Array.isArray(item && item.attachments) ? item.attachments : [];
      if (text) return text;
      if (attachments.length) return attachments.length + " 个附件";
      return "空消息";
    }

    async function deletePendingInput(queueId) {
      const session = state.activeSession;
      if (!session || !queueId) return;
      const payload = await api(
        ROUTE_BASE + "/api/sessions/" + encodeURIComponent(session.id) + "/pending/" + encodeURIComponent(queueId),
        {
          method: "DELETE",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ expected_runtime: session.runtime_family || session.runtime_kind || "" })
        }
      );
      if (payload && payload.session) mergeSessionSnapshot(payload.session);
      renderComposer();
      if (payload && payload.message) toast(payload.message, "success");
    }

    async function sendPendingInputNow(queueId) {
      const session = state.activeSession;
      if (!session || !queueId) return;
      const payload = await api(
        ROUTE_BASE + "/api/sessions/" + encodeURIComponent(session.id) + "/pending/" + encodeURIComponent(queueId) + "/send-now",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            mode: "soft",
            expected_runtime: session.runtime_family || session.runtime_kind || ""
          })
        }
      );
      if (payload && payload.session) mergeSessionSnapshot(payload.session);
      try {
        await refreshSessionEvents(session.id);
        requestTimelineFollowBottom(1200);
      } catch (error) {}
      renderAll();
      if (payload && payload.message) toast(payload.message, "success");
    }

    async function loadFileBrowser(path) {
      const project = currentProject();
      if (!project) {
        resetFileTree();
        state.fileEntries = [];
        return;
      }
      const normalizedPath = normalizePath(path);
      const payload = await api(
        ROUTE_BASE + "/api/projects/" + encodeURIComponent(project.name) + "/files?path=" + encodeURIComponent(normalizedPath)
      );
      const entries = (payload.entries || []).map((entry) => ({
        name: entry.name,
        type: entry.type,
        size: entry.size,
        mtime: entry.mtime,
        path: joinPath(normalizedPath, entry.name)
      }));
      state.fileBrowserPath = normalizedPath;
      state.fileEntries = entries;
      state.fileTreeCache.set(normalizedPath, entries);
    }

    function projectFileRawUrl(projectName, path) {
      const query = new URLSearchParams({ path: normalizePath(path) });
      return ROUTE_BASE + "/api/projects/" + encodeURIComponent(projectName) + "/files/raw?" + query.toString();
    }

    function previewFileExtension(path) {
      const name = basename(path);
      const dotIndex = name.lastIndexOf(".");
      return dotIndex >= 0 ? name.slice(dotIndex + 1).toLowerCase() : "";
    }

    function fileShouldRenderInBrowser(path, payload) {
      const ext = previewFileExtension(path);
      const contentType = String((payload && payload.content_type) || "").toLowerCase();
      return ext === "html" || contentType.includes("text/html");
    }

    async function openFile(path, options) {
      const requestedProjectName = String((options && options.projectName) || "").trim();
      const project = requestedProjectName
        ? state.projects.find((item) => item.name === requestedProjectName)
        : currentProject();
      if (!project) return;
      const normalizedPath = normalizePath(path);
      const allowBasenameSearch = !!(options && options.allowBasenameSearch);
      const query = new URLSearchParams({ path: normalizedPath });
      if (allowBasenameSearch) query.set("fallback_basename", "1");
      if (project.name !== state.currentProjectName) {
        state.currentProjectName = project.name;
        resetFileTree();
        loadFileBrowser("").then(renderFilesPane).catch((error) => toast(error.message, "error"));
      }
      const payload = await api(
        ROUTE_BASE + "/api/projects/" + encodeURIComponent(project.name) + "/files?" + query.toString()
      );
      const resolvedPath = normalizePath(payload.path || normalizedPath);
      const rawUrl = projectFileRawUrl(project.name, resolvedPath);
      const browserPreviewUrl = fileShouldRenderInBrowser(resolvedPath, payload) ? rawUrl : "";
      state.preview = {
        kind: "file",
        project: project.name,
        path: resolvedPath,
        line: Number((options && options.line) || 0),
        column: Number((options && options.column) || 0),
        binary: !!payload.binary,
        content: payload.content || "",
        size: payload.size || 0,
        name: payload.name || basename(resolvedPath),
        raw_url: rawUrl,
        url: browserPreviewUrl,
        render_mode: browserPreviewUrl ? "iframe" : ""
      };
      if (IS_MOBILE_MODE) openSheet("preview");
      renderPreview();
    }

    async function openDialogueLocalPath(value, options) {
      const repairedValue = repairDialogueLocalPathToken(value, options && options.sourceElement);
      const target = resolveDialogueLocalFileTarget(repairedValue);
      if (!target) {
        toast("这个本地路径不在当前项目范围内，暂时无法预览。", "error");
        return;
      }
      await openFile(target.path, {
        projectName: target.project.name,
        line: target.line,
        column: target.column,
        allowBasenameSearch: true
      });
    }

    function openTaskOutputPreview(taskId, outputDoc, previewUrl, title, projectName) {
      const resolvedTaskId = String(taskId || "").trim();
      const resolvedDoc = String(outputDoc || "").trim();
      const resolvedTitle = String(title || resolvedDoc || "任务产出").trim();
      const resolvedUrl = String(
        previewUrl || (resolvedTaskId && resolvedDoc
          ? ("/vizo/api/tasks/" + encodeURIComponent(resolvedTaskId) + "/outputs/" + encodeURIComponent(resolvedDoc))
          : "")
      ).trim();
      if (!resolvedUrl) {
        toast("当前产出暂时没有可预览地址。", "error");
        return;
      }
      const activeProject = currentProject();
      state.preview = {
        kind: "task_output",
        project: String(projectName || (activeProject ? activeProject.name : "") || ""),
        path: resolvedUrl,
        binary: false,
        content: "",
        size: 0,
        name: resolvedDoc || resolvedTitle,
        title: resolvedTitle,
        url: resolvedUrl,
        taskId: resolvedTaskId
      };
      if (IS_MOBILE_MODE) openSheet("preview");
      renderPreview();
    }

    function openSessionImagePreview(image) {
      const normalized = normalizeSessionImage(image);
      if (!normalized || !normalized.url) {
        toast("当前图片暂时没有可预览地址。", "error");
        return;
      }
      state.imageViewer = normalized;
      resetImageViewerTransform();
      renderImageViewer();
    }

    function closeImageViewer() {
      state.imageViewer = null;
      resetImageViewerTransform();
      renderImageViewer();
    }

    function imageExtension(image) {
      const ext = String((image && image.extension) || "").trim().replace(/^\.+/, "").toLowerCase();
      if (ext) return ext === "jpeg" ? "jpg" : ext;
      const mime = String((image && image.mime_type) || "").toLowerCase();
      if (mime.includes("jpeg")) return "jpg";
      if (mime.includes("webp")) return "webp";
      if (mime.includes("gif")) return "gif";
      return "png";
    }

    function sanitizeDownloadFileName(value) {
      return String(value || "")
        .replace(/[\\/:*?"<>|]+/g, " ")
        .replace(/\s+/g, " ")
        .trim()
        .slice(0, 80);
    }

    function sessionImageFileName(image) {
      const baseName = sanitizeDownloadFileName(sessionImageTitle(image)) || ("vizo-image-" + String((image && image.id) || "image").slice(0, 16));
      return baseName + "." + imageExtension(image);
    }

    function compactImageFileName(filename) {
      const value = String(filename || "").trim();
      if (value.length <= 28) return value;
      const dotIndex = value.lastIndexOf(".");
      const ext = dotIndex > 0 ? value.slice(dotIndex) : "";
      const stem = dotIndex > 0 ? value.slice(0, dotIndex) : value;
      return stem.slice(0, Math.max(10, 26 - ext.length)).trimEnd() + "..." + ext;
    }

    function triggerBrowserDownload(url, filename) {
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      link.rel = "noreferrer";
      link.style.display = "none";
      document.body.appendChild(link);
      link.click();
      link.remove();
    }

    async function downloadSessionImage(image) {
      const normalized = normalizeSessionImage(image || state.imageViewer);
      if (!normalized || !normalized.url) {
        toast("当前图片暂时没有可下载地址。", "error");
        return;
      }
      const filename = sessionImageFileName(normalized);
      const button = $("imageViewerDownloadBtn");
      if (button) button.disabled = true;
      try {
        const response = await fetch(normalized.url, { credentials: "same-origin" });
        if (!response.ok) throw new Error("download_failed");
        const blob = await response.blob();
        const objectUrl = URL.createObjectURL(blob);
        triggerBrowserDownload(objectUrl, filename);
        window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
        toast("图片下载已开始。", "success");
      } catch (error) {
        try {
          triggerBrowserDownload(normalized.url, filename);
          toast("图片下载已开始。", "success");
        } catch (fallbackError) {
          toast("图片下载失败。", "error");
        }
      } finally {
        if (button) button.disabled = false;
      }
    }

    function resetImageViewerTransform() {
      state.imageViewerTransform = {
        scale: 1,
        x: 0,
        y: 0,
        dragging: false,
        dragPointerId: null,
        dragStartX: 0,
        dragStartY: 0,
        dragOriginX: 0,
        dragOriginY: 0
      };
    }

    function currentImageViewerTransform() {
      if (!state.imageViewerTransform) resetImageViewerTransform();
      return state.imageViewerTransform;
    }

    function clampImageViewerScale(value) {
      const scale = Number(value || 1);
      if (!Number.isFinite(scale)) return 1;
      return Math.min(6, Math.max(0.25, scale));
    }

    function applyImageViewerTransform() {
      const img = $("imageViewerImg");
      const stage = $("imageViewerStage");
      if (!img || !stage) return;
      const transform = currentImageViewerTransform();
      img.style.transform =
        "translate3d(" + transform.x.toFixed(2) + "px," + transform.y.toFixed(2) + "px,0) scale(" + transform.scale.toFixed(3) + ")";
      stage.classList.toggle("is-zoomed", transform.scale > 1.01);
      stage.classList.toggle("is-dragging", !!transform.dragging);
    }

    function zoomImageViewerAt(clientX, clientY, scaleFactor) {
      const stage = $("imageViewerStage");
      if (!stage || !state.imageViewer) return;
      const rect = stage.getBoundingClientRect();
      const transform = currentImageViewerTransform();
      const previousScale = transform.scale;
      const nextScale = clampImageViewerScale(previousScale * scaleFactor);
      if (Math.abs(nextScale - previousScale) < 0.001) return;
      const originX = Number(clientX) - (rect.left + rect.width / 2);
      const originY = Number(clientY) - (rect.top + rect.height / 2);
      transform.x = originX - ((originX - transform.x) / previousScale) * nextScale;
      transform.y = originY - ((originY - transform.y) / previousScale) * nextScale;
      transform.scale = nextScale;
      if (nextScale <= 1.001) {
        transform.x = 0;
        transform.y = 0;
      }
      applyImageViewerTransform();
    }

    function handleImageViewerWheel(event) {
      if (!state.imageViewer) return;
      event.preventDefault();
      const factor = Math.exp(-Number(event.deltaY || 0) * 0.0016);
      zoomImageViewerAt(event.clientX, event.clientY, factor);
    }

    function handleImageViewerPointerDown(event) {
      if (!state.imageViewer) return;
      const transform = currentImageViewerTransform();
      if (transform.scale <= 1.01) return;
      event.preventDefault();
      transform.dragging = true;
      transform.dragPointerId = event.pointerId;
      transform.dragStartX = event.clientX;
      transform.dragStartY = event.clientY;
      transform.dragOriginX = transform.x;
      transform.dragOriginY = transform.y;
      const stage = $("imageViewerStage");
      if (stage && typeof stage.setPointerCapture === "function") {
        stage.setPointerCapture(event.pointerId);
      }
      applyImageViewerTransform();
    }

    function handleImageViewerPointerMove(event) {
      const transform = currentImageViewerTransform();
      if (!transform.dragging || transform.dragPointerId !== event.pointerId) return;
      event.preventDefault();
      transform.x = transform.dragOriginX + event.clientX - transform.dragStartX;
      transform.y = transform.dragOriginY + event.clientY - transform.dragStartY;
      applyImageViewerTransform();
    }

    function endImageViewerDrag(event) {
      const transform = currentImageViewerTransform();
      if (!transform.dragging) return;
      if (event && transform.dragPointerId !== event.pointerId) return;
      const stage = $("imageViewerStage");
      if (stage && event && typeof stage.releasePointerCapture === "function") {
        try {
          stage.releasePointerCapture(event.pointerId);
        } catch (error) {}
      }
      transform.dragging = false;
      transform.dragPointerId = null;
      applyImageViewerTransform();
    }

    function resetImageViewerZoom() {
      if (!state.imageViewer) return;
      resetImageViewerTransform();
      applyImageViewerTransform();
    }

    function renderImageViewer() {
      const viewer = $("imageViewer");
      if (!viewer) return;
      const image = state.imageViewer;
      const downloadButton = $("imageViewerDownloadBtn");
      if (downloadButton) downloadButton.disabled = !image;
      viewer.hidden = !image;
      if (!image) {
        const img = $("imageViewerImg");
        if (img) {
          img.removeAttribute("src");
          img.style.transform = "";
        }
        const stage = $("imageViewerStage");
        if (stage) {
          stage.classList.remove("is-zoomed");
          stage.classList.remove("is-dragging");
        }
        return;
      }
      const title = sessionImageTitle(image);
      const img = $("imageViewerImg");
      const filename = sessionImageFileName(image);
      setText("imageViewerTitle", title || "生成图片");
      setText(
        "imageViewerMeta",
        [
          compactImageFileName(filename),
          image.size ? formatFileSize(image.size) : "",
          image.created_at ? isoTime(image.created_at) : ""
        ].filter(Boolean).join(" · ")
      );
      const meta = $("imageViewerMeta");
      if (meta) meta.title = filename;
      if (img) {
        img.src = image.url;
        img.alt = title || "生成图片";
      }
      applyImageViewerTransform();
    }

    async function refreshTaskState() {
      return livePanel.refreshTaskState();
    }

    function scheduleTaskRefresh(delay) {
      return livePanel.scheduleTaskRefresh(delay);
    }

    async function loadTaskDetail(taskId, projectName) {
      return livePanel.loadTaskDetail(taskId, projectName);
    }

    function taskProjectName(task) {
      const project = currentProject();
      return project ? project.name : String((task && task.project) || "");
    }

    function liveSubTaskCacheKey(task, subName) {
      return [task.task_id, taskProjectName(task), "subtask", subName].join("|");
    }

    function liveStepLogCacheKey(task, stepName, role) {
      return [task.task_id, taskProjectName(task), "step", stepName, role || stepName].join("|");
    }

    function liveSubstepLogCacheKey(task, subId, stepName, role) {
      return [task.task_id, taskProjectName(task), "substep", subId || "", stepName, role || stepName].join("|");
    }

    function liveNodeKey(node) {
      if (!node) return "";
      if (node.type === "substep") {
        return ["substep", node.taskId, node.subId || "", node.id, node.role || node.id].join("|");
      }
      return ["step", node.taskId, node.id, node.role || node.id].join("|");
    }

    function sameLiveNode(left, right) {
      return liveNodeKey(left) !== "" && liveNodeKey(left) === liveNodeKey(right);
    }

    function findTaskSubTask(task, subName) {
      return (task && task.sub_tasks ? task.sub_tasks : []).find((subTask) => {
        const candidate = subTask.name || String(subTask.id || "");
        return candidate === subName;
      }) || null;
    }

    function findCachedSubTask(task, subName) {
      return state.subTasks.get(liveSubTaskCacheKey(task, subName)) || null;
    }

    function buildStepNode(task, step) {
      const stepName = step.name || step.role || "";
      if (!stepName) return null;
      return {
        type: "step",
        taskId: task.task_id,
        id: stepName,
        role: step.role || stepName
      };
    }

    function buildSubstepNode(task, subTask, cachedSubTask, step) {
      const stepName = step.name || step.role || "";
      const subName = subTask.name || String(subTask.id || "");
      if (!stepName || !subName) return null;
      return {
        type: "substep",
        taskId: task.task_id,
        subId: String((cachedSubTask && cachedSubTask.sub_id) || subTask.id || ""),
        subtaskName: subName,
        id: stepName,
        role: step.role || stepName
      };
    }

    function findCurrentStepNode(task) {
      const steps = task && task.steps ? task.steps : [];
      const activeStep = steps.find((step) => step.status === "running" || step.status === "waiting_confirm");
      if (activeStep) return buildStepNode(task, activeStep);
      const taskStatus = String((task && task.status) || "").toLowerCase();
      const currentStepName = String((task && task.current_step) || "").trim();
      if ((taskStatus === "waiting_confirm" || taskStatus === "paused") && currentStepName) {
        const currentStep = steps.find((step) => {
          const stepName = String((step && (step.name || step.role || "")) || "").trim();
          return stepName === currentStepName;
        });
        if (currentStep) return buildStepNode(task, currentStep);
      }
      return null;
    }

    function findCurrentSubstepNode(task) {
      const subTasks = task && task.sub_tasks ? task.sub_tasks : [];
      for (const subTask of subTasks) {
        const subName = subTask.name || String(subTask.id || "");
        if (!subName) continue;
        const cachedSubTask = findCachedSubTask(task, subName);
        if (!cachedSubTask || !cachedSubTask.steps) continue;
        const activeStep = cachedSubTask.steps.find((step) => step.status === "running" || step.status === "waiting_confirm");
        if (!activeStep) continue;
        return buildSubstepNode(task, subTask, cachedSubTask, activeStep);
      }
      return null;
    }

    function firstAvailableLiveNode(task) {
      const currentSubstep = findCurrentSubstepNode(task);
      if (currentSubstep) return currentSubstep;
      const currentStep = findCurrentStepNode(task);
      if (currentStep) return currentStep;
      const firstStep = (task && task.steps ? task.steps : [])[0];
      if (firstStep) return buildStepNode(task, firstStep);
      const firstSubTask = (task && task.sub_tasks ? task.sub_tasks : [])[0];
      if (!firstSubTask) return null;
      const subName = firstSubTask.name || String(firstSubTask.id || "");
      const cachedSubTask = findCachedSubTask(task, subName);
      const firstSubStep = cachedSubTask && cachedSubTask.steps ? cachedSubTask.steps[0] : null;
      return firstSubStep ? buildSubstepNode(task, firstSubTask, cachedSubTask, firstSubStep) : null;
    }

    function isLiveNodeAvailable(task, node) {
      if (!task || !node || node.taskId !== task.task_id) return false;
      if (node.type === "substep") {
        const cachedSubTask = findCachedSubTask(task, node.subtaskName || "");
        if (!cachedSubTask || !cachedSubTask.steps) return false;
        return cachedSubTask.steps.some((step) => {
          const stepName = step.name || step.role || "";
          return stepName === node.id;
        });
      }
      return (task.steps || []).some((step) => {
        const stepName = step.name || step.role || "";
        return stepName === node.id;
      });
    }

    function shouldAutoFollowLiveNode(task, nextNode) {
      if (!task || !nextNode) return false;
      if (!state.selectedLiveNode || state.selectedLiveNode.taskId !== task.task_id) return true;
      if (!isLiveNodeAvailable(task, state.selectedLiveNode)) return true;
      if (state.manualLiveSelection) return false;
      return !sameLiveNode(state.selectedLiveNode, nextNode);
    }

    async function runTaskAction(target) {
      return livePanel.runTaskAction(target);
    }

    async function runConfirmAction(target) {
      return livePanel.runConfirmAction(target);
    }

    async function focusLiveTask() {
      return livePanel.focusLiveTask();
    }

    async function refreshRawLog(reset) {
      const statusNode = $("rawStatusText");
      const contentNode = $("rawLogContent");
      if (!state.activeSessionId || !statusNode || !contentNode) return;
      const offset = reset ? 0 : state.rawLogOffset;
      const payload = await api(
        ROUTE_BASE + "/api/sessions/" + encodeURIComponent(state.activeSessionId) + "/logs?offset=" + encodeURIComponent(offset) + "&limit=65536"
      );
      if (reset) state.rawLogContent = payload.content || "";
      else state.rawLogContent += payload.content || "";
      state.rawLogOffset = Number(payload.next_offset || 0);
      statusNode.textContent = (state.activeSession ? (state.activeSession.name || state.activeSession.id) : "当前会话") +
        " · " + (payload.runtime_kind || "") + " · " + state.rawLogOffset + " bytes";
      contentNode.textContent = state.rawLogContent || "当前会话暂无 raw log。";
      const mobileRaw = $("mobileRawLog");
      if (mobileRaw) mobileRaw.textContent = state.rawLogContent || "当前会话暂无 raw log。";
    }

    function openRawDrawer() {
      const backdrop = $("drawerBackdrop");
      const drawer = $("rawDrawer");
      if (!backdrop || !drawer) return;
      backdrop.classList.add("backdrop-open");
      drawer.classList.add("open");
      refreshRawLog(true).catch(() => {});
      if (state.rawLogHandle) clearInterval(state.rawLogHandle);
      state.rawLogHandle = window.setInterval(() => {
        refreshRawLog(false).catch(() => {});
      }, 1800);
    }

    function closeRawDrawer() {
      const backdrop = $("drawerBackdrop");
      const drawer = $("rawDrawer");
      if (backdrop) backdrop.classList.remove("backdrop-open");
      if (drawer) drawer.classList.remove("open");
      if (state.rawLogHandle) {
        clearInterval(state.rawLogHandle);
        state.rawLogHandle = null;
      }
    }

    function openSheet(kind) {
      const backdrop = $("sheetBackdrop");
      const sheet = $("sheet");
      if (!backdrop || !sheet) return;
      state.sheetKind = kind;
      renderSheet();
      backdrop.classList.add("backdrop-open");
      sheet.classList.add("open");
    }

    function closeSheet() {
      const backdrop = $("sheetBackdrop");
      const sheet = $("sheet");
      state.sheetKind = "";
      if (backdrop) backdrop.classList.remove("backdrop-open");
      if (sheet) sheet.classList.remove("open");
    }

    function updateNewProjectHint() {
      const input = $("newProjectNameInput");
      const pathInput = $("newProjectPathInput");
      const hint = $("newProjectHint");
      if (!hint) return;
      const name = normalizePath(input ? input.value.trim() : "") || "my-project";
      const customPath = normalizeProjectRoot(pathInput ? pathInput.value.trim() : "");
      if (customPath) {
        hint.textContent = "将创建在: " + customPath;
        return;
      }
      hint.textContent = "留空则创建在: " + CREATE_PROJECTS_BASE + "/" + name;
    }

    function renderCreateProjectOptions(preferredPath) {
      const select = $("projectSelect");
      if (!select) return;
      if (!state.projects.length) {
        select.innerHTML = '<option value="">当前还没有项目，请先创建一个项目。</option>';
        select.value = "";
        select.disabled = true;
        syncDialogueDropdown(select);
        return;
      }
      select.disabled = false;
      const selectedPath = String(preferredPath || "");
      const preferred = state.projects.find((project) => project.path === selectedPath)
        || state.projects.find((project) => project.name === state.currentProjectName)
        || state.projects.find((project) => project.name === MAINLINE_PROJECT_NAME)
        || state.projects[0];
      select.innerHTML = state.projects.map((project) => {
        return '<option value="' + escapeHtml(project.path) + '"' +
          htmlAttr("data-description", project.path || "") +
          htmlAttr("data-badge", project.name === MAINLINE_PROJECT_NAME ? "主线" : "") +
          '>' + escapeHtml(project.name) + "</option>";
      }).join("");
      select.value = preferred ? preferred.path : state.projects[0].path;
      syncDialogueDropdown(select);
    }

    function renderCreateProviderOptions() {
      const providerSelect = $("providerSelect");
      if (!providerSelect) return;
      const options = createProviderOptions();
      if (!state.createRuntimeFamily || !options.some((option) => option.value === state.createRuntimeFamily)) {
        state.createRuntimeFamily = options[0] ? options[0].value : "claude_code";
      }
      providerSelect.innerHTML = options.map((option) => {
        return '<option value="' + escapeHtml(option.value) + '"' +
          htmlAttr("data-description", option.description || "") +
          '>' + escapeHtml(option.label) + "</option>";
      }).join("");
      providerSelect.value = state.createRuntimeFamily;
      providerSelect.disabled = options.length <= 1;
      syncDialogueDropdown(providerSelect);
    }

    function renderCreateConnections() {
      const field = $("connectionField");
      const select = $("connectionSelect");
      if (!field || !select) return;
      const family = state.createRuntimeFamily || "claude_code";
      const candidates = connectionsForFamily(family);
      const enabledCandidates = selectableConnectionsForFamily(family);
      if (!candidates.length) {
        state.createConnectionId = "";
        field.hidden = true;
        select.innerHTML = '<option value="">当前默认连接</option>';
        select.disabled = true;
        syncDialogueDropdown(select);
        return;
      }
      const selectedCandidate = candidates.find((connection) => connection.id === state.createConnectionId) || null;
      if (
        !state.createConnectionId ||
        !selectedCandidate ||
        (!isConnectionReadyForMainSession(selectedCandidate) && enabledCandidates.length)
      ) {
        state.createConnectionId = (enabledCandidates[0] && enabledCandidates[0].id) || candidates[0].id || "";
      }
      field.hidden = candidates.length <= 1 && enabledCandidates.length > 0;
      select.innerHTML = candidates.map((conn) => {
        const description = connectionSelectionDescription(conn);
        const badge = connectionAuthStatusBadge(conn);
        return '<option value="' + escapeHtml(conn.id || "") + '"' +
          htmlAttr("data-description", description) +
          htmlAttr("data-badge", badge) +
          (!isConnectionReadyForMainSession(conn) ? " disabled" : "") +
          '>' +
          escapeHtml(conn.name || "未命名连接") +
          "</option>";
      }).join("");
      select.value = state.createConnectionId;
      select.disabled = candidates.length <= 1 && enabledCandidates.length > 0;
      syncDialogueDropdown(select);
    }

    function renderCreateModelOptions() {
      const select = $("modelInput");
      if (!select) return;
      const family = state.createRuntimeFamily || "claude_code";
      const connection = connectionsForFamily(family).find((item) => item.id === state.createConnectionId) || null;
      const options = (CREATE_MODEL_OPTIONS[family] || []).slice();
      if (!options.length) options.push({ value: "", label: "自动选择", description: "使用当前连接的默认模型" });
      if (!options.some((option) => option.value === select.value)) {
        const preferred = preferredCreateModel(family, connection);
        select.innerHTML = options.map((option) => {
          return '<option value="' + escapeHtml(option.value) + '"' +
            htmlAttr("data-description", option.description || "") +
            '>' + escapeHtml(option.label) + "</option>";
        }).join("");
        select.value = options.some((option) => option.value === preferred) ? preferred : options[0].value;
        select.disabled = options.length <= 1;
        syncDialogueDropdown(select);
        return;
      }
      select.innerHTML = options.map((option) => {
        return '<option value="' + escapeHtml(option.value) + '"' +
          htmlAttr("data-description", option.description || "") +
          '>' + escapeHtml(option.label) + "</option>";
      }).join("");
      select.value = options.some((option) => option.value === select.value) ? select.value : options[0].value;
      select.disabled = options.length <= 1;
      syncDialogueDropdown(select);
    }

    function syncCreateForm() {
      renderCreateProviderOptions();
      renderCreateConnections();
      renderCreateModelOptions();
      updateNewProjectHint();
    }

    async function openModal() {
      const backdrop = $("modalBackdrop");
      const modal = $("sessionModal");
      if (!backdrop || !modal) return;
      closeDialogueDropdown(activeDialogueDropdownId);
      await refreshCreateConnectionsFromServer();
      await refreshCreateProjectOptionsFromServer($("projectSelect") ? $("projectSelect").value : "");
      populateCreateForm();
      backdrop.classList.add("backdrop-open");
      modal.classList.add("open");
    }

    function closeModal() {
      const backdrop = $("modalBackdrop");
      const modal = $("sessionModal");
      closeDialogueDropdown(activeDialogueDropdownId);
      if (backdrop) backdrop.classList.remove("backdrop-open");
      if (modal) modal.classList.remove("open");
    }

    function renderTaskActionModal(spec) {
      const title = $("taskActionModalTitle");
      const subtitle = $("taskActionModalSubtitle");
      const message = $("taskActionModalMessage");
      const choiceList = $("taskActionChoiceList");
      const actions = $("taskActionModalActions");
      if (title) title.textContent = String((spec && spec.title) || "操作确认");
      if (subtitle) {
        const subtitleText = String((spec && spec.subtitle) || "");
        subtitle.textContent = subtitleText;
        subtitle.hidden = !subtitleText;
      }
      if (message) message.textContent = String((spec && spec.message) || "");
      taskActionModalOptions = [];
      const renderButtons = (items, attrName) => (items || []).map((item) => {
        const index = taskActionModalOptions.push(item && item.result) - 1;
        return '<button class="' + escapeHtml(item && item.cls || "ghost-btn") + '" type="button" ' +
          attrName + '="' + escapeHtml(String(index)) + '">' +
          escapeHtml(String((item && item.label) || "确认")) + "</button>";
      }).join("");
      if (choiceList) choiceList.innerHTML = renderButtons(spec && spec.choices, "data-task-action-choice");
      if (actions) actions.innerHTML = renderButtons(spec && spec.actions, "data-task-action-result");
    }

    function closeTaskActionModal(result) {
      const backdrop = $("taskActionModalBackdrop");
      const modal = $("taskActionModal");
      if (backdrop) backdrop.classList.remove("backdrop-open");
      if (modal) modal.classList.remove("open");
      taskActionModalOptions = [];
      const resolver = taskActionModalResolver;
      taskActionModalResolver = null;
      if (resolver) resolver(result);
    }

    function requestTaskActionConfirmation(spec) {
      const backdrop = $("taskActionModalBackdrop");
      const modal = $("taskActionModal");
      if (!backdrop || !modal) return Promise.resolve(null);
      if (taskActionModalResolver) closeTaskActionModal(null);
      renderTaskActionModal(spec || {});
      backdrop.classList.add("backdrop-open");
      modal.classList.add("open");
      return new Promise((resolve) => {
        taskActionModalResolver = resolve;
      });
    }

    function populateCreateForm() {
      const projectSelect = $("projectSelect");
      const sessionName = $("sessionNameInput");
      const newProjectInput = $("newProjectInput");
      const newProjectName = $("newProjectNameInput");
      const newProjectPath = $("newProjectPathInput");
      if (sessionName) sessionName.value = "";
      if (newProjectName) newProjectName.value = "";
      if (newProjectPath) newProjectPath.value = "";
      if (newProjectInput) newProjectInput.classList.remove("show");
      if (projectSelect) projectSelect.value = "";
      state.createConnectionId = "";
      state.createRuntimeFamily = state.activeSession && state.activeSession.runtime_family
        ? state.activeSession.runtime_family
        : "";
      renderCreateProjectOptions();
      syncCreateForm();
      syncDialogueDropdowns();
    }

    function renderIdentity() {
      const session = state.activeSession;
      const status = session ? (session.current_turn_status || session.status || "idle") : "idle";
      const meta = [];
      if (session) {
        meta.push('<span class="pill ' + escapeHtml(status) + '">' + escapeHtml(formatStatus(status)) + "</span>");
        if (session.provider_display || session.runtime_label) {
          meta.push('<span class="pill">' + escapeHtml(session.provider_display || session.runtime_label || "Runtime") + "</span>");
        }
        if (session.updated_at || session.created_at) {
          meta.push('<span class="pill">' + escapeHtml(isoTime(session.updated_at || session.created_at || "")) + "</span>");
        }
      } else {
        meta.push('<span class="pill">未选择会话</span>');
      }
      const connected = !!(state.ws && state.ws.readyState === WebSocket.OPEN);
      const identityName = $("identityName");
      if (identityName) identityName.textContent = session ? (session.name || session.id) : "还没有主会话";
      const identityMeta = $("identityMeta");
      if (identityMeta) identityMeta.innerHTML = meta.join("");
      const pill = $("connectionPill");
      if (pill) {
        pill.className = "pill " + (connected ? "connected" : "");
        pill.textContent = connected ? "已连接" : (session ? "重连中" : "未连接");
      }
      const connectionBadge = $("connectionBadge");
      if (connectionBadge) {
        connectionBadge.textContent = connected ? "已连接" : (session ? "重连中" : "未连接");
        connectionBadge.style.color = connected ? "var(--green)" : "var(--text-faint)";
      }
      setText("headerSession", session ? (session.name || session.id) : "还没有主会话");
      setText("mobileConnStatus", connected ? "在线" : (session ? "重连中" : "离线"));
      renderChannelSelect();
      renderModelSettingsSelect();
    }

    function renderEmbeddedSurface() {
      const embeddedShell = $("embeddedShell");
      const workspace = $("workspaceShell");
      const banner = $("taskBanner");
      const open = !!state.embeddedView;
      if (embeddedShell) embeddedShell.hidden = !open;
      if (workspace) workspace.style.display = open ? "none" : "flex";
      if (banner && !IS_MOBILE_MODE) {
        const visible = banner.dataset.taskBannerVisible === "true";
        banner.style.display = !open && visible ? "flex" : "none";
      }
      const settingsEntry = $("openSettingsEntry");
      if (settingsEntry) settingsEntry.classList.toggle("active", state.embeddedView === "settings");
      const agentsEntry = $("openAgentsEntry");
      if (agentsEntry) agentsEntry.classList.toggle("active", state.embeddedView === "agents");
      setText("embeddedViewTitle", state.embeddedView === "agents" ? "智能体中心" : "设置");
      setText(
        "embeddedViewDesc",
        state.embeddedView === "agents"
          ? "全屏查看智能体与角色运行入口，可随时回到主页。"
          : "全屏查看连接、诊断与主会话设置，可随时回到主页。"
      );
      const loading = $("embeddedLoading");
      if (loading) loading.hidden = !open || state.embeddedFrameReady;
    }

    function buildStandaloneConsoleUrl(kind, section) {
      const url = new URL(
        kind === "agents" ? "/vizo/console/dialogue/agents" : "/vizo/console/dialogue/settings",
        window.location.origin
      );
      if (kind !== "agents") {
        url.searchParams.set("section", section || "main-session");
      }
      return url.toString();
    }

    function openEmbeddedView(kind, section) {
      closeModal();
      closeRawDrawer();
      window.location.href = buildStandaloneConsoleUrl(kind, section);
    }

    function closeEmbeddedView() {
      state.embeddedView = "";
      state.embeddedSection = "";
      renderEmbeddedSurface();
    }

    function injectEmbeddedConsoleStyle(doc) {
      if (!doc || !doc.head) return;
      let style = doc.getElementById("dialogueEmbeddedConsoleStyle");
      if (!style) {
        style = doc.createElement("style");
        style.id = "dialogueEmbeddedConsoleStyle";
        doc.head.appendChild(style);
      }
      style.textContent = buildWorkbenchInjectedRules().join("");
    }

    function syncEmbeddedConsoleFrame() {
      const iframe = $("embeddedConsoleFrame");
      if (!iframe || !state.embeddedView || !state.embeddedFrameReady) return;
      try {
        const win = iframe.contentWindow;
        const doc = iframe.contentDocument;
        injectEmbeddedConsoleStyle(doc);
        if (state.embeddedView === "agents" && typeof win.openAgentsView === "function") {
          win.openAgentsView();
        } else if (typeof win.openSettings === "function") {
          win.openSettings(state.embeddedSection || "main-session");
        }
      } catch (error) {}
    }

    function ensureEmbeddedConsoleFrame() {
      const iframe = $("embeddedConsoleFrame");
      if (!iframe) return;
      if (!iframe.dataset.bound) {
        iframe.dataset.bound = "1";
        iframe.addEventListener("load", () => {
          state.embeddedFrameReady = true;
          renderEmbeddedSurface();
          syncEmbeddedConsoleFrame();
        });
      }
      if (iframe.dataset.loaded === "1") return;
      state.embeddedFrameReady = false;
      iframe.dataset.loaded = "1";
      iframe.src = "/vizo/console";
    }

    function renderSessionsPane() {
      const groups = splitSessions(state.sessions);
      function sessionItemHtml(session, history) {
        const active = session.id === state.activeSessionId ? " active" : "";
        const status = sessionStatus(session);
        const providerKind = String(session.runtime_family || session.runtime_kind || "").includes("codex") ||
          String(session.provider_display || "").toLowerCase().includes("openai")
          ? "openai"
          : "anthropic";
        const providerName = session.provider_display ||
          (providerKind === "openai" ? "Codex" : "Claude");
        if (history) {
          const historyMeta = [
            providerName,
            session.display_model || "-",
            formatHistoryDate(session.updated_at || session.created_at || ""),
            eventCountForSession(session) ? (eventCountForSession(session) + " 事件") : ""
          ].filter(Boolean).join(" · ");
          const deleteButton = (
            '<button class="history-delete-btn" type="button" data-delete-session="' + escapeHtml(session.id) + '"' +
            ' aria-label="' + escapeHtml("删除历史会话 " + (session.name || session.id || "")) + '"' +
            ' title="删除历史会话">' +
              '<span class="history-delete-icon" aria-hidden="true">' +
                '<svg viewBox="0 0 16 16" fill="none" focusable="false" aria-hidden="true">' +
                  '<path d="M4 4l8 8M12 4L4 12" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>' +
                "</svg>" +
              "</span>" +
            "</button>"
          );
          return (
            '<article class="history-item' + active + '" data-session-id="' + escapeHtml(session.id) + '">' +
              '<div class="history-row">' +
                '<div class="history-copy">' +
                  '<div class="history-name">' + escapeHtml(session.name || session.id) + '</div>' +
                  '<div class="history-meta">' + escapeHtml(historyMeta) + "</div>" +
                "</div>" +
                deleteButton +
              "</div>" +
            "</article>"
          );
        }
        const age = formatRelativeTime(session.updated_at || session.created_at || "");
        const closeButton = (
          '<button class="session-close-btn" type="button" data-close-session="' + escapeHtml(session.id) + '"' +
          (sessionCanClose(session) ? "" : ' disabled aria-disabled="true"') +
          ' aria-label="' + escapeHtml(sessionCanClose(session) ? "关闭会话" : "当前 turn 运行中，暂时不能关闭") + '"' +
          ' title="' + escapeHtml(sessionCanClose(session) ? "关闭会话" : "当前 turn 运行中，暂时不能关闭") + '">' +
          '<span class="session-close-icon" aria-hidden="true">' +
            '<svg viewBox="0 0 16 16" fill="none" focusable="false" aria-hidden="true">' +
              '<path d="M4 4l8 8M12 4L4 12" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>' +
            "</svg>" +
          "</span>" +
          "</button>"
        );
        return (
          '<article class="session-item' + active + '" data-session-id="' + escapeHtml(session.id) + '">' +
            '<div class="session-item-head">' +
              '<div class="session-item-title">' +
                '<div class="session-name">' + escapeHtml(session.name || session.id) + "</div>" +
              "</div>" +
              '<div class="session-item-actions">' +
                (age ? ('<span class="session-age">' + escapeHtml(age) + "</span>") : "") +
                closeButton +
              "</div>" +
            "</div>" +
            '<div class="session-cwd">' + escapeHtml(session.cwd || "") + "</div>" +
            '<div class="session-meta">' +
              '<span class="session-status ' + escapeHtml(status) + '">' + escapeHtml(formatStatus(status)) + "</span>" +
              '<span class="provider-badge ' + providerKind + '">' + escapeHtml(providerName) + "</span>" +
              '<span class="tag">' + escapeHtml(session.display_model || "-") + "</span>" +
            "</div>" +
          "</article>"
        );
      }
      const activeHtml = groups.active.length
        ? groups.active.map((session) => sessionItemHtml(session, false)).join("")
        : "";
      const historyHtml = groups.history.length
        ? groups.history.map((session) => sessionItemHtml(session, true)).join("")
        : "";
      const sidebarSignature = JSON.stringify({
        active: groups.active.map((session) => [
          session.id || "",
          session.name || "",
          sessionStatus(session),
          session.updated_at || session.created_at || "",
          eventCountForSession(session),
          session.display_model || ""
        ]),
        history: groups.history.map((session) => [
          session.id || "",
          session.name || "",
          sessionStatus(session),
          session.updated_at || session.created_at || "",
          eventCountForSession(session),
          session.display_model || ""
        ]),
        activeSessionId: state.activeSessionId || ""
      });
      const previousSignature = state.sessionPaneSignature || "";
      const shouldRestoreSidebarScroll = !!previousSignature;
      const emptyHtml = '<div class="empty-state sidebar-panel-empty">当前还没有会话，点击上方“新建会话”开始。</div>';
      const activeTarget = $("activeSessionList");
      const activeSnapshot = shouldRestoreSidebarScroll ? snapshotScrollNode(activeTarget) : null;
      if (activeTarget && previousSignature !== sidebarSignature) activeTarget.innerHTML = activeHtml;
      const activeSection = $("activeSessionSection");
      if (activeSection) activeSection.classList.toggle("session-section-fill", groups.active.length > 0 && groups.history.length === 0);
      if (activeSection) activeSection.hidden = groups.active.length === 0;
      const historyTarget = $("historySessionList");
      const historySnapshot = shouldRestoreSidebarScroll ? snapshotScrollNode(historyTarget) : null;
      if (historyTarget && previousSignature !== sidebarSignature) historyTarget.innerHTML = historyHtml;
      const historySection = $("historySessionSection");
      if (historySection) historySection.classList.toggle("session-section-fill", groups.history.length > 0 && groups.active.length === 0);
      if (historySection) historySection.hidden = groups.history.length === 0;
      const emptyState = $("sessionPaneEmpty");
      if (emptyState) emptyState.hidden = groups.active.length + groups.history.length > 0;
      const mobileTarget = $("desktopSessionsPane");
      if (mobileTarget) {
        const sections = [];
        if (groups.active.length) {
          sections.push(
            '<div class="session-section-label">活跃</div>' +
            '<div class="session-list">' + activeHtml + "</div>"
          );
        }
        if (groups.history.length) {
          sections.push(
            '<div class="session-section-label">历史</div>' +
            '<div class="session-list history-list">' + historyHtml + "</div>"
          );
        }
        if (previousSignature !== sidebarSignature) {
          mobileTarget.innerHTML = sections.length ? sections.join("") : emptyHtml;
        }
      }
      if (shouldRestoreSidebarScroll) {
        restoreScrollNode(activeTarget, activeSnapshot);
        restoreScrollNode(historyTarget, historySnapshot);
      } else {
        if (activeTarget) activeTarget.scrollTop = 0;
        if (historyTarget) historyTarget.scrollTop = 0;
      }
      state.sessionPaneSignature = sidebarSignature;
    }

    function snapshotScrollNode(node) {
      if (!node) return null;
      const maxScrollTop = Math.max(0, node.scrollHeight - node.clientHeight);
      const scrollTop = Math.max(0, node.scrollTop || 0);
      return {
        scrollTop,
        stickToBottom: maxScrollTop - scrollTop <= 24
      };
    }

    function restoreScrollNode(node, snapshot) {
      if (!node || !snapshot) return;
      const maxScrollTop = Math.max(0, node.scrollHeight - node.clientHeight);
      node.scrollTop = snapshot.stickToBottom ? maxScrollTop : Math.min(snapshot.scrollTop || 0, maxScrollTop);
    }

    function updateTimelineStickToBottom(node) {
      const timeline = node || $("timeline");
      if (!timeline) return;
      const maxScrollTop = Math.max(0, timeline.scrollHeight - timeline.clientHeight);
      const scrollTop = Math.max(0, timeline.scrollTop || 0);
      state.timelineStickToBottom = maxScrollTop - scrollTop <= 32;
      updateTimelineBottomButton(timeline);
    }

    function updateTimelineBottomButton(node) {
      const button = $("scrollTimelineBottomBtn");
      if (!button) return;
      const timeline = node || $("timeline");
      const maxScrollTop = timeline ? Math.max(0, timeline.scrollHeight - timeline.clientHeight) : 0;
      const scrollTop = timeline ? Math.max(0, timeline.scrollTop || 0) : 0;
      const hasScrollableHistory = maxScrollTop > 32;
      const awayFromBottom = maxScrollTop - scrollTop > 48;
      button.classList.toggle("visible", !!state.activeSession && hasScrollableHistory && awayFromBottom);
    }

    function syncSidebarLayout() {
      const content = document.querySelector(".sidebar-content");
      if (!content) return;
      content.style.removeProperty("--sidebar-primary-max");
      const utility = content.querySelector(".sidebar-utility-nav");
      const utilityHeight = utility && !utility.hidden ? utility.offsetHeight : 0;
      const available = content.clientHeight - utilityHeight - (utilityHeight ? 14 : 0);
      if (available > 0) {
        content.style.setProperty("--sidebar-primary-max", available + "px");
      }
    }

    function renderFilesPane() {
      const project = currentProject();
      function renderTree(path) {
        const entries = state.fileTreeCache.get(path);
        if (!entries) {
          return '<div class="empty-state">正在加载项目文件...</div>';
        }
        if (!entries.length) {
          return '<div class="empty-state">当前目录下没有可显示文件。</div>';
        }
        return entries.map((entry) => {
          if (entry.type === "dir") {
            const expanded = state.expandedDirs.has(entry.path);
            return (
              '<div class="tree-node">' +
                '<div class="tree-dir' + (expanded ? " expanded" : "") + '" data-tree-dir="' + escapeHtml(entry.path) + '">' +
                  '<span class="tree-icon">' + (expanded ? "&#9660;" : "&#9654;") + "</span>" +
                  '<span class="tree-label">' + escapeHtml(entry.name) + '/</span>' +
                "</div>" +
                (expanded ? ('<div class="tree-children">' + renderTree(entry.path) + "</div>") : "") +
              "</div>"
            );
          }
          const active = state.preview && state.preview.path === entry.path ? " active-file" : "";
          return (
            '<div class="tree-node"><div class="tree-file' + active + '" data-file-entry="' + escapeHtml(JSON.stringify(entry)) + '">' +
              '<span class="tree-icon">&#9642;</span><span class="tree-label">' + escapeHtml(entry.name) + "</span></div></div>"
          );
        }).join("");
      }

      const treeHtml = project
        ? renderTree("")
        : '<div class="empty-state">请先切到一个会话所属项目。</div>';
      setText("treeCwd", project ? project.path : "等待匹配项目...");
      const treeTarget = $("fileTree");
      if (treeTarget) treeTarget.innerHTML = treeHtml;
      const mobileTarget = $("desktopFilesPane");
      if (mobileTarget) {
        mobileTarget.innerHTML =
          '<div class="tree-cwd">' + escapeHtml(project ? project.path : "请先切到一个会话所属项目。") + "</div>" +
          '<div class="file-tree">' + treeHtml + "</div>";
      }
    }

    function renderPreview() {
      const target = $("previewPane");
      if (!target) return;
      state.previewRenderToken = Number(state.previewRenderToken || 0) + 1;
      const previewRenderToken = state.previewRenderToken;
      const previewPanel = $("previewPanel");
      const splitHandle = $("splitHandle");
      const openBrowserBtn = $("openPreviewBrowserBtn");
      setText("previewFileName", state.preview ? state.preview.name : "文件预览");
      const previewLang = $("previewLang");
      const previewLangText = state.preview
        ? (
            state.preview.kind === "task_output"
              ? ""
              : (state.preview.path && state.preview.path.includes(".") ? basename(state.preview.path).split(".").pop() : "preview")
          )
        : "preview";
      setText("previewLang", previewLangText);
      if (previewLang) previewLang.hidden = !previewLangText;
      if (!state.preview) {
        if (previewPanel) previewPanel.classList.remove("open");
        if (splitHandle) splitHandle.classList.remove("visible");
        if (openBrowserBtn) openBrowserBtn.hidden = true;
        cleanupPreviewPanelResize();
        target.innerHTML = '<div class="preview-placeholder">从左侧文件菜单选择文件后，在这里查看代码与文档片段。</div>';
        return;
      }
      applyPreviewPanelWidth(readPreviewPanelWidth());
      if (previewPanel) previewPanel.classList.add("open");
      if (splitHandle) splitHandle.classList.add("visible");
      if (openBrowserBtn) openBrowserBtn.hidden = !state.preview.url;
      if (state.preview.url) {
        if (state.preview.kind === "file" && state.preview.render_mode === "iframe") {
          renderPreviewUrlFallback(target, state.preview);
        } else if (isSameOriginPreviewUrl(state.preview.url)) {
          renderInlineTaskOutputPreview(state.preview, previewRenderToken);
        } else {
          renderPreviewUrlFallback(target, state.preview);
        }
        return;
      }
      target.innerHTML =
        '<div class="preview-content">' +
          '<strong class="preview-title">' + escapeHtml(state.preview.name) + '</strong>' +
          '<div class="preview-path">' + escapeHtml(state.preview.path) + '</div>' +
          '<div class="preview-meta">项目 ' + escapeHtml(state.preview.project) +
            (state.preview.line ? ' · 行 ' + escapeHtml(String(state.preview.line)) : "") +
            ' · ' + escapeHtml(String(state.preview.size || 0)) + ' bytes</div>' +
          (state.preview.binary
            ? '<div class="preview-placeholder">该文件是二进制或超大文件，当前仅展示元信息。</div>'
            : '<pre class="preview-code">' + escapeHtml(state.preview.content || "") + "</pre>") +
        "</div>";
    }

    function renderTaskBanner() {
      return livePanel.renderTaskBanner();
    }

    function pendingSessionInteraction(session) {
      const pending = session && session.pending_interaction;
      if (!pending || !pending.request_id) return null;
      if (sessionStatus(session) === "waiting_interaction") return pending;
      return String(pending.status || "").trim() === "pending" ? pending : null;
    }

    function interactionActionLabel(option) {
      const action = String((option && (option.id || option.action)) || "").trim();
      const explicit = String((option && option.label) || "").trim();
      if (explicit) return explicit;
      const mapping = {
        approve_once: "允许一次",
        approve_prefix: "允许同前缀",
        approve_session: "本会话允许",
        approve_always: "总是允许",
        deny: "拒绝"
      };
      return mapping[action] || action || "提交";
    }

    function renderPendingInteraction() {
      const target = $("sessionInteraction");
      if (!target) return;
      const pending = pendingSessionInteraction(state.activeSession);
      if (!pending) {
        target.hidden = true;
        target.innerHTML = "";
        return;
      }
      const options = Array.isArray(pending.options) && pending.options.length
        ? pending.options
        : (pending.available_actions || []).map((action) => ({ id: action }));
      const needsText = !!pending.requires_text || options.some((item) => !!item.requires_text);
      const command = pending.command || pending.tool_name || "";
      const details = pending.details || "";
      target.hidden = false;
      target.innerHTML =
        '<div class="session-interaction-head">' +
          '<div class="session-interaction-title">' + escapeHtml(pending.title || "需要确认") + '</div>' +
          '<div class="session-interaction-kind">' + escapeHtml(pending.interaction_kind || "interaction") + '</div>' +
        '</div>' +
        '<div class="session-interaction-summary">' + escapeHtml(pending.summary || "当前会话正在等待你的选择。") + '</div>' +
        (command ? '<div class="session-interaction-command">' + escapeHtml(command) + '</div>' : "") +
        (details ? '<div class="session-interaction-details">' + escapeHtml(details) + '</div>' : "") +
        (needsText ? '<textarea class="session-interaction-feedback" id="sessionInteractionText" placeholder="补充说明"></textarea>' : "") +
        '<div class="session-interaction-actions">' +
          options.map((option) => {
            const action = String(option.id || option.action || "").trim();
            if (!action) return "";
            const cls = action === "deny" ? "ghost-btn" : "btn";
            return '<button class="' + cls + '" type="button" data-session-interaction-action="' +
              escapeHtml(action) + '">' + escapeHtml(interactionActionLabel(option)) + '</button>';
          }).join("") +
        '</div>';
    }

    async function submitSessionInteraction(action) {
      const session = state.activeSession;
      const pending = pendingSessionInteraction(session);
      if (!session || !pending) {
        toast("当前会话没有待处理的交互请求。", "error");
        return;
      }
      const textNode = $("sessionInteractionText");
      const payload = await api(ROUTE_BASE + "/api/sessions/" + encodeURIComponent(session.id) + "/interaction", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          request_id: pending.request_id,
          action: action,
          text: textNode ? textNode.value : "",
          expected_runtime: session.runtime_family || session.runtime_kind || ""
        })
      });
      if (payload.session) mergeSessionSnapshot(payload.session);
      renderSessionSurfaces({ includeTimeline: true, includeLivePanel: true });
      if (payload.message) toast(payload.message, "success");
    }

    function renderTimeline() {
      const result = livePanel.renderTimeline();
      updateTimelineBottomButton();
      return result;
    }

    function renderLivePanel() {
      return livePanel.renderLivePanel();
    }

    function compactTaskText(value, maxLength, fallback) {
      let text = String(value || fallback || "").replace(/\s+/g, " ").trim();
      if (!text) text = String(fallback || "").trim();
      const limit = Number(maxLength || 0);
      if (limit > 0 && text.length > limit) return text.slice(0, limit);
      return text;
    }

    function taskCardTitle(task) {
      return compactTaskText(
        task && (task.task_title || task.task_name || task.summary_title || task.title || task.description || task.id),
        20,
        "未命名任务"
      );
    }

    function taskCardSummary(task) {
      return compactTaskText(
        task && (task.task_summary || task.summary || ""),
        60,
        "查看该任务的步骤、日志与确认状态。"
      );
    }

    function renderMobileTaskScreen() {
      const target = $("mobileTaskScreen");
      if (!IS_MOBILE_MODE || !target) return;
      target.style.display = state.mobileTab === "tasks" ? "block" : "none";
      if (state.mobileTab !== "tasks") return;
      const taskCards = state.tasks.length
        ? state.tasks.map((task) => {
            const active = state.taskDetail && state.taskDetail.task_id === task.id ? " active" : "";
            return (
              '<article class="task-item' + active + '" data-open-task="' + escapeHtml(task.id) + '">' +
                '<div class="task-row">' +
                  '<strong class="task-name">' + escapeHtml(taskCardTitle(task)) + '</strong>' +
                  '<span class="pill ' + escapeHtml(task.status || "") + '">' + escapeHtml(formatStatus(task.status)) + '</span>' +
                '</div>' +
                '<div class="task-desc">' + escapeHtml(taskCardSummary(task)) + '</div>' +
              "</article>"
            );
          }).join("")
        : '<div class="empty-state">当前项目下没有历史任务。</div>';
      target.innerHTML =
        '<div class="me-stack">' +
          '<div class="group-title">任务列表</div>' +
          taskCards +
          '<div class="helper">点击任务卡片后，以 overlay / sheet 查看任务直播和确认操作。</div>' +
        '</div>';
    }

    function renderMeScreen() {
      const target = $("mobileMeScreen");
      if (!IS_MOBILE_MODE || !target) return;
      target.style.display = state.mobileTab === "me" ? "block" : "none";
      if (state.mobileTab !== "me") return;
      const session = state.activeSession;
      target.innerHTML =
        '<div class="me-stack">' +
          '<article class="me-card">' +
            '<div class="group-title">我的主会话</div>' +
            '<strong class="task-name">' + escapeHtml(session ? (session.name || session.id) : "未选择会话") + '</strong>' +
            '<div class="task-desc">' + escapeHtml(session ? (session.provider_display || "") : "先创建或切换一个主会话。") + '</div>' +
            '<div class="task-meta">' +
              '<span class="pill">' + escapeHtml(session ? (session.display_model || "-") : "-") + '</span>' +
              '<span class="pill">' + escapeHtml(state.currentProjectName || "未匹配项目") + '</span>' +
            '</div>' +
          '</article>' +
          '<article class="me-card">' +
            '<div class="group-title">快捷入口</div>' +
            '<div style="display:flex; flex-wrap:wrap; gap:8px;">' +
              '<button class="ghost-btn" type="button" data-open-sheet="sessions">会话</button>' +
              '<button class="ghost-btn" type="button" data-open-sheet="files">文件</button>' +
              '<button class="ghost-btn" type="button" data-open-raw="1">Raw Log</button>' +
              '<a class="link-btn" href="/vizo/console#settings/main-session" target="_blank" rel="noopener">设置</a>' +
            '</div>' +
          '</article>' +
        '</div>';
    }

    function renderSheet() {
      const body = $("sheetBody");
      const title = $("sheetTitle");
      if (!body || !title) return;
      if (state.sheetKind === "sessions") {
        title.textContent = "会话";
        body.innerHTML =
          '<div style="display:flex; justify-content:flex-end; margin-bottom:8px;">' +
            '<button class="btn" type="button" data-open-create="1">新建会话</button>' +
          '</div>' +
          ($("desktopSessionsPane").innerHTML || '<div class="empty-state">暂无会话。</div>');
      } else if (state.sheetKind === "files") {
        title.textContent = "文件";
        body.innerHTML = $("desktopFilesPane").innerHTML || '<div class="empty-state">暂无文件。</div>';
      } else if (state.sheetKind === "task") {
        title.textContent = "任务详情";
        body.innerHTML = $("livePanelBody").innerHTML || '<div class="empty-state">暂无任务详情。</div>';
      } else if (state.sheetKind === "preview") {
        title.textContent = "文件预览";
        body.innerHTML = $("previewPane").innerHTML || '<div class="empty-state">暂无文件预览。</div>';
      } else {
        title.textContent = "详情";
        body.innerHTML = '<div class="empty-state">没有可展示内容。</div>';
      }
    }

    function renderComposer() {
      const input = $("composerInput");
      const sendButton = $("sendBtn");
      const attachButton = $("composerAttachBtn");
      const fileInput = $("composerFileInput");
      if (!input || !sendButton) return;
      const session = state.activeSession;
      const disabled = !session;
      const interruptible = sessionIsInterruptible(session) && !composerHasSendableContent(input);
      const interruptMode = interruptModeForSession(session);
      const sendIcon = sendButton.querySelector(".composer-svg-icon");
      input.disabled = disabled;
      sendButton.disabled = disabled;
      sendButton.classList.toggle("interrupt-mode", interruptible);
      sendButton.classList.toggle("force-mode", interruptMode === "force");
      sendButton.setAttribute("aria-label", interruptible ? (interruptMode === "force" ? "强制停止" : "停止当前回复") : (sessionIsInterruptible(session) ? "发送到队列" : "发送"));
      sendButton.title = interruptible
        ? (interruptMode === "force" ? "强制停止当前回复" : "停止当前回复")
        : (sessionIsInterruptible(session) ? "发送到队列" : "发送");
      if (sendIcon) {
        sendIcon.src = interruptible
          ? "/vizo/static/icons/material-design/outlined/stop.svg"
          : "/vizo/static/icons/material-design/outlined/arrow_upward.svg";
      }
      if (attachButton) attachButton.disabled = disabled;
      if (fileInput) fileInput.disabled = disabled;
      renderComposerAttachments();
      renderComposerQueue();
    }

    function renderComposerQueue() {
      const host = $("composerQueue");
      if (!host) return;
      const items = pendingMessageItems(state.activeSession);
      host.hidden = !items.length;
      if (!items.length) {
        host.innerHTML = "";
        return;
      }
      host.innerHTML = items.map((item, index) => {
        const queueId = String(item.queue_id || "");
        const attachments = Array.isArray(item.attachments) ? item.attachments : [];
        const attachmentText = attachments.length ? " · " + attachments.length + " 个附件" : "";
        return '<div class="composer-queue-item" data-pending-queue-id="' + escapeHtml(queueId) + '">' +
          '<div class="composer-queue-main">' +
            '<div class="composer-queue-label">排队消息 ' + escapeHtml(String(index + 1)) + attachmentText + '</div>' +
            '<div class="composer-queue-text" title="' + escapeHtml(pendingInputText(item)) + '">' + escapeHtml(pendingInputText(item)) + '</div>' +
          '</div>' +
          '<div class="composer-queue-actions">' +
            '<button class="composer-queue-action primary" type="button" data-pending-send-now="' + escapeHtml(queueId) + '">立即发送</button>' +
            '<button class="composer-queue-action danger" type="button" data-pending-delete="' + escapeHtml(queueId) + '">删除</button>' +
          '</div>' +
        '</div>';
      }).join("");
    }

    function renderMobileDialogueView() {
      if (!IS_MOBILE_MODE) return;
      const content = $("contentArea");
      const raw = $("mobileRawLog");
      if (!content || !raw) return;
      document.querySelectorAll("[data-mobile-view]").forEach((button) => {
        button.classList.toggle("active", button.getAttribute("data-mobile-view") === state.mobileDialogueView);
      });
      content.classList.toggle("raw-mode", state.mobileDialogueView === "raw");
      raw.textContent = state.rawLogContent || "等待加载 raw log...";
    }

    function renderTabs() {
      if (!IS_MOBILE_MODE) return;
      document.querySelectorAll("[data-mobile-tab]").forEach((button) => {
        button.classList.toggle("active", button.getAttribute("data-mobile-tab") === state.mobileTab);
      });
      const timeline = $("timeline");
      const composer = $("composerForm");
      const banner = $("taskBanner");
      const toolbar = $("mobileQuickToolbar");
      const modeTabs = document.querySelector(".mode-tabs");
      if (timeline) timeline.style.display = state.mobileTab === "dialogue" ? "block" : "none";
      if (composer) composer.style.display = state.mobileTab === "dialogue" ? "block" : "none";
      if (banner) {
        const visible = banner.dataset.taskBannerVisible === "true";
        banner.style.display = state.mobileTab === "dialogue" && visible ? "flex" : "none";
      }
      if (toolbar) toolbar.style.display = state.mobileTab === "dialogue" ? "flex" : "none";
      if (modeTabs) modeTabs.style.display = state.mobileTab === "dialogue" ? "flex" : "none";
      renderMobileTaskScreen();
      renderMeScreen();
      renderMobileDialogueView();
    }

    function resetLivePanelState() {
      return livePanel.resetState();
    }

    function handleLivePanelClick(event) {
      return livePanel.handleClick(event);
    }

    function handleLivePanelInput(event) {
      return livePanel.handleInput(event);
    }

    livePanel = createDialogueLivePanelComponent({
      state,
      ROUTE_BASE,
      IS_MOBILE_MODE,
      $,
      api,
      toast,
      escapeHtml,
      formatCurrency,
      formatDuration,
      formatStatus,
      isoTime,
      currentProject,
      renderAll,
      renderTaskSurfaces,
      renderMobileTaskScreen,
      openSheet,
      openTaskOutputPreview,
      isDialogueLocalPathCandidate,
      resolveDisplayTask,
      resolveLiveSwitchState,
      computeTaskElapsed,
      resolveKnownTask,
      resolveTaskActionButtons,
      resolveConfirmButtons,
      renderTaskActionSection,
      renderConfirmControls,
      requestTaskActionConfirmation,
      resolveSelectedLiveNodeData,
      taskProjectName,
      liveSubTaskCacheKey,
      liveStepLogCacheKey,
      liveSubstepLogCacheKey,
      sameLiveNode,
      findCachedSubTask,
      buildStepNode,
      buildSubstepNode,
      findCurrentStepNode,
      findCurrentSubstepNode,
      firstAvailableLiveNode,
      isLiveNodeAvailable,
      shouldAutoFollowLiveNode,
      renderStepMetaBadges,
      getConfirmDraft,
      isConfirmEditorOpen,
      openConfirmEditor,
      closeConfirmEditor,
      clearConfirmDraft,
      setConfirmDraft,
      nextTaskRefreshDelay,
      setLivePanelVisibility
    });

    function setLivePanelVisibility(visible) {
      state.livePanelVisible = !!visible;
      try {
        localStorage.setItem(LIVE_PANEL_VISIBILITY_KEY, state.livePanelVisible ? "true" : "false");
      } catch (error) {}
    }

    function renderAll() {
      renderIdentity();
      renderEmbeddedSurface();
      renderSessionsPane();
      renderFilesPane();
      renderPreview();
      renderTaskBanner();
      renderPendingInteraction();
      renderTimeline();
      renderSessionImageRail();
      renderLivePanel();
      renderComposer();
      renderImageViewer();
      renderMobileDialogueView();
      renderTabs();
      if (state.sheetKind) renderSheet();
      const livePanel = $("livePanel");
      if (livePanel) livePanel.classList.toggle("open", state.livePanelVisible);
      const liveToggle = $("toggleLiveBtn");
      if (liveToggle) liveToggle.classList.toggle("active", !state.embeddedView && state.livePanelVisible);
      syncDialogueDropdowns();
      window.requestAnimationFrame(syncSidebarLayout);
    }

    function renderSessionSurfaces(options) {
      const includeTimeline = !options || options.includeTimeline !== false;
      const includeLivePanel = !!(options && options.includeLivePanel);
      renderIdentity();
      renderSessionsPane();
      renderPendingInteraction();
      renderComposer();
      if (includeTimeline) {
        renderTimeline();
        renderSessionImageRail();
      }
      if (includeLivePanel) renderLivePanel();
      if (IS_MOBILE_MODE) {
        renderMeScreen();
        renderMobileDialogueView();
        renderTabs();
      }
      if (state.sheetKind === "sessions" || state.sheetKind === "task") renderSheet();
      window.requestAnimationFrame(syncSidebarLayout);
    }

    function renderTaskSurfaces() {
      renderTaskBanner();
      renderTimeline();
      renderLivePanel();
      renderComposer();
      if (IS_MOBILE_MODE) {
        renderMobileTaskScreen();
        renderMeScreen();
        renderMobileDialogueView();
        renderTabs();
      }
      if (state.sheetKind === "task") renderSheet();
    }

    function bindEvents() {
      const refreshCreateProjectsOnDemand = (preferredValue) => {
        refreshCreateProjectOptionsFromServer(preferredValue || "")
          .then(() => syncDialogueDropdown("projectSelect"))
          .catch((error) => toast(error.message, "error"));
      };
      bindIfPresent("newSessionBtn", "click", () => {
        openModal().catch((error) => toast(error.message, "error"));
      });
      bindIfPresent("newSessionQuickBtn", "click", () => {
        openModal().catch((error) => toast(error.message, "error"));
      });
      bindIfPresent("toggleNewProjectBtn", "click", () => {
        const panel = $("newProjectInput");
        if (!panel) return;
        panel.classList.toggle("show");
        updateNewProjectHint();
      });
      bindIfPresent("newProjectNameInput", "input", updateNewProjectHint);
      bindIfPresent("newProjectPathInput", "input", updateNewProjectHint);
      bindIfPresent("providerSelect", "change", (event) => {
        state.createRuntimeFamily = event.target.value || "claude_code";
        state.createConnectionId = "";
        renderCreateConnections();
        renderCreateModelOptions();
      });
      bindIfPresent("connectionSelect", "change", (event) => {
        state.createConnectionId = event.target.value || "";
        renderCreateModelOptions();
      });
      bindIfPresent("projectSelect", "focus", (event) => {
        refreshCreateProjectsOnDemand(event.target.value || "");
      });
      bindIfPresent("projectSelect", "pointerdown", (event) => {
        refreshCreateProjectsOnDemand(event.target.value || "");
      });
      bindIfPresent("projectSelect", "vizo-select:beforeopen", (event) => {
        refreshCreateProjectsOnDemand(event.target.value || "");
      });
      bindIfPresent("confirmCreateBtn", "click", () => {
        createSession().catch((error) => toast(error.message, "error"));
      });
      bindIfPresent("cancelCreateBtn", "click", closeModal);
      bindIfPresent("closeModalBtn", "click", closeModal);
      bindIfPresent("modalBackdrop", "click", closeModal);
      bindIfPresent("closeTaskActionModalBtn", "click", () => closeTaskActionModal(null));
      bindIfPresent("taskActionModalBackdrop", "click", () => closeTaskActionModal(null));
      bindIfPresent("taskActionChoiceList", "click", (event) => {
        const button = event.target.closest("[data-task-action-choice]");
        if (!button) return;
        const index = Number(button.getAttribute("data-task-action-choice") || "-1");
        closeTaskActionModal(index >= 0 ? taskActionModalOptions[index] : null);
      });
      bindIfPresent("taskActionModalActions", "click", (event) => {
        const button = event.target.closest("[data-task-action-result]");
        if (!button) return;
        const index = Number(button.getAttribute("data-task-action-result") || "-1");
        closeTaskActionModal(index >= 0 ? taskActionModalOptions[index] : null);
      });
      bindIfPresent("openRawBtn", "click", openRawDrawer);
      bindIfPresent("closeRawBtn", "click", closeRawDrawer);
      bindIfPresent("drawerBackdrop", "click", closeRawDrawer);
      bindIfPresent("refreshRawBtn", "click", () => refreshRawLog(true).catch((error) => toast(error.message, "error")));
      bindIfPresent("openSheetSessionsBtn", "click", () => openSheet("sessions"));
      bindIfPresent("openSheetFilesBtn", "click", () => openSheet("files"));
      bindIfPresent("closeSheetBtn", "click", closeSheet);
      bindIfPresent("sheetBackdrop", "click", closeSheet);
      bindIfPresent("channelSelect", "change", () => {
        switchChannel().catch((error) => toast(error.message, "error"));
      });
      bindIfPresent("modelSettingsSelect", "change", () => {
        switchModelSettings();
      });
      bindIfPresent("openSettingsEntry", "click", () => {
        openEmbeddedView("settings", "main-session");
      });
      bindIfPresent("openAgentsEntry", "click", () => {
        openEmbeddedView("agents");
      });
      bindIfPresent("closeEmbeddedViewBtn", "click", closeEmbeddedView);
      bindIfPresent("refreshTaskBtn", "click", () => refreshTaskState().catch((error) => toast(error.message, "error")));
      bindIfPresent("clearPreviewBtn", "click", () => {
        state.preview = null;
        renderPreview();
      });
      bindIfPresent("openPreviewBrowserBtn", "click", openPreviewInBrowser);
      bindIfPresent("splitHandle", "pointerdown", beginPreviewPanelResize);
      bindIfPresent("imageViewerBackdrop", "click", closeImageViewer);
      bindIfPresent("imageViewerDownloadBtn", "click", () => {
        downloadSessionImage(state.imageViewer).catch(() => toast("图片下载失败。", "error"));
      });
      const imageViewerStage = $("imageViewerStage");
      if (imageViewerStage) {
        imageViewerStage.addEventListener("wheel", handleImageViewerWheel, { passive: false });
        imageViewerStage.addEventListener("pointerdown", handleImageViewerPointerDown);
        imageViewerStage.addEventListener("pointermove", handleImageViewerPointerMove);
        imageViewerStage.addEventListener("pointerup", endImageViewerDrag);
        imageViewerStage.addEventListener("pointercancel", endImageViewerDrag);
        imageViewerStage.addEventListener("dblclick", resetImageViewerZoom);
      }
      bindIfPresent("collapseSidebar", "click", () => {
        const sidebar = $("sidebar");
        if (sidebar) sidebar.classList.add("collapsed");
      });
      bindIfPresent("expandSidebar", "click", () => {
        const sidebar = $("sidebar");
        if (sidebar) sidebar.classList.remove("collapsed");
      });
      bindIfPresent("toggleLiveBtn", "click", () => {
        closeModal();
        closeRawDrawer();
        if (state.embeddedView) {
          state.embeddedView = "";
          state.embeddedSection = "";
          setLivePanelVisibility(true);
          renderAll();
          return;
        }
        setLivePanelVisibility(!state.livePanelVisible);
        renderAll();
      });
      bindIfPresent("closeRightPanelBtn", "click", () => {
        setLivePanelVisibility(false);
        renderAll();
      });
      bindIfPresent("taskBanner", "click", (event) => {
        if (event.target.closest("[data-task-banner-dismiss]")) return;
        if (IS_MOBILE_MODE && (state.currentTask || state.taskDetail)) openSheet("task");
      });
      bindIfPresent("composerForm", "submit", (event) => {
        event.preventDefault();
        sendMessage().catch((error) => toast(error.message, "error"));
      });
      bindIfPresent("sendBtn", "click", (event) => {
        const input = $("composerInput");
        if (!sessionIsInterruptible(state.activeSession) || composerHasSendableContent(input)) return;
        event.preventDefault();
        interruptActiveSession().catch((error) => toast(error.message, "error"));
      });
      bindIfPresent("composerAttachBtn", "click", () => {
        const input = $("composerFileInput");
        if (input && !input.disabled) input.click();
      });
      bindIfPresent("composerFileInput", "change", (event) => {
        addComposerFiles(event.target.files).catch((error) => toast(error.message, "error"));
        event.target.value = "";
      });
      bindIfPresent("composerInput", "keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          sendMessage().catch((error) => toast(error.message, "error"));
        }
      });
      bindIfPresent("composerInput", "input", (event) => {
        resizeComposerInput(event.target);
        persistComposerDraft(state.activeSessionId);
        renderComposer();
      });
      bindIfPresent("scrollTimelineBottomBtn", "click", () => {
        scrollTimelineToBottomNow();
        requestTimelineFollowBottom(700);
      });
      bindIfPresent("timeline", "scroll", (event) => {
        const timelineNode = event.currentTarget;
        updateTimelineStickToBottom(timelineNode);
        if (timelineNode && Number(timelineNode.scrollTop || 0) <= 80) {
          loadOlderSessionEvents().catch((error) => console.warn("Failed to load older session events", error));
        }
      });
      const timeline = $("timeline");
      if (timeline) {
        timeline.addEventListener("wheel", cancelTimelineFollowBottom, { passive: true });
        timeline.addEventListener("touchstart", cancelTimelineFollowBottom, { passive: true });
        timeline.addEventListener("pointerdown", cancelTimelineFollowBottom, { passive: true });
      }
      document.addEventListener("click", (event) => {
        const interactionButton = event.target.closest("[data-session-interaction-action]");
        if (interactionButton) {
          event.preventDefault();
          submitSessionInteraction(interactionButton.getAttribute("data-session-interaction-action") || "")
            .catch((error) => toast(error.message, "error"));
          return;
        }
        const localPathLink = event.target.closest("[data-dialogue-local-path]");
        if (localPathLink) {
          event.preventDefault();
          openDialogueLocalPath(localPathLink.getAttribute("data-dialogue-local-path") || "", { sourceElement: localPathLink })
            .catch((error) => toast(error.message, "error"));
          return;
        }
        const removeFileButton = event.target.closest("[data-composer-file-remove]");
        if (removeFileButton) {
          event.preventDefault();
          removeComposerAttachment(removeFileButton.getAttribute("data-composer-file-remove") || "");
          return;
        }
        const sendNowButton = event.target.closest("[data-pending-send-now]");
        if (sendNowButton) {
          event.preventDefault();
          sendPendingInputNow(sendNowButton.getAttribute("data-pending-send-now") || "")
            .catch((error) => toast(error.message, "error"));
          return;
        }
        const deletePendingButton = event.target.closest("[data-pending-delete]");
        if (deletePendingButton) {
          event.preventDefault();
          deletePendingInput(deletePendingButton.getAttribute("data-pending-delete") || "")
            .catch((error) => toast(error.message, "error"));
          return;
        }
        const imageOpen = event.target.closest("[data-session-image-open]");
        if (imageOpen) {
          event.preventDefault();
          const imageId = imageOpen.getAttribute("data-session-image-open") || "";
          const image = (state.sessionImages || []).find((item) => String(item.id || "") === imageId);
          if (image) openSessionImagePreview(image);
          return;
        }
        const thinking = event.target.closest(".ev-thinking");
        if (thinking) {
          const nextExpanded = !thinking.classList.contains("expanded");
          thinking.classList.toggle("expanded", nextExpanded);
          const cardKey = thinking.getAttribute("data-timeline-card-key") || "";
          if (cardKey) state.timelineCardExpansion.set(cardKey, nextExpanded);
          return;
        }
        const processToggle = event.target.closest(".ev-process-toggle");
        if (processToggle) {
          const processCard = processToggle.closest(".ev-process");
          if (processCard) {
            const nextExpanded = !processCard.classList.contains("expanded");
            processCard.classList.toggle("expanded", nextExpanded);
            const cardKey = processCard.getAttribute("data-timeline-card-key") || "";
            if (cardKey) {
              state.timelineCardExpansion.set(cardKey, nextExpanded);
              if (state.timelineAutoCollapsedProcessCards) {
                state.timelineAutoCollapsedProcessCards.delete(cardKey);
              }
              if (!processCard.classList.contains("live") && state.completedAssistantProcessCards) {
                state.completedAssistantProcessCards.add(cardKey);
              }
            }
          }
          return;
        }
        const collapsedResult = event.target.closest(".ev-result.collapsed");
        if (collapsedResult) {
          collapsedResult.classList.remove("collapsed");
          const bodyKey = collapsedResult.getAttribute("data-timeline-body-key") || "";
          if (bodyKey) state.expandedTimelineBodies.add(bodyKey);
          return;
        }
        const closeNode = event.target.closest("[data-close-session]");
        if (closeNode) {
          if (closeNode.hasAttribute("disabled") || closeNode.getAttribute("aria-disabled") === "true") {
            toast("当前会话正在执行，暂时不能关闭。", "error");
            return;
          }
          requestCloseSession(closeNode.getAttribute("data-close-session")).catch((error) => toast(error.message, "error"));
          return;
        }
        const deleteNode = event.target.closest("[data-delete-session]");
        if (deleteNode) {
          event.preventDefault();
          requestDeleteHistorySession(deleteNode.getAttribute("data-delete-session")).catch((error) => toast(error.message, "error"));
          return;
        }
        const sessionNode = event.target.closest("[data-session-id]");
        if (sessionNode && !event.target.closest("[data-delete-session]") && !event.target.closest("[data-close-session]")) {
          switchSession(sessionNode.getAttribute("data-session-id")).catch((error) => toast(error.message, "error"));
          if (IS_MOBILE_MODE) closeSheet();
          return;
        }
        const projectNode = event.target.closest("[data-project-name]");
        if (projectNode) {
          state.currentProjectName = projectNode.getAttribute("data-project-name");
          resetFileTree();
          loadFileBrowser("").then(renderAll).catch((error) => toast(error.message, "error"));
          return;
        }
        const dirNode = event.target.closest("[data-tree-dir]");
        if (dirNode) {
          const dirPath = normalizePath(dirNode.getAttribute("data-tree-dir") || "");
          if (state.expandedDirs.has(dirPath)) {
            Array.from(state.expandedDirs).forEach((item) => {
              if (item === dirPath || item.startsWith(dirPath + "/")) state.expandedDirs.delete(item);
            });
            renderFilesPane();
            if (IS_MOBILE_MODE) renderSheet();
            return;
          }
          state.expandedDirs.add(dirPath);
          loadFileBrowser(dirPath).then(() => {
            renderFilesPane();
            if (IS_MOBILE_MODE) renderSheet();
          }).catch((error) => toast(error.message, "error"));
          return;
        }
        const fileNode = event.target.closest("[data-file-entry]");
        if (fileNode) {
          const entry = JSON.parse(fileNode.getAttribute("data-file-entry"));
          openFile(entry.path).catch((error) => toast(error.message, "error"));
          return;
        }
        if (handleLivePanelClick(event)) {
          return;
        }
        const sheetLink = event.target.closest("[data-open-sheet]");
        if (sheetLink) {
          openSheet(sheetLink.getAttribute("data-open-sheet"));
          return;
        }
        if (event.target.closest("[data-open-create]")) {
          closeSheet();
          openModal().catch((error) => toast(error.message, "error"));
          return;
        }
        if (event.target.closest("[data-open-raw]")) {
          openRawDrawer();
        }
        const insertCommand = event.target.closest("[data-insert-command]");
        if (insertCommand) {
          const input = $("composerInput");
          if (!input) return;
          const command = insertCommand.getAttribute("data-insert-command") || "";
          input.value = command + (command ? " " : "");
          resizeComposerInput(input);
          persistComposerDraft(state.activeSessionId);
          input.focus();
          return;
        }
        const quick = event.target.closest("[data-mobile-quick]");
        if (quick) {
          const quickAction = quick.getAttribute("data-mobile-quick");
          if (quickAction === "confirm_yes") {
            runConfirmAction("y").catch((error) => toast(error.message, "error"));
            return;
          }
          if (quickAction === "confirm_no") {
            runConfirmAction("n").catch((error) => toast(error.message, "error"));
            return;
          }
          const input = $("composerInput");
          if (!input) return;
          const mapping = {
            esc: "ESC",
            ctrl_c: "^C",
            tab: "Tab"
          };
          input.value = mapping[quickAction] || "";
          resizeComposerInput(input);
          persistComposerDraft(state.activeSessionId);
          input.focus();
          return;
        }
      });
      document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && state.imageViewer) closeImageViewer();
      });
      document.addEventListener("input", (event) => {
        if (handleLivePanelInput(event)) return;
      });
      document.querySelectorAll("[data-left-tab]").forEach((button) => {
        button.addEventListener("click", () => {
          state.leftTab = button.getAttribute("data-left-tab");
          document.querySelectorAll("[data-left-tab]").forEach((item) => item.classList.toggle("active", item === button));
          const sessionsPanel = $("panelSessions");
          const filesPanel = $("panelFiles");
          if (sessionsPanel) sessionsPanel.hidden = state.leftTab !== "sessions";
          if (filesPanel) filesPanel.hidden = state.leftTab !== "files";
          window.requestAnimationFrame(syncSidebarLayout);
        });
      });
      document.querySelectorAll("[data-mobile-tab]").forEach((button) => {
        button.addEventListener("click", () => {
          state.mobileTab = button.getAttribute("data-mobile-tab");
          renderTabs();
        });
      });
      document.querySelectorAll("[data-mobile-view]").forEach((button) => {
        button.addEventListener("click", () => {
          state.mobileDialogueView = button.getAttribute("data-mobile-view") || "stream";
          if (state.mobileDialogueView === "raw") {
            refreshRawLog(true).catch(() => {});
          }
          renderMobileDialogueView();
        });
      });
      window.addEventListener("resize", () => window.requestAnimationFrame(() => {
        applyPreviewPanelWidth(readPreviewPanelWidth());
        syncSidebarLayout();
      }));
    }

    async function init() {
      bindEvents();
      syncDialogueDropdowns();
      await Promise.all([loadProjects(), loadConnections(), loadSessions()]);
      renderAll();
      if (state.activeSessionId) {
        await switchSession(state.activeSessionId);
      }
      window.addEventListener("focus", () => {
        syncEmbeddedConsoleFrame();
        scheduleTaskRefresh(200);
      });
      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") {
          syncEmbeddedConsoleFrame();
        }
        scheduleTaskRefresh(document.visibilityState === "visible" ? 200 : nextTaskRefreshDelay());
      });
      scheduleTaskRefresh(200);
    }

    init().catch((error) => {
      toast(error.message || "页面初始化失败。", "error");
    });
