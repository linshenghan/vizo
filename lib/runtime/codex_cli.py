from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path


CODEX_AUTOMATION_SANDBOX = "workspace-write"
CODEX_AUTOMATION_APPROVAL = "never"
CODEX_AUTOMATION_SANDBOX_MODES = {
    "read-only",
    "workspace-write",
    "danger-full-access",
}


def resolve_codex_automation_sandbox(sandbox_mode: str | None = None) -> str:
    mode = str(sandbox_mode or CODEX_AUTOMATION_SANDBOX).strip()
    if mode in CODEX_AUTOMATION_SANDBOX_MODES:
        return mode
    return CODEX_AUTOMATION_SANDBOX


def resolve_codex_main_session_sandbox(config: Mapping[str, object] | None = None) -> str:
    selected = None
    if isinstance(config, Mapping):
        selected = config.get("codex_main_session_sandbox")
    return resolve_codex_automation_sandbox(str(selected).strip() if selected is not None else None)


def build_codex_interactive_automation_args(sandbox_mode: str | None = None) -> list[str]:
    """Current Codex TUI replacement for the removed --full-auto shortcut."""
    return [
        "--sandbox",
        resolve_codex_automation_sandbox(sandbox_mode),
        "--ask-for-approval",
        CODEX_AUTOMATION_APPROVAL,
    ]


def build_codex_exec_automation_args(sandbox_mode: str | None = None) -> list[str]:
    """Current Codex exec replacement for the removed --full-auto shortcut."""
    return [
        "--sandbox",
        resolve_codex_automation_sandbox(sandbox_mode),
        "-c",
        f'approval_policy="{CODEX_AUTOMATION_APPROVAL}"',
    ]


def build_codex_exec_command(
    *,
    profile: dict,
    resume_session: str,
    last_message_path: Path,
    provider_args: list[str] | None = None,
    config_args: list[str] | None = None,
    enable_image_generation: bool = False,
    sandbox_mode: str | None = None,
) -> list[str]:
    args = [
        "codex",
        "exec",
        "--json",
        *build_codex_exec_automation_args(sandbox_mode),
        "--skip-git-repo-check",
        "--output-last-message",
        str(last_message_path),
        *(provider_args or []),
        *(config_args or []),
        "--model",
        str(profile["selected_model"]),
    ]
    if enable_image_generation:
        args[3:3] = ["--enable", "image_generation"]
    if resume_session:
        args.extend(["resume", str(resume_session), "-"])
    else:
        args.append("-")
    return args
