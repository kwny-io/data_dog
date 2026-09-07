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
python -m streamlit run ui/app.py
```

### 跑一下 Agent 评估（可选）

```bash
python -m evals.run_evals --mode mock    # 离线确定性评估，5 case
python -m evals.run_evals --mode real    # 用真实模型评估（需先 data-dog init）
```

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
ui/             Streamlit 网页版
evals/          Agent 评估框架（5 case）
scripts/        数据清洗 / 算子生成 / RAG 落地脚本
```
