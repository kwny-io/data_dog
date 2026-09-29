"""异步调查的**执行侧**（G1）：job 生命周期 + worker 池。

为什么需要它
------------
`POST /v1/investigations` 此前同步阻塞整个请求（真实 LLM 可达 10–60s），占满 worker
线程，端到端水平扩展卡在这一处。本模块把"提交"与"执行"拆开：提交立即返回一个可
轮询的 `job_id`，执行交给后台 worker 池。

并发正确性（关键）
------------------
两个层次保证**同一个 job 不会被两个 worker 重复调查**：
1. **共享队列**：待办 job 存在 `StateStore`（`state_kv` / PG / Redis）的全局队列里，
   多副本各自 `mutate` 原子取件——一个 job 只被一个副本取走。
2. **条件领取**：真正开工前用 `repo.claim_job`（`UPDATE ... WHERE status='pending'`）
   原子把 pending→running。即使队列因任何原因给了同一 job 两次，数据库的条件更新
   也只让一个成功（rowcount=1），其余返回 False 而跳过。
   这一层是**最终防线**——即使共享队列语义在某个后端上退化，也不会重复执行。

诚实边界
--------
- **进程内线程池**：worker 池是本进程的线程；多副本靠上面两层共享语义协作，**不是**
  分布式调度器。要更强保证需外部队列（Celery/RQ/Kafka）。
- **取消是协作式**：不强制杀线程。取消后 worker 在**步边界**（每次 LLM 调用前）看到
  并停止；一次已发出的 LLM 调用要等它返回。这与既有 `_DeadlineProvider` 的超时语义一致。
- **崩在取件与领取之间**：若副本取走 job 后、领取前崩溃，该 job 会停留在 pending。
  这是已知窗口（见 docs/p4/gaps/01_async_investigation.md）；生产应定期扫 pending
  重投（运维文档记）。本模块不内置分布式恢复。
"""
from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..state import get_state_store
from ..state.base import StateStore

_log = logging.getLogger(__name__)

# 共享待办队列在 StateStore 里的 key（全局，无租户维度——worker 需跨租户取件）
_QUEUE_KEY = "investigation:queue"
_MAX_BATCH = 16


def make_job_id(tenant_id: str, candidate_id: str) -> str:
    """生成 job id。带随机数——同一候选可被重复调查（不同 job），不应撞 id。"""
    return f"job_{uuid.uuid4().hex[:16]}"


class SharedJobQueue:
    """建立在 `StateStore.mutate` 之上的**跨副本待办队列**。

    存的是 job 引用（tenant_id + job_id + candidate_id），不是产物。取件即从队列
    移除——配合 `repo.claim_job` 的条件领取，保证不重复执行。
    """

    def __init__(self, state: StateStore | None = None) -> None:
        self._state = state or get_state_store()

    def push(self, entry: dict[str, Any]) -> None:
        def fn(cur: Any) -> tuple[Any, Any]:
            items = list(cur.get("items", [])) if isinstance(cur, dict) else []
            items.append(entry)
            return {"items": items}, None

        self._state.mutate(_QUEUE_KEY, fn)

    def pop_batch(self, n: int = 1) -> list[dict[str, Any]]:
        """原子取走队首至多 n 个条目。无则返回空列表。"""
        def fn(cur: Any) -> tuple[Any, Any]:
            items = list(cur.get("items", [])) if isinstance(cur, dict) else []
            taken, rest = items[:n], items[n:]
            return {"items": rest}, taken

        return list(self._state.mutate(_QUEUE_KEY, fn) or [])

    def depth(self) -> int:
        cur = self._state.get(_QUEUE_KEY)
        if isinstance(cur, dict):
            return len(cur.get("items", []) or [])
        return 0


