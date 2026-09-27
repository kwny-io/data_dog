"""合成遥测数据生成器（Go2 物理参数 + 故障注入 + ground truth）。

目标：产出一批"物理自洽、带真实标签"的遥测时间序列，喂给 data_dog 的 Agent 调查，
再用 ground truth 算调查准确率。MuJoCo 跑通后，这个生成器是其"无 MuJoCo 依赖"的降级方案
（接口相同：输出 N 秒、N 个关节、采样率固定的遥测流 + 每样本一段故障标签）。

物理模型（基于 Unitree Go2 公开参数近似）：
- 关节扭矩 = 负载扭矩 + 阻尼 + 库仑摩擦（Go2 阻尼矩阵量级 ~0.1 N·m·s/rad）
- 电流  i = τ / kt        （kt 用无刷电机扭矩常数近似）
- 温度  一阶热模型：dT = (I²·R - (T-T_amb)/Rth)·dt / Cth
- 观测  各量加高斯噪声

故障注入（5 类）：
  F1_stall       卡死/堵转：扭矩/电流骤升到限幅
  F2_temp_rise   热退化：散热失效或持续大负载 → 温度爬升越阈值
  F3_packet_loss 通信丢包：观测随机缺失（NaN/丢点）
  F4_current_bias 电流偏置：传感器零漂（电流整体 +Δ）
  F5_load_spike  外部大负载骤增：一段突增扭矩 → 电流/温度上探

每条样本都带 ground truth：一段(部件, 时间窗, 故障类型) 的标签，供评估对比。
"""
from .generator import (
    DEFAULT_PARAMS,
    FAULT_DEFS,
    Go2MotorModel,
    SynthGenConfig,
    SynthSample,
    generate_sample,
    synthesize_batch,
)

__all__ = [
    "Go2MotorModel",
    "SynthGenConfig",
    "SynthSample",
    "generate_sample",
    "synthesize_batch",
    "DEFAULT_PARAMS",
    "FAULT_DEFS",
]
