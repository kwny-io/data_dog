# 真机接入指南（Real Device Integration）

> 面向：把一台**真实机器狗（如 Unitree Go2）**的遥测接到 data_dog，跑通
> **接入 → 候选异常 → Agent 调查**。
>
> 本文只讲"怎么接"。链路上游的数据面细节见 [`data_plane.md`](data_plane.md)，
> Agent 与策略门见 [`architecture.md`](architecture.md) / [`alerting.md`](alerting.md)。

---

## 0. 先建立一个关键认知：真机不走 `/v1/telemetry`

项目有**两种**接入方式，用途不同，别搞混：

| 方式 | 端点 / 入口 | 谁用 | 管线生命周期 |
|---|---|---|---|
| **请求隔离** | `POST /v1/telemetry` | 测试、一次性推送、curl 联调 | **每请求新建一次** plane |
| **长驻流式** ★ | 启动时开启后台消费者 | **真实设备** | **只建一次** plane，跨消息累积窗口 |

**为什么真机必须走流式**：机器狗是**连续吐数据**的。`/v1/telemetry` 每个请求都重建
处理管线，窗口缓冲无法跨请求累积——**连续事件串不成同一条候选，候选根本攒不出来**。
流式消费者只建**一次** plane 复用，窗口状态跨消息保留（见
[`data_plane.md`](data_plane.md) §6 与 `src/data_dog/stream/runner.py` 头部注释）。

> 一句话：**一次性 push 用 `/v1/telemetry`；持续吐数据用流式源。**

---

## 1. 四扇门：选哪一扇由"机器人怎么吐数据"决定

流式源由 `DD_STREAM_SOURCE` 决定（见 `src/data_dog/stream/factory.py`）：

| `DD_STREAM_SOURCE` | 场景 | 额外依赖 | 身份来源 | 多副本 |
|---|---|---|---|---|
| **`file`** | 机器人本机写日志文件，我们 `tail -f` | 无（标准库） | 配置，或每行的 `device_from_field` | ❌ 单副本（租约选主） |
| **`http`** | 机器人只会往一个 URL **POST**（webhook） | 无（标准库） | 请求头 `X-Tenant-Id` / `X-Device-Sn` | ❌ 单副本 |
| **`mqtt`** | 机器人发 MQTT | `pip install -e .[mqtt]`（paho-mqtt） | 订阅配置 | ✅ 共享订阅 |
| **`kafka`** | 机器人/上游有 Kafka | `pip install -e .[kafka]`（kafka-python） | 消费者组配置 | ✅ 消费者组 |

**选法**：
- 机器人能**存日志文件** → 用 `file`（最简单，零依赖）。
- 机器人能**发 HTTP POST** → 用 `http`。
- 机器人**发 MQTT** → 用 `mqtt`（Go2 类设备常见；需装 paho-mqtt）。
- 上游**已有 Kafka** → 用 `kafka`。

> 先看机器人说明书/SDK 确认它的**数据出口**，再回来对号入座。

---

## 2. 启动命令

服务入口：`python -m data_dog.service`（读 `DD_SERVICE_HOST` / `DD_SERVICE_PORT`，
见 `src/data_dog/service/__main__.py`）。流式默认**关闭**，需显式开。

**通用开关**：`DD_STREAM_ENABLED=1` 才启用流式消费者。

### file 源（最常用）

```bash
DD_STREAM_ENABLED=1 \
DD_STREAM_SOURCE=file \
DD_STREAM_FILE_PATH=/var/log/robot.log \
DD_STREAM_TENANT=demo \
DD_STREAM_DEVICE_SN=go2-001 \
python -m data_dog.service
```

- `DD_STREAM_FILE_PATH`：**必填**，尾随的日志文件（支持通配，按最新 mtime 选，
  见 `stream/file.py:resolve_watch_path`）。
- `DD_STREAM_DEVICE_SN`：设备号。**同一文件承载多台设备**时，改设
  `DD_STREAM_DEVICE_FIELD=<正文里的字段名>`，从每行正文取设备号（`tenant_id`
  仍只认配置，**绝不读正文**）。

### http 源（设备只会 POST）

```bash
DD_STREAM_ENABLED=1 \
DD_STREAM_SOURCE=http \
DD_STREAM_HTTP_HOST=0.0.0.0 \
DD_STREAM_HTTP_PORT=8099 \
DD_STREAM_HTTP_PATH=/ingest \
DD_STREAM_TENANT=demo \
python -m data_dog.service
```

设备侧往 `http://<你的IP>:8099/ingest` POST 即可，请求头带身份：

