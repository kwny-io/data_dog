# -*- coding: utf-8 -*-
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

import os
from pathlib import Path
from typing import Optional

from alembic import command
from alembic.config import Config


def project_root() -> Path:
    """仓库根目录（含 alembic.ini）。从本文件向上找到含 alembic.ini 的那层。"""
    for parent in Path(__file__).resolve().parents:
        if (parent / "alembic.ini").is_file():
            return parent
    return Path(__file__).resolve().parents[3]


def make_config(url: str) -> Config:
    """构造 alembic Config，把连接串显式注入（不依赖 alembic.ini 里的值）。"""
    root = project_root()
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def upgrade_head(url: Optional[str] = None) -> None:
    """把数据库迁移到最新版本。"""
    from .db import database_url
    command.upgrade(make_config(url or database_url()), "head")


def current_revision(url: Optional[str] = None) -> str:
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
    return os.environ.get("DD_AUTO_MIGRATE", "1").strip() != "0"
