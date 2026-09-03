"""媒体库入库联动：全量同步 Emby/Jellyfin，建立番号 -> 条目索引。"""

from __future__ import annotations

from .db import Database
from .mediaserver import MediaServerClient, extract_code


def sync_library(db: Database, servers: list[dict], progress=None) -> tuple[int, list[tuple[str, str, int]]]:
    """对每个服务器拉全库、提取番号、写入 library_items。

    返回 (入库番号总数, [(服务器名, 状态, 该服务器入库条数)]).
    """
    total = 0
    results: list[tuple[str, str, int]] = []
    for srv in servers:
        label = srv.get("name") or srv.get("url", "")
        client = MediaServerClient(
            url=srv["url"], api_key=srv["api_key"],
            name=srv.get("name", ""), type_=srv.get("type", "emby"),
        )
        ok, msg = client.ping()
        if not ok:
            results.append((label, f"连接失败: {msg}", 0))
            continue

        try:
            items = client.fetch_all_movies()
        except Exception as e:  # noqa: BLE001
            results.append((label, f"拉取失败: {e}", 0))
            continue

        server_id = db.add_server(srv.get("name", label), srv["url"], srv["api_key"], srv.get("type", "emby"))
        db.clear_library(server_id)
        n = 0
        for it in items:
            code = extract_code(it.get("Name")) or extract_code(it.get("Path"))
            if code:
                db.upsert_library_item(server_id, code, it)
                n += 1
            if progress:
                progress(it)
        db.commit()
        total += n
        results.append((label, f"在线（{msg}）", n))
    return total, results


def check_library(db: Database, code: str, servers: list[dict] | None = None,
                  live: bool = False) -> list[dict]:
    """查询某番号是否在库。默认查索引；live=True 时对服务器实时搜索补充。"""
    hits = [dict(r) for r in db.lookup_library(code)]
    if live and servers:
        seen = {h.get("server_name") for h in hits}
        for srv in servers:
            label = srv.get("name") or srv.get("url", "")
            if label in seen:
                continue
            client = MediaServerClient(srv["url"], srv["api_key"], label, srv.get("type", "emby"))
            try:
                item = client.search(code)
            except Exception:  # noqa: BLE001
                continue
            if item:
                hits.append({
                    "code": code.upper(),
                    "title": item.get("Name"),
                    "item_id": item.get("Id"),
                    "server_name": label,
                    "server_type": srv.get("type", "emby"),
                    "server_url": srv["url"],
                })
    return hits


def match_movies(db: Database) -> list[dict]:
    """把库里的 movies 与 library_items 按番号关联，返回每部的入库情况。"""
    rows = db.conn.execute(
        """
        SELECT m.number, m.title AS movie_title,
               GROUP_CONCAT(DISTINCT s.name) AS servers
        FROM movies m
        LEFT JOIN library_items li ON li.code = m.number
        LEFT JOIN media_servers s ON s.id = li.server_id
        GROUP BY m.id
        ORDER BY m.release_date DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]
