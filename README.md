# data_dog —— 机器狗遥测故障调查 Agent

**一句话**：把机器狗的原始日志，变成一份**可上报、可追责、可复现**的故障结论——全链离线确定性，热路径不调大模型。

一个会**自己调查异常**的 Agent：喂给它一个候选异常，它会调用只读工具查遥测 / 厂商知识 / 邻居事件 / 近期变更，逐步推理、建立假设、寻找反证，最后输出一份结构化 `InvestigationReport`（裁决 + 置信度 + 假设 + 缺失证据 + 建议）。

核心目标不是"让模型答得聪明"，而是**让结论值得信**：数值只用有出处的、结论必须挂证据、拿不准就失败关闭。

---

## 亮点速览

| | 亮点 | 证据 |
|---|---|---|
| 🔗 | **全链闭环，一条命令跑通** | 厂商文档 →[P1]→ 算子 →[P2]→ 候选 →[Agent]→ 报告 →[P3]→ 策略门 → SaaS 反馈，`python -m data_dog.e2e.replay_demo` |
| 🧪 | **645 项测试，全离线确定性** | `pytest -q` → `641 passed, 4 skipped`（49 个测试文件，fake provider，无需 API Key、不联网；4 skipped 为未设 `DD_TEST_PG_URL` 的 PostgreSQL 集成用例 ×4 —— Redis 后端用例已由 `.[dev]` 里的 `fakeredis` 离线跑起来，不再 skip） |
| 🔒 | **热路径确定性：上报不交给模型判断** | 上不上报由**确定性策略门**（四条件决策表 + 冲突消解）决定，同输入永远同结论；模型只出现在"调查报告"那一步 |
| 🧱 | **不拿 `eval` 执行厂商字符串** | 自建**受限 DSL**：词法 → AST 解释器，无动态执行原语、函数闭集白名单、编译期类型检查 |
| 🔍 | **诚实抽取，区分"能证实 / 待确认"** | 官方 SDK 头文件明写电机 **90°C 触发保护** → `verified`；无出处的 75°C → `unconfirmed`，**不冒充厂商事实** |
| 🚪 | **fail-closed 贯穿全链** | 身份只在网关注入（正文自称一律丢弃）、兼容绑定拿不准就拒、错误语义收敛为有限错误码，**绝不降级为无鉴权** |
| 🔁 | **幂等上报** | `(幂等键, revision)` 复合主键，网络重试 / 进程重启都不会产生重复告警 |
| 🛡 | **安全设计落地** | SSRF 前置校验（仅 http/https、拒绝 localhost/环回/私有/保留，校验**全部解析 IP**；IP 未 pin 到连接，接线时需在连接层再校验）、`hmac.compare_digest` 常量时间比较、容器非 root、密钥不进镜像 |
| 📊 | **带 ground truth 的规模化评估** | **忠实 MuJoCo 数据集 10 万条**，分层抽 n=2000 跑真实 Agent：**准确率 0.818**（Wilson 95% CI 0.801–0.834）、**normal 误报率 0.064**、逐类召回一并如实报（**F4 偏置 0.43 为当前最低**）；另有 7 个评估 case（含 2 个对抗性：提示注入 / 身份伪装） |
| 🚚 | **能交付：服务 + 容器 + HTTPS** | FastAPI（liveness / readiness 分离）+ Dockerfile + compose（含 Caddy 自动申请 HTTPS）+ Makefile |

**规模**：源码 123 个 Python 文件 / 21708 行，测试 49 个文件 / 9389 行；前端 `web/` 为 Next.js 15 控制台（63 个 TS/TSX 文件）；5 份正式事件 JSON Schema；22 个 `make` 命令（另有 `help`）。

---

## 诚实边界（先说清楚，免得误会）

