"""
StreamRenderer — AI 员工工作过程实时渲染引擎

将 claude -p --output-format stream-json 的 NDJSON 事件流
转换为人类可读的格式化文本，输出到终端和日志文件。
"""

import time
import shutil
import unicodedata
from pathlib import Path
from dataclasses import dataclass, field
from collections import deque

# ─── 缓冲行数据类 ───

@dataclass
class BufferLine:
    """分屏模式下的缓冲行"""
    text: str         # 纯文本（用于日志）
    rich_obj: object  # Rich Text 对象或 markup 字符串
    style: str = ""
    is_markup: bool = False


# ─── 角色名映射 ───

ROLE_DISPLAY = {
    "requirement_analyst": "需求分析师",
    "product_manager": "产品经理",
    "project_manager": "项目经理",
    "architect": "架构师",
    "backend_developer": "后端开发",
    "frontend_developer": "前端开发",
    "embedded_engineer": "嵌入式开发",
    "integration_engineer": "集成工程师",
    "qa_engineer": "测试工程师",
    "fix_engineer": "修复工程师",
    "devops_engineer": "运维部署",
    "knowledge_engineer": "知识工程师",
    "knowledge_admin": "知识管理员",
    "code_explorer": "代码探索",
    "assistant": "助手",
    "merge_resolver": "冲突解决",
    "handoff_extractor": "交接提取",
    "manual_updater": "手册更新",
}

MODEL_SHORT = {
    "claude-opus-4-7": "opus",
    "claude-opus-4-6": "opus",
    "claude-sonnet-4-7": "sonnet",
    "claude-sonnet-4-6": "sonnet",
    "claude-sonnet-4-5-20250929": "sonnet",
    "claude-haiku-4-7": "haiku",
    "claude-haiku-4-5": "haiku",
    "claude-haiku-4-5-20251001": "haiku",
}


# ─── 辅助函数 ───

def _tail(s: str, n: int) -> str:
    """取字符串最后 n 个字符"""
    return s[-n:] if len(s) > n else s


def _head(s: str, n: int) -> str:
    """取字符串前 n 个字符"""
    return s[:n] if len(s) > n else s


def _line_range(inp: dict) -> str:
    """从 Read 工具参数提取行范围"""
    offset = inp.get("offset")
    limit = inp.get("limit")
    if offset and limit:
        return f"行 {offset}-{offset + limit}"
    elif offset:
        return f"从行 {offset}"
    elif limit:
        return f"前 {limit} 行"
    return ""


def _line_range_serena(inp: dict) -> str:
    """从 Serena read_file 参数提取行范围"""
    start = inp.get("start_line")
    end = inp.get("end_line")
    if start is not None and end is not None:
        return f"行 {start}-{end}"
    elif start is not None:
        return f"从行 {start}"
    return ""


def _fmt_duration(seconds: float) -> str:
    """格式化耗时"""
    if seconds >= 60:
        return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"
    return f"{int(seconds)}s"


