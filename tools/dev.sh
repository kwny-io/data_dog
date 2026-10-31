#!/usr/bin/env bash
# 本地调试三件套的启动器：后端 + 仿真控制台 + 前端。
#
#   bash tools/dev.sh            启动（后台守护，命令立刻返回）
#   bash tools/dev.sh stop       停止（只停本脚本起的，不误杀别人的进程）
#   bash tools/dev.sh status     看现在是什么状态
#   bash tools/dev.sh restart    停止后再启动
#   bash tools/dev.sh logs       跟三份日志
#   bash tools/dev.sh start -f   启动后留在前台跟日志（老行为）
#
#   make dev / make dev-stop / make dev-status / make dev-restart
#
# 三块各是什么、为什么是三个进程
# ------------------------------
#   :8000  data_dog.service  业务 API（候选 / 调查 / 知识库），有 DB 和认证
#   :8099  data_dog.go2_sim  仿真控制台：MuJoCo 跑起来、电机数据灌进数据面
#   :3000  web/              Next.js 控制台，服务端代理转发 /api/dd/* 到 :8000
#
# **仿真器和后端是两个独立进程**：仿真器自己跑数据面（`subsystem_operator`），
# 故障一条链路在 :8099 页面就能看到；后端那条链路（候选→Agent→报告）走 :3000。
# 两者都要开，调试才完整。
#
# 为什么要有守护进程
# ------------------
# 直接 `cmd &` 起的子进程挂在当前 shell 上：关掉终端、或者被上游工具回收时
# 会一起死（实测在自动化环境里 100% 被杀）。所以 `start` 不自己起服务，
# 而是把一份 `__supervise` 脚本**脱离当前会话**拉起来，由它持有三个子进程。
# 启动器本身随即退出，你可以在同一个终端继续敲别的命令。
#
# 守护进程的生命周期规则：**任一服务意外退出 = 整栈收尾**。
# 刻意不自动重启——半死的栈（比如前端还在、后端没了）会让人误判"后端挂了但页面能开"，
# 比直接停掉更难查。要重跑就 `restart`，日志会留在这里说明为什么挂。
#
# 端口冲突的处置
# --------------
# **start 不自动杀进程。** 本机 8000 常年跑着旧实例（实测一个跑了 255390 秒），
# 直接接管会杀掉别人没保存状态的进程。这里只探测并说清楚是谁占着，由人决定。
# **stop 只杀确认是自己人**：先过服务探针（比对 /healthz 的 service 字段、
# /api/state 的字段名、页面里的 __next 标记），探针不通过就跳过——端口被别的
# 程序占着时"端口通了"是假信号，误杀比不杀代价大得多。
#
# 就绪判定
# --------
# 不只看"端口通了"，还要看**是不是我们自己的服务**。端口被别的程序占着时，
# "通了"和"起不来"的处置方向相反，必须分开报。
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
SELF="$ROOT/tools/dev.sh"
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
# 守护进程是全新的一次解释器，靠环境变量把这些选择带过去。
export PY PORT SIM_PORT WEB_PORT

LOGDIR="$ROOT/.dev-logs"
mkdir -p "$LOGDIR"
API_LOG="$LOGDIR/api.log"
SIM_LOG="$LOGDIR/sim.log"
WEB_LOG="$LOGDIR/web.log"
DEV_LOG="$LOGDIR/dev.log"
PIDS_FILE="$LOGDIR/dev.pids"
FAIL_FILE="$LOGDIR/dev.failed"
READY_FILE="$LOGDIR/dev.ready"

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

# 端口现在被哪个 pid 监听？stop 需要一个确定的杀谁的目标。
listen_pid() {  # $1=端口
  if command -v netstat >/dev/null 2>&1; then
    netstat -ano 2>/dev/null \
      | grep -E "[.:]$1[[:space:]]" | grep -i 'LISTENING' \
      | awk '{print $NF}' | head -1
  elif command -v lsof >/dev/null 2>&1; then
    lsof -ti "tcp:$1" -sTCP:LISTEN 2>/dev/null | head -1
  else
    ss -lptn 2>/dev/null | grep -E "[.:]$1[[:space:]]" \
      | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2
  fi
}

