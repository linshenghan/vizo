#!/usr/bin/env python3
"""控制信号模块：任务生命周期控制的跨进程信号传递

信号文件格式：.vizo/signals/{task_id}.json
信号消费语义：read 即消费（读后删除），避免重复处理
"""

import json
import logging
import time
from pathlib import Path

from lib.paths import SIGNALS_DIR


logger = logging.getLogger(__name__)

# 信号过期时间（秒）
SIGNAL_EXPIRY = 30 * 60  # 30 分钟


def write_signal(task_id: str, action: str, source: str = "cli", **kwargs) -> str:
    """写入控制信号文件

    Args:
        task_id: 目标任务 ID
        action: 控制动作（pause / rollback / terminate）
        source: 信号来源（cli / web）
        **kwargs: 附加参数（target_step, feedback, rollback 等）

    Returns:
        信号文件路径
    """
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    signal_file = SIGNALS_DIR / f"{task_id}.json"

    data = {
        "action": action,
        "task_id": task_id,
        "target_step": kwargs.get("target_step", ""),
        "feedback": kwargs.get("feedback", ""),
        "created_at": time.time(),
        "source": source,
    }

    # 透传额外字段（rollback 等）
    for key in ("rollback",):
        if key in kwargs:
            data[key] = kwargs[key]

    try:
        signal_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info(f"控制信号已写入: {action} → 任务 {task_id} (source={source})")
        return str(signal_file)
    except OSError as e:
        logger.error(f"写入控制信号失败: {e}")
        raise


def read_signal(task_id: str) -> dict | None:
    """读取并消费控制信号（读后删除）

    Args:
        task_id: 目标任务 ID

    Returns:
        信号数据 dict，无信号或已过期返回 None
    """
    signal_file = SIGNALS_DIR / f"{task_id}.json"
    if not signal_file.exists():
        return None

    try:
        data = json.loads(signal_file.read_text(encoding="utf-8"))

        # 过期信号忽略并清理
        created_at = data.get("created_at", 0)
        if time.time() - created_at > SIGNAL_EXPIRY:
            logger.info(f"忽略过期控制信号: {data.get('action')} (任务 {task_id})")
            signal_file.unlink(missing_ok=True)
            return None

        # 消费：读后删除
        signal_file.unlink(missing_ok=True)
        logger.info(f"消费控制信号: {data.get('action')} → 任务 {task_id}")
        return data

    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"读取控制信号失败: {e}")
        # 损坏的信号文件也清理掉
        try:
            signal_file.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def clear_signal(task_id: str) -> None:
    """清除信号文件（无论是否存在）"""
    signal_file = SIGNALS_DIR / f"{task_id}.json"
    try:
        signal_file.unlink(missing_ok=True)
    except OSError:
        pass


def write_audit_log(task_dir: Path, action: str, source: str = "",
                    task_id: str = "", **kwargs) -> None:
    """写入审计日志（JSONL 格式）

    Args:
        task_dir: 任务目录
        action: 操作类型（pause / resume / rollback / terminate）
        source: 来源
        task_id: 任务 ID
        **kwargs: 附加信息（target_step, detail 等）
    """
    from datetime import datetime

    task_dir = Path(task_dir)
    task_dir.mkdir(parents=True, exist_ok=True)
    audit_file = task_dir / "audit.jsonl"

    entry = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "action": action,
        "source": source,
        "task_id": task_id,
    }
    # 合并附加字段
    for k, v in kwargs.items():
        if v:
            entry[k] = v

    try:
        with open(audit_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning(f"写入审计日志失败: {e}")
