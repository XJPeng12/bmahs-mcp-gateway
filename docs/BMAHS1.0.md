# BMAHS （巴马斯）大模型智能体硬件标准

**BMAHS**（Big Model Agent Hardware Standard）是一项用于定义大模型智能体如何发现、连接、交互和控制外部硬件设备的标准。


| 项目   | 内容                                                                                                         |
| ---- | ---------------------------------------------------------------------------------------------------------- |
| 英文全称 | Big Model Agent Hardware Standard                                                                    |
| 定义   | 大模型智能体发现、连接、交互和控制外部硬件设备的标准                                                                                        |
| 文档状态 | 草案 / 现行候选（含操作界面与端点媒体通道）；参考实现的条款覆盖情况见 §7.3                                                                         |
| 适用对象 | 硬件制造商、智能体 / Agent 运行时、客户端开发者                                                                               |
| 传输   | 无线或有线均可，但本版只通过 **IP** 发现与控制：IPv4 / IPv6 UDP 组播 + 可选 Bonjour / DNS-SD（RFC 6762 / 6763），使用层为 `tcp://IP:PORT` |


硬件按本标准制造并发布自己；大模型智能体按本标准发现设备、连上 `control`，再按 `service` 操作和使用。本文规定**设备如何发布自己与下线、智能体如何发现并连上、连上之后如何声明自己、如何独占与报错**；若设备声明有**操作界面**，还规定如何用**非 JSON 的 UI 流**高效传画面；若声明有**端点**，还规定如何开媒体流供网关或对端消费。具体业务命令由设备的 `service` 定义，不在本文件内。

UDP 字段 `version` 仍为整数 **1**（主版本）。`protocol` 写 `bmahs/1.0`。

---



## 1. 目标

BMAHS 让大模型智能体把外部硬件当成可发现、可操作的工具：设备按本标准制造并实现后，任意智能体都能发现并使用它，不必为每种硬件单独写发现逻辑。

设备侧必须做到：

