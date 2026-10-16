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
  - 两种模式都按「上限 + 1」字节去取 → `len(data) > 上限` 即**确定**被截断；
    本地还能给出完整字节数，http 只能说"完整长度未知"。
 - **本地模式的 TOCTOU（按平台分别处理）**：
   - **POSIX**：逐段 `openat` + `O_NOFOLLOW`（从根 fd 出发，每段都在**已打开**的父目录里开）
     → 路径上任何符号链接都被拒绝，`resolve()` 与 `read` 之间的换链窗口**关掉**。
     代价是**比逃逸检查更严**：指向根目录内的符号链接也拒（证据读取这样更安全）。
     已在 Linux 容器实测：普通/多级路径可读，symlink-final / symlink-escape / symlink-dir 全拒。
   - **Windows**：没有 `O_NOFOLLOW`/`dir_fd`，改用
     `CreateFileW(FILE_FLAG_OPEN_REPARSE_POINT)`——命名对象是**链接本身**（不跟随最后一段），
     再 `GetFinalPathNameByHandleW` 核对**已打开句柄**的真实路径是否等于受控路径。
     窗口只剩两次系统调用之间，而核对对象是已打开的文件对象 → 中间目录被换成 junction/symlink
     只会暴露成不一致（失败关闭）。**残留边界：同卷硬链接**（真实路径相同，本规则不区分，
     也不该区分——那就是同一个文件）。本机实测：普通/多级路径可读，junction 中间段、
     根内 junction 别名、symlink-final 全拒。
- **本模块不做鉴权**：权限来自调用方注入的 `lookup`（按租户查 `raw_log_ref`，配合 RLS）。
  本模块只负责"键 → 正文"。
- `content_sha256` 是**返回内容**（可能已截断）的摘要，不是整份文件的摘要；
  `EvidenceResolver` 的 `expected_digest` 校验的是**整个 payload**——两者互补，不要混用。
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import stat
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


def _open_beneath(root: Path, rel: Path):
    """在受控根目录下打开文件，**尽量关掉 TOCTOU**。

    - **POSIX**：从根 fd 出发逐段 `openat` + `O_NOFOLLOW`（目录段与文件段都不跟随符号链接），
      路径每一段都是在**已打开**的父目录里打开的——中途把某一段换成符号链接不会生效，
      `resolve()` 与 `read` 之间那个窗口因此关掉。
    - **Windows**：没有 `O_NOFOLLOW` / `dir_fd`，退回"`resolve()` 后做包含性检查"。
      仍有 TOCTOU 窗口——**记为边界**（见模块 docstring），不假装平台无关地解决了。

    Returns: (已打开的文件对象 | None, 完整字节数 | None)；None 表示"这条不存在"。
    """
    parts = rel.parts
    # POSIX 专有常量：Windows 上没有，用 getattr 取值（行为不变，同时让 Windows 上的 mypy 说得通）
    o_dir, o_nofollow = getattr(os, "O_DIRECTORY", 0), getattr(os, "O_NOFOLLOW", 0)
    posix_walk = (hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY")
                  and os.open in getattr(os, "supports_dir_fd", set()))
    if posix_walk:
        try:
            dir_fd = os.open(str(root), os.O_RDONLY | o_dir)
        except FileNotFoundError:
            return None, None
        except OSError as e:
            raise RawLogRefused("受控目录不可打开：" + str(e)) from e
        try:
            for part in parts[:-1]:
                try:
                    nxt = os.open(part, os.O_RDONLY | o_dir | o_nofollow,
                                  dir_fd=dir_fd)
                except FileNotFoundError:
                    return None, None
                except OSError as e:
                    raise RawLogRefused("路径段不可用（符号链接/权限）：" + repr(part)) from e
                os.close(dir_fd)
                dir_fd = nxt
            try:
                file_fd = os.open(parts[-1], os.O_RDONLY | o_nofollow, dir_fd=dir_fd)
            except FileNotFoundError:
                return None, None
            except OSError as e:
                raise RawLogRefused("存储键指向符号链接或不可读：" + repr(parts[-1])) from e
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode):      # 目录/设备文件一律不当"日志正文"
                os.close(file_fd)
                return None, None
            return os.fdopen(file_fd, "rb"), info.st_size
        finally:
            os.close(dir_fd)

    return _open_beneath_windows(root, rel)


