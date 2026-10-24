#!/usr/bin/env bash
# ============================================================================
# data_dog：把 GitHub Actions 的 9 条 job 在**本机 Linux 容器**里按 CI 原命令跑一遍
#
# 为什么要有它：CI 自 2026-09-28 起全红（GitHub 账户扣费，不是代码问题），
# 而“我们在本地验过”如果没有可复现的脚本 + 日志，就只是口头保证。
# 这个脚本的目标是：**读者只需 Docker，不需要 GitHub 账户**，就能得到与 CI 同口径的结论。
#
# 用法：
#   bash scripts/verify_ci_offline.sh                 # 跑全部 9 条腿
#   bash scripts/verify_ci_offline.sh lint helm web    # 只跑指定腿
#   OUT=/tmp/xx bash scripts/verify_ci_offline.sh      # 指定输出目录
#
# 产物（$OUT，默认 artifacts/ci-offline/<时间戳>/）：
#   <leg>.log       每条腿的完整 stdout+stderr
#   rc.tsv          leg<TAB>退出码<TAB>耗时秒
#   manifest.txt    环境与版本快照（镜像 id、helm/kubeconform/python/node 版本、commit、git status）
#   SUMMARY.md      给人看的结论表（自动生成）
#
# 与 CI 的差异（诚实声明，也由 run 时的 manifest 记录）：
#   * 镜像不是 ubuntu-latest 而是 python:3.12-slim / node:20-bookworm-slim（同为 glibc/Debian），
#     所以依赖轮子、mypy 平台判定、Playwright 依赖链与 CI 一致；但**内核/系统库版本**不保证逐字节相同。
#   * CI 的服务容器由 GitHub 托管（service containers），这里用 docker 网络里的同版本镜像代替。
#   * 无 GitHub Actions 缓存（pip/npm 每次都重新装），因此耗时长于 CI，但不影响判定。
#   * 源码不是跑在宿主机绑定挂载上：每条腿先把仓库顺序拷进容器本地盘再跑。
#     原因：Docker Desktop 把 D:\... 以 9p/gRPC-FUSE 挂进容器，冷启动与随机读写慢一个量级。
#     实测后果是 spawn 子进程卡在 state=D / wchan=p9_client_rpc，30s ready 握手超时，
#     6 条沙箱用例在“全量套件”里假红、单文件跑却全绿。拷贝后 I/O 行为与 runner 同构。
#     调试时可用 DD_CI_NO_COPY=1 退回直挂。
# ============================================================================
set -uo pipefail

# git-bash（MSYS2）会把传给 native 程序的 `/xxx` 参数改写成 `C:/Program Files/Git/xxx`，
# 后果是 `-w /work` 变成 `C:/Program Files/Git/work` → docker 直接 rc=125 拒绝，
# 而且每条腿只花 1–2 秒就“失败”，极易被误读成测试挂了（本仓真踩过：9 条腿全 125）。
# 关掉转换后，宿主路径改用 `cygpath -m` 给的 Windows 风格（`D:/data_dog`）。
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if command -v cygpath >/dev/null 2>&1; then
  REPO_DOCKER=$(cygpath -m "$REPO")      # /d/data_dog -> D:/data_dog
else
  REPO_DOCKER=$REPO
fi
OUT=${OUT:-$REPO/artifacts/ci-offline/$(date +%Y%m%d-%H%M%S)}
PY_IMG=${PY_IMG:-dd-ci-python:3.12}
NODE_IMG=${NODE_IMG:-dd-ci-node:22}
E2E_IMG=${E2E_IMG:-dd-ci-e2e:latest}
NET=dd-ci-offline-net
PG_CT=dd-ci-offline-pg
REDIS_CT=dd-ci-offline-redis
PW_VOL=dd-ci-offline-pw
NM_VOL=dd-ci-offline-web-nm
LEG_TIMEOUT=${LEG_TIMEOUT:-3600}