- **遥测接入支持真实流**，但**默认不开启**。`src/data_dog/stream/` 提供四种传输的流式源：**文件/日志尾随**（标准库，机器人本机日志最真实）、**HTTP 推送**（标准库）、**MQTT**、**Kafka**（后两者需可选依赖 `.[mqtt]` / `.[kafka]`）。配 `DD_STREAM_ENABLED=1` + `DD_STREAM_SOURCE=...` 即启用，走与 HTTP 入口**同一条**数据面。默认关闭是因为演示/CI 环境没有真实设备——**不是做不到**。评估集的证据仍来自 mock / 合成 / MuJoCo（可复现），故"真实误报率"仍需真实数据回灌才能定论。
- **多租户已加数据库强制层（PostgreSQL RLS）**。应用层：`tenant_id` 进复合主键、仓储强制过滤、跨租户读 404；数据库层：迁移 `a7b8c9d0e1f2` 对 8 张租户表启用 `ROW LEVEL SECURITY` + 策略（`USING`/`WITH CHECK`）。**适用于 PostgreSQL**；SQLite 无 RLS，本地开发仍是应用层隔离（诚实边界）。另有两条必须知道的边界：**superuser 与表属主默认绕过 RLS** → 生产连接须用**非属主、非 superuser** 角色；未设 `app.tenant_id` → 谓词为 NULL → **零行**（失败关闭）。真库验证见 `tests/test_storage_postgres.py`（建非特权角色，裸 SQL 断言跨租户读写都被挡）。
- **支持水平扩展**：有状态算子已**外置**到可插拔 `StateStore`（`mutate(key, fn)` 原子 RMW 原语）。后端：`memory`（默认，单副本正确）/ `postgres`（`state_kv` 表，行锁）/ `redis`（WATCH/MULTI/EXEC，需 `.[redis]`）。候选归并（`SharedCandidateBuilder`）与限流桶都在共享后端，故多副本归并/限流一致。**边界**：默认后端随数据库类型自动选（PG→postgres，否则 memory）；用 `DD_STATE_BACKEND` 显式指定；`DD_RATE_BACKEND` 可单独把限流切到 Redis（限流成热路径瓶颈时）。file/MQTT 源多副本会**重复消费**（Kafka 用消费者组、MQTT 用共享订阅主题可避免）。
- **P4 大工程八项已落地**（2026-09-29 重估后）：**P4-1 算子沙箱**、**P4-2 控制面**（Registry/Canary/回滚/签名）、**P4-3 通知/值班/升级 SLA**、**P4-4 K8s/Helm 编排**、**P4-5 车队级分片**、**P4-6 跨地域多活**（应用层）、**P4-7 退化趋势预测**、**P4-8 软实时基准**。各文档带**诚实边界**：硬实时 <1ms 仍为非目标（改述软实时）；多活 RPO>0（非同步复制）；沙箱在 Windows 无 seccomp（子进程 + 资源上限降级）；分片再平衡双读未实现（不声称 exactly-once）。设计见 `docs/p4/`。

> 接真实设备时：走流式源（file/http/mqtt/kafka）或 HTTP `/v1/telemetry`，Agent / 工具逻辑不用改。

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
python -m streamlit run ui/app.py           # 调查台：数据面产出候选 → 调查 → ReAct 轨迹 → 报告
python -m streamlit run ui/mujoco_page.py   # MuJoCo 演示页：点按钮跑仿真 → 遥测 → Agent 调查 → 准确率
```

`ui/app.py` 按**检测异常 → 诊断异常 → 待人工审核**三步走：

1. **描述问题**：输入关键词 → 检索出相关日志场景（像搜索建议）→ 选一个；
2. **检测**：把该场景的**原始日志**送进在线数据面（网关校验 → 兼容绑定 →
   解析归一 → 检测 → 候选合并）→ 产出候选，或**明确说明为什么没有候选**；
3. **诊断**：对候选跑 Agent 受控调查（ReAct 轨迹 + 结构化报告）；
4. **待人工审核**：报告是**建议**，最终由确定性策略门与人工确认。

**候选不是手写清单，而是真跑 P2 数据面产出的**；且候选与 Agent 查到的证据
**来自同一批原始日志**（单一事实源）。页面底部展开明细可看到全部场景的真实处理结果，
包括**产出 0 条候选**的情形（低于阈值不误报、厂商无阈值则不编规则、固件不匹配则失败关闭）。

```bash
pytest tests/test_scenarios.py -q   # 17 项：锁定候选来源、证据对齐、问题检索
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

