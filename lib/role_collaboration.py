#!/usr/bin/env python3
"""
多角色协同模块 - Opus 智能协作系统 v5.0

核心原则：所有角色都由 Claude Opus 扮演，而非委派给外部模型。
只有 Opus 拥有项目的完整上下文（代码、MCP、Git、记忆、知识库），
外部模型对项目一无所知，让它们扮演"后端工程师"审查代码是伪协作。

使用方式：
1. prompt_enhancer.py 检测用户输入 → 调用 detect_collaboration_mode()
2. 匹配到模式后 → 调用 generate_instruction() 生成提示词
3. 提示词注入给 Opus → Opus 按步骤切换视角执行
"""

import re
import json
import argparse
import sys
from typing import Optional, List, Dict


# ==================== 角色定义 ====================

ROLE_DEFINITIONS = {
    "product_manager": {
        "title": "产品经理",
        "responsibility": "需求分析、功能拆分、用户场景、验收标准定义",
        "focus_areas": ["用户价值", "功能边界", "MVP定义", "验收条件"],
        "output_format": "PRD摘要：功能列表 + 验收标准（每条不超过1句话）",
        "always_participate": True,
    },
    "frontend_dev": {
        "title": "前端研发工程师",
        "responsibility": "UI/UX设计、组件方案、交互逻辑、前端架构",
        "focus_areas": ["组件复用", "用户交互", "响应式设计", "前端性能"],
        "output_format": "技术方案：组件清单 + 交互说明 + 改动文件",
        "always_participate": False,
        "trigger_patterns": [
            r"(页面|界面|UI|前端|组件|样式|Vue|React|Element|按钮|表格|弹窗)",
            r"(CSS|HTML|模板|布局|导航|菜单|表单|输入框|下拉)",
        ],
    },
    "backend_dev": {
        "title": "后端研发工程师",
        "responsibility": "API设计、数据库方案、业务逻辑、服务架构",
        "focus_areas": ["API设计", "数据模型", "性能优化", "安全性"],
        "output_format": "技术方案：API清单 + 数据库变更 + 改动文件",
        "always_participate": False,
        "trigger_patterns": [
            r"(后端|API|接口|数据库|服务|SQL|Java|Python|Spring|Docker)",
            r"(Redis|MySQL|MongoDB|微服务|中间件|消息队列|缓存)",
        ],
    },
    "embedded_dev": {
        "title": "嵌入式硬件全栈工程师",
        "responsibility": "ESP32固件、硬件协议、音频处理、通信方案",
        "focus_areas": ["资源优化", "实时性", "协议设计", "功耗管理"],
        "output_format": "技术方案：固件改动 + 协议说明 + 硬件约束",
        "always_participate": False,
        "trigger_patterns": [
            r"(ESP32|硬件|固件|嵌入式|GPIO|串口|音频|语音|MQTT|WiFi)",
            r"(I2C|SPI|UART|ADC|DAC|PWM|中断|定时器|看门狗)",
        ],
    },
    "qa_engineer": {
        "title": "测试工程师",
        "responsibility": "测试用例设计、边界条件分析、回归测试方案",
        "focus_areas": ["边界条件", "异常场景", "兼容性", "回归风险"],
        "output_format": "测试方案：测试用例清单（正常+异常+边界）",
        "always_participate": True,
    },
}


# ==================== 触发模式检测 ====================

# 强触发：流水线模式（新功能开发、重大重构）
STRONG_COLLAB_PATTERNS = [
    # 开发术语版本
    r"(加|添加|新增|实现|开发|创建).{0,10}(功能|模块|页面|API|接口|服务)",
    r"(设计|重构|重写|迁移).{0,10}(系统|架构|模块|方案)",
    r"(完成|做).{0,10}(需求|任务|功能)",
    # 产品经理自然语言版本（不含技术术语）
    r"(我想让|让|能不能让|希望).{0,20}(能|可以|支持|会|记住|学会)",
    r"(给|帮).{0,10}(做|加|弄|搞).{0,10}(一个|个)",
    r"(需要|要).{0,5}(支持|能够|可以)",
    r"(做|加|弄)(一个|个).{2,}",
    r"(改成|换成|升级到|迁移到)",
]

