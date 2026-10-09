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
import random
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .base import StreamMessage, StreamSource

_log = logging.getLogger("data_dog.stream")

_PersistFn = Callable[[str, list[Any], list[Any]], None]

# ---- 源轮询失败的重试预算（**次数 + 墙钟双上限 + 抖动退避**）----
#
# 与 `state/redis.py` 的 `mutate()` 同一口径：次数与时间**先到为准**，两次尝试之间
# 用抖动的指数退避错开；任一上限耗尽即**显式终止**，绝不无限静默重试。
#
# 为什么源断开也要有终点：MQTT 掉线/kafka 滚动重启确实该重试，但"永远重试"会让
# 一个**配置写错的源地址**看起来和"暂时抖动"一模一样——消费永远不前进、日志每轮
# 刷一条、也没有任何"我已放弃"的信号。运维要的是：短期抖动自动恢复，长期故障
# **停下来报警**。故默认允许 30 次 / 120s（≈2 分钟内自愈，够覆盖重启与重连），
# 超了就停并把原因写进 `stats`（`/healthz` 直接可见），由人介入。
_POLL_MAX_CONSECUTIVE_FAILURES = 30
_POLL_FAILURE_BUDGET_S = 120.0
_POLL_BACKOFF_BASE_S = 0.05
_POLL_BACKOFF_MAX_S = 2.0


@dataclass(frozen=True)
class PollRetryPolicy:
    """源轮询失败的重试预算：次数与墙钟双上限（先到为准）+ 抖动退避。

    退避**不随 `poll_timeout` 变化**：`poll_timeout` 是"正常轮询一次的阻塞时长"，
    与"故障后该等多久再试"是两个语义；混用会让"把 poll_timeout 调大以降低空转"
    意外变成"故障时也要等很久"。
    """

    max_consecutive: int = _POLL_MAX_CONSECUTIVE_FAILURES
    budget_s: float = _POLL_FAILURE_BUDGET_S
    base_sleep_s: float = _POLL_BACKOFF_BASE_S
    max_sleep_s: float = _POLL_BACKOFF_MAX_S

    def backoff_s(self, consecutive: int, *,
                  rand: Callable[[], float] = random.random) -> float:
        """第 `consecutive` 次（从 1 起）失败后的退避：指数增长 + 单次上限 + 抖动。

        抖动取 `U(0.5, 1.5)`：多个副本/多个源同时掉线时错开重连时刻，避免恢复瞬间
        "整整齐齐"地一起打上去（惊群）。
        """
        n = max(1, int(consecutive))
        nominal = min(max(0.0, self.max_sleep_s),
                      self.base_sleep_s * (2 ** min(n - 1, 6)))
        return nominal * (0.5 + rand())

    def exhausted(self, *, consecutive: int, elapsed_s: float) -> bool:
        """次数或墙钟任一耗尽 → True（先到为准）。"""
        return consecutive >= max(1, int(self.max_consecutive)) \
            or elapsed_s >= self.budget_s


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


