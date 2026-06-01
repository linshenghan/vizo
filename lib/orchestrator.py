#!/usr/bin/env python3
"""
任务调度中心 - Opus 智能协作系统核心
Opus 智能协作系统 v5.0

v5.0 更新：
- 明确外部模型只做脱离项目上下文的通用子任务
- 添加白名单/黑名单机制
- 涉及项目代码/架构/上下文的工作由 Opus 自己完成
"""

import os
import re
import json
import uuid
import asyncio
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime

from .cache_manager import CacheManager, cache_get, cache_set
from .model_router import ModelRouter, TaskType, ModelResponse, get_router
from .notification import NotificationManager, NotificationType, get_notification_manager
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _LIB_DIR.parent

# memory_system 已在 v2.5 重构中删除，改用 Serena MCP
# from .memory_system import MemorySystem, get_memory_system


# ==================== 委派策略 ====================
# 可委派给外部模型的任务类型（白名单）
# 这些任务不需要项目上下文，外部模型可以独立完成
DELEGATABLE_TASK_TYPES = {
    TaskType.TEXT,       # 文本总结/摘要（给定文本即可）
    TaskType.CONVERT,    # 格式转换（JSON/YAML/CSV互转）
    TaskType.RESEARCH,   # 联网搜索（技术方案、API文档）
    TaskType.ALGO,       # 纯数学/算法推理（不涉及项目逻辑）
}

# 禁止委派的任务类型（黑名单）
# 这些任务需要项目上下文（代码、MCP、Git、记忆），只有 Opus 能做
NON_DELEGATABLE_TASK_TYPES = {
    TaskType.CODE_GEN,   # 代码审查/生成/修复（需要理解项目代码）
    TaskType.SYSTEM,     # 系统级开发（需要理解架构）
    TaskType.DATA_PROC,  # 数据处理（可能涉及项目数据结构）
}


class TaskStatus(Enum):
    """任务状态"""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_AUTH = "needs_auth"
    NEEDS_CONFIRM = "needs_confirm"
    NEEDS_INFO = "needs_info"
    PAUSED = "paused"


@dataclass
class AtomicTask:
    """原子任务"""
    id: str
    type: TaskType
    instruction: str
    context: str = ""
    constraints: List[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    result: str = ""
    error: str = ""
    retry_count: int = 0
    executor: str = ""  # 实际执行的模型
    cache_key: str = ""


@dataclass
class OrchestratorResult:
    """调度结果"""
    success: bool
    tasks: List[AtomicTask]
    summary: str
    opus_tokens_used: int = 0
    router_tokens_used: int = 0
    from_cache: bool = False


class TaskClassifier:
    """任务分类器 - 基于关键词和模式"""

    PATTERNS = {
        TaskType.CODE_GEN: [
            r"写|编写|实现|开发|创建|添加|生成.*代码",
            r"write|implement|develop|create|add|generate.*code",
            r"函数|类|方法|接口|模块|组件",
            r"function|class|method|interface|module|component",
            r"调试|debug|修复|fix|bug"
        ],
        TaskType.DATA_PROC: [
            r"数据|处理|分析|统计|聚合|过滤|排序",
            r"data|process|analyze|statistics|aggregate|filter|sort",
            r"JSON|XML|CSV|Excel|数据库"
        ],
        TaskType.ALGO: [
            r"算法|推理|计算|数学|公式|证明",
            r"algorithm|reasoning|calculate|math|formula|proof",
            r"复杂度|优化|效率|性能分析"
        ],
        TaskType.CONVERT: [
            r"转换|格式化|转成|变成|翻译",
            r"convert|format|transform|translate",
            r"JSON.*YAML|YAML.*JSON|XML.*JSON"
        ],
        TaskType.TEXT: [
            r"解释|说明|文档|注释|描述",
            r"explain|describe|document|comment",
            r"总结|摘要|概括|summarize"
        ],
        TaskType.SYSTEM: [
            r"系统|底层|内核|驱动|嵌入式",
            r"system|kernel|driver|embedded",
            r"ESP32|Arduino|GPIO|串口|协议"
        ],
        TaskType.RESEARCH: [
            r"搜索|查找|研究|调研|资料",
            r"search|find|research|investigate",
            r"github|开源|文档|API"
        ]
    }

    @classmethod
    def classify(cls, task_description: str) -> TaskType:
        """分类任务"""
        scores = {t: 0 for t in TaskType}

        for task_type, patterns in cls.PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, task_description, re.IGNORECASE):
                    scores[task_type] += 1

        # 找出最高分
        max_type = max(scores, key=scores.get)
        if scores[max_type] > 0:
            return max_type

        # 默认为代码生成
        return TaskType.CODE_GEN


