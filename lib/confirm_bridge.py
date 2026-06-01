"""跨进程确认 IPC（文件轮询机制）

子进程创建 .vizo/confirms/{id}.request.json 并阻塞等待，
父进程（或 opus --confirm）写入 {id}.response.json 后子进程继续。
"""

import json
import time
import uuid

from lib.paths import CONFIRMS_DIR


CONFIRM_DIR = CONFIRMS_DIR


def create_request(task_id, message, file_path=None, summary="",
                   preview_link="", req_type="confirm_with_feedback",
                   context=None):
    """创建确认请求文件，返回 request_id"""
    CONFIRM_DIR.mkdir(parents=True, exist_ok=True)
    short_id = uuid.uuid4().hex[:6]
    request_id = f"{task_id}-{short_id}" if task_id else f"req-{short_id}"

    request = {
        "id": request_id,
        "task_id": task_id or "",
        "type": req_type,
        "message": message,
        "file": str(file_path) if file_path else "",
        "preview_link": preview_link or "",
        "summary": summary or "",
        "created_at": time.time(),
    }
    if context:
        request["context"] = context
    (CONFIRM_DIR / f"{request_id}.request.json").write_text(
        json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 输出 stderr 标记供父进程感知
    # 扩展格式：OPUS_CONFIRM_PENDING|id|type|message[:80]|role_display|step_display
    import sys
    role_display = ""
    step_display = ""
    if context:
        role_display = context.get("role_display", "")
        step_display = context.get("step_display", "")
    print(
        f"OPUS_CONFIRM_PENDING|{request_id}|{req_type}|{message[:80]}|{role_display}|{step_display}",
        file=sys.stderr, flush=True
    )

    return request_id


def wait_for_response(request_id, timeout=86400, poll_interval=3):
    """轮询等待响应文件，返回 (action, feedback)"""
    response_file = CONFIRM_DIR / f"{request_id}.response.json"
    deadline = time.time() + timeout

    while time.time() < deadline:
        if response_file.exists():
            try:
                data = json.loads(response_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                time.sleep(poll_interval)
                continue
            # 清理文件
            response_file.unlink(missing_ok=True)
            (CONFIRM_DIR / f"{request_id}.request.json").unlink(missing_ok=True)
            return data.get("action") or data.get("response", "confirm"), data.get("feedback", "")
        time.sleep(poll_interval)

    # 超时自动取消（不能自动确认，防止未授权操作被执行）
    (CONFIRM_DIR / f"{request_id}.request.json").unlink(missing_ok=True)
    return "cancel", ""


def respond(request_id, action, feedback=""):
    """写入响应文件（供 opus --confirm 调用）"""
    CONFIRM_DIR.mkdir(parents=True, exist_ok=True)
    response = {
        "action": action,
        "feedback": feedback,
        "responded_at": time.time(),
    }
    (CONFIRM_DIR / f"{request_id}.response.json").write_text(
        json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get_pending():
    """列出所有待确认请求"""
    if not CONFIRM_DIR.exists():
        return []
    requests = []
    for f in sorted(CONFIRM_DIR.glob("*.request.json")):
        resp_file = f.with_name(f.name.replace(".request.json", ".response.json"))
        if not resp_file.exists():
            try:
                requests.append(json.loads(f.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
    return requests
