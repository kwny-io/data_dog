"""真实遥测流接入：把 file/http/mqtt/kafka 四种传输统一成"轮询 → 数据面 → 落库"。

对外只暴露少量符号；具体源按需 import（可选依赖缺失不影响本包被导入）。
"""
from __future__ import annotations

# Lease 已移到 `state/`（它是 StateStore 之上的通用原语，告警 sweeper 也用）；这里继续
# 导出，保持 `from data_dog.stream import Lease` 可用。
from ..state.lease import Lease, default_owner
from .base import QueueSource, StreamMessage, StreamSource
from .factory import build_router, build_source
from .runner import StreamRunner
from .sharding import ShardRouter, pick_owner, pick_shard, shard_key

__all__ = ["Lease", "QueueSource", "ShardRouter", "StreamMessage", "StreamSource",
           "StreamRunner", "build_router", "build_source", "default_owner",
           "pick_owner", "pick_shard", "shard_key"]