```
X-Tenant-Id: demo
X-Device-Sn: go2-001
```

- 若设备固件**不带** `X-Device-Sn`，设 `DD_STREAM_DEVICE_FIELD=<正文字段名>` 从正文取
  （`tenant_id` 仍只认头）。见 `stream/http.py` 头部"安全边界"。
- **诚实边界**：这是**极简接入服务，无 TLS、无鉴权**——生产必须放在反向代理/网关之后，
  或直接用主服务的 `/v1/telemetry`（已有 API key + 租户头校验）。

### mqtt / kafka 源

```bash
# MQTT
DD_STREAM_ENABLED=1 DD_STREAM_SOURCE=mqtt \
DD_STREAM_MQTT_URL=tcp://<broker>:1883 \
DD_STREAM_MQTT_TOPIC='telemetry/#' \
DD_STREAM_TENANT=demo DD_STREAM_DEVICE_SN=go2-001 \
python -m data_dog.service

# Kafka
DD_STREAM_ENABLED=1 DD_STREAM_SOURCE=kafka \
DD_STREAM_KAFKA_BOOTSTRAP=<broker>:9092 \
DD_STREAM_KAFKA_TOPIC=telemetry \
DD_STREAM_KAFKA_GROUP=data-dog \
DD_STREAM_TENANT=demo \
python -m data_dog.service
```

> Windows 上环境变量**逐条设置**更稳（`$env:DD_STREAM_ENABLED="1"` 等），或写进
> `.env`。多行 `\` 续行只在 bash 有效。

---

## 3. 真正要干的活：把机器人格式"翻译"成 data_dog 格式

**这是接入真机唯一需要新写的东西。** 机器人吐的字段名/单位/结构与你系统要的不一样，
需要一个**转换器**。系统接收的每条原始日志，**必须**满足下列字段（见
`src/data_dog/data_plane/gateway.py:24` 与 `events.py:RawLogEnvelope`）：

### 必需字段（缺一 → 拒绝，失败关闭）

| 字段 | 含义 | 示例 |
|---|---|---|
| `event_id` | 本条唯一 ID | `"e-0001"` |
| `occurred_at` | 设备事件时间（ISO8601） | `"2026-10-10T09:55:00Z"` |
| `received_at` | 网关接收时间 | `"2026-10-10T09:55:01Z"` |
| `sequence` | 设备单调序列号（整数） | `1001` |
| `vendor` | 厂商 | `"unitree"` |
| `model` | 型号 | `"go2"` |
| `firmware` | 固件版本 | `"1.0.3"` |

### 可选字段

| 字段 | 含义 |
|---|---|
| `component_hint` | 部件提示（**非权威**），如 `joint_motor.front_left_hip` |
| `payload_inline` | 小载荷直接内联（≤ 64 KiB，见 `gateway.py:_MAX_PAYLOAD_BYTES`） |
| `payload_ref` / `payload_digest` | 大载荷走外置引用（`ref://` + sha256） |
| `encoding` | 白名单：`utf-8` / `utf8` / `ascii` / `hex` / `base64`（默认 utf-8） |

> **身份不放在正文里。** `tenant_id` / `device_sn` 由**信道注入**（流式配置或请求头），
> 正文里即使有同名字段也**一律被丢弃**——这是刻意的安全设计，不是 bug。

### 翻译器做什么

```
机器人原始行  →  [你的翻译器]  →  上面这个结构  →  喂给流式源
```

**翻译器可以很薄**：字段改名、单位换算、补 `event_id`/`received_at`。真正的遥测
指标（关节温度、电流…）放进 `payload_inline`。

---

## 4. 不等真机也能先跑通：用假日志验证

> **本节的完整链路已实测跑通**（见 §4.4 验证记录）。两个配套脚本已放在 `scripts/`。

真机到家前，先"假装"数据源把管道验通：

1. 写一行符合 §3 的假日志，存成文件：

```json
{"event_id":"e-0001","occurred_at":"2026-10-10T09:55:00Z","received_at":"2026-10-10T09:55:01Z","sequence":1,"vendor":"unitree","model":"go2","firmware":"1.0.3","component_hint":"joint_motor.front_left_hip","payload_inline":"{\"temp_c\":78.5,\"current_a\":4.2}"}
```

2. 用 `file` 源指向它启动（§2 的 file 命令，`DD_STREAM_FILE_PATH` 指向该文件）。
   **注意**：file 源默认**从文件末尾开始读**（`start_at_end=True`）——**先启动服务，
   再往文件里追加行**，否则会漏掉已存在的行。

