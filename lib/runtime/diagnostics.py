from __future__ import annotations

from pathlib import Path
from typing import Any

from vizo_core.agent_runner import AgentRunner
from lib.paths import (
    DATA_DIR_NAME,
    VIZO_HOME,
    data_dir,
    get_legacy_import_manifest_scopes,
    list_pending_legacy_import_scopes,
    resolve_project_root,
)
from lib.settings_handler import (
    DEFAULT_MODELS,
    get_main_session_api_model,
    get_subagent_codex_candidate_roles,
    resolve_main_session_connection,
)

from .cli_discovery import CLI_SOURCE_MISSING, describe_cli
from .contracts import (
    RUNTIME_FAMILY_CLAUDE_CODE,
    RUNTIME_FAMILY_CODEX,
    RUNTIME_SCOPE_MAIN_SESSION,
    RUNTIME_SCOPE_SUBAGENT,
    RuntimeModelDescriptor,
)
from .policy import resolve_main_session_route, resolve_subagent_route


RUNTIME_LABELS = {
    RUNTIME_FAMILY_CLAUDE_CODE: "Claude runtime",
    RUNTIME_FAMILY_CODEX: "Codex runtime",
}

ISSUE_MESSAGES = {
    "claude_cli_missing": "本机未检测到 claude CLI，Claude runtime 当前不可用。",
    "codex_role_evaluated": "该角色按当前主会话连接能力评估子代理 runtime。",
    "codex_candidate_from_external_model": "Codex 候选来自角色独立的外部模型配置。",
    "codex_external_model_not_openai_compatible": "角色独立模型不是 OpenAI-compatible 接入，不能走 Codex direct。",
    "codex_candidate_from_main_session": "Codex 候选复用当前主会话连接。",
    "codex_main_session_not_openai_compatible": "当前主会话不是 OpenAI-compatible 接入，Codex direct 不可用。",
    "codex_direct_requires_upstream_base_url": "当前地址仍指向本地 bridge；Codex direct 需要上游原生 OpenAI-compatible Base URL。",
    "codex_base_url_missing": "缺少可直连的上游 Base URL。",
    "codex_provider_family_unsupported": "当前 provider family 不在 Codex direct 支持范围内。",
    "codex_responses_api_required": "当前上游不支持 Codex 所需的 Responses API。",
    "codex_selected_model_missing": "缺少可用于 Codex direct 的实际模型名。",
    "codex_account_login_unsupported": "当前 OpenAPI 连接不支持账号登录模式，请改用 API Key。",
    "codex_account_login_missing": "当前 OpenAPI 账号连接尚未登录或登录态失效。",
    "codex_api_key_missing": "缺少上游 API Key，Codex direct 不能建立认证。",
    "codex_cli_missing": "本机未检测到 codex CLI，Codex runtime 当前不可用。",
    "codex_direct_profile_ready": "Codex direct 前置条件满足。",
    "codex_direct_selected": "当前策略选择 Codex 作为子代理 runtime。",
    "codex_direct_unavailable_fallback_claude": "Codex direct 条件未满足，子代理继续保持 Claude 主链。",
    "resume_session_locked_to_codex_runtime": "检测到历史 Codex 会话，resume 继续锁定 Codex runtime。",
    "resume_session_codex_profile_missing": "历史 Codex 会话存在，但当前 direct profile 不满足 resume 条件。",
    "resume_session_locked_to_claude_runtime": "检测到历史 Claude 会话，resume 继续锁定 Claude runtime。",
    "resume_session_uses_native_claude_model": "历史 Claude 会话仍使用 Claude native resume 语义。",
    "cross_family_switch_forbidden": "运行中的主会话只允许同 family 切模型；跨 family 切换必须新建会话。",
    "model_not_available": "当前连接未提供所选模型或别名映射。",
    "runtime_missing": "目标 runtime 的本机依赖或认证条件未满足。",
    "provider_is_gateway": "当前接入属于兼容网关，模型映射与平台能力需以 probe 结果为准。",
    "codex_direct_preferred_for_openai_family": "对于 OpenAI-compatible 来源，主会话仅支持 Codex direct。",
    "openai_external_model_bridge_removed": "OpenAPI 外部模型桥接已移除；外部模型/子代理不再支持该链路。",
}

