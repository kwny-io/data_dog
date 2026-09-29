"""P4-2 控制面：算子注册表的**领域逻辑**（在 lifecycle 状态机 + 仓储之上）。

职责（docs/p4/02_control_plane.md §3）：
  - **注册**：验签通过方可入库（失败关闭）；内容绑定（改一字节即拒）。
  - **状态转移**：复用 `lifecycle.OperatorLifecycle` 的确定性状态机与门控，每步落台账。
  - **Canary 流量路由**：按 `rollout.percent` **确定性**分流（同 (operator, device)
    稳定落同一侧，否则抖动污染指标）。
  - **自动回滚**：Canary 阶段挂指标看门狗，越限即回滚到上一签名版本。

与 lifecycle 的关系：lifecycle 是**纯内存状态机**（可单测、无 IO）；本模块**包住**它，
把状态与台账持久化到仓储，并在其上补 canary 路由与看门狗。核心不变量仍由 lifecycle 守。

诚实边界（§6）：本模块不做密钥管理（私钥进环境变量，生产接 KMS）；看门狗依赖有意义
的指标，冷启动样本不足时**默认不自动推进**（保守，见 `should_rollback`）。
"""
from __future__ import annotations

import binascii
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from ..storage import repository as repo
from . import signing
from .lifecycle import OperatorLifecycle, TransitionError

# canary 阶段（rollout.stage）取值
STAGE_NONE = "none"
STAGE_CANARY = "canary"
STAGE_STABLE = "stable"


class RegistryError(ValueError):
    """注册/转移的领域错误（统一归类 invalid_input）。"""


class SignatureRejected(RegistryError):
    """验签失败——不可注册（失败关闭）。"""


@dataclass(frozen=True)
class CanaryThresholds:
    """自动回滚阈值（越限即回滚）。默认值保守：宁可多回滚，不可放过坏版本。"""
    max_crash_rate: float = 0.02          # 崩溃率上限 2%
    max_parse_drop: float = 0.05          # 解析成功率相对下降上限 5%
    max_candidate_multiple: float = 3.0   # 候选数相对 stable 的倍数上限（防误报暴涨）
    min_samples: int = 50                 # 样本不足 → 不自动判（保守）


def register_operator(
    session: Session, *, tenant_id: str, operator_id: str, version: str,
    dsl_source: str, signature: dict[str, Any], actor: str = "",
    secret: str | None = None, pubkey: str | None = None,
) -> dict[str, Any]:
    """注册一个算子版本。**验签失败即拒绝**（失败关闭）。

    验签对象含 dsl_source——调用方传的 source 与服务端重算的 hash 必须一致：
    客户端给 `signature.dsl_hash` 与 `dsl_source` 不符 → 拒（防"签名一套、内容是另一套"）。
    """
    if not signature or not signature.get("sig"):
        raise SignatureRejected("缺少签名（§11.3 失败关闭）")
    try:
        signing.verify_or_raise(signature, secret=secret, pubkey=pubkey,
                                source=dsl_source)
    except signing.SigningError as e:
        raise SignatureRejected(str(e)) from e

    dsl_hash = signing.source_hash(dsl_source)
    row = repo.upsert_operator_version(
        session, tenant_id=tenant_id, operator_id=operator_id, version=version,
        state="Registered", dsl_source=dsl_source, dsl_hash=dsl_hash,
        signature=signature, rollout={})
    repo.record_rollout_event(
        session, tenant_id=tenant_id, operator_id=operator_id, version=version,
        from_state=None, to_state="Registered", reason="注册（验签通过）", actor=actor)
    return row