3. 看健康与候选：
   - `GET /readyz` —— 流式消费者是否健康（未启用/线程死亡 → not ready）
   - `GET /metrics` —— `data_dog_stream_messages_total{source}`、
     `..._candidates_total`、`..._dropped_total`、`data_dog_stream_up`

**跑通后，真机到了只需把假文件换成真日志（或换源）——其余不用动。**

### 4.4 两个配套脚本

| 脚本 | 作用 |
|---|---|
| `scripts/make_fake_go2_log.py` | 生成假 Go2 日志（温度爬升越过 90°C 阈值）。`--follow` 可模拟持续吐数据 |
| `scripts/to_data_dog.py` | **机器人原始行 → data_dog 格式的转换器**（你接真机唯一要改的文件：只改 `map_row()`） |

### 4.5 端到端验证记录（2026-10-10 实测）

在 Windows 本机跑通（服务端口 8125，file 源）：

```bash
# 1. 先起服务（file 源从文件末尾读，必须"先起服务再追加"）
DD_STREAM_ENABLED=1 DD_STREAM_SOURCE=file \
DD_STREAM_FILE_PATH="D:/tmp/e2e.log" \
DD_STREAM_TENANT=demo DD_STREAM_DEVICE_SN=go2-001 \
DD_SERVICE_PORT=8125 python -m data_dog.service

# 2. 追加假日志（20 条，seq≥13 越过 90°C）
python scripts/make_fake_go2_log.py --lines 20 --out D:/tmp/e2e.log

# 3. 看结果
curl -s localhost:8125/readyz            # → stream: ok, messages/normalized/candidates
curl -s localhost:8125/v1/candidates -H "X-Tenant-Id: demo"
```

**实测结果**：`messages=35, normalized=35, candidates=1`，产出候选：

```json
{"candidate_id":"cand_...","component_ref":"joint_motor.front_left_hip",
 "signal":"temperature_high","severity_hint":"critical",
 "rule_matches":["MOTOR_TEMP_PROTECT"],
 "evidence":[{"key":"temperature","to_value":125,"note":"MOTOR_TEMP_PROTECT: ge 90.0"}]}
```

即：**假日志 → file 源 → 数据面 → 候选异常** 全链路已通，候选人头到尾没改一行。

> **踩过的坑（帮你省时间）**：
> 1. file 源**必须先起服务再追加日志**，否则已存在的行会被跳过（从末尾读）。
> 2. Windows 上 `DD_STREAM_FILE_PATH` 用 **`D:/tmp/x.log` 这类绝对路径**；`/tmp/` 会被
>    Python 解析成 `D:\tmp\`，路径不存在则 file 源起不来（`/readyz` 报"已启用但未启动"）。
> 3. 温度必须**越过 90°C** 阈值才会出候选；只在 80 多度跑不出候选。

---

## 5. 排错对照

| 现象 | 可能原因 |
|---|---|
| `/readyz` not ready | 流式未启用（没设 `DD_STREAM_ENABLED=1`），或线程已死（看服务日志） |
| 消息计数涨、候选恒为 0 | 数据格式不对（字段缺失/指标名不匹配）→ 看 `..._dropped_total` 与 rejected 原因 |
| 一条日志都没读到（file） | 忘了"先启动再追加"；或路径通配没匹配到文件 |
| `413` / 载荷过大 | `payload_inline` 超 64 KiB → 改走 `payload_ref` |
| 启动即报错 `不支持多副本` | `DD_STREAM_SHARDS>1` 但源是 file/http（无协调）→ 设回 `1` 或改 kafka/mqtt |
| MQTT/Kafka 导入失败 | 没装 extras：`pip install -e .[mqtt]` / `.[kafka]` |

---

## 6. 下一步

接入跑通后：

- 候选异常怎么产生 → [`data_plane.md`](data_plane.md)
- 候选如何进入 Agent 调查 → [`architecture.md`](architecture.md) §13
- 调查结论如何过策略门、如何上报 → [`alerting.md`](alerting.md)

---

## 诚实边界

- **未做**：本指南的每条命令均以**代码/配置为准**核过，但**尚未在真实 Go2 上端到端验证**
  ——目前项目所有验证基于合成数据与 MuJoCo 仿真。真机接入属于首次真实验证。
- `file` / `http` 源**无多副本协调**，多副本会重复消费；要水平扩展用 `kafka` / `mqtt`。
- `http` 流式源**无 TLS/鉴权**，仅用于接入联调；生产请置于网关之后或用主服务 `/v1/telemetry`。
