"""PostgreSQL 集成测试（默认跳过）。

为什么需要它
------------
`test_storage.py::test_database_url_env_switch` 只断言**连接串字符串**，
从不真的连库——所以 CI 里光起一个 PG service 是**形同虚设**的：没有任何测试
真正跑在 PG 上。本文件补上真正的 PG 覆盖：迁移、往返、**租户隔离**都在真库上验证。

运行条件
--------
设 `DD_TEST_PG_URL`（如 `postgresql+psycopg://u:p@localhost:5432/testdb`）才跑；
否则整体 skip。CI 的 pg job 提供该变量。需装 `.[postgres]` 驱动。

隔离方式
--------
每个测试用**独立 schema**（`pgtest_<随机>`），跑完 drop schema cascade，
避免污染、也避免测试间互相干扰。比 drop 整个库更轻、可并行。
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from data_dog.schemas.events import CandidateEvent, EvidenceSummary
from data_dog.storage import repository as repo
from data_dog.storage.db import session_scope

_PG_URL = os.environ.get("DD_TEST_PG_URL", "").strip()

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not _PG_URL, reason="未设 DD_TEST_PG_URL，跳过 PG 集成测试"),
]


@pytest.fixture()
def pg_url():
    """建一个隔离 schema，返回把它设为默认 search_path 的连接串。"""
    schema = f"pgtest_{uuid.uuid4().hex[:12]}"
    admin = create_engine(_PG_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    # 连接串带上 options 指定 search_path，让所有表落在该 schema
    sep = "&" if "?" in _PG_URL else "?"
    url = f"{_PG_URL}{sep}options=-csearch_path%3D{schema}"
    try:
        yield url
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def _cand(cid="cand_1", device="dev-1"):
    return CandidateEvent(
        candidate_id=cid, device_sn=device, component_ref="joint_motor.front_left_hip",
        signal="temperature_high", severity_hint="critical",
        window_start="2026-09-13T10:00:00Z", window_end="2026-09-13T10:00:04Z",
        sample_count=3, rule_matches=["MOTOR_TEMP_PROTECT"], anomaly_score=1.0,
        evidence=[EvidenceSummary(key="temperature", from_value=40, to_value=95)],
        evidence_refs=["metricwin://x"], detector_provenance="d@1.0")


def _alembic_cfg(url: str):
    from alembic import command
    from data_dog.storage import migrations
    return command, migrations.make_config(url)


def test_alembic_upgrade_head_on_pg(pg_url):
    """迁移能在真实 PG 上跑到 head，且主键为复合（含 tenant_id）。"""
    command, cfg = _alembic_cfg(pg_url)
    command.upgrade(cfg, "head")

    eng = create_engine(pg_url)
    with eng.connect() as c:
        cols = c.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='candidates' AND column_name='tenant_id'"
        )).fetchall()
    eng.dispose()
    assert cols, "PG 上 candidates 缺 tenant_id 列"


def test_candidate_roundtrip_and_tenant_isolation_on_pg(pg_url):
    """真库往返 + 租户隔离读（PG 上同样只读得到本租户）。"""
    from data_dog.storage.db import init_db
    init_db(pg_url)

    with session_scope(pg_url) as s:
        repo.save_candidates(s, [_cand("c1")], tenant_id="t1")
        repo.save_candidates(s, [_cand("c1", device="dev-2")], tenant_id="t2")

    with session_scope(pg_url) as s:
        r1 = repo.get_candidate(s, "c1", tenant_id="t1")
        r2 = repo.get_candidate(s, "c1", tenant_id="t2")
        assert repo.get_candidate(s, "c1", tenant_id="t3") is None
    assert r1["device_sn"] == "dev-1"
    assert r2["device_sn"] == "dev-2", "PG 上两租户同名 id 互相覆盖了"

    with session_scope(pg_url) as s:
        assert len(repo.list_candidates(s, tenant_id="t1")) == 1
        assert repo.list_candidates(s, tenant_id="t3") == []
