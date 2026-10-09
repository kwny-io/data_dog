"""G7 变更管理源适配器测试：真按 since 过滤 + 推送落库 + SSRF + 租户隔离。

守住的关键事：
  1. **since 真正生效**（此前静默忽略）——mock/jsonl/webhook/db 四种来源一致。
  2. **契约不变**：对 Agent 的返回形状与内置表一致（id/type/what/…）。
  3. **推送式**：`POST /v1/changes/ingest` → 落库 → Agent 查得到。
  4. **SSRF**：webhook 目标过 `fetch.validate_url`，内网 URL 被拒。
  5. **租户隔离** + 缺租户头 401。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from data_dog.evidence.change_source import (
    ChangeRecord,
    DbChangeSource,
    JsonlChangeSource,
    MockChangeSource,
    WebhookChangeSource,
    _in_window,
    default_change_source,
)
from data_dog.storage.db import init_db, reset_engine

# ---------------------------------------------------------------- 过滤语义

def test_in_window_unparseable_kept():
    """无法解析的 applied_at 一律保留（宁多勿漏）。"""
    assert _in_window("", "2026-08-01", "") is True
    assert _in_window("不是时间", "2026-08-01", "") is True


def test_in_window_since_filters():
    assert _in_window("2026-08-05", "2026-08-01", "") is True
    assert _in_window("2026-07-05", "2026-08-01", "") is False
    # 边界：等于 since 保留（闭区间）
    assert _in_window("2026-08-01", "2026-08-01", "") is True


def test_in_window_until_filters():
    assert _in_window("2026-08-05", "", "2026-08-10") is True
    assert _in_window("2026-08-15", "", "2026-08-10") is False


# ---------------------------------------------------------------- mock

def _rec(cid, component, applied_at):
    return ChangeRecord(change_id=cid, kind="firmware", target=component,
                        applied_at=applied_at)


def test_mock_source_filters_since():
    src = MockChangeSource([
        _rec("c1", "joint_motor.front_left_hip", "2026-07-01"),
        _rec("c2", "joint_motor.front_left_hip", "2026-08-20"),
    ])
    got = src.query(device_sn="d", component="joint_motor.front_left_hip",
                    since="2026-08-01")
    assert [r.change_id for r in got] == ["c2"]


def test_mock_source_defaults_to_builtin():
    """默认包装内置表：能查到一个白名单部件的变更。"""
    src = MockChangeSource()
    got = src.query(device_sn="d", component="joint_motor.front_left_hip",
                    since="2000-01-01")
    assert len(got) >= 1
    d = got[0].to_dict()
    # 契约形状：id/type 仍在（向后兼容内置表消费者）
    assert "id" in d and "type" in d and d["target"] == "joint_motor.front_left_hip"


def test_mock_source_filters_component():
    src = MockChangeSource([_rec("c1", "a", "2026-08-20"),
                            _rec("c2", "b", "2026-08-20")])
    got = src.query(device_sn="d", component="a", since="")
    assert [r.target for r in got] == ["a"]


# ---------------------------------------------------------------- jsonl

def test_jsonl_source_reads_and_filters(tmp_path):
    p = tmp_path / "changes.jsonl"
    p.write_text(
        '{"change_id":"c1","kind":"firmware","target":"joint_motor.front_left_hip",'
        '"applied_at":"2026-07-01","what":"old"}\n'
        '{"change_id":"c2","kind":"config","target":"joint_motor.front_left_hip",'
        '"applied_at":"2026-08-20","what":"new"}\n'
        "坏行不是 JSON\n"
        "\n",
        encoding="utf-8",
    )
    src = JsonlChangeSource(str(p))
    got = src.query(device_sn="d", component="joint_motor.front_left_hip",
                    since="2026-08-01")
    assert [r.change_id for r in got] == ["c2"]
    # 未知字段进 detail，并被 to_dict 展开
    assert got[0].to_dict()["what"] == "new"


def test_jsonl_source_missing_file_is_empty(tmp_path):
    src = JsonlChangeSource(str(tmp_path / "nope.jsonl"))
    assert src.query(device_sn="d", component="x", since="") == []


# ---------------------------------------------------------------- webhook（SSRF）

def test_webhook_rejects_internal_url():
    """内网/环回地址过不了 validate_url → 返回空（失败关闭），不发起请求。"""
    called = {"n": 0}

    def _fetcher(url, timeout):
        called["n"] += 1
        return [{"change_id": "c1", "target": "x", "applied_at": "2026-08-20"}]

    src = WebhookChangeSource("http://127.0.0.1/cmdb", fetcher=_fetcher)
    got = src.query(device_sn="d", component="joint_motor.front_left_hip",
                    since="2026-08-01")
    assert got == []
    assert called["n"] == 0                 # 校验不过就根本没发请求
    assert src.last_reason                    # 拒绝原因可查


def test_webhook_local_refilter_applied():
    """远端不按 since 过滤 → 本地再保证一次（契约不靠对方）。"""
    def _fetcher(url, timeout):
        return [
            {"change_id": "old", "target": "joint_motor.front_left_hip",
             "applied_at": "2026-01-01"},
            {"change_id": "new", "target": "joint_motor.front_left_hip",
             "applied_at": "2026-08-20"},
        ]

    # allowed=True 绕过 SSRF 校验（只测本地复过滤逻辑）
    src = WebhookChangeSource("https://cmdb.example.com/api", fetcher=_fetcher,
                              allowed=True)
    src._validated = True                    # 跳过真实校验（本用例只测过滤）
    got = src.query(device_sn="d", component="joint_motor.front_left_hip",
                    since="2026-08-01")
    assert [r.change_id for r in got] == ["new"]


# ---------------------------------------------------------------- db（推送式落库）

@pytest.fixture()
def svc(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'svc.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", url)
    reset_engine()
    init_db(url)
    from data_dog.service.app import create_app
    with TestClient(create_app()) as c:
        yield c
    reset_engine()


_Q = {"X-Tenant-Id": "tenant-real"}


def test_ingest_then_query_via_db_source(svc):
    """推送式：POST /v1/changes/ingest → 落库 → DbChangeSource 查得到。"""
    r = svc.post("/v1/changes/ingest", headers=_Q, json={
        "change_id": "chg-900", "kind": "firmware",
        "target": "joint_motor.front_left_hip",
        "applied_at": "2026-09-20T10:00:00Z", "to_version": "1.2.1",
        "note": "冷却参数调整",       # 未知字段 → raw
    })
    assert r.status_code == 200, r.text
    assert r.json()["created"] is True

    from data_dog.storage.db import session_scope
    src = DbChangeSource(session_scope, tenant_id="tenant-real")
    got = src.query(device_sn="d", component="joint_motor.front_left_hip",
                    since="2026-09-01")
    assert len(got) == 1
    d = got[0].to_dict()
    assert d["id"] == "chg-900" and d["type"] == "firmware"
    assert d["note"] == "冷却参数调整"      # raw 展开
    assert d["to_version"] == "1.2.1"


def test_ingest_idempotent(svc):
    """同 change_id 重复推送 → 覆盖更新，不重复插入。"""
    body = {"change_id": "chg-1", "target": "battery.main",
            "applied_at": "2026-09-20T10:00:00Z", "to_version": "1.0"}
    a = svc.post("/v1/changes/ingest", headers=_Q, json=body).json()
    body["to_version"] = "1.1"
    b = svc.post("/v1/changes/ingest", headers=_Q, json=body).json()
    assert a["created"] is True and b["created"] is False
    items = svc.get("/v1/changes", headers=_Q).json()["items"]
    assert len(items) == 1
    assert items[0]["to_version"] == "1.1"


def test_ingest_validation(svc):
    assert svc.post("/v1/changes/ingest", headers=_Q,
                    json={"target": "battery.main"}).status_code == 422
    assert svc.post("/v1/changes/ingest", headers=_Q,
                    json={"change_id": "c1"}).status_code == 422


def test_changes_requires_tenant(svc):
    assert svc.get("/v1/changes").status_code == 401
    assert svc.post("/v1/changes/ingest",
                    json={"change_id": "c", "target": "battery.main"}
                    ).status_code == 401


def test_changes_tenant_isolation(svc):
    svc.post("/v1/changes/ingest", headers=_Q, json={
        "change_id": "chg-mine", "target": "battery.main",
        "applied_at": "2026-09-20"})
    # 另一租户看不到
    other = svc.get("/v1/changes", headers={"X-Tenant-Id": "tenant-other"}).json()
    assert other["count"] == 0
    # 本租户看得到
    mine = svc.get("/v1/changes", headers=_Q).json()
    assert mine["count"] == 1


def test_bridge_query_uses_change_source():
    """证据桥走 ChangeSource，返回带 source 标注且真过滤 since。"""
    from data_dog.service.evidence import RepoEvidenceBridge
    b = RepoEvidenceBridge([], device_sn="d", change_source=MockChangeSource([
        _rec("c1", "joint_motor.front_left_hip", "2026-07-01"),
        _rec("c2", "joint_motor.front_left_hip", "2026-08-20"),
    ]))
    res = b.query_recent_changes("d", "joint_motor.front_left_hip", "2026-08-01")
    assert [c["id"] for c in res["changes"]] == ["c2"]
    assert res["source"] == "MockChangeSource"


def test_bridge_fails_closed_on_source_error(monkeypatch):
    """来源抛异常 → **失败关闭**（默认）：回 unavailable 错误，且**不含 changes 字段**。

    为什么不回 `{"changes": []}`：空列表在 Agent 眼里等价于"近期没有变更"，
    那是假结论——"查不到" ≠ "没有"。这条测试锁的正是这个区分。
    """
    from data_dog.service.evidence import RepoEvidenceBridge

    class _Boom:
        def query(self, **kw):
            raise RuntimeError("cmdb down")

    monkeypatch.delenv("DD_CHANGE_FALLBACK_DEMO", raising=False)
    b = RepoEvidenceBridge([], device_sn="d", change_source=_Boom())
    res = b.query_recent_changes("d", "joint_motor.front_left_hip", "2000-01-01")
    assert res["ok"] is False
    assert res["error"] == "unavailable"
    assert res["reason"] == "change_source_unavailable"
    assert "changes" not in res                  # 绝不给出看似有效的变更列表
    print("PASS test_bridge_fails_closed_on_source_error")


def test_bridge_demo_fallback_requires_explicit_switch(monkeypatch):
    """只有显式 `DD_CHANGE_FALLBACK_DEMO=1` 才回退样例表，且标记 `degraded=True`。"""
    from data_dog.service.evidence import RepoEvidenceBridge

    class _Boom:
        def query(self, **kw):
            raise RuntimeError("cmdb down")

    monkeypatch.setenv("DD_CHANGE_FALLBACK_DEMO", "1")
    b = RepoEvidenceBridge([], device_sn="d", change_source=_Boom())
    res = b.query_recent_changes("d", "joint_motor.front_left_hip", "2000-01-01")
    assert res["source"] == "builtin_fallback"
    assert res["degraded"] is True               # 不得被误读成生产数据
    assert len(res["changes"]) >= 1
    print("PASS test_bridge_demo_fallback_requires_explicit_switch")


def test_default_source_is_mock(monkeypatch):
    monkeypatch.delenv("DD_CHANGE_SOURCE", raising=False)
    assert isinstance(default_change_source(), MockChangeSource)
    monkeypatch.setenv("DD_CHANGE_SOURCE", "jsonl")
    monkeypatch.setenv("DD_CHANGE_JSONL_PATH", "/tmp/x.jsonl")
    assert isinstance(default_change_source(), JsonlChangeSource)
    # 配了 jsonl 却没路径 → 退回 mock（不静默无数据）
    monkeypatch.delenv("DD_CHANGE_JSONL_PATH", raising=False)
    assert isinstance(default_change_source(), MockChangeSource)
