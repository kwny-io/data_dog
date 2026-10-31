"""把仿真遥测推进 data_dog 业务服务（`POST /v1/telemetry`）。

和另外两个出口的关系
--------------------
仿真器有三条出口，**各管一件事，不是三选一**：

| 出口 | 去处 | 用于 |
|---|---|---|
| `_PlaneSink`（进程内） | 仿真进程自己的 `/api/candidates` | 仿真控制台上当场看到检测结果 |
| `MqttTelemetryPublisher` | MQTT broker → `stream` 订阅端 | 证明数据真的上了总线 |
| **`HttpTelemetryPublisher`（本模块）** | `POST /v1/telemetry` | **候选落库、进控制台** |

前两条都不落库：进程内数据面的候选活在内存，MQTT 那条要后端另起流式消费者
（`DD_STREAM_ENABLED=1`）才有人收。本模块是**唯一一条让数据进 DB 的路**，
所以它是"链路接通"这四个字真正的落点。

为什么用标准库 `urllib` 而不是 `httpx`
--------------------------------------
`httpx` 只在 `[dev]` extra 里（测试用）。`requests` 根本没装。为了一个演示
出口把 HTTP 客户端塞进主依赖不值得——`urllib.request` 够用，且与
`alerting/notifiers.py` 的做法一致（那边也明确写了"不引入 requests/httpx"）。

线程模型
--------
`publish()` 从仿真线程调用，**绝不能阻塞**：一次同步 POST 若赶上对端慢，
会把 100Hz 的物理循环拖住，整个仿真掉帧。所以：

- 用**有界队列** + 一个后台投递线程。队列满即**丢弃并计数**，不阻塞不无限涨。
- 丢弃是**如实上报**的（`dropped`），不是静默的——页面上能看到"这些帧没发出去"。
  静默丢帧会让人以为链路正常而实际漏数据，比报错更糟。

为什么自己攒批
--------------
`/v1/telemetry` 一次最多 1000 条事件，而仿真每秒产出 50 条（10Hz × 5 部件）。
逐条 POST 等于每秒 50 次 HTTP 往返，纯属浪费。这里按**时间**攒（默认 200ms）
而不是按条数：按条数攒会在低速（暂停仿真）时把最后几条永远卡在缓冲里。
200ms × 50/s = 每批约 10 条，往返开销摊薄到可接受。

身份
----
照抄网关契约：租户与设备号走**请求头**（`X-Tenant-Id` / `X-Device-Sn`），
正文里不带。这与 `emit.py` 的"身份不写进正文"是同一件事的两端。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class HttpStats:
    """投递计数。字段名与页面展示一一对应，不设"看起来对但没人读"的字段。"""
    sent: int = 0            # 成功 POST 的**批**数
    events: int = 0          # 成功送达的**事件**条数
    candidates: int = 0      # 对端回执里报告的候选数（回执没给就保持 0）
    failed: int = 0          # POST 失败的批数（连接失败 / 非 2xx / 超时）
    dropped: int = 0         # 队列满而**未入队**的事件条数
    last_error: str | None = None
    last_ok_at: float | None = None


class HttpTelemetryPublisher:
    """把信封批次 POST 给 data_dog 服务。失败不抛异常——演示不能因为后端没起就停。

    `enabled=False` 时是**空实现**：`publish()` 直接返回，`state()` 里
    `enabled=False` 并给出原因。调用方无需分支。
    """

    def __init__(self, *, base_url: str = "http://127.0.0.1:8000",
                 tenant_id: str = "tenant-demo",
                 device_sn: str = "device-go2-sim-01",
                 api_key: str | None = None,
                 flush_interval_s: float = 0.2,
                 queue_max_events: int = 2000,
                 timeout_s: float = 5.0,
                 enabled: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self.tenant_id = tenant_id
        self.device_sn = device_sn
        self._api_key = api_key
        self.flush_interval_s = float(flush_interval_s)
        self.queue_max_events = int(queue_max_events)
        self.timeout_s = float(timeout_s)
        self._enabled = bool(enabled)
        self._lock = threading.Lock()
        self._pending: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.stats = HttpStats()

        if not self._enabled:
            self.stats.last_error = "已关闭（未配置 --api-url）"
            return
        self._thread = threading.Thread(
            target=self._loop, name="go2sim-http", daemon=True)
        self._thread.start()

    # ---------------------------------------------------------------- 发布
    def publish(self, env: dict[str, Any]) -> bool:
        """入队一条信封。返回是否**入队成功**（不是"对端已收"）。

        队列满 → 丢弃 + 计数。绝不阻塞仿真线程，理由见模块 docstring。
        """
        if not self._enabled:
            return False
        with self._lock:
            if len(self._pending) >= self.queue_max_events:
                self.stats.dropped += 1
                return False
            self._pending.append(env)
            return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            # 用 `wait` 而不是 `sleep`：`close()` 时能立刻醒来，不必等满一个周期。
            self._stop.wait(self.flush_interval_s)
            self.flush()

    def flush(self) -> None:
        """把当前缓冲发出去（一次 POST）。失败时**不重试也不回填缓冲**。

        为什么不重试：这些是遥测，不是需要恰好一次的事务。重试会与下一批
        撞在一起、放大积压，而演示要的是"链路活着"不是"一条不丢"。
        失败如实计 `failed` 并把错误留在 `last_error` 上，页面据此显示断连。
        """
        with self._lock:
            batch = self._pending
            self._pending = []
        if not batch:
            return
        body = json.dumps({"events": batch}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/v1/telemetry", data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Tenant-Id": self.tenant_id,
                "X-Device-Sn": self.device_sn,
                **({"X-API-Key": self._api_key} if self._api_key else {}),
            })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            with self._lock:
                self.stats.failed += 1
                self.stats.last_error = f"HTTP {e.code}: {_first_line(e.read())}"
            return
        except Exception as e:              # noqa: BLE001
            with self._lock:
                self.stats.failed += 1
                self.stats.last_error = f"{type(e).__name__}: {e}"
            return
        with self._lock:
            self.stats.sent += 1
            self.stats.events += len(batch)
            self.stats.candidates += _candidate_count(raw)
            self.stats.last_ok_at = time.time()
            self.stats.last_error = None

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self._enabled,
                "url": f"{self.base_url}/v1/telemetry",
                "tenant_id": self.tenant_id,
                "device_sn": self.device_sn,
                "pending": len(self._pending),
                "sent": self.stats.sent,
                "events": self.stats.events,
                "candidates": self.stats.candidates,
                "failed": self.stats.failed,
                "dropped": self.stats.dropped,
                "connected": self.stats.last_error is None and self.stats.sent > 0,
                "last_ok_at": self.stats.last_ok_at,
                "last_error": self.stats.last_error,
            }

    def close(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        self.flush()          # 收尾把剩的再试一次，避免最后半批丢掉


def _first_line(raw: bytes) -> str:
    """错误正文只取第一行：FastAPI 的 422 详情可能很长，全塞进 last_error 会把页面撑坏。"""
    try:
        text = raw.decode("utf-8", errors="replace").strip()
    except Exception:                       # noqa: BLE001
        return ""
    return text.splitlines()[0][:200] if text else ""


def _candidate_count(raw: bytes) -> int:
    """从回执里取候选条数。取不到就是 0——回执格式的变化不该让投递判失败。"""
    try:
        data = json.loads(raw)
    except Exception:                       # noqa: BLE001
        return 0
    if not isinstance(data, dict):
        return 0
    cands = data.get("candidates")
    return len(cands) if isinstance(cands, list) else 0


__all__ = ["HttpTelemetryPublisher", "HttpStats"]
