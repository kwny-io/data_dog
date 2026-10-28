"""长跑的 Go2 实时仿真：物理步进 + 故障注入 + 遥测采样 + 离屏渲染。

与 `mujoco_sim.simulate` 的区别
-------------------------------
`simulate()` 是**跑完一段就返回**的批处理：给定 duration，一次性算完再交结果。
本模块是**持续步进**的：拿一个 `step(n)`，走 n 个仿真步，返回这段时间产生的
遥测。调用方（HTTP 服务）按自己的节奏反复调它，于是"仿真时钟"可以随时暂停、
变速、重来——这正是演示要的。

线程安全
--------
MuJoCo 的 MjModel/MjData **不是线程安全的**（`mujoco_sim` 里也明确锁着）。
本类所有公开方法都过一把可重入锁；HTTP 层多线程调用安全。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from data_dog.mujoco_sim.simulate import ACTUATORS, DEFAULT_MODEL, MOTOR_PARAMS

from .faults import FaultSpec, fault_by_key
from .postures import POSTURES, Posture
from .subsystems import (
    BatteryModel,
    BatteryState,
    ImuModel,
    ImuState,
    SystemState,
    ThermalState,
    fan_frequency,
)

# F12 单体不均衡的注入幅度（V）：整包 6 节，最差一节被拉低这么多
BATTERY_CELL_IMBALANCE = 0.12

# 电控常驻电流（A）：主控板 + 感知 + 风扇的固定开销，与步态无关。
# 真机 Go2 待机电流在个位数安（项目假设，厂商手册未给该值）。
ELEC_IDLE_CURRENT = 3.0

# 电机温度读数上限（°C）——**传感器量程，不是模型随便定的**。
#
# 依据：厂商头文件 `motor_msg_GO-M8010-6.h` 第 45 行
#   `int8_t temp;   // 电机温度: -128~127°C 90°C时触发温度保护`
# 驱动侧温度字段是 int8_t，能表示的**最大就是 127°C**。电机真烧到 600°C
# 也读不出 600——超过量程的读数在驱动里根本不成立。
#
# 为什么需要这个上限：一阶热模型是线性的，稳态 ΔT = I²·R·Rth，没有物理上限。
# F2 把 Rth 放大 20 倍后，奔跑电流 12.2A 的稳态是 598°C、额定 15A 是 891°C。
# 面板上因此出现过 296°C 这种数——演示视频里这不像"故障"，像程序算错了。
# 加了饱和之后，F2 不再是一个"越来越离谱的数字"，而是**钳在量程顶端**，
# 这正好对应真机"温度保护已触发、读数顶死"的现场表现。
MOTOR_TEMP_CEILING = 127.0

# 关节侧等效扭矩常数 = MOTOR_PARAMS["kt"] × 减速比。
#
# ⚠ 为什么不直接用 MOTOR_PARAMS["kt"]=0.25 —— 那是个真实存在的建模错误：
#   1. 厂商头文件 `motor_msg_GO-M8010-6.h` 写明 fbk.torque 是
#      "实际关节输出扭矩 N.m"，是**关节轴**上的量；
#   2. 0.25 N·m/A 是公开资料给的**电机轴**扭矩常数；
#   3. GO-M8010-6 是行星减速执行器，两者之间差一个减速比 n。
#   直接用 0.25 会把关节扭矩当电机扭矩，电流被放大 n 倍。
#
# n=6.33 的确定过程（不是试出来的，是被三条硬约束夹出来的）：
#   - 额定扭矩 23.7 N·m（= go2.xml 的 ctrlrange，厂商值）连续运行，
#     稳态必须低于厂商写明的 90°C 保护点：
#       I²·R·Rth < 65K  →  I < 18.4A  →  kt_eff > 1.29  →  n > 5.2
#   - 实测站立峰值 30.5A（n=1 时）→ 稳态 203°C，真实 Go2 站着不动绝不至此；
#   - n=6.33 时：站立 4.8A / 29.5°C，奔跑 12.2A / 53.8°C，额定 15.0A / 68.2°C。
#   只有这一档同时满足"站立不烫、奔跑明显更热、额定留在保护线内"。
#
# ⚠ 与 `data/mj_60s_100k/` 不一致（必须知道，跨源比较时不要当笔误）：
#   那十万条数据的 i_peak 中位数是 95.8A、**与故障类别无关**（normal 样本
#   也是 95.8A），正好等于 23.7 / 0.25 = 94.8A —— 旧管线同样把关节扭矩
#   直接除以电机侧 kt。旧数据的 normal 样本 t_peak 因此最高到 162°C。
#   比较新旧数据时**只比趋势和无量纲量，不要比电流/温度的绝对值**。
GEAR_RATIO = 6.33
KT_JOINT = MOTOR_PARAMS["kt"] * GEAR_RATIO       # ≈1.583 N·m/A（关节侧）


def _fit(img, width: int, height: int):
    """把渲染出的整幅画面按**长边等比**缩放再居中裁切到目标尺寸。

    不直接 `resize((w,h))`：那样会把 16:10 的渲染拉伸成任意比例，狗会
    被压扁/拉长。等比放大后裁掉多余部分，几何始终正确。
    """
    from PIL import Image

    out = Image.fromarray(img)
    if out.size == (width, height):
        return img
    scale = max(width / out.width, height / out.height)
    mid = out.resize((max(1, round(out.width * scale)),
                      max(1, round(out.height * scale))), Image.LANCZOS)
    left = (mid.width - width) // 2
    top = (mid.height - height) // 2
    return np.asarray(mid.crop((left, top, left + width, top + height)))


def _quat_to_mat(q: tuple[float, float, float, float]) -> np.ndarray:
    """四元数 (w,x,y,z) → 3×3 旋转矩阵（机身 → 世界）。"""
    w, x, y, z = q
    n = w * w + x * x + y * y + z * z
    if n < 1e-12:
        return np.eye(3)
    s = 2.0 / n
    return np.array([
        [1 - s * (y * y + z * z), s * (x * y - w * z), s * (x * z + w * y)],
        [s * (x * y + w * z), 1 - s * (x * x + z * z), s * (y * z - w * x)],
        [s * (x * z - w * y), s * (y * z + w * x), 1 - s * (x * x + y * y)],
    ])

# go2.xml 的仿真步长是 0.002s；遥测按 100Hz（0.01s）采样，
# 于是每采一个点要跑 5 个物理步。这个比例从模型里读，不写死。
TELEMETRY_HZ = 100.0

# 部件名映射。算子绑定的 `component` 只是**声明值**：`CompatibilityResolver`
# 只按 vendor/model/firmware/protocol 匹配，`component_ref` 直接取自信封的
# `component_hint`。所以这 4 个名字都能出候选，真正决定"能不能报出来"的
# 是**物理**——见 `demo_joint()`。
_JOINT_TO_COMPONENT = {
    "FL_hip": "joint_motor.front_left_hip",
    "FR_hip": "joint_motor.front_right_hip",
    "FL_thigh": "joint_motor.front_left_knee",
    "FR_thigh": "joint_motor.front_right_knee",
}
_COMPONENT_TO_JOINT = {v: k for k, v in _JOINT_TO_COMPONENT.items()}


def demo_joint() -> str:
    """演示主关节 = `emit.DEMO_COMPONENT` 对应的那一个。

    **所有关节类故障都必须定向到这里**，否则故障打在没人看的数据上，
    测出来是"检不出"，实际是"根本没送到检测器眼前"。

    为什么是 thigh 不是 hip（实测，60 秒奔跑 + 散热失效）：

        RR_thigh 129.3°C   RL_thigh 129.0°C
        FR_thigh  96.6°C   FL_thigh  95.5°C
        RL_hip    36.8°C   RR_hip    36.0°C
        FL_hip    27.3°C   FR_hip    26.2°C

    hip 是偏摆轴，几乎不承重也不发热；检测器的阈值是 `temperature >= 90`，
    发 hip 就永远不可能触发。早期这里把 4 条关节故障（F1/F6/F7/F8）都定向到
    FL_hip，覆盖矩阵因此把它们全判成 MISS——查下来是仿真器打错了位置。
    """
    from .emit import DEMO_COMPONENT
    return _COMPONENT_TO_JOINT[DEMO_COMPONENT]


@dataclass
class SimConfig:
    """一次仿真会话的配置。"""

    posture: str = "stand"
    fault: str = "normal"
    severity: float = 1.0
    seed: int | None = None
    # 故障何时开始（仿真秒）。给一段"健康基线"，否则一开局就坏，
    # 参照窗拿不到干净数据——这与数据集的 FAULT_START_FRAC 是同一个考虑。
    fault_start_s: float = 8.0
    # 故障持续多久；None = 一直持续到会话结束
    fault_duration_s: float | None = None
    # 热时间缩放：把 Cth 除以这个倍数。
    # **1.0 = 物理原值**（与 data_dog.synth / mujoco_sim 同一套参数）。
    # 调大只用于录像：一阶热模型的时间常数 Cth·Rth ≈ 495s，散热失效这类
    # 慢故障在真实时间下要几十分钟才爬到位——这是物理事实，不是 bug。
    # UI 上开大于 1 必须显示"非物理加速"标记。
    thermal_scale: float = 1.0

    def resolved_seed(self) -> int:
        return int(self.seed) if self.seed is not None else int(time.time_ns() & 0x7FFFFFFF)


@dataclass
class Telemetry:
    """一次采样：12 关节的观测量 + 基座位姿 + 故障真值。

    **字段对齐厂商 IDL MotorState**（q/dq/ddq/tau_est/q_raw/dq_raw/temperature）。
    区分 raw 与滤波值是必要的：编码器故障（F6）只在 raw 与 q 的关系上暴露，
    力矩估算故障（F7）只在 tau_est 与 |I|·kt 的关系上暴露——只给一套数就没法判。

    温度与电流是**观测值**（含噪声/偏置/丢包），不是仿真真值——
    和真机一致：下游只能看到传感器读到的东西。
    """

    t: float                       # 仿真时钟（秒）
    joints: list[str] = field(default_factory=lambda: list(ACTUATORS))
    temperature: np.ndarray = field(default_factory=lambda: np.zeros(12))
    current: np.ndarray = field(default_factory=lambda: np.zeros(12))
    torque: np.ndarray = field(default_factory=lambda: np.zeros(12))       # tau_est
    q: np.ndarray = field(default_factory=lambda: np.zeros(12))
    dq: np.ndarray = field(default_factory=lambda: np.zeros(12))
    q_raw: np.ndarray = field(default_factory=lambda: np.zeros(12))
    dq_raw: np.ndarray = field(default_factory=lambda: np.zeros(12))
    valid: np.ndarray = field(default_factory=lambda: np.ones(12, dtype=bool))
    base_pos: tuple[float, float, float] = (0.0, 0.0, 0.0)
    base_quat: tuple[float, float, float, float] = (1.0, 0.0, 0.0, 0.0)
    base_vel: float = 0.0
    # 关节以外的子系统（None 表示本次采样没有该通道）
    battery: BatteryState | None = None
    imu: ImuState | None = None
    thermal: ThermalState | None = None
    system: SystemState | None = None

    def component_row(self, component: str) -> dict[str, float] | None:
        """取某个 Agent 部件名对应的观测量（字段名对齐 MotorState）。"""
        joint = _COMPONENT_TO_JOINT.get(component)
        if joint is None or joint not in self.joints:
            return None
        i = self.joints.index(joint)
        return {
            "temperature": float(self.temperature[i]),
            "current": float(self.current[i]),
            "tau_est": float(self.torque[i]),
            "q": float(self.q[i]),
            "q_raw": float(self.q_raw[i]),
            "dq": float(self.dq[i]),
            "dq_raw": float(self.dq_raw[i]),
            "lost": 0.0 if self.valid[i] else 1.0,
        }

    def flat_metrics(self) -> dict[str, float]:
        """把所有子系统的指标压成扁平字典（给信封 payload / UI 用）。

        只收标量：数组型字段（cell_vol / foot_force）由子系统自己在
        `as_metrics()` 里展开成 min/max/spread 或逐腿标量。
        """
        out: dict[str, float] = {}
        for s in (self.battery, self.imu, self.thermal, self.system):
            if s is not None:
                out.update(s.as_metrics())
        out["temp_max"] = round(float(np.nanmax(self.temperature)), 3)
        out["current_max"] = round(float(np.nanmax(self.current)), 3)
        out["valid_rate"] = round(float(np.mean(self.valid)), 4)
        return out


class Go2Sim:
    """一只活在 MuJoCo 里的 Go2。"""

    def __init__(self, config: SimConfig, model_path: Path | None = None) -> None:
        import mujoco

        self._mujoco = mujoco
        self._lock = threading.RLock()
        self.config = config
        self.seed = config.resolved_seed()
        self._rng = np.random.default_rng(self.seed)
        self.posture: Posture = POSTURES.get(config.posture) or POSTURES["stand"]
        self.fault: FaultSpec = fault_by_key(config.fault)

        self._model = mujoco.MjModel.from_xml_path(
            str(model_path or DEFAULT_MODEL))
        # 抬高离屏 framebuffer：go2 场景默认只有 640×480，控制台要 880×550
        # 会直接抛 ValueError（见 render_jpeg 的注解）。这是模型属性，
        # 必须在建 MjData / MjRenderer **之前**改。
        self._model.vis.global_.offwidth = max(
            int(self._model.vis.global_.offwidth), self.OFFSCREEN_W)
        self._model.vis.global_.offheight = max(
            int(self._model.vis.global_.offheight), self.OFFSCREEN_H)

        self._data = mujoco.MjData(self._model)
        self._substeps = max(1, round(1.0 / (TELEMETRY_HZ * self._model.opt.timestep)))

        # 关节 → qpos/qvel 下标
        self._qidx = list(range(7, 19))               # 前 7 个是基座 freejoint
        joint_names = [a + "_joint" for a in ACTUATORS]
        self._dof = [self._model.jnt_dofadr[self._model.joint(n).id]
                     for n in joint_names]

        # 地面 geom（name="floor"），F22 足端打滑要改它的摩擦系数
        self._floor_geom_id = mujoco.mj_name2id(
            self._model, mujoco.mjtObj.mjOBJ_GEOM, "floor")
        if self._floor_geom_id < 0:
            self._floor_geom_id = None
        self._floor_friction0 = (
            float(self._model.geom_friction[self._floor_geom_id][0])
            if self._floor_geom_id is not None else 1.0)

        # 足端 body id → 腿序号（0=FL 1=FR 2=RL 3=RR）。
        # 脚掌 geom 挂在 calf 上，故按 calf 的 body 名建映射，不写死 body id。
        self._foot_body_to_leg: dict[int, int] = {}
        self._leg_to_foot_body: dict[int, int] = {}
        self._foot_geom: dict[str, int] = {}
        for idx, leg in enumerate(("FL", "FR", "RL", "RR")):
            bid = mujoco.mj_name2id(self._model, mujoco.mjtObj.mjOBJ_BODY,
                                    f"{leg}_calf")
            if bid >= 0:
                self._foot_body_to_leg[int(bid)] = idx
                self._leg_to_foot_body[idx] = int(bid)
            # 足端 geom 名就是腿名（FL / FR / RL / RR），F22 要改它的摩擦
            gid = mujoco.mj_name2id(self._model, mujoco.mjtObj.mjOBJ_GEOM, leg)
            if gid >= 0:
                self._foot_geom[leg] = int(gid)

        # PD 增益：沿用 gait.py 的取值（站立档），跑步档在 Posture 里同源
        self._kp = np.array([40.0, 40.0, 60.0] * 4)
        self._kd = np.array([2.0, 2.0, 3.0] * 4)

        # 热模型（一阶，与 mujoco_sim.simulate 同一套参数）
        self._T = np.full(12, MOTOR_PARAMS["T_amb"])
        # 关节侧 kt（含减速比）。厂商 IDL 的 MotorState **没有 current 字段**，
        # 电流是我们从 tau_est 推的派生量；用电机的轴侧 kt 会放大 6.33 倍。
        self._kt = KT_JOINT
        self._R = MOTOR_PARAMS["R"]

        # 关节以外的子系统：电池 / IMU / 热管理 / 通信 / 整机。
        # 这些通道在厂商 IDL 里都是真实存在的，缺了它们就只能演 5 类电机故障。
        self._battery = BatteryModel(seed=self.seed)
        self._imu = ImuModel(seed=self.seed + 1)
        self._thermal = ThermalState()
        self._system = SystemState()
        # 通信/整机类故障的运行计数
        self._lost_count = 0
        self._crc_fails = 0
        self._last: Telemetry | None = None

        # 时钟
        self._t = 0.0
        self._steps = 0
        self._fell = False
        self._reset_data()

        # 渲染器惰性创建（不进仿真热路径，只有要画面时才建）
        self._renderer = None
        self._camera = None
        self._gl_thread: int | None = None    # 建 GL 上下文的线程（见 render_frame）
        self._close_pending = False           # 跨线程调 close，等仿真线程收尾

    # ---------------------------------------------------------------- 状态
    def _reset_data(self) -> None:
        d = self._data
        m = self._mujoco
        m.mj_resetData(self._model, d)
        d.qpos[2] = self.posture.nominal_z
        d.qpos[3] = 1.0                                # 四元数 w，无旋转
        home = self.posture.targets(0.0)
        for i in range(12):
            d.qpos[self._qidx[i]] = home[i]
        # 预滚动：让接触稳定下来，避免开局那几帧的穿透抖动被当成"异常"。
        # 必须带着 PD 环滚（而不是空放物理步），否则狗在重力下直接蹲到地上，
        # 开局那一段会被 `fallen_min_z` 判成翻倒——实测站姿会掉到 z≈0.10。
        for _ in range(2000):
            targets = self.posture.targets(0.0)
            for a in range(12):
                tau = (self._kp[a] * (targets[a] - d.qpos[self._qidx[a]])
                       - self._kd[a] * d.qvel[self._dof[a]])
                d.ctrl[a] = float(np.clip(tau,
                                          self._model.actuator_ctrlrange[a][0],
                                          self._model.actuator_ctrlrange[a][1]))
            m.mj_step(self._model, d)
        self._T[:] = MOTOR_PARAMS["T_amb"]
        self._t = 0.0
        self._steps = 0
        self._fell = False

    def reset(self, config: SimConfig | None = None) -> None:
        """重开一局：换配置则重建，不换则原地复位到直立。"""
        with self._lock:
            if config is not None:
                self.config = config
                self.seed = config.resolved_seed()
                self._rng = np.random.default_rng(self.seed)
                self.posture = POSTURES.get(config.posture) or POSTURES["stand"]
                self.fault = fault_by_key(config.fault)
            self._reset_data()

    def inject(self, key: str, *, severity: float | None = None,
               duration_s: float | None = None, start_s: float = 0.0) -> None:
        """**运行中**换故障：不停机、不复位、不丢热状态。

        与 `reset(config=...)` 的区别：reset 会把物理与热状态推回初值，
        等于"换一台机器"；本方法保留当前位姿、关节温度、电池等一切连续量，
        只换"从这一刻起要注入什么"——这才是"运行中被打断"的语义，
        也是演示里最关键的观感（温度曲线不该在注入瞬间跳回 25°C）。

        `start_s` 是**相对现在**的延迟（默认 0 = 立刻生效）。这里必须把
        `fault_start_s` 重算成 `self._t + start_s`：原配置里它是绝对仿真
        时钟，若 `_t` 早已越过旧值，直接沿用会让新故障"一注入就已生效"，
        看不到从正常到异常的过渡。
        """
        with self._lock:
            self.fault = fault_by_key(key)
            cfg = self.config
            self.config = SimConfig(
                posture=cfg.posture,
                fault=key,
                severity=cfg.severity if severity is None else float(severity),
                seed=cfg.seed,
                fault_start_s=self._t + max(0.0, float(start_s)),
                fault_duration_s=duration_s,
                thermal_scale=cfg.thermal_scale,
            )

    def _fault_active(self) -> bool:
        if self.fault.key == "normal":
            return False
        c = self.config
        if self._t < c.fault_start_s:
            return False
        if c.fault_duration_s is not None:
            return self._t < c.fault_start_s + c.fault_duration_s
        return True

    # ------------------------------------------------------------ 故障注入
    def _severity(self) -> float:
        return max(0.0, min(1.5, self.config.severity))

    def _apply_physics_fault(self, active: bool) -> None:
        """物理层：直接改仿真受到的力 / 环境。"""
        d = self._data
        if not active:
            return
        sev = self._severity()
        k = self.fault.key
        if k == "F1_stall":
            # 堵转：对被卡住的关节施加反向大扭矩。用 qfrc_applied 而不是摩擦，
            # 因为 MuJoCo 的摩擦是软约束，持续大力下会缓慢蠕变（见桌面文档坑 1）。
            j = ACTUATORS.index(demo_joint())
            d.qfrc_applied[self._dof[j]] = 30.0 * sev
        elif k == "F5_load_spike":
            # 负载骤增：往机身施加向下的外力（frc_applied 作用在基座 body 上）
            d.xfrc_applied[1, 2] = -60.0 * sev          # body 1 = base，z 方向
        elif k == "F18_fallen":
            # 翻倒：对机身施加横向冲量式外力，把狗推倒（真物理，不是改读数）
            d.xfrc_applied[1, 0] = 55.0 * sev
        elif k == "F22_foot_slip":
            # 足端打滑：**只降低单只脚**（FL）的摩擦，而不是整个地面。
            # 改整片地面会四条腿一起滑，机身只是整体打滑、轨迹仍然笔直，
            # 读数上几乎看不出来；单腿失抓会产生**偏航**——这才是现场
            # "脚垫磨秃一只"该有的样子，也才能在 yaw_speed / 轨迹上判出来。
            self._set_foot_friction("FL", max(0.03, 0.8 - 0.75 * sev))

    def _set_floor_friction(self, value: float) -> None:
        """改地面摩擦系数。只在值变化时写，避免每步都碰模型。"""
        gid = self._floor_geom_id
        if gid is None:
            return
        f = self._model.geom_friction[gid]
        if abs(float(f[0]) - value) > 1e-6:
            f[0] = value

    def _set_foot_friction(self, leg: str, value: float) -> None:
        """改单只足端 geom 的摩擦系数（F22 用）。"""
        gid = self._foot_geom.get(leg)
        if gid is None:
            return
        f = self._model.geom_friction[gid]
        if abs(float(f[0]) - value) > 1e-6:
            f[0] = value

    def _thermal_params(self, active: bool) -> tuple[float, float, float]:
        """热层：返回本步的 (散热热阻, 额外产热系数, 环境温度)。

        F2 只动 Rth、**不动产热**：电流保持正常而温度爬升，这正是散热失效
        与"真加载"（F5）的判别特征。

        ⚠ 幅度按修正后的电流重新标定过（见 KT_JOINT 的说明）。
        稳态 ΔT = I²·R·Rth·k，修正后站立 I≈5.0A、奔跑 I≈10~12A：
          - 原实现 F2 用 k=9，是按旧电流（30A）标定的；换成 5A 后
            k=9 只能到 68°C，跨不过 75°C 告警线——故障"注入了但检测不出"。
          - k=20 → 站立稳态 121°C，先过 75°C 再过 90°C，与"风扇堵死还
            长时间通电"的现场一致（这是真实存在的烧毁工况）。
        """
        rth = MOTOR_PARAMS["Rth"]
        extra = 1.0
        t_amb = MOTOR_PARAMS["T_amb"]
        if active:
            sev = self._severity()
            k = self.fault.key
            if k == "F2_temp_rise":
                rth *= (1.0 + 19.0 * sev)    # sev=1 → k=20 → 站立稳态 121°C
            elif k == "F9_fan_stalled":
                # 风扇停转：外壳仍在自然对流，热阻只升到 4 倍。
                # 站立 39°C、奔跑 85°C——**轻度**故障，明显偏离正常但
                # 不该和 F2（烧毁级）混为一谈，这正是要区分的两种程度。
                rth *= (1.0 + 3.0 * sev)
            elif k == "F10_ambient_hot":
                # 环境温度抬升：全体读数的**台阶上移**，与负载无关。
                # 站立 Imax=5A 时自发温升只有 4.8K，所以环境必须抬到 75°C
                # 以上才越过告警线——55K 对应"夏天暴晒后的停机坪"。
                t_amb += 55.0 * sev
            elif k == "F5_load_spike":
                extra = 1.0 + 3.0 * sev      # 产热 ∝ I²，extra 直接乘在 I² 上
        return rth, extra, t_amb

    def _apply_observation_fault(self, active: bool, obs: dict[str, np.ndarray],
                                 valid: np.ndarray) -> None:
        """观测层：只改读到的数，物理状态不动。原地修改 obs 里的数组。

        obs 的键对齐 IDL MotorState：temperature / current / tau_est /
        q / dq / q_raw / dq_raw。
        """
        if not active:
            return
        sev = self._severity()
        k = self.fault.key
        i_obs = obs["current"]
        if k == "F4_current_bias":
            # 电流传感器零点漂移：**只抬升读数**，电机真实电流不变，
            # 所以温度不会跟着涨——这正是它和 F5（真加载）的区分点。
            # +5A 是相对量：修正后正常站立约 5A，所以这是"读数翻倍而
            # 温度不动"，偏差足够显眼；若按旧量级（30A）标定反而看不出来。
            i_obs[:] += 5.0 * sev
        elif k == "F3_packet_loss":
            drop = self._rng.random(12) < (0.15 * sev)
            valid[drop] = False
            # 丢掉的读数不是"0"，是"没有"——用 NaN 表达，下游应据 valid 跳过
            for arr in obs.values():
                arr[drop] = np.nan
        elif k == "F8_motor_lost":
            # 演示主关节持续失联（不是写死的 FL_hip——见 demo_joint()）
            j = ACTUATORS.index(demo_joint())
            valid[j] = False
            for arr in obs.values():
                arr[j] = np.nan
            self._lost_count += 1
        elif k == "F19_crc_error":
            # CRC 失败：帧被丢弃，等效于随机小比例丢包 + 失败率上升
            drop = self._rng.random(12) < (0.10 * sev)
            valid[drop] = False
            for arr in obs.values():
                arr[drop] = np.nan
            self._crc_fails += int(drop.sum())
        elif k == "F6_encoder_jump":
            # 编码器跳变：**滤波后的 q 相对 raw 出现固定偏移**，raw 保持真实。
            # 这是编码器类故障的判别特征——只看 q 会以为腿真的偏了，
            # 对照 q_raw 才能判定"信号错了，机构没坏"。
            j = ACTUATORS.index(demo_joint())
            obs["q"][j] += 0.18 * sev
            # dq 由差分得到，跳变会让它出现尖峰
            obs["dq"][j] += 2.5 * sev
        elif k == "F7_tau_est_fail":
            # 力矩估算失效：FOC 的 kt 标定漂了，tau_est 与真实 |I|·kt 脱钩。
            # 电流读数不变（电流环自己没问题），只是"由电流推算力矩"这一步错了。
            j = ACTUATORS.index(demo_joint())
            obs["tau_est"][j] *= (1.0 - 0.45 * sev)

    # ---------------------------------------------------------------- 步进
    def step(self, telemetry_steps: int = 1) -> list[Telemetry]:
        """推进 n 个遥测周期（默认 1 个 = 10ms 仿真时间），返回期间每次采样。"""
        with self._lock:
            out: list[Telemetry] = []
            for _ in range(max(0, telemetry_steps)):
                out.append(self._step_once())
            return out

    def _step_once(self) -> Telemetry:
        mj = self._mujoco
        m, d = self._model, self._data
        dt = 1.0 / TELEMETRY_HZ
        active = self._fault_active()

        d.qfrc_applied[:] = 0.0
        d.xfrc_applied[:] = 0.0
        self._apply_physics_fault(active)

        targets = self.posture.targets(self._t)
        for a in range(12):
            tau = (self._kp[a] * (targets[a] - d.qpos[self._qidx[a]])
                   - self._kd[a] * d.qvel[self._dof[a]])
            d.ctrl[a] = float(np.clip(tau,
                                      m.actuator_ctrlrange[a][0],
                                      m.actuator_ctrlrange[a][1]))
        for _ in range(self._substeps):
            mj.mj_step(m, d)

        torque = np.array([d.ctrl[a] for a in range(12)], dtype=float)
        current = np.abs(torque) / self._kt

        # 一阶热模型。F2/F9 只放大热阻：电流照常、温度爬升，这是它的判别特征。
        rth, extra, t_amb = self._thermal_params(active)
        cth = MOTOR_PARAMS["Cth"] / max(0.05, self.config.thermal_scale)
        self._T = self._T + ((current ** 2) * extra * self._R
                            - (self._T - t_amb) / rth) * dt / cth
        # 钳到传感器量程顶端（见 MOTOR_TEMP_CEILING）。线性模型本身没有上限，
        # 不钳就会算出 598°C 这种读数——而驱动是 int8_t，读不到。
        # 真机上这个状态意味着"温度保护已触发、读数顶死"。
        np.clip(self._T, None, MOTOR_TEMP_CEILING, out=self._T)

        pos = d.qpos
        up_z = 1.0 - 2.0 * (pos[4] ** 2 + pos[5] ** 2)
        if not self._fell:
            self._fell = bool(pos[2] < self.posture.fallen_min_z or up_z < 0.7)

        # 观测量：真值打底，再按故障改。raw 与滤波值分开，
        # 否则编码器类故障（F6）没有分辨依据。
        dq_true = np.array([d.qvel[i] for i in self._dof], dtype=float)
        q_true = np.array([d.qpos[i] for i in self._qidx], dtype=float)
        obs = {
            "temperature": self._T.copy(),
            "current": current.copy(),
            "tau_est": torque.copy(),
            "q": q_true.copy(),
            "dq": dq_true.copy(),
            # raw 是编码器原始计数换算值：与滤波值差一个量化噪声量级
            "q_raw": q_true + self._rng.normal(0, 0.0015, 12),
            "dq_raw": dq_true + self._rng.normal(0, 0.02, 12),
        }
        valid = np.ones(12, dtype=bool)
        self._apply_observation_fault(active, obs, valid)
        t_obs = obs["temperature"]
        i_obs = obs["current"]
        tau_obs = obs["tau_est"]

        # ---- 关节以外：电池 / IMU / 热管理 / 通信 / 整机 ----
        sev = self._severity()
        k = self.fault.key
        # 电池看到的是**整机**电流。关节电流是各关节的相电流（|tau|/kt），
        # 12 路直接相加得到一百多安，远超这块 6.8Ah 电池的放电能力——那样
        # 母线电压会一直贴着下限，物理上是错的。
        # 正确关系是**功率守恒**：母线电流 = Σ|tau·dq| / (η·V) + 电控常驻。
        # 这样负载加重→母线电流上升→电压下垂的因果链仍然是真实的。
        mech_power = float(np.nansum(np.abs(torque * d.qvel[self._dof])))
        v_now = max(15.0, self._battery.state.voltage)
        bus_current = mech_power / (0.78 * v_now) + ELEC_IDLE_CURRENT
        # F15：电气过载是"负载侧要的电流超出电池能力"，所以要真的把电流推上去，
        # 只置 status 位是改读数、不是改物理，下游看到的 current 该跟着涨。
        load_extra = 1.0
        if active and k == "F5_load_spike":
            load_extra = 1.0 + 0.6 * sev
        elif active and k == "F15_battery_overcurrent":
            load_extra = 2.4 * sev
        batt = self._battery.step(
            total_current=bus_current,
            dt=dt,
            load_extra=load_extra,
            voltage_sag=(5.0 * sev) if (active and k == "F14_bus_voltage_drop") else 0.0,
            imbalance=(BATTERY_CELL_IMBALANCE * sev)
            if (active and k == "F12_cell_imbalance") else 0.0,
            overcurrent=bool(active and k == "F15_battery_overcurrent"),
            # F11 是"换上一块快没电的电池"——SOC 由外部给定，不是容量积分的结果
            soc_override=(100.0 - 85.0 * sev) if (active and k == "F11_battery_low") else None,
            # F13 是"电池包散热不良"——抬高热平衡点，温度自己爬上去
            thermal_offset=(30.0 * sev) if (active and k == "F13_battery_hot") else 0.0,
            thermal_scale=self.config.thermal_scale,
        )

        imu = self._imu.step(
            quat=tuple(float(v) for v in pos[3:7]),
            gyro_true=tuple(float(v) for v in d.qvel[3:6]),
            accel_true=self._accel_true(),
            dt=dt,
            gyro_bias=(0.06 * sev) if (active and k == "F16_gyro_bias") else 0.0,
            accel_fail=bool(active and k == "F17_accel_fail"),
            extra_temp=8.0 if (active and k == "F10_ambient_hot") else 0.0,
        )

        temp_max = float(np.nanmax(t_obs)) if np.any(valid) else float(np.nanmax(self._T))
        self._thermal = ThermalState(
            fan_frequency=fan_frequency(
                temp_max=temp_max, stalled=bool(active and k == "F9_fan_stalled")),
            bandwidth=340 if (active and k == "F20_bandwidth_low") else 1000,
            crc_fail_rate=min(0.9, 0.02 * sev) if (active and k == "F19_crc_error") else 0.0,
            temperature_ntc1=float(temp_max * 0.6 + 12.0),
        )

        feet = self._foot_forces()
        speeds = self._foot_speeds()
        gait_declared = 1 if self.posture.gait_freq > 0 else 0
        if active and k == "F21_gait_anomaly":
            # 步态异常：控制器**声明**的步态与实际运动不符。
            # 它声称在 trot（声明 1），实际有一条腿始终不着地——足端力恒为 0。
            # 判别依据只能是"声明 vs 足端力相位"，单看 gait_type 抓不到。
            gait_declared = 1 if gait_declared != 1 else 2
            feet = (feet[0], feet[1], 0.0, feet[3])   # RL 拖行
        vel_w = np.asarray(d.qvel[0:3], dtype=float)
        w_w = np.asarray(d.qvel[3:6], dtype=float)
        self._system = SystemState(
            error_code=(0x0001 if k == "F23_system_error" else 0) if active else 0,
            gait_type=gait_declared,
            body_height=float(pos[2]),
            foot_force=feet,
            mode=1,
            progress=float((self.posture.gait_freq * self._t) % 1.0)
            if self.posture.gait_freq > 0 else 0.0,
            position=(float(pos[0]), float(pos[1]), float(pos[2])),
            velocity=(float(vel_w[0]), float(vel_w[1]), float(vel_w[2])),
            yaw_speed=float(w_w[2]),
            foot_raise_height=0.0,
            foot_speed_body=speeds,
        )

        tel = Telemetry(
            t=self._t, temperature=t_obs, current=i_obs, torque=tau_obs,
            q=obs["q"], dq=obs["dq"], q_raw=obs["q_raw"], dq_raw=obs["dq_raw"],
            valid=valid,
            base_pos=(float(pos[0]), float(pos[1]), float(pos[2])),
            base_quat=(float(pos[3]), float(pos[4]), float(pos[5]), float(pos[6])),
            base_vel=float(np.linalg.norm(d.qvel[0:2])),
            battery=batt, imu=imu, thermal=self._thermal, system=self._system,
        )
        self._t += dt
        self._steps += 1
        self._last = tel
        return tel

    def _accel_true(self) -> tuple[float, float, float]:
        """机身加速度真值（有限差分 + 重力），给 IMU 读数做基准。"""
        d = self._data
        # MuJoCo 的 qacc 是广义加速度；freejoint 前 3 维即机身线加速度
        acc = np.array([d.qacc[0], d.qacc[1], d.qacc[2]], dtype=float)
        # 机体坐标系下的比力 ≈ 线加速度 + 重力在机体的分量。这里做简化：
        # 用世界系加速度 + 重力，再转到机身系（演示对 IMU 精度要求不高，
        # 真实接入时换成 MuJoCo 的 sensordata 更准）。
        q = d.qpos[3:7]
        g_world = np.array([0.0, 0.0, -9.81])
        a_world = acc + g_world
        rot = _quat_to_mat(tuple(float(v) for v in q))
        a_body = rot.T @ a_world
        return (float(a_body[0]), float(a_body[1]), float(a_body[2]))

    def _foot_forces(self) -> tuple[float, float, float, float]:
        """四足接触力（FL/FR/RL/RR）。从 MuJoCo 接触中按足端 geom 汇总。

        用于 F18 翻倒（足端力归零）与 F21 步态异常（某条腿不着地）。
        """
        d = self._data
        out = [0.0, 0.0, 0.0, 0.0]
        for i in range(d.ncon):
            c = d.contact[i]
            g1, g2 = int(c.geom1), int(c.geom2)
            # 只看与地面(geom 0)的接触，取另一方 geom 所属 body
            if g1 != 0 and g2 != 0:
                continue
            other = g2 if g1 == 0 else g1
            body = int(self._model.geom_bodyid[other])
            leg = self._foot_body_to_leg.get(body)
            if leg is None:
                continue
            f = np.zeros(6)
            self._mujoco.mj_contactForce(self._model, d, i, f)
            out[leg] += float(abs(f[0]))
        return (out[0], out[1], out[2], out[3])

    def _foot_speeds(self) -> tuple[float, float, float, float]:
        """四足足端速度模长（m/s），**机身坐标系**（对齐 IDL foot_speed_body）。

        **这是判定打滑的唯一依据**：着地腿的足端相对地面应该静止，所以它
        相对机身的速度≈0；打滑时这一项显著非零。只看 foot_force 判不出来——
        打滑的脚照样受力，力的大小几乎不变。
        """
        d = self._data
        out = [0.0, 0.0, 0.0, 0.0]
        rot = _quat_to_mat(tuple(float(v) for v in d.qpos[3:7]))
        for leg, bid in self._leg_to_foot_body.items():
            vel = np.zeros(6)
            # flg_local=0 → 前 3 项是世界系线速度
            self._mujoco.mj_objectVelocity(
                self._model, d, self._mujoco.mjtObj.mjOBJ_BODY, bid, vel, 0)
            v_body = rot.T @ np.asarray(vel[0:3], dtype=float)
            out[leg] = float(np.linalg.norm(v_body))
        return (out[0], out[1], out[2], out[3])

    # ---------------------------------------------------------------- 画面
    # 离屏渲染的可用上限，由模型 XML 的 `visual/global/@offwidth,@offheight`
    # 决定（go2 场景里是 640×480）。**超过它会直接抛 ValueError**：
    #   "Image width 880 > framebuffer width 640"
    # 实测踩过：控制台请求 880×550 → 503 → 页面 3D 画面全黑。
    # 所以这里既钳制入参，也在建模型时把 framebuffer 抬到目标尺寸。
    OFFSCREEN_W = 1280
    OFFSCREEN_H = 800

    def render_frame(self, width: int = 640, height: int = 400):
        """渲染当前状态为 RGB 数组。**必须在拥有渲染器的那个线程调用。**

        线程亲和性是硬约束，不是风格问题：MuJoCo 的离屏 Renderer 持有一个
        GL 上下文，而 GLFW/OpenGL 上下文绑在创建它的线程上。从另一个线程
        （比如 uvicorn 的请求线程池）调用 `update_scene`/`render`，
        在 Windows 上直接访问冲突崩溃（实测 exit 0xC0000005，日志里连
        请求都没来得及记）。所以控制台走 `SimSession` 的帧缓存：
        仿真线程渲染，HTTP 线程只读已经编码好的字节。
        """
        width = max(64, min(int(width), self.OFFSCREEN_W))
        height = max(48, min(int(height), self.OFFSCREEN_H))
        with self._lock:
            if self._renderer is None:
                # 记住是谁把 GL 上下文建起来的；之后换线程调用会直接崩，
                # 所以宁可抛一个能看懂的错误，也不要进程无声无息地死。
                self._gl_thread = threading.get_ident()
                mj = self._mujoco
                # 固定按最大尺寸建一次。不改尺寸重建的原因见类文档与
                # render_jpeg：close() 后再建同样会崩。
                self._renderer = mj.Renderer(self._model, height=self.OFFSCREEN_H,
                                             width=self.OFFSCREEN_W)
                cam = mj.MjvCamera()
                mj.mjv_defaultCamera(cam)
                cam.distance = 2.1
                cam.elevation = -18
                cam.lookat[:] = [float(self._data.qpos[0]),
                                 float(self._data.qpos[1]), 0.25]
                self._camera = cam
            cam = self._camera
            # 相机跟随机身，否则狗跑出画面
            cam.lookat[:] = [float(self._data.qpos[0]),
                             float(self._data.qpos[1]), 0.25]
            cam.azimuth = 130
            if threading.get_ident() != self._gl_thread:
                raise RuntimeError(
                    "MuJoCo 离屏渲染只能在建渲染器的线程调用"
                    f"（建在 {self._gl_thread}，现在 {threading.get_ident()}）。"
                    "HTTP 层请用 SimSession.frame_jpeg() 读缓存帧。")
            self._renderer.update_scene(self._data, camera=cam)
            img = self._renderer.render()
        return _fit(img, width, height)

    def render_jpeg(self, width: int = 640, height: int = 400,
                    quality: int = 78, t: float | None = None) -> bytes:
        """渲染并编码为 JPEG。**同样必须在本线程调用**（见 render_frame）。

        HTTP 层不要直接调这个——走 `SimSession.frame_jpeg()`。
        """
        import io

        from PIL import Image
        out = Image.fromarray(self.render_frame(width=width, height=height))
        buf = io.BytesIO()
        out.save(buf, "JPEG", quality=quality)
        return buf.getvalue()

    def close(self) -> None:
        """释放渲染器。**必须在建它的那个线程调用**，否则整个进程段错误。

        MuJoCo 的 Renderer 在析构时要去注销 GL 资源，跨线程做同样会崩
        （实测 `Segmentation fault`，即使主线程只是调了 stop()）。
        所以从别的线程调到这里，只做一个标记，真正的释放留给仿真线程
        退出前的 `close()`（见 `SimSession._run` 的 finally）。
        """
        with self._lock:
            if self._renderer is not None:
                if self._gl_thread not in (None, threading.get_ident()):
                    self._close_pending = True
                    return
                self._renderer.close()
                self._renderer = None
                self._gl_thread = None

    # ---------------------------------------------------------------- 视图
    def snapshot(self) -> dict:
        """给 UI 的状态快照（不含逐采样明细）。

        关节部分给真值（`_T` / ctrl 推算），子系统部分取最近一次采样——
        与真机一致：UI 看到的子系统读数就是下发给 data_dog 的那一份。
        """
        with self._lock:
            out = {
                "t": round(self._t, 3),
                "steps": self._steps,
                "posture": self.posture.key,
                "posture_label": self.posture.label,
                "fault": self.fault.describe(),
                "severity": self.config.severity,
                "seed": self.seed,
                "fault_start_s": self.config.fault_start_s,
                "fault_active": self._fault_active(),
                "fell": self._fell,
                "base_pos": [round(v, 4) for v in self._data.qpos[0:3]],
                "up_z": round(float(1.0 - 2.0 * (self._data.qpos[4] ** 2
                                                 + self._data.qpos[5] ** 2)), 4),
                "temp_max": round(float(np.nanmax(self._T)), 2),
                "current_max": round(float(np.max(np.abs(self._data.ctrl)
                                                  / self._kt)), 2),
            }
            if self._last is not None:
                out["subsystems"] = {
                    "battery": self._last.battery.as_metrics()
                    if self._last.battery else {},
                    "imu": self._last.imu.as_metrics()
                    if self._last.imu else {},
                    "thermal": self._last.thermal.as_metrics()
                    if self._last.thermal else {},
                    "system": self._last.system.as_metrics()
                    if self._last.system else {},
                }
            return out


__all__ = ["Go2Sim", "SimConfig", "Telemetry", "ACTUATORS",
           "_JOINT_TO_COMPONENT", "_COMPONENT_TO_JOINT", "TELEMETRY_HZ"]
