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
| `/metrics` | GET | 进程内指标（计数，JSON；全局口径不分租户） |
| `/v1/telemetry` | POST | 接入原始日志 → 候选异常 → 落库 |
| `/v1/summary` | GET | 聚合：候选 / 告警按状态 / 出箱各计数 |
| `/v1/candidates` | GET | 查已落库候选（可按设备/部件过滤） |
| `/v1/candidates/{id}` | GET | 候选项详情 |
| `/v1/candidates/{id}/evidence` | GET | Agent 所见证据 |
| `/v1/candidates/{id}/refs` | GET | 结论所挂证据引用 |
| `/v1/alerts` | GET | 查告警 |
| `/v1/outbox` | GET | 查待投递队列 |
| `/v1/reports` | GET | 分页列历史报告 |
| `/v1/investigations` | POST | 对候选跑 Agent 调查 → 持久化报告（**同步、走 LLM**，不在热路径） |
| `/v1/investigations/{id}` | GET | 读持久化报告 |
| `/v1/feedback` | POST | 人工反馈 → 驱动告警状态机 |
| `/v1/alerts/{id}/feedback` | GET | 读告警反馈历史 |
| `/v1/eval/feedback` | GET | 反馈回灌：一致率 / 误报率 / 漏报率 |
| `/v1/knowledge/documents` | POST/GET | 导入 / 列出用户厂商文档 |
| `/v1/knowledge/search` | GET | 词法检索文档切片 |
| `/v1/knowledge/documents/{id}` | DELETE | 删除文档及切片 |

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

#### 单密钥模式挡不住"持密钥者冒充租户"（已提供绑定模式）

上面那句"先设密钥"只解决了**未授权访问**，没解决**越权访问**：`DD_API_KEY` 是**全局
单一**密钥，租户身份仍由调用方自报。实测（`DD_API_KEY` 已设、非空）：

```
[write] tenant=victim-a   同一把密钥 -> 200
[write] tenant=victim-b   同一把密钥 -> 200   ← 写进了另一个租户
[read ] tenant=victim-a   同一把密钥 -> 200
[read ] tenant=victim-b   同一把密钥 -> 200
[read ] tenant=victim-a   不带密钥    -> 401
```

即：**一把密钥 = 全租户通吃**。应用层租户过滤与 PostgreSQL RLS 挡的是"代码写错导致
串租户"，挡不住"持密钥者主动冒充"——RLS 按 `app.tenant_id` 放行，而那个值正是冒充者
自己填的。

需要真正的租户隔离时，配**租户绑定密钥**（推荐对外部署）：

```bash
DD_API_KEY=<管理密钥>            # 仅用于非租户作用域（/metrics、/v1/state 运维面）
DD_TENANT_KEYS='{"tenant-a":"<key-a>","tenant-b":"<key-b>"}'
```

绑定模式下，带 `X-Tenant-Id` 的请求必须出示**该租户**绑定的密钥，否则 403
（`reason=tenant_key_mismatch`；未知租户与密钥不符返回**同一**状态码，不做租户枚举）。
全局密钥在绑定模式下**不再**是租户面的万能钥匙。未配 `DD_TENANT_KEYS`（默认）时行为
与之前完全一致——切换是显式的，不静默改语义。`DD_TENANT_KEYS` 写坏（非法 JSON /
空值）→ **503 失败关闭**，绝不退回单密钥模式（退回 = 漏洞照旧）。

##### 密钥轮换与吊销（②，v2 密钥环）

上面那种 `{"租户": "密钥"}` 写法（v1）解决的是**冒充**，但它有个运维上绕不开的问题：
密钥是**静态**的——**换不掉**（换 = 所有调用方必须同一瞬间改，没有交叠窗口）、
**撤不掉**（泄露了只能全量替换 + 重启）。v2 允许一个租户在册**多把**密钥：

```bash
DD_TENANT_KEYS='{"tenant-a":{"keys":[
  {"id":"2026-10","secret":"<新钥匙>","not_after":"2027-01-01T00:00:00Z"},
  {"id":"2026-07","secret":"<旧钥匙>","revoked_at":"2026-10-01T00:00:00Z"}
]}}'
```

