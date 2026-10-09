"""可靠性加固的回归测试：超时/重试预算 + 失败关闭。

覆盖 `docs/reliability.md` 审计清单里已处理的条目（A / B / C / D / E / F），改动都
落在**失败语义**上：等不到时必须快速报错、重试必须有上限且可观测、依赖不可用时必须
让 Agent 拿不到"看似权威"的数据。失败语义最怕"改回去也没人发现"，故每条都用
**可断言的行为**钉住（预算耗尽 → 显式失败/可观测；正常路径 → 不变）：

- A **PG 三段超时**：断言 URL 方言 → `create_engine` kwargs 与运行时引擎都带上了
  `connect_timeout` / `pool_timeout` / `statement_timeout`，且 **SQLite 的 kwargs
  逐字节不变**。不需要真 PG：`create_engine` 惰性建连，只检查配置面。
- B **流式源轮询**：次数与墙钟双上限（先到为准）+ 抖动退避；耗尽后**停下来并把原因
  写进 `stats`**（`stop_reason` / `poll_failures_exhausted`），不无限静默重试。
- C **Outbox 投递**：单次 `deliver()` 内按"次数 + 墙钟"重试、抖动退避；耗尽进死信并
  带 `dead_reason`；幂等键/revision 每次重试不变（至少一次 + 一次外部效果）。
- D **LLM 客户端**：显式 `max_retries=0`，把重试预算收回我们自己的 deadline。
- E **可信上下文 MCP**：看门狗由 `timeout_s` 派生（≥ 调用超时）、抖动退避、总预算。
- F **变更源失败关闭**：来源抛错 → `unavailable` 统一错误（无 `changes` 字段），
  经工具层透传后 Agent 仍拿不到变更列表；只有 `DD_CHANGE_FALLBACK_DEMO=1` 才回退样例表。

正常路径（来源可用、越权拒绝、MCP 正常取回）也在本文件里回归，防止"为了失败关闭把
成功路径也关掉了"。
"""
from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from data_dog.alerting.outbox import AlertOutbox
from data_dog.evidence.change_source import ChangeRecord, MockChangeSource
from data_dog.providers.client import LLMCallError, LLMChatMessage, Provider
from data_dog.service.evidence import RepoEvidenceBridge
from data_dog.storage import db
from data_dog.stream.base import StreamMessage, StreamSource
from data_dog.stream.runner import PollRetryPolicy, StreamRunner
from data_dog.tools.investigation import InvestigationTools

# 假 PG URL：`create_engine` 惰性建连，本文件**不会**真的连上去（无网络依赖）。
_PG_URL = "postgresql+psycopg://user:pw@db.example:5432/data_dog"
_COMPONENT = "joint_motor.front_left_hip"


class _BoomSource:
    """变更来源替身：任何查询都抛错（模拟 CMDB 挂掉 / 文件不可读 / 库连不上）。"""

    def query(self, **_kw: Any) -> list[Any]:
        raise ConnectionError("cmdb down")


class _AbortConnect(Exception):
    """在 `do_connect` 事件里抛出：截获 libpq 参数后**中止建连**（无网络依赖）。"""


def _bridge(source: Any) -> RepoEvidenceBridge:
    return RepoEvidenceBridge([], device_sn="dev-1", change_source=source)


# ================================================================ 1. PG 三段超时

def test_pg_engine_kwargs_carry_three_timeouts():
    """PG URL → connect/pool/statement 三段等待全部有上限（且非库默认的无限/30s）。"""
    kwargs = db.engine_kwargs(_PG_URL)
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["pool_timeout"] == 5.0, "拿连接必须有上限（库默认 30s）"
    assert kwargs["connect_args"]["connect_timeout"] == 5.0, "建连必须有限（libpq 默认无限）"
    assert "statement_timeout=15000" in kwargs["connect_args"]["options"]
    print("PASS test_pg_engine_kwargs_carry_three_timeouts")


