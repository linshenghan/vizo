#!/usr/bin/env python3
"""
Claude Code 会话恢复守护器
Opus 智能协作系统 v3.2

功能：
- 监控 Claude Code 会话状态
- 检测 API overload 错误
- 自动恢复会话，持续重试最多 2 小时

用法：
  # 启动守护（后台运行）
  python3 session_guardian.py start

  # 手动触发恢复（当 Claude Code 因 overload 退出时）
  python3 session_guardian.py recover <session-id>

  # 查看状态
  python3 session_guardian.py status
"""

import os
import sys
import json
import time
import signal
import subprocess
import argparse
from pathlib import Path
from datetime import datetime, timedelta
import redis

_LIB_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _LIB_DIR.parent


# 配置
CONFIG = {
    "max_retry_duration": 7200,     # 最大重试时长（秒）= 2 小时
    "initial_retry_delay": 10,       # 初始重试延迟（秒）
    "max_retry_delay": 300,          # 最大重试延迟（秒）= 5 分钟
    "backoff_multiplier": 1.5,       # 退避乘数
    "health_check_interval": 30,     # 健康检查间隔（秒）
    "pid_file": "/tmp/claude_guardian.pid",
    "log_file": "/tmp/claude_guardian.log",
    "state_file": "/tmp/claude_guardian_state.json",
}

# 需要重试的错误关键词
RETRY_KEYWORDS = [
    "overloaded",
    "rate_limit",
    "429",
    "529",
    "too many requests",
    "service_unavailable",
    "temporarily unavailable",
    "capacity",
]


