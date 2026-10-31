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
- 近期变更（`query_recent_changes`）**已接入 `ChangeSource` 适配器**（G7）：
  按 `DD_CHANGE_SOURCE` 选 mock（内置表）/jsonl/webhook/db（推送式落库），
  **四种来源都真按 since 过滤**（此前 since 被静默忽略）。
  来源**不可用时失败关闭**：返回统一 `unavailable` 错误（工具层原样透传，Agent 拿不到
  `changes` 字段，因而无法据此下"近期无变更"的结论）——绝不把内置样例表冒充成权威
  变更。只有显式打开 `DD_CHANGE_FALLBACK_DEMO=1`（本地演示用）才回退样例表，且回退
  结果带 `degraded=True` 标注。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ..data_plane.causal import NeighborEvent, infer_causal_links
from ..data_plane.scenarios import _STANDARD_NAME
from ..errors import ToolErrorCode, err
from ..evidence.detectors import GeneratedRuleDetector, load_detector
from ..evidence.store import ALLOWED_COMPONENTS, VENDOR_KNOWLEDGE

_log = logging.getLogger(__name__)


def _epoch_s(iso: str) -> float | None:
    """ISO8601 字符串 → epoch 秒；无法解析 → None（该事件不参与因果推导）。"""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def _source_name(src: Any) -> str:
    """变更来源的可读名（供 Agent 报告标注"变更数据从哪来"）。"""
    return type(src).__name__


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


def _sort_key(row: dict[str, Any]) -> tuple[int, float]:
    """排序键：解析不了时间的行整体沉到最前（`(0, 0.0)`），其余按真实时刻排。

    为什么不用字符串：见 `RepoEvidenceBridge.__init__` 的注释——整秒时刻不带
    小数位，字典序会把它们排到同秒的其它样本之后。
    """
    t = _epoch_s(row.get("occurred_at", ""))
    return (0, 0.0) if t is None else (1, t)


def _row_before(rows: list[dict[str, Any]],
                first: dict[str, Any]) -> dict[str, Any] | None:
    """同部件中**早于窗口首行**的最近一条；窗口从设备第一条数据开始时返回 None。"""
    idx = rows.index(first)
    return rows[idx - 1] if idx > 0 else None


def _peak_row(rows: list[dict[str, Any]],
              ref: dict[str, float] | None) -> dict[str, Any]:
    """窗口内**偏离参照最大**的一行，用作"这个窗口实际发生了什么"的代表值。

    判据是各指标相对参照的**归一化偏离**之和（`|v - ref| / (|ref| + 1e-9)`），
    取最大者。没有参照（窗口从数据开头起）时退回窗口末行——此时没有基线可比，
    末行至少是"当前状态"。

    为什么不用"最后一个样本"：持续性故障的窗口末尾常常是解除瞬间。仿真器
    F1 堵转实跑时，5 分钟窗口 17710 行里有 10956 行是 14.98A，最后 3 行却是
    恢复后的 0.29A；取末行会让 Agent 得出"候选声称 14.5A、实测 0.29A → 误报"
    的反向结论，而真堵转就此被否掉。
    """
    if ref is None:
        return rows[-1]

    def deviation(row: dict[str, Any]) -> float:
        std = _standardize(row.get("metrics"))
        total = 0.0
        for k, base in ref.items():
            v = std.get(k)
            if v is None:
                continue
            total += abs(v - base) / (abs(base) + 1e-9)
        return total

    return max(rows, key=deviation)


