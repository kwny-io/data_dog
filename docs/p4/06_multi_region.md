# P4-6 跨地域多活（异步，RPO>0）

> **建议缓做**：ROI 最低——当前无跨地域需求，是大工程换小展示价值。**设计给全**，
> 排在本批最后。

## 1. 目标

多地域部署：区域就近接入、区域内可独立服务、区域间**异步**复制与冲突消解。
**不追求 RPO=0**（那需数据库同步复制设施，非代码可解）。

## 2. 现状（已核实）

`src/data_dog` 内**无** region/replica/replication 代码；`slo.md:117` 列为非目标。

## 3. 设计

### 3.1 拓扑：单元化 + 异步复制

```
        ┌── Region A (主) ──┐        ┌── Region B (从) ──┐
        │  api + pg + redis │◄──复制──►│  api + pg + redis │
        │  本地读写          │  单向/双向 │  本地读写          │
        └───────────────────┘        └───────────────────┘
              ▲                             ▲
              │ 就近接入                     │
          车队(A 区设备)                 车队(B 区设备)
```

- **单元化（cell）**：每区域一套完整栈（api/pg/redis），**本地读写**，无跨区同步调用——
  跨区调用会毁掉延迟与可用性。
- **数据边界**：优先**按租户/车队就近归属**（改设备归属比改算法容易）——避免同一条数据
  跨区并发写。这是**最省事的冲突规避**。
- **复制**：PG 逻辑复制（`pglogical`/原生 pub/sub）或 CDC（Debezium）单向/双向同步。

### 3.2 冲突消解（仅当必须双向写同一条数据）

- **确定性 ID 是天然优势**：`cand_<hash>` / `event_id` / 幂等键**全局确定** →
  两区算出同 ID = 同一条记录，复制即收敛（LWW 或"先到先得"都安全）。
- 需要消解的是**可变状态**（如 alert 状态机、ack）：
  - 用**版本向量 / revision**（`alerting` 已有 `revision` 递增语义）判定因果序；
  - 无因果关系（真并发）→ 按策略：告警状态取**更严重**、ack 取**先到**。

### 3.3 复制延迟与一致性

- **RPO > 0**：异步复制下，主区故障可能丢最后几秒（复制延迟内）的数据。**如实声明**。
- **读己之写**：用户写 A 区就在 A 区读（sticky）；跨区读可能读到旧值 → UI 需标注
  "最终一致"。
- 复制延迟**暴露为指标**（`dd_replication_lag_seconds{from,to}`），超限告警。

### 3.4 故障切换

- **区域级**：DNS/Anycast 把流量切到健康区；**手动或半自动**（自动切换需防脑裂）。
- **防脑裂**：主选举走外部共识（etcd/Consul），**不用自研**。
- 演练：`make dr-drill` 模拟区域故障的 runbook。

## 4. 要建/要改的文件

| 文件 | 动作 |
|---|---|
| `helm/data-dog/values.<region>.yaml` | 每区域一份 values（P4-4 复用） |
| `deploy/replication/*.yaml` | PG 逻辑复制 / CDC 配置 |
| `src/data_dog/settings.py` | 加 `DD_REGION`、`DD_REPLICATION_SOURCE` |
| `src/data_dog/core/metrics.py` | 加复制延迟指标 |
| `docs/p4/06_runbook.md` | **新建**：故障切换/回切/演练 runbook |
| `tests/test_conflict_resolution.py` | **新建**：确定性 ID 收敛 + 状态消解 |

## 5. 验收

1. **确定性 ID 收敛**：两区各自算出同一条候选/事件 → 复制后**不产生重复行**。
2. **冲突消解**：并发改同 alert 状态 → 按策略收敛，结果确定（可重放验证）。
3. **就近接入**：A 区设备写 A 区，延迟不跨区。
4. **复制延迟指标**可见且有告警阈值。
5. **故障演练**：切到 B 区，服务可用；**记录丢了多少数据**（验证 RPO>0 的真实值）。
6. **脑裂防护**：模拟网络分区 → 不产生双主同时接受写。

## 6. 诚实边界（本项边界最多，务必写清）

- **RPO > 0**：异步复制的本质。要 RPO=0 需**数据库同步复制设施**（如 PG synchronous_commit
  + 多数派），会牺牲可用性与延迟，且**非应用代码可解**。
- **不做自动脑裂恢复**：主选举依赖外部共识，自研必错。
- **跨区一致性最终一致**：不宣称强一致。
- **成本**：多区 = 多份基础设施；ROI 需真实跨区需求支撑。
- **本项工程量大**：建议在 P4-4/P4-5 稳定运行、且**确有跨区需求**后再启动。

## 7. 依赖

- **P4-4 K8s**（每区一套编排）、**P4-5 分片**（区域≈粗粒度分片）。
- 确定性 ID 派生（已有）是复制的**天然收敛基础**——这是既有设计的红利。

---

## 8. 实现与验收（已落地 —— 仅应用层可解部分）

> **范围声明**：P4-6 是**大工程**，其验收项**大多需要真实多区基础设施**（多套 PG、
> DNS/Anycast、etcd/Consul）。本机（单区）**只实现应用层可解的部分**，其余如实
> 标注「本机做不了」——**不假装已测**。这是与本项目「诚实边界」一致的取舍。

### 8.1 落地的文件

