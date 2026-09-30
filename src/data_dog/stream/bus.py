"""消息总线源：MQTT 订阅 / Kafka 消费（**可选依赖**，缺依赖时报错清晰）。

为什么放在一起、且是可选依赖
----------------------------
项目准则是"零新依赖跑通离线套件"。MQTT（`paho-mqtt`）与 Kafka（`kafka-python`）
都需要第三方客户端，不能进基座依赖。故：

- 本模块在**实例化时**才 import 对应库；缺失则抛 `RuntimeError`，消息里给出
  确切的安装命令（`pip install '.[mqtt]'`）。绝不在 import 期就让整个包挂掉。
- 二者语义都是"从一条总线收消息"，归到一个文件、共用 `QueueSource` 骨架。

身份从哪来（安全模型）
----------------------
网络总线**不带**应用层身份，故身份只能来自**订阅配置**：这条 topic/队列的数据
属于哪个租户、按什么规则取设备号。
- `tenant_id`：只从配置取。
- `device_sn`：若配置了 `device_from_field` 则从每条消息正文取；否则取配置默认值。
无论哪种，正文里的 tenant_id 一律丢弃（与 HTTP 入口一致）。

水平扩展
--------
- Kafka：配同一个 `group_id`，Kafka 的消费者组协调会让**每个分区**只被组内一个
  副本消费——这正是多副本分工的落点。
- MQTT：配 `share_group`（`DD_MQTT_SHARE_GROUP`），本模块把订阅主题拼成
  `$share/<group>/<topic>`，由 broker 在组内轮询投递（MQTT 5 / EMQX 等支持）。
  不配则每个副本收**全量**、多副本重复消费——见 docs 的诚实边界。
"""
from __future__ import annotations

import json
from typing import Any

from .base import QueueSource, StreamMessage


def _device_of(body: Any, field: str, default: str) -> str:
    if field and isinstance(body, dict):
        return str(body.get(field) or default)
    return default


class MqttSource(QueueSource):
    """订阅 MQTT topic，把每条消息塞进队列。需要 `paho-mqtt`。

    多副本协调：`share_group` 非空时把订阅主题前缀成 `$share/<group>/<topic>`，
    由 broker 在**同组订阅者之间轮询投递**——一条消息只给一个副本，天然负载均衡。
    这是 MQTT 5 / EMQX 等 broker 的共享订阅（shared subscription）语义。
    `share_group=""` 则不前缀（每个副本收全量，仅单副本可接受）。
    """

    name = "mqtt"

    def __init__(self, *, url: str, topic: str, tenant_id: str,
                 device_sn: str = "", device_from_field: str = "",
                 share_group: str = "", qos: int = 1, maxsize: int = 10_000,
                 client: Any = None) -> None:
        super().__init__(maxsize=maxsize)
        self._tenant = tenant_id
        self._device = device_sn
        self._device_field = device_from_field
        self._share_group = share_group.strip()
        if client is None:
            try:
                import paho.mqtt.client as mqtt
            except ImportError as e:                       # pragma: no cover
                raise RuntimeError(
                    "MQTT 源需要 paho-mqtt：pip install 'data-dog[mqtt]' "
                    "（或 pip install paho-mqtt）") from e
            client = mqtt.Client()
        self._client = client
        # 用 urlparse 拆 scheme://host:port，兼容 tcp:// / mqtt://
        host, port = _split_url(url, default_port=1883)
        sub_topic = shared_topic(topic, self._share_group)
        self._sub_topic = sub_topic
        src = self

        def on_message(_c, _u, m):                         # noqa: ANN001
            try:
                body = json.loads(m.payload.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return                                     # 坏消息跳过
            payload = body if isinstance(body, dict) else {"raw": body}
            src.push(StreamMessage(
                payload=payload,
                auth={"tenant_id": src._tenant,
                      "device_sn": _device_of(payload, src._device_field,
                                              src._device),
                      "principal": "mqtt"},
                source=f"mqtt:{m.topic}"))

        self._client.on_message = on_message
        self._client.connect(host, port, keepalive=60)
        self._client.subscribe(sub_topic, qos=qos)
        self._client.loop_start()

    def coordination(self) -> str:
        return "broker-group" if self._share_group else "none"

    def close(self) -> None:
        super().close()
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:                                  # noqa: BLE001
            pass


class KafkaSource(QueueSource):
    """消费 Kafka topic，把每条消息塞进队列。需要 `kafka-python`。

    这里**没有**后台线程：Kafka 的 `consumer.poll` 本就是拉式的，直接在
    `poll()` 里消费，语义更直白，也天然尊重 `timeout`。
    """

    name = "kafka"

    def __init__(self, *, bootstrap: str, topic: str, tenant_id: str,
                 group_id: str = "data-dog", device_sn: str = "",
                 device_from_field: str = "", maxsize: int = 10_000,
                 consumer: Any = None) -> None:
        super().__init__(maxsize=maxsize)
        self._tenant = tenant_id
        self._device = device_sn
        self._device_field = device_from_field
        self._topic = topic
        if consumer is None:
            try:
                from kafka import KafkaConsumer
            except ImportError as e:                           # pragma: no cover
                raise RuntimeError(
                    "Kafka 源需要 kafka-python：pip install 'data-dog[kafka]' "
                    "（或 pip install kafka-python）") from e
            consumer = KafkaConsumer(
                topic, bootstrap_servers=bootstrap.split(","),
                group_id=group_id, auto_offset_reset="latest",
                enable_auto_commit=True,
                value_deserializer=lambda b: b)
        self._consumer = consumer

    def poll(self, timeout: float) -> list[StreamMessage]:
        # kafka 的 poll 自行拉取；这里把 timeout 交给它（毫秒）
        records = self._consumer.poll(timeout_ms=int(max(0.0, timeout) * 1000))
        out: list[StreamMessage] = []
        for _tp, batch in (records or {}).items():
            for rec in batch:
                raw = rec.value
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", errors="replace")
                try:
                    body = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                payload = body if isinstance(body, dict) else {"raw": body}
                out.append(StreamMessage(
                    payload=payload,
                    auth={"tenant_id": self._tenant,
                          "device_sn": _device_of(payload, self._device_field,
                                                  self._device),
                          "principal": "kafka"},
                    source=f"kafka:{_tp.topic}"))
        return out

    def coordination(self) -> str:
        return "broker-group"

    def close(self) -> None:
        super().close()
        try:
            self._consumer.close()
        except Exception:                                      # noqa: BLE001
            pass


def shared_topic(topic: str, share_group: str) -> str:
    """把 topic 拼成 MQTT 共享订阅形式 `$share/<group>/<topic>`。

    `share_group` 为空 → 原样返回（不启用共享订阅）。已带 `$share/` 前缀的
    topic 不再重复加前缀（幂等，避免误配双前缀）。
    """
    g = share_group.strip()
    if not g or topic.startswith("$share/"):
        return topic
    return f"$share/{g}/{topic}"


def _split_url(url: str, *, default_port: int) -> tuple[str, int]:
    """解析 `scheme://host:port`（无 scheme 时按 host:port 处理）。"""
    s = url.split("://", 1)[1] if "://" in url else url
    if ":" in s:
        host, port = s.rsplit(":", 1)
        try:
            return host, int(port)
        except ValueError:
            return host, default_port
    return s, default_port
