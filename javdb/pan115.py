"""115 网盘客户端封装（基于 p115client）。

调用方式参照 115-auto 项目 pan115.py：
- P115Client(cookie) 初始化
- c.fs_files({"cid": ..., "limit": ...})      列目录
- c.clouddownload_task_add_url({"url": 磁链, "wp_path_id": cid})  加离线下载
"""
from __future__ import annotations


class Pan115Error(Exception):
    pass


def _client(cookie: str | None):
    if not cookie:
        raise Pan115Error("未配置 115 Cookie")
    try:
        from p115client import P115Client
    except ImportError as e:
        raise Pan115Error("未安装 p115client，请 pip install p115client") from e
    try:
        return P115Client(cookie)
    except Exception as e:
        raise Pan115Error(f"115 客户端初始化失败: {e}") from e


def test_connection(cookie: str | None) -> tuple[bool, str]:
    """校验 Cookie 是否可用。"""
    try:
        c = _client(cookie)
        r = c.fs_files({"cid": 0, "limit": 1})
        if isinstance(r, dict) and r.get("state"):
            return True, "连接正常"
        return False, (r.get("error") if isinstance(r, dict) else "Cookie 无效")
    except Pan115Error as e:
        return False, str(e)
    except Exception as e:
        return False, f"连接异常: {e}"


def list_dirs(cookie: str | None, cid: str = "0", limit: int = 500) -> list[dict]:
    """列出某目录下的文件夹，供下载目录选择器使用。"""
    c = _client(cookie)
    try:
        r = c.fs_files({"cid": int(cid or "0"), "limit": limit})
    except AttributeError:
        r = c.fs_files(int(cid or "0"), limit=limit)
    except (TypeError, ValueError):
        r = c.fs_files(fid=int(cid or "0"), limit=limit)

    data = (r.get("data") if isinstance(r, dict) else None) or []
    out = []
    for f in data:
        if not isinstance(f, dict):
            continue
        size = int(f.get("fs") or f.get("s") or 0)
        if size != 0:  # 只保留文件夹（115 文件夹大小=0）
            continue
        name = f.get("n") or f.get("fn") or f.get("file_name") or f.get("name") or ""
        cid2 = str(f.get("cid") or f.get("fid") or f.get("file_id") or "")
        if not cid2:
            continue
        out.append({"cid": cid2, "name": name})
    return out


def add_magnet(cookie: str | None, magnet: str, cid: str) -> dict:
    """把磁力链接加为 115 离线下载，保存到指定目录 cid。"""
    c = _client(cookie)
    payload = {"url": magnet.strip(), "wp_path_id": str(cid or "0")}
    try:
        r = c.clouddownload_task_add_url(payload)
    except TypeError:
        r = c.clouddownload_task_add_url(payload["url"], wp_path_id=payload["wp_path_id"])
    if isinstance(r, dict) and not r.get("state", True):
        err = r.get("error") or r.get("error_msg") or "未知错误"
        raise Pan115Error(str(err))
    return r if isinstance(r, dict) else {"state": True, "data": r}
