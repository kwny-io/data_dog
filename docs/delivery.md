# 落地交付：把 P2 数据面跑成一个服务

前面 P0~P3 都是**库/演示脚本**（跑一次就退出）。"落地交付"要的是**一个能长期运行、可被探活、可被外部接入的服务**。本文说明这一层做了什么、怎么跑、怎么验证。

## 1. 交付形态对照

| 层级 | 形态 | 怎么用 | 状态 |
|---|---|---|---|
| L0 | 库代码 | `import data_dog` | ✅ |
| L1 | CLI | `data-dog run` | ✅ |
| L2 | 演示脚本 | `python -m data_dog.e2e.replay_demo` | ✅ |
| L3 | **HTTP 服务** | `python -m data_dog.service` / `docker compose up` | ✅ 本文 |
| L4 | 编排部署 | K8s / 云托管 | ⬜ 未做（超范围） |

**L3 是"能交付"的门槛**：有了固定的服务地址与探针，别人才敢把流量接进来。

## 2. 服务长什么样

代码在 `src/data_dog/service/`：

| 端点 | 方法 | 作用 |
|---|---|---|
| `/healthz` | GET | **存活探针**：进程活着就 200，不查依赖 |
| `/readyz` | GET | **就绪探针**：探数据库连通；不通 → 503 |
| `/metrics` | GET | 进程内指标（计数，JSON） |
| `/v1/telemetry` | POST | 接入原始日志 → 候选异常 → 落库 |
| `/v1/candidates` | GET | 查已落库候选（可按设备/部件过滤） |
| `/v1/alerts` | GET | 查告警 |
| `/v1/outbox` | GET | 查待投递队列 |

### 存活 vs 就绪，为什么要分开

- **存活（liveness）**：问"进程是不是死了"。死了才该重启。它**不该查数据库**——否则数据库抖一下，编排层会把好好的进程反复杀掉重启，雪上加霜。
- **就绪（readiness）**：问"现在能不能接流量"。数据库不通就返回 503，编排层把流量摘掉，但**不重启进程**。

这个区分是"服务能稳定跑"的关键，也是新手最容易做错的地方（把两个探针写成一样）。

### 身份从请求头来，不从正文来

`POST /v1/telemetry` 要求两个请求头：

```
X-Tenant-Id: tenant-real
X-Device-Sn: device-go2-0001
```

**正文里就算写了 `tenant_id`/`device_sn` 也一律被忽略。** 这与 P2 网关"正文身份一律不作数"的失败语义一致——身份由可信层注入，不能被日志内容伪造。缺任一请求头 → 401（失败关闭）。

## 3. 一条命令起服务

### 方式 A：本机直接跑（不用 Docker）

```bash
pip install -e ".[dev,postgres]"     # 装依赖（含服务与 PostgreSQL 驱动）
python -m data_dog.service           # 起在 0.0.0.0:8000
```

默认用**本地 SQLite**（零安装），适合开发演示。

### 方式 B：Docker Compose 起整套（应用 + PostgreSQL）

```bash
docker compose up -d --build     # 构建镜像并后台起服务
docker compose ps                # 看状态（应有 app/db 两个，db 显示 healthy）
docker compose logs -f app       # 跟日志
docker compose down              # 停（保留数据卷 pgdata）
docker compose down -v           # 停并清库（慎用）
```

`compose.yaml` 里 app 会 **等 db 健康后**才启动（`depends_on.condition: service_healthy`），避免"应用先起来连不上库"的竞态。

### 方式 C：只用 Docker 跑应用（连外部数据库）

```bash
docker build -t data-dog:0.1.0 .
docker run --rm -p 8000:8000 \
  -e DD_DATABASE_URL="postgresql+psycopg://user:pw@your-db-host:5432/data_dog" \
  data-dog:0.1.0
```

## 4. 怎么验证它真的活着

```bash
curl http://127.0.0.1:8000/healthz     # {"status":"ok",...}
curl http://127.0.0.1:8000/readyz      # {"status":"ready","checks":{"database":{"ok":true}}}
curl http://127.0.0.1:8000/metrics     # 计数指标
```

发一条过温日志（应产出 1 条 `critical` 候选并落库）：