1. 经无线或有线加入 IP 网络，并取得地址（IPv4 和/或 IPv6）。物理链路不限；本版不规定 USB bulk、纯串口等非 IP 会话。
2. 按本协议**发布**自己（`announce`，下线发 `goodbye`）。
3. 应答智能体的**发现**请求（`query`）。
4. 在 `control` 入口提供可用的业务服务，并在连接后立即声明 `type` / `service`、**带类型的操作清单**与**安全边界**。
5. 维护自我控制状态，向占用方签发 **token**，并用稳定 `code` 报错。
6. **本机局域网地址（**`ip` **/** `ipv6`**）变化时，必须自动刷新发现注册**（见第 3.3 节），使智能体拿到的 `control` 始终可连。
7. **用自然语言描述自己**：发布与 `hello` 中提供可读的 `name` / `summary`（及完整 `operations` / `security` 自然语言说明）（见第 4.5 节）。

智能体侧必须做到：

1. 发送 `query`，并/或浏览 Bonjour `_bmahs._tcp`。
2. 收集 `announce`，按 `id` 去重；把 `name` / `summary` 交给模型做设备选型。
3. 连接 `control`，读取第一行 `hello`（含自然语言自述、操作清单与安全边界），再只调用清单内、且不越过安全边界的命令。
4. 遵守第 4.8 节的智能体义务（先看 `state`、占用、任务结束必须 `release`、收到 `goodbye` 即删除）。

BMAHS **不**规定：云账号、公网穿透、固件升级、各硬件类型业务语义、模型本身、USB-C 电气特性与非 IP 的USB 会话。这些由各 `service`、厂商私有通道、后续传输通道或智能体运行时处理。

---



## 2. 角色与分层

```
┌─────────────┐     announce / goodbye      ┌────────────────┐
│ BMAHS 设备   │ ──────────────────────────► │                │
│ (外部硬件)   │ ◄────────────────────────── │ 大模型智能体     │
└──────┬──────┘           query             └────────┬───────┘
       │                                             │
       │  control（如 tcp://IP:PORT）                  │
       └─────────────────────────────────────────────┘
              第一行 hello，之后按 service 使用
       若声明 ui：另开 UI 流（见 4.9），画面不走 control JSON
       若声明 endpoints：另开媒体流（见 4.10），不走 control JSON
```


| 层        | 标识                     | 谁来定义     | 作用                           |
| -------- | ---------------------- | -------- | ---------------------------- |
| 发布 / 发现  | `protocol = bmahs/1.0` | 本文件      | 设备发布自己、下线；智能体找到设备、得到连接方式     |
| 业务使用     | `service`，如 `light/1`  | 该硬件类型规范  | 连上之后发什么命令                    |
| 操作界面（可选） | 声明 `ui`                | 本文件 4.9  | 声明了就必须实现二进制 UI 流；未声明则智能体不得开流 |
| 端点媒体（可选） | 声明 `endpoints`         | 本文件 4.10 | 声明了就必须提供端点表与开流；未声明则不得猜测媒体通道  |


新硬件**不要**改发现地址与组播端口，只需：

- 选一个 `type`（设备种类，小写英文，如 `light`、`display`、`lock`、`phone`）
- 定义一个 `service`（`种类/主版本`，如 `light/1`）
- 提供完整 **操作清单**（`operations`）与 **安全边界**（`security`）
- `announce` 里用短字段 `capabilities` / `security` 做摘要
- 按第 5、6、7 节发布并提供 `control`
- 有可操作画面时：在 `capabilities` 中加入剖面标签 `ui`（有触控再加 `tap` / `swipe`），在 `operations` 中提供 `ui.start` / `ui.stop`，给出真实逻辑分辨率，并实现第 4.9 节 UI 流
- 可产/可消费媒体供设备间转发时：在 `hello` 声明 `endpoints`，并实现第 4.10 节开流

---



## 3. 传输

本版覆盖**无线和有线**，发现与控制都走 **IP**。物理怎么连由设备自己选；连上之后必须能拿到地址，并按 3.1 / 3.2 发布、按第 6 节提供 `tcp://`（或 `tcp://[IPv6]:`）的 `control`。设备取得地址后应同时启用下面两条通道。只实现 UDP 也可被智能体发现；同时实现 Bonjour 可被系统工具与 OS 原生浏览。

### 3.1 UDP 组播（必须）


| 项               | IPv4                             | IPv6                             |
| --------------- | -------------------------------- | -------------------------------- |
| 组播地址            | `239.255.42.42`                  | `ff02::4242`（链路本地，scope `ff02`）  |
| 端口              | `5354`                           | 同左                               |
| 协议              | UDP                              | UDP                              |
| TTL / Hop Limit | `2`                              | `2`（`IPV6_MULTICAST_HOPS`）       |
| 加入组播            | `IP_ADD_MEMBERSHIP`，本机 `0.0.0.0` | `IPV6_JOIN_GROUP`；建议指定网卡 ifindex |
| 绑定              | `0.0.0.0:5354`                   | `[::]:5354`，且 `IPV6_V6ONLY=1`（UDP 组播接收须为 1，**与 §6.1 的 TCP 双栈监听取值不同，勿混淆**） |


共同规则：


| 项    | 值                                   |
| ---- | ----------------------------------- |
| 编码   | 一个数据报 = 一个 UTF-8 JSON 对象            |
| 最大长度 | **1400 字节**（含 JSON，避免分片）            |
| 套接字  | `SO_REUSEADDR`；支持时再开 `SO_REUSEPORT` |


设备必须同时加入 IPv4 组。有 IPv6 的设备**应当**再加入 IPv6 组；无 IPv6 栈时跳过即可，不得因此退出。智能体发现时应对两个组各发一次 `query`。

设备需要**发送 + 接收**两只逻辑套接字（每个地址族各一对亦可）：

- **发送**：向 `239.255.42.42:5354` 与（若启用）`[ff02::4242]:5354` 发送 `announce` / `goodbye`
- **接收**：绑定并加入对应组播组，接收 `query`



### 3.2 Bonjour / DNS-SD（应当）


| 项    | 值                               |
| ---- | ------------------------------- |
| 服务类型 | `_bmahs._tcp`                   |
| 域    | `local.`                        |
| 实例名  | 与 `name` 相同，建议可打印 ASCII / UTF-8 |
| 端口   | 与 `control` 中的 TCP 端口相同         |
| 主机名  | `{id}.local.`（`id` 见 4.3）       |
| 地址记录 | 同时发布 A（IPv4）与 AAAA（IPv6），有则发    |


TXT 记录必须与 UDP `announce` 字段对齐，见第 6.4 节。

地址或 TXT 摘要变化时，必须按第 3.3 节**重新注册**服务（更新 A / AAAA 与 TXT），不得长期保留过期地址。

### 3.3 地址变化时刷新注册（必须）

设备在线期间，本机用于对外服务的局域网地址可能因 DHCP、漫游、换网、接口切换等原因改变。若发现层仍广播旧 `ip` / `ipv6` / `control`，智能体将出现 `connect failed` / 超时，设备等于「看得见、连不上」。

**必须**遵守：


| 要求       | 说明                                                                                                                   |
| -------- | -------------------------------------------------------------------------------------------------------------------- |
| 监视       | 持续获知本机当前局域网 IPv4 / IPv6（建议至少每数秒检测一次，或订阅系统链路 / 地址变更事件）                                                                |
| 判定       | 对外发布用的 `ip`、`ipv6`，或由此生成的 `control` 主机部分，任一相对上次成功发布发生变化，即视为需要刷新                                                      |
| UDP      | 立刻按第 5.1 节再发 `announce`（字段必须写入**新**地址与**新** `control`）；此后稳态周期广播亦使用新值                                                 |
| Bonjour  | 若已实现第 3.2 节：必须更新或重新注册 `_bmahs._tcp`，使 A / AAAA 与 TXT 中的 `ip` / `ipv6` / 相关摘要与当前 `announce` **一致**；禁止长期保留过期 TXT `ip=` |
| `id`     | **不得**因换 IP 而更换 `id`；换址不是新设备                                                                                         |
| `hello`  | 新 TCP 连接上的 `hello` / `describe` 中的 `control`（及地址相关字段）必须与当前发布一致                                                       |
| 虚拟 / 多端口 | 同一进程对外发布的多个 `control`（如网关发布的虚拟设备）必须一并更新其 advertise 地址                                                                |


**不要求**：地址变化时发送 `goodbye`（除非设备即将真正离线）；也不要求作废未过期的占用 `token`——占用关系跟 `id` 与进程，不跟 IP。智能体侧若连接失败，应重新发现并使用最新 `control`。

---



## 4. 公共编码规则



### 4.1 JSON

- 报文必须是 JSON 对象，编码为 UTF-8，且不得包含 BOM。
- 字段名必须全部小写，且区分大小写。
- 接收方必须忽略未知字段，以保证协议可扩展。
- 接收方必须丢弃满足下列任一条件的报文：不是 JSON 对象、`kind` 非法、`version` 不是 `1`、`protocol` 不是 `bmahs`*。



### 4.2 公共头（所有 UDP 报文）


| 字段          | 类型     | 必须  | 说明                                                          |
| ----------- | ------ | --- | ----------------------------------------------------------- |
| `version`   | number | 是   | 协议主版本，当前固定 `1`                                              |
| `protocol`  | string | 是   | 现行写 `bmahs/1.0` |
| `kind`      | string | 是   | `announce` / `query` / `goodbye`                            |
| `timestamp` | number | 是   | Unix 秒（整数即可）                                                |
| `id`        | string | 是   | 稳定设备（或客户端）标识                                                |




### 4.3 `id` 规则

- 设备 ID 在同一台设备的生命周期内必须保持不变。
- 设备 ID 仅允许使用 `A-Z`、`a-z`、`0-9` 和 `-`；推荐全部使用小写。
- 由显示名生成设备 ID 时：
  - 将不允许的字符替换为 `-`；
  - 去掉首尾的 `-`；
  - 若结果为空，则使用 `bmahs-device`。
- 实现建议：设备 ID 可写入出厂配置或 Flash。
- 示例：`ACME-Lamp-00A1` → `acme-lamp-00a1`。



### 4.4 `type` / `service` / `want` / `control` / `capabilities` / `security` / `port`


| 字段             | 规则                                                                                                                                                                 |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `type`         | 小写英文单词，表示硬件类型。新硬件类型由厂商自报，智能体按字符串匹配。示例：`light`、`display`、`lock`。                                                                                                    |
| `service`      | `{type}/{主版本}`，如 `light/1`。同一 `type` 可演进为 `light/2`。                                                                                                               |
| `want`         | 仅 `query`。`*` 或 `bmahs` 或空 = 全部设备；否则为逗号分隔的 `type` 列表，如 `light`、`light,display`。匹配时大小写不敏感。                                                                          |
| `control`      | URI。IPv4 写 `tcp://<ipv4>:<port>`；IPv6 必须加方括号：`tcp://[<ipv6>]:<port>`（RFC 3986）。同时有双栈时，`control` **优先写 IPv4**。其它 scheme（`ws://`、`http://`）可发，但须在对应 `service` 文档中说明。 |
| `capabilities` | **能力标签数组**：可含动作名与**剖面标签**（`ui` / `tap` / `swipe`）。剖面标签只作声明用，不是可调用动作；可调用面以 `operations` 为准。UDP / TXT 只用这个短列表。                                                                                                                 |
| `security`     | 安全边界摘要：`{"scope":"lan","auth":"none"}`。**完整安全边界见 §4.5「安全边界 `security`」**。                                                                                    |
| `port`         | 1–65535 的整数，与 `control` 中的端口一致。                                                                                                                                    |


`query` 匹配算法（设备侧）：

1. 将 `want` 按逗号拆开、去空白、转小写。
2. 若列表为空，或含 `*` / `bmahs`，则应答。
3. 否则仅当本机 `type` 出现在列表中时应答。



### 4.5 设备必须声明：自然语言自述、操作清单、安全边界

智能体把硬件当工具用，必须先用**自然语言**理解「这是什么、能做什么、不能越什么界」，再按机器字段调用。所有 BMAHS 设备都必须提供这三份描述。UDP `announce` 只带摘要（1400 字节限制）；完整内容放在 TCP `hello`，并用 `describe` 再取一次。

#### 自然语言自述（必须）

大模型按**语义**选设备与填参数，稳定机器码（`id` / `type` / `service` / `action` / `code`）保持英文标识；**凡供选型、决策、填参、读结果的说明文字，必须是完整自然语言句子**（建议与用户界面同一语言；本文件示例用简体中文）。


| 字段                               | 位置                      | 必须  | 写作要求                                                                    |
| -------------------------------- | ----------------------- | --- | ----------------------------------------------------------------------- |
| `name`                           | announce / hello / TXT  | 是   | 用户可读显示名，如「客厅灯」「书房手机」。**禁止**只写型号码、SKU、或纯英文内部代号而无语义                       |
| `summary`                        | announce（应当）/ hello（必须） | 见左  | **一两句**自述：我是什么设备、主要能帮智能体做什么、使用前要注意什么（如须占用）。UDP 建议 ≤ 120 字，整包仍 ≤ 1400 字节 |
| `hint`                           | hello                   | 应当  | 给模型的使用提示，如「先 occupy，再 brightness；任务结束必须 release」                        |
| `operations[].description`              | hello / describe        | 是   | 完整一句：何时用、做什么；禁止空串或仅重复 `name`                                            |
| `operations[].result`            | hello / describe        | 是   | 成功后模型应读哪些结果，用自然语言写                                                      |
| `args[].description` / `returns[].description` | hello / describe        | 是   | 含义、怎么填、单位、与兄弟参数关系；枚举须在 `description` 或 `enum` 中说清可选值含义                         |
| `security.notes`                 | hello / describe        | 是   | 边界自然语言：允许什么、禁止什么、是否要用户确认                                                |
| `error`                          | 失败响应                    | 是   | 给人 / 模型看的失败原因短句；**不得**只回与 `code` 相同的无信息词（可与 `code` 同词，但应尽量说明「为什么」）      |
| `endpoints[].description`               | 有端点时                    | 是   | 该端点产/消费什么、给谁用（见 4.10）                                                   |


规则：

1. **发布即自述**：`announce` 的 `name` + `summary` 应让智能体在未连 `control` 前就能判断「要不要连这台」。
2. **机器码 ≠ 说明**：不得用 `type=light` 代替 `summary`；不得用 `action=on` 代替 `operations[].description`。
3. **可执行**：说明里写清前置条件（如「须先 occupy 并携带 token」）、互斥参数、常见失败（已被占用等）。
4. **勿塞密钥**：自然语言字段同样禁止写入密码、`token`、账号。
5. **语言稳定**：同一设备生命周期内 `summary` / 主要 `description` 不要频繁改写到改变语义；能力变化应升 `service` 或更新 `operations` 并立刻再 `announce`。

`announce` 自述示例（字段摘录）：

```json
{
  "name": "客厅灯",
  "summary": "客厅吸顶灯，可开关和调节亮度。占用后才能改亮度；任务结束请 release。",
  "type": "light",
  "service": "light/1"
}
```

`hello` 还应带更完整的 `summary`（可与 announce 相同或稍详）与 `hint`：

```json
{
  "name": "客厅灯",
  "summary": "这是客厅的吸顶灯。智能体可开关灯光、把亮度设到 0–100%。控制前请 occupy，结束后必须 release。",
  "hint": "推荐流程：occupy → on/brightness → release。亮度参数名是 level，单位 %。"
}
```

Bonjour TXT：应当含短 `name`；`summary` 若放入 TXT 须截断（建议 ≤ 80 字），完整语义以 TCP `hello` 为准。

#### 操作清单 `operations`（必须）

智能体可使用的全部动作。未出现在清单中的 `action`，设备必须拒绝（`code=unknown-action`）**。** 智能体运行时必须把 `operations` 转成工具 schema：工具说明来自 `description`，参数定义只来自 `args`，返回说明只来自 `result` / `returns`。


| 字段        | 类型       | 必须  | 说明                                              |
| --------- | -------- | --- | ----------------------------------------------- |
| `name`    | string   | 是   | 动作名，小写，与请求里的 `action` 相同（机器码）                   |
| `description`    | string   | 是   | **自然语言**：这个动作做什么、何时用；禁止空串或仅重复 `name`            |
| `args`    | object[] | 是   | 参数对象列表；无参必须为 `[]`，禁止再写成字符串数组                    |
| `any_of`  | string[] | 可以  | 这些参数名至少提供一个（互斥或二选一）。都未提供则 `code=bad-arg`        |
| `result`  | string   | 是   | **自然语言**：成功时模型应如何理解返回，例如「成功时灯亮，并回 state=on」     |
| `returns` | object[] | 是   | 成功响应里会出现的字段；无额外字段则为 `[]`（仍有公共的 `ok` / `action`） |


新设备必须发对象数组。若对端仍把 `args` / `returns` 写成字符串数组（仅名字），智能体仅作兼容：按 `type=string`、`required=false`、`description=""` 理解，并应再发 `describe` 要求完整清单。

`args[]` 与 `returns[]` 每一项同一套字段：


| 字段            | 类型          | 必须          | 说明                                                                                         |
| ------------- | ----------- | ----------- | ------------------------------------------------------------------------------------------ |
| `name`        | string      | 是           | 请求或响应 JSON 中的字段名                                                                           |
| `type`        | string      | 是           | `string` / `int` / `number` / `bool`。`returns` 若是嵌套 JSON，可用 `object` / `array`，细节写在 `description` |
| `required`    | bool        | 是（仅 `args`） | 缺省不得省略；无参列表则为 `[]`。`returns` 不写 `required`                                                 |
| `description`        | string      | 是           | **自然语言**：说明含义、怎么填、单位、和其它参数的关系；禁止空串等                                                        |
| `min` / `max` | number      | 数值应当        | 闭区间。有上下限就必须写，例如音量 / 亮度 0–100                                                               |
| `enum`        | string[]    | 枚举应当        | 允许取值；有固定集合就必须写；取值含义可在同行 `description` 用自然语言补充                                                     |
| `unit`        | string      | 可以          | 如 `%`、`K`、`s`、`Hz`                                                                         |
| `example`     | 与 `type` 一致 | 应当          | 一个合法示例，供模型直接套用                                                                             |
| `default`     | 与 `type` 一致 | 可以          | 省略时设备使用的默认值                                                                                |


请求里出现清单未声明的参数名，设备应当忽略或回 `bad-arg`。声明为 `required` 却缺失、类型不符、越出 `min`/`max`、不在 `enum` 中、或不满足 `any_of`：必须 `ok=false`，`code=bad-arg`。

```json
{
  "operations": [
    {
      "name": "on",
      "description": "打开这盏灯，让房间亮起来；进入房间或需要照明时使用",
      "args": [],
      "result": "成功时灯亮，回 state=on",
      "returns": [
        {"name": "state", "type": "string", "enum": ["on", "off"], "description": "灯是否亮着", "example": "on"}
      ]
    },
    {
      "name": "brightness",
      "description": "把灯的亮度调到指定百分比；看电影等需要调暗时使用，须先 occupy 并带 token",
      "args": [
        {
          "name": "level",
          "type": "int",
          "required": true,
          "min": 0,
          "max": 100,
          "unit": "%",
          "example": 40,
          "description": "亮度，0 最暗、100 最亮"
        }
      ],
      "result": "成功时回开关状态与当前亮度",
      "returns": [
        {"name": "state", "type": "string", "enum": ["on", "off"], "description": "灯是否亮着", "example": "on"},
        {"name": "level", "type": "int", "min": 0, "max": 100, "unit": "%", "description": "当前亮度", "example": 40}
      ]
    },
    {
      "name": "scene",
      "description": "按名称或序号切换场景，二者至少提供一个",
      "args": [
        {"name": "name", "type": "string", "required": false, "example": "reading", "description": "场景名；与 index 二选一"},
        {"name": "index", "type": "int", "required": false, "min": 0, "example": 0, "description": "场景序号，从 0 起；与 name 二选一"}
      ],
      "any_of": ["name", "index"],
      "result": "成功时回当前场景名",
      "returns": [
        {"name": "name", "type": "string", "description": "当前场景", "example": "reading"}
      ]
    }
  ]
}
```

智能体把 `operations` 转成工具时建议如下对应（不得另编参数）：


| `operations`                                                                  | 工具 schema         |
| ----------------------------------------------------------------------------- | ----------------- |
| `name`                                                                        | 函数 / tool 名       |
| `description`                                                                        | 工具说明              |
| `args[].name` + `type` / `min` / `max` / `enum` / `description` / `example` / `unit` | 参数属性              |
| `args` 中 `required=true` 的 `name`                                             | 必填参数列表            |
| `any_of`                                                                      | 写入工具说明，并在发请求前检查   |
| `result` + `returns`                                                          | 不作为入参；调用后按这些字段读响应 |


`announce.capabilities` / Bonjour `capabilities` 仍只是短名列表，例如 `["on","off","brightness","describe"]`。完整 schema 只出现在 TCP `hello` / `describe`。

全硬件类型必须在 `operations` 中声明并实现六个**通用动作**：`describe`、`info`、`register`、`occupy`、`release`、`who`。这六个动作同样必须带齐 `args` / `result` / `returns`，且**必须按下表定义**——厂商不得自行增删字段名或改变语义，否则智能体无法编写通用逻辑。

##### 通用动作的标准定义（必须）

| 动作 | 作用 | `args`（公共 `agent` / `token` 除外） | 成功响应的 `returns`（公共 `ok` / `action` 除外） |
| --- | --- | --- | --- |
| `describe` | **权威来源**：回完整操作清单与安全边界 | `[]` | `operations`、`security`、`id`、`type`、`service`、`name`、`summary`、`hint`、`state` |
| `info` | **精简**描述：这台是什么、现在什么状态 | `[]` | `id`、`type`、`service`、`name`、`summary`、`model`、`state` |
| `who` | **只看占用关系**：现在谁在用、用到什么时候 | `[]` | `id`、`name`、`type`、`service`、`state`、`holder`、`until`、`lease_security` |
| `register` | **幂等登记刷新**：回送当前公开状态快照（**字段同 `who`**） | `[]` | `id`、`name`、`type`、`service`、`state`、`holder`、`until`、`lease_security`（**同 `who`**） |
| `occupy` | 取得独占权并签发 `token` | `ttl`（可选，10–9999，见 4.6） | `state`、`event`、`busy`、`holder`、`until`、`lease_security`、`token` |
| `release` | 归还独占权、作废 `token` | `[]` | `state`（`registered`）、`event`（`release`） |

四个只读动作的语义边界（**必须**区分，不得互相替代）：

| 动作 | 与其它动作的差异 | 需要 `token` | 改变占用关系 |
| --- | --- | --- | --- |
| `describe` | 唯一保证返回**完整** `operations` / `security` 的动作 | 否 | 否 |
| `info` | **可以**不含完整 `operations` / `security`（厂商取舍，但身份字段必须齐） | 否 | 否 |
| `who` | **不返回** `operations` / `security`，只回占用关系；`holder` 缺省表示当前无人占用 | 否 | 否 |
| `register` | **不签发** `token`；设备自身的上线登记走同一语义（见 4.6 规则 1），智能体调用它仅作登记刷新 | 否 | 否 |

`register` 的双重语义（**必须**按此实现）：

1. **设备侧**：设备入网并开始监听后，由设备**自行**进入 `registered` 并发布 `announce`——这是设备内部行为，不需要任何智能体发起。
2. **智能体侧**：智能体**可以**调用 `register` 作为幂等刷新——设备收到后重新计算并回送当前公开状态快照（**字段与 `who` 完全一致**：`id` / `name` / `type` / `service` / `state` / `holder` / `until` / `lease_security`），**不得**因此签发 `token` 或改变占用关系。设备已被他人 `managed` 时，回 `code=occupied` 并带 `holder` / `until`。
3. **与 `who` 的关系**：两者**返回字段完全一致**，差别只在语义——`who` 是**查询**（回答「现在谁在用」），`register` 是**登记刷新**（要求设备重新确认并回送自己的公开状态）。设备实现**可以**共用同一段序列化代码，但 `operations` 中两者都要声明、都要可调用。

六个通用动作的 `operations` 条目示例（**照此形状声明**，业务动作同法追加）：

```json
{
  "operations": [
    {"name": "describe",
     "description": "查询本设备的完整操作清单、安全边界与自然语言自述；需要权威参数定义时用它",
     "args": [], "result": "成功时回完整 operations、security 与全部身份字段（含 hint / state），不含 token",
     "returns": [
       {"name": "operations", "type": "array", "description": "完整操作清单"},
       {"name": "security", "type": "object", "description": "完整安全边界"},
       {"name": "summary", "type": "string", "description": "设备自然语言自述"},
       {"name": "hint", "type": "string", "description": "使用提示，如推荐调用顺序"},
       {"name": "state", "type": "string", "enum": ["registered", "managed"], "description": "设备当前状态"}
     ]},
    {"name": "info",
     "description": "查询本设备的精简描述与当前状态；只想快速知道这是什么设备时用它，不要用它取参数定义",
     "args": [], "result": "成功时回身份字段与 state",
     "returns": [
       {"name": "state", "type": "string", "enum": ["registered", "managed"], "description": "设备当前状态"},
       {"name": "summary", "type": "string", "description": "设备自然语言自述"}
     ]},
    {"name": "who",
     "description": "查询当前是谁占用了本设备、租约到什么时候；用于被拒绝时判断该等待还是换设备",
     "args": [], "result": "成功时回占用关系，不含 token",
     "returns": [
       {"name": "state", "type": "string", "enum": ["registered", "managed"], "description": "设备当前状态"},
       {"name": "holder", "type": "string", "description": "占用方的 agent；未受管时为空"},
       {"name": "until", "type": "int", "description": "租约到期 Unix 秒；无限期时为 0"},
       {"name": "lease_security", "type": "int", "description": "本次租约秒数；9999 = 无限期"}
     ]},
    {"name": "register",
     "description": "登记刷新本设备，回送当前公开状态快照（字段同 who）；不改变占用关系、不签发 token",
     "args": [], "result": "成功时回送当前公开状态快照，字段与 who 完全一致，不含 token",
     "returns": [
       {"name": "state", "type": "string", "enum": ["registered", "managed"], "description": "设备当前状态"},
       {"name": "holder", "type": "string", "description": "占用方的 agent；未受管时为空"},
       {"name": "until", "type": "int", "description": "租约到期 Unix 秒；无限期时为 0"},
       {"name": "lease_security", "type": "int", "description": "本次租约秒数；9999 = 无限期"}
     ]},
    {"name": "occupy",
     "description": "独占本设备并取得 token；成功后其它智能体的控制请求会被拒绝，直到 release 或租约到期",
     "args": [
       {"name": "ttl", "type": "int", "required": false, "min": 10, "max": 9999, "unit": "s", "example": 120,
        "description": "期望租约秒数；省略则用设备默认 60 秒；9999 表示无限期，不会自动释放"}
     ],
     "result": "成功时回 token、holder 与 until；此后控制命令必须带该 token",
     "returns": [
       {"name": "holder", "type": "string", "description": "占用方 agent"},
       {"name": "until", "type": "int", "description": "租约到期 Unix 秒；无限期时为 0"},
       {"name": "lease_security", "type": "int", "description": "本次租约秒数；9999 = 无限期"},
       {"name": "token", "type": "string", "description": "占用令牌，仅出现在本次 TCP 响应中"}
     ]},
    {"name": "release",
     "description": "主动释放独占权并作废 token；任务结束必须调用，不要依赖租约到期",
     "args": [], "result": "成功时 state 回到 registered，event=release",
     "returns": [
       {"name": "state", "type": "string", "enum": ["registered"], "description": "释放后回到已注册"},
       {"name": "event", "type": "string", "enum": ["release"], "description": "状态变迁为释放"}
     ]}
  ]
}
```

`announce.capabilities`（及 TXT 的 `capabilities`）**应当**包含这六个动作名，便于智能体在未连 TCP 前就知道设备支持通用动作。

#### 安全边界 `security`（必须）

设备承诺的能力上限。智能体不得请求 `deny` 中的能力；设备收到越界请求必须拒绝并回 `code=denied`。


| 字段        | 类型       | 必须  | 说明                                                 |
| --------- | -------- | --- | -------------------------------------------------- |
| `scope`   | string   | 是   | 网络范围：`lan`（仅局域网）/ `local`（仅本机）/ `wan`（若开放须在文档说明）   |
| `auth`    | string   | 是   | 鉴权：`none` / `token` / `tls` 等。本版占用令牌见 4.6，不代替 TLS     |
| `allow`   | string[] | 应当  | 明确允许的能力标签                                          |
| `deny`    | string[] | 是   | 明确禁止的能力。建议至少覆盖：任意读盘、任意写盘、执行命令、联网拉取、云上传、摄像头 / 麦克风偷采 |
| `confirm` | string[] | 可以  | 需要用户确认后才执行的动作名；确认通路、超时与失败语义见本节「用户确认」            |
| `confirm_timeout` | int | 可以  | 需确认动作的**最长等待秒数**，范围 **5–300**，缺省 **30**；**应当**在 `hello.security` / `describe.security` 中公告实际取值，供智能体设置自身超时 |
| `notes`   | string   | 是   | **自然语言**边界说明：允许什么、禁止什么、是否要确认；禁止空串                  |


```json
{
  "security": {
    "scope": "lan",
    "auth": "token",
    "allow": ["local-light-control", "exclusive-occupy"],
    "deny": ["write-filesystem", "exec", "network-fetch", "cloud-upload", "camera-mic-capture"],
    "confirm": [],
    "notes": "只控制本灯的开关与亮度，不能读盘、执行命令或上传云端。控制命令须带 occupy 返回的 token。"
  }
}
```

`announce.security` / Bonjour `security` 只带摘要：`{"scope":"lan","auth":"token"}`，TXT 写成 `security=lan,token`（键名与 UDP 字段一致，见 §6.4）。UDP / TXT **不得**带占用令牌本身。

通用动作 `describe`：返回**完整** `operations` + `security` + **全部身份字段**（`id` / `type` / `service` / `name` / `summary` / `hint` / `state`）。不得返回 `token`。四个只读动作的完整定义见上文「通用动作的标准定义」。

#### 用户确认（`security.confirm`，可选）

`security.confirm` 列出的动作，设备**必须**在本次执行前取得**本地**用户确认：

1. **确认通道由设备本地提供**（物理按键、设备屏幕弹窗、厂商官方 App 等）。本协议**不**规定其形态，也**不**新增控制动作。
2. 设备收到需确认的动作后**最多等待 `confirm_timeout`**（取值与公告方式见 §4.5「安全边界 `security`」表；缺省 **30 秒**，可配置 **5–300 秒**），等待期间**不回复**该请求。
3. 在 `confirm_timeout` 内完成本地确认 → 正常执行，按成功响应返回；超时或用户否决 → `ok=false`、`code=denied`、`retryable=true`，`error` 须说明「需要用户在设备上确认，未在 N 秒内完成」或「用户已拒绝」。
4. 设备**应当**在等待期间给出本地提示（提示音、指示灯、屏幕弹窗）。
5. 智能体**应当**在调用 `confirm` 内的动作前，先向用户说明将要做什么；**不得**因为用户未响应而高频重试。
6. 智能体**应当**按 `hello.security.confirm_timeout` / `describe.security.confirm_timeout`（缺省 **30 秒**）设置自身等待超时，且**不得短于**该值；未公告时按缺省 30 秒处理。

### 4.6 设备自我控制（必须）

设备在**本机进程内存**维护自己的生命周期，不写路由器、不写磁盘。状态变化后应立刻再发一次 `announce`（或 `goodbye`）。本机局域网地址变化时按第 3.3 节刷新注册（`id` / `token` 不变）。

```
          register                 occupy / 控制命令
  offline ────────► registered ──────────────────► managed
     ▲                  ▲                              │
     │     offline      │  release / 租约到期            │
     └──────────────────┴──────────────────────────────┘
```


| 状态           | 中文  | 含义                   |
| ------------ | --- | -------------------- |
| `offline`    | 离线  | 未入网或即将退出；发 `goodbye` |
| `registered` | 已注册 | 在线空闲，可被发现、可被受管       |
| `managed`    | 受管  | 已被一个智能体独占控制          |


`release`（释放）是**动作**，不是常驻状态：成功后 `state` 回到 `registered`，`event=release`。


| 字段               | 位置                                      | 说明                                                   |
| ---------------- | --------------------------------------- | ---------------------------------------------------- |
| `state`          | announce / hello / TXT / goodbye        | `offline` / `registered` / `managed`                 |
| `event`          | 应当                                      | 最近一次变迁：`register` / `manage` / `release` / `offline` |
| `busy`           | 兼容字段                                    | 等价于 `state=managed`                                  |
| `holder`         | 受管时必须（公开）                               | 占用方的 `agent`                                         |
| `until`          | 受管时应当（公开）                               | 租约到期 Unix 秒；**无限期时为** `0`（表示无到期）                     |
| `lease_security` | TCP 应当                                  | 本次租约时长（秒）。设备默认 **60**；`9999` **= 无限期**。不要与 `token` 混淆            |
| `ttl`            | occupy 请求可选                             | 期望租约秒数；合法范围 **10–9999**；省略则用设备默认（60）                 |
| `agent`          | `occupy` 与所有控制请求必须；只读动作应当 | 智能体稳定标识（机器码，建议 `[a-z0-9-]`，如 `alice`）；仅兼容 `bmahs/1` 老客户端时按 `anonymous` 兜底。只读动作（`describe` / `info` / `who` / `register`）**应当**携带 `agent`，但设备**不得**因其缺失而拒绝 |
| `token`          | **仅 TCP** occupy 成功与占用方后续控制 / `release` | 设备签发的不透明占用令牌                                         |



| 项          | 值                                                                                                               |
| ---------- | --------------------------------------------------------------------------------------------------------------- |
| 默认租约       | **60 秒**：未带 `ttl` 的 `occupy`，`until = now + 60`，到期自动回到 `registered`（`event=release`）                                                                                       |
| `ttl` 合法范围 | **10–9999**（闭区间）；`9999` = 无限期；越界回 `bad-arg`                                                                     |
| 有限租约       | `10–9998`：`until = now + lease_security`，到期自动回到 `registered`                                                    |
| 无限期        | `lease_security=9999`：`until=0`；**不得**按时间自动到期；须显式 `release`                                                          |
| 刷新         | 有限租约时，占用方后续**带 token** 的命令刷新 `until`；无限期保持 `until=0`                                                            |
| 卡住恢复       | 有限租约（含默认 60 s）：等 `until` 到期自动回 `registered`，**无需**人工干预。仅 **显式 `ttl=9999`** 且占用方丢失 `token` 时，才需**重启 BMAHS 设备**（进程退出发 `goodbye` 后重新上线为 `registered`），再重新 `occupy` / `release` |




#### 占用令牌 `token`

`agent` 只是标识字符串，可被伪造。`token` 用于证明「我就是当前占用方」。

1. 设备在 `occupy` 成功（含已注册时空闲设备上的第一条控制命令自动受管）时签发新 `token`（建议 ≥ 16 个十六进制字符的随机串）。
2. 占用方此后的控制命令与 `release` **必须**带同一 `token`。缺失或不匹配：`code=unauthorized`。
3. 同一 `holder` 再次 `occupy` 且 `token` 正确：刷新租约，返回同一 `token`。
4. `release` 成功或租约到期后，旧 `token` 立即作废。
5. **禁止**把 `token` 写入 UDP `announce` / `query` / `goodbye`、Bonjour TXT、TCP `hello`、`who`、`describe`。其它智能体只能看到 `state` / `holder` / `until`。

`token` 校验与并发连接（**必须**遵守）：

- **必须**校验 `token` 本身。`token` **与签发时的 `agent` 绑定**：设备**应当**同时校验请求里的 `agent` 与签发值一致；`token` 缺失、不匹配，或 `agent` 与签发值不一致，一律回 `code=unauthorized`。
- 设备**必须**至少支持 **2 路**并发 TCP 控制连接（占用方的控制 + 其它方的只读查询），并共享同一份占用状态。只读动作（`describe` / `info` / `who` / `register`）在任何连接上均可调用；控制动作按规则 4 校验占用。
- 若硬件受限只能开 1 路，则**必须**在接受新连接时**优先**保证只读查询可用，并在 `security.notes` 中写明限制。

规则：

1. 设备入网并开始监听后必须 `register`，`announce.state=registered`。
2. `describe` / `info` / `who` / `register` 为只读或登记刷新，不改变受管关系，也不返回 `token`；各自的标准 `args` / `returns` 见 §4.5「通用动作的标准定义」。
3. 已注册时，`occupy` 或第一条控制命令进入受管，默认租约 **60s**（可用 `ttl` 覆盖，须在 10–9999）；有限租约下占用方后续**带 token** 的命令刷新 `until`。
4. 受管期间，其它 `agent` 的控制必须 `ok=false`，`code=occupied`，并带回 `state` / `holder` / `until`（仍不含 `token`）。
5. 占用方带 `token` 的 `release`，或**有限**租约到期（`event=release`），回到已注册。无限期（`lease_security=9999`）**不会**因时间到期。
6. 进程退出必须先标 `offline`，再发两次 `goodbye`。重启后内存中的占用状态清空，重新 `register` 为 `registered`，其它智能体可再 `occupy`。
7. 所有硬件产品必须实现六个通用动作：`describe` / `info` / `register` / `occupy` / `release` / `who`（与 §4.5 一致）。
8. **未释放卡住**：默认 60 秒有限租约下，占用方崩溃后**至多 60 秒**即自动回到 `registered`，一般无需人工干预。**仅显式 `ttl=9999`（无限期）**时，若占用方未 `release` 且丢失 `token` / 进程已死，其它智能体将一直收到 `occupied`；本版**不**提供管理员强行 release，恢复办法是**重启该 BMAHS 设备**，待其重新发布后再 `occupy` / `release`。

```json
{"action":"occupy","agent":"alice"}
{"ok":true,"action":"occupy","state":"managed","event":"manage","busy":true,"holder":"alice","until":1712345738,"lease_security":60,"token":"a1b2c3d4e5f60708"}
{"action":"occupy","agent":"alice","ttl":120}
{"ok":true,"action":"occupy","state":"managed","event":"manage","busy":true,"holder":"alice","until":1712345798,"lease_security":120,"token":"a1b2c3d4e5f60708"}
{"action":"occupy","agent":"alice","ttl":9999}
{"ok":true,"action":"occupy","state":"managed","event":"manage","busy":true,"holder":"alice","until":0,"lease_security":9999,"token":"a1b2c3d4e5f60708"}
{"ok":false,"action":"brightness","code":"occupied","error":"客厅灯正被 alice 占用，租约约 42 秒后到期；也可等它自动释放后再试","retryable":true,"state":"managed","holder":"alice","until":1712345738}
{"action":"release","agent":"alice","token":"a1b2c3d4e5f60708"}
{"ok":true,"action":"release","state":"registered","event":"release"}
```



#### 存活检测与脱网（必须）

本版**不**另设独立心跳报文。发现层用周期 `announce` 充当存活信号：


| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| 稳态 `announce` 间隔 | **5 秒** | 设备心跳周期（**硬性下限**，不得调小） |
| 无心跳删除时限 | **60 秒** | = `clamp(12 × announce 间隔, 60 秒, 30 分钟)`；默认 5 s → **60 秒** |
| 删除时限上限 | **30 分钟**（1800 s） | 低功耗设备把 `announce` 间隔调大时的封顶值 |


**可配置性与边界（必须遵守）**：

1. **出厂默认必须是 `announce` 间隔 5 秒**；只允许**调大**（不得小于 5 秒），且**必须**在产品文档中写明，并在 `announce` / `hello` / TXT 的 `hb` 字段中**公告实际取值**（缺省视为 5）。
2. 智能体的**无心跳删除时限 = 12 × 该设备公告的 `hb` 间隔**，且**不小于 60 秒、不大于 30 分钟**；无 `hb` 或未知对端时按 5 秒（即 60 秒）处理。
3. 设备**不得**用高频组播代替约定周期；智能体也**不得**要求每台设备另开 TCP 心跳（见本节脱网规则 5）。


| 通道   | 机制                               | 超时 / 行为                                                                       |
| ---- | -------------------------------- | ----------------------------------------------------------------------------- |
| 发现层  | 稳态 `announce`（默认每 **5 秒**）       | 智能体：超过**无心跳删除时限**（默认 **60 秒**，见上表）未更新 → 从列表删除该 `id`，丢弃对应 `token`                              |
| 发现层  | 主动下线                             | 设备发两次 `goodbye`；智能体立刻删除                                                       |
| 控制层  | 有限占用租约 `until>0`                 | 到期 → `event=release`，回到 `registered`；旧 `token` 作废             |
| 控制层  | 无限期占用 `lease_security=9999`，`until=0` | **不**自动到期；须显式 `release`                                                       |
| 控制层  | TCP 断开                           | **不等于**设备离线，也**不等于**自动 `release`                                              |
| 运维恢复 | **仅显式 `ttl=9999`** 的占用方丢失 `token`，导致长期 `managed` | **重启 BMAHS 设备**：退出时 `goodbye`，再上线为 `registered`，然后重新 `occupy` / 用完再 `release`。默认 60 秒租约**不需要**此手段 |


智能体与设备脱网时：

1. **设备仍在线、智能体消失且未** `release`**（默认 60 秒租约）**：设备**至多**保持 `managed` 60 秒，`until` 到期后自动回到 `registered`，其它智能体即可占用，**无需**人工干预。占用方若仍在，应尽快重连并 `release`。
2. **无限期占用**（显式 `ttl=9999`，`until=0`）：**不会**自动到期。正确做法是原占用方重连并 `release`；若做不到（`token` 丢失、进程已死），只能**重启该 BMAHS 设备**后再发现、占用、释放。
3. **设备崩溃 / 断网且未发** `goodbye`：智能体靠 `announce` 无心跳超时（默认 60 秒）删除该 `id`；设备若自行恢复进程，占用内存状态已清空，视为重新上线（须重新 `occupy`）。
4. **换 IP**：按第 3.3 节刷新注册；`id` / `token` 不变；智能体连接失败时应重新发现并用新 `control`。
5. **不得**要求智能体对每个设备另开 TCP 心跳；也不得用高频组播代替约定的 `announce` 周期。
6. **不得**在协议层提供「任意智能体强夺占用」；强清占用的标准手段就是重启设备进程 / 电源。



### 4.7 错误信封（必须）

失败响应必须是一个 JSON 对象，至少含：


| 字段          | 类型     | 必须  | 说明                                  |
| ----------- | ------ | --- | ----------------------------------- |
| `ok`        | bool   | 是   | 固定 `false`                          |
| `action`    | string | 是   | 被拒绝的动作                              |
| `code`      | string | 是   | 稳定机器码，见下表                           |
| `error`     | string | 是   | **自然语言**失败原因；应尽量说明「为什么」，不得只回无信息的机器词 |
| `retryable` | bool   | 应当  | 智能体是否值得稍后重试同一动作                     |



| `code`           | `retryable` | 何时                               |
| ---------------- | ----------- | -------------------------------- |
| `occupied`       | true        | 已被其它智能体受管                        |
| `offline`        | true        | 设备已离线或即将退出                       |
| `busy`           | true        | 设备暂时无法执行（非占用）                    |
| `unauthorized`   | false       | `token` 缺失或不匹配                   |
| `bad-arg`        | false       | 参数缺失、类型或范围错误                     |
| `denied`         | false       | 触犯 `security.deny` 或未过 `confirm` |
| `unknown-action` | false       | `action` 不在 `operations` 中       |


成功响应至少含 `ok=true` 与 `action`。占用方控制成功时**应当**回显 `token`（便于一次性命令拿到令牌）。失败示例：

```json
{"ok":false,"action":"brightness","code":"occupied","error":"客厅灯正被 alice 占用，租约约 42 秒后到期；也可等它自动释放后再试","retryable":true,"state":"managed","holder":"alice","until":1712352878}
```



### 4.8 智能体义务（必须）

实现 BMAHS 智能体 / Agent 运行时必须遵守：

1. 读完 `hello` 后，只把 `operations` 登记为可使用工具；**必须把** `name` / `summary` / `hint` / `operations[].description` / `args[].description` / `result` / `returns[].description` / `security.notes` **交给模型**（可映射为工具 schema，不得剥掉自然语言只留动作名）。工具参数 schema **必须**来自 `args` 的 `type` / `required` / `enum` / `min`–`max` / `unit` / `example` / `description`。把 `security.deny` / `notes` / `confirm` 当作硬约束。不得只根据动作名猜参数或取值范围。
2. 发现列表选型优先用 `name` + `summary`，不要只甩 `id` / `type` 给模型。
3. 先看 `state`。若 `managed` 且 `holder` 不是自己，不要对控制动作重试轰炸。
4. 使用前 `occupy`（或接受空闲设备上第一条控制的自动受管），**保存**返回的 `token`。
5. 后续控制与 `release` 必须带该 `token` 与同一 `agent`。
6. 任务正常结束、失败、取消或进程退出时**必须** `release`，不得把设备留在 `managed`。**默认 60 秒有限租约**下，即使漏掉 `release`，设备也会在 `until` 到期后自动回到 `registered`（`event=release`），旧 `token` 立即作废，**无需**人工干预。**仅显式 `ttl=9999`（无限期）**时不会自动到期，其它智能体将一直收到 `occupied`，此时才需**重启该 BMAHS 设备**后才能重新占用（见 4.6 规则 8）。
7. 收到 `goodbye`，或超过**无心跳删除时限**（默认 **60 秒**，= 12 × 设备公告的 `hb` 间隔，上限 30 分钟）未更新的 `announce`，从列表中删除该 `id`，丢弃对应 `token`。
8. 未知 `service` 时只展示设备（`describe` / `info` / `who`），不要猜测未声明的 `action`。
9. 不得把 `token` 写入组播、日志聚合或离开本机的遥测。
10. 若 `capabilities` / `operations` 中**没有** `ui`，不得猜测画面通道、不得要求镜像或点击。若**有** `ui`，画面只按第 4.9 节的二进制 UI 流接收；不得用控制 JSON、base64 或其它未声明的媒体方式收帧。镜像上的 `tap` / `swipe` **必须**按**当前帧**的逻辑分辨率映射，不得沿用过期的 `ui.start` 宽高。
11. 若 `hello` **没有** `endpoints`，不得猜测媒体流或设备间转发端点；有则按第 4.10 节开流。



### 4.9 操作界面通道（可选，声明则必须实现）

本剖面适用于有可操作画面的设备（手机、平板、带屏锁、车机、示教器、触摸面板等）。无操作界面的设备（音箱、灯泡、纯传感器）**不声明** `ui`。

#### 4.9.1 声明义务

有可操作画面的设备**必须**：

- 在 `announce.capabilities` 中含剖面标签 `ui`（有触控时再加 `tap`、`swipe`）；剖面标签只作声明用，**不是**可调用动作
- 在 `hello.operations` 中含 `ui.start`、`ui.stop`（完整 `args` / `result` / `returns`）
- **给出当前可操作画面的逻辑分辨率**（`width` × `height`，像素），见 4.9.5；否则智能体无法正确镜像与点击
- 实现本节规定的**唯一**媒体方法（二进制 UI 流）

声明了 `ui` 却未实现 UI 流，或仍用控制 JSON / base64 传帧，视为违反协议；智能体应视为该设备界面不可用。

与第 4.5 节同一原则：

1. **有没有界面**：只看 `capabilities` / `operations` 里有没有 `ui`
2. **画面怎么收**：只认本节帧格式，不猜 WebRTC、HTTP MJPEG 或 JSON 内嵌图
3. **参数从哪来**：`fps`、`max_width`、`codec`、**屏幕逻辑宽高**、坐标范围只来自 `operations.args`、`ui.start` 的 `returns` 与**当前帧头**（须与 JPEG 像素一致）



#### 4.9.2 两通道，不得混用


| 通道     | 编码                            | 用途                                                                |
| ------ | ----------------------------- | ----------------------------------------------------------------- |
| 控制 TCP | 一行一个 UTF-8 JSON + `\n`（见 6.3） | `occupy`、`ui.start` / `ui.stop`、`screenshot`、`tap` / `swipe`、报错信封 |
| UI 流   | **非 JSON 二进制帧**               | 只传画面；先鉴权再出帧                                                       |


`ui` URI、帧数据、`token` **不得**出现在 UDP、Bonjour TXT、`hello`、`who`、`describe`。

#### 4.9.3 控制动作（JSON，必须 occupy + token）


| 动作           | 必须性 | 说明                                                                                  |
| ------------ | --- | ----------------------------------------------------------------------------------- |
| `ui.start`   | 必须  | 启动 UI 流；成功后才返回 `ui` URI 与**本次出流的实际** `width` / `height` / `codec` / `fps`（宽高不得用占位值） |
| `ui.stop`    | 必须  | 停止 UI 流，断开占用方，作废本次 URI                                                              |
| `screenshot` | 应当  | 只回元数据或指示「到 UI 流取下一帧」；**不得**在控制 JSON 里带回整图或 base64                                   |
| `tap`        | 可选  | `x`、`y` 必填；与当前帧同一坐标系                                                                |
| `swipe`      | 可选  | `x`、`y`、`x2`、`y2` 必填；可选 `duration`                                                  |


`ui.start` 请求示例：

```json
{"action":"ui.start","agent":"alice","token":"...","codec":"jpeg","fps":10,"max_width":720}
```

成功响应示例：

```json
{"ok":true,"action":"ui.start","state":"managed","width":720,"height":1600,"codec":"jpeg","fps":10,"ui":"tcp://192.168.1.8:9531","token":"..."}
```

`hello` 可提示本机支持 `ui`，但**不得**预先给出 UI 会话地址；地址只在 `ui.start` 成功后返回。`ui` URI 格式与 `control` 相同：`tcp://{ip}:{port}`；仅 IPv6 时用 `tcp://[{ipv6}]:{port}`。

#### 4.9.4 UI 流（非 JSON，声明 `ui` 则必须实现）

连接 `ui.start` 返回的 URI 后：

1. **鉴权**：客户端先发占用令牌：`[u8 token_len][token 字节]`（`token_len` 为 token 字符串 UTF-8 字节长度）。校验失败，设备立刻断开，不出帧。
2. **应答**：设备回 1 字节：`0x00` 表示成功；其它值表示失败并断开。
3. **出帧**：之后只发送二进制帧，格式固定：

```
[u32 BE payload_len][u16 BE width][u16 BE height][u8 codec][u8 flags][payload]
```


| 字段                 | 说明                                                               |
| ------------------ | ---------------------------------------------------------------- |
| `payload_len`      | 载荷字节数（大端 u32）                                                    |
| `width` / `height` | 本帧逻辑宽高（大端 u16），**必须等于该帧 JPEG 解码后的像素宽高**，并与 `tap` / `swipe` 坐标系一致 |
| `codec`            | `1` = JPEG（**必须实现**）；`2` = H.264（可选，须同时在 `operations` 中声明）       |
| `flags`            | bit0 = 关键帧；其余位保留为 0                                              |
| `payload`          | 一帧编码数据；**禁止**在 payload 内再套 JSON                                  |


一帧一个包。设备按自身能力出帧，但须在 `ui.start` 的 `returns` 与 `operations.args`（`fps`、`max_width`、`codec` 等）中写清预期，便于智能体设播放器参数。建议局域网 JPEG 连续帧：约 5–15 fps、长边 ≤ 720；具体上下限写在 `operations.args` 的 `min` / `max`。

#### 4.9.5 屏幕分辨率、坐标系与触控

声明了 `ui` 的设备**必须**给出当前可操作画面的**逻辑分辨率**，否则智能体无法按镜像画面操作。


| 字段       | 类型  | 必须  | 说明                        |
| -------- | --- | --- | ------------------------- |
| `width`  | int | 是   | 画面逻辑宽，单位像素，≥ 1，原点在左上，向右为正 |
| `height` | int | 是   | 画面逻辑高，单位像素，≥ 1，原点在左上，向下为正 |


这是镜像与点击的唯一坐标系，**不是**物理毫米、dp/pt，也不是未缩放的原生屏宽高（除非出流就是原生分辨率）。

必须同时满足：

1. `ui.start` **成功响应**带回本次出流的实际 `width` / `height`。若请求里有 `max_width` 并因此缩小了画面，必须报缩小后的宽高，禁止写死分辨率和与当前 JPEG 不符的占位值。
2. **每一帧帧头**的 `width` / `height` 必须等于**该帧 JPEG 解码后的像素宽高**。禁止帧头沿用上一次会话、旋转前或 `ui.start` 时的旧尺寸。
3. `hello` **应当**给出当前屏幕逻辑宽高（仍**不得**带 `ui` URI），便于智能体预留镜像窗；此值可在出流前是原生屏，出流开始后以帧头与 `ui.start` 为准。
4. 若声明 `tap` / `swipe`：`x` / `y`（及滑动终点）与**当前帧** `width` × `height` 相同。越界回 `bad-arg`。

智能体映射镜像点击时：以**当前帧 JPEG 像素**（及与之相等的帧头）为准，不得只信过期的 `ui.start` 宽高。设备若做不到帧头与 JPEG 一致，视为违反本剖面。

旋转、折叠、分屏或改分辨率后，须在**下一帧头**（并在随后的 `ui.start` / `status`（若实现，见 §4.9.6）中）给出新宽高。出流相对物理屏做了缩放时，点击走出流坐标，由设备内部映射到物理屏；不得要求智能体按未声明的物理分辨率发 `tap`。

只读屏可只声明 `ui`、不声明 `tap` / `swipe`。`home` / `back` / 输入文字等仍由硬件类型 `service` 定义，不在本剖面。

#### 4.9.6 占用与会话

- `ui.start` / `ui.stop` / `screenshot` / `tap` / `swipe` 均为控制动作，必须 `occupy` 且带有效 `token`。
- 同时只允许一路 UI 流。**同一占用方**重复 `ui.start`：设备**可以**刷新会话（旧 URI 立即作废、旧连接断开），或回 `code=busy`——**必须在 `operations[].description` 中写明采用哪一种**；**其它占用方**在已有活动流时 `ui.start` **必须**回 `code=busy`。
- `release`、租约到期、`goodbye`、进程退出：必须停流、断开 UI 端口占用方、作废该次 URI。
- UI 连接 token 无效：断开，不出帧。

`status` 是本版**可选**的**厂商自定义动作**，**不属于**六个通用动作（§4.5），也不在 §7.1 清单的必做项内。设备若实现它，**应当**包含：`ui`（是否在出流）、`width`、`height`、`ui_enabled`。智能体**不得**在 `operations` 未声明 `status` 时调用它。

#### 4.9.7 安全

整屏 / 整界面可见，默认应视为敏感能力：

- `security.allow` 应含 `local-ui-stream`；有触控时再加 `local-touch-inject`
- 用户未确认时回 `denied`（`security.confirm` 可列 `ui.start`、`tap`）；确认通路与超时见 §4.5「用户确认」
- `scope` 应为 `lan`；UI 端口只绑局域网地址
- 帧、URI、`token` 禁止写入 UDP / TXT / `hello` / `who` / `describe`

系统权限（录屏、无障碍等）写在该 `service` 文档。不能注入点击的平台：不要声明 `tap` / `swipe`。

### 4.10 端点与媒体流通道（可选，声明则必须实现）

本通道适用于可向其它设备**提供或消费**连续媒体的设备（音乐流 Source、可灌流音箱、摄像头等）。不参与设备间转发的设备**不声明** `endpoints`。网关如何 `route` 由硬件类型文档（如 `bridge/1`）规定；本文件只规定端点如何自述与开流。

#### 4.10.1 声明义务

参与转发的设备**必须**：

- 在 `hello` / `describe` 中提供 `endpoints[]`（完整对象，含自然语言 `description`）
- 在 `capabilities` 中含相关开流动作名（如 `stream.start`、`stream.stop`）；`announce` 可用短名摘要
- 实现开流控制动作，并在成功后返回独立的 `stream` URI（**不得**出现在 UDP / TXT / 未开流的 `hello`）



#### 4.10.2 端点对象


| 字段               | 类型       | 必须  | 说明                                                              |
| ---------------- | -------- | --- | --------------------------------------------------------------- |
| `id`             | string   | 是   | 机器码，如 `audio_out`、`a2dp_in`                                     |
| `dir`            | string   | 是   | `output`（产出）或 `input`（消费）                                       |
| `kind`           | string   | 是   | 本版常用 `audio`；亦可 `video` / `frame` / `byte`（扩展）                  |
| `media`          | string[] | 是   | 至少 1 个 IANA 类型，如 `audio/pcm`                                    |
| `access`         | string   | 是   | output 用 `provide`，input 用 `consume`                            |
| `max_sessions`   | int      | 应当  | 默认 1                                                            |
| `description`           | string   | 是   | **自然语言**：这个端点是什么、给谁用、产/消费什么                                     |
| `open` / `close` | string   | 应当  | 默认 output=`stream.start`，input=`stream.accept`，关闭=`stream.stop` |
| `close_on_release` | bool  | 可以  | 缺省 `false`；置 `true` 表示 `release` / 租约到期时**关闭**本端点已建立的流（覆盖 §4.10.5 的默认不停流行为）。需在 `hello.endpoints` 中如实公告 |


示例：

```json
{
  "endpoints": [
    {
      "id": "audio_out",
      "dir": "output",
      "kind": "audio",
      "media": ["audio/pcm"],
      "access": "provide",
      "max_sessions": 1,
      "description": "正在播放的音乐音频输出，可供网关转发到音箱；PCM，单会话。",
      "open": "stream.start",
      "close": "stream.stop"
    }
  ]
}
```



#### 4.10.3 开流与格式

- `stream.start` / `stream.accept` / `stream.stop` 均为控制动作，必须 `occupy` 且带有效 `token`。
- 成功响应返回 `stream` URI 与 `format` 对象；URI 格式与 `control` 相同（IPv6 用方括号），端口与 `control` 分开监听，建议默认 **9532**（见 7.2）。

`format` 对象（**必须**带齐，`raw` 与 `framed` 都一样）：

| 字段 | 类型 | 必须 | 说明 |
| --- | --- | --- | --- |
| `mode` | string | 是 | `raw`（连接后即为连续裸载荷）或 `framed`（按下方分帧） |
| `media` | string | 是 | IANA 媒体类型，须与端点 `media[]` 中的一项一致，如 `audio/pcm` |
| `sample_rate` | int | 音频必须 | 采样率 Hz，如 `48000` |
| `channels` | int | 音频必须 | 声道数，如 `1` / `2` |
| `bits` | int | 音频必须 | 每样本位深，如 `16` |
| `endian` | string | 位深 > 8 时必须 | `le` / `be` |
| `signed` | bool | 音频必须 | 有符号 / 无符号 |

示例：`{"mode":"framed","media":"audio/pcm","sample_rate":48000,"channels":2,"bits":16,"endian":"le","signed":true}`

`framed` 分帧（**必须**按此格式）：每个包 = `[u32 BE payload_len][payload]`，`payload` 为一帧编码数据（如一个 PCM 块、一个 Opus/AAC 帧）；**禁止**在 `payload` 内再套 JSON。`raw` 模式则不做任何分帧，收方按 `format` 参数自行切块。

#### 4.10.4 流连接与鉴权（**必须**，与 UI 剖面统一）

连接 `stream` URI 后：

1. **鉴权**：客户端先发占用令牌：`[u8 token_len][token 字节]`（`token_len` 为 token 字符串 UTF-8 字节长度）——**与 §4.9.4 UI 流完全相同的格式**。
2. **应答**：设备回 1 字节：`0x00` 表示成功；其它值表示失败并断开，不出数据。
3. 鉴权通过后才开始按 `format.mode` 传载荷。禁止把连续媒体塞进控制 JSON。

#### 4.10.5 生命周期与所有权（**必须**）

- **默认**：`release` / 租约到期**不**自动关闭已建立的媒体 `stream`（便于智能体按对话轮次 `release` 后，网关仍可持续拉流）。若硬件要求释放即停流，须在该 `service` 文档中写明，并可用 `endpoints[].close_on_release=true` 声明。
- 这与 UI 剖面不同：`ui` 在 `release` 时**必须**停流（见 4.9.6）。
- **流的归属**：每条流在设备侧记录「开流时的 `holder` + `token`」。`release` **不改变**该记录。
- **停止权限**（**必须**按此实现，解决「新占用者能否停掉前任的流」）：
  1. **开流者**：持**原 token**（即使已 `release`）**可以**停掉自己开立的流；
  2. **当前占用方**：`occupy` 成功后，带有效 `token` **可以** `stream.stop` 掉**任意**仍在传输的流（含前任开立的）；
  3. 其它智能体不得停流，违者回 `code=unauthorized`。
- **公开当前流**：设备**应当**在 `occupy` 成功响应、`who` 与 `describe` 中返回 `streams` 数组（`[{id, endpoint, holder, since}]`，**不含** URI 与 `token`），使新占用者知道有哪些流活着。
- `goodbye` / 进程退出：**必须**停掉全部流。



#### 4.10.6 安全

- `security.allow` 应含相应标签（如 `local-audio-stream`）
- `stream` URI、`token` 禁止写入 UDP / TXT / 未开流的 `hello` / `who` / `describe`

---



## 5. 报文定义



### 5.1 `announce`（设备 → 组播）

设备发布「我是谁、怎么连、怎么用」。**不要**在 UDP 里放业务数据、用户数据或 `token`。


| 字段             | 类型       | 必须    | 说明                                                       |
| -------------- | -------- | ----- | -------------------------------------------------------- |
| 公共头            |          | 是     | `kind` 必须为 `announce`                                    |
| `type`         | string   | 是     | 设备种类                                                     |
| `service`      | string   | 是     | 业务协议                                                     |
| `name`         | string   | 是     | **自然语言**显示名（见 4.5），如「客厅灯」                                |
| `summary`      | string   | 应当    | **自然语言**自述一两句（见 4.5）；建议 ≤ 120 字                          |
| `model`        | string   | 应当    | 硬件型号                                                     |
| `ip`           | string   | 应当    | 本机局域网 IPv4，不要发 `127.0.0.1`。仅有 IPv6 时可省略，此时必须填 `ipv6`     |
| `ipv6`         | string   | 应当    | 本机局域网 IPv6（不要带 `%zone`）。仅链路本地 `fe80::` 时智能体可能无法跨网卡直连     |
| `port`         | number   | 是     | 业务端口                                                     |
| `control`      | string   | 是     | 建议 `tcp://{ip}:{port}`；仅 IPv6 时用 `tcp://[{ipv6}]:{port}` |
| `capabilities` | string[] | 是     | **能力标签数组**：可含动作名与**剖面标签**（`ui` / `tap` / `swipe`）；剖面标签只作声明用，不是可调用动作，可调用面以 `operations` 为准（见 §4.4） |
| `security`     | object   | 是     | 安全边界摘要：`scope` + `auth`                                  |
| `state`        | string   | 是     | `registered` / `managed`（goodbye 用 `offline`）            |
| `event`        | string   | 应当    | `register` / `manage` / `release` / `offline`            |
| `busy`         | bool     | 是     | 兼容字段，`state=managed` 时为 true                             |
| `holder`       | string   | 受管时必须 | 占用方 `agent`                                              |
| `until`        | number   | 受管时应当 | 租约到期 Unix 秒；**有限租约为到期时刻，无限期（`lease_security=9999`）时为 `0`** |
| `hb`           | number   | 可以    | 稳态 `announce` 间隔（秒）；缺省视为 **5**。调大时**必须**公告（见 4.6 存活检测） |


发送时机（必须遵守）：


| 时机                             | 次数 / 间隔                                    |
| ------------------------------ | ------------------------------------------ |
| 入网且 `control` 端口已监听            | 连发 **3** 次，间隔约 **0.3 s**                   |
| 稳态（发现心跳）                       | 默认每 **5 秒**一次；只允许调大且须公告 `hb`（见 4.6）          |
| 收到匹配的 `query`                  | 等待 **20–120 ms** 随机抖动后立刻再发 1 次；同一发送方（按 `id`）**1 秒内只应答一次**，避免应答风暴 |
| 收到自己的 `announce`               | 忽略，不要递归发送                                  |
| `state` 变化                     | 立刻再发 1 次                                   |
| `ip` / `ipv6` / `control` 主机变化 | **立刻**再发（字段用新地址）；并按第 3.3 节刷新 Bonjour（若已实现） |


示例：

```json
{
  "version": 1,
  "protocol": "bmahs/1.0",
  "kind": "announce",
  "timestamp": 1712345678,
  "id": "acme-lamp-00a1",
  "type": "light",
  "service": "light/1",
  "name": "客厅灯",
  "summary": "客厅吸顶灯，可开关和调节亮度。占用后才能改亮度；任务结束请 release。",
  "model": "ACME-BULB-1",
  "ip": "192.168.1.42",
  "ipv6": "fd12:3456:789a:1::42",
  "port": 9527,
  "capabilities": ["on", "off", "brightness", "describe", "info", "who", "register", "occupy", "release"],
  "security": {"scope": "lan", "auth": "token"},
  "control": "tcp://192.168.1.42:9527",
  "state": "registered",
  "event": "register",
  "busy": false,
  "hb": 5
}
```


> `holder` / `until` 仅在受管时出现；空闲设备（如上例）不携带。`hb` 缺省视为 5。


### 5.2 `query`（智能体 → 组播）

```json
{
  "version": 1,
  "protocol": "bmahs/1.0",
  "kind": "query",
  "timestamp": 1712345678,
  "id": "bmahs-ctl",
  "want": "*"
}
```


| 字段     | 必须  | 说明                 |
| ------ | --- | ------------------ |
| 公共头    | 是   | `kind` 必须为 `query` |
| `want` | 应当  | 缺省按 `*` 处理         |


智能体建议：发出后 **0.2 s** 仍无结果可再发 1 次；总等待 **2–4 s**。不要高频扫描。同一 `want` 在 **1 秒内**不要重复发送；设备侧对同一发送方在 1 秒内只应答一次（见 5.1）。

### 5.3 `goodbye`（设备 → 组播）

进程退出、主动下线或即将断开网络时发送。

```json
{
  "version": 1,
  "protocol": "bmahs/1.0",
  "kind": "goodbye",
  "timestamp": 1712345678,
  "id": "acme-lamp-00a1",
  "type": "light",
  "service": "light/1",
  "name": "客厅灯",
  "ip": "192.168.1.42",
  "ipv6": "fd12:3456:789a:1::42",
  "port": 9527,
  "state": "offline",
  "event": "offline"
}
```

必须连发 **2** 次，间隔约 **0.3 s**。智能体收到后应从列表中移除该 `id`。未收到 `goodbye` 时，可将超过**无心跳删除时限**（默认 60 秒）未更新的条目标为离线并删除。

---



## 6. 发现之后如何使用

BMAHS 只负责把智能体带到门口。进门后的握手如下，**所有硬件类型都必须实现**（与具体 `service` 命令无关）。

### 6.1 连接

1. 解析 `control`。`tcp://A.B.C.D:PORT` 对 IPv4 建连；`tcp://[IPv6]:PORT` 对 IPv6 建连（去掉方括号）。双栈设备优先连 `ip`。
2. 设备必须已在该端口监听（建议双栈：`::` + `IPV6_V6ONLY=0`，或同时绑 `0.0.0.0` 与 `::`）；`announce` 不得早于监听就绪。（此处是 **TCP** 监听，`IPV6_V6ONLY=0` 以求双栈；**与 §3.1 的 UDP 组播接收 `IPV6_V6ONLY=1` 取值相反，勿混淆**。）
3. 连接成功后，设备**立刻**写一行 JSON + `\n`（`hello`），再读智能体命令。



### 6.2 `hello`（设备 → 智能体，TCP 首行）


| 字段                                              | 必须         | 说明                                                          |
| ----------------------------------------------- | ---------- | ----------------------------------------------------------- |
| `ok`                                            | 是          | `true`                                                      |
| `action`                                        | 是          | 固定 `hello`                                                  |
| `protocol`                                      | 是          | `bmahs/1.0`                                                 |
| `type`                                          | 是          | 与 `announce` 相同                                             |
| `service`                                       | 是          | 与 `announce` 相同                                             |
| `id`                                            | 是          | 与 `announce` 相同                                             |
| `name`                                          | 是          | **自然语言**显示名（见 4.5）                                          |
| `summary`                                       | 是          | **自然语言**自述：是什么、能做什么、使用注意（见 4.5）                             |
| `hint`                                          | 应当         | **自然语言**使用提示，如推荐调用顺序                                        |
| `capabilities`                                  | 是          | **能力标签数组**：可含动作名与**剖面标签**（`ui` / `tap` / `swipe`）；剖面标签只作声明用，不是可调用动作（见 §4.4） |
| `operations`                                    | 是          | 完整操作清单，见 4.5                                                |
| `security`                                      | 是          | 完整安全边界，见 4.5                                                |
| `endpoints`                                     | 有端点时必须     | 端点表，见 4.10；每项必须含自然语言 `description`                                 |
| `state` / `event` / `busy`                      | 是          | 设备自我控制，见 4.6                                                |
| `holder` / `until`                              | 受管时必须 / 受管时应当 | `holder` 受管时必须、`until` 受管时应当；空闲设备不得携带（同 §4.6）                |
| `control`                                       | 可以         | 回显控制 URI                                                    |
| `hb`                                            | 可以         | 与 `announce` 相同的 `announce` 间隔（秒），缺省 5                        |
| `width` / `height`                              | 有 `ui` 时应当 | 当前可操作画面的逻辑分辨率（像素）。**不得**附带 `ui` URI；出流后的权威值是 `ui.start` 与帧头 |


`hello` **不得**包含 `token`。

示例：

```json
{
  "ok": true,
  "action": "hello",
  "protocol": "bmahs/1.0",
  "type": "light",
  "service": "light/1",
  "id": "acme-lamp-00a1",
  "name": "客厅灯",
  "summary": "这是客厅的吸顶灯。智能体可开关灯光、把亮度设到 0–100%。控制前请 occupy，结束后必须 release。",
  "hint": "推荐流程：occupy → on/brightness → release。亮度参数名是 level，单位 %。",
  "capabilities": ["on", "off", "brightness", "describe", "info", "who", "register", "occupy", "release"],
  "operations": [
    {
      "name": "on",
      "description": "打开这盏客厅灯",
      "args": [],
      "result": "成功时灯亮，并回 state=on",
      "returns": [
        {"name": "state", "type": "string", "enum": ["on", "off"], "description": "灯当前是否亮着", "example": "on"}
      ]
    },
    {
      "name": "off",
      "description": "关闭这盏客厅灯",
      "args": [],
      "result": "成功时灯灭，并回 state=off",
      "returns": [
        {"name": "state", "type": "string", "enum": ["on", "off"], "description": "灯当前是否亮着", "example": "off"}
      ]
    },
    {
      "name": "brightness",
      "description": "设置客厅灯亮度；须已 occupy 并携带 token",
      "args": [
        {
          "name": "level",
          "type": "int",
          "required": true,
          "min": 0,
          "max": 100,
          "unit": "%",
          "example": 40,
          "description": "亮度百分比：0 最暗、100 最亮"
        }
      ],
      "result": "成功时回开关状态与当前亮度",
      "returns": [
        {"name": "state", "type": "string", "enum": ["on", "off"], "description": "灯当前是否亮着", "example": "on"},
        {"name": "level", "type": "int", "min": 0, "max": 100, "unit": "%", "description": "当前亮度百分比", "example": 40}
      ]
    },
    {
      "name": "describe",
      "description": "查询本灯的完整操作清单、安全边界与自然语言自述",
      "args": [],
      "result": "成功时回完整 operations、security 与全部身份字段（含 hint / state），不含 token",
      "returns": [
        {"name": "operations", "type": "array", "description": "完整操作清单"},
        {"name": "security", "type": "object", "description": "完整安全边界"},
        {"name": "summary", "type": "string", "description": "设备自然语言自述"},
        {"name": "hint", "type": "string", "description": "使用提示，如推荐调用顺序"},
        {"name": "state", "type": "string", "enum": ["registered", "managed"], "description": "设备当前状态"}
      ]
    }
  ],
  "security": {
    "scope": "lan",
    "auth": "token",
    "allow": ["local-light-control"],
    "deny": ["exec", "network-fetch", "cloud-upload", "write-filesystem"],
    "confirm": [],
    "notes": "只控制本灯的开关与亮度，不能读盘、执行命令或上传云端"
  },
  "state": "registered",
  "event": "register",
  "busy": false,
  "control": "tcp://192.168.1.42:9527"
}
```

> 上例只展开业务动作与 `describe`。**六个通用动作必须全部出现在 `operations` 中**，形状照抄 §4.5「通用动作的标准定义」。

智能体读完 `hello` 后按第 4.8 节执行。

### 6.3 业务通道约定（各 `service` 共用）

为降低智能体适配成本，所有 `service` **应当**采用同一成帧（这是使用层默认约定）：

- TCP，一行一个 UTF-8 JSON，以 `\n` 结束。
- 请求至少含 `action`（小写）；控制类请求还含 `agent`，受管后还含 `token`。
- 成功响应至少含 `ok=true` 和 `action`。
- 失败响应见 4.7。
- 提供 `describe`：回**完整** `operations` 与 `security`（权威来源），不含 `token`。
- 提供 `info`：回**精简**设备描述（身份字段 + `state`），**可以**不含完整 `operations` / `security`；需要完整清单时用 `describe`。
- 提供 `who`：只回**占用关系**（`state` / `holder` / `until` / `lease_security`），用于回答「现在谁在用」。
- 提供 `register`：幂等登记刷新，回当前公开状态快照（**字段同 `who`**，见 §4.5），**不**改变占用关系、**不**签发 `token`。

四个只读动作的完整定义见 §4.5「通用动作的标准定义」。

**画面与帧数据不得走本通道。** 声明了 `ui` 的设备，画面只走第 4.9 节的二进制 UI 流；禁止在控制 JSON 或 base64 字段中传整帧图像。

参数字段名与 `operations.args[].name` 对齐，例如 `{"action":"brightness","level":40,"agent":"alice","token":"..."}`。

### 6.4 Bonjour TXT（与 UDP 对齐）

全部为字符串。`capabilities` 用逗号分隔。**不得**有 `token` 键。


| TXT 键          | 对应 UDP 字段                | 必须                          |
| -------------- | ------------------------ | --------------------------- |
| `txtvers`      | （固定 `1`）                 | 是                           |
| `protocol`     | `protocol`               | 是                           |
| `type`         | `type`                   | 是                           |
| `service`      | `service`                | 是                           |
| `id`           | `id`                     | 是                           |
| `name`         | `name`                   | 是                           |
| `summary`      | `summary`（可截断）           | 应当                          |
| `model`        | `model`                  | 应当                          |
| `capabilities` | `capabilities`           | 是                           |
| `security`     | `security`（`scope,auth`） | 是                           |
| `ip`           | `ip`                     | 应当                          |
| `ipv6`         | `ipv6`                   | 有 IPv6 时应当                  |
| `state`        | `state`                  | 是（`registered` / `managed`） |
| `event`        | `event`                  | 应当                          |
| `busy`         | `busy`                   | 是（`0` / `1`）                |
| `holder`       | `holder`                 | 受管时应当                       |
| `hb`           | `hb`                     | 可以（调大心跳间隔时**必须**，见 4.6）     |


TXT 总体积应尽量小（建议整包 < 400 字节）。不要把业务数据、日志写入 TXT。

系统侧自检（macOS 示例）：

```bash
dns-sd -B _bmahs._tcp local
dns-sd -L "设备显示名" _bmahs._tcp local
```

---



## 7. 制造商实现清单

按顺序做完即可被 BMAHS 智能体发现并使用。

### 7.1 设备固件 / 嵌入式软件

- [ ] 经无线或有线取得 IPv4 写入 `ip`；有 IPv6 时写入 `ipv6`
- [ ] 选定并固化 `id`、`type`、`service`、**自然语言** `name` / `summary`、`model`
- [ ] 在业务端口开始监听（双栈）
- [ ] 加入 `239.255.42.42:5354`，有 IPv6 时再加入 `[ff02::4242]:5354`，按第 5.1 节发 `announce`（含 `name` / 建议含 `summary`）；稳态间隔默认 **5 秒**（**只可调大**，调大时**必须**用 `hb` 公告）
- [ ] 收到匹配的 `query` 后抖动并回 `announce`
- [ ] `ip` **/** `ipv6` **变化时自动刷新注册**（第 3.3 节）：立刻重发带新地址的 `announce`；已实现 Bonjour 则更新 A/AAAA 与 TXT；`id` 不变
- [ ] 退出时发两次 `goodbye`（间隔约 0.3 s）
- [ ] 注册 Bonjour `_bmahs._tcp`，TXT 按 6.4
- [ ] 写好自然语言自述与操作清单 `operations`：`summary` / 各 `description` / `result` / `notes` 均为完整自然语言；`args`/`returns` 为对象；数值补 min–max，枚举补 enum，并给 example
- [ ] 写好安全边界 `security`（scope / auth / allow / deny / **必须** notes）
- [ ] TCP 连接后立即发送含 `summary` / `hint`（应当）/ `operations` / `security` 的 `hello`（不含 token）
- [ ] 实现六个通用动作 `describe` / `info` / `register` / `occupy` / `release` / `who`（标准 `args` / `returns` 见 §4.5）；实现本硬件类型 `service` 命令
- [ ] 清单外动作回 `unknown-action`；`deny` 能力回 `denied`；参数错误回 `bad-arg`；失败 `error` 用自然语言说明原因
- [ ] 维护 `offline` / `registered` / `managed`；默认租约 **60 秒**，`ttl` 10–9999；无限期时 `until=0` 且不按时间到期；有限租约到期自动回 `registered` 并作废旧 token
- [ ] `occupy` 签发 `token`；控制与 `release` 校验 `token`（并与签发时的 `agent` 绑定）；token 不出现在 UDP / TXT / hello / who / describe
- [ ] 支持至少 **2 路**并发 TCP 控制连接（占用方控制 + 其它方只读查询），共享同一占用状态；只读动作在任意连接可用
- [ ] 按 §4.6 存活检测：智能体侧无心跳删除时限 = `clamp(12 × hb, 60 秒, 30 分钟)`
- [ ] 若声明 `security.confirm`：实现本地确认通路与 `confirm_timeout`（默认 30 秒），超时/否决回 `denied` 且 `retryable=true`
- [ ] 失败响应带 `code` 与 `retryable`；退出发 `goodbye`
- [ ] **（有操作界面时）** `capabilities` 含 `ui`；`operations` 含 `ui.start` / `ui.stop`（及可选 `tap` / `swipe`）
- [ ] **（有操作界面时）** 给出真实逻辑分辨率：`ui.start` 与每帧帧头的 `width`×`height` 必须等于该帧 JPEG 像素，并作为 `tap` / `swipe` 坐标系；禁止占位宽高
- [ ] **（有操作界面时）** 实现第 4.9 节二进制 UI 流：连接后校验 token，按定长头发 JPEG 帧
- [ ] **（有操作界面时）** `ui.start` 成功后才返回 `ui` URI；`release` / `goodbye` 时停流并作废 URI
- [ ] **（有操作界面时）** 禁止在控制 JSON 或 base64 中传帧；禁止在 UDP / TXT / `hello` 中发 URI 或画面
- [ ] **（有端点时）** `hello.endpoints` 每项含自然语言 `description`；实现 `stream.start`/`accept`/`stop`；URI 仅开流后返回；若硬件要求释放即停流，须声明 `close_on_release=true`（见 §4.10.2）



