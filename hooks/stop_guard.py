#!/usr/bin/env python3
"""
Stop hook — 防止主会话在有待处理确认请求时停止

当子代理阻塞等待确认时，主会话不能停止工作，
否则子进程会永远挂起。

v2: 增加过期机制 — 超过 2 小时的请求自动过期，不再阻塞主会话。
v3: 区分主会话/子代理上下文 — 子代理只检查自身任务的确认，
    且指令中不引用 AskUserQuestion（子代理无此工具）。
"""

import json
import os
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / 'lib'))
from paths import OPUS_HOME, CONFIRMS_DIR, CONFIG_FILE, write_data_path

CONFIRM_DIR = CONFIRMS_DIR
EXPIRE_SECONDS = 86400  # 24 小时


def _get_request_age(req_file, data):
    """获取请求的存活时间（秒）。优先用 created_at 字段，否则用文件 mtime。"""
    created_at = data.get("created_at")
    if created_at is not None:
        try:
            return time.time() - float(created_at)
        except (TypeError, ValueError):
            pass
    # fallback: 文件修改时间
    try:
        return time.time() - os.path.getmtime(req_file)
    except OSError:
        return 0


def _expire_request(req_file, data):
    """为过期请求自动创建 .response.json，避免下次再检查。"""
    resp_file = req_file.with_name(
        req_file.name.replace(".request.json", ".response.json")
    )
    response = {
        "action": "expired",
        "feedback": "",
        "responded_at": time.time(),
        "reason": "auto-expired after 24 hours",
    }
    try:
        resp_file.write_text(
            json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass

def _collect_pending(task_id_filter=None):
    """收集待确认请求。task_id_filter 非空时只返回匹配该任务的请求。"""
    if not CONFIRM_DIR.exists():
        return []

    pending = []
    for req_file in CONFIRM_DIR.glob("*.request.json"):
        resp_file = req_file.with_name(
            req_file.name.replace(".request.json", ".response.json")
        )
        if resp_file.exists():
            continue
        try:
            data = json.loads(req_file.read_text(encoding='utf-8'))
        except Exception:
            continue

        # 检查是否过期
        age = _get_request_age(req_file, data)
        if age > EXPIRE_SECONDS:
            _expire_request(req_file, data)
            continue

        # 子代理模式：只保留当前任务的确认请求
        if task_id_filter:
            req_task_id = data.get("task_id", "")
            if req_task_id != task_id_filter:
                continue

        pending.append(data)

    return pending


def _build_main_session_notice(req):
    """构建主会话的确认通知（含 AskUserQuestion 指引）"""
    req_id = req.get('id', '')
    message = req.get('message', '未知')
    req_type = req.get('type', 'confirm')
    summary = req.get('summary', '')
    file_path = req.get('file', '')
    preview_link = req.get('preview_link', '')
    context = req.get('context', {})

    lines = [
        f"Opus 子代理正在等待确认（{message}），请先处理：",
    ]
    if context:
        if context.get("task_name"):
            lines.append(f"任务：{context['task_name']}")
        if context.get("role_display"):
            lines.append(f"角色：{context['role_display']}")
        if context.get("step_display"):
            lines.append(f"步骤：{context['step_display']}")
        cost_usd = context.get("cost_usd", 0)
        if cost_usd > 0:
            duration = context.get("duration", 0)
            m, s = divmod(int(duration), 60)
            dur_str = f"{m}分{s}秒" if m else f"{s}秒"
            lines.append(f"本步骤：{dur_str} | ${cost_usd:.2f}")
        total_cost = context.get("total_cost_usd", 0)
        if total_cost > 0:
            lines.append(f"任务累计：${total_cost:.2f}")
    if summary:
        short_summary = summary[:300] + ("..." if len(summary) > 300 else "")
        lines.append(f"摘要：{short_summary}")
    if file_path:
        lines.append(f"文档路径：{file_path}")
    if preview_link:
        lines.append(f"在线预览：{preview_link}")

    lines.append("")
    lines.append("操作步骤：")
    lines.append("1. 向用户展示确认信息（如需查看完整文档请 Read 上方文档路径）")
    lines.append("2. 使用 AskUserQuestion 询问用户")

    if req_type in ("confirm_with_feedback", "confirm_with_discussion"):
        response_path = write_data_path("confirms", f"{req_id}.response.json", project_root=OPUS_HOME)
        lines.append(f"3. 写入响应文件 {response_path}")
        lines.append('   确认: {{"id":"' + req_id + '","action":"confirm","feedback":""}}')
        lines.append('   取消: {{"id":"' + req_id + '","action":"cancel","feedback":""}}')
        lines.append('   修改: {{"id":"' + req_id + '","action":"feedback","feedback":"用户意见"}}')
        if req_type == "confirm_with_discussion":
            lines.append('   讨论: {{"id":"' + req_id + '","action":"discussion","feedback":""}}')
    else:
        response_path = write_data_path("confirms", f"{req_id}.response.json", project_root=OPUS_HOME)
        lines.append(f"3. 写入响应文件 {response_path}")
        lines.append('   确认: {{"id":"' + req_id + '","action":"confirm","feedback":""}}')
        lines.append('   取消: {{"id":"' + req_id + '","response":"cancel","feedback":""}}')

    return "\n".join(lines)


def _build_subagent_notice(req):
    """构建子代理的确认通知（不引用 AskUserQuestion，直接用 opus --confirm）"""
    req_id = req.get('id', '')
    message = req.get('message', '未知')
    req_type = req.get('type', 'confirm')

    lines = [
        f"编排器正在等待确认（{message}），请直接执行以下命令处理：",
        "",
    ]

    if req_type in ("confirm_with_feedback", "confirm_with_discussion"):
        lines.append(f'确认继续: python3 -c "from lib.confirm_bridge import respond; respond(\'{req_id}\', \'confirm\')"')
        lines.append(f'取消任务: python3 -c "from lib.confirm_bridge import respond; respond(\'{req_id}\', \'cancel\')"')
        lines.append(f'提供修改意见: python3 -c "from lib.confirm_bridge import respond; respond(\'{req_id}\', \'feedback\', \'修改意见\')"')
    else:
        lines.append(f'确认: python3 -c "from lib.confirm_bridge import respond; respond(\'{req_id}\', \'confirm\')"')
        lines.append(f'取消: python3 -c "from lib.confirm_bridge import respond; respond(\'{req_id}\', \'cancel\')"')

    lines.append("")
    lines.append("注意：不要使用 AskUserQuestion，不要手动写 response.json 文件。")

    return "\n".join(lines)


def main():
    # 检测运行环境：子代理通过 OPUS_AGENT_ROLE 标识
    agent_role = os.environ.get("OPUS_AGENT_ROLE")
    agent_task_id = os.environ.get("OPUS_TASK_ID")

    # 子代理模式：只检查当前任务的确认请求
    task_filter = agent_task_id if agent_role else None
    pending = _collect_pending(task_id_filter=task_filter)

    if not pending:
        print(json.dumps({}))
        return

    req = pending[0]

    if agent_role:
        notice = _build_subagent_notice(req)
    else:
        notice = _build_main_session_notice(req)

    print(json.dumps({
        "decision": "block",
        "reason": notice
    }))


if __name__ == '__main__':
    main()
