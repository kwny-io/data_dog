"""P2 在线处理链（对齐架构 §9.3）。

    Parser → Normalizer → Enricher → Window/Feature → Detector → (候选)

- Parser：把厂商私有格式/字段按声明式映射解成标准字段（**执行 P1 的 FieldSpec 映射**）。
- Normalizer：单位/枚举/错误码规范化。
- Enricher：补设备元数据、组件拓扑、运行工况（**没工况就没基线**）。
- Window：滑动窗口特征（均值/斜率/分位数/极值）——热退化是趋势不是瞬时值。
- Detector：执行 P1 生成的算子（声明式 ThresholdRule + 受限 DSL），产出命中。

全程**确定性、不调大模型**（§4.1）；字段缺失走 missing 语义，不中断数据流（§10.3）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..operator_factory.dsl import packet_coverage_of
from ..schemas.operator_config import DetectorConfig, FieldSpec

# ---------------------------------------------------------------- Parser

class Parser:
    """按算子声明的 FieldSpec 把原始键映射为标准字段名 + 类型转换。"""

    def __init__(self, fields: list[FieldSpec], *, strict: bool = False):
        self.fields = fields
        self.strict = strict

    def parse(self, raw: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for spec in self.fields:
            if spec.source not in raw:
                if not spec.optional:
                    out.setdefault("__missing__", []).append(spec.source)
                continue
            out[spec.name] = _coerce(raw[spec.source], spec.type)
        return out


def _coerce(value: Any, typ: str) -> Any:
    try:
        if typ in ("uint8", "uint16", "uint32", "int8", "int16", "int32"):
            return int(value)
        if typ in ("float32", "float64", "float"):
            return float(value)
        if typ in ("bool", "boolean"):
            return bool(value)
    except (TypeError, ValueError):
        return value          # 转换失败保留原值，交由下游 missing 语义处理
    return value


# ---------------------------------------------------------------- Normalizer

# 单位换算表（文档声明的量纲 → 标准量纲）
_UNIT_CONV = {
    ("celsius", "celsius"): lambda x: x,
    ("raw_temp", "celsius"): lambda x: x,               # 由算子声明具体换算
}


class Normalizer:
    """单位/量纲规范化。声明式 ->标准化声明（本层只做**有来源**的换算，不猜）。"""

    def __init__(self, unit_rules: dict[str, str] | None = None):
        # {field: standard_unit}
        self.unit_rules = unit_rules or {}

    def normalize(self, metrics: dict[str, Any]) -> dict[str, Any]:
        return dict(metrics)      # 无声明换算时原样返回（不擅自换算，避免"猜单位"）


class StatusCodeNormalizer:
    """把厂商错误码/状态位映射为标准错误码（来源：P1 抽取的 MError 枚举）。"""

    def __init__(self, mapping: dict[int, str] | None = None):
        self.mapping = mapping or {
            0: "OK", 1: "OVERHEAT", 2: "OVERCURRENT", 3: "OVERVOLTAGE",
            4: "ENCODER_FAULT",
        }

    def codes_for(self, merror: Any) -> list[str]:
        try:
            m = int(merror)
        except (TypeError, ValueError):
            return []
        code = self.mapping.get(m)
        return [code] if code and code != "OK" else []


# ---------------------------------------------------------------- Enricher

# 在线基线的三个常数（都是**项目假设**，非厂商规格）：
#   WARMUP：前 N 个样本用简单均值建基线，之后才对外开放（少于 N 个 → 基线不可用）；
#   TAU   ：此后按时间常数缓慢跟踪（秒）。故障窗是分钟级，TAU 取 600s 使一次
#           60s 的故障最多把基线拖动 ~9.5%，既跟得上季节/装配漂移，又不会把故障
#           本身学成"正常"；
#   MAX_A：单样本对基线的最大改动比例（无事件时间时按此固定步长），作为**投毒护栏**：
#           任何一个畸形样本最多让基线移动 10%，不会被一条 1e9 直接带跑。
_BASELINE_WARMUP = 5
_BASELINE_TAU_S = 600.0
_BASELINE_MAX_ALPHA = 0.1


@dataclass
class Enricher:
    """补运行工况与同工况基线。基线按 (component, field, condition) 维护。

    基线来源（在线，无需离线训练）：**同设备 + 同部件 + 同工况的短时滚动基线**。
    逐样本以时间常数 TAU 缓慢跟随，前 WARMUP 个样本用均值冷启动。

    为什么不是"同型号历史窗口平均"：在线热路径拿不到其它设备的样本，跨设备平均
    会把个体差异（装配、负载史）抹平，反而更容易误判。同部件自身的近期基线是
    唯一在本进程内可验证、可回溯的来源。

    降级语义（**规则依赖它**）：
      - 样本数 < WARMUP → `baseline_for` 返回 None → 依赖基线的规则**不命中**；
      - 工况缺失 → 工况记为 "unknown"，只在 unknown 工况之间互比，不跨工况借用；
      - 非数值/NaN/Inf → 丢弃且不计入样本数（畸形输入绝不能变成基线）。
    """
    baselines: dict[str, float] = field(default_factory=dict)   # "comp|field|condition" -> 基线
    warmup_samples: int = _BASELINE_WARMUP
    tau_s: float = _BASELINE_TAU_S
    max_alpha: float = _BASELINE_MAX_ALPHA
    # key -> [value, n, last_at_s]；与 baselines 分开，因为未满 warmup 前不对外开放。
    _state: dict[str, list[Any]] = field(default_factory=dict)
    rejected_samples: dict[str, int] = field(default_factory=dict)

    def add_baseline(self, component: str, field_name: str, condition: str, value: float) -> None:
        """显式登记基线（例如从离线标定/运维配置注入）。会直接开放为可用。"""
        self.baselines[f"{component}|{field_name}|{condition}"] = float(value)

    def baseline_for(self, component: str, field_name: str, condition: str) -> float | None:
        return self.baselines.get(f"{component}|{field_name}|{condition}")

    def condition_from(self, raw: dict[str, Any]) -> str:
        """运行工况。缺失 → "unknown"：**只在 unknown 之间互比**，不跨工况借用基线。"""
        return str(raw.get("operating_condition") or "unknown")

    def observe(self, component: str, field_name: str, condition: str,
                value: Any, at_s: float | None = None) -> float | None:
        """把一个观测值并入滚动基线，返回当前基线（未满 warmup 时返回 None）。

        **两个基线同时维护**：`x` 的有符号均值，与 `|x|` 的均值（键加 `|abs` 后缀）。

        为什么要两个：扭矩是**有符号**的。步态摆动时 τ 正负对称振荡，有符号均值
        会被正负抵消掉（go2_sim 奔跑实测基线 4.88，而 mean|τ| 是 9.14），于是
        「mean|τ| ≥ 1.5 × 基线」在**正常奔跑**时恒成立、比值 1.9 —— 用有符号基线
        去比绝对值型统计量，差的就是这个恒定的抵消倍数，与负载无关。
        规则用 `mean_abs(x)` 就必须配 `baseline_abs(x)`，两边同一口径。

        有符号基线保留不动：既有规则 `MOTOR_TEMP_RISE_UNDER_LOAD` 用的是
        `mean(current) <= 1.2 * baseline(current)`，电流无符号，两者本来就一致。
        """
        x = _as_finite(value)
        if x is None:
            self.rejected_samples["non_numeric"] = \
                self.rejected_samples.get("non_numeric", 0) + 1
            return None
        self._observe_one(component, f"{field_name}|abs", condition, abs(x), at_s)
        return self._observe_one(component, field_name, condition, x, at_s)

    def _observe_one(self, component: str, field_name: str, condition: str,
                     x: float, at_s: float | None) -> float | None:
        key = f"{component}|{field_name}|{condition}"
        st = self._state.get(key)
        if st is None:
            st = [x, 1, at_s]
            self._state[key] = st
        else:
            cur, n, last_t = st
            if n < self.warmup_samples:
                cur = (cur * n + x) / (n + 1)      # 冷启动：均值
                n += 1
            else:
                alpha = self.max_alpha
                if at_s is not None and last_t is not None and at_s > last_t:
                    import math
                    alpha = min(self.max_alpha,
                                1.0 - math.exp(-(at_s - last_t) / max(self.tau_s, 1e-9)))
                cur = cur + (x - cur) * alpha
            st[0], st[1] = cur, n
            if at_s is not None:
                st[2] = at_s
        if st[1] >= self.warmup_samples:
            self.baselines[key] = float(st[0])
            return float(st[0])
        return None

    def observe_metrics(self, component: str, metrics: dict[str, Any],
                        condition: str, at_s: float | None = None) -> None:
        for name, val in metrics.items():
            if name.startswith("_"):
                continue                      # 内部字段（如 _merror）不进基线
            self.observe(component, name, condition, val, at_s=at_s)

    def snapshot(self, component: str, conditions_for_field: dict[str, str]
                 ) -> dict[str, float]:
        """{field: condition} → 当前可用基线快照（拿不到的不出现在结果里）。

        **同时带上 `<field>|abs`**：`observe` 为每个字段维护了两个口径，但这里
        只按 metrics 里出现的字段名查——不显式带出 `|abs`，`baseline_abs()` 就
        永远拿到 None，用它的规则会**静默不命中**（不报错，只是不出结果）。
        """
        out: dict[str, float] = {}
        for fld, cond in conditions_for_field.items():
            for key in (fld, f"{fld}|abs"):
                b = self.baseline_for(component, key, cond)
                if b is not None:
                    out[key] = b
        return out


def _as_finite(value: Any) -> float | None:
    """严格取有限数值：bool / 字符串 / NaN / ±Inf 一律视为"不是数"。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    x = float(value)
    if x != x or x in (float("inf"), float("-inf")):
        return None
    return x


