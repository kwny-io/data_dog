"""写路径「进程内」性能探针：**不压测、不起容器、不连 PG**，一条命令拿到三个关键数字。

为什么需要它（`docs/write_path_batching.md` §9 的教训）
-----------------------------------------------------
⑤ 的根因被推翻过**四次**（连接池 → 提交吞吐 → 往返次数 → 领域代码），每一次都是"推理"
出来的，而每次推理都错。最后把它定下来靠的是**三个进程内量出来的数**：

1. **应用数据面 CPU/请求**（`process_one`，state=memory）——领域逻辑到底贵不贵；
2. **HTTP + 落库 CPU/请求**（TestClient + SQLite）——框架栈 + 持久化贵不贵；
3. **每请求 SQL 语句数**——往返次数。

这三个数**不需要压测**就能拿（因此不需要申请窗口、不受机器噪声影响），却足以证伪
"墙在领域代码里"这类说法。本工具把它们固化下来，让下次不用再从零写探针。

⚠️ 口径警告（本轮踩过两次）
--------------------
1. `cProfile` 会把 CPU 抬高约 **2 倍**。本工具**默认不开**剖析器；要热点排名时加 `--profile`，
   但**不要**拿 `--profile` 的数字与默认数字比。
2. **绝对值不可复现**：同一份代码、同一台机器、不同时段实测，数据面 0.039 → **0.312 ms**（8×）、
   HTTP 项 6.51 → **16.15 ms**（2.5×）；且已排除"两项同进程互相影响"（`--only` 单跑一样）。
   ⇒ **只有"同一时段、同口径、前后各跑一次"的对比才有意义**（本工具正是为这种 A/B 准备的）。
   要对**外**给绝对值，先确保机器空闲，并在同一窗口内跑完全部对比。

用法：
    python tools/perf_probe.py              # 三个数
    python tools/perf_probe.py --profile    # 额外给 tottime 热点排名（CPU 绝对值不可比）
    python tools/perf_probe.py --n 500      # 调样本数
"""
from __future__ import annotations

import argparse
import cProfile
import io
import json
import os
import pstats
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "tools"))


def _prepare_sqlite() -> str:
    """把 DD_DATABASE_URL 指到一个临时 SQLite 文件（**刻意不连 PG**）。

    为什么用 SQLite：它的调用在**本进程内**，没有网络等待 —— 这样 `process_time()`
    量到的就是纯 CPU，不会被"等网络"淹掉（用 PG 剖时 `select.select` 占了 45%，
    真正的 CPU 热点全被盖住）。
    """
    db = os.path.join(tempfile.gettempdir(), "dd_perf_probe.db")
    if os.path.exists(db):
        os.remove(db)
    os.environ["DD_DATABASE_URL"] = f"sqlite:///{db.replace(chr(92), '/')}"
    return db


def probe_app_cpu(n: int) -> tuple[float, float, int]:
    """数据面 CPU/请求：直接调 `process_one`，**绕开 HTTP 与落库**。"""
    from loadgen import _event, _Worker

    from data_dog.service.runtime import ServiceState

    state = ServiceState()
    plane = state.new_plane()
    if plane is None:
        raise SystemExit(f"算子绑定不可用：{state.build_error}")
    auth = {"tenant_id": "perfapp", "device_sn": "bench-dev-0001"}

    def one(i: int):
        return plane.process_one(_event("bench-dev-0001", i, _Worker._iso(i)), auth)

    for i in range(50):
        one(i)
    c0, w0 = time.process_time(), time.perf_counter()
    ok = sum(1 for i in range(n) if one(1000 + i)[0] is not None)
    c1, w1 = time.process_time(), time.perf_counter()
    return 1000 * (c1 - c0) / n, 1000 * (w1 - w0) / n, ok


