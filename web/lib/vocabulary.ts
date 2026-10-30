/**
 * Display vocabulary — the translation layer between machine identifiers and
 * operator language.
 *
 * The problem this solves: the wire format is full of snake_case identifiers
 * (`joint_motor.rear_right_knee`, `motor_over_temperature`, `MOTOR_TEMP_PROTECT`).
 * Rendering them verbatim next to Chinese labels makes the UI read like a debug
 * dump — two vocabularies at the same visual rank. But we cannot simply delete
 * them: an operator needs `alert_9f8e7d6c` to paste into a ticket, and
 * `event_type` to grep logs.
 *
 * So: translate to human language for the primary read, and demote the raw
 * value to a secondary rank (see `RawValue` in primitives). This module is the
 * translation half.
 *
 * DESIGN CONSTRAINT — identifiers are an OPEN set. `event_type` is set by the
 * investigation report, `signal` by whatever detector fired, `component_ref` by
 * the device map. A hardcoded lookup table would silently miss every value not
 * anticipated and fall back to dumping the raw identifier — which is exactly the
 * "dirty" failure we are fixing. So this is NOT a lookup table; it is a
 * TOKEN-BY-TOKEN composition with graceful degradation:
 *
 *   "temperature_rising_under_normalized_load"
 *     → ["temperature","rising","under","normalized","load"]
 *     → ["温度","持续上升","在","正常化","负载"]
 *     → "温度持续上升"                    (recognised tokens joined, glue dropped)
 *
 * An unrecognised token is never a reason to show the raw identifier: it is
 * kept verbatim but lowercased-in-place, so the worst case is a mixed-language
 * phrase, never raw snake_case.
 */

import {
  COMPONENT_KIND_NAME,
  COMPONENT_PART_NAME,
  EVENT_TYPE_NAME,
  FAULT_NAME,
  RULE_NAME,
  SIGNAL_NAME,
} from "./glossary";

// ------------------------------------------------------------- dictionaries

/** Position tokens, so an unlisted joint still reads correctly. */
const POSITION: Record<string, string> = {
  front: "前",
  rear: "后",
  left: "左",
  right: "右",
  front_left: "左前",
  front_right: "右前",
  rear_left: "左后",
  rear_right: "右后",
};

/** Joint tokens, for composing an unlisted combination. */
const JOINT: Record<string, string> = {
  hip: "髋",
  knee: "膝",
  foot: "足",
  ankle: "踝",
  shoulder: "肩",
  elbow: "肘",
};

// ----------------------------------------------------------------- tokens

