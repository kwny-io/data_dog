#!/usr/bin/env bash
# P4-4 验收：kind 上跑多副本 API，证明"状态外置 + 编排"真的成立。
#
# 为什么自建而不是只跑 helm lint
# ------------------------------
# lint/渲染只能证明"清单长对了"；证明不了"多副本共享同一份状态"这件事真的发生。
# 本脚本在 kind 上起 chart + 一份**共享 PostgreSQL**，然后**把同设备的两条事件
# 分别打到两个不同的 pod**——若是各副本各写各的本地库，查询会看到 2 条候选；
# 只有状态外置（共享 PG）才会归并成 1 条。这是 §5 验收第 3、6 条的实证。
#
# 为什么用共享 PostgreSQL、而不是库内 SQLite
# ------------------------------------------
# SQLite 是**单文件、无并发协调**的本地库：多 pod 各写各的文件，归并必然失败
# （除非把同一文件共享给所有 pod——那既非生产形态、也要 ReadWriteMany）。
# 生产多副本**就是**连一个共享 PG，故这里就用它。镜像已含 psycopg 驱动。
# 注：pg 仅在验收**结束后**删除（见 cleanup），失败时保留集群便于排查。
#
# 前置：kind / kubectl / helm / docker 均在 PATH。
# 用法：bash tools/k8s_e2e.sh
# 退出码：0=全过；非 0=有用例失败。
set -uo pipefail

CLUSTER="${DD_KIND_CLUSTER:-dd-e2e}"
IMAGE="${DD_E2E_IMAGE:-data-dog:e2e}"
RELEASE="dd"
NS="default"
TENANT="tenant-e2e"
DEVICE="device-e2e-0001"
COMPONENT="joint_motor.front_left_hip"
PGNAME="dd-e2e-pg"
PGUSER="data_dog"
PGPASS="data_dog_e2e_pw"
PGDB="data_dog"

