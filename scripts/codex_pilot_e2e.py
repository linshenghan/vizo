#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vizo_core.agent_runner import AgentRunner
from lib.config_loader import load_config
from lib.runtime.policy import resolve_subagent_route
from lib.runtime.subagents.controller import SubAgentRuntimeController


FIXTURE_REQUIREMENT_ANALYSIS = """# 需求分析报告

## 基本信息

- 任务名称：`R17 Codex 子代理端到端补测`
- `ra_task_type`：`new_feature`
- `scale_hint`：`normal`

## 背景

当前有一个最小 profile demo：

- `backend/profile_service.py` 只返回 `userName`
- `frontend/profileCard.js` 只渲染 `userName`

## 本期目标

在不新增依赖、不改函数名的前提下，完成一个最小的后端 + 前端联动改动：

1. 后端 `get_profile_payload()` 需要新增字段 `roleLabel`
2. `roleLabel` 固定返回字符串 `Core Builder`
3. 前端 `renderProfileCard(profile)` 需要把 `profile.roleLabel` 渲染到卡片中

## 约束

- 只允许修改以下代码文件：
  - `backend/profile_service.py`
  - `frontend/profileCard.js`
- 不新增第三方依赖
- 不改公开函数名
- 架构设计文档必须明确列出上面两个文件

## 验收标准

- `backend/profile_service.py` 中可以看到 `roleLabel`
- `frontend/profileCard.js` 中可以看到 `profile.roleLabel`
- 架构设计文档、后端总结、前端总结都成功写入指定输出文件
"""


BACKEND_SOURCE = """from __future__ import annotations


def get_profile_payload() -> dict[str, str]:
    return {
        "userName": "Vizo User",
    }
"""


FRONTEND_SOURCE = """export function renderProfileCard(profile) {
  return `<section class="profile-card">
    <h1>${profile.userName}</h1>
  </section>`;
}
"""


SERENA_MEMORY_FILES = {
    "project_overview.md": """# Project Overview

- Purpose: tiny demo workspace for Codex subagent verification.
- Stack: Python helper + plain JavaScript renderer.
- Scope: only `backend/profile_service.py` and `frontend/profileCard.js`.
""",
    "suggested_commands.md": """# Suggested Commands

- `rg --files`
- `nl -ba backend/profile_service.py`
- `nl -ba frontend/profileCard.js`
""",
    "style_and_conventions.md": """# Style And Conventions

- Prefer minimal changes.
- Keep existing function names unchanged.
- Do not add dependencies.
""",
}


R17_BASE_WORK_STATE_SUMMARY = """
当前任务是 R17 Codex pilot 的端到端验证，只需要完成当前角色的最小交付。

- 不要执行 Serena onboarding
- 不要调用 `write_memory`
- 不要为这个临时 fixture 创建长期项目记忆
- 如果 Serena 提示 onboarding 未完成，直接忽略并继续完成当前角色任务
- 输出尽量简洁，重点是完成指定文件与 JSON 摘要
""".strip()


@dataclass
class FixtureContext:
    run_root: Path
    workspace_root: Path
    task_dir: Path
    requirement_file: Path
    design_output: Path
    backend_output: Path
    frontend_output: Path
    backend_file: Path
    frontend_file: Path
    summary_file: Path


@dataclass
class RoleSpec:
    role: str
    output_file: Path
    input_docs: dict[str, str]
    output_markers: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    workspace_markers: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class RoleEvidence:
    role: str
    status: str
    passed: bool
    workspace_root: str
    output_file: str
    output_exists: bool
    changed_files: list[str]
    output_marker_results: dict[str, bool]
    changed_file_results: dict[str, bool]
    workspace_marker_results: dict[str, dict[str, bool]]
    route_decision: dict[str, Any]
    runtime_metadata: dict[str, Any]
    usage: dict[str, Any]
    session_id: str
    selected_model: str
    output_excerpt: str
    error: str = ""


