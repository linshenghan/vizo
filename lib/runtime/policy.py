from __future__ import annotations

from lib.openai_compat_bridge import (
    infer_openai_provider_capabilities,
    is_openai_bridge_base_url,
    normalize_openai_compatible_base_url,
)
from lib.settings_handler import (
    CODEX_SUPPORTED_PROVIDER_FAMILIES,
    MAIN_SESSION_DEFAULT_TIER,
    OPENAPI_AUTH_MODE_ACCOUNT_LOGIN,
    OPENAPI_AUTH_STATUS_READY,
    VALID_MODEL_IDS,
    get_main_session_api_model,
    resolve_main_session_connection,
    resolve_subagent_codex_candidate,
)

from .cli_discovery import cli_available
from .contracts import (
    RUNTIME_FAMILY_CLAUDE_CODE,
    RUNTIME_FAMILY_CODEX,
    RUNTIME_SCOPE_MAIN_SESSION,
    RUNTIME_SCOPE_SUBAGENT,
    RouteDecision,
)


def resolve_subagent_route(
    *,
    config: dict,
    role: str,
    model_override: str | None = None,
    resume_session: str | None = None,
    resume_runtime_family: str | None = None,
) -> RouteDecision:
    candidate = resolve_subagent_codex_candidate(
        config,
        role=role,
        model_override=model_override,
    )
    requested_model = str(candidate.get("requested_model") or "sonnet")
    codex_selected_model = str(candidate.get("selected_model") or requested_model)
    connection_id = str(candidate.get("source") or "main_session")
    codex_ready = candidate.get("profile") is not None
    diagnostics = list(candidate.get("diagnostics") or ())
    blocking_issues = tuple(candidate.get("blocking_issues") or ())

    if blocking_issues:
        return RouteDecision(
            scope=RUNTIME_SCOPE_SUBAGENT,
            runtime_family=RUNTIME_FAMILY_CLAUDE_CODE,
            adapter_key=f"{RUNTIME_SCOPE_SUBAGENT}:{RUNTIME_FAMILY_CLAUDE_CODE}",
            requested_model=requested_model,
            selected_model=requested_model,
            can_resume=False,
            supports_pause=True,
            supports_pause_feedback=True,
            supports_native_resume=True,
            reason="OpenAPI 外部模型桥接已移除，当前子代理配置被阻断",
            display_model=requested_model,
            provider_model=requested_model,
            connection_id=connection_id,
            reason_code="external_openai_bridge_removed",
            diagnostics=tuple(diagnostics),
            blocking_issues=blocking_issues,
        )

    if resume_session:
        if resume_runtime_family == RUNTIME_FAMILY_CODEX:
            diagnostics.append("resume_session_locked_to_codex_runtime")
            if not codex_ready:
                diagnostics.append("resume_session_codex_profile_missing")
            return RouteDecision(
                scope=RUNTIME_SCOPE_SUBAGENT,
                runtime_family=RUNTIME_FAMILY_CODEX,
                adapter_key=f"{RUNTIME_SCOPE_SUBAGENT}:{RUNTIME_FAMILY_CODEX}",
                requested_model=requested_model,
                selected_model=codex_selected_model,
                can_resume=codex_ready,
                supports_pause=True,
                supports_pause_feedback=False,
                supports_native_resume=True,
                reason=(
                    "检测到历史 Codex 会话，resume 继续锁定 Codex runtime"
                    if codex_ready
                    else "检测到历史 Codex 会话，但当前 direct profile 不满足 resume 条件"
                ),
                display_model=codex_selected_model,
                provider_model=codex_selected_model,
                connection_id=connection_id,
                reason_code="resume_locked_to_codex",
                diagnostics=tuple(diagnostics),
            )

        diagnostics.append("resume_session_locked_to_claude_runtime")
        diagnostics.append("resume_session_uses_native_claude_model")
        return RouteDecision(
            scope=RUNTIME_SCOPE_SUBAGENT,
            runtime_family=RUNTIME_FAMILY_CLAUDE_CODE,
            adapter_key=f"{RUNTIME_SCOPE_SUBAGENT}:{RUNTIME_FAMILY_CLAUDE_CODE}",
            requested_model=requested_model,
            selected_model=requested_model,
            can_resume=True,
            supports_pause=True,
            supports_pause_feedback=True,
            supports_native_resume=True,
            reason="检测到已有 Claude native session，本轮继续锁定 Claude runtime",
            display_model=requested_model,
            provider_model=requested_model,
            connection_id="main_session",
            reason_code="resume_locked_to_claude",
            diagnostics=tuple(diagnostics),
        )

    if codex_ready:
        diagnostics.append("codex_direct_selected")
        return RouteDecision(
            scope=RUNTIME_SCOPE_SUBAGENT,
            runtime_family=RUNTIME_FAMILY_CODEX,
            adapter_key=f"{RUNTIME_SCOPE_SUBAGENT}:{RUNTIME_FAMILY_CODEX}",
            requested_model=requested_model,
            selected_model=codex_selected_model,
            can_resume=False,
            supports_pause=True,
            supports_pause_feedback=False,
            supports_native_resume=True,
            reason="当前角色命中 OpenAI/Codex 主会话直连路径",
            display_model=codex_selected_model,
            provider_model=codex_selected_model,
            connection_id=connection_id,
            reason_code="codex_direct",
            diagnostics=tuple(diagnostics),
        )

    diagnostics.append("codex_direct_unavailable_fallback_claude")
    return RouteDecision(
        scope=RUNTIME_SCOPE_SUBAGENT,
        runtime_family=RUNTIME_FAMILY_CLAUDE_CODE,
        adapter_key=f"{RUNTIME_SCOPE_SUBAGENT}:{RUNTIME_FAMILY_CLAUDE_CODE}",
        requested_model=requested_model,
        selected_model=requested_model,
        can_resume=False,
        supports_pause=True,
        supports_pause_feedback=True,
        supports_native_resume=True,
        reason="Codex direct 条件未满足，本轮保持 Claude 子代理主链",
        display_model=requested_model,
        provider_model=requested_model,
        connection_id=connection_id,
        reason_code="codex_direct_fallback_claude",
        diagnostics=tuple(diagnostics),
    )


