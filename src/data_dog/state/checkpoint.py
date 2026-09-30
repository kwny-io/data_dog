"""状态快照 / 恢复 / 对账（G3：StateStore checkpoint）。

解决什么问题
------------
状态已外置到共享后端（`state_kv`）可跨副本共享，但**进程重启或状态淘汰后
无法确认/重建"上次算到哪"**。本模块提供：

- `write_checkpoint`：把状态词典（按 prefix 过滤）导出一份存 `state_checkpoint`。
- `restore_latest`：启动时从最新快照恢复（`overwrite=False` 只补缺失 key）。
- `diff_checkpoint`：快照 vs 当前状态差异（新增/变更/删除），用于验证恢复正确性、
  排查状态漂移。

语义边界（诚实，务必读）
------------------------
- **不是精确一次**：快照是**周期**的，两次快照之间重启会丢这段增量——RPO = 间隔
  （`DD_CHECKPOINT_INTERVAL_S`）。要更强需 offset+状态的事务性联动（大工程）。
- **导出非原子**：跨 key 的导出不是单个原子操作；导出期间有并发写时，各 key 可能
  取自不同瞬间。对"近似"状态够用（状态本身是合并窗口/令牌桶，容忍轻微时移）。
- **快照含租户数据**：`payload` 是**敏感数据**（可含各租户的分组状态），存储加密与
  访问控制属运营职责。运维端点由 `DD_OPS_TOKEN` 门控。
- **不替代持久化真相**：候选/报告的真相在 `candidates`/`reports` 表（已幂等落库）；
  快照只保证**算子内部状态**可续。
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Any

from ..storage import repository as repo
from ..storage.db import session_scope
from .base import StateStore

_log = logging.getLogger("data_dog.state.checkpoint")


def make_checkpoint_id(*, now: float | None = None, prefix: str = "") -> str:
    """生成时间有序的快照 id：`chk_<毫秒时间戳>_<prefix hash>`。

    时间戳在前 → 字符串排序即时间排序，便于取最新/按序清理。
    """
    ts = int((now if now is not None else time.time()) * 1000)
    h = hashlib.sha256(prefix.encode("utf-8")).hexdigest()[:8]
    return f"chk_{ts}_{h}"


def write_checkpoint(store: StateStore, *, prefix: str = "") -> dict[str, Any]:
    """导出一份状态快照并落库。返回元数据（不含 payload）。"""
    payload = store.snapshot(prefix=prefix)
    cid = make_checkpoint_id(prefix=prefix)
    with session_scope() as s:
        meta = repo.save_checkpoint(s, checkpoint_id=cid, prefix=prefix,
                                    payload=payload)
    meta.pop("payload", None)
    _log.info("状态快照已写入", extra={"checkpoint_id": cid, "n_keys": len(payload)})
    return meta


def restore_latest(store: StateStore, *, overwrite: bool = False
                   ) -> dict[str, Any]:
    """从最新快照恢复状态。返回 {restored, checkpoint_id, n_keys, skipped}。

    `overwrite=False` 时只补**缺失** key（不覆盖已有——重启后新写入的状态优先）。
    """
    with session_scope() as s:
        ckpt = repo.latest_checkpoint(s)
    if ckpt is None:
        return {"restored": 0, "checkpoint_id": None, "n_keys": 0, "skipped": True,
                "reason": "无可用快照"}
    payload = ckpt.get("payload") or {}
    written = store.restore(payload, overwrite=overwrite)
    _log.info("已从快照恢复状态", extra={
        "checkpoint_id": ckpt["checkpoint_id"], "restored": written,
        "total": len(payload), "overwrite": overwrite})
    return {"restored": written, "checkpoint_id": ckpt["checkpoint_id"],
            "n_keys": len(payload), "skipped": False}


def diff_checkpoint(store: StateStore, checkpoint_id: str) -> dict[str, Any]:
    """快照 vs 当前状态差异：added / changed / removed 三类 key。

    用于验证"恢复是否完整"与排查状态漂移。值用 `!=` 比较（dict 值可比较）。
    """
    with session_scope() as s:
        ckpt = repo.get_checkpoint(s, checkpoint_id)
    if ckpt is None:
        return {"found": False, "checkpoint_id": checkpoint_id}
    snap = ckpt.get("payload") or {}
    cur = store.snapshot(prefix=ckpt.get("prefix") or "")

    added = sorted(k for k in cur if k not in snap)
    removed = sorted(k for k in snap if k not in cur)
    changed = sorted(k for k in cur if k in snap and cur[k] != snap[k])
    return {"found": True, "checkpoint_id": checkpoint_id,
            "added": added, "changed": changed, "removed": removed,
            "n_added": len(added), "n_changed": len(changed),
            "n_removed": len(removed)}


class CheckpointLoop:
    """周期快照的后台任务：每 `interval_s` 导出一份，保留最近 `keep` 份。

    用 `threading.Event.wait(timeout)` 而非 `time.sleep`——停止信号能立刻唤醒
    （sleep 会拖满整个间隔才退出，拖慢关停）。
    """

    def __init__(self, store: StateStore, *, interval_s: float = 300.0,
                 keep: int = 5, prefix: str = "") -> None:
        self._store = store
        self._interval = max(1.0, float(interval_s))
        self._keep = max(1, int(keep))
        self._prefix = prefix
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def snapshot_once(self) -> dict[str, Any]:
        """导出一次并执行保留策略（保留最近 keep 份）。"""
        meta = write_checkpoint(self._store, prefix=self._prefix)
        with session_scope() as s:
            repo.prune_checkpoints(s, keep=self._keep)
        return meta

    def _run(self) -> None:
        # 先等一个间隔再首拍（启动瞬间状态尚空，拍了意义不大）
        while not self._stop.wait(self._interval):
            try:
                self.snapshot_once()
            except Exception as e:                       # noqa: BLE001
                # 快照失败不致命（下次再试）——但必须记 error，别静默
                _log.error("周期快照失败", extra={
                    "error": f"{type(e).__name__}: {e}"})

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="dd-checkpoint",
                                        daemon=True)
        self._thread.start()
        _log.info("状态快照周期任务已启动", extra={
            "interval_s": self._interval, "keep": self._keep})

    def stop(self) -> None:
        self._stop.set()


__all__ = [
    "CheckpointLoop",
    "diff_checkpoint",
    "make_checkpoint_id",
    "restore_latest",
    "write_checkpoint",
]
