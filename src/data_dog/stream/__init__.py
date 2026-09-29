"""真实遥测流接入：把 file/http/mqtt/kafka 四种传输统一成"轮询 → 数据面 → 落库"。

对外只暴露少量符号；具体源按需 import（可选依赖缺失不影响本包被导入）。
"""
from __future__ import annotations

from .base import QueueSource, StreamMessage, StreamSource
from .factory import build_source
from .runner import StreamRunner

__all__ = ["QueueSource", "StreamMessage", "StreamSource", "StreamRunner",
           "build_source"]
