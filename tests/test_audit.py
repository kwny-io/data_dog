"""安全审计（A1）：记录、查询、两种视野、以及"审计写失败不拖垮业务"。

为什么需要这一份
----------------
`docs/slo.md` §6 声明"审计记录保留 ≥ 1 年"，此前 `src/` 里零实现——只有访问日志
（运维观测，会滚动）与业务台账（回答"业务现在什么状态"，不是"谁做的、允许还是拒绝"）。
这份测试锁住的是**合规能用的最小面**：

1. **拒绝也留痕**：`auth.denied` 里最有价值的是"声称某租户但密钥不对"的冒充尝试；
2. **两种视野分得清**：租户只能看自己；`X-Ops-Token` 才看得到跨租户（含 `-` 下的系统事件）；
3. **审计写失败不拖垮业务**（但必须可观测：日志 + `data_dog_audit_write_failed_total`）；
4. **不记录秘密**：只记密钥**指纹**，不记密钥本身、不记请求体。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from data_dog import audit

_OPS = "ops-secret-token"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """单元用例用：临时 SQLite + 建表。"""
    url = f"sqlite:///{(tmp_path / 'audit.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", url)
    from data_dog.storage.db import init_db, reset_engine
    reset_engine()
    init_db(url)
    yield url
    reset_engine()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """端到端用例用：API key 开启 + 运维令牌配置好。"""
    url = f"sqlite:///{(tmp_path / 'audit_api.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", url)
    monkeypatch.setenv("DD_API_KEY", "global-key")
    monkeypatch.setenv("DD_OPS_TOKEN", _OPS)
    from data_dog.storage.db import init_db, reset_engine
    reset_engine()
    init_db(url)
    from data_dog.service.app import create_app
    with TestClient(create_app()) as c:
        yield c
    reset_engine()


def _items(resp) -> list[dict]:
    return resp.json()["items"]


# ---------------------------------------------------------------- 单元：记录/查询

def test_record_and_query_roundtrip(db):
    assert audit.record(action=audit.ACTION_AUTH_DENIED, outcome=audit.OUTCOME_DENIED,
                        tenant_id="t1", actor="abc123", target="/v1/candidates",
                        reason="tenant_key_mismatch", key_id="abc123",
                        detail={"claimed": "t1"}) is True
    out = audit.query(tenant_id="t1")
    assert out["total"] == 1 and out["count"] == 1
    row = out["items"][0]
    assert row["tenant_id"] == "t1"
    assert row["action"] == "auth.denied" and row["outcome"] == "denied"
    assert row["actor"] == "abc123" and row["key_id"] == "abc123"
    assert row["reason"] == "tenant_key_mismatch"
    assert row["detail"] == {"claimed": "t1"}
    assert row["ts"] > 0
    assert row["audit_id"].startswith("aud_")


def test_query_filters_and_pagination(db):
    for i in range(5):
        audit.record(action=audit.ACTION_OPS_ACCESS if i % 2 else audit.ACTION_AUTH_DENIED,
                     outcome=audit.OUTCOME_ALLOWED if i % 2 else audit.OUTCOME_DENIED,
                     tenant_id="t1", actor=f"k{i}")
    page = audit.query(tenant_id="t1", limit=2, offset=0)
    assert page["count"] == 2 and page["total"] == 5 and page["has_more"] is True
    # 新的在前：最后写的那条排第一
    assert page["items"][0]["actor"] == "k4"
    assert audit.query(tenant_id="t1", action=audit.ACTION_OPS_ACCESS)["total"] == 2
    assert audit.query(tenant_id="t1", outcome=audit.OUTCOME_DENIED)["total"] == 3
    assert audit.query(tenant_id="t1", actor="k3")["total"] == 1
    # `until` 设在很久以前 → 没有记录落在那个区间（注意别用 `since=1e9`：
    # 那是 2001 年，在测试当前时间**之前**，反而会命中全部记录）
    assert audit.query(tenant_id="t1", until=1.0)["total"] == 0
    assert audit.query(tenant_id="t1", since=1.0)["total"] == 5


def test_tenant_view_is_scoped_but_system_tenant_is_separate(db):
    audit.record(action=audit.ACTION_AUTH_DENIED, outcome=audit.OUTCOME_DENIED,
                 tenant_id="t1", reason="a")
    audit.record(action=audit.ACTION_AUTH_DENIED, outcome=audit.OUTCOME_DENIED,
                 tenant_id="t2", reason="b")
    audit.record(action=audit.ACTION_OPS_ACCESS, outcome=audit.OUTCOME_ALLOWED,
                 tenant_id=None, reason="no-tenant")     # → 保留租户 `-`

    assert audit.query(tenant_id="t1")["total"] == 1
    assert audit.query(tenant_id="t2")["total"] == 1
    assert audit.query(tenant_id=audit.SYSTEM_TENANT)["total"] == 1
    allrows = audit.query(tenant_id=None)                 # 跨租户（运维视野）
    assert allrows["total"] == 3
    assert {r["tenant_id"] for r in allrows["items"]} == {"t1", "t2", audit.SYSTEM_TENANT}


def test_record_picks_up_request_context(db):
    from data_dog.core.logging import clear_request_id, set_request_id
    from data_dog.core.tracing import clear_trace, start_trace

    set_request_id("rid-123")
    trace_id, _ = start_trace(None)
    try:
        audit.record(action=audit.ACTION_OPS_ACCESS, outcome=audit.OUTCOME_ALLOWED,
                     tenant_id="t1")
    finally:
        clear_request_id()
        clear_trace()

    row = audit.query(tenant_id="t1")["items"][0]
    assert row["request_id"] == "rid-123"
    assert row["trace_id"] == trace_id


def test_record_never_raises_and_counts_failure(db, monkeypatch):
    """审计写失败**不能**抛出去（否则一个合规组件会把业务打挂），但必须可观测。"""
    from data_dog.core import metrics
    from data_dog.storage import repository as repo

    before = metrics.audit_write_failed._value.get()      # noqa: SLF001 - 断言计数器

    def _boom(*a, **k):
        raise RuntimeError("audit table missing")

    monkeypatch.setattr(repo, "save_audit", _boom)
    assert audit.record(action=audit.ACTION_AUTH_DENIED,
                        outcome=audit.OUTCOME_DENIED, tenant_id="t1") is False
    assert metrics.audit_write_failed._value.get() == before + 1   # noqa: SLF001


def test_key_fingerprint_does_not_reveal_the_key():
    fp = audit.key_fingerprint("super-secret-key")
    assert fp and len(fp) == 12 and "super-secret" not in fp
    assert audit.key_fingerprint("") == ""


# ---------------------------------------------------------------- 端点：记录

def _h(tenant: str | None = None, ops: str | None = None) -> dict[str, str]:
    """认证头。**注意**：设了 `DD_API_KEY` 时，最外层守卫要求所有非探针端点都带它
    ——审计/运维端点也不例外；`X-Ops-Token` / `X-Tenant-Id` 是在它**之内**的视野选择。"""
    h = {"X-API-Key": "global-key"}
    if tenant:
        h["X-Tenant-Id"] = tenant
    if ops:
        h["X-Ops-Token"] = ops
    return h


def test_denied_request_is_audited_under_claimed_tenant(client):
    """声称 t1 但密钥不对 → 401，且**审计记在 t1 名下**（冒充尝试要能被 t1 看到）。"""
    r = client.get("/v1/candidates", headers={"X-Tenant-Id": "t1",
                                             "X-API-Key": "wrong"})
    assert r.status_code == 401                        # 全局单密钥模式：错密钥一律 401

    rows = _items(client.get("/v1/audit", headers=_h(tenant="t1")))
    assert len(rows) == 1
    assert rows[0]["action"] == "auth.denied"
    assert rows[0]["outcome"] == "denied"
    assert rows[0]["reason"] == "unauthorized"
    assert rows[0]["target"] == "/v1/candidates"
    # 只记**指纹**，不记密钥本身
    assert rows[0]["actor"] == audit.key_fingerprint("wrong")


def test_denied_request_without_tenant_lands_in_system_tenant(client):
    r = client.get("/v1/candidates", headers={"X-API-Key": "wrong"})
    assert r.status_code == 401
    # 租户视野看不到它
    assert _items(client.get("/v1/audit", headers=_h(tenant="t1"))) == []
    # 运维视野看得到（记在保留租户 `-` 下）。注意：**读跨租户审计本身也留痕**，
    # 所以这一次响应里除了那条拒绝，还会带上本次 `audit.read_all`。
    rows = _items(client.get("/v1/audit", headers=_h(ops=_OPS)))
    denied = [x for x in rows if x["action"] == "auth.denied"]
    assert [x["tenant_id"] for x in denied] == [audit.SYSTEM_TENANT]


def test_ops_access_is_audited(client):
    r = client.get("/v1/state/checkpoints", headers=_h(ops=_OPS))
    assert r.status_code == 200
    rows = _items(client.get("/v1/audit", headers=_h(ops=_OPS)))
    ops = [x for x in rows if x["action"] == "ops.access"]
    assert len(ops) == 1 and ops[0]["outcome"] == "allowed"
    assert ops[0]["target"] == "/v1/state/checkpoints"
    assert ops[0]["actor"] == audit.key_fingerprint(_OPS)


def test_audit_write_failure_does_not_break_the_request(client, monkeypatch):
    """审计表坏了，401 仍然是 401（可用性优先），但错误会被记下来。"""
    from data_dog.storage import repository as repo

    monkeypatch.setattr(repo, "save_audit",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = client.get("/v1/candidates", headers={"X-Tenant-Id": "t1",
                                             "X-API-Key": "wrong"})
    assert r.status_code == 401


# ---------------------------------------------------------------- 端点：两种视野

def test_audit_endpoint_requires_identity(client):
    assert client.get("/v1/audit", headers=_h()).status_code == 401
    # 出示了错的运维令牌 → 401（**不静默降级**成租户视野）
    assert client.get("/v1/audit",
                      headers=_h(ops="nope")).status_code == 401


def test_audit_endpoint_tenant_scope_isolated(client):
    client.get("/v1/candidates", headers={"X-Tenant-Id": "t1", "X-API-Key": "bad"})
    client.get("/v1/candidates", headers={"X-Tenant-Id": "t2", "X-API-Key": "bad"})

    t1 = _items(client.get("/v1/audit", headers=_h(tenant="t1")))
    t2 = _items(client.get("/v1/audit", headers=_h(tenant="t2")))
    assert len(t1) == 1 and t1[0]["tenant_id"] == "t1"
    assert len(t2) == 1 and t2[0]["tenant_id"] == "t2"


def test_audit_endpoint_ops_view_sees_all_and_is_itself_audited(client):
    client.get("/v1/candidates", headers={"X-Tenant-Id": "t1", "X-API-Key": "bad"})
    client.get("/v1/candidates", headers={"X-Tenant-Id": "t2", "X-API-Key": "bad"})

    rows = _items(client.get("/v1/audit", headers=_h(ops=_OPS)))
    assert {"t1", "t2"} <= {r["tenant_id"] for r in rows}, "运维视野应看到全部租户"
    # "谁来审计审计者"：跨租户读审计本身也留痕（保留租户 `-`），且**本次响应里就能看到**
    reads = [r for r in rows if r["action"] == "audit.read_all"]
    assert len(reads) == 1 and reads[0]["tenant_id"] == audit.SYSTEM_TENANT


def test_audit_endpoint_ops_token_unset_returns_404(tmp_path, monkeypatch):
    """未启用运维面 → 404（不暴露端点存在性），且不因此把跨租户数据放给租户视野。"""
    url = f"sqlite:///{(tmp_path / 'no_ops.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", url)
    monkeypatch.setenv("DD_API_KEY", "global-key")
    monkeypatch.delenv("DD_OPS_TOKEN", raising=False)
    from data_dog.storage.db import init_db, reset_engine
    reset_engine()
    init_db(url)
    try:
        from data_dog.service.app import create_app
        with TestClient(create_app()) as c:
            r = c.get("/v1/audit", headers=_h(ops="anything"))
            assert r.status_code == 404
    finally:
        reset_engine()