def transition_operator(
    session: Session, *, tenant_id: str, operator_id: str, version: str,
    target: str, evidence: dict[str, Any] | None = None, actor: str = "",
    secret: str | None = None,
) -> dict[str, Any]:
    """把一个算子版本转移到 `target`（合法性由 lifecycle 判定）。

    状态由**持久化行**恢复进内存状态机（rebuild），转移成功再写回——保证状态机
    是唯一合法性来源，重启后仍一致。
    """
    row = repo.get_operator_version(session, tenant_id=tenant_id,
                                    operator_id=operator_id, version=version)
    if row is None:
        raise RegistryError("算子版本不存在")

    lc = _rebuild_lifecycle(row)
    ev = dict(evidence or {})
    # Registered 门控要求 signature：从已存行补上（注册时已验过）
    if target == "Registered" and "signature" not in ev:
        ev["signature"] = row.get("signature") or {}
    try:
        new_state = lc.transition(target, ev)
    except TransitionError as e:
        raise RegistryError(str(e)) from e

    rollout = row.get("rollout") or {}
    if target == "Canary":
        rollout = {**rollout, "stage": STAGE_CANARY,
                   "percent": int(ev.get("percent", rollout.get("percent", 0)))}
    elif target == "Active":
        rollout = {**rollout, "stage": STAGE_STABLE, "percent": 0}
    metrics = ev.get("metrics") or None
    updated = repo.set_operator_state(
        session, tenant_id=tenant_id, operator_id=operator_id, version=version,
        state=new_state, rollout=rollout, metrics_snapshot=metrics)
    repo.record_rollout_event(
        session, tenant_id=tenant_id, operator_id=operator_id, version=version,
        from_state=row.get("state"), to_state=new_state,
        reason=ev.get("reason", f"转移至 {target}"), actor=actor)
    return updated or row


def _rebuild_lifecycle(row: dict[str, Any]) -> OperatorLifecycle:
    """从持久化行恢复一个内存状态机（不重放 history——状态是权威，history 是台账）。"""
    lc = OperatorLifecycle(
        operator_id=row["operator_id"], version=row["version"],
        state=row.get("state") or "Registered")
    return lc


