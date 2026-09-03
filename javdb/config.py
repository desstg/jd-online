"""配置管理：config.json 的加载 / 保存与默认值。

设置页（网页界面）会把用户填入的 Emby API Key、JAVDB 账号密码、网络代理等
写进这里；媒体服务器（Emby/Jellyfin）本体存 SQLite 的 media_servers 表。
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

# 允许用环境变量覆盖的配置项（容器 / Docker 部署常见需求；未设置则忽略）。
# 键 = config 字段，值 = 环境变量名。
ENV_OVERRIDES = {
    "web_username": "JD_WEB_USERNAME",
    "web_password": "JD_WEB_PASSWORD",
}

DEFAULTS: dict = {
    "web_username": "123",
    "web_password": "abc123",
    "secret_key": "",
    "javdb_username": "",
    "javdb_password": "",
    "javdb_token": "",
    "proxy": "",
    "image_route": "auto",
    "api_base": "https://jdforrepam.com/api",
    "image_cdn": "c0.jdbstatic.com",
    "javbus_base": "https://www.javbus.com",
    "min_interval": 0.5,
    "sync_cron": "0 */6 * * *",
    "db_path": "javdb.db",
    "port": 9091,
    "host": "0.0.0.0",
    "default_player": "",
    # 订阅调度（设置页「订阅配置」，18.png）
    "sub_check_enabled": False,
    "sub_daily_times": [],
    "sub_check_interval": 120,
    "sub_concurrency": 2,
    "sub_interval_min": 3,
    "sub_interval_max": 10,
    "sub_timeout": 30,
    "sub_retry_enabled": True,
    "sub_sync_enabled": False,
    "sub_sync_times": ["08:00", "20:00"],
}

# JAVDB 移动端 API 镜像节点（设置页可切换）
API_NODES: list[tuple[str, str]] = [
    ("jdforrepam.com", "https://jdforrepam.com/api"),
    ("apidd.spthgb.com", "https://apidd.spthgb.com/api"),
    ("apidd.czssdgz.com", "https://apidd.czssdgz.com/api"),
]


def load(path: str = "config.json") -> dict:
    cfg = dict(DEFAULTS)
    p = Path(path)
    if p.exists():
        try:
            cfg.update(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    # 环境变量优先于 config.json / 默认值（容器部署时用 compose 注入）
    for key, env in ENV_OVERRIDES.items():
        val = os.environ.get(env)
        if val is not None:
            cfg[key] = val
    if not cfg.get("secret_key"):
        cfg["secret_key"] = secrets.token_hex(32)
        save(cfg, path)
    return cfg


def save(cfg: dict, path: str = "config.json") -> None:
    Path(path).write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def editable_keys() -> tuple[str, ...]:
    """设置页允许用户修改的字段。"""
    return (
        "javdb_username", "javdb_password", "javdb_token",
        "proxy", "image_route", "api_base", "javbus_base", "min_interval",
        "web_username", "web_password", "host", "port", "default_player",
        "sub_check_enabled", "sub_daily_times", "sub_check_interval", "sub_concurrency",
        "sub_interval_min", "sub_interval_max", "sub_timeout", "sub_retry_enabled",
        "sub_sync_enabled", "sub_sync_times",
    )
