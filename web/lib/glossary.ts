/**
 * Exact-name glossary — the last mile of "translate identifiers to language".
 *
 * ## Why this exists on top of vocabulary.ts
 *
 * `vocabulary.ts` composes a phrase token by token. That is the right
 * architecture for an OPEN set of identifiers (component refs, ad-hoc signals
 * from an LLM), but it is measurably wrong for the CLOSED set of names we
 * actually ship. Measured 2026-10-30 after adding the fault words:
 *
 *   POWER_SOC_LOW          → "功率电量过低"     (should be "电量过低")
 *   COM_CRC_ERROR_RATE     → "com CRC误差率"    (should be "CRC 错误率偏高")
 *   SYS_ERROR_CODE_SET     → "sys 误差 code 置位"(should be "整机报错")
 *   MOTOR_STALL_SUSTAINED  → "电机堵转持续"     (should be "电机持续堵转")
 *
 * Every one of those is a word that means something different in Chinese than
 * its parts suggest. Token composition cannot fix that; only an exact entry can.
 *
 * So: **exact table first, token composition as the fallback.** Unknown
 * identifiers still degrade gracefully through vocabulary.ts — this file only
 * adds precision where precision is achievable.
 *
 * ## Keeping the two sides in agreement
 *
 * The Python side (`src/data_dog/go2_sim/semantic.py`) is the source of truth
 * for fault names and rule names. This file mirrors it for the web bundle,
 * which cannot import Python. `tests/test_go2_sim_semantic.py` parses this file
 * and fails if the two drift — so a rule added on one side cannot silently
 * render as snake_case on the other.
 */

/** Fault keys (`F1_stall`) and their names. Mirrors `semantic.FAULT_NAME`. */
export const FAULT_NAME: Record<string, string> = {
  normal: "正常",
  F1_stall: "堵转",
  F6_encoder_jump: "编码器跳变",
  F7_tau_est_fail: "力矩估算失效",
  F8_motor_lost: "电机失联",
  F4_current_bias: "电流传感器零漂",
  F2_temp_rise: "散热失效",
  F9_fan_stalled: "风扇停转",
  F10_ambient_hot: "环境高温",
  F11_battery_low: "电量低",
  F12_cell_imbalance: "单体电压不均衡",
  F13_battery_hot: "电池过温",
  F14_bus_voltage_drop: "母线电压跌落",
  F15_battery_overcurrent: "电池过流",
  F16_gyro_bias: "陀螺零偏",
  F17_accel_fail: "加速度计失效",
  F18_fallen: "翻倒",
  F3_packet_loss: "通信丢包",
  F19_crc_error: "CRC 错误",
  F20_bandwidth_low: "带宽下降",
  F5_load_spike: "负载骤增",
  F21_gait_anomaly: "步态异常",
  F22_foot_slip: "足端打滑",
  F23_system_error: "整机报错",
};

