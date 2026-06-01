#!/usr/bin/env python3
"""
企业微信通知模块 - 推送消息到个人微信
Opus 智能协作系统 v3.0

通过企业微信应用消息接口，将消息推送到用户的个人微信（微工作台）
用户无需安装企业微信APP，只需用个人微信关注微工作台即可接收消息
"""

import time
import aiohttp
from typing import Optional, Dict
from datetime import datetime
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _LIB_DIR.parent



class WeComNotifier:
    """企业微信消息推送"""

    def __init__(self, corpid: str, agentid: str, secret: str, userid: str):
        """
        初始化企业微信通知器

        Args:
            corpid: 企业ID
            agentid: 应用ID
            secret: 应用Secret
            userid: 接收消息的用户ID
        """
        self.corpid = corpid
        self.agentid = agentid
        self.secret = secret
        self.userid = userid
        self.api_base = "https://qyapi.weixin.qq.com/cgi-bin"
        self._session: Optional[aiohttp.ClientSession] = None
        self._access_token: Optional[str] = None
        self._token_expires: int = 0

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get_access_token(self) -> str:
        """获取 access_token（带缓存）"""
        # 检查缓存是否有效
        if self._access_token and time.time() < self._token_expires - 60:
            return self._access_token

        session = await self._get_session()
        url = f"{self.api_base}/gettoken?corpid={self.corpid}&corpsecret={self.secret}"

        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                result = await resp.json()
                if result.get("errcode") == 0:
                    self._access_token = result["access_token"]
                    self._token_expires = time.time() + result.get("expires_in", 7200)
                    return self._access_token
                else:
                    raise Exception(f"获取 access_token 失败: {result}")
        except Exception as e:
            raise Exception(f"获取 access_token 异常: {e}")

    async def send_text(self, content: str) -> Dict:
        """
        发送文本消息

        Args:
            content: 消息内容（最长2048字节）

        Returns:
            API 响应
        """
        access_token = await self._get_access_token()
        session = await self._get_session()

        url = f"{self.api_base}/message/send?access_token={access_token}"
        data = {
            "touser": self.userid,
            "msgtype": "text",
            "agentid": self.agentid,
            "text": {
                "content": content
            }
        }

        try:
            async with session.post(url, json=data, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                result = await resp.json()
                result["success"] = result.get("errcode") == 0
                return result
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def send_textcard(self, title: str, description: str, url: str = "", btntxt: str = "详情") -> Dict:
        """
        发送文本卡片消息（支持点击跳转）

        Args:
            title: 标题（最长128字节）
            description: 描述（最长512字节）
            url: 点击跳转链接
            btntxt: 按钮文字

        Returns:
            API 响应
        """
        access_token = await self._get_access_token()
        session = await self._get_session()

        api_url = f"{self.api_base}/message/send?access_token={access_token}"
        data = {
            "touser": self.userid,
            "msgtype": "textcard",
            "agentid": self.agentid,
            "textcard": {
                "title": title[:128],
                "description": description[:512],
                "url": url or "https://work.weixin.qq.com",
                "btntxt": btntxt
            }
        }

        try:
            async with session.post(api_url, json=data, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                result = await resp.json()
                result["success"] = result.get("errcode") == 0
                return result
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def send_markdown(self, content: str) -> Dict:
        """
        发送 Markdown 消息
        注意：仅企业微信客户端支持，微工作台（个人微信）不支持 Markdown

        Args:
            content: Markdown 内容

        Returns:
            API 响应
        """
        access_token = await self._get_access_token()
        session = await self._get_session()

        url = f"{self.api_base}/message/send?access_token={access_token}"
        data = {
            "touser": self.userid,
            "msgtype": "markdown",
            "agentid": self.agentid,
            "markdown": {
                "content": content
            }
        }

        try:
            async with session.post(url, json=data, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                result = await resp.json()
                result["success"] = result.get("errcode") == 0
                return result
        except Exception as e:
            return {"success": False, "error": str(e)}


async def test_wecom():
    """测试企业微信推送"""
    import json
    import os

    config_path = str(_PROJECT_ROOT / 'config.json')
    with open(config_path) as f:
        config = json.load(f)

    secrets = config.get("secrets", {})
    notifier = WeComNotifier(
        corpid=secrets.get("wecom_corpid", ""),
        agentid=secrets.get("wecom_agentid", ""),
        secret=secrets.get("wecom_secret", ""),
        userid=secrets.get("wecom_userid", "")
    )

    print("发送测试消息...")
    result = await notifier.send_text(
        f"🎉 企业微信推送测试成功！\n\n"
        f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"这条消息来自 Opus v3.0 智能协作系统"
    )
    print(f"结果: {result}")

    await notifier.close()
    return result.get("success", False)


def send_wecom_notification(content: str, title: str = None, **kwargs) -> bool:
    """
    简单的同步接口，供 Claude 快速发送企业微信通知

    Args:
        content: 消息内容
        title: 可选标题（会加到内容前面）
        **kwargs: 忽略的额外参数（兼容性）

    Returns:
        True 表示发送成功，False 表示失败

    Usage:
        from wecom_notifier import send_wecom_notification
        send_wecom_notification("消息内容", title="标题")
    """
    import json
    import os

    config_path = str(_PROJECT_ROOT / 'config.json')
    try:
        with open(config_path) as f:
            config = json.load(f)
    except Exception:
        print("⚠️ 无法读取配置文件")
        return False

    secrets = config.get("secrets", {})
    notifier = WeComNotifier(
        corpid=secrets.get("wecom_corpid", ""),
        agentid=secrets.get("wecom_agentid", ""),
        secret=secrets.get("wecom_secret", ""),
        userid=secrets.get("wecom_userid", "")
    )

    # 构建消息
    message = f"{title}\n\n{content}" if title else content

    async def _send():
        try:
            result = await notifier.send_text(message[:2000])  # 企微限制2048字节
            await notifier.close()
            return result.get("success", False)
        except Exception as e:
            print(f"⚠️ 发送失败: {e}")
            return False

    import asyncio
    return asyncio.run(_send())


# 为了兼容性，创建别名模块
# Claude 可能会尝试: from wecom_notification import send_wecom_notification
# 现在正确的方式是: from wecom_notifier import send_wecom_notification


if __name__ == "__main__":
    import asyncio
    asyncio.run(test_wecom())
