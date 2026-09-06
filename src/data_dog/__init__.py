# -*- coding: utf-8 -*-
"""data_dog：机器狗及其他具身设备遥测的 Agentic 故障调查平台。"""
__version__ = "0.1.0"

from .providers import Provider
from .schemas import CandidateEvent, InvestigationReport
from .agent.loop import AgentLoop
from .evidence.store import EvidenceStore
from .tools.investigation import InvestigationTools
from .correlation import Correlator
from .errors import ToolErrorCode

__all__ = [
    "__version__",
    "Provider",
    "CandidateEvent",
    "InvestigationReport",
    "AgentLoop",
    "EvidenceStore",
    "InvestigationTools",
    "Correlator",
    "ToolErrorCode",
]
