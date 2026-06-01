#!/usr/bin/env python3
"""
日报管理模块 - 多项目日报隔离
Opus 智能协作系统 v3.5

功能：
- 根据项目自动确定日报目录
- 支持追加更新（一天一个文件）
- 防止跨项目日报混淆

使用:
    from daily_report import DailyReportManager

    manager = DailyReportManager()

    # 获取今日日报路径
    path = manager.get_report_path("xiaozhi")

    # 追加内容到日报
    manager.append_section("xiaozhi", "## 新增任务", "内容...")

    # 创建或更新完整日报
    manager.save_report("xiaozhi", report_content)
"""

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict

from lib.project_identity import build_project_path_map, canonicalize_project_name

_LIB_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _LIB_DIR.parent



# 项目日报目录映射
# 所有项目日报统一存放在 OPUS_HOME/docs/日报/<项目名>/
DAILY_REPORT_BASE = os.path.expanduser(str(_PROJECT_ROOT / "docs/日报"))


def _load_project_name_map() -> dict:
    """项目名映射（硬编码，V6 不再依赖 V5 project_registry）"""
    return {
        "xiaozhi": "xiaozhi",
        "xiaozhi-server": "xiaozhi",
        "vizo": "vizo",
    }


# 项目名映射（从注册表动态加载）
PROJECT_NAME_MAP = _load_project_name_map()

def get_project_report_dir(project: str) -> str:
    """获取项目日报目录（统一在 OPUS_HOME/docs/日报/<项目>/）"""
    project = canonicalize_project_name(project) or project
    normalized = PROJECT_NAME_MAP.get(project, project)
    return os.path.join(DAILY_REPORT_BASE, normalized)

# 兼容旧代码的映射（已废弃，仅用于迁移）
PROJECT_REPORT_DIRS = {
    "xiaozhi": get_project_report_dir("xiaozhi"),
    "vizo": get_project_report_dir("vizo"),
}


