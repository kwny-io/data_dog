"""候选状态写入的热路径口径：**每条事件只提交一次**，且不再有全局索引 key。

背景（两轮实测各挖出一个热路径单车道）
------------------------------------
1. **每个命中一次提交**。旧版 `SharedCandidateBuilder.add()` 对每个命中单独 `mutate`
   → 一次共享后端提交。一条事件常命中多条规则（落在多个 `base` key 上），于是
   "每请求提交次数 = 命中数 + 限流"，而**提交吞吐就是写路径的实测天花板**
   （`docs/write_path_batching.md` §2.2）。现在整条事件的全部命中走一次
   `mutate_many`：N 次提交 → **1 次**。
2. **全局索引 key** `candidate:index`。旧版每个新出现的 base 都要写它一次，而它是
   **一把全局行锁**——所有设备、所有租户的命中都在同一条提交链上排队（先前的缓解是
   实例内去重）。它只服务 `build(only_touched=False)` 的运维/对账枚举，对热路径
   **零收益**。现在改为**前缀扫描**（`keys("cand:")`），索引彻底删除。

本文件锁四件事：
1. 一条事件 N 个命中 → 共享后端只被写**一次**（不是 N 次）；
2. 同一 base 在一次事件里命中多次 → **不丢更新**（同一事务内按序应用）；
3. 全量枚举（`only_touched=False`）跨副本仍完整——前缀扫描与旧的"并集索引"等价；
4. 热路径 `build()` 仍只返回本实例触碰过的组（语义没被改松）。
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from data_dog.data_plane.candidate_shared import SharedCandidateBuilder
from data_dog.state.memory import InMemoryStateStore

# 三个命中 → 三个不同 signal 族（temperature_high / current_anomaly / fault_flag）
# → 同一部件、同一事件上落 **三个不同 base key**。
HITS_3 = [
    {"rule_id": "MOTOR_TEMP_PROTECT", "field": "temperature", "value": 95,
     "severity_hint": "critical", "operator_id": "d@1"},
    {"rule_id": "MOTOR_OVERCURRENT", "field": "current", "value": 12.0,
     "severity_hint": "warning", "operator_id": "d@1"},
    {"rule_id": "ENCODER_FAULT", "field": "mode", "value": 3,
     "severity_hint": "critical", "operator_id": "d@1"},
]

INDEX_KEY = "candidate:index"


class CountingStore(InMemoryStateStore):
    """数"共享后端被写了几次"——语义与父类完全一致，只加计数。

    这就是热路径真正要看的量：**提交次数**。PG 后端下 `mutate` 与 `mutate_many`
    各对应一次 `COMMIT`（后者整批一次），所以 `commits` 直接可比。
    """

    def __init__(self) -> None:
        super().__init__()
        self.mutates: Counter[str] = Counter()
        self.commits = 0
        self.batches: list[list[str]] = []

    def mutate(self, key: str, fn: Any) -> Any:
        self.commits += 1
        self.mutates[key] += 1
        return super().mutate(key, fn)

    def mutate_many(self, items: Any) -> list[Any]:
        keys = [k for k, _ in items]
        if keys:
            self.commits += 1                      # 整批 = 一次提交
        self.batches.append(keys)
        for k in keys:
            self.mutates[k] += 1
        return super().mutate_many(items)


def _add(b: SharedCandidateBuilder, device: str, *,
         at: str = "2026-09-29T10:00:00Z", hits: list[dict[str, Any]] | None = None) -> None:
    b.add(device_sn=device, component_ref="joint_motor.hip", metrics={},
          hits=(HITS_3 if hits is None else hits), occurred_at=at, received_at="x")


# ---------------------------------------------------------------- 提交次数

def test_one_event_with_many_hits_is_a_single_commit():
    """一条事件 3 个命中 → 只提交 1 次（旧版是 3 次）。"""
    store = CountingStore()
    b = SharedCandidateBuilder(state_store=store)
    _add(b, "dev")

    assert store.commits == 1, f"3 个命中共用了 {store.commits} 次提交（应为 1）"
    assert len(store.batches) == 1, store.batches
    assert len(store.batches[0]) == 3, store.batches
    assert len(set(store.batches[0])) == 3, f"3 个命中应落在 3 个不同 base：{store.batches}"


def test_steady_state_is_one_commit_per_event():
    """稳态（每秒一条遥测 × 20）→ 20 次提交；旧版是 20×命中数，还要外加索引登记。"""
    store = CountingStore()
    b = SharedCandidateBuilder(state_store=store, merge_window_s=5.0)
    for i in range(20):
        _add(b, "dev", at=f"2026-09-29T10:00:{i:02d}Z")

    assert store.commits == 20, store.commits


def test_empty_hits_do_not_touch_shared_state():
    """没有命中就不该碰共享后端（别为"没有异常"也付一次提交）。"""
    store = CountingStore()
    b = SharedCandidateBuilder(state_store=store)
    _add(b, "dev", hits=[])

    assert store.commits == 0
    assert store.keys("") == []


# ---------------------------------------------------------------- 索引已删除

def test_no_global_index_key_is_ever_written():
    """全局索引 key 彻底消失：共享后端里只剩 base key。"""
    store = CountingStore()
    b = SharedCandidateBuilder(state_store=store)
    for i in range(5):
        _add(b, f"dev-{i}")

    assert INDEX_KEY not in store.keys(""), sorted(store.keys(""))
    assert all(k.startswith("cand:") for k in store.keys("")), sorted(store.keys(""))


# ---------------------------------------------------------------- 不丢更新

def test_repeated_hits_on_same_base_apply_in_order():
    """同一 base 在一次事件里命中两次 → 同批同 key 出现两次，按序应用（sample_count=2）。"""
    store = CountingStore()
    b = SharedCandidateBuilder(state_store=store)
    _add(b, "dev", hits=[HITS_3[0], HITS_3[0]])

    assert len(store.batches) == 1, store.batches
    assert store.batches[0][0] == store.batches[0][1], (
        f"两次命中应落在同一 base：{store.batches}")
    cands = b.build()
    assert len(cands) == 1, cands
    assert cands[0].sample_count == 2, (
        f"同一事务内两次命中被覆盖了（sample_count={cands[0].sample_count}）")


# ---------------------------------------------------------------- 枚举完整性

def test_full_enumeration_is_complete_across_replicas():
    """前缀扫描与旧"并集索引"等价：任一副本写过的 base，别的副本也枚举得到。"""
    store = CountingStore()
    a = SharedCandidateBuilder(state_store=store)
    b = SharedCandidateBuilder(state_store=store)
    _add(a, "dev-a")
    _add(b, "dev-b")

    assert {c.device_sn for c in a.build(only_touched=False)} == {"dev-a", "dev-b"}


def test_full_enumeration_survives_many_bases():
    """base 数量不受任何实例内缓存上限影响（索引版曾有 4096 的 FIFO 淘汰）。

    50 个设备 × 3 个信号族 = 150 个 base，每个 base 一个候选。
    """
    store = CountingStore()
    b = SharedCandidateBuilder(state_store=store)
    for i in range(50):
        _add(b, f"dev-{i:03d}")

    out = b.build(only_touched=False)
    assert len({c.device_sn for c in out}) == 50
    assert len(out) == 150, len(out)


def test_hot_path_build_only_returns_own_candidates():
    """热路径 `build()` 仍只返回本实例触碰过的组（语义没被改松）。

    HITS_3 落三个信号族 → 每个设备三个候选；关键是**不含别的副本的设备**。
    """
    store = CountingStore()
    a = SharedCandidateBuilder(state_store=store)
    b = SharedCandidateBuilder(state_store=store)
    _add(a, "dev-a")
    _add(b, "dev-b")

    assert {c.device_sn for c in a.build()} == {"dev-a"}
    assert {c.device_sn for c in b.build()} == {"dev-b"}
