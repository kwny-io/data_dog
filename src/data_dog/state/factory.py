"""共享状态后端选择（调用点唯一入口）。

两个独立的后端槽位
------------------
- **状态后端**（`DD_STATE_BACKEND`）：候选分组等业务状态。默认 memory；配了 PG
  则自动 postgres（多副本安全默认）。
- **限流后端**（`DD_RATE_BACKEND`）：令牌桶。空 = 跟随状态后端（默认不拆）。

为什么允许拆：限流每条遥测一次 check-and-decrement，是热路径；当它成为瓶颈时，
`DD_RATE_BACKEND=redis` 把限流切到 Redis（sub-ms + 原生 TTL），业务状态仍走 PG
（耐久 + 可查询）。默认同源，少运维一个组件；要提速只改环境变量，调用点不动。

诚实边界：`memory` 后端在多副本下**不共享**——单副本/测试用；多副本必须让两个
槽位都落在 postgres/redis，否则限流与候选合并会各算各的（有启动期告警）。
"""
from __future__ import annotations

from .base import StateStore
from .memory import InMemoryStateStore

_state: StateStore | None = None
_rate: StateStore | None = None


def _auto_backend() -> str:
    """未显式设置时的默认：PG 连接串 → postgres；否则 memory。"""
    from .. import settings
    url = settings.database_url()
    if url.startswith(("postgresql", "postgres")):
        return "postgres"
    return "memory"


def _build(name: str) -> StateStore:
    from .. import settings
    if name == "memory":
        return InMemoryStateStore()
    if name == "postgres":
        from .pg import PostgresStateStore
        url = settings.database_url()
        if not url.startswith(("postgresql", "postgres")):
            raise RuntimeError(
                "postgres 状态后端需要 PostgreSQL 的 DD_DATABASE_URL"
                "（SQLite 不支持跨副本共享状态）")
        return PostgresStateStore(url)
    if name == "redis":
        from .redis import RedisStateStore
        return RedisStateStore(settings.redis_url())
    raise RuntimeError(f"未知的状态后端: {name!r}（可用 memory / postgres / redis）")


def get_state_store() -> StateStore:
    """业务状态后端（候选分组等）。"""
    global _state
    if _state is None:
        from .. import settings
        _state = _build(settings.state_backend() or _auto_backend())
    return _state


def get_rate_store() -> StateStore:
    """限流后端。未显式配置时与业务状态后端**同源**（默认不拆）。"""
    global _rate
    if _rate is not None:
        return _rate
    from .. import settings
    name = settings.rate_backend()
    if not name:
        return get_state_store()          # 跟随状态后端（同一实例）
    _rate = _build(name)
    return _rate


def reset_state_store() -> None:
    """清空缓存（测试切换后端用）。"""
    global _state, _rate
    if _state is not None:
        _state.close()
    if _rate is not None and _rate is not _state:
        _rate.close()
    _state = None
    _rate = None
