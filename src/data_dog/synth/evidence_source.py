"""合成遥测数据源：把 Go2 合成生成器的输出接进现有 Agent 链路。

对接方式：实现与 EvidenceStore 相同的查询接口（query_telemetry /
query_vendor_knowledge / query_neighbor_events / query_recent_changes），
这样 InvestigationTools / AgentLoop 完全不用改——换数据源即可。
这是"MuJoCo 跑通后降级方案"的数据层：MuJoCo 只换这一层，Agent 逻辑不动。

查询语义：
- query_telemetry(device, component, ws, we) 从对应 sample 里取该部件遥测窗口。
- 权限白名单复用 EvidenceStore 的 ALLOWED_COMPONENTS（或本模块映射）。
"""
from __future__ import annotations

from ..errors import ToolErrorCode, err
from ..evidence.store import ALLOWED_COMPONENTS, RECENT_CHANGES, VENDOR_KNOWLEDGE
from .generator import SynthSample


class SyntheticEvidenceSource:
    """基于合成样本的证据库。兼容 EvidenceStore 查询接口。"""

    def __init__(self, samples: list[SynthSample],
                 known_faults: dict[str, str] | None = None):
        # 按 (joint, sample) 存，便于按部件查
        self.samples = samples
        # 每个部件 → 它的样本（用于查询）
        self._by_joint: dict[str, SynthSample] = {}
        for s in samples:
            for j in s.joints:
                self._by_joint.setdefault(j, s)

    # ---- 与 EvidenceStore 相同的查询接口 ----
    def query_telemetry(self, device_sn, component, window_start, window_end):
        if component not in ALLOWED_COMPONENTS:
            return err(ToolErrorCode.DENIED, component)
        sample = self._by_joint.get(component)
        if sample is None:
            return None
        return sample.telemetry(component)

    def query_vendor_knowledge(self, component_type, question=None):
        # 复用内置厂商知识（阈值/原因）
        return [{
            **item, "retriever": "synthetic_kb"
        } for item in VENDOR_KNOWLEDGE.get(component_type, [])]

    def query_neighbor_events(self, device_sn, component, window_start, window_end):
        # 简化：返回空列表（合成单样本调查不需要邻居关联）
        return []

    def query_recent_changes(self, device_sn, component, since):
        if component not in ALLOWED_COMPONENTS:
            return err(ToolErrorCode.DENIED, component)
        return {"changes": RECENT_CHANGES.get(component, [])}

    @staticmethod
    def known_fault_components():
        return set(ALLOWED_COMPONENTS)
