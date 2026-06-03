import asyncio

from vizo_core.agent_runner import AgentRunner


def _runner(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    return AgentRunner(
        {
            "default_project": "demo",
            "projects": {
                "demo": {
                    "path": str(project_root),
                }
            },
        }
    )


def test_operational_guidance_excludes_generated_runtime_logs_from_rg(tmp_path):
    task_dir = tmp_path / ".vizo" / "tasks" / "task-1"
    guidance = AgentRunner._build_role_operational_guidance("frontend_developer", task_dir)

    assert str(task_dir) in guidance
    assert ".vizo/tasks/" in guidance
    assert ".opus/tasks" in guidance
    assert "不要查询 `.opus/tasks/` 旧路径" in guidance
    assert "--glob '!.vizo/tasks/**/runtime/subagents/**'" in guidance
    assert "--glob '!**/raw-events*.jsonl'" in guidance
    assert "--glob '!**/logs/**'" in guidance
    assert "Python-only Serena" in guidance
    assert "不要优先调用 `mcp__serena__get_symbols_overview`" in guidance


def test_knowledge_admin_guidance_requires_memory_files_in_files_changed(tmp_path):
    guidance = AgentRunner._build_role_operational_guidance("knowledge_admin", tmp_path / "task")

    assert ".serena/memories/**" in guidance
    assert "`files_changed` 必须包含实际变更的 memory 文件路径" in guidance
    assert "edit_memory" in guidance
    assert "不要把直接文件补丁伪装成 API 写入成功" in guidance


def test_build_prompt_includes_operational_guidance(tmp_path):
    runner = _runner(tmp_path)
    template_path = tmp_path / "role.md"
    template_path.write_text("# Frontend Role\n", encoding="utf-8")
    task_dir = tmp_path / ".vizo" / "tasks" / "task-2"
    task_dir.mkdir(parents=True)

    prompt = asyncio.run(
        runner._build_prompt(
            "frontend_developer",
            task_dir,
            {"request": "Update app.js and index.html"},
            memories=None,
            output_file=task_dir / "frontend-result.md",
            project="demo",
            template_override=template_path,
        )
    )

    assert "Update app.js and index.html" in prompt
    assert "使用 `rg`/Grep 搜索时默认排除生成日志和子代理运行事件" in prompt
    assert "不要查询 `.opus/tasks/` 旧路径" in prompt
    assert "对 JS/HTML/CSS/静态资源/模板文件，优先用 `rg`" in prompt
    assert str(task_dir) in prompt


def test_truncate_prompt_preserves_operational_guidance(tmp_path):
    runner = _runner(tmp_path)
    template_path = tmp_path / "role.md"
    template_path.write_text("# Knowledge Admin\n", encoding="utf-8")
    task_dir = tmp_path / ".vizo" / "tasks" / "task-3"
    task_dir.mkdir(parents=True)

    prompt = asyncio.run(
        runner._build_prompt(
            "knowledge_admin",
            task_dir,
            {"large": "A" * 20000},
            memories=None,
            output_file=task_dir / "knowledge-result.md",
            project="demo",
            template_override=template_path,
        )
    )
    truncated = runner._truncate_prompt(prompt, target=1000)

    assert "knowledge_admin 记忆写入报告要求" in truncated
    assert "`files_changed` 必须包含实际变更的 memory 文件路径" in truncated
    assert "--glob '!.vizo/tasks/**/runtime/subagents/**'" in truncated
    assert str(task_dir) in truncated