def create_fixture(run_root: Path) -> FixtureContext:
    workspace_root = run_root / "workspace"
    task_dir = run_root / "task-artifacts"
    backend_dir = workspace_root / "backend"
    frontend_dir = workspace_root / "frontend"
    backend_dir.mkdir(parents=True, exist_ok=True)
    frontend_dir.mkdir(parents=True, exist_ok=True)
    task_dir.mkdir(parents=True, exist_ok=True)
    serena_memories_dir = workspace_root / ".serena" / "memories"
    serena_memories_dir.mkdir(parents=True, exist_ok=True)

    requirement_file = task_dir / "00-requirement-analysis.md"
    design_output = task_dir / "02-design.md"
    backend_output = task_dir / "03-backend-summary.md"
    frontend_output = task_dir / "04-frontend-summary.md"
    backend_file = backend_dir / "profile_service.py"
    frontend_file = frontend_dir / "profileCard.js"
    summary_file = run_root / "summary.json"

    requirement_file.write_text(FIXTURE_REQUIREMENT_ANALYSIS, encoding="utf-8")
    (workspace_root / "README.md").write_text(
        "# R17 Codex Pilot Fixture\n\nA tiny demo workspace for Codex subagent verification.\n",
        encoding="utf-8",
    )
    backend_file.write_text(BACKEND_SOURCE, encoding="utf-8")
    frontend_file.write_text(FRONTEND_SOURCE, encoding="utf-8")
    for name, content in SERENA_MEMORY_FILES.items():
        (serena_memories_dir / name).write_text(content, encoding="utf-8")

    return FixtureContext(
        run_root=run_root,
        workspace_root=workspace_root,
        task_dir=task_dir,
        requirement_file=requirement_file,
        design_output=design_output,
        backend_output=backend_output,
        frontend_output=frontend_output,
        backend_file=backend_file,
        frontend_file=frontend_file,
        summary_file=summary_file,
    )


def build_role_specs(fixture: FixtureContext) -> list[RoleSpec]:
    requirement_path = str(fixture.requirement_file)
    design_path = str(fixture.design_output)
    return [
        RoleSpec(
            role="architect",
            output_file=fixture.design_output,
            input_docs={"requirement_analysis": requirement_path},
            output_markers=[
                "backend/profile_service.py",
                "frontend/profileCard.js",
            ],
        ),
        RoleSpec(
            role="backend_developer",
            output_file=fixture.backend_output,
            input_docs={
                "design": design_path,
                "requirement_analysis": requirement_path,
            },
            output_markers=["backend/profile_service.py"],
            changed_files=["backend/profile_service.py"],
            workspace_markers={
                "backend/profile_service.py": ["roleLabel", "Core Builder"],
            },
        ),
        RoleSpec(
            role="frontend_developer",
            output_file=fixture.frontend_output,
            input_docs={
                "design": design_path,
                "requirement_analysis": requirement_path,
            },
            output_markers=["frontend/profileCard.js"],
            changed_files=["frontend/profileCard.js"],
            workspace_markers={
                "frontend/profileCard.js": ["profile.roleLabel"],
            },
        ),
    ]


def build_runtime_config(base_config: dict, workspace_root: Path) -> dict:
    config = copy.deepcopy(base_config)
    config["default_project"] = "__r17_codex_fixture__"
    projects = dict(config.get("projects") or {})
    projects["__r17_codex_fixture__"] = {
        "path": str(workspace_root),
        "serena_project": "vizo",
    }
    config["projects"] = projects
    return config


