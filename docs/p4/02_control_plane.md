# P4-2 控制面：Registry / Canary / 回滚 / 签名

## 1. 目标

把算子工厂从「demo」变「产品」：算子能**版本化注册、签名、灰度上线、按指标自动回滚**。
没有控制面，算子无法安全迭代——这是 P1 算子工厂真正可运营的前提。

## 2. 现状（已核实）

| 已有 | 位置 | 说明 |
|---|---|---|
| 发布状态机 | `operator_factory/lifecycle.py` | `OperatorLifecycle`：`Registered→Shadow→Canary→Active` + 分支 `Rejected/Quarantined/RolledBack`；`transition/quarantine/rollback/run_happy_path` |
| 门控检查 | 同上 `_gate` | 检查 `evidence["signature"]`（**仅字符串布尔**）、`metrics_ok` |
| 版本标签 | `operator_factory/pipeline.py` | 算子带 `operator_id@version` |

**缺**：真实 Registry 服务与**持久化**；真实**签名/验签**（现在只是字符串检查）；Canary
**流量路由**与指标门；`cli.py` / `service/app.py` 无相关命令/端点。lifecycle 第 13 行明示
"不接真实 Registry/签名服务（属 P4）"。

## 3. 设计

### 3.1 数据模型（新表，租户作用域）

```sql
-- operator_registry: 每个 (operator_id, version) 一行
CREATE TABLE operator_registry (
    tenant_id     TEXT NOT NULL,
    operator_id   TEXT NOT NULL,
    version       TEXT NOT NULL,
    state         TEXT NOT NULL,          -- 复用 lifecycle.STATES
    dsl_source    TEXT NOT NULL,          -- 算子源（受限 DSL）
    dsl_hash      TEXT NOT NULL,          -- sha256(source)：确定性 ID 的基础
    signature     JSONB,                  -- {alg, key_id, sig, signed_at}
    rollout       JSONB,                  -- {stage, percent, shadow_of}
    metrics_snapshot JSONB,               -- 最近一次门控指标
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, operator_id, version)
);
-- operator_rollout_event: 状态转移台账（审计）
CREATE TABLE operator_rollout_event (
    tenant_id TEXT NOT NULL, operator_id TEXT NOT NULL, version TEXT NOT NULL,
    from_state TEXT, to_state TEXT, reason TEXT, actor TEXT,
    at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`tenant_id` 进主键（同现有约定）；读写走仓储强制过滤；RLS 迁移同步为两表加策略。

### 3.2 签名（真实，非字符串）

- 算法：**Ed25519**（`cryptography` 库，已是常见依赖）或 HMAC-SHA256 起步。
- 签名对象：`dsl_hash`（源内容哈希）+ `operator_id` + `version`，即**内容绑定**——
  改一个字符就验签失败。
- 密钥：`DD_OPERATOR_SIGNING_KEY`（私钥，不进仓库）；公钥 `DD_OPERATOR_PUBKEY` 验签。
- 验签失败 = **不可注册**（失败关闭）。

```python
# operator_factory/signing.py（新建）
def sign_package(operator_id: str, version: str, source: str, *, priv_key: str) -> dict: ...
def verify_package(pkg: dict, *, pub_key: str) -> bool: ...
```

### 3.3 Canary 流量路由

网关/解析层选算子集时，按 `rollout.percent` 把流量**确定性地**分给 canary 或 stable：

```python
# 确定性分流：同一 (tenant, device) 稳定落同一侧（否则抖动会污染指标）
def pick_version(candidates: list[Row], device_sn: str) -> Row:
    target = candidates[0]
    if not target.rollout or target.rollout.get("stage") != "canary":
        return target
    pct = int(target.rollout.get("percent", 0))
    bucket = crc32(f"{target.operator_id}|{device_sn}".encode()) % 100
    return target if bucket < pct else stable_of(candidates)
