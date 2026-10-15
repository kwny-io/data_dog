"""Evidence Ref 解析协议测试（缺架构口 E-8）。

覆盖：
  - 语法：合法/非法/畸形（路径穿越、空、非白名单 scheme）
  - 解析：scheme/authority/path 结构化、raw 往返
  - 受控解析：权限（denied）/ 不存在（not_found）/ 数据源异常（execution_failed）
  - 完整性校验：digest 相符→通过，不符→contract_mismatch
  - 有效期：未过期→通过，过期→not_found（用注入时钟，不依赖真实时间）
  - 批量：保序、单条失败不影响其他条
  - 与真实链路对齐：CandidateBuilder 产出的 ref 能被解析
"""
from __future__ import annotations

import pytest

from data_dog.data_plane.candidate import CandidateBuilder
from data_dog.evidence.refs import (
    EvidenceResolver,
    default_resolver,
    digest,
    is_valid_ref,
    make_candidate_metricwin_source,
    make_telemetry_source,
    metricwin_ref_for,
    parse_ref,
)

# ---------- 1. 语法 ----------

@pytest.mark.parametrize("raw,scheme,authority,path", [
    ("metricwin://cand_abc", "metricwin", "cand_abc", []),
    ("rawlog://opaque-ref", "rawlog", "opaque-ref", []),
    ("vendordoc://motor-x2-thermal", "vendordoc", "motor-x2-thermal", []),
    # 窗口级引用：path 段含 ':' 与 '-'（真实 TELEMETRY 键形如 "09:55-10:00"）
    ("metricwin://joint_motor.front_left_hip/09:55-10:00",
     "metricwin", "joint_motor.front_left_hip", ["09:55-10:00"]),
])
def test_parse_valid(raw, scheme, authority, path):
    r = parse_ref(raw)
    assert r.scheme == scheme
    assert r.authority == authority
    assert r.path == path


@pytest.mark.parametrize("bad", [
    "", "   ", "no-scheme", "metricwin:/single-slash",
    "http://example.com",              # 非白名单 scheme
    "ftp://x",                          # 非白名单 scheme
    "metricwin://",                     # 缺 authority
    "metricwin://a/../../etc/passwd",   # 路径穿越：.. 非法字符
    "metricwin://a/%2e%2e/x",           # 编码穿越：% 非法字符
    "metricwin://a b",                  # 空格
])
def test_parse_invalid(bad):
    with pytest.raises(ValueError):
        parse_ref(bad)
    assert is_valid_ref(bad) is False


def test_parsed_ref_roundtrip():
    raw = "metricwin://comp/win1/win2"
    assert parse_ref(raw).raw == raw


# ---------- 2. 受控解析 ----------

def _rawlog_stub(authority: str, path: list[str]):
    """rawlog 解析用的桩。

    `rawlog://` 的**默认源现在是真读正文**（`evidence/rawlog.py`），没配存放位置就失败关闭；
    本文件测的是**解析器机制**（有效期/完整性/批量），所以显式注入桩，
    不再依赖"默认源恰好返回 mock 内容"。默认源自身的真实读取与失败关闭由
    `tests/test_rawlog_source.py` 单独钉住。
    """
    return True, {"event_id": authority}


def test_default_rawlog_source_fails_closed_when_unconfigured(monkeypatch):
    """默认 rawlog 源是**真读**：没配存放位置 → execution_failed，**不再返回 mock 内容**。"""
    monkeypatch.delenv("DD_RAWLOG_BASE_DIR", raising=False)
    monkeypatch.delenv("DD_RAWLOG_BASE_URL", raising=False)
    out = default_resolver().resolve("rawlog://e1")
    assert out["ok"] is False and out["error"] == "execution_failed"
    assert "未配置" in str(out.get("detail", ""))


def test_resolve_ok_and_payload():
    res = default_resolver()
    res.replace_source("rawlog", _rawlog_stub)
    out = res.resolve("rawlog://e1")
    assert out["ok"] is True
    assert out["source"] == "rawlog"
    assert out["payload"]["event_id"] == "e1"
    assert out["ref"]["scheme"] == "rawlog"


def test_resolve_denied_for_unauthorized_component():
    res = EvidenceResolver({"metricwin": make_telemetry_source()})
    out = res.resolve(metricwin_ref_for("secret.other", "09:55-10:00"))
    assert out["ok"] is False
    assert out["error"] == "denied"


def test_resolve_not_found_for_missing_window():
    res = EvidenceResolver({"metricwin": make_telemetry_source()})
    out = res.resolve(metricwin_ref_for("battery.main", "23:00-23:30"))
    assert out["ok"] is False
    assert out["error"] == "not_found"


def test_resolve_ok_for_real_telemetry_window():
    res = EvidenceResolver({"metricwin": make_telemetry_source()})
    out = res.resolve(metricwin_ref_for("joint_motor.front_left_hip", "09:55-10:00"))
    assert out["ok"] is True
    assert out["payload"]["metrics"]["temperature_c"] == 78.2


def test_resolve_invalid_input():
    out = default_resolver().resolve("not a ref")
    assert out["ok"] is False and out["error"] == "invalid_input"


def test_resolve_unknown_scheme_blocked_at_parse():
    # fs:// 不在白名单 → 语法层就挡掉，不会走到数据源
    out = default_resolver().resolve("fs://etc/passwd")
    assert out["ok"] is False and out["error"] == "invalid_input"


