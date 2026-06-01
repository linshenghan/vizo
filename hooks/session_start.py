#!/usr/bin/env python3
"""
SessionStart hook - 自动回忆项目记忆
Vizo 智能协作系统

重构: 使用 ProjectRegistry 替代硬编码路径
"""

import sys
import json
import os
from pathlib import Path

# 添加项目根目录与 lib 模块路径
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / 'lib'))
from paths import CONFIG_FILE, OPUS_HOME

# V6: 不再依赖 V5 project_registry，使用硬编码映射
USE_REGISTRY = False
AUTO_ACTIVATE_PATHS = {
    '/opt/xiaozhi-server': 'xiaozhi',
    '/opt/xiaozhi': 'xiaozhi',
    str(OPUS_HOME): 'opus-router',
}


def build_main_session_route_notice() -> str:
    """读取当前主会话真实路由配置，生成顶部提示文案。"""
    try:
        from settings_handler import extract_managed_main_session_env, resolve_main_session_connection

        settings_path = Path.home() / ".claude" / "settings.json"
        if not settings_path.exists():
            return ""

        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        env = settings.get("env", {})
        base_url = str(env.get("ANTHROPIC_BASE_URL", "") or "").strip()
        managed_env = extract_managed_main_session_env(env)

        stored_provider_id = None
        try:
            cfg = json.loads(Path(CONFIG_FILE).read_text(encoding="utf-8"))
            stored_provider_id = (
                cfg.get("external_models", {})
                .get("anthropic", {})
                .get("provider_id")
            )
        except Exception:
            stored_provider_id = None

        resolved = resolve_main_session_connection(
            base_url,
            current_env=managed_env,
            stored_provider_id=stored_provider_id,
        )

        current_model = str(settings.get("model", "") or "").strip()
        current_alias = current_model.replace("[1m]", "") if current_model else ""
        routing_models = resolved.get("routing_models", {})

        if resolved["provider_id"] == "anthropic":
            parts = [f"🧭 当前主会话连接：{resolved['provider_display']}"]
            if current_model:
                parts.append(f"启动别名：{current_model}")
            return " | ".join(parts)

        if resolved["provider_id"] == "custom":
            parts = [f"🧭 当前主会话连接：{resolved['provider_display']}"]
            if current_model:
                parts.append(f"启动别名：{current_model}")
            parts.append("实际模型：沿用 Claude 官方模型别名")
            return " | ".join(parts)

        if current_alias and current_alias in routing_models:
            actual_model = routing_models.get(current_alias, "")
            parts = [f"🧭 当前主会话连接：{resolved['provider_display']}"]
            parts.append(f"启动别名：{current_model}")
            if actual_model:
                parts.append(f"实际模型：{actual_model}")
            return " | ".join(parts)

        return (
            f"🧭 当前主会话连接：{resolved['provider_display']} | "
            f"Opus → {routing_models.get('opus', 'opus')} | "
            f"Sonnet → {routing_models.get('sonnet', 'sonnet')} | "
            f"Haiku → {routing_models.get('haiku', 'haiku')}"
        )
    except Exception:
        return ""


def check_and_start_services() -> list:
    """检查并启动必要的后台服务

    Returns:
        服务状态消息列表
    """
    import subprocess
    import socket

    messages = []

    # 检查确认服务是否在运行（通过端口检测，兼容 systemd 和手动启动）
    def is_port_listening(port=9390):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                return s.connect_ex(("127.0.0.1", port)) == 0
        except Exception:
            return False

    if is_port_listening():
        # 服务已运行，获取 tunnel URL
        try:
            import redis as sync_redis
            r = sync_redis.Redis(host="127.0.0.1", port=6380, decode_responses=True, socket_timeout=2, socket_connect_timeout=1)
            tunnel_url = r.get("confirm_server:tunnel_url")
            r.close()

            if tunnel_url:
                messages.append(f"🌐 确认服务运行中: {tunnel_url}")
        except Exception:
            messages.append("🌐 确认服务运行中")
    else:
        # 尝试通过 systemd 启动
        try:
            result = subprocess.run(
                ["systemctl", "--user", "start", "vizo-secretary.service"],
                capture_output=True, timeout=5
            )
            if result.returncode == 0:
                messages.append("🌐 确认服务已通过 systemd 启动")
            else:
                messages.append("⚠️ 确认服务未运行，请手动检查: systemctl --user status vizo-secretary")
        except Exception as e:
            messages.append(f"⚠️ 确认服务启动失败: {e}")

    return messages


