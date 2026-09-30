"""P4-2 控制面领域逻辑测试（对齐 docs/p4/02_control_plane.md §5.2–§5.6）。

覆盖六条验收：验签失败关闭 / 状态机持久化（重启可读）/ Canary 确定性分流 /
自动回滚越限触发 / 租户隔离 / 零回归。用临时文件 SQLite（不是 :memory:），
以真实模拟"进程重启后数据还在"。
"""
from __future__ import annotations

import pytest

from data_dog.operator_factory import registry
from data_dog.operator_factory.registry import (
    CanaryThresholds,
    RegistryError,
    SignatureRejected,
)
from data_dog.operator_factory.signing import sign_package
from data_dog.storage import repository as repo
from data_dog.storage.db import init_db, session_scope

SECRET = "cp-secret"
T1 = "t1"
T2 = "t2"


@pytest.fixture()
def db_url(tmp_path):
    url = f"sqlite:///{(tmp_path / 'reg.db').as_posix()}"
    init_db(url)
    return url


def _sig(oid="op.temp", ver="1.0.0", src="temperature >= 75"):
    return sign_package(oid, ver, src, secret=SECRET).to_dict()


def _register(db_url, *, oid="op.temp", ver="1.0.0", src="temperature >= 75",
              tenant=T1, signature=None):
    with session_scope(db_url) as s:
        return registry.register_operator(
            s, tenant_id=tenant, operator_id=oid, version=ver, dsl_source=src,
            signature=signature if signature is not None else _sig(oid, ver, src),
            actor="test", secret=SECRET)


# ---------------------------------------------------------------- 注册 + 验签（§5.2）

def test_register_verify_roundtrip_and_persist(db_url):
    """注册成功 → 落库；新会话（模拟重启）仍可读，字段完整。"""
    _register(db_url)
    with session_scope(db_url) as s:
        row = repo.get_operator_version(s, tenant_id=T1,
                                        operator_id="op.temp", version="1.0.0")
    assert row is not None
    assert row["state"] == "Registered"
    assert row["dsl_hash"] == _sig()["dsl_hash"]


def test_register_tampered_source_rejected(db_url):
    """客户端给的签名 dsl_hash 与正文 dsl_source 不符 → 拒（内容绑定）。"""
    bad = _sig(src="temperature >= 75")  # 签名绑的是原来的内容
    with pytest.raises(SignatureRejected):
        _register(db_url, src="temperature >= 999", signature=bad)
    # 未落库
    with session_scope(db_url) as s:
        assert repo.get_operator_version(s, tenant_id=T1, operator_id="op.temp",
                                         version="1.0.0") is None


def test_register_missing_or_bad_signature_rejected(db_url):
    with pytest.raises(SignatureRejected):
        _register(db_url, signature={})
    with pytest.raises(SignatureRejected):
        _register(db_url, signature={"alg": "hmac-sha256", "sig": "deadbeef"})


def test_register_no_key_is_fail_closed(db_url):
    """无签名凭据（secret/pubkey 皆空）→ 验签失败，不放行未签制品。"""
    with session_scope(db_url) as s:
        with pytest.raises(SignatureRejected):
            registry.register_operator(
                s, tenant_id=T1, operator_id="op.temp", version="1.0.0",
                dsl_source="temperature >= 75", signature=_sig())


# ---------------------------------------------------------------- 状态机持久化（§5.3）

def test_transition_happy_and_persist(db_url):
    """Registered→Shadow→Canary→Active，每步落库；新会话读到终态。"""
    _register(db_url)

    def step(target, **ev):
        with session_scope(db_url) as s:
            return registry.transition_operator(
                s, tenant_id=T1, operator_id="op.temp", version="1.0.0",
                target=target, evidence=ev, actor="test")

    step("Shadow")
    step("Canary", metrics_ok=True, percent=10)
    step("Active", metrics_ok=True)

    with session_scope(db_url) as s:
        row = repo.get_operator_version(s, tenant_id=T1,
                                        operator_id="op.temp", version="1.0.0")
        events = repo.list_rollout_events(s, tenant_id=T1,
                                          operator_id="op.temp", version="1.0.0")
    assert row["state"] == "Active"
    assert row["rollout"]["stage"] == registry.STAGE_STABLE
    # 台账：注册 + Shadow + Canary + Active = 4 条
    assert [e["to_state"] for e in events] == [
        "Registered", "Shadow", "Canary", "Active"]


def test_illegal_transition_rejected(db_url):
    """Registered 不能直接到 Active（§8.5）→ RegistryError，状态不变。"""
    _register(db_url)
    with session_scope(db_url) as s:
        with pytest.raises(RegistryError):
            registry.transition_operator(
                s, tenant_id=T1, operator_id="op.temp", version="1.0.0",
                target="Active", evidence={"metrics_ok": True}, actor="test")
    with session_scope(db_url) as s:
        row = repo.get_operator_version(s, tenant_id=T1,
                                        operator_id="op.temp", version="1.0.0")
    assert row["state"] == "Registered"


