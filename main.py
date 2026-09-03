#!/usr/bin/env python3
"""JAVDB 抓取管线命令行入口。

用法示例：
    python main.py search "SSIS-001"
    python main.py detail "SSIS-001"          # 抓详情 + 演员入库
    python main.py reviews "SSIS-001"         # 抓评论入库
    python main.py hot                        # 热播榜入库（抓详情）
    python main.py top250 --type all --page 1 # 需登录
    python main.py login                      # 登录并保存 token
    python main.py stats
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from javdb.client import JavdbClient
from javdb.db import Database
from javdb.javbus import JavbusClient
from javdb.mediaserver import MediaServerClient
from javdb import scrape
from javdb import sync

DEFAULT_CONFIG = "config.json"
DEFAULT_DB = "javdb.db"


def _ensure_utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def load_config(path: str) -> dict:
    p = Path(path)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def save_config(path: str, cfg: dict) -> None:
    Path(path).write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def build_client(cfg: dict) -> JavdbClient:
    return JavdbClient(
        username=cfg.get("javdb_username") or None,
        password=cfg.get("javdb_password") or None,
        token=cfg.get("javdb_token") or None,
        min_interval=float(cfg.get("min_interval", 0.5)),
        proxy=cfg.get("proxy") or None,
        api_base=cfg.get("api_base") or None,
    )


def _movie_id(client: JavdbClient, arg: str) -> str:
    """番号含 '-'（如 SSIS-001）则先搜索；否则视为 API id 直接使用。"""
    if "-" in arg:
        res = client.search(arg, limit=1)
        movies = (res.get("data") or {}).get("movies") or []
        if not movies:
            raise SystemExit(f"未找到: {arg}")
        return movies[0]["id"]
    return arg


def _resolve_code_movie(client: JavdbClient, db: Database, arg: str) -> tuple[str, str | None]:
    """返回 (code, movie_id)。arg 可为番号或 API id。"""
    if "-" in arg:
        row = db.movie_by_number(arg)
        return arg, (row["id"] if row else None)
    row = db.get_movie(arg)
    if row and row["number"]:
        return row["number"], arg
    res = client.movie(arg)
    return res["data"]["movie"]["number"], arg


def cmd_search(client, cfg, args):
    res = client.search(args.query, limit=args.limit)
    movies = (res.get("data") or {}).get("movies") or []
    print(f"命中 {len(movies)} 条：")
    for m in movies:
        print(f"  {m.get('number')}  [{m.get('id')}]  score={m.get('score')}  {m.get('title')}")


def cmd_detail(client, cfg, args):
    db = Database(cfg.get("db_path", DEFAULT_DB))
    try:
        mid = _movie_id(client, args.target)
        movie = scrape.ingest_movie_id(client, db, mid)
        print(f"已入库: {movie['number']}  [{movie['id']}]")
        print(f"  标题: {movie['title']}")
        print(f"  评分: {movie['score']}  发行: {movie['release_date']}  时长: {movie['duration']}")
        print(f"  片商: {movie['maker_name']}  系列: {movie['series_name']}")
        print(f"  预览图: {len(json.loads(movie['preview_images'] or '[]'))} 张  磁链数: {movie['magnets_count']}")
        hits = sync.check_library(db, movie["number"])
        if hits:
            print(f"  入库状态: 已入库 @ {', '.join(h['server_name'] for h in hits)}")
        else:
            print(f"  入库状态: 未入库")
        if args.magnets:
            javbus = JavbusClient(base_url=cfg.get("javbus_base", "https://www.javbus.com"))
            n = scrape.ingest_magnets(javbus, db, movie["number"], mid)
            print(f"  磁链入库 {n} 条 (JAVBUS)")
    finally:
        db.close()


def cmd_reviews(client, cfg, args):
    db = Database(cfg.get("db_path", DEFAULT_DB))
    try:
        mid = _movie_id(client, args.target)
        n = scrape.ingest_reviews(client, db, mid, max_pages=args.pages)
        print(f"评论入库 {n} 条 (movie_id={mid})")
    finally:
        db.close()


def cmd_magnets(client, cfg, args):
    db = Database(cfg.get("db_path", DEFAULT_DB))
    try:
        javbus = JavbusClient(base_url=cfg.get("javbus_base", "https://www.javbus.com"))
        code, movie_id = _resolve_code_movie(client, db, args.target)
        n = scrape.ingest_magnets(javbus, db, code, movie_id)
        print(f"磁链入库 {n} 条 (番号={code})")
    finally:
        db.close()


def cmd_hot(client, cfg, args):
    db = Database(cfg.get("db_path", DEFAULT_DB))
    try:
        res = client.hot(period=args.period)
        movies = (res.get("data") or {}).get("movies") or []
        print(f"热播榜 {args.period}: {len(movies)} 部")
        n = scrape.ingest_ranking(client, db, movies, detail=not args.summary_only)
        print(f"入库 {n} 部" + ("（仅摘要）" if args.summary_only else "（含详情+演员）"))
    finally:
        db.close()


def cmd_top250(client, cfg, args):
    db = Database(cfg.get("db_path", DEFAULT_DB))
    try:
        res = client.top250(type_=args.type, type_value=args.type_value or "",
                            page=args.page, limit=args.limit, year=args.year or "")
        if res.get("success") != 1 and not res.get("data"):
            raise SystemExit("Top250 获取失败（是否已登录？先 python main.py login）: "
                             + str(res.get("message")))
        movies = (res.get("data") or {}).get("movies") or []
        print(f"Top250 第{args.page}页: {len(movies)} 部")
        n = scrape.ingest_ranking(client, db, movies, detail=not args.summary_only)
        print(f"入库 {n} 部" + ("（仅摘要）" if args.summary_only else "（含详情+演员）"))
    finally:
        db.close()


def cmd_login(client, cfg, args):
    username = args.username or cfg.get("javdb_username") or ""
    password = args.password or cfg.get("javdb_password") or ""
    if not username or not password:
        raise SystemExit("缺少用户名/密码：请用 --username / --password，或先在 config.json 填 javdb_username / javdb_password")
    token = client.login(username, password)
    cfg["javdb_token"] = token
    cfg["javdb_username"] = client.username or username
    cfg["javdb_password"] = client.password or password
    save_config(args.config, cfg)
    print(f"登录成功，token 已保存到 {args.config}")


def cmd_stats(client, cfg, args):
    db = Database(cfg.get("db_path", DEFAULT_DB))
    try:
        c = db.counts()
        print("库统计:", ", ".join(f"{k}={v}" for k, v in c.items()))
    finally:
        db.close()


def cmd_server_add(client, cfg, args):
    db = Database(cfg.get("db_path", DEFAULT_DB))
    try:
        sid = db.add_server(args.name, args.url, args.api_key, args.type)
        print(f"已添加服务器 id={sid}: {args.name} ({args.url}) [{args.type}]")
    finally:
        db.close()


def cmd_server_list(client, cfg, args):
    db = Database(cfg.get("db_path", DEFAULT_DB))
    try:
        rows = db.list_servers()
        if not rows:
            print("未配置服务器。用 server-add 添加。")
            return
        for r in rows:
            print(f"  [{r['id']}] {r['name']}  {r['url']}  type={r['type']}")
    finally:
        db.close()


def cmd_server_rm(client, cfg, args):
    db = Database(cfg.get("db_path", DEFAULT_DB))
    try:
        db.remove_server(args.id)
        print(f"已删除服务器 id={args.id}")
    finally:
        db.close()


def cmd_server_ping(client, cfg, args):
    db = Database(cfg.get("db_path", DEFAULT_DB))
    try:
        for r in db.list_servers():
            c = MediaServerClient(r["url"], r["api_key"], r["name"], r["type"])
            ok, msg = c.ping()
            print(f"  [{r['id']}] {r['name']}: {'✓ ' + msg if ok else '✗ ' + msg}")
    finally:
        db.close()


def cmd_sync(client, cfg, args):
    db = Database(cfg.get("db_path", DEFAULT_DB))
    try:
        servers = db.get_servers()
        if not servers:
            print("未配置服务器。先 `python main.py server-add --name .. --url .. --api-key ..`")
            return
        total, results = sync.sync_library(db, servers)
        for name, msg, n in results:
            print(f"  {name}: {msg}  入库 {n} 条")
        print(f"总计 {total} 个番号入索引")
    finally:
        db.close()


def cmd_status(client, cfg, args):
    db = Database(cfg.get("db_path", DEFAULT_DB))
    try:
        code = args.code.upper()
        hits = sync.check_library(db, code, servers=db.get_servers(), live=args.live)
        if hits:
            for h in hits:
                print(f"已入库: {code} @ {h['server_name']} ({h['server_type']})  {h.get('title') or ''}")
        else:
            print(f"未入库: {code}")
    finally:
        db.close()


def cmd_match(client, cfg, args):
    db = Database(cfg.get("db_path", DEFAULT_DB))
    try:
        rows = sync.match_movies(db)
        in_lib = [r for r in rows if r["servers"]]
        missing = [r for r in rows if not r["servers"]]
        print(f"共 {len(rows)} 部已抓取：已入库 {len(in_lib)} 部，未入库 {len(missing)} 部")
        if args.missing:
            for r in missing:
                print(f"  缺: {r['number']}  {r['movie_title'][:40]}")
        elif args.have:
            for r in in_lib:
                print(f"  有: {r['number']} @ {r['servers']}")
    finally:
        db.close()


def main(argv=None):
    _ensure_utf8_stdout()
    ap = argparse.ArgumentParser(description="JAVDB 抓取管线")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径")
    ap.add_argument("--db", default=None, help="SQLite 路径（覆盖 config.db_path）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("search"); p.add_argument("query"); p.add_argument("--limit", type=int, default=20); p.set_defaults(fn=cmd_search)
    p = sub.add_parser("detail"); p.add_argument("target"); p.add_argument("--magnets", action="store_true", help="同时抓 JAVBUS 磁链"); p.set_defaults(fn=cmd_detail)
    p = sub.add_parser("reviews"); p.add_argument("target"); p.add_argument("--pages", type=int, default=5); p.set_defaults(fn=cmd_reviews)
    p = sub.add_parser("magnets"); p.add_argument("target"); p.set_defaults(fn=cmd_magnets)
    p = sub.add_parser("hot"); p.add_argument("--period", default="daily"); p.add_argument("--summary-only", action="store_true"); p.set_defaults(fn=cmd_hot)
    p = sub.add_parser("top250"); p.add_argument("--type", default="all"); p.add_argument("--type-value", default=""); p.add_argument("--year", default=""); p.add_argument("--page", type=int, default=1); p.add_argument("--limit", type=int, default=50); p.add_argument("--summary-only", action="store_true"); p.set_defaults(fn=cmd_top250)
    p = sub.add_parser("login"); p.add_argument("--username"); p.add_argument("--password"); p.set_defaults(fn=cmd_login)
    p = sub.add_parser("stats"); p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("server-add"); p.add_argument("--name", required=True); p.add_argument("--url", required=True); p.add_argument("--api-key", required=True); p.add_argument("--type", choices=["emby", "jellyfin"], default="emby"); p.set_defaults(fn=cmd_server_add)
    p = sub.add_parser("server-list"); p.set_defaults(fn=cmd_server_list)
    p = sub.add_parser("server-rm"); p.add_argument("id", type=int); p.set_defaults(fn=cmd_server_rm)
    p = sub.add_parser("server-ping"); p.set_defaults(fn=cmd_server_ping)
    p = sub.add_parser("sync"); p.set_defaults(fn=cmd_sync)
    p = sub.add_parser("status"); p.add_argument("code"); p.add_argument("--live", action="store_true"); p.set_defaults(fn=cmd_status)
    p = sub.add_parser("match"); p.add_argument("--missing", action="store_true"); p.add_argument("--have", action="store_true"); p.set_defaults(fn=cmd_match)

    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    if args.db:
        cfg["db_path"] = args.db
    client = build_client(cfg)
    args.fn(client, cfg, args)


if __name__ == "__main__":
    main()
