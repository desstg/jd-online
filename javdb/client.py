"""JAVDB 移动端 API 客户端。

签名算法、接口参数与请求头均提取自 JavdbBuddy.user.js（原脚本逆向自官方 App）。

注意：
- 签名密钥硬编码在脚本里，官方若轮换会失效，届时需更新 SECRET。
- 账号密码 / token 以明文存于本地 config（与原脚本行为一致），见 README。
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .http import make_opener

API_BASE = "https://jdforrepam.com/api"

# 提取自 JavdbBuddy.user.js jbBuildSignature()
SECRET = (
    "71cf27bb3c0bcdf207b64abecddc970098c7421ee7203b9cdae54478478a199e"
    "7d5a6e1a57691123c1a931c057842fb73ba3b3c83bcd69c17ccf174081e3d8aa"
)
SALT = "lpw6vgqzsp"

# 伪装成官方 iOS App 的设备信息（提取自登录接口参数）
DEVICE = {
    "device_uuid": "04b9534d-5118-53de-9f87-2ddded77111e",
    "device_name": "iPhone",
    "device_model": "iPhone",
    "platform": "ios",
    "system_version": "17.4",
    "app_version": "official",
    "app_version_number": "1.9.29",
    "app_channel": "official",
}

def normalize_image_url(url: str) -> str:
    """保留原始图片 URL。

    原 JavdbBuddy 脚本会把预览图重写到 c0.jdbstatic.com，但该域名在当前网络
    不可达（实测超时），而原始 CDN tp.spfcas.com 可直链，故不做重写。
    """
    return url


class JavdbError(Exception):
    """API 层错误（含服务端返回的 message）。"""


class JavdbClient:
    def __init__(self, username: str | None = None, password: str | None = None,
                 token: str | None = None, min_interval: float = 0.5,
                 timeout: float = 20, retries: int = 3, proxy: str | None = None,
                 api_base: str | None = None):
        self.username = username
        self.password = password
        self.token = token
        self.min_interval = min_interval
        self.timeout = timeout
        self.retries = retries
        self.api_base = (api_base or API_BASE).rstrip("/")
        self._last_ts = 0.0
        self._opener = make_opener(proxy)

    # ---- 签名 / 请求底层 ----
    def signature(self) -> str:
        curr = int(time.time())
        digest = hashlib.md5(f"{curr}{SECRET}".encode()).hexdigest()
        return f"{curr}.{SALT}.{digest}"

    def _headers(self, extra: dict | None = None) -> dict:
        h = {
            "jdSignature": self.signature(),
            "user-agent": "Dart/3.5 (dart:io)",
            "accept-language": "zh-TW",
            "host": "jdforrepam.com",
        }
        if self.token:
            h["authorization"] = "Bearer " + self.token
        if extra:
            h.update(extra)
        return h

    def _throttle(self) -> None:
        wait = self._last_ts + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_ts = time.monotonic()

    def request(self, method: str, path: str, params: dict | None = None,
                body: bytes | None = None, extra_headers: dict | None = None,
                retries: int | None = None) -> dict:
        """发送请求并返回解析后的 JSON；带限流与重试。"""
        retries = self.retries if retries is None else retries
        url = self.api_base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)

        last_err: Exception | None = None
        for attempt in range(retries):
            self._throttle()
            req = urllib.request.Request(
                url, data=body, headers=self._headers(extra_headers), method=method
            )
            try:
                with self._opener.open(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8")
                return json.loads(raw)
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", "replace")
                try:
                    data = json.loads(raw)
                    last_err = JavdbError(data.get("message") or f"HTTP {e.code}")
                except json.JSONDecodeError:
                    last_err = JavdbError(f"HTTP {e.code}: {raw[:200]}")
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = JavdbError(f"网络错误: {e}")
            if attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
        raise last_err or JavdbError("请求失败")

    def _require_token(self) -> None:
        if not self.token:
            raise JavdbError("该接口需要登录 token，请先 login 或配置 token")

    # ---- 鉴权 ----
    def login(self, username: str | None = None, password: str | None = None) -> str:
        """POST /v1/sessions 登录，返回 token 并写入 self.token。"""
        username = username or self.username
        password = password or self.password
        if not username or not password:
            raise JavdbError("缺少用户名/密码")

        params = dict(DEVICE)
        params["username"] = username
        params["password"] = password
        res = self.request(
            "POST", "/v1/sessions", params=params,
            extra_headers={"Content-Type": "multipart/form-data; boundary=--dio-boundary-2210433284"},
        )
        data = res.get("data") or {}
        token = data.get("token")
        if not token:
            raise JavdbError(res.get("message") or "登录失败，无 token 返回")
        self.token = token
        self.username = username
        self.password = password
        return token

    # ---- 业务接口 ----
    def search(self, keyword: str, page: int = 1, limit: int = 20,
               movie_type: str = "all", movie_sort_by: str = "relevance") -> dict:
        res = self.request("GET", "/v2/search", {
            "q": keyword, "page": page, "type": "movie", "limit": limit,
            "movie_type": movie_type, "from_recent": "false",
            "movie_filter_by": "all", "movie_sort_by": movie_sort_by,
        })
        return res

    def search_by_type(self, keyword: str, movie_type: str, page: int = 1, limit: int = 20) -> dict:
        """按指定类型搜索（actor/series/maker/director/label 等）。"""
        return self.search(keyword, page=page, limit=limit, movie_type=movie_type)

    def movie(self, movie_id: str) -> dict:
        res = self.request("GET", f"/v4/movies/{movie_id}")
        if not res.get("data"):
            raise JavdbError(res.get("message") or "获取详情失败")
        return res

    def reviews(self, movie_id: str, page: int = 1, page_size: int = 20) -> dict:
        res = self.request("GET", f"/v1/movies/{movie_id}/reviews", {
            "page": page, "sort_by": "hotly", "limit": page_size,
        })
        return res

    def hot(self, period: str = "daily", filter_by: str = "high_score") -> dict:
        return self.request("GET", "/v1/rankings/playback", {
            "period": period, "filter_by": filter_by,
        })

    def actor_rank(self, type_: str = "0", page: int = 1, limit: int = 200) -> dict:
        """演员热度榜。type：0 有码 / 1 无码 / 2 欧美 / 3 FC2（与影片分类一致）。"""
        self._require_token()
        return self.request("GET", "/v1/rankings/actors", {
            "type": type_, "page": page, "limit": limit,
        })

    def top250(self, type_: str = "all", type_value: str = "",
               page: int = 1, limit: int = 50, year: str = "") -> dict:
        self._require_token()
        params = {
            "start_rank": "1", "type": type_,
            "type_value": type_value, "ignore_watched": "false",
            "page": page, "limit": limit,
        }
        if year:
            params["year"] = year
        return self.request("GET", "/v1/movies/top", params)

    def related(self, movie_id: str, page: int = 1, limit: int = 20) -> dict:
        return self.request("GET", "/v1/lists/related", {
            "movie_id": movie_id, "page": page, "limit": limit,
        })

    def list_info(self, list_id: str) -> dict:
        """清单详情（名称/影片数等）。"""
        return self.request("GET", f"/v1/lists/{list_id}")

    def list_movies(self, list_name: str, page: int = 1, limit: int = 24) -> dict:
        """按清单名搜影片（type=lists）。"""
        return self.request("GET", "/v2/search", {
            "q": list_name, "page": page, "type": "lists", "limit": limit,
        })