### 7.2 建议默认端口

无冲突时，业务 TCP（`control`）使用 **9527**。被占用可改，但 `announce.port` / `control` / Bonjour 端口必须一致。

有操作界面的设备，UI 流端口与 `control` **分开监听**；端口由 `ui.start` 返回，建议默认 **9531**（被占用可改，但须在 `ui.start` 的 `returns` 中如实返回）。

有端点的设备，媒体 `stream` 端口同样与 `control` 分开监听；端口由开流响应返回，建议默认 **9532**（被占用可改，须在开流响应的 `returns` 中如实返回）。

### 7.3 联调

同一局域网电脑：成功时 `discover` 会打印 `id`、`type`、`service`、`control`、`state`。再用 TCP 连接 `control`，应立刻收到 `hello`。智能体即可按 `service` 调用该硬件。

智能体参考实现：`bmahs/`（发现 / 使用）。各硬件类型设备实现不在本协议范围内。

**参考实现的条款覆盖情况**（对应文首「文档状态」；如与仓库实际不符，请以仓库为准并更新本表）：

| 条款 | 参考实现状态 |
| --- | --- |
| §3.1 UDP 组播发现、§3.3 地址刷新、§5.1–5.3 报文 | 已覆盖 |
| §6.1–6.3 控制连接、`hello`、成行 JSON、占用与租约 | 已覆盖 |
| §4.7 错误信封、§4.8 智能体义务 | 已覆盖 |
| §4.9 操作界面（二进制 UI 流） | **尚未覆盖** |
| §4.10 端点与媒体流 | **尚未覆盖** |
| §6.4 Bonjour / DNS-SD | 部分覆盖（依赖系统 mDNS） |

