# G6 跨部件因果时序 + 拓扑链

## 1. 目标

邻居关联现在只做**同窗共现**（同一时间窗内别的部件也异常）。本项补两件事：
① **因果时序**（"电压先跌 → 电流后涨"，顺序与滞后有意义，共现不够）；
② **层级拓扑链**（电池 → 驱动板 → 电机，沿供电/信号层级传播）。

## 2. 现状（已核实）

| 已有 | 位置 | 说明 |
|---|---|---|
| 邻居事件 | `service/evidence.py:125-159` `query_neighbor_events` | 扫**同设备其它部件、同 ISO 时间窗**落库遥测，用真实算子判定异常——**只做同窗共现，无因果序** |
| 部件相关系 | `correlation.py`（标注"雏形"） | `Correlator.correlate` 按键 `(device, family, window_start, window_end)` 归并；拓扑维度仅 `COMPONENT_POWER_DOMAIN`（`:35-41`，硬编码 5 部件→"power_bus"）+ `linked_group_ids`；`window_tolerance` **存了未用** |
| 层级结构 | **无统一定义** | `component_ref` 仅 `schemas/events.py:28` 字符串注释 `joint_motor.front_left_hip`；前缀散落在 `evidence/store.py:48`、`correlation.py:35`、各 fixtures |

**缺**：因果时序键、层级拓扑链（父/子解析）、传播方向。

## 3. 设计

### 3.1 统一定义部件拓扑（先解决"无定义"）

新建**单一真相源**（其余地方引用它，消除散落硬编码）：

```python
# data_plane/topology.py（新建）
@dataclass(frozen=True)
class ComponentNode:
    ref: str                 # joint_motor.front_left_hip
    parent: str | None       # 层级父：如 power.drive_board_front_left
    domain: str              # power_bus / sensor_bus / ...
    kind: str                # motor / bms / board / sensor

TOPOLOGY: dict[str, ComponentNode] = { ... }   # 唯一真相源

def ancestors(ref: str) -> list[str]: ...      # 沿 parent 上溯（→ 供电链）
def children(ref: str) -> list[str]: ...
def propagate_path(a: str, b: str) -> list[str] | None: ...  # 从 a 到 b 的层级路径
```

- 迁移 `correlation.py` 的 `COMPONENT_POWER_DOMAIN` 到此处（保留兼容别名）。
- 加**校验**：`component_ref` 必须在 `TOPOLOGY` 中（否则 `unconfirmed`，不硬塞）。

### 3.2 因果时序（有序 + 滞后）

共现 → **带方向的有序关联**：

```python
@dataclass(frozen=True)
class CausalLink:
    cause_ref: str
    effect_ref: str
    lag_s: float               # effect.occurred_at - cause.occurred_at，须 > 0 且 <= max_lag
    direction: str             # "parent_to_child"（供电链常见）/ "sibling"
    confidence: float

def infer_causal_links(
    events: list[NeighborEvent], *, max_lag_s: float = 5.0,
) -> list[CausalLink]:
    """找出"先发生 + 层级合理（沿拓扑）+ 滞后在窗内"的有序对。"""
```

判定规则（确定性）：
1. **时间序**：`cause` 必须**早于** `effect`，滞后 ≤ `max_lag_s`；
2. **拓扑合理**：`cause` 是 `effect` 的祖先/兄弟（沿 `TOPOLOGY`）——纯时序共现若无拓扑关系，
   **不判因果**（避免把无关部件的巧合当因果）；
3. **置信度**：拓扑越近 + 滞后越短 → 越高；反向（子→父异常同时出现）→ 标 `suspect`。

### 3.3 接进证据

`RepoEvidenceBridge` 新增：
- `query_causal_chain(device, component, window)` → 返回该部件的**前序因果链**
  （"先跌的是 X，滞后 t 秒 Y 跟着异常"）；
- 作为 Agent 工具的**新证据源**（只读，白名单部件，失败关闭）；
- 写进报告的 `hypotheses`/`counter_evidence_refs`（**证据引用不复制**，沿用现有 ref 机制）。

### 3.4 复用现有 correlation

`Correlator.correlate` 的按键归并**保留**，但：
- 启用**已存未用的 `window_tolerance`**（现在忽略）；
- 归并键加**拓扑族**（domain），让同供电域的候选能关联；
- `linked_group_ids` 补**方向**（谁先谁后）。

## 4. 要建/要改的文件

| 文件 | 动作 |
|---|---|
| `src/data_dog/data_plane/topology.py` | **新建**：部件拓扑唯一真相源 + 祖先/路径 |
| `src/data_dog/data_plane/causal.py` | **新建**：`CausalLink` + `infer_causal_links` |
| `src/data_dog/service/evidence.py` | 加 `query_causal_chain`；邻居查询接拓扑 |
| `src/data_dog/correlation.py` | 迁移 `COMPONENT_POWER_DOMAIN`；启用 `window_tolerance`；补方向 |
| `src/data_dog/tools/investigation.py` | 加因果链工具（只读、白名单） |
| `src/data_dog/evidence/store.py` | 引用 `TOPOLOGY`（去散落硬编码） |
| `tests/test_topology.py`、`tests/test_causal.py` | **新建** |

## 5. 验收