def _metric_shard(stats: dict[str, Any], seen: dict[str, Any]) -> None:
    """把分片跳过计数按**增量**记进 Prometheus（P4-5）。

    分片过滤"让路"掉的消息必须可见——若某副本的 key 判定与集群不一致，会表现为
    异常高的 shard_skipped（本副本全让路、没人接手）。增量记法避免重复累加。
    """
    try:
        from ..core import metrics
        cur = int(stats.get("shard_skipped", 0))
        delta = cur - int(seen.get("shard_skipped", 0))
        if delta > 0:
            metrics.shard_skipped_total.inc(delta)
            seen["shard_skipped"] = cur
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
                 default_tenant: str = "", router: Any = None,
                 poll_retry: PollRetryPolicy | None = None) -> None:
        self.plane = plane
        self.source = source
        self._persist_fn = persist
        self.flush_max = max(1, flush_max)
        self._default_tenant = default_tenant
        # 分片路由（P4-5）：非空且启用时，**只在"每个副本都收全量"的源**上过滤——
        # 本副本只处理归属自己的键，其余交给属主副本。默认 None（不过滤）。
        self._router = router
        # 源轮询失败的重试预算（次数 + 墙钟 + 抖动）；见 PollRetryPolicy。
        self._poll_retry = poll_retry or PollRetryPolicy()
        self._pending: dict[str, list[StreamMessage]] = defaultdict(list)
        # stop_reason：run() 的退出原因（"stopped" / "max_iters" / "source_unavailable"）。
        # 放进 stats 是刻意的——service/routes/health.py 直接回吐 runner.stats，
        # 于是"因源不可用而停"在不改任何路由的情况下就可见了。
        # 注解成 Any（而非让 mypy 推断成 dict[str, int]）：stats 里既有计数器也有
        # 字符串状态（stop_reason），混型是刻意的。
        self.stats: dict[str, Any] = {
            "messages": 0, "normalized": 0, "rejected": 0,
            "candidates": 0, "flushes": 0, "errors": 0,
            "poll_errors": 0, "shard_skipped": 0,
            "poll_failures_exhausted": 0, "stop_reason": ""}
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

    # ---- 分片归属（P4-5）----
    def _owns(self, msg: StreamMessage, tenant: str) -> bool:
        """本副本是否该处理这条消息。无 router / router 未启用 → 恒真（不过滤）。"""
        if self._router is None:
            return True
        from .sharding import shard_key
        device = str((msg.auth or {}).get("device_sn") or "")
        key = shard_key(tenant_id=tenant, device_sn=device)
        return bool(self._router.owns(key))

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
            # 分片过滤：不归本副本的键**不改动**——交给属主副本消费。
            # 同设备所有事件恒落同一键 → 恒去同一副本，窗口状态不分裂。
            if not self._owns(msg, tenant):
                self.stats["shard_skipped"] += 1
                continue
            self._pending[tenant].append(msg)
        _metric_messages(self.source.name, len(msgs), self.source)
        _metric_shard(self.stats, self._metric_seen)
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
        """轮询源直到 `stop` 置位。`max_iters` 仅测试用（有界迭代便于断言）。

        退出条件（三种，都会写进 `stats["stop_reason"]`）：
        - `stop` 置位 → `"stopped"`（正常关停）；
        - `max_iters` 用尽 → `"max_iters"`（仅测试）；
        - **轮询重试预算耗尽** → `"source_unavailable"`（源长期不可用）。

        为什么预算耗尽时**返回**而不是抛异常：`run()` 的调用方有两个都在"长驻线程"
        里（`start_background` 起的守护线程、`service/stream_worker.py` 的 CLI）。
        抛异常只会让守护线程带着 traceback 静默死掉，且会跳过收尾 `flush()`
        （残留候选不落库）。改成"停下来 + 把原因写进 stats"后：调用方不需要改一行，
        而 `/healthz`（直接回吐 `runner.stats`）能立刻看到 `stop_reason` 与
        `poll_failures_exhausted`——这就是本仓要的"可观测的放弃"，不是无限静默重试。
        """
        iters = 0
        consecutive = 0
        first_failure_at: float | None = None
        while not stop.is_set():
            try:
                msgs = self.source.poll(poll_timeout)
            except Exception as e:                          # noqa: BLE001
                consecutive += 1
                now = time.monotonic()
                if first_failure_at is None:
                    first_failure_at = now
                elapsed = now - first_failure_at
                self.stats["poll_errors"] += 1
                _log.warning("流式源轮询失败", extra={
                    "error": f"{type(e).__name__}: {e}",
                    "consecutive": consecutive, "elapsed_s": round(elapsed, 3)})
                if self._poll_retry.exhausted(consecutive=consecutive,
                                              elapsed_s=elapsed):
                    # 次数/墙钟任一耗尽 → 显式放弃（不无限重试），原因落进 stats。
                    self.stats["poll_failures_exhausted"] += 1
                    self.stats["stop_reason"] = "source_unavailable"
                    _log.error("流式源不可用：轮询重试预算耗尽，已停止消费", extra={
                        "error": f"{type(e).__name__}: {e}",
                        "consecutive": consecutive,
                        "elapsed_s": round(elapsed, 3)})
                    break
                # 抖动退避：错开多副本的重连时刻；下一次成功即清零（见 else 分支）
                time.sleep(self._poll_retry.backoff_s(consecutive))
                msgs = []
            else:
                consecutive = 0                             # 成功 → 预算重新开始
                first_failure_at = None
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
        if not self.stats["stop_reason"]:
            self.stats["stop_reason"] = "stopped" if stop.is_set() else "max_iters"
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
