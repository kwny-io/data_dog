"""安全审计（A1）：**谁在何时对什么做了什么、结果如何**——只增、可查、按年保留。

为什么单独一个顶层模块
----------------------
`docs/slo.md` §6 声明"审计记录保留 ≥ 1 年"，但此前 `src/` 里**零实现**：只有访问日志
（运维观测，会被滚动/采样）与业务台账（算子状态机、投递记录——它们回答的是"业务现在
是什么状态"，不是"谁做的、允许还是拒绝"）。采购/合规评审问的正是后者。

放顶层（而不是 `core/` 或 `storage/`）：它同时要 `storage`（写库）与 `core`（request_id/
trace_id 上下文），而这两个都是**叶子层**，叶子层之间不许互相 import（`test_layering`
守着）。顶层横切模块（与 `knowledge.py` / `reporter.py` 同类）可以自由组合，也不会被
叶子层反向依赖。

记录什么（v1）
--------------
- `auth.denied`：认证/授权**失败**——含"声称某租户但密钥不对"的**冒充尝试**。
  失败比成功更值得留痕，所以 `outcome` 有 `denied` 这一档，而不是"只记成功"。
- `ops.access`：运维面（`/v1/state/*`）被访问（低量、高价值）。
- 未来的 `tenant.purge`（⑥）/ `key.rotate`（②）等直接调 `record()`。

三条纪律
--------
1. **best-effort 写入**：`record()` **不抛异常**（审计写失败绝不能把业务请求带崩），
   失败返回 False + 记 error 日志 + 打指标。调用方若需要"记不上就别做"，可检查返回值
   （见 `DD_AUDIT_REQUIRED` 的说明）。
2. **不记录秘密**：只记 `key_id`（哪把钥匙），不记密钥本身、不记请求体。
3. **无租户上下文的系统事件**记到保留租户 `SYSTEM_TENANT`（`-`）：未带 `X-Tenant-Id`
   的拒绝、运维操作都属此类。它们只能被系统扫描/运维查询看到（RLS 策略的租户分支
   不会把它们放进任何真实租户的视野）。
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from .core.logging import get_request_id
from .core.tracing import get_trace_id

_log = logging.getLogger(__name__)

# 保留租户：无租户上下文的系统/运维事件记在它名下。
# 用 `-`（短、可见、不像正常租户 id）而不是空串：`tenant_id` 是 NOT NULL 且进主键，
# 空串会和"忘了传租户"的 bug 混在一起，无法区分"系统事件"与"漏了"。
SYSTEM_TENANT = "-"

# 动作名闭集（避免散落的字符串导致查询口径漂移）
ACTION_AUTH_DENIED = "auth.denied"
ACTION_OPS_ACCESS = "ops.access"
ACTION_AUDIT_READ_ALL = "audit.read_all"

OUTCOME_ALLOWED = "allowed"
OUTCOME_DENIED = "denied"
OUTCOME_ERROR = "error"


def key_fingerprint(secret: str) -> str:
    """密钥指纹（前 12 位）：只用于**标识是哪把钥匙**，不泄露密钥本身。

    与 `operator_factory/signing.py::_key_id_hmac` 同口径（`sha256("hmac:"+secret)`），
    但这里独立实现以免为审计引入对算子工厂的依赖。
    """
    import hashlib
    if not secret:
        return ""
    return hashlib.sha256(("hmac:" + secret).encode("utf-8")).hexdigest()[:12]


def make_audit_id() -> str:
    """审计 id：**唯一**（不是确定性幂等 id）——两次相同事件就是两条事实。"""
    return f"aud_{uuid.uuid4().hex[:16]}"


def record(*, action: str, outcome: str, tenant_id: str | None = None,
           actor: str = "", target: str = "", reason: str = "",
           key_id: str = "", source_ip: str = "",
           detail: dict[str, Any] | None = None,
           request_id: str | None = None, trace_id: str | None = None) -> bool:
    """写一条审计记录。**不抛异常**（返回是否写成）。

    `tenant_id` 语义：
      - 传具体租户 → 记在该租户名下（RLS 租户分支可见），用于"请求声称了某租户"的场景；
      - 传 `None` → 记在 `SYSTEM_TENANT`（未声称租户的系统/运维事件）。

    `request_id` / `trace_id`：不传则取当前请求上下文（`core.logging` / `core.tracing`）。
    **最外层中间件**（鉴权守卫）里上下文尚未建立，那里必须显式传（见 `service/app.py::_deny`）。
    """
    from .storage.db import session_scope
    from .storage.repository import save_audit

    tenant = tenant_id or SYSTEM_TENANT
    row_id = make_audit_id()
    payload = dict(detail or {})
    try:
        with session_scope(tenant_id=tenant) as s:
            save_audit(s, tenant_id=tenant, audit_id=row_id, ts=_now(),
                       actor=actor, action=action, target=target,
                       outcome=outcome, reason=reason[-200:],
                       request_id=(get_request_id() if request_id is None
                                   else request_id),
                       trace_id=(get_trace_id() if trace_id is None else trace_id),
                       key_id=key_id, source_ip=source_ip, detail=payload)
    except Exception as e:                            # noqa: BLE001
        # 审计写失败**不能**把业务请求带崩，但绝不能静默：日志 + 指标
        _log.error("审计写入失败", extra={
            "action": action, "outcome": outcome, "tenant_id": tenant,
            "error": f"{type(e).__name__}: {e}"})
        try:
            from .core import metrics
            metrics.audit_write_failed.inc()
        except Exception:                             # noqa: BLE001
            pass
        return False
    return True


def query(*, tenant_id: str | None, action: str | None = None,
          outcome: str | None = None, actor: str | None = None,
          since: float | None = None, until: float | None = None,
          limit: int | None = None, offset: int = 0) -> dict[str, Any]:
    """翻审计记录（新的在前），返回与其它列表端点同形的信封。

    `tenant_id=None` = **跨租户**（运维视图）：走 `system=True` 会话，否则 RLS 下零行可见。
    """
    from .storage.db import session_scope
    from .storage.repository import count_audit, list_audit

    with session_scope(system=tenant_id is None) as s:
        items = list_audit(s, tenant_id=tenant_id, action=action, outcome=outcome,
                           actor=actor, since=since, until=until,
                           limit=limit, offset=offset)
        total = count_audit(s, tenant_id=tenant_id, action=action,
                            outcome=outcome, actor=actor, since=since, until=until)
    return {"ok": True, "count": len(items), "total": total,
            "limit": limit, "offset": offset,
            "has_more": (offset + len(items)) < total, "items": items}


def _now() -> float:
    import time
    return time.time()
