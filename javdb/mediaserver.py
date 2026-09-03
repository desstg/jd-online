"""Emby / Jellyfin 媒体服务器客户端。

两者共用同一套 API（Jellyfin 是 Emby 的分支），入库联动逻辑移植自
JavdbBuddy.user.js：
- 全量拉取：GET /Items?Recursive=true&IncludeItemTypes=Movie&Fields=Path
- 连通性：  GET /System/Info
- 按番号查：GET /Items?searchTerm=<code>&IncludeItemTypes=Movie
- 从条目 Name / Path 中提取番号（extractCodeFromTitle 正则）
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from .http import make_opener

_RE_STANDARD = re.compile(r"([A-Z0-9]{2,12}[-_][A-Z0-9]{2,10}|[A-Z]{2,10}\d{3,6})", re.I)
_RE_FIRST_WORD = re.compile(r"^([a-z0-9_-]{3,25})", re.I)
_EXCLUDE_WORDS = {"THE", "THIS", "WHAT", "WITH"}


def extract_code(text: str | None) -> str | None:
    """从条目名称/路径中提取番号（照搬脚本 extractCodeFromTitle）。"""
    if not text:
        return None
    text = text.strip()
    m = _RE_STANDARD.search(text)
    if m:
        return m.group(1).upper()
    m = _RE_FIRST_WORD.match(text)
    if m:
        code = m.group(1)
        if code.upper() not in _EXCLUDE_WORDS:
            return code.upper()
    return None


class MediaServerError(Exception):
    pass


class MediaServerClient:
    def __init__(self, url: str, api_key: str, name: str = "",
                 type_: str = "emby", timeout: float = 20, proxy: str | None = None):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.name = name
        self.type = type_
        self.timeout = timeout
        self._opener = make_opener(proxy)

    def _get(self, path: str, params: dict) -> dict:
        p = dict(params)
        p["api_key"] = self.api_key
        url = self.url + path + "?" + urllib.parse.urlencode(p)
        req = urllib.request.Request(url, headers={"User-Agent": "jd-online"})
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise MediaServerError("API Key 错误 (401)")
            raise MediaServerError(f"连接失败 (HTTP {e.code})")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise MediaServerError(f"地址错误或无法连接: {e}")

    def ping(self) -> tuple[bool, str]:
        """返回 (是否连通, 描述)。"""
        try:
            data = self._get("/System/Info", {})
            label = data.get("ServerName") or data.get("Version") or "在线"
            return True, label
        except MediaServerError as e:
            return False, str(e)

    def fetch_all_movies(self) -> list[dict]:
        """分页拉取全部电影条目。"""
        items: list[dict] = []
        start, limit = 0, 500
        while True:
            data = self._get("/Items", {
                "Recursive": "true",
                "IncludeItemTypes": "Movie",
                "Fields": "Path",
                "StartIndex": start,
                "Limit": limit,
            })
            batch = data.get("Items") or []
            items.extend(batch)
            total = data.get("TotalRecordCount", len(batch))
            start += len(batch)
            if len(batch) < limit or start >= total:
                break
        return items

    def search(self, code: str) -> dict | None:
        """按番号实时搜索，返回第一条命中或 None。"""
        data = self._get("/Items", {
            "searchTerm": code,
            "Recursive": "true",
            "IncludeItemTypes": "Movie",
            "Limit": "1",
        })
        items = data.get("Items") or []
        return items[0] if items else None
