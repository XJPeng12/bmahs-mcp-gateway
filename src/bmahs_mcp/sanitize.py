"""参数净化器（防线②，docs/工具调用参数死循环_网关侧防护方案.md §5）。

背景：本地小模型填工具参数时会写出「形状正确但内容畸形」的调用（device 传成
``{id: 名称}`` 映射、整数传成字符串数字、动作名近似拼错…），报错后原样重试即
锁死成循环。本模块在调用分发前做一次无歧义矫正，把「首错」变成「首成功」。

三条原则：
1. 无歧义才静默矫正：解包/转换/近似匹配结果唯一才自动改，歧义一律报错并附候选；
2. 矫正必须透明：静默矫正的清单（coerced）随成功响应回带，模型下一轮直接填对；
3. 控制类动作只建议、不代执行：动作名近似匹配仅对只读动作自动改写。

所有报错都用富错误信封（:func:`rich_error`）：``echo`` 用 JSON 语法回显实际传参
（杜绝 Python repr 诱导模型写出 Python 字典），``retry_with`` 给出可逐字照抄的
正确形态——利用「模型照抄上下文最近先例」的机制，让最近的先例变成正确模板。
"""

from __future__ import annotations

import difflib
import json
from typing import TYPE_CHECKING, Any

from . import schemas

if TYPE_CHECKING:
    from .gateway import Gateway

# 布尔字面量仅限无歧义的四个（方案 §5.1）；yes/no/on/off 等语义因设备而异，
# 不做静默矫正，交设备按自己的 bad-arg 语义拒绝
_TRUE_STRINGS = {"true", "1"}
_FALSE_STRINGS = {"false", "0"}

# 近似匹配阈值：id/动作名编辑距离敏感场景，0.75 足以抓住 "lamp01"→"lamp-01"
# 这类漏字符/加连字符错误，又不会把 lamp-01/lamp-02 混为一谈
_DEVICE_MATCH_CUTOFF = 0.75
_ACTION_MATCH_CUTOFF = 0.6


def rich_error(
    error: str,
    *,
    code: str = "bad-arg",
    echo: Any = None,
    retry_with: Any = None,
    candidates: list[str] | None = None,
    retryable: bool = False,
) -> dict:
    """组装富错误信封（§4.7 兼容：ok/action 语义不变，额外字段模型可读）。"""
    env: dict = {"ok": False, "code": code, "error": error, "retryable": retryable}
    if echo is not None:
        env["echo"] = echo  # 你实际传的（JSON 风格回显）
    if retry_with is not None:
        env["retry_with"] = retry_with  # 可逐字照抄的正确形态
    if candidates:
        env["candidates"] = candidates
    return env


def example_device_ref(gw: "Gateway") -> str:
    """取一个真实设备 id 做示例（排序第一台）；无设备时回退占例 ``lamp-01``。"""
    devs = sorted(gw.discovery.all(), key=lambda d: d.id)
    return devs[0].id if devs else "lamp-01"


def known_device_list(gw: "Gateway", limit: int = 8) -> list[str]:
    """当前已知设备的「id（显示名）」清单，报错候选用。"""
    devs = sorted(gw.discovery.all(), key=lambda d: d.id)
    items = [f"{d.id}（{d.name}）" for d in devs]
    if len(items) > limit:
        items = items[:limit] + [f"…（共 {len(devs)} 台）"]
    return items


def near_match_device(gw: "Gateway", ref: str) -> tuple[str | None, list[str]]:
    """difflib 近似匹配设备 id/显示名。

    返回 ``(唯一命中的设备 id 或 None, 其余候选 id 列表)``；命中的映射到设备 id，
    避免把显示名原样返回（显示名不是规范引用）。
    """
    pool: dict[str, str] = {}
    for d in gw.discovery.all():
        pool[d.id] = d.id
        pool.setdefault(d.name, d.id)
    close = difflib.get_close_matches(ref, list(pool.keys()), n=2, cutoff=_DEVICE_MATCH_CUTOFF)
    ids = sorted({pool[c] for c in close})
    if len(ids) == 1:
        return ids[0], []
    return None, ids


def near_match_action(hello: dict, action: str) -> tuple[str | None, list[str]]:
    """在设备操作清单里近似匹配动作名。返回 ``(唯一命中或 None, 全部动作名)``。"""
    names = [
        str(op.get("name"))
        for op in (hello.get("operations") or hello.get("ops") or [])
        if isinstance(op, dict) and op.get("name")
    ]
    close = difflib.get_close_matches(action, names, n=2, cutoff=_ACTION_MATCH_CUTOFF)
    if len(close) == 1:
        return close[0], names
    return None, names


