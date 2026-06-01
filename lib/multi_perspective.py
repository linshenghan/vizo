#!/usr/bin/env python3
"""
多视角分析模块 - 调用不同模型扮演不同角色分析需求
Opus 智能协作系统 v4.0
"""

import asyncio
import sys
from pathlib import Path
from typing import List, Dict, Optional

sys.path.insert(0, str(Path(__file__).parent))

from model_router import get_router, TaskType


# 角色定义和 Prompt 模板
PERSPECTIVES = {
    "product_manager": {
        "name": "产品经理",
        "model_hint": TaskType.TEXT_SUMMARY,  # 使用 Qwen2.5-72B
        "prompt_template": """你是一位经验丰富的产品经理，请从产品角度分析以下需求：

## 需求描述
{requirement}

## 请分析以下方面（简洁，每点 1-2 句话）：

1. **用户价值**：这个功能对用户有什么价值？
2. **功能完整性**：需求描述是否完整？是否有遗漏的场景？
3. **边界情况**：有哪些边界情况需要考虑？
4. **优先级建议**：核心功能 vs 可选功能如何划分？
5. **潜在风险**：从产品角度有什么风险？

请直接给出分析，不要重复需求描述。"""
    },

    "architect": {
        "name": "架构师",
        "model_hint": TaskType.CODE_GEN,  # 使用 Qwen3-Coder
        "prompt_template": """你是一位资深软件架构师，请从技术架构角度分析以下需求：

## 需求描述
{requirement}

## 请分析以下方面（简洁，每点 1-2 句话）：

1. **技术可行性**：实现难度如何？有技术障碍吗？
2. **架构设计**：建议的模块划分和数据流？
3. **技术选型**：推荐使用什么技术/库？
4. **扩展性**：设计如何支持未来扩展？
5. **性能考虑**：有什么性能瓶颈需要注意？

请直接给出分析，不要重复需求描述。"""
    },

    "frontend_expert": {
        "name": "前端专家",
        "model_hint": TaskType.CODE_GEN,  # 使用 Qwen3-Coder
        "prompt_template": """你是一位前端技术专家，请从前端实现角度分析以下需求：

## 需求描述
{requirement}

## 请分析以下方面（简洁，每点 1-2 句话）：

1. **UI/UX 设计**：界面布局和交互设计建议？
2. **响应式**：如何适配不同屏幕？
3. **组件拆分**：建议的组件结构？
4. **状态管理**：数据流和状态如何管理？
5. **用户体验细节**：loading、错误处理、动画等细节？

请直接给出分析，不要重复需求描述。如果需求不涉及前端，请简要说明。"""
    },

    "reviewer": {
        "name": "审查员",
        "model_hint": TaskType.TEXT_SUMMARY,  # 使用 Qwen2.5-72B
        "prompt_template": """你是一位严谨的技术审查员，请从风险和问题角度分析以下需求：

## 需求描述
{requirement}

## 请重点分析（简洁，每点 1-2 句话）：

1. **需求遗漏**：有什么没考虑到的情况？
2. **潜在 Bug**：可能出现什么问题？
3. **安全风险**：有安全隐患吗？
4. **维护成本**：长期维护有什么挑战？
5. **建议**：有什么改进建议？

请直接给出分析，以批判性思维找出潜在问题。"""
    },

    "user": {
        "name": "最终用户",
        "model_hint": TaskType.TEXT_SUMMARY,
        "prompt_template": """你是这个功能的最终用户，请从使用者角度分析以下需求：

## 需求描述
{requirement}

## 请回答（简洁，每点 1-2 句话）：

1. **第一印象**：看到这个功能描述，你的第一反应是什么？
2. **使用场景**：你会在什么情况下使用？
3. **期望**：你期望它能做什么？
4. **担忧**：你有什么担忧或疑虑？
5. **建议**：你希望它还能有什么功能？

请直接给出分析，以普通用户视角回答。"""
    }
}


async def analyze_single_perspective(
    router,
    requirement: str,
    perspective_key: str
) -> Dict:
    """单个视角分析"""
    perspective = PERSPECTIVES.get(perspective_key)
    if not perspective:
        return {"perspective": perspective_key, "error": "未知视角"}

    prompt = perspective["prompt_template"].format(requirement=requirement)

    try:
        result = await router.route_and_call(
            perspective["model_hint"],
            prompt
        )
        return {
            "perspective": perspective_key,
            "name": perspective["name"],
            "analysis": result.content,
            "model": result.model_used
        }
    except Exception as e:
        return {
            "perspective": perspective_key,
            "name": perspective["name"],
            "error": str(e)
        }


async def analyze_from_perspectives(
    requirement: str,
    perspectives: List[str] = None
) -> str:
    """
    从多个视角分析需求

    Args:
        requirement: 需求描述
        perspectives: 要使用的视角列表，默认全部

    Returns:
        格式化的分析结果
    """
    if perspectives is None:
        perspectives = ["product_manager", "architect", "frontend_expert", "reviewer"]

    router = get_router()

    try:
        # 并行调用所有视角
        tasks = [
            analyze_single_perspective(router, requirement, p)
            for p in perspectives
        ]
        results = await asyncio.gather(*tasks)

        # 格式化输出
        output = []
        output.append("## 多视角分析结果\n")

        for result in results:
            name = result.get("name", result["perspective"])
            output.append(f"### {name}视角")
            if "error" in result:
                output.append(f"*分析失败: {result['error']}*\n")
            else:
                output.append(result["analysis"])
                output.append(f"*（模型: {result.get('model', 'unknown')}）*\n")

        # 添加综合建议提示
        output.append("---\n")
        output.append("### 综合分析")
        output.append("*请 Opus 根据以上各视角分析，综合给出推荐方案。*")

        return "\n\n".join(output)

    finally:
        await router.close()


async def quick_analyze(requirement: str) -> str:
    """快速分析（只使用产品经理和架构师视角）"""
    return await analyze_from_perspectives(
        requirement,
        perspectives=["product_manager", "architect"]
    )


# CLI 支持
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="多视角需求分析")
    parser.add_argument("requirement", nargs="?", help="需求描述（也可通过 stdin 输入）")
    parser.add_argument("--perspectives", "-p", default="",
                        help="视角列表，逗号分隔（默认全部）")
    parser.add_argument("--quick", "-q", action="store_true",
                        help="快速模式（只用产品经理+架构师）")
    args = parser.parse_args()

    # 获取需求描述
    if args.requirement:
        requirement = args.requirement
    else:
        print("请输入需求描述（Ctrl+D 结束）:", file=sys.stderr)
        requirement = sys.stdin.read().strip()

    if not requirement:
        print("错误: 需求描述不能为空", file=sys.stderr)
        sys.exit(1)

    # 确定视角
    if args.quick:
        perspectives = ["product_manager", "architect"]
    elif args.perspectives:
        perspectives = [p.strip() for p in args.perspectives.split(",")]
    else:
        perspectives = None  # 使用默认

    # 执行分析
    result = asyncio.run(analyze_from_perspectives(requirement, perspectives))
    print(result)
