
"""写路径 write-behind：把业务落库移出请求路径（**默认关闭**，见 `settings.write_behind`）。

为什么需要它
-----------
`docs/write_path_batching.md` 把热路径 SLO（P99 ≤ 500ms）未达标定位在"每请求的数据库
往返 + 同步提交排队"上。`POST /v1/telemetry` 的落库（遥测 + 候选，一个事务）是请求
路径上**最后一段同步 I/O**：必须在响应前完成，于是并发下每个请求都被"等一次提交"
钉住。write-behind 把这段写进**有界队列**，由后台线程落库，请求立即返回。

⚠️ 语义代价（这正是它默认关闭的原因）
-------------------------------------
开启后 **HTTP 200 不再代表已落库**：
- **队列满 → 503 失败关闭**（不静默丢；调用方可重试）。
- **后台写失败 → error 日志 + `data_dog_write_behind_total{result="failed"}`**，
  但**已返回的 200 无法追回**——这段数据丢了。
- **进程被杀 / 容器重启 → 队列里未落库的数据丢失**（队列只在进程内）。
- 多副本各自一个队列，**互不共享**；某副本挂了，它队列里的数据不会由别的副本续写。

所以它是**用持久性换延迟**的显式取舍，不是"更快的落库"。只在"延迟是主要矛盾、
且可容忍少量丢失"的部署里开，并**必须**对
`data_dog_write_behind_total{result=~"failed|rejected_full"}` 告警。

实现要点
--------
- **有界队列 + 单个后台线程**：落库事务串行，无并发争用。
- **租户显式传入**：后台线程没有请求的 ContextVar，必须 `session_scope(tenant_id=...)`
  显式注入 RLS 租户——否则 PG 上会**静默写 0 行**。
- **停机排空**：`stop()` 先置停止位，再把队列里剩余写完后退出（带上限，不挂死）。
"""
from __future__ import annotations

import queue
import threading

from .. import settings
from ..core import metrics
from ..core.logging import get_logger

_log = get_logger(__name__)

_QUEUE_GET_TIMEOUT_S = 0.2


class WriteBehindWriter:
    """有界队列 + 单后台线程：把 (tenant, 遥测, 候选) 落库移出请求路径。"""

    def __init__(self, *, maxsize: int = 1000) -> None:
        self._q: queue.Queue = queue.Queue(maxsize=max(1, int(maxsize)))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.stats = {"enqueued": 0, "written": 0, "failed": 0, "rejected_full": 0}

    # ---- 生命周期 ----
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="dd-write-behind",
                                        daemon=True)
        self._thread.start()

    def stop(self, drain_timeout_s: float = 10.0) -> None:
        """停止接受新任务，尽力把队列排空后退出线程（不保证一定排空）。"""
        self._stop.set()
        t = self._thread
        if t is None:
            return
        t.join(timeout=max(0.0, drain_timeout_s))
        self._thread = None
        if t.is_alive():
            _log.warning("write-behind 线程未在排空期限内退出（仍有未落库数据）",
                         extra={"queue_depth": self._q.qsize(),
                                "queued": self.stats["enqueued"],
                                "written": self.stats["written"]})

    # ---- 提交（请求路径调用）----
    def submit(self, *, tenant_id: str, normalized: list, candidates: list) -> bool:
        """入队。返回 False = 队列满（调用方须**失败关闭**，不能假装成功）。"""
        try:
            self._q.put_nowait((tenant_id, normalized, candidates))
        except queue.Full:
            self.stats["rejected_full"] += 1
            metrics.write_behind_total.labels(result="rejected_full").inc()
            return False
        self.stats["enqueued"] += 1
        metrics.write_behind_total.labels(result="enqueued").inc()
        metrics.write_behind_queue_depth.set(self._q.qsize())
        return True

    # ---- 后台 ----
    def _run(self) -> None:
        while True:
            try:
                item = self._q.get(timeout=_QUEUE_GET_TIMEOUT_S)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            try:
                self._persist(*item)
                self.stats["written"] += 1
                metrics.write_behind_total.labels(result="written").inc()
            except Exception as e:                    # noqa: BLE001
                # 不重排重试：数据库持续不可用时重试只会空转；丢一条必须让指标/日志可见。
                self.stats["failed"] += 1
                metrics.write_behind_total.labels(result="failed").inc()
                _log.error("write-behind 落库失败（该批数据丢失）",
                           extra={"error": f"{type(e).__name__}: {e}"})
            finally:
                metrics.write_behind_queue_depth.set(self._q.qsize())

    @staticmethod
    def _persist(tenant_id: str, normalized: list, candidates: list) -> None:
        from ..storage import repository as repo
        from ..storage.db import session_scope
        # 后台线程没有请求上下文 → 必须显式带 tenant_id（RLS 下否则静默 0 行）
        with session_scope(tenant_id=tenant_id) as s:
            if normalized:
                repo.save_telemetry(s, normalized, tenant_id=tenant_id)
            if candidates:
                repo.save_candidates(s, candidates, tenant_id=tenant_id)


# ---- 进程内单例（由 app lifespan 启停；路由只读）----
_writer: WriteBehindWriter | None = None
_lock = threading.Lock()


def start_writer_if_enabled() -> WriteBehindWriter | None:
    """按 `DD_WRITE_BEHIND` 起后台写入线程。未启用返回 None（零行为变化）。"""
    if not settings.write_behind():
        return None
    global _writer
    with _lock:
        if _writer is None:
            w = WriteBehindWriter(maxsize=settings.write_behind_queue_size())
            w.start()                       # 起不来就不赋值：路由据此 503（失败关闭）
            _writer = w
            _log.info("write-behind 已启用：业务落库移出请求路径",
                      extra={"queue_size": settings.write_behind_queue_size()})
    return _writer


def get_writer() -> WriteBehindWriter | None:
    return _writer


def stop_writer(drain_timeout_s: float = 10.0) -> None:
    global _writer
    with _lock:
        w, _writer = _writer, None
    if w is not None:
        w.stop(drain_timeout_s=drain_timeout_s)
        _log.info("write-behind 已停止", extra={"stats": dict(w.stats)})
