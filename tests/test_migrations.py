"""数据库迁移测试（alembic）。

覆盖：
  1. 空库 upgrade head → 六张表都带 tenant_id，且改主键的四表主键含 tenant_id。
  2. **在已填充的库上跑迁移**——守住 SQLite 重建表的拷贝路径（防丢行）。
  3. 回填语义：既有行归属 default tenant；telemetry 的原值被保留。
  4. downgrade 还原为单列主键。
  5. 降级遇到跨租户同主键 → 明确报错（失败关闭，不静默丢行）。

用真实临时文件 SQLite（不是 :memory:）——迁移是文件级操作。
"""
from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine, inspect

from alembic import command
from data_dog.storage import migrations

_HEAD = "head"
_BASELINE = "a06f16a9eff1"


def _upgrade(url: str, rev: str) -> None:
    command.upgrade(migrations.make_config(url), rev)


def _downgrade(url: str, rev: str) -> None:
    command.downgrade(migrations.make_config(url), rev)

# 改主键的表 → 期望的复合主键列（顺序即主键顺序）
_COMPOSITE_PKS = {
    "telemetry_events": ["tenant_id", "event_id"],
    "candidates": ["tenant_id", "candidate_id"],
    "reports": ["tenant_id", "investigation_id"],
    "alerts": ["tenant_id", "event_id"],
    "alert_outbox": ["tenant_id", "idempotency_key", "revision"],
}


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "mig.db"


def _url(db_path) -> str:
    return f"sqlite:///{db_path.as_posix()}"


def _engine(db_path):
    return create_engine(_url(db_path))


def _pk_cols(engine, table) -> list:
    return inspect(engine).get_pk_constraint(table)["constrained_columns"]


# ---------------------------------------------------------------- 空库

def test_upgrade_head_creates_tenant_columns(db_path, monkeypatch):
    """空库 upgrade head：六表都有 tenant_id，改主键的表主键含 tenant_id。"""
    monkeypatch.setenv("DD_DATABASE_URL", _url(db_path))
    _upgrade(_url(db_path), _HEAD)

    eng = _engine(db_path)
    insp = inspect(eng)
    for table in ("telemetry_events", "candidates", "reports", "alerts",
                  "alert_outbox", "alert_feedback"):
        cols = {c["name"] for c in insp.get_columns(table)}
        assert "tenant_id" in cols, f"{table} 缺 tenant_id"

    for table, expected_pk in _COMPOSITE_PKS.items():
        assert _pk_cols(eng, table) == expected_pk, f"{table} 主键不对"
    # 反馈保留代理主键
    assert _pk_cols(eng, "alert_feedback") == ["id"]
    eng.dispose()


# ---------------------------------------------------------------- 已填充库

def _seed_baseline(db_path, monkeypatch):
    """建到 baseline，插入各表一行，返回各行数据。"""
    monkeypatch.setenv("DD_DATABASE_URL", _url(db_path))
    _upgrade(_url(db_path), _BASELINE)
    con = sqlite3.connect(db_path)
    con.execute("INSERT INTO candidates VALUES "
                "('cand_x','SN1','leg.hip','temp','critical','','','0',"
                "'[]',1.0,'[]','[]','d@1',1.0)")
    con.execute("INSERT INTO reports VALUES "
                "('inv_x','confirmed','high',0.9,'e','s','[]','[]','[]','[]',"
                "NULL,'{}',1.0)")
    con.execute("INSERT INTO alerts VALUES "
                "('alert_x','k1','SN1','leg.hip','motor_thermal','detected',"
                "'warning',0.5,'',1,'s','[]','','', '[]',1.0,1.0)")
    con.execute("INSERT INTO alert_outbox VALUES ('k1',1,'{}','pending',0,'','',1.0)")
    con.execute("INSERT INTO telemetry_events VALUES "
                "('ev1','T9','SN1','2026-01-01','leg.hip','{}','[]','','','','','',1.0)")
    con.commit()
    con.close()


def test_migrate_populated_db_preserves_rows(db_path, monkeypatch):
    """在已填充的库上迁移：不丢行，且租户按约定回填。"""
    _seed_baseline(db_path, monkeypatch)
    _upgrade(_url(db_path), _HEAD)

    con = sqlite3.connect(db_path)
    for table in ("candidates", "reports", "alerts", "alert_outbox",
                  "telemetry_events"):
        n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert n == 1, f"{table} 迁移后行数变了（应为 1，实际 {n}）"

    # 新加列的表：既有行回填为 default tenant
    assert con.execute("SELECT tenant_id FROM candidates").fetchone()[0] == "demo"
    assert con.execute("SELECT tenant_id FROM reports").fetchone()[0] == "demo"
    # telemetry 的 tenant_id 是既有列 → 原值 T9 必须被保留，不能覆盖成 demo
    assert con.execute("SELECT tenant_id FROM telemetry_events").fetchone()[0] == "T9"
    con.close()