# ---------------------------------------------------------------- Window

@dataclass
class WindowFeature:
    """一个事件的窗口特征（只依赖事件时间序列，确定性）。"""
    mean: dict[str, float] = field(default_factory=dict)
    slope: dict[str, float] = field(default_factory=dict)
    last: dict[str, float] = field(default_factory=dict)
    max: dict[str, float] = field(default_factory=dict)
    count: int = 0


class WindowBuffer:
    """按 (device, component) 维护事件时间序列并算窗口特征。

    **事件时间有序（G2）**：样本按 `(occurred_at_s, seq)` 有序插入，乱序到达不再
    污染 `mean/slope/max/last`。未提供 `occurred_at_s` 时退化为到达顺序（用内部
    自增 `seq` 当时间键）——与旧行为一致（零回归，旧调用点无需改）。

    诚实边界：同一 buffer **不要**混用"给时间"与"不给时间"的调用——不给时间的样本
    以 `seq` 为时间键（小整数），会排到真实 epoch 时间之前。一个 buffer 一种用法。
    """

    def __init__(self, size: int = 30):
        self.size = size
        # key → 有序样本列表 [(t, seq, metrics_dict), ...]
        self._samples: dict[str, list[tuple[float, int, dict[str, float]]]] = {}
        # key → 与 `_samples` **逐条平行**的**设备序列号**（信封的 `sequence`）。
        # 独立于样本存，是因为 `_samples` 的元组第 2 位是窗口**内部**自增序号
        # （只用于同事件时间下的稳定排序），与设备序列号不是一回事。
        self._seqnums: dict[str, list[int | None]] = {}
        self._seq = 0

    def push(self, key: str, metrics: dict[str, Any],
             occurred_at_s: float | None = None,
             sequence: int | None = None) -> None:
        """压入一条样本。

        `sequence`：信封上的**设备序列号**（`RawLogEnvelope.sequence`）。给了才能
        统计**包级**丢包（见 `packet_coverage`）；不给则包级判据一律"不知道"。
        """
        self._seq += 1
        seq = self._seq
        t = float(occurred_at_s) if occurred_at_s is not None else float(seq)
        # 只收**有限**数值：NaN/±Inf 一律按"该样本没有这个字段"处理。
        # 否则一个 NaN 会让整个窗口的 mean/max 变成 NaN，后续比较全部为假——
        # 于是窗口在故障期间静默失效（既不复位也不报警），比漏报更糟。
        clean: dict[str, float] = {}
        for k, v in metrics.items():
            x = _as_finite(v)
            if x is not None:
                clean[k] = x
        buf = self._samples.setdefault(key, [])
        entry = (t, seq, clean)
        # 有序插入：找到第一个 (t, seq) 更大的位置（乱序事件插入到正确位置）。
        lo, hi = 0, len(buf)
        while lo < hi:
            mid = (lo + hi) // 2
            if (buf[mid][0], buf[mid][1]) <= (t, seq):
                lo = mid + 1
            else:
                hi = mid
        buf.insert(lo, entry)
        # 设备序列号必须插在**同一个位置**——它与样本按下标一一对应，
        # 错位一位就会把"相邻两个序列号"算成别人的，丢包率随之全错。
        seqbuf = self._seqnums.setdefault(key, [])
        seqbuf.insert(lo, int(sequence) if sequence is not None else None)
        if len(buf) > self.size:
            buf.pop(0)                      # 淘汰**事件时间最早**的样本
            seqbuf.pop(0)

    def features(self, key: str) -> WindowFeature:
        """窗口特征（mean/slope/last/max）。见 `series()` 关于缺样本的说明。"""
        buf = self._samples.get(key, [])
        series: dict[str, list[float]] = {}
        for _t, _seq, metrics in buf:
            for k, v in metrics.items():
                series.setdefault(k, []).append(v)
        f = WindowFeature()
        for k, vals in series.items():
            if not vals:
                continue
            f.mean[k] = sum(vals) / len(vals)
            f.last[k] = vals[-1]
            f.max[k] = max(vals)
            f.slope[k] = _slope(vals)
        f.count = max((len(v) for v in series.values()), default=0)
        return f

    def series(self, key: str) -> dict[str, list[float]]:
        """窗口内逐字段的**有序**取值序列（供 DSL 窗口函数与丢包率计算）。

        缺样本语义（关键）：`push()` 只收数值，非数值/缺失字段**不会**留下占位。
        所以某字段长度 < 该窗口的样本总数，就意味着那几条样本该字段缺失
        （丢包/解析不出）。DSL 的 window.mean 等仍是"有效点上的聚合"——
        与本方法一致，不会把缺失当 0（那会把丢包伪装成"电流为零"）。
        """
        buf = self._samples.get(key, [])
        out: dict[str, list[float]] = {}
        for _t, _seq, metrics in buf:
            for k, v in metrics.items():
                out.setdefault(k, []).append(v)
        return out

    def sample_count(self, key: str) -> int:
        """该窗口收到的样本条数（与字段无关），用于算"某字段的有效率"。"""
        return len(self._samples.get(key, []))

    def packet_coverage(self, key: str) -> dict[str, Any]:
        """窗口内的**包级**到达率 —— 用设备序列号的连续性算，不靠字段缺失率。

        见 `packet_coverage_of`（真正的算法在那里，离线 fixture 复用同一份）。
        """
        return packet_coverage_of(self._seqnums.get(key) or [])

    def times(self, key: str) -> dict[str, list[float]]:
        """field → 与 `series()[field]` 逐一对应的事件时间（秒）。

        有了它，`window.slope` 才是"字段单位/秒"，不随采样率变形。
        """
        out: dict[str, list[float]] = {}
        for t, _seq, metrics in self._samples.get(key, []):
            for k in metrics:
                out.setdefault(k, []).append(t)
        return out