| 文件 | 动作 | 说明 |
|---|---|---|
| `src/data_dog/replication/__init__.py` | **新建** | 包导出 |
| `src/data_dog/replication/conflict.py` | **新建** | `resolve_two` / `merge_alert_state` / `resolve_severity` / `Conflict`（**纯函数**，顺序无关） |
| `src/data_dog/settings.py` | 改 | 加 `DD_REGION` / `DD_REPLICATION_SOURCE` / `DD_REPLICATION_LAG_WARN_S` |
| `src/data_dog/core/metrics.py` | 改 | 加 `replication_lag_seconds{from,to}` / `replication_conflicts_total{path}` + observer |
| `tests/test_conflict_resolution.py` | **新建** | 17 条 |
| `docs/p4/06_runbook.md` | **新建** | 切换/回切/演练/分歧排查 runbook |
| `helm/data-dog/values.region.yaml` | **新建** | 区域覆盖**模板**（一份复制成多份，只改 region/source） |
| `deploy/replication/README.md` + `pglogical-pubsub.yaml.example` | **新建** | 复制链路脚手架（**不可本机验证**） |

### 8.2 核心：消解必须是**纯函数**

跨区域异步双向复制要对，只有一条判据：**两区域最终收敛到同一结果**。故
`resolve_two` 被设计成**纯函数**并钉住三条代数性质：

| 性质 | 含义 | 为何必须 |
|---|---|---|
| **交换律** | `resolve_two(a,b) == resolve_two(b,a)` | 复制顺序/方向不影响结果 |
| **结合律** | N 路折叠与折叠次序无关 | 复制批次切分不影响结果 |
| **幂等** | 重复合并不改结果 | 至少一次投递重放安全 |

**踩过的坑**：初版 `resolve_severity` 在 rank **并列**时返回**第一个参数**（`a`），
于是 `resolve_severity("warning","bogus")` 与 `("bogus","warning")` 结果不同——
**恰好破坏了消解最核心的交换律**。一个"看起来对"的严重度比较，会静默让两区永远分歧。
修法：半径并列时**归一**到已知词（`bogus` → `info`），并在 state 持有者选择上引入
**确定的**次级判据（state 字典序）。测试 `test_commutative` / `test_associative_over_n_way_fold`
钉住。

### 8.3 消解策略（§3.2 的落地）

| 情形 | 策略 | 理由 |
|---|---|---|
| `revision` 不等 | 高者胜（**因果序**） | 高 revision 是低 revision 的后继，取"最新已知" |
| `revision` 相等（真并发） | severity 取**更严重** | 乐观偏安全：宁多报重报，不漏报重症 |
| ack（任意 revision） | 取**先到**，且**不可逆** | ack 是"真人接手"的事实，不该被复制抹回未确认 |

> **确定性 ID 让大部分数据无需消解**：`event_id` 由三要素 `sha256` 派生（`alerting/outbox.py`），
> 两区对同一 Episode 算出**同一个** ID → 复制后是同一条记录，天然收敛。需消解的只有
> **可变状态**（alert 状态机 / severity / ack）。

### 8.4 验收对照（§5 六条）

| # | 验收项 | 怎么验 | 状态 |
|---|---|---|---|
| 1 | 确定性 ID 收敛 | `test_same_episode_same_event_id_across_regions` / `test_different_episode_different_event_id` | ✅ |
| 2 | 冲突消解确定、可重放 | `test_commutative` / `test_idempotent` / `test_associative_over_n_way_fold` | ✅ |
| 3 | 就近接入（延迟不跨区） | 需**区域化网络**——本机单区**无法验证** | ➖ **本机做不了** |
| 4 | 复制延迟指标可见 + 告警阈值 | 指标 `replication_lag_seconds` + 阈值 `DD_REPLICATION_LAG_WARN_S` 已落地；**端到端可见性**需多区 exporter | 🟡 指标/阈值已落地，采集链路未验 |
| 5 | 故障演练（记录 RPO） | 需**多区 + DNS 切换**——本机**无法演练**（runbook §5 已写步骤） | ➖ **本机做不了** |
| 6 | 脑裂防护 | 依赖**外部共识（etcd/Consul）** ——本机**无法验证** | ➖ **本机做不了** |

> **诚实标注**：6 条里 **1 条真验（✅）**、**1 条部分（🟡）**、**3 条本机做不了（➖）**。
> 消解逻辑是**纯函数**，可离线完整验证；但"就近接入 / 演练 / 脑裂"是**基础设施**问题，
> 写代码解决不了，如实保留为**未验证**。

### 8.5 诚实边界（补充 §6）

- **本机只验了"消解逻辑"**：`resolve_two` 的正确性（纯函数、收敛、可重放）是**代码可证**的；
  但它在**真实复制链路**下的表现（乱序到达、部分复制、网络分区）**未被本机验证**。
- **复制链路不可本机验证**：`deploy/replication/*.yaml.example` 是**骨架**，需真实多区
  PG 才能 apply。落地前必须先建多区环境并按 runbook 演练。
- **RPO > 0 未实测**：runbook §2 的"记录丢了多少秒"需**真实演练**才能填出数字；
  本机**没有**这个数字，故**不报**。
- **不声称 exactly-once / 强一致**：跨区最终一致；RPO 与 RTO 数字待真实演练实测。
- **ROI 提示**：本项工程量大（多套基础设施），建议**确有跨区需求**后再启动——
  当前实现是"逻辑就绪 + 脚手架就位"，不是"可上生产"。
