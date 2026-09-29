# P4-4 L4 编排：K8s / Helm

## 1. 目标

让刚做完的**水平扩展（StateStore 外置）真正跑起来**——多副本没有编排就落不了地。
产出一套能 `helm install` 就起来的 chart，并用 `kind` 本地验收多副本 + HPA 扩容。

## 2. 现状（已核实）

| 已有 | 说明 |
|---|---|
| `Dockerfile` | 非 root、healthcheck |
| `compose.yaml` | app + pg |
| `compose.prod.yaml` | Caddy（自动 HTTPS）+ app + pg |
| `deploy/Caddyfile` | 反代配置 |
| `/healthz` `/readyz` `/metrics` | 探针已就绪（K8s 直接用） |
| `Makefile` `up-prod` / `down-prod` | 现有部署入口 |

**缺**：全部 K8s/Helm 清单、HPA、PDB、迁移 Job 编排、多副本流式消费者编排。

## 3. 设计

### 3.1 Chart 结构

```
helm/data-dog/
  Chart.yaml
  values.yaml
  values.prod.yaml
  templates/
    _helpers.tpl
    deployment-api.yaml        # 无状态 API，replicas=N
    deployment-stream.yaml     # 流式消费者（见 §3.3）
    service.yaml               # ClusterIP
    ingress.yaml               # 可选（或沿用 Caddy 侧）
    hpa.yaml                   # 基于 CPU/自定义指标
    pdb.yaml                   # PodDisruptionBudget
    job-migrate.yaml           # Helm hook: pre-install/pre-upgrade 跑 alembic
    secret.yaml                # 从外部注入（不落 values 明文）
    configmap.yaml             # 非敏感配置
    servicemonitor.yaml        # 可选：Prometheus Operator 抓取 /metrics
    postgres-statefulset.yaml  # 可选：内嵌 PG（生产建议外部托管）
```

### 3.2 关键 values

```yaml
api:
  replicas: 3
  resources: { requests: {cpu: 200m, mem: 512Mi}, limits: {cpu: 1, mem: 1Gi} }
  probes:
    liveness:  { path: /healthz, initialDelaySeconds: 10 }
    readiness: { path: /readyz,  initialDelaySeconds: 5 }
stream:
  enabled: true
  replicas: 2
  source: kafka                # 多副本必须用 kafka 消费者组 / mqtt 共享订阅
state:
  backend: postgres            # 多副本必须共享后端（memory 仅单副本）
  rateBackend: redis
postgres:
  external: true               # 生产用外部托管；否则内嵌
  url: ""                      # 走 Secret
hpa:
  enabled: true
  minReplicas: 3
  maxReplicas: 20
  targetCPUUtilization: 70
```

### 3.3 两种工作负载（关键区分）

- **`deployment-api`（无状态）**：跑 FastAPI。可任意扩缩。**必须** `DD_STATE_BACKEND != memory`
  （否则多副本状态不一致）。启 readiness 探针（`/readyz` 已含 DB + stream 检查）。
- **`deployment-stream`（消费）**：
  - **Kafka 源** → 用消费者组，`replicas` 任意，partition 决定并行度；
  - **MQTT 源** → 用**共享订阅**（`$share/group/topic`）避免重复消费；
  - **file 源** → **只能 1 副本**（单文件，无协调），chart 里用 `replicas: 1` + 
    若配了 file 源而 replicas>1 → **启动即报错**（失败关闭，见缺口-5）。

### 3.4 迁移 Job（不能忘）

alembic `upgrade head` 必须**在应用启动前**跑且**只跑一次**：

```yaml
# templates/job-migrate.yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ .Release.Name }}-migrate
  annotations:
    "helm.sh/hook": pre-install,pre-upgrade
    "helm.sh/hook-delete-policy": before-hook-creation
spec:
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: migrate
          image: {{ .Values.image }}
          command: ["python", "-m", "data_dog.storage.migrations", "upgrade"]
          envFrom: [...]
```

> **已落地**：`storage/migrations.py` 加了 `__main__` 入口（`upgrade` / `current`）。
> **镜像前提**：`Dockerfile` 必须 `COPY alembic.ini` 与 `COPY alembic/` 进去——
> 否则容器内既跑不了本 Job，也做不了启动时的 `DD_AUTO_MIGRATE`（见 §6 诚实边界）。

### 3.5 本地验收：kind