def test_resolve_source_exception_is_execution_failed():
    def boom(authority, path):
        raise RuntimeError("データ源挂了")

    res = EvidenceResolver({"rawlog": boom})
    out = res.resolve("rawlog://e1")
    assert out["ok"] is False and out["error"] == "execution_failed"


def test_resolve_missing_source_is_unavailable():
    res = EvidenceResolver({})       # 没有任何源
    out = res.resolve("rawlog://e1")
    assert out["ok"] is False and out["error"] == "unavailable"


# ---------- 3. 完整性校验 ----------

def test_digest_match_passes():
    res = default_resolver()
    first = res.resolve("vendordoc://motor-x2-thermal")
    d = digest(first["payload"])
    again = res.resolve("vendordoc://motor-x2-thermal", expected_digest=d)
    assert again["ok"] is True


def test_digest_mismatch_is_contract_mismatch():
    out = default_resolver().resolve("vendordoc://motor-x2-thermal",
                                     expected_digest="sha256:deadbeef")
    assert out["ok"] is False and out["error"] == "contract_mismatch"


# ---------- 4. 有效期（注入时钟） ----------

def test_not_expired_passes():
    res = EvidenceResolver({"rawlog": _rawlog_stub}, ttl_seconds=100, clock=lambda: 1000.0)
    out = res.resolve("rawlog://e1", issued_at=950.0)      # 50s 前签发
    assert out["ok"] is True


def test_expired_is_not_found():
    res = EvidenceResolver(ttl_seconds=100, clock=lambda: 1000.0)
    out = res.resolve("rawlog://e1", issued_at=800.0)      # 200s 前签发
    assert out["ok"] is False and out["error"] == "not_found"


def test_ttl_disabled_skips_expiry():
    res = EvidenceResolver({"rawlog": _rawlog_stub}, ttl_seconds=0, clock=lambda: 10**9)
    out = res.resolve("rawlog://e1", issued_at=0.0)
    assert out["ok"] is True


# ---------- 5. 批量 ----------

def test_resolve_all_preserves_order_and_isolates_failures():
    res = default_resolver()
    res.replace_source("rawlog", _rawlog_stub)
    out = res.resolve_all(["rawlog://e1", "bogus", "vendordoc://motor-x2-thermal"])
    assert out["ok"] is True
    assert out["total"] == 3
    assert out["resolved"] == 2
    assert [r["ok"] for r in out["results"]] == [True, False, True]


# ---------- 6. 与真实链路对齐 ----------

def test_real_candidate_refs_are_parseable():
    """CandidateBuilder 产出的 evidence_refs 必须能被本模块解析（契约对齐）。"""
    b = CandidateBuilder(merge_window_s=5.0)
    b.add(device_sn="device-abc123", component_ref="joint_motor.front_left_hip",
          metrics={"temperature": 95}, occurred_at="2026-09-13T10:00:00Z",
          received_at="2026-09-13T10:00:01Z", raw_ref="rawlog://opaque-ref",
          hits=[{"rule_id": "motor_temp_protect", "field": "temperature",
                 "value": 95, "severity_hint": "critical",
                 "operator_id": "op-1", "operator": ">", "threshold": 90}])
    cands = b.build()
    assert cands
    for cand in cands:
        assert cand.evidence_refs
        for ref in cand.evidence_refs:
            assert is_valid_ref(ref), f"链路产出的 ref 不可解析: {ref}"


def test_candidate_metricwin_source_indexes_real_row():
    """注入真实反查源后，metricwin ref 解析出**索引内容**而非 not_indexed 占位。"""
    row = {"device_sn": "device-1", "component_ref": "joint_motor.front_left_hip",
           "signal": "temperature_rising", "severity_hint": "critical",
           "window_start": "2026-09-13T09:55:00Z",
           "window_end": "2026-09-13T10:00:00Z",
           "sample_count": 3, "rule_matches": ["motor_temp_protect"],
           "anomaly_score": 0.8,
           "evidence": [{"key": "temperature_c", "from_value": 90, "to_value": 96}],
           "evidence_refs": ["metricwin://cand_abc"], "detector_provenance": "op-1:1"}
    res = EvidenceResolver({"metricwin": make_candidate_metricwin_source(
        lambda cid: row if cid == "cand_abc" else None)})
    out = res.resolve("metricwin://cand_abc")
    assert out["ok"] is True
    p = out["payload"]
    assert p["indexed"] is True
    assert p["component_ref"] == "joint_motor.front_left_hip"
    assert p["window"]["start"].startswith("2026-09-13")


def test_candidate_metricwin_source_missing_is_not_found():
    res = EvidenceResolver({"metricwin": make_candidate_metricwin_source(
        lambda cid: None)})
    out = res.resolve("metricwin://cand_missing")
    assert out["ok"] is False
    assert out["error"] == "not_found"


def test_candidate_metricwin_source_ignores_non_candidate_authority():
    """本源只管 cand_ 前缀；非候选 authority 交回 not_found（不被误当候选查）。"""
    called = []
    res = EvidenceResolver({"metricwin": make_candidate_metricwin_source(
        lambda cid: called.append(cid))})
    out = res.resolve("metricwin://some_component/09:55-10:00")
    assert out["ok"] is False
    assert out["error"] == "not_found"
    assert called == []          # lookup 根本没被调用