```bash
curl -s -X POST http://127.0.0.1:8000/v1/telemetry \
  -H "Content-Type: application/json" \
  -H "X-Tenant-Id: tenant-real" \
  -H "X-Device-Sn: device-go2-0001" \
  -d '{"events":[{"event_id":"e1","occurred_at":"2026-09-13T10:00:00Z",
       "received_at":"2026-09-13T10:00:00.1Z","sequence":1,
       "vendor":"unitree","model":"go2","firmware":"3.2.1",
       "protocol_version":"motor-log-v2",
       "component_hint":"joint_motor.front_left_hip",
       "payload_inline":"{\"temperature\":96,\"current\":12.0,\"mode\":0}"}]}'
```

再查落库结果：

```bash
curl "http://127.0.0.1:8000/v1/candidates?device_sn=device-go2-0001"
```

`make probe` / `make ingest` 封装了上面这些命令。

## 5. 配置项

全部走环境变量（见 `.env.example`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `DD_DATABASE_URL` | 空 → 本地 SQLite | 生产指向 PostgreSQL |
| `DD_SERVICE_HOST` | `0.0.0.0` | 监听地址 |
| `DD_SERVICE_PORT` | `8000` | 监听端口 |
| `DD_SERVICE_NAME` | `data-dog` | 服务名（探针/指标里回显） |
| `DD_LOG_LEVEL` | `info` | 日志级别 |
| `DD_PROJECT_ROOT` | 自动上溯 | 资源根目录（**容器里必须显式指定**） |
| `DD_VENDOR_DOCS` | `<root>/vendor_docs` | 厂商文档目录 |
| `DD_OPERATORS_DIR` | `<root>/operators` | 算子产物目录 |
| `DD_DATA_DIR` | `<root>` | 本地 SQLite 等运行期数据目录 |
| `POSTGRES_USER/PASSWORD/DB` | 见 compose | compose 里数据库账号 |

> **`DD_VENDOR_DOCS` 不是可选项。** 镜像里代码被装进 `site-packages`，靠 `__file__`
> 上溯找不到 `/app/vendor_docs`；Dockerfile 已把它固定为 `/app/vendor_docs`。
> 详见下面第 8 节——这是实跑时抓到的真实 bug。

> compose 中的口令是**本地开发默认值**。生产必须改，并通过密钥管理注入，不要提交真实口令。

## 6. 设计取舍（要讲清楚的地方）

### 每个请求新建一个 DataPlane

`DataPlane` 里的 `CandidateBuilder` 是**累积器**：它按 `(设备, 部件, 信号)` 合并命中，且**没有时间淘汰**。如果整个进程共用一个实例，跑久了会：候选无限增长（内存泄漏），且把相隔很远的命中错误合并成一条。

所以服务**按请求隔离**：resolver/绑定是无状态只读的，构建一次复用；每个请求新建一个 DataPlane。代价是跨请求的窗口特征不累积——当前 Detector 只依赖单条事件的 metrics，不受影响。若将来规则要用跨请求窗口，需要引入带时间淘汰的窗口存储（属于后续工作）。

### 鉴权是可选的，但对外暴露时是必须的

`DD_API_KEY` **留空 = 不校验**（本机开发/评测方便，行为与之前一致）；**一旦设置**，则除
`/healthz`、`/readyz` 外所有端点都要求请求头 `X-API-Key`，否则 401。比较用常量时间
（`hmac.compare_digest`），避免按字符逐位试探的时序侧信道。

> 为什么探针豁免：编排层/LB 探活不该被要求带业务密钥，否则密钥轮换会连带把健康检查打挂。

**开放端口前请务必先设密钥。** 没有它，任何能连上该端口的人都能以任意租户身份注入数据、
读走全部候选——`X-Tenant-Id` 是调用方自己填的，不是认证。

### 落库失败不静默

`/v1/telemetry` 里落库若失败，返回 **207** 且 `ok:false`，同时**仍带上已算出的候选**。这样调用方能看见"算出来了但没存上"，而不是被一个笼统的 500 糊弄。

### 热路径不调大模型

接入→检测→候选全程纯确定性（§4.1）。大模型只在后续"调查报告"阶段用，且报告阶段不在这条热路径上。

## 7. 回归测试

`tests/test_service.py` 覆盖 11 项：存活/就绪探针、指标、鉴权失败关闭、接入→候选→落库、**正文身份被忽略**、固件不匹配失败关闭、查询过滤、批量上限、重复接入幂等。

```bash
pytest tests/test_service.py -q
pytest -q            # 全量
```

## 8. 实跑中修复的真实 bug：安装态下资源路径失联

**这是"落地交付"最有价值的一次发现——本地测试全绿，Docker 一跑就露。**

### 现象

