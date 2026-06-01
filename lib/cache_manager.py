#!/usr/bin/env python3
"""
缓存管理器 - Redis 缓存操作
Opus 智能协作系统 v3.0
"""

import json
import hashlib
import asyncio
from typing import Optional, Any, Dict
from datetime import datetime
import redis.asyncio as redis


class CacheManager:
    """Redis 缓存管理器"""

    def __init__(self, host: str = "127.0.0.1", port: int = 6380, db: int = 0, password: str = ""):
        self.host = host
        self.port = port
        self.db = db
        self.password = password
        self._client: Optional[redis.Redis] = None

    async def connect(self) -> bool:
        """连接 Redis"""
        try:
            self._client = redis.Redis(
                host=self.host,
                port=self.port,
                db=self.db,
                password=self.password or None,
                decode_responses=True
            )
            await self._client.ping()
            return True
        except Exception as e:
            print(f"[CacheManager] Redis 连接失败: {e}")
            return False

    async def close(self):
        """关闭连接"""
        if self._client:
            await self._client.close()

    @staticmethod
    def hash_key(content: str) -> str:
        """生成缓存键"""
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    # ==================== 任务结果缓存 ====================

    async def get_task_result(self, task_hash: str) -> Optional[Dict]:
        """获取任务缓存结果"""
        if not self._client:
            return None
        try:
            data = await self._client.get(f"task:{task_hash}")
            return json.loads(data) if data else None
        except Exception:
            return None

    async def set_task_result(self, task_hash: str, result: Dict, ttl: int = 3600):
        """缓存任务结果"""
        if not self._client:
            return False
        try:
            result["cached_at"] = datetime.now().isoformat()
            await self._client.setex(
                f"task:{task_hash}",
                ttl,
                json.dumps(result, ensure_ascii=False)
            )
            return True
        except Exception as e:
            print(f"[CacheManager] 缓存写入失败: {e}")
            return False

    # ==================== 会话状态 ====================

    async def save_session_state(self, session_id: str, state: Dict):
        """保存会话状态（用于会话恢复）"""
        if not self._client:
            return False
        try:
            state["saved_at"] = datetime.now().isoformat()
            await self._client.setex(
                f"session:{session_id}",
                86400,  # 24小时
                json.dumps(state, ensure_ascii=False)
            )
            return True
        except Exception:
            return False

    async def get_session_state(self, session_id: str) -> Optional[Dict]:
        """获取会话状态"""
        if not self._client:
            return None
        try:
            data = await self._client.get(f"session:{session_id}")
            return json.loads(data) if data else None
        except Exception:
            return None

    # ==================== 模型额度追踪 ====================

    async def update_quota(self, provider: str, model: str, status: str):
        """更新模型额度状态"""
        if not self._client:
            return
        try:
            key = f"quota:{provider}:{model}"
            data = {
                "status": status,  # "ok" | "limited" | "exhausted"
                "last_check": datetime.now().isoformat()
            }
            await self._client.setex(key, 300, json.dumps(data))  # 5分钟过期
        except Exception:
            pass

    async def get_quota_status(self, provider: str, model: str) -> Optional[str]:
        """获取模型额度状态"""
        if not self._client:
            return None
        try:
            data = await self._client.get(f"quota:{provider}:{model}")
            if data:
                return json.loads(data).get("status")
            return None
        except Exception:
            return None

    # ==================== 通知队列 ====================

    async def push_notification(self, notification: Dict):
        """推送通知到队列"""
        if not self._client:
            return False
        try:
            notification["created_at"] = datetime.now().isoformat()
            await self._client.lpush(
                "notifications:pending",
                json.dumps(notification, ensure_ascii=False)
            )
            return True
        except Exception:
            return False

    async def pop_notification(self) -> Optional[Dict]:
        """获取待处理通知"""
        if not self._client:
            return None
        try:
            data = await self._client.rpop("notifications:pending")
            return json.loads(data) if data else None
        except Exception:
            return None

    # ==================== 用户响应等待 ====================

    async def wait_for_response(self, request_id: str, timeout: int = 300) -> Optional[str]:
        """等待用户响应"""
        if not self._client:
            return None
        try:
            key = f"response:{request_id}"
            # 轮询等待
            for _ in range(timeout):
                data = await self._client.get(key)
                if data:
                    await self._client.delete(key)
                    return data
                await asyncio.sleep(1)
            return None
        except Exception:
            return None

    async def set_response(self, request_id: str, response: str):
        """设置用户响应（由通知系统调用）"""
        if not self._client:
            return False
        try:
            await self._client.setex(f"response:{request_id}", 600, response)
            return True
        except Exception:
            return False


# 全局实例
_cache_manager: Optional[CacheManager] = None


async def get_cache_manager() -> CacheManager:
    """获取缓存管理器单例"""
    global _cache_manager
    if _cache_manager is None:
        _cache_manager = CacheManager()
        await _cache_manager.connect()
    return _cache_manager


# 便捷函数
async def cache_get(task_input: str) -> Optional[Dict]:
    """从缓存获取任务结果"""
    cm = await get_cache_manager()
    task_hash = CacheManager.hash_key(task_input)
    return await cm.get_task_result(task_hash)


async def cache_set(task_input: str, result: Dict, ttl: int = 3600) -> bool:
    """缓存任务结果"""
    cm = await get_cache_manager()
    task_hash = CacheManager.hash_key(task_input)
    return await cm.set_task_result(task_hash, result, ttl)