class Orchestrator:
    """任务调度中心"""

    # Opus 输出的 JSON 指令模板
    OPUS_PROMPT_TEMPLATE = """你是一个任务分解专家。请将用户需求分解为原子化子任务。

## 输出格式（严格 JSON，不要任何解释）:
```json
{
  "tasks": [
    {
      "type": "code_gen|data_proc|algo|convert|text|system|research",
      "instruction": "具体执行指令（简洁明确）",
      "context": "必要上下文（最小化）",
      "constraints": ["约束1", "约束2"]
    }
  ],
  "needs_opus": false,
  "opus_reason": ""
}
```

## 任务类型说明:
- code_gen: 代码生成、调试、修复
- data_proc: 数据处理、分析
- algo: 算法设计、数学推理
- convert: 格式转换
- text: 文本处理、文档
- system: 系统级、底层开发
- research: 资料查询、GitHub 分析

## 规则:
1. 每个子任务只能是一个独立的原子操作
2. instruction 必须清晰、可执行
3. 如果任务需要深度思考/架构设计/复杂决策，设置 needs_opus=true
4. 不要添加任何解释或注释

## 用户需求:
{user_input}

## 执行上下文:
{context}
"""

    def __init__(self, config_path: str = None):
        if config_path is None:
            config_path = str(_PROJECT_ROOT / 'config.json')

        with open(config_path) as f:
            self.config = json.load(f)

        self.router = get_router()
        self.notifier = get_notification_manager(self.config)
        # memory_system 已删除，改用 Serena MCP：mcp__serena__read_memory()
        # self.memory = get_memory_system()
        self.cache = CacheManager(
            host=self.config.get("redis", {}).get("host", "127.0.0.1"),
            port=self.config.get("redis", {}).get("port", 6380)
        )

    async def initialize(self):
        """初始化（连接 Redis 等）"""
        await self.cache.connect()

    async def close(self):
        """清理资源"""
        await self.cache.close()
        await self.router.close()

    # ==================== 核心流程 ====================

    async def process(
        self,
        user_input: str,
        context: str = "",
        project: str = "default",
        use_cache: bool = True
    ) -> OrchestratorResult:
        """
        处理用户输入

        Args:
            user_input: 用户输入
            context: 上下文信息
            project: 项目名称
            use_cache: 是否使用缓存

        Returns:
            OrchestratorResult
        """
        # 记录到记忆系统
        self.memory.record_message("user", user_input)

        # 1. 检查缓存
        if use_cache:
            cached = await cache_get(user_input)
            if cached:
                return OrchestratorResult(
                    success=True,
                    tasks=[],
                    summary=cached.get("summary", ""),
                    from_cache=True
                )

        # 2. 快速分类（本地判断是否需要 Opus 分解）
        task_type = TaskClassifier.classify(user_input)
        complexity = self._estimate_complexity(user_input)

        # 3. 简单任务直接执行
        if complexity < 0.3:
            return await self._execute_simple_task(user_input, task_type, context)

        # 4. 复杂任务需要 Opus 分解（这里返回需要 Opus 的标记）
        # 实际的 Opus 调用由 Claude Code 本身完成
        return await self._decompose_and_execute(user_input, context, project)

    def _estimate_complexity(self, user_input: str) -> float:
        """估算任务复杂度 (0-1)"""
        score = 0.3  # 基准

        # 长度因素
        if len(user_input) > 200:
            score += 0.2
        if len(user_input) > 500:
            score += 0.2

        # 复杂度关键词
        complex_keywords = [
            "分析", "设计", "架构", "重构", "优化", "比较", "评估",
            "analyze", "design", "architecture", "refactor", "optimize"
        ]
        for kw in complex_keywords:
            if kw in user_input.lower():
                score += 0.1

        # 多步骤指示
        if re.search(r"(首先|然后|接着|最后|第[一二三四五]步)", user_input):
            score += 0.2
        if re.search(r"(first|then|next|finally|step \d)", user_input, re.I):
            score += 0.2

        return min(1.0, score)

    async def _execute_simple_task(
        self,
        user_input: str,
        task_type: TaskType,
        context: str
    ) -> OrchestratorResult:
        """执行简单任务"""
        task = AtomicTask(
            id=str(uuid.uuid4())[:8],
            type=task_type,
            instruction=user_input,
            context=context,
            status=TaskStatus.IN_PROGRESS
        )

        # 调用模型
        response = await self.router.route_and_call(
            task_type,
            [{"role": "user", "content": user_input}]
        )

        if response.success:
            task.status = TaskStatus.COMPLETED
            task.result = response.content
            task.executor = f"{response.provider}:{response.model}"
        else:
            # 检查是否需要 Claude 处理
            if response.error == "NEED_CLAUDE":
                task.status = TaskStatus.PENDING
                task.error = "NEED_OPUS"
            else:
                task.status = TaskStatus.FAILED
                task.error = response.error

        # 记录
        self.memory.record_task(user_input[:50], task.status.value, task.result[:100])

        return OrchestratorResult(
            success=task.status == TaskStatus.COMPLETED,
            tasks=[task],
            summary=task.result if task.status == TaskStatus.COMPLETED else task.error,
            router_tokens_used=response.tokens_used
        )

    async def _decompose_and_execute(
        self,
        user_input: str,
        context: str,
        project: str
    ) -> OrchestratorResult:
        """
        分解并执行复杂任务

        注意：实际的任务分解由 Claude Code (Opus) 完成
        这里返回分解请求，让 Opus 处理
        """
        # 返回需要 Opus 分解的标记
        return OrchestratorResult(
            success=False,
            tasks=[],
            summary="NEED_OPUS_DECOMPOSE",
            opus_tokens_used=0
        )

    async def execute_decomposed_tasks(
        self,
        tasks_json: str,
        project: str = "default"
    ) -> OrchestratorResult:
        """
        执行已分解的任务（由 Opus 分解后调用）

        Args:
            tasks_json: Opus 输出的 JSON 任务列表
            project: 项目名称
        """
        try:
            data = json.loads(tasks_json)
        except json.JSONDecodeError as e:
            return OrchestratorResult(
                success=False,
                tasks=[],
                summary=f"JSON 解析失败: {e}"
            )

        tasks_data = data.get("tasks", [])
        needs_opus = data.get("needs_opus", False)
        opus_reason = data.get("opus_reason", "")

        if needs_opus:
            # 需要 Opus 直接处理
            return OrchestratorResult(
                success=False,
                tasks=[],
                summary=f"NEED_OPUS_DIRECT: {opus_reason}"
            )

        # 执行每个子任务
        tasks = []
        total_router_tokens = 0

        for i, task_data in enumerate(tasks_data):
            task = AtomicTask(
                id=str(uuid.uuid4())[:8],
                type=TaskType(task_data.get("type", "code_gen")),
                instruction=task_data.get("instruction", ""),
                context=task_data.get("context", ""),
                constraints=task_data.get("constraints", []),
                status=TaskStatus.IN_PROGRESS
            )

            # 执行任务
            response = await self.router.route_and_call(
                task.type,
                [
                    {"role": "system", "content": task.context} if task.context else None,
                    {"role": "user", "content": task.instruction}
                ]
            )

            if response.success:
                task.status = TaskStatus.COMPLETED
                task.result = response.content
                task.executor = f"{response.provider}:{response.model}"
                total_router_tokens += response.tokens_used
            else:
                # 失败，检查是否重试
                if task.retry_count < self.config.get("orchestrator", {}).get("max_retries", 1):
                    task.retry_count += 1
                    # 重试一次
                    response = await self.router.route_and_call(task.type, [{"role": "user", "content": task.instruction}])
                    if response.success:
                        task.status = TaskStatus.COMPLETED
                        task.result = response.content
                        task.executor = f"{response.provider}:{response.model}"
                        total_router_tokens += response.tokens_used
                    else:
                        task.status = TaskStatus.FAILED
                        task.error = response.error
                else:
                    task.status = TaskStatus.FAILED
                    task.error = response.error

            tasks.append(task)
            self.memory.record_task(task.instruction[:50], task.status.value)

        # 汇总结果
        success = all(t.status == TaskStatus.COMPLETED for t in tasks)
        summary = "\n\n".join([
            f"### 任务 {i+1}: {t.instruction[:30]}...\n{t.result if t.status == TaskStatus.COMPLETED else f'失败: {t.error}'}"
            for i, t in enumerate(tasks)
        ])

        # 缓存成功结果
        if success:
            await cache_set(
                json.dumps(tasks_data),
                {"summary": summary, "tasks": [t.__dict__ for t in tasks]}
            )

        return OrchestratorResult(
            success=success,
            tasks=tasks,
            summary=summary,
            router_tokens_used=total_router_tokens
        )

    # ==================== 交互处理 ====================

    async def handle_auth_required(self, resource: str, reason: str) -> bool:
        """处理需要授权的情况"""
        return await self.notifier.request_auth(resource, reason)

    async def handle_confirm_needed(self, action: str, details: str) -> bool:
        """处理需要确认的情况"""
        return await self.notifier.request_confirm(action, details)

    async def handle_info_needed(self, what: str, prompt: str) -> Optional[str]:
        """处理需要补充信息的情况"""
        return await self.notifier.request_info(what, prompt)

    async def report_progress(self, task: str, progress: int, details: str = ""):
        """汇报进度"""
        await self.notifier.report_progress(task, progress, details)

    # ==================== 会话管理 ====================

    def save_session(self, project: str, summary: str = ""):
        """保存当前会话"""
        return self.memory.save_session(project, summary)

    def recall_project(self, project: str) -> str:
        """回忆项目"""
        return self.memory.recall_last_session(project) or f"项目 '{project}' 暂无历史记录"