/** Rule ids and their names. Mirrors `semantic.RULE_NAME`. */
export const RULE_NAME: Record<string, string> = {
  MOTOR_STALL_SUSTAINED: "电机持续堵转",
  MOTOR_LOAD_SPIKE: "电机负载骤增",
  MOTOR_TEMP_PROTECT: "电机过温保护",
  MOTOR_TEMP_DEGRADE: "电机高温降额",
  MOTOR_TEMP_RISE_UNDER_LOAD: "负载下温度上升",
  MOTOR_ENCODER_JUMP: "编码器跳变",
  MOTOR_TAU_EST_FAIL: "力矩估算失效",
  MOTOR_LOST_COUNTER_RISING: "电机失联计数上涨",
  MOTOR_CURRENT_BIAS: "电流零点漂移",
  MOTOR_MODE_FLAG_SET: "电机模式位异常",
  POWER_SOC_LOW: "电量过低",
  POWER_BUS_OVERCURRENT: "母线过流",
  POWER_BUS_VOLTAGE_LOW: "母线电压过低",
  BATTERY_CELL_IMBALANCE: "单体电压不均衡",
  BATTERY_OVERHEAT: "电池过温",
  COMM_PACKET_LOSS: "通信丢包",
  COMM_FIELD_INCOMPLETE: "报文缺字段",
  COM_CRC_ERROR_RATE: "CRC 错误率偏高",
  COM_BANDWIDTH_LOW: "带宽不足",
  COM_FAN_STALLED: "风扇停转",
  POSTURE_FALLEN: "机身翻倒",
  SYS_ERROR_CODE_SET: "整机报错",
  FIELD_SANITY_TEMP_INVALID: "温度字段越界",
  // 真算子（operators/generated/*.yaml）
  MOTOR_TEMP_TREND: "电机热退化风险",
  MOTOR_TEMP_OVERPROTECT: "电机过温停机",
  BATT_SOC_LOW: "电池电量过低",
  // 演示夹具 / 评测样本
  MOTOR_TEMP_TREND_V2: "电机热退化风险（V2 阈值）",
  BATT_VOLT_TREND: "电池电压趋势异常",
  SPURIOUS_CHECK: "误报复核",
  LOW_CONF: "低置信度",
  MANUAL_IMPORT: "人工导入",
  UNKNOWN: "未识别规则",
};

/**
 * Signal families and their names. Mirrors `semantic.SIGNAL_NAME`.
 *
 * A candidate's `signal` is NOT `event_type` and not a rule id — it comes from
 * `_signal_of(rule_id, field)`, which classifies by keyword and otherwise
 * **returns the bare field name**. That is why `tau_est` / `q` / `lost` / `soc`
 * show up on candidate cards. Table keyed by the literal signal value.
 */
export const SIGNAL_NAME: Record<string, string> = {
  temperature_high: "温度过高",
  temperature_rising: "温度上升",
  current_anomaly: "电流异常",
  fault_flag: "故障标志位",
  temperature: "温度",
  current: "电流",
  tau_est: "估计力矩",
  q: "关节角度",
  lost: "失联计数",
  // `POWER_SOC_LOW` is declared twice with different fields: the simulator
  // uses the payload key `soc`, the real pipeline uses the IDL-mapped
  // `bms_soc`. Both reach the card, so both need a name.
  soc: "电量",
  bms_soc: "电量",
  power_v: "母线电压",
  cell_v_spread: "单体压差",
  fan_frequency: "风扇转速",
  bandwidth: "通信带宽",
  body_height: "机身高度",
};

/** Component kind (before the "."). Mirrors `semantic.COMPONENT_KIND_NAME`. */
export const COMPONENT_KIND_NAME: Record<string, string> = {
  joint_motor: "关节电机",
  motor: "电机",
  battery: "电池",
  bms: "BMS",
  controller: "主控",
  imu: "IMU",
  lidar: "激光雷达",
  camera: "相机",
  main: "主机",
};

/** Component instance (after the "."). Mirrors `semantic.COMPONENT_PART_NAME`. */
export const COMPONENT_PART_NAME: Record<string, string> = {
  front_left_hip: "左前髋",
  front_right_hip: "右前髋",
  rear_left_hip: "左后髋",
  rear_right_hip: "右后髋",
  front_left_knee: "左前膝",
  front_right_knee: "右前膝",
  rear_left_knee: "左后膝",
  rear_right_knee: "右后膝",
  front_left_foot: "左前足",
  front_right_foot: "右前足",
  rear_left_foot: "左后足",
  rear_right_foot: "右后足",
  main: "主机",
  bms: "BMS",
};

/** Derived event types and their names. Mirrors `semantic.EVENT_TYPE_NAME`. */
export const EVENT_TYPE_NAME: Record<string, string> = {
  motor_over_temperature: "电机过温",
  joint_motor_thermal_degradation: "关节电机热退化",
  joint_motor_temperature_rising_under_normalized_load: "负载未变但温度上升",
  battery_over_discharge: "电池过放",
  battery_cell_imbalance: "电池单体不均衡",
  communication_packet_loss: "通信丢包",
  unknown_event: "未识别事件",
};
