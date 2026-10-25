# data_dog 常用命令（Windows 无 make 时见 README 的等价命令）
# 注意：Makefile 的命令行必须以 TAB 缩进。

PY ?= $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; \
              elif [ -n "$(OS)" ] && [ -x .venv/Scripts/python.exe ]; then echo .venv/Scripts/python.exe; \
              else echo python; fi)
PORT ?= 8000
WEB_PORT ?= 3000
# 演示租户/设备：必须与控台的 DD_WEB_TENANT（compose 默认 tenant-demo）一致，
# 否则“按文档跑完 ingest，控台永远是空”——数据落在另一个租户，而是多租户隔离在正常工作。
# 一致性有守卫：tests/test_demo_tenant_consistency.py
TENANT ?= tenant-demo
DEVICE_SN ?= device-demo-0001

.PHONY: help gates hooks numbers requirements format install test lint typecheck cov serve probe ingest e2e ui bench up up-prod down down-prod logs ps health clean helm-lint verify-chart k8s-e2e retention partition partition-check cov-gate cov-gate-pg perf-probe config-check

help:            ## 显示所有命令
	@echo "make install  安装依赖(含服务)"
	@echo "make test     跑全量测试"
	@echo "make lint     ruff 静态检查"
	@echo "make typecheck mypy 类型检查"
	@echo "make gates    提交前必过(ruff + mypy + 全量测试)"
	@echo "make hooks    装 git 钩子(pre-commit: ruff / pre-push: make gates)"
	@echo "make numbers  重算并回填 README 的规模数字"
	@echo "make config-check 配置体检(未知键/非法取值/危险组合; STRICT=1 警告也算失败)"
	@echo "make requirements  由 pyproject.toml 重新生成 requirements.txt"
	@echo "make format   可选: ruff format(会重排大量文件, 慎用)"
	@echo "make cov      带覆盖率跑测试"
	@echo "make cov-gate 单模块覆盖率门禁(默认腿, 下限 70%)"
	@echo "make cov-gate-pg PG 腿门禁(PG-only 文件, 下限 80%)"
	@echo "make serve    本机起服务(不用 Docker)"
	@echo "make probe    探存活/就绪/指标"
	@echo "make bench    P4-8 软实时基准(起服务→压测→出报告)"
	@echo "make retention 数据保留清理(默认 dry-run; FORCE=1 真删)"
	@echo "make partition 确保未来 N 个月分区存在(A9; MONTHS=3)"
	@echo "make partition-check 分区现状(只读, 非 PG 明确说不可用)"
	@echo "make perf-probe 写路径进程内性能探针(不压测)"
	@echo "make ingest   发一条演示日志(过温)"
	@echo "make up       Docker 起整套(应用+PostgreSQL)"
	@echo "make down     停 Docker 整套"
	@echo "make logs     跟 Docker 应用日志"
	@echo "make ps       看 Docker 容器状态"
	@echo "make health   看容器健康状态"
	@echo "make helm-lint    helm lint + template 渲染检查"
	@echo "make verify-chart chart 渲染不变量断言(P4-4)"
	@echo "make k8s-e2e      kind 多副本状态一致验收(需 kind/kubectl)"

install:         ## 安装依赖
	$(PY) -m pip install -e ".[dev,postgres]"

test:            ## 全量测试（-rs：把跳过原因打出来，不把 skip 当 passed）
	$(PY) -m pytest -q -rs

lint:            ## ruff 静态检查（全仓，单一真源）
	$(PY) -m ruff check .

typecheck:       ## mypy 类型检查（两个平台都查：CI 在 Ubuntu 上跑，只看 Windows 会漏）
	$(PY) -m mypy src
	$(PY) -m mypy --platform linux src

