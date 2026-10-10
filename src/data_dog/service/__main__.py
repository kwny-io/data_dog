"""`python -m data_dog.service` 启动器。

读 DD_SERVICE_HOST / DD_SERVICE_PORT / DD_SERVICE_NAME / DD_SERVICE_WORKERS，
交给 uvicorn 起服务。

多 worker 的**失败关闭**（为什么在启动器里拦）
--------------------------------------------
`DD_SERVICE_WORKERS > 1` 是真正的水平扩展（每进程一份解释器 → 绕开 GIL），也是本机
实测里唯一能立刻把中负载 P99 压进 SLO 的旋钮（见 `docs/write_path_batching.md` §6）。
但它有一个硬前提：**有状态的东西不能留在进程内**。限流桶与候选分组状态若落在
`memory` 后端，多 worker 就会各算各的——限流各挡各的（总放行量变成 N 倍）、候选
分组各归各的（同一段事件被切成 N 段候选）。

这类错误**不会报错**，只会静默给出错误的语义，所以这里**拒绝启动**而不是打个警告：
与全链其它"拿不准就失败关闭"的口径一致（见 `docs/reliability.md`）。
"""
from __future__ import annotations

import sys

from .. import settings


def _reject_per_process_state(workers: int) -> None:
    """多 worker + 进程内状态后端 → 失败关闭（附可直接照做的修法）。"""
    from ..state.factory import effective_rate_backend, effective_state_backend

    per_process = sorted({b for b in (effective_state_backend(),
                                      effective_rate_backend()) if b == "memory"})
    if not per_process:
        return
    raise SystemExit(
        f"拒绝启动：DD_SERVICE_WORKERS={workers} 需要**共享**状态后端，"
        f"但当前生效的是 {per_process}（进程内，多 worker 下不共享）。\n"
        "  → 限流会各挡各的（总放行量翻 N 倍）、候选分组会各归各的"
        "（同一段事件被切成 N 段候选），且**不会报错**。\n"
        "  修法任选其一：\n"
        "    · 设 DD_STATE_BACKEND=postgres（配 PostgreSQL 的 DD_DATABASE_URL），或\n"
        "    · 设 DD_STATE_BACKEND=redis（配 DD_REDIS_URL），或\n"
        "    · 把 DD_SERVICE_WORKERS 设回 1。")


def main() -> int:
    import uvicorn

    workers = settings.service_workers()
    if workers > 1:
        _reject_per_process_state(workers)

    uvicorn.run("data_dog.service.app:app", host=settings.service_host(),
                port=settings.service_port(),
                workers=workers,
                log_level=settings.log_level().lower())
    return 0


if __name__ == "__main__":
    sys.exit(main())