- **轮换**：旧新两把**同时在册**（交叠窗口）→ 调用方逐个切 → 旧那把置 `revoked_at`。
  全程不需要"同时改"，也不需要停机。
- **吊销**：置 `revoked_at` 后**下一次请求立即失效**——`settings` 是"调用时读取、
  从不缓存"，所以改配置即生效（进程无需重启）。
- **过期**：`not_after`（ISO-8601，`Z` 按 UTC）单独一档，拒绝原因 `key_expired`；
  两样都设时判定为 `revoked`（更强语义先答）。
- **可审计**：`key_id` 落进 `audit_log.key_id`——"**已吊销的那把**被谁在什么时候用过"
  在台账里认得出来（只有指纹的话，吊销这把与那把分不出）。读密钥环本身也留痕
  （`keyring.read`）。
- **运维视图**：`GET /v1/keys`（需 `X-Ops-Token`）列出在册密钥的**元数据**
  （`key_id`/`status`/`not_after`/`revoked_at`/指纹），**永不回密钥本身**——用来核对
  "轮换做完了没、旧钥匙真的失效了没"。**没有签发/吊销 API**，这是有意的：把"发密钥"
  做成端点等于造一个"拿下它就能给自己发钥匙"的高价值目标，而轮换本就是低频变更动作。

配置层面仍然**失败关闭**，且两类"看起来无害、实际会毁掉语义"的写法会被直接拒绝：
同一租户内 `id` 重复（审计分不清是哪把）、旧新**密钥值相同**（同一值必然同时命中两把，
"只吊销旧的那把"不成立，轮换会静默退化成没轮换）。

> **仍未做的**：没有 **KMS**（密钥仍在环境变量里，不轮转托管、无 HSM），密钥下发与
> 吊销仍是**配置动作**（走变更流程 + 重载），不是带审批流的密钥管理服务。密钥环把"租户这一层
> 的**生命周期**"补齐到可用（轮换/吊销/过期/可审计），不等于完整的 IAM。
>
> **设备级身份（⑦）已有过渡档**：`DD_DEVICE_SIGNING=off|optional|required`，设备用
> 预共享密钥对「租户+设备+event_id+occurred_at+sequence+sha256(载荷)」做 HMAC-SHA256，
> 网关验签（默认 `off`，**零回归**；`optional` **按设备**生效）。三条必须知道的边界：
> ① 它仍是**预共享密钥**，不是出厂证书/TPM 背书的密钥——**泄露等价于设备被克隆**，
> 真正的设备身份要 mTLS 或安全元件（属更大工程）；
> ② 签名覆盖**数据**（不只是设备号），故换正文/换事件/换时间都验不过；但**同一条报文
> 重放**仍会被受理——那由既有的 `(幂等键, revision)` 幂等机制挡住，两层职责不重叠；
> ③ `optional` 档下"签名不对"**一律拒绝**（没有"忽略"分支）——否则攻击者只要故意发
> 垃圾签名就能绕开整个校验。

#### 前端控制台：密钥**不进浏览器**（服务端代理）

Web 控制台此前用 `NEXT_PUBLIC_API_KEY` 在浏览器里设 `X-API-Key`。Next.js 会把
`NEXT_PUBLIC_*` **内联进客户端包**，所以任何人打开 devtools 就能读到一把可用的密钥；
叠加"单密钥 = 全租户"，泄露即**租户冒充**。现已改为服务端代理：

```
浏览器 ──(无凭据)──▶ /api/dd/*  ──(服务端附加 X-API-Key / X-Tenant-Id)──▶ data_dog 服务
                      app/api/dd/[...path]/route.ts
```