def detect_project_from_cwd(cwd: str) -> tuple:
    """检测当前目录对应的项目

    Returns:
        (项目ID, 是否自动激活, ProjectInfo或None)
    """
    if USE_REGISTRY:
        registry = get_registry()
        project = registry.detect_project(cwd)
        if project:
            return (project.id, project.auto_activate, project)

        # 使用目录名作为项目名（未注册项目）
        dir_name = os.path.basename(cwd)
        if dir_name:
            return (dir_name, False, None)

        return (None, False, None)
    else:
        # 兼容旧逻辑
        for path, project_id in AUTO_ACTIVATE_PATHS.items():
            if cwd.startswith(path):
                return (project_id, True, None)

        dir_name = os.path.basename(cwd)
        if dir_name:
            return (dir_name, False, None)

        return (None, False, None)


# 行为规则（精简版 v5.1）
# 详细流程由当前注册 hook 按需注入。
OPUS_BEHAVIOR_RULES = """
## 维造 / Vizo 智能协作系统 - 核心规则

**你的定位**：流程统筹主管。需求理解/架构决策/结果审核由你完成，通用子任务可委派外部模型。

**详细规则由 hook 自动注入（你不需要记忆全部）**：
- 开发任务 → 自动注入知识库读取指令 + 委派规则
- 日报/文档 → 自动注入生成流程
- 代码分析 → 自动提示 Serena MCP
- 前端/UI → 自动提示 Chrome DevTools / shadcn-ui MCP
- 复杂需求 → 自动触发多角色协同分析

### 关键命令速查
| 场景 | 命令 |
|------|------|
| 执行开发任务 | `vizo "需求描述"` |
| 恢复中断任务 | `vizo --resume` |
| 快速问答 | `vizo --chat "问题"` |
| 查看任务历史 | `vizo --history` |
| 查看花费 | `vizo --cost` |
| Git 提交 | `git status && git add -A && git commit -m "描述"` |
| Secretary 状态 | `systemctl --user status vizo-secretary` |

### 知识库位置
- 持久知识: `<项目>/.serena/memories/`
- 日报: `{OPUS_HOME}/docs/日报/<项目ID>/`
- 分析报告: `{OPUS_HOME}/docs/分析报告/<项目ID>/`

""".strip()


def read_tools_installed() -> str:
    """读取已安装工具清单"""
    tools_file = Path.home() / '.claude/TOOLS_INSTALLED.md'
    try:
        if tools_file.exists():
            content = tools_file.read_text()
            # 只返回关键部分（MCP 工具表格和强制规则）
            lines = content.split('\n')
            key_sections = []
            in_key_section = False
            for line in lines:
                if '## MCP 工具' in line or '## 强制规则' in line:
                    in_key_section = True
                elif line.startswith('## ') and in_key_section:
                    in_key_section = False
                if in_key_section:
                    key_sections.append(line)
            return '\n'.join(key_sections) if key_sections else ''
    except Exception:
        pass
    return ''


def main():
    try:
        cwd = os.getcwd()
        project_id, auto_activated, project_info = detect_project_from_cwd(cwd)

        messages = []

        route_notice = build_main_session_route_notice()
        if route_notice:
            messages.append(route_notice)

        # 回忆项目记忆（独立 try，失败不影响后续服务启动和规则注入）
        recall = None
        if project_id:
            try:
                from memory_system import get_memory_system
                memory = get_memory_system()

                try:
                    memory.cleanup_old_memories(project_id, keep_days=3)
                except Exception:
                    pass

                recall = memory.recall_last_session(project_id)
            except Exception:
                pass  # memory_system 不可用时跳过回忆，不影响下方逻辑

            # 获取项目显示名称
            if project_info:
                display_name = f"{project_info.name} ({project_id})"
            else:
                display_name = project_id

            if auto_activated:
                activation_msg = f"🚀 已自动激活 **{display_name}** 项目的维造 / Vizo 智能协作系统"
                messages.insert(0, activation_msg)

                # 检查并启动必要服务（重启电脑后自动恢复）
                service_messages = check_and_start_services()
                messages.extend(service_messages)

                # 注入行为规则（改变默认行为），参考模板按需读取 SKILL.md
                messages.append(OPUS_BEHAVIOR_RULES)

                # 注入已安装工具清单（防遗忘）
                tools_installed = read_tools_installed()
                if tools_installed:
                    messages.append(f"## 已安装 MCP 工具清单\n{tools_installed}")

                if recall:
                    messages.append(f"📚 上次会话回忆:\n{recall[:600]}")
            else:
                if recall:
                    messages.append(f"[回忆] {display_name} 项目\n{recall[:400]}...")
                messages.append("💡 使用 `/vizo` 激活完整协同模式")
        else:
            messages.append("[提示] 使用 `/vizo` 激活智能协作模式")

        output = {"systemMessage": "\n\n".join(messages)}
        print(json.dumps(output))

    except Exception as e:
        print(json.dumps({"systemMessage": f"[Hook 警告] {e}"}))

if __name__ == '__main__':
    main()
