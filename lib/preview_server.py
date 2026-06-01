#!/usr/bin/env python3
"""
前端预览工具 - 生成可通过手机访问的预览链接
Opus 智能协作系统 v4.0

用法:
    # 从命令行创建预览
    python3 preview_server.py create "预览标题" < index.html

    # 从字符串创建预览
    echo '<h1>Hello</h1>' | python3 preview_server.py create "测试"

    # 删除预览
    python3 preview_server.py delete <preview_id>

    # 列出所有预览
    python3 preview_server.py list
"""

import sys
import json
import argparse
import os
import uuid
from datetime import datetime
from pathlib import Path

import redis
from lib.paths import write_data_path
from lib.web_paths import absolute_url, preview_path

_LIB_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _LIB_DIR.parent



def get_redis():
    """获取 Redis 连接"""
    config_path = _PROJECT_ROOT / 'config.json'
    with open(config_path) as f:
        config = json.load(f)

    redis_config = config.get("redis", {})
    return redis.Redis(
        host=redis_config.get("host", "127.0.0.1"),
        port=redis_config.get("port", 6380),
        decode_responses=True
    )


def get_config():
    """获取配置"""
    config_path = _PROJECT_ROOT / 'config.json'
    with open(config_path) as f:
        return json.load(f)


def get_base_url(r: redis.Redis = None) -> str:
    """
    获取预览基础 URL
    优先级: PUBLIC_BASE_URL > custom_domain > localhost
    """
    config = get_config()

    # 0. 优先使用显式公开入口（适用于 Docker 端口映射）
    public_base_url = str(os.environ.get("PUBLIC_BASE_URL", "") or "").strip()
    if public_base_url:
        return public_base_url.rstrip("/")

    # 1. 优先使用自定义域名
    custom_domain = config.get("custom_domain")
    if custom_domain:
        return f"https://{custom_domain}"

    # 2. 使用本地 Vizo 默认入口
    return "http://127.0.0.1:9390"


def create_preview(title: str, html_content: str, ttl: int = 86400) -> dict:
    """
    创建预览

    Args:
        title: 预览标题
        html_content: HTML 内容
        ttl: 过期时间（秒），默认 24 小时

    Returns:
        {"preview_id": "...", "url": "...", "expires_in": ...}
    """
    r = get_redis()

    # 生成唯一 ID
    preview_id = str(uuid.uuid4())[:8]

    # 存储预览数据
    data = {
        "title": title,
        "html": html_content,
        "created_at": datetime.now().isoformat(),
        "ttl": ttl
    }

    r.setex(f"preview:{preview_id}", ttl, json.dumps(data, ensure_ascii=False))

    # F6.2: 磁盘备份
    backup_dir = write_data_path("previews", project_root=_PROJECT_ROOT)
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_file = backup_dir / f"{preview_id}.json"
    try:
        backup_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # 非关键路径

    # 获取基础 URL（优先使用自定义域名）
    base_url = get_base_url(r)
    url = absolute_url(base_url, preview_path(preview_id))

    r.close()

    return {
        "preview_id": preview_id,
        "url": url,
        "expires_in": ttl,
        "title": title
    }


def delete_preview(preview_id: str) -> bool:
    """删除预览"""
    r = get_redis()
    result = r.delete(f"preview:{preview_id}")
    r.close()
    return result > 0


def list_previews() -> list:
    """列出所有预览"""
    r = get_redis()
    keys = r.keys("preview:*")

    previews = []
    base_url = get_base_url(r)

    for key in keys:
        preview_id = key.replace("preview:", "")
        data = r.get(key)
        ttl = r.ttl(key)

        if data:
            try:
                info = json.loads(data)
                previews.append({
                    "preview_id": preview_id,
                    "title": info.get("title", "无标题"),
                    "created_at": info.get("created_at", ""),
                    "ttl_remaining": ttl,
                    "url": absolute_url(base_url, preview_path(preview_id))
                })
            except json.JSONDecodeError:
                previews.append({
                    "preview_id": preview_id,
                    "title": "（旧格式）",
                    "ttl_remaining": ttl,
                    "url": absolute_url(base_url, preview_path(preview_id))
                })

    r.close()
    return previews


def get_preview_url(preview_id: str) -> str:
    """获取预览 URL"""
    base_url = get_base_url()
    return absolute_url(base_url, preview_path(preview_id))


def main():
    parser = argparse.ArgumentParser(description="前端预览工具")
    subparsers = parser.add_subparsers(dest="command", help="命令")

    # create 命令
    create_parser = subparsers.add_parser("create", help="创建预览")
    create_parser.add_argument("title", help="预览标题")
    create_parser.add_argument("--ttl", type=int, default=86400, help="过期时间（秒）")
    create_parser.add_argument("--file", "-f", help="HTML 文件路径（不指定则从 stdin 读取）")

    # delete 命令
    delete_parser = subparsers.add_parser("delete", help="删除预览")
    delete_parser.add_argument("preview_id", help="预览 ID")

    # list 命令
    subparsers.add_parser("list", help="列出所有预览")

    # url 命令
    url_parser = subparsers.add_parser("url", help="获取预览 URL")
    url_parser.add_argument("preview_id", help="预览 ID")

    args = parser.parse_args()

    if args.command == "create":
        # 读取 HTML 内容
        if args.file:
            with open(args.file, 'r', encoding='utf-8') as f:
                html_content = f.read()
        else:
            html_content = sys.stdin.read()

        if not html_content.strip():
            print("错误: HTML 内容不能为空", file=sys.stderr)
            sys.exit(1)

        result = create_preview(args.title, html_content, args.ttl)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print(f"\n预览链接: {result['url']}", file=sys.stderr)

    elif args.command == "delete":
        if delete_preview(args.preview_id):
            print(f"已删除预览: {args.preview_id}")
        else:
            print(f"预览不存在: {args.preview_id}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "list":
        previews = list_previews()
        if previews:
            print(f"共 {len(previews)} 个预览:\n")
            for p in previews:
                ttl_min = p['ttl_remaining'] // 60
                print(f"  [{p['preview_id']}] {p['title']}")
                print(f"      URL: {p['url']}")
                print(f"      剩余: {ttl_min} 分钟")
                print()
        else:
            print("暂无预览")

    elif args.command == "url":
        print(get_preview_url(args.preview_id))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
