"""Eval 运行与打分：把一个 EvalCase 交给 AgentLoop 调查，按软约束评分。

支持两种 provider：
- mock：用预置的脚本 Provider（确定性，不联网），用于离线回归。
- real：用真实 LLM（需已配置 key），观察真实 Agent 行为。

评分是"软约束打分"：返回每项 通过/未达，以及总体通过率。真实 LLM 偶有波动，
所以这里是给"报告"，不对单一 case 做硬断言（mock 模式下可以做硬断言）。
"""
from __future__ import annotations

from data_dog.agent.loop import AgentLoop
from data_dog.evidence.store import EvidenceStore
from data_dog.schemas.events import CandidateEvent
from data_dog.tools.investigation import InvestigationTools

from .cases import CASES, EvalCase


def run_case(case: EvalCase, provider, model=None) -> dict:
    """跑单个 case，返回：report dict + 各项评分。"""
    candidate = CandidateEvent(**case.candidate)
    tools = InvestigationTools(EvidenceStore())
    agent = AgentLoop(provider, tools, model=model)
    report = agent.investigate(candidate)
    return _score(case, report.to_dict(), report)


def _score(case: EvalCase, report_dict: dict, report) -> dict:
    disposition = report_dict.get("disposition", "")
    checks = {}

    # 1) 裁决是否落在允许集 / 命中禁止集
    checks["disposition_in_expected"] = disposition in case.expects_disposition_in
    if case.forbids_disposition:
        checks["disposition_not_forbidden"] = disposition not in case.forbids_disposition
    else:
        checks["disposition_not_forbidden"] = True

    # 2) 证据召回 / 假设（软）：若是"禁止无证据推断"的 case，则不要求一定有假设；
    #    否则期望有结构化假设（说明 Agent 在做推理而不只是贴标签）。
    #    注意：rejected / needs_human_review / 无证据 case 合理可能没有假设，不强求。
    has_hypotheses = len(report_dict.get("hypotheses", [])) > 0
    if disposition in ("confirmed", "inconclusive"):
        checks["has_hypotheses"] = has_hypotheses   # 主张故障/结论时应有假设支撑
    else:
        checks["has_hypotheses"] = True             # 排除/待人工时不强求

    # 3) 置信度 >= 门槛
    checks["confidence_ok"] = float(report_dict.get("confidence", 0.0)) >= case.min_confidence

    # 4) 禁止无证据推断：若该 case 要求，则必须有缺失证据说明，且不能空口 confirmed
    if case.must_not_infer_without_evidence:
        has_missing = len(report_dict.get("missing_evidence", [])) > 0
        checks["no_infer_without_evidence"] = (
            (not (disposition == "confirmed")) and has_missing
        )
    else:
        checks["no_infer_without_evidence"] = True

    passed = sum(1 for v in checks.values() if v)
    total = len(checks)
    return {
        "case_id": case.case_id,
        "name": case.name,
        "disposition": disposition,
        "confidence": report_dict.get("confidence", 0.0),
        "summary": report_dict.get("summary", "")[:120],
        "checks": checks,
        "score": f"{passed}/{total}",
        "passed": passed == total,
    }


def run_all(provider, model=None, only: list[str] | None = None) -> list[dict]:
    cases = [c for c in CASES if only is None or c.case_id in only]
    results = []
    for c in cases:
        results.append(run_case(c, provider, model=model))
    return results


def summarize(results: list[dict]) -> dict:
    if not results:
        return {"n": 0, "pass_rate": 0.0}
    passed = sum(1 for r in results if r["passed"])
    return {
        "n": len(results),
        "passed": passed,
        "pass_rate": round(passed / len(results), 2),
    }
