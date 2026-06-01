from __future__ import annotations

from pathlib import Path


def bootstrap_serena_project(
    project_root: Path | str,
    *,
    project_name: str,
    display_name: str | None = None,
    description: str = "",
    project_type: str = "dev",
) -> list[Path]:
    root = Path(project_root)
    serena_dir = root / ".serena"
    memories_dir = serena_dir / "memories"
    startup_dir = memories_dir / "startup"
    startup_dir.mkdir(parents=True, exist_ok=True)

    created: list[Path] = []
    files = {
        serena_dir / "project.yml": _render_project_yml(
            project_name=project_name,
            display_name=display_name,
            description=description,
        ),
        memories_dir / "project-overview.md": _render_project_overview(
            project_name=project_name,
            display_name=display_name,
            description=description,
            project_type=project_type,
        ),
        memories_dir / "index.md": _render_index_template(),
        memories_dir / "architecture.md": _render_architecture_template(),
        memories_dir / "code-style.md": _render_code_style_template(),
        memories_dir / "common-pitfalls.md": _render_common_pitfalls_template(),
        memories_dir / "tech-stack.md": _render_tech_stack_template(),
        startup_dir / "role-memory-whitelist.md": _render_role_memory_whitelist_template(),
        startup_dir / "vizo-collaboration-protocol.md": _render_vizo_collaboration_protocol(),
    }

    for path, content in files.items():
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        created.append(path)
    return created


def _render_project_yml(*, project_name: str, display_name: str | None, description: str) -> str:
    initial_prompt = (description or display_name or project_name).strip()
    return (
        f'project_name: "{project_name}"\n'
        'languages: []\n'
        'encoding: "utf-8"\n'
        'ignore_all_files_in_gitignore: false\n'
        'ignored_paths:\n'
        '  - "**/__pycache__/**"\n'
        'read_only: false\n'
        'initial_prompt: |\n'
        f"  {initial_prompt or project_name}\n"
    )


def _render_project_overview(*, project_name: str, display_name: str | None, description: str, project_type: str) -> str:
    title = display_name or project_name
    project_kind = "AgentHub 项目" if project_type == "hub" else "研发项目"
    goal = description or "（待补充）"
    return f"""# 项目概述

## 项目标识
- 项目名：`{project_name}`
- 展示名：`{title}`
- 项目类型：`{project_kind}`

## 项目目标
{goal}

## 领域范围
（待补充）

## 关键业务约束
- 用户特定要求：（待补充）
- 外部系统或第三方约束：（待补充）

## 相关文档
- `index`
- `architecture`
- `code-style`
- `common-pitfalls`
- `tech-stack`
"""


def _render_architecture_template() -> str:
    return """# 技术架构

## 当前系统结构
（待补充）

## 核心模块
（待补充）

## 关键数据流 / 调用链
（待补充）

## 近期架构决策
- （待补充）
"""


def _render_code_style_template() -> str:
    return """# 代码风格

## 通用约定
- 保持与现有代码风格一致
- 修改公共接口前先确认影响范围
- 优先复用已有工具、框架和目录结构

## 项目特定规范
（待补充）
"""


def _render_common_pitfalls_template() -> str:
    return """# 常见陷阱

## 已知高风险点
- （待补充）

## 调试提示
- （待补充）

## 不要这样做
- （待补充）
"""


def _render_tech_stack_template() -> str:
    return """# 技术栈

## 产品形态
（待识别）

## 前端栈
（待补充）

## 后端栈
（待补充）

## 部署方式
（待补充）

## 自定义约束
（待补充）
"""


