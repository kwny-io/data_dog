"""P4-5 车队级分片测试：确定性路由 / 最小扰动 / 同设备不分裂 / 归属过滤。

为什么这些断言重要
------------------
分片的两类致命错误都是**静默**的：
- **归属判定不确定**：同键在不同副本算出不同属主 → 消息被所有副本"让路"或重复处理。
  故必须钉住"同键恒同属主"、"成员列表相同则全副本一致"。
- **状态分裂**：同设备事件被路由到不同副本 → 半条轨迹各落一处，产出两条残缺候选。
  故必须钉住"同设备所有事件恒落同一属主"。

另：rendezvous 的意义是**最小扰动**——副本增减时只迁移约 1/N 键。这点若不测，
很容易退化成"取模哈希"还以为没事。用分布均匀性 + 迁移比例两条钉住。
"""
from __future__ import annotations

import pytest

from data_dog.stream.base import StreamMessage
from data_dog.stream.sharding import (
    ShardRouter,
    pick_owner,
    pick_shard,
    shard_key,
)

# ---------------------------------------------------------------- 分片键

def test_shard_key_default_is_tenant_device():
    assert shard_key(tenant_id="t1", device_sn="go2-01") == "t1|go2-01"


def test_shard_key_with_component():
    assert shard_key(tenant_id="t1", device_sn="go2-01",
                     component="joint_motor") == "t1|go2-01|joint_motor"


def test_same_device_same_key_regardless_of_component_default():
    """默认不纳入 component：同设备不同部件 → 同键（状态不分裂）。"""
    a = shard_key(tenant_id="t1", device_sn="go2-01")
    b = shard_key(tenant_id="t1", device_sn="go2-01")
    assert a == b


# ---------------------------------------------------------------- 确定性路由

def test_pick_shard_deterministic_and_in_range():
    for k in ("t1|go2-01", "t1|go2-02", "t2|go2-01"):
        s = pick_shard(k, 256)
        assert 0 <= s < 256
        assert pick_shard(k, 256) == s            # 同键恒同分片


def test_pick_owner_deterministic():
    reps = ["r0", "r1", "r2"]
    o = pick_owner("t1|go2-01", reps)
    assert o in reps
    assert all(pick_owner("t1|go2-01", reps) == o for _ in range(5))


def test_pick_owner_empty_replicas_returns_blank():
    assert pick_owner("k", []) == ""


def test_same_device_same_owner_across_all_events():
    """核心保证：同设备（不同事件）恒落同一属主 → 窗口状态不分裂。"""
    reps = ["r0", "r1", "r2", "r3"]
    key = shard_key(tenant_id="t1", device_sn="go2-07")
    owners = {pick_owner(key, reps) for _ in range(100)}
    assert len(owners) == 1


# ---------------------------------------------------------------- 分布均匀性

def test_distribution_reasonably_uniform():
    """1000 个设备键在 4 副本上应大致均匀（不要求精确，只排除严重倾斜）。"""
    reps = ["r0", "r1", "r2", "r3"]
    counts = {r: 0 for r in reps}
    for i in range(1000):
        counts[pick_owner(f"t1|dev-{i}", reps)] += 1
    # 期望各 250；允许 ±40% 波动（rendezvous 在小样本下有抖动，但不应严重偏斜）
    for r, n in counts.items():
        assert 150 <= n <= 350, f"{r} 分到 {n}（严重倾斜，疑似路由退化）"


# ---------------------------------------------------------------- 最小扰动

def test_minimal_disruption_on_add_replica():
    """4→5 副本：只有约 1/5 的键换属主（rendezvous 最小扰动）。

    若退化为取模哈希，会有约 4/5 的键换主——本条会失败。
    """
    base = ["r0", "r1", "r2", "r3"]
    more = base + ["r4"]
    keys = [f"t1|dev-{i}" for i in range(2000)]
    moved = sum(1 for k in keys if pick_owner(k, base) != pick_owner(k, more))
    ratio = moved / len(keys)
    # 理论 1/5=0.2；crc32 小样本下有偏（实测≈0.12），允许 0.08~0.30。
    # 下限 0.08 远高于"取模哈希会换 4/5=0.8"——足以区分 rendezvous 与取模。
    assert 0.08 <= ratio <= 0.30, f"迁移比例 {ratio:.3f} 偏离 1/5，疑似非 rendezvous"


