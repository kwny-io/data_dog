"""流式源的配置装配：按 `DD_STREAM_SOURCE` 造出具体源。

单一入口 `build_source()` 让服务/CLI 只需一句"按配置起源"，不必到处 if/elif。
新增传输时只改这一个函数 + `settings` 的读取点。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .. import settings
from .base import StreamSource

if TYPE_CHECKING:
    pass


def build_source(kind: str | None = None) -> StreamSource:
    """按配置构造流式源。`kind` 缺省取 `DD_STREAM_SOURCE`。"""
    kind = (kind or settings.stream_source()).lower()
    tenant = settings.stream_tenant()
    device = settings.stream_device_sn()
    dev_field = settings.stream_device_from_field()

    if kind == "file":
        from .file import FileTailSource, resolve_watch_path
        path = settings.stream_file_path()
        if not path:
            raise RuntimeError("file 源需要 DD_STREAM_FILE_PATH")
        resolved = resolve_watch_path(path)
        if resolved is None:
            raise RuntimeError(f"file 源路径无匹配文件：{path}")
        return FileTailSource(resolved, tenant_id=tenant, device_sn=device,
                              device_from_field=dev_field)

    if kind == "http":
        from .http import HttpPushSource
        return HttpPushSource(
            tenant_id=tenant, host=settings.stream_http_host(),
            port=settings.stream_http_port(), path=settings.stream_http_path(),
            device_from_field=dev_field)

    if kind == "mqtt":
        from .bus import MqttSource
        return MqttSource(url=settings.stream_mqtt_url(),
                          topic=settings.stream_mqtt_topic(), tenant_id=tenant,
                          device_sn=device, device_from_field=dev_field)

    if kind == "kafka":
        from .bus import KafkaSource
        return KafkaSource(bootstrap=settings.stream_kafka_bootstrap(),
                           topic=settings.stream_kafka_topic(), tenant_id=tenant,
                           group_id=settings.stream_kafka_group(),
                           device_sn=device, device_from_field=dev_field)

    raise RuntimeError(
        f"未知流式源类型：{kind!r}（支持 file/http/mqtt/kafka，见 DD_STREAM_SOURCE）")
