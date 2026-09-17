"""BMAHS（比马斯）设备协议 ↔ MCP 网关。

把局域网内按 bmahs/1.0 发布的设备（解析侧兼容旧版 bmahs/1–1.2 字段名）
动态映射为 MCP 工具，使大模型可以通过 Model Context Protocol
发现、占用与操作 BMAHS 硬件。
"""

__version__ = "0.1.1"
