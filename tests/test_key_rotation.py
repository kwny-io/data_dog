"""密钥轮换与吊销（②）：`DD_TENANT_KEYS` 的 v2 密钥环写法。

要解决的问题（`docs/delivery.md` 自认的差距）
-------------------------------------------
静态字符串密钥有两个运维上绕不开的问题：**换不掉**（换 = 所有调用方同一瞬间改，
没有交叠窗口）与**撤不掉**（泄露了只能全量替换 + 重启）。v2 允许一个租户在册
**多把**密钥，各带 `id` / `not_after` / `revoked_at`：

- **轮换** = 旧新并存（交叠窗口）→ 调用方逐个切 → 旧那把置 `revoked_at`；
- **吊销** = 置 `revoked_at` 后**下一次请求即失效**（`settings` 调用时读取、不缓存，
  无需重启）；
- **可审计** = `key_id` 落进 `audit_log.key_id`，"已吊销的那把被谁用过"可查。

本文件锁六件事：
1. 轮换交叠期**两把都能用**；
2. 吊销后**立刻 403 `key_revoked`**，且新钥匙不受影响；
3. 过期（`not_after`）单独一档 `key_expired`；吊销优先于过期；
4. 被判定的拒绝**照样留审计并带上 `key_id`**；
5. 两类"看起来无害但会毁掉轮换/审计语义"的配置**失败关闭**：环内 `id` 重复、
   旧新密钥同值；
6. **不许提前 return**——比较次数只与环长有关（否则时序差泄露"哪把在生效"）。
"""
from __future__ import annotations

import hmac as _hmac
import json

import pytest
from fastapi.testclient import TestClient

from data_dog import settings
from data_dog.storage.db import init_db, reset_engine

GLOBAL = "global-admin-key"
OPS = "ops-token-xyz"
NEW_KEY = "tenant-a-key-2026-10"
OLD_KEY = "tenant-a-key-2026-07"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'keyring.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", url)
    reset_engine()
    init_db(url)

    from data_dog.service.app import create_app
    with TestClient(create_app()) as c:
        yield c
    reset_engine()


def _raw(event_id="e1"):
    return {
        "event_id": event_id, "occurred_at": "2026-09-13T10:00:00Z",
        "received_at": "2026-09-13T10:00:00.100Z", "sequence": 1,
        "vendor": "unitree", "model": "go2", "firmware": "3.2.1",
        "protocol_version": "motor-log-v2",
        "component_hint": "joint_motor.front_left_hip",
        "payload_inline": json.dumps({"temperature": 96, "current": 12.0, "mode": 0}),
    }


def _ingest(client, tenant, key, event_id="e1"):
    h = {"X-Device-Sn": "go2-sn-001", "X-Tenant-Id": tenant}
    if key is not None:
        h["X-API-Key"] = key
    return client.post("/v1/telemetry", headers=h, json={"events": [_raw(event_id)]})


def _ring(monkeypatch, keys, tenant="tenant-a"):
    monkeypatch.setenv("DD_TENANT_KEYS", json.dumps({tenant: {"keys": keys}}))
    monkeypatch.setenv("DD_API_KEY", GLOBAL)


# ---------------------------------------------------------------- 轮换

def test_rotation_overlap_accepts_both_keys(client, monkeypatch):
    """交叠窗口：旧新两把**同时有效**——这是"换得掉"的前提。"""
    _ring(monkeypatch, [
        {"id": "2026-10", "secret": NEW_KEY},
        {"id": "2026-07", "secret": OLD_KEY},
    ])
    assert _ingest(client, "tenant-a", NEW_KEY).status_code == 200
    assert _ingest(client, "tenant-a", OLD_KEY, "e2").status_code == 200


def test_revoked_key_is_rejected_immediately(client, monkeypatch):
    """吊销：旧那把置 `revoked_at` 后**下一次请求即失效**，新钥匙照常。"""
    _ring(monkeypatch, [
        {"id": "2026-10", "secret": NEW_KEY},
        {"id": "2026-07", "secret": OLD_KEY, "revoked_at": "2026-10-01T00:00:00Z"},
    ])
    r = _ingest(client, "tenant-a", OLD_KEY)
    assert r.status_code == 403
    assert r.json()["reason"] == "key_revoked"
    # 新钥匙不受影响——吊销一把不该连带打挂整个租户
    assert _ingest(client, "tenant-a", NEW_KEY, "e2").status_code == 200