# **Git Bash 里有两套 pid，混用会静默出错。** `$$` / `$!` / `jobs -pr` 给的是
# MSYS 虚拟 pid（本次实测 130141），而 `tasklist` / `taskkill` / `netstat -ano`
# 认的是 Windows 原生 pid（同一个进程是 704）。拿虚拟 pid 去 taskkill，要么
# 报"找不到进程"，要么更糟——撞上另一个恰好同号的 Windows 进程。
# 所以：**跨进程的落盘和系统级操作一律用 Windows pid**，只在 shell 内部
# （`jobs -pr` 判断子进程存活）才用 MSYS pid。
to_winpid() {  # $1=MSYS pid → Windows pid；POSIX 上原样返回
  local w
  w="$(cat "/proc/$1/winpid" 2>/dev/null)"
  [ -n "$w" ] && { echo "$w"; return 0; }
  echo "$1"
}

pid_alive() {  # $1=Windows pid
  [ -n "${1:-}" ] || return 1
  if command -v tasklist >/dev/null 2>&1; then
    tasklist //FI "PID eq $1" 2>/dev/null | grep -qw "$1"
  else
    kill -0 "$1" 2>/dev/null
  fi
}

# 整棵进程树：uvicorn / next dev 都会再 fork 子进程，单杀父进程会留孤儿。
# 入参可以是 MSYS pid 或 Windows pid，内部统一成 Windows pid 再动手。
kill_tree() {  # $1=pid
  local pid="${1:-}"
  [ -n "$pid" ] || return 0
  if command -v taskkill >/dev/null 2>&1; then
    taskkill //PID "$(to_winpid "$pid")" //T //F >/dev/null 2>&1
  else
    pkill -TERM -P "$pid" >/dev/null 2>&1
    kill -TERM "$pid" >/dev/null 2>&1
  fi
}

# 探针统一用 `--connect-timeout 1`：端口没人监听时立刻返回（约 1.2s），
# 而不是等默认的 TCP 超时（2.1s）。一次探针的代价直接决定轮询节奏。
#
# **不能写成 `curl ... | grep -q`。** 脚本开了 `set -o pipefail`，而 `grep -q`
# 一命中就退出 → curl 还在往管道里写 → 收到 SIGPIPE → curl 退出码 23 →
# 整条管道被判失败。页面越大越容易触发（首页 32871 字节，实测退出码 23），
# 而 curl 单独跑是 0。这会让"服务健康"和"服务没起"随机混淆。
# 处置：把响应收进变量再匹配，不成了管道。
_probe() {  # $1=URL $2=期望出现的片段 $3=超时秒数
  local body
  body="$(curl -sf --connect-timeout 1 --max-time "${3:-3}" "$1" 2>/dev/null)" || return 1
  case "$body" in *"$2"*) return 0 ;; esac
  return 1
}

api_is_ours() {  # 后端探针：/healthz 的 service 字段
  _probe "http://127.0.0.1:$PORT/healthz" '"service"'
}

sim_is_ours() {  # 仿真器探针：/api/state 有 data_plane/frame 这几个键
  _probe "http://127.0.0.1:$SIM_PORT/api/state" '"data_plane"'
}

web_is_ours() {  # 前端探针：next dev 的页面里有 __next 标记
  # 超时给到 12s，不是统一的 3s。**next dev 在重编译时 `GET /` 实测 4642ms**，
  # 3s 会超时 → 探针返回假阴性 → status 把"正在编译"错报成"端口被别的程序占用"，
  # 而那句话会引导人去杀掉一个其实健康的进程。慢，但答案是对的。
  # 标记用 `/_next/`：页面里一定出现（`__NEXT_DATA__` 在新版 App Router 下不一定有）。
  _probe "http://127.0.0.1:$WEB_PORT/" "/_next/" 12
}

