"""仓储证据桥：把**真实落库的遥测**接成 Agent 的证据源（与 EvidenceStore 同形）。

为什么需要它
------------
在线接入（`POST /v1/telemetry`）会把规范化遥测写进 `telemetry_events`；但 Agent
的调查工具（`InvestigationTools`）此前只认识 `EvidenceStore`（内置假数据）或
`CandidateEvidenceBridge`（E2E 回放内存数据）。于是 **HTTP 调查端点没有真实的
证据来源**——它要么查不到数据、要么得再造一份映射。

本桥从 `telemetry_events` 的行重建证据源，键名映射**复用 `scenarios._STANDARD_NAME`**
（唯一实现）：否则 Agent 看到的证据字段会与 demo/前端静默分叉（一处写
temperature_c、另一处写 temperature，检测逻辑对不上）。

诚实边界
--------
- 邻居事件（`query_neighbor_events`）**已真做**：扫同设备其它部件在同一时间窗内的
  落库遥测，用**真实算子规则**（`GeneratedRuleDetector`，阈值来自
  `operators/generated/*.yaml`，非臆造）判定其是否有异常。命中即作为邻居事件返回。
  仍未做的：跨部件的**因果时序**（如"电压先跌、电流后涨"）与层级拓扑链
  （电池→驱动板→电机）——当前只做"同设备同窗其它部件是否异常"这一层。
- 近期变更（`query_recent_changes`）复用内置变更表：真实的变更管理源尚未接入，
  在文档里如实标明，不冒充"已接生产数据源"。
"""
from __future__ import annotations

from typing import Any

from ..data_plane.scenarios import _STANDARD_NAME
from ..errors import ToolErrorCode, err
from ..evidence.detectors import GeneratedRuleDetector, load_detector
from ..evidence.store import ALLOWED_COMPONENTS, RECENT_CHANGES, VENDOR_KNOWLEDGE


def _detector_for(bridge: RepoEvidenceBridge) -> GeneratedRuleDetector:
    """取算子规则检测器：注入优先，否则用**按目录缓存**的全局实例。

    算子 YAML 是只读产物，每次邻居查询都重读磁盘纯属浪费；但 `DD_OPERATORS_DIR`
    可在测试里改，故以解析后的目录串为键缓存，而不是无条件单例。
    """
    if bridge._detector is not None:
        return bridge._detector
    from ..paths import operators_dir
    key = str(operators_dir() / "generated")
    cached = _DETECTOR_CACHE.get(key)
    if cached is None:
        cached = load_detector()
        _DETECTOR_CACHE[key] = cached
    return cached


_DETECTOR_CACHE: dict[str, GeneratedRuleDetector] = {}


def _standardize(metrics: dict[str, Any] | None) -> dict[str, float]:
    """把 P2 原始字段名转成 Agent 证据侧的标准名（temperature → temperature_c）。"""
    if not metrics:
        return {}
    out: dict[str, float] = {}
    for k, v in metrics.items():
        try:
            out[_STANDARD_NAME.get(k, k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


class RepoEvidenceBridge:
    """按 (device, component) 从落库遥测重建的证据源。

    构造入参 `telemetry_rows` 是 `repo.list_telemetry()` 的返回：每行含
    `device_sn` / `occurred_at` / `component_ref` / `metrics`。本桥只读、不做写，
    与 `EvidenceSource` Protocol 同形，故 Agent 链路无需改动。
    """

    def __init__(self, telemetry_rows: list[dict[str, Any]], *, device_sn: str,
                 detector: GeneratedRuleDetector | None = None):
        self.device_sn = device_sn
        # 邻居检测复用**真实算子规则**（阈值出自 operators/generated/*.yaml）。
        # 注入以便测试；默认惰性加载。
        self._detector = detector
        self._by_component: dict[str, list[dict[str, Any]]] = {}
        for r in telemetry_rows:
            comp = r.get("component_ref", "")
            self._by_component.setdefault(comp, []).append(r)
        # occurred_at 是 ISO8601 字符串 → 字典序即时间序（同格式下成立）
        for rows in self._by_component.values():
            rows.sort(key=lambda x: x.get("occurred_at", ""))

    def _window_rows(self, component: str, window_start: str, window_end: str
                     ) -> list[dict[str, Any]]:
        rows = self._by_component.get(component, [])
        if not rows:
            return []
        # 窗口用 ISO8601 字符串比较（字典序=时间序）。候选窗即事件时间，故通常
        # 恰好命中；若窗口是 "09:55" 之类的纯时刻（旧测试形态）则比较不成立，
        # 此时退化为"取该部件全部行"，保证仍能给出证据而不是误报 not_found。
        hit = [r for r in rows
               if window_start <= r.get("occurred_at", "") <= window_end]
        return hit or rows

    def query_telemetry(self, device_sn, component, window_start, window_end):
        if component not in ALLOWED_COMPONENTS:
            return err(ToolErrorCode.DENIED, component)
        if device_sn != self.device_sn:
            return err(ToolErrorCode.DENIED, "device mismatch")
        rows = self._window_rows(component, window_start, window_end)
        if not rows:
            return None
        main = rows[-1]
        # 参照窗：同部件更早一行（若有）。让 Agent 能看出"窗口内指标相对参照的抬升"。
        idx = self._by_component[component].index(main)
        ref = self._by_component[component][idx - 1] if idx > 0 else None
        return {
            "device_sn": self.device_sn,
            "component": component,
            "window": f"{window_start}-{window_end}",
            "metrics": _standardize(main.get("metrics")),
            "reference_window_metrics": _standardize(ref.get("metrics")) if ref else None,
        }

    def query_vendor_knowledge(self, component_type, question=None):
        return [dict(item, retriever="repo_kb")
                for item in VENDOR_KNOWLEDGE.get(component_type, [])]

    def query_neighbor_events(self, device_sn, component, window_start, window_end):
        """查**同设备其它部件**在同一时间窗内是否有异常（真关联，非桩）。

        做法：遍历该设备其它部件的落库遥测 → 取窗口内（退化为该部件全部行）的
        最新一行 → 用真实算子规则检测 → 有命中才作为邻居事件返回，形如
        `{event, component, window, severity_hint, rule_id, observed, threshold}`。

        这与内置 `EvidenceStore.query_neighbor_events` 的返回形状一致（见其
        `NEIGHBOR_EVENTS`），故 Agent/工具层无需改动。
        """
        if component not in ALLOWED_COMPONENTS:
            return err(ToolErrorCode.DENIED, component)
        if device_sn != self.device_sn:
            return err(ToolErrorCode.DENIED, "device mismatch")
        detector = _detector_for(self)
        events: list[dict[str, Any]] = []
        for comp, rows in sorted(self._by_component.items()):
            if comp == component:
                continue                      # 邻居 = 其它部件，不含自身
            hit_rows = [r for r in rows
                        if window_start <= r.get("occurred_at", "") <= window_end]
            if not hit_rows:
                continue                      # 该邻居在此窗内无数据 → 不谈它
            latest = hit_rows[-1]
            for h in detector.detect(comp, _standardize(latest.get("metrics"))):
                events.append({
                    "event": h["rule_id"],
                    "component": comp,
                    "window": f"{window_start}-{window_end}",
                    "severity_hint": h["severity_hint"],
                    "rule_id": h["rule_id"],
                    "observed": h["observed"],
                    "threshold": h["threshold"],
                })
        return events

    def query_recent_changes(self, device_sn, component, since):
        if component not in ALLOWED_COMPONENTS:
            return err(ToolErrorCode.DENIED, component)
        return {"changes": RECENT_CHANGES.get(component, [])}
