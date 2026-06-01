#!/usr/bin/env python3
"""
PreCompact 统一调度器 — 合并 pre_compact_cleaner + pre_compact_saver

v5.0: 将2个独立hooks合并为1个进程，共享1个Redis连接。
"""

import os
import sys
import json
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / 'lib'))
from paths import CONFIG_FILE, OPUS_HOME
from project_identity import detect_project as detect_runtime_project


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


def main():
    try:
        if sys.stdin.isatty():
            print(json.dumps({}))
            return

        data = sys.stdin.read().strip()
        if not data:
            print(json.dumps({}))
            return

        project = detect_project()
        r = get_redis_client()
        messages = []

        if r:
            try:
                # === 逻辑1: 清理缓存（原 pre_compact_cleaner） ===
                for key in r.scan_iter(f"serena_cache:{project}:*", count=100):
                    r.delete(key)
                for key in r.scan_iter(f"delegate_cache:{project}:*", count=100):
                    r.delete(key)

                # === 逻辑2: 保存工作状态（原 pre_compact_saver） ===
                import asyncio
                from datetime import datetime

                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                # 保存压缩事件到 Redis
                compact_event = {
                    "timestamp": timestamp,
                    "project": project,
                    "type": "auto_compact"
                }
                r.lpush(f"compact_events:{project}", json.dumps(compact_event))
                r.ltrim(f"compact_events:{project}", 0, 49)

                # 获取工作状态摘要
                work_state_summary = ""
                try:
                    from work_state import WorkStateManager
                    wsm = WorkStateManager(r, project)
                    state = wsm.get_current_state()
                    if state:
                        work_state_summary = state.get("summary", "")[:300]
                except Exception:
                    pass

                # 企微通知（5秒超时）
                try:
                    config_path = CONFIG_FILE
                    with open(config_path) as f:
                        config = json.load(f)

                    secrets = config.get("secrets", {})
                    if secrets.get("wecom_corp_id") and secrets.get("wecom_agent_secret"):
                        from wecom_notification import WeComNotifier
                        notifier = WeComNotifier(
                            corp_id=secrets.get('wecom_corp_id', ''),
                            agent_id=secrets.get('wecom_agent_id', ''),
                            agent_secret=secrets.get('wecom_agent_secret', ''),
                            userid=secrets.get('wecom_userid', '')
                        )

                        async def send():
                            try:
                                await asyncio.wait_for(
                                    notifier.send_text(
                                        f"⚠️ [上下文压缩] {project}\n\n"
                                        f"会话内容已自动保存到记忆。\n"
                                        f"时间: {timestamp}\n\n"
                                        f"工作状态:\n{work_state_summary}"
                                    ),
                                    timeout=5.0
                                )
                            except asyncio.TimeoutError:
                                pass
                            finally:
                                await notifier.close()

                        asyncio.run(send())
                except Exception:
                    pass

                messages.append("使用 `/opus` 重新激活智能协作模式")

            except Exception:
                pass
            finally:
                r.close()

        if messages:
            print(json.dumps({"systemMessage": "[上下文已压缩] " + "; ".join(messages)}))
        else:
            print(json.dumps({}))

    except Exception:
        print(json.dumps({}))


if __name__ == '__main__':
    main()