# ---------------------------------------------------------------------------
# Windows：CreateFileW(FILE_FLAG_OPEN_REPARSE_POINT) + 核对**已打开句柄**的真实路径
# ---------------------------------------------------------------------------
_FILE_ATTRIBUTE_NORMAL = 0x80
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_GENERIC_READ = 0x80000000
_FILE_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004
_OPEN_EXISTING = 3
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_ERROR_ACCESS_DENIED = 5
_INVALID_HANDLE_VALUE = -1
_K32 = None


class _FileTime(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32),
                ("dwHighDateTime", ctypes.c_uint32)]


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [("dwFileAttributes", ctypes.c_uint32),
                ("ftCreationTime", _FileTime),
                ("ftLastAccessTime", _FileTime),
                ("ftLastWriteTime", _FileTime),
                ("dwVolumeSerialNumber", ctypes.c_uint32),
                ("nFileSizeHigh", ctypes.c_uint32),
                ("nFileSizeLow", ctypes.c_uint32),
                ("nNumberOfLinks", ctypes.c_uint32),
                ("nFileIndexHigh", ctypes.c_uint32),
                ("nFileIndexLow", ctypes.c_uint32)]


def _win_kernel32():
    """惰性装好 Win32 原型（只在 Windows 上真正用到）。"""
    global _K32
    if os.name != "nt":
        raise RuntimeError("只有 Windows 才有 Win32 句柄 API")
    if _K32 is None:
        import ctypes.wintypes as wt

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wt.HANDLE
        k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                   wt.DWORD, wt.DWORD, wt.HANDLE]
        k32.GetFinalPathNameByHandleW.restype = wt.DWORD
        k32.GetFinalPathNameByHandleW.argtypes = [wt.HANDLE, wt.LPWSTR, wt.DWORD, wt.DWORD]
        k32.GetFileInformationByHandle.restype = wt.BOOL
        k32.GetFileInformationByHandle.argtypes = [wt.HANDLE, ctypes.c_void_p]
        k32.CloseHandle.restype = wt.BOOL
        k32.CloseHandle.argtypes = [wt.HANDLE]
        _K32 = k32
    return _K32


def _normalize_win_path(p: str) -> str:
    """剥掉 Windows 长路径前缀（问号前缀 / UNC 长路径）后归一化，便于逐字比对。"""
    bs = chr(92)
    unc = bs * 2 + "?" + bs + "UNC" + bs
    dos = bs * 2 + "?" + bs
    if p.startswith(unc):
        p = bs * 2 + p[len(unc):]
    elif p.startswith(dos):
        p = p[len(dos):]
    return os.path.normcase(os.path.normpath(p))


def _final_path(k32, handle) -> str | None:
    """句柄**实际**指向哪里（POST-open 事实，不是"稍后要打开的路径"）。"""
    buf = ctypes.create_unicode_buffer(32768)
    n = k32.GetFinalPathNameByHandleW(handle, buf, len(buf), 0)
    if n == 0 or n >= len(buf):
        return None
    return _normalize_win_path(buf.value)


