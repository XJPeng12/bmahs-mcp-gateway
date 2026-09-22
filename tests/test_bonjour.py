"""Bonjour 浏览通道单元测试（不占用网络端口、不启动真实 mDNS）。

覆盖两条线：
- 注册表侧：TXT 摘要入库、hello 重键、与 UDP announce 合并去重、mDNS 下线
  移除、不过心跳过期（docs/组播发现失败-原因与排查.md §5.5 的根治改进）；
- 浏览器侧：用注入 sys.modules 的假 zeroconf 验证「服务事件 → 主循环回调」
  的投递链路与开关行为。
"""

import asyncio
import socket
import sys
import types

import pytest

from bmahs_mcp import protocol as P
from bmahs_mcp.bonjour import BonjourBrowser, build_txt, txt_to_announce
from bmahs_mcp.discovery import Discovery


def make_disc(**kw) -> Discovery:
    kw.setdefault("bonjour", False)  # 单测默认关浏览，注册表逻辑直接调内部方法
    return Discovery("agent-x", **kw)


def fake_txt(**over) -> dict:
    base = {
        "txtvers": "1",
        "protocol": "bmahs/1.0",
        "type": "bridge",
        "service": "bridge/1",
        "id": "bmahs-bridge-01",
        "name": "BMAHS-Bridge-01",
        "summary": "软件网关",
        "capabilities": "bt.scan,route",
        "security": "lan,token",
        "state": "registered",
        "busy": "0",
        "hb": "5",
    }
    base.update(over)
    return base


def announce_bytes(dev_id: str, control: str, state: str = "registered") -> bytes:
    return P.build_announce(
        {
            "id": dev_id,
            "type": "light",
            "service": "light/1",
            "name": f"设备{dev_id}",
            "control": control,
            "state": state,
            "busy": state == "managed",
        }
    )


class TestTxtRoundTrip:
    def test_build_then_parse(self):
        announce = {
            "id": "x1",
            "name": "客厅灯",
            "type": "light",
            "service": "light/1",
            "protocol": "bmahs/1.0",
            "capabilities": ["on", "off"],
            "security": {"scope": "lan", "auth": "token"},
            "summary": "吸顶灯",
            "state": "registered",
            "busy": False,
            "hb": 5,
        }
        parsed = txt_to_announce(build_txt(announce), "192.168.3.51", 9527)
        assert parsed["id"] == "x1"
        assert parsed["control"] == "tcp://192.168.3.51:9527"
        assert parsed["capabilities"] == ["on", "off"]
        # TXT 解析保持忠实还原（两字段不臆造字段）；occupancy 缺省 exclusive
        # 由注册表 Device.occupancy / protocol.normalize_occupancy 统一兜底
        assert parsed["security"] == {"scope": "lan", "auth": "token"}
        assert parsed["state"] == "registered"
        assert parsed["hb"] == 5

    def test_bytes_keys_and_missing_fields(self):
        parsed = txt_to_announce({b"id": b"b1", b"name": "桥".encode()}, "10.0.0.9", 9528)
        assert parsed["id"] == "b1"
        assert parsed["name"] == "桥"
        assert parsed["control"] == "tcp://10.0.0.9:9528"
        assert parsed["state"] == "registered"  # 缺省值
        assert parsed["capabilities"] == []
        assert parsed["security"] == {}
        assert parsed["hb"] is None

    def test_build_txt_11_three_field_security(self):
        """1.1 §6.4：TXT security=scope,auth,occupancy 三字段。"""
        announce = {
            "id": "x1",
            "name": "客厅灯",
            "type": "light",
            "service": "light/1",
            "protocol": "bmahs/1.1",
            "capabilities": ["on", "off"],
            "security": {"scope": "lan", "auth": "none", "occupancy": "last-wins"},
            "summary": "吸顶灯",
            "state": "online",
            "busy": False,
            "hb": 5,
        }
        txt = build_txt(announce)
        assert txt["security"] == "lan,none,last-wins"
        parsed = txt_to_announce(txt, "192.168.3.51", 9527)
        assert parsed["security"] == {"scope": "lan", "auth": "none", "occupancy": "last-wins"}
        assert parsed["protocol"] == "bmahs/1.1"
        assert parsed["state"] == "online"

    def test_parse_txt_10_two_fields_stay_two(self):
        """1.0 两字段 TXT 忠实还原为两字段 security；不在此层补缺省。"""
        parsed = txt_to_announce(fake_txt(), "192.168.3.51", 9527)
        assert parsed["security"] == {"scope": "lan", "auth": "token"}
        # 畸形 security（单字段/为空）不产生半截 dict
        parsed = txt_to_announce(fake_txt(security="lan"), "192.168.3.51", 9527)
        assert parsed["security"] == {}
        parsed = txt_to_announce(fake_txt(security=""), "192.168.3.51", 9527)
        assert parsed["security"] == {}


