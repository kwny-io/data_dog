"""统一错误体与鉴权（含声明式依赖）。不 import app，避免循环。"""
from __future__ import annotations

import hmac
from typing import Any

from fastapi import Header, HTTPException

from .. import settings
from ..errors import ToolErrorCode


def _auth_from_headers(tenant_id: str | None, device_sn: str | None
                       ) -> dict[str, Any]:
    """身份只认请求头（认证主体），不认正文。缺一即拒绝（失败关闭）。"""
    if not tenant_id or not device_sn:
        raise HTTPException(status_code=401,
                            detail="缺少可信身份：需要请求头 X-Tenant-Id 与 X-Device-Sn")
    return {"tenant_id": tenant_id, "device_sn": device_sn, "principal": "http-gateway"}
def _tenant_required(tenant_id: str | None) -> str:
    """读路径的租户上下文：只认 `X-Tenant-Id`，缺失/空 → 401（失败关闭）。

    为什么不接受 `?tenant_id=` 查询参数：查询串可被链接、日志、浏览器历史携带与
    篡改，不是可信身份来源。租户只能来自请求头（认证主体注入的位置）——查询参数
    里的 tenant_id 一律被忽略（有测试断言这一点）。
    """
    if not tenant_id:
        raise HTTPException(status_code=401,
                            detail="缺少可信租户：需要请求头 X-Tenant-Id")
    return tenant_id
def _api_key_ok(provided: str, expected: str) -> bool:
    """常量时间比较，避免按字符逐位试探（时序侧信道）。"""
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
def _extract_api_key(request) -> str:
    """从请求头取调用方密钥（`X-API-Key`）。"""
    return request.headers.get("X-API-Key", "") or ""
def _error_body(code: ToolErrorCode, detail: Any = "", *, reason: str = ""
                ) -> dict[str, Any]:
    """服务边界统一错误体：{ok, error, detail}。错误码保持有限闭集。"""
    body: dict[str, Any] = {"ok": False, "error": code.value, "detail": detail}
    if reason:
        body["reason"] = reason
    return body
def _require_ops_token(x_ops_token: str | None) -> None:
    """运维端点鉴权：无 `DD_OPS_TOKEN` → 404（端点不暴露）；有则须匹配。

    状态快照/diff 含各租户算子状态（敏感），故**独立**于 DD_API_KEY 门控——
    即便开了 API 鉴权，运维端点也只对持 ops token 者开放。
    """
    expected = settings.ops_token()
    if not expected:
        raise HTTPException(status_code=404, detail="未启用运维端点")
    if not _api_key_ok(x_ops_token or "", expected):
        raise HTTPException(status_code=401, detail="缺少或错误的 X-Ops-Token")


# ---------------------------------------------------------------- 声明式依赖
# 为什么把上面这些助手再包成 FastAPI 依赖：拆分前 42 个端点各自在函数体里手写
# `tenant = _tenant_required(x_tenant_id)`——**漏写**只能靠人肉 review 发现，
# 而漏掉的后果是无租户上下文/越权。包成 `Depends(...)` 后它会出现在
# `route.dependant` 里，于是 tests/test_service_routes.py 可以反射断言
# "每个 /v1 路由都挂了鉴权依赖"：把纪律变成可执行的检查，而不是靠记性。

def require_tenant(
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
) -> str:
    """声明式租户校验：缺 `X-Tenant-Id` → 401（失败关闭）。"""
    return _tenant_required(x_tenant_id)


def require_tenant_and_device(
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_device_sn: str | None = Header(default=None, alias="X-Device-Sn"),
) -> dict[str, Any]:
    """写入路径的声明式身份校验：租户与设备号都要，缺一 → 401（失败关闭）。"""
    return _auth_from_headers(x_tenant_id, x_device_sn)


def require_ops_token(
    x_ops_token: str | None = Header(default=None, alias="X-Ops-Token"),
) -> None:
    """运维端点的声明式鉴权：未启用运维面 → 404；token 不符 → 401。"""
    _require_ops_token(x_ops_token)


def require_tenant_or_ops(
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_ops_token: str | None = Header(default=None, alias="X-Ops-Token"),
) -> dict[str, Any]:
    """**租户或运维**二选一的声明式鉴权，返回"视野"：`{"scope","tenant_id"}`。

    用途：安全审计查询（`/v1/audit`）——租户看自己，运维看全部（含保留租户 `-` 下的
    系统事件，例如"未带租户头的拒绝"）。

    规则（拿不准就拒，审计是最不该被匿名读的东西）：
      - 出示了 `X-Ops-Token`：必须匹配 `DD_OPS_TOKEN`（未启用运维面 → 404；不匹配 → 401），
        成功后视野为**全部租户**。**不静默降级**成租户视野——"带了运维令牌却只看到
        自己"会让人以为审计丢了数据；
      - 否则必须带 `X-Tenant-Id`（缺失 → 401），视野为**该租户**。

    为什么做成声明式依赖（而不是在路由函数体里读 header）：`tests/test_service_routes.py`
    会反射 `route.dependant` 断言"每个 /v1 端点都挂了鉴权依赖"——写在依赖里才能被它看见，
    写在函数体里只能靠人肉 review。
    """
    if x_ops_token:
        _require_ops_token(x_ops_token)
        from .. import audit
        # 只回传**指纹**（哪把令牌），不回传令牌本身——调用方要留痕时用它
        return {"scope": "all", "tenant_id": None,
                "key_id": audit.key_fingerprint(x_ops_token)}
    return {"scope": "tenant", "tenant_id": _tenant_required(x_tenant_id)}
