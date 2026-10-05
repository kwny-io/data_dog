# data_dog 标准事件 Schema

> P0-1 交付物：把架构文档 §12 的 **JSON 示例**固化成**可机器校验的正式 Schema**（JSON Schema Draft 2020-12），并定义**版本演进策略**。
> 对齐缺口：**A-2 / E-1**（"契约只有示例、没有正式 Schema 与版本演进策略"）。

---

## 1. 文件清单

| Schema 文件 | 对应事件 | 架构出处 | 方向 |
|---|---|---|---|
| `raw-log-envelope-v1.json` | RawLogEnvelope | §12.1 | 网关接入（原始日志信封） |
| `normalized-telemetry-event-v1.json` | NormalizedTelemetryEvent | §12.2 | 解析/标准化输出 |
| `candidate-event-v1.json` | CandidateEvent | §12.3 | 检测算子输出（待 Agent 复核） |
| `investigation-report-v1.json` | InvestigationReport | §12.4 | Agent 调查结论 |
| `saas-alert-event-v1.json` | SaaS AlertEvent | §12.5 | 正式上报（进入 SaaS 生命周期） |

**权威性**：字段名与嵌套结构**以架构蓝图为准**。若与现有代码实现不一致，**Schema 是契约、代码应向它靠拢**；差异已在 §3 明确列出，不允许出现"第二份真相"。

---

## 2. 版本演进策略

### 2.1 命名与 "$id"

- 文件名与 `$id` 都带版本后缀：`…-v1.json`。
- Schema 内**版本只体现主版本**（v1）；次版本兼容变更**不新建文件**，靠 `$comment` 与本文档 changelog 记录。

### 2.2 兼容性定义（决定"改字段能不能不升版本"）

| 变更类型 | 是否兼容 | 处理方式 |
|---|---|---|
| 新增**可选**字段 | ✅ 向后兼容（老消费者可忽略） | 主版本不变，记入 changelog |
| 新增枚举值（消费端需容忍未知值） | ⚠️ 有条件兼容 | 消费者**必须**对未知枚举容错；否则视为破坏性 |
| 放宽约束（如 range 扩大、required 减少） | ✅ 兼容 | 主版本不变 |
| 收窄约束（required 增多、range 缩小、pattern 收紧） | ❌ 破坏性 | **升主版本**（v2） |
| 删除字段 / 改字段名 / 改类型 / 改嵌套结构 | ❌ 破坏性 | **升主版本**（v2） |
| 改字段**语义**（同名不同义） | ❌ 破坏性 | **升主版本**，并在旧字段加 `deprecated` 注释 |

### 2.3 迁移规则

- 破坏性变更时，**新旧 Schema 并存**：`…-v1.json` 与 `…-v2.json` 同时保留，事件体带 `schema_version`（见 §2.4）让消费端路由。
- 至少一次传输 + 幂等（§16.1）：**允许重放旧版本事件**，消费者按 `schema_version` 选择校验器。
- 弃用流程：`deprecated` → 观察期（≥1 个发布周期）→ 移除 → 升主版本。

### 2.4 事件体里如何声明版本

事件**正文**不重复塞完整 schema，用**短版本号**声明，便于路由：

```json
{ "event_id": "…", "schema_version": "candidate-event/v1", … }
```

> `schema_version` 字段本身是**实现扩展**（不在 §12 示例中），但它是多版本并存的前提；已在本目录 README 说明，属"正交新增的可选字段"，不破坏蓝图语义。

---

## 3. Schema ↔ 现有实现 的映射（重要）

现有 `src/data_dog/schemas/events.py` 用 dataclass 定义了 `CandidateEvent` / `InvestigationReport`，结构**基本一致但有两处扁平化**。正式 Schema 按蓝图**嵌套**写，差异如下：

| 项 | 蓝图 / Schema（权威） | 现有 dataclass | 处理建议 |
|---|---|---|---|
| 窗口 | `window: {start, end, sample_count}` | 扁平 `window_start/window_end/sample_count` | 迁移时在 dataclass 里改成嵌套 `window`，或在 `to_dict()` 输出嵌套 |
| 证据摘要 | `evidence_summary: {指标 from/to, rule_matches, anomaly_score}` | `evidence: List[EvidenceSummary]`（扁平列表） | 明确二选一：推荐保留列表但**改名/嵌套**进 `evidence_summary`，避免同义异名 |
| `candidate_version` | 蓝图有，整数 | 无 | 补字段（默认 1） |
| `severity_hint` 取值 | `info/warning/critical` | 注释写 `warning\|critical`（漏 info） | dataclass 注释补 `info` |
| `detector_provenance` | 对象 `{operator_id, operator_version, state_generation}` | 字符串 `"operator_id:version"` | 迁移为对象，或保留字符串并在适配层转换（需在文档标注） |
| 实现扩展字段 | — | `correlation_group_id` / `linked_candidate_refs` | Schema 已收录为**可选**，标注"实现扩展（§14.1）" |

> **不要求立刻改代码**。本步骤只定契约；代码向契约靠拢属 P1 的迁移工作，届时同步更新 `11-测试文档` 与 `06-对照`。

---

## 4. 校验方式

`jsonschema` 已在本环境可用（4.26.0）。示例：

```python
import json, jsonschema
schema = json.load(open("schemas/candidate-event-v1.json", encoding="utf-8"))
jsonschema.validate(instance, schema)   # 不合规抛 ValidationError
```

本目录另附 `examples/` 与 `tools/validate_examples.py`，可一键校验"正例通过 / 反例被拒"（见下）。

---

## 5. 已明确收录的"缺口相关"约束

- **幂等键**（缺口 E-7）：`saas-alert-event-v1.json` 把 `idempotency_key` 列为 **required**，迫使上报侧必须给出业务幂等键（构成规则见 P3，此处先锁"必须有"）。
- **身份注入**（§13.3）：`raw-log-envelope-v1.json` 的 `tenant_id/device_sn` 描述注明"仅由网关注入"，Schema 层面不接受"可缺省/来自正文"的语义。
- **证据 opaque ref**：`evidence_refs` / `payload_ref` / `evidence_receipt` 均带 `://` pattern，坐实"受控引用"语义。
