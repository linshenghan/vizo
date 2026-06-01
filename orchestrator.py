#!/usr/bin/env python3
"""调度器：系统的大脑（Python 代码，不是 LLM）"""

import asyncio
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from agent_runner import AgentRunner, AgentResult, AgentTimeoutError, AgentError, AgentRateLimitError, AgentSignalInterrupt
from state_manager import StateManager, Task, WorkflowError, GitMergeConflict
from user_interface import UserInterface, ConfirmContext
from lib.runtime.subagents.controller import SubAgentRuntimeController
from lib.paths import (
    CONFIRMS_DIR,
    iter_task_dirs,
    task_dir as resolve_task_dir,
)

logger = logging.getLogger(__name__)


def load_config(config_path: str) -> dict:
    """加载配置，优先使用 config_loader（支持 .env 覆盖真实 API Key）"""
    try:
        from lib.config_loader import load_config as _cl_load
        return _cl_load()
    except Exception:
        # 兜底：直接读 JSON（config_loader 不可用时）
        config_file = Path(__file__).parent / config_path
        with open(config_file) as f:
            return json.load(f)


BUG_AREA_TO_ROLE = {
    "backend": "backend_developer",
    "frontend": "frontend_developer",
    "embedded": "embedded_engineer",
    "integration": "integration_engineer",
}


class BudgetExceeded(Exception):
    """预算暂停异常（非错误，任务保存进度不回滚）"""
    pass


class WorkflowPaused(Exception):
    """工作流被用户暂停（可 resume 继续）"""
    pass