# 子进程还活着吗？用 `jobs -pr` 而不是 `kill -0`：
#   - `jobs -pr` 问的是"shell 手上还有没有正在跑的作业"，正是这里要判的语义；
#   - `kill -0` 判的是"信号能不能发出去"。子进程秒退后如果还没被回收，
#     这个判断会给出与"已退出"相反的答案，而且它的行为依赖 shell 对僵尸的
#     处理细节，不适合当唯一依据。
alive() {  # $1=pid（必须是本 shell 的子进程）
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
  echo "  停掉本脚本起的那套：bash tools/dev.sh stop"
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

pid_from_file() {  # $1=KEY
  [ -s "$PIDS_FILE" ] || return 0
  sed -n "s/^$1=//p" "$PIDS_FILE" | head -1
}

# ---- 前置检查 ---------------------------------------------------------------

preflight() {
  if [ ! -d web/node_modules ]; then
    echo "✗ web/node_modules 不存在，先装依赖：cd web && npm install"
    exit 1
  fi

  local p port rest name var
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
  local db
  db="$(ls data_dog.db data/data_dog.db 2>/dev/null | head -1)"
  if [ -n "$db" ] && [ -z "${DD_DATABASE_URL:-}" ]; then
    echo "  ⚠ 使用本机 SQLite：$db（可用 DD_DATABASE_URL 换库）"
    echo "    提示：若启动后长时间停在 'Waiting for application startup'，"
    echo "    多半是另一个 data_dog 实例正占着这个库的写锁。"
    echo "    换库重跑：DD_DATABASE_URL=sqlite:///./data_dog_dev.db bash tools/dev.sh"
  fi
  echo
}

# ---- 后台守护 ---------------------------------------------------------------
#
# 脱离当前会话的两种做法，按平台选：
#   Windows  Start-Process：新建独立进程，不挂在当前控制台上；
#   POSIX    setsid（没有就 nohup）。
# **必须传脚本文件路径，不能传 `-c "内联命令"`**：实测内联字符串会被上层
# 的引号解析吞掉，进程要么不启动、要么起来就退，而且是静默的。

spawn_detached() {
  local bash_bin bash_win self_win
  bash_bin="$(command -v bash 2>/dev/null || true)"
  [ -n "$bash_bin" ] || { echo "✗ 找不到 bash" >&2; return 1; }

  if [ "${OS:-}" = "Windows_NT" ] && command -v powershell >/dev/null 2>&1 \
     && command -v cygpath >/dev/null 2>&1; then
    bash_win="$(cygpath -w "$bash_bin")"
    self_win="$(cygpath -w "$SELF")"
    powershell -NoProfile -NonInteractive -Command \
      "Start-Process -FilePath '$bash_win' -ArgumentList '\"$self_win\"','__supervise' -WindowStyle Hidden" \
      >/dev/null 2>&1
    return $?
  fi

  if command -v setsid >/dev/null 2>&1; then
    setsid "$bash_bin" "$SELF" __supervise </dev/null >/dev/null 2>&1 &
  else
    nohup "$bash_bin" "$SELF" __supervise </dev/null >/dev/null 2>&1 &
  fi
  disown 2>/dev/null || true
  return 0
}

# 守护进程本体：持有三个子进程，把这些 pid 写进 dev.pids 供 stop/status 用。
supervise() {
  rm -f "$PIDS_FILE" "$FAIL_FILE" "$READY_FILE"
  : >"$DEV_LOG"
  # 落盘一律写 Windows pid：后续 stop/status 是另一次进程，拿不到 MSYS 命名空间。
  printf 'SUPERVISOR=%s\nPORTS=%s %s %s\n' \
    "$(to_winpid $$)" "$PORT" "$SIM_PORT" "$WEB_PORT" >"$PIDS_FILE"

  local API_PID="" SIM_PID="" WEB_PID=""

  sup_teardown() {
    trap - INT TERM EXIT
    # 这些 pid 是本进程亲手起的，不需要归属判断，直接杀树。
    for pid in "$WEB_PID" "$SIM_PID" "$API_PID"; do kill_tree "$pid"; done
    rm -f "$PIDS_FILE" "$READY_FILE"
  }
  trap sup_teardown INT TERM EXIT

  die() {  # $1=消息
    echo "✗ $1" >>"$DEV_LOG"
    printf '%s\n' "$1" >"$FAIL_FILE"
    exit 1
  }

  echo "→ 起后端 :$PORT（日志 $API_LOG）" >>"$DEV_LOG"
  "$PY" -m data_dog.service >"$API_LOG" 2>&1 &
  API_PID=$!
  echo "API=$(to_winpid "$API_PID")" >>"$PIDS_FILE"
  wait_for "后端" api_is_ours "$API_LOG" 90 "$API_PID" >>"$DEV_LOG" 2>&1 \
    || die "后端启动失败"
  echo "  ✓ 后端就绪" >>"$DEV_LOG"

  # `--publish-hz` 默认 10Hz：控制台页面够用。要压真链路再往上调，
  # 并同步调高下游限流（见 server.py 里的频率说明）。
  echo "→ 起仿真控制台 :$SIM_PORT（日志 $SIM_LOG）" >>"$DEV_LOG"
  "$PY" -m data_dog.go2_sim.server --port "$SIM_PORT" >"$SIM_LOG" 2>&1 &
  SIM_PID=$!
  echo "SIM=$(to_winpid "$SIM_PID")" >>"$PIDS_FILE"
  wait_for "仿真控制台" sim_is_ours "$SIM_LOG" 90 "$SIM_PID" >>"$DEV_LOG" 2>&1 \
    || die "仿真控制台启动失败"
  echo "  ✓ 仿真控制台就绪（MuJoCo 首帧较慢，之后正常）" >>"$DEV_LOG"

  # 在同端子 shell 里导出 DD_WEB_*，**不写进 web/.env.local**——
  # 那份文件的旧变量（NEXT_PUBLIC_API_KEY 等）已无人读取且会进浏览器包，
  # 不该再往里加东西，也不该在这里悄悄改用户的文件。
  echo "→ 起前端 :$WEB_PORT（日志 $WEB_LOG）" >>"$DEV_LOG"
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
  echo "WEB=$(to_winpid "$WEB_PID")" >>"$PIDS_FILE"
  wait_for "前端" web_is_ours "$WEB_LOG" 120 "$WEB_PID" >>"$DEV_LOG" 2>&1 \
    || die "前端启动失败"
  echo "  ✓ 前端就绪" >>"$DEV_LOG"

  : >"$READY_FILE"
  echo "✓ 三件套已就绪" >>"$DEV_LOG"

  # 守住三个子进程。任一退出就整栈收尾：半死的栈比全停更难查。
  local gone="" name="服务" rc=0 pid
  while :; do
    for pid in "$API_PID" "$SIM_PID" "$WEB_PID"; do
      if ! alive "$pid"; then gone="$pid"; break; fi
    done
    [ -n "$gone" ] && break
    sleep 2
  done

  local log="$API_LOG"
  [ "$gone" = "$SIM_PID" ] && { name="仿真控制台"; log="$SIM_LOG"; }
  [ "$gone" = "$WEB_PID" ] && { name="前端"; log="$WEB_LOG"; }
  wait "$gone" 2>/dev/null; rc=$?
  echo "✗ $name（pid $gone）已退出，exit=$rc；整栈随之收尾。" >>"$DEV_LOG"
  echo "  看它为什么挂：tail -50 $log" >>"$DEV_LOG"
  exit 1
}

# ---- 子命令 -----------------------------------------------------------------

# 启动后把控制台打开。用户要的是"一键"，不是"再复制一个地址去粘"。
#
# 三个刻意的决定：
# 1. **只开前端，不开后端和仿真。** 前端是给人看的那个；后端 /docs 是 Swagger，
#    仿真控制台是调试用的 —— 一次弹三个窗口是打扰，不是便利。
# 2. **失败不影响启动。** 没有图形界面（CI、SSH、服务账号）时打不开浏览器是正常的，
#    这时候只提示一行怎么手动打开，绝不因为"打不开浏览器"把已经起好的服务判失败。
# 3. **URL 单独占一行、前后不加中文。** 终端和编辑器靠正则识别链接，
#    夹在中文中间的地址识别不出来，粘不进浏览器。
open_console() {  # $1=端口
  local url="http://127.0.0.1:$1" rc=0
  case "${OS:-}$(uname -s 2>/dev/null)" in
    *Windows_NT*|*MINGW*|*MSYS*|*CYGWIN*)
      # explorer.exe 在 Windows 上最稳：cmd 的 `start` 会把 URL 里的 & 当分隔符，
      # powershell 的 Start-Process 在无 GUI 会话里可能挂住。explorer 立刻返回。
      command -v explorer.exe >/dev/null 2>&1 || rc=1
      [ "$rc" -eq 0 ] && { explorer.exe "$url" >/dev/null 2>&1 & }
      ;;
    Darwin*)
      command -v open >/dev/null 2>&1 || rc=1
      [ "$rc" -eq 0 ] && { open "$url" >/dev/null 2>&1 & }
      ;;
    *)
      command -v xdg-open >/dev/null 2>&1 || rc=1
      [ "$rc" -eq 0 ] && { xdg-open "$url" >/dev/null 2>&1 & }
      ;;
  esac
  if [ "$rc" -eq 0 ]; then
    echo "  → 已发起打开浏览器（没弹出来就点下面的链接）"
  else
    # 无图形界面（CI / SSH / 服务账号）很正常，不是错误。
    echo "  · 本机没有可用的浏览器启动器，点下面的链接打开"
  fi
}

