# 持久化层（SQLAlchemy 2.0）

> 把项目里**内存态**的核心对象落库，重启不丢——把交付档次从 L1（可复现 Demo）推到 **L2（MVP 可交付）**。
> 架构 §15「数据、状态与存储」的最小落地。

---

## 1. 一句话

**同一份代码，本地用 SQLite（零安装），生产用 PostgreSQL；改一个连接串即可，代码不动。**

```bash
pytest tests/test_storage.py -q     # 10 项
```

---

## 2. 为什么用 SQLAlchemy + PostgreSQL（而不是 Redis）

| | PostgreSQL | Redis |
|---|---|---|
| 本质 | 关系型数据库，**落盘持久** | 内存键值库，主要为**快** |
| 用途 | **长期存储 / 查询 / 审计 / 可追溯** | 缓存 / 会话 / 限流计数 |
| 本项目 | 事件、候选、报告、Alert、Outbox | （将来）网关限流、热点缓存 |

**"长期存储" = 要落盘、要活过重启 → PostgreSQL。** Redis 是配角（快但不保证持久），不适合做长期存储。
**SQLAlchemy 是工具**：让你不写 SQL、且能在 SQLite / PostgreSQL 之间无缝切换。

---

## 3. 模块

| 文件 | 职责 |
|---|---|
| `db.py` | 引擎/会话管理、`Base`、`init_db`；`DD_DATABASE_URL` 决定连接串 |
| `models.py` | ORM 表（6 张，见下） |
| `repository.py` | 仓储层：领域对象 ↔ 行的转换、幂等 upsert、查询 |

### 表

| 表 | 内容 | 来源 |
|---|---|---|
| `telemetry_events` | 规范化遥测事件 | P2 |
| `candidates` | 候选异常（§12.3） | P2 |
| `reports` | 调查报告（§12.4） | Agent |
| `alerts` | 正式事件（§12.5） | P3 |
| `alert_outbox` | 上报 outbox（幂等，要求持久） | P3 |
| `alert_feedback` | 人工/维修反馈 | P3 |

**设计取舍**：多态字段（metrics / evidence / hypotheses / history）用 **JSON**（PostgreSQL 为 JSONB，SQLite 存 JSON 文本），关键字段单独建列 + 索引，避免过早拆表。

---

## 4. 用法

```python
from data_dog.storage.db import init_db, session_scope
from data_dog.storage import repository as repo

init_db()                                  # 建表（幂等）
with session_scope() as s:
    repo.save_candidates(s, candidates)    # 幂等 upsert
    repo.upsert_outbox(s, idempotency_key="dev:thermal:e1", revision=1, payload={...})
with session_scope() as s:                 # 新会话（模拟重启）
    print(repo.list_candidates(s, device_sn="dev-go2-0001"))
```

---

## 5. 切换数据库（本地 ↔ 生产）

```bash
# 本地开发（默认）：SQLite 文件，零安装
#   → ./data_dog.db

# 生产：PostgreSQL（装驱动 + 设环境变量）
pip install -e ".[postgres]"
export DD_DATABASE_URL="postgresql+psycopg://user:pw@host:5432/data_dog"
```

连接串示例：
```
sqlite:///data_dog.db                                  # 本地
postgresql+psycopg://user:pw@host:5432/data_dog        # 生产
```

**代码零改动**——这是用 SQLAlchemy 的核心收益。

---

## 6. 安全

- **无字符串拼接 SQL**：全部走 ORM 的 `session.get` / `session.query` / 绑定参数；
- 幂等写：按主键 / 复合主键 upsert，重复保存不产生重复记录（`alert_outbox` 用 `(幂等键, revision)` 复合主键强制去重）。

---

## 7. 与架构条目对照

| 架构 §15 存储 | 本项目 | 状态 |
|---|---|---|
| Event Log（规范化事件） | `telemetry_events` | ✅ |
| Alert Outbox（幂等、重试、死信） | `alert_outbox` | ✅ |
| Evidence / Candidate / Report | `candidates` / `reports` | ✅ |
| Control DB / Artifact Registry | — | ❌（属 P4 控制面） |
| Time-series Store | 复用 PostgreSQL（可加 TimescaleDB） | 🟡 |
| Document Store（不可变、加密） | 文件系统 + revision 哈希（P1） | 🟡 |
| State Store（Checkpoint） | — | ❌（属 P4 实时流） |

> 这一步把项目从"重启即丢"变成"**数据可持久化、可审计**"，是 L1→L2 的关键一环。
