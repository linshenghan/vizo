#!/usr/bin/env python3
"""通知与确认请求管理。"""

import json
import uuid
import asyncio
from typing import Optional, Dict
from datetime import datetime
from enum import Enum
from pathlib import Path

from lib.web_paths import confirm_path

_LIB_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _LIB_DIR.parent


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


class NotificationManager:
    """通知管理器。"""

    def __init__(self, config: Dict):
        self.config = config

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
        记录通知；交互类请求通过 Redis 等待 Web 确认页响应。
        
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

        print(f"\n{'='*60}")
        print(f"[通知] {title}")
        print(f"{content}")
        confirm_url = await get_confirm_url(request_id)
        if confirm_url and needs_interaction:
            print(f"确认链接: {confirm_url}")
        print(f"{'='*60}\n")

        if not wait_response:
            return None

        # 等待响应
        return await self._wait_for_response(request_id, timeout, title, content)

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

                if not reminder_sent and elapsed >= timeout // 2:
                    reminder_sent = True
                    confirm_url = await get_confirm_url(request_id)
                    if confirm_url:
                        print(
                            f"[NotificationManager] 请求 {request_id} 等待回复中，"
                            f"剩余 {(timeout - elapsed) // 60} 分钟，确认链接: {confirm_url}"
                        )
                    else:
                        print(
                            f"[NotificationManager] 请求 {request_id} 等待回复中，"
                            f"剩余 {(timeout - elapsed) // 60} 分钟"
                        )

            # 超时，不删除 pending_request（让它自然过期，用户还能看到页面）
            print(f"[NotificationManager] 请求 {request_id} 已超时（{timeout}秒）")
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
