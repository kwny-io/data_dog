"""Redis 后端：多副本共享状态（限流检查成为热路径瓶颈时的选择）。

原子性实现
----------
用 Redis 原生的 **WATCH/MULTI/EXEC 乐观锁**做 read-modify-write：
WATCH key → 读值 → 在本地算 fn → MULTI 写回 → EXEC。若在我们 WATCH 之后有
别的客户端动了这个 key，EXEC 返回 None（事务被打断）→ 我们**重新读取并重跑
fn**。这正是 `mutate` 契约要求的"冲突则重试 fn"。

为什么乐观锁而非 Lua 脚本：fn 是任意 Python（补令牌、按时间窗合并分组），
无法在 Lua 里执行。乐观重试在按设备/部件的 key 上冲突率低（不同 key 不互相
干扰）；但**同一个 key 被多个写者高并发抢**时（如限流桶），无退避的同步重试会
互相反复打断（活锁）——实测 16 并发即 100% 失败。

两级并发控制（这是最终形态）
--------------------------
1. **同进程按 key 串行**（`_local_lock`）：本进程内多个线程抢同一 key 时，它们
   互相作废 WATCH 属于"自己打自己"。用本地锁排队后，进程内竞争者不再产生任何
   WATCH 冲突（实测 24 线程从"100% 抛错"变成"24/24 全部成功"）。锁表按 FIFO 淘汰，
   淘汰只会让锁退化成"没锁"（正确性仍由 WATCH 兜住），不会出错。
2. **跨进程/跨副本仍靠 WATCH 乐观锁 + 重试**：退避 + 抖动 + 次数/时间双上限
   （见 `_RETRY_*`）。这一层才是为真实多副本准备的，冲突率低但必须失败关闭：
   预算耗尽就抛错，绝不静默丢更新。

为什么限流特别适合 Redis：每条遥测都要一次 check-and-decrement，PG 的每条
一次 DB 往返会成为热路径；Redis 原生 TTL + 内存原子操作是 sub-ms。窗口/候选
分组更新频率低、要耐久，PG 更合适——所以两个后端共用同一接口，按需混用。
"""
from __future__ import annotations

import json
import random
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from .base import StateStore

# 重试预算用**次数 + 时间双上限**（先到为准），而不是固定次数：固定次数在
# "多写者抢同一 key"下没有意义——所有失败者会同步重试、反复互相打断，8 次在
# 16 并发下就被用尽（实测 100% 失败）。改为随机退避错开 + 双上限都耗尽才失败
# 关闭（抛错而非静默丢更新）。
# 为什么还要留次数上限：慢环境（覆盖率插桩、CI 机器、被其它线程挤 GIL）下单次
# 尝试的耗时可放大一个量级，纯墙钟预算会让"只是慢"变成"报错"。次数上限保证
# 这类环境下仍有足够尝试次数，时间上限则把最坏阻塞钉住。
# 注：这一层只面对**跨进程**冲突（进程内已由 `_local_lock` 排队），故预算不必很大。
_RETRY_MAX_ATTEMPTS = 512      # 尝试次数上限
_RETRY_DEADLINE_S = 5.0        # 墙钟上限（正常冲突在 µs 级解决，两者都只在病态争用下才触及）
_RETRY_BASE_SLEEP_S = 0.0005   # 退避基数（小退避 = 更频繁让出 GIL，慢环境下进展更快）
_RETRY_MAX_SLEEP_S = 0.003     # 单次退避上限，保住 Redis 热路径的量级

# 进程内按 key 的锁表：FIFO 淘汰，避免 key 无限增长（淘汰是安全的：锁没了就退化成
# "无本地锁"，WATCH 仍保证正确性，只是多几次重试）。
_LOCAL_LOCKS_GUARD = threading.Lock()
_LOCAL_LOCKS: OrderedDict[str, threading.Lock] = OrderedDict()
_LOCAL_LOCKS_MAX = 4096


def _local_lock(key: str) -> threading.Lock:
    """取该 key 的进程内锁（同进程竞争者在此排队，不再互相作废 WATCH）。"""
    with _LOCAL_LOCKS_GUARD:
        lock = _LOCAL_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCAL_LOCKS[key] = lock
            if len(_LOCAL_LOCKS) > _LOCAL_LOCKS_MAX:
                _LOCAL_LOCKS.popitem(last=False)
        else:
            _LOCAL_LOCKS.move_to_end(key)
        return lock


@lru_cache(maxsize=1)
def _watch_error_class() -> type[BaseException] | None:
    """惰性取到 `redis.exceptions.WatchError`；拿不到（未装 redis）返回 None。

    为什么要惰性：redis 是**可选依赖**（`pip install data-dog[redis]`），没装时本模块
    的 import 仍须成功（其它后端不受影响），所以顶层不能 `import redis`。这里只在第一次
    真的判定异常时才解析，并缓存结果——一个进程内"装没装 redis"不会中途改变。

    为什么还要留类名兜底、兜底何时失效
    ----------------------------------
    类型判定只有拿到**真类**才精确：比类名会把任何恰好叫 `WatchError` 的异常（别的库、
    业务自定义类）都当成"事务被打断"→ 退避重试，从而吞掉真实错误。故正常路径一律
    `isinstance(e, WatchError)`；只有**未装 redis**（本函数返回 None）时才退回类名比对。
    此时的已知失效条件：异常来自非 redis 客户端、且类名恰好是 `WatchError`，仍会被误判为
    冲突（这是旧代码一直存在的行为，不是新增风险）。装了 redis 就走不到兜底分支。
    """
    try:
        from redis.exceptions import WatchError
    except ImportError:            # 可选依赖缺失
        return None
    return WatchError


