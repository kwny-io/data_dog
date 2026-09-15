# P2 在线数据面

> 把架构蓝图 §9「日志到候选异常」的高频在线流程做成**本地可跑的最小闭环**。
> 前置：P0 契约（`../schemas/`）+ P1 算子（`../docs/operator_factory.md`）。

---

## 1. 一句话

**源源不断的原始日志 → 少量、结构化、带证据的候选异常**，热路径**不碰大模型**。

```bash
python -m data_dog.data_plane.p2_demo          # 人读输出
python -m data_dog.data_plane.p2_demo --json   # 机器可读
pytest tests/test_data_plane.py -q             # 18 项
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

> 注意：若本机开着代理（fake-IP 模式），公网域名可能被解析到保留段 `198.18.x.x` → 会被严格拒。这是**预期的安全行为**，不是 bug。

---

## 6. 与 Agent 主线的关系

候选异常（`CandidateEvent`）**直接喂给已有 Agent 调查线**（`agent/loop.py`）——与 P1"换算子不改 Agent"、MuJoCo"换数据源不改 Agent"是同一个解耦原则。整条链打通后：

```
厂商文档 →[P1]→ 算子 →[P2]→ 遥测/候选 →[已有]→ Agent 调查 →[P3]→ 策略门 → SaaS
```

---

## 7. 与架构条目对照

| 架构 | 状态 |
|---|---|
| §9.1 接入与缓冲（认证/注入/校验/限流/分区） | 🟡 网关最小版✅；断线重传/背压待补 |
| §9.2 兼容性解析（唯一匹配、失败关闭） | ✅ |
| §9.3 在线处理链（Parser→…→Candidate Builder） | ✅ 最小版 |
| §9.4 Agent 不可用降级 | 🟡 热路径不依赖模型✅；候选队列/重放待补 |
| §16 有状态算子 checkpoint | ❌ 未做（缺口 E-4，下一步） |
| §16.2 事件时间/水位线/迟到 | ❌ 未做（窗口当前按到达顺序） |
