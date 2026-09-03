"""JAVBUS 磁链抓取器。

机制提取自 JavdbBuddy.user.js，本质是两步 AJAX（非加密）：
1. GET `{base}/{code}` 详情页，从中提取 `gid` / `uc` / `img` 三个变量；
2. GET `{base}/ajax/uncledatoolsbyajax.php?gid=..&lang=zh&img=..&uc=..`，
   返回磁链表格（<tr> 每行：名称 / 大小 / 日期，各带 magnet: 链接）。

关键 cookie 为 `existmag=all`（显示全部磁链）。
"""

from __future__ import annotations

import html
import re
import urllib.error
import urllib.parse
import urllib.request

from .http import make_opener

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
)

_RE_GID = re.compile(r"var\s+gid\s*=\s*(\d+)\s*;")
_RE_UC = re.compile(r"var\s+uc\s*=\s*(\d+)\s*;")
_RE_IMG = re.compile(r"var\s+img\s*=\s*['\"]([^'\"]+)['\"]\s*;")
_RE_TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_RE_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
_RE_HREF = re.compile(r"href\s*=\s*[\"'](magnet:[^\"']+)[\"']", re.I)
_RE_ONCLICK = re.compile(r"window\.open\(\s*[\"'](magnet:[^\"']+)[\"']", re.I)
_RE_BTN = re.compile(r"<a[^>]*class=\"[^\"]*btn[^\"]*\"[^>]*>.*?</a>", re.S | re.I)
_RE_TAG = re.compile(r"<[^>]+>")
_RE_BTIH = re.compile(r"btih:([0-9a-fA-F]{40})")
_RE_SAMPLE = re.compile(r'/pics/sample/[^"\']+\.jpg', re.I)

# 磁链名中「破解/无码/流出」等标记 → 判定为破解版
_RE_UNCENSORED = re.compile(r"(?i)(?:[-_]|^)(u|uc|restored)(?:[^a-z0-9]|$)")
_UC_KEYWORDS = ("破解", "uncensored", "无码", "流出", "无修正")

# 磁链名中「中文/中字/字幕」等标记 → 判定为字幕版（中文片）
_RE_CN = re.compile(r"(?i)(?:[-_]|^)(c|ch|chs|cht)(?:[^a-z0-9]|$)")
_CN_KEYWORDS = ("中字", "中文", "字幕", "国语", "简中", "繁中", "chinese", "chs", "cht", "zho")


def is_uncensored(name: str | None) -> bool:
    """从磁链文件名判断是否为破解版（无码/流出/uncensored）。"""
    if not name:
        return False
    if _RE_UNCENSORED.search(name):
        return True
    n = name.lower()
    return any(k in n for k in _UC_KEYWORDS)


def is_chinese(name: str | None) -> bool:
    """从磁链文件名判断是否为字幕版（中文片，含 -c / -ch 等标识）。"""
    if not name:
        return False
    if _RE_CN.search(name):
        return True
    n = name.lower()
    return any(k in n for k in _CN_KEYWORDS)


class JavbusError(Exception):
    pass


def _strip_tags(s: str) -> str:
    return html.unescape(_RE_TAG.sub(" ", s)).strip()


def _clean_name(cell_html: str) -> str:
    """名称单元格：去掉 HD/字幕 标签按钮，返回干净名称。"""
    no_btn = _RE_BTN.sub("", cell_html)
    return re.sub(r"\s+", " ", _strip_tags(no_btn)).strip()


class JavbusClient:
    def __init__(self, base_url: str = "https://www.javbus.com", timeout: float = 20,
                 proxy: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._opener = make_opener(proxy)

    def _get(self, url: str, referer: str | None = None, ajax: bool = False):
        headers = {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cookie": "existmag=all",
        }
        if referer:
            headers["Referer"] = referer
        if ajax:
            headers["X-Requested-With"] = "XMLHttpRequest"
        req = urllib.request.Request(url, headers=headers)
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise JavbusError(f"JAVBUS 请求失败: {e}") from e

    def fetch_magnets(self, code: str) -> tuple[list[dict], str | None, list[str]]:
        """抓取某番号的磁链 + JAVBUS 封面 + 预览图。

        返回 (磁链列表, 封面 URL 或 None, 预览图 URL 列表)。
        """
        detail_url = f"{self.base_url}/{code}"
        status, body = self._get(detail_url, referer=self.base_url + "/")
        if status != 200:
            return [], None, []

        gid = _RE_GID.search(body)
        uc = _RE_UC.search(body)
        img = _RE_IMG.search(body)

        # JAVBUS 封面 + 预览图（干净图，可走代理直链）
        cover_url = f"{self.base_url}{img.group(1)}" if img else None
        samples: list[str] = []
        seen_sample: set[str] = set()
        for m in _RE_SAMPLE.findall(body):
            u = self.base_url + m
            if u not in seen_sample:
                seen_sample.add(u)
                samples.append(u)

        magnets: list[dict] = []
        if gid and uc and img:
            api = (
                f"{self.base_url}/ajax/uncledatoolsbyajax.php?gid={gid.group(1)}"
                f"&lang=zh&img={urllib.parse.quote(img.group(1))}&uc={uc.group(1)}"
            )
            st2, frag = self._get(api, referer=detail_url, ajax=True)
            if st2 == 200:
                magnets = self._parse_rows(frag)

        if not magnets:
            # 回退：直接从详情页解析 magnet: 链接
            magnets = self._parse_rows(body)

        # 去重（同一 btih 只保留一条）
        seen: set[str] = set()
        deduped = []
        for m in magnets:
            key = m["btih"] or m["magnet"]
            if key in seen:
                continue
            seen.add(key)
            deduped.append(m)
        return deduped, cover_url, samples

    def fetch_cover_only(self, code: str) -> tuple[str | None, list[str]]:
        """轻量抓取：只取 JAVBUS 封面 + 预览图（单次请求，不做磁链 ajax）。"""
        detail_url = f"{self.base_url}/{code}"
        status, body = self._get(detail_url, referer=self.base_url + "/")
        if status != 200:
            return None, []
        img = _RE_IMG.search(body)
        cover_url = f"{self.base_url}{img.group(1)}" if img else None
        samples: list[str] = []
        seen: set[str] = set()
        for m in _RE_SAMPLE.findall(body):
            u = self.base_url + m
            if u not in seen:
                seen.add(u)
                samples.append(u)
        return cover_url, samples

    def _parse_rows(self, html_text: str) -> list[dict]:
        out = []
        for row in _RE_TR.findall(html_text):
            cells = _RE_TD.findall(row)
            if not cells:
                continue
            cell0 = cells[0]
            href = _RE_HREF.search(cell0) or _RE_ONCLICK.search(cell0)
            if not href:
                continue
            magnet = html.unescape(href.group(1))
            btih_m = _RE_BTIH.search(magnet)
            out.append({
                "name": _clean_name(cell0),
                "size": _strip_tags(cells[1]) if len(cells) > 1 else "",
                "date": _strip_tags(cells[2]) if len(cells) > 2 else "",
                "magnet": magnet,
                "has_hd": 1 if "高清" in cell0 else 0,
                "has_sub": 1 if "字幕" in cell0 else 0,
                "btih": btih_m.group(1).lower() if btih_m else None,
            })
        return out
