# P2 在线数据面

> 把架构蓝图 §9「日志到候选异常」的高频在线流程做成**本地可跑的最小闭环**。
> 前置：P0 契约（`../schemas/`）+ P1 算子（`../docs/operator_factory.md`）。

---

## 1. 一句话

**源源不断的原始日志 → 少量、结构化、带证据的候选异常**，热路径**不碰大模型**。

```bash
python -m data_dog.data_plane.p2_demo          # 人读输出
python -m data_dog.data_plane.p2_demo --json   # 机器可读
pytest tests/test_data_plane.py -q             # 20 项
```

实测输出（节选）：
```
收到 6 条 | 绑定成功 4 | 失败关闭 2  {'resolve': 2}
候选异常：1 条
  ▸ cand_b654abdb3269  [critical] joint_motor.front_left_hip
     信号=temperature_high  规则=['FIELD_SANITY_TEMP_NONNEG','MOTOR_TEMP_PROTECT']  score=1.0
     证据 temperature: None → 97  (MOTOR_TEMP_PROTECT: ge 90.0)
```

---

## 2. 数据流（架构 §9.3）

```
raw envelope
  → ① Gateway（§9.1）      认证 + 注入身份 + 校验 + 分区 + 限流
  → ② Resolver（§9.2）     兼容绑定：vendor/model/firmware/protocol → 唯一算子
  → ③ Parser               私有字段 → 标准字段（执行 P1 的 FieldSpec 映射）
  → ④ Normalizer           单位/状态码规范化
  → ⑤ Enricher             工况 + 同工况基线
  → ⑥ Window               窗口特征（均值/斜率/极值）
  → ⑦ Detector             执行 P1 算子 → 命中
  → ⑧ CandidateBuilder     合并去重 + 证据引用 → CandidateEvent
```

---

## 3. 模块

| 文件 | 职责 |
|---|---|
| `events.py` | 事件模型：RawLogEnvelope(§12.1) / NormalizedTelemetryEvent(§12.2) |
| `gateway.py` | 网关：认证桩、**身份注入**（不信任正文）、校验、分区、令牌桶限流 |
| `resolver.py` | 兼容绑定：版本区间匹配；无匹配 `not_found`、多匹配 `ambiguous` |
| `chain.py` | Parser / Normalizer / StatusCodeNormalizer / Enricher / Window / Detector |
| `candidate.py` | 候选构建：同信号合并去重、severity 取最高、证据 |
| `fetch.py` | **SSRF 安全**：`validate_url` 受控引用校验（仅 http/https，拒绝内网/环回/保留）+ `safe_request` 安全取数（逐跳复验重定向、连接钉 IP） |
| `pipeline.py` | 端到端编排 |

### 3.4 最小可复制接入示例（照抄就能产出候选）

接入是最容易“看着文档做却得 0 候选”的地方，所以这里给**一份可复制、且被测试守着**的示例：
`tests/test_doc_ingest_example.py` 会从本文抽出下面这段 JSON 原样 POST 一遍，
要求 `ingested=1` **且真的产出候选**。改坏了文档 = CI 红（文档与代码同源）。

两个必知点：

1. **七个字段一个都不能少**：`event_id` / `occurred_at` / `received_at` / `sequence` /
   `vendor` / `model` / `firmware`。少了任何一个 → 200 但
   `partial:true` + `rejected_by_reason:{"missing_fields":1}`，**不报错、也永远没有候选**。
2. **指标在 `payload_inline` 里**（一段 JSON 字符串，不是对象）：解析器从它取温度/电流等量，
   再和知识包的阈值比。字段对但 `payload_inline` 里没有超阈值的量，依旧不会有候选。

身份**不在正文**，走请求头：`X-Tenant-Id` / `X-Device-Sn`（正文里同名一律忽略）。

<!-- dd-doc-sample: ingest-event -->
```json
{
  "event_id": "doc-example-1",
  "occurred_at": "2026-09-13T10:00:00Z",
  "received_at": "2026-09-13T10:00:00.100Z",
  "sequence": 1,
  "vendor": "unitree",
  "model": "go2",
  "firmware": "3.2.1",
  "protocol_version": "motor-log-v2",
  "component_hint": "joint_motor.front_left_hip",
  "payload_inline": "{\"temperature\": 96, \"current\": 12.0, \"mode\": 0}"
}
```
<!-- /dd-doc-sample: ingest-event -->

一条命令看效果（服务已在 `127.0.0.1:8000` 起来时）：把上面那段 JSON 存成 `event.json`，然后

