from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from lib.project_identity import canonicalize_project_name, detect_project, normalize_runtime_path
from lib.project_memory_whitelist import (
    MAIN_SESSION_SCOPE,
    WHITELIST_MEMORY_NAME,
    load_project_memory_whitelist,
    resolve_project_root_by_name,
    resolve_required_memories,
    resolve_serena_project_name,
)


_PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class StartupProtocolContext:
    project_name: str
    cwd: str
    project_root: str
    serena_project: str
    scope: str
    whitelist_loaded: bool
    available_memories: tuple[str, ...]
    required_memories: tuple[str, ...]


def resolve_startup_protocol_context(
    *,
    project: str | None = None,
    cwd: str | None = None,
    scope: str | None = None,
    config: dict | None = None,
    fallback_root: Path | str | None = None,
) -> StartupProtocolContext:
    candidate_cwd = normalize_runtime_path(cwd or fallback_root or ".")
    project_name = canonicalize_project_name(project) or detect_project(candidate_cwd)
    # When the caller provides an explicit cwd, treat that path as the primary
    # project-root fallback. This prevents Vizo's own repo root from leaking its
    # memories into an unregistered user project opened via custom path.
    root_fallback = Path(candidate_cwd) if cwd else Path(normalize_runtime_path(fallback_root or candidate_cwd))
    project_root_path = resolve_project_root_by_name(
        project_name,
        config=config,
        fallback_root=root_fallback,
    )
    project_root = str((project_root_path or Path(candidate_cwd)).resolve())
    available_memories = tuple(_list_available_memories(project_root))
    whitelist = load_project_memory_whitelist(project_root)
    required = [
        name
        for name in resolve_required_memories(whitelist, scope or MAIN_SESSION_SCOPE)
        if name in set(available_memories)
    ]
    if not required:
        fallback_reads = [name for name in ("project-overview", "tech-stack") if name in set(available_memories)]
        required = fallback_reads

    serena_project = (
        resolve_serena_project_name(project_name, config=config)
        or project_name
        or Path(project_root).name
    )
    return StartupProtocolContext(
        project_name=project_name or Path(project_root).name,
        cwd=candidate_cwd,
        project_root=project_root,
        serena_project=serena_project,
        scope=str(scope or ""),
        whitelist_loaded=bool(whitelist),
        available_memories=available_memories,
        required_memories=tuple(required),
    )


def build_memory_read_sequence(context: StartupProtocolContext) -> list[str]:
    items: list[str] = []
    if context.whitelist_loaded:
        items.append(WHITELIST_MEMORY_NAME)
    items.extend(context.required_memories)
    return _dedupe(items)


