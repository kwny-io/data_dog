# data_dog 受限 DSL v0.1 规范

> P0-3 交付物。对齐缺口 **A-1 / E-3**（架构 §8.3 列了"受限 DSL"却无任何定义）。路线见桌面 `18-缺口分析与分阶段实现路线.md` §2 P0。
> 定位：架构 §8.3 的**四层表达能力**中的**第二层**——`声明式配置 → 受限 DSL → WASM → 受限容器`。
> 原则：**配置优先、代码兜底**。DSL 只做"比声明式多一点、但远不是通用编程"的事；越界一律**不许升级表达**，必须先证明低层无法表达（§8.3）。

---

## 0. DSL 是什么、不是什么

| 是 | 不是 |
|---|---|
| 一个**无副作用、纯函数式**的表达式/规则语言 | 通用编程语言（没有循环、没有递归、没有 I/O） |
| 运行在**受限解释器**里，能力白名单（§11.1） | 任意代码执行（没有 `import`、`eval`、文件、网络） |
| 每条规则**确定性**：同输入必同输出 | 有随机、有挂钟时间、有隐式外部状态 |
| 面向解析/标准化/窗口/规则检测 | 面向复杂算法（那属 WASM/容器层） |

**一句话**：DSL 是"**给检测工程师用的、能被审计的、绝不会执行任意代码的规则语言**"。

---

## 1. 与现有实现的关系

现有 `schemas/operator_config.py` 的 `ThresholdRule`（`field/operator/value`）就是**第 1 层"声明式配置"**，`scripts/run_generated_operator.py` 是它的最小执行器。

DSL 是**在它之上的第 2 层**：`ThresholdRule` 表达不了的（多字段组合、位运算、窗口聚合、状态机）用 DSL 表达。**DSL 规则与声明式规则可共存于同一个算子 yaml**：

```yaml
operator_id: vendor-a.motor-detector
component: joint_motor.front_left_hip
input_schema: raw-log-envelope-v1
output_schema: normalized-telemetry-v1

rules:                     # 第 1 层：声明式（现有）
  - rule_id: MOTOR_TEMP_TREND
    field: temperature
    operator: ge
    value: 75.0
    severity_hint: warning

dsl_rules:                 # 第 2 层：受限 DSL（本规范）
  - rule_id: MOTOR_TEMP_RISE_UNDER_LOAD
    when: "window.mean(temperature) >= 75 and window.mean(current) > 1.5 * window.baseline(current)"
    for: 30s
    severity_hint: warning
    message: "负载未增但温升持续，疑热退化"
```

**升级判据（§8.3）**：能用 `rules` 表达就**不许**用 `dsl_rules`；能用 `dsl_rules` 就**不许**上 WASM。算子 Manifest 必须记录 `why_not_lower_layer` 字段说明理由。

---

## 2. 词法（Lexical）

### 2.1 字面量与标识符

| 类别 | 形式 | 例 |
|---|---|---|
| 整数 | `[0-9]+` | `75` |
| 浮点 | `[0-9]+\.[0-9]+` | `1.5` |
| 时长 | 数字 + `ms`/`s`/`m` | `20ms` `30s` `5m` |
| 时长字符串 | 带引号同上 | `"30s"` |
| 布尔 | `true` / `false` | |
| 字符串 | 双引号，禁转义爆炸（仅支持 `\"` `\\`） | `"W_TEMP_RISING"` |
| 标识符 | `[A-Za-z_][A-Za-z0-9_]*` | `temperature` `MOTOR_OVERHEAT` |
| 字段引用 | 标识符（当前事件指标 / 窗口聚合） | `temperature` `current` |

### 2.2 注释与空白

- 行注释：`#` 到行尾（yaml 内注意引号包裹）。
- 空白不敏感。

---

## 3. 表达式语法（Expression）

### 3.1 运算符（按优先级从高到低）

| 优先级 | 运算符 | 说明 | 示例 |
|---|---|---|---|
| 1 | `()` | 分组 | `(a or b) and c` |
| 2 | `not` | 逻辑非 | `not lost` |
| 3 | `*` `/` `%` | 算术 | `1.5 * baseline_current` |
| 4 | `+` `-` | 算术 | `q - q_ref` |
| 5 | `<` `<=` `>` `>=` | 比较 | `temperature >= 75` |
| 6 | `==` `!=` | 相等 | `mode == 1` |
| 7 | `&` | 按位与（整数） | `status & 0x04` |
| 8 | `and` | 逻辑与（短路） | `rising and hot` |
| 9 | `or` | 逻辑或（短路） | `hot or overcurrent` |