```bash
kind create cluster --name dd
helm install dd ./helm/data-dog -f values.local.yaml
kubectl -n default rollout status deploy/dd-api
# 验多副本状态一致：往两个副本各发一条同设备事件 → 应归并成 1 条候选
kubectl -n default port-forward svc/dd-api 8000:80 &
curl -X POST localhost:8000/v1/telemetry -H 'X-Tenant-Id: t1' ... (打到不同 pod)
curl localhost:8000/v1/candidates -H 'X-Tenant-Id: t1'   # 断言 1 条
```

## 4. 要建/要改的文件

| 文件 | 动作 |
|---|---|
| `helm/data-dog/**` | **全新建**（见 §3.1） |
| `scripts/migrate.py` 或 `storage/migrations.py` | 加 `__main__` 入口供 Job 调用 |
| `src/data_dog/settings.py` | 确认所有配置都能走环境变量（多副本必需） |
| `Makefile` | 加 `helm-lint` / `kind-up` / `k8s-e2e` 目标 |
| `.github/workflows/ci.yml` | 加 `helm lint` + `kubeconform` 校验 |
| `docs/p4/04_k8s_helm.md` | 本文 + 运维 runbook 附录 |

## 5. 验收

1. `helm lint` 通过；`helm template` 渲染无错；`kubeconform` 校验通过。
2. `kind` 起 3 副本 API + 迁移 Job 成功；`/readyz` 全绿。
3. **多副本状态一致**：同设备事件打到不同 pod → 归并成 1 条候选（证明 StateStore 外置生效）。
4. **HPA**：压测触发扩容（或至少 `kubectl describe hpa` 显示目标正确）。
5. **滚动更新**不丢请求（PDB + readiness 生效）。
6. **file 源误配**：file 源 + replicas>1 → 启动失败并给出清晰原因。

## 6. 诚实边界

- **需要一台集群验收**：本地用 `kind` 能验进程编排与多副本一致性；**真云环境**（存储类、
  负载均衡、网络策略）需目标集群实测。
- **内嵌 PG** 仅 demo 用；生产**建议外部托管**（备份/高可用不由 chart 负责）。
- **HPA 按 CPU** 起步；按队列深度等**自定义指标**需 Prometheus Adapter（可选加强）。
- 多区域部署见 P4-6。

## 7. 依赖

- **P4-5 分片**：多副本路由的分片键设计与之衔接（分片是编排的上层语义）。
- **缺口-5 多副本协调**：stream 消费者不重复消费依赖它——**建议先合缺口批**。

## 8. 实现与验收（已落地）

### 8.1 落地的文件

| 文件 | 作用 |
|---|---|
| `helm/data-dog/Chart.yaml` | chart 元数据（v2） |
| `helm/data-dog/values.yaml` | 默认值（kind/本地：SQLite、stream 关、单副本） |
| `helm/data-dog/values.prod.yaml` | 生产覆盖（kafka stream 3 副本、HPA、PDB≥2、ServiceMonitor） |
| `helm/data-dog/templates/_helpers.tpl` | 命名/标签/镜像/envFrom 助手 |
| `helm/data-dog/templates/configmap.yaml` | 非敏感 `DD_*`（**不含** `DD_STREAM_*`，见 8.2） |
| `helm/data-dog/templates/deployment-api.yaml` | 无状态 API；`maxUnavailable:0` 滚动；非 root、drop ALL |
| `helm/data-dog/templates/deployment-stream.yaml` | 独立消费者；**file 源 + 多副本 → `fail`** |
| `helm/data-dog/templates/job-migrate.yaml` | `pre-install,pre-upgrade` hook，跑 `migrations upgrade` |
| `helm/data-dog/templates/{service,ingress,hpa,pdb,servicemonitor,secret,serviceaccount}.yaml` | 配套资源 |
| `src/data_dog/service/stream_worker.py` | 消费者独立入口（`python -m data_dog.service.stream_worker`） |
| `src/data_dog/stream/factory.py` | 多副本**失败关闭**（file/http 源 + `DD_STREAM_SHARDS>1` → 抛错） |
| `src/data_dog/storage/migrations.py` | 加 `__main__`（`upgrade`/`current`）；`project_root()` 委托统一根解析 |
| `Dockerfile` | `COPY alembic.ini` + `COPY alembic/`（见 8.4 的真 bug） |
| `scripts/verify_chart.py` | 渲染后**不变量断言**（不是只 lint） |
| `tools/k8s_e2e.sh` | kind 多副本验收脚本 |
| `Makefile` / `.github/workflows/ci.yml` | `helm-lint`/`verify-chart`/`k8s-e2e` + CI `helm` 腿 |

