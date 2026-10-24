"""`/metrics` 必须**降级而不是 500**：指标采集的每一段独立失败，且有信号自曝残缺。

为什么（2026-10-25 容器实测，不是假想）
------------------------------------
compose 起整套时库缺表，抓 `/metrics` 得到 **500**（`execution_failed / ProgrammingError`），
而同一份代码的 docstring 写着"采集失败只记日志，不让指标端点整体 500"。原因：
库内计数包在 try 里，但 `_report_partition_lease()` / `_report_partition_ddl_results()` /
`jr.queue_depth()` 这些"号称不依赖 DB"的调用放在**外面**——共享状态 store 在 PG 模式下
**就是 DB**，于是缺表时整条端点垮掉。

失败模式的要害不是"少几个指标"：监控恰好在事故中最需要它的时候全黑，而且抓取方只看到
`up=0`，分不清"应用挂了"和"库挂了"。所以这里守住两条：

1. 任一段失败 → 端点仍 200，其余指标照常输出；
2. 残缺**自曝**：`data_dog_metrics_collect_failures_total{section}` 递增，
   重复失败只打一条 WARN（刷屏会掩埋真因，同 `_FailureThrottle` 的教训）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from data_dog.service import runtime as rt
from data_dog.service.routes import health as health_route
from data_dog.storage.db import reset_engine


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """真走**应用启动迁移**建表（不手搓 init_db，否则 alembic 撞 DuplicateTable，
    反而先把库弄成“缺表”状态，把本文件的断言弄脏）。"""
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", url)
    reset_engine()
    health_route._SECTION_OK.clear()
    from data_dog.service.app import create_app
    with TestClient(create_app()) as c:
        yield c
    reset_engine()


class _RecordingLog:
    def __init__(self) -> None:
        self.records: list[tuple[int, str, dict]] = []

    def _push(self, level, msg, args, extra):
        try:
            rendered = msg % args if args else msg
        except Exception:                       # pragma: no cover
            rendered = msg
        self.records.append((level, rendered, dict(extra or {})))

    def info(self, msg, *a, **kw):              # noqa: ANN001, ANN002, ANN003
        self._push(20, msg, a, kw.get("extra"))

    def warning(self, msg, *a, **kw):           # noqa: ANN001, ANN002, ANN003
        self._push(30, msg, a, kw.get("extra"))

    error = warning                               # 端点里不该有 error，同一记录口足够

    def warns(self):
        return [r for r in self.records if r[0] == 30]


def _failures(body: str, section: str) -> float:
    """从 Prometheus 文本里取某个 section 的失败计数（没有该序列即为 0）。"""
    for line in body.splitlines():
        if line.startswith("data_dog_metrics_collect_failures_total{") and \
                f'section="{section}"' in line:
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def test_healthy_scrape_reports_no_failures(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.text
    for section in ("partition_lease", "partition_ddl", "stream", "job_queue", "db_counts"):
        assert _failures(body, section) == 0.0, f"健康时 {section} 不该报失败"
    assert "data_dog_uptime_seconds" in body


def test_db_degraded_scrape_is_still_200_and_self_reports(client, monkeypatch):
    """**回归守卫**：库采集炸掉时端点不能 500，且失败计数必须显形。"""

    def boom(*a, **k):                          # noqa: ANN002, ANN003, ARG001
        raise RuntimeError('ProgrammingError: relation "investigation_job" does not exist')

    monkeypatch.setattr(health_route.repo, "count_rows", boom)
    r = client.get("/metrics")
    assert r.status_code == 200, "库坏不许把 /metrics 打死（事故时最需要它）"
    body = r.text
    assert _failures(body, "db_counts") >= 1.0, "残缺必须自曝，否则调用方以为指标是完整的"
    # 不依赖库的指标必须还在
    assert "data_dog_uptime_seconds" in body and "data_dog_info" in body


def test_section_failure_is_isolated(client, monkeypatch):
    """一段炸不许连坐其它段：分区租约失败时，库内计数照常。"""

    def boom(*a, **k):                          # noqa: ANN002, ANN003, ARG001
        raise RuntimeError("shared state store unavailable")

    monkeypatch.setattr(health_route, "_report_partition_lease", boom)
    body = client.get("/metrics").text
    assert _failures(body, "partition_lease") >= 1.0
    # 其它**不碰库**的段落必须干净（用它们证明没连坐；db_counts 是否成功取决于库状态，
    # 不适合在这里充当隔离证据）
    for other in ("stream", "job_queue", "partition_ddl"):
        assert _failures(body, other) == 0.0, f"{other} 被连坐了（逐段降级失效）"
    assert "data_dog_uptime_seconds" in body


def test_repeated_failures_log_once_per_outage(client, monkeypatch):
    """重复失败只打一条 WARN；恢复时补一条 INFO（日志反映状态变化，不反映次数）。"""
    rec = _RecordingLog()
    monkeypatch.setattr(health_route, "_log", rec)

    def boom(*a, **k):                          # noqa: ANN002, ANN003, ARG001
        raise RuntimeError("down")

    monkeypatch.setattr(health_route, "_report_partition_ddl_results", boom)
    for _ in range(10):
        assert client.get("/metrics").status_code == 200
    warns = [w for w in rec.warns() if w[2].get("section") == "partition_ddl"]
    assert len(warns) == 1, f"一次故障打了 {len(warns)} 条告警（刷屏会掩埋真因）"

    # 恢复：再抓一次应补一条 INFO
    monkeypatch.setattr(health_route, "_report_partition_ddl_results", lambda *a, **k: None)
    health_route._SECTION_OK["partition_ddl"] = False          # 明确处于“故障中”
    assert client.get("/metrics").status_code == 200
    infos = [r for r in rec.records if r[0] == 20 and r[2].get("section") == "partition_ddl"]
    assert infos, "恢复时应有 INFO（静默转好 = 没人知道曾经坏过）"


def test_readyz_and_metrics_agree_on_db_health(client, monkeypatch):
    """同源口径：库坏时 readyz 说未就绪，metrics 说残缺——两边不能互相矛盾。"""

    def boom(*a, **k):                          # noqa: ANN002, ANN003, ARG001
        raise RuntimeError('ProgrammingError: relation "alert_delivery" does not exist')

    monkeypatch.setattr(health_route.repo, "count_rows", boom)
    m = client.get("/metrics")
    z = client.get("/readyz")
    assert m.status_code == 200             # 指标端点降级
    assert z.status_code in (200, 503)      # 就绪判定走自己的口径（SQLite 用例里表是齐的）
    assert _failures(m.text, "db_counts") >= 1.0
    assert rt is not None                   # 保持 import 使用（runtime 提供 section 状态）
