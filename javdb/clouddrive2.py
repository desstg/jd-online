"""CloudDrive2 gRPC 客户端（基于 h2 纯 Python HTTP/2，无 grpcio 依赖）。

服务定义见仓库根目录 `clouddrive.proto`（package=clouddrive，service=CloudDriveFileSrv）。
官方文档：https://www.clouddrive2.com/api/CloudDrive2_gRPC_API_Guide.html

认证方式二选一：
- API Token（推荐）：设置 → API 令牌 → 填 API Token，调用时带 `authorization: Bearer <token>`
- 账号/密码：调用 GetToken 换取 JWT（username/password），再用同一 Bearer 头

本模块仅封装「测试连接 / 获取 Token / 加离线下载（磁链/ed2k）」，供设置页与推送管线使用。
"""
from __future__ import annotations

import socket
import struct
from urllib.parse import unquote

import h2.connection
import h2.config
import h2.events


class CloudDrive2Error(Exception):
    pass


SERVICE = "clouddrive.CloudDriveFileSrv"


# ---------------------------------------------------------------- protobuf 基础编解码
def _varint(n: int) -> bytes:
    out = bytearray()
    n = int(n) & 0xFFFFFFFFFFFFFFFF
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _field(fno: int, wt: int) -> bytes:
    return _varint((fno << 3) | wt)


def enc_bytes(fno: int, data: bytes) -> bytes:
    return _field(fno, 2) + _varint(len(data)) + data


def enc_str(fno: int, s: str) -> bytes:
    return enc_bytes(fno, s.encode("utf-8"))


def enc_bool(fno: int, v: bool) -> bytes:
    return _field(fno, 0) + _varint(1 if v else 0)


def enc_uint(fno: int, v: int) -> bytes:
    return _field(fno, 0) + _varint(v)


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    r = 0
    shift = 0
    while i < len(buf):
        c = buf[i]
        i += 1
        r |= (c & 0x7F) << shift
        if not c & 0x80:
            return r, i
        shift += 7
    return r, i


def decode_fields(pb: bytes) -> dict:
    """把 protobuf 解成 {field_no: value}，LEN 类型为 bytes、VARINT 为 int。"""
    out: dict = {}
    i = 0
    while i < len(pb):
        key, ni = _read_varint(pb, i)
        i = ni
        fno, wt = key >> 3, key & 7
        if wt == 2:
            ln, ni = _read_varint(pb, i)
            i = ni
            out[fno] = pb[i:i + ln]
            i += ln
        elif wt == 0:
            v, ni = _read_varint(pb, i)
            i = ni
            out[fno] = v
        else:
            break  # 未知 wire type，停止
    return out


def _msg_body(resp: bytes) -> bytes:
    """gRPC 响应体去掉帧头，返回第一个消息的 protobuf。"""
    if len(resp) < 5:
        return resp
    return resp[5:5 + struct.unpack(">I", resp[1:5])[0]]


# ---------------------------------------------------------------- HTTP/2(gRPC) 传输
def _grpc_unary(host: str, port: int, method: str, request_pb: bytes,
                token: str | None = None, timeout: float = 8) -> bytes:
    """对 CloudDrive2 发送一次 gRPC unary 调用，返回响应消息体（去掉帧头）。

    底层为明文 HTTP/2（h2c）——gRPC 服务不认 HTTP/1.1，必须先 h2 握手。
    """
    try:
        s = socket.create_connection((host, int(port)), timeout=timeout)
    except OSError as e:
        raise CloudDrive2Error(f"连接 {host}:{port} 失败：{e}") from e
    s.settimeout(timeout)
    cfg = h2.config.H2Configuration(client_side=True, header_encoding="utf-8")
    conn = h2.connection.H2Connection(config=cfg)
    conn.initiate_connection()
    s.sendall(conn.data_to_send())

    headers = [
        (":method", "POST"),
        (":scheme", "http"),
        (":authority", f"{host}:{port}"),
        (":path", f"/{SERVICE}/{method}"),
        ("content-type", "application/grpc"),
        ("te", "trailers"),
    ]
    if token:
        headers.append(("authorization", f"Bearer {token}"))
    # gRPC 帧：1 字节压缩标志 + 4 字节大端长度 + protobuf
    framed = b"\x00" + struct.pack(">I", len(request_pb)) + request_pb
    conn.send_headers(1, headers, end_stream=False)
    conn.send_data(1, framed, end_stream=True)
    s.sendall(conn.data_to_send())

    chunks: list[bytes] = []
    grpc_status: str | None = None
    grpc_message = ""
    done = False
    try:
        while not done:
            data = s.recv(65535)
            if not data:
                break
            for ev in conn.receive_data(data):
                if isinstance(ev, h2.events.DataReceived):
                    chunks.append(ev.data)
                elif isinstance(ev, h2.events.TrailersReceived):
                    for k, v in ev.headers:
                        if k.lower() == "grpc-status":
                            grpc_status = v
                        elif k.lower() == "grpc-message":
                            grpc_message = unquote(v)
                elif isinstance(ev, h2.events.StreamEnded):
                    done = True
                elif isinstance(ev, h2.events.StreamReset):
                    done = True
            s.sendall(conn.data_to_send())
    except socket.timeout:
        raise CloudDrive2Error("请求超时")
    finally:
        try:
            s.close()
        except OSError:
            pass

    if grpc_status is not None and grpc_status != "0":
        msg = unquote(grpc_message) if grpc_message else "gRPC 调用失败"
        raise CloudDrive2Error(msg)

    return b"".join(chunks)


