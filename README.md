# data_dog —— 机器狗遥测故障调查 Agent

一个会**自己调查异常**的 Agent：喂给它一个候选异常，它会调用只读工具查遥测 / 厂商知识 / 邻居事件 / 近期变更，逐步推理、建立假设、寻找反证，最后输出一份结构化 `InvestigationReport`（裁决 + 置信度 + 假设 + 缺失证据 + 建议）。

> **当前数据是 mock**：用于跑通"Agent 调查闭环"这条链路。接真实设备时只需替换 `EvidenceStore` 的数据源，不改 Agent / 工具逻辑（见「数据从哪来」）。

---

## 3 分钟快速上手

```bash
# 1) 安装（会自动装好 rich / PyYAML / openai 等全部依赖）
pip install -e .

# 2a) 不想联网 / 没有 API Key —— 直接看离线演示（免费、秒开）
data-dog test --offline

# 2b) 想走真实大模型 —— 先接入供应商，再跑
data-dog init
data-dog test
```

`data-dog test` 是交互窗：列出候选异常清单，输入编号即调查；`n` 临时新建异常；`i` 导入自定义 JSON；一轮可以连续调查多个，最后汇总成**对比表**（不同候选 → 不同结论）。

### 还想看 Web 界面（可选）

```bash
pip install -e ".[ui]"          # 装上 streamlit
python -m streamlit run ui/app.py           # 调查台：选候选 → ReAct 轨迹 → 报告
python -m streamlit run ui/mujoco_page.py   # MuJoCo 演示页：点按钮跑仿真 → 遥测 → Agent 调查 → 准确率
```

### 跑一下 Agent 评估（可选）

```bash
python -m evals.run_evals --mode mock    # 离线确定性评估，7 case（含 2 个对抗性：提示注入/身份伪装）
python -m evals.run_evals --mode real    # 用真实模型评估（需先 data-dog init）
```

### 跑合成数据 ground truth 评估（可选，出新数字）

自建合成数据生成器（基于 Go2 公开动力学/电机热参数 + 高斯噪声 + 5 类故障注入），
每条带 ground truth 标签，Agent 调查后与真值对比算准确率：

```bash
python -m data_dog.synth.synth_eval            # 离线（确定性 provider）→ 得到 X/N 准确率
python -m data_dog.synth.synth_eval --real     # 用真实 LLM 调查合成遥测（需先 data-dog init）
python -m data_dog.synth.synth_eval --json     # 只输出 {accuracy} JSON，便于脚本/简历取数
```

### 可观测性：token 消耗 / 耗时 / 成本

用真实模型跑调查时会自动统计**每次调用的 token 用量、耗时**并按供应商价格表**估算成本**：
- 后端在 `Provider.chat` 采集（openai `usage` + 计时），`AgentLoop` 聚合成每次调查的统计，写进报告 `model_provenance.usage`。
- 前端（rich 命令行）在报告头部显示一行：`次调用 · tokens (in/out) · 耗时 · 成本`。
- 离线 fake provider 无 token 统计，故离线演示不显示这行（避免无意义 0）。

实现：`data_dog/observability.py`（`UsageTracker` + 价格表）。真实输出示例：
`⏱ 2 次调用 · 150 tokens (in 100 / out 50) · 0.12s · 成本 $0.000082`

### 跑测试

```bash
pip install -e ".[dev]"
pytest tests/ -q
```

---

## 三个命令

| 命令 | 干什么 |
|---|---|
| `data-dog init` | 交互式接入 LLM 供应商（选供应商 → 填 key → 校验 → 保存到 `.env`） |
| `data-dog run` | 调查**一个**指定 JSON 候选，输出报告 |
| `data-dog test` | **交互窗**：候选清单挑 / 临时新建(n) / 导入自定义(i)，一轮对比多个 |

---

## 数据从哪来（诚实说明）

程序**当前不接真实遥测流**，证据来自 `src/data_dog/evidence/store.py` 写死的 mock 数据，但**设计成自洽且有现场感**：

- **左髋电机**：一整段真实升温过程（09:40 → 09:55 温度从 52°C 逐步爬到 78.2°C，超过 75°C 告警阈，电流同步抬升）——标准的**热退化**剧本。
- **右髋电机**：温度 61°C / 电流 6.2A，全在基线内 —— **误报**剧本（应排除）。
- **电池 BMS**：SOC 0.72，远超低压告警阈 —— **正常巡检**剧本。
- 附带厂商知识库（阈值/原因/安全上限）、邻居事件、近期变更（固件/算子升级）。

