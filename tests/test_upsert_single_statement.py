"""写路径 T2：业务 upsert 从"每行 4 条语句"降到**1 条**（`INSERT … ON CONFLICT DO UPDATE`）。

为什么要换（`docs/write_path_batching.md` §7.1 的实测清单）
--------------------------------------------------------
旧写法是"`get` → 命中则 UPDATE；未命中则 `SAVEPOINT` + `INSERT` + `RELEASE`"，
为的是扛"并发两个请求同时 `get` 到 None"这个竞态。**它是正确的**，但每行 4 条语句，
而且 `SAVEPOINT`/`RELEASE` 与 upsert 前的 `get` 合起来占掉每请求约 5 条（实测 12.1 条/请求）。
`ON CONFLICT DO UPDATE` 把同一件事故交给数据库原子完成：**1 条语句，且竞态从"被兜住"
变成"不存在"**。

本文件锁五件事
-------------
1. **每行只发 1 条 SQL**，且**不再出现 `SAVEPOINT`/预查 `SELECT`**（回归守卫，无需压测）；
2. **重复投递仍是更新而不是插重**（幂等语义不变）；
3. **`created_at` 不被刷新**——它是首次写入时间，也是 ④ 保留策略的判据；
   若被反复重投刷新成"刚写入"，那条事件就**永远不会被清理**（保留策略静默失效）；
4. **复合主键仍然成立**：跨租户同 id 保住两行（删掉这条会静默丢租户数据）；
5. **JSON 列经 Core 路径往返无损**（`metrics` / `evidence`）。
"""
from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import event, text

from data_dog.schemas.events import CandidateEvent, EvidenceSummary
from data_dog.storage import repository as repo
from data_dog.storage.db import get_engine, init_db, reset_engine, session_scope

TENANT = "t1"


@pytest.fixture()
def url(tmp_path, monkeypatch):
    u = f"sqlite:///{(tmp_path / 'upsert.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", u)
    reset_engine()
    init_db(u)
    yield u
    reset_engine()


def _telemetry(event_id: str = "e1", **over: Any) -> SimpleNamespace:
    base = dict(event_id=event_id, device_sn="SN1", occurred_at="2026-01-01T00:00:00Z",
                component_type="joint_motor", component_instance="front_left_hip",
                metrics={"temperature": 96.0}, status_codes=["OVERHEAT"],
                operating_condition="trot", operator_id="op@1", operator_version="1",
                binding_revision="rev", raw_log_ref="ref")
    base.update(over)
    return SimpleNamespace(**base)


def _candidate(cid: str = "cand_1", **over: Any) -> CandidateEvent:
    base: dict[str, Any] = dict(
        candidate_id=cid, device_sn="SN1", component_ref="joint_motor.front_left_hip",
        signal="temperature_high", severity_hint="critical",
        window_start="2026-01-01T00:00:00Z", window_end="2026-01-01T00:00:05Z",
        sample_count=3, rule_matches=["MOTOR_TEMP_PROTECT"], anomaly_score=0.9,
        evidence=[EvidenceSummary(key="temperature", from_value=61.0, to_value=96.0,
                                 baseline=61.0, unit=None, note="n")],
        evidence_refs=["metricwin://x"], detector_provenance="op@1")
    base.update(over)
    return CandidateEvent(**base)


def _statements(u: str, fn) -> list[str]:
    """跑 `fn`，把期间发出的所有 SQL 语句按原文收下来。

    监听挂在 `Engine` **类**上，而不是某个 `get_engine()` 返回的实例上：
    会话用的 engine 未必是同一个对象（缓存键/默认 URL 的差别就够），
    挂错对象会**一条都抓不到 → 静默假绿**——那比测试失败更糟。
    """
    from sqlalchemy.engine import Engine

    seen: list[str] = []

    def rec(conn, cursor, statement, params, context, executemany):  # noqa: ANN001
        seen.append(" ".join(statement.split()))

    event.listen(Engine, "before_cursor_execute", rec)
    try:
        fn()
    finally:
        event.remove(Engine, "before_cursor_execute", rec)
    return seen


