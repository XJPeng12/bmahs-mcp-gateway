"""BMAHS ↔ MCP 网关核心。

实现协议 §4.8「智能体与协议客户端义务」中与运行时相关的部分：

1. 持续发现设备，缓存每台设备的 ``hello``（自述 / operations / security）；
2. 把每台设备的 ``operations`` 动态映射为 MCP 工具（``<设备id>__<动作>``），
   工具说明全部来自设备的自然语言字段；
3. 按 1.1 ``security.occupancy`` 逐台选择控制序列：``last-wins`` 设备直接发
   业务动作（不 occupy、不带 token，§4.8-4）；``exclusive`` 设备代管占用
   ``token``（自动占用为有限租约，后续控制自动携带，token 不回显给模型、
   不写入任何日志）；occupancy 缺省按 exclusive，1.0 设备零回归；
4. 服务退出时统一 ``release``（仅 exclusive 会话持有 token），不把设备留在占用态；
5. ``security`` 当作硬约束（越界动作由设备拒绝，网关原样转达）。
"""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import json
import logging
import os
import secrets
import socket
import tempfile
import time
from pathlib import Path

from mcp import types
from mcp.shared.exceptions import MCPError

from . import client, protocol as P, schemas
from .discovery import Device, Discovery

log = logging.getLogger("bmahs.gateway")

# 回显给模型时的 token 替身：真实 token 只保存在网关内存里，绝不能出现在
# 模型可见的文本 / 日志 / 会话记录中（§4.8 第 9 条）
TOKEN_PLACEHOLDER = "«token 已由网关保存，调用设备动作时自动携带，无需在会话中传递»"
# hello 维护循环的轮询周期（秒）：每轮为还没拿到 hello 的设备补读一次
HELLO_PRIME_INTERVAL = 5.0
# hello 超过该秒数未刷新即视为过期，bmahs_refresh 时会重读
HELLO_STALE_SEC = 300.0
# hello 读取失败后的重试退避间隔（秒），避免对离线设备疯狂建连
HELLO_RETRY_SEC = 30.0
# JSON-RPC「参数无效」错误码：调用不存在的工具时返回
INVALID_PARAMS = 32602


class GatewayError(Exception):
    """网关本地错误（设备不可达、参数问题等）。"""


class DeviceEnvelope(Exception):
    """设备返回了 ``ok=false`` 错误信封（§4.7），原样透传给模型。"""

    def __init__(self, envelope: dict) -> None:
        super().__init__(str(envelope.get("error") or envelope.get("code") or "device error"))
        # 完整的设备错误信封（ok/action/code/error/retryable），server 层原样回给模型
        self.envelope = envelope


