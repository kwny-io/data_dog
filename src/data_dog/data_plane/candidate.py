"""P2 候选异常构建（对齐架构 §9.3 第 6 步 + §12.3）。

把 Detector 的命中合并、去重成**少量** CandidateEvent：
  - 同一 (device, component, signal) 在**合并窗口**（merge_window_s 秒）内的多次命中
    → 合成**一条**候选（不然刷屏；重复信号合并是 §9.3 第 6 步明确要求）。
  - 超出合并窗口的命中 → 开启**新的一条**候选（视为另起一个"短时事件"）。
  - 附上 evidence（指标 from/to/baseline）+ evidence_refs（受控引用）。
  - anomaly_score：由**命中严重度**映射（确定性，不用模型）。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..schemas.events import CandidateEvent, EvidenceSummary
from .watermark import Watermark

_SEV_RANK = {"info": 0, "warning": 1, "critical": 2}
_SEV_SCORE = {"info": 0.3, "warning": 0.7, "critical": 1.0}


def _parse_time(value: Any) -> datetime | None:
    """尽量把 occurred_at 解析成带时区的 datetime；解析不了返回 None。

    支持 "2026-09-13T10:00:00Z" / "...+08:00" / 纯 epoch 秒。解析失败时
    调用方按"同一窗口"处理，保持向后兼容（不因数据质量差而丢合并能力）。
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):          # epoch 秒
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    s = str(value).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass
class _Group:
    device_sn: str
    component_ref: str
    signal: str
    window_start: str
    window_end: str
    rule_ids: list[str] = field(default_factory=list)
    severity: str = "info"
    sample_count: int = 0
    evidence: list[EvidenceSummary] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    operator_id: str = ""
    # 该组最近一次命中/创建的时间（用于判断是否还在合并窗口内）
    last_dt: datetime | None = None