PASS=0; FAIL=0
ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
bad()  { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
need() { command -v "$1" >/dev/null 2>&1 || { echo "[k8s_e2e] 缺少 $1（请安装后重试）" >&2; exit 2; }; }

need kind; need kubectl; need helm; need docker

PF_PIDS=()
cleanup() {
  for p in "${PF_PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done
  # 保留集群便于排查；要清理：kind delete cluster --name "$CLUSTER"
}
trap cleanup EXIT

wait_pf() {  # wait_pf <local_port>：等 port-forward 就绪
  local port="$1"
  for _ in $(seq 1 40); do
    if curl -sf -o /dev/null "http://127.0.0.1:${port}/healthz"; then return 0; fi
    sleep 0.5
  done
  return 1
}

pf_pod() {  # pf_pod <pod> <local_port>：转发到具名 pod → 确定性命中该副本
  local pod="$1" port="$2"
  kubectl -n "$NS" port-forward "pod/${pod}" "${port}:8000" >/dev/null 2>&1 &
  PF_PIDS+=($!)
  wait_pf "$port"
}

post_event() {  # post_event <local_port> <event_id>
  local port="$1" eid="$2"
  curl -s -X POST "http://127.0.0.1:${port}/v1/telemetry" \
    -H "Content-Type: application/json" \
    -H "X-Tenant-Id: ${TENANT}" \
    -H "X-Device-Sn: ${DEVICE}" \
    -d "{\"events\":[{\"event_id\":\"${eid}\",\"occurred_at\":\"2026-09-30T10:00:00Z\",\"received_at\":\"2026-09-30T10:00:00.1Z\",\"sequence\":1,\"vendor\":\"unitree\",\"model\":\"go2\",\"firmware\":\"3.2.1\",\"protocol_version\":\"motor-log-v2\",\"component_hint\":\"${COMPONENT}\",\"payload_inline\":\"{\\\"temperature\\\":96,\\\"current\\\":12.0,\\\"mode\\\":0}\"}]}"
}

echo "== [1/6] 建 kind 集群 =="
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  ok "集群 ${CLUSTER} 已存在，复用"
else
  kind create cluster --name "$CLUSTER" --wait 120s >/dev/null && ok "集群已建" || { bad "建集群失败"; exit 1; }
fi

echo "== [2/6] 构建镜像并载入集群 =="
docker build -t "$IMAGE" . >/dev/null && ok "镜像构建完成：$IMAGE" || { bad "镜像构建失败"; exit 1; }
kind load docker-image "$IMAGE" --name "$CLUSTER" >/dev/null && ok "镜像已载入 kind" || { bad "载入失败"; exit 1; }

echo "== [3/6] 起一份共享 PostgreSQL（多副本一致性的前提）=="
kubectl -n "$NS" apply -f - >/dev/null <<YAML
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${PGNAME}
spec:
  replicas: 1
  selector:
    matchLabels: { app: ${PGNAME} }
  template:
    metadata:
      labels: { app: ${PGNAME} }
    spec:
      containers:
        - name: pg
          image: postgres:16-alpine
          env:
            - { name: POSTGRES_USER, value: "${PGUSER}" }
            - { name: POSTGRES_PASSWORD, value: "${PGPASS}" }
            - { name: POSTGRES_DB, value: "${PGDB}" }
          ports:
            - { containerPort: 5432 }
          readinessProbe:
            exec: { command: ["pg_isready", "-U", "${PGUSER}"] }
            initialDelaySeconds: 5
            periodSeconds: 5
---
apiVersion: v1
kind: Service
metadata:
  name: ${PGNAME}
spec:
  selector: { app: ${PGNAME} }
  ports:
    - { port: 5432, targetPort: 5432 }
YAML
kubectl -n "$NS" rollout status deploy/"${PGNAME}" --timeout=120s >/dev/null \
  && ok "PostgreSQL 就绪" || { bad "PostgreSQL 未就绪"; exit 1; }

echo "== [4/6] helm install（3 副本 API + 迁移 hook + 连共享 PG）=="
PGURL="postgresql+psycopg://${PGUSER}:${PGPASS}@${PGNAME}:5432/${PGDB}"
helm upgrade --install "$RELEASE" ./helm/data-dog --wait --timeout 240s \
  --set image.repository="${IMAGE%%:*}" \
  --set image.tag="${IMAGE##*:}" \
  --set image.pullPolicy=IfNotPresent \
  --set api.replicas=3 \
  --set config.DD_STATE_BACKEND=postgres \
  --set secret.create=true \
  --set "secret.data.DD_DATABASE_URL=${PGURL}" \
  >/dev/null && ok "helm install 成功" || { bad "helm install 失败"; kubectl -n "$NS" get all; exit 1; }

kubectl -n "$NS" rollout status deploy/"${RELEASE}-data-dog-api" --timeout=180s >/dev/null \
  && ok "API rollout 完成" || bad "API rollout 超时"

JOB=$(kubectl -n "$NS" get job -o name | grep migrate | head -1)
if [ -n "$JOB" ]; then
  kubectl -n "$NS" wait --for=condition=complete "$JOB" --timeout=120s >/dev/null \
    && ok "迁移 Job 完成（pre-install hook）" || bad "迁移 Job 未完成"
fi

READY=$(kubectl -n "$NS" get deploy "${RELEASE}-data-dog-api" -o jsonpath='{.status.readyReplicas}')
[ "${READY:-0}" = "3" ] && ok "3 个副本全部 Ready" || bad "Ready 副本数=${READY:-0}（期望 3）"

echo "== [5/6] 多副本状态一致（验收 3）=="
# 先确认应用**真的**连的是 PG（否则一致性结论不成立）
kubectl -n "$NS" port-forward "svc/${RELEASE}-data-dog" "18002:80" >/dev/null 2>&1 &
PF_PIDS+=($!)
wait_pf 18002 || bad "port-forward svc 失败"
SCHEME=$(curl -s "http://127.0.0.1:18002/readyz" \
          | kubectl -n "$NS" exec -i deploy/"${RELEASE}-data-dog-api" -- \
            python -c "import sys,json;print(json.load(sys.stdin)['checks']['database']['url_scheme'])" 2>/dev/null)
[ "$SCHEME" = "postgresql" ] && ok "应用连的是共享 PG（url_scheme=postgresql）" \
                             || bad "数据库后端=${SCHEME:-?}（期望 postgresql，否则多副本必不一致）"

PODS=($(kubectl -n "$NS" get pods -l app.kubernetes.io/component=api \
        --field-selector=status.phase=Running -o jsonpath='{.items[*].metadata.name}'))
if [ "${#PODS[@]}" -lt 2 ]; then
  bad "运行中的 API pod 少于 2 个，无法验证跨副本"
else
  pf_pod "${PODS[0]}" 18000 || bad "port-forward pod0 失败"
  pf_pod "${PODS[1]}" 18001 || bad "port-forward pod1 失败"
  # 同设备、同部件、同信号的两条事件，分别打到**不同**的 pod
  post_event 18000 "e-a" >/dev/null && ok "事件 e-a 打到 ${PODS[0]}" || bad "e-a 发送失败"
  post_event 18001 "e-b" >/dev/null && ok "事件 e-b 打到 ${PODS[1]}" || bad "e-b 发送失败"

  TOTAL=$(curl -s "http://127.0.0.1:18002/v1/candidates" -H "X-Tenant-Id: ${TENANT}" \
            | kubectl -n "$NS" exec -i deploy/"${RELEASE}-data-dog-api" -- \
              python -c "import sys,json;print(json.load(sys.stdin)['total'])" 2>/dev/null)
  [ "$TOTAL" = "1" ] && ok "跨副本归并为 1 条候选（状态外置生效）" \
                     || bad "候选数=${TOTAL:-?}（期望 1；>1 说明各副本写了各自的库）"
fi

echo "== [6/6] 失败关闭：file 源 + 多副本被拒（验收 6）=="
if helm template "$RELEASE" ./helm/data-dog \
     --set stream.enabled=true --set stream.source=file --set stream.replicas=3 \
     >/dev/null 2>&1; then
  bad "file 源 + replicas=3 竟被渲染通过（应 fail）"
else
  ok "file 源 + replicas=3 被 chart 拒绝（fail 拦截，非静默）"
fi

echo
echo "==================== 结果 ===================="
echo "通过 ${PASS} 项，失败 ${FAIL} 项"
echo "集群保留：kind delete cluster --name ${CLUSTER}"
[ "$FAIL" -eq 0 ] || exit 1