cmd_start() {
  local follow=0
  case "${1:-}" in -f|--follow) follow=1 ;; esac

  # 全都在跑就直接说，别让人对着"端口被占用"猜。
  if api_is_ours && sim_is_ours && web_is_ours; then
    echo "✓ 三件套已经在跑："
    cmd_status
    exit 0
  fi

  echo "===== data_dog 本地调试环境 ====="
  echo
  preflight

  rm -f "$PIDS_FILE" "$FAIL_FILE" "$READY_FILE"

  echo "→ 拉起守护进程…"
  spawn_detached || { echo "✗ 无法创建后台进程"; exit 1; }

  local t0 sup_pid
  t0=$(( $(date +%s) + 20 ))
  while [ ! -s "$PIDS_FILE" ]; do
    if [ -s "$FAIL_FILE" ]; then
      echo "✗ 启动失败：$(cat "$FAIL_FILE")"; tail -40 "$DEV_LOG"; exit 1
    fi
    if [ "$(date +%s)" -ge "$t0" ]; then
      echo "✗ 守护进程 20s 内没有注册。日志尾部："; tail -40 "$DEV_LOG"; exit 1
    fi
    sleep 0.3
  done
  sup_pid="$(pid_from_file SUPERVISOR)"

  printf '  启动中'
  local deadline
  deadline=$(( $(date +%s) + 200 ))
  while :; do
    # 就绪标记是**空文件**，判断存在性（-e）不是非空（-s）——写 -s 会永远等不到。
    if [ -e "$READY_FILE" ]; then echo " ✓ 全部就绪"; break; fi
    if [ -s "$FAIL_FILE" ]; then
      echo; echo "✗ 启动失败：$(cat "$FAIL_FILE")"
      echo "--- 守护日志 ---"; tail -50 "$DEV_LOG"; exit 1
    fi
    if ! pid_alive "$sup_pid"; then
      echo; echo "✗ 守护进程意外退出。日志尾部："; tail -50 "$DEV_LOG"; exit 1
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      echo; echo "✗ 200s 内未就绪（守护进程还活着，是启动慢）。日志尾部："
      tail -50 "$DEV_LOG"; exit 1
    fi
    printf '.'
    sleep 1
  done

  open_console "$WEB_PORT"

  cat <<EOF

