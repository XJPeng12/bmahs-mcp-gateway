"""BMAHS 发现层客户端（协议 §3/§5）。

网关扮演协议中的「智能体」角色：
- 发送 ``query``（启动时 + 周期性）；
- 监听组播，收集 ``announce``（按 id 去重）、``goodbye``（移除）；
- 浏览 Bonjour ``_bmahs._tcp``（协议 §3.2「应当浏览」）：TXT 摘要入库、
  服务下线即移除——组播静默但 mDNS/TCP 正常的设备的第二发现通道；
- 超过无心跳删除时限（默认 30 分钟）未更新的设备从列表删除（§4.8 第 7 条；
  静态/Bonjour 设备除外，后者的生命周期由登记值 / mDNS 记录驱动）。

另支持通过环境变量 ``BMAHS_STATIC_DEVICES``（逗号分隔的 tcp://host:port）
注册固定设备，用于无组播环境（如容器、跨网段）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import struct
import time
from dataclasses import dataclass, field

from . import protocol as P
from .bonjour import BonjourBrowser, txt_to_announce

log = logging.getLogger("bmahs.discovery")

# id 冲突判定的活跃窗口（秒）：稳态 announce 间隔 5s 是协议硬性下限（§15），
# 60s = 12 个心跳；正常换 IP（§3.3）的旧地址会在一个窗口后自动衰减出判定
ID_CONFLICT_WINDOW = 60.0


@dataclass
class Device:
    """注册表中的一台设备。动态设备以 announce.id 为键；静态/Bonjour 设备
    初始以 ``static:host:port`` / 设备 id（或 ``bonjour:host:port`` 过渡键）入库，
    读到 hello 后统一重键为设备 id。"""

    # 注册表主键：动态设备 = 设备 id；静态设备在读到 hello 前 = "static:host:port"
    key: str
    # 最近一次 announce 报文原文（§4.2 公共头 + 摘要字段），用于心跳计时与列表展示；
    # Bonjour 通道的设备在 UDP 见到它之前，存放 TXT 还原的摘要
    announce: dict = field(default_factory=dict)
    # 首次发现 / 最近一次确认存活的时间（Unix 秒）；last_seen 是过期删除的依据
    first_seen: int = 0
    last_seen: int = 0
    # TCP hello（§6.2，设备完整自述：name/summary/operations/security…）；
    # None 表示尚未读过 hello（该设备的动态工具尚未生成）
    hello: dict | None = None
    # hello 最近一次成功读取 / 刷新的时刻（monotonic 秒），用于判断 hello 是否过期
    hello_at: float = 0.0
    # hello 上次读取失败的时刻（monotonic 秒），用于失败后的重试退避
    hello_fail_at: float = 0.0
    # 静态设备登记的 tcp://host:port（BMAHS_STATIC_DEVICES）；其余设备为 None
    static_uri: str | None = None
    # Bonjour 浏览登记的 tcp://host:port（_bmahs._tcp 的 SRV 地址）；其余设备为
    # None。该通道设备的生命周期由 mDNS 记录增删驱动，不参与心跳过期
    bonjour_uri: str | None = None
    # 同 id 多地址观测（id 冲突检测，docs/设备id冲突-现状与改进.md §4）：
    # control -> (最近一次该 control 的广播时刻 Unix 秒, model)；检测关闭时不记录
    controls_seen: dict[str, tuple[float, str]] = field(default_factory=dict)
    # 活跃窗口内观测到 ≥2 个不同 control：疑似两台设备撞 id，或同一设备的多网卡多地址
    id_conflict: bool = False

    @property
    def id(self) -> str:
        """设备唯一 id：优先取 hello（最权威），其次 announce，兜底用注册表键。"""
        if self.hello and self.hello.get("id"):
            return str(self.hello["id"])
        return str(self.announce.get("id") or self.key)

    @property
    def name(self) -> str:
        """设备显示名（人类可读，供模型选型），取不到时退回注册表键。"""
        src = self.hello or self.announce
        return str(src.get("name") or self.key)

    @property
    def uri(self) -> str | None:
        """control 层 TCP 地址（tcp://host:port）。

        优先级：静态登记 > UDP announce 的 control > Bonjour SRV 地址；
        （静态是显式配置，announce 是协议主通道，Bonjour 是兜底通道）。
        """
        return self.static_uri or self.announce.get("control") or self.bonjour_uri

    @property
    def state(self) -> str:
        """设备公告的原始状态值：1.0 为 registered/managed/offline，1.1 为 online/offline。

        归一化判断请用 :attr:`online` / :attr:`busy`（两类协议互通）。
        """
        src = self.hello or self.announce
        return str(src.get("state") or "unknown")

    @property
    def protocol(self) -> str:
        """设备公告的协议版本字符串（bmahs/1.0 或 bmahs/1.1），未知时为空。"""
        src = self.hello or self.announce
        proto = src.get("protocol")
        return str(proto) if isinstance(proto, str) else ""

    @property
    def occupancy(self) -> str:
        """占用策略（1.1 §4.6）：last-wins / exclusive。

        读取顺序：hello.security（TCP 自述最权威）→ announce.security 摘要 →
        announce 顶层 occupancy → 缺省 exclusive（兼容 1.0 设备）。
        """
        hello = self.hello or {}
        return P.normalize_occupancy(
            hello.get("security"), self.announce.get("security"), self.announce
        )

    @property
    def online(self) -> bool:
        """归一化在线状态（1.1 §9：registered/managed 当作 online）。"""
        return P.is_online(self.state)

    @property
    def busy(self) -> bool:
        """归一化忙碌状态：1.0 state=managed；1.1 公告 busy（exclusive 占用或有活动流）。"""
        src = self.hello or self.announce
        return P.busy_of(src.get("state"), src.get("busy"))

    @property
    def holder(self) -> str | None:
        """当前占用方（exclusive）或最后控制者（last-wins）的 agent 名；无人时为 None。"""
        return (self.hello or self.announce).get("holder")

    @property
    def until(self) -> int:
        """当前占用租约的到期时刻（Unix 秒）；0 表示未占用或设备未公告。"""
        return (self.hello or self.announce).get("until") or 0

    @property
    def source(self) -> str:
        """设备来源："static"（环境变量登记）、"bonjour"（mDNS 浏览）或 "multicast"（UDP 组播）。"""
        if self.static_uri:
            return "static"
        return "bonjour" if self.bonjour_uri else "multicast"

    def eval_id_conflict(self, window: float) -> bool:
        """活跃窗口（秒）内是否观测到 ≥2 个不同 control：同 id 冲突信号。

        窗口外的地址条目顺带清理。正常换 IP（§3.3）的旧地址一个窗口后自动
        衰减、冲突解除；同一设备的多网卡多地址会持续命中（误报源，见
        docs/设备id冲突-现状与改进.md §4），处置策略交由网关层配置。
        """
        now = P.now()
        self.controls_seen = {
            c: v for c, v in self.controls_seen.items() if now - v[0] <= window
        }
        return len(self.controls_seen) >= 2


class _Rx(asyncio.DatagramProtocol):
    """UDP 数据报回调适配器：把 asyncio 传输层收到的报文转交 Discovery 处理。"""

    def __init__(self, disc: "Discovery") -> None:
        self.disc = disc

    def connection_made(self, transport) -> None:  # noqa: ANN001
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:  # noqa: ANN001
        self.disc._on_datagram(data, addr)

    def error_received(self, exc: Exception) -> None:
        log.debug("UDP 接收错误: %s", exc)


class Discovery:
    """UDP 组播发现与设备注册表。所有方法须在事件循环内调用。"""

    def __init__(
        self,
        agent_id: str,
        *,
        query_interval: float = 300.0,
        expire_sec: float = 1800.0,
        static_uris: list[str] | None = None,
        bonjour: bool = True,
        conflict_detect: bool = True,
        on_change=None,  # Callable[[], Awaitable[None]] | None
    ) -> None:
        self.agent_id = agent_id
        # 周期性广播 query 的间隔 / 设备无心跳过期时限（秒），均设下限防误配
        self.query_interval = max(30.0, query_interval)
        self.expire_sec = max(60.0, expire_sec)
        self.static_uris = static_uris or []
        # 是否启用 Bonjour 浏览通道（协议 §3.2；zeroconf 缺失时自动降级）
        self.bonjour = bonjour
        # 是否检测同 id 多控制地址（id 冲突）；关闭时不记录 controls_seen
        self.conflict_detect = conflict_detect
        self._browser: BonjourBrowser | None = None
        # 设备列表变化（新增/下线/状态变化）时的异步回调，网关用它触发工具表重建
        self.on_change = on_change
        # 注册表：key -> Device；设备的增删与状态更新都发生在这里
        self.devices: dict[str, Device] = {}
        self._tasks: list[asyncio.Task] = []
        self._transports: list[asyncio.DatagramTransport] = []
        self._send4: socket.socket | None = None
        self._send6: socket.socket | None = None
        self._stopping = False
        # 多网卡主机（如装有 VMware/Hyper-V 的 Windows）上，内核默认选中的组播
        # 接口往往不是目标网段：join 在错误网卡上收不到外部 announce，发送同样
        # 出不去。因此对全部本机 IPv4 逐一 join + 逐一发送；BMAHS_MCAST_IF_V4
        # 可手动指定（逗号分隔 IP）。列表按查询周期刷新（_refresh_v4_membership），
        # DHCP 续租 / 网卡变化后能自愈。
        self._v4_addrs = self._local_v4_addrs()
        self._rx4_sock: socket.socket | None = None
        self._joined_v4: set[str] = set()

    # ------------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        """登记静态设备 → 启动 Bonjour 浏览 → 打开 IPv4/IPv6 组播接收 → 启动 query/过期两个周期任务 → 立即扫描一次。"""
        for uri in self.static_uris:
            self.add_static(uri)
        if self.bonjour:
            browser = BonjourBrowser()
            if await browser.start(self._on_bonjour_add, self._on_bonjour_remove):
                self._browser = browser  # 启动失败（zeroconf 缺失等）只降级
        loop = asyncio.get_running_loop()
        try:
            sock = self._make_v4_rx()
            transport, _ = await loop.create_datagram_endpoint(lambda: _Rx(self), sock=sock)
            self._transports.append(transport)
        except OSError as e:
            log.warning("IPv4 组播接收不可用（发现将依赖静态设备表）: %s", e)
        try:
            sock6 = self._make_v6_rx()
            transport, _ = await loop.create_datagram_endpoint(lambda: _Rx(self), sock=sock6)
            self._transports.append(transport)
        except OSError as e:
            log.info("IPv6 组播接收不可用（可忽略，IPv4 仍可用）: %s", e)
        self._tasks.append(asyncio.create_task(self._query_loop(), name="bmahs-query"))
        self._tasks.append(asyncio.create_task(self._expire_loop(), name="bmahs-expire"))
        await self.query()

    async def stop(self) -> None:
        """停掉周期任务、关闭组播套接字与 Bonjour 浏览；幂等，进程退出前调用。"""
        self._stopping = True
        if self._browser is not None:
            await self._browser.stop()
            self._browser = None
        for t in self._tasks:
            t.cancel()
        for transport in self._transports:
            try:
                transport.close()
            except Exception:
                pass
        for sock in (self._send4, self._send6):
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
        self._tasks.clear()
        self._transports.clear()
        self._send4 = self._send6 = None
        self._rx4_sock = None
        self._joined_v4.clear()

    # ------------------------------------------------------------------ 查询与注册表

    async def query(self, want: str = "*") -> None:
        """向两个组播组各发一次 query（§3.1：智能体应对两个组各发一次）。"""
        payload = P.build_query(self.agent_id, want)
        self._send_multicast(payload)

    def add_static(self, uri: str) -> Device | None:
        """登记一台静态设备（无组播环境用）；URI 非法时告警并返回 None。"""
        target = P.parse_control_uri(uri.strip())
        if not target:
            log.warning("无法解析静态设备地址: %r", uri)
            return None
        key = f"static:{target[0]}:{target[1]}"
        dev = self.devices.get(key)
        if dev is None:
            dev = Device(key=key, static_uri=f"tcp://{target[0]}:{target[1]}")
            dev.first_seen = dev.last_seen = P.now()
            self.devices[key] = dev
            log.info("登记静态设备 %s", dev.static_uri)
        return dev

    # ------------------------------------------------------------------ Bonjour 通道（§3.2）

    def _on_bonjour_add(self, host: str, port: int, txt: dict) -> None:
        """Bonjour 服务出现/更新（BonjourBrowser 投递到主循环线程调用）。

        TXT 还原出 announce 摘要，使 UDP 组播静默的设备在 hello 前就能被列表
        展示与选中；已被 UDP/静态通道发现的设备只补 bonjour_uri，不重复建条目。
        """
        if self._stopping or not host or int(port) <= 0:
            return
        uri = f"tcp://{host}:{int(port)}"
        preview = txt_to_announce(txt, host, int(port))
        dev_id = str(preview.get("id") or "")
        dev = self.get(dev_id) if dev_id else None
        if dev is None:
            dev = self.devices.get(dev_id or f"bonjour:{host}:{int(port)}")
        new = dev is None
        if new:
            dev = Device(key=dev_id or f"bonjour:{host}:{int(port)}")
            dev.first_seen = P.now()
            self.devices[dev.key] = dev
            log.info(
                "Bonjour 发现设备 %s（%s/%s，%s）@ %s",
                dev_id or dev.key,
                preview.get("type"),
                preview.get("service"),
                preview.get("name"),
                uri,
            )
        changed = new or dev.bonjour_uri != uri
        dev.bonjour_uri = uri
        dev.last_seen = P.now()
        if dev.announce.get("kind") != "announce":
            # UDP 通道从未见过它（当前只是 TXT 还原的摘要）：摘要顶上并随
            # Bonjour 记录刷新（换端口/换地址）；组播可见的设备以 announce 为准
            dev.announce = preview
        if changed:
            self._fire_change()

    def _on_bonjour_remove(self, name: str, dev_id: str, host: str, port: int) -> None:
        """Bonjour 服务记录消失（mDNS 过期/注销）；按通道优先级收敛设备条目。"""
        if self._stopping:
            return
        dev = self.get(dev_id) if dev_id else None
        if dev is None and host:
            dev = self.devices.get(f"bonjour:{host}:{int(port)}")
        if dev is None or dev.static_uri:
            return  # 静态登记的设备不随 mDNS 下线（登记值仍在，重连即恢复）
        dev.bonjour_uri = None
        if dev.announce.get("kind") != "announce":
            # 只有 Bonjour 见过它（announce 是 TXT 还原的摘要）：随 mDNS 记录一起下线
            self.devices.pop(dev.key, None)
            log.info("Bonjour 服务 %s 已下线，移除设备 %s", name, dev_id or dev.key)
        self._fire_change()

    def get(self, device_id: str) -> Device | None:
        """按注册表键或设备 id 查设备（兼容静态设备重键前的过渡期）。"""
        dev = self.devices.get(device_id)
        if dev is not None:
            return dev
        for d in self.devices.values():
            if d.id == device_id:
                return d
        return None

    def all(self) -> list[Device]:
        """当前注册表快照（所有已知设备，含尚未读到 hello 的）。"""
        return list(self.devices.values())

    def bind_hello(self, key: str, hello: dict) -> Device | None:
        """把 hello 绑定到设备；静态/Bonjour 设备借此从过渡键重键为设备 id。"""
        dev = self.devices.get(key)
        if dev is None:
            return self.get(hello.get("id") or key)
        dev.hello = hello
        dev.hello_at = time.monotonic()
        dev.hello_fail_at = 0.0
        # TCP 上成功读到 hello 即证明设备存活：刷新 last_seen，跨网段/组播静默
        # 设备不会被心跳过期误删（docs/跨网段发现-原因与方案.md §2.3）
        dev.last_seen = P.now()
        dev_id = str(hello.get("id") or "")
        if (dev.static_uri or dev.bonjour_uri) and dev_id and dev.key != dev_id:
            self.devices.pop(dev.key, None)
            dev.key = dev_id
            existing = self.devices.get(dev_id)
            if existing is not dev:
                self.devices[dev_id] = dev
        return dev

    # ------------------------------------------------------------------ 报文处理

    def _on_datagram(self, data: bytes, addr) -> None:  # noqa: ANN001
        """处理一个组播报文：announce 入库（新增/变化触发回调），goodbye 移除，其余忽略。"""
        if self._stopping or len(data) > P.MAX_DGRAM:
            return
        msg = P.parse_message(data)
        if msg is None:
            return
        kind = msg.get("kind")
        dev_id = msg.get("id")
        if dev_id == self.agent_id:
            return  # 收到自己的报文（协议 §5.1：忽略，不要递归发送）
        if kind == "query":
            return  # 别的智能体在扫描，与网关无关
        if kind == "announce":
            if self._upsert(dev_id, msg):
                self._fire_change()
        elif kind == "goodbye":
            if self._remove(dev_id):
                log.info("设备 %s 已下线（goodbye）", dev_id)
                self._fire_change()

    def _upsert(self, dev_id: str, msg: dict) -> bool:
        """入库一条 announce：新建设备或刷新其 announce/last_seen。

        返回 True 表示注册表发生了可观察变化（新设备，或 control/state 变化），
        调用方据此触发 on_change（进而重建 MCP 工具表）。
        """
        dev = self.devices.get(dev_id)
        if dev is None:
            for d in self.devices.values():
                if (d.static_uri or d.bonjour_uri) and d.id == dev_id:
                    dev = d
                    break
        new = dev is None
        if new:
            dev = Device(key=dev_id)
            dev.first_seen = P.now()
            self.devices[dev.key] = dev
            log.info(
                "发现设备 %s（%s/%s，%s）@ %s",
                dev_id,
                msg.get("type"),
                msg.get("service"),
                msg.get("name"),
                msg.get("control"),
            )
        changed = (
            dev.announce.get("control") != msg.get("control")
            or dev.announce.get("state") != msg.get("state")
        )
        dev.announce = msg
        dev.last_seen = P.now()
        return self._track_conflict(dev) or bool(new or changed)

    def _track_conflict(self, dev: Device) -> bool:
        """记录本条 announce 的 control 指纹并评估 id 冲突；标记翻转时打日志。

        返回 True 表示冲突标记发生变化（调用方应触发 on_change 重建工具表）。
        """
        if not self.conflict_detect:
            return False
        control = str(dev.announce.get("control") or "")
        if control:
            dev.controls_seen[control] = (P.now(), str(dev.announce.get("model") or ""))
        was = dev.id_conflict
        dev.id_conflict = dev.eval_id_conflict(ID_CONFLICT_WINDOW)
        if dev.id_conflict and not was:
            log.warning(
                "设备 id 冲突告警：%s 在 %d 秒窗口内观测到多个控制地址（%s）——"
                "可能是两台设备撞 id（控制会串台），也可能是同一设备的多网卡多地址；"
                "处置策略见 BMAHS_ID_CONFLICT_POLICY",
                dev.id,
                int(ID_CONFLICT_WINDOW),
                "、".join(sorted(dev.controls_seen)),
            )
        elif was and not dev.id_conflict:
            log.info("设备 %s 的 id 冲突已解除（活跃窗口内仅剩单一控制地址）", dev.id)
        return dev.id_conflict != was

    def _remove(self, dev_id: str) -> bool:
        """把设备移出注册表（goodbye 下线）；静态设备不删（登记值仍在，重连即恢复）。"""
        dev = self.get(dev_id)
        if dev is None or dev.static_uri:
            return False
        self.devices.pop(dev.key, None)
        return True

    def _fire_change(self) -> None:
        """调度一次 on_change 回调（异步、异常隔离），通知上层设备列表已变化。"""
        if self.on_change is None:
            return
        try:
            task = asyncio.get_running_loop().create_task(self._safe_on_change())
            # 事件循环对任务只持弱引用，必须自持引用防止任务被 GC 中途回收
            if not hasattr(self, "_bg_tasks"):
                self._bg_tasks: set[asyncio.Task] = set()
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        except RuntimeError:
            pass

    async def _safe_on_change(self) -> None:
        try:
            await self.on_change()
        except Exception as e:  # noqa: BLE001
            log.debug("on_change 回调失败: %s", e)

    # ------------------------------------------------------------------ 周期任务

    async def _query_loop(self) -> None:
        """周期任务：按 query_interval 重刷组播接口并广播 query，驱动设备持续应答 announce。"""
        while not self._stopping:
            try:
                self._refresh_v4_membership()
                await self.query()
            except Exception as e:  # noqa: BLE001
                log.debug("query 发送失败: %s", e)
            await asyncio.sleep(self.query_interval)

    async def _expire_loop(self) -> None:
        """周期任务：每 30 秒清一次过期设备，有移除则触发 on_change。"""
        while not self._stopping:
            await asyncio.sleep(30)
            if self.purge_expired():
                self._fire_change()

    def purge_expired(self) -> list[str]:
        """删除超过无心跳时限的设备（静态/Bonjour 设备不过期），返回被移除的键。

        §4.6/§4.8-7：时限按设备公告的 ``hb`` 逐台计算 =
        clamp(12 × hb, 60 秒, 30 分钟)；无 hb / 未知对端按缺省 5 秒（即 60 秒）。
        Bonjour 设备的存活由 mDNS 记录增删驱动（_on_bonjour_remove），
        不参与心跳过期，否则无组播心跳的它们会被立即误删。
        """
        gone: list[str] = []
        for d in self.devices.values():
            if d.static_uri or d.bonjour_uri:
                continue
            hb = P.hb_of(d.announce) if d.announce else P.DEFAULT_HB
            limit = P.expire_sec_for(hb)
            # 上限仍受构造参数 expire_sec 约束（缺省 1800s = 协议上限 30 分钟）
            limit = min(limit, self.expire_sec)
            if d.last_seen < P.now() - int(limit):
                gone.append(d.key)
        for key in gone:
            self.devices.pop(key, None)
            log.info("设备 %s 超过无心跳删除时限，已移除", key)
        return gone

    # ------------------------------------------------------------------ 套接字

    def _local_v4_addrs(self) -> list[str]:
        """枚举本机全部 IPv4 地址；解析失败回退内核默认（0.0.0.0）。"""
        raw = os.environ.get("BMAHS_MCAST_IF_V4", "")
        explicit = [s.strip() for s in raw.replace(";", ",").split(",") if s.strip()]
        if explicit:
            return explicit
        addrs: list[str] = []

        def _collect(ip: str) -> None:
            if ip != "127.0.0.1" and ip not in addrs:
                addrs.append(ip)

        # Windows 上 gethostbyname_ex 返回全部网卡的 IPv4；
        # getaddrinfo 只保证主机名能解析出的那部分，作回退
        try:
            for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
                _collect(ip)
        except OSError:
            pass
        if not addrs:
            try:
                for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                    _collect(info[4][0])
            except OSError:
                pass
        return addrs or ["0.0.0.0"]

    def _join_v4(self, s: socket.socket, ip: str) -> bool:
        """在本机某个 IPv4 地址（网卡）上加入组播组；失败仅告警，返回 False。"""
        try:
            mreq = socket.inet_aton(P.MULTICAST_V4) + socket.inet_aton(ip)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            self._joined_v4.add(ip)
            return True
        except OSError as e:
            if os.environ.get("BMAHS_MCAST_IF_V4"):
                log.warning("无法在 %s 上加入组播组: %s", ip, e)
            return False

    def _refresh_v4_membership(self) -> None:
        """重解析本机地址：新增接口补 join，消失的接口退组。

        接口列表若只在初始化时快照一次，DHCP 续租 / 换网后旧成员关系失效、
        新地址未加入，组播会永久失聪，只能重启进程恢复。
        """
        if self._rx4_sock is None:
            return
        addrs = self._local_v4_addrs()
        if addrs == self._v4_addrs:
            return
        for ip in addrs:
            if ip not in self._joined_v4:
                self._join_v4(self._rx4_sock, ip)
        for ip in list(self._joined_v4):
            if ip not in addrs:
                try:
                    mreq = socket.inet_aton(P.MULTICAST_V4) + socket.inet_aton(ip)
                    self._rx4_sock.setsockopt(
                        socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, mreq
                    )
                except OSError:
                    pass
                self._joined_v4.discard(ip)
        self._v4_addrs = addrs
        log.info("组播接口已刷新: %s", ", ".join(addrs))

    def _make_v4_rx(self) -> socket.socket:
        """创建 IPv4 组播接收套接字：REUSEADDR + 绑定 5354 端口 + 在全部网卡上 join。"""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", P.DISCOVERY_PORT))
        self._rx4_sock = s
        joined = 0
        for ip in self._v4_addrs:
            if self._join_v4(s, ip):
                joined += 1
        if not joined:
            s.close()
            self._rx4_sock = None
            raise OSError("无法加入 239.255.42.42 组播组")
        return s

    def _make_v6_rx(self) -> socket.socket:
        """创建 IPv6 组播接收套接字：在每个接口的 scope 上 join ff02::4242（尽力而为）。"""
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        except OSError:
            pass
        s.bind(("::", P.DISCOVERY_PORT))
        try:
            ifindexes = [idx for idx, _name in socket.if_nameindex()]
        except OSError:
            ifindexes = [0]
        joined = 0
        for idx in ifindexes:
            try:
                mreq = socket.inet_pton(socket.AF_INET6, P.MULTICAST_V6) + struct.pack("I", idx)
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_JOIN_GROUP, mreq)
                joined += 1
            except OSError:
                continue
        if not joined:
            s.close()
            raise OSError("无法加入 ff02::4242 组播组")
        return s

    def _send_multicast(self, payload: bytes) -> None:
        """把一条报文发到两个组播组：IPv4 逐网卡各发一次（TTL=2），IPv6 逐接口尽力而为。"""
        try:
            if self._send4 is None:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
                try:
                    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
                except OSError:
                    pass
                self._send4 = s
            sent = 0
            for ip in self._v4_addrs:
                try:
                    self._send4.setsockopt(
                        socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip)
                    )
                    self._send4.sendto(payload, (P.MULTICAST_V4, P.DISCOVERY_PORT))
                    sent += 1
                except OSError:
                    continue
            if not sent:
                raise OSError(f"所有网卡的 IPv4 组播发送均失败: {self._v4_addrs}")
        except OSError as e:
            log.warning("IPv4 组播发送失败: %s", e)
        # IPv6 尽力而为：链路本地组播需要 scope id，逐网卡尝试
        try:
            if self._send6 is None:
                s6 = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
                s6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, 2)
                self._send6 = s6
            try:
                ifindexes = [idx for idx, _name in socket.if_nameindex()]
            except OSError:
                ifindexes = [0]
            sent = False
            for idx in ifindexes:
                try:
                    self._send6.sendto(payload, (P.MULTICAST_V6, P.DISCOVERY_PORT, 0, idx))
                    sent = True
                    break
                except OSError:
                    continue
            if not sent:
                raise OSError("所有网卡的 IPv6 组播发送均失败")
        except OSError as e:
            log.debug("IPv6 组播发送失败（忽略）: %s", e)
