"""CWRU 轴承数据集证据源：把**外部公开数据集**接进 data_dog 的 Agent 链路。

为什么要有它
-----------
项目此前的评估全部在**自己造的合成 / MuJoCo 数据**上。工业界的第一个问题是
"你只对自己造的数据有效吧？"。本模块把 **CWRU 轴承故障数据集**（Case Western Reserve
University，公开、带明确故障标签、第三方镜像可下载）接成 `EvidenceSource`，用于
**跨数据集泛化**验证——数据源一换，Agent / 工具链**一行不改**（这正是本仓
"换数据源只需替换合成层"设计的兑现）。

实现要点
--------
- **零新依赖**：MAT v5 是未压缩格式，这里**手写最小解析器**（本机装不上 scipy）。
- 信号 → **诊断特征**：RMS / 峰值 / 峭度 / 波峰因子（经典轴承诊断量）。Agent 看到的是
  标量而不是 12 万点原始波形：省 token，且与遥测证据的形态一致。
- 组件固定为 `bearing.main`——它**不属于**机器人电机拓扑，所以本源**自带**授权校验，
  不依赖 `evidence.store.ALLOWED_COMPONENTS`（Agent 侧的 `RuntimeContext` 会把它列入
  允许范围）。

诚实边界
--------
- 标签口径**粗粒度**：normal → rejected；ball / inner / outer → needs_human_review
  （"识别出异常"）。不区分 BPFO/BPFI/BSF 缺陷频率，也不做严重度排序。
- 窗口切分是**人为**的，同一条信号的多个窗口**高度相关、不是独立样本**：n 是"窗口数"
  而非"独立试验数"，CI 要按此打折。
- 参照窗取窗口**前 35%**（与 synth 的 `REF_WINDOW_END_FRAC` 同口径），是窗口内基线，
  **不是**真实"无故障基线"。
- 数据来自第三方镜像仓库（`data/cwru`，gitignore）；原始出处 case.edu。
"""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

from ..errors import ToolErrorCode, err

BEARING_COMPONENT = "bearing.main"

# MAT v5 数据类型码 → numpy dtype（只覆盖本模块用得到的数值型）
_MI: dict[int, str] = {0: "<u1", 1: "<i1", 2: "<u2", 3: "<i2", 4: "<u4", 5: "<i4",
                       6: "<f4", 7: "<f8", 9: "<f8", 12: "<i8", 13: "<u8"}
_REF_END_FRAC = 0.35


def _elem(b: bytes, pos: int) -> tuple[int, bytes, int]:
    """读一个 MAT v5 数据元素，返回 (类型码, 数据字节, 下一个位置)。"""
    t, s = struct.unpack_from("<II", b, pos)
    pos += 8
    if (t >> 16) != 0:                     # 小元素：低 16=type、高 16=size
        typ, size = t & 0xFFFF, t >> 16
        return typ, b[pos:pos + size], pos + 4
    return t, b[pos:pos + s], pos + s + ((8 - s % 8) % 8)


def _parse_matrix(data: bytes) -> tuple[str, np.ndarray]:
    """解析一个 miMATRIX 负载 → (变量名, 一维 float 数组)。"""
    ip = 0
    _t, d_flags, ip = _elem(data, ip)                  # array flags
    cls = struct.unpack_from("<H", d_flags, 0)[0]      # mxClassID
    _t, d_dims, ip = _elem(data, ip)                   # dimensions
    _t, d_name, ip = _elem(data, ip)                   # name
    name = d_name.decode("ascii", "replace")
    t_data, d_data, ip = _elem(data, ip)               # 数值数据
    dt = _MI.get(t_data, _MI.get(cls, "<f8"))
    return name, np.frombuffer(d_data, dtype=dt).astype(float)


def read_mat_variables(path: str | Path) -> dict[str, np.ndarray]:
    """读 MAT v5 文件里的**所有**变量（CWRU 一个文件含 DE/FE/BA/RPM 多条）。"""
    b = Path(path).read_bytes()
    out: dict[str, np.ndarray] = {}
    pos = 128                                          # 跳过 128 字节头
    while pos < len(b):
        try:
            typ, data, pos = _elem(b, pos)
        except struct.error:
            break
        if typ != 14:
            continue
        try:
            name, arr = _parse_matrix(data)
        except (struct.error, ValueError):
            continue
        if name:
            out[name] = arr
    return out


def read_mat_vector(path: str | Path) -> tuple[str, np.ndarray]:
    """读 MAT v5 文件里第一个 mxArray（数值型），返回 (变量名, 一维 float 数组)。"""
    vars_ = read_mat_variables(path)
    if not vars_:
        raise ValueError(f"{path}: 没有解析出任何变量")
    name = next(iter(vars_))
    return name, vars_[name]


def features(x: np.ndarray) -> dict[str, float]:
    """经典轴承诊断特征（标量，供 Agent 判断）。"""
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if x.size < 8:
        return {}
    rms = float(np.sqrt(np.mean(x ** 2)))
    peak = float(np.max(np.abs(x)))
    sd = float(np.std(x))
    kurt = float(np.mean(((x - x.mean()) / sd) ** 4)) if sd > 0 else 0.0
    crest = peak / rms if rms > 0 else 0.0
    return {"rms": round(rms, 4), "peak": round(peak, 4),
            "kurtosis": round(kurt, 3), "crest_factor": round(crest, 3)}


class CWRUEvidenceSource:
    """把**一条** CWRU 信号（一个窗口）接成证据源。"""

    def __init__(self, x: np.ndarray, *, device_sn: str = "device-cwru",
                 component: str = BEARING_COMPONENT):
        self.device_sn = device_sn
        self.component = component
        self._x = np.asarray(x, dtype=float)

    def _denied(self, device_sn: str, component: str):
        if component != self.component:
            return err(ToolErrorCode.DENIED, component)
        if device_sn != self.device_sn:
            return err(ToolErrorCode.DENIED, "device mismatch")
        return None

    def query_telemetry(self, device_sn, component, window_start, window_end):
        bad = self._denied(device_sn, component)
        if bad is not None:
            return bad
        x = self._x
        ref_hi = max(8, int(len(x) * _REF_END_FRAC))
        main, ref = features(x), features(x[:ref_hi])
        if not main:
            return None
        return {"device_sn": self.device_sn, "component": component,
                "window": f"{window_start or 'w0'}-{window_end or 'w0'}",
                "sample_count": int(len(x)), "metrics": main,
                "reference_window_metrics": ref,
                "peak_temperature_c": main.get("peak"),
                "packet_loss_rate": 0.0}

    def query_vendor_knowledge(self, component_type, question=None):
        return [{"id": "BEARING_VIB", "component_type": "bearing",
                 "source": "CWRU 公开数据集（case.edu）",
                 "thresholds": {"kurtosis_alert": 4.0, "crest_factor_alert": 5.0,
                                "rms_alert": 0.6},
                 "note": "滚动轴承早期缺陷常先表现为**峭度 / 波峰因子抬升**，RMS 后知后觉。"}]

    def query_neighbor_events(self, device_sn, component, window_start, window_end):
        return []

    def query_recent_changes(self, device_sn, component, since):
        return {"changes": []}
