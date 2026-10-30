#!/usr/bin/env bash
# 一键起本地调试三件套：后端 + 仿真控制台 + 前端。
#
#   bash tools/dev.sh            # 或 make dev
#
# 三块各是什么、为什么是三个进程
# ------------------------------
#   :8000  data_dog.service  业务 API（候选 / 调查 / 知识库），有 DB 和认证
#   :8099  data_dog.go2_sim  仿真控制台：MuJoCo 跑起来、电机数据灌进数据面，
#                            并把候选转给同一个页面看
#   :3000  web/              Next.js 控制台，服务端代理转发 /api/dd/* 到 :8000
#
# **仿真器和后端是两个独立进程**：仿真器自己跑数据面（`subsystem_operator`），
# 故障一条链路在 :8099 页面就能看到；后端那条链路（候选→Agent→报告）走 :3000。
# 两者都要开，调试才完整。
#
# 端口冲突的处置
# --------------
# **不自动杀进程。** 本机 8000 常年跑着旧实例（实测一个跑了 255390 秒），
# 直接接管会杀掉别人没保存状态的进程。这里只探测并说清楚是谁占着，
# 由人决定。要自动清理是另一个决策，不做。
#
# 就绪判定
# --------
# 不只看"端口通了"，还要看**是不是我们自己的服务**：后端比对 /healthz 的
# service 字段，仿真器比对 /api/state 的字段名。端口被别的程序占着时，
# "通了"是个假信号，必须和"起不来"分开报（处置方向相反）。
#
# 三条踩过的坑（每一条都会让"脚本看起来在等"，实际永远等不到）
# ------------------------------------------------------------
# 1. **后端认的是 DD_SERVICE_PORT，不是 PORT。** 只传 PORT=8011 时，
#    后端照样去绑 8000；8000 被旧实例占着 → bind 失败 → 进程秒退。
#    脚本却一直在探 8011，探到超时。**发送目标的变量名必须和目标程序的
#    配置键一致**，不能因为"看着都叫端口"就假设同名。
# 2. **子进程退出了要立刻知道。** 上面那种秒退，如果只轮询探测函数，
#    要等满一整个超时才报"未就绪"，把"进程死了"误报成"启动慢"——
#    两者的处置完全相反。所以每轮都查子进程还在不在，退出就立刻取退出码。
# 3. **探测超时要按真实网络行为算。** 往一个没人监听的端口发 HTTP，
#    Windows 上要到 TCP 连接超时才返回（实测 2.1s），不是脚本里写的那 2s
#    预算。`--max-time 2` 只是上限，下限由连接本身决定。轮询间隔因此
#    必须用**墙钟**算，不能拿"轮询次数 × 间隔"冒充秒数。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

PORT="${PORT:-8000}"
SIM_PORT="${SIM_PORT:-8099}"
WEB_PORT="${WEB_PORT:-3000}"

# 后端读 DD_SERVICE_PORT（见 src/data_dog/settings.py::service_port）。
# 这里显式导出，别指望它跟 PORT 同名——那是坑 1。
export DD_SERVICE_PORT="$PORT"

PY="${PY:-}"
if [ -z "$PY" ]; then
  if [ -x .venv/Scripts/python.exe ]; then PY=".venv/Scripts/python.exe"
  elif [ -x .venv/bin/python ]; then PY=".venv/bin/python"
  else PY="python"; fi
fi

LOGDIR="$ROOT/.dev-logs"
mkdir -p "$LOGDIR"
API_LOG="$LOGDIR/api.log"
SIM_LOG="$LOGDIR/sim.log"
WEB_LOG="$LOGDIR/web.log"

PIDS=()
API_PID=""; SIM_PID=""; WEB_PID=""

cleanup() {
  trap - INT TERM EXIT
  echo
  echo "→ 停止三件套…"
  for pid in "${PIDS[@]:-}"; do
    [ -n "$pid" ] || continue
    # 整棵进程树：uvicorn / next dev 都会再 fork 子进程，单杀父进程会留孤儿
    if command -v taskkill >/dev/null 2>&1; then
      taskkill //PID "$pid" //T //F >/dev/null 2>&1
    else
      pkill -TERM -P "$pid" >/dev/null 2>&1
      kill -TERM "$pid" >/dev/null 2>&1
    fi
  done
  wait 2>/dev/null
  echo "→ 已停止。日志留在 .dev-logs/"
}
trap cleanup INT TERM EXIT

# ---- 工具函数 ---------------------------------------------------------------

