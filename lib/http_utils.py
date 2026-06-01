"""
HTTP helpers for loopback requests.

本机回环地址（127.0.0.1 / localhost / ::1）的请求不应走系统代理，
否则在设置了 http_proxy 的部署环境下，读取本机健康检查和 bridge
端点会被错误地转发到代理。
"""

from __future__ import annotations

from urllib.parse import urlparse
import urllib.request

_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def is_loopback_url(url: str | None) -> bool:
    """判断 URL 是否指向本机回环地址。"""
    try:
        host = (urlparse(str(url or "").strip()).hostname or "").lower()
    except Exception:
        return False
    return host in {"127.0.0.1", "localhost", "::1"}


def urlopen_safely(request, timeout=None):
    """对本机回环请求禁用代理，其余请求保持默认行为。"""
    full_url = request.full_url if hasattr(request, "full_url") else str(request)
    if is_loopback_url(full_url):
        return _NO_PROXY_OPENER.open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)