def resolve_main_session_route(
    *,
    connection: dict,
    config: dict | None = None,
    display_model: str | None = None,
    runtime_override: str | None = None,
    current_runtime_family: str | None = None,
) -> RouteDecision:
    """Resolve runtime choice for a Vizo-owned main session."""

    config = config or {}
    connection_id = str(connection.get("connection_id") or "")
    diagnostics: list[str] = []
    warnings: list[str] = []
    blocking_issues: list[str] = []

    resolved = resolve_main_session_connection(
        connection.get("base_url", ""),
        values=connection,
        current_env=connection.get("env", {}) or {},
        stored_provider_id=connection.get("provider_id"),
    )
    access_mode = str(resolved.get("access_mode") or "")
    provider_family = str(resolved.get("provider_family") or "")
    provider_display = str(resolved.get("provider_display") or "")
    auth_mode = str(resolved.get("auth_mode") or "")
    auth_status = str(resolved.get("auth_status") or "")

    codex_selected_model = ""
    if display_model and display_model not in VALID_MODEL_IDS:
        codex_selected_model = str(display_model).strip()
    else:
        codex_selected_model = get_main_session_api_model(
            connection.get("base_url", ""),
            current_env=connection.get("env", {}) or {},
            prefer=str(display_model or MAIN_SESSION_DEFAULT_TIER),
            stored_provider_id=connection.get("provider_id"),
        )

    provider_caps = resolved.get("provider_capabilities") or {}
    if access_mode == "openai_compatible":
        base_url = normalize_openai_compatible_base_url(resolved.get("base_url", ""))
        provider_caps = provider_caps or infer_openai_provider_capabilities(
            base_url,
            provider_family=provider_family,
        )
        provider_family = str(provider_caps.get("provider_family") or provider_family or "")
        if is_openai_bridge_base_url(base_url):
            diagnostics.append("codex_direct_requires_upstream_base_url")
            base_url = ""
        if not base_url:
            diagnostics.append("codex_base_url_missing")
        if provider_family not in CODEX_SUPPORTED_PROVIDER_FAMILIES:
            diagnostics.append("codex_provider_family_unsupported")
        if not provider_caps.get("supports_responses_api"):
            diagnostics.append("codex_responses_api_required")
        if not codex_selected_model:
            diagnostics.append("codex_selected_model_missing")
        if auth_mode == OPENAPI_AUTH_MODE_ACCOUNT_LOGIN:
            if not bool(resolved.get("account_login_supported")):
                diagnostics.append("codex_account_login_unsupported")
            if auth_status != OPENAPI_AUTH_STATUS_READY:
                diagnostics.append("codex_account_login_missing")
        elif not str(connection.get("api_key") or "").strip():
            diagnostics.append("codex_api_key_missing")
        if not cli_available("codex"):
            diagnostics.append("codex_cli_missing")
        codex_ready = not any(
            item in diagnostics
            for item in (
                "codex_direct_requires_upstream_base_url",
                "codex_base_url_missing",
                "codex_provider_family_unsupported",
                "codex_responses_api_required",
                "codex_selected_model_missing",
                "codex_account_login_unsupported",
                "codex_account_login_missing",
                "codex_api_key_missing",
                "codex_cli_missing",
            )
        )
        if codex_ready:
            diagnostics.append("codex_direct_profile_ready")
        warnings.append("codex_direct_preferred_for_openai_family")
    else:
        codex_ready = False

    requested_override = str(runtime_override or "").strip()
    target_runtime_family = RUNTIME_FAMILY_CLAUDE_CODE
    reason = "主会话默认走 Claude runtime"
    reason_code = "default_claude_main_session"
    display_model_value = MAIN_SESSION_DEFAULT_TIER
    provider_model = get_main_session_api_model(
        connection.get("base_url", ""),
        current_env=connection.get("env", {}) or {},
        prefer=display_model or MAIN_SESSION_DEFAULT_TIER,
        stored_provider_id=connection.get("provider_id"),
    )

    if current_runtime_family == RUNTIME_FAMILY_CODEX and display_model in VALID_MODEL_IDS:
        blocking_issues.append("cross_family_switch_forbidden")
    elif display_model and display_model not in VALID_MODEL_IDS and access_mode != "openai_compatible":
        blocking_issues.append("model_not_available")
    elif display_model and display_model in VALID_MODEL_IDS:
        display_model_value = display_model

    if codex_ready:
        target_runtime_family = RUNTIME_FAMILY_CODEX
        reason = "当前连接满足 Codex direct 前置条件，主会话命中 Codex runtime"
        reason_code = "provider_supports_codex_family"
        display_model_value = codex_selected_model
        provider_model = codex_selected_model
    elif access_mode == "openai_compatible":
        target_runtime_family = RUNTIME_FAMILY_CODEX
        reason = "OpenAPI 主会话仅支持 Codex runtime，当前 direct 条件未满足"
        reason_code = "codex_direct_unavailable"
        display_model_value = codex_selected_model or provider_model or display_model_value
        provider_model = codex_selected_model or provider_model
        if "runtime_missing" not in blocking_issues:
            blocking_issues.append("runtime_missing")

    if requested_override:
        if requested_override == RUNTIME_FAMILY_CLAUDE_CODE and access_mode == "openai_compatible":
            if "runtime_missing" not in blocking_issues:
                blocking_issues.append("runtime_missing")
        elif requested_override == RUNTIME_FAMILY_CODEX and not codex_ready:
            blocking_issues.append("runtime_missing")
        elif requested_override == RUNTIME_FAMILY_CLAUDE_CODE:
            target_runtime_family = RUNTIME_FAMILY_CLAUDE_CODE
            reason = "显式指定 Claude runtime"
            reason_code = "runtime_override_claude_code"
            display_model_value = display_model if display_model in VALID_MODEL_IDS else MAIN_SESSION_DEFAULT_TIER
            provider_model = get_main_session_api_model(
                connection.get("base_url", ""),
                current_env=connection.get("env", {}) or {},
                prefer=display_model_value,
                stored_provider_id=connection.get("provider_id"),
            )
        elif requested_override == RUNTIME_FAMILY_CODEX and codex_ready:
            target_runtime_family = RUNTIME_FAMILY_CODEX
            reason = "显式指定 Codex runtime"
            reason_code = "runtime_override_codex"
            display_model_value = codex_selected_model
            provider_model = codex_selected_model

    if current_runtime_family and target_runtime_family != current_runtime_family:
        blocking_issues.append("cross_family_switch_forbidden")
    elif current_runtime_family == RUNTIME_FAMILY_CLAUDE_CODE and display_model and display_model not in VALID_MODEL_IDS:
        if codex_ready:
            blocking_issues.append("cross_family_switch_forbidden")
        else:
            blocking_issues.append("model_not_available")

    if target_runtime_family == RUNTIME_FAMILY_CLAUDE_CODE and not cli_available("claude"):
        blocking_issues.append("runtime_missing")
        diagnostics.append("claude_cli_missing")
    if access_mode == "anthropic_gateway":
        warnings.append("provider_is_gateway")

    return RouteDecision(
        scope=RUNTIME_SCOPE_MAIN_SESSION,
        runtime_family=target_runtime_family,
        adapter_key=f"{RUNTIME_SCOPE_MAIN_SESSION}:{target_runtime_family}",
        requested_model=str(display_model or display_model_value or MAIN_SESSION_DEFAULT_TIER),
        selected_model=str(display_model_value or provider_model or MAIN_SESSION_DEFAULT_TIER),
        can_resume=bool(current_runtime_family and not blocking_issues),
        supports_pause=True,
        supports_pause_feedback=(target_runtime_family == RUNTIME_FAMILY_CLAUDE_CODE),
        supports_native_resume=True,
        reason=reason,
        diagnostics=tuple(diagnostics),
        display_model=str(display_model_value or provider_model or ""),
        provider_model=str(provider_model or ""),
        connection_id=connection_id,
        reason_code=reason_code,
        requires_new_session="cross_family_switch_forbidden" in blocking_issues,
        blocking_issues=tuple(blocking_issues),
        warnings=tuple(warnings),
    )
