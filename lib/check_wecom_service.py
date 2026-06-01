#!/usr/bin/env python3
"""
企业微信服务可用性检查脚本

用于在激活 Opus 智能协作系统时检查远程会话服务是否可用。
检查内容：
1. 配置文件是否存在且有效
2. 企业微信 API 是否可连通
3. IP 白名单是否正确配置
"""

import sys
import json
import asyncio
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _LIB_DIR.parent


sys.path.insert(0, str(Path(__file__).resolve().parent))


def load_config():
    """加载配置文件"""
    config_path = _PROJECT_ROOT / 'config.json'
    if not config_path.exists():
        return None, f"配置文件不存在: {config_path}"

    try:
        with open(config_path) as f:
            return json.load(f), None
    except json.JSONDecodeError as e:
        return None, f"配置文件格式错误: {e}"


def check_secrets(config):
    """检查必要的密钥配置"""
    secrets = config.get('secrets', {})
    required = ['wecom_corpid', 'wecom_agentid', 'wecom_secret', 'wecom_userid']
    missing = [k for k in required if not secrets.get(k)]

    if missing:
        return False, f"缺少企业微信配置: {', '.join(missing)}"
    return True, None


async def test_wecom_api(config):
    """测试企业微信 API 连通性"""
    try:
        from wecom_notifier import WeComNotifier

        secrets = config.get('secrets', {})
        wecom = WeComNotifier(
            corpid=secrets.get('wecom_corpid', ''),
            agentid=secrets.get('wecom_agentid', ''),
            secret=secrets.get('wecom_secret', ''),
            userid=secrets.get('wecom_userid', '')
        )

        # 发送测试消息
        result = await wecom.send_text('🔍 Opus 系统启动检查')
        await wecom.close()  # 关闭 session 避免警告

        if result.get('errcode') == 0:
            return True, None
        elif result.get('errcode') == 60020:
            # IP 白名单错误
            errmsg = result.get('errmsg', '')
            # 提取 IP 地址
            import re
            ip_match = re.search(r'from ip: ([\d.]+)', errmsg)
            ip = ip_match.group(1) if ip_match else '未知'
            return False, f"IP 白名单错误: 当前 IP {ip} 不在企业微信可信列表中\n解决方案: 登录企业微信管理后台 → 应用管理 → 企业可信IP → 添加 {ip}"
        else:
            return False, f"企业微信 API 错误 ({result.get('errcode')}): {result.get('errmsg')}"

    except ImportError as e:
        return False, f"模块导入失败: {e}"
    except Exception as e:
        return False, f"API 测试异常: {e}"


async def main():
    """主检查流程"""
    print("=" * 50)
    print("🔍 Opus 远程会话服务可用性检查")
    print("=" * 50)

    all_passed = True

    # 1. 检查配置文件
    print("\n[1/3] 检查配置文件...")
    config, error = load_config()
    if error:
        print(f"  ❌ {error}")
        all_passed = False
    else:
        print("  ✅ 配置文件加载成功")

    if not config:
        print("\n" + "=" * 50)
        print("❌ 检查失败，无法继续")
        print("=" * 50)
        sys.exit(1)

    # 2. 检查密钥配置
    print("\n[2/3] 检查企业微信密钥...")
    ok, error = check_secrets(config)
    if not ok:
        print(f"  ❌ {error}")
        all_passed = False
    else:
        print("  ✅ 密钥配置完整")

    # 3. 测试 API 连通性
    print("\n[3/3] 测试企业微信 API...")
    ok, error = await test_wecom_api(config)
    if not ok:
        print(f"  ❌ {error}")
        all_passed = False
    else:
        print("  ✅ API 连通正常，测试消息已发送")

    # 结果汇总
    print("\n" + "=" * 50)
    if all_passed:
        print("✅ 所有检查通过，远程会话服务可用")
        print("=" * 50)
        sys.exit(0)
    else:
        print("❌ 部分检查失败，请按上述提示修复")
        print("=" * 50)
        sys.exit(1)


if __name__ == '__main__':
    asyncio.run(main())