port_busy() {  # $1=端口
  "$PY" - "$1" <<'PY'
import socket, sys
s = socket.socket()
s.settimeout(0.4)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
}

# 端口上是谁？取 openapi.json 里 info.title，分得清"我们自己"/"别的程序"。
owner_title() {  # $1=端口
  curl -sf --max-time 3 "http://127.0.0.1:$1/openapi.json" 2>/dev/null \
    | tr -d '\n' | grep -o '"info":{[^}]*}' | head -1 \
    | grep -o '"title":"[^"]*"' | head -1 | sed 's/.*:"//; s/"$//'
}

# 探针统一用 `--connect-timeout 1`：端口没人监听时立刻返回（约 1.2s），
# 而不是等默认的 TCP 超时（2.1s）。一次探针的代价直接决定轮询节奏。
_probe() {  # $1=URL $2=期望出现的片段
  curl -sf --connect-timeout 1 --max-time 3 "$1" 2>/dev/null | grep -q "$2"
}

api_is_ours() {  # 后端探针：/healthz 的 service 字段
  _probe "http://127.0.0.1:$PORT/healthz" '"service"'
}

sim_is_ours() {  # 仿真器探针：/api/state 有 data_plane/frame 这几个键
  _probe "http://127.0.0.1:$SIM_PORT/api/state" '"data_plane"'
}

web_is_ours() {  # 前端探针：next dev 的页面里有 __next 标记
  _probe "http://127.0.0.1:$WEB_PORT/" '__NEXT_DATA__\|/_next/'
}

# 子进程还活着吗？**不能用 `kill -0`**：bash 里已退出但尚未 wait 的子进程
# 是僵尸，`kill -0` 对它照样返回 0（实测如此），等于永远"活着"。
# `jobs -pr` 只列"正在运行"的作业，僵尸不在其中。
alive() {  # $1=pid
  jobs -pr 2>/dev/null | grep -qx "$1"
}

fail_port() {  # $1=端口 $2=用途 $3=变量名
  local title
  title="$(owner_title "$1")"
  if [ -n "$title" ]; then
    echo "✗ 端口 $1（$2）被另一个服务占用：它自报 title=「$title」"
  else
    echo "✗ 端口 $1（$2）已被占用（未响应 openapi，可能是非 HTTP 进程）"
  fi
  echo "  查占用者：  netstat -ano | grep \":$1\""
  echo "  换端口重跑：$3=8011 bash tools/dev.sh"
  echo
  echo "  ⚠ 不自动杀进程：那可能是你没保存状态的调试实例。"
  exit 2
}

# 等一个服务就绪。$5 是它的子进程 pid——每轮先查这个，比探测函数更重要：
# 进程已经退出却还在轮询，就是把"死了"错报成"慢"（坑 2）。
wait_for() {  # $1=描述 $2=探测函数名 $3=日志 $4=秒 $5=pid
  local desc="$1" probe="$2" log="$3" limit="${4:-60}" pid="${5:-}"
  local start now rc
  start=$(date +%s)
  # 墙钟计时：单次探针本身要花 1.2s，拿"次数×间隔"算秒数会严重低估（坑 3）。
  while :; do
    if "$probe"; then return 0; fi
    if [ -n "$pid" ] && ! alive "$pid"; then
      wait "$pid" 2>/dev/null; rc=$?
      echo "✗ $desc 的进程已退出（exit=$rc），不是启动慢。日志尾部："
      tail -30 "$log"
      return 1
    fi
    now=$(date +%s)
    if [ $((now - start)) -ge "$limit" ]; then
      echo "✗ $desc 未在 ${limit}s 内就绪（进程仍在跑，是启动慢）。日志尾部："
      tail -30 "$log"
      return 1
    fi
    sleep 0.5
  done
}

# ---- 0. 前置检查 ------------------------------------------------------------

echo "===== data_dog 本地调试环境 ====="
echo

if [ ! -d web/node_modules ]; then
  echo "✗ web/node_modules 不存在，先装依赖：cd web && npm install"
  exit 1
fi

for p in "$PORT:后端:PORT" "$SIM_PORT:仿真控制台:SIM_PORT" "$WEB_PORT:前端:WEB_PORT"; do
  port="${p%%:*}"; rest="${p#*:}"; name="${rest%%:*}"; var="${rest#*:}"
  if port_busy "$port"; then
    fail_port "$port" "$name" "$var"
  fi
