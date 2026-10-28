# 在线故障覆盖矩阵（按子系统）

> 规则定义：`src/data_dog/operator_factory/p1_demo.py`
> 在线实测（合成器）：`python scripts/online_fault_coverage.py`
> 在线实测（MuJoCo 仿真）：`python scripts/go2_sim_coverage.py`
> 黄金用例：`python -m data_dog.operator_factory.p1_demo`（25 条，正例+反例）
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
5. **物理上根本没有那个信号**——静止姿态不发热，所以"散热变差"在站立/趴卧上测不出来。
   （整包丢包曾属此类，现已用包级判据解决，见 §"F3 为什么以前测不出来"。）

## ⚠ 两套数据源的结果不同，不要混着引用

本项目的在线实测有两个数据源，**它们对同一类故障的建模强度本身就不同**，
所以同一套规则的命中率必须分开报，合成一个数字是误导：

| | `synth/generator.py` | `go2_sim`（MuJoCo） |
|---|---|---|
| 性质 | 纯扭矩 + 一阶热模型，**没有动力学** | 接触动力学 + 电池/热/通信子系统 |
| `dq` | 不产出，按故障类型约定（见脚本注释） | 真实解算 |
| τ 符号 | 全正（负载单向） | **正负交替**（步态摆动） |
| 用途 | 规则逻辑的回归基线 | **演示真正跑的那条链** |

**为什么必须两边都测**：`synth` 上全绿的规则，在 `go2_sim` 上曾 100% 误报——
因为 `synth` 的 τ 全为正，`mean|τ|` 与有符号均值几乎相等，那个"绝对值 vs 有符号"
的口径错配在 `synth` 上根本暴露不出来。只在 `synth` 上标定 = 只测了一半。

## 规则 × 子系统（`synth` 数据源）

5 个 seed（0–4），120s，故障窗 40%~90%，判定看**命中哪条规则**。

| 子系统 | rule_id | 判据 | 阈值出处 | 在线实测 |
|---|---|---|---|---|
| A 关节/电机 | `MOTOR_STALL_SUSTAINED` | ≥5s 窗口内 ≥50% 时间 I≥14.5A **且** 50% 时间 \|dq\|<0.05 | 假设 | F1 **99.8%**，3.1s 报出 |
| A 关节/电机 | `MOTOR_CURRENT_BIAS` | `mean(I) − mean_abs(τ)/1.5825 ≥ 4A` | 假设 | F4 **97.6%**，4.0s 报出 |
| A 关节/电机 | `MOTOR_ENCODER_JUMP` | 采样率≥50Hz 且单步 \|Δq\|≥3.0rad | 假设 | 仅 fixture |
| A 关节/电机 | `MOTOR_MODE_FLAG_SET` | `(mode & 0x04) != 0` | 假设（位语义 verified） | 仅 fixture |
| B 热 | `MOTOR_TEMP_PROTECT` | `temperature ≥ 90°C` | **verified** | 随 F1/F5 连带命中 |
| B 热 | `MOTOR_TEMP_DEGRADE` | `mean T≥40 且 slope≥0.25 且 mean I<7.9` | 假设 | F2 **98.9%**，3.5s 报出 |
| B 热 | `MOTOR_TEMP_RISE_UNDER_LOAD` | `mean T≥75 且 mean I ≤1.2×基线` | 假设 | 仅 fixture |
| B 热 | `FIELD_SANITY_TEMP_INVALID` | `temperature < 0` | 假设 | 仅 fixture |
| C 电源 | `POWER_SOC_LOW` | `bms_soc ≤ 15%` | 假设（字段 verified） | 仅 fixture |
| E 通信 | `COMM_PACKET_LOSS` | **包级**到达率 < 95%（设备序列号空洞） | 假设 | F3 **97.0%**，3.9s 报出；`go2_sim` 三姿态全中 |
| E 通信 | `COMM_FIELD_INCOMPLETE` | 包都到了，但字段有效率 < 95% | 假设 | 仅 fixture |
| E 通信 | `MOTOR_LOST_COUNTER_RISING` | 窗口内 `Δlost > 0` | 假设 | 仅 fixture |
| F 整机/步态 | `MOTOR_LOAD_SPIKE` | `mean I≥1.4 且 I≥14.5A 占比<50% 且 mean_abs(τ) ≥1.5×baseline_abs(τ)，且排除热退化画像` | 假设 | F5 **99.6%**，3.2s 报出 |

**normal 在 5 个 seed 上全部 0 命中**（此前 seed=0 有 1.7% 误报 LOAD_SPIKE，已随
`baseline_abs` 修正归零）。

