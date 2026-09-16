# -*- coding: utf-8 -*-
"""E2E 证据桥：把 P2 数据面/合成样本接成 Agent 的查询接口（与 EvidenceStore 同形）。

Agent 通过 4 个只读工具查证据；这里提供一个**由候选反查遥测**的证据源：
  - query_telemetry：从候选窗口对应的样本里取部件遥测（含参照窗口）
  - query_vendor_knowledge：复用内置厂商知识（阈值/原因）
  - query_neighbor_events / query_recent_changes：空/内置

实现与 `SyntheticEvidenceSource` 同接口，故 `InvestigationTools` / `AgentLoop` 不用改——
这正是"换数据源不改 Agent"的解耦原则（P1/P2 都已兑现，这里再次复用）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..errors import ToolErrorCode, err
from ..evidence.store import ALLOWED_COMPONENTS, RECENT_CHANGES, VENDOR_KNOWLEDGE


class CandidateEvidenceBridge:
    """按候选 → 遥测窗口的证据源。telemetry_by_component 由 P2 回放时填充。"""

    def __init__(self, telemetry_by_component: Optional[Dict[str, Dict]] = None,
                 *, device_sn: str = "device-go2-0001"):
        self.telemetry = telemetry_by_component or {}
        self.device_sn = device_sn

    def query_telemetry(self, device_sn, component, window_start, window_end):
        if component not in ALLOWED_COMPONENTS:
            return err(ToolErrorCode.DENIED, component)
        tel = self.telemetry.get(component)
        if tel is None:
            return None
        # 只允许查自己设备的证据（身份注入的越权校验交给上层 ctx，这里做基础防护）
        if device_sn != self.device_sn:
            return err(ToolErrorCode.DENIED, "device mismatch")
        return tel

    def query_vendor_knowledge(self, component_type, question=None):
        return [dict(item, retriever="e2e_kb")
                for item in VENDOR_KNOWLEDGE.get(component_type, [])]

    def query_neighbor_events(self, device_sn, component, window_start, window_end):
        return {"device_sn": device_sn, "events": []}

    def query_recent_changes(self, device_sn, component, since):
        if component not in ALLOWED_COMPONENTS:
            return err(ToolErrorCode.DENIED, component)
        return {"changes": RECENT_CHANGES.get(component, [])}