- 密钥与租户来自**服务端**变量：`DD_WEB_API_KEY` / `DD_WEB_TENANT`（**不带 `NEXT_PUBLIC_` 前缀**）；
- `DD_WEB_API_BASE_URL` 指定上游服务地址（默认 `http://127.0.0.1:8000`）；
- 浏览器传来的 `X-Tenant-Id` / `X-API-Key` **一律丢弃**（否则单密钥模式下仍可冒充）；
- 代理只放行 `v1/*` 与 `healthz`/`readyz`/`metrics`，并拒绝路径穿越——**不是开放中继**；
- 控制台本身是特权界面，故**失败关闭**：配了 `DD_WEB_API_KEY` 却没配用户认证时，
  代理返回 503 并提示配置 `DD_WEB_BASIC_AUTH=user:pass`（本机开发可显式
  `DD_WEB_ALLOW_ANONYMOUS=1`，这是一个可审计的选择）；
- 租户徽标因此改为**只读**（浏览器改不了租户，可改的控件只会静默失效）。

> **边界**：`DD_WEB_BASIC_AUTH` 是**单一共享口令**，不是用户体系——没有账号、角色与
> 审计。对外长期运行应置于带认证的反代之后，或等用户级登录落地（见 §10）。
>
> **门只加在数据面**：`/api/dd/*`（凭据所在处）有门；控制台的**页面外壳**不加门——
> 未认证的访客能看到空壳与 401 报错，但拿不到任何数据。要连页面一起挡，就把它放在
> 带认证的反代之后。
>
> **实测（端到端，真起 Python 服务 + 生产版 `node server.js`）**：直连 API 不带密钥
> → 401；经代理、浏览器不带任何凭据 → **200**（证明密钥确实由服务端注入）；
> 未认证访问代理 → 401、带 Basic → 200；`/api/dd/etc/passwd` 与编码穿越 → **404**；
> POST 时客户端谎称 `X-Tenant-Id: victim-tenant` → 数据落在 **`DD_WEB_TENANT`** 指定的
> 租户（victim 侧 0 条、目标租户 1 条）。构建产物亦已核对：客户端 JS 里 **0 命中**
> `X-API-Key` / `DD_WEB_*` / `NEXT_PUBLIC_API_KEY`。

### 落库失败不静默

`/v1/telemetry` 里落库若失败，返回 **207** 且 `ok:false`，同时**仍带上已算出的候选**。这样调用方能看见"算出来了但没存上"，而不是被一个笼统的 500 糊弄。

### 热路径不调大模型

接入→检测→候选全程纯确定性（§4.1）。大模型只在后续"调查报告"阶段用，且报告阶段不在这条热路径上。

## 7. 回归测试

`tests/test_service.py` 覆盖 77 项：存活/就绪探针、指标、鉴权失败关闭、接入→候选→落库、**正文身份被忽略**、固件不匹配失败关闭、查询过滤、批量上限、重复接入幂等；外加多租户隔离（缺租户头 401、跨租户 404、`?tenant_id=` 被忽略）、调查端点、P3 告警落库接线、反馈回灌分类、用户文档导入与检索。

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

> 本节 2026-10-10 重写。旧版四行（"限流进程内令牌桶""`X-Tenant-Id` 由调用方声明"
> "单副本（窗口/合并器在进程内）""JSON 指标端点"）**已经过期**——那些能力此后都落地了，
> 留着等于低报自己、也让真正的缺口被埋掉。下表只列**现在仍然成立**的差距。