# --- 非 root 腿（DD_CI_RUN_USER=65534）--------------------------------------
# 为什么必须有这条腿：root 带 CAP_SYS_RESOURCE，会绕过 RLIMIT_NPROC / ptrace /
# 一部分 /proc 可写性检查，于是「root 容器绿」**不能**证明「CI 绿」——
# GitHub runner 以普通用户 runner 执行。2026-10-23 实测同镜像同代码仅换 uid：
#   root      → pytest tests/test_sandbox.py: 16 passed
#   --user 65534 → 6 failed, 10 passed（全是 RuntimeError: can't start new thread）
# 这条腿存在的意义就是把那类缺陷在本地就抓住。
DOCKER_USER_ARGS=()
EXTRA_DOCKER=()
if [ -n "${DD_CI_RUN_USER:-}" ]; then
  DOCKER_USER_ARGS=(--user "$DD_CI_RUN_USER")
  # 不再挂 tmpfs 到 /work（**实测反向效果**）：Docker 会把镜像里的 /work/web（root:755）
  # 一并复制进 tmpfs，非 root 腿既写不进去、又因 sticky 位删不掉，web/web-e2e/e2e-real
  # 三腿集体假红（`tar: web/tests: Cannot mkdir: Permission denied`）。
  # 腿内拷贝本来就只几十 MB、容器 --rm 就清，容器本地盘完全够用。
  EXTRA_DOCKER=(-e HOME=/tmp -e PYTEST_ADDOPTS="-p no:cacheprovider")
fi

