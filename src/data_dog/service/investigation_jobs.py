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
- **报告与终态同事务**（已修）：此前报告在调查实现里单独提交、异常还被吞掉，会出现
  "job=succeeded 但报告缺失"。现在两件事在**一个事务**里做（见 `_finish`）。
- **卡死窗口已由恢复扫描兜住**（已修）：取件后/领取前崩溃 → job 停在 pending；
  完成写回前进程被杀 → 停在 running。`recover_stale()` 定期把过旧的 pending
  **重新入队**（幂等）、把过旧的 running 标 `failed(abandoned)`（**不自动重跑**，
  避免慢但活着的 worker 与重跑并发）。**边界**：判死依赖时间阈值，阈值须显著大于
  最坏单次耗时；跨副本不保证"只扫一次"（幂等即可）。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .. import settings
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
    `{"status": "succeeded"|"failed"|"cancelled", "report_id": str|None,
      "error_code": str|None, "report": <报告对象，仅 succeeded 且未落库时>}`。
    把执行逻辑注入进来，本模块只负责并发/生命周期，不耦合 provider/AgentLoop。

    **报告与终态在同一个事务里落库**（见 `_finish`）：此前 `save_report` 在调查实现
    里单独提交、异常还被吞掉，于是可能出现"job=succeeded 但报告查不到"——查得到
    任务、查不到产物，是最难解释的一类不一致。
    """

    def __init__(self, run_fn: Callable[..., dict[str, Any]], *,
                 workers: int = 4, queue: SharedJobQueue | None = None,
                 poll_interval_s: float = 0.2,
                 recover_interval_s: float | None = None) -> None:
        self._run_fn = run_fn
        self._workers = max(1, int(workers))
        self._queue = queue or SharedJobQueue()
        self._poll_s = poll_interval_s
        self._recover_s = (settings.investigate_recover_interval_s()
                           if recover_interval_s is None else float(recover_interval_s))
        self._executor: ThreadPoolExecutor | None = None
        self._stop = threading.Event()
        self._futures: list[Any] = []
        # 每个 worker 线程用独立 worker_id，便于日志定位"谁在跑这个 job"
        self._worker_ids: dict[int, str] = {}
        self.recovered: dict[str, int] = {"requeued": 0, "abandoned": 0, "errors": 0}

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
        last_recover = time.monotonic()
        while not self._stop.is_set():
            # 恢复扫描：只让 0 号 worker 做（进程内一次就够；多副本各自扫也没关系，
            # 见 recover_stale 的幂等性说明）
            if idx == 0 and time.monotonic() - last_recover >= self._recover_s:
                last_recover = time.monotonic()
                self.recover_stale()
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

        self._finish(tenant_id=tenant_id, job_id=job_id, result=result)

    def _finish(self, *, tenant_id: str, job_id: str,
                result: dict[str, Any]) -> None:
        """落终态：**报告 + job 状态在同一个事务里**（要么都成，要么都不成）。

        顺序刻意是"先条件终态、后写产物"：`finish_job` 是
        `UPDATE ... WHERE status='running'`，返回 False 说明 job 已不是 running
        （典型是已被取消）——协作式取消的语义要求**不提交产物**，故此时跳过报告。

        事务整体失败（如报告落库报错）时该事务已回滚，job 会停在 `running`；这里
        **再补一次显式失败**（`persist_failed`），不让它挂在那儿等人扫。恢复扫描是
        兜底，不该当主路径。
        """
        from ..storage import repository as repo
        from ..storage.db import session_scope

        status = str(result.get("status", "failed"))
        report = result.get("report")
        # 运行日志与 job 终态**同事务**：run_id 就用 job_id（技术员手上的句柄），
        # 失败的 job 也写——"为什么失败"正是要事后调的。执行侧没带日志（比如它自己抛了
        # 异常）时在这里补一条骨架，保证"job 有终态 ⇔ 有运行日志"不被静默破坏。
        log = result.get("run_log")
        if not isinstance(log, dict):
            log = {"steps": [], "step_count": 0, "usage": {}, "duration_ms": 0.0,
                   "model": "", "candidate_id": str(result.get("candidate_id", ""))}
        log = {**log, "run_id": job_id, "kind": "async", "status": status,
               "report_id": result.get("report_id"),
               "error_code": result.get("error_code")}
        try:
            with session_scope(tenant_id=tenant_id) as s:
                applied = repo.finish_job(
                    s, job_id, tenant_id=tenant_id, status=status,
                    report_id=result.get("report_id"),
                    error_code=result.get("error_code"))
                if applied and status == "succeeded" and report is not None:
                    repo.save_report(s, report, tenant_id=tenant_id)
                if applied:
                    repo.save_run_log(s, log, tenant_id=tenant_id)
        except Exception as e:                        # noqa: BLE001
            _log.error("job 终态与报告同事务落库失败（已回滚）",
                       extra={"job_id": job_id,
                              "error": f"{type(e).__name__}: {e}"})
            try:
                with session_scope(tenant_id=tenant_id) as s:
                    repo.finish_job(s, job_id, tenant_id=tenant_id,
                                    status="failed", error_code="persist_failed")
            except Exception as e2:                   # noqa: BLE001
                _log.error("补记 job 失败也失败（留给恢复扫描）",
                           extra={"job_id": job_id,
                                  "error": f"{type(e2).__name__}: {e2}"})

    # ---- 卡死恢复 -----------------------------------------------------

    def recover_stale(self, *, pending_after_s: float | None = None,
                      running_after_s: float | None = None,
                      limit: int | None = None) -> dict[str, int]:
        """恢复扫描：把**卡住**的 job 拉回正轨。返回本轮统计。

        两类卡死（此前只写在文档里的窗口）：
        - `pending` 且过旧 → **重新入队**。成因是"取件后、条件领取前崩溃"，
          job 在库里但不在队列里，永远不会被执行。重新入队是幂等的：真开工前有
          条件领取（`UPDATE ... WHERE status='pending'`），重复条目只会被跳过。
        - `running` 且过旧 → 置 `failed`（`error_code=abandoned`），**不自动重跑**。
          因为"慢但还活着"的 worker 无法与"已经死了"区分；重跑有让同一 job 被两个
          worker **并发执行**的风险（那会重复产生 LLM 花费与报告）。判死至少是
          可解释的，而且把"这次没成"如实写在 job 上。

        多副本可并发调用：入队幂等、终态是条件更新（`finish_job` 只接受 running）。
        跨租户扫描必须走 `session_scope(system=True)` —— 否则 RLS 下零行可见
        （见 `storage/db.py` 的说明与迁移 `b7c8d9e0f1a2`）。
        """
        from ..storage import repository as repo
        from ..storage.db import session_scope

        pend = (settings.investigate_stale_pending_s()
                if pending_after_s is None else float(pending_after_s))
        run = (settings.investigate_stale_running_s()
               if running_after_s is None else float(running_after_s))
        cap = settings.investigate_recover_batch() if limit is None else int(limit)
        stats = {"requeued": 0, "abandoned": 0, "errors": 0}

        try:
            with session_scope(system=True) as s:
                stale_pending = repo.list_stale_jobs(
                    s, status="pending", older_than_s=pend, limit=cap)
        except Exception as e:                        # noqa: BLE001
            _log.warning("恢复扫描（pending）失败",
                         extra={"error": f"{type(e).__name__}: {e}"})
            stats["errors"] += 1
            stale_pending = []
        for row in stale_pending:
            try:
                self._queue.push({"tenant_id": row["tenant_id"],
                                  "job_id": row["job_id"],
                                  "candidate_id": row["candidate_id"]})
                stats["requeued"] += 1
            except Exception as e:                    # noqa: BLE001
                _log.warning("重新入队失败", extra={
                    "job_id": row.get("job_id"),
                    "error": f"{type(e).__name__}: {e}"})
                stats["errors"] += 1

        try:
            with session_scope(system=True) as s:
                stale_running = repo.list_stale_jobs(
                    s, status="running", older_than_s=run, limit=cap)
        except Exception as e:                        # noqa: BLE001
            _log.warning("恢复扫描（running）失败",
                         extra={"error": f"{type(e).__name__}: {e}"})
            stats["errors"] += 1
            stale_running = []
        for row in stale_running:
            try:
                with session_scope(tenant_id=row["tenant_id"]) as s:
                    if repo.finish_job(s, row["job_id"],
                                       tenant_id=row["tenant_id"],
                                       status="failed",
                                       error_code="abandoned"):
                        stats["abandoned"] += 1
                        _log.warning("僵死 job 已标记失败（不自动重跑）", extra={
                            "job_id": row.get("job_id"),
                            "tenant_id": row.get("tenant_id"),
                            "worker_id": row.get("worker_id", "")})
            except Exception as e:                    # noqa: BLE001
                _log.warning("标记僵死 job 失败", extra={
                    "job_id": row.get("job_id"),
                    "error": f"{type(e).__name__}: {e}"})
                stats["errors"] += 1

        for k, v in stats.items():
            self.recovered[k] = self.recovered.get(k, 0) + v
        if stats["requeued"] or stats["abandoned"] or stats["errors"]:
            _log.info("异步 job 恢复扫描", extra=stats)
        return stats