def test_minimal_disruption_on_remove_replica():
    base = ["r0", "r1", "r2", "r3"]
    less = ["r0", "r1", "r2"]
    keys = [f"t1|dev-{i}" for i in range(2000)]
    moved = sum(1 for k in keys if pick_owner(k, base) != pick_owner(k, less))
    ratio = moved / len(keys)
    # 移除 r3：只有原本归属 r3 的键（约 1/4）迁移
    assert 0.18 <= ratio <= 0.32, f"迁移比例 {ratio:.3f} 偏离 1/4"


# ---------------------------------------------------------------- ShardRouter

def test_router_single_replica_owns_everything():
    """单副本：不过滤（owns 恒真）——行为与分片前一致，不丢任何消息。"""
    r = ShardRouter(replica_id="r0", members=["r0"])
    assert not r.enabled
    assert r.owns("t1|anything")


def test_router_empty_members_owns_everything():
    r = ShardRouter(replica_id="r0", members=[])
    assert not r.enabled
    assert r.owns("t1|anything")


def test_router_partitions_keys_exactly_once():
    """多副本：每个键恰有一个属主持有（无遗漏、无重复）。"""
    members = ["r0", "r1", "r2"]
    routers = [ShardRouter(replica_id=m, members=members) for m in members]
    keys = [f"t1|dev-{i}" for i in range(500)]
    for k in keys:
        owners = [rt for rt in routers if rt.owns(k)]
        assert len(owners) == 1, f"键 {k} 有 {len(owners)} 个属主（应恰 1 个）"


def test_router_consistent_across_replicas():
    """所有副本对同一键的看法一致——属主自认拥有，非属主自认不拥有。"""
    members = ["r0", "r1", "r2"]
    r0 = ShardRouter(replica_id="r0", members=members)
    r1 = ShardRouter(replica_id="r1", members=members)
    key = "t1|go2-01"
    owner = r0.owner_of(key)
    for rt in (r0, r1):
        assert rt.owns(key) == (rt.replica_id == owner)


def test_router_member_order_does_not_matter():
    """成员列表顺序不应影响归属（内部排序）。"""
    a = ShardRouter(replica_id="r1", members=["r0", "r1", "r2"])
    b = ShardRouter(replica_id="r1", members=["r2", "r0", "r1"])
    for k in ("t1|dev-1", "t1|dev-2", "t2|dev-9"):
        assert a.owns(k) == b.owns(k)


def test_router_self_not_in_members_owns_nothing():
    """游离副本（自己不在成员列表）：owns 恒假——失败关闭，不抢别人的键。"""
    r = ShardRouter(replica_id="rX", members=["r0", "r1", "r2"])
    assert r.enabled
    for k in ("t1|dev-1", "t1|dev-2", "t1|dev-3"):
        assert not r.owns(k)


# ---------------------------------------------------------------- runner 集成

def _plane():
    from data_dog.data_plane.candidate_shared import SharedCandidateBuilder
    from data_dog.data_plane.p2_demo import _detector_config
    from data_dog.data_plane.pipeline import DataPlane, build_demo_resolver
    from data_dog.state.memory import InMemoryStateStore
    return DataPlane(build_demo_resolver(_detector_config()),
                     builder=SharedCandidateBuilder(
                         state_store=InMemoryStateStore()))


class _FakeSource:
    name = "fake"

    def __init__(self, msgs):
        self._msgs = msgs

    def poll(self, timeout):
        out, self._msgs = self._msgs, []
        return out

    def coordination(self):
        return "none"

    def close(self):
        pass


def _msg(device):
    import json
    body = {"event_id": f"{device}-1",
            "occurred_at": "2026-09-13T10:00:00Z",
            "received_at": "2026-09-13T10:00:00.1Z", "sequence": 1,
            "vendor": "unitree", "model": "go2", "firmware": "3.2.1",
            "protocol_version": "motor-log-v2",
            "component_hint": "joint_motor.front_left_hip",
            "payload_inline": json.dumps({"temperature": 95, "current": 12.0,
                                          "mode": 0})}
    return StreamMessage(payload=body,
                         auth={"tenant_id": "t1", "device_sn": device,
                               "principal": "test"}, source="fake")


