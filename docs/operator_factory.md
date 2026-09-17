# P1 离线算子工厂

> 把架构蓝图的"低频离线闭环"（§8 文档到算子）从**演示脚本**做成**可跑、可测、可追溯**的流水线。
> 前置：P0 设计底座（[`slo.md`](./slo.md)、[`dsl_spec.md`](./dsl_spec.md)、`../schemas/`）。

---

## 1. 一句话

**厂商文档 → 结构化知识（带来源/定位）→ 检测算子（声明式 + 受限 DSL）→ Fixture 黄金回放验证。**

跑法：
```bash
python -m data_dog.operator_factory.p1_demo          # 人读输出
python -m data_dog.operator_factory.p1_demo --json   # 机器可读
pytest tests/test_operator_factory.py tests/test_operator_verification.py -q   # 27 项
```

实测输出：
```
知识包：11 份 revision，61 字段规格，9 条数值规格，6 个错误码
  ✅ 温度保护阈值 90.0°C —— verified 于 .../motor_msg_GO-M8010-6.h@docrev_99a124c861db (line:45)
  待确认（无出处，不冒充厂商事实）：['motor_temp_warning_c']
算子  ：local.joint_motor-motorstate-detector
  7 字段 / 2 声明式规则 / 2 DSL 规则（编译通过 2）
回放结果：5/5  准确率 1.0
```

---

## 2. 模块

| 模块 | 职责 |
|---|---|
| `knowledge.py` | **文档 Revision 管理**（不可变、内容寻址）+ VendorKnowledgePackage 结构化抽取 |
| `dsl.py` | **受限 DSL 安全求值器**（自建 词法→AST→解释器，无动态执行原语） |
| `pipeline.py` | 算子装配 + Fixture 黄金回放验证 |
| `lifecycle.py` | **发布状态机**（§8.5：Draft→…→Active + 门控 + Quarantine/Rollback） |
| `verification.py` | **Fuzz**（畸形输入不得崩）+ **新旧版本差分回放**（差异须可解释） |
| `p1_demo.py` | 端到端演示入口 |
| `fixtures/*.json` | 黄金测试用例（正例命中 / 反例不命中） |

---

## 3. 文档 Revision（§8.1）

每次接入生成**不可变** `DocumentRevision`，`revision_id = docrev_<sha256前12>`（内容寻址）——内容变了 id 必然变，可追溯、可去重、可表达"替代关系"。

知识与算子的每条规格都带 `source = "<文件>@<revision_id>"` + `locator = "line:N"`。

---

## 4. 诚实抽取（§8.2 的关键）

架构 §8.2 明确：**"模型推断不能被标为厂商事实"**。本模块把它变成机制：

- **只抽文档里能定位的规格**：数值正则要求数字是独立 token，跳过版权/页眉行（防 `© 2023` 被当成 `2023A`）。清理后从原始 30 条噪声降到 **8 条真实规格**。
- **抽不到的不许编**：公开语料里**没有**电机 75°C warning 阈值（90°C 保护阈见下）。抽取器不会把它写成厂商事实——它只能以 `unconfirmed` 出现在知识包里，或在 DSL 规则里显式标注为 `PROJECT ASSUMPTION`。
- **单位来自命名约定 → `confidence=inferred`**：IDL 不带单位，`temperature→celsius` 是推断，如实标注。
- **只吃厂商语料**：`SOURCES.md` / `LICENSE` / `README` 等**我们自己的元文件被显式排除**，不得当语料抽取。真实踩坑：SOURCES.md 里自述的"75°C 是假设"曾被抽成 `verified` 规格 → **循环自证**（自己写的说明被当成厂商出处）。修复后数值规格从虚高的 14 条降到 **9 条真实**，并有回归测试守护。

### 4.1 权威来源：厂商开源的电机协议头

