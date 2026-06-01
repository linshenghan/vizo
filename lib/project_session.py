#!/usr/bin/env python3
"""
项目会话管理器 - 自动管理项目级对话记忆
Opus 智能协作系统 v3.0

使用方法:
1. 对话开始时: session = start_project_session("xiaozhi")
2. 记录任务: session.task("任务描述", "completed", "结果")
3. 记录决策: session.decision("决策内容", "原因")
4. 添加待办: session.pending("待办事项")
5. 对话结束时: session.end("本次对话总结")
"""

import os
import json
import asyncio
from typing import Optional, Dict, List, TYPE_CHECKING
from datetime import datetime
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _LIB_DIR.parent


if TYPE_CHECKING:
    from .model_router import ModelRouter
else:
    ModelRouter = None

# memory_system 已在 v2.5 重构中删除
# MemorySystem 保留为 stub 类以兼容旧代码
class MemorySystem:
    """Stub - v2.5 已删除，改用 Serena MCP"""
    pass
from .wecom_notifier import WeComNotifier


# ==================== 企微通知 ====================

def get_wecom_notifier() -> Optional[WeComNotifier]:
    """获取企业微信通知器"""
    try:
        config_path = str(_PROJECT_ROOT / 'config.json')
        with open(config_path) as f:
            config = json.load(f)

        secrets = config.get("secrets", {})
        if all([
            secrets.get("wecom_corpid"),
            secrets.get("wecom_agentid"),
            secrets.get("wecom_secret"),
            secrets.get("wecom_userid")
        ]):
            return WeComNotifier(
                corpid=secrets["wecom_corpid"],
                agentid=secrets["wecom_agentid"],
                secret=secrets["wecom_secret"],
                userid=secrets["wecom_userid"]
            )
    except Exception as e:
        print(f"[警告] 企微通知初始化失败: {e}")

    return None


