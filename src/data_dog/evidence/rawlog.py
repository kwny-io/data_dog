"""原始日志正文的**真实读取**（`rawlog://<event_id>`）。

## 为什么要这个模块

`evidence/refs.py` 补的是"缺口 E-8"（ref → 可读证据），但 `rawlog://` 的默认源此前是
**mock**：只认几个写死的 authority，返回
`{"digest": "sha256:mock", "note": "原始日志正文外置（mock 摘要）"}`。
于是审计方拿到 `rawlog://evt-123`，解析成功、权限校验通过，**内容却是假的**。

真实引用其实一直在库里：`data_plane/pipeline.py` 把正文外置引用写进
`telemetry_events.raw_log_ref`；真取数原语 `data_plane/fetch.py` 也早就有，
只是从没接到 `rawlog` 上（当时只用在上报告警 webhook 与 CMDB）。

## 做什么

`make_rawlog_source()` 造一个真实数据源，两种存放方式**二选一**：

- `DD_RAWLOG_BASE_DIR`：本地/挂载目录。存储键在**受控根目录内**解析——拒绝绝对路径与
  `..`，且 `resolve()` 之后必须仍在根内（挡住符号链接逃逸）。
- `DD_RAWLOG_BASE_URL`：http(s) 对象存储。`BASE_URL + "/" + quote(key)` 走
  `data_plane.fetch.safe_request`（逐跳复验重定向 + 连接钉在已校验 IP 上），
  默认**不允许私网**（要指内网对象存储得显式开 `DD_RAWLOG_ALLOW_PRIVATE=1`）。

**都没配 = 失败关闭**：抛 `RawLogNotConfigured`，解析器收敛成 `execution_failed`。
绝不回退 mock、绝不猜路径——"没配"必须看起来就像"没配"。

## 边界（如实写）

- **大小上限** `DD_RAWLOG_MAX_BYTES`（默认 64 KiB）：
  - 本地：多读 1 字节判断是否超限 → `truncated` 是**确定**的，并给出完整字节数；
  - http：取数层自己也是"读满即止"，无法区分"恰好等于上限"与"被截断"，故到达上限即按
    `truncated=True` **保守标注**（宁可说可能被截，不假装完整）。
- **本地模式的 TOCTOU**：`resolve()` 与 `read` 之间有窗口，同卷上有人能换链就能换内容。
  本读器假定受控目录**不可被非受信方写入**；要彻底关掉需 `O_NOFOLLOW`+`openat`
  （POSIX 专用，Windows 上不可移植）——记为边界，不假装解决了。
- **本模块不做鉴权**：权限来自调用方注入的 `lookup`（按租户查 `raw_log_ref`，配合 RLS）。
  本模块只负责"键 → 正文"。
- `content_sha256` 是**返回内容**（可能已截断）的摘要，不是整份文件的摘要；
  `EvidenceResolver` 的 `expected_digest` 校验的是**整个 payload**——两者互补，不要混用。
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

DEFAULT_MAX_BYTES = 64 * 1024
DEFAULT_TIMEOUT_S = 5.0

# 与 `evidence/refs.py` 的 SourceFn 同形：(authority, path) -> (found, payload)
Lookup = Callable[[str], "str | None"]


class RawLogNotConfigured(RuntimeError):
    """两种存放方式都没配——**失败关闭**（这**不是**"没有这条日志"）。"""


class RawLogRefused(RuntimeError):
    """存储键非法 / 取数失败 / 越界——**拒绝并说明原因**，不静默返回空内容。"""


@dataclass(frozen=True)
class RawLogConfig:
    """一次读取所需的全部配置（由 `load_config()` 从 settings 组装）。"""

    base_dir: Path | None
    base_url: str
    max_bytes: int
    timeout_s: float
    allow_private: bool


def load_config() -> RawLogConfig:
    """从 settings 读配置（**惰性**：调用/解析那一刻才读，便于测试与热改）。"""
    from .. import settings

    base_dir = (settings.rawlog_base_dir() or "").strip()
    base_url = (settings.rawlog_base_url() or "").strip()
    if base_dir and base_url:
        raise RawLogRefused(
            "DD_RAWLOG_BASE_DIR 与 DD_RAWLOG_BASE_URL **互斥**：只能配一个（配哪个都行）")
    return RawLogConfig(
        base_dir=Path(base_dir).expanduser() if base_dir else None,
        base_url=base_url.rstrip("/"),
        max_bytes=max(1, int(settings.rawlog_max_bytes())),
        timeout_s=max(0.1, float(settings.rawlog_timeout_s())),
        allow_private=bool(settings.rawlog_allow_private()),
    )


def _read_local(cfg: RawLogConfig, key: str) -> tuple[bytes | None, dict[str, Any]]:
    """目录模式：容器化解析 + 读上限+1 字节（用来判定截断）。"""
    if cfg.base_dir is None:                                  # pragma: no cover - 由调用方保证
        raise RawLogRefused("内部错误：目录模式缺 base_dir")
    root = cfg.base_dir.resolve()
    rel = Path(key)
    if rel.is_absolute() or not rel.parts or any(p in ("..", "") for p in rel.parts):
        raise RawLogRefused("非法存储键（拒绝绝对路径与路径穿越）：" + repr(key))
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise RawLogRefused("存储键逃出受控目录：" + repr(key))
    if not target.is_file():
        return None, {}
    size = target.stat().st_size
    with target.open("rb") as fh:
        data = fh.read(cfg.max_bytes + 1)
    return data, {"source": "file", "bytes": size, "path": str(target)}


def _read_http(cfg: RawLogConfig, key: str) -> tuple[bytes | None, dict[str, Any]]:
    """对象存储模式：走 `safe_request`（SSRF 防护在那一层）。"""
    from ..data_plane.fetch import safe_request

    url = cfg.base_url + "/" + quote(key, safe="/")
    res = safe_request(url, method="GET", timeout_s=cfg.timeout_s,
                       max_bytes=cfg.max_bytes + 1, allow_private=cfg.allow_private)
    if not res.ok:
        if res.status == 404:
            return None, {}
        raise RawLogRefused("取数失败（" + (res.error or ("HTTP " + str(res.status))) + "）")
    return res.body, {"source": "http", "url": res.final_url or url,
                      "hops": list(res.hops)}


def _payload(event_id: str, key: str, data: bytes, meta: dict[str, Any],
             cfg: RawLogConfig) -> dict[str, Any]:
    """把读到的字节装成可读证据（含截断标注与内容摘要）。"""
    truncated = len(data) > cfg.max_bytes
    body = data[: cfg.max_bytes]
    full = meta.get("bytes")
    if full is None and len(body) >= cfg.max_bytes:
        # http：取数层读满即止 → 无法区分"恰好等于上限"与"被截"，保守按可能截断处理
        truncated = True
    out: dict[str, Any] = {
        "event_id": event_id,
        "key": key,
        "source": meta.get("source", "?"),
        "content": body.decode("utf-8", errors="replace"),
        "content_sha256": hashlib.sha256(body).hexdigest(),
        "bytes": int(full) if full is not None else len(body),
        "truncated": bool(truncated),
    }
    if truncated:
        out["note"] = ("正文超过上限 " + str(cfg.max_bytes) + " 字节，已截断（完整 "
                       + str(full) + " 字节）") if full is not None else (
            "正文到达读取上限 " + str(cfg.max_bytes) + " 字节，可能被截断")
    if meta.get("url"):
        out["url"] = meta["url"]
    return out


def make_rawlog_source(lookup: Lookup | None = None, *,
                       config: RawLogConfig | None = None) -> Callable[[str, list[str]], Any]:
    """造 `rawlog` 数据源。

    Args:
        lookup: `authority -> 存储键 | None`。服务层应传入"按租户查
            `telemetry_events.raw_log_ref`"的查询（查不到返回 None → `not_found`）；
            缺省时把 authority 本身当存储键（容器化校验仍然生效）。
        config: 注入配置（测试用）；缺省每次解析时从 settings 现读。

    Returns:
        与 `evidence/refs.py` 的 `SourceFn` 同形的回调。
    """

    def source(authority: str, path: list[str]) -> tuple[bool, Any]:
        if path:
            raise RawLogRefused("rawlog 引用不支持子路径：" + authority + "/" + "/".join(path))
        cfg = config if config is not None else load_config()
        if cfg.base_dir is None and not cfg.base_url:
            # 配置检查放在查库之前：这是"环境没配好"，不是"这条日志不存在"
            raise RawLogNotConfigured(
                "rawlog 正文读取未配置：设 DD_RAWLOG_BASE_DIR（目录）或 "
                "DD_RAWLOG_BASE_URL（对象存储前缀）")
        key = authority if lookup is None else lookup(authority)
        if key is None or not str(key).strip():
            return False, None
        key = str(key).strip()
        data, meta = (_read_local(cfg, key) if cfg.base_dir is not None
                      else _read_http(cfg, key))
        if data is None:
            return False, None
        return True, _payload(authority, key, data, meta, cfg)

    return source
