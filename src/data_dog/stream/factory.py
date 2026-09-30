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


# 能安全多副本消费的源：kafka 靠消费者组、mqtt 靠共享订阅做跨副本协调。
# file（单文件）/http（push 接收）无协调 → 只能单副本。
_MULTI_REPLICA_SAFE = frozenset({"kafka", "mqtt"})


def build_source(kind: str | None = None) -> StreamSource:
    """按配置构造流式源。`kind` 缺省取 `DD_STREAM_SOURCE`。

    多副本安全自检（失败关闭，见 docs/p4/04_k8s_helm.md §3.3）：file 源**只能单副本**
    （单文件无协调，多副本会各尾随一份 → 事件重复处理），配了 `DD_STREAM_SHARDS>1`
    即**拒绝启动**并说明原因——宁可起不来，不要静默重复消费。
    """
    kind = (kind or settings.stream_source()).lower()
    tenant = settings.stream_tenant()
    device = settings.stream_device_sn()
    dev_field = settings.stream_device_from_field()

    shards = settings.stream_shards()
    if shards > 1 and kind not in _MULTI_REPLICA_SAFE:
        raise RuntimeError(
            f"流式源 {kind!r} 不支持多副本（DD_STREAM_SHARDS={shards}）："
            f"只有 {sorted(_MULTI_REPLICA_SAFE)} 能安全多副本消费——"
            "kafka 靠消费者组、mqtt 靠共享订阅；file/http 无跨副本协调，"
            "多副本会重复消费。请把 DD_STREAM_SHARDS 设为 1，或改用 kafka/mqtt。")

    if kind == "file":
        from .file import FileTailSource, resolve_watch_path
        path = settings.stream_file_path()
        if not path:
            raise RuntimeError("file 源需要 DD_STREAM_FILE_PATH")
        resolved = resolve_watch_path(path)
        if resolved is None:
            raise RuntimeError(f"file 源路径无匹配文件：{path}")
        lease = _file_lease(path) if settings.stream_share_offset() else None
        return FileTailSource(resolved, tenant_id=tenant, device_sn=device,
                              device_from_field=dev_field, lease=lease,
                              shared_offset=settings.stream_share_offset())

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
                          device_sn=device, device_from_field=dev_field,
                          share_group=settings.stream_mqtt_share_group())

    if kind == "kafka":
        from .bus import KafkaSource
        return KafkaSource(bootstrap=settings.stream_kafka_bootstrap(),
                           topic=settings.stream_kafka_topic(), tenant_id=tenant,
                           group_id=settings.stream_kafka_group(),
                           device_sn=device, device_from_field=dev_field)

    raise RuntimeError(
        f"未知流式源类型：{kind!r}（支持 file/http/mqtt/kafka，见 DD_STREAM_SOURCE）")


def _file_lease(path: str):
    """构造 file 源的选主租约（基于共享状态后端）。"""
    from ..state import get_state_store
    from .lease import Lease, default_owner
    return Lease(get_state_store(), f"stream:lease:file:{path}",
                 default_owner(), ttl_s=settings.stream_file_lease_ttl_s())
