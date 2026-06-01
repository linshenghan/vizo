(function () {
  const root = document.body;
  const kind = root.dataset.kind || "settings";
  const target = root.dataset.target || "/vizo/console";
  const backTarget = root.dataset.return || "/vizo/console/dialogue";
  const frame = document.getElementById("workbenchFrame");
  const loading = document.getElementById("workbenchLoading");
  const backBtn = document.getElementById("workbenchBackBtn");

  function buildWorkbenchInjectedRules() {
    const pageBg = "#06111d";
    return [
      "html,body,#app,.main-area{height:100%!important;min-height:0!important;background:" + pageBg + "!important;}",
      "body{overflow:hidden!important;}",
      "#sidebar,.toolbar{display:none!important;}",
      ".main-area{width:100%!important;flex:1 1 auto!important;}",
      ".agents-back-btn,.settings-back-btn{display:none!important;}",
    ];
  }

  function injectWorkbenchRules(doc) {
    if (!doc || !doc.head) return;
    let style = doc.getElementById("dialogueStandaloneWorkbenchStyle");
    if (!style) {
      style = doc.createElement("style");
      style.id = "dialogueStandaloneWorkbenchStyle";
      doc.head.appendChild(style);
    }
    const rules = buildWorkbenchInjectedRules();
    if (kind === "agents") {
      rules.push(".agents-view{height:100%!important;min-height:0!important;display:flex!important;}");
    } else {
      rules.push(".settings-view{height:100%!important;min-height:0!important;display:flex!important;}");
      rules.push(".settings-page-shell{flex:1 1 auto!important;min-height:0!important;}");
    }
    style.textContent = rules.join("");
  }

  function syncWorkbenchFrame(source) {
    try {
      if (frame && frame.contentDocument) injectWorkbenchRules(frame.contentDocument);
    } catch (error) {
      console.warn("workbench_iframe_sync_failed", {
        page: "dialogue-workbench",
        target: kind,
        source: source || "",
        error: String(error && error.message || error)
      });
    }
  }

  function onFrameReady() {
    try {
      injectWorkbenchRules(frame.contentDocument);
    } catch (error) {
      console.warn("workbench_iframe_sync_failed", {
        page: "dialogue-workbench",
        target: kind,
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

  syncWorkbenchFrame("initial");
  window.addEventListener("focus", () => syncWorkbenchFrame("focus"));
})();
