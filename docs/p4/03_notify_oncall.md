# P4-3 通知 / 值班 / 升级 SLA 引擎

## 1. 目标

告警闭环现在**止步于测试 SaaS adapter**。本项补最后一公里：**真人能收到、没人理会上自动升级**。
这才是"真能值班"的定义。

## 2. 现状（已核实）

| 已有 | 位置 | 说明 |
|---|---|---|
| 幂等出箱 | `alerting/outbox.py` | `AlertOutbox.enqueue/deliver`；状态 `pending/delivered/failed/dead`；3 次重试 |
| 上报服务 | `alerting/service.py` | `AlertReportingService.process` → `outbox.deliver(adapter)` |
| 策略门 | `alerting/policy.py` | `ReportPolicyGate`（确定性决策表） |
| 告警生命周期 | `alerting/lifecycle.py` | `AlertLifecycle`；**已含 `escalated` 状态** |
| 投递适配器 | `alerting/saas.py` | `SaaSTestAdapter.deliver` —— **测试替身（mock）** |

**缺**：真实 notifier（webhook/邮件/IM）；值班表；升级 SLA 计时器；`/v1/outbox` 只查询不投递。

## 3. 设计

### 3.1 通知适配器（可插拔，同 `SaaSTestAdapter` 的接口）

```python
# alerting/notifiers.py（新建）
class Notifier(Protocol):
    name: str
    def send(self, alert: AlertEvent, *, route: Route) -> DeliveryResult: ...

class WebhookNotifier:   # POST JSON 到 route.target（复用 fetch 的 SSRF 校验！）
class EmailNotifier:     # SMTP；目标来自 route.target
class IMNotifier:        # 飞书/钉钉/企微 群机器人 webhook（国内值班常用）
class NotifierRegistry:  # 按 route.channel 选 notifier
```

**复用安全件**：webhook 目标 URL **必须**过 `data_plane/fetch`——`validate_url` 做前置
快速失败，取数走 `safe_request`（**逐跳复验重定向 + 连接钉在校验过的 IP 上**，
`allow_private` 显式放行本机/受信内网）——别让"通知"变成 SSRF 入口。

### 3.2 数据模型（新表）

```sql
-- oncall_rota: 值班表（谁在什么时段值班，按领域）
CREATE TABLE oncall_rota (
    tenant_id TEXT NOT NULL, rota_id TEXT NOT NULL,
    domain TEXT NOT NULL,                 -- 如 joint_motor / bms
    level INT NOT NULL,                   -- 1=一线 2=二线 3=专家
    assignee TEXT NOT NULL,               -- 人或组
    start_at TIMESTAMPTZ NOT NULL, end_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, rota_id)
);
-- alert_delivery: 每次投递尝试（审计 + 升级计时）
CREATE TABLE alert_delivery (
    tenant_id TEXT NOT NULL, delivery_id TEXT NOT NULL,
    event_id TEXT NOT NULL, level INT NOT NULL,
    channel TEXT NOT NULL, target TEXT NOT NULL,
    status TEXT NOT NULL,                 -- pending/acked/escalated/failed
    sent_at TIMESTAMPTZ, acked_at TIMESTAMPTZ,
    ack_deadline TIMESTAMPTZ,             -- 超此未 ack → 升级
    PRIMARY KEY (tenant_id, delivery_id)
);
```

### 3.3 升级引擎

```
alert 产生
  → 查 oncall_rota(domain, now) 取 level=1 值班人
  → NotifierRegistry 按 channel 投递 → 落 alert_delivery(ack_deadline = now + SLA)
  → 后台 sweeper 周期扫：status=pending 且 now > ack_deadline
       → 升到 level+1（写 alert_delivery 新行，level 递增）
       → 复用 alerting/lifecycle 的 escalated 状态
  → 有人 ack（POST /v1/alerts/{id}/ack）→ status=acked，停止升级
```

