from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "lib/templates/dialogue_console/workbench.html"
RUNTIME = ROOT / "lib/templates/dialogue_console/workbench-runtime.js"


def test_workbench_template_has_theme_bootstrap():
    html = TEMPLATE.read_text(encoding="utf-8")
    storage_index = html.index("localStorage.getItem('opus_theme')")
    style_index = html.index("<style>")
    assert storage_index < style_index
    assert '[data-theme="light"]' in html
    assert "--topbar-bg" in html
    assert "--loading-bg" in html


def test_workbench_runtime_uses_shared_theme_key_and_injection_rules():
    runtime = RUNTIME.read_text(encoding="utf-8")
    assert 'const THEME_STORAGE_KEY = "opus_theme"' in runtime
    assert "function normalizeTheme(value)" in runtime
    assert "function buildWorkbenchInjectedRules(theme)" in runtime
    assert "doc.documentElement.setAttribute(\"data-theme\", normalizedTheme)" in runtime
    assert "background:#06111d!important" not in runtime
