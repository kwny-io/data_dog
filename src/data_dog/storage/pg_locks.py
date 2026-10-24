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

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, text

_log = logging.getLogger(__name__)


class MigrationLockTimeout(RuntimeError):
    """等迁移咨询锁超时（超时而不是无限阻塞，见 `migration_lock` 的说明）。"""


def _migration_lock_timeout_s() -> float:
    """从 settings 读超时；读不到也不让锁逻辑本身炸。"""
    try:
        from .. import settings
        return float(settings.migration_lock_timeout_s())
    except Exception:                                  # pragma: no cover - 防御性
        return 60.0

# 固定常量：出现在 `pg_locks` / 日志里要能一眼认出"是谁在动手"。
MIGRATION_LOCK_KEY = 0x64646D67           # "ddmg"：alembic upgrade head
PARTITION_CONVERT_LOCK_KEY = 0x64647063   # "ddpc"：分区就地转换
PARTITION_BACKFILL_LOCK_KEY = 0x64647062  # "ddpb"：created_day 分批回填
PARTITION_ENSURE_LOCK_KEY = 0x64647065    # "ddpe"：ensure/drop 的库层兜底（租约关掉时）
PARTITION_UNPARTITION_LOCK_KEY = 0x64647570  # "ddup"：分区表 → 普通表（转换的回退）


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


def try_xact_lock(session: Any, key: int) -> bool:
    """**不阻塞**地试抢事务级咨询锁；拿到返回 True，别人正持有返回 False。

    只读探测用（例如"报告转换为什么被拒"）：在 PG 里**同一事务**已经持有该锁时再调
    `pg_try_advisory_xact_lock` 仍会返回 False（锁按"持有个数"计），所以它回答的是
    "此刻还有没有别人拿着"，不是"我能不能重入"。非 PG：no-op（返回 True）。
    """
    if not is_postgres(session):
        return True
    got = session.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": key}).scalar()
    return bool(got)


@contextmanager
def advisory_xact_lock(session: Any, key: int, *, wait: bool = True) -> Iterator[bool]:
    """事务级咨询锁的上下文形式：`with` ... as got`，`got=False` 表示"别人正持有"。

    `wait=False` 时**不阻塞**、把决定权交给调用方——这是"失败关闭但不排队"的用法
    （例如宁可直接拒绝，也不要挂住一次 API 请求）。
    """
    yield acquire_xact_lock(session, key, wait=wait)


@contextmanager
def advisory_session_lock(session: Any, key: int, *, wait: bool = True) -> Iterator[bool]:
    """**会话级**咨询锁（PG）：跨多个事务持有，直到显式解锁或连接关闭。

    用于"自己会分批提交的长任务"（例如大表 `created_day` 分批回填）——事务级锁在第一批
    提交时就释放了，等于没有。锁挂在 `session` 当前那条连接上，所以**必须**在 finally 里解锁：
    否则连接回池后锁还在，后续任务会莫名其妙抢不到。

    非 PG：no-op（`yield True`）。`wait=False` 时不阻塞，把"别人正在跑"交给调用方处置。
    """
    if not is_postgres(session):
        yield True
        return
    sql = "SELECT pg_advisory_lock(:k)" if wait else "SELECT pg_try_advisory_lock(:k)"
    got = session.execute(text(sql), {"k": key}).scalar()
    if not wait and not bool(got):
        yield False
        return
    session.commit()                       # 让"取锁"这一步不挂在悬空事务里
    try:
        yield True
    finally:
        try:
            session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
            session.commit()
        except Exception:                  # pragma: no cover - 解锁失败不掩盖主流程异常
            session.rollback()


@contextmanager
def migration_lock(url: str) -> Iterator[bool]:
    """迁移期间的**会话级**咨询锁（独立连接持锁，yield 是否真的持有了锁）。

    为什么是会话级：alembic 的 `upgrade` 内部有自己的事务边界，事务级锁活不过第一个
    `COMMIT`。取锁后先 `commit()`，让持有的锁不挂在一个悬空事务里。

    **等待有上界**（2026-10-25 实测后改）：原来用阻塞式 `pg_advisory_lock`，一旦有别的
    连接持有该锁就一直等。实测场景：compose 里起服务，库是旧卷、另一进程/上次残留
    会话仍持锁 → `alembic` 打印完 `Running upgrade -> …` 就再无下文，**无 ERROR、
    无超时、无退出**；同时 `/healthz` 照旧 200，于是服务“起来了但永远不 ready”，
    现场只能看到别的告警刷屏，根本看不出卡在迁移锁上。
    现在改成 `pg_try_advisory_lock` + 有限重试，超时**抛 `MigrationLockTimeout`**，
    由启动路径记成“迁移失败”，错误信息直接说清是谁可能持锁、怎么查。

    超时默认 60s（`DD_MIGRATION_LOCK_TIMEOUT`）；正常迁移是秒级到几十秒级，
    60s 足够区分“正在跑”与“卡住了”。

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
        timeout_s = _migration_lock_timeout_s()
        deadline = time.monotonic() + timeout_s
        waited = False
        while True:
            got = bool(
                conn.execute(
                    text("SELECT pg_try_advisory_lock(:k)"), {"k": MIGRATION_LOCK_KEY}
                ).scalar()
            )
            if got:
                break
            left = deadline - time.monotonic()
            if left <= 0:
                raise MigrationLockTimeout(
                    f"等待迁移咨询锁超时（{timeout_s:.0f}s，key={MIGRATION_LOCK_KEY}）："
                    "另一个副本/部署任务可能正在迁移，或某个会话异常残留了这把锁。"
                    "排查：SELECT pid, state, query FROM pg_locks l JOIN pg_stat_activity a "
                    "ON a.pid = l.pid WHERE l.locktype = 'advisory'；"
                    "确认持锁进程已无用后可 SELECT pg_terminate_backend(<pid>)。"
                    "（这把锁是会话级的：持锁连接断开即自动释放，所以要查会话数量而不是等）"
                )
            if not waited:
                waited = True
                _log.warning(
                    "迁移锁已被占用，等待中（最多 %.0fs）", timeout_s,
                    extra={"lock_key": MIGRATION_LOCK_KEY, "timeout_s": timeout_s},
                )
            time.sleep(min(0.5, max(0.0, left)))
        conn.commit()
        held = True
        if waited:
            _log.warning(
                "已取得迁移锁（曾等待）", extra={"lock_key": MIGRATION_LOCK_KEY},
            )
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
