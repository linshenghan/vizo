#!/usr/bin/env python3
"""
远程任务监听服务 - 后台常驻，监听用户手机端提交的新任务
Opus 智能协作系统 v3.0

此服务作为后台守护进程运行，功能：
1. 监听 Redis 中的新任务提交
2. 收到任务后，写入待执行队列
3. 用户启动 claude 时，session_start hook 自动读取待执行任务

使用:
    python3 task_monitor.py start       # 后台启动
    python3 task_monitor.py start -f    # 前台启动
    python3 task_monitor.py stop        # 停止
    python3 task_monitor.py status      # 查看状态
"""

import os
import sys
import json
import signal
import asyncio
import argparse
from pathlib import Path
from datetime import datetime
from typing import Optional

import redis.asyncio as aioredis
from lib.project_identity import canonicalize_project_name

_LIB_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _LIB_DIR.parent


# 配置
CONFIG_PATH = str(_PROJECT_ROOT / 'config.json')
PID_FILE = os.path.expanduser(str(_PROJECT_ROOT / "lib/task_monitor.pid"))
LOG_FILE = os.path.expanduser(str(_PROJECT_ROOT / "logs/task_monitor.log"))

# 项目路径映射
PROJECT_PATHS = {
    'xiaozhi': '/opt/xiaozhi-server',
    'vizo': str(_PROJECT_ROOT),
    'default': os.path.expanduser('~'),
}


class TaskMonitor:
    """任务监听服务"""

    def __init__(self, redis_host="127.0.0.1", redis_port=6380):
        self.redis_host = redis_host
        self.redis_port = redis_port
        self._redis: Optional[aioredis.Redis] = None
        self._running = True

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = aioredis.Redis(
                host=self.redis_host, port=self.redis_port,
                decode_responses=True
            )
        return self._redis

    async def run(self):
        """主循环：监听新任务"""
        r = await self._get_redis()
        log(f"任务监听服务启动，监听 Redis {self.redis_host}:{self.redis_port}")

        while self._running:
            try:
                # 扫描所有 newtask-* 的响应
                async for key in r.scan_iter("response:newtask-*"):
                    task_content = await r.get(key)
                    if task_content:
                        request_id = key.replace("response:", "")
                        
                        # 读取 pending_request 获取项目信息
                        pending_key = f"pending_request:{request_id}"
                        pending_data = await r.get(pending_key)
                        
                        project = "default"
                        if pending_data:
                            try:
                                info = json.loads(pending_data)
                                content = info.get("content", "")
                                if "项目:" in content:
                                    project = content.split("项目:")[1].split("\n")[0].strip()
                            except:
                                pass
                        
                        log(f"收到新任务: {task_content[:100]}...")
                        project = canonicalize_project_name(project) or project
                        log(f"项目: {project}")
                        
                        # 删除响应（避免重复处理）
                        await r.delete(key)
                        await r.delete(pending_key)
                        
                        # 保存为待执行任务
                        await self.save_pending_task(project, task_content)
                        
                await asyncio.sleep(2)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log(f"监听错误: {e}")
                await asyncio.sleep(5)

        log("任务监听服务停止")

    async def save_pending_task(self, project: str, task: str):
        """保存待执行任务到 Redis"""
        r = await self._get_redis()
        project = canonicalize_project_name(project) or project
        task_data = json.dumps({
            "task": task,
            "project": project,
            "work_dir": PROJECT_PATHS.get(project, PROJECT_PATHS['default']),
            "created_at": datetime.now().isoformat()
        })
        # 使用 list 存储，支持多个任务排队
        await r.lpush(f"pending_tasks:{project}", task_data)
        # 设置过期时间（7天）
        await r.expire(f"pending_tasks:{project}", 86400 * 7)
        log(f"任务已保存到队列: pending_tasks:{project}")

    def stop(self):
        self._running = False


