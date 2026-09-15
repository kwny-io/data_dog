# data_dog 常用命令（Windows 无 make 时见 README 的等价命令）
# 注意：Makefile 的命令行必须以 TAB 缩进。

PY ?= .venv/Scripts/python.exe
PORT ?= 8000

.PHONY: help install test serve probe ingest e2e ui up down logs ps health clean

help:            ## 显示所有命令
	@echo "make install  安装依赖(含服务)"
	@echo "make test     跑全量测试"
	@echo "make serve    本机起服务(不用 Docker)"
	@echo "make probe    探存活/就绪/指标"
	@echo "make ingest   发一条演示日志(过温)"
	@echo "make up       Docker 起整套(应用+PostgreSQL)"
	@echo "make down     停 Docker 整套"
	@echo "make logs     跟 Docker 应用日志"
	@echo "make ps       看 Docker 容器状态"
	@echo "make health   看容器健康状态"

install:         ## 安装依赖
	$(PY) -m pip install -e ".[dev,postgres]"

test:            ## 全量测试
	$(PY) -m pytest -q

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

clean:           ## 清本地缓存与 SQLite
	rm -rf .pytest_cache **/__pycache__ *.db