def window_quality(series: dict[str, list[float]], samples: int,
                   field: str) -> dict[str, Any]:
    """窗口数据质量：该字段的有效样本数 / 总样本数 / 丢失率。

    `samples` 为窗口收到的样本条数（`WindowBuffer.sample_count`）。无样本时
    `known=False`，调用方必须据此**跳过**基于缺失率的判据——"没有数据"不等于
    "数据全丢"，把二者混同会让启动首个事件就误报丢包。
    """
    if samples <= 0:
        return {"known": False, "valid": 0, "samples": 0, "missing_rate": 0.0}
    valid = len(series.get(field, ()))
    return {
        "known": True,
        "valid": valid,
        "samples": samples,
        "missing_rate": max(0.0, 1.0 - valid / samples),
    }


def _slope(vals: list[float]) -> float:
    n = len(vals)
    if n < 2:
        return 0.0
    mx = (n - 1) / 2
    my = sum(vals) / n
    num = sum((i - mx) * (v - my) for i, v in enumerate(vals))
    den = sum((i - mx) ** 2 for i in range(n))
    return num / den if den else 0.0


# ---------------------------------------------------------------- Detector

class Detector:
    """执行声明式算子：单条阈值（ThresholdRule）+ 窗口/基线 DSL 规则（DslRuleSpec）。

    语义分层（这是"纯增量、无回归"的关键）：
      - 单条阈值规则只看**本条** metrics，与改造前逐字节等价；
      - DSL 规则需要窗口序列（`series`）与同工况基线（`baseline`）。任一缺失时该规则
        **不命中**，而不是猜——见 `_detect_dsl` 的降级说明。
    """

    def __init__(self, config: DetectorConfig):
        self.config = config
        self._compiled: list[Any] | None = None
        self._compile_error: str | None = None

    # -- DSL 规则编译（惰性、只做一次；编译失败不拖垮单条阈值路径）--
    def _dsl_compiled(self) -> list[Any]:
        if self._compiled is not None or self._compile_error is not None:
            return self._compiled or []
        specs = list(getattr(self.config, "dsl_rules", ()) or ())
        if not specs:
            self._compiled = []
            return self._compiled
        try:
            from ..operator_factory.dsl import compile_rule
            declared = [f.name for f in self.config.fields]
            self._compiled = [
                compile_rule(
                    {"rule_id": s.rule_id, "when": s.when,
                     "severity_hint": s.severity_hint, "message": s.message},
                    declared,
                )
                for s in specs
            ]
        except Exception as e:      # 编译失败：记下来、DSL 一律不命中
            self._compile_error = f"{type(e).__name__}: {e}"
            self._compiled = []
        return self._compiled

    @property
    def dsl_compile_error(self) -> str | None:
        return self._compile_error

    def detect(self, metrics: dict[str, Any], *,
               series: dict[str, list[float]] | None = None,
               baseline: dict[str, float] | None = None,
               samples: int = 0,
               times: dict[str, list[float]] | None = None,
               packets: dict[str, Any] | None = None,
               ) -> list[dict[str, Any]]:
        """对一条事件跑全部规则。

        `series`/`baseline`/`samples`/`times` 是可选窗口上下文：不传时**只**跑单条
        阈值规则（与改造前完全一致），故旧调用点零回归。

        `packets`：窗口的**包级**到达情况（`WindowBuffer.packet_coverage`）。
        与 `samples` 分开传，因为二者是不同的量：`samples` 是"网关收了几条"，
        `packets` 是"设备发了几条、到了几条"。整包丢失时前者完全看不出来。
        """
        hits: list[dict[str, Any]] = []
        for rule in self.config.rules:
            if rule.field not in metrics:
                continue
            val = metrics[rule.field]
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                continue                    # 畸形输入不命中（§10.3）
            if _cmp(rule.operator, val, rule.value):
                hits.append({
                    "rule_id": rule.rule_id,
                    "field": rule.field,
                    "value": val,
                    "operator": rule.operator,
                    "threshold": rule.value,
                    "severity_hint": rule.severity_hint,
                    "message": rule.message,
                    "provenance": getattr(rule, "provenance", ""),
                    "subsystem": getattr(rule, "subsystem", ""),
                    "operator_id": self.config.operator_id,
                })
        hits.extend(self._detect_dsl(metrics, series, baseline, samples, times, packets))
        return hits

    def _detect_dsl(self, metrics: dict[str, Any],
                    series: dict[str, list[float]] | None,
                    baseline: dict[str, float] | None,
                    samples: int,
                    times: dict[str, list[float]] | None = None,
                    packets: dict[str, Any] | None = None,
                    ) -> list[dict[str, Any]]:
        specs = list(getattr(self.config, "dsl_rules", ()) or ())
        compiled = self._dsl_compiled()
        if not specs or not compiled:
            return []

        from ..operator_factory.dsl import WindowContext, evaluate

        # 降级语义（**关键**）：没有窗口序列就没有趋势/计数可算，DSL 规则一律不命中。
        # 不去"用当前这一条 metrics 冒充窗口"——那会让 window.slope 恒为 0，
        # 从而把"数据不足"静默判成"趋势正常"。基线缺失则交给 DSL 的 missing 语义：
        # window.baseline(x) 求值为 None，任何与它比较的规则在求值期抛 TypeError
        # → evaluate() 视为不命中。即"没基线 = 不命中"，绝不退化成"基线=0"而误报。
        if not series:
            return []
        window = WindowContext(series=series, baseline=dict(baseline or {}),
                               samples=samples, times=dict(times or {}),
                               packets=dict(packets or {}))

        hits: list[dict[str, Any]] = []
        # strict=True：两者由同一次编译产出，长度不等说明编译环节出了问题，
        # 静默截断会少跑规则且没人发现。
        for spec, rule in zip(specs, compiled, strict=True):
            if not evaluate(rule, metrics, window):
                continue
            field = spec.field or _first_field_ref(rule)
            hits.append({
                "rule_id": rule.rule_id,
                "field": field,
                "value": _witness(metrics, series, baseline, field),
                "operator": "dsl",
                "threshold": rule.source,
                "severity_hint": rule.severity_hint,
                "message": rule.message,
                "provenance": getattr(spec, "provenance", ""),
                "subsystem": getattr(spec, "subsystem", ""),
                "operator_id": self.config.operator_id,
            })
        return hits