def log(msg: str):
    """写日志"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except:
        pass


# ==================== 进程管理 ====================

def write_pid():
    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def read_pid() -> Optional[int]:
    try:
        with open(PID_FILE) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def is_running() -> bool:
    pid = read_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def ensure_running():
    """确保服务正在运行，如果没有则启动"""
    if not is_running():
        # 读取配置
        with open(CONFIG_PATH) as f:
            config = json.load(f)
        redis_config = config.get("redis", {})
        
        # 后台启动
        pid = os.fork()
        if pid > 0:
            return True  # 父进程返回
        
        os.setsid()
        write_pid()
        
        # 重定向输出
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        sys.stdout = open(LOG_FILE, "a")
        sys.stderr = sys.stdout
        
        monitor = TaskMonitor(
            redis_host=redis_config.get("host", "127.0.0.1"),
            redis_port=redis_config.get("port", 6380)
        )
        
        def signal_handler(sig, frame):
            monitor.stop()
        
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
        
        try:
            asyncio.run(monitor.run())
        finally:
            if os.path.exists(PID_FILE):
                os.remove(PID_FILE)
        
        sys.exit(0)
    return True


def cmd_start(args):
    """启动服务"""
    if is_running():
        pid = read_pid()
        print(f"任务监听服务已在运行中 (PID: {pid})")
        return

    with open(CONFIG_PATH) as f:
        config = json.load(f)
    redis_config = config.get("redis", {})

    if args.foreground:
        write_pid()
        monitor = TaskMonitor(
            redis_host=redis_config.get("host", "127.0.0.1"),
            redis_port=redis_config.get("port", 6380)
        )

        def signal_handler(sig, frame):
            monitor.stop()

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        try:
            asyncio.run(monitor.run())
        finally:
            if os.path.exists(PID_FILE):
                os.remove(PID_FILE)
    else:
        pid = os.fork()
        if pid > 0:
            print(f"任务监听服务已启动 (PID: {pid})")
            print(f"日志: {LOG_FILE}")
            sys.exit(0)

        os.setsid()
        write_pid()

        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        sys.stdout = open(LOG_FILE, "a")
        sys.stderr = sys.stdout

        monitor = TaskMonitor(
            redis_host=redis_config.get("host", "127.0.0.1"),
            redis_port=redis_config.get("port", 6380)
        )

        def signal_handler(sig, frame):
            monitor.stop()

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        try:
            asyncio.run(monitor.run())
        finally:
            if os.path.exists(PID_FILE):
                os.remove(PID_FILE)


def cmd_stop(args):
    """停止服务"""
    pid = read_pid()
    if pid is None or not is_running():
        print("任务监听服务未运行")
        return

    os.kill(pid, signal.SIGTERM)
    print(f"已发送停止信号 (PID: {pid})")

    import time
    for _ in range(10):
        if not is_running():
            break
        time.sleep(0.5)

    if is_running():
        os.kill(pid, signal.SIGKILL)
        print("强制终止")

    if os.path.exists(PID_FILE):
        os.remove(PID_FILE)
    print("任务监听服务已停止")


def cmd_status(args):
    """查看状态"""
    pid = read_pid()
    running = is_running()
    print(f"任务监听服务: {'运行中' if running else '未运行'}", end="")
    if running:
        print(f" (PID: {pid})")
    else:
        print()


def main():
    parser = argparse.ArgumentParser(description="Opus v3 任务监听服务")
    parser.add_argument("command", nargs="?", default="status",
                        choices=["start", "stop", "status", "ensure"],
                        help="start/stop/status/ensure")
    parser.add_argument("-f", "--foreground", action="store_true",
                        help="前台运行")
    args = parser.parse_args()

    if args.command == "start":
        cmd_start(args)
    elif args.command == "stop":
        cmd_stop(args)
    elif args.command == "ensure":
        ensure_running()
        if is_running():
            print(f"任务监听服务运行中 (PID: {read_pid()})")
    else:
        cmd_status(args)


if __name__ == "__main__":
    main()
