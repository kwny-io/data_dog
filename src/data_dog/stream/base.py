"""流式接入源的抽象（把 4 种传输统一成一个轮询接口）。

为什么是 `poll()` 而不是 `__iter__`
-----------------------------------
四种传输里，file/kafka 天然是"拉"（读偏移/消费位点），MQTT/HTTP 天然是"推"
（订阅回调/请求到达）。若接口做成 `__iter__`，推式源要么在迭代器里阻塞、
要么自己开线程——两者都让"谁负责停、谁负责背压"变得含糊。

`poll(timeout) -> list[StreamMessage]` 让**所有源**的行为一致：调用方（`StreamRunner`）
在一个循环里轮询、拿到一批就处理、拿不到就再等。推式源在内部把回调写进一个线程
安全队列，`poll` 负责把队列 drain 出来——对调用方仍是"轮询"，但语义没被扭曲。

身份从哪来（安全模型）
---------------------
`StreamMessage.auth` 是喂给 `Gateway.ingest` 的**可信身份**。与 HTTP 入口一致：
身份由**信道**注入，不读正文。信道是什么由源决定：
- 网络源（http/mqtt/kafka）：身份来自**源配置**（订阅的 topic/队列归属哪个租户），
  正文里的 tenant_id/device_sn 一律丢弃。
- 本地文件源（file）：文件是可信本地输入，默认身份来自配置；可选从每行正文取
  device_sn（见 `FileTailSource` 的文档），但仍不取 tenant_id。

诚实边界：本模块只提供**进程内**轮询循环。多副本分工消费靠各源自己的协调
（见各源的 `coordination()`）：Kafka 用消费者组（broker 协调，已正确）；MQTT 用
共享订阅 `$share/<group>/<topic>`（broker 轮询投递）；file 用应用层**租约选主**
（`lease.py`，同时刻只有一个副本读该文件）。file 源本质仍是**单消费者**——租约只是
"选一个"，要真正并行须转 Kafka/MQTT。
"""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StreamMessage:
    """一条待摄入的消息：原始正文 + 该信道的可信身份。"""
    payload: dict[str, Any]
    auth: dict[str, Any] | None
    source: str = ""
    # 消息进入消费循环的时刻（`time.perf_counter()`），用于测消费延迟
    # （收到 → 落库完成）。默认 0 表示"未打点"，此时不计消费延迟。
    #
    # **必须是 perf_counter，不能用 monotonic**：Windows 上 monotonic 的分辨率
    # 只有 16ms，而消费延迟通常就在毫秒级——量出来不是 0 就是 16，指标直接失真。
    enqueued_at: float = 0.0


class StreamSource(ABC):
    """流式源基类。实现 `poll`；推式源可复用 `QueueSource`。"""

    name: str = "?"

    @abstractmethod
    def poll(self, timeout: float) -> list[StreamMessage]:
        """取一批消息（至多一次 poll 的量）；`timeout` 秒内无新消息则返回空列表。"""

    def coordination(self) -> str:
        """多副本协调模式：`'broker-group' | 'lease' | 'none'`。

        供 `/readyz` 与运维一眼看出"这个源靠什么保证多副本不重复消费"。
        默认 `'none'`（无协调：多副本会重复消费，见本模块头部边界说明）。
        """
        return "none"

    def close(self) -> None:                       # noqa: B027 - 有意可选，非抽象
        pass


class QueueSource(StreamSource):
    """推式源（MQTT/HTTP）的公共骨架：后台把消息塞进队列，`poll` 排空队列。

    子类/调用方在收到推送时调 `push()`（线程安全）。`poll` 在队列空时最多等
    `timeout` 秒——用一个条件变量，而非忙等，避免空转烧 CPU。
    """

    def __init__(self, *, maxsize: int = 10_000) -> None:
        self._q: deque[StreamMessage] = deque()
        self._cv = threading.Condition()
        self._maxsize = maxsize
        self._closed = False
        self.dropped = 0                          # 队列满时丢弃计数（背压证据）

    def push(self, msg: StreamMessage) -> None:
        with self._cv:
            if len(self._q) >= self._maxsize:
                # 背压：满则丢**新**消息并计数（不阻塞推送方——阻塞会拖垮
                # MQTT/HTTP 回调线程）。丢弃是有代价的，故计数暴露到指标。
                self.dropped += 1
                return
            self._q.append(msg)
            self._cv.notify()

    def poll(self, timeout: float) -> list[StreamMessage]:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cv:
            if not self._q and not self._closed:
                self._cv.wait(timeout=max(0.0, deadline - time.monotonic()))
            out: list[StreamMessage] = []
            while self._q:
                out.append(self._q.popleft())
            return out

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()
