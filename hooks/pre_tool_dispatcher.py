#!/usr/bin/env python3
"""
PreToolUse 统一调度器 — 合并 wecom_message_injector + message_interceptor + auto_serena

v5.0: 将3个独立hooks合并为1个进程，共享1个Redis连接。
每次工具调用只启动1个Python进程，而非3个。

额外优化:
- 消息检查节流: 每10秒最多检查一次Redis消息队列
- 共享Redis连接: 3个逻辑共用1个连接
"""

import os
import re
import sys
import json
import time
import uuid
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / 'lib'))
from paths import CONFIG_FILE, OPUS_HOME
from project_identity import detect_project as detect_runtime_project

# ==================== 安全限制 ====================
MAX_MESSAGES = 5
MAX_MSG_CHARS = 500
MAX_TOTAL_CHARS = 2000
THROTTLE_SECONDS = 10  # 消息检查节流间隔

# 企微启用状态（模块级读取，hook 进程不使用 config_loader）
_wecom_enabled = False
try:
    with open(CONFIG_FILE) as _f:
        _wecom_enabled = json.load(_f).get("wecom", {}).get("enabled", False)
except Exception:
    pass
if os.environ.get("WECOM_ENABLED", "").lower() == "true":
    _wecom_enabled = True

# 代码文件扩展名
CODE_EXTENSIONS = {
    '.py', '.js', '.ts', '.tsx', '.jsx', '.java', '.go', '.rs', '.c', '.cpp',
    '.h', '.hpp', '.cs', '.rb', '.php', '.swift', '.kt', '.scala', '.vue'
}

# 节流状态文件（避免频繁Redis查询）
THROTTLE_FILE = '/tmp/.vizo_msg_check_ts'


def get_redis_client():
    config_path = CONFIG_FILE
    try:
        import redis
        with open(config_path) as f:
            config = json.load(f)
        redis_config = config.get("redis", {})
        return redis.Redis(
            host=redis_config.get("host", "127.0.0.1"),
            port=redis_config.get("port", 6380),
            decode_responses=True,
            socket_timeout=2,
            socket_connect_timeout=1
        )
    except Exception:
        return None


def should_check_messages():
    """节流: 每 THROTTLE_SECONDS 秒最多检查一次消息"""
    try:
        if os.path.exists(THROTTLE_FILE):
            last_check = os.path.getmtime(THROTTLE_FILE)
            if time.time() - last_check < THROTTLE_SECONDS:
                return False
        # 更新时间戳
        Path(THROTTLE_FILE).touch()
        return True
    except Exception:
        return True


def detect_project():
    return detect_runtime_project()


# ==================== 逻辑1: 企微消息检查 ====================
def check_wecom_messages(r, session_id):
    """检查企微消息队列（per-session + 兼容全局）"""
    queue_key = f"wecom_session:{session_id}"
    messages = []

    # 优先消费 per-session 队列
    for _ in range(MAX_MESSAGES):
        msg = r.rpop(queue_key)
        if not msg:
            break
        try:
            messages.append(json.loads(msg))
        except (json.JSONDecodeError, TypeError):
            continue

    # 过渡期：per-session 无消息时，尝试全局队列
    if not messages:
        for _ in range(MAX_MESSAGES):
            msg = r.rpop("wecom_messages")
            if not msg:
                break
            try:
                messages.append(json.loads(msg))
            except (json.JSONDecodeError, TypeError):
                continue

    # 丢弃 per-session 队列溢出消息
    overflow = 0
    while True:
        leftover = r.rpop(queue_key)
        if not leftover:
            break
        overflow += 1

    if not messages:
        return None

    lines = ["📱 **收到企微消息：**"]
    total_chars = 0
    for m in messages:
        content = m.get("content", "")[:MAX_MSG_CHARS]
        time_str = m.get("received_at", "")[:19]
        line = f"  [{time_str}] {content}"
        total_chars += len(line)
        if total_chars > MAX_TOTAL_CHARS:
            lines.append("  ...（内容过长，已截断）")
            break
        lines.append(line)

    if overflow > 0:
        lines.append(f"  ⚠️ 还有 {overflow} 条消息因队列过多被丢弃")

    return "\n".join(lines)


# ==================== 逻辑2: 待处理消息拦截 ====================



