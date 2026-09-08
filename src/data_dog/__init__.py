# -*- coding: utf-8 -*-
"""data_dog：机器狗及其他具身设备遥测的 Agentic 故障调查平台。"""
__version__ = "0.1.0"

from .providers import Provider
from .schemas import CandidateEvent, InvestigationReport
from .agent.loop import AgentLoop
from .evidence.store import EvidenceStore
from .tools.investigation import InvestigationTools
from .correlation import Correlator, explain_episode
from .errors import ToolErrorCode
from .reporter import (
    ProposeOutbox,
    get_outbox,
    propose_alert_report,
    propose_operator_release,
    propose_operator_rollback,
)

__all__ = [
    "__version__",
    "Provider",
    "CandidateEvent",
    "InvestigationReport",
    "AgentLoop",
    "EvidenceStore",
    "InvestigationTools",
    "Correlator",
    "explain_episode",
    "ToolErrorCode",
    "ProposeOutbox",
    "get_outbox",
    "propose_alert_report",
    "propose_operator_release",
    "propose_operator_rollback",
]