1. **拓扑解析**：`ancestors("joint_motor.front_left_hip")` 返回供电链；未知 ref → 报错/`unconfirmed`。
2. **因果序**：电压先跌（t=0）、电流后涨（t=1s）、拓扑同链 → 产出 `CausalLink(lag=1.0)`。
3. **拒绝伪因果**：两个**无拓扑关系**的部件同时异常 → **不**判因果。
4. **方向**：子部件先异常、父后异常 → 标 `suspect`（方向反了）。
5. **滞后上界**：超过 `max_lag_s` → 不关联。
6. **确定性**：同输入 → 同 `CausalLink` 集合。
7. **证据引用**：因果链以 ref 形式进报告，**不内嵌原文**。
8. **零回归**：`test_correlation*`、`test_service_evidence*` 全绿（拓扑迁移的兼容别名保证）。

## 6. 诚实边界

- **"因果"是工程启发式**：时间序 + 拓扑相邻是**强提示**，不是真正因果推断（那需干预实验
  或完整物理模型）。文档与证据措辞用"疑似因果链"，不宣称确定因果。
- **拓扑表是人工维护的**：真实车队部件拓扑可能更复杂/可变；本项给**结构与接口**，
  真实拓扑需按机型录入（对齐 A7 知识导入的做法）。
- **滞后窗需按机型调**：5s 是默认，电气/热传播时标不同。
- **单设备内**：本项不跨设备（跨设备因果属更大工程）。

## 7. 依赖

- 无强前置。与 **P4-7 退化预测** 互补（一个查因果序，一个查趋势）。

## 8. 实现状态（已落地）

已实现，新增 `tests/test_topology.py`（7 项）与 `tests/test_causal.py`（11 项），
并在 `test_correlation.py` / `test_service_evidence.py` 补 5 项；全量 433 通过 / 5 跳过。
落地时相对本设计的**调整与诚实边界**，如实记录：

- **拓扑表落在 `data_plane/topology.py`**：`ComponentNode(ref, parent, domain, kind)`
  + `TOPOLOGY` + `ancestors/children/propagate_path/domain_of/is_known/parent_of`。
  当前 5 个部件**确实同一供电域**（`power_bus`），故 `domain` 对它们不产生区分——
  domain 的价值在引入 `sensor_bus` 等其它总线后才显现（接口已就位，表待录）。
  供电链：`battery.main → controller.main → joint_motor.*`。
- **兼容别名保证零回归**：`correlation.COMPONENT_POWER_DOMAIN` 改为由 `TOPOLOGY`
  **派生**（不再是硬编码字典），`component_power_domain()` 转调 `topology.domain_of`。
  既有测试（`test_correlation.py::test_topology_power_domain_link`）逐条不改即绿。
- **`ALLOWED_COMPONENTS` 由 `TOPOLOGY` 派生**（`evidence/store.py`）：消除"白名单与
  拓扑各写一份、可能漂移"的散落硬编码；`test_topology.py` 断言二者相等。
- **因果是"工程启发式"**：`infer_causal_links` 规则 = ①因严格早于果且 `0<lag<=max_lag`；
  ②沿 `TOPOLOGY` 有层级关系（祖先 / 兄弟），**无关系不判因果**（拒伪因果）；③置信度随
  滞后线性衰减（`_BASE_CONF` × `max(0.2, 1-lag/max_lag)`）。这是**有序关联**，不是真正
  因果推断——文档与工具描述一律用"疑似因果链"。
- **方向**：`parent_to_child`（上游先动，正常传播）/ `sibling` / `child_to_parent`
  （子先父后，标 `suspect=True` 仅作提示）。`CausalLink` 冻结、`to_dict` 自描述。
- **`window_tolerance` 已启用**（此前存而未用）：`correlation.py` 里把窗口起点解析成秒，
  同设备 + 同电源域 + `|Δ起点| ≤ 容差` 即互为拓扑关联；`linked_group_roles` 补方向
  （`upstream`/`downstream`/`sibling`）。**解析失败退回精确字符串相等**——既有纯时刻
  形态（"09:55"）行为不变；默认容差 `"00:05"` 即 300s。
- **证据桥 `query_causal_chain`**：`RepoEvidenceBridge` 新增，复用抽出的 `_window_events`
  （同部件 + 邻居，附 `occurred_at_s`）→ `infer_causal_links`。**无事件时间的事件不参与**
  （不拿 0.0 硬凑）；未在拓扑登记的部件不参与。工具层 `query_causal_chain` 用 `getattr`
  探测证据源是否支持——**不支持则返回空链**（不冒充有数据），故未进 `EvidenceSource`
  Protocol 的必需集，旧证据源无需改。
- **settings**：`DD_CAUSAL_MAX_LAG_S`（默认 5）、`DD_CAUSAL_ENABLED`（默认开）。
- **`NeighborEvent.from_dict`** 容错解析，兼容 `severity_hint` / `severity` 两种键。

仍未解（诚实边界，同 §6）：**"因果"是强提示非确定**（需干预实验 / 完整物理模型）；
**拓扑表人工维护**，真实车队需按机型录入（本项只给结构与接口）；**滞后窗需按机型调**；
**单设备内**，不跨设备。报告回填（`hypotheses`/`counter_evidence_refs`）**未做**——
`query_causal_chain` 已是只读工具、Agent 可自行调用并据 ref 引用证据，但把它**自动**
写进报告假设是 AgentLoop 编排层的进一步接线，本项未接（沿用现有 ref 机制即可，无需复制
原文）。
