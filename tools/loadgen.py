#!/usr/bin/env python3
"""P4-8 轻量并发压测：实测热路径客户端侧 P50/P95/P99 + 吞吐。

设计取舍
--------
- **纯标准库**（http.client + threading）：压测工具**不进 src/**、不增生产依赖，
  避免"工具自身引入的依赖"污染被测环境。可选 requests 一律不用。
- **客户端侧计时**：记录**端到端**延迟（含连接复用/网络），再与服务端
  `/metrics` 的 `http_request_duration_seconds` 直方图**对照**——两侧量级一致
  才可信（验收点 2）。只信客户端数字会把网络抖动算进去，只信服务端数字会漏掉
  "请求根本没到达"的情况。
- **尽力逼近目标 RPS**：按 `workers/rps` 间隔发送，不追求精确速率——单机
  loadgen 自身会成为瓶颈（见文档「诚实边界」），数字是**下界近似**。
- **--assert-no-llm**：压测前后对比 `/metrics` 里的调查计数，证明热路径**零 LLM**
  （验收点 5）。热路径若触发调查 = 意外耦合，直接判失败。

用法::

    python tools/loadgen.py --url http://localhost:8000 --devices 120 \
        --rps 500 --duration 30 --tenant bench-t1

    python tools/loadgen.py --url http://localhost:8000 --mode read \
        --rps 200 --duration 20 --tenant bench-t1
"""
from __future__ import annotations

import argparse
import http.client
import json
import sys
import threading
import time
from datetime import datetime, timezone
from itertools import count
from urllib.parse import urlparse

# 与服务端同构的原始日志（见 tests/test_service.py::_raw），保证走完整解析链路
_COMPONENT = "joint_motor.front_left_hip"


def _event(device_sn: str, seq: int, occurred_at: str) -> dict:
    return {
        "event_id": f"{device_sn}-{seq}",
        "occurred_at": occurred_at,
        "received_at": occurred_at,
        "sequence": seq,
        "vendor": "unitree",
        "model": "go2",
        "firmware": "3.2.1",
        "protocol_version": "motor-log-v2",
        "component_hint": _COMPONENT,
        "payload_inline": json.dumps(
            {"temperature": 95.0, "current": 12.0, "mode": 0}),
    }


def _connection(url: str) -> tuple[http.client.HTTPConnection, str]:
    """按 url 建一个长连接（keep-alive），返回 (conn, base_path)。"""
    u = urlparse(url)
    host = u.hostname or "localhost"
    port = u.port or (443 if u.scheme == "https" else 80)
    if u.scheme == "https":
        conn = http.client.HTTPSConnection(host, port, timeout=30)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=30)
    return conn, (u.path.rstrip("/") if u.path else "")


# ---------------------------------------------------------------- /metrics 解析

def _counter_total(body: str, family: str) -> float:
    """把一个 Counter family 的所有 series 值求和（纯文本解析，不依赖 prometheus_client）。"""
    total = 0.0
    for line in body.splitlines():
        if line.startswith("#") or not line:
            continue
        # 精确匹配 `family value` 或 `family{labels} value`，避免前缀误撞
        if line.startswith(family + " ") or line.startswith(family + "{"):
            try:
                total += float(line.rsplit(" ", 1)[1])
            except (ValueError, IndexError):
                continue
    return total


def _hist_count(body: str, family: str) -> float:
    """直方图的 `_count` 之和 = 观测到的请求数。"""
    total = 0.0
    suffix = family + "_count"
    for line in body.splitlines():
        if line.startswith("#") or not line:
            continue
        if line.startswith(suffix + " ") or line.startswith(suffix + "{"):
            try:
                total += float(line.rsplit(" ", 1)[1])
            except (ValueError, IndexError):
                continue
    return total


def _scrape(conn_addr: tuple, base_path: str, tenant: str, api_key: str | None) -> str:
    """抓一次 /metrics 文本。"""
    conn, _ = _connection(conn_addr[0])
    try:
        headers = {"X-Tenant-Id": tenant}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        conn.request("GET", f"{base_path}/metrics", headers=headers)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", "replace")
        return body
    finally:
        conn.close()


# ---------------------------------------------------------------- 延迟统计

def _percentile(sorted_vals: list[float], p: float) -> float:
    """线性插值分位数（与 numpy.percentile 同法，避免依赖 numpy）。"""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


