#!/usr/bin/env bash
# P4-8 一键基准：起服务 → 等就绪 → 跑 loadgen → 抓 /metrics → 出报告。
#
# 用法（Git Bash / WSL / Linux 均可）:
#   bash tools/bench.sh                      # 默认热路径写入 + 候选读取
#   PORT=8000 DURATION=20 bash tools/bench.sh
#
# 设计取舍：
#   - 用**本机 SQLite + 离线 provider**起服务，排除 DB/LLM 外因干扰，先量"代码本身"的延迟；
#     要量 PostgreSQL/容器形态，请改用 `make up` 后再跑 loadgen（见文档「本机数字≠生产数字」）。
#   - 服务端日志进临时文件，压测结束回显尾部，便于看有无异常。
#   - 报告同时写入 benchmarks/last_run.txt（git 忽略），人工核对后把**真实数字**填进 baseline.md。
set -uo pipefail

PORT="${PORT:-8000}"
DURATION="${DURATION:-30}"
DEVICES="${DEVICES:-120}"
WRITE_RPS="${WRITE_RPS:-500}"
READ_RPS="${READ_RPS:-200}"
TENANT="${TENANT:-bench-t1}"
PY="${PY:-.venv/Scripts/python.exe}"
[ -x "$PY" ] || PY="python"

URL="http://127.0.0.1:${PORT}"
LOG="$(mktemp -t data_dog_bench_XXXXXX.log)"
OUT="benchmarks/last_run.txt"
mkdir -p benchmarks

echo "===== P4-8 基准开始（$(date)）====="
echo "URL=$URL  时长=${DURATION}s  workers=$DEVICES  写=${WRITE_RPS}rps 读=${READ_RPS}rps"

# ---- 1. 起服务（后台），只等 /healthz ----
DD_SERVICE_PORT="$PORT" "$PY" -m data_dog.service >"$LOG" 2>&1 &
SRV_PID=$!
cleanup() { kill "$SRV_PID" 2>/dev/null; wait "$SRV_PID" 2>/dev/null; }
trap cleanup EXIT

ready=0
for _ in $(seq 1 40); do
  if curl -sf "$URL/healthz" >/dev/null 2>&1; then ready=1; break; fi
  sleep 0.5
done
if [ "$ready" != "1" ]; then
  # 先分清两种完全不同的失败：① 端口被别人的服务占着（本机 8000 就常被占）
  # ② 我们自己的服务起不来。两者都表现为"未就绪"，但处置方向相反。
  # 只取 info.title。JSON 是压缩的，直接抽 info 对象再抽其中的 title；
  # 不要用 sed 贪婪匹配整行——那会拿到 schema 里的最后一个 "title"（实测拿到 ValidationError）。
  other_title="$(curl -sf --max-time 3 "$URL/openapi.json" 2>/dev/null \
    | tr -d '\n' | grep -o '"info":{[^}]*}' | head -1 \
    | grep -o '"title":"[^"]*"' | head -1 | sed 's/.*:"//; s/"$//')"
  if [ -n "$other_title" ] && ! printf '%s' "$other_title" | grep -qi 'data_dog'; then
    echo "✗ 端口 $PORT 已被另一个服务占用（它自报 title=「$other_title」，不是 data_dog）。"
    echo "  这不是本项目启动失败。换一个端口重跑：PORT=8011 bash tools/bench.sh"
    exit 2
  fi
  echo "✗ 服务未在 20s 内就绪，日志尾部："; tail -30 "$LOG"; exit 1
fi
echo "✓ 服务就绪（pid=$SRV_PID）"

{
  echo "===== data_dog 基准报告 ====="
  echo "日期   : $(date -Is)"
  echo "环境   : 本机 / SQLite / 离线 provider"
  echo "Python : $("$PY" --version 2>&1)"
  echo

  echo "--- 场景 1：遥测写入 POST /v1/telemetry ---"
  "$PY" tools/loadgen.py --url "$URL" --mode write \
      --devices "$DEVICES" --rps "$WRITE_RPS" --duration "$DURATION" \
      --tenant "$TENANT" --verify-server --assert-no-llm
  echo

  echo "--- 场景 2：候选读取 GET /v1/candidates ---"
  "$PY" tools/loadgen.py --url "$URL" --mode read \
      --devices 32 --rps "$READ_RPS" --duration "$DURATION" \
      --tenant "$TENANT" --verify-server
  echo

  echo "--- 服务端 /metrics 关键指标（尾部）---"
  curl -s "$URL/metrics" | grep -E '^data_dog_(http_requests_total|telemetry_events_total|candidates_created_total)' | head -20
} 2>&1 | tee "$OUT"

echo
echo "===== 报告已存 $OUT ====="
echo "把上面 P50/P95/P99 与吞吐核对后，填入 benchmarks/baseline.md（真实数字，勿占位）。"