### 7.4 不要做的事

- 不要在组播里发送密码、密钥、`token`、用户账号。
- 不要把 `announce` 当作状态推送（业务进度、传感器高频数据）。
- 不要使用 `127.0.0.1` 或链路本地以外的不可达地址作为 `ip`。
- 不要修改组播地址 / 端口，否则智能体找不到设备。
- 不要用新的 `kind` 代替 `announce` / `query` / `goodbye`；扩展请加字段，不要加新 `kind`。
- 不要用 `version` 以外的字段名（如 `v`）表示协议主版本；也不要改写 `description` 等已定字段名。
- 不要在 `release` 时顺手停掉非自己开立、且当前占用方仍在使用的媒体流（停流权限见 §4.10.5）。
- 不要在本文件中捆绑某一硬件类型的业务命令；硬件类型命令写在该 `service` 自己的文档里。
- 不要在本版用 USB bulk 或纯串口代替 IP 上的发现与 `control`。有线设备若用 USB，须先成为 IP 网卡，再走本协议。
- 不要在组播、Bonjour TXT、`hello` 中发送画面、UI URI 或帧数据。
- 不要在控制 JSON 行中传整帧图像或 base64 编码的画面（声明 `ui` 的设备必须用第 4.9 节 UI 流）。
- 不要只用 `type` / `action` 机器码冒充说明：发布必须带可读 `name` / `summary`，动作必须带自然语言 `description`。