搜索确认 Unitree 官方在 GitHub 开源了电机协议（[unitreerobotics/unitree_actuator_sdk](https://github.com/unitreerobotics/unitree_actuator_sdk)，**BSD-3-Clause**）。其 `motor_msg_GO-M8010-6.h` 明写：

```c
int8_t   temp;       // 电机温度: -128~127°C 90°C时触发温度保护
uint8_t  MError :3;  // 0.正常 1.过热 2.过流 3.过压 4.编码器故障 5-7.保留
```

抽取器现在能识别 `.h` 协议头注释：
- **90°C 温度保护阈 → `verified`**（厂商官方开源出处，带 `line:45` 定位）；
- **MError 枚举（0~4）→ 结构化 `ErrorCode`**；
- **75°C warning 阈 → 仍 `unconfirmed`**（开源协议里也没有）。

> 这就是"诚实抽取"的价值兑现：**能证实的（90°C）升级为厂商事实，证不了的（75°C）保持待确认**——不因为"反正差不多"就把 75 也说成官方数据。语料来源与许可见 `vendor_docs/unitree/SOURCES.md`。

> 另外，这一条相对原 `generate_operators.py` 是核心改进：原脚本把 75/90 **硬编码**且注释自认来自"架构剧本"；现在**90 有出处、75 标假设**，来源全可追溯。

---

## 5. 受限 DSL 落地（§8.3 第二层）

按 [`dsl_spec.md`](./dsl_spec.md) v0.1 实现的**可落地子集**：

- 词法 → 递归下降语法 → AST 解释器，**完全自建**；不调用任何动态代码执行原语（有测试做源码级保证）。
- 函数**闭集白名单**（`abs/min/max/floor/ceil/round/clamp/bit/popcount/present/missing/crc16/crc32/checksum8`）；白名单外**编译期**拒绝。
- **编译期类型检查**（布尔参与算术、`when` 非布尔 → 拒绝）。
- 字段必须在算子 `fields` 里**声明过**（未声明 → 编译期拒绝）。
- 窗口聚合（`window.mean/slope/p95/baseline/count/...`）只依赖事件时间序列，**确定性**。
- 校验和实现 CRC-16/CCITT-FALSE、CRC-32/IEEE、checksum8。

**未落地（显式声明，不假装支持）**：单位换算函数（`to_celsius` 等，需算子声明单位表，留 P2）；`for:` 持续性与状态机（属有状态层，与 checkpoint 一起做）。

---

## 6. Fixture 回放与验证（§8.4）

`fixtures/*.json` 每个用例声明 `expect_hit` / `expect_miss`；`replay()` 要求**正例命中且反例不命中**——防"永远为真"的假算子。当前 5 个用例，**5/5**。

**Fuzz**（`verification.fuzz_operator`）：喂超长/乱码/类型错乱输入，算子**不得崩溃、不得误命中**（§10.3）。首轮 Fuzz **抓到 43 个崩溃**（声明式比较拿 str/None 比大小抛 TypeError）→ 加固后（非数值一律不命中）**500+ 迭代 0 崩溃**。固定种子保证可复现。

**新旧差分**（`verification.diff_replay`）：同批数据喂新旧算子，差异必须能解释（在白名单内），否则判行为漂移、回退。

---

## 6.5 发布状态机（§8.5）

`lifecycle.py`：`Draft→Generated→Building→Validating→AwaitingApproval→Signed→Registered→Shadow→Canary→Active`，分支 `Rejected / Quarantined / RolledBack / Deprecated / Retired`。**确定性**：非法转移与门控失败均抛错。门控包括：回放全过、`why_not_lower_layer`、人工批准、制品签名、有状态算子 `state_policy`、灰度指标达标。

> 本地可控实现；真实 Registry/签名服务属 P4 控制面。

---

## 7. 与 Agent 主线的关系

算子（`operators/generated/*.yaml`）产出**候选异常**，下游仍走已有的 Agent 调查线（`data_dog/agent/loop.py`）——**换算子来源不改 Agent**，与 MuJoCo/合成数据那条"换数据源不改 Agent"是同一个解耦原则。

---

## 8. 与架构条目的对照

| 架构 | 状态 |
|---|---|
| §8.1 文档 Revision（不可变 + 哈希 + 来源） | ✅ 本模块 |
| §8.2 VendorKnowledgePackage + 事实/推断分层 | ✅ 本模块（含量化噪声过滤 + unconfirmed 标注） |
| §8.3 配置优先 + 受限 DSL 第二层 | 🟡 声明式✅ + DSL 表达式层✅；单位换算/`for`/状态机待 P2 |
| §8.4 验证流水线（Fixture 黄金 + 回放 + Fuzz + 新旧差分） | ✅ Fixture 回放 5/5 + Fuzz 0 崩溃 + 新旧差分；编译扫描/SBOM 待补 |
| §8.5 发布状态机（Shadow/Canary/Rollback） | ✅ `lifecycle.py`（本地确定性 + 门控）；真实 Registry 属 P4 |
