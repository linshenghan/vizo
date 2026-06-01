#!/usr/bin/env python3
"""
Vizo 秘书进程 — confirm_server 生命周期管理器

管理 confirm_server 的启动、健康检查和重启。
企微消息路由已迁移到 confirm_server + pre_tool_dispatcher，
指令调度功能已被 Web 控制台完全替代。

用法：
  python3 secretary.py                    # 前台运行
  python3 secretary.py --config other.json # 指定配置
  systemctl --user start vizo-secretary   # systemd 托管
"""

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path

from lib.confirm_server_runtime import resolve_confirm_server_pid_file, resolve_confirm_server_port

# 确保能 import 同目录下的模块
sys.path.insert(0, str(Path(__file__).parent))

logger = logging.getLogger(__name__)


def _load_secretary_config(config_arg: str) -> dict:
    project_root = Path(__file__).parent
    requested = (project_root / config_arg).resolve()
    default_config = (project_root / "config.json").resolve()
    if requested == default_config:
        from lib.config_loader import load_config

        return load_config(force_reload=True)
    with open(requested, encoding="utf-8") as f:
        return json.load(f)


def _resolve_secretary_log_file() -> Path:
    """选择一个当前用户可写的日志文件路径。"""
    project_root = Path(__file__).parent
    candidates = [
        project_root / "logs" / "secretary.log",
        Path.home() / ".local" / "state" / "vizo" / "logs" / "secretary.log",
        Path("/tmp/vizo-secretary.log"),
    ]
    for candidate in candidates:
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            probe = candidate.parent / ".secretary_write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return candidate
        except OSError:
            continue
    return Path("/tmp/vizo-secretary.log")


class Secretary:
    """confirm_server 生命周期管理器"""

    def __init__(self, config: dict):
        self.config = config
        self._shutdown = False
        self._confirm_server_proc: asyncio.subprocess.Process | None = None

    async def _start_confirm_server(self):
        """启动 confirm_server 作为子进程（前台模式），跟随 secretary 生命周期"""
        confirm_script = str(Path(__file__).parent / "lib" / "confirm_server.py")
        confirm_port = resolve_confirm_server_port(self.config)

        # 先杀掉可能残留的旧进程（上次异常退出遗留的孤儿进程）
        pid_file = resolve_confirm_server_pid_file()
        try:
            with open(pid_file) as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, signal.SIGTERM)
            logger.info(f"已终止残留的 confirm_server (PID: {old_pid})")
            await asyncio.sleep(1)
        except (FileNotFoundError, ValueError, ProcessLookupError):
            pass

        # 以前台模式 (-f) 启动，作为 secretary 的子进程
        self._confirm_server_proc = await asyncio.create_subprocess_exec(
            sys.executable, confirm_script, "start", "-f", "-p", str(confirm_port),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        logger.info(
            "confirm_server 已启动 (PID: %s, port: %s)",
            self._confirm_server_proc.pid,
            confirm_port,
        )

    async def _stop_confirm_server(self):
        """停止 confirm_server 子进程"""
        if self._confirm_server_proc and self._confirm_server_proc.returncode is None:
            self._confirm_server_proc.terminate()
            try:
                await asyncio.wait_for(self._confirm_server_proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._confirm_server_proc.kill()
            logger.info("confirm_server 已停止")

    async def run(self):
        """主循环：管理 confirm_server 生命周期"""
        await self._start_confirm_server()

        # 企微状态日志（与 confirm_server.py 风格一致）
        wecom_enabled = self.config.get("wecom", {}).get("enabled", False)
        if wecom_enabled:
            logger.info("秘书进程启动，企微功能已启用")
        else:
            logger.info("秘书进程启动，企微未启用（如需启用请设置 WECOM_ENABLED=true）")

        try:
            while not self._shutdown:
                await asyncio.sleep(30)
                # 健康检查：如果 confirm_server 进程退出了，重启
                if self._confirm_server_proc and self._confirm_server_proc.returncode is not None:
                    logger.warning(f"confirm_server 已退出 (rc={self._confirm_server_proc.returncode})，正在重启...")
                    await self._start_confirm_server()
        finally:
            await self._stop_confirm_server()

    def shutdown(self):
        """优雅关闭"""
        logger.info("收到关闭信号")
        self._shutdown = True
        # confirm_server 的异步清理在 run() 的 finally 中执行
        # 这里做同步的兜底终止（防止 finally 未执行）
        if self._confirm_server_proc and self._confirm_server_proc.returncode is None:
            self._confirm_server_proc.terminate()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Vizo 秘书进程")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    args = parser.parse_args()

    config = _load_secretary_config(args.config)

    # 配置日志：同时输出到文件和控制台
    log_file = _resolve_secretary_log_file()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ]
    )

    logger.info(f"秘书进程启动，日志文件: {log_file}")

    secretary = Secretary(config)

    loop = asyncio.new_event_loop()

    # 注册信号处理
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, secretary.shutdown)

    try:
        loop.run_until_complete(secretary.run())
    except KeyboardInterrupt:
        logger.info("收到键盘中断信号")
    finally:
        loop.close()
        logger.info("秘书进程已退出")


if __name__ == "__main__":
    main()