def test_expired_key_is_rejected(client, monkeypatch):
    _ring(monkeypatch, [
        {"id": "2026-10", "secret": NEW_KEY},
        {"id": "2025-01", "secret": OLD_KEY, "not_after": "2025-02-01T00:00:00Z"},
    ])
    r = _ingest(client, "tenant-a", OLD_KEY)
    assert r.status_code == 403
    assert r.json()["reason"] == "key_expired"


def test_not_after_in_the_future_is_still_active(client, monkeypatch):
    _ring(monkeypatch, [
        {"id": "2026-10", "secret": NEW_KEY, "not_after": "2099-01-01T00:00:00Z"},
    ])
    assert _ingest(client, "tenant-a", NEW_KEY).status_code == 200


def test_revocation_wins_over_expiry(monkeypatch):
    """两样都设时判定为 `revoked`：更强的语义先答，且**放行判断与运维视图同源**。"""
    k = settings.TenantKey("t", "k", "s",
                           not_after=1.0, revoked_at=2.0)
    assert k.status(now=100.0) == "revoked"


# ---------------------------------------------------------------- 可审计

def test_revoked_denial_is_audited_with_key_id(client, monkeypatch):
    """② 与 ① 的接点：被吊销的钥匙被用时，台账里能认出是**哪一把**。

    只有指纹的话，"吊销的这把被用过"与"那把被用过"分不出来——`key_id` 才是
    运维真正要看的字段。
    """
    from data_dog import audit

    _ring(monkeypatch, [
        {"id": "2026-10", "secret": NEW_KEY},
        {"id": "2026-07", "secret": OLD_KEY, "revoked_at": "2026-10-01T00:00:00Z"},
    ])
    assert _ingest(client, "tenant-a", OLD_KEY).status_code == 403

    rows = audit.query(tenant_id="tenant-a", action=audit.ACTION_AUTH_DENIED)["items"]
    assert rows, "拒绝没有留审计"
    rec = rows[0]
    assert rec["reason"] == "key_revoked"
    assert rec["key_id"] == "2026-07", (
        f"审计里的 key_id 是 {rec['key_id']!r}——吊销追责要靠它，不能是空或别的钥匙")
    assert OLD_KEY not in json.dumps(rec, ensure_ascii=False), "审计里混进了密钥本身"


# ---------------------------------------------------------------- 失败关闭

@pytest.mark.parametrize("keys,why", [
    ([{"id": "dup", "secret": "s1"}, {"id": "dup", "secret": "s2"}], "id 重复"),
    ([{"id": "a", "secret": "same"}, {"id": "b", "secret": "same"}], "密钥值相同"),
])
def test_semantics_breaking_rings_fail_closed(client, monkeypatch, keys, why):
    """两类配置必须拒绝启动而不是"能用就行"：

    - `id` 重复 → 审计记了 key_id 也分不清是哪把；
    - 旧新**同值** → "只吊销旧的那把"不成立（同一值必然同时命中两把），
      轮换会**静默退化成没轮换**。
    """
    _ring(monkeypatch, keys)
    r = _ingest(client, "tenant-a", "s1")
    assert r.status_code == 503, f"{why} 应失败关闭"
    assert r.json()["reason"] == "tenant_keys_misconfigured"


@pytest.mark.parametrize("bad", [
    {"keys": []}, {"keys": "not-a-list"}, {"keys": [{"id": "a"}]},
    {"keys": [{"secret": ""}]}, {"keys": [{"secret": "s", "not_after": "not-a-date"}]},
    {"keys": [{"secret": "s", "revoked_at": 12345}]},
])
def test_malformed_keys_shape_fails_closed(monkeypatch, bad):
    monkeypatch.setenv("DD_TENANT_KEYS", json.dumps({"tenant-a": bad}))
    with pytest.raises(ValueError):
        settings.tenant_keyring()


# ---------------------------------------------------------------- 时序

def test_resolution_compares_every_key_without_short_circuit(monkeypatch):
    """**不许提前 return**：比较次数必须等于环长。

    若"第一把就中"比"最后一把才中"快，攻击者用时序差就能探出**当前生效的是哪把**——
    轮换交叠期正好把这件事暴露出来（哪把是新的、哪把快废了）。用 spy 数比较次数，
    比计时断言稳定得多。
    """
    calls: list[tuple] = []
    real = _hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(_hmac, "compare_digest", spy)
    ring = {"t": tuple(settings.TenantKey("t", f"k{i}", f"s{i}") for i in range(4))}

    key, verdict = settings.resolve_tenant_key("t", "s0", keyring=ring)
    assert (key.key_id, verdict) == ("k0", "active")
    assert len(calls) == 4, (
        f"只比较了 {len(calls)} 次（环长 4）——提前 return 会泄露密钥在环里的位置")

    calls.clear()
    key, verdict = settings.resolve_tenant_key("t", "s3", keyring=ring)
    assert (key.key_id, verdict) == ("k3", "active")
    assert len(calls) == 4, "命中最末一把时比较次数也不该变"