# 弱触发：评审模式（优化、改进、复杂修复）
WEAK_COLLAB_PATTERNS = [
    # 开发术语版本
    r"(优化|改进|增强|升级).{0,10}(功能|性能|体验|方案|效率|速度)",
    r"(修复|解决).{0,10}(复杂|严重|多个|疑难)",
    r"(分析|评估).{0,10}(方案|架构|可行性)",
    # 产品经理自然语言版本
    r"(太慢|太小|太大|太卡|不好用|体验差|不稳定)",
    r"(怎么不能|为什么不能|无法|不支持)",
    r"(连不上|用不了|打不开|没反应|出错)",
    r".{2,}(有问题|有bug|不正常|异常)",
]

# 排除模式：不触发多角色
EXCLUDE_COLLAB_PATTERNS = [
    r"^(看|查|读|找|搜索|分析|理解|了解)",
    r"^(日报|记忆|回忆|总结|托管|远程|写日报)",
    r"(直接改|简单修|快速修|改个字|改一下)",
    r"^(帮我看|什么意思|怎么回事|为什么)",
    r"(git|commit|push|pull|merge)",
]


def detect_collaboration_mode(user_input: str) -> Optional[str]:
    """
    检测用户输入是否触发多角色协同

    Returns:
        "pipeline" - 流水线模式（强触发）
        "review"   - 评审模式（弱触发）
        None       - 不触发
    """
    if not user_input or len(user_input) < 4:
        return None

    # 先检查排除模式
    for pattern in EXCLUDE_COLLAB_PATTERNS:
        if re.search(pattern, user_input):
            return None

    # 检查强触发
    for pattern in STRONG_COLLAB_PATTERNS:
        if re.search(pattern, user_input):
            return "pipeline"

    # 检查弱触发
    for pattern in WEAK_COLLAB_PATTERNS:
        if re.search(pattern, user_input):
            return "review"

    return None


def select_roles(requirement: str) -> List[str]:
    """
    根据需求内容选择参与的角色

    始终参与：product_manager, qa_engineer
    关键词触发：frontend_dev, backend_dev, embedded_dev
    """
    roles = []

    for role_id, role_def in ROLE_DEFINITIONS.items():
        if role_def.get("always_participate"):
            roles.append(role_id)
            continue

        patterns = role_def.get("trigger_patterns", [])
        for pattern in patterns:
            if re.search(pattern, requirement, re.IGNORECASE):
                roles.append(role_id)
                break

    # 如果没有触发任何技术角色，默认加入 backend_dev
    tech_roles = {"frontend_dev", "backend_dev", "embedded_dev"}
    if not tech_roles.intersection(set(roles)):
        roles.append("backend_dev")

    return roles


# ==================== 指令生成 ====================

def generate_role_prompt(role: str, requirement: str) -> str:
    """生成单个角色的思考提示词"""
    role_def = ROLE_DEFINITIONS[role]
    return (
        f"**{role_def['title']}视角**\n"
        f"职责: {role_def['responsibility']}\n"
        f"关注: {', '.join(role_def['focus_areas'])}\n"
        f"输出: {role_def['output_format']}"
    )