class Gateway:
    """网关核心：设备注册表 + MCP 工具表 + 占用 token 代管。

    对模型暴露两类工具：7 个固定工具（bmahs_devices/refresh/describe/occupy/
    release/call/screenshot）+ 每台设备每个非通用动作一个动态工具。
    所有设备交互经 :mod:`client` 的短连接完成，token 由本类按会话保管、
    请求时自动附带并在返回前遮蔽。
    """

    def __init__(self) -> None:
        # 每进程唯一：同机多个网关进程身份不同，占用方崩溃后可按名辨识，
        # 且避免「同名不同 token」造成的相互锁死（§4.8）
        self.agent_id = (
            os.environ.get("BMAHS_AGENT_ID")
            or f"bmahs-mcp-{socket.gethostname().split('.')[0].lower() or 'gateway'}"
            f"-{secrets.token_hex(3)}"
        )
        self.auto_occupy = os.environ.get("BMAHS_AUTO_OCCUPY", "1").strip().lower() not in (
            "0",
            "false",
            "no",
        )
        # 自动占用（模型未显式 occupy 时）用有限租约：网关进程若崩溃未 release，
        # 设备在租约到期后自动收回，不会永久锁死
        try:
            auto_ttl = int(os.environ.get("BMAHS_AUTO_OCCUPY_TTL", "120") or 120)
        except ValueError:
            auto_ttl = 120
        self.auto_occupy_ttl = max(10, min(9998, auto_ttl))
        # 租约上限：无论模型请求多长的租约，都不会超过它（0/负值视为 3600）
        try:
            max_lease = int(os.environ.get("BMAHS_MAX_LEASE", "3600") or 3600)
        except ValueError:
            max_lease = 3600
        self.max_lease = max(self.auto_occupy_ttl, min(9998, max_lease))
        # 单次 TCP 调用（连接/动作/响应）的超时秒数
        self.call_timeout = float(os.environ.get("BMAHS_CALL_TIMEOUT", "30") or 30)
        query_interval = float(os.environ.get("BMAHS_QUERY_INTERVAL", "300") or 300)
        expire_sec = float(os.environ.get("BMAHS_EXPIRE_SEC", "1800") or 1800)
        static_raw = os.environ.get("BMAHS_STATIC_DEVICES", "")
        static_uris = [s.strip() for s in static_raw.replace(";", ",").split(",") if s.strip()]
        # Bonjour 浏览通道（协议 §3.2「智能体应浏览 _bmahs._tcp」）：UDP 组播静默
        # 但 mDNS/TCP 正常的设备（多网卡绑错接口等，docs/组播发现失败-原因与排查.md）
        # 也能进注册表；zeroconf 缺失时自动降级。BMAHS_BONJOUR_BROWSE=0 关闭
        bonjour_browse = os.environ.get("BMAHS_BONJOUR_BROWSE", "1").strip().lower() not in (
            "0",
            "false",
            "no",
        )
        # 截图帧落盘目录（bmahs_screenshot 的 saved_to）
        self.capture_dir = Path(
            os.environ.get("BMAHS_CAPTURE_DIR") or Path(tempfile.gettempdir()) / "bmahs_captures"
        )
        # id 冲突处置策略（docs/设备id冲突-现状与改进.md §5.1）：warn=标记+告警+
        # 工具描述警示（默认，兼容多网卡设备的持续误报源）；isolate=冲突设备不
        # 生成动态工具且拒绝控制类调用；off=完全不检测（多网卡误报时的逃生门）
        policy = os.environ.get("BMAHS_ID_CONFLICT_POLICY", "warn").strip().lower()
        self.id_conflict_policy = policy if policy in ("warn", "isolate", "off") else "warn"
        self.discovery = Discovery(
            self.agent_id,
            query_interval=query_interval,
            expire_sec=expire_sec,
            static_uris=static_uris,
            bonjour=bonjour_browse,
            conflict_detect=self.id_conflict_policy != "off",
            on_change=self._on_devices_changed,
        )
        # 工具暴露过滤（P1）：BMAHS_TOOL_ALLOW / BMAHS_TOOL_DENY，逗号分隔的通配符
        # 模式，匹配动态工具名（如 fake-dev-1__on）、「设备id__动作」或纯动作名；
        # 只作用于动态工具，固定工具始终可用
        self.tool_allow = self._patterns("BMAHS_TOOL_ALLOW")
        self.tool_deny = self._patterns("BMAHS_TOOL_DENY")
        # 占用 token 按会话隔离（P1）：stdio 单会话用 "local"；HTTP 模式每个 MCP
        # 会话一个键（s1/s2…），占用方显示为 <agent_id>-sN，可追溯到对话会话。
        # 注意 SDK 2.x 的 ctx.session 是"每请求重建的代理"，稳定的会话身份是
        # session._connection（由 server 层解析后传入，见 server.build_server）。
        self.tokens: dict[str, dict[str, str]] = {}  # session_key -> {device id: token}
        self._session_keys: dict[int, str] = {}  # id(连接对象) -> 会话键
        self._session_seq = 0
        self._client_proxies: dict[int, object] = {}  # id(连接) -> 最近的会话代理（发通知用）
        # MCP 动态工具名 -> (设备 id, 动作名)：call_tool 时按它路由到具体设备动作
        self.tool_map: dict[str, tuple[str, str]] = {}
        # 上一版工具表的指纹（工具名+描述哈希）；变化才向客户端发 tools/list_changed
        self._tools_sig: str = ""
        # hello 并发读取闸门：限制同时建连的设备数，防止刷新风暴
        self._hello_sema = asyncio.Semaphore(8)
        # 后台周期任务（hello 维护循环），aclose 时统一取消
        self._tasks: list[asyncio.Task] = []

    @staticmethod
    def _patterns(env: str) -> list[str]:
        """读一个逗号/分号分隔的环境变量为通配符模式列表（工具过滤用）。"""
        return [
            p.strip()
            for p in os.environ.get(env, "").replace(";", ",").split(",")
            if p.strip()
        ]

    # ------------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        """启动发现层、构建初始工具表，并拉起 hello 维护后台任务。"""
        await self.discovery.start()
        self._rebuild_tools()
        self._tasks.append(asyncio.create_task(self._hello_loop(), name="bmahs-hello"))

    async def aclose(self) -> None:
        """退出前统一释放占用的设备（§4.8 第 6 条：任务结束 / 进程退出必须 release）。"""
        for t in self._tasks:
            t.cancel()
        for skey, toks in list(self.tokens.items()):
            agent = self.agent_for(skey)
            for dev_id, token in list(toks.items()):
                try:
                    dev = self.discovery.get(dev_id)
                    if dev is not None and dev.uri:
                        await asyncio.wait_for(
                            client.call_action(
                                dev.uri,
                                {"action": "release", "agent": agent, "token": token},
                                timeout=5.0,
                            ),
                            timeout=8.0,
                        )
                        log.info("退出前已释放设备 %s（会话 %s）", dev_id, skey)
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "退出时释放设备 %s 失败（可能需重启该设备以解除占用）: %s", dev_id, e
                    )
        self.tokens.clear()
        await self.discovery.stop()

    def note_session(self, session) -> str:  # noqa: ANN001
        """登记一个 MCP 会话（对象或其连接对象）并返回会话键；None → "local"。"""
        if session is None:
            return "local"
        identity = id(getattr(session, "_connection", None) or session)
        self._client_proxies[identity] = session  # 最近代理，用于发 tools/list_changed
        key = self._session_keys.get(identity)
        if key is None:
            self._session_seq += 1
            key = f"s{self._session_seq}"
            self._session_keys[identity] = key
            self.tokens.setdefault(key, {})
            log.info("新 MCP 会话 %s（占用方身份 %s）", key, self.agent_for(key))
        return key

    def session_key(self, session) -> str:  # noqa: ANN001
        """查询会话键（未登记过则顺带登记）；None → "local"。"""
        if session is None:
            return "local"
        identity = id(getattr(session, "_connection", None) or session)
        key = self._session_keys.get(identity)
        if key is None:
            key = self.note_session(session)
        return key

    def agent_for(self, session_key: str | None) -> str:
        """会话在协议中的占用方身份：stdio 用进程主身份，HTTP 会话带 -sN 后缀。"""
        if not session_key or session_key == "local":
            return self.agent_id
        return f"{self.agent_id}-{session_key}"

    def _token(self, skey: str, dev_id: str) -> str | None:
        """当前会话持有的某设备占用 token；None 表示未占用。"""
        return self.tokens.get(skey, {}).get(dev_id)

    def _set_token(self, skey: str, dev_id: str, token: str) -> None:
        """记录 occupy 成功后设备签发的 token（仅内存，不落盘不打日志）。"""
        self.tokens.setdefault(skey, {})[dev_id] = token

    def _pop_token(self, skey: str, dev_id: str) -> str | None:
        """取出并清除某设备的 token（release 时用，防止重复释放）。"""
        return self.tokens.get(skey, {}).pop(dev_id, None)

    async def _on_devices_changed(self) -> None:
        """发现层回调：设备列表有可观变化时重建工具表并通知客户端。"""
        if self._rebuild_tools():
            await self._notify_tools_changed()

    async def _notify_tools_changed(self) -> None:
        """向所有已知 MCP 会话广播 tools/list_changed，促使客户端重新拉取工具表。"""
        count = 0
        for proxy in list(self._client_proxies.values()):
            try:
                await proxy.send_notification(types.ToolListChangedNotification())
                count += 1
            except Exception as e:  # noqa: BLE001
                log.debug("发送 tools/list_changed 失败: %s", e)
        log.info("已发送 tools/list_changed 通知（%d/%d 个会话）", count, len(self._client_proxies))

    # ------------------------------------------------------------------ hello 维护

    async def _hello_loop(self) -> None:
        """为尚未拿到 hello 的设备主动建连读取；失败按退避重试。"""
        while True:
            try:
                changed = False
                now_mono = time.monotonic()
                for dev in self.discovery.all():
                    if dev.hello is not None:
                        continue
                    # hello_fail_at=0 表示从未失败；系统刚开机时 time.monotonic()
                    # 可能还很小，不排除 0 会把全部设备误判为退避中
                    if dev.hello_fail_at > 0 and now_mono - dev.hello_fail_at < HELLO_RETRY_SEC:
                        continue
                    try:
                        async with self._hello_sema:
                            await self._refresh_hello(dev)
                        changed = True
                    except client.BmahsError as e:
                        dev.hello_fail_at = time.monotonic()
                        log.debug("读取设备 %s hello 失败: %s", dev.id, e)
                    except Exception as e:  # noqa: BLE001
                        dev.hello_fail_at = time.monotonic()
                        log.debug("读取设备 %s hello 异常: %s", dev.id, e)
                if changed and self._rebuild_tools():
                    await self._notify_tools_changed()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.debug("hello 维护循环异常: %s", e)
            await asyncio.sleep(HELLO_PRIME_INTERVAL)

    async def _refresh_hello(self, dev: Device) -> dict:
        """建连读取（必要时 describe 补全）并缓存设备 hello；工具表变化则通知客户端。"""
        if not dev.uri:
            raise GatewayError(f"设备 {dev.id} 当前没有可连的 control 地址，请稍后重新发现")
        hello = await client.fetch_hello(dev.uri, timeout=min(self.call_timeout, 8.0))
        # §4.5：args 仍是字符串数组的旧版（bmahs/1）设备，应再发 describe 要求完整清单
        if self._hello_is_legacy(hello):
            try:
                _, full = await client.call_action(dev.uri, {"action": "describe"}, timeout=self.call_timeout)
                if isinstance(full.get("operations"), list) or isinstance(full.get("ops"), list):
                    hello = full
            except Exception:  # noqa: BLE001 — 拿不到 describe 时按原 hello 降级使用
                pass
        dev = self.discovery.bind_hello(dev.key, hello) or dev
        dev.hello = hello
        dev.hello_at = time.monotonic()
        dev.hello_fail_at = 0.0
        if self._rebuild_tools():
            await self._notify_tools_changed()
        return hello

    async def _ensure_hello(self, dev: Device) -> dict:
        """取设备 hello，缓存缺失时现场补读一次；失败抛异常。"""
        if dev.hello is None:
            await self._refresh_hello(dev)
        assert dev.hello is not None
        return dev.hello

    @staticmethod
    def _hello_is_legacy(hello: dict) -> bool:
        """旧版字符串数组 args（仅名字，无类型/说明）检测（§4.5 兼容条款）。"""
        ops = hello.get("operations") or hello.get("ops") or []
        for op in ops:
            if isinstance(op, dict) and any(isinstance(a, str) for a in (op.get("args") or [])):
                return True
        return False

    # ------------------------------------------------------------------ 工具表

    def _tool_hidden(self, name: str, dev_id: str, action: str) -> bool:
        """BMAHS_TOOL_ALLOW / BMAHS_TOOL_DENY 过滤（支持通配符，只作用于动态工具）。"""
        targets = (name, f"{dev_id}__{action}", action)

        def matches(pattern: str) -> bool:
            return any(fnmatch.fnmatch(t, pattern) for t in targets)

        if any(matches(p) for p in self.tool_deny):
            return True
        if self.tool_allow:
            return not any(matches(p) for p in self.tool_allow)
        return False

    def _rebuild_tools(self) -> bool:
        """根据缓存 hello 重建工具名映射；返回签名是否变化（决定是否通知客户端）。"""
        mapping: dict[str, tuple[str, str]] = {}
        sig_parts: list[str] = []
        for dev in sorted(self.discovery.all(), key=lambda d: d.id):
            hello = dev.hello
            if not hello:
                continue
            if self.id_conflict_policy == "isolate" and dev.id_conflict:
                continue  # isolate：疑似 id 冲突的设备不暴露动态工具，防控制串台
            for op in hello.get("operations") or hello.get("ops") or []:
                if not isinstance(op, dict):
                    continue
                action = str(op.get("name") or "")
                if not action or action in P.GENERIC_ACTIONS:
                    continue  # 六个通用动作由网关静态工具统一提供，避免每台设备重复
                name = schemas.mcp_tool_name(dev.id, action)
                base, n = name, 2
                while name in mapping and mapping[name] != (dev.id, action):
                    suffix = f"-{n}"
                    name = base[: 64 - len(suffix)] + suffix
                    n += 1
                if self._tool_hidden(name, dev.id, action):
                    continue
                mapping[name] = (dev.id, action)
                desc_hash = hash(schemas.tool_description(hello, op)) & 0xFFFFFF
                sig_parts.append(f"{name}:{desc_hash}")
        sig = "|".join(sorted(sig_parts))
        changed = sig != self._tools_sig
        self.tool_map = mapping
        self._tools_sig = sig
        return changed

    # ------------------------------------------------------------------ 设备解析与控制

    def resolve_device(self, ref) -> Device:  # noqa: ANN001
        """把模型给的设备引用解析为 Device：先按 id 精确匹配 → 唯一同名 → 唯一子串模糊匹配。

        三级都落空时抛 GatewayError 并附上当前已知设备清单，引导模型自查。
        """
        ref = str(ref or "").strip()
        if not ref:
            raise GatewayError("未指定设备（请传设备 id 或显示名，可先用 bmahs_devices 查询）")
        dev = self.discovery.get(ref)
        if dev is None:
            named = [d for d in self.discovery.all() if d.name == ref]
            if len(named) == 1:
                dev = named[0]
        if dev is None:
            needle = ref.lower()
            fuzzy = [
                d
                for d in self.discovery.all()
                if needle in d.id.lower() or needle in d.name.lower()
            ]
            if len(fuzzy) == 1:
                dev = fuzzy[0]
        if dev is None:
            known = [
                f"{d.id}（{d.name}）" for d in self.discovery.all()
            ] or ["（局域网内暂无设备，可调用 bmahs_refresh 重新扫描）"]
            raise GatewayError(f"找不到设备 {ref!r}。当前已知设备：{'；'.join(known)}")
        return dev

    @staticmethod
    def _mask(obj):  # noqa: ANN001, ANN202
        """递归遮蔽 token：不回显给模型，也不进入会话记录（§4.8 第 9 条）。"""
        if isinstance(obj, dict):
            return {
                k: (TOKEN_PLACEHOLDER if k == "token" and isinstance(v, str) else Gateway._mask(v))
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [Gateway._mask(x) for x in obj]
        return obj

    def _require_ok(self, resp: dict) -> dict:
        """设备回 ok=false 信封时转成 DeviceEnvelope 抛出（由 server 层透传给模型）。"""
        if isinstance(resp, dict) and resp.get("ok") is False:
            raise DeviceEnvelope(resp)
        return resp

    async def _raw_call(self, dev: Device, payload: dict) -> tuple[dict, dict]:
        """对设备发一次原始动作请求（不自动占用/带 token），返回 (hello, 响应信封)。

        顺带把 TCP 可达当作存活信号刷新 last_seen，并用新 hello 更新自述缓存。
        """
        if not dev.uri:
            raise GatewayError(
                f"设备 {dev.id} 当前没有可连的 control 地址（可能刚换网），"
                "请稍后调用 bmahs_refresh 重新发现"
            )
        try:
            hello, resp = await client.call_action(dev.uri, payload, timeout=self.call_timeout)
        except client.BmahsError as e:
            raise GatewayError(str(e)) from e
        # TCP 可达即视为存活：刷新 last_seen，跨网段/组播静默设备不会被
        # 心跳过期误删（docs/跨网段发现-原因与方案.md §2.3）
        dev.last_seen = P.now()
        if isinstance(hello, dict) and hello.get("action") == "hello":
            dev.hello = hello
            dev.hello_at = time.monotonic()
            if self._rebuild_tools():
                await self._notify_tools_changed()
        return hello, resp

    def _guard_conflict(self, dev: Device) -> None:
        """isolate 策略下拒绝与疑似 id 冲突设备的控制交互；只读动作放行，便于诊断。"""
        if self.id_conflict_policy == "isolate" and dev.id_conflict:
            raise GatewayError(
                f"设备 {dev.id} 疑似 id 冲突（局域网内多个控制地址自称 {dev.id}，"
                f"观测到 {('、'.join(sorted(dev.controls_seen))) or '多个地址'}），"
                "已按 BMAHS_ID_CONFLICT_POLICY=isolate 隔离控制类动作；"
                "请人工核实并修改重复的设备 id 后重试"
            )

    async def _occupy(self, dev: Device, ttl=None, skey: str = "local") -> dict:
        """占用一台设备并把签发的 token 存入本会话；返回设备响应信封。

        ttl 缺省用自动占用租约，超上限截断（见下方行内注释）；
        已持有 token 时带上它以刷新租约，token 失效则去掉重占一次。
        last-wins 设备无需占用（1.1 §4.6.2）：不发网络包，直接返回提示信封，
        避免对未实现 occupy 空操作的设备收到 unknown-action。
        """
        if dev.occupancy == P.OCCUPANCY_LAST_WINS:
            return {
                "ok": True,
                "action": "occupy",
                "occupancy": P.OCCUPANCY_LAST_WINS,
                "note": "该设备为 last-wins（最后控制生效）：无需占用，直接调用业务动作即可，最后一条命令自动生效。",
            }
        self._guard_conflict(dev)
        agent = self.agent_for(skey)
        payload: dict = {"action": "occupy", "agent": agent}
        # 网关租约策略：不允许无限期占用。不带 ttl 用默认有限租约（120s），
        # 显式请求值超过上限截断到上限（默认 3600s），9999（无限期）一律截断。
        if ttl is None:
            ttl = self.auto_occupy_ttl
        if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 10:
            raise GatewayError("ttl 必须是 ≥10 的整数秒")
        # 过长（含 9999 无限期）一律截断到网关上限，不拒绝也不放行
        if ttl >= 9999 or ttl > self.max_lease:
            ttl = self.max_lease
        payload["ttl"] = int(ttl)
        held = self._token(skey, dev.id)
        if held:
            payload["token"] = held  # 已持有时带上以便刷新租约
        _, resp = await self._raw_call(dev, payload)
        if resp.get("code") == "unauthorized" and held:
            # 旧 token 已作废（设备重启 / 租约被接管）：去掉 token 重新占用
            payload.pop("token", None)
            _, resp = await self._raw_call(dev, payload)
        if resp.get("ok") and resp.get("token"):
            self._set_token(skey, dev.id, str(resp["token"]))
        return resp

    async def _release(self, dev: Device, skey: str = "local") -> dict:
        """释放本会话对设备的占用；未持有 token 直接返回提示信封，token 已失效视同已释放。

        last-wins 设备无占用关系可释放（1.1 §4.6.2）：不发网络包，返回提示信封。
        """
        if dev.occupancy == P.OCCUPANCY_LAST_WINS:
            return {
                "ok": True,
                "action": "release",
                "occupancy": P.OCCUPANCY_LAST_WINS,
                "note": "该设备为 last-wins：没有占用关系，无需释放；后续控制会自然覆盖先前控制。",
            }
        token = self._pop_token(skey, dev.id)
        if not token:
            return {
                "ok": False,
                "action": "release",
                "code": "no-token",
                "error": "当前会话未持有该设备的占用 token，无需释放",
                "retryable": False,
            }
        _, resp = await self._raw_call(
            dev, {"action": "release", "agent": self.agent_for(skey), "token": token}
        )
        if resp.get("ok") is False and resp.get("code") == "unauthorized":
            return {
                "ok": True,
                "action": "release",
                "state": "online",
                "event": "release",
                "note": "原 token 已失效（设备重启或租约变化），视同已释放",
            }
        return resp

    async def _send_control(
        self, dev: Device, action: str, extra: dict | None = None, skey: str = "local"
    ) -> dict:
        """发送一条动作请求；按设备占用策略（1.1 §4.6）选择控制序列。

        - ``last-wins``：直接发业务动作 + ``agent``，不 occupy、不带 token，
          也不做 unauthorized 重试（§4.8-4：对 last-wins 不得再自动 occupy）；
        - ``exclusive``：按需自动占用并携带 token，token 失效自动重占用一次。
        """
        extra = dict(extra or {})
        if action in P.READONLY_ACTIONS:
            _, resp = await self._raw_call(
                dev, {"action": action, "agent": self.agent_for(skey), **extra}
            )
            return resp
        self._guard_conflict(dev)
        if dev.occupancy == P.OCCUPANCY_LAST_WINS:
            _, resp = await self._raw_call(
                dev, {"action": action, **extra, "agent": self.agent_for(skey)}
            )
            return resp
        token = self._token(skey, dev.id)
        if token is None:
            if not self.auto_occupy:
                raise GatewayError(
                    f"设备 {dev.id} 尚未被本会话占用（BMAHS_AUTO_OCCUPY=0 时须先调用 bmahs_occupy）"
                )
            occ = await self._occupy(dev, self.auto_occupy_ttl, skey)
            if not occ.get("ok"):
                return occ  # occupied / offline 等设备信封原样返回
            token = self._token(skey, dev.id)
        payload = {"action": action, **extra, "agent": self.agent_for(skey)}
        if token:
            payload["token"] = token
        _, resp = await self._raw_call(dev, payload)
        if resp.get("code") == "unauthorized" and token:
            self._pop_token(skey, dev.id)
            occ = await self._occupy(dev, None, skey)
            if not occ.get("ok"):
                return occ  # 重占用失败（如已被他人占用）：返回最新信封而非旧 unauthorized
            payload["token"] = self._token(skey, dev.id)
            _, resp = await self._raw_call(dev, payload)
        return resp

    # ------------------------------------------------------------------ MCP: list_tools

    async def list_tools(self) -> list[types.Tool]:
        """组装 MCP 工具表：7 个固定工具 + 每台设备的每个非通用动作一个动态工具。"""
        tools: list[types.Tool] = [
            types.Tool(
                name="bmahs_devices",
                description=(
                    "列出当前发现的全部 BMAHS 设备（id、显示名、自然语言自述、类型、状态、"
                    "占用方与连接地址）。选设备、查占用状态时先用这个工具。"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "description": "按品类过滤（可选），如 light、display",
                        }
                    },
                    "additionalProperties": False,
                },
                annotations=types.ToolAnnotations(readOnlyHint=True),
            ),
            types.Tool(
                name="bmahs_refresh",
                description=(
                    "重新扫描 BMAHS 设备：发送组播 query 并刷新各设备的自述（hello）。"
                    "当设备列表为空、新设备刚上电、或怀疑列表过期时调用。"
                ),
                inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
                annotations=types.ToolAnnotations(readOnlyHint=True),
            ),
            types.Tool(
                name="bmahs_describe",
                description="读取某台 BMAHS 设备的完整操作清单（operations）、安全边界（security）与自然语言自述。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "device": {"type": "string", "description": "设备 id 或显示名"}
                    },
                    "required": ["device"],
                    "additionalProperties": False,
                },
                annotations=types.ToolAnnotations(readOnlyHint=True),
            ),
            types.Tool(
                name="bmahs_occupy",
                description=(
                    "独占占用一台 BMAHS 设备（返回的 token 由网关保存并自动携带）。"
                    "仅对 occupancy=exclusive 的设备有意义；last-wins 设备无需占用，"
                    "调用时网关会直接返回提示而不发网络请求。"
                    "租约由网关强制为有限时长：不带 ttl 默认 120 秒（2 分钟），请求值超过"
                    "上限（默认 3600 秒）会被截断，不支持无限期占用。租约到期设备自动收回"
                    "占用权；任务结束也可调用 bmahs_release 提前释放。"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "device": {
                            "type": "string",
                            "description": "设备 id 或显示名",
                        },
                        "ttl": {
                            "type": "integer",
                            "minimum": 10,
                            "maximum": 9998,
                            "description": "租约秒数（默认 120=2 分钟；超过上限会被截断）",
                        },
                    },
                    "required": ["device"],
                    "additionalProperties": False,
                },
            ),
            types.Tool(
                name="bmahs_release",
                description=(
                    "释放对某台 BMAHS 设备的占用（仅 occupancy=exclusive 的设备需要）。"
                    "任务结束、失败或取消后必须调用，否则其它智能体会一直收到「被占用」；"
                    "last-wins 设备无需释放。"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "device": {"type": "string", "description": "设备 id 或显示名"}
                    },
                    "required": ["device"],
                    "additionalProperties": False,
                },
            ),
            types.Tool(
                name="bmahs_call",
                description=(
                    "对 BMAHS 设备执行任意其操作清单内（operations）的动作，参数按该动作的 args 传。"
                    "适合调用尚未生成独立工具的动作，或临时查看新设备。"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "device": {
                            "type": "string",
                            "description": "设备 id 或显示名",
                        },
                        "action": {
                            "type": "string",
                            "description": "动作名，必须在设备 operations 清单中",
                        },
                        "args": {
                            "type": "object",
                            "description": "动作参数（按该设备 operations 中该动作 args 的字段名与类型）",
                        },
                    },
                    "required": ["device", "action"],
                    "additionalProperties": False,
                },
            ),
            types.Tool(
                name="bmahs_screenshot",
                description=(
                    "（实验）对声明了 ui 能力的 BMAHS 设备抓取一帧当前画面：自动 ui.start → "
                    "二进制流取一帧 JPEG → ui.stop，返回图片与保存路径。"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "device": {
                            "type": "string",
                            "description": "设备 id 或显示名",
                        },
                        "max_width": {
                            "type": "integer",
                            "minimum": 64,
                            "description": "期望画面最大宽度（像素），设备按自身能力缩放",
                        },
                    },
                    "required": ["device"],
                    "additionalProperties": False,
                },
            ),
        ]
        for name, (dev_id, action) in sorted(self.tool_map.items()):
            dev = self.discovery.get(dev_id)
            if dev is None or not dev.hello:
                continue
            op = schemas.find_op(dev.hello, action)
            if op is None:
                continue
            tools.append(
                types.Tool(
                    name=name,
                    description=self._tool_description_guarded(dev, op),
                    inputSchema=schemas.input_schema(op),
                )
            )
        return tools

    def _tool_description_guarded(self, dev, op: dict) -> str:  # noqa: ANN001
        """组装动态工具描述；warn 策略下为疑似 id 冲突的设备追加警示行。"""
        desc = schemas.tool_description(dev.hello, op)
        if dev.id_conflict and self.id_conflict_policy == "warn":
            desc += (
                "\n⚠️ 该设备 id 在局域网内观测到多个控制地址（疑似 id 冲突），"
                "控制结果可能并非总是命中同一台实体设备，建议人工核实后再依赖。"
            )
        return desc

    # ------------------------------------------------------------------ MCP: call_tool

    async def call_tool(self, name: str, arguments: dict | None, skey: str = "local") -> list:
        """MCP 工具调用总入口：先路由固定工具，再按 tool_map 路由到具体设备动作。

        动态动作执行前校验 any_of（至少提供一个参数）约束；所有响应经 _mask
        遮蔽 token、经 _require_ok 校验后以文本内容返回。
        """
        arguments = dict(arguments or {})
        if name == "bmahs_devices":
            return self._text(await self.tool_devices(arguments.get("type")))
        if name == "bmahs_refresh":
            return self._text(await self.tool_refresh())
        if name == "bmahs_describe":
            dev = self.resolve_device(arguments.get("device"))
            await self._ensure_hello(dev)
            _, resp = await self._raw_call(
                dev, {"action": "describe", "agent": self.agent_for(skey)}
            )
            return self._text(self._require_ok(resp))
        if name == "bmahs_occupy":
            dev = self.resolve_device(arguments.get("device"))
            return self._text(
                self._require_ok(
                    self._mask(await self._occupy(dev, arguments.get("ttl"), skey))
                )
            )
        if name == "bmahs_release":
            dev = self.resolve_device(arguments.get("device"))
            return self._text(
                self._require_ok(self._mask(await self._release(dev, skey)))
            )
        if name == "bmahs_call":
            dev = self.resolve_device(arguments.get("device"))
            action = str(arguments.get("action") or "").strip()
            if not action:
                raise GatewayError("缺少 action 参数")
            args = arguments.get("args")
            if args is not None and not isinstance(args, dict):
                raise GatewayError("args 必须是对象（键为该动作的参数名）")
            return self._text(
                self._require_ok(
                    self._mask(await self._send_control(dev, action, args, skey))
                )
            )
        if name == "bmahs_screenshot":
            dev = self.resolve_device(arguments.get("device"))
            return await self.tool_screenshot(dev, arguments.get("max_width"), skey)
        entry = self.tool_map.get(name)
        if entry is None:
            raise MCPError(
                INVALID_PARAMS,
                f"未知工具：{name}（设备列表可能已变化，可调用 bmahs_refresh 后重试）",
            )
        dev_id, action = entry
        dev = self.discovery.get(dev_id)
        if dev is None:
            raise GatewayError(f"设备 {dev_id} 已下线，请调用 bmahs_refresh 刷新列表")
        hello = await self._ensure_hello(dev)
        op = schemas.find_op(hello, action)
        if op is None:
            raise GatewayError(f"设备 {dev_id} 的操作清单中已没有 {action}（设备能力可能已更新）")
        any_of = op.get("any_of") or []
        if any_of and not any(a in arguments for a in any_of):
            raise DeviceEnvelope(
                {
                    "ok": False,
                    "action": action,
                    "code": "bad-arg",
                    "error": "参数 "
                    + "、".join(f"「{a}」" for a in any_of)
                    + " 至少需要提供一个（二选一/多选一约束，见工具说明）",
                    "retryable": False,
                }
            )
        return self._text(
            self._require_ok(
                self._mask(await self._send_control(dev, action, arguments, skey))
            )
        )

    # ------------------------------------------------------------------ 静态工具实现

    async def tool_devices(self, type_filter=None) -> dict:  # noqa: ANN001
        """bmahs_devices：列出全部设备（含占用状态、动态工具名、hello 是否就绪）。"""
        await self.query_once()
        items = []
        now_s = P.now()
        for dev in sorted(self.discovery.all(), key=lambda d: d.id):
            if type_filter and str(type_filter).lower() != self._dev_type(dev).lower():
                continue
            hello = dev.hello or {}
            tools = sorted(n for n, (d, _) in self.tool_map.items() if d == dev.id)
            items.append(
                {
                    "id": dev.id,
                    "name": dev.name,
                    "summary": hello.get("summary") or dev.announce.get("summary") or "",
                    "type": self._dev_type(dev),
                    "service": hello.get("service") or hello.get("svc") or dev.announce.get("service") or "",
                    "protocol": dev.protocol or None,
                    "occupancy": dev.occupancy,
                    "state": dev.state,
                    "online": dev.online,
                    "busy": dev.busy,
                    "holder": dev.holder,
                    "until": dev.until or None,
                    "occupied_by_gateway": any(
                        dev.id in toks for toks in self.tokens.values()
                    ),
                    "control": dev.uri,
                    "id_conflict": dev.id_conflict,
                    "conflict_controls": sorted(dev.controls_seen) if dev.id_conflict else [],
                    "model": hello.get("model") or dev.announce.get("model") or "",
                    "last_seen_age_sec": max(0, now_s - dev.last_seen) if dev.last_seen else None,
                    "source": dev.source,
                    "ops_ready": bool(dev.hello),
                    "tool_names": tools,
                    "hint": hello.get("hint") or "",
                }
            )
        return {
            "ok": True,
            "count": len(items),
            "devices": items,
            "note": (
                "occupancy=exclusive 的设备：控制类动作前网关会自动 occupy（默认 120 秒有限租约，"
                "可用 BMAHS_AUTO_OCCUPY_TTL 调整）并携带 token，任务结束请 bmahs_release；"
                "occupancy=last-wins 的设备：无需占用/释放，直接调用业务动作，最后一条命令生效。"
                "ops_ready=false 的设备稍后自动就绪，或调用 bmahs_refresh。"
                "id_conflict=true 的设备：局域网内观测到多个控制地址自称同一 id（可能是两台设备撞 id，"
                "也可能是同一设备多网卡），控制结果可能不确定，建议先人工核实。"
            ),
        }

    async def tool_refresh(self) -> dict:
        """bmahs_refresh：重发 query → 等待 announce → 为 hello 缺失/过期的设备补读 → 返回设备快照。"""
        await self.query_once()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            devs = self.discovery.all()
            if devs and all(dev.hello is not None for dev in devs):
                break
            await asyncio.sleep(0.3)
        stale = [
            dev
            for dev in self.discovery.all()
            if dev.hello is None or time.monotonic() - dev.hello_at > HELLO_STALE_SEC
        ]
        results = await asyncio.gather(
            *(self._safe_refresh_hello(dev) for dev in stale), return_exceptions=True
        )
        failures = sum(1 for r in results if isinstance(r, Exception))
        snapshot = await self.tool_devices()
        snapshot["refreshed"] = len(stale) - failures
        snapshot["refresh_failures"] = failures
        return snapshot

    async def query_once(self) -> None:
        """快速扫描：连发两次 query（间隔 0.2 秒防丢包），给设备留出应答窗口。"""
        await self.discovery.query()
        await asyncio.sleep(0.2)
        await self.discovery.query()

    async def _safe_refresh_hello(self, dev: Device) -> None:
        """限流版的 hello 刷新，供 tool_refresh 并发批量调用。"""
        async with self._hello_sema:
            await self._refresh_hello(dev)

    @staticmethod
    def _dev_type(dev: Device) -> str:
        """设备品类（light/display/switch…），优先 hello，其次 announce。"""
        return str((dev.hello or dev.announce).get("type") or "")

    # ------------------------------------------------------------------ 截图（ui 剖面 §4.9）

    async def tool_screenshot(self, dev: Device, max_width=None, skey: str = "local") -> list:
        """bmahs_screenshot：抓一帧设备画面（ui.start → 读一帧二进制流 → ui.stop）。

        返回 [文本元数据, JPEG 图片内容] 两个内容块，帧同时落盘到 capture_dir；
        设备未声明 ui 能力、未占用或流失败时抛 GatewayError / DeviceEnvelope。
        """
        hello = await self._ensure_hello(dev)
        op = schemas.find_op(hello, "ui.start")
        if op is None:
            raise GatewayError(f"设备 {dev.id} 未声明 ui 能力（operations 中没有 ui.start），无法抓屏")
        if self._token(skey, dev.id) is None:
            if not self.auto_occupy:
                raise GatewayError("抓屏前须先 bmahs_occupy（ui 动作要求携带占用 token）")
            occ = await self._occupy(dev, self.auto_occupy_ttl, skey)
            if not occ.get("ok"):
                raise DeviceEnvelope(occ)
        token = self._token(skey, dev.id)
        if token is None:
            # 设备回了 ok 却没签发 token（协议违规）：避免后续 KeyError
            raise GatewayError(f"设备 {dev.id} 占用成功但未返回 token（设备协议实现有误），无法抓屏")
        extra: dict = {}
        arg_names = {str(a.get("name")): a for a in schemas.normalize_args(op)}
        if "codec" in arg_names:
            extra["codec"] = "jpeg"
        if "max_width" in arg_names and max_width:
            spec = arg_names["max_width"]
            try:
                w = int(max_width)
            except (TypeError, ValueError):
                raise GatewayError("max_width 必须是整数") from None
            if isinstance(spec.get("min"), (int, float)):
                w = max(w, int(spec["min"]))
            if isinstance(spec.get("max"), (int, float)):
                w = min(w, int(spec["max"]))
            extra["max_width"] = w
        _, resp = await self._raw_call(
            dev,
            {"action": "ui.start", **extra, "agent": self.agent_for(skey), "token": token},
        )
        if not resp.get("ok"):
            raise DeviceEnvelope(resp)
        ui_uri = resp.get("ui")
        try:
            codec, width, height, frame = await client.read_ui_frame(ui_uri, token)
        except client.BmahsError as e:
            await self._safe_ui_stop(dev, skey)
            raise DeviceEnvelope(
                {
                    "ok": False,
                    "action": "screenshot",
                    "code": "ui-stream",
                    "error": str(e),
                    "retryable": True,
                }
            ) from e
        await self._safe_ui_stop(dev, skey)
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        ext = "jpg" if codec == 1 else "h264"
        path = self.capture_dir / f"{dev.id}_{int(time.time())}.{ext}"
        path.write_bytes(frame)
        blocks: list = [
            types.TextContent(
                type="text",
                text=json.dumps(
                    {
                        "ok": True,
                        "action": "screenshot",
                        "device": dev.id,
                        "width": width,
                        "height": height,
                        "codec": "jpeg" if codec == 1 else f"codec-{codec}",
                        "saved_to": str(path),
                        "bytes": len(frame),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]
        if codec == 1:
            blocks.append(
                types.ImageContent(
                    type="image",
                    data=base64.b64encode(frame).decode("ascii"),
                    mimeType="image/jpeg",
                )
            )
        return blocks

    async def _safe_ui_stop(self, dev: Device, skey: str = "local") -> None:
        """尽力停止设备的 UI 流会话：失败只记 debug，不影响主流程。"""
        token = self._token(skey, dev.id)
        if token is None or not dev.uri:
            return
        try:
            await asyncio.wait_for(
                client.call_action(
                    dev.uri,
                    {"action": "ui.stop", "agent": self.agent_for(skey), "token": token},
                    timeout=5.0,
                ),
                timeout=8.0,
            )
        except Exception as e:  # noqa: BLE001
            log.debug("ui.stop 失败（忽略）: %s", e)

    # ------------------------------------------------------------------ 输出

    @staticmethod
    def _text(obj) -> list:  # noqa: ANN001
        """把 dict/str 包装成 MCP 文本内容块（工具返回值的标准出口）。"""
        text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=2)
        return [types.TextContent(type="text", text=text)]