---



## 8. 新硬件类型登记表示例

厂商提交对接材料时，按此表填写即可（不必改 BMAHS 本身）。


| 项              | 示例                                                                   |
| -------------- | -------------------------------------------------------------------- |
| 厂家 / 型号        | ACME / BULB-1                                                        |
| `type`         | `light`                                                              |
| `service`      | `light/1`                                                            |
| `name`         | 自然语言显示名，如「客厅灯」                                                       |
| `summary`      | 自然语言自述一两句（能做什么、使用注意）                                                 |
| `capabilities` | `on,off,brightness,describe,info,who,register,occupy,release`（含六个通用动作）      |
| `operations`   | name / 自然语言描述 description·result / args 对象含 type·required·example / returns |
| `security`     | scope / auth / allow / deny / **必须** notes（自然语言）                     |
| `control`      | `tcp://<dhcp-ip>:9527`                                               |
| 业务文档           | 公司内部 `light-1.md`（请求 / 响应字段）                                         |


---



## 9. 兼容性


| 对端                    | 行为                                                             |
| --------------------- | -------------------------------------------------------------- |
| `bmahs/1.0`           | 现行候选，新设备应发送此值                                                  |
| `version != 1`        | 必须丢弃                                                           |
| 未知 `type` / `service` | 发现层照常列出；使用层可只显示、不控制                                            |


