#!/usr/bin/env python3
"""
模型路由器 v3.0 - 智能模型选择与降级
Opus 智能协作系统 v3.0
"""

import os
import json
import asyncio
import aiohttp
import subprocess
from typing import Optional, Dict, List, Tuple, Union
from enum import Enum
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _LIB_DIR.parent



class ModelProvider(Enum):
    """模型提供商"""
    MODELSCOPE = "modelscope"
    OLLAMA = "ollama"
    CLAUDE = "claude"


class TaskType(Enum):
    """任务类型"""
    CODE_GEN = "code_gen"       # 代码生成/调试
    DATA_PROC = "data_proc"     # 数据处理/分析
    ALGO = "algo"               # 数学推理/算法
    CONVERT = "convert"         # 格式转换
    TEXT = "text"               # 文本推理/文档
    SYSTEM = "system"           # 系统级/底层
    RESEARCH = "research"       # 资料查询


@dataclass
class ModelResponse:
    """模型响应"""
    success: bool
    content: str
    model: str
    provider: str
    tokens_used: int = 0
    error: str = ""
    fallback_used: bool = False


class ModelRouter:
    """模型路由器"""

    def __init__(self, config_path: str = None):
        if config_path is None:
            config_path = str(_PROJECT_ROOT / 'config.json')

        with open(config_path) as f:
            self.config = json.load(f)

        self._session: Optional[aiohttp.ClientSession] = None
        self._model_status: Dict[str, str] = {}  # "ok", "limited", "exhausted"

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ==================== 模型调用 ====================

    async def call_modelscope(
        self,
        model_name: str,
        messages: List[Dict],
        max_tokens: int = 8192,
        temperature: float = 0.7
    ) -> ModelResponse:
        """调用魔搭模型"""
        provider_config = self.config.get("model_providers", {}).get("modelscope", {})
        models_config = provider_config.get("models", {})
        model_info = models_config.get(model_name)

        # 兼容新旧配置格式
        if model_info is None:
            return ModelResponse(
                success=False,
                content="",
                model=model_name,
                provider="modelscope",
                error=f"未知模型: {model_name}"
            )

        # 新格式: {"id": "xxx", "capabilities": [...], "cost": "free"}
        # 旧格式: "model_id_string"
        if isinstance(model_info, dict):
            model_id = model_info.get("id", "")
        else:
            model_id = model_info

        if not model_id:
            return ModelResponse(
                success=False,
                content="",
                model=model_name,
                provider="modelscope",
                error=f"未知模型: {model_name}"
            )

        api_key = self.config.get("secrets", {}).get("modelscope_api_key", "")
        if not api_key:
            return ModelResponse(
                success=False,
                content="",
                model=model_name,
                provider="modelscope",
                error="ModelScope API Key 未配置"
            )

        session = await self._get_session()
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        data = {
            "model": model_id,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature
        }

        try:
            async with session.post(
                provider_config.get("base_url"),
                headers=headers,
                json=data,
                timeout=aiohttp.ClientTimeout(total=provider_config.get("timeout", 120))
            ) as resp:
                result = await resp.json()

                # 检查错误
                if "error" in result:
                    error_msg = result.get("error", {})
                    if isinstance(error_msg, dict):
                        error_msg = error_msg.get("message", str(error_msg))

                    # 检查是否是额度问题
                    if "quota" in str(error_msg).lower() or "limit" in str(error_msg).lower():
                        self._model_status[model_name] = "exhausted"

                    return ModelResponse(
                        success=False,
                        content="",
                        model=model_name,
                        provider="modelscope",
                        error=str(error_msg)
                    )

                # 成功
                content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                tokens = result.get("usage", {}).get("total_tokens", 0)
                self._model_status[model_name] = "ok"

                return ModelResponse(
                    success=True,
                    content=content,
                    model=model_name,
                    provider="modelscope",
                    tokens_used=tokens
                )

        except asyncio.TimeoutError:
            return ModelResponse(
                success=False,
                content="",
                model=model_name,
                provider="modelscope",
                error="请求超时"
            )
        except Exception as e:
            return ModelResponse(
                success=False,
                content="",
                model=model_name,
                provider="modelscope",
                error=str(e)
            )

    async def call_ollama(
        self,
        model_name: str,
        messages: List[Dict],
        max_tokens: int = 4096
    ) -> ModelResponse:
        """调用本地 Ollama 模型"""
        ollama_config = self.config.get("ollama", {})
        host = ollama_config.get("host", "http://127.0.0.1:11434")

        # 检查 Ollama 是否运行
        if not await self._check_ollama_running():
            return ModelResponse(
                success=False,
                content="",
                model=model_name,
                provider="ollama",
                error="Ollama 未运行"
            )

        session = await self._get_session()
        data = {
            "model": model_name,
            "messages": messages,
            "stream": False,
            "options": {
                "num_predict": max_tokens
            }
        }

        try:
            async with session.post(
                f"{host}/api/chat",
                json=data,
                timeout=aiohttp.ClientTimeout(total=ollama_config.get("timeout", 120))
            ) as resp:
                result = await resp.json()

                if "error" in result:
                    return ModelResponse(
                        success=False,
                        content="",
                        model=model_name,
                        provider="ollama",
                        error=result.get("error", "未知错误")
                    )

                content = result.get("message", {}).get("content", "")
                return ModelResponse(
                    success=True,
                    content=content,
                    model=model_name,
                    provider="ollama"
                )

        except Exception as e:
            return ModelResponse(
                success=False,
                content="",
                model=model_name,
                provider="ollama",
                error=str(e)
            )

    async def _check_ollama_running(self) -> bool:
        """检查 Ollama 是否运行，如果未运行则尝试启动"""
        try:
            ollama_config = self.config.get("ollama", {})
            host = ollama_config.get("host", "http://127.0.0.1:11434")
            session = await self._get_session()
            async with session.get(
                f"{host}/api/tags",
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    return True
        except:
            pass
        
        # Ollama 未运行，尝试启动
        return await self._start_ollama()
    
    async def _start_ollama(self) -> bool:
        """尝试启动本地 Ollama 服务"""
        try:
            ollama_config = self.config.get("ollama", {})
            host = ollama_config.get("host", "http://127.0.0.1:11434")
            
            # 只有本地地址才尝试启动
            if "127.0.0.1" not in host and "localhost" not in host:
                print(f"[ModelRouter] Ollama 配置为远程地址 {host}，无法自动启动")
                return False
            
            print("[ModelRouter] Ollama 未运行，正在尝试启动...")
            
            # 尝试启动 Ollama（后台运行）
            # 方法1: 使用 ollama serve（推荐）
            try:
                process = subprocess.Popen(
                    ["ollama", "serve"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True  # 脱离父进程
                )
                print(f"[ModelRouter] 已启动 ollama serve (PID: {process.pid})")
            except FileNotFoundError:
                # 方法2: 使用 systemctl（如果 ollama 命令不在 PATH 中）
                try:
                    subprocess.run(
                        ["systemctl", "--user", "start", "ollama"],
                        check=True,
                        capture_output=True
                    )
                    print("[ModelRouter] 已通过 systemctl 启动 ollama")
                except Exception as e:
                    print(f"[ModelRouter] systemctl 启动失败: {e}")
                    return False
            
            # 等待 Ollama 启动（最多 30 秒）
            session = await self._get_session()
            for i in range(15):
                await asyncio.sleep(2)
                try:
                    async with session.get(
                        f"{host}/api/tags",
                        timeout=aiohttp.ClientTimeout(total=3)
                    ) as resp:
                        if resp.status == 200:
                            print(f"[ModelRouter] Ollama 启动成功（等待 {(i+1)*2} 秒）")
                            return True
                except:
                    continue
            
            print("[ModelRouter] Ollama 启动超时")
            return False
            
        except Exception as e:
            print(f"[ModelRouter] 启动 Ollama 失败: {e}")
            return False

    # ==================== 智能路由 ====================

    def get_model_for_task(self, task_type: TaskType) -> Tuple[str, str]:
        """
        根据任务类型获取推荐模型

        Returns:
            (model_name, provider)
        """
        task_config = self.config.get("task_types", {}).get(task_type.value, {})
        primary = task_config.get("primary_model", "Qwen2.5-Coder-32B")

        # 检查模型状态
        if self._model_status.get(primary) == "exhausted":
            # 使用降级链
            fallback = task_config.get("fallback", [])
            for model in fallback:
                if model.startswith("ollama:"):
                    return (model.split(":")[1], "ollama")
                if self._model_status.get(model) != "exhausted":
                    return (model, "modelscope")
            # 全部不可用，返回 Ollama
            return ("deepseek-coder:6.7b", "ollama")

        return (primary, "modelscope")

    async def route_and_call(
        self,
        task_type: TaskType,
        messages: Union[List[Dict], str],
        max_tokens: int = 8192
    ) -> ModelResponse:
        """
        智能路由并调用模型

        Args:
            task_type: 任务类型
            messages: 消息列表 List[Dict] 或直接传入 str（会自动包装为 user 消息）
            max_tokens: 最大 token 数

        Returns:
            ModelResponse
        """
        # 兼容字符串输入：自动包装为消息列表
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]

        task_config = self.config.get("task_types", {}).get(task_type.value, {})
        fallback_chain = [task_config.get("primary_model")] + task_config.get("fallback", [])

        for model in fallback_chain:
            if model is None:
                continue

            # 跳过已知不可用的模型
            if self._model_status.get(model) == "exhausted":
                continue

            # Ollama 模型
            if model.startswith("ollama:"):
                ollama_alias = model.split(":")[1]
                # 从配置中查找实际模型名（别名 -> 实际模型名映射）
                ollama_models_config = self.config.get("ollama", {}).get("models", {})
                ollama_model = ollama_models_config.get(ollama_alias, ollama_alias)
                response = await self.call_ollama(ollama_model, messages)
            # Claude 降级
            elif model == "opus":
                # 标记需要 Claude 处理
                return ModelResponse(
                    success=False,
                    content="",
                    model="opus",
                    provider="claude",
                    error="NEED_CLAUDE"
                )
            # ModelScope 模型
            else:
                response = await self.call_modelscope(model, messages, max_tokens)

            if response.success:
                response.fallback_used = (model != fallback_chain[0])
                return response

            # 记录失败
            if "quota" in response.error.lower() or "limit" in response.error.lower():
                self._model_status[model] = "exhausted"

        # 全部失败，返回最后的错误
        return ModelResponse(
            success=False,
            content="",
            model="all",
            provider="none",
            error="所有模型均不可用，请检查网络或额度"
        )

    # ==================== 状态查询 ====================

    def get_status(self) -> Dict:
        """获取路由器状态"""
        return {
            "model_status": self._model_status.copy(),
            "available_models": list(self.config.get("model_providers", {}).get("modelscope", {}).get("models", {}).keys()),
            "ollama_models": list(self.config.get("ollama", {}).get("models", {}).values())
        }

    async def check_all_models(self) -> Dict[str, str]:
        """检查所有模型可用性"""
        status = {}
        test_messages = [{"role": "user", "content": "Say 'OK' if you can receive this."}]

        # 检查 ModelScope 模型
        for model_name in self.config.get("model_providers", {}).get("modelscope", {}).get("models", {}).keys():
            response = await self.call_modelscope(model_name, test_messages, max_tokens=10)
            status[f"modelscope:{model_name}"] = "ok" if response.success else response.error

        # 检查 Ollama
        if await self._check_ollama_running():
            for alias, model in self.config.get("ollama", {}).get("models", {}).items():
                response = await self.call_ollama(model, test_messages)
                status[f"ollama:{model}"] = "ok" if response.success else response.error
        else:
            status["ollama"] = "not running"

        return status


# 全局实例
_router: Optional[ModelRouter] = None


def get_router() -> ModelRouter:
    """获取路由器单例"""
    global _router
    if _router is None:
        _router = ModelRouter()
    return _router


# 便捷函数
async def smart_call(task_type: str, prompt: str, context: str = "") -> ModelResponse:
    """智能调用模型"""
    router = get_router()
    messages = []
    if context:
        messages.append({"role": "system", "content": context})
    messages.append({"role": "user", "content": prompt})

    task_type_enum = TaskType(task_type)
    return await router.route_and_call(task_type_enum, messages)
