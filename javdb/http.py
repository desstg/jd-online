"""共享 HTTP 打开器：给所有抓取客户端统一加代理支持。"""

from __future__ import annotations

import urllib.request


def make_opener(proxy: str | None = None):
    """返回一个 opener；proxy 为空时等价于默认 urlopen 行为。"""
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)