gates:           ## 提交前必过：ruff + mypy + 全量测试（命令与 CI 逐字一致）
	$(PY) -m ruff check .
	$(PY) -m mypy src
	# 两个平台都查：CI 的静态腿跑在 Ubuntu 上。只在本机 Windows 上查会漏掉
	# “平台条件属性”（ctypes.WinDLL / msvcrt.open_osfhandle / os.O_BINARY …）——
	# 2026-10-17 的 CI 就是这样红了一个月而本机全绿（ledger #103）。
	$(PY) -m mypy --platform linux src
	$(PY) -m pytest -q -rs
	@test -n "$${DD_TEST_PG_URL:-}" || echo "[gates] ⚠ 未设 DD_TEST_PG_URL：postgres 腿被跳过（没跑 ≠ 通过）。本机验：pytest -q -rs -m postgres（或 make pg-test）"
	@test -n "$${DD_TEST_REDIS_URL:-}" || echo "[gates] ⚠ 未设 DD_TEST_REDIS_URL：redis 腿被跳过（没跑 ≠ 通过）。本机验：docker run -d -p 6379:6379 redis:7-alpine && export DD_TEST_REDIS_URL=redis://127.0.0.1:6379/0 && pytest -q -rs -m redis"

hooks:           ## 装 git 钩子（pre-commit: ruff 变更文件；pre-push: make gates）
	git config core.hooksPath scripts/git-hooks
	@echo "已启用 scripts/git-hooks：pre-commit 跑 ruff，pre-push 跑 make gates"

numbers:         ## 重算并回填 README 的规模数字（由 tests/test_project_meta.py 把关）
	$(PY) scripts/readme_counts.py

config-check:    ## 配置体检：未知键/非法取值/危险组合（STRICT=1 时警告也算失败）
	$(PY) -m data_dog.config_check $(if $(STRICT),--strict,)

requirements:    ## 由 pyproject.toml 重新生成 requirements.txt（单一事实源）
	$(PY) scripts/gen_requirements.py

format:          ## 可选：ruff format（仓库当前未统一格式，会重排约 170 个文件）
	$(PY) -m ruff format src tests

cov:             ## 带覆盖率跑测试（阈值与 CI 一致：总分 85% + 单模块 70%）
	$(PY) -m pytest -q --cov=src/data_dog --cov-report=term-missing --cov-report=json:coverage.json --cov-fail-under=85
	$(PY) scripts/cov_gate.py --json coverage.json --min 70

demo:            ## 一条命令看效果（离线、无网络、无 API Key、无 Docker）
	$(PY) -m data_dog.cli test --offline --all

serve:           ## 本机起服务（零安装：SQLite 自动建表）
	@echo "→ http://127.0.0.1:$(PORT)（另开终端跑 make probe 自检）"
	$(PY) -m data_dog.service

probe:           ## 探针自检
	@echo "--- /healthz ---"; curl -s http://127.0.0.1:$(PORT)/healthz; echo
	@echo "--- /readyz  ---"; curl -s http://127.0.0.1:$(PORT)/readyz; echo
	@echo "--- /metrics ---"; curl -s http://127.0.0.1:$(PORT)/metrics; echo

ingest:          ## 发一条演示日志(96°C 过温 → 应产出 critical 候选)
	curl -s -X POST http://127.0.0.1:$(PORT)/v1/telemetry \
	  -H "Content-Type: application/json" \
	  -H "X-Tenant-Id: $(TENANT)" \
	  -H "X-Device-Sn: $(DEVICE_SN)" \
	  -d '{"events":[{"event_id":"e1","occurred_at":"2026-09-13T10:00:00Z","received_at":"2026-09-13T10:00:00.1Z","sequence":1,"vendor":"unitree","model":"go2","firmware":"3.2.1","protocol_version":"motor-log-v2","component_hint":"joint_motor.front_left_hip","payload_inline":"{\"temperature\":96,\"current\":12.0,\"mode\":0}"}]}'
	@echo
	@echo "→ 控台看这条：http://127.0.0.1:$(WEB_PORT)（租户 $(TENANT)，需与控台 DD_WEB_TENANT 一致）"

e2e:             ## 端到端整链回放
	$(PY) -m data_dog.e2e.replay_demo

