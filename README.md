# data_dog

机器狗遥测的故障调查系统：把设备日志变成一条可上报的故障结论。

它做两件必须分开的事。

**检测**是确定性的。原始日志经网关校验、协议绑定、解析归一、窗口聚合，跑一组从厂商文档
生成的阈值算子，产出候选异常。这一段不调用大模型，同样的输入永远得到同样的候选——
检测器要能在设备侧跑，也要能被审计。

**分辨**才用大模型。候选异常里有一部分其实是正常的：工况切换、传感器噪声、刚维修过的
余温。要结合遥测趋势、厂商阈值、邻居设备状态、近期变更，判断它究竟是不是故障。
这一步由 Agent 调只读工具做受限调查，输出结构化报告。

报告出来之后还有一道确定性策略门：上不上报由它决定，不由模型决定。

核心目标不是让模型答得聪明，是让结论值得信。数值必须有出处，结论必须挂证据，
证据不够就交给人。

```
原始日志 / 遥测
  → 检测：网关 → 协议绑定 → 解析归一 → 窗口 → 阈值算子   （确定性，无 LLM）
  → 候选异常
  → 分辨：Agent 受限调查（只读工具 + 部件白名单）→ 结构化报告
  → 策略门：四条件决策表 + 冲突消解                     （确定性，无 LLM）
  → 上报 / 交人工审核
```

一条命令可以走完整条链：`python -m data_dog.e2e.replay_demo`。

---

## 亮点

| 亮点 | 实测证据 |
|---|---|
| **检测与分辨分离** | 检测走确定性阈值算子，热路径零 LLM 调用；只有「这次到底是不是故障」交给 Agent。两层可分别演进、分别评估 |
| **全链闭环** | 厂商文档 → 算子 → 候选 → Agent 报告 → 策略门 → SaaS 反馈，`python -m data_dog.e2e.replay_demo` 全程离线确定性 |
| **带 ground truth 的规模化评估** | MuJoCo 仿真数据集 10 万条。确定性 provider n=2000 → 准确率 **0.818**（Wilson 95% CI 0.801–0.834）、normal 误报 0.064；真 LLM n=500 → **0.708**（0.667–0.746）、normal 误报 **0.72** |
| **上报不由模型判断** | 策略门按四条件决策表加冲突消解决定，同输入同结论 |
| **fail-closed 贯穿全链** | 身份只在网关注入、协议绑定拿不准就拒、错误收敛为有限错误码，不降级成无鉴权 |
| **不拿 `eval` 执行厂商字符串** | 自建受限 DSL：词法 → AST 解释器，无动态执行原语，函数闭集白名单，编译期类型检查 |
| **诚实抽取** | 官方 SDK 头文件明写电机 90°C 触发保护 → `verified`；无出处的 75°C → `unconfirmed`，不写成厂商事实 |
| **能交付** | FastAPI（存活/就绪分离）+ Dockerfile + compose（Caddy 自动 HTTPS）+ Helm chart（含 PrometheusRule） |
| **测试全离线确定性** | `pytest -q` 本机全绿，fake provider，无需 API Key、不联网；真 PG / Redis 两腿由 CI 实测 |

**规模**：源码 172 个 Python 文件 / 37431 行，测试 138 个文件 / 33190 行（138 个测试文件，全离线跑）；前端 `web/` 为 Next.js 15 控制台（77 个 TS/TSX 文件）；5 份正式事件 JSON Schema；46 个 `make` 命令（另有 `help`）。

---

## 快速上手

```bash
pip install -e .

# 离线跑一遍：确定性 provider，不联网、不需要 API Key
data-dog test --offline --all

# 接真实大模型
data-dog init      # 选供应商 → 填 key → 校验 → 写进 .env
data-dog test      # 交互窗：挑候选调查，一轮可连续跑多个，最后出对比表
```

复现 MuJoCo 数据集与准确率数字要额外装 `.[sim]`（约 50MB；未装时 `gen_dataset`
会报 `ModuleNotFoundError`）。`data-dog test` 不用 `--all` 时需要真实终端，它会等你输入编号。

### 起服务与前端

```bash
make dev          # 后端 + 仿真控制台 + 前端，后台守护，命令立刻返回
make dev-status   # 三块各在跑没跑
make dev-logs     # 跟日志（Ctrl-C 只退出日志）
make dev-stop     # 只停脚本自己起的进程
```

Windows 没装 `make` 时，用根目录的 `dev.bat`，一条命令等价：

```bat
dev.bat           :: 启动（双击或终端里敲都行）
dev.bat status
dev.bat logs
dev.bat stop
dev.bat restart
```