done
echo "✓ 端口 $PORT / $SIM_PORT / $WEB_PORT 均空闲"

# 本机 SQLite 只有一把写锁。另一个 data_dog 实例占着同一个库时，新实例会**卡死在
# 迁移那一步**——日志停在 "Waiting for application startup"，不报错、不退出，
# 看起来像"启动很慢"（实测 >4 分钟仍不响应）。这是最容易误判的一种失败：
# 端口检查过得去（换端口了），却永远起不来。
DB_FILE="$(ls data_dog.db data/data_dog.db 2>/dev/null | head -1)"
if [ -n "$DB_FILE" ] && [ -z "${DD_DATABASE_URL:-}" ]; then
  echo "  ⚠ 使用本机 SQLite：$DB_FILE（可用 DD_DATABASE_URL 换库）"
  echo "    提示：若启动后长时间停在 'Waiting for application startup'，"
  echo "    多半是另一个 data_dog 实例正占着这个库的写锁。"
  echo "    换库重跑：DD_DATABASE_URL=sqlite:///./data_dog_dev.db bash tools/dev.sh"
fi
echo

# ---- 1. 后端 ----------------------------------------------------------------

echo "→ 起后端 :$PORT（日志 $API_LOG）"
"$PY" -m data_dog.service >"$API_LOG" 2>&1 &
API_PID=$!
PIDS+=("$API_PID")
wait_for "后端" api_is_ours "$API_LOG" 90 "$API_PID" || exit 1
echo "  ✓ 后端就绪"

# ---- 2. 仿真控制台 -----------------------------------------------------------
#
# `--publish-hz` 默认 10Hz：控制台页面够用。要压真链路再往上调，
# 并同步调高下游限流（见 server.py 里的频率说明）。

echo "→ 起仿真控制台 :$SIM_PORT（日志 $SIM_LOG）"
"$PY" -m data_dog.go2_sim.server --port "$SIM_PORT" >"$SIM_LOG" 2>&1 &
SIM_PID=$!
PIDS+=("$SIM_PID")
wait_for "仿真控制台" sim_is_ours "$SIM_LOG" 90 "$SIM_PID" || exit 1
echo "  ✓ 仿真控制台就绪（MuJoCo 首帧较慢，之后正常）"

# ---- 3. 前端 ----------------------------------------------------------------
#
# 在同端子 shell 里导出 DD_WEB_*，**不写进 web/.env.local**——
# 那份文件的旧变量（NEXT_PUBLIC_API_KEY 等）已无人读取且会进浏览器包，
# 不该再往里加东西，也不该在这里悄悄改用户的文件。

echo "→ 起前端 :$WEB_PORT（日志 $WEB_LOG）"
(
  cd web || exit 1
  export DD_WEB_API_BASE_URL="http://127.0.0.1:$PORT"
  export DD_WEB_TENANT="${DD_WEB_TENANT:-tenant-demo}"
  # 后端没设 DD_API_KEY 时无需前端带密钥；设了则从这里传。
  if [ -n "${DD_API_KEY:-}" ]; then export DD_WEB_API_KEY="$DD_API_KEY"; fi
  # 本机调试：后端无密钥时代理默认放行；设了密钥却没门禁时它失败关闭（503），
  # 那时按它报的错设 DD_WEB_BASIC_AUTH 或 DD_WEB_ALLOW_ANONYMOUS=1。
  exec npx next dev -p "$WEB_PORT"
) >"$WEB_LOG" 2>&1 &
WEB_PID=$!
PIDS+=("$WEB_PID")
wait_for "前端" web_is_ours "$WEB_LOG" 120 "$WEB_PID" || exit 1
echo "  ✓ 前端就绪"

# ---- 4. 汇总 ----------------------------------------------------------------

cat <<EOF

===== 三个入口 =====
  后端 API      http://127.0.0.1:$PORT      （文档 /docs，自检 /healthz）
  仿真控制台    http://127.0.0.1:$SIM_PORT    ← 3D 视图、12 关节遥测、故障注入
  前端控制台    http://127.0.0.1:$WEB_PORT

  Ctrl-C 一次性全停。日志：.dev-logs/{api,sim,web}.log

  想看后端那条完整链路（候选→调查→报告），先灌一条演示日志：
    make ingest      # 96°C 过温，应产出 critical 候选
EOF

# 前台跟随日志：任一进程退出就提示（不自动重启，让人看见为什么挂）
tail -f "$API_LOG" "$SIM_LOG" "$WEB_LOG" 2>/dev/null
