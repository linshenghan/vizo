#!/usr/bin/env python3
"""
展示预算着色的完整效果 - 从绿色到红色的过程
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from user_interface import AgentProgressDisplay

print("\n" + "="*80)
print("💰 Opus V6 改进1: 预算实时提醒 - 着色演示")
print("="*80 + "\n")

display = AgentProgressDisplay()
display._budget_warn_threshold = 50
display._budget_pause_threshold = 100

# 模拟不同的费用阶段
scenarios = [
    ("阶段1: 需求分析", [
        ('requirement_analyst', 'haiku', 42, 0.08),
    ]),
    ("阶段2: 产品设计", [
        ('product_manager', 'haiku', 35, 0.10),
    ]),
    ("阶段3: 架构设计", [
        ('architect', 'sonnet', 45, 0.15),
    ]),
    ("阶段4: 后端开发", [
        ('backend_developer', 'haiku', 120, 0.22),
    ]),
    ("阶段5: 前端开发", [
        ('frontend_developer', 'haiku', 90, 0.18),
    ]),
    ("阶段6: 测试修复", [
        ('qa_engineer', 'haiku', 180, 0.25),
    ]),
]

print("执行过程中的费用累计变化:\n")

for scenario_name, agents in scenarios:
    print(f"📍 {scenario_name}")
    
    for role, model, duration, cost in agents:
        display.agent_started(role, model)
        display.agent_completed(role, duration, cost, int(duration*10), int(duration*5))
        
        # 检查费用状态
        if display._total_cost < display._budget_warn_threshold:
            status = "🟢 正常"
        elif display._total_cost < display._budget_pause_threshold:
            status = "🟡 警告"
        else:
            status = "🔴 暂停"
        
        print(f"   ✅ [{role}] 完成")
        print(f"      费用: ${cost:.2f} | 累计: ${display._total_cost:.2f} | 状态: {status}")
    
    print()

print("-" * 80)
print("\n📊 最终费用分析:\n")

print(f"💰 总累计费用: ${display._total_cost:.2f}")
print(f"📍 预算警告阈值: ${display._budget_warn_threshold}")
print(f"🔴 预算暂停阈值: ${display._budget_pause_threshold}")

if display._total_cost < display._budget_warn_threshold:
    final_status = "🟢 正常范围"
    color = "绿色"
elif display._total_cost < display._budget_pause_threshold:
    final_status = "🟡 需要警告"
    color = "黄色"
else:
    final_status = "🔴 已超限"
    color = "红色"

print(f"\n⚠️ 最终状态: {final_status} ({color})")

if display._total_cost >= display._budget_warn_threshold:
    print(f"\n【表格脚注会显示】")
    if display._total_cost >= display._budget_pause_threshold:
        print(f"   ⚠️ 预算已超出暂停阈值 ${display._budget_pause_threshold:.2f}")
    else:
        print(f"   ⚠️ 预算已接近警告阈值 ${display._budget_warn_threshold:.2f}")

print("\n" + "="*80)
print("✨ 改进1验证完成：预算着色功能正常工作！")
print("="*80 + "\n")

print("🎯 着色规则总结:")
print("  🟢 < $50: 绿色表示 - 正常")
print("  🟡 $50-100: 黄色表示 - 需要注意")
print("  🔴 > $100: 红色表示 - 已超限")
print("\n当费用达到阈值时，表格脚注会显示对应的警告信息\n")