def test_pg_engine_kwargs_follow_settings_overrides(monkeypatch: pytest.MonkeyPatch):
    """三个值都从 settings 读：运维可用 DD_DB_* 调整，不必改代码。"""
    monkeypatch.setenv("DD_DB_CONNECT_TIMEOUT_S", "1.5")
    monkeypatch.setenv("DD_DB_POOL_TIMEOUT_S", "2.5")
    monkeypatch.setenv("DD_DB_STATEMENT_TIMEOUT_MS", "250")
    kwargs = db.engine_kwargs(_PG_URL)
    assert kwargs["connect_args"]["connect_timeout"] == 1.5
    assert kwargs["pool_timeout"] == 2.5
    assert "statement_timeout=250" in kwargs["connect_args"]["options"]
    print("PASS test_pg_engine_kwargs_follow_settings_overrides")


def test_pg_engine_kwargs_omit_statement_timeout_when_zero(monkeypatch: pytest.MonkeyPatch):
    """`DD_DB_STATEMENT_TIMEOUT_MS=0` = 不限语句时长 → 不塞 options（其余两项仍在）。

    这条守的是"显式关闭"与"忘了配"的区别：0 是一个可审计的选择，不是静默缺省。
    """
    monkeypatch.setenv("DD_DB_STATEMENT_TIMEOUT_MS", "0")
    kwargs = db.engine_kwargs(_PG_URL)
    assert "options" not in kwargs["connect_args"]
    assert kwargs["connect_args"]["connect_timeout"] == 5.0
    assert kwargs["pool_timeout"] == 5.0
    print("PASS test_pg_engine_kwargs_omit_statement_timeout_when_zero")


def test_pg_engine_kwargs_merge_existing_url_options(monkeypatch: pytest.MonkeyPatch):
    """URL 里已有的 `?options=...` 必须**保留**并与 statement_timeout 合并（不静默覆盖）。

    `create_engine(connect_args=...)` 的优先级高于 URL 查询串：若这里直接写单值，
    运维在连接串里传的 `-csearch_path=...` 会被悄悄丢掉——正是本仓最忌讳的
    "没报错但行为变了"。
    """
    monkeypatch.delenv("DD_DB_STATEMENT_TIMEOUT_MS", raising=False)
    url = _PG_URL + "?options=-csearch_path%3Dapp"
    opts = db.engine_kwargs(url)["connect_args"]["options"]
    assert "-csearch_path=app" in opts
    assert "-c statement_timeout=15000" in opts
    print("PASS test_pg_engine_kwargs_merge_existing_url_options")


def test_sqlite_engine_kwargs_unchanged_by_pg_hardening():
    """SQLite 路径**不受影响**：kwargs 只有既有的 check_same_thread，无 PG 专有参数。"""
    kwargs = db.engine_kwargs("sqlite:///local.db")
    assert kwargs == {"future": True,
                      "connect_args": {"check_same_thread": False}}, kwargs
    print("PASS test_sqlite_engine_kwargs_unchanged_by_pg_hardening")


def test_sqlite_engine_keeps_default_pool_timeout(tmp_path: Path):
    """真建一个 SQLite 引擎：池超时仍是 SQLAlchemy 默认（未被 5s 波及）。"""
    eng = db.get_engine(f"sqlite:///{(tmp_path / 'x.db').as_posix()}")
    try:
        assert eng.url.get_backend_name() == "sqlite"
        assert eng.dialect.create_connect_args(eng.url)[1]["check_same_thread"] is False
        assert eng.pool._timeout == 30.0, "SQLite 不该套用 PG 的 5s 池超时"
    finally:
        eng.dispose()
    print("PASS test_sqlite_engine_keeps_default_pool_timeout")


