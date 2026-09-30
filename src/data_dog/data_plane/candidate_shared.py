"""候选构建的**共享状态**版本：跨副本一致地归并命中。

与 `CandidateBuilder`（进程内）的区别
--------------------------------------
原版把分组状态 (`_groups`/`_open`/`_seq`) 放在实例内存里——单副本正确，多副本
各算各的：同一设备在 A、B 两实例上被当成两段独立事件，产生两条候选。本实现把
**每段事件的分组状态放进共享后端**，任一副本写入对其它副本可见，于是合并窗口
的判定是全局一致的。

实现要点
--------
- 每个 `base = device|component|signal` 一个状态条目：`{"seq": n, "groups": [...]}`，
  最后一个 group 是"打开中"的。`add` 在**一次原子 mutate** 内完成"是否在窗口内
  → 归并或新开"，seq 从共享状态递增，因此 `group_key = base#seq` 跨副本确定，
  candidate_id（=hash(group_key)）也随之稳定。
- 内存有界：每个 base 只保留最近 `max_groups` 段（打开中的 + 若干已关闭）。更早的
  已关闭段在前几次 build 时已随幂等 upsert 落库，淘汰不丢真相。
- 索引键 `_CAND_INDEX` 记录出现过的 base，`build` 据此枚举（跨副本可见）。

诚实边界：淘汰旧段后，若某个副本的 build 晚于淘汰、且该段从未被任何副本 emit，
则该段不会被产出。工程上以"每次 ingest 后立即 build 并落库"规避——见 stream
消费者的 flush 语义。
"""
from __future__ import annotations

import hashlib
from typing import Any

from ..schemas.events import CandidateEvent, EvidenceSummary
from ..state import get_state_store
from .candidate import _SEV_RANK, _SEV_SCORE, _parse_time, _signal_of
from .watermark import Watermark

_INDEX_KEY = "candidate:index"


