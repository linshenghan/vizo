#!/usr/bin/env python3
"""
获取项目会话记忆的便捷脚本
Opus 智能协作系统 v3.0
"""

import sys
import json
from pathlib import Path
# memory_system 已在 v2.5 重构中删除，改用 Serena MCP
# from memory_system import get_memory_system, recall_project

def main():
    """获取并打印项目记忆"""
    print("❌ 此脚本已在 v2.5 重构中废弃", file=sys.stderr)
    print("请使用 Serena MCP 的工具:", file=sys.stderr)
    print("  mcp__serena__read_memory('memory_name')", file=sys.stderr)
    sys.exit(1)

if __name__ == "__main__":
    main()