- **禁止**：赋值 `=`、自增、逗号表达式、三元 `?:`、函数定义。
- `and`/`or` 必须**短路**求值（保证 `x != 0 and 1 / x > 0.5` 安全）。

### 3.2 字段与常量

- **原始字段**：直接写字段名（须在算子 `fields` 里声明过），如 `temperature`。
- **枚举/状态码**：用字符串常量比较，如 `status_code == "W_TEMP_RISING"`。
- **十六进制**：`0x` 前缀，如 `status & 0x04`。

### 3.3 内置函数（白名单，闭集，v0.1 仅这些）

| 类别 | 函数 | 语义 | 返回 |
|---|---|---|---|
| 数值 | `abs(x)` `min(a,b)` `max(a,b)` | 绝对值/最值 | number |
| 取整 | `floor(x)` `ceil(x)` `round(x)` | 取整 | integer |
| 范围 | `clamp(x, lo, hi)` | 夹取 | number |
| 位 | `bit(x, n)` | 第 n 位 | 0/1 |
| 位 | `popcount(x)` | 置位数 | integer |
| 校验 | `crc16(data)` `crc32(data)` `checksum8(data)` | 校验和（对字节数组） | integer |
| 转换 | `to_celsius(x)` `to_ampere(x)` `to_rpm(x)` | **按算子声明的单位换算** | number |
| 存在 | `present(field)` | 字段在本事件中存在 | bool |
| 缺失 | `missing(field)` | 字段缺失 | bool |

> **闭集原则**：白名单外的函数名 → 编译期拒绝（`invalid_input`），不是运行时错误。
> 新增函数 = 改 DSL 版本 + 评审，**不通过环境注入**。

### 3.4 窗口聚合（Window 层）

只在算子声明了 `window` 时可用：

| 函数 | 语义 |
|---|---|
| `window.mean(field)` | 窗口均值 |
| `window.max(field)` / `window.min(field)` | 窗口极值 |
| `window.last(field)` / `window.first(field)` | 窗口首末值 |
| `window.sum(field)` | 窗口求和 |
| `window.std(field)` | 窗口标准差 |
| `window.p95(field)` / `window.p50(field)` | 分位数 |
| `window.slope(field)` | 窗口内线性斜率（趋势） |
| `window.count(field, op, value)` | 满足条件的样本数，如 `window.count(temperature, ">", 75)` |
| `window.baseline(field)` | **同工况基线值**（来自 Enricher 的基线快照） |
| `window.delta(field)` | 窗口末值 − 首值 |

**确定性要求**：窗口聚合必须只依赖**事件时间**（`occurred_at`），**禁止**用挂钟时间（§10.3 `clocks: event-time-only`）。

### 3.5 时间持续性（`for`）

规则可带 `for: <duration>`，表示条件**持续满足**该时长才触发：

```yaml
when: "window.mean(temperature) >= 75"
for: 30s
```

语义：从条件**首次为真**的事件时间起算，持续 ≥30s 仍为真 → 触发。中途为假则计时重置。**必须**由事件时间驱动（迟到数据按算子声明的策略处理，§16.2）。

---

## 4. 规则结构（Rule）

```yaml
dsl_rules:
  - rule_id: <唯一标识，大写蛇形>          # required
    when: <表达式>                          # required，须求值为 bool
    for: <时长，可选>                        # 持续条件
    severity_hint: info|warning|critical    # required
    message: <触发说明>                      # required
    emit:                                   # 可选：命中后产出什么
      signal: <信号名>                       # 默认 = rule_id
      extra_evidence: [<字段名>, ...]        # 附到证据摘要的字段
```

- 一个算子可有多条 `dsl_rules`；**全部规则并列求值**（无隐式顺序）。
- 命中即产出候选信号（交由 Candidate Builder 合并，§9.3 第 6 步）。
- **禁止**规则之间共享可变状态（状态机的状态是"算子状态"，不是"规则间全局变量"）。

---

## 5. 状态机（可选能力，v0.1 受限）

对"需要记住上一步"的检测（如"温度先升后降"），提供**单一、有限**的状态机原语：

