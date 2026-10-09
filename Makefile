# data_dog 常用命令（Windows 无 make 时见 README 的等价命令）
# 注意：Makefile 的命令行必须以 TAB 缩进。

PY ?= $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; \
              elif [ -x .venv/Scripts/python.exe ]; then echo .venv/Scripts/python.exe; \
              else echo python; fi)
PORT ?= 8000

.PHONY: help gates hooks numbers requirements format install test lint typecheck cov serve probe ingest e2e ui bench up up-prod down down-prod logs ps health clean helm-lint verify-chart k8s-e2e

help:            ## 显示所有命令
	@echo "make install  安装依赖(含服务)"
	@echo "make test     跑全量测试"
	@echo "make lint     ruff 静态检查"
	@echo "make typecheck mypy 类型检查"
	@echo "make gates    提交前必过(ruff + mypy + 全量测试)"
	@echo "make hooks    装 git 钩子(pre-commit: ruff / pre-push: make gates)"
	@echo "make numbers  重算并回填 README 的规模数字"
	@echo "make requirements  由 pyproject.toml 重新生成 requirements.txt"
	@echo "make format   可选: ruff format(会重排大量文件, 慎用)"
	@echo "make cov      带覆盖率跑测试"
	@echo "make serve    本机起服务(不用 Docker)"
	@echo "make probe    探存活/就绪/指标"
	@echo "make bench    P4-8 软实时基准(起服务→压测→出报告)"
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

test:            ## 全量测试
	$(PY) -m pytest -q

lint:            ## ruff 静态检查
	$(PY) -m ruff check src tests scripts

typecheck:       ## mypy 类型检查
	$(PY) -m mypy src

gates:           ## 提交前必过：ruff + mypy + 全量测试（等价 CI 的 lint/test 腿）
	$(PY) -m ruff check src tests scripts
	$(PY) -m mypy src
	$(PY) -m pytest -q

hooks:           ## 装 git 钩子（pre-commit: ruff 变更文件；pre-push: make gates）
	git config core.hooksPath scripts/git-hooks
	@echo "已启用 scripts/git-hooks：pre-commit 跑 ruff，pre-push 跑 make gates"

numbers:         ## 重算并回填 README 的规模数字（由 tests/test_project_meta.py 把关）
	$(PY) scripts/readme_counts.py

requirements:    ## 由 pyproject.toml 重新生成 requirements.txt（单一事实源）
	$(PY) scripts/gen_requirements.py

format:          ## 可选：ruff format（仓库当前未统一格式，会重排约 170 个文件）
	$(PY) -m ruff format src tests

cov:             ## 带覆盖率跑测试（阈值与 CI 一致：总分 85% + 单模块 60%）
	$(PY) -m pytest -q --cov=src/data_dog --cov-report=term-missing --cov-report=json:coverage.json --cov-fail-under=85
	$(PY) scripts/cov_gate.py --json coverage.json --min 60

serve:           ## 本机起服务
	$(PY) -m data_dog.service

probe:           ## 探针自检
	@echo "--- /healthz ---"; curl -s http://127.0.0.1:$(PORT)/healthz; echo
	@echo "--- /readyz  ---"; curl -s http://127.0.0.1:$(PORT)/readyz; echo
	@echo "--- /metrics ---"; curl -s http://127.0.0.1:$(PORT)/metrics; echo

ingest:          ## 发一条演示日志(96°C 过温 → 应产出 critical 候选)
	curl -s -X POST http://127.0.0.1:$(PORT)/v1/telemetry \
	  -H "Content-Type: application/json" \
	  -H "X-Tenant-Id: tenant-real" \
	  -H "X-Device-Sn: device-go2-0001" \
	  -d '{"events":[{"event_id":"e1","occurred_at":"2026-09-13T10:00:00Z","received_at":"2026-09-13T10:00:00.1Z","sequence":1,"vendor":"unitree","model":"go2","firmware":"3.2.1","protocol_version":"motor-log-v2","component_hint":"joint_motor.front_left_hip","payload_inline":"{\"temperature\":96,\"current\":12.0,\"mode\":0}"}]}'
	@echo

e2e:             ## 端到端整链回放
	$(PY) -m data_dog.e2e.replay_demo

bench:           ## P4-8 软实时基准（一键：起服务→loadgen→报告）
	bash tools/bench.sh

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

k8s-e2e:         ## kind 多副本状态一致验收（P4-4 验收 6，需 kind/kubectl/helm）
	bash tools/k8s_e2e.sh

clean:           ## 清本地缓存与 SQLite
	rm -rf .pytest_cache **/__pycache__ *.db
