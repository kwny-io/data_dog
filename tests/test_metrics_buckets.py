"""可观测性覆盖：延迟直方图的桶必须**覆盖真实长尾**（"声明 == 事实"版，B1）。

背景：`http_request_duration_seconds` 的桶曾顶到 2.5s，而同步 `POST /v1/investigations` 在等
服务端 LLM（10–60s）——那些请求**全部落进 `+Inf`**：图上有 P95/P99，其实不可观测。异步路另有
`investigation_job_duration_seconds`（到 120s），所以缺的只是同步路。

本文件钉两件事：

1. **契约**：两条直方图的顶格有限桶分别 ≥ 60s / ≥ 120s（改小 → 长尾重新不可观测）；
2. **行为**：观测一个 30s 的请求后，它真的落在有限的 `le="30.0"` 桶里，而不是只进 `+Inf`。

只动进程内注册表（`metrics.registry` 是独立注册表），不影响生产。
"""
from __future__ import annotations

from prometheus_client import generate_latest

from data_dog.core import metrics


def _finite_buckets(name: str) -> list[float]:
    """从导出文本里取某直方图的**有限**桶上界（去重升序）。"""
    out: set[float] = set()
    for line in generate_latest(metrics.registry).decode().splitlines():
        if not line.startswith(f"{name}_bucket") or "+Inf" in line:
            continue
        out.add(float(line.split('le="', 1)[1].split('"', 1)[0]))
    return sorted(out)


def _bucket_value(name: str, le: str, **labels: str) -> float:
    """某桶当前计数（找不到 → 0.0）。"""
    needle = ",".join(f'{k}="{v}"' for k, v in labels.items())
    for line in generate_latest(metrics.registry).decode().splitlines():
        if not line.startswith(f"{name}_bucket") or f'le="{le}"' not in line:
            continue
        if needle and needle not in line:
            continue
        return float(line.rsplit(" ", 1)[1])
    return 0.0


def test_http_histogram_covers_the_synchronous_llm_tail():
    """同步调查要等一次真 LLM（10–60s）：桶上限必须够高，否则 P95/P99 全是 +Inf。"""
    metrics.observe_http("POST", "/v1/investigations", 200, 30.0)
    assert _finite_buckets("data_dog_http_request_duration_seconds")[-1] >= 60.0
    assert _bucket_value("data_dog_http_request_duration_seconds", "30.0",
                         path="/v1/investigations") >= 1.0


def test_job_duration_histogram_covers_the_async_tail():
    """异步调查有 60s 硬超时 + 排队：桶要覆盖到 120s。"""
    metrics.investigation_job_duration_seconds.observe(90.0)
    assert _finite_buckets("data_dog_investigation_job_duration_seconds")[-1] >= 120.0


def test_fast_endpoints_keep_fine_grained_buckets():
    """长尾不能靠"整体拉大桶"换来：几十毫秒的端点仍要有细分桶（否则分辨率被牺牲）。"""
    fine = [b for b in _finite_buckets("data_dog_http_request_duration_seconds") if b <= 0.25]
    assert len(fine) >= 5, fine