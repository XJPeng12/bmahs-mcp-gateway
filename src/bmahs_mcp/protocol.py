"""BMAHS 发现层常量与编解码（对应最新协议 bmahs/1.0 §3/§4/§5/§A）。

字段名按最新标准：``version`` / ``protocol`` / ``timestamp`` / ``service`` /
``capabilities`` / ``security``。解析侧同时接受旧版（bmahs/1.2 时期）的
``v`` / ``proto`` / ``ts`` / ``svc`` / ``caps`` / ``sec``，便于平滑迁移；
发送侧一律使用新字段名（§7.4：不得用 ``v`` 之类的旧字段名）。
"""

from __future__ import annotations

import json
import re
import time

PROTO = "bmahs/1.0"
PROTO_PREFIX = "bmahs"  # 智能体必须接受 bmahs*（现行候选 bmahs/1.0）
# UDP 组播报文的三种类型（§3）：announce=设备上线/状态变化广播，
# query=智能体主动扫描（设备以 announce 应答），goodbye=设备下线告别。
KINDS = ("announce", "query", "goodbye")

# 发现层组播组与端口：IPv4/IPv6 双栈，设备与智能体都在这两个组上收发
MULTICAST_V4 = "239.255.42.42"
MULTICAST_V6 = "ff02::4242"
DISCOVERY_PORT = 5354
# 协议硬性上限：一个 UDP 数据报 = 一个完整 JSON，不得超过 1400 字节（避免 IP 分片）
MAX_DGRAM = 1400

# control（TCP 行协议，hello/动作）与 ui（二进制视频流）的缺省端口
DEFAULT_CONTROL_PORT = 9527
DEFAULT_UI_PORT = 9531

# 稳态 announce 间隔（§4.6 存活检测）：出厂默认 5 秒，硬性下限，只允许调大
DEFAULT_HB = 5.0
HB_MIN = 5.0

# 全品类必须在 operations 中声明并实现的动作（§4.5）
GENERIC_ACTIONS = frozenset({"describe", "info", "register", "occupy", "release", "who"})
# 只读 / 登记刷新动作：不改变受管关系，不需要 token（§4.6 规则 2）
READONLY_ACTIONS = frozenset({"describe", "info", "who", "register"})

# 默认占用租约（§4.6）：未带 ttl 的 occupy 用 60 秒；9999 = 无限期
DEFAULT_LEASE_SEC = 60
LEASE_UNLIMITED = 9999

# query 应答限频（§5.1）：同一发送方（按 id）1 秒内只应答一次
QUERY_REPLY_MIN_INTERVAL = 1.0

_URI_RE = re.compile(r"^tcp://(\[[0-9A-Fa-f:.]+\]|[^:\[\]/]+):(\d+)$")


def now() -> int:
    """Unix 秒。"""
    return int(time.time())


def sanitize_id(name: str) -> str:
    """由显示名生成稳定 id（§4.3）：非法字符改 -，去首尾 -，小写。"""
    out = re.sub(r"[^A-Za-z0-9-]", "-", str(name)).strip("-").lower()
    return out or "bmahs-device"


def parse_message(data: bytes) -> dict | None:
    """解析并校验一个 UDP 报文；不合规按 §4.1 直接丢弃（返回 None）。

    兼容期：公共头同时接受新（``version``/``protocol``）旧（``v``/``proto``）
    字段名；返回的 dict 统一补齐新字段名，调用方无需再区分。
    """
    try:
        msg = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(msg, dict):
        return None
    version = msg.get("version", msg.get("v"))
    if version != 1:
        return None
    proto = msg.get("protocol", msg.get("proto"))
    if not isinstance(proto, str) or not proto.startswith(PROTO_PREFIX):
        return None
    if msg.get("kind") not in KINDS:
        return None
    if not isinstance(msg.get("id"), str) or not msg.get("id"):
        return None
    # 统一回填新字段名（原文里可能只有旧名）
    msg.setdefault("version", 1)
    msg.setdefault("protocol", proto)
    msg.setdefault("timestamp", msg.get("ts", now()))
    if "service" not in msg:
        svc = msg.get("svc")
        if isinstance(svc, str):
            msg["service"] = svc
    if "capabilities" not in msg:
        caps = msg.get("caps")
        if isinstance(caps, list):
            msg["capabilities"] = caps
    if "security" not in msg:
        sec = msg.get("sec")
        if isinstance(sec, dict):
            msg["security"] = sec
    return msg