def test_verdict_closed_set(monkeypatch):
    """判定是**闭集**，且未知租户与不符在服务边界同码同 reason。"""
    ring = {"t": (settings.TenantKey("t", "k", "s", revoked_at=1.0),)}
    assert settings.resolve_tenant_key("t", "nope", keyring=ring)[1] == "mismatch"
    assert settings.resolve_tenant_key("other", "s", keyring=ring)[1] == "unknown_tenant"
    assert settings.resolve_tenant_key("t", "s", keyring=ring)[1] == "revoked"


# ---------------------------------------------------------------- 运维视图

def _keys(client, ops=OPS, global_key=GLOBAL):
    h = {}
    if global_key is not None:
        h["X-API-Key"] = global_key
    if ops is not None:
        h["X-Ops-Token"] = ops
    return client.get("/v1/keys", headers=h)


def test_keys_endpoint_lists_metadata_but_never_secrets(client, monkeypatch):
    monkeypatch.setenv("DD_OPS_TOKEN", OPS)
    _ring(monkeypatch, [
        {"id": "2026-10", "secret": NEW_KEY},
        {"id": "2026-07", "secret": OLD_KEY, "revoked_at": "2026-10-01T00:00:00Z"},
    ])
    r = _keys(client)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    by_id = {i["key_id"]: i for i in body["items"]}
    assert by_id["2026-10"]["status"] == "active"
    assert by_id["2026-07"]["status"] == "revoked"
    assert NEW_KEY not in r.text and OLD_KEY not in r.text, "运维视图泄露了密钥本身"


def test_keys_endpoint_requires_ops_token(client, monkeypatch):
    """未启用运维面 → 404（端点不暴露）；令牌不符 → 401。"""
    monkeypatch.setenv("DD_OPS_TOKEN", OPS)
    _ring(monkeypatch, [{"id": "k1", "secret": NEW_KEY}])
    assert _keys(client, ops=None).status_code == 401
    assert _keys(client, ops="wrong").status_code == 401

    monkeypatch.delenv("DD_OPS_TOKEN", raising=False)
    assert _keys(client, ops=OPS).status_code == 404


def test_keys_endpoint_fails_closed_on_bad_config(client, monkeypatch):
    """配置坏 → 503，**不回一个"看起来正常"的空列表**（那会让人以为密钥环是空的）。

    注意这里断言的是**最外层守卫**的错误体（`reason` 在顶层）：守卫是最外层中间件，
    它先于路由发现配置坏并失败关闭，所以 HTTP 层面根本走不到路由。路由自身那条
    同类兜底另有单测（`test_keys_route_itself_fails_closed`），不在 HTTP 层。
    """
    monkeypatch.setenv("DD_OPS_TOKEN", OPS)
    monkeypatch.setenv("DD_API_KEY", GLOBAL)
    monkeypatch.setenv("DD_TENANT_KEYS", "{not json")
    r = _keys(client)
    assert r.status_code == 503
    assert r.json()["reason"] == "tenant_keys_misconfigured"


def test_keys_route_itself_fails_closed(monkeypatch):
    """路由**自己**也要失败关闭，不依赖"外层守卫一定会先拦"。

    守卫是最外层中间件，今天它先拦；但这条兜底是**纵深防御**——一旦守卫的顺序或
    覆盖范围变了（新增公开路径、换中间件栈），路由不该悄悄回一个空列表。
    """
    from fastapi import HTTPException

    from data_dog.service.routes.keys import list_keys

    monkeypatch.setenv("DD_TENANT_KEYS", "{not json")
    with pytest.raises(HTTPException) as ei:
        list_keys(scope={"key_id": "ops"})
    assert ei.value.status_code == 503
    assert ei.value.detail["reason"] == "tenant_keys_misconfigured"


def test_keys_endpoint_read_is_audited(client, monkeypatch):
    """读密钥环本身是敏感动作（能看出哪把已吊销 → 挑软的下手）→ 留痕。"""
    from data_dog import audit

    monkeypatch.setenv("DD_OPS_TOKEN", OPS)
    _ring(monkeypatch, [{"id": "k1", "secret": NEW_KEY}])
    assert _keys(client).status_code == 200
    rows = audit.query(tenant_id=None, action=audit.ACTION_KEYRING_READ)["items"]
    assert rows, "读密钥环没有留审计"