**遥测接入已支持真实流式源（见上文「诚实边界」，默认关闭）**；本节的示例证据默认来自 `src/data_dog/evidence/store.py` 的 mock 数据，**设计成自洽且有现场感**：

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
> **在其之上还有一层「忠实仿真数据集」（已生成 10 万条正式集）**：自写 trot 步态控制器让狗
> **自由基座真走路**，叠加真实噪声模型与 5 类故障，批量生成带 ground truth 的 trial，
> 喂给**同一个** Agent 做规模化评估。
> - **规模**：`data/mj_60s_100k` —— **100,000 条**（60s/条，100 Hz，35,198s≈9.8h，8 worker），
>   400 分片，**100% free_trot**（自由基座真走路，非吊挂/微摆），**0 翻倒**，53 MB（仅物化标量汇总）。
> - **评估实测**（分层抽 **n=2000**，确定性 provider，跑真实 Agent 调查）：
>   **准确率 0.818**（Wilson 95% CI 0.801–0.834）；逐类召回
>   F1 堵转 **1.00** / F5 负载 **0.96** / F3 丢包 **0.70** / F2 温升 **0.69** / F4 偏置 **0.43**；
>   **normal 误报率 0.064**。
> - **命令**：生成 `python -m data_dog.mujoco_sim.gen_dataset`；评估 `python -m data_dog.synth.dataset_eval`。
> - **诚实边界**（不夸大）：① 仿真 ≠ 真机，**真实误报率仍需真机数据回灌**才能定论；
>   ② **F4 召回偏低源于评估器两处缺陷（均已定位并修复）**——(a) **参照窗与故障窗重叠**：旧检测器取
>   「全窗电流均值 − **前半段(0–50%)**参照均值 > 1.5A」，而 F4 注入窗 `[40%, 90%]` 与其**重叠**，+5A 偏置漏进参照被稀释；
>   修法是把参照窗移到**故障起点之前**（`REF_WINDOW_END_FRAC=0.35 < FAULT_START_FRAC=0.4`），**只改观测口径、不动仿真**；
>   (b) **证据白名单漏了 `front_right_knee`**：该部件（占总故障 ~25%）查证据被拒 → 走"无证据"分支 →
>   对故障样本是**免费命中**，**虚高**了历史分数。两处修复后，**权威准确率＝0.818**——
>   **低于此前被虚高的 0.879/0.844**：这是**暴露旧数虚高、而非 Agent 退化**（同种子单变量对照已核实）。
>   normal 误报率**保持 0.064**（未提高灵敏度）；残余 F4 未命中源于偏置与 trot 电流自然漂移**同量级**——
>   诚实上限，非判据可再压。**与信号强弱无关**（正常 trial 电流峰值同样达 ~94A）；
>   ③ 评估器判据（热阈值、电流参照窗口径）原按合成数据标定/设定，忠实档需**重新标定**，**绝不改仿真迁就分数**。
>   保真度档次、假设值清单见 [docs/mujoco_dataset.md](docs/mujoco_dataset.md)。
>
> **数据源二：纯 NumPy 合成遥测（`data_dog/synth/`，无 MuJoCo 依赖的降级方案）**。基于 Go2 公开
> 物理参数用 NumPy 生成物理自洽的时间序列，同样带 5 类故障注入 + ground truth
> （`python -m data_dog.synth.synth_eval`）。
>
> 两个数据源都实现与 `EvidenceStore` 相同的查询接口 —— **换数据源只需替换合成层，Agent / 工具逻辑完全不用改**。

生产环境接真实设备时，**接入侧**走 `stream/`（file / http / mqtt / kafka）或 HTTP `/v1/telemetry`；**证据侧**把 `EvidenceStore` 换成真实时间序库 / 厂商知识库的实现即可。Agent / Tool 逻辑不用改。

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
  tools/        受控调查工具（5 个只读：遥测/厂商知识/邻居事件/近期变更/因果链）
  agent/        AgentLoop（调查主循环）
  schemas/      CandidateEvent / InvestigationReport
  correlation/  多事件关联
  reporter/     提出上报意图（A3，本地 outbox + 幂等 + Receipt）
  synth/        合成遥测生成器（Go2 物理参数 + 故障注入 + ground truth）+ 评估（synth_eval 小集 / dataset_eval 数据集规模评估）
  mujoco_sim/   MuJoCo 仿真数据源（conditions 条件网格 + gait trot 步态 + simulate 跑 Go2 + noise 噪声层 + gen_dataset 分片并行生成 + to_sample 转样本 + mujoco_eval 评估）
  operator_factory/  离线算子工厂（文档 revision + 诚实知识抽取 + 受限 DSL + Fixture 回放）
  data_plane/   在线数据面（网关/兼容绑定/解析链/候选构建 + SSRF 安全引用）
  alerting/     上报闭环（确定性策略门 + Alert 生命周期 + 幂等 Outbox + 测试 SaaS）
  e2e/          整链回放（厂商文档→算子→候选→Agent→策略门→SaaS，一条命令）
  storage/      持久化（SQLAlchemy：本地 SQLite / 生产 PostgreSQL）+ 仓储层
  service/      HTTP 服务（FastAPI：探针 + 遥测接入 + 调查/查询端 + P3 落库接线）
  knowledge.py          向量知识库客户端（Chroma，厂商知识检索）
  knowledge_ingest.py   用户文档导入的纯逻辑（切片 + 词法检索，无外部依赖）
  eval_feedback.py      反馈回灌：人工标签 vs Agent 处置 → 一致率/误报率/漏报率
  settings.py   配置集中（纯 os.environ，调用时惰性读取，不缓存）
  paths.py      资源定位（资源位置外部可注入，防"打包后路径失联"）
  observability.py  可观测性（token 消耗 / 每步耗时 / 成本统计）