def test_pg_engine_runtime_config_matches_kwargs():
    """真建一个 PG 引擎（**不连库**）：dialect / 池超时 / 交给 libpq 的参数都对得上。

    用 SQLAlchemy 的 `do_connect` 事件截获"最终交给 DBAPI 的参数"（建连前的最后一站），
    随即抛异常中止连接。断言的是"libpq 确实会收到 connect_timeout / statement_timeout"，
    而不只是"kwargs 字典里有"——两者之间正是容易漂移的地方。
    """
    pytest.importorskip("psycopg")               # libpq 驱动（pyproject 的 postgres extra）
    from sqlalchemy import event
    # 带 `?options=` 的 URL：同时验证"URL 的 options 不被吞掉"（见 kwargs 层那条用例）
    eng = db.get_engine(_PG_URL + "?options=-csearch_path%3Dapp")
    captured: dict[str, Any] = {}

    @event.listens_for(eng, "do_connect")
    def _capture(dialect: Any, conn_rec: Any, cargs: Any, cparams: Any) -> None:
        captured.update(cparams)
        raise _AbortConnect("拿到参数即中止，不真连")

    try:
        assert eng.dialect.name == "postgresql"
        assert eng.url.host == "db.example" and eng.url.port == 5432
        assert eng.pool._timeout == 5.0
        assert eng.pool._pre_ping is True
        with pytest.raises(_AbortConnect):
            eng.connect()
        assert captured["connect_timeout"] == 5.0
        assert captured["options"] == "-csearch_path=app -c statement_timeout=15000"
    finally:
        eng.dispose()
    print("PASS test_pg_engine_runtime_config_matches_kwargs")


def test_pg_state_store_engine_reuses_same_timeouts():
    """PG 状态后端与业务库**共用同一份引擎参数**（不许两处各写一套而漂移）。"""
    pytest.importorskip("psycopg")
    from data_dog.state.pg import PostgresStateStore
    store = PostgresStateStore(_PG_URL)
    try:
        assert store._engine.pool._timeout == 5.0
        assert store._engine.pool._pre_ping is True
        assert store._engine.dialect.name == "postgresql"
    finally:
        store.close()
    print("PASS test_pg_state_store_engine_reuses_same_timeouts")


# ================================================================ 2. 变更源失败关闭

def test_change_fallback_demo_switch_defaults_off(monkeypatch: pytest.MonkeyPatch):
    """样例回退开关默认**关**：默认口径是失败关闭，不是"给点假数据"。"""
    from data_dog import settings
    monkeypatch.delenv("DD_CHANGE_FALLBACK_DEMO", raising=False)
    assert settings.change_fallback_demo() is False
    print("PASS test_change_fallback_demo_switch_defaults_off")


def test_change_source_unavailable_is_fail_closed(monkeypatch: pytest.MonkeyPatch):
    """来源抛错 → 返回 unavailable 错误（不是空列表、不是样例数据）。

    "查不到"与"没有变更"对结论是反向的：前者只能得出"变更证据缺失"，后者才支持
    "变更无关"。失败关闭要求这里只能是前者。
    """
    monkeypatch.delenv("DD_CHANGE_FALLBACK_DEMO", raising=False)
    res = _bridge(_BoomSource()).query_recent_changes("dev-1", _COMPONENT, "2026-08-01")
    assert res["ok"] is False
    assert res["error"] == "unavailable", "依赖不可用必须报 unavailable"
    assert res["reason"] == "change_source_unavailable"
    assert "changes" not in res, "错误结果里绝不能带变更列表"
    assert _BoomSource.__name__ in res["detail"]
    print("PASS test_change_source_unavailable_is_fail_closed")


def test_change_unavailable_never_looks_like_no_changes(monkeypatch: pytest.MonkeyPatch):
    """错误形状**不可能**被读成"近期无变更"：无 changes 字段，且错误码不是 not_found。

    工具层对 `ok=False` 原样透传（不包 `changes`），故 Agent 看到的就是这个错误对象。
    """
    monkeypatch.delenv("DD_CHANGE_FALLBACK_DEMO", raising=False)
    bridge = _bridge(_BoomSource())
    raw = bridge.query_recent_changes("dev-1", _COMPONENT, "2026-08-01")
    assert raw != {"changes": [], "source": "builtin_fallback"}
    assert raw.get("changes") is None
    assert raw.get("error") != "not_found"
    # 经 Agent 的工具层：仍是同一个错误对象，没有被补上空 changes 列表
    out = InvestigationTools(bridge).query_recent_changes("dev-1", _COMPONENT, "2026-08-01")
    assert out.get("ok") is False and "changes" not in out
    assert out.get("error") == "unavailable"
    print("PASS test_change_unavailable_never_looks_like_no_changes")


