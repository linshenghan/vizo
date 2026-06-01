#!/usr/bin/env python3
"""
修复子任务失败时父任务状态不更新的问题
"""

import os
import sys
from pathlib import Path

def get_project_root():
    current = Path(__file__).resolve().parent
    while current != current.parent:
        if (current / "orchestrator.py").exists():
            return current
        current = current.parent
    raise FileNotFoundError("找不到 orchestrator.py")

def apply_fix():
    project_root = get_project_root()
    orchestrator_file = project_root / "orchestrator.py"
    
    if not orchestrator_file.exists():
        print(f"错误：找不到 orchestrator.py 文件")
        return False
    
    original_content = orchestrator_file.read_text(encoding="utf-8")
    
    search_pattern = """            except (WorkflowError, AgentTimeoutError, AgentError) as e:
                logger.error(f"子任务 {sub_task_def.get('name', sub_task_def['id'])} 失败: {e}")
                # 先读取费用再回滚（rollback 不删 cost.json 但保险起见先读取）
                sub_err_cost = self.state.get_task_cost(sub_task)
                sub_err_duration = time.time() - sub_start_time
                self.state.rollback_sub_task(sub_task)
                sub_task_def["status"] = "failed"
                sub_task_def["error"] = str(e)[:200]
                self._update_progress(parent_task, "agent_error",
                                      role=f"sub-task:{sub_name}", model="multi",
                                      cost_usd=sub_err_cost["total_usd"],
                                      duration=sub_err_duration,
                                      error_msg=str(e)[:200])
                await self._push_step_card(parent_task, "agent_error",
                                           role=f"sub-task:{sub_name}",
                                           error_msg=str(e)[:200])
                self.state._save_state(parent_task)
                self.ui.print_warning(
                    f"子任务 [{sub_task_def.get('name')}] 失败并已回滚: {e}"
                )
                return False"""
    
    replacement_pattern = """            except (WorkflowError, AgentTimeoutError, AgentError) as e:
                logger.error(f"子任务 {sub_task_def.get('name', sub_task_def['id'])} 失败: {e}")
                # 先读取费用再回滚（rollback 不删 cost.json 但保险起见先读取）
                sub_err_cost = self.state.get_task_cost(sub_task)
                sub_err_duration = time.time() - sub_start_time
                self.state.rollback_sub_task(sub_task)
                sub_task_def["status"] = "failed"
                sub_task_def["error"] = str(e)[:200]
                self._update_progress(parent_task, "agent_error",
                                      role=f"sub-task:{sub_name}", model="multi",
                                      cost_usd=sub_err_cost["total_usd"],
                                      duration=sub_err_duration,
                                      error_msg=str(e)[:200])
                await self._push_step_card(parent_task, "agent_error",
                                           role=f"sub-task:{sub_name}",
                                           error_msg=str(e)[:200])
                self.state._save_state(parent_task)
                self.ui.print_warning(
                    f"子任务 [{sub_task_def.get('name')}] 失败并已回滚: {e}"
                )
                
                # 新增：检查是否应该提前终止父任务
                failed_sub_id = sub_task_def.get("id")
                if failed_sub_id:
                    dependent_subs = [
                        s for s in (parent_task.sub_tasks or [])
                        if failed_sub_id in s.get("depends_on", [])
                    ]
                    if dependent_subs:
                        dep_names = [s.get("name", s.get("id", "?")) for s in dependent_subs]
                        logger.info(
                            f"子任务 {failed_sub_id} 失败，"
                            f"依赖它的子任务 {dep_names} 无法执行，提前终止父任务"
                        )
                        parent_task.status = "failed"
                        self.state._save_state(parent_task)
                        self._update_progress(parent_task, "agent_error",
                                              role=f"sub-task:{sub_name}",
                                              error_msg=f"关键子任务失败，依赖它的子任务无法执行")
                
                return False"""
    
    if search_pattern in original_content:
        new_content = original_content.replace(search_pattern, replacement_pattern)
        backup_file = orchestrator_file.with_suffix(".py.bak")
        backup_file.write_text(original_content, encoding="utf-8")
        print(f"已备份原文件到: {backup_file}")
        orchestrator_file.write_text(new_content, encoding="utf-8")
        print(f"已修改 orchestrator.py")
        return True
    else:
        print("警告：找不到需要修改的代码段")
        return False

def main():
    print("=" * 60)
    print("子任务失败状态同步修复工具")
    print("=" * 60)
    print()
    
    try:
        if apply_fix():
            print()
            print("✓ 修复成功！")
            print()
            print("修改说明：")
            print("当子任务失败时，系统会检查是否有其他子任务依赖它")
            print("如果有依赖关系，父任务将被标记为 'failed' 并提前终止")
            print()
            print("重启服务后生效：")
            print("  systemctl --user restart vizo-secretary.service")
        else:
            print()
            print("✗ 修复失败，请手动修改")
    except Exception as e:
        print(f"错误: {e}")
        return 1
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
