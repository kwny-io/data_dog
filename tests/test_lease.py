"""流式源多副本协调测试（G5）：租约选主 + 共享偏移 + MQTT 共享订阅 + 工厂装配。

为什么这些断言重要
------------------
多副本下 file 源若无协调会**全量重复消费**（候选取重、限流口径翻倍）——是最隐蔽
的一类"数据看着对、其实多算了一遍"错误。故这里逐条钉住：
- 租约：同一时刻只有一个持有者；过期可抢占；只有持有者能续期；
- file 源：非持有者不读文件；持有者崩溃→旁人在 TTL 后接管，**从共享偏移续读**
  （不丢已消费、不重放半行）；
- MQTT：拼出 `$share/<group>/<topic>`；空组退回原 topic；
- 工厂：按配置装配租约与共享订阅。

全离线：memory 状态后端 + 标准库；mqtt 用**假 client**（不连真 broker）。
"""
from __future__ import annotations

import json

from data_dog.state.lease import Lease
from data_dog.state.memory import InMemoryStateStore
from data_dog.stream.bus import MqttSource, shared_topic
from data_dog.stream.factory import build_source
from data_dog.stream.file import FileTailSource


class _Clock:
    """可控时钟：测试里手动推进时间，避免 sleep。"""
    def __init__(self, t0: float = 1000.0) -> None:
        self.t = t0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _raw(event_id, temp=95):
    return json.dumps({
        "event_id": event_id, "occurred_at": "2026-09-13T10:00:00Z",
        "received_at": "2026-09-13T10:00:00.1Z", "sequence": 1,
        "vendor": "unitree", "model": "go2", "firmware": "3.2.1",
        "protocol_version": "motor-log-v2",
        "component_hint": "joint_motor.front_left_hip",
        "payload_inline": json.dumps({"temperature": temp, "current": 12.0,
                                      "mode": 0}),
    })


# ---------------------------------------------------------------- 租约原语

def test_lease_single_holder():
    """两个副本抢同一租约 → 只有一个成功。"""
    st = InMemoryStateStore()
    clk = _Clock()
    a = Lease(st, "k", "hostA:1", ttl_s=30, clock=clk)
    b = Lease(st, "k", "hostB:2", ttl_s=30, clock=clk)
    assert a.acquire() is True
    assert b.acquire() is False            # A 持有且未过期
    assert a.holder() == "hostA:1"
    assert b.holder() == "hostA:1"


def test_lease_expiry_allows_takeover():
    """持有者不续约 → TTL 过后旁人可抢占。"""
    st = InMemoryStateStore()
    clk = _Clock()
    a = Lease(st, "k", "hostA:1", ttl_s=30, clock=clk)
    b = Lease(st, "k", "hostB:2", ttl_s=30, clock=clk)
    assert a.acquire() is True
    clk.advance(10)
    assert b.acquire() is False            # 未过期
    clk.advance(25)                        # 累计 35 > 30
    assert b.acquire() is True             # 过期 → B 接管
    assert b.holder() == "hostB:2"
    assert a.holder() == "hostB:2"


def test_lease_renew_only_by_holder():
    """renew 只续自己的租约，**不会**抢占他人。"""
    st = InMemoryStateStore()
    clk = _Clock()
    a = Lease(st, "k", "hostA:1", ttl_s=30, clock=clk)
    b = Lease(st, "k", "hostB:2", ttl_s=30, clock=clk)
    assert a.acquire() is True
    assert b.renew() is False              # B 非持有者，renew 无效
    assert b.acquire() is False            # 抢不走 A 的有效租约
    assert a.renew() is True               # A 续期成功
    assert a.holder() == "hostA:1"


def test_lease_release_frees_immediately():
    st = InMemoryStateStore()
    clk = _Clock()
    a = Lease(st, "k", "hostA:1", ttl_s=30, clock=clk)
    b = Lease(st, "k", "hostB:2", ttl_s=30, clock=clk)
    a.acquire()
    a.release()
    assert a.holder() == ""                # 释放后无主
    assert b.acquire() is True             # 旁人立即接管（无需等 TTL）


# ---------------------------------------------------------------- file 选主

def test_file_non_holder_does_not_read(tmp_path):
    """非持有者 poll 返回空（不读文件），即便文件有新内容。"""
    st = InMemoryStateStore()
    clk = _Clock()
    f = tmp_path / "robot.log"
    f.write_text("", encoding="utf-8")

    holder = FileTailSource(str(f), tenant_id="t", device_sn="d",
                            lease=Lease(st, "lk", "holder", ttl_s=30, clock=clk))
    other = FileTailSource(str(f), tenant_id="t", device_sn="d",
                           lease=Lease(st, "lk", "other", ttl_s=30, clock=clk))
    holder.poll(0.0)                       # holder 抢占并打开（定位末尾）
    assert holder.lease_holder() == "holder"

    with open(f, "a", encoding="utf-8") as fh:
        fh.write(_raw("e1") + "\n")

    assert other.poll(0.0) == []           # 非持有者 → 空转，不读
    msgs = holder.poll(0.0)                 # 持有者读到
    assert len(msgs) == 1 and msgs[0].payload["event_id"] == "e1"