def _window_stats(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """窗口内每个指标的 min/max/mean。

    Agent 要看的是"这 5 分钟里有没有过半时间在 14.5A 以上"，单点代表值答不了
    这个问题；均值/min/max 让它能在不读全量原始日志的前提下自行判断持续性。
    """
    acc: dict[str, list[float]] = {}
    for row in rows:
        for k, v in _standardize(row.get("metrics")).items():
            acc.setdefault(k, []).append(v)
    return {
        k: {"min": min(vs), "max": max(vs), "mean": sum(vs) / len(vs)}
        for k, vs in acc.items() if vs
    }


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
                 detector: GeneratedRuleDetector | None = None,
                 change_source: Any = None,
                 prior_reports: list[dict[str, Any]] | None = None):
        self.device_sn = device_sn
        # 跨调查记忆：该设备/部件的既往报告（调用方已按 component 过滤；空 = 不暴露该工具）。
        self._prior_reports = list(prior_reports or [])
        # 邻居检测复用**真实算子规则**（阈值出自 operators/generated/*.yaml）。
        # 注入以便测试；默认惰性加载。
        self._detector = detector
        # G7：变更来源。注入优先；默认惰性取 `settings` 指定的来源（mock/jsonl/webhook）。
        self._change_source = change_source
        self._by_component: dict[str, list[dict[str, Any]]] = {}
        for r in telemetry_rows:
            comp = r.get("component_ref", "")
            self._by_component.setdefault(comp, []).append(r)
        # ⚠ 必须按**解析后的时间**排序，不能按字符串。
        # `_iso()` 在整秒时刻不写小数位（`...:49Z`），而 `.`(0x2E) < `Z`(0x5A)，
        # 字典序会把 `...:49Z` 排到 `...:49.970000Z` **之后**。同一秒内几十条样本
        # 的顺序因此是错的，"取最后一条"就取到了恢复后的那条。
        for rows in self._by_component.values():
            rows.sort(key=_sort_key)

    def _window_rows(self, component: str, window_start: str, window_end: str
                     ) -> list[dict[str, Any]]:
        rows = self._by_component.get(component, [])
        if not rows:
            return []
        lo, hi = _epoch_s(window_start), _epoch_s(window_end)
        # 窗口按**事件时间**比较。解析不了（如旧测试的 "09:55" 纯时刻）就退化为
        # "取该部件全部行"，保证仍能给出证据而不是误报 not_found。
        if lo is None or hi is None:
            return rows
        hit = [r for r in rows
               if (t := _epoch_s(r.get("occurred_at", ""))) is not None
               and lo <= t <= hi]
        return hit or rows

    def query_telemetry(self, device_sn, component, window_start, window_end):
        if component not in ALLOWED_COMPONENTS:
            return err(ToolErrorCode.DENIED, component)
        if device_sn != self.device_sn:
            return err(ToolErrorCode.DENIED, "device mismatch")
        rows = self._window_rows(component, window_start, window_end)
        if not rows:
            return None

        # 参照窗 = 窗口**开始之前**的最后一条，而不是"代表行的上一条"。
        # 后者是窗口内部的相邻样本（同样处于故障中），拿它当基线等于把故障
        # 自己的值当参照，抬升被稀释成 0。
        ref = _row_before(self._by_component[component], rows[0])
        ref_metrics = _standardize(ref.get("metrics")) if ref else None

        # 代表值取窗口内**偏离参照最大**的那条，而不是最后一条。
        # 持续性故障（如堵转 5 分钟）的窗口末尾恰好是解除瞬间——末行读到的是
        # 恢复后的正常值，Agent 据此会判定"候选声称 14.5A，实测 0.29A，误报"。
        # 这正是 2026-10-31 实跑时把真堵转判成假警报的那条路径。
        main = _peak_row(rows, ref_metrics)
        return {
            "device_sn": self.device_sn,
            "component": component,
            "window": f"{window_start}-{window_end}",
            "metrics": _standardize(main.get("metrics")),
            "reference_window_metrics": ref_metrics,
            "sample_count": len(rows),
            "window_metrics": _window_stats(rows),
        }

    def query_vendor_knowledge(self, component_type, question=None):
        return [dict(item, retriever="repo_kb")
                for item in VENDOR_KNOWLEDGE.get(component_type, [])]

    @property
    def supports_prior_memory(self) -> bool:
        """本设备是否有既往调查报告（决定是否向 Agent 暴露跨调查记忆工具）。"""
        return bool(self._prior_reports)

    def query_prior_investigations(self, device_sn, component, signal=""):
        """既往调查报告（先验）。按 component（可选 signal）过滤，新的在前，最多 5 条。"""
        bad = self._denied(device_sn, component)
        if bad is not None:
            return bad
        items = [p for p in self._prior_reports
                 if p.get("component_ref") == component
                 and (not signal or p.get("signal") == signal)]
        items.sort(key=lambda p: p.get("created_at") or "", reverse=True)
        return {"device_sn": self.device_sn, "component": component, "signal": signal,
                "prior_investigations": items[:5], "truncated": len(items) > 5,
                "note": "既往结论是先验，非本次证据；须与当前证据交叉验证。"}

    def _denied(self, device_sn: str, component: str) -> dict[str, Any] | None:
        """统一的越权/设备校验；通过返回 None，否则返回错误 dict。"""
        if component not in ALLOWED_COMPONENTS:
            return err(ToolErrorCode.DENIED, component)
        if device_sn != self.device_sn:
            return err(ToolErrorCode.DENIED, "device mismatch")
        return None

    def _window_events(
        self, component: str, window_start: str, window_end: str,
        *, include_self: bool, with_time: bool = False,
    ) -> list[dict[str, Any]]:
        """扫落库遥测，对窗口内（退化为全部行）的每行跑真实算子，收集命中事件。

        每个事件形如 `{event, component, window, severity_hint, rule_id,
        observed, threshold}`；`with_time=True` 时附 `occurred_at` / `occurred_at_s`
        （供因果时序推导用）。
        """
        detector = _detector_for(self)
        events: list[dict[str, Any]] = []
        for comp, rows in sorted(self._by_component.items()):
            if comp == component and not include_self:
                continue                      # 邻居 = 其它部件，不含自身
            hit_rows = [r for r in rows
                        if window_start <= r.get("occurred_at", "") <= window_end]
            if not hit_rows:
                continue                      # 该部件在此窗内无数据 → 不谈它
            latest = hit_rows[-1]
            for h in detector.detect(comp, _standardize(latest.get("metrics"))):
                ev: dict[str, Any] = {
                    "event": h["rule_id"],
                    "component": comp,
                    "window": f"{window_start}-{window_end}",
                    "severity_hint": h["severity_hint"],
                    "rule_id": h["rule_id"],
                    "observed": h["observed"],
                    "threshold": h["threshold"],
                }
                if with_time:
                    occ = latest.get("occurred_at", "")
                    ev["occurred_at"] = occ
                    ev["occurred_at_s"] = _epoch_s(occ)
                events.append(ev)
        return events

    def query_neighbor_events(self, device_sn, component, window_start, window_end):
        """查**同设备其它部件**在同一时间窗内是否有异常（真关联，非桩）。

        做法：遍历该设备其它部件的落库遥测 → 取窗口内（退化为该部件全部行）的
        最新一行 → 用真实算子规则检测 → 有命中才作为邻居事件返回，形如
        `{event, component, window, severity_hint, rule_id, observed, threshold}`。

        这与内置 `EvidenceStore.query_neighbor_events` 的返回形状一致（见其
        `NEIGHBOR_EVENTS`），故 Agent/工具层无需改动。
        """
        denied = self._denied(device_sn, component)
        if denied is not None:
            return denied
        return self._window_events(component, window_start, window_end,
                                   include_self=False)

    def query_causal_chain(self, device_sn, component, window_start, window_end):
        """查该部件窗口内的**疑似因果链**（G6：跨部件、带方向与滞后）。

        与 `query_neighbor_events`（只做同窗共现）不同：本方法把窗口内**本部件 +
        邻居**的命中事件按**事件时间**排序，用 `infer_causal_links` 找出"先发生 +
        拓扑合理（沿供电链/兄弟）+ 滞后在窗内"的有序对。

        返回 `{causal_chains: [{cause, effect, lag_s, direction, confidence,
        suspect}], window, truncated}`：
        - `direction` = parent_to_child（上游先动，正常传播）/ sibling / child_to_parent；
        - `suspect=True` 表示方向反了（子先父后），仅作提示、不否认；
        - 未在拓扑表登记的部件不参与（不硬塞）。

        诚实边界：这是**工程启发式**（时间序 + 拓扑相邻），非真正因果推断，故
        对外措辞一律"疑似因果链"。
        """
        from .. import settings
        if settings.causal_enabled() is False:
            return {"causal_chains": [], "window": f"{window_start}-{window_end}",
                    "truncated": False, "reason": "causal_disabled"}
        denied = self._denied(device_sn, component)
        if denied is not None:
            return denied
        raw = self._window_events(component, window_start, window_end,
                                  include_self=True, with_time=True)
        # 无事件时间的事件不参与因果推导（宁缺毋滥，不拿 0.0 硬凑）
        timed = [e for e in raw if e.get("occurred_at_s") is not None]
        nevents = [NeighborEvent(component=e["component"],
                                 occurred_at_s=e["occurred_at_s"],
                                 rule_id=e.get("rule_id", ""),
                                 severity=e.get("severity_hint", ""))
                   for e in timed]
        links = infer_causal_links(nevents, max_lag_s=settings.causal_max_lag_s())
        return {
            "device_sn": device_sn,
            "component": component,
            "window": f"{window_start}-{window_end}",
            "causal_chains": [link.to_dict() for link in links],
            "truncated": False,
        }

    def _changes(self) -> Any:
        """取变更来源（注入优先；否则按 settings 建默认来源，惰性缓存）。"""
        if self._change_source is None:
            from ..evidence.change_source import default_change_source
            self._change_source = default_change_source()
        return self._change_source

    def query_recent_changes(self, device_sn, component, since):
        """查某部件近期变更（G7：走 `ChangeSource`，**真按 since 过滤**）。

        此前这里读内置假表且**忽略 since**（静默错误：Agent 以为按时间过滤了、
        实际拿到很久前的变更）。现改为经 `ChangeSource`，所有实现都真正过滤。

        来源不可用 → **失败关闭**（默认口径）：返回 `unavailable` 统一错误，形状与
        越权的 `denied` 一致。选错误而不是 `{"changes": []}` 是刻意的——空列表在
        Agent 眼里等价于"近期没有变更"，那是**假结论**（"查不到"≠"没有"），而本仓
        的承诺是"拿不准就不下结论"。工具层（`tools/investigation.py`）对 `ok=False`
        原样透传，故 Agent 拿到的是明确的错误码、连 `changes` 字段都没有。

        要演示/离线复现"降级"这条路径，必须显式打开 `DD_CHANGE_FALLBACK_DEMO=1`：
        此时才回退内置样例表，且结果带 `degraded=True`，不至于被误读成生产数据。
        """
        if component not in ALLOWED_COMPONENTS:
            return err(ToolErrorCode.DENIED, component)
        src = self._changes()
        try:
            records = src.query(device_sn=device_sn, component=component, since=since)
        except Exception as e:                 # noqa: BLE001 - 任何来源故障都必须显式化
            from .. import settings
            _log.warning("变更来源不可用，失败关闭", extra={
                "source": _source_name(src), "component": component,
                "error": f"{type(e).__name__}: {e}",
                "demo_fallback": settings.change_fallback_demo()})
            if not settings.change_fallback_demo():
                # 只回错误码 + 来源名 + 异常类名：不回异常正文（不向 Agent 泄内部细节）
                return err(ToolErrorCode.UNAVAILABLE,
                           f"变更来源不可用: {_source_name(src)}"
                           f"（{type(e).__name__}）",
                           reason="change_source_unavailable")
            from ..evidence.change_source import MockChangeSource
            demo = MockChangeSource().query(
                device_sn=device_sn, component=component, since=since)
            return {"changes": [r.to_dict() for r in demo],
                    "source": "builtin_fallback", "degraded": True}
        return {"changes": [r.to_dict() for r in records],
                "source": _source_name(src)}