bench:           ## P4-8 软实时基准（一键：起服务→loadgen→报告）
	bash tools/bench.sh

retention:       ## 数据保留清理（④）：默认 dry-run 只统计，FORCE=1 才真删
	$(PY) -m data_dog.retention $(if $(FORCE),,--dry-run)

partition-check: ## 分区现状（A9，只读）：是否分区表 / 有哪些分区 / 转换阻塞项
	$(PY) scripts/partition_telemetry.py --check

cov-gate:        ## 单模块覆盖率门禁（默认腿；先 make cov 产出 coverage.json）
	$(PY) scripts/cov_gate.py --json coverage.json --min 70

cov-gate-pg:     ## PG 腿门禁：只查 SQLite 跑不到的 PG-only 文件（先跑 -m postgres --cov）
	$(PY) scripts/cov_gate.py --json coverage-pg.json --only storage/partitioning.py,state/pg.py --no-allow --min 80

pg-test:         ## PG 腿真库测试（先放开 compose.yaml 里 db 的 ports，再 export DD_TEST_PG_URL）
	$(PY) -m pytest -q -rs -m postgres

partition:       ## 分区维护（A9）：确保未来 N 个月分区存在（幂等 DDL；MONTHS=3）
	$(PY) scripts/partition_telemetry.py --ensure-months $(or $(MONTHS),3)

redteam:         ## 提示注入红队（A3）：默认打真模型（需 key）；OFFLINE=1 只自检管道
	$(PY) scripts/redteam_injection.py $(if $(OFFLINE),--offline) --runs $(or $(RUNS),3)

perf-probe:      ## 写路径进程内性能探针（不压测；--only 单跑才有可比绝对值）
	$(PY) tools/perf_probe.py --only http --n $(or $(N),300)

ui:              ## 起 Streamlit 演示台
	$(PY) -m streamlit run ui/app.py

up:              ## Docker 起整套（本机/局域网）
	docker compose up -d --build
	@echo "等待就绪…"; sleep 5; docker compose ps

up-prod:         ## 对外部署（Caddy 自动 HTTPS；需先设 DD_DOMAIN/DD_API_KEY/POSTGRES_PASSWORD）
	docker compose -f compose.prod.yaml up -d --build
	@echo "等待就绪…"; sleep 8; docker compose -f compose.prod.yaml ps

down-prod:       ## 停对外部署
	docker compose -f compose.prod.yaml down

down:            ## 停 Docker 整套
	docker compose down

logs:            ## 跟应用日志
	docker compose logs -f app

ps:              ## 容器状态
	docker compose ps

health:          ## 容器健康状态
	docker inspect --format '{{.Name}} {{.State.Health.Status}}' data_dog-app-1 data_dog-db-1 2>/dev/null || docker compose ps

helm-lint:       ## helm lint + 渲染（默认值与生产覆盖值）
	helm lint helm/data-dog
	helm template dd helm/data-dog >/dev/null
	helm template dd helm/data-dog -f helm/data-dog/values.prod.yaml >/dev/null
	@echo "[helm-lint] lint 与渲染均通过"

verify-chart:    ## chart 渲染不变量断言（P4-4 验收 1）
	$(PY) scripts/verify_chart.py

ci-offline:      ## 离线默认 4 腿（lint test helm web），只需 Docker
	bash scripts/verify_ci_offline.sh

ci-offline-all:  ## 离线 9 腿（`all` 关键字，名单单一真源在 .sh 的 ALL_LEGS）
	bash scripts/verify_ci_offline.sh all

ci-offline-nonroot: ## 9 腿以 uid 65534 重跑（root 会绕过 rlimit，root 绿≠CI 绿）
	DD_CI_RUN_USER=65534 bash scripts/verify_ci_offline.sh all

k8s-e2e:         ## kind 多副本状态一致验收（P4-4 验收 6，需 kind/kubectl/helm）
	bash tools/k8s_e2e.sh

clean:           ## 清本地缓存与 SQLite
	rm -rf .pytest_cache **/__pycache__ *.db