"仅 fixture" = 该项目前只用黄金用例验证了判据逻辑，**没有**进在线端到端实测——
因为项目自带的合成器（`synth/generator.py`）只注入 F1~F5 五类故障，没有编码器跳变、
SOC 掉电、lost 计数器这几类的物理注入器。这类规则要么补注入器，要么等真机数据。

## 规则 × 姿态（`go2_sim` 数据源，40s，seed 7）

这是演示真正跑的那条链。**表里"—"不都是规则缺陷，逐条注明性质。**

| 姿态 | F1 堵转 | F2 热退化 | F3 丢包 | F4 电流偏置 | F5 负载骤增 |
|---|---|---|---|---|---|
| stand | ✅ STALL | ❌ 数据边界 | ✅ PACKET_LOSS | ✅ CURRENT_BIAS | ✅ LOAD_SPIKE |
| run | ✅ STALL | ✅ TEMP_DEGRADE | ✅ PACKET_LOSS | ✅ CURRENT_BIAS | ⚠ 报成 TEMP_RISE_UNDER_LOAD |
| prone | ✅ STALL | ❌ 数据边界 | ✅ PACKET_LOSS | ✅ CURRENT_BIAS | ❌ 数据边界 |

**`normal` 在三种姿态上全部 0 命中**——这是这张表最重要的一行。

各项说明：

- **F2 在 stand / prone 完全无信号**：实测温度 60 秒只从 25°C 爬到 26°C（stand），
  prone 一步不动。静止姿态电机几乎不发热，`rth` 变大也没有热量可憋。
  **不是规则漏，是物理上没有那个信号。** 60s 长跑结论相同。
- **F1 会连带报 `MOTOR_LOAD_SPIKE`**（stand 302 帧里 29 帧、prone 26 帧）：堵转时
  `mean_abs(τ)` 确实相对基线大幅上升，F 子系统的判据在物理上成立。两规则并列上报，
  下游按子系统聚合时应以 A（堵转，`critical`）为准。
- **F3 三种姿态全部检出**（曾三姿态全漏，已修，见下节）。
- **F5 在 prone 无信号**：`go2_sim` 的 F5 是往机身施加 −60N 下压，趴卧时关节已在
  最低位、无处可让，`mean|τ|` 从 0.39 只到 0.41。同样是物理边界。
- **F5 在 run 报成 `TEMP_RISE_UNDER_LOAD`**：F5 的 `on_step` 把产热乘 4
  （`extra = 1+3·sev`），奔跑时会烧到高温，于是"高温 + 电流未显著上升"这条先成立
  （40s 下 149 帧报 `TEMP_RISE_UNDER_LOAD`、192 帧报 `TEMP_DEGRADE`，后者末帧仍在）。
  **这是真实的物理耦合**（重载确实会过热），不是误报，但子系统归因偏了 B 而非 F。

## F3 为什么以前测不出来（包级 vs 字段级）

先说结论：**整包丢失对字段级覆盖率结构上不可见。** 这不是阈值调得不够松，
是判据本身看不见这类现象。

`window.coverage(field)` 的算法是 `len(series[field]) / samples`，而 `samples` 是
**网关收了多少条**。整包丢失时：

- 包根本没进网关 → `push()` 没被调用 → `samples` 少加一；
- 这个字段自然也没进 `series` → 分子少加一。

**分子分母一起缩水，比值恒为 1.0。** 实测 `go2_sim` 40s 的 F3 故障窗整包丢了 16%
（332 帧丢 53 帧），字段级覆盖率全程 1.0，`COMM_PACKET_LOSS` 一次都没命中过。

唯一能揭示丢包的信息是**设备序列号**：`RawLogEnvelope.sequence` 由设备单调递增，
网关收到 3、5 就说明 4 丢了。判据改成包级：

```
expected = 1 + Σ max(1, b − a)        # a、b 为相邻两个到达的序列号
coverage = arrived / expected
```

两个坑，都已用测试钉住：

1. **不能写 `Σ max(0, b − a − 1)`**。求和 `b−a` 本身会望远镜式抵消成
   `last − first`，再减 1 就少算了每段的第一包。实测该写法把 60/80 算成 20/20
   （覆盖率 1.0，报不出来），正确写法是 60/79。
2. **序列号回跳不能算丢包**。设备重启会把序列号归零（40 → 1），
   若按 `b − a` 直接减，会读成"一次丢了 39 包"，每次重启都误报。
   `b − a ≤ 0` 一律按 1 算，即"这是紧接着的一条"。

`samples` 与 `packets` 是**两个量，不能混**：前者答"网关收到几条"，
后者答"设备发了几条、到了几条"。旧规则把前者当后者用，就是这次的根因。

