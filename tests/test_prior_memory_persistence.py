"""跨调查记忆的**持久化 / 与模型-key 解耦**：记忆落在 DB，不绑 provider。

产品口径：谁都能接自己的 key / 模型；agent 的记忆应当**继承**（同一部署里继续积累），
且**按租户隔离**（换 key 不会看到别人的记忆）。本文件把这条性质钉住：

- 记忆读的是 `reports` 表（`service.runtime._prior_report_items`），**与 provider 无关**；
- 按 `(device, component)` 过滤、可排除当前候选、上限 5（`limit`）；
- **跨租户不可见**。

约定：每个 test 结尾 print("PASS <name>")。
"""
from __future__ import annotations

from data_dog.service.runtime import _prior_report_items
from data_dog.storage import repository as repo
from data_dog.storage.db import init_db, session_scope

COMP = "joint_motor.front_left_hip"
OTHER = "joint_motor.front_right_hip"


def _init(monkeypatch, tmp_path) -> None:
    url = "sqlite:///" + (tmp_path / "prior.db").as_posix()
    monkeypatch.setenv("DD_DATABASE_URL", url)
    init_db(url)


def _cand(cid: str, comp: str) -> dict:
    return {"candidate_id": cid, "device_sn": "d1", "component_ref": comp,
            "signal": "temperature_high", "severity_hint": "warning",
            "window_start": "09:55", "window_end": "10:00", "sample_count": 1,
            "rule_matches": ["R"], "anomaly_score": 0.5, "evidence": [],
            "evidence_refs": [], "detector_provenance": "op"}


def _report(rid: str, refs: list, disposition: str = "rejected") -> dict:
    return {"investigation_id": rid, "candidate_refs": list(refs),
            "disposition": disposition, "severity": "info", "confidence": 0.8,
            "event_type": "none", "summary": f"summary {rid}",
            "counter_evidence_refs": [f"ref://{rid}"]}


def test_prior_items_are_db_backed_and_filtered(monkeypatch, tmp_path):
    _init(monkeypatch, tmp_path)
    with session_scope() as s:
        repo.save_candidates(s, [_cand("c1", COMP), _cand("c2", OTHER), _cand("c3", COMP)],
                             tenant_id="t1")
        repo.save_report(s, _report("r1", ["c1"]), tenant_id="t1")
        repo.save_report(s, _report("r2", ["c2"]), tenant_id="t1")     # 别的部件
        repo.save_report(s, _report("r3", ["c3"]), tenant_id="t1")
        items = _prior_report_items(s, "t1", device_sn="d1", component_ref=COMP)
    assert {i["candidate_id"] for i in items} == {"c1", "c3"}          # 只含本部件
    assert all(i["disposition"] == "rejected" for i in items)          # 从 DB 读回
    assert all(i["counter_evidence_refs"] for i in items)              # 无损保留证据引用
    with session_scope() as s:
        ex = _prior_report_items(s, "t1", device_sn="d1", component_ref=COMP,
                                 exclude_candidate="c3")
    assert {i["candidate_id"] for i in ex} == {"c1"}                   # 不把自己算成既往
    print("PASS test_prior_items_are_db_backed_and_filtered")


def test_prior_items_capped(monkeypatch, tmp_path):
    _init(monkeypatch, tmp_path)
    with session_scope() as s:
        for i in range(8):
            repo.save_candidates(s, [_cand(f"c{i}", COMP)], tenant_id="t1")
            repo.save_report(s, _report(f"r{i}", [f"c{i}"]), tenant_id="t1")
        items = _prior_report_items(s, "t1", device_sn="d1", component_ref=COMP, limit=5)
    assert len(items) == 5
    print("PASS test_prior_items_capped")


def test_prior_items_tenant_isolated(monkeypatch, tmp_path):
    _init(monkeypatch, tmp_path)
    with session_scope() as s:
        repo.save_candidates(s, [_cand("c1", COMP)], tenant_id="t1")
        repo.save_report(s, _report("r1", ["c1"]), tenant_id="t1")
        repo.save_candidates(s, [_cand("c2", COMP)], tenant_id="t2")
        repo.save_report(s, _report("r2", ["c2"]), tenant_id="t2")
        t1 = _prior_report_items(s, "t1", device_sn="d1", component_ref=COMP)
        t2 = _prior_report_items(s, "t2", device_sn="d1", component_ref=COMP)
    assert {i["candidate_id"] for i in t1} == {"c1"}
    assert {i["candidate_id"] for i in t2} == {"c2"}    # 换 key/租户不会看到别人的记忆
    print("PASS test_prior_items_tenant_isolated")
