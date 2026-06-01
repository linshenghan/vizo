#!/usr/bin/env python3
"""
PreToolUse 统一调度器 — 合并 message_interceptor + auto_serena

v5.0: 将独立 hooks 合并为 1 个进程，共享 1 个 Redis 连接。
"""

import os
import re
import sys
import json
import uuid
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / 'lib'))
from paths import CONFIG_FILE, OPUS_HOME
from project_identity import detect_project as detect_runtime_project

# 代码文件扩展名
CODE_EXTENSIONS = {
    '.py', '.js', '.ts', '.tsx', '.jsx', '.java', '.go', '.rs', '.c', '.cpp',
    '.h', '.hpp', '.cs', '.rb', '.php', '.swift', '.kt', '.scala', '.vue'
}

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


def detect_project():
    return detect_runtime_project()


# ==================== 逻辑1: 待处理消息拦截 ====================



# ==================== 逻辑2: Serena 自动提示 ====================
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

        r = get_redis_client()
        if not r:
            print(json.dumps({}))
            return

        try:
            # 暂停反馈注入（每次工具调用都检查）
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