def test_file_takeover_resumes_from_shared_offset(tmp_path):
    """持有者"崩溃" → 旁人接管 → 从**共享偏移**续读（不重不丢）。"""
    st = InMemoryStateStore()
    clk = _Clock()
    f = tmp_path / "robot.log"
    f.write_text("", encoding="utf-8")

    a = FileTailSource(str(f), tenant_id="t", device_sn="d", shared_offset=True,
                       lease=Lease(st, "lk", "hostA", ttl_s=30, clock=clk))
    a.poll(0.0)                            # A 抢占，定位末尾（空文件 → off=0）
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(_raw("e1") + "\n")
    got_a = a.poll(0.0)
    assert [m.payload["event_id"] for m in got_a] == ["e1"]
    # A 消费完 e1 后，"崩溃"（不再 poll、不续约）
    clk.advance(31)                        # 租约过期

    b = FileTailSource(str(f), tenant_id="t", device_sn="d", shared_offset=True,
                       lease=Lease(st, "lk", "hostB", ttl_s=30, clock=clk))
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(_raw("e2") + "\n")
    got_b = b.poll(0.0)
    # B 从共享偏移（e1 之后）续读 → 只拿到 e2，不重放 e1
    assert [m.payload["event_id"] for m in got_b] == ["e2"]
    assert b.lease_holder() == "hostB"


def test_file_shared_offset_does_not_persist_partial_line(tmp_path):
    """半行不写进共享偏移：接管者从半行**行首**续读，而非把它当坏行丢掉。"""
    st = InMemoryStateStore()
    clk = _Clock()
    f = tmp_path / "robot.log"
    f.write_text("", encoding="utf-8")

    a = FileTailSource(str(f), tenant_id="t", device_sn="d", shared_offset=True,
                       lease=Lease(st, "lk", "hostA", ttl_s=1, clock=clk))
    a.poll(0.0)
    with open(f, "a", encoding="utf-8") as fh:
        fh.write('{"event_id":"part')      # 半行（无换行）
    assert a.poll(0.0) == []               # 半行不产出，也不提交偏移
    clk.advance(2)                          # 租约过期

    b = FileTailSource(str(f), tenant_id="t", device_sn="d", shared_offset=True,
                       lease=Lease(st, "lk", "hostB", ttl_s=1, clock=clk))
    with open(f, "a", encoding="utf-8") as fh:
        fh.write('ial"}\n')                # 补全
    got = b.poll(0.0)
    # B 从半行行首续读 → 拼出完整的 partial（若偏移错存为半行末尾，这里会丢）
    assert [m.payload["event_id"] for m in got] == ["partial"]


# ---------------------------------------------------------------- MQTT 共享订阅

def test_shared_topic_prefixes_group():
    assert shared_topic("telemetry/#", "data-dog") == "$share/data-dog/telemetry/#"
    assert shared_topic("telemetry/#", "") == "telemetry/#"       # 空组 → 原样
    assert shared_topic("$share/g/t", "other") == "$share/g/t"     # 幂等，不双前缀


def test_mqtt_source_uses_shared_subscription():
    """假 client：断言 subscribe 收到的是共享订阅主题，coordination 为 broker-group。"""
    captured = {}

    class _FakeClient:
        def __init__(self):
            self.on_message = None

        def connect(self, host, port, keepalive=60):
            captured["connect"] = (host, port)

        def subscribe(self, topic, qos=0):
            captured["topic"] = topic

        def loop_start(self):
            pass

        def loop_stop(self):
            pass

        def disconnect(self):
            pass

    src = MqttSource(url="tcp://broker:1883", topic="telemetry/#",
                     tenant_id="t", share_group="grp", client=_FakeClient())
    assert captured["topic"] == "$share/grp/telemetry/#"
    assert src.coordination() == "broker-group"
    src.close()


def test_mqtt_source_without_group_is_none_coordination():
    captured = {}

    class _FakeClient:
        on_message = None

        def connect(self, *a, **k):
            pass

        def subscribe(self, topic, qos=0):
            captured["topic"] = topic

        def loop_start(self):
            pass

        def loop_stop(self):
            pass

        def disconnect(self):
            pass

    src = MqttSource(url="tcp://broker:1883", topic="telemetry/#",
                     tenant_id="t", share_group="", client=_FakeClient())
    assert captured["topic"] == "telemetry/#"
    assert src.coordination() == "none"
    src.close()


# ---------------------------------------------------------------- 工厂装配

def test_factory_file_gets_lease(monkeypatch, tmp_path):
    f = tmp_path / "r.log"
    f.write_text("")
    monkeypatch.setenv("DD_STREAM_SOURCE", "file")
    monkeypatch.setenv("DD_STREAM_FILE_PATH", str(f))
    monkeypatch.setenv("DD_STREAM_SHARE_OFFSET", "1")
    # 用独立状态后端，避免污染进程级单例
    monkeypatch.setenv("DD_STATE_BACKEND", "memory")
    import data_dog.state.factory as sf
    sf.reset_state_store()
    src = build_source()
    assert isinstance(src, FileTailSource)
    assert src.coordination() == "lease"
    sf.reset_state_store()


def test_factory_mqtt_gets_share_group(monkeypatch):
    monkeypatch.setenv("DD_STREAM_SOURCE", "mqtt")
    monkeypatch.setenv("DD_MQTT_SHARE_GROUP", "teamA")
    # 不真连 broker：paho 缺失时工厂会抛 RuntimeError（点明装什么），
    # 装了则连不上会抛——两者都在**构造**阶段，故只断言配置读取正确，
    # 用假 client 直接验（见 test_mqtt_source_uses_shared_subscription）。
    import data_dog.settings as s
    assert s.stream_mqtt_share_group() == "teamA"


def test_factory_kafka_coordination_is_broker_group():
    from data_dog.stream.bus import KafkaSource

    class _FakeConsumer:
        def poll(self, timeout_ms=0):
            return {}

        def close(self):
            pass

    src = KafkaSource(bootstrap="h:9092", topic="t", tenant_id="x",
                      consumer=_FakeConsumer())
    assert src.coordination() == "broker-group"
    src.close()
