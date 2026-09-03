"""JD online — JAVDB 媒体管理中心 Web 界面（Flask）。

启动：python webapp.py   （默认 http://0.0.0.0:9091）
登录：123 / abc123（可在设置页修改）
所有需要用户提供的凭据（Emby API Key、JAVDB 账号密码、网络代理等）
都在「设置」页填写，存入 config.json + SQLite。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime
from urllib.parse import quote, unquote, urlparse

from flask import Flask, g, jsonify, make_response, redirect, render_template, request, session, url_for

from javdb import config as cfgmod
from javdb import clouddrive2, pan115, scrape, subscriptions, sync
from javdb.client import JavdbClient
from javdb.db import Database
from javdb.http import make_opener
from javdb.javbus import JavbusClient, is_uncensored, is_chinese
from javdb.mediaserver import MediaServerClient

CONFIG_PATH = "config.json"
DEFAULT_JAVBUS = "https://www.javbus.com"

# 图片代理允许转发的域名（防 SSRF，避免被当开放代理）
ALLOWED_IMG_HOSTS = {"tp.spfcas.com", "c0.jdbstatic.com", "javdb.com", "www.javdb.com",
                     "avdb.com", "javbus.com", "www.javbus.com", "pics.javbus.com"}
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


# ---------- 轻量 cron 匹配（分 时 日 月 星期，0=周日） ----------
def _cron_field_match(pat: str, val: int) -> bool:
    for part in pat.split(","):
        part = part.strip()
        if not part:
            continue
        if part == "*":
            return True
        if part.startswith("*/"):
            step = int(part[2:])
            if step and val % step == 0:
                return True
            continue
        if "/" in part:
            base, step = part.split("/")
            step = int(step or 1)
            if base == "*":
                if step and val % step == 0:
                    return True
            elif val >= int(base) and (val - int(base)) % step == 0:
                return True
            continue
        if "-" in part:
            a, b = part.split("-")
            if int(a) <= val <= int(b):
                return True
            continue
        if int(part) == val:
            return True
    return False


def _cron_match(expr: str, now: datetime) -> bool:
    try:
        f = expr.split()
        if len(f) != 5:
            return False
        return (_cron_field_match(f[0], now.minute)
                and _cron_field_match(f[1], now.hour)
                and _cron_field_match(f[2], now.day)
                and _cron_field_match(f[3], now.month)
                and _cron_field_match(f[4], now.isoweekday() % 7))
    except Exception:  # noqa: BLE001
        return False


_sync_thread_started = False


def start_sync_scheduler() -> None:
    """后台守护线程：按 config 的 sync_cron 定时全量同步媒体库。"""
    global _sync_thread_started
    if _sync_thread_started:
        return
    _sync_thread_started = True

    import time as _t

    def loop():
        last_key = None
        while True:
            try:
                cfg = cfgmod.load(CONFIG_PATH)
                cron = (cfg.get("sync_cron") or "").strip()
                now = datetime.now()
                if cron and _cron_match(cron, now):
                    key = now.strftime("%Y%m%d%H%M")
                    if key != last_key:
                        last_key = key
                        db = Database(cfg.get("db_path", "javdb.db"))
                        try:
                            if db.get_servers():
                                sync.sync_library(db, db.get_servers())
                        finally:
                            db.close()
            except Exception:  # noqa: BLE001
                pass
            _t.sleep(30)

    threading.Thread(target=loop, daemon=True).start()


def _in_times(times: list[str], now: datetime) -> bool:
    """now 的 HH:MM 是否落在 times（如 ["08:00","20:00"]）里。"""
    hm = now.strftime("%H:%M")
    return hm in times


_sub_sched_started = False


def start_subscription_scheduler() -> None:
    """后台守护线程：设置页「订阅配置」的到点调度。

    - 「订阅配置」(sub_check_enabled + sub_daily_times)：到点**检查全部订阅**(run_check，不推送)。
      无每日检查时间时，按 sub_check_interval 分钟间隔反复检查。
    - 「自动同步在线订阅」(sub_sync_enabled + sub_sync_times)：到点**推送**(全量订阅执行 = 订阅页「执行订阅」run-all)。
    - 触发的作业在独立子线程执行，避免阻塞；结果写 webapp.log；分钟级去重避免重复触发。
    """
    global _sub_sched_started
    if _sub_sched_started:
        return
    _sub_sched_started = True

    import time as _t

    def _fire(db_path: str, cfg: dict, kind: str, reason: str) -> None:
        def _job():
            try:
                print(f"[订阅调度] 开始 {reason}", flush=True)
                db = Database(db_path)
                try:
                    if kind == "check":
                        res = _run_subscription_check_all(db, cfg, pace=True)
                        print(f"[订阅调度] {reason} -> 检查 checked={res['checked']} "
                              f"failed={res['failed']}", flush=True)
                    else:
                        res = _run_subscription_full_push(db, cfg, pace=True)
                        print(f"[订阅调度] {reason} -> 推送 pushed={res['pushed']} "
                              f"failed={res['failed']} skipped={res['skipped']}", flush=True)
                finally:
                    db.close()
            except Exception as e:  # noqa: BLE001
                print(f"[订阅调度] {reason} 执行异常: {e}", flush=True)

        threading.Thread(target=_job, daemon=True).start()

    def loop():
        last_check_key = None       # 检查型(每日/区间)去重
        last_push_key = None        # 推送型(同步时间表)去重
        last_interval_fire = None   # 区间模式上次触发时间戳
        while True:
            try:
                cfg = cfgmod.load(CONFIG_PATH)
                db_path = cfg.get("db_path", "javdb.db")
                now = datetime.now()
                hm = now.strftime("%H:%M")
                min_key = now.strftime("%Y%m%d%H%M")
                daily = [t for t in (cfg.get("sub_daily_times") or []) if t]
                interval = max(120, int(cfg.get("sub_check_interval") or 0))
                sync_times = [t for t in (cfg.get("sub_sync_times") or []) if t]

                # —— 「订阅配置」：检查订阅(不推送) ——
                if cfg.get("sub_check_enabled"):
                    if daily:
                        if hm in daily and min_key != last_check_key:
                            last_check_key = min_key
                            _fire(db_path, cfg, "check", f"每日检查时间 {hm}")
                    elif interval > 0 and (last_interval_fire is None
                            or (now.timestamp() - last_interval_fire) >= interval * 60):
                        last_interval_fire = now.timestamp()
                        _fire(db_path, cfg, "check", f"检查间隔 {interval} 分钟")

                # —— 「自动同步在线订阅」：推送(全量订阅执行) ——
                if cfg.get("sub_sync_enabled") and sync_times and hm in sync_times \
                        and min_key != last_push_key:
                    last_push_key = min_key
                    _fire(db_path, cfg, "push", f"同步时间表 {hm}")
            except Exception:  # noqa: BLE001
                pass
            _t.sleep(20)

    threading.Thread(target=loop, daemon=True).start()


def _img_content_type(url: str) -> str:
    """按扩展名定 Content-Type。上游 CDN 有时返回 binary/octet-stream，
    导致浏览器不渲染，故不信任上游，直接按 URL 判断。"""
    p = urlparse(url).path.lower()
    if p.endswith(".png"):
        return "image/png"
    if p.endswith(".webp"):
        return "image/webp"
    if p.endswith(".gif"):
        return "image/gif"
    return "image/jpeg"


def _decode_scrambled(data: bytes) -> bytes:
    """解码 JAVDB 混淆图片：第 1 字节是 XOR 密钥，其余字节逐个 XOR 该密钥。"""
    if not data:
        return data
    key = data[0]
    return bytes(b ^ key for b in data[1:])


def _scrape_list_page(list_id: str, page: int, proxy: str | None):
    """从 JAVDB 官网清单页精确抓取影片（每页 40 部）。返回 (movies, total)。"""
    url = f"https://javdb.com/lists/{list_id}?page={page}"
    headers = {"User-Agent": BROWSER_UA, "Accept": "text/html,application/xhtml+xml"}
    body = None
    for opener in (make_opener(None), make_opener(proxy)):
        try:
            req = urllib.request.Request(url, headers=headers)
            with opener.open(req, timeout=20) as r:
                body = r.read().decode("utf-8", "replace")
            break
        except Exception:  # noqa: BLE001
            continue
    if body is None:
        return None, 0  # 网络失败（区别于正常空页 []）

    movies = []
    for href, title, inner in re.findall(
            r'<a href="(/v/[A-Za-z0-9]+)" class="box" title="([^"]*)">(.*?)</a>', body, re.S):
        num = re.search(r"<strong>([^<]+)</strong>", inner)
        cover = re.search(r'<img[^>]*src="([^"]+)"', inner)
        date = re.search(r'<div class="meta">\s*([^\s<]+)', inner)
        movies.append({
            "id": href.split("/v/")[-1],
            "number": num.group(1).strip() if num else "",
            "title": title,
            "cover_url": cover.group(1) if cover else "",
            "release_date": date.group(1) if date else "",
        })

    total = 0
    m = re.search(r"(\d+)\s*部影片", body)
    if m:
        total = int(m.group(1))
    return movies, total


# ---------- 清单页抓取缓存 ----------
# 现场逐页抓 JAVDB 官网不稳定（偶发整次失败），会造成翻页空白。
# 抓全一次后缓存 TTL 内的清单内容，翻页/换筛选直接复用，不再重复现场抓取。
_list_movies_cache: dict[str, list] = {}
_list_movies_cache_ts: dict[str, float] = {}
_list_movies_lock = threading.Lock()
_LIST_MOVIES_TTL = 600  # 秒，缓存 10 分钟
_LIST_MOVIES_MAX = 200  # 缓存条目上限，超出清理最旧的


def _fetch_list_movies(list_id: str, proxy: str | None) -> list:
    """获取清单全量影片（每页 40 部，最多 20 页），带内存缓存 + 单页重试。

    命中未过期缓存直接返回；未命中则逐页抓取，成功后才写入缓存。
    - 单页网络失败重试 2 次，仍失败：若已抓到部分数据则保留该部分，
      若第一页就失败则抛异常（让页面显示错误而非「暂无影片」）。
    - 正常空页（[]）视为已到末页，正常结束。
    """
    now = time.time()
    with _list_movies_lock:
        if (list_id in _list_movies_cache
                and now - _list_movies_cache_ts.get(list_id, 0) < _LIST_MOVIES_TTL):
            return _list_movies_cache[list_id]

    all_items: list = []
    for p in range(1, 21):
        batch = None
        for attempt in range(3):  # 单页网络失败最多重试 2 次
            batch, _ = _scrape_list_page(list_id, p, proxy)
            if batch is not None:  # 成功（含正常空页）
                break
            time.sleep(0.8 * (attempt + 1))
        if batch is None:  # 该页网络失败
            if not all_items:
                raise RuntimeError("无法连接 JAVDB 清单页，请稍后重试")
            break  # 已抓到部分，保留已抓部分
        if not batch:  # 正常空页 = 已到末页
            break
        all_items.extend(batch)
        if len(batch) < 40:
            break

    if all_items:
        with _list_movies_lock:
            _list_movies_cache[list_id] = all_items
            _list_movies_cache_ts[list_id] = now
            # 容量保护：超出上限时清掉最早写入的条目
            if len(_list_movies_cache) > _LIST_MOVIES_MAX:
                oldest = min(_list_movies_cache_ts, key=_list_movies_cache_ts.get)
                _list_movies_cache.pop(oldest, None)
                _list_movies_cache_ts.pop(oldest, None)
    return all_items


# ---------- 评论区磁链提取 ----------
_RE_MAGNET = re.compile(r"(?:magnet:\?[^\s<>\"']+|ed2k://[^\s<>\"']+)", re.I)


def _extract_links(content: str) -> list[dict]:
    """从一条评论正文里提取磁链/ED2K 链接（含名称/大小/标签等解析）。

    返回 [{magnet, name, size, kind, comment, has_hd, has_cn, has_uc}]，
    不含分享者等上下文字段（由调用方补充）。
    """
    if not content:
        return []
    links = _RE_MAGNET.findall(content)
    if not links:
        return []
    # 描述文本 = 评论去掉链接后的剩余部分（用于大小/标签判断）
    text = _RE_MAGNET.sub(" ", content)
    text = re.sub(r"\s+", " ", text).strip()
    out: list[dict] = []
    for link in links:
        kind = "ed2k" if link.lower().startswith("ed2k://") else "magnet"
        # 名称：优先取磁链 dn= 参数（解码），否则用评论描述
        name = ""
        dm = re.search(r"[?&]dn=([^&\s]+)", link)
        if dm:
            try:
                name = unquote(dm.group(1))
            except Exception:  # noqa: BLE001
                name = dm.group(1)
        if not name:
            name = (text or link)[:80]
        size = ""
        sm = re.search(r"(\d+(?:\.\d+)?)\s*(GB|GiB|G|MB|MiB|M)", text)
        if sm:
            size = f"{sm.group(1)} {sm.group(2)}"
        out.append({
            "magnet": link,
            "name": name,
            "size": size,
            "kind": kind,
            "comment": text,
            "has_hd": 1 if re.search(r"高清|HD|4K|8K|超清", text, re.I) else 0,
            "has_cn": 1 if re.search(r"字幕|中字|中文|国语|chinese|chs|cht", text, re.I) else 0,
            "has_uc": 1 if re.search(r"破解|无码|流出|无修正|uncensored", text, re.I) else 0,
        })
    return out


def _comment_magnets(db, movie_id: str) -> list[dict]:
    """从某部影片的评论里提取用户贴出的磁链/ED2K 链接。

    每条评论可能含多条链接；组装成与磁链卡同构的 dict，额外带 sharer（分享者）：
    {magnet, name, size, date, kind, sharer, sharer_id, comment, has_hd, has_cn, has_uc}
    """
    rows = db.conn.execute(
        "SELECT * FROM reviews WHERE movie_id=? ORDER BY created_at DESC", (movie_id,)
    ).fetchall()
    pushed_set = db.pushed_magnet_set()
    out: list[dict] = []
    for r in rows:
        for item in _extract_links(r["content"] or ""):
            item = dict(item)
            item["date"] = (r["created_at"] or "")[:10]
            item["sharer"] = r["username"] or ""
            item["sharer_id"] = r["user_id"] or ""
            item["pushed"] = 1 if item["magnet"] in pushed_set else 0
            out.append(item)
    return out


_push_verify_thread_started = False


def start_push_verify_worker() -> None:
    """后台守护线程：轮询 115 离线任务，确认自动推送结果，失败换下一颗磁链重试。"""
    global _push_verify_thread_started
    if _push_verify_thread_started:
        return
    _push_verify_thread_started = True

    def loop():
        import time as _t

        while True:
            try:
                cfg = cfgmod.load(CONFIG_PATH)
                db = Database(cfg.get("db_path", "javdb.db"))
                cfg115 = db.get_pan115_config()
                if cfg115.get("cookie") and cfg115.get("enabled"):
                    service = subscriptions.SubscriptionPushService(
                        db, lambda magnet, name, size, movie_id, code:
                            _do_push(db, magnet, name, size, movie_id, code)
                    )
                    try:
                        subscriptions.verify_and_retry(db, service, cfg115["cookie"])
                    except Exception:  # noqa: BLE001
                        pass
                db.close()
            except Exception:  # noqa: BLE001
                pass
            _t.sleep(30)

    threading.Thread(target=loop, daemon=True).start()


def build_client(cfg: dict) -> JavdbClient:
    return JavdbClient(
        username=cfg.get("javdb_username") or None,
        password=cfg.get("javdb_password") or None,
        token=cfg.get("javdb_token") or None,
        min_interval=float(cfg.get("min_interval", 0.5)),
        proxy=cfg.get("proxy") or None,
        api_base=cfg.get("api_base") or None,
    )


def build_javbus(cfg: dict) -> JavbusClient:
    return JavbusClient(base_url=cfg.get("javbus_base") or DEFAULT_JAVBUS,
                        proxy=cfg.get("proxy") or None)


# ---------------------------------------------------------------- 下载器类型接入注册表
# 未来新增下载器：在此加一个 {kind: {ready, push}} 条目即可（ready=配置满足，push=真正推送）。
# 手动推送时弹窗列出「优先启用 + ready」的下载器；再按 kind 走对应 push。
def _pan115_ready(db: Database) -> bool:
    c = db.get_pan115_config()
    return bool(c.get("enabled") and c.get("cookie") and c.get("target_cid"))


def _pan115_push(db: Database, magnet: str) -> tuple[bool, str]:
    c = db.get_pan115_config()
    if not c.get("cookie"):
        return False, "未配置 115 Cookie"
    if not c.get("target_cid"):
        return False, "未设置 115 下载目录"
    try:
        pan115.add_magnet(c["cookie"], magnet, c["target_cid"])
        return True, "已推送"
    except Exception as e:  # noqa: BLE001
        return False, f"推送失败: {e}"


def _cd2_ready(db: Database) -> bool:
    c = db.get_clouddrive2_config()
    has_auth = bool(c.get("api_token") or (c.get("user_name") and c.get("password")))
    return bool(c.get("enabled") and c.get("host") and c.get("rpc_port") and has_auth)


def _cd2_push(db: Database, magnet: str) -> tuple[bool, str]:
    c = db.get_clouddrive2_config()
    if not c.get("enabled"):
        return False, "CloudDrive2 未启用"
    token = (c.get("api_token") or "").strip()
    if not token and c.get("user_name") and c.get("password"):
        try:
            token = clouddrive2.get_token(c["host"], c["rpc_port"],
                                          c["user_name"], c["password"])
        except Exception as e:  # noqa: BLE001
            return False, f"CloudDrive2 登录失败: {e}"
    if not token:
        return False, "未配置 CloudDrive2 API Token / 账号密码"
    target = (c.get("save_path") or "").strip() or "/downloads"
    try:
        clouddrive2.add_offline_files(c["host"], c["rpc_port"], magnet, target, token)
        return True, "已推送"
    except Exception as e:  # noqa: BLE001
        return False, f"推送失败: {e}"


_DL_HANDLERS: dict = {
    "pan115": {"ready": _pan115_ready, "push": _pan115_push},
    "clouddrive2": {"ready": _cd2_ready, "push": _cd2_push},
}


def _do_push(db: Database, magnet: str, name: str = "", size: str = "",
             movie_id: str | None = None, code: str = "",
             downloader: str | None = None) -> tuple[bool, int, str]:
    """把磁链推送到指定下载器（手动选择）或第一个可用的（自动）。返回 (ok, qid, 描述)。"""
    magnet = (magnet or "").strip()
    if not magnet:
        return False, 0, "缺少磁链"
    if downloader:
        dl = next((d for d in db.list_downloaders()
                   if d["name"] == downloader and d["enabled"]), None)
        if not dl:
            return False, 0, "所选下载器不存在或未启用"
    else:
        dl = _primary_downloader(db)
    if not dl:
        return False, 0, "没有启用的下载器，请先在设置里启用"
    qid = db.add_push(magnet=magnet, name=name, size=size, movie_id=movie_id, code=code,
                      downloader=dl["name"])
    handler = _DL_HANDLERS.get(dl["kind"])
    if handler:
        ok, msg = handler["push"](db, magnet)
        if ok:
            db.set_push_status(qid, "pushed")
            return True, qid, f"已推送到 {dl['name']}"
        return False, qid, msg
    return True, qid, "已加入推送队列"


def _primary_downloader(db: Database) -> dict | None:
    """下载器优先级里「排第一且已启用」的下载器（未接入类型自动跳过）。

    供订阅「执行操作」类批量推送自动使用：不弹选项，直接取最前启用的那个。
    """
    for d in db.list_downloaders():
        if d["enabled"] and d["kind"] in _DL_HANDLERS:
            return d
    return None


def _available_downloaders(db: Database) -> list[dict]:
    """所有「优先启用 + 已配置」的下载器，按优先级顺序（供手动推送弹窗）。"""
    return [{"name": d["name"], "kind": d["kind"]}
            for d in db.list_downloaders()
            if d["enabled"] and d["kind"] in _DL_HANDLERS
            and _DL_HANDLERS[d["kind"]]["ready"](db)]


def _pace_sleep(cfg: dict) -> None:
    """按「间隔范围(秒)」在两次订阅操作间随机停顿，避免瞬间打满接口触发风控。"""
    lo = int(cfg.get("sub_interval_min") or 0)
    hi = int(cfg.get("sub_interval_max") or 0)
    if lo > 0 and hi >= lo:
        import random
        import time as _t
        _t.sleep(random.uniform(lo, hi))


def _run_subscription_full_push(db: Database, cfg: dict | None = None,
                                pace: bool = False) -> dict:
    """全量订阅执行：遍历所有启用订阅 auto_push。

    与订阅页右上角操作下拉的「执行订阅」run-all 一致（检查 + 自动推送最优磁链，
    经 _do_push 使用 _primary_downloader 主下载器）。供接口与定时调度复用。
    某影片推送失败仅记一次、跳过本轮，等下次调度再推（不重试本轮）。
    pace=True（定时调度）时，逐订阅之间按「间隔范围(秒)」随机停顿，降低风控风险。
    """
    service = subscriptions.SubscriptionPushService(
        db, lambda magnet, name, size, movie_id, code:
            _do_push(db, magnet, name, size, movie_id, code)
    )
    rows = db.conn.execute(
        "SELECT id FROM subscriptions WHERE status='active' AND enabled=1 ORDER BY id"
    ).fetchall()
    pushed = failed = skipped = 0
    results = []
    for r in rows:
        sid = r["id"]
        sub = db.get_subscription(sid)
        try:
            result = service.auto_push(sid)
        except Exception as e:  # noqa: BLE001
            result = {"ok": False, "message": str(e)}
        if result.get("ok"):
            pushed += 1
        elif result.get("message") and ("需手动确认" in result["message"]
                                        or "已完成" in result["message"]
                                        or "没有完全命中" in result["message"]
                                        or "入库" in result["message"]
                                        or "跳过" in result["message"]):
            skipped += 1
        else:
            failed += 1
        results.append({"id": sid, "name": sub["target_name"] if sub else sid,
                        "ok": bool(result.get("ok")), "message": result.get("message", "")})
        if pace and cfg:
            _pace_sleep(cfg)
    return {"ok": True, "pushed": pushed, "failed": failed, "skipped": skipped, "results": results}


def _run_subscription_check_all(db: Database, cfg: dict, pace: bool = False) -> dict:
    """检查全部订阅：遍历所有启用订阅 run_check，刷新命中数据（不推送）。"""
    service = subscriptions.SubscriptionCheckService(
        db, build_client(cfg), build_javbus(cfg)
    )
    rows = db.conn.execute(
        "SELECT id FROM subscriptions WHERE status='active' AND enabled=1 ORDER BY id"
    ).fetchall()
    checked = failed = 0
    for r in rows:
        try:
            service.run_check(r["id"])
            checked += 1
        except Exception:  # noqa: BLE001
            failed += 1
        if pace:
            _pace_sleep(cfg)
    return {"ok": True, "checked": checked, "failed": failed}


def _background_fetch_movie(db_path: str, movie_id: str, number: str,
                            api_base: str, javbus_base: str, proxy: str) -> None:
    """后台线程：补抓一部影片的完整详情 / 磁链 / 评论（各自缺失才抓）。"""
    try:
        db = Database(db_path)
        client = JavdbClient(api_base=api_base or None, proxy=proxy or None, min_interval=0)
        javbus = JavbusClient(base_url=javbus_base or DEFAULT_JAVBUS, proxy=proxy or None)
        if not db.conn.execute("SELECT 1 FROM movie_actors WHERE movie_id=?", (movie_id,)).fetchone():
            try:
                scrape.ingest_movie_id(client, db, movie_id)
            except Exception:  # noqa: BLE001
                pass
        if not db.conn.execute("SELECT 1 FROM magnets WHERE movie_id=?", (movie_id,)).fetchone():
            try:
                scrape.ingest_magnets(javbus, db, number, movie_id)
            except Exception:  # noqa: BLE001
                pass
        if not db.conn.execute("SELECT 1 FROM reviews WHERE movie_id=?", (movie_id,)).fetchone():
            try:
                scrape.ingest_reviews(client, db, movie_id, max_pages=5)
            except Exception:  # noqa: BLE001
                pass
        db.close()
    except Exception:  # noqa: BLE001
        pass


def _resolve_id(client: JavdbClient, target: str) -> str:
    """番号含 '-' 则搜索取第一部；否则视为 API id。"""
    if "-" in target:
        res = client.search(target, limit=1)
        movies = (res.get("data") or {}).get("movies") or []
        if not movies:
            raise ValueError(f"未找到番号: {target}")
        return movies[0]["id"]
    return target


def _resolve_code_movie(client: JavdbClient, db: Database, target: str) -> tuple[str, str | None]:
    """返回 (code, movie_id)。"""
    if "-" in target:
        row = db.movie_by_number(target)
        return target, (row["id"] if row else None)
    row = db.get_movie(target)
    if row and row["number"]:
        return row["number"], target
    res = client.movie(target)
    return res["data"]["movie"]["number"], target


TYPE_LABELS = {"movie": "影片", "online": "在线", "actor": "演员", "list": "清单",
               "volunteer": "影片"}

# 类别过滤选项：参考 5.png + 库中影片标签词频，去重排序
CATEGORY_SEED = ["16小时以上作品", "4小时以上作品", "按摩", "白天出勤", "唯美",
                 "学生", "OL", "人妻", "邻居", "乘务", "教师", "护士", "巨乳",
                 "动漫", "素人", "NTR", "3P", "SM", "痴汉", "中出", "口交"]


def _lib_codes(db) -> set[str]:
    """媒体库中已入库的番号集合。"""
    return {r["code"] for r in db.conn.execute("SELECT DISTINCT code FROM library_items")}


# 外置播放器（通过系统 URL scheme 唤起本地应用；协议未注册时前端兜底「复制播放地址」）
EXTERNAL_PLAYERS = [
    {"key": "potplayer", "label": "PotPlayer", "scheme": "potplayer://"},
    {"key": "mpv",       "label": "mpv",        "scheme": "mpv://"},
    {"key": "vlc",       "label": "VLC",        "scheme": "vlc://"},
]


def _emby_stream_url(server_url: str, item_id: str, api_key: str, hls: bool = False) -> str:
    """拼出 Emby/Jellyfin 的影片流地址。

    hls=False → 直接输出视频流（static=true 跳过转码）；hls=True → 转码 HLS 播单。
    """
    base = f"{server_url}".rstrip("/")
    path = f"/Videos/{item_id}/master.m3u8" if hls else f"/Videos/{item_id}/stream"
    return f"{base}{path}?static=true&api_key={api_key}"


def _parse_t_param(url: str) -> int | None:
    """从 URL query 里取 unix 到期时间 t=。用于判断 preview_video_url 预告流签名是否过期。"""
    try:
        q = urlparse(url).query
        for kv in q.split("&"):
            if kv.startswith("t="):
                return int(kv[2:])
    except (ValueError, TypeError):
        return None
    return None


def _tag_vocabulary(db) -> list[str]:
    seen: set[str] = set()
    for row in db.conn.execute("SELECT tags FROM movies WHERE tags IS NOT NULL"):
        try:
            for tag in json.loads(row["tags"]):
                if isinstance(tag, str) and tag:
                    seen.add(tag)
        except (ValueError, TypeError):
            continue
    seen.update(CATEGORY_SEED)
    return sorted(seen)


def _want_card(item: dict, db: Database) -> dict:
    """给想看页订阅卡片补齐封面 / 番号 / 标题 / 日期展示字段。"""
    card = dict(item)
    target_type = item.get("target_type") or ""
    movie = None
    if target_type == "movie" and item.get("target_id"):
        row = db.get_movie(item["target_id"])
        movie = dict(row) if row else None

    # 演员订阅：显示演员头像 + 演员名（参考 6.png）；清单订阅：字母徽标 + 名称（参考 11.png）
    card["kind"] = {"actor": "actor", "list": "list"}.get(target_type, "movie")
    card["cover"] = ""
    card["number"] = ""
    card["actor_avatar"] = ""
    card["list_logo"] = ""
    if target_type == "actor" and item.get("target_id"):
        actor = db.conn.execute("SELECT name, avatar_url FROM actors WHERE id=?",
                                (item["target_id"],)).fetchone()
        card["actor_avatar"] = actor["avatar_url"] if actor else ""
    if target_type == "list":
        name = (item.get("target_name") or "清").strip()
        card["list_logo"] = name[:1]

    if movie:
        card["cover"] = (movie.get("cover_url") or movie.get("javbus_cover")) or ""
        card["number"] = movie.get("number") or ""
    card["title"] = (movie.get("title") or movie.get("origin_title")) if movie else item.get("target_name") or ""
    card["type_label"] = TYPE_LABELS.get(target_type, target_type)
    card["is_movie"] = 1 if target_type == "movie" else 0
    # 想看页影片订阅卡：算出可跳转到详情页的地址
    card["movie_url"] = ""
    if target_type == "movie":
        mid = item.get("target_id") or ""
        if mid and db.get_movie(mid):
            card["movie_url"] = f"/movie/{mid}"
        else:
            row = db.movie_by_number(mid) if mid else None
            if row:
                card["movie_url"] = f"/movie/{row['id']}"
    stamp = item.get("updated_at") or item.get("last_checked_at") or item.get("created_at") or ""
    card["date_str"] = stamp[:16].replace("T", " ")
    return card


def _completed_card(item: dict, db: Database) -> dict:
    """把推送成功的影片行转成想看页视频卡需要的字段。"""
    movie = db.get_movie(item.get("movie_id")) if item.get("movie_id") else None
    card = {
        "id": item["sub_id"],
        "kind": "movie",
        "cover": item.get("cover") or "",
        "number": item.get("number") or "",
        "title": item.get("title") or item.get("target_name") or "",
        "type_label": TYPE_LABELS.get(item.get("target_type"), item.get("target_type") or ""),
        "date_str": item.get("date_str") or "",
        "status": "completed",
        "run_count": 0,
        "push_count": 1,
        "source_name": item.get("target_name") or "",
        "movie_url": f"/movie/{movie['id']}" if movie else "",
    }
    return card


def create_app() -> Flask:
    app = Flask(__name__, template_folder="web/templates", static_folder="web/static")
    cfg = cfgmod.load(CONFIG_PATH)
    app.secret_key = cfg.get("secret_key") or "dev-secret"

    def get_cfg() -> dict:
        return cfgmod.load(CONFIG_PATH)

    def get_db() -> Database:
        if "db" not in g:
            g.db = Database(get_cfg().get("db_path", "javdb.db"))
        return g.db

    @app.context_processor
    def _global_categories():
        """全局注入订阅弹窗需要的类别词库（各页面都能用订阅弹窗）。"""
        try:
            return {"categories": _tag_vocabulary(get_db())}
        except Exception:  # noqa: BLE001
            return {"categories": []}

    @app.after_request
    def nocache_static(resp):
        # 静态资源（尤其 theme.css）设为不缓存，避免浏览器用旧版样式
        if request.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.teardown_appcontext
    def close_db(exc):
        db = g.pop("db", None)
        if db:
            db.close()

    @app.before_request
    def require_login():
        if request.endpoint in ("login", "static", "img_proxy"):
            return None
        if not session.get("logged_in"):
            nxt = request.full_path if request.query_string else request.path
            return redirect(url_for("login", next=nxt))
        return None

    # 图片代理：浏览器只访问本服务器，由服务器抓图转发（绕开客户端网络/hotlink 问题）
    @app.route("/img")
    def img_proxy():
        cfg = get_cfg()
        url = request.args.get("url", "")
        if not url or not url.startswith(("http://", "https://")):
            return "", 400
        host = urlparse(url).netloc.lower()
        if not (host in ALLOWED_IMG_HOSTS or host.endswith(".jdbstatic.com")
                or host.endswith(".spfcas.com") or host.endswith(".javbus.com")):
            return "", 403
        # 混淆图（tp.spfcas.com/rhe951l4q）直连后本地解码；线路：直连/代理/自动
        is_scrambled = "/rhe951l4q/" in url
        referer = "https://www.javbus.com/" if "javbus" in host else "https://javdb.com/"
        route = cfg.get("image_route") or "auto"
        proxy = cfg.get("proxy") or None

        def _fetch(opener):
            req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Referer": referer})
            return opener.open(req, timeout=20).read()

        data = None
        if route == "proxy" and proxy:
            try:
                data = _fetch(make_opener(proxy))
            except Exception:  # noqa: BLE001
                data = None
        elif route == "direct":
            try:
                data = _fetch(make_opener(None))
            except Exception:  # noqa: BLE001
                data = None
        else:  # auto：直连优先，失败走代理备选
            try:
                data = _fetch(make_opener(None))
            except Exception:  # noqa: BLE001
                data = None
            if data is None and proxy:
                for _ in range(2):
                    try:
                        data = _fetch(make_opener(proxy))
                        break
                    except Exception:  # noqa: BLE001
                        time.sleep(0.5)
        if data is None:
            return "", 404
        if is_scrambled:
            data = _decode_scrambled(data)
        resp = make_response(data)
        resp.headers["Content-Type"] = _img_content_type(url)
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp

    app.jinja_env.globals["imgproxy"] = lambda u: ("/img?url=" + quote(u, safe="") + "&v=3") if u else ""

    # ---------- 页面 ----------
    @app.route("/login", methods=["GET", "POST"])
    def login():
        cfg = get_cfg()
        error = None
        nxt = request.args.get("next", "")
        if request.method == "POST":
            u = request.form.get("username", "")
            p = request.form.get("password", "")
            if u == cfg.get("web_username", "123") and p == cfg.get("web_password", "abc123"):
                session["logged_in"] = True
                session.permanent = True
                return redirect(nxt or url_for("index"))
            error = "用户名或密码错误"
        return render_template("login.html", error=error, next=nxt)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    def index():
        db = get_db()
        cfg = get_cfg()
        q = request.args.get("q", "").strip()
        st = request.args.get("st", "number").strip()
        lib_codes = {r["code"] for r in db.conn.execute("SELECT DISTINCT code FROM library_items")}

        st_to_mt = {"all": "all", "number": "all", "actor": "actor", "series": "series",
                    "maker": "maker", "director": "director", "label": "label"}
        st_label = {"all": "列表搜索", "number": "番号直达", "actor": "演员搜索", "series": "系列搜索",
                    "maker": "片商搜索", "director": "导演搜索", "label": "清单搜索"}.get(st, "搜索")

        if q:
            # 番号直达：库里已有精确匹配则直接跳详情
            if st == "number":
                exact = db.movie_by_number(q.upper())
                if exact:
                    return redirect(url_for("detail", movie_id=exact["id"]))
            rows = []
            try:
                res = build_client(cfg).search(q, limit=24, movie_type=st_to_mt.get(st, "all"))
                ids = []
                for m in (res.get("data") or {}).get("movies") or []:
                    if not db.has_movie(m["id"]):
                        db.upsert_movie(scrape.normalize_movie(m))
                    ids.append(m["id"])
                db.commit()
                if ids:
                    ph = ",".join("?" * len(ids))
                    row_map = {r["id"]: r for r in
                               db.conn.execute(f"SELECT * FROM movies WHERE id IN ({ph})", ids).fetchall()}
                    rows = [row_map[i] for i in ids if i in row_map]
            except Exception:  # noqa: BLE001
                rows = []
        else:
            rows = db.conn.execute("SELECT * FROM movies ORDER BY COALESCE(last_viewed_at, fetched_at) DESC LIMIT 40").fetchall()

        return render_template("index.html", movies=rows, counts=db.counts(),
                               lib_codes=lib_codes, q=q, st=st, st_label=st_label, active="index")

    @app.route("/movie/<movie_id>")
    def detail(movie_id):
        db = get_db()
        cfg = get_cfg()
        row = db.get_movie(movie_id)
        if not row:
            # 不在库里（比如从清单页点进来的）：先抓取完整详情
            try:
                scrape.ingest_movie_id(build_client(cfg), db, movie_id)
                row = db.get_movie(movie_id)
            except Exception:  # noqa: BLE001
                row = None
            if not row:
                return "未找到该影片", 404

        # 有缺失数据则后台补抓（不阻塞页面）；_auto 标记避免自动刷新死循环
        has_actors = db.conn.execute(
            "SELECT 1 FROM movie_actors WHERE movie_id=?", (movie_id,)).fetchone() is not None
        has_magnets = db.conn.execute(
            "SELECT 1 FROM magnets WHERE movie_id=?", (movie_id,)).fetchone() is not None
        has_reviews = db.conn.execute(
            "SELECT 1 FROM reviews WHERE movie_id=?", (movie_id,)).fetchone() is not None
        pending_fetch = (not has_actors or not has_magnets or not has_reviews) and not request.args.get("_auto")
        if pending_fetch:
            threading.Thread(
                target=_background_fetch_movie,
                args=(cfg.get("db_path", "javdb.db"), movie_id, row["number"],
                      cfg.get("api_base") or None, cfg.get("javbus_base") or DEFAULT_JAVBUS,
                      cfg.get("proxy") or ""),
                daemon=True,
            ).start()

        # —— 先渲染已有数据（后台补抓完成后由页面自动刷新显示全量）——
        movie = dict(row)
        movie["tags_list"] = json.loads(movie["tags"]) if movie.get("tags") else []
        movie["preview_list"] = json.loads(movie["preview_images"]) if movie.get("preview_images") else []
        # 关联影片（来自详情接口 data.movie.relative_movies）
        relative_movies = []
        if movie.get("raw"):
            try:
                for x in (json.loads(movie["raw"]).get("relative_movies") or []):
                    if x.get("id"):
                        relative_movies.append({
                            "id": x["id"], "number": x.get("number") or "",
                            "thumb": x.get("thumb_url") or "",
                        })
            except Exception:  # noqa: BLE001
                relative_movies = []
        actors = db.conn.execute(
            "SELECT a.* FROM actors a JOIN movie_actors ma ON ma.actor_id=a.id WHERE ma.movie_id=?",
            (movie_id,),
        ).fetchall()
        magnets = [dict(m) for m in db.conn.execute("SELECT * FROM magnets WHERE movie_id=?", (movie_id,)).fetchall()]
        pushed_set = db.pushed_magnet_set()
        for m in magnets:
            m["has_uc"] = 1 if is_uncensored(m.get("name")) else 0
            m["has_cn"] = 1 if is_chinese(m.get("name")) else 0
            m["pushed"] = 1 if m.get("magnet") in pushed_set else 0
        # 破解版排最前，其次字幕版，其余按日期（倒序）
        magnets.sort(key=lambda m: (m["has_uc"], m["has_cn"], m.get("date") or ""), reverse=True)
        rpage = max(1, request.args.get("rpage", 1, type=int))
        per_page = 15
        total_reviews = db.conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE movie_id=?", (movie_id,)).fetchone()[0]
        reviews = db.conn.execute(
            "SELECT * FROM reviews WHERE movie_id=? ORDER BY likes_count DESC LIMIT ? OFFSET ?",
            (movie_id, per_page, (rpage - 1) * per_page),
        ).fetchall()
        total_pages = max(1, (total_reviews + per_page - 1) // per_page)
        # 评论区用户贴出的磁链/ED2K（含分享者）
        comment_magnets = _comment_magnets(db, movie_id)
        # 关联清单（同步拉取，数据小）
        related_lists = []
        try:
            res = build_client(cfg).related(movie_id, limit=60)
            related_lists = (res.get("data") or {}).get("lists") or []
        except Exception:  # noqa: BLE001
            related_lists = []
        # 记录查看时间（用于首页「最近查看」排序）
        db.mark_viewed(movie_id)
        hits = sync.check_library(db, movie["number"] or "")
        return render_template(
            "detail.html", movie=movie, actors=actors, magnets=magnets, reviews=reviews, hits=hits,
            comment_magnets=comment_magnets,
            pending_fetch=pending_fetch,
            fetching_detail=pending_fetch and not has_actors,
            fetching_magnets=pending_fetch and not has_magnets,
            fetching_reviews=pending_fetch and not has_reviews,
            rpage=rpage, total_reviews=total_reviews, total_pages=total_pages,
            related_lists=related_lists, relative_movies=relative_movies,
            lib_codes=_lib_codes(db), active="index")

    @app.route("/api/user/<user_id>/shares")
    def user_shares(user_id):
        """某用户在本地库中分享过链接的所有影片（按影片聚合）。

        供「评论区分享」tab 点击用户名弹窗使用；数据来自移动端 API 入库的评论，
        与 「用户资源」同源（本地聚合，非 javdb.com 官网拉取）。
        """
        db = get_db()
        rows = db.reviews_by_user(user_id)
        username = ""
        pushed_set = db.pushed_magnet_set()
        lib_codes = _lib_codes(db)
        by_movie: dict = {}
        for r in rows:
            if not username and r["username"]:
                username = r["username"]
            links = _extract_links(r["content"] or "")
            if not links:
                continue
            mid = r["movie_id"] or ""
            if mid not in by_movie:
                by_movie[mid] = {
                    "movie_id": mid,
                    "number": r["number"] or "",
                    "title": r["title"] or "",
                    "poster_url": r["thumb_url"] or r["cover_url"] or "",
                    "release_date": r["release_date"] or "",
                    "in_library": 1 if (r["number"] or "") in lib_codes else 0,
                    "links": [],
                }
            for item in links:
                item = dict(item)
                item["date"] = (r["created_at"] or "")[:10]
                item["pushed"] = 1 if item["magnet"] in pushed_set else 0
                by_movie[mid]["links"].append(item)
        items = sorted(by_movie.values(), key=lambda m: m["number"] or "")
        return jsonify(ok=True, username=username, items=items,
                       following=db.is_following(user_id))

    @app.route("/api/user/<user_id>/follow", methods=["POST"])
    def user_follow(user_id):
        """关注某位分享者（可带 username 更新显示名）。"""
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return jsonify(ok=False, error="无效的用户 ID"), 400
        db = get_db()
        data = request.get_json(silent=True) or {}
        username = (data.get("username") or "").strip() or None
        db.follow_user(uid, username)
        return jsonify(ok=True, following=True, msg="已关注")

    @app.route("/api/user/<user_id>/unfollow", methods=["POST"])
    def user_unfollow(user_id):
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return jsonify(ok=False, error="无效的用户 ID"), 400
        db = get_db()
        db.unfollow_user(uid)
        return jsonify(ok=True, following=False, msg="已取消关注")

    @app.route("/settings")
    def settings_page():
        db = get_db()
        cfg = get_cfg()
        return render_template("settings.html", cfg=cfg, servers=db.list_servers(),
                               api_nodes=cfgmod.API_NODES, pan115=db.get_pan115_config(),
                               clouddrive2=db.get_clouddrive2_config(),
                               downloaders=db.list_downloaders(),
                               sync_cron=cfg.get("sync_cron", ""),
                               players=EXTERNAL_PLAYERS,
                               default_player=cfg.get("default_player", ""),
                               active="settings")

    @app.route("/top250")
    def top250_page():
        db = get_db()
        cfg = get_cfg()
        tab = request.args.get("tab", "top250")
        per_page = 40
        page = max(1, request.args.get("page", 1, type=int))
        client = build_client(cfg)
        movies, actors, error = [], [], None

        try:
            if tab in ("daily", "weekly", "monthly"):
                # 日/周/月榜：hot() 无 ranking 字段，按返回顺序补 ranking 后本地分页
                res = client.hot(period=tab)
                all_movies = []
                for i, m in enumerate((res.get("data") or {}).get("movies") or []):
                    m = dict(m)
                    m["ranking"] = i + 1
                    all_movies.append(m)
                count = len(all_movies)
                total_pages = max(1, (count + per_page - 1) // per_page)
                page = min(page, total_pages)
                start = (page - 1) * per_page
                movies = all_movies[start:start + per_page]
            elif tab == "top250":
                res = client.top250(type_="all", page=page, limit=per_page)
                movies = (res.get("data") or {}).get("movies") or []
                count = 250
                total_pages = (count + per_page - 1) // per_page
                if not movies and res.get("success") != 1:
                    error = res.get("message") or "排行榜获取失败"
            elif tab == "actor":
                # 演员热度榜：JAVDB /v1/rankings/actors，type=0 有码
                res = client.actor_rank(type_="0", page=1, limit=500)
                actors = (res.get("data") or {}).get("actors") or []
                data_total = ((res.get("data") or {}).get("total") or 0)
                count = int(data_total) if data_total else len(actors)
                if not actors and res.get("success") != 1:
                    error = res.get("message") or "演员榜获取失败"
                page, total_pages = 1, 1
            else:
                res = client.top250(type_="all", page=page, limit=per_page)
                movies = (res.get("data") or {}).get("movies") or []
                count = 250
                total_pages = (count + per_page - 1) // per_page
        except Exception as e:  # noqa: BLE001
            error = str(e)
            count, total_pages = 0, 1

        # 未入库的影片先写摘要行（不覆盖已有的完整详情）
        for m in movies:
            if m.get("id") and not db.has_movie(m["id"]):
                db.upsert_movie(scrape.normalize_movie(m))
        db.commit()

        return render_template("top250.html", movies=movies, actors=actors, error=error, page=page,
                               tab=tab, count=count, lib_codes=_lib_codes(db),
                               total_pages=total_pages, per_page=per_page, active="top250")

    @app.route("/search")
    def search_page():
        db = get_db()
        cfg = get_cfg()
        q = request.args.get("q", "").strip()
        stype = request.args.get("type", "all")
        filter_ = request.args.get("filter", "all")
        year = request.args.get("year", "")
        sort = request.args.get("sort", "release_date")
        page = max(1, request.args.get("page", 1, type=int))
        type_label = {"actor": "演员", "series": "系列", "maker": "片商",
                      "director": "导演", "number": "番号", "label": "清单"}.get(stype, "影片")
        TYPE_MAP = {"censored": 0, "uncensored": 1, "european": 2, "fc2": 3}

        def match_type(item):
            # 类别：优先用库里的 type，其次是番号 FC2 前缀，默认按有码(0)
            t = db.conn.execute("SELECT type FROM movies WHERE id=?", (item.get("id"),)).fetchone()
            if t is not None and t["type"] is not None:
                v = int(t["type"])
            elif (item.get("number") or "").upper().startswith("FC2"):
                v = 3
            else:
                v = 0
            return v == TYPE_MAP.get(filter_, -1) if filter_ != "all" else True

        if not q:
            return render_template("search.html", movies=[], q="", type_label=type_label,
                                   count=0, page=1, sort=sort, filter_=filter_, year=year,
                                   error=None, st=request.args.get("type", "number"), active="search")

        orig_type = request.args.get("type", "all")
        if stype not in ("actor", "series", "maker", "director", "label"):
            stype = "all"
        movies, error = [], None
        try:
            sort_by = {"score": "score", "date": "date"}.get(sort, "relevance")
            # 拉取该查询的全部结果（≤10 页 * 60），供本地二次过滤
            all_items, p = [], 1
            while p <= 10:
                res = build_client(cfg).request("GET", "/v2/search", {
                    "q": q, "page": p, "type": "movie", "limit": 60, "movie_type": stype,
                    "from_recent": "false", "movie_filter_by": "all", "movie_sort_by": sort_by,
                })
                batch = (res.get("data") or {}).get("movies") or []
                all_items.extend(batch)
                if len(batch) < 60:
                    break
                p += 1

            # 番号直达：精确匹配（规范化后比较，兼容 SSIS-001 / SSIS001 写法）
            if orig_type == 'number':
                def _norm(s): return re.sub(r'[^A-Z0-9]', '', (s or '').upper())
                target = _norm(q)
                all_items = [it for it in all_items if _norm(it.get('number')) == target]

            # 本地二次过滤：年份（release_date） + 类别（type）
            filtered = []
            for it in all_items:
                if year and not (it.get("release_date") or "").startswith(year):
                    continue
                if not match_type(it):
                    continue
                filtered.append(it)

            # 分页返回
            per = 24
            start = (page - 1) * per
            movies = filtered[start:start + per]
        except Exception as e:  # noqa: BLE001
            error = str(e)

        for m in movies:
            if m.get("id") and not db.has_movie(m["id"]):
                db.upsert_movie(scrape.normalize_movie(m))
        db.commit()

        return render_template("search.html", movies=movies, q=q, type_label=type_label,
                               count=len(filtered) if not error else 0, page=page, sort=sort,
                               filter_=filter_, year=year, error=error,
                               total_pages=((len(filtered) + 23) // 24) if not error else 1,
                               st=request.args.get("type", "number"), lib_codes=_lib_codes(db),
                               active="search")

    @app.route("/library")
    def library_page():
        db = get_db()
        page = max(1, request.args.get("page", 1, type=int))
        per_page = 60
        total = db.conn.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
        rows = db.conn.execute(
            "SELECT * FROM movies ORDER BY release_date DESC LIMIT ? OFFSET ?",
            (per_page, (page - 1) * per_page),
        ).fetchall()
        total_pages = max(1, (total + per_page - 1) // per_page)
        return render_template("library.html", movies=rows, page=page,
                               total_pages=total_pages, total=total, lib_codes=_lib_codes(db), active="library")

    @app.route("/want")
    def want_page():
        db = get_db()
        views = (
            {"key": "pending", "label": "订阅中", "icon": "bell"},
            {"key": "completed", "label": "已完成", "icon": "check-circle"},
            {"key": "online", "label": "在线订阅", "icon": "globe"},
            {"key": "actors", "label": "演员订阅", "icon": "user"},
            {"key": "lists", "label": "清单订阅", "icon": "list"},
            {"key": "blacklist", "label": "黑名单", "icon": "ban"},
        )
        allowed = {v["key"] for v in views}
        view = request.args.get("view", "pending")
        if view not in allowed:
            view = "pending"
        page = max(1, request.args.get("page", 1, type=int))
        per_page = 20
        counts = db.subscription_counts()
        total = counts[view]
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, total_pages)
        if view == "completed":
            # 已完成：跨订阅推送成功的影片列表，多时分页
            items = [_completed_card(it, db) for it
                     in db.completed_push_items(per_page, (page - 1) * per_page)]
        elif view == "blacklist":
            items = db.list_blacklist(limit=per_page, offset=(page - 1) * per_page)
            items = [_want_card(it, db) for it in items]
        else:
            items = [_want_card(it, db) for it in
                     db.list_subscriptions(view, limit=per_page, offset=(page - 1) * per_page)]
            # 演员/清单订阅：渲染时直接算卡片状态（还有“订阅中”影片则显示订阅中，不显示已完成）
            if view in ("actors", "lists"):
                try:
                    service = _subscription_services(db)
                    for it in items:
                        try:
                            sts = (service.actor_movie_statuses(it["id"]) if view == "actors"
                                   else service.list_movie_statuses(it["id"]))
                        except Exception:  # noqa: BLE001
                            sts = []
                        if sts:
                            it["status"] = "active" if any(s["sub_status"] == "active" for s in sts) else "completed"
                except Exception:  # noqa: BLE001
                    pass
        return render_template("want.html", views=views, view=view, counts=counts,
                               items=items, page=page, total_pages=total_pages,
                               categories=_tag_vocabulary(db), active="want")

    @app.route("/follow")
    def follow_page():
        db = get_db()
        users = db.list_followed_users()
        return render_template("follow.html", users=users, empty=not users, active="follow")

    @app.route("/watched")
    def watched_page():
        return render_template("empty.html", title="看过", hint="暂无看过的记录",
                               icon="clock", active="watched")

    @app.route("/list/<list_id>")
    def list_page(list_id):
        db = get_db()
        cfg = get_cfg()
        page = max(1, request.args.get("page", 1, type=int))
        filter_ = request.args.get("filter", "all")
        year = request.args.get("year", "")
        sort = request.args.get("sort", "release_date")
        per_page = 40
        type_map = {"censored": 0, "uncensored": 1, "european": 2, "fc2": 3}
        name, movies, error, total, total_pages = "", [], None, 0, 1
        try:
            client = build_client(cfg)
            # 清单名（JAVDB API）
            lst = (client.list_info(list_id).get("data") or {}).get("list") or {}
            name = lst.get("name") or ""
            # 清单影片：带缓存 + 单页重试（见 _fetch_list_movies），
            # 供本地过滤/排序/分页，避免每次翻页现场抓取导致偶发空白
            all_items = _fetch_list_movies(list_id, cfg.get("proxy") or None)
            # 类别匹配：优先库里的 type，其次番号 FC2 前缀，默认按有码
            def match_type(item):
                tb = db.conn.execute(
                    "SELECT type FROM movies WHERE id=?", (item.get("id"),)).fetchone()
                if tb is not None and tb["type"] is not None:
                    v = int(tb["type"])
                elif (item.get("number") or "").upper().startswith("FC2"):
                    v = 3
                else:
                    v = 0
                return v == type_map.get(filter_, -1) if filter_ != "all" else True
            # 本地二次过滤：类别 + 年份
            filtered = []
            for it in all_items:
                if year and not (it.get("release_date") or "").startswith(year):
                    continue
                if not match_type(it):
                    continue
                filtered.append(it)
            # 排序：评分（库中 score） / 上映日期 / 相关度（保持官网顺序）
            if sort == "score":
                def _score(it):
                    r = db.conn.execute(
                        "SELECT score FROM movies WHERE id=?", (it.get("id"),)).fetchone()
                    return (r["score"] if r and r["score"] is not None else 0) or 0
                filtered.sort(key=_score, reverse=True)
            elif sort == "release_date":
                filtered.sort(key=lambda it: (it.get("release_date") or ""), reverse=True)
            # 本地分页
            total = len(filtered)
            total_pages = max(1, (total + per_page - 1) // per_page)
            page = min(page, total_pages)
            start = (page - 1) * per_page
            movies = filtered[start:start + per_page]
        except Exception as e:  # noqa: BLE001
            error = str(e)
        return render_template("list.html", movies=movies, name=name, error=error,
                               page=page, list_id=list_id, total=total, total_pages=total_pages,
                               filter_=filter_, year=year, sort=sort, lib_codes=_lib_codes(db), active="index")

    # ---------- 订阅管理 ----------
    def _subscription_services(db):
        cfg = get_cfg()
        return subscriptions.SubscriptionCheckService(
            db, build_client(cfg), build_javbus(cfg)
        )

    @app.route("/api/want/counts")
    def api_want_counts():
        return jsonify(ok=True, counts=get_db().subscription_counts())

    @app.route("/api/want/subscriptions")
    def api_want_subscriptions():
        db = get_db()
        view = request.args.get("view", "pending")
        if view not in ("pending", "completed", "online", "actors", "lists"):
            return jsonify(ok=False, error="非法视图"), 400
        page = max(1, request.args.get("page", 1, type=int))
        page_size = min(100, max(1, request.args.get("page_size", 20, type=int)))
        return jsonify(ok=True, items=db.list_subscriptions(
            view, page_size, (page - 1) * page_size), total=db.count_subscriptions(view))

    @app.route("/api/want/subscriptions", methods=["POST"])
    def api_want_subscription_create():
        db = get_db()
        data = request.get_json(silent=True) or {}
        try:
            clean, qualities = subscriptions.validate_subscription_payload(data)
            existing = db.subscription_by_target(clean["target_type"], clean["target_key"])
            if existing:
                return jsonify(ok=False, error="该目标已经订阅", id=existing["id"]), 409
            sid = db.create_subscription(clean, qualities)
            return jsonify(ok=True, subscription=db.get_subscription(sid)), 201
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 422
        except sqlite3.IntegrityError:
            return jsonify(ok=False, error="该目标已经订阅"), 409

    @app.get("/api/movie/<movie_id>/pending")
    def api_movie_pending(movie_id):
        """后台补抓是否完成：演员/磁链/评论是否都已入库。"""
        db = get_db()
        has_actors = db.conn.execute(
            "SELECT 1 FROM movie_actors WHERE movie_id=?", (movie_id,)).fetchone() is not None
        has_magnets = db.conn.execute(
            "SELECT 1 FROM magnets WHERE movie_id=?", (movie_id,)).fetchone() is not None
        has_reviews = db.conn.execute(
            "SELECT 1 FROM reviews WHERE movie_id=?", (movie_id,)).fetchone() is not None
        return jsonify(done=bool(has_actors and has_magnets and has_reviews))

    @app.get("/api/movie/<movie_id>/play")
    def api_movie_play(movie_id):
        """返回该影片的播放信息。

        整片优先（Emby/Jellyfin 流），无媒体流则回退 preview_video_url 预告（HLS）。
        同时返回外置播放器清单与默认播放器，供前端「用电脑播放器」。
        """
        db = get_db()
        cfg = get_cfg()
        row = db.get_movie(movie_id)
        if not row:
            return jsonify(ok=False, error="影片不存在"), 404
        movie = dict(row)          # sqlite3.Row 无 .get()，转 dict

        number = (movie.get("number") or "").strip()
        source = {"type": "none", "label": "无可用播放源", "url": "", "urls": [],
                  "is_hls": False, "expires": None, "expired": False, "server": None}

        # —— 1) 整片优先：media_servers + library_items 拼 Emby/Jellyfin 流 ——
        if number:
            for hit in db.library_stream_lookup(number):   # 取第一个命中；可扩展排序
                api_key = hit["api_key"]
                base = hit["server_url"].rstrip("/")
                direct = _emby_stream_url(base, hit["item_id"], api_key, hls=False)
                hls_url = _emby_stream_url(base, hit["item_id"], api_key, hls=True)
                source = {
                    "type": hit["server_type"],            # 'emby' | 'jellyfin'
                    "label": f"{hit['server_name']} · 整片",
                    "url": direct,
                    "urls": [
                        {"kind": "direct", "url": direct, "label": "直接播放（推荐）"},
                        {"kind": "hls", "url": hls_url, "label": "转码 HLS"},
                    ],
                    "is_hls": False,
                    "expires": None,
                    "expired": False,
                    "server": {"name": hit["server_name"], "type": hit["server_type"], "url": base},
                }
                break

        # —— 2) 预告兜底：preview_video_url（HLS .m3u8，带签名时效） ——
        if source["type"] == "none" and movie.get("preview_video_url"):
            expires = _parse_t_param(movie["preview_video_url"])
            source = {
                "type": "preview",
                "label": "JAVDB 预告",
                "url": movie["preview_video_url"],
                "urls": [{"kind": "hls", "url": movie["preview_video_url"], "label": "预告 HLS"}],
                "is_hls": True,
                "expires": expires,
                "expired": bool(expires and expires < time.time()),
                "server": None,
            }

        return jsonify(
            ok=source["type"] != "none",
            movie_id=movie_id,
            number=number,
            title=movie.get("title") or movie.get("origin_title") or "",
            source=source,
            players=EXTERNAL_PLAYERS,
            default_player=cfg.get("default_player") or (EXTERNAL_PLAYERS[0]["key"] if EXTERNAL_PLAYERS else ""),
        )

    @app.route("/api/want/subscriptions/by-target")
    def api_want_subscription_by_target():
        db = get_db()
        target_type = str(request.args.get("type") or "").strip()
        target_id = str(request.args.get("id") or "").strip()
        if target_type not in subscriptions.TARGET_TYPES or not target_id:
            return jsonify(ok=False, error="参数错误"), 400
        key = subscriptions.canonical_target_key(target_type, target_id)
        sub = db.subscription_by_target(target_type, key)
        return jsonify(ok=True, subscription=sub)

    @app.route("/api/want/subscriptions/<int:sid>")
    def api_want_subscription_get(sid):
        item = get_db().get_subscription(sid)
        if not item:
            return jsonify(ok=False, error="订阅不存在"), 404
        return jsonify(ok=True, subscription=item)

    @app.route("/api/want/subscriptions/<int:sid>", methods=["PATCH"])
    def api_want_subscription_update(sid):
        db = get_db()
        current = db.get_subscription(sid)
        if not current:
            return jsonify(ok=False, error="订阅不存在"), 404
        data = dict(current)
        data.update(request.get_json(silent=True) or {})
        try:
            clean, qualities = subscriptions.validate_subscription_payload(data, creating=False)
            db.update_subscription(sid, clean, qualities)
            return jsonify(ok=True, subscription=db.get_subscription(sid))
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 422

    @app.route("/api/want/subscriptions/<int:sid>", methods=["DELETE"])
    def api_want_subscription_delete(sid):
        if not get_db().delete_subscription(sid):
            return jsonify(ok=False, error="订阅不存在"), 404
        return jsonify(ok=True)

    @app.route("/api/want/subscriptions/<int:sid>/<action>", methods=["POST"])
    def api_want_subscription_status(sid, action):
        status = {"pause": "paused", "activate": "active"}.get(action)
        if not status:
            return jsonify(ok=False, error="非法操作"), 400
        if not get_db().set_subscription_status(sid, status):
            return jsonify(ok=False, error="订阅不存在"), 404
        return jsonify(ok=True)

    @app.route("/api/want/movie-sub-state", methods=["POST"])
    def api_want_movie_sub_state():
        db = get_db()
        data = request.get_json(silent=True) or {}
        ids = data.get("ids") or []
        return jsonify(ok=True, subscribed=db.subscribed_movie_ids(ids))

    @app.route("/api/want/subscribe-state", methods=["POST"])
    def api_want_subscribe_state():
        """批量查询某类型（actor/list/movie）已订阅目标 id。"""
        db = get_db()
        data = request.get_json(silent=True) or {}
        target_type = str(data.get("type") or "").strip()
        ids = data.get("ids") or []
        if target_type not in subscriptions.TARGET_TYPES or not ids:
            return jsonify(ok=True, subscribed=[])
        ph = ",".join("?" * len(ids))
        rows = db.conn.execute(
            f"SELECT DISTINCT target_id FROM subscriptions WHERE target_type=? AND target_id IN ({ph})",
            [target_type, *ids],
        ).fetchall()
        return jsonify(ok=True, subscribed=[r["target_id"] for r in rows])

    @app.route("/api/want/subscriptions/unsubscribe", methods=["POST"])
    def api_want_subscription_unsubscribe():
        db = get_db()
        data = request.get_json(silent=True) or {}
        target_type = str(data.get("target_type") or "").strip()
        target_id = str(data.get("target_id") or "").strip()
        if target_type not in subscriptions.TARGET_TYPES or not target_id:
            return jsonify(ok=False, error="参数错误"), 400
        ok = db.delete_subscription_by_target(target_type, target_id)
        if not ok:
            return jsonify(ok=False, error="未找到订阅"), 404
        return jsonify(ok=True)

    @app.route("/api/want/operations/targets")
    def api_want_operations_targets():
        db = get_db()
        rows = db.conn.execute(
            "SELECT id, target_type, target_name, pre_download FROM subscriptions "
            "WHERE status='active' AND enabled=1 ORDER BY id"
        ).fetchall()
        targets = [dict(r) for r in rows]
        for t in targets:
            t["pre_download"] = bool(t.get("pre_download"))
        return jsonify(ok=True, targets=targets)

    @app.route("/api/want/subscriptions/<int:sid>/checks", methods=["POST"])
    def api_want_subscription_check(sid):
        db = get_db()
        if not db.get_subscription(sid):
            return jsonify(ok=False, error="订阅不存在"), 404
        try:
            result = _subscription_services(db).run_check(sid)
            return jsonify(ok=True, **result)
        except Exception as e:  # noqa: BLE001
            return jsonify(ok=False, error=str(e)), 400

    @app.route("/api/want/checks/<int:run_id>")
    def api_want_check_get(run_id):
        db = get_db()
        run = db.conn.execute(
            "SELECT * FROM subscription_check_runs WHERE id=?", (run_id,)
        ).fetchone()
        if not run:
            return jsonify(ok=False, error="检查记录不存在"), 404
        return jsonify(ok=True, run=dict(run), candidates=db.list_subscription_candidates(run_id))

    @app.route("/api/want/subscriptions/<int:sid>/movies/<movie_id>/subscribe", methods=["POST"])
    def api_want_movie_subscribe(sid, movie_id):
        db = get_db()
        if not db.get_subscription(sid):
            return jsonify(ok=False, error="订阅不存在"), 404
        service = subscriptions.SubscriptionPushService(
            db, lambda magnet, name, size, movie_id_, code:
                _do_push(db, magnet, name, size, movie_id_, code)
        )
        try:
            result = service.subscribe_movie(sid, movie_id)
            try:
                _subscription_services(db).refresh_status(sid)
            except Exception:  # noqa: BLE001
                pass
            return jsonify(**result)
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 409

    @app.route("/api/want/subscriptions/<int:sid>/auto-push", methods=["POST"])
    def api_want_subscription_auto_push(sid):
        db = get_db()
        if not db.get_subscription(sid):
            return jsonify(ok=False, error="订阅不存在"), 404
        service = subscriptions.SubscriptionPushService(
            db, lambda magnet, name, size, movie_id, code:
                _do_push(db, magnet, name, size, movie_id, code)
        )
        try:
            force = bool((request.get_json(silent=True) or {}).get("force")) if request.is_json else False
            result = service.auto_push(sid, force=force)
            try:
                _subscription_services(db).refresh_status(sid)
            except Exception:  # noqa: BLE001
                pass
            return jsonify(**result)
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 409

    @app.route("/api/want/subscriptions/<int:sid>/candidates/<int:cid>/push", methods=["POST"])
    def api_want_candidate_push(sid, cid):
        db = get_db()
        data = request.get_json(silent=True) or {}
        key = str(data.get("idempotency_key") or f"manual:{sid}:{cid}")
        service = subscriptions.SubscriptionPushService(
            db, lambda magnet, name, size, movie_id, code:
                _do_push(db, magnet, name, size, movie_id, code)
        )
        try:
            result = service.push_candidate(sid, cid, key)
            if result.get("ok"):
                try:
                    _subscription_services(db).refresh_status(sid)
                except Exception:  # noqa: BLE001
                    pass
            return jsonify(**result)
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 409

    @app.route("/api/want/subscriptions/<int:sid>/movies")
    def api_want_subscription_movies(sid):
        db = get_db()
        sub = db.get_subscription(sid)
        if not sub:
            return jsonify(ok=False, error="订阅不存在"), 404
        if sub["target_type"] not in ("actor", "list"):
            return jsonify(ok=False, error="非演员/清单订阅"), 400
        try:
            service = _subscription_services(db)
            movies = (service.actor_movie_statuses(sid) if sub["target_type"] == "actor"
                      else service.list_movie_statuses(sid))
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400
        lib_codes = _lib_codes(db)
        for mv in movies:
            mv["in_library"] = 1 if (mv.get("number") or "") in lib_codes else 0
        counts = {"active": 0, "completed": 0, "skipped": 0, "all": len(movies)}
        for m in movies:
            counts[m["sub_status"]] += 1
        filter_ = request.args.get("filter", "active")
        if filter_ not in ("active", "completed", "skipped", "all"):
            filter_ = "active"
        filtered = [m for m in movies if filter_ == "all" or m["sub_status"] == filter_]
        checked_at = (sub.get("last_checked_at") or sub.get("updated_at") or "")[:16].replace("T", " ")
        return jsonify(ok=True, movies=filtered, counts=counts, filter=filter_,
                       name=sub["target_name"], checked_at=checked_at)

    @app.route("/api/want/operations/push", methods=["POST"])
    def api_want_operations_push():
        return jsonify(**_run_subscription_full_push(get_db(), get_cfg()))

    @app.route("/api/want/subscriptions/<int:sid>/movies/<movie_id>/skip", methods=["POST"])
    def api_want_movie_skip(sid, movie_id):
        db = get_db()
        if not db.get_subscription(sid):
            return jsonify(ok=False, error="订阅不存在"), 404
        db.add_skip(sid, movie_id)
        try:
            _subscription_services(db).refresh_status(sid)
        except Exception:  # noqa: BLE001
            pass
        return jsonify(ok=True)

    @app.route("/api/want/subscriptions/<int:sid>/movies/<movie_id>/unskip", methods=["POST"])
    def api_want_movie_unskip(sid, movie_id):
        db = get_db()
        if not db.get_subscription(sid):
            return jsonify(ok=False, error="订阅不存在"), 404
        db.remove_skip(sid, movie_id)
        try:
            _subscription_services(db).refresh_status(sid)
        except Exception:  # noqa: BLE001
            pass
        return jsonify(ok=True)

    @app.route("/api/want/subscriptions/<int:sid>/pushes")
    def api_want_pushes(sid):
        if not get_db().get_subscription(sid):
            return jsonify(ok=False, error="订阅不存在"), 404
        return jsonify(ok=True, items=get_db().list_subscription_pushes(sid))

    @app.route("/api/want/blacklist")
    def api_want_blacklist():
        db = get_db()
        target_type = request.args.get("target_type") or None
        if target_type and target_type not in subscriptions.BLACKLIST_TYPES:
            return jsonify(ok=False, error="非法黑名单类型"), 400
        return jsonify(ok=True, items=db.list_blacklist(target_type),
                       total=db.subscription_counts()["blacklist"])

    @app.route("/api/want/blacklist", methods=["POST"])
    def api_want_blacklist_create():
        db = get_db()
        data = request.get_json(silent=True) or {}
        target_type = str(data.get("target_type") or "").lower()
        target_id = str(data.get("target_id") or "").strip() or None
        target_name = str(data.get("target_name") or "").strip()
        if target_type not in subscriptions.BLACKLIST_TYPES or not target_id or not target_name:
            return jsonify(ok=False, error="黑名单目标不完整"), 422
        item = {"target_type": target_type, "target_id": target_id,
                "target_key": subscriptions.canonical_target_key(target_type, target_id),
                "target_name": target_name, "reason": str(data.get("reason") or "").strip()}
        try:
            bid = db.add_blacklist(item)
            return jsonify(ok=True, id=bid), 201
        except sqlite3.IntegrityError:
            return jsonify(ok=False, error="该目标已在黑名单"), 409

    @app.route("/api/want/blacklist/<int:bid>", methods=["DELETE"])
    def api_want_blacklist_delete(bid):
        if not get_db().delete_blacklist(bid):
            return jsonify(ok=False, error="黑名单记录不存在"), 404
        return jsonify(ok=True)

    # ---------- API ----------
    @app.route("/api/settings", methods=["POST"])
    def api_settings():
        cfg = get_cfg()
        data = request.get_json(force=True)
        for k in cfgmod.editable_keys():
            if k in data:
                cfg[k] = data[k]
        try:
            cfg["min_interval"] = float(cfg.get("min_interval", 0.5))
        except (TypeError, ValueError):
            cfg["min_interval"] = 0.5
        cfgmod.save(cfg, CONFIG_PATH)
        return jsonify(ok=True)

    @app.route("/api/servers", methods=["POST"])
    def api_add_server():
        db = get_db()
        data = request.get_json(force=True)
        name = (data.get("name") or "").strip()
        url = (data.get("url") or "").strip()
        api_key = (data.get("api_key") or "").strip()
        type_ = data.get("type", "emby")
        if not name or not url or not api_key:
            return jsonify(ok=False, error="名称 / 地址 / API Key 不能为空"), 400
        sid = db.add_server(name, url, api_key, type_)
        return jsonify(ok=True, id=sid)

    @app.route("/api/servers/<int:sid>", methods=["DELETE"])
    def api_del_server(sid):
        get_db().remove_server(sid)
        return jsonify(ok=True)

    @app.route("/api/server/ping/<int:sid>")
    def api_ping_server(sid):
        db = get_db()
        cfg = get_cfg()
        row = db.conn.execute("SELECT * FROM media_servers WHERE id=?", (sid,)).fetchone()
        if not row:
            return jsonify(ok=False, msg="服务器不存在"), 404
        c = MediaServerClient(row["url"], row["api_key"], row["name"], row["type"],
                              proxy=cfg.get("proxy") or None)
        ok, msg = c.ping()
        return jsonify(ok=ok, msg=msg)

    @app.route("/api/sync", methods=["POST"])
    def api_sync():
        db = get_db()
        servers = db.get_servers()
        if not servers:
            return jsonify(ok=False, error="没有配置媒体服务器"), 400
        total, results = sync.sync_library(db, servers)
        return jsonify(ok=True, total=total, results=results)

    @app.route("/api/sync/schedule", methods=["POST"])
    def api_sync_schedule():
        cfg = get_cfg()
        data = request.get_json(silent=True) or {}
        cron = (data.get("cron") or "").strip()
        cfg["sync_cron"] = cron
        cfgmod.save(cfg, CONFIG_PATH)
        return jsonify(ok=True)

    @app.route("/api/sync/stats")
    def api_sync_stats():
        db = get_db()
        rows = db.conn.execute(
            """
            SELECT s.name, s.type, COUNT(DISTINCT li.code) AS cnt
            FROM media_servers s LEFT JOIN library_items li ON li.server_id = s.id
            GROUP BY s.id ORDER BY s.id
            """
        ).fetchall()
        total = db.conn.execute("SELECT COUNT(DISTINCT code) FROM library_items").fetchone()[0]
        return jsonify(ok=True, servers=[dict(r) for r in rows], total=total)

    @app.route("/api/ingest", methods=["POST"])
    def api_ingest():
        db = get_db()
        cfg = get_cfg()
        data = request.get_json(force=True)
        target = (data.get("target") or "").strip()
        if not target:
            return jsonify(ok=False, error="缺少目标"), 400
        try:
            mid = _resolve_id(build_client(cfg), target)
            movie = scrape.ingest_movie_id(build_client(cfg), db, mid)
            if data.get("magnets"):
                scrape.ingest_magnets(build_javbus(cfg), db, movie["number"], mid)
            return jsonify(ok=True, id=mid, number=movie["number"])
        except Exception as e:  # noqa: BLE001
            return jsonify(ok=False, error=str(e)), 400

    @app.route("/api/magnets", methods=["POST"])
    def api_magnets():
        db = get_db()
        cfg = get_cfg()
        data = request.get_json(force=True)
        target = (data.get("target") or "").strip()
        if not target:
            return jsonify(ok=False, error="缺少目标"), 400
        try:
            code, movie_id = _resolve_code_movie(build_client(cfg), db, target)
            n = scrape.ingest_magnets(build_javbus(cfg), db, code, movie_id)
            return jsonify(ok=True, count=n)
        except Exception as e:  # noqa: BLE001
            return jsonify(ok=False, error=str(e)), 400

    @app.route("/api/magnet/push", methods=["POST"])
    def api_magnet_push():
        db = get_db()
        data = request.get_json(silent=True) or {}
        code = data.get("code") or ""
        dl = (data.get("downloader") or "").strip()
        if db.has_push_code(code, dl):
            return jsonify(ok=False, error=f"已推送到「{dl or '下载器'}」，如需再次推送请删除记录后重试"), 400
        ok, qid, msg = _do_push(
            db, data.get("magnet") or "", name=data.get("name") or "",
            size=data.get("size") or "", movie_id=data.get("movie_id") or None,
            code=code, downloader=(data.get("downloader") or "").strip() or None,
        )
        return jsonify(ok=ok, qid=qid, msg=msg) if ok else jsonify(ok=False, qid=qid, error=msg), 400

    @app.route("/api/downloaders/options")
    def api_downloaders_options():
        db = get_db()
        return jsonify(ok=True, options=_available_downloaders(db))

    @app.route("/api/reviews", methods=["POST"])
    def api_reviews():
        db = get_db()
        cfg = get_cfg()
        data = request.get_json(force=True)
        target = (data.get("target") or "").strip()
        if not target:
            return jsonify(ok=False, error="缺少目标"), 400
        try:
            mid = _resolve_id(build_client(cfg), target)
            n = scrape.ingest_reviews(build_client(cfg), db, mid, max_pages=5)
            return jsonify(ok=True, count=n)
        except Exception as e:  # noqa: BLE001
            return jsonify(ok=False, error=str(e)), 400

    @app.route("/api/javdb-login", methods=["POST"])
    def api_javdb_login():
        cfg = get_cfg()
        data = request.get_json(silent=True) or {}
        username = (data.get("username") or cfg.get("javdb_username") or "").strip()
        password = data.get("password") or cfg.get("javdb_password") or ""
        if not username or not password:
            return jsonify(ok=False, error="请先填写 JAVDB 用户名和密码"), 400
        client = JavdbClient(
            username=username, password=password,
            min_interval=float(cfg.get("min_interval", 0.5)),
            proxy=cfg.get("proxy") or None,
        )
        try:
            token = client.login(username, password)
            cfg["javdb_token"] = token
            cfg["javdb_username"] = username
            cfg["javdb_password"] = password
            cfgmod.save(cfg, CONFIG_PATH)
            return jsonify(ok=True, token=token)
        except Exception as e:  # noqa: BLE001
            return jsonify(ok=False, error=str(e)), 400

    @app.route("/api/test-nodes")
    def api_test_nodes():
        cfg = get_cfg()
        results = []
        for name, base in cfgmod.API_NODES:
            try:
                c = JavdbClient(api_base=base, proxy=cfg.get("proxy") or None,
                                min_interval=0, timeout=8)
                t0 = time.time()
                c.search("SSIS-001", limit=1)
                results.append({"name": name, "ok": True, "ms": round((time.time() - t0) * 1000)})
            except Exception as e:  # noqa: BLE001
                results.append({"name": name, "ok": False, "error": str(e)[:80]})
        return jsonify(results=results)

    # ---------- 115 网盘 / 下载器 ----------
    @app.route("/api/pan115/test", methods=["POST"])
    def api_pan115_test():
        db = get_db()
        data = request.get_json(silent=True) or {}
        cookie = (data.get("cookie") or "").strip() or db.get_pan115_config().get("cookie") or ""
        if not cookie:
            return jsonify(ok=False, msg="未配置 115 Cookie"), 400
        ok, msg = pan115.test_connection(cookie)
        return jsonify(ok=ok, msg=msg)

    @app.route("/api/pan115/dirs", methods=["POST"])
    def api_pan115_dirs():
        db = get_db()
        data = request.get_json(silent=True) or {}
        cid = (data.get("cid") or "").strip()
        cfg115 = db.get_pan115_config()
        cookie = (data.get("cookie") or "").strip() or cfg115.get("cookie") or ""
        if not cookie:
            return jsonify(ok=False, error="未配置 115 Cookie"), 400
        try:
            dirs = pan115.list_dirs(cookie, cid=cid or "0")
            return jsonify(ok=True, dirs=dirs)
        except Exception as e:  # noqa: BLE001
            return jsonify(ok=False, error=str(e)), 400

    @app.route("/api/pan115/config", methods=["POST"])
    def api_pan115_config():
        db = get_db()
        data = request.get_json(silent=True) or {}
        cur = db.get_pan115_config()
        cur.update({
            "cookie": data.get("cookie", cur.get("cookie", "")).strip(),
            "timeout": int(data.get("timeout", cur.get("timeout", 30)) or 30),
            "quota": int(data.get("quota", cur.get("quota", 0)) or 0),
            "target_cid": data.get("target_cid", cur.get("target_cid", "")).strip(),
            "target_name": data.get("target_name", cur.get("target_name", "")).strip(),
            "enabled": 1 if data.get("enabled", cur.get("enabled", 0)) else 0,
        })
        db.save_pan115_config(cur)
        return jsonify(ok=True)

    @app.route("/api/clouddrive2/test", methods=["POST"])
    def api_clouddrive2_test():
        db = get_db()
        data = request.get_json(silent=True) or {}
        cur = db.get_clouddrive2_config()
        host = (data.get("host") or "").strip() or cur.get("host") or "127.0.0.1"
        rpc_port = int(data.get("rpc_port") or cur.get("rpc_port") or 19798)
        timeout = int(data.get("timeout") or cur.get("timeout") or 30)
        token = (data.get("api_token") or "").strip() or cur.get("api_token") or ""
        user = (data.get("user_name") or "").strip() or cur.get("user_name") or ""
        pwd = (data.get("password") or "").strip() or cur.get("password") or ""
        obtained = None
        if not token and user and pwd:
            try:
                obtained = clouddrive2.get_token(host, rpc_port, user, pwd, timeout=timeout)
                token = obtained
            except Exception as e:  # noqa: BLE001
                return jsonify(ok=False, msg=f"登录失败：{e}")
        ok, msg = clouddrive2.test_connection(host, rpc_port, api_token=token,
                                              user_name=user, password=pwd, timeout=timeout)
        resp = {"ok": ok, "msg": msg}
        if obtained:
            resp["token"] = obtained
        return jsonify(**resp)

    @app.route("/api/clouddrive2/config", methods=["POST"])
    def api_clouddrive2_config():
        db = get_db()
        data = request.get_json(silent=True) or {}
        cur = db.get_clouddrive2_config()
        cur.update({
            "host": data.get("host", cur.get("host", "127.0.0.1")).strip(),
            "rpc_port": int(data.get("rpc_port", cur.get("rpc_port", 19798)) or 19798),
            "timeout": int(data.get("timeout", cur.get("timeout", 30)) or 30),
            "enable_ed2k": 1 if data.get("enable_ed2k", cur.get("enable_ed2k", 0)) else 0,
            "api_token": data.get("api_token", cur.get("api_token", "")).strip(),
            "user_name": data.get("user_name", cur.get("user_name", "")).strip(),
            "password": data.get("password", cur.get("password", "")).strip(),
            "save_path": data.get("save_path", cur.get("save_path", "")).strip(),
            "enabled": 1 if data.get("enabled", cur.get("enabled", 0)) else 0,
        })
        db.save_clouddrive2_config(cur)
        return jsonify(ok=True)

    @app.route("/api/downloader/toggle", methods=["POST"])
    def api_downloader_toggle():
        db = get_db()
        data = request.get_json(silent=True) or {}
        did = int(data.get("id", 0))
        if "enabled" not in data:
            return jsonify(ok=False, error="缺少 enabled"), 400
        db.update_downloader(did, enabled=int(data.get("enabled")))
        db.renumber_downloaders()
        return jsonify(ok=True)

    @app.route("/api/downloader/order", methods=["POST"])
    def api_downloader_order():
        db = get_db()
        data = request.get_json(silent=True) or {}
        did = int(data.get("id", 0))
        direction = data.get("direction")
        dls = db.list_downloaders()
        idx = next((i for i, d in enumerate(dls) if d["id"] == did), None)
        if idx is None:
            return jsonify(ok=False, error="下载器不存在"), 400
        swap = None
        if direction == "up" and idx > 0 and dls[idx - 1]["enabled"] == dls[idx]["enabled"]:
            swap = idx - 1
        elif direction == "down" and idx < len(dls) - 1 and dls[idx + 1]["enabled"] == dls[idx]["enabled"]:
            swap = idx + 1
        if swap is not None:
            a, b = dls[idx]["sort_order"], dls[swap]["sort_order"]
            db.update_downloader(dls[idx]["id"], sort_order=b)
            db.update_downloader(dls[swap]["id"], sort_order=a)
            db.renumber_downloaders()
        return jsonify(ok=True)

    @app.route("/push")
    def push_page():
        db = get_db()
        items = db.list_push_all(limit=200)
        return render_template("push.html", items=items, active="push")

    @app.route("/records")
    def records_page():
        db = get_db()
        today = datetime.now().strftime("%Y-%m-%d")
        kw = request.args.get("kw", "").strip()
        status = request.args.get("status", "").strip()
        dl = request.args.get("downloader", "").strip()
        # 未指定日期时默认显示当天
        d1 = request.args.get("from", "").strip() or today
        d2 = request.args.get("to", "").strip() or today
        rtype = request.args.get("rtype", "").strip()
        source = request.args.get("source", "").strip()
        rows = db.list_push_records(status=status or None, keyword=kw or None,
                                    downloader=dl or None, date_from=d1 or None, date_to=d2 or None)
        records = []
        for r in rows:
            rec = dict(r)
            n = rec.get("name") or ""
            rec["hd"] = 1 if re.search(r"高清|1080|4k|2160|hd", n, re.I) else 0
            rec["uc"] = 1 if is_uncensored(n) else 0
            rec["cover"] = rec.get("cover_url") or rec.get("javbus_cover") or ""
            rec["time"] = (rec.get("pushed_at") or rec.get("created_at") or "")[:16].replace("T", " ")
            rec["source"] = "手动"  # 当前推送均为手动发起
            # 资源类型 / 来源 二次过滤
            if rtype == "高清" and not rec["hd"]:
                continue
            if rtype == "破解" and not rec["uc"]:
                continue
            if source and source != "手动":
                continue
            records.append(rec)
        dl_opts = sorted({r.get("downloader") for r in records if r.get("downloader")})
        return render_template("records.html", records=records, kw=kw, status=status, dl=dl,
                               d1=d1, d2=d2, rtype=rtype, source=source,
                               dl_opts=dl_opts, active="records")

    @app.route("/api/push/<int:pid>/repush", methods=["POST"])
    def api_push_repush(pid):
        db = get_db()
        row = db.get_push(pid)
        if not row:
            return jsonify(ok=False, error="记录不存在"), 404
        ok, qid, msg = _do_push(db, row["magnet"], name=row["name"] or "",
                                size=row["size"] or "", movie_id=row["movie_id"],
                                code=row["code"] or "")
        if ok:
            db.delete_push(pid)
            return jsonify(ok=True, qid=qid, msg=msg)
        return jsonify(ok=False, qid=qid, error=msg), 400

    @app.route("/api/push/<int:pid>/status", methods=["POST"])
    def api_push_status(pid):
        db = get_db()
        data = request.get_json(silent=True) or {}
        status = data.get("status")
        if status not in ("pending", "pushed", "failed"):
            return jsonify(ok=False, error="非法状态"), 400
        db.set_push_status(pid, status)
        return jsonify(ok=True)

    @app.route("/api/push/<int:pid>", methods=["DELETE"])
    def api_push_delete(pid):
        get_db().delete_push(pid)
        return jsonify(ok=True)

    start_sync_scheduler()
    start_push_verify_worker()
    start_subscription_scheduler()
    return app


if __name__ == "__main__":
    app = create_app()
    cfg = cfgmod.load(CONFIG_PATH)
    app.run(host=cfg.get("host", "0.0.0.0"), port=int(cfg.get("port", 9091)),
            debug=False, threaded=True)
