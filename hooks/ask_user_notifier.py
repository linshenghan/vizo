#!/usr/bin/env python3
"""PreToolUse hook - AskUserQuestion 通知

当 Claude Code 使用 AskUserQuestion 工具时，发送企微通知并附带交互链接。
用户可以在手机上直接选择选项或输入自定义回复。
"""

import sys
import json
import asyncio
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / 'lib'))
from paths import CONFIG_FILE, OPUS_HOME

async def main():
    try:
        # 读取 hook 输入
        input_data = json.load(sys.stdin)
        tool_input = input_data.get('tool_input', {})

        # 提取问题和选项
        questions = tool_input.get('questions', [])
        if not questions:
            print(json.dumps({"continue": True}))
            return

        # 构建问题内容（支持多个问题）
        all_questions = []
        all_options = []
        
        for q in questions:
            question_text = q.get('question', '')
            header = q.get('header', '')
            options = q.get('options', [])
            multi_select = q.get('multiSelect', False)
            
            q_info = {
                'question': question_text,
                'header': header,
                'options': options,
                'multiSelect': multi_select
            }
            all_questions.append(q_info)
            
            # 提取选项文本
            for opt in options:
                label = opt.get('label', '')
                desc = opt.get('description', '')
                all_options.append({'label': label, 'description': desc})

        # 构建通知内容
        content_lines = []
        for i, q in enumerate(all_questions):
            if len(all_questions) > 1:
                content_lines.append(f"问题 {i+1}: {q['question']}")
            else:
                content_lines.append(q['question'])
            
            if q['options']:
                content_lines.append("")
                for j, opt in enumerate(q['options']):
                    label = opt.get('label', '')
                    desc = opt.get('description', '')
                    content_lines.append(f"  {j+1}. {label}")
                    if desc:
                        content_lines.append(f"     {desc}")
            content_lines.append("")

        content = "\n".join(content_lines)

        # 发送通知（带选项）
        from notification import get_notification_manager, NotificationType
        config_path = CONFIG_FILE
        with open(config_path) as f:
            config = json.load(f)

        notifier = get_notification_manager(config)
        
        # v4.1: 5 秒超时保护
        try:
            await asyncio.wait_for(
                notifier.notify(
                    NotificationType.INFO_SUPPLEMENT,
                    "Claude Code 需要你的选择",
                    content,
                    wait_response=False,
                    options=all_options,
                    extra_data={'questions': all_questions}
                ),
                timeout=5.0
            )
        except asyncio.TimeoutError:
            pass  # 超时不阻塞工具调用

        # 允许工具继续执行
        print(json.dumps({"continue": True}))
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(json.dumps({"continue": True, "systemMessage": f"[通知警告] {e}"}))

if __name__ == '__main__':
    asyncio.run(main())
