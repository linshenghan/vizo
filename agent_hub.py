"""
AgentHub 编排器

manifest 驱动的非开发任务编排引擎。
加载模块 → 校验 manifest → 顺序执行步骤 → 汇总产出。
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import traceback
from datetime import datetime
from pathlib import Path

from agent_runner import AgentRunner
from lib.paths import (
    iter_task_dirs,
    task_dir as resolve_task_dir,
    write_data_path,
)
from lib.project_memory_whitelist import load_project_memory_whitelist, resolve_required_memories
from lib.runtime.subagents.controller import SubAgentRuntimeController
from lib.task_resource_guard import assert_task_creation_allowed
from lib.task_titles import summarize_task_title
from user_interface import ConfirmContext, UserInterface

logger = logging.getLogger(__name__)

MAIN_SESSION_MODEL_ID = "__main_session__"
VALID_REASONING_EFFORTS = {"inherit", "low", "medium", "high", "xhigh"}
AGENTHUB_STATE_HEARTBEAT_INTERVAL = 30


class PauseSignal(Exception):
    """hub 任务暂停信号"""
    pass

class TerminateSignal(Exception):
    """hub 任务终止信号"""
    pass

class AgentHub:
    def __init__(self, config: dict):
        self.config = config
        self.agent = AgentRunner(config)
        self.ui = UserInterface(config)
        self._task_lock_fd = None

    async def execute(self, module_id: str = None, user_request: str = None,
                      workflow_id: str = None, project_config: dict = None,
                      resume: bool = False, task_id: str = None):
        """执行 AgentHub 工作流主入口"""
        project_config = project_config or {}

        try:
            if resume and task_id:
                # === resume 分支：从 state.json 恢复 ===
                task = self._load_task_for_resume(task_id)
                module_id = task["module_id"]
                manifest, module_dir = self._load_and_validate_manifest(module_id)
                wf_id = task["workflow_id"]
                steps = self._resolve_workflow(manifest, wf_id)[1]

                self._acquire_task_lock(task)
                self._ensure_task_title(task)
                task["status"] = "running"
                self._set_next_pending_step(task, steps)
                self._save_task_state(task)

                completed = task.get("completed_steps", [])
                self.ui.print_info(f"▶ 恢复任务 {task['id']}")
                self.ui.print_info(f"   已完成步骤：{', '.join(completed) if completed else '无'}")
            else:
                # === 新任务分支（现有逻辑不变） ===
                manifest, module_dir = self._load_and_validate_manifest(module_id)
                wf_id, steps = self._resolve_workflow(manifest, workflow_id)
                task = self._create_task(module_id, wf_id, user_request, project_config)
                self.ui.print_info(f"📋 任务 {task['id']} 已创建")
                self.ui.print_info(
                    f"   模块: {manifest['name']} · "
                    f"{manifest['workflows'][wf_id].get('name', wf_id)}"
                )

            try:
                await self._execute_steps(task, steps, manifest, module_dir)
                await self._finalize_task(task)
            except PauseSignal:
                completed = task.get("completed_steps", [])
                total = len(steps)
                self.ui.print_info(
                    f"\n⏸ 任务已暂停（步骤 {len(completed)}/{total} 完成）\n"
                    f"   已完成：{', '.join(completed) if completed else '无'}\n"
                    f"   恢复：opus --resume --task-id {task['id']}"
                )
            except TerminateSignal:
                task["status"] = "terminated"
                task["completed_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                self._save_task_state(task)
                self._write_hub_history(task)
                self.ui.print_info(
                    f"\n🛑 任务已终止（用户主动终止）\n"
                    f"   已完成步骤产出保留在：{task['task_dir']}/outputs/"
                )
            except Exception as exc:
                task["status"] = "failed"
                task["failed_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                task["last_error"] = f"{type(exc).__name__}: {exc}"
                self._append_task_live_log(
                    task,
                    "AgentHub 执行异常，任务已标记失败。\n"
                    + traceback.format_exc(limit=12),
                )
                self._save_task_state(task)
                raise
        finally:
            self._release_task_lock()

        return task

    def _acquire_task_lock(self, task: dict):
        """Hold a task-level lock so orphan cleanup can distinguish live hub tasks."""
        import fcntl

        task_dir = Path(task["task_dir"])
        task_dir.mkdir(parents=True, exist_ok=True)
        lock_file_path = task_dir / ".lock"
        lock_fd = open(lock_file_path, "w")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            lock_fd.close()
            raise RuntimeError(f"任务 {task.get('id', '')} 已在其他进程中运行")

        lock_fd.seek(0)
        lock_fd.write(str(os.getpid()))
        lock_fd.truncate()
        lock_fd.flush()
        self._task_lock_fd = lock_fd
        task["runner_pid"] = os.getpid()

    def _release_task_lock(self):
        """Release the task-level lock held by this AgentHub process."""
        if not self._task_lock_fd:
            return
        try:
            import fcntl
            fcntl.flock(self._task_lock_fd, fcntl.LOCK_UN)
            self._task_lock_fd.close()
        except Exception:
            pass
        finally:
            self._task_lock_fd = None

    @staticmethod
    def _ensure_task_title(task: dict) -> str:
        title = summarize_task_title(
            task.get("task_name") or task.get("description") or task.get("original_request") or task.get("id", ""),
            fallback=task.get("id", "未命名任务"),
        )
        task["task_name"] = title
        task["task_title"] = title
        task.setdefault("task_summary", title)
        return title

    def _load_and_validate_manifest(self, module_id: str) -> tuple:
        """加载并校验 manifest，返回 (manifest, module_dir)"""
        base_dir = Path(__file__).parent / "agents"
        module_dir = None

        for search_dir in [base_dir / "_builtin", base_dir / "_user"]:
            candidate = search_dir / module_id
            if (candidate / "manifest.json").exists():
                module_dir = candidate
                break

        if not module_dir:
            raise ValueError(f"模块不存在: {module_id}")

        manifest = json.loads((module_dir / "manifest.json").read_text(encoding="utf-8"))

        # 必填字段校验
        required_fields = ["id", "name", "description", "workflows", "roles"]
        for field in required_fields:
            if field not in manifest:
                raise ValueError(f"manifest 缺少必填字段: {field}")

        # 角色引用校验
        for wf_id, wf in manifest["workflows"].items():
            steps = self._flatten_steps(wf)
            for step in steps:
                role = step["role"]
                if role not in manifest["roles"]:
                    raise ValueError(
                        f"工作流 {wf_id} 步骤 {step['step']} 引用了不存在的角色: {role}")

        # 模板路径安全校验
        for role_name, role_cfg in manifest["roles"].items():
            template = role_cfg.get("template", "")
            if ".." in template:
                raise ValueError(f"角色 {role_name} 模板路径包含非法字符 '..': {template}")
            if not template.startswith("roles/"):
                raise ValueError(f"角色 {role_name} 模板路径必须在 roles/ 目录下: {template}")
            template_path = module_dir / template
            if not template_path.exists():
                raise ValueError(f"模板文件不存在: {template} (角色 {role_name})")

        # 步骤静态输入校验
        module_root = module_dir.resolve()
        for wf_id, wf in manifest["workflows"].items():
            steps = self._flatten_steps(wf)
            for step in steps:
                extra_inputs = step.get("extra_inputs", [])
                if not extra_inputs:
                    continue
                if not isinstance(extra_inputs, list):
                    raise ValueError(f"工作流 {wf_id} 步骤 {step['step']} 的 extra_inputs 必须是数组")
                for item in extra_inputs:
                    if isinstance(item, str):
                        relative_path = item.strip()
                    elif isinstance(item, dict):
                        relative_path = str(item.get("path") or "").strip()
                    else:
                        raise ValueError(
                            f"工作流 {wf_id} 步骤 {step['step']} 的 extra_inputs 项格式非法: {item!r}"
                        )
                    if not relative_path or ".." in relative_path:
                        raise ValueError(
                            f"工作流 {wf_id} 步骤 {step['step']} 的 extra_inputs 路径非法: {relative_path}"
                        )
                    reference_path = (module_dir / relative_path).resolve()
                    try:
                        reference_path.relative_to(module_root)
                    except ValueError as error:
                        raise ValueError(
                            f"工作流 {wf_id} 步骤 {step['step']} 的 extra_inputs 越界: {relative_path}"
                        ) from error
                    if not reference_path.exists() or not reference_path.is_file():
                        raise ValueError(
                            f"工作流 {wf_id} 步骤 {step['step']} 的 extra_inputs 文件不存在: {relative_path}"
                        )

        return manifest, module_dir

    def _flatten_steps(self, workflow: dict) -> list:
        """将 stages 嵌套结构展开为扁平 steps 列表"""
        if "stages" in workflow:
            steps = []
            for stage in workflow["stages"]:
                steps.extend(stage.get("steps", []))
            return steps
        return workflow.get("steps", [])

    def _resolve_workflow(self, manifest: dict, workflow_id: str = None) -> tuple:
        """确定工作流并展开为扁平 steps 列表，返回 (wf_id, steps)"""
        workflows = manifest["workflows"]

        if workflow_id:
            if workflow_id not in workflows:
                available = list(workflows.keys())
                raise ValueError(f"工作流不存在: {workflow_id}，可用: {available}")
            wf = workflows[workflow_id]
        else:
            # 默认取第一个工作流
            workflow_id = next(iter(workflows))
            wf = workflows[workflow_id]

        steps = self._flatten_steps(wf)
        if not steps:
            raise ValueError(f"工作流 {workflow_id} 没有步骤")

        return workflow_id, steps

    def _create_task(self, module_id: str, workflow_id: str,
                     user_request: str, project_config: dict) -> dict:
        """创建任务目录和 state.json"""
        project_path = project_config.get("path", ".")
        assert_task_creation_allowed(project_path, self.config)
        now = datetime.now()
        short_hash = hashlib.md5(f"{now.isoformat()}{user_request}".encode()).hexdigest()[:4]
        task_id = f"hub-{now.strftime('%Y%m%d-%H%M%S')}-{short_hash}"

        task_dir = resolve_task_dir(
            task_id,
            project_root=project_path,
            prefer_existing=False,
        )
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "inputs").mkdir(exist_ok=True)
        (task_dir / "outputs").mkdir(exist_ok=True)
        task_title = summarize_task_title(user_request, fallback=task_id)

        task = {
            "id": task_id,
            "module_id": module_id,
            "workflow_id": workflow_id,
            "project_path": project_path,
            "project_type": project_config.get("type", "dev"),
            "task_name": task_title,
            "task_title": task_title,
            "task_summary": task_title,
            "original_request": user_request,
            "description": user_request,
            "status": "running",
            "completed_steps": [],
            "created_at": now.strftime("%Y-%m-%dT%H:%M:%S"),
            "task_dir": str(task_dir),
        }

        self._acquire_task_lock(task)
        self._save_task_state(task)
        return task

    def _save_task_state(self, task: dict):
        """持久化任务状态"""
        self._ensure_task_title(task)
        task["updated_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        state_file = Path(task["task_dir"]) / "state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(task, indent=2, ensure_ascii=False), encoding="utf-8")

    def _set_current_step(self, task: dict, step: dict, *, status: str = "running") -> None:
        """Update top-level step fields used by external monitors."""
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        task["current_step"] = step.get("step")
        task["current_role"] = step.get("role")
        task["current_step_description"] = step.get("description", step.get("step"))
        task["current_step_status"] = status
        task["heartbeat_at"] = now

    def _set_next_pending_step(self, task: dict, steps: list) -> None:
        completed = set(task.get("completed_steps", []))
        for step in steps:
            if step.get("step") not in completed:
                self._set_current_step(task, step, status="running")
                return
        task["current_step_status"] = "completed"
        task["heartbeat_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    @staticmethod
    def _append_task_live_log(task: dict, message: str) -> None:
        task_dir = task.get("task_dir")
        if not task_dir:
            return
        try:
            log_dir = Path(task_dir) / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%H:%M:%S")
            with (log_dir / "live.log").open("a", encoding="utf-8") as log_file:
                log_file.write(f"\n[{ts}] ❌ {message.rstrip()}\n")
        except Exception:
            pass

    def _load_task_for_resume(self, task_id: str) -> dict:
        """从 state.json 加载任务用于 resume"""
        projects = self.config.get("projects", {})
        for proj_name, proj_info in projects.items():
            proj_path = proj_info.get("path", "")
            if not proj_path:
                continue
            state_file = resolve_task_dir(task_id, project_root=proj_path) / "state.json"
            if state_file.exists():
                try:
                    task = json.loads(state_file.read_text(encoding="utf-8"))
                    # 向后兼容：status 为 None 或缺失时视为 running（中断态）
                    if not task.get("status"):
                        task["status"] = "running"
                    task.setdefault("completed_steps", [])
                    return task
                except (json.JSONDecodeError, OSError) as e:
                    raise RuntimeError(
                        f"state.json 损坏，无法恢复任务 {task_id}: {e}"
                    )

        raise FileNotFoundError(f"未找到任务 {task_id} 的 state.json")

    def rollback_to_step(self, task_id: str, target_step: str):
        """将 hub 任务回退到指定步骤，删除该步骤及之后的产出，重置 completed_steps"""
        # 1. 加载任务（复用已有的异常处理：FileNotFoundError / RuntimeError）
        task = self._load_task_for_resume(task_id)

        # 2. 获取完整步骤列表（通过 task 中保存的 module_id + workflow_id 还原）
        manifest, module_dir = self._load_and_validate_manifest(task["module_id"])
        _, steps = self._resolve_workflow(manifest, task["workflow_id"])

        # 3. 查找目标步骤下标
        step_names = [s["step"] for s in steps]
        if target_step not in step_names:
            raise ValueError(
                f"步骤 '{target_step}' 不存在，可用步骤：{', '.join(step_names)}"
            )
        target_index = step_names.index(target_step)

        # 4. 从 target_index 开始，逐步删除产出 + 清理 completed_steps
        task_dir = Path(task["task_dir"])
        removed_files = []
        for idx in range(target_index, len(steps)):
            step = steps[idx]
            s_name = step["step"]
            # output 文件名：与 _execute_steps 相同的默认值逻辑
            output_file = step.get("output", f"{idx+1:02d}-{s_name}.md")
            output_path = task_dir / "outputs" / output_file
            if output_path.exists():
                output_path.unlink(missing_ok=True)
                removed_files.append(output_file)
            # 从 completed_steps 移除（防御性：不在列表中也不报错）
            if s_name in task.get("completed_steps", []):
                task["completed_steps"].remove(s_name)

        # 5. 更新状态并持久化
        task["status"] = "paused"
        self._save_task_state(task)

        # 6. 输出用户提示
        target_desc = steps[target_index].get("description", target_step)
        removed_hint = f"（{', '.join(removed_files)} 已删除）" if removed_files else ""
        self.ui.print_info(
            f"\n↩ 已回退到步骤 \"{target_desc}\"{removed_hint}\n"
            f"   执行 opus --resume --task-id {task_id} 从此步骤重新执行"
        )

    def _resolve_role_config(self, manifest: dict, module_id: str, role_name: str) -> dict:
        """合并 manifest 默认值和用户配置覆盖"""
        manifest_role = manifest["roles"][role_name]

        # 用户模型覆盖：命名空间形式 "contract_service.legal_analyst"
        namespaced_role = f"{module_id}.{role_name}"
        overrides = self.config.get("model_overrides", {}) or {}
        user_model = overrides.get(namespaced_role) or overrides.get(role_name)
        role_reasoning = self.config.get("role_reasoning_efforts", {}) or {}
        reasoning_effort = (
            role_reasoning.get(namespaced_role)
            or role_reasoning.get(role_name)
            or "inherit"
        )
        if reasoning_effort not in VALID_REASONING_EFFORTS:
            reasoning_effort = "inherit"
        default_model = manifest_role.get("model", "sonnet")
        if module_id == "interaction_design":
            default_model = MAIN_SESSION_MODEL_ID

        # 用户工具覆盖
        user_tools = (self.config.get("hub_tool_overrides", {})
                      .get(module_id, {})
                      .get(role_name))

        return {
            "model": user_model or default_model,
            "default_model": default_model,
            "reasoning_effort": reasoning_effort,
            "tools": user_tools or manifest_role.get("tools", ["Read"]),
            "template": manifest_role["template"],
            "memory_scope": manifest_role.get("memory_scope", role_name),
        }

    def _record_step_runtime(
        self,
        task: dict,
        step_name: str,
        role_name: str,
        role_config: dict,
        *,
        status: str,
        selected_model: str = "",
        error: str = "",
    ) -> None:
        runtime = dict(task.get("runtime") or {})
        subagents = dict(runtime.get("subagents") or {})
        record = dict(subagents.get(step_name) or {})
        record.update({
            "role": role_name,
            "requested_model": role_config.get("model", ""),
            "default_model": role_config.get("default_model", ""),
            "reasoning_effort": role_config.get("reasoning_effort", "inherit"),
            "status": status,
            "updated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        })
        if selected_model:
            record["selected_model"] = selected_model
        if error:
            record["last_error"] = error
        subagents[step_name] = record
        runtime["subagents"] = subagents
        task["runtime"] = runtime

    def _record_step_runtime_metadata(self, task: dict, step_name: str, metadata: dict) -> None:
        runtime = dict(task.get("runtime") or {})
        subagents = dict(runtime.get("subagents") or {})
        record = dict(subagents.get(step_name) or {})
        record.update({
            "runtime_family": str(metadata.get("runtime_family") or ""),
            "adapter_key": str(metadata.get("adapter_key") or ""),
            "native_session_id": str(metadata.get("native_session_id") or ""),
            "resume_token": str(metadata.get("resume_token") or ""),
            "resume_allowed": bool(metadata.get("resume_allowed")),
            "event_log_path": str(metadata.get("event_log_path") or ""),
            "raw_event_log_path": str(metadata.get("raw_event_log_path") or ""),
            "runtime_status": str(metadata.get("status") or ""),
            "runtime_diagnostics": list(metadata.get("diagnostics") or []),
        })
        subagents[step_name] = record
        runtime["subagents"] = subagents
        task["runtime"] = runtime

    def _build_role_memory_instructions(self, project_path: str, memory_scope: str = "") -> str:
        """为 AgentHub 角色生成项目记忆读取提示。"""
        whitelist = load_project_memory_whitelist(project_path)
        if not whitelist:
            return ""
        required = resolve_required_memories(whitelist, memory_scope)
        if not required:
            return ""
        lines = [
            "# 项目记忆启动要求",
            "如果当前运行时可用 Serena，请先激活当前项目并读取项目级白名单。",
            "- 先读：startup/role-memory-whitelist",
            f"- 本角色至少补读：{', '.join(required)}",
            "- 然后再按当前步骤补读其他相关 memory。",
        ]
        return "\n".join(lines)

    def _check_control_signal(self, task: dict):
        """检查控制信号，必要时抛出 PauseSignal 或 TerminateSignal"""
        from lib.control_signals import read_signal

        signal = read_signal(task["id"])
        if signal is None:
            return

        action = signal.get("action")

        if action == "terminate":
            logger.info(f"收到终止信号，任务 {task['id']}")
            task["status"] = "terminated"
            self._save_task_state(task)
            raise TerminateSignal("用户主动终止")

        if action == "pause":
            # 再检查一次是否有 terminate 覆盖（terminate 优先）
            term_signal = read_signal(task["id"])
            if term_signal and term_signal.get("action") == "terminate":
                logger.info(f"收到终止信号（覆盖暂停），任务 {task['id']}")
                task["status"] = "terminated"
                self._save_task_state(task)
                raise TerminateSignal("用户主动终止（覆盖暂停）")

            logger.info(f"收到暂停信号，任务 {task['id']}")
            task["status"] = "paused"
            self._save_task_state(task)
            raise PauseSignal("用户主动暂停")

    async def _execute_steps(self, task: dict, steps: list, manifest: dict, module_dir: Path):
        """顺序执行工作流步骤"""
        task_dir = Path(task["task_dir"])
        total = len(steps)
        runtime_controller = SubAgentRuntimeController(
            self.agent,
            project_root=task.get("project_path") or ".",
        )

        # 读取项目知识
        knowledge_text = self._inject_project_knowledge(
            task["project_path"],
            project_type=task.get("project_type", "dev"),
            module_id=task.get("module_id", ""),
        )

        for i, step in enumerate(steps):
            step_name = step["step"]

            # 控制信号检查（每步骤前）
            self._check_control_signal(task)

            # 预算检查（每步骤前）
            self._check_budget(task)

            # resume 跳过已完成步骤
            if step_name in task.get("completed_steps", []):
                logger.info(f"跳过已完成步骤: {step_name}")
                continue

            role_name = step["role"]
            output_file = step.get("output", f"{i+1:02d}-{step_name}.md")

            # 进度展示
            self.ui.print_info(f"\n📋 步骤 {i+1}/{total}：{step.get('description', step_name)} ({role_name})")

            # 合并角色配置
            role_config = self._resolve_role_config(manifest, task["module_id"], role_name)
            self._set_current_step(task, step, status="running")
            self._record_step_runtime(
                task,
                step_name,
                role_name,
                role_config,
                status="running",
            )
            self._save_task_state(task)
            memory_instructions = self._build_role_memory_instructions(
                task["project_path"],
                str(role_config.get("memory_scope") or ""),
            )

            # 构建输入文档
            input_docs = self._build_step_input_docs(
                task,
                i,
                steps,
                module_dir=module_dir,
                step=step,
            )

            # 读取角色模板
            template_path = module_dir / role_config["template"]
            role_template = template_path.read_text(encoding="utf-8")

            # 构建完整 prompt
            prompt = self._build_step_prompt(
                role_template,
                knowledge_text,
                input_docs,
                task_dir,
                output_file,
                step=step,
                step_index=i + 1,
                total_steps=total,
                workflow_id=str(task.get("workflow_id") or ""),
                memory_instructions=memory_instructions,
                feedback_notes=task.get("feedback_notes"),
            )

            # 输出文件完整路径
            output_path = task_dir / "outputs" / output_file

            # 调用 AgentRunner.run()（prompt_override 直接传入完整 prompt，绕过 _build_prompt）
            heartbeat_task = None
            async def _heartbeat_current_step():
                while True:
                    await asyncio.sleep(AGENTHUB_STATE_HEARTBEAT_INTERVAL)
                    self._set_current_step(task, step, status="running")
                    self._save_task_state(task)

            try:
                heartbeat_task = asyncio.create_task(_heartbeat_current_step())
                result = await runtime_controller.run(
                    task=task,
                    step_name=step_name,
                    config=self.config,
                    role=role_name,
                    task_dir=str(task_dir),
                    input_docs={},
                    output_file=str(output_path),
                    cwd=task["project_path"],
                    task_id=task.get("id", ""),
                    model_override=role_config["model"],
                    reasoning_effort_override=role_config.get("reasoning_effort"),
                    tools_override=role_config["tools"],
                    prompt_override=prompt,
                )
                if heartbeat_task:
                    heartbeat_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat_task
                self._record_step_runtime(
                    task,
                    step_name,
                    role_name,
                    role_config,
                    status="completed",
                    selected_model=str(getattr(result, "model", "") or ""),
                )
                runtime_metadata = getattr(result, "runtime_metadata", None)
                if isinstance(runtime_metadata, dict):
                    self._record_step_runtime_metadata(task, step_name, runtime_metadata)
            except Exception as exc:
                if heartbeat_task:
                    heartbeat_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat_task
                self._set_current_step(task, step, status="failed")
                self._record_step_runtime(
                    task,
                    step_name,
                    role_name,
                    role_config,
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
                runtime_metadata = getattr(exc, "runtime_metadata", None)
                if isinstance(runtime_metadata, dict):
                    self._record_step_runtime_metadata(task, step_name, runtime_metadata)
                self._save_task_state(task)
                raise

            # 检查产出文件
            if output_path.exists() and output_path.stat().st_size > 0:
                self.ui.print_info(f"   ✓ 完成 → outputs/{output_file}")
            else:
                logger.warning(f"步骤 {step_name} 产出文件为空: {output_path}")
                self.ui.print_warning("   ⚠ 步骤完成但产出文件为空")

            # 关键节点确认（confirm: true 步骤）
            await self._handle_step_confirm(task, step, steps, output_path)

            # 标记步骤完成
            task["completed_steps"].append(step_name)
            self._set_current_step(task, step, status="completed")
            self._save_task_state(task)

    async def _handle_step_confirm(self, task: dict, step: dict, steps: list,
                                    output_path: Path):
        """步骤完成后的确认检查，confirm:true 步骤触发用户确认"""
        if not step.get("confirm", False):
            return


        # 生成预览链接（失败不阻塞）
        preview_link = ""
        try:
            preview_link = await self.ui._upload_preview(output_path)
        except Exception as e:
            logger.warning(f"预览链接生成失败: {e}")

        # 计算累计费用
        task_dir = Path(task["task_dir"])
        total_cost = 0.0
        cost_file = task_dir / "cost.json"
        if cost_file.exists():
            try:
                costs = json.loads(cost_file.read_text(encoding="utf-8"))
                if isinstance(costs, list):
                    total_cost = sum(c.get("cost_usd", 0) for c in costs)
            except Exception:
                pass

        # 构建 ConfirmContext
        completed = task.get("completed_steps", [])
        button_set = str(step.get("button_set") or "doc_review").strip() or "doc_review"
        confirm_ctx = ConfirmContext(
            task_name=task.get("task_name") or summarize_task_title(task.get("description", ""), fallback=task["id"]),
            task_id=task["id"],
            role=step["role"],
            role_display=step.get("description", step["role"]),
            step=step["step"],
            step_display=step.get("description", step["step"]),
            completed_steps=[s.get("description", s["step"])
                             for s in steps if s["step"] in completed],
            total_steps=len(steps),
            total_cost_usd=total_cost,
            file_path=str(output_path),
            button_set=button_set,
        )

        # 确认消息
        message = str(step.get("confirm_message") or "").strip()
        if not message:
            message = (
                f"📋 步骤完成：{step.get('description', step['step'])}\n"
                f"   请确认产出文件后继续"
            )
        if preview_link:
            message += f"\n   📎 预览：{preview_link}"

        # 调用确认（复用已有的 confirm_with_feedback 全通道能力）
        action = await self.ui.confirm_with_feedback(
            message=message,
            file=output_path,
            context=confirm_ctx,
        )

        # 处理结果
        if action in ("confirm", "y"):
            logger.info(f"步骤 {step['step']} 确认通过")
            return

        if action == "feedback":
            feedback = await self.ui.get_user_feedback()
            if feedback:
                if "feedback_notes" not in task:
                    task["feedback_notes"] = []
                task["feedback_notes"].append(feedback)
                self._save_task_state(task)
                logger.info(f"步骤 {step['step']} 收到修改意见: {feedback[:100]}")
                self.ui.print_info("   📝 修改意见已记录，将在后续步骤中体现")
            return

        if action in ("cancel", "n"):
            logger.info(f"步骤 {step['step']} 用户取消任务")
            task["status"] = "cancelled"
            self._save_task_state(task)
            raise TerminateSignal("用户取消任务")

    def _load_step_extra_inputs(self, module_dir: Path, step: dict) -> dict:
        """读取步骤声明的模块静态输入文件。"""
        input_docs = {}
        for item in step.get("extra_inputs", []) or []:
            if isinstance(item, str):
                relative_path = item.strip()
                alias = Path(relative_path).stem
            else:
                relative_path = str(item.get("path") or "").strip()
                alias = str(item.get("name") or Path(relative_path).stem).strip()
            if not relative_path or not alias:
                continue
            path = module_dir / relative_path
            try:
                input_docs[alias] = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                logger.warning(f"跳过非文本静态输入: {relative_path}")
            except OSError as error:
                logger.warning(f"读取静态输入失败 {relative_path}: {error}")
        return input_docs

    def _build_step_input_docs(self, task: dict, current_step_index: int,
                                workflow_steps: list, module_dir: Path | None = None,
                                step: dict | None = None) -> dict:
        """自动累积：用户请求 + 用户文件 + 前序产出"""
        task_dir = Path(task["task_dir"])
        input_docs = {"user_request": task.get("original_request") or task["description"]}

        if module_dir is not None and step:
            input_docs.update(self._load_step_extra_inputs(module_dir, step))

        # 用户上传文件
        inputs_dir = task_dir / "inputs"
        if inputs_dir.exists():
            for f in sorted(inputs_dir.iterdir()):
                if f.is_file():
                    try:
                        input_docs[f"user_file_{f.name}"] = f.read_text(encoding="utf-8")
                    except UnicodeDecodeError:
                        logger.warning(f"跳过非文本文件: {f.name}")

        # 累积前序步骤产出
        for step in workflow_steps[:current_step_index]:
            output_file = step.get("output", "")
            if output_file:
                output_path = task_dir / "outputs" / output_file
                if output_path.exists():
                    content = output_path.read_text(encoding="utf-8")
                    if content.strip():
                        input_docs[step["step"]] = content

        return input_docs

    def _build_step_prompt(self, role_template: str, knowledge_text: str,
                            input_docs: dict, task_dir: Path, output_file: str,
                            step: dict | None = None, step_index: int = 0,
                            total_steps: int = 0, workflow_id: str = "",
                            memory_instructions: str = "",
                            feedback_notes: list = None) -> str:
        """构建步骤的完整 prompt"""
        # 输入文档部分
        docs_text = ""
        for doc_name, doc_content in input_docs.items():
            docs_text += f"\n## {doc_name}\n{doc_content}\n"

        # 用户修改意见注入
        feedback_text = ""
        if feedback_notes:
            items = "\n".join(f"{i+1}. {note}" for i, note in enumerate(feedback_notes))
            feedback_text = (
                "\n## 用户修改意见\n"
                "以下是用户在前序步骤提供的修改意见，请在本步骤中充分考虑并体现：\n\n"
                f"{items}\n"
            )

        step_name = str((step or {}).get("step") or "")
        step_desc = str((step or {}).get("description") or step_name or "未命名步骤")
        prompt_hint = str((step or {}).get("prompt_hint") or "").strip()
        step_section_lines = [
            "# 当前步骤",
            f"- 工作流：{workflow_id or '未命名工作流'}",
            f"- 步骤序号：{step_index}/{total_steps or '?'}",
            f"- step_id：{step_name or 'unknown'}",
            f"- 步骤目标：{step_desc}",
        ]
        if prompt_hint:
            step_section_lines.append(f"- 附加要求：{prompt_hint}")
        step_section = "\n".join(step_section_lines)

        output_target = task_dir / "outputs" / output_file
        output_requirement = "优先输出结构化 Markdown，标题、小节和列表清晰。"
        if output_target.suffix.lower() in {".html", ".htm"}:
            output_requirement = (
                "输出必须是完整、自包含的 HTML 文件；直接写可预览页面，不要再包在 Markdown 代码块里。"
            )
        elif output_target.suffix.lower() == ".json":
            output_requirement = (
                "输出必须是合法 JSON；直接写 JSON 对象，不要包在 Markdown 代码块里，也不要添加解释性前后文。"
            )

        # 输出要求
        output_instruction = f"""
