#!/usr/bin/env python3
"""
Vizo 智能协作系统

默认入口：
  vizo "帮小智添加定时关机功能"     # 执行任务
  vizo --resume                     # 恢复中断的任务
  vizo --chat "问个问题"            # 直接问答
  vizo --history                    # 查看任务历史
  vizo --cost                       # 查看今日花费

兼容入口：
  opus "帮小智添加定时关机功能"     # 兼容别名，推荐改用 vizo
"""

import os
import sys
import asyncio
import argparse
import logging
import json
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from lib.paths import (
    iter_task_dirs,
    task_dir as resolve_task_dir,
)
from lib.task_resource_guard import TaskAdmissionError

_DEFAULT_CLI_COMMAND = Path(sys.argv[0]).stem or "vizo"
if _DEFAULT_CLI_COMMAND in {"python", "python3", "python3.11"}:
    _DEFAULT_CLI_COMMAND = "vizo"

CLI_COMMAND = os.environ.get("OPUS_CMD_NAME", _DEFAULT_CLI_COMMAND)
CLI_BRAND_DISPLAY = os.environ.get("OPUS_BRAND_DISPLAY", "Vizo")
CLI_COMPAT_ENTRY = os.environ.get("OPUS_COMPAT_ENTRY") == "1" or CLI_COMMAND == "opus"
CLI_SYSTEM_TITLE = f"{CLI_BRAND_DISPLAY} 智能协作系统"


def _detach_session():
    """创建新进程会话，脱离父 PTY。
    确保 confirm_server 重启/PTY 销毁时，长任务进程树不被级联杀死。
    """
    try:
        os.setsid()
    except OSError:
        pass  # 已是会话领导者

# 确保能 import 同目录下的模块
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from vizo_core.orchestrator import Orchestrator

logger = logging.getLogger(__name__)


class ChineseArgumentParser(argparse.ArgumentParser):
    """自定义中文化的 ArgumentParser"""

    def __init__(self, *args, add_help=True, **kwargs):
        # 先关闭默认的帮助选项
        super().__init__(*args, add_help=False, **kwargs)

        # 手动添加中文化的帮助选项
        if add_help:
            self.add_argument(
                '-h', '--help',
                action='help',
                help='显示此帮助消息并退出'
            )

    def format_help(self):
        """覆盖帮助文本格式"""
        formatter = self._get_formatter()
        formatter.add_usage(self.usage, self._actions,
                            self._mutually_exclusive_groups, prefix='用法: ')

        if self.description:
            formatter.add_text(self.description)

        # 位置参数和选项的标题中文化
        for action_group in self._action_groups:
            if action_group.title == 'positional arguments':
                action_group.title = '位置参数'
            elif action_group.title == 'options':
                action_group.title = '选项'
            formatter.start_section(action_group.title)
            formatter.add_arguments(action_group._group_actions)
            formatter.end_section()

        if self.epilog:
            formatter.add_text(self.epilog)

        return formatter.format_help()