def probe_http_cpu(n: int, profile: bool) -> tuple[float, float, str]:
    """HTTP+落库 CPU/请求，并顺手数每请求语句数（挂 `Engine` 类，防挂错对象假绿）。"""
    from fastapi.testclient import TestClient
    from loadgen import _event, _Worker
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    from data_dog.service.app import app

    client = TestClient(app)
    hdr = {"X-Tenant-Id": "perfhttp", "X-Device-Sn": "bench-dev-0001",
           "Content-Type": "application/json"}

    def body(i: int) -> str:
        return json.dumps({"events": [_event("bench-dev-0001", i, _Worker._iso(i))]})

    stmts: list[str] = []

    def rec(conn, cursor, statement, params, context, executemany):  # noqa: ANN001
        stmts.append(statement.split()[0].upper() if statement.split() else "?")

    with client:
        for i in range(30):
            client.post("/v1/telemetry", content=body(i), headers=hdr)

        event.listen(Engine, "before_cursor_execute", rec)
        try:
            c0, w0 = time.process_time(), time.perf_counter()
            for i in range(n):
                r = client.post("/v1/telemetry", content=body(500 + i), headers=hdr)
            c1, w1 = time.process_time(), time.perf_counter()
        finally:
            event.remove(Engine, "before_cursor_execute", rec)

        cpu = 1000 * (c1 - c0) / n
        wall = 1000 * (w1 - w0) / n
        per_req = len(stmts) / n

        hot = ""
        if profile:
            pr = cProfile.Profile()
            pr.enable()
            for i in range(n):
                client.post("/v1/telemetry", content=body(9000 + i), headers=hdr)
            pr.disable()
            s = io.StringIO()
            pstats.Stats(pr, stream=s).sort_stats("tottime").print_stats(12)
            hot = s.getvalue()

    if r.status_code != 200:
        raise SystemExit(f"探针请求未成功：HTTP {r.status_code} {r.text[:200]}")
    print(f"  每请求语句数 = {per_req:.1f}（形态：{_top_shapes(stmts)}）")
    return cpu, wall, hot


def _top_shapes(stmts: list[str]) -> str:
    from collections import Counter
    c = Counter(stmts)
    return ", ".join(f"{k}×{v}" for k, v in c.most_common(4))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="写路径进程内性能探针（不压测）")
    p.add_argument("--n", type=int, default=300, help="每项样本数")
    p.add_argument("--only", choices=("app", "http", "both"), default="both",
                   help="只跑其中一项。**量绝对值时必须单跑**：两项同进程会互相影响"
                        "（实测 HTTP 项 6.5ms → 17ms，机制未查明）")
    p.add_argument("--profile", action="store_true",
                   help="额外给热点排名（注意：剖析器会把 CPU 抬高约 2 倍，绝对值不可与默认口径比）")
    args = p.parse_args(argv)

    _prepare_sqlite()
    from data_dog.storage.db import init_db
    init_db(os.environ["DD_DATABASE_URL"])

    print("===== 写路径进程内探针（不压测 / 不起容器 / 不连 PG）=====")
    a_cpu = None
    if args.only in ("app", "both"):
        a_cpu, a_wall, ok = probe_app_cpu(args.n)
        print(f"[1] 应用数据面      CPU/请求 = {a_cpu:.3f} ms   墙钟 = {a_wall:.3f} ms"
              f"   （成功解析 {ok}/{args.n}）")
    if args.only in ("http", "both"):
        h_cpu, h_wall, hot = probe_http_cpu(args.n, args.profile)
        print(f"[2] HTTP+落库       CPU/请求 = {h_cpu:.3f} ms   墙钟 = {h_wall:.3f} ms")
        if a_cpu:
            print(f"    => 框架栈/持久化是数据面的 {h_cpu / max(a_cpu, 1e-9):,.0f} 倍；"
                  f"单核上限约 {1000 / h_cpu:,.0f} req/s")
        if hot:
            print("\n[3] 热点排名（--profile，绝对值不可与上面比）")
            print(hot)
    if args.only == "both":
        print("\n⚠️ 两项跑在同一进程里**会互相影响**：实测 HTTP 项从 ~6.5ms 抬到 ~17ms"
              "（稳定复现，机制未查明）。\n"
              "   引用绝对值请分别单跑（`--only app` / `--only http`）；"
              "只有**同口径的前后对比**才可比。")
    print("提示：端到端 P99 / worker 曲线需要真实压测（`tools/loadgen.py`），"
          "且必须在**没有残留服务**的干净机器上跑——见 docs/write_path_batching.md §7.4。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