def _save_telemetry(u: str, events: list[Any]) -> None:
    with session_scope(tenant_id=TENANT) as s:
        repo.save_telemetry(s, events, tenant_id=TENANT)


def _save_candidates(u: str, cands: list[Any]) -> None:
    with session_scope(tenant_id=TENANT) as s:
        repo.save_candidates(s, cands, tenant_id=TENANT)


def _scalar(u: str, sql: str) -> Any:
    eng = get_engine(u)
    with eng.connect() as c:
        return c.execute(text(sql)).scalar()


# ---------------------------------------------------------------- 1) 语句数

def test_telemetry_row_costs_one_statement(url):
    """一行遥测 = **1 条 SQL**，且不再有 SAVEPOINT / upsert 前的预查 SELECT。"""
    stmts = _statements(url, lambda: _save_telemetry(url, [_telemetry()]))

    inserts = [s for s in stmts if s.upper().startswith("INSERT")]
    assert len(inserts) == 1, f"应只发 1 条 INSERT，实际 {len(inserts)}：{stmts}"
    assert not [s for s in stmts if s.upper().startswith("SAVEPOINT")], \
        f"仍有 SAVEPOINT（旧路径的每行开销）：{stmts}"
    assert not [s for s in stmts if s.upper().startswith("SELECT")], \
        f"仍有 upsert 前的预查 SELECT：{stmts}"


def test_candidate_row_costs_one_statement(url):
    stmts = _statements(url, lambda: _save_candidates(url, [_candidate()]))

    inserts = [s for s in stmts if s.upper().startswith("INSERT")]
    assert len(inserts) == 1, f"应只发 1 条 INSERT，实际 {len(inserts)}：{stmts}"
    assert not [s for s in stmts if s.upper().startswith("SAVEPOINT")], stmts
    assert not [s for s in stmts if s.upper().startswith("SELECT")], stmts


def test_resave_is_still_one_statement(url):
    """**重复投递**（命中 ON CONFLICT 分支）也必须只 1 条——旧路径这时是 2 条（get+UPDATE）。"""
    _save_telemetry(url, [_telemetry()])
    stmts = _statements(url, lambda: _save_telemetry(url, [_telemetry()]))

    assert len([s for s in stmts if s.upper().startswith("INSERT")]) == 1, stmts
    assert not [s for s in stmts if s.upper().startswith("SELECT")], stmts


# ---------------------------------------------------------------- 2) 幂等

def test_resave_updates_instead_of_duplicating(url):
    _save_telemetry(url, [_telemetry()])
    _save_telemetry(url, [_telemetry(metrics={"temperature": 20.0})])

    assert _scalar(url, "SELECT COUNT(*) FROM telemetry_events") == 1
    assert _scalar(url, "SELECT metrics FROM telemetry_events") .find("20") > 0


def test_three_saves_still_one_row(url):
    for _ in range(3):
        _save_candidates(url, [_candidate()])
    assert _scalar(url, "SELECT COUNT(*) FROM candidates") == 1


# ---------------------------------------------------------------- 3) created_at

def test_created_at_is_not_refreshed_on_resave(url):
    """**这条守的是 ④ 的保留策略**：`created_at` 是首次写入时间。

    若重复投递把它刷新成"刚写入"，一条被反复重投的老事件就永远不会被清理
    ——保留策略会**静默失效**（没有报错，只是数据永远不消失）。
    """
    _save_telemetry(url, [_telemetry()])
    original = _scalar(url, "SELECT created_at FROM telemetry_events")

    _save_telemetry(url, [_telemetry(metrics={"temperature": 1.0})])  # 再投一次

    assert _scalar(url, "SELECT created_at FROM telemetry_events") == original, \
        "created_at 被重复投递刷新了——保留策略会因此静默失效"


