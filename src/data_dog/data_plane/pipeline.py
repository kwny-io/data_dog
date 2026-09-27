"""P2 在线数据面：端到端流水线（把 §9.1~§9.3 串起来）。

    raw envelope → Gateway(注入身份/校验/限流)
                 → Resolver(兼容绑定，拿不准失败关闭)
                 → Parser → Normalizer → Enricher → Window → Detector
                 → CandidateBuilder(合并/去重/证据)

输出：NormalizedTelemetryEvent 列表 + CandidateEvent 列表（可直接喂给已有 Agent）。

失败语义：任何一步失败 → 该条事件被**失败关闭**（记统计），不中断整条流（§16.3）。
热路径不调大模型（§4.1）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..errors import ToolErrorCode, err
from .candidate import CandidateBuilder
from .chain import (
    Detector,
    Enricher,
    Normalizer,
    Parser,
    StatusCodeNormalizer,
    WindowBuffer,
)
from .events import NormalizedTelemetryEvent, RawLogEnvelope
from .gateway import TelemetryGateway
from .resolver import CompatibilityResolver, OperatorBinding


@dataclass
class DataPlaneStats:
    ingested: int = 0
    resolved: int = 0
    rejected: int = 0
    rejected_by_reason: dict[str, int] = field(default_factory=dict)

    def bump(self, reason: str) -> None:
        self.rejected_by_reason[reason] = self.rejected_by_reason.get(reason, 0) + 1


@dataclass
class DataPlaneResult:
    normalized: list[NormalizedTelemetryEvent] = field(default_factory=list)
    candidates: list[Any] = field(default_factory=list)
    stats: DataPlaneStats = field(default_factory=DataPlaneStats)


class DataPlane:
    """在线数据面最小闭环。"""

    def __init__(self, resolver: CompatibilityResolver, *,
                 gateway: TelemetryGateway | None = None,
                 window_size: int = 30,
                 normalizer: Normalizer | None = None,
                 status_normalizer: StatusCodeNormalizer | None = None,
                 enricher: Enricher | None = None):
        self.resolver = resolver
        self.gateway = gateway or TelemetryGateway()
        self.normalizer = normalizer or Normalizer()
        self.status_normalizer = status_normalizer or StatusCodeNormalizer()
        self.enricher = enricher or Enricher()
        self.window = WindowBuffer(size=window_size)
        self.builder = CandidateBuilder()
        self.stats = DataPlaneStats()

    # ---- 单个事件 ----
    def process_one(self, raw: dict[str, Any], auth: dict[str, Any] | None
                    ) -> tuple[NormalizedTelemetryEvent | None, Any | None]:
        self.stats.ingested += 1
        ok, env_or_err = self.gateway.ingest(raw, auth)
        if not ok:
            self.stats.rejected += 1
            # 保留网关给出的细分失败原因（如 rate_limited / bad_encoding / too_large），
            # 否则限流等被拒绝的情形会被笼统记成 "gateway"，无法在指标上区分。
            self.stats.bump(_gateway_reason(env_or_err))
            return None, env_or_err
        env: RawLogEnvelope = env_or_err

        # 兼容绑定（拿不准失败关闭）
        ok, binding = self.resolver.resolve(
            vendor=env.vendor, model=env.model, firmware=env.firmware,
            protocol_version=env.protocol_version)
        if not ok:
            self.stats.rejected += 1
            self.stats.bump("resolve")
            return None, binding
        self.stats.resolved += 1

        # Parser：需要从注册的算子取 FieldSpec。这里用 binding.extra 携带的 parser 配置。
        parser = binding.extra.get("_parser")
        detector: Detector | None = binding.extra.get("_detector")
        if parser is None:
            self.stats.rejected += 1
            self.stats.bump("no_parser")
            return None, {"ok": False, "error": "contract_mismatch",
                          "detail": f"绑定 {binding.revision} 未挂载 parser"}

        # 取载荷：外置引用必须先过 SSRF 安全校验；被拒 → **失败关闭**。
        # 不能静默当作"空载荷"继续（那会让一条被安全策略拒绝的日志，
        # 表现为"没检出异常"，与 §16.4 失败关闭语义相悖）。
        payload, payload_err = _payload(env)
        if payload_err is not None:
            self.stats.rejected += 1
            self.stats.bump(payload_err.get("reason", "payload_ref"))
            return None, payload_err

        metrics = parser.parse(payload)
        missing = metrics.pop("__missing__", [])

        # Normalizer + 状态码规范化
        metrics = self.normalizer.normalize(metrics)
        status_codes: list[str] = []
        if "_merror" in metrics:
            status_codes = self.status_normalizer.codes_for(metrics.pop("_merror"))

        # Enricher：工况 + 基线（载荷已在上方解析并校验过，这里复用同一份）
        condition = self.enricher.condition_from(payload)
        component = _component_of(env)
        baseline = {}
        for fld in metrics:
            b = self.enricher.baseline_for(component, fld, condition)
            if b is not None:
                baseline[fld] = b

        # 窗口特征
        key = f"{env.device_sn}|{component}"
        self.window.push(key, metrics)

        norm = NormalizedTelemetryEvent(
            event_id=env.event_id, tenant_id=env.tenant_id, device_sn=env.device_sn,
            occurred_at=env.occurred_at,
            component_type=component.split(".")[0],
            component_instance=component.split(".", 1)[1] if "." in component else "",
            metrics=metrics, status_codes=status_codes,
            operating_condition=condition,
            parse_confidence=1.0 if not missing else 0.8,
            missing_fields=missing,
            operator_id=binding.operator_id, operator_version=binding.version,
            binding_revision=binding.revision, raw_log_ref=env.payload_ref,
        )

        # Detector（执行 P1 生成的算子）+ Candidate Builder
        if detector is not None:
            hits = detector.detect(metrics)
            if hits:
                self.builder.add(
                    device_sn=env.device_sn, component_ref=component,
                    metrics=metrics, hits=hits,
                    occurred_at=env.occurred_at, received_at=env.received_at,
                    raw_ref=env.payload_ref, baseline=baseline,
                )
        return norm, None

    # ---- 批量 ----
    def process_batch(self, raws: list[dict[str, Any]],
                      auth: dict[str, Any] | None) -> DataPlaneResult:
        normalized: list[NormalizedTelemetryEvent] = []
        for raw in raws:
            norm, _err = self.process_one(raw, auth)
            if norm is not None:
                normalized.append(norm)
        return DataPlaneResult(
            normalized=normalized, candidates=self.builder.build(), stats=self.stats,
        )


def _payload(env: RawLogEnvelope) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """解析事件载荷。返回 (payload, error)。

    error 非 None 表示**失败关闭**：调用方必须据此拒绝该条事件，不得继续。

    - 外置引用（payload_ref）：必须先过 SSRF 安全校验；被拒 → 返回错误，不静默降级。
    - 内联载荷（payload_inline）：JSON 解析失败 → 返回错误（畸形输入不该被当作
      "正常但无数据"，否则一条坏日志会伪装成"没检出异常"）。
    """
    if env.payload_ref:
        from .fetch import validate_url  # 外置引用必须先过安全校验
        d = validate_url(env.payload_ref)
        if not d.allowed:
            return {}, err(ToolErrorCode.DENIED,
                           f"payload_ref 被安全策略拒绝: {d.reason}",
                           reason="payload_ref_denied")
    if env.payload_inline:
        import json
        try:
            return json.loads(env.payload_inline), None
        except (json.JSONDecodeError, TypeError) as e:
            return {}, err(ToolErrorCode.INVALID_INPUT,
                           f"payload_inline 不是合法 JSON: {e}",
                           reason="payload_malformed")
    return {}, None


def _component_of(env: RawLogEnvelope) -> str:
    if env.component_hint:
        return env.component_hint
    return "unknown.unknown"


def _gateway_reason(err_obj: Any) -> str:
    """从网关错误体里取细分原因；取不到就退回笼统的 "gateway"。"""
    if isinstance(err_obj, dict):
        r = err_obj.get("reason")
        if r:
            return str(r)
    return "gateway"


def build_demo_resolver(detector_config) -> CompatibilityResolver:
    """便捷：用 P1 的 DetectorConfig 组一个绑定（fields/reules 编成 Parser+Detector）。"""
    from ..schemas.operator_config import DetectorConfig
    cfg: DetectorConfig = detector_config
    parser = Parser(cfg.fields)
    detector = Detector(cfg)
    binding = OperatorBinding(
        operator_id=cfg.operator_id, version="1.0.0",
        compat=_compat_from(cfg),
        parser=f"{cfg.operator_id}.parser", detector=cfg.operator_id,
    )
    binding.extra["_parser"] = parser
    binding.extra["_detector"] = detector
    rv = CompatibilityResolver()
    rv.register(binding)
    return rv


def _compat_from(cfg):
    from .resolver import Compatibility
    return Compatibility(vendor="unitree", models=("go2",),
                         firmware=">=3.1,<4.0",
                         protocol_versions=("motor-log-v2",))