# ---------------------------------------------------------------- worker

class _Worker(threading.Thread):
    def __init__(self, *, url, base_path, mode, tenant, api_key,
                 device_sn, deadline, interval, results, errors, barrier, seq_start):
        super().__init__(daemon=True)
        self.url = url
        self.base_path = base_path
        self.mode = mode
        self.tenant = tenant
        self.api_key = api_key
        self.device_sn = device_sn
        self.deadline = deadline
        self.interval = interval
        self.results = results          # 共享 list（Append 在 CPython 里原子，够用）
        self.errors = errors            # 共享 list，元素为 (status, snippet)
        self.barrier = barrier
        self.seq = count(seq_start)
        self.conn: http.client.HTTPConnection | None = None

    def _headers(self) -> dict:
        h = {"X-Tenant-Id": self.tenant}
        if self.mode == "write":
            h["X-Device-Sn"] = self.device_sn
            # http.client 不会自动加 Content-Type；缺它 FastAPI 解析 JSON 正文会 422
            h["Content-Type"] = "application/json"
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _send(self) -> tuple[float, int]:
        """发一次请求，返回 (延迟秒, 状态码)。连接断了就重连一次。"""
        if self.mode == "write":
            seq = next(self.seq)
            body = json.dumps({"events": [_event(self.device_sn, seq, self._iso(seq))]})
            method, path, payload = "POST", f"{self.base_path}/v1/telemetry", body
        else:
            method, path, payload = "GET", f"{self.base_path}/v1/candidates?limit=50", None

        for attempt in (1, 2):
            try:
                if self.conn is None:
                    self.conn, _ = _connection(self.url)
                t0 = time.perf_counter()
                self.conn.request(method, path, body=payload, headers=self._headers())
                resp = self.conn.getresponse()
                resp.read()
                dt = time.perf_counter() - t0
                return dt, resp.status
            except (http.client.HTTPException, OSError):
                # keep-alive 被服务端关闭 / 网络抖动：重连重试一次
                if self.conn is not None:
                    self.conn.close()
                    self.conn = None
                if attempt == 2:
                    raise
        raise RuntimeError("unreachable")

    @staticmethod
    def _iso(seq: int) -> str:
        """递增一秒的合法 ISO-8601 时间戳（UTC，带 Z）。

        必须走 datetime：此前手工拼 `epoch.000Z` 产出的不是合法 ISO，
        网关解析失败会让**每个**请求 422，压测毫无意义。
        """
        ts = 1_757_760_000 + seq            # 2025-09-13T10:40:00Z 起，逐秒递增
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")

    def run(self) -> None:
        self.barrier.wait()                 # 所有线程齐发，避免前几个线程独占计时
        next_t = time.perf_counter()
        while time.perf_counter() < self.deadline:
            try:
                dt, status = self._send()
                self.results.append(dt)
                if status >= 400:
                    self.errors.append((status, ""))
            except Exception as e:          # noqa: BLE001 —— 压测工具，任何异常都算失败样本
                self.errors.append((-1, f"{type(e).__name__}: {e}"))
            next_t += self.interval
            sleep = next_t - time.perf_counter()
            if sleep > 0:
                time.sleep(sleep)
        if self.conn is not None:
            self.conn.close()


# ---------------------------------------------------------------- 主流程

def _fmt_ms(s: float) -> str:
    return f"{s * 1000:.1f}ms"