def start_canary(session: Session, *, tenant_id: str, operator_id: str,
                 version: str, percent: int, actor: str = "",
                 evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    """启动/调整灰度：把版本推入 Canary 并设流量百分比（0–100）。"""
    if not 0 <= percent <= 100:
        raise RegistryError("percent 须在 0–100")
    ev = dict(evidence or {})
    ev["percent"] = percent
    # 已在 Canary：只调百分比，不再走状态机（避免非法自转）
    row = repo.get_operator_version(session, tenant_id=tenant_id,
                                    operator_id=operator_id, version=version)
    if row is None:
        raise RegistryError("算子版本不存在")
    if row.get("state") == "Canary":
        rollout = {**(row.get("rollout") or {}), "stage": STAGE_CANARY,
                   "percent": percent}
        updated = repo.set_operator_state(
            session, tenant_id=tenant_id, operator_id=operator_id, version=version,
            state="Canary", rollout=rollout)
        repo.record_rollout_event(
            session, tenant_id=tenant_id, operator_id=operator_id, version=version,
            from_state="Canary", to_state="Canary",
            reason=f"调整灰度至 {percent}%", actor=actor)
        return updated or row
    return transition_operator(session, tenant_id=tenant_id,
                               operator_id=operator_id, version=version,
                               target="Canary", evidence=ev, actor=actor)


def rollback_operator(session: Session, *, tenant_id: str, operator_id: str,
                      version: str, reason: str, actor: str = ""
                      ) -> dict[str, Any]:
    """回滚一个算子版本（→ RolledBack）。语义是"停用当前版本"，非删除（§6）。"""
    row = repo.get_operator_version(session, tenant_id=tenant_id,
                                    operator_id=operator_id, version=version)
    if row is None:
        raise RegistryError("算子版本不存在")
    lc = _rebuild_lifecycle(row)
    try:
        new_state = lc.rollback(reason)
    except TransitionError as e:
        raise RegistryError(str(e)) from e
    updated = repo.set_operator_state(
        session, tenant_id=tenant_id, operator_id=operator_id, version=version,
        state=new_state, rollout={**(row.get("rollout") or {}), "stage": STAGE_NONE,
                                  "percent": 0})
    repo.record_rollout_event(
        session, tenant_id=tenant_id, operator_id=operator_id, version=version,
        from_state=row.get("state"), to_state=new_state, reason=reason, actor=actor)
    return updated or row


def previous_signed_version(session: Session, *, tenant_id: str,
                            operator_id: str, version: str
                            ) -> dict[str, Any] | None:
    """取某版本**之前**的最近签名版本（回滚目标）。见 §6：回到上一签名版本。"""
    row = repo.get_operator_version(session, tenant_id=tenant_id,
                                    operator_id=operator_id, version=version)
    if row is None:
        return None
    return repo.find_previous_signed_version(
        session, tenant_id=tenant_id, operator_id=operator_id,
        before_created_at=row["created_at"])


# ---------------------------------------------------------------- Canary 流量路由

def pick_version(candidates: list[dict[str, Any]], device_sn: str) -> dict[str, Any] | None:
    """按 rollout 把流量**确定性地**分给 canary 或 stable（§3.3）。

    输入：同一 operator_id 的候选版本行（含 canary 与 stable）。
    算法：仅当存在 canary 版本时按 `crc32(operator_id|device_sn) % 100 < percent`
    决定该设备走 canary；否则走 stable。

    **为什么用 crc32 而非随机**：同一 (算子, 设备) 必须**稳定**落同侧——否则同一
    设备时上时下，指标被抖动污染，也无法复现问题。确定性哈希就是这个不变量。
    """
    if not candidates:
        return None
    canary = next((c for c in candidates
                   if (c.get("rollout") or {}).get("stage") == STAGE_CANARY), None)
    stable = _stable_of(candidates)
    if canary is None:
        return stable or candidates[0]
    pct = int((canary.get("rollout") or {}).get("percent", 0))
    if pct <= 0:
        return stable or candidates[0]
    if pct >= 100:
        return canary
    key = f"{canary['operator_id']}|{device_sn}".encode()
    bucket = binascii.crc32(key) % 100
    return canary if bucket < pct else (stable or candidates[0])


def _stable_of(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """选 stable 版本：优先 Active，其次非 canary 的最新。"""
    actives = [c for c in candidates if c.get("state") == "Active"]
    if actives:
        return max(actives, key=lambda c: c["created_at"])
    non_canary = [c for c in candidates
                  if (c.get("rollout") or {}).get("stage") != STAGE_CANARY]
    if non_canary:
        return max(non_canary, key=lambda c: c["created_at"])
    return None


# ---------------------------------------------------------------- 自动回滚看门狗

def should_rollback(canary_metrics: dict[str, Any], stable_metrics: dict[str, Any],
                    thresholds: CanaryThresholds | None = None) -> tuple[bool, str]:
    """看门狗判定：canary 指标相对 stable 是否越限 → (是否回滚, 原因)。

    **保守默认**（§6）：样本数不足 `min_samples` → 不判定（返回 False），
    冷启动不误伤。任一越限即回滚，原因为越限项文字说明。
    """
    th = thresholds or CanaryThresholds()
    n = int(canary_metrics.get("samples", 0))
    if n < th.min_samples:
        return False, f"样本不足（{n} < {th.min_samples}），保守不判"

    crash = float(canary_metrics.get("crash_rate", 0.0))
    if crash > th.max_crash_rate:
        return True, f"崩溃率 {crash:.3f} > {th.max_crash_rate}"

    c_parse = canary_metrics.get("parse_success_rate")
    s_parse = stable_metrics.get("parse_success_rate")
    if c_parse is not None and s_parse is not None:
        if (s_parse - float(c_parse)) > th.max_parse_drop:
            return True, f"解析成功率下降 {s_parse - float(c_parse):.3f} > {th.max_parse_drop}"

    c_cand = canary_metrics.get("candidate_count")
    s_cand = stable_metrics.get("candidate_count")
    if c_cand is not None and s_cand is not None and float(s_cand) > 0:
        mult = float(c_cand) / float(s_cand)
        if mult > th.max_candidate_multiple:
            return True, f"候选数倍数 {mult:.2f} > {th.max_candidate_multiple}"

    return False, "指标正常"


def evaluate_watchdog(session: Session, *, tenant_id: str, operator_id: str,
                      version: str, canary_metrics: dict[str, Any],
                      stable_metrics: dict[str, Any], actor: str = "watchdog",
                      thresholds: CanaryThresholds | None = None
                      ) -> dict[str, Any]:
    """把看门狗判定落到一个算子版本上：越限即自动回滚。返回判定结果。

    仅在 Canary 态生效（其余态不动）。回滚复用 `rollback_operator`（确定性转移 + 台账）。
    """
    row = repo.get_operator_version(session, tenant_id=tenant_id,
                                    operator_id=operator_id, version=version)
    if row is None:
        raise RegistryError("算子版本不存在")
    if row.get("state") != "Canary":
        return {"rolled_back": False, "reason": f"当前态 {row.get('state')}，看门狗不生效"}

    verdict, reason = should_rollback(canary_metrics, stable_metrics, thresholds)
    if verdict:
        rollback_operator(session, tenant_id=tenant_id, operator_id=operator_id,
                          version=version, reason=f"自动回滚：{reason}", actor=actor)
    else:
        repo.set_operator_state(
            session, tenant_id=tenant_id, operator_id=operator_id, version=version,
            state="Canary", metrics_snapshot=canary_metrics)
    return {"rolled_back": verdict, "reason": reason}