def test_composite_pk_allows_same_id_across_tenants(db_path, monkeypatch):
    """复合主键的核心保证：两租户可持同一 candidate_id，互不覆盖。"""
    _seed_baseline(db_path, monkeypatch)
    _upgrade(_url(db_path), _HEAD)

    con = sqlite3.connect(db_path)
    # baseline 行已回填为 demo；再插一条 t2 的同名 candidate_id
    con.execute("INSERT INTO candidates VALUES "
                "('t2','cand_x','SN2','leg.hip','temp','info','','','0',"
                "'[]',0.0,'[]','[]','d@2',2.0)")
    con.commit()
    rows = con.execute(
        "SELECT tenant_id, candidate_id FROM candidates ORDER BY tenant_id"
    ).fetchall()
    con.close()
    assert rows == [("demo", "cand_x"), ("t2", "cand_x")]


# ---------------------------------------------------------------- downgrade

def test_downgrade_restores_single_column_pk(db_path, monkeypatch):
    monkeypatch.setenv("DD_DATABASE_URL", _url(db_path))
    _upgrade(_url(db_path), _HEAD)
    _downgrade(_url(db_path), _BASELINE)

    eng = _engine(db_path)
    assert _pk_cols(eng, "candidates") == ["candidate_id"]
    assert _pk_cols(eng, "telemetry_events") == ["event_id"]
    # telemetry 的 tenant_id 列保留（baseline 即有）
    tcols = {c["name"] for c in inspect(eng).get_columns("telemetry_events")}
    assert "tenant_id" in tcols
    # candidates 的 tenant_id 列被移除
    ccols = {c["name"] for c in inspect(eng).get_columns("candidates")}
    assert "tenant_id" not in ccols
    eng.dispose()


def test_downgrade_fails_closed_on_tenant_collision(db_path, monkeypatch):
    """跨租户同主键时降级必须明确报错，而不是静默丢行/裸 IntegrityError。"""
    monkeypatch.setenv("DD_DATABASE_URL", _url(db_path))
    _upgrade(_url(db_path), _HEAD)
    con = sqlite3.connect(db_path)
    for t in ("demo", "t2"):
        con.execute("INSERT INTO candidates VALUES "
                    f"('{t}','cand_x','SN','leg.hip','temp','info','','','0',"
                    "'[]',0.0,'[]','[]','d',1.0)")
    con.commit()
    con.close()

    with pytest.raises(Exception) as ei:
        _downgrade(_url(db_path), _BASELINE)
    assert "无法降级" in str(ei.value) or "duplicate" in str(ei.value).lower()


# ---------------------------------------------------------------- 命令行入口（K8s Job 用）

def test_cli_upgrade_is_idempotent(db_path, monkeypatch):
    """`python -m data_dog.storage.migrations upgrade` 幂等：连跑两次都成功。"""
    monkeypatch.setenv("DD_DATABASE_URL", _url(db_path))
    assert migrations._main(["upgrade"]) == 0
    assert migrations.current_revision(_url(db_path)) == "a2b3c4d5e6f7"
    assert migrations._main(["upgrade"]) == 0           # 再跑一次不报错


def test_cli_current_and_unknown(db_path, monkeypatch, capsys):
    monkeypatch.setenv("DD_DATABASE_URL", _url(db_path))
    migrations._main(["upgrade"])
    capsys.readouterr()                                 # 丢掉 upgrade 的输出
    assert migrations._main(["current"]) == 0
    assert capsys.readouterr().out.strip() == "a2b3c4d5e6f7"
    # 未知子命令 → 退出码 2（非 0，脚本能据此判失败）
    assert migrations._main(["bogus"]) == 2


def test_dockerfile_ships_migration_assets():
    """镜像必须带 alembic.ini + alembic/——否则容器内既跑不了迁移 Job，
    也做不了启动时的 DD_AUTO_MIGRATE（project_root 找不到 alembic.ini）。
    这曾在 compose 上真实翻车：启动静默记'迁移失败'→ /readyz 永远未就绪。"""
    dockerfile = migrations.project_root() / "Dockerfile"
    text = dockerfile.read_text(encoding="utf-8")
    assert "alembic.ini" in text, "Dockerfile 未 COPY alembic.ini"
    assert "COPY alembic " in text or "COPY alembic/" in text, \
        "Dockerfile 未 COPY alembic/（迁移脚本目录）"
