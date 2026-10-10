"""候选索引登记：热路径上**不该**每次命中都去写共享后端那个全局 key。

背景（实测出来的热路径单车道）
----------------------------
`SharedCandidateBuilder.add()` 每次命中都会 `_register_index(base)`，而索引是**一个全局
key**（`_INDEX_KEY`）。在 postgres/redis 后端下，一次 `mutate` = 拿行锁 → 改 → 提交，
于是**所有设备、所有租户的命中都排在同一个行锁后面**：吞吐被钉在单条提交链上。

而这条登记对热路径**没有收益**——HTTP 走 `build(only_touched=True)`（读本地 `_touched`），
索引只服务 `build(only_touched=False)` 的运维/对账全量枚举。

本文件锁三件事：
1. 同一 base 反复命中 → 索引只登记**一次**（去重生效）；
2. 不同 base 各登记一次（没有被误合并）；
3. 去重**不破坏**全量枚举的完整性（跨副本并集语义不变），淘汰后仍能补登记。
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from data_dog.data_plane.candidate_shared import _INDEX_KEY, SharedCandidateBuilder
from data_dog.state.memory import InMemoryStateStore

HIT = [{"rule_id": "MOTOR_TEMP_PROTECT", "field": "temperature", "value": 95,
        "severity_hint": "critical", "operator_id": "d@1"}]


class CountingStore(InMemoryStateStore):
    """按 key 统计 mutate 次数（只统计，语义与父类完全一致）。"""

    def __init__(self) -> None:
        super().__init__()
        self.mutates: Counter[str] = Counter()

    def mutate(self, key: str, fn: Any) -> Any:
        self.mutates[key] += 1
        return super().mutate(key, fn)


def _add(b: SharedCandidateBuilder, device: str, at: str = "2026-09-29T10:00:00Z") -> None:
    b.add(device_sn=device, component_ref="joint_motor.hip", metrics={}, hits=HIT,
          occurred_at=at, received_at="x")


# ---------------------------------------------------------------- 去重

def test_repeated_hits_register_index_once():
    """同设备反复命中（正常稳态：每秒一条遥测）→ 索引只写一次，而不是每条一次。"""
    store = CountingStore()
    b = SharedCandidateBuilder(state_store=store, merge_window_s=5.0)
    for i in range(20):
        _add(b, "dev", at=f"2026-09-29T10:00:{i:02d}Z")

    assert store.mutates[_INDEX_KEY] == 1, (
        f"索引被登记了 {store.mutates[_INDEX_KEY]} 次——热路径上的全局锁没去掉")
    # 分组状态仍然每次都写（那是热路径真正需要的原子读改写）
    group_writes = sum(v for k, v in store.mutates.items() if k != _INDEX_KEY)
    assert group_writes == 20, store.mutates


def test_distinct_bases_are_each_registered_once():
    """不同设备/部件/信号各自登记一次，不会被去重误合并。"""
    store = CountingStore()
    b = SharedCandidateBuilder(state_store=store)
    for dev in ("d1", "d2", "d3"):
        _add(b, dev)
        _add(b, dev)              # 每个设备打两次

    assert store.mutates[_INDEX_KEY] == 3, store.mutates
    assert len(b.build(only_touched=False)) == 3


def test_dedup_is_per_instance_not_global():
    """两个实例各自登记自己见过的 base（并集语义不变）→ 各写一次索引。"""
    store = CountingStore()
    a = SharedCandidateBuilder(state_store=store)
    bld = SharedCandidateBuilder(state_store=store)
    _add(a, "dev-a")
    _add(bld, "dev-b")

    assert store.mutates[_INDEX_KEY] == 2
    assert len(a.build(only_touched=False)) == 2, "全量枚举应看到两个副本的设备并集"


# ---------------------------------------------------------------- 完整性

def test_full_enumeration_still_complete_after_dedup():
    """去重后，`only_touched=False` 仍能枚举出**所有**出现过的 base。"""
    store = CountingStore()
    b = SharedCandidateBuilder(state_store=store)
    for i in range(5):
        _add(b, f"dev-{i}")
    b.build()                      # 清空 _touched，模拟"已 emit 过"
    for i in range(5):             # 再来一轮：索引已有这些 base
        _add(b, f"dev-{i}")

    assert len(b.build(only_touched=False)) == 5
    assert store.mutates[_INDEX_KEY] == 5, "第二轮不该再写索引"


def test_cache_eviction_re_registers_without_duplicating(monkeypatch):
    """缓存有界：被淘汰的 base 下次命中会补登记，且索引里**不出现重复项**。"""
    import data_dog.data_plane.candidate_shared as cs

    monkeypatch.setattr(cs, "_INDEX_CACHE_MAX", 2)
    store = CountingStore()
    b = SharedCandidateBuilder(state_store=store)
    for dev in ("d1", "d2", "d3"):        # 容量 2 → d1 会被淘汰
        _add(b, dev)
    _add(b, "d1")                          # 补登记

    assert store.mutates[_INDEX_KEY] == 4
    index = store.get(cs._INDEX_KEY)
    assert len(index) == len(set(index)) == 3, index
    assert len(b.build(only_touched=False)) == 3


def test_hot_path_build_only_returns_own_candidates():
    """热路径 build() 仍只返回本实例触碰过的组（去重没把语义改松）。"""
    store = CountingStore()
    a = SharedCandidateBuilder(state_store=store)
    b = SharedCandidateBuilder(state_store=store)
    _add(a, "dev-a")
    _add(b, "dev-b")

    assert [c.device_sn for c in a.build()] == ["dev-a"]
    assert [c.device_sn for c in b.build()] == ["dev-b"]
