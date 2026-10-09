"""可靠性加固的回归测试：PG 三段超时 + 变更源失败关闭。

为什么单独一个文件
------------------
这两条来自 `docs/reliability.md` 的审计清单（A=高、F=中），改动都落在**失败语义**上：
数据库等不到时必须快速报错、变更依赖不可用时必须让 Agent 拿不到"看似权威"的数据。
失败语义最怕"改回去也没人发现"，故每条都用**可断言的行为**钉住：

1. **PG 超时**：断言 URL 方言 → `create_engine` kwargs 与运行时引擎都带上了
   `connect_timeout` / `pool_timeout` / `statement_timeout`；同时断言 **SQLite 的
   kwargs 逐字节不变**（本地/测试行为不得被牵连）。
   不需要真 PG：`create_engine` 惰性建连，只检查配置面（dialect / pool / connect_args）。
2. **变更源失败关闭**：断言来源抛错时返回的是 `unavailable` **统一错误**（没有
   `changes` 字段），且经工具层透传后 Agent 仍拿不到变更列表——即"查不到"不会被
   伪装成"没有变更"；只有显式打开 `DD_CHANGE_FALLBACK_DEMO=1` 才回退样例表。

正常路径（来源可用、越权拒绝）也在本文件里回归一条，防止"为了失败关闭把成功路径
也关掉了"。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from data_dog.evidence.change_source import ChangeRecord, MockChangeSource
from data_dog.service.evidence import RepoEvidenceBridge
from data_dog.storage import db
from data_dog.tools.investigation import InvestigationTools

# 假 PG URL：`create_engine` 惰性建连，本文件**不会**真的连上去（无网络依赖）。
_PG_URL = "postgresql+psycopg://user:pw@db.example:5432/data_dog"
_COMPONENT = "joint_motor.front_left_hip"


class _BoomSource:
    """变更来源替身：任何查询都抛错（模拟 CMDB 挂掉 / 文件不可读 / 库连不上）。"""

    def query(self, **_kw: Any) -> list[Any]:
        raise ConnectionError("cmdb down")


class _AbortConnect(Exception):
    """在 `do_connect` 事件里抛出：截获 libpq 参数后**中止建连**（无网络依赖）。"""


def _bridge(source: Any) -> RepoEvidenceBridge:
    return RepoEvidenceBridge([], device_sn="dev-1", change_source=source)


# ================================================================ 1. PG 三段超时

def test_pg_engine_kwargs_carry_three_timeouts():
    """PG URL → connect/pool/statement 三段等待全部有上限（且非库默认的无限/30s）。"""
    kwargs = db.engine_kwargs(_PG_URL)
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["pool_timeout"] == 5.0, "拿连接必须有上限（库默认 30s）"
    assert kwargs["connect_args"]["connect_timeout"] == 5.0, "建连必须有限（libpq 默认无限）"
    assert "statement_timeout=15000" in kwargs["connect_args"]["options"]
    print("PASS test_pg_engine_kwargs_carry_three_timeouts")


def test_pg_engine_kwargs_follow_settings_overrides(monkeypatch: pytest.MonkeyPatch):
    """三个值都从 settings 读：运维可用 DD_DB_* 调整，不必改代码。"""
    monkeypatch.setenv("DD_DB_CONNECT_TIMEOUT_S", "1.5")
    monkeypatch.setenv("DD_DB_POOL_TIMEOUT_S", "2.5")
    monkeypatch.setenv("DD_DB_STATEMENT_TIMEOUT_MS", "250")
    kwargs = db.engine_kwargs(_PG_URL)
    assert kwargs["connect_args"]["connect_timeout"] == 1.5
    assert kwargs["pool_timeout"] == 2.5
    assert "statement_timeout=250" in kwargs["connect_args"]["options"]
    print("PASS test_pg_engine_kwargs_follow_settings_overrides")


def test_pg_engine_kwargs_omit_statement_timeout_when_zero(monkeypatch: pytest.MonkeyPatch):
    """`DD_DB_STATEMENT_TIMEOUT_MS=0` = 不限语句时长 → 不塞 options（其余两项仍在）。

    这条守的是"显式关闭"与"忘了配"的区别：0 是一个可审计的选择，不是静默缺省。
    """
    monkeypatch.setenv("DD_DB_STATEMENT_TIMEOUT_MS", "0")
    kwargs = db.engine_kwargs(_PG_URL)
    assert "options" not in kwargs["connect_args"]
    assert kwargs["connect_args"]["connect_timeout"] == 5.0
    assert kwargs["pool_timeout"] == 5.0
    print("PASS test_pg_engine_kwargs_omit_statement_timeout_when_zero")


def test_pg_engine_kwargs_merge_existing_url_options(monkeypatch: pytest.MonkeyPatch):
    """URL 里已有的 `?options=...` 必须**保留**并与 statement_timeout 合并（不静默覆盖）。

    `create_engine(connect_args=...)` 的优先级高于 URL 查询串：若这里直接写单值，
    运维在连接串里传的 `-csearch_path=...` 会被悄悄丢掉——正是本仓最忌讳的
    "没报错但行为变了"。
    """
    monkeypatch.delenv("DD_DB_STATEMENT_TIMEOUT_MS", raising=False)
    url = _PG_URL + "?options=-csearch_path%3Dapp"
    opts = db.engine_kwargs(url)["connect_args"]["options"]
    assert "-csearch_path=app" in opts
    assert "-c statement_timeout=15000" in opts
    print("PASS test_pg_engine_kwargs_merge_existing_url_options")


def test_sqlite_engine_kwargs_unchanged_by_pg_hardening():
    """SQLite 路径**不受影响**：kwargs 只有既有的 check_same_thread，无 PG 专有参数。"""
    kwargs = db.engine_kwargs("sqlite:///local.db")
    assert kwargs == {"future": True,
                      "connect_args": {"check_same_thread": False}}, kwargs
    print("PASS test_sqlite_engine_kwargs_unchanged_by_pg_hardening")


def test_sqlite_engine_keeps_default_pool_timeout(tmp_path: Path):
    """真建一个 SQLite 引擎：池超时仍是 SQLAlchemy 默认（未被 5s 波及）。"""
    eng = db.get_engine(f"sqlite:///{(tmp_path / 'x.db').as_posix()}")
    try:
        assert eng.url.get_backend_name() == "sqlite"
        assert eng.dialect.create_connect_args(eng.url)[1]["check_same_thread"] is False
        assert eng.pool._timeout == 30.0, "SQLite 不该套用 PG 的 5s 池超时"
    finally:
        eng.dispose()
    print("PASS test_sqlite_engine_keeps_default_pool_timeout")


def test_pg_engine_runtime_config_matches_kwargs():
    """真建一个 PG 引擎（**不连库**）：dialect / 池超时 / 交给 libpq 的参数都对得上。

    用 SQLAlchemy 的 `do_connect` 事件截获"最终交给 DBAPI 的参数"（建连前的最后一站），
    随即抛异常中止连接。断言的是"libpq 确实会收到 connect_timeout / statement_timeout"，
    而不只是"kwargs 字典里有"——两者之间正是容易漂移的地方。
    """
    pytest.importorskip("psycopg")               # libpq 驱动（pyproject 的 postgres extra）
    from sqlalchemy import event
    # 带 `?options=` 的 URL：同时验证"URL 的 options 不被吞掉"（见 kwargs 层那条用例）
    eng = db.get_engine(_PG_URL + "?options=-csearch_path%3Dapp")
    captured: dict[str, Any] = {}

    @event.listens_for(eng, "do_connect")
    def _capture(dialect: Any, conn_rec: Any, cargs: Any, cparams: Any) -> None:
        captured.update(cparams)
        raise _AbortConnect("拿到参数即中止，不真连")

    try:
        assert eng.dialect.name == "postgresql"
        assert eng.url.host == "db.example" and eng.url.port == 5432
        assert eng.pool._timeout == 5.0
        assert eng.pool._pre_ping is True
        with pytest.raises(_AbortConnect):
            eng.connect()
        assert captured["connect_timeout"] == 5.0
        assert captured["options"] == "-csearch_path=app -c statement_timeout=15000"
    finally:
        eng.dispose()
    print("PASS test_pg_engine_runtime_config_matches_kwargs")


def test_pg_state_store_engine_reuses_same_timeouts():
    """PG 状态后端与业务库**共用同一份引擎参数**（不许两处各写一套而漂移）。"""
    pytest.importorskip("psycopg")
    from data_dog.state.pg import PostgresStateStore
    store = PostgresStateStore(_PG_URL)
    try:
        assert store._engine.pool._timeout == 5.0
        assert store._engine.pool._pre_ping is True
        assert store._engine.dialect.name == "postgresql"
    finally:
        store.close()
    print("PASS test_pg_state_store_engine_reuses_same_timeouts")


# ================================================================ 2. 变更源失败关闭

def test_change_fallback_demo_switch_defaults_off(monkeypatch: pytest.MonkeyPatch):
    """样例回退开关默认**关**：默认口径是失败关闭，不是"给点假数据"。"""
    from data_dog import settings
    monkeypatch.delenv("DD_CHANGE_FALLBACK_DEMO", raising=False)
    assert settings.change_fallback_demo() is False
    print("PASS test_change_fallback_demo_switch_defaults_off")


def test_change_source_unavailable_is_fail_closed(monkeypatch: pytest.MonkeyPatch):
    """来源抛错 → 返回 unavailable 错误（不是空列表、不是样例数据）。

    "查不到"与"没有变更"对结论是反向的：前者只能得出"变更证据缺失"，后者才支持
    "变更无关"。失败关闭要求这里只能是前者。
    """
    monkeypatch.delenv("DD_CHANGE_FALLBACK_DEMO", raising=False)
    res = _bridge(_BoomSource()).query_recent_changes("dev-1", _COMPONENT, "2026-08-01")
    assert res["ok"] is False
    assert res["error"] == "unavailable", "依赖不可用必须报 unavailable"
    assert res["reason"] == "change_source_unavailable"
    assert "changes" not in res, "错误结果里绝不能带变更列表"
    assert _BoomSource.__name__ in res["detail"]
    print("PASS test_change_source_unavailable_is_fail_closed")


def test_change_unavailable_never_looks_like_no_changes(monkeypatch: pytest.MonkeyPatch):
    """错误形状**不可能**被读成"近期无变更"：无 changes 字段，且错误码不是 not_found。

    工具层对 `ok=False` 原样透传（不包 `changes`），故 Agent 看到的就是这个错误对象。
    """
    monkeypatch.delenv("DD_CHANGE_FALLBACK_DEMO", raising=False)
    bridge = _bridge(_BoomSource())
    raw = bridge.query_recent_changes("dev-1", _COMPONENT, "2026-08-01")
    assert raw != {"changes": [], "source": "builtin_fallback"}
    assert raw.get("changes") is None
    assert raw.get("error") != "not_found"
    # 经 Agent 的工具层：仍是同一个错误对象，没有被补上空 changes 列表
    out = InvestigationTools(bridge).query_recent_changes("dev-1", _COMPONENT, "2026-08-01")
    assert out.get("ok") is False and "changes" not in out
    assert out.get("error") == "unavailable"
    print("PASS test_change_unavailable_never_looks_like_no_changes")


def test_change_fallback_demo_switch_returns_marked_samples(monkeypatch: pytest.MonkeyPatch):
    """显式开 demo 开关时才回退样例表，且结果带 degraded 标注（可被上层识别）。"""
    monkeypatch.setenv("DD_CHANGE_FALLBACK_DEMO", "1")
    res = _bridge(_BoomSource()).query_recent_changes("dev-1", _COMPONENT, "2000-01-01")
    assert res["source"] == "builtin_fallback"
    assert res["degraded"] is True
    assert len(res["changes"]) >= 1
    print("PASS test_change_fallback_demo_switch_returns_marked_samples")


def test_change_source_available_path_label_unchanged(monkeypatch: pytest.MonkeyPatch):
    """正常路径不受影响：来源可用 → 真按 since 过滤 + 标注来源名，无 degraded 字段。"""
    monkeypatch.delenv("DD_CHANGE_FALLBACK_DEMO", raising=False)
    src = MockChangeSource([
        ChangeRecord(change_id="c1", kind="firmware", target=_COMPONENT,
                     applied_at="2026-07-01"),
        ChangeRecord(change_id="c2", kind="config", target=_COMPONENT,
                     applied_at="2026-08-20"),
    ])
    res = _bridge(src).query_recent_changes("dev-1", _COMPONENT, "2026-08-01")
    assert res["source"] == "MockChangeSource"
    assert [c["id"] for c in res["changes"]] == ["c2"]
    assert "degraded" not in res
    print("PASS test_change_source_available_path_label_unchanged")


def test_change_denied_path_is_still_denied():
    """越权仍走 denied（比"来源不可用"优先级更高）：加固没有改变权限语义。"""
    res = _bridge(MockChangeSource()).query_recent_changes("dev-1", "secret.other", "")
    assert res["ok"] is False and res["error"] == "denied"
    print("PASS test_change_denied_path_is_still_denied")
