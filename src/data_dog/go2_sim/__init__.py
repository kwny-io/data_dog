"""Go2 实时仿真接入器：MuJoCo 跑真物理 → 出遥测 → MQTT 发给 data_dog。

与 `data_dog.mujoco_sim` 的分工
------------------------------
`mujoco_sim` 是**批量数据集生产线**：跑固定时长的 trial，导出标量汇总，喂评估。
本包是**在线演示接入器**：持续步进一个长跑仿真，按真实节奏产出遥测事件，
经 MQTT 送到 data_dog 的流式入口，走的是和真机完全相同的链路。

设计边界（诚实标注）
--------------------
- 物理是真的：扭矩来自 MuJoCo 动力学求解，不是公式拟合。
- 温度是**热扩展层**：一阶热模型（`dT = (I²R − (T−T_amb)/Rth)·dt / Cth`），
  与 `mujoco_sim.simulate` 同一套参数。MuJoCo 本身没有温度场。
- 故障分三层注入，与真机故障的层次对应：
    * 物理层（F1 堵转 / F5 负载）—— 改仿真本身
    * 热层（F2 散热恶化）—— 改热模型
    * 观测层（F3 丢包 / F4 电流偏置）—— 只改"读到的数"
"""
from __future__ import annotations

__all__ = [
    "FaultSpec", "Posture", "SimConfig", "Go2Sim",
    "FAULT_LIBRARY", "POSTURES",
]

from .faults import FAULT_LIBRARY, FaultSpec
from .postures import POSTURES, Posture
from .rig import Go2Sim, SimConfig
