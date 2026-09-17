"""命令行入口：``bmahs-mcp``（默认 stdio 网关）、``http``（共享网关）、``discover``、``ctl``。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from . import __version__
from .server import run_main


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bmahs-mcp",
        description="BMAHS（比马斯）设备协议 ↔ MCP 网关",
    )
    parser.add_argument("--version", action="version", version=f"bmahs-mcp {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("serve", help="启动 MCP 网关（stdio，供 MCP 客户端连接；默认）")

    h = sub.add_parser("http", help="以 Streamable HTTP 模式启动共享网关（多客户端同时连接）")
    h.add_argument("--host", default=os.environ.get("BMAHS_HTTP_HOST", "0.0.0.0"))
    h.add_argument("--port", type=int, default=int(os.environ.get("BMAHS_HTTP_PORT", "9530") or 9530))
    h.add_argument("--path", default=os.environ.get("BMAHS_HTTP_PATH", "/mcp"))
    h.add_argument("--token", default=os.environ.get("BMAHS_HTTP_TOKEN") or None,
                   help="访问令牌：设置后客户端须携带 Authorization: Bearer <token>（推荐）")

    d = sub.add_parser("discover", help="扫描局域网内的 BMAHS 设备并列出")
    d.add_argument("--want", default="*", help="按品类过滤，如 light 或 light,display（默认 *）")
    d.add_argument("--seconds", type=float, default=4.0, help="监听时长（秒，默认 4）")

    c = sub.add_parser("ctl", help="对设备执行一个动作（联调用）：bmahs-mcp ctl <设备id> <action>")
    c.add_argument("device", help="设备 id（或显示名）")
    c.add_argument("action", help="动作名，如 on / brightness / describe")
    c.add_argument("--arg", action="append", default=[], metavar="K=V", help="动作参数，可多次")
    c.add_argument("--ttl", type=int, default=None, help="occupy 租约秒数（10–9999）")
    c.add_argument("--no-release", action="store_true", help="控制动作后不自动 release（保持占用）")
    c.add_argument("--timeout", type=float, default=30.0, help="单次调用超时秒数")
    return parser


def _parse_kv(pairs: list[str]) -> dict:
    """把 ``--arg K=V`` 重复参数解析成 dict：V 能按 JSON 解析就用类型化值，否则当普通字符串。"""
    out = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--arg 需要 K=V 形式，收到：{pair}")
        k, v = pair.split("=", 1)
        try:
            v = json.loads(v)  # 数字 / 布尔 / JSON 字面量
        except json.JSONDecodeError:
            pass  # 普通字符串
        out[k] = v
    return out


async def cmd_discover(args: argparse.Namespace) -> int:
    """discover 子命令：监听组播一段时间，打印设备清单并对第一台做 hello 连通性校验。"""
    from . import client
    from .discovery import Discovery

    disc = Discovery("bmahs-mcp-cli")
    await disc.start()
    await asyncio.sleep(max(1.0, args.seconds))
    devs = [d for d in disc.all() if args.want == "*" or (d.announce.get("type") or "").lower() in
            [w.strip().lower() for w in args.want.split(",")]]
    if not devs:
        print("未发现 BMAHS 设备。请确认设备已上电并在同一局域网（UDP 5354 组播可达）。")
        await disc.stop()
        return 1
    print(f"发现 {len(devs)} 台设备：")
    for d in sorted(devs, key=lambda x: x.id):
        print(
            f"  {d.id}  [{d.announce.get('type')}/{d.announce.get('service') or d.announce.get('svc')}]"
            f"  {d.name}  state={d.state}"
            + (f"  holder={d.holder}" if d.holder else "")
        )
        print(f"      control: {d.uri}  summary: {d.announce.get('summary') or '-'}")
    # 顺手验证第一台设备可连并读 hello
    target = sorted(devs, key=lambda x: x.id)[0]
    if target.uri:
        try:
            hello = await client.fetch_hello(target.uri, timeout=5.0)
            print(f"  hello 校验：{target.id} 共 {len(hello.get('operations') or hello.get('ops') or [])} 个动作，安全边界 auth={ (hello.get('security') or {}).get('auth') }")
        except Exception as e:  # noqa: BLE001
            print(f"  hello 校验失败：{e}")
    await disc.stop()
    return 0


async def cmd_ctl(args: argparse.Namespace) -> int:
    """ctl 子命令：一次性地对设备执行单个动作。

    按协议自动处理占用流程：控制类动作先 occupy（成功即持 token）→ 执行动作
    → 默认 release；只读动作免 token 直发。
    """
    from . import client
    from . import protocol as P
    from .discovery import Discovery

    disc = Discovery("bmahs-mcp-cli")
    await disc.start()
    deadline = asyncio.get_running_loop().time() + 4.0
    dev = None
    while dev is None and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.3)
        dev = disc.get(args.device)
        if dev is None:
            named = [d for d in disc.all() if d.name == args.device]
            dev = named[0] if len(named) == 1 else None
    if dev is None:
        print(f"找不到设备 {args.device!r}；已发现的设备：{[d.id for d in disc.all()] or '无'}")
        await disc.stop()
        return 1
    if not dev.uri:
        print(f"设备 {dev.id} 没有可连的 control 地址")
        await disc.stop()
        return 1

    async def call(payload: dict) -> dict:
        _, resp = await client.call_action(dev.uri, payload, timeout=args.timeout)
        return resp

    agent = "bmahs-mcp-cli"
    action = args.action
    extra = _parse_kv(args.arg)
    token = None
    released = False
    try:
        if action == "release":
            # release 需要原占用 token：CLI 不持久化，须显式通过 --arg token=... 提供
            if not extra.get("token"):
                print("release 需要原占用 token；请使用 --arg token=<occupy 返回值> 传入")
                return 2
            resp = await call({"action": action, "agent": agent, **extra})
            print(json.dumps(resp, ensure_ascii=False, indent=2))
            return 0 if resp.get("ok") else 1
        if action == "occupy":
            payload = {"action": "occupy", "agent": agent}
            if args.ttl is not None:
                payload["ttl"] = args.ttl
            resp = await call(payload)
            if resp.get("ok"):
                token = resp.get("token")
        elif action in P.READONLY_ACTIONS:
            resp = await call({"action": action, "agent": agent, **extra})
        else:
            occ = await call({"action": "occupy", "agent": agent})
            if not occ.get("ok"):
                print(json.dumps(occ, ensure_ascii=False, indent=2))
                await disc.stop()
                return 1
            token = occ.get("token")
            resp = await call({"action": action, "agent": agent, "token": token, **extra})
            if not args.no_release:
                # 动作成败都释放：CLI 是一次性调用，失败后继续持有只会让设备
                # 对其它智能体显示 occupied 直到租约到期
                rel = await call({"action": "release", "agent": agent, "token": token})
                released = rel.get("ok")
        print(json.dumps(resp, ensure_ascii=False, indent=2))
        if token and not released:
            print(f"（当前占用 token：{token}；释放：bmahs-mcp ctl {dev.id} release 需携带该 token）")
        return 0 if resp.get("ok") else 1
    finally:
        await disc.stop()


def main(argv: list[str] | None = None) -> int:
    """CLI 总入口：按子命令分发（serve/http/discover/ctl，无参数默认 serve）。"""
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    args = _build_parser().parse_args(argv)
    if args.cmd in (None, "serve"):
        return run_main()
    if args.cmd == "http":
        from .server import run_http_main

        return run_http_main(args.host, args.port, args.path, args.token)
    if args.cmd == "discover":
        return asyncio.run(cmd_discover(args))
    if args.cmd == "ctl":
        return asyncio.run(cmd_ctl(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
