#!/usr/bin/env python3
"""
通知系统 - 微信公众号推送
Opus 智能协作系统 v3.0
"""

import json
import uuid
import asyncio
import aiohttp
from typing import Optional, Dict, Callable
from datetime import datetime
from enum import Enum
from pathlib import Path

from lib.web_paths import confirm_path, input_path

_LIB_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _LIB_DIR.parent


# 兼容导入：支持作为包内模块或独立脚本运行
try:
    from wecom_notifier import WeComNotifier
except ImportError:
    from .wecom_notifier import WeComNotifier

try:
    from cache_manager import get_cache_manager
except ImportError:
    from .cache_manager import get_cache_manager


class NotificationType(Enum):
    """通知类型"""
    AUTH_REQUIRED = "auth_required"       # 需要授权
    CONFIRM_NEEDED = "confirm_needed"     # 需要确认
    INFO_SUPPLEMENT = "info_supplement"   # 需要补充信息
    TASK_PAUSED = "task_paused"          # 任务暂停
    TASK_COMPLETED = "task_completed"    # 任务完成
    PROGRESS_REPORT = "progress_report"  # 进度汇报
    ERROR_CRITICAL = "error_critical"    # 严重错误


class WxPusherNotifier:
    """WxPusher 微信推送"""

    API_BASE = "https://wxpusher.zjiecode.com/api"

    def __init__(self, app_token: str, uid: str):
        self.app_token = app_token
        self.uid = uid
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def send_message(
        self,
        content: str,
        summary: str = "",
        content_type: int = 1,
        url: str = ""
    ) -> Dict:
        """
        发送消息

        Args:
            content: 消息内容
            summary: 摘要（显示在通知栏）
            content_type: 1=文本, 2=HTML, 3=Markdown
            url: 点击跳转链接

        Returns:
            API 响应
        """
        session = await self._get_session()

        data = {
            "appToken": self.app_token,
            "content": content,
            "summary": summary or content[:50],
            "contentType": content_type,
            "uids": [self.uid]
        }
        if url:
            data["url"] = url

        try:
            async with session.post(
                f"{self.API_BASE}/send/message",
                json=data,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                result = await resp.json()
                return result
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def send_notification(
        self,
        notification_type: "NotificationType",
        title: str,
        content: str,
        request_id: str = "",
        options: list = None
    ) -> Dict:
        """
        发送结构化通知

        Args:
            notification_type: 通知类型
            title: 标题
            content: 内容
            request_id: 请求ID（用于等待响应）
            options: 可选项列表（用于选择）

        Returns:
            包含 request_id 的响应
        """
        request_id = request_id or str(uuid.uuid4())[:8]

        # 构建消息
        emoji_map = {
            NotificationType.AUTH_REQUIRED: "🔐",
            NotificationType.CONFIRM_NEEDED: "⚠️",
            NotificationType.INFO_SUPPLEMENT: "📝",
            NotificationType.TASK_PAUSED: "⏸️",
            NotificationType.TASK_COMPLETED: "✅",
            NotificationType.PROGRESS_REPORT: "📊",
            NotificationType.ERROR_CRITICAL: "❌"
        }

        emoji = emoji_map.get(notification_type, "📢")
        message = f"{emoji} **{title}**\n\n{content}"

        # 对确认类通知，尝试生成 Web 确认链接
        confirm_url = None
        if notification_type in [
            NotificationType.AUTH_REQUIRED,
            NotificationType.CONFIRM_NEEDED,
        ]:
            confirm_url = await get_confirm_url(request_id)
            if confirm_url:
                message += f"\n\n---\n🔗 **[点击确认/取消]({confirm_url})**\n"
                message += f"\n请求ID: `{request_id}`"
            else:
                message += f"\n\n---\n请求ID: `{request_id}`\n"
                if options:
                    message += "\n可选回复:\n"
                    for i, opt in enumerate(options, 1):
                        message += f"  {i}. {opt}\n"
                message += f"\n回复方式: `opus3-reply {request_id} Y/N`"
        elif notification_type == NotificationType.INFO_SUPPLEMENT:
            message += f"\n\n---\n请求ID: `{request_id}`\n"
            if options:
                message += "\n可选回复:\n"
                for i, opt in enumerate(options, 1):
                    message += f"  {i}. {opt}\n"
            message += f"\n回复方式: `opus3-reply {request_id} 你的回复`"

        message += f"\n\n_时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_"

        result = await self.send_message(
            content=message,
            summary=f"{emoji} {title}",
            content_type=3,  # Markdown
            url=confirm_url or ""
        )

        result["request_id"] = request_id
        return result


class NotificationManager:
    """通知管理器（支持企业微信和 WxPusher）"""

    def __init__(self, config: Dict):
        self.config = config
        self._wxpusher: Optional[WxPusherNotifier] = None
        self._wecom = None  # WeComNotifier
        self._provider = config.get("notification", {}).get("provider", "wxpusher")
        self._response_callbacks: Dict[str, Callable] = {}

    def _load_wxpusher(self) -> Optional[WxPusherNotifier]:
        """加载 WxPusher 通知器"""
        if self._wxpusher:
            return self._wxpusher

        secrets = self.config.get("secrets", {})
        app_token = secrets.get("wxpusher_app_token", "")
        uid = secrets.get("wxpusher_uid", "")

        if not app_token or not uid or app_token.startswith("YOUR_"):
            return None

        self._wxpusher = WxPusherNotifier(app_token, uid)
        return self._wxpusher

    def _load_wecom(self):
        """加载企业微信通知器"""
        if self._wecom:
            return self._wecom

        secrets = self.config.get("secrets", {})
        corpid = secrets.get("wecom_corpid", "")
        agentid = secrets.get("wecom_agentid", "")
        secret = secrets.get("wecom_secret", "")
        userid = secrets.get("wecom_userid", "")

        if not all([corpid, agentid, secret, userid]):
            return None

        self._wecom = WeComNotifier(corpid, agentid, secret, userid)
        return self._wecom

    def _load_notifier(self):
        """加载当前通知器（兼容旧代码）"""
        if self._provider == "wecom":
            return self._load_wecom() or self._load_wxpusher()
        else:
            return self._load_wxpusher() or self._load_wecom()

    async def notify(
        self,
        notification_type: NotificationType,
        title: str,
        content: str,
        wait_response: bool = False,
        timeout: int = 300,
        options: list = None,
        extra_data: dict = None
    ) -> Optional[str]:
        """
        发送通知（自动选择企业微信或 WxPusher）
        
        Args:
            notification_type: 通知类型
            title: 标题
            content: 内容
            wait_response: 是否等待响应
            timeout: 超时时间（秒）
            options: 选项列表 [{'label': '选项1', 'description': '描述'}]
            extra_data: 额外数据（如完整的问题结构）
        """
        # 检查是否启用此类型通知
        events_config = self.config.get("notification", {}).get("events", {})
        type_key = notification_type.value
        if not events_config.get(type_key, True):
            return None

        notifier = self._load_notifier()
        if not notifier:
            # 通知不可用时，打印到控制台
            print(f"\n{'='*60}")
            print(f"[通知] {title}")
            print(f"{content}")
            if wait_response:
                print(f"\n请在控制台输入响应:")
                try:
                    return input("> ").strip()
                except:
                    return None
            print(f"{'='*60}\n")
            return None

        # 生成 request_id
        request_id = str(uuid.uuid4())[:8]

        # 对于需要交互的通知类型，先注册 pending_request
        needs_interaction = notification_type in [
            NotificationType.AUTH_REQUIRED,
            NotificationType.CONFIRM_NEEDED,
            NotificationType.INFO_SUPPLEMENT,
        ]
        if needs_interaction:
            try:
                cm = await get_cache_manager()
                import json as json_mod
                request_data = {
                    "title": title,
                    "content": content,
                    "created_at": datetime.now().isoformat(),
                    "timeout": timeout
                }
                # 保存选项信息（供 Web 页面显示）
                if options:
                    request_data["options"] = options
                if extra_data:
                    request_data["extra_data"] = extra_data
                await cm._client.setex(
                    f"pending_request:{request_id}",
                    max(timeout + 3600, 7200),  # 至少 2 小时有效，或者 timeout + 1 小时
                    json_mod.dumps(request_data)
                )
            except Exception as e:
                print(f"[NotificationManager] 注册 pending_request 失败: {e}")

        # 根据通知器类型分发
        if isinstance(notifier, WeComNotifier):
            result = await self._send_via_wecom(
                notifier, notification_type, title, content, request_id, options
            )
        else:
            result = await notifier.send_notification(
                notification_type=notification_type,
                title=title,
                content=content,
                request_id=request_id,
                options=options
            )

        if not result.get("success", False) and "error" in result:
            print(f"[NotificationManager] 发送失败: {result.get('error')}")
            return None

        if not wait_response:
            return None

        # 等待响应
        return await self._wait_for_response(request_id, timeout, title, content)

    async def _send_via_wecom(
        self, notifier, notification_type, title, content, request_id, options
    ) -> Dict:
        """通过企业微信发送通知（卡片+文本双发，解决新域名需申诉问题）"""
        emoji_map = {
            NotificationType.AUTH_REQUIRED: "🔐",
            NotificationType.CONFIRM_NEEDED: "⚠️",
            NotificationType.INFO_SUPPLEMENT: "📝",
            NotificationType.TASK_PAUSED: "⏸️",
            NotificationType.TASK_COMPLETED: "✅",
            NotificationType.PROGRESS_REPORT: "📊",
            NotificationType.ERROR_CRITICAL: "❌"
        }
        emoji = emoji_map.get(notification_type, "📢")

        # 确认类通知：使用文本卡片（可点击跳转确认页面）
        is_confirm = notification_type in [
            NotificationType.AUTH_REQUIRED,
            NotificationType.CONFIRM_NEEDED,
        ]

        # 输入类通知：使用文本卡片（可点击跳转输入页面）
        is_input = notification_type == NotificationType.INFO_SUPPLEMENT

        result = {"success": False}
        action_url = None

        if is_confirm:
            action_url = await get_confirm_url(request_id)
            if action_url:
                desc = f"{content}\n\n请求ID: {request_id}"
                # 1. 发送卡片消息
                result = await notifier.send_textcard(
                    title=f"{emoji} {title}",
                    description=desc,
                    url=action_url,
                    btntxt="确认/取消"
                )
                # 2. 发送完整文本消息（恢复原格式）
                text_msg = f"{emoji} {title}\n\n{content}\n\n---\n请求ID: {request_id}\n👉 点击确认/取消: {action_url}"
                await notifier.send_text(text_msg)

                result["request_id"] = request_id
                return result

        if is_input:
            action_url = await get_input_url(request_id)
            if action_url:
                desc = f"{content}\n\n请求ID: {request_id}"
                # 1. 发送卡片消息
                result = await notifier.send_textcard(
                    title=f"{emoji} {title}",
                    description=desc,
                    url=action_url,
                    btntxt="点击回复"
                )
                # 2. 发送完整文本消息（恢复原格式）
                text_msg = f"{emoji} {title}\n\n{content}\n\n---\n请求ID: {request_id}\n👉 点击回复: {action_url}"
                await notifier.send_text(text_msg)

                result["request_id"] = request_id
                return result

        # 普通通知或无 URL：使用文本消息
        message = f"{emoji} {title}\n\n{content}"

        if is_confirm:
            message += f"\n\n---\n请求ID: {request_id}"
            message += f"\n回复方式: opus3-reply {request_id} Y/N"
        elif is_input:
            message += f"\n\n---\n请求ID: {request_id}"
            message += f"\n回复方式: opus3-reply {request_id} 你的回复"

        message += f"\n\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

        result = await notifier.send_text(message)
        result["request_id"] = request_id
        return result

    async def _wait_for_response(self, request_id: str, timeout: int,
                                   title: str = "", content: str = "") -> Optional[str]:
        """等待用户响应（通过 Redis）

        Args:
            request_id: 请求ID
            timeout: 超时时间（秒）
            title: 请求标题（用于 Web 确认页面显示）
            content: 请求详情（用于 Web 确认页面显示）
        """
        try:
            cm = await get_cache_manager()

            # 注册待响应请求（包含标题和内容，供 Web 确认页面使用）
            await cm._client.setex(
                f"pending_request:{request_id}",
                max(timeout + 3600, 7200),  # 至少 2 小时有效
                json.dumps({
                    "title": title,
                    "content": content,
                    "created_at": datetime.now().isoformat(),
                    "timeout": timeout
                })
            )

            # 轮询等待响应
            elapsed = 0
            reminder_sent = False

            while elapsed < timeout:
                response = await cm._client.get(f"response:{request_id}")
                if response:
                    # 清理 response（已读取）
                    await cm._client.delete(f"response:{request_id}")
                    # 保留 pending_request 一段时间，让用户刷新页面时能看到"已处理"
                    # 它会自然过期
                    return response

                await asyncio.sleep(2)
                elapsed += 2

                # 超时一半时发送提醒
                if not reminder_sent and elapsed >= timeout // 2:
                    reminder_sent = True
                    notifier = self._load_notifier()
                    if notifier:
                        # 尝试获取确认链接
                        confirm_url = await get_confirm_url(request_id)
                        if confirm_url:
                            reply_hint = f"点击确认/取消: {confirm_url}"
                        else:
                            reply_hint = f"回复方式: opus3-reply {request_id} Y/N"

                        if isinstance(notifier, WeComNotifier):
                            await notifier.send_text(
                                f"⏰ 提醒\n\n请求 {request_id} 等待回复中...\n"
                                f"剩余时间: {(timeout - elapsed) // 60} 分钟\n\n"
                                f"{reply_hint}"
                            )
                        else:
                            await notifier.send_message(
                                f"⏰ **提醒**\n\n请求 `{request_id}` 等待回复中...\n\n"
                                f"剩余时间: {(timeout - elapsed) // 60} 分钟\n\n"
                                f"{reply_hint}",
                                summary=f"⏰ 请求 {request_id} 等待回复",
                                content_type=3,
                                url=confirm_url or ""
                            )

            # 超时，不删除 pending_request（让它自然过期，用户还能看到页面）
            notifier = self._load_notifier()
            if notifier:
                await notifier.send_message(
                    f"⌛ **请求超时**\n\n请求 `{request_id}` 已超时（{timeout}秒）\n\n任务将暂停等待下次交互",
                    summary=f"⌛ 请求 {request_id} 超时",
                    content_type=3
                )
            return None

        except Exception as e:
            print(f"[NotificationManager] 等待响应失败: {e}")
            return None

    # ==================== 便捷方法 ====================

    async def request_auth(self, resource: str, reason: str) -> bool:
        """请求授权"""
        response = await self.notify(
            NotificationType.AUTH_REQUIRED,
            f"需要授权: {resource}",
            f"原因: {reason}\n\n请回复 Y 授权，N 拒绝",
            wait_response=True,
            options=["Y - 授权", "N - 拒绝"]
        )
        return response and response.upper().strip() in ["Y", "YES", "1"]

    async def request_confirm(self, action: str, details: str) -> bool:
        """请求确认"""
        response = await self.notify(
            NotificationType.CONFIRM_NEEDED,
            f"操作确认: {action}",
            f"详情:\n{details}\n\n请回复 Y 确认，N 取消",
            wait_response=True,
            options=["Y - 确认", "N - 取消"]
        )
        return response and response.upper().strip() in ["Y", "YES", "1"]

    async def request_info(self, what: str, prompt: str) -> Optional[str]:
        """请求补充信息"""
        return await self.notify(
            NotificationType.INFO_SUPPLEMENT,
            f"需要补充: {what}",
            prompt,
            wait_response=True
        )

    async def report_progress(self, task: str, progress: int, details: str = ""):
        """汇报进度"""
        bar = "█" * (progress // 10) + "░" * (10 - progress // 10)
        await self.notify(
            NotificationType.PROGRESS_REPORT,
            f"进度: {task}",
            f"进度: [{bar}] {progress}%\n\n{details}",
            wait_response=False
        )

    async def report_error(self, error: str, context: str = ""):
        """报告错误"""
        await self.notify(
            NotificationType.ERROR_CRITICAL,
            "任务执行错误",
            f"错误: {error}\n\n上下文: {context}",
            wait_response=False
        )

    async def report_completion(self, task: str, summary: str):
        """报告完成"""
        await self.notify(
            NotificationType.TASK_COMPLETED,
            f"完成: {task}",
            summary,
            wait_response=False
        )


# ==================== 辅助函数 ====================

async def get_confirm_url(request_id: str) -> Optional[str]:
    """从 Redis 获取确认页面 URL

    Args:
        request_id: 请求ID

    Returns:
        完整的确认页面 URL，如 https://xxx.trycloudflare.com/vizo/confirm/{request_id}
        如果 confirm_server 未运行或无 tunnel URL，返回 None
    """
    try:
        cm = await get_cache_manager()
        tunnel_url = await cm._client.get("confirm_server:tunnel_url")
        if tunnel_url:
            return tunnel_url.rstrip("/") + confirm_path(request_id)
        return None
    except Exception:
        return None


async def get_input_url(request_id: str) -> Optional[str]:
    """从 Redis 获取输入页面 URL

    Args:
        request_id: 请求ID

    Returns:
        完整的输入页面 URL，如 https://xxx.trycloudflare.com/vizo/input/{request_id}
        如果 confirm_server 未运行或无 tunnel URL，返回 None
    """
    try:
        cm = await get_cache_manager()
        tunnel_url = await cm._client.get("confirm_server:tunnel_url")
        if tunnel_url:
            return tunnel_url.rstrip("/") + input_path(request_id)
        return None
    except Exception:
        return None


# 全局实例
_notification_manager: Optional[NotificationManager] = None


def get_notification_manager(config: Dict = None) -> NotificationManager:
    """获取通知管理器单例"""
    global _notification_manager
    if _notification_manager is None:
        if config is None:
            import os
            config_path = str(_PROJECT_ROOT / 'config.json')
            with open(config_path) as f:
                config = json.load(f)
        _notification_manager = NotificationManager(config)
    return _notification_manager
