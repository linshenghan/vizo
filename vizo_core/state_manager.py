#!/usr/bin/env python3
"""状态管理器：任务持久化、Git 分支管理、失败回滚"""

import fnmatch
import os
import shlex
import json
import logging
import random
import re
import string
import subprocess
import time
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from lib.paths import (
    CONFIRMS_DIR,
    iter_storage_dirs,
    iter_task_dirs,
    read_data_path,
    task_dir as resolve_task_dir,
    write_data_path,
)
from lib.task_resource_guard import assert_task_creation_allowed

logger = logging.getLogger(__name__)


class GitMergeConflict(Exception):
    pass


class WorkflowError(Exception):
    pass


def check_task_code_changes(state: dict, project_path: str = None) -> bool:
    """基于 git diff 检测任务是否有实际代码变更（供 confirm_server / web_console 等外部模块调用）

    Args:
        state: 任务的 state.json 内容（dict）
        project_path: 项目根目录路径（如不提供，从 config.json 解析）
    Returns:
        True 表示有实际代码变更
    """
    restore_point = state.get("restore_point")
    if not restore_point:
        return False
    # 快速判断：无已完成步骤且无检查点 → 一定没有代码变更
    if not (state.get("completed_steps") or state.get("step_checkpoints")):
        return False
    # 解析项目路径
    if not project_path:
        project_name = state.get("project", "")
        if project_name:
            try:
                _root = Path(__file__).resolve().parents[1]
                cfg = json.loads((_root / "config.json").read_text("utf-8"))
                project_path = cfg.get("projects", {}).get(project_name, {}).get("path", ".")
            except Exception:
                return False
        else:
            return False
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", restore_point, "HEAD"],
            cwd=str(project_path),
            capture_output=True, text=True, timeout=10,
        )
        return bool(result.returncode == 0 and result.stdout.strip())
    except Exception:
        return False


@dataclass
class Task:
    """任务数据"""
    id: str = ""
    description: str = ""
    task_name: str = ""              # 简短任务标题（从 RA 输出提取）
    dir: Path = None
    project: str = ""
    status: str = "pending"          # pending, running, completed, failed, paused, rolled_back
    task_type: str = ""              # new_feature, bug_fix, refactor, debug_embedded, non_dev
    scale: str = "normal"            # normal, large
    current_step: str = ""
    completed_steps: list = field(default_factory=list)
    sub_tasks: list = field(default_factory=list)              # 子任务定义列表
    completed_sub_tasks: list = field(default_factory=list)    # 已完成子任务 id
    step_checkpoints: dict = field(default_factory=dict)       # {step_name: commit_hash}
    modified_files: dict = field(default_factory=dict)         # {step_name: [file_paths]} 精确回滚用
    restore_point: str = None        # Git tag 名
    created_at: datetime = None
    completed_at: datetime = None
    pending_confirm: dict = field(default_factory=dict)  # 待确认请求信息
    metadata: dict = field(default_factory=dict)         # 扩展元数据（workflow 等）
    rollback_info: dict = field(default_factory=dict)    # 回滚恢复信息（snapshot_branch, recovery_cmd 等）

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        return cls(
            id=d["id"],
            description=d["description"],
            task_name=d.get("task_name", ""),
            dir=Path(d["dir"]) if d.get("dir") else resolve_task_dir(d["id"]),
            project=d.get("project", ""),
            status=d.get("status", "pending"),
            task_type=d.get("task_type", ""),
            scale=d.get("scale", "normal"),
            current_step=d.get("current_step", ""),
            completed_steps=d.get("completed_steps", []),
            sub_tasks=d.get("sub_tasks", []),
            completed_sub_tasks=d.get("completed_sub_tasks", []),
            step_checkpoints=d.get("step_checkpoints", {}),
            modified_files=d.get("modified_files", {}),
            restore_point=d.get("restore_point"),
            created_at=datetime.fromisoformat(d["created_at"]) if d.get("created_at") else None,
            completed_at=datetime.fromisoformat(d["completed_at"]) if d.get("completed_at") else None,
            pending_confirm=d.get("pending_confirm", {}),
            metadata=d.get("metadata", {}),
            rollback_info=d.get("rollback_info", {}),
        )

    @property
    def duration(self) -> timedelta | None:
        """任务耗时（从创建到完成，或从创建到现在）"""
        if not self.created_at:
            return None
        end = self.completed_at or datetime.now()
        # 统一为 naive datetime 避免 offset-naive/aware 混合
        if hasattr(self.created_at, 'tzinfo') and self.created_at.tzinfo:
            start = self.created_at.replace(tzinfo=None)
        else:
            start = self.created_at
        if hasattr(end, 'tzinfo') and end.tzinfo:
            end = end.replace(tzinfo=None)
        return end - start

    @property
    def duration_str(self) -> str:
        """任务耗时的可读字符串"""
        d = self.duration
        if not d:
            return "未知"
        total_seconds = int(d.total_seconds())
        if total_seconds < 60:
            return f"{total_seconds}秒"
        minutes = total_seconds // 60
        if minutes < 60:
            return f"{minutes}分{total_seconds % 60}秒"
        hours = minutes // 60
        return f"{hours}小时{minutes % 60}分"


@dataclass
class WorktreeInfo:
    """Worktree 信息"""
    branch: str = ""
    path: Path = None


