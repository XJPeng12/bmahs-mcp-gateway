"""把网关装配为 MCP 服务器（stdio 传输；P1 增加 Streamable HTTP 共享传输）。"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import sys

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError

from . import __version__
from .gateway import DeviceEnvelope, Gateway, GatewayError

log = logging.getLogger("bmahs.server")


def build_server(gw: Gateway, *, per_session: bool = False) -> Server:
    """装配 MCP Server。

    ``per_session=False``（stdio）：整个进程一个会话，占用身份为进程主 agent。
    ``per_session=True``（HTTP）：每个 MCP 会话独立会话键（s1/s2…），占用身份
    为 ``<agent_id>-sN``，多客户端的占用互斥可追溯到具体会话。
    """

    def _skey(ctx) -> str | None:  # noqa: ANN001
        if not per_session:
            return None
        # ctx.session 是 SDK 2.x 每请求重建的代理，不能当会话身份；
        # 其 _connection 在 MCP 会话生命周期内稳定（私有属性，缺失时退回对象本身）
        return gw.session_key(getattr(ctx.session, "_connection", None) or ctx.session)

    server = Server(
        "bmahs-mcp-gateway",
        version=__version__,
        instructions=(
            "本服务器是 BMAHS（比马斯）设备网关：每台发现的 BMAHS 设备的 operations 已映射为"
            "「<设备id>__<动作>」形式的工具，工具说明含设备的自然语言自述与安全边界。"
            "先用 bmahs_devices 查看设备并按 name/summary 选型。网关按设备占用策略"
            "（occupancy）自动选择控制方式：exclusive 设备控制前自动 occupy 并携带 token，"
            "任务结束（含失败/取消）必须 bmahs_release；last-wins 设备无需占用/释放，"
            "直接调用业务动作即可（最后一条命令生效）。"
        ),
        on_list_tools=_make_list_tools(gw, _skey),
        on_call_tool=_make_call_tool(gw, _skey),
    )
    return server


def _make_list_tools(gw: Gateway, _skey):
    """生成 MCP list_tools 处理器：登记会话后返回网关的完整工具表。"""
    async def on_list_tools(ctx, params):  # noqa: ANN001, ANN202
        gw.note_session(ctx.session)
        tools = await gw.list_tools()
        return types.ListToolsResult(tools=tools)

    return on_list_tools


def _make_call_tool(gw: Gateway, _skey):
    """生成 MCP call_tool 处理器：执行工具并把三类失败转成模型可读的错误内容。

    设备信封（DeviceEnvelope）与网关本地错误（GatewayError）都以 is_error=true
    的结构化 JSON 返回（模型可据此重试/换设备）；未知工具等 MCPError 与取消
    异常原样上抛；其余异常兜底为 internal 错误，绝不让网关进程崩溃。
    """
    async def on_call_tool(ctx, params):  # noqa: ANN001, ANN202
        gw.note_session(ctx.session)
        name = params.name
        arguments = dict(params.arguments or {})
        try:
            content = await gw.call_tool(name, arguments, _skey(ctx))
            return types.CallToolResult(content=content, is_error=False)
        except DeviceEnvelope as e:
            # 设备明确拒绝（occupied / unauthorized / bad-arg / denied …）：
            # 把协议错误信封完整交给模型判断
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=json.dumps(gw._mask(e.envelope), ensure_ascii=False, indent=2),
                    )
                ],
                is_error=True,
            )
        except GatewayError as e:
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=json.dumps(
                            {"ok": False, "code": "gateway", "error": str(e)},
                            ensure_ascii=False,
                            indent=2,
                        ),
                    )
                ],
                is_error=True,
            )
        except MCPError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("工具 %s 执行异常", name)
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=json.dumps(
                            {"ok": False, "code": "internal", "error": f"网关内部错误：{e}"},
                            ensure_ascii=False,
                            indent=2,
                        ),
                    )
                ],
                is_error=True,
            )

    return on_call_tool


async def serve() -> int:
    """stdio 模式主循环：启动网关 → 挂到标准输入输出上跑 MCP 协议 → 退出前释放设备。"""
    gw = Gateway()
    server = build_server(gw)
    code = 0
    try:
        await gw.start()
        async with stdio_server() as (read_stream, write_stream):
            init_options = server.create_initialization_options(
                NotificationOptions(tools_changed=True)
            )
            await server.run(read_stream, write_stream, init_options)
    except asyncio.CancelledError:
        pass
    except Exception as e:  # noqa: BLE001
        log.error("网关异常退出: %s", e)
        code = 1
    finally:
        await gw.aclose()
    return code


def run_main() -> int:
    """stdio 网关的进程入口：配置日志后启动事件循环。"""
    _setup_logging()
    return asyncio.run(serve())


# ------------------------------------------------------------------ Streamable HTTP（P1）


class BearerTokenGuard:
    """ASGI 中间件：配置了访问令牌时，校验 ``Authorization: Bearer <token>``。"""

    def __init__(self, app, token: str) -> None:  # noqa: ANN001
        self.app = app                    # 被包裹的内层 ASGI 应用（MCP 服务）
        self.expected = b"Bearer " + token.encode("utf-8")  # 期望的完整请求头值

    async def __call__(self, scope, receive, send):  # noqa: ANN001
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in (scope.get("headers") or [])}
        # 常量时间比较，避免对令牌的计时侧信道
        if not hmac.compare_digest(headers.get(b"authorization", b""), self.expected):
            body = json.dumps(
                {"ok": False, "code": "unauthorized", "error": "缺少或错误的访问令牌（Authorization: Bearer <token>）"},
                ensure_ascii=False,
            ).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json; charset=utf-8"),
                        (b"content-length", str(len(body)).encode("latin-1")),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def build_http_app(
    gw: Gateway,
    *,
    host: str = "0.0.0.0",
    path: str = "/mcp",
    token: str | None = None,
):
    """构建 Streamable HTTP ASGI 应用（多客户端共享一个网关进程）。"""
    server = build_server(gw, per_session=True)
    app = server.streamable_http_app(streamable_http_path=path, host=host)
    if token:
        app = BearerTokenGuard(app, token)
    return app


async def serve_http(
    gw: Gateway, app, host: str, port: int
) -> None:  # noqa: ANN001
    """HTTP 模式主循环：启动网关后用 uvicorn 持续服务，退出前释放设备。"""
    try:
        import uvicorn
    except ImportError:
        log.error("HTTP 共享模式需要 uvicorn：请安装 bmahs-mcp-gateway[http] 后重试")
        raise SystemExit(2) from None

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    http_server = uvicorn.Server(config)
    await gw.start()
    log.info("HTTP 共享网关已启动：http://%s:%d", host, port)
    try:
        await http_server.serve()
    finally:
        await gw.aclose()


def run_http_main(host: str, port: int, path: str = "/mcp", token: str | None = None) -> int:
    """HTTP 共享网关的进程入口。"""
    _setup_logging()
    gw = Gateway()
    if token:
        log.info("已启用访问令牌鉴权（BMAHS_HTTP_TOKEN / --token）")
    app = build_http_app(gw, host=host, path=path, token=token)
    try:
        asyncio.run(serve_http(gw, app, host, port))
        return 0
    except KeyboardInterrupt:
        return 0


def _setup_logging() -> None:
    """初始化日志：全部写 stderr（stdout 属于 MCP 协议，绝不能打印日志），级别由 BMAHS_LOG_LEVEL 控制。"""
    if os.name == "nt":
        # Windows 控制台：强制 stderr 用 UTF-8，避免中文日志乱码；stdout 属于 MCP 协议，禁止打印
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    raw_level = os.environ.get("BMAHS_LOG_LEVEL", "INFO").upper()
    # 非法级别名（如 "VERBOSE"）回退 INFO，避免 getLevelName 返回字符串参与数值比较
    level = raw_level if isinstance(logging.getLevelName(raw_level), int) else "INFO"
    logging.basicConfig(
        stream=sys.stderr,
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("mcp").setLevel(max(logging.INFO, min(logging.WARNING, logging.getLevelName(level))))