def parse_args():
    compat_note = ""
    if CLI_COMPAT_ENTRY:
        compat_note = """

兼容入口说明：
  当前通过 `opus` 兼容入口启动；推荐默认命令：vizo
        """

    parser = ChineseArgumentParser(
        prog=CLI_COMMAND,
        description=CLI_SYSTEM_TITLE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
示例：
  {CLI_COMMAND} "帮小智添加定时关机功能"
  {CLI_COMMAND} --chat "数据库密码是什么"
  {CLI_COMMAND} --resume
  {CLI_COMMAND} --stream           # 实时查看当前任务的子代理工作画面
  {CLI_COMMAND} --stream TASK_ID   # 查看指定任务
  {CLI_COMMAND} --replay           # 回放最近任务的工作记录
  {CLI_COMMAND} --replay TASK_ID --role architect  # 回放指定角色
  {CLI_COMMAND} --pause            # 暂停当前运行中的任务
  {CLI_COMMAND} --rollback --step architect  # 回滚到指定步骤
  {CLI_COMMAND} --terminate        # 终止任务并回滚代码
  {CLI_COMMAND} --history
  {CLI_COMMAND} --cost
{compat_note}
        """
    )
    parser.add_argument("requirement", nargs="?", help="需求描述")
    parser.add_argument("--resume", action="store_true", help="恢复中断的任务")
    parser.add_argument("--task-id", type=str, help="指定要恢复的任务 ID（可选）")
    parser.add_argument("--chat", type=str, help="直接问答（不走工作流）")
    parser.add_argument("--history", action="store_true", help="查看任务历史")
    parser.add_argument("--cost", action="store_true", help="查看今日花费")
    parser.add_argument("--task", type=str, nargs="?", const="__list__", help="查看任务（无参数列表，指定ID看详情）")
    parser.add_argument("--project", type=str, help="指定项目（覆盖默认项目）")
    parser.add_argument("--config", type=str, default="config.json", help="配置文件路径")
    parser.add_argument("--stream", type=str, nargs="?", const="__latest__", help="实时查看子代理工作画面（无参数=最新任务）")
    parser.add_argument("--replay", type=str, nargs="?", const="__latest__", help="回放已完成任务的工作记录")
    parser.add_argument("--role", type=str, help="配合 --replay 指定角色过滤")
    parser.add_argument("--pause", type=str, nargs="?", const="__latest__",
                        help="暂停执行中的任务（无参数=最新任务）")
    parser.add_argument("--rollback", type=str, nargs="?", const="__latest__",
                        help="回滚到指定步骤（格式: TASK_ID 或无参数=最新任务）")
    parser.add_argument("--terminate", type=str, nargs="?", const="__latest__",
                        help="终止任务并回滚代码（需确认）")
    parser.add_argument("--no-rollback", action="store_true",
                        help="配合 --terminate 使用，终止但保留代码不回滚")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="跳过 --terminate 确认提示（非交互式环境使用）")
    parser.add_argument("--step", type=str,
                        help="配合 --rollback 指定目标步骤")
    parser.add_argument("--feedback", type=str,
                        help="配合 --rollback 附加修正意见")
    parser.add_argument("--workflow", type=str,
                        choices=["new_feature", "bug_fix", "refactor", "embedded", "non_dev", "auto"],
                        default="auto",
                        help="指定工作流类型（默认 auto 由 AI 判定）")
    parser.add_argument("--dev", action="store_true",
                        help="跳过语义路由，直接走 Orchestrator 开发流")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细日志")
    parser.add_argument("--auto-confirm", action="store_true",
                        help="自动确认所有节点（跳过人工审批，适用于 --resume 或直接运行）")
    return parser.parse_args()


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )



def _arrow_select(options: list, title: str = "") -> int | None:
    """终端箭头键选择器，支持多行选项，返回选中索引（Ctrl+C 返回 None）

    options 中每个元素可含 \\n 表示多行。
    """
    import tty
    import termios

    if title:
        print(title)

    selected = 0
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)

    # 计算每个选项的行数和总行数
    option_lines = [opt.split("\n") for opt in options]
    total_lines = sum(len(lines) for lines in option_lines)

    def _render():
        for i, lines in enumerate(option_lines):
            is_sel = i == selected
            marker = "❯" if is_sel else " "
            style = "\033[1;36m" if is_sel else "\033[0m"
            dim = "\033[2m" if not is_sel else "\033[1;36m"
            # 第一行带 marker
            print(f"\r\033[K  {marker} {style}{lines[0]}\033[0m")
            # 后续行缩进对齐
            for sub in lines[1:]:
                print(f"\r\033[K    {dim}{sub}\033[0m")
        # 光标回到顶部
        print(f"\033[{total_lines}A", end="", flush=True)

    try:
        tty.setraw(fd)
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        _render()
        tty.setraw(fd)

        while True:
            ch = sys.stdin.read(1)
            if ch == "\x1b":  # ESC 序列
                ch2 = sys.stdin.read(1)
                if ch2 == "[":
                    ch3 = sys.stdin.read(1)
                    if ch3 == "A":  # 上
                        selected = (selected - 1) % len(options)
                    elif ch3 == "B":  # 下
                        selected = (selected + 1) % len(options)
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    _render()
                    tty.setraw(fd)
            elif ch in ("\r", "\n"):  # 回车
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                print(f"\033[{total_lines}B")
                return selected
            elif ch == "\x03":  # Ctrl+C
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                print(f"\033[{total_lines}B")
                return None
    except Exception:
        return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def watch_task(orch, task_id_or_latest):
    """[已废弃] 旧版观察模式，请使用 opus --stream"""
    print(f"⚠️  --watch 已废弃，请使用 {CLI_COMMAND} --stream")
    stream_task(orch, task_id_or_latest)


def _find_task(orch, task_id_or_latest):
    """查找任务（共用逻辑）"""
    if task_id_or_latest == "__latest__":
        tasks = orch.state.find_all_incomplete_tasks()
        if not tasks:
            # 找最新的任务（任意状态）— 按 created_at 降序
            # 收集所有有效任务，按 created_at 排序
            candidates = []
            for td in iter_task_dirs(orch.state.project_path):
                if not td.is_dir():
                    continue
                state_file = td / "state.json"
                if state_file.exists():
                    try:
                        state = json.loads(state_file.read_text(encoding="utf-8"))
                        created = state.get("created_at", "")
                        candidates.append((created, state))
                    except (json.JSONDecodeError, OSError):
                        continue

            if not candidates:
                print("没有找到任何任务")
                sys.exit(0)

            # 按 created_at 降序（ISO 格式可直接字符串排序）
            candidates.sort(key=lambda x: x[0], reverse=True)
            from vizo_core.state_manager import Task
            return Task.from_dict(candidates[0][1])
        return tasks[0]
    else:
        state_file = resolve_task_dir(
            task_id_or_latest,
            project_root=orch.state.project_path,
        ) / "state.json"
        if not state_file.exists():
            print(f"任务 {task_id_or_latest} 不存在")
            sys.exit(1)
        state = json.loads(state_file.read_text(encoding="utf-8"))
        from vizo_core.state_manager import Task
        return Task.from_dict(state)


def stream_task(orch, task_id_or_latest):
    """实时查看子代理工作画面（opus --stream）"""
    task = _find_task(orch, task_id_or_latest)

    print(f"📋 任务: {task.description[:60]}")
    print(f"🆔 ID: {task.id}")
    print(f"📊 状态: {task.status}")
    print("━" * 60)

    from vizo_core.stream_renderer import StreamViewer
    StreamViewer.run(task_dir=Path(task.dir), task_desc=task.description)


def replay_task(orch, task_id_or_latest, role_filter=None):
    """回放已完成任务的工作记录（opus --replay）"""
    task = _find_task(orch, task_id_or_latest)

    print(f"📋 回放任务: {task.description[:60]}")
    print(f"🆔 ID: {task.id}")
    print("━" * 60)

    from vizo_core.stream_renderer import StreamViewer
    StreamViewer.replay(task_dir=Path(task.dir), role_filter=role_filter)

def _is_hub_task(task_id: str) -> bool:
    """判断 task_id 是否为 AgentHub 任务"""
    return task_id is not None and task_id.startswith("hub-")


def _find_running_task(orch, task_id=None):
    """查找运行中的任务"""
    if task_id and task_id != "__latest__":
        state_file = resolve_task_dir(
            task_id,
            project_root=orch.state.project_path,
        ) / "state.json"
        if not state_file.exists():
            print(f"任务 {task_id} 不存在")
            sys.exit(1)
        from vizo_core.state_manager import Task
        task = Task.from_dict(json.loads(state_file.read_text(encoding="utf-8")))
        if task.status != "running":
            print(f"任务 {task_id} 当前状态为 {task.status}，不是运行中")
            sys.exit(1)
        return task

    # 查找最新的运行中任务
    tasks = orch.state.find_all_incomplete_tasks()
    running = [t for t in tasks if t.status == "running"]
    if not running:
        print("没有正在运行的任务")
        return None
    return running[0]


def _find_target_task(orch, task_id=None):
    """查找目标任务（运行中或已暂停/失败）"""
    if task_id and task_id != "__latest__":
        state_file = resolve_task_dir(
            task_id,
            project_root=orch.state.project_path,
        ) / "state.json"
        if not state_file.exists():
            print(f"任务 {task_id} 不存在")
            sys.exit(1)
        from vizo_core.state_manager import Task
        return Task.from_dict(json.loads(state_file.read_text(encoding="utf-8")))

    # 查找最新的未完成任务
    tasks = orch.state.find_all_incomplete_tasks()
    if not tasks:
        print("没有未完成的任务")
        sys.exit(0)
    return tasks[0]


def _select_step(task):
    """交互式选择回滚目标步骤"""
    if not task.completed_steps:
        print("该任务没有已完成的步骤，无法回滚")
        return None

    # 只展示有 checkpoint 的步骤
    available = []
    for step in task.completed_steps:
        has_cp = step in task.step_checkpoints
        label = f"{step}"
        if has_cp:
            label += f" (checkpoint: {task.step_checkpoints[step][:8]})"
        else:
            label += " (无 checkpoint)"
        available.append((step, label, has_cp))

    print("可回滚的步骤：")
    valid_indices = []
    for i, (step, label, has_cp) in enumerate(available):
        marker = f"  {i+1}. {label}"
        if not has_cp:
            marker += " [不可回滚]"
        print(marker)
        if has_cp:
            valid_indices.append(i)

    if not valid_indices:
        print("没有可回滚的步骤（所有步骤均无 checkpoint）")
        return None

    try:
        choice = input("请输入步骤编号（回车取消）: ").strip()
        if not choice:
            return None
        idx = int(choice) - 1
        if idx < 0 or idx >= len(available) or not available[idx][2]:
            print("无效选择")
            return None
        return available[idx][0]
    except (ValueError, EOFError):
        return None


def _handle_agents_command(config: dict):
    """列出所有可用 Agent 模块"""
    # 检查 --json 参数
    json_mode = "--json" in sys.argv

    try:
        from vizo_core.agent_router import AgentRouter
        router = AgentRouter(config)
        modules = router._load_available_modules()
    except Exception as e:
        if json_mode:
            print("[]")
        else:
            print(f"加载模块失败: {e}")
        return

    if json_mode:
        result = []
        for mod in modules:
            m = mod["manifest"]
            workflows = {}
            for wf_id, wf in m.get("workflows", {}).items():
                workflows[wf_id] = wf.get("name", wf_id)
            result.append({
                "id": m["id"],
                "name": m.get("name", m["id"]),
                "description": m.get("description", ""),
                "workflows": workflows,
            })
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if not modules:
            print("暂无已安装的 Agent 模块")
            print()
            print("─" * 38)
            print(f'💡 {CLI_COMMAND} new "描述" 创建自定义模块')
        else:
            print("可用 Agent 模块\n")
            for mod in modules:
                m = mod["manifest"]
                icon = m.get("icon", "🔧")
                print(f"{icon} {m['name']} ({m['id']})")
                print(f"   {m['description']}")

                wf_names = []
                for wf_id, wf in m.get("workflows", {}).items():
                    wf_names.append(wf.get("name", wf_id))
                if wf_names:
                    print(f"   工作流：{'  /  '.join(wf_names)}")
                print()

            print("─" * 38)
            print(f'用法: {CLI_COMMAND} "任务描述" [@模块ID]')
            print(f'💡 {CLI_COMMAND} new "描述" 创建自定义模块')

    # agents 命令执行后也触发 sync
    try:
        from lib.module_sync import sync_modules_to_claude_md
        sync_modules_to_claude_md(str(_PROJECT_ROOT))
    except Exception:
        pass


# ── handle_create_agent 统一入口及辅助函数 ──────────────────


def _show_spinner(message: str, stop_event):
    """在终端同行显示旋转动画"""
    import itertools
    import time
    chars = itertools.cycle(["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"])
    start = time.time()
    while not stop_event.is_set():
        elapsed = int(time.time() - start)
        suffix = ""
        if elapsed > 60:
            suffix = f"（已等待 {elapsed}s，仍在生成中，可按 Ctrl+C 取消）"
        print(f"\r{next(chars)} {message}{suffix}", end="", flush=True)
        stop_event.wait(0.1)
    print("\r\033[K", end="", flush=True)


def _render_preview(manifest: dict):
    """渲染模块预览到终端"""
    from lib.agent_creator import _flatten_steps

    print("━" * 38)
    print("📋 模块预览")
    print("─" * 38)
    print(f"  名称: {manifest['name']}")
    print(f"  ID:   {manifest['id']}")
    desc = manifest.get('description', '')
    if len(desc) > 60:
        desc = desc[:57] + "..."
    print(f"  描述: {desc}")

    # 收集所有步骤
    all_steps = []
    for wf in manifest.get("workflows", {}).values():
        all_steps.extend(_flatten_steps(wf))

    print(f"  步骤: {len(all_steps)} 步")
    print()

    icons = ["🔍", "💰", "📝", "📊", "✅"]
    for i, step in enumerate(all_steps):
        icon = icons[i % len(icons)]
        role = step.get("role", "")
        model = manifest.get("roles", {}).get(role, {}).get("model", "sonnet")
        desc = step.get("description", step.get("step", ""))
        if len(desc) > 40:
            desc = desc[:37] + "..."
        confirm = ", 需确认" if step.get("confirm") else ""
        print(f"  {i+1}. {icon} {desc}（{role}）— {model}{confirm}")

    print("━" * 38)


def _print_limit_exceeded(limit_result: dict):
    """打印模块数量超限提示"""
    print(f"✗ 已达用户自定义模块上限（{limit_result['max_limit']} 个）。")
    print()
    if limit_result["existing_modules"]:
        print("现有模块：")
        for m in limit_result["existing_modules"]:
            print(f"  · {m['id']} — {m['name']}")
        print()
    print("请删除不需要的模块目录后重试：")
    print("  rm -rf agents/_user/default/{module_id}/")


def _handle_id_conflict(module_id: str) -> str | None:
    """处理 ID 冲突，返回最终使用的 module_id 或 None（取消）"""
    import re
    from lib.agent_creator import check_id_conflict, ID_PATTERN

    conflict = check_id_conflict(module_id)
    if not conflict["conflict"]:
        return module_id

    source = conflict["conflict_source"]
    name = conflict["conflict_name"]
    print(f"\n⚠️  模块 ID {module_id} 已存在（{source}/{name}）")

    if source == "_builtin":
        print("  系统内置模块不可覆盖。")
        new_id = input("  输入新 ID（或回车取消）: ").strip()
        if not new_id:
            return None
        if not re.match(ID_PATTERN, new_id):
            print("  ID 格式不合规：只允许小写字母开头，后跟小写字母/数字/下划线，长度 2~32")
            return None
        return _handle_id_conflict(new_id)  # 递归检查新 ID
    else:
        choice = input("  [y] 覆盖  输入新 ID  [n] 取消: ").strip().lower()
        if choice == "n":
            return None
        elif choice == "y":
            return module_id
        else:
            return _handle_id_conflict(choice)  # 递归检查


def _handle_edit_manifest(manifest, roles, tmp_dir=None):
    """让用户通过 $EDITOR 编辑 manifest，返回修改后的 (manifest, roles) 或 None"""
    import subprocess
    import tempfile
    import os

    if not tmp_dir:
        tmp_dir = tempfile.mkdtemp(prefix="opus_agent_edit_")
    edit_path = Path(tmp_dir) / "manifest.json"

    edit_data = {"manifest": manifest, "roles": roles}
    edit_path.write_text(
        json.dumps(edit_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    editor = os.environ.get("EDITOR", "vi")
    try:
        subprocess.run([editor, str(edit_path)], check=True)
    except FileNotFoundError:
        print(f"编辑器 '{editor}' 不可用。")
        print(f"请手动编辑文件: {edit_path}")
        print(f"编辑完成后重新执行 {CLI_COMMAND} new")
        return None

    try:
        data = json.loads(edit_path.read_text(encoding="utf-8"))
        return data.get("manifest"), data.get("roles", roles)
    except (json.JSONDecodeError, OSError) as e:
        print(f"读取编辑后文件失败: {e}")
        return None


async def _handle_ai_create(config: dict, source: str, description: str):
    """AI 创建模式核心流程"""
    import threading
    from lib.agent_creator import (
        call_agent_creator, validate_manifest, check_module_limit,
        check_id_conflict, write_module_to_disk,
    )

    # === source="web" 快捷路径 ===
    if source == "web":
        if len(description) < 10:
            return {"action": "error", "message": "描述太简短（<10字符）"}
        limit = check_module_limit(config)
        if not limit["allowed"]:
            return {"action": "error", "message": f"已达模块上限（{limit['max_limit']}个）"}
        result = await call_agent_creator(config, description)
        if not result["success"]:
            return {"action": "error", "message": result["error"]}
        validation = validate_manifest(result["manifest"])
        if not validation["valid"]:
            return {"action": "error", "message": "; ".join(validation["errors"])}
        return {"action": "preview", "module": {
            "manifest": result["manifest"],
            "roles": result["roles"],
            "warnings": validation["warnings"],
        }}

    # === source="cli" / "route_suggest" 路径 ===

    # 描述长度检查
    if len(description) < 10:
        print("描述太简短（至少 10 个字符），请补充更多细节。")
        print(f'示例: {CLI_COMMAND} new "一个帮我分析竞品的 Agent，输入竞品 URL，输出分析报告"')
        sys.exit(1)

    # 描述截断检查
    if len(description) > 500:
        description = description[:500]
        print("⚠ 描述已截断至 500 字符")

    # 数量检查
    limit = check_module_limit(config)
    if not limit["allowed"]:
        _print_limit_exceeded(limit)
        sys.exit(1)

    # 生成循环（支持 [r] 重新生成）
    while True:
        # 启动 spinner + 调用 AI
        stop = threading.Event()
        t = threading.Thread(target=_show_spinner, args=("正在生成模块定义...", stop), daemon=True)
        t.start()
        try:
            result = await call_agent_creator(config, description)
        finally:
            stop.set()
            t.join(timeout=1)

        if not result["success"]:
            print(f"\n✗ {result['error']}")
            sys.exit(1)

        # 校验 manifest
        validation = validate_manifest(result["manifest"])
        if not validation["valid"]:
            print("\n✗ AI 生成的模块定义未通过校验：")
            for err in validation["errors"]:
                print(f"  · {err}")
            if sys.stdin.isatty():
                retry = input("\n[r] 重新生成  [n] 取消: ").strip().lower()
                if retry == "r":
                    continue
            sys.exit(1)

        # 校验通过的 warnings
        if validation.get("warnings"):
            print()
            for w in validation["warnings"]:
                print(f"  ⚠ {w}")

        # Bash 工具警告检查
        if validation.get("has_bash_warning") and sys.stdin.isatty():
            confirm_bash = input("\n确认允许 Bash 工具？[y/n]: ").strip().lower()
            if confirm_bash != "y":
                print("已取消。请修改描述，避免生成需要 Bash 的模块。")
                return

        # ID 冲突检查
        module_id = result["manifest"]["id"]
        if sys.stdin.isatty():
            final_id = _handle_id_conflict(module_id)
        else:
            conflict = check_id_conflict(module_id)
            final_id = module_id if not conflict["conflict"] else None
        if final_id is None:
            print("已取消。")
            return

        manifest = result["manifest"]
        roles = result["roles"]

        # 展示预览
        print()
        _render_preview(manifest)

        # 用户选择
        if sys.stdin.isatty():
            print("\n  [y] 保存  [e] 编辑  [r] 重新生成  [n] 取消")
            choice = input("  请选择: ").strip().lower()
        else:
            # 非 TTY 直接保存
            choice = "y"

        if choice == "y":
            write_module_to_disk(manifest, roles, module_id=final_id)
            # 触发 CLAUDE.md 模块同步
            try:
                from lib.module_sync import sync_modules_to_claude_md
                sync_modules_to_claude_md(str(_PROJECT_ROOT))
            except Exception:
                pass
            mid = final_id or manifest["id"]
            print(f"\n✅ 模块已保存到 agents/_user/default/{mid}/")
            print(f'   立即使用: {CLI_COMMAND} "示例任务" @{mid}')
            return
        elif choice == "e":
            edited = _handle_edit_manifest(manifest, roles)
            if edited is None:
                continue
            manifest, roles = edited
            # 重新校验
            validation = validate_manifest(manifest)
            if not validation["valid"]:
                print("编辑后的 manifest 校验失败：")
                for err in validation["errors"]:
                    print(f"  · {err}")
                continue
            # 编辑后直接回到预览确认
            result["manifest"] = manifest
            result["roles"] = roles
            continue
        elif choice == "r":
            continue
        else:
            print("已取消")
            return


def _handle_copy_mode(config: dict, from_module: str):
    """模板复制模式"""
    import re
    from lib.agent_creator import copy_module, check_module_limit, check_id_conflict, ID_PATTERN

    limit = check_module_limit(config)
    if not limit["allowed"]:
        _print_limit_exceeded(limit)
        sys.exit(1)

    default_id = f"{from_module}_copy"
    new_id = input(f"新模块 ID（默认: {default_id}）: ").strip() or default_id

    # ID 格式校验
    if not re.match(ID_PATTERN, new_id):
        print("✗ id 格式不合规：只允许小写字母开头，后跟小写字母/数字/下划线，长度 2~32")
        sys.exit(1)

    # 冲突检查（复用 _handle_id_conflict 交互）
    if sys.stdin.isatty():
        final_id = _handle_id_conflict(new_id)
    else:
        conflict = check_id_conflict(new_id)
        final_id = new_id if not conflict["conflict"] else None
    if final_id is None:
        print("已取消。")
        sys.exit(1)

    # 冲突覆盖：先删旧目录
    if final_id == new_id:
        conflict = check_id_conflict(final_id)
        if conflict["conflict"] and conflict["conflict_source"] == "_user":
            import shutil
            base = _PROJECT_ROOT / "agents" / "_user" / "default" / final_id
            if base.exists():
                shutil.rmtree(base)

    try:
        dest = copy_module(from_module, final_id)
        print(f"\n✅ 已从 {from_module} 复制到 {dest.relative_to(_PROJECT_ROOT)}/")
        print("\n📝 建议修改以下内容：")
        print("  - manifest.json → name, description（让路由器能精准匹配）")
        print("  - roles/*.md → 根据新场景调整角色指令")
        print(f"\n修改完成后执行: {CLI_COMMAND} agents（确认模块已加载）")
    except FileNotFoundError as e:
        print(f"✗ {e}")
        print(f"\n正确用法: {CLI_COMMAND} new --from {{模块ID}}")
        sys.exit(1)
    except ValueError as e:
        print(f"✗ {e}")
        sys.exit(1)


def _handle_skeleton_mode(config: dict):
    """空白模板模式"""
    import re
    from lib.agent_creator import generate_skeleton, check_module_limit, check_id_conflict, ID_PATTERN

    limit = check_module_limit(config)
    if not limit["allowed"]:
        _print_limit_exceeded(limit)
        sys.exit(1)

    module_id = input("模块 ID（默认: my_agent）: ").strip() or "my_agent"

    # ID 格式校验
    if not re.match(ID_PATTERN, module_id):
        print("✗ id 格式不合规：只允许小写字母开头，后跟小写字母/数字/下划线，长度 2~32")
        sys.exit(1)

    # 冲突检查
    if sys.stdin.isatty():
        final_id = _handle_id_conflict(module_id)
    else:
        conflict = check_id_conflict(module_id)
        final_id = module_id if not conflict["conflict"] else None
    if final_id is None:
        print("已取消。")
        sys.exit(1)

    # 冲突覆盖：先删旧目录
    conflict = check_id_conflict(final_id)
    if conflict["conflict"] and conflict["conflict_source"] == "_user":
        import shutil
        base = _PROJECT_ROOT / "agents" / "_user" / "default" / final_id
        if base.exists():
            shutil.rmtree(base)

    dest = generate_skeleton(final_id)
    print(f"\n✅ 骨架模块已生成: {dest.relative_to(_PROJECT_ROOT)}/")
    print("   manifest.json — 模块定义（每个字段有说明）")
    print("   roles/my_role.md — 示例角色模板")
    print("   README.md — 字段说明文档")
    print(f"\n修改完成后执行: {CLI_COMMAND} agents（确认模块已加载）")


async def _handle_interactive_menu(config: dict):
    """交互式向导菜单"""
    if not sys.stdin.isatty():
        print(f"错误: {CLI_COMMAND} new 需要交互式终端（TTY）。", file=sys.stderr)
        print(f"  AI 创建模式: {CLI_COMMAND} new \"描述\"", file=sys.stderr)
        print(f"  模板复制模式: {CLI_COMMAND} new --from {{模块ID}}", file=sys.stderr)
        sys.exit(1)

    print("🤖 创建新 Agent 模块")
    print("─" * 38)
    print()
    print("创建方式：")
    print("  [1] AI 帮我创建（推荐）— 描述需求，AI 自动生成")
    print("  [2] 从已有模块复制    — 基于现有模块修改")
    print("  [3] 空白模板          — 生成骨架文件，自行编辑")
    print()
    choice = input("请选择 [1/2/3]: ").strip()

    if choice == "1":
        desc = input("\n请描述你想创建的 Agent（功能、输入、输出）：\n> ").strip()
        await _handle_ai_create(config, "cli", desc)
    elif choice == "2":
        import json as _json
        from lib.agent_creator import _get_agents_base_dir
        base = _get_agents_base_dir()
        modules = []
        for d in ["_builtin", "_user"]:
            sd = base / d
            if not sd.exists():
                continue
            for mf in sd.rglob("manifest.json"):
                try:
                    m = _json.loads(mf.read_text(encoding="utf-8"))
                    modules.append((mf.parent.name, m.get("name", ""), d))
                except Exception:
                    pass
        if not modules:
            print("\n当前没有可用模块可供复制。")
            return
        print("\n可用模块：")
        for mid, mname, src in sorted(modules):
            tag = "内置" if src == "_builtin" else "自建"
            print(f"  · {mid} — {mname}（{tag}）")
        print()
        from_id = input("输入源模块 ID: ").strip()
        if not from_id:
            print("已取消。")
            return
        _handle_copy_mode(config, from_id)
    elif choice == "3":
        _handle_skeleton_mode(config)
    else:
        print("无效选择")


async def handle_create_agent(
    config: dict,
    source: str = "cli",
    description: str = "",
    from_module: str = "",
) -> dict | None:
    """所有创建入口的统一函数。

    Args:
        config: 系统配置。
        source: 来源 "cli" | "web" | "route_suggest"。
        description: 用户描述（AI 创建模式）。
        from_module: 源模块 ID（模板复制模式）。

    Returns:
        source="cli" 返回 None（直接 TTY 交互）。
        source="web" 返回 dict。
    """
    if from_module:
        if source == "web":
            return {"action": "error", "message": "Web 端暂不支持模板复制模式"}
        _handle_copy_mode(config, from_module)
        return None

    if description:
        return await _handle_ai_create(config, source, description)

    # 两者都空 → 交互式菜单
    if source == "web":
        return {"action": "error", "message": "请提供 Agent 描述"}
    await _handle_interactive_menu(config)
    return None


async def _handle_project_new(config: dict, project_name: str,
                                module: str = None, description: str = ""):
    """创建非开发项目"""
    import re as _re
    from lib.project_bootstrap import bootstrap_serena_project

    # ASCII-only slug：中文等 Unicode 字符一律替换为 '-'
    slug = _re.sub(r'[^a-z0-9\-]', '-', project_name.lower()).strip('-')
    slug = _re.sub(r'-+', '-', slug)  # 去除连续 '-'
    if not slug or len(slug) < 3:
        # 纯中文或极短名称：用时间戳保证唯一
        import hashlib
        short_hash = hashlib.md5(project_name.encode()).hexdigest()[:6]
        slug = f"{slug}-{short_hash}" if slug else f"project-{short_hash}"
        slug = slug.strip('-')

    # 工作区根目录：优先使用 config 中的配置，默认 ~/.vizo/workspaces
    workspaces_root = Path(config.get("workspaces_root", str(Path.home() / ".vizo" / "workspaces")))
    workspaces_root.mkdir(parents=True, exist_ok=True)
    project_path = workspaces_root / slug

    if project_path.exists():
        print(f"✗ 目录已存在: {project_path}")
        return

    print(f"✨ 正在创建项目：{project_name}")
    print(f"   路径：{project_path}")

    # 创建目录结构
    (project_path / ".serena" / "memories").mkdir(parents=True)
    (project_path / ".vizo" / "tasks").mkdir(parents=True)

    print("   初始化知识库...")
    bootstrap_serena_project(
        project_path,
        project_name=slug,
        display_name=project_name,
        description=description,
        project_type="hub",
    )

    # 注册到 config.json
    print("   注册到配置...")
    config_path = _PROJECT_ROOT / "config.json"
    config_data = json.loads(config_path.read_text(encoding="utf-8"))

    config_data.setdefault("projects", {})[slug] = {
        "path": str(project_path),
        "type": "hub",
        "default_module": module or None,
        "description": description or "",
        "serena_project": slug,
    }

    config_path.write_text(
        json.dumps(config_data, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n✅ 项目已创建！")
    print("   后续任务将在此项目下运行")
    print("   项目知识：.serena/memories/project-overview.md")


def _check_subcommands():
    """检查 sys.argv 中是否有 AgentHub 子命令（在 argparse 之前处理，避免冲突）"""
    if len(sys.argv) < 2:
        return None

    first_arg = sys.argv[1]

    # opus agents
    if first_arg == "agents":
        return "agents"

    # opus project new <name> [--module X] [--description Y]
    if first_arg == "project" and len(sys.argv) >= 3 and sys.argv[2] == "new":
        return "project_new"

    # opus new（Phase 3 占位）
    if first_arg == "new":
        return "new"

    return None


def _detect_project_from_cwd(config_arg: str = None) -> str:
    """通过 CWD 匹配 config.json 中注册的项目（最长路径前缀优先）。
    无匹配时返回 None，由调用方决定后续行为。"""
    try:
        cfg_path = Path(config_arg) if config_arg else _PROJECT_ROOT / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    cwd = Path.cwd().resolve()
    best_match, best_len = None, 0
    for proj_name, proj_info in (cfg.get("projects") or {}).items():
        proj_path_str = proj_info.get("path", "")
        if not proj_path_str:
            continue
        proj_path = Path(proj_path_str).resolve()
        try:
            cwd.relative_to(proj_path)
            if len(str(proj_path)) > best_len:
                best_match, best_len = proj_name, len(str(proj_path))
        except ValueError:
            continue
    return best_match


def main():
    # 模块信息同步到 CLAUDE.md（hash 一致时 ~10ms）
    try:
        from lib.module_sync import sync_modules_to_claude_md
        sync_modules_to_claude_md(str(_PROJECT_ROOT))
    except Exception:
        pass

    # 优先处理 AgentHub 子命令（避免与 argparse positional argument 冲突）
    subcmd = _check_subcommands()
    if subcmd:
        setup_logging(False)
        try:
            from lib.config_loader import load_config
            config = load_config()
        except Exception:
            config = json.loads((_PROJECT_ROOT / "config.json").read_text("utf-8"))

        if subcmd == "agents":
            _handle_agents_command(config)
            return
        elif subcmd == "project_new":
            # 解析 project new 参数
            pn_args = sys.argv[3:]  # 跳过 "opus project new"
            project_name = pn_args[0] if pn_args else ""
            if not project_name or project_name.startswith("-"):
                print(f"用法: {CLI_COMMAND} project new <项目名称> [--module <模块ID>] [--description <描述>]")
                return
            module_val = ""
            desc_val = ""
            i = 1
            while i < len(pn_args):
                if pn_args[i] == "--module" and i + 1 < len(pn_args):
                    module_val = pn_args[i + 1]
                    i += 2
                elif pn_args[i] == "--description" and i + 1 < len(pn_args):
                    desc_val = pn_args[i + 1]
                    i += 2
                else:
                    i += 1
            asyncio.run(_handle_project_new(config, project_name, module_val, desc_val))
            return
        elif subcmd == "new":
            # 解析 opus new 的参数
            new_args = sys.argv[2:]  # 跳过 "opus new"
            description = ""
            from_module = ""

            i = 0
            while i < len(new_args):
                if new_args[i] == "--from" and i + 1 < len(new_args):
                    from_module = new_args[i + 1]
                    i += 2
                elif not new_args[i].startswith("-"):
                    description = new_args[i]
                    i += 1
                else:
                    i += 1

            asyncio.run(handle_create_agent(
                config, source="cli",
                description=description,
                from_module=from_module,
            ))
            return

    args = parse_args()
    setup_logging(args.verbose)

    # 非 TTY 输出提示
    if not sys.stdout.isatty() and args.requirement:
        print(f"提示：{CLI_COMMAND} 建议以后台模式运行，避免管道信号中断任务",
              file=sys.stderr)

    # 没有任何参数时显示帮助
    if not args.requirement and not args.resume and not args.chat \
       and not args.history and not args.cost \
       and args.task is None and args.stream is None and args.replay is None \
       and args.pause is None and args.rollback is None and args.terminate is None:
        print(CLI_SYSTEM_TITLE)
        print(f'用法: {CLI_COMMAND} "你的需求" 或 {CLI_COMMAND} --help 查看更多选项')
        sys.exit(0)

    try:
        # 自动检测当前项目（如果未指定）
        if not args.project:
            args.project = _detect_project_from_cwd(args.config)

        orch = Orchestrator(config_path=args.config, project_override=args.project)

        # 调试：打印当前项目
        current_project = orch.config.get('default_project', '')
        if args.verbose and current_project:
            logging.getLogger(__name__).debug(f"当前项目: {current_project}")

        if args.auto_confirm:
            if not args.resume:
                print("警告: --auto-confirm 仅在 --resume 时有效，已忽略")
            else:
                orch.ui.auto_confirm = True
        elif args.resume:
            # --resume 默认启用 auto_confirm（跳过非关键步骤的确认）
            # 关键步骤（如需求分析、PRD）通过 force=True 仍会要求确认
            orch.ui.auto_confirm = True

        if args.pause is not None:
            pause_id = args.pause if args.pause != "__latest__" else None
            if pause_id and _is_hub_task(pause_id):
                from lib.control_signals import write_signal
                write_signal(pause_id, "pause", source="cli")
                print(f"已发送暂停信号 → 任务 {pause_id}")
                print("当前步骤完成后任务将自动暂停")
            else:
                task = _find_running_task(orch, args.pause)
                if task:
                    from lib.control_signals import write_signal, write_audit_log
                    write_signal(task.id, "pause", source="cli")
                    write_audit_log(task.dir, "pause_requested", source="cli",
                                    task_id=task.id, detail="CLI 发送暂停信号")
                    print(f"已发送暂停信号 → 任务 {task.id}")
                    print("当前 Agent 完成后任务将自动暂停")

        elif args.rollback is not None:
            rollback_id = args.rollback if args.rollback != "__latest__" else None

            if rollback_id and _is_hub_task(rollback_id):
                # hub 任务回退：必须指定 --step
                if not args.step:
                    print("hub 任务回退需要指定步骤名：")
                    print(f"  {CLI_COMMAND} --rollback {rollback_id} --step <step_name>")
                    sys.exit(1)
                from vizo_core.agent_hub import AgentHub
                hub = AgentHub(orch.config)
                try:
                    hub.rollback_to_step(rollback_id, args.step)
                except FileNotFoundError as e:
                    print(str(e))
                    sys.exit(1)
                except ValueError as e:
                    print(str(e))
                    sys.exit(1)
            else:
                task = _find_target_task(orch, args.rollback)
                target_step = args.step
                if not target_step:
                    target_step = _select_step(task)
                if target_step:
                    from lib.control_signals import write_signal, write_audit_log
                    work_dir = orch.agent._get_project_path(
                        task.project or orch.config.get("default_project", "")
                    )
                    if task.status == "running":
                        write_signal(task.id, "rollback",
                                     target_step=target_step,
                                     feedback=args.feedback or "",
                                     source="cli")
                        write_audit_log(task.dir, "rollback_requested", source="cli",
                                        task_id=task.id, target_step=target_step,
                                        detail=f"CLI 发送回滚信号 → {target_step}")
                        print(f"已发送回滚信号 → 步骤 {target_step}")
                        print("当前 Agent 完成后将自动回滚")
                    else:
                        # 任务已暂停/失败 → 直接执行回滚
                        orch.state.rollback_to_step(task, target_step, work_dir)
                        if args.feedback:
                            injection_file = task.dir / "user_injection.md"
                            injection_file.write_text(
                                f"# 用户回滚修正意见\n\n{args.feedback}\n",
                                encoding="utf-8",
                            )
                        write_audit_log(task.dir, "rollback", source="cli",
                                        task_id=task.id, target_step=target_step,
                                        detail=f"直接回滚到步骤 {target_step}")
                        print(f"已回滚到步骤 {target_step}")
                        print(f"使用 {CLI_COMMAND} --resume 可继续执行")

        elif args.terminate is not None:
            terminate_id = args.terminate if args.terminate != "__latest__" else None

            if terminate_id and _is_hub_task(terminate_id):
                # hub 任务简化终止（无 git 操作）
                from vizo_core.agent_hub import AgentHub
                hub = AgentHub(orch.config)
                try:
                    task = hub._load_task_for_resume(terminate_id)
                except FileNotFoundError:
                    print(f"任务 {terminate_id} 不存在")
                    sys.exit(1)
                except RuntimeError as e:
                    print(str(e))
                    sys.exit(1)

                print(f"即将终止 hub 任务: {task.get('description', '')[:60]}")
                completed = task.get("completed_steps", [])
                print(f"已完成步骤: {', '.join(completed) or '无'}")
                prompt = "确认终止？[y/N] "
                if args.yes:
                    confirm = "y"
                    print(prompt + "y（--yes 自动确认）")
                else:
                    try:
                        confirm = input(prompt).strip().lower()
                    except EOFError:
                        print("\n非交互式环境，请使用 -y/--yes 参数，例如：")
                        print(f"  {CLI_COMMAND} --terminate {terminate_id} --yes")
                        sys.exit(1)

                if confirm == "y":
                    if task.get("status") == "running":
                        from lib.control_signals import write_signal
                        write_signal(terminate_id, "terminate", source="cli")
                        print("已发送终止信号，当前步骤完成后将自动终止")
                    else:
                        from datetime import datetime
                        task["status"] = "terminated"
                        task["completed_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                        hub._save_task_state(task)
                        hub._write_hub_history(task)
                        print("任务已终止，已完成步骤的产出保留在 outputs/ 目录")
                else:
                    print("已取消")
            else:
                task = _find_target_task(orch, args.terminate)
                rollback = not args.no_rollback
                # 终止确认
                print(f"即将终止任务: {task.description[:60]}")
                print(f"已完成步骤: {', '.join(task.completed_steps) or '无'}")
                cost = orch.state.get_task_cost(task)
                print(f"已消耗: ${cost['total_usd']:.2f}")
                prompt = "确认终止并回滚代码？[y/N] " if rollback else "确认终止（保留代码）？[y/N] "
                if args.yes:
                    confirm = "y"
                    print(prompt + "y（--yes 自动确认）")
                else:
                    try:
                        confirm = input(prompt).strip().lower()
                    except EOFError:
                        print("\n非交互式环境，请使用 -y/--yes 参数跳过确认，例如：")
                        print(f"  {CLI_COMMAND} --terminate {task.id} --yes")
                        sys.exit(1)
                if confirm == "y":
                    from lib.control_signals import write_signal, write_audit_log
                    work_dir = orch.agent._get_project_path(
                        task.project or orch.config.get("default_project", "")
                    )
                    if task.status == "running":
                        write_signal(task.id, "terminate", source="cli", rollback=rollback)
                        write_audit_log(task.dir, "terminate_requested", source="cli",
                                        task_id=task.id, detail=f"CLI 发送终止信号 (rollback={rollback})")
                        if rollback:
                            print("已发送终止信号，当前 Agent 完成后将自动终止并回滚")
                        else:
                            print("已发送终止信号，当前 Agent 完成后将终止（保留代码）")
                    else:
                        asyncio.run(orch.state.terminate_task(task, work_dir, rollback=rollback))
                        write_audit_log(task.dir, "terminate", source="cli",
                                        task_id=task.id, detail=f"直接终止任务 (rollback={rollback})")
                        if rollback:
                            print("任务已终止，代码已回滚")
                        else:
                            print("任务已终止，代码已保留")
                else:
                    print("已取消")

        elif args.resume:
            # 如果没有指定 task_id，先获取列表让用户选择
            if not args.task_id:
                tasks = orch.state.find_all_incomplete_tasks()
                if not tasks:
                    print("没有未完成的任务")
                    sys.exit(0)

                # 限制显示到 10 个任务
                display_tasks = tasks[:10]
                total_count = len(tasks)

                # 展示标题
                current_project = orch.config.get('default_project', '')
                title = f"📋 {current_project} - " if current_project else "📋 "
                print(f"{title}未完成的任务（{total_count} 个）")
                print()

                if total_count > 10:
                    print(f"  显示最近 10 个，还有 {total_count - 10} 个更早的任务")
                    print()

                # 构建多行选项
                status_labels = {
                    "paused": "⏸ 暂停", "failed": "❌ 失败", "running": "⚡ 中断"
                }

                def _extract_title(desc: str) -> str:
                    """从描述中提取短标题"""
                    s = desc.strip().strip('"').strip('\u201c\u201d')
                    # 去掉常见前缀
                    for prefix in ("请", "帮我", "帮", "麻烦"):
                        if s.startswith(prefix):
                            s = s[len(prefix):]
                    # 取第一句
                    for sep in ('。', '，', '；', '、', '\n', ','):
                        if sep in s:
                            s = s[:s.index(sep)]
                            break
                    if len(s) > 25:
                        s = s[:25] + "…"
                    return s

                options = []
                for t in display_tasks:
                    title = _extract_title(t.description)
                    desc = t.description[:60]
                    status = status_labels.get(t.status, "⚡ 中断")
                    progress = orch._format_current_progress(t)
                    created = t.created_at.strftime("%m-%d %H:%M") if t.created_at else ""
                    stopped = t.completed_at.strftime("%m-%d %H:%M") if t.completed_at else ""
                    time_info = f"创建: {created}"
                    if stopped:
                        time_info += f"  停止: {stopped}"
                    line1 = f"【{title}】{desc}"
                    line2 = f"{status} │ {progress} │ {time_info}"
                    options.append(f"{line1}\n{line2}")
                options.append("放弃选择")

                idx = _arrow_select(options, title="👉 上下键选择任务，回车确认：")
                if idx is None or idx == len(display_tasks):
                    print("已取消")
                    sys.exit(0)

                args.task_id = display_tasks[idx].id

            _detach_session()
            if _is_hub_task(args.task_id):
                from vizo_core.agent_hub import AgentHub
                hub = AgentHub(orch.config)
                asyncio.run(hub.execute(resume=True, task_id=args.task_id))
            else:
                asyncio.run(orch.resume_last_task(args.task_id))
        elif args.chat:
            asyncio.run(orch.quick_chat(args.chat))
        elif args.task is not None:
            if args.task == "__list__":
                orch.show_task_list()
            else:
                orch.show_task_detail(args.task)
        elif args.history:
            orch.show_history()
        elif args.cost:
            orch.show_cost_today()
        elif args.stream is not None:
            stream_task(orch, args.stream)
        elif args.replay is not None:
            replay_task(orch, args.replay, args.role)
        else:
            # AgentHub 路由：先判断是否路由到 AgentHub，否则走 Orchestrator
            # 跳过路由的条件：--dev 显式跳过，或 --workflow 显式指定了工作流类型（非 auto）
            _routed = False
            _skip_route = args.dev or (getattr(args, 'workflow', 'auto') != 'auto')
            if not _skip_route:
                try:
                    from vizo_core.agent_router import AgentRouter
                    router = AgentRouter(orch.config)

                    # 获取当前项目配置
                    current_project = orch.config.get("default_project", "")
                    project_config = orch.config.get("projects", {}).get(current_project, {})

                    route_result = asyncio.run(router.route(args.requirement, project_config))

                    if route_result["target"] == "agent_hub":
                        from vizo_core.agent_hub import AgentHub
                        hub = AgentHub(orch.config)

                        reason = route_result.get("reason", "")
                        if reason and reason not in ("显式指定", "项目推断"):
                            print(f"🔍 语义路由 → {route_result['module_id']}")
                            print(f"   理由：{reason}")

                        _detach_session()
                        asyncio.run(hub.execute(
                            module_id=route_result["module_id"],
                            user_request=route_result.get("request", args.requirement),
                            workflow_id=route_result.get("workflow_id"),
                            project_config=project_config,
                        ))
                        _routed = True

                    elif route_result["target"] == "error":
                        error = route_result.get("error", "")
                        if error == "module_not_found":
                            mid = route_result["module_id"]
                            available = route_result.get("available", [])
                            print(f"✗ 未找到模块：{mid}")
                            if available:
                                print("\n可用模块：")
                                for m in available:
                                    print(f"  · {m}")
                            print(f"\n提示：使用 {CLI_COMMAND} agents 查看完整列表")
                            _routed = True
                        elif error == "no_modules":
                            if not sys.stdin.isatty():
                                # 非 TTY：不展示交互菜单，直接提示退出
                                print("⚠️  没有找到匹配的 Agent 模块处理此请求。")
                                print(f'提示：使用 {CLI_COMMAND} "任务" @模块ID 显式指定，或 {CLI_COMMAND} "任务" --dev 走开发流')
                                _routed = True
                            else:
                                # TTY：展示三选项菜单
                                print()
                                print("⚠️  没有找到匹配的 Agent 模块处理此请求。")
                                print()
                                print("   [1] 作为开发任务执行（适合编码类请求）")
                                print("   [2] 创建新的 Agent 模块来处理此类任务")
                                print("   [3] 取消")
                                print()
                                try:
                                    choice = input("请选择 [1/2/3]: ").strip()
                                except (EOFError, KeyboardInterrupt):
                                    choice = "3"

                                if choice == "1":
                                    # 走 Orchestrator 开发流：不标记 routed，后续 orch.run() 执行
                                    _routed = False
                                elif choice == "2":
                                    # 带入原始请求描述，调用创建流程
                                    asyncio.run(handle_create_agent(
                                        config=orch.config,
                                        source="cli",
                                        description=args.requirement,
                                    ))
                                    _routed = True
                                else:
                                    # 取消
                                    _routed = True
                        elif error in ("timeout", "route_error"):
                            print(f"⚠️ {route_result.get('reason', '路由错误')}")
                            _routed = True
                        elif error in ("api_unreachable", "api_call_failed"):
                            print("⚠️ 语义路由 API 不可用，请判断任务类型后重新执行：")
                            print(f"   原因：{route_result.get('reason', '未知')}\n")
                            try:
                                modules = router._load_available_modules()
                                if modules:
                                    print("已安装的 Agent 模块：")
                                    for mod in modules:
                                        m = mod["manifest"]
                                        icon = m.get("icon", "🔧")
                                        desc = m.get("description", "")
                                        if len(desc) > 60:
                                            desc = desc[:57] + "..."
                                        print(f"  {icon} {m['id']} — {m['name']}：{desc}")
                                    print(f'\n  如匹配以上模块 → {CLI_COMMAND} "任务" @模块ID')
                                print(f'  如为开发任务   → {CLI_COMMAND} "任务" --dev（跳过路由，直接走开发流）')
                            except Exception:
                                pass
                            _routed = True

                    # target == "orchestrator" → _routed 仍为 False，走下面的 Orchestrator 逻辑

                except ImportError:
                    logger.warning("agent_router 模块未安装，跳过路由")
                except Exception as e:
                    logger.warning(f"路由逻辑异常: {e}，回退到 Orchestrator")

            if not _routed:
                _detach_session()
                asyncio.run(orch.run(args.requirement, workflow=getattr(args, 'workflow', 'auto')))

    except BrokenPipeError:
        # 管道关闭（如 | head），静默退出
        try:
            sys.stdout.close()
        except Exception:
            pass
        try:
            sys.stderr.close()
        except Exception:
            pass
        sys.exit(0)
    except KeyboardInterrupt:
        print("\n用户中断")
        sys.exit(1)
    except TaskAdmissionError as e:
        print(f"无法创建任务：{e}")
        sys.exit(2)
    except FileNotFoundError as e:
        print(f"文件未找到：{e}")
        sys.exit(1)
    except Exception as e:
        logging.getLogger(__name__).error(f"未预期的错误：{e}", exc_info=True)
        print(f"出错了：{e}")
        sys.exit(1)


if __name__ == "__main__":
    import signal
    # 忽略 SIGPIPE 信号（防止父进程 PTY 关闭后管道断裂杀死进程）
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    # 忽略 SIGHUP 信号（防止 PTY 关闭时编排器被杀）
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    main()
