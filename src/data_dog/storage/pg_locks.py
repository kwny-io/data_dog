"""PostgreSQL **咨询锁**（advisory lock）：不依赖任何表的**库层硬互斥**。

为什么还需要它（与 `state/lease.py` 的分工，别重复造机制）
-------------------------------------------------------------
仓里已有租约原语 `state/lease.py`（A6/A9 用它做协作式互斥：抢不到就跳过、谁在动手可查）。
但租约**依赖一张状态表**，因此有两处天然够不着：

1. **迁移**（`alembic upgrade head`）：表可能还没建出来 / 版本不对——"用被迁移的东西保护
   迁移"是鸡生蛋。多个副本、多个部署任务同时 upgrade 是经典事故（版本表交错，落到难以恢复的
   中间态）。
2. **租约 TTL 过期而 DDL 还在跑**：整表拷贝这类长 DDL 超过 TTL 时，第二个副本会抢到租约，
   互斥就失效——A9 的实现里如实写了这条边界。

咨询锁两者都能覆盖：它是**连接级**的、生命周期由 PG 自己管，不需要任何表。

- 事务级 `pg_advisory_xact_lock(k)`：随事务提交/回滚**自动释放**，不可能泄漏；
- 会话级 `pg_advisory_lock(k)`：必须显式解锁——迁移要跨 alembic 自己的多个事务，只能用这个。

分工写清楚（刻意不合并成一个机制）：
- **跨入口的协作式互斥** → `state/lease.py`（`partition:ddl` 租约：抢不到就跳过、谁在动手可查）；
- **库层硬互斥、且要在"没有表"时也能用** → 本模块：迁移入口 + 分区就地转换事务。

边界（如实写）
--------------
- **非 PostgreSQL（SQLite/memory）是 no-op**：加锁不能把单机部署变成"必须 PG"。这时互斥退回到
  "单进程假设 + 部署形态"这一层——多副本请用 PG/Redis。
- 咨询锁是**协作式**的：绕过本模块直接跑 DDL 不会受阻（该层由 PG 自己的表锁兜底：不会坏数据，
  但会撞锁、报难读的错）。
- `migration_lock` 用**独立连接**持锁：如果调用方把它塞进一个已开的长事务里，锁的生命周期会
  跟那个事务纠缠——所以它自己建引擎、自己提交"取锁"这一步。
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, text

# 固定常量：出现在 `pg_locks` / 日志里要能一眼认出"是谁在动手"。
MIGRATION_LOCK_KEY = 0x64646D67           # "ddmg"：alembic upgrade head
PARTITION_CONVERT_LOCK_KEY = 0x64647063   # "ddpc"：分区就地转换


def is_postgres(session_or_bind: Any) -> bool:
    """这个会话/引擎是不是 PostgreSQL（决定锁是真做还是 no-op）。"""
    bind = getattr(session_or_bind, "bind", session_or_bind)
    return getattr(getattr(bind, "dialect", None), "name", "") == "postgresql"


def acquire_xact_lock(session: Any, key: int, *, wait: bool = True) -> bool:
    """立即抢**事务级**咨询锁，返回是否拿到；随本事务结束自动释放。

    不需要 `with` 包住整段代码、也不改变调用方的事务边界时用它（例如"整个转换函数本就在
    一个事务里"）。非 PG：直接返回 `True`（no-op）。
    """
    if not is_postgres(session):
        return True
    sql = "SELECT pg_advisory_xact_lock(:k)" if wait else "SELECT pg_try_advisory_xact_lock(:k)"
    got = session.execute(text(sql), {"k": key}).scalar()
    return True if wait else bool(got)


@contextmanager
def advisory_xact_lock(session: Any, key: int, *, wait: bool = True) -> Iterator[bool]:
    """事务级咨询锁的上下文形式：`with` ... as got`，`got=False` 表示"别人正持有"。

    `wait=False` 时**不阻塞**、把决定权交给调用方——这是"失败关闭但不排队"的用法
    （例如宁可直接拒绝，也不要挂住一次 API 请求）。
    """
    yield acquire_xact_lock(session, key, wait=wait)


@contextmanager
def migration_lock(url: str) -> Iterator[bool]:
    """迁移期间的**会话级**咨询锁（独立连接持锁，yield 是否真的持有了锁）。

    为什么是会话级：alembic 的 `upgrade` 内部有自己的事务边界，事务级锁活不过第一个
    `COMMIT`。取锁后先 `commit()`，让持有的锁不挂在一个悬空事务里。

    非 PG：no-op（`yield False`，调用方据此知道"没有互斥保护"）。
    """
    engine = create_engine(url)
    if engine.dialect.name != "postgresql":
        engine.dispose()
        yield False
        return
    conn = engine.connect()
    held = False
    try:
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": MIGRATION_LOCK_KEY})
        conn.commit()
        held = True
        yield True
    finally:
        if held:
            try:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": MIGRATION_LOCK_KEY})
                conn.commit()
            except Exception:      # pragma: no cover - 解锁失败不该掩盖迁移本身的异常
                pass
        conn.close()
        engine.dispose()
