# 在线故障覆盖矩阵（按子系统）

> 规则定义：`src/data_dog/operator_factory/p1_demo.py`
> 在线实测：`python scripts/online_fault_coverage.py`（走真实 `DataPlane.process_one`）
> 黄金用例：`python -m data_dog.operator_factory.p1_demo`（22 条，正例+反例）
> 字段出处：`vendor_docs/unitree/cleaned/`（IDL 与协议头）

## 先看结论

**能报的**：本文件表格里"有规则"的那些行，都跑过在线链实测，命中率与首次报出延迟见下表。

**不能报的**，有下面几类，性质完全不同，必须分开看：

1. **遥测里根本没有信号的物理损伤**——外壳裂纹、螺丝松动、线缆破皮但还没影响电信号。
   这类不是"我们没做"，是"现有传感器看不见"，属于采集能力边界，不在检测层职责内。
2. **原始 IDL 路径上数组字段进不来**（`Parser._coerce` 不展平）——见 §"摄取边界 2"。
   **注意：仿真路径不受这条限制**，它已经把数组拆成标量了（§"摄取边界 3"）。
3. **链路两头各缺一半**——仿真侧还没把非电机子系统发进信封，检测侧也还没声明
   IMU/整机算子字段。这是**未开工**，不是做不了；见 §"摄取边界 3"。
4. **字段能进来、但物理上分不开**——见 §"分不开的情况"，硬做只会造出互相抢报的假规则。

## 改前 / 改后对照（同一批数据，同一套链路）

把改前的提交（`9ae5469`）检出到临时 worktree，用**同一个探针脚本**喂同样的合成数据，
量的是"故障窗内有多少帧报出了规则"。方法：`git worktree add --detach <tmp> 9ae5469`，
探针按 `inspect.signature` 判断 `detect` 是否收窗口，两版共用同一份调用代码。

| 故障 | 改前窗内触发率 | 改前命中规则 | 改后窗内触发率 | 改后命中规则 |
|---|---|---|---|---|
| normal | 0.0% | （无） | 0.0% | （无） |
| F1_stall | **70.0%** | TEMP_PROTECT | **95.2%** | STALL_SUSTAINED + TEMP_PROTECT |
| F2_temp_rise | **0.0%** | （无） | **51.9%** | TEMP_DEGRADE |
| F3_packet_loss | **0.0%** | （无） | **97.1%** | COMM_PACKET_LOSS |
| F4_current_bias | **0.0%** | （无） | **92.6%** | CURRENT_BIAS |
| F5_load_spike | **42.1%** | TEMP_PROTECT | **93.3%** | LOAD_SPIKE + TEMP_PROTECT |

两条要说清楚：

1. **F1/F5 改前不是"完全检不出"，是"检出了但报错原因"**。改前只有 `temperature ≥ 90°C`
   一条有效判据，堵转/重载把温度烧过 90°C 后靠温度报出来——报的是"过热"，
   不是"堵转"。运维看到的是 B 子系统告警，会去查散热，不会去查电机。改后 A/F 各自的
   规则先报出来，温度告警退为并发现象。
2. **F2/F3/F4 改前是真的一点都检不出**（0.0%）。这三类的信号全在窗口统计量里——
   温度斜率、窗口覆盖率、电流与扭矩的残差——单点阈值判据看不见。改前
   `Detector.detect(metrics)` 的签名里就没有窗口，所以参数再怎么调都不可能命中。

## 规则 × 子系统

