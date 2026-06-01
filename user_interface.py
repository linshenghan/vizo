#!/usr/bin/env python3
"""用户界面：终端 + 企微两种模式

优化记录:
- 优化1: 卡片+文本双推送 (_wecom_send_dual)
- 优化2: Web 页面回复替代企微对话框 (_create_web_confirm, _wecom_wait_web_reply)
- 优化3: 回复确认反馈 (_send_reply_feedback)
- 优化4a: 重复推送提醒机制 (remind_schedule in _wecom_wait_web_reply)
- 优化5a: 中断检查 (check_interrupt)
"""

import asyncio
import hashlib
import json
import logging
import select
import time
import uuid
from pathlib import Path

from lib.paths import CONFIRMS_DIR, PREVIEWS_DIR
from lib.preview_server import get_base_url
from lib.web_paths import absolute_url, confirm_path, preview_path, task_detail_path

logger = logging.getLogger(__name__)

# 中断指令提示（追加到企微消息末尾）
from dataclasses import dataclass, field


@dataclass
class ConfirmContext:
    """确认消息的上下文元数据"""
    task_name: str = ""         # 任务描述（task.description）
    task_id: str = ""           # 任务 ID
    role: str = ""              # 当前角色（如 "requirement_analyst"）
    role_display: str = ""      # 角色中文名（如 "需求分析师"）
    step: str = ""              # 当前步骤（如 "requirement_analysis"）
    step_display: str = ""      # 步骤中文名（如 "需求分析"）
    model: str = ""             # 使用的模型（如 "claude-opus-4-6"）
    # 当前步骤费用
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    duration: float = 0.0       # 秒
    # 任务累计费用
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    # 进度信息
    completed_steps: list = field(default_factory=list)  # ["需求分析", "架构设计"]
    total_steps: int = 0        # 工作流总步骤数（估算）
    # 文档信息
    file_path: str = ""         # 文档本地路径
    button_set: str = "generic_confirm"  # 按钮集 ID


BUTTON_SET_CONFIGS = {
    "doc_review": {
        "buttons": [
            {"label": "✓ 确认继续", "action": "y", "icon": "check"},
            {"label": "✎ 补充修改意见", "action": "f", "icon": "edit"},
            {"label": "✗ 取消任务", "action": "n", "icon": "cancel", "confirm_required": True},
        ],
        "wecom_hint": "📋 快捷回复：1=确认继续 2=修改意见 3=取消任务",
    },
    "doc_review_discussion": {
        "buttons": [
            {"label": "✓ 确认继续", "action": "y", "icon": "check"},
            {"label": "✎ 补充修改意见", "action": "f", "icon": "edit"},
            {"label": "💬 发起讨论", "action": "d", "icon": "chat"},
            {"label": "✗ 取消任务", "action": "n", "icon": "cancel", "confirm_required": True},
        ],
        "wecom_hint": "📋 快捷回复：1=确认 2=修改意见 3=发起讨论 4=取消",
    },
    "agent_exception": {
        "buttons": [
            {"label": "🔄 重试", "action": "y", "icon": "retry"},
            {"label": "⏸ 暂停等待", "action": "n", "icon": "pause"},
            {"label": "🛑 终止任务", "action": "terminate", "icon": "stop"},
        ],
        "wecom_hint": "⚡ 快捷回复：1=重试 2=暂停等待 3=终止任务",
    },
    "terminate_rollback_choice": {
        "buttons": [
            {"label": "🔄 回滚代码", "action": "confirm", "icon": "rollback"},
            {"label": "📌 保留代码", "action": "cancel", "icon": "keep"},
        ],
        "wecom_hint": "⚠️ 此任务已修改代码，请选择：1=回滚代码 2=保留代码",
    },
    "subtask_failure": {
        "buttons": [
            {"label": "🔄 恢复（重试失败子任务）", "action": "resume", "icon": "resume"},
            {"label": "🛑 终止任务", "action": "terminate", "icon": "stop"},
            {"label": "💬 提供修改意见", "action": "f", "icon": "edit"},
        ],
        "wecom_hint": "⚠️ 子任务失败：1=恢复重试 2=终止任务 3=修改意见",
    },
    "deploy_confirm": {
        "buttons": [
            {"label": "✓ 确认部署", "action": "y", "icon": "deploy"},
            {"label": "✗ 取消", "action": "n", "icon": "cancel"},
        ],
        "wecom_hint": "🚀 快捷回复：1=确认部署 2=取消",
    },
    "generic_confirm": {
        "buttons": [
            {"label": "✓ 确认", "action": "y", "icon": "check"},
            {"label": "✗ 取消", "action": "n", "icon": "cancel"},
            {"label": "✎ 补充意见", "action": "f", "icon": "edit"},
        ],
        "wecom_hint": "📋 快捷回复：1=确认 2=取消 3=补充意见",
    },
    "doc_review_ra_design": {
        "buttons": [
            {"label": "✓ 确认继续", "action": "y", "icon": "check"},
            {"label": "✎ 补充修改意见", "action": "f", "icon": "edit"},
            {"label": "💬 发起讨论", "action": "d", "icon": "chat"},
            {"label": "🎨 发起交互设计", "action": "i", "icon": "design"},
            {"label": "✗ 取消任务", "action": "n", "icon": "cancel", "confirm_required": True},
        ],
        "wecom_hint": "📋 快捷回复：1=确认 2=修改意见 3=讨论 4=交互设计 5=取消",
    },
    "doc_review_pm_design": {
        "buttons": [
            {"label": "✓ 确认继续", "action": "y", "icon": "check"},
            {"label": "✎ 补充修改意见", "action": "f", "icon": "edit"},
            {"label": "🎨 发起交互设计", "action": "i", "icon": "design"},
            {"label": "✗ 取消任务", "action": "n", "icon": "cancel", "confirm_required": True},
        ],
        "wecom_hint": "📋 快捷回复：1=确认 2=修改意见 3=交互设计 4=取消",
    },
}


INTERRUPT_TIPS = "\n\n---\n💡 随时回复「叫停」「中断」「暂停」或「stop」可中断当前流程"


