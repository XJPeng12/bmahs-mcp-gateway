"""BMAHS 控制层 TCP 客户端（协议 §6）：hello 读取与一问一答行协议。

成帧：一条 TCP 记录 = 一行 UTF-8 JSON + ``\\n``。
网关对每次调用使用短连接（连上先读 hello，再发一行动作，读一行响应即断开），
既刷新了设备自述，又避免了设备/网关两侧的半开连接状态。
"""

from __future__ import annotations

import asyncio
import json
import struct

from . import protocol as P

READ_LIMIT = 1 << 20  # hello 携带完整 ops，放宽行长度上限
DEFAULT_TIMEOUT = 30.0


class BmahsError(Exception):
    """本地连接 / 成帧错误（未到达设备业务层）。"""


async def _connect(uri: str, timeout: float) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """建立到设备 control 地址的 TCP 连接；URI 非法 / 超时 / 不可达统一抛 BmahsError。"""
    target = P.parse_control_uri(uri)
    if target is None:
        raise BmahsError(f"无法解析 control URI：{uri!r}（期望形如 tcp://IP:PORT）")
    host, port = target
    try:
        return await asyncio.wait_for(asyncio.open_connection(host, port, limit=READ_LIMIT), timeout)
    except asyncio.TimeoutError as e:
        raise BmahsError(f"连接设备 {host}:{port} 超时（{timeout:.0f}s）") from e
    except OSError as e:
        raise BmahsError(f"连接设备 {host}:{port} 失败：{e}") from e


async def _close(writer: asyncio.StreamWriter) -> None:
    """静默关闭连接：短连接模型下断开是常态，关闭失败无需上抛。"""
    try:
        writer.close()
        await writer.wait_closed()
    except (OSError, asyncio.TimeoutError):
        pass


async def _read_obj(reader: asyncio.StreamReader, timeout: float) -> dict:
    """按行协议读一行并解析为 JSON 对象；超时 / 断连 / 非 JSON / 非对象均抛 BmahsError。"""
    try:
        line = await asyncio.wait_for(reader.readline(), timeout)
    except asyncio.TimeoutError as e:
        raise BmahsError(f"设备响应超时（{timeout:.0f}s）") from e
    if not line:
        raise BmahsError("设备断开了连接（未返回数据）")
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as e:
        raise BmahsError(f"设备返回了非 JSON 行：{line[:160]!r}") from e
    if not isinstance(obj, dict):
        raise BmahsError("设备返回了非 JSON 对象")
    return obj


async def fetch_hello(uri: str, timeout: float = 8.0) -> dict:
    """连上 control 并读取首行 hello。

    双协议（1.2 §20 编码协商）：按首行**实际形态**识别——JSON-RPC 通知
    （``method:"hello"`` 无 id）为 bmahs/1.2 设备，取其 params 归一为扁平 hello；
    1.x 响应式对象（含 ``action:"hello"``）为旧版设备，原样使用。
    校验 `operations`（兼容期同时接受旧版 bmahs/1–1.2 时期设备的 `ops` 键）。
    """
    reader, writer = await _connect(uri, timeout)
    try:
        raw = await _read_obj(reader, timeout)
    finally:
        await _close(writer)
    hello = P.normalize_hello(raw)
    if hello is None or not (
        isinstance(hello.get("operations"), list) or isinstance(hello.get("ops"), list)
    ):
        raise BmahsError("连接后未收到合法的 hello（对端可能不是 BMAHS 设备）")
    return hello


async def call_action(
    uri: str, payload: dict, timeout: float = DEFAULT_TIMEOUT
) -> tuple[dict, dict]:
    """短连接一问一答：hello → 请求行 → 响应行。返回 (hello, response)。

    双协议：hello 为 1.2 通知形态时按 JSON-RPC 2.0 发请求（``method``=动作、
    ``params``=扁平参数去 action），并把 result/error 还原为 1.x 扁平信封——
    上游（网关占用分流 / 错误透传 / token 遮蔽）完全不感知编码差异；
    hello 为 1.x 形态时走原自定义 JSON 行协议。
    """
    reader, writer = await _connect(uri, timeout)
    try:
        raw_hello = await _read_obj(reader, min(timeout, 10.0))
        hello = P.normalize_hello(raw_hello)
        if hello is None:
            raise BmahsError("连接后未收到合法的 hello（对端可能不是 BMAHS 设备）")
        action = str(payload.get("action") or "")
        if P.is_hello_notification(raw_hello):
            writer.write(P.rpc_call_line(1, action,
                                         {k: v for k, v in payload.items() if k != "action"}))
            await writer.drain()
            resp_obj = await _read_obj(reader, timeout)
            resp = P.rpc_response_to_envelope(action, resp_obj)
        else:
            line = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
            writer.write(line)
            await writer.drain()
            resp = await _read_obj(reader, timeout)
    finally:
        await _close(writer)
    return hello, resp