def render_task_summary(task_dir: Path):
    """任务完成后渲染总结表格（供 StreamViewer 使用）"""
    import json as _json
    from rich.console import Console
    from rich.table import Table

    cost_file = task_dir / "cost.json"
    state_file = task_dir / "state.json"

    # 读取 cost.json
    if not cost_file.exists():
        print("（无执行记录）")
        return
    try:
        costs = _json.loads(cost_file.read_text(encoding="utf-8"))
    except (_json.JSONDecodeError, OSError):
        print("（费用数据损坏）")
        return
    if not costs:
        print("（无执行记录）")
        return

    # 读取 state.json 获取任务元信息
    task_desc = ""
    task_id = ""
    task_status = ""
    if state_file.exists():
        try:
            state = _json.loads(state_file.read_text(encoding="utf-8"))
            task_desc = state.get("description", "")
            task_id = state.get("task_id", "")
            task_status = state.get("status", "")
        except Exception:
            pass

    # 构建标题
    short_id = task_id[-4:] if task_id else ""
    desc_line = task_desc.split('\n')[0].strip()[:40] if task_desc else ""
    if desc_line and short_id:
        title = f"任务总结 | {short_id} | {desc_line}"
    else:
        title = "任务执行总结"

    console = Console()
    table = Table(title=title, show_lines=False, title_style="bold cyan",
                  border_style="dim", pad_edge=False, expand=False)
    table.add_column("#", style="dim", width=3, justify="right")
    table.add_column("角色", style="cyan", no_wrap=True)
    table.add_column("模型", style="dim", no_wrap=True)
    table.add_column("状态", no_wrap=True)
    table.add_column("启动", no_wrap=True)
    table.add_column("耗时", justify="right", no_wrap=True)
    table.add_column("费用", justify="right", no_wrap=True)
    table.add_column("产出文档", style="dim", no_wrap=True)

    total_cost = 0.0
    total_duration = 0.0

    for i, c in enumerate(costs, 1):
        role = c.get("role", "unknown")
        model = c.get("model", "-")
        cost_usd = c.get("cost_usd", 0)
        duration = c.get("duration", 0)
        timestamp = c.get("timestamp", "")
        output_file = c.get("output_file", "")
        preview_url = c.get("preview_url", "")

        # 角色中文名
        role_name = ROLE_DISPLAY.get(role, role)

        # 状态（cost.json 中有记录说明 Agent 正常完成）
        status_str = "[green]完成[/green]"

        # 启动时间：timestamp 是写入时间（约等于结束时间），减去 duration 推算
        start_str = timestamp.split("T")[1] if "T" in timestamp else "-"
        if duration > 0 and "T" in timestamp:
            try:
                from datetime import datetime, timedelta
                end_dt = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S")
                start_dt = end_dt - timedelta(seconds=duration)
                start_str = start_dt.strftime("%H:%M:%S")
            except Exception:
                pass

        # 耗时
        elapsed_str = _fmt_duration(duration) if duration > 0 else "-"

        # 费用
        cost_str = f"${cost_usd:.2f}" if cost_usd > 0 else "-"

        # 产出文档
        if output_file:
            doc_str = output_file
        else:
            doc_str = "-"

        total_cost += cost_usd
        total_duration += duration

        table.add_row(
            str(i), role_name, model, status_str,
            start_str, elapsed_str, cost_str, doc_str,
        )

    # 合计行
    table.add_row(
        "", "[bold]合计[/bold]", "", "",
        "", f"[bold]{_fmt_duration(total_duration)}[/bold]",
        f"[bold yellow]${total_cost:.2f}[/bold yellow]", "",
    )

    # 渲染
    console.print()
    console.print(table)

    # 任务最终状态
    if task_status:
        status_display = {
            "completed": "[green]已完成[/green]",
            "failed": "[red]已失败[/red]",
            "cancelled": "已取消",
            "rolled_back": "已回滚",
        }
        console.print(f"  任务状态: {status_display.get(task_status, task_status)}")
    console.print()


def _visual_width(s: str) -> int:
    """计算字符串在终端中的实际显示宽度（中文/emoji 占 2 列）"""
    w = 0
    for ch in s:
        eaw = unicodedata.east_asian_width(ch)
        if eaw in ('F', 'W'):
            w += 2
        else:
            w += 1
    return w


# ─── ToolNameMapper ───

