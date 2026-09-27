"""`python -m data_dog.service` 启动器。

读 DD_SERVICE_HOST / DD_SERVICE_PORT / DD_SERVICE_NAME，交给 uvicorn 起服务。
"""
from __future__ import annotations

import sys

from .. import settings


def main() -> int:
    import uvicorn

    uvicorn.run("data_dog.service.app:app", host=settings.service_host(),
                port=settings.service_port(),
                log_level=settings.log_level().lower())
    return 0


if __name__ == "__main__":
    sys.exit(main())