修完的完整链路实测（`go2_sim`，40s，seed 7，故障自 5s 注入）：

| 姿态 | 情形 | 末帧判定 | 事件数 | 命中 |
|---|---|---|---|---|
| stand / run / prone | normal | — | 332 | 无 |
| stand / run / prone | F3 丢包 | `COMM_PACKET_LOSS` | 279 | ×236，末帧仍在 ✓ |

同时新增两条规则分工（**不是重复**）：

- `COMM_PACKET_LOSS` —— 包没到；
- `COMM_FIELD_INCOMPLETE` —— 包到了，但某个字段没解出来（载荷残缺或解析失败）。

两条互斥触发，`COMM_FIELD_INCOMPLETE` 额外要求 `packet_coverage() >= 0.95`。

**这个缺陷已进问题台账**——它是本项目第三个"判据结构上盲"的例子
（前两个是 `dq` 未取绝对值、`mean_abs(τ)` 配有符号基线），共同特征是
**在旧数据源上永远测不出来**，只能靠换一个数据源或换一个观测量才暴露。

## 三条修过的口径错误（都用实测数字说话）

三条是**同一类错**：把"有符号量"当"绝对量"用。这类错有个共同特征——
**在合成器上测不出来**（合成器的量恒为正），只在真实步态（正负交替）上暴露。

### 1. 堵转判据里的 `dq` 没取绝对值

`MOTOR_STALL_SUSTAINED` 的第二个条件原写作
`window.count(dq, "<", 0.05) >= 0.5 * window.samples()`。`dq` 是**有符号角速度**，
反向转动（`dq = −2.8 rad/s`）同样满足 `< 0.05`，于是"转得飞快"被算成"没转"。

实测（seed 7，30 帧窗，只看故障窗 240 帧）：

| 姿态 | 情形 | `dq<0.05` 占比 | `\|dq\|<0.05` 占比 | I≥14.5 占比 |
|---|---|---|---|---|
| run | normal | **46.2%** | 0.8% | 0.0% |
| run | F5 负载骤增 | 41.2% | 1.2% | 15.8% |
| run | F1 堵转 | 96.7% | 95.4% | 99.2% |
| stand / prone | normal | 100% | 100% | 0.0% |

奔跑正常只差 **3.8 个百分点**就没到 50% 门槛——换个步态频率或 seed 就会跨过去，
正常奔跑会被报成堵转。取 `|dq|` 后同一组数据是 0.8%，余量 49 个点。

修法：DSL 新增 `window.count_abs(field, op, value)`（与 `mean_abs`/`baseline_abs`
同族），规则改用 `count_abs(dq, "<", 0.05)`。新增回归用例
`fixtures/stall_saturated_but_reversing.json`：电流 100% 饱和、`dq` 恒 −2.8，
**旧写法 100% 误报，新写法 0%**——这条用例就是为了让这个 bug 不会再悄悄回来。

### 2. kt 用错了轴（电机轴 vs 关节侧）

`synth` 的 `DEFAULT_PARAMS["kt"]=0.25` 是**电机轴**扭矩常数；规则的电流残差判据
必须用**关节侧** `KT_JOINT = 0.25 × 6.33 ≈ 1.5825`（见 `go2_sim/rig.py` 的推导）。
用错轴会让电流整体偏大 6.33 倍，**每一条**残差判据都错到十万八千里——
实测 `normal` 也报 CURRENT_BIAS（3000/3000 帧）。

`online_fault_coverage.py` 用**保残差换算**修掉了（不改 `synth` 本体：`R/Rth/Cth`
是配套 0.25 标定的，改 kt 会破坏热模型）：

```
residual = I_old − τ/0.25        # 偏置是安培量，不随 kt 变
I_new    = τ/1.5825 + residual
```

### 3. `mean_abs(τ)` 配了**有符号**基线

规则写的是 `mean_abs(tau_est) >= 1.5 * window.baseline(tau_est)`：左边是 |τ| 的均值，
右边是**有符号** τ 的 EMA。步态摆动时 τ 正负对称振荡，有符号均值被正负抵消，
于是比值里混进一个**与负载无关的恒定倍数**：

| 姿态 | 有符号基线 | `mean|τ|` | 旧比值 | 新比值（`baseline_abs`） |
|---|---|---|---|---|
| 奔跑 normal | 4.81 | 9.14 | **1.90（恒误报）** | 1.01 |
| 趴卧 normal | **−0.66** | 0.39 | −0.59（恒不成立） | 0.59 |
| 站立 F5 | 1.47 | 2.72 | 1.85 | 1.85（真实负载） |

