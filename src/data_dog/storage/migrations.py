"""数据库迁移的程序化入口（alembic 封装）。

为什么需要它
------------
此前只有 `Base.metadata.create_all()`（`db.py:init_db`），它有两个致命问题：

1. **对已存在的表静默跳过**：加列、改类型、加索引在生产库上**完全不生效**，
   且不报错——典型的"看起来跑通了、其实字段没上"。
2. **无版本概念**：无法知道当前库是哪个版本的 schema，也无法回退。

本模块让服务能通过 alembic 走**版本化迁移**。

自动迁移的取舍
--------------
- `DD_AUTO_MIGRATE=1`（默认）：服务启动时自动 `upgrade head`，方便本地/单副本。
- `DD_AUTO_MIGRATE=0`：**生产多副本必须设这个**——多个实例同时启动会并发跑迁移，
  互相竞争。此时由部署流程显式执行 `alembic upgrade head` 或 `make migrate`。
"""
from __future__ import annotations

from pathlib import Path

import alembic.command as command
from alembic.config import Config


def project_root() -> Path:
    """项目根目录（含 alembic.ini / alembic/）。

    **必须**委托给 `paths.project_root()`——它是全项目统一的根解析器（环境变量
    `DD_PROJECT_ROOT` 优先，其次从本文件、再从 cwd 上溯找 marker）。这里不能再
    自己写一套"从本文件上溯找 alembic.ini"的逻辑：`pip install` 后本模块落入
    `site-packages/data_dog/storage/`，上溯路径里根本没有 alembic.ini，会一路
    退化成猜错目录 → 容器内迁移必失败（曾在 Docker 实跑暴露）。容器里由
    Dockerfile 的 `DD_PROJECT_ROOT=/app` 给出正确答案。
    """
    from ..paths import project_root as _root
    return _root()


def make_config(url: str) -> Config:
    """构造 alembic Config，把连接串显式注入（不依赖 alembic.ini 里的值）。

    注意：alembic 的 `set_main_option` 底层是 `configparser`，其中 `%` 是
    插值语法。若连接串含百分号编码（如 `?options=-csearch_path%3Dxxx`，
    或 URI 形式密码里的 `%40`），必须转义为 `%%`，否则 configparser 会抛
    `ValueError: invalid interpolation syntax`。转义只影响这里存的字符串，
    alembic 读回时会还原为单个 `%`。
    """
    root = project_root()
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def upgrade_head(url: str | None = None) -> None:
    """把数据库迁移到最新版本（**多入口互斥**：见下）。

    为什么先抢咨询锁：多个副本 / 多个部署任务同时 `alembic upgrade head` 会交错改版本表，
    落到难以恢复的中间态。锁是**连接级、不需要任何表**的（迁移的表可能还不存在），
    拿不到就阻塞等待——排队比并行乱跑好。非 PostgreSQL 上自动 no-op。
    """
    from .db import database_url
    from .pg_locks import migration_lock

    target = url or database_url()
    with migration_lock(target):
        command.upgrade(make_config(target), "head")


def current_revision(url: str | None = None) -> str:
    """返回当前库的迁移版本号（未迁移过返回空串）。"""
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine

    from .db import database_url
    eng = create_engine(url or database_url())
    try:
        with eng.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision() or ""
    finally:
        eng.dispose()


def auto_migrate_enabled() -> bool:
    """是否允许服务启动时自动迁移（默认允许；生产多副本应设 DD_AUTO_MIGRATE=0）。"""
    from .. import settings
    return settings.auto_migrate()


# ---------------------------------------------------------------- 命令行入口

def _main(argv: list[str] | None = None) -> int:
    """`python -m data_dog.storage.migrations <upgrade|current>`。

    供 K8s 迁移 Job 直接调用——**不依赖仓库内 alembic.ini 的相对路径**，
    连接串走 `DD_DATABASE_URL`（与运行时同一读取点）。

    为什么要有它：Helm 的迁移 Job 需要一个可执行的**迁移命令**；服务启动时的
    自动迁移（`DD_AUTO_MIGRATE=1`）在多副本下会并发竞争，生产必须关掉、改用
    一次性 Job 显式跑（见 docs/p4/04_k8s_helm.md §3.4）。没有这个入口，Job 就
    只能 `alembic` 裸调，而裸调依赖工作目录里恰有 alembic.ini——容器里不保证。
    """
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "upgrade"
    if cmd == "upgrade":
        upgrade_head()
        rev = current_revision()
        print(f"[migrate] upgrade head 完成，当前 revision={rev or '(空)'}")
        return 0
    if cmd == "current":
        print(current_revision() or "")
        return 0
    print(f"[migrate] 未知子命令 {cmd!r}（支持 upgrade / current）", file=sys.stderr)
    return 2


if __name__ == "__main__":
    import sys
    sys.exit(_main())
