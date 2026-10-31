"""把仿真遥测发布到 MQTT（`paho-mqtt`，可选依赖）。

和 `data_dog.stream.bus.MqttSource` 的关系
-----------------------------------------
那是**订阅**端（收数据进 data_dog），这是**发布**端（仿真器冒充真机发数据）。
两边共用同一套 topic 约定，故仿真数据能直接被现有订阅端接住：

    发布： telemetry/<tenant_id>/<device_sn>
    订阅： telemetry/#                （`DD_STREAM_MQTT_TOPIC` 默认值）

payload 就是**原始日志信封**（`go2_sim.emit.envelope` 的产物），与 HTTP
文件源、网关入口完全同构。所以下游走的仍是 Resolver→Parser→Normalizer→
Detector，不是捷径。

身份与安全
----------
MQTT 报文**不带应用层身份**，所以租户/设备号只能来自订阅侧配置
（`MqttSource` 的 `tenant_id` / `device_sn_from_field`）。仿真器把
`tenant_id`/`device_sn` 写进 topic 只是为了**便于人工排查和按设备分 topic**，
**不是**可信身份来源——这点与 `stream/bus.py` 的模型一致，不能混淆。

为什么用后台线程而不是 paho 的 loop_start
----------------------------------------
paho 的 `loop_start()` 起一个网络线程，它的回调在 paho 线程里跑；
`publish()` 本身是线程安全的队列投递，但 QoS>0 的重传状态机在
`loop_forever/loop_start` 里推进。用 `loop_start()` 最省事，这里就用它；
`publish()` 从仿真线程调用是 paho 明确支持的多线程用法。
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any


@dataclass
class MqttStats:
    published: int = 0
    failed: int = 0
    connected: bool = False
    last_error: str | None = None


class MqttTelemetryPublisher:
    """把 `SimEvent` 发到 MQTT。失败不抛异常——演示不能因为 broker 挂了就停。

    `enabled=False` 或依赖缺失时是**空实现**：调用方无需分支，`stats()`
    里 `connected=False`、`last_error` 给出原因，页面据此如实显示。
    """

    def __init__(self, *, host: str = "127.0.0.1", port: int = 1883,
                 tenant_id: str = "tenant-demo",
                 device_sn: str = "device-go2-sim-01",
                 qos: int = 1, keepalive: int = 30,
                 client_id: str | None = None,
                 enabled: bool = True) -> None:
        self.host = host
        self.port = port
        self.tenant_id = tenant_id
        self.device_sn = device_sn
        self.qos = int(qos)
        self.topic = f"telemetry/{tenant_id}/{device_sn}"
        self._lock = threading.Lock()
        self.stats = MqttStats()
        self._client: Any = None
        self._enabled = bool(enabled)

        if not self._enabled:
            self.stats.last_error = "已关闭（--no-mqtt 或配置）"
            return
        try:
            import paho.mqtt.client as mqtt
        except ModuleNotFoundError:
            self.stats.last_error = (
                "缺少 paho-mqtt：pip install 'data-dog[mqtt]'"
                "（或 pip install paho-mqtt）")
            return

        try:
            # callback_api_version 在 paho 2.x 是**必需参数**；不传会走废弃路径并告警。
            self._client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=client_id or f"go2sim-{device_sn}")
            self._client.on_connect = self._on_connect
            self._client.on_disconnect = self._on_disconnect
            self._client.connect_async(host, port, keepalive)
            self._client.loop_start()
        except Exception as e:              # noqa: BLE001
            self.stats.last_error = f"{type(e).__name__}: {e}"
            self._client = None

    # ---------------------------------------------------------------- 回调
    def _on_connect(self, _client, _userdata, _flags, reason_code, _props=None):
        ok = (getattr(reason_code, "is_failure", False) is False
              and (getattr(reason_code, "value", 1) == 0
                   or str(reason_code) in ("Success", "0")))
        with self._lock:
            self.stats.connected = bool(ok)
            self.stats.last_error = None if ok else f"连接被拒：{reason_code}"

    def _on_disconnect(self, _client, _userdata, _flags=None, reason_code=None,
                       _props=None):
        with self._lock:
            self.stats.connected = False
            if reason_code is not None and str(reason_code) not in ("Success", "0"):
                self.stats.last_error = f"断开：{reason_code}"

    # ---------------------------------------------------------------- 发布
    def publish(self, env: dict[str, Any]) -> bool:
        """发一条信封。返回是否成功入队（不是"broker 已确认"）。"""
        if self._client is None:
            return False
        try:
            payload = json.dumps(env, ensure_ascii=False)
            info = self._client.publish(self.topic, payload, qos=self.qos)
        except Exception as e:              # noqa: BLE001
            with self._lock:
                self.stats.failed += 1
                self.stats.last_error = f"{type(e).__name__}: {e}"
            return False
        rc = getattr(info, "rc", 0)
        with self._lock:
            if rc == 0:
                self.stats.published += 1
            else:
                self.stats.failed += 1
                self.stats.last_error = f"publish rc={rc}"
        return rc == 0

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {"enabled": self._enabled, "topic": self.topic,
                    "host": self.host, "port": self.port, "qos": self.qos,
                    "published": self.stats.published,
                    "failed": self.stats.failed,
                    "connected": self.stats.connected,
                    "last_error": self.stats.last_error}

    def close(self, timeout: float = 2.0) -> None:
        if self._client is None:
            return
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:                   # noqa: BLE001
            pass
        self._client = None


__all__ = ["MqttTelemetryPublisher", "MqttStats"]