```

### 3.4 自动回滚（指标门）

`Canary` 阶段挂一个**指标看门狗**：拉 canary 版算子与 stable 版的候选质量指标
（候选数/误报代理/崩溃计数），越限（`DD_CANARY_MAX_*`）→ 自动 `rollback()`（复用
lifecycle 的确定性转移）。看门狗可复用 `eval_feedback` 的一致率/误报率口径。

### 3.5 API

```
POST /v1/operators                 注册（验签，失败关闭）
GET  /v1/operators                 列表（按 operator_id 聚合，含当前 Active 版本）
GET  /v1/operators/{id}/{version}  详情（含 rollout、指标快照）
POST /v1/operators/{id}/transition 状态转移（可选 body: {to, reason}）
POST /v1/operators/{id}/rollback   回滚到上一签名版本
POST /v1/operators/{id}/canary     {percent} 启动/调整灰度
```

全部 `X-Tenant-Id` 作用域；跨租户 404；缺头 401。

## 4. 要建/要改的文件

| 文件 | 动作 |
|---|---|
| `src/data_dog/storage/models.py` | 加 2 表（operator_registry / operator_rollout_event） |
| `alembic/versions/20260930_0800_a1_operator_registry.py` | **新建迁移**（含 RLS 策略） |
| `src/data_dog/storage/repository.py` | 加 operator registry 的 CRUD + 列表 |
| `src/data_dog/operator_factory/signing.py` | **新建**：Ed25519/HMAC 签名验签 |
| `src/data_dog/operator_factory/registry.py` | **新建**：注册/查询/转移/回滚/canary 的领域逻辑，包住 `lifecycle.py` |
| `src/data_dog/operator_factory/lifecycle.py` | 复用（`_gate` 改调真实 `verify_package`） |
| `src/data_dog/service/app.py` | 加 6 个 `/v1/operators*` 端点（文件末尾新分节） |
| `src/data_dog/settings.py` | 加签名密钥、canary 阈值配置 |
| `tests/test_operator_registry.py`、`tests/test_signing.py` | **新建** |

## 5. 验收

1. **验签**：改一个字节 → `verify_package` 为 False → 注册被拒（失败关闭）。
2. **状态机持久化**：注册→shadow→canary→active 每步落 `operator_rollout_event`；重启后状态可读。
3. **Canary 确定性**：同一 (operator, device) 多次 `pick_version` 落同一侧；`percent=0` 全 stable、`100` 全 canary。
4. **自动回滚**：注入越限指标 → 看门狗触发 `rollback` → 回上一签名版本。
5. **租户隔离**：t1 注册的算子 t2 不可见/不可操作（404）；RLS 真库验证。
6. **零回归**：`test_operator_factory*`、`test_lifecycle*` 全绿。

## 6. 诚实边界

- **签名密钥管理**是运营职责：本项提供签名机制，**不提供 KMS/HSM**。私钥进环境变量，
  生产应接 KMS（属更大工程）。
- **Canary 指标门**依赖有意义的指标；冷启动期间样本不足时**默认不自动推进**（保守）。
- **回滚是回到上一签名版本**，不是删除当前版本（对齐 lifecycle 语义）。
- Registry 是**单区域**的；跨区域复制见 P4-6。

## 7. 依赖

- **P4-1 沙箱**：验签通过仅代表"来源可信"，执行仍需沙箱——两者合起来才是完整信任链。
- **lifecycle.py** 已有状态机复用，本项主要补持久化 + 真签名 + 路由 + API。

## 8. 实现与验收（已完成）

**状态：✅ 已交付**（分支 `feat/p4-orchestration`；430 passed / 4 skipped，ruff clean）。

### 8.1 落地清单（对照 §4）

| 文件 | 动作 | 说明 |
|---|---|---|
| `src/data_dog/operator_factory/signing.py` | 新建 | `SignedPackage`、`sign_package`/`verify_package`/`verify_or_raise`、`canonical_payload`、`source_hash` |
| `src/data_dog/storage/models.py` | 改 | `OperatorRegistryRow`（复合主键）+ `OperatorRolloutEventRow`（代理主键台账） |
| `alembic/versions/20260930_0800_a1_operator_registry.py` | 新建 | 两表 + 索引 + RLS（仅 PostgreSQL；SQLite 跳过） |
| `src/data_dog/storage/repository.py` | 改 | 8 个函数：upsert/get/list（含聚合）/set_state/record_event/list_events/find_previous_signed |
| `src/data_dog/operator_factory/registry.py` | 新建 | 注册/转移/灰度/回滚/看门狗/确定性路由，包住 `lifecycle` |
| `src/data_dog/service/app.py` | 改 | 6 个 `/v1/operators*` 端点（文末新分节） |
| `src/data_dog/settings.py` | 改 | 签名密钥/算法 + 4 个 canary 阈值 |
| `tests/test_signing.py` `test_operator_registry.py` `test_operators_api.py` | 新建 | 51 个用例 |

### 8.2 算法取舍（相对 §3.2 的偏离，已核实）

- **默认 HMAC-SHA256 而非 Ed25519**：`cryptography` 不是本项目依赖，项目风格偏好**零新增必需依赖**。
  Ed25519 作为**可选**（`[signing]` extra）保留，`_try_ed25519()` 探测；未装时对 Ed25519 请求
  **明确报错**（`SigningError`），**绝不静默降级**成 HMAC。
- 签名对象确为**内容绑定**：`canonical_payload` 对 `(operator_id, version, dsl_hash)` 做
  JSON `sort_keys` 规范化编码再签——`"a"+"b|c"` 与 `"a|b"+"c"` 产出不同签名串（防移位拼接）。

### 8.3 验收对照（§5）

| # | 验收项 | 证据 |
|---|---|---|
| 1 | 验签：改一字节 → 拒 | `test_tamper_one_byte_rejected` / `test_register_tampered_source_rejected`（422）|
| 2 | 状态机持久化 + 重启可读 | `test_transition_happy_and_persist`（4 条台账，新会话读 Active）|
| 3 | Canary 确定性 | `test_pick_version_deterministic_same_device`（20 次同侧）/ `pct_0`=全 stable / `pct_100`=全 canary |
| 4 | 自动回滚越限触发 | `test_evaluate_watchdog_auto_rolls_back` / `test_watchdog_auto_rollback`（→ RolledBack）|
| 5 | 租户隔离 404 | `test_get_cross_tenant_404` / `test_cross_tenant_isolated` |
| 6 | 零回归 | 全量 430 passed（含既有 `test_lifecycle`/`test_operator_factory`）|

额外覆盖：无钥匙 **失败关闭**（`verify_package` 返 False；注册端点 503）、样本不足**保守不判**
（`test_should_rollback_min_samples_conservative`）、看门狗**仅 Canary 态生效**。

### 8.4 诚实边界（对照 §6，已落实）

- 密钥托管仍是运营职责：本项只提供机制，环境变量注入（`DD_OPERATOR_SIGNING_KEY` / `DD_OPERATOR_PUBKEY`）。
- 冷启动样本不足 → 看门狗不自动判定（`min_samples`，默认 50；API 测试置 10）。
- 回滚 = 停用当前版本（→ RolledBack，非删除），并回报 `fallback_to` 上一签名版本。
