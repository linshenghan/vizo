#!/usr/bin/env python3
"""
UserPromptSubmit hook - Chrome DevTools 自动检测器

v4.1: 检测用户描述的前端问题，自动检测 Chrome DevTools 可用性。

工作原理：
1. 检测用户消息是否涉及前端/UI 问题
2. 如果涉及，检测 Chrome DevTools 是否可用
3. 如果可用，自动获取页面列表并注入上下文
4. 如果不可用，提示用户启动 Chrome 调试模式

触发关键词：
- 页面、按钮、点击、显示、弹窗、下拉、选择、输入框
- 看不到、点不了、没反应、消失、被挡住
- 样式、布局、位置、颜色
"""

import os
import sys
import json
import urllib.request
import urllib.error
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / 'lib'))
from paths import CONFIG_FILE, OPUS_HOME

import redis

# Chrome DevTools 地址
CHROME_DEVTOOLS_URL = "http://127.0.0.1:9222"

# 前端问题关键词
FRONTEND_KEYWORDS = [
    # UI 元素
    "页面", "按钮", "点击", "显示", "弹窗", "下拉", "选择", "输入框",
    "菜单", "导航", "列表", "表格", "表单", "图片", "图标", "标签",
    "模态框", "对话框", "提示", "toast", "弹出", "浮层",
    # 问题描述
    "看不到", "点不了", "没反应", "消失", "被挡住", "被遮住", "不见了",
    "点击无效", "无法点击", "不能选", "选不了", "打不开", "关不掉",
    "闪退", "卡住", "空白", "加载", "刷新",
    # 样式相关
    "样式", "布局", "位置", "颜色", "大小", "宽度", "高度", "间距",
    "对齐", "居中", "偏移", "错位", "重叠", "溢出",
    # 技术术语（产品经理可能偶尔用到）
    "前端", "界面", "UI", "交互",
]

# 排除关键词（避免误触发）
EXCLUDE_KEYWORDS = [
    "后端", "接口", "API", "数据库", "服务器", "部署", "配置",
    "代码", "函数", "类", "模块", "Python", "Java",
]


def get_redis_client():
    """获取 Redis 连接"""
    config_path = CONFIG_FILE
    try:
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


def is_frontend_issue(text: str) -> bool:
    """判断是否是前端问题描述"""
    text_lower = text.lower()

    # 检查排除关键词
    for kw in EXCLUDE_KEYWORDS:
        if kw.lower() in text_lower:
            return False

    # 检查前端关键词
    for kw in FRONTEND_KEYWORDS:
        if kw.lower() in text_lower:
            return True

    return False


def check_chrome_devtools() -> dict:
    """
    检测 Chrome DevTools 是否可用

    Returns:
        {
            "available": bool,
            "pages": list,  # 如果可用，返回页面列表
            "error": str    # 如果不可用，返回错误信息
        }
    """
    try:
        url = f"{CHROME_DEVTOOLS_URL}/json"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=1) as response:
            pages = json.loads(response.read().decode('utf-8'))

            # 过滤出普通页面（排除扩展、service worker 等）
            real_pages = [
                p for p in pages
                if p.get('type') == 'page'
                and not p.get('url', '').startswith('chrome')
                and not p.get('url', '').startswith('chrome-extension')
            ]

            return {
                "available": True,
                "pages": real_pages,
                "error": None
            }
    except urllib.error.URLError:
        return {
            "available": False,
            "pages": [],
            "error": "Chrome 调试模式未启动"
        }
    except Exception as e:
        return {
            "available": False,
            "pages": [],
            "error": str(e)
        }


def get_session_key(project: str) -> str:
    """获取会话标记键（避免重复提示）"""
    ppid = os.getppid()
    return f"chrome_devtools_prompted:{project}:{ppid}"


def main():
    try:
        # 读取 hook 传入的上下文
        if sys.stdin.isatty():
            return

        data = sys.stdin.read().strip()
        if not data:
            return

        context = json.loads(data)
        user_prompt = context.get('prompt', '')

        if not user_prompt:
            return

        # 检测是否是前端问题
        if not is_frontend_issue(user_prompt):
            return

        # 获取 Redis 连接
        r = get_redis_client()
        if not r:
            return

        # 检测当前项目
        cwd = os.getcwd()
        project = os.path.basename(cwd) or 'default'

        # 检查本次会话是否已提示过（5分钟内不重复提示）
        session_key = get_session_key(project)
        if r.exists(session_key):
            r.close()
            return

        # 检测 Chrome DevTools
        result = check_chrome_devtools()

        if result["available"]:
            # Chrome 可用，获取页面信息
            pages = result["pages"]
            if pages:
                page_list = "\n".join([
                    f"  - **{p.get('title', '无标题')}**: {p.get('url', 'N/A')}"
                    for p in pages[:5]  # 最多显示5个
                ])

                message = f"""<system-reminder>
🔍 **Chrome DevTools MCP 已连接** - 检测到前端问题描述

当前打开的页面：
{page_list}

**【强制】必须使用 MCP 工具，禁止手写 Python 脚本：**
- `mcp__chrome-devtools__take_screenshot` - 截图验证效果
- `mcp__chrome-devtools__evaluate_script` - 执行 JS 检查元素
- `mcp__chrome-devtools__list_pages` - 列出页面
- `mcp__chrome-devtools__select_page` - 选择页面

**禁止**：使用 Python websockets 连接 CDP 协议。
</system-reminder>"""
            else:
                message = """<system-reminder>
🔍 **Chrome DevTools 已连接** - 但没有检测到可调试的页面

请让用户在调试浏览器中打开需要调试的页面。
</system-reminder>"""

            # 设置缓存，5分钟内不重复提示
            r.setex(session_key, 300, "1")

        else:
            # Chrome 不可用，提示用户启动
            message = """<system-reminder>
⚠️ **检测到前端问题** - Chrome 调试模式未启动

如果需要查看用户的页面来定位问题，请提示用户：

> 请双击桌面的「调试Chrome.bat」，然后在打开的浏览器中访问有问题的页面，完成后告诉我。

用户完成后，你就可以通过 Chrome DevTools 查看页面进行调试。
</system-reminder>"""

            # 设置缓存，2分钟内不重复提示
            r.setex(session_key, 120, "1")

        r.close()
        print(message)

    except Exception:
        # hook 失败不应阻塞用户输入
        pass


if __name__ == '__main__':
    main()
