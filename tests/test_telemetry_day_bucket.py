"""摄取日桶（A9）：同日重投递仍幂等、跨天多一行、created_day 与 created_at 同源。

背景：`telemetry_events` 主键加了 `created_day`（= floor(created_at/86400)）才能按时间分区
（PG 要求分区表的唯一约束包含分区键，见 docs/storage.md §8.2）。这个改动**改变了幂等语义的边界**：
同日重投递（绝大多数重试）仍然去重，**跨天重投递会多一行**。本文件把**两边**都钉死——尤其是
那个代价本身："声明 == 事实"要求已知代价写成可执行的断言，而不是只写在文档里。
"""
from __future__ import annotations

import pytest

from data_dog.storage import models as M
from data_dog.storage import repository as R
from data_dog.storage.db import init_db, reset_engine, session_scope

_DAY = 86400.0
_T1 = 1_800_036_000.0            # 固定基准：日桶 20833 的 18:00（桶内偏后，能区分 floor 与四舍五入）
_T1_NEXT_DAY = _T1 + _DAY


@pytest.fixture()
def db(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'day_bucket.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", url)
    reset_engine()
    init_db(url)
    yield url
    reset_engine()


def _event(eid: str = "ev1") -> dict:
    return {
        "event_id": eid, "device_sn": "SN1", "occurred_at": "2026-10-14T00:00:00Z",
        "component_type": "joint_motor", "component_instance": "front_left_hip",
        "metrics": {"temperature_c": 61.0}, "status_codes": ["ok"],
    }


def _save(url: str, events: list, now: float) -> int:
    with session_scope(url) as s:
        return R.save_telemetry(s, events, tenant_id="t1", now=now)


def _rows(url: str) -> list:
    with session_scope(url) as s:
        return [(r.event_id, r.created_day, r.created_at)
                for r in s.query(M.TelemetryEventRow).order_by(M.TelemetryEventRow.created_at)]


def test_created_day_is_floor_of_created_at_and_same_instant(db):
    """created_day 与 created_at **同一时刻**算出（否则跨午夜会差一天）。"""
    _save(db, [_event()], _T1)
    eid, day, ts = _rows(db)[0]
    assert day == M.day_of(_T1) == 20833
    assert ts == _T1


def test_same_day_redelivery_stays_idempotent_and_keeps_first_write_time(db):
    """同日重投递：仍 1 行，且 created_at 保留**首次写入**时间（保留策略的判据）。"""
    _save(db, [_event()], _T1)
    _save(db, [_event()], _T1 + 3600.0)
    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0][2] == _T1


def test_cross_day_redelivery_creates_a_second_row(db):
    """**已知代价**：跨天重投递多一行（日桶不同 → 主键不同）。检测见 docs/storage.md §8.2。"""
    _save(db, [_event()], _T1)
    _save(db, [_event()], _T1_NEXT_DAY)
    rows = _rows(db)
    assert len(rows) == 2
    assert [r[1] for r in rows] == [M.day_of(_T1), M.day_of(_T1_NEXT_DAY)]


def test_same_event_across_tenants_does_not_collide(db):
    """tenant_id 仍在主键里：两租户同 event_id / 同日桶互不覆盖。"""
    with session_scope(db) as s:
        R.save_telemetry(s, [_event()], tenant_id="t1", now=_T1)
    with session_scope(db) as s:
        R.save_telemetry(s, [_event()], tenant_id="t2", now=_T1)
    with session_scope(db) as s:
        assert s.query(M.TelemetryEventRow).count() == 2


def test_cross_day_duplicates_are_detectable(db):
    """检测函数能**找出**跨天重复——否则"已知代价"就没人看得见。"""
    _save(db, [_event()], _T1)
    with session_scope(db) as s:
        assert R.count_cross_day_duplicates(s) == []          # 同日重投递没重复
    _save(db, [_event()], _T1_NEXT_DAY)
    with session_scope(db) as s:
        assert R.count_cross_day_duplicates(s) == [("t1", "ev1", 2)]
        assert R.count_cross_day_duplicates(s, tenant_id="t2") == []
        assert R.count_cross_day_duplicates(s, since=_T1_NEXT_DAY) == []   # 只看次日窗口


def test_batch_is_upserted_per_event_with_its_own_instant(db):
    """一批多事件：各自带自己的 created_day（不是整批一个时间戳）。"""
    _save(db, [_event("ev1"), _event("ev2")], _T1)
    assert [r[0] for r in _rows(db)] == ["ev1", "ev2"]
    assert all(r[1] == M.day_of(_T1) for r in _rows(db))