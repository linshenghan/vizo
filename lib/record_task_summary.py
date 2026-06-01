#!/usr/bin/env python3
"""
任务总结记录器 - 由 Claude 主动调用，记录准确的任务总结

用法:
    python3 lib/record_task_summary.py \
        --task "用户让我做了什么" \
        --conclusion "我完成了什么，结果是什么"

示例:
    python3 lib/record_task_summary.py \
        --task "配置手机远程连接 Windows WSL2 终端" \
        --conclusion "已完成 Tailscale + SSH 配置。"

这个脚本会将任务总结存储到 Redis，供 completion_notifier.py 读取。
"""

import os
import sys
import json
import redis
import argparse
from pathlib import Path
from datetime import datetime

_LIB_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _LIB_DIR.parent

from lib.project_identity import canonicalize_project_name, detect_project as detect_runtime_project

sys.path.insert(0, str(Path(__file__).resolve().parent))

def _load_project_path_map():
    """从 config.json 动态加载项目路径映射"""
    base_map = {}
    try:
        config_path = _LIB_DIR.parent / 'config.json'
        with open(config_path, encoding='utf-8') as f:
            config = json.load(f)
        for name, info in config.get('projects', {}).items():
            path = info.get('path', '')
            if path:
                base_map[path] = name
    except Exception:
        pass
    # Fallback defaults (in case config is missing)
    if str(_PROJECT_ROOT) not in base_map:
        base_map[str(_PROJECT_ROOT)] = 'vizo'
    return base_map


# 项目路径映射（从 config.json 动态加载）
PROJECT_PATH_MAP = _load_project_path_map()


def detect_project() -> str:
    """检测当前项目"""
    cwd = os.getcwd()

    for path, project in PROJECT_PATH_MAP.items():
        if cwd.startswith(path):
            return canonicalize_project_name(project) or project

    project = detect_runtime_project(cwd)
    if project:
        return project

    if 'openclaw' in cwd.lower() or '.openclaw' in cwd.lower():
        return 'openclaw'

    dir_name = os.path.basename(cwd)
    return dir_name if dir_name else 'default'


def get_redis():
    """获取 Redis 连接"""
    config_path = _PROJECT_ROOT / 'config.json'
    with open(config_path) as f:
        config = json.load(f)
    redis_config = config.get("redis", {})
    return redis.Redis(
        host=redis_config.get("host", "127.0.0.1"),
        port=redis_config.get("port", 6380),
        decode_responses=True,
        socket_timeout=5
    )


def record_summary(task: str, conclusion: str, project: str = None):
    """
    记录任务总结到 Redis
    
    Args:
        task: 本次任务描述（Claude 自己总结的）
        conclusion: 工作结论（Claude 自己总结的）
        project: 项目名（可选，自动检测）
    """
    if not project:
        project = detect_project()
    project = canonicalize_project_name(project) or project
    
    r = get_redis()
    
    # 存储结构
    summary_data = {
        "task": task,
        "conclusion": conclusion,
        "project": project,
        "recorded_at": datetime.now().isoformat(),
        "cwd": os.getcwd(),
    }
    
    # 使用项目为 key，存储最新的任务总结
    # TTL 1 小时，足够 stop hook 读取
    key = f"task_summary:{project}"
    r.setex(key, 3600, json.dumps(summary_data, ensure_ascii=False))
    
    # 同时追加到历史记录（用于调试）
    history_key = f"task_summary_history:{project}"
    r.lpush(history_key, json.dumps(summary_data, ensure_ascii=False))
    r.ltrim(history_key, 0, 9)  # 只保留最近 10 条
    
    r.close()
    
    print(f"✅ 任务总结已记录")
    print(f"   项目: {project}")
    print(f"   任务: {task[:50]}..." if len(task) > 50 else f"   任务: {task}")
    print(f"   结论: {conclusion[:50]}..." if len(conclusion) > 50 else f"   结论: {conclusion}")


def main():
    parser = argparse.ArgumentParser(description="记录任务总结（由 Claude 主动调用）")
    parser.add_argument("--task", "-t", required=True, help="本次任务描述")
    parser.add_argument("--conclusion", "-c", required=True, help="工作结论")
    parser.add_argument("--project", "-p", default="", help="项目名（可选，自动检测）")
    args = parser.parse_args()
    
    record_summary(
        task=args.task,
        conclusion=args.conclusion,
        project=args.project if args.project else None
    )


if __name__ == "__main__":
    main()