REASON_SUMMARIES = {
    "default_claude_main_session": "当前接入默认走 Claude runtime。",
    "provider_supports_codex_family": "当前连接满足 Codex direct 条件，主会话优先走 Codex runtime。",
    "runtime_override_claude_code": "当前策略显式指定 Claude runtime。",
    "runtime_override_codex": "当前策略显式指定 Codex runtime。",
    "claude_bridge_compat_only": "Claude bridge 已从主会话链路移除。",
    "provider_not_openai_compatible": "当前 provider 不是 OpenAI-compatible 接入，主会话不能走 Codex direct。",
    "codex_direct_unavailable": "OpenAI-compatible 条件存在，但 Codex direct 前置条件尚未满足。",
    "codex_direct": "该角色命中 Codex direct 策略。",
    "codex_direct_fallback_claude": "Codex direct 条件未满足，保持 Claude 子代理。",
    "resume_locked_to_codex": "resume 必须继续锁定已有的 Codex runtime。",
    "resume_locked_to_claude": "resume 必须继续锁定已有的 Claude runtime。",
}

LEGACY_CUTOVER_STATUS_CLEAN = "clean"
LEGACY_CUTOVER_STATUS_MIGRATION_INPUT_PENDING = "migration_input_pending"
LEGACY_CUTOVER_STATUS_LEGACY_DEPENDENCY_DETECTED = "legacy_dependency_detected"
LEGACY_REFERENCE_ROOT = Path("/opt/opus-v6")
LEGACY_ALLOWED_EXCEPTION = {
    "path": "~/.claude/settings.json",
    "reason": "Claude Code CLI 的宿主配置入口，按执行宪章保留。",
}


def _safe_connection_id(connection: dict[str, Any] | None, fallback: str = "") -> str:
    connection = connection or {}
    return str(connection.get("connection_id") or connection.get("id") or fallback or "")


def _issue_entry(code: str, *, severity: str) -> dict[str, str]:
    return {
        "code": code,
        "severity": severity,
        "message": ISSUE_MESSAGES.get(code, code),
    }


def _issue_entries(codes: list[str] | tuple[str, ...], *, severity: str) -> list[dict[str, str]]:
    return [_issue_entry(str(code), severity=severity) for code in (codes or ())]


def _legacy_entry(code: str, *, severity: str, message: str, path: str = "") -> dict[str, str]:
    payload = {
        "code": code,
        "severity": severity,
        "message": message,
    }
    if path:
        payload["path"] = path
    return payload


def _serialize_decision(decision) -> dict[str, Any]:
    payload = decision.to_dict()
    payload["label"] = RUNTIME_LABELS.get(decision.runtime_family, decision.runtime_family)
    if decision.reason_code:
        payload["reason_summary"] = REASON_SUMMARIES.get(decision.reason_code, decision.reason)
    return payload


def _build_descriptor(
    *,
    display_model: str,
    provider_model: str,
    runtime_family: str,
    connection_id: str,
    access_mode: str,
    provider_family: str,
    supports_main_session: bool,
    supports_subagent: bool,
    supports_native_resume: bool,
) -> RuntimeModelDescriptor:
    return RuntimeModelDescriptor(
        display_model=str(display_model or ""),
        provider_model=str(provider_model or display_model or ""),
        runtime_family=str(runtime_family or ""),
        connection_id=str(connection_id or ""),
        access_mode=str(access_mode or ""),
        provider_family=str(provider_family or ""),
        supports_main_session=bool(supports_main_session),
        supports_subagent=bool(supports_subagent),
        supports_native_resume=bool(supports_native_resume),
    )


