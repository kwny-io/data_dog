"""W3C traceparent 传播测试。

这是 **correlation-id 传播**，不是完整分布式追踪（无 span/采样/导出）。
重点守住：合法上游 trace-id 被沿用、span-id 每次新建、非法头不炸业务。
"""
from __future__ import annotations

from data_dog.core import tracing


def test_parse_valid_traceparent():
    tp = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    assert tracing.parse_traceparent(tp) == ("a" * 32, "b" * 16)


def test_parse_rejects_malformed():
    assert tracing.parse_traceparent(None) is None
    assert tracing.parse_traceparent("") is None
    assert tracing.parse_traceparent("garbage") is None
    assert tracing.parse_traceparent("00-" + "a" * 31 + "-" + "b" * 16 + "-01") is None
    assert tracing.parse_traceparent("00-" + "a" * 32 + "-" + "b" * 15 + "-01") is None
    # 全零 trace-id / span-id 非法（规范禁止）
    assert tracing.parse_traceparent("00-" + "0" * 32 + "-" + "b" * 16 + "-01") is None
    assert tracing.parse_traceparent("00-" + "a" * 32 + "-" + "0" * 16 + "-01") is None


def test_start_trace_inherits_upstream_trace_id():
    """上游带合法 traceparent → 沿用其 trace-id（跨服务同一条链路）。"""
    upstream_trace = "c" * 32
    tp = f"00-{upstream_trace}-{'d' * 16}-01"
    trace_id, out = tracing.start_trace(tp)
    assert trace_id == upstream_trace
    assert tracing.get_trace_id() == upstream_trace
    # 响应头必须是合法 traceparent，且 trace-id 一致
    parsed = tracing.parse_traceparent(out)
    assert parsed is not None and parsed[0] == upstream_trace


def test_start_trace_makes_new_span_id():
    """即使沿用上游 trace-id，span-id 也必须新建（不照抄上游）。"""
    upstream_span = "d" * 16
    tp = f"00-{'c' * 32}-{upstream_span}-01"
    _, out = tracing.start_trace(tp)
    new_span = tracing.parse_traceparent(out)[1]
    assert new_span != upstream_span, "span-id 不应照抄上游"


def test_start_trace_generates_when_absent():
    trace_id, out = tracing.start_trace(None)
    assert len(trace_id) == 32
    assert tracing.parse_traceparent(out) is not None
    tracing.clear_trace()


def test_start_trace_ignores_invalid_header():
    """非法上游头不抛异常，而是当作没有上游 trace 处理。"""
    trace_id, out = tracing.start_trace("not-a-valid-traceparent")
    assert len(trace_id) == 32
    assert tracing.parse_traceparent(out) is not None
    tracing.clear_trace()