def coerce_device_ref(gw: "Gateway", value: Any, *, arg: str = "device") -> tuple[str | None, list[dict], dict | None]:
    """净化设备引用参数。

    返回 ``(ref, notes, error)``：

    - ``ref``：矫正后的引用字符串；``None`` 表示无法矫正（``error`` 已给出富错误）；
    - ``notes``：coerced 透明标注（成功响应回带给模型）；
    - ``error``：富错误信封（无法矫正时非 ``None``）。
    """
    notes: list[dict] = []
    example = example_device_ref(gw)
    # 0) 缺失 / 空串：报错并给真实示例
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, notes, rich_error(
            f"缺少 {arg} 参数：请传设备 id 字符串本身，如 {json.dumps(example, ensure_ascii=False)}"
            "（可先用 bmahs_devices 查询设备列表）。",
            echo={arg: value},
            retry_with={arg: example},
        )
    # 1) 字符串：能解析直接用；解析不了做近似推荐（唯一命中才自动矫正）
    if isinstance(value, str):
        ref = value.strip()
        if gw.resolve_quiet(ref) is not None:
            return ref, notes, None
        hit, _others = near_match_device(gw, ref)
        if hit is not None:
            notes.append(
                {
                    "arg": arg,
                    "from": ref,
                    "to": hit,
                    "note": f"已把 {arg} 从 {json.dumps(ref, ensure_ascii=False)} 近似矫正为 {json.dumps(hit, ensure_ascii=False)}",
                }
            )
            return hit, notes, None
        # 近似也无唯一命中：交回 resolve_device 报错（其错误附已知设备清单与 retry_with）
        return ref, notes, None
    # 2) 对象：本案的 {"lamp-01": "宝莲灯"} 形态——取能唯一解析出设备的键/值
    if isinstance(value, dict):
        entries = list(value.items())
        hits: set[str] = set()
        for k, v in entries:
            for cand in (k, v):
                if isinstance(cand, str):
                    dev = gw.resolve_quiet(cand.strip())
                    if dev is not None:
                        hits.add(dev.id)
        if len(hits) == 1:
            dev_id = next(iter(hits))
            notes.append(
                {
                    "arg": arg,
                    "from": value,
                    "to": dev_id,
                    "note": f"{arg} 传了对象，已自动取其中的设备 id {json.dumps(dev_id, ensure_ascii=False)}；"
                    f"下次请直接传字符串，不要传 {{id: 名称}} 映射",
                }
            )
            return dev_id, notes, None
        # 无法唯一解析：单条目对象把键当作意图引用交回解析，否则报富错误
        if len(entries) == 1 and isinstance(entries[0][0], str):
            return entries[0][0].strip(), notes, None
        return None, notes, rich_error(
            f"{arg} 参数收到了对象且无法唯一解析出设备（收到 {json.dumps(value, ensure_ascii=False, default=str)}）。"
            f"{arg} 必须是设备 id 字符串本身，如 {json.dumps(example, ensure_ascii=False)}。",
            echo={arg: value},
            retry_with={arg: example},
            candidates=known_device_list(gw) or None,
        )
    # 3) 列表：取第一个能解析出设备的字符串元素；单元素字符串列表取首元素
    if isinstance(value, list):
        for el in value:
            if isinstance(el, str):
                dev = gw.resolve_quiet(el.strip())
                if dev is not None:
                    notes.append(
                        {
                            "arg": arg,
                            "from": value,
                            "to": dev.id,
                            "note": f"{arg} 传了数组，已取其中可解析的元素 {json.dumps(dev.id, ensure_ascii=False)}；下次请直接传字符串",
                        }
                    )
                    return dev.id, notes, None
        if len(value) == 1 and isinstance(value[0], str):
            notes.append(
                {
                    "arg": arg,
                    "from": value,
                    "to": value[0].strip(),
                    "note": f"{arg} 传了单元素数组，已取首元素；下次请直接传字符串",
                }
            )
            return value[0].strip(), notes, None
        return None, notes, rich_error(
            f"{arg} 参数收到了数组且无法解析出设备。{arg} 必须是设备 id 字符串本身，"
            f"如 {json.dumps(example, ensure_ascii=False)}。",
            echo={arg: value},
            retry_with={arg: example},
            candidates=known_device_list(gw) or None,
        )
    # 4) 数字/布尔等其他类型：直接报错
    return None, notes, rich_error(
        f"{arg} 参数类型错误：需要设备 id 字符串，收到 {type(value).__name__}。"
        f"示例：{json.dumps({arg: example}, ensure_ascii=False)}。",
        echo={arg: value},
        retry_with={arg: example},
    )