class UserInterface:
    """用户交互"""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.mode = self.config.get("ui_mode", "terminal")
        self._wecom_enabled = self.config.get("wecom", {}).get("enabled", False)
        self._current_task_id = None
        # 企微 access_token 缓存
        self._access_token = None
        self._token_expires = 0
        # WeComNotifier 实例缓存（优化1）
        self._notifier = None
        # Web 页面内联提交的修改内容（优化2: 无需二次交互）
        self._pending_feedback = None
        # 进度显示实例（优化4b）
        self._progress_display = None
        # 自动确认模式：跳过所有人工确认，直接 confirm
        self.auto_confirm = False

    # ─────────────── 确认交互 ───────────────

    def _pause_progress_display(self):
        """暂停 Rich Live 进度显示，防止覆盖 input() 提示。"""
        if self._progress_display and self._progress_display._live:
            try:
                self._progress_display._live.stop()
            except Exception:
                pass

    def _resume_progress_display(self):
        """恢复 Rich Live 进度显示（pause 后调用）"""
        if self._progress_display and self._progress_display._live:
            try:
                if not self._progress_display._live.is_started:
                    self._progress_display._live.start()
            except Exception:
                pass

    def pause_progress(self):
        """Public wrapper used by confirmation flows before blocking prompts."""
        self._pause_progress_display()

    def resume_progress(self):
        """Public wrapper used by confirmation flows after blocking prompts."""
        self._resume_progress_display()

    async def confirm(self, message: str) -> bool:
        if self.auto_confirm:
            logger.info(f"[auto-confirm] 自动确认: {message}")
            return True
        if self.mode == "terminal":
            return self._terminal_confirm(message)
        else:
            return await self._wecom_confirm(message)

    def _terminal_confirm(self, message: str) -> bool:
        import sys
        if not sys.stdin.isatty():
            # 非 TTY 环境：通过 confirm_bridge 跨进程确认
            return self._bridge_confirm(message, req_type="confirm")
        self.pause_progress()
        try:
            choice = input(f"\n{message} [y/n]: ").strip().lower()
        finally:
            self.resume_progress()
        return choice in ("y", "yes", "")

    def _bridge_confirm(self, message: str, req_type: str = "confirm") -> bool:
        """通过 confirm_bridge 跨进程确认（非 TTY 环境）

        创建 .vizo/confirms/{id}.request.json 并阻塞等待父进程响应。
        父进程的 PreToolUse hook 会自动检测请求并提示用户。
        """
        from lib.confirm_bridge import create_request, wait_for_response

        task_id = self._current_task_id or "default"
        request_id = create_request(
            task_id=task_id,
            message=message,
            req_type=req_type,
        )
        logger.info(f"确认请求已创建: {request_id}, 等待父进程响应...")
        action, _ = wait_for_response(request_id, timeout=7200)
        return action in ("confirm", "y", "yes", "approve")

    def _bridge_confirm_with_feedback(self, message: str, file: Path = None,
                                       allow_discussion: bool = False,
                                       context: "ConfirmContext" = None) -> tuple:
        """通过 confirm_bridge 跨进程确认（支持修改意见）

        返回: (action, feedback)
              action = 'confirm' | 'cancel' | 'feedback' | 'discussion' | 'interaction_design'
              feedback = 用户修改意见或空字符串
        """
        from lib.confirm_bridge import create_request, wait_for_response

        task_id = self._current_task_id or "default"
        summary = self._summarize_file(file) if file else ""
        req_type = "confirm_with_discussion" if allow_discussion else "confirm_with_feedback"

        context_dict = None
        if context:
            context_dict = {
                "task_name": context.task_name,
                "task_id": context.task_id,
                "role": context.role,
                "role_display": context.role_display,
                "step": context.step,
                "step_display": context.step_display,
                "model": context.model,
                "cost_usd": context.cost_usd,
                "input_tokens": context.input_tokens,
                "output_tokens": context.output_tokens,
                "duration": context.duration,
                "total_cost_usd": context.total_cost_usd,
                "total_tokens": context.total_tokens,
                "completed_steps": context.completed_steps,
                "total_steps": context.total_steps,
                "file_path": context.file_path,
                "button_set": context.button_set,
            }

        request_id = create_request(
            task_id=task_id,
            message=message,
            file_path=file,
            summary=summary,
            req_type=req_type,
            context=context_dict,
        )
        logger.info(f"确认请求已创建: {request_id}, 等待父进程响应...")
        action, feedback = wait_for_response(request_id, timeout=7200)
        return action, feedback

    @staticmethod
    def _build_confirm_prompt(allow_discussion: bool = False,
                              context: "ConfirmContext" = None) -> tuple:
        """按 button_set 生成终端确认提示和动作映射。"""
        action_map = {"y": "confirm", "n": "cancel", "f": "feedback",
                      "d": "discussion", "i": "interaction_design"}
        action_labels = {"y": "确认", "n": "取消", "f": "补充意见",
                         "d": "发起讨论", "i": "交互设计"}
        button_set = context.button_set if context else None
        button_config = BUTTON_SET_CONFIGS.get(button_set) if button_set else None
        if button_config:
            actions = [btn["action"] for btn in button_config["buttons"]]
            prompt_parts = [
                f"[{action}] {action_labels.get(action, action)}"
                for action in actions
                if action in action_labels
            ]
            mapping = {
                action: action_map[action]
                for action in actions
                if action in action_map
            }
            return prompt_parts, mapping
        if allow_discussion:
            return (
                ["[y] 确认", "[n] 取消", "[f] 补充意见", "[d] 发起讨论"],
                {"y": "confirm", "n": "cancel", "f": "feedback", "d": "discussion"},
            )
        return (
            ["[y] 确认", "[n] 取消", "[f] 修改意见"],
            {"y": "confirm", "n": "cancel", "f": "feedback"},
        )

    async def _wait_for_terminal_or_web_reply(self, prompt: str, mapping: dict,
                                              web_request_id: str = "",
                                              timeout: int = 7200) -> tuple:
        """TTY 模式下同时接受终端输入和 Web 确认页回执。"""
        import sys
        import redis.asyncio as aioredis

        redis_client = None
        response_key = ""
        if web_request_id:
            preview_redis_url = self.config.get(
                "preview_redis_url", "redis://localhost:6380")
            redis_client = aioredis.from_url(
                preview_redis_url,
                socket_connect_timeout=0.3,
                socket_timeout=0.3,
            )
            response_key = f"response:{web_request_id}"

        deadline = time.time() + timeout
        print("   " + prompt + ": ", end="", flush=True)
        try:
            while time.time() < deadline:
                if redis_client and response_key:
                    try:
                        web_reply = await asyncio.wait_for(
                            redis_client.get(response_key), timeout=0.5)
                        if web_reply:
                            await asyncio.wait_for(
                                redis_client.delete(response_key), timeout=0.5)
                            reply_text = (web_reply.decode("utf-8", errors="replace")
                                          if isinstance(web_reply, bytes)
                                          else str(web_reply))
                            print()
                            return self._parse_web_reply(reply_text)
                    except Exception as e:
                        logger.warning(f"Web 确认回执 Redis 不可用，降级为终端输入: {e}")
                        try:
                            await redis_client.close()
                        except Exception:
                            pass
                        redis_client = None

                ready, _, _ = select.select([sys.stdin], [], [], 1)
                if ready:
                    choice = sys.stdin.readline().strip().lower()
                    return mapping.get(choice, "confirm"), ""

            raise TimeoutError("等待回复超时")
        finally:
            if redis_client:
                await redis_client.close()

    async def confirm_with_feedback(self, message: str, file: Path = None,
                                     allow_discussion: bool = False,
                                     context: "ConfirmContext" = None,
                                     force: bool = False) -> str:
        """返回: 'confirm' | 'cancel' | 'feedback' | 'discussion' | 'interaction_design'"""

        # ── auto-confirm 模式：跳过交互，直接确认（force=True 时不跳过）──
        if self.auto_confirm and not force:
            logger.info(f"[auto-confirm] 自动确认(with_feedback): {message}")
            return "confirm"

        # ── 统一企微推送（不管 TTY / 非 TTY / wecom 模式，推送内容一致）──
        summary = self._summarize_file(file, max_chars=500) if file else ""
        preview_link = ""
        web_url = ""
        web_request_id = ""
        bridge_request_id = ""  # 仅非 TTY terminal 使用

        # ── 始终创建 Web 确认页（confirm_server 独立于企微运行）──
        try:
            preview_link = await self._upload_preview(file)
            web_request_id, web_url = await self._create_web_confirm(
                title=message,
                summary=summary or "请查看并确认",
                preview_link=preview_link,
                allow_discussion=allow_discussion,
                context=context,
            )
        except Exception as e:
            logger.warning(f"Web 确认页创建失败: {e}")

        # ── 企微通知（可选）──
        if self.config.get("wecom", {}).get("enabled") and web_url:
            try:
                await self._wecom_send_dual(
                    title=message[:60],
                    summary=summary[:200] if summary else "请查看并确认",
                    web_url=web_url,
                    preview_link=preview_link,
                    allow_discussion=allow_discussion,
                    context=context,
                )
            except Exception as e:
                logger.warning(f"企微确认通知推送失败: {e}")

        # ── 按模式等待用户响应 ──
        if self.mode == "terminal":
            import sys
            if not sys.stdin.isatty():
                # 非 TTY：通过 confirm_bridge 文件 IPC 跨进程确认
                from lib.confirm_bridge import create_request, wait_for_response

                task_id = self._current_task_id or "default"
                req_type = ("confirm_with_discussion"
                            if allow_discussion else "confirm_with_feedback")

                context_dict = None
                if context:
                    context_dict = {
                        "task_name": context.task_name,
                        "task_id": context.task_id,
                        "role": context.role,
                        "role_display": context.role_display,
                        "step": context.step,
                        "step_display": context.step_display,
                        "model": context.model,
                        "cost_usd": context.cost_usd,
                        "input_tokens": context.input_tokens,
                        "output_tokens": context.output_tokens,
                        "duration": context.duration,
                        "total_cost_usd": context.total_cost_usd,
                        "total_tokens": context.total_tokens,
                        "completed_steps": context.completed_steps,
                        "total_steps": context.total_steps,
                        "file_path": context.file_path,
                        "button_set": context.button_set,
                    }

                bridge_request_id = create_request(
                    task_id=task_id,
                    message=message,
                    file_path=file,
                    summary=summary,
                    preview_link=preview_link,
                    req_type=req_type,
                    context=context_dict,
                )
                logger.info(f"确认请求已创建: {bridge_request_id}")

                # 关联 Web 确认页 → bridge，让 Web 端回复也能触发 bridge
                if web_request_id and bridge_request_id:
                    await self._link_web_to_bridge(
                        web_request_id, bridge_request_id)

                # 输出 Web 确认链接到 stderr，供 CLI 用户直接访问
                if web_url:
                    import sys as _sys
                    print(f"OPUS_CONFIRM_URL|{bridge_request_id}|{web_url}",
                          file=_sys.stderr, flush=True)

                action, feedback = wait_for_response(
                    bridge_request_id, timeout=7200)
                if action == "feedback" and feedback:
                    self._pending_feedback = feedback
                return action

            # TTY：终端交互
            self.pause_progress()
            try:
                print(f"\n{message}")
                if context:
                    if context.task_name:
                        print(f"   📋 任务：{context.task_name}")
                    if context.role_display:
                        print(f"   🤖 角色：{context.role_display}")
                    if context.duration > 0:
                        m, s = divmod(int(context.duration), 60)
                        dur_str = f"{m}分{s}秒" if m else f"{s}秒"
                        print(f"   ⏱️  耗时：{dur_str} | "
                              f"${context.cost_usd:.2f} | "
                              f"{context.input_tokens + context.output_tokens:,} tokens")
                    if context.total_cost_usd > 0:
                        progress = f"{len(context.completed_steps)}/{context.total_steps}"
                        print(f"   📊 任务进度：{progress} | 累计 ${context.total_cost_usd:.2f}")
                if file and file.exists():
                    if preview_link:
                        print(f"   📎 在线预览：{preview_link}")
                    print(f"   📁 本地文件：{file}")

                prompt_parts, mapping = self._build_confirm_prompt(
                    allow_discussion=allow_discussion,
                    context=context,
                )
                prompt_text = "  ".join(prompt_parts)
                if web_request_id:
                    action, feedback_content = await self._wait_for_terminal_or_web_reply(
                        prompt_text,
                        mapping,
                        web_request_id=web_request_id,
                        timeout=7200,
                    )
                else:
                    choice = input("   " + prompt_text + ": ").strip().lower()
                    action = mapping.get(choice, "confirm")
                    feedback_content = ""

                if action == "feedback" and feedback_content:
                    self._pending_feedback = feedback_content
                return action
            finally:
                self.resume_progress()
        else:
            # wecom 模式：Web 确认页 + 企微文本回复双通道
            # 企微推送已在上方统一完成，此处只做响应等待
            bs = context.button_set if context else "generic_confirm"
            action, feedback_content = await self._wecom_wait_web_reply(
                web_request_id, timeout=7200, web_url=web_url, title=message,
                button_set=bs,
            )

            # 发送确认反馈（优化3）
            await self._send_reply_feedback(action, feedback_content)

            # 存储修改内容供 get_user_feedback() 直接返回
            if action == "feedback" and feedback_content:
                self._pending_feedback = feedback_content

            return action

    async def get_user_feedback(self) -> str:
        """获取用户的文本反馈"""
        # 优先返回 Web 页面内联提交的修改内容（无需二次交互）
        if self._pending_feedback:
            feedback = self._pending_feedback
            self._pending_feedback = None
            return feedback

        if self.mode == "terminal":
            print("   请输入你的修改意见（输入完按回车）:")
            return input("   > ").strip()
        else:
            await self._wecom_send("请输入你的修改意见：")
            reply = await self._wecom_wait_reply(timeout=7200)
            return reply.strip()

    # ─────────────── 输出方法 ───────────────

    def print_task_created(self, task):
        print(f"\n任务已创建：{task.description}")
        print(f"   任务ID：{task.id}")
        print(f"   任务目录：{task.dir}")

    def print_success(self, msg: str):
        if self._progress_display and self._progress_display._live:
            self._progress_display.live_print(f"[OK] {msg}")
        else:
            print(f"\n[OK] {msg}")

    def print_error(self, msg: str):
        if self._progress_display and self._progress_display._live:
            self._progress_display.live_print(f"[ERR] {msg}")
        else:
            print(f"\n[ERR] {msg}")

    def print_warning(self, msg: str):
        if self._progress_display and self._progress_display._live:
            self._progress_display.live_print(f"[WARN] {msg}")
        else:
            print(f"\n[WARN] {msg}")

    def print_info(self, msg: str):
        if self._progress_display and self._progress_display._live:
            self._progress_display.live_print(f"[INFO] {msg}")
        else:
            print(f"\n[INFO] {msg}")

    def print_result(self, msg: str):
        if self._progress_display and self._progress_display._live:
            self._progress_display.live_print(msg)
        else:
            print(msg)

    # ─────────────── 企微对接 ───────────────

    async def _wecom_confirm(self, message: str) -> bool:
        """简单确认（布尔值）- 精确匹配指令"""
        if not self._wecom_enabled:
            return False
        await self._wecom_send(f"{message}\n\n回复 1=确认 / 2=取消")
        while True:
            reply = await self._wecom_wait_reply(timeout=7200)
            text = reply.strip()
            if text in ("1", "确认", "y", "yes"):
                await self._send_reply_feedback("confirm")
                return True
            if text in ("2", "取消", "n", "no"):
                await self._send_reply_feedback("cancel")
                return False
            # 未识别，发送帮助提示
            await self._wecom_send(
                f"未识别指令「{text[:20]}」\n请回复：1=确认 / 2=取消",
                append_tips=False,
            )

    async def _wecom_send(self, message: str, append_tips: bool = True):
        """通过企微自建应用 API 发送文本消息"""
        if not self._wecom_enabled:
            return
        if append_tips:
            message = message.rstrip() + INTERRUPT_TIPS
        try:
            import aiohttp
            token = await self._get_access_token()
            url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
            wecom_config = self.config.get("wecom", {})
            payload = {
                "touser": "@all",
                "msgtype": "text",
                "agentid": wecom_config.get("agent_id", 1000002),
                "text": {"content": message[:2048]},
            }
            async with aiohttp.ClientSession() as session:
                resp = await session.post(url, json=payload)
                result = await resp.json()
                if result.get("errcode", 0) != 0:
                    logger.error(f"企微发送失败: {result}")
        except Exception as e:
            print(f"[企微发送失败: {e}] {message[:100]}")

    async def _get_access_token(self) -> str:
        """获取企微 access_token（带缓存，有效期 7200 秒）"""
        if self._access_token and time.time() < self._token_expires:
            return self._access_token

        import aiohttp
        wecom_config = self.config.get("wecom", {})
        corp_id = wecom_config.get("corp_id", "")
        corp_secret = wecom_config.get("corp_secret", "")
        url = (
            f"https://qyapi.weixin.qq.com/cgi-bin/gettoken"
            f"?corpid={corp_id}&corpsecret={corp_secret}"
        )
        async with aiohttp.ClientSession() as session:
            resp = await session.get(url)
            data = await resp.json()

        if data.get("errcode", 0) != 0:
            raise RuntimeError(f"获取 access_token 失败: {data.get('errmsg', 'unknown')}")

        self._access_token = data["access_token"]
        # 提前 5 分钟刷新，避免边界过期
        self._token_expires = time.time() + data.get("expires_in", 7200) - 300
        return self._access_token

    async def _wecom_wait_reply(self, timeout: int = 7200) -> str:
        """通过 Redis 轮询等待企微回复"""
        try:
            import redis.asyncio as aioredis
            r = aioredis.from_url("redis://localhost:6379")
            task_id = self._current_task_id or "default"
            key = f"opus:reply:{task_id}"
            # 告知 callback server 当前等待回复的 task_id
            await r.set("opus:active_task", task_id, ex=timeout)
            deadline = time.time() + timeout
            while time.time() < deadline:
                reply = await r.get(key)
                if reply:
                    await r.delete(key)
                    await r.close()
                    return reply.decode()
                await asyncio.sleep(3)
            await r.close()
            raise TimeoutError("企微回复超时")
        except ImportError:
            logger.warning("redis 未安装，降级到终端输入")
            return input("请输入回复: ").strip()

    # ─────────────── WeComNotifier 桥接（优化1）───────────────

    def _get_wecom_notifier(self):
        """懒加载 WeComNotifier"""
        if self._notifier is None:
            from lib.wecom_notifier import WeComNotifier
            wecom_cfg = self.config.get("wecom", {})
            self._notifier = WeComNotifier(
                corpid=wecom_cfg.get("corp_id", ""),
                agentid=str(wecom_cfg.get("agent_id", "")),
                secret=wecom_cfg.get("corp_secret", ""),
                userid=wecom_cfg.get("userid", "@all"),
            )
        return self._notifier

    async def _wecom_send_dual(self, title: str, summary: str,
                                web_url: str, preview_link: str = "",
                                allow_discussion: bool = False,
                                context: "ConfirmContext" = None):
        """卡片消息 + 文本消息双推送（优化1）"""
        if not self._wecom_enabled:
            return
        notifier = self._get_wecom_notifier()

        # 1. 发送卡片消息（点击进入确认页面）
        card_parts = []
        if context:
            if context.task_name:
                card_parts.append(f"📋 {context.task_name[:30]}")
            role_step = []
            if context.role_display:
                role_step.append(context.role_display)
            if context.step_display:
                role_step.append(context.step_display)
            if role_step:
                card_parts.append(" | ".join(role_step))
        if summary:
            remaining = 200 - sum(len(p) for p in card_parts) - len(card_parts) * 2
            card_parts.append(summary[:max(50, remaining)])
        card_desc = "\n".join(card_parts) if card_parts else summary[:200]

        try:
            await notifier.send_textcard(
                title=title[:128],
                description=card_desc[:512],
                url=web_url,
                btntxt="查看并确认",
            )
        except Exception as e:
            logger.warning(f"卡片消息发送失败: {e}")

        # 2. 发送文本消息（含上下文信息 + 指令说明 + 中断提示）
        ctx_lines = []
        if context:
            if context.task_name:
                ctx_lines.append(f"📋 任务：{context.task_name[:40]}")

            role_info = []
            if context.role_display:
                role_info.append(f"🤖 {context.role_display}")
            if context.model:
                model_short = {"claude-opus-4-7": "opus",
                               "claude-opus-4-6": "opus",
                               "claude-sonnet-4-7": "sonnet",
                               "claude-sonnet-4-6": "sonnet",
                               "claude-sonnet-4-5-20250929": "sonnet",
                               "claude-haiku-4-7": "haiku",
                               "claude-haiku-4-5": "haiku",
                               "claude-haiku-4-5-20251001": "haiku"}.get(context.model, context.model)
                role_info.append(model_short)
            if role_info:
                ctx_lines.append(" | ".join(role_info))

            cost_parts = []
            if context.duration > 0:
                m, s = divmod(int(context.duration), 60)
                cost_parts.append(f"⏱️ {m}分{s}秒" if m else f"⏱️ {s}秒")
            if context.cost_usd > 0:
                cost_parts.append(f"💰 ${context.cost_usd:.2f}")
            if context.input_tokens + context.output_tokens > 0:
                total_t = context.input_tokens + context.output_tokens
                cost_parts.append(f"📊 {total_t:,} tokens")
            if cost_parts:
                ctx_lines.append(" | ".join(cost_parts))

            if context.total_cost_usd > 0:
                progress = f"{len(context.completed_steps)}/{context.total_steps}"
                ctx_lines.append(f"📈 任务进度 {progress} | 累计 ${context.total_cost_usd:.2f}")

        ctx_block = "\n".join(ctx_lines)

        doc_line = f"\n📄 查看完整文档：{preview_link}" if preview_link else ""
        file_line = ""
        if context and context.file_path:
            file_line = f"\n📁 本地路径：{context.file_path}"

        bs = context.button_set if context else "generic_confirm"
        bs_config = BUTTON_SET_CONFIGS.get(bs, BUTTON_SET_CONFIGS["generic_confirm"])
        quick_reply = bs_config["wecom_hint"]

        if ctx_block:
            text = (
                f"{title}\n\n"
                f"{ctx_block}\n\n"
                f"{summary}"
                f"{doc_line}"
                f"{file_line}\n\n"
                f"👉 点击确认页面：{web_url}\n\n"
                f"{quick_reply}"
                f"{INTERRUPT_TIPS}"
            )
        else:
            text = (
                f"{title}\n\n"
                f"{summary}"
                f"{doc_line}\n\n"
                f"👉 点击确认页面：{web_url}\n\n"
                f"{quick_reply}"
                f"{INTERRUPT_TIPS}"
            )
        try:
            await notifier.send_text(text[:2048])
        except Exception as e:
            logger.warning(f"文本消息发送失败: {e}")
            await self._wecom_send(text[:2048], append_tips=False)

    async def _wecom_push_card(self, title: str, description: str,
                               task_id: str, btntxt: str = "查看详情"):
        """通用企微卡片推送（非确认场景的单向通知）"""
        if not self._wecom_enabled:
            return
        base_url = (self.config.get("wecom", {})
                    .get("callback_server", {})
                    .get("url", "https://opus.bingbing.asia/wecom/callback"))
        preview_base = base_url.rsplit("/wecom/callback", 1)[0]
        url = absolute_url(preview_base, task_detail_path(task_id))

        try:
            notifier = self._get_wecom_notifier()
            await notifier.send_textcard(
                title=title[:128],
                description=description[:512],
                url=url,
                btntxt=btntxt,
            )
        except Exception as e:
            logger.warning(f"卡片推送失败: {e}")

    # ─────────────── Web 确认页面（优化2）───────────────

    async def _link_web_to_bridge(self, web_request_id: str,
                                    bridge_request_id: str):
        """将 bridge_request_id 补写到已创建的 Web 确认页 Redis 数据中"""
        import redis.asyncio as aioredis
        preview_redis_url = self.config.get(
            "preview_redis_url", "redis://localhost:6380")
        r = aioredis.from_url(
            preview_redis_url,
            socket_connect_timeout=0.3,
            socket_timeout=0.3,
        )
        try:
            raw = await asyncio.wait_for(
                r.get(f"pending_request:{web_request_id}"), timeout=0.5)
            if raw:
                data = json.loads(raw)
                data["bridge_request_id"] = bridge_request_id
                ttl = await asyncio.wait_for(
                    r.ttl(f"pending_request:{web_request_id}"), timeout=0.5)
                await asyncio.wait_for(
                    r.set(
                        f"pending_request:{web_request_id}",
                        json.dumps(data, ensure_ascii=False),
                        ex=max(ttl, 3600),
                    ),
                    timeout=0.5,
                )
        except Exception as e:
            logger.warning(f"关联 Web→Bridge 失败: {e}")
        finally:
            await r.close()

    async def _create_web_confirm(self, title: str, summary: str,
                                   preview_link: str = "",
                                   allow_discussion: bool = False,
                                   context: "ConfirmContext" = None,
                                   bridge_request_id: str = "") -> tuple:
        """创建 Web 确认页面，返回 (request_id, web_url)"""
        import redis.asyncio as aioredis

        task_id = self._current_task_id or "default"
        request_id = f"vizo-{task_id}-{uuid.uuid4().hex[:8]}"

        data = {
            "type": "v6_confirm",
            "title": title,
            "summary": summary,
            "preview_link": preview_link,
            "created_at": time.time(),
            "allow_discussion": allow_discussion,
            "button_set": context.button_set if context else "generic_confirm",
        }
        if bridge_request_id:
            data["bridge_request_id"] = bridge_request_id
        if context:
            data["context"] = {
                "task_name": context.task_name,
                "task_id": context.task_id,
                "role_display": context.role_display,
                "step_display": context.step_display,
                "model": context.model,
                "cost_usd": context.cost_usd,
                "input_tokens": context.input_tokens,
                "output_tokens": context.output_tokens,
                "duration": context.duration,
                "total_cost_usd": context.total_cost_usd,
                "total_tokens": context.total_tokens,
                "completed_steps": context.completed_steps,
                "total_steps": context.total_steps,
                "file_path": context.file_path,
                "button_set": context.button_set,
            }

        preview_redis_url = self.config.get("preview_redis_url", "redis://localhost:6380")
        r = aioredis.from_url(
            preview_redis_url,
            socket_connect_timeout=0.3,
            socket_timeout=0.3,
        )
        try:
            await asyncio.wait_for(
                r.set(
                    f"pending_request:{request_id}",
                    json.dumps(data, ensure_ascii=False),
                    ex=86400,
                ),
                timeout=0.5,
            )
        finally:
            await r.close()

        # F6.1: 写入 meta 文件（request_id → task_id 映射），用于过期确认链接跳转
        try:
            meta_dir = CONFIRMS_DIR / "meta"
            meta_dir.mkdir(parents=True, exist_ok=True)
            ctx = data.get("context", {})
            ctx_task_id = ctx.get("task_id", "") if isinstance(ctx, dict) else ""
            if not ctx_task_id:
                ctx_task_id = self._current_task_id or ""
            if ctx_task_id:
                meta_file = meta_dir / f"{request_id}.json"
                meta_file.write_text(
                    json.dumps({"task_id": ctx_task_id}, ensure_ascii=False),
                    encoding="utf-8"
                )
        except Exception:
            pass  # 非关键路径

        web_base = get_base_url()
        web_url = absolute_url(web_base, confirm_path(request_id))

        return request_id, web_url

    async def _wecom_wait_web_reply(self, request_id: str, timeout: int = 7200,
                                     web_url: str = "", title: str = "",
                                     button_set: str = "generic_confirm") -> tuple:
        """双通道等待回复：Web 页面(Redis 6380) + 企微文本(Redis 6379)

        返回: (action, feedback_content)
              action = 'confirm' | 'cancel' | 'feedback'
              feedback_content = 修改内容或 None
        """
        import redis.asyncio as aioredis

        preview_redis_url = self.config.get("preview_redis_url", "redis://localhost:6380")
        r_web = aioredis.from_url(preview_redis_url)
        r_wecom = aioredis.from_url("redis://localhost:6379")

        task_id = self._current_task_id or "default"
        wecom_reply_key = f"opus:reply:{task_id}"
        web_response_key = f"response:{request_id}"

        # 告知 callback server 当前等待回复的 task_id
        await r_wecom.set("opus:active_task", task_id, ex=timeout)

        deadline = time.time() + timeout
        # 提醒调度（优化4a）
        remind_schedule = [180, 600, 1800]  # 3分钟, 10分钟, 30分钟
        reminded_count = 0
        start_time = time.time()

        try:
            while time.time() < deadline:
                # 检查 Web 页面回复（主路径，零歧义）
                web_reply = await r_web.get(web_response_key)
                if web_reply:
                    await r_web.delete(web_response_key)
                    return self._parse_web_reply(web_reply.decode())

                # 检查企微文本回复（降级路径，精确匹配）
                wecom_reply = await r_wecom.get(wecom_reply_key)
                if wecom_reply:
                    await r_wecom.delete(wecom_reply_key)
                    reply_text = wecom_reply.decode().strip()
                    action, content = self._parse_wecom_reply(reply_text, button_set)
                    if action == "unknown":
                        # 未识别，根据 button_set 生成帮助提示
                        bs_config = BUTTON_SET_CONFIGS.get(button_set, BUTTON_SET_CONFIGS["generic_confirm"])
                        hint_lines = []
                        for i, btn in enumerate(bs_config["buttons"], 1):
                            hint_lines.append(f"  {i} = {btn['label']}")
                        help_msg = (
                            f"未识别指令「{reply_text[:20]}」\n\n"
                            f"请回复：\n"
                            + "\n".join(hint_lines)
                            + "\n\n"
                            f"或点击确认页面操作：{web_url}"
                        )
                        await self._wecom_send(help_msg, append_tips=False)
                        continue
                    return action, content

                # 提醒机制（优化4a）
                elapsed = time.time() - start_time
                if (reminded_count < len(remind_schedule)
                        and elapsed >= remind_schedule[reminded_count]):
                    reminded_count += 1
                    remind_msg = (
                        f"⏰ 提醒（第{reminded_count}次）: {title[:40]}\n\n"
                        f"等待您的确认已 {int(elapsed / 60)} 分钟\n"
                        f"👉 {web_url}"
                    )
                    notifier = self._get_wecom_notifier()
                    try:
                        await notifier.send_text(remind_msg + INTERRUPT_TIPS)
                    except Exception:
                        await self._wecom_send(remind_msg)

                await asyncio.sleep(3)

            raise TimeoutError("等待回复超时")
        finally:
            await r_web.close()
            await r_wecom.close()

    @staticmethod
    def _parse_web_reply(reply_str: str) -> tuple:
        """解析 Web 页面回复（枚举值，零歧义）"""
        if reply_str.startswith("feedback:"):
            return "feedback", reply_str[len("feedback:"):]
        if reply_str == "cancel":
            return "cancel", None
        if reply_str in ("N",):
            return "cancel", None
        if reply_str in ("discussion",):
            return "discussion", None
        if reply_str in ("interaction_design",):
            return "interaction_design", None
        if reply_str in ("terminate",):
            return "terminate", None
        return "confirm", None

    @staticmethod
    def _parse_wecom_reply(reply_text: str, button_set: str = "generic_confirm") -> tuple:
        """精确解析企微文本回复，根据 button_set 动态映射数字

        返回: (action, content)
              action = 'confirm' | 'cancel' | 'feedback' | 'discussion' | 'terminate' | 'unknown'
        """
        text = reply_text.strip()

        # 构建动态数字映射：数字 N → 第 N 个按钮的 action
        action_to_result = {
            "y": "confirm", "n": "cancel", "f": "feedback",
            "d": "discussion", "i": "interaction_design",
            "terminate": "terminate", "resume": "resume",
        }
        bs_config = BUTTON_SET_CONFIGS.get(button_set, BUTTON_SET_CONFIGS["generic_confirm"])
        digit_map = {}
        for i, btn in enumerate(bs_config["buttons"], 1):
            digit_map[str(i)] = action_to_result.get(btn["action"], btn["action"])

        # 数字精确匹配
        if text in digit_map:
            return digit_map[text], None

        # 中文关键字匹配
        if text == "确认":
            return "confirm", None
        if text == "取消":
            return "cancel", None
        if text == "讨论":
            return "discussion", None

        # 前缀匹配：修改指令（查找 feedback action 对应的数字键）
        feedback_digits = [k for k, v in digit_map.items() if v == "feedback"]
        prefixes = ["修改 ", "修改:", "修改："]
        for fd in feedback_digits:
            prefixes.extend([f"{fd} ", f"{fd}:", f"{fd}："])
        for prefix in prefixes:
            if text.startswith(prefix):
                content = text[len(prefix):].strip()
                if content:
                    return "feedback", content
                return "unknown", None  # 前缀后无内容

        return "unknown", None

    async def _send_reply_feedback(self, action: str, content: str = None):
        """收到回复后立即发送确认反馈（优化3）"""
        if action == "confirm":
            msg = "✅ 已收到确认，正在继续执行..."
        elif action == "cancel":
            msg = "🛑 已收到取消指令，任务将终止"
        elif action == "feedback":
            preview = (content[:50] + "...") if content and len(content) > 50 else (content or "")
            msg = f"✏️ 已收到修改意见: {preview}\n正在根据意见修改..."
        elif action == "discussion":
            msg = "💬 已发起方向讨论，正在启动技术评估..."
        else:
            return

        notifier = self._get_wecom_notifier()
        try:
            await notifier.send_text(msg)
        except Exception:
            await self._wecom_send(msg, append_tips=False)

    # ─────────────── 进度显示（优化4b）───────────────

    def create_progress_display(self, task_id: str = "", task_desc: str = ""):
        """创建终端进度显示实例"""
        display = AgentProgressDisplay(task_id=task_id, task_desc=task_desc)
        self._progress_display = display
        return display

    # ─────────────── 中断检查（优化5a）───────────────

    async def check_interrupt(self) -> str | None:
        """检查是否有中断信号

        返回: 'cancel' | 'modify' | None
        """
        try:
            import redis.asyncio as aioredis
            r = aioredis.from_url("redis://localhost:6379")
            task_id = self._current_task_id or "default"
            key = f"opus:interrupt:{task_id}"
            value = await r.get(key)
            if value:
                await r.delete(key)
                await r.close()
                return value.decode()
            await r.close()
        except Exception:
            pass
        return None

    # ─────────────── 辅助方法 ───────────────

    async def check_wecom_services(self) -> dict:
        """检查企微相关服务可用性，返回 {'callback': bool, 'redis': bool}"""
        if not self._wecom_enabled:
            return {"callback": False, "redis": False}
        result = {"callback": False, "redis": False}

        # 检查 callback server
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                resp = await session.get("http://localhost:9390/health", timeout=aiohttp.ClientTimeout(total=3))
                result["callback"] = resp.status == 200
        except Exception:
            pass

        # 检查 Redis
        try:
            import redis.asyncio as aioredis
            r = aioredis.from_url("redis://localhost:6379")
            await r.ping()
            result["redis"] = True
            await r.close()
        except Exception:
            pass

        return result

    def _summarize_file(self, file: Path, max_chars: int = 500) -> str:
        """从 Markdown 文件提取结构化摘要，适合企微文本消息展示"""
        if not file or not file.exists():
            return ""
        content = file.read_text(encoding="utf-8")
        if not content.strip():
            return ""

        lines = content.split('\n')
        summary_lines = []
        chars_used = 0
        prev_was_heading = False
        found_first_heading = False

        for line in lines:
            stripped = line.strip()
            if not stripped:
                prev_was_heading = False
                continue

            is_heading = stripped.startswith('#')
            is_bullet = (stripped.startswith('- ') or stripped.startswith('* ')
                         or stripped.startswith('• '))

            # 跳过第一个标题之前的内容（Agent 前言）
            if not found_first_heading:
                if is_heading:
                    found_first_heading = True
                else:
                    continue

            # 包含：标题、标题后首行内容、要点列表
            if is_heading or is_bullet or prev_was_heading:
                # 清理 markdown 标记为纯文本
                display = stripped.lstrip('#').strip() if is_heading else stripped
                if is_heading:
                    display = f"【{display}】"

                if chars_used + len(display) + 1 > max_chars:
                    summary_lines.append("...")
                    break
                summary_lines.append(display)
                chars_used += len(display) + 1

            prev_was_heading = is_heading

        if summary_lines:
            return '\n'.join(summary_lines)
        # 无结构化内容时，退回到简单截断
        return content[:max_chars].rstrip() + "\n..."

    async def _upload_preview(self, file: Path) -> str:
        """将文件上传到 preview Redis，返回可点击链接"""
        if not file or not file.exists():
            return ""
        preview_id = ""
        html = ""
        try:
            import redis.asyncio as aioredis
            content = file.read_text(encoding="utf-8")
            html = self._md_to_html(content, title=file.name)

            preview_id = hashlib.md5(
                f"{file.name}:{time.time()}".encode()
            ).hexdigest()[:8]
            payload = {
                "html": html,
                "filename": file.name,
                "title": file.name,
            }

            backup_written = False
            try:
                backup_dir = PREVIEWS_DIR
                backup_dir.mkdir(parents=True, exist_ok=True)
                backup_file = backup_dir / f"{preview_id}.json"
                backup_file.write_text(
                    json.dumps(payload, ensure_ascii=False),
                    encoding="utf-8"
                )
                backup_written = True
            except OSError as e:
                logger.warning(f"预览磁盘备份失败: {e}")

            preview_redis_url = self.config.get("preview_redis_url", "redis://localhost:6380")
            r = aioredis.from_url(
                preview_redis_url,
                socket_connect_timeout=0.3,
                socket_timeout=0.3,
            )
            try:
                await asyncio.wait_for(
                    r.set(
                        f"preview:{preview_id}",
                        json.dumps(payload, ensure_ascii=False),
                        ex=86400 * 30,  # 30 天过期
                    ),
                    timeout=0.5,
                )
            except Exception as e:
                logger.warning(f"预览 Redis 写入失败，使用磁盘备份降级: {e}")
                if not backup_written:
                    return ""
            finally:
                await r.close()

            base_url = (self.config.get("wecom", {})
                        .get("callback_server", {})
                        .get("url", "https://opus.bingbing.asia/wecom/callback"))
            # 从 callback URL 推导 preview URL
            preview_base = base_url.rsplit("/wecom/callback", 1)[0]
            return absolute_url(preview_base, preview_path(preview_id))
        except Exception as e:
            logger.warning(f"上传预览失败: {e}")
            return ""

    @staticmethod
    def _md_to_html(md_content: str, title: str = "Preview") -> str:
        """Markdown 转 HTML（使用 markdown 库，支持表格、代码块等）"""
        import markdown

        body = markdown.markdown(
            md_content,
            extensions=["tables", "fenced_code", "nl2br", "sane_lists"],
            output_format="html",
        )

        return f"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ --bg: #ffffff; --fg: #1a1a2e; --fg2: #374151; --border: #e5e7eb; --accent: #2563eb; --code-bg: #f8f9fa; --block-bg: #fffbeb; }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  max-width: 860px; margin: 0 auto; padding: 24px 20px 60px; line-height: 1.75; color: var(--fg2);
  font-size: 15px; background: var(--bg); }}