class JobRunner:
    """后台 worker 池：从共享队列取件 → 条件领取 → 执行 → 落终态。

    `run_fn` 契约：`run_fn(tenant_id, candidate_id, should_cancel) -> dict`，返回
    `{"status": "succeeded"|"failed", "report_id": str|None, "error_code": str|None}`。
    把执行逻辑注入进来，本模块只负责并发/生命周期，不耦合 provider/AgentLoop。
    """

    def __init__(self, run_fn: Callable[..., dict[str, Any]], *,
                 workers: int = 4, queue: SharedJobQueue | None = None,
                 poll_interval_s: float = 0.2) -> None:
        self._run_fn = run_fn
        self._workers = max(1, int(workers))
        self._queue = queue or SharedJobQueue()
        self._poll_s = poll_interval_s
        self._executor: ThreadPoolExecutor | None = None
        self._stop = threading.Event()
        self._futures: list[Any] = []
        # 每个 worker 线程用独立 worker_id，便于日志定位"谁在跑这个 job"
        self._worker_ids: dict[int, str] = {}

    # ---- 提交 ---------------------------------------------------------

    def submit(self, *, tenant_id: str, candidate_id: str,
               job_id: str) -> None:
        """把 job 放进共享队列（job 行已由调用方落库为 pending）。"""
        self._queue.push({"tenant_id": tenant_id, "job_id": job_id,
                          "candidate_id": candidate_id})

    # ---- 生命周期 -----------------------------------------------------

    def start(self) -> None:
        if self._executor is not None:
            return
        self._stop.clear()
        self._executor = ThreadPoolExecutor(
            max_workers=self._workers, thread_name_prefix="dd-investigate")
        for i in range(self._workers):
            self._futures.append(self._executor.submit(self._worker_loop, i))
        _log.info("调查 worker 池已启动", extra={"workers": self._workers})

    def stop(self, *, wait: bool = False) -> None:
        self._stop.set()
        ex = self._executor
        self._executor = None
        if ex is not None:
            ex.shutdown(wait=wait, cancel_futures=False)
        _log.info("调查 worker 池已停止")

    @property
    def alive(self) -> bool:
        return self._executor is not None and not self._stop.is_set()

    def queue_depth(self) -> int:
        try:
            return self._queue.depth()
        except Exception:                             # noqa: BLE001
            return -1

    # ---- worker 循环 --------------------------------------------------

    def _worker_loop(self, idx: int) -> None:
        worker_id = f"{threading.current_thread().name}-{idx}"
        self._worker_ids[threading.get_ident()] = worker_id
        while not self._stop.is_set():
            batch = self._pop(batch_size=self._workers)
            if not batch:
                # 无活可干：短睡后再查（不忙等）
                self._stop.wait(self._poll_s)
                continue
            for entry in batch:
                if self._stop.is_set():
                    break
                self._handle(entry, worker_id)

    def _pop(self, *, batch_size: int) -> list[dict[str, Any]]:
        try:
            return self._queue.pop_batch(min(_MAX_BATCH, max(1, batch_size)))
        except Exception as e:                        # noqa: BLE001
            _log.warning("取件失败（共享队列不可用）",
                         extra={"error": f"{type(e).__name__}: {e}"})
            self._stop.wait(self._poll_s)
            return []

    def _handle(self, entry: dict[str, Any], worker_id: str) -> None:
        from ..storage import repository as repo
        from ..storage.db import session_scope

        tenant_id = entry.get("tenant_id", "")
        job_id = entry.get("job_id", "")
        candidate_id = entry.get("candidate_id", "")
        if not tenant_id or not job_id:
            return

        # 条件领取：pending → running。抢不到（已被别处领/已取消）→ 跳过。
        try:
            with session_scope(tenant_id=tenant_id) as s:
                claimed = repo.claim_job(s, job_id, tenant_id=tenant_id,
                                         worker_id=worker_id)
        except Exception as e:                        # noqa: BLE001
            _log.warning("领取 job 失败",
                         extra={"job_id": job_id,
                                "error": f"{type(e).__name__}: {e}"})
            return
        if not claimed:
            return

        def should_cancel() -> bool:
            # 协作式取消：任何时刻 job 不再是 running（被 cancel 置为 cancelled）
            # 即表示应停。查库而非查内存，故跨副本取消也生效。
            try:
                with session_scope(tenant_id=tenant_id) as s:
                    row = repo.get_job(s, job_id, tenant_id=tenant_id)
                return row is None or row.get("status") != "running"
            except Exception:                         # noqa: BLE001
                return False                          # 查不到不误杀，交给超时兜底

        try:
            result = self._run_fn(tenant_id, candidate_id, should_cancel)
        except Exception as e:                        # noqa: BLE001
            _log.error("调查执行异常",
                       extra={"job_id": job_id,
                              "error": f"{type(e).__name__}: {e}"})
            result = {"status": "failed", "report_id": None,
                      "error_code": "execution_failed"}

        # 落终态。若是被取消（cancelled）→ finish_job 的条件更新会拒绝
        # （只有 running 能到 succeeded/failed），cancelled 不被覆盖。
        try:
            with session_scope(tenant_id=tenant_id) as s:
                repo.finish_job(
                    s, job_id, tenant_id=tenant_id,
                    status=result.get("status", "failed"),
                    report_id=result.get("report_id"),
                    error_code=result.get("error_code"))
        except Exception as e:                        # noqa: BLE001
            _log.error("写 job 终态失败",
                       extra={"job_id": job_id,
                              "error": f"{type(e).__name__}: {e}"})
