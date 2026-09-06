"""aria2 客户端封装（基于 aria2 的 JSON-RPC，HTTP POST）。

aria2 的 RPC 端点是 `http(s)://<host>:<port>/jsonrpc`，
- secret 通过 `{"token": "<secret>"}`（或 params 第一个元素 `token:<secret>`）鉴权；
- 加磁链调用 `aria2.addUri`，可带 `{"dir": "<保存目录>"}` 选项。
"""
from __future__ import annotations

import requests


class Aria2Error(Exception):
    pass


def endpoint(host: str, port: int, https: bool = False) -> str:
    scheme = "https" if https else "http"
    netloc = host or "127.0.0.1"
    port = int(port or 6801)
    # 已是带端口/路径的地址则直接使用
    if "://" in netloc:
        return f"{scheme}://{netloc}"
    return f"{scheme}://{netloc}:{port}/jsonrpc"


def _rpc(host: str, port: int, secret: str | None, method: str, params: list,
         timeout: int = 30, https: bool = False):
    url = endpoint(host, port, https)
    payload = {
        "jsonrpc": "2.0",
        "id": "javdb",
        "method": method,
        "params": params,
    }
    try:
        resp = requests.post(url, json=payload, timeout=int(timeout or 30))
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        raise Aria2Error(f"无法连接 aria2（{url}）：{exc}") from exc
    except ValueError as exc:
        raise Aria2Error(f"aria2 返回非 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise Aria2Error(f"aria2 返回异常：{data!r}")
    if "error" in data:
        err = data["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        code = err.get("code") if isinstance(err, dict) else ""
        raise Aria2Error(f"aria2 错误[{code}]: {msg}")
    return data.get("result")


def add_magnet(host: str, port: int, secret: str | None, magnet: str,
               path: str = "", timeout: int = 30, https: bool = False) -> str:
    """把磁链加入 aria2 下载。返回任务 gid。"""
    magnet = (magnet or "").strip()
    if not magnet:
        raise Aria2Error("缺少磁链")
    params: list = [[magnet]]
    if (path or "").strip():
        params.append({"dir": (path or "").strip()})
    if secret:
        params.insert(0, f"token:{secret}")
    return _rpc(host, port, secret, "aria2.addUri", params, timeout, https)


def test_connection(host: str, port: int, secret: str | None,
                    timeout: int = 30, https: bool = False) -> dict:
    """测试 aria2 连通性并读取版本信息。"""
    params = [f"token:{secret}"] if secret else []
    version = _rpc(host, port, secret, "aria2.getVersion", params, timeout, https)
    if isinstance(version, dict):
        return {"ok": True, "version": version.get("version")}
    return {"ok": True, "version": version}