```yaml
state_machine:
  initial: NORMAL
  states:
    NORMAL: { to: RISING, when: "window.slope(temperature) > 0.5" }
    RISING: { to: OVERHEAT, when: "temperature >= 75", for: 10s }
    OVERHEAT: { to: NORMAL, when: "temperature < 60", for: 30s }
  on_enter:
    OVERHEAT: { emit: { rule_id: MOTOR_OVERHEAT, severity_hint: critical } }
```

**边界**：
- 状态数 ≤ 16；无嵌套状态；转移条件同上表达式语法。
- 状态**必须**可序列化进 checkpoint（§8.5 / A-3），迁移策略见算子 Manifest `state.mode`。
- 无"广播""并行区域""历史状态"等 UML 高级特性。

---

## 6. 能力边界（安全，对齐 §11）

DSL 运行时**默认且仅允许**：

| 能力 | 许可 |
|---|---|
| 读当前事件字段 | ✅ |
| 读窗口聚合 / 基线快照 | ✅ |
| 读声明的只读参考数据（单位/枚举表） | ✅ |
| 写声明的输出（候选信号 / 证据） | ✅ |
| 网络 | ❌ |
| 文件系统 | ❌ |
| 进程/Shell | ❌ |
| 挂钟时间 / 随机数 | ❌ |
| 动态代码求值（`eval`/`exec`/反射） | ❌ |
| 无限循环 / 递归 | ❌（语言无循环构造） |
| 访问其它租户/设备数据 | ❌ |

任何越界在**编译期**（加载算子时）拒绝，不进入运行时。

---

## 7. 运行时语义

- **纯函数**：同 `(事件输入, 状态, 基线)` → 同输出，且不得修改输入。
- **超时**：单条求值 ≤ 20ms（SLO §4），超时熔断回退稳定版（§16.3）。
- **资源**：CPU ≤10ms、内存 ≤32MB、输出 ≤16KB（SLO §4 / §10.2）。
- **时钟**：只用 `occurred_at`（event-time），水位线/迟到按 §16.2。
- **错误**：表达式类型错误（如 bool 参与算术）→ 编译期拒绝；运行时字段缺失 → `missing()` 语义（返回 false / 按 `optional` 处理），**不得**抛异常中断整条流。

---

## 8. 编译与升级判据（§8.3）

选择表达能力的**强制顺序**：

```
1. 声明式配置（ThresholdRule）   ← 能用就必须用
2. 受限 DSL（本规范）             ← 多字段/窗口/状态机时才用
3. WASM                          ← DSL 无法表达且高频/复杂
4. 受限容器（Python 等）          ← 需科学计算/重依赖
```

算子 Manifest 必须含：

```yaml
why_not_lower_layer: "<一句话说明为何更低层表达不了>"
```

**没有该字段或理由不成立 → 发布门拒绝**（P1 实现）。

---

## 9. 完整示例

### 9.1 热退化（多字段 + 基线 + 持续性）

```yaml
dsl_rules:
  - rule_id: MOTOR_TEMP_RISE_UNDER_NORMAL_LOAD
    when: "window.mean(temperature) >= 75 and window.mean(current) <= 1.2 * window.baseline(current)"
    for: 60s
    severity_hint: warning
    message: "负载未见上升但温度持续偏高，疑轴承摩擦/冷却退化"
    emit:
      signal: temperature_rising_under_normalized_load
      extra_evidence: [temperature, current]
  - rule_id: MOTOR_OVERHEAT_TRIP
    when: "temperature >= 90"
    severity_hint: critical
    message: "温度达 90°C，触发硬件过温保护阈值"
```

### 9.2 协议解析（位运算 + 校验）

```yaml
dsl_rules:
  - rule_id: FRAME_CHECKSUM_BAD
    when: "crc16(payload) != frame_crc"
    severity_hint: warning
    message: "帧校验和不匹配，疑传输损坏"
  - rule_id: FAULT_FLAG_SET
    when: "(status & 0x04) != 0"
    severity_hint: critical
    message: "故障标志位 bit2 置位"
```

### 9.3 状态机（先升后降）

见 §5 示例。

---

## 10. 明确不做（v0.1 边界）

- 用户自定义函数 / 宏
- 数组/映射的任意操作（只支持 `bit`/`popcount` 等原语）
- 字符串处理（比较之外）
- 跨事件的 join（那是 Correlator 的事，§14.1）
- 调用模型或任何外部服务

---

## 11. 版本记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v0.1 | 2026-09-13 | 首版：词法、表达式、白名单函数、窗口聚合、`for` 持续性、受限状态机、能力边界、升级判据 |
