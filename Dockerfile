# data_dog 在线服务镜像（P2 数据面 HTTP 入口）
# 构建：docker build -t data-dog:0.1.0 .
# 运行：docker run --rm -p 8000:8000 data-dog:0.1.0
FROM python:3.12-slim

# 不写 .pyc、stdout/stderr 不缓冲（容器日志实时可见）
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DD_SERVICE_HOST=0.0.0.0 \
    DD_SERVICE_PORT=8000 \
    DD_SERVICE_NAME=data-dog \
    DD_PROJECT_ROOT=/app \
    DD_VENDOR_DOCS=/app/vendor_docs \
    DD_OPERATORS_DIR=/app/operators

WORKDIR /app

# 先只拷贝依赖清单 → 依赖层可被 Docker 缓存（改代码不必重装依赖）
COPY pyproject.toml README.md ./
COPY src ./src
COPY requirements.txt ./

# 以可编辑方式安装（含 PostgreSQL 驱动），镜像自包含全部运行依赖
RUN pip install --upgrade pip && \
    pip install ".[postgres]"

# 运行期资源：厂商文档 + 算子产物 + Schema（服务启动时要读）
COPY vendor_docs ./vendor_docs
COPY operators ./operators
COPY schemas ./schemas

# 非 root 运行（最小权限）
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

# 容器自检：探 /healthz（探测目标是容器内固定地址，非用户可控 URL）
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "import sys,urllib.request; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status==200 else 1)"

CMD ["python", "-m", "data_dog.service"]