def _open_beneath_windows(root: Path, rel: Path):
    """Windows 受控打开：**不跟随最后一段 reparse point** + 核对已打开句柄的真实路径。

    与 POSIX 的差别（如实写）：POSIX 逐段 `openat` 把"检查"与"打开"合成同一个系统调用，
    窗口彻底不存在；Windows 没有 `dir_fd`，这里是"一次打开 + 对**已打开句柄**核路径"。
    窗口只剩两次系统调用之间，而核对对象是**已经打开的那个文件对象**：把中间目录换成
    junction/符号链接只会在核对时暴露成不一致 → 失败关闭，不会读到根外内容。

    **残留边界**：同卷**硬链接**与目标真实路径相同，本规则不区分（也不该区分：那就是同一个文件）。
    **副作用（保守）**：根**内部**再套一个 junction 指向根内另一处，也会被拒绝（路径不一致）；
    根自身是符号链接/挂载点没问题——`cfg.base_dir` 在配置加载时就 `resolve()` 过了。
    """
    k32 = _win_kernel32()
    import msvcrt

    full = root / rel
    handle = k32.CreateFileW(
        str(full), _GENERIC_READ, _FILE_SHARE_ALL, None, _OPEN_EXISTING,
        _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_SEQUENTIAL_SCAN,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        err = ctypes.get_last_error()
        if err in (_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND):
            return None, None
        if err == _ERROR_ACCESS_DENIED and full.is_dir():
            return None, None                      # 目录不是"日志正文"（与 POSIX 一致）
        raise RawLogRefused("受控目录内不可打开：" + repr(str(rel))
                            + "（WinError " + str(err) + "）")
    try:
        info = _ByHandleFileInformation()
        if not k32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise RawLogRefused("取句柄属性失败：" + repr(str(rel)))
        if info.dwFileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise RawLogRefused("存储键指向 reparse point（符号链接/junction）："
                                + repr(str(rel)))
        actual = _final_path(k32, handle)
        if actual is None or actual != _normalize_win_path(str(full)):
            raise RawLogRefused("句柄实际指向 " + repr(actual)
                                + "，与受控路径不一致（拒绝读取）")
        size = (int(info.nFileSizeHigh) << 32) | int(info.nFileSizeLow)
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        if fd == -1:                                    # pragma: no cover - 极罕见
            raise RawLogRefused("句柄转 fd 失败：" + repr(str(rel)))
        handle = None                                   # 所有权已交给 fd
        return os.fdopen(fd, "rb"), size
    finally:
        if handle is not None:
            k32.CloseHandle(handle)


def _read_local(cfg: RawLogConfig, key: str) -> tuple[bytes | None, dict[str, Any]]:
    """目录模式：容器化解析 + 读上限+1 字节（用来判定截断）。"""
    if cfg.base_dir is None:                                  # pragma: no cover - 由调用方保证
        raise RawLogRefused("内部错误：目录模式缺 base_dir")
    root = cfg.base_dir.resolve()
    rel = Path(key)
    if rel.is_absolute() or not rel.parts or any(p in ("..", "") for p in rel.parts):
        raise RawLogRefused("非法存储键（拒绝绝对路径与路径穿越）：" + repr(key))
    fh, size = _open_beneath(root, rel)
    if fh is None:
        return None, {}
    with fh:
        data = fh.read(cfg.max_bytes + 1)
    return data, {"source": "file", "bytes": size, "path": str(root / rel)}


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
    """把读到的字节装成可读证据（含截断标注与内容摘要）。

    截断判定统一为 `len(data) > 上限`：读取时都按「上限 + 1」字节去取，所以
    - 超过上限 → 拿到"上限+1"字节 → **确定**截断；
    - 不超过 → 服务端/文件就这么多 → **确定**完整。
    （此前 http 要"保守标注"，是因为传输层硬编码只读 64 KiB、无法区分；
    `data_plane/fetch.py` 已把 `max_bytes` 贯通到传输层，这个不确定性没了。）
    """
    truncated = len(data) > cfg.max_bytes
    body = data[: cfg.max_bytes]
    full = meta.get("bytes")
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
            "正文超过上限 " + str(cfg.max_bytes) + " 字节，已截断（完整长度未知）")
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
        try:
            data, meta = (_read_local(cfg, key) if cfg.base_dir is not None
                          else _read_http(cfg, key))
        except RawLogRefused:
            raise
        except (OSError, ValueError, UnicodeError) as e:
            # **性质测试抓到的真缺口**：含 NUL（和孤立代理项、超长路径）的键在文件系统层抛的是
            # `ValueError: stat: embedded null character in path` 之类——不是我们约定的
            # RawLogRefused。手写用例想得到 `../../`，想不到 `"\x00"`。
            # 统一收敛：调用方只认"键不可用"这一种失败，别让它冒成 500 / execution_failed。
            raise RawLogRefused(
                "存储键无法用于读取（" + type(e).__name__ + ": " + str(e) + "）") from e
        if data is None:
            return False, None
        return True, _payload(authority, key, data, meta, cfg)

    return source