def test_transition_missing_version(db_url):
    with session_scope(db_url) as s:
        with pytest.raises(RegistryError):
            registry.transition_operator(
                s, tenant_id=T1, operator_id="ghost", version="9.9.9",
                target="Shadow", actor="test")


# ---------------------------------------------------------------- Canary 确定性分流（§5.4）

def _canary_row(state="Canary", pct=50, oid="op.temp", created=1.0):
    return {"operator_id": oid, "version": "1.0.0", "state": state,
            "created_at": created, "rollout": {"stage": registry.STAGE_CANARY,
                                               "percent": pct}}


def _stable_row(oid="op.temp", created=0.5):
    return {"operator_id": oid, "version": "0.9.0", "state": "Active",
            "created_at": created, "rollout": {"stage": registry.STAGE_STABLE,
                                               "percent": 0}}


def test_pick_version_deterministic_same_device():
    """同一 (算子, 设备) 反复调用落**同侧**——确定性哈希，否则指标被抖动污染。"""
    cands = [_stable_row(), _canary_row(pct=50)]
    picks = {registry.pick_version(cands, "dev-42")["version"] for _ in range(20)}
    assert len(picks) == 1  # 稳定


def test_pick_version_pct_0_all_stable():
    cands = [_stable_row(), _canary_row(pct=0)]
    for i in range(50):
        assert registry.pick_version(cands, f"dev-{i}")["version"] == "0.9.0"


def test_pick_version_pct_100_all_canary():
    cands = [_stable_row(), _canary_row(pct=100)]
    for i in range(50):
        assert registry.pick_version(cands, f"dev-{i}")["version"] == "1.0.0"


def test_pick_version_pct_50_splits():
    cands = [_stable_row(), _canary_row(pct=50)]
    hits = sum(1 for i in range(400)
               if registry.pick_version(cands, f"dev-{i}")["version"] == "1.0.0")
    assert 100 < hits < 300  # 大致一半，不追求精确（哈希分布）


def test_pick_version_no_canary_returns_stable():
    assert registry.pick_version([_stable_row()], "dev-1")["version"] == "0.9.0"


def test_start_canary_sets_percent(db_url):
    _register(db_url)
    with session_scope(db_url) as s:
        registry.transition_operator(s, tenant_id=T1, operator_id="op.temp",
                                     version="1.0.0", target="Shadow", actor="t")
    with session_scope(db_url) as s:
        row = registry.start_canary(s, tenant_id=T1, operator_id="op.temp",
                                    version="1.0.0", percent=25, actor="t")
    assert row["state"] == "Canary"
    assert row["rollout"]["percent"] == 25
    # 再次调 percent（已在 Canary）→ 只改百分比，不报非法自转
    with session_scope(db_url) as s:
        row2 = registry.start_canary(s, tenant_id=T1, operator_id="op.temp",
                                     version="1.0.0", percent=80, actor="t")
    assert row2["rollout"]["percent"] == 80


def test_start_canary_bad_percent(db_url):
    _register(db_url)
    with session_scope(db_url) as s:
        with pytest.raises(RegistryError):
            registry.start_canary(s, tenant_id=T1, operator_id="op.temp",
                                  version="1.0.0", percent=101)


# ---------------------------------------------------------------- 自动回滚（§5.5）

def test_should_rollback_min_samples_conservative():
    """样本不足 → 不判（冷启动保守），即便指标看起来很糟。"""
    th = CanaryThresholds(min_samples=100)
    verdict, reason = registry.should_rollback(
        {"samples": 10, "crash_rate": 0.9}, {"samples": 1000}, th)
    assert verdict is False
    assert "样本不足" in reason


def test_should_rollback_crash_rate():
    th = CanaryThresholds(min_samples=10)
    verdict, reason = registry.should_rollback(
        {"samples": 100, "crash_rate": 0.5}, {"samples": 100}, th)
    assert verdict is True
    assert "崩溃率" in reason


def test_should_rollback_parse_drop():
    th = CanaryThresholds(min_samples=10)
    verdict, _ = registry.should_rollback(
        {"samples": 100, "crash_rate": 0.0, "parse_success_rate": 0.80},
        {"parse_success_rate": 0.99}, th)
    assert verdict is True


def test_should_rollback_candidate_multiple():
    th = CanaryThresholds(min_samples=10)
    verdict, _ = registry.should_rollback(
        {"samples": 100, "crash_rate": 0.0, "candidate_count": 40},
        {"candidate_count": 10}, th)
    assert verdict is True


def test_should_rollback_healthy_no_rollback():
    th = CanaryThresholds(min_samples=10)
    verdict, reason = registry.should_rollback(
        {"samples": 500, "crash_rate": 0.001, "parse_success_rate": 0.99,
         "candidate_count": 11},
        {"parse_success_rate": 0.99, "candidate_count": 10}, th)
    assert verdict is False
    assert reason == "指标正常"


