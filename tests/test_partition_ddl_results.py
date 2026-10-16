"""结构变更的**结果记账**（A9 失败告警②）：连续失败 / 上次成功 / 上次失败。

为什么要独立测：这套记账跨**进程**（CronJob 写共享 state、service 在读时转成指标），错一处
就会出现"迁移一直在失败，但指标说一切正常"——那正是引入它的原因。所以既测状态机本身，
也测"读共享 state → set Gauge"这条转发链。
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest
from prometheus_client import generate_latest

from data_dog import partition_ddl
from data_dog.core import metrics
from data_dog.service import partition_maintenance as pm

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture()
def clean_state():
    from data_dog.state import reset_state_store

    reset_state_store()
    yield
    reset_state_store()


def _load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_dd_{name}", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _raw() -> dict:
    from data_dog.state import get_state_store

    return get_state_store().get(partition_ddl.PARTITION_DDL_RESULT_KEY) or {}


# ---------------------------------------------------------------- 状态机

def test_failures_accumulate_and_success_resets_them(clean_state):
    partition_ddl.record_result("ensure", ok=False, kind="error", detail="boom")
    partition_ddl.record_result("ensure", ok=False, kind="refused", detail="no")
    assert partition_ddl.ddl_results()["ensure"]["fails"] == 2
    assert partition_ddl.ddl_results()["ensure"]["kind"] == "refused"

    partition_ddl.record_result("ensure", ok=True)
    rec = partition_ddl.ddl_results()["ensure"]
    assert rec["ok"] is True and rec["fails"] == 0 and rec["detail"] == ""


def test_success_keeps_the_last_failure_timestamp(clean_state):
    """连续失败数要归零（回答"现在是不是坏的"），但"上次什么时候坏过"必须留下。"""
    partition_ddl.record_result("convert", ok=False, kind="error", detail="x")
    failed_at = partition_ddl.ddl_results()["convert"]["fail_at"]
    ok_at_before = partition_ddl.ddl_results()["convert"]["ok_at"]
    assert ok_at_before == 0                    # 从没成功过

    partition_ddl.record_result("convert", ok=True)
    rec = partition_ddl.ddl_results()["convert"]
    assert rec["fails"] == 0
    assert rec["fail_at"] == failed_at, "成功把失败历史抹掉了——「上次什么时候坏过」就查不到了"
    assert rec["ok_at"] > 0


def test_ops_are_recorded_independently(clean_state):
    partition_ddl.record_result("ensure", ok=False, kind="error")
    partition_ddl.record_result("drop", ok=True)
    results = partition_ddl.ddl_results()
    assert results["ensure"]["fails"] == 1
    assert results["drop"]["fails"] == 0


def test_detail_is_truncated_for_the_state_store(clean_state):
    partition_ddl.record_result("unpartition", ok=False, kind="error", detail="x" * 900)
    assert len(partition_ddl.ddl_results()["unpartition"]["detail"]) == 500


def test_unknown_ops_and_non_dict_values_are_filtered(clean_state):
    """共享 state 可能被手工改过或是旧版本写的：脏 key 不该进 Prometheus 标签（基数）。"""
    from data_dog.state import get_state_store

    get_state_store().mutate(
        partition_ddl.PARTITION_DDL_RESULT_KEY,
        lambda _cur: ({"ensure": {"fails": 3}, "bogus": {"fails": 9}, "drop": "nope"}, None))

    results = partition_ddl.ddl_results()
    assert results == {"ensure": {"fails": 3}}


def test_bookkeeping_failure_is_swallowed(clean_state, monkeypatch):
    """记账失败不该把一次转换升级成"迁移失败"——但也不许撒谎（这次就是没记上）。"""
    def boom():
        raise RuntimeError("state store 挂了")

    monkeypatch.setattr(partition_ddl, "get_state_store", boom)
    partition_ddl.record_result("ensure", ok=True)          # 不抛
    assert partition_ddl.ddl_results() == {}                # 读也失败 → 空，不编造


# ---------------------------------------------------------------- 转发成指标

def test_report_ddl_results_publishes_three_gauges(clean_state):
    partition_ddl.record_result("ensure", ok=False, kind="error", detail="boom")
    partition_ddl.record_result("ensure", ok=False, kind="error", detail="boom")
    partition_ddl.record_result("drop", ok=True)

    results = pm.report_ddl_results()
    assert set(results) == {"ensure", "drop"}

    assert metrics.partition_ddl_consecutive_failures.labels(op="ensure")._value.get() == 2
    assert metrics.partition_ddl_last_failure_timestamp_seconds.labels(op="ensure")._value.get() > 0
    assert metrics.partition_ddl_last_success_timestamp_seconds.labels(op="ensure")._value.get() == 0
    assert metrics.partition_ddl_consecutive_failures.labels(op="drop")._value.get() == 0
    assert metrics.partition_ddl_last_success_timestamp_seconds.labels(op="drop")._value.get() > 0

    body = generate_latest(metrics.registry).decode()
    assert 'data_dog_partition_ddl_consecutive_failures{op="ensure"} 2.0' in body


def test_report_ddl_results_is_a_noop_when_nothing_was_recorded(clean_state):
    """CronJob 一整天没跑 → 一个样本都没有，不该凭空造出 op 标签。"""
    assert pm.report_ddl_results() == {}


# ---------------------------------------------------------------- backfill 脚本记账

def test_backfill_helper_records_success_and_failure(clean_state, monkeypatch):
    mod = _load_script("backfill_created_day")

    def ok(_s, _t, *, batch, log):
        return {"updated": 3, "batches": 1}

    monkeypatch.setattr(mod, "backfill_created_day", ok)
    rep = mod._backfill(None, "telemetry_events", 10, print)
    assert rep == {"updated": 3, "batches": 1}
    rec = partition_ddl.ddl_results()["backfill"]
    assert rec["ok"] is True and "updated=3" in rec["detail"]

    def boom(*_a, **_k):
        raise RuntimeError("库断了")

    monkeypatch.setattr(mod, "backfill_created_day", boom)
    with pytest.raises(RuntimeError):
        mod._backfill(None, "telemetry_events", 10, print)
    rec = partition_ddl.ddl_results()["backfill"]
    assert rec["ok"] is False and rec["fails"] == 1 and rec["kind"] == "error"
    assert rec["ok_at"] > 0, "失败把上次成功的时间戳抹了"