class ProjectSession:
    """项目会话 - v2.5 已删除 memory_system，改用 Serena MCP

    此类保留供向后兼容，但功能已移除。
    请使用 Serena MCP 的工具：
    - mcp__serena__read_memory("memory_name")
    - mcp__serena__write_memory("memory_name", "content")
    """

    def __init__(self, project: str, memory: Optional[MemorySystem] = None, router: Optional[ModelRouter] = None):
        self.project = project
        self.memory = memory  # 已废弃
        self.router = router
        self.started_at = datetime.now()
        self._task_count = 0
        self._decision_count = 0
        self._active = True

        # memory_system 已删除，set_project 不再可用
        # if self.memory:
        #     self.memory.set_project(project)

    # ==================== 记录 API ====================

    def task(self, description: str, status: str = "completed", result: str = "") -> "ProjectSession":
        """记录任务 - v2.5 已删除 memory_system"""
        # memory_system 已删除，此方法无操作
        # if self.memory:
        #     self.memory.record_task(description, status, result)
        self._task_count += 1
        return self

    def decision(self, content: str, reason: str = "") -> "ProjectSession":
        """记录关键决策 - v2.5 已删除 memory_system"""
        # memory_system 已删除，此方法无操作
        # if self.memory:
        #     self.memory.record_decision(content, reason)
        self._decision_count += 1
        return self

    def pending(self, item: str) -> "ProjectSession":
        """添加待办事项 - v2.5 已删除 memory_system"""
        # memory_system 已删除，此方法无操作
        # if self.memory:
        #     self.memory.add_pending(item)
        return self

    def message(self, role: str, content: str) -> "ProjectSession":
        """记录消息 - v2.5 已删除 memory_system"""
        # memory_system 已删除，此方法无操作
        # if self.memory:
        #     self.memory.record_message(role, content)
        return self

    def context(self, key: str, value) -> "ProjectSession":
        """更新项目上下文 - v2.5 已删除 memory_system"""
        # memory_system 已删除，此方法无操作
        # if self.memory:
        #     self.memory.update_context(self.project, key, value)
        return self

    def notify(self, message: str, event_type: str = "info") -> "ProjectSession":
        """
        发送通知

        Args:
            message: 通知消息
            event_type: 事件类型 (info/success/error/warning)

        Returns:
            self (支持链式调用)
        """
        try:
            asyncio.create_task(self._send_notification(message, event_type))
        except RuntimeError:
            pass

        return self

    async def _send_notification(self, message: str, event_type: str = "info"):
        """异步发送通知"""
        notifier = get_wecom_notifier()
        if not notifier:
            return

        try:
            emoji_map = {
                "info": "ℹ️",
                "success": "✅",
                "error": "❌",
                "warning": "⚠️"
            }
            emoji = emoji_map.get(event_type, "")
            full_message = f"{emoji} [{self.project}] {message}"
            await notifier.send_text(full_message)
            await notifier.close()
        except Exception as e:
            print(f"[警告] 通知发送失败: {e}")

    # ==================== 模型调用 API ====================

    async def call_model(
        self,
        task_type: str,
        prompt: str,
        max_tokens: int = 8192
    ) -> Optional[Dict]:
        """
        调用模型（通过智能路由）- v2.5 已删除 memory_system 和 router

        Args:
            task_type: 任务类型 (code_gen/data_proc/algo/convert/text/system/research)
            prompt: 提示词
            max_tokens: 最大 token 数

        Returns:
            ModelResponse（或 None）
        """
        if not self.router:
            print("[警告] router 未初始化，无法调用模型")
            return None

        try:
            from .model_router import TaskType
            task_type_enum = TaskType(task_type)
            response = await self.router.route_and_call(task_type_enum, prompt, max_tokens)

            # 自动记录任务
            status = "completed" if response.success else "failed"
            result = response.content[:100] if response.success else response.error
            self.task(f"模型调用: {prompt[:50]}...", status, result)

            return response
        except Exception as e:
            print(f"[警告] 模型调用失败: {e}")
            return None

    # ==================== 会话管理 ====================

    def end(self, summary: str = "") -> str:
        """
        结束会话并保存

        Args:
            summary: 会话总结

        Returns:
            保存的文件路径
        """
        if not self._active:
            return ""

        self._active = False

        # 生成自动总结
        if not summary:
            summary = self._generate_auto_summary()

        # memory_system 已删除，无法保存会话
        file_path = ""

        # 异步发送企微通知
        try:
            asyncio.create_task(self._notify_session_end(summary))
        except RuntimeError:
            # 如果没有事件循环，则在后台启动一个
            pass

        return file_path

    async def _notify_session_end(self, summary: str):
        """异步发送会话结束通知"""
        notifier = get_wecom_notifier()
        if not notifier:
            return

        try:
            message = (
                f"✅ 项目会话已完成\n\n"
                f"项目: {self.project}\n"
                f"总结: {summary}\n"
                f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            await notifier.send_text(message)
            await notifier.close()
        except Exception as e:
            print(f"[警告] 企微通知发送失败: {e}")

    def _generate_auto_summary(self) -> str:
        """生成自动总结"""
        duration = datetime.now() - self.started_at
        minutes = int(duration.total_seconds() / 60)

        return f"会话时长 {minutes} 分钟，完成 {self._task_count} 项任务，做出 {self._decision_count} 项决策"

    # ==================== 状态查询 ====================

    def get_context(self) -> Dict:
        """获取项目上下文 - v2.5 已删除 memory_system"""
        return {}

    def get_recall(self) -> Optional[str]:
        """获取上次会话回忆 - v2.5 已删除 memory_system"""
        return None

    @property
    def is_active(self) -> bool:
        """会话是否活跃"""
        return self._active


# ==================== 全局状态 ====================

_current_session: Optional[ProjectSession] = None


# ==================== 多项目会话管理器 ====================

class MultiProjectSessionManager:
    """
    多项目会话管理器 - 支持一个窗口同时管理多个项目

    使用场景：
    - 用户在同一个 Claude 窗口中处理多个项目
    - 需要在项目间切换，同时保持各自的会话状态

    使用示例:
        manager = get_session_manager()

        # 切换到 xiaozhi 项目
        session1 = manager.switch_project("xiaozhi")
        session1.task("完成任务A")

        # 切换到 vizo 项目
        session2 = manager.switch_project("vizo")
        session2.task("完成任务B")

        # 获取当前活动项目
        current = manager.get_active_session()

        # 结束所有会话
        manager.end_all()
    """

    def __init__(self):
        self._sessions: Dict[str, ProjectSession] = {}
        self._active_project: Optional[str] = None

    def switch_project(self, project: str) -> ProjectSession:
        """
        切换到指定项目（如不存在则创建新会话）

        Args:
            project: 项目名称

        Returns:
            ProjectSession 实例
        """
        if project not in self._sessions:
            self._sessions[project] = self._create_session(project)
        self._active_project = project
        return self._sessions[project]

    def get_session(self, project: str = None) -> Optional[ProjectSession]:
        """
        获取指定项目的会话

        Args:
            project: 项目名称，默认为当前活动项目

        Returns:
            ProjectSession 或 None
        """
        proj = project or self._active_project
        return self._sessions.get(proj) if proj else None

    def get_active_session(self) -> Optional[ProjectSession]:
        """获取当前活动项目的会话"""
        if self._active_project:
            return self._sessions.get(self._active_project)
        return None

    @property
    def active_project(self) -> Optional[str]:
        """当前活动的项目名称"""
        return self._active_project

    def list_projects(self) -> List[str]:
        """列出所有活跃的项目"""
        return list(self._sessions.keys())

    def is_project_active(self, project: str) -> bool:
        """检查指定项目是否有活跃会话"""
        return project in self._sessions

    def end_project(self, project: str = None, summary: str = "") -> str:
        """
        结束指定项目的会话

        Args:
            project: 项目名称，默认为当前活动项目
            summary: 会话总结

        Returns:
            保存的文件路径
        """
        proj = project or self._active_project
        if proj and proj in self._sessions:
            session = self._sessions[proj]
            path = session.end(summary)
            del self._sessions[proj]
            if self._active_project == proj:
                # 如果结束的是当前项目，切换到其他项目或清空
                self._active_project = next(iter(self._sessions), None)
            return path
        return ""

    def end_all(self) -> Dict[str, str]:
        """
        结束所有项目的会话

        Returns:
            {项目名: 保存路径} 字典
        """
        results = {}
        for project in list(self._sessions.keys()):
            results[project] = self.end_project(project)
        return results

    def _create_session(self, project: str) -> ProjectSession:
        """创建新的项目会话"""
        memory = get_memory_system()
        router = get_router()
        session = ProjectSession(project, memory, router)

        # 打印上次会话回忆
        recall = session.get_recall()
        if recall:
            print(f"\n{'='*60}")
            print(f"[切换到项目 '{project}']")
            print(recall)
            print(f"{'='*60}\n")
        else:
            print(f"\n[项目 '{project}' 开始新会话，暂无历史记录]\n")

        return session


# ==================== 全局多项目管理器 ====================

_session_manager: Optional[MultiProjectSessionManager] = None


def get_session_manager() -> MultiProjectSessionManager:
    """
    获取全局多项目会话管理器

    Returns:
        MultiProjectSessionManager 单例实例
    """
    global _session_manager
    if _session_manager is None:
        _session_manager = MultiProjectSessionManager()
    return _session_manager


def switch_project(project: str) -> ProjectSession:
    """
    切换到指定项目（便捷函数）

    Args:
        project: 项目名称

    Returns:
        ProjectSession 实例
    """
    return get_session_manager().switch_project(project)


def start_project_session(project: str) -> ProjectSession:
    """
    开始项目会话

    Args:
        project: 项目名称 (如 "xiaozhi")

    Returns:
        ProjectSession 实例

    使用示例:
        session = start_project_session("xiaozhi")
        # 会自动打印上次会话回忆

        session.task("分析代码结构", "completed", "找到3个模块")
        session.decision("使用方案A", "性能更好")
        session.pending("待测试功能X")

        session.end("本次完成了XXX")

    注意:
        此函数现在通过 MultiProjectSessionManager 管理，支持多项目并行。
        向后兼容：单项目使用方式不变。
    """
    global _current_session

    # 使用多项目管理器
    manager = get_session_manager()
    session = manager.switch_project(project)

    # 同时更新传统全局变量（向后兼容）
    _current_session = session

    return session


def get_current_session() -> Optional[ProjectSession]:
    """获取当前会话"""
    global _current_session
    return _current_session


def end_current_session(summary: str = "") -> str:
    """
    结束当前会话

    注意:
        此函数现在通过 MultiProjectSessionManager 管理。
        只结束当前活动项目，其他项目会话保持活跃。
    """
    global _current_session

    manager = get_session_manager()
    path = manager.end_project(summary=summary)

    # 更新传统全局变量
    _current_session = manager.get_active_session()

    return path


# ==================== 便捷函数（供 Claude Code 直接调用）====================

def project_task(description: str, status: str = "completed", result: str = ""):
    """记录任务（需先调用 start_project_session）"""
    if _current_session:
        _current_session.task(description, status, result)


def project_decision(content: str, reason: str = ""):
    """记录决策（需先调用 start_project_session）"""
    if _current_session:
        _current_session.decision(content, reason)


def project_pending(item: str):
    """添加待办（需先调用 start_project_session）"""
    if _current_session:
        _current_session.pending(item)


def project_end(summary: str = "") -> str:
    """结束会话（需先调用 start_project_session）"""
    return end_current_session(summary)


# ==================== 项目检测 ====================

# 已知项目列表
KNOWN_PROJECTS = {
    "xiaozhi": {
        "keywords": [
            # 项目名
            "小智", "xiaozhi", "xiao zhi",
            # 硬件相关
            "esp32", "esp-32", "乐鑫", "espressif",
            "inmp441", "max98357", "es8311", "es7210",
            # 语音相关
            "语音识别", "语音合成", "语音检测", "语音唤醒",
            "asr", "tts", "vad", "stt",
            "funasr", "sherpa", "silero",
            "opus编码", "opus解码", "opuslib",
            # 麦克风/音频
            "麦克风", "microphone", "mic", "音频",
            "i2s", "pcm", "采样率",
            # 功能模块
            "唤醒词", "wake word", "热词",
            "声纹", "voiceprint", "speaker"
        ],
        "paths": [
            "/opt/xiaozhi-server",
            "/opt/xiaozhi",
            "xiaozhi-esp32-server",
            "xiaozhi-esp32"
        ]
    },
    "vizo": {
        "keywords": [
            "vizo", "维造", "协作系统", "多模型协同",
            "orchestrator", "secretary",
            "企微", "wecom",
            "记忆系统", "project_session"
        ],
        "paths": [
            str(_PROJECT_ROOT),
            'vizo'
        ]
    }
}


def detect_project(user_input: str, file_paths: List[str] = None) -> Optional[str]:
    """
    从用户输入和文件路径检测涉及的项目

    Args:
        user_input: 用户输入
        file_paths: 涉及的文件路径列表（可选）

    Returns:
        项目名称，如果未检测到返回 None
    """
    input_lower = user_input.lower()

    for project, config in KNOWN_PROJECTS.items():
        # 1. 关键词匹配
        for keyword in config["keywords"]:
            if keyword.lower() in input_lower:
                return project

        # 2. 路径匹配
        if file_paths:
            for file_path in file_paths:
                for project_path in config.get("paths", []):
                    # 展开 ~ 路径
                    expanded_path = os.path.expanduser(project_path)
                    if expanded_path in file_path or project_path in file_path:
                        return project

    return None


def detect_project_from_path(file_path: str) -> Optional[str]:
    """
    仅从文件路径检测项目

    Args:
        file_path: 文件路径

    Returns:
        项目名称，如果未检测到返回 None
    """
    for project, config in KNOWN_PROJECTS.items():
        for project_path in config.get("paths", []):
            expanded_path = os.path.expanduser(project_path)
            if expanded_path in file_path or project_path in file_path:
                return project
    return None


def auto_start_session_if_needed(user_input: str, file_paths: List[str] = None) -> Optional[ProjectSession]:
    """
    根据用户输入或文件路径自动启动项目会话

    Args:
        user_input: 用户输入
        file_paths: 涉及的文件路径列表（可选）

    Returns:
        ProjectSession 如果检测到项目，否则 None
    """
    project = detect_project(user_input, file_paths)
    if project:
        return start_project_session(project)
    return None