# --- 每条腿的拷贝前置段：把只读挂载的源码铺到容器本地盘 -----------------------
# GitHub runner 的 job 跑在本地盘；本机 Docker Desktop 的 9p 挂载慢一个量级，会把
# “冷的全新解释器启动”拖过 30s ready 握手超时。顺序拷一次（几十秒），换整条腿的 I/O 同构。
#
# 为什么不是 `tar .`：工作树里有两类东西**不能**进验证——
#   1) 上一轮 root 容器留下的缓存（`.hypothesis/`、`.pytest_cache/` 是 root:700），
#      非 root 腿一读就 Permission denied（实测 4 条腿集体 rc=2）；
#   2) 被 .gitignore 的草稿（如 `/data/*.py` 压测脚本）——GitHub CI 全新 clone 根本看不到它们，
#      拿它们判红就是假红（实测 7 条 ruff 错误全来自 data/）。
# 所以文件集定义必须与 CI **同源**：已跟踪文件 + 未忽略的新文件，仅此而已。
COPY_PRELUDE=$(cat <<'EOS'
set -e
# 非 root 腿（uid 65534）必须有可写的 HOME：pip/npm/mypy 默认把缓存写进 /root/.cache，
# 容器里 /root 不可写 → EACCES → 假红。指向 /tmp 比 chown /root 更贴近真实 runner。
export HOME=/tmp XDG_CACHE_HOME=/tmp/.cache npm_config_cache=/tmp/.npm
export PYTHONPYCACHEPREFIX=/tmp/.pycache
mkdir -p /work
# 镜像里的 /work 可能残留 root 拥有的子目录（node 镜像有 /work/web），
# 非 root 腿里它们既写不进去也删不掉子孙，所以先把树清干净再铺源码。
# 安全前提：本前置段只在“拷贝模式”下执行；直挂调试模式（DD_CI_NO_COPY=1）不跑它。
rm -rf /work/* 2>/dev/null || true
if [ -d /src/.git ]; then
  # `-c safe.directory=/src` 必需：非 root 腿（uid 65534）访问 root 拥有的 /src 时，
  # git 会以 “detected dubious ownership” 直接死（rc=128），
  # 表现是**9 条腿各 1-2 秒全部红**——看起来像代码全崩，实际是环境权限。
  # 不用 `git config --global`：那会把信任写进容器外镜像层的用户配置里，副作用跨腿传播。
  git -c safe.directory=/src -C /src ls-files -z > /tmp/filelist
  git -c safe.directory=/src -C /src ls-files -o --exclude-standard -z >> /tmp/filelist
  tar -C /src --null -T /tmp/filelist -cf - | tar -C /work -xf -
else
  # 无 .git（例如从归档包里验）：退化成排除法，至少挡住缓存目录
  tar -C /src -cf - --exclude=.git --exclude=.pi --exclude=artifacts \
    --exclude=.venv --exclude=node_modules --exclude=.next \
    --exclude=.hypothesis --exclude=.pytest_cache --exclude=.mypy_cache \
    --exclude=.ruff_cache . | tar -C /work -xf -
fi
EOS
)

ALL_LEGS="lint test postgres redis supply-chain helm web web-e2e web-e2e-real-backend"
# 默认只跑 4 条腿：这 4 条覆盖了 CI 红过的全部失败模式（#103 平台差、#106/#107 chart、
# 前端 109 单测 + build）。
# 另 5 条（两个服务集成腿、供应链、两条 E2E）耗时/内存都重得多（Playwright 要下 Chromium，
# 两条 E2E 各起一套 Next 构建 + 浏览器），需要时显式点名跑：
#   bash scripts/verify_ci_offline.sh postgres redis supply-chain web-e2e web-e2e-real-backend
# 这样默认路径占内存 ≈ 一个容器（顺序执行，永不并行）。
DEFAULT_LEGS="lint test helm web"
# `all` 是包错：展开成 ALL_LEGS（单一真源，不把 9 条腿的名单再拄一份到 Makefile/文档里）。
if [ "${1:-}" = "all" ]; then
  LEGS=$ALL_LEGS
else
  LEGS=${*:-$DEFAULT_LEGS}
fi

mkdir -p "$OUT"
# 非 root 腿要把覆盖率 json / 日志回传到 /artifacts，而它属于容器外的 root——放开写。
if [ -n "${DD_CI_RUN_USER:-}" ]; then chmod -R 777 "$OUT" 2>/dev/null || true; fi
RC_FILE="$OUT/rc.tsv"
: > "$RC_FILE"

log() { printf '%s\n' "$*" >&2; }

# --- 镜像自举：缺镜像就构建，建不成直接中止（rc=9）--------------------------
# 为何不用“让 docker run 报错算了”：docker run 对缺镜像只返回 rc=125 和一句
# `Unable to find image ... locally`，与“腿里代码挂了”在 SUMMARY 里长得差不多，
# 很容易被当成假红收尾（2026-10-24 实际发生过一次：web 腿 4 秒 rc=125）。
# 现在要么镜像就位，要么构建日志留下且总 rc=9 —— 没有第三种可能。
ensure_image() { # tag dockerfile
  local tag=$1 df=$2 want have
  want=$(image_inputs_hash "$df")
  have=$(docker image inspect --format '{{index .Config.Labels "dd.ci.inputs"}}' "$tag" 2>/dev/null || true)
  if [ "$want" = "$have" ] && [ -n "$want" ]; then return 0; fi
  if docker image inspect "$tag" >/dev/null 2>&1; then
    log "!! 镜像 $tag 已过期（Dockerfile/依赖清单变了）→ 重建"
  else
    log "!! 镜像缺失：$tag → 从 $df 构建（首次需下载依赖，失败会直接中止）"
  fi
  # 上下文必须用 `.` 并且先 cd 进去：Windows 版 docker 不认 Git Bash 的 /d/... 路径
  # （实测报错：unable to prepare context: path "/d/data_dog" not found）。
  # 两段式：BuildKit 快但有缓存坑；失败时回退经典构建器（直接吃本地已有的基镜像，
  # 不需要向 registry 拉 manifest）。2026-10-24 实测：BuildKit 报
  # `dialing registry-1.docker.io:443 ... failed` 时基镜像其实就在本地，
  # 回退后构建照常进行——否则一次网络抖动就变成“验证失败”。
  if ! ( cd "$REPO" && DOCKER_BUILDKIT=1 docker build -f "$df" \
         --label "dd.ci.inputs=$want" -t "$tag" . ) >> "$OUT/images.log" 2>&1; then
    log "!! BuildKit 构建失败，回退经典构建器（不拉 manifest，用本地基镜像）"
    if ! ( cd "$REPO" && DOCKER_BUILDKIT=0 docker build -f "$df" \
           --label "dd.ci.inputs=$want" -t "$tag" . ) >> "$OUT/images.log" 2>&1; then
      log "!! 镜像构建失败：$tag（详见 $OUT/images.log）"
      exit 9
    fi
  fi
}

# 镜像新鲜度的**唯一判据**是输入指纹（Dockerfile + 依赖清单），不是“镜像存在”。
# 否则改了 Dockerfile 却复用旧镜像，腿跑的是旧依赖，而 SUMMARY 看不出来。
image_inputs_hash() { # dockerfile -> 短指纹
  local df=$1 extra="" base="" base_label=""
  case "$df" in
    *Dockerfile.python) extra="pyproject.toml requirements.txt" ;;
    *Dockerfile.node)   extra="web/package.json web/package-lock.json" ;;
    *Dockerfile.e2e)    extra="pyproject.toml web/package.json web/package-lock.json" ;;
  esac
  # 派生镜像必须把**基镜像的指纹**也算进去：e2e 是从 dd-ci-node 建的，
  # 基镜像的 Dockerfile 变了而 e2e 的没变时，旧 e2e 镜像会继续被用——实测就踩过：
  # 基镜像已把 /work 改 1777，e2e 镜像里还是 root:755，非 root 腿直接假红。
  base=$(sed -n 's/^FROM[[:space:]]\+\([^[:space:]]*\).*/\1/p' "$df" 2>/dev/null | head -1)
  if [ -n "$base" ]; then
    base_label=$(docker image inspect --format '{{index .Config.Labels "dd.ci.inputs"}}' "$base" 2>/dev/null || true)
  fi
  ( cd "$REPO" && { cat "$df" $extra 2>/dev/null; printf '\nBASE=%s\n' "${base_label:-$base}"; } | sha256sum | cut -c1-16 )
}

