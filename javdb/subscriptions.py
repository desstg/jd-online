"""订阅解析、条件匹配、手动检查与推送编排。"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable

from . import scrape
from .db import Database
from .javbus import is_uncensored

# 115 风控限制：同时提交到云盘的磁链不超过 2 个，避免一次推太多触发风控。
PUSH_CONCURRENCY = threading.Semaphore(2)
PUSH_SPACE_SECONDS = 3.0

MATCHER_VERSION = "v1"
QUALITY_VALUES = {"hd", "uhd", "subtitle", "uncensored"}
TARGET_TYPES = {"movie", "online", "actor", "list"}
DOWNLOAD_MODES = {"strict", "upgrade"}
BLACKLIST_TYPES = {"movie", "actor", "list"}


def canonical_target_key(target_type: str, target_id: str | None,
                         target_url: str | None = None) -> str:
    value = (target_id or target_url or "").strip()
    return f"{target_type}:{value.lower()}"


def validate_subscription_payload(data: dict, *, creating: bool = True) -> tuple[dict, list[str]]:
    target_type = str(data.get("target_type") or "").strip().lower()
    if target_type not in TARGET_TYPES:
        raise ValueError("不支持的订阅类型")
    target_id = str(data.get("target_id") or "").strip() or None
    target_url = str(data.get("target_url") or "").strip() or None
    target_name = str(data.get("target_name") or "").strip()
    if not target_name:
        raise ValueError("订阅名称不能为空")
    if creating and not (target_id or target_url):
        raise ValueError("订阅目标不能为空")
    if target_type == "online" and target_url and not target_url.lower().startswith(("http://", "https://")):
        raise ValueError("在线订阅地址必须是 HTTP 或 HTTPS")

    mode = str(data.get("download_mode") or "strict").strip().lower()
    if mode not in DOWNLOAD_MODES:
        raise ValueError("非法下载模式")

    def optional_int(key: str) -> int | None:
        value = data.get(key)
        if value in (None, ""):
            return None
        try:
            value = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} 必须是整数") from exc
        if value < 0:
            raise ValueError(f"{key} 不能小于 0")
        return value

    min_size = optional_int("min_size_mb")
    max_size = optional_int("max_size_mb")
    max_files = optional_int("max_file_count")
    if min_size is not None and max_size is not None and min_size > max_size:
        raise ValueError("最小文件大小不能大于最大文件大小")
    if max_files == 0:
        max_files = None

    def optional_date(key: str) -> str | None:
        value = str(data.get(key) or "").strip()
        if not value:
            return None
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{key} 必须是有效日期") from exc
        return value

    date_from = optional_date("release_date_from")
    date_to = optional_date("release_date_to")
    if date_from and date_to and date_from > date_to:
        raise ValueError("上映开始日期不能晚于结束日期")

    expiry_days = optional_int("expiry_days")

    def _cat_list(key: str) -> list[str]:
        raw = data.get(key) or []
        if not isinstance(raw, list):
            raise ValueError("类别过滤格式错误")
        return [str(c).strip() for c in raw if str(c).strip()]

    categories = _cat_list("categories")
    exclude_categories = _cat_list("exclude_categories")

    raw_qualities = data.get("qualities") or []
    if not isinstance(raw_qualities, list):
        raise ValueError("质量条件格式错误")
    qualities = sorted({str(q).lower() for q in raw_qualities if str(q).lower() in QUALITY_VALUES})

    clean = {
        "target_type": target_type,
        "target_id": target_id,
        "target_url": target_url,
        "target_key": canonical_target_key(target_type, target_id, target_url),
        "target_name": target_name,
        "download_mode": mode,
        "pre_download": 1 if data.get("pre_download") else 0,
        "min_size_mb": min_size,
        "max_size_mb": max_size,
        "max_file_count": max_files,
        "release_date_from": date_from,
        "release_date_to": date_to,
        "expiry_days": expiry_days,
        "categories": categories,
        "exclude_categories": exclude_categories,
    }
    return clean, qualities


def parse_size_bytes(value: str | None) -> int | None:
    text = (value or "").strip().upper().replace(",", "")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(TB|GB|MB|KB|B)", text)
    if not match:
        return None
    number = float(match.group(1))
    scale = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}
    return int(number * scale[match.group(2)])


def detect_quality_tags(name: str, has_hd: int = 0, has_sub: int = 0) -> set[str]:
    lower = (name or "").lower()
    tags: set[str] = set()
    if has_hd or re.search(r"\b(?:720p|1080p|hd|fhd|blu-?ray|bd)\b|高清", lower, re.I):
        tags.add("hd")
    if re.search(r"\b(?:2160p|4k|uhd)\b|超清", lower, re.I):
        tags.update(("hd", "uhd"))
    if has_sub or re.search(r"字幕|中文|中字|sub(?:title)?", lower, re.I):
        tags.add("subtitle")
    if re.search(r"编辑|精剪|剪辑|edited|director'?s cut", lower, re.I):
        tags.add("edited")
    if is_uncensored(name or ""):
        tags.add("uncensored")
    return tags


def resource_score(tags: set[str], size_bytes: int | None) -> list[int]:
    # 固定优先级：高清等级 -> 破解 -> 文件大小（越大越优先）。
    resolution = 2 if "uhd" in tags else 1 if "hd" in tags else 0
    return [resolution, 1 if "uncensored" in tags else 0, int(size_bytes or 0)]


def _info_hash_of(magnet_uri: str | None) -> str:
    m = re.search(r"btih:([0-9a-fA-F]{40})", magnet_uri or "")
    return m.group(1).lower() if m else ""


def _task_outcome(task: dict) -> str:
    """根据 115 离线任务字段判断结果：success / failed / running。"""
    ds = (task.get("display_status") or "").lower()
    st = task.get("status")
    stt = (task.get("status_text") or "").lower()
    if ds in ("failed", "error", "fail") or st == -1 or "失败" in stt:
        return "failed"
    if ds in ("success", "complete", "completed", "downloaded", "done") or st in (2, 3) or "完成" in stt:
        return "success"
    return "running"


def find_115_task(client, info_hash: str, pages: int = 4) -> dict | None:
    """在 115 离线任务列表里按 info_hash(btih) 找到对应任务。"""
    info_hash = (info_hash or "").lower()
    if not info_hash:
        return None
    for page in range(1, pages + 1):
        try:
            r = client.clouddownload_task_list(page)
        except Exception:  # noqa: BLE001
            return None
        tasks = (r or {}).get("tasks") or []
        for t in tasks:
            if (str(t.get("info_hash") or "")).lower() == info_hash:
                return t
        if len(tasks) < 20:
            break
    return None


def delete_115_task(client, task: dict) -> None:
    """删除失败的 115 离线任务，释放配额。"""
    key = task.get("info_hash") or task.get("file_id") or task.get("pick_code")
    if not key:
        return
    try:
        client.clouddownload_task_del(key)
    except Exception:  # noqa: BLE001
        pass


def verify_and_retry(db: Database, push_service: "SubscriptionPushService", cookie: str) -> dict:
    """轮询 115：确认自动推送任务的下载结果，失败则换下一颗磁链重试。"""
    from p115client import P115Client
    client = P115Client(cookie)
    attempts = db.list_verify_pending()
    summary = {"succeeded": 0, "failed": 0, "running": 0, "retried": 0}
    for a in attempts:
        task = find_115_task(client, a["info_hash"])
        if not task:
            summary["running"] += 1
            continue
        outcome = _task_outcome(task)
        if outcome == "running":
            summary["running"] += 1
            continue
        if outcome == "success":
            db.mark_subscription_push(a["id"], "succeeded")
            summary["succeeded"] += 1
        else:  # failed：删除任务，标记失败，重试下一颗
            delete_115_task(client, task)
            db.mark_subscription_push(a["id"], "failed", error="网盘下载失败")
            summary["failed"] += 1
            try:
                push_service.auto_push(a["subscription_id"])
                summary["retried"] += 1
            except Exception:  # noqa: BLE001
                pass
        # 重算订阅状态：还有“订阅中”影片就不算完成
        try:
            SubscriptionCheckService(db).refresh_status(a["subscription_id"])
        except Exception:  # noqa: BLE001
            pass
    return summary


def resource_fingerprint(magnet: dict) -> str:
    source = str(magnet.get("btih") or magnet.get("magnet") or magnet.get("name") or "")
    return hashlib.sha256(source.encode("utf-8", "replace")).hexdigest()


@dataclass
class MatchResult:
    movie_ok: bool       # 影片命中订阅（影片订阅=仅黑名单；演员/清单=日期+包含/排除类别+黑名单）
    push_ok: bool        # 磁链满足推送条件（质量+大小+文件数）
    reasons: list[str]
    tags: set[str]
    size_bytes: int | None
    score: list[int]


class BlacklistPolicy:
    def __init__(self, db: Database):
        self.db = db

    def reasons(self, movie_id: str | None, actor_ids: set[str],
                list_id: str | None = None) -> list[str]:
        reasons: list[str] = []
        if movie_id and self.db.blacklist_match("movie", canonical_target_key("movie", movie_id)):
            reasons.append("blacklisted_movie")
        for actor_id in actor_ids:
            if self.db.blacklist_match("actor", canonical_target_key("actor", actor_id)):
                reasons.append("blacklisted_actor")
                break
        if list_id and self.db.blacklist_match("list", canonical_target_key("list", list_id)):
            reasons.append("blacklisted_list")
        return reasons


class CandidateMatcher:
    def __init__(self, db: Database):
        self.blacklist = BlacklistPolicy(db)

    def movie_ok(self, subscription: dict, movie: dict,
                 *, actor_ids: set[str], list_id: str | None = None) -> tuple[bool, list[str]]:
        """影片是否命中订阅。

        影片订阅：仅黑名单（单部影片无需日期/类别筛选）。
        演员/清单订阅：黑名单 + 上映日期区间 + 包含/排除类别。
        """
        reasons = self.blacklist.reasons(movie.get("id"), actor_ids, list_id)
        if subscription["target_type"] in ("actor", "list"):
            release_date = str(movie.get("release_date") or "")[:10]
            if subscription.get("release_date_from"):
                if not release_date:
                    reasons.append("release_date_unknown")
                elif release_date < subscription["release_date_from"]:
                    reasons.append("release_date_before_start")
            if subscription.get("release_date_to"):
                if not release_date:
                    reasons.append("release_date_unknown")
                elif release_date > subscription["release_date_to"]:
                    reasons.append("release_date_after_end")
            try:
                movie_tags = set(json.loads(movie.get("tags") or "[]"))
            except (ValueError, TypeError):
                movie_tags = set()
            include_cats = set(subscription.get("categories") or [])
            exclude_cats = set(subscription.get("exclude_categories") or [])
            if include_cats and not include_cats.intersection(movie_tags):
                reasons.append("category_not_matched")
            if exclude_cats and exclude_cats.intersection(movie_tags):
                reasons.append("category_excluded")
        reasons = list(dict.fromkeys(reasons))
        return (not reasons, reasons)

    def prompt_ok(self, subscription: dict, tags: set[str], size_bytes: int | None,
                  file_count: int | None) -> tuple[bool, list[str]]:
        """磁链是否满足推送条件：质量 + 大小 + 文件数。"""
        reasons: list[str] = []
        qualities = set(subscription.get("qualities") or [])
        if qualities and not qualities.intersection(tags):
            reasons.append("quality_not_matched")
        if subscription.get("min_size_mb") is not None:
            if size_bytes is None:
                reasons.append("size_unknown")
            elif size_bytes < subscription["min_size_mb"] * 1024 ** 2:
                reasons.append("below_min_size")
        if subscription.get("max_size_mb") is not None:
            if size_bytes is None:
                reasons.append("size_unknown")
            elif size_bytes > subscription["max_size_mb"] * 1024 ** 2:
                reasons.append("above_max_size")
        if subscription.get("max_file_count") is not None:
            if file_count is None:
                reasons.append("file_count_unknown")
            elif int(file_count) > subscription["max_file_count"]:
                reasons.append("too_many_files")
        return (not reasons, reasons)

    def evaluate(self, subscription: dict, movie: dict, magnet: dict,
                 *, actor_ids: set[str], list_id: str | None = None) -> MatchResult:
        tags = detect_quality_tags(magnet.get("name") or "", magnet.get("has_hd") or 0,
                                   magnet.get("has_sub") or 0)
        size_bytes = parse_size_bytes(magnet.get("size"))
        movie_ok, movie_reasons = self.movie_ok(subscription, movie, actor_ids=actor_ids, list_id=list_id)
        push_ok, push_reasons = self.prompt_ok(subscription, tags, size_bytes, magnet.get("file_count"))
        return MatchResult(movie_ok, push_ok, movie_reasons + push_reasons, tags, size_bytes,
                           resource_score(tags, size_bytes))


class TargetResolver:
    def __init__(self, db: Database, client=None, javbus=None):
        self.db = db
        self.client = client
        self.javbus = javbus

    def resolve_movies(self, subscription: dict) -> list[dict]:
        target_type = subscription["target_type"]
        target_id = subscription.get("target_id")
        if target_type in ("movie", "online"):
            row = self.db.get_movie(target_id) if target_id else None
            if not row and target_id and self.client:
                scrape.ingest_movie_id(self.client, self.db, target_id)
                row = self.db.get_movie(target_id)
            return [dict(row)] if row else []
        if target_type == "actor":
            rows = self.db.conn.execute(
                """
                SELECT m.* FROM movies m JOIN movie_actors ma ON ma.movie_id=m.id
                WHERE ma.actor_id=? ORDER BY m.release_date DESC
                """,
                (target_id,),
            ).fetchall()
            if not rows and self.client:
                res = self.client.search_by_type(subscription["target_name"], "actor", limit=60)
                for item in (res.get("data") or {}).get("movies") or []:
                    if item.get("id"):
                        try:
                            scrape.ingest_movie_id(self.client, self.db, item["id"])
                        except Exception:  # noqa: BLE001
                            continue
                rows = self.db.conn.execute(
                    "SELECT m.* FROM movies m JOIN movie_actors ma ON ma.movie_id=m.id "
                    "WHERE ma.actor_id=? ORDER BY m.release_date DESC", (target_id,),
                ).fetchall()
            return [dict(r) for r in rows]
        if target_type == "list" and self.client:
            # 按清单名拉取全部影片（多页），并缓存到 list_movies，供弹窗秒开
            out = []
            page = 1
            while page <= 8:
                res = self.client.list_movies(subscription["target_name"], page=page, limit=100)
                movies = (res.get("data") or {}).get("movies") or []
                if not movies:
                    break
                for item in movies:
                    mid = item.get("id")
                    if not mid:
                        continue
                    if not self.db.has_movie(mid):
                        self.db.upsert_movie(scrape.normalize_movie(item))
                    row = self.db.get_movie(mid)
                    if row:
                        out.append(dict(row))
                if len(movies) < 100:
                    break
                page += 1
            self.db.replace_list_movies(subscription["target_id"], [m["id"] for m in out])
            return out
        return []

    def ensure_magnets(self, movie: dict) -> list[dict]:
        rows = self.db.movie_magnets(movie.get("id"), movie.get("number"))
        if not rows and self.javbus and movie.get("number"):
            scrape.ingest_magnets(self.javbus, self.db, movie["number"], movie.get("id"))
            rows = self.db.movie_magnets(movie.get("id"), movie.get("number"))
        return [dict(r) for r in rows]


class SubscriptionCheckService:
    def __init__(self, db: Database, client=None, javbus=None):
        self.db = db
        self.resolver = TargetResolver(db, client, javbus)
        self.matcher = CandidateMatcher(db)

    def run_check(self, subscription_id: int, trigger_type: str = "manual") -> dict:
        subscription = self.db.get_subscription(subscription_id)
        if not subscription:
            raise ValueError("订阅不存在")
        run_id = self.db.create_subscription_run(subscription_id, trigger_type, MATCHER_VERSION)
        matched = rejected = 0
        candidate_rows: list[tuple[int, MatchResult]] = []
        matched_movies: set[str] = set()
        rejected_movies: set[str] = set()
        try:
            movies = self.resolver.resolve_movies(subscription)
            for movie in movies:
                actor_ids = self.db.movie_actor_ids(movie["id"])
                list_id = subscription.get("target_id") if subscription["target_type"] == "list" else None
                movie_ok, _ = self.matcher.movie_ok(subscription, movie, actor_ids=actor_ids, list_id=list_id)
                (matched_movies if movie_ok else rejected_movies).add(movie["id"])
                for magnet in self.resolver.ensure_magnets(movie):
                    result = self.matcher.evaluate(subscription, movie, magnet,
                                                   actor_ids=actor_ids, list_id=list_id)
                    data = {
                        "check_run_id": run_id,
                        "subscription_id": subscription_id,
                        "movie_id": movie.get("id"),
                        "magnet_id": magnet.get("btih"),
                        "magnet_hash": magnet.get("btih"),
                        "magnet_name": magnet.get("name") or movie.get("number") or "磁链资源",
                        "magnet_uri": magnet.get("magnet"),
                        "size_text": magnet.get("size"),
                        "size_bytes": result.size_bytes,
                        "file_count": magnet.get("file_count"),
                        "release_date": movie.get("release_date"),
                        "quality_tags": sorted(result.tags),
                        "resource_fingerprint": resource_fingerprint(magnet),
                        "resource_score": result.score,
                        "matched": result.movie_ok,       # 影片命中订阅（黑名单/日期/类别）
                        "push_ok": result.push_ok,        # 磁链满足推送条件（质量/大小/文件数）
                        "predownload": 0,
                        "rejection_reasons": result.reasons,
                    }
                    cid = self.db.add_subscription_candidate(data)
                    candidate_rows.append((cid, result))

            matched = len(matched_movies)
            rejected = len(rejected_movies)

            # 预下载：有命中影片、但没有任何磁链满足推送条件时，选最优磁链作预下载候选
            if subscription.get("pre_download") and matched and not any(res.push_ok for _, res in candidate_rows):
                eligible = [(cid, res) for cid, res in candidate_rows if res.movie_ok]
                if eligible:
                    best_id, _ = max(eligible, key=lambda pair: tuple(pair[1].score))
                    self.db.conn.execute(
                        "UPDATE subscription_candidates SET predownload=1 WHERE id=?", (best_id,)
                    )
                    self.db.commit()

            self.db.set_subscription_matched_count(subscription_id, matched)
            self.db.finish_subscription_run(run_id, "completed", matched, rejected)
            self.db.set_subscription_error(subscription_id, None)
            return {"run_id": run_id, "candidates": self.db.list_subscription_candidates(run_id),
                    "matched_count": matched, "rejected_count": rejected}
        except Exception as exc:
            self.db.finish_subscription_run(run_id, "failed", matched, rejected, str(exc))
            self.db.set_subscription_error(subscription_id, str(exc))
            raise


    def actor_movie_statuses(self, subscription_id: int) -> list[dict]:
        """返回演员订阅的影片及订阅处理状态（供 8.png 弹窗）。

        - active(订阅中)：满足订阅条件（日期/类别/黑名单），尚未推送
        - completed(已完成)：已成功推送网盘
        - skipped(跳过)：用户手动点“跳过”，或未命中订阅条件（不再显示在订阅中）
        磁链质量/大小/文件数只在推送时匹配。
        """
        subscription = self.db.get_subscription(subscription_id)
        if not subscription or subscription["target_type"] != "actor":
            raise ValueError("非演员订阅")
        actor_id = subscription["target_id"]
        rows = self.db.conn.execute(
            """
            SELECT m.id, m.number, m.title, m.origin_title, m.cover_url,
                   m.javbus_cover, m.release_date, m.tags
            FROM movies m JOIN movie_actors ma ON ma.movie_id=m.id
            WHERE ma.actor_id=? ORDER BY m.release_date DESC, m.id
            """,
            (actor_id,),
        ).fetchall()
        pushed_ids = {r["movie_id"] for r in self.db.conn.execute(
            "SELECT DISTINCT c.movie_id FROM subscription_push_attempts p "
            "JOIN subscription_candidates c ON c.id=p.candidate_id "
            "WHERE p.subscription_id=? AND p.status='succeeded'", (subscription_id,)
        )}
        skipped_ids = self.db.skipped_set(subscription_id)

        out = []
        for row in rows:
            movie = dict(row)
            movie_ok, _ = self.matcher.movie_ok(subscription, movie, actor_ids={actor_id})
            if not movie_ok:
                continue  # 未命中订阅条件，不展示
            status = "active"
            if movie["id"] in pushed_ids:
                status = "completed"
            elif movie["id"] in skipped_ids:
                status = "skipped"
            out.append({
                "id": movie["id"], "number": movie["number"],
                "title": movie.get("title") or movie.get("origin_title") or "",
                "release_date": movie.get("release_date"),
                "cover": movie.get("cover_url") or movie.get("javbus_cover") or "",
                "sub_status": status,
            })
        return out

    def list_movie_statuses(self, subscription_id: int) -> list[dict]:
        """返回清单订阅里命中的影片（供清单影片弹窗）。

        与 matched_count 同口径：实时解析清单影片并应用订阅命中条件（movie_ok），
        而不是只读候选表（候选表只有带磁链的影片才会生成）。状态与演员弹窗一致。
        """
        subscription = self.db.get_subscription(subscription_id)
        if not subscription or subscription["target_type"] != "list":
            raise ValueError("非清单订阅")
        # 从本地清单影片缓存读取（resolve_movies 已缓存，秒开）；无缓存则实时拉取并补缓存
        movie_ids = self.db.list_movie_ids(subscription["target_id"])
        if not movie_ids:
            self.resolver.resolve_movies(subscription)
            movie_ids = self.db.list_movie_ids(subscription["target_id"])
        movies = []
        for mid in movie_ids:
            row = self.db.get_movie(mid)
            if row:
                movies.append(dict(row))
        pushed_ids = {r["movie_id"] for r in self.db.conn.execute(
            "SELECT DISTINCT c.movie_id FROM subscription_push_attempts p "
            "JOIN subscription_candidates c ON c.id=p.candidate_id "
            "WHERE p.subscription_id=? AND p.status='succeeded'", (subscription_id,)
        )}
        skipped_ids = self.db.skipped_set(subscription_id)
        out = []
        for movie in movies:
            movie_ok, _ = self.matcher.movie_ok(subscription, movie,
                                                actor_ids=self.db.movie_actor_ids(movie["id"]),
                                                list_id=subscription["target_id"])
            if not movie_ok:
                continue
            status = "completed" if movie["id"] in pushed_ids else ("skipped" if movie["id"] in skipped_ids else "active")
            out.append({
                "id": movie["id"], "number": movie["number"],
                "title": movie.get("title") or movie.get("origin_title") or "",
                "release_date": movie.get("release_date"),
                "cover": movie.get("cover_url") or movie.get("javbus_cover") or "",
                "sub_status": status,
            })
        return out


    def refresh_status(self, subscription_id: int) -> None:
        """重算订阅状态：只要还有“订阅中”影片，就保持 active；
        只有所有命中影片都推送/跳过完，才置为 completed。影片订阅不适用（单部即整体）。
        """
        subscription = self.db.get_subscription(subscription_id)
        if not subscription or subscription["target_type"] == "movie":
            return
        if subscription["target_type"] == "actor":
            statuses = self.actor_movie_statuses(subscription_id)
        elif self.resolver.client:
            statuses = self.list_movie_statuses(subscription_id)
        else:
            return  # 清单需 client 解析，无法确定则保持现状
        if not statuses:
            return  # 暂无影片/无法确定，保持现状，避免误判
        has_active = any(s["sub_status"] == "active" for s in statuses)
        new_status = "active" if has_active else "completed"
        if subscription.get("status") != new_status:
            self.db.conn.execute(
                "UPDATE subscriptions SET status=?, completed_at=?, updated_at=? WHERE id=?",
                (new_status, _now() if new_status == "completed" else None, _now(), subscription_id),
            )
            self.db.commit()


class SubscriptionPushService:
    def __init__(self, db: Database,
                 push_func: Callable[[str, str, str, str | None, str], tuple[bool, int, str]]):
        self.db = db
        self.push_func = push_func

    def push_candidate(self, subscription_id: int, candidate_id: int,
                       idempotency_key: str) -> dict:
        subscription = self.db.get_subscription(subscription_id)
        candidate = self.db.get_subscription_candidate(candidate_id)
        if not subscription or not candidate or candidate["subscription_id"] != subscription_id:
            raise ValueError("候选资源不存在")
        if not (candidate["matched"] or candidate["predownload"]):
            raise ValueError("该资源不符合订阅条件")

        existing = self.db.conn.execute(
            "SELECT * FROM subscription_push_attempts WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        if existing:
            ok = existing["status"] == "succeeded"
            return {"ok": ok, "attempt_id": existing["id"], "push_id": existing["push_id"],
                    "message": "已处理过" if ok else (existing["error_message"] or "上次推送失败")}

        if subscription["download_mode"] == "upgrade":
            previous = self.db.best_successful_score(subscription_id)
            if previous is not None and tuple(candidate["resource_score"]) <= tuple(previous):
                raise ValueError("该资源并不优于已完成资源")

        attempt_id = self.db.create_push_attempt(subscription_id, candidate_id,
                                                  idempotency_key, "running")
        movie = self.db.get_movie(candidate.get("movie_id")) if candidate.get("movie_id") else None
        code = movie["number"] if movie else ""
        try:
            with PUSH_CONCURRENCY:
                ok, push_id, message = self.push_func(
                    candidate["magnet_uri"], candidate["magnet_name"], candidate.get("size_text") or "",
                    candidate.get("movie_id"), code or "",
                )
        except Exception as e:  # noqa: BLE001
            ok, push_id, message = False, None, str(e)
        self.db.mark_subscription_push(attempt_id, "succeeded" if ok else "failed",
                                       push_id=push_id or None, error=None if ok else message)
        return {"ok": ok, "attempt_id": attempt_id, "push_id": push_id, "message": message}

    def _pick_best(self, candidates: list[dict]) -> dict | None:
        def key(c):
            has_tracker = 1 if "&tr=" in (c.get("magnet_uri") or "") else 0
            return (tuple(c["resource_score"]), has_tracker, c["id"])
        return max(candidates, key=key) if candidates else None

    def _movie_in_library(self, candidate: dict, lib_codes: set[str]) -> bool:
        """候选影片番号是否已在媒体库。无 movie_id / 无番号时视为未入库。"""
        movie_id = candidate.get("movie_id")
        if not movie_id:
            return False
        movie = self.db.get_movie(movie_id)
        return bool(movie) and (movie["number"] or "") in lib_codes

    def _library_movie_ids(self, candidates: list[dict], lib_codes: set[str]) -> set[str]:
        """候选中已在媒体库的影片，按 movie_id 去重。"""
        ids: set[str] = set()
        for c in candidates:
            mid = c.get("movie_id")
            if mid and mid not in ids and self._movie_in_library(c, lib_codes):
                ids.add(mid)
        return ids

    def auto_push(self, subscription_id: int, force: bool = False) -> dict:
        """执行操作：对订阅自动推送最优命中候选。

        force=True（弹窗「执行订阅」用户主动确认）：预下载订阅也放行，
        且预下载可推送非 push_ok 的 predownload 候选。
        提交到 115 后不立即判成功（下载异步完成），记 info_hash 交给后台
        verify_and_retry 轮询确认；失败自动换下一颗磁链重试。
        """
        subscription = self.db.get_subscription(subscription_id)
        if not subscription:
            return {"ok": False, "message": "订阅不存在"}
        if subscription.get("status") == "completed":
            return {"ok": False, "message": "订阅已完成，跳过"}
        if subscription.get("pre_download") and not force:
            return {"ok": False, "message": "预下载订阅需手动确认"}
        # 已有一次推送正等网盘验证：不再叠加提交，等待结果
        pending = self.db.conn.execute(
            "SELECT id FROM subscription_push_attempts WHERE subscription_id=? "
            "AND status='running' AND info_hash IS NOT NULL LIMIT 1", (subscription_id,)
        ).fetchone()
        if pending:
            return {"ok": True, "attempt_id": pending["id"], "message": "已有推送在等待网盘下载"}
        # 洗版模式（upgrade）：已入库且符合条件的片才允许推送，故放行全部候选；
        # 其余模式：已在媒体库的影片跳过不推送，并记入“跳过”选项卡（仅演员/清单订阅）。
        is_upgrade = subscription.get("download_mode") == "upgrade"
        lib_codes = None if is_upgrade else self.db.library_codes()
        matched = self.db.untried_matched_candidates(subscription_id, False)
        if not matched:
            return {"ok": False, "message": "没有可用的命中磁链"}
        if lib_codes is not None and subscription["target_type"] in ("actor", "list"):
            lib_ids = self._library_movie_ids(matched, lib_codes)
            for mid in lib_ids:
                self.db.add_skip(subscription_id, mid)
            avail = [c for c in matched if c.get("movie_id") not in lib_ids]
        else:
            lib_ids = set()
            avail = matched
        # 优先 push_ok 候选；预下载(force)时放宽为只要 matched 即可（推 predownload）
        require_push_ok = not (subscription.get("pre_download") and force)
        if require_push_ok:
            candidate = self._pick_best([c for c in avail if c.get("push_ok")])
        else:
            candidate = self._pick_best(avail)
        if not candidate:
            msg = "没有可用的命中磁链"
            if not is_upgrade and lib_ids:
                msg = f"命中影片均已入库，跳过 {len(lib_ids)} 部（洗版模式才可推送已入库影片）"
            return {"ok": False, "message": msg}
        key = f"auto:{subscription_id}:{candidate['resource_fingerprint']}"
        existing = self.db.conn.execute(
            "SELECT * FROM subscription_push_attempts WHERE idempotency_key=?", (key,)
        ).fetchone()
        if existing:
            ok = existing["status"] == "succeeded"
            return {"ok": ok, "attempt_id": existing["id"],
                    "message": "已推送过" if ok else (existing["error_message"] or "待网盘下载")}
        attempt_id = self.db.create_push_attempt(subscription_id, candidate["id"], key, "running")
        self.db.mark_candidate_attempted(candidate["id"])
        movie = self.db.get_movie(candidate.get("movie_id")) if candidate.get("movie_id") else None
        code = movie["number"] if movie else ""
        ok, push_id, message = self.push_func(
            candidate["magnet_uri"], candidate["magnet_name"], candidate.get("size_text") or "",
            candidate.get("movie_id"), code or "",
        )
        if not ok:
            self.db.mark_subscription_push(attempt_id, "failed", error=message)
            return {"ok": False, "attempt_id": attempt_id, "message": message}
        # 已提交 115，写入 info_hash 交给后台验证，不立即判成功
        self.db.mark_push_info_hash(attempt_id, _info_hash_of(candidate.get("magnet_uri")))
        return {"ok": True, "attempt_id": attempt_id,
                "message": "已提交，等待网盘下载 " + (candidate.get("magnet_name") or "")}

    def subscribe_movie(self, subscription_id: int, movie_id: str) -> dict:
        """对某部命中影片推送最优磁链（弹窗「执行订阅」逐部调用）。

        候选优先 push_ok；若没有 push_ok（预下载/条件放宽）则退回 matched 候选直接用。
        """
        sub = self.db.get_subscription(subscription_id)
        if not sub:
            return {"ok": False, "message": "订阅不存在"}
        # 非洗版模式：已在媒体库的影片跳过不推送，并记入“跳过”选项卡；洗版模式放行。
        if sub.get("download_mode") != "upgrade":
            movie = self.db.get_movie(movie_id)
            if movie and (movie["number"] or "") in self.db.library_codes():
                if sub["target_type"] in ("actor", "list"):
                    self.db.add_skip(subscription_id, movie_id)
                return {"ok": False, "message": "影片已入库，跳过"}
        row = self.db.conn.execute(
            "SELECT id FROM subscription_candidates WHERE subscription_id=? AND movie_id=? AND matched=1 "
            "ORDER BY push_ok DESC, resource_score DESC, id DESC LIMIT 1",
            (subscription_id, movie_id),
        ).fetchone()
        if not row:
            row = self.db.conn.execute(
                "SELECT id FROM subscription_candidates WHERE subscription_id=? AND movie_id=? AND matched=1 "
                "ORDER BY resource_score DESC, id DESC LIMIT 1",
                (subscription_id, movie_id),
            ).fetchone()
        if not row:
            return {"ok": False, "message": "该影片无命中磁链"}
        candidate = self.db.get_subscription_candidate(row["id"])
        key = f"sub:{subscription_id}:{movie_id}:{candidate['resource_fingerprint']}"
        result = self.push_candidate(subscription_id, candidate["id"], key)
        # 115 提示“任务已存在”实际是已推送，视为成功
        if not result.get("ok") and ("已存在" in (result.get("message") or "")):
            result["ok"] = True
            result["message"] = "已存在，无需重复推送"
        return result
