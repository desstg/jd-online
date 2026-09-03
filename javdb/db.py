"""SQLite 存储层：movies / actors / movie_actors / reviews 四张表。

归一化列之外保留 raw 全量 JSON，保证不丢字段、后续可随时扩展。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

SCHEMA = """
CREATE TABLE IF NOT EXISTS movies (
    id                TEXT PRIMARY KEY,   -- API id，如 ZY5eq
    number            TEXT,               -- 番号，如 SSIS-001
    title             TEXT,
    origin_title      TEXT,
    cover_url         TEXT,
    thumb_url         TEXT,
    javbus_cover      TEXT,               -- JAVBUS 干净封面（走代理直链）
    duration          INTEGER,
    release_date      TEXT,
    score             REAL,
    summary           TEXT,
    review            TEXT,
    director_id       TEXT,
    director_name     TEXT,
    maker_id          TEXT,
    maker_name        TEXT,
    publisher_id      TEXT,
    publisher_name    TEXT,
    series_id         TEXT,
    series_name       TEXT,
    tags              TEXT,               -- JSON array
    preview_images    TEXT,               -- JSON array（已归一化 CDN）
    preview_video_url TEXT,
    play_sources      TEXT,               -- JSON
    magnets_count     INTEGER,
    reviews_count     INTEGER,
    comments_count    INTEGER,
    watched_count     INTEGER,
    want_watch_count  INTEGER,
    has_cnsub         INTEGER,
    has_preview_images INTEGER,
    has_preview_video  INTEGER,
    can_play          INTEGER,
    type              TEXT,
    number_letter     TEXT,
    raw               TEXT,               -- 详情接口原始 JSON
    fetched_at        TEXT,
    last_viewed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_movies_number ON movies(number);
CREATE INDEX IF NOT EXISTS idx_movies_release_date ON movies(release_date);
CREATE INDEX IF NOT EXISTS idx_movies_score ON movies(score);

CREATE TABLE IF NOT EXISTS actors (
    id          TEXT PRIMARY KEY,        -- API id，如 A5yq
    name        TEXT,
    gender      INTEGER,
    avatar_url  TEXT
);
CREATE INDEX IF NOT EXISTS idx_actors_name ON actors(name);

CREATE TABLE IF NOT EXISTS movie_actors (
    movie_id TEXT,
    actor_id TEXT,
    PRIMARY KEY (movie_id, actor_id)
);

CREATE TABLE IF NOT EXISTS reviews (
    id            INTEGER PRIMARY KEY,   -- API review id
    movie_id      TEXT,
    user_id       INTEGER,
    username      TEXT,
    score         REAL,
    content       TEXT,
    status        TEXT,
    status_title  TEXT,
    watched_count INTEGER,
    likes_count   INTEGER,
    liked         INTEGER,
    created_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_reviews_movie ON reviews(movie_id);

CREATE TABLE IF NOT EXISTS magnets (
    btih       TEXT PRIMARY KEY,         -- magnet 的 40 位 info hash（去重键）
    movie_id   TEXT,                     -- 关联 movies.id（可为空）
    code       TEXT,                     -- 番号
    name       TEXT,
    size       TEXT,
    date       TEXT,
    magnet     TEXT,                     -- 完整 magnet URL
    has_hd     INTEGER,
    has_sub    INTEGER,
    source     TEXT,                     -- 'javbus'
    fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_magnets_code ON magnets(code);
CREATE INDEX IF NOT EXISTS idx_magnets_movie ON magnets(movie_id);

CREATE TABLE IF NOT EXISTS push_queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    magnet     TEXT NOT NULL,
    name       TEXT,
    size       TEXT,
    movie_id   TEXT,
    code       TEXT,
    status     TEXT NOT NULL DEFAULT 'pending',
    downloader TEXT,
    pushed_at  TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_push_status ON push_queue(status);

CREATE TABLE IF NOT EXISTS pan115_config (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    cookie      TEXT,
    timeout     INTEGER DEFAULT 30,
    quota       INTEGER DEFAULT 0,
    target_cid  TEXT DEFAULT '',
    target_name TEXT DEFAULT '',
    enabled     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS clouddrive2_config (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    host        TEXT DEFAULT '127.0.0.1',
    rpc_port    INTEGER DEFAULT 19798,
    timeout     INTEGER DEFAULT 30,
    enable_ed2k INTEGER DEFAULT 0,
    api_token   TEXT DEFAULT '',
    user_name   TEXT DEFAULT '',
    password    TEXT DEFAULT '',
    save_path   TEXT DEFAULT '',
    enabled     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS downloader (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    enabled    INTEGER DEFAULT 0,
    sort_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS media_servers (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL,
    url     TEXT NOT NULL UNIQUE,
    api_key TEXT NOT NULL,
    type    TEXT NOT NULL DEFAULT 'emby'
);

CREATE TABLE IF NOT EXISTS library_items (
    item_id   TEXT NOT NULL,
    server_id INTEGER NOT NULL REFERENCES media_servers(id),
    code      TEXT NOT NULL,
    title     TEXT,
    path      TEXT,
    synced_at TEXT,
    PRIMARY KEY (server_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_library_code ON library_items(code);

CREATE TABLE IF NOT EXISTS subscriptions (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type             TEXT NOT NULL CHECK (target_type IN ('movie','online','actor','list')),
    target_id               TEXT,
    target_key              TEXT NOT NULL,
    target_name             TEXT NOT NULL,
    target_url              TEXT,
    status                  TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','completed')),
    download_mode           TEXT NOT NULL DEFAULT 'strict' CHECK (download_mode IN ('strict','upgrade')),
    pre_download            INTEGER NOT NULL DEFAULT 0,
    min_size_mb             INTEGER,
    max_size_mb             INTEGER,
    max_file_count          INTEGER,
    release_date_from       TEXT,
    release_date_to         TEXT,
    expiry_days             INTEGER,
    categories              TEXT,               -- JSON array：包含类别（影片必须属于其中之一）
    exclude_categories      TEXT,               -- JSON array：排除类别（属于这些类别时不订阅）
    enabled                 INTEGER NOT NULL DEFAULT 1,
    last_checked_at         TEXT,
    last_successful_push_at TEXT,
    completed_at            TEXT,
    last_error              TEXT,
    matched_count           INTEGER NOT NULL DEFAULT 0,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    UNIQUE (target_type, target_key)
);
CREATE INDEX IF NOT EXISTS idx_subscriptions_status ON subscriptions(status, enabled);
CREATE INDEX IF NOT EXISTS idx_subscriptions_target ON subscriptions(target_type, target_id);

CREATE TABLE IF NOT EXISTS subscription_quality_options (
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    quality         TEXT NOT NULL,
    PRIMARY KEY (subscription_id, quality)
);

CREATE TABLE IF NOT EXISTS subscription_priority_options (
    subscription_id INTEGER PRIMARY KEY REFERENCES subscriptions(id) ON DELETE CASCADE,
    priority_order  TEXT NOT NULL DEFAULT '["hd","uncensored","size"]',
    score_version   TEXT NOT NULL DEFAULT 'v1'
);

CREATE TABLE IF NOT EXISTS subscription_check_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    trigger_type    TEXT NOT NULL DEFAULT 'manual' CHECK (trigger_type IN ('manual','scheduled')),
    status          TEXT NOT NULL CHECK (status IN ('running','completed','failed')),
    matcher_version TEXT NOT NULL,
    matched_count   INTEGER NOT NULL DEFAULT 0,
    rejected_count  INTEGER NOT NULL DEFAULT 0,
    error_message   TEXT,
    started_at      TEXT NOT NULL,
    completed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_subscription_runs ON subscription_check_runs(subscription_id, id DESC);

CREATE TABLE IF NOT EXISTS subscription_candidates (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    check_run_id       INTEGER NOT NULL REFERENCES subscription_check_runs(id) ON DELETE CASCADE,
    subscription_id    INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    movie_id           TEXT,
    magnet_id          TEXT,
    magnet_hash        TEXT,
    magnet_name        TEXT NOT NULL,
    magnet_uri         TEXT NOT NULL,
    size_text          TEXT,
    size_bytes         INTEGER,
    file_count         INTEGER,
    release_date       TEXT,
    quality_tags       TEXT,
    resource_fingerprint TEXT NOT NULL,
    resource_score     TEXT NOT NULL,
    matched            INTEGER NOT NULL DEFAULT 0,
    push_ok            INTEGER NOT NULL DEFAULT 0,
    predownload        INTEGER NOT NULL DEFAULT 0,
    rejection_reasons  TEXT,
    created_at         TEXT NOT NULL,
    UNIQUE (check_run_id, resource_fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_subscription_candidates ON subscription_candidates(subscription_id, check_run_id);

CREATE TABLE IF NOT EXISTS subscription_push_attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    candidate_id    INTEGER NOT NULL REFERENCES subscription_candidates(id),
    push_id         INTEGER REFERENCES push_queue(id),
    idempotency_key TEXT NOT NULL UNIQUE,
    status          TEXT NOT NULL CHECK (status IN ('queued','running','succeeded','failed')),
    error_message   TEXT,
    requested_at    TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_subscription_pushes ON subscription_push_attempts(subscription_id, id DESC);

CREATE TABLE IF NOT EXISTS subscription_blacklist (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type TEXT NOT NULL CHECK (target_type IN ('movie','actor','list')),
    target_id   TEXT,
    target_key  TEXT NOT NULL,
    target_name TEXT NOT NULL,
    reason      TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (target_type, target_key)
);
CREATE INDEX IF NOT EXISTS idx_subscription_blacklist_type ON subscription_blacklist(target_type);

CREATE TABLE IF NOT EXISTS subscription_skips (
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    movie_id        TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (subscription_id, movie_id)
);

CREATE TABLE IF NOT EXISTS list_movies (
    list_id   TEXT NOT NULL,
    movie_id  TEXT NOT NULL,
    synced_at TEXT,
    PRIMARY KEY (list_id, movie_id)
);
CREATE INDEX IF NOT EXISTS idx_list_movies_list ON list_movies(list_id);

CREATE TABLE IF NOT EXISTS follows (
    user_id    INTEGER PRIMARY KEY,   -- 分享者 user_id（reviews.user_id）
    username   TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_follows_created ON follows(created_at);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self._seed()
        self.conn.commit()

    def _migrate(self) -> None:
        """为已存在的旧库补齐新增列（幂等）。"""
        for stmt in ("ALTER TABLE movies ADD COLUMN javbus_cover TEXT",
                     "ALTER TABLE movies ADD COLUMN last_viewed_at TEXT",
                     "ALTER TABLE push_queue ADD COLUMN downloader TEXT",
                     "ALTER TABLE subscriptions ADD COLUMN expiry_days INTEGER",
                     "ALTER TABLE subscriptions ADD COLUMN categories TEXT",
                     "ALTER TABLE subscriptions ADD COLUMN exclude_categories TEXT",
                     "ALTER TABLE subscription_push_attempts ADD COLUMN info_hash TEXT",
                     "ALTER TABLE subscription_candidates ADD COLUMN attempted INTEGER DEFAULT 0",
                     "ALTER TABLE subscription_candidates ADD COLUMN push_ok INTEGER DEFAULT 0",
                     "ALTER TABLE subscriptions ADD COLUMN matched_count INTEGER DEFAULT 0"):
            try:
                self.conn.execute(stmt)
            except sqlite3.OperationalError:
                pass
        # 质量表 CHECK 升级：允许 uncensored（旧表用 edited 表示破解），重建去掉旧 CHECK
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='subscription_quality_options'"
        ).fetchone()
        if row and "CHECK" in (row["sql"] or ""):
            self.conn.executescript("""
                BEGIN;
                ALTER TABLE subscription_quality_options RENAME TO subscription_quality_options_old;
                CREATE TABLE subscription_quality_options (
                    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                    quality         TEXT NOT NULL,
                    PRIMARY KEY (subscription_id, quality)
                );
                INSERT INTO subscription_quality_options (subscription_id, quality)
                    SELECT subscription_id, quality FROM subscription_quality_options_old;
                DROP TABLE subscription_quality_options_old;
                UPDATE subscription_quality_options SET quality='uncensored' WHERE quality='edited';
                COMMIT;
            """)

    def _seed(self) -> None:
        """初始化下载器优先级与 pan115 配置行。"""
        if self.conn.execute("SELECT COUNT(*) FROM downloader").fetchone()[0] == 0:
            for i, (name, kind) in enumerate([
                    ("115网盘", "pan115"), ("qBittorrent", "qbittorrent"),
                    ("Aria2", "aria2"), ("CloudDrive2", "clouddrive2"), ("迅雷", "xunlei")], start=1):
                self.conn.execute(
                    "INSERT INTO downloader (name, kind, enabled, sort_order) VALUES (?,?,?,?)",
                    (name, kind, 1 if kind == "pan115" else 0, i))
        self.conn.execute("INSERT OR IGNORE INTO pan115_config (id) VALUES (1)")
        self.conn.execute("INSERT OR IGNORE INTO clouddrive2_config (id) VALUES (1)")

    def close(self) -> None:
        self.conn.close()

    # ---- movies ----
    def upsert_movie(self, movie: dict) -> None:
        """movie 为 /v4/movies/{id} 返回的 data.movie（已归一化处理）。"""
        self.conn.execute(
            """
            INSERT INTO movies (
                id, number, title, origin_title, cover_url, thumb_url,
                duration, release_date, score, summary, review,
                director_id, director_name, maker_id, maker_name,
                publisher_id, publisher_name, series_id, series_name,
                tags, preview_images, preview_video_url, play_sources,
                magnets_count, reviews_count, comments_count,
                watched_count, want_watch_count,
                has_cnsub, has_preview_images, has_preview_video, can_play,
                type, number_letter, raw, fetched_at
            ) VALUES (
                :id, :number, :title, :origin_title, :cover_url, :thumb_url,
                :duration, :release_date, :score, :summary, :review,
                :director_id, :director_name, :maker_id, :maker_name,
                :publisher_id, :publisher_name, :series_id, :series_name,
                :tags, :preview_images, :preview_video_url, :play_sources,
                :magnets_count, :reviews_count, :comments_count,
                :watched_count, :want_watch_count,
                :has_cnsub, :has_preview_images, :has_preview_video, :can_play,
                :type, :number_letter, :raw, :fetched_at
            )
            ON CONFLICT(id) DO UPDATE SET
                number=excluded.number, title=excluded.title, origin_title=excluded.origin_title,
                cover_url=excluded.cover_url, thumb_url=excluded.thumb_url,
                duration=excluded.duration, release_date=excluded.release_date, score=excluded.score,
                summary=excluded.summary, review=excluded.review,
                director_id=excluded.director_id, director_name=excluded.director_name,
                maker_id=excluded.maker_id, maker_name=excluded.maker_name,
                publisher_id=excluded.publisher_id, publisher_name=excluded.publisher_name,
                series_id=excluded.series_id, series_name=excluded.series_name,
                tags=excluded.tags, preview_images=excluded.preview_images,
                preview_video_url=excluded.preview_video_url, play_sources=excluded.play_sources,
                magnets_count=excluded.magnets_count, reviews_count=excluded.reviews_count,
                comments_count=excluded.comments_count, watched_count=excluded.watched_count,
                want_watch_count=excluded.want_watch_count,
                has_cnsub=excluded.has_cnsub, has_preview_images=excluded.has_preview_images,
                has_preview_video=excluded.has_preview_video, can_play=excluded.can_play,
                type=excluded.type, number_letter=excluded.number_letter,
                raw=excluded.raw, fetched_at=excluded.fetched_at
            """,
            movie,
        )
        self.conn.commit()

    # ---- actors ----
    def upsert_actor(self, actor: dict) -> None:
        self.conn.execute(
            """
            INSERT INTO actors (id, name, gender, avatar_url)
            VALUES (:id, :name, :gender, :avatar_url)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, gender=excluded.gender, avatar_url=excluded.avatar_url
            """,
            actor,
        )

    def link_movie_actor(self, movie_id: str, actor_id: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO movie_actors (movie_id, actor_id) VALUES (?, ?)",
            (movie_id, actor_id),
        )

    # ---- reviews ----
    def upsert_review(self, review: dict) -> None:
        self.conn.execute(
            """
            INSERT INTO reviews (
                id, movie_id, user_id, username, score, content,
                status, status_title, watched_count, likes_count, liked, created_at
            ) VALUES (
                :id, :movie_id, :user_id, :username, :score, :content,
                :status, :status_title, :watched_count, :likes_count, :liked, :created_at
            )
            ON CONFLICT(id) DO UPDATE SET
                content=excluded.content, score=excluded.score,
                likes_count=excluded.likes_count, liked=excluded.liked,
                watched_count=excluded.watched_count
            """,
            review,
        )

    def reviews_by_user(self, user_id: str) -> list[sqlite3.Row]:
        """某个用户发布的所有评论（JOIN 影片信息），按评论时间倒序。

        用于「评论区分享」点击用户名后展示该用户分享过的链接所涉及的影片。
        """
        return self.conn.execute(
            """
            SELECT r.id, r.movie_id, r.user_id, r.username, r.content, r.created_at,
                   m.number, m.title, m.cover_url, m.thumb_url, m.release_date
            FROM reviews r
            LEFT JOIN movies m ON m.id = r.movie_id
            WHERE r.user_id = ? AND r.content IS NOT NULL
            ORDER BY r.created_at DESC
            """,
            (user_id,),
        ).fetchall()

    # ---- 关注分享者 ----
    def follow_user(self, user_id: int, username: str | None = None) -> None:
        """关注（或更新用户名后）关注某位分享者。幂等：重复关注不报错。"""
        self.conn.execute(
            """
            INSERT INTO follows (user_id, username, created_at) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET username=COALESCE(excluded.username, follows.username)
            """,
            (user_id, username, _now()),
        )
        self.conn.commit()

    def unfollow_user(self, user_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM follows WHERE user_id=?", (user_id,))
        self.conn.commit()
        return bool(cur.rowcount)

    def is_following(self, user_id: int) -> bool:
        row = self.conn.execute("SELECT 1 FROM follows WHERE user_id=?", (user_id,)).fetchone()
        return row is not None

    def list_followed_users(self) -> list[dict]:
        """我关注的所有分享者，附带各自在本地库分享过的影片（去重按片）数量。"""
        rows = self.conn.execute(
            """
            SELECT f.user_id, f.username, f.created_at,
                   (SELECT COUNT(DISTINCT r.movie_id)
                      FROM reviews r
                      WHERE r.user_id=f.user_id AND r.movie_id IS NOT NULL
                        AND (r.content LIKE '%magnet:%' OR r.content LIKE '%ed2k:%')) AS share_count
            FROM follows f
            ORDER BY f.created_at DESC, f.user_id DESC
            """
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            # username 优先用最新评论里的（若关注时用户名未记录）
            if not item["username"]:
                rr = self.conn.execute(
                    "SELECT username FROM reviews WHERE user_id=? AND username IS NOT NULL "
                    "ORDER BY created_at DESC LIMIT 1", (r["user_id"],)).fetchone()
                item["username"] = rr["username"] if rr else ""
            out.append(item)
        return out

    # ---- magnets ----
    def upsert_magnet(self, m: dict) -> None:
        self.conn.execute(
            """
            INSERT INTO magnets (
                btih, movie_id, code, name, size, date, magnet,
                has_hd, has_sub, source, fetched_at
            ) VALUES (
                :btih, :movie_id, :code, :name, :size, :date, :magnet,
                :has_hd, :has_sub, :source, :fetched_at
            )
            ON CONFLICT(btih) DO UPDATE SET
                name=excluded.name, size=excluded.size, date=excluded.date,
                magnet=excluded.magnet, has_hd=excluded.has_hd, has_sub=excluded.has_sub,
                code=excluded.code,
                movie_id=COALESCE(excluded.movie_id, magnets.movie_id)
            """,
            m,
        )

    def get_movie(self, movie_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM movies WHERE id=?", (movie_id,)).fetchone()

    def library_codes(self) -> set[str]:
        """媒体库中已入库影片的番号集合。"""
        return {r["code"] for r in self.conn.execute("SELECT DISTINCT code FROM library_items")}

    def mark_viewed(self, movie_id: str) -> None:
        self.conn.execute("UPDATE movies SET last_viewed_at=? WHERE id=?", (_now(), movie_id))
        self.conn.commit()

    def add_push(self, magnet: str, name: str | None = None, size: str | None = None,
                 movie_id: str | None = None, code: str | None = None,
                 downloader: str | None = None) -> int:
        """把推送请求写入 push_queue（待网盘功能消费）。"""
        self.conn.execute(
            "INSERT INTO push_queue (magnet, name, size, movie_id, code, status, downloader, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            (magnet, name, size, movie_id, code, downloader, _now()),
        )
        self.conn.commit()
        return self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def list_push(self, status: str = "pending", limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM push_queue WHERE status=? ORDER BY id DESC LIMIT ?",
            (status, limit),
        ).fetchall()

    def list_push_all(self, limit: int = 200) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM push_queue ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def list_push_records(self, status: str | None = None, keyword: str | None = None,
                          downloader: str | None = None, date_from: str | None = None,
                          date_to: str | None = None, limit: int = 200) -> list[sqlite3.Row]:
        """下载记录：push_queue 关联 movies 取封面/标题，支持筛选。"""
        sql = ("SELECT pq.*, m.cover_url, m.javbus_cover, m.title AS movie_title "
               "FROM push_queue pq LEFT JOIN movies m ON m.id = pq.movie_id WHERE 1=1")
        params = []
        if status:
            sql += " AND pq.status=?"
            params.append(status)
        if downloader:
            sql += " AND pq.downloader=?"
            params.append(downloader)
        if keyword:
            sql += (" AND (pq.code LIKE ? OR pq.name LIKE ? OR m.title LIKE ? OR pq.magnet LIKE ?)")
            like = f"%{keyword}%"
            params += [like, like, like, like]
        if date_from:
            sql += " AND pq.created_at >= ?"
            params.append(date_from)
        if date_to:
            sql += " AND pq.created_at <= ?"
            params.append(date_to + "T23:59:59")
        sql += " ORDER BY pq.id DESC LIMIT ?"
        params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def get_push(self, push_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM push_queue WHERE id=?", (push_id,)).fetchone()

    def set_push_status(self, push_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE push_queue SET status=?, pushed_at=? WHERE id=?",
            (status, _now() if status == "pushed" else None, push_id),
        )
        self.conn.commit()

    def pushed_magnet_set(self) -> set[str]:
        """返回所有『已推送』的磁链字符串集合，用于磁链卡打标。"""
        rows = self.conn.execute("SELECT magnet FROM push_queue WHERE status='pushed'").fetchall()
        return {r["magnet"] for r in rows}

    def delete_push(self, push_id: int) -> None:
        # 若该推送记录被订阅推送尝试引用，先解除外键（置空 push_id），否则删除会因外键约束失败。
        self.conn.execute(
            "UPDATE subscription_push_attempts SET push_id=NULL WHERE push_id=?", (push_id,)
        )
        self.conn.execute("DELETE FROM push_queue WHERE id=?", (push_id,))
        self.conn.commit()

    def has_push_code(self, code: str, downloader: str | None = None) -> bool:
        """是否存在「该番号 + 该下载器」的『已推送』记录，用于详情页防重复推送。

        仅当番号与下载器都相同时视为重复；番号相同但下载器不同（如 115 与 CloudDrive2
        各推一次）则允许再推。downloader 为空时退化为按番号判断。
        """
        if not code:
            return False
        if downloader:
            row = self.conn.execute(
                "SELECT 1 FROM push_queue WHERE code=? AND downloader=? AND status='pushed' LIMIT 1",
                (code, downloader),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT 1 FROM push_queue WHERE code=? AND status='pushed' LIMIT 1", (code,)
            ).fetchone()
        return row is not None

    # ---- pan115 配置 / 下载器优先级 ----
    def get_pan115_config(self) -> dict:
        row = self.conn.execute("SELECT * FROM pan115_config WHERE id=1").fetchone()
        return dict(row) if row else {
            "cookie": "", "timeout": 30, "quota": 0,
            "target_cid": "", "target_name": "", "enabled": 0,
        }

    def save_pan115_config(self, cfg: dict) -> None:
        self.conn.execute(
            """
            INSERT INTO pan115_config (id, cookie, timeout, quota, target_cid, target_name, enabled)
            VALUES (1, :cookie, :timeout, :quota, :target_cid, :target_name, :enabled)
            ON CONFLICT(id) DO UPDATE SET
                cookie=excluded.cookie, timeout=excluded.timeout, quota=excluded.quota,
                target_cid=excluded.target_cid, target_name=excluded.target_name,
                enabled=excluded.enabled
            """,
            cfg,
        )
        self.conn.commit()

    def get_clouddrive2_config(self) -> dict:
        row = self.conn.execute("SELECT * FROM clouddrive2_config WHERE id=1").fetchone()
        return dict(row) if row else {
            "host": "127.0.0.1", "rpc_port": 19798, "timeout": 30,
            "enable_ed2k": 0, "api_token": "", "user_name": "", "password": "",
            "save_path": "", "enabled": 0,
        }

    def save_clouddrive2_config(self, cfg: dict) -> None:
        self.conn.execute(
            """
            INSERT INTO clouddrive2_config (
                id, host, rpc_port, timeout, enable_ed2k, api_token,
                user_name, password, save_path, enabled
            )
            VALUES (1, :host, :rpc_port, :timeout, :enable_ed2k, :api_token,
                    :user_name, :password, :save_path, :enabled)
            ON CONFLICT(id) DO UPDATE SET
                host=excluded.host, rpc_port=excluded.rpc_port, timeout=excluded.timeout,
                enable_ed2k=excluded.enable_ed2k, api_token=excluded.api_token,
                user_name=excluded.user_name, password=excluded.password,
                save_path=excluded.save_path, enabled=excluded.enabled
            """,
            cfg,
        )
        self.conn.commit()

    def list_downloaders(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM downloader ORDER BY enabled DESC, sort_order ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def update_downloader(self, did: int, enabled: int | None = None,
                          sort_order: int | None = None) -> None:
        sets, params = [], []
        if enabled is not None:
            sets.append("enabled=?")
            params.append(int(enabled))
        if sort_order is not None:
            sets.append("sort_order=?")
            params.append(int(sort_order))
        if not sets:
            return
        params.append(did)
        self.conn.execute(f"UPDATE downloader SET {', '.join(sets)} WHERE id=?", params)
        self.conn.commit()

    def renumber_downloaders(self) -> None:
        """按显示顺序（启用在前，禁用在后）重排 sort_order 为 1..N。"""
        rows = self.conn.execute(
            "SELECT id FROM downloader ORDER BY enabled DESC, sort_order ASC"
        ).fetchall()
        for i, r in enumerate(rows, start=1):
            self.conn.execute("UPDATE downloader SET sort_order=? WHERE id=?", (i, r["id"]))
        self.conn.commit()

    def set_javbus_cover(self, movie_id: str, url: str) -> None:
        self.conn.execute("UPDATE movies SET javbus_cover=? WHERE id=?", (url, movie_id))

    def set_preview_images(self, movie_id: str, urls: list[str]) -> None:
        items = [{"large": u, "thumb": ""} for u in urls]
        self.conn.execute(
            "UPDATE movies SET preview_images=?, has_preview_images=? WHERE id=?",
            (json.dumps(items, ensure_ascii=False), 1 if urls else 0, movie_id),
        )

    # ---- media servers ----
    def add_server(self, name: str, url: str, api_key: str, type_: str) -> int:
        self.conn.execute(
            """
            INSERT INTO media_servers (name, url, api_key, type) VALUES (?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET name=excluded.name, api_key=excluded.api_key, type=excluded.type
            """,
            (name, url.rstrip("/"), api_key, type_),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT id FROM media_servers WHERE url=?", (url.rstrip("/"),)).fetchone()
        return row["id"]

    def list_servers(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM media_servers ORDER BY id").fetchall()

    def get_servers(self) -> list[dict]:
        return [dict(r) for r in self.list_servers()]

    def remove_server(self, server_id: int) -> None:
        self.conn.execute("DELETE FROM library_items WHERE server_id=?", (server_id,))
        self.conn.execute("DELETE FROM media_servers WHERE id=?", (server_id,))
        self.conn.commit()

    # ---- library items ----
    def clear_library(self, server_id: int) -> None:
        self.conn.execute("DELETE FROM library_items WHERE server_id=?", (server_id,))

    def upsert_library_item(self, server_id: int, code: str, item: dict) -> None:
        self.conn.execute(
            """
            INSERT INTO library_items (item_id, server_id, code, title, path, synced_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(server_id, item_id) DO UPDATE SET
                code=excluded.code, title=excluded.title, path=excluded.path, synced_at=excluded.synced_at
            """,
            (item.get("Id"), server_id, code, item.get("Name"), item.get("Path"), _now()),
        )

    def lookup_library(self, code: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT li.code, li.title, li.item_id, s.name AS server_name, s.type AS server_type, s.url AS server_url
            FROM library_items li JOIN media_servers s ON s.id = li.server_id
            WHERE li.code = ?
            """,
            (code.upper(),),
        ).fetchall()

    def library_stream_lookup(self, code: str) -> list[dict]:
        """内部专用：按番号查出所有命中的媒体条目（含 api_key / server_id）。

        警告：返回 api_key，仅限后端接口（如 /api/movie/<id>/play）组装流地址使用，
        严禁注入模板（模板层用 lookup_library，已隔离 api_key）。
        """
        if not code:
            return []
        rows = self.conn.execute(
            """
            SELECT li.item_id, li.title,
                   s.id      AS server_id,
                   s.name    AS server_name,
                   s.type    AS server_type,
                   s.url     AS server_url,
                   s.api_key
            FROM library_items li JOIN media_servers s ON s.id = li.server_id
            WHERE li.code = ?
            """,
            (code.upper(),),
        ).fetchall()
        return [dict(r) for r in rows]

    def movie_magnets(self, movie_id: str | None = None,
                      code: str | None = None) -> list[sqlite3.Row]:
        if movie_id:
            return self.conn.execute(
                "SELECT * FROM magnets WHERE movie_id=? ORDER BY date DESC, btih", (movie_id,)
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM magnets WHERE code=? ORDER BY date DESC, btih", (code,)
        ).fetchall()

    def movie_actor_ids(self, movie_id: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT actor_id FROM movie_actors WHERE movie_id=?", (movie_id,)
        ).fetchall()
        return {r["actor_id"] for r in rows}

    # ---- subscriptions ----
    def create_subscription(self, data: dict, qualities: list[str]) -> int:
        now = _now()
        cur = self.conn.execute(
            """
            INSERT INTO subscriptions (
                target_type, target_id, target_key, target_name, target_url,
                status, download_mode, pre_download, min_size_mb, max_size_mb,
                max_file_count, release_date_from, release_date_to,
                expiry_days, categories, exclude_categories, enabled, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (data["target_type"], data.get("target_id"), data["target_key"],
             data["target_name"], data.get("target_url"), data["download_mode"],
             int(data.get("pre_download", 0)), data.get("min_size_mb"),
             data.get("max_size_mb"), data.get("max_file_count"),
             data.get("release_date_from"), data.get("release_date_to"),
             data.get("expiry_days"),
             json.dumps(data.get("categories") or [], ensure_ascii=False),
             json.dumps(data.get("exclude_categories") or [], ensure_ascii=False), now, now),
        )
        sid = int(cur.lastrowid)
        self._replace_subscription_qualities(sid, qualities)
        self.conn.execute(
            "INSERT INTO subscription_priority_options (subscription_id) VALUES (?)", (sid,)
        )
        self.conn.commit()
        return sid

    def _replace_subscription_qualities(self, sid: int, qualities: list[str]) -> None:
        self.conn.execute(
            "DELETE FROM subscription_quality_options WHERE subscription_id=?", (sid,)
        )
        for quality in qualities:
            self.conn.execute(
                "INSERT INTO subscription_quality_options (subscription_id, quality) VALUES (?, ?)",
                (sid, quality),
            )

    def update_subscription(self, sid: int, data: dict, qualities: list[str]) -> bool:
        cur = self.conn.execute(
            """
            UPDATE subscriptions SET target_name=?, target_url=?, download_mode=?,
                pre_download=?, min_size_mb=?, max_size_mb=?, max_file_count=?,
                release_date_from=?, release_date_to=?, expiry_days=?, categories=?,
                exclude_categories=?, updated_at=?
            WHERE id=?
            """,
            (data["target_name"], data.get("target_url"), data["download_mode"],
             int(data.get("pre_download", 0)), data.get("min_size_mb"),
             data.get("max_size_mb"), data.get("max_file_count"),
             data.get("release_date_from"), data.get("release_date_to"),
             data.get("expiry_days"),
             json.dumps(data.get("categories") or [], ensure_ascii=False),
             json.dumps(data.get("exclude_categories") or [], ensure_ascii=False), _now(), sid),
        )
        if cur.rowcount:
            self._replace_subscription_qualities(sid, qualities)
            self.conn.commit()
            return True
        return False

    def get_subscription(self, sid: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM subscriptions WHERE id=?", (sid,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["qualities"] = [r["quality"] for r in self.conn.execute(
            "SELECT quality FROM subscription_quality_options WHERE subscription_id=? ORDER BY quality",
            (sid,),
        ).fetchall()]
        priority = self.conn.execute(
            "SELECT priority_order, score_version FROM subscription_priority_options WHERE subscription_id=?",
            (sid,),
        ).fetchone()
        item["priority_order"] = json.loads(priority["priority_order"]) if priority else ["hd", "uncensored", "size"]
        item["score_version"] = priority["score_version"] if priority else "v1"
        item["categories"] = json.loads(item.get("categories") or "[]")
        item["exclude_categories"] = json.loads(item.get("exclude_categories") or "[]")
        return item

    def subscription_by_target(self, target_type: str, target_key: str) -> dict | None:
        row = self.conn.execute(
            "SELECT id FROM subscriptions WHERE target_type=? AND target_key=?",
            (target_type, target_key),
        ).fetchone()
        return self.get_subscription(row["id"]) if row else None

    def list_subscriptions(self, view: str = "pending", limit: int = 24,
                           offset: int = 0) -> list[dict]:
        where = {
            "pending": "s.target_type='movie' AND s.status IN ('active','paused')",
            "completed": "s.target_type='movie' AND s.status='completed'",
            "online": "s.target_type='online'",
            "actors": "s.target_type='actor'",
            "lists": "s.target_type='list'",
        }.get(view, "s.target_type='movie' AND s.status IN ('active','paused')")
        rows = self.conn.execute(
            f"""
            SELECT s.*,
              (SELECT COUNT(*) FROM subscription_check_runs r WHERE r.subscription_id=s.id) AS run_count,
              (SELECT COUNT(*) FROM subscription_push_attempts p WHERE p.subscription_id=s.id AND p.status='succeeded') AS push_count
            FROM subscriptions s WHERE {where}
            ORDER BY COALESCE(s.updated_at, s.created_at) DESC LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        result = []
        for row in rows:
            item = self.get_subscription(row["id"])
            item["run_count"] = row["run_count"]
            item["push_count"] = row["push_count"]
            result.append(item)
        return result

    def count_subscriptions(self, view: str = "pending") -> int:
        where = {
            "pending": "target_type='movie' AND status IN ('active','paused')",
            "completed": "target_type='movie' AND status='completed'",
            "online": "target_type='online'",
            "actors": "target_type='actor'",
            "lists": "target_type='list'",
        }.get(view, "target_type='movie' AND status IN ('active','paused')")
        return int(self.conn.execute(f"SELECT COUNT(*) FROM subscriptions WHERE {where}").fetchone()[0])

    def subscribed_movie_ids(self, ids: list[str]) -> list[str]:
        """返回给定影片 id 中，已被订阅的（任意状态，用于卡片订阅态展示）。"""
        ids = [str(i) for i in ids if i]
        if not ids:
            return []
        ph = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT DISTINCT target_id FROM subscriptions WHERE target_type='movie' AND target_id IN ({ph})",
            ids,
        ).fetchall()
        return [r["target_id"] for r in rows]

    def delete_subscription_by_target(self, target_type: str, target_id: str) -> bool:
        row = self.conn.execute(
            "SELECT id FROM subscriptions WHERE target_type=? AND target_id=?",
            (target_type, target_id),
        ).fetchone()
        if not row:
            return False
        self.conn.execute("DELETE FROM subscriptions WHERE id=?", (row["id"],))
        self.conn.commit()
        return True

    def subscription_counts(self) -> dict:
        return {
            "pending": self.count_subscriptions("pending"),
            # 已完成：影片/在线/演员/清单各订阅推送成功的影片数之和
            "completed": self.count_completed_push_items(),
            "online": self.count_subscriptions("online"),
            "actors": self.count_subscriptions("actors"),
            "lists": self.count_subscriptions("lists"),
            "blacklist": int(self.conn.execute("SELECT COUNT(*) FROM subscription_blacklist").fetchone()[0]),
        }

    def set_subscription_status(self, sid: int, status: str) -> bool:
        completed = _now() if status == "completed" else None
        cur = self.conn.execute(
            "UPDATE subscriptions SET status=?, enabled=?, completed_at=?, updated_at=? WHERE id=?",
            (status, 0 if status == "paused" else 1, completed, _now(), sid),
        )
        self.conn.commit()
        return bool(cur.rowcount)

    def set_subscription_error(self, sid: int, error: str | None) -> None:
        self.conn.execute(
            "UPDATE subscriptions SET last_error=?, last_checked_at=?, updated_at=? WHERE id=?",
            (error, _now(), _now(), sid),
        )
        self.conn.commit()

    def delete_subscription(self, sid: int) -> bool:
        cur = self.conn.execute("DELETE FROM subscriptions WHERE id=?", (sid,))
        self.conn.commit()
        return bool(cur.rowcount)

    def create_subscription_run(self, sid: int, trigger: str, matcher_version: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO subscription_check_runs (subscription_id, trigger_type, status, matcher_version, started_at) "
            "VALUES (?, ?, 'running', ?, ?)",
            (sid, trigger, matcher_version, _now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_subscription_run(self, run_id: int, status: str, matched: int = 0,
                                rejected: int = 0, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE subscription_check_runs SET status=?, matched_count=?, rejected_count=?, "
            "error_message=?, completed_at=? WHERE id=?",
            (status, matched, rejected, error, _now(), run_id),
        )
        self.conn.commit()

    def add_subscription_candidate(self, data: dict) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO subscription_candidates (
                check_run_id, subscription_id, movie_id, magnet_id, magnet_hash,
                magnet_name, magnet_uri, size_text, size_bytes, file_count,
                release_date, quality_tags, resource_fingerprint, resource_score,
                matched, push_ok, predownload, rejection_reasons, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (data["check_run_id"], data["subscription_id"], data.get("movie_id"),
             data.get("magnet_id"), data.get("magnet_hash"), data["magnet_name"],
             data["magnet_uri"], data.get("size_text"), data.get("size_bytes"),
             data.get("file_count"), data.get("release_date"),
             json.dumps(data.get("quality_tags") or [], ensure_ascii=False),
             data["resource_fingerprint"], json.dumps(data["resource_score"]),
             int(data.get("matched", 0)), int(data.get("push_ok", 0)), int(data.get("predownload", 0)),
             json.dumps(data.get("rejection_reasons") or [], ensure_ascii=False), _now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def set_subscription_matched_count(self, sid: int, count: int) -> None:
        self.conn.execute("UPDATE subscriptions SET matched_count=? WHERE id=?", (count, sid))
        self.conn.commit()

    def get_subscription_candidate(self, cid: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM subscription_candidates WHERE id=?", (cid,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["quality_tags"] = json.loads(item["quality_tags"] or "[]")
        item["resource_score"] = json.loads(item["resource_score"] or "[]")
        item["rejection_reasons"] = json.loads(item["rejection_reasons"] or "[]")
        return item

    def list_subscription_candidates(self, run_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id FROM subscription_candidates WHERE check_run_id=?", (run_id,)
        ).fetchall()
        items = [self.get_subscription_candidate(r["id"]) for r in rows]
        return sorted(items, key=lambda item: (
            int(bool(item["matched"])), int(bool(item["predownload"])),
            tuple(item["resource_score"]), -item["id"]), reverse=True)

    def subscription_actor_movies(self, sid: int, actor_id: str) -> list[dict]:
        """返回演员订阅对应的全部影片，并标注每部的订阅状态、
        是否预下载候选，用于 8.png 弹窗。"""
        rows = self.conn.execute(
            """
            SELECT m.id, m.number, m.title, m.origin_title, m.cover_url,
                   m.javbus_cover, m.release_date
            FROM movies m JOIN movie_actors ma ON ma.movie_id=m.id
            WHERE ma.actor_id=? ORDER BY m.release_date DESC, m.id
            """,
            (actor_id,),
        ).fetchall()
        pushed = self.conn.execute(
            "SELECT DISTINCT c.movie_id FROM subscription_push_attempts p "
            "JOIN subscription_candidates c ON c.id=p.candidate_id "
            "WHERE p.subscription_id=? AND p.status='succeeded'", (sid,)
        ).fetchall()
        pushed_ids = {r["movie_id"] for r in pushed}
        cands = self.conn.execute(
            "SELECT movie_id, matched, predownload FROM subscription_candidates WHERE subscription_id=?",
            (sid,),
        ).fetchall()
        grouped: dict[str, list[dict]] = {}
        for c in cands:
            grouped.setdefault(c["movie_id"], []).append(dict(c))

        out = []
        for r in rows:
            item = dict(r)
            movie_cands = grouped.get(item["id"], [])
            predownload = 1 if any(c["predownload"] for c in movie_cands) else 0
            if item["id"] in pushed_ids:
                status = "completed"
            elif movie_cands and not any(c["matched"] or c["predownload"] for c in movie_cands):
                status = "skipped"
            else:
                status = "active"
            item["sub_status"] = status
            item["predownload"] = predownload
            item["cover"] = item.get("cover_url") or item.get("javbus_cover") or ""
            item["title"] = item.get("title") or item.get("origin_title") or ""
            item.pop("cover_url", None); item.pop("javbus_cover", None)
            out.append(item)
        return out

    def completed_push_items(self, limit: int = 20, offset: int = 0) -> list[dict]:
        """已完成：所有订阅中成功推送到网盘的影片（跨影片/演员/清单/在线来源）。"""
        rows = self.conn.execute(
            """
            SELECT p.id AS attempt_id, p.finished_at, c.movie_id, c.magnet_name, c.size_text,
                   s.id AS sub_id, s.target_type, s.target_name
            FROM subscription_push_attempts p
            JOIN subscription_candidates c ON c.id=p.candidate_id
            JOIN subscriptions s ON s.id=p.subscription_id
            WHERE p.status='succeeded'
            ORDER BY p.finished_at DESC, p.id DESC LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            movie = self.get_movie(item["movie_id"]) if item["movie_id"] else None
            item["cover"] = (movie["cover_url"] or movie["javbus_cover"]) if movie else ""
            item["number"] = movie["number"] if movie else ""
            item["title"] = (movie["title"] or movie["origin_title"]) if movie else item["target_name"]
            item["date_str"] = (item["finished_at"] or "")[:16].replace("T", " ")
            out.append(item)
        return out

    def count_completed_push_items(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM subscription_push_attempts WHERE status='succeeded'").fetchone()[0])

    # ---- 清单影片缓存（避免每次打开清单影片弹窗都调 JAVDB）----
    def replace_list_movies(self, list_id: str, movie_ids: list[str]) -> None:
        self.conn.execute("DELETE FROM list_movies WHERE list_id=?", (list_id,))
        for mid in movie_ids:
            self.conn.execute(
                "INSERT OR IGNORE INTO list_movies (list_id, movie_id, synced_at) VALUES (?, ?, ?)",
                (list_id, mid, _now()),
            )
        self.conn.commit()

    def list_movie_ids(self, list_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT movie_id FROM list_movies WHERE list_id=? ORDER BY movie_id", (list_id,)
        ).fetchall()
        return [r["movie_id"] for r in rows]

    # ---- subscription skips（用户点击“跳过”的影片） ----
    def add_skip(self, subscription_id: int, movie_id: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO subscription_skips (subscription_id, movie_id, created_at) VALUES (?, ?, ?)",
            (subscription_id, movie_id, _now()),
        )
        self.conn.commit()

    def remove_skip(self, subscription_id: int, movie_id: str) -> None:
        self.conn.execute(
            "DELETE FROM subscription_skips WHERE subscription_id=? AND movie_id=?",
            (subscription_id, movie_id),
        )
        self.conn.commit()

    def skipped_set(self, subscription_id: int) -> set[str]:
        rows = self.conn.execute(
            "SELECT movie_id FROM subscription_skips WHERE subscription_id=?", (subscription_id,)
        ).fetchall()
        return {r["movie_id"] for r in rows}

    def best_successful_score(self, sid: int) -> list | None:
        rows = self.conn.execute(
            """
            SELECT c.resource_score FROM subscription_push_attempts p
            JOIN subscription_candidates c ON c.id=p.candidate_id
            WHERE p.subscription_id=? AND p.status='succeeded'
            """,
            (sid,),
        ).fetchall()
        scores = [json.loads(row["resource_score"]) for row in rows]
        return max(scores, key=tuple) if scores else None

    def create_push_attempt(self, sid: int, candidate_id: int, key: str,
                            status: str = "queued", push_id: int | None = None,
                            error: str | None = None) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO subscription_push_attempts (
                subscription_id, candidate_id, push_id, idempotency_key, status,
                error_message, requested_at, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (sid, candidate_id, push_id, key, status, error, _now(),
             _now() if status in ("running", "succeeded", "failed") else None,
             _now() if status in ("succeeded", "failed") else None),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def mark_subscription_push(self, attempt_id: int, status: str,
                               push_id: int | None = None, error: str | None = None) -> None:
        row = self.conn.execute(
            "SELECT subscription_id FROM subscription_push_attempts WHERE id=?", (attempt_id,)
        ).fetchone()
        if not row:
            return
        self.conn.execute(
            "UPDATE subscription_push_attempts SET status=?, push_id=COALESCE(?, push_id), "
            "error_message=?, started_at=COALESCE(started_at, ?), finished_at=? WHERE id=?",
            (status, push_id, error, _now(), _now() if status in ("succeeded", "failed") else None,
             attempt_id),
        )
        if status == "succeeded":
            # 影片订阅：单部影片推送成功即整体“已完成”；
            # 演员/清单/在线订阅是长期规则，保持“订阅中”，只记录该影片推送成功。
            sub_type = self.conn.execute(
                "SELECT target_type FROM subscriptions WHERE id=?",
                (row["subscription_id"],),
            ).fetchone()
            is_movie = bool(sub_type and sub_type["target_type"] == "movie")
            self.conn.execute(
                "UPDATE subscriptions SET status=?, last_successful_push_at=?, "
                "completed_at=COALESCE(completed_at, ?), last_error=NULL, updated_at=? WHERE id=?",
                ("completed" if is_movie else "active", _now(),
                 _now() if is_movie else None, _now(), row["subscription_id"]),
            )
        elif status == "failed":
            self.conn.execute(
                "UPDATE subscriptions SET last_error=?, updated_at=? WHERE id=?",
                (error, _now(), row["subscription_id"]),
            )
        self.conn.commit()

    def mark_push_info_hash(self, attempt_id: int, info_hash: str) -> None:
        self.conn.execute(
            "UPDATE subscription_push_attempts SET info_hash=? WHERE id=?",
            (info_hash, attempt_id),
        )
        self.conn.commit()

    def list_verify_pending(self) -> list[dict]:
        """待 115 验证下载结果的自动推送尝试（已提交、还没确认成败）。"""
        rows = self.conn.execute(
            "SELECT * FROM subscription_push_attempts WHERE status='running' AND info_hash IS NOT NULL "
            "ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_candidate_attempted(self, candidate_id: int) -> None:
        self.conn.execute("UPDATE subscription_candidates SET attempted=1 WHERE id=?", (candidate_id,))
        self.conn.commit()

    def untried_matched_candidates(self, subscription_id: int,
                                   require_push_ok: bool = True) -> list[dict]:
        """已命中的候选里，尚未提交过 115 的那些（自动推送重试时换下一颗）。

        require_push_ok=True：仅 matched=1 且 push_ok=1（质量/大小/文件数满足）；
        False：仅 matched=1（预下载模式放宽，允许推非 push_ok 的 predownload 候选）。
        均按 info_hash 去重。
        """
        push_cond = "AND c.push_ok=1" if require_push_ok else ""
        rows = self.conn.execute(
            f"""
            SELECT c.id FROM subscription_candidates c
            WHERE c.subscription_id=:sid AND c.matched=1 {push_cond}
              AND c.magnet_hash NOT IN (
                  SELECT COALESCE(p.info_hash, c2.magnet_hash)
                  FROM subscription_push_attempts p
                  JOIN subscription_candidates c2 ON c2.id=p.candidate_id
                  WHERE p.subscription_id=:sid AND COALESCE(p.info_hash, c2.magnet_hash) IS NOT NULL
              )
            ORDER BY c.id
            """,
            {"sid": subscription_id},
        ).fetchall()
        # 按磁链 info_hash 去重，每个磁链只保留一个代表（分数最高者）
        best = {}
        for r in rows:
            c = self.get_subscription_candidate(r["id"])
            h = c.get("magnet_hash") or c.get("magnet_uri") or ""
            if h not in best or tuple(c["resource_score"]) > tuple(best[h]["resource_score"]):
                best[h] = c
        return list(best.values())

    def list_subscription_pushes(self, sid: int) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT p.*, c.magnet_name, c.size_text, c.quality_tags, c.resource_score
            FROM subscription_push_attempts p JOIN subscription_candidates c ON c.id=p.candidate_id
            WHERE p.subscription_id=? ORDER BY p.id DESC
            """,
            (sid,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- subscription blacklist ----
    def add_blacklist(self, data: dict) -> int:
        now = _now()
        cur = self.conn.execute(
            "INSERT INTO subscription_blacklist (target_type, target_id, target_key, target_name, reason, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (data["target_type"], data.get("target_id"), data["target_key"],
             data["target_name"], data.get("reason"), now, now),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_blacklist(self, target_type: str | None = None, limit: int = 100,
                       offset: int = 0) -> list[dict]:
        if target_type:
            rows = self.conn.execute(
                "SELECT * FROM subscription_blacklist WHERE target_type=? ORDER BY id DESC LIMIT ? OFFSET ?",
                (target_type, limit, offset),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM subscription_blacklist ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]

    def blacklist_match(self, target_type: str, target_key: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM subscription_blacklist WHERE target_type=? AND target_key=?",
            (target_type, target_key),
        ).fetchone()
        return dict(row) if row else None

    def delete_blacklist(self, bid: int) -> bool:
        cur = self.conn.execute("DELETE FROM subscription_blacklist WHERE id=?", (bid,))
        self.conn.commit()
        return bool(cur.rowcount)

    def commit(self) -> None:
        self.conn.commit()

    # ---- 查询辅助 ----
    def has_movie(self, movie_id: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM movies WHERE id=?", (movie_id,)).fetchone()
        return row is not None

    def movie_by_number(self, number: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM movies WHERE number=? ORDER BY release_date DESC LIMIT 1", (number,)
        ).fetchone()

    def counts(self) -> dict:
        out = {}
        for table in ("movies", "actors", "movie_actors", "reviews", "magnets",
                      "media_servers", "library_items"):
            out[table] = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return out
