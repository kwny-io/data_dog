# 语料来源说明（Unitree 公开资料）

本目录的语料来自 Unitree Robotics **公开发布**的资料，用于 `data_dog` 离线算子工厂（P1）的
"文档 → 知识 → 算子"链路验证。**每个文件的来源与许可如下**，使用时请遵守对应许可。

| 文件 | 来源 | 许可 |
|---|---|---|
| `unitree_go2_user_manual.md`（原始 `raw/go2_user_manual.pdf`） | Unitree 官方 Go2 用户手册（公开） | Unitree 官网公开文档 |
| `raw/unitree_sdk2__*.idl.txt` | [unitreerobotics/unitree_sdk2](https://github.com/unitreerobotics/unitree_sdk2) 的 IDL 定义 | BSD-3-Clause |
| `raw/unitree_actuator_sdk/motor_msg_GO-M8010-6.h` | [unitreerobotics/unitree_actuator_sdk](https://github.com/unitreerobotics/unitree_actuator_sdk) 电机协议头 | BSD-3-Clause |
| `raw/unitree_actuator_sdk/LICENSE` | 上述仓库 LICENSE（署名要求） | BSD-3-Clause |
| `go2_webrtc*.md` | [tfoldi/go2-webrtc](https://github.com/tfoldi/go2-webrtc) 社区解码文档 | 见原仓库 |
| `westonrobot_go2_diag.md`（原始 `raw/*.html`） | Weston Robot Go2 诊断指南（公开） | 见原站点 |

## 关键：电机温度保护阈值（权威出处）

`raw/unitree_actuator_sdk/motor_msg_GO-M8010-6.h` 第 45–46 行（**厂商官方开源，BSD-3-Clause**）：

```c
int8_t   temp;          // 电机温度: -128~127°C 90°C时触发温度保护
uint8_t  MError :3;     // 电机错误标识: 0.正常 1.过热 2.过流 3.过压 4.编码器故障 5-7.保留
```

**意义**：
- `90°C`（温度保护/停机阈）→ 由 `unconfirmed` **升级为 `verified`**（有厂商官方出处）。
- `75°C`（warning 阈）→ **此开源协议中未出现**，仍为 `unconfirmed` / 项目假设。
- `MError` 枚举（正常/过热/过流/过压/编码器故障）→ 可抽为结构化错误码知识。

> 这正是"诚实抽取"机制的价值：**能证实的才升级为厂商事实，证不了的保持待确认**，不因为"反正差不多"就把 75°C 也说成官方数据。

---

## 2026-10-29 补充：SDK 本体可获取，DDS 已在本机验证

上面表格里只有 IDL 文本，**没有 SDK 代码**。补一次实测记录，供后续接真机时直接照做。

| 项 | 值 |
|---|---|
| 仓库 | [unitreerobotics/unitree_sdk2_python](https://github.com/unitreerobotics/unitree_sdk2_python) |
| 许可 | **BSD-3-Clause**（可商用；分发需保留版权声明） |
| 版本 | HEAD `814556d15970dd2ecf1c9984e845ca02ab07e206`（2026-09-21） |
| 体积 | 1.5 MB（`--depth 1` 足够，IDL 与示例都在） |
| 安装 | `pip install --no-deps .`；**不要照 `setup.py` 的 `cyclonedds==0.10.2` 装** |

### 实测结论（Windows 11，本机）

- `import unitree_sdk2py` ✅
- `LowState_` 构造 ✅（22 个字段，`motor_state` 定长 20 槽）
- `ChannelFactoryInitialize(0)` ✅
- **`rt/lowstate` DDS 环回 ✅**：自建 pub/sub，22 字段完整往返
- `bms_state.soc` / `imu_state.accelerometer` / `motor_state[0].q` / `sn` / `tick` 全部对上

### 安装陷阱（会卡住人的那一条）

`setup.py` 写的是：

```python
install_requires=["cyclonedds==0.10.2", "numpy", "opencv-python"]
```

**0.10.2 在 Windows 上没有预编译 wheel**，pip 回退源码编译，报
`Could not locate cyclonedds. Try to set CYCLONEDDS_HOME or CMAKE_PREFIX_PATH`
（README 里那段解法是 Linux 编译 C 库的路子）。

**实际解**：装较新版本的 `cyclonedds` 预编译 wheel（实测 **11.0.1**），SDK 照常工作。
注意这是**跨大版本实测通过**，不是官方声明——日后遇到序列化/API 差异先怀疑这里。

**DDS 还要一个系统级运行库**：CycloneDDS 的 Python 绑定依赖 C 库
（Windows 上是 `ddsc.dll`，随 wheel 一起带；Linux 上要另外装 `cyclonedds` 的
`.deb` 或自行编译）。本机 Windows 环境实测无需额外操作，**Linux 部署时要单独确认**。

**依赖放置：可选 extra，不进主依赖也不进 dev**

`cyclonedds` 在 `pyproject.toml` 里是 **`data-dog[dds]` 可选依赖**，
不在主依赖、也不在 `dev`：

- 主依赖：默认路径（进程内数据面 / MQTT）完全用不到它，塞进去会让每次
  `pip install` 和 CI 都白付几十 MB 与一次 C 扩展安装。
- `dev`：CI 装的是 `.[dev,postgres]`；如果它在 dev 里，每次 push 的 test job
  都要多装一个 C 扩展，而**映射单测刻意不 import 它**（见下条）。

**`opencv-python` 仍然不引入**：SDK 的 `setup.py` 要它，但那是给示例代码
（摄像头/图像）用的，遥测桥接用不到。装 SDK 时用 `pip install --no-deps`
再单独装 `cyclonedds` 即可。

### 这套依赖怎么落到代码上

`src/data_dog/go2_sim/dds_bridge.py` **刻意分两段**：

| 段 | 内容 | 是否 import cyclonedds |
|---|---|---|
| 纯映射 `build_low_state_kwargs` | `Telemetry` → `LowState_` 的 22 字段字典 | **否** |
| DDS 出口 `DdsTelemetryPublisher` | 惰性 import SDK，发 `rt/lowstate` | 是（在 `__init__` 里） |

**为什么必须分**：`tests/test_dds_bridge.py` 的 12 条用例覆盖的正是最容易错的
部分（槽位顺序、定长槽数、量纲、NaN），这些**必须能在没装 cyclonedds 的 CI 里跑**。
绑在一起把映射和 SDK 放同一 import 路径，最该测的东西反而测不到。

| 自检 | 是否需要 DDS | 命令 |
|---|---|---|
| 纯映射单测 | 否 | `pytest tests/test_dds_bridge.py` |
| 整帧环回 | **是** | `python scripts/dds_loopback.py` |
| 服务器端到端 | **是** | `python -m data_dog.go2_sim.server --dds` |

### 对桥接设计的直接影响

SDK 全仓只有 4 个 DDS 通道名：

```
rt/lowstate    rt/lowcmd    rt/arm_sdk    rt/secondary_imu
```

**IMU / BMS / 电机全部装在 `LowState_` 一个结构里**，不是各走各通道。
仿真器那套 `joint_motor` / `imu.body` / `battery.main` / `thermal.main` 四路切分
**是给检测器做的语义切分**；接真机时必须由桥接层按 `LowState_` 重建，
否则一个丢包会被误报成四个部件各丢一次。**这一步已在
`build_low_state_kwargs` 里做了**（四路合成一帧）。

另外：高层运动接口（`sport_client`）走 **RPC，不是 DDS 遥测**，
只发指令不回状态；状态仍从 `rt/lowstate` 出。

### 关节槽位顺序：真机与仿真器是反的

写桥接时**最容易踩且最难发现**的一条。厂商
`example/go2/low_level/unitree_legged_const.py`：

```python
LegID = {"FR_0": 0,  "FR_1": 1,  "FR_2": 2,     # 前右
         "FL_0": 3,  "FL_1": 4,  "FL_2": 5,     # 前左
         "RR_0": 6,  "RR_1": 7,  "RR_2": 8,     # 后右
         "RL_0": 9,  "RL_1": 10, "RL_2": 11}    # 后左
```

真机顺序 **FR → FL → RR → RL**；本项目 `ACTUATORS` 是 **FL → FR → RL → RR**
（前两腿对调、后两腿也对调）。按 `range(12)` 顺次填会让四条腿左右互换，
而**数据看起来完全正常**（12 槽都有数、量纲也对）。唯一权威是
`dds_bridge._SLOT_OF_JOINT`，由 `test_slot_order_matches_vendor_legid` 钉住
（那条用例逐槽写死数字，**不从表里反查**——反查的话表整体改错测试也跟着错）。

### 收端注意：`uint8` 定长数组过一趟 DDS 会变成 `bytes`

实测（2026-10-30，CycloneDDS 11.0.1 + SDK）：

```python
m = 收到的 LowState_
type(m.head)            # bytes，不是 list
list(m.head)            # [0, 0]  ← 这样才是 int 序列
type(m.bms_state.bq_ntc)     # bytes
type(m.wireless_remote)      # bytes
```

`uint16`/`uint32`/`float` 的定长数组仍是 list/array，**只有 `uint8` 数组**
按字节串返回。这是序列化表示，不是数据错了；但如果按 `isinstance(x, list)`
去校验或按下标直接比，就会在 `head` / `sn` / `bq_ntc` / `mcu_ntc` /
`wireless_remote` 上误报"类型不一致"。

`scripts/dds_loopback.py` 的 `_to_plain` 统一把 `bytes/bytearray` 摊回
`list[int]` 再比——这个坑是**换成全字段递归比对后才暴露的**，之前只抽查
`q`/`soc`/`power_v` 的写法完全看不到。