class ToolNameMapper:
    """将内部工具名映射为中文操作名 + 目标摘要 + 补充信息"""

    MAPPING = {
        # Claude Code 内置工具
        "Read":      ("读取文件", lambda i: _tail(i.get("file_path", ""), 50), _line_range),
        "Write":     ("写入文件", lambda i: _tail(i.get("file_path", ""), 50), None),
        "Edit":      ("编辑文件", lambda i: _tail(i.get("file_path", ""), 50), None),
        "Bash":      ("运行命令", lambda i: _head(i.get("command", ""), 60), None),
        "Grep":      ("搜索内容", lambda i: _head(i.get("pattern", ""), 40), None),
        "Glob":      ("搜索文件", lambda i: _head(i.get("pattern", ""), 40), None),
        "Task":      ("启动子任务", lambda i: _head(i.get("description", ""), 40), None),
        "TodoWrite": ("更新待办", lambda i: "", None),
        # Serena MCP 工具
        "mcp__serena__find_symbol": (
            "查看代码",
            lambda i: i.get("name_path_pattern", ""),
            lambda i: i.get("relative_path", "")),
        "mcp__serena__get_symbols_overview": (
            "浏览结构",
            lambda i: i.get("relative_path", ""), None),
        "mcp__serena__find_referencing_symbols": (
            "查找引用",
            lambda i: i.get("name_path", ""),
            lambda i: i.get("relative_path", "")),
        "mcp__serena__replace_symbol_body": (
            "修改代码",
            lambda i: i.get("name_path", ""),
            lambda i: i.get("relative_path", "")),
        "mcp__serena__replace_content": (
            "替换内容",
            lambda i: i.get("relative_path", ""), None),
        "mcp__serena__insert_after_symbol": (
            "插入代码",
            lambda i: i.get("name_path", ""),
            lambda i: i.get("relative_path", "")),
        "mcp__serena__insert_before_symbol": (
            "插入代码",
            lambda i: i.get("name_path", ""),
            lambda i: i.get("relative_path", "")),
        "mcp__serena__read_file": (
            "读取文件",
            lambda i: i.get("relative_path", ""),
            _line_range_serena),
        "mcp__serena__read_memory": (
            "读取记忆",
            lambda i: i.get("memory_file_name", ""), None),
        "mcp__serena__search_for_pattern": (
            "搜索代码",
            lambda i: _head(i.get("substring_pattern", ""), 40), None),
        "mcp__serena__rename_symbol": (
            "重命名",
            lambda i: f"{i.get('name_path', '')} → {i.get('new_name', '')}",
            lambda i: i.get("relative_path", "")),
        "mcp__serena__execute_shell_command": (
            "运行命令",
            lambda i: _head(i.get("command", ""), 60), None),
    }

    # 工具名 → 操作类型分类（供 Web Console 直播面板使用）
    ACTION_TYPE_MAP = {
        # read
        "Read": "read",
        "mcp__serena__read_file": "read",
        "mcp__serena__get_symbols_overview": "read",
        "mcp__serena__find_symbol": "read",
        "mcp__serena__read_memory": "read",
        "mcp__serena__list_dir": "read",
        "mcp__serena__list_memories": "read",
        "mcp__serena__check_onboarding_performed": "read",
        "mcp__serena__get_current_config": "read",
        # write
        "Write": "write",
        "Edit": "write",
        "NotebookEdit": "write",
        "mcp__serena__replace_content": "write",
        "mcp__serena__replace_symbol_body": "write",
        "mcp__serena__insert_after_symbol": "write",
        "mcp__serena__insert_before_symbol": "write",
        "mcp__serena__create_text_file": "write",
        "mcp__serena__rename_symbol": "write",
        "mcp__serena__write_memory": "write",
        "mcp__serena__edit_memory": "write",
        # exec
        "Bash": "exec",
        "mcp__serena__execute_shell_command": "exec",
        # search
        "Grep": "search",
        "Glob": "search",
        "mcp__serena__search_for_pattern": "search",
        "mcp__serena__find_referencing_symbols": "search",
        "mcp__serena__find_file": "search",
        "WebFetch": "search",
        "WebSearch": "search",
    }

    @classmethod
    def classify(cls, tool_name: str) -> str:
        """返回操作类型：read/write/exec/search，未知工具降级为 exec"""
        return cls.ACTION_TYPE_MAP.get(tool_name, "exec")

    def map(self, tool_name: str, input_data: dict) -> tuple:
        """返回 (操作名, 目标摘要, 补充信息)"""
        if tool_name in self.MAPPING:
            op_name, target_fn, supp_fn = self.MAPPING[tool_name]
            target = target_fn(input_data) if target_fn else ""
            supplement = supp_fn(input_data) if supp_fn else ""
            return op_name, target, supplement or ""

        # 其他 Serena 工具
        if tool_name.startswith("mcp__serena__"):
            short = tool_name.replace("mcp__serena__", "")
            return f"Serena:{short}", "", ""

        # 未知工具
        return tool_name, "", ""


# ─── ContentTruncator ───

class ContentTruncator:
    """按 PRD 规格截断内容"""

    @staticmethod
    def truncate_thinking(lines: list) -> list:
        """思考块：≤5 行全显示；>5 行显示前 3 行 + 折叠提示"""
        if len(lines) <= 5:
            return lines
        folded = len(lines) - 3
        return lines[:3] + [f"... (折叠了 {folded} 行)"]

    @staticmethod
    def truncate_text(lines: list) -> list:
        """文本/结果：≤20 行全显示；>20 行显示前 15 + 省略 + 尾 3"""
        if len(lines) <= 20:
            return lines
        omitted = len(lines) - 18
        return lines[:15] + [f"... (省略 {omitted} 行)"] + lines[-3:]

    @staticmethod
    def truncate_result(lines: list) -> list:
        """工具结果：同文本规则"""
        return ContentTruncator.truncate_text(lines)


# ─── StreamRenderer ───