> **数据源一：仿真遥测（`data_dog/mujoco_sim/`）**。用 **MuJoCo** 加载 Go2 真实四足模型
> （`third_party/mujoco_menagerie/unitree_go2/scene.xml`）跑仿真，做受控小摆动，导出物理自洽的
> 关节力矩 / 电流，再叠加第 `simulate.MOTOR_PARAMS` 的一阶热模型得到电机温度。可注入 **5 类故障**
> （堵转 / 热退化 / 通信丢包 / 电流偏置 / 负载骤增），每条样本带 **ground truth**。Agent 调查后与真值
> 对比算准确率：`python -m data_dog.mujoco_sim.mujoco_eval`（当前 n=6，accuracy 1.0）。
>
> **数据源二：纯 NumPy 合成遥测（`data_dog/synth/`，无 MuJoCo 依赖的降级方案）**。基于 Go2 公开
> 物理参数用 NumPy 生成物理自洽的时间序列，同样带 5 类故障注入 + ground truth
> （`python -m data_dog.synth.synth_eval`）。
>
> 两个数据源都实现与 `EvidenceStore` 相同的查询接口 —— **换数据源只需替换合成层，Agent / 工具逻辑完全不用改**。

生产环境接真实设备时，`EvidenceStore` 换成真实时间序库 / 厂商知识库 / 事件流的实现即可，Agent / Tool 逻辑不用改。

---

## Agent 怎么工作的（30 秒版）

```
候选异常
  → AgentLoop 循环：想(Reason) → 调工具(Act) → 看结果(Observe) → 再想…
  → 工具全是只读 + 白名单部件（越权返回 denied）
  → 证据足够 → 输出 InvestigationReport；证据不足 → needs_human_review，不瞎猜
```

ReAct 轨迹 = Agent 一步步的行动脚印（调了哪个工具、传了什么参数、看到什么结果），CLI 用 rich 实时渲染，Web 界面用 Streamlit 渲染。

---

## 目录结构

```
src/data_dog/
  providers/    多供应商接入（DeepSeek/OpenAI/智谱）
  evidence/     证据库 + 在线检测算子
  tools/        受控调查工具（4 个只读）
  agent/        AgentLoop（调查主循环）
  schemas/      CandidateEvent / InvestigationReport
  correlation/  多事件关联
  reporter/     提出上报意图（A3，本地 outbox + 幂等 + Receipt）
  synth/        合成遥测生成器（Go2 物理参数 + 故障注入 + ground truth）+ 评估
  mujoco_sim/   MuJoCo 仿真数据源（simulate 跑 Go2 + to_sample 转样本 + mujoco_eval 评估）
  operator_factory/  离线算子工厂（文档 revision + 诚实知识抽取 + 受限 DSL + Fixture 回放）
  data_plane/   在线数据面（网关/兼容绑定/解析链/候选构建 + SSRF 安全引用）
  alerting/     上报闭环（确定性策略门 + Alert 生命周期 + 幂等 Outbox + 测试 SaaS）
  e2e/          整链回放（厂商文档→算子→候选→Agent→策略门→SaaS，一条命令）
  observability.py  可观测性（token 消耗 / 每步耗时 / 成本统计）
ui/             Streamlit 网页版
evals/          Agent 评估框架（7 case，含对抗性）
scripts/        数据清洗 / 算子生成 / RAG 落地脚本
schemas/        标准事件正式 Schema（JSON Schema，5 份）+ 版本演进策略 + 校验工具
docs/           设计文档（MuJoCo 仿真与输出解读、SLO 量化目标、受限 DSL 规范）
```

---

## 设计契约与规范（P0 设计底座）

对齐架构蓝图里"Agent 之外的层"所需的地基（详见仓库 `schemas/README.md`、`docs/slo.md`、`docs/dsl_spec.md`）：

| 产物 | 是什么 | 校验 |
|---|---|---|
| `schemas/*.json` | 5 类标准事件（RawLogEnvelope / NormalizedTelemetry / CandidateEvent / InvestigationReport / SaaS AlertEvent）的**正式 JSON Schema** | `python schemas/tools/validate_examples.py` |
| `schemas/README.md` | **版本演进策略**（兼容性定义 + 破环式变更升主版本）+ 与现有 dataclass 的字段映射 | — |
| `docs/slo.md` | 首期**量化目标**（吞吐/延迟/算子资源/质量/保留），全部为设计预算值，附推导与待校准项 | — |
| `docs/dsl_spec.md` | **受限 DSL v0.1** 规范：词法/表达式/白名单函数/窗口聚合/受限状态机/能力边界/升级判据 | — |

