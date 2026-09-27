"""data_dog 在线服务层（落地交付入口）。

把 P2 在线数据面包成一个可长期运行的 HTTP 服务：
    - 存活探针 /healthz、就绪探针 /readyz（供容器编排/负载均衡探测）
    - 指标 /metrics（进程内计数，JSON）
    - 接入 POST /v1/telemetry（原始日志 → 候选异常，落库）
    - 查询 GET /v1/candidates、/v1/alerts、/v1/outbox

设计边界：
    - 身份**只从请求头注入**（X-Tenant-Id / X-Device-Sn），绝不信任请求正文，
      与 P2 网关"正文身份一律不作数"的失败语义保持一致。
    - 无请求时不做任何大模型调用；热路径纯确定性。
"""
from .app import app, create_app

__all__ = ["app", "create_app"]