- **SLA 可配**：`DD_NOTIFY_ACK_SLA_S`（默认 300s），按 level 可不同（`DD_NOTIFY_SLA_L2_S`…）。
- **升级链有限**：到最高 level 仍无 ack → `status=failed` + 记 `dead`（对齐 outbox 语义）。
- **幂等**：同 alert 同 level 不重复投递（`delivery_id = hash(event_id, level)`）。

### 3.4 API

```
POST /v1/alerts/{event_id}/ack     确认（停止升级）
GET  /v1/oncall                    当前值班表
PUT  /v1/oncall                    设置值班（{domain,level,assignee,start_at,end_at}）
GET  /v1/notifications             投递记录（审计）
```

## 4. 要建/要改的文件

| 文件 | 动作 |
|---|---|
| `src/data_dog/alerting/notifiers.py` | **新建**：Notifier 协议 + Webhook/Email/IM 实现 |
| `src/data_dog/alerting/oncall.py` | **新建**：值班表查询（按 domain+time 取值班人） |
| `src/data_dog/alerting/escalation.py` | **新建**：升级引擎（sweeper 循环 + ack 停止） |
| `src/data_dog/storage/models.py` | 加 2 表 |
| `alembic/versions/20260930_0810_a2_oncall.py` | **新建迁移**（含 RLS） |
| `src/data_dog/alerting/service.py` | 改：`process` 后接 notifier 投递 + 排 ack SLA |
| `src/data_dog/service/app.py` | 加 4 个端点（末尾新分节） |
| `src/data_dog/service/app.py` lifespan | 挂升级 sweeper 后台线程（对齐 stream runner 的挂载方式） |
| `src/data_dog/settings.py` | 加 SLA、SMTP、IM webhook 配置 |
| `tests/test_notifiers.py`、`tests/test_escalation.py` | **新建** |

## 5. 验收

1. **投递**：webhook notifier 对本地测试 server 真发 POST；目标过 SSRF 校验（内网 URL 被拒）。
2. **升级**：ack_deadline 到点未 ack → sweeper 升到 level+1，写新 `alert_delivery` 行。
3. **停止**：ack 后不再升级。
4. **幂等**：同 alert 同 level 只投一次。
5. **升级链尽头**：最高 level 无 ack → `failed`，不无限升级。
6. **租户隔离** + 缺头 401。
7. **零回归**：`test_alerting*`、`test_outbox*` 全绿。

## 6. 诚实边界

- **不做 7×24 运营**：提供值班表与升级机制，**排班是人**的事。
- **通知可靠性 = 适配器的可靠性**：webhook 失败按 outbox 重试语义处理（3 次→dead），
  **不保证必达**（要必达需接专业告警平台，属更大工程）。
- **IM/SMTP 凭据**走环境变量，不进仓库。
- sweeper 是**进程内后台线程**，但已加**单持有者租约**（`state/lease.py`，key
  `alert:escalation:sweeper`）：多副本时同一时刻只有一个副本在扫，不再重复升级。
  **为什么必须**：台账按 `delivery_id = hash(event_id, level)` 去重，而
  `_deliver` 是"**先 `registry.send()` 再 upsert 台账**"——去重挡的是台账，
  **通知已经发出去了**（值班人被重复打扰，P0 场景下会让人开始忽略告警）。
  TTL 实际取值 = `max(DD_NOTIFY_SWEEP_LEASE_TTL_S, 3 × 扫描间隔)`；租约不可用时
  **本轮不扫**（失败关闭：到期行还在库里，下轮接着扫，不丢计时）。
  `/readyz` 的 `checks.escalation` 会报出 `lease_holder` 与 `skipped_ticks`。
  **边界**：memory 状态后端不提供跨副本保证（多副本须用 postgres/redis）。

## 7. 依赖

无强依赖。可与 P4-2 并行（各占自己的表与端点分节）。