def coerce_int(value: Any, *, arg: str, example: int = 120) -> tuple[int | None, list[dict], dict | None]:
    """把数字字符串 / 整数值小数矫正为 int；返回 ``(值, notes, error)``。``None`` 原样通过。

    只做类型矫正，不查范围（范围语义归各工具自己的截断/校验逻辑）。
    """
    if value is None:
        return None, [], None
    if isinstance(value, bool):
        return None, [], rich_error(
            f"{arg} 必须是整数，收到布尔值。示例：{json.dumps({arg: example}, ensure_ascii=False)}。",
            echo={arg: value},
            retry_with={arg: example},
        )
    if isinstance(value, int):
        return value, [], None
    if isinstance(value, float) and value.is_integer():
        iv = int(value)
        return iv, [{"arg": arg, "from": value, "to": iv, "note": f"{arg} 收到小数，已取整为 {iv}"}], None
    if isinstance(value, str):
        try:
            iv = int(value.strip())
        except ValueError:
            return None, [], rich_error(
                f"{arg} 必须是整数，收到字符串 {json.dumps(value, ensure_ascii=False)}。"
                f"示例：{json.dumps({arg: example}, ensure_ascii=False)}。",
                echo={arg: value},
                retry_with={arg: example},
            )
        return iv, [{"arg": arg, "from": value, "to": iv, "note": f"{arg} 收到字符串数字，已转为整数 {iv}"}], None
    return None, [], rich_error(
        f"{arg} 必须是整数，收到 {type(value).__name__}。示例：{json.dumps({arg: example}, ensure_ascii=False)}。",
        echo={arg: value},
        retry_with={arg: example},
    )


def _type_example(a: dict) -> Any:  # noqa: ANN001
    """按参数声明挑一个示例值：example > default > enum[0] > 类型占位。"""
    for key in ("example", "default"):
        if isinstance(a.get(key), (str, int, float, bool)):
            return a[key]
    enum = a.get("enum")
    if isinstance(enum, list) and enum and isinstance(enum[0], (str, int, float, bool)):
        return enum[0]
    t = schemas.TYPE_MAP.get(str(a.get("type", "string")).lower(), "string")
    return {"integer": 1, "number": 1.0, "boolean": True, "string": "…", "object": {}, "array": []}[t]


def coerce_args_object(value: Any, op: dict | None) -> tuple[dict | None, list[dict], dict | None]:
    """bmahs_call 的 args 参数：必须是对象；单元素 ``[obj]`` 取元素。

    返回 ``(args, notes, error)``；``op`` 用于在报错时给出该动作的真实参数示例。
    """
    if value is None:
        return None, [], None
    if isinstance(value, dict):
        return value, [], None
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
        return (
            value[0],
            [{"arg": "args", "from": value, "to": value[0], "note": "args 传了单元素数组，已取其中的对象；下次直接传对象"}],
            None,
        )
    example: dict = {}
    if op is not None:
        for a in schemas.normalize_args(op)[:3]:
            example[str(a["name"])] = _type_example(a)
    return None, [], rich_error(
        "args 必须是对象（键为该动作的参数名），收到 "
        + type(value).__name__
        + "。示例："
        + json.dumps({"args": example}, ensure_ascii=False)
        + "。",
        echo={"args": value},
        retry_with={"args": example} if example else None,
    )


def coerce_op_arguments(op: dict, arguments: dict) -> tuple[dict, list[dict]]:
    """按动作 args 声明矫正参数值类型 / enum；返回 ``(新参数, notes)``。

    只做无歧义矫正（字符串数字→数值、布尔字面量→布尔、string 槽收数字→字符串、
    enum 忽略大小写/空白唯一匹配），矫正不了原样保留交设备判断（其信封错误会经
    防线③守卫升级提示）；未声明的键原样保留。
    """
    specs = {str(a.get("name")): a for a in schemas.normalize_args(op)}
    out = dict(arguments)
    notes: list[dict] = []
    for key, val in list(out.items()):
        spec = specs.get(key)
        if not spec:
            continue
        jtype = schemas.TYPE_MAP.get(str(spec.get("type", "string")).lower(), "string")
        new = val
        changed = False
        if jtype in ("integer", "number") and isinstance(val, str):
            try:
                new = int(val.strip()) if jtype == "integer" else float(val.strip())
                changed = True
            except ValueError:
                pass
        elif jtype == "boolean" and isinstance(val, str):
            low = val.strip().lower()
            if low in _TRUE_STRINGS:
                new, changed = True, True
            elif low in _FALSE_STRINGS:
                new, changed = False, True
        elif jtype == "string" and isinstance(val, (int, float)) and not isinstance(val, bool):
            new, changed = str(val), True
        # enum 归一匹配：trim + 忽略大小写，唯一命中才矫正
        enum = spec.get("enum")
        if isinstance(enum, list) and enum and new not in enum:
            matches = [e for e in enum if str(e).strip().casefold() == str(new).strip().casefold()]
            if len(matches) == 1:
                new, changed = matches[0], True
        if changed and new != val:
            out[key] = new
            notes.append(
                {
                    "arg": key,
                    "from": val,
                    "to": new,
                    "note": f"参数 {key} 已按声明类型/可选值从 {json.dumps(val, ensure_ascii=False)} "
                    f"矫正为 {json.dumps(new, ensure_ascii=False)}",
                }
            )
    return out, notes