class DailyReportManager:
    """日报管理器 - 管理多项目的日报文件"""

    def __init__(self, custom_dirs: Dict[str, str] = None):
        """
        初始化日报管理器

        Args:
            custom_dirs: 自定义项目目录映射 {项目名: 日报目录路径}
        """
        self._dirs = PROJECT_REPORT_DIRS.copy()
        if custom_dirs:
            self._dirs.update(custom_dirs)

    def get_report_dir(self, project: str) -> str:
        """
        获取项目的日报目录

        Args:
            project: 项目名称

        Returns:
            日报目录路径
        """
        if project in self._dirs:
            return self._dirs[project]

        # 未知项目：使用通用目录
        return os.path.join(DAILY_REPORT_BASE, project)

    def get_report_path(self, project: str, date: datetime = None) -> str:
        """
        获取指定日期的日报文件路径

        Args:
            project: 项目名称
            date: 日期，默认为今天

        Returns:
            日报文件路径（如 /opt/xiaozhi-server/daily_reports/2026-02-01.md）
        """
        if date is None:
            date = datetime.now()

        report_dir = self.get_report_dir(project)
        filename = date.strftime("%Y-%m-%d.md")

        return os.path.join(report_dir, filename)

    def ensure_dir(self, project: str) -> str:
        """
        确保日报目录存在

        Args:
            project: 项目名称

        Returns:
            目录路径
        """
        report_dir = self.get_report_dir(project)
        os.makedirs(report_dir, exist_ok=True)
        return report_dir

    def report_exists(self, project: str, date: datetime = None) -> bool:
        """
        检查日报文件是否存在

        Args:
            project: 项目名称
            date: 日期，默认为今天

        Returns:
            是否存在
        """
        path = self.get_report_path(project, date)
        return os.path.exists(path)

    def read_report(self, project: str, date: datetime = None) -> Optional[str]:
        """
        读取日报内容

        Args:
            project: 项目名称
            date: 日期，默认为今天

        Returns:
            日报内容，不存在则返回 None
        """
        path = self.get_report_path(project, date)
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                return f.read()
        return None

    def save_report(self, project: str, content: str, date: datetime = None) -> str:
        """
        保存日报（覆盖写入）

        Args:
            project: 项目名称
            content: 日报内容
            date: 日期，默认为今天

        Returns:
            保存的文件路径
        """
        self.ensure_dir(project)
        path = self.get_report_path(project, date)

        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)

        return path

    def append_section(self, project: str, section_title: str, section_content: str,
                       date: datetime = None) -> str:
        """
        追加内容到日报

        如果日报不存在，会先创建基本结构。
        如果同名 section 已存在，会追加到该 section 末尾。

        Args:
            project: 项目名称
            section_title: 段落标题（如 "## 新增任务"）
            section_content: 段落内容
            date: 日期，默认为今天

        Returns:
            保存的文件路径
        """
        existing = self.read_report(project, date)

        if existing is None:
            # 创建新日报
            if date is None:
                date = datetime.now()
            header = f"# 日报 {date.strftime('%Y-%m-%d')}\n\n"
            content = header + f"{section_title}\n\n{section_content}\n"
        else:
            # 检查是否已有同名 section
            if section_title in existing:
                # 在该 section 末尾追加
                lines = existing.split('\n')
                result = []
                in_section = False
                inserted = False

                for i, line in enumerate(lines):
                    result.append(line)

                    if line.strip() == section_title.strip():
                        in_section = True
                    elif in_section and line.startswith('#') and not inserted:
                        # 遇到下一个 section，在前面插入内容
                        result.insert(len(result) - 1, section_content + '\n')
                        in_section = False
                        inserted = True

                # 如果是最后一个 section
                if in_section and not inserted:
                    result.append('\n' + section_content)

                content = '\n'.join(result)
            else:
                # 新增 section
                content = existing.rstrip() + f"\n\n{section_title}\n\n{section_content}\n"

        return self.save_report(project, content, date)

    def update_section(self, project: str, section_title: str, new_content: str,
                       date: datetime = None) -> str:
        """
        更新（替换）日报中的某个 section

        Args:
            project: 项目名称
            section_title: 段落标题
            new_content: 新的段落内容（不包含标题）
            date: 日期，默认为今天

        Returns:
            保存的文件路径
        """
        existing = self.read_report(project, date)

        if existing is None:
            # 直接创建
            return self.append_section(project, section_title, new_content, date)

        if section_title not in existing:
            # section 不存在，追加
            return self.append_section(project, section_title, new_content, date)

        # 替换 section 内容
        lines = existing.split('\n')
        result = []
        in_section = False
        replaced = False

        for line in lines:
            if line.strip() == section_title.strip():
                # 进入目标 section
                result.append(line)
                result.append('')
                result.append(new_content)
                in_section = True
                replaced = True
            elif in_section and line.startswith('#'):
                # 退出目标 section
                in_section = False
                result.append('')
                result.append(line)
            elif not in_section:
                result.append(line)
            # in_section 时跳过原内容

        content = '\n'.join(result)
        return self.save_report(project, content, date)


# ==================== 全局实例 ====================

_report_manager: Optional[DailyReportManager] = None


def get_report_manager() -> DailyReportManager:
    """获取全局日报管理器"""
    global _report_manager
    if _report_manager is None:
        _report_manager = DailyReportManager()
    return _report_manager


# ==================== 便捷函数 ====================

def get_daily_report_path(project: str) -> str:
    """获取今日日报路径"""
    return get_report_manager().get_report_path(project)


def save_daily_report(project: str, content: str) -> str:
    """保存今日日报"""
    return get_report_manager().save_report(project, content)


def append_to_daily_report(project: str, section_title: str, content: str) -> str:
    """追加内容到今日日报"""
    return get_report_manager().append_section(project, section_title, content)


# ==================== 项目检测 ====================

def detect_project_from_path(file_path: str) -> Optional[str]:
    """
    从文件路径检测所属项目

    Args:
        file_path: 文件路径

    Returns:
        项目名称，无法检测则返回 None
    """
    path = os.path.abspath(os.path.expanduser(file_path))
    project_map = build_project_path_map()

    for root, project in sorted(project_map.items(), key=lambda item: len(item[0]), reverse=True):
        root_abs = os.path.abspath(os.path.expanduser(root))
        if path == root_abs or path.startswith(root_abs + os.sep):
            return project

    return None


def detect_project_from_cwd() -> Optional[str]:
    """从当前工作目录检测项目"""
    return detect_project_from_path(os.getcwd())
