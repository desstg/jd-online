"""抓取管线编排：搜索 -> 详情 -> 归一化 -> 入库。"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .client import JavdbClient, normalize_image_url
from .db import Database
from .magnetlib import MagnetLibClient, MagnetLibError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(v) -> str | None:
    return json.dumps(v, ensure_ascii=False) if v else None


def _tag_names(tags) -> list[str] | None:
    """标签在 API 里是 [{id,name}] 字典列表，这里只取 name。"""
    if not tags:
        return None
    out = []
    for t in tags:
        if isinstance(t, dict):
            name = t.get("name")
            if name:
                out.append(name)
        elif isinstance(t, str):
            out.append(t)
    return out or None


def normalize_movie(movie: dict) -> dict:
    """把 /v4/movies/{id} 的 data.movie 归一化成 movies 表一行。"""
    preview_images = []
    for item in movie.get("preview_images") or []:
        if isinstance(item, dict):
            large = normalize_image_url(item.get("large_url") or "")
            preview_images.append({"large": large, "thumb": item.get("thumb_url") or ""})
        elif isinstance(item, str):
            preview_images.append({"large": normalize_image_url(item), "thumb": ""})

    def b(key):
        return 1 if movie.get(key) else 0

    return {
        "id": movie.get("id"),
        "number": movie.get("number"),
        "title": movie.get("title"),
        "origin_title": movie.get("origin_title"),
        "cover_url": movie.get("cover_url"),
        "thumb_url": movie.get("thumb_url"),
        "duration": movie.get("duration"),
        "release_date": movie.get("release_date"),
        "score": movie.get("score"),
        "summary": movie.get("summary"),
        "review": movie.get("review"),
        "director_id": movie.get("director_id"),
        "director_name": movie.get("director_name"),
        "maker_id": movie.get("maker_id"),
        "maker_name": movie.get("maker_name"),
        "publisher_id": movie.get("publisher_id"),
        "publisher_name": movie.get("publisher_name"),
        "series_id": movie.get("series_id"),
        "series_name": movie.get("series_name"),
        "tags": _json(_tag_names(movie.get("tags"))),
        "preview_images": _json(preview_images),
        "preview_video_url": movie.get("preview_video_url"),
        "play_sources": _json(movie.get("play_sources")),
        "magnets_count": movie.get("magnets_count"),
        "reviews_count": movie.get("reviews_count"),
        "comments_count": movie.get("comments_count"),
        "watched_count": movie.get("watched_count"),
        "want_watch_count": movie.get("want_watch_count"),
        "has_cnsub": b("has_cnsub"),
        "has_preview_images": b("has_preview_images"),
        "has_preview_video": b("has_preview_video"),
        "can_play": b("can_play"),
        "type": movie.get("type"),
        "number_letter": movie.get("number_letter"),
        "raw": json.dumps(movie, ensure_ascii=False),
        "fetched_at": _now(),
    }


def normalize_review(review: dict, movie_id: str) -> dict:
    return {
        "id": review.get("id"),
        "movie_id": movie_id,
        "user_id": review.get("user_id"),
        "username": review.get("username"),
        "score": review.get("score"),
        "content": review.get("content"),
        "status": review.get("status"),
        "status_title": review.get("status_title"),
        "watched_count": review.get("watched_count"),
        "likes_count": review.get("likes_count"),
        "liked": 1 if review.get("liked") else 0,
        "created_at": review.get("created_at"),
    }


def ingest_movie_id(client: JavdbClient, db: Database, movie_id: str) -> dict:
    """抓取并入库一部影片的完整详情 + 演员关联。返回归一化后的 movie。"""
    res = client.movie(movie_id)
    movie = normalize_movie(res["data"]["movie"])
    db.upsert_movie(movie)

    for actor in res["data"]["movie"].get("actors") or []:
        db.upsert_actor({
            "id": actor.get("id"),
            "name": actor.get("name"),
            "gender": actor.get("gender"),
            "avatar_url": actor.get("avatar_url"),
        })
        db.link_movie_actor(movie["id"], actor.get("id"))
    db.commit()
    return movie


def ingest_by_number(client: JavdbClient, db: Database, number: str) -> dict:
    """按番号搜索并入库第一部命中结果。"""
    res = client.search(number, limit=1)
    movies = (res.get("data") or {}).get("movies") or []
    if not movies:
        raise ValueError(f"未找到番号: {number}")
    movie_id = movies[0]["id"]
    return ingest_movie_id(client, db, movie_id)


def ingest_reviews(client: JavdbClient, db: Database, movie_id: str,
                   max_pages: int = 5, page_size: int = 20) -> int:
    """抓取并入库某部影片的评论（默认最多 5 页）。返回入库条数。"""
    total = 0
    for page in range(1, max_pages + 1):
        res = client.reviews(movie_id, page=page, page_size=page_size)
        reviews = (res.get("data") or {}).get("reviews") or []
        if not reviews:
            break
        for r in reviews:
            db.upsert_review(normalize_review(r, movie_id))
        total += len(reviews)
        if len(reviews) < page_size:
            break
    db.commit()
    return total


def ingest_magnets(javbus, db: Database, code: str, movie_id: str | None = None) -> int:
    """抓取某番号在 JAVBUS 的磁链 + 封面 + 预览图并入库。返回入库磁链条数。"""
    items, cover_url, samples = javbus.fetch_magnets(code)
    for it in items:
        db.upsert_magnet({
            "btih": it["btih"],
            "movie_id": movie_id,
            "code": code,
            "name": it["name"],
            "size": it["size"],
            "date": it["date"],
            "magnet": it["magnet"],
            "has_hd": it["has_hd"],
            "has_sub": it["has_sub"],
            "source": "javbus",
            "fetched_at": _now(),
        })
    mid = movie_id
    if not mid:
        row = db.movie_by_number(code)
        mid = row["id"] if row else None
    if mid and cover_url:
        db.set_javbus_cover(mid, cover_url)
    db.commit()
    return len(items)


def ingest_source_magnets(lib: dict, db: Database, code: str, movie_id: str | None = None,
                          timeout: int = 30, proxy: str | None = None) -> int:
    """用自定义磁链库（lib 含 api_url_template/headers）抓取某番号磁链入库。

    source 取库的 name（无则 id），便于详情页区分磁链来源。返回入库条数。
    """
    client = MagnetLibClient(
        api_url_template=lib.get("api_url_template") or "",
        headers=lib.get("headers") or [],
        timeout=timeout,
        proxy=proxy,
    )
    src = lib.get("name") or lib.get("id") or "magnetlib"
    try:
        items = client.fetch_magnets(code)
    except MagnetLibError:
        return 0
    mid = movie_id
    if not mid:
        row = db.movie_by_number(code)
        mid = row["id"] if row else None
    for it in items:
        db.upsert_magnet({
            "btih": it["btih"],
            "movie_id": mid,
            "code": code,
            "name": it["name"],
            "size": it["size"],
            "date": it["date"],
            "magnet": it["magnet"],
            "has_hd": it["has_hd"],
            "has_sub": it["has_sub"],
            "source": src,
            "fetched_at": _now(),
        })
    db.commit()
    return len(items)


def ingest_ranking(client: JavdbClient, db: Database, movies: list[dict],
                   detail: bool = True) -> int:
    """把榜单/搜索返回的影片列表入库；detail=True 时逐部抓详情（含演员）。"""
    count = 0
    for item in movies:
        movie_id = item.get("id")
        if not movie_id:
            continue
        if detail:
            ingest_movie_id(client, db, movie_id)
        else:
            # 仅存列表摘要：补足缺失字段，避免覆盖已有完整详情
            if not db.has_movie(movie_id):
                db.upsert_movie(normalize_movie(item))
        count += 1
    return count