def _render_index_template() -> str:
    return """# 项目记忆治理入口

## 直接 CLI 会话先读本文件

**决策**：在 Codex CLI、Claude Code CLI 或其他非 Vizo 托管会话中，先读取 `index`，再按任务读取其他 memory。
**理由**：直接 CLI 会话不一定经过 Vizo 的角色启动协议，也不一定自动遵循 `startup/role-memory-whitelist`。
**约束**：本文只做目录地图和读取顺序，不替代 Vizo 托管流程的机器可读白名单。

## 默认开发任务读取顺序

**方案**：普通代码修改先读 `project-overview`、`tech-stack`、`architecture`、`code-style`、`common-pitfalls`。
**约束**：读完入口和索引后，再按任务补读 `architecture/*`、`common-pitfalls/*` 等子文件，避免一次性读取全部记忆。

## UI 和前端任务读取顺序

**方案**：涉及 UI、前端、交互、视觉或组件时，先读设计入口；涉及系统级颜色、字号、圆角、阴影、字体或组件参数时，再读设计 token。
**约束**：设计预览、结构契约和本地资源快照属于设计资产，不应混入普通架构或陷阱记忆。

## 构建部署和运维任务读取顺序

**方案**：涉及构建、部署、服务重启或环境排查时，按需读取 `build/*` 和相关 `common-pitfalls/*`。
**约束**：项目专属运维事实应沉淀到 `build/*`，不要放进 `startup/*`。

## startup 目录职责

**决策**：`startup/role-memory-whitelist` 是 Vizo 托管流程的机器可读必读清单；`startup/vizo-collaboration-protocol` 是托管流程协作协议。
**约束**：直接 CLI 会话可以参考它们，但不要把普通项目知识、交付记录或运维手册继续放进 `startup/*`。

## active memory 不存历史交付记录

**决策**：`.serena/memories` 只放长期稳定知识、治理入口、设计资产入口和运维知识。
**约束**：历史交付记录、一次性任务清单和阶段文档应留在项目文档或 archive，不应放进 active memories。

## 记忆更新必须遵守 knowledge 角色治理

**决策**：长期记忆更新以 `role_templates/knowledge_engineer.md` 和 `role_templates/knowledge_admin.md` 为最高规则。
**约束**：除本入口外，只写入白名单允许的项目知识目标；新增记忆前先确认目录职责。
"""


def _render_role_memory_whitelist_template() -> str:
    return """# 项目记忆白名单

这是项目级唯一的必读 Serena memory 清单，主会话和各个角色都从这里取必读项。

- `always`：所有会话都必须先读
- `main_session`：主会话开工前必须先读
- `<role>`：对应角色开工前必须先读

如果项目后续新增设计规范、架构决策或领域知识，应把对应 memory 加到正确角色段，而不是修改通用角色模板。

补读规则（说明，不是 scope）：
- 任何角色只要任务涉及 UI、前端、交互、视觉或组件文件语义识别，都必须额外补读 `design/redmine`。
- 如果任务需要系统级 token、色板、字号、圆角、阴影或组件参数，再补读 `design/vizo-ui-tokens`。
- 如果任务命中具体组件，再按 `design/redmine` 的“当前组件资产”表打开对应 `design/html/*.html` 或读取对应 `design/html/*.md`。
- 只有需要定位当前实现落点时，才根据 `design/redmine` 里的搜索线索继续用 `rg` 查代码。

## always
- index
- project-overview
- tech-stack

## main_session
- startup/vizo-collaboration-protocol
- architecture
- code-style
- common-pitfalls

## assistant
- startup/vizo-collaboration-protocol

## requirement_analyst
- startup/vizo-collaboration-protocol
- project-overview
- architecture

## product_manager
- startup/vizo-collaboration-protocol
- project-overview

## architect
- startup/vizo-collaboration-protocol
- architecture
- tech-stack
- common-pitfalls

## backend_developer
- startup/vizo-collaboration-protocol
- architecture
- code-style
- common-pitfalls

## frontend_developer
- startup/vizo-collaboration-protocol
- design/redmine
- design/vizo-ui-tokens
- architecture
- code-style

## interaction_designer
- startup/vizo-collaboration-protocol
- design/redmine
- design/vizo-ui-tokens

## qa_engineer
- startup/vizo-collaboration-protocol
- architecture
- common-pitfalls

## integration_engineer
- startup/vizo-collaboration-protocol
- architecture
- code-style

## devops_engineer
- startup/vizo-collaboration-protocol
- architecture
- common-pitfalls

## fix_engineer
- startup/vizo-collaboration-protocol
- architecture
- code-style
- common-pitfalls

## knowledge_engineer
- startup/vizo-collaboration-protocol
- project-overview
- architecture

## knowledge_admin
- startup/vizo-collaboration-protocol
- project-overview
- architecture
- code-style
"""