async def read_ui_frame(uri: str, token: str, timeout: float = 15.0) -> tuple[int, int, int, bytes]:
    """连接 ui.start 返回的 UI 流并读取一帧（协议 §4.9.4 二进制帧）。

    返回 ``(codec, width, height, payload)``；codec 1 = JPEG。
    """
    target = P.parse_control_uri(uri)
    if target is None:
        raise BmahsError(f"无法解析 ui URI：{uri!r}")
    host, port = target
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, limit=READ_LIMIT), timeout
        )
    except asyncio.TimeoutError as e:
        raise BmahsError(f"连接 UI 流 {host}:{port} 超时") from e
    except OSError as e:
        raise BmahsError(f"连接 UI 流 {host}:{port} 失败：{e}") from e
    try:
        token_bytes = token.encode("utf-8")
        writer.write(bytes([len(token_bytes)]) + token_bytes)
        await writer.drain()
        status = await asyncio.wait_for(reader.readexactly(1), timeout)
        if status != b"\x00":
            raise BmahsError(f"UI 流鉴权失败（status={status[0]}），设备拒绝出帧")
        header = await asyncio.wait_for(reader.readexactly(10), timeout)
        payload_len, width, height, codec, _flags = struct.unpack("!IHHBB", header)
        if payload_len <= 0 or payload_len > 32 * 1024 * 1024:
            raise BmahsError(f"UI 帧长度异常：{payload_len}")
        payload = await asyncio.wait_for(reader.readexactly(payload_len), timeout)
    except asyncio.IncompleteReadError as e:
        raise BmahsError(f"UI 流提前断开（已读 {len(e.partial)} 字节）") from e
    except asyncio.TimeoutError as e:
        raise BmahsError("读取 UI 帧超时") from e
    finally:
        await _close(writer)
    return codec, width, height, payload


async def read_ui_frames(uri: str, token: str, timeout: float = 30.0):
    """持续读 UI 流（协议 §4.9.4）：鉴权后逐帧 yield ``(codec, width, height, payload)``。

    设备正常关闭流（帧读完）时正常结束；超时抛 :class:`BmahsError`。
    """
    target = P.parse_control_uri(uri)
    if target is None:
        raise BmahsError(f"无法解析 ui URI：{uri!r}")
    host, port = target
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, limit=READ_LIMIT), timeout
        )
    except asyncio.TimeoutError as e:
        raise BmahsError(f"连接 UI 流 {host}:{port} 超时") from e
    except OSError as e:
        raise BmahsError(f"连接 UI 流 {host}:{port} 失败：{e}") from e
    try:
        token_bytes = token.encode("utf-8")
        writer.write(bytes([len(token_bytes)]) + token_bytes)
        await writer.drain()
        status = await asyncio.wait_for(reader.readexactly(1), timeout)
        if status != b"\x00":
            raise BmahsError(f"UI 流鉴权失败（status={status[0]}），设备拒绝出帧")
        while True:
            try:
                header = await asyncio.wait_for(reader.readexactly(10), timeout)
                payload_len, width, height, codec, _flags = struct.unpack("!IHHBB", header)
                if payload_len <= 0 or payload_len > 32 * 1024 * 1024:
                    raise BmahsError(f"UI 帧长度异常：{payload_len}")
                payload = await asyncio.wait_for(reader.readexactly(payload_len), timeout)
            except asyncio.IncompleteReadError:
                return  # 设备端正常关闭流（帧已读完）
            yield codec, width, height, payload
    except asyncio.TimeoutError as e:
        raise BmahsError("读取 UI 帧超时") from e
    finally:
        await _close(writer)
