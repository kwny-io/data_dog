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

# ---- 依赖层（可被 Docker 缓存）------------------------------------------------
# **只拷清单，不拷源码**。之前这里写了“先只拷依赖清单 → 改代码不必重装依赖”，
# 但紧跟着就把 `src` 拷了进来，于是 `pip install .` 那一层**每次改代码都会失效**，
# 重装全部依赖（实测一次 >15 分钟，`docker compose up -d --build` 直接超时）。
# 注释承诺了而代码没做的事，比没写注释更坑。现在真的拆开了：
#   1) 先按清单装**第三方依赖**（只有清单变了这层才失效）；
#   2) 再拷源码，用 `--no-deps` 做可编辑安装（秒级，不碰网络）。
COPY requirements.txt ./
RUN pip install --upgrade pip && \
    pip install -r requirements.txt "psycopg[binary]>=3.1"

# ---- 代码层（每次改代码只重跑这几层，秒级）--------------------------------
COPY pyproject.toml README.md ./
COPY src ./src
# `--no-deps`：依赖已在上一层装齐。加 extras 名只为让元数据（版本号/入口点）正确生成。
RUN pip install --no-deps -e ".[postgres]"

# 运行期资源：厂商文档 + 算子产物 + Schema（服务启动时要读）
COPY vendor_docs ./vendor_docs
COPY operators ./operators
COPY schemas ./schemas

# 迁移资源：alembic.ini + alembic/ 必须进镜像——否则容器内既跑不了
# `python -m data_dog.storage.migrations upgrade`（K8s 迁移 Job / compose），
# 也做不了启动时的 `DD_AUTO_MIGRATE=1`（migrations.project_root() 找不到 alembic.ini
# 会退化成猜路径 → 迁移失败）。二者都依赖这两份。
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic

# 非 root 运行（最小权限）
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

# 容器自检：探 /healthz（探测目标是容器内固定地址，非用户可控 URL）
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "import sys,urllib.request; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status==200 else 1)"

CMD ["python", "-m", "data_dog.service"]