服务起来了、探针全绿、接入返回 200，但一条 96°C 过温日志**产出 0 条候选**。
同一份代码在本地（源码态）跑同样的输入，稳定产出 1 条 `critical` 候选。

### 定位

进容器打印算子配置：

```
CLEANED = /usr/local/lib/python3.12/vendor_docs/unitree/cleaned   ← 不存在
rules = 1     ← 只剩一条兜底规则，过温规则(MOTOR_TEMP_PROTECT ≥90 critical)没了
```

### 根因

各模块用 `Path(__file__).resolve().parents[3]` 推"仓库根"：

- **源码态**：`src/data_dog/data_plane/p2_demo.py` 上溯 3 层 = 仓库根 ✅
- **安装态**：代码在 `site-packages/data_dog/data_plane/`，上溯 3 层 = `site-packages` 上层 ❌

于是 `vendor_docs/` 找不到 → 知识包为空 → 过温规则缺失。**注意它是"静默"失败**：
不报错、不告警，只是少了一条规则、少出候选——比崩溃更危险。

### 修复

1. 新增 `src/data_dog/paths.py`：统一解析 `vendor_docs/`、`operators/`、数据目录；
   优先读环境变量（`DD_VENDOR_DOCS` 等），其次从 `__file__`/CWD 上溯找 marker，
   不再依赖固定的相对层级。改运行时解析（函数）而非导入期常量，以便环境变量生效。
2. 收口 5 处同样的坏假设：`data_plane/p2_demo.py`、`operator_factory/p1_demo.py`、
   `e2e/replay.py`、`evidence/detectors.py`、`storage/db.py`（外加 `knowledge.py` 的向量库目录）。
3. Dockerfile 显式 `ENV DD_PROJECT_ROOT=/app DD_VENDOR_DOCS=/app/vendor_docs DD_OPERATORS_DIR=/app/operators`。

### 防回归

`tests/test_paths.py`（7 项）锁定：

- 路径解析**由环境变量驱动**，与 `__file__` 层级无关；
- 厂商文档在位时过温规则**必须**被加载；
- 文档缺失时退化**必须可观测**（从 2 条掉到 1 条会立刻暴露，而不是静默）；
- Dockerfile 必须声明文档位置。

修完后容器内 `rules=2`，96°C → `cand_fa122dc15642 [critical] temperature_high`，
落 PostgreSQL，重启不丢。

## 9. 怎么让别人用

服务默认只监听本机。按"对方在哪"分三种给法，**风险依次升高**。

### 9.1 对方在同一局域网（最常用）

服务已监听 `0.0.0.0:8000`，找出本机局域网 IP 即可：

```bash
ipconfig            # Windows：看 "IPv4 地址"，如 192.168.1.75
```

把 `http://<你的局域网IP>:8000` 给对方（同一 Wi-Fi/网线下）。Windows 首次会被**防火墙**拦，
放行一次即可：

```powershell
# 仅放行局域网来源到 8000，比直接关防火墙安全
New-NetFirewallRule -DisplayName "data_dog 8000" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private
```

**先做这一步**：`.env` 里设 `DD_API_KEY=<强随机串>`，`docker compose up -d` 重建；
然后建一个只读的演示密钥发给对方。

```bash
python -c "import secrets;print(secrets.token_urlsafe(32))"   # 生成密钥
```

对方调用（带 `X-API-Key`）：

```bash
curl http://192.168.1.75:8000/healthz                          # 探针免密钥
curl -H "X-API-Key: <密钥>" http://192.168.1.75:8000/v1/candidates
```

### 9.2 对方不在局域网 —— 本质问题

局域网靠"同一网段直达"。不在同一网段，就必须有一个**公网能到达的入口**。三选一：

| 方式 | 入口在哪 | 适合 | 代价 |
|---|---|---|---|
| 隧道 | 第三方给的临时公网域名 | 临时演示 | 你的电脑暴露公网；速度受上行带宽限制 |
| 自建 frp | 你的云主机 | 服务留在家里、要长期可访问 | 需要一台公网小服务器 + 一点配置功力 |
| 云主机部署 | 云主机 | **正式对外** | 要买服务器 + 域名 |

### 9.3 方式一：隧道（临时，最快）

在本机跑一条命令，把 8000 映射成公网 HTTPS 地址，无需服务器：

```bash
cloudflared tunnel --url http://localhost:8000   # 免费、自动 HTTPS、无需注册
ngrok http 8000                                   # 上手最快、需注册
```

