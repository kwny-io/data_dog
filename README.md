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
  observability.py  可观测性（token 消耗 / 每步耗时 / 成本统计）
ui/             Streamlit 网页版
evals/          Agent 评估框架（7 case，含对抗性）
scripts/        数据清洗 / 算子生成 / RAG 落地脚本
```