def build_main_session_protocol(context: StartupProtocolContext, *, repo_root: Path | str | None = None) -> str:
    lines = [
        "# Vizo 统一启动协议",
        "你运行在 Vizo Web Console 的主会话中。以下规则由平台注入，适用于 Claude Code 和 Codex。",
        "",
        "## 当前项目上下文",
        f"- 当前项目：{context.project_name}",
        f"- 当前工作目录：{context.cwd}",
        f"- 项目根目录：{context.project_root}",
        f"- Serena project：{context.serena_project}",
        "",
        "## 处理当前用户请求前的工作方法",
        "1. 如果问题涉及当前项目的代码、架构、文档、设计规范或项目记忆，先使用 Serena 再处理。",
        "2. 如果用户是在描述一个新的小程序、App、网站、页面或概念 demo，且你判断适合先在当前会话里快速预览，优先使用 `vizo-router` 的 `interaction_design_session` 工具：先看 `status`，需要候选时再 `propose`，不要直接套用当前项目的旧设计记忆。",
        "3. 如果你判断这类页面/demo需求更适合走正式后台多步骤任务，优先使用 `vizo-router` 的 `route_agent_request` 判断是否应委派给 `interaction_design`，而不是直接手工在主会话里模拟完整交互设计流程。",
        "4. 如果用户明确说“把这套设为项目默认设计系统”或“用刚才那套替换当前默认”，先用 `interaction_design_session(action=\"status\")` 确认当前是 preview 还是最近正式交互设计任务结果可被采纳，再调用 `interaction_design_session(action=\"adopt\")`；adopt 后应说明平台会写 `.vizo/design-assets.json` 和 `.serena/memories/design/assets.md`，不要在未确认来源时直接声称已经写入项目设计记忆。",
        "5. 如果你需要判断该走哪个 AgentHub 模块、是否该发起正式任务、有哪些模块可用，优先使用 `vizo-router` 的 `route_agent_request` 或 `list_agent_capabilities`，不要只靠关键词主观猜测。",
        "6. 如果当前运行时没有 `vizo-router`，明确告诉用户工具缺失；不要假装已经完成结构化路由或交互设计流程。",
        "7. 如果你已经判断本次需求应该委派给后台任务处理，先用 `vizo-router` 的 `background_task_session` 创建任务提案，不要直接执行 `opus.py` 或手工 shell 启动正式任务。",
        "8. 只有在用户明确同意“继续/开始”之后，才调用 `background_task_session(action=\"confirm\")` 真正启动后台任务；如果用户拒绝，则调用 `cancel` 或继续留在主会话沟通。",
        "9. 如果用户明确要求你就在当前会话里直接完成一个小改动、快速分析或方案讨论，不要额外发起正式任务。",
        "",
        "## Serena 启动顺序",
        f'1. `mcp__serena__activate_project("{context.serena_project}")`',
    ]
    reads = build_memory_read_sequence(context)
    if reads:
        for index, memory_name in enumerate(reads, start=2):
            lines.append(f'{index}. `mcp__serena__read_memory("{memory_name}")`')
    else:
        lines.append("2. 当前项目暂无白名单或显式必读条目；先用 `mcp__serena__list_memories()` 查看可用条目，再按需读取。")
    next_index = len(reads) + 2
    lines.extend(
        [
            f"{next_index}. 读完以上条目后，再按当前任务继续补读相关 memory，避免一次性读取全部条目。",
            "",
            "## 记忆边界",
            "- 研发项目知识走项目 `.serena/memories/*.md`。",
            "- AgentHub 领域知识走 hub 命名空间；不要把 `hub/{module_id}` 知识当成通用研发项目记忆。",
            "- 如果你发起的是正式研发任务，应让流程继续经过知识沉淀阶段，不要手工绕过。",
            "- 后续生成前端页面时，如果项目已有 adopted design context，应先读取该上下文和 `.vizo/design-assets.json` 本地资源 manifest；字体、图标、CSS 通过 manifest 中的 `resources[].usage` 或 `css.imports` 取用，不要硬写远程 CDN 或外部 URL。",
        ]
    )
    return "\n".join(lines).strip()


def build_role_startup_instructions(context: StartupProtocolContext, *, role: str) -> str:
    lines = [
        "## Vizo 项目启动步骤（必须先执行）",
        f"- 当前角色：{role}",
        f"- 当前项目：{context.project_name}",
        f"- Serena project：{context.serena_project}",
        "按以下顺序执行：",
        f'1. `mcp__serena__activate_project("{context.serena_project}")`',
    ]
    reads = build_memory_read_sequence(context)
    if reads:
        for index, memory_name in enumerate(reads, start=2):
            lines.append(f'{index}. `mcp__serena__read_memory("{memory_name}")`')
    else:
        lines.append("2. 当前项目未声明必读记忆；先用 `mcp__serena__list_memories()` 查看可用条目，再按任务读取。")
    next_index = len(reads) + 2
    lines.extend(
        [
            f"{next_index}. 读完以上条目后，再按当前任务补读其他相关 memory，不要跳过白名单直接开始编码、设计或测试。",
            "- 输入文档中的当轮技术方案/设计稿是当前任务约束，项目记忆负责提供长期背景。",
            "- 如果当前任务属于正式 Vizo 流程，不要自行猜测 AgentHub 的知识路径或绕开项目知识沉淀规则。",
        ]
    )
    return "\n".join(lines).strip()


def _list_available_memories(project_root: str | Path) -> Sequence[str]:
    memories_dir = Path(project_root) / ".serena" / "memories"
    if not memories_dir.exists():
        return ()
    names: list[str] = []
    for md_file in sorted(memories_dir.rglob("*.md")):
        name = str(md_file.relative_to(memories_dir))[:-3]
        if name not in names:
            names.append(name)
    return tuple(names)


def _dedupe(items: Iterable[str]) -> list[str]:
    deduped: list[str] = []
    for item in items:
        value = str(item or "").strip()
        if value and value not in deduped:
            deduped.append(value)
    return deduped
