"""流式消费循环：把一个 `StreamSource` 接到数据面 + 落库。

与 HTTP 入口的区别
------------------
HTTP 入口每个请求新建一个 DataPlane（请求隔离）；流式消费者是**长驻**的，
只建**一次** plane 复用，让窗口缓冲/合并允许跨消息累积——这正是流式接入的意义
（连续事件能正确并入同一条候选）。候选分组状态在**共享后端**（`SharedCandidateBuilder`），
故多副本仍一致。

按租户分组 flush
----------------
一个流可能承载多个租户（订阅配置决定身份）。落库按租户分组：先按租户把消息分桶，
逐租户处理 → `build()`（只产出本租户刚触碰的分组）→ 用该租户身份落库。这样
每条候选都能被正确归属到租户，不会张冠李戴。

背压
----
`flush_max` 到量即落库，避免无限积累；拉式源（file/kafka）天然被 poll 量限流，
推式源（mqtt/http）队列满时丢弃并计数（见 `QueueSource.dropped`）。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from .base import StreamMessage, StreamSource

_log = logging.getLogger("data_dog.stream")

_PersistFn = Callable[[str, list[Any], list[Any]], None]


def _metric_consume_latency(msgs: list[StreamMessage]) -> None:
    """一批消息落库后，按"最早入队 → 现在"记消费延迟。

    取最早一条：它代表本批最长等待（头部延迟），比取平均更能暴露积压。
    桶内无产出（全部被拒）也记——被拒同样是"处理完成"，其延迟也应可见。
    """
    if not msgs:
        return
    stamps = [m.enqueued_at for m in msgs if m.enqueued_at]
    if not stamps:
        return
    try:
        from ..core import metrics
        metrics.stream_consume_latency_seconds.observe(time.monotonic() - min(stamps))
    except Exception:                                       # noqa: BLE001
        pass


def _metric_messages(source_name: str, n: int, source: Any) -> None:
    """记录收消息 + 队列丢弃（背压证据）；指标不可用时静默跳过。"""
    try:
        from ..core import metrics
        metrics.stream_messages_total.labels(source=source_name).inc(n)
        dropped = getattr(source, "dropped", 0)
        tracked = getattr(source, "_metric_dropped", 0)
        if dropped > tracked:                            # 只记增量
            metrics.stream_dropped_total.labels(source=source_name).inc(
                dropped - tracked)
            source._metric_dropped = dropped
    except Exception:                                    # noqa: BLE001
        pass


def _metric_candidates(n: int) -> None:
    try:
        from ..core import metrics
        metrics.stream_candidates_total.inc(n)
    except Exception:                                    # noqa: BLE001
        pass


def _metric_late(builder: Any, seen: dict[str, Any]) -> None:
    """把候选构建器的迟到计数按**增量**记进 Prometheus（G2）。

    静默丢迟到数据是最难排查的错误，故 `late_rejected`（早于水位线被拒）与
    `late_merged`（容忍窗内并入）都必须可见。
    """
    try:
        from ..core import metrics
        for attr, counter in (("late_merged", metrics.late_merged_total),
                              ("late_rejected", metrics.events_late_total)):
            cur = int(getattr(builder, attr, 0) or 0)
            delta = cur - int(seen.get(attr, 0))
            if delta > 0:
                counter.labels(source="stream").inc(delta)
                seen[attr] = cur
    except Exception:                                    # noqa: BLE001
        pass


def _metric_data_plane(plane: Any, seen: dict[str, Any]) -> None:
    """把**长驻** plane 的累计统计按**增量**记进 Prometheus 指标。

    plane 复用整个进程生命周期，其 `stats` 是累计值；指标是累加计数器，故
    只能记"相对上次的增量"，否则每 flush 一次就把历史全部重记一遍（数值暴涨）。
    """
    try:
        from ..core import metrics
        st = plane.stats
        for k in ("ingested", "resolved", "rejected"):
            cur = int(getattr(st, k, 0))
            delta = cur - int(seen.get(k, 0))
            if delta > 0:
                metrics.telemetry_events_total.labels(result=k).inc(delta)
                seen[k] = cur
        reasons = getattr(st, "rejected_by_reason", {}) or {}
        rseen = seen.setdefault("_reasons", {})
        for reason, n in reasons.items():
            delta = int(n) - int(rseen.get(reason, 0))
            if delta > 0:
                metrics.telemetry_rejected_by_reason.labels(reason=reason).inc(delta)
                if reason == "rate_limited":
                    metrics.rate_limited_total.inc(delta)
                rseen[reason] = int(n)
    except Exception:                                    # noqa: BLE001
        pass


class StreamRunner:
    """把源的消息喂给数据面并落库。`run()` 阻塞直到 `stop` 置位。"""

    def __init__(self, plane: Any, source: StreamSource, *,
                 persist: _PersistFn | None = None, flush_max: int = 200,
                 default_tenant: str = "") -> None:
        self.plane = plane
        self.source = source
        self._persist_fn = persist
        self.flush_max = max(1, flush_max)
        self._default_tenant = default_tenant
        self._pending: dict[str, list[StreamMessage]] = defaultdict(list)
        self.stats = {"messages": 0, "normalized": 0, "rejected": 0,
                      "candidates": 0, "flushes": 0, "errors": 0,
                      "poll_errors": 0}
        # 长驻 plane 累计统计的"上次已记"快照（按增量转成 Prometheus 计数）
        self._metric_seen: dict[str, Any] = {}

    # ---- 落库 ----
    def _persist(self, tenant: str, norms: list[Any], cands: list[Any]) -> None:
        if self._persist_fn is not None:
            self._persist_fn(tenant, norms, cands)
            return
        if not norms and not cands:
            return
        from ..storage import repository as repo
        from ..storage.db import session_scope
        with session_scope(tenant_id=tenant or None) as s:
            if norms:
                repo.save_telemetry(s, norms, tenant_id=tenant)
            if cands:
                repo.save_candidates(s, cands, tenant_id=tenant)

    # ---- 一批消息 ----
    def _drain(self, msgs: list[StreamMessage]) -> None:
        self.stats["messages"] += len(msgs)
        now = time.monotonic()
        for msg in msgs:
            # 源未打点（enqueued_at=0）时补当前时刻，保证消费延迟指标有基线可测；
            # 源已打点则尊重源侧更准的"到达"时刻。
            if not msg.enqueued_at:
                msg = StreamMessage(payload=msg.payload, auth=msg.auth,
                                    source=msg.source, enqueued_at=now)
            tenant = str((msg.auth or {}).get("tenant_id") or self._default_tenant)
            self._pending[tenant].append(msg)
        _metric_messages(self.source.name, len(msgs), self.source)
        if sum(len(v) for v in self._pending.values()) >= self.flush_max:
            self.flush()

    def flush(self) -> None:
        """按租户处理并落库所有待处理消息。"""
        pending, self._pending = self._pending, defaultdict(list)
        for tenant, msgs in pending.items():
            norms: list[Any] = []
            for msg in msgs:
                try:
                    norm, err = self.plane.process_one(msg.payload, msg.auth)
                except Exception as e:                      # noqa: BLE001
                    # 单条异常不中断整条流（失败关闭：该条丢弃并计数）
                    self.stats["errors"] += 1
                    _log.warning("流式处理单条失败", extra={
                        "error": f"{type(e).__name__}: {e}", "source": msg.source})
                    continue
                if norm is not None:
                    norms.append(norm)
                elif err is not None:
                    # 被网关/解析拒绝（缺身份、限流、契约不符…）：计数，别静默丢
                    self.stats["rejected"] += 1
            # 把本批数据面统计并入全局指标（否则流式侧的拒绝在 /metrics 不可见）
            _metric_data_plane(self.plane, self._metric_seen)
            _metric_late(self.plane.builder, self._metric_seen)
            cands = self.plane.builder.build()             # only_touched
            self.stats["normalized"] += len(norms)
            self.stats["candidates"] += len(cands)
            if cands:
                _metric_candidates(len(cands))
            try:
                self._persist(tenant, norms, cands)
            except Exception as e:                          # noqa: BLE001
                self.stats["errors"] += 1
                _log.error("流式落库失败", extra={
                    "error": f"{type(e).__name__}: {e}", "tenant_id": tenant})
            # 消费延迟：本桶最早一条的入队时刻 → 此刻落库完成
            _metric_consume_latency(msgs)
            self.stats["flushes"] += 1

    # ---- 主循环 ----
    def run(self, *, stop: threading.Event, poll_timeout: float = 1.0,
            max_iters: int | None = None) -> None:
        """轮询源直到 `stop` 置位。`max_iters` 仅测试用（有界迭代便于断言）。"""
        iters = 0
        while not stop.is_set():
            try:
                msgs = self.source.poll(poll_timeout)
            except Exception as e:                          # noqa: BLE001
                # 源断开（MQTT 掉线/kafka 超时）不致命：记数后继续下一轮重试
                self.stats["poll_errors"] += 1
                _log.warning("流式源轮询失败", extra={
                    "error": f"{type(e).__name__}: {e}"})
                time.sleep(min(1.0, poll_timeout))
                msgs = []
            if msgs:
                self._drain(msgs)
            elif self._pending:
                # 源安静（本轮无新消息）而暂存区非空 → 立即落库。
                # 否则低流量下要凑满 flush_max 才落库，候选会长时间可见性缺失
                # （"收到消息却查不到候选"——最难排查的静默延迟）。
                self.flush()
            iters += 1
            if max_iters is not None and iters >= max_iters:
                break
        self.flush()                                        # 收尾：清空残留

    def start_background(self, *, poll_timeout: float = 1.0
                         ) -> tuple[threading.Thread, threading.Event]:
        """在后台线程跑 `run()`，返回 (线程, stop 事件)。供服务启动时挂载。"""
        stop = threading.Event()
        t = threading.Thread(target=self.run, kwargs={
            "stop": stop, "poll_timeout": poll_timeout},
            name="dd-stream-runner", daemon=True)
        t.start()
        return t, stop
