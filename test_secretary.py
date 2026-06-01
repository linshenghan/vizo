#!/usr/bin/env python3
"""
秘书进程快速测试脚本 - 模拟企微消息并验证功能
"""

import json
import asyncio
import redis.asyncio as aioredis
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent

async def test_secretary():
    """测试秘书进程的核心功能"""
    
    # 连接 Redis
    r = aioredis.from_url("redis://localhost:6379")
    
    print("=" * 60)
    print("🤖 Opus V6 秘书进程功能测试")
    print("=" * 60)
    
    # 测试 1：简单问答
    print("\n[测试 1] 简单问答")
    print("  发送: Python 是什么")
    await r.set("opus:reply:default", "Python 是什么")
    print("  ✓ 消息已发送到 Redis")
    
    # 等待秘书处理
    print("  ⏳ 等待秘书响应...")
    await asyncio.sleep(3)
    
    # 检查秘书是否处理
    msg = await r.get("opus:reply:default")
    if msg is None:
        print("  ✅ 秘书已接收并开始处理")
    else:
        print("  ❌ 秘书未接收消息（Redis 仍有消息）")
    
    # 测试 2：状态查询
    print("\n[测试 2] 状态查询")
    print("  发送: 状态")
    await r.set("opus:reply:default", "状态")
    print("  ✓ 消息已发送到 Redis")
    await asyncio.sleep(2)
    
    msg = await r.get("opus:reply:default")
    if msg is None:
        print("  ✅ 秘书已处理状态查询")
    else:
        print("  ❌ 秘书未处理（可能秘书未运行）")
    
    # 测试 3：Redis 连接检查
    print("\n[测试 3] Redis 连接检查")
    try:
        pong = await r.ping()
        print(f"  ✅ Redis 6379 连接正常: {pong}")
    except Exception as e:
        print(f"  ❌ Redis 6379 连接失败: {e}")
    
    # 测试 4：opus.py 存在性检查
    print("\n[测试 4] 依赖检查")
    opus_path = _PROJECT_ROOT / "opus.py"
    if opus_path.exists():
        print(f"  ✅ opus.py 存在: {opus_path}")
    else:
        print(f"  ❌ opus.py 不存在: {opus_path}")
    
    config_path = _PROJECT_ROOT / "config.json"
    if config_path.exists():
        print(f"  ✅ config.json 存在: {config_path}")
        try:
            with open(config_path) as f:
                cfg = json.load(f)
            print(f"  ✅ config.json 格式正确")
            if cfg.get("wecom"):
                print(f"  ✅ 企微配置已设置")
            else:
                print(f"  ⚠️  企微配置缺失")
        except Exception as e:
            print(f"  ❌ config.json 解析失败: {e}")
    else:
        print(f"  ❌ config.json 不存在: {config_path}")
    
    # 测试 5：秘书日志检查
    print("\n[测试 5] 日志检查")
    log_file = _PROJECT_ROOT / "logs" / "secretary.log"
    if log_file.exists():
        print(f"  ✅ 日志文件存在: {log_file}")
        # 显示最后 5 行
        with open(log_file) as f:
            lines = f.readlines()
        print("  最近日志：")
        for line in lines[-5:]:
            print(f"    {line.rstrip()}")
    else:
        print(f"  ⚠️  日志文件不存在（秘书可能未启动）: {log_file}")
    
    await r.close()
    
    print("\n" + "=" * 60)
    print("✅ 测试完成")
    print("=" * 60)
    print("\n💡 下一步：")
    print("  1. 启动秘书进程: systemctl --user start vizo-secretary")
    print("  2. 在企微发送消息进行实际测试")
    print("  3. 查看日志: tail -f {_PROJECT_ROOT}/logs/secretary.log")

if __name__ == "__main__":
    asyncio.run(test_secretary())