def test_change_fallback_demo_switch_returns_marked_samples(monkeypatch: pytest.MonkeyPatch):
    """显式开 demo 开关时才回退样例表，且结果带 degraded 标注（可被上层识别）。"""
    monkeypatch.setenv("DD_CHANGE_FALLBACK_DEMO", "1")
    res = _bridge(_BoomSource()).query_recent_changes("dev-1", _COMPONENT, "2000-01-01")
    assert res["source"] == "builtin_fallback"
    assert res["degraded"] is True
    assert len(res["changes"]) >= 1
    print("PASS test_change_fallback_demo_switch_returns_marked_samples")


def test_change_source_available_path_label_unchanged(monkeypatch: pytest.MonkeyPatch):
    """正常路径不受影响：来源可用 → 真按 since 过滤 + 标注来源名，无 degraded 字段。"""
    monkeypatch.delenv("DD_CHANGE_FALLBACK_DEMO", raising=False)
    src = MockChangeSource([
        ChangeRecord(change_id="c1", kind="firmware", target=_COMPONENT,
                     applied_at="2026-07-01"),
        ChangeRecord(change_id="c2", kind="config", target=_COMPONENT,
                     applied_at="2026-08-20"),
    ])
    res = _bridge(src).query_recent_changes("dev-1", _COMPONENT, "2026-08-01")
    assert res["source"] == "MockChangeSource"
    assert [c["id"] for c in res["changes"]] == ["c2"]
    assert "degraded" not in res
    print("PASS test_change_source_available_path_label_unchanged")


def test_change_denied_path_is_still_denied():
    """越权仍走 denied（比"来源不可用"优先级更高）：加固没有改变权限语义。"""
    res = _bridge(MockChangeSource()).query_recent_changes("dev-1", "secret.other", "")
    assert res["ok"] is False and res["error"] == "denied"
    print("PASS test_change_denied_path_is_still_denied")


# ================================================================ 3. 流式源轮询预算（B）
# 口径：次数 + 墙钟双上限（先到为准）+ 抖动退避；耗尽 → 停下来并把原因写进 stats。

class _DeadSource(StreamSource):
    """永远抛错的源（模拟地址写错 / broker 长期不可用）。"""

    name = "dead"

    def __init__(self) -> None:
        self.polls = 0

    def poll(self, timeout: float) -> list[StreamMessage]:
        self.polls += 1
        raise ConnectionError("broker 不可达")


class _FlakyThenEmpty(StreamSource):
    """先失败一次，之后正常（模拟短暂掉线后自愈）。"""

    name = "flaky"

    def __init__(self) -> None:
        self.polls = 0

    def poll(self, timeout: float) -> list[StreamMessage]:
        self.polls += 1
        if self.polls == 1:
            raise ConnectionError("掉线")
        return []


_NO_SLEEP = PollRetryPolicy(max_consecutive=3, budget_s=999.0,
                            base_sleep_s=0.0, max_sleep_s=0.0)


def _runner(source: StreamSource, policy: PollRetryPolicy) -> StreamRunner:
    return StreamRunner(None, source, persist=lambda *a: None, poll_retry=policy)


def test_stream_poll_failures_exhaust_count_budget_and_expose_reason():
    """连续失败达次数上限 → **停止消费**并把原因写进 stats（不无限重试）。"""
    src = _DeadSource()
    runner = _runner(src, _NO_SLEEP)
    runner.run(stop=threading.Event(), poll_timeout=0.0, max_iters=50)
    assert src.polls == 3, "应在第 3 次失败后停止，而不是继续轮询"
    assert runner.stats["poll_errors"] == 3
    assert runner.stats["poll_failures_exhausted"] == 1
    assert runner.stats["stop_reason"] == "source_unavailable"   # /healthz 可见
    print("PASS test_stream_poll_failures_exhaust_count_budget_and_expose_reason")


