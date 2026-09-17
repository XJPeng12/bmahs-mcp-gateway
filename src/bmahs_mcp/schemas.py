"""把 BMAHS ``hello.ops`` 转成 MCP 工具 schema（协议 §4.5 / §4.8）。

协议要求智能体运行时：
- 工具说明来自 ``desc``，参数定义只来自 ``args``，返回说明只来自 ``result`` / ``returns``；
- ``name`` / ``summary`` / ``hint`` / ``security.notes`` 等自然语言必须交给模型，不得剥掉；
- 兼容 bmahs/1 的旧字符串数组 ``args``：按 type=string、required=false 理解。
"""

from __future__ import annotations

import hashlib
import re

# BMAHS 参数类型 -> JSON Schema 类型（§4.5）；int/float 是 bool 型别名的归一化
TYPE_MAP = {
    "string": "string",
    "int": "integer",
    "integer": "integer",
    "number": "number",
    "float": "number",
    "bool": "boolean",
    "boolean": "boolean",
    "object": "object",
    "array": "array",
}


def normalize_args(op: dict) -> list[dict]:
    """把某动作的 args 归一为统一的 dict 列表（新协议原样保留，旧版字符串数组补默认定义）。"""
    args = op.get("args") or []
    if not isinstance(args, list):
        return []
    out: list[dict] = []
    for a in args:
        if isinstance(a, str) and a:
            # 兼容旧版（bmahs/1）字符串数组 args：按 type=string、required=false 理解
            out.append({"name": a, "type": "string", "required": False,
                        "description": "（旧版设备未提供参数说明；可发 describe 获取完整清单）"})
        elif isinstance(a, dict) and a.get("name"):
            out.append(a)
    return out


def arg_property(a: dict) -> dict:
    """把一个参数定义转成 JSON Schema 的 property 片段（type/description/min/max/enum…）。

    description/unit/enum/default/example 拼成一句自然语言说明，帮助模型理解参数含义。
    """
    jtype = TYPE_MAP.get(str(a.get("type", "string")).lower(), "string")
    parts: list[str] = []
    # §4.5 字段名为 description；兼容旧版 bmahs/1.2 设备的 desc
    desc = a.get("description") or a.get("desc")
    if desc:
        parts.append(str(desc))
    if a.get("unit"):
        parts.append(f"单位：{a['unit']}。")
    enum = a.get("enum")
    # enum 必须是数组才进 schema；字符串等畸形值会被 join 成单字符列表
    if isinstance(enum, list) and enum:
        parts.append("可选值：" + "、".join(str(x) for x in enum) + "。")
    if a.get("default") is not None:
        parts.append(f"缺省值：{a['default']}。")
    if a.get("example") is not None:
        parts.append(f"示例：{a['example']}。")
    prop: dict = {"type": jtype}
    if parts:
        prop["description"] = " ".join(parts)
    if isinstance(a.get("min"), (int, float)) and not isinstance(a.get("min"), bool):
        prop["minimum"] = a["min"]
    if isinstance(a.get("max"), (int, float)) and not isinstance(a.get("max"), bool):
        prop["maximum"] = a["max"]
    if isinstance(enum, list) and enum:
        prop["enum"] = enum
    return prop


def input_schema(op: dict) -> dict:
    """把动作的 args 列表转成 MCP 工具的完整 inputSchema（含 required 数组）。"""
    props: dict = {}
    required: list[str] = []
    for a in normalize_args(op):
        props[str(a["name"])] = arg_property(a)
        if a.get("required") is True:
            required.append(str(a["name"]))
    schema: dict = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


def tool_description(hello: dict, op: dict) -> str:
    """组装工具说明：设备自述 + 动作说明 + 结果读法 + 约束 + 安全边界。

    §4.5：``any_of`` 写入工具说明（不在 JSON Schema 里强校验），供模型发请求前自查。
    """
    name = hello.get("name") or hello.get("id") or "?"
    summary = hello.get("summary") or ""
    lines = [f"[BMAHS 设备「{name}」] {summary}".rstrip()]
    desc = op.get("description") or op.get("desc") or op.get("name") or ""
    lines.append(str(desc))
    if op.get("result"):
        lines.append(f"成功时：{op['result']}")
    any_of = op.get("any_of") or []
    if any_of:
        lines.append("参数约束：" + "、".join(f"「{a}」" for a in any_of) + " 至少提供一个。")
    sec = hello.get("security") or {}
    if sec.get("notes"):
        lines.append(f"安全边界：{sec['notes']}")
    if hello.get("hint"):
        lines.append(f"设备提示：{hello['hint']}")
    return "\n".join(x for x in lines if x)


def mcp_tool_name(device_id: str, action: str) -> str:
    """工具名 ``<设备id>__<动作>``，限定 ``[A-Za-z0-9_-]{1,64}``；超长时哈希截断防碰撞。"""
    raw = f"{device_id}__{action}"
    name = re.sub(r"[^A-Za-z0-9_-]", "_", raw)
    if len(name) > 64:
        digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:6]
        name = name[:57] + "-" + digest
    return name


def find_op(hello: dict, action: str) -> dict | None:
    """在设备 hello 的操作清单（新 operations / 旧 ops）里按名字找动作定义。"""
    for op in hello.get("operations") or hello.get("ops") or []:
        if isinstance(op, dict) and op.get("name") == action:
            return op
    return None
