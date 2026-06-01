#!/usr/bin/env python3
"""
规则外部存储 - Redis 存储核心行为规则

v4.0: 规则不占用上下文，按需读取

规则类型：
1. 代码分析规则 - 用 Serena 还是 Grep
2. 文档生成规则 - 必须创建预览链接
3. 多模型委派规则 - 什么任务委派给什么模型
4. 远程会话规则 - 轮询行为

存储结构：
- opus_rules:{project} - JSON 格式的规则集合
- opus_rule_version:{project} - 规则版本号
"""

import json
from pathlib import Path
from typing import Optional, Dict, Any

import redis

_LIB_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _LIB_DIR.parent



# 默认规则（首次初始化时写入 Redis）
DEFAULT_RULES = {
    "code_analysis": {
        "prefer_serena": True,
        "serena_tools": ["get_symbols_overview", "find_symbol", "find_referencing_symbols"],
        "fallback_to_grep": True,
        "reminder": "代码分析请用 Serena，不要直接 grep"
    },
    "frontend_debug": {
        "prefer_mcp": True,
        "mcp_tools": ["take_screenshot", "evaluate_script", "list_pages", "select_page"],
        "fallback_to_script": False,
        "reminder": "前端调试必须用 mcp__chrome-devtools__xxx，禁止手写 Python 脚本"
    },
    "doc_generation": {
        "force_preview_link": True,
        "domain": "opus.bingbing.asia",
        "reminder": "文档必须创建预览链接，禁止只发文本"
    },
    "multi_model": {
        "enabled": True,
        "code_gen": {
            "min_lines": 50,
            "model": "Qwen3-Coder-480B",
            "reminder": "大段代码生成请委派给 Qwen3-Coder"
        },
        "text_summary": {
            "model": "Qwen2.5-72B",
            "reminder": "文档润色请委派给 Qwen2.5-72B"
        },
        "format_convert": {
            "model": "Qwen2.5-7B",
            "reminder": "格式转换请委派给 Qwen2.5-7B"
        }
    },

}


def get_redis_client() -> Optional[redis.Redis]:
    """获取 Redis 连接"""
    config_path = _PROJECT_ROOT / 'config.json'
    try:
        with open(config_path) as f:
            config = json.load(f)
        redis_config = config.get("redis", {})
        return redis.Redis(
            host=redis_config.get("host", "127.0.0.1"),
            port=redis_config.get("port", 6380),
            decode_responses=True
        )
    except Exception:
        return None


def init_rules(project: str, force: bool = False) -> bool:
    """
    初始化项目规则

    Args:
        project: 项目名称
        force: 是否强制覆盖现有规则

    Returns:
        是否成功初始化
    """
    r = get_redis_client()
    if not r:
        return False

    try:
        key = f"opus_rules:{project}"

        # 如果规则已存在且不强制覆盖，跳过
        if r.exists(key) and not force:
            r.close()
            return True

        # 写入默认规则
        r.set(key, json.dumps(DEFAULT_RULES, ensure_ascii=False))
        r.set(f"opus_rule_version:{project}", "4.0")
        r.close()
        return True
    except Exception:
        return False


def get_rules(project: str) -> Optional[Dict[str, Any]]:
    """
    获取项目规则

    Args:
        project: 项目名称

    Returns:
        规则字典，失败返回 None
    """
    r = get_redis_client()
    if not r:
        return None

    try:
        key = f"opus_rules:{project}"
        data = r.get(key)
        r.close()

        if data:
            return json.loads(data)

        # 规则不存在，初始化
        init_rules(project)
        return DEFAULT_RULES
    except Exception:
        return None


def get_rule(project: str, rule_path: str) -> Optional[Any]:
    """
    获取单条规则

    Args:
        project: 项目名称
        rule_path: 规则路径，如 "code_analysis.prefer_serena"

    Returns:
        规则值
    """
    rules = get_rules(project)
    if not rules:
        return None

    try:
        keys = rule_path.split(".")
        value = rules
        for key in keys:
            value = value[key]
        return value
    except (KeyError, TypeError):
        return None


def update_rule(project: str, rule_path: str, value: Any) -> bool:
    """
    更新单条规则

    Args:
        project: 项目名称
        rule_path: 规则路径
        value: 新值

    Returns:
        是否更新成功
    """
    rules = get_rules(project)
    if not rules:
        return False

    r = get_redis_client()
    if not r:
        return False

    try:
        keys = rule_path.split(".")
        target = rules
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = value

        r.set(f"opus_rules:{project}", json.dumps(rules, ensure_ascii=False))
        r.close()
        return True
    except Exception:
        return False


def get_reminder(project: str, category: str) -> Optional[str]:
    """
    获取指定类别的提醒文本

    Args:
        project: 项目名称
        category: 类别 (code_analysis, doc_generation, multi_model)

    Returns:
        提醒文本
    """
    return get_rule(project, f"{category}.reminder")


def get_all_reminders(project: str) -> Dict[str, str]:
    """
    获取所有类别的提醒文本

    Args:
        project: 项目名称

    Returns:
        {category: reminder} 字典
    """
    rules = get_rules(project)
    if not rules:
        return {}

    reminders = {}
    for category, config in rules.items():
        if isinstance(config, dict) and "reminder" in config:
            reminders[category] = config["reminder"]
    return reminders


if __name__ == "__main__":
    # 测试
    import os

    project = "test"

    # 初始化规则
    print(f"Init rules: {init_rules(project, force=True)}")

    # 获取规则
    rules = get_rules(project)
    print(f"Rules: {json.dumps(rules, indent=2, ensure_ascii=False)}")

    # 获取单条规则
    print(f"Prefer Serena: {get_rule(project, 'code_analysis.prefer_serena')}")

    # 获取提醒
    print(f"Reminders: {get_all_reminders(project)}")
