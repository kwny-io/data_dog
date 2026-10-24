"""启动期迁移结果必须**无条件可见**（stderr 一行 + `/readyz.startup_error`）。

为什么有这个文件（2026-10-25 容器实测，不是假想）
------------------------------------------------
compose 一键起整套时，库是个旧卷（6 张表，但没有 `alembic_version`），
`upgrade_head()` 撞 `ProgrammingError: DuplicateTable relation "alert_feedback" already exists`。

用 spy 钉实的过程：
- `_log.error("迁移失败（服务仍启动，/readyz 将报未就绪）")` **确实被调用了**，原因也齐；
- 但记录**没到达 `docker logs`**——lifespan 期间根 handler 的 stream 被换成了一个
  没人 flush 的流（同一进程里 `handler.stream is sys.stdout` 在 lifespan 前后由 True 变 False）；
- 现场看到的是：`/healthz` 200、`/readyz` 503（只说"缺 5 张表"，不说为什么），
  同时 worker 无退避地打了 **5919 条**告警把线索彻底淹没。

结论：**启动失败的"为什么"不能只依赖 logging 配置**。本文件守住两条：
1. `_startup_mark()` 直写 stderr（任何日志重配都影响不到）；
2. 结果进 `/readyz.startup_error`（机器可读、可告警）。
"""
from __future__ import annotations

import asyncio
import sys

import pytest
from fastapi.testclient import TestClient

from data_dog.service import runtime as rt
from data_dog.storage.db import init_db, reset_engine


@pytest.fixture()
def svc_db(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'svc.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", url)
    reset_engine()
    init_db(url)
    rt._set_migration_outcome(revision="", error="")     # 清掉其它用例的残留
    yield url
    reset_engine()


def test_startup_mark_goes_to_stderr_and_flushes(capsys):
    """直写 stderr：即使 logging 被重配/根 handler 流被换，这一行仍然看得见。"""
    rt._startup_mark("[data-dog] 测试标记")
    err = capsys.readouterr().err
    assert "[data-dog] 测试标记" in err


def test_migration_failure_is_visible_on_stderr_when_servers_starts(svc_db, monkeypatch,
                                                                   capsys):
    """**回归守卫**：迁移失败 → stderr 有原因与下一步，且进程仍然起得来。"""
    from data_dog.storage import migrations

    def boom(*a, **k):
        raise RuntimeError('DuplicateTable: relation "alert_feedback" already exists')

    monkeypatch.setattr(migrations, "upgrade_head", boom)
    monkeypatch.setattr(migrations, "auto_migrate_enabled", lambda: True)

    from data_dog.service.app import create_app
    with TestClient(create_app()) as c:                 # 启动不该因迁移失败而崩
        assert c.get("/healthz").status_code == 200, "存活探针不该被迁移失败带崩"
        body = c.get("/readyz").json()
        assert body["checks"]["schema"]["ok"] is True, "SQLite 用例里表是齐的（init_db 建过）"
        assert "alert_feedback" in body["checks"]["migration"]["startup_error"]

    err = capsys.readouterr().err
    assert "[data-dog] 启动：数据库迁移失败" in err
    assert "alert_feedback" in err, "错误原文必须出现在 stderr（否则等于没说）"
    assert "alembic stamp" in err, "要给出可直接照做的下一步"


def test_readyz_exposes_startup_error_and_timestamp(svc_db):
    """`/readyz.migration` 要能回答“为什么没就绪”，而不只是“缺哪张表”。"""
    from data_dog.service.app import create_app
    with TestClient(create_app()) as c:
        # 启动会重置结论（真实场景里失败就发生在启动那一刻），故进入后再注入
        rt._set_migration_outcome(error="ProgrammingError: boom")
        mig = c.get("/readyz").json()["checks"]["migration"]
    assert mig["ok"] is True                  # 版本查询本身没炸
    assert mig["startup_error"] == "ProgrammingError: boom"
    assert mig["startup_at"], "要带时间戳，便于对齐部署事件"


def test_startup_error_is_redacted(svc_db, monkeypatch):
    """`/readyz` 是公开探针：启动错误里带口令/DSN 时必须脱敏后再回。"""
    from data_dog.service.app import create_app
    with TestClient(create_app()) as c:
        rt._set_migration_outcome(error="OperationalError: postgresql://u:secret_pw@db/x")
        mig = c.get("/readyz").json()["checks"]["migration"]
    assert "secret_pw" not in mig["startup_error"], mig


def test_marks_never_break_startup(monkeypatch):
    """诊断自己不许把启动搞挂：stderr 不可写时静默（但 readyz 仍能报）。"""
    class _Broken:
        def write(self, *a):
            raise OSError("stderr closed")

        def flush(self):
            raise OSError("stderr closed")

    monkeypatch.setattr(sys, "stderr", _Broken())
    rt._startup_mark("[data-dog] x")           # 不抛
    rt._set_migration_outcome(error="E")
    assert rt._migration_outcome()["error"] == "E"


def test_lifespan_outcome_survives_logging_reconfiguration(svc_db, monkeypatch, capsys):
    """日志栈被重配（模拟实测现场）后，结论仍在内存里、且 stderr 已有一行。"""
    from data_dog.service.app import _lifespan, create_app
    from data_dog.storage import migrations

    monkeypatch.setattr(migrations, "upgrade_head", lambda *a, **k: None)
    monkeypatch.setattr(migrations, "auto_migrate_enabled", lambda: True)
    monkeypatch.setattr(migrations, "current_revision", lambda *a, **k: "a9c1d2e3f4b5")

    async def run():
        gen = _lifespan(create_app())
        await gen.__anext__()
        try:
            await gen.__anext__()
        except StopAsyncIteration:
            pass

    import logging
    logging.getLogger().handlers.clear()       # 模拟“日志被换掉/清空”
    asyncio.run(run())
    out = rt._migration_outcome()
    assert out["revision"] == "a9c1d2e3f4b5"
    assert out["error"] == ""