def _render_vizo_collaboration_protocol() -> str:
    return """# Vizo 协作协议

## 目标
这是一份由 `Vizo` 平台维护的共享工作协议，主会话和各角色都应按它理解项目记忆、正式任务和知识沉淀。

## 启动顺序
1. 激活当前项目的 Serena project
2. 读取 `startup/role-memory-whitelist`
3. 读取自己 scope 对应的必读条目
4. 再按当前任务补读其他相关 memory

## 方法选择
- 当前项目代码、架构、文档、设计规范问题：先用 Serena 再处理
- 如果主会话需要判断该走哪个 AgentHub 模块、是否该发起正式任务或有哪些模块可用，优先使用 `vizo-router`
- 如果用户是在描述一个新的小程序、App、网站、页面或概念 demo，且你判断适合先在当前会话快速预览，优先使用 `vizo-router` 的交互设计流程，不要直接套用当前项目的旧设计记忆
- 如果你判断这类页面/demo需求更适合走正式后台多步骤任务，优先通过 `vizo-router` 路由到 `interaction_design`，再走后台任务提案
- 如果用户明确说“把这套设为项目默认设计系统”或“用刚才那套替换当前默认”，先用 `interaction_design_session(status)` 确认当前是 preview 还是最近正式交互设计任务结果可被采纳，再调用 `interaction_design_session(adopt)`；adopt 后应说明平台会写 `.vizo/design-assets.json` 和 `.serena/memories/design/assets.md`
- 生成前端页面时，如果项目已有 adopted design context，应优先读取 adopted design context 和 `.vizo/design-assets.json` 本地资源 manifest；字体、图标、CSS 通过 manifest 中的 `resources[].usage` 或 `css.imports` 取用，不要硬写远程 CDN 或外部 URL
- 小问题、小改动、快速分析：可在当前会话直接完成
- 如果主会话已经判断当前需求应该委派到后台任务，先通过 `vizo-router` 的 `background_task_session` 创建任务提案，再向用户确认是否继续
- 只有用户明确同意后，才允许真正启动后台正式任务；如果用户拒绝，则留在主会话继续沟通
- 如果当前运行时没有 `vizo-router`，应明确告知工具缺失，不要伪造结构化路由结果

## 记忆边界
- 研发项目知识使用项目 `.serena/memories/*.md`
- AgentHub 领域知识保持独立命名空间，不要与研发项目知识混写
- 不要跳过项目白名单直接开始工作

## UI / Design 发现协议
- 任何角色只要任务涉及 UI、前端、交互、视觉、组件或页面文件语义识别，先补读 `design/redmine`
- 如果任务需要统一查看当前组件效果和交互，下一步优先补读 `design/html/component-showcase`
- 如果任务需要精确复用组件结构，下一步按 `design/redmine` 的“当前组件资产”表读取对应 `design/html/*.md` 或打开对应 HTML 预览
- 如果任务还涉及系统级 token、色板、字号、圆角、阴影或组件参数，再补读 `design/vizo-ui-tokens`

## 收尾要求
- 正式开发任务应继续经过知识沉淀阶段
- 如果本次只是在当前会话内补文档或做小修，也应优先更新项目文档和项目记忆，而不是只留在临时上下文里
"""
