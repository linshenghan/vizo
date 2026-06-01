#!/usr/bin/env python3
"""健康检查脚本 - 检查 Redis 服务状态"""

import argparse
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CONFIG_PATH = str(Path(__file__).resolve().parents[1] / "config.json")
DEFAULT_TIMEOUT = 5


def load_config(config_path: str) -> tuple:
    """加载配置文件

    返回 (config_dict, warning_message)
    - 成功: (config, None)
    - 文件不存在: ({}, "配置文件不存在，使用默认值")
    - JSON 解析失败: ({}, "配置文件解析失败，使用默认值")
    """
    path = Path(config_path)
    if not path.exists():
        return {}, "配置文件不存在，使用默认值"
    try:
        with open(path) as f:
            return json.load(f), None
    except (json.JSONDecodeError, IOError):
        return {}, "配置文件解析失败，使用默认值"


def check_redis(host: str, port: int, password, db: int, timeout: int) -> dict:
    """检查 Redis 连通性

    返回字典：
    - 成功: {"status": "ok", "latency_ms": 1.23, "host": "...", "port": 6380}
    - 失败: {"status": "error", "error": "...", "host": "...", "port": 6380}

    使用线程强制超时，防止 Linux TCP SYN 重试导致实际耗时远超配置值。
    """
    base = {"host": host, "port": port}
    try:
        import redis
    except ImportError:
        return {**base, "status": "error",
                "error": "redis 模块未安装，请执行: pip install redis"}

    result_box = [None]

    def _do_check():
        try:
            r = redis.Redis(host=host, port=port, password=password, db=db,
                            socket_timeout=timeout, socket_connect_timeout=timeout)
            start = time.monotonic()
            r.ping()
            latency = (time.monotonic() - start) * 1000
            r.close()
            result_box[0] = {**base, "status": "ok", "latency_ms": round(latency, 2)}
        except redis.AuthenticationError:
            result_box[0] = {**base, "status": "error", "error": "认证失败 (Authentication failed)"}
        except redis.ConnectionError:
            result_box[0] = {**base, "status": "error", "error": "连接被拒绝 (Connection refused)"}
        except redis.TimeoutError:
            result_box[0] = {**base, "status": "error",
                             "error": f"连接超时 (Timeout after {timeout}s)"}
        except Exception as e:
            result_box[0] = {**base, "status": "error", "error": f"Redis 异常: {e}"}

    t = threading.Thread(target=_do_check, daemon=True)
    t.start()
    t.join(timeout=timeout)

    if result_box[0] is not None:
        return result_box[0]
    return {**base, "status": "error",
            "error": f"连接超时 (Timeout after {timeout}s)"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="健康检查 - 检查 Redis 服务状态"
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                        help=f"配置文件路径 (默认: {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help=f"各项检查超时时间/秒 (默认: {DEFAULT_TIMEOUT})")
    parser.add_argument("--pretty", action="store_true",
                        help="格式化 JSON 输出（缩进 2 空格）")
    return parser.parse_args()


def main():
    try:
        args = parse_args()
        config, config_warning = load_config(args.config)

        # 从配置提取 Redis 参数
        redis_cfg = config.get("redis", {})
        redis_host = redis_cfg.get("host", "127.0.0.1")
        redis_port = redis_cfg.get("port", 6380)
        redis_password = redis_cfg.get("password", "") or None
        redis_db = redis_cfg.get("db", 0)

        timeout = args.timeout

        redis_result = check_redis(redis_host, redis_port, redis_password, redis_db, timeout)

        overall = "healthy" if redis_result["status"] == "ok" else "unhealthy"

        result = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "overall": overall,
            "checks": {
                "redis": redis_result
            }
        }
        if config_warning:
            result["config_warning"] = config_warning

        indent = 2 if args.pretty else None
        print(json.dumps(result, ensure_ascii=False, indent=indent))
        sys.exit(0 if overall == "healthy" else 1)
    except SystemExit:
        raise
    except Exception as e:
        try:
            fallback = {
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "overall": "unhealthy",
                "error": f"Unhandled {type(e).__name__}: {e}"
            }
            print(json.dumps(fallback, ensure_ascii=False))
        except Exception:
            print('{"overall": "unhealthy", "error": "health check internal error"}')
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        sys.exit(e.code)
    except Exception as e:
        try:
            fallback = {
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "overall": "unhealthy",
                "error": f"Unhandled {type(e).__name__}: {e}"
            }
            print(json.dumps(fallback, ensure_ascii=False))
        except Exception:
            print('{"overall": "unhealthy", "error": "health check internal error"}')
        sys.exit(1)