class StateManager:
    """状态持久化、Git 分支管理、失败回滚"""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.project_path = Path(
            self.config.get("projects", {})
            .get(self.config.get("default_project", ""), {})
            .get("path", ".")
        )

    # ─── 任务生命周期 ─────────────────────────────────────────


    def _ensure_commit_tag_hook(self):
        """确保 prepare-commit-msg hook 已安装（commit 打标，用于精确回滚）"""
        hook_path = self.project_path / ".git" / "hooks" / "prepare-commit-msg"
        if hook_path.exists():
            return  # 已安装
        try:
            hook_path.parent.mkdir(parents=True, exist_ok=True)
            hook_path.write_text(
                '#!/bin/bash\n'
                '# Opus V6 Commit Tagging Hook — 自动安装\n'
                'COMMIT_MSG_FILE="$1"; COMMIT_SOURCE="$2"\n'
                '[ -z "$OPUS_TASK_ID" ] && exit 0\n'
                '[ "$COMMIT_SOURCE" = "squash" ] || [ "$COMMIT_SOURCE" = "merge" ] && exit 0\n'
                'TAG="[opus:${OPUS_TASK_ID}]"\n'
                'MSG=$(cat "$COMMIT_MSG_FILE")\n'
                'case "$MSG" in *"$TAG"*) exit 0 ;; esac\n'
                'echo "${TAG} ${MSG}" > "$COMMIT_MSG_FILE"\n',
                encoding="utf-8",
            )
            hook_path.chmod(0o755)
            logger.info("已自动安装 prepare-commit-msg hook（commit 打标）")
        except Exception as e:
            logger.warning(f"安装 prepare-commit-msg hook 失败: {e}")

    def create_task(self, description: str) -> Task:
        """创建任务：建目录 + 设 Git restore point + 确保 commit 打标 hook"""
        assert_task_creation_allowed(self.project_path, self.config)
        self._ensure_commit_tag_hook()

        task_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{self._short_hash()}"
        task_dir = resolve_task_dir(
            task_id,
            project_root=self.project_path,
            prefer_existing=False,
        )
        task_dir.mkdir(parents=True, exist_ok=True)

        task = Task(
            id=task_id,
            description=description,
            dir=task_dir,
            project=self.config.get("default_project", ""),
            created_at=datetime.now(),
            status="running",
        )
        self._ensure_runtime_metadata(task)

        task.restore_point = self._create_git_tag(f"opus-restore-{task_id}")
        self._save_state(task)
        return task

    @staticmethod
    def _ensure_runtime_metadata(task: Task) -> dict:
        runtime_meta = task.metadata.setdefault("runtime", {})
        runtime_meta.setdefault("schema_version", 1)
        runtime_meta.setdefault("subagents", {})
        runtime_meta.setdefault("history", [])
        return runtime_meta

    def record_subagent_runtime(self, task: Task, runtime_metadata: dict, step_name: str = ""):
        """记录子代理运行时元数据，作为后续 resume / 审计 / 诊断事实源。"""
        if not runtime_metadata:
            return

        runtime_root = self._ensure_runtime_metadata(task)
        record = dict(runtime_metadata)
        record["recorded_at"] = datetime.now().isoformat()
        record_step = step_name or record.get("step_name") or task.current_step or record.get("role", "unknown")

        runtime_root["subagents"][record_step] = record
        runtime_root["last_subagent"] = record

        history = runtime_root.setdefault("history", [])
        history.append({
            "step_name": record_step,
            "role": record.get("role", ""),
            "runtime_family": record.get("runtime_family", ""),
            "status": record.get("status", ""),
            "selected_model": record.get("selected_model", ""),
            "native_session_id": record.get("native_session_id", ""),
            "recorded_at": record["recorded_at"],
        })
        if len(history) > 50:
            del history[:-50]

        self._save_state(task)

    def update_step(self, task: Task, step: str):
        """标记步骤开始：只设置 current_step，不加入 completed_steps。
        步骤成功后必须调用 complete_step() 才标记为已完成。"""
        task.current_step = step
        self._save_state(task)
        self.update_work_state(task)

    def complete_step(self, task: Task, step: str):
        """标记步骤完成：加入 completed_steps 并持久化。"""
        if step not in task.completed_steps:
            task.completed_steps.append(step)
        self._save_state(task)
        self.update_work_state(task)

    def complete_task(self, task: Task):
        """标记任务完成（同时清除 work_state）"""
        task.status = "completed"
        task.completed_at = datetime.now()
        self._save_state(task)
        if task.restore_point:
            self._delete_git_tag(task.restore_point)
        self.clear_work_state()

    def fail_task(self, task: Task, reason: str = "", sync_parent: bool = False):
        """标记任务失败（可通过 resume 重试）

        Args:
            task: 任务对象
            reason: 失败原因
            sync_parent: 是否同步更新父任务状态为 partially_failed
        """
        task.status = "failed"
        self._save_state(task)
        if reason:
            pause_file = task.dir / "pause_reason.txt"
            pause_file.parent.mkdir(parents=True, exist_ok=True)
            pause_file.write_text(reason, encoding="utf-8")

        # 子任务失败时同步父任务
        if sync_parent and "-sub" in task.id:
            parent_id = task.id.rsplit("-sub", 1)[0]
            parent_dir = task.dir.parent
            parent_state_file = parent_dir / "state.json"
            if parent_state_file.exists():
                try:
                    parent_data = json.loads(parent_state_file.read_text("utf-8"))
                    parent_data["status"] = "partially_failed"
                    parent_state_file.write_text(
                        json.dumps(parent_data, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    logger.info(f"子任务 {task.id} 失败，父任务 {parent_id} 已同步为 partially_failed")
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning(f"同步父任务状态失败: {e}")

    def set_pending_confirm(self, task: Task, request_id: str, req_type: str,
                            summary: str, created_at: str = "",
                            button_set: str = ""):
        """设置待确认请求"""
        task.pending_confirm = {
            "request_id": request_id,
            "type": req_type,
            "summary": summary[:200],
            "created_at": created_at or datetime.now().isoformat(timespec="seconds"),
        }
        if button_set:
            task.pending_confirm["context"] = {"button_set": button_set}
        self._save_state(task)

    def clear_pending_confirm(self, task: Task):
        """清除待确认请求"""
        task.pending_confirm = {}
        self._save_state(task)

    def get_task_cost(self, task: Task) -> dict:
        """统计单个任务的费用（含子任务）

        Returns:
            {"total_usd": float, "total_tokens": int, "by_role": [...]}
        """
        cost_file = task.dir / "cost.json"
        costs = []
        if cost_file.exists():
            try:
                costs = json.loads(cost_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        # 扫描所有子任务 cost.json
        for sub_cost_file in sorted(task.dir.glob("sub-*/cost.json")):
            try:
                sub_costs = json.loads(sub_cost_file.read_text(encoding="utf-8"))
                costs.extend(sub_costs)
            except (json.JSONDecodeError, OSError):
                continue

        total_usd = sum(c.get("cost_usd", 0) for c in costs if c.get("billable", True))
        total_tokens = sum(
            c.get("input_tokens", 0) + c.get("output_tokens", 0) for c in costs
        )
        return {
            "total_usd": round(total_usd, 4),
            "total_tokens": total_tokens,
            "by_role": costs,
        }

    def _save_snapshot_before_rollback(self, task_id: str, cwd: str = None):
        """在 rollback 前保存当前工作状态到快照分支。

        创建分支 opus-snapshot/{task_id}，将所有未提交的修改（含未跟踪文件）
        保存为一个 commit。回滚后可通过以下方式恢复：
          git cherry-pick opus-snapshot/{task_id}
          git checkout opus-snapshot/{task_id} -- .
        """
        work_dir = str(cwd or self.project_path)
        branch_name = f"opus-snapshot/{task_id}"

        try:
            # 检查是否有任何变更（tracked + untracked）
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=work_dir, capture_output=True, text=True, timeout=10,
            )
            if status.returncode != 0 or not status.stdout.strip():
                logger.info(f"快照跳过：无未提交的变更（task={task_id}）")
                return

            # 记录当前分支/HEAD
            head_ref = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=work_dir, capture_output=True, text=True, timeout=10,
            )
            current_head = head_ref.stdout.strip() if head_ref.returncode == 0 else "unknown"

            # 暂存所有变更（含 untracked）
            subprocess.run(
                ["git", "add", "-A"],
                cwd=work_dir, capture_output=True, timeout=10,
            )

            # 创建快照 commit（在当前分支上临时提交）
            subprocess.run(
                ["git", "commit", "-m",
                 f"snapshot: 任务 {task_id} 回滚前快照\n\n"
                 f"Base: {current_head[:8]}\n"
                 f"此快照可用于恢复回滚前的代码状态"],
                cwd=work_dir, capture_output=True, timeout=30,
            )

            # 获取快照 commit hash
            snap_hash = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=work_dir, capture_output=True, text=True, timeout=10,
            )

            # 创建快照分支（指向这个 commit）
            subprocess.run(
                ["git", "branch", "-f", branch_name, "HEAD"],
                cwd=work_dir, capture_output=True, timeout=10,
            )

            snap_short = snap_hash.stdout.strip()[:8] if snap_hash.returncode == 0 else "?"
            logger.info(
                f"快照已保存: {branch_name} ({snap_short})，"
                f"恢复命令: git cherry-pick {branch_name}"
            )
        except Exception as e:
            logger.warning(f"快照保存失败（不影响回滚）: {e}")

    def _safe_revert_to(self, target_ref: str, cwd: str, commit_msg: str,
                         scope_files: list = None) -> bool:
        """安全回滚到指定 ref：逐文件还原 + 新 commit（保留 git 历史）

        与 git reset --hard 的区别：
        - 不销毁 commit 历史，而是创建一个新的 revert commit
        - 如果 target_ref 和 HEAD 无差异，什么都不做
        - 任务新增的文件会被删除，任务修改的文件会还原到 target_ref 版本

        Args:
            target_ref: 目标 Git ref（tag 名、commit hash、分支名）
            cwd: Git 工作目录
            commit_msg: 回滚 commit 的提交信息
            scope_files: 可选，限定只回滚这些文件（精确回滚，防止误回滚其他任务/人的代码）
        Returns:
            True 如果有变更被回滚，False 如果无差异（无需回滚）
        """
        if scope_files is not None:
            # 精确模式：只回滚 scope_files 中在 target_ref..HEAD 之间有差异的文件
            changed_files = []
            for f in scope_files:
                diff_check = subprocess.run(
                    ["git", "diff", "--name-only", target_ref, "HEAD", "--", f],
                    cwd=cwd, capture_output=True, text=True, timeout=10,
                )
                if diff_check.returncode == 0 and diff_check.stdout.strip():
                    changed_files.append(f)
            if not changed_files:
                logger.info(f"scope_files 中无代码差异，跳过回滚 (target={target_ref})")
                return False
        else:
            # 广谱模式（老逻辑）：回滚 target_ref..HEAD 之间所有差异文件
            result = subprocess.run(
                ["git", "diff", "--name-only", target_ref, "HEAD"],
                cwd=cwd, capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0 or not result.stdout.strip():
                logger.info(f"无代码差异，跳过回滚 (target={target_ref})")
                return False
            changed_files = [f for f in result.stdout.strip().split("\n") if f.strip()]
            if not changed_files:
                return False

        # 排除运行时配置文件（非任务产出，不应被回滚）
        ROLLBACK_EXCLUDE = {"config.json", ".env"}
        changed_files = [f for f in changed_files if f not in ROLLBACK_EXCLUDE]
        if not changed_files:
            logger.info("排除运行时配置后无文件需要回滚")
            return False

        # 逐文件还原
        for f in changed_files:
            # 检查文件在 target_ref 中是否存在
            check = subprocess.run(
                ["git", "cat-file", "-e", f"{target_ref}:{f}"],
                cwd=cwd, capture_output=True, timeout=10,
            )
            if check.returncode == 0:
                # 文件在 target_ref 中存在 → 还原到该版本
                subprocess.run(
                    ["git", "checkout", target_ref, "--", f],
                    cwd=cwd, capture_output=True, timeout=10,
                )
            else:
                # 文件在 target_ref 中不存在 → 这是任务新增的文件，删除它
                subprocess.run(
                    ["git", "rm", "-f", "--", f],
                    cwd=cwd, capture_output=True, timeout=10,
                )

        # 暂存所有变更
        subprocess.run(
            ["git", "add", "-A"], cwd=cwd, capture_output=True, timeout=10,
        )

        # 检查是否真的有暂存的改动（防止空 commit）
        status = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=cwd, capture_output=True, timeout=10,
        )
        if status.returncode == 0:
            logger.info(f"暂存区无变更，跳过回滚 commit")
            return False

        # 提交回滚
        subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=cwd, capture_output=True, timeout=30,
        )
        logger.info(f"安全回滚完成: {commit_msg} (文件数: {len(changed_files)})")
        return True


    def _get_task_files_from_tagged_commits(self, task: Task) -> list | None:
        """从 git log 中筛选带 [opus:{task_id}] 标记的 commit，提取修改文件列表。

        Returns:
            list: 任务修改的文件列表（可能为空 = 任务没改文件）
            None: 没有找到 tagged commits（hook 不存在或任务早于 hook 安装）
        """
        if not task.restore_point:
            return None
        cwd = str(self.project_path)
        tag = f"[opus:{task.id}]"

        try:
            # 找出 restore_point..HEAD 之间带标记的 commit
            log_result = subprocess.run(
                ["git", "log", "--format=%H", "--fixed-strings",
                 f"--grep={tag}", f"{task.restore_point}..HEAD"],
                cwd=cwd, capture_output=True, text=True, timeout=30,
            )
            if log_result.returncode != 0:
                logger.debug(f"git log 查询 tagged commits 失败: {log_result.stderr}")
                return None

            commit_hashes = [h.strip() for h in log_result.stdout.strip().split("\n") if h.strip()]
            if not commit_hashes:
                # 范围内没有 tagged commit
                # 区分两种情况：hook 不存在（返回 None fallback）vs 任务真没产出 commit
                # 如果任务有 completed_steps 但没有 tagged commits，说明 hook 当时不存在
                if task.completed_steps:
                    return None  # fallback 到 modified_files
                return []  # 任务确实没产出代码

            # 从每个 tagged commit 提取修改的文件
            all_files = set()
            for commit_hash in commit_hashes:
                diff_result = subprocess.run(
                    ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit_hash],
                    cwd=cwd, capture_output=True, text=True, timeout=10,
                )
                if diff_result.returncode == 0 and diff_result.stdout.strip():
                    files = [f.strip() for f in diff_result.stdout.strip().split("\n") if f.strip()]
                    all_files.update(files)

            logger.info(f"任务 {task.id} 通过 commit 打标找到 {len(commit_hashes)} 个 commit，"
                        f"涉及 {len(all_files)} 个文件")
            return list(all_files)

        except Exception as e:
            logger.warning(f"从 tagged commits 提取文件列表失败: {e}")
            return None

    def _get_task_scope_files(self, task: Task) -> list | None:
        """获取任务实际修改的文件列表（三层 fallback）。

        优先级：
        1. Commit 打标法：从 git log 中筛选含 [opus:{task_id}] 的 commit，提取文件
           → 100% 精确，只含本任务的 commit
        2. modified_files 法：从 task.modified_files 汇总（步骤窗口内的 git diff）
           → 90% 精确，步骤窗口内可能混入并发任务 commit
        3. 返回 None → fallback 到广谱回滚（target_ref..HEAD 全部文件）

        返回空列表表示任务确实没改任何文件（跳过回滚）。
        """
        # Layer 3: Commit 打标法（最精确）
        tagged_files = self._get_task_files_from_tagged_commits(task)
        if tagged_files is not None:
            return tagged_files

        # Layer 2: modified_files 法
        if task.modified_files:
            all_files = set()
            for files in task.modified_files.values():
                all_files.update(files)
            return list(all_files)

        return None  # 无精确数据，走老逻辑（广谱回滚）

    def rollback(self, task: Task) -> bool:
        """安全回滚任务代码（逐文件还原，不销毁 git 历史）

        Returns:
            True 如果有代码被实际回滚，False 如果无代码变更
        """
        actually_rolled_back = False
        if task.restore_point:
            # 如果任务没有产出任何代码（无已完成步骤），跳过回滚
            # 避免误回滚其他任务在 restore_point 之后提交的代码
            if not task.completed_steps and not task.step_checkpoints:
                logger.info(f"任务 {task.id} 无已完成步骤，跳过代码回滚（防止误回滚其他任务代码）")
            else:
                self._save_snapshot_before_rollback(task.id)
                # 优先使用 modified_files 精确回滚，防止误回滚其他人的代码
                scope = self._get_task_scope_files(task)
                reverted = self._safe_revert_to(
                    task.restore_point,
                    str(self.project_path),
                    f"revert: 任务 {task.id} 回滚",
                    scope_files=scope,
                )
                if not reverted:
                    logger.info(f"任务 {task.id} 无代码变更，跳过回滚")
                else:
                    actually_rolled_back = True
        if actually_rolled_back:
            task.status = "rolled_back"
        else:
            task.status = "failed"
        self._save_state(task)
        self.clear_work_state()
        return actually_rolled_back

    def resume_sub_tasks(self, task: Task) -> str:
        """恢复子任务状态：paused → running, failed → pending（待重试）

        Returns:
            消息类型: "retry" 表示有失败子任务需要重试, "normal" 表示正常恢复
        """
        has_failed = False
        for st in task.sub_tasks:
            status = st.get("status", "")
            if status == "paused":
                st["status"] = "running"
            elif status == "failed" or status == "rolled_back":
                st["status"] = "pending"  # 重置为 pending，等待重新执行
                has_failed = True
        # 同步子任务目录的 state.json
        for st in task.sub_tasks:
            sub_dir = task.dir / f"sub-{st.get('id')}"
            sub_state_file = sub_dir / "state.json"
            if sub_state_file.exists():
                try:
                    sub_data = json.loads(sub_state_file.read_text("utf-8"))
                    sub_data["status"] = st.get("status", "running")
                    sub_state_file.write_text(
                        json.dumps(sub_data, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                except (json.JSONDecodeError, OSError):
                    pass
        logger.info(f"子任务状态已恢复（{len(task.sub_tasks)} 个），有失败需重试: {has_failed}")
        return "retry" if has_failed else "normal"

    def rollback_sub_task(self, sub_task: Task):
        """回滚单个子任务到其独立的 restore point（不影响其他子任务的代码）"""
        if sub_task.restore_point:
            self._save_snapshot_before_rollback(sub_task.id)
            scope = self._get_task_scope_files(sub_task)
            reverted = self._safe_revert_to(
                sub_task.restore_point,
                str(self.project_path),
                f"revert: 子任务 {sub_task.id} 回滚",
                scope_files=scope,
            )
            sub_task.status = "rolled_back" if reverted else "failed"
            self._save_state(sub_task)
            logger.info(f"子任务 {sub_task.id} 已回滚到 {sub_task.restore_point}")
        else:
            logger.warning(f"子任务 {sub_task.id} 无 restore_point，无法回滚")

    def find_incomplete_task(self):
        """扫描最新的未完成任务"""
        tasks = self.find_all_incomplete_tasks()
        return tasks[0] if tasks else None

    def find_all_incomplete_tasks(self):
        """扫描所有未完成任务（按时间倒序），兼容不同 schema"""
        result = []
        for task_dir in iter_task_dirs(self.project_path):
            state_file = task_dir / "state.json"
            if state_file.exists():
                try:
                    state = json.loads(state_file.read_text(encoding="utf-8"))
                    status = state.get("status", "")
                    if status in ("running", "paused", "in_progress", "failed", "rolled_back", "partially_failed"):
                        normalized = {
                            "id": state.get("id", state.get("task_id", task_dir.name)),
                            "description": state.get("description", state.get("task_name", "")),
                            "dir": str(task_dir),
                            "project": state.get("project", ""),
                            "status": "running" if status == "in_progress" else status,
                            "task_type": state.get("task_type", ""),
                            "scale": state.get("scale", "normal"),
                            "current_step": state.get("current_step", state.get("phase", "")),
                            "completed_steps": state.get("completed_steps", []),
                            "sub_tasks": state.get("sub_tasks", []),
                            "completed_sub_tasks": state.get("completed_sub_tasks", []),
                            "step_checkpoints": state.get("step_checkpoints", {}),
                            "restore_point": state.get("restore_point"),
                            "created_at": state.get("created_at"),
                            "completed_at": state.get("completed_at"),
                        }
                        result.append(Task.from_dict(normalized))
                except (json.JSONDecodeError, KeyError) as e:
                    logger.warning(f"跳过损坏的任务状态文件: {state_file} ({e})")
                    continue
        return result

    def pause_task(self, task: Task, reason: str = "", save_snapshot: bool = False,
                   step_name: str = "", work_dir: str = "", sync_sub_tasks: bool = False):
        """暂停任务，可选保存 Git diff 快照，可选同步子任务状态"""
        task.status = "paused"
        if reason:
            # 写入暂停原因文件
            pause_file = task.dir / "pause_reason.txt"
            pause_file.parent.mkdir(parents=True, exist_ok=True)
            pause_file.write_text(reason, encoding="utf-8")

        # 同步子任务状态：将所有 running 的子任务也标记为 paused
        if sync_sub_tasks and task.sub_tasks:
            for st in task.sub_tasks:
                if st.get("status") == "running":
                    st["status"] = "paused"
            # 同步子任务目录的 state.json
            for st in task.sub_tasks:
                if st.get("status") == "paused":
                    sub_dir = task.dir / f"sub-{st.get('id')}"
                    sub_state_file = sub_dir / "state.json"
                    if sub_state_file.exists():
                        try:
                            sub_data = json.loads(sub_state_file.read_text("utf-8"))
                            sub_data["status"] = "paused"
                            sub_state_file.write_text(
                                json.dumps(sub_data, ensure_ascii=False, indent=2), encoding="utf-8"
                            )
                        except (json.JSONDecodeError, OSError):
                            pass

        # 保存暂停时的费用快照
        cost = self.get_task_cost(task)
        if cost and cost["total_usd"] > 0:
            pause_meta = task.dir / "pause_meta.json"
            pause_meta.write_text(json.dumps({
                "reason": reason,
                "paused_at": datetime.now().isoformat(),
                "cost_at_pause": cost,
            }, ensure_ascii=False, indent=2), encoding="utf-8")

        self._save_state(task)
        logger.info(f"任务 {task.id} 已暂停: {reason}")

        if save_snapshot and work_dir and step_name:
            self._save_git_snapshot(task, step_name, work_dir)

    def rollback_to_step(self, task: Task, target_step: str, work_dir: str) -> None:
        """回滚到指定步骤的起始状态

        Args:
            task: 任务对象
            target_step: 回滚目标步骤名
            work_dir: 项目工作目录
        """
        # 1. 验证目标步骤有 checkpoint
        commit_hash = task.step_checkpoints.get(target_step)
        if not commit_hash:
            raise ValueError(f"步骤 {target_step} 没有 checkpoint 记录")

        # 2. 验证 commit hash 有效
        verify = subprocess.run(
            ["git", "cat-file", "-t", commit_hash],
            cwd=str(work_dir), capture_output=True, text=True, timeout=10,
        )
        if verify.returncode != 0:
            raise ValueError(f"步骤 {target_step} 的 checkpoint {commit_hash[:8]} 已失效")

        # 3. 保存快照后安全回滚到该 checkpoint
        self._save_snapshot_before_rollback(task.id, cwd=work_dir)
        self._safe_revert_to(
            commit_hash,
            str(work_dir),
            f"revert: 任务 {task.id} 回滚到步骤 {target_step}",
        )

        # 4. 清理 completed_steps：移除目标步骤及其后续步骤
        if target_step in task.completed_steps:
            idx = task.completed_steps.index(target_step)
            removed = task.completed_steps[idx:]
            task.completed_steps = task.completed_steps[:idx]
        else:
            removed = []

        # 5. 清理后续步骤的 checkpoint 和 modified_files
        for step in removed:
            task.step_checkpoints.pop(step, None)
            task.modified_files.pop(step, None)

        # 6. 清理后续步骤的产出文档
        step_to_doc = {
            "requirement_analysis": "00-requirement-analysis.md",
            "pm_prd": "01-prd.md",
            "architect": "02-design.md",
            "design_confirmed": None,
            "qa_test_cases": "03-test-cases.md",
            "parallel_dev": ["04-backend-result.md", "05-frontend-result.md"],
            "integration": "06-integration-report.md",
            "test_fix": "07-test-report-round-*.md",
            "deploy": None,
            "knowledge": "08-knowledge-updates.md",
        }
        for step in removed:
            docs = step_to_doc.get(step)
            if docs:
                if isinstance(docs, str):
                    docs = [docs]
                for doc in docs:
                    if '*' in doc or '?' in doc:
                        # glob 模式：匹配并删除所有符合的文件
                        for matched in task.dir.glob(doc):
                            matched.unlink()
                    else:
                        doc_path = task.dir / doc
                        if doc_path.exists():
                            doc_path.unlink()

        # 6.5 清理锁文件（防止 resume 时检测到残留锁）
        lock_file = task.dir / ".lock"
        if lock_file.exists():
            lock_file.unlink(missing_ok=True)
            logger.info(f"已清理锁文件: {lock_file}")

        # 7. 更新状态
        task.current_step = ""
        task.status = "paused"
        self._save_state(task)
        logger.info(f"任务 {task.id} 已回滚到步骤 {target_step} (commit: {commit_hash[:8]})")

    async def terminate_task(self, task: Task, work_dir: str, rollback: bool = True):
        """安全终止任务：可选保存修改 + 回滚代码 + 清理资源

        Args:
            task: 任务对象
            work_dir: 项目工作目录
            rollback: 是否回滚代码（True=回滚, False=保留代码仅终止）
        """
        # 1. 回滚代码（仅当 rollback=True 且有 restore_point）
        stash_saved = False
        scope_files = None
        if rollback and task.restore_point:
            # 如果任务没有产出任何代码（无已完成步骤），跳过回滚
            # 避免误回滚其他任务在 restore_point 之后提交的代码
            has_code_output = bool(task.completed_steps or task.step_checkpoints)

            if has_code_output:
                # 检查是否有未提交的修改
                status_result = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=str(work_dir), capture_output=True, text=True, timeout=10,
                )
                if status_result.stdout.strip():
                    stash_msg = f"opus-terminate-{task.id}-{int(time.time())}"
                    stash_result = subprocess.run(
                        ["git", "stash", "push", "-m", stash_msg, "--include-untracked"],
                        cwd=str(work_dir), capture_output=True, text=True, timeout=30,
                    )
                    if stash_result.returncode == 0 and "No local changes" not in stash_result.stdout:
                        stash_saved = True
                        logger.info(f"已保存未提交修改到 git stash: {stash_msg}")

                # 优先使用 modified_files 精确回滚，防止误回滚其他人的代码
                scope_files = self._get_task_scope_files(task)
                self._safe_revert_to(
                    task.restore_point,
                    str(work_dir),
                    f"revert: 任务 {task.id} 终止回滚",
                    scope_files=scope_files,
                )

                if stash_saved:
                    logger.info(f"提示：可用 git stash pop 恢复被保存的修改")
            else:
                logger.info(f"任务 {task.id} 无已完成步骤，跳过代码回滚（防止误回滚其他任务代码）")

        # 2. 标记状态
        if rollback:
            task.status = "rolled_back"
            # 记录回滚恢复信息
            task.rollback_info = {
                "snapshot_branch": f"opus-snapshot/{task.id}",
                "recovery_cmd": f"git cherry-pick opus-snapshot/{task.id}",
                "scope_files": scope_files or [],
            }
        else:
            task.status = "terminated"

        task.completed_at = datetime.now()
        self._save_state(task)

        # 3. 清理 confirm 残留
        confirms_dir = CONFIRMS_DIR
        if confirms_dir.exists():
            for f in confirms_dir.glob(f"{task.id}*.request.json"):
                resp_file = f.with_name(f.name.replace(".request.json", ".response.json"))
                if not resp_file.exists():
                    resp_file.write_text(json.dumps({
                        "action": "cancel", "feedback": "任务已终止",
                        "responded_at": time.time(),
                    }), encoding="utf-8")

        # 4. 清理控制信号
        from lib.control_signals import clear_signal
        clear_signal(task.id)

        # 5. 清理 work_state
        self.clear_work_state()

        # 6. 删除 restore point tag
        if task.restore_point:
            self._delete_git_tag(task.restore_point)

        if rollback:
            if stash_saved:
                logger.info(f"任务 {task.id} 已安全终止（未提交修改已 stash，可用 git stash pop 恢复）")
            else:
                logger.info(f"任务 {task.id} 已安全终止（代码已回滚）")
        else:
            logger.info(f"任务 {task.id} 已终止（代码已保留）")

    def _save_git_snapshot(self, task: Task, step_name: str, work_dir: str):
        """保存 Git diff 快照（同步，因为只在暂停时调用一次）"""
        import subprocess
        snapshot_dir = task.dir / "snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        timestamp = int(time.time())

        # 保存当前 HEAD hash 作为 base
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=work_dir, capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                base_file = snapshot_dir / f"{step_name}-base.hash"
                base_file.write_text(result.stdout.strip(), encoding="utf-8")
        except Exception as e:
            logger.warning(f"保存 base hash 失败: {e}")

        # 保存 git diff（含未暂存变更）
        try:
            result = subprocess.run(
                ["git", "diff", "HEAD"],
                cwd=work_dir, capture_output=True, text=True, timeout=30
            )
            if result.stdout:
                diff_file = snapshot_dir / f"{step_name}-{timestamp}.diff"
                diff_file.write_text(result.stdout, encoding="utf-8")
                logger.info(f"已保存 Git 快照: {diff_file}")
        except Exception as e:
            logger.warning(f"保存 git diff 失败: {e}")

    def _save_state(self, task: Task):
        """序列化任务状态到 JSON（原子写入，防崩溃损坏）"""
        task.dir.mkdir(parents=True, exist_ok=True)
        state_file = task.dir / "state.json"
        tmp_file = state_file.with_suffix(".json.tmp")
        data = json.dumps({
            "id": task.id,
            "description": task.description,
            "task_name": task.task_name,
            "dir": str(task.dir),
            "project": task.project,
            "status": task.status,
            "task_type": task.task_type,
            "scale": task.scale,
            "current_step": task.current_step,
            "completed_steps": task.completed_steps,
            "sub_tasks": task.sub_tasks,
            "completed_sub_tasks": task.completed_sub_tasks,
            "step_checkpoints": task.step_checkpoints,
            "modified_files": task.modified_files,
            "restore_point": task.restore_point,
            "pending_confirm": task.pending_confirm,
            "metadata": task.metadata,
            "rollback_info": task.rollback_info,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        }, indent=2, ensure_ascii=False)
        tmp_file.write_text(data, encoding="utf-8")
        os.replace(str(tmp_file), str(state_file))

    def validate_sub_tasks(self, sub_tasks: list) -> list:
        """校验子任务列表格式，返回标准化后的列表。校验失败 raise WorkflowError"""
        if not isinstance(sub_tasks, list):
            raise WorkflowError(f"sub_tasks 必须是列表，实际类型: {type(sub_tasks)}")
        if not sub_tasks:
            raise WorkflowError("sub_tasks 不能为空")
        if len(sub_tasks) > 10:
            raise WorkflowError(f"子任务数量超过上限: {len(sub_tasks)}/10")

        REQUIRED_FIELDS = {"id", "name", "type", "depends_on", "description"}
        VALID_TYPES = {"new_feature", "bug_fix", "refactor", "debug_embedded", "non_dev"}
        VALID_EXECUTION_ORDERS = {"serial", "parallel", "mixed"}
        seen_ids = set()
        all_ids = {st.get("id") for st in sub_tasks if "id" in st}

        for i, st in enumerate(sub_tasks):
            missing = REQUIRED_FIELDS - set(st.keys())
            if missing:
                raise WorkflowError(f"子任务 #{i} 缺少必填字段: {missing}")

            sid = st["id"]
            if sid in seen_ids:
                raise WorkflowError(f"子任务 id 重复: {sid}")
            seen_ids.add(sid)

            if st["type"] not in VALID_TYPES:
                raise WorkflowError(
                    f"子任务 #{sid} type 非法: {st['type']}，合法值: {VALID_TYPES}"
                )

            for dep in st.get("depends_on", []):
                if dep not in all_ids:
                    raise WorkflowError(f"子任务 #{sid} 依赖不存在的任务: {dep}")

            st.setdefault("status", "pending")
            st.setdefault("restore_point", None)
            st.setdefault("modified_files", {})
            st.setdefault("handoff_file", None)
            st.setdefault("execution_order", "serial")  # 默认串行

        return sub_tasks

    def create_sub_task(self, parent_task: Task, sub_def: dict) -> Task:
        """创建子任务（用于多任务项目流），支持恢复已有进度"""
        sub_id = f"{parent_task.id}-sub{sub_def['id']}"
        sub_dir = parent_task.dir / f"sub-{sub_def['id']}"

        # 【新增】Resume 时加载已有状态
        state_file = sub_dir / "state.json"
        if state_file.exists():
            try:
                data = json.loads(state_file.read_text(encoding="utf-8"))
                existing = Task.from_dict(data)
                if existing.completed_steps:
                    logger.info(
                        f"恢复子任务 {sub_id} 已有进度: "
                        f"{existing.completed_steps}"
                    )
                    existing.status = "running"
                    self._save_state(existing)
                    return existing
            except (json.JSONDecodeError, OSError, KeyError) as e:
                logger.warning(f"子任务状态文件损坏，重新创建: {e}")

        # 正常创建新子任务（原有逻辑不变）
        sub_dir.mkdir(exist_ok=True)

        # 复制父任务的 PRD 和设计文档到子任务目录
        for doc in ["01-prd.md", "02-design.md"]:
            src = parent_task.dir / doc
            if src.exists():
                (sub_dir / doc).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

        sub_task = Task(
            id=sub_id,
            description=sub_def.get("name", ""),
            dir=sub_dir,
            project=parent_task.project,
            task_type=sub_def.get("type", "new_feature"),
            status="running",
            created_at=datetime.now(),
        )

        # 子任务独立 restore point（用于错误隔离回滚）
        sub_task.restore_point = self._create_git_tag(f"opus-restore-{sub_id}")
        self._save_state(sub_task)
        return sub_task

    # ─── 工作状态（实时，每步骤更新） ────────────────────────

    def update_work_state(self, task: Task, note: str = ""):
        """更新实时工作状态（由 update_step 自动调用）"""
        ws_file = write_data_path("work_state.json", project_root=self.project_path)
        ws_file.parent.mkdir(parents=True, exist_ok=True)

        # 保留已有的 recent_progress
        progress = []
        if ws_file.exists():
            try:
                existing = json.loads(ws_file.read_text(encoding="utf-8"))
                progress = existing.get("recent_progress", [])
            except (json.JSONDecodeError, OSError):
                pass

        timestamp = datetime.now().strftime("%H:%M")
        entry = f"[{timestamp}] {task.current_step} 完成"
        if note:
            entry += f"，{note}"
        progress.append(entry)
        progress = progress[-20:]  # 保留最近 20 条

        state = {
            "project": task.project,
            "current_task": {
                "id": task.id,
                "description": task.description,
                "current_step": task.current_step,
                "completed_steps": task.completed_steps,
            },
            "recent_progress": progress,
            "updated_at": datetime.now().isoformat(),
        }
        ws_file.write_text(
            json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def load_work_state(self) -> dict:
        """读取工作状态"""
        ws_file = read_data_path("work_state.json", project_root=self.project_path)
        if not ws_file.exists():
            return {}
        try:
            return json.loads(ws_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def clear_work_state(self):
        """任务完成时清除工作状态"""
        ws_file = write_data_path("work_state.json", project_root=self.project_path)
        state = {
            "project": self.config.get("default_project", ""),
            "current_task": None,
            "recent_progress": [],
            "updated_at": datetime.now().isoformat(),
        }
        ws_file.parent.mkdir(parents=True, exist_ok=True)
        ws_file.write_text(
            json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # ─── 会话摘要（天级，任务结束时写入） ──────────────────

    def save_session_summary(self, task: Task, status: str):
        """追加会话摘要到当天的 session 文件（单行索引格式）"""
        sessions_dir = write_data_path("sessions", project_root=self.project_path)
        sessions_dir.mkdir(parents=True, exist_ok=True)

        today = datetime.now().strftime("%Y-%m-%d")
        session_file = sessions_dir / f"{today}.md"

        cost = self.get_task_cost(task)

        # 简短耗时格式
        dur = task.duration
        if not dur:
            dur_str = "未知"
        else:
            total_sec = int(dur.total_seconds())
            if total_sec < 60:
                dur_str = f"{total_sec}s"
            else:
                minutes = total_sec // 60
                if minutes < 60:
                    dur_str = f"{minutes}min"
                else:
                    hours = minutes // 60
                    remaining_min = minutes % 60
                    dur_str = f"{hours}h{remaining_min}min" if remaining_min else f"{hours}h"

        entry = f"[{task.id}] {task.description} | {status} | {dur_str} | ${cost['total_usd']:.2f}\n"

        if session_file.exists():
            existing = session_file.read_text(encoding="utf-8")
            session_file.write_text(existing + entry, encoding="utf-8")
        else:
            header = f"# {today} 会话记录\n"
            session_file.write_text(header + entry, encoding="utf-8")

    def load_last_session(self) -> str:
        """读取最近 1 天的 session 摘要（最多 20 条，过滤 rolled_back）"""
        import re

        today = datetime.now()
        yesterday = today - timedelta(days=1)
        candidates = [
            today.strftime("%Y-%m-%d"),
            yesterday.strftime("%Y-%m-%d"),
        ]

        for date_str in candidates:
            session_file = read_data_path(
                "sessions",
                f"{date_str}.md",
                project_root=self.project_path,
            )
            if not session_file.exists():
                continue
            try:
                content = session_file.read_text(encoding="utf-8").strip()
                if not content:
                    continue
            except OSError:
                continue

            # 按行过滤 rolled_back（只对新格式行生效）
            rolled_back_pattern = re.compile(r"^\[.*?\].*\|\s*rolled_back\s*\|")
            lines = content.split("\n")
            filtered = [line for line in lines if not rolled_back_pattern.match(line)]

            # 限制最多 20 条数据行（不计标题行和空行）
            result_lines = []
            data_count = 0
            for line in filtered:
                if line.startswith("# ") or line.strip() == "":
                    result_lines.append(line)
                else:
                    if data_count < 20:
                        result_lines.append(line)
                        data_count += 1

            return "\n".join(result_lines)

        return ""

    # ─── Git Worktree ─────────────────────────────────────────

    async def create_worktrees(self, task: Task, branches: list) -> dict:
        """为并行 Agent 创建独立 Worktree"""
        # 【新增】获取并记录基线 commit hash
        base_hash = (await self._run_git("rev-parse", "HEAD")).stdout.strip()
        logger.info(f"创建 worktree 基线: {base_hash[:8]}")

        worktrees = {}
        for branch_name in branches:
            full_branch = f"opus/{task.id}/{branch_name}"
            worktree_path = write_data_path(
                "worktrees",
                f"{task.id}-{branch_name}",
                project_root=self.project_path,
            )
            worktree_path.parent.mkdir(parents=True, exist_ok=True)

            await self._run_git(
                "worktree", "add", "-b", full_branch,
                str(worktree_path), base_hash  # 【修改】显式使用 hash
            )
            worktrees[branch_name] = WorktreeInfo(
                branch=full_branch,
                path=worktree_path,
            )
        return worktrees

    async def merge_worktrees(self, task: Task, worktrees: dict,
                              conflict_resolver=None):
        """合并 Worktree 回主分支，支持 AI 冲突解决"""
        for name, wt in worktrees.items():
            # 预合并检查
            if not await self._pre_merge_check(wt.path):
                raise WorkflowError(f"预合并检查失败（{name}），请联调工程师处理")

            # 在 worktree 中提交所有变更
            await self._run_git("add", "-A", cwd=wt.path)
            await self._run_git(
                "commit", "-m", f"opus: {task.id} - {name}",
                "--allow-empty", cwd=wt.path
            )

            # 合并到主分支
            try:
                await self._run_git("merge", wt.branch, "--no-ff")
            except GitMergeConflict as e:
                if conflict_resolver:
                    # L2: 尝试 AI 解决冲突（带超时保护）
                    try:
                        resolved = await asyncio.wait_for(
                            conflict_resolver(
                                task=task,
                                branch_name=name,
                                conflict_info=str(e),
                            ),
                            timeout=600,  # 10 分钟超时
                        )
                    except asyncio.TimeoutError:
                        logger.error(f"AI 冲突解决超时（10min），放弃")
                        resolved = False
                    except Exception as exc:
                        logger.error(f"AI 冲突解决异常: {exc}")
                        resolved = False
                    if resolved:
                        # AI 已修复冲突文件并 git add
                        await self._run_git(
                            "commit", "-m",
                            f"opus: {task.id} - {name} (AI resolved conflicts)"
                        )
                        continue
                # AI 解决失败或无 resolver → abort + 抛异常
                await self._run_git("merge", "--abort")
                raise WorkflowError(f"Git 合并冲突（{name}），将由联调工程师处理")

        await self.cleanup_worktrees(worktrees)

    async def _pre_merge_check(self, worktree_path: Path) -> bool:
        """预合并 lint/type check"""
        checks = self.config.get("pre_merge_checks", [])
        for check in checks:
            changed_files = await self._get_changed_files(worktree_path, check["glob"])
            for f in changed_files:
                result = await self._run_shell(
                    check["cmd"].format(file=shlex.quote(f)), cwd=worktree_path
                )
                if result.returncode != 0:
                    logger.error(f"预合并检查失败: {f}\n{result.stderr}")
                    return False
        return True

    async def _get_changed_files(self, worktree_path: Path, glob_pattern: str) -> list:
        """获取 worktree 中变更的文件（匹配 glob 模式）"""
        try:
            result = await self._run_git("diff", "--name-only", "HEAD", cwd=worktree_path)
            all_files = [f for f in result.stdout.strip().split('\n') if f]
        except subprocess.CalledProcessError:
            return []
        # 展开 brace expansion: *.{js,ts,vue} → [*.js, *.ts, *.vue]
        patterns = self._expand_brace_glob(glob_pattern)
        return [f for f in all_files if any(fnmatch.fnmatch(f, p) for p in patterns)]

    @staticmethod
    def _expand_brace_glob(pattern: str) -> list:
        """展开 brace expansion: *.{js,ts} → [*.js, *.ts]"""
        m = re.search(r'\{([^}]+)\}', pattern)
        if not m:
            return [pattern]
        prefix = pattern[:m.start()]
        suffix = pattern[m.end():]
        return [f"{prefix}{opt}{suffix}" for opt in m.group(1).split(',')]

    async def cleanup_worktrees(self, worktrees: dict):
        """清理 Worktree 和临时分支"""
        for name, wt in worktrees.items():
            try:
                await self._run_git("worktree", "remove", str(wt.path), "--force")
            except Exception:
                pass
            try:
                await self._run_git("branch", "-D", wt.branch)
            except Exception:
                pass

    # ─── 清理 ─────────────────────────────────────────────────

    async def cleanup_old_tasks(self, keep_days: int = 7):
        """清理过期 Git 资源"""
        cutoff = datetime.now() - timedelta(days=keep_days)

        # 1. 清理过期 restore point tag
        try:
            result = await self._run_git("tag", "-l", "opus-restore-*")
            for tag in result.stdout.strip().split('\n'):
                tag = tag.strip()
                if not tag:
                    continue
                tag_date = await self._get_tag_date(tag)
                if tag_date and tag_date < cutoff:
                    await self._run_git("tag", "-d", tag)
        except Exception:
            pass

        # 2. 清理残留 worktree 分支
        try:
            result = await self._run_git("branch", "--list", "opus/*")
            for branch in result.stdout.strip().split('\n'):
                branch = branch.strip()
                if branch:
                    try:
                        await self._run_git("branch", "-D", branch)
                    except Exception:
                        pass
        except Exception:
            pass

        # 3. 清理残留 worktree 目录
        for wt_dir in iter_storage_dirs("worktrees", project_root=self.project_path):
            for d in wt_dir.iterdir():
                if d.is_dir():
                    try:
                        await self._run_git("worktree", "remove", str(d), "--force")
                    except Exception:
                        pass

        # 4. 清理过期任务目录 + 自动终止超期的暂停/失败任务
        for task_dir in iter_task_dirs(self.project_path):
            state_file = task_dir / "state.json"
            if state_file.exists():
                try:
                    state = json.loads(state_file.read_text(encoding="utf-8"))
                    created = datetime.fromisoformat(state["created_at"]) if state.get("created_at") else None
                    if not created or created >= cutoff:
                        continue
                    status = state.get("status", "")
                    if status in ("completed", "rolled_back", "terminated"):
                        # 过期的已结束任务：删除目录
                        import shutil
                        shutil.rmtree(task_dir)
                    elif status in ("paused", "failed"):
                        # 过期的暂停/失败任务：标记为 terminated（git tag 已清理，无法回滚/恢复）
                        state["status"] = "terminated"
                        state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
                        logger.info(f"超期任务 {state.get('id', task_dir.name)} 自动标记为 terminated")
                except Exception:
                    pass

        # 5. 清理过期 session 文件（保留 7 天）
        session_cutoff = datetime.now() - timedelta(days=7)
        for sessions_dir in iter_storage_dirs("sessions", project_root=self.project_path):
            for sf in sessions_dir.glob("*.md"):
                try:
                    file_date = datetime.strptime(sf.stem, "%Y-%m-%d")
                    if file_date < session_cutoff:
                        sf.unlink()
                except (ValueError, OSError):
                    pass

    async def _get_tag_date(self, tag: str):
        """获取 tag 对应的提交日期"""
        try:
            result = await self._run_git("log", "-1", "--format=%ci", tag)
            date_str = result.stdout.strip()
            return datetime.fromisoformat(date_str.split('+')[0].strip())
        except Exception:
            return None

    # ─── 底层工具 ──────────────────────────────────────────────

    def _short_hash(self) -> str:
        return ''.join(random.choices(string.ascii_lowercase + string.digits, k=4))

    def _create_git_tag(self, tag_name: str) -> str:
        result = subprocess.run(
            ["git", "tag", tag_name],
            cwd=str(self.project_path),
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            # tag 可能已存在（上次崩溃未清理），尝试删除后重建
            logger.warning(f"Git tag '{tag_name}' 创建失败: {result.stderr.strip()}，尝试重建")
            subprocess.run(["git", "tag", "-d", tag_name], cwd=str(self.project_path), capture_output=True)
            result = subprocess.run(
                ["git", "tag", tag_name],
                cwd=str(self.project_path),
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                logger.error(f"Git tag '{tag_name}' 重建也失败: {result.stderr.strip()}")
                return None
        return tag_name

    def _delete_git_tag(self, tag_name: str):
        subprocess.run(
            ["git", "tag", "-d", tag_name],
            cwd=str(self.project_path),
            capture_output=True,
        )

    async def _run_git(self, *args, cwd=None):
        """异步 Git 命令执行"""
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd or self.project_path),
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            error_msg = stderr.decode().strip()
            if "CONFLICT" in error_msg:
                raise GitMergeConflict(error_msg)
            raise subprocess.CalledProcessError(proc.returncode, "git", stderr)
        return type("GitResult", (), {"stdout": stdout.decode(), "stderr": stderr.decode()})()

    async def _run_shell(self, cmd: str, cwd: Path = None, timeout: int = 60):
        """异步 Shell 命令执行（带超时保护）"""
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd or self.project_path),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return type("ShellResult", (), {
                "returncode": -1,
                "stdout": "",
                "stderr": f"命令超时 ({timeout}s): {cmd[:100]}",
            })()
        return type("ShellResult", (), {
            "returncode": proc.returncode,
            "stdout": stdout.decode(),
            "stderr": stderr.decode(),
        })()