ensure_image "$PY_IMG"  scripts/ci_offline/Dockerfile.python
ensure_image "$NODE_IMG" scripts/ci_offline/Dockerfile.node
for _leg in $LEGS; do
  case "$_leg" in
    web-e2e-real-backend) ensure_image "$E2E_IMG" scripts/ci_offline/Dockerfile.e2e ;;
  esac
done

cleanup() {
  docker rm -f "$PG_CT" "$REDIS_CT" >/dev/null 2>&1
  docker network rm "$NET" >/dev/null 2>&1
}
trap cleanup EXIT

# --- 统一的腿执行器：docker run + 超时 + rc/耗时记账 -------------------------
# 用 docker run 而不是 docker compose：CI 的 job 天然是一条独立进程链，
# 这里保持同样的“每条腿一个干净容器”，避免上一条腿的副作用污染下一条。
run_docker() {
  local leg=$1 img=$2 workdir=$3 script=$4
  shift 4
  local logf="$OUT/$leg.log" start end rc
  start=$(date +%s)
  log ">>> [$leg] docker run $img"
  {
    echo "# leg=$leg  image=$img  workdir=$workdir  $(date -Is)"
    echo "# script: $script"
    echo "# extra docker args: $*"
    echo
  } > "$logf"
  local src_args prelude
  if [ "${DD_CI_NO_COPY:-0}" = "1" ]; then
    src_args=(-v "$REPO_DOCKER:/work")      # 直挂：仅调试用，交付不要用它
    prelude=":"
  else
    src_args=(-v "$REPO_DOCKER:/src:ro")    # 只读挂载 + 腿内拷贝，见顶部差异声明
    prelude=$COPY_PRELUDE
  fi
  # 证据回传：拷贝模式下 /work 是容器本地盘，--rm 后腿内产物会一起消失。
  # 所以把宿主产物目录挂到 /artifacts，跑完把可查证据（覆盖率 json / 回流日志）拷出来。
  # 注意：腿脚本自带 `set -e`，cov_gate 一失败就直接退出、跑不到 cp；
  # 所以先 `set +e` 并把脚本放进子 shell：先记 rc → 回传证据 → 再按 rc 退出。
  timeout -k 10 "$LEG_TIMEOUT" docker run --rm \
    "${DOCKER_USER_ARGS[@]}" "${EXTRA_DOCKER[@]}" \
    "${src_args[@]}" -v "$OUT:/artifacts" \
    "$@" "$img" bash -lc "$prelude
# 不用 -w $workdir：目录不存在时 Docker 会以 **root:755** 创建它，而 /work 的 tmpfs
# 带 sticky 位，非 root 腿既写不进去也删不掉（实测 9 腿中 web/web-e2e/e2e-real 三腿假红）。
# 容器内注释里绝对不能出现反引号：这段是双引号字符串，反引号会被当命令替换执行。
# 改由容器用户自己建目录，属主就对上了。
mkdir -p $workdir
cd $workdir
set +e
( $script )
rc=\$?
cp -a /work/coverage*.json /work/*.log /artifacts/ 2>/dev/null || true
exit \$rc" >>"$logf" 2>&1
  rc=$?
  end=$(date +%s)
  printf '%s\t%s\t%s\n' "$leg" "$rc" "$((end - start))" >> "$RC_FILE"
  # 把“容器根本没起来”与“容器里测试红了”分开报——两者 rc 不同，但日志里长得很像。
  # rc=125 实测出现过两次：一次镜像名写错，一次腿体注释里的引号把命令切碎（见顶部说明）。
  case "$rc" in
    125) log "<<< [$leg] FAIL rc=125 —— docker 拒绝启动（不是测试红）：查镜像名与腿体内的引号/参数" ;;
    124|137) log "<<< [$leg] FAIL rc=$rc —— 腿超时被 timeout 强杀（LEG_TIMEOUT=${LEG_TIMEOUT}s）" ;;
    *) : ;;
  esac
  if [ "$rc" -eq 0 ]; then log "<<< [$leg] OK ($((end - start))s)"; else log "<<< [$leg] FAIL rc=$rc ($((end - start))s)"; fi
  return "$rc"
}

# --- 服务容器（对齐 ci.yml 的 services:）-----------------------------------
start_pg() {
  docker network create "$NET" >/dev/null 2>&1 || true
  docker rm -f "$PG_CT" >/dev/null 2>&1
  docker run -d --name "$PG_CT" --network "$NET" --network-alias pg \
    -e POSTGRES_USER=data_dog -e POSTGRES_PASSWORD=data_dog_ci_pw -e POSTGRES_DB=data_dog_test \
    postgres:16-alpine >/dev/null
  for _ in $(seq 1 40); do
    docker exec "$PG_CT" pg_isready -U data_dog >/dev/null 2>&1 && return 0
    sleep 1
  done
  log "!! postgres 未就绪"; return 1
}

start_redis() {
  docker network create "$NET" >/dev/null 2>&1 || true
  docker rm -f "$REDIS_CT" >/dev/null 2>&1
  docker run -d --name "$REDIS_CT" --network "$NET" --network-alias redis redis:7-alpine >/dev/null
  for _ in $(seq 1 30); do
    docker exec "$REDIS_CT" redis-cli ping 2>/dev/null | grep -q PONG && return 0
    sleep 1
  done
  log "!! redis 未就绪"; return 1
}

# 依赖已在镜像里（见 scripts/ci_offline/Dockerfile.python）；腿内只做**纯本地**的
# 可编辑安装：验证「当前工作树的包能按 pyproject 装上」，不再碰公网。
# --no-deps：依赖已预装（否则会重新解析/下载）；--no-build-isolation：用镜像里的 setuptools。
install_py='python -m pip install -e . --no-deps --no-build-isolation -q'

# ===========================================================================
for leg in $LEGS; do
  case "$leg" in
    # -------- 1. lint：ruff + mypy（+ 两个漂移守卫 + 元数据测试）----------
    lint)
      run_docker lint "$PY_IMG" /work "
        set -e
        $install_py
        echo '=== ruff check .';            ruff check .
        echo '=== mypy src';                mypy src
        echo '=== 依赖清单漂移';             python scripts/gen_requirements.py --check
        echo '=== README 规模数字漂移';       python scripts/readme_counts.py --check
        echo '=== 元数据一致性测试';          pytest -q tests/test_project_meta.py
      "
      ;;

    # -------- 2. test：全量（SQLite）+ 覆盖率门禁 --------------------------
    test)
      run_docker test "$PY_IMG" /work "
        set -e
        $install_py
        pytest -q --cov=data_dog --cov-report=term-missing --cov-report=json:coverage.json \\
               --cov-fail-under=85
        python scripts/cov_gate.py --json coverage.json --min 70
      "
      ;;

    # -------- 3. postgres：真 PG 集成 + PG-only 覆盖率 ---------------------
    postgres)
      start_pg || { printf 'postgres\t9\t0\n' >> "$RC_FILE"; continue; }
      run_docker postgres "$PY_IMG" /work "
        set -e
        $install_py
        pytest -q -m postgres --cov=data_dog --cov-report=term-missing \\
               --cov-report=json:coverage-pg.json
        # PG-only 覆盖率分两档，**与 ci.yml 逐字对齐**（单一口径原则）。
        # 为什么不能自己加严：这里曾经多写一道 --only …pg_locks.py --min 68，而 ci.yml
        # 并不卡 pg_locks——于是验证台在 2026-10-25 实测 67.4% 时变红，而同一提交的真 CI 是绿的。
        # 那种「本机红 / CI 绿」比假绿更贵：它会让人开始不信验证台，然后绕过它。
        # 注意：这段正文在双引号字符串里——不能出现未转义的双引号、反引号、美元括号，
        # 否则会被 shell 提前闭合或当真命令执行（本行注释就是为了记录踩过的坑）。
        # pg_locks 在 PG 腿里的未覆盖行主要是「非 PG 分支直接 return True」这类
        # （PG 腿里根本跑不到）与 wait=False 快路径 → 口径不适用，记录在
        # scripts/cov_gate.py 的 ALLOW 里，不作为棘轮。
        python scripts/cov_gate.py --json coverage-pg.json \\
          --only storage/partitioning.py --no-allow --min 75
        python scripts/cov_gate.py --json coverage-pg.json \\
          --only state/pg.py --no-allow --min 80
      " --network "$NET" -e DD_TEST_PG_URL='postgresql+psycopg://data_dog:data_dog_ci_pw@pg:5432/data_dog_test'
      ;;

    # -------- 4. redis：真 Redis 契约 --------------------------------------
    redis)
      start_redis || { printf 'redis\t9\t0\n' >> "$RC_FILE"; continue; }
      run_docker redis "$PY_IMG" /work "
        set -e
        $install_py
        pytest -q -m redis
      " --network "$NET" -e DD_TEST_REDIS_URL='redis://redis:6379/0'
      ;;

    # -------- 5. supply-chain：pip-audit + SBOM ----------------------------
    supply-chain)
      run_docker supply-chain "$PY_IMG" /work "
        set -e
        $install_py
        # pip-audit 本身要联网查漏洞库（这是这门工具的固有性质，无法离线）；
        # 依赖已经在镜像里，所以不会再出现 pip 解析超时那种假红。
        echo '=== pip-audit（告警不拦）'
        pip-audit -f json -o pip-audit.json || true
        python -c \"import json;d=json.load(open('pip-audit.json'));print('vulns:',len(d.get('dependencies') or []),'deps scanned')\" || true
        echo '=== SBOM'
        mkdir -p dist
        cyclonedx-py environment -o dist/sbom.json --output-format json \\
          || python -c \"from data_dog.operator_factory.sbom import generate_sbom; generate_sbom(out_path='dist/sbom.json')\"
        python -c \"import json;d=json.load(open('dist/sbom.json'));print('sbom components:',len(d.get('components') or []))\"
      "
      ;;

    # -------- 6. helm：lint + 不变量 + kubeconform -------------------------
    helm)
      run_docker helm "$PY_IMG" /work "
        set -e
        pip install --quiet pyyaml
        echo '=== helm lint'; helm lint helm/data-dog
        echo '=== 渲染不变量'; python scripts/verify_chart.py
        # schema 收在 helm/schemas/：kubeconform 的 -ignore-missing-schemas 只容忍 404，
        # 不容忍连接失败（实测离线 EOF -> Errors: 6 -> 假红，而 chart 本身没错）。
        # -skip 的两个 CRD 上游没有 schema，它们的正确性由 verify_chart.py 的不变量断言覆盖；
        # skip 列表与 scripts/verify_schemas.py 的 SKIP_KINDS 必须一致（否则同一次运行两种口径）。
        # 注意：这段是双引号字符串，注释里不能出现反引号（会被当命令替换执行），
        # 也不能用行尾反斜杠续行（不会被消化，会变成“命令不存在”）。
        echo '=== schema 覆盖检查（默认 + 生产 values）'
        helm template dd helm/data-dog | python scripts/verify_schemas.py
        helm template dd helm/data-dog -f helm/data-dog/values.prod.yaml | python scripts/verify_schemas.py
        echo '=== kubeconform 默认 values'
        helm template dd helm/data-dog | kubeconform -strict -summary -ignore-missing-schemas -skip PrometheusRule,ServiceMonitor -schema-location '/work/helm/schemas/{{.ResourceKind}}{{.KindSuffix}}.json' | tee /tmp/kc-default.txt
        python scripts/assert_kc_summary.py 0 < /tmp/kc-default.txt
        echo '=== kubeconform 生产 values'
        helm template dd helm/data-dog -f helm/data-dog/values.prod.yaml | kubeconform -strict -summary -ignore-missing-schemas -skip PrometheusRule,ServiceMonitor -schema-location '/work/helm/schemas/{{.ResourceKind}}{{.KindSuffix}}.json' | tee /tmp/kc-prod.txt
        python scripts/assert_kc_summary.py 2 < /tmp/kc-prod.txt
      "
      ;;

    # -------- 7. web：typecheck + lint + 单测 ------------------------------
    web)
      run_docker web "$NODE_IMG" /work/web "
        set -e
        # 依赖由镜像供给（构建期 npm ci），腿内不打公网。
        cp -a /opt/web-deps/node_modules /work/web/node_modules
        echo '=== typecheck'; npm run typecheck
        echo '=== lint';      npm run lint
        echo '=== test';      npm test
      "
      ;;

    # -------- 8. web-e2e：真 Chromium（devmock 上游）----------------------
    web-e2e)
      run_docker web-e2e "$NODE_IMG" /work/web "
        set -e
        # 依赖与 Chromium 都已在镜像里，腿内不打公网。
        cp -a /opt/web-deps/node_modules /work/web/node_modules
        npm run e2e
      "
      ;;

    # -------- 9. web-e2e-real-backend：浏览器 → Next 代理 → 真 Python -----
    web-e2e-real-backend)
      # 镜像 dd-ci-e2e 已同时烘好 node 依赖、Chromium 与 python 依赖（见 Dockerfile.e2e）。
      run_docker web-e2e-real-backend "$E2E_IMG" /work "
        set -e
        cp -a /opt/web-deps/node_modules /work/web/node_modules
        cd web
        E2E_REAL_BACKEND=1 E2E_REAL_PYTHON=/venv/bin/python npm run e2e
      "
      ;;

    *)
      log "!! 未知腿：$leg（可用：$ALL_LEGS）"
      printf '%s\t9\t0\n' "$leg" >> "$RC_FILE"
      ;;
  esac
done

# ===========================================================================
# manifest + SUMMARY
# ===========================================================================
{
  echo "# data_dog CI 离线复现快照"
  echo "生成时间: $(date -Is)"
  echo "主机: $(uname -srm)"
  echo "commit: $(cd "$REPO" && git rev-parse HEAD)"
  echo "branch: $(cd "$REPO" && git rev-parse --abbrev-ref HEAD)"
  echo "工作树: $(cd "$REPO" && git status --porcelain | wc -l) 个改动"
  echo
  echo "## 容器与镜像"
  docker version --format 'docker server {{.Server.Version}} {{.Server.Os}}/{{.Server.Arch}}' 2>/dev/null
  for i in "$PY_IMG" "$NODE_IMG"; do
    echo "$i -> $(docker image inspect "$i" --format '{{.Id}}' 2>/dev/null | cut -c1-19)"
  done
  echo "postgres:16-alpine -> $(docker image inspect postgres:16-alpine --format '{{index .RepoDigests 0}}' 2>/dev/null)"
  echo "redis:7-alpine     -> $(docker image inspect redis:7-alpine --format '{{index .RepoDigests 0}}' 2>/dev/null)"
  echo
  echo "## 工具版本（容器内）"
  docker run --rm "$PY_IMG" bash -lc 'python -V; helm version --short; kubeconform -v' 2>/dev/null
  docker run --rm "$NODE_IMG" bash -lc 'node -v; npm -v' 2>/dev/null
} > "$OUT/manifest.txt" 2>&1

{
  echo "# CI 离线复现结论（$(date -Is)）"
  echo
  echo "commit \`$(cd "$REPO" && git rev-parse --short HEAD)\`（$(cd "$REPO" && git rev-parse --abbrev-ref HEAD)）"
  # 证据完整性：腿是从挂载的工作区取源的，不是从 commit。
  # 工作区脏时必须**明写**，否则这份报告会被当成“某个 commit 的体检结果”去引用。
  dirty=$(cd "$REPO" && git status --porcelain | wc -l | tr -d ' ')
  if [ "$dirty" = "0" ]; then
    echo
    echo "工作区：**clean**（结果对应上面的 commit）"
  else
    echo
    echo "> ⚠️ **工作区 DIRTY：$dirty 个未提交改动**。腿从挂载目录取源，"
    echo "> 所以本结果**不对应任何 commit**，不能当作提交/发布的体检证据。"
    echo "> 提交后重跑：\`git stash && bash scripts/verify_ci_offline.sh all\`。"
  fi
  echo
  echo "| 腿 | 退出码 | 耗时 | 结论 |"
  echo "|---|---|---|---|"
  fails=0
  while IFS=$'\t' read -r leg rc secs; do
    [ -z "${leg:-}" ] && continue
    if [ "$rc" = "0" ]; then mark="✅ 通过"; else mark="❌ 红（见 $leg.log）"; fails=$((fails + 1)); fi
    echo "| $leg | $rc | ${secs}s | $mark |"
  done < "$RC_FILE"
  echo
  ran=$(wc -l < "$RC_FILE")
  total=$(printf '%s\n' $ALL_LEGS | wc -l)
  if [ "$fails" -eq 0 ]; then
    echo "**本轮 $ran/$total 条腿全部通过。**"
  else
    echo "**$fails 条腿红（本轮 $ran/$total）。**"
  fi
  # 跑的是子集就说清楚，否则“全部通过”会被读成“9 条都绿”。
  if [ "$ran" -lt "$total" ]; then
    missing=$(TAB=$(printf '\t'); for l in $ALL_LEGS; do grep -q "^${l}${TAB}" "$RC_FILE" || printf '%s ' "$l"; done)
    echo "未覆盖：${missing}（证据包会把它标成 PARTIAL，不得表述为已通过）"
  fi
  echo
  echo "复现：\`bash scripts/verify_ci_offline.sh\`（只需 Docker；镜像由 scripts/ci_offline/Dockerfile.* 构建）"
} > "$OUT/SUMMARY.md"

log ""
log "产物目录：$OUT"
cat "$OUT/SUMMARY.md" >&2
fails=$(awk -F'\t' '$2 != 0' "$RC_FILE" | wc -l)
[ "$fails" -eq 0 ] || exit 1