===== 三个入口 =====
  URL      http://127.0.0.1:$WEB_PORT
  控制台   http://127.0.0.1:$SIM_PORT
  文档     http://127.0.0.1:$PORT/docs
EOF
  printf '  状态     '; printf 'http://127.0.0.1:%s/healthz\n' "$PORT"
  cat <<EOF

  端口     $PORT（后端）  $SIM_PORT（仿真）  $WEB_PORT（前端）
  进程     后端 $(pid_from_file API) · 仿真 $(pid_from_file SIM) · 前端 $(pid_from_file WEB)

  后台跑着，关掉这个终端不影响。
  停：.\\dev.bat stop      状态：.\\dev.bat status
  日志：.dev-logs/{api,sim,web}.log

  想看后端那条完整链路（候选→调查→报告），先灌一条演示日志：
    bash tools/ingest_demo.sh       # 96°C 过温，应产出 critical 候选
EOF

  if [ "$follow" -eq 1 ]; then
    echo
    echo "→ 跟日志（Ctrl-C 只离开日志，服务继续跑）"
    tail -f "$API_LOG" "$SIM_LOG" "$WEB_LOG" 2>/dev/null
  fi
}

# 停一个服务：先过探针确认是我们自己的，再杀；探针不过就跳过。
stop_one() {  # $1=名称 $2=端口 $3=探针 $4=记录 pid
  local name="$1" port="$2" probe="$3" pid="${4:-}" listener
  if ! port_busy "$port"; then
    echo "  · $name（:$port）本来就没在跑"
    return 0
  fi
  if ! "$probe"; then
    echo "  ⚠ $name（:$port）被别的程序占着，探针不通过，跳过（不误杀）"
    echo "    查占用者：netstat -ano | grep \":$port\""
    return 0
  fi
  listener="$(listen_pid "$port")"
  if [ -n "$pid" ] && [ "$pid" != "$listener" ] && pid_alive "$pid"; then
    kill_tree "$pid"            # 记录的是整棵树的根，杀根能带走子进程
    echo "  ✓ $name（:$port，pid $pid）已停"
  elif [ -n "$listener" ]; then
    kill_tree "$listener"
    echo "  ✓ $name（:$port，pid $listener）已停"
  else
    echo "  ⚠ $name（:$port）探针通过但查不到监听进程，跳过"
    return 0
  fi
}