class StreamRenderer:
    """将 stream-json 事件转换为人类可读的格式化文本

    输出目标：
    - console (Rich Console)：终端彩色输出（可选）
    - log_path (Path)：纯文本日志文件（可选）
    """

    def __init__(self, role: str, model: str, console=None, log_path: Path = None,
                 buffer_mode: bool = False):
        self._role = role
        self._model = model
        self._console = console
        self._log_path = log_path
        self._log_file = None
        # _rendered_up_to 已移除：assistant 事件是独立快照，无需增量追踪
        self._event_count = 0
        self._mapper = ToolNameMapper()
        self._truncator = ContentTruncator()
        self._buffer_mode = buffer_mode
        self._buffer: deque = deque(maxlen=200)

    def start(self):
        """输出 Agent 头，打开日志文件"""
        # 打开日志
        if self._log_path:
            try:
                self._log_path.parent.mkdir(parents=True, exist_ok=True)
                self._log_file = open(self._log_path, "a", encoding="utf-8")
            except Exception:
                self._log_file = None

        # Agent 头
        role_name = ROLE_DISPLAY.get(self._role, self._role)
        model_short = MODEL_SHORT.get(self._model, self._model)

        # 终端显示：使用终端宽度
        width = shutil.get_terminal_size((80, 24)).columns - 2  # 留 2 列安全边距
        label = f" 🤖 {role_name} ({model_short}) "
        label_width = _visual_width(label)
        pad = max(0, width - label_width)
        line = "━" * (pad // 2) + label + "━" * (pad - pad // 2)

        self._output_rich(line, style="bold")

        # 日志写入：固定 60 列宽度（避免手机/web 端换行错位）
        log_width = 60
        log_pad = max(0, log_width - label_width)
        log_line = "━" * (log_pad // 2) + label + "━" * (log_pad - log_pad // 2)
        self._log(log_line)

    def feed(self, event: dict):
        """处理一个 stream-json 事件"""
        try:
            event_type = event.get("type", "")

            if event_type == "system":
                self._handle_system(event)
            elif event_type == "assistant":
                self._handle_assistant(event)
            elif event_type == "_heartbeat":
                idle = event.get("idle_seconds", 0)
                text = f"⏳ 等待响应... (已等待 {int(idle)}s)"
                self._output_rich(text, style="dim")
                self._log(text)
            elif event_type == "_raw_line":
                content = event.get("content", "")
                text = f"⚠️ {content}"
                self._output_rich(text, style="dim")
                self._log(text)
            # result 事件不在 feed 中处理，由 finish() 统一处理
        except Exception:
            pass  # 渲染失败不影响执行

    def finish(self, duration: float, cost_usd: float,
               input_tokens: int, output_tokens: int,
               error: str = None, timeout_type: str = None):
        """输出 Agent 尾，关闭日志文件"""
        if error:
            if timeout_type:
                icon, label = "⏱️", "超时"
                text = f"{icon} {label} | 耗时 {_fmt_duration(duration)} | 原因：{error[:80]}"
                self._output_dotted(text, style="yellow")
            else:
                icon, label = "❌", "失败"
                text = f"{icon} {label} | 耗时 {_fmt_duration(duration)} | 原因：{error[:80]}"
                self._output_dotted(text, style="red")
            self._log(f"● {text}")
        else:
            plain = (
                f"✅ 完成 | 耗时 {_fmt_duration(duration)} | "
                f"输入 {input_tokens:,} tokens | 输出 {output_tokens:,} tokens | "
                f"${cost_usd:.2f}"
            )
            # Rich 彩色版本
            rich_text = (
                f"[green]●[/green] ✅ [bold green]完成[/bold green] [dim]|[/dim] "
                f"耗时 {_fmt_duration(duration)} [dim]|[/dim] "
                f"输入 {input_tokens:,} tokens [dim]|[/dim] 输出 {output_tokens:,} tokens [dim]|[/dim] "
                f"[yellow]${cost_usd:.2f}[/yellow]"
            )
            self._output_markup(rich_text)
            self._log(f"● {plain}")

        # 空行间隔
        self._output_rich("")
        self._log("")

        # 关闭日志
        if self._log_file:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None

    def render_waiting_confirm(self):
        """输出等待确认状态"""
        text = "⏸️  等待确认 — 请在终端中回复（或在 Claude Code 中处理）"
        self._output_dotted(text, style="bold yellow", blink=True)
        self._log(f"● {text}")

    def render_confirm_done(self):
        """确认完成，恢复"""
        text = "▶️  确认完成，继续执行"
        self._output_dotted(text, style="green")
        self._log(f"● {text}")

    # ─── 内部：事件类型处理 ───

    def _handle_system(self, event: dict):
        subtype = event.get("subtype", "")
        if subtype == "init":
            model = event.get("model", "")
            session_id = event.get("session_id", "")[:6]
            text = f"🚀 初始化 | 模型 {model} | 会话 {session_id}"
            self._output_dotted(text, style="dim")
            self._log(f"● {text}")

    def _handle_assistant(self, event: dict):
        """处理 assistant 事件，渲染 content blocks"""
        msg = event.get("message", {})
        if not isinstance(msg, dict):
            return
        content = msg.get("content", [])
        if not isinstance(content, list):
            return

        # Claude Code 的 assistant 事件是独立的完整消息快照，
        # 每个事件包含一个完整的 content blocks 列表。
        # 直接渲染所有 blocks。
        for block in content:
            self._render_block(block)

    # ─── 内部：Block 渲染 ───

    def _render_block(self, block: dict):
        block_type = block.get("type", "")
        if block_type == "thinking":
            self._render_thinking(block.get("thinking", ""))
        elif block_type == "text":
            self._render_text(block.get("text", ""))
        elif block_type == "tool_use":
            self._render_tool_use(block)
        elif block_type == "tool_result":
            self._render_tool_result(block)

    def _render_thinking(self, text: str):
        if not text or not text.strip():
            return
        lines = text.strip().split("\n")
        truncated = self._truncator.truncate_thinking(lines)

        first = f"💭 {truncated[0]}"
        rest = [f"   {line}" for line in truncated[1:]]
        plain = "\n".join([first] + rest)

        self._output_dotted(plain, style="dim italic")
        self._log(f"● {plain}")

    def _render_text(self, text: str):
        if not text or not text.strip():
            return
        lines = text.strip().split("\n")
        truncated = self._truncator.truncate_text(lines)

        first = f"📝 {truncated[0]}"
        rest = [f"   {line}" for line in truncated[1:]]
        plain = "\n".join([first] + rest)

        self._output_dotted(plain)
        self._log(f"● {plain}")

    def _render_tool_use(self, block: dict):
        name = block.get("name", "")
        input_data = block.get("input", {})
        if not isinstance(input_data, dict):
            input_data = {}

        op_name, target, supplement = self._mapper.map(name, input_data)

        # 主行（闪烁圆点 = 工具正在执行）
        if target:
            plain = f"🔧 {op_name} → {target}"
            rich = f"[blink green]●[/blink green] 🔧 [cyan bold]{op_name}[/cyan bold] [dim]→[/dim] {target}"
        else:
            plain = f"🔧 {op_name}"
            rich = f"[blink green]●[/blink green] 🔧 [cyan bold]{op_name}[/cyan bold]"

        self._output_markup(rich)
        self._log(f"● {plain}")

        # 补充行
        if supplement:
            supp_plain = f"   ↳ {supplement}"
            self._output_rich(supp_plain, style="dim")
            self._log(supp_plain)

        self._event_count += 1

    def _render_tool_result(self, block: dict):
        content = block.get("content", "")

        # content 可能是 list（多段文本）
        if isinstance(content, list):
            parts = []
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    parts.append(c.get("text", ""))
                elif isinstance(c, str):
                    parts.append(c)
            content = "\n".join(parts)

        if not isinstance(content, str):
            content = str(content) if content else ""

        is_error = block.get("is_error", False)

        if not content.strip():
            text = "   ┊ (无输出)"
            self._output_rich(text, style="dim")
            self._log(text)
            return

        lines = content.strip().split("\n")
        truncated = self._truncator.truncate_result(lines)

        for line in truncated:
            if is_error:
                text = f"   ┊ ❌ {line}"
                self._output_rich(text, style="red")
            else:
                text = f"   ┊ {line}"
                self._output_rich(text, style="dim")
            self._log(text)

    # ─── 内部：输出方法 ───


    def _output_dotted(self, text: str, style: str = None, blink: bool = False):
        """输出带状态指示点的行（终端显示绿色 ●，进行中闪烁）"""
        from rich.text import Text
        dot_style = "blink green" if blink else "green"
        t = Text()
        t.append("● ", style=dot_style)
        t.append(text, style=style)

        if self._buffer_mode:
            self._buffer.append(BufferLine(text=f"● {text}", rich_obj=t, style=style or ""))
            return
        if not self._console:
            return
        try:
            self._console.print(t, highlight=False)
        except Exception:
            pass

    def _output_rich(self, text: str, style: str = None):
        """输出到终端（Rich Text 对象，支持 style 参数）"""
        from rich.text import Text
        if style:
            t = Text(text)
            t.stylize(style)
        else:
            t = Text(text)

        if self._buffer_mode:
            self._buffer.append(BufferLine(text=text, rich_obj=t, style=style or ""))
            return
        if not self._console:
            return
        try:
            self._console.print(t, highlight=False)
        except Exception:
            pass

    def _output_markup(self, markup: str):
        """输出到终端（Rich markup 字符串，如 [bold]text[/bold]）"""
        if self._buffer_mode:
            self._buffer.append(BufferLine(text=markup, rich_obj=markup, is_markup=True))
            return
        if not self._console:
            return
        try:
            self._console.print(markup, highlight=False)
        except Exception:
            pass

    def _log(self, text: str):
        """追加写入日志文件（纯文本）"""
        if not self._log_file:
            return
        try:
            self._log_file.write(text + "\n")
            self._log_file.flush()
        except Exception:
            pass

    def flush_buffer_to_console(self, console):
        """退出分屏时将缓冲内容输出到滚动区"""
        if not console:
            return
        for line in self._buffer:
            try:
                if line.is_markup:
                    console.print(line.rich_obj, highlight=False)
                elif line.rich_obj:
                    console.print(line.rich_obj, highlight=False)
            except Exception:
                pass
        self._buffer.clear()

    def get_buffer_lines(self, n: int):
        """获取最近 n 行缓冲内容（用于分屏渲染）"""
        items = list(self._buffer)
        return items[-n:] if len(items) > n else items


# ─── StreamViewer（opus --stream 查看器）───

class StreamViewer:
    """实时查看子代理工作画面（opus --stream）"""

    @staticmethod
    def run(task_dir: Path, task_desc: str = ""):
        """跟踪 stream.log 文件的新内容"""
        import sys
        import json as _json

        log_dir = task_dir / "logs"

        # 先检查任务是否已经结束（避免对已完成/失败的旧任务无意义等待）
        state_file = task_dir / "state.json"
        if state_file.exists():
            try:
                state = _json.loads(state_file.read_text(encoding="utf-8"))
                status = state.get("status", "")
                if status in ("completed", "failed", "cancelled", "rolled_back"):
                    if not log_dir.exists() or not list(log_dir.glob("*.stream.log")):
                        print(f"⚠ 任务已结束 (状态: {status})，且无 stream.log 日志")
                        render_task_summary(task_dir)
                        print(f"  该任务可能在直播引擎上线之前运行")
                        print(f"  任务目录: {task_dir}")
                        sys.exit(0)
            except Exception:
                pass

        if not log_dir.exists():
            print("⏳ 等待日志目录创建...")
            waited = 0
            while waited < 120:
                if log_dir.exists():
                    break
                # 每 10 秒检查一次任务状态，已结束就不等了
                if waited > 0 and waited % 10 == 0 and state_file.exists():
                    try:
                        state = _json.loads(state_file.read_text(encoding="utf-8"))
                        status = state.get("status", "")
                        if status in ("completed", "failed", "cancelled", "rolled_back"):
                            print(f"⚠ 任务已结束 (状态: {status})，日志目录未创建")
                            sys.exit(0)
                    except Exception:
                        pass
                time.sleep(1)
                waited += 1
            else:
                print("超时：日志目录 120 秒内未创建")
                print(f"  任务目录: {task_dir}")
                print(f"  提示: 用 opus --stream TASK_ID 指定正确的任务")
                sys.exit(1)

        tracked = {}  # {path: file_handle}

        try:
            while True:
                # 扫描新的 stream.log 文件（含子任务目录）
                all_logs = list(log_dir.glob("*.stream.log"))
                for sub_log_dir in task_dir.glob("sub-*/logs"):
                    all_logs.extend(sub_log_dir.glob("*.stream.log"))
                for f in sorted(all_logs, key=lambda p: p.stat().st_mtime):
                    if f not in tracked:
                        fh = open(f, "r", encoding="utf-8")
                        content = fh.read()
                        lines = content.split("\n")
                        # 快速回放
                        if len(lines) > 100:
                            skip = len(lines) - 100
                            print(f"... (已跳过前 {skip} 行，完整记录见日志文件)")
                            for line in lines[-100:]:
                                print(line)
                        elif content:
                            print(content, end="")
                        tracked[f] = fh

                # 读取新内容
                for path, fh in tracked.items():
                    new = fh.read()
                    if new:
                        print(new, end="", flush=True)

                # 检查任务是否结束
                if state_file.exists():
                    try:
                        state = _json.loads(state_file.read_text(encoding="utf-8"))
                        status = state.get("status", "")
                        if status in ("completed", "failed", "cancelled", "rolled_back"):
                            time.sleep(1)  # 等待最后的日志写入
                            # 最后读一次
                            for path, fh in tracked.items():
                                final = fh.read()
                                if final:
                                    print(final, end="", flush=True)
                            print(f"\n📊 任务已结束 (状态: {status})")
                            render_task_summary(task_dir)
                            break
                    except Exception:
                        pass

                time.sleep(0.5)

        except KeyboardInterrupt:
            print("\n👋 已退出查看模式")
        finally:
            for fh in tracked.values():
                try:
                    fh.close()
                except Exception:
                    pass

    @staticmethod
    def replay(task_dir: Path, role_filter: str = None):
        """回放已完成任务的工作记录"""
        log_dir = task_dir / "logs"
        if not log_dir.exists():
            print("没有找到日志目录")
            return

        logs = sorted(log_dir.glob("*.stream.log"), key=lambda f: f.stat().st_mtime)

        if role_filter:
            logs = [f for f in logs if role_filter in f.stem]

        if not logs:
            print("没有找到工作记录")
            return

        for log_file in logs:
            content = log_file.read_text(encoding="utf-8")
            print(content, end="")

        print(f"\n📊 共回放 {len(logs)} 个角色的工作记录")
        render_task_summary(task_dir)


# ─── 分屏管理器 ───

class SplitScreenManager:
    """管理左右双栏分屏显示"""

    def __init__(self, left_renderer, right_renderer, left_role: str, right_role: str):
        self._left = left_renderer
        self._right = right_renderer
        self._left_role = left_role
        self._right_role = right_role
        self._start_time = time.time()
        self._finished = {}  # role -> stats dict

    def mark_finished(self, role: str, stats: dict):
        """标记一侧完成"""
        self._finished[role] = stats

    def build_renderable(self, table_height: int):
        """构建 Rich Layout 用于 Live 渲染

        Args:
            table_height: 进度表占用的行数

        Returns:
            Rich renderable 对象（Group）
        """
        from rich.columns import Columns
        from rich.panel import Panel
        from rich.text import Text
        from rich.console import Group

        term_width = shutil.get_terminal_size((120, 24)).columns
        term_height = shutil.get_terminal_size((120, 24)).lines

        # 窄终端降级
        if term_width < 80:
            return Text("⚠ 终端太窄（<80列），分屏已禁用", style="yellow")

        # 计算可用行数：终端高度 - 进度表 - 标题栏(2) - 状态栏(1) - 边距(2)
        available_lines = max(3, term_height - table_height - 5)

        # 每栏宽度
        half_width = (term_width - 3) // 2  # 3 = 分隔线 + 两侧空格

        # 左栏
        left_panel = self._build_panel(
            self._left, self._left_role, available_lines, half_width
        )
        # 右栏
        right_panel = self._build_panel(
            self._right, self._right_role, available_lines, half_width
        )

        # 左右并排
        columns = Columns([left_panel, right_panel], expand=True, equal=True)

        # 状态栏
        elapsed = time.time() - self._start_time
        elapsed_str = f"{int(elapsed // 60)}m{int(elapsed % 60):02d}s"
        left_name = ROLE_DISPLAY.get(self._left_role, self._left_role)
        right_name = ROLE_DISPLAY.get(self._right_role, self._right_role)
        left_count = self._left._event_count
        right_count = self._right._event_count

        left_status = "✅" if self._left_role in self._finished else "🔴"
        right_status = "✅" if self._right_role in self._finished else "🔴"

        status_text = Text.assemble(
            (f" {left_status} {left_name} {left_count} 事件", ""),
            (" │ ", "dim"),
            (f"{right_status} {right_name} {right_count} 事件", ""),
            (" │ ", "dim"),
            (f"运行中 {elapsed_str} ", ""),
        )

        return Group(columns, status_text)

    def _build_panel(self, renderer, role: str, max_lines: int, width: int):
        """构建单侧面板"""
        from rich.panel import Panel
        from rich.text import Text
        from rich.console import Group as RichGroup

        role_name = ROLE_DISPLAY.get(role, role)
        is_done = role in self._finished

        # 标题
        if is_done:
            title = f"✅ {role_name}"
            border_style = "green"
        else:
            title = f"🔴 {role_name}"
            border_style = "cyan"

        # 获取缓冲区最近 N 行
        lines = renderer.get_buffer_lines(max_lines)

        # 构建内容
        parts = []
        for buf_line in lines:
            if buf_line.is_markup:
                # markup 字符串直接作为 Text
                try:
                    from rich.markup import render as render_markup
                    parts.append(render_markup(str(buf_line.rich_obj)))
                except Exception:
                    parts.append(Text(buf_line.text))
            elif buf_line.rich_obj and hasattr(buf_line.rich_obj, 'plain'):
                parts.append(buf_line.rich_obj)
            else:
                parts.append(Text(buf_line.text))

        if not parts:
            parts = [Text("等待输出...", style="dim italic")]

        content = RichGroup(*parts)
        return Panel(content, title=title, border_style=border_style,
                     height=max_lines + 2, width=width)


# ─── 布局状态机 ───

class LayoutMode:
    SINGLE = "single"
    SPLIT = "split"


class LayoutStateMachine:
    """管理分屏布局的状态转换

    SINGLE → SPLIT：当第二个 Agent 启动时
    SPLIT → SINGLE：当 exit_split() 被显式调用时（双方都完成后）
    """

    def __init__(self):
        self._mode = LayoutMode.SINGLE
        self._active_agents: dict = {}  # role -> StreamRenderer
        self._split_manager: SplitScreenManager = None
        self._transition_callback = None
        self._finished_roles: dict = {}  # role -> stats

    def set_transition_callback(self, cb):
        """设置模式切换回调：cb(mode, split_manager_or_None)"""
        self._transition_callback = cb

    def agent_started(self, role: str, renderer):
        """注册新的 Agent，达到 2 个时触发 SINGLE→SPLIT"""
        self._active_agents[role] = renderer
        if len(self._active_agents) >= 2 and self._mode == LayoutMode.SINGLE:
            self._enter_split()

    def agent_finished(self, role: str, stats: dict):
        """标记 Agent 完成"""
        self._finished_roles[role] = stats
        if self._split_manager:
            self._split_manager.mark_finished(role, stats)

    def exit_split(self):
        """显式退出分屏→SINGLE"""
        if self._mode == LayoutMode.SPLIT:
            self._mode = LayoutMode.SINGLE
            self._split_manager = None
            if self._transition_callback:
                self._transition_callback(LayoutMode.SINGLE, None)

    def _enter_split(self):
        """进入分屏模式"""
        roles = list(self._active_agents.keys())
        left_role = roles[0]
        right_role = roles[1]
        self._split_manager = SplitScreenManager(
            left_renderer=self._active_agents[left_role],
            right_renderer=self._active_agents[right_role],
            left_role=left_role,
            right_role=right_role,
        )
        self._mode = LayoutMode.SPLIT
        if self._transition_callback:
            self._transition_callback(LayoutMode.SPLIT, self._split_manager)

    @property
    def mode(self):
        return self._mode

    @property
    def split_manager(self):
        return self._split_manager


def get_recent_stream_summary(task_dir, role: str) -> str:
    """从 stream.log 读取最近操作摘要（方式 C 增强摘要用）"""
    import os

    log_path = Path(task_dir) / "logs" / f"{role}.stream.log"
    if not log_path.exists():
        return ""

    try:
        content = log_path.read_text(encoding="utf-8").strip()
        if not content:
            return ""

        lines = content.split("\n")
        # 提取有图标前缀的行
        event_lines = [
            l for l in lines
            if l and len(l) > 0 and l[0] in "💭🔧📝✅❌⏸⏱🚀⏳⚠"
        ]
        recent = event_lines[-5:] if len(event_lines) > 5 else event_lines

        if not recent:
            return ""

        # 计算时间差
        mtime = os.path.getmtime(log_path)
        ago = int(time.time() - mtime)

        role_name = ROLE_DISPLAY.get(role, role)
        header = f"📡 {role_name} 最近动作 ({ago} 秒前)："
        body = "\n".join(f"   {line}" for line in recent)
        return f"{header}\n{body}"
    except Exception:
        return ""