```bash
curl -s -X POST http://127.0.0.1:8000/v1/telemetry \
  -H 'content-type: application/json' \
  -H 'X-Tenant-Id: tenant-demo' -H 'X-Device-Sn: device-go2-0001' \
  -d "{\"events\":[$(cat event.json)]}"
```

看到 `"candidates":[{...}]` 就说明链路通了（`temperature=96` 超过该关节的阈值）；
若得 0 条，看 `rejected_by_reason`：`missing_fields` = 字段不全，
`payload_not_object` / `payload_inline_not_string` = `payload_inline` 给成了对象而非字符串。

### 3.5 响应语义：`ok` / `partial` / `stats`（#102）

`POST /v1/telemetry` 是**批处理**接口，且限流做在批内**每个事件**上（`gateway.ingest`），
所以"HTTP 200"与"数据都落了库"是两件事：

| 字段 | 含义 | 常见误读 |
|---|---|---|
| `ok` | **请求被正常处理**（没抛异常、没触发失败关闭的 503） | 当成"全部事件已入库" |
| `partial` | 批内**有**事件被拒（等价于 `stats.rejected > 0`） | —— |
| `stats.ingested` | **收到**的事件数，**含被拒的**（`process_one` 入口就 `+= 1`） | 当成"成功接入数" |
| `stats.normalized` | 规范化成功、进入落库的条数 | 最接近"真正落库"的 stats 字段 |
| `stats.rejected` + `stats.rejected_by_reason` | 被拒条数与细分原因（`rate_limited` / `bad_encoding` / `too_large` / `resolve` / `no_parser` …） | 不看 → 静默丢数据 |
| `persisted.telemetry` / `persisted.candidates` | **同步落库**后的实际行数。write-behind 模式下换成 `{"mode": "write_behind", "queued": true}`，此时**不保证**已落库 | —— |

所以接入方该看的组合是 `persisted` 与 `partial`：

```python
body = post("/v1/telemetry", events).json()
if body["partial"]:
    log.warning("部分事件被拒", reasons=body["stats"]["rejected_by_reason"])
landed = body["persisted"]["telemetry"]      # write-behind 模式下看 queued，不看这个字段
```

反面例子（**稳态路径**，不是罕见情形）：配额耗尽时 100 条事件全被限流 →

```json
200 {"ok": true, "partial": true,
     "stats": {"ingested": 100, "rejected": 100, "normalized": 0,
               "rejected_by_reason": {"rate_limited": 100}},
     "persisted": {"telemetry": 0, "candidates": 0}, "candidates": []}
```

只看 `ok`（或只看 `ingested`）的实现会把这次**全丢**记成**全成**——后果是"几周后发现某段时间数据缺一段"，
而事后无法从 200 里回溯。

> 历史：`partial` 字段与本小节是缺陷 #102 的修复产物。在此之前 `rejected_by_reason` 在 README 与
> `docs/` 里零提及，而限流本身是设计上要丢数据的路径。

### 3.6 `rejected_by_reason` 全表：**谁的错**，以及能不能自动重试

> 单独成节的原因：§3.5 讲的是**响应的顶层字段**（`ok`/`partial`/`stats`/`persisted`），
> 由 `tests/test_web_api_contract.py` 强制与 TS 契约同源；reason 是 `stats.rejected_by_reason`
> 内部的键，不在顶层契约里。把两类东西放在同一节会让那个守卫把 reason 当成顶层字段
> （2026-10-24 真发生了：文档一改就报 `not_a_dict/payload_malformed/... 不在 IngestResult`）。
> 本节与代码的同源由另一个守卫把守：`test_every_emitted_reject_reason_is_documented`。

下表由代码枚举得出（`gateway.py` 的 `_reject(...)` 与 `pipeline.py` 的 `reason=`），
**不是设计意图清单**——它回答两个接入方真正关心的问题：这条该不该重试、该找谁。

