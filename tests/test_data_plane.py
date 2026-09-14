# -*- coding: utf-8 -*-
"""P2 在线数据面测试：网关 / 绑定 / 处理链 / 候选 / SSRF 安全。

覆盖重点：
  - 网关：认证失败关闭、正文身份被覆盖、必需字段/编码/大小/限流
  - Resolver：唯一匹配、无匹配 not_found、多匹配 ambiguous、版本区间
  - 处理链：Parser 映射、缺失字段不崩、Detector 类型防御
  - 候选：合并去重、severity 取最高、证据
  - SSRF：仅 http/https，拒绝环回/私有/保留/localhost
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from data_dog.data_plane.candidate import CandidateBuilder
from data_dog.data_plane.chain import Detector, Parser, WindowBuffer
from data_dog.data_plane.fetch import validate_url
from data_dog.data_plane.gateway import TelemetryGateway
from data_dog.data_plane.pipeline import DataPlane, build_demo_resolver
from data_dog.data_plane.resolver import (
    Compatibility, CompatibilityResolver, OperatorBinding,
)
from data_dog.operator_factory.knowledge import build_knowledge_package
from data_dog.operator_factory.p1_demo import build_motor_operator
from data_dog.schemas.operator_config import DetectorConfig, FieldSpec, ThresholdRule

ROOT = Path(__file__).resolve().parents[1]
CLEANED = ROOT / "vendor_docs" / "unitree" / "cleaned"
AUTH = {"tenant_id": "tenant-real", "device_sn": "device-go2-0001"}


def _raw(event_id="e1", **kw):
    base = dict(event_id=event_id, occurred_at="2026-09-13T10:00:00Z",
                received_at="2026-09-13T10:00:00.1Z", sequence=1,
                vendor="unitree", model="go2", firmware="3.2.1",
                protocol_version="motor-log-v2",
                component_hint="joint_motor.front_left_hip")
    base.update(kw)
    return base


@pytest.fixture(scope="module")
def detector_config() -> DetectorConfig:
    pkg = build_knowledge_package(CLEANED)
    op = build_motor_operator(pkg)
    return DetectorConfig(
        operator_id=op.operator_id, operator_type="parser+detector",
        component="joint_motor.front_left_hip",
        input_schema="raw-log-envelope-v1", output_schema="normalized-telemetry-v1",
        fields=[FieldSpec(**f) for f in op.fields],
        rules=[ThresholdRule(r.rule_id, r.field, r.operator, r.value,
                             r.severity_hint, r.message) for r in op.rules],
    )


# ---------------------------------------------------------------- 网关

def test_gateway_fail_closed_without_auth():
    gw = TelemetryGateway()
    ok, e = gw.ingest(_raw(), None)
    assert not ok and e["error"] == "denied"


def test_gateway_injects_identity_ignoring_body():
    gw = TelemetryGateway()
    ok, env = gw.ingest(_raw(tenant_id="FAKE", device_sn="FAKE"), AUTH)
    assert ok
    assert env.tenant_id == "tenant-real" and env.device_sn == "device-go2-0001"


def test_gateway_rejects_missing_fields():
    gw = TelemetryGateway()
    bad = _raw()
    del bad["firmware"]
    ok, e = gw.ingest(bad, AUTH)
    assert not ok and e["error"] == "invalid_input"


def test_gateway_rejects_bad_encoding_and_oversize():
    gw = TelemetryGateway()
    ok, e = gw.ingest(_raw(encoding="rot13"), AUTH)
    assert not ok and e["error"] == "invalid_input"
    ok, e2 = gw.ingest(_raw(payload_inline="x" * (65 * 1024)), AUTH)
    assert not ok and e2["error"] == "invalid_input"


def test_gateway_rate_limit():
    gw = TelemetryGateway(rate_per_sec=1.0, burst=2)
    results = [gw.ingest(_raw(event_id=f"e{i}"), AUTH)[0] for i in range(6)]
    assert results.count(False) >= 1     # 超限被拒
    assert gw.stats.rate_limited >= 1


# ---------------------------------------------------------------- Resolver

def _resolver():
    rv = CompatibilityResolver()
    rv.register(OperatorBinding(
        "u.motor", "1.0.0",
        Compatibility("unitree", ("go2",), ">=3.1,<4.0", ("motor-log-v2",)),
        detector="d"))
    return rv


def test_resolver_unique_no_match_ambiguous():
    rv = _resolver()
    ok, b = rv.resolve(vendor="unitree", model="go2", firmware="3.2.1",
                       protocol_version="motor-log-v2")
    assert ok and b.revision == "u.motor@1.0.0"
    ok, e = rv.resolve(vendor="unitree", model="go2", firmware="9.9.9",
                       protocol_version="motor-log-v2")
    assert not ok and e["error"] == "not_found"
    rv.register(OperatorBinding("u.motor2", "2.0.0",
                                Compatibility("unitree", ("go2",), ">=3.1,<4.0",
                                              ("motor-log-v2",)), detector="d2"))
    ok, e2 = rv.resolve(vendor="unitree", model="go2", firmware="3.2.1",
                        protocol_version="motor-log-v2")
    assert not ok and e2["error"] == "ambiguous"


def test_resolver_version_range():
    rv = _resolver()
    assert rv.resolve(vendor="unitree", model="go2", firmware="3.9.9",
                      protocol_version="motor-log-v2")[0]
    assert not rv.resolve(vendor="unitree", model="go2", firmware="4.0.0",
                          protocol_version="motor-log-v2")[0]


# ---------------------------------------------------------------- 处理链

def test_parser_maps_and_reports_missing():
    fields = [FieldSpec(name="temperature", source="temp_raw", type="uint8"),
              FieldSpec(name="current", source="cur", type="float32")]
    out = Parser(fields).parse({"temp_raw": "42"})
    assert out["temperature"] == 42
    assert out.get("__missing__") == ["cur"]


def test_detector_type_defensive():
    cfg = DetectorConfig(
        operator_id="d", operator_type="detector", component="c",
        input_schema="s", output_schema="o",
        fields=[], rules=[ThresholdRule("R", "temperature", "ge", 90.0, "critical", "m")])
    d = Detector(cfg)
    assert d.detect({"temperature": 95})
    assert d.detect({"temperature": "hot"}) == []      # 类型防御：不崩、不误命中
    assert d.detect({}) == []


def test_window_features():
    w = WindowBuffer(size=10)
    for v in (1, 2, 3, 4):
        w.push("k", {"temperature": v})
    f = w.features("k")
    assert f.mean["temperature"] == pytest.approx(2.5)
    assert f.slope["temperature"] > 0


# ---------------------------------------------------------------- 候选构建

def test_candidate_merge_and_severity():
    b = CandidateBuilder()
    hits = [{"rule_id": "MOTOR_TEMP_PROTECT", "field": "temperature", "value": 95,
             "operator": "ge", "threshold": 90.0, "severity_hint": "critical",
             "operator_id": "d"}]
    for _ in range(3):
        b.add(device_sn="dev", component_ref="joint_motor.front_left_hip",
              metrics={"temperature": 95}, hits=hits,
              occurred_at="2026-09-13T10:00:00Z", received_at="t",
              raw_ref="rawlog://x", baseline={"temperature": 40})
    cands = b.build()
    assert len(cands) == 1                 # 合并去重
    c = cands[0]
    assert c.severity_hint == "critical" and c.anomaly_score == 1.0
    assert c.evidence and c.evidence_refs


# ---------------------------------------------------------------- 端到端

def test_data_plane_end_to_end(detector_config):
    dp = DataPlane(build_demo_resolver(detector_config))
    batch = [
        _raw("e1", payload_inline=json.dumps({"temperature": 42, "current": 6.0, "mode": 0})),
        _raw("e2", payload_inline=json.dumps({"temperature": 96, "current": 12.0, "mode": 0})),
        _raw("e3", firmware="9.9.9", payload_inline="{}"),     # 失败关闭
    ]
    res = dp.process_batch(batch, AUTH)
    assert res.stats.ingested == 3
    assert res.stats.resolved == 2
    assert res.stats.rejected == 1
    assert len(res.candidates) == 1
    assert res.candidates[0].severity_hint == "critical"


# ---------------------------------------------------------------- SSRF

def test_fetch_rejects_non_http_and_internal():
    assert not validate_url("file:///etc/passwd").allowed
    assert not validate_url("ftp://example.com/x").allowed
    assert not validate_url("http://127.0.0.1/admin").allowed
    assert not validate_url("http://localhost/x").allowed
    assert not validate_url("http://10.0.0.5/x").allowed
    assert not validate_url("http://192.168.1.1/x").allowed
    assert not validate_url("http://169.254.169.254/latest/meta-data").allowed


def test_fetch_allows_public_ip_literal():
    d = validate_url("https://93.184.216.34/x")
    assert d.allowed


def test_fetch_rejects_dns_rebind_to_private():
    # 注入 resolver 模拟"域名解析到内网"（DNS rebinding）
    d = validate_url("https://evil.example/x", resolver=lambda h: ["10.0.0.9"])
    assert not d.allowed and "非公网" in d.reason