# ---------------------------------------------------------------- 业务方法
def get_system_info(host: str, rpc_port: int, timeout: float = 8) -> dict:
    """免授权，查询服务是否登录、用户、就绪状态。"""
    resp = _grpc_unary(host, rpc_port, "GetSystemInfo", b"", timeout=timeout)
    f = decode_fields(_msg_body(resp))
    return {
        "is_login": bool(f.get(1)),
        "user_name": (f.get(2) or b"").decode("utf-8", "replace"),
        "system_ready": bool(f.get(3)),
        "system_message": (f.get(4) or b"").decode("utf-8", "replace") or None,
        "has_error": bool(f.get(5)),
        "device_power_type": f.get(6),
    }


def get_token(host: str, rpc_port: int, user_name: str, password: str,
              timeout: float = 8) -> str:
    """用账号/密码换取 JWT Token；失败抛 CloudDrive2Error。"""
    req = enc_str(1, user_name) + enc_str(2, password)
    resp = _grpc_unary(host, rpc_port, "GetToken", req, timeout=timeout)
    f = decode_fields(_msg_body(resp))
    if not f.get(1):
        err = (f.get(2) or b"").decode("utf-8", "replace") or "登录失败"
        raise CloudDrive2Error(err)
    token = f.get(3)
    if not token:
        raise CloudDrive2Error("CloudDrive2 未返回 Token")
    return token.decode("utf-8")


def add_offline_files(host: str, rpc_port: int, urls: str, to_folder: str,
                      token: str, timeout: float = 30) -> dict:
    """把磁链/ed2k 加为离线下载（远程上传）。

    AddOfflineFileRequest{urls=1, toFolder=2, checkFolderAfterSecs=3}
    urls 支持换行分隔的多个链接；toFolder 为服务器上的目标路径（如 /downloads）。
    """
    req = enc_str(1, urls) + enc_str(2, to_folder)
    resp = _grpc_unary(host, rpc_port, "AddOfflineFiles", req, token=token, timeout=timeout)
    f = decode_fields(_msg_body(resp))
    if not f.get(1):
        err = (f.get(2) or b"").decode("utf-8", "replace") or "添加离线下载失败"
        raise CloudDrive2Error(err)
    return {"success": True, "paths": [p.decode() for p in (f.get(3) or [])]}


def test_connection(host: str, rpc_port: int, api_token: str | None = None,
                    user_name: str | None = None, password: str | None = None,
                    timeout: float = 8) -> tuple[bool, str]:
    """测试连接。

    1. GetSystemInfo（免授权）——验证地址/端口可达、服务就绪。
    2. 有 API Token 或账号密码则进一步校验凭证（GetToken / 已授权调用）。
    """
    try:
        info = get_system_info(host, rpc_port, timeout=timeout)
    except CloudDrive2Error as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001
        return False, f"连接异常：{e}"

    # 免授权可达性已通过，再校验凭证
    token = (api_token or "").strip()
    if not token and user_name and password:
        try:
            token = get_token(host, rpc_port, user_name, password, timeout=timeout)
        except CloudDrive2Error as e:
            return False, f"登录失败：{e}"

    if token:
        # 用 token 调一个鉴权空方法验证有效性
        try:
            _grpc_unary(host, rpc_port, "GetMountPoints", b"", token=token, timeout=timeout)
        except CloudDrive2Error as e:
            return False, f"Token 无效：{e}"

    ready = "；服务已就绪" if info.get("system_ready") else ""
    return True, f"连接正常，服务端登录：{info.get('user_name') or '未登录'}{ready}"
