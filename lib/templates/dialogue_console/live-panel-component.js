function createDialogueLivePanelComponent(deps) {
  const LIVE_TASK_SELECTION_KEY = "vizo.dialogue.liveTaskSelection";
  const DISMISSED_TASK_BANNERS_KEY = "vizo.dialogue.dismissedTaskBanners";
  const TASK_BANNER_ATTENTION_WINDOW_HOURS = 24;
  const LIVE_PANEL_COLLAPSE_MS = 220;
  const {
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
  } = deps;

  function taskActionSubtitle(task) {
    return taskDisplayTitle(task);
  }

  function readStoredLiveTaskSelection() {
    try {
      const raw = localStorage.getItem(LIVE_TASK_SELECTION_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      const taskId = String((parsed && parsed.taskId) || "").trim();
      if (!taskId) return null;
      return {
        taskId,
        project: String((parsed && parsed.project) || "").trim()
      };
    } catch (error) {
      return null;
    }
  }

  function storeLiveTaskSelection(taskId, projectName) {
    const normalizedTaskId = String(taskId || "").trim();
    if (!normalizedTaskId) return;
    try {
      localStorage.setItem(
        LIVE_TASK_SELECTION_KEY,
        JSON.stringify({
          taskId: normalizedTaskId,
          project: String(projectName || "").trim()
        })
      );
    } catch (error) {}
  }

  function clearStoredLiveTaskSelection(taskId) {
    try {
      if (!taskId) {
        localStorage.removeItem(LIVE_TASK_SELECTION_KEY);
        return;
      }
      const stored = readStoredLiveTaskSelection();
      if (stored && stored.taskId === taskId) localStorage.removeItem(LIVE_TASK_SELECTION_KEY);
    } catch (error) {}
  }

  function taskBannerDismissKey(task) {
    if (!task) return "";
    const taskId = String(task.task_id || task.id || "").trim();
    if (!taskId) return "";
    return [
      taskId,
      String(task.status || "").toLowerCase(),
      String(task.updated_at || task.completed_at || task.finished_at || "")
    ].join("|");
  }

  function readDismissedTaskBanners() {
    try {
      const raw = localStorage.getItem(DISMISSED_TASK_BANNERS_KEY);
      const parsed = raw ? JSON.parse(raw) : [];
      return Array.isArray(parsed) ? parsed.filter(Boolean).slice(-50) : [];
    } catch (error) {
      return [];
    }
  }

  function isTaskBannerDismissed(task) {
    const key = taskBannerDismissKey(task);
    if (!key) return false;
    return readDismissedTaskBanners().includes(key);
  }

  function dismissTaskBanner(task) {
    const key = taskBannerDismissKey(task);
    if (!key) return;
    const dismissed = readDismissedTaskBanners().filter((item) => item !== key);
    dismissed.push(key);
    try {
      localStorage.setItem(DISMISSED_TASK_BANNERS_KEY, JSON.stringify(dismissed.slice(-50)));
    } catch (error) {}
    if (state.taskDetail && String(state.taskDetail.task_id || state.taskDetail.id || "") === String(task.task_id || task.id || "")) {
      state.taskDetail = null;
      state.selectedLiveNode = null;
      state.manualLiveSelection = false;
    }
  }

  function compactTaskText(value, maxLength, fallback) {
    let text = String(value || fallback || "").replace(/\s+/g, " ").trim();
    if (!text) text = String(fallback || "").trim();
    const limit = Number(maxLength || 0);
    if (limit > 0 && text.length > limit) return text.slice(0, limit);
    return text;
  }

  function taskDisplayTitle(task) {
    if (!task) return "未命名任务";
    return compactTaskText(
      task.task_title || task.task_name || task.summary_title || task.title || task.description || task.task_id || task.id,
      20,
      "未命名任务"
    );
  }

  function taskDisplaySummary(task) {
    if (!task) return "查看该任务的步骤、日志与确认状态。";
    return compactTaskText(
      task.task_summary || task.summary || "",
      60,
      "查看该任务的步骤、日志与确认状态。"
    );
  }

  function resolveTaskActionLabel(task, action, fallback) {
    const button = (resolveTaskActionButtons(task) || []).find((item) => item.action === action);
    return String((button && button.label) || fallback || action || "");
  }

  function resolveConfirmActionLabel(task, action, fallback) {
    const button = (resolveConfirmButtons(task) || []).find((item) => item.action === action);
    return String((button && button.label) || fallback || action || "");
  }

  async function confirmTaskAction(task, payload) {
    const action = String((payload && payload.action) || "");
    if (!task || !action || payload && payload.skipConfirm === true) return { confirmed: true };
    if (action === "pause") {
      return await requestTaskActionConfirmation({
        title: "暂停任务",
        subtitle: taskActionSubtitle(task),
        message: "确认暂停此任务？",
        actions: [
          { label: "取消", cls: "ghost-btn", result: null },
          { label: "确认暂停", cls: "btn warn", result: { confirmed: true } }
        ]
      });
    }
    if (action === "terminate") {
      if (task.has_code_changes) {
        return await requestTaskActionConfirmation({
          title: "终止任务",
          subtitle: taskActionSubtitle(task),
          message: "此任务检测到代码变更，请选择终止后的代码处理方式。",
          choices: [
            { label: "回滚代码（推荐）", cls: "btn", result: { confirmed: true, rollback: true } },
            { label: "保留代码", cls: "ghost-btn", result: { confirmed: true, rollback: false } }
          ],
          actions: [
            { label: "取消", cls: "ghost-btn", result: null }
          ]
        });
      }
      return await requestTaskActionConfirmation({
        title: "终止任务",
        subtitle: taskActionSubtitle(task),
        message: "确认终止此任务？",
        actions: [
          { label: "取消", cls: "ghost-btn", result: null },
          { label: "确认终止", cls: "btn danger", result: { confirmed: true, rollback: false } }
        ]
      });
    }
    return { confirmed: true };
  }

  async function confirmPendingRequestAction(task, payload) {
    const action = String((payload && payload.action) || "");
    if (!task || !action || payload && payload.skipConfirm === true) return { confirmed: true };
    const label = resolveConfirmActionLabel(task, action, payload && payload.label);
    const pending = task.pending_confirm || {};
    if (action === "y") {
      return await requestTaskActionConfirmation({
        title: label || "确认继续",
        subtitle: taskActionSubtitle(task),
        message: pending.summary || pending.title || "确认后将继续当前阶段。",
        actions: [
          { label: "取消", cls: "ghost-btn", result: null },
          { label: label || "确认", cls: "btn", result: { confirmed: true } }
        ]
      });
    }
    if (action === "n") {
      return await requestTaskActionConfirmation({
        title: "取消当前阶段",
        subtitle: taskActionSubtitle(task),
        message: "确认取消当前阶段？取消后当前任务流程通常会停止在这里。",
        actions: [
          { label: "取消", cls: "ghost-btn", result: null },
          { label: "确认取消", cls: "btn danger", result: { confirmed: true } }
        ]
      });
    }
    if (action === "terminate") {
      return await confirmTaskAction(task, payload);
    }
    return { confirmed: true };
  }

  function resetState() {
    state.taskRefreshGeneration = Number(state.taskRefreshGeneration || 0) + 1;
    if (state.taskRefreshHandle) {
      clearTimeout(state.taskRefreshHandle);
      state.taskRefreshHandle = null;
    }
    state.currentTask = null;
    state.taskDetail = null;
    state.selectedLiveNode = null;
    state.manualLiveSelection = false;
    state.expandedLiveSteps.clear();
    state.expandedLiveSubtasks.clear();
    if (state.livePanelCollapsingCards && typeof state.livePanelCollapsingCards.clear === "function") {
      state.livePanelCollapsingCards.clear();
    }
    if (state.livePanelOpeningCards && typeof state.livePanelOpeningCards.clear === "function") {
      state.livePanelOpeningCards.clear();
    }
    state.liveLogs.clear();
    state.subTasks.clear();
    state.confirmDraftsByRequestId.clear();
    state.confirmEditorsByRequestId.clear();
    state.liveTaskSwitcherOpen = false;
  }

  function isTaskRefreshStale(generation) {
    return Number(state.taskRefreshGeneration || 0) !== Number(generation || 0);
  }

  async function refreshTaskState() {
    const refreshGeneration = Number(state.taskRefreshGeneration || 0);
    if (state.taskRefreshPromise && state.taskRefreshPromiseGeneration === refreshGeneration) {
      return state.taskRefreshPromise;
    }
    const refreshPromise = (async () => {
      const project = currentProject();
      const projectQuery = project ? ("?project=" + encodeURIComponent(project.name)) : "";
      let currentPayload = await api(ROUTE_BASE + "/api/opus/current" + projectQuery).catch(() => ({ has_task: false }));
      if (isTaskRefreshStale(refreshGeneration)) return;
      let currentTask = currentPayload.has_task ? currentPayload.task : null;
      const listUrl = project
        ? (ROUTE_BASE + "/api/projects/" + encodeURIComponent(project.name) + "/tasks")
        : (ROUTE_BASE + "/api/tasks");
      let listPayload = await api(listUrl).catch(() => ({ tasks: [] }));
      if (isTaskRefreshStale(refreshGeneration)) return;
      let tasks = listPayload.tasks || [];
      let fallbackTask = (tasks || []).find((task) => {
        const status = String((task && task.status) || "").toLowerCase();
        if (isTaskBannerDismissed(task) && !taskBannerStatusIsActive(status) && !task.pending_confirm) return false;
        return taskNeedsBannerAttention(task);
      }) || null;
      state.currentTask = currentTask;
      state.tasks = tasks;
      let storedSelection = readStoredLiveTaskSelection();
      const currentProjectName = project ? String(project.name || "").trim() : "";
      if (
        storedSelection &&
        currentProjectName &&
        storedSelection.project &&
        String(storedSelection.project || "").trim() !== currentProjectName
      ) {
        clearStoredLiveTaskSelection(storedSelection.taskId);
        storedSelection = null;
      }
      const storedTask = storedSelection
        ? (
            [currentTask].concat(tasks || []).find((task) => {
              const taskId = String((task && (task.task_id || task.id)) || "").trim();
              return taskId === storedSelection.taskId;
            }) || {
              task_id: storedSelection.taskId,
              id: storedSelection.taskId,
              project: storedSelection.project
            }
          )
        : null;
      const targetTask = state.taskDetail && state.taskDetail.task_id
        ? state.taskDetail
        : (storedTask || currentTask || fallbackTask);
      const targetTaskId = targetTask
        ? (targetTask.task_id || targetTask.id || "")
        : "";
      const targetProjectName = targetTask
        ? String(targetTask.project || targetTask.project_name || (project ? project.name : ""))
        : "";
      if (targetTaskId) {
        const loaded = await loadTaskDetail(targetTaskId, targetProjectName, { preserveSwitcher: true, refreshGeneration });
        if (isTaskRefreshStale(refreshGeneration)) return;
        if (!loaded && storedSelection && targetTaskId === storedSelection.taskId) {
          clearStoredLiveTaskSelection(storedSelection.taskId);
          const fallbackTarget = currentTask || fallbackTask;
          const fallbackTaskId = fallbackTarget ? (fallbackTarget.task_id || fallbackTarget.id || "") : "";
          const fallbackProjectName = fallbackTarget
            ? String(fallbackTarget.project || fallbackTarget.project_name || (project ? project.name : ""))
            : "";
          if (fallbackTaskId && fallbackTaskId !== targetTaskId) {
            await loadTaskDetail(fallbackTaskId, fallbackProjectName, { preserveSwitcher: true, refreshGeneration });
            if (isTaskRefreshStale(refreshGeneration)) return;
          }
        }
      } else {
        state.taskDetail = null;
        state.selectedLiveNode = null;
        state.manualLiveSelection = false;
        state.expandedLiveSteps.clear();
        state.expandedLiveSubtasks.clear();
        if (state.livePanelCollapsingCards && typeof state.livePanelCollapsingCards.clear === "function") {
          state.livePanelCollapsingCards.clear();
        }
        if (state.livePanelOpeningCards && typeof state.livePanelOpeningCards.clear === "function") {
          state.livePanelOpeningCards.clear();
        }
      }
      if (isTaskRefreshStale(refreshGeneration)) return;
      if (typeof renderTaskSurfaces === "function") renderTaskSurfaces();
      else renderAll();
    })();
    state.taskRefreshPromise = refreshPromise;
    state.taskRefreshPromiseGeneration = refreshGeneration;
    try {
      await refreshPromise;
    } finally {
      if (state.taskRefreshPromise === refreshPromise) {
        state.taskRefreshPromise = null;
        state.taskRefreshPromiseGeneration = null;
      }
    }
  }

  function scheduleTaskRefresh(delay) {
    if (state.taskRefreshHandle) clearTimeout(state.taskRefreshHandle);
    state.taskRefreshHandle = window.setTimeout(() => {
      state.taskRefreshHandle = null;
      refreshTaskState().catch(() => {}).finally(() => {
        scheduleTaskRefresh(nextTaskRefreshDelay());
      });
    }, typeof delay === "number" ? delay : nextTaskRefreshDelay());
  }

  async function loadTaskDetail(taskId, projectName, options) {
    if (!taskId) return false;
    const refreshGeneration = options && options.refreshGeneration !== undefined
      ? Number(options.refreshGeneration)
      : null;
    const url = ROUTE_BASE + "/api/opus/current?task_id=" + encodeURIComponent(taskId) +
      (projectName ? "&project=" + encodeURIComponent(projectName) : "");
    const payload = await api(url).catch(() => ({ has_task: false }));
    if (refreshGeneration !== null && isTaskRefreshStale(refreshGeneration)) return false;
    if (!payload.has_task) {
      state.taskDetail = null;
      state.selectedLiveNode = null;
      state.manualLiveSelection = false;
      state.expandedLiveSteps.clear();
      state.expandedLiveSubtasks.clear();
      if (state.livePanelCollapsingCards && typeof state.livePanelCollapsingCards.clear === "function") {
        state.livePanelCollapsingCards.clear();
      }
      if (state.livePanelOpeningCards && typeof state.livePanelOpeningCards.clear === "function") {
        state.livePanelOpeningCards.clear();
      }
      if (!(options && options.preserveSwitcher === true)) state.liveTaskSwitcherOpen = false;
      return false;
    }
    const previousTaskId = state.taskDetail && state.taskDetail.task_id;
    state.taskDetail = payload.task;
    if (options && options.persistSelection === true) {
      storeLiveTaskSelection(
        state.taskDetail.task_id || taskId,
        state.taskDetail.project || state.taskDetail.project_name || projectName || ""
      );
    }
    if (!(options && options.preserveSwitcher === true)) state.liveTaskSwitcherOpen = false;
    if (previousTaskId !== state.taskDetail.task_id) {
      state.selectedLiveNode = null;
      state.manualLiveSelection = false;
      state.expandedLiveSteps.clear();
      state.expandedLiveSubtasks.clear();
      if (state.livePanelCollapsingCards && typeof state.livePanelCollapsingCards.clear === "function") {
        state.livePanelCollapsingCards.clear();
      }
      if (state.livePanelOpeningCards && typeof state.livePanelOpeningCards.clear === "function") {
        state.livePanelOpeningCards.clear();
      }
    }
    await syncRelevantSubTasks(state.taskDetail);
    if (refreshGeneration !== null && isTaskRefreshStale(refreshGeneration)) return false;
    ensureLiveSelection();
    await loadSelectedLiveNode();
    if (refreshGeneration !== null && isTaskRefreshStale(refreshGeneration)) return false;
    return true;
  }

  async function loadSubTaskDetail(task, subName, options) {
    if (!task || !subName) return null;
    const cacheKey = liveSubTaskCacheKey(task, subName);
    if (!options || options.force !== true) {
      const cached = state.subTasks.get(cacheKey);
      if (cached) return cached;
    }
    const projectName = taskProjectName(task);
    const payload = await api(
      ROUTE_BASE + "/api/opus/subtask?task_id=" + encodeURIComponent(task.task_id) +
      "&sub_name=" + encodeURIComponent(subName) +
      (projectName ? "&project=" + encodeURIComponent(projectName) : "")
    ).catch(() => ({ found: false }));
    if (!payload.found || !payload.sub_task) {
      state.subTasks.delete(cacheKey);
      return null;
    }
    state.subTasks.set(cacheKey, payload.sub_task);
    return payload.sub_task;
  }

  async function syncRelevantSubTasks(task) {
    if (!task || !(task.sub_tasks || []).length) return;
    const targetSubTasks = new Set();
    const selectedNode = state.selectedLiveNode;
    (task.sub_tasks || []).forEach((subTask) => {
      const subName = subTask.name || String(subTask.id || "");
      if (!subName) return;
      const status = String(subTask.status || "").toLowerCase();
      const cacheKey = liveSubTaskCacheKey(task, subName);
      if (
        status === "running" ||
        status === "waiting_confirm" ||
        state.expandedLiveSubtasks.has(cacheKey) ||
        (selectedNode && selectedNode.type === "substep" && selectedNode.subtaskName === subName)
      ) {
        targetSubTasks.add(subName);
      }
    });
    await Promise.all(Array.from(targetSubTasks).map((subName) => loadSubTaskDetail(task, subName, { force: true })));
  }

  function ensureLiveSelection() {
    const task = state.taskDetail;
    if (!task) {
      state.selectedLiveNode = null;
      state.manualLiveSelection = false;
      return;
    }
    if (!state.selectedLiveNode || state.selectedLiveNode.taskId !== task.task_id) {
      state.manualLiveSelection = false;
    }
    const nextNode = findCurrentSubstepNode(task) || findCurrentStepNode(task) || firstAvailableLiveNode(task);
    if (shouldAutoFollowLiveNode(task, nextNode)) {
      state.selectedLiveNode = nextNode;
      return;
    }
    if (!isLiveNodeAvailable(task, state.selectedLiveNode)) {
      state.selectedLiveNode = nextNode;
      state.manualLiveSelection = false;
      return;
    }
    if (!state.selectedLiveNode) {
      state.selectedLiveNode = nextNode;
    }
  }

  async function loadLiveNodeLogs(task, node) {
    if (!task || !node) return;
    const projectName = taskProjectName(task);
    if (node.type === "step") {
      const cacheKey = liveStepLogCacheKey(task, node.id, node.role);
      const payload = await api(
        ROUTE_BASE + "/api/opus/logs?task_id=" + encodeURIComponent(task.task_id) +
        "&step_id=" + encodeURIComponent(node.id) +
        "&role=" + encodeURIComponent(node.role || node.id) +
        "&run_index=-1" +
        (projectName ? "&project=" + encodeURIComponent(projectName) : "")
      ).catch(() => ({ actions: [] }));
      state.liveLogs.set(cacheKey, payload.actions || []);
      return;
    }
    if (node.type === "substep") {
      const cacheKey = liveSubstepLogCacheKey(task, node.subId, node.id, node.role);
      const payload = await api(
        ROUTE_BASE + "/api/opus/logs?task_id=" + encodeURIComponent(task.task_id) +
        "&step_id=" + encodeURIComponent(node.id) +
        "&role=" + encodeURIComponent(node.role || node.id) +
        "&sub_id=" + encodeURIComponent(node.subId || "") +
        "&run_index=-1" +
        (projectName ? "&project=" + encodeURIComponent(projectName) : "")
      ).catch(() => ({ actions: [] }));
      state.liveLogs.set(cacheKey, payload.actions || []);
    }
  }

  async function loadSelectedLiveNode() {
    const task = resolveDisplayTask();
    const selected = state.selectedLiveNode;
    if (!task || !selected) return;
    await loadLiveNodeLogs(task, selected);
  }

  function logsForNode(task, node) {
    if (!task || !node) return [];
    if (node.type === "step") {
      return state.liveLogs.get(liveStepLogCacheKey(task, node.id, node.role)) || [];
    }
    if (node.type === "substep") {
      return state.liveLogs.get(liveSubstepLogCacheKey(task, node.subId, node.id, node.role)) || [];
    }
    return [];
  }

  function currentLiveLogs() {
    return logsForNode(resolveDisplayTask(), state.selectedLiveNode);
  }

  async function runTaskAction(target) {
    const payload = typeof target === "string" ? { action: target, placement: "banner" } : (target || {});
    const action = String(payload.action || "");
    const task = resolveKnownTask(payload.taskId || "", payload.placement || "");
    const taskId = String(payload.taskId || (task && task.task_id) || "");
    if (!action || !taskId) return;
    const decision = await confirmTaskAction(task, payload);
    if (!decision || decision.confirmed !== true) return;
    const body = {};
    if (action === "terminate") body.rollback = decision.rollback === true;
    const response = await api("/vizo/api/tasks/" + encodeURIComponent(taskId) + "/" + encodeURIComponent(action), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    toast(response.message || "任务操作已提交。", "success");
    scheduleTaskRefresh(300);
  }

  function clearPendingConfirmFromTask(task, requestId) {
    if (!task || !task.pending_confirm) return;
    const currentRequestId = String(task.pending_confirm.request_id || "");
    const expectedRequestId = String(requestId || "");
    if (expectedRequestId && currentRequestId && currentRequestId !== expectedRequestId) return;
    delete task.pending_confirm;
    if (String(task.status || "").toLowerCase() === "waiting_confirm") {
      task.status = "running";
    }
  }

  function markConfirmResolvedLocally(taskId, requestId) {
    const expectedTaskId = String(taskId || "");
    [state.currentTask, state.taskDetail].forEach((task) => {
      const currentTaskId = String((task && (task.task_id || task.id)) || "");
      if (!expectedTaskId || currentTaskId === expectedTaskId) {
        clearPendingConfirmFromTask(task, requestId);
      }
    });
    (state.tasks || []).forEach((task) => {
      const currentTaskId = String((task && (task.task_id || task.id)) || "");
      if (!expectedTaskId || currentTaskId === expectedTaskId) {
        clearPendingConfirmFromTask(task, requestId);
      }
    });
  }

  async function runConfirmAction(target) {
    const payload = typeof target === "string" ? { action: target, placement: "banner" } : (target || {});
    const action = String(payload.action || "");
    const task = resolveKnownTask(payload.taskId || "", payload.placement || "");
    const taskId = String(payload.taskId || (task && task.task_id) || "");
    const requestId = String(payload.requestId || ((task && task.pending_confirm && task.pending_confirm.request_id) || ""));
    const draftKey = String(payload.draftKey || requestId || taskId);
    const editorKey = String(payload.editorKey || [payload.placement || "panel", draftKey].join("::"));
    const submitFeedback = payload.submitFeedback === true;
    if (!action || !taskId) return;
    if (action === "f" && !submitFeedback) {
      openConfirmEditor(editorKey);
      rerenderConfirmPlacement(payload.placement);
      return;
    }
    const decision = await confirmPendingRequestAction(task, payload);
    if (!decision || decision.confirmed !== true) return;
    let body = { action };
    if (action === "f") {
      const feedback = getConfirmDraft(draftKey).trim();
      if (!feedback) throw new Error("请先输入补充意见。");
      body.feedback = feedback;
    }
    if (action === "terminate") body.rollback = decision.rollback === true;
    const response = await api("/vizo/api/tasks/" + encodeURIComponent(taskId) + "/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    markConfirmResolvedLocally(taskId, requestId);
    clearConfirmDraft(draftKey);
    closeConfirmEditor(editorKey, { clearDraft: false });
    if (typeof renderTaskSurfaces === "function") renderTaskSurfaces();
    else rerenderConfirmPlacement(payload.placement);
    toast(response.message || "确认操作已提交。", "success");
    scheduleTaskRefresh(300);
  }

  async function focusLiveTask() {
    if (!state.currentTask || !state.currentTask.task_id) return;
    const projectName = String(
      state.currentTask.project ||
      state.currentTask.project_name ||
      (currentProject() ? currentProject().name : "")
    );
    state.embeddedView = "";
    state.embeddedSection = "";
    setLivePanelVisibility(true);
    state.manualLiveSelection = false;
    await loadTaskDetail(state.currentTask.task_id, projectName, { persistSelection: true });
    renderAll();
    if (IS_MOBILE_MODE) openSheet("task");
  }

  function resolveBannerNotification(task, liveSwitchState) {
    if (!task) return null;
    const pending = task.pending_confirm;
    if (pending) {
      return {
        tone: "warning",
        text: pending.summary || pending.title || "当前任务有新的确认请求，等待你处理。"
      };
    }
    const status = String(task.status || "").toLowerCase();
    if (["failed", "error", "runtime_error", "interrupted"].includes(status)) {
      return {
        tone: "danger",
        text: "当前任务出现异常，请进入直播面板查看原因并处理。"
      };
    }
    const failedStep = (task.steps || []).find((step) => {
      const stepStatus = String(step && step.status || "").toLowerCase();
      return stepStatus === "failed" || stepStatus === "error";
    });
    if (failedStep) {
      return {
        tone: "danger",
        text: "步骤 " + (failedStep.name || failedStep.role || "任务步骤") + " 执行异常，请进入直播面板查看。"
      };
    }
    if (liveSwitchState) {
      return {
        tone: "info",
        text: "当前有其他直播任务在推进，你现在看到的是通知入口。"
      };
    }
    return null;
  }

  function taskBannerStatusIsActive(status) {
    return ["running", "waiting_confirm", "paused"].includes(String(status || "").toLowerCase());
  }

  function taskBannerStatusIsAbnormal(status) {
    return ["failed", "error", "runtime_error", "interrupted"].includes(String(status || "").toLowerCase());
  }

  function taskBannerAttentionTimestamp(task) {
    if (!task) return "";
    return String(task.updated_at || task.completed_at || task.finished_at || task.created_at || task.started_at || "");
  }

  function taskBannerIsRecentAttention(task) {
    const raw = taskBannerAttentionTimestamp(task).trim();
    if (!raw) return false;
    const parsed = new Date(raw);
    const millis = parsed.getTime();
    if (!Number.isFinite(millis)) return false;
    const age = Date.now() - millis;
    return age >= 0 && age <= TASK_BANNER_ATTENTION_WINDOW_HOURS * 60 * 60 * 1000;
  }

  function taskHasAbnormalStep(task) {
    return !!(task && (task.steps || []).some((step) => taskBannerStatusIsAbnormal(step && step.status)));
  }

  function taskNeedsBannerAttention(task) {
    if (!task) return false;
    if (task.pending_confirm) return true;
    const status = String(task.status || "").toLowerCase();
    if (taskBannerStatusIsActive(status)) return true;
    if (!taskBannerIsRecentAttention(task)) return false;
    return taskBannerStatusIsAbnormal(status) || taskHasAbnormalStep(task);
  }

  function shouldShowTaskBanner(task) {
    if (!task) return false;
    if (task.pending_confirm) return true;
    const status = String(task.status || "").toLowerCase();
    if (isTaskBannerDismissed(task) && !taskBannerStatusIsActive(status)) return false;
    return taskNeedsBannerAttention(task);
  }

  function canDismissTaskBanner(task, bannerNotice) {
    if (!task || task.pending_confirm) return false;
    const status = String(task.status || "").toLowerCase();
    return !!bannerNotice && (taskBannerStatusIsAbnormal(status) || taskHasAbnormalStep(task));
  }

  function setTaskBannerVisible(banner, visible) {
    const displayVisible = visible &&
      !(state.embeddedView && !IS_MOBILE_MODE) &&
      (!IS_MOBILE_MODE || state.mobileTab === "dialogue");
    banner.dataset.taskBannerVisible = visible ? "true" : "false";
    banner.hidden = !visible;
    banner.style.display = displayVisible ? "flex" : "none";
  }

  function renderTaskBanner() {
    const banner = $("taskBanner");
    if (!banner) return;
    const task = state.currentTask;
    const liveSwitchState = resolveLiveSwitchState();
    if (!shouldShowTaskBanner(task)) {
      banner.innerHTML = "";
      setTaskBannerVisible(banner, false);
      return;
    }
    setTaskBannerVisible(banner, true);
    const bannerNotice = resolveBannerNotification(task, liveSwitchState);
    const bannerNoticeHtml = bannerNotice
      ? '<div class="task-banner-notice ' + escapeHtml(bannerNotice.tone) + '">' + escapeHtml(bannerNotice.text) + "</div>"
      : '<div class="task-banner-desc">' + escapeHtml(taskDisplaySummary(task)) + "</div>";
    const viewTaskButton = bannerNotice
      ? '<button class="btn-sm primary task-banner-cta" type="button" data-task-focus-live="1">查看任务</button>'
      : "";
    const dismissButton = canDismissTaskBanner(task, bannerNotice)
      ? '<button class="btn-sm task-banner-dismiss" type="button" data-task-banner-dismiss="1" aria-label="隐藏任务提醒" title="隐藏提醒">隐藏提醒</button>'
      : "";
    const titleRowHtml = '<div class="task-banner-title-row">' +
      '<span class="task-banner-name">' + escapeHtml(taskDisplayTitle(task)) + '</span>' +
      dismissButton +
      '</div>';
    banner.innerHTML = IS_MOBILE_MODE
      ? (
          '<span class="task-badge">' + escapeHtml(task.status || "task") + '</span>' +
          '<span class="task-name">' + escapeHtml(taskDisplayTitle(task)) + '</span>' +
          dismissButton +
          '<span class="task-arrow">&#8250;</span>'
        )
      : (
          '<span class="task-banner-badge">' + escapeHtml(task.status || "task") + '</span>' +
          '<div class="task-banner-main">' +
            titleRowHtml +
            bannerNoticeHtml +
          '</div>' +
          (viewTaskButton
            ? '<div class="task-banner-actions">' + viewTaskButton + "</div>"
            : "")
        );
  }

  function buildTimelineItems() {
    const items = state.events.slice();
    const task = state.currentTask;
    if (task && task.pending_confirm) {
      items.push({
        id: "pending-confirm-" + task.task_id,
        seq: (items[items.length - 1] ? items[items.length - 1].seq : 0) + 1,
        kind: "confirm",
        title: "等待确认",
        badge: task.pending_confirm.type || "confirm",
        time: isoTime(task.updated_at || task.started_at || ""),
        body: [task.pending_confirm.summary || task.pending_confirm.title || "当前任务需要确认。"],
        task_id: task.task_id,
        confirm: task.pending_confirm
      });
    }
    const grouped = groupTimelineItems(items.sort(compareTimelineItems));
    return attachStreamingAssistantTurn(grouped);
  }

  function timelineSortTime(item) {
    const value = Date.parse(String((item && item.timestamp) || ""));
    return Number.isFinite(value) ? value : 0;
  }

  function compareTimelineItems(left, right) {
    const leftTime = timelineSortTime(left);
    const rightTime = timelineSortTime(right);
    if (leftTime !== rightTime) return leftTime - rightTime;
    const leftSeq = Number((left && left.seq) || 0);
    const rightSeq = Number((right && right.seq) || 0);
    if (leftSeq !== rightSeq) return leftSeq - rightSeq;
    return Number((left && left.order) || 0) - Number((right && right.order) || 0);
  }

  function isAssistantTurnStreaming() {
    const status = String((state.activeSession && (state.activeSession.current_turn_status || state.activeSession.status)) || "").toLowerCase();
    return status === "running";
  }

  function timelineCardStateKey(kind, item) {
    const base = item && (item.id || item.seq || item.time || "");
    return base ? (String(kind || "card") + ":" + String(base)) : "";
  }

  function timelineBodyStateKey(kind, item) {
    const base = item && (item.id || item.seq || item.time || "");
    return base ? (String(kind || "body") + ":" + String(base)) : "";
  }

  function timelineItemStateKey(item) {
    const base = item && (item.id || item.seq || item.time || "");
    return base ? ("item:" + String(item.kind || "event") + ":" + String(base)) : "";
  }

  function timelineFreshClass(key) {
    if (!key) return "";
    if (!state.timelineSeenRenderKeys) state.timelineSeenRenderKeys = new Set();
    if (!state.timelineFreshRenderKeys) state.timelineFreshRenderKeys = new Set();
    if (!state.timelineSeenRenderKeys.has(key)) {
      state.timelineSeenRenderKeys.add(key);
      if (state.timelineRenderInitialized) {
        state.timelineFreshRenderKeys.add(key);
        scheduleTimelineFreshCleanup();
      }
    }
    return state.timelineFreshRenderKeys.has(key) ? " stream-fresh" : "";
  }

  function scheduleTimelineFreshCleanup() {
    if (state.timelineFreshCleanupHandle) return;
    state.timelineFreshCleanupHandle = window.setTimeout(() => {
      state.timelineFreshCleanupHandle = null;
      if (state.timelineFreshRenderKeys && state.timelineFreshRenderKeys.clear) {
        state.timelineFreshRenderKeys.clear();
      }
      const target = $("timeline");
      if (!target || !target.querySelectorAll) return;
      target.querySelectorAll(".stream-fresh").forEach((node) => {
        node.classList.remove("stream-fresh");
      });
    }, 280);
  }

  function timelineKeyAttr(key) {
    return key ? (' data-timeline-item-key="' + escapeHtml(key) + '"') : "";
  }

  function isTimelineCardExpanded(key, fallback) {
    if (!key) return !!fallback;
    if (state.timelineCardExpansion && state.timelineCardExpansion.has(key)) {
      return !!state.timelineCardExpansion.get(key);
    }
    return !!fallback;
  }

  function ensureAssistantProcessExpansionState() {
    if (!state.completedAssistantProcessCards) state.completedAssistantProcessCards = new Set();
    if (!state.timelineAutoCollapsedProcessCards) state.timelineAutoCollapsedProcessCards = new Set();
  }

  function syncAssistantProcessCompletionState(processKey, live, steps) {
    if (!processKey || !(steps || []).length) return;
    ensureAssistantProcessExpansionState();
    if (live) {
      state.completedAssistantProcessCards.delete(processKey);
      state.timelineAutoCollapsedProcessCards.delete(processKey);
      return;
    }
    if (state.completedAssistantProcessCards.has(processKey)) return;
    state.completedAssistantProcessCards.add(processKey);
    state.timelineAutoCollapsedProcessCards.add(processKey);
    if (state.timelineCardExpansion && state.timelineCardExpansion.get(processKey) === true) {
      state.timelineCardExpansion.delete(processKey);
    }
  }

  function isAssistantProcessExpanded(processKey, live, steps) {
    syncAssistantProcessCompletionState(processKey, !!live, steps);
    if (processKey && state.timelineCardExpansion && state.timelineCardExpansion.has(processKey)) {
      return !!state.timelineCardExpansion.get(processKey);
    }
    if (
      processKey &&
      state.timelineAutoCollapsedProcessCards &&
      state.timelineAutoCollapsedProcessCards.has(processKey)
    ) {
      return false;
    }
    return !!live;
  }

  function isTimelineBodyExpanded(key) {
    return !!(key && state.expandedTimelineBodies && state.expandedTimelineBodies.has(key));
  }

  function isAssistantProcessItem(item) {
    if (!item) return false;
    if (item.kind === "work") return true;
    return item.kind === "tool";
  }

  function createAssistantTurnItem(item) {
    return {
      id: "assistant-turn-" + String((item && item.id) || ""),
      seq: Number((item && item.seq) || 0),
      kind: "assistant_turn",
      title: "助手输出",
      badge: String((item && item.badge) || ""),
      time: String((item && item.time) || ""),
      body: [],
      process_steps: [],
      segments: [],
      live: false,
      placeholder: false
    };
  }

  function appendAssistantTurnItem(turn, item, options) {
    if (!turn || !item) return;
    if (item.time) turn.time = item.time;
    if (item.badge) turn.badge = item.badge;
    if (item.kind === "assistant" && !(options && options.asProcess)) {
      const text = (item.body || []).join("\n").trim();
      if (text) {
        const block = { id: item.id, text };
        turn.body.push(block);
        turn.segments.push({ type: "body", block });
      }
      return;
    }
    if (item.kind === "assistant") {
      const step = {
        id: item.id,
        kind: "progress",
        title: item.title,
        badge: item.badge,
        time: item.time,
        body: Array.isArray(item.body) ? item.body.slice() : [],
        event_name: item.event_name || ""
      };
      turn.process_steps.push(step);
      turn.segments.push({ type: "process", step });
      return;
    }
    const step = {
      id: item.id,
      kind: item.kind,
      title: item.title,
      badge: item.badge,
      time: item.time,
      body: Array.isArray(item.body) ? item.body.slice() : [],
      event_name: item.event_name || ""
    };
    turn.process_steps.push(step);
    turn.segments.push({ type: "process", step });
  }

  function groupTimelineItems(items) {
    const grouped = [];
    let activeTurn = null;

    function flushActiveTurn() {
      if (!activeTurn) return;
      if (activeTurn.body.length || activeTurn.process_steps.length) grouped.push(activeTurn);
      activeTurn = null;
    }

    (items || []).forEach((item) => {
      if (!item) return;
      if (item.kind === "assistant" || isAssistantProcessItem(item)) {
        if (!activeTurn) activeTurn = createAssistantTurnItem(item);
        appendAssistantTurnItem(activeTurn, item, { asProcess: false });
        return;
      }
      flushActiveTurn();
      grouped.push(item);
    });

    flushActiveTurn();
    return grouped;
  }

  function attachStreamingAssistantTurn(items) {
    if (!isAssistantTurnStreaming()) return items;
    const grouped = Array.isArray(items) ? items.slice() : [];
    const lastItem = grouped[grouped.length - 1] || null;
    if (lastItem && lastItem.kind === "assistant_turn") {
      lastItem.live = true;
      return grouped;
    }
    const liveTurn = createAssistantTurnItem({
      id: "assistant-live-" + String(state.activeSessionId || "current"),
      seq: Number((lastItem && lastItem.seq) || 0) + 1,
      badge: String((state.activeSession && state.activeSession.runtime_family) || ""),
      time: isoTime((state.activeSession && (state.activeSession.updated_at || state.activeSession.last_active_at || "")) || "")
    });
    liveTurn.live = true;
    liveTurn.placeholder = true;
    grouped.push(liveTurn);
    return grouped;
  }

  function summarizeAssistantProcess(processSteps, live) {
    return "执行过程";
  }

  function previewTimelineText(value, limit) {
    const normalized = String(value || "").replace(/\s+/g, " ").trim();
    if (!normalized) return "";
    if (normalized.length <= limit) return normalized;
    return normalized.slice(0, limit).trimEnd() + "...";
  }

  function assistantProcessStepKindLabel(step) {
    if (!step) return "过程";
    if (step.kind === "thinking") return "思考";
    if (step.kind === "progress") return "进展";
    if (step.kind === "work") return step.badge || "工作";
    if (step.kind === "tool" && step.title === "代码修改") return "修改";
    if (step.kind === "tool") return "工具";
    return "状态";
  }

  function assistantProcessStepTitle(step) {
    if (!step) return "";
    const fullText = Array.isArray(step.body) ? step.body.join("\n").trim() : "";
    if (step.kind === "thinking" || step.kind === "progress") return previewTimelineText(fullText || step.title || "思考中", 96);
    if (step.kind === "control" && step.event_name === "runtime_status") {
      return previewTimelineText(fullText || step.title || "运行状态", 96);
    }
    return String(step.title || step.badge || assistantProcessStepKindLabel(step));
  }

  function assistantProcessStepBody(step) {
    if (!step) return [];
    const fullText = Array.isArray(step.body) ? step.body.join("\n").trim() : "";
    if (step.kind === "thinking" || step.kind === "progress") {
      if (!fullText) return [];
      const preview = assistantProcessStepTitle(step);
      return fullText === preview ? [] : [fullText];
    }
    if (step.kind === "control" && step.event_name === "runtime_status") {
      if (!fullText) return [];
      const preview = assistantProcessStepTitle(step);
      return fullText === preview ? [] : [fullText];
    }
    return Array.isArray(step.body) ? step.body.slice() : [];
  }

  function renderAssistantProcessPulse() {
    return (
      '<span class="typing-dots ev-process-dots" aria-hidden="true">' +
        '<span></span><span></span><span></span>' +
      "</span>"
    );
  }

  function renderAssistantProcessSummary(live) {
    return (
      '<span class="ev-process-summary">执行过程</span>' +
      (live ? renderAssistantProcessPulse() : "")
    );
  }

  function normalizeAssistantMarkdown(value) {
    return String(value || "")
      .replace(/\r\n?/g, "\n")
      .replace(/(^|\s)(\*\*[\u4e00-\u9fffA-Za-z0-9 、]{2,14}\*\*)\s+/g, "$1\n\n$2\n");
  }

  function sanitizeMarkdownHref(value) {
    const href = String(value || "").trim();
    if (!href) return "";
    if (/^(https?:\/\/|\/|#)/i.test(href)) return href;
    return "";
  }

  function renderEscapedInlineMarkdown(value) {
    return escapeHtml(value)
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/__([^_]+)__/g, "<strong>$1</strong>");
  }

  function splitTrailingInlinePunctuation(value) {
    let token = String(value || "");
    let suffix = "";
    while (/[.,;!?，。；！？、]$/.test(token)) {
      suffix = token.slice(-1) + suffix;
      token = token.slice(0, -1);
    }
    while (/[\])}）】]$/.test(token)) {
      const left = token.includes("(") || token.includes("[") || token.includes("{") || token.includes("（") || token.includes("【");
      if (left) break;
      suffix = token.slice(-1) + suffix;
      token = token.slice(0, -1);
    }
    return { token, suffix };
  }

  function renderDialogueBrowserLink(href, labelHtml) {
    return '<a class="ev-md-link" href="' + escapeHtml(href) + '" target="_blank" rel="noopener noreferrer">' + labelHtml + "</a>";
  }

  function renderDialogueLocalPathLink(path, labelHtml) {
    return '<button class="ev-md-link ev-md-local-path" type="button" data-dialogue-local-path="' +
      escapeHtml(path) + '" title="在 Vizo 内预览 ' + escapeHtml(path) + '">' + labelHtml + "</button>";
  }

  function renderDialogueInlineEntity(value, labelHtml) {
    const raw = String(value || "").trim();
    if (!raw) return labelHtml;
    if (typeof isDialogueLocalPathCandidate === "function" && isDialogueLocalPathCandidate(raw)) {
      return renderDialogueLocalPathLink(raw, labelHtml);
    }
    const href = sanitizeMarkdownHref(raw);
    if (href) return renderDialogueBrowserLink(href, labelHtml);
    return labelHtml;
  }

  function parseUserAttachment(value, index) {
    if (value && typeof value === "object") {
      const name = String(value.name || value.filename || value.id || ("附件 " + (index + 1))).trim();
      const meta = [];
      const size = Number(value.size || 0);
      if (Number.isFinite(size) && size > 0) meta.push(formatAttachmentSize(size));
      const type = String(value.type || value.mime_type || "").trim();
      if (type) meta.push(type);
      return { name, meta: meta.join(" · ") };
    }
    const text = String(value || "").replace(/\r\n?/g, "\n").trim();
    const nameMatch = text.match(/^文件：(.+)$/m);
    const sizeMatch = text.match(/^大小：(.+)$/m);
    const typeMatch = text.match(/^类型：(.+)$/m);
    const name = String((nameMatch && nameMatch[1]) || ("附件 " + (index + 1))).trim();
    const meta = [];
    if (sizeMatch && String(sizeMatch[1] || "").trim()) meta.push(String(sizeMatch[1]).trim());
    if (typeMatch && String(typeMatch[1] || "").trim()) meta.push(String(typeMatch[1]).trim());
    if (/内容（已截断）/.test(text)) meta.push("已截断");
    if (/读取状态：/.test(text) || /内容：未读取/.test(text)) meta.push("未完整读取");
    return { name, meta: meta.join(" · ") };
  }

  function formatAttachmentSize(bytes) {
    const size = Number(bytes || 0);
    if (!Number.isFinite(size) || size <= 0) return "";
    const units = ["B", "KB", "MB", "GB"];
    let value = size;
    let unitIndex = 0;
    while (value >= 1024 && unitIndex < units.length - 1) {
      value = value / 1024;
      unitIndex += 1;
    }
    return (unitIndex === 0 ? String(Math.round(value)) : value.toFixed(value >= 10 ? 1 : 2)) + " " + units[unitIndex];
  }

  function renderUserAttachments(item) {
    const attachments = Array.isArray(item && item.attachments) ? item.attachments : [];
    if (!attachments.length) return "";
    return (
      '<div class="ev-attachments">' +
        attachments.map((attachment, index) => {
          const parsed = parseUserAttachment(attachment, index);
          return (
            '<span class="ev-attachment-chip" title="' + escapeHtml(parsed.name) + '">' +
              '<span class="ev-attachment-icon" aria-hidden="true">' +
                '<img src="/vizo/static/icons/material-design/outlined/insert_drive_file.svg" alt="" aria-hidden="true">' +
              "</span>" +
              '<span class="ev-attachment-main">' +
                '<span class="ev-attachment-name">' + escapeHtml(parsed.name) + "</span>" +
                (parsed.meta ? '<span class="ev-attachment-meta">' + escapeHtml(parsed.meta) + "</span>" : "") +
              "</span>" +
            "</span>"
          );
        }).join("") +
      "</div>"
    );
  }

  function renderAutoLinkedInlineText(value) {
    const text = String(value || "");
    const entityPattern = /(https?:\/\/[^\s<>"']+)|(^|[\s([{（【])((?:~\/|\.{1,2}\/|\/[A-Za-z0-9._@+-]+\/|[A-Za-z0-9._@+-]+\/)(?:[A-Za-z0-9._@+-]+\/)*[A-Za-z0-9._@+-]+(?::\d+(?::\d+)?)?|[A-Za-z0-9._@+-]+\.(?:c|cc|conf|cpp|cs|css|csv|dockerfile|env|gif|go|h|hpp|html|ico|ini|java|jpeg|jpg|js|json|jsonl|jsx|log|lua|md|mjs|pdf|png|py|rb|rs|scss|sh|sql|svg|swift|toml|ts|tsx|txt|vue|webp|xml|yaml|yml)(?::\d+(?::\d+)?)?)/gi;
    let output = "";
    let cursor = 0;
    let match = null;
    while ((match = entityPattern.exec(text))) {
      const leading = match[2] || "";
      const rawToken = match[1] || match[3] || "";
      const tokenStart = match.index + leading.length;
      const split = splitTrailingInlinePunctuation(rawToken);
      output += renderEscapedInlineMarkdown(text.slice(cursor, tokenStart));
      const label = renderEscapedInlineMarkdown(split.token);
      output += renderDialogueInlineEntity(split.token, label);
      output += renderEscapedInlineMarkdown(split.suffix);
      cursor = tokenStart + rawToken.length;
    }
    output += renderEscapedInlineMarkdown(text.slice(cursor));
    return output;
  }

  function renderInlineMarkdown(value) {
    const parts = String(value || "").split(/(`[^`]*`)/g);
    return parts.map((part) => {
      if (!part) return "";
      if (part.startsWith("`") && part.endsWith("`") && part.length >= 2) {
        return "<code>" + escapeHtml(part.slice(1, -1)) + "</code>";
      }
      let output = "";
      let cursor = 0;
      const linkPattern = /\[([^\]]+)\]\(([^)]+)\)/g;
      let match = null;
      while ((match = linkPattern.exec(part))) {
        output += renderAutoLinkedInlineText(part.slice(cursor, match.index));
        const href = String(match[2] || "").trim();
        const label = renderEscapedInlineMarkdown(match[1]);
        output += renderDialogueInlineEntity(href, label);
        cursor = match.index + match[0].length;
      }
      output += renderAutoLinkedInlineText(part.slice(cursor));
      return output;
    }).join("");
  }

  function renderAssistantMarkdown(value) {
    const lines = normalizeAssistantMarkdown(value).split("\n");
    const blocks = [];
    let paragraph = [];
    let listItems = [];
    let listType = "";
    let codeFence = null;
    let codeLines = [];

    function flushParagraph() {
      if (!paragraph.length) return;
      blocks.push('<p class="ev-md-p">' + renderInlineMarkdown(paragraph.join(" ").trim()) + "</p>");
      paragraph = [];
    }

    function flushList() {
      if (!listItems.length) return;
      const tag = listType === "ol" ? "ol" : "ul";
      blocks.push(
        '<' + tag + ' class="ev-md-list ev-md-' + tag + '">' +
          listItems.map((item) => "<li>" + renderInlineMarkdown(item) + "</li>").join("") +
        "</" + tag + ">"
      );
      listItems = [];
      listType = "";
    }

    function flushCode() {
      const lang = codeFence ? codeFence.replace(/[^\w+-]/g, "").slice(0, 24) : "";
      blocks.push(
        '<pre class="ev-md-codeblock"' + (lang ? (' data-lang="' + escapeHtml(lang) + '"') : "") + "><code>" +
          escapeHtml(codeLines.join("\n")) +
        "</code></pre>"
      );
      codeFence = null;
      codeLines = [];
    }

    lines.forEach((rawLine) => {
      const line = String(rawLine || "");
      const trimmed = line.trim();
      if (codeFence) {
        if (/^```/.test(trimmed)) flushCode();
        else codeLines.push(line);
        return;
      }
      const fenceMatch = trimmed.match(/^```([\w+-]*)/);
      if (fenceMatch) {
        flushParagraph();
        flushList();
        codeFence = fenceMatch[1] || "";
        codeLines = [];
        return;
      }
      if (!trimmed) {
        flushParagraph();
        flushList();
        return;
      }
      const headingMatch = trimmed.match(/^(#{1,4})\s+(.+)$/);
      if (headingMatch) {
        flushParagraph();
        flushList();
        const level = Math.min(headingMatch[1].length + 2, 6);
        blocks.push('<h' + level + ' class="ev-md-heading">' + renderInlineMarkdown(headingMatch[2]) + "</h" + level + ">");
        return;
      }
      const sectionMatch = trimmed.match(/^\*\*([^*]{2,28})\*\*$/);
      if (sectionMatch) {
        flushParagraph();
        flushList();
        blocks.push('<div class="ev-md-section-title">' + escapeHtml(sectionMatch[1]) + "</div>");
        return;
      }
      const unorderedMatch = trimmed.match(/^[-*]\s+(.+)$/);
      if (unorderedMatch) {
        flushParagraph();
        if (listType && listType !== "ul") flushList();
        listType = "ul";
        listItems.push(unorderedMatch[1]);
        return;
      }
      const orderedMatch = trimmed.match(/^\d+[.)]\s+(.+)$/);
      if (orderedMatch) {
        flushParagraph();
        if (listType && listType !== "ol") flushList();
        listType = "ol";
        listItems.push(orderedMatch[1]);
        return;
      }
      const quoteMatch = trimmed.match(/^>\s?(.+)$/);
      if (quoteMatch) {
        flushParagraph();
        flushList();
        blocks.push('<blockquote class="ev-md-quote">' + renderInlineMarkdown(quoteMatch[1]) + "</blockquote>");
        return;
      }
      flushList();
      paragraph.push(trimmed);
    });

    if (codeFence) flushCode();
    flushParagraph();
    flushList();
    return blocks.length ? blocks.join("") : '<p class="ev-md-p">' + renderInlineMarkdown(value) + "</p>";
  }

  function renderAssistantBodyBlock(block, index) {
    const text = typeof block === "string" ? block : String((block && block.text) || "");
    const key = "assistant-body:" + String((block && block.id) || index || text.slice(0, 24));
    return '<div class="ev-md-block' + timelineFreshClass(key) + '">' + renderAssistantMarkdown(text) + "</div>";
  }

  function isNaturalLanguageProcessStep(step) {
    return !!step && (step.kind === "thinking" || step.kind === "progress" || step.kind === "control");
  }

  function renderAssistantProcessStepTitle(step) {
    const title = assistantProcessStepTitle(step);
    return isNaturalLanguageProcessStep(step) ? renderInlineMarkdown(title) : escapeHtml(title);
  }

  function renderAssistantProcessStep(step) {
    const bodyText = assistantProcessStepBody(step).join("\n");
    const bodyKey = timelineBodyStateKey("process-step", step);
    const stepKey = "process-step:" + String((step && (step.id || step.time || step.title)) || "");
    const collapsed = bodyText.length > 200 && !isTimelineBodyExpanded(bodyKey) ? " collapsed" : "";
    const bodyAttr = bodyKey ? (' data-timeline-body-key="' + escapeHtml(bodyKey) + '"') : "";
    const bodyIsMarkdown = isNaturalLanguageProcessStep(step);
    const bodyHtml = bodyIsMarkdown ? renderAssistantMarkdown(bodyText) : escapeHtml(bodyText);
    const kindLabel = assistantProcessStepKindLabel(step);
    const showKindLabel = !(step && (step.kind === "thinking" || step.kind === "progress"));
    return (
      '<div class="ev-process-step ev-process-step-' + escapeHtml(step.kind || "control") + timelineFreshClass(stepKey) + '">' +
        '<div class="ev-process-step-head">' +
          (showKindLabel ? ('<span class="ev-process-step-kind">' + escapeHtml(kindLabel) + "</span>") : "") +
          '<span class="ev-process-step-title">' + renderAssistantProcessStepTitle(step) + "</span>" +
          (step.time ? ('<span class="ev-process-step-time">' + escapeHtml(step.time) + "</span>") : "") +
        "</div>" +
        (bodyText
          ? ('<div class="ev-result ev-process-step-body' + (bodyIsMarkdown ? " ev-process-step-body-markdown" : "") + collapsed + '"' + bodyAttr + ">" + bodyHtml + "</div>")
          : "") +
      "</div>"
    );
  }

  function renderAssistantProcessBlock(item, processSteps, groupIndex, live) {
    const steps = Array.isArray(processSteps) ? processSteps : [];
    const baseKey = timelineCardStateKey("assistant-turn", item);
    const processKey = baseKey ? (baseKey + ":process:" + String(groupIndex || 0)) : "";
    const processExpanded = isAssistantProcessExpanded(processKey, !!live, steps);
    const processSummary = summarizeAssistantProcess(processSteps, !!live);
    const processCardAttr = processKey ? (' data-timeline-card-key="' + escapeHtml(processKey) + '"') : "";
    if (!steps.length && !live) return "";
    return (
      '<div class="ev-process' + (processExpanded ? " expanded" : "") + (live ? " live" : "") + '"' + processCardAttr + ">" +
        (
          steps.length
            ? (
                '<button class="ev-process-toggle" type="button">' +
                  '<span class="ev-process-icon">&#9656;</span>' +
                  renderAssistantProcessSummary(!!live) +
                "</button>"
              )
            : (
                '<div class="ev-process-head">' +
                  '<span class="ev-process-summary">' + escapeHtml(processSummary) + "</span>" +
                  renderAssistantProcessPulse() +
                "</div>"
              )
        ) +
        '<div class="ev-process-detail">' +
          (steps.length
            ? steps.map((step) => renderAssistantProcessStep(step)).join("")
            : '<div class="ev-process-placeholder">Vizo 正在整理执行动作...</div>') +
        "</div>" +
      "</div>"
    );
  }

  function renderAssistantTurnSegments(item) {
    const segments = Array.isArray(item.segments) && item.segments.length
      ? item.segments
      : []
          .concat((item.process_steps || []).map((step) => ({ type: "process", step })))
          .concat((item.body || []).map((block) => ({ type: "body", block })));
    const parts = [];
    let processBuffer = [];
    let processGroupIndex = 0;

    function flushProcessBuffer(isLive) {
      if (!processBuffer.length && !isLive) return;
      parts.push(renderAssistantProcessBlock(item, processBuffer, processGroupIndex, isLive));
      processGroupIndex += 1;
      processBuffer = [];
    }

    segments.forEach((segment, index) => {
      if (!segment) return;
      if (segment.type === "process" && segment.step) {
        processBuffer.push(segment.step);
        return;
      }
      flushProcessBuffer(false);
      if (segment.type === "body" && segment.block) {
        parts.push(
          '<div class="ev-assistant-text">' +
            renderAssistantBodyBlock(segment.block, index) +
          "</div>"
        );
      }
    });
    flushProcessBuffer(!!item.live);
    return parts.filter(Boolean).join("");
  }

  function renderAssistantTurn(item) {
    const itemKey = timelineItemStateKey(item);
    const outputHtml = renderAssistantTurnSegments(item);
    return (
      '<div class="ev ev-output ev-assistant-turn' + timelineFreshClass(itemKey) + '"' + timelineKeyAttr(itemKey) + ">" +
        '<div class="ev-assistant-body">' + outputHtml + "</div>" +
        '<div class="ev-message-time">' + escapeHtml(item.time || "") + "</div>" +
      "</div>"
    );
  }

  function syncDialogueEmptyState(target, isEmpty) {
    const shell = target && target.closest ? target.closest(".stream-shell") : null;
    if (shell) shell.classList.toggle("dialogue-empty", !!isEmpty);
  }

  function renderTimeline() {
    const target = $("timeline");
    if (!target) return;
    const wasInitialized = !!state.timelineRenderInitialized;
    const scrollSnapshot = snapshotScrollBox(target);
    const processScrollState = captureAssistantProcessScrollState(target);
    const forceFollowBottom = Number(state.timelineFollowBottomUntil || 0) > Date.now();
    const shouldFollowBottom = forceFollowBottom || state.timelineStickToBottom !== false || (scrollSnapshot && scrollSnapshot.stickToBottom);
    if (state.mobileTab !== "dialogue" && IS_MOBILE_MODE) {
      syncDialogueEmptyState(target, false);
      target.innerHTML = "";
      return;
    }
    const items = buildTimelineItems();
    syncDialogueEmptyState(target, !items.length);
    if (!items.length) {
      target.innerHTML = '<div class="timeline-inner timeline-inner-empty" aria-hidden="true"></div>';
      state.timelineStickToBottom = true;
      return;
    }
    const timelineHtml = items.map((item) => {
      const lines = item.body || [];
      const itemKey = timelineItemStateKey(item);
      const itemFresh = itemKey ? timelineFreshClass(itemKey) : "";
      const itemKeyAttr = timelineKeyAttr(itemKey);
      if (item.kind === "user") {
        return (
          '<div class="ev ev-user' + itemFresh + '"' + itemKeyAttr + ">" +
            '<div class="ev-text">' + lines.map((line) => renderInlineMarkdown(line)).join("<br>") + "</div>" +
            renderUserAttachments(item) +
            '<div class="ev-message-time">' + escapeHtml(item.time || "") + "</div>" +
          "</div>"
        );
      }
      if (item.kind === "thinking") {
        const bodyText = lines.join("\n").trim();
        const summaryText = previewTimelineText(bodyText || item.title || "思考中", 96);
        const detail = bodyText && bodyText !== summaryText ? '<div class="ev-detail">' + escapeHtml(bodyText) + "</div>" : "";
        const cardKey = timelineCardStateKey("thinking", item);
        const expanded = isTimelineCardExpanded(cardKey, false);
        const cardAttr = cardKey ? (' data-timeline-card-key="' + escapeHtml(cardKey) + '"') : "";
        return (
          '<div class="ev ev-thinking' + (expanded ? " expanded" : "") + itemFresh + '"' + cardAttr + itemKeyAttr + ">" +
            '<span class="ev-icon">&#9656;</span>' +
            '<div class="ev-content"><div class="ev-summary">' + escapeHtml(summaryText) + "</div>" + detail + "</div>" +
          "</div>"
        );
      }
      if (item.kind === "tool" && item.title === "代码修改") {
        return (
          '<div class="ev ev-code' + itemFresh + '"' + itemKeyAttr + ">" +
            '<div class="ev-head"><span class="ev-label">Edit</span><span class="ev-file">' + escapeHtml(item.badge || item.title) + '</span></div>' +
            '<div class="ev-diff">' + escapeHtml(lines.join("\n")) + "</div>" +
          "</div>"
        );
      }
      if (item.kind === "tool") {
        const toolText = lines.join("\n");
        const bodyKey = timelineBodyStateKey("tool", item);
        const collapsed = toolText.length > 240 && !isTimelineBodyExpanded(bodyKey) ? " collapsed" : "";
        const bodyAttr = bodyKey ? (' data-timeline-body-key="' + escapeHtml(bodyKey) + '"') : "";
        return (
          '<div class="ev ev-tool' + itemFresh + '"' + itemKeyAttr + ">" +
            '<div class="ev-head"><span class="ev-label">' + escapeHtml(item.title || "Tool") + '</span><span class="ev-cmd">' + escapeHtml(item.badge || "tool") + '</span><span class="ev-time">' + escapeHtml(item.time || "") + "</span></div>" +
            '<div class="ev-result' + collapsed + '"' + bodyAttr + ">" + escapeHtml(toolText) + "</div>" +
          "</div>"
        );
      }
      if (item.kind === "image") {
        const image = item.image || {};
        const imageId = String(image.id || "");
        const promptText = String(image.revised_prompt || image.prompt || lines.join("\n") || "已生成图片").trim();
        return (
          '<div class="ev ev-output ev-image-card' + itemFresh + '"' + itemKeyAttr + ">" +
            '<button class="ev-image-frame" type="button" data-session-image-open="' + escapeHtml(imageId) + '" aria-label="查看生成图片">' +
              '<img class="ev-image-img" src="' + escapeHtml(image.url || "") + '" alt="' + escapeHtml(promptText || "生成图片") + '" loading="lazy">' +
            "</button>" +
            (promptText ? '<div class="ev-image-caption">' + escapeHtml(promptText) + "</div>" : "") +
            '<div class="ev-message-time">' + escapeHtml(item.time || "") + "</div>" +
          "</div>"
        );
      }
      if (item.kind === "confirm") {
        const displayTask = resolveDisplayTask();
        const confirmStep = displayTask && String(displayTask.task_id || "") === String(item.task_id || "")
          ? resolveConfirmReminderStep(displayTask)
          : null;
        const confirmControls = renderConfirmControls({
          task_id: item.task_id || (state.currentTask ? state.currentTask.task_id : ""),
          status: state.currentTask ? state.currentTask.status : "",
          pending_confirm: item.confirm
        }, "timeline", { includeSummary: false });
        const confirmDocs = displayTask && confirmStep
          ? renderConfirmOutputAccess(displayTask, confirmStep, {
              wrapperClass: "ev-confirm-docs",
              fallbackLabel: "查看步骤产出"
            })
          : "";
        return (
          '<div class="ev ev-confirm' + itemFresh + '"' + itemKeyAttr + ">" +
            '<div class="ev-head"><span class="ev-label">需要确认</span><span class="ev-source">' + escapeHtml(item.badge || item.title || "confirm") + '</span><span class="ev-time">' + escapeHtml(item.time || "") + "</span></div>" +
            '<div class="ev-text">' + lines.map((line) => escapeHtml(line)).join("<br>") + "</div>" +
            confirmDocs +
            '<div class="ev-confirm-actions">' + confirmControls + "</div>" +
          "</div>"
        );
      }
      if (item.kind === "assistant_turn") {
        return renderAssistantTurn(item);
      }
      return (
        '<div class="ev ev-output' + itemFresh + '"' + itemKeyAttr + ">" +
          '<div class="ev-head"><span class="ev-who">' + escapeHtml(item.title || item.kind || "Event") + '</span><span class="ev-time">' + escapeHtml(item.time || "") + "</span></div>" +
          '<div class="ev-assistant-text">' + lines.map((line, index) => renderAssistantBodyBlock({ id: item.id + ":" + index, text: line }, index)).join("") + "</div>" +
        "</div>"
      );
    }).join("");
    renderTimelineMarkup(target, timelineHtml);
    if (shouldFollowBottom) {
      const followBottom = () => {
        syncAssistantProcessScrollState(target, processScrollState, true);
        scrollScrollBoxToBottom(target, false);
        state.timelineStickToBottom = true;
      };
      followBottom();
      window.requestAnimationFrame(() => {
        followBottom();
        window.requestAnimationFrame(followBottom);
      });
      return;
    }
    syncAssistantProcessScrollState(target, processScrollState, false);
    restoreScrollBox(target, scrollSnapshot);
    window.requestAnimationFrame(() => {
      syncAssistantProcessScrollState(target, processScrollState, false);
      restoreScrollBox(target, scrollSnapshot);
    });
    state.timelineStickToBottom = false;
  }

  function currentLiveLogPlaceholder() {
    if (!state.selectedLiveNode) return "请选择一个步骤以查看动作日志。";
    return "当前节点没有动作日志事实源。";
  }

  function liveStepCardKey(task, stepNode, step) {
    return [
      task ? task.task_id : "",
      task ? taskProjectName(task) : "",
      stepNode ? stepNode.id : (step && (step.name || step.role || "")),
      stepNode ? stepNode.role : (step && (step.role || step.name || ""))
    ].join("|");
  }

  function isStepExpanded(task, step, stepNode) {
    if (!task || !stepNode) return false;
    const key = liveStepCardKey(task, stepNode, step);
    if (state.expandedLiveSteps.has(key)) return !!state.expandedLiveSteps.get(key);
    if (sameLiveNode(state.selectedLiveNode, stepNode)) return true;
    const status = String((step && step.status) || "").toLowerCase();
    return status === "running" || status === "waiting_confirm" || status === "paused";
  }

  function setStepExpanded(task, step, stepNode, expanded) {
    if (!task || !stepNode) return;
    state.expandedLiveSteps.set(liveStepCardKey(task, stepNode, step), !!expanded);
  }

  function livePanelCollapsingCards() {
    if (!state.livePanelCollapsingCards || typeof state.livePanelCollapsingCards.has !== "function") {
      state.livePanelCollapsingCards = new Set();
    }
    return state.livePanelCollapsingCards;
  }

  function livePanelOpeningCards() {
    if (!state.livePanelOpeningCards || typeof state.livePanelOpeningCards.has !== "function") {
      state.livePanelOpeningCards = new Set();
    }
    return state.livePanelOpeningCards;
  }

  function isLivePanelCardOpening(cardKey) {
    return !!(cardKey && livePanelOpeningCards().has(cardKey));
  }

  function isLivePanelCardCollapsing(cardKey) {
    return !!(cardKey && livePanelCollapsingCards().has(cardKey));
  }

  function clearLivePanelCardCollapse(cardKey) {
    if (cardKey) livePanelCollapsingCards().delete(cardKey);
  }

  function markLivePanelCardOpening(cardKey) {
    if (!cardKey) return;
    const opening = livePanelOpeningCards();
    opening.add(cardKey);
    window.setTimeout(() => {
      opening.delete(cardKey);
    }, LIVE_PANEL_COLLAPSE_MS + 80);
  }

  function markLivePanelCardCollapsing(cardKey) {
    if (!cardKey) return;
    const collapsing = livePanelCollapsingCards();
    livePanelOpeningCards().delete(cardKey);
    collapsing.add(cardKey);
    window.setTimeout(() => {
      if (!collapsing.delete(cardKey)) return;
      renderLivePanel();
    }, LIVE_PANEL_COLLAPSE_MS);
  }

  function renderLivePanelCollapse(cardKey, isOpen, bodyHtml) {
    if (!isOpen && !isLivePanelCardCollapsing(cardKey)) return "";
    const collapsing = !isOpen && isLivePanelCardCollapsing(cardKey);
    const opening = isOpen && isLivePanelCardOpening(cardKey);
    return (
      '<div class="task-monitor-collapse' + (collapsing ? " is-collapsing" : (opening ? " is-opening" : " is-open")) + '" data-live-collapse-key="' + escapeHtml(cardKey || "") + '">' +
        '<div class="task-monitor-collapse-inner">' + bodyHtml + "</div>" +
      "</div>"
    );
  }

  function resolveTaskStatusClass(status) {
    const value = String(status || "").toLowerCase();
    if (value === "completed") return "completed";
    if (value === "running") return "running";
    if (value === "waiting_confirm") return "waiting_confirm";
    if (value === "paused") return "paused";
    if (value === "failed" || value === "error" || value === "terminated" || value === "interrupted" || value === "rolled_back") return "danger";
    return "idle";
  }

  function resolveTaskTypeLabel(task) {
    const type = String((task && task.task_type) || "").toLowerCase();
    const mapping = {
      new_feature: "新功能",
      bug_fix: "缺陷修复",
      refactor: "重构",
      debug_embedded: "嵌入式调试",
      non_dev: "非开发任务"
    };
    if (mapping[type]) return mapping[type];
    if (task && (task.module_id || String(task.task_id || "").startsWith("hub-"))) return "Agent Hub";
    return "任务";
  }

  function formatStepTitle(step, index) {
    const label = String((step && (step.name || step.role || "")) || ("步骤 " + (index + 1))).trim();
    if (/^步骤\s*\d+/u.test(label)) return label;
    return "步骤 " + (index + 1) + ": " + label;
  }

  function formatStepStats(step) {
    const parts = [];
    const durationValue = Number(step && (step.duration_seconds || step.duration || 0));
    if (Number.isFinite(durationValue) && durationValue > 0) {
      parts.push(formatDuration(durationValue));
    }
    const costValue = Number(step && (step.cost_usd || 0));
    if (Number.isFinite(costValue) && costValue > 0) {
      parts.push(formatCurrency(costValue));
    }
    return parts.join(" • ");
  }

  function renderOutputChips(task, entries) {
    return (entries || []).map((item) => {
      if (!item) return "";
      const outputDoc = String(item.output_doc || "").trim();
      const previewUrl = String(item.preview_url || "").trim();
      const outputUrl = String(item.output_url || "").trim();
      const resolvedPreviewUrl = outputUrl || previewUrl;
      if (!outputDoc && !resolvedPreviewUrl) return "";
      const outputAvailable = item.output_available !== false;
      const isClickable = !!(
        resolvedPreviewUrl ||
        (outputAvailable && task && task.task_id && outputDoc && String(item.status || "").toLowerCase() !== "pending")
      );
      const title = String(item.description || item.name || outputDoc || "任务产出");
      return (
        '<button class="task-output-chip' + (isClickable ? " clickable" : " disabled") + '" type="button"' +
          (isClickable
            ? (
                ' data-output-preview="1"' +
                ' data-output-task-id="' + escapeHtml(task ? task.task_id : "") + '"' +
                ' data-output-doc="' + escapeHtml(outputDoc) + '"' +
                ' data-output-url="' + escapeHtml(resolvedPreviewUrl) + '"' +
                ' data-output-title="' + escapeHtml(title) + '"' +
                ' data-output-project="' + escapeHtml(task ? taskProjectName(task) : "") + '"'
              )
            : ' disabled') +
          '>' +
          '<span class="artifact-icon">DOC</span>' +
          '<span class="artifact-name">' + escapeHtml(outputDoc || title) + "</span>" +
        "</button>"
      );
    }).filter(Boolean).join("");
  }

  function renderConfirmOutputAccess(task, step, options) {
    if (!task) return "";
    const wrapperClass = options && options.wrapperClass ? options.wrapperClass : "task-monitor-reminder-preview";
    const outputChips = step ? renderOutputChips(task, [step]) : "";
    if (outputChips) {
      return '<div class="' + escapeHtml(wrapperClass) + '">' + outputChips + "</div>";
    }
    const pending = task.pending_confirm || {};
    const previewUrl = String((pending && (pending.output_url || pending.preview_url)) || "").trim();
    if (!previewUrl) return "";
    const outputDoc = String((step && step.output_doc) || pending.title || "任务产出");
    const outputTitle = String(pending.title || pending.summary || (step && step.name) || "任务产出");
    const label = options && options.fallbackLabel ? options.fallbackLabel : "查看步骤产出";
    return (
      '<div class="' + escapeHtml(wrapperClass) + '">' +
        '<button class="task-output-chip clickable" type="button" data-output-preview="1"' +
          ' data-output-task-id="' + escapeHtml(task.task_id || "") + '"' +
          ' data-output-doc="' + escapeHtml(outputDoc) + '"' +
          ' data-output-url="' + escapeHtml(previewUrl) + '"' +
          ' data-output-title="' + escapeHtml(outputTitle) + '"' +
          ' data-output-project="' + escapeHtml(taskProjectName(task)) + '">' +
          '<span class="artifact-icon">DOC</span>' +
          '<span class="artifact-name">' + escapeHtml(label) + "</span>" +
        "</button>" +
      "</div>"
    );
  }

  function renderLogEntries(logs) {
    return logs.map((entry) => {
      const label = String(entry.type || entry.action || entry.label || "log");
      const text = String(entry.text || entry.content || entry.message || JSON.stringify(entry, null, 2));
      return (
        '<div class="task-monitor-log-item">' +
          '<span class="task-monitor-log-label">' + escapeHtml(label) + "</span>" +
          '<div class="task-monitor-log-text">' + escapeHtml(text) + "</div>" +
        "</div>"
      );
    }).join("");
  }

  function resolveLogViewportId(task, node) {
    if (!task || !node) return "";
    if (node.type === "substep") {
      return liveSubstepLogCacheKey(task, node.subId || "", node.id, node.role);
    }
    return liveStepLogCacheKey(task, node.id, node.role);
  }

  function renderLogBlock(task, node, emptyText) {
    const logs = logsForNode(task, node);
    const viewportId = resolveLogViewportId(task, node);
    return (
      '<div class="task-monitor-detail-group">' +
        '<div class="task-monitor-section-title">执行日志</div>' +
        '<div class="task-monitor-log-box"' +
          (viewportId ? ' data-log-box-id="' + escapeHtml(viewportId) + '"' : "") +
          '>' +
          (logs.length
            ? renderLogEntries(logs)
            : '<div class="task-monitor-log-empty">' + escapeHtml(emptyText) + "</div>") +
        "</div>" +
      "</div>"
    );
  }

  function snapshotScrollBox(node) {
    if (!node) return null;
    const maxScrollTop = Math.max(0, node.scrollHeight - node.clientHeight);
    const scrollTop = Math.max(0, node.scrollTop || 0);
    return {
      scrollTop,
      stickToBottom: maxScrollTop - scrollTop <= 24
    };
  }

  function restoreScrollBox(node, snapshot) {
    if (!node || !snapshot) return;
    const maxScrollTop = Math.max(0, node.scrollHeight - node.clientHeight);
    node.scrollTop = snapshot.stickToBottom ? maxScrollTop : Math.min(snapshot.scrollTop || 0, maxScrollTop);
  }

  function renderTimelineMarkup(target, timelineHtml) {
    if (!target) return;
    const markup = '<div class="timeline-inner">' + timelineHtml + '<div class="timeline-bottom-sentinel" data-timeline-item-key="timeline-bottom" aria-hidden="true"></div></div>';
    const currentInner = target ? target.firstElementChild : null;
    if (!target || !currentInner || !currentInner.classList || !currentInner.classList.contains("timeline-inner") || currentInner.classList.contains("timeline-inner-empty")) {
      target.innerHTML = markup;
      state.timelineRenderInitialized = true;
      return;
    }
    const template = document.createElement("template");
    template.innerHTML = markup;
    const nextInner = template.content.firstElementChild;
    if (!nextInner) {
      target.innerHTML = markup;
      state.timelineRenderInitialized = true;
      return;
    }
    const currentByKey = new Map();
    Array.from(currentInner.children).forEach((child) => {
      const key = child.getAttribute("data-timeline-item-key") || "";
      if (key) currentByKey.set(key, child);
    });
    const usedNodes = new Set();
    Array.from(nextInner.children).forEach((nextChild, index) => {
      const key = nextChild.getAttribute("data-timeline-item-key") || "";
      let node = key ? currentByKey.get(key) : null;
      if (node) {
        currentByKey.delete(key);
        if (node.outerHTML !== nextChild.outerHTML) {
          patchTimelineDomNode(node, nextChild);
        }
      } else {
        node = nextChild.cloneNode(true);
      }
      const reference = currentInner.children[index] || null;
      if (node.parentNode !== currentInner) {
        currentInner.insertBefore(node, reference);
      } else if (node !== reference) {
        currentInner.insertBefore(node, reference);
      }
      usedNodes.add(node);
    });
    Array.from(currentInner.children).forEach((node) => {
      if (!usedNodes.has(node)) node.remove();
    });
    state.timelineRenderInitialized = true;
	  }

  function patchTimelineDomNode(current, next) {
    if (!current || !next) return;
    if (current.nodeType !== next.nodeType) {
      current.replaceWith(next.cloneNode(true));
      return;
    }
    if (current.nodeType === Node.TEXT_NODE) {
      if (current.nodeValue !== next.nodeValue) current.nodeValue = next.nodeValue;
      return;
    }
    if (current.nodeType !== Node.ELEMENT_NODE) return;
    if (current.tagName !== next.tagName) {
      current.replaceWith(next.cloneNode(true));
      return;
    }
    syncTimelineAttributes(current, next);
    patchTimelineChildren(current, next);
  }

  function syncTimelineAttributes(current, next) {
    Array.from(current.attributes || []).forEach((attr) => {
      if (!next.hasAttribute(attr.name)) current.removeAttribute(attr.name);
    });
    Array.from(next.attributes || []).forEach((attr) => {
      if (current.getAttribute(attr.name) !== attr.value) {
        current.setAttribute(attr.name, attr.value);
      }
    });
  }

  function patchTimelineChildren(current, next) {
    const maxLength = Math.max(current.childNodes.length, next.childNodes.length);
    for (let index = 0; index < maxLength; index += 1) {
      const currentChild = current.childNodes[index] || null;
      const nextChild = next.childNodes[index] || null;
      if (!nextChild && currentChild) {
        currentChild.remove();
        index -= 1;
        continue;
      }
      if (nextChild && !currentChild) {
        current.appendChild(nextChild.cloneNode(true));
        continue;
      }
      patchTimelineDomNode(currentChild, nextChild);
    }
  }

  function shouldReduceMotion() {
    return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  function scrollScrollBoxToBottom(node, smooth) {
    if (!node) return;
    const top = Math.max(0, node.scrollHeight - node.clientHeight);
    if (smooth && !shouldReduceMotion() && typeof node.scrollTo === "function") {
      node.scrollTo({ top, behavior: "smooth" });
      return;
    }
    node.scrollTop = top;
  }

  function assistantProcessDetailKey(node) {
    const processCard = node && node.closest ? node.closest(".ev-process") : null;
    return processCard ? String(processCard.getAttribute("data-timeline-card-key") || "") : "";
  }

  function captureAssistantProcessScrollState(root) {
    const snapshots = new Map();
    if (!root || !root.querySelectorAll) return snapshots;
    root.querySelectorAll(".ev-process.expanded .ev-process-detail").forEach((node) => {
      const key = assistantProcessDetailKey(node);
      if (key) snapshots.set(key, snapshotScrollBox(node));
    });
    return snapshots;
  }

  function syncAssistantProcessScrollState(root, snapshots, followActive) {
    if (!root || !root.querySelectorAll) return;
    const details = Array.from(root.querySelectorAll(".ev-process.expanded .ev-process-detail"));
    const liveDetails = details.filter((node) => {
      const processCard = node.closest ? node.closest(".ev-process") : null;
      return !!(processCard && processCard.classList.contains("live"));
    });
    const activeDetails = liveDetails.length ? liveDetails : details.slice(-1);
    details.forEach((node) => {
      const key = assistantProcessDetailKey(node);
      const snapshot = key && snapshots ? snapshots.get(key) : null;
      if (followActive && activeDetails.includes(node) && (!snapshot || snapshot.stickToBottom)) {
        scrollScrollBoxToBottom(node, false);
        return;
      }
      if (snapshot) restoreScrollBox(node, snapshot);
    });
  }

  function captureLivePanelUiState(target) {
    if (!target) return null;
    const scrollState = {
      stream: snapshotScrollBox(target.querySelector(".task-monitor-stream")),
      logs: new Map()
    };
    target.querySelectorAll("[data-log-box-id]").forEach((node) => {
      scrollState.logs.set(node.getAttribute("data-log-box-id") || "", snapshotScrollBox(node));
    });
    const activeElement = document.activeElement;
    let activeFeedback = null;
    if (
      activeElement &&
      target.contains(activeElement) &&
      typeof activeElement.matches === "function" &&
      activeElement.matches("[data-confirm-feedback]")
    ) {
      activeFeedback = {
        key: activeElement.getAttribute("data-confirm-feedback") || "",
        selectionStart: typeof activeElement.selectionStart === "number" ? activeElement.selectionStart : null,
        selectionEnd: typeof activeElement.selectionEnd === "number" ? activeElement.selectionEnd : null
      };
    }
    return {
      scrollState,
      activeFeedback
    };
  }

  function restoreLivePanelUiState(target, uiState) {
    if (!target || !uiState) return;
    const scrollState = uiState.scrollState || {};
    restoreScrollBox(target.querySelector(".task-monitor-stream"), scrollState.stream);
    target.querySelectorAll("[data-log-box-id]").forEach((node) => {
      restoreScrollBox(node, scrollState.logs && scrollState.logs.get(node.getAttribute("data-log-box-id") || ""));
    });
    if (!uiState.activeFeedback || !uiState.activeFeedback.key) return;
    const feedbackNode = Array.from(target.querySelectorAll("[data-confirm-feedback]")).find((node) => {
      return node.getAttribute("data-confirm-feedback") === uiState.activeFeedback.key;
    });
    if (!feedbackNode) return;
    feedbackNode.focus({ preventScroll: true });
    if (
      typeof uiState.activeFeedback.selectionStart === "number" &&
      typeof uiState.activeFeedback.selectionEnd === "number" &&
      typeof feedbackNode.setSelectionRange === "function"
    ) {
      feedbackNode.setSelectionRange(uiState.activeFeedback.selectionStart, uiState.activeFeedback.selectionEnd);
    }
  }

  function captureLivePanelScrollAnchor(target, sourceNode) {
    if (!target || !sourceNode || !sourceNode.closest) return null;
    const stream = target.querySelector(".task-monitor-stream");
    const card = sourceNode.closest("[data-live-card-key]");
    if (!stream || !card) return null;
    const key = card.getAttribute("data-live-card-key") || "";
    if (!key) return null;
    return {
      key,
      top: card.getBoundingClientRect().top
    };
  }

  function restoreLivePanelScrollAnchor(target, anchor) {
    if (!target || !anchor || !anchor.key) return;
    const stream = target.querySelector(".task-monitor-stream");
    if (!stream) return;
    const card = Array.from(target.querySelectorAll("[data-live-card-key]")).find((node) => {
      return node.getAttribute("data-live-card-key") === anchor.key;
    });
    if (!card) return;
    const delta = card.getBoundingClientRect().top - Number(anchor.top || 0);
    stream.scrollTop = Math.max(0, Number(stream.scrollTop || 0) + delta);
  }

  function rerenderConfirmPlacement(placement) {
    const placementKey = String(placement || "");
    if (placementKey === "timeline") {
      renderTimeline();
      return;
    }
    if (placementKey === "banner") {
      renderTaskBanner();
      return;
    }
    renderLivePanel();
  }

  function resolveTaskSwitcherItems() {
    const seen = new Set();
    const items = [];
    const pushTask = (task, options) => {
      if (!task) return;
      const taskId = String((options && options.taskId) || task.task_id || task.id || "").trim();
      if (!taskId || seen.has(taskId)) return;
      seen.add(taskId);
      items.push({
        taskId,
        task_name: taskDisplayTitle(task),
        description: taskDisplaySummary(task),
        status: task.status || "",
        cost_usd: task.cost_usd || 0,
        created_at: task.created_at || task.updated_at || "",
        is_live: !!(options && options.isLive),
        project: task.project || ""
      });
    };
    pushTask(state.currentTask, { taskId: state.currentTask && state.currentTask.task_id, isLive: true });
    (state.tasks || []).forEach((task) => pushTask(task, { taskId: task.id }));
    return items.sort((left, right) => {
      const leftRank = left.is_live || ["running", "waiting_confirm", "paused"].includes(String(left.status || "").toLowerCase()) ? 0 : 1;
      const rightRank = right.is_live || ["running", "waiting_confirm", "paused"].includes(String(right.status || "").toLowerCase()) ? 0 : 1;
      if (leftRank !== rightRank) return leftRank - rightRank;
      return String(right.created_at || "").localeCompare(String(left.created_at || ""));
    });
  }

  function renderTaskSwitcher(task) {
    const items = resolveTaskSwitcherItems();
    const activeTaskId = String((task && task.task_id) || "");
    const listHtml = renderTaskSwitcherList(items, activeTaskId);
    const menu = state.liveTaskSwitcherOpen
      ? (
          '<div class="task-switch-menu">' +
            '<div class="task-switch-menu-title">切换任务</div>' +
            listHtml +
          "</div>"
        )
      : "";
    return (
      '<div class="task-switcher" data-task-switcher-root="1">' +
        '<button class="task-switch-trigger" type="button" data-task-switcher-toggle="1">切换</button>' +
        menu +
      "</div>"
    );
  }

  function renderTaskSwitcherList(items, activeTaskId) {
    const taskItems = items || resolveTaskSwitcherItems();
    const selectedTaskId = String(activeTaskId || "");
    if (!taskItems.length) {
      return '<div class="task-switch-empty">当前项目下没有历史任务。</div>';
    }
    return (
      '<div class="task-switch-list">' +
        taskItems.map((item) => {
          const active = item.taskId === selectedTaskId ? " active" : "";
          return (
            '<button class="task-switch-item' + active + '" type="button" data-open-task="' + escapeHtml(item.taskId) + '">' +
              '<div class="task-switch-main">' +
                '<div class="task-switch-name">' + escapeHtml(item.task_name || item.taskId) + '</div>' +
                '<div class="task-switch-desc">' + escapeHtml(
                  (item.project ? ("[" + item.project + "] ") : "") +
                  (item.description || "查看该任务的步骤、日志与确认状态。")
                ) + "</div>" +
              "</div>" +
              '<div class="task-switch-meta">' +
                (item.is_live ? '<span class="pill running">直播中</span>' : "") +
                '<span class="pill ' + escapeHtml(String(item.status || "").toLowerCase()) + '">' + escapeHtml(formatStatus(item.status || "")) + "</span>" +
              "</div>" +
            "</button>"
          );
        }).join("") +
      "</div>"
    );
  }

  function renderPanelFooterActions(task) {
    if (!task) return "";
    const status = String((task.status || "")).toLowerCase();
    const buttons = [];
    if (status === "running" || status === "waiting_confirm") {
      buttons.push({ action: "pause", label: "暂停任务", cls: "warn" });
      buttons.push({ action: "terminate", label: "终止任务", cls: "danger" });
    } else if (status === "paused" || status === "failed") {
      buttons.push({ action: "resume", label: "继续任务", cls: "" });
      buttons.push({ action: "terminate", label: "终止任务", cls: "danger" });
    }
    if (!buttons.length) return "";
    return buttons.map((button) => {
      return '<button class="btn ' + (button.cls || "") + '" type="button" data-task-action="' + escapeHtml(button.action) +
        '" data-task-id="' + escapeHtml(task.task_id || "") +
        '" data-task-placement="panel-footer">' +
        escapeHtml(button.label) + "</button>";
    }).join("");
  }

  function resolveConfirmHostNode(task) {
    if (!task || !task.pending_confirm) return null;
    const current = findCurrentStepNode(task);
    if (current) return current;
    const waitingStep = (task.steps || []).find((step) => {
      const status = String(step.status || "").toLowerCase();
      return status === "running" || status === "waiting_confirm" || status === "paused";
    });
    return waitingStep ? buildStepNode(task, waitingStep) : firstAvailableLiveNode(task);
  }

  function stepHostsPendingConfirm(task, stepNode) {
    const confirmHost = resolveConfirmHostNode(task);
    return !!(confirmHost && stepNode && sameLiveNode(confirmHost, stepNode));
  }

  function resolveSubTaskHostNode(task) {
    if (!task || !(task.sub_tasks || []).length) return null;
    const current = findCurrentStepNode(task);
    if (current) return current;
    const hostStep = (task.steps || []).find((step) => {
      const status = String(step.status || "").toLowerCase();
      return status === "running" || status === "waiting_confirm" || status === "paused";
    });
    return hostStep ? buildStepNode(task, hostStep) : null;
  }

  function stepHostsSubTasks(task, stepNode) {
    const hostNode = resolveSubTaskHostNode(task);
    return !!(hostNode && stepNode && sameLiveNode(hostNode, stepNode));
  }

  function renderSubTaskCluster(task, options) {
    const subTasks = renderSubTaskItems(task);
    if (!subTasks) return "";
    const inline = options && options.inline === true;
    return (
      '<section class="task-monitor-cluster' + (inline ? " task-monitor-inline-cluster" : "") + '">' +
        '<div class="task-monitor-cluster-title">子任务拆解</div>' +
        '<div class="task-monitor-cluster-copy">大型需求已拆成子任务并按真实事实源展示，不伪造父步骤编号层级。</div>' +
        '<div class="task-monitor-subtasks">' + subTasks + "</div>" +
      "</section>"
    );
  }

  function resolveConfirmReminderStep(task) {
    if (!task || !task.pending_confirm) return null;
    const hostNode = resolveConfirmHostNode(task);
    if (!hostNode || hostNode.type !== "step") return null;
    return (task.steps || []).find((step) => {
      if (!step) return false;
      const name = String(step.name || step.role || "").trim();
      const role = String(step.role || step.name || "").trim();
      return name === hostNode.id && role === String(hostNode.role || "").trim();
    }) || (task.steps || []).find((step) => {
      if (!step) return false;
      const name = String(step.name || step.role || "").trim();
      return name === hostNode.id;
    }) || null;
  }

  function renderPanelConfirmReminder(task) {
    if (!task || !task.pending_confirm) return "";
    const pending = task.pending_confirm;
    const step = resolveConfirmReminderStep(task) || {};
    const previewBlock = renderConfirmOutputAccess(task, step, {
      wrapperClass: "task-monitor-reminder-preview",
      fallbackLabel: "查看步骤产出"
    });
    return (
      '<section class="task-monitor-reminder warning">' +
        '<div class="task-monitor-reminder-head">' +
          '<span class="task-monitor-reminder-pill">待处理提醒</span>' +
          '<div class="task-monitor-reminder-copy">' +
            '<div class="task-monitor-reminder-title">' + escapeHtml(pending.title || "需要审核") + "</div>" +
            '<div class="task-monitor-reminder-text">' + escapeHtml(pending.summary || "请确认当前步骤产出后继续。") + "</div>" +
          "</div>" +
        "</div>" +
        previewBlock +
        '<div class="task-monitor-reminder-actions">' + renderConfirmControls(task, "panel-reminder", { includeSummary: false }) + "</div>" +
      "</section>"
    );
  }

  function renderSubTaskItems(task) {
    return (task.sub_tasks || []).map((subTask) => {
      const name = subTask.name || String(subTask.id || "");
      if (!name) return "";
      const cacheKey = liveSubTaskCacheKey(task, name);
      const expanded = state.expandedLiveSubtasks.has(cacheKey);
      const shouldRenderBody = expanded || isLivePanelCardCollapsing(cacheKey);
      const cachedSubTask = findCachedSubTask(task, name);
      const subTaskStatus = String(subTask.status || "").toLowerCase();
      let innerHtml = "";
      if (shouldRenderBody) {
        if (cachedSubTask && cachedSubTask.steps && cachedSubTask.steps.length) {
          innerHtml = cachedSubTask.steps.map((subStep) => {
            const stepNode = buildSubstepNode(task, subTask, cachedSubTask, subStep);
            if (!stepNode) return "";
            const active = sameLiveNode(state.selectedLiveNode, stepNode) ? " active" : "";
            const subStepStatus = String(subStep.status || "").toLowerCase();
            const metaBadges = renderStepMetaBadges(subStep);
            const outputRow = renderOutputChips(task, [subStep]);
            const logsHtml = sameLiveNode(state.selectedLiveNode, stepNode)
              ? renderLogBlock(task, stepNode, "当前子步骤还没有执行日志。")
              : "";
            return (
              '<div class="task-monitor-substep' + active + '">' +
                '<div class="task-monitor-substep-head">' +
                  '<button class="task-monitor-substep-toggle" type="button" data-live-substep="' + escapeHtml(JSON.stringify({
                    subId: stepNode.subId,
                    subtaskName: stepNode.subtaskName,
                    name: stepNode.id,
                    role: stepNode.role
                  })) + '">' +
                    '<div class="task-monitor-substep-main">' +
                      '<span class="task-monitor-substep-title">' + escapeHtml(stepNode.id) + "</span>" +
                      '<span class="pill ' + escapeHtml(subStepStatus || "idle") + '">' + escapeHtml(formatStatus(subStep.status || "pending")) + "</span>" +
                    "</div>" +
                    '<div class="task-monitor-substep-meta">' +
                      '<span>' + escapeHtml(formatStepStats(subStep) || (subStep.description || "展开查看该子步骤日志。")) + "</span>" +
                      metaBadges +
                    "</div>" +
                  "</button>" +
                  (outputRow ? '<div class="task-monitor-output-row">' + outputRow + "</div>" : "") +
                "</div>" +
                logsHtml +
              "</div>"
            );
          }).join("");
        } else {
          innerHtml = '<div class="task-monitor-subtask-empty">' +
            escapeHtml(cachedSubTask ? "当前子任务没有内部步骤事实源。" : "正在加载子任务内部步骤...") +
          "</div>";
        }
      }
      return (
        '<article class="task-monitor-subtask' + (expanded ? " expanded" : "") + '" data-live-card-key="' + escapeHtml(cacheKey) + '">' +
          '<button class="task-monitor-subtask-head" type="button" data-live-subtask="' + escapeHtml(name) + '">' +
            '<div class="task-monitor-subtask-main">' +
              '<span class="task-monitor-subtask-name">' + escapeHtml(name) + "</span>" +
              '<span class="pill ' + escapeHtml(subTaskStatus || "idle") + '">' + escapeHtml(formatStatus(subTask.status || "pending")) + "</span>" +
            "</div>" +
            '<div class="task-monitor-subtask-desc">' + escapeHtml(subTask.description || "展开查看子任务内部步骤。") + "</div>" +
            '<span class="task-monitor-expand">' + (expanded ? "▴" : "▾") + "</span>" +
          "</button>" +
          renderLivePanelCollapse(cacheKey, expanded, '<div class="task-monitor-subtask-body">' + innerHtml + "</div>") +
        "</article>"
      );
    }).join("");
  }

  function renderStepDetails(task, step, stepNode) {
    const status = String(step.status || "").toLowerCase();
    const description = String(step.description || "").trim();
    const logsBlock = renderLogBlock(
      task,
      stepNode,
      status === "pending" ? "等待前序步骤完成后，这里会出现执行日志。" : "当前步骤还没有执行日志。"
    );
    const subTaskCluster = stepHostsSubTasks(task, stepNode) ? renderSubTaskCluster(task, { inline: true }) : "";
    const descriptionHtml = description
      ? '<div class="task-monitor-step-note">' + escapeHtml(description) + "</div>"
      : "";
    if (status === "pending") {
      return (
        '<div class="task-monitor-step-detail">' +
          descriptionHtml +
          '<div class="task-monitor-awaiting">等待前序步骤完成后开始执行。</div>' +
        "</div>"
      );
    }
    return (
      '<div class="task-monitor-step-detail">' +
        descriptionHtml +
        (step.error ? '<div class="task-monitor-error">' + escapeHtml(step.error) + "</div>" : "") +
        subTaskCluster +
        logsBlock +
      "</div>"
    );
  }

  function renderStepCard(task, step, index) {
    const stepNode = buildStepNode(task, step);
    if (!stepNode) return "";
    const status = String(step.status || "").toLowerCase();
    const expanded = isStepExpanded(task, step, stepNode);
    const selected = sameLiveNode(state.selectedLiveNode, stepNode) ? " active" : "";
    const metaBadges = renderStepMetaBadges(step);
    const statText = formatStepStats(step);
    const headerOutputs = renderOutputChips(task, [step]);
    const icon = status === "completed" ? "✓" : (status === "running" ? "◌" : (status === "waiting_confirm" ? "!" : "•"));
    const cardKey = liveStepCardKey(task, stepNode, step);
    const detailHtml = renderLivePanelCollapse(
      cardKey,
      expanded,
      renderStepDetails(task, step, stepNode)
    );
    return (
      '<article class="task-monitor-step-card ' + escapeHtml(resolveTaskStatusClass(status)) + selected + (expanded ? " expanded" : "") + '" data-live-card-key="' + escapeHtml(cardKey) + '">' +
        '<div class="task-monitor-step-summary">' +
          '<button class="task-monitor-step-toggle" type="button" data-live-step="' + escapeHtml(JSON.stringify({
            name: stepNode.id,
            role: stepNode.role
          })) + '">' +
            '<div class="task-monitor-step-main">' +
              '<span class="task-monitor-step-icon">' + escapeHtml(icon) + "</span>" +
              '<div class="task-monitor-step-copy">' +
                '<div class="task-monitor-step-title">' + escapeHtml(formatStepTitle(step, index)) + "</div>" +
                '<div class="task-monitor-step-meta">' +
                  '<span class="task-monitor-step-status">' + escapeHtml(formatStatus(step.status || "pending")) + "</span>" +
                  (statText ? '<span class="task-monitor-step-stat">' + escapeHtml(statText) + "</span>" : "") +
                  metaBadges +
                "</div>" +
              "</div>" +
            "</div>" +
            '<span class="task-monitor-expand">' + (expanded ? "▴" : "▾") + "</span>" +
          "</button>" +
          (headerOutputs ? '<div class="task-monitor-output-row">' + headerOutputs + "</div>" : "") +
        "</div>" +
        detailHtml +
      "</article>"
    );
  }

  function renderLivePanel() {
    const target = $("livePanelBody");
    if (!target) return;
    const uiState = captureLivePanelUiState(target);
    const scrollAnchor = state.livePanelScrollAnchor || null;
    const task = resolveDisplayTask();
    if (!task) {
      const taskList = renderTaskSwitcherList(resolveTaskSwitcherItems(), "");
      target.innerHTML =
        '<div class="task-monitor-shell">' +
          '<div class="task-monitor-header">' +
            '<div class="task-monitor-topbar">' +
              '<span class="task-monitor-status-chip idle">' +
                '<span class="task-monitor-status-dot"></span>' +
                "idle" +
              "</span>" +
            "</div>" +
            '<h3 class="task-monitor-title">任务直播</h3>' +
            '<div class="task-monitor-empty-state">当前没有运行中的任务。下面是当前项目的历史任务，可以点开查看步骤、日志和产物。</div>' +
          "</div>" +
          '<div class="task-monitor-stream">' + taskList + "</div>" +
        "</div>";
      state.livePanelScrollAnchor = null;
      renderMobileTaskScreen();
      return;
    }

    const steps = task.steps || [];
    const stepItems = steps.length
      ? steps.map((step, index) => renderStepCard(task, step, index)).join("")
      : '<div class="task-monitor-empty-state">当前任务还没有步骤信息。</div>';
    const detachedSubTaskCluster = resolveSubTaskHostNode(task) ? "" : renderSubTaskCluster(task, { inline: false });
    const confirmReminder = renderPanelConfirmReminder(task);
    const footerButtons = renderPanelFooterActions(task);

    target.innerHTML =
      '<div class="task-monitor-shell">' +
        '<div class="task-monitor-header">' +
          '<div class="task-monitor-topbar">' +
            '<span class="task-monitor-status-chip ' + escapeHtml(resolveTaskStatusClass(task.status || "")) + '">' +
              '<span class="task-monitor-status-dot"></span>' +
              escapeHtml(formatStatus(task.status || "running")) +
            "</span>" +
            renderTaskSwitcher(task) +
          "</div>" +
          '<h3 class="task-monitor-title">' + escapeHtml(taskDisplayTitle(task)) + "</h3>" +
          '<div class="task-monitor-meta-grid">' +
            '<div class="task-monitor-meta-item"><span class="task-monitor-meta-label">ID</span><span class="task-monitor-meta-value">' + escapeHtml(task.task_id || "-") + "</span></div>" +
            '<div class="task-monitor-meta-item"><span class="task-monitor-meta-label">类型</span><span class="task-monitor-meta-value">' + escapeHtml(resolveTaskTypeLabel(task)) + "</span></div>" +
            '<div class="task-monitor-meta-item"><span class="task-monitor-meta-label">耗时</span><span class="task-monitor-meta-value">' + escapeHtml(formatDuration(computeTaskElapsed(task))) + "</span></div>" +
            '<div class="task-monitor-meta-item"><span class="task-monitor-meta-label">费用</span><span class="task-monitor-meta-value emphasis">' + escapeHtml(formatCurrency(task.cost_usd || 0)) + "</span></div>" +
          "</div>" +
        "</div>" +
        '<div class="task-monitor-stream">' +
          stepItems +
          detachedSubTaskCluster +
        "</div>" +
        confirmReminder +
        (
          footerButtons
            ? '<div class="task-monitor-footer">' + footerButtons + "</div>"
            : ""
        ) +
      "</div>";
    restoreLivePanelUiState(target, uiState);
    restoreLivePanelScrollAnchor(target, scrollAnchor);
    state.livePanelScrollAnchor = null;
    renderMobileTaskScreen();
  }

  function handleClick(event) {
    if (state.liveTaskSwitcherOpen && !event.target.closest("[data-task-switcher-root]")) {
      state.liveTaskSwitcherOpen = false;
      renderLivePanel();
    }

    const dismissBanner = event.target.closest("[data-task-banner-dismiss]");
    if (dismissBanner) {
      event.preventDefault();
      event.stopPropagation();
      const task = state.currentTask || state.taskDetail;
      if (task) dismissTaskBanner(task);
      renderTaskBanner();
      return true;
    }

    const taskAction = event.target.closest("[data-task-action]");
    if (taskAction) {
      runTaskAction({
        action: taskAction.getAttribute("data-task-action"),
        taskId: taskAction.getAttribute("data-task-id"),
        placement: taskAction.getAttribute("data-task-placement"),
        label: (taskAction.textContent || "").trim()
      }).catch((error) => toast(error.message, "error"));
      return true;
    }

    const confirmSubmit = event.target.closest("[data-confirm-submit]");
    if (confirmSubmit) {
      runConfirmAction({
        action: "f",
        taskId: confirmSubmit.getAttribute("data-task-id"),
        requestId: confirmSubmit.getAttribute("data-request-id"),
        placement: confirmSubmit.getAttribute("data-confirm-placement"),
        submitFeedback: true,
        editorKey: confirmSubmit.getAttribute("data-editor-key"),
        draftKey: confirmSubmit.getAttribute("data-draft-key"),
        label: (confirmSubmit.textContent || "").trim()
      }).catch((error) => toast(error.message, "error"));
      return true;
    }

    const confirmCancel = event.target.closest("[data-confirm-cancel]");
    if (confirmCancel) {
      const editorKey = String(
        confirmCancel.getAttribute("data-editor-key") ||
        [confirmCancel.getAttribute("data-confirm-placement") || "panel",
          confirmCancel.getAttribute("data-draft-key") ||
          confirmCancel.getAttribute("data-request-id") ||
          confirmCancel.getAttribute("data-task-id") ||
          ""].join("::") ||
        ""
      );
      const draftKey = String(
        confirmCancel.getAttribute("data-draft-key") ||
        confirmCancel.getAttribute("data-request-id") ||
        confirmCancel.getAttribute("data-task-id") ||
        ""
      );
      closeConfirmEditor(editorKey, { clearDraft: false });
      clearConfirmDraft(draftKey);
      rerenderConfirmPlacement(confirmCancel.getAttribute("data-confirm-placement"));
      return true;
    }

    const confirmAction = event.target.closest("[data-confirm-action]");
    if (confirmAction) {
      const action = confirmAction.getAttribute("data-confirm-action");
      const editorKey = String(
        confirmAction.getAttribute("data-editor-key") ||
        [confirmAction.getAttribute("data-confirm-placement") || "panel",
          confirmAction.getAttribute("data-draft-key") ||
          confirmAction.getAttribute("data-request-id") ||
          confirmAction.getAttribute("data-task-id") ||
          ""].join("::") ||
        ""
      );
      const draftKey = String(
        confirmAction.getAttribute("data-draft-key") ||
        confirmAction.getAttribute("data-request-id") ||
        confirmAction.getAttribute("data-task-id") ||
        ""
      );
      if (action === "f") {
        openConfirmEditor(editorKey);
        rerenderConfirmPlacement(confirmAction.getAttribute("data-confirm-placement"));
        return true;
      }
      runConfirmAction({
        action,
        taskId: confirmAction.getAttribute("data-task-id"),
        requestId: confirmAction.getAttribute("data-request-id"),
        placement: confirmAction.getAttribute("data-confirm-placement"),
        editorKey,
        draftKey,
        label: (confirmAction.textContent || "").trim()
      }).catch((error) => toast(error.message, "error"));
      return true;
    }

    const outputPreview = event.target.closest("[data-output-preview]");
    if (outputPreview) {
      openTaskOutputPreview(
        outputPreview.getAttribute("data-output-task-id"),
        outputPreview.getAttribute("data-output-doc"),
        outputPreview.getAttribute("data-output-url"),
        outputPreview.getAttribute("data-output-title"),
        outputPreview.getAttribute("data-output-project")
      );
      return true;
    }

    const toggleSwitcher = event.target.closest("[data-task-switcher-toggle]");
    if (toggleSwitcher) {
      state.liveTaskSwitcherOpen = !state.liveTaskSwitcherOpen;
      renderLivePanel();
      return true;
    }

    const focusLive = event.target.closest("[data-task-focus-live]");
    if (focusLive) {
      focusLiveTask().catch((error) => toast(error.message, "error"));
      return true;
    }

    const liveStep = event.target.closest("[data-live-step]");
    if (liveStep) {
      const payload = JSON.parse(liveStep.getAttribute("data-live-step"));
      const displayTask = resolveDisplayTask();
      if (!displayTask) return true;
      state.livePanelScrollAnchor = captureLivePanelScrollAnchor($("livePanelBody"), liveStep);
      const stepNode = {
        type: "step",
        taskId: displayTask.task_id || "",
        id: payload.name,
        role: payload.role
      };
      const stepData = resolveSelectedLiveNodeData(displayTask, stepNode);
      const expanded = isStepExpanded(displayTask, stepData, stepNode);
      const nextExpanded = !(sameLiveNode(state.selectedLiveNode, stepNode) && expanded);
      const cardKey = liveStepCardKey(displayTask, stepNode, stepData);
      state.selectedLiveNode = stepNode;
      state.manualLiveSelection = true;
      if (nextExpanded) {
        clearLivePanelCardCollapse(cardKey);
        markLivePanelCardOpening(cardKey);
      } else {
        markLivePanelCardCollapsing(cardKey);
      }
      setStepExpanded(displayTask, stepData, stepNode, nextExpanded);
      if (!nextExpanded) {
        renderLivePanel();
        return true;
      }
      loadLiveNodeLogs(displayTask, stepNode).then(renderLivePanel).catch((error) => toast(error.message, "error"));
      return true;
    }

    const liveSubstep = event.target.closest("[data-live-substep]");
    if (liveSubstep) {
      const payload = JSON.parse(liveSubstep.getAttribute("data-live-substep"));
      const displayTask = resolveDisplayTask();
      if (!displayTask) return true;
      state.selectedLiveNode = {
        type: "substep",
        taskId: displayTask.task_id || "",
        subId: payload.subId || "",
        subtaskName: payload.subtaskName || "",
        id: payload.name,
        role: payload.role
      };
      state.manualLiveSelection = true;
      loadSelectedLiveNode().then(renderLivePanel).catch((error) => toast(error.message, "error"));
      return true;
    }

    const liveSubtask = event.target.closest("[data-live-subtask]");
    if (liveSubtask) {
      const task = resolveDisplayTask();
      const subName = liveSubtask.getAttribute("data-live-subtask") || "";
      if (!task || !subName) return true;
      state.livePanelScrollAnchor = captureLivePanelScrollAnchor($("livePanelBody"), liveSubtask);
      const cacheKey = liveSubTaskCacheKey(task, subName);
      if (state.expandedLiveSubtasks.has(cacheKey)) {
        state.expandedLiveSubtasks.delete(cacheKey);
        markLivePanelCardCollapsing(cacheKey);
      } else {
        clearLivePanelCardCollapse(cacheKey);
        markLivePanelCardOpening(cacheKey);
        state.expandedLiveSubtasks.add(cacheKey);
      }
      loadSubTaskDetail(task, subName, { force: true }).then(() => {
        ensureLiveSelection();
        return loadSelectedLiveNode();
      }).then(renderLivePanel).catch((error) => toast(error.message, "error"));
      return true;
    }

    const openTask = event.target.closest("[data-open-task]");
    if (openTask) {
      const targetTaskId = openTask.getAttribute("data-open-task") || "";
      const targetTask = resolveTaskSwitcherItems().find((item) => item.taskId === targetTaskId);
      const projectName = targetTask && targetTask.project
        ? targetTask.project
        : (currentProject() ? currentProject().name : "");
      state.liveTaskSwitcherOpen = false;
      loadTaskDetail(targetTaskId, projectName, { persistSelection: true }).then(() => {
        renderAll();
        if (IS_MOBILE_MODE) openSheet("task");
      }).catch((error) => toast(error.message, "error"));
      return true;
    }

    return false;
  }

  function handleInput(event) {
    const confirmFeedback = event.target.closest("[data-confirm-feedback]");
    if (!confirmFeedback) return false;
    setConfirmDraft(confirmFeedback.getAttribute("data-confirm-feedback"), confirmFeedback.value || "");
    return true;
  }

  return {
    resetState,
    refreshTaskState,
    scheduleTaskRefresh,
    loadTaskDetail,
    runTaskAction,
    runConfirmAction,
    focusLiveTask,
    renderTaskBanner,
    renderTimeline,
    currentLiveLogPlaceholder,
    renderSubTaskItems,
    renderLivePanel,
    handleClick,
    handleInput
  };
}