def build_query(agent_id: str, want: str = "*") -> bytes:
    """构造智能体的扫描报文（§3.1）：设备收到后按 want 过滤并以 announce 应答。

    ``want`` 为品类过滤串（如 ``light,display``），``*`` 表示不过滤。
    """
    return _dump(
        {
            "version": 1,
            "protocol": PROTO,
            "kind": "query",
            "timestamp": now(),
            "id": agent_id,
            "want": want or "*",
        }
    )


def build_announce(device: dict) -> bytes:
    """构造设备上线/状态变化广播（§4.2）：周期性发送，兼作心跳；new 设备上电后立即发一次。"""
    msg = {"version": 1, "protocol": PROTO, "kind": "announce", "timestamp": now()}
    msg.update(device)
    return _dump(msg)


def build_goodbye(device: dict) -> bytes:
    """构造设备下线告别报文（§4.3）：设备优雅退出时发送一次，智能体收到后移除该设备。"""
    msg = {"version": 1, "protocol": PROTO, "kind": "goodbye", "timestamp": now(), "state": "offline", "event": "offline"}
    msg.update(device)
    return _dump(msg)


def _dump(msg: dict) -> bytes:
    """一个数据报 = 一个 UTF-8 JSON 对象，≤1400 字节。

    超长时逐级收缩摘要字段（截短 summary → 丢弃 summary/model/ipv6/event →
    丢弃 capabilities/security 摘要），保证接收方拿到的始终是合法 JSON；
    仅极端情况下才硬截断。
    """
    data = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    if len(data) <= MAX_DGRAM:
        return data
    trimmed = dict(msg)
    summary = trimmed.get("summary")
    if isinstance(summary, str):
        for cut in (80, 40):
            trimmed["summary"] = summary[:cut]
            data = json.dumps(trimmed, ensure_ascii=False).encode("utf-8")
            if len(data) <= MAX_DGRAM:
                return data
        trimmed.pop("summary", None)
    for key in ("model", "event", "ipv6", "capabilities", "security"):
        trimmed.pop(key, None)
        data = json.dumps(trimmed, ensure_ascii=False).encode("utf-8")
        if len(data) <= MAX_DGRAM:
            return data
    return data[:MAX_DGRAM]


def parse_control_uri(uri: str | None) -> tuple[str, int] | None:
    """解析 tcp://IP:PORT / tcp://[IPv6]:PORT → (host, port)。"""
    if not uri:
        return None
    m = _URI_RE.match(uri.strip())
    if not m:
        return None
    host = m.group(1)
    if host.startswith("["):
        host = host[1:-1]
    return host, int(m.group(2))


def matches_want(device_type: str | None, want: str | None) -> bool:
    """query.want 匹配算法（§4.4）：空 / * / bmahs = 全部；否则逗号分隔 type 列表。"""
    device_type = (device_type or "").lower()
    parts = [w.strip().lower() for w in (want or "*").split(",") if w.strip()]
    if not parts or "*" in parts or "bmahs" in parts:
        return True
    return device_type in parts


def hb_of(msg: dict) -> float:
    """读取设备公告的心跳间隔（§5.1 ``hb``，缺省视为 5 秒）。"""
    hb = msg.get("hb")
    if isinstance(hb, (int, float)) and not isinstance(hb, bool) and hb > 0:
        return float(hb)
    return DEFAULT_HB


def expire_sec_for(hb: float) -> float:
    """无心跳删除时限（§4.6/§4.8-7）：clamp(12 × hb, 60 秒, 30 分钟)。"""
    return max(60.0, min(12.0 * max(float(hb), DEFAULT_HB), 1800.0))
