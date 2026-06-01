#!/usr/bin/env python3
"""企微 Callback Server - 接收企微消息，解密后写入 Redis"""

import base64
import hashlib
import json
import logging
import os
import struct
import time
from pathlib import Path
from xml.etree import ElementTree as ET

from aiohttp import web
from Crypto.Cipher import AES

logger = logging.getLogger(__name__)


# ─── 企微消息加解密 ──────────────────────────────────

def verify_signature(token: str, timestamp: str, nonce: str,
                     encrypt: str, msg_signature: str) -> bool:
    """验证企微消息签名"""
    parts = sorted([token, timestamp, nonce, encrypt])
    calc = hashlib.sha1("".join(parts).encode()).hexdigest()
    return calc == msg_signature


def decode_aes_key(encoding_aes_key: str) -> bytes:
    """将 EncodingAESKey（43字符 Base64）解码为 32 字节 AES 密钥"""
    return base64.b64decode(encoding_aes_key + "=")


def pkcs7_unpad(data: bytes, block_size: int = 32) -> bytes:
    """PKCS7 去填充（企微使用 block_size=32）"""
    pad_len = data[-1]
    if pad_len < 1 or pad_len > block_size:
        raise ValueError(f"无效的 PKCS7 填充: {pad_len}")
    if data[-pad_len:] != bytes([pad_len]) * pad_len:
        raise ValueError("PKCS7 填充数据不一致")
    return data[:-pad_len]


def pkcs7_pad(data: bytes, block_size: int = 32) -> bytes:
    """PKCS7 填充"""
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len]) * pad_len


def decrypt_message(encrypt: str, encoding_aes_key: str, corp_id: str) -> str:
    """解密企微消息

    密文结构（解密后）: 16字节随机 + 4字节消息长度(big-endian) + 消息体 + receiveId
    """
    key = decode_aes_key(encoding_aes_key)
    iv = key[:16]
    cipher = AES.new(key, AES.MODE_CBC, iv)
    decrypted = cipher.decrypt(base64.b64decode(encrypt))
    unpadded = pkcs7_unpad(decrypted, block_size=32)

    # 解析: 跳过16字节随机前缀，读取4字节消息长度
    msg_len = struct.unpack(">I", unpadded[16:20])[0]
    msg = unpadded[20:20 + msg_len].decode("utf-8")
    receive_id = unpadded[20 + msg_len:].decode("utf-8")

    if corp_id and receive_id != corp_id:
        raise ValueError(f"receiveId 不匹配: 期望 {corp_id}, 收到 {receive_id}")

    return msg


def encrypt_message(msg: str, encoding_aes_key: str, corp_id: str) -> str:
    """加密消息（用于回复企微验证请求）"""
    key = decode_aes_key(encoding_aes_key)
    iv = key[:16]
    msg_bytes = msg.encode("utf-8")
    corp_bytes = corp_id.encode("utf-8")
    random_prefix = os.urandom(16)

    plaintext = random_prefix + struct.pack(">I", len(msg_bytes)) + msg_bytes + corp_bytes
    padded = pkcs7_pad(plaintext, block_size=32)

    cipher = AES.new(key, AES.MODE_CBC, iv)
    encrypted = cipher.encrypt(padded)
    return base64.b64encode(encrypted).decode("utf-8")


def make_response_xml(encrypt: str, signature: str,
                      timestamp: str, nonce: str) -> str:
    """构造企微回调响应 XML"""
    return (
        f"<xml>"
        f"<Encrypt><![CDATA[{encrypt}]]></Encrypt>"
        f"<MsgSignature><![CDATA[{signature}]]></MsgSignature>"
        f"<TimeStamp>{timestamp}</TimeStamp>"
        f"<Nonce><![CDATA[{nonce}]]></Nonce>"
        f"</xml>"
    )


# ─── Callback Server ─────────────────────────────────

