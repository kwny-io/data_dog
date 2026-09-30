# G3 StateStore checkpoint / 恢复

## 1. 目标

状态已**外置**（水平扩展基础），但**无检查点/恢复**语义——进程重启或状态淘汰后，
无法确认/重建"上次算到哪"。本项补**快照 + 恢复 + 对账**，让重启可续、可审计。

## 2. 现状（已核实）

| 已有 | 位置 | 说明 |
|---|---|---|
| StateStore 接口 | `state/base.py:30-71` | `mutate(key,fn)`、`get(key)`；可选 `delete/close`；`rate_limit_allow`（令牌桶，基于 mutate） |
| memory 后端 | `state/memory.py` | 全局锁 dict |
| PG 后端 | `state/pg.py` | `state_kv` 表 + `SELECT ... FOR UPDATE` 行锁 |
| redis 后端 | `state/redis.py` | WATCH/MULTI/EXEC 乐观重试（`_MAX_RETRIES=8`） |
| 双层槽位 | `state/factory.py` | state / rate 可拆（`DD_STATE_BACKEND` / `DD_RATE_BACKEND`） |
| 表结构 | `models.py:181-185` + 迁移 `20260929_0600` | `state_kv(key PK, value JSON, updated_at FLOAT)` |

**缺**：checkpoint/snapshot/restore/export/import（全仓库 grep 零命中）。

## 3. 设计

### 3.1 语义边界（先说清楚）

- **不是**流处理器的精确一次 checkpoint（那需 offset + 状态的事务性联动）。
- 而是**状态词典的周期快照 + 可恢复导出**——足以让重启后**窗口/分组/限流**不丢历史，
  并支持**对账**（快照 vs 实时）。

### 3.2 接口扩展（`StateStore`）

```python
# state/base.py
class StateStore(ABC):
    ...
    def snapshot(self, *, prefix: str = "") -> dict[str, Any]:
        """导出前缀匹配的所有 key→value（浅拷贝，供持久化）。"""   # 默认实现：遍历
    def restore(self, data: dict[str, Any], *, overwrite: bool = False) -> int:
        """从快照恢复；overwrite=False 时只补缺失 key。返回写入数。"""  # 默认实现
```

- memory：直接遍历 dict（持锁拷贝）。
- PG：`SELECT key, value FROM state_kv WHERE key LIKE :prefix%`。
- redis：`SCAN` + `MGET`（避免 `KEYS` 阻塞）。

### 3.3 快照落盘（chk 表 / 文件）

```sql
CREATE TABLE state_checkpoint (
    checkpoint_id TEXT PRIMARY KEY,     -- chk_<ts>_<hash>
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    prefix        TEXT NOT NULL DEFAULT '',
    n_keys        INT NOT NULL,
    payload       JSONB NOT NULL        -- 状态词典（可 gzip 后 base64 存 text）
);
```

- **周期任务**：`DD_CHECKPOINT_INTERVAL_S`（默认 300s）导出一次；保留最近 `DD_CHECKPOINT_KEEP`（默认 5）。
- **手动**：`POST /v1/state/checkpoint`（运维端点）触发一次。
- **恢复**：启动时若 `DD_CHECKPOINT_RESTORE=1` → 读最新快照 `restore()`（`overwrite=False`）。

### 3.4 对账

`GET /v1/state/diff?checkpoint_id=...` → 快照与当前状态差异（新增/变更/删除的 key）。
用于验证恢复正确性、排查状态漂移。

> **安全**：状态端点**仅运维**——加 `DD_OPS_TOKEN` 头校验（无 token 不暴露）；状态含分组
> 数据，**不跨租户展示**（快照按 prefix 可含租户维度，diff 亦然）。

## 4. 要建/要改的文件

| 文件 | 动作 |
|---|---|
| `src/data_dog/state/base.py` | 加 `snapshot`/`restore`（默认实现） |
| `src/data_dog/state/pg.py`、`memory.py`、`redis.py` | 各后端高效实现 |
| `src/data_dog/state/checkpoint.py` | **新建**：周期快照 + 保留策略 + 恢复 |
| `src/data_dog/storage/models.py` | 加 `state_checkpoint` 表 |
| `alembic/versions/20260930_0910_g3_state_checkpoint.py` | **新建迁移** |
| `src/data_dog/service/app.py` | 加 state 运维端点（token 门控）+ lifespan 挂快照周期任务 |
| `src/data_dog/settings.py` | 加间隔/保留/恢复开关/ops token |
| `tests/test_checkpoint.py` | **新建** |

