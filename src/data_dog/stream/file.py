"""文件/日志尾随源（`tail -f` 语义）——机器人本机日志最真实的接入方式。

为什么用标准库手写而非 watchdog/inotify
---------------------------------------
- 零新依赖（项目准则）。文件尾随只需"按偏移读新增字节"，标准库足够。
- 跨平台：`watchdog` 在 Windows 上靠轮询、在 Linux 上靠 inotify，行为不一致；
  本项目大量在 Windows 上开发，统一用**按 poll 间隔检查大小**反而更可预测。

轮转 / 截断处理
--------------
每次 poll 读文件大小：
- 变小 → 文件被截断/轮转（`logrotate` 常见）→ 从头重读。
- 变大 → 从上次偏移续读。
只处理**完整的行**（以 `\n` 结尾）；读到半行时把偏移退回到行首，等下次补全，
避免把一条被切成两半的 JSON 当成坏数据丢弃。

多副本（G5）
-----------
单文件无 broker 协调 → 多副本各读各偏移会**重复消费**。故本源可选**租约选主**：
传入 `lease` 后，`poll` **只在本副本持有租约时读文件**，否则返回空（空转等待）。
持有者把当前偏移周期性写入共享状态，接管者从**上次共享偏移**续读（`shared_offset`），
从而不丢不重（末尾少量重放由候选幂等 upsert 兜底）。**file 源本质仍是单消费者**
——租约只是"选一个"，要真正并行须转 Kafka/MQTT。

身份
----
- `tenant_id` 只从**配置**取（可信信道身份），绝不读正文。
- `device_sn` 默认也取配置；若配置了 `device_from_field`，则从每行正文的该字段
  取设备序列号（同一文件可能承载多台设备的日志），取不到则退回配置默认值。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .base import StreamMessage, StreamSource

if TYPE_CHECKING:
    from .lease import Lease


class FileTailSource(StreamSource):
    """尾随一个文本文件（每行一条 JSON 原始日志）。"""

    name = "file"

    def __init__(self, path: str, *, tenant_id: str, device_sn: str = "",
                 device_from_field: str = "", encoding: str = "utf-8",
                 start_at_end: bool = True, lease: Lease | None = None,
                 shared_offset: bool = False) -> None:
        self._path = Path(path)
        self._tenant = tenant_id
        self._device = device_sn
        self._device_field = device_from_field
        self._encoding = encoding
        self._off = 0
        self._buf = b""
        self._start_at_end = start_at_end
        self._opened_once = False
        self._lease = lease
        self._shared_offset = shared_offset
        self._offset_key = f"stream:offset:{self._path}" if shared_offset else ""
        # 从共享偏移初始化（若有）——接管者续读而非从尾重来
        self._off_loaded = False

    def coordination(self) -> str:
        return "lease" if self._lease is not None else "none"

    def lease_holder(self) -> str:
        """当前租约持有者（无租约返回空串），供 /readyz 展示。"""
        return self._lease.holder() if self._lease is not None else ""

    def _load_offset(self) -> None:
        """从共享状态恢复偏移（仅在启用共享偏移且租约存在时）。"""
        self._off_loaded = True
        if not self._offset_key or self._lease is None:
            return
        rec = self._lease.state.get(self._offset_key)
        if isinstance(rec, dict) and "off" in rec:
            self._off = int(rec.get("off") or 0)
            self._opened_once = True                   # 已有偏移 → 不再是"首次"

    def _save_offset(self) -> None:
        if not self._offset_key or self._lease is None:
            return
        # 存"已完整消费"的位置：退回缓冲区内尚未见换行的半行字节，让接管者
        # 从该半行的**行首**续读（否则接管会以半行为起点读出一整条坏行）。
        commit = max(0, self._off - len(self._buf))
        rec = {"off": int(commit)}
        self._lease.state.mutate(self._offset_key, lambda _cur: (rec, None))

    def _size(self) -> int | None:
        try:
            return self._path.stat().st_size
        except OSError:
            return None

    def poll(self, timeout: float) -> list[StreamMessage]:
        # 多副本选主：非持有者不读文件（空转等待）。持有者每轮续约；未持有则
        # 尝试抢占（空闲/已过期租约 → 接管），确保新副本能顶上、崩溃者的租约
        # 自然过期后旁人可接管。
        if self._lease is not None:
            if not (self._lease.renew() or self._lease.acquire()):
                return []
            if not self._off_loaded:
                self._load_offset()

        size = self._size()
        if size is None:
            # 文件还没出现（或刚被删）——不是错误，等下一次 poll
            return []
        if not self._opened_once:
            self._opened_once = True
            if self._start_at_end:
                self._off = size                       # 只跟新增内容（tail -f）
        if size < self._off:
            self._off = 0                              # 被截断/轮转 → 从头
            self._buf = b""

        out: list[StreamMessage] = []
        if size > self._off:
            try:
                with open(self._path, "rb") as f:
                    f.seek(self._off)
                    chunk = f.read(size - self._off)
                    self._off = f.tell()
            except OSError:
                return []
            data = self._buf + chunk
            lines = data.split(b"\n")
            # 最后一个元素可能是不完整的半行 → 留到下次
            self._buf = lines.pop()
            for raw_line in lines:
                msg = self._parse(raw_line)
                if msg is not None:
                    out.append(msg)
            self._save_offset()
        return out

    def _parse(self, raw: bytes) -> StreamMessage | None:
        line = raw.strip()
        if not line:
            return None
        try:
            body = json.loads(line.decode(self._encoding))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None                                # 坏行跳过（不中断整条流）
        if not isinstance(body, dict):
            return None
        device = self._device
        if self._device_field:
            device = str(body.get(self._device_field) or self._device)
        auth: dict[str, Any] = {"tenant_id": self._tenant, "device_sn": device,
                                "principal": "file-tail"}
        return StreamMessage(payload=body, auth=auth, source=f"file:{self._path.name}")


def resolve_watch_path(path: str, *, pattern: str = "*.log") -> str | None:
    """把配置路径解析成具体文件：是目录则取其中**最新修改**的匹配文件。

    机器人常把日志按 `robot-20260929.log` 轮转，配置写目录比写死文件名稳。
    """
    p = Path(path)
    if p.is_file():
        return str(p)
    if p.is_dir():
        cands = sorted(p.glob(pattern), key=lambda f: f.stat().st_mtime,
                       reverse=True)
        return str(cands[0]) if cands else None
    return None


__all__ = ["FileTailSource", "resolve_watch_path"]