| reason | 含义 | 该重试吗 | 谁的错 |
|---|---|---|---|
| `missing_fields` | 缺必需字段 | 否 | 接入方（报文缺字段） |
| `not_a_dict` | 单条事件不是对象 | 否 | 接入方 |
| `payload_inline_not_string` | `payload_inline` 传了数字/对象/数组（它必须是**字符串**） | 否 | 接入方 |
| `payload_malformed` | `payload_inline` 不是合法 JSON | 否 | 接入方 |
| `payload_not_object` | 是合法 JSON、但不是**对象**（如 `42`、`[1,2]`） | 否 | 接入方 |
| `bad_encoding` | 编码不在白名单 | 否 | 接入方 |
| `too_large` | 单条载荷超上限 | 否 | 接入方（改分批/截断） |
| `auth` | 认证失败或缺可信身份（`401`，但计入 `rejected_by_reason` 为 `auth`） | 否 | 接入方或凭据过期 |
| `device_signature_invalid` | 设备签名格式不对或校验不过 | 否 | 接入方（签错/重放） |
| `device_signature_missing` | 强制验签档却没带 `X-Device-Signature` | 否 | 接入方 |
| `device_signature_unknown` | 设备未配置签名密钥 | 否 | 平台方（漏配密钥） |
| `device_signing_misconfigured` | 验签配置写坏（**失败关闭**） | 否 | 平台方 |
| `payload_ref_denied` | 外置引用被 SSRF 策略拒 | 否 | 接入方（或平台策略过紧） |
| `rate_limited` | 设备配额耗尽 | **是（退避）** | 正常业务路径 |
| `resolve` | 兼容绑定解析失败（vendor/model/firmware/protocol 组合无绑定） | 否 | 平台方（缺绑定） |
| `no_parser` | 绑定存在但未挂 parser（`contract_mismatch`） | 否 | 平台方（配置缺一块） |

> 上表只覆盖 `POST /v1/telemetry` 的 `rejected_by_reason`（数据面拒绝）。
> 其它接口的 `reason` 是**另一套分类**，不要混用：`unauthorized`（无/错 `X-API-Key`，
> HTTP 401）、`tenant_keys_misconfigured`（租户密钥配置坏，HTTP 500）、
> `change_source_unavailable`（证据数据源不可用，证据解释接口）。

**两个实现教训写在这里，防止再犯**（均于 2026-10-24 由敌意输入测试
`tests/test_telemetry_hostile_input.py` 抓出，提交 `9495320`）：

- 合法 JSON **不等于**合法载荷：`42` / `[1,2]` / `"x"` / `true` 都能通过
  `json.loads`，但下文全部按 mapping 用。放过它们等于让一条坏事件把**整批**打成 500。
- “不会是这个类型”是假设，不是事实：`payload_inline` 被当文本用（`.encode()`、
  `json.loads`），就必须先卡它是 `str`——否则 `AttributeError` 直接变 500。

---

## 4. 三个关键设计（面试能讲）

1. **身份只在网关注入**：正文里的 `tenant_id/device_sn` **一律丢弃**，以认证上下文为准——对齐 §9.1"不信任日志正文中的身份字段"，与 Agent 侧的 `RuntimeContext` 同一原则。
2. **拿不准就失败关闭**：兼容绑定匹配不到 → `not_found`；匹配多个 → `ambiguous`；认证失败 → `denied`。**绝不猜测、绝不降级为无鉴权**（§9.2/§13.4）。
3. **热路径不调大模型**：③~⑦ 全是确定性算子；模型挂了它们照跑（§9.4 降级）。

---

## 5. 安全：受控引用 / SSRF 防护

大载荷经 `payload_ref` 外置，取数前必须过 `fetch.validate_url`：

- **仅允许 http/https**（拒绝 `file://` 等）；
- **拒绝 localhost / 环回 / 私有 / 链接本地 / 保留 / 组播地址**；
- 解析域名后校验**所有** IP（防 DNS rebinding）；
- 端口白名单、大小上限、失败关闭。

### 5.1 安全取数原语 `safe_request`（**取数必须用它，不要用裸 `urlopen`**）

`validate_url` 只回答"这个 URL 能不能请求"，它既看不见 302 的目标，也管不住"校验之后
DNS 改口"。真正的取数走 `fetch.safe_request`：

| 缺口 | 做法 |
|---|---|
| `urlopen` 默认跟随 3xx 且不看目标（"公网域名 → 302 → `169.254.169.254`" 可读云凭据） | **不自动跟随**：每一跳重跑结构检查 + IP 检查，任一跳不过 → 整体失败且不再往下发；跳数上限 3 |
| 校验与建连之间**二次解析**（DNS 重绑定 TOCTOU） | **把连接钉在校验出的 `resolved_ips[0]`**：`http.client` 连那个 IP，不再解析；TLS 的 SNI/证书校验仍按**主机名**（否则要么验不过、要么只能关校验） |
| 响应体无上限 | `max_bytes`（默认 64 KiB）截断 |
| 失败语义散乱 | 一律返回 `SafeResponse(ok=False, error=...)`，不抛异常 |

透明性：3xx 跟随 `max_redirects`（默认 3）跳；`301/302/303` 按浏览器语义降级为 GET（丢 body），
`307/308` 保留方法与 body。`allow_private=True` 是**显式信任放宽**（对应
`DD_NOTIFY_ALLOW_PRIVATE=1`）：只跳过"解析结果必须是公网"，结构检查与钉住照旧，**仅限本机/受信内网**。