def _first_field_ref(rule: Any) -> str:
    """取表达式里第一个字段引用，作为证据归因字段（无则空串）。"""
    from ..operator_factory.dsl import Call, FieldRef

    stack = [rule.ast]
    while stack:
        node = stack.pop()
        if isinstance(node, FieldRef):
            return node.name
        if isinstance(node, Call):
            stack.extend(node.args)
        for attr in ("left", "right", "operand"):
            child = getattr(node, attr, None)
            if child is not None:
                stack.append(child)
    return ""


def _witness(metrics: dict[str, Any], series: dict[str, list[float]],
             baseline: dict[str, float] | None, field: str) -> Any:
    """命中时写进证据的观测值：优先本条 metrics，其次窗口末值。

    DSL 规则命中往往说明"窗口累积出的事"，单条 metrics 可能恰好缺该字段；
    退到窗口末值，避免候选里出现 value=None 这种不可核对的证据。
    """
    if field in metrics and isinstance(metrics[field], (int, float)) \
            and not isinstance(metrics[field], bool):
        return metrics[field]
    vals = series.get(field) or []
    return vals[-1] if vals else None


def _cmp(op: str, a: float, b: float) -> bool:
    return {
        "gt": a > b, "lt": a < b, "ge": a >= b, "le": a <= b, "eq": a == b,
    }.get(op, False)