# ==================== 逻辑3: Serena 自动提示 ====================
def check_serena_hint(r, project, tool_name, tool_input):
    """Serena 代码符号提示（原 auto_serena.py，仅 Read/Grep）"""
    if tool_name not in ('Grep', 'Read'):
        return None

    if tool_name == 'Read':
        file_path = tool_input.get('file_path', '')
        if not file_path:
            return None
        ext = Path(file_path).suffix.lower()
        if ext not in CODE_EXTENSIONS:
            return None
        cache_key = f"serena_cache:{project}:read:{file_path}"
        if r.exists(cache_key):
            return None
        r.setex(cache_key, 1800, "1")
        file_name = Path(file_path).name
        inject_id = str(uuid.uuid4())[:8]
        return f"""<!-- OPUS_TEMP_INJECT:auto_serena:{inject_id} -->
<system-reminder>
💡 **Serena 自动提示** - 检测到代码文件读取

你正在读取: `{file_name}`

推荐先使用 Serena 获取文件结构：
```
mcp__serena__get_symbols_overview("{file_path}")
```
</system-reminder>
<!-- /OPUS_TEMP_INJECT -->"""

    elif tool_name == 'Grep':
        pattern = tool_input.get('pattern', '')
        path = tool_input.get('path', '')
        code_patterns = ['def ', 'class ', 'function ', 'async ', 'import ', 'from ',
                         'const ', 'let ', 'var ', 'export ', 'interface ', 'type ']
        is_code = (path and Path(path).suffix.lower() in CODE_EXTENSIONS) or \
                  any(p in pattern for p in code_patterns)
        if not is_code:
            return None
        cache_key = f"serena_cache:{project}:grep:{pattern[:50]}"
        if r.exists(cache_key):
            return None
        r.setex(cache_key, 1800, "1")
        keyword = pattern.split('(')[0].split('[')[0].strip()
        inject_id = str(uuid.uuid4())[:8]
        return f"""<!-- OPUS_TEMP_INJECT:auto_serena:{inject_id} -->
<system-reminder>
💡 **Serena 自动提示** - 检测到代码搜索

你正在搜索: `{pattern}`

推荐先使用 Serena 获取精确符号信息：
```
mcp__serena__find_symbol("{keyword}")
```
</system-reminder>
<!-- /OPUS_TEMP_INJECT -->"""

    return None


# ==================== 安全拦截: 子代理危险命令 ====================
# 子代理通过 OPUS_AGENT_ROLE 标识身份，此处硬性阻止危险操作
BLOCKED_COMMANDS_RE = re.compile(
    r'systemctl\s+.*restart|systemctl\s+.*stop', re.IGNORECASE
)

def check_agent_blocked_command(tool_name, tool_input):
    """检查子代理是否试图执行被禁止的命令（如重启 secretary）"""
    role = os.environ.get("OPUS_AGENT_ROLE")
    if not role:
        return None
    if tool_name != "Bash":
        return None
    command = tool_input.get("command", "")
    if BLOCKED_COMMANDS_RE.search(command):
        return (f"子代理 [{role}] 禁止执行服务重启/停止命令。"
                f"请在 JSON 输出中设置 needs_restart: true，由编排器安全处理。")
    return None


# ==================== 主入口 ====================
def main():
    try:
        if sys.stdin.isatty():
            return

        data = sys.stdin.read().strip()
        if not data:
            return

        context = json.loads(data)
        tool_name = context.get('tool_name', '')
        tool_input = context.get('tool_input', {})

        # 🛡️ 优先检查：子代理危险命令拦截（无需 Redis，直接阻止）
        block_reason = check_agent_blocked_command(tool_name, tool_input)
        if block_reason:
            print(json.dumps({"decision": "block", "reason": block_reason}))
            return

        project = detect_project()
        results = []

        # 消息检查有节流
        check_msgs = should_check_messages()

        r = get_redis_client()
        if not r:
            print(json.dumps({}))
            return

        try:
            # 计算 session_id
            from session_utils import get_session_project
            claude_pid = os.getppid()
            project_name = get_session_project(os.getcwd())
            session_id = f"{project_name}:{claude_pid}"

            # 心跳刷新（不受节流控制，每次工具调用都刷新）
            try:
                r.expire(f"claude_session:{session_id}", 7200)
            except Exception:
                pass

            # 逻辑0: 暂停反馈注入（不受节流控制，每次工具调用都检查）
            # 当子代理被 SIGSTOP 暂停后用户补充信息，SIGCONT 恢复后
            # agent_runner 将反馈写入 Redis pause_feedback:{task_id}，
            # 此处读取并注入到子代理会话中（一次性消费）
            task_id = os.environ.get("OPUS_TASK_ID")
            if task_id:
                try:
                    feedback = r.get(f"pause_feedback:{task_id}")
                    if feedback:
                        r.delete(f"pause_feedback:{task_id}")
                        results.append(
                            f"⚠️ **用户暂停后补充的信息（请务必参考）：**\n\n{feedback}"
                        )
                except Exception:
                    pass

            # 逻辑1: 企微消息（节流，仅企微启用时检查）
            if check_msgs and _wecom_enabled:
                msg = check_wecom_messages(r, session_id)
                if msg:
                    results.append(msg)

            # 逻辑2: Serena 提示（有自己的缓存机制，不需要额外节流）
            hint = check_serena_hint(r, project, tool_name, tool_input)
            if hint:
                results.append(hint)
        finally:
            r.close()

        if results:
            # 合并所有输出
            combined = "\n\n".join(results)
            print(json.dumps({"systemMessage": combined}))
        else:
            print(json.dumps({}))

    except Exception as e:
        import traceback
        print(json.dumps({"systemMessage": f"[pre_tool_dispatcher 异常] {e}\n{traceback.format_exc()[-500:]}"}), file=sys.stderr)
        print(json.dumps({}))


if __name__ == '__main__':
    main()