def snapshot_workspace(workspace_root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(p for p in workspace_root.rglob("*") if p.is_file()):
        rel = path.relative_to(workspace_root).as_posix()
        snapshot[rel] = hashlib.sha1(path.read_bytes()).hexdigest()
    return snapshot


def diff_workspace(before: dict[str, str], after: dict[str, str]) -> list[str]:
    changed = {
        rel_path
        for rel_path in set(before) | set(after)
        if before.get(rel_path) != after.get(rel_path)
    }
    return sorted(changed)


def read_text_if_exists(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not path.exists():
        return items
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return items


def load_route_decision(event_log_path: str) -> dict[str, Any]:
    if not event_log_path:
        return {}
    for item in read_jsonl(Path(event_log_path)):
        if item.get("event_name") == "route_decision":
            payload = item.get("payload", {})
            if isinstance(payload, dict):
                decision = payload.get("decision", {})
                if isinstance(decision, dict):
                    return decision
    return {}


def marker_results(text: str, markers: list[str]) -> dict[str, bool]:
    return {marker: marker in text for marker in markers}


def collect_workspace_marker_results(
    workspace_root: Path,
    expected: dict[str, list[str]],
) -> dict[str, dict[str, bool]]:
    results: dict[str, dict[str, bool]] = {}
    for rel_path, markers in expected.items():
        target = workspace_root / rel_path
        results[rel_path] = marker_results(read_text_if_exists(target), markers)
    return results


def role_work_state_summary(role: str) -> str:
    role_constraints = {
        "architect": (
            "当前角色是 architect，只允许读取代码和写设计文档，不要修改 workspace 中的业务代码文件。"
        ),
        "backend_developer": (
            "当前角色是 backend_developer，只允许修改 `backend/profile_service.py` 和输出总结文件；"
            "不要修改 `frontend/profileCard.js`。"
        ),
        "frontend_developer": (
            "当前角色是 frontend_developer，只允许修改 `frontend/profileCard.js` 和输出总结文件；"
            "不要修改 `backend/profile_service.py`。"
        ),
    }
    extra = role_constraints.get(role, "")
    return f"{R17_BASE_WORK_STATE_SUMMARY}\n\n{extra}".strip()


def prepare_role_workspace(fixture: FixtureContext, role: str) -> Path:
    if role == "architect":
        return fixture.workspace_root

    target = fixture.run_root / f"workspace-{role}"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(fixture.workspace_root, target)
    return target


def build_role_evidence(
    *,
    spec: RoleSpec,
    fixture: FixtureContext,
    workspace_root: Path,
    run_result: Any | None,
    runtime_metadata: dict[str, Any],
    changed_files: list[str],
    error: str,
) -> RoleEvidence:
    output_text = read_text_if_exists(spec.output_file)
    output_exists = spec.output_file.exists()
    output_marker_hits = marker_results(output_text, spec.output_markers)
    changed_file_hits = {
        rel_path: rel_path in changed_files
        for rel_path in spec.changed_files
    }
    workspace_marker_hits = collect_workspace_marker_results(
        workspace_root,
        spec.workspace_markers,
    )
    route_decision = load_route_decision(str(runtime_metadata.get("event_log_path") or ""))
    result_data = getattr(run_result, "data", {}) if run_result is not None else {}
    usage = result_data.get("usage", {}) if isinstance(result_data, dict) else {}
    session_id = ""
    if isinstance(result_data, dict):
        session_id = str(result_data.get("session_id") or "")
    session_id = session_id or str(runtime_metadata.get("native_session_id") or "")
    selected_model = str(getattr(run_result, "model", "") or runtime_metadata.get("selected_model") or "")

    checks = [
        not error,
        output_exists,
        all(output_marker_hits.values()),
        all(changed_file_hits.values()),
        all(
            all(marker_result.values())
            for marker_result in workspace_marker_hits.values()
        ),
    ]

    return RoleEvidence(
        role=spec.role,
        status="passed" if all(checks) else "failed",
        passed=all(checks),
        workspace_root=str(workspace_root),
        output_file=str(spec.output_file),
        output_exists=output_exists,
        changed_files=changed_files,
        output_marker_results=output_marker_hits,
        changed_file_results=changed_file_hits,
        workspace_marker_results=workspace_marker_hits,
        route_decision=route_decision,
        runtime_metadata=runtime_metadata,
        usage=usage if isinstance(usage, dict) else {},
        session_id=session_id,
        selected_model=selected_model,
        output_excerpt=output_text[:400],
        error=error,
    )


async def execute_role(
    controller: Any,
    fixture: FixtureContext,
    spec: RoleSpec,
) -> RoleEvidence:
    workspace_root = prepare_role_workspace(fixture, spec.role)
    before = snapshot_workspace(workspace_root)
    run_result = None
    runtime_metadata: dict[str, Any] = {}
    error = ""
    try:
        run_result = await controller.run(
            task=SimpleNamespace(id="r17-codex-pilot-e2e", current_step=spec.role),
            step_name=spec.role,
            role=spec.role,
            task_dir=fixture.task_dir,
            input_docs=spec.input_docs,
            memories=[],
            output_file=spec.output_file,
            cwd=str(workspace_root),
            work_state_summary=role_work_state_summary(spec.role),
        )
        if isinstance(getattr(run_result, "data", None), dict):
            runtime_metadata = dict(run_result.data.get("runtime_metadata") or {})
        if not runtime_metadata:
            runtime_metadata = dict(getattr(run_result, "runtime_metadata", {}) or {})
    except Exception as exc:
        error = str(exc)
        runtime_metadata = dict(getattr(exc, "runtime_metadata", {}) or {})

    after = snapshot_workspace(workspace_root)
    changed_files = diff_workspace(before, after)
    return build_role_evidence(
        spec=spec,
        fixture=fixture,
        workspace_root=workspace_root,
        run_result=run_result,
        runtime_metadata=runtime_metadata,
        changed_files=changed_files,
        error=error,
    )


async def run_codex_pilot_e2e(
    *,
    run_root: Path,
    controller: Any | None = None,
    base_config: dict | None = None,
) -> dict[str, Any]:
    run_root.mkdir(parents=True, exist_ok=True)
    fixture = create_fixture(run_root)
    config = build_runtime_config(base_config or load_config(force_reload=True), fixture.workspace_root)
    preflight = {
        role: resolve_subagent_route(config=config, role=role).to_dict()
        for role in ("architect", "backend_developer", "frontend_developer")
    }
    preflight_errors = {
        role: decision
        for role, decision in preflight.items()
        if decision.get("runtime_family") != "codex"
    }

    if controller is None:
        runner = AgentRunner(config)
        controller = SubAgentRuntimeController(runner, project_root=run_root)

    started_at = datetime.now(timezone.utc).isoformat()
    role_results: dict[str, dict[str, Any]] = {}
    overall_passed = True

    if preflight_errors:
        summary = {
            "status": "failed",
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "run_root": str(run_root),
            "workspace_root": str(fixture.workspace_root),
            "task_dir": str(fixture.task_dir),
            "summary_file": str(fixture.summary_file),
            "preflight_routes": preflight,
            "preflight_errors": preflight_errors,
            "roles": role_results,
        }
        fixture.summary_file.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return summary

    for spec in build_role_specs(fixture):
        architect_failed = (
            spec.role != "architect"
            and role_results.get("architect", {}).get("passed") is False
        )
        if architect_failed or (spec.role != "architect" and not fixture.design_output.exists()):
            evidence = RoleEvidence(
                role=spec.role,
                status="failed",
                passed=False,
                workspace_root="",
                output_file=str(spec.output_file),
                output_exists=False,
                changed_files=[],
                output_marker_results={marker: False for marker in spec.output_markers},
                changed_file_results={rel_path: False for rel_path in spec.changed_files},
                workspace_marker_results={
                    rel_path: {marker: False for marker in markers}
                    for rel_path, markers in spec.workspace_markers.items()
                },
                route_decision={},
                runtime_metadata={},
                usage={},
                session_id="",
                selected_model="",
                output_excerpt="",
                error="architect 阶段未成功完成，后续角色跳过",
            )
        else:
            evidence = await execute_role(controller, fixture, spec)
        role_results[spec.role] = asdict(evidence)
        overall_passed = overall_passed and evidence.passed

    summary = {
        "status": "success" if overall_passed else "failed",
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "run_root": str(run_root),
        "workspace_root": str(fixture.workspace_root),
        "task_dir": str(fixture.task_dir),
        "summary_file": str(fixture.summary_file),
        "preflight_routes": preflight,
        "roles": role_results,
    }
    fixture.summary_file.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run real Codex pilot E2E verification for architect/backend/frontend roles.",
    )
    parser.add_argument(
        "--run-root",
        default="",
        help="Artifact root directory. Defaults to a new temp directory.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.run_root:
        run_root = Path(args.run_root).expanduser().resolve()
        run_root.mkdir(parents=True, exist_ok=True)
    else:
        run_root = Path(tempfile.mkdtemp(prefix="vizo-codex-pilot-e2e-"))

    summary = asyncio.run(run_codex_pilot_e2e(run_root=run_root))
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if summary["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