## 5. 验收

1. **快照往返**：写若干状态 → `snapshot` → 清空 → `restore` → `get` 与原始一致。
2. **overwrite 语义**：`overwrite=False` 不覆盖已有 key；`True` 强制覆盖。
3. **周期快照**：到点自动落一行 `state_checkpoint`，保留数不超 `DD_CHECKPOINT_KEEP`。
4. **恢复启动**：`DD_CHECKPOINT_RESTORE=1` 启动 → 状态从最新快照恢复。
5. **对账**：人为改一个 key → `diff` 报出该差异。
6. **三后端一致**：memory/pg/redis 跑同一套断言。
7. **安全**：无 ops token → 运维端点 401/404；不泄露跨租户状态。

## 6. 诚实边界

- **非精确一次**：快照是**周期**的，两次快照之间重启会丢这段增量（RPO = 间隔）。
  要更强需 offset+状态事务性联动（大工程）。
- **快照期间的一致性**：导出**非原子**跨 key（除非加全局锁）；对"近似"状态够用，
  要严格一致需在快照时短暂冻结写（可选 `DD_CHECKPOINT_FREEZE=1`，有延迟代价）。
- **状态含租户数据**：快照**是敏感数据**，存储加密/访问控制属运营职责。
- **不替代持久化真相**：候选/报告的真相在 `candidates`/`reports` 表（已幂等落库）；
  状态快照只保证**算子内部状态**可续。

## 7. 依赖

无。可独立做。与 G1（异步 job 租约）、P4-5（分片成员视图）都用共享状态，本项的快照能力
对它们**有复用价值**（可先做 G3，再让 G1/P4-5 复用）。

## 8. 实现状态（已落地）

已实现，`386 passed` 全绿（新增 `tests/test_checkpoint.py` 13 项）。落地时相对本设计的
调整，如实记录：

- **`snapshot`/`restore` 建立在新增的 `keys(prefix)` 之上**：接口只要求后端实现
  `keys`（memory 遍历、PG `LIKE`、redis `SCAN`），快照/恢复逻辑在 `StateStore` 基类
  只写一遍，三个后端**免费获得**一致语义。`keys` 默认返回 `[]`（不强制后端实现）。
- **PG 的 `LIKE` 前缀做了转义**（`%`/`_`/`\`），否则前缀里含这些字符会被当通配符，
  匹配到本不该进的 key；用 `ESCAPE '\'`。
- **redis 用 `SCAN` 游标而非 `KEYS`**：`KEYS` 会阻塞整个 Redis（大库上是生产事故）。
- **默认关**（`DD_CHECKPOINT_ENABLED=0`）：快照能力对单副本/测试无必要，且涉及敏感
  数据；开启项，配 `DD_CHECKPOINT_RESTORE=1` 决定启动时是否恢复。
- **运维端点独立门控**：用 `DD_OPS_TOKEN`（`X-Ops-Token`）而非复用 `DD_API_KEY`——
  快照/diff 含各租户算子状态（敏感），即便开了 API 鉴权也只应给运维开。未配 token
  → 端点 **404**（不暴露），配了但 token 错 → 401。
- **`list_checkpoints` 不回 payload**：只回元数据，避免一次把**所有**状态词典拉进响应
  （大状态后端上会拖垮服务）。
- **周期任务先等一个间隔再首拍**：启动瞬间状态尚空，立即拍意义不大。停止用
  `Event.wait(timeout)` 而非 `sleep`——关停能立刻唤醒，不拖满整个间隔。

仍未解（诚实边界，同 §6）：**非精确一次**——快照周期执行，两次快照之间重启会丢这段
增量（RPO = `DD_CHECKPOINT_INTERVAL_S`）；**导出非原子**跨 key（并发写时各 key 可能取自
不同瞬间，对合并窗口/令牌桶这类近似状态够用）；**快照含租户数据**（敏感，存储加密/
访问控制属运营职责）；**不替代持久化真相**（候选/报告真相在 `candidates`/`reports` 表，
快照只保证算子内部状态可续）。
