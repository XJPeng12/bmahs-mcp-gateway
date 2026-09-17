"""Bonjour / DNS-SD 助手（协议 §3.2 / §6.4，应当实现）。

两个角色，共用一套线程隔离设施（见下）：

- 设备端 ``BonjourAdvertiser``：注册 ``_bmahs._tcp``。实例名 = 设备显示名，
  端口 = control 端口，TXT 与 UDP ``announce`` 字段对齐。地址或摘要变化时调
  ``update`` 重注册（§3.3：不得长期保留过期 TXT ip=）。
- 智能体端 ``BonjourBrowser``：浏览 ``_bmahs._tcp``，把服务增删事件回调给
  发现层（协议 §3.2：智能体应发送 query 并/或浏览 Bonjour）。这是组播之外
  的第二发现通道：UDP 组播静默但 mDNS/TCP 正常的设备（多网卡绑错接口的
  常见病，docs/组播发现失败-原因与排查.md）也能进入注册表。

崩溃隔离（重要）：zeroconf 在 Windows/Proactor 上会把 mDNS socket 绑到
构造时枚举的全部本机地址；网卡变化时这些 socket 可能报 WinError 59，
异常从 asyncio 回调冒出，**连带杀死整个进程**。因此 AsyncZeroconf 跑在
独立守护线程的事件循环里：网络栈异常最多终止该线程（Bonjour 降级失效），
设备进程与 UDP 组播发现不受影响。

zeroconf 缺失或注册失败同样不致命：方法返回 False，设备照常走 UDP 发现
（§3.1 是必须项，本节是应当项）。
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time
from typing import Any

log = logging.getLogger("bmahs.bonjour")

SERVICE_TYPE = "_bmahs._tcp.local."
TXTVERS = "1"
_CALL_TIMEOUT = 10.0


def build_txt(props: dict[str, Any]) -> dict[str, str]:
    """按 §6.4 把 announce 摘要转成 TXT 键值（全部字符串，capabilities 逗号分隔）。"""
    caps = props.get("capabilities") or []
    sec = props.get("security") or {}
    txt: dict[str, str] = {
        "txtvers": TXTVERS,
        "protocol": str(props.get("protocol", "")),
        "type": str(props.get("type", "")),
        "service": str(props.get("service", "")),
        "id": str(props.get("id", "")),
        "name": str(props.get("name", "")),
        "capabilities": ",".join(str(c) for c in caps),
        "security": f"{sec.get('scope', '')},{sec.get('auth', '')}",
        "state": str(props.get("state", "registered")),
        "busy": "1" if props.get("busy") else "0",
    }
    if props.get("summary"):
        # TXT 建议整包 < 400 字节：summary 截断到 80 字，完整语义以 TCP hello 为准
        txt["summary"] = str(props["summary"])[:80]
    if props.get("model"):
        txt["model"] = str(props["model"])
    if props.get("event"):
        txt["event"] = str(props["event"])
    if props.get("ip"):
        txt["ip"] = str(props["ip"])
    if props.get("ipv6"):
        txt["ipv6"] = str(props["ipv6"])
    if props.get("hb"):
        txt["hb"] = str(props["hb"])
    if props.get("holder"):
        txt["holder"] = str(props["holder"])
    return txt


def txt_to_announce(txt: dict, host: str, port: int) -> dict:
    """把 ``_bmahs._tcp`` 的 TXT 记录 + SRV 地址还原为 announce 摘要（build_txt 的逆）。

    供智能体端 Bonjour 浏览通道使用：设备 TCP hello 到手前，注册表里也能展示
    name/summary/type 等摘要。字段缺失一律留空，完整自述以 TCP hello 为准；
    键值容忍 bytes（zeroconf 文本记录的原始形态）。
    """

    def s(v: Any) -> str:
        if isinstance(v, bytes):
            return v.decode("utf-8", "replace")
        return "" if v is None else str(v)

    norm = {s(k): v for k, v in txt.items()}
    sec = s(norm.get("security")).split(",", 1)
    hb = s(norm.get("hb"))
    return {
        # 来源标记：真 UDP announce 的 kind 是 "announce"；注册表靠它区分
        # 「UDP 见过的设备」与「只有 Bonjour 见过的设备」（影响下线/过期语义）
        "kind": "bonjour",
        "protocol": s(norm.get("protocol")),
        "type": s(norm.get("type")),
        "service": s(norm.get("service")),
        "id": s(norm.get("id")),
        "name": s(norm.get("name")),
        "summary": s(norm.get("summary")),
        "model": s(norm.get("model")),
        "control": f"tcp://{host}:{port}",
        "capabilities": [c for c in s(norm.get("capabilities")).split(",") if c],
        "security": {"scope": sec[0], "auth": sec[1]} if len(sec) == 2 else {},
        "state": s(norm.get("state")) or "registered",
        "busy": s(norm.get("busy")) == "1",
        "hb": int(hb) if hb.isdigit() else None,
    }


class _IsolatedLoop:
    """守护线程 + 独立事件循环，Bonjour 设施的公共底座。

    崩溃隔离（重要）：zeroconf 在 Windows/Proactor 上会把 mDNS socket 绑到
    构造时枚举的全部本机地址；网卡变化时这些 socket 可能报 WinError 59，
    异常从 asyncio 回调冒出，**连带杀死整个进程**。因此 AsyncZeroconf 跑在
    独立守护线程的事件循环里：网络栈异常最多终止该线程（Bonjour 降级失效），
    其余功能不受影响。
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _thread_died(self) -> None:
        """线程退出后的子类钩子（默认无操作）。"""

    def _ensure_thread(self) -> asyncio.AbstractEventLoop | None:
        """拿到（或启动）Bonjour 专用线程的事件循环；线程不可用返回 None = 降级。"""
        if self._thread is not None and not self._thread.is_alive():
            # 上一条线程已因网络栈异常退出：Bonjour 降级失效，不再重启
            # （重启会立刻在同一批地址上再崩一次）；设备继续走 UDP 发现。
            self._loop = None
            self._thread_died()
            return None
        if self._thread is not None:
            # 线程存活但 loop 可能尚未就绪（启动窗口）：等它就绪，而不是再开
            # 一条线程——后者会泄漏旧线程并让 self._loop 被覆盖
            deadline = time.monotonic() + 2.0
            while self._loop is None and time.monotonic() < deadline and self._thread.is_alive():
                time.sleep(0.01)
            return self._loop
        self._thread = threading.Thread(target=self._thread_main, name="bmahs-bonjour", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 5.0
        while self._loop is None and time.monotonic() < deadline:
            if not self._thread.is_alive():
                return None
            time.sleep(0.01)
        return self._loop

    def _thread_main(self) -> None:
        """Bonjour 线程主体：建独立事件循环并常驻；崩溃只带走本线程。"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_forever()
        except Exception as e:  # noqa: BLE001 — 网络栈异常（WinError 59 等）只终止本线程
            log.warning("Bonjour 线程已退出（不影响 UDP 组播发现）：%r", e)
        finally:
            self._thread_died()
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass

    def _call(self, coro) -> Any:  # noqa: ANN001
        """把协程投递到隔离线程的事件循环同步等待执行；任何失败都返回 None（不抛出）。"""
        loop = self._ensure_thread()
        if loop is None:
            return None
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return fut.result(timeout=_CALL_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            log.warning("Bonjour 调用失败（不影响 UDP 发现）：%r", e)
            return None


class BonjourAdvertiser(_IsolatedLoop):
    """可选的 _bmahs._tcp 注册器；任何失败都只降级、不抛出、不拖垮进程。"""

    def __init__(self) -> None:
        super().__init__()
        self._alive = False

    @property
    def active(self) -> bool:
        """Bonjour 注册当前是否生效（False 不影响 UDP 发现，仅少了 mDNS 入口）。"""
        return self._alive

    def _thread_died(self) -> None:
        self._alive = False

    # ------------------------------------------------------------------ 公共接口（async，供设备骨架 await）

    async def advertise(self, props: dict[str, Any], port: int, addresses: list[str]) -> bool:
        """注册或更新服务；addresses 为本机局域网 IPv4 字符串列表。"""
        try:
            from zeroconf import ServiceInfo  # noqa: F401 — 提前探依赖，缺失直接降级
            from zeroconf.asyncio import AsyncZeroconf  # noqa: F401
        except ImportError:
            log.info("未安装 zeroconf，跳过 Bonjour 注册（不影响 UDP 组播发现）")
            return False
        ok = self._call(self._advertise_inner(props, port, list(addresses)))
        self._alive = bool(ok)
        return self._alive

    async def _advertise_inner(self, props: dict[str, Any], port: int, addresses: list[str]) -> bool:
        """在隔离线程的事件循环里执行（AsyncZeroconf 须在有运行的 loop 的线程创建）。"""
        from zeroconf import ServiceInfo
        from zeroconf.asyncio import AsyncZeroconf

        if not getattr(self, "_azc", None):
            self._azc = AsyncZeroconf()
        # 新版 zeroconf 无 IPAddress 类：addresses 直接收 IPv4 的 4 字节网络序
        addrs = [socket.inet_aton(a) for a in addresses if a]
        if not addrs:
            return False
        name = str(props.get("name") or props.get("id") or "bmahs-device")
        server = f"{props.get('id') or 'bmahs-device'}.local."
        info = ServiceInfo(
            SERVICE_TYPE,
            f"{name}.{SERVICE_TYPE}",
            addresses=addrs,
            port=port,
            server=server,
            properties=build_txt(props),
        )
        if getattr(self, "_info", None) is not None:
            await self._azc.async_unregister_service(self._info)
        await self._azc.async_register_service(info, ttl=60)
        self._info = info
        return True

    async def update(self, props: dict[str, Any], port: int, addresses: list[str]) -> bool:
        """地址 / 摘要变化时刷新注册（§3.3）。"""
        return await self.advertise(props, port, addresses)

    async def shutdown(self) -> None:
        """注销服务并关闭 zeroconf；失败静默（进程本就要退出了）。"""
        self._call(self._shutdown_inner())

    async def _shutdown_inner(self) -> None:
        """在隔离线程里执行注销与资源回收，最后停掉该线程的事件循环。"""
        azc = getattr(self, "_azc", None)
        info = getattr(self, "_info", None)
        try:
            if info is not None and azc is not None:
                await azc.async_unregister_service(info)
            if azc is not None:
                await azc.async_close()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._info = None
            self._azc = None
        # 给 zeroconf 内部的 goodbye 广播任务留出发包时间，避免 loop 停早了报 pending
        await asyncio.sleep(0.3)
        loop = asyncio.get_running_loop()
        loop.call_soon_threadsafe(loop.stop)


class _BrowserListener:
    """AsyncServiceBrowser 监听器：解析 SRV/TXT 后交回 BonjourBrowser。

    注意 zeroconf 0.151 的分发表只调同步方法 ``add_service`` /
    ``update_service`` / ``remove_service``（``async_*`` 监听器方法不再被识别）；
    回调在 Bonjour 隔离线程的事件循环里执行，解析（async_request 须 await）
    用 create_task 调度，失败只丢这一次事件（下一次 update 会重试）。
    """

    def __init__(self, owner: "BonjourBrowser") -> None:
        self._owner = owner

    def add_service(self, zeroconf, type_, name) -> None:  # noqa: ANN001
        self._schedule(zeroconf, type_, name)

    def update_service(self, zeroconf, type_, name) -> None:  # noqa: ANN001
        self._schedule(zeroconf, type_, name)

    def remove_service(self, zeroconf, type_, name) -> None:  # noqa: ANN001
        self._owner._service_removed(name)

    def _schedule(self, zeroconf, type_, name) -> None:  # noqa: ANN001
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # 不在事件循环里（不会发生）：丢这一次事件
        loop.create_task(self._resolve(zeroconf, type_, name))

    async def _resolve(self, zeroconf, type_, name) -> None:  # noqa: ANN001
        from zeroconf.asyncio import AsyncServiceInfo

        info = AsyncServiceInfo(type_, name)
        try:
            if not await info.async_request(zeroconf, 2500):
                return  # 没人应答（服务刚消失等）：等下一次 update 再试
        except Exception as e:  # noqa: BLE001
            log.debug("解析 Bonjour 服务 %s 失败: %s", name, e)
            return
        addrs = info.parsed_addresses() or []
        # 优先 IPv4：BMAHS control 层公告的是 IPv4 地址
        host = next((a for a in addrs if ":" not in a), addrs[0] if addrs else "")
        if not host or not info.port:
            return
        self._owner._service_added(name, host, int(info.port), dict(info.decoded_properties or {}))


class BonjourBrowser(_IsolatedLoop):
    """浏览局域网内的 ``_bmahs._tcp``（协议 §3.2 智能体端「应当浏览」）。

    第二发现通道：UDP 组播静默但 mDNS/TCP 正常的设备（多网卡绑错接口等，
    docs/组播发现失败-原因与排查.md §5.5）也能进入注册表。服务事件经
    ``call_soon_threadsafe`` 投递回网关主循环；zeroconf 缺失或浏览失败只降级
    （start 返回 False），不影响 UDP 组播发现。
    """

    def __init__(self) -> None:
        super().__init__()
        self._on_add = None  # Callable[[str, int, dict], None] | None
        self._on_remove = None  # Callable[[str, str, str, int], None] | None
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._browser = None
        self._azc = None
        # 服务实例名 -> (host, port, 设备id)：remove 事件只给名字，靠它找回设备
        self._seen: dict[str, tuple[str, int, str]] = {}

    @property
    def active(self) -> bool:
        """浏览当前是否生效（False 不影响 UDP 发现，仅少了 Bonjour 通道）。"""
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------ 公共接口（async）

    async def start(self, on_add, on_remove) -> bool:  # noqa: ANN001
        """开始浏览。

        ``on_add(host, port, txt)`` / ``on_remove(name, dev_id, host, port)``
        都在调用方的事件循环线程执行。返回 False 表示 zeroconf 不可用（降级）。
        """
        try:
            from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf  # noqa: F401 — 探依赖
        except ImportError:
            log.info("未安装 zeroconf，跳过 Bonjour 浏览（不影响 UDP 组播发现）")
            return False
        self._on_add = on_add
        self._on_remove = on_remove
        self._main_loop = asyncio.get_running_loop()
        return bool(self._call(self._browse_inner()))

    async def stop(self) -> None:
        """停浏览、关 zeroconf、结束隔离线程；失败静默（进程本就要退出了）。"""
        self._call(self._stop_inner())

    # ------------------------------------------------------------------ 隔离线程内执行

    async def _browse_inner(self) -> bool:
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf

        azc = AsyncZeroconf()
        self._azc = azc
        self._browser = AsyncServiceBrowser(azc.zeroconf, SERVICE_TYPE, _BrowserListener(self))
        return True

    async def _stop_inner(self) -> None:
        browser, azc = self._browser, self._azc
        self._browser = self._azc = None
        try:
            if browser is not None:
                await browser.async_cancel()
            if azc is not None:
                await azc.async_close()
        except Exception:  # noqa: BLE001
            pass
        # 取消尚未完成的解析任务，避免停线程时留下 pending 告警
        for t in [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]:
            t.cancel()
        await asyncio.sleep(0.2)
        loop = asyncio.get_running_loop()
        loop.call_soon_threadsafe(loop.stop)

    # ------------------------------------------------------------------ 事件投递（bonjour 线程 → 主循环）

    def _service_added(self, name: str, host: str, port: int, txt: dict) -> None:
        dev_id = str(txt.get("id") or "")
        self._seen[name] = (host, port, dev_id)
        self._emit(lambda: self._on_add and self._on_add(host, port, txt))

    def _service_removed(self, name: str) -> None:
        host, port, dev_id = self._seen.pop(name, ("", 0, ""))
        self._emit(lambda: self._on_remove and self._on_remove(name, dev_id, host, port))

    def _emit(self, fn) -> None:  # noqa: ANN001
        """把事件投递回网关主循环（线程安全）；主循环已关则丢弃。"""
        loop = self._main_loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(fn)
            except RuntimeError:
                pass