def run(args) -> int:
    _, base_path = _connection(args.url)
    workers = args.devices if args.mode == "write" else max(1, min(args.devices, 64))
    interval = workers / args.rps if args.rps > 0 else 0.0

    # ---- 压测前抓指标（为 --assert-no-llm 与两侧对照留基线）----
    pre = None
    if args.assert_no_llm or args.verify_server:
        try:
            pre = _scrape((args.url, None), base_path, args.tenant, args.api_key)
        except Exception as e:              # noqa: BLE001
            print(f"[warn] 压测前抓 /metrics 失败，跳过对照：{e}", file=sys.stderr)

    results: list[float] = []
    errors: list[tuple] = []
    barrier = threading.Barrier(workers)
    t_start = time.perf_counter()
    deadline = t_start + args.duration

    threads = []
    for i in range(workers):
        device_sn = f"bench-dev-{i:04d}"
        threads.append(_Worker(
            url=args.url, base_path=base_path, mode=args.mode, tenant=args.tenant,
            api_key=args.api_key, device_sn=device_sn, deadline=deadline,
            interval=interval, results=results, errors=errors, barrier=barrier,
            seq_start=i * 1_000_000))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - t_start

    # ---- 结果 ----
    ok = len(results)
    results.sort()
    throughput = ok / wall if wall > 0 else 0.0

    print("\n===== data_dog 压测结果 =====")
    print(f"模式       : {args.mode}")
    print(f"目标       : {args.rps} rps × {args.duration}s，{workers} workers")
    print(f"完成       : {ok} 请求 / {wall:.1f}s  →  实测 {throughput:.0f} req/s")
    print(f"错误       : {len(errors)}")
    if results:
        print("延迟(客户端，端到端):")
        print(f"  P50 {_fmt_ms(_percentile(results, 0.50))}   "
              f"P95 {_fmt_ms(_percentile(results, 0.95))}   "
              f"P99 {_fmt_ms(_percentile(results, 0.99))}   "
              f"max {_fmt_ms(results[-1])}")
    if errors:
        from collections import Counter
        kinds = Counter(("status" if s > 0 else "exc", s) for s, _ in errors)
        print(f"错误分布   : {dict(kinds)}")

    rc = 0

    # ---- 服务端对照（验收点 2）----
    post = None
    if pre is not None:
        try:
            post = _scrape((args.url, None), base_path, args.tenant, args.api_key)
        except Exception as e:              # noqa: BLE001
            print(f"[warn] 压测后抓 /metrics 失败：{e}", file=sys.stderr)

    if pre is not None and post is not None and args.verify_server:
        fam = "data_dog_http_request_duration_seconds"
        server_n = _hist_count(post, fam) - _hist_count(pre, fam)
        print(f"服务端直方图观测数: {server_n:.0f}（客户端 {ok}）")
        if ok > 0:
            drift = abs(server_n - ok) / ok
            if drift > 0.05:
                print(f"  [WARN] 两侧偏差 {drift:.1%} > 5%——可能有请求未达服务端，或路径未被计数")
                rc = 1
            else:
                print(f"  [OK] 两侧一致（偏差 {drift:.1%}）")

    # ---- 热路径零 LLM（验收点 5）----
    if pre is not None and post is not None and args.assert_no_llm:
        inv = _counter_total(post, "data_dog_investigations_total") \
            - _counter_total(pre, "data_dog_investigations_total")
        jobs = _counter_total(post, "data_dog_investigation_jobs_total") \
            - _counter_total(pre, "data_dog_investigation_jobs_total")
        print(f"热路径调查计数增量: investigations_total={inv:.0f}, jobs_total={jobs:.0f}")
        if inv != 0 or jobs != 0:
            print("  [FAIL] 热路径触发了调查（可能含 LLM）——不符合「零 LLM」验收")
            rc = 1
        else:
            print("  [OK] 热路径零 LLM（调查计数无增长）")
    elif args.assert_no_llm:
        print("[warn] --assert-no-llm 需要能抓 /metrics；本次跳过", file=sys.stderr)

    return rc


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="data_dog 热路径并发压测（P4-8）")
    p.add_argument("--url", default="http://localhost:8000", help="服务基址")
    p.add_argument("--mode", choices=("write", "read"), default="write",
                   help="write=打 /v1/telemetry；read=打 /v1/candidates")
    p.add_argument("--devices", type=int, default=120, help="模拟设备数（=worker 数，write 模式）")
    p.add_argument("--rps", type=int, default=500, help="目标总吞吐 req/s")
    p.add_argument("--duration", type=float, default=30.0, help="压测时长（秒）")
    p.add_argument("--tenant", default="bench-t1", help="X-Tenant-Id")
    p.add_argument("--api-key", default=None, help="若服务启用鉴权，传 Bearer key")
    p.add_argument("--verify-server", action="store_true",
                   help="与服务端 /metrics 直方图对照（验收点 2）")
    p.add_argument("--assert-no-llm", action="store_true",
                   help="断言热路径未触发调查（验收点 5）")
    args = p.parse_args(argv)

    if args.devices < 1 or args.rps < 1 or args.duration <= 0:
        print("参数非法：devices/rps 须 ≥1，duration 须 >0", file=sys.stderr)
        return 2
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