---

## P1 离线算子工厂（文档 → 算子，可跑可测）

```bash
python -m data_dog.operator_factory.p1_demo          # 人读输出
python -m data_dog.operator_factory.p1_demo --json   # 机器可读
pytest tests/test_operator_factory.py tests/test_operator_verification.py -q   # 27 项
```

**厂商文档 → 结构化知识（带 revision/来源/定位）→ 检测算子（声明式 + 受限 DSL）→ Fixture 黄金回放**。
实测：知识包 11 revisions / 61 字段 / 9 条数值规格 / 6 个错误码；回放 5/5 accuracy 1.0。

关键点（对齐架构 §8.2）：**只抽文档真有的规格，并区分"能证实 / 待确认"**——官方开源协议头（`unitree_actuator_sdk`，BSD-3）明写电机 **90°C 触发温度保护**，故标 `verified`；而 **75°C warning 阈无出处**，标 `unconfirmed` / `PROJECT ASSUMPTION`，**不冒充厂商事实**。受限 DSL 是自建的 词法→AST 解释器（无动态执行原语，函数闭集白名单，编译期类型检查）。来源与许可见 `vendor_docs/unitree/SOURCES.md`，详见 `docs/operator_factory.md`。

---

## P2 在线数据面（日志 → 候选异常，可跑可测）

```bash
python -m data_dog.data_plane.p2_demo          # 人读输出
python -m data_dog.data_plane.p2_demo --json   # 机器可读
pytest tests/test_data_plane.py -q             # 15 项
```

**原始日志 → 网关(注入身份/校验/限流) → 兼容绑定(失败关闭) → Parser→Normalizer→Enricher→Window→Detector → 候选异常**。
实测：6 条日志 → 4 条规范化、2 条失败关闭、1 条 critical 候选（带证据）。

关键点（对齐 §9.1/§9.2）：**身份只在网关注入**（正文自称的身份一律丢弃）；**拿不准就失败关闭**（绑定 `not_found`/`ambiguous`、认证 `denied`，绝不猜）；**热路径不调大模型**。大载荷经受控引用外置，取数前过 **SSRF 校验**（仅 http/https，拒绝 localhost/环回/私有/保留）。详见 `docs/data_plane.md`。

---

## P3 上报闭环（策略门 + 事件生命周期 + SaaS，可跑可测）

```bash
python -m data_dog.alerting.p3_demo          # 人读输出
python -m data_dog.alerting.p3_demo --json   # 机器可读
pytest tests/test_alerting.py -q             # 20 项
```

**调查结论 → 确定性策略门(§14.2 四条件+冲突消解) → 正式事件(同一 Episode 复用 event_id) → 幂等投递 SaaS → 反馈回填**。
实测：confirmed+严重码→上报 critical；rejected/缺证据→不上报（失败关闭）；同 Episode 更新 revision 递增、不重复投递。

关键点：**确定性决策表（不让模型判断上报）**、**Episode 语义**（同一故障同一 event_id + 幂等键，构成规则定死）、**失败关闭**、投递前**剔除禁止字段**。详见 `docs/alerting.md`。

> **至此整条链贯通**：厂商文档 →[P1]→ 算子 →[P2]→ 遥测/候选 → Agent 调查 →[P3]→ 策略门 → SaaS。

---

## E2E 整链回放（一条命令跑通全链，§19.3 验收路径）

```bash
python -m data_dog.e2e.replay_demo          # 人读输出
python -m data_dog.e2e.replay_demo --json   # 机器可读
pytest tests/test_e2e.py -q                 # 5 项
```

**厂商文档 →[P1]→ 算子 →[P2]→ 候选 →[Agent]→ 报告 →[P3]→ 策略门 → SaaS → 反馈**，全程离线确定性可复现。
实测：98°C 过温日志 → critical 候选 → Agent confirmed(0.9) → 上报 critical → SaaS 接收。
详见 `docs/e2e.md`（含回放中发现并修复的"跨模块硬编码常量"真实 bug）。
