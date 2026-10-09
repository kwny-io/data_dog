"""HTTP 端点按域拆分（原先全部内联在 create_app 里）。"""
from __future__ import annotations

from .changes import router as changes_router
from .feedback import router as feedback_router
from .forecast import router as forecast_router
from .health import router as health_router
from .investigations import router as investigations_router
from .knowledge import router as knowledge_router
from .notify import router as notify_router
from .operators import router as operators_router
from .queries import router as queries_router
from .state import router as state_router
from .telemetry import router as telemetry_router


def all_routers() -> list:
    """按域聚合的路由表；路径互不重叠，故创建顺序不影响匹配。"""
    return [health_router, state_router, telemetry_router, queries_router, investigations_router, feedback_router, knowledge_router, changes_router, operators_router, forecast_router, notify_router]
