# G7 真实变更管理源适配器

## 1. 目标

`query_recent_changes`（Agent 查"近期有无固件/算子/配置变更"）现在**复用内置假数据表**
（`RECENT_CHANGES`）。本项接**真实的变更管理源**（CMDB / 发布系统），用适配器 + mock，
对齐 A7 知识导入的做法。

## 2. 现状（已核实）

| 已有 | 位置 | 说明 |
|---|---|---|
| 工具封装 | `tools/investigation.py:56-60` | `query_recent_changes` 工具 |
| Bridge 实现 | `service/evidence.py:161-164` | 返回 `{"changes": RECENT_CHANGES.get(...)}` |
| 内置假数据 | `evidence/store.py:138-` | `RECENT_CHANGES` 硬编码 dict（firmware/operator/config 三类） |
| 签名 | `(device_sn, component, since)` | **`since` 实际未用**（忽略时间过滤） |

**缺**：CMDB/发布系统适配器；`since` 过滤；真实数据源接入。

## 3. 设计

### 3.1 适配器接口（对齐 A7 `knowledge_ingest` 的做法）

```python
# evidence/change_source.py（新建）
@dataclass(frozen=True)
class ChangeRecord:
    change_id: str
    kind: str                 # firmware / operator / config / hardware
    target: str               # 部件或设备
    from_version: str
    to_version: str
    applied_at: str           # ISO
    actor: str
    source: str               # 来源系统标识

class ChangeSource(Protocol):
    def query(self, *, device_sn: str, component: str = "",
              since: str = "", until: str = "") -> list[ChangeRecord]: ...

class MockChangeSource:        # 包装现有 RECENT_CHANGES，供离线/CI
class JsonlChangeSource:       # 从 jsonl 文件读（导入导出用）
class WebhookChangeSource:     # POST 查询（CMDB API；目标过 SSRF 校验！）
class SqlChangeSource:         # 可选：直查变更库表
```

- **默认 MockChangeSource**（保持离线确定性，CI 不变）。
- `DD_CHANGE_SOURCE=mock|jsonl|webhook` 切换——**关键字切换**，同现有后端风格。

### 3.2 真实接入点（弹性的关键）

现实里变更系统五花八门，故**不强绑一家**：
- **推送式**：变更系统调 `POST /v1/changes/ingest`（本项目收）→ 落 `change_records` 表 →
  Agent 查库（最稳，推荐）；
- **拉取式**：`WebhookChangeSource` 按需查 CMDB API；
- **文件式**：CI/发布流水线导出 jsonl → `JsonlChangeSource` 读（最易落地）。

### 3.3 表（推送式用）

```sql
CREATE TABLE change_records (
    tenant_id  TEXT NOT NULL,
    change_id  TEXT NOT NULL,
    kind       TEXT NOT NULL, target TEXT NOT NULL,
    from_version TEXT, to_version TEXT, applied_at TIMESTAMPTZ NOT NULL,
    actor      TEXT, source TEXT NOT NULL, raw JSONB,
    PRIMARY KEY (tenant_id, change_id)
);
```

Agent 侧 `query_recent_changes(since=...)` → 查表（**启用 `since` 过滤**）。

### 3.4 修复 `since`

现有 `since` 被忽略是**静默错误**：Agent 以为按时间过滤了，实际没有（可能拿到很久前的
变更误导结论）。本项**必须修**：所有 `ChangeSource` 实现都**真正按 `since`/`until` 过滤**。

## 4. 要建/要改的文件

| 文件 | 动作 |
|---|---|
| `src/data_dog/evidence/change_source.py` | **新建**：ChangeRecord + 各适配器 |
| `src/data_dog/service/evidence.py` | 改：`query_recent_changes` 走 `ChangeSource`，真过滤 `since` |
| `src/data_dog/evidence/store.py` | `RECENT_CHANGES` 降级为 `MockChangeSource` 的数据 |
| `src/data_dog/storage/models.py` | 加 `change_records` 表（推送式） |
| `alembic/versions/20260930_0920_g7_change_records.py` | **新建迁移**（含 RLS） |
| `src/data_dog/service/app.py` | 加 `POST /v1/changes/ingest`、`GET /v1/changes` |
| `src/data_dog/settings.py` | 加 `DD_CHANGE_SOURCE` 等 |
| `tests/test_change_source.py` | **新建** |

## 5. 验收

1. **since 真过滤**：`since` 之后的变更被返回，之前的被排除（**修复静默忽略**）。
2. **适配器一致**：mock/jsonl/（webhook）跑同一套断言。
3. **推送式**：`POST /v1/changes/ingest` → 落库 → Agent 查到。
4. **SSRF**：webhook 目标过 `fetch.validate_url`，内网 URL 被拒。
5. **契约不变**：工具签名与返回结构不变，Agent 侧零改动。
6. **租户隔离** + 缺头 401。
7. **零回归**：`test_service_evidence*` 全绿（mock 默认保持输出）。

## 6. 诚实边界

- **不绑定具体 CMDB 产品**：本项给**适配器骨架 + 一个可用的 mock/jsonl/webhook**；对接
  ServiceNow/Jira/自研发布系统需按实际 API 写一个适配器（几十行）。
- **变更 ≠ 故障**：记录的是"变了什么"，**因果仍由 Agent 结合证据推断**——不宣称"变更即根因"。
- **推送式需变更系统配合**：拉取式受限于 CMDB API 能力。
- **时效性**：变更记录可能滞后于实际（发布系统录入延迟），Agent 应容忍。

## 7. 依赖

无。对齐 A7（`knowledge_ingest`）已有模式，可借鉴其适配器结构。
