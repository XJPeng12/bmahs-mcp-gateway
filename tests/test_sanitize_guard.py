"""参数净化器与防循环守卫的单测（防线②③，docs/工具调用参数死循环_网关侧防护方案.md §9.1）。

不依赖真实设备：替换 discovery 为内存注册表、替换 _raw_call 捕获下发报文，
覆盖 device 解包 / 类型矫正 / 近似匹配 / 动作建议 / 重复失败升级 / describe 可选。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from bmahs_mcp import sanitize
from bmahs_mcp.discovery import Device
from bmahs_mcp.gateway import DeviceEnvelope, Gateway, GatewayError


class _FakeDiscovery:
    """内存注册表：resolve_quiet / near_match 所需的 get/all 接口。"""

    def __init__(self, devices: list[Device]) -> None:
        self._by_key: dict[str, Device] = {d.id: d for d in devices}

    def all(self) -> list[Device]:
        return list(self._by_key.values())

    def get(self, device_id: str) -> Device | None:
        return self._by_key.get(device_id)


def make_device(dev_id: str = "lamp-01", name: str = "宝莲灯", ops: list | None = None) -> Device:
    hello: dict = {
        "id": dev_id,
        "name": name,
        "type": "light",
        "service": "light/1",
        "security": {"occupancy": "last-wins"},  # 免占用，单测不发 occupy 网络请求
    }
    if ops is not None:
        hello["operations"] = ops
    return Device(key=dev_id, announce={"id": dev_id, "name": name, "type": "light"}, hello=hello)


def make_gw(*devices: Device) -> Gateway:
    gw = Gateway()
    gw.discovery = _FakeDiscovery(list(devices))
    return gw


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ 防线②：device 引用净化


def test_device_dict_unpack_single_entry():
    """本案形态 {"lamp-01": "宝莲灯"} → 静默矫正为 "lamp-01"，附 coerced 标注。"""
    gw = make_gw(make_device())
    ref, notes, err = sanitize.coerce_device_ref(gw, {"lamp-01": "宝莲灯"})
    assert err is None and ref == "lamp-01"
    assert notes and notes[0]["to"] == "lamp-01"


def test_device_dict_key_and_value_both_resolve_same():
    gw = make_gw(make_device())
    ref, _notes, err = sanitize.coerce_device_ref(gw, {"lamp-01": "宝莲灯", "ref": "lamp-01"})
    assert err is None and ref == "lamp-01"


def test_device_dict_ambiguous_returns_rich_error():
    gw = make_gw(make_device("lamp-01", "宝莲灯"), make_device("lamp-02", "书房灯"))
    bad = {"a": "宝莲灯", "b": "书房灯"}
    ref, _notes, err = sanitize.coerce_device_ref(gw, bad)
    assert ref is None and err is not None
    assert err["retry_with"]["device"] == "lamp-01"  # 示例取排序第一的真实 id
    assert err["echo"]["device"] == bad
    assert err["candidates"]


def test_device_list_takes_resolvable_element():
    gw = make_gw(make_device())
    ref, notes, err = sanitize.coerce_device_ref(gw, ["lamp-01"])
    assert err is None and ref == "lamp-01" and notes


def test_device_wrong_type_errors_with_example():
    gw = make_gw(make_device())
    for bad in (42, 4.2, True):
        ref, _notes, err = sanitize.coerce_device_ref(gw, bad)
        assert ref is None and err["retry_with"]["device"] == "lamp-01"


def test_device_missing_gives_retry_with():
    gw = make_gw(make_device())
    for blank in (None, ""):
        ref, _notes, err = sanitize.coerce_device_ref(gw, blank)
        assert ref is None and err["retry_with"]["device"] == "lamp-01"


def test_device_near_match_unique_autocorrects():
    gw = make_gw(make_device())
    ref, notes, err = sanitize.coerce_device_ref(gw, "lamp01")
    assert err is None and ref == "lamp-01" and notes


def test_resolve_device_failure_has_rich_envelope():
    gw = make_gw(make_device())
    with pytest.raises(GatewayError) as ei:
        gw.resolve_device("no-such-dev")
    env = ei.value.envelope
    assert env and env["echo"] == {"device": "no-such-dev"}
    assert env["retry_with"] == {"device": "lamp-01"}
    assert any("lamp-01" in c for c in env["candidates"])


# ------------------------------------------------------------------ 防线②：类型 / enum / args 矫正


def test_coerce_int_strings_and_floats():
    gw = make_gw()
    v, notes, err = sanitize.coerce_int("120", arg="ttl")
    assert (v, err) == (120, None) and notes
    v, _notes, err = sanitize.coerce_int(120.0, arg="ttl")
    assert (v, err) == (120, None)
    assert sanitize.coerce_int(None, arg="ttl") == (None, [], None)
    _v, _n, err = sanitize.coerce_int(True, arg="ttl")
    assert err and err["retry_with"] == {"ttl": 120}
    _v, _n, err = sanitize.coerce_int("两分钟", arg="ttl")
    assert err and "示例" in err["error"]


def test_coerce_op_arguments_typed_and_enum():
    op = {
        "name": "brightness",
        "args": [
            {"name": "level", "type": "int", "min": 0, "max": 100},
            {"name": "mode", "type": "string", "enum": ["on", "off"]},
        ],
    }
    out, notes = sanitize.coerce_op_arguments(op, {"level": "66", "mode": "ON"})
    assert out == {"level": 66, "mode": "on"}
    assert {n["arg"] for n in notes} == {"level", "mode"}
    # 矫正不了的原样保留
    out2, notes2 = sanitize.coerce_op_arguments(op, {"level": "abc"})
    assert out2 == {"level": "abc"} and not notes2
    # 未声明键不动
    out3, _ = sanitize.coerce_op_arguments(op, {"extra": "x"})
    assert out3 == {"extra": "x"}


def test_coerce_args_object_shapes():
    op = {"name": "brightness", "args": [{"name": "level", "type": "int", "example": 50}]}
    args, notes, err = sanitize.coerce_args_object([{"level": 1}], op)
    assert err is None and args == {"level": 1} and notes
    assert sanitize.coerce_args_object(None, op) == (None, [], None)
    args, _n, err = sanitize.coerce_args_object("x", op)
    assert args is None and err["retry_with"] == {"args": {"level": 50}}


def test_near_match_action():
    hello = {"operations": [{"name": "on"}, {"name": "off"}, {"name": "brightness"}]}
    hit, names = sanitize.near_match_action(hello, "of")
    assert hit == "off" and set(names) == {"on", "off", "brightness"}
    hit2, _ = sanitize.near_match_action(hello, "完全不像的名字")
    assert hit2 is None


# ------------------------------------------------------------------ 防线③：重复失败守卫


def test_guard_escalates_and_directives_stop():
    gw = make_gw(make_device())
    args = {"device": "lamp-01"}
    base = {"ok": False, "code": "bad-arg", "error": "原始错误"}
    e1 = gw._guarded_error("local", "bmahs_describe", args, dict(base))
    assert "repeat_count" not in e1
    e2 = gw._guarded_error("local", "bmahs_describe", args, dict(base))
    assert e2["repeat_count"] == 2 and "第 2 次" in e2["error"] and "hint" in e2
    e3 = gw._guarded_error("local", "bmahs_describe", args, dict(base))
    assert e3["repeat_count"] == 3 and e3["directive"] == "stop"
    # 逐级措辞必须变化（字节级相同的错误本身就是强化燃料）
    assert e2["error"] != e3["error"]


def test_guard_resets_on_different_call_and_isolates_sessions():
    gw = make_gw(make_device())
    base = {"ok": False, "error": "x"}
    gw._guarded_error("local", "bmahs_describe", {"device": "lamp-01"}, dict(base))
    gw._guarded_error("local", "bmahs_describe", {"device": "lamp-01"}, dict(base))
    # 换参数 → 计数重置
    e3 = gw._guarded_error("local", "bmahs_describe", {"device": "lamp-02"}, dict(base))
    assert "repeat_count" not in e3
    # 换会话 → 互不影响
    e4 = gw._guarded_error("s2", "bmahs_describe", {"device": "lamp-01"}, dict(base))
    assert "repeat_count" not in e4


def test_guard_disabled_via_env(monkeypatch):
    monkeypatch.setenv("BMAHS_LOOP_GUARD", "0")
    gw = make_gw(make_device())
    base = {"ok": False, "error": "x"}
    for _ in range(4):
        out = gw._guarded_error("local", "t", {"a": 1}, dict(base))
    assert "repeat_count" not in out


# ------------------------------------------------------------------ 防线①：describe 可选 + 工具描述示例


def test_describe_optional_branches():
    # 0 台：信息性返回
    gw0 = make_gw()
    res0 = json.loads(run(gw0._tool_describe({}, "local"))[0].text)
    assert res0["ok"] is True and res0["count"] == 0 and "bmahs_refresh" in res0["note"]
    # 多台：返回选择清单而非报错
    gwn = make_gw(make_device("lamp-01"), make_device("lamp-02", "书房灯"))
    resn = json.loads(run(gwn._tool_describe({}, "local"))[0].text)
    assert resn["ok"] is True and resn["count"] == 2 and "未指定 device" in resn["note"]
    # 唯一台：自动选中并附 coerced（_raw_call 打桩避免真实网络）
    gw1 = make_gw(make_device())
    gw1._raw_call = _fake_raw_call
    res1 = json.loads(run(gw1._tool_describe({}, "local"))[0].text)
    assert res1["ok"] is True and res1["coerced"][0]["to"] == "lamp-01"


async def _fake_raw_call(dev, payload):  # noqa: ANN001
    return (dev.hello, {"ok": True, "action": payload.get("action"), "operations": []})


def test_describe_dict_device_end_to_end():
    """案件重放：{"device": {"lamp-01": "宝莲灯"}} 经 describe 全路径 → 成功 + coerced。"""
    gw = make_gw(make_device())
    gw._raw_call = _fake_raw_call
    blocks = run(gw.call_tool("bmahs_describe", {"device": {"lamp-01": "宝莲灯"}}))
    payload = json.loads(blocks[0].text)
    assert payload["ok"] is True
    assert any(c["arg"] == "device" and c["to"] == "lamp-01" for c in payload["coerced"])


def test_bmahs_call_action_suggestion_and_readonly_autocorrect():
    # 控制动作近似：只建议、不代执行
    gw = make_gw(make_device(ops=[{"name": "on", "args": []}]))
    with pytest.raises(DeviceEnvelope) as ei:
        run(gw.call_tool("bmahs_call", {"device": "lamp-01", "action": "onn"}))
    env = ei.value.envelope
    assert env["retry_with"] == {"device": "lamp-01", "action": "on"}
    # 只读动作近似：自动改写并执行（who 在 READONLY_ACTIONS 里）
    gw2 = make_gw(make_device(ops=[{"name": "who", "args": []}]))
    gw2._raw_call = _fake_raw_call
    blocks = run(gw2.call_tool("bmahs_call", {"device": "lamp-01", "action": "whoo"}))
    payload = json.loads(blocks[0].text)
    assert payload["ok"] is True
    assert any(c["arg"] == "action" and c["to"] == "who" for c in payload["coerced"])


def test_bmahs_call_args_coerced_end_to_end():
    captured: dict = {}

    async def capture_raw_call(dev, payload):  # noqa: ANN001
        captured.update(payload)
        return (dev.hello, {"ok": True, "action": payload.get("action")})

    gw = make_gw(make_device(ops=[{"name": "brightness", "args": [{"name": "level", "type": "int"}]}]))
    gw._raw_call = capture_raw_call
    blocks = run(
        gw.call_tool("bmahs_call", {"device": "lamp-01", "action": "brightness", "args": {"level": "66"}})
    )
    assert captured["level"] == 66  # 字符串数字已按声明转为整数
    payload = json.loads(blocks[0].text)
    assert payload["ok"] is True and payload["coerced"][0]["arg"] == "level"


def test_list_tools_descriptions_and_describe_optional(monkeypatch):
    gw = make_gw(make_device())
    tools = run(gw.list_tools())
    by_name = {t.name: t for t in tools}
    dev_prop = by_name["bmahs_occupy"].input_schema["properties"]["device"]
    assert "禁止传对象" in dev_prop["description"] and "lamp-01" in dev_prop["description"]
    assert "brightness" in by_name["bmahs_call"].input_schema["properties"]["args"]["description"]
    # describe 默认可选：schema 无 required
    assert "required" not in by_name["bmahs_describe"].input_schema
    # 关闭后恢复必填
    monkeypatch.setenv("BMAHS_DESCRIBE_OPTIONAL", "0")
    gw2 = Gateway()
    gw2.discovery = _FakeDiscovery([make_device()])
    tools2 = run(gw2.list_tools())
    desc2 = next(t for t in tools2 if t.name == "bmahs_describe")
    assert desc2.input_schema.get("required") == ["device"]


def test_tool_devices_note_carries_template():
    gw = make_gw(make_device())
    gw.query_once = _noop_query
    res = run(gw.tool_devices())
    assert "填参提醒" in res["note"] and "lamp-01" in res["note"]


async def _noop_query():  # noqa: ANN201
    return None


def test_device_describe_tools_alias():
    import os

    os.environ["BMAHS_DEVICE_TOOLS"] = "1"
    try:
        gw = make_gw(make_device())
        assert gw._rebuild_tools()
        names = {t.name for t in run(gw.list_tools())}
        assert "lamp-01__describe" in names
        gw._raw_call = _fake_raw_call
        blocks = run(gw.call_tool("lamp-01__describe", {}))
        assert json.loads(blocks[0].text)["ok"] is True
    finally:
        os.environ.pop("BMAHS_DEVICE_TOOLS", None)