| 项 | 现状 | 生产需要 |
|---|---|---|
| 鉴权 | 全局静态密钥 + **租户密钥环**（`DD_TENANT_KEYS`：v1 单密钥 / v2 多密钥带 `key_id`+`not_after`+`revoked_at`，**可轮换、可吊销、可过期**；未配则退回单密钥）+ **设备身份签名**（⑦：`DD_DEVICE_SIGNING=off\|optional\|required`，设备对「身份+本条数据」做 HMAC-SHA256，网关验签；`optional` **按设备**生效，可一台台开强制）+ **安全审计**（`audit_log`：认证拒绝、运维面访问、跨租户读审计本身、读密钥环都留痕，拒绝记录带 `key_id`；`GET /v1/audit` 分租户/运维两种视野；`GET /v1/keys` 只读元数据） | **KMS 托管密钥**（环境变量 → 托管/HSM、自动轮转）；**真正的设备身份**：出厂证书 / TPM / **mTLS**（当前是**预共享密钥**，泄露等价于设备被克隆）；密钥变更的**审批流** |
| 传输加密 | `compose.prod.yaml` 里 Caddy 自动申请 HTTPS；应用内**无**安全响应头 | HSTS / `X-Content-Type-Options` 等头部 |
| 多租户隔离 | 应用层越权过滤 + **PG RLS** + 租户绑定密钥；绑定的租户由**服务端**注入 | 仍靠"受信网关/绑定密钥"承担主体派生；无用户级 RBAC |
| 限流 | **共享令牌桶**（`StateStore`：memory/postgres/redis，多副本一致） | 后端须选非 `memory`（默认随库里类型自动选）；`memory` 多副本下各限各的 |
| 水平扩展 | 有状态算子已外置 + 分片；file 源、**告警升级 sweeper** 与**保留清理**都有单持有者租约（`state/lease.py`）；异步 job：共享队列 + 条件领取 + **报告与终态同事务** + **卡死恢复扫描**；**job 表保留策略已落地**（④：`DD_RETENTION_JOB_DAYS` 默认 7 天，**只清已终态**，`pending`/`running` 再老也不删） | 僵死判据是时间阈值（极端慢任务可能被误判 `abandoned`，阈值可配） |
| 数据保留 | **已落地**（④）：`data_dog.retention` + `make retention`——原始日志 7 天 / 派生候选 90 天 / 已终态 job 7 天**默认执行**；告警台账与安全审计**默认 0 = 不删**。有界分批、`system=True` 跨租户、**真删前抢 `retention:purge` 单持有者租约**（`DD_RETENTION_LEASE` 默认开，多副本同一时刻只有一个在清，抢不到的本轮跳过）、删除写 `retention.purge` 审计，且先加时间列索引（迁移 `d0e1f2a3b4c5`） | **按租户合同差异化**保留期（当前是全局配置，不是每租户一份）；**冷归档**（当前是删除，不是转冷存储）；清理作业的**调度**交给外部（cron / K8s CronJob），未内置调度器 |
| 数据合规（可携 / 被遗忘） | **已落地**（⑥）：`GET /v1/tenant/export`（本租户导出，每表有上限并显式回 `truncated`）与 `POST /v1/tenant/purge`（**仅运维面** + 必须 `X-Confirm-Tenant` 原样复述租户 id，否则一行不删）。表清单**按元数据动态发现**（新增租户表自动进入范围），删除走有界分批 + `system=True`，**删除证据记在保留租户 `-` 名下**（否则会随该租户的审计一起被删） | **不是全量流式导出**（有界快照）；**不是加密擦除**；**备份与只读副本上的残留不在范围**（靠备份过期策略）；导出限租户视野（运维需该租户密钥，有意不把"能读走"与"能删"绑在同一个令牌上） |
| 可观测 | Prometheus 指标 + **PrometheusRule 告警规则**（四组：可用性 / 热路径 P99 / 治理积压 / 静默失败；与 §9 表一一对应）+ JSON 结构化日志 + W3C traceparent 传播 + **调查运行日志可回看**（`investigation_run_log`：ReAct 轨迹/用量耗时/失败上下文，`GET /v1/investigations/{id}/log`、`/jobs/{job_id}/log`、`/v1/investigations/runs`；告警通知带 run 引用。见 [run_log.md](run_log.md)） | Alertmanager 的**接收方 / 路由 / 静默**由集群侧配（chart 只出规则）；日志聚合与**分布式追踪**仍需外部设施（无 OTel SDK，`trace_id` 只做 correlation-id 传播） |
| 前端控制台 | **服务端代理**（浏览器不持有凭据）+ 可选 Basic 门（`DD_WEB_BASIC_AUTH`） | 用户级登录/RBAC；现为**单租户**控制台（租户由 `DD_WEB_TENANT` 决定） |
| 多地部署 | 跨地域多活为**应用层**近似，RPO > 0 | 数据库同步复制设施（非应用代码可解） |