> 调用方已全部切换：`alerting/notifiers.py::_post_json`、`evidence/change_source.py::_post`。
> 回归见 `tests/test_http_safe.py`（12 条，离线）与 `tests/test_ssrf_adversarial.py`。

> 注意：若本机开着代理（fake-IP 模式），公网域名可能被解析到保留段 `198.18.x.x` → 会被严格拒。这是**预期的安全行为**，不是 bug。

---

## 6. 流式接入（真实遥测源）

HTTP `POST /v1/telemetry` 是**请求隔离**入口——每请求新建 plane。真实设备持续吐数据时，
走**长驻**流式消费者：只建**一次** plane 复用，让窗口缓冲跨消息累积（连续事件才能正确
并入同一条候选）。四类源统一实现 `StreamSource.poll(timeout) -> list[StreamMessage]`：

| 源 | 实现 | 依赖 | 身份来源 |
|---|---|---|---|
| `file` | 文件/日志尾随（`file.py`） | 标准库 | 配置（`DD_STREAM_TENANT` / `DD_STREAM_DEVICE_SN`，或正文取值字段） |
| `http` | 推送（`http.py`，内建 `ThreadingHTTPServer`） | 标准库 | 请求头 `X-Tenant-Id` / `X-Device-Sn`（正文身份一律丢弃） |
| `mqtt` | 订阅（`bus.py`） | `.[mqtt]`（paho-mqtt） | 订阅配置 + 载荷取值字段 |
| `kafka` | 消费者（`bus.py`） | `.[kafka]`（kafka-python） | 同上 |

**启用**（默认关，演示/CI 无真实设备）：

```bash
DD_STREAM_ENABLED=1 DD_STREAM_SOURCE=file DD_STREAM_FILE_PATH=/var/log/robot.log \
  DD_STREAM_TENANT=demo DD_STREAM_DEVICE_SN=go2-001 python -m data_dog.service
```

启动后 `/readyz` 会报告流式消费者健康（未启用/线程死亡 → not ready），`/metrics`
暴露 `data_dog_stream_messages_total{source}`、`..._candidates_total`、`..._dropped_total`、`data_dog_stream_up`。

**实现要点**
- **身份只从通道注入**（配置或请求头），载荷自称的 tenant/device **一律丢弃**——与 §9.1 同原则。
- **按租户分组落库**：一个流可承载多租户，`StreamRunner` 先按 `auth.tenant_id` 分桶，
  逐租户 `build()`（只产出本租户刚触碰的分组）+ 用该租户身份落库，候选不会张冠李戴。
- **候选归并在共享后端**（`SharedCandidateBuilder`）：多副本同一条设备事件合并一致，
  `candidate_id` 含租户前缀，跨租户不撞（见 `tests/test_stream.py`）。
- **低流量可见性**：源安静且暂存非空 → 立即 flush，不必凑满 `flush_max`（否则"收到消息却
  查不到候选"）。file 源处理坏行（跳过不中断）/半行（等补全）/截断轮转（从头读）。
- **背压**：推式源（http/mqtt）队列满即丢弃并计数（`dropped`），不无限积压。

> **诚实边界**：**file / MQTT 源多副本会重复消费**（无消费者组语义）——要水平扩展用
> Kafka 消费者组或 MQTT 共享订阅。file 源只跟**单个**文件（按最新 mtime 选），不支持跨副本协调。

---

## 7. 与 Agent 主线的关系

候选异常（`CandidateEvent`）**直接喂给已有 Agent 调查线**（`agent/loop.py`）——与 P1"换算子不改 Agent"、MuJoCo"换数据源不改 Agent"是同一个解耦原则。整条链打通后：

```
厂商文档 →[P1]→ 算子 →[P2]→ 遥测/候选 →[已有]→ Agent 调查 →[P3]→ 策略门 → SaaS
```

---

## 8. 与架构条目对照

| 架构 | 状态 |
|---|---|
| §9.1 接入与缓冲（认证/注入/校验/限流/分区） | ✅ 网关 + 四类流式源（file/http/mqtt/kafka）+ 背压丢弃计数；跨副本协调未做 |
| §9.2 兼容性解析（唯一匹配、失败关闭） | ✅ |
| §9.3 在线处理链（Parser→…→Candidate Builder） | ✅ 最小版 |
| §9.4 Agent 不可用降级 | 🟡 热路径不依赖模型✅；候选队列/重放待补 |
| §16 有状态算子外置（水平扩展） | ✅ 状态外置到 `StateStore`（memory/pg/redis）；checkpoint/恢复未做 |
| §16.2 事件时间/水位线/迟到 | ❌ 未做（窗口当前按到达顺序） |
