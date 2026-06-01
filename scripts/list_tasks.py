#!/usr/bin/env python3
"""快速列出当前项目的未完成任务（不依赖任何交互）"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from lib.project_identity import detect_project as detect_runtime_project
from lib.paths import iter_task_dirs


def main():
    # 自动检测当前项目
    cwd = Path.cwd()
    project_root = PROJECT_ROOT
    project_name = detect_runtime_project(str(cwd))
    if project_name == 'vizo':
        project_path = project_root
    elif project_name == 'xiaozhi':
        project_name = 'xiaozhi-server'
        project_path = Path('/opt/xiaozhi-server')
    else:
        print(f"❌ 无法识别当前项目（需在 {project_root} 或 /opt/xiaozhi-server 下运行）")
        sys.exit(1)

    # 读取任务列表
    tasks = []
    for task_dir in iter_task_dirs(project_path):
        state_file = task_dir / 'state.json'
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text(encoding='utf-8'))
                if state.get('status') in ('running', 'paused', 'in_progress'):
                    tasks.append(state)
            except Exception:
                pass

    if not tasks:
        print(f"📭 {project_name} 项目暂无未完成任务")
        sys.exit(0)

    print(f"\n📋 {project_name} 项目 - 未完成的任务 ({len(tasks)} 个)\n")

    # 步骤中文化映射
    step_map = {
        "requirement_analysis": "已完成需求分析",
        "pm_prd": "已完成产品文档",
        "architect": "已完成架构设计",
        "parallel_dev": "已完成代码编写",
        "integration": "已完成集成测试",
        "test_round_1": "第1轮测试修复",
        "test_round_2": "第2轮测试修复",
        "test_round_3": "第3轮测试修复",
        "knowledge": "已完成知识沉淀",
        "deploy": "已完成部署上线",
        "fix": "已完成问题修复",
        "refactor": "已完成代码重构",
        "qa_engineer": "已完成测试验证",
        "embedded": "已完成嵌入式调试",
        "project_planning": "已完成项目规划",
        "final_integration_test": "已完成最终集成测试",
    }

    for i, task in enumerate(tasks[:10], 1):
        status = "⏸ 暂停" if task.get('status') == 'paused' else "⚡ 中断"
        desc = task.get('description', '无描述')
        task_id = task.get('id')

        # 获取当前进度
        completed_steps = task.get('completed_steps', [])
        if completed_steps:
            last_step = completed_steps[-1]
            progress = step_map.get(last_step, last_step)
        else:
            progress = "尚未开始"

        print(f"  [{i}] {desc}")
        print(f"      {status} | 进度: {progress}")
        print(f"      ID: {task_id}")
        print()

    if len(tasks) > 10:
        print(f"  ... 还有 {len(tasks) - 10} 个任务")
        print()

    print("💡 要恢复任务，运行:")
    print(f"   vizo --resume --task-id <任务ID>")
    print()
    print("📋 例如:")
    if tasks:
        print(f"   vizo --resume --task-id {tasks[0].get('id')}")

if __name__ == '__main__':
    main()
