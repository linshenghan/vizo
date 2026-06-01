"""
跨进程文件锁：保护 ~/.claude/settings.json 的并发读写。

问题背景：
  Claude Code CLI 在启动时读取 ~/.claude/settings.json 的 env 段，
  并用其中的 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL 覆盖 OS 环境变量。
  agent_runner.py 需要在启动子进程前临时替换此文件为对应模型的认证信息，
  confirm_server 中的 sync_main_session_to_claude_settings 也会写入此文件。
  两者分属不同进程，asyncio.Lock 无法跨进程互斥。

解决方案：
  使用 fcntl.flock 在 ~/.claude/settings.json.flock 上做跨进程互斥。
  提供 async（agent_runner 用）和 sync（settings_handler 用）两个版本。
  均使用 LOCK_NB（非阻塞）+ 重试，避免阻塞 asyncio 事件循环。
"""

import time
import asyncio
import logging
from pathlib import Path

try:
    import fcntl
except ModuleNotFoundError:
    fcntl = None

logger = logging.getLogger("settings_lock")

SETTINGS_LOCKFILE = Path.home() / ".claude" / "settings.json.flock"


def _open_lockfile():
    """打开锁文件（自动创建目录）"""
    SETTINGS_LOCKFILE.parent.mkdir(parents=True, exist_ok=True)
    return open(SETTINGS_LOCKFILE, "w")


def acquire_sync(timeout: float = 5.0):
    """同步获取跨进程文件锁。

    供 sync_main_session_to_claude_settings 等同步函数使用。
    使用 LOCK_NB + sleep 重试，避免无限阻塞。

    Returns:
        file descriptor（调用方负责传给 release）

    Raises:
        TimeoutError: 超时未获取到锁
    """
    fd = _open_lockfile()
    if fcntl is None:
        return fd
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except (BlockingIOError, OSError):
            if time.monotonic() >= deadline:
                fd.close()
                raise TimeoutError(
                    f"settings.json 文件锁获取超时 ({timeout}s)，"
                    "可能有其他进程长时间持有锁"
                )
            time.sleep(0.05)


async def acquire_async(timeout: float = 10.0):
    """异步获取跨进程文件锁。

    供 agent_runner 等 async 函数使用。
    通过 run_in_executor 把阻塞 flock 放到线程池，不阻塞事件循环。
    使用 LOCK_NB + asyncio.sleep 重试。

    Returns:
        file descriptor（调用方负责传给 release）

    Raises:
        TimeoutError: 超时未获取到锁
    """
    fd = _open_lockfile()
    if fcntl is None:
        return fd
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout

    while True:
        try:
            # LOCK_NB 保证立即返回（不阻塞线程池 worker）
            await loop.run_in_executor(
                None, lambda: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            )
            return fd
        except (BlockingIOError, OSError):
            if loop.time() >= deadline:
                fd.close()
                raise TimeoutError(
                    f"settings.json 文件锁获取超时 ({timeout}s)，"
                    "可能有其他进程长时间持有锁"
                )
            await asyncio.sleep(0.1)


def release(fd):
    """释放文件锁并关闭文件描述符。

    安全调用：即使 fd 为 None 或已关闭也不会抛异常。
    """
    if fd is None:
        return
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        pass
    finally:
        try:
            fd.close()
        except Exception:
            pass
