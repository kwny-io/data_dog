"""独立流式消费者进程入口：`python -m data_dog.service.stream_worker`。

为什么单独一个进程（而不是绑在 API 上）
--------------------------------------
K8s 里 API（无状态、可任意扩缩）和流式消费（有协调语义）是**两种工作负载**，
扩缩理由不同：API 按 QPS/CPU 扩，消费者按**分区数**（kafka）/ 共享订阅组扩。
把消费者塞进 API 进程会导致"为了扩 API 而无意多开消费者副本"——对 file 源就是
重复消费事故。故拆成独立 deployment（见 docs/p4/04_k8s_helm.md §3.3）。

失败关闭
--------
- `DD_STREAM_ENABLED=0` → 直接报错退出（这个二进制就是为消费而生的，没配源是误用）。
- 源不支持多副本（file/http）而 `DD_STREAM_SHARDS>1` → `build_source()` 抛错，
  进程以非零码退出（K8s 记 CrashLoopBackOff，运维一眼看出是配置错）。
- 算子绑定不可用（缺厂商文档）→ 报错退出，不空转。

优雅停机
--------
SIGTERM（K8s 停止 Pod 时发）→ 置 stop 事件 → `runner.run()` 收尾 flush 后退出，
已拉取但未落库的暂存消息不丢。
"""
from __future__ import annotations

import signal
import sys
import threading

from .. import settings
from ..core.logging import setup_logging


def main() -> int:
    setup_logging(version="stream-worker")

    # 只导入 ServiceState 类（不 import app 模块）——这样不触发 `app = create_app()`
    # 的模块级副作用，消费者进程不白建一个 FastAPI 应用与其进程级 gateway/SaaS 适配器。
    from ..service.app import ServiceState
    from ..stream import StreamRunner, build_source

    state = ServiceState()

    if not settings.stream_enabled():
        print("[stream-worker] DD_STREAM_ENABLED=0：未配置流式源，退出。",
              file=sys.stderr)
        return 2

    # 预检：源装配失败（含 file+多副本误配）在**这里**就报错退出，不进消费循环。
    try:
        source = build_source()
    except RuntimeError as e:
        print(f"[stream-worker] 流式源装配失败：{e}", file=sys.stderr)
        return 2

    plane = state.new_plane()
    if plane is None:
        print(f"[stream-worker] 算子绑定不可用：{state.build_error}", file=sys.stderr)
        return 3

    stop = threading.Event()

    def _on_signal(signum, _frame):                      # pragma: no cover - 信号
        print(f"[stream-worker] 收到信号 {signum}，优雅停机中…", file=sys.stderr)
        stop.set()
        try:
            source.close()                               # 唤醒 poll，加快退出
        except Exception:                                # noqa: BLE001
            pass

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    runner = StreamRunner(plane, source, default_tenant=settings.stream_tenant())
    print(f"[stream-worker] 已启动：source={source.name} "
          f"shards={settings.stream_shards()}", file=sys.stderr)
    runner.run(stop=stop)                                # 阻塞直到停机
    print(f"[stream-worker] 已停止：stats={runner.stats}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