# 全局实例
_orchestrator: Optional[Orchestrator] = None


async def get_orchestrator() -> Orchestrator:
    """获取调度器单例"""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
        await _orchestrator.initialize()
    return _orchestrator


# ==================== 便捷入口函数 ====================

async def smart_process(user_input: str, context: str = "", project: str = "default") -> str:
    """
    智能处理用户输入 - 主入口

    这是供 Claude Code 调用的主函数
    """
    orch = await get_orchestrator()
    result = await orch.process(user_input, context, project)

    if result.from_cache:
        return f"[缓存命中]\n{result.summary}"

    if result.summary == "NEED_OPUS_DECOMPOSE":
        # 返回分解模板，让 Opus 填充
        return Orchestrator.OPUS_PROMPT_TEMPLATE.format(
            user_input=user_input,
            context=context
        )

    if result.summary.startswith("NEED_OPUS_DIRECT"):
        reason = result.summary.replace("NEED_OPUS_DIRECT: ", "")
        return f"[需要 Opus 直接处理]\n原因: {reason}"

    return result.summary


async def execute_tasks(tasks_json: str, project: str = "default") -> str:
    """执行已分解的任务"""
    orch = await get_orchestrator()
    result = await orch.execute_decomposed_tasks(tasks_json, project)
    return result.summary


def recall(project: str) -> str:
    """回忆项目 - v2.5 已删除，改用 Serena MCP"""
    # memory_system 已在 v2.5 重构中删除
    # 使用 Serena MCP 替代：mcp__serena__read_memory(project + "_memory")
    return f"项目 '{project}' 记忆系统已迁移到 Serena MCP，请使用 mcp__serena__read_memory()"
