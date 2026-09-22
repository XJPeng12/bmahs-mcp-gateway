# bmahs-mcp-gateway

> **BMAHS（比马斯）** 是一个开放的局域网硬件协议：每台设备上电即用自然语言「自我介绍」——我是谁、能做什么、安全边界在哪——让大模型智能体像接入 USB 设备一样，即插即用地发现、识别、按权限独占并安全地操作它们。

BMAHS（比马斯）设备协议 ↔ MCP 网关：把局域网内按 `bmahs/1.0` 协议发布的硬件设备（解析侧兼容旧版 1–1.2 字段名）动态映射为 [Model Context Protocol](https://modelcontextprotocol.io) 工具，让大模型客户端可以直接发现、占用与操作这些设备。

## 特性

- **零配置发现**：UDP 组播（`239.255.42.42:5354` / `[ff02::4242]:5354`）+ Bonjour/mDNS 双通道，设备上线即被识别；也支持 `BMAHS_STATIC_DEVICES` 静态设备表（适配不支持组播的环境）。
- **动态工具映射**：设备的动作清单（`ops`）自动映射为 MCP 工具，含参数 Schema 与 `any_of` 预检，无需为每类设备写适配代码。
- **协议级占用安全**：控制前自动 `occupy`、自动携带 token、任务结束/进程退出自动 `release`，token 不写入 UDP/TXT/日志。
- **标准错误信封**：设备错误按 §4.7 信封（`ok/action/code/error/retryable`）透传给模型，`error` 为自然语言中文。
- **两种接入模式**：stdio（单客户端，MCP 客户端直接拉起）与 Streamable HTTP（多客户端共享一个网关进程，可选 Bearer Token 鉴权）。

## 安装

推荐隔离安装（uv tool 或 pipx）：包与依赖装在独立环境，不污染系统/conda Python，任何终端可直接用 `bmahs-mcp`：

```bash
uv tool install "bmahs-mcp-gateway[http]"   # 首选；只用 stdio 模式可去掉 [http]
pipx install "bmahs-mcp-gateway[http]"     # 等效的 pipx 写法
```

也可直接 pip 装（装进当前 Python 环境，依赖与其它包共享、可能冲突，换环境后命令不可用）：

```bash
pip install bmahs-mcp-gateway            # stdio 模式，最小依赖
pip install "bmahs-mcp-gateway[http]"    # 需要 Streamable HTTP 共享模式时
```

作为库集成时用 [uv](https://docs.astral.sh/uv/)：`uv add bmahs-mcp-gateway`。要求 Python ≥ 3.10。

验证：`bmahs-mcp --version` 输出 `bmahs-mcp 0.1.1`。

## 快速开始

```bash
# 扫描局域网内的 BMAHS 设备
bmahs-mcp discover

# 联调：对设备执行一个动作（控制类动作自动 occupy → 执行 → release）
bmahs-mcp ctl 客厅灯 on
bmahs-mcp ctl 客厅灯 brightness --arg level=80
bmahs-mcp ctl 客厅灯 scene --arg name=cinema --ttl 600        # 指定占用租约 600 秒
bmahs-mcp ctl 客厅灯 on --no-release                          # 动作后保持占用（打印 token）
bmahs-mcp ctl 客厅灯 release --arg token=<占有时返回的 token>  # 手动释放保持的占用

# 启动 MCP 网关（stdio，供 MCP 客户端连接；默认子命令）
bmahs-mcp serve

# 以 Streamable HTTP 共享模式启动（多客户端同时连接）
bmahs-mcp http --host 0.0.0.0 --port 9530 --token 换成你的令牌
```

在 MCP 客户端（ZCode / Claude Desktop 等）中配置 stdio 接入：

```json
{
  "mcpServers": {
    "bmahs": {
      "command": "bmahs-mcp",
      "args": ["serve"]
    }
  }
}
```

HTTP 模式的端点为 `http://<host>:9530/mcp`；设置了 `--token` 后客户端须携带 `Authorization: Bearer <token>`。

重启客户端后，模型可见两类工具：**7 个固定工具**——`bmahs_devices`（列设备）、`bmahs_refresh`（重扫描）、`bmahs_describe`（读自述）、`bmahs_occupy` / `bmahs_release`（占用/释放）、`bmahs_call`（泛化调用）、`bmahs_screenshot`（ui 设备抓屏）；以及**每台设备的动态工具**——`<设备id>__<动作>`（如 `demo-light-001__brightness`），参数说明来自设备自述。典型流程：`bmahs_devices` 选型 → 直接调动态工具（网关自动 occupy 并携带 token，默认 120 秒租约）→ 用完 `bmahs_release`；token 由网关代管并遮蔽，不进模型上下文。

## 常见问题

- **扫不到设备？** 确认设备已上电且同网段、Windows 防火墙放行 UDP 5354 入站；多网卡机器用 `BMAHS_MCAST_IF_V4` 指定网卡；跨网段/容器用 `BMAHS_STATIC_DEVICES=tcp://IP:端口` 静态表兜底。
- **报「正被 xxx 占用」（occupied）？** 设备独占中：`bmahs_devices` 看 `holder`/`until`，等租约到期或请占用方 `bmahs_release`；协议无强夺机制（防止两个模型打架）。
- **HTTP 模式 401？** 请求头须带 `Authorization: Bearer <--token 设置的值>`。
- **stdio 模式没有输出？** 正常：stdout 是 MCP 协议通道，日志全走 stderr（`BMAHS_LOG_LEVEL=debug` 调高）。

## 环境变量

| 变量 | 说明 |
| --- | --- |
| `BMAHS_AGENT_ID` | 网关在协议中的智能体 id（默认自动生成） |
| `BMAHS_STATIC_DEVICES` | 静态设备表，如 `tcp://192.168.1.10:9527`，逗号分隔 |
| `BMAHS_BONJOUR_BROWSE` | `0` 关闭 mDNS 浏览通道（默认开） |
| `BMAHS_AUTO_OCCUPY` / `BMAHS_AUTO_OCCUPY_TTL` / `BMAHS_MAX_LEASE` | 自动占用策略与租约上限 |
| `BMAHS_CALL_TIMEOUT` / `BMAHS_QUERY_INTERVAL` / `BMAHS_EXPIRE_SEC` | 调用超时、设备表刷新与过期时间 |
| `BMAHS_HTTP_HOST` / `BMAHS_HTTP_PORT` / `BMAHS_HTTP_PATH` / `BMAHS_HTTP_TOKEN` | HTTP 模式默认参数 |
| `BMAHS_LOG_LEVEL` | 日志级别（日志一律走 stderr，不污染 stdio 协议通道） |

## 协议

BMAHS 协议要点：UDP 组播一报文一 JSON（≤1400 字节）负责发现，TCP 一行 JSON + `\n` 负责控制，连接后先读设备 hello；网关在协议中承担「智能体」角色。完整协议文档见 [docs/BMAHS1.0.md](https://github.com/XJPeng12/bmahs-mcp-gateway/blob/main/docs/BMAHS1.0.md)。

## 本地开发与构建

```bash
uv sync        # 安装依赖（含 dev 组）
uv build       # 在本目录构建 wheel + sdist（注意在包目录内执行并加 --out-dir dist）
uv publish     # 发布到 PyPI（需配置 token）
uv run pytest  # 运行测试
```

## License

[MIT](https://github.com/XJPeng12/bmahs-mcp-gateway/blob/main/LICENSE)