class SharedCandidateBuilder:
    """命中 → 候选，状态在共享后端（多副本一致）。

    **事件时间与迟到（G2）**：语义与 `CandidateBuilder` 一致——注入 `watermark` 时
    合并判定收紧为**有向 + 水位线**（见 `candidate.CandidateBuilder` 文档）；未注入时
    退回原 `abs(delta) <= merge_window_s`（零回归）。水位线是**每副本一份**的观测
    （诚实边界：多副本下各见各的到货序，严格共享需按分片维护，对齐 P4-5）。
    """

    def __init__(self, *, merge_window_s: float = 5.0, max_groups: int = 32,
                 state_store: Any = None, namespace: str = "cand",
                 watermark: Watermark | None = None):
        self.merge_window_s = merge_window_s
        self.max_groups = max_groups
        self._ns = namespace
        self._state = state_store if state_store is not None else get_state_store()
        self.watermark = watermark
        self.late_merged = 0
        self.late_rejected = 0
        # 本实例（副本）自上次 flush 以来触碰过的组 key。build() 默认只产出这些
        # ——让 HTTP 请求/流式 flush 只返回"自己刚处理出来的候选"，而不是共享
        # 存储里所有设备/租户的全部候选。
        self._touched: set[str] = set()

    # ---- 内部 ----
    def _base_key(self, device_sn: str, component_ref: str, signal: str,
                  tenant_id: str = "") -> str:
        # 含租户：不同租户的同名设备不得共享分组状态（否则跨租户合并/候选 id 相撞）。
        prefix = f"{tenant_id}|" if tenant_id else ""
        return f"{self._ns}:{prefix}{device_sn}|{component_ref}|{signal}"

    def _register_index(self, base: str) -> None:
        def fn(cur: Any) -> tuple[Any, Any]:
            keys = list(cur) if isinstance(cur, list) else []
            if base not in keys:
                keys.append(base)
            return keys, None

        self._state.mutate(_INDEX_KEY, fn)

    @staticmethod
    def _new_group(device_sn: str, component_ref: str, signal: str,
                   occurred_at: str, seq: int, base: str) -> dict[str, Any]:
        return {
            "key": f"{base}#{seq}", "device_sn": device_sn,
            "component_ref": component_ref, "signal": signal,
            "window_start": occurred_at, "window_end": occurred_at,
            "rule_ids": [], "severity": "info", "sample_count": 0,
            "evidence": [], "evidence_refs": [], "operator_id": "",
            "last_dt": occurred_at,
        }

    def _apply_hit(self, g: dict[str, Any], h: dict[str, Any],
                   occurred_at: str, raw_ref: str,
                   baseline: dict[str, float] | None) -> None:
        """把一个命中并入组 g（原地修改）。"""
        g["sample_count"] = int(g.get("sample_count", 0)) + 1
        # last_dt/window_end 取最大事件时间（乱序并入时不把基准拉回；同进程内版一致）。
        new_dt = _parse_time(occurred_at)
        end_dt = _parse_time(g.get("window_end"))
        if new_dt is None:
            g["window_end"] = occurred_at
        elif end_dt is None or new_dt >= end_dt:
            g["window_end"] = occurred_at
            last_dt = _parse_time(g.get("last_dt"))
            if last_dt is None or new_dt > last_dt:
                g["last_dt"] = occurred_at
        rule_id = h["rule_id"]
        if rule_id not in g["rule_ids"]:
            g["rule_ids"].append(rule_id)
        sev = h.get("severity_hint", "info")
        if _SEV_RANK.get(sev, 0) > _SEV_RANK.get(g.get("severity", "info"), 0):
            g["severity"] = sev
        g["operator_id"] = h.get("operator_id", g.get("operator_id", ""))
        fld = h.get("field", "")
        base_val = (baseline or {}).get(fld)
        g["evidence"] = [e for e in g.get("evidence", []) if e.get("key") != fld]
        g["evidence"].append({
            "key": fld, "from_value": base_val, "to_value": h.get("value"),
            "baseline": base_val, "unit": None,
            "note": f"{rule_id}: {h.get('operator')} {h.get('threshold')}",
        })
        if raw_ref and raw_ref not in g.get("evidence_refs", []):
            g["evidence_refs"].append(raw_ref)

    def add(self, *, device_sn: str, component_ref: str, metrics: dict[str, Any],
            hits: list[dict[str, Any]], occurred_at: str, received_at: str,
            raw_ref: str = "", baseline: dict[str, float] | None = None,
            tenant_id: str = "") -> None:
        """加入一个事件的所有命中（同一事件多规则命中 → 各自归组）。"""
        for h in hits:
            signal = _signal_of(h["rule_id"], h.get("field", ""))
            base = self._base_key(device_sn, component_ref, signal, tenant_id)
            self._register_index(base)
            now_dt = _parse_time(occurred_at)
            if self.watermark is not None and now_dt is not None:
                self.watermark.observe(now_dt.timestamp())

            def fn(cur: Any, _h=h, _now=now_dt, _b=base, _sig=signal
                   ) -> tuple[Any, Any]:
                st: dict[str, Any] = (
                    cur if isinstance(cur, dict) else {"seq": 0, "groups": []})
                groups: list[dict[str, Any]] = st["groups"]
                last = groups[-1] if groups else None
                in_window = False
                if last is not None:
                    last_dt = _parse_time(last.get("last_dt"))
                    if _now is None or last_dt is None:
                        in_window = True
                    elif self.watermark is None:
                        in_window = abs(
                            (_now - last_dt).total_seconds()) <= self.merge_window_s
                    else:
                        in_window = self._in_window_with_watermark(last_dt, _now)
                if not in_window:
                    st["seq"] = int(st.get("seq", 0)) + 1
                    groups.append(self._new_group(
                        device_sn, component_ref, _sig, occurred_at,
                        st["seq"], _b))
                self._apply_hit(groups[-1], _h, occurred_at, raw_ref, baseline)
                # 有界：只留最近 max_groups 段（打开中的在末尾）
                if len(groups) > self.max_groups:
                    del groups[: len(groups) - self.max_groups]
                self._touched.add(groups[-1]["key"])
                return st, None

            self._state.mutate(base, fn)

    def _in_window_with_watermark(self, last_dt: Any, now_dt: Any) -> bool:
        """有界迟到语义下的合并判定（与进程内版同构）。"""
        assert self.watermark is not None
        delta = (now_dt - last_dt).total_seconds()
        if 0.0 <= delta <= self.merge_window_s:
            return True
        if -self.watermark.allowed_lateness_s <= delta < 0.0:
            if not self.watermark.is_late(now_dt.timestamp()):
                self.late_merged += 1
                return True
            self.late_rejected += 1
            return False
        if delta < 0.0:
            self.late_rejected += 1
        return False

    def build(self, *, only_touched: bool = True) -> list[CandidateEvent]:
        """产出候选列表（确定性排序：设备→部件→信号→首见时间）。

        `only_touched=True`（默认）只产出本实例触碰过的组——HTTP 请求/流式 flush
        只返回自己刚处理出来的候选。`False` 则枚举共享存储里**所有**组（运维视角
        ／全量对账用）。
        """
        if only_touched:
            keys = set(self._touched)
            self._touched.clear()
            bases = sorted({k.split("#", 1)[0] for k in keys})
        else:
            index = self._state.get(_INDEX_KEY) or []
            bases = sorted(index)
            keys = None

        out: list[CandidateEvent] = []
        for base in bases:
            st = self._state.get(base)
            if not isinstance(st, dict):
                continue
            for g in st.get("groups", []):
                if keys is None or g["key"] in keys:
                    out.append(self._to_candidate(g))
        out.sort(key=lambda c: (c.device_sn, c.component_ref, c.signal,
                                c.window_start, c.candidate_id))
        return out

    @staticmethod
    def _to_candidate(g: dict[str, Any]) -> CandidateEvent:
        cid = "cand_" + hashlib.sha256(g["key"].encode("utf-8")).hexdigest()[:12]
        evidence = [EvidenceSummary(**e) for e in g.get("evidence", [])]
        return CandidateEvent(
            candidate_id=cid,
            device_sn=g["device_sn"], component_ref=g["component_ref"],
            signal=g["signal"], severity_hint=g.get("severity", "info"),
            window_start=g["window_start"], window_end=g["window_end"],
            sample_count=int(g.get("sample_count", 0)),
            rule_matches=list(g.get("rule_ids", [])),
            anomaly_score=_SEV_SCORE.get(g.get("severity", "info"), 0.3),
            evidence=evidence,
            evidence_refs=list(g.get("evidence_refs", [])) + [f"metricwin://{cid}"],
            detector_provenance=g.get("operator_id", ""),
        )