| 子系统 | rule_id | 判据 | 阈值出处 | 在线实测 |
|---|---|---|---|---|
| A 关节/电机 | `MOTOR_STALL_SUSTAINED` | ≥5s 窗口内 ≥50% 时间 I≥90A | 假设 | F1 **95.2%**，5.9s 报出 |
| A 关节/电机 | `MOTOR_CURRENT_BIAS` | `mean(I) − mean(τ)/0.25 ≥ 4A` | 假设 | F4 **92.7%**，7.3s 报出 |
| A 关节/电机 | `MOTOR_ENCODER_JUMP` | 采样率≥50Hz 且单步 \|Δq\|≥3.0rad | 假设 | 仅 fixture |
| A 关节/电机 | `MOTOR_MODE_FLAG_SET` | `(mode & 0x04) != 0` | 假设（位语义 verified） | 仅 fixture |
| B 热 | `MOTOR_TEMP_PROTECT` | `temperature ≥ 90°C` | **verified** | 随 F1/F5 连带命中 |
| B 热 | `MOTOR_TEMP_DEGRADE` | `mean T≥40 且 slope≥0.25 且 mean I<50` | 假设 | F2 **51.8%**，31.5s 报出 |
| B 热 | `MOTOR_TEMP_RISE_UNDER_LOAD` | `mean T≥75 且 mean I ≤1.2×基线` | 假设 | 仅 fixture |
| B 热 | `FIELD_SANITY_TEMP_INVALID` | `temperature < 0` | 假设 | 仅 fixture |
| C 电源 | `POWER_SOC_LOW` | `bms_soc ≤ 15%` | 假设（字段 verified） | 仅 fixture |
| E 通信 | `COMM_PACKET_LOSS` | 窗口覆盖率 < 95% | 假设 | F3 **97.0%**，3.9s 报出 |
| E 通信 | `MOTOR_LOST_COUNTER_RISING` | 窗口内 `Δlost > 0` | 假设 | 仅 fixture |
| F 整机/步态 | `MOTOR_LOAD_SPIKE` | `mean I≥50 且 max I<90 且 mean τ ≥1.5×基线` | 假设 | F5 **93.3%**，7.0s 报出 |

"仅 fixture" = 该项目前只用黄金用例验证了判据逻辑，**没有**进在线端到端实测——
因为项目自带的合成器（`synth/generator.py`）只注入 F1~F5 五类故障，没有编码器跳变、
SOC 掉电、lost 计数器这几类的物理注入器。这类规则要么补注入器，要么等真机数据。

## 摄取边界（实测确认，不是推测）

### 1. BMS 总线电流与电机电流同名不同义

`BmsState.current`（int32，电池总线电流）与载荷里的 `current`（每关节电机电流，
项目自定义协议）**同名不同义**。若把总线电流也声明成 `source="current"`，
Parser 会把电机电流的值写进 `bms_current`——实测 95A 的电机电流被原样复制过去。

**当前处理**：总线电流不进算子，列为需采集侧补字段（采集侧须用独立键名如
`bus_current` 上报）。`bms_soc` / `bms_status` / `bms_cycle` 三个无冲突的标量已接入。

### 2. 数组型字段进不来（仅限**原始 IDL 路径**）

`Parser._coerce` 不展平数组，`WindowBuffer` 也只收标量。所以**直接从 IDL 结构体
反序列化**的路径上，数组字段无法参与窗口/基线判据：

| 结构体 | 字段 | 影响的子系统 |
|---|---|---|
| IMUState | `quaternion` `gyroscope` `accelerometer` `rpy` | D 姿态 |
| BmsState | `bq_ntc` `mcu_ntc` `cell_vol` | C 电源（电芯均衡/过温） |
| LowState | `fan_frequency` `foot_force` `foot_force_est` `motor_state` `wireless_remote` | B 热 / F 整机 |
| SportModeState | `foot_force` `foot_position_body` `foot_speed_body` `position` `velocity` `range_obstacle` | F 整机步态 |
| LowState | `head` `version` `sn` `reserve` | — |

**但仿真路径不受此限**——见下一节，这条以前被写错过。

### 3. 仿真路径已经在吐标量了（此前结论有误，已更正）

先说清错在哪：本文件早先写"IMUState 四个字段全是数组、唯一标量是 `temperature`，
所以 D 子系统一条规则都做不了，是数据进不来"。**这个结论对原始 IDL 路径成立，
对仿真路径不成立。**

`go2_sim/subsystems.py` 的 `ImuState.as_metrics()`（约 202-215 行）早就把四个数组
拆成了标量，`rig.py:227 flat_metrics()` 再把各子系统压成扁平字典：