class TestBonjourRegistry:
    def test_add_shows_txt_preview(self):
        disc = make_disc()
        disc._on_bonjour_add("192.168.3.51", 9527, fake_txt())
        dev = disc.get("bmahs-bridge-01")
        assert dev is not None
        assert dev.source == "bonjour"
        assert dev.uri == "tcp://192.168.3.51:9527"
        assert dev.name == "BMAHS-Bridge-01"
        assert dev.announce["summary"] == "软件网关"
        assert dev.announce["capabilities"] == ["bt.scan", "route"]
        assert dev.announce["security"] == {"scope": "lan", "auth": "token"}

    def test_rekey_when_txt_lacks_id(self):
        disc = make_disc()
        disc._on_bonjour_add("192.168.3.51", 9527, fake_txt(id=""))
        assert "bonjour:192.168.3.51:9527" in disc.devices
        disc.bind_hello("bonjour:192.168.3.51:9527", {"id": "bridge-x"})
        assert disc.get("bridge-x") is not None
        assert "bonjour:192.168.3.51:9527" not in disc.devices
        assert disc.get("bridge-x").source == "bonjour"

    def test_udp_announce_merges_no_duplicate(self):
        """Bonjour 先发现、设备随后开始组播：同一物理设备不得出现两条记录。"""
        disc = make_disc()
        disc._on_bonjour_add("192.168.3.51", 9527, fake_txt())
        disc._on_datagram(announce_bytes("bmahs-bridge-01", "tcp://192.168.3.51:9527"), ("192.168.3.51", 40000))
        assert len(disc.all()) == 1
        dev = disc.get("bmahs-bridge-01")
        assert dev.announce["name"] == "设备bmahs-bridge-01"  # UDP announce 覆盖 TXT 摘要
        assert dev.source == "bonjour"  # 双通道设备：Bonjour 登记仍在

    def test_hello_rekey_finds_bonjour_device(self):
        disc = make_disc()
        disc._on_bonjour_add("192.168.3.51", 9527, fake_txt(id=""))
        disc.bind_hello("bonjour:192.168.3.51:9527", {"id": "bridge-y", "name": "桥"})
        # 重键后新的 Bonjour 事件仍按 id 找到同一条记录
        disc._on_bonjour_add("192.168.3.51", 9528, fake_txt(id="bridge-y"))
        assert len(disc.all()) == 1
        assert disc.get("bridge-y").uri == "tcp://192.168.3.51:9528"

    def test_remove_drops_bonjour_only_device(self):
        disc = make_disc()
        disc._on_bonjour_add("192.168.3.51", 9527, fake_txt())
        disc._on_bonjour_remove("BMAHS-Bridge-01._bmahs._tcp.local.", "bmahs-bridge-01", "192.168.3.51", 9527)
        assert disc.get("bmahs-bridge-01") is None

    def test_remove_keeps_device_seen_via_udp(self):
        """UDP 也见过它：mDNS 记录消失只是少了一条通道，设备仍由组播心跳保活。"""
        disc = make_disc()
        disc._on_bonjour_add("192.168.3.51", 9527, fake_txt())
        disc._on_datagram(announce_bytes("bmahs-bridge-01", "tcp://192.168.3.51:9527"), ("192.168.3.51", 40000))
        disc._on_bonjour_remove("BMAHS-Bridge-01._bmahs._tcp.local.", "bmahs-bridge-01", "192.168.3.51", 9527)
        dev = disc.get("bmahs-bridge-01")
        assert dev is not None
        assert dev.bonjour_uri is None
        assert dev.source == "multicast"

    def test_remove_ignores_static_devices(self):
        disc = make_disc()
        disc.add_static("tcp://192.168.3.51:9527")
        disc._on_bonjour_add("192.168.3.51", 9527, fake_txt())
        disc._on_bonjour_remove("n", "bmahs-bridge-01", "192.168.3.51", 9527)
        assert disc.get("static:192.168.3.51:9527") is not None

    def test_bonjour_devices_not_purged(self):
        """Bonjour 通道的设备无 UDP 心跳：不参与心跳过期，否则会被立即误删。"""
        disc = make_disc()
        disc._on_bonjour_add("192.168.3.51", 9527, fake_txt())
        disc.get("bmahs-bridge-01").last_seen = P.now() - 9999
        assert disc.purge_expired() == []
        assert disc.get("bmahs-bridge-01") is not None

    def test_goodbye_still_removes_bonjour_device(self):
        disc = make_disc()
        disc._on_bonjour_add("192.168.3.51", 9527, fake_txt())
        goodbye = P.build_goodbye(
            {"id": "bmahs-bridge-01", "type": "bridge", "service": "bridge/1", "name": "桥"}
        )
        disc._on_datagram(goodbye, ("192.168.3.51", 40000))
        assert disc.get("bmahs-bridge-01") is None