def generate_pipeline_instruction(requirement: str, roles: List[str]) -> str:
    """
    生成流水线模式指令（注入给 Opus）

    Opus 按阶段切换视角执行，全程使用项目工具（Serena/Git/Memory）
    """
    role_prompts = []
    for i, role in enumerate(roles, 1):
        role_def = ROLE_DEFINITIONS.get(role)
        if not role_def:
            continue
        role_prompts.append(
            f"### 阶段{i}: {role_def['title']}视角\n"
            f"- **职责**: {role_def['responsibility']}\n"
            f"- **关注**: {', '.join(role_def['focus_areas'])}\n"
            f"- **输出**: {role_def['output_format']}\n"
            f"- **工具**: 使用 Serena MCP 分析代码结构，读取项目记忆获取上下文"
        )

    roles_text = "\n\n".join(role_prompts)
    total = len(role_prompts)

    return f"""<system-reminder>
🔄 **多角色协同 - 流水线模式已激活**

用户需求: {requirement}

请按以下流程，依次切换不同专业视角完成分析。每个阶段你需要充分利用项目工具（Serena MCP、Git、项目记忆）进行深度分析，而非凭空推断。

{roles_text}

### 阶段{total + 1}: 统筹决策
- **职责**: 综合以上所有视角的分析，解决冲突，制定最终实施方案
- **输出**: 最终方案（含改动文件清单 + 实施步骤）
- **决策**: 如果方案涉及较大改动，生成预览文档发送到用户手机确认后再执行

**执行规则**:
1. 每个阶段开始时明确标注当前视角
2. 使用 Serena MCP (`mcp__serena__find_symbol` 等) 分析现有代码
3. 阶段间结论要衔接，后续阶段可引用前序阶段结论
4. 最终统筹阶段做出取舍决策，而非简单拼凑
</system-reminder>"""


def generate_review_instruction(requirement: str, roles: List[str]) -> str:
    """
    生成评审模式指令（注入给 Opus）

    Opus 从多个视角审视方案，提出改进建议
    """
    perspectives = []
    for role in roles:
        role_def = ROLE_DEFINITIONS.get(role)
        if not role_def:
            continue
        perspectives.append(
            f"- **{role_def['title']}**: 关注{', '.join(role_def['focus_areas'][:2])}"
        )

    perspectives_text = "\n".join(perspectives)

    return f"""<system-reminder>
🔍 **多角色协同 - 评审模式已激活**

用户需求: {requirement}

请从以下专业视角依次审视需求/方案，给出专业意见：

{perspectives_text}

**执行规则**:
1. 每个视角给出 2-3 条核心意见（不要泛泛而谈）
2. 使用 Serena MCP 验证代码相关的判断
3. 最后综合所有视角，给出明确的行动建议
</system-reminder>"""


def generate_instruction(mode: str, requirement: str, roles: List[str]) -> str:
    """根据模式生成对应指令"""
    if mode == "pipeline":
        return generate_pipeline_instruction(requirement, roles)
    elif mode == "review":
        return generate_review_instruction(requirement, roles)
    return ""


# ==================== CLI 接口 ====================

def main():
    parser = argparse.ArgumentParser(description="多角色协同模块")
    subparsers = parser.add_subparsers(dest="command")

    # detect 子命令
    detect_parser = subparsers.add_parser("detect", help="检测协同模式")
    detect_parser.add_argument("--requirement", "-r", required=True, help="用户输入")

    # generate 子命令
    gen_parser = subparsers.add_parser("generate", help="生成协同指令")
    gen_parser.add_argument("--mode", "-m", required=True, choices=["pipeline", "review"])
    gen_parser.add_argument("--requirement", "-r", required=True, help="用户需求")
    gen_parser.add_argument("--roles", help="角色列表(逗号分隔)，不指定则自动选择")

    args = parser.parse_args()

    if args.command == "detect":
        mode = detect_collaboration_mode(args.requirement)
        if mode:
            roles = select_roles(args.requirement)
            result = {"mode": mode, "roles": roles}
        else:
            result = {"mode": None, "roles": []}
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == "generate":
        if args.roles:
            roles = [r.strip() for r in args.roles.split(",")]
        else:
            roles = select_roles(args.requirement)
        instruction = generate_instruction(args.mode, args.requirement, roles)
        print(instruction)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