def _is_watch_error(exc: BaseException) -> bool:
    """该异常是否表示"我们 WATCH 的 key 被别人改了"（→ 可退避重试）。"""
    cls = _watch_error_class()
    if cls is not None:
        return isinstance(exc, cls)
    # 未装 redis：没有真类可比，只能按类名兜底（失效条件见 _watch_error_class）
    return type(exc).__name__ == "WatchError"


def make_client(url: str) -> Any:
    """按统一口径构造 Redis 客户端（生产路径与"真服务契约测试"共用这一处）。

    为什么显式 `protocol=2`（RESP2），而不是用 redis-py 的默认值
    ------------------------------------------------------------
    redis-py 8.x 的默认协议是 **RESP3**，连接时会先发 `HELLO 3`——而 `HELLO` 是
    **Redis 6.0 才有的命令**。于是对着 Redis 5.x（企业里仍在跑的老版本、Windows
    移植版也停在这个版本）连接会直接抛：

        ResponseError: unknown command `HELLO`, with args beginning with: `3`

    这是**装得上、连不上**的静默天花板：`pyproject` 里的 `redis>=5.0` 约束的是
    **客户端**版本，跟**服务端**版本无关，所以依赖解析一切正常、CI（redis:7）也全绿，
    只有到真实老环境才炸。

    本模块只用 `WATCH`/`MULTI`/`EXEC`/`GET`/`SET`/`TTL`/`SCAN`，这些在 RESP2 与
    RESP3 下**语义完全一致**，没有任何 RESP3 专属能力被用到——故固定 RESP2：
    可运行区间从"Redis ≥ 6"放宽到"Redis ≥ 2.0"，代价为零。
    """
    import redis  # 延迟导入：无 redis 依赖时不影响其它后端

    return redis.Redis.from_url(url, decode_responses=True, protocol=2)


class RedisStateStore(StateStore):
    def __init__(self, url: str = "", *, client: Any = None) -> None:
        if client is not None:
            self._client = client          # 测试注入（如 fakeredis）
        else:
            self._client = make_client(url)

    def _load(self, key: str) -> Any:
        raw = self._client.get(key)
        if raw is None:
            return None
        val = json.loads(raw)
        return None if val == {} else val

    def mutate(self, key: str, fn: Callable[[Any], tuple[Any, Any]]) -> Any:
        # 两级并发控制的第一级：同进程按 key 排队（见模块 docstring）。
        # 同进程多线程抢同一 key 时，它们互相作废 WATCH 属于"自己打自己"——实测
        # 24 线程在慢环境下会把重试预算耗光；排队后进程内不再产生 WATCH 冲突。
        with _local_lock(key):
            return self._mutate_optimistic(key, fn)

    def _mutate_optimistic(self, key: str, fn: Callable[[Any], tuple[Any, Any]]) -> Any:
        """第二级：跨进程/跨副本用 WATCH 乐观锁 + 退避重试（预算耗尽则失败关闭）。"""
        client = self._client
        deadline = time.monotonic() + _RETRY_DEADLINE_S
        attempt = 0
        while True:
            attempt += 1
            with client.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    cur = self._load(key)
                    new_value, result = fn(cur)
                    pipe.multi()
                    pipe.set(key, json.dumps(new_value, ensure_ascii=False,
                                             separators=(",", ":")))
                    pipe.execute()
                    return result
                except Exception as e:      # WatchError 或 JSON 异常
                    # 事务被打断（并发写了同一 key）→ 退避后重试；其它异常上抛。
                    # 判定走 `_is_watch_error`（真类 isinstance，未装 redis 才退回类名比对）。
                    if not _is_watch_error(e):
                        raise
                finally:
                    pipe.reset()
            if attempt >= _RETRY_MAX_ATTEMPTS or time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Redis 状态更新冲突：重试 {attempt} 次（上限 "
                    f"{_RETRY_MAX_ATTEMPTS} 次 / {_RETRY_DEADLINE_S}s）仍未成功：{key}")
            # 指数退避 + 抖动：把失败者随机错开，打破"同步重试 → 反复互撞"的活锁
            backoff = min(_RETRY_BASE_SLEEP_S * (2 ** min(attempt, 8)), _RETRY_MAX_SLEEP_S)
            time.sleep(backoff * (0.5 + random.random()))

    def get(self, key: str) -> Any:
        return self._load(key)

    def keys(self, prefix: str = "") -> list[str]:
        # 用 SCAN 游标迭代而非 KEYS：KEYS 会阻塞整个 Redis（大库上是生产事故）。
        pattern = f"{prefix}*" if prefix else "*"
        out: list[str] = []
        cursor = 0
        while True:
            cursor, batch = self._client.scan(cursor=cursor, match=pattern,
                                              count=500)
            out.extend(batch)
            if cursor == 0:
                break
        return out

    def delete(self, key: str) -> None:
        self._client.delete(key)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:                    # noqa: BLE001  某些 client 无 close
            pass
