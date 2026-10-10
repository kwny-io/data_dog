"""`keys` 域端点：租户密钥环的**运维视图**（② 密钥轮换/吊销）。

为什么只有只读视图、没有"签发/吊销 API"
---------------------------------------
密钥环是**环境变量配置**（`DD_TENANT_KEYS`），轮换/吊销是有意的**配置动作**（改配置
+ 重载），而不是 HTTP 动作：把"签发密钥"做成端点，等于给攻击者造了一个
"拿下这个端点就等于能给自己发钥匙"的高价值目标，而它换来的便利在真实运维里很小
（轮换本就是低频、走变更流程的事）。

但"可撤销"要能**被验证**，否则运维没法确认轮换做完了。所以本端点提供只读核对：
**现在在册的有哪几把、哪把已吊销/过期、指纹是多少**（指纹可与 `audit_log.actor`
对上）。它只回**元数据**，**永不回密钥本身**——这个端点要回答的是"有哪几把"，
不是把秘密再抄一遍。
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ... import audit, settings
from ...errors import ToolErrorCode
from ..deps import require_ops_token_scope

router = APIRouter()


@router.get("/v1/keys", tags=["keys"])
def list_keys(scope: dict[str, Any] = Depends(require_ops_token_scope)) -> dict[str, Any]:
    """列出密钥环元数据（**运维视野**，需 `X-Ops-Token`）。

    排序固定为 `(tenant_id, key_id)`：运维要能直接 diff 两次输出看"哪把没了"。
    `status` 由 `TenantKey.status()` 给出（吊销优先于过期），与放行判断**同源**，
    避免"运维视图说 active、请求却被拒"这种最招人恨的漂移。
    """
    try:
        keyring = settings.tenant_keyring()
    except ValueError as e:
        # 与最外层守卫同一口径：配置坏 → 503 失败关闭。
        # 绝不回一个"看起来正常"的空列表——那会让运维以为密钥环是空的。
        raise HTTPException(status_code=503, detail={
            "ok": False, "error": ToolErrorCode.UNAVAILABLE.value,
            "detail": str(e), "reason": "tenant_keys_misconfigured"}) from e

    now = time.time()
    items: list[dict[str, Any]] = []
    for tenant, keys in sorted(keyring.items()):
        for k in sorted(keys, key=lambda x: x.key_id):
            items.append({
                "tenant_id": tenant,
                "key_id": k.key_id,
                "status": k.status(now),
                "not_after": k.not_after,
                "revoked_at": k.revoked_at,
                # 与 audit_log.actor 里存的指纹同口径（sha256("hmac:"+secret)[:12]），
                # 这样"这把钥匙谁在用"能在台账里对上。**不是**密钥本身。
                "fingerprint": audit.key_fingerprint(k.secret),
            })

    # 读密钥环本身是敏感动作：能看出"哪把已吊销/过期"就能挑软的下手 → 留痕。
    audit.record(action=audit.ACTION_KEYRING_READ, outcome=audit.OUTCOME_ALLOWED,
                 actor=scope.get("key_id", ""), target="/v1/keys")
    return {"ok": True, "count": len(items), "total": len(items),
            "limit": None, "offset": 0, "has_more": False, "items": items}