国内网络提醒：cloudflared / ngrok 有时不稳或需代理；国内替代有 **cpolar**、**natapp**、
**樱花 frp（natfrp）**、**花生壳**。

**务必先设 `DD_API_KEY`**（隧道一开，你的机器就在公网上了）；演示完立即关掉。

### 9.4 方式二：自建 frp（服务留家里、长期对外）

在一台有公网 IP 的云主机上跑 `frps`（服务端），家里跑 `frpc`（客户端）把本机 8000 穿出去。
适合"机器不想搬、但要让外网长期访问"。仍然要设 `DD_API_KEY`，并且公网侧最好加 TLS。

### 9.5 方式三：部署到云主机（正式，推荐）

买一台带公网 IP 的云服务器（阿里云/腾讯云轻量即是够用），装 Docker，然后在本地准备好
**域名 + 配置**再上：

1. 域名 A 记录解析到云主机公网 IP；安全组放行 **80/443**。
2. 复制配置模板并填值：

```bash
cp .env.example .env && vi .env
#   填 DD_DOMAIN=<你的域名>   ACME_EMAIL=<邮箱>
#      DD_API_KEY=<强随机串>   POSTGRES_PASSWORD=<强口令>
```

3. 一条命令起（含自动 HTTPS）：

```bash
make up-prod
# 等价于 docker compose -f compose.prod.yaml up -d --build
```

`compose.prod.yaml` 相比开发版做了三件事（这是"对外"和"本机"的关键差别）：

- **加了 Caddy 反向代理**，按 `DD_DOMAIN` **自动申请并续期 Let's Encrypt 证书**，
  TLS 在 Caddy 终止——不用手工搞证书。
- **应用容器不再对外发布 8000 端口**（`expose` 而非 `ports`），只允许经 Caddy 进入。
- **数据库不对外发布端口**；口令/密钥缺失时**启动即失败**（`${VAR:?}` 强制），
  避免"忘了设密钥就把数据面公开"。

> **TLS ≠ 鉴权。** HTTPS 只保证传输不被窃听，不解决"谁都能调"。`DD_API_KEY` 仍然必须设。

### 9.6 关于国内云主机的一个提醒

若云主机在国内，**域名需要备案**才能用 80/443 提供 Web 服务；未备案可先用高位端口 + 直连 IP
（或选香港/海外节点）。这属于运营层面，与代码无关，但会卡住"能不能对外"。

### 9.7 只分享"代码/镜像"，让对方自己跑

如果他只是想自己起一份（不看你的数据）：见 9.8 表最后一列。
也可以把镜像推到镜像仓库，对方 `docker pull` 即可，不用源码：

```bash
docker tag data-dog:0.1.0 <你的账号>/data-dog:0.1.0
docker push <你的账号>/data-dog:0.1.0
docker run --rm -p 8000:8000 <你的账号>/data-dog:0.1.0
```

### 9.8 一张对照表

| 场景 | 怎么做 | 需要 | 风险 |
|---|---|---|---|
| 同局域网 | 给 `http://<局域网IP>:8000` + 放行防火墙 | 同网段 | 中（内网可扫到） |
| 临时外网演示 | 隧道（cloudflared/ngrok/cpolar） | 一条命令 | 高（公网可达） |
| 服务留家里长期对外 | 自建 frp（frps 在云主机） | 云主机 + 配置 | 中高 |
| 正式对外 | 云主机 + `compose.prod.yaml`（Caddy 自动 HTTPS） | 服务器 + 域名 | 低（可加固） |
| 只给代码 | 仓库 / 镜像仓库 | 对方有 Docker | 无 |

> **不管哪种，先设 `DD_API_KEY`。** 这是把"能跑"变成"能安全给别人用"的关键一步。

## 10. 现在还不是生产级的几件事（诚实说明）

| 项 | 现状 | 生产需要 |
|---|---|---|
| 鉴权 | 单一静态 API Key（可选） | 每租户独立凭据、可撤销、可审计 |
| 传输加密 | 明文 HTTP | TLS（反代终止） |
| 多租户隔离 | `X-Tenant-Id` 由调用方声明 | 从认证主体派生，做行级隔离 |
| 限流 | 进程内令牌桶 | 分布式限流（多副本共享） |
| 水平扩展 | 单副本（窗口/合并器在进程内） | 有状态算子需状态外置（E-4 缺口） |
| 可观测 | JSON 指标端点 | Prometheus 指标 + 日志聚合 + 追踪 |