class CandidateBuilder:
    """累积命中 → 按合并窗口归并成候选。

    **事件时间与迟到（G2）**：合并判定从"无界 `abs`"收紧为**有向 + 水位线**：

    - 正常：`0 <= delta <= merge_window_s`（顺序命中）→ 并入。
    - 迟到：`-allowed_lateness <= delta < 0` 且事件不早于水位线 → 并入，计 `late_merged`
      （这就是原 `abs` 想表达的"迟到容忍"，但**有界**）。
    - 太晚：事件早于水位线 → **不与旧组混**，新开组，计 `late_rejected`
      （无界 `abs` 会把 1 小时前的事件也并进来，这是要修的）。

    **零回归**：未注入 `watermark` 时（默认）退回原 `abs(delta) <= merge_window_s`
    语义——既有调用点行为逐字节不变；要启用有界迟到语义，构造时传 `Watermark`。
    """

    def __init__(self, *, merge_window_s: float = 5.0,
                 watermark: Watermark | None = None):
        self.merge_window_s = merge_window_s
        self.watermark = watermark
        # 所有已形成的组（含已关闭的）；key 唯一，支持同一信号多段事件
        self._groups: dict[str, _Group] = {}
        # (device, component, signal) → 当前"打开中"的组 key
        self._open: dict[str, str] = {}
        self._seq = 0
        # 迟到统计（供指标/测试）
        self.late_merged = 0        # 容忍窗内并入的迟到命中数
        self.late_rejected = 0      # 太晚（早于水位线）被拒、另开新组的命中数

    @staticmethod
    def _base_key(device_sn: str, component_ref: str, signal: str,
                  tenant_id: str = "") -> str:
        """分组键。**含租户**：否则不同租户的同名设备序列号会撞同一 key，
        被合并成同一条候选（candidate_id 也相同）→ 跨租户数据泄漏/覆盖。
        租户为空（兼容旧调用）时退化为不含租户，但服务路径总是带租户。"""
        prefix = f"{tenant_id}|" if tenant_id else ""
        return f"{prefix}{device_sn}|{component_ref}|{signal}"

    def _find_group(self, device_sn: str, component_ref: str, signal: str,
                    occurred_at: str, tenant_id: str = "") -> _Group:
        """找到应归入的组：同信号、且仍在合并窗口内；否则开新组。"""
        base = self._base_key(device_sn, component_ref, signal, tenant_id)
        now_dt = _parse_time(occurred_at)
        if self.watermark is not None and now_dt is not None:
            self.watermark.observe(now_dt.timestamp())
        open_key = self._open.get(base)
        if open_key is not None:
            g = self._groups[open_key]
            if now_dt is None or g.last_dt is None:
                in_window = True                     # 解析不了 → 保守合并
            elif self.watermark is None:
                # 旧语义（零回归）：无界 abs。
                in_window = abs((now_dt - g.last_dt).total_seconds()) <= self.merge_window_s
            else:
                in_window = self._in_window_with_watermark(g, now_dt)
            if in_window:
                return g
        # 开启新组
        self._seq += 1
        key = f"{base}#{self._seq}"
        g = _Group(device_sn=device_sn, component_ref=component_ref,
                   signal=signal, window_start=occurred_at, window_end=occurred_at,
                   last_dt=now_dt)
        self._groups[key] = g
        self._open[base] = key
        return g

    def _in_window_with_watermark(self, g: _Group, now_dt: datetime) -> bool:
        """有界迟到语义下的合并判定（并维护迟到计数）。"""
        assert self.watermark is not None and g.last_dt is not None
        delta = (now_dt - g.last_dt).total_seconds()
        # 正常（顺序）：仍在合并窗口内。
        if 0.0 <= delta <= self.merge_window_s:
            return True
        # 迟到：在容忍窗内且未早于水位线 → 并入（这就是有界的"迟到容忍"）。
        if -self.watermark.allowed_lateness_s <= delta < 0.0:
            if not self.watermark.is_late(now_dt.timestamp()):
                self.late_merged += 1
                return True
            self.late_rejected += 1
            return False
        # 太晚（早于水位线）或大幅前跳 → 不混入旧组。
        if delta < 0.0:
            self.late_rejected += 1
        return False

    def add(self, *, device_sn: str, component_ref: str, metrics: dict[str, Any],
            hits: list[dict[str, Any]], occurred_at: str, received_at: str,
            raw_ref: str = "", baseline: dict[str, float] | None = None,
            tenant_id: str = "") -> None:
        """加入一个事件的所有命中（同一事件多规则命中 → 归到各自的 signal）。"""
        for h in hits:
            rule_id = h["rule_id"]
            signal = _signal_of(rule_id, h.get("field", ""))
            g = self._find_group(device_sn, component_ref, signal, occurred_at,
                                 tenant_id)
            g.sample_count += 1
            # last_dt/window_end 取**最大**事件时间：乱序迟到事件并入后不能把组的时间
            # 基准拉回去，否则后续正常事件会被误判为"大幅前跳"。顺序到达时二者自然
            # 等于最新事件（与旧行为一致）。
            new_dt = _parse_time(occurred_at)
            end_dt = _parse_time(g.window_end)
            if new_dt is None:
                g.window_end = occurred_at
            elif end_dt is None or new_dt >= end_dt:
                g.window_end = occurred_at
                if g.last_dt is None or new_dt > g.last_dt:
                    g.last_dt = new_dt
            if rule_id not in g.rule_ids:
                g.rule_ids.append(rule_id)
            # 严重度取最高
            sev = h.get("severity_hint", "info")
            if _SEV_RANK.get(sev, 0) > _SEV_RANK.get(g.severity, 0):
                g.severity = sev
            g.operator_id = h.get("operator_id", g.operator_id)
            # 证据（该字段的观测值 / 阈值 / 基线）
            fld = h.get("field", "")
            base = (baseline or {}).get(fld)
            g.evidence = [e for e in g.evidence if e.key != fld]   # 同字段只留最新
            g.evidence.append(EvidenceSummary(
                key=fld, from_value=base, to_value=h.get("value"),
                baseline=base, unit=None,
                note=f"{rule_id}: {h.get('operator')} {h.get('threshold')}",
            ))
            if raw_ref and raw_ref not in g.evidence_refs:
                g.evidence_refs.append(raw_ref)

    def build(self) -> list[CandidateEvent]:
        """产出候选列表（确定性排序：设备→部件→信号→首见时间）。"""
        out: list[CandidateEvent] = []
        for key in sorted(self._groups):
            g = self._groups[key]
            cid = "cand_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
            out.append(CandidateEvent(
                candidate_id=cid,
                device_sn=g.device_sn,
                component_ref=g.component_ref,
                signal=g.signal,
                severity_hint=g.severity,
                window_start=g.window_start,
                window_end=g.window_end,
                sample_count=g.sample_count,
                rule_matches=list(g.rule_ids),
                anomaly_score=_SEV_SCORE.get(g.severity, 0.3),
                evidence=list(g.evidence),
                evidence_refs=list(g.evidence_refs) + [f"metricwin://{cid}"],
                detector_provenance=g.operator_id,
            ))
        return out


def _signal_of(rule_id: str, field_name: str) -> str:
    """规则的信号族：优先用 rule_id 语义，退化用字段名。"""
    r = rule_id.lower()
    if "overheat" in r or "protect" in r or (field_name == "temperature" and "temp" in r):
        return "temperature_high"
    if "temp" in r:
        return "temperature_rising"
    if "overcurrent" in r or "current" in r:
        return "current_anomaly"
    if "fault" in r or "mode" in r or "error" in r:
        return "fault_flag"
    return field_name or "unknown"