def log(message: str):
    """记录日志"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}\n"
    print(line, end="")
    with open(CONFIG["log_file"], "a") as f:
        f.write(line)


def get_redis():
    """获取 Redis 连接"""
    config_path = _PROJECT_ROOT / 'config.json'
    if config_path.exists():
        with open(config_path) as f:
            config = json.load(f)
        redis_config = config.get("redis", {})
        return redis.Redis(
            host=redis_config.get("host", "127.0.0.1"),
            port=redis_config.get("port", 6380),
            decode_responses=True,
            socket_timeout=5
        )
    return redis.Redis(host="127.0.0.1", port=6380, decode_responses=True)


def save_state(state: dict):
    """保存守护状态"""
    with open(CONFIG["state_file"], "w") as f:
        json.dump(state, f, indent=2, default=str)


def load_state() -> dict:
    """加载守护状态"""
    if os.path.exists(CONFIG["state_file"]):
        with open(CONFIG["state_file"]) as f:
            return json.load(f)
    return {}


def notify_wecom(title: str, message: str):
    """发送企微通知"""
    try:
        r = get_redis()
        webhook_url = r.get('wecom:webhook_url')
        if webhook_url:
            import requests
            msg = {
                "msgtype": "text",
                "text": {
                    "content": f"🔄 {title}\n\n{message}"
                }
            }
            requests.post(webhook_url, json=msg, timeout=5)
    except Exception as e:
        log(f"企微通知失败: {e}")


def get_latest_session_id() -> str:
    """获取最新的会话 ID"""
    claude_projects = Path.home() / '.claude/projects'
    if not claude_projects.exists():
        return None

    # 查找最近修改的 jsonl 文件
    jsonl_files = list(claude_projects.rglob('*.jsonl'))
    if not jsonl_files:
        return None

    latest = max(jsonl_files, key=lambda p: p.stat().st_mtime)
    return latest.stem


def check_api_health() -> tuple[bool, str]:
    """检查 Anthropic API 健康状态"""
    try:
        import httpx
        from lib.settings_handler import extract_managed_main_session_env, get_main_session_api_model

        # 从环境获取 API 配置
        base_url = os.environ.get('ANTHROPIC_BASE_URL', 'https://api.anthropic.com')
        api_key = os.environ.get('ANTHROPIC_API_KEY', '')
        current_env = {}

        if not api_key:
            # 从 settings.json 读取
            settings_path = Path.home() / '.claude/settings.json'
            if settings_path.exists():
                with open(settings_path) as f:
                    settings = json.load(f)
                settings_env = settings.get('env', {})
                base_url = settings_env.get('ANTHROPIC_BASE_URL', base_url)
                api_key = settings_env.get('ANTHROPIC_AUTH_TOKEN', '') or settings_env.get('ANTHROPIC_API_KEY', '')
                current_env = extract_managed_main_session_env(settings_env)

        if not api_key:
            return True, "无法获取 API Key，跳过健康检查"

        # 发送一个最小的请求来检查 API 状态
        # 使用 messages API 的一个空请求来测试连接
        headers = {
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }
        if api_key.startswith("sk-ant-"):
            headers["x-api-key"] = api_key
        else:
            headers["Authorization"] = f"Bearer {api_key}"

        with httpx.Client(timeout=10) as client:
            # 尝试请求 messages endpoint（会失败但可以检查状态码）
            response = client.post(
                f"{base_url}/v1/messages",
                headers=headers,
                json={
                    "model": get_main_session_api_model(base_url, current_env=current_env, prefer="haiku"),
                    "max_tokens": 1,
                    "messages": []
                }
            )

            # 200-499 表示 API 在线（即使是错误响应）
            if response.status_code < 500:
                return True, "API 正常"
            elif response.status_code == 529:
                return False, "API overloaded (529)"
            else:
                return False, f"API 错误 ({response.status_code})"

    except httpx.ConnectError:
        return False, "无法连接到 API"
    except httpx.TimeoutException:
        return False, "API 请求超时"
    except Exception as e:
        return True, f"健康检查异常: {e}"


def resume_session(session_id: str) -> bool:
    """恢复指定会话"""
    log(f"尝试恢复会话: {session_id}")

    try:
        # 使用 claude --resume 恢复会话
        # 但这需要在终端中运行，无法在后台直接恢复交互式会话
        # 因此我们只能记录恢复指令供用户使用

        cmd = f"claude --resume {session_id}"
        log(f"恢复命令: {cmd}")

        # 发送通知
        notify_wecom(
            "会话恢复就绪",
            f"API 恢复正常\n\n请在终端执行：\n{cmd}"
        )

        return True
    except Exception as e:
        log(f"恢复失败: {e}")
        return False


def wait_for_api_recovery():
    """等待 API 恢复，支持 2 小时持续重试"""
    start_time = datetime.now()
    end_time = start_time + timedelta(seconds=CONFIG["max_retry_duration"])
    retry_delay = CONFIG["initial_retry_delay"]
    attempt = 0

    state = {
        "status": "waiting_for_recovery",
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "last_check": None,
        "attempts": 0
    }
    save_state(state)

    notify_wecom(
        "API Overload 检测",
        f"开始自动重试，最长持续 2 小时\n开始时间: {start_time.strftime('%H:%M:%S')}\n结束时间: {end_time.strftime('%H:%M:%S')}"
    )

    while datetime.now() < end_time:
        attempt += 1
        state["attempts"] = attempt
        state["last_check"] = datetime.now().isoformat()
        save_state(state)

        log(f"第 {attempt} 次健康检查...")
        healthy, message = check_api_health()

        if healthy and "overload" not in message.lower():
            log(f"API 恢复正常: {message}")
            state["status"] = "recovered"
            save_state(state)

            # 获取最新会话并通知用户
            session_id = get_latest_session_id()
            if session_id:
                resume_session(session_id)
            else:
                notify_wecom("API 恢复正常", "API 已恢复，可以开始新会话")

            return True

        log(f"API 仍不可用: {message}")
        log(f"等待 {retry_delay} 秒后重试...")

        time.sleep(retry_delay)

        # 指数退避
        retry_delay = min(
            retry_delay * CONFIG["backoff_multiplier"],
            CONFIG["max_retry_delay"]
        )

    # 超时
    log("达到最大重试时长（2小时），停止重试")
    state["status"] = "timeout"
    save_state(state)

    notify_wecom(
        "重试超时",
        "已持续重试 2 小时，API 仍不可用\n请手动检查或稍后重试"
    )

    return False


def start_daemon():
    """启动守护进程"""
    # 检查是否已运行
    if os.path.exists(CONFIG["pid_file"]):
        with open(CONFIG["pid_file"]) as f:
            old_pid = f.read().strip()
        try:
            os.kill(int(old_pid), 0)
            print(f"守护进程已在运行 (PID: {old_pid})")
            return
        except OSError:
            pass  # 进程不存在

    # Fork 进入后台
    if os.fork() > 0:
        print("守护进程已启动")
        return

    os.setsid()

    if os.fork() > 0:
        sys.exit(0)

    # 写入 PID
    with open(CONFIG["pid_file"], "w") as f:
        f.write(str(os.getpid()))

    # 设置信号处理
    def cleanup(signum, frame):
        log("收到停止信号，清理退出")
        if os.path.exists(CONFIG["pid_file"]):
            os.remove(CONFIG["pid_file"])
        sys.exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    log("守护进程启动")

    # 主循环：定期检查状态
    while True:
        try:
            state = load_state()

            if state.get("status") == "waiting_for_recovery":
                # 已经在等待恢复，继续
                pass
            else:
                # 检查 Redis 中是否有 overload 标记
                try:
                    r = get_redis()
                    overload_flag = r.get("claude:api_overloaded")
                    if overload_flag:
                        log("检测到 API overload 标记，开始恢复流程")
                        r.delete("claude:api_overloaded")
                        wait_for_api_recovery()
                except Exception as e:
                    log(f"Redis 检查失败: {e}")

            time.sleep(CONFIG["health_check_interval"])

        except Exception as e:
            log(f"守护循环异常: {e}")
            time.sleep(60)


def stop_daemon():
    """停止守护进程"""
    if not os.path.exists(CONFIG["pid_file"]):
        print("守护进程未运行")
        return

    with open(CONFIG["pid_file"]) as f:
        pid = int(f.read().strip())

    try:
        os.kill(pid, signal.SIGTERM)
        print(f"已发送停止信号到 PID {pid}")
        time.sleep(1)
        os.remove(CONFIG["pid_file"])
    except OSError as e:
        print(f"停止失败: {e}")


def show_status():
    """显示状态"""
    print("=" * 50)
    print("Claude Code 会话守护器状态")
    print("=" * 50)

    # 守护进程状态
    if os.path.exists(CONFIG["pid_file"]):
        with open(CONFIG["pid_file"]) as f:
            pid = f.read().strip()
        try:
            os.kill(int(pid), 0)
            print(f"守护进程: 运行中 (PID: {pid})")
        except OSError:
            print("守护进程: 已停止（PID 文件存在但进程不存在）")
    else:
        print("守护进程: 未运行")

    # 状态信息
    state = load_state()
    if state:
        print(f"\n当前状态: {state.get('status', 'unknown')}")
        print(f"开始时间: {state.get('start_time', '-')}")
        print(f"最后检查: {state.get('last_check', '-')}")
        print(f"重试次数: {state.get('attempts', 0)}")

    # API 健康状态
    print("\n正在检查 API 状态...")
    healthy, message = check_api_health()
    print(f"API 状态: {'✅ ' if healthy else '❌ '}{message}")

    # 最新会话
    session_id = get_latest_session_id()
    if session_id:
        print(f"\n最新会话: {session_id}")
        print(f"恢复命令: claude --resume {session_id}")


def trigger_recovery():
    """手动触发恢复流程"""
    print("开始 API 恢复等待（最长 2 小时）...")
    success = wait_for_api_recovery()
    if success:
        print("✅ API 已恢复")
    else:
        print("❌ 恢复超时")


def mark_overloaded():
    """标记 API overloaded（供 hooks 调用）"""
    try:
        r = get_redis()
        r.setex("claude:api_overloaded", 300, "1")  # 5 分钟过期
        log("已标记 API overloaded")

        # 如果守护进程未运行，直接触发恢复
        if not os.path.exists(CONFIG["pid_file"]):
            log("守护进程未运行，直接启动恢复流程")
            wait_for_api_recovery()

    except Exception as e:
        log(f"标记失败: {e}")


def main():
    parser = argparse.ArgumentParser(description="Claude Code 会话恢复守护器")
    parser.add_argument("action", choices=["start", "stop", "status", "recover", "mark-overload"],
                       help="执行的操作")
    parser.add_argument("session_id", nargs="?", help="会话 ID（recover 时使用）")

    args = parser.parse_args()

    if args.action == "start":
        start_daemon()
    elif args.action == "stop":
        stop_daemon()
    elif args.action == "status":
        show_status()
    elif args.action == "recover":
        trigger_recovery()
    elif args.action == "mark-overload":
        mark_overloaded()


if __name__ == "__main__":
    main()