def build_legacy_cutover_diagnostics_snapshot(
    *,
    config: dict[str, Any] | None,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    project_root_path = resolve_project_root(project_root)
    active_data_root = data_dir(project_root_path)
    runtime_dependencies: list[dict[str, str]] = []
    migration_inputs: list[dict[str, str]] = []
    info_entries: list[dict[str, str]] = []
    project_entries = (config or {}).get("projects", {}) or {}

    if active_data_root.name == DATA_DIR_NAME:
        info_entries.append(
            _legacy_entry(
                "active_state_root_vizo",
                severity="info",
                message=f"当前主状态目录为 {active_data_root}.",
                path=str(active_data_root),
            )
        )
    else:
        runtime_dependencies.append(
            _legacy_entry(
                "active_state_root_not_vizo",
                severity="blocking",
                message=f"当前主状态目录不是 .vizo：{active_data_root}",
                path=str(active_data_root),
            )
        )

    if isinstance(project_entries, dict):
        for name, info in project_entries.items():
            if not isinstance(info, dict):
                continue
            project_path = str(info.get("path", "") or "").strip()
            if not project_path:
                continue
            if project_path.startswith(str(LEGACY_REFERENCE_ROOT)):
                runtime_dependencies.append(
                    _legacy_entry(
                        "legacy_project_path_configured",
                        severity="blocking",
                        message=f"项目 {name} 仍指向旧主线路径：{project_path}",
                        path=project_path,
                    )
                )

    pending_scopes = list_pending_legacy_import_scopes(project_root_path)
    if pending_scopes:
        migration_inputs.append(
            _legacy_entry(
                "legacy_opus_data_pending_import",
                severity="warning",
                message=(
                    "检测到待导入的旧 .opus 历史数据："
                    + ", ".join(pending_scopes[:6])
                    + (" 等" if len(pending_scopes) > 6 else "")
                ),
                path=str(project_root_path / ".opus"),
            )
        )

    imported_scopes = get_legacy_import_manifest_scopes(project_root_path)
    if imported_scopes:
        info_entries.append(
            _legacy_entry(
                "legacy_import_manifest_present",
                severity="info",
                message=f"已记录一次性导入 scopes：{', '.join(imported_scopes[:6])}",
                path=str(project_root_path / DATA_DIR_NAME / ".legacy-import-manifest.json"),
            )
        )

    if LEGACY_REFERENCE_ROOT.exists():
        info_entries.append(
            _legacy_entry(
                "legacy_reference_root_present",
                severity="info",
                message="检测到 /opt/opus-v6 存在；只要未被 active runtime 依赖，它可继续作为历史参考目录。",
                path=str(LEGACY_REFERENCE_ROOT),
            )
        )

    legacy_runtime_home = Path.home() / ".opus-v6"
    if legacy_runtime_home.exists():
        info_entries.append(
            _legacy_entry(
                "legacy_runtime_home_present",
                severity="info",
                message="检测到 ~/.opus-v6 存在；若当前系统已切到 ~/.vizo，则它只应被视为历史残留。",
                path=str(legacy_runtime_home),
            )
        )

    if runtime_dependencies:
        status = LEGACY_CUTOVER_STATUS_LEGACY_DEPENDENCY_DETECTED
        summary = f"检测到 {len(runtime_dependencies)} 项旧项目运行时依赖，尚未达到零旧依赖状态。"
    elif migration_inputs:
        status = LEGACY_CUTOVER_STATUS_MIGRATION_INPUT_PENDING
        summary = "active runtime 已切到 Vizo 主线，但仍检测到待导入的旧 .opus 历史数据。"
    else:
        status = LEGACY_CUTOVER_STATUS_CLEAN
        summary = "active runtime 对旧项目依赖已归零；旧项目只剩历史材料或明确例外项。"

    return {
        "status": status,
        "summary": summary,
        "project_root": str(project_root_path),
        "active_data_root": str(active_data_root),
        "runtime_dependencies": runtime_dependencies,
        "migration_inputs": migration_inputs,
        "info_entries": info_entries,
        "allowed_exception": dict(LEGACY_ALLOWED_EXCEPTION),
    }


def _build_backend_inventory(
    *,
    main_session_payload: dict[str, Any] | None,
    subagent_payload: dict[str, Any] | None,
    api_key_present: bool,
) -> dict[str, dict[str, Any]]:
    claude_cli = describe_cli("claude")
    codex_cli = describe_cli("codex")
    claude_installed = bool(claude_cli["available"])
    codex_installed = bool(codex_cli["available"])

    main_route = (main_session_payload or {}).get("route_decision") or {}
    roles = (subagent_payload or {}).get("roles") or []

    codex_main_supported = any(
        item.get("runtime_kind") == RUNTIME_FAMILY_CODEX and item.get("supported")
        for item in (main_session_payload or {}).get("runtime_candidates", [])
    )
    codex_subagent_supported = any(
        (item.get("route_decision", {}) or {}).get("runtime_family") == RUNTIME_FAMILY_CODEX
        and "codex_direct_profile_ready" in (item.get("diagnostics") or [])
        for item in roles
    )
    codex_authenticated = codex_main_supported or codex_subagent_supported

    claude_auth = bool(api_key_present)
    claude_main_supported = claude_installed and (
        main_route.get("runtime_family") == RUNTIME_FAMILY_CLAUDE_CODE or claude_auth
    )
    claude_subagent_supported = claude_installed and (claude_auth or bool(roles))

    return {
        RUNTIME_FAMILY_CLAUDE_CODE: {
            "installed": claude_installed,
            "authenticated": claude_auth,
            "supports_main_session": bool(claude_main_supported),
            "supports_subagent": bool(claude_subagent_supported),
            "path": str(claude_cli["path"] or ""),
            "resolved_path": str(claude_cli["resolved_path"] or ""),
            "source": str(claude_cli["source"] or CLI_SOURCE_MISSING),
        },
        RUNTIME_FAMILY_CODEX: {
            "installed": codex_installed,
            "authenticated": bool(codex_authenticated),
            "supports_main_session": bool(codex_installed and codex_main_supported),
            "supports_subagent": bool(codex_installed and codex_subagent_supported),
            "path": str(codex_cli["path"] or ""),
            "resolved_path": str(codex_cli["resolved_path"] or ""),
            "source": str(codex_cli["source"] or CLI_SOURCE_MISSING),
        },
    }


def _build_main_session_candidates(
    *,
    resolved_connection: dict[str, Any],
    decision,
) -> list[dict[str, Any]]:
    access_mode = str(resolved_connection.get("access_mode") or "")
    provider_model = str(
        decision.provider_model
        or get_main_session_api_model(
            resolved_connection.get("base_url", ""),
            current_env=resolved_connection.get("env", {}) or {},
            prefer=decision.display_model or decision.requested_model or "sonnet",
            stored_provider_id=resolved_connection.get("provider_id"),
        )
        or ""
    )
    codex_cli = describe_cli("codex")
    claude_cli = describe_cli("claude")

    codex_supported = (
        access_mode == "openai_compatible"
        and bool(codex_cli["available"])
        and "runtime_missing" not in decision.blocking_issues
        and "cross_family_switch_forbidden" not in decision.blocking_issues
        and "model_not_available" not in decision.blocking_issues
        and "codex_direct_profile_ready" in decision.diagnostics
    )
    claude_supported = bool(claude_cli["available"])

    candidates = []
    if access_mode != "openai_compatible":
        candidates.append(
            {
                "runtime_kind": RUNTIME_FAMILY_CLAUDE_CODE,
                "scope": RUNTIME_SCOPE_MAIN_SESSION,
                "supported": bool(claude_supported),
                "category": "primary",
                "reason_code": "default_claude_main_session",
                "label": RUNTIME_LABELS[RUNTIME_FAMILY_CLAUDE_CODE],
                "summary": REASON_SUMMARIES["default_claude_main_session"],
                "cli_source": str(claude_cli["source"] or CLI_SOURCE_MISSING),
                "display_model": decision.display_model if decision.runtime_family == RUNTIME_FAMILY_CLAUDE_CODE else "sonnet",
                "provider_model": (
                    provider_model
                    if decision.runtime_family == RUNTIME_FAMILY_CLAUDE_CODE
                    else str(
                        get_main_session_api_model(
                            resolved_connection.get("base_url", ""),
                            current_env=resolved_connection.get("env", {}) or {},
                            prefer="sonnet",
                            stored_provider_id=resolved_connection.get("provider_id"),
                        )
                        or provider_model
                    )
                ),
                "blocking_issues": _issue_entries(
                    ("runtime_missing",) if not claude_supported else (),
                    severity="blocking",
                ),
            }
        )

    codex_reason = "provider_supports_codex_family"
    if access_mode != "openai_compatible":
        codex_reason = "provider_not_openai_compatible"
    elif not codex_supported:
        codex_reason = "codex_direct_unavailable"

    candidates.insert(
        0,
        {
            "runtime_kind": RUNTIME_FAMILY_CODEX,
            "scope": RUNTIME_SCOPE_MAIN_SESSION,
            "supported": bool(codex_supported),
            "category": "primary",
            "reason_code": codex_reason,
            "label": RUNTIME_LABELS[RUNTIME_FAMILY_CODEX],
            "summary": REASON_SUMMARIES[codex_reason],
            "cli_source": str(codex_cli["source"] or CLI_SOURCE_MISSING),
            "display_model": decision.display_model if decision.runtime_family == RUNTIME_FAMILY_CODEX else decision.requested_model,
            "provider_model": provider_model,
            "blocking_issues": _issue_entries(
                decision.blocking_issues if access_mode == "openai_compatible" else ("runtime_missing",),
                severity="blocking",
            ),
        },
    )
    return candidates


def build_main_session_runtime_diagnostics(
    *,
    config: dict[str, Any] | None,
    connection: dict[str, Any],
    display_model: str | None = None,
    runtime_override: str | None = None,
    current_runtime_family: str | None = None,
) -> dict[str, Any]:
    config = config or {}
    resolved_connection = resolve_main_session_connection(
        connection.get("base_url", ""),
        values=connection,
        current_env=connection.get("env", {}) or {},
        stored_provider_id=connection.get("provider_id"),
    )
    decision = resolve_main_session_route(
        connection={
            "connection_id": _safe_connection_id(connection, fallback="main_session"),
            "base_url": resolved_connection.get("base_url", ""),
            "provider_id": resolved_connection.get("provider_id"),
            "env": resolved_connection.get("env", {}) or {},
            "api_key": connection.get("api_key", ""),
            "auth_mode": resolved_connection.get("auth_mode", ""),
            "auth_status": resolved_connection.get("auth_status", ""),
            "codex_home": resolved_connection.get("codex_home", ""),
        },
        config=config,
        display_model=display_model,
        runtime_override=runtime_override,
        current_runtime_family=current_runtime_family,
    )
    descriptor = _build_descriptor(
        display_model=decision.display_model or decision.selected_model,
        provider_model=decision.provider_model or decision.selected_model,
        runtime_family=decision.runtime_family,
        connection_id=decision.connection_id or _safe_connection_id(connection, fallback="main_session"),
        access_mode=resolved_connection.get("access_mode", ""),
        provider_family=resolved_connection.get("provider_family", ""),
        supports_main_session=not decision.blocking_issues,
        supports_subagent=True,
        supports_native_resume=decision.supports_native_resume,
    )

    api_key_present = bool(str(connection.get("api_key") or "").strip())
    candidates = _build_main_session_candidates(
        resolved_connection=resolved_connection,
        decision=decision,
    )

    running_summary = (
        f"当前运行中的主会话是 {RUNTIME_LABELS.get(current_runtime_family, current_runtime_family)}；"
        "运行中只允许同 family 切模型，跨 family 切换必须新建会话。"
        if current_runtime_family
        else "新建主会话会按当前 diagnostics 决策选择 runtime。"
    )

    return {
        "resolved_connection": {
            "connection_id": _safe_connection_id(connection, fallback="main_session"),
            "provider_id": resolved_connection.get("provider_id", ""),
            "provider_display": resolved_connection.get("provider_display", ""),
            "provider_family": resolved_connection.get("provider_family", ""),
            "access_mode": resolved_connection.get("access_mode", ""),
            "base_url": resolved_connection.get("base_url", ""),
            "runtime_base_url": resolved_connection.get("runtime_base_url", ""),
            "auth_mode": resolved_connection.get("auth_mode", ""),
            "auth_status": resolved_connection.get("auth_status", ""),
            "auth_last_verified_at": resolved_connection.get("auth_last_verified_at", ""),
            "auth_account_label": resolved_connection.get("auth_account_label", ""),
            "codex_home": resolved_connection.get("codex_home", ""),
            "account_login_supported": bool(resolved_connection.get("account_login_supported")),
            "routing_summary": resolved_connection.get("routing_summary", ""),
            "routing_models": dict(resolved_connection.get("routing_models", {}) or {}),
        },
        "route_decision": _serialize_decision(decision),
        "descriptor": descriptor.to_dict(),
        "runtime_candidates": candidates,
        "blocking_issues": _issue_entries(decision.blocking_issues, severity="blocking"),
        "warnings": _issue_entries(decision.warnings, severity="warning"),
        "diagnostics": _issue_entries(decision.diagnostics, severity="info"),
        "summary": decision.reason,
        "switch_policy": {
            "same_family_model_switch_only": True,
            "cross_family_switch_allowed": False,
            "connection_switch_requires_re_evaluation": True,
            "summary": running_summary,
        },
    }


def build_subagent_runtime_diagnostics(
    *,
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    config = config or {}
    roles = []
    codex_roles = []
    claude_roles = []
    candidate_roles = set(get_subagent_codex_candidate_roles())
    for role in AgentRunner.DEFAULT_MODELS:
        requested_model = str(
            (config.get("model_overrides", {}) or {}).get(role)
            or DEFAULT_MODELS.get(role, "sonnet")
        ).strip()
        decision = resolve_subagent_route(
            config=config,
            role=role,
            model_override=requested_model,
        )
        item = {
            "role": role,
            "role_label": role,
            "requested_model": requested_model,
            "route_decision": _serialize_decision(decision),
            "descriptor": _build_descriptor(
                display_model=decision.selected_model or decision.requested_model,
                provider_model=decision.provider_model or decision.selected_model or decision.requested_model,
                runtime_family=decision.runtime_family,
                connection_id=decision.connection_id or "main_session",
                access_mode="auto",
                provider_family="auto",
                supports_main_session=False,
                supports_subagent=True,
                supports_native_resume=decision.supports_native_resume,
            ).to_dict(),
            "reason": decision.reason,
            "reason_code": decision.reason_code,
            "diagnostics": list(decision.diagnostics),
            "diagnostic_entries": _issue_entries(decision.diagnostics, severity="info"),
            "blocking_issues": _issue_entries(decision.blocking_issues, severity="blocking"),
            "warnings": _issue_entries(decision.warnings, severity="warning"),
            "codex_candidate": role in candidate_roles,
        }
        roles.append(item)
        if decision.runtime_family == RUNTIME_FAMILY_CODEX:
            codex_roles.append(role)
        else:
            claude_roles.append(role)

    summary = (
        f"Codex 子代理角色：{', '.join(codex_roles)}。"
        if codex_roles
        else "当前没有角色满足 Codex direct 子代理条件，全部维持 Claude 子代理。"
    )
    if claude_roles:
        summary += f" 其余角色保持 Claude 子代理：{', '.join(claude_roles[:8])}"
        if len(claude_roles) > 8:
            summary += f" 等 {len(claude_roles)} 个角色。"

    return {
        "roles": roles,
        "codex_candidate_roles": sorted(candidate_roles),
        "codex_roles": codex_roles,
        "claude_roles": claude_roles,
        "summary": summary,
    }


def build_runtime_diagnostics_snapshot(
    *,
    config: dict[str, Any] | None,
    connection: dict[str, Any],
    display_model: str | None = None,
    runtime_override: str | None = None,
    current_runtime_family: str | None = None,
    probe_scope: str = "all",
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    scope = str(probe_scope or "all")
    include_main = scope in {"all", RUNTIME_SCOPE_MAIN_SESSION}
    include_subagent = scope in {"all", RUNTIME_SCOPE_SUBAGENT}
    api_key_present = bool(str(connection.get("api_key") or "").strip())

    main_payload = None
    if include_main:
        main_payload = build_main_session_runtime_diagnostics(
            config=config,
            connection=connection,
            display_model=display_model,
            runtime_override=runtime_override,
            current_runtime_family=current_runtime_family,
        )

    subagent_payload = None
    if include_subagent:
        subagent_payload = build_subagent_runtime_diagnostics(config=config)

    backends = _build_backend_inventory(
        main_session_payload=main_payload,
        subagent_payload=subagent_payload,
        api_key_present=api_key_present,
    )

    payload = {
        "backends": backends,
        "policy": {
            "main_session": "auto",
            "subagent": "auto",
        },
        "legacy_cutover": build_legacy_cutover_diagnostics_snapshot(
            config=config,
            project_root=project_root or VIZO_HOME,
        ),
    }
    if include_main and main_payload:
        payload["resolved_connection"] = main_payload["resolved_connection"]
        payload["main_session"] = main_payload
        payload["runtime_candidates"] = main_payload["runtime_candidates"]
        payload["blocking_issues"] = main_payload["blocking_issues"]
        payload["warnings"] = main_payload["warnings"]
    if include_subagent and subagent_payload:
        payload["subagent"] = subagent_payload
    return payload
