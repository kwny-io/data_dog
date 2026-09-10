# MuJoCo 仿真数据源 —— 步骤与术语

> 状态：已完成（能出遥测数据 + 接进现有 Agent 评估）
> 一句话：**让 Go2 在 MuJoCo 里跑出物理自洽的遥测，替换掉纯 NumPy 合成数据源，Agent 逻辑不动。**

---

## 1. 这个东西解决了什么

机器狗没有真实遥测流，项目需要"物理自洽、带真实标签"的数据喂给调查 Agent。
原来用纯 NumPy 合成（`synth/`），那只是"仿真出来自己算"。现在用 **MuJoCo** 真正跑 Go2 的
物理模型，得到关节受力/运动的真实物理结果，再叠加热模型得到温度。这是从"算出来的数据"
升级到"仿出来的数据"——对简历是更硬的数据源卖点。

**边界（诚实说明）**：MuJoCo 给的是关节位置/速度/力矩，**它不含电机温度**。
所以温度用 `simulate.MOTOR_PARAMS` 的一阶热模型（`dT=(I²R - (T-T_amb)/Rth)/Cth·dt`）估算。
这是层叠关系：MuJoCo 提供动力学，热模型补温度，二者都是物理模型。

## 2. 三步链路（代码结构）

```
third_party/mujoco_menagerie/unitree_go2/scene.xml   ← Go2 四足模型（本体+地面）
        │
        ▼ simulate.py
MuJoCo 跑受控小摆动 → 导出 12 关节 pos/vel/torque + 热模型温度 (SimResult)
        │
        ▼ to_sample.py
把 mujoco 关节映射到 Agent 认识的部件名 → 注入 5 类故障 → 转 SynthSample
        │
        ▼ mujoco_eval.py
喂给 InvestigationTools / AgentLoop → 与 ground truth 对比 → 准确率 X/N
```

### 模块文件
- `simulate.py` —— 核心仿真。加载 `scene.xml`，PD 控制做小幅摆动，推进仿真，每步记录
  关节位置/速度/力矩，并用热模型更新 12 个电机温度。可直接运行：
  `python -m data_dog.mujoco_sim.simulate --dur 30`
- `to_sample.py` —— 把 `SimResult` 转成 `synth.SynthSample`，并支持 5 类故障注入 + ground truth。
  关键：把 mujoco 执行器名映射到 Agent `ALLOWED_COMPONENTS` 认识的名字。
- `mujoco_eval.py` —— 评估入口：跑 6 段仿真（正常 + 5 类故障），Agent 逐个调查，算准确率：
  `python -m data_dog.mujoco_sim.mujoco_eval`（当前 n=6, accuracy 1.0）

## 3. 复现步骤（照做即可）

```bash
cd D:\data_dog

# 1) 装 MuJoCo（3.x，已经在 venv 里）
.venv\Scripts\python.exe -m pip install mujoco

# 2) 确认模型能加载
.venv\Scripts\python.exe -c "import mujoco; m=mujoco.MjModel.from_xml_path('third_party/mujoco_menagerie/unitree_go2/scene.xml'); print('OK', m.nu, 'actuators')"

# 3) 跑一段仿真，看关节遥测（力矩/电流/温度）
.venv\Scripts\python.exe -m data_dog.mujoco_sim.simulate --dur 30

# 4) 跑完整 Agent 评估（默认 30s/段，正常+5故障=6 段）
.venv\Scripts\python.exe -m data_dog.mujoco_sim.mujoco_eval          # 打印明细
.venv\Scripts\python.exe -m data_dog.mujoco_sim.mujoco_eval --json   # 只输出 {n,hit,accuracy}
```

## 4. 为什么故障要落在"大腿(thigh)"上而不是膝盖(calf)？—— 一个物理细节

这是本模块最容易踩的坑，值得记：

- **站立四足的膝盖(calf)本就承重**，关节力矩饱和（到达限幅 ~45 N·m / 电流 ~90A）。
  在"已经满载"的信号上再注入故障，下游 Agent 看不出"相对正常"的差异 → 误报/漏报。
- **髋(hip)偏摆几乎不发力**（电流 ~2A），信号太弱，也看不出故障。
- **大腿(thigh/屈髋)有真实负载但不过饱和**（基线电流 ~6A、有裕量）——这是让
  "健康=正常电流、注入故障=异常"能被 Agent 判出的诚实关节。

所以在 `to_sample._MUJOCO_TO_COMPONENT` 里，把 `FL_thigh / FR_thigh` 映射到
Agent 认识的 `joint_motor.front_left_knee / front_right_knee`。故障落在它上面。
**结论：仿真数据源的"故障可检测性"取决于选一个不过饱和、又有信号的关节，而不是随机挑。**

同理，**热故障需要较长的仿真窗口**：电机热容 Cth=900 J/K，2 秒来不及升温，
必须跑 30 秒左右热模型才有物理意义的爬升（这也是 `mujoco_eval --dur` 默认 30 的原因）。

## 5. 术语（新增/澄清）

| 术语 | 解释 |
|---|---|
| **MuJoCo (mujoco)** | 物理仿真引擎。`mujoco 3.12.0` 是 Python 包；加载 `.xml`(MJCF) 模型跑动力学。 |
| **MJCF (.xml)** | MuJoCo 的模型文件格式。`scene.xml` 是场景（含地面），`go2.xml` 是本体（被 scene include）。 |
| **actuator / joint** | actuator=执行器/电机，joint=关节自由度。`go2.xml` 有 12 个电机（4腿×hip/thigh/calf）。 |
| **SimResult** | `simulate.py` 导出的原始仿真结果（ts/pos/vel/torque/current/temperature/base_pos）。 |
| **SynthSample** | `synth.generator` 里的统一遥测样本结构，Agent 评估消费它。mujoco 数据要转成它才能接入。 |
| **ground truth** | 每条样本的"正确答案"标签（正常/哪类故障），评估用它跟 Agent 裁决对比算准确率。 |
| **一阶热模型** | `dT=(I²R - (T-T_amb)/Rth)/Cth·dt`，由电流I、电阻R、热容Cth、热阻Rth 算出温度变化。 |
| **PD 控制** | 比例-微分控制：`τ=kp*(目标-实际) - kd*速度`，让关节跟住目标角度的简单控制。 |
| **故障可检测性** | 一个故障信号能否被下游阈值/Agent 判出来，取决于它是否落在"有信号、不过饱和"的量上。 |

## 6. 想跑真实 LLM？

`mujoco_eval` 目前用**确定性 Provider**（按物理阈值裁决），所以准确率可复现、可写简历。
确定性 Provider 在 `mujoco_eval.MujocoDeterministicProvider`，它"真读遥测、按温度/电流阈值裁决"，
逻辑与 `synth.synth_eval.DeterministicSynthProvider` 同源——**接真实 LLM 时只需把 provider 换成
真实模型实例即可**（`data_dog/providers/` 已有多供应商接入），其余评估流程不变。
