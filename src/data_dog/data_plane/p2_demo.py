"""P2 端到端演示：原始日志流 → 候选异常（含失败关闭与安全校验）。

跑法：
    python -m data_dog.data_plane.p2_demo          # 人读输出
    python -m data_dog.data_plane.p2_demo --json   # 机器可读

演示内容：
  1. 一批原始日志（正常/过温/固件不匹配/伪身份/超限）进数据面
  2. 网关注入身份 + 校验 + 限流；Resolver 兼容绑定
  3. 解析→规范化→增强→窗口→检测 → 合并成候选
  4. 失败样例全部"失败关闭"（不猜、不降级）
  5. URL 安全校验（SSRF 防护）演示
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..core.console import ensure_utf8_console
from ..operator_factory.knowledge import build_knowledge_package
from ..operator_factory.p1_demo import build_motor_operator
from ..paths import vendor_docs_dir
from ..schemas.operator_config import DetectorConfig, FieldSpec, ThresholdRule
from .fetch import validate_url
from .pipeline import DataPlane, build_demo_resolver


def cleaned_dir() -> Path:
    """厂商清洗后文档目录。

    运行时解析（而非模块导入时固定），以便尊重 DD_VENDOR_DOCS 覆盖——
    这是"装进 site-packages 后找不到文档"那个 bug 的修复点。
    """
    return vendor_docs_dir() / "unitree" / "cleaned"


def _detector_config() -> DetectorConfig:
    pkg = build_knowledge_package(cleaned_dir())
    op = build_motor_operator(pkg)
    return DetectorConfig(
        operator_id=op.operator_id, operator_type="parser+detector",
        component="joint_motor.front_left_hip",
        input_schema="raw-log-envelope-v1", output_schema="normalized-telemetry-v1",
        fields=[FieldSpec(**f) for f in op.fields],
        rules=[ThresholdRule(r.rule_id, r.field, r.operator, r.value,
                             r.severity_hint, r.message) for r in op.rules],
    )


def _raw(event_id: str, **kw):
    base = dict(
        event_id=event_id, occurred_at="2026-09-13T10:00:00Z",
        received_at="2026-09-13T10:00:00.100Z", sequence=1,
        vendor="unitree", model="go2", firmware="3.2.1",
        protocol_version="motor-log-v2", component_hint="joint_motor.front_left_hip",
    )
    base.update(kw)
    return base


def main() -> int:
    ensure_utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    dp = DataPlane(build_demo_resolver(_detector_config()))
    auth = {"tenant_id": "tenant-real", "device_sn": "device-go2-0001"}

    batch = [
        _raw("e1", payload_inline=json.dumps({"temperature": 42, "current": 6.0, "mode": 0})),
        _raw("e2", payload_inline=json.dumps({"temperature": 60, "current": 7.0, "mode": 0})),
        _raw("e3", payload_inline=json.dumps({"temperature": 95, "current": 12.0, "mode": 0})),
        _raw("e4", payload_inline=json.dumps({"temperature": 97, "current": 13.0, "mode": 0})),
        # 固件不在兼容范围 → 失败关闭
        _raw("e5", firmware="9.9.9", payload_inline="{}"),
        # 伪身份（正文谎报）→ 被注入身份覆盖；用错协议版本 → 失败关闭
        _raw("e6", protocol_version="old-v1", payload_inline="{}"),
    ]

    result = dp.process_batch(batch, auth)

    # 失败关闭样例（网关层）
    ok_reject, err_auth = dp.gateway.ingest(_raw("e7", payload_inline="{}"), None)
    ok_reject2, err_fake = dp.gateway.ingest(
        _raw("e8", tenant_id="FAKE", device_sn="FAKE", payload_inline="{}"), auth)

    # SSRF 安全校验
    url_checks = [
        "https://github.com/kwny-io/data_dog",
        "http://127.0.0.1:8000/admin",
        "http://localhost/secret",
        "http://10.0.0.5/internal",
        "file:///etc/passwd",
    ]

    if args.json:
        print(json.dumps({
            "ingested": result.stats.ingested,
            "resolved": result.stats.resolved,
            "rejected": result.stats.rejected,
            "rejected_by_reason": result.stats.rejected_by_reason,
            "normalized_count": len(result.normalized),
            "candidates": [c.to_dict() for c in result.candidates],
            "url_checks": [{"url": u, "allowed": validate_url(u).allowed,
                            "reason": validate_url(u).reason} for u in url_checks],
        }, ensure_ascii=False, indent=2, default=str))
        return 0

    print("=" * 68)
    print("P2 在线数据面 —— 原始日志 → 候选异常")
    print("=" * 68)
    print(f"收到 {result.stats.ingested} 条 | 绑定成功 {result.stats.resolved} | "
          f"失败关闭 {result.stats.rejected}  {result.stats.rejected_by_reason}")
    print(f"规范化遥测：{len(result.normalized)} 条")
    print("-" * 68)
    print(f"候选异常：{len(result.candidates)} 条")
    for c in result.candidates:
        print(f"  ▸ {c.candidate_id}  [{c.severity_hint}] {c.component_ref}")
        print(f"     信号={c.signal}  规则={c.rule_matches}  score={c.anomaly_score}")
        for e in c.evidence:
            print(f"     证据 {e.key}: {e.from_value} → {e.to_value}  ({e.note})")
    print("-" * 68)
    print("失败关闭演示（这些都必须被拒，且不猜）：")
    print(f"  无认证      → {err_auth['error']}: {err_auth['detail']}")
    ok2, env2 = ok_reject2, err_fake
    print(f"  伪身份覆盖  → 正文谎报被丢弃，实际注入 device={env2.device_sn if ok2 else 'REJECTED'}")
    print("-" * 68)
    print("URL 安全校验（仅 http/https；拒绝内网/环回/保留）：")
    for u in url_checks:
        d = validate_url(u)
        mark = "✅ 允许" if d.allowed else "⛔ 拒绝"
        print(f"  {mark}  {u}   {('' if d.allowed else '— ' + d.reason)}")
    print("  (注：若公网域名被拒且原因是“解析到非公网地址”，通常是本机代理的 fake-IP")
    print("   把域名解析到了保留段——严格校验按失败关闭处理，属预期安全行为。)")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