class Orchestrator:
    """调度器 - 系统的大脑"""

    def __init__(self, config_path: str = "config.json", project_override: str = None):
        self.config = load_config(config_path)
        if project_override:
            self.config["default_project"] = project_override
        else:
            # 确保运行时槽位存在（config.json 不再静态写死 default_project）
            self.config.setdefault("default_project", "")
        self.agent = AgentRunner(self.config)
        self.state = StateManager(self.config)
        self.subagent_runtime = SubAgentRuntimeController(
            self.agent,
            project_root=self.state.project_path,
        )
        self.ui = UserInterface(self.config)
        self._warned_budget = False  # 避免重复警告
        self._progress_display = None  # 优化4b: 终端进度显示
        self._cleanup_done = False
        self._progress_redis = None  # lazy-init Redis for progress PUBLISH
        self._step_action_throttle = {}  # {step_id: last_publish_timestamp}
        self._current_parent_task_id = None  # 子任务执行时的父任务 ID
        self._current_sub_id = None  # 子任务执行时的子任务编号

    async def _execute_subagent_runtime(self, *, task=None, step_name=None, on_action=None, on_stream_event=None, **kwargs):
        """通过统一 runtime controller 执行子代理。"""
        return await self.subagent_runtime.run(
            task=task,
            step_name=step_name,
            config=self.config,
            on_action=on_action,
            on_stream_event=on_stream_event,
            **kwargs,
        )

    def _record_runtime_metadata(self, task: Task | None, step_name: str | None, payload):
        """将 runtime controller 产出的元数据持久化到 task state。"""
        if not task or not payload:
            return
        try:
            self.state.record_subagent_runtime(task, payload, step_name=step_name or "")
        except Exception as e:
            logger.warning(f"记录 runtime metadata 失败: {e}")

    # ─── 主入口 ───────────────────────────────────────────────

    async def _startup_cleanup(self):
        """启动时清理过期资源（只执行一次）"""
        if self._cleanup_done:
            return
        self._cleanup_done = True
        try:
            keep_days = self.config.get("cleanup_keep_days", 14)
            await self.state.cleanup_old_tasks(keep_days=keep_days)
        except Exception as e:
            logger.warning(f"启动清理失败（不影响任务执行）: {e}")

        # 检测并修复孤立的 running 任务（编排器进程崩溃后遗留）
        try:
            self._fix_orphaned_running_tasks()
        except Exception as e:
            logger.warning(f"修复孤立任务失败（不影响任务执行）: {e}")

    def _fix_orphaned_running_tasks(self):
        """修复孤立的 running 任务：编排器崩溃后 state.json 仍为 running 但无锁"""
        import fcntl
        for task_dir in iter_task_dirs(self.state.project_path):
            state_file = task_dir / "state.json"
            if not state_file.exists():
                continue
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if state.get("status") != "running":
                continue
            # 检查是否有活跃的锁
            lock_file = task_dir / ".lock"
            if lock_file.exists():
                try:
                    fd = open(lock_file, "r")
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    # 成功获取锁 → 说明原进程已死，释放锁并修复状态
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    fd.close()
                except (BlockingIOError, OSError):
                    # 锁被占用 → 有活跃进程，跳过
                    continue
            # 无活跃锁 → 孤立任务，修复 state.json 和 progress.json
            now = datetime.now().isoformat(timespec="seconds")
            state["status"] = "failed"
            state_file.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            # 修复 progress.json
            progress_file = task_dir / "progress.json"
            if progress_file.exists():
                try:
                    progress = json.loads(progress_file.read_text(encoding="utf-8"))
                    if progress.get("status") == "running":
                        progress["status"] = "failed"
                        progress["updated_at"] = now
                        for step in progress.get("steps", []):
                            if step.get("status") == "running":
                                step["status"] = "error"
                                step["completed_at"] = now
                                step.setdefault("error", "编排器进程异常退出")
                        # 补充费用：从子任务 cost.json 汇总未被统计的费用
                        self._patch_orphan_costs(task_dir, progress)
                        progress_file.write_text(
                            json.dumps(progress, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                except (json.JSONDecodeError, OSError):
                    pass
            logger.info(f"修复孤立任务: {task_dir.name} (running → failed)")

    @staticmethod
    def _patch_orphan_costs(task_dir: Path, progress: dict):
        """编排器崩溃恢复时，从子任务 cost.json 补充未统计的步骤费用。"""
        # 收集父任务和所有子任务的 cost.json 总费用
        total_from_costs = 0.0
        # 父任务自己的 cost.json
        parent_cost = task_dir / "cost.json"
        if parent_cost.is_file():
            try:
                costs = json.loads(parent_cost.read_text("utf-8"))
                total_from_costs += sum(c.get("cost_usd", 0) for c in costs)
            except (json.JSONDecodeError, OSError):
                pass
        # 子任务目录的 cost.json
        for sub_dir in sorted(task_dir.iterdir()):
            if sub_dir.is_dir() and sub_dir.name.startswith("sub-"):
                sub_cost = sub_dir / "cost.json"
                if sub_cost.is_file():
                    try:
                        costs = json.loads(sub_cost.read_text("utf-8"))
                        total_from_costs += sum(c.get("cost_usd", 0) for c in costs)
                    except (json.JSONDecodeError, OSError):
                        pass
        # 用 cost.json 汇总更新 progress 总费用（取两者较大值，避免覆盖已有的准确值）
        if total_from_costs > 0:
            existing = progress.get("cost_usd", 0) or 0
            progress["cost_usd"] = round(max(existing, total_from_costs), 2)

    async def _prefetch_references(self, text: str) -> str:
        """预抓取文本中的 URL 和本地文件引用，供 Agent 作为输入参考"""
        import re as _re
        results = []

        # 1. 抓取 URL（带重试）
        url_pattern = _re.compile(r'https?://[A-Za-z0-9._~:/?#\[\]@!$&\'()*+,;=%-]+')
        urls = url_pattern.findall(text)
        for url in urls[:3]:
            url = url.rstrip('.,;!?)\'"')
            content = self._fetch_url_with_retry(url, retries=2)
            if content:
                results.append(f"## 参考文档（来自 {url}）\n\n{content}")

        # 2. 提取本地文件路径（.md / .txt / .doc / .docx / .pdf）
        file_pattern = _re.compile(r'(?:^|\s)(/[\w./-]+\.(?:md|txt|doc|docx|pdf))\b')
        file_paths = file_pattern.findall(text)
        for fpath in file_paths[:3]:
            content = self._read_local_file(fpath)
            if content:
                results.append(f"## 参考文档（来自 {fpath}）\n\n{content}")

        return "\n\n---\n\n".join(results)

    def _fetch_url_with_retry(self, url: str, retries: int = 2) -> str:
        """抓取 URL 内容（HTML → Markdown），失败重试"""
        import urllib.request
        import html2text
        import time

        for attempt in range(retries + 1):
            try:
                logger.info(f"预抓取 URL: {url}" + (f" (重试 {attempt})" if attempt else ""))
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                resp = urllib.request.urlopen(req, timeout=15)
                html = resp.read().decode("utf-8", errors="replace")
                h = html2text.HTML2Text()
                h.ignore_links = False
                h.body_width = 0
                content = h.handle(html).strip()
                if content and len(content) > 50:
                    logger.info(f"URL 抓取成功: {len(content)} 字符")
                    return content
            except Exception as e:
                logger.warning(f"URL 抓取异常: {url} - {e}")
                if attempt < retries:
                    time.sleep(2)
        return ""

    def _read_local_file(self, fpath: str) -> str:
        """读取本地文件内容（md/txt 直接读，docx 用 python-docx，pdf 用文本提取）"""
        from pathlib import Path
        p = Path(fpath)
        if not p.is_file():
            logger.warning(f"本地文件不存在: {fpath}")
            return ""
        try:
            if p.suffix in (".md", ".txt"):
                content = p.read_text(encoding="utf-8", errors="replace").strip()
                logger.info(f"本地文件读取成功: {fpath} ({len(content)} 字符)")
                return content
            elif p.suffix == ".docx":
                try:
                    import docx
                    doc = docx.Document(str(p))
                    content = "\n\n".join(para.text for para in doc.paragraphs if para.text.strip())
                    logger.info(f"DOCX 读取成功: {fpath} ({len(content)} 字符)")
                    return content
                except ImportError:
                    logger.warning("python-docx 未安装，无法读取 .docx 文件")
                    return ""
            elif p.suffix == ".pdf":
                try:
                    import subprocess
                    result = subprocess.run(
                        ["pdftotext", "-layout", str(p), "-"],
                        capture_output=True, text=True, timeout=30)
                    if result.returncode == 0 and result.stdout.strip():
                        logger.info(f"PDF 读取成功: {fpath} ({len(result.stdout)} 字符)")
                        return result.stdout.strip()
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    logger.warning("pdftotext 不可用，无法读取 .pdf 文件")
                return ""
            elif p.suffix == ".doc":
                try:
                    import subprocess
                    result = subprocess.run(
                        ["antiword", str(p)],
                        capture_output=True, text=True, timeout=30)
                    if result.returncode == 0 and result.stdout.strip():
                        logger.info(f"DOC 读取成功: {fpath} ({len(result.stdout)} 字符)")
                        return result.stdout.strip()
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    logger.warning("antiword 不可用，无法读取 .doc 文件")
                return ""
        except Exception as e:
            logger.warning(f"文件读取异常: {fpath} - {e}")
        return ""

    @staticmethod
    def _compact_runtime_neutral_doc(content: str, *, max_lines: int = 12, max_chars: int = 1200) -> str:
        lines = [line.rstrip() for line in str(content or "").splitlines() if line.strip()]
        excerpt = "\n".join(lines[:max_lines]).strip()
        if len(excerpt) > max_chars:
            excerpt = excerpt[:max_chars].rstrip() + "..."
        elif len(lines) > max_lines:
            excerpt = excerpt + "\n..."
        return excerpt

    def _build_runtime_neutral_docs_summary(self, project_path: Path | str | None) -> str:
        """Compatibility stub: formal Opus tasks no longer inject temporary docs."""
        return ""

    async def run(self, user_request: str, workflow: str = "auto"):
        """主入口：分类→对应模式→需求分析→PRD→工作流"""
        await self._startup_cleanup()
        # 0. 企微模式下检查服务可用性
        if self.ui.mode == "wecom":
            status = await self.ui.check_wecom_services()
            if not status["callback"]:
                self.ui.print_warning("企微 Callback Server 未运行，远程回复将不可用")
                self.ui.print_info("启动方式: systemctl --user start wecom-callback")
            if not status["redis"]:
                self.ui.print_warning("Redis 不可用，企微消息收发将降级到终端")

        # 1. 分类
        task_mode = self._classify_request(user_request)

        if task_mode == "chat":
            await self._quick_answer(user_request)
            return
        if task_mode == "explore":
            await self._explore_code(user_request)
            return
        if task_mode == "resume":
            await self.resume_last_task()
            return

        # 读取会话回忆和工作状态（注入首个 Agent）
        session_recall = self.state.load_last_session()
        work_state = self.state.load_work_state()
        work_state_summary = ""
        if work_state and work_state.get("current_task"):
            ct = work_state["current_task"]
            work_state_summary = (
                f"进行中任务: {ct['description']}\n"
                f"当前步骤: {ct['current_step']}\n"
                f"已完成: {', '.join(ct.get('completed_steps', []))}\n"
            )
            if work_state.get("recent_progress"):
                work_state_summary += "进度:\n" + "\n".join(work_state["recent_progress"][-5:])

        # 开发任务
        task = self.state.create_task(user_request)
        self.ui._current_task_id = task.id

        # 记录显式 workflow 到 metadata，并预设 task_type（前端立即可见）
        if workflow != "auto":
            task.metadata["workflow"] = workflow
            _wf_to_tt = {
                "new_feature": "new_feature", "bug_fix": "bug_fix",
                "refactor": "refactor", "embedded": "debug_embedded",
                "non_dev": "non_dev",
            }
            if workflow in _wf_to_tt:
                task.task_type = _wf_to_tt[workflow]
            self.state._save_state(task)

        # 🔒 任务级文件锁
        lock_file_path = task.dir / ".lock"
        self._task_lock_fd = open(lock_file_path, "w")
        try:
            fcntl.flock(self._task_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            self._task_lock_fd.close()
            self._task_lock_fd = None
            raise WorkflowError(f"任务 {task.id} 已在其他进程中运行")
        self.ui.print_task_created(task)

        # 优化4b: 启动终端进度显示
        if self.ui.mode == "terminal":
            self._progress_display = self.ui.create_progress_display(
                task_id=task.id, task_desc=task.description
            )
            self._progress_display.start()

        try:
            await self._execute_main_workflow(task, session_recall, work_state_summary)

        except WorkflowPaused as e:
            self.state.save_session_summary(task, f"暂停（用户选择）")
            self.state.pause_task(task, reason=str(e))
            self.ui.print_info(f"任务已暂停：{e}")
            self.ui.print_info("可通过 opus --resume 继续")
        except WorkflowError as e:
            await self._generate_failure_report(task, e)
            actually_rolled_back = self.state.rollback(task)
            if actually_rolled_back:
                self.state.save_session_summary(task, f"回滚（{e}）")
                self.ui.print_info(
                    f"代码已安全回滚（历史保留）\n"
                    f"恢复分支: opus-snapshot/{task.id}\n"
                    f"恢复命令: git cherry-pick opus-snapshot/{task.id}"
                )
            else:
                self.state.save_session_summary(task, f"取消（{e}）")
                self.ui.print_info(f"任务已取消（无代码变更，无需回滚）")
        except BudgetExceeded as e:
            self.state.save_session_summary(task, f"暂停（预算）")
            self.state.pause_task(task, reason=str(e))
            self.ui.print_warning(
                f"任务已暂停（预算）: {e}\n"
                f"已完成步骤: {', '.join(task.completed_steps)}\n"
                f"使用 opus --resume 可继续执行"
            )
        except (AgentTimeoutError, AgentError) as e:
            logger.error(f"Agent 执行失败: {e}")
            await self._generate_failure_report(task, e)
            self.state.save_session_summary(task, f"失败（{type(e).__name__}）")
            self.state.fail_task(task, reason=str(e))
            self.ui.print_error(
                f"任务失败: {e}\n"
                f"使用 opus --resume 可重试失败步骤"
            )
        except Exception as e:
            logger.exception(f"未预期的错误: {e}")
            await self._generate_failure_report(task, e)
            self.state.save_session_summary(task, f"异常（{type(e).__name__}）")
            self.state.fail_task(task, reason=str(e))
            self.ui.print_error(
                f"任务异常: {e}\n"
                f"使用 opus --resume 可重试"
            )
        finally:
            # 优化4b: 停止终端进度显示
            if self._progress_display:
                self._progress_display.stop()
                self._progress_display = None
            # 🔒 释放任务级文件锁
            self._release_task_lock()

    def _release_task_lock(self):
        """释放任务级文件锁"""
        if hasattr(self, "_task_lock_fd") and self._task_lock_fd:
            try:
                fcntl.flock(self._task_lock_fd, fcntl.LOCK_UN)
                self._task_lock_fd.close()
            except Exception:
                pass
            self._task_lock_fd = None

    @staticmethod
    def _read_lock_pid(lock_file_path: Path) -> int | None:
        """从锁文件中读取 PID"""
        try:
            content = lock_file_path.read_text(encoding="utf-8").strip()
            return int(content) if content else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _is_opus_process_alive(pid: int) -> bool:
        """检查指定 PID 是否是存活的 opus 进程"""
        try:
            os.kill(pid, 0)  # 检查进程是否存在
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # 进程存在但无权限发信号

        # 进程存在，验证是否是 opus 相关进程
        try:
            cmdline_path = Path(f"/proc/{pid}/cmdline")
            if cmdline_path.exists():
                cmdline = cmdline_path.read_bytes().decode("utf-8", errors="replace")
                return "opus.py" in cmdline
        except (OSError, PermissionError):
            pass
        return True  # 无法确认时保守返回 True

    def _cleanup_stale_confirms(self, task: Task):
        """清理旧进程留下的孤立确认请求"""
        confirm_dir = CONFIRMS_DIR
        if not confirm_dir.exists():
            return

        current_pid = os.getpid()
        cleaned = 0
        for req_file in confirm_dir.glob("*.request.json"):
            resp_file = req_file.with_name(
                req_file.name.replace(".request.json", ".response.json")
            )
            if resp_file.exists():
                continue  # 已有响应，跳过

            try:
                data = json.loads(req_file.read_text(encoding="utf-8"))
            except Exception:
                continue

            # 只清理属于当前任务的确认请求
            ctx = data.get("context", {})
            if ctx.get("task_id") != task.id:
                continue

            # 检查创建时间，超过 24 小时的视为孤立
            created_at = data.get("created_at")
            if created_at:
                age = time.time() - float(created_at)
                if age > 86400:  # 24 小时
                    response = {
                        "action": "expired",
                        "feedback": f"旧进程孤立请求，被 PID {current_pid} 清理",
                        "responded_at": time.time(),
                        "reason": "stale confirm from dead process",
                    }
                    resp_file.write_text(
                        json.dumps(response, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    cleaned += 1
                    logger.info(f"清理孤立确认请求: {req_file.name}")

        if cleaned:
            logger.info(f"共清理 {cleaned} 个孤立确认请求")

    async def _execute_main_workflow(self, task, session_recall="", work_state_summary=""):
        """核心工作流：需求分析 → PM → 架构师 → 开发流程。run() 和 _resume_task() 共用。"""
        user_request = task.description

        # 0. 技术栈自动检测（首次运行时触发）
        project_name = task.project or self.config.get("default_project", "")
        project_path = self.agent._get_project_path(project_name)
        if project_path:
            tech_stack_file = Path(project_path) / ".serena" / "memories" / "tech-stack.md"
            if not tech_stack_file.exists():
                tech_info = self._detect_tech_stack(Path(project_path))
                if tech_info["platform"] != "unknown":
                    self._generate_tech_stack_memory(Path(project_path), tech_info)
                else:
                    logger.debug("技术栈检测结果为 unknown，跳过生成 tech-stack.md")

        # 1.5 预抓取用户请求中的 URL 内容（供后续 Agent 使用）
        url_content = await self._prefetch_references(user_request)
        input_docs_base = {"user_request": user_request}
        if url_content:
            input_docs_base["reference_document"] = url_content

        # 1.6 持久化原始需求 + URL 参考内容（供所有后续步骤自动注入）
        original_request_file = task.dir / "00-user-request.md"
        if not original_request_file.exists():
            content_parts = [f"# 用户原始需求\n\n{user_request}"]
            if url_content:
                content_parts.append(f"\n\n---\n\n# 参考文档（预抓取）\n\n{url_content}")
            original_request_file.write_text(
                "\n".join(content_parts), encoding="utf-8"
            )

        # 注入用户补充意见（resume 场景可能有）
        injection_file = task.dir / "user_injection.md"
        if injection_file.exists():
            input_docs_base["user_feedback"] = injection_file.read_text(encoding="utf-8")

        # 2. 需求分析
        ra_result = None
        if "requirement_analysis" not in task.completed_steps:
            self.state.update_step(task, "requirement_analysis")
            ra_result = await self._run_agent(
                task=task, step_name="requirement_analysis",
                role="requirement_analyst",
                task_dir=task.dir,
                input_docs=input_docs_base,
                memories=[],
                output_file=task.dir / "00-requirement-analysis.md",
                project=self.config["default_project"],
                session_recall=session_recall,
                work_state_summary=work_state_summary,
            )

            # 从 RA 输出提取简短任务标题
            if not task.task_name:
                task.task_name = self._extract_task_name(task)
                self.state._save_state(task)

            _ctx = self._build_confirm_context(
                task, role="requirement_analyst",
                file=task.dir / "00-requirement-analysis.md",
                button_set="doc_review_discussion",
            )
            self.state.set_pending_confirm(task, "", "confirm_with_feedback",
                                           "需求分析报告已生成",
                                           button_set=_ctx.button_set)
            self._update_progress(task, "waiting_confirm",
                                  pending_confirm=task.pending_confirm)
            action = await self.ui.confirm_with_feedback(
                "需求分析报告已生成",
                file=task.dir / "00-requirement-analysis.md",
                allow_discussion=True,
                context=_ctx,
                force=self._step_requires_confirm(task.current_step),
            )
            self.state.clear_pending_confirm(task)
            self._update_progress(task, "confirm_resolved")
            if action == "cancel":
                raise WorkflowError("用户取消")
            if action == "feedback":
                await self._revise_requirement_analysis(task, max_rounds=2)
            if action == "discussion":
                await self._run_direction_discussion(task)
                await self._post_discussion_loop(task)

        # 3. RA 粗判 task_type 和 scale
        ra_task_type = "new_feature"  # 默认值（兼容旧 RA 无 task_type 输出）
        if ra_result:
            ra_task_type = ra_result.data.get("task_type", "new_feature")
            # RA 粗判 scale（仅首次执行时设置，架构师终判会覆盖）
            ra_scale = ra_result.data.get("scale", "")
            if ra_scale:
                task.scale = ra_scale
                self.state._save_state(task)
        elif task.task_type:
            # 恢复场景：task_type 和 scale 保持 state.json 中的值，不再重新提取
            ra_task_type = task.task_type
        logger.info(f"RA 粗判 task_type={ra_task_type}, scale={task.scale}")

        # === 显式 workflow 读取 ===
        explicit_workflow = task.metadata.get("workflow", "auto")
        WORKFLOW_TO_TASK_TYPE = {
            "new_feature": "new_feature",
            "bug_fix": "bug_fix",
            "refactor": "refactor",
            "embedded": "debug_embedded",
            "non_dev": "non_dev",
        }

        # 如果用户显式指定了非 auto 的 workflow，覆盖 RA 粗判
        if explicit_workflow != "auto":
            task_type = WORKFLOW_TO_TASK_TYPE[explicit_workflow]
            task.task_type = task_type
            self.state._save_state(task)
            logger.info(f"显式 workflow={explicit_workflow}，task_type 设为 {task_type}，跳过 AI 分类")

        # 4. 根据粗判分流（显式 workflow 时 task_type 已确定）
        if explicit_workflow == "auto":
            task_type = ra_task_type
        if not task.task_type:
            task.task_type = task_type
            self.state._save_state(task)
        task_scale = task.scale or "normal"
        architect_result = None

        if explicit_workflow != "auto":
            # 显式指定：跳过 PM（除非是 new_feature），直接走架构师设计
            if explicit_workflow == "new_feature":
                await self._run_pm_if_needed(task, user_request)
            if "architect" not in task.completed_steps:
                architect_result = await self._run_full_architect(task)
        elif ra_task_type == "new_feature":
            # 路径 A：RA 判为 new_feature → PM 先出 PRD → 架构师完整设计
            await self._run_pm_if_needed(task, user_request)

            if "architect" not in task.completed_steps:
                architect_result = await self._run_full_architect(task)

        else:
            # 路径 B：RA 判为非 new_feature → 架构师读代码 + 分类 + 设计
            if "architect" not in task.completed_steps:
                architect_result = await self._run_full_architect(
                    task, ra_task_type=ra_task_type,
                )

                # 检查架构师是否升级为 new_feature
                if architect_result and architect_result.data.get("upgrade_needed"):
                    logger.info("架构师升级为 new_feature，补跑 PM → resume 架构师")
                    task_type = "new_feature"
                    task.task_type = "new_feature"
                    self.state._save_state(task)

                    # 保存架构师 session_id 用于 resume
                    arch_session_id = architect_result.data.get("session_id", "")

                    # 补跑 PM
                    await self._run_pm_if_needed(task, user_request)

                    # resume 架构师（带 PRD 完成完整设计）
                    if arch_session_id:
                        self.state.update_step(task, "architect")
                        architect_result = await self._run_agent(
                            task=task, step_name="architect",
                            role="architect",
                            task_dir=task.dir,
                            input_docs={"prd": "01-prd.md"},
                            memories=[],
                            output_file=task.dir / "02-design.md",
                            project=task.project or self.config["default_project"],
                            resume_session=arch_session_id,
                        )
                    else:
                        # fallback: 无 session_id 则重新跑完整架构师
                        logger.warning("无架构师 session_id，重新执行完整设计")
                        architect_result = await self._run_full_architect(task)

        # 5. 处理架构师完整设计结果
        if architect_result:
            architect_task_type = architect_result.data.get("task_type", task_type)
            task_scale = architect_result.data.get("scale", task_scale)

            # 关键保护：显式 workflow 时，architect 的 task_type 仅记录，不覆盖路由
            if explicit_workflow != "auto":
                logger.info(f"显式 workflow 保护：architect 判定 {architect_task_type}，"
                            f"但路由仍使用 {task_type}")
                # task_type 保持显式指定的值不变
            else:
                task_type = architect_task_type
                task.task_type = task_type

            task.scale = task_scale
            # 持久化架构师输出的 sub_tasks
            arch_sub_tasks = architect_result.data.get("sub_tasks", [])
            # fallback: 若 result 事件中没有 sub_tasks，从 02-design.md 文件提取
            if not arch_sub_tasks and task_scale == "large":
                arch_sub_tasks = self._extract_sub_tasks_from_design(task.dir / "02-design.md")
                if arch_sub_tasks:
                    logger.info(f"从 02-design.md 提取到 {len(arch_sub_tasks)} 个子任务（result 事件缺失 fallback）")
            if arch_sub_tasks and task_scale == "large":
                task.sub_tasks = self.state.validate_sub_tasks(arch_sub_tasks)
            self.state._save_state(task)

            # 持久化 behavior_changes 供任务完成时使用
            behavior_changes = architect_result.data.get("behavior_changes", [])
            if behavior_changes:
                (task.dir / "behavior_changes.json").write_text(
                    json.dumps(behavior_changes, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

            # 持久化 files_to_modify 供工作流智能跳过
            files_to_modify = architect_result.data.get("files_to_modify", {})
            if files_to_modify:
                (task.dir / "files_to_modify.json").write_text(
                    json.dumps(files_to_modify, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

            # 架构师确认已去掉（V3 精简），直接继续
            self.state.complete_step(task, "design_confirmed")
        else:
            # resume 场景：从已有 state 恢复
            task_type = task.task_type or "new_feature"
            task_scale = task.scale

        # 6. 分流执行
        if task_scale == "large":
            await self._run_multi_task_workflow(task)
        else:
            workflow = self._select_workflow(task_type)
            await workflow(task)

        # 完成收尾
        await self._finalize_task(task)

    # ─── 请求分类 ─────────────────────────────────────────────

    def _classify_request(self, user_request: str) -> str:
        """纯 Python 关键词分类（不调 LLM）"""
        request_lower = user_request.lower()

        # 恢复/继续：仅对短文本做 resume 分类，长文本直接走 dev 模式
        # 避免需求描述中包含"恢复暂停的任务"等文字被误判
        if len(user_request) < 50:
            resume_patterns = [
                r"继续.{0,4}(任务|工作|上次|之前|未完成|中断)",
                r"恢复.{0,4}(任务|中断|上次|之前)",
                r"接着.{0,4}(任务|工作|上次|之前|做|干)",
                r"从断点",
            ]
            if any(re.search(p, request_lower) for p in resume_patterns):
                return "resume"

        # 探索关键词（疑问句式优先）
        explore_keywords = [
            "怎么实现", "是怎么", "如何实现", "实现原理", "代码在哪",
            "怎么工作", "流程是什么", "架构是什么", "分析一下",
            "看看", "了解", "解释", "explain", "how does",
        ]
        if any(kw in request_lower for kw in explore_keywords):
            return "explore"

        # 开发动作关键词
        dev_keywords = [
            "帮我", "添加", "修改", "修复", "删除", "重构", "优化",
            "实现", "开发", "部署", "升级", "迁移", "新增",
            "fix", "add", "remove", "refactor", "implement",
        ]
        if any(kw in request_lower for kw in dev_keywords):
            return "dev"

        # 简短问答
        chat_keywords = [
            "是什么", "是多少", "密码", "地址", "端口", "版本",
            "配置", "在哪里", "what is", "where is",
        ]
        if any(kw in request_lower for kw in chat_keywords) and len(user_request) < 30:
            return "chat"

        return "dev"

    # ─── 快速模式 ─────────────────────────────────────────────

    async def _quick_answer(self, user_request: str):
        result = await self._run_agent(
            role="assistant",
            task_dir=None,
            input_docs={"user_request": user_request},
            memories=[],
            project=self.config["default_project"],
        )
        self.ui.print_result(result.data.get("answer", result.raw_output))

    async def _explore_code(self, user_request: str):
        result = await self._run_agent(
            role="code_explorer",
            task_dir=None,
            input_docs={"user_request": user_request},
            memories=[],
            project=self.config["default_project"],
        )
        self.ui.print_result(result.data.get("answer", result.raw_output))

    async def quick_chat(self, question: str):
        await self._quick_answer(question)

    # ─── 修订循环 ─────────────────────────────────────────────

    async def _revise_requirement_analysis(self, task, max_rounds=2):
        for _ in range(max_rounds):
            feedback = await self.ui.get_user_feedback()
            if not feedback:
                return
            self.state.update_step(task, "requirement_analysis")
            await self._run_agent(
                task=task, step_name="requirement_analysis",
                role="requirement_analyst",
                task_dir=task.dir,
                input_docs={
                    "original_analysis": "00-requirement-analysis.md",
                    "user_feedback": feedback,
                },
                output_file=task.dir / "00-requirement-analysis.md",
                project=self.config["default_project"],
            )
            _ctx = self._build_confirm_context(
                task, role="requirement_analyst",
                file=task.dir / "00-requirement-analysis.md",
                button_set="doc_review",
            )
            self.state.set_pending_confirm(task, "", "confirm_with_feedback",
                                           "修改后的需求分析",
                                           button_set=_ctx.button_set)
            self._update_progress(task, "waiting_confirm",
                                  pending_confirm=task.pending_confirm)
            action = await self.ui.confirm_with_feedback(
                "修改后的需求分析", file=task.dir / "00-requirement-analysis.md",
                context=_ctx,
                force=self._step_requires_confirm(task.current_step),
            )
            self.state.clear_pending_confirm(task)
            self._update_progress(task, "confirm_resolved")
            if action == "cancel":
                raise WorkflowError("用户在修订轮次中取消")
            if action != "feedback":
                return

    async def _revise_prd(self, task, max_rounds=2):
        for _ in range(max_rounds):
            feedback = await self.ui.get_user_feedback()
            if not feedback:
                return
            self.state.update_step(task, "pm_prd")
            await self._run_agent(
                task=task, step_name="pm_prd",
                role="product_manager",
                task_dir=task.dir,
                input_docs={
                    "requirement_analysis": "00-requirement-analysis.md",
                    "original_prd": "01-prd.md",
                    "user_feedback": feedback,
                },
                output_file=task.dir / "01-prd.md",
                project=self.config["default_project"],
            )
            _ctx = self._build_confirm_context(
                task, role="product_manager",
                file=task.dir / "01-prd.md",
                button_set="doc_review",
            )
            self.state.set_pending_confirm(task, "", "confirm_with_feedback",
                                           "修改后的 PRD",
                                           button_set=_ctx.button_set)
            self._update_progress(task, "waiting_confirm",
                                  pending_confirm=task.pending_confirm)
            action = await self.ui.confirm_with_feedback(
                "修改后的 PRD", file=task.dir / "01-prd.md",
                context=_ctx,
                force=self._step_requires_confirm(task.current_step),
            )
            self.state.clear_pending_confirm(task)
            self._update_progress(task, "confirm_resolved")
            if action == "cancel":
                raise WorkflowError("用户在修订轮次中取消")
            if action != "feedback":
                return

    async def _run_full_architect(self, task, ra_task_type=None):
        """运行架构师完整设计（读代码 + 写技术方案）"""
        self.state.update_step(task, "architect")
        arch_input_docs = {"requirement_analysis": "00-requirement-analysis.md"}
        if (task.dir / "00-technical-assessment.md").exists():
            arch_input_docs["technical_assessment"] = "00-technical-assessment.md"
        if (task.dir / "01-prd.md").exists():
            arch_input_docs["prd"] = "01-prd.md"
        if ra_task_type:
            arch_input_docs["ra_task_type"] = ra_task_type
        return await self._run_agent(
            task=task, step_name="architect",
            role="architect",
            task_dir=task.dir,
            input_docs=arch_input_docs,
            memories=[],
            output_file=task.dir / "02-design.md",
            project=task.project or self.config["default_project"],
        )

    async def _run_pm_if_needed(self, task, user_request):
        """运行产品经理出 PRD（如尚未完成）"""
        if "pm_prd" in task.completed_steps:
            return
        self.state.update_step(task, "pm_prd")
        pm_input_docs = {
            "user_request": user_request,
            "requirement_analysis": "00-requirement-analysis.md",
        }
        if (task.dir / "00-technical-assessment.md").exists():
            pm_input_docs["technical_assessment"] = "00-technical-assessment.md"
        await self._run_agent(
            task=task, step_name="pm_prd",
            role="product_manager",
            task_dir=task.dir,
            input_docs=pm_input_docs,
            memories=[],
            output_file=task.dir / "01-prd.md",
            project=self.config["default_project"],
        )

        _ctx = self._build_confirm_context(
            task, role="product_manager",
            file=task.dir / "01-prd.md",
            button_set="doc_review",
        )
        self.state.set_pending_confirm(task, "", "confirm_with_feedback",
                                       "PRD 已生成",
                                       button_set=_ctx.button_set)
        self._update_progress(task, "waiting_confirm",
                              pending_confirm=task.pending_confirm)
        action = await self.ui.confirm_with_feedback(
            "PRD 已生成", file=task.dir / "01-prd.md",
            context=_ctx,
            force=self._step_requires_confirm(task.current_step),
        )
        self.state.clear_pending_confirm(task)
        self._update_progress(task, "confirm_resolved")
        if action == "cancel":
            raise WorkflowError("用户取消")
        if action == "feedback":
            await self._revise_prd(task, max_rounds=2)

    async def _revise_design(self, task, max_rounds=2):
        for _ in range(max_rounds):
            feedback = await self.ui.get_user_feedback()
            if not feedback:
                return
            self.state.update_step(task, "architect")
            await self._run_agent(
                task=task, step_name="architect",
                role="architect",
                task_dir=task.dir,
                input_docs={
                    "prd": "01-prd.md",
                    "original_design": "02-design.md",
                    "user_feedback": feedback,
                },
                memories=[],
                output_file=task.dir / "02-design.md",
                project=task.project,
            )
            _ctx = self._build_confirm_context(
                task, role="architect",
                file=task.dir / "02-design.md",
                button_set="doc_review",
            )
            self.state.set_pending_confirm(task, "", "confirm_with_feedback",
                                           "修改后的方案",
                                           button_set=_ctx.button_set)
            self._update_progress(task, "waiting_confirm",
                                  pending_confirm=task.pending_confirm)
            action = await self.ui.confirm_with_feedback(
                "修改后的方案", file=task.dir / "02-design.md",
                context=_ctx,
            )
            self.state.clear_pending_confirm(task)
            self._update_progress(task, "confirm_resolved")
            if action == "cancel":
                raise WorkflowError("用户在修订轮次中取消")
            if action != "feedback":
                return

    # ─── 方向讨论 ────────────────────────────────────────────

    def _save_discussion_session(self, task, role, session_id):
        """保存讨论会话 session_id 供 resume 使用"""
        if session_id:
            session_file = task.dir / f".discussion_session_{role}"
            session_file.write_text(session_id, encoding="utf-8")

    def _get_discussion_session(self, task, role):
        """获取已保存的讨论会话 session_id"""
        session_file = task.dir / f".discussion_session_{role}"
        if session_file.exists():
            return session_file.read_text(encoding="utf-8").strip()
        return None

    async def _run_direction_discussion(self, task):
        """方向讨论：架构师技术评估 + 条件合并"""

        # Step 1: 架构师技术评估（首次新建 or resume 已有会话）
        saved_session = self._get_discussion_session(task, "technical_assessor")

        assessor_result = await self._run_agent(
            task=task, step_name="technical_assessment",
            role="technical_assessor",
            task_dir=task.dir,
            input_docs={"requirement_analysis": "00-requirement-analysis.md"},
            memories=[],
            output_file=task.dir / "00-technical-assessment.md",
            project=self.config["default_project"],
            resume_session=saved_session,
        )

        # 保存 session_id 供后续 resume
        session_id = None
        if assessor_result and assessor_result.data:
            session_id = assessor_result.data.get("session_id")
        self._save_discussion_session(task, "technical_assessor", session_id)

        # Step 2: 根据评估结果决定是否需要合并
        has_concerns = False
        if assessor_result and assessor_result.data:
            has_concerns = assessor_result.data.get("has_direction_concerns", False)

        if not has_concerns:
            self.ui.print_info("技术评估通过，未发现方向性问题，自动追加摘要到需求文档")
            # 无方向性顾虑时，零成本追加技术评估摘要到需求文档
            ta_path = task.dir / "00-technical-assessment.md"
            ra_path = task.dir / "00-requirement-analysis.md"
            if ta_path.exists() and ra_path.exists():
                ta_content = ta_path.read_text(encoding="utf-8").strip()
                ra_content = ra_path.read_text(encoding="utf-8").strip()
                if ta_content:
                    merged = (
                        ra_content
                        + "\n\n---\n\n"
                        + "## 技术评估摘要（自动追加）\n\n"
                        + "> 以下内容由架构师技术评估自动合入，无方向性风险。\n\n"
                        + ta_content
                    )
                    ra_path.write_text(merged, encoding="utf-8")
                    logger.info("已将技术评估摘要追加到需求分析文档")
            return

        # Step 3: RA 合并双视角（resume 已有会话，保持上下文）
        ra_session = self._get_discussion_session(task, "requirement_analyst")
        merge_instruction = (
            "你之前做了初步需求分析，现在架构师从技术角度提供了评估。\n"
            "请将两份文档合并，更新需求分析文档：\n"
            "1. 保留原有的用户视角分析\n"
            "2. 将技术评估中的约束和影响纳入'潜在问题'和'边界条件'\n"
            "3. 如果技术评估指出某些需求不可行或代价极高，在'建议补充'中提出替代方向\n"
            "4. 新增'技术约束摘要'节（提炼可行性结论、影响范围、关键风险）\n"
            "5. 新增'待确认事项'节（汇总双方未决问题）\n"
        )
        ra_merge_result = await self._run_agent(
            task=task, step_name="requirement_merge",
            role="requirement_analyst",
            task_dir=task.dir,
            input_docs={
                "original_analysis": "00-requirement-analysis.md",
                "technical_assessment": "00-technical-assessment.md",
                "merge_instruction": merge_instruction,
            },
            output_file=task.dir / "00-requirement-analysis.md",
            project=self.config["default_project"],
            resume_session=ra_session,
        )
        # 保存 RA session_id 供后续 resume
        ra_sid = None
        if ra_merge_result and ra_merge_result.data:
            ra_sid = ra_merge_result.data.get("session_id")
        self._save_discussion_session(task, "requirement_analyst", ra_sid)

    async def _post_discussion_loop(self, task):
        """讨论后的确认循环，无轮次上限，用户确认即结束"""
        while True:
            _ctx = self._build_confirm_context(
                task, role="requirement_analyst",
                file=task.dir / "00-requirement-analysis.md",
                button_set="doc_review_discussion",
            )
            self.state.set_pending_confirm(task, "", "confirm_with_feedback",
                                           "需求讨论已完成，请确认需求文档",
                                           button_set=_ctx.button_set)
            self._update_progress(task, "waiting_confirm",
                                  pending_confirm=task.pending_confirm)
            action = await self.ui.confirm_with_feedback(
                "需求讨论已完成，请确认需求文档",
                file=task.dir / "00-requirement-analysis.md",
                allow_discussion=True,
                context=_ctx,
            )
            self.state.clear_pending_confirm(task)
            self._update_progress(task, "confirm_resolved")
            if action == "confirm":
                return
            if action == "cancel":
                raise WorkflowError("用户取消")
            if action == "feedback":
                feedback = await self.ui.get_user_feedback()
                if not feedback:
                    return
                ra_session = self._get_discussion_session(task, "requirement_analyst")
                ra_fb_result = await self._run_agent(
                    role="requirement_analyst",
                    task_dir=task.dir,
                    input_docs={
                        "original_analysis": "00-requirement-analysis.md",
                        "technical_assessment": "00-technical-assessment.md",
                        "user_feedback": feedback,
                    },
                    output_file=task.dir / "00-requirement-analysis.md",
                    project=self.config["default_project"],
                    resume_session=ra_session,
                )
                ra_sid = None
                if ra_fb_result and ra_fb_result.data:
                    ra_sid = ra_fb_result.data.get("session_id")
                self._save_discussion_session(task, "requirement_analyst", ra_sid)
            if action == "discussion":
                # 再次讨论 → 架构师 resume 重新评估 + 条件合并
                await self._run_direction_discussion(task)

    # ─── 工作流路由 ────────────────────────────────────────────


    async def _finalize_task(self, task):
        """任务完成后的收尾工作：标记完成、手册更新、成本汇报"""
        # 停止进度显示
        if self._progress_display:
            self._progress_display.stop()

        # 标记完成
        self.state.save_session_summary(task, "完成")
        self.state.complete_task(task)

        # 安全网：自动提交未提交的代码变更
        commit_hash = self._auto_commit_changes(task)

        # 更新 progress.json + 企微卡片推送
        self._update_progress(task, "complete")
        await self._push_step_card(task, "complete")

        # 用户手册自动更新（有行为变更时触发）
        behavior_changes_file = task.dir / "behavior_changes.json"
        if behavior_changes_file.exists():
            bc_content = behavior_changes_file.read_text(encoding="utf-8").strip()
            if bc_content and bc_content != "[]":
                try:
                    await self._run_agent(
                        role="manual_updater",
                        task_dir=task.dir,
                        input_docs={"behavior_changes": bc_content},
                        memories=[],
                        output_file=task.dir / "10-manual-update.md",
                        project=task.project,
                    )
                except (AgentTimeoutError, AgentError) as me:
                    logger.warning(f"manual_updater 执行失败（不阻塞流程）: {me}")

        # 收集产出文档链接
        doc_links = []
        key_docs = [
            ("需求分析", "00-requirement-analysis.md"),
            ("PRD", "01-prd.md"),
            ("技术设计", "02-design.md"),
            ("测试报告", self._find_latest_test_report_name(task.dir)),
        ]
        for label, filename in key_docs:
            doc_file = task.dir / filename
            if doc_file.exists():
                link = await self.ui._upload_preview(doc_file)
                if link:
                    doc_links.append(f"  {label}: {link}")

        # 成本汇报
        cost = self.state.get_task_cost(task)
        msg = (
            f"任务完成: {task.description}\n"
            f"耗时: {task.duration_str} | 消耗: {cost['total_tokens']:,} tokens = ${cost['total_usd']:.2f}"
        )
        if doc_links:
            msg += "\n📎 产出文档：\n" + "\n".join(doc_links)
        if commit_hash:
            msg += f"\n代码已提交: {commit_hash}"
        self.ui.print_success(msg)

    def _auto_commit_changes(self, task) -> str | None:
        """安全网：任务完成后自动提交未提交的代码变更，返回 commit hash 或 None"""
        try:
            project_root = str(self.state.project_path)

            # 1. 检查有无变更
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=project_root, capture_output=True, text=True, timeout=15,
            )
            if status.returncode != 0 or not status.stdout.strip():
                logger.info("auto_commit: 工作区干净，跳过提交")
                return None

            # 2. 暂存所有文件（排除 Vizo/legacy 状态目录）
            subprocess.run(
                ["git", "add", "--all", "--", ".", ":!.vizo", ":!.opus"],
                cwd=project_root, capture_output=True, text=True, timeout=30,
            )

            # 3. 确认暂存区有内容
            diff_check = subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                cwd=project_root, capture_output=True, timeout=15,
            )
            if diff_check.returncode == 0:
                logger.info("auto_commit: 暂存区为空，跳过提交")
                return None

            # 4. 提交
            desc = task.description[:60] if task.description else "task complete"
            commit_msg = f"feat(opus-task): {desc} [{task.id}]"
            commit = subprocess.run(
                ["git", "commit", "-m", commit_msg],
                cwd=project_root, capture_output=True, text=True, timeout=30,
            )
            if commit.returncode != 0:
                logger.warning(f"auto_commit: git commit 失败: {commit.stderr.strip()}")
                return None

            # 5. 获取 commit hash
            rev = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=project_root, capture_output=True, text=True, timeout=10,
            )
            commit_hash = rev.stdout.strip() if rev.returncode == 0 else "unknown"
            logger.info(f"auto_commit: 已提交 {commit_hash}")
            return commit_hash

        except subprocess.TimeoutExpired:
            logger.warning("auto_commit: git 操作超时，跳过提交")
            return None
        except Exception as e:
            logger.warning(f"auto_commit: 提交失败（不影响任务状态）: {e}")
            return None

    async def _confirm_behavior_changes(self, task):
        """检查并标记设计已确认（V3 精简：不再弹确认）"""
        if "design_confirmed" not in task.completed_steps:
            self.state.complete_step(task, "design_confirmed")

    # ─── 技术栈检测 ────────────────────────────────────────────

    def _detect_tech_stack(self, project_path: Path) -> dict:
        """纯文件特征匹配检测项目技术栈，不调 LLM。"""
        result = {
            "platform": "unknown",
            "frontend_framework": "",
            "backend_framework": "",
            "deploy_target": "",
            "key_constraints": [],
            "detected_files": [],
        }

        # 辅助：读取 package.json 的 dependencies
        pkg_json = project_path / "package.json"
        pkg_deps = {}
        if pkg_json.exists():
            try:
                pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
                pkg_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                result["detected_files"].append("package.json")
            except (json.JSONDecodeError, OSError):
                pass

        # 检测顺序（优先级从高到低）
        # 1. 微信小程序
        if (project_path / "app.json").exists() and (project_path / "project.config.json").exists():
            result["platform"] = "wechat_miniprogram"
            result["detected_files"].extend(["app.json", "project.config.json"])
            result["deploy_target"] = "微信开发者工具上传"
            result["key_constraints"] = [
                "禁止使用 fetch，网络请求必须使用 wx.request()",
                "主包体积限制 2MB，超出需拆分为分包",
                "使用 wx.setStorageSync/wx.getStorageSync，不可用 localStorage",
                "禁止直接操作 DOM，使用 this.setData() 驱动视图",
                "调试使用微信开发者工具，不支持 Chrome DevTools / Chrome MCP",
            ]
            return result

        # 2. Flutter
        if (project_path / "pubspec.yaml").exists():
            result["platform"] = "flutter"
            result["detected_files"].append("pubspec.yaml")
            try:
                content = (project_path / "pubspec.yaml").read_text(encoding="utf-8")
                m = re.search(r'flutter:\s*\n\s*sdk:\s*["\']?([^"\'>\n]+)', content)
                if m:
                    result["frontend_framework"] = f"Flutter SDK {m.group(1).strip()}"
            except OSError:
                pass
            return result

        # 3. Android
        if (project_path / "android" / "build.gradle").exists() or \
           (project_path / "AndroidManifest.xml").exists():
            result["platform"] = "android"
            result["detected_files"].append("android/build.gradle or AndroidManifest.xml")
            return result

        # 4. iOS
        if (project_path / "ios" / "Podfile").exists():
            result["platform"] = "ios"
            result["detected_files"].append("ios/Podfile")
            return result

        # 5-8: 基于 package.json 的 JS 项目
        if pkg_deps:
            # 5. React Native
            if "react-native" in pkg_deps:
                result["platform"] = "react_native"
                rn_ver = pkg_deps.get("react-native", "")
                result["frontend_framework"] = f"React Native {rn_ver}".strip()
                return result

            # 6. Next.js
            if "next" in pkg_deps:
                result["platform"] = "nextjs"
                next_ver = pkg_deps.get("next", "")
                result["frontend_framework"] = f"Next.js {next_ver}".strip()
                return result

            # 7. React
            if "react" in pkg_deps:
                result["platform"] = "react"
                react_ver = pkg_deps.get("react", "")
                result["frontend_framework"] = f"React {react_ver}".strip()
                return result

            # 8. Vue
            if "vue" in pkg_deps:
                result["platform"] = "vue"
                vue_ver = pkg_deps.get("vue", "")
                result["frontend_framework"] = f"Vue {vue_ver}".strip()
                return result

        # 9. Python Web
        if (project_path / "requirements.txt").exists() or \
           (project_path / "pyproject.toml").exists():
            result["platform"] = "python_web"
            if (project_path / "requirements.txt").exists():
                result["detected_files"].append("requirements.txt")
                try:
                    reqs = (project_path / "requirements.txt").read_text(encoding="utf-8")
                    for framework in ["django", "flask", "fastapi", "tornado", "aiohttp"]:
                        for line in reqs.lower().splitlines():
                            if line.strip().startswith(framework):
                                result["backend_framework"] = line.strip()
                                break
                except OSError:
                    pass
            if (project_path / "pyproject.toml").exists():
                result["detected_files"].append("pyproject.toml")
            return result

        # 10. 未知
        return result

    def _generate_tech_stack_memory(self, project_path: Path, tech_info: dict) -> None:
        """根据检测结果生成 tech-stack.md 记忆文件。原子写入。"""
        PLATFORM_NAMES = {
            "wechat_miniprogram": "微信小程序",
            "flutter": "Flutter App",
            "android": "Android App",
            "ios": "iOS App",
            "react_native": "React Native App",
            "nextjs": "Next.js Web",
            "react": "React Web",
            "vue": "Vue Web",
            "python_web": "Python Web",
        }
        platform_name = PLATFORM_NAMES.get(tech_info["platform"], tech_info["platform"])

        lines = [
            f"# 技术栈上下文 — {platform_name}",
            "",
            "## 产品形态",
            platform_name,
            "",
        ]
        if tech_info["frontend_framework"]:
            lines += ["## 前端框架", tech_info["frontend_framework"], ""]
        if tech_info["backend_framework"]:
            lines += ["## 后端框架", tech_info["backend_framework"], ""]
        if tech_info["deploy_target"]:
            lines += ["## 部署方式", tech_info["deploy_target"], ""]
        if tech_info["key_constraints"]:
            lines += ["## 关键平台约束", ""]
            for c in tech_info["key_constraints"]:
                lines.append(f"- **{c}**")
            lines.append("")
        if tech_info["detected_files"]:
            lines += [
                "## 自动检测依据",
                f"- 检测到文件：{', '.join(tech_info['detected_files'])}",
                f"- 检测时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
                "",
            ]
        lines += [
            "## 自定义补充（可手动编辑）",
            "（用户可在此追加项目特定约束，如接口格式约定、认证方式等）",
            "",
        ]

        # 原子写入
        memories_dir = project_path / ".serena" / "memories"
        memories_dir.mkdir(parents=True, exist_ok=True)
        target = memories_dir / "tech-stack.md"

        # check-then-write（并发安全）
        if target.exists():
            return

        import tempfile
        content = "\n".join(lines)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(memories_dir), suffix=".tmp")
        try:
            os.write(tmp_fd, content.encode("utf-8"))
            os.close(tmp_fd)
            os.rename(tmp_path, str(target))
            logger.info(f"已生成 tech-stack.md：{tech_info['platform']}")
        except OSError as e:
            logger.warning(f"写入 tech-stack.md 失败: {e}")
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


    def _select_workflow(self, task_type: str):
        return {
            "new_feature": self._workflow_full_dev,
            "bug_fix": self._workflow_quick_fix,
            "refactor": self._workflow_refactor,
            "debug_embedded": self._workflow_embedded,
            "non_dev": self._workflow_non_dev,
        }.get(task_type, self._workflow_full_dev)


    async def _workflow_non_dev(self, task: Task):
        """非开发任务：单 Agent 直接执行"""
        # architect fallback（resume 时可能跳过了 run() 中的架构师步骤）
        if "architect" not in task.completed_steps:
            logger.warning("architect 未在 run() 中完成，non_dev 流程跳过（不依赖设计文档）")
            self.state.complete_step(task, "architect")

        if "assistant" not in task.completed_steps:
            self.state.update_step(task, "assistant")
            await self._run_agent(
                task=task, step_name="assistant",
                role="assistant",
                task_dir=task.dir,
                input_docs={"requirement": "00-requirement-analysis.md"},
                memories=[],
                output_file=task.dir / "result.md",
                project=task.project,
            )

    # ─── 完整开发流（6阶段） ──────────────────────────────────


    async def _check_file_overlap(self, worktrees: dict) -> set:
        """L1: 检查多个 worktree 修改的文件是否有交叉，返回重叠文件集合"""
        files_by_area = {}
        for name, wt in worktrees.items():
            try:
                result = await self.state._run_git(
                    "diff", "--name-only", "HEAD", cwd=wt.path
                )
                files = set(f for f in result.stdout.strip().split('\n') if f)
                files_by_area[name] = files
            except Exception:
                files_by_area[name] = set()

        overlap = set()
        areas = list(files_by_area.keys())
        for i in range(len(areas)):
            for j in range(i + 1, len(areas)):
                overlap |= files_by_area[areas[i]] & files_by_area[areas[j]]
        if overlap:
            logger.warning(f"L1 文件交叉检测: {overlap}")
        return overlap

    async def _resolve_merge_conflict(self, task, branch_name, conflict_info):
        """L2: 用 AI Agent 解决 git merge 冲突"""
        # 1. 获取冲突文件列表
        result = await self.state._run_git("diff", "--name-only", "--diff-filter=U")
        conflict_files = [f for f in result.stdout.strip().split('\n') if f]

        if not conflict_files:
            return False

        # 2. 读取冲突内容（含 conflict markers）
        conflict_content = {}
        for f in conflict_files:
            fpath = self.state.project_path / f
            if fpath.exists():
                conflict_content[f] = fpath.read_text(
                    encoding='utf-8', errors='replace'
                )[:10000]

        # 3. 启动轻量 merge_resolver Agent
        try:
            await self._run_agent(
                role="merge_resolver",
                task_dir=task.dir,
                input_docs={
                    "conflict_files": json.dumps(
                        conflict_content, ensure_ascii=False
                    ),
                    "branch_name": branch_name,
                    "design": "02-design.md",
                },
                output_file=task.dir / f"06-merge-resolve-{branch_name}.md",
                project=task.project,
            )

            # 4. 验证所有冲突已解决
            check = await self.state._run_git(
                "diff", "--name-only", "--diff-filter=U"
            )
            remaining = [f for f in check.stdout.strip().split('\n') if f]
            if remaining:
                logger.warning(f"AI 解决后仍有冲突文件: {remaining}")
                return False

            return True
        except Exception as e:
            logger.warning(f"merge_resolver 执行失败: {e}")
            return False

    async def _sequential_fallback(self, task):
        """L3: 合并冲突后串行降级——在主分支上依次重新开发"""
        logger.info("L3 串行降级：在主分支上重新执行开发")

        # 回滚到 parallel_dev 前的状态（安全方式，不销毁历史）
        if task.restore_point:
            self.state._safe_revert_to(
                task.restore_point,
                str(self.state.project_path),
                f"revert: 任务 {task.id} 串行降级（并行合并失败）",
            )

        # 依次在主分支上执行（无 worktree，无冲突可能）
        # 用 _retry 后缀区分，避免与并行阶段的 progress 条目混淆
        # 后端先做
        self.state.update_step(task, "backend_dev_retry")
        await self._run_agent(
            task=task, step_name=None,
            role="backend_developer", task_dir=task.dir,
            input_docs={"design": "02-design.md"},
            memories=[],
            output_file=task.dir / "04-backend-result.md",
            project=task.project,
        )
        # 前端基于后端成果做
        self.state.update_step(task, "frontend_dev_retry")
        frontend_input_docs = await self._prepare_frontend_input_docs(
            task,
            {"design": "02-design.md"},
            ensure_design_gate=False,
        )
        await self._run_agent(
            task=task, step_name=None,
            role="frontend_developer", task_dir=task.dir,
            input_docs=frontend_input_docs,
            memories=[],
            output_file=task.dir / "05-frontend-result.md",
            project=task.project,
        )

    def _build_frontend_design_request(self, task: Task) -> str:
        parts = [
            "以下是一个正式研发任务，在进入前端开发前需要先为当前任务补齐视觉系统。",
            "请基于任务描述、产品文档和技术设计，为本次任务推荐并固化一套可执行的视觉系统，再生成交互 demo。",
            "除非用户后续明确采纳，否则这套设计只约束当前任务，不代表项目默认设计系统。",
            "",
            "## 当前任务",
            task.description,
        ]
        for filename, title, limit in (
            ("01-prd.md", "产品文档", 6000),
            ("02-design.md", "技术设计", 8000),
        ):
            path = task.dir / filename
            if not path.exists():
                continue
            try:
                content = path.read_text(encoding="utf-8").strip()
            except Exception:
                continue
            if not content:
                continue
            if len(content) > limit:
                content = content[:limit].rstrip() + "\n...(已截断)"
            parts.extend(["", f"## {title}", content])
        return "\n".join(parts).strip()

    @staticmethod
    def _frontend_design_doc_names() -> tuple[str, str]:
        return "03-frontend-design-context.md", "03-frontend-selected-system.json"

    @staticmethod
    def _write_frontend_design_context_files(
        *,
        design: dict,
        context_path: Path,
        selected_path: Path,
        mode: str,
        note: str,
    ) -> None:
        from lib.interaction_design_service import InteractionDesignService

        selected_path.write_text(
            json.dumps(design, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        mode_label = "项目已采纳设计系统" if mode == "adopted" else "当前任务临时设计系统"
        context_text = "\n".join(
            [
                "# Frontend Design Context",
                "",
                f"- 模式：{mode_label}",
                f"- 说明：{note}",
                "",
                "## 当前任务必须遵守的视觉系统",
                InteractionDesignService.build_runtime_context(
                    design,
                    mode="adopted" if mode == "adopted" else "preview",
                ),
                "",
                "## 使用边界",
                "- 这是当前正式开发任务的前端实现约束，不允许前端工程师自由改大字号、大圆角和组件气质。",
                "- 如果用户尚未明确采纳，这套方案只约束当前任务，不应回写项目默认设计记忆。",
            ]
        ).strip()
        context_path.write_text(context_text + "\n", encoding="utf-8")

    async def _run_frontend_design_gate(self, task: Task, *, project_root: Path) -> dict:
        from agent_hub import AgentHub

        hub = AgentHub(self.config)
        hub.ui = self.ui
        hub.agent = self.agent
        return await hub.execute(
            module_id="interaction_design",
            user_request=self._build_frontend_design_request(task),
            project_config={"path": str(project_root), "type": "dev"},
        )

    async def _prepare_frontend_input_docs(
        self,
        task: Task,
        input_docs: dict | None,
        *,
        ensure_design_gate: bool,
    ) -> dict:
        docs = dict(input_docs or {})
        context_name, selected_name = self._frontend_design_doc_names()
        context_path = task.dir / context_name
        selected_path = task.dir / selected_name
        if context_path.exists():
            docs["frontend_design_context"] = context_name
            if selected_path.exists():
                docs["frontend_selected_system"] = selected_name
            return docs

        project_root = Path(
            self.agent._get_project_path(task.project or self.config.get("default_project", ""))
        )
        from lib.interaction_design_service import InteractionDesignService

        design_service = InteractionDesignService(agent_runner=self.agent)
        adopted = design_service.load_adopted_design(project_root)
        if adopted:
            self._write_frontend_design_context_files(
                design=adopted,
                context_path=context_path,
                selected_path=selected_path,
                mode="adopted",
                note="当前项目已有已采纳设计系统，本次前端开发必须直接复用。",
            )
            docs["frontend_design_context"] = context_name
            docs["frontend_selected_system"] = selected_name
            return docs

        if not ensure_design_gate:
            return docs

        if "interaction_design" not in task.completed_steps:
            self.state.update_step(task, "interaction_design")
        hub_task = await self._run_frontend_design_gate(task, project_root=project_root)
        status = str((hub_task or {}).get("status") or "").strip()
        if status in {"cancelled", "terminated"}:
            raise WorkflowPaused("前端设计方案选择已取消，任务已暂停")
        if status != "completed":
            raise WorkflowError("前端设计方案补齐未完成，无法继续前端开发")

        candidate = design_service.load_latest_task_design(
            project_root,
            task_id=str((hub_task or {}).get("id") or ""),
        )
        if not candidate:
            raise WorkflowError("交互设计任务未产出可用的视觉系统，无法继续前端开发")

        self._write_frontend_design_context_files(
            design=candidate,
            context_path=context_path,
            selected_path=selected_path,
            mode="preview",
            note="这是当前正式开发任务自动触发的交互设计结果，仅约束本次任务，不代表项目默认设计系统。",
        )
        if "interaction_design" not in task.completed_steps:
            self.state.complete_step(task, "interaction_design")
        docs["frontend_design_context"] = context_name
        docs["frontend_selected_system"] = selected_name
        return docs

    def _detect_dev_scope(self, task) -> tuple:
        """从架构师产出判断需要哪些开发者，返回 (has_backend, has_frontend)

        优先级：files_to_modify.json > 设计文档章节扫描 > 默认仅后端
        如果前后端文件有交叉，降级为仅后端（避免 worktree merge 冲突浪费资源）
        """
        # 1. 优先读 JSON 文件清单（架构师结构化输出）
        ftm_file = task.dir / "files_to_modify.json"
        if ftm_file.exists():
            try:
                ftm = json.loads(ftm_file.read_text(encoding="utf-8"))
                if ftm:
                    be_files = set(ftm.get("backend") or [])
                    fe_files = set(ftm.get("frontend") or [])
                    # 文件交叉 → 降级为仅后端，避免 worktree 合并冲突
                    if be_files and fe_files and be_files & fe_files:
                        logger.warning(
                            f"前后端文件交叉 {be_files & fe_files}，降级为仅后端开发"
                        )
                        return True, False
                    return bool(be_files), bool(fe_files)
            except Exception:
                pass

        # 2. 降级：从设计文档的"文件清单"章节推断
        design_file = task.dir / "02-design.md"
        if design_file.exists():
            try:
                content = design_file.read_text(encoding="utf-8")
                # 查找"### 后端" / "### 前端"章节是否有实质内容
                has_backend = bool(re.search(
                    r"###\s*后端.*?\n\s*\|.*\|", content, re.DOTALL
                ))
                has_frontend = bool(re.search(
                    r"###\s*前端.*?\n\s*\|.*\|", content, re.DOTALL
                ))
                if has_backend and has_frontend:
                    # 提取章节标题+内容中的文件路径，检测交叉
                    be_match = re.search(
                        r"(###\s*后端\s*[^\n]*\n.*?)(?=\n###|\n##|\Z)", content, re.DOTALL
                    )
                    fe_match = re.search(
                        r"(###\s*前端\s*[^\n]*\n.*?)(?=\n###|\n##|\Z)", content, re.DOTALL
                    )
                    be_files = set(re.findall(r"`([^`]+\.\w+)`", be_match.group(1))) if be_match else set()
                    fe_files = set(re.findall(r"`([^`]+\.\w+)`", fe_match.group(1))) if fe_match else set()
                    overlap = be_files & fe_files
                    if overlap:
                        logger.warning(
                            f"设计文档中前后端文件交叉 {overlap}，降级为仅后端开发"
                        )
                        return True, False
                if has_backend or has_frontend:
                    logger.info(f"从设计文档推断开发范围: backend={has_backend}, frontend={has_frontend}")
                    return has_backend, has_frontend
            except Exception:
                pass

        # 3. 兜底：默认仅后端
        logger.info("无法确定开发范围，默认仅后端")
        return True, False

    async def _workflow_full_dev(self, task: Task):
        # --- 阶段1：架构设计（已在 run() 中完成，自动跳过） ---
        if "architect" not in task.completed_steps:
            logger.warning("architect 步骤未在 run() 中完成，补执行")
            self.state.update_step(task, "architect")
            architect_result = await self._run_agent(
                task=task, step_name="architect",
                role="architect",
                task_dir=task.dir,
                input_docs={"prd": "01-prd.md"},
                memories=[],
                output_file=task.dir / "02-design.md",
                project=task.project,
            )
            # 持久化 files_to_modify（与 run() 中逻辑对齐，供 _detect_dev_scope 使用）
            if architect_result and architect_result.data:
                files_to_modify = architect_result.data.get("files_to_modify", {})
                if files_to_modify:
                    (task.dir / "files_to_modify.json").write_text(
                        json.dumps(files_to_modify, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
        else:
            logger.info("跳过已完成步骤: architect")

        # --- 兼容旧任务：parallel_dev 已完成则跳过所有开发步骤 ---
        if "parallel_dev" in task.completed_steps:
            for _s in ("qa_test_cases", "backend_dev", "frontend_dev"):
                if _s not in task.completed_steps:
                    task.completed_steps.append(_s)
            self.state._save_state(task)

        # --- 阶段2a：QA 出测试用例 ---
        if "qa_test_cases" not in task.completed_steps:
            self.state.update_step(task, "qa_test_cases")
            await self._run_agent(
                task=task, step_name="qa_test_cases",
                role="qa_engineer", task_dir=task.dir,
                input_docs={"prd": "01-prd.md", "design": "02-design.md"},
                memories=[],
                output_file=task.dir / "03-test-cases.md",
                project=task.project,
            )
            self.state.complete_step(task, "qa_test_cases")
        else:
            logger.info("跳过已完成步骤: qa_test_cases")

        # --- 阶段2b：开发（根据架构师分析智能决策） ---
        has_backend, has_frontend = self._detect_dev_scope(task)
        backend_needed = has_backend and "backend_dev" not in task.completed_steps
        frontend_needed = has_frontend and "frontend_dev" not in task.completed_steps
        frontend_input_docs = {"prd": "01-prd.md", "design": "02-design.md"}
        if frontend_needed:
            frontend_input_docs = await self._prepare_frontend_input_docs(
                task,
                frontend_input_docs,
                ensure_design_gate=True,
            )

        if backend_needed or frontend_needed:
            if backend_needed and frontend_needed:
                # 前后端都有改动 → worktree 并行开发 + 合并
                self.state.update_step(task, "backend_dev")
                from stream_renderer import LayoutStateMachine
                worktrees = await self.state.create_worktrees(task, branches=["backend", "frontend"])

                # 初始化分屏状态机
                layout_sm = LayoutStateMachine()
                def _on_layout_transition(mode, mgr):
                    if not self._progress_display:
                        return
                    if mgr:
                        self._progress_display.activate_split(mgr)
                    else:
                        self._progress_display.deactivate_split()
                layout_sm.set_transition_callback(_on_layout_transition)

                try:
                    results = await asyncio.gather(
                        self._run_agent_parallel(
                            layout_sm,
                            task=task, step_name="backend_dev",
                            role="backend_developer", task_dir=task.dir,
                            input_docs={"prd": "01-prd.md", "design": "02-design.md"},
                            memories=[],
                            output_file=task.dir / "04-backend-result.md",
                            project=task.project, cwd=worktrees["backend"].path,
                        ),
                        self._run_agent_parallel(
                            layout_sm,
                            task=task, step_name="frontend_dev",
                            role="frontend_developer", task_dir=task.dir,
                            input_docs=frontend_input_docs,
                            memories=[],
                            output_file=task.dir / "05-frontend-result.md",
                            project=task.project, cwd=worktrees["frontend"].path,
                        ),
                        return_exceptions=True,
                    )

                    # 让用户看到完成统计
                    await asyncio.sleep(1)
                    layout_sm.exit_split()

                    # gather 完成后检查控制信号
                    await self._check_control_signal(task)

                    # 检查并行结果中的异常
                    errors = [(i, r) for i, r in enumerate(results) if isinstance(r, Exception)]
                    if errors:
                        for i, e in errors:
                            logger.error(f"并行开发异常 (agent {i}): {e}")
                        # 先尝试合并成功的 worktree（保留已完成的工作）
                        try:
                            await self.state.merge_worktrees(
                                task, worktrees,
                                conflict_resolver=self._resolve_merge_conflict
                            )
                            logger.info("部分代理失败，但已合并成功的 worktree 代码")
                        except Exception as merge_err:
                            logger.warning(f"合并部分结果失败: {merge_err}")
                        await self.state.cleanup_worktrees(worktrees)
                        raise errors[0][1]

                    overlap = await self._check_file_overlap(worktrees)
                    if overlap:
                        logger.warning(f"文件交叉检测: {overlap}，跳过合并直接串行降级")
                        await self.state.cleanup_worktrees(worktrees)
                        await self._sequential_fallback(task)
                        self.state.complete_step(task, "backend_dev")
                        self.state.complete_step(task, "frontend_dev")
                    else:
                        try:
                            await self.state.merge_worktrees(
                                task, worktrees,
                                conflict_resolver=self._resolve_merge_conflict
                            )
                            self.state.complete_step(task, "backend_dev")
                            self.state.complete_step(task, "frontend_dev")
                        except (WorkflowError, subprocess.CalledProcessError) as e:
                            logger.warning(f"合并失败，启动 L3 串行降级: {e}")
                            await self.state.cleanup_worktrees(worktrees)
                            await self._sequential_fallback(task)
                            self.state.complete_step(task, "backend_dev")
                            self.state.complete_step(task, "frontend_dev")
                except Exception:
                    layout_sm.exit_split()  # 确保异常时也退出分屏
                    try:
                        await self.state.cleanup_worktrees(worktrees)
                    except Exception as cleanup_err:
                        logger.warning(f"worktree 清理失败（可能已不存在）: {cleanup_err}")
                    raise
            else:
                # 只有一端改动 → 直接在主分支开发，无需 worktree
                if has_backend:
                    self.state.update_step(task, "backend_dev")
                    await self._run_agent(
                        task=task, step_name="backend_dev",
                        role="backend_developer", task_dir=task.dir,
                        input_docs={"prd": "01-prd.md", "design": "02-design.md"},
                        memories=[],
                        output_file=task.dir / "04-backend-result.md",
                        project=task.project,
                    )
                    self.state.complete_step(task, "backend_dev")
                if has_frontend:
                    self.state.update_step(task, "frontend_dev")
                    await self._run_agent(
                        task=task, step_name="frontend_dev",
                        role="frontend_developer", task_dir=task.dir,
                        input_docs=frontend_input_docs,
                        memories=[],
                        output_file=task.dir / "05-frontend-result.md",
                        project=task.project,
                    )
                    self.state.complete_step(task, "frontend_dev")
        else:
            logger.info("跳过已完成步骤: backend_dev/frontend_dev")

        # --- 阶段3：联调 ---
        if "integration" not in task.completed_steps:
            self.state.update_step(task, "integration")
            await self._run_agent(
                task=task, step_name="integration",
                role="integration_engineer", task_dir=task.dir,
                input_docs={
                    "prd": "01-prd.md",
                    "design": "02-design.md",
                    "backend_result": "04-backend-result.md",
                    "frontend_result": "05-frontend-result.md",
                },
                memories=[],
                output_file=task.dir / "06-integration-report.md",
                project=task.project,
            )
        else:
            logger.info("跳过已完成步骤: integration")

        # --- 阶段4：测试+修bug ---
        self._check_and_restart_service(task)
        await self._test_fix_loop(task, max_rounds=3)

        # --- 阶段5：部署 ---
        await self._deploy_fix_loop(task, "测试通过，是否部署？")

        # --- 阶段6：知识沉淀 ---
        await self._accumulate_knowledge(task)

    # ─── 部署-修复循环 ────────────────────────────────────────

    async def _deploy_fix_loop(self, task: Task,
                              confirm_msg: str = "测试通过，是否部署？",
                              max_rounds: int = 3):
        """部署并在失败时路由到开发者修复，最多 max_rounds 轮"""
        if "deploy" in task.completed_steps:
            return
        # 部署确认已去掉（V3 精简），自动执行

        for round_num in range(1, max_rounds + 1):
            step_name = "deploy" if round_num == 1 else f"deploy_round_{round_num}"
            self.state.update_step(task, step_name)

            deploy_result = await self._run_agent(
                task=task, step_name=step_name,
                role="devops_engineer", task_dir=task.dir,
                input_docs={"design": "02-design.md"},
                memories=[],
                output_file=task.dir / "08-deploy-report.md",
                project=task.project,
            )

            if deploy_result.data.get("status") == "success" or deploy_result.data.get("service_healthy", False):
                if deploy_result.data.get("needs_restart"):
                    reason = deploy_result.data.get("restart_reason", "核心模块有变更")
                    self.ui.print_warning(f"代码已提交，需要重启 secretary 服务（{reason}）")
                    await self._restart_secretary()
                self.ui.print_success("部署成功")
                return

            # 部署失败 — 最后一轮不再修复
            error_detail = deploy_result.data.get("error_detail", "未知错误")
            error_area = deploy_result.data.get("error_area", "backend")
            self.ui.print_warning(f"部署失败（第 {round_num}/{max_rounds} 轮）：{error_detail}")

            blocker = self._classify_deploy_infra_blocker(deploy_result.data)
            if blocker:
                self.ui.print_error(
                    "部署被环境/基础设施阻塞，已停止自动代码修复循环。"
                    f"分类: {blocker['category']}；原因: {blocker['reason']}。"
                    "请由人工或上层运行环境处理后重试部署。"
                )
                logger.warning(
                    "部署环境阻塞，停止 deploy fix loop: category=%s reason=%s detail=%s",
                    blocker["category"],
                    blocker["reason"],
                    error_detail,
                )
                return

            if round_num == max_rounds:
                self.ui.print_error(f"部署 {max_rounds} 轮仍失败，需要人工介入")
                return

            # 路由到开发者修复
            fix_role = "backend_developer" if error_area != "frontend" else "frontend_developer"
            self.ui.print_info(f"路由到 {fix_role} 修复部署问题...")
            fix_input_docs = {"deploy_report": "08-deploy-report.md", "design": "02-design.md"}
            if fix_role == "frontend_developer":
                fix_input_docs = await self._prepare_frontend_input_docs(
                    task,
                    fix_input_docs,
                    ensure_design_gate=False,
                )
            await self._run_agent(
                task=task, step_name=f"deploy_fix_{round_num}",
                role=fix_role, task_dir=task.dir,
                input_docs=fix_input_docs,
                memories=[],
                project=task.project,
            )

    def _classify_deploy_infra_blocker(self, deploy_data: dict) -> dict | None:
        """识别不可由 backend/frontend 代码修复的 deploy 环境阻塞。"""
        if not isinstance(deploy_data, dict):
            return None
        status = str(deploy_data.get("status") or "").strip().lower()
        error_area = str(deploy_data.get("error_area") or "").strip().lower().replace("-", "_")
        error_detail = str(deploy_data.get("error_detail") or "")
        detail_lower = error_detail.lower()
        service_healthy = deploy_data.get("service_healthy")

        area_categories = {
            "infra": "infra",
            "infrastructure": "infra",
            "environment": "infra",
            "env": "infra",
            "sandbox": "sandbox",
            "git": "git",
            "git_commit": "git",
            "commit": "git",
            "systemd": "systemd",
            "service_observability": "service-observability",
            "service_observation": "service-observability",
            "observability": "service-observability",
        }
        status_categories = {
            "infra_blocked": "infra",
            "infrastructure_blocked": "infra",
            "environment_blocked": "infra",
            "env_blocked": "infra",
            "sandbox_blocked": "sandbox",
            "git_blocked": "git",
            "commit_blocked": "git",
            "systemd_blocked": "systemd",
            "service_observability_blocked": "service-observability",
            "observability_blocked": "service-observability",
        }
        category = area_categories.get(error_area) or status_categories.get(status)
        if category:
            return {"category": category, "reason": error_detail or status or error_area}

        git_patterns = (
            ".git/index.lock",
            "read-only file system",
            "read only file system",
            "unable to create",
            "cannot lock ref",
            "index.lock",
        )
        if (".git" in detail_lower or "index.lock" in detail_lower) and any(p in detail_lower for p in git_patterns):
            return {"category": "git", "reason": "Git metadata is not writable in the deploy runtime"}

        sandbox_patterns = (
            "codex sandbox",
            "workspace-write",
            "sandbox",
            "cannot open netlink socket",
            "operation not permitted",
            "different pid namespace",
            "different network namespace",
            "host confirm_server",
        )
        confirm_server_unreachable = (
            "confirm_server" in detail_lower
            and (
                "failed to connect" in detail_lower
                or "connection refused" in detail_lower
                or "cannot connect" in detail_lower
                or "pid" in detail_lower
                or "listening" in detail_lower
                or "health" in detail_lower
            )
        )
        if confirm_server_unreachable and any(p in detail_lower for p in sandbox_patterns):
            return {"category": "sandbox", "reason": "Deploy runtime cannot observe the host confirm_server"}

        systemd_patterns = (
            "failed to connect to bus",
            "no medium found",
            "systemctl --user",
            "systemd bus",
            "dbus",
            "d-bus",
        )
        if any(p in detail_lower for p in systemd_patterns):
            return {"category": "systemd", "reason": "systemd user bus is unavailable in the deploy runtime"}

        observability_patterns = (
            "service observability",
            "service-observability",
            "service observation",
            "service visibility",
            "健康误报",
            "服务可观测",
            "看不到 pid",
            "看不到监听",
            "看不到 host",
            "host service",
            "host confirm_server",
        )
        if service_healthy is False and any(p in detail_lower for p in observability_patterns):
            return {"category": "service-observability", "reason": "Service health could not be observed from deploy runtime"}

        return None

    async def _restart_secretary(self):
        """部署后重启 secretary 服务，使用 nohup 延迟执行避免杀死自身进程树"""
        import subprocess
        self.ui.print_info("3 秒后重启 secretary 服务...")
        # 用 nohup + sleep 延迟重启，让当前进程有时间完成收尾
        subprocess.Popen(
            ["bash", "-c", "sleep 3 && systemctl --user restart vizo-secretary.service"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,  # 脱离当前进程组，不受父进程终止影响
        )

    # ─── 测试-修复循环 ────────────────────────────────────────

    async def _run_auto_regression(self, task) -> Path:
        """运行项目的自动化测试套件（如有），返回报告路径"""
        project_root = self.state.project_path
        report_file = task.dir / "06-auto-test.md"

        # 检测测试框架
        test_cmd = None
        if (project_root / "pytest.ini").exists() or (project_root / "pyproject.toml").exists():
            test_cmd = "python3 -m pytest --tb=short -q 2>&1 | head -200"
        elif (project_root / "package.json").exists():
            test_cmd = "npm test 2>&1 | head -200"
        elif (project_root / "Makefile").exists():
            test_cmd = "make test 2>&1 | head -200"

        if not test_cmd:
            logger.info("未检测到自动化测试框架，跳过 auto regression")
            return None

        try:
            result = subprocess.run(
                test_cmd, shell=True, cwd=str(project_root),
                capture_output=True, text=True, timeout=300,
            )
            output = result.stdout or result.stderr or "(无输出)"
            report = f"# 自动化测试结果\n\n"
            report += f"**命令**: `{test_cmd}`\n"
            report += f"**退出码**: {result.returncode}\n\n"
            report += f"```\n{output}\n```\n"
            report_file.write_text(report, encoding="utf-8")
            logger.info(f"自动化测试完成，退出码: {result.returncode}")
            return report_file
        except subprocess.TimeoutExpired:
            logger.warning("自动化测试超时（300s），跳过")
            return None
        except Exception as e:
            logger.warning(f"自动化测试执行失败: {e}")
            return None

    def _run_change_impact_analysis(self, task) -> Path:
        """分析改动文件的影响范围，返回报告路径"""
        report_file = task.dir / "06-change-impact.md"

        # 获取 restore_point → HEAD 之间的变更文件
        restore_point = task.restore_point
        if not restore_point:
            logger.info("无 restore_point，跳过变更影响分析")
            return None

        result = subprocess.run(
            ["git", "diff", "--name-only", restore_point, "HEAD"],
            cwd=str(self.state.project_path),
            capture_output=True, text=True,
        )
        if result.returncode != 0 or not result.stdout.strip():
            logger.info("无代码变更，跳过变更影响分析")
            return None

        changed_files = result.stdout.strip().split("\n")
        report_lines = ["# 变更影响分析\n"]
        report_lines.append(f"**restore point**: `{restore_point}`\n")
        report_lines.append(f"**改动文件数**: {len(changed_files)}\n")

        for f in changed_files[:30]:  # 限制分析文件数
            report_lines.append(f"\n## {f}\n")
            # 用 git grep 找引用该文件的地方（简化：按模块名搜索）
            module_name = Path(f).stem
            if module_name in ("__init__", "index", "main"):
                continue
            grep_result = subprocess.run(
                ["git", "grep", "-l", module_name, "--", "*.py", "*.js", "*.ts", "*.vue"],
                cwd=str(self.state.project_path),
                capture_output=True, text=True,
            )
            if grep_result.stdout.strip():
                refs = grep_result.stdout.strip().split("\n")
                # 排除自身
                refs = [r for r in refs if r != f][:10]
                if refs:
                    report_lines.append("**引用方**:\n")
                    for r in refs:
                        report_lines.append(f"- `{r}`\n")
                else:
                    report_lines.append("*无外部引用*\n")
            else:
                report_lines.append("*无外部引用*\n")

        report_file.write_text("".join(report_lines), encoding="utf-8")
        logger.info(f"变更影响分析完成，{len(changed_files)} 个文件")

    def _run_lint_check(self, task) -> Path | None:
        """对本任务修改的文件运行静态检查（ruff/tsc），返回报告路径"""
        scope_files = self._get_scope_files(task)
        if not scope_files:
            return None

        project_root = self.state.project_path
        sections = []

        # Python: ruff check
        py_files = [f for f in scope_files if f.endswith('.py')
                    and (project_root / f).exists()]
        if py_files and shutil.which("ruff"):
            try:
                result = subprocess.run(
                    ["ruff", "check", "--output-format=concise", "--no-fix"] + py_files,
                    cwd=str(project_root),
                    capture_output=True, text=True, timeout=30,
                )
                output = (result.stdout or "").strip()
                if output:
                    lines = output.split("\n")[:100]
                    sections.append(
                        f"## Python (ruff)\n\n"
                        f"检查了 {len(py_files)} 个文件，发现以下问题：\n\n"
                        f"```\n{chr(10).join(lines)}\n```\n"
                    )
            except (subprocess.TimeoutExpired, Exception) as e:
                logger.warning(f"ruff 检查失败: {e}")

        # TypeScript/JavaScript: tsc --noEmit (仅当项目有 tsconfig.json)
        ts_files = [f for f in scope_files if f.endswith(('.ts', '.tsx'))
                    and (project_root / f).exists()]
        if ts_files and (project_root / "tsconfig.json").exists() and shutil.which("npx"):
            try:
                result = subprocess.run(
                    ["npx", "tsc", "--noEmit", "--pretty", "false"],
                    cwd=str(project_root),
                    capture_output=True, text=True, timeout=60,
                )
                output = (result.stdout or "").strip()
                if output:
                    relevant = [l for l in output.split("\n")
                                if any(f in l for f in ts_files)][:100]
                    if relevant:
                        sections.append(
                            f"## TypeScript (tsc)\n\n"
                            f"发现以下类型错误：\n\n"
                            f"```\n{chr(10).join(relevant)}\n```\n"
                        )
            except (subprocess.TimeoutExpired, Exception) as e:
                logger.warning(f"tsc 检查失败: {e}")

        if not sections:
            return None

        report_file = task.dir / "06-lint-report.md"
        report = "# 静态代码检查报告\n\n"
        report += "以下问题由自动化工具检测，修复时请优先处理。\n\n"
        report += "\n".join(sections)
        report_file.write_text(report, encoding="utf-8")
        logger.info(f"Lint 检查完成，生成报告: {report_file.name}")
        return report_file
        return report_file

    def _get_current_commit_hash(self) -> str | None:
        """获取当前 HEAD commit hash"""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(self.state.project_path),
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None

    def _build_fix_impact_doc(self, fix_start_hash: str) -> str | None:
        """分析修复变更的影响范围，返回文档内容字符串（非文件路径）"""
        result = subprocess.run(
            ["git", "diff", "--name-only", fix_start_hash, "HEAD"],
            cwd=str(self.state.project_path),
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None

        fix_files = result.stdout.strip().split("\n")
        lines = ["# 修复变更影响分析\n"]
        lines.append(f"修复改动了 {len(fix_files)} 个文件：\n")
        for f in fix_files:
            lines.append(f"- `{f}`\n")

        lines.append("\n## 受影响模块（引用了修复文件的代码）\n")
        affected_modules = set()
        for f in fix_files[:20]:
            module_name = Path(f).stem
            if module_name in ("__init__", "index", "main"):
                continue
            grep_result = subprocess.run(
                ["git", "grep", "-l", module_name, "--", "*.py", "*.js", "*.ts", "*.vue"],
                cwd=str(self.state.project_path),
                capture_output=True, text=True, timeout=10,
            )
            if grep_result.stdout.strip():
                refs = [r for r in grep_result.stdout.strip().split("\n") if r != f][:10]
                for r in refs:
                    affected_modules.add(r)
                if refs:
                    lines.append(f"\n`{f}` 被以下文件引用：\n")
                    for r in refs:
                        lines.append(f"  - `{r}`\n")

        if affected_modules:
            lines.append(f"\n**总计 {len(affected_modules)} 个关联模块需要回归验证。**\n")
        else:
            lines.append("\n*修复文件无外部引用，回归风险低。*\n")

        lines.append("\n## 测试策略建议\n")
        lines.append("1. **必测**：上轮失败的测试用例（验证修复是否生效）\n")
        lines.append("2. **应测**：涉及上述关联模块的测试用例（检查回归）\n")
        lines.append("3. **可跳过**：上轮已通过且不涉及修复文件及关联模块的测试用例\n")

        return "".join(lines)

    def _get_scope_files(self, task) -> list[str] | None:
        """获取当前任务/子任务的修改文件范围。
        返回文件路径列表，或 None 表示无范围限制（全量测试）。

        数据源优先级：
        1. files_to_modify.json（架构师结构化输出）
        2. 子任务定义的 files_involved 字段
        3. git diff --name-only（实际改动文件）
        合并所有来源，去重后返回。
        """
        scope = set()

        # 来源1：files_to_modify.json
        ftm_file = task.dir / "files_to_modify.json"
        if ftm_file.exists():
            try:
                ftm = json.loads(ftm_file.read_text(encoding="utf-8"))
                for files in ftm.values():
                    if files:
                        scope.update(files)
            except (json.JSONDecodeError, OSError):
                pass

        # 来源2：子任务定义的 files_involved（从父任务的 sub_tasks 中查找）
        # 子任务 id 格式：{parent_id}-sub{N}
        if "-sub" in task.id:
            parent_dir = task.dir.parent
            state_file = parent_dir / "state.json"
            if state_file.exists():
                try:
                    parent_data = json.loads(state_file.read_text(encoding="utf-8"))
                    sub_id = task.id.rsplit("-sub", 1)[-1]
                    for sub in parent_data.get("sub_tasks", []):
                        if str(sub.get("id")) == sub_id:
                            scope.update(sub.get("files_involved", []))
                            break
                except (json.JSONDecodeError, OSError):
                    pass

        # 来源3：git diff（实际改动文件）
        if task.restore_point:
            try:
                result = subprocess.run(
                    ["git", "diff", "--name-only", task.restore_point, "HEAD"],
                    cwd=str(self.state.project_path),
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0 and result.stdout.strip():
                    scope.update(result.stdout.strip().split("\n"))
            except Exception:
                pass

        if scope:
            return list(scope)
        return None  # 无范围 → 降级为全量

    @staticmethod
    def _find_latest_test_report_name(task_dir: Path) -> str:
        """查找最后一轮测试报告的文件名。

        按轮次编号降序查找 07-test-report-round-{N}.md，
        找到第一个存在的文件即返回。兼容旧格式 07-test-report.md。
        """
        # 新格式：按轮次降序查找
        for n in range(10, 0, -1):
            name = f"07-test-report-round-{n}.md"
            if (task_dir / name).exists():
                return name
        # 兼容旧格式
        if (task_dir / "07-test-report.md").exists():
            return "07-test-report.md"
        # 都不存在，返回第 1 轮文件名（供写入用）
        return "07-test-report-round-1.md"

    @staticmethod
    def _extract_sub_tasks_from_design(design_file: Path) -> list:
        """从 02-design.md 文件中提取 sub_tasks。

        架构师的 claude -p 进程可能在写完文件后未返回 result 事件
        （超时/中断），导致 architect_result.data 中缺少 sub_tasks。
        此方法作为 fallback，从设计方案文件末尾的 JSON 块中提取。
        """
        if not design_file.exists():
            return []
        try:
            content = design_file.read_text(encoding="utf-8")
            blocks = re.findall(r'```json\s*\n(.*?)\n\s*```', content, re.DOTALL)
            if not blocks:
                return []
            # 从最后一个 JSON 块提取（架构师的 summary JSON 在文件末尾）
            data = json.loads(blocks[-1])
            sub_tasks = data.get("sub_tasks", [])
            if isinstance(sub_tasks, list) and sub_tasks:
                return sub_tasks
            return []
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return []

    @staticmethod
    def _extract_test_json_from_report(report_file: Path) -> dict | None:
        """从已有的 07-test-report.md 文件中提取 JSON 数据块。

        QA Agent 超时/跳过时，测试报告可能已写入文件但 JSON 未返回。
        此方法从 markdown 中查找最后一个 ```json 块并解析。
        """
        if not report_file.exists():
            return None
        try:
            content = report_file.read_text(encoding="utf-8")
            # 查找最后一个 ```json ... ``` 块
            import re
            blocks = re.findall(r'```json\s*\n(.*?)\n```', content, re.DOTALL)
            if not blocks:
                return None
            data = json.loads(blocks[-1])
            # 最低验证：必须有 total 或 all_passed 字段
            if "total" in data or "all_passed" in data:
                return data
            return None
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return None

    def _is_failure_in_scope(self, failure: dict, scope_files: list[str]) -> bool:
        """判断一个测试失败是否在修改范围内。

        匹配逻辑：failure["file"] 与 scope_files 中任一文件路径匹配。
        支持部分匹配（failure 的 file 字段可能是相对路径或绝对路径）。
        """
        fail_file = failure.get("file", "")
        if not fail_file:
            return True  # 无文件信息 → 保守判定为范围内

        fail_path = Path(fail_file)
        for sf in scope_files:
            sf_path = Path(sf)
            # 精确匹配或后缀匹配
            if fail_path == sf_path or str(fail_path).endswith(str(sf_path)) \
               or str(sf_path).endswith(str(fail_path)):
                return True
        return False

    @staticmethod
    def _flatten_qa_text(value) -> str:
        """Collect QA result text for lightweight blocker classification."""
        parts = []

        def collect(item):
            if item is None:
                return
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                for nested in item.values():
                    collect(nested)
            elif isinstance(item, (list, tuple, set)):
                for nested in item:
                    collect(nested)
            else:
                parts.append(str(item))

        collect(value)
        return "\n".join(parts).lower()

    @classmethod
    def _is_qa_environment_or_validator_failure(cls, failure: dict) -> bool:
        """Return True when a QA failure is about test setup, not product code."""
        if not isinstance(failure, dict):
            return False

        markers = {
            str(failure.get("area") or "").strip().lower().replace("-", "_"),
            str(failure.get("category") or "").strip().lower().replace("-", "_"),
            str(failure.get("type") or "").strip().lower().replace("-", "_"),
            str(failure.get("status") or "").strip().lower().replace("-", "_"),
        }
        if markers & {
            "environment",
            "env",
            "infra",
            "fixture",
            "test_fixture",
            "validator",
            "validation_script",
            "qa_environment",
            "browser_environment",
            "blocked",
            "environment_blocked",
            "fixture_blocked",
            "validator_blocked",
        }:
            return True

        text = cls._flatten_qa_text(failure)
        strong_patterns = (
            "cannot find module 'playwright'",
            'cannot find module "playwright"',
            "operation not permitted",
            "user cancelled mcp tool call",
            "browser_run_code_unsafe",
            "require is not defined",
            "node require",
            "chromium sandbox",
            "browser sandbox",
            "failed to connect to 127.0.0.1",
            "localhost",
            "confirm_server",
        )
        if any(pattern in text for pattern in strong_patterns):
            return True

        artifact_patterns = ("enoent", "no such file or directory")
        artifact_context = ("artifact", "artifacts", "screenshot", ".png")
        if any(pattern in text for pattern in artifact_patterns) and any(context in text for context in artifact_context):
            return True

        disabled_context = (
            "page.fill",
            "timeout 30000ms exceeded",
            "composerinput",
            "fixture",
            "session",
            "connection",
            "auth",
        )
        if "disabled" in text and any(context in text for context in disabled_context):
            return True

        return False

    @classmethod
    def _classify_qa_non_product_blocker(cls, test_data: dict, failures: list) -> dict | None:
        """Identify QA environment/fixture/validator blockers that should not start fix_round."""
        if not isinstance(test_data, dict) or test_data.get("all_passed", False):
            return None

        normalized_failures = [f for f in failures if isinstance(f, dict)]
        env_failures = [
            f for f in normalized_failures
            if cls._is_qa_environment_or_validator_failure(f)
        ]
        if normalized_failures:
            if len(env_failures) == len(normalized_failures):
                return {
                    "category": "qa-environment",
                    "reason": env_failures[0].get("detail")
                              or env_failures[0].get("test")
                              or "QA 环境/fixture/验证脚本阻塞",
                }
            return None

        status = str(test_data.get("status") or "").strip().lower().replace("-", "_")
        subtype = str(test_data.get("subtype") or "").strip().lower().replace("-", "_")
        category = str(test_data.get("category") or "").strip().lower().replace("-", "_")
        explicit_blocker = {
            status,
            subtype,
            category,
        } & {
            "blocked",
            "environment_blocked",
            "env_blocked",
            "fixture_blocked",
            "validator_blocked",
            "qa_environment_blocked",
            "browser_environment_blocked",
        }

        text = cls._flatten_qa_text({k: v for k, v in test_data.items() if k != "failures"})
        environment_patterns = (
            "环境阻塞",
            "浏览器环境",
            "fixture",
            "playwright",
            "browser",
            "sandbox",
            "cannot find module 'playwright'",
            "operation not permitted",
            "user cancelled mcp tool call",
            "require is not defined",
            "artifact",
            "screenshot",
            "disabled",
            "confirm_server",
            "failed to connect to 127.0.0.1",
        )
        if explicit_blocker or any(pattern in text for pattern in environment_patterns):
            return {
                "category": "qa-environment",
                "reason": test_data.get("blocked_reason")
                          or test_data.get("summary")
                          or "QA 环境/fixture/验证脚本阻塞",
            }
        return None

    # 服务文件集合：变更时触发 confirm_server 重启
    SERVICE_FILES = {
        "lib/confirm_server.py",
        "lib/web_console.py",
        "lib/mobile_console.py",
        "lib/pty_manager.py",
        "lib/preview_server.py",
    }

    def _check_and_restart_service(self, task: Task):
        """检测 git diff 是否包含服务文件变更，若包含则重启 confirm_server"""
        try:
            base_hash = task.step_checkpoints.get("requirement_analysis") or ""
            if not base_hash:
                return
            work_dir = self.agent._get_project_path(
                task.project or self.config.get("default_project", "")
            )
            result = subprocess.run(
                ["git", "diff", "--name-only", base_hash, "HEAD"],
                cwd=str(work_dir), capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return
            changed_files = set(result.stdout.strip().split("\n"))
            service_changed = changed_files & self.SERVICE_FILES
            if not service_changed:
                return

            logger.info(f"检测到服务文件变更: {service_changed}，重启 confirm_server")
            self.ui.print_info(
                f"检测到服务文件变更({', '.join(f.split('/')[-1] for f in service_changed)})，"
                "正在重启服务..."
            )
            # 直接 kill confirm_server，让 secretary 自动拉起
            subprocess.run(
                ["pkill", "-f", "confirm_server"],
                capture_output=True, timeout=10,
            )
            time.sleep(3)
            self.ui.print_success("confirm_server 已重启")
        except Exception as e:
            logger.warning(f"服务重启检测失败（不影响任务继续）: {e}")

    async def _test_fix_loop(self, task, max_rounds=3):
        # resume 兼容：如果测试已通过，跳过整个循环
        if "test_passed" in task.completed_steps:
            return

        # 收敛检测：跟踪每轮的 bug ID 集合
        prev_bug_ids = set()
        non_convergence_count = 0

        # 在第一轮 QA 前运行自动化测试和变更影响分析
        auto_test_file = await self._run_auto_regression(task)
        impact_file = self._run_change_impact_analysis(task)
        lint_file = self._run_lint_check(task)

        # 【新增】获取修改文件范围
        scope_files = self._get_scope_files(task)
        if scope_files:
            logger.info(f"测试隔离范围: {len(scope_files)} 个文件")

        prev_test_report = None  # 上轮测试报告内容（第1轮为 None，触发全量测试）
        fix_impact_doc = None     # 修复影响范围文档（第1轮为 None）

        # max_rounds 轮修复 + (max_rounds+1) 轮测试
        # 最后一轮测试是纯验证，失败才回滚
        total_test_rounds = max_rounds + 1
        for round_num in range(1, total_test_rounds + 1):
            # 当前轮次的测试报告文件名
            report_filename = f"07-test-report-round-{round_num}.md"

            # resume 时跳过已完成的测试轮次
            if f"test_round_{round_num}" in task.completed_steps:
                continue
            self.state.update_step(task, f"test_round_{round_num}")

            # QA 输入文档（条件化注入自动测试和影响分析）
            qa_input_docs = {"test_cases": "03-test-cases.md", "design": "02-design.md"}
            if auto_test_file and auto_test_file.exists():
                qa_input_docs["auto_test"] = "06-auto-test.md"
            if impact_file and impact_file.exists():
                qa_input_docs["change_impact"] = "06-change-impact.md"
            if lint_file and lint_file.exists():
                qa_input_docs["lint_report"] = "06-lint-report.md"

            # 精准回归：注入上轮测试报告 + 修复影响范围
            if round_num > 1 and prev_test_report:
                qa_input_docs["prev_test_report"] = prev_test_report
            if fix_impact_doc:
                qa_input_docs["fix_impact"] = fix_impact_doc

            # 【新增】将修改范围注入 QA 输入
            if scope_files:
                scope_doc = "# 本任务修改文件范围\n\n"
                scope_doc += "以下文件是本任务的修改范围。"
                scope_doc += "测试时请区分：范围内失败=bug，范围外失败=回归警告。\n\n"
                for f in scope_files:
                    scope_doc += f"- `{f}`\n"
                qa_input_docs["scope_files"] = scope_doc

            # 检测已有测试报告：仅第 1 轮 resume 时复用（fix 后的轮次必须重新测试）
            existing_report = task.dir / report_filename
            existing_data = None
            if round_num == 1:
                existing_data = self._extract_test_json_from_report(existing_report)
            if existing_data:
                logger.info(
                    f"复用已有测试报告（第 {round_num} 轮）："
                    f"{existing_data.get('passed', '?')}/{existing_data.get('total', '?')} 通过"
                )
                self.ui.print_info(
                    f"检测到已有测试报告（{existing_data.get('passed', '?')}/{existing_data.get('total', '?')} 通过），直接复用"
                )
                # 补发 progress 更新（让 progress.json 正确反映步骤状态）
                self._update_progress(task, "agent_start", role="qa_engineer",
                                      model="(cached)", step_name=f"test_round_{round_num}")
                self._update_progress(task, "agent_complete", role="qa_engineer",
                                      step_name=f"test_round_{round_num}")
                test_result = AgentResult(
                    success=existing_data.get("all_passed", False),
                    data=existing_data,
                    raw_output="", cost_tokens=0,
                    duration=0, exit_code=-1,
                )
            else:
                try:
                    test_result = await self._run_agent(
                        task=task, step_name=None,  # 不自动标记完成，等修复也完成后再标记
                        role="qa_engineer", task_dir=task.dir,
                        input_docs=qa_input_docs,
                        memories=[],
                        output_file=task.dir / report_filename,
                        project=task.project,
                    )
                except (AgentTimeoutError, AgentError) as e:
                    # QA 超时/崩溃但测试报告可能已写入 → 尝试从文件恢复
                    report_file = task.dir / report_filename
                    fallback_data = self._extract_test_json_from_report(report_file)
                    if fallback_data:
                        logger.warning(
                            f"QA 第 {round_num} 轮超时但测试报告已存在，从文件恢复"
                            f"（{fallback_data.get('passed', '?')}/{fallback_data.get('total', '?')} 通过）"
                        )
                        self.ui.print_warning(
                            f"QA 超时但测试报告已写入，从文件恢复继续"
                        )
                        test_result = AgentResult(
                            success=fallback_data.get("all_passed", False),
                            data=fallback_data,
                            raw_output="", cost_tokens=0,
                            duration=0, exit_code=-1,
                        )
                    else:
                        # 报告不存在或无法解析 → 原样抛出
                        raise

            # 【修复】skip 状态 fallback：从已有测试报告提取数据
            if test_result.data.get("status") == "skipped":
                report_file = task.dir / report_filename
                fallback_data = self._extract_test_json_from_report(report_file)
                if fallback_data:
                    self.ui.print_info(
                        f"QA 跳过但测试报告已存在，使用报告数据"
                        f"（{fallback_data.get('passed', '?')}/{fallback_data.get('total', '?')} 通过）"
                    )
                    test_result = AgentResult(
                        success=fallback_data.get("all_passed", False),
                        data=fallback_data,
                        raw_output=test_result.raw_output,
                        cost_tokens=test_result.cost_tokens,
                        duration=test_result.duration,
                        exit_code=test_result.exit_code,
                    )
                else:
                    self.ui.print_warning(
                        f"QA 第 {round_num} 轮跳过且无可用测试报告，重试中..."
                    )
                    continue

            # 【修改】三级判定逻辑
            all_passed = test_result.data.get("all_passed", False)
            # 【fallback】QA 摘要 JSON 缺少 all_passed 时，从测试报告文件补读
            if not all_passed and "all_passed" not in test_result.data:
                report_file = task.dir / report_filename
                fallback_data = self._extract_test_json_from_report(report_file)
                if fallback_data and "all_passed" in fallback_data:
                    all_passed = fallback_data["all_passed"]
                    logger.info(
                        f"QA 摘要 JSON 缺少 all_passed，从测试报告文件读取：{all_passed}"
                        f"（{fallback_data.get('passed', '?')}/{fallback_data.get('total', '?')} 通过）"
                    )
                    # 同步补全 test_result.data 供后续 failures 读取
                    test_result = AgentResult(
                        success=all_passed,
                        data={**test_result.data, **fallback_data},
                        raw_output=test_result.raw_output,
                        cost_tokens=test_result.cost_tokens,
                        duration=test_result.duration,
                        exit_code=test_result.exit_code,
                    )
            if all_passed:
                self.ui.print_success(f"测试通过（第 {round_num} 轮）")
                self.state.complete_step(task, f"test_round_{round_num}")
                task.completed_steps.append("test_passed")
                self.state._save_state(task)
                # 同步 completed_steps 到 progress.json + Redis
                self._update_progress(task, "confirm_resolved")
                return

            # QA 因 max_turns 未完成且无 failures 数据：重试 QA，不启动 fix
            subtype = test_result.data.get("subtype", "")
            failures = test_result.data.get("failures", [])

            non_product_blocker = self._classify_qa_non_product_blocker(
                test_result.data, failures
            )
            if non_product_blocker:
                reason = str(non_product_blocker.get("reason") or "")[:240]
                category = non_product_blocker.get("category", "qa-environment")
                self.ui.print_error(
                    f"QA 第 {round_num} 轮被{category}阻塞，未发现可行动产品缺陷，"
                    f"已停止自动代码修复循环：{reason}"
                )
                task.completed_steps.append("test_blocked")
                self.state._save_state(task)
                raise WorkflowError(
                    f"QA 环境/验证阻塞，停止测试-修复循环：{reason or category}"
                )

            # 【fallback】QA 未输出 failures 数组但测试未通过 → 合成 failure 条目
            # 防止修复开发者因 failures=[] 而永远不被调用
            if not failures and not all_passed:
                summary = test_result.data.get("summary", "测试失败，详见测试报告")
                logger.warning(f"QA 未输出 failures 数组（summary: {summary[:80]}），合成 fallback failure")
                failures = [{
                    "test": "fallback_from_report",
                    "area": "backend",
                    "file": "",
                    "detail": summary,
                }]

            if subtype == "error_max_turns" and not failures:
                self.ui.print_warning(
                    f"QA 第 {round_num} 轮未能完成测试（达到轮次上限），重试中..."
                )
                continue

            # 【收敛检测】提取当前轮的 bug ID
            current_bug_ids = set()
            for f in failures:
                # 使用测试用例 ID 或测试名称作为 bug 标识
                test_id = f.get("test") or f.get("id", "")
                if test_id:
                    current_bug_ids.add(test_id)

            # 检测是否与前轮 bug 集合相同
            if prev_bug_ids and current_bug_ids == prev_bug_ids:
                non_convergence_count += 1
                logger.warning(
                    f"检测到非收敛：第 {round_num} 轮 bug 与前轮完全相同 "
                    f"({len(current_bug_ids)} 个），非收敛计数 = {non_convergence_count}"
                )
                # 连续 2 轮相同 bug → 强制停止，避免无限循环
                if non_convergence_count >= 2:
                    self.ui.print_error(
                        f"测试-修复循环未收敛：连续 {non_convergence_count} 轮 "
                        f"发现相同 bug {list(current_bug_ids)[:3]}..."
                    )
                    task.completed_steps.append("test_failed")
                    self.state._save_state(task)
                    raise WorkflowError(
                        f"测试-修复循环未收敛：连续 {non_convergence_count} 轮发现相同 bug"
                    )
            else:
                # bug 集合变化，重置收敛计数
                non_convergence_count = 0

            # 更新 prev_bug_ids 供下轮使用
            prev_bug_ids = current_bug_ids

            # 【新增】过滤：只保留修改范围内的失败
            regression_warnings = test_result.data.get("regression_warnings", [])
            relevant_failures = failures  # 默认全部相关
            skipped_regressions = []
            if scope_files and failures:
                relevant_failures = []
                for f in failures:
                    if self._is_failure_in_scope(f, scope_files):
                        relevant_failures.append(f)
                    else:
                        skipped_regressions.append(f)
                if skipped_regressions:
                    logger.info(
                        f"过滤 {len(skipped_regressions)} 个非本任务范围的测试失败"
                    )

            # 【新增】条件通过判定：范围内无失败 + 通过率 >= 90%
            total = test_result.data.get("total", 0)
            passed_count = test_result.data.get("passed", 0)
            effective_pass_rate = passed_count / total if total > 0 else 0

            if not relevant_failures and effective_pass_rate >= 0.9:
                # passed_with_warnings
                warning_count = len(skipped_regressions) + len(regression_warnings)
                self.ui.print_success(
                    f"测试条件通过（第 {round_num} 轮）：通过率 "
                    f"{effective_pass_rate:.1%}，{warning_count} 个回归警告"
                )
                self.state.complete_step(task, f"test_round_{round_num}")
                task.completed_steps.append("test_passed")
                self.state._save_state(task)
                return

            # 【修改】使用过滤后的失败列表
            failures_for_fix = relevant_failures if scope_files else failures
            if not failures_for_fix:
                if effective_pass_rate >= 0.9:
                    # 所有失败都被过滤 + 通过率够高 → 条件通过
                    self.ui.print_success(
                        f"测试条件通过（第 {round_num} 轮）：所有失败均在范围外"
                    )
                    self.state.complete_step(task, f"test_round_{round_num}")
                    task.completed_steps.append("test_passed")
                    self.state._save_state(task)
                    return
                else:
                    # 通过率太低，所有失败在范围外 → 降级为全量修复（不直接判死）
                    self.ui.print_warning(
                        f"通过率 {effective_pass_rate:.1%} 低于 90% 阈值，"
                        f"所有 {len(failures)} 个失败均在范围外，降级为全量修复"
                    )
                    failures_for_fix = failures  # 用全量 failures 进入修复循环

            self.ui.print_warning(f"测试未通过（第 {round_num}/{total_test_rounds} 轮），启动修复")

            if round_num == total_test_rounds:
                self.ui.print_error(f"测试 {total_test_rounds} 轮（含 {max_rounds} 轮修复）未通过，回滚代码")
                self.state.rollback(task)
                raise WorkflowError(f"测试 {total_test_rounds} 轮未通过")

            # 记录修复前 commit，用于计算修复 diff
            fix_start_hash = self._get_current_commit_hash()

            # 【改进】角色升级：重复 bug 升级为全栈 fix_engineer
            escalate_to_fullstack = (round_num > 1 and non_convergence_count > 0)
            if escalate_to_fullstack:
                logger.info(
                    f"第 {round_num} 轮修复：bug 与前轮相同，升级为 fix_engineer（全栈）"
                )

            # 【改进】构建修复上下文，注入上轮修复报告
            base_fix_input = {
                "test_report": report_filename,
                "design": "02-design.md",
            }
            lint_report = task.dir / "06-lint-report.md"
            if lint_report.exists():
                base_fix_input["lint_report"] = "06-lint-report.md"
            if round_num > 1:
                for pf in sorted(task.dir.glob(f"07-fix-round-{round_num - 1}-*.md")):
                    base_fix_input[f"prev_fix_{pf.stem}"] = pf.name
                    logger.info(f"注入上轮修复报告: {pf.name}")

            # 按 bug 领域分组修复（使用过滤后的列表）
            failures_by_area = {}
            for f in failures_for_fix:
                area = f.get("area", "unknown")
                failures_by_area.setdefault(area, []).append(f)

            # 多领域串行修复，使用 worktree 隔离
            if len(failures_by_area) > 1:
                worktrees = await self.state.create_worktrees(
                    task, list(failures_by_area.keys())
                )
                try:
                    for area, bugs in failures_by_area.items():
                        role = "fix_engineer" if escalate_to_fullstack else BUG_AREA_TO_ROLE.get(area, "fix_engineer")
                        wt = worktrees[area]
                        await self._run_agent(
                            task=task, step_name=f"fix_round_{round_num}",
                            role=role, task_dir=task.dir,
                            input_docs={**base_fix_input, "bugs_to_fix": json.dumps(bugs, ensure_ascii=False)},
                            memories=[],
                            output_file=task.dir / f"07-fix-round-{round_num}-{area}.md",
                            project=task.project,
                            cwd=wt.path,
                        )
                    # L1: 检测文件交叉
                    overlap = await self._check_file_overlap(worktrees)
                    if overlap:
                        logger.warning(f"bug fix 文件交叉检测: {overlap}，跳过合并直接串行降级")
                        await self.state.cleanup_worktrees(worktrees)
                        for area, bugs in failures_by_area.items():
                            role = "fix_engineer" if escalate_to_fullstack else BUG_AREA_TO_ROLE.get(area, "fix_engineer")
                            await self._run_agent(
                                task=task, step_name=f"fix_round_{round_num}",
                                role=role, task_dir=task.dir,
                                input_docs={**base_fix_input, "bugs_to_fix": json.dumps(bugs, ensure_ascii=False)},
                                memories=[],
                                output_file=task.dir / f"07-fix-round-{round_num}-{area}.md",
                                project=task.project,
                            )
                        worktrees = None  # 已清理
                    else:
                        # 无交叉，正常合并
                        try:
                            await self.state.merge_worktrees(
                                task, worktrees,
                                conflict_resolver=self._resolve_merge_conflict
                            )
                        except (WorkflowError, subprocess.CalledProcessError):
                            logger.warning("bug fix 合并失败，L3 串行降级")
                            await self.state.cleanup_worktrees(worktrees)
                            for area, bugs in failures_by_area.items():
                                role = "fix_engineer" if escalate_to_fullstack else BUG_AREA_TO_ROLE.get(area, "fix_engineer")
                                await self._run_agent(
                                    task=task, step_name=f"fix_round_{round_num}",
                                    role=role, task_dir=task.dir,
                                    input_docs={**base_fix_input, "bugs_to_fix": json.dumps(bugs, ensure_ascii=False)},
                                    memories=[],
                                    output_file=task.dir / f"07-fix-round-{round_num}-{area}.md",
                                    project=task.project,
                                )
                            worktrees = None
                except Exception:
                    if worktrees:
                        await self.state.cleanup_worktrees(worktrees)
                    raise
            else:
                # 单领域直接修复，无需 worktree
                for area, bugs in failures_by_area.items():
                    role = "fix_engineer" if escalate_to_fullstack else BUG_AREA_TO_ROLE.get(area, "fix_engineer")
                    await self._run_agent(
                        task=task, step_name=f"fix_round_{round_num}",
                        role=role, task_dir=task.dir,
                        input_docs={**base_fix_input, "bugs_to_fix": json.dumps(bugs, ensure_ascii=False)},
                        memories=[],
                        output_file=task.dir / f"07-fix-round-{round_num}-{area}.md",
                        project=task.project,
                    )

            # 保存本轮测试报告供下轮 QA 参考
            report_file = task.dir / report_filename
            if report_file.exists():
                prev_test_report = report_file.read_text(encoding="utf-8")

            # 计算修复影响范围
            if fix_start_hash:
                fix_impact_doc = self._build_fix_impact_doc(fix_start_hash)

            # 整个轮次（测试+修复）完成，标记 test_round_N
            self.state.complete_step(task, f"test_round_{round_num}")

    # ─── 知识沉淀 ─────────────────────────────────────────────

    async def _accumulate_knowledge(self, task):
        """知识沉淀：三级门控 + 智能选材 + 提案-审核双角色"""
        if "knowledge" in task.completed_steps:
            return
        self.state.update_step(task, "knowledge")

        # Gate 1: 任务状态
        if task.status in ("rolled_back", "pending"):
            logger.info(f"知识沉淀跳过：任务状态为 {task.status}")
            return

        # Gate 2: 任务类型
        KNOWLEDGE_WORTHY_TYPES = {"new_feature", "refactor", "debug_embedded", "bug_fix"}
        if task.task_type not in KNOWLEDGE_WORTHY_TYPES:
            logger.info(f"知识沉淀跳过：任务类型 {task.task_type} 不在允许集合中")
            return

        # Gate 3: 文档丰富度
        if not (task.dir / "02-design.md").exists():
            logger.info("知识沉淀跳过：02-design.md 不存在")
            return

        # 智能选材
        selected_docs = self._select_knowledge_input(task.dir)
        if not selected_docs:
            logger.warning("知识沉淀跳过：无可用文档")
            return

        # Phase 1: knowledge_engineer 生成提案
        proposal_file = task.dir / "08-knowledge-updates.md"
        await self._run_agent(
            task=task, step_name="knowledge",
            role="knowledge_engineer", task_dir=task.dir,
            input_docs=selected_docs,
            memories=[],
            output_file=proposal_file,
            project=task.project,
        )

        # 检查提案文件
        if not proposal_file.exists():
            logger.warning("knowledge_engineer 未生成 08-knowledge-updates.md，跳过 admin")
            self.state.complete_step(task, "knowledge")
            return
        proposal_content = proposal_file.read_text(encoding="utf-8").strip()
        if not proposal_content:
            logger.warning("08-knowledge-updates.md 为空，跳过 admin")
            self.state.complete_step(task, "knowledge")
            return

        # Phase 2: knowledge_admin 审核并写入
        await self._run_agent(
            task=task, step_name="knowledge_review",
            role="knowledge_admin", task_dir=task.dir,
            input_docs={"knowledge_proposal": proposal_content},
            memories=[],
            output_file=task.dir / "09-knowledge-review.md",
            project=task.project,
        )

        # 两个 agent 都完成后标记步骤完成
        self.state.complete_step(task, "knowledge")

    def _select_knowledge_input(self, task_dir: Path) -> dict:
        """按优先级选取知识沉淀的输入文档，控制总字符预算"""
        PRIORITY_FILES = [
            ("02-design.md", 50000),
            (self._find_latest_test_report_name(task_dir), 25000),
            ("01-prd.md", 20000),
            ("06-integration-report.md", 15000),
            ("04-backend-result.md", 12000),
            ("05-frontend-result.md", 12000),
        ]

        # 扫描子任务目录的交接和设计文档
        SUB_TASK_FILES = [
            ("handoff.md", 8000),
            ("02-design.md", 6000),
            ("04-backend-result.md", 4000),
        ]

        # 大任务预算更高
        has_sub_tasks = any(task_dir.glob("sub-*"))
        TOTAL_BUDGET = 150000 if has_sub_tasks else 120000
        MIN_REMAINING = 2000

        selected = {}
        used = 0

        # 主任务文档
        for filename, max_chars in PRIORITY_FILES:
            remaining = TOTAL_BUDGET - used
            if remaining < MIN_REMAINING:
                break
            filepath = task_dir / filename
            if not filepath.exists():
                continue
            try:
                content = filepath.read_text(encoding="utf-8")
            except OSError:
                continue
            budget = min(max_chars, remaining)
            selected[filename] = content[:budget]
            used += len(selected[filename])

        # 子任务文档（按子目录排序）
        if has_sub_tasks:
            sub_dirs = sorted(task_dir.glob("sub-*"))
            for sub_dir in sub_dirs:
                if not sub_dir.is_dir():
                    continue
                for filename, max_chars in SUB_TASK_FILES:
                    remaining = TOTAL_BUDGET - used
                    if remaining < MIN_REMAINING:
                        break
                    filepath = sub_dir / filename
                    if not filepath.exists():
                        continue
                    try:
                        content = filepath.read_text(encoding="utf-8")
                    except OSError:
                        continue
                    key = f"{sub_dir.name}/{filename}"
                    budget = min(max_chars, remaining)
                    selected[key] = content[:budget]
                    used += len(selected[key])

        if not selected:
            logger.warning(f"任务目录 {task_dir} 中无可用知识文档")
        return selected

    # ─── 失败报告 ─────────────────────────────────────────────

    async def _generate_failure_report(self, task, error):
        report = {
            "task_id": task.id,
            "description": task.description,
            "failed_at_step": task.current_step,
            "error_type": type(error).__name__,
            "error_message": str(error),
            "completed_steps": task.completed_steps,
            "possible_causes": self._analyze_failure_cause(error),
            "timestamp": datetime.now().isoformat(),
        }
        try:
            task.dir.mkdir(parents=True, exist_ok=True)
            report_file = task.dir / "99-failure-report.md"
            report_file.write_text(self._format_failure_report(report), encoding="utf-8")
        except OSError as e:
            logger.error(f"无法写入失败报告: {e}")

        summary = (
            f"任务失败：{task.description}\n"
            f"失败步骤：{task.current_step}\n"
            f"错误类型：{type(error).__name__}\n"
            f"详细报告：{report_file}"
        )
        self.ui.print_error(summary)
        return report

    # 默认必须确认的步骤（恢复任务时 auto_confirm 不跳过这些步骤）
    _DEFAULT_CONFIRM_STEPS = {"requirement_analysis": True, "pm_prd": True}

    def _step_requires_confirm(self, step_name: str) -> bool:
        """判断当前步骤是否必须用户确认（不受 auto_confirm 影响）

        config.json 中 confirm_required_steps 支持两种格式：
        - dict: {"requirement_analysis": true, "pm_prd": true, "architect": false}
        - list: ["requirement_analysis", "pm_prd"]（向后兼容）
        """
        required = self.config.get("confirm_required_steps", self._DEFAULT_CONFIRM_STEPS)
        if isinstance(required, dict):
            return bool(required.get(step_name, False))
        return step_name in required

    def _analyze_failure_cause(self, error):
        causes = []
        error_str = str(error)
        if "用户主动终止" in error_str:
            causes.append("用户手动终止了任务执行")
        elif isinstance(error, AgentTimeoutError):
            causes.append("Agent 执行超时，可能任务过于复杂或模型响应慢")
        elif "测试" in error_str:
            causes.append("代码逻辑错误，3轮修复未能解决")
        elif "合并冲突" in error_str:
            causes.append("并行开发的文件存在重叠修改")
        return causes or ["未知原因，请查看 logs/ 目录下的 Agent 原始输出"]

    def _format_failure_report(self, report: dict) -> str:
        steps = '\n'.join(f'- {s}' for s in report['completed_steps'])
        causes = '\n'.join(f'- {c}' for c in report['possible_causes'])
        is_terminated = "用户主动终止" in report.get('error_message', '')
        if is_terminated:
            suggestions = "- 使用 `opus --resume --task-id {task_id}` 从中断处继续\n- 如需重新开始，提交新任务即可".format(task_id=report['task_id'])
        else:
            suggestions = "- 查看 logs/ 目录下的 Agent 原始输出\n- 检查设计方案是否过于复杂\n- 尝试简化需求后重新执行"
        return f"""# 任务失败报告

## 基本信息
- 任务ID: {report['task_id']}
- 描述: {report['description']}
- 失败步骤: {report['failed_at_step']}
- 错误类型: {report['error_type']}
- 时间: {report['timestamp']}

## 错误信息
{report['error_message']}

## 已完成步骤
{steps}

## 可能原因
{causes}

## 建议
{suggestions}
"""

    # ─── 其他工作流 ──────────────────────────────────────────

    async def _workflow_quick_fix(self, task: Task):
        """快速修复流：架构师定位 → 修复 → 测试 → 部署"""
        # --- 架构设计（已在 run() 中完成，自动跳过） ---
        if "architect" not in task.completed_steps:
            logger.warning("architect 步骤未在 run() 中完成，补执行")
            self.state.update_step(task, "architect")
            await self._run_agent(
                task=task, step_name="architect",
                role="architect",
                task_dir=task.dir,
                input_docs={"prd": "01-prd.md"},
                memories=[],
                output_file=task.dir / "02-design.md",
                project=task.project,
            )
        else:
            logger.info("跳过已完成步骤: architect")

        if "fix" not in task.completed_steps:
            self.state.update_step(task, "fix")
            await self._run_agent(
                task=task, step_name="fix",
                role="fix_engineer",
                task_dir=task.dir,
                input_docs={"prd": "01-prd.md", "design": "02-design.md"},
                memories=[],
                output_file=task.dir / "04-fix-result.md",
                project=task.project,
            )

        # 测试-修复循环（QA 会自动生成 test cases）
        if "qa_engineer" not in task.completed_steps:
            self.state.update_step(task, "qa_engineer")
            await self._run_agent(
                task=task, step_name="qa_engineer",
                role="qa_engineer",
                task_dir=task.dir,
                input_docs={"prd": "01-prd.md", "design": "02-design.md"},
                memories=[],
                output_file=task.dir / "03-test-cases.md",
                project=task.project,
            )
        self._check_and_restart_service(task)
        await self._test_fix_loop(task, max_rounds=3)

        if "deploy" not in task.completed_steps:
            await self._deploy_fix_loop(task, "修复完成，测试通过。是否部署？")

        await self._accumulate_knowledge(task)

    async def _workflow_refactor(self, task: Task):
        """重构优化流：架构师分析 → 逐步重构 → 回归测试"""
        # --- 架构设计（已在 run() 中完成，自动跳过） ---
        if "architect" not in task.completed_steps:
            logger.warning("architect 步骤未在 run() 中完成，补执行")
            self.state.update_step(task, "architect")
            await self._run_agent(
                task=task, step_name="architect",
                role="architect",
                task_dir=task.dir,
                input_docs={"prd": "01-prd.md"},
                memories=[],
                output_file=task.dir / "02-design.md",
                project=task.project,
            )
        else:
            logger.info("跳过已完成步骤: architect")

        if "refactor" not in task.completed_steps:
            self.state.update_step(task, "refactor")
            await self._run_agent(
                task=task, step_name="refactor",
                role="backend_developer",
                task_dir=task.dir,
                input_docs={"prd": "01-prd.md", "design": "02-design.md"},
                memories=[],
                output_file=task.dir / "04-refactor-result.md",
                project=task.project,
            )

        if "qa_engineer" not in task.completed_steps:
            self.state.update_step(task, "qa_engineer")
            await self._run_agent(
                task=task, step_name="qa_engineer",
                role="qa_engineer",
                task_dir=task.dir,
                input_docs={"prd": "01-prd.md", "design": "02-design.md"},
                memories=[],
                output_file=task.dir / "03-test-cases.md",
                project=task.project,
            )
        self._check_and_restart_service(task)
        await self._test_fix_loop(task, max_rounds=3)

        if "deploy" not in task.completed_steps:
            await self._deploy_fix_loop(task, "重构完成，回归测试通过。是否部署？")

        await self._accumulate_knowledge(task)

    async def _workflow_embedded(self, task: Task):
        """嵌入式调试流：单个 Agent 一口气完成"""
        if "embedded" not in task.completed_steps:
            self.state.update_step(task, "embedded")
            await self._run_agent(
                task=task, step_name="embedded",
                role="embedded_engineer",
                task_dir=task.dir,
                input_docs={"prd": "01-prd.md"},
                memories=[],
                output_file=task.dir / "04-embedded-result.md",
                project=task.project,
                timeout=900,
            )

        await self._accumulate_knowledge(task)

    # ─── 多任务项目流 ─────────────────────────────────────────

    async def _run_multi_task_workflow(self, task: Task, pm_result: dict = None):
        """大需求的多任务项目流"""
        # 1. 获取子任务清单（优先级：持久化 > architect_result > pm_result > 空）
        sub_tasks = task.sub_tasks
        if not sub_tasks:
            source = pm_result or {}
            sub_tasks = source.get("sub_tasks", [])
        if sub_tasks:
            sub_tasks = self.state.validate_sub_tasks(sub_tasks)
            task.sub_tasks = sub_tasks
            self.state._save_state(task)

        if not sub_tasks:
            # 最后尝试从 02-design.md 文件提取
            sub_tasks = self._extract_sub_tasks_from_design(task.dir / "02-design.md")
            if sub_tasks:
                logger.info(f"从 02-design.md 恢复 {len(sub_tasks)} 个子任务")
                sub_tasks = self.state.validate_sub_tasks(sub_tasks)
                task.sub_tasks = sub_tasks
                self.state._save_state(task)

        if not sub_tasks:
            raise WorkflowError("大需求无子任务清单，无法执行多任务流")

        # 2. 按依赖顺序执行子任务（跳过已完成的）
        batches = topological_sort_into_batches(sub_tasks)
        total_batches = len(batches)
        failed_subs = []

        for batch_idx, batch in enumerate(batches):
            batch_num = batch_idx + 1

            # 批次间确认检查点（仅企微模式下触发，终端模式自治）
            if batch_idx > 0 and total_batches > 2 and self.ui.mode == "wecom":
                batch_summary = self._generate_batch_summary(
                    batches, batch_idx, sub_tasks,
                )
                _ctx = self._build_confirm_context(task, button_set="doc_review")
                self.state.set_pending_confirm(task, "", "confirm_with_feedback",
                                               f"阶段 {batch_num}/{total_batches} 即将开始",
                                               button_set=_ctx.button_set)
                self._update_progress(task, "waiting_confirm",
                                      pending_confirm=task.pending_confirm)
                action = await self.ui.confirm_with_feedback(
                    f"阶段 {batch_num}/{total_batches} 即将开始\n\n{batch_summary}",
                    context=_ctx,
                )
                self.state.clear_pending_confirm(task)
                self._update_progress(task, "confirm_resolved")
                if action == "cancel":
                    raise WorkflowError(f"用户在阶段 {batch_num} 取消")
                if action == "feedback":
                    feedback = await self.ui.get_user_feedback()
                    logger.info(f"用户对阶段 {batch_num} 的意见: {feedback}")

            self.ui.print_info(f"执行第 {batch_num}/{total_batches} 批（{len(batch)} 个子任务）")

            # 过滤出本批次可执行的子任务（跳过已完成和依赖失败的）
            ready_subs = []
            for sub in batch:
                if str(sub.get("id")) in [str(x) for x in task.completed_sub_tasks]:
                    logger.info(f"跳过已完成子任务: {sub.get('name', sub.get('id'))}")
                    continue
                failed_deps = [
                    d for d in sub.get("depends_on", [])
                    if any(f.get("id") == d for f in failed_subs)
                ]
                if failed_deps:
                    logger.warning(f"跳过子任务 {sub.get('name')}：依赖 {failed_deps} 已失败")
                    sub["status"] = "skipped"
                    failed_subs.append(sub)
                    continue
                ready_subs.append(sub)

            if not ready_subs:
                logger.info(f"批次 {batch_num} 无可执行子任务，跳过")
                continue

            # 判断是否可并行执行（所有子任务 execution_order 都不是 serial）
            can_parallel = len(ready_subs) > 1 and all(
                s.get("execution_order", "serial") != "serial" for s in ready_subs
            )

            batch_failed = False
            if can_parallel:
                # 并行执行同一批次子任务
                self.ui.print_info(f"批次 {batch_num} 并行执行 {len(ready_subs)} 个子任务")
                results = await asyncio.gather(
                    *[self._execute_sub_task(task, sub) for sub in ready_subs],
                    return_exceptions=True,
                )
                for sub, result in zip(ready_subs, results):
                    if isinstance(result, Exception):
                        logger.error(f"子任务 {sub.get('name')} 并行异常: {result}")
                        failed_subs.append(sub)
                        batch_failed = True
                    elif result:
                        task.completed_sub_tasks.append(sub["id"])
                    else:
                        failed_subs.append(sub)
                        batch_failed = True
                    self.state._save_state(task)
            else:
                # 串行执行
                for sub in ready_subs:
                    success = await self._execute_sub_task(task, sub)
                    if success:
                        task.completed_sub_tasks.append(sub["id"])
                    else:
                        failed_subs.append(sub)
                        batch_failed = True
                        break  # 串行失败后停止后续子任务
                    self.state._save_state(task)

            # 子任务失败 → 立即标记父任务为 partially_failed，停止后续批次
            if batch_failed:
                failed_names = ", ".join(
                    s.get("name", s.get("id", "?")) for s in failed_subs
                )
                task.status = "partially_failed"
                self.state._save_state(task)
                self.ui.print_error(
                    f"子任务失败: {failed_names}\n"
                    f"父任务已标记为 partially_failed\n"
                    f"可通过 opus --resume 恢复任务并重试失败的子任务"
                )
                # 推送一次清晰的企微消息
                await self.ui._wecom_send(
                    f"⚠️ 任务部分失败\n"
                    f"失败子任务: {failed_names}\n"
                    f"请通过 opus --resume --task-id {task.id} 恢复或终止",
                    append_tips=False,
                )
                return  # 直接返回，不执行后续批次和部署

            # 阶段完成通知（企微模式下推送）
            if self.ui.mode == "wecom" and batch_idx < total_batches - 1:
                batch_names = ", ".join(s.get("name", f"#{s.get('id', '?')}") for s in batch)
                await self.ui._wecom_send(
                    f"📦 阶段 {batch_num}/{total_batches} 完成\n"
                    f"已完成: {batch_names}\n"
                    f"剩余: {total_batches - batch_num} 个阶段",
                    append_tips=True,
                )

        # 3. 所有批次成功完成
        self.ui.print_success(f"所有 {len(sub_tasks)} 个子任务执行完成")

        # 4. 全量集成测试
        if "final_integration_test" not in task.completed_steps:
            self.state.update_step(task, "final_integration_test")
            await self._run_agent(
                task=task, step_name="final_integration_test",
                role="qa_engineer",
                task_dir=task.dir,
                input_docs={"design": "02-design.md"},
                memories=[],
                project=task.project,
                output_file=task.dir / "99-final-test-report.md",
            )

        # 5. 部署
        if "deploy" not in task.completed_steps:
            await self._deploy_fix_loop(task, "全量测试通过，是否部署？")

        await self._accumulate_knowledge(task)

    @staticmethod
    def _generate_batch_summary(batches: list, current_idx: int, sub_tasks: list) -> str:
        """生成阶段摘要 markdown（优化5b）"""
        lines = []

        # 已完成的阶段
        completed_names = []
        for i in range(current_idx):
            for s in batches[i]:
                completed_names.append(s.get("name", f"#{s.get('id', '?')}"))
        if completed_names:
            lines.append(f"✅ 已完成 ({len(completed_names)}): {', '.join(completed_names)}")

        # 当前阶段
        current_names = [s.get("name", f"#{s.get('id', '?')}") for s in batches[current_idx]]
        lines.append(f"🔄 当前阶段 ({len(current_names)}): {', '.join(current_names)}")

        # 剩余阶段
        remaining_count = 0
        for i in range(current_idx + 1, len(batches)):
            remaining_count += len(batches[i])
        if remaining_count > 0:
            lines.append(f"⏳ 剩余: {remaining_count} 个子任务")

        return "\n".join(lines)

    async def _execute_sub_task(self, parent_task: Task, sub_task_def: dict) -> bool:
        """执行一个子任务，失败时回滚自身代码但不传播异常。返回 True=成功, False=失败"""
        sub_id = sub_task_def.get("id", "")
        self._current_parent_task_id = parent_task.id
        self._current_sub_id = sub_id

        sub_task = self.state.create_sub_task(parent_task, sub_task_def)

        # 设置父子任务 context（用于 step_action 双发）
        sub_id = sub_task_def.get("id", "")
        self._current_parent_task_id = parent_task.id
        self._current_sub_id = sub_id

        try:
            # 更新主任务 progress.json（让浏览器面板感知子任务活动）
            sub_name = sub_task_def.get("name", sub_task_def["id"])
            self._update_progress(parent_task, "agent_start",
                                  role=f"sub-task:{sub_name}", model="multi")
            await self._push_step_card(parent_task, "agent_start",
                                       role=f"sub-task:{sub_name}")

            # 写入子任务范围清单（让开发者明确知道自己只需做什么）
            scope_file = sub_task.dir / "00-sub-task-scope.md"
            if not scope_file.exists():
                sub_id = sub_task_def.get("id", "?")
                sub_desc = sub_task_def.get("description", "")
                files_involved = sub_task_def.get("files_involved", [])
                verify = sub_task_def.get("verify_method", "")
                scope_lines = [
                    f"# 子任务 {sub_id}: {sub_name}",
                    "",
                    "> **重要：你只需要完成本文件描述的范围，不要实现其他子任务的内容。**",
                    "> 完整的 PRD 和设计文档仅供参考上下文，你的实际工作范围以本文件为准。",
                    "",
                    "## 任务描述",
                    "",
                    sub_desc,
                    "",
                ]
                if files_involved:
                    scope_lines += [
                        "## 需要修改的文件",
                        "",
                        *[f"- `{f}`" for f in files_involved],
                        "",
                    ]
                if verify:
                    scope_lines += [
                        "## 验证方法",
                        "",
                        verify,
                        "",
                    ]
                # 列出其他子任务（让开发者知道不属于自己的范围）
                all_subs = parent_task.sub_tasks or []
                other_subs = [s for s in all_subs if s.get("id") != sub_id]
                if other_subs:
                    scope_lines += [
                        "## 其他子任务（仅列出，不要实现）",
                        "",
                        *[f"- ~~{s.get('name', s.get('id'))}~~" for s in other_subs],
                        "",
                    ]
                scope_file.write_text("\n".join(scope_lines), encoding="utf-8")

            # 用聚焦内容覆盖子任务的 02-design.md（原文件是父任务的全量设计，包含所有子任务）
            # 开发者只需看到自己子任务的设计，避免被无关内容干扰导致空跑
            # 但如果 architect 步骤已完成，说明 design.md 是架构师产出的完整方案，不覆盖
            focused_design = sub_task.dir / "02-design.md"
            architect_done = "architect" in (sub_task.completed_steps or [])
            if focused_design.exists() and not architect_done:
                sub_id_for_design = sub_task_def.get("id", "?")
                sub_desc_for_design = sub_task_def.get("description", "")
                files_for_design = sub_task_def.get("files_involved", [])
                verify_for_design = sub_task_def.get("verify_method", "")
                design_lines = [
                    f"# 技术设计：{sub_name}",
                    "",
                    "> **范围限定：你只需要实现本文件描述的功能。**",
                    "",
                    "## 任务描述",
                    "",
                    sub_desc_for_design,
                    "",
                ]
                if files_for_design:
                    design_lines += [
                        "## 需要修改的文件",
                        "",
                        *[f"- `{f}`" for f in files_for_design],
                        "",
                    ]
                if verify_for_design:
                    design_lines += [
                        "## 验证方法",
                        "",
                        verify_for_design,
                        "",
                    ]
                # 上游依赖说明
                depends = sub_task_def.get("depends_on", [])
                if depends:
                    all_subs_map = {s.get("id"): s.get("name", s.get("id"))
                                    for s in (parent_task.sub_tasks or [])}
                    dep_names = [all_subs_map.get(d, d) for d in depends]
                    design_lines += [
                        "## 前置依赖",
                        "",
                        *[f"- {n}（已完成）" for n in dep_names],
                        "",
                    ]
                focused_design.write_text("\n".join(design_lines), encoding="utf-8")

            # 收集上游子任务的 handoff 数据，注入到子任务目录
            depends_on = sub_task_def.get("depends_on", [])
            if depends_on:
                handoff_parts = []
                for dep_id in depends_on:
                    dep_dir = parent_task.dir / f"sub-{dep_id}"
                    handoff_file = dep_dir / "handoff.md"
                    if handoff_file.exists():
                        handoff_parts.append(
                            f"## 上游子任务 {dep_id} 的交接信息\n\n"
                            + handoff_file.read_text(encoding="utf-8")
                        )
                if handoff_parts:
                    upstream_handoff = "\n\n---\n\n".join(handoff_parts)
                    (sub_task.dir / "upstream-handoff.md").write_text(
                        f"# 上游交接信息\n\n{upstream_handoff}", encoding="utf-8"
                    )

            workflow = self._select_workflow(sub_task_def.get("type", "new_feature"))
            sub_start_time = time.time()
            try:
                await workflow(sub_task)
                sub_task_def["status"] = "completed"
                sub_cost = self.state.get_task_cost(sub_task)
                sub_duration = time.time() - sub_start_time
                self._update_progress(parent_task, "agent_complete",
                                      role=f"sub-task:{sub_name}", model="multi",
                                      cost_usd=sub_cost["total_usd"],
                                      duration=sub_duration)
                await self._push_step_card(parent_task, "agent_complete",
                                           role=f"sub-task:{sub_name}",
                                           cost_usd=sub_cost["total_usd"],
                                           duration=sub_duration)

                # 成功后运行 handoff_extractor 提取交接信息
                try:
                    await self._run_agent(
                        role="handoff_extractor",
                        task_dir=sub_task.dir,
                        input_docs={"design": "02-design.md"},
                        memories=[],
                        output_file=sub_task.dir / "handoff.md",
                        project=sub_task.project,
                    )
                    sub_task_def["handoff_file"] = str(sub_task.dir / "handoff.md")
                except (AgentTimeoutError, AgentError) as he:
                    logger.warning(f"handoff_extractor 执行失败（不阻塞流程）: {he}")

                return True
            except (WorkflowError, AgentTimeoutError, AgentError) as e:
                logger.error(f"子任务 {sub_task_def.get('name', sub_task_def['id'])} 失败: {e}")
                # 先读取费用再回滚（rollback 不删 cost.json 但保险起见先读取）
                sub_err_cost = self.state.get_task_cost(sub_task)
                sub_err_duration = time.time() - sub_start_time
                self.state.rollback_sub_task(sub_task)
                sub_task_def["status"] = "failed"
                sub_task_def["error"] = str(e)[:200]
                # 同步父任务为 partially_failed
                parent_task.status = "partially_failed"
                self.state._save_state(parent_task)
                self._update_progress(parent_task, "agent_error",
                                      role=f"sub-task:{sub_name}", model="multi",
                                      cost_usd=sub_err_cost["total_usd"],
                                      duration=sub_err_duration,
                                      error_msg=str(e)[:200])
                # 不推送企微卡片（由 _run_multi_task_workflow 统一推送，避免多条消息）
                self.state._save_state(parent_task)
                self.ui.print_warning(
                    f"子任务 [{sub_task_def.get('name')}] 失败并已回滚: {e}"
                )
                return False
        finally:
            # 确保清除 context（即使异常也清除）
            self._current_parent_task_id = None
            self._current_sub_id = None

    async def _revise_plan(self, task, max_rounds=2):
        for _ in range(max_rounds):
            feedback = await self.ui.get_user_feedback()
            if not feedback:
                return
            self.state.update_step(task, "project_plan")
            await self._run_agent(
                task=task, step_name="project_plan",
                role="project_manager",
                task_dir=task.dir,
                input_docs={
                    "prd": "01-prd.md",
                    "original_plan": "00-project-plan.md",
                    "user_feedback": feedback,
                },
                output_file=task.dir / "00-project-plan.md",
                project=task.project,
            )
            _ctx = self._build_confirm_context(
                task, role="project_manager",
                file=task.dir / "00-project-plan.md",
                button_set="doc_review",
            )
            self.state.set_pending_confirm(task, "", "confirm_with_feedback",
                                           "修改后的计划",
                                           button_set=_ctx.button_set)
            self._update_progress(task, "waiting_confirm",
                                  pending_confirm=task.pending_confirm)
            action = await self.ui.confirm_with_feedback(
                "修改后的计划", file=task.dir / "00-project-plan.md",
                context=_ctx,
            )
            self.state.clear_pending_confirm(task)
            self._update_progress(task, "confirm_resolved")
            if action != "feedback":
                return

    # ─── 恢复 + 历史 ─────────────────────────────────────────

    # 步骤名称 → 中文进度描述
    STEP_DISPLAY_MAP = {
        # 通用流程
        "requirement_analysis": "已完成需求分析",
        "pm_prd": "已完成产品文档",
        "architect": "已完成架构设计",
        "interaction_design": "已完成前端设计选型",
        "qa_test_cases": "已完成测试用例",
        "backend_dev": "已完成后端开发",
        "frontend_dev": "已完成前端开发",
        "integration": "已完成集成测试",
        "test_round_1": "已完成第1轮测试",
        "test_round_2": "已完成第2轮测试",
        "test_round_3": "已完成第3轮测试",
        "fix_round_1": "已完成第1轮修复",
        "fix_round_2": "已完成第2轮修复",
        "fix_round_3": "已完成第3轮修复",
        "knowledge": "已完成知识沉淀",
        "deploy": "已完成部署上线",
        # 快速修复
        "fix": "已完成问题修复",
        # 重构
        "refactor": "已完成代码重构",
        # 测试
        "qa_engineer": "已完成测试验证",
        # 嵌入式
        "embedded": "已完成嵌入式调试",
        # 多任务
        "project_planning": "已完成项目规划",
        "final_integration_test": "已完成最终集成测试",
    }

    def _format_step(self, step: str) -> str:
        """将步骤名称转换为中文进度描述"""
        return self.STEP_DISPLAY_MAP.get(step, step)

    def _recover_task_type(self, task) -> str:
        """从 RA/架构师输出文件中恢复 task_type，避免 resume 时默认 new_feature 走错流程"""
        import re
        # 优先从架构师输出恢复（架构师是精判）
        for filename in ["01-architecture.md", "00-requirement-analysis.md"]:
            filepath = task.dir / filename
            try:
                if filepath.exists():
                    content = filepath.read_text(encoding="utf-8")
                    # 匹配 JSON 中的 "task_type": "xxx"
                    m = re.search(r'"task_type"\s*:\s*"(\w+)"', content)
                    if m:
                        recovered = m.group(1)
                        logger.info(f"从 {filename} 恢复 task_type={recovered}")
                        return recovered
            except Exception:
                continue
        logger.warning("无法从输出文件恢复 task_type，默认 new_feature")
        return "new_feature"

    @staticmethod
    def _extract_task_name(task) -> str:
        """从 RA 输出文件的第一行标题提取简短任务名称"""
        import re
        ra_file = task.dir / "00-requirement-analysis.md"
        try:
            if ra_file.exists():
                first_line = ra_file.read_text(encoding="utf-8").split("\n", 1)[0].strip()
                # 格式1: "# 需求分析：XXX" 或 "# 需求分析报告：XXX"
                m = re.match(r"^#\s*需求分析(?:报告)?[：:]\s*(.+)$", first_line)
                if m:
                    return m.group(1).strip()[:60]
                # 格式2: "# XXX — 需求分析(文档/报告)"
                m = re.match(r'^#\s*(.+?)\s*[—\-]+\s*需求分析', first_line)
                if m:
                    return m.group(1).strip()[:60]
        except Exception:
            pass
        # fallback: 用 description 的第一句
        desc = getattr(task, 'description', '') or ''
        if desc:
            # 取第一个自然断句（句号/换行/链接开头之前的内容）
            import re as _re
            first_sentence = _re.split(r'[。\n]|(?:https?://)|(?:需求分析报告：)', desc, 1)[0].strip()
            return (first_sentence or desc)[:60]
        return ""

    def _build_confirm_context(
        self, task, role: str = "", result=None, file=None,
        button_set: str = "generic_confirm",
    ) -> ConfirmContext:
        """构建确认消息的上下文"""
        from stream_renderer import ROLE_DISPLAY

        # 任务累计费用
        task_cost = self.state.get_task_cost(task)

        # 已完成步骤中文名
        completed_cn = [self._format_step(s) for s in task.completed_steps]

        total_steps = self._estimate_total_steps(task)

        ctx = ConfirmContext(
            task_name=task.task_name or task.description[:60],
            task_id=task.id,
            role=role,
            role_display=ROLE_DISPLAY.get(role, role),
            step=task.current_step or "",
            step_display=self._format_step(task.current_step) if task.current_step else "",
            total_cost_usd=task_cost["total_usd"],
            total_tokens=task_cost["total_tokens"],
            completed_steps=completed_cn,
            total_steps=total_steps,
            file_path=str(file) if file else "",
            button_set=button_set,
        )

        if result:
            ctx.model = result.model
            ctx.cost_usd = result.cost_usd
            ctx.input_tokens = result.input_tokens
            ctx.output_tokens = result.output_tokens
            ctx.duration = result.duration

        return ctx

    def _estimate_total_steps(self, task: Task) -> int:
        """根据 task_type 估算总步数"""
        step_count_map = {
            "new_feature": 10,
            "bug_fix": 5,
            "refactor": 5,
            "debug_embedded": 3,
            "non_dev": 1,
        }
        return step_count_map.get(task.task_type, 6)

    def _format_duration(self, seconds: float) -> str:
        """格式化耗时"""
        total = int(seconds)
        m, s = divmod(total, 60)
        if m > 0:
            return f"{m}分{s}秒"
        return f"{s}秒"

    def _update_progress(self, task: Task, event: str, role: str = "",
                         cost_usd: float = 0, duration: float = 0,
                         output_doc: str = "", preview_url: str = "",
                         model: str = "", tokens: int = 0,
                         error_msg: str = "", pending_confirm: dict = None,
                         step_name: str = None, rollback: bool = True,
                         attempt: int = None):
        """原子更新 progress.json

        event: agent_start / agent_complete / agent_error / pause / complete / terminate / waiting_confirm / confirm_resolved
        """
        progress_file = task.dir / "progress.json"

        # 读取现有数据或初始化
        if progress_file.exists():
            try:
                progress = json.loads(progress_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                progress = {}
        else:
            progress = {}

        now = datetime.now().isoformat(timespec="seconds")

        # 初始化顶层字段
        if "task_id" not in progress:
            progress.update({
                "task_id": task.id,
                "task_name": task.task_name or task.description[:60],
                "status": "running",
                "current_role": "",
                "current_step": "",
                "completed_steps": [],
                "total_steps": self._estimate_total_steps(task),
                "cost_usd": 0,
                "started_at": task.created_at.isoformat(timespec="seconds") if task.created_at else now,
                "updated_at": now,
                "steps": [],
                "task_type": task.task_type or "",
                "scale": task.scale or "normal",
            })

        steps = progress.setdefault("steps", [])

        # 确保 task_type/description 始终同步（旧 progress 可能缺失）
        if task.task_type and not progress.get("task_type"):
            progress["task_type"] = task.task_type
        if task.description and not progress.get("description"):
            progress["description"] = task.description[:200]

        if event == "agent_start":
            # 新 agent 启动意味着确认已通过，清除残留的 pending_confirm
            progress.pop("pending_confirm", None)
            # 子任务用 role 中的描述名称，而非 task.current_step（后者仍是父步骤名如 "architect"）
            if role.startswith("sub-task:"):
                entry_name = role[len("sub-task:"):]
            else:
                entry_name = step_name or task.current_step or role
            # 同一步骤重跑时，旧失败/暂停/运行行移入历史，避免 UI 同时显示旧失败和新运行。
            attempt_history = progress.setdefault("attempt_history", [])
            retained_steps = []
            prior_attempts = sum(
                1
                for step in attempt_history
                if step.get("name") == entry_name or step.get("role") == role
            )
            for step in steps:
                same_step = step.get("name") == entry_name or step.get("role") == role
                if same_step and step.get("status") in {"running", "paused", "error"}:
                    archived = dict(step)
                    archived["superseded_at"] = now
                    archived["superseded_by_event"] = "agent_start"
                    if archived.get("status") == "running":
                        archived["status"] = "error"
                        archived.setdefault("completed_at", now)
                        archived.setdefault("error", "步骤重跑，旧运行记录已归档")
                    attempt_history.append(archived)
                    prior_attempts += 1
                else:
                    retained_steps.append(step)
            if len(attempt_history) > 100:
                del attempt_history[:-100]
            steps[:] = retained_steps
            step_attempt = attempt if attempt is not None else prior_attempts + 1
            steps.append({
                "name": entry_name,
                "role": role,
                "status": "running",
                "started_at": now,
                "completed_at": None,
                "cost_usd": 0,
                "duration": 0,
                "output_doc": "",
                "preview_url": "",
                "model": model,
                "attempt": step_attempt,
                "attempt_key": f"{entry_name}#attempt-{step_attempt}",
                "event_stream_policy": "events.jsonl may contain multiple attempts; use attempt metadata",
            })
            progress["current_role"] = role
            progress["current_step"] = entry_name

        elif event == "agent_complete":
            # 找到最后一个匹配 role 的 running step
            for step in reversed(steps):
                if step["role"] == role and step["status"] == "running":
                    step["status"] = "completed"
                    step["completed_at"] = now
                    step["cost_usd"] = round(cost_usd, 4)
                    step["duration"] = round(duration, 1)
                    step["output_doc"] = output_doc
                    step["preview_url"] = preview_url
                    if tokens:
                        step["tokens"] = tokens
                    break
            progress["current_role"] = ""
            if step_name:
                progress["current_step"] = step_name

        elif event == "agent_error":
            for step in reversed(steps):
                if step["role"] == role and step["status"] == "running":
                    step["status"] = "error"
                    step["completed_at"] = now
                    if error_msg:
                        step["error"] = error_msg
                    if cost_usd:
                        step["cost_usd"] = round(cost_usd, 4)
                    if duration:
                        step["duration"] = round(duration, 1)
                    break
            progress["current_role"] = ""

        elif event == "pause":
            progress["status"] = "paused"
            # 将当前 running 的 step 标记为 paused
            for step in reversed(steps):
                if step.get("status") == "running":
                    step["status"] = "paused"
                    break
            progress["current_role"] = ""

        elif event == "resume":
            progress["status"] = "running"
            # 将 paused 的 step 恢复为 running
            for step in reversed(steps):
                if step.get("status") == "paused":
                    step["status"] = "running"
                    break
            progress["current_role"] = role

        elif event == "complete":
            progress["status"] = "completed"
            progress["current_role"] = ""
            # 清理所有残留的 "running" 步骤（防止僵尸条目）
            for step in steps:
                if step.get("status") == "running":
                    step["status"] = "error"
                    step["completed_at"] = now
                    step.setdefault("error", "任务完成时仍在运行（已强制标记）")

        elif event == "terminate":
            progress["status"] = "rolled_back" if rollback else "terminated"
            progress["current_role"] = ""
            # 清理所有残留的 "running" 步骤
            for step in steps:
                if step.get("status") == "running":
                    step["status"] = "error"
                    step["completed_at"] = now
                    step.setdefault("error", "任务终止时仍在运行（已强制标记）")
            # 记录回滚恢复信息
            if rollback and task.rollback_info:
                progress["rollback_info"] = task.rollback_info

        elif event == "waiting_confirm":
            progress["status"] = "waiting_confirm"
            pending = dict(pending_confirm or {})
            if pending and not pending.get("request_id"):
                pending["request_id"] = f"vizo-{task.id}-pending-confirm"
                task.pending_confirm = pending
                try:
                    self.state._save_state(task)
                except Exception:
                    pass
            progress["pending_confirm"] = pending
            progress["current_role"] = ""

        elif event == "confirm_resolved":
            progress["status"] = "running"
            progress.pop("pending_confirm", None)

        # 始终同步 completed_steps（source of truth 是 task.completed_steps）
        progress["completed_steps"] = list(task.completed_steps)
        completed_set = set(task.completed_steps or [])
        for step in steps:
            step_name_for_completion = step.get("name") or ""
            if step_name_for_completion in completed_set and step.get("status") == "running":
                step["status"] = "completed"
                step["completed_at"] = step.get("completed_at") or now

        # 更新汇总（从 steps 数组汇总，包含所有费用，不受 billable 标志过滤）
        progress["cost_usd"] = round(sum(s.get("cost_usd", 0) for s in steps), 2)
        last_event_at = progress.get("last_event_at")
        if last_event_at:
            try:
                last_dt = datetime.fromisoformat(last_event_at)
                progress["last_event_age_seconds"] = max(0, int((datetime.now() - last_dt).total_seconds()))
            except (TypeError, ValueError):
                pass
        progress["updated_at"] = now

        # 原子写入
        tmp_file = progress_file.with_suffix(".tmp")
        tmp_file.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_file.rename(progress_file)

        # P1: Redis PUBLISH 进度事件（fire-and-forget，不阻塞主流程）
        progress_event = {
            "event": event,
            "task_id": task.id,
            "task_name": progress.get("task_name", ""),
            "description": progress.get("description", ""),
            "role": role,
            "status": progress["status"],
            "cost_usd": progress.get("cost_usd", 0),
            "steps": progress.get("steps", []),
            "completed_steps": progress.get("completed_steps", []),
            "current_step": progress.get("current_step", ""),
            "total_steps": progress.get("total_steps", 0),
            "task_type": progress.get("task_type", ""),
            "scale": progress.get("scale", "normal"),
            "started_at": progress.get("started_at", ""),
            "updated_at": progress["updated_at"],
        }
        if progress.get("pending_confirm"):
            progress_event["pending_confirm"] = progress["pending_confirm"]
        try:
            asyncio.get_running_loop().create_task(self._publish_progress(task.id, progress_event))
        except RuntimeError:
            pass  # No event loop (e.g., during tests)

    async def _publish_progress(self, task_id: str, progress_event: dict):
        """Publish progress event to Redis PubSub for Web Console live panel.

        Uses lazy-init Redis connection. PUBLISH failure is non-fatal (warning only).
        """
        try:
            if self._progress_redis is None:
                import redis.asyncio as aioredis
                redis_config = self.config.get("redis", {})
                self._progress_redis = aioredis.Redis(
                    host=redis_config.get("host", "127.0.0.1"),
                    port=redis_config.get("port", 6380),
                    decode_responses=True,
                )
            channel = f"opus:progress:{task_id}"
            await self._progress_redis.publish(channel, json.dumps(progress_event, ensure_ascii=False))
        except Exception as e:
            logger.warning("Redis PUBLISH progress failed: %s", e)
            self._progress_redis = None  # 重置连接，下次操作自动重建

    # 模块级敏感信息过滤模式（step_action 专用）
    _SENSITIVE_PATTERN = re.compile(
        r'(?:password|passwd|secret|token|api_key|apikey|access_key|private_key)'
        r'\s*[:=]\s*\S+',
        re.IGNORECASE
    )

    def _try_publish_step_action(self, task_id: str, step_id: str, event: dict):
        """从 NDJSON 流事件中提取操作日志并发布到 Redis PubSub。

        同步方法，通过 asyncio.ensure_future 触发异步 PUBLISH。
        推送失败静默忽略，不影响 Agent 执行。
        同时将结构化 action 追加写入 {task_dir}/logs/{step_id}.actions.jsonl
        供 Web Console 历史回放使用。
        """
        try:
            actions = self._extract_step_action(step_id, event)
            if not actions:
                return

            # 节流：同一事件批次只检查一次，think/output 不占节流名额
            now = time.time()
            has_tool = any(a.get("type") not in ("think", "output", "init") for a in actions)
            if has_tool:
                last = self._step_action_throttle.get(step_id, 0)
                if now - last < 0.1:
                    return
                self._step_action_throttle[step_id] = now

            log_dir = resolve_task_dir(
                task_id,
                project_root=self.state.project_path,
            ) / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            jsonl_path = log_dir / f"{step_id}.actions.jsonl"

            for action in actions:
                self._persist_step_action_progress(task_id, step_id, action)
                step_action_event = {
                    "event": "step_action",
                    "task_id": task_id,
                    "step_id": step_id,
                    "action": action,
                }

                asyncio.ensure_future(self._publish_progress(task_id, step_action_event))

                # 双发到父任务频道
                if self._current_parent_task_id and self._current_sub_id is not None:
                    parent_event = {
                        "event": "step_action",
                        "task_id": self._current_parent_task_id,
                        "step_id": f"sub-{self._current_sub_id}:{step_id}",
                        "sub_id": self._current_sub_id,
                        "action": action,
                    }
                    asyncio.ensure_future(
                        self._publish_progress(self._current_parent_task_id, parent_event)
                    )

                # 持久化到 JSONL 文件
                try:
                    with open(jsonl_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(action, ensure_ascii=False) + "\n")
                except Exception:
                    pass
        except Exception:
            pass  # 非关键路径，静默忽略

    def _persist_step_action_progress(self, task_id: str, step_id: str, action: dict):
        """把运行中 action 写回 progress.json，刷新后也能看到活跃状态。"""
        try:
            now_ts = time.time()
            throttle_key = f"progress:{task_id}:{step_id}"
            last = self._step_action_throttle.get(throttle_key, 0)
            action_type = action.get("type", "")
            if action_type in {"think", "output"} and now_ts - last < 5:
                return
            if action_type not in {"think", "output"} and now_ts - last < 1:
                return
            self._step_action_throttle[throttle_key] = now_ts

            task_dir = resolve_task_dir(task_id, project_root=self.state.project_path)
            progress_file = task_dir / "progress.json"
            if not progress_file.exists():
                return
            progress = json.loads(progress_file.read_text(encoding="utf-8"))
            steps = progress.get("steps", [])
            now = datetime.now().isoformat(timespec="seconds")

            target_step = None
            for step in reversed(steps):
                if step.get("status") == "running" and (
                    step.get("name") == step_id or progress.get("current_step") == step_id
                ):
                    target_step = step
                    break
            if target_step is None:
                for step in reversed(steps):
                    if step.get("status") == "running":
                        target_step = step
                        break
            if target_step is None:
                return

            phase = "running"
            if action_type == "think":
                phase = "summarizing"
            elif action_type == "output":
                phase = "reporting"

            current_action = {
                "type": action_type,
                "target": action.get("target", ""),
                "snippet": action.get("snippet", ""),
                "timestamp": action.get("timestamp", ""),
            }
            target_step["current_action"] = current_action
            target_step["current_phase"] = phase
            target_step["last_event_at"] = now
            target_step["last_event_age_seconds"] = 0
            progress["current_action"] = current_action
            progress["current_phase"] = phase
            progress["last_event_at"] = now
            progress["last_event_age_seconds"] = 0
            progress["updated_at"] = now

            tmp_file = progress_file.with_suffix(".tmp")
            tmp_file.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp_file.rename(progress_file)
        except Exception:
            pass  # durable progress is best-effort for live action events

    def _extract_step_action(self, step_id: str, event: dict) -> list:
        """从 NDJSON 事件中提取结构化操作信息列表。

        返回所有内容块的 action（工具调用、思考、文本输出），不过滤。
        Returns:
            [{"type": "read", "target": "src/foo.py", "snippet": None, "timestamp": "10:23:45"}, ...]
        """
        from stream_renderer import ToolNameMapper

        event_type = event.get("type", "")
        ts = time.strftime("%H:%M:%S")
        actions = []

        if event_type == "assistant":
            msg = event.get("message", {})
            content = msg.get("content", []) if isinstance(msg, dict) else []
            mapper = ToolNameMapper()

            for block in content:
                block_type = block.get("type", "")

                if block_type == "tool_use":
                    tool_name = block.get("name", "")
                    input_data = block.get("input", {})

                    action_type = ToolNameMapper.classify(tool_name)
                    label, target, supplement = mapper.map(tool_name, input_data)

                    if supplement:
                        target = f"{target} ({supplement})" if target else supplement

                    snippet = None
                    if action_type == "write":
                        new_str = (input_data.get("new_string", "")
                                   or input_data.get("content", "")
                                   or input_data.get("body", ""))
                        if new_str:
                            snippet = new_str
                    elif action_type == "exec":
                        cmd = input_data.get("command", "")
                        if cmd:
                            snippet = cmd
                    elif action_type == "search":
                        pattern = input_data.get("pattern", "")
                        query = input_data.get("query", "")
                        if pattern:
                            snippet = f"pattern: {pattern}"
                        elif query:
                            snippet = f"query: {query}"

                    if snippet:
                        snippet = self._SENSITIVE_PATTERN.sub("***", snippet)
                    if target:
                        target = self._SENSITIVE_PATTERN.sub("***", target)

                    actions.append({
                        "type": action_type,
                        "target": target or tool_name,
                        "snippet": snippet,
                        "timestamp": ts,
                    })

                elif block_type == "thinking":
                    thinking_text = block.get("thinking", "")
                    if thinking_text and len(thinking_text) > 20:
                        actions.append({
                            "type": "think",
                            "target": "",
                            "snippet": thinking_text,
                            "timestamp": ts,
                        })

                elif block_type == "text":
                    text = block.get("text", "")
                    if text and len(text.strip()) > 10:
                        actions.append({
                            "type": "output",
                            "target": "",
                            "snippet": text.strip(),
                            "timestamp": ts,
                        })

        elif event_type == "system" and event.get("subtype") == "init":
            model = event.get("model", "")
            actions.append({
                "type": "init",
                "target": f"model={model}",
                "snippet": None,
                "timestamp": ts,
            })

        return actions

    async def _push_step_card(self, task: Task, event: str, role: str = "",
                              cost_usd: float = 0, duration: float = 0,
                              output_doc: str = "", error_msg: str = "",
                              rollback: bool = True):
        """根据事件类型组装企微卡片内容并推送"""
        from stream_renderer import ROLE_DISPLAY

        if not self.ui.config.get("wecom", {}).get("enabled"):
            return

        # 子任务角色格式 "sub-task:密码管理（F4）" → 显示为 "子任务：密码管理（F4）"
        if role.startswith("sub-task:"):
            role_cn = f"子任务：{role[len('sub-task:'):]}"
        else:
            role_cn = ROLE_DISPLAY.get(role, role)
        task_cost = self.state.get_task_cost(task)
        total_cost = f"${task_cost['total_usd']:.2f}"
        n_completed = len(task.completed_steps)
        total_steps = self._estimate_total_steps(task)
        task_name = (task.task_name or task.description or "")[:30]

        if event == "agent_start":
            title = f"🤖 {role_cn}已启动"
            desc = f"📋 {task_name}\n第 {n_completed + 1}/{total_steps} 步 · 已消耗 {total_cost}"

        elif event == "agent_complete":
            title = f"✅ {role_cn}完成"
            dur_str = self._format_duration(duration)
            parts = [dur_str, f"${cost_usd:.2f}"]
            if output_doc:
                parts.append(output_doc)
            desc = f"📋 {task_name}\n" + " · ".join(parts)

        elif event == "agent_error":
            title = f"❌ {role_cn}失败"
            desc = f"📋 {task_name}\n{error_msg[:60]} · 已自动暂停"

        elif event == "pause":
            title = "⏸️ 任务已暂停"
            step_cn = self._format_step(task.current_step) if task.current_step else "未知"
            desc = f"📋 {task_name}\n停在：{step_cn} · {n_completed}/{total_steps} 步 · {total_cost}"

        elif event == "resume":
            title = "🔄 任务已恢复"
            step_cn = self._format_step(task.current_step) if task.current_step else "继续执行"
            desc = f"📋 {task_name}\n{step_cn} · {n_completed}/{total_steps} 步 · {total_cost}"

        elif event == "complete":
            title = "🎉 任务完成"
            dur_str = task.duration_str
            desc = f"📋 {task_name}\n{n_completed}/{total_steps} 步 · 总计 {total_cost} · {dur_str}"

        elif event == "terminate":
            title = "🛑 任务已终止"
            if rollback:
                desc = (f"📋 {task_name}\n已消耗 {total_cost} · 代码已回滚\n"
                        f"恢复: git cherry-pick opus-snapshot/{task.id}")
            else:
                desc = f"📋 {task_name}\n已消耗 {total_cost} · 代码已保留"

        else:
            return

        try:
            await self.ui._wecom_push_card(
                title=title, description=desc,
                task_id=task.id,
                btntxt="查看进度" if event == "agent_start" else "查看详情",
            )
        except Exception as e:
            logger.warning(f"步骤卡片推送失败: {e}")

    def _format_current_progress(self, task) -> str:
        """根据已完成步骤和当前步骤，生成中文进度描述"""
        if not task.completed_steps:
            return "尚未开始"
        # 用最后完成的步骤来描述当前进度
        last_step = task.completed_steps[-1]
        return self._format_step(last_step)

    async def resume_last_task(self, task_id: str = None):
        """恢复未完成的任务（展示列表供用户选择，不自动执行）

        Args:
            task_id: 指定任务 ID 时直接恢复（可选）
        """
        await self._startup_cleanup()
        tasks = self.state.find_all_incomplete_tasks()
        if not tasks:
            self.ui.print_info("没有未完成的任务")
            return

        # 如果指定了任务 ID，直接查找并恢复
        if task_id:
            task = next((t for t in tasks if t.id == task_id), None)
            if not task:
                self.ui.print_error(f"未找到任务: {task_id}")
                return
            # 直接恢复该任务
            await self._resume_task(task)
            return

        # 如果没有指定 task_id，仅显示列表，不自动执行
        display_tasks = tasks[:10]
        total_count = len(tasks)

        print()
        if total_count > 10:
            self.ui.print_info(f"发现 {total_count} 个未完成任务（显示最近 10 个）：")
        else:
            self.ui.print_info(f"发现 {total_count} 个未完成任务：")
        print()

        for i, t in enumerate(display_tasks, 1):
            status_label = {"paused": "⏸ 暂停", "failed": "❌ 失败"}.get(t.status, "⚡ 中断")
            progress = self._format_current_progress(t)
            print(f"  [{i}] {t.description}")
            print(f"      {status_label} | 当前进度: {progress}")
            print(f"      ID: {t.id}")
        print()

        self.ui.print_info("要恢复任务，请使用:")
        print("  opus --resume --task-id <任务ID>")
        print()
        print("示例:")
        for t in display_tasks[:2]:
            print(f"  opus --resume --task-id {t.id}")

    async def _resume_task(self, task: Task):
        """内部方法：恢复指定的任务"""
        # 🔒 任务级文件锁 + PID 验证：防止多个进程同时 resume 同一任务
        lock_file_path = task.dir / ".lock"
        self._task_lock_fd = open(lock_file_path, "w")
        try:
            fcntl.flock(self._task_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            # 锁被占用，检查持有者是否仍存活
            old_pid = self._read_lock_pid(lock_file_path)
            if old_pid and not self._is_opus_process_alive(old_pid):
                # 旧进程已死，强制接管
                logger.warning(f"旧进程 PID {old_pid} 已死亡，强制接管锁")
                self._task_lock_fd.close()
                # 删除旧锁文件并重新创建
                lock_file_path.unlink(missing_ok=True)
                self._task_lock_fd = open(lock_file_path, "w")
                try:
                    fcntl.flock(self._task_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (BlockingIOError, OSError):
                    self._task_lock_fd.close()
                    self._task_lock_fd = None
                    raise WorkflowError(f"任务 {task.id} 已在其他进程中运行，无法重复恢复")
            else:
                self._task_lock_fd.close()
                self._task_lock_fd = None
                pid_info = f"（PID {old_pid}）" if old_pid else ""
                raise WorkflowError(f"任务 {task.id} 已在其他进程中运行{pid_info}，无法重复恢复")

        # 写入当前 PID 到锁文件
        self._task_lock_fd.seek(0)
        self._task_lock_fd.write(str(os.getpid()))
        self._task_lock_fd.flush()

        # 清理旧进程留下的孤立确认请求
        self._cleanup_stale_confirms(task)

        self._current_task_id = task.id
        self.ui._current_task_id = task.id
        pause_reason = ""
        pause_file = task.dir / "pause_reason.txt"
        if pause_file.exists():
            pause_reason = pause_file.read_text(encoding="utf-8").strip()

        work_state = self.state.load_work_state()

        status_label = {"paused": "暂停", "failed": "失败"}.get(task.status, "中断")
        progress = self._format_current_progress(task)
        completed_steps_cn = [self._format_step(s) for s in task.completed_steps]
        if completed_steps_cn:
            steps_display = "\n".join(f"  ✅ {s}" for s in completed_steps_cn)
        else:
            steps_display = "  （无）"
        info = (
            f"准备恢复{status_label}任务：{task.description}\n"
            f"已完成步骤：\n{steps_display}\n"
            f"当前进度：{progress}"
        )
        if pause_reason:
            info += f"\n暂停原因：{pause_reason}"
        if work_state and work_state.get("recent_progress"):
            info += "\n最近进度：\n  " + "\n  ".join(work_state["recent_progress"][-5:])
        self.ui.print_info(info)

        # 检查 Git 快照
        snapshot_dir = task.dir / "snapshots"
        if snapshot_dir.exists():
            diff_files = sorted(snapshot_dir.glob("*.diff"))
            if diff_files:
                # 找到最近的快照
                latest_diff = diff_files[-1]
                # 从文件名提取 step_name：{step_name}-{timestamp}.diff
                stem = latest_diff.stem
                parts = stem.rsplit("-", 1)
                rollback_step = parts[0] if len(parts) == 2 else stem

                base_hash_file = snapshot_dir / f"{rollback_step}-base.hash"
                if base_hash_file.exists():
                    base_hash = base_hash_file.read_text(encoding="utf-8").strip()
                    self.ui.print_info(
                        f"\n发现上次超时暂停的快照（步骤：{rollback_step}）"
                    )
                    _ctx = self._build_confirm_context(task)
                    self.state.set_pending_confirm(task, "", "confirm_with_feedback",
                                                   f"步骤 [{rollback_step}] 有 Git 快照",
                                                   button_set=_ctx.button_set)
                    self._update_progress(task, "waiting_confirm",
                                          pending_confirm=task.pending_confirm)
                    action = await self.ui.confirm_with_feedback(
                        f"步骤 [{rollback_step}] 有 Git 快照，请选择：\n"
                        f"[确认] 回退到步骤开始前的状态（{base_hash[:8]}），重新执行\n"
                        f"[取消] 在当前基础上继续执行下一步\n"
                        f"[修改意见] 提供其他指示",
                        context=_ctx,
                    )
                    self.state.clear_pending_confirm(task)
                    self._update_progress(task, "confirm_resolved")

                    if action == "confirm":
                        # 回退到 base_hash
                        work_dir = self.agent._get_project_path(
                            self.config.get("default_project", "")
                        )
                        await self.state._run_git("checkout", base_hash, cwd=work_dir)
                        self.ui.print_info(f"已回退到 {base_hash[:8]}")
                        # 从 completed_steps 中移除该步骤，准备重新执行
                        if rollback_step in task.completed_steps:
                            task.completed_steps.remove(rollback_step)
                        self.state._save_state(task)

                # 清理 snapshots
                import shutil
                shutil.rmtree(snapshot_dir, ignore_errors=True)

        confirmed = await self.ui.confirm("是否继续？")
        if confirmed:
            task.status = "running"
            # resume 时补全可能丢失的 task_type（从 RA/架构师输出文件恢复）
            if not task.task_type:
                task.task_type = self._recover_task_type(task)
            self.state._save_state(task)
            # 同步 progress.json 状态为 running（防止与 state.json 不一致）
            self._update_progress(task, "resume")

            # 启动终端进度显示
            if self.ui.mode == "terminal" and not self._progress_display:
                self._progress_display = self.ui.create_progress_display(
                    task_id=task.id, task_desc=task.description
                )
                self._progress_display.start()

            try:
                # 读取用户补充意见（恢复时注入给第一个实际执行的 agent）
                injection_file = task.dir / "user_injection.md"
                if injection_file.exists():
                    self._pending_user_injection = injection_file.read_text(encoding="utf-8")
                    injection_file.unlink()
                else:
                    self._pending_user_injection = None

                # 直接复用主工作流——每一步内部都有 completed_steps 检查，
                # 已完成的步骤自动跳过，未完成的从头执行
                await self._execute_main_workflow(task)

            except WorkflowPaused as e:
                self.state.save_session_summary(task, f"暂停（用户选择）")
                self.state.pause_task(task, reason=str(e))
                self.ui.print_info(f"任务已暂停：{e}")
                self.ui.print_info("可通过 opus --resume 继续")
            except WorkflowError as e:
                await self._generate_failure_report(task, e)
                actually_rolled_back = self.state.rollback(task)
                if actually_rolled_back:
                    self.state.save_session_summary(task, f"回滚（{e}）")
                    self.ui.print_info(
                        f"代码已安全回滚（历史保留）\n"
                        f"恢复分支: opus-snapshot/{task.id}\n"
                        f"恢复命令: git cherry-pick opus-snapshot/{task.id}"
                    )
                else:
                    self.state.save_session_summary(task, f"取消（{e}）")
                    self.ui.print_info(f"任务已取消（无代码变更，无需回滚）")
            except BudgetExceeded as e:
                self.state.save_session_summary(task, f"暂停（预算）")
                self.state.pause_task(task, reason=str(e))
                self.ui.print_warning(
                    f"任务已暂停（预算）: {e}\n"
                    f"已完成步骤: {', '.join(task.completed_steps)}\n"
                    f"使用 opus --resume 可继续执行"
                )
            except (AgentTimeoutError, AgentError) as e:
                logger.error(f"Agent 执行失败: {e}")
                await self._generate_failure_report(task, e)
                self.state.save_session_summary(task, f"失败（{type(e).__name__}）")
                self.state.fail_task(task, reason=str(e))
            finally:
                if self._progress_display:
                    self._progress_display.stop()
                    self._progress_display = None
                # 🔒 释放任务级文件锁
                self._release_task_lock()

    def show_history(self):
        """查看最近10个任务"""
        from state_manager import Task

        # 收集任务
        tasks = []
        for task_dir in iter_task_dirs(self.state.project_path):
            state_file = task_dir / "state.json"
            if state_file.exists():
                try:
                    state_dict = json.loads(state_file.read_text(encoding="utf-8"))
                    task_obj = Task.from_dict(state_dict)
                    tasks.append(task_obj)
                except Exception:
                    pass

        if not tasks:
            self.ui.print_info("暂无任务历史")
            return

        # 只显示最近10个
        display_tasks = tasks[:10]
        total_count = len(tasks)

        # 获取当前项目名
        current_project = self.config.get('default_project', 'unknown')

        # 计算列宽
        col_num_width = 4
        col_id_width = 20
        col_desc_width = 36
        col_progress_width = 25
        col_dur_width = 12

        print()
        print(f"📋 {current_project} 项目 - 任务历史记录")
        print()

        # 打印表头
        print("┌" + "─" * col_num_width + "┬" + "─" * col_id_width + "┬" + "─" * col_desc_width + "┬" + "─" * col_progress_width + "┬" + "─" * col_dur_width + "┐")
        print(f"│ {'#':<{col_num_width-2}} │ {'任务 ID':<{col_id_width-2}} │ {'描述':<{col_desc_width-2}} │ {'进度':<{col_progress_width-2}} │ {'耗时':<{col_dur_width-2}} │")
        print("├" + "─" * col_num_width + "┼" + "─" * col_id_width + "┼" + "─" * col_desc_width + "┼" + "─" * col_progress_width + "┼" + "─" * col_dur_width + "┤")

        # 打印数据行
        for i, t in enumerate(display_tasks, 1):
            progress = self._format_current_progress(t)

            # 生成耗时文本
            duration_text = t.duration_str
            if t.status != "completed":
                duration_text += "⏳"

            # 截断过长的文本
            task_id = t.id[:col_id_width-3] if len(t.id) > col_id_width-3 else t.id
            desc = t.description[:col_desc_width-3] if len(t.description) > col_desc_width-3 else t.description
            prog = progress[:col_progress_width-3] if len(progress) > col_progress_width-3 else progress
            dur = duration_text[:col_dur_width-3] if len(duration_text) > col_dur_width-3 else duration_text

            print(f"│ {i:<{col_num_width-2}} │ {task_id:<{col_id_width-2}} │ {desc:<{col_desc_width-2}} │ {prog:<{col_progress_width-2}} │ {dur:<{col_dur_width-2}} │")

            # 如果不是最后一行，打印分隔线
            if i < len(display_tasks):
                print("├" + "─" * col_num_width + "┼" + "─" * col_id_width + "┼" + "─" * col_desc_width + "┼" + "─" * col_progress_width + "┼" + "─" * col_dur_width + "┤")

        # 打印底部边框
        print("└" + "─" * col_num_width + "┴" + "─" * col_id_width + "┴" + "─" * col_desc_width + "┴" + "─" * col_progress_width + "┴" + "─" * col_dur_width + "┘")

        print()
        if total_count > 10:
            print(f"  ... 还有 {total_count - 10} 个任务")

    def show_task_list(self, limit: int = 10):
        """列出最近的任务（精简表格）"""
        from state_manager import Task

        # 收集任务（复用 show_history 的扫描逻辑）
        tasks = []
        for task_dir in iter_task_dirs(self.state.project_path):
            state_file = task_dir / "state.json"
            if state_file.exists():
                try:
                    state_dict = json.loads(state_file.read_text(encoding="utf-8"))
                    task_obj = Task.from_dict(state_dict)
                    tasks.append(task_obj)
                except Exception:
                    pass

        if not tasks:
            print("暂无任务记录")
            return

        display_tasks = tasks[:limit]
        current_project = self.config.get('default_project', 'unknown')

        # 表格列宽
        col_num = 4
        col_id = 22
        col_desc = 40
        col_status = 12
        col_dur = 10
        col_cost = 10

        print()
        print(f"📋 {current_project} 项目 - 任务列表")
        print()

        # 表头
        header = f"│ {'#':<{col_num-2}} │ {'任务 ID':<{col_id-2}} │ {'描述':<{col_desc-2}} │ {'状态':<{col_status-2}} │ {'耗时':<{col_dur-2}} │ {'费用':<{col_cost-2}} │"
        sep = "├" + "─"*col_num + "┼" + "─"*col_id + "┼" + "─"*col_desc + "┼" + "─"*col_status + "┼" + "─"*col_dur + "┼" + "─"*col_cost + "┤"
        print("┌" + "─"*col_num + "┬" + "─"*col_id + "┬" + "─"*col_desc + "┬" + "─"*col_status + "┬" + "─"*col_dur + "┬" + "─"*col_cost + "┐")
        print(header)
        print(sep)

        for i, t in enumerate(display_tasks, 1):
            cost = self.state.get_task_cost(t)
            tid = t.id[:col_id-3] if len(t.id) > col_id-3 else t.id
            desc = t.description[:col_desc-3] if len(t.description) > col_desc-3 else t.description
            status = t.status[:col_status-3] if len(t.status) > col_status-3 else t.status
            dur = t.duration_str[:col_dur-3] if len(t.duration_str) > col_dur-3 else t.duration_str
            cost_str = f"${cost['total_usd']:.2f}"

            print(f"│ {i:<{col_num-2}} │ {tid:<{col_id-2}} │ {desc:<{col_desc-2}} │ {status:<{col_status-2}} │ {dur:<{col_dur-2}} │ {cost_str:<{col_cost-2}} │")
            if i < len(display_tasks):
                print(sep)

        print("└" + "─"*col_num + "┴" + "─"*col_id + "┴" + "─"*col_desc + "┴" + "─"*col_status + "┴" + "─"*col_dur + "┴" + "─"*col_cost + "┘")

        if len(tasks) > limit:
            print(f"\n  ... 还有 {len(tasks) - limit} 个任务")
        print()

    def show_task_detail(self, task_id: str):
        """显示指定任务的详情和文档摘要"""
        from state_manager import Task

        task_dir = resolve_task_dir(task_id, project_root=self.state.project_path)
        state_file = task_dir / "state.json"

        if not state_file.exists():
            print(f"❌ 任务不存在: {task_id}")
            print("💡 使用 opus --task 查看任务列表")
            return

        try:
            state_dict = json.loads(state_file.read_text(encoding="utf-8"))
            task = Task.from_dict(state_dict)
        except Exception as e:
            print(f"❌ 读取任务状态失败: {e}")
            return

        cost = self.state.get_task_cost(task)

        print()
        print(f"📋 任务详情: {task.id}")
        print()
        print(f"  描述: {task.description}")
        print(f"  状态: {task.status}")
        print(f"  创建: {task.created_at or '未知'}")
        print(f"  耗时: {task.duration_str}")
        print(f"  费用: ${cost['total_usd']:.2f}")

        # 列出文档文件
        md_files = sorted(task.dir.glob("*.md"))
        if md_files:
            print()
            print("📄 任务文档:")
            print()
            for f in md_files:
                size = f.stat().st_size
                size_str = f"{size / 1024:.1f} KB" if size >= 1024 else f"{size} B"
                print(f"  {f.name} ({size_str})")
                try:
                    preview = f.read_text(encoding="utf-8")[:200].replace("\n", " ").strip()
                    if preview:
                        print(f"    {preview}...")
                except OSError:
                    pass
                print()
        else:
            print("\n  （无文档文件）")
        print()

    def show_cost_today(self):
        """查看今日消耗"""
        today = datetime.now().strftime("%Y-%m-%d")
        total_cost = 0.0
        total_tokens = 0
        cost_details = []

        task_dirs = list(iter_task_dirs(self.state.project_path))
        if not task_dirs:
            self.ui.print_info("暂无费用记录")
            return

        for task_dir in task_dirs:
            cost_file = task_dir / "cost.json"
            if not cost_file.exists():
                continue
            try:
                costs = json.loads(cost_file.read_text(encoding="utf-8"))
                for c in costs:
                    if today in c.get("timestamp", ""):
                        total_cost += c.get("cost_usd", 0)
                        total_tokens += c.get("input_tokens", 0) + c.get("output_tokens", 0)
                        cost_details.append(c)
            except (json.JSONDecodeError, ValueError):
                pass

        # 获取当前项目名
        current_project = self.config.get('default_project', 'unknown')

        print()
        print(f"💰 {current_project} 项目 - 今日费用统计")
        print()

        if not cost_details:
            print("  暂无费用记录")
            print()
            return

        # 显示汇总
        print(f"  总消耗: {total_tokens:,} tokens = ${total_cost:.2f}")
        print()

        # 按角色/模型统计
        role_costs = {}
        for c in cost_details:
            role = c.get("role", "unknown")
            if role not in role_costs:
                role_costs[role] = {"tokens": 0, "cost": 0.0}
            role_costs[role]["tokens"] += c.get("input_tokens", 0) + c.get("output_tokens", 0)
            role_costs[role]["cost"] += c.get("cost_usd", 0)

        print("  按角色统计:")
        for role, stats in sorted(role_costs.items()):
            print(f"    • {role}: {stats['tokens']:,} tokens = ${stats['cost']:.2f}")
        print()

    # ─── 预算守卫 ─────────────────────────────────────────────

    def _get_today_cost(self) -> float:
        """汇总今日所有任务的累计费用"""
        today = datetime.now().strftime("%Y-%m-%d")
        total = 0.0
        task_dirs = list(iter_task_dirs(self.state.project_path))
        if not task_dirs:
            return 0.0
        for task_dir in task_dirs:
            cost_file = task_dir / "cost.json"
            if not cost_file.exists():
                continue
            try:
                costs = json.loads(cost_file.read_text(encoding="utf-8"))
                for c in costs:
                    if today in c.get("timestamp", ""):
                        total += c.get("cost_usd", 0)
            except (json.JSONDecodeError, OSError):
                continue
        return total

    async def _check_budget(self, next_role: str):
        """预算检查，在每次 agent.run() 前调用

        ≥ warn_threshold_usd ($50): 通知用户，继续执行
        ≥ pause_threshold_usd ($100): 通知用户，暂停等待决策
        """
        budget = self.config.get("budget", {})
        warn_usd = budget.get("warn_threshold_usd", 50)
        pause_usd = budget.get("pause_threshold_usd", 100)

        today_cost = self._get_today_cost()

        if today_cost >= pause_usd:
            msg = (
                f"今日费用已达 ${today_cost:.2f}（上限 ${pause_usd}），"
                f"下一步 [{next_role}] 将消耗额外费用。\n"
                f"回复「继续」放行，回复「停止」保存进度暂停。"
            )
            confirmed = await self.ui.confirm(msg)
            if not confirmed:
                raise BudgetExceeded(f"用户选择暂停，今日累计 ${today_cost:.2f}")

        elif today_cost >= warn_usd and not self._warned_budget:
            self._warned_budget = True
            remaining = pause_usd - today_cost
            self.ui.print_warning(
                f"今日费用已达 ${today_cost:.2f}，剩余预算 ${remaining:.2f}"
            )

    async def _check_control_signal(self, task: Task) -> None:
        """检查并处理控制信号，必要时抛出异常中断流程"""
        from lib.control_signals import read_signal, write_audit_log
        signal = read_signal(task.id)
        # 子任务执行时，同时检查父任务信号
        if not signal and self._current_parent_task_id and self._current_parent_task_id != task.id:
            signal = read_signal(self._current_parent_task_id)
        if not signal:
            return

        action = signal.get("action")
        source = signal.get("source", "unknown")

        if action == "pause":
            logger.info(f"收到暂停信号 (source={source})")
            write_audit_log(task.dir, "pause", source=source,
                            task_id=task.id, detail="用户主动暂停")
            self.state.pause_task(task, reason="用户主动暂停")
            # 更新 progress.json + 企微卡片推送
            self._update_progress(task, "pause")
            await self._push_step_card(task, "pause")
            raise WorkflowPaused("用户主动暂停")

        elif action == "rollback":
            target_step = signal.get("target_step", "")
            feedback = signal.get("feedback", "")
            logger.info(f"收到回滚信号 → {target_step}")
            write_audit_log(task.dir, "rollback", source=source,
                            task_id=task.id, detail=f"回滚到步骤 {target_step}",
                            target_step=target_step)
            await self._handle_rollback_signal(task, target_step, feedback)
            raise WorkflowPaused(f"用户回滚到步骤 {target_step}")

        elif action == "terminate":
            rollback = signal.get("rollback", False)
            logger.info(f"收到终止信号 (source={source}, rollback={rollback})")
            write_audit_log(task.dir, "terminate", source=source,
                            task_id=task.id, detail=f"用户主动终止 (rollback={rollback})")
            await self._handle_terminate_signal(task, rollback=rollback)
            step_label = task.current_step or "未知步骤"
            source_label = {"web": "Web Console", "terminal": "终端", "wecom": "企微"}.get(source, source or "未知来源")
            raise WorkflowError(f"用户主动终止（来源: {source_label}，终止时步骤: {step_label}，回滚: {'是' if rollback else '否'}）")

    async def _handle_rollback_signal(self, task: Task, target_step: str, feedback: str):
        """处理回滚信号"""
        work_dir = self.agent._get_project_path(
            task.project or self.config.get("default_project", "")
        )

        # 如果有 worktree 未清理，先清理
        await self._cleanup_any_worktrees(task)

        # 执行回滚
        self.state.rollback_to_step(task, target_step, work_dir)

        # 保存用户反馈（供恢复执行时注入下一个 Agent）
        if feedback:
            injection_file = task.dir / "user_injection.md"
            injection_file.write_text(
                f"# 用户回滚修正意见\n\n{feedback}\n",
                encoding="utf-8",
            )

        logger.info(f"已回滚到步骤 {target_step} (commit: {task.step_checkpoints.get(target_step, '?')[:8]})")

    def _task_has_code_changes(self, task) -> bool:
        """检查任务是否有代码变更（基于 git diff）"""
        if not task.restore_point:
            return False
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", task.restore_point, "HEAD"],
                cwd=str(self.state.project_path),
                capture_output=True, text=True, timeout=10,
            )
            return bool(result.returncode == 0 and result.stdout.strip())
        except Exception:
            return False

    async def _handle_terminate_signal(self, task: Task, rollback: bool = False):
        """处理终止信号"""
        work_dir = self.agent._get_project_path(
            task.project or self.config.get("default_project", "")
        )

        # 清理 worktree（如果有）
        await self._cleanup_any_worktrees(task)

        # 执行安全终止
        await self.state.terminate_task(task, work_dir, rollback=rollback)

        # 更新 progress.json + 企微卡片推送
        self._update_progress(task, "terminate", rollback=rollback)
        await self._push_step_card(task, "terminate", rollback=rollback)

        if rollback:
            self.ui.print_info(
                f"任务已安全终止，代码已回滚（历史保留）\n"
                f"恢复分支: opus-snapshot/{task.id}\n"
                f"恢复命令: git cherry-pick opus-snapshot/{task.id}"
            )
        else:
            self.ui.print_info("任务已终止（代码已保留）")

    async def _cleanup_any_worktrees(self, task: Task):
        """检测并清理可能存活的 worktree"""
        work_dir = self.agent._get_project_path(
            task.project or self.config.get("default_project", "")
        )
        try:
            result = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                cwd=str(work_dir), capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                # 查找非主 worktree
                from state_manager import WorktreeInfo
                worktrees = {}
                lines = result.stdout.strip().split("\n")
                for line in lines:
                    if line.startswith("worktree "):
                        wt_path = line.split(" ", 1)[1]
                        # 只清理任务相关的 worktree（路径含 opus-wt-）
                        if "opus-wt-" in wt_path:
                            # 提取分支名
                            branch = Path(wt_path).name.replace("opus-wt-", "").split("-")[0]
                            worktrees[branch] = WorktreeInfo(
                                path=Path(wt_path),
                                branch=f"opus-{task.id}-{branch}",
                            )
                if worktrees:
                    logger.info(f"清理 {len(worktrees)} 个残留 worktree")
                    await self.state.cleanup_worktrees(worktrees)
        except Exception as e:
            logger.warning(f"清理 worktree 时出错: {e}")

    async def _run_agent(self, task=None, step_name=None, _retry_count=0, **kwargs):
        """受保护的 agent 调用入口，执行前检查预算 + 中断 + 进度跟踪。

        Args:
            task: 当前任务对象（可选，用于在成功后标记步骤完成）
            step_name: 当前步骤名称（可选，与 task 配合使用）
            _retry_count: 内部重试计数器（自动重试+用户确认重试的总次数）
            **kwargs: 传递给 AgentRunner.run() 的参数
        """
        role = kwargs.get("role", "unknown")
        await self._check_budget(role)

        # 优化5a: 检查中断信号
        interrupt = await self.ui.check_interrupt()
        if interrupt == "cancel":
            raise WorkflowError(f"用户中断任务（cancel）")
        elif interrupt == "modify":
            raise WorkflowError(f"用户请求修改方案（modify）")

        # 检查控制信号（暂停/回滚/终止）
        if task:
            await self._check_control_signal(task)

        # 优化4b: 进度显示
        model = (self.config.get("model_overrides", {}).get(role)
                 or self.agent.DEFAULT_MODELS.get(role, "sonnet"))
        fallback = self.agent.FALLBACK_MODELS.get(model, "") or ""
        output_file = str(kwargs.get("output_file", "")) if kwargs.get("output_file") else ""
        if self._progress_display:
            self._progress_display.agent_started(
                role, model, output_file=output_file, fallback=fallback, attempt=_retry_count
            )
            # 设置动作回调：子代理每个工具调用都更新进度表
            self.agent._on_action_callback = lambda action: (
                self._progress_display.update_action(role, action)
            )

        # 创建流式渲染器
        stream_renderer = None
        task_dir_path = Path(str(kwargs.get("task_dir", ""))) if kwargs.get("task_dir") else None
        if task_dir_path:
            from stream_renderer import StreamRenderer
            log_dir = task_dir_path / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            _log_step_id = step_name or (task.current_step if task else None) or role
            log_path = log_dir / f"{_log_step_id}.stream.log"
            jsonl_path = log_dir / f"{_log_step_id}.actions.jsonl"

            # 恢复任务重新执行同一步骤时，清除旧日志避免内容叠加
            if log_path.exists():
                log_path.unlink()
            if jsonl_path.exists():
                jsonl_path.unlink()

            # 终端模式：输出到 console + 日志；非终端：只写日志
            console = None
            if self._progress_display:
                console = self._progress_display.get_console()

            stream_renderer = StreamRenderer(
                role=role, model=model,
                console=console,
                log_path=log_path,
            )
            stream_renderer.start()
            _action_step_id = step_name or (task.current_step if task else None) or role
            def _on_stream_with_publish(event, _sid=_action_step_id):
                stream_renderer.feed(event)
                if task:
                    self._try_publish_step_action(task.id, _sid, event)

            self.agent._on_stream_event_callback = _on_stream_with_publish
        elif task:
            # 无 stream_renderer 时也发布（非终端模式下仍需推送到 Web）
            _action_step_id = step_name or (task.current_step if task else None) or role
            self.agent._on_stream_event_callback = lambda event, _sid=_action_step_id: (
                self._try_publish_step_action(task.id, _sid, event)
            )

        # 保存步骤开始前的 commit hash

        # 更新 progress.json + 企微卡片推送
        if task:
            self._update_progress(
                task, "agent_start", role=role, model=model,
                step_name=step_name, attempt=_retry_count + 1,
            )
            await self._push_step_card(task, "agent_start", role=role)

        # 保存步骤开始前的 commit hash（供超时回退使用）
        if task and step_name:
            work_dir = kwargs.get("cwd") or self.agent._get_project_path(
                kwargs.get("project") or self.config.get("default_project", "")
            )
            snapshot_dir = task.dir / "snapshots"
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            try:
                import subprocess
                result_proc = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=str(work_dir), capture_output=True, text=True, timeout=10
                )
                if result_proc.returncode == 0:
                    base_file = snapshot_dir / f"{step_name}-base.hash"
                    base_file.write_text(result_proc.stdout.strip(), encoding="utf-8")
                    # 同步记录到 Task 的 step_checkpoints
                    task.step_checkpoints[step_name] = result_proc.stdout.strip()
                    self.state._save_state(task)
            except Exception:
                pass  # 非关键路径，不阻塞执行

        # 读取用户注入（恢复时的补充意见，只注入给恢复后第一个实际执行的 agent）
        if task and getattr(self, '_pending_user_injection', None):
            if "input_docs" not in kwargs:
                kwargs["input_docs"] = {}
            kwargs["input_docs"]["user_injection"] = self._pending_user_injection
            self._pending_user_injection = None  # 消费后清除

        # 子任务范围清单自动注入（让开发者明确知道自己的边界）
        task_dir_for_scope = Path(str(kwargs.get("task_dir", ""))) if kwargs.get("task_dir") else None
        if task_dir_for_scope:
            scope_file = task_dir_for_scope / "00-sub-task-scope.md"
            if scope_file.exists():
                if "input_docs" not in kwargs:
                    kwargs["input_docs"] = {}
                kwargs["input_docs"]["sub_task_scope"] = "00-sub-task-scope.md"

        # 原始需求自动注入（让所有角色都能看到用户原始需求和参考文档）
        if task:
            original_req_file = task.dir / "00-user-request.md"
            if original_req_file.exists():
                if "input_docs" not in kwargs:
                    kwargs["input_docs"] = {}
                if "original_request" not in kwargs["input_docs"]:
                    kwargs["input_docs"]["original_request"] = "00-user-request.md"

        start_time = time.time()
        try:
            # 注入 task_id 以便子代理执行中轮询控制信号（秒级响应暂停/终止）
            if task:
                kwargs["task_id"] = task.id
                # 子任务执行时，同时传入父任务 ID 以便 agent_runner 轮询父任务信号
                if self._current_parent_task_id and self._current_parent_task_id != task.id:
                    kwargs["parent_task_id"] = self._current_parent_task_id

                # 暂停回调：进程被 SIGSTOP 冻结时推卡片、更新状态
                async def on_paused(signal):
                    # 1. 最关键：更新状态文件（前端依赖 progress.json 显示状态）
                    try:
                        self.state.pause_task(task, reason="用户主动暂停")
                    except Exception as e:
                        logger.error(f"on_paused: pause_task 失败: {e}")
                    try:
                        self._update_progress(task, "pause")
                    except Exception as e:
                        logger.error(f"on_paused: update_progress 失败: {e}")
                    # 2. 写冻结标记（Web 控制台据此判断应发 resume 而非 spawn）
                    try:
                        (task.dir / "frozen.flag").touch()
                    except Exception as e:
                        logger.error(f"on_paused: frozen.flag 失败: {e}")
                    # 3. 审计日志
                    try:
                        from lib.control_signals import write_audit_log
                        source = signal.get("source", "unknown")
                        write_audit_log(task.dir, "pause", source=source,
                                        task_id=task.id, detail="子代理执行中暂停（进程冻结）")
                    except Exception as e:
                        logger.error(f"on_paused: write_audit_log 失败: {e}")
                    # 4. 最不重要：推送企微卡片（涉及网络，最可能失败）
                    try:
                        await self._push_step_card(task, "pause")
                    except Exception as e:
                        logger.error(f"on_paused: push_step_card 失败: {e}")
                    # 5. 子任务暂停时，同步更新父任务状态
                    if self._current_parent_task_id and self._current_parent_task_id != task.id:
                        try:
                            parent = self.state.load_task(self._current_parent_task_id)
                            if parent:
                                self.state.pause_task(parent, reason="子任务暂停")
                                self._update_progress(parent, "pause")
                                (parent.dir / "frozen.flag").touch()
                        except Exception as e:
                            logger.error(f"on_paused: 父任务状态同步失败: {e}")

                # 恢复回调：进程被 SIGCONT 解冻时更新状态
                async def on_resumed(signal):
                    try:
                        task.status = "running"
                        self.state._save_state(task)
                        self._update_progress(task, "resume", role=role)
                    except Exception as e:
                        logger.error(f"on_resumed: 状态更新失败: {e}")
                    try:
                        await self._push_step_card(task, "agent_start", role=role)
                    except Exception as e:
                        logger.error(f"on_resumed: push_step_card 失败: {e}")
                    try:
                        (task.dir / "frozen.flag").unlink(missing_ok=True)
                    except Exception as e:
                        logger.error(f"on_resumed: frozen.flag 清理失败: {e}")
                    # 子任务恢复时，同步更新父任务状态
                    if self._current_parent_task_id and self._current_parent_task_id != task.id:
                        try:
                            parent = self.state.load_task(self._current_parent_task_id)
                            if parent:
                                parent.status = "running"
                                self.state._save_state(parent)
                                self._update_progress(parent, "resume", role=role)
                                (parent.dir / "frozen.flag").unlink(missing_ok=True)
                        except Exception as e:
                            logger.error(f"on_resumed: 父任务状态同步失败: {e}")

                kwargs["on_paused"] = on_paused
                kwargs["on_resumed"] = on_resumed

            result = await self._execute_subagent_runtime(
                task=task,
                step_name=step_name,
                **kwargs,
            )
            self._record_runtime_metadata(task, step_name, getattr(result, "runtime_metadata", None))

            # 流式渲染 Agent 尾
            if stream_renderer:
                stream_renderer.finish(
                    duration=result.duration, cost_usd=result.cost_usd,
                    input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                )

            # 上传产出文件获取预览 URL
            preview_url = ""
            output_path = kwargs.get("output_file")
            if output_path and Path(str(output_path)).exists():
                try:
                    preview_url = await self.ui._upload_preview(Path(str(output_path)))
                except Exception:
                    pass

            # 追加 preview_url 到 cost.json 最后一条记录
            if preview_url and task_dir_path:
                cost_file = task_dir_path / "cost.json"
                if cost_file.exists():
                    try:
                        costs = json.loads(cost_file.read_text(encoding="utf-8"))
                        if costs and costs[-1].get("role") == role:
                            costs[-1]["preview_url"] = preview_url
                            cost_file.write_text(json.dumps(costs, indent=2, ensure_ascii=False), encoding="utf-8")
                    except (json.JSONDecodeError, OSError):
                        pass  # 非关键路径

            if self._progress_display:
                self._progress_display.agent_completed(
                    role, result.duration, result.cost_usd,
                    result.input_tokens, result.output_tokens,
                    preview_url=preview_url,
                )
                if preview_url:
                    self._progress_display.live_print(
                        f"📄 {Path(str(output_path)).name} → {preview_url}"
                    )
            # Agent 完成后检查控制信号

            # 更新 progress.json + 企微卡片推送
            if task:
                output_doc_name = (
                    Path(str(output_path)).name
                    if output_path and Path(str(output_path)).exists()
                    else ""
                )
                if step_name:
                    self.state.complete_step(task, step_name)
                self._update_progress(task, "agent_complete", role=role,
                                      cost_usd=result.cost_usd, duration=result.duration,
                                      output_doc=output_doc_name, preview_url=preview_url,
                                      tokens=result.input_tokens + result.output_tokens,
                                      step_name=step_name)
                await self._push_step_card(task, "agent_complete", role=role,
                                           cost_usd=result.cost_usd, duration=result.duration,
                                           output_doc=output_doc_name)

            # Agent 完成后检查控制信号
            if task:
                await self._check_control_signal(task)

            # 记录该步骤实际修改的文件（供精确回滚使用）
            if task and step_name and step_name in task.step_checkpoints:
                try:
                    import subprocess as _sp
                    pre_step_hash = task.step_checkpoints[step_name]
                    _work_dir = kwargs.get("cwd") or self.agent._get_project_path(
                        kwargs.get("project") or self.config.get("default_project", ""))
                    _diff_result = _sp.run(
                        ["git", "diff", "--name-only", pre_step_hash, "HEAD"],
                        cwd=str(_work_dir), capture_output=True, text=True, timeout=30,
                    )
                    if _diff_result.returncode == 0 and _diff_result.stdout.strip():
                        _files = [f.strip() for f in _diff_result.stdout.strip().split("\n") if f.strip()]
                        task.modified_files[step_name] = _files
                        self.state._save_state(task)
                        logger.info(f"步骤 {step_name} 修改了 {len(_files)} 个文件: {_files[:5]}")
                except Exception as _e:
                    logger.warning(f"记录步骤修改文件失败: {_e}")

            return result
        except AgentRateLimitError as e:
            self._record_runtime_metadata(task, step_name, getattr(e, "runtime_metadata", None))
            if stream_renderer:
                stream_renderer.finish(
                    duration=time.time() - start_time, cost_usd=0,
                    input_tokens=0, output_tokens=0, error=str(e)[:80],
                )
            if self._progress_display:
                self._progress_display.agent_error(role, "限流")
            if task:
                self._update_progress(task, "agent_error", role=role,
                                      error_msg="API限流",
                                      duration=time.time() - start_time)
                await self._push_step_card(task, "agent_error", role=role, error_msg="API限流")
            return await self._handle_rate_limit(role, kwargs, e, task=task, step_name=step_name)
        except AgentSignalInterrupt as e:
            self._record_runtime_metadata(task, step_name, getattr(e, "runtime_metadata", None))
            # 子代理执行中收到回退/终止信号（暂停后用户选择回退/终止，或直接终止）
            sig_cost = getattr(e, 'cost_usd', 0.0) or 0.0
            sig_duration = time.time() - start_time
            if stream_renderer:
                stream_renderer.finish(
                    duration=sig_duration, cost_usd=sig_cost,
                    input_tokens=0, output_tokens=0, error=str(e)[:80],
                )
            if self._progress_display:
                self._progress_display.agent_error(role, f"信号中断: {e.action}")
            if task:
                self._update_progress(task, "agent_error", role=role,
                                      error_msg=f"信号中断: {e.action}",
                                      cost_usd=sig_cost, duration=sig_duration)
                from lib.control_signals import write_audit_log
                signal = e.signal
                action = e.action
                source = signal.get("source", "unknown")
                if action == "rollback":
                    target_step = signal.get("target_step", "")
                    feedback = signal.get("feedback", "")
                    logger.info(f"子代理执行中响应回滚信号 → {target_step}")
                    write_audit_log(task.dir, "rollback", source=source,
                                    task_id=task.id, detail=f"回滚到步骤 {target_step}",
                                    target_step=target_step)
                    await self._handle_rollback_signal(task, target_step, feedback)
                    raise WorkflowPaused(f"用户回滚到步骤 {target_step}")
                elif action == "terminate":
                    rollback = signal.get("rollback", False)
                    logger.info(f"子代理执行中响应终止信号 (source={source}, rollback={rollback})")
                    write_audit_log(task.dir, "terminate", source=source,
                                    task_id=task.id, detail=f"子代理执行中终止 (rollback={rollback})")
                    await self._handle_terminate_signal(task, rollback=rollback)
                    step_label = task.current_step or "未知步骤"
                    source_label = {"web": "Web Console", "terminal": "终端", "wecom": "企微"}.get(source, source or "未知来源")
                    raise WorkflowError(f"用户主动终止（来源: {source_label}，终止时步骤: {step_label}，回滚: {'是' if rollback else '否'}）")
            raise
        except (AgentTimeoutError, AgentError) as e:
            self._record_runtime_metadata(task, step_name, getattr(e, "runtime_metadata", None))
            err_cost = getattr(e, 'cost_usd', 0.0) or 0.0
            err_duration = time.time() - start_time
            if stream_renderer:
                stream_renderer.finish(
                    duration=err_duration, cost_usd=err_cost,
                    input_tokens=0, output_tokens=0, error=str(e)[:80],
                    timeout_type=getattr(e, 'timeout_type', None),
                )
            if self._progress_display:
                self._progress_display.agent_error(role, str(e)[:50])
            if task:
                human_error = self._humanize_error(role, e)
                self._update_progress(task, "agent_error", role=role,
                                      error_msg=human_error,
                                      duration=err_duration,
                                      cost_usd=err_cost)
                await self._push_step_card(task, "agent_error", role=role, error_msg=human_error)
            if task and step_name:
                max_total_retries = self.config.get("max_total_retries", 3)
                if _retry_count >= max_total_retries:
                    logger.error(
                        f"Agent [{role}] 步骤 [{step_name}] 已重试 {_retry_count} 次，"
                        f"达到上限 {max_total_retries}，任务标记失败"
                    )
                    self.ui.print_error(
                        f"Agent [{role}] 已重试 {_retry_count} 次达到上限，任务已暂停等待处理"
                    )
                    # 重试耗尽 → 直接抛出，由 run() 的顶层 except 捕获并 fail_task
                    raise
                action = await self._handle_agent_exception(
                    task, step_name, role, e, retry_count=_retry_count
                )
                if action == "retry":
                    return await self._run_agent(
                        task=task, step_name=step_name,
                        _retry_count=_retry_count + 1, **kwargs
                    )
                elif action == "skip":
                    return AgentResult(
                        success=False, data={"status": "skipped", "error": str(e)[:200]},
                        raw_output="", cost_tokens=0, duration=0, exit_code=-1,
                    )
                elif action == "pause":
                    # 保存快照并暂停
                    work_dir = kwargs.get("cwd") or self.agent._get_project_path(
                        kwargs.get("project") or self.config.get("default_project", "")
                    )
                    self.state.pause_task(
                        task, reason=f"{getattr(e, 'error_code', 'E301')} {str(e)[:100]}",
                        save_snapshot=True, step_name=step_name, work_dir=str(work_dir)
                    )
                    raise WorkflowPaused(f"用户选择暂停：{e}")
            raise
        finally:
            # 清理回调引用
            self.agent._on_stream_event_callback = None  # 无 task/step_name 时保持原行为

    async def _run_agent_parallel(self, layout_sm, task=None, step_name=None, **kwargs):
        """并行版 _run_agent：使用 buffer_mode StreamRenderer + 参数传递回调

        与 _run_agent 的关键区别：
        1. 创建 buffer_mode=True 的 StreamRenderer（写入内存缓冲而非直接 console.print）
        2. 回调通过 on_action/on_stream_event 参数传递给 agent.run()，不设置实例属性
        3. 完成/失败时通知 LayoutStateMachine
        """
        from stream_renderer import StreamRenderer

        role = kwargs.get("role", "unknown")
        await self._check_budget(role)

        # 检查中断
        interrupt = await self.ui.check_interrupt()
        if interrupt == "cancel":
            raise WorkflowError(f"用户中断任务（cancel）")
        elif interrupt == "modify":
            raise WorkflowError(f"用户请求修改方案（modify）")

        # 进度显示
        model = (self.config.get("model_overrides", {}).get(role)
                 or self.agent.DEFAULT_MODELS.get(role, "sonnet"))
        fallback = self.agent.FALLBACK_MODELS.get(model, "") or ""
        output_file = str(kwargs.get("output_file", "")) if kwargs.get("output_file") else ""
        if self._progress_display:
            self._progress_display.agent_started(role, model, output_file=output_file, fallback=fallback)

        # 创建动作回调（通过参数传递，不设置实例属性）
        def on_action(action):
            if self._progress_display:
                self._progress_display.update_action(role, action)

        # 创建 buffer_mode StreamRenderer
        stream_renderer = None
        task_dir_path = Path(str(kwargs.get("task_dir", ""))) if kwargs.get("task_dir") else None
        if task_dir_path:
            log_dir = task_dir_path / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            _log_step_id = step_name or (task.current_step if task else None) or role
            log_path = log_dir / f"{_log_step_id}.stream.log"

            stream_renderer = StreamRenderer(
                role=role, model=model,
                console=None,  # buffer 模式不需要 console
                log_path=log_path,
                buffer_mode=True,
            )
            stream_renderer.start()

        _action_step_id = step_name or (task.current_step if task else None) or role
        def on_stream_event(event, _sid=_action_step_id):
            if stream_renderer:
                stream_renderer.feed(event)
            if task:
                self._try_publish_step_action(task.id, _sid, event)

        # 注册到状态机（会在第 2 个 agent 注册时触发 SPLIT）
        if stream_renderer:
            layout_sm.agent_started(role, stream_renderer)

        start_time = time.time()

        # 更新 progress.json + 企微卡片推送
        if task:
            self._update_progress(task, "agent_start", role=role, model=model, step_name=step_name)
            await self._push_step_card(task, "agent_start", role=role)

        try:
            # 注入 task_id 以便子代理执行中轮询控制信号
            if task:
                kwargs["task_id"] = task.id

                async def on_paused_parallel(signal):
                    try:
                        self.state.pause_task(task, reason="用户主动暂停")
                    except Exception as e:
                        logger.error(f"on_paused_parallel: pause_task 失败: {e}")
                    try:
                        self._update_progress(task, "pause")
                    except Exception as e:
                        logger.error(f"on_paused_parallel: update_progress 失败: {e}")
                    try:
                        (task.dir / "frozen.flag").touch()
                    except Exception as e:
                        logger.error(f"on_paused_parallel: frozen.flag 失败: {e}")
                    try:
                        from lib.control_signals import write_audit_log
                        source = signal.get("source", "unknown")
                        write_audit_log(task.dir, "pause", source=source,
                                        task_id=task.id, detail="子代理执行中暂停（进程冻结）")
                    except Exception as e:
                        logger.error(f"on_paused_parallel: write_audit_log 失败: {e}")
                    try:
                        await self._push_step_card(task, "pause")
                    except Exception as e:
                        logger.error(f"on_paused_parallel: push_step_card 失败: {e}")

                async def on_resumed_parallel(signal):
                    try:
                        task.status = "running"
                        self.state._save_state(task)
                        self._update_progress(task, "resume", role=role)
                    except Exception as e:
                        logger.error(f"on_resumed_parallel: 状态更新失败: {e}")
                    try:
                        (task.dir / "frozen.flag").unlink(missing_ok=True)
                    except Exception as e:
                        logger.error(f"on_resumed_parallel: frozen.flag 清理失败: {e}")

                kwargs["on_paused"] = on_paused_parallel
                kwargs["on_resumed"] = on_resumed_parallel

            result = await self._execute_subagent_runtime(
                task=task,
                step_name=step_name,
                on_action=on_action,
                on_stream_event=on_stream_event,
                **kwargs,
            )
            self._record_runtime_metadata(task, step_name, getattr(result, "runtime_metadata", None))

            # 流式渲染 Agent 尾
            if stream_renderer:
                stream_renderer.finish(
                    duration=result.duration, cost_usd=result.cost_usd,
                    input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                )

            # 通知状态机完成
            stats = {
                "duration": result.duration,
                "cost_usd": result.cost_usd,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
            }
            layout_sm.agent_finished(role, stats)

            # 上传产出文件获取预览 URL
            preview_url = ""
            output_path = kwargs.get("output_file")
            if output_path and Path(str(output_path)).exists():
                try:
                    preview_url = await self.ui._upload_preview(Path(str(output_path)))
                except Exception:
                    pass

            # 追加 preview_url 到 cost.json 最后一条记录
            if preview_url and task_dir_path:
                cost_file = task_dir_path / "cost.json"
                if cost_file.exists():
                    try:
                        costs = json.loads(cost_file.read_text(encoding="utf-8"))
                        if costs and costs[-1].get("role") == role:
                            costs[-1]["preview_url"] = preview_url
                            cost_file.write_text(json.dumps(costs, indent=2, ensure_ascii=False), encoding="utf-8")
                    except (json.JSONDecodeError, OSError):
                        pass  # 非关键路径

            if self._progress_display:
                self._progress_display.agent_completed(
                    role, result.duration, result.cost_usd,
                    result.input_tokens, result.output_tokens,
                    preview_url=preview_url,
                )
                if preview_url:
                    self._progress_display.live_print(
                        f"📄 {Path(str(output_path)).name} → {preview_url}"
                    )

            # 更新 progress.json + 企微卡片推送
            if task:
                output_doc_name = (
                    Path(str(output_path)).name
                    if output_path and Path(str(output_path)).exists()
                    else ""
                )
                if step_name:
                    self.state.complete_step(task, step_name)
                self._update_progress(task, "agent_complete", role=role,
                                      cost_usd=result.cost_usd, duration=result.duration,
                                      output_doc=output_doc_name, preview_url=preview_url,
                                      tokens=result.input_tokens + result.output_tokens,
                                      step_name=step_name)
                await self._push_step_card(task, "agent_complete", role=role,
                                           cost_usd=result.cost_usd, duration=result.duration,
                                           output_doc=output_doc_name)

            return result

        except AgentSignalInterrupt as e:
            self._record_runtime_metadata(task, step_name, getattr(e, "runtime_metadata", None))
            # 子代理执行中收到回退/终止信号（暂停后用户选择回退/终止，或直接终止）
            sig_cost = getattr(e, 'cost_usd', 0.0) or 0.0
            sig_duration = time.time() - start_time
            if stream_renderer:
                stream_renderer.finish(
                    duration=sig_duration, cost_usd=sig_cost,
                    input_tokens=0, output_tokens=0, error=str(e)[:80],
                )
            layout_sm.agent_finished(role, {"error": str(e)[:100]})
            if self._progress_display:
                self._progress_display.agent_error(role, f"信号中断: {e.action}")
            if task:
                self._update_progress(task, "agent_error", role=role,
                                      error_msg=f"信号中断: {e.action}",
                                      cost_usd=sig_cost, duration=sig_duration)
                from lib.control_signals import write_audit_log
                signal = e.signal
                action = e.action
                source = signal.get("source", "unknown")
                if action == "rollback":
                    target_step = signal.get("target_step", "")
                    feedback = signal.get("feedback", "")
                    write_audit_log(task.dir, "rollback", source=source,
                                    task_id=task.id, detail=f"回滚到步骤 {target_step}",
                                    target_step=target_step)
                    await self._handle_rollback_signal(task, target_step, feedback)
                    raise WorkflowPaused(f"用户回滚到步骤 {target_step}")
                elif action == "terminate":
                    rollback = signal.get("rollback", False)
                    write_audit_log(task.dir, "terminate", source=source,
                                    task_id=task.id, detail=f"子代理执行中终止 (rollback={rollback})")
                    await self._handle_terminate_signal(task, rollback=rollback)
                    step_label = task.current_step or "未知步骤"
                    source_label = {"web": "Web Console", "terminal": "终端", "wecom": "企微"}.get(source, source or "未知来源")
                    raise WorkflowError(f"用户主动终止（来源: {source_label}，终止时步骤: {step_label}，回滚: {'是' if rollback else '否'}）")
            raise
        except (AgentTimeoutError, AgentError, AgentRateLimitError) as e:
            self._record_runtime_metadata(task, step_name, getattr(e, "runtime_metadata", None))
            if stream_renderer:
                stream_renderer.finish(
                    duration=time.time() - start_time, cost_usd=0,
                    input_tokens=0, output_tokens=0, error=str(e)[:80],
                    timeout_type=getattr(e, 'timeout_type', None),
                )
            # 通知状态机失败（按完成处理，显示错误状态）
            layout_sm.agent_finished(role, {"error": str(e)[:100]})
            if self._progress_display:
                self._progress_display.agent_error(role, str(e)[:50])
            if task:
                self._update_progress(task, "agent_error", role=role,
                                      error_msg=str(e)[:200],
                                      duration=time.time() - start_time)
                await self._push_step_card(task, "agent_error", role=role, error_msg=str(e)[:60])
            raise

    async def _handle_agent_exception(
        self, task: Task, step_name: str, role: str, error: Exception,
        retry_count: int = 0,
    ) -> str:
        """统一异常处理：可恢复错误自动重试，不可恢复才通知用户

        Args:
            retry_count: 当前已重试次数（由 _run_agent 传入）
        """
        error_code = getattr(error, "error_code", "E301")
        max_auto_retries = self.config.get("max_auto_retries", 2)
        runtime_metadata = getattr(error, "runtime_metadata", {}) or {}
        if runtime_metadata.get("runtime_family") == "codex":
            max_auto_retries = self.config.get("codex_subagent_auto_retries", max_auto_retries)
        try:
            max_auto_retries = int(max_auto_retries)
        except (TypeError, ValueError):
            max_auto_retries = 2

        # 可自动重试的错误类型
        auto_retryable = {"E101", "E102", "E201", "E202", "E204", "E303", "E305"}

        # 如果是可自动恢复的错误且未超过自动重试限制 → 直接重试，不阻塞等确认
        if error_code in auto_retryable and retry_count < max_auto_retries:
            type_desc_map = {
                "E101": "空闲超时", "E102": "绝对超时",
                "E201": "API 限流", "E202": "服务过载",
                "E204": "Codex 云端预检超时", "E303": "MCP 服务失败",
                "E305": "Codex CLI 重连失败",
            }
            self.ui.print_warning(
                f"Agent [{role}] {type_desc_map.get(error_code, '异常')}，"
                f"自动重试 ({retry_count + 1}/{max_auto_retries})"
            )
            logger.info(
                f"自动重试 {role} 步骤 {step_name}，错误码 {error_code}，"
                f"重试 {retry_count + 1}/{max_auto_retries}"
            )
            return "retry"

        # 不可自动恢复 或 已超过自动重试限制 → 通知用户
        type_desc_map = {
            "E101": "空闲超时", "E102": "绝对超时",
            "E201": "API 限流", "E202": "服务过载",
            "E204": "Codex 云端预检超时",
            "E301": "进程崩溃", "E302": "结果解析失败", "E303": "MCP 服务失败",
            "E305": "Codex CLI 重连失败",
        }
        error_type_desc = type_desc_map.get(error_code, "未知错误")

        retry_info = f"\n已自动重试 {retry_count} 次" if retry_count > 0 else ""
        message = (
            f"⚠️ Agent [{role}] 在步骤 [{step_name}] 发生异常\n\n"
            f"异常码：{error_code}\n"
            f"类型：{error_type_desc}\n"
            f"详情：{str(error)[:200]}{retry_info}"
        )

        _ctx = self._build_confirm_context(task, role=role, button_set="agent_exception")
        self.state.set_pending_confirm(task, "", "confirm_with_feedback",
                                       f"Agent [{role}] 异常，请选择操作",
                                       button_set=_ctx.button_set)
        self._update_progress(task, "waiting_confirm",
                              pending_confirm=task.pending_confirm)
        action = await self.ui.confirm_with_feedback(message, context=_ctx)
        self.state.clear_pending_confirm(task)
        self._update_progress(task, "confirm_resolved")

        if action == "confirm":
            return "retry"
        elif action == "cancel":
            return "pause"
        elif action == "terminate":
            # 检查是否有代码变更，有则追问是否回滚
            rollback = False
            has_changes = self._task_has_code_changes(task)
            if has_changes:
                rollback_ctx = ConfirmContext(button_set="terminate_rollback_choice")
                rollback_action = await self.ui.confirm_with_feedback(
                    "此任务已修改代码，请选择处理方式：\n"
                    "1. 🔄 回滚代码\n"
                    "2. 📌 保留代码",
                    context=rollback_ctx,
                )
                rollback = (rollback_action == "confirm")
            self.ui.print_warning(f"用户终止任务（{'回滚代码' if rollback else '保留代码'}）")
            await self._handle_terminate_signal(task, rollback=rollback)
            step_label = task.current_step or "未知步骤"
            source_label = {"wecom": "企微"}.get(self.ui.mode, "确认交互")
            raise WorkflowError(f"用户主动终止（来源: {source_label}，终止时步骤: {step_label}，回滚: {'是' if rollback else '否'}）")
        else:
            return "pause"  # 默认暂停

    # 错误信息中文映射
    _ERROR_TYPE_CN = {
        "E101": "执行空闲超时",
        "E102": "执行总时间超时",
        "E201": "API 调用限流",
        "E202": "API 服务过载",
        "E204": "Codex 云端预检超时",
        "E301": "执行过程中遇到异常",
        "E302": "输出结果解析失败",
        "E303": "代码工具服务暂时不可用",
        "E305": "Codex CLI 重连失败",
    }

    def _humanize_error(self, role: str, error: Exception) -> str:
        """将技术异常转化为用户可读的中文摘要"""
        from stream_renderer import ROLE_DISPLAY
        try:
            if role.startswith("sub-task:"):
                role_cn = f"子任务：{role[len('sub-task:'):]}"
            else:
                role_cn = ROLE_DISPLAY.get(role, role)
            error_code = getattr(error, "error_code", "E301")
            type_cn = self._ERROR_TYPE_CN.get(error_code, "执行遇到问题")

            raw = str(error)
            if raw.startswith("{") or "\\n" in raw[:50]:
                detail = type_cn
            else:
                first_line = raw.split("\n")[0][:100]
                if "] " in first_line:
                    first_line = first_line.split("] ", 1)[-1]
                detail = first_line

            msg = f"{role_cn}{type_cn}：{detail}"
            if len(msg) > 200:
                msg = msg[:197] + "..."
            return msg
        except Exception:
            role_cn = ROLE_DISPLAY.get(role, role) if not role.startswith("sub-task:") else role
            return f"{role_cn}执行遇到问题，请查看详细日志。"

    async def _handle_rate_limit(self, role: str, kwargs: dict, error, task: Task | None = None, step_name: str | None = None):
        """处理限流：提示用户选择降级方案"""
        self.ui.print_warning(f"Agent [{role}] 3次重试后仍被限流")

        # 非交互模式（后台进程 stdin=/dev/null）：自动等待一次后放弃
        if not sys.stdin.isatty():
            logger.warning(f"非交互模式，限流后等待 60s 重试一次")
            self.ui.print_info("非交互模式，等待 60s 后重试...")
            task_id = kwargs.get("task_id")
            for _ in range(20):  # 20 * 3s = 60s
                await asyncio.sleep(3)
                if task_id:
                    from lib.control_signals import read_signal
                    sig = read_signal(task_id)
                    if sig and sig.get("action") in ("pause", "terminate", "rollback"):
                        raise WorkflowError(f"限流等待期间收到{sig['action']}信号，停止重试")
            try:
                result = await self._execute_subagent_runtime(
                    task=task,
                    step_name=step_name,
                    **{**kwargs, "_retry_count": 0},
                )
                self._record_runtime_metadata(task, step_name, getattr(result, "runtime_metadata", None))
                return result
            except Exception as e:
                logger.error(f"限流重试仍失败: {e}")
                raise WorkflowError(f"Agent [{role}] 限流重试失败: {e}")

        # 构建选项
        can_use_external = role in self.agent.TEXT_ONLY_ROLES
        tool_warning = "" if can_use_external else "（⚠️ 该角色需要工具，降级后只能产出文本建议，不能直接改代码）"

        options = ["等待 5 分钟后重试（Claude）"]
        for key, info in self.agent.EXTERNAL_MODELS.items():
            options.append(f"降级到 {info['label']}{tool_warning}")
        options.append("跳过此步骤")
        options.append("取消任务")

        print("\n请选择：")
        for i, opt in enumerate(options, 1):
            print(f"  [{i}] {opt}")

        choice = input("\n选择编号: ").strip()
        try:
            idx = int(choice)
        except ValueError:
            idx = 1  # 默认等待重试

        if idx == 1:
            # 等待 5 分钟后重试（每 3 秒检查控制信号）
            self.ui.print_info("等待 5 分钟后重试...")
            task_id = kwargs.get("task_id")
            for _ in range(100):  # 100 * 3s = 300s = 5 分钟
                await asyncio.sleep(3)
                if task_id:
                    from lib.control_signals import read_signal
                    sig = read_signal(task_id)
                    if sig and sig.get("action") in ("pause", "terminate", "rollback"):
                        raise WorkflowError(f"限流等待期间收到{sig['action']}信号，停止重试")
            result = await self._execute_subagent_runtime(
                task=task,
                step_name=step_name,
                **{**kwargs, "_retry_count": 0},
            )
            self._record_runtime_metadata(task, step_name, getattr(result, "runtime_metadata", None))
            return result

        external_keys = list(self.agent.EXTERNAL_MODELS.keys())
        if 2 <= idx <= 1 + len(external_keys):
            # 使用外部模型
            model_key = external_keys[idx - 2]
            prompt = await self.agent._build_prompt(
                kwargs.get("role"), kwargs.get("task_dir"),
                kwargs.get("input_docs"), kwargs.get("memories"),
                kwargs.get("output_file"), kwargs.get("project"),
                kwargs.get("session_recall", ""),
                kwargs.get("work_state_summary", ""),
            )
            return await self.agent.run_with_external_model(
                role=role, prompt=prompt, model_key=model_key,
                output_file=kwargs.get("output_file"),
                task_dir=kwargs.get("task_dir"),
            )

        if idx == len(options) - 1:
            # 跳过此步骤
            from agent_runner import AgentResult
            self.ui.print_warning(f"已跳过 [{role}]")
            return AgentResult(
                success=True, data={"status": "skipped"},
                raw_output="", cost_tokens=0, duration=0, exit_code=0,
            )

        # 取消任务
        raise WorkflowError(f"用户在限流后取消任务")


# ─── 模块级辅助函数 ───────────────────────────────────────

def topological_sort_into_batches(sub_tasks: list) -> list:
    """拓扑排序，分成可并行的批次

    sub_tasks: [{"id": 1, "depends_on": []}, {"id": 2, "depends_on": [1]}, ...]
    返回: [[task1, task3], [task2, task4], ...]  每个内层列表可并行

    Raises:
        WorkflowError: 循环依赖或引用不存在的任务 id
    """
    if not sub_tasks:
        return []

    in_degree = {}
    graph = {}
    task_map = {}

    for t in sub_tasks:
        tid = t["id"]
        task_map[tid] = t
        in_degree[tid] = 0
        graph[tid] = []

    for t in sub_tasks:
        for dep in t.get("depends_on", []):
            if dep not in task_map:
                raise WorkflowError(f"子任务 {t['id']} 依赖不存在的任务 {dep}")
            graph[dep].append(t["id"])
            in_degree[t["id"]] += 1

    batches = []
    queue = [tid for tid, deg in in_degree.items() if deg == 0]
    processed = 0

    while queue:
        batch = [task_map[tid] for tid in queue]
        batches.append(batch)
        processed += len(queue)

        next_queue = []
        for tid in queue:
            for neighbor in graph.get(tid, []):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    next_queue.append(neighbor)
        queue = next_queue

    if processed < len(sub_tasks):
        raise WorkflowError(f"子任务存在循环依赖，已处理 {processed}/{len(sub_tasks)} 个任务")

    return batches
