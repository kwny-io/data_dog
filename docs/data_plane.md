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
| `fetch.py` | **SSRF 安全**的受控引用校验（仅 http/https，拒绝内网/环回/保留） |
| `pipeline.py` | 端到端编排 |

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

> **诚实边界**：`fetch.py` 是**纯校验模块，不含任何网络请求**；它校验解析出的所有 IP 均为公网，但**解析结果未 pin 到实际连接**——真正的取数由调用方执行。因此在"校验通过"与"建立连接"之间仍存在 DNS rebinding 的时间窗。**接入真实网络请求时，必须在连接层用已校验的 IP 重建连接（不二次解析域名）**，否则校验只是前置防线而非完备防护。

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