局域网占用令牌不是传输层认证。量产若需防伪控，应再叠加 TLS，或在 `control` 使用 `tcp+tls://`（须另发 `service` 修订版，不能 silently 改 `bmahs/1.0` 的发现语义）。

---




本文件的变更按时间倒序追加。`protocol` 主线仍为 `bmahs/1.0` 时，发现语义向后兼容。

## 附录 A. 索引（纯索引）

**本附录不含规范内容。** 它只回答一个问题：「这条东西定义在哪一节？」——不复制字段定义、取值、默认值、约束条件，也不重复任何示例。

- 任何条目**是什么、怎么取值、是否必须**，一律以所指章节为准；
- 本附录与正文如有任何出入，**以正文为准**，并请修本附录而非修正文；
- 维护约定：正文新增 / 更名 / 删除字段或动作时，本附录**只需改指向**；**禁止**在此复述定义——附录一旦复制定义，就会再次出现「正文改了附录没改」。

---

### A.1 结构总览

```
                    IP 网络（无线或有线，IPv4 / IPv6）              §3
 ┌────────────────────────────────────────────────────────────┐
 │  发现层                                                       │
 │  标识      protocol = bmahs/1.0                               │
 │  通道      UDP 239.255.42.42 / ff02::4242 :5354               │
 │            Bonjour _bmahs._tcp                                │
 │  报文      announce / query / goodbye                         │
 │  入口      announce.control → tcp://IP:PORT                   │
 └───────────────────────────────┬────────────────────────────┘
                                 ▼
 ┌────────────────────────────────────────────────────────────┐
 │  使用层（控制）                                                 │
 │  标识      service = 各硬件类型自定，如 light/1                  │
 │  通道      TCP，一行一个 UTF-8 JSON + \n                        │
 │  首行      hello                                              │
 │  工具箱    operations / security / token                      │
 │  错误      ok / code / error / retryable                       │
 └───────────────────────────────┬────────────────────────────┘
                                 ▼
 ┌────────────────────────────────────────────────────────────┐
 │  可选媒体层   UI 流 / 端点媒体流                                │
 │  独立 TCP，先 token 鉴权，再二进制载荷                           │
 └────────────────────────────────────────────────────────────┘
```


