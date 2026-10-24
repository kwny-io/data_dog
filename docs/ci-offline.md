# CI 的离线等价验证（不需要 GitHub 账户）

## 为什么要有这份东西

CI 自 2026-09-28 起 9 条 job 全红，原因是 **GitHub 账户扣费/额度**，不是代码。

> **2026-10-23 更新（已恢复）**：仓库转 public 后 Actions 恢复，run
> [39957101297](https://github.com/kwny-io/data_dog/actions/runs/39957101297)
> 在 10 分钟内 9 条 job 全部真实启动（对照旧 run 的 4 秒瞬失败），
> 其中 7 条直接绿。这条 run 本身就是「红是 billing 不是代码」的硬证据，
> 但它同样是**反例教材**：它跑在旧提交上，且以 GitHub runner 的普通用户执行，
> 于是暴露了两条本地 root 容器永远测不出的真缺陷——
> `RLIMIT_NPROC=(0,0)` 令 worker 的线程起不来（root 有 `CAP_SYS_RESOURCE` 绕过），
> 以及 `test_project_meta` 的文档数字漂移守卫。**root 容器的绿不具证明力。**
> 下面的离线等价验证因此新增第三条腿：**非 root（uid 65534）**。
但"我们在本地验过了"如果没有 **可复现的脚本 + 可归档的日志**，它就是一句口头保证——
下一次谁都可以合理地问："你说验过，怎么验的？" 而且 `#103` 恰好证明了另一件事：
**Windows 上绿不等于 Linux 上绿**（`ctypes.WinDLL` 这类平台条件属性只有 Linux 的 mypy 才报）。

所以这里给出的不是结论，是**方法**：把 CI 的每条 job 用完全相同的命令，在
(a) 本机 Linux 容器、(b) Windows 原生 两种环境下各跑一遍，把 stdout/退出码/版本快照落盘。

## 三条腿（环境）各自证明什么

| 环境 | 脚本 | 证明的问题 |
|---|---|---|
| Linux 容器（python:3.12-slim / node:20-bookworm-slim） | `scripts/verify_ci_offline.sh` | "在 Linux + 3.12/Node20 上绿"——与 CI 的**平台判定**一致（glibc、mypy 平台条件、Playwright 依赖链） |
| Windows 原生（.venv + 本机 helm/node） | `scripts/verify_ci_native_windows.sh` | "在开发者机器上绿"——与 CI 的**平台判定**不同，所以两边都跑才有意义 |
| **非 root 容器（uid 65534）** | `diag/probe_nonroot.sh` | **"以普通用户绿"**——与 root 容器是两个不同环境。`RLIMIT_NPROC`、`ptrace`、`/proc` 可写范围等均依赖 uid 与 capabilities，root 会绕过它们（GitHub runner 以 `runner` 普通用户跑） |

## 怎么复现

```bash
# 一次性准备镜像（Docker 即可，无需 GitHub 账户）
docker build -f scripts/ci_offline/Dockerfile.python -t dd-ci-python:3.12 scripts/ci_offline
docker build -f scripts/ci_offline/Dockerfile.node   -t dd-ci-node:20   scripts/ci_offline

# 默认跑 4 条腿（lint test helm web）：顺序执行，任一时刻只有一个容器，内存峰值≈1 容器
bash scripts/verify_ci_offline.sh

# 另 5 条重腿要显式点名（Playwright 要下 Chromium；两条 E2E 各起一套 Next 构建）
bash scripts/verify_ci_offline.sh postgres redis supply-chain web-e2e web-e2e-real-backend
# 或者直接要全量（`all` = 脚本里的 ALL_LEGS，9 条腿名单只有一处真源）
bash scripts/verify_ci_offline.sh all

# 只跑某几条
bash scripts/verify_ci_offline.sh lint helm web

# **非 root 腿**（强烈建议提交前跑一次）：以 uid 65534 跑，对齐 GitHub runner 的普通用户
DD_CI_RUN_USER=65534 bash scripts/verify_ci_offline.sh
# 产品落到 artifacts/ci-offline/<ts>/，可与 root 跑的结果并排对比

# Windows 原生侧
bash scripts/verify_ci_native_windows.sh lint helm web

# Windows cmd / PowerShell 下两条降级方式：
# 1) 用仓库自带入口（推荐）：它只负责找到 Git 的 bash.exe，逻辑仍全在 .sh 里
scripts\verify_ci_offline.bat
scripts\verify_ci_offline.bat lint test
# 2) 或自己用绝对路径："C:\Program Files\Git\bin\bash.exe" scripts/verify_ci_offline.sh
# ⚠ 千万别直接 `bash scripts/...`：cmd 下 `bash` 命中 C:\Windows\System32\bash.exe（WSL），
#   报 `execvpe(/bin/bash) failed`；同理 `cd /d/repo` 是 MSYS 写法，cmd 里要 `cd /d D:\repo`。
```

### Windows/git-bash 陷阱（本地真踩过）

git-bash（MSYS2）会把传给 native 程序的 `/xxx` 参数改写成 `C:/Program Files/Git/xxx`。
后果：`docker run -w /work` 变成 `-w "C:/Program Files/Git/work"`，docker 直接 **rc=125** 拒绝，
而耗时只有 1–2 秒——9 条腿齐刷刷“失败”，极易被误读成“测试全挂”。
脚本已处理：`export MSYS_NO_PATHCONV=1` + `cygpath -m` 把宿主路径转成 `D:/data_dog` 风格。
手敲 `docker run` 时请照抄这两点。

产物（`artifacts/ci-offline/<时间戳>/`，已在 `.gitignore` 里）：

- `<leg>.log`：该腿完整输出（命令 + stdout + stderr）
- `rc.tsv`：`leg<TAB>退出码<TAB>耗时秒`
- `manifest.txt`：镜像 id、helm/kubeconform/python/node 版本、commit、工作树状态
- `SUMMARY.md`：给人看的结论表

## 与真 CI 的差异（必须诚实标注）

1. **镜像**不是 `ubuntu-latest`，而是 `python:3.12-slim` / `node:22-bookworm-slim`。同为 glibc/Debian，
   依赖轮子与 Playwright 依赖链一致，但**内核与系统库版本不保证逐字节相同**。
2. **服务容器**（postgres:16-alpine / redis:7-alpine）用的是同版本镜像，由 docker 网络代替 GitHub 的 service container。
3. **没有 Actions 缓存，但依赖已烘进镜像**：Python 依赖在构建期从 `pyproject.toml` 抽装，
   Node 依赖在构建期 `npm ci`，Chromium 在构建期 `playwright install`；腿内只装工作树
   （`pip install -e . --no-deps`）+ `cp -a` 镜像里的 `node_modules`。
   口径与 CI 一致，且消除了“网络决定结论”的假红。
4. 本文的结论**不能**替代第一次真跑 CI 的验证——它只能回答"代码在 Linux 上该不该绿"。
5. 跑验证时要**独占 CPU**：沙箱用例带 0.5s 墙钟预算，并行跑别的重活会把真绿压成假红。
6. **源码不跑在宿主绑定挂载上**：每条腿把仓库只读挂到 `/src`，进容器后顺序拷到本地盘 `/work` 再跑，
   腿内产物（覆盖率 json、回流日志）通过 `/artifacts` 回传。原因见下方“假红”段——Docker Desktop 的
   9p/gRPC-FUSE 挂载慢一个量级，会把“冷的全新解释器启动”拖过 30s 握手超时，制造假红。
   调试可用 `DD_CI_NO_COPY=1` 退回直挂（交付证据不要用它）。
7. **`web-e2e-real-backend` 也已离线**：不再在腿内 `apt-get install python3` + `pip install`，
   改用 `scripts/ci_offline/Dockerfile.e2e`（node 基底 + python3 + 预装 py 依赖）。
8. **唯一仍需联网的是 `supply-chain` 腿的 `pip-audit`**：它要去漏洞库取数据，这是工具固有性质，
   不是我们的依赖安装。降级方式：该腿失败时看日志是 `pip-audit` 网络错误还是 SBOM 生成错误，
   后者才是代码问题。

## 维护约定

- **镜像必须预装依赖**（2026-10-24 教训）：腿内不再 `pip install` / `npm ci` 打公网。
  原因不是“快”，而是**网络不该决定结论**：首轮实测 lint 腿跑 757s 后死在
  `ReadTimeoutError(files.pythonhosted.org)`→`ResolutionImpossible`，web 腿撞 `ECONNRESET`——
  两条都是假红。现在 `Dockerfile.python` 从 `pyproject.toml` 抽依赖预装，腿内只做纯本地
  `pip install -e . --no-deps --no-build-isolation`（验证“工作树能按 pyproject 装上”）；
  node 镜像升 22（`jest-dom@6.10.0` 声明 `engines.node>=22`）并加 npm 重试。
  效果：lint 757s→21s，helm 93s→32s，test 233s→117s。
- **腿内必须自带拷贝前置段**：在 Docker Desktop（9p）上从只读挂载跑子进程会卡在
  `wchan=p9_client_rpc`；每条腿开头把源码（文件集与 CI 同源：`git ls-files` +
  `git ls-files -o --exclude-standard`）拷到容器本地盘再跑。
- **绝不用管道吞 rc**：`docker build ... | tail -6` 后 `%ERRORLEVEL%` 是 `tail` 的返回码，
  真失败会被当成成功（本轮就因此“构建成功”却没有镜像）。一律重定向到日志文件再读 rc。
- **镜像缺失不能静默**：脚本启动时对 `PY_IMG`/`NODE_IMG`/`E2E_IMG` 跑 `ensure_image`（缺则构建，
  构建失败则整轮 rc=9）。理由：`docker run` 对缺镜像只给 rc=125 一句 `Unable to find image`，
  与“腿里代码挂了”在 SUMMARY 里几乎无法区分 —— 2026-10-24 实测 web 腿 4 秒 rc=125 就是这种误判。
- **镜像新鲜度的判据是指纹，不是“镜像存在”**：`ensure_image` 把 Dockerfile + 依赖清单
  （`pyproject.toml` / `requirements.txt` / `web/package-lock.json`）的 sha256 前 16 位写到镜像标签
  `dd.ci.inputs`，与当前文件不符就重建。否则改了 Dockerfile 仍复用旧镜像，
  腿跑的是旧依赖而 SUMMARY 看不出来（2026-10-24 实际发生过）。
- **构建上下文用 `.`**（先 `cd "$REPO"`）：Windows 版 docker 不认 Git Bash 的 `/d/...` 路径，
  实测报 `unable to prepare context: path "/d/data_dog" not found`。
- **命名卷会遮住镜像内容**：不再把 `node_modules` / `ms-playwright` 挂命名卷；卷里一旦有旧内容，
  镜像里烘好的依赖就等于没有。
- 改 `.github/workflows/ci.yml` 里任何一条 `run:` 命令，就同步改 `scripts/verify_ci_offline.sh` 里对应的腿。
  脚本里每条腿的注释都标了它对应哪个 job。
- 新增 job 时，`ALL_LEGS` 里也要加，否则 SUMMARY 会缺一条腿（少一条腿比红一条腿更危险）。
- `DEFAULT_LEGS`（默认 4 条）与 `ALL_LEGS`（全部 9 条）是**两个不同的东西**：前者是日常回归路径（覆盖
  已经被证伪过的失败模式），后者是“证据包”承诺的全集。改前者时必须在注释里写清“为什么这 4 条够了”。
- **覆盖率门禁分两条腿判，且两侧阈值必须逐字相同**：通用模块由 `test` 腿的 `cov_gate.py --min 70` 把关；
  PG-only 文件在 `postgres` 腿里单独卡：`storage/partitioning.py --min 75`、`state/pg.py --min 80`。
  `storage/pg_locks.py` **不卡棘轮**（实测 67.4%，缺的 28 行主要是非 PG 分支与 `wait=False` 快路径，
  属该腿口径的结构性天花板）。2026-10-25 的教训：验证台曾自己多写一道 `--only …pg_locks.py --min 68`，
  ci.yml 却从不卡——于是出现“离线红 / 同一 commit 的真 CI 绿”的**假红**。假红比假绿更贵：
  它会让人不再信验证台，然后绕开它。现在由 `tests/test_ci_optimize_tooling.py::
  test_coverage_gates_are_not_stricter_than_ci` 机械比对两份文件的 `--only/--min`，两侧不一致就红。
  `cov_gate.py` 里 `ALLOW` 写的实测值**必须来自最近一次真跑的日志**，不许照抄旧数字——
  `scripts/cov_gate.py` 的 ALLOW 里曾写“实测 PG 腿 86.7%”，本轮真跑是 75.1%——过时数字等于假话。

## 本轮结果（会过时，以 SUMMARY.md 为准）

产物在 `artifacts/ci-offline/*/`（容器侧）与 `artifacts/ci-offline-native/*/`（原生侧），
每次跑的 `SUMMARY.md` + `rc.tsv` + 逐腿日志才是唯一证据；本文不代替它们。

**已存在但从未验证的两句话（此前写在本文件里，已删）**：“9/9 容器腿通过”“原生侧关键腿通过”——
这是**假话**。真跑一遍立刻出红，且红的都不是“玄学 flake”：

| 发现 | 现象 | 定性 |
|---|---|---|
| 视觉通道 5 项 | `ModuleNotFoundError: PIL` | CI 装 `.[dev,postgres]`，而 `.[dev]` 里没 Pillow → **从来红过**（已修：Pillow 进 dev extra） |
| 沙箱 3 项（首轮 5 项） | Linux 容器内 worker 被 `SIGXCPU` 杀，只报“求值超时” | `RLIMIT_CPU` 写成绝对值 `int(cpu_s)+1`（而非已用量+余量）→ **Linux 独有、Windows 永远绿**（已修：增量语义 + 退出码诊断） |
| SSRF 1 项 | 断言钉死 `::ffff:8.8.8.8` 必须被拒 | 依赖 CPython **补丁版本**的 `is_reserved` 分类（3.12.6 拒 / 3.12.14 放） → 换个补丁版就红（已修：改钉“与 `_is_public_ip` 一致”） |

教训（值得写进任何“CI 从没真绿”的仓库）：**本地 Windows 全绿 + 文档写“已通过”是最贵的假信号**。
红色不靠运气发现，靠把 CI 的命令原样搬到 Linux 跑一遍。

**还有一类纯假红——不是仓库的 bug，是验证环境的 bug**：`tests/test_sandbox.py` 的 6 条 spawn 用例在
“全量套件”里必红、单文件跑全绿。取证（诊断插件打印残留子进程）显示子进程 `state=D`、
`wchan=p9_client_rpc`——卡在 Docker Desktop 的 9p 绑定挂载上；同时 `fds`、`nofile`、`/dev/shm`、
`cgroup pids` 全部宽松，假设逐个排除。修法就是差异第 6 条：让离线腿跑在容器本地盘。
**假红和假绿是同一个根子——环境不同构。**

## 出证据包（给别人看的那种）

全绿之后一条命令：

```bash
python scripts/pack_ci_evidence.py                        # 取 artifacts/ci-offline 下最新一轮
python scripts/pack_ci_evidence.py <轮次目录> --image dd-ci-python:3.12 --image dd-ci-node:20
```

产物是 `artifacts/ci-evidence/ci-evidence-<时间戳>.tar.gz`，包内固定包含：

| 文件 | 作用 |
| --- | --- |
| `EVIDENCE.md` | 自动判定的 PASS/FAIL（**不是人写的**）+ 分支与提交 + 每条腿 rc/耗时 + 镜像 ID + 每个日志的 sha256 + 未提交改动清单 |
| `RERUN.md` | 别人从零复现的命令（含 `MSYS_NO_PATHCONV=1` 这个坑） |
| `<轮次>/*.log` `rc.tsv` `SUMMARY.md` | 原始产物，哈希已在 `EVIDENCE.md` 里 |

**硬规则：任何一条腿 rc≠0，脚本直接拒绝出包（退出码 1）。**
理由见上一节教训——证据包如果能装订一句手写的"已通过"，它只是把假话做得更精致。
另一条：证据里的"工作区：有未提交改动"是**故意**显示的。想得到"干净提交 + 证据"的组合，
顺序必须是 **先提交 → 在该提交上重跑 → 再打包**，而不是"先跑出绿 → 带着一堆未提交改动打包"。

### 坑：新增文件要先 `git add -N`，否则 lint 腿红
`scripts/readme_counts.py` 统计的是 **git 已跟踪**文件，而腿内拷贝的是「已跟踪 + 未忽略的新文件」
（为了能验 WIP）。于是新加一个测试文件、却没 `git add` 时：本地计数说 116，
容器里数出 117 → `lint` 腿报「README 规模与测试不一致」。

这不是假红，是在提醒你**这个文件还没进索引**——先 `git add -N <file>`（或正常 `git add`）
再跑 `python scripts/readme_counts.py`。

## 网络受限环境：先把基镜像"就地"

离线验证台只需要 **Docker**，但**首次**构建镜像仍要能取到三个基镜像：

| 基镜像 | 用在哪 |
|---|---|
| `python:3.12-slim` | `dd-ci-python:3.12`（7 条 Python 腿） |
| `node:22-bookworm-slim` | `dd-ci-node:22`（web / web-e2e） |
| `dd-ci-node:22`（本地派生） | `dd-ci-e2e`（web-e2e-real-backend） |

Docker Hub 直连不通时（实测报 `dialing registry-1.docker.io:443 ... failed`），
用任意加速器取回**再打回官方名**即可，脚本不需要感知镜像源：

```bash
docker pull docker.1ms.run/library/node:22-bookworm-slim
docker tag  docker.1ms.run/library/node:22-bookworm-slim node:22-bookworm-slim
```

> 为什么不在 Dockerfile 里写加速器地址：那会把某台机器的网络拓扑烧进交付物，
> 换环境必炸。镜像源属于**运行环境**，不属于仓库。

`ensure_image` 的策略（三者互补，缺一不可）：
1. **输入指纹**决定是否重建（Dockerfile + 依赖清单 + **基镜像指纹**）；
2. BuildKit 失败时**回退经典构建器**（不拉 manifest，直接吃本地基镜像）；
3. 两者都失败 → `exit 9` 并留下 `images.log`，绝不"跳过这条腿然后写 PASS"。

## kubeconform 的 schema 放在仓库里（否则离线必假红）

`kubeconform` 默认从 `raw.githubusercontent.com` 下载 JSON schema。
它的 `-ignore-missing-schemas` **只容忍 404，不容忍连接失败**——离线时会给
`failed downloading schema ... EOF` → `Errors: 6` → helm 腿变红，而 chart 完全正确。

所以 schema 作为**验证输入**进了仓库：`helm/schemas/`（7 个文件，约 1.7MB）。

| 文件 | 来源 kind |
|---|---|
| `service-v1.json` | Service |
| `serviceaccount-v1.json` | ServiceAccount |
| `configmap-v1.json` | ConfigMap |
| `deployment-apps-v1.json` | Deployment |
| `job-batch-v1.json` | Job |
| `poddisruptionbudget-policy-v1.json` | PodDisruptionBudget |
| `horizontalpodautoscaler-autoscaling-v2.json` | HPA（生产值开启） |

两个 CRD（`PrometheusRule`、`ServiceMonitor`）上游**没有** schema，用 `-skip` 显式排除，
它们的正确性由 `scripts/verify_chart.py` 的不变量断言覆盖。

### 两个守卫（缺一就是"零校验的绿"）

1. `scripts/verify_schemas.py`：渲染出的每个 kind 都必须有本地 schema 文件，
   否则报出缺哪个文件名（新增 HPA/Ingress 时会立刻红）。
2. `scripts/assert_kc_summary.py`：把 kubeconform 的 `Summary` 行当**断言**读——
   `Skipped` 必须恰好等于预期 CRD 数。实测过的反例：schema 文件名大小写不对时，
   它是 `Valid: 0, Skipped: 6` 且**退出码 0**，肉眼看像通过，实际一个资源都没校验。

> kubeconform 解析 `{{.ResourceKind}}{{.KindSuffix}}` 后会把文件名**转小写**再读，
> 所以仓库里必须是 `serviceaccount-v1.json` 而不是 `ServiceAccount-v1.json`。

三条验证口径（原生 Windows / 离线容器 / GitHub CI）用的是**同一份** `helm/schemas/`
和**同一个** summary 断言，避免"三条腿三种说法"。
