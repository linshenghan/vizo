"""Dialogue Console page renderer."""

from importlib import resources
from urllib.parse import quote


_DIALOGUE_RESOURCE_PACKAGE = "lib.templates.dialogue_console"


def _read_dialogue_resource(name: str) -> str:
    return resources.files(_DIALOGUE_RESOURCE_PACKAGE).joinpath(name).read_text(encoding="utf-8")


def _dialogue_runtime() -> str:
    live_panel_component = _read_dialogue_resource("live-panel-component.js").strip()
    runtime = _read_dialogue_resource("runtime.js").strip()
    return f"{live_panel_component}\n\n{runtime}"


def _dialogue_template() -> str:
    return _read_dialogue_resource("pc.html")


def _dialogue_workbench_template() -> str:
    return _read_dialogue_resource("workbench.html")


def _dialogue_workbench_runtime() -> str:
    return _read_dialogue_resource("workbench-runtime.js").strip()


def render_dialogue_console_html() -> str:
    """Return the PC Dialogue Console HTML."""
    html = _dialogue_template()
    return (
        html.replace("__DIALOGUE_MODE__", "desktop")
        .replace("__DIALOGUE_RUNTIME__", _dialogue_runtime())
    )


def render_dialogue_workbench_html(kind: str, *, section: str = "main-session") -> str:
    """Return standalone Dialogue workbench page for settings or agents."""
    normalized = "agents" if kind == "agents" else "settings"
    title = "智能体" if normalized == "agents" else "设置"
    target = "/vizo/console#agents"
    if normalized == "settings":
        target = f"/vizo/console#settings/{quote(section or 'main-session', safe='')}"
    html = _dialogue_workbench_template()
    return (
        html.replace("__WORKBENCH_KIND__", normalized)
        .replace("__WORKBENCH_TITLE__", title)
        .replace("__WORKBENCH_TARGET__", target)
        .replace("__WORKBENCH_RETURN__", "/vizo/console/dialogue")
        .replace("__WORKBENCH_RUNTIME__", _dialogue_workbench_runtime())
    )