ui/             Streamlit 网页版
evals/          Agent 评估框架（7 case，含对抗性）
scripts/        数据清洗 / 算子生成 / RAG 落地脚本
schemas/        标准事件正式 Schema（JSON Schema，5 份）+ 版本演进策略 + 校验工具
docs/           设计文档（架构总览、落地交付、各子系统、SLO、受限 DSL 规范）
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
pytest tests/test_operator_factory.py tests/test_operator_verification.py -q   # 28 项
```

**厂商文档 → 结构化知识（带 revision/来源/定位）→ 检测算子（声明式 + 受限 DSL）→ Fixture 黄金回放**。
实测：知识包 11 revisions / 61 字段 / 9 条数值规格 / 6 个错误码；回放 6/6 accuracy 1.0。

关键点（对齐架构 §8.2）：**只抽文档真有的规格，并区分"能证实 / 待确认"**——官方开源协议头（`unitree_actuator_sdk`，BSD-3）明写电机 **90°C 触发温度保护**，故标 `verified`；而 **75°C warning 阈无出处**，标 `unconfirmed` / `PROJECT ASSUMPTION`，**不冒充厂商事实**。受限 DSL 是自建的 词法→AST 解释器（无动态执行原语，函数闭集白名单，编译期类型检查）。来源与许可见 `vendor_docs/unitree/SOURCES.md`，详见 `docs/operator_factory.md`。

---

## 真实 RAG 知识检索（可选，默认关闭）

厂商知识不只是"写死的条目"：把**真实公开厂商资料**（官方 Go2 用户手册、官方 SDK 的 IDL 字段定义、第三方诊断指南，共 10 份，经 MarkItDown 转文本）切块灌进 **Chroma 向量库**，让 Agent 的工具 `query_vendor_knowledge` 走**语义检索**。

- 链路：MarkItDown（文档解析）→ Chroma（`chroma-mcp` 常驻连接）→ `query_vendor_knowledge`
- 开关：设 `DD_KB_CHROMA=1` 启用；**取不到就降级回内置条目并标注 `retriever`**，不静默假装是检索结果
- 落地脚本：`scripts/chroma_ingest.py`；来源与许可见 `vendor_docs/unitree/SOURCES.md`

---

## 工程可靠性：一次"静默失败"的发现与防护

本地跑得好好的，**装进容器后悄悄少了一条检测规则**——不报错、不崩溃，只是结果不对。根因是代码用"文件往上数三层"去推配置目录，本地对、安装后路径变了就找不到。

- **识别类别**：这是**静默失败**，比崩溃更危险——**单测全绿也抓不到**，因为它不抛异常。
- **修法**：资源位置改为**外部可注入**（`data_dog/paths.py` + 环境变量），不再让代码猜路径。
- **防回归**：`tests/test_paths.py`（7 项）锁定"**降级必须可观测**"。

---

## P2 在线数据面（日志 → 候选异常，可跑可测）

```bash
python -m data_dog.data_plane.p2_demo          # 人读输出
python -m data_dog.data_plane.p2_demo --json   # 机器可读
pytest tests/test_data_plane.py -q             # 20 项
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

---

## 持久化（SQLAlchemy：本地 SQLite ↔ 生产 PostgreSQL）

```bash
pytest tests/test_storage.py -q     # 14 项
```

核心对象（遥测事件 / 候选 / 调查报告 / Alert / Outbox / 反馈）**落库，重启不丢**。
**同一份代码，本地默认 SQLite（零安装），生产设 `DD_DATABASE_URL` 切 PostgreSQL，代码不改。**
```bash
export DD_DATABASE_URL="postgresql+psycopg://user:pw@host:5432/data_dog"   # 生产
```
安全：全部走 ORM 绑定参数，无字符串拼接 SQL；`alert_outbox` 用 `(幂等键, revision)` 复合主键强制去重。
详见 `docs/storage.md`。