/* 标题 */
h1 {{ font-size: 1.7em; color: var(--fg); margin: 32px 0 16px; padding-bottom: 8px; border-bottom: 2px solid var(--accent); }}
h2 {{ font-size: 1.35em; color: var(--fg); margin: 28px 0 12px; padding-bottom: 6px; border-bottom: 1px solid var(--border); }}
h3 {{ font-size: 1.15em; color: var(--fg); margin: 24px 0 10px; }}
h4 {{ font-size: 1.05em; color: var(--fg2); margin: 20px 0 8px; }}
h5,h6 {{ font-size: 1em; color: var(--fg2); margin: 16px 0 6px; }}

/* 段落与文本 */
p {{ margin: 8px 0; }}
strong {{ color: #b91c1c; font-weight: 600; }}
em {{ color: #6b7280; }}
a {{ color: var(--accent); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
hr {{ border: none; border-top: 1px solid var(--border); margin: 24px 0; }}
blockquote {{ border-left: 3px solid var(--accent); background: #f0f7ff; padding: 10px 16px; margin: 12px 0; border-radius: 0 6px 6px 0; color: #1e40af; }}

/* 列表 */
ul, ol {{ padding-left: 24px; margin: 8px 0; }}
li {{ margin: 4px 0; }}
li > ul, li > ol {{ margin: 2px 0; }}

/* 代码 */
code {{ background: var(--code-bg); border: 1px solid var(--border); padding: 1px 5px; border-radius: 4px; font-size: 0.88em; font-family: "SF Mono", "Fira Code", Consolas, monospace; color: #be185d; }}
pre {{ background: #1e293b; color: #e2e8f0; padding: 16px; border-radius: 8px; overflow-x: auto; margin: 12px 0; line-height: 1.5; }}
pre code {{ background: none; border: none; padding: 0; color: inherit; font-size: 0.85em; }}

/* 表格 */
table {{ width: 100%; border-collapse: collapse; margin: 14px 0; font-size: 0.92em; }}
thead th {{ background: #f1f5f9; color: var(--fg); font-weight: 600; text-align: left;
  padding: 10px 12px; border: 1px solid var(--border); white-space: nowrap; }}
tbody td {{ padding: 8px 12px; border: 1px solid var(--border); vertical-align: top; }}
tbody tr:nth-child(even) {{ background: #f8fafc; }}
tbody tr:hover {{ background: #eef2ff; }}

/* 移动端适配 */
@media (max-width: 600px) {{
  body {{ padding: 14px 12px 40px; font-size: 14px; }}
  table {{ font-size: 0.82em; display: block; overflow-x: auto; -webkit-overflow-scrolling: touch; }}
  pre {{ font-size: 0.8em; padding: 12px; }}
}}
</style>
</head><body>{body}</body></html>"""


# ─────────────── 进度显示类（优化4b）───────────────


class _LiveRenderable:
    """Rich __rich__() 协议包装器，让 Live 每次刷新都重新渲染表格（实现实时计时器）"""

    def __init__(self, display):
        self._display = display

    def __rich__(self):
        table = self._display._render_table()
        if self._display._split_manager:
            import shutil
            from rich.text import Text
            from rich.console import Group
            term_width = shutil.get_terminal_size((120, 24)).columns
            if term_width < 80:
                return Group(table, Text("⚠ 终端太窄，分屏已禁用", style="yellow"))
            # 估算进度表高度：表头(3) + 每行(1) + 汇总(1) + 边距(1)
            table_height = 3 + len(self._display._entries) + 2
            split_view = self._display._split_manager.build_renderable(table_height)
            return Group(table, split_view)
        return table

class AgentProgressDisplay:
    """终端 Agent 执行进度可视化（rich 表格）"""

    def __init__(self, task_id: str = "", task_desc: str = ""):
        self._entries = []  # [{role, model, status, start_time, duration, cost_usd, output, output_file}]
        self._total_cost = 0.0
        self._live = None
        self._use_rich = False
        self._task_id = task_id
        self._task_desc = task_desc
        self._split_manager = None  # SplitScreenManager，分屏模式时设置

    def start(self):
        """启动 Live 显示"""
        try:
            from rich.live import Live
            from rich.console import Console
            console = Console()
            self._live = Live(
                _LiveRenderable(self),
                console=console,
                refresh_per_second=1,
            )
            self._live.start()
            self._use_rich = True
        except ImportError:
            logger.info("rich 未安装，使用简单文本进度")

    def stop(self):
        """停止 Live 显示"""
        if self._live:
            try:
                self._live.stop()
            except Exception:
                pass
            self._live = None

    def agent_started(self, role: str, model: str, estimated_tokens: int = 0,
                      output_file: str = "", fallback: str = "", attempt: int = 0):
        """Agent 开始执行"""
        self._entries.append({
            "role": role,
            "model": model,
            "fallback": fallback,
            "attempt": max(0, int(attempt or 0)),
            "status": "running",
            "start_time": time.time(),
            "duration": 0,
            "cost_usd": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "output": "",
            "output_file": output_file,
            "preview_url": "",
        })
        self._update()
        if not self._use_rich:
            # 首个 Agent 启动时打印任务信息
            if len(self._entries) == 1 and self._task_desc:
                short_id = self._task_id[-4:] if self._task_id else ""
                desc_line = self._task_desc.split('\n')[0].strip()
                if len(desc_line) > 40:
                    desc_line = desc_line[:40] + "…"
                print(f"  📋 任务 [{short_id}] {desc_line}")
            fallback_info = f"→{fallback}" if fallback else ""
            attempt_info = f" 重试{attempt}" if attempt else ""
            print(f"  🔄 [{role}{attempt_info}] 启动中... model={model}{fallback_info}")
    def agent_completed(self, role: str, duration: float, cost_usd: float,
                        input_tokens: int = 0, output_tokens: int = 0,
                        preview_url: str = ""):
        """Agent 执行完成"""
        for e in self._entries:
            if e["role"] == role and e["status"] == "running":
                e["status"] = "completed"
                e["duration"] = duration
                e["cost_usd"] = cost_usd
                e["input_tokens"] = input_tokens
                e["output_tokens"] = output_tokens
                e["output"] = f"in={input_tokens} out={output_tokens}"
                e["preview_url"] = preview_url
                self._total_cost += cost_usd
                break
        self._update()
        if not self._use_rich:
            print(f"  ✅ [{role}] 完成 | {duration:.0f}s | ${cost_usd:.2f} | 累计=${self._total_cost:.2f}")

    def agent_error(self, role: str, error_msg: str):
        """Agent 执行出错"""
        for e in self._entries:
            if e["role"] == role and e["status"] == "running":
                e["status"] = "error"
                e["duration"] = time.time() - e["start_time"]
                e["output"] = error_msg[:50]
                break
        self._update()
        if not self._use_rich:
            print(f"  ❌ [{role}] 失败: {error_msg[:60]}")

    def update_action(self, role: str, action: str):
        """更新指定角色的当前动作（由流式回调触发）"""
        for e in self._entries:
            if e["role"] == role and e["status"] == "running":
                e["output"] = action[:60]
                break
        self._update()

    def live_print(self, msg: str):
        """通过 Live console 输出文本，避免破坏 Live 布局"""
        if self._live:
            try:
                self._live.console.print(msg, highlight=False)
            except Exception:
                print(msg)
        else:
            print(msg)

    def get_console(self):
        """返回 Live 关联的 Console，供 StreamRenderer 使用"""
        if self._live:
            return self._live.console
        return None

    def activate_split(self, split_manager):
        """激活分屏模式"""
        self._split_manager = split_manager

    def deactivate_split(self):
        """退出分屏模式"""
        self._split_manager = None

    def _update(self):
        """刷新 Live 显示"""
        if self._live:
            try:
                self._live.refresh()
            except Exception:
                pass

    def _render_table(self):
        """渲染 rich Table（自适应终端宽度）"""
        from rich.table import Table
        from rich.markup import escape
        import shutil

        term_width = shutil.get_terminal_size((120, 24)).columns

        # 构建标题
        if self._task_desc:
            short_id = self._task_id[-4:] if self._task_id else ""
            # 单行化 + 截断
            desc_line = self._task_desc.split('\n')[0].strip()
            max_desc_len = 40
            if len(desc_line) > max_desc_len:
                desc_line = desc_line[:max_desc_len] + "…"
            # 转义 Rich 标记
            desc_line = escape(desc_line)
            title = f"🤖 任务进度 ｜ {short_id} ｜ {desc_line}"
        else:
            title = "🤖 Agent 执行进度"

        table = Table(title=title, show_lines=False, title_style="bold cyan",
                      border_style="dim", pad_edge=False, expand=True, width=min(term_width, 120))
        table.add_column("#", style="dim", width=3, justify="right")
        table.add_column("角色", style="cyan", no_wrap=True)
        table.add_column("模型", style="dim", no_wrap=True, max_width=12)
        table.add_column("状态", no_wrap=True)
        table.add_column("启动", no_wrap=True)
        table.add_column("耗时", justify="right", no_wrap=True)
        table.add_column("费用", justify="right", no_wrap=True)
        table.add_column("产出文件", style="dim", no_wrap=True, max_width=25, overflow="ellipsis")
        table.add_column("当前动作", style="dim", ratio=1, overflow="ellipsis", no_wrap=True)

        status_map = {
            "running": "[bold yellow]⏳ 运行中[/bold yellow]",
            "completed": "[green]✅ 完成[/green]",
            "error": "[red]❌ 失败[/red]",
        }

        role_display = {
            "requirement_analyst": "需求分析",
            "product_manager": "产品经理",
            "project_manager": "项目经理",
            "architect": "架构师",
            "backend_developer": "后端开发",
            "frontend_developer": "前端开发",
            "embedded_engineer": "嵌入式开发",
            "integration_engineer": "联调工程师",
            "qa_engineer": "QA 测试",
            "fix_engineer": "修复工程师",
            "devops_engineer": "运维部署",
            "knowledge_engineer": "知识工程师",
            "knowledge_admin": "知识管理",
            "code_explorer": "代码探索",
            "assistant": "助手",
            "merge_resolver": "冲突解决",
            "handoff_extractor": "交接提取",
            "manual_updater": "手册更新",
            "technical_assessor": "技术评估",
        }

        for i, e in enumerate(self._entries, 1):
            status = status_map.get(e["status"], e["status"])

            # 启动时间 HH:MM:SS
            start_ts = e.get("start_time", 0)
            if start_ts > 0:
                start_str = time.strftime("%H:%M:%S", time.localtime(start_ts))
            else:
                start_str = "-"

            # 耗时（运行中实时计算）
            if e["status"] == "running":
                elapsed = time.time() - e["start_time"]
            else:
                elapsed = e["duration"]

            if elapsed >= 60:
                elapsed_str = f"{int(elapsed // 60)}m{int(elapsed % 60):02d}s"
            else:
                elapsed_str = f"{int(elapsed)}s"

            cost = e["cost_usd"]
            cost_str = f"${cost:.2f}" if cost > 0 else "-"

            # 产出文件：区分无产出 / 待产出 / 已产出
            output_file = e.get("output_file", "")
            preview_url = e.get("preview_url", "")
            if output_file:
                from pathlib import Path
                fname = Path(output_file).name
                if e["status"] == "running":
                    output_file = f"[dim italic]⏳ {fname}[/dim italic]"
                elif preview_url:
                    output_file = f"[link={preview_url}]{fname}[/link]"
                else:
                    output_file = fname
            else:
                output_file = "[dim]-[/dim]"

            # 当前动作：仅运行中的 Agent 显示
            action_str = e.get("output", "") if e["status"] == "running" else ""

            fallback = e.get("fallback", "")
            model_str = f"{e['model']}→{fallback}" if fallback else e["model"]

            role_label = role_display.get(e["role"], e["role"])
            attempt = int(e.get("attempt") or 0)
            if attempt:
                role_label = f"{role_label} (重试{attempt})"

            table.add_row(
                str(i),
                role_label,
                model_str,
                status,
                start_str,
                elapsed_str,
                cost_str,
                output_file,
                action_str,
            )

        # 底部汇总行
        if self._total_cost > 0:
            table.add_row("", "[bold]合计[/bold]", "", "", "", "", f"[bold green]${self._total_cost:.2f}[/bold green]", "", "")

        return table