class WecomCallbackServer:
    """企微回调服务器"""

    def __init__(self, config: dict):
        wecom_cfg = config.get("wecom", {})
        cb_cfg = wecom_cfg.get("callback_server", {})
        self.token = cb_cfg.get("token", "")
        self.encoding_aes_key = cb_cfg.get("encoding_aes_key", "")
        self.corp_id = wecom_cfg.get("corp_id", "")
        self.redis_url = wecom_cfg.get("redis_url", "redis://localhost:6379")
        self._redis = None

    async def _get_redis(self):
        if self._redis is None:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(self.redis_url)
        # 检查连接是否可用，断线时自动重建
        try:
            await self._redis.ping()
        except Exception:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(self.redis_url)
        return self._redis

    def create_app(self) -> web.Application:
        """创建 aiohttp Application"""
        app = web.Application()
        app.router.add_get("/wecom/callback", self.handle_verify)
        app.router.add_post("/wecom/callback", self.handle_message)
        app.router.add_get("/health", self.handle_health)
        app.on_cleanup.append(self._cleanup)
        return app

    async def _cleanup(self, app):
        if self._redis:
            await self._redis.close()

    async def handle_health(self, request: web.Request) -> web.Response:
        """健康检查端点"""
        return web.Response(text=json.dumps({"status": "ok", "service": "wecom-callback"}),
                            content_type="application/json")

    # ─── GET: URL 验证 ───

    async def handle_verify(self, request: web.Request) -> web.Response:
        """处理企微 URL 验证（首次配置回调时）"""
        msg_signature = request.query.get("msg_signature", "")
        timestamp = request.query.get("timestamp", "")
        nonce = request.query.get("nonce", "")
        echostr = request.query.get("echostr", "")

        if not verify_signature(self.token, timestamp, nonce, echostr, msg_signature):
            logger.warning("URL 验证签名失败")
            return web.Response(status=403, text="签名验证失败")

        try:
            decrypted = decrypt_message(echostr, self.encoding_aes_key, self.corp_id)
            return web.Response(text=decrypted)
        except Exception as e:
            logger.error(f"URL 验证解密失败: {e}")
            return web.Response(status=500, text="解密失败")

    # ─── POST: 消息接收 ───

    async def handle_message(self, request: web.Request) -> web.Response:
        """处理企微消息回调"""
        msg_signature = request.query.get("msg_signature", "")
        timestamp = request.query.get("timestamp", "")
        nonce = request.query.get("nonce", "")

        body = await request.text()
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return web.Response(status=400, text="无效的 XML")

        encrypt = root.findtext("Encrypt", "")
        if not encrypt:
            return web.Response(status=400, text="缺少 Encrypt 字段")

        # 验签
        if not verify_signature(self.token, timestamp, nonce, encrypt, msg_signature):
            logger.warning("消息签名验证失败")
            return web.Response(status=403, text="签名验证失败")

        # 解密
        try:
            decrypted_xml = decrypt_message(encrypt, self.encoding_aes_key, self.corp_id)
        except Exception as e:
            logger.error(f"消息解密失败: {e}")
            return web.Response(status=500, text="解密失败")

        # 解析消息内容
        try:
            msg_root = ET.fromstring(decrypted_xml)
            msg_type = msg_root.findtext("MsgType", "")
            content = msg_root.findtext("Content", "")
            from_user = msg_root.findtext("FromUserName", "")
        except ET.ParseError:
            logger.error("解密后的消息 XML 解析失败")
            return web.Response(status=500, text="消息解析失败")

        if msg_type == "text" and content:
            await self._store_reply(from_user, content.strip())

        return web.Response(text="success")

    async def _store_reply(self, user_id: str, content: str):
        """将用户回复写入 Redis，与 _wecom_wait_reply 对接"""
        r = await self._get_redis()

        # 读取当前活跃的 task_id
        active_task = await r.get("opus:active_task")
        task_id = active_task.decode() if active_task else "default"

        key = f"opus:reply:{task_id}"
        await r.set(key, content, ex=3600)  # 1小时过期
        logger.info(f"收到企微回复 [{user_id}] → {key}: {content[:50]}")


# ─── 独立运行入口 ─────────────────────────────────────

def run_server(config: dict, host: str = "0.0.0.0", port: int = 8080):
    """启动 Callback Server"""
    server = WecomCallbackServer(config)
    app = server.create_app()
    logger.info(f"启动企微 Callback Server: {host}:{port}")
    web.run_app(app, host=host, port=port, print=lambda *a: None)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="企微 Callback Server")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=8080, help="监听端口")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    config_path = Path(__file__).parent / args.config
    with open(config_path) as f:
        config = json.load(f)
    run_server(config, host=args.host, port=args.port)
