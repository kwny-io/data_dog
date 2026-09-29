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


# ---------------------------------------------------------------- RLS（数据库强制隔离）

_APP_PW = "dd_rls_pw"


def _schema_of(url: str) -> str:
    """从 fixture 生成的连接串里取出隔离 schema 名。"""
    import re
    m = re.search(r"search_path%3D([^&%]+)", url)
    assert m, "连接串未带 search_path，RLS 测试无法定位 schema"
    return m.group(1)


@pytest.fixture()
def app_role(pg_url):
    """建一个**非属主、非 superuser** 的应用角色，测试结束彻底清理。

    RLS 只对"非属主且非 superuser"的角色生效（属主被 FORCE 约束，但 superuser
    永远绕过）。故测试必须用这样一个角色连接，否则断言会"假绿"。
    角色是集群级对象，故用随机名 + `DROP OWNED BY` 清理，避免与并发测试互踩。
    """
    role = f"dd_rls_{uuid.uuid4().hex[:8]}"
    schema = _schema_of(pg_url)
    admin = create_engine(_PG_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE ROLE "{role}" LOGIN PASSWORD \'{_APP_PW}\''))
        c.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"'))
    try:
        yield role
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP OWNED BY "{role}"'))      # 回收本库里授它的全部权限
            c.execute(text(f'DROP ROLE "{role}"'))
        admin.dispose()


def _grant_app_role(pg_url: str, role: str) -> None:
    """把测试 schema 内已建好的表/序列授权给应用角色（须在迁移**之后**调）。"""
    schema = _schema_of(pg_url)
    admin = create_engine(_PG_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES '
                       f'IN SCHEMA "{schema}" TO "{role}"'))
        c.execute(text(f'GRANT USAGE, SELECT ON ALL SEQUENCES '
                       f'IN SCHEMA "{schema}" TO "{role}"'))
    admin.dispose()


def _as_role(url: str, role: str) -> str:
    """把连接串的用户换成应用角色（保留 host/db/search_path）。

    原串可能带密码也可能不带（如 trust 认证的 `postgres@host`）；密码只在
    原串有的时候才补——否则会拼出非法的 `role:@host`。
    """
    import re
    m = re.match(r"(postgresql\+psycopg://)([^:@/]+)(?::([^@]*))?@(.+)", url)
    assert m, "连接串格式不支持切换角色"
    prefix, _user, orig_pw, hostdb = m.groups()
    creds = f"{role}:{_APP_PW}" if orig_pw is not None else role
    return f"{prefix}{creds}@{hostdb}"


def test_rls_blocks_cross_tenant_even_without_app_filter(pg_url, app_role):
    """**核心 RLS 断言**：绕开应用层过滤，数据库仍挡住跨租户读。

    这是 RLS 存在的全部理由。应用层过滤（仓储层的 `WHERE tenant_id`）此前是
    唯一防线——一条漏写的 SQL 就能读到别人的数据。本测试**故意**发不带
    `WHERE tenant_id` 的裸 SQL，模拟那个 bug：

    - 设了租户上下文 → 数据库只放行该租户的行。
    - 上下文缺失 → 零行（`current_setting(..., true)` 为 NULL，谓词非真 → 失败关闭）。
    """
    from alembic import command
    from data_dog.storage import migrations
    from data_dog.storage.db import session_scope as scope

    # 建表 + 策略（属主/superuser 下跑），再授权给应用角色
    command.upgrade(migrations.make_config(pg_url), "head")
    _grant_app_role(pg_url, app_role)
    app_url = _as_role(pg_url, app_role)

    # 以应用角色、带正确租户上下文写入两个租户同名候选
    with scope(app_url, tenant_id="t1") as s:
        repo.save_candidates(s, [_cand("c1")], tenant_id="t1")
    with scope(app_url, tenant_id="t2") as s:
        repo.save_candidates(s, [_cand("c1", device="dev-2")], tenant_id="t2")

    # 裸 SQL：**故意不带** WHERE tenant_id
    with scope(app_url, tenant_id="t1") as s:
        rows = s.execute(text("SELECT device_sn FROM candidates")).fetchall()
    assert [r[0] for r in rows] == ["dev-1"], \
        "RLS 未生效：t1 上下文下裸 SQL 读到了 t2 的行"

    with scope(app_url, tenant_id="t2") as s:
        rows = s.execute(text("SELECT device_sn FROM candidates")).fetchall()
    assert [r[0] for r in rows] == ["dev-2"]

    # 不设上下文 → 应零行（失败关闭：忘设租户 = 什么都读不到，而非读到全部）
    with scope(app_url) as s:
        rows = s.execute(text("SELECT device_sn FROM candidates")).fetchall()
    assert rows == [], "未设租户上下文却读到了行（RLS 谓词未按预期失败关闭）"


def test_rls_blocks_cross_tenant_write(pg_url, app_role):
    """`WITH CHECK` 挡住**写入他人租户**的行（读隔离之外的写侧防线）。"""
    from alembic import command
    from data_dog.storage import migrations
    from data_dog.storage.db import session_scope as scope

    command.upgrade(migrations.make_config(pg_url), "head")
    _grant_app_role(pg_url, app_role)
    app_url = _as_role(pg_url, app_role)

    with pytest.raises(Exception) as ei:
        # 上下文设为 t1，仓储层却要写 tenant_id=t2 的行 → WITH CHECK 应拒绝
        with scope(app_url, tenant_id="t1") as s:
            repo.save_candidates(s, [_cand("c_x", device="dev-x")], tenant_id="t2")
    msg = str(ei.value).lower()
    assert "row-level security" in msg or "violates" in msg, \
        f"跨租户写入未被 RLS 拒绝，而是：{ei.value!r}"