/** Event / signal tokens. Order matters only for jieba-free word building. */
const TOKEN: Record<string, string> = {
  // nouns
  sample: "采样",
  count: "数",
  temperature: "温度",
  temp: "温度",
  current: "电流",
  voltage: "电压",
  torque: "力矩",
  position: "位置",
  power: "功率",
  soc: "电量",
  bus: "母线",
  motor: "电机",
  battery: "电池",
  bms: "BMS",
  load: "负载",
  thermal: "热",
  err: "误差",
  error: "误差",
  fault: "故障",
  fault_: "故障",
  shut: "关",
  down: "断",
  spike: "骤升",
  sag: "跌落",
  drop: "下降",
  high: "过高",
  low: "过低",
  over: "过",
  // `under` 在标识符里有两种含义，逐词翻译分不清：
  //   `motor_over_temperature`   → "过"（电气语境：超过）
  //   `rising_under_normal_load` → "在…下"（条件语境：负载没变的情况下）
  // 译成"欠"（欠载）只在 `under_load` 这一种里对，而它正好是最常见的反例。
  // 折中成一字"下"：`温度持续上升下负载` 仍不通顺，但比 `欠正常化负载`
  // 少一个错误含义；真正通顺要靠 semantic.py 里写死的整句。
  under: "下",
  rising: "持续上升",
  falling: "持续下降",
  normalized: "正常化",
  normal: "正常",
  abnormal: "异常",
  degraded: "退化",
  degradation: "退化",
  tracking: "跟随",
  protection: "保护",
  protect: "保护",
  checking: "检查",
  check: "检查",
  spurious: "误报",
  alarm: "告警",
  state: "状态",
  synthetic: "合成",
  anomaly: "异常",
  unknown: "未知",
  emergency: "紧急",
  event: "事件",
  control: "控制",
  controller: "主控",
  // ---- 故障词汇表（2026-10-30 补）
  //
  // 补之前实测：`joint_motor_temperature_rising_under_normalized_load` 译成
  // "温度持续上升欠正常化负载"，`POWER_SOC_LOW` 译成"功率电量过低"。
  // 原因是这些业务词一个都不在表里，`under` 还被当介词译成了"欠"。
  // 下面这批词专门为**故障/告警**这一路补，覆盖 23 条故障键
  // 与 22 条规则 ID 里的全部实义词。
  //
  // 与 Python 侧 `src/data_dog/go2_sim/semantic.py` 的 `RULE_NAME` 对齐：
  // 逐词是兜底，那边写死的整句才是首选（例：`MOTOR_TEMP_PROTECT`
  // 拼出来是"电机温度保护"，而那边给的是"电机过温保护"——后者更像人话）。
  stall: "堵转",
  stalled: "停转",
  slip: "打滑",
  gait: "步态",
  crc: "CRC",
  bandwidth: "带宽",
  packet: "报文",
  loss: "丢失",
  encoder: "编码器",
  tau: "力矩",
  bias: "漂移",
  fallen: "翻倒",
  fall: "翻倒",
  joint: "关节",
  sustained: "持续",
  jumped: "跳变",
  jump: "跳变",
  incomplete: "不完整",
  invalid: "越界",
  sanity: "合法性",
  field: "字段",
  rate: "率",
  degrade: "降额",
  failed: "失效",
  fail: "失效",
  lost: "失联",
  counter: "计数",
  mode: "模式",
  flag: "标志",
  set: "置位",
  cell: "单体",
  imbalance: "不均衡",
  overheat: "过温",
  overcurrent: "过流",
  ambient: "环境",
  hot: "高温",
  fan: "风扇",
  // glue — recognised but dropped when joining
  of: "",
  the: "",
  a: "",
  an: "",
  on: "",
  in: "",
  at: "",
  to: "",
  for: "",
  and: "",
  is: "",
};

const GLUE = new Set(["of", "the", "a", "an", "on", "in", "at", "to", "for", "and", "is", "with"]);

// ---------------------------------------------------------------- helpers

/** Split an identifier into lowercase word tokens on `.`, `_`, `-`, camelCase. */
export function splitTokens(s: string): string[] {
  return s
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2") // camelCase → camel Case
    .split(/[._\-\s]+/)
    .map((t) => t.trim().toLowerCase())
    .filter(Boolean);
}

/** Translate one token; unrecognised tokens pass through verbatim (never raw snake_case). */
function word(t: string): string {
  const hit = TOKEN[t];
  if (hit !== undefined) return hit;
  // All-caps acronyms (BMS, IMU) read fine as-is.
  if (/^[A-Z]{2,}$/.test(t)) return t;
  return t;
}

/**
 * Join translated tokens into a phrase.
 *
 * Chinese tokens concatenate ("温度"+"持续上升" → "温度持续上升"), but two
 * *unrecognised* English tokens must not fuse — joining "sample"+"foo" into
 * "samplefoo" is worse than showing the raw key. So an unrecognised token is
 * space-separated from its neighbour, which keeps a partially-translated
 * identifier readable rather than gluing it into a non-word.
 */
function joinParts(tokens: string[]): string {
  let out = "";
  let prevKnown = true;
  for (const t of tokens) {
    if (t === "") continue;
    const known = TOKEN[t] !== undefined;
    if (out === "") out = word(t);
    else out += (known && prevKnown ? "" : " ") + word(t);
    prevKnown = known;
  }
  return out;
}

/** Compose a phrase from an identifier, dropping glue words. */
function compose(s: string): string {
  const joined = joinParts(splitTokens(s).filter((t) => !GLUE.has(t)));
  return joined || s;
}

// ------------------------------------------------------------ public API