# 输出要求
1. 将工作成果写入文件：{output_target}
2. 完成后，在最终回复中输出简要总结
3. {output_requirement}
"""

        memory_section = memory_instructions or "# 项目记忆启动要求\n（当前项目未声明额外白名单）"

        # 组装（feedback_text 紧跟 docs_text 之后）
        return (
            f"{role_template}\n\n---\n\n"
            f"{memory_section}\n\n---\n\n"
            f"# 项目知识\n{knowledge_text or '（无项目知识）'}\n\n---\n\n"
            f"{step_section}\n\n---\n\n"
            f"# 本次任务的输入文档\n{docs_text}{feedback_text}\n\n---\n\n"
            f"{output_instruction}"
        )

    @staticmethod
    def _compact_runtime_neutral_doc(content: str, *, max_lines: int = 12, max_chars: int = 1200) -> str:
        lines = [line.rstrip() for line in str(content or "").splitlines() if line.strip()]
        excerpt = "\n".join(lines[:max_lines]).strip()
        if len(excerpt) > max_chars:
            excerpt = excerpt[:max_chars].rstrip() + "..."
        elif len(lines) > max_lines:
            excerpt = excerpt + "\n..."
        return excerpt

    def _inject_runtime_neutral_stage_docs(self, project_path: str) -> list[str]:
        """Compatibility stub: formal AgentHub tasks no longer inject temporary docs."""
        return []

    def _inject_project_knowledge(self, project_path: str, project_type: str = "dev", module_id: str = "") -> str:
        """读取项目的 Serena 记忆，构建知识文本"""
        memories_dir = Path(project_path) / ".serena" / "memories"

        # dev 项目 + 指定 module_id → 读取命名空间子目录
        if project_type == "dev" and module_id:
            memories_dir = memories_dir / "hub" / module_id

        if not memories_dir.exists():
            return ""

        knowledge_parts = []
        for md_file in sorted(memories_dir.glob("*.md")):
            try:
                content = md_file.read_text(encoding="utf-8").strip()
                if content:
                    knowledge_parts.append(f"### {md_file.stem}\n{content}")
            except Exception as e:
                logger.warning(f"读取知识文件失败 {md_file}: {e}")
        return "\n\n".join(knowledge_parts)


    def _calc_total_cost(self, task: dict) -> float:
        """从 cost.json 汇总总费用"""
        task_dir = Path(task["task_dir"])
        cost_file = task_dir / "cost.json"
        if not cost_file.exists():
            return 0.0
        try:
            costs = json.loads(cost_file.read_text(encoding="utf-8"))
            if isinstance(costs, list):
                return sum(c.get("cost_usd", 0) for c in costs)
        except Exception:
            pass
        return 0.0

    async def _finalize_task(self, task: dict):
        """汇总产出、通知用户"""
        import subprocess

        task_dir = Path(task["task_dir"])
        outputs_dir = task_dir / "outputs"

        # 收集产出文件
        output_files = sorted(outputs_dir.glob("*")) if outputs_dir.exists() else []
        output_names = [f.name for f in output_files if f.is_file() and not f.name.startswith("_")]

        # 按角色汇总步骤费用
        cost_file = task_dir / "cost.json"
        total_cost = 0.0
        step_costs = {}
        if cost_file.exists():
            try:
                costs = json.loads(cost_file.read_text(encoding="utf-8"))
                if isinstance(costs, list):
                    for c in costs:
                        role = c.get("role", "unknown")
                        cost_val = c.get("cost_usd", 0)
                        step_costs[role] = step_costs.get(role, 0) + cost_val
                        total_cost += cost_val
            except Exception:
                pass

        # 生成预览链接（合并所有产出为 HTML）
        preview_url = ""
        html_outputs = [
            f for f in output_files
            if f.is_file() and not f.name.startswith("_") and f.suffix.lower() in {".html", ".htm"}
        ]
        if html_outputs:
            try:
                result = subprocess.run(
                    ["python3", str(Path(__file__).parent / "lib" / "preview_server.py"),
                    "create", f"AgentHub: {task.get('task_name') or task['description'][:50]}", "-f", str(html_outputs[-1])],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0:
                    for line in result.stdout.strip().split("\n"):
                        if "http" in line:
                            preview_url = line.strip()
                            break
            except Exception as e:
                logger.warning(f"生成 HTML 预览链接失败: {e}")
        elif output_files:
            try:
                combined = f"# {task.get('task_name') or task['description']}\n\n"
                for f in output_files:
                    if f.is_file() and not f.name.startswith("_"):
                        combined += f"---\n\n## {f.name}\n\n{f.read_text(encoding='utf-8')}\n\n"

                html_path = task_dir / "outputs" / "_preview.html"
                html_path.write_text(combined, encoding="utf-8")

                result = subprocess.run(
                    ["python3", str(Path(__file__).parent / "lib" / "preview_server.py"),
                    "create", f"AgentHub: {task.get('task_name') or task['description'][:50]}", "-f", str(html_path)],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0:
                    for line in result.stdout.strip().split("\n"):
                        if "http" in line:
                            preview_url = line.strip()
                            break
            except Exception as e:
                logger.warning(f"生成预览链接失败: {e}")

        # 更新状态
        task["status"] = "completed"
        task["completed_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        self._save_task_state(task)

        # 知识提取（异常不阻塞主任务）
        await self._trigger_knowledge_extraction(task)

        # 输出完成摘要
        self.ui.print_info("\n🎉 任务完成！")
        self.ui.print_info(f"   产出文件：{len(output_names)} 个")
        for name in output_names:
            self.ui.print_info(f"     · outputs/{name}")
        if preview_url:
            self.ui.print_info(f"   预览链接：{preview_url}")
        if step_costs:
            self.ui.print_info("   步骤费用：")
            for role, cost in step_costs.items():
                self.ui.print_info(f"     · {role}: ${cost:.2f}")
        if total_cost > 0:
            self.ui.print_info(f"   总费用: ${total_cost:.2f}")

        # 写入历史记录
        self._write_hub_history(task)

    def _check_budget(self, task: dict):
        """检查预算上限，超出则暂停任务"""
        budget = self.config.get("hub_budget_usd")
        if not budget:
            return

        current_cost = self._calc_total_cost(task)
        if current_cost > float(budget):
            task["status"] = "paused"
            self._save_task_state(task)
            self.ui.print_warning(
                f"\n⚠ 累计费用 ${current_cost:.2f} 已超出预算上限 ${budget}，任务已暂停。\n"
                f"  如需继续，请调整 config.json 中的 hub_budget_usd 后执行：\n"
                f"  opus --resume --task-id {task['id']}"
            )
            raise PauseSignal("预算超限暂停")

    def _write_hub_history(self, task: dict):
        """追加 hub-history.jsonl 记录"""
        try:
            project_path = task.get("project_path", ".")
            history_file = write_data_path(
                "tasks",
                "hub-history.jsonl",
                project_root=project_path,
            )
            history_file.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "task_id": task["id"],
                "task_name": task.get("task_name", ""),
                "module_id": task.get("module_id", ""),
                "workflow_id": task.get("workflow_id", ""),
                "description": task.get("description", ""),
                "status": task.get("status", ""),
                "started_at": task.get("created_at", ""),
                "completed_at": task.get("completed_at", ""),
                "cost_usd": self._calc_total_cost(task),
                "steps_completed": len(task.get("completed_steps", [])),
            }
            with open(history_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"写入 hub-history.jsonl 失败: {e}")

    async def _trigger_knowledge_extraction(self, task: dict):
        """检查触发条件，按需调用 knowledge_extractor 提取领域知识"""
        try:
            project_path = task.get("project_path", ".")
            project_type = task.get("project_type", "dev")
            module_id = task.get("module_id", "")

            # 计算 memories 写入路径（与 _inject_project_knowledge 路径规则一致）
            if project_type == "dev" and module_id:
                memories_output_dir = Path(project_path) / ".serena" / "memories" / "hub" / module_id
            else:
                memories_output_dir = Path(project_path) / ".serena" / "memories"

            # === 触发条件检查（任意满足即触发）===
            existing_md_count = len(list(memories_output_dir.glob("*.md"))) if memories_output_dir.exists() else 0
            cond1 = existing_md_count < 3

            completed_count = self._count_completed_hub_tasks(project_path, module_id)
            cond2 = completed_count < 3
            cond3 = completed_count > 0 and completed_count % 10 == 0

            if not (cond1 or cond2 or cond3):
                return

            # === 触发知识提取 ===
            logger.info(f"触发知识提取: cond1={cond1} cond2={cond2} cond3={cond3}, "
                        f"module={module_id}, memories_dir={memories_output_dir}")
            self.ui.print_info("📚 正在提取知识...")

            memories_output_dir.mkdir(parents=True, exist_ok=True)

            # 读取角色模板
            role_template_path = Path(__file__).parent / "role_templates" / "knowledge_extractor.md"
            role_template = role_template_path.read_text(encoding="utf-8")

            # 收集产出文件内容
            task_dir = Path(task["task_dir"])
            outputs_dir = task_dir / "outputs"
            outputs_text = ""
            if outputs_dir.exists():
                for f in sorted(outputs_dir.iterdir()):
                    if f.is_file() and not f.name.startswith("_"):
                        try:
                            content = f.read_text(encoding="utf-8")
                            outputs_text += f"\n## {f.name}\n{content}\n"
                        except Exception:
                            pass

            # feedback_notes（如有）
            feedback_text = ""
            feedback_notes = task.get("feedback_notes", [])
            if feedback_notes:
                items = "\n".join(f"- {note}" for note in feedback_notes)
                feedback_text = f"\n## 用户修改意见记录\n{items}\n"

            # 组装完整 prompt
            prompt = (
                f"{role_template}\n\n---\n\n"
                f"# 任务产出文件\n{outputs_text}{feedback_text}\n\n---\n\n"
                f"# 输出要求\n"
                f"你需要将知识写入以下目录：{memories_output_dir}\n"
                f"请阅读该目录下已有的 .md 文件（如有），做增量更新。\n"
                f"如目录下没有文件，创建新文件。\n"
            )

            runtime_controller = SubAgentRuntimeController(
                self.agent,
                project_root=task.get("project_path") or ".",
            )
            await runtime_controller.run(
                task=task,
                step_name="knowledge_extraction",
                config=self.config,
                role="knowledge_extractor",
                task_dir=str(task_dir),
                input_docs={},
                cwd=task["project_path"],
                task_id=task.get("id", ""),
                model_override=MAIN_SESSION_MODEL_ID,
                tools_override=["Read", "Write"],
                prompt_override=prompt,
            )

            logger.info("知识提取完成")

        except Exception as e:
            logger.warning(f"知识提取异常（不影响主任务）: {e}")

    def _count_completed_hub_tasks(self, project_path: str, module_id: str) -> int:
        """统计指定项目中该模块已完成的 hub 任务数"""
        count = 0
        for task_dir in iter_task_dirs(project_path):
            if not task_dir.name.startswith("hub-"):
                continue
            state_file = task_dir / "state.json"
            if not state_file.exists():
                continue
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
                if (state.get("module_id") == module_id and
                    state.get("status") == "completed"):
                    count += 1
            except Exception:
                continue

        return count