cmd_stop() {
  local api_pid sim_pid web_pid sup_pid p
  api_pid="$(pid_from_file API)"
  sim_pid="$(pid_from_file SIM)"
  web_pid="$(pid_from_file WEB)"
  sup_pid="$(pid_from_file SUPERVISOR)"

  echo "→ 停止三件套…"
  # 顺序：前端 → 仿真 → 后端。前端先停，免得它继续往后端打请求。
  stop_one "前端"       "$WEB_PORT" web_is_ours "$web_pid"
  stop_one "仿真控制台" "$SIM_PORT" sim_is_ours "$sim_pid"
  stop_one "后端"       "$PORT"     api_is_ours "$api_pid"

  if pid_alive "$sup_pid"; then
    kill_tree "$sup_pid"
  fi
  rm -f "$PIDS_FILE" "$READY_FILE" "$FAIL_FILE"

  # 等端口真的松手：taskkill 是异步的，"已停止"不能靠刚下完命令就说
  local waited=0 busy=""
  while [ "$waited" -lt 20 ]; do
    busy=""
    for p in "$PORT" "$SIM_PORT" "$WEB_PORT"; do
      port_busy "$p" && busy="$busy $p"
    done
    [ -z "$busy" ] && break
    sleep 0.5
    waited=$((waited + 1))
  done

  if [ -z "$busy" ]; then
    echo "✓ 端口 $PORT / $SIM_PORT / $WEB_PORT 均已释放"
  else
    echo "⚠ 这些端口仍未释放：$busy"
    echo "  可能不归本脚本管（stop 不误杀）。查占用者："
    for p in $busy; do echo "    netstat -ano | grep \":$p\""; done
    exit 1
  fi
}

cmd_status() {
  local rc=0 api_pid sim_pid web_pid sup_pid
  api_pid="$(pid_from_file API)"
  sim_pid="$(pid_from_file SIM)"
  web_pid="$(pid_from_file WEB)"
  sup_pid="$(pid_from_file SUPERVISOR)"

  echo "===== data_dog 本地调试环境 ====="
  status_row "后端 API  "        "$PORT"     api_is_ours "$api_pid" || rc=$?
  status_row "仿真控制台"        "$SIM_PORT" sim_is_ours "$sim_pid" || rc=$?
  status_row "前端控制台"        "$WEB_PORT" web_is_ours "$web_pid" || rc=$?
  echo
  if pid_alive "$sup_pid"; then
    echo "  守护进程      pid $sup_pid（PIDS 记录，退出后自动清）"
  else
    echo "  守护进程      · 没有守护进程（服务可能是手动起的，或已停）"
  fi
  exit "$rc"
}

status_row() {  # $1=名称 $2=端口 $3=探针 $4=pid
  local name="$1" port="$2" probe="$3" pid="${4:-}"
  if "$probe" >/dev/null 2>&1; then
    [ -n "$pid" ] || pid="$(listen_pid "$port")"
    printf '  %s（:%s）  ✓ 运行中   pid %s\n' "$name" "$port" "${pid:-?}"
    return 0
  fi
  if port_busy "$port"; then
    printf '  %s（:%s）  ⚠ 端口被别的程序占用\n' "$name" "$port"
    return 2
  fi
  printf '  %s（:%s）  · 未运行\n' "$name" "$port"
  return 1
}

cmd_restart() {
  cmd_stop
  echo
  cmd_start "$@"
}

cmd_logs() {
  echo "→ 跟日志（Ctrl-C 退出；服务继续后台跑）"
  tail -f "$API_LOG" "$SIM_LOG" "$WEB_LOG" 2>/dev/null
}

usage() {
  sed -n '4,11p' "$SELF" | sed 's/^# \{0,1\}//'
}

# ---- 分发 -------------------------------------------------------------------

case "${1:-start}" in
  __supervise) supervise ;;
  start)   shift; cmd_start "$@" ;;
  stop)    cmd_stop ;;
  status)  cmd_status ;;
  restart) shift; cmd_restart "$@" ;;
  logs)    cmd_logs ;;
  -h|--help|help) usage ;;
  *)
    echo "✗ 不认识的子命令：$1"
    echo
    usage
    exit 2
    ;;
esac
