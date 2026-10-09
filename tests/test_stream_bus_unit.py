"""消息总线（MQTT / Kafka）的**离线**单测：不连 broker，钉住编解码与身份注入。

为什么这些断言重要
-----------------
网络总线**不带**应用层身份，故"这条消息属于哪个租户/哪台设备"完全由订阅配置 +
本模块的取字段规则决定。写错的现象是"消息进来了、却落到别的租户"，或"一条坏消息
把消费循环带崩"。故这里逐条钉住：

- 身份：tenant 只从配置取（正文里的 tenant_id 一律丢弃）、device 按字段取或回落默认；
- 编解码：坏 JSON / 非 UTF-8 / 非对象正文的处置（跳过 vs 包成 `{"raw": ...}`）；
- 依赖：paho/kafka 缺失时报错必须**点明装什么**（bare ImportError 无法自愈）；
- 关停：client/consumer 的 close 抛错不能把上层带崩（关停路径要稳）。

全离线：假 client/consumer 直接注入；"装了可选依赖"的情形用 `sys.modules` 里的
**桩模块**模拟——不 import 真库、不建任何连接。真缺依赖的情形则用 `find_spec` 判断，
装了真库就 skip（与 tests/test_stream.py 的既有写法一致）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from typing import Any

import pytest

from data_dog.stream.base import StreamMessage
from data_dog.stream.bus import (
    KafkaSource,
    MqttSource,
    _device_of,
    _split_url,
    shared_topic,
)


def _installed(module: str) -> bool:
    """本机是否真的装了该可选依赖（装了就没法断言"缺依赖"路径）。"""
    return importlib.util.find_spec(module) is not None


class _MqttMessage:
    """一条 MQTT 下行消息（回调只用到 payload / topic 两个属性）。"""

    def __init__(self, payload: bytes, topic: str = "telemetry/dev") -> None:
        self.payload = payload
        self.topic = topic


class _FakeClient:
    """假 MQTT client：记录调用，供 on_message 回放；不连网。"""

    def __init__(self) -> None:
        self.on_message: Any = None
        self.calls: list[tuple[Any, ...]] = []
        self.stop_error: Exception | None = None

    def connect(self, host: str, port: int, keepalive: int = 60) -> None:
        self.calls.append(("connect", host, port, keepalive))

    def subscribe(self, topic: str, qos: int = 0) -> None:
        self.calls.append(("subscribe", topic, qos))

    def loop_start(self) -> None:
        self.calls.append(("loop_start",))

    def loop_stop(self) -> None:
        self.calls.append(("loop_stop",))
        if self.stop_error is not None:
            raise self.stop_error

    def disconnect(self) -> None:
        self.calls.append(("disconnect",))


class _Rec:
    """一条 Kafka 记录（只用到 value）。"""

    def __init__(self, value: Any) -> None:
        self.value = value


class _TP:
    """假 TopicPartition（只用到 topic）。"""

    def __init__(self, topic: str) -> None:
        self.topic = topic


class _FakeConsumer:
    """假 Kafka consumer：poll 返回脚本化批次，并记录 timeout_ms。"""

    def __init__(self, records: Any = None) -> None:
        self.records = records
        self.timeouts: list[int] = []
        self.closed = 0
        self.close_error: Exception | None = None

    def poll(self, timeout_ms: int = 0) -> Any:
        self.timeouts.append(timeout_ms)
        return self.records

    def close(self) -> None:
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


def _stub_paho(monkeypatch) -> dict[str, Any]:
    """往 `sys.modules` 塞一个假 `paho.mqtt.client`，模拟"装了可选依赖"。

    为什么不用 importorskip：本机没装 paho，importorskip 只会 skip；而
    `MqttSource.__init__` 里"自己建 client 并连接/订阅"的那几行恰恰只在装了依赖时
    才走。桩模块既能覆盖这段，又不连任何 broker。
    三层模块都塞并互相挂属性：`import paho.mqtt.client as mqtt` 在 CPython 里是
    "IMPORT_NAME + 逐级 getattr"，只塞叶子模块会解析失败。
    """
    created: dict[str, Any] = {}

    class _Client(_FakeClient):
        def __init__(self) -> None:
            super().__init__()
            created["client"] = self

    client_mod = types.ModuleType("paho.mqtt.client")
    client_mod.Client = _Client
    mqtt_mod = types.ModuleType("paho.mqtt")
    mqtt_mod.client = client_mod
    paho_mod = types.ModuleType("paho")
    paho_mod.mqtt = mqtt_mod
    monkeypatch.setitem(sys.modules, "paho", paho_mod)
    monkeypatch.setitem(sys.modules, "paho.mqtt", mqtt_mod)
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", client_mod)
    return created


def _stub_kafka(monkeypatch) -> dict[str, Any]:
    """往 `sys.modules` 塞一个假 `kafka` 模块，记录 KafkaConsumer 的构造入参。"""
    created: dict[str, Any] = {}

    class _KafkaConsumer:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            created["args"] = args
            created["kwargs"] = kwargs

    mod = types.ModuleType("kafka")
    mod.KafkaConsumer = _KafkaConsumer
    monkeypatch.setitem(sys.modules, "kafka", mod)
    return created


# ---------------------------------------------------------------- 纯逻辑

def test_shared_topic_prefixes_and_stays_idempotent():
    """共享订阅主题：组名去空白、空组不前缀、已带 `$share/` 不双前缀。"""
    assert shared_topic("t/#", "grp") == "$share/grp/t/#"
    assert shared_topic("t/#", "  grp  ") == "$share/grp/t/#"        # 误配空格不算组
    assert shared_topic("t/#", "") == "t/#"
    assert shared_topic("t/#", "   ") == "t/#"                       # 全空白 → 未配
    assert shared_topic("$share/g/t/#", "other") == "$share/g/t/#"   # 幂等
    print("PASS test_shared_topic_prefixes_and_stays_idempotent")


def test_device_of_rule_table():
    """设备号取法：配了字段就从正文取，空值/缺字段/正文非对象一律回落配置默认。"""
    assert _device_of({"device_sn": "SN-1"}, "device_sn", "cfg") == "SN-1"
    assert _device_of({"device_sn": None}, "device_sn", "cfg") == "cfg"
    assert _device_of({"other": 1}, "device_sn", "cfg") == "cfg"
    assert _device_of({"n": 42}, "n", "cfg") == "42"                 # 非字符串转 str
    assert _device_of(["not", "dict"], "device_sn", "cfg") == "cfg"  # 正文非对象
    assert _device_of({"device_sn": "x"}, "", "cfg") == "cfg"        # 未配字段
    print("PASS test_device_of_rule_table")


def test_split_url_variants():
    """URL 拆分：带/不带 scheme、缺端口、端口非法都不得抛错（启动要能降级）。"""
    assert _split_url("tcp://broker:1883", default_port=1883) == ("broker", 1883)
    assert _split_url("mqtt://broker", default_port=1883) == ("broker", 1883)
    assert _split_url("broker:1884", default_port=1883) == ("broker", 1884)
    assert _split_url("broker", default_port=1883) == ("broker", 1883)
    assert _split_url("ssl://broker:8883", default_port=1883) == ("broker", 8883)
    assert _split_url("tcp://broker:abc", default_port=1883) == ("broker", 1883)
    assert _split_url("tcp://broker:", default_port=1883) == ("broker", 1883)
    print("PASS test_split_url_variants")


# ---------------------------------------------------------------- 缺依赖路径

def test_mqtt_without_paho_reports_install_command():
    if _installed("paho"):
        pytest.skip("本机装了 paho，无法断言缺依赖路径")
    with pytest.raises(RuntimeError, match="paho-mqtt"):
        MqttSource(url="tcp://broker:1883", topic="t/#", tenant_id="t")
    print("PASS test_mqtt_without_paho_reports_install_command")


def test_kafka_without_client_reports_install_command():
    if _installed("kafka"):
        pytest.skip("本机装了 kafka，无法断言缺依赖路径")
    with pytest.raises(RuntimeError, match="kafka-python"):
        KafkaSource(bootstrap="h:9092", topic="t", tenant_id="t")
    print("PASS test_kafka_without_client_reports_install_command")


# ---------------------------------------------------------------- MQTT 源

def test_mqtt_stub_client_connects_and_subscribes(monkeypatch):
    """未注入 client 时自己建（桩模块）：连接 host/port、订阅共享主题、起回调线程。"""
    created = _stub_paho(monkeypatch)
    src = MqttSource(url="tcp://broker:1883", topic="t/#", tenant_id="t",
                     device_sn="cfg-dev", share_group=" grp ", qos=2, maxsize=7)
    client = created["client"]
    assert client.calls[:3] == [("connect", "broker", 1883, 60),
                                ("subscribe", "$share/grp/t/#", 2),
                                ("loop_start",)]
    assert src.coordination() == "broker-group"
    src.close()
    assert client.calls[-2:] == [("loop_stop",), ("disconnect",)]
    print("PASS test_mqtt_stub_client_connects_and_subscribes")


def test_mqtt_message_uses_config_tenant_and_body_device():
    """正文身份**部分**可信：tenant 只认配置（正文里的 tenant_id 丢弃），device 可取自正文。"""
    client = _FakeClient()
    src = MqttSource(url="tcp://broker:1883", topic="t/#", tenant_id="cfg-tenant",
                     device_sn="cfg-dev", device_from_field="device_sn",
                     client=client)
    body = {"device_sn": "SN-9", "tenant_id": "evil-tenant", "temperature": 95}
    client.on_message(client, None, _MqttMessage(json.dumps(body).encode(), "t/dev"))

    msgs = src.poll(0.0)
    assert len(msgs) == 1
    msg = msgs[0]
    assert msg.auth == {"tenant_id": "cfg-tenant", "device_sn": "SN-9",
                        "principal": "mqtt"}
    assert msg.payload["tenant_id"] == "evil-tenant"       # 正文原样保留，交给网关丢弃
    assert msg.source == "mqtt:t/dev"
    assert isinstance(msg, StreamMessage)
    src.close()
    print("PASS test_mqtt_message_uses_config_tenant_and_body_device")


def test_mqtt_message_device_falls_back_to_config():
    """配了 device_from_field 但正文没有该字段 → 用配置默认值，而不是空设备号。"""
    client = _FakeClient()
    src = MqttSource(url="tcp://broker:1883", topic="t/#", tenant_id="t",
                     device_sn="cfg-dev", device_from_field="device_sn",
                     client=client)
    client.on_message(None, None, _MqttMessage(b'{"temperature": 95}'))
    msgs = src.poll(0.0)
    assert [m.auth["device_sn"] for m in msgs] == ["cfg-dev"]
    print("PASS test_mqtt_message_device_falls_back_to_config")


def test_mqtt_message_bad_payloads_are_skipped_or_wrapped():
    """坏 JSON / 非 UTF-8 整条跳过；合法但非对象的 JSON 包成 `{"raw": ...}`（不丢事件）。"""
    client = _FakeClient()
    src = MqttSource(url="tcp://broker:1883", topic="t/#", tenant_id="t",
                     client=client)
    for payload in (b"not json", b'{"a": 1"', b"\xff\xfe\x00bad-utf8"):
        client.on_message(None, None, _MqttMessage(payload))
    assert src.poll(0.0) == []          # 坏消息不产出，也不炸回调线程

    client.on_message(None, None, _MqttMessage(b"[1, 2]"))
    client.on_message(None, None, _MqttMessage(b"42"))
    assert [m.payload for m in src.poll(0.0)] == [{"raw": [1, 2]}, {"raw": 42}]
    assert src.dropped == 0
    print("PASS test_mqtt_message_bad_payloads_are_skipped_or_wrapped")


def test_mqtt_close_survives_client_errors():
    """关停：正常路径停线程+断开；client 抛错也必须吞掉（不能盖住真正的退出原因）。"""
    client = _FakeClient()
    src = MqttSource(url="tcp://broker:1883", topic="t/#", tenant_id="t",
                     client=client)
    src.push(StreamMessage(payload={"a": 1}, auth=None))
    src.close()
    assert client.calls[-2:] == [("loop_stop",), ("disconnect",)]

    broken = _FakeClient()
    broken.stop_error = RuntimeError("broker 已经走了")
    broken_src = MqttSource(url="tcp://broker:1883", topic="t/#", tenant_id="t",
                            client=broken)
    broken_src.close()                  # 不抛
    print("PASS test_mqtt_close_survives_client_errors")


# ---------------------------------------------------------------- Kafka 源

def test_kafka_stub_consumer_receives_expected_options(monkeypatch):
    """未注入 consumer 时自己建（桩模块）：多 broker 逗号拆分、latest、自动提交。"""
    created = _stub_kafka(monkeypatch)
    src = KafkaSource(bootstrap="h1:9092,h2:9092", topic="telemetry",
                      tenant_id="t", group_id="grp")
    assert created["args"] == ("telemetry",)
    kw = created["kwargs"]
    assert kw["bootstrap_servers"] == ["h1:9092", "h2:9092"]
    assert kw["group_id"] == "grp"
    assert kw["auto_offset_reset"] == "latest"      # 只消费新数据，不重放历史
    assert kw["enable_auto_commit"] is True
    # deserializer 只是原样透传：解码放在本模块 poll 里做，才能逐条跳过坏消息
    assert kw["value_deserializer"](b"x") == b"x"
    assert src.coordination() == "broker-group"
    src.close()                                     # 桩 consumer 无 close → 被吞掉
    print("PASS test_kafka_stub_consumer_receives_expected_options")


def test_kafka_poll_decodes_and_skips_bad_records():
    """poll：秒→毫秒、bytes/bytearray/str 三种值都解、坏 JSON 跳过、非对象包 raw。"""
    consumer = _FakeConsumer({
        _TP("telemetry"): [
            _Rec(b'{"device_sn": "SN-1", "temperature": 95}'),
            _Rec('{"device_sn": "SN-2"}'),                 # str 值（客户端可能已解）
            _Rec(bytearray(b'{"device_sn": "SN-3"}')),
            _Rec(b"[1, 2]"),                               # 合法 JSON 但非对象
            _Rec(b"not json"),                             # 坏 JSON → 跳过
            _Rec(None),                                    # json.loads(None) → 跳过
        ]})
    src = KafkaSource(bootstrap="h:9092", topic="telemetry", tenant_id="cfg",
                      device_sn="fallback", device_from_field="device_sn",
                      consumer=consumer)
    msgs = src.poll(0.25)

    assert consumer.timeouts == [250]                      # 秒 → 毫秒
    assert [m.auth["device_sn"] for m in msgs] == ["SN-1", "SN-2", "SN-3", "fallback"]
    assert {m.auth["tenant_id"] for m in msgs} == {"cfg"}
    assert {m.auth["principal"] for m in msgs} == {"kafka"}
    assert [m.payload for m in msgs][3] == {"raw": [1, 2]}
    assert msgs[0].source == "kafka:telemetry"
    print("PASS test_kafka_poll_decodes_and_skips_bad_records")


def test_kafka_poll_empty_batches_and_timeout_clamp():
    """records 为 None / 空 dict 都得安全返空；负超时夹到 0，不把负数丢给客户端。"""
    none_consumer = _FakeConsumer(None)
    none_src = KafkaSource(bootstrap="h:9092", topic="t", tenant_id="x",
                           consumer=none_consumer)
    assert none_src.poll(-1.0) == []
    assert none_consumer.timeouts == [0]

    empty_consumer = _FakeConsumer({})
    empty_src = KafkaSource(bootstrap="h:9092", topic="t", tenant_id="x",
                            consumer=empty_consumer)
    assert empty_src.poll(0.0) == []
    assert empty_consumer.timeouts == [0]
    print("PASS test_kafka_poll_empty_batches_and_timeout_clamp")


def test_kafka_close_survives_consumer_errors():
    """关停：正常关一次；consumer.close 抛错也吞掉（关停失败不该掩盖退出原因）。"""
    consumer = _FakeConsumer({})
    src = KafkaSource(bootstrap="h:9092", topic="t", tenant_id="x",
                      consumer=consumer)
    src.close()
    assert consumer.closed == 1

    broken = _FakeConsumer({})
    broken.close_error = RuntimeError("broker 没了")
    broken_src = KafkaSource(bootstrap="h:9092", topic="t", tenant_id="x",
                             consumer=broken)
    broken_src.close()                  # 不抛
    print("PASS test_kafka_close_survives_consumer_errors")