def test_runner_filters_non_owned_keys():
    """runner + router：不归本副本的键被跳过，计入 shard_skipped。"""
    from data_dog.stream.runner import StreamRunner
    members = ["r0", "r1", "r2"]
    router = ShardRouter(replica_id="r0", members=members)
    devices = [f"go2-{i:03d}" for i in range(30)]
    msgs = [_msg(d) for d in devices]
    persisted: list = []
    runner = StreamRunner(_plane(), _FakeSource(msgs),
                          persist=lambda t, n, c: persisted.append((t, n, c)),
                          router=router)
    runner._drain(msgs)
    runner.flush()
    # 本副本只处理归属自己的设备；其余跳过
    owned = sum(1 for d in devices
                if router.owns(shard_key(tenant_id="t1", device_sn=d)))
    assert runner.stats["shard_skipped"] == len(devices) - owned
    assert owned > 0 and owned < len(devices)   # 确有分工，非全拥有/全跳过


def test_runner_without_router_processes_all():
    """无 router：全部处理，shard_skipped 恒 0（向后兼容）。"""
    from data_dog.stream.runner import StreamRunner
    devices = ["go2-01", "go2-02", "go2-03"]
    msgs = [_msg(d) for d in devices]
    runner = StreamRunner(_plane(), _FakeSource(msgs),
                          persist=lambda t, n, c: None)
    runner._drain(msgs)
    runner.flush()
    assert runner.stats["shard_skipped"] == 0


def test_runner_same_device_never_split_across_replicas():
    """核心：同设备的多条事件在任意副本集合下，恒只被一个副本处理。"""
    members = ["r0", "r1", "r2"]
    routers = {m: ShardRouter(replica_id=m, members=members) for m in members}
    device = "go2-42"
    key = shard_key(tenant_id="t1", device_sn=device)
    handlers = [m for m, rt in routers.items() if rt.owns(key)]
    assert len(handlers) == 1                 # 恰一个副本接手该设备


# ---------------------------------------------------------------- build_router 装配

class _Src:
    def __init__(self, coord):
        self.name = "x"
        self._coord = coord

    def coordination(self):
        return self._coord


def test_build_router_disabled_when_no_members(monkeypatch):
    monkeypatch.delenv("DD_REPLICA_MEMBERS", raising=False)
    monkeypatch.delenv("DD_REPLICA_ID", raising=False)
    from data_dog.stream.factory import build_router
    assert build_router(_Src("none")) is None


def test_build_router_disabled_for_single_member(monkeypatch):
    monkeypatch.setenv("DD_REPLICA_MEMBERS", "r0")
    monkeypatch.setenv("DD_REPLICA_ID", "r0")
    from data_dog.stream.factory import build_router
    assert build_router(_Src("none")) is None


def test_build_router_disabled_for_broker_group(monkeypatch):
    """broker 已分区（kafka/mqtt 共享订阅）：不叠加客户端过滤（否则丢事件）。"""
    monkeypatch.setenv("DD_REPLICA_MEMBERS", "r0,r1,r2")
    monkeypatch.setenv("DD_REPLICA_ID", "r0")
    from data_dog.stream.factory import build_router
    assert build_router(_Src("broker-group")) is None


def test_build_router_enabled_for_uncoordinated_source(monkeypatch):
    """每个副本收全量的源（coordination=none）+ 多成员 → 启用分片过滤。"""
    monkeypatch.setenv("DD_REPLICA_MEMBERS", "r0,r1,r2")
    monkeypatch.setenv("DD_REPLICA_ID", "r0")
    from data_dog.stream.factory import build_router
    router = build_router(_Src("none"))
    assert router is not None
    assert router.enabled
    assert router.replica_id == "r0"
    assert router.members == ["r0", "r1", "r2"]


def test_build_router_self_not_in_members_fails_closed(monkeypatch):
    """配了成员列表但本副本不在其中 → 启动即报错（防"跳过所有消息"的静默丢数据）。"""
    monkeypatch.setenv("DD_REPLICA_MEMBERS", "r0,r1,r2")
    monkeypatch.setenv("DD_REPLICA_ID", "rX")          # 不在列表里
    from data_dog.stream.factory import build_router
    with pytest.raises(RuntimeError, match="不在"):
        build_router(_Src("none"))