/**
 * "joint_motor.front_left_hip" → "左前髋关节电机"
 * "battery.main"              → "主机电池"
 * "joint_motor.rear_right_knee" → "右后膝关节电机"   (was: raw, fell through the old dict)
 */
export function componentName(ref: string): string {
  const [kindRaw, partRaw] = ref.split(".");
  const kind = kindRaw ? (COMPONENT_KIND_NAME[kindRaw] ?? compose(kindRaw)) : "";
  const part = partRaw
    ? // exact instance, else compose from position + joint tokens
      (COMPONENT_PART_NAME[partRaw] ?? composePart(partRaw))
    : "";
  if (!kind) return part || ref;
  if (!part) return kind;
  // "左前髋" + "关节电机" reads better as "左前髋关节电机"
  return `${part}${kind}`;
}

/** "rear_right_knee" → "右后膝" even though it is not in COMPONENT_PART_NAME. */
function composePart(part: string): string {
  const exact = POSITION[part];
  if (exact) return exact;
  const toks = splitTokens(part);
  // position tokens first, then joint tokens
  const pos = toks.filter((t) => POSITION[t]).map((t) => POSITION[t]!).join("");
  const joint = toks.filter((t) => JOINT[t]).map((t) => JOINT[t]!).join("");
  if (pos || joint) return `${pos}${joint}`;
  return compose(part);
}

/** "F1_stall" → "堵转". Fault keys are a CLOSED set — unknown falls through to compose. */
export function faultName(key: string): string {
  return FAULT_NAME[key] ?? compose(key);
}

/** "motor_over_temperature" → "电机过温" */
export function eventTypeName(t: string): string {
  return EVENT_TYPE_NAME[t] ?? compose(t);
}

/**
 * A candidate's signal family → a readable label.
 *
 * `signal` is `_signal_of(rule_id, field)`: four semantic buckets
 * (`temperature_high`, `current_anomaly`, …) plus a fallback that returns the
 * **bare field name** (`tau_est`, `q`, `lost`, `soc`). The glossary covers all
 * 15 observed values; composition handles anything new.
 */
export function signalName(s: string): string {
  return SIGNAL_NAME[s] ?? compose(s);
}

/** "MOTOR_TEMP_PROTECT" → "电机过温保护" */
export function ruleName(r: string): string {
  return RULE_NAME[r] ?? compose(r.toLowerCase());
}

/** "op.temperature_protect:1" → "温度保护算子 v1" */
export function operatorName(p: string): string {
  if (!p) return "—";
  const [head = "", version] = p.split(":");
  const [prefix = "", name] = head.split(".");
  const label = name ? ruleName(name) : ruleName(prefix || head);
  const suffix = name && prefix !== "op" ? `${compose(prefix)}·` : "";
  return version ? `${suffix}${label}算子 v${version}` : `${suffix}${label}算子`;
}

/** "needs_human_review" → already covered by DISPOSITION_LABEL; fallback here. */
export function genericName(s: string): string {
  return compose(s);
}

/** Physical-unit suffixes, so "temperature_c" reads "温度 °C" not "温度 c". */
const UNIT_SUFFIX: Record<string, string> = {
  c: "°C",
  v: "V",
  a: "A",
  w: "W",
  kpa: "kPa",
  rpm: "rpm",
  mm: "mm",
  hz: "Hz",
  ms: "ms",
  pct: "%",
};

/**
 * A reference-window metric key → a readable label.
 * "temperature_c" → "温度 °C", "bus_voltage_v" → "母线电压 V",
 * "sample_count" → "采样数". Unknown keys compose token-by-token.
 */
export function metricName(k: string): string {
  const toks = splitTokens(k);
  const last = toks[toks.length - 1];
  const unit = last ? UNIT_SUFFIX[last] : undefined;
  const body = unit ? toks.slice(0, -1) : toks;
  const label = joinParts(body);
  if (unit) return `${label || k} ${unit}`;
  return label || k;
}

/** Device serial: keep verbatim (it is an opaque hardware id). */
export function deviceName(sn: string): string {
  return sn;
}