def test_stream_poll_failures_exhaust_wall_clock_budget_first():
    """墙钟上限可以**先于**次数上限生效（同一 "先到为准" 口径）。"""
    src = _DeadSource()
    runner = _runner(src, PollRetryPolicy(max_consecutive=10_000, budget_s=0.0,
                                          base_sleep_s=0.0, max_sleep_s=0.0))
    runner.run(stop=threading.Event(), poll_timeout=0.0, max_iters=50)
    assert src.polls == 1, "墙钟预算为 0 → 第一次失败即耗尽，不应再试"
    assert runner.stats["poll_failures_exhausted"] == 1
    assert runner.stats["stop_reason"] == "source_unavailable"
    print("PASS test_stream_poll_failures_exhaust_wall_clock_budget_first")


def test_stream_poll_backoff_is_jittered_and_bounded():
    """退避 = 指数 + 单次上限 + `U(0.5,1.5)` 抖动（有界、非固定、会封顶）。"""
    policy = PollRetryPolicy(base_sleep_s=0.05, max_sleep_s=0.4)
    assert 0.025 <= policy.backoff_s(1) <= 0.075
    assert 0.1 <= policy.backoff_s(3) <= 0.3
    assert 0.2 <= policy.backoff_s(20) <= 0.6                  # 封顶在 0.4×1.5
    samples = {round(policy.backoff_s(3), 6) for _ in range(50)}
    assert len(samples) > 1, "没有抖动 → 多副本会同时重连（惊群）"
    print("PASS test_stream_poll_backoff_is_jittered_and_bounded")


def test_stream_runner_recovers_after_transient_poll_error():
    """正常路径不变：短暂掉线后自愈，预算清零、继续消费、不误判为"源不可用"。"""
    src = _FlakyThenEmpty()
    policy = PollRetryPolicy(max_consecutive=5, budget_s=999.0,
                             base_sleep_s=0.0, max_sleep_s=0.0)
    runner = _runner(src, policy)
    runner.run(stop=threading.Event(), poll_timeout=0.0, max_iters=3)
    assert runner.stats["poll_errors"] == 1
    assert runner.stats["poll_failures_exhausted"] == 0
    assert runner.stats["stop_reason"] == "max_iters"           # 正常跑完，未放弃
    assert src.polls == 3
    print("PASS test_stream_runner_recovers_after_transient_poll_error")


# ================================================================ 4. Outbox 投递预算（C）

