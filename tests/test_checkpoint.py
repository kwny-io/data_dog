"""状态快照/恢复/对账测试（G3）。

覆盖：snapshot/restore 往返（三后端语义一致）、overwrite 语义、周期快照 + 保留
策略、手动端点（token 门控）、对账 diff、以及**快照不含旁路**（只导前缀匹配 key）。

离线：memory 后端 + SQLite；redis 用 fakeredis（装了才跑）。
"""
from __future__ import annotations

import pytest

from data_dog.state.checkpoint import (
    CheckpointLoop,
    diff_checkpoint,
    make_checkpoint_id,
    restore_latest,
    write_checkpoint,
)
from data_dog.state.memory import InMemoryStateStore
from data_dog.storage.db import init_db, reset_engine


def _seed(store, n=3, prefix="g:"):
    for i in range(n):
        store.mutate(f"{prefix}{i}", lambda _cur, v=i: ({"v": v}, None))


@pytest.fixture()
def db(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'ckpt.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", url)
    reset_engine()
    init_db(url)
    yield url
    reset_engine()


# ---------------------------------------------------------------- 原语往返

def test_snapshot_restore_roundtrip():
    """写状态 → snapshot → 清空 → restore → 与原值一致。"""
    src = InMemoryStateStore()
    _seed(src, 3)
    snap = src.snapshot()
    assert set(snap) == {"g:0", "g:1", "g:2"}

    dst = InMemoryStateStore()
    n = dst.restore(snap)
    assert n == 3
    assert dst.snapshot() == snap


def test_snapshot_prefix_filters():
    st = InMemoryStateStore()
    _seed(st, 2, prefix="g:")
    _seed(st, 2, prefix="rate:")
    assert set(st.snapshot(prefix="g:")) == {"g:0", "g:1"}
    assert set(st.keys("rate:")) == {"rate:0", "rate:1"}


def test_restore_overwrite_semantics():
    """overwrite=False 只补缺失（不覆盖已有）；True 强制覆盖。"""
    st = InMemoryStateStore()
    st.mutate("k", lambda _c: ({"v": "old"}, None))
    n = st.restore({"k": {"v": "new"}, "other": {"v": 1}}, overwrite=False)
    assert n == 1                              # 只写了 other
    assert st.get("k") == {"v": "old"}
    assert st.get("other") == {"v": 1}

    st.restore({"k": {"v": "new"}}, overwrite=True)
    assert st.get("k") == {"v": "new"}


# ---------------------------------------------------------------- 检查点落库

def test_write_and_latest_checkpoint(db):
    st = InMemoryStateStore()
    _seed(st, 3)
    meta = write_checkpoint(st)
    assert meta["n_keys"] == 3
    assert "payload" not in meta               # 元数据不回 payload

    res = restore_latest(InMemoryStateStore())
    assert res["restored"] == 3
    assert res["checkpoint_id"] == meta["checkpoint_id"]


def test_restore_latest_no_checkpoint_is_graceful(db):
    res = restore_latest(InMemoryStateStore())
    assert res["restored"] == 0 and res["skipped"] is True


def test_checkpoint_ids_are_time_ordered():
    a = make_checkpoint_id(now=1000.0, prefix="x")
    b = make_checkpoint_id(now=1001.0, prefix="x")
    assert a < b                               # 字符串序 == 时间序


def test_prune_keeps_recent_only(db):
    from data_dog.storage import repository as repo
    from data_dog.storage.db import session_scope
    for i in range(5):
        # 直接写：create 时间由 _now() 定，故顺序靠 flush 先后
        with session_scope() as s:
            repo.save_checkpoint(s, checkpoint_id=f"chk_{i:03d}", prefix="",
                                 payload={"k": i})
    with session_scope() as s:
        removed = repo.prune_checkpoints(s, keep=2)
    assert removed == 3
    with session_scope() as s:
        items = repo.list_checkpoints(s, limit=10)
    assert len(items) == 2


# ---------------------------------------------------------------- 对账 diff

def test_diff_detects_added_changed_removed(db):
    st = InMemoryStateStore()
    st.mutate("a", lambda _c: ({"v": 1}, None))
    st.mutate("b", lambda _c: ({"v": 2}, None))
    meta = write_checkpoint(st)

    st.mutate("a", lambda _c: ({"v": 99}, None))     # changed
    st.mutate("c", lambda _c: ({"v": 3}, None))      # added
    st.delete("b")                                    # removed

    d = diff_checkpoint(st, meta["checkpoint_id"])
    assert d["found"] is True
    assert d["added"] == ["c"]
    assert d["changed"] == ["a"]
    assert d["removed"] == ["b"]


def test_diff_unknown_checkpoint(db):
    d = diff_checkpoint(InMemoryStateStore(), "chk_nope")
    assert d["found"] is False


# ---------------------------------------------------------------- 周期任务

def test_checkpoint_loop_snapshot_once_and_keep(db, monkeypatch):
    st = InMemoryStateStore()
    _seed(st, 2)
    loop = CheckpointLoop(st, interval_s=1000, keep=2)
    loop.snapshot_once()
    loop.snapshot_once()
    loop.snapshot_once()                        # 超保留数 → 旧的被清
    from data_dog.storage import repository as repo
    from data_dog.storage.db import session_scope
    with session_scope() as s:
        items = repo.list_checkpoints(s, limit=10)
    assert len(items) == 2


# ---------------------------------------------------------------- 运维端点

def _client(tmp_path, monkeypatch, **env):
    url = f"sqlite:///{(tmp_path / 'ops.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", url)
    monkeypatch.setenv("DD_INVESTIGATE_OFFLINE", "1")
    monkeypatch.setenv("DD_STATE_BACKEND", "memory")
    import data_dog.state.factory as sf
    sf.reset_state_store()
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    reset_engine()
    init_db(url)
    from fastapi.testclient import TestClient

    from data_dog.service.app import create_app
    return TestClient(create_app()), sf


