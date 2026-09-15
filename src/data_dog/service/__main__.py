# -*- coding: utf-8 -*-
"""`python -m data_dog.service` 启动器。

读 DD_SERVICE_HOST / DD_SERVICE_PORT / DD_SERVICE_NAME，交给 uvicorn 起服务。
"""
from __future__ import annotations

import os
import sys


def main() -> int:
    import uvicorn

    host = os.environ.get("DD_SERVICE_HOST", "0.0.0.0")
    port = int(os.environ.get("DD_SERVICE_PORT", "8000"))
    uvicorn.run("data_dog.service.app:app", host=host, port=port,
                log_level=os.environ.get("DD_LOG_LEVEL", "info"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
