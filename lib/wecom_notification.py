#!/usr/bin/env python3
"""
wecom_notification.py - 兼容性别名模块

Claude 有时会尝试:
    from wecom_notification import send_wecom_notification

这个模块提供别名，重导向到正确的 wecom_notifier 模块。
"""

# 从正确的模块导入所有内容
from wecom_notifier import (
    WeComNotifier,
    send_wecom_notification,
    test_wecom,
)

# 显式导出
__all__ = ['WeComNotifier', 'send_wecom_notification', 'test_wecom']