def test_evaluate_watchdog_auto_rolls_back(db_url):
    """Canary 态越限 → 自动回滚到 RolledBack，并落台账。"""
    _register_all(db_url)
    th = CanaryThresholds(min_samples=10)
    with session_scope(db_url) as s:
        result = registry.evaluate_watchdog(
            s, tenant_id=T1, operator_id="op.temp", version="1.0.0",
            canary_metrics={"samples": 100, "crash_rate": 0.5},
            stable_metrics={"samples": 100}, thresholds=th)
    assert result["rolled_back"] is True
    with session_scope(db_url) as s:
        row = repo.get_operator_version(s, tenant_id=T1,
                                        operator_id="op.temp", version="1.0.0")
    assert row["state"] == "RolledBack"


def test_evaluate_watchdog_only_in_canary(db_url):
    """非 Canary 态：看门狗不生效（不误动 Registered）。"""
    _register(db_url)
    with session_scope(db_url) as s:
        result = registry.evaluate_watchdog(
            s, tenant_id=T1, operator_id="op.temp", version="1.0.0",
            canary_metrics={"samples": 100, "crash_rate": 0.9},
            stable_metrics={})
    assert result["rolled_back"] is False
    with session_scope(db_url) as s:
        row = repo.get_operator_version(s, tenant_id=T1,
                                        operator_id="op.temp", version="1.0.0")
    assert row["state"] == "Registered"


# ---------------------------------------------------------------- 回滚 + 上一签名版本（§6）

def _register_all(db_url):
    """注册 1.0.0，并推入 Canary。"""
    _register(db_url)
    with session_scope(db_url) as s:
        registry.transition_operator(s, tenant_id=T1, operator_id="op.temp",
                                     version="1.0.0", target="Shadow", actor="t")
    with session_scope(db_url) as s:
        registry.start_canary(s, tenant_id=T1, operator_id="op.temp",
                              version="1.0.0", percent=10, actor="t")


def test_rollback_to_previous_signed_version(db_url):
    """回滚取上一签名版本：先注册 0.9.0（带签名），再注册 1.0.0 并回滚 → fallback 指向 0.9.0。"""
    _register(db_url, ver="0.9.0", src="temperature >= 70")
    _register(db_url, ver="1.0.0", src="temperature >= 75")
    with session_scope(db_url) as s:
        reg = registry.transition_operator(s, tenant_id=T1, operator_id="op.temp",
                                           version="1.0.0", target="Shadow", actor="t")
    assert reg["state"] == "Shadow"
    with session_scope(db_url) as s:
        row = registry.rollback_operator(s, tenant_id=T1, operator_id="op.temp",
                                         version="1.0.0", reason="手动回滚", actor="t")
        prev = registry.previous_signed_version(
            s, tenant_id=T1, operator_id="op.temp", version="1.0.0")
    assert row["state"] == "RolledBack"
    assert prev is not None and prev["version"] == "0.9.0"


# ---------------------------------------------------------------- 租户隔离（§5.6）

def test_cross_tenant_isolated(db_url):
    """T1 注册的算子，T2 读不到（None → 路由 404）。"""
    _register(db_url, tenant=T1)
    with session_scope(db_url) as s:
        assert repo.get_operator_version(s, tenant_id=T2, operator_id="op.temp",
                                         version="1.0.0") is None
        assert repo.list_operators(s, tenant_id=T2) == []
        # T2 不能转移 T1 的算子
        with pytest.raises(RegistryError):
            registry.transition_operator(s, tenant_id=T2, operator_id="op.temp",
                                         version="1.0.0", target="Shadow", actor="t")
    # T1 自己仍看得到
    with session_scope(db_url) as s:
        assert repo.get_operator_version(s, tenant_id=T1, operator_id="op.temp",
                                         version="1.0.0") is not None


def test_list_operators_prefers_active(db_url):
    """聚合列表：同一算子多版本，Active 优先返回。"""
    _register(db_url, ver="0.9.0", src="a >= 1")
    _register(db_url, ver="1.0.0", src="a >= 2")
    with session_scope(db_url) as s:
        # 把 0.9.0 推到 Active
        registry.transition_operator(s, tenant_id=T1, operator_id="op.temp",
                                     version="0.9.0", target="Shadow", actor="t")
        registry.transition_operator(s, tenant_id=T1, operator_id="op.temp",
                                     version="0.9.0", target="Canary",
                                     evidence={"metrics_ok": True}, actor="t")
        registry.transition_operator(s, tenant_id=T1, operator_id="op.temp",
                                     version="0.9.0", target="Active",
                                     evidence={"metrics_ok": True}, actor="t")
    with session_scope(db_url) as s:
        ops = repo.list_operators(s, tenant_id=T1)
    assert len(ops) == 1
    assert ops[0]["version"] == "0.9.0"
    assert ops[0]["state"] == "Active"
