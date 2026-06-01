(function () {
  const root = document.body;
  const kind = root.dataset.kind || "settings";
  const target = root.dataset.target || "/vizo/console";
  const backTarget = root.dataset.return || "/vizo/console/dialogue";
  const frame = document.getElementById("workbenchFrame");
  const loading = document.getElementById("workbenchLoading");
  const backBtn = document.getElementById("workbenchBackBtn");
  const THEME_STORAGE_KEY = "opus_theme";

  function normalizeTheme(value) {
    return value === "light" ? "light" : "dark";
  }

  function readStoredTheme() {
    try {
      return normalizeTheme(localStorage.getItem(THEME_STORAGE_KEY));
    } catch (error) {
      return "dark";
    }
  }

  function setWorkbenchTheme(theme) {
    document.documentElement.setAttribute("data-theme", normalizeTheme(theme));
  }

  function buildWorkbenchInjectedRules(theme) {
    const normalizedTheme = normalizeTheme(theme);
    const pageBg = normalizedTheme === "light" ? "#f6f8fa" : "#06111d";
    return [
      "html,body,#app,.main-area{height:100%!important;min-height:0!important;background:" + pageBg + "!important;}",
      "body{overflow:hidden!important;}",
      "#sidebar,.toolbar{display:none!important;}",
      ".main-area{width:100%!important;flex:1 1 auto!important;}",
      ".agents-back-btn,.settings-back-btn{display:none!important;}",
    ];
  }

  function injectChromeKiller(doc, theme) {
    if (!doc || !doc.head) return;
    let style = doc.getElementById("dialogueStandaloneWorkbenchStyle");
    if (!style) {
      style = doc.createElement("style");
      style.id = "dialogueStandaloneWorkbenchStyle";
      doc.head.appendChild(style);
    }
    const normalizedTheme = normalizeTheme(theme);
    const rules = buildWorkbenchInjectedRules(normalizedTheme);
    doc.documentElement.setAttribute("data-theme", normalizedTheme);
    if (kind === "agents") {
      rules.push(".agents-view{height:100%!important;min-height:0!important;display:flex!important;}");
    } else {
      rules.push(".settings-view{height:100%!important;min-height:0!important;display:flex!important;}");
      rules.push(".settings-page-shell{flex:1 1 auto!important;min-height:0!important;}");
    }
    style.textContent = rules.join("");
  }

  function syncWorkbenchTheme(source) {
    const theme = readStoredTheme();
    setWorkbenchTheme(theme);
    try {
      // workbench 内页是同源 Web Console，需要外壳同步主题，但不改变内页业务状态。
      if (frame && frame.contentDocument) injectChromeKiller(frame.contentDocument, theme);
    } catch (error) {
      console.warn("theme_iframe_sync_failed", {
        page: "dialogue-workbench",
        target: kind,
        theme: theme,
        source: source || "",
        error: String(error && error.message || error)
      });
    }
  }

  function onFrameReady() {
    try {
      injectChromeKiller(frame.contentDocument, readStoredTheme());
    } catch (error) {
      console.warn("theme_iframe_sync_failed", {
        page: "dialogue-workbench",
        target: kind,
        theme: readStoredTheme(),
        error: String(error && error.message || error)
      });
    }
    if (loading) loading.hidden = true;
  }

  if (backBtn) {
    backBtn.addEventListener("click", () => {
      window.location.href = backTarget;
    });
  }

  if (frame) {
    frame.addEventListener("load", onFrameReady);
    frame.src = target;
  }

  syncWorkbenchTheme("initial");
  window.addEventListener("focus", () => syncWorkbenchTheme("focus"));
  window.addEventListener("storage", (event) => {
    if (event.key === THEME_STORAGE_KEY) syncWorkbenchTheme("storage");
  });
})();