### 8.2 两处"看似对、其实错"的编排坑（都已在模板层钉死）

1. **ConfigMap 共用导致 API 重复消费**：ConfigMap 是 API 与消费者**共用**的 `envFrom`。
   若把 `DD_STREAM_ENABLED=1` 写进 ConfigMap，API 副本会**各自**再起一个进程内消费者，
   与独立 `deployment-stream` 重复消费。→ 修法：`DD_STREAM_*` **只**由 stream
   deployment 自己的 `env` 注入（`env` 覆盖 `envFrom`），ConfigMap 里恒为 `0`。
2. **HPA 与写死 replicas 打架**：HPA 开启时 Deployment **不能**再写 `replicas`，
   否则滚回一个固定值。→ 模板用 `{{- if not .Values.autoscaling.enabled }}replicas:{{- end }}`。

这两条 `helm lint` **查不出来**（清单语法都对），只有把渲染结果解析出来断言才抓得到——
所以有了 `scripts/verify_chart.py`。

### 8.3 验收对照（§5 六条）

| # | 验收项 | 怎么验 | 状态 |
|---|---|---|---|
| 1 | lint + 渲染 + schema | `make helm-lint` + `make verify-chart` + CI `kubeconform` | ✅ 16 条不变量全过；CI 腿已加 |
| 2 | kind 3 副本 + 迁移 Job + `/readyz` | `make k8s-e2e` | ✅ 脚本就绪（需本地 kind） |
| 3 | 多副本状态一致 | `k8s-e2e`：同设备两事件打到两个 pod → 断言候选=1 | ✅ 脚本就绪（§8.5 说明为何用 PG） |
| 4 | HPA | `values.prod.yaml` 渲染出 `autoscaling/v2`；`kubectl describe hpa` | ✅ 模板 + 断言在 |
| 5 | 滚动不丢请求 | `maxUnavailable:0` + `/readyz` + PDB | ✅ 模板层保证 |
| 6 | file 源误配失败关闭 | chart `fail` + 运行时 `build_source` 抛错 | ✅ 双层，均有测试 |

### 8.4 实跑暴露的真 bug：容器里根本跑不了迁移

在 `docker build` 后实跑 `python -m data_dog.storage.migrations upgrade` 才发现两处接不上：

1. **`Dockerfile` 没拷 `alembic.ini` / `alembic/`**——`pip install.[postgres]` 会带上
   alembic 库，但没有迁移脚本目录，Job 与启动期 `DD_AUTO_MIGRATE=1` 都无处可跑。
2. **`migrations.project_root()` 无视 `DD_PROJECT_ROOT`**——`pip install` 后本模块落在
   `site-packages/data_dog/storage/`，"从本文件上溯找 alembic.ini"必然落空。
   项目本有统一的 `paths.project_root()`（环境变量优先），这里却是**另起一套、env 不认**。

修法：`Dockerfile` 补 `COPY`；`migrations.project_root()` 改为委托 `paths.project_root()`。
两者都补了回归测试（`test_dockerfile_ships_migration_assets`）。**这类 bug 单元测试抓不到**——
源码态下 `alembic.ini` 就在身边、`project_root()` 也对，只有真打镜像、真跑容器才现形。

### 8.5 诚实边界

- **多副本一致性验收需要真共享存储**：`k8s_e2e.sh` 设计为用**共享 PostgreSQL**
  （`DD_STATE_BACKEND=postgres`）验"跨副本归 1 条"。若图省事用库内 SQLite，
  各 pod 各写各的本地文件——必须给 `emptyDir`/hostPath 让文件真共享，且只在
  **单节点** kind 成立；这不是生产形态，脚本里对此有显式注释。生产一律外部托管 PG。
- **HPA 仅按 CPU**：模板已留 `targetMemoryUtilizationPercentage` 钩子；按队列深度等
  **自定义指标**需 Prometheus Adapter，不在本批范围。
- **ServiceMonitor 需集群已装 Prometheus Operator**（提供 CRD），否则该资源渲染出来也应用不上；
  默认关（`serviceMonitor.enabled=false`）。
- **kubeconform 只在 CI 跑**：本地没强依赖它（Windows 上装它反复失败），
  故 `verify_chart.py` 用 helm+PyYAML 做语义断言，schema 校验交给 CI 的 kubeconform 腿。
