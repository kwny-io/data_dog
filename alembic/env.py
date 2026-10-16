"""Alembic 环境配置。

关键点
------
1. **连接串从运行时同一来源取**（`data_dog.storage.db.database_url()`，即
   `DD_DATABASE_URL`）——避免"迁移连 A 库、应用连 B 库"的错配。
2. **target_metadata 指向真实 Base.metadata**——`--autogenerate` 才能正确
   比对模型与库的差异。
3. 支持离线（`--sql`，只生成 SQL 不连库）与在线两种模式。
"""
from __future__ import annotations

# 让 alembic 能看到 src/ 布局下的包
import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from data_dog.storage import models  # noqa: F401,E402  确保模型注册到 Base.metadata
from data_dog.storage.db import Base, database_url  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# autogenerate 的比对基准
target_metadata = Base.metadata


def _url() -> str:
    """连接串优先级：`-x db_url=...` > 显式注入的 `sqlalchemy.url` > 应用同源配置。

    CLI 直接跑 alembic 时，ini 里没有 `sqlalchemy.url`（刻意留空），于是回落到
    `DD_DATABASE_URL`——保证迁移与应用连同一个库。程序化调用
    （`storage.migrations.make_config(url)`）会把连接串写进 `sqlalchemy.url`，
    这里必须认它，否则那些显式传入的 URL（如测试用的独立 schema）会被忽略、
    静默迁移到 `DD_DATABASE_URL` 指向的库上。
    """
    override = context.get_x_argument(as_dictionary=True).get("db_url")
    if override:
        return override
    cfg_url = config.get_main_option("sqlalchemy.url")
    return cfg_url or database_url()


def run_migrations_offline() -> None:
    """离线模式：只把 SQL 打到 stdout，不连库（供人工审阅 / DBA 执行）。"""
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,            # 类型变化也纳入 autogenerate
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连库执行迁移。"""
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _url()
    connectable = engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # SQLite 的 ALTER 能力有限，用 batch 模式改写（建新表→拷数据→换名）
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
