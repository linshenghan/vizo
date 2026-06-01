#!/usr/bin/env python3
"""
Git 提交守卫 - 防止跨项目提交
Opus 智能协作系统 v3.5

功能：
- 验证待提交的文件是否属于当前项目
- 防止意外将 A 项目的文件提交到 B 项目的仓库
- 提供友好的错误提示

使用:
    # 命令行使用
    python3 git_commit_guard.py /opt/xiaozhi-server

    # 作为模块使用
    from git_commit_guard import GitCommitGuard

    guard = GitCommitGuard("/opt/xiaozhi-server")
    is_safe, errors = guard.check_staged_files()
"""

import os
import sys
import subprocess
from typing import List, Tuple, Optional
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _LIB_DIR.parent



def _load_project_roots() -> dict:
    """项目根目录映射（硬编码，V6 不再依赖 V5 project_registry）"""
    return {
        "xiaozhi": ["/opt/xiaozhi-server", "/opt/xiaozhi"],
        "vizo": [
            str(_PROJECT_ROOT),
        ],
    }


# 项目根目录映射（从注册表动态加载）
PROJECT_ROOTS = _load_project_roots()


class GitCommitGuard:
    """Git 提交守卫 - 验证提交文件的项目归属"""

    def __init__(self, repo_path: str):
        """
        初始化守卫

        Args:
            repo_path: Git 仓库根目录
        """
        self.repo_path = os.path.abspath(repo_path)
        self.project = self._detect_project()

    def _detect_project(self) -> Optional[str]:
        """检测当前仓库属于哪个项目"""
        for project, roots in PROJECT_ROOTS.items():
            for root in roots:
                root_abs = os.path.abspath(os.path.expanduser(root))
                if self.repo_path.startswith(root_abs) or root_abs.startswith(self.repo_path):
                    return project
        return None

    def get_staged_files(self) -> List[str]:
        """获取暂存区的文件列表"""
        try:
            result = subprocess.run(
                ["git", "diff", "--cached", "--name-only"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                check=True
            )
            files = result.stdout.strip().split('\n')
            return [f for f in files if f]  # 过滤空字符串
        except subprocess.CalledProcessError:
            return []

    def get_untracked_files(self) -> List[str]:
        """获取未跟踪的文件列表"""
        try:
            result = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                check=True
            )
            files = result.stdout.strip().split('\n')
            return [f for f in files if f]
        except subprocess.CalledProcessError:
            return []

    def check_file_belongs_to_project(self, file_path: str) -> Tuple[bool, Optional[str]]:
        """
        检查文件是否属于当前项目

        Args:
            file_path: 相对于仓库根目录的文件路径

        Returns:
            (是否属于, 如果不属于则返回实际所属项目)
        """
        abs_path = os.path.join(self.repo_path, file_path)

        for project, roots in PROJECT_ROOTS.items():
            for root in roots:
                root_abs = os.path.abspath(os.path.expanduser(root))
                if abs_path.startswith(root_abs):
                    if project == self.project:
                        return True, None
                    else:
                        return False, project

        # 无法确定项目，默认允许
        return True, None

    def check_staged_files(self) -> Tuple[bool, List[str]]:
        """
        检查暂存区文件是否都属于当前项目

        Returns:
            (是否安全, 错误消息列表)
        """
        staged = self.get_staged_files()
        if not staged:
            return True, []

        errors = []
        for file_path in staged:
            belongs, other_project = self.check_file_belongs_to_project(file_path)
            if not belongs:
                errors.append(
                    f"[跨项目] {file_path} 属于 {other_project}，"
                    f"但当前仓库是 {self.project}"
                )

        return len(errors) == 0, errors

    def validate_commit(self) -> bool:
        """
        验证提交是否安全

        Returns:
            是否可以安全提交
        """
        is_safe, errors = self.check_staged_files()

        if not is_safe:
            print("\n" + "=" * 60)
            print("Git 提交守卫：检测到跨项目文件！")
            print("=" * 60)
            for error in errors:
                print(f"  {error}")
            print("\n建议：")
            print("  1. 使用 git reset HEAD <file> 取消暂存")
            print("  2. 切换到正确的项目目录再提交")
            print("=" * 60 + "\n")
            return False

        return True


def main():
    """命令行入口"""
    if len(sys.argv) < 2:
        repo_path = os.getcwd()
    else:
        repo_path = sys.argv[1]

    guard = GitCommitGuard(repo_path)

    print(f"[Git 守卫] 检查仓库: {repo_path}")
    print(f"[Git 守卫] 检测到项目: {guard.project or '未知'}")

    is_safe = guard.validate_commit()

    if is_safe:
        print("[Git 守卫] 检查通过，可以安全提交")
        sys.exit(0)
    else:
        print("[Git 守卫] 检查失败，请修正后再提交")
        sys.exit(1)


if __name__ == "__main__":
    main()
