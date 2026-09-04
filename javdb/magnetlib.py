"""自定义磁链库客户端：按「API URL 模板 + 请求头」从外部磁力源拉取磁链 / 统计。

一套可配置的磁力源（如内网 AVdb）由设置页「扩展磁链库」定义，每项含：
  - API URL 模板：可含 {code}（番号）与 {key}（取 X-API-Key 请求头值）占位符；
  - 请求头列表：如 X-API-Key: <令牌>；
  - 统计 API（可选）：单独统计接口地址。

字段解析（parse_magnets / parse_stats）各自隔离，外部接口结构变化只改这两个函数。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from .http import make_opener

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
)

_RE_BTIH = re.compile(r"btih:([0-9a-fA-F]{40})")
_RE_HD = re.compile(r"(?i)\b(4k|2160p|1080p|fhd|hd)\b")
_RE_SUB = re.compile(r"(?i)(中字|中文|字幕|国语|chs|cht|chinese)")


class MagnetLibError(Exception):
    pass


def _pick(obj: dict, keys: tuple[str, ...], default=None):
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if v is not None:
                return v
    return default


def _extract_btih(magnet: str) -> str:
    m = _RE_BTIH.search(magnet or "")
    return m.group(1).lower() if m else ""


def _fmt_size(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        n = float(v)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024 or unit == "TB":
                return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
            n /= 1024
    return str(v)


def parse_magnets(payload) -> list[dict]:
    """把磁力接口响应解析成 [{btih,magnet,name,size,date,has_hd,has_sub}, ...]。

    仅此一处假设接口结构：兼容列表或常见的包裹键（data/torrents/items/results/list/magnets）。
    """
    items = None
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        for k in ("data", "torrents", "items", "results", "list", "magnets", "downloads"):
            v = payload.get(k)
            if isinstance(v, list):
                items = v
                break
        if items is None:
            for k in ("data", "torrents", "items", "results", "list", "magnets"):
                v = payload.get(k)
                if isinstance(v, dict):
                    for k2 in ("items", "list", "data", "results", "downloads"):
                        if isinstance(v.get(k2), list):
                            items = v[k2]
                            break
                    if items is not None:
                        break
    out: list[dict] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        magnet = _pick(it, ("download_url", "magnet_url", "magnet_uri", "magnet", "url", "torrent", "uri", "link"))
        # 避免 dict 值（如 {"url": ...}）剥不干净
        while isinstance(magnet, dict):
            magnet = _pick(magnet, ("url", "link", "uri", "value"))
        if not isinstance(magnet, str) or "magnet:" not in magnet:
            continue
        name = _pick(it, ("name", "title", "display_name", "file_name", "fileName", "label"))
        if isinstance(name, dict):
            name = _pick(name, ("text", "value"))
        name = str(name or "")
        btih = _pick(it, ("btih", "info_hash", "infohash", "hash")) or _extract_btih(magnet)
        # 大小：size_mb 是 MB，其余按字节处理
        if it.get("size_mb") is not None:
            size = _fmt_size(it["size_mb"] * 1024 * 1024)
        else:
            size = _fmt_size(_pick(it, ("size_bytes", "size_str", "size_text", "length", "size", "byte_size")))
        date = _pick(it, ("post_time", "date", "created_at", "add_time", "upload_time", "release_date"))
        hd = it.get("hd") or it.get("uhd")
        has_hd = _pick(it, ("has_hd", "is_hd"))
        if has_hd is None:
            has_hd = hd if hd is not None else (1 if _RE_HD.search(name) else 0)
        has_sub = _pick(it, ("chinese", "has_sub", "has_subtitle", "subtitle", "is_chinese"))
        if has_sub is None:
            has_sub = 1 if _RE_SUB.search(name) else 0
        out.append({
            "btih": str(btih or "").lower(),
            "magnet": magnet.strip(),
            "name": name,
            "size": size,
            "date": "" if date is None else str(date),
            "has_hd": 1 if has_hd else 0,
            "has_sub": 1 if has_sub else 0,
        })
    # 同一 btih 只保留一条
    seen: set[str] = set()
    ded: list[dict] = []
    for m in out:
        key = m["btih"] or m["magnet"]
        if key in seen:
            continue
        seen.add(key)
        ded.append(m)
    return ded


def parse_stats(payload) -> tuple[list[dict], str | None]:
    """把统计接口响应解析成 ([{label,count}, ...], 日期)。仅此一处假设统计结构。"""
    items = None
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        for k in ("tables", "categories", "stats", "items", "data", "result", "list", "library"):
            v = payload.get(k)
            if isinstance(v, list):
                items = v
                break
            if isinstance(v, dict):
                for k2 in ("tables", "categories", "stats", "items", "list"):
                    if isinstance(v.get(k2), list):
                        items = v[k2]
                        break
                if items is not None:
                    break
    cats: list[dict] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        label = _pick(it, ("alias", "label", "name", "category", "key", "title", "text", "site"))
        count = _pick(it, ("count", "value", "num", "number", "total"))
        if label is None or count is None or isinstance(count, (dict, list)):
            continue
        try:
            count = int(float(str(count).replace(",", "")))
        except (ValueError, TypeError):
            continue
        cats.append({"label": str(label), "count": count})
    date = None
    if isinstance(payload, dict):
        date = _pick(payload, ("last_update", "updated_at", "update_time", "as_of", "date", "generated_at"))
        if not isinstance(date, str):
            date = None
    return cats, date


def parse_stats_groups(payload) -> dict:
    """按站点分组统计。返回 {total, groups:[{site, items:[{label,count}], subtotal}], date}。

    适配 AVdb /api/v1/stats 的 tables[]（entry 含 alias/site/count）与 total/last_update。
    """
    items = None
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        for k in ("tables", "categories", "stats", "items", "data", "result", "list", "library"):
            v = payload.get(k)
            if isinstance(v, list):
                items = v
                break
            if isinstance(v, dict):
                for k2 in ("tables", "categories", "stats", "items", "list"):
                    if isinstance(v.get(k2), list):
                        items = v[k2]
                        break
                if items is not None:
                    break
    groups: dict[str, dict] = {}
    total = 0
    for it in items or []:
        if not isinstance(it, dict):
            continue
        label = _pick(it, ("alias", "label", "name", "category", "key", "title", "text"))
        count = _pick(it, ("count", "value", "num", "number", "total"))
        if label is None or count is None or isinstance(count, (dict, list)):
            continue
        try:
            count = int(float(str(count).replace(",", "")))
        except (ValueError, TypeError):
            continue
        site = str(_pick(it, ("site", "source", "type", "group")) or "未分组")
        total += count
        g = groups.setdefault(site, {"items": {}})
        g["items"][str(label)] = g["items"].get(str(label), 0) + count
    if isinstance(payload, dict):
        t = payload.get("total")
        if isinstance(t, (int, float)) and not isinstance(t, bool):
            total = int(t)
    groups_out = []
    for site, g in groups.items():
        its = [{"label": k, "count": v} for k, v in g["items"].items()]
        groups_out.append({"site": site, "items": its, "subtotal": sum(v for v in g["items"].values())})
    date = None
    if isinstance(payload, dict):
        date = _pick(payload, ("last_update", "updated_at", "update_time", "as_of", "date", "generated_at"))
        if not isinstance(date, str):
            date = None
    return {"total": total, "groups": groups_out, "date": date}


class MagnetLibClient:
    def __init__(self, api_url_template: str = "", headers: list | None = None,
                 timeout: float = 30, proxy: str | None = None):
        self.template = (api_url_template or "").strip()
        self.headers = headers or []
        self.timeout = timeout
        self._opener = make_opener(proxy)

    def _api_key(self) -> str:
        for h in self.headers:
            if str((h.get("name") or "").strip()).lower() == "x-api-key":
                return str(h.get("value") or "").strip()
        return ""

    def build_url(self, code: str) -> str:
        return self.template.replace("{code}", code or "").replace("{key}", self._api_key())

    def _get(self, url: str) -> tuple[int, str]:
        hdrs = {"User-Agent": UA, "Accept": "application/json,text/plain,*/*"}
        for h in self.headers:
            name = (h.get("name") or "").strip()
            if name and h.get("value") is not None:
                hdrs[name] = str(h.get("value"))
        req = urllib.request.Request(url, headers=hdrs)
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise MagnetLibError(f"请求失败: {e}") from e

    def fetch_magnets(self, code: str) -> list[dict]:
        if not self.template:
            raise MagnetLibError("API URL 模板为空")
        status, body = self._get(self.build_url(code))
        if status in (401, 403):
            raise MagnetLibError(f"鉴权失败（HTTP {status}）")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = body
        return parse_magnets(payload)

    def fetch_stats(self, url: str | None) -> tuple[list[dict], str | None]:
        if not url:
            return [], None
        status, body = self._get(url)
        if status in (401, 403):
            raise MagnetLibError(f"统计鉴权失败（HTTP {status}）")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return [], None
        return parse_stats(payload)

    def get_json(self, url: str) -> dict | None:
        """GET 并解析 JSON（供统计历史快照使用）。"""
        status, body = self._get(url)
        if status in (401, 403):
            raise MagnetLibError(f"鉴权失败（HTTP {status}）")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None

    def test(self, url: str | None = None) -> tuple[bool, str]:
        u = url or self.build_url("TEST-001")
        if not u:
            return False, "未配置 API URL"
        try:
            status, _ = self._get(u)
        except MagnetLibError as e:
            return False, str(e)
        if status in (401, 403):
            return False, f"鉴权失败（HTTP {status}）"
        return True, f"连通成功（HTTP {status}）"
