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
          command: ["python", "-m", "data_dog.storage.migrations"]  # 待加 __main__ 入口
          envFrom: [...]
```

> **要补**：给 `storage/migrations.py` 加 `if __name__ == "__main__": upgrade_head()` 入口，
> 或写一个 `scripts/migrate.py`，让 Job 能直接调。

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
