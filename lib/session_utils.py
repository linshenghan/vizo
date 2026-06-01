"""
会话注册工具 — 统一的项目名映射和 session_id 生成

被 session_start.py、stop_guard.py、pre_tool_dispatcher.py、confirm_server.py 共同引用。
仅用于会话注册和消息路由，不影响各 Hook 原有的 detect_project() 逻辑。
"""

import os
from pathlib import Path

from lib.project_identity import detect_project as detect_runtime_project

_LIB_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _LIB_DIR.parent


def get_session_project(cwd: str) -> str:
    """获取用于会话注册的规范化项目名"""
    return detect_runtime_project(cwd)