| 子系统 | 仿真侧已产出的标量 |
|---|---|
| D 姿态/IMU | `roll` `pitch` `yaw` `gyro_x` `gyro_y` `gyro_z` `accel_x` `accel_y` `accel_z` `accel_norm` `imu_temp` |
| F 整机/步态 | `gait_type` `body_height` `yaw_speed` `foot_raise_height` `foot_slip_max` |
| C 电源 | `soc` `power_v` `current` `power_a` `bq_ntc` `mcu_ntc` `cycle` `status` `cell_v_max` `cell_v_min` `cell_v_spread` |
| B 热 | 热管理子系统同样有 `as_metrics()` |

**⚠ 加多子系统信封时必踩的一个坑**：`BatteryState.as_metrics()` 里有个
`"current"`，那是**电池总线电流**；而电机载荷里的 `current` 是**每关节电机电流**。
两者同名。如果多子系统信封直接 `payload.update(flat_metrics())`，总线电流会把
电机电流覆盖掉（或反过来），下游判据全错——这正是"摄取边界 §1"那个撞名问题
在仿真侧的同一副面孔。所以仿真侧发多子系统时必须**分键名**（如 `bus_current`），
不能共用一个扁平字典。

**所以 D 子系统 0 条规则的真实卡点不是 Parser，是另外两处**：

1. `go2_sim/emit.py` 的 `payload_for()` 只序列化电机行（`component_row()`
   返回的是 MotorState 那 8 个字段），IMU/电池/热管理/整机的指标没有进信封。
   要发出去必须加**多子系统信封**——这是仿真侧的事。
2. `operator_factory/` 里没有任何 IMU 字段声明（精确 grep
   `"gyro_x"|"accel_x"|"roll"|...` 零命中），所以就算数据到了也没有算子接。
   这是检测侧的事。

**两边各做一半，不要给 Parser 打洞**：仿真侧把多子系统发进信封，检测侧声明
IMU 算子字段并写规则。本文件先前提出的"给 Parser 加数组展平"不是必要条件，
也不是首选——展平数组会改变所有既有算子的字段语义，代价远大于收益。

### 4. 订阅说明

`soc` / `status` / `cycle` 之外，`BmsState.current`（总线电流）的撞名问题见上一节，
仍待采集侧用独立键名解决。

## 分不开的情况（物理上不可区分）

**F2 热退化 vs F4 电流零漂**：合成器给 F2 注入的是"散热变差 + 持续大负载"，
所以 F2 同时抬高温度和电流；F4 是纯电流偏置。两者靠"电流是否远超扭矩"区分：
F4 的残差 `I−τ/kt` 恒为 +5A，F2 的残差 ≈0。但只要现场出现"真实大负载 + 散热不良"
（比如爬长坡），残差和温度会同时满足两条规则——此时报出的规则集合是并集，
需要下游按子系统聚合判断，不能指望规则本身互斥。

**验证过的一点**：正常样本 1230 条全程零命中；五类故障各自只命中本子系统规则
（F1/F5 额外连带 `MOTOR_TEMP_PROTECT`，这是真实的物理耦合——堵转确实会过热）。

## 降级语义（缺数据时不误报）

三条，都在代码里显式写了，不是隐含行为：

1. **样本不足**：窗口内 <8 条样本，一切窗口判据不命中（`_win()` 统一加前置条件）。
2. **基线缺失**：`window.baseline()` 返回 None，与之比较的规则在求值期抛错 →
   `evaluate()` 视为不命中。**绝不退化成"基线=0"**（那会把每条正常读数判成异常）。
3. **窗口缺失**：`series` 为空时所有 DSL 规则直接跳过，不拿单条 metrics 冒充窗口。

实测：拿 `load_spike_positive` 的正例，有基线时命中、无基线时**不命中**且不抛异常。

## 复现

```bash
python -m data_dog.operator_factory.p1_demo          # 22 条黄金用例
python scripts/online_fault_coverage.py              # 在线链 5 类故障实测
python scripts/online_fault_coverage.py --json       # 机器可读
```