`dev.bat` 只做一件事：找到 Git for Windows 自带的 `bash.exe`。cmd / PowerShell 里的
`bash` 会解析到 WSL，而 WSL 看不到 `D:\`。逻辑仍全在 `tools/dev.sh` 里，两边不分叉。

换端口：`PORT=8011 SIM_PORT=8099 WEB_PORT=3000 bash tools/dev.sh start`（`dev.bat` 同样认这些环境变量）。

产品控制台是 `web/`（Next.js 15，5 个页面）。只起前端的话：

```bash
cd web && npm ci
DD_WEB_API_BASE_URL=http://127.0.0.1:8000 DD_WEB_ALLOW_ANONYMOUS=1 npm run dev
```

`DD_WEB_API_BASE_URL` 是服务端代理指向的后端地址。后端换了端口这里要跟着改，
否则页面能打开但数据全空。`DD_WEB_ALLOW_ANONYMOUS=1` 只用于本地演示，生产要用反代身份
或 `DD_WEB_BASIC_AUTH`。密钥不进浏览器，浏览器只访问 `/api/dd/*`，凭据由服务端注入。

### 跑测试

```bash
pip install -e ".[dev]"
pytest tests/ -q
```

想连真库跑 PG / Redis 腿（默认被 skip，没跑不等于通过）：

```bash
docker run -d --name dd-pg -p 5432:5432 \
  -e POSTGRES_USER=data_dog -e POSTGRES_PASSWORD=data_dog_dev_pw -e POSTGRES_DB=data_dog postgres:16
export DD_TEST_PG_URL=postgresql+psycopg://data_dog:data_dog_dev_pw@127.0.0.1:5432/data_dog
pytest -q -rs -m postgres
```

Windows 上很多机器没装 `make`，直接跑上面的 `pytest` 即可。注意 Windows 的 `make`
调到的 `bash` 可能是 WSL，看不到 `D:\`，所以本仓库的 Windows 验证入口是 `.bat` / `.sh`。

---

## 检测：日志 → 候选异常

```bash
python -m data_dog.data_plane.p2_demo          # 人读输出
python -m data_dog.data_plane.p2_demo --json   # 机器可读
pytest tests/test_data_plane.py -q
```

网关校验 → 协议绑定 → 解析 → 归一 → 富化 → 窗口 → 阈值算子 → 候选异常。
实测：6 条原始日志 → 4 条归一化、2 条失败关闭、1 条 critical 候选（带证据）。

三条设计约束：

- **身份只在网关注入**，正文里自称的身份一律丢弃。设备无法靠改报文冒充别人。
- **拿不准就失败关闭**。协议绑定找不到或有多解（`not_found` / `ambiguous`）、
  认证不过（`denied`），一律拒绝，不猜一个最像的继续跑。
- **热路径不调大模型**。检测要在设备侧或边缘跑，也必须能被审计——同输入同输出。

大载荷不塞进日志管道，改为受控引用外置；取数前过 SSRF 校验（仅 http/https，
拒绝 localhost、环回、私有、保留地址）。见 [docs/data_plane.md](docs/data_plane.md)。

检测用的阈值不是手写常量，是从厂商文档里抽出来的。

### 检测规则的来源

```bash
python -m data_dog.operator_factory.p1_demo          # 人读输出
python -m data_dog.operator_factory.p1_demo --json   # 机器可读
pytest tests/test_operator_factory.py tests/test_operator_verification.py -q
```

厂商文档 → 结构化知识（带 revision / 来源 / 定位）→ 检测算子（声明式 + 受限 DSL）→
fixture 回放。实测：知识包 11 revisions / 61 字段 / 9 条数值规格 / 6 个错误码；回放 6/6。

关键在只抽文档里真有的规格，并区分「能证实」与「待确认」：官方开源协议头
（`unitree_actuator_sdk`，BSD-3）明写电机 90°C 触发温度保护，标 `verified`；
75°C 的告警阈在文档里找不到出处，标 `unconfirmed`。

受限 DSL 是自建的词法 → AST 解释器：无动态执行原语，函数是闭集白名单，编译期做类型检查。
所以厂商字符串永远不会被 `eval` 执行。来源与许可见 `vendor_docs/unitree/SOURCES.md`，
详见 [docs/operator_factory.md](docs/operator_factory.md)。

---

## 分辨：候选异常 → 故障结论

```bash
python -m evals.run_evals --mode mock    # 离线确定性评估，7 case（含 2 个对抗性）
python -m evals.run_evals --mode real    # 用真实模型评估（需先 data-dog init）
```

候选异常只是「某条指标越过了线」，不等于故障。分辨这一步把候选连同上下文交给 Agent，
由它调只读工具自己决定还要查什么：

| 工具 | 查什么 |
|---|---|
| 遥测 | 故障窗口内外的指标曲线、参考窗口基线 |
| 厂商知识 | 该部件的阈值、成因、安全上限 |
| 邻居事件 | 同一批次其它设备是否同时异常（区分设备故障与批次问题） |
| 近期变更 | 固件、算子是否刚升级过 |
| 因果链 | 多个异常之间是否同一根因 |

约束是只读加部件白名单，越权部件直接返回 `denied`。调查循环：

```
候选异常
  → Reason：根据已有证据决定下一步查什么
  → Act：调用只读工具
  → Observe：读工具返回，更新假设
  → 证据足够 → 输出 InvestigationReport
  → 证据不足 → needs_human_review
```

报告含裁决、置信度、支持的假设、缺失的证据、建议动作。ReAct 轨迹记录每步调了哪个工具、
传了什么参数、看到什么结果，CLI 实时渲染，事后可从运行日志回看。

一个实际出现的分歧：真 LLM 的故障召回比确定性基线更高，但误报率高一个数量级。
所以这一层不能只看准确率，误报率要单独盯。

### 上报：结论 → 告警

```bash
python -m data_dog.alerting.p3_demo          # 人读输出
python -m data_dog.alerting.p3_demo --json   # 机器可读
pytest tests/test_alerting.py -q
```

上不上报由确定性策略门决定，不看模型怎么说。策略门按四条件决策表加冲突消解，
对同一个报告永远给出同一个结论。

- **Episode 语义**：同一故障始终是同一个 `event_id`，构成规则写死在代码里。
- **幂等投递**：`(幂等键, revision)` 复合主键，网络重试与进程重启都不产生重复告警。
- **投递前剔除禁止字段**，不让不该外发的字段随载荷出去。

实测：confirmed 加严重码 → 上报 critical；rejected 或缺证据 → 不上报；
同一 Episode 更新时 revision 递增，不重复投递。见 [docs/alerting.md](docs/alerting.md)。

### 整链回放

```bash
python -m data_dog.e2e.replay_demo
pytest tests/test_e2e.py -q
```

厂商文档 → 算子 → 候选 → Agent 报告 → 策略门 → SaaS → 反馈，全程离线确定性可复现。
实测：98°C 过温日志 → critical 候选 → Agent confirmed(0.9) → 上报 critical → SaaS 接收。
见 [docs/e2e.md](docs/e2e.md)。

---

## 数据从哪来

系统自带三档数据源，都实现与 `EvidenceStore` 相同的查询接口。换数据源只需替换这一层，
Agent 与工具逻辑不用改。

**mock 证据**（`src/data_dog/evidence/store.py`）是 CLI 与评估默认用的那份：左髋电机
温度从 52°C 升到 78.2°C 越过 75°C 告警阈、电流同步抬升，应判热退化；右髋电机 61°C / 6.2A
都在基线内，应排除；电池 BMS SOC 0.72，应判正常。附带厂商知识库、邻居事件与近期变更。

**MuJoCo 仿真**（`data_dog/mujoco_sim/`）加载 Go2 真实四足模型，跑受控小摆动，导出物理自洽的
关节力矩与电流，再叠一阶热模型得到电机温度。可注入 5 类故障（堵转 / 热退化 / 通信丢包 /
电流偏置 / 负载骤增），每条样本带 ground truth。`python -m data_dog.mujoco_sim.mujoco_eval`
在小集上跑（n=6，accuracy 1.0）。

**忠实仿真数据集**（`data/mj_60s_100k`）在其之上加了自写 trot 步态控制器，让狗自由基座真走路，
叠真实噪声模型与 5 类故障批量生成：100,000 条，60s/条，100 Hz，8 worker，400 分片，
100% free_trot，0 翻倒，53 MB（仅物化标量汇总）。保真度档次与假设值清单见
[docs/mujoco_dataset.md](docs/mujoco_dataset.md)。

在该数据集上做了同口径分层抽样评估（`seed=7`，强制含 normal 层），两层换的只是「谁来下判断」：

| 栏 | n | 准确率（Wilson 95% CI） | normal 误报 | F1 | F2 | F3 | F4 | F5 |
|---|---|---|---|---|---|---|---|---|
| 确定性 provider | 2000 | **0.818**（0.801–0.834） | 0.064 | 1.00 | 0.69 | 0.70 | 0.43 | 0.96 |
| 真 LLM | 500 | **0.708**（0.667–0.746） | **0.72** | 1.00 | 0.79 | 0.97 | 0.95 | 0.99 |

结论是反直觉的：真 LLM 的故障召回其实更好（F2 / F3 / F4 都显著高于确定性档），
但误报率差一个数量级——175 台健康设备里 123 台被判 `needs_human_review`，只有 49 台被正确排除。
**真 LLM 上生产的瓶颈不是漏报，是淹没值班人的误报。** 证据文件：
`data/mj_60s_100k/final_eval_n2000_clean.json`、`final_eval_n500_real.json`。

误报的根因已定位在裁决口径本身：原 `SYSTEM_PROMPT` 写的是「若证据不足以给出确定结论，
应判 inconclusive」，把「证据不足」和「证据充分但显示正常」混成了一件事。口径已改成有序四档，
`needs_human_review` 只在关键证据拿不到或冲突无法消解时用，并写明「没发现异常 ≠ 证据不足」。
**治理后的数字暂缺**：重跑时本机 key 余额为零，每次调用都失败关闭，跑出来的不是模型行为，
所以这栏留空。充值后复跑即可补齐：

```bash
python -m data_dog.synth.dataset_eval --manifest data/mj_60s_100k --n 500 --seed 7 --mode real --workers 8
```

顺带得到一次真实验证：LLM 不可用时系统失败关闭，全判 `needs_human_review` 并标注 `fail_closed`，
绝不编造 confirmed。`--workers` 是因为真 LLM 每步是秒级网络往返；仿真回放仍在锁内串行，
MuJoCo 的模型实例非线程安全，并发回放会静默污染数字。

两条边界。一是仿真不等于真机，真实误报率仍需真机数据回灌才能定论。二是 F4 召回偏低源于
评估器两处缺陷（均已修复）：参照窗与故障窗重叠，把故障起点之前作为参照后解决；
证据白名单漏了 `front_right_knee`，该部件占总故障约 25%，查证据被拒后对故障样本是免费命中、
虚高了历史分数。两处修复后的权威准确率是 0.818，低于此前虚高的 0.879 / 0.844——这是暴露旧数
虚高，不是 Agent 退化，同种子单变量对照已核实。normal 误报保持 0.064，未提高灵敏度。

**纯 NumPy 合成遥测**（`data_dog/synth/`）是不依赖 MuJoCo 的降级方案，基于 Go2 公开物理参数
生成时间序列，同样带 5 类故障注入与 ground truth（`python -m data_dog.synth.synth_eval`）。

生产环境接真机时，接入侧走 `stream/` 或 HTTP `/v1/telemetry`，证据侧把 `EvidenceStore`
换成真实时间序库与厂商知识库的实现。见 [docs/real_device_integration.md](docs/real_device_integration.md)。

---

## 部署

```bash
pip install -e ".[dev,postgres]"
python -m data_dog.service                 # 本机起，默认本地 SQLite，零安装

docker compose up -d --build               # 应用 + PostgreSQL
docker compose up -d --build --profile web # 连控制台一起
```

控台默认读租户 `tenant-demo`。要让页面有数据，投一条演示日志：

```bash
bash tools/ingest_demo.sh    # 96°C 过温日志 → 产出一个候选（make ingest 是它的别名）
```

默认投的租户也是 `tenant-demo`，两侧必须一致，投到别的租户的数据不会出现在控台——
这是多租户隔离在正常工作。这条一致性有测试守着。

`--build` 该带就带：首次构建要装全部依赖，之后增量重建约 1 分钟且不重装依赖。
跳过它会跑到旧镜像。

端口被占用时改发布端口即可（`APP_PORT=8011 docker compose up -d`）。并且确认打的是本服务：

```bash
curl -s localhost:8000/openapi.json | head -c 80   # 应出现 data_dog
```

Windows 上还有一种更隐蔽的情况：别的服务绑在 `127.0.0.1:8010`，Docker 发在
`0.0.0.0:8010`，两个 socket 都绑定成功但具体 IP 优先，于是 `curl 127.0.0.1:8010`
打到的是别的产品。容器内自证可以区分：

```bash
docker compose exec -T app python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/openapi.json').status)"
```

它通而宿主端口不通，就是端口撞了，不是服务坏了。

复用旧数据卷时注意：如果 `pgdata` 是迁移体系之前留下的库（有表、没有 `alembic_version`），
`upgrade_head()` 会撞 `DuplicateTable`，服务照常启动但一直未就绪。可丢数据的库
`docker compose down -v` 重来；要保留数据就先 `alembic stamp <revision>` 再 `alembic upgrade head`。

部署前建议体检配置：

```bash
data-dog config check    # STRICT=1 时警告也算失败
```

只判定能用环境变量判定的三件事：未知键（拼错等于值被忽略）、非法取值（静默回落默认值）、
危险组合（鉴权全关 / 异步调查加进程内状态 / 启动即自动迁移）。不连数据库、不验密钥真假。

**给别人用**先设 `DD_API_KEY`，否则端口开放等于任何人可读写。同一局域网直接给
`http://<你的IP>:8000`，调用带 `X-API-Key`。对外部署：

```bash
cp .env.example .env && vi .env      # 填 DD_DOMAIN / ACME_EMAIL / DD_API_KEY / POSTGRES_PASSWORD
make up-prod                         # Caddy 自动申请 HTTPS，只开 80/443
```

`compose.prod.yaml` 里应用与数据库都不对外发布端口，只经 Caddy 进入。

### 接口

| 端点 | 作用 |
|---|---|
| `/healthz` `/readyz` | 存活 / 就绪探针。数据库不通或表缺失 → 503 |
| `/v1/telemetry` | 接入原始日志 → 候选异常 → 落库 |
| `/v1/candidates` `/v1/alerts` `/v1/reports` | 租户内分页查询 |
| `/v1/candidates/{id}/evidence` | Agent 所见证据 |
| `/v1/candidates/{id}/refs` | 证据引用 → 解析成可读证据 |
| `/v1/investigations` | 对候选跑调查。默认异步，回 `202 {job_id}`，`?sync=1` 退回同步 |
| `/v1/investigations/{id}/log` | 调出某次调查的运行日志（ReAct 每步 + 用量 + 失败上下文） |
| `/v1/audit` | 安全审计：认证拒绝 / 运维面访问 / 审计读取 |

身份只认请求头 `X-Tenant-Id` / `X-Device-Sn`，正文与查询串里的身份字段一律忽略。
缺租户头 → 401，跨租户读 → 404。完整端点表、Docker 细节与端口排查见
[docs/delivery.md](docs/delivery.md)。

---

## 工程说明

**持久化**：同一份代码，本地默认 SQLite（零安装），生产设 `DD_DATABASE_URL` 切 PostgreSQL。
核心对象（遥测 / 候选 / 报告 / Alert / Outbox / 反馈）都落库，重启不丢。全部走 ORM 绑定参数，
无字符串拼接 SQL。见 [docs/storage.md](docs/storage.md)。

**可观测性**：真实模型调查会统计每次调用的 token 用量与耗时，按供应商价格表估算成本，
写进报告 `model_provenance.usage`，CLI 报告头部显示一行。离线 fake provider 无 token 统计，
故不显示（避免无意义 0）。

**一次静默失败与防护**：本地跑正常，装进容器后少了一条检测规则——不报错、不崩溃，只是结果不对。
根因是代码按「从当前文件往上数三层」推算配置目录，本地对，安装后路径变了就找不到。
修法是资源位置改为外部可注入（`data_dog/paths.py` 加环境变量），不让代码猜路径；
`tests/test_paths.py`（7 项）锁定「降级必须可观测」。

**可选的 RAG 检索**：默认厂商知识是内置条目。要让它来自真实资料，可以把 10 份公开厂商文档
经 MarkItDown 转文本、切块灌进 Chroma，之后 `query_vendor_knowledge` 走语义检索。
设 `DD_KB_CHROMA=1` 启用，取不到就降级回内置条目并标注 `retriever`，不静默假装是检索结果。
落地脚本 `scripts/chroma_ingest.py`。

**设计契约**：`schemas/*.json` 是 5 类标准事件（RawLogEnvelope / NormalizedTelemetry /
CandidateEvent / InvestigationReport / SaaS AlertEvent）的正式 JSON Schema，
校验用 `python schemas/tools/validate_examples.py`。量化目标在 [docs/slo.md](docs/slo.md)，
受限 DSL 规范在 [docs/dsl_spec.md](docs/dsl_spec.md)。

---

## 边界与未做的部分

这一节列的都是当前真实存在的限制。没做的写没做，做了但有前提的写前提。

### 数据、隔离与合规

**遥测支持真实流，但默认关闭。** `src/data_dog/stream/` 有四种传输：文件与日志尾随、
HTTP 推送用标准库，MQTT 和 Kafka 需可选依赖 `.[mqtt]` / `.[kafka]`。配
`DD_STREAM_ENABLED=1` 和 `DD_STREAM_SOURCE=...` 启用，走与 HTTP 入口同一条数据面。
默认关闭是因为演示和 CI 环境没有真实设备，不是做不到。评估集的证据仍来自 mock、合成与
MuJoCo（可复现），真实误报率要等真实数据回灌才能定论。

**多租户有数据库强制层，仅限 PostgreSQL。** 应用层让 `tenant_id` 进复合主键、仓储强制过滤、
跨租户读返回 404；数据库层由迁移 `a7b8c9d0e1f2` 对租户表启用 `ROW LEVEL SECURITY`
加 `USING` / `WITH CHECK` 策略。SQLite 没有 RLS，本地开发仍是应用层隔离。三条必须知道的边界：

- superuser 与表属主默认绕过 RLS，生产连接必须用非属主、非 superuser 角色。
- 未设 `app.tenant_id` 时谓词为 NULL，查询返回零行，属于失败关闭。
- 后台的跨租户扫描（升级 sweeper、异步 job 恢复扫描）必须走显式
  `session_scope(system=True)`，迁移 `b7c8d9e0f1a2` 给策略加了这条 `OR` 分支。
  不给这条通道，它们在非特权角色下会扫到 0 行、静默不干活，而 CI 的 PG 腿连的是 superuser，
  所以一直没暴露。

真库验证在 `tests/test_storage_postgres.py`。

**鉴权分两档，默认那档挡不住租户冒充。** `DD_API_KEY` 是全局单密钥，而租户身份来自调用方
自报的 `X-Tenant-Id` 请求头。实测同一把密钥以 `victim-a` 和 `victim-b` 身份写入遥测，
两次都返回 200——一把密钥等于全租户通吃。应用层过滤与 RLS 防的是代码写错导致的串租户，
防不了持密钥者主动冒充，因为 RLS 按 `app.tenant_id` 放行，而那是冒充者自己填的。

需要真隔离时配 `DD_TENANT_KEYS='{"tenant-a":"…","tenant-b":"…"}'` 进入租户绑定模式：
带租户头的请求必须出示该租户绑定的密钥，否则 403（未知租户与密钥不符返回同一状态码，
不做租户枚举）。配置写坏返回 503，失败关闭，不退回单密钥模式。

密钥仍配置在环境变量里，没有 KMS 托管，也没有变更审批流。轮换、吊销、过期已落地
（一个租户可在册多把，新旧交叠着换，`revoked_at` 后下一次请求即失效，`key_id` 进审计），
但签发和吊销仍是配置动作。设备级身份有过渡档 `DD_DEVICE_SIGNING=off|optional|required`，
设备对「身份 + 本条数据」做 HMAC，网关验签，按设备生效。它仍是预共享密钥，
真正的设备身份还要 mTLS 或安全元件。见 [docs/delivery.md](docs/delivery.md)。

**保留策略真的在删。** `docs/slo.md` §6 一直写着「原始日志 7 天 / 规范化指标 90 天 /
审计记录 ≥ 1 年」，但此前没有任何清理路径，声明只活在文档里。现在 `telemetry_events`
按摄取日分桶分区，清理改走整段 `DROP PARTITION`，真 PG 实测过。主键因此加了 `created_day`，
代价是跨天重投递会多一行，同日仍幂等。

执行入口是 `make retention`，默认 dry-run。默认值按「删了能不能复原」分档：可重灌的
（原始日志 7 天、派生候选 90 天、已终态调查 job 7 天）默认执行；告警台账与安全审计默认 0，
即不删——销毁审计记录本身就是合规风险，要删必须由人显式开启。

执行上有五条硬约束：有界分批（批数用满即停并标记 `capped`，剩下的下一轮继续，不为清完把库
锁死）；跨租户走 `session_scope(system=True)`；保留判据用 ingestion 时间而非事件时间，
否则补传的历史时间戳会让该行永远「年轻」；单持有者租约（抢不到 `retention:purge` 的副本
本轮跳过并标记 `skipped="lease_held"`，绝不并发删同一批）；删除动作本身写审计。
边界：保留期是全局配置，是删除而非转冷归档，调度交给外部 cron，没有内置调度器。
见 [docs/storage.md](docs/storage.md) §8。

**合规要的两件事成对给了。** `GET /v1/tenant/export` 导出本租户快照，每表有上限并显式返回
`truncated`；`POST /v1/tenant/purge` 只在运维面开放，且必须在 `X-Confirm-Tenant` 里原样复述
租户 id，否则一行都不删。表清单按元数据动态发现，新增带 `tenant_id` 的表自动进入范围；
删除证据记在保留租户 `-` 名下，记在该租户名下会随之被删，等于没有证据。
边界：导出是有界快照不是全量流式，删除是按表删行不是加密擦除，备份与只读副本上的残留不在范围。

### 运行时与可靠性

**有状态后端显式声明 RESP2，Redis 要求 ≥ 2 而非 ≥ 6。** redis-py 8.x 默认走 RESP3，
连接先发 `HELLO 3`，而 `HELLO` 要到 Redis 6 才有——对着 Redis 5.x 直接抛错，依赖解析和
CI（redis:7）全绿，只在真实老环境炸。本模块只用 `WATCH` / `MULTI` / `EXEC` / `GET` / `SET` /
`TTL` / `SCAN`，两个协议语义一致，所以固定 `protocol=2`。真服务契约测试见
`tests/test_state_redis_real.py`。

**PG 上的 JSON 列真的是 jsonb 了。** `storage/models.py` 的 docstring 一直写「PostgreSQL 为
JSONB」，但 baseline 迁移用的是 `sa.JSON()`，落的是 `json`——不能建 GIN 索引、不去重。
现已改为方言变体加迁移 `f2a3b4c5d6e7`。另补了 `(tenant_id, created_at[, tiebreaker])`
复合索引，以及把 `http_request_duration_seconds` 顶格从 2.5s 补到 120s——同步调查要等一次
真 LLM（10–60s），旧顶格下那条路的 P95 / P99 全落 `+Inf`，实际不可观测。

**SLO 从写在文档里变成会响的。** 此前 helm 只有 ServiceMonitor，指标抓得到但没有告警规则。
现在随 chart 出 `PrometheusRule`，覆盖可用性、热路径 P99、慢路径治理，以及三条静默失败的
唯一可见窗口（审计写入失败、write-behind 丢数、流式队列满丢弃）。只有 P99 那条阈值来自
`docs/slo.md` §3，其余是工程阈值。表达式全是全局聚合、不带租户标签（防基数爆炸）。
`tests/test_helm_alerts.py` 要求规则里每个指标都在 `core/metrics.py` 真实声明——
引用不存在的指标就是永不触发的死规则。

### Agent 治理与安全

**提示注入有结构性防御。** 候选正文、遥测、用户上传的厂商文档、工具返回都会进上下文。
三层处置：能力边界（模型不能直接产生副作用，上报由策略门决定，工具只读加白名单，
热路径无 LLM——这是唯一在「防住」意义上的那一层）；结构分隔（不可信文本包成
`<<<UNTRUSTED_DATA>>>` 数据块，原文逐字保留，悄悄改写证据比注入更危险）；特征扫描
（8 条模式命中进运行日志）。红队入口 `make redteam RUNS=5`。

边界：正则可绕过，命中不等于恶意；多模态图像里的文字注入尚未覆盖；真模型脆弱性是抽样结论。
另有一处容易漏：数据块剥壳要用 `core/injection.unwrap`，漏了会让裁决静默退化成
「无法获取遥测」，评测数字崩掉而不报错。见 [docs/prompt_injection.md](docs/prompt_injection.md)。

**LLM 治理的三道闸都排在花钱之前。** 此前只有统计，没有任何地方在花钱前做限制——
单个租户持续提交候选就能把当日额度用完。现有：`Idempotency-Key` 幂等（窗口内同一 key
只真跑一次，进行中 409）；队列准入（满了返回 429 加 `Retry-After`，不落 job——收下再拒是
假的受理）；额度硬熔断（接活前与每次模型调用前各查一次，跑一半也会被叫停）。扣费按 provider
真实 usage，跨周期自动归零。调用侧有有界重试加备用模型降级，只重试超时、不可用、被限流。

边界：单次调用最多超一次额度（预授权才能零超出，复杂度不抵收益）；阈值是全局配置不是每租户
合同；单副本用 `memory` 状态后端时额度与幂等只在进程内成立，多副本须 `postgres` 或 `redis`。
见 [docs/llm_governance.md](docs/llm_governance.md)。

**调查运行日志可回看。** 此前 ReAct 轨迹只在同步响应里返回，异步 worker 直接丢弃，
job 失败只留一个 `error_code`。现在落库到 `investigation_run_log`（启用 RLS），同步与异步、
成功与失败与取消都写，异步日志与 job 终态同事务。三个端点可按报告 id、job id 或候选 id 查。
告警通知也带 `investigation_id` 和 `run_log_path`，值班人收到告警能一键回到诊断过程。

边界：轨迹脱敏并裁剪（单步 4 KB，最多 40 步），是过程记录不是证据包。写放大上有取舍：
同步「成功且已判定」只落轨迹骨架，失败、取消、`needs_human_review` 落全文，异步一律全文。
见 [docs/run_log.md](docs/run_log.md)。

**Windows 控制台的 UTF-8 兜底。** 演示脚本与 CLI 输出含 `▸`、`✅` 等符号，Windows 简体中文
控制台按 GBK 编码 stdout，会直接抛 `UnicodeEncodeError` 崩溃。CI 只跑 Linux，单测全绿也抓不到。
现由 `core/console.py` 在各入口统一把 stdout / stderr 重配为 UTF-8，包括 `__main__` 路径
覆盖不到的 console_script 回调。

**支持水平扩展。** 有状态算子已外置到可插拔的 `StateStore`，原语是 `mutate(key, fn)` 原子读改写。
后端有 `memory`（默认，单副本正确）、`postgres`（行锁）、`redis`（需 `.[redis]`）。
候选归并与限流桶都在共享后端，所以多副本归并和限流一致。流式源的多副本消费按源协调：
Kafka 用消费者组，MQTT 配共享订阅，file 用应用层租约选主。告警升级 sweeper 用同一把租约，
否则多副本会各自扫到同一条到期投递，台账虽按 `delivery_id` 去重，但通知已经发出去了。

剩下的边界：MQTT 未配共享订阅且未配 `DD_REPLICA_MEMBERS` 时每个副本收全量，会重复处理；
file 源本质仍是单消费者，要并行得转 Kafka 或 MQTT。

**P4 大工程八项已落地**：算子沙箱、控制面（Registry / Canary / 回滚 / 签名）、通知与值班
升级 SLA、K8s 与 Helm 编排、车队级分片、跨地域多活（应用层）、退化趋势预测、软实时基准。
边界：硬实时 < 1ms 仍为非目标（已改述为软实时）；多活 RPO > 0，不是同步复制；沙箱在 Windows
无 seccomp，降级为子进程加资源上限；分片再平衡的双读未实现，不声称 exactly-once。
见 `docs/p4/`。

### 评估与泛化的已知问题

**评测完整性更正。** 证据源此前把 `fault_ground_truth` 和逐点 `valid_mask`（12000 点）
原样喂给 Agent——前者是评测污染，真 LLM 可能直接读到答案；后者是每次约 2 万 tokens 的浪费。
已修，`tests/test_evidence_no_leak.py` 钉住。同口径 token 从 P95 13 万降到 9.6k。
**因此上面「真 LLM 0.708 / normal 误报 0.72」那栏须在修复后重跑再引用。**
成本与时延见 [docs/agent_cost_latency.md](docs/agent_cost_latency.md)。

**跨数据集泛化：外部数据集已接，判据未迁移。** CWRU 轴承公开数据集已接成 `EvidenceSource`
（手写 MAT v5 解析，零新依赖），Agent 与工具链一行未改。36 窗口实测：确定性基线
（0.75 / 0.719 / FP 0）与真 LLM Agent 逐条一致；四载荷留一交叉验证（LOLO）的结论是
召回可迁移、误报不免费。

装载层后来查出两个错误：官方 normal 页没写采样率，按「点数 ÷ 录音时长」反推应为 48 kHz，
旧代码按 12 kHz 算包络导致频带偏了 4 倍；`99.mat` 含两条记录，旧装载只取第一个 DE 变量，
导致 op_2 的 normal 与 op_1 逐点重复。修正后换零误报约束的双条件规则（缺陷频带 OR 峭度，
阈值取标定集正常上界乘 1.1，不扫分位数——分位数会把报警线压进正常分布内部），
LOLO 四折 acc / 召回 / FP 均为 1.000 / 1.000 / 0.000。

边界：独立单元只有 4 个载荷，normal 每载荷 1 个文件，「误报 0」的置信区间很宽，
且阈值仍需带标签的标定集。见 [docs/cross_dataset_cwru.md](docs/cross_dataset_cwru.md) §8–§9。

**Agent 有两条可选证据通道，默认关闭。** 一是跨调查记忆：把该部件既往裁决当先验，
只在有既往报告时才暴露，系统提示明说「先验 ≠ 本次证据」。二是无损视觉记忆：设备相机原始帧
按内容寻址无损留存，可检索可放大，需 Pillow 与多模态 provider。

合成渲染实测显示，把遥测画成曲线没有增益（图和文本同源，accuracy 1.00 → 0.89，误报
0.00 → 0.67）；换成外观故障（遥测正常，只有画面里有漏油、形变或异物）结果是正的——
关视觉 accuracy 0.583 / 干净误报 1.00，开视觉 1.00 / 召回 1.00 / 误报 0.00。
**视觉的价值在于携带文本没有的信息。**

尚未接真机相机。方法论出自 VISTA，只移植了「无损记忆 + 主动检索」这一半，
无动作回环，不声称复现其基准。见
[docs/agent_memory_and_vision.md](docs/agent_memory_and_vision.md)。

---

## 目录结构

```
src/data_dog/            # 17 个包 + 20 个顶层模块（.py）
  providers/    多供应商接入（DeepSeek/OpenAI/智谱）
  evidence/     证据库 + 在线检测算子
  tools/        受控调查工具（5 个只读）
  agent/        AgentLoop
  schemas/      CandidateEvent / InvestigationReport
  synth/        合成遥测生成器 + 评估
  mujoco_sim/   MuJoCo 仿真数据源（表达式见 docs/mujoco_dataset.md）
  operator_factory/  离线算子工厂（诚实抽取 + 受限 DSL + Fixture 回放）
  data_plane/   在线数据面（网关/绑定/解析/候选 + SSRF 安全引用）
  alerting/     上报闭环（策略门 + Alert 生命周期 + 幂等 Outbox）
  e2e/          整链回放
  storage/      持久化（SQLAlchemy）+ 仓储层 + 分区 DDL
  stream/       真实遥测流式接入（file / http / mqtt / kafka）
  replication/  跨地域多活（应用层可见部分）
  service/      HTTP 服务（FastAPI）
  core/         横切关注点（日志 / 追踪 / 指标 / 注入防御 / 控制台 UTF-8）
  state/        跨副本共享状态 + 租约
  # 以下是仓库根目录
  ---
web/            产品控制台（Next.js 15）
ui/             Streamlit 演示台（不是产品控制台）
evals/          Agent 评估框架（7 case，含对抗性）
scripts/        数据清洗 / 算子生成 / RAG 落地 / 覆盖率门禁 / git-hooks
schemas/        标准事件正式 Schema（5 份）
docs/           设计文档（含 docs/p4/ 十篇）
tests/          pytest
alembic/        数据库迁移（启动时按 DD_AUTO_MIGRATE 自动 upgrade head）
helm/           K8s 部署 chart（含 PrometheusRule）
deploy/         容器与生产 compose
operators/      算子产物
benchmarks/     性能基准
data/           数据集与本地 SQLite
vendor_docs/    厂商文档备份（含 SOURCES.md 来源与许可）
third_party/    第三方源码（mujoco_menagerie）
tools/          开发辅助（loadgen / perf_probe / bench / k8s_e2e）
```

---

## 文档地图

| 想了解 | 看 |
|---|---|
| 架构评审逐项落地台账 | [docs/architecture_review_ab.md](docs/architecture_review_ab.md) |
| 全局架构（分层 / 依赖 / 数据流） | [docs/architecture.md](docs/architecture.md) |
| 落地交付、Docker、生产 compose | [docs/delivery.md](docs/delivery.md) |
| 在线数据面 | [docs/data_plane.md](docs/data_plane.md) |
| 上报闭环 | [docs/alerting.md](docs/alerting.md) |
| Agent 降级路径 | [docs/degradation.md](docs/degradation.md) |
| 持久化 / 多租户 / 保留 | [docs/storage.md](docs/storage.md) |
| 整链回放 | [docs/e2e.md](docs/e2e.md) |
| SLO 量化目标 | [docs/slo.md](docs/slo.md) |
| 事件 Schema 与版本策略 | [schemas/README.md](schemas/README.md) |
| MuJoCo 数据源与保真度 | [docs/mujoco_dataset.md](docs/mujoco_dataset.md) |
| 真机接入 | [docs/real_device_integration.md](docs/real_device_integration.md) |
| P4 大工程八篇 | [docs/p4/](docs/p4/) |
