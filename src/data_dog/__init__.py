"""data_dog：机器狗及其他具身设备遥测的 Agentic 故障调查平台。"""
__version__ = "0.1.0"

from .agent.loop import AgentLoop
from .correlation import Correlator, explain_episode
from .errors import ToolErrorCode
from .evidence.store import EvidenceStore
from .observability import CallStat, UsageTracker, fmt_cost, lookup_price
from .providers import Provider
from .reporter import (
    ProposeOutbox,
    get_outbox,
    propose_alert_report,
    propose_operator_release,
    propose_operator_rollback,
)
from .schemas import CandidateEvent, InvestigationReport
from .synth import (
    DEFAULT_PARAMS,
    FAULT_DEFS,
    Go2MotorModel,
    SynthGenConfig,
    SynthSample,
    generate_sample,
    synthesize_batch,
)
from .tools.investigation import InvestigationTools

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
    "Go2MotorModel",
    "SynthGenConfig",
    "SynthSample",
    "generate_sample",
    "synthesize_batch",
    "DEFAULT_PARAMS",
    "FAULT_DEFS",
    "UsageTracker",
    "CallStat",
    "lookup_price",
    "fmt_cost",
]