---

## 落地交付：在线服务（HTTP + Docker）

把 P2 数据面跑成**可长期运行、可探活、可被外部接入**的服务。

```bash
# 本机跑（默认本地 SQLite，零安装）
pip install -e ".[dev,postgres]"
python -m data_dog.service                 # 起在 0.0.0.0:8000

# 或一条命令起整套（应用 + PostgreSQL）
docker compose up -d --build
docker compose ps
```

端点：

| 端点 | 方法 | 作用 |
|---|---|---|
| `/healthz` | GET | 存活探针（进程活着即 200，不查依赖） |
| `/readyz` | GET | 就绪探针（数据库不通 → 503，摘流量不重启） |
| `/metrics` | GET | 进程内指标（全局运维口径，不分租户） |
| `/v1/telemetry` | POST | 接入原始日志 → 候选异常 → 落库 |
| `/v1/summary` | GET | 聚合：候选 / 告警按状态 / 出箱各计数 |
| `/v1/candidates` `/v1/alerts` `/v1/outbox` `/v1/reports` | GET | 分页查询（租户内） |
| `/v1/candidates/{id}` | GET | 候选项详情 |
| `/v1/candidates/{id}/evidence` | GET | Agent 所见证据（指标 / 参考窗口 / 厂商知识） |
| `/v1/candidates/{id}/refs` | GET | 证据引用（挂到结论的出处） |
| `/v1/investigations` | POST | 对候选跑 Agent 受控调查 → 持久化报告 |
| `/v1/investigations/{id}` | GET | 读持久化报告 |
| `/v1/feedback` | POST | 人工反馈（误报 / 已修复 / 无需动作）→ 驱动告警状态机 |
| `/v1/alerts/{id}/feedback` | GET | 读某告警的反馈历史 |
| `/v1/eval/feedback` | GET | 反馈回灌：人工标签 vs Agent 处置 → 一致率 / 误报率 / 漏报率 |
| `/v1/knowledge/documents` | POST / GET | 导入 / 列出用户厂商文档（切片落库） |
| `/v1/knowledge/search` | GET | 词法检索用户文档切片 |
| `/v1/knowledge/documents/{id}` | DELETE | 删除文档及其切片 |

身份**只认请求头** `X-Tenant-Id` / `X-Device-Sn`，正文与查询串里的身份字段一律被忽略（与 P2 网关失败语义一致）；缺租户头 → 401，跨租户读 → 404（不泄露存在性）。

```bash
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/readyz
```
`make probe` / `make ingest` 封装了这些命令。

```bash
pytest tests/test_service.py -q
```

**给别人用**：先设 `DD_API_KEY`（不然端口开放=任何人可读写）。

- **同一局域网**：把 `http://<你的局域网IP>:8000` 给对方，调用带请求头 `X-API-Key`。
- **不在局域网**：临时演示用隧道（cloudflared/ngrok/cpolar）；正式对外部署到云主机：

```bash
cp .env.example .env && vi .env      # 填 DD_DOMAIN / ACME_EMAIL / DD_API_KEY / POSTGRES_PASSWORD
make up-prod                         # = compose.prod.yaml：Caddy 自动申请 HTTPS，只开 80/443
```

`compose.prod.yaml` 里应用与数据库都**不对外发布端口**，只经 Caddy 进入。

详见 `docs/delivery.md`（含存活/就绪为何分开、每请求新建 DataPlane 的取舍、
不在局域网的三条路、以及还不是生产级的项）。

---

## 文档地图

| 想了解 | 看 |
|---|---|
| **全局架构（分层 / 依赖 / 数据流）** | `docs/architecture.md` |
| 落地交付（服务怎么跑、Docker、生产 compose） | `docs/delivery.md` |
| P1 离线算子工厂 | `docs/operator_factory.md`、`docs/dsl_spec.md` |
| P2 在线数据面 | `docs/data_plane.md` |
| P3 上报闭环 | `docs/alerting.md` |
| Agent 降级路径 | `docs/degradation.md` |
| 持久化 / 多租户 | `docs/storage.md` |
| 整链回放 | `docs/e2e.md` |
| SLO 量化目标 | `docs/slo.md` |
| 事件 Schema 与版本策略 | `schemas/README.md` |
| MuJoCo 数据源 | `docs/mujoco_sim.md` |