class TestGatewayEnvSwitch:
    def test_default_on(self, monkeypatch):
        monkeypatch.delenv("BMAHS_BONJOUR_BROWSE", raising=False)
        from bmahs_mcp.gateway import Gateway

        assert Gateway().discovery.bonjour is True

    def test_can_disable(self, monkeypatch):
        monkeypatch.setenv("BMAHS_BONJOUR_BROWSE", "0")
        from bmahs_mcp.gateway import Gateway

        assert Gateway().discovery.bonjour is False


@pytest.fixture()
def fake_zeroconf(monkeypatch):
    """把假 zeroconf / zeroconf.asyncio 注入 sys.modules，浏览器全程不碰网络。"""
    state: dict = {"info": None, "browsers": []}

    class FakeAsyncServiceInfo:
        def __init__(self, type_, name):
            self.name = name
            self.addresses: list[bytes] = []
            self.port = 0
            self.properties: dict = {}

        async def async_request(self, zc, timeout, **kw):  # noqa: ANN001
            spec = state["info"]
            if spec is None:
                return False
            self.addresses = [socket.inet_aton(spec["host"])]
            self.port = spec["port"]
            self.properties = spec.get("props", {})
            return True

        def parsed_addresses(self):
            return [socket.inet_ntoa(a) for a in self.addresses]

        @property
        def decoded_properties(self):
            return {k.decode(): v.decode() for k, v in self.properties.items()}

    class FakeBrowser:
        def __init__(self, zc, type_, listener):  # noqa: ANN001
            self.listener = listener
            self.cancelled = False
            state["browsers"].append(self)

        async def async_cancel(self):
            self.cancelled = True

    class FakeAsyncZeroconf:
        def __init__(self):
            self.zeroconf = object()
            self.closed = False

        async def async_close(self):
            self.closed = True

    aio = types.ModuleType("zeroconf.asyncio")
    aio.AsyncServiceInfo = FakeAsyncServiceInfo
    aio.AsyncServiceBrowser = FakeBrowser
    aio.AsyncZeroconf = FakeAsyncZeroconf
    zc = types.ModuleType("zeroconf")
    zc.asyncio = aio
    monkeypatch.setitem(sys.modules, "zeroconf", zc)
    monkeypatch.setitem(sys.modules, "zeroconf.asyncio", aio)
    return state


class TestBonjourBrowser:
    def test_add_and_remove_dispatch_to_main_loop(self, fake_zeroconf):
        async def scenario():
            events: list[tuple] = []
            br = BonjourBrowser()
            assert await br.start(
                lambda h, p, t: events.append(("add", h, p, t.get("id"))),
                lambda n, d, h, p: events.append(("remove", n, d)),
            )
            listener = fake_zeroconf["browsers"][-1].listener
            fake_zeroconf["info"] = {
                "host": "192.168.3.51",
                "port": 9527,
                "props": {b"id": b"bmahs-bridge-01"},
            }
            listener.add_service(None, "_bmahs._tcp.local.", "BMAHS-Bridge-01._bmahs._tcp.local.")
            await asyncio.sleep(0.1)  # 等 create_task 解析 + call_soon_threadsafe 投回主循环
            listener.remove_service(None, "_bmahs._tcp.local.", "BMAHS-Bridge-01._bmahs._tcp.local.")
            await asyncio.sleep(0.1)
            await br.stop()
            return events

        events = asyncio.run(scenario())
        assert ("add", "192.168.3.51", 9527, "bmahs-bridge-01") in events
        assert ("remove", "BMAHS-Bridge-01._bmahs._tcp.local.", "bmahs-bridge-01") in events

    def test_unresolvable_service_is_dropped(self, fake_zeroconf):
        async def scenario():
            events: list[tuple] = []
            br = BonjourBrowser()
            assert await br.start(lambda *a: events.append(a), lambda *a: events.append(a))
            listener = fake_zeroconf["browsers"][-1].listener
            fake_zeroconf["info"] = None  # async_request 返回 False：无人应答
            listener.add_service(None, "_bmahs._tcp.local.", "ghost._bmahs._tcp.local.")
            await asyncio.sleep(0.1)
            await br.stop()
            return events

        assert asyncio.run(scenario()) == []

    def test_stop_cancels_browser_and_closes_zeroconf(self, fake_zeroconf):
        async def scenario():
            br = BonjourBrowser()
            assert await br.start(lambda *a: None, lambda *a: None)
            browser = fake_zeroconf["browsers"][-1]
            zc_obj = getattr(br, "_azc", None)  # stop 前留个引用
            assert zc_obj is not None
            await br.stop()
            return browser, zc_obj

        browser, zc_obj = asyncio.run(scenario())
        assert browser.cancelled is True
        assert zc_obj.closed is True
