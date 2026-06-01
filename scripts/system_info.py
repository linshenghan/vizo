#!/usr/bin/env python3
"""系统信息采集工具 - 输出 CPU、内存、磁盘使用率（JSON 格式）"""

import argparse
import json
import socket
import sys
from datetime import datetime, timezone

try:
    import psutil
except ImportError:
    print(json.dumps({"error": "psutil 未安装，请执行 pip install psutil"},
                     ensure_ascii=False))
    sys.exit(1)


def get_system_info() -> dict:
    """采集当前系统的 CPU、内存、磁盘使用率，返回结构化字典"""
    result = {
        "hostname": socket.gethostname(),
        "timestamp": datetime.now(timezone.utc).astimezone().isoformat(),
    }

    # CPU 使用率（1 秒采样）
    result["cpu_percent"] = round(psutil.cpu_percent(interval=1), 1)

    # 内存信息
    try:
        mem = psutil.virtual_memory()
        result["memory"] = {
            "total_gb": round(mem.total / (1024 ** 3), 1),
            "used_gb": round(mem.used / (1024 ** 3), 1),
            "percent": round(mem.percent, 1),
        }
    except Exception:
        result["memory"] = {"error": "无法获取内存信息"}

    # 磁盘信息（根分区）
    try:
        disk = psutil.disk_usage("/")
        result["disk"] = {
            "/": {
                "total_gb": round(disk.total / (1024 ** 3), 1),
                "used_gb": round(disk.used / (1024 ** 3), 1),
                "percent": round(disk.percent, 1),
            }
        }
    except OSError:
        result["disk"] = {"/": {"error": "无法访问磁盘路径: /"}}

    return result


def main():
    try:
        parser = argparse.ArgumentParser(
            description="系统信息采集工具 - 输出 CPU、内存、磁盘使用率（JSON 格式）"
        )
        parser.add_argument("--pretty", action="store_true",
                            help="格式化 JSON 输出（缩进 2 空格）")
        args = parser.parse_args()

        info = get_system_info()
        indent = 2 if args.pretty else None
        print(json.dumps(info, ensure_ascii=False, indent=indent))
        sys.exit(0)
    except SystemExit:
        raise
    except Exception as e:
        try:
            fallback = {
                "error": f"系统信息采集失败: {e}",
                "timestamp": datetime.now(timezone.utc).astimezone().isoformat(),
            }
            print(json.dumps(fallback, ensure_ascii=False))
        except Exception:
            print('{"error": "system_info internal error"}')
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        sys.exit(e.code)
    except Exception as e:
        try:
            fallback = {
                "error": f"系统信息采集失败: {e}",
                "timestamp": datetime.now(timezone.utc).astimezone().isoformat(),
            }
            print(json.dumps(fallback, ensure_ascii=False))
        except Exception:
            print('{"error": "system_info internal error"}')
        sys.exit(1)
