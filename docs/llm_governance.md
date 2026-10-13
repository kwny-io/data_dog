# LLM 治理与韧性：额度、准入、幂等、重试

> 这四件事都属于"出事时才发现没做"的类型：额度超了才看账单、队列爆了才发现没背压、
> 重复提交了才发现没幂等、上游抖一下才发现没重试。本文写清楚**它们各自在哪一层、怎么配、
> 出问题时的边界在哪**。观测（花了多少）见 `observability.py` 与 `docs/agent_cost_latency.md`；
> 本文管的是**治理**（还能不能花、能不能收）。

## 1. 一次调查的生命周期与三道闸门

```
POST /v1/investigations
   │
   ├─ ① 幂等占位（Idempotency-Key）      命中已完成 → 直接返回同一结果（不再花钱）
   │                                     命中进行中 → 409 + Retry-After
   ├─ ② 队列准入（DD_INVESTIGATE_QUEUE_MAX）  队列满 → 429 + Retry-After（**不落 job**）
   ├─ ③ 额度闸门（budget.ensure）        超限 → 429；被拦下也写运行日志（error_code=budget_exceeded）
   │
   └─ worker / 同步执行
         └─ 每次模型调用前：budget.ensure  ← 跑一半也会被叫停
                   调用后：budget.charge  ← 按 provider 真实 usage 记账
```

三道闸门**都排在花钱之前**。特别是队列与额度：**收下再拒等于假的受理**——调用方以为在排队，
其实只是在等一个注定失败的活。

## 2. 配置（全部默认值 = 加这套东西之前的行为）

| 配置 | 默认 | 作用 |
|---|---|---|
| `DD_LLM_BUDGET_TOKENS_PER_DAY` | `0`（不限） | 每租户每日 token 上限 |
| `DD_LLM_BUDGET_USD_PER_DAY` | `0`（不限） | 每租户每日花费上限（估算） |
| `DD_LLM_BUDGET_TOKENS_PER_MONTH` / `DD_LLM_BUDGET_USD_PER_MONTH` | `0` | 同上，月粒度 |
| `DD_LLM_BUDGET_ENFORCE` | `1` | `0` = **只记不拦**（灰度：先看几天真实用量再定阈值） |
| `DD_INVESTIGATE_QUEUE_MAX` | `0`（不限） | 异步队列准入上限；超限 429 |
| `DD_INVESTIGATE_IDEMPOTENCY_TTL_S` | `1800` | 幂等窗口；`0` = 关闭 |
| `DD_LLM_RETRIES` | `1` | 单次 `chat()` 的**额外**重试次数（0 = 不重试） |
| `DD_LLM_RETRY_BASE_MS` | `300` | 退避基数（`base*2^n × U(0.5,1.5)`） |
| `DD_LLM_FALLBACK_MODEL` | 空 | 重试耗尽后用它再试一次（降级而不是失败关闭） |

## 3. 语义细节（容易被问到的几条）

**幂等**
- 只在带 `Idempotency-Key` 头时生效；不带 = 完全旧行为。
- 窗口内第二次：已完成 → 返回**同一份**结果 + `idempotent_replay: true`；进行中 → `409`。
- 占位是**原子**的（`StateStore.mutate`），两个并发重复请求只有一个拿到"首次"——多副本同样成立。
- **失败会释放占位**（队列满 / 额度不足 / 执行失败）：否则调用方连"改完重试"的机会都没有。
- 过期靠**读时判断**（记 `at`），不依赖后端 TTL 原语；key 不绑定请求体（一个 key 只用于一次提交）。

**额度**
- 日/月各一个 key：`budget:{tenant}:day` / `:month`，**跨周期首次访问自动归零**——不需要定时任务。
- 扣费发生在**调用之后**、按 provider 的真实 usage（不是估算的预授权）。
- 因此单次调用**必然**可能超出上限（最多超一次调用），但**下一次调用前**一定被拦。
  要"零超出"得按 `max_tokens` 预授权——复杂度不抵收益，见 `budget.py` 的边界段。
- 金额按价格表**估算**，不是账单；未定价模型只计 token。阈值是**全局配置**，不是每租户合同。

**重试 / 降级**
- 包装顺序（外→内）：`Deadline → Retry → Budget → 真 provider`。
  这样每次重试都要**再过一遍总预算**（"没时间了"不会被重试放大）、也要**重新计费**（重试不白花）。
- 只重试 `LLMCallError.retryable`：超时 / 不可用 / 被限流；鉴权与入参错误不重试。
- 总预算耗尽的异常显式标 `retryable=False`——它是"没时间了"，不是"上游抖了一下"。
- 备用模型只在**重试都失败后**用一次，且要求调用方本来传了 `model`。

## 4. 指标（Prometheus）

| 指标 | 类型 | 用途 |
|---|---|---|
| `data_dog_llm_budget_exceeded_total{stage}` | Counter | 被额度拦下的次数；`stage` 区分 `precheck` / `midrun`（还没花钱 vs 跑一半被拦） |
| `data_dog_llm_budget_tokens_total` | Counter | 记入额度的 token 总量（**不带租户标签**，避免基数爆炸） |
| `data_dog_llm_budget_usd_total` | Counter | 同上，估算花费 |
| `data_dog_investigation_queue_depth` | Gauge | 队列深度——**准入阈值与 HPA 都用它**（`helm/templates/prometheusrule.yaml` 有告警规则） |

还剩多少额度：`budget.snapshot(tenant)`（`limits` + 当日/当月用量 + `exceeded`/`reason`）。

## 5. 诚实边界

- **额度是全局阈值**，不是每租户一份合同；计数器不随租户删除而消失（每租户固定 2 个 key）。
- **幂等键不绑定请求体**：同一个 key 配不同 `candidate_id` 会拿到首次的结果（与主流实现一致）。
- **队列上限是"深度"**，不是"每租户配额"：一个租户理论上可以把队列占满（要真公平得按租户配额入队，
  属下一步；当前至少做到**不假装受理**）。
- **重试不是重放**：重试的是"这次 HTTP 往返"，不会重跑已经成功的工具调用步骤。
- 单副本用 `memory` 状态后端时，额度/幂等**只在进程内**成立——多副本必须 `postgres`/`redis`（同限流）。

## 6. 回归防线

`tests/test_llm_governance.py`（13 条）：重试只重可重试错 / deadline 标记不被重试 / 备用模型降级 /
额度累计与跨周期归零 / `ENFORCE=0` 只记不拦 / 额度耗尽 429 **且留运行日志** / 队列满 429 **且不落 job** /
幂等单元态（new→inflight→done→过期→release 不删已完成）/ 同步与异步重复提交只真跑一次 /
不带 key 时行为不变。

```bash
pytest tests/test_llm_governance.py -q
```