奔跑的比值从 t=0.5s 起就是 1.90 且**不随时间变化**——它不是负载，是口径差。
修法：`Enricher` 同时维护 `|x|` 的 EMA（键 `field|abs`），DSL 加 `window.baseline_abs()`，
规则改用 `baseline_abs`。**不能只在规则侧取 `abs(baseline)`**：趴卧基线是负数，
取绝对值只会把"恒不成立"翻成"恒成立"，两种都是错的。

### 附带修掉的一个静默失败

`Enricher.snapshot()` 原先只按 `metrics` 里出现的字段名查基线，不会带出 `|abs` 键，
于是 `baseline_abs()` 恒为 None → 规则在求值期失败 → **静默不命中**（不报错、不出结果）。
表现是 F5 的触发率从 99.8% 掉到 42.1%、末帧什么都报不出来。
`snapshot()` 现在同时带出 `<field>|abs`。

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

**F5 负载骤增 vs B 热剖面**：`go2_sim` 的 F5 注入同时把产热乘 4，所以奔跑姿态下
60 秒就烧到 77°C，"高温 + 电流未显著上升"这条（`MOTOR_TEMP_RISE_UNDER_LOAD`）
会先成立，报出的子系统是 B 而不是 F。**这是真实的物理耦合**（重载确实会过热），
不是误报，但归因会偏。反过来 `MOTOR_LOAD_SPIKE` 里显式加了一条排除：

```
not (mean(temperature) >= 40 and slope(temperature) >= 0.25 and mean(current) < 7.9)
```

即"温度已经在明显爬升、而电流没到负载量级"时让给 B 子系统，避免两条规则对同一现象
抢报。这条排除是**经验性的**，依据是 F2 的实测画像（`docs` 里的五类故障参数表），
不是厂商给的分界，标为 `assumption`。

**验证过的一点**：`synth` 的 5 个 seed 上 `normal` 全程零命中（改前 seed=0 有 1.7% 误报）；
`go2_sim` 的 stand/run/prone 三种姿态上 `normal` 也全部零命中。五类故障各自只命中
本子系统规则（F1/F5 额外连带温度规则，见上）。

## 降级语义（缺数据时不误报）

三条，都在代码里显式写了，不是隐含行为：

1. **样本不足**：窗口内 <8 条样本，一切窗口判据不命中（`_win()` 统一加前置条件）。
2. **基线缺失**：`window.baseline()` / `baseline_abs()` 返回 None，与之比较的规则
   在求值期抛 `DSLError` → `evaluate()` 视为不命中。**绝不退化成"基线=0"**
   （那会把每条正常读数判成异常），也**绝不回退到另一个口径的基线**
   （`baseline_abs()` 不会去读有符号基线——那正是要被修掉的错配）。
3. **窗口缺失**：`series` 为空时所有 DSL 规则直接跳过，不拿单条 metrics 冒充窗口。
4. **包级统计不足**：`window.packet_coverage()` / `packets_lost()` 在
   "窗口内不足两条"或"任一条缺序列号"时返回 None → 判据不命中。
   **"不知道"绝不能报成 1.0**——那会把"没给序列号"伪装成"没丢包"。
   这正是旧 fixture 一度白过（而不是真过）的原因：它们没写 `sequences`，
   规则静默跳过，看起来像"反例通过"。现在几条相关 fixture 都补了真实序列号。

实测：拿 `load_spike_positive` 的正例，有基线时命中、无基线时**不命中**且不抛异常。

**⚠ 这条设计的反面**：降级"静默不命中"有代价——如果**基线键名本身写错**或没被带出来，
规则不会报错，只是永远不出结果，看起来和"正常"一模一样。`snapshot()` 漏带 `|abs`
键就是这么藏住的（F5 触发率从 99.8% 掉到 42.1% 才被发现，根因查了一轮）。
所以新增口径时必须同时验证**正例命中**与**反例不命中**，只看"没报错"不算通过。

## 复现

```bash
python -m data_dog.operator_factory.p1_demo          # 25 条黄金用例
python scripts/online_fault_coverage.py              # 合成器：5 类故障 × 5 seed
python scripts/online_fault_coverage.py --json       # 机器可读
python scripts/go2_sim_coverage.py                   # MuJoCo：3 姿态 × 6 情形
python scripts/go2_sim_coverage.py --seconds 60 --seed 7 --postures run,prone
```

`go2_sim_coverage.py` 单独跑比较慢（MuJoCo 要真解算，3 姿态 × 6 情形约 2 分钟），
建议后台跑。它默认 40s、seed 7；上表的数字就是这组参数。