def test_candidate_created_at_is_not_refreshed(url):
    _save_candidates(url, [_candidate()])
    original = _scalar(url, "SELECT created_at FROM candidates")
    _save_candidates(url, [_candidate(sample_count=9)])
    assert _scalar(url, "SELECT created_at FROM candidates") == original


# ---------------------------------------------------------------- 4) 复合主键

def test_same_id_across_tenants_keeps_two_rows(url):
    """复合主键 (tenant_id, id)：跨租户同名 id 必须各存一行（否则静默丢数据）。"""
    for t in ("t1", "t2"):
        with session_scope(tenant_id=t) as s:
            repo.save_candidates(s, [_candidate()], tenant_id=t)
    assert _scalar(url, "SELECT COUNT(*) FROM candidates") == 2


# ---------------------------------------------------------------- 5) JSON 往返

def test_json_columns_round_trip_through_core_upsert(url):
    """JSON 列经 Core upsert 路径往返无损（`evidence` 是 dict 列表，`metrics` 是 dict）。"""
    _save_candidates(url, [_candidate()])
    raw_ev = _scalar(url, "SELECT evidence FROM candidates")
    assert "temperature" in raw_ev and "96" in raw_ev, raw_ev

    _save_telemetry(url, [_telemetry()])
    raw_m = _scalar(url, "SELECT metrics FROM telemetry_events")
    assert "temperature" in raw_m and "96" in raw_m, raw_m


def test_component_ref_is_composed_from_type_and_instance(url):
    """`component_ref` 由 `component_type`+`component_instance` 组装（值构造器唯一来源）。"""
    _save_telemetry(url, [_telemetry()])
    assert _scalar(url, "SELECT component_ref FROM telemetry_events") == \
        "joint_motor.front_left_hip"


# ---------------------------------------------------------------- 6) 语句缓存

def test_upsert_statement_is_built_once_and_reused(url):
    """语句结构只随表变化 ⇒ 必须**缓存复用**，不能每行重建。

    实测（`write_path_batching.md` §9）：每请求只执行 2 条语句，却有 **28 次**
    SQLAlchemy schema 对象构造；缓存后 HTTP+落库 CPU 从 **7.76 → 6.51 ms/请求（−16%）**。
    """
    from data_dog.storage import repository as r

    r._UPSERT_STMT_CACHE.clear()
    a = r._upsert_stmt(r.M.TelemetryEventRow, "sqlite")
    b = r._upsert_stmt(r.M.TelemetryEventRow, "sqlite")

    assert a is not None
    assert a is b, "第二次调用没有命中缓存——又回到每行重建 Core 语句"
    assert ("sqlite", "telemetry_events") in r._UPSERT_STMT_CACHE


def test_cached_statement_targets_every_updatable_column():
    """更新列**从表结构推导**：除主键与 `created_at` 外**一个都不能漏**。

    漏一列的表现是"重复投递时那一列永远不更新"——静默、且很难发现。
    这条把"表结构"与"更新集"钉在一起，将来加列也不会漏。
    """
    from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect

    from data_dog.storage import repository as r

    for model in (r.M.TelemetryEventRow, r.M.CandidateRow):
        stmt = r._upsert_stmt(model, "sqlite")
        compiled = str(stmt.compile(dialect=sqlite_dialect()))
        pk = {c.name for c in model.__table__.primary_key.columns}
        expected = {c.name for c in model.__table__.columns} - pk - {"created_at"}

        assert "DO UPDATE SET" in compiled, compiled
        set_cols = set(re.findall(r"(\w+)\s*=\s*excluded\.", compiled))
        assert set_cols == expected, (
            f"{model.__tablename__} 的更新集与表结构不一致\n"
            f"  漏: {sorted(expected - set_cols)}\n  多: {sorted(set_cols - expected)}")
        assert "created_at" not in set_cols, (
            "created_at 进了更新集——重复投递会刷新首次写入时间，④ 的保留策略会静默失效")