| 层 | 涉及条目 | 章节 |
| --- | --- | --- |
| 发现层 | `protocol`、`announce` / `query` / `goodbye`、`control`、组播地址 | §3.1、§3.2、§3.3、§4.2、§5.1–§5.3、§6.4 |
| 使用层（控制） | `hello`、`operations`、`security`、`agent` / `token`、错误信封 | §4.1、§4.5–§4.8、§6.1–§6.3 |
| 操作界面层（可选） | `ui` 剖面、`ui.start` / `ui.stop`、帧头、坐标系 | §4.9.1–§4.9.7 |
| 端点媒体层（可选） | `endpoints`、开流动作、`format`、流鉴权与生命周期 | §4.10.1–§4.10.6 |


### A.2 章节索引


| 主题 | 章节 |
| --- | --- |
| 目标、范围与「本文件不规定的内容」 | §1 |
| 角色与分层 | §2 |
| 传输：UDP 组播 | §3.1 |
| 传输：Bonjour / DNS-SD | §3.2 |
| 传输：地址变化时刷新注册 | §3.3 |
| 公共编码：JSON 与成帧 | §4.1 |
| 公共头字段（所有 UDP 报文） | §4.2 |
| `id` 命名规则 | §4.3 |
| `type` / `service` / `want` / `control` / `capabilities` / `security` / `port` | §4.4 |
| 自然语言自述（`name` / `summary` / `hint` 等） | §4.5「自然语言自述」 |
| 操作清单 `operations` 与 `args` / `returns` 结构 | §4.5「操作清单 `operations`」 |
| 六个通用动作的标准定义与语义边界 | §4.5「通用动作的标准定义」 |
| 安全边界 `security` 全文 | §4.5「安全边界 `security`」 |
| 用户确认通路 `security.confirm` | §4.5「用户确认」 |
| 设备自我控制：状态机与字段 | §4.6 |
| 占用令牌 `token` | §4.6「占用令牌 `token`」 |
| 存活检测、心跳与脱网 | §4.6「存活检测与脱网」 |
| 错误信封与稳定错误码 | §4.7 |
| 智能体义务 | §4.8 |
| 操作界面通道（UI 剖面） | §4.9.1–§4.9.7 |
| 端点与媒体流通道 | §4.10.1–§4.10.6 |
| `announce` 报文 | §5.1 |
| `query` 报文 | §5.2 |
| `goodbye` 报文 | §5.3 |
| 连接 `control` | §6.1 |
| `hello`（TCP 首行） | §6.2 |
| 业务通道共同约定 | §6.3 |
| Bonjour TXT | §6.4 |
| 制造商实现清单（固件 / 端口 / 联调 / 禁止项） | §7.1、§7.2、§7.3、§7.4 |
| 参考实现条款覆盖矩阵 | §7.3 |
| 新硬件类型登记表示例 | §8 |
| 兼容性（`bmahs/1` / `1.1` / `1.2`） | §9 |
| 修订记录 | §10 |


