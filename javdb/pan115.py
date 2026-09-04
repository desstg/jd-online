"""115 网盘客户端封装（基于 p115client）。

调用方式参照 115-auto 项目 pan115.py：
- P115Client(cookie) 初始化
- c.fs_files({"cid": ..., "limit": ...})      列目录
- c.clouddownload_task_add_url({"url": 磁链, "wp_path_id": cid})  加离线下载
- 扫码登录（见文末 start/poll/finish_qrcode_login）
"""
from __future__ import annotations

import base64

import requests


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


# ---------------------------------------------------------------- 扫码登录（可切换客户端）
# 流程：login_qrcode_token 拿 uid → 取二维码 PNG（base64 内嵌）→ 轮询登录状态
#       → login_qrcode_scan_result(uid, app=所选客户端) 拿到对应客户端的 Cookie
# app 决定了扫码成功后保存哪个客户端的 Cookie；二维码 token/图片固定用 web 端生成。
_QR_STATUS_MSG: dict[int, str] = {
    0: "等待扫码…",
    1: "已扫码，请在手机上确认…",
    2: "登录成功",
    -1: "二维码已过期，请点击刷新",
    -2: "已取消扫码",
}


def _fetch_qr_image(uid: str, app: str = "web") -> bytes:
    """下载二维码 PNG。不用 p115client 的 login_qrcode()（它忽略 payload）。"""
    url = f"https://qrcodeapi.115.com/api/1.0/{app}/1.0/qrcode?uid={uid}"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return r.content
    except Exception as e:
        raise Pan115Error(f"获取二维码图片失败: {e}") from e


def start_qrcode_login() -> dict:
    """获取登录二维码，返回 {uid, token, qr}，qr 为 base64 data-URI PNG。"""
    try:
        from p115client import P115Client
    except ImportError as e:
        raise Pan115Error("未安装 p115client，请 pip install p115client") from e
    try:
        tok = P115Client.login_qrcode_token(app="web")  # 二维码 token 固定 web 端
    except Exception as e:
        raise Pan115Error(f"获取二维码失败: {e}") from e
    data = (tok.get("data") or {}) if isinstance(tok, dict) else {}
    uid = data.get("uid")
    if not uid:
        raise Pan115Error("未取到二维码 UID")
    # token 含 uid/time/sign，轮询接口需要，随扫码进度回传（不落库）
    token = {"uid": str(data.get("uid")), "time": data.get("time"), "sign": data.get("sign")}
    img = _fetch_qr_image(str(uid))
    b64 = base64.b64encode(img).decode("ascii")
    return {"uid": str(uid), "token": token, "qr": "data:image/png;base64," + b64}


def poll_qrcode_login(token: dict) -> dict:
    """轮询扫码状态，返回 {status, msg}。

    用静态 login_qrcode_scan_status（走 get/status，需 uid/time/sign），
    避免 P115Client() 无 cookie 实例化会触发交互式登录。
    """
    try:
        from p115client import P115Client
        payload = {"uid": str(token.get("uid")),
                   "time": token.get("time"),
                   "sign": token.get("sign")}
        r = P115Client.login_qrcode_scan_status(payload)
    except Exception as e:
        raise Pan115Error(f"查询扫码状态失败: {e}") from e
    data = r.get("data") or {} if isinstance(r, dict) else {}
    state = data.get("status") if isinstance(data, dict) else None
    # 未扫描时 data.status 可能为空，视为等待扫码
    if state is None and isinstance(r, dict) and r.get("state"):
        state = 0
    else:
        try:
            state = int(state)
        except (TypeError, ValueError):
            state = 0
    return {"status": state, "msg": _QR_STATUS_MSG.get(state, "未知状态")}


def _extract_cookie(resp: dict) -> str:
    """稳健提取 Cookie：兼容 str / dict-of-pairs / 嵌套 data.user.cookie。"""

    def _norm(v):
        if isinstance(v, dict):
            return "; ".join(f"{k}={v2}" for k, v2 in v.items())
        return str(v).strip()

    if not isinstance(resp, dict):
        raise Pan115Error("登录结果异常（非对象）")
    data = resp.get("data") or {}
    for bucket in (data, resp):
        ck = bucket.get("cookie") or bucket.get("cookie_str")
        if ck:
            return _norm(ck)
        user = bucket.get("user")
        if isinstance(user, dict) and user.get("cookie"):
            return _norm(user["cookie"])
    raise Pan115Error("登录结果未包含 Cookie")


def finish_qrcode_login(uid: str, app: str = "web") -> str:
    """绑定扫码结果并返回 Cookie 字符串。app 决定拿到哪个客户端账号的 Cookie。"""
    try:
        from p115client import P115Client
        # 注意：login_qrcode_scan_result 默认 app="alipaymini"，必须显式传 app
        resp = P115Client.login_qrcode_scan_result(str(uid), app=app or "web")
    except Exception as e:
        raise Pan115Error(f"获取登录 Cookie 失败: {e}") from e
    return _extract_cookie(resp)