def test_ops_endpoint_hidden_without_token(tmp_path, monkeypatch):
    """未配 DD_OPS_TOKEN → 端点 404（不暴露）。"""
    c, sf = _client(tmp_path, monkeypatch)
    with c:
        assert c.post("/v1/state/checkpoint").status_code == 404
    sf.reset_state_store()
    reset_engine()


def test_ops_endpoint_requires_correct_token(tmp_path, monkeypatch):
    c, sf = _client(tmp_path, monkeypatch, DD_OPS_TOKEN="secret-ops")
    with c:
        assert c.post("/v1/state/checkpoint").status_code == 401
        r = c.post("/v1/state/checkpoint",
                   headers={"X-Ops-Token": "wrong"})
        assert r.status_code == 401
        ok = c.post("/v1/state/checkpoint", headers={"X-Ops-Token": "secret-ops"})
        assert ok.status_code == 200
        assert ok.json()["ok"] is True
        # 列表可见
        lst = c.get("/v1/state/checkpoints",
                    headers={"X-Ops-Token": "secret-ops"})
        assert lst.json()["total"] >= 1
        cid = lst.json()["items"][0]["checkpoint_id"]
        d = c.get("/v1/state/diff", params={"checkpoint_id": cid},
                  headers={"X-Ops-Token": "secret-ops"})
        assert d.json()["diff"]["found"] is True
    sf.reset_state_store()
    reset_engine()


# ---------------------------------------------------------------- redis 后端一致

def test_redis_backend_snapshot_roundtrip():
    fakeredis = pytest.importorskip("fakeredis")
    from data_dog.state.redis import RedisStateStore
    client = fakeredis.FakeRedis(decode_responses=True)
    st = RedisStateStore(client=client)
    _seed(st, 3)
    snap = st.snapshot()
    assert set(snap) == {"g:0", "g:1", "g:2"}
    assert set(st.keys("g:")) == {"g:0", "g:1", "g:2"}
    st2 = RedisStateStore(client=fakeredis.FakeRedis(decode_responses=True))
    assert st2.restore(snap) == 3