### A.3 报文与动作索引


| 条目 | 类别 | 定义位置 |
| --- | --- | --- |
| `announce` | 设备 → 组播 | §5.1 |
| `query` | 智能体 → 组播 | §5.2 |
| `goodbye` | 设备 → 组播 | §5.3 |
| `hello` | 设备 → 智能体（TCP 首行） | §6.1、§6.2 |
| `describe` | 通用动作 | §4.5「通用动作的标准定义」、§6.3 |
| `info` | 通用动作 | §4.5「通用动作的标准定义」、§6.3 |
| `who` | 通用动作 | §4.5「通用动作的标准定义」、§6.3 |
| `register` | 通用动作（双重语义） | §4.5「通用动作的标准定义」、§4.6 规则 2、§6.3 |
| `occupy` | 通用动作 | §4.5「通用动作的标准定义」、§4.6 |
| `release` | 通用动作 | §4.5「通用动作的标准定义」、§4.6 |
| `status` | 厂商自定义可选动作（**不属于**通用动作） | §4.9.6 |
| `ui.start` / `ui.stop` | UI 剖面控制动作 | §4.9.3 |
| `screenshot` | UI 剖面控制动作 | §4.9.3 |
| `tap` / `swipe` | UI 剖面控制动作 | §4.9.3、§4.9.5 |
| `stream.start` / `stream.accept` / `stream.stop` | 端点剖面控制动作 | §4.10.3 |
| 业务动作（如 `on` / `off` / `brightness`） | 由各 `service` 自定 | **不在本文件**：见设备 `hello.operations` 与对应硬件类型规范 |


### A.4 字段索引


| 字段 | 定义位置 |
| --- | --- |
| `version` / `protocol` / `kind` / `timestamp` / `id`（UDP 公共头） | §4.2 |
| `id` 的字符集与派生规则 | §4.3 |
| `type` / `service` / `want` / `control` / `capabilities` / `port` | §4.4 |
| `security`（UDP / TXT 中的摘要形态） | §4.4 |
| `name` / `summary` / `hint` / `operations[].description` / `operations[].result` / `args[].description` / `returns[].description` / `security.notes` / `error` / `endpoints[].description` | §4.5「自然语言自述」 |
| `operations`、`operations[].name` / `description` / `args` / `any_of` / `result` / `returns` | §4.5「操作清单 `operations`」 |
| `args[].name` / `type` / `required` / `description` / `min` / `max` / `enum` / `unit` / `example` / `default` | §4.5「操作清单 `operations`」 |
| `returns[].*` | §4.5「操作清单 `operations`」 |
| `security.scope` / `auth` / `allow` / `deny` / `confirm` / `confirm_timeout` / `notes` | §4.5「安全边界 `security`」 |
| `state` / `event` / `busy` / `holder` / `until` / `lease_security` / `ttl` / `agent` | §4.6 |
| `token` | §4.6「占用令牌 `token`」 |
| `hb` | §4.6「存活检测与脱网」 |
| `ok` / `action` / `code` / `error` / `retryable` | §4.7 |
| `model` / `ip` / `ipv6`（`announce` 专有部分） | §5.1 |
| `want`（`query` 专有） | §5.2 |
| `hello` 的字段全集 | §6.2 |
| Bonjour TXT 键 | §6.4 |
| `ui`（UI 流入口 URI）、`codec` / `fps` / `max_width` | §4.9.3 |
| `width` / `height` | §4.9.5 |
| UI 帧头 `payload_len` / `width` / `height` / `codec` / `flags` / `payload` | §4.9.4 |
| 端点对象 `id` / `dir` / `kind` / `media` / `access` / `max_sessions` / `description` / `open` / `close` / `close_on_release` | §4.10.2 |
| `stream`（媒体流入口 URI） | §4.10.3 |
| `format` 对象 `mode` / `media` / `sample_rate` / `channels` / `bits` / `endian` / `signed` | §4.10.3 |
| `streams`（活动流公开摘要） | §4.10.5 |
| 业务参数名（如 `level` / `x` / `y`） | 由各 `service` 的 `operations.args[].name` 定义，**不在本文件** |


### A.5 状态、租约与定时索引


| 条目 | 定义位置 |
| --- | --- |
| 状态取值 `offline` / `registered` / `managed` 与状态机 | §4.6 |
| `event` 取值 `register` / `manage` / `release` / `offline` | §4.6 |
| 默认租约时长、`ttl` 合法范围、无限期占用 | §4.6（规则 3、规则 5、规则 8，及租约表） |
| 租约刷新、到期自动回收、未释放卡住的恢复办法 | §4.6、§4.8 第 6 条 |
| 稳态 `announce` 间隔与 `hb` 字段 | §4.6「存活检测与脱网」 |
| 无心跳删除时限 | §4.6「存活检测与脱网」、§4.8 第 7 条 |
| 设备主动下线（`goodbye` 次数与间隔） | §5.3 |
| `query` 重发节奏与应答合并 | §5.2、§5.1 |
| 设备上线 / 状态变化 / 换 IP 时的重发时机 | §5.1、§3.3 |
| 脱网与恢复的分场景处理 | §4.6「存活检测与脱网」、§4.8 |


### A.6 错误码索引


| 条目 | 定义位置 |
| --- | --- |
| 错误信封字段 `ok` / `action` / `code` / `error` / `retryable` | §4.7 |
| `occupied` | §4.7 |
| `offline` | §4.7 |
| `busy` | §4.7（与 §4.6 的兼容字段 `busy` **同名不同义**，见 §4.7 表注） |
| `unauthorized` | §4.7 |
| `bad-arg` | §4.7 |
| `denied` | §4.7 |
| `unknown-action` | §4.7 |
| 各码的 `retryable` 取值 | §4.7 |


### A.7 剖面、媒体与帧索引


| 条目 | 定义位置 |
| --- | --- |
| `capabilities` 中的剖面标签 `ui` / `tap` / `swipe` | §4.4 |
| UI 剖面的声明义务 | §4.9.1 |
| 控制通道与 UI 流两条通道不得混用 | §4.9.2 |
| UI 流鉴权握手与二进制帧格式 | §4.9.4 |
| UI 逻辑分辨率与坐标系 | §4.9.5 |
| UI 会话、占用与并发限制 | §4.9.6 |
| UI 剖面安全要求 | §4.9.7 |
| 端点表与端点对象字段 | §4.10.2 |
| 开流动作与 `format` 对象 | §4.10.3 |
| 媒体流鉴权握手 | §4.10.4 |
| 媒体流生命周期与停流权限 | §4.10.5 |
| 端点剖面安全要求 | §4.10.6 |
| 建议默认端口（`control` / UI / `stream`） | §7.2 |


### A.8 Bonjour 索引


| 条目 | 定义位置 |
| --- | --- |
| 服务类型 `_bmahs._tcp`、域 `local.`、实例名与端口来源 | §3.2 |
| TXT 键全集及其与 UDP 字段的逐项对照 | §6.4 |
| 系统侧自检命令 | §6.4 |