class _AlwaysFailAdapter:
    """永远投递失败，并记录每次收到的 (幂等键, revision)。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def deliver(self, payload: dict[str, Any], *, idempotency_key: str,
                revision: int) -> tuple[bool, dict[str, Any]]:
        self.calls.append((idempotency_key, revision))
        return False, {"error": "saas_unavailable"}


class _FailThenSucceedAdapter(_AlwaysFailAdapter):
    """前 N 次失败、之后成功（验证"至少一次"最终仍送达）。"""

    def __init__(self, fail_times: int) -> None:
        super().__init__()
        self.fail_times = fail_times

    def deliver(self, payload: dict[str, Any], *, idempotency_key: str,
                revision: int) -> tuple[bool, dict[str, Any]]:
        self.calls.append((idempotency_key, revision))
        if self.fail_times > 0:
            self.fail_times -= 1
            return False, {"error": "saas_unavailable"}
        return True, {"receipt_id": f"r{len(self.calls)}"}


def _fast_outbox(**kw: Any) -> AlertOutbox:
    """把等待变成确定（不睡）：测的是预算逻辑，不是真实墙钟。"""
    kw.setdefault("sleep", lambda _s: None)
    return AlertOutbox(**kw)


def test_outbox_wall_clock_budget_marks_dead_below_max_attempts():
    """墙钟预算耗尽 → 进死信（即使次数远未用完），并留下可查的原因。"""
    ad = _AlwaysFailAdapter()
    ob = _fast_outbox(max_attempts=99, retry_deadline_s=0.0)
    ob.enqueue("k", 1, {"x": 1})
    result = ob.deliver(ad)
    entry = ob.list()[0]
    assert entry["status"] == "dead"
    assert entry["attempts"] == 1, "时间预算为 0 → 一次尝试后即放弃"
    assert entry["dead_reason"] == "retry_deadline"
    assert entry["last_error"] == "saas_unavailable"
    assert result == {"delivered": 0, "failed": 0, "dead": 1}
    assert ob.stats["dead"] == 1 and ob.stats["deadline_exceeded"] == 1
    print("PASS test_outbox_wall_clock_budget_marks_dead_below_max_attempts")


def test_outbox_attempts_budget_marks_dead_with_reason():
    """次数上限仍生效（先到为准的另一半），并注明是次数用尽而非时间。"""
    ad = _AlwaysFailAdapter()
    ob = _fast_outbox(max_attempts=2, retry_deadline_s=999.0)
    ob.enqueue("k", 1, {"x": 1})
    result = ob.deliver(ad)
    entry = ob.list()[0]
    assert entry["status"] == "dead" and entry["attempts"] == 2
    assert entry["dead_reason"] == "max_attempts"
    assert result["dead"] == 1 and result["failed"] == 1         # 中间那次算"重试"
    assert len(ad.calls) == 2
    print("PASS test_outbox_attempts_budget_marks_dead_with_reason")


def test_outbox_deliver_retries_in_one_call_and_keeps_idempotency():
    """单次 deliver 内重试到成功；**幂等键/revision 每次相同**（一次外部效果）。"""
    ad = _FailThenSucceedAdapter(fail_times=2)
    delays: list[float] = []
    ob = AlertOutbox(max_attempts=5, retry_deadline_s=999.0,
                     base_sleep_s=0.05, max_sleep_s=0.4, sleep=delays.append)
    ob.enqueue("dev:thermal:ep-1", 7, {"x": 1})
    result = ob.deliver(ad)
    entry = ob.list()[0]
    assert entry["status"] == "delivered" and entry["attempts"] == 3
    assert entry["receipt"] == "r3"
    assert result == {"delivered": 1, "failed": 2, "dead": 0}
    assert ob.stats["retries"] == 2 and ob.stats["dead"] == 0
    assert ad.calls == [("dev:thermal:ep-1", 7)] * 3, "重试必须复用同一 (键, revision)"
    assert len(delays) == 2 and all(d > 0.0 for d in delays), "两次失败之间要退避"
    print("PASS test_outbox_deliver_retries_in_one_call_and_keeps_idempotency")


def test_outbox_backoff_is_jittered_and_bounded():
    """退避有界且带抖动：不同 outbox 实例（多副本）不会同刻重投。"""
    ob = AlertOutbox(base_sleep_s=0.05, max_sleep_s=0.4)
    assert 0.025 <= ob.retry_backoff_s(1) <= 0.075
    assert 0.2 <= ob.retry_backoff_s(20) <= 0.6
    samples = {round(ob.retry_backoff_s(2), 6) for _ in range(50)}
    assert len(samples) > 1, "没有抖动 → 多副本同时重投（把下游瞬时故障放大）"
    print("PASS test_outbox_backoff_is_jittered_and_bounded")


# ================================================================ 5. LLM 客户端重试预算（D）

def test_provider_disables_sdk_level_retries():
    """`OpenAI(...)` 必须显式 `max_retries=0`：一次 chat = 一次 HTTP 往返。"""
    p = Provider.from_config("deepseek", "sk-test", timeout=1.0)
    assert p._client.max_retries == 0, (
        "SDK 默认会自己重试 2 次且无墙钟上限，会绕过调用方（_DeadlineProvider）的预算")
    assert p.timeout == 1.0
    print("PASS test_provider_disables_sdk_level_retries")


def test_provider_chat_passes_our_timeout_and_does_not_retry():
    """单次调用把**我们的**超时交给 SDK，失败即归一化上抛（由上层决定要不要再调）。

    用一个记录入参的假 client：断言 `timeout` 确实传下去了、且**一次 chat 只发一次**
    请求（SDK 不再自作主张重试）。
    """
    from data_dog.errors import ToolErrorCode
    p = Provider.from_config("deepseek", "sk-test", timeout=3.0)
    seen: list[dict[str, Any]] = []

    def create(**kw: Any) -> Any:
        seen.append(kw)
        raise TimeoutError("上游超时")

    p._client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    with pytest.raises(LLMCallError) as ei:
        p.chat([LLMChatMessage(role="user", content="hi")])
    assert ei.value.code == ToolErrorCode.TIMEOUT        # 失败关闭的稳定分类
    assert len(seen) == 1, "一次 chat 只应发一次请求（重试由我们决定）"
    assert seen[0]["timeout"] == 3.0
    print("PASS test_provider_chat_passes_our_timeout_and_does_not_retry")


# ================================================================ 6. 可信上下文 MCP 预算（E）
# 口径：看门狗 ≥ 调用超时；每次尝试超时被总预算夹住；退避有抖动；耗尽即失败关闭。

@pytest.fixture()
def mcp() -> Any:
    """取出 mcp_context 模块，并保证用例后清掉常驻 server 单例（防跨用例串味）。"""
    from data_dog import mcp_context as mc
    mc.close_persistent_context_server()
    yield mc
    mc.close_persistent_context_server()


def test_mcp_watchdog_is_never_shorter_than_call_timeout(mcp: Any):
    """不变量：看门狗 > 本次调用超时——否则会在超时之前就把还活着的 server 杀掉。"""
    for timeout_s in (0.0, 0.1, 1.0, 20.0):
        assert mcp._watchdog_s(timeout_s) > timeout_s
    assert mcp._watchdog_s(20.0, slack_s=0.5) == 20.5
    print("PASS test_mcp_watchdog_is_never_shorter_than_call_timeout")


def test_mcp_attempt_timeout_is_clamped_by_remaining_budget(mcp: Any):
    """单次尝试超时 = min(调用超时, 剩余总预算)；预算为负 → 0（不再发起尝试）。"""
    assert mcp._attempt_timeout_s(20.0, 5.0) == 5.0
    assert mcp._attempt_timeout_s(20.0, 100.0) == 20.0
    assert mcp._attempt_timeout_s(5.0, 0.0) == 0.0
    assert mcp._attempt_timeout_s(5.0, -3.0) == 0.0
    print("PASS test_mcp_attempt_timeout_is_clamped_by_remaining_budget")


def test_mcp_retry_backoff_is_jittered_and_bounded(mcp: Any):
    """退避带抖动（错开同刻重连），且在基数附近有界。"""
    base = mcp._RETRY_BASE_SLEEP_S
    assert base * 0.5 <= mcp._retry_delay_s(1) <= base * 1.5
    assert base <= mcp._retry_delay_s(2) <= base * 3.0
    samples = {round(mcp._retry_delay_s(1), 6) for _ in range(50)}
    assert len(samples) > 1, "没有抖动 → 多副本同刻重连"
    print("PASS test_mcp_retry_backoff_is_jittered_and_bounded")


def test_mcp_total_budget_gate_blocks_attempts(monkeypatch: pytest.MonkeyPatch,
                                               mcp: Any):
    """总预算为 0 → 连第一次尝试都不发起，直接失败关闭（预算不是装饰）。"""
    monkeypatch.setattr(mcp, "_RETRY_TOTAL_BUDGET_S", 0.0)
    with pytest.raises(mcp.McpContextError, match="预算"):
        mcp.fetch_trusted_context()
    assert mcp._PERSISTENT is None, "失败后不应留下一个已知坏掉的常驻单例"
    print("PASS test_mcp_total_budget_gate_blocks_attempts")


def test_mcp_normal_path_still_fetches_context(mcp: Any):
    """正常路径不变：能起真 server 并取回校验过的可信上下文。"""
    ctx = mcp.fetch_trusted_context()
    assert ctx.device_sn == "device-abc123"
    assert ctx.tenant_id == "tenant-default"
    assert mcp._PERSISTENT is not None
    print("PASS test_mcp_normal_path_still_fetches_context")
