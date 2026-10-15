"""集中配置：所有环境变量的**唯一读取点**。

为什么要集中
------------
此前 `os.environ.get("DD_...")` 散落在十几个模块里，同一个语义可能出现
两种前缀（`DD_*` 与 `DATA_DOG_*`）——排查"这个值从哪来"要全仓搜。

设计取舍
--------
- **零新依赖**：纯 `os.environ` 读取，不引入 pydantic-settings。
- **调用时读取，绝不缓存**：测试大量使用 `monkeypatch.setenv` 后再调用，
  若在 import 时求值会全部失效。因此每个函数每次调用都重新读环境。
- **别名兼容**：`DATA_DOG_VENDOR` / `DATA_DOG_MODEL` 是历史前缀，保留为
  别名；规范名迁移到 `DD_LLM_VENDOR` / `DD_LLM_MODEL`。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


def _get(canonical: str, *aliases: str, default: str = "") -> str:
    """按顺序取第一个**非空**的环境变量值；都没有则返回 default。

    空串视为未设置（与项目里 `os.environ.get(x, "").strip() or default` 的
    既有惯例一致）——避免 `DD_X=""` 意外覆盖默认值。
    """
    for name in (canonical, *aliases):
        v = os.environ.get(name, "").strip()
        if v:
            return v
    return default


def _get_float(canonical: str, *aliases: str, default: float) -> float:
    raw = _get(canonical, *aliases)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_int(canonical: str, *aliases: str, default: int) -> int:
    raw = _get(canonical, *aliases)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_bool(canonical: str, *aliases: str, default: bool) -> bool:
    """布尔语义：显式 falsy 串判假，其余非空判真。"""
    raw = _get(canonical, *aliases)
    if not raw:
        return default
    return raw.lower() not in {"0", "false", "no", "off"}


# ---------------------------------------------------------------- 服务

def service_name() -> str:
    return _get("DD_SERVICE_NAME", default="data-dog")


def service_host() -> str:
    return _get("DD_SERVICE_HOST", default="0.0.0.0")


def service_port() -> int:
    return _get_int("DD_SERVICE_PORT", default=8000)


def service_workers() -> int:
    """uvicorn worker 进程数（仅 `python -m data_dog.service` 生效）。默认 1。

    为什么需要它（这是"单进程 P99 不达标"的正解之一）
    ------------------------------------------------
    本机实测（`docs/write_path_batching.md` §6）：单进程写入服务率约 **36 req/s**，
    而瓶颈是**每请求的数据库往返次数**（约 16 条 SQL / 5 次事务）× 单次往返 ~2~3ms
    ——不是 CPU、不是提交吞吐（PG 侧实测 76% 空闲、后端全在 `ClientRead`）。
    在"往返次数"这一项被结构性削减之前，靠**多进程**把服务率线性放大是唯一能立刻
    让中负载 P99 达标的旋钮（每进程一份解释器，绕开 GIL）。

    为什么默认 1、而不是按核数自动放大
    ----------------------------------
    多 worker 的**正确性前提是"有状态的东西都不在进程内"**。本项目已把状态外置
    （限流/候选分组走 `StateStore`、调查 job 队列在 DB、升级 sweeper 有租约），
    所以配了 postgres/redis 时多 worker 是安全的；但 `DD_STATE_BACKEND=memory`
    下会退化成"各 worker 各算各的"（限流各挡各的、候选分组各归各的）。
    故默认 1，且在 `__main__` 里对"多 worker + 进程内后端"**失败关闭**——
    宁可拒绝启动，也不静默给出错误的多副本语义。
    """
    return max(1, _get_int("DD_SERVICE_WORKERS", default=1))


def log_level() -> str:
    return _get("DD_LOG_LEVEL", default="INFO")


def log_json() -> bool:
    return _get_bool("DD_LOG_JSON", default=True)


def api_key() -> str:
    """对外暴露时**必须**设置的静态密钥；空串 = 不校验（本地开发）。"""
    return _get("DD_API_KEY")


@dataclass(frozen=True)
class TenantKey:
    """租户的一把密钥，**含生命周期**（② 密钥轮换/吊销）。

    为什么要把"密钥"从字符串升级成带 id + 生命周期的对象
    --------------------------------------------------
    静态字符串密钥有两个运维上绕不开的问题：**换不掉**、**撤不掉**。
    换 = 所有调用方必须同一瞬间改（没有交叠窗口，必然有中断）；撤 = 密钥泄露了
    只能全量替换 + 重启。带上 `key_id` 与 `not_after`/`revoked_at` 之后：

    - **轮换**：旧新两把**同时在册**（交叠窗口）→ 调用方逐个切换 → 旧那把置
      `revoked_at`。全程不需要调用方"同时改"，也不需要停机。
    - **吊销**：给 `revoked_at` 一个时间戳，**下一次请求立即失效**——因为
      `settings` 是"调用时读取、绝不缓存"（本模块的设计取舍），无需重启。
    - **可审计**：`key_id` 落进 `audit_log.key_id`，"谁用**哪把**钥匙做了什么"可查。
      这是把 ① 的安全审计面与密钥生命周期接起来的那根线——没有它，审计只能记
      一个指纹，"吊销的那把被用过"这件事在台账里认不出来。

    `key_id` 允许为空（v1 单密钥写法）：此时审计退回按**指纹**标识（与 ① 既有行为
    逐字节一致），这样"没配密钥环"的部署审计记录不会变。
    """

    tenant_id: str
    key_id: str
    secret: str
    not_after: float | None = None      # epoch 秒；None = 不设到期
    revoked_at: float | None = None

    def status(self, now: float) -> str:
        """`active` / `expired` / `revoked`。**吊销优先于过期**（更强的语义先答）。

        只用于运维视图与拒绝原因；**放行判断走 `active()`**，两者同源以免漂移。
        """
        if self.revoked_at is not None:
            return "revoked"
        if self.not_after is not None and now >= self.not_after:
            return "expired"
        return "active"

    def active(self, now: float) -> bool:
        return self.status(now) == "active"


def tenant_keyring() -> dict[str, tuple[TenantKey, ...]]:
    """租户密钥环 `{tenant_id: (TenantKey, ...)}`（`DD_TENANT_KEYS`，JSON 对象）。

    为什么需要它（补一个真实漏洞）
    ----------------------------
    `DD_API_KEY` 是**全局单一**密钥，而租户身份来自调用方自报的 `X-Tenant-Id` 请求头。
    于是**任何持密钥者改一个 header 就能以任意租户身份读写**——实测：同一把密钥
    写 `X-Tenant-Id: victim-a` 与 `victim-b` 均 200。RLS 与复合主键防的是
    "代码写错导致串租户"，防不了"持密钥者主动冒充"。

    两种写法（同一个环境变量，**v1 逐字节保持原语义**）：

        {"tenant-a": "static-key"}                    ← v1：单密钥（= 一把不过期的 key）
        {"tenant-b": {"keys": [                       ← v2：密钥环（可轮换/吊销）
            {"id": "2026-10", "secret": "new",
             "not_after": "2027-01-01T00:00:00Z"},
            {"id": "2026-07", "secret": "old",
             "revoked_at": "2026-10-01T00:00:00Z"}
        ]}}

    未配（默认）= 保持原有单密钥模式，向后兼容（本地开发/评测不受影响）。

    解析失败**绝不静默降级**（降级 = 退回单密钥模式 = 漏洞照旧）：抛 `ValueError`，
    由服务边界转 503，失败关闭。密钥环内部同样失败关闭，两条**看起来无害但会毁掉
    审计/轮换语义**的配置一律拒绝：
      - 同一租户内 `id` 重复 → 审计记下 `key_id` 也分不清是哪把钥匙；
      - 同一租户内两把 `secret` 相同 → "只吊销旧的那把"在实现上不可能成立
        （同一密钥值必然同时命中两把），轮换会静默退化成"没轮换"。
    """
    raw = _get("DD_TENANT_KEYS").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"DD_TENANT_KEYS 不是合法 JSON：{e}") from e
    if not isinstance(data, dict) or not data:
        raise ValueError("DD_TENANT_KEYS 必须是非空 JSON 对象 {租户id: 密钥}")
    out: dict[str, tuple[TenantKey, ...]] = {}
    for tenant, value in data.items():
        if not isinstance(tenant, str) or not tenant.strip():
            raise ValueError("DD_TENANT_KEYS 的租户 id 必须是非空字符串")
        out[tenant] = _parse_tenant_keys(tenant, value)
    return out


def resolve_tenant_key(tenant: str, provided: str, *,
                       now: float | None = None, keyring: dict[str, tuple[TenantKey, ...]]
                       | None = None) -> tuple[TenantKey | None, str]:
    """在租户密钥环里解析出示的密钥，返回 `(命中的 key 或 None, 判定)`。

    判定闭集：`active` / `revoked` / `expired` / `mismatch` / `unknown_tenant`。

    为什么要**分开回"命中但已吊销/过期"与"没命中"**
    ----------------------------------------------
    这两类对**调用方**含义完全不同：前者说明"你拿的是一把真钥匙，但它已失效"，
    调用方该去换钥匙；后者说明"这把钥匙根本不对"，该去查配置或怀疑泄露。
    且它**不构成信息泄露**——能拿到 `revoked` 的前提是已经出示了那把密钥本身，
    调用方本来就知道它的存在。租户枚举同样不受影响：`unknown_tenant` 与
    `mismatch` 在服务边界**回同一个状态码与同一个 reason**。

    为什么必须**全程常量时间、不许提前返回**
    ----------------------------------------
    逐个密钥用 `hmac.compare_digest` 比较，且**累积**结果而不是命中即 return：
    否则"第一把就中"比"最后一把才中"快，攻击者可以用时序差把**密钥在环里的位置**
    探出来（多密钥轮换期正好暴露"哪把是当前生效的"）。比较次数只与密钥环长度有关，
    与出示的值无关。
    """
    if now is None:
        import time
        now = time.time()
    ring = tenant_keyring() if keyring is None else keyring
    keys = ring.get(tenant)
    if not keys:
        return None, "unknown_tenant"

    import hmac
    matched: TenantKey | None = None
    for k in keys:
        # 不短路：每一把都要比，命中只做记录（见 docstring 的时序说明）。
        if hmac.compare_digest(provided.encode("utf-8"), k.secret.encode("utf-8")):
            matched = k
    if matched is None:
        return None, "mismatch"
    return matched, matched.status(now)


def _parse_tenant_keys(tenant: str, value: Any) -> tuple[TenantKey, ...]:
    """v1（字符串）或 v2（`{"keys": [...]}`）→ 规整的密钥元组。"""
    if isinstance(value, str):
        if not value:
            raise ValueError(f"DD_TENANT_KEYS[{tenant!r}] 的密钥必须是非空字符串")
        return (TenantKey(tenant_id=tenant, key_id="", secret=value),)

    if not isinstance(value, dict):
        raise ValueError(
            f"DD_TENANT_KEYS[{tenant!r}] 必须是密钥字符串，或 {{\"keys\": [...]}} 对象")

    entries = value.get("keys")
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"DD_TENANT_KEYS[{tenant!r}].keys 必须是非空数组")

    keys: list[TenantKey] = []
    seen_ids: set[str] = set()
    seen_secrets: set[str] = set()
    for i, ent in enumerate(entries):
        if not isinstance(ent, dict):
            raise ValueError(f"DD_TENANT_KEYS[{tenant!r}].keys[{i}] 必须是对象")
        secret = ent.get("secret")
        if not isinstance(secret, str) or not secret:
            raise ValueError(
                f"DD_TENANT_KEYS[{tenant!r}].keys[{i}].secret 必须是非空字符串")
        key_id = ent.get("id", "")
        if not isinstance(key_id, str):
            raise ValueError(f"DD_TENANT_KEYS[{tenant!r}].keys[{i}].id 必须是字符串")
        key_id = key_id.strip()
        if key_id:
            if key_id in seen_ids:
                raise ValueError(
                    f"DD_TENANT_KEYS[{tenant!r}] 的 key id 重复：{key_id!r}"
                    "（审计将无法区分是哪把钥匙）")
            seen_ids.add(key_id)
        if secret in seen_secrets:
            raise ValueError(
                f"DD_TENANT_KEYS[{tenant!r}] 出现重复的密钥值：轮换时旧新必须是两把"
                "不同的密钥，否则「只吊销旧的那把」不成立，轮换会静默失效")
        seen_secrets.add(secret)
        keys.append(TenantKey(
            tenant_id=tenant, key_id=key_id, secret=secret,
            not_after=_parse_ts(tenant, i, "not_after", ent.get("not_after")),
            revoked_at=_parse_ts(tenant, i, "revoked_at", ent.get("revoked_at")),
        ))
    return tuple(keys)


def _parse_ts(tenant: str, idx: int, field: str, raw: Any) -> float | None:
    """ISO-8601 → epoch 秒（`Z` 按 UTC 解释，与全仓时间口径一致）。空值 = 不设。"""
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise ValueError(
            f"DD_TENANT_KEYS[{tenant!r}].keys[{idx}].{field} 必须是 ISO-8601 字符串")
    txt = raw.strip()
    try:
        dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(
            f"DD_TENANT_KEYS[{tenant!r}].keys[{idx}].{field} 不是合法 ISO-8601："
            f"{txt!r}") from e
    if dt.tzinfo is None:                       # 无时区 → 按 UTC（不猜本地时区）
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def device_signing() -> str:
    """设备身份签名档位（`DD_DEVICE_SIGNING`，默认 `off`）。

    三档，按"能渐进上线"设计（细节见 `data_plane/device_signature.py`）：
      - `off`（默认）：完全不验 —— **行为与改动前逐字节一致**（向后兼容，零回归）；
      - `optional`：**带了签名就必须对**（错的一律拒），没带则放行 —— 渐进上线用，
        可以一个设备一个设备地开，不需要"全网同一天切换"；
      - `required`：必须带且必须对；设备没配密钥也拒（失败关闭）。

    取值非法 → 抛 `ValueError`（由服务边界转 503 失败关闭）。**绝不把拼错的值当成
    `off`**：那会让"我明明开了强制签名"变成"其实一直没验"，是最危险的一类静默降级。
    """
    raw = _get("DD_DEVICE_SIGNING", default="off").strip().lower()
    if raw not in ("off", "optional", "required"):
        raise ValueError(
            f"DD_DEVICE_SIGNING 取值非法：{raw!r}（可用 off / optional / required）")
    return raw


def device_keys() -> dict[str, str]:
    """每设备密钥表 `{device_sn: secret}`（`DD_DEVICE_KEYS`，JSON 对象）。

    与租户密钥环分开配置：设备密钥**量大、生命周期短、由设备侧持有**，和"租户/调用方"
    的密钥不是一类东西（混在一张表里会让轮换粒度错位——换一把设备密钥不该影响租户）。
    未配（默认）= 没有设备可验签；此时 `required` 档下所有请求都会被拒（失败关闭，
    因为"要求签名却没有密钥"意味着配置没做完）。

    解析失败**不静默降级**（降级 = 退回不验签）：抛 `ValueError` → 服务边界 503。
    """
    raw = _get("DD_DEVICE_KEYS").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"DD_DEVICE_KEYS 不是合法 JSON：{e}") from e
    if not isinstance(data, dict):
        raise ValueError("DD_DEVICE_KEYS 必须是 JSON 对象 {设备序列号: 密钥}")
    out: dict[str, str] = {}
    for sn, secret in data.items():
        if not isinstance(sn, str) or not sn.strip():
            raise ValueError("DD_DEVICE_KEYS 的设备序列号必须是非空字符串")
        if not isinstance(secret, str) or not secret:
            raise ValueError(f"DD_DEVICE_KEYS[{sn!r}] 的密钥必须是非空字符串")
        out[sn] = secret
    return out


def tenant_export_limit() -> int:
    """`GET /v1/tenant/export` 每表最多导出多少行（`DD_TENANT_EXPORT_LIMIT`，默认 1000）。

    为什么必须**有上限**：导出是"一个请求把某租户的全部历史拉走"。没有上限时，
    一个存量很大的租户会把内存、响应体、以及数据库连接一起拖垮（而导出端点本身
    又常常是合规审计时被反复调用的）。有界 + 在响应里显式回 `truncated`，
    比"看起来能导出全量、实际把服务打挂"诚实得多。
    """
    return max(1, _get_int("DD_TENANT_EXPORT_LIMIT", default=1000))


def retention_days() -> dict[str, int]:
    """各表的保留天数（`DD_RETENTION_*`）。**0 = 关闭该表的清理**（默认不动）。

    为什么默认值必须是"不删"而不是"删"
    ----------------------------------
    清理是**破坏性、不可逆**的动作。默认值决定"忘了配会不会出事"：把某个默认值设成
    正数，就等于**默认销毁**那类数据。所以这里按"删了能不能复原"分档：

    - **原始日志（7 天）/ 派生候选（90 天）**：可从设备重灌、且 `docs/slo.md` §6 明确
      声明了保留期 → 默认**执行**。这是"声明了就得真的做"的那一半。
    - **告警 + 报告 + 投递台账（0）/ 安全审计（0）**：删了**不可复原**，而且是业务/合规
      台账本身（`slo.md` 只说审计"≥1 年"这个下界，没给上界）→ 默认**不删**，要删必须
      由人显式开启。审计尤其如此：销毁审计记录**本身就是**合规风险。
    - **已终态调查 job（7 天）**：`docs/delivery.md` 的差距表点名"job 表仍需保留策略"，
      且 job 只是一次执行的记录（结论已落在 `reports`/`alerts`，删除不丢真相）→ 默认执行。

    单位是天；`now - days*86400` 即截止时间。时间列一律是 ingestion 侧的 epoch 秒
    （`created_at` / `ts` / `finished_at`）——用**事件时间**做保留会被乱序/补传的历史数据
    绕过（一条 2019 年的时间戳会让它永远"年轻"）。
    """
    return {
        "telemetry_events": max(0, _get_int("DD_RETENTION_RAW_DAYS", default=7)),
        "candidates": max(0, _get_int("DD_RETENTION_CANDIDATE_DAYS", default=90)),
        "investigation_job": max(0, _get_int("DD_RETENTION_JOB_DAYS", default=7)),
        # 运行日志是**派生的执行记录**（不是业务台账），但删了**不能复原**——重跑一次
        # 得到的轨迹不会与当时相同。故不跟 job 的 7 天，而与"派生候选"同档 90 天，
        # 覆盖事故复盘的常见窗口；要永久保留就设 0（0 = 关闭该表的清理）。
        "investigation_run_log": max(0, _get_int("DD_RETENTION_RUN_LOG_DAYS", default=90)),
        "alerts": max(0, _get_int("DD_RETENTION_ALERT_DAYS", default=0)),
        "audit_log": max(0, _get_int("DD_RETENTION_AUDIT_DAYS", default=0)),
    }


def retention_lease() -> bool:
    """保留清理是否先抢**单持有者租约**（DD_RETENTION_LEASE，默认 1 = 抢）。

    为什么：清理本身幂等（同一批删两次结果一样），但多副本同时跑会互相锁行、制造 I/O 尖峰，
    而且"谁在删"没人知道。抢不到就**本轮跳过**（不是失败）——下一轮再来。
    单机直跑想省掉一步可设 0。
    """
    return _get_bool("DD_RETENTION_LEASE", default=True)


def retention_lease_ttl_s() -> float:
    """清理租约 TTL（秒，DD_RETENTION_LEASE_TTL_S，默认 300）。

    TTL 即"持有者崩了之后多久别人能接管"：太长则崩溃后空窗久，太短则长跑会被抢。
    默认给得比一轮清理宽裕（单轮通常秒级，批数用满时可能分钟级）。
    """
    return max(1.0, _get_float("DD_RETENTION_LEASE_TTL_S", default=300.0))


def public_base_url() -> str:
    """对外可访问的服务基址（`DD_PUBLIC_BASE_URL`，如 https://dog.example.com）。

    唯一用途：把"这次调查的运行日志在哪"写进**告警通知**，让值班人收到告警就能回到
    诊断过程（见 alerting/notifiers.build_notification）。留空则通知里只给**相对路径**
    ——不猜部署，也不把内网地址写进外发消息。
    """
    return _get("DD_PUBLIC_BASE_URL").rstrip("/")


def cors_origins() -> list[str]:
    """允许的跨源来源（逗号分隔）。空列表 = 不挂载 CORS 中间件（同源）。"""
    raw = _get("DD_CORS_ORIGINS")
    return [o.strip() for o in raw.split(",") if o.strip()]


# ---------------------------------------------------------------- 数据库

def database_url() -> str:
    """连接串；未设则本地 SQLite（由 storage.db 兜底具体路径）。"""
    return _get("DD_DATABASE_URL")


def auto_migrate() -> bool:
    """服务启动时是否自动 `alembic upgrade head`。生产多副本应设 0。"""
    return _get_bool("DD_AUTO_MIGRATE", default=True)


# 为什么这三个超时默认 5s（而不是库默认的"无限/30s"）：见 storage/db.py 的
# `engine_kwargs` docstring。要点是——建连/拿连接的等待必须有上界，且上界要与
# 本仓其它"快速失败"预算同量级（redis 状态冲突重试 5s、通知 HTTP 5s），
# 这样"数据库慢了"会以显式错误暴露，而不是把请求线程静默钉住。

def db_connect_timeout_s() -> float:
    """建连超时（秒，仅 PostgreSQL 生效）：TCP 建连 + 启动握手的总预算。

    libpq 默认 `connect_timeout=0` = **无限等**（最终靠 OS 的 TCP 超时，可达分钟级），
    故必须显式给值。有上限之后，"连不上"会以 `OperationalError`（connection timeout
    expired）快速浮出来，而不是让请求线程在 SYN 重传里无声等待。
    """
    return max(0.1, _get_float("DD_DB_CONNECT_TIMEOUT_S", default=5.0))


def db_pool_timeout_s() -> float:
    """从连接池取连接的最长等待（秒，仅 PostgreSQL 生效）。

    SQLAlchemy 默认 30s：池被占满时请求会**静默排队半分钟**再报错。改成 5s 是
    "服务器过载就快速失败"的选择——快速 503 比慢 200 更早暴露容量问题。
    """
    return max(0.1, _get_float("DD_DB_POOL_TIMEOUT_S", default=5.0))


def db_statement_timeout_ms() -> int:
    """单条语句最长时间（毫秒，仅 PostgreSQL 生效）；**0 = 不限**。

    服务端 `statement_timeout`：卡死的查询/被锁住的写不会永久占着连接。默认 15s
    远高于应用查询的量级（docs/slo.md 的热路径预算是毫秒级），只用来兜住"病态慢"。
    注意：alembic 迁移走自己的引擎（storage/migrations.py），不受此值约束，
    长迁移不会被误杀。
    """
    return max(0, _get_int("DD_DB_STATEMENT_TIMEOUT_MS", default=15_000))


def db_pool_size() -> int:
    """连接池常驻连接数（仅 PostgreSQL 生效）。默认 10（SQLAlchemy 默认只有 5）。

    为什么必须可配：**池容量就是并发上限**。实测（`docs/write_path_batching.md`）
    同库同目标 RPS、只把并发从 60 降到 15（= 当时的池容量 5+10）：
    **吞吐 72 → 73 req/s 几乎不变，而 P99 1778 → 290ms（6.1×）**——多出来的线程
    不换来任何吞吐，只堆在池门口。抬高池容量是把这段排队还给服务能力的最省事一步。
    """
    return max(1, _get_int("DD_DB_POOL_SIZE", default=10))


def db_max_overflow() -> int:
    """池满后允许临时新建的额外连接数（仅 PostgreSQL 生效）。默认 10。

    容量上界 = `pool_size + max_overflow`。**多副本部署必须满足**
    `(pool_size + max_overflow) × 副本数 < PG max_connections`（默认 100），
    否则池会去抢一个不存在的连接，表现为**建连超时**而不是排队——比排队更难定位。
    """
    return max(0, _get_int("DD_DB_MAX_OVERFLOW", default=10))


def sqlite_wal() -> bool:
    """SQLite 是否启用 WAL + `synchronous=NORMAL`（默认**开**）。

    默认的 rollback journal + `synchronous=FULL` 下**每次 COMMIT 一次 fsync**，
    而且写会阻塞读：实测单机写入只有 36 req/s、P99 8.0s（`docs/write_path_batching.md`）。
    WAL 让读不再被写阻塞。

    `synchronous=NORMAL` 在 WAL 下**仍然是"提交即持久"**（进程崩溃不丢已提交数据），
    只是**操作系统崩溃/断电**可能丢最后几个已提交事务——这是 SQLite 文档推荐的标准组合，
    但确实是对耐久性的取舍，故给开关（`DD_SQLITE_WAL=0` 回到旧行为）。
    """
    return _get_bool("DD_SQLITE_WAL", default=True)


# ---------------------------------------------------------------- 共享状态

def state_backend() -> str:
    """跨副本共享状态后端：memory / postgres / redis。空=按数据库类型自动选。"""
    return _get("DD_STATE_BACKEND").lower()


def rate_backend() -> str:
    """限流后端（可独立于状态后端）。空 = 跟随 `DD_STATE_BACKEND`。

    拆分场景（限流检查成为热路径瓶颈时）：`DD_RATE_BACKEND=redis` 让限流走
    Redis（sub-ms + 原生 TTL），业务状态仍走 PG（耐久 + 可查询）。默认不拆，
    两者同源，少运维一个组件。
    """
    return _get("DD_RATE_BACKEND").lower()


def redis_url() -> str:
    """Redis 连接串（DD_STATE_BACKEND=redis 时需要）。"""
    return _get("DD_REDIS_URL", default="redis://localhost:6379/0")


# ---------------------------------------------------------------- 状态快照（G3）

def checkpoint_enabled() -> bool:
    """是否启用周期状态快照（默认关；多副本/需恢复时开）。"""
    return _get_bool("DD_CHECKPOINT_ENABLED", default=False)


def checkpoint_interval_s() -> float:
    """周期快照间隔（秒）。也是恢复的 RPO 上界（两次快照间重启会丢这段增量）。"""
    return max(5.0, _get_float("DD_CHECKPOINT_INTERVAL_S", default=300.0))


def checkpoint_keep() -> int:
    """保留最近多少份快照（防表无限增长）。"""
    return max(1, _get_int("DD_CHECKPOINT_KEEP", default=5))


def checkpoint_restore() -> bool:
    """启动时是否从最新快照恢复状态。"""
    return _get_bool("DD_CHECKPOINT_RESTORE", default=False)


def checkpoint_prefix() -> str:
    """快照只导出此前缀匹配的 key（空 = 全部状态）。"""
    return _get("DD_CHECKPOINT_PREFIX")


def ops_token() -> str:
    """运维端点令牌（`X-Ops-Token`）。空 = 运维端点不暴露（404）。

    状态快照/diff 涉及各租户算子状态，属敏感数据，故**独立**于 `DD_API_KEY`
    门控——即便开了 API 鉴权，运维端点也只对持 ops token 者开放。
    """
    return _get("DD_OPS_TOKEN")


# ---------------------------------------------------------------- 流式接入

def stream_enabled() -> bool:
    """是否在进程内启动流式接入消费者。"""
    return _get_bool("DD_STREAM_ENABLED", default=False)


def stream_shards() -> int:
    """流式消费者的**副本数**（K8s 里 `deployment-stream` 的 replicas）。

    这个值**不在消费进程内决定并行度**，而是为了让进程自检：
    file 源（单文件、无协调）配了多副本 → 启动即失败关闭（见 stream/factory.py），
    免得多个副本各尾随一份、事件被重复处理而无人察觉。
    多副本只对 kafka（消费者组）/ mqtt（共享订阅）安全。
    """
    return max(1, _get_int("DD_STREAM_SHARDS", default=1))


def stream_source() -> str:
    """流式源类型：file / http / mqtt / kafka。"""
    return _get("DD_STREAM_SOURCE", default="file").lower()


def stream_tenant() -> str:
    """流式信道的租户（身份来源）。本地文件/总线不带应用层身份，只能从配置取。"""
    return _get("DD_STREAM_TENANT", default="demo")


def stream_device_sn() -> str:
    """流式信道的默认设备号（正文取不到 device 时的兜底）。"""
    return _get("DD_STREAM_DEVICE_SN")


def stream_device_from_field() -> str:
    """从每条消息正文的该字段取 device_sn（一个信道承载多台设备时用）。"""
    return _get("DD_STREAM_DEVICE_FIELD")


def stream_http_host() -> str:
    return _get("DD_STREAM_HTTP_HOST", default="127.0.0.1")


def stream_http_port() -> int:
    return _get_int("DD_STREAM_HTTP_PORT", default=8099)


def stream_http_path() -> str:
    return _get("DD_STREAM_HTTP_PATH", default="/ingest")


def stream_file_path() -> str:
    """file 源：尾随的日志文件/目录路径。"""
    return _get("DD_STREAM_FILE_PATH")


def stream_poll_ms() -> int:
    """file 源：轮询间隔（毫秒）。"""
    return _get_int("DD_STREAM_POLL_MS", default=500)


def stream_mqtt_url() -> str:
    return _get("DD_STREAM_MQTT_URL", default="tcp://localhost:1883")


def stream_mqtt_topic() -> str:
    return _get("DD_STREAM_MQTT_TOPIC", default="telemetry/#")


def stream_kafka_bootstrap() -> str:
    return _get("DD_STREAM_KAFKA_BOOTSTRAP", default="localhost:9092")


def stream_kafka_topic() -> str:
    return _get("DD_STREAM_KAFKA_TOPIC", default="telemetry")


def stream_kafka_group() -> str:
    return _get("DD_STREAM_KAFKA_GROUP", default="data-dog")


# ---------------------------------------------------------------- 车队级分片（P4-5）

def stream_replica_id() -> str:
    """本副本的 id（分片路由用）。缺省空串——单副本场景无需配。"""
    return _get("DD_REPLICA_ID")


def stream_replica_members() -> list[str]:
    """集群里所有消费者副本 id（逗号分隔）。所有副本必须看到**相同**的列表，
    否则 rendezvous 归属判定不一致 → 丢事件。单副本留空即可。"""
    raw = _get("DD_REPLICA_MEMBERS")
    return [m.strip() for m in raw.split(",") if m.strip()] if raw else []


def stream_shard_count() -> int:
    """分片总数（远大于副本数，便于均衡与再平衡）。"""
    return max(1, _get_int("DD_SHARD_COUNT", default=256))


# ---------------------------------------------------------------- 跨地域多活（P4-6）

def region() -> str:
    """本实例所在区域标识（如 `cn-east` / `cn-north`）。

    用于度量与运维归属（复制延迟标签 `from`/`to`）。缺省空串=未分区（单区部署，
    与现状行为一致）。**不参与**数据路由——路由按租户/车队就近归属，是编排层决策。
    """
    return _get("DD_REGION")


def replication_source() -> str:
    """本实例的复制**上游**区域（订阅谁的变更）。空串=非复制部署。

    只作部署标注与可观测性用途；真正的复制由 PG 逻辑复制 / CDC 承担
    （见 `docs/p4/06_multi_region.md`），**非应用代码**。
    """
    return _get("DD_REPLICATION_SOURCE")


def replication_lag_warn_s() -> float:
    """复制延迟告警阈值（秒）：`dd_replication_lag_seconds` 超过即应告警。

    异步复制下延迟本就 >0；此阈值界定"可接受 vs 需介入"，供 runbook 使用。
    """
    return max(0.0, _get_float("DD_REPLICATION_LAG_WARN_S", default=30.0))


# ---------------------------------------------------------------- 多副本协调（G5）

def stream_mqtt_share_group() -> str:
    """MQTT 共享订阅组名：拼成 `$share/<group>/<topic>`，broker 在组内轮询投递。

    空串 = 不用共享订阅（退回"每个副本收全量"——单副本可接受，多副本会重复）。
    """
    return _get("DD_MQTT_SHARE_GROUP", default="data-dog")


def stream_file_lease_ttl_s() -> float:
    """file 源租约 TTL（秒）：持有者崩溃后至多这么久被旁人接管。"""
    return max(1.0, _get_float("DD_STREAM_LEASE_TTL_S", default=30.0))


def stream_share_offset() -> bool:
    """file 源偏移是否共享（接管者从上次共享偏移续读，而非从头/从尾）。"""
    return _get_bool("DD_STREAM_SHARE_OFFSET", default=True)


# ---------------------------------------------------------------- 限流

def rate_per_sec() -> float:
    return _get_float("DD_RATE_PER_SEC", default=200.0)


def rate_burst() -> int:
    return _get_int("DD_RATE_BURST", default=400)


# ---------------------------------------------------------------- LLM

def llm_vendor() -> str:
    return _get("DD_LLM_VENDOR", "DATA_DOG_VENDOR", default="deepseek")


def llm_model() -> str:
    return _get("DD_LLM_MODEL", "DATA_DOG_MODEL")


def llm_price_in() -> float:
    """自定义端点的**输入价**（USD / 1M tokens）。0 = 未声明。

    价格表只内置了三家公开定价；自带端点（DD_LLM_VENDOR=custom）由使用者声明，
    否则成本一律显示"未定价"（cost_estimated=True），**绝不假装知道价钱**。
    """
    return max(0.0, _get_float("DD_LLM_PRICE_IN", default=0.0))


def llm_price_out() -> float:
    """自定义端点的**输出价**（USD / 1M tokens）。0 = 未声明。"""
    return max(0.0, _get_float("DD_LLM_PRICE_OUT", default=0.0))


def llm_base_url() -> str:
    """自定义 OpenAI 兼容端点的 base_url（`DD_LLM_VENDOR=custom` 时用）。

    产品落地：使用者自带模型/网关，只要兼容 OpenAI 协议即可接入。空 = 未配置
    （用内置三家时无需设置）。见 providers/registry.py 的 custom 分支。
    """
    return _get("DD_LLM_BASE_URL", "DATA_DOG_BASE_URL")


def llm_timeout_s() -> float | None:
    """LLM 调用超时（秒）。0 或负值 = 不限时（不推荐）。未设 = 60s。"""
    raw = _get("DD_LLM_TIMEOUT_S")
    if not raw:
        return 60.0
    try:
        v = float(raw)
    except ValueError:
        return 60.0
    return None if v <= 0 else v


# ---------------------------------------------------------------- 调查端点

def investigate_timeout_s() -> float:
    return _get_float("DD_INVESTIGATE_TIMEOUT_S", default=90.0)


def investigate_max_steps() -> int:
    return _get_int("DD_INVESTIGATE_MAX_STEPS", default=8)


def investigate_offline() -> bool:
    """强制用离线确定性 provider（CI/测试用，不调真实 LLM）。"""
    return _get_bool("DD_INVESTIGATE_OFFLINE", default=False)


def llm_retries() -> int:
    """单次模型调用的**额外重试次数**（DD_LLM_RETRIES，默认 1，0 = 不重试）。

    为什么默认 1 而不是 0：providers/client.py 刻意设了 SDK 的 `max_retries=0`——把重试
    预算收回**我们自己的** deadline，那是对的一半；错的一半是"我们自己的重试一直没写"，
    于是上游 504/连接重置一次就把整次调查打成失败关闭（还多堆一条人工工单）。
    只重试可重试的错（超时/不可用/被限流），且每次尝试都要再过一遍 deadline。
    """
    return max(0, _get_int("DD_LLM_RETRIES", default=1))


def llm_retry_base_ms() -> int:
    """重试退避基数（毫秒，DD_LLM_RETRY_BASE_MS，默认 300）：第 n 次等待
    `base * 2^(n-1)` 再加 50%~150% 抖动（避免多副本同步重试打上游）。"""
    return max(0, _get_int("DD_LLM_RETRY_BASE_MS", default=300))


def llm_fallback_model() -> str:
    """备用模型（DD_LLM_FALLBACK_MODEL，默认空 = 不降级）。

    主模型在重试次数内都没成功时，用它再试**一次**——"降级到一个能用的模型"
    比"整次调查失败关闭"对使用者更有价值。留空即维持旧行为。
    """
    return _get("DD_LLM_FALLBACK_MODEL", default="")


def llm_budget_tokens_per_day() -> int:
    """每租户**每日** token 上限（DD_LLM_BUDGET_TOKENS_PER_DAY）。0 = 不限（默认）。

    为什么默认不限：加这个模块之前的行为必须一字不变；治理是**按需开启**的能力，
    不是替使用者拍一个额度。开启后额度耗尽会 fail-closed（429），见 budget.py。
    """
    return max(0, _get_int("DD_LLM_BUDGET_TOKENS_PER_DAY", default=0))


def llm_budget_usd_per_day() -> float:
    """每租户**每日**花费上限（USD，估算值）。0 = 不限（默认）。"""
    return max(0.0, _get_float("DD_LLM_BUDGET_USD_PER_DAY", default=0.0))


def llm_budget_tokens_per_month() -> int:
    """每租户**每月** token 上限。0 = 不限（默认）。"""
    return max(0, _get_int("DD_LLM_BUDGET_TOKENS_PER_MONTH", default=0))


def llm_budget_usd_per_month() -> float:
    """每租户**每月**花费上限（USD，估算值）。0 = 不限（默认）。"""
    return max(0.0, _get_float("DD_LLM_BUDGET_USD_PER_MONTH", default=0.0))


def llm_budget_enforce() -> bool:
    """额度耗尽是否**拦下**（DD_LLM_BUDGET_ENFORCE，默认 1 = 拦）。

    置 0 = 只记用量不拦（灰度：先看几天真实用量，再决定阈值）。
    """
    return _get_bool("DD_LLM_BUDGET_ENFORCE", default=True)


def investigate_queue_max() -> int:
    """异步调查队列**准入上限**（DD_INVESTIGATE_QUEUE_MAX，默认 0 = 不限制）。

    超限 → POST /v1/investigations 回 429 + Retry-After，而不是收下再排队。
    为什么需要：队列是 state store 里的一个 list，**没有上限**；提交快于消费时
    "已受理"是假的——用户以为在排队，其实只是一直在等。准入控制要把背压显式化。
    """
    return max(0, _get_int("DD_INVESTIGATE_QUEUE_MAX", default=0))


def investigate_idempotency_ttl_s() -> float:
    """提交幂等窗口（秒，DD_INVESTIGATE_IDEMPOTENCY_TTL_S，默认 1800）。

    同一个 (租户, Idempotency-Key) 在窗口内重复提交 → 返回**同一个** job/报告，
    不重复调模型。置 0 = 关闭幂等（回退到旧行为）。
    """
    return max(0.0, _get_float("DD_INVESTIGATE_IDEMPOTENCY_TTL_S", default=1800.0))


def investigate_async() -> bool:
    """`POST /v1/investigations` 默认是否异步（提交→202 job 句柄）。

    默认 **True**（2026-10-14 架构评审 B2 翻转）：异步化是把 HTTP worker 从长 LLM 调用里
    解放出来的关键，而控制台已改造成**两种模式都认**（同步结果直接用 / 202 就轮询 job 再取
    `.../result`），所以默认异步不再破坏前端。调用方仍可用 `?sync=1` 单独退回同步。

    **代价（如实说明）**：默认异步意味着**每个应用实例**启动时都会拉起一个 worker 线程池 +
    队列轮询线程（`service/runtime.py`）。单副本或纯 API 实例不需要时，置
    `DD_INVESTIGATE_ASYNC=0` 退回同步。多副本还必须配 `DD_STATE_BACKEND=postgres`——memory
    后端的队列只活在进程内（`data-dog config check` 会对"异步 + memory"报警告）。
    """
    return _get_bool("DD_INVESTIGATE_ASYNC", default=True)


def run_log_full_steps() -> str:
    """运行日志落库时保留多少轨迹步骤（DD_RUN_LOG_FULL_STEPS，默认 auto）。

    - **auto**（默认）：**成功且已判定**只落"调了什么工具"的骨架（步号 + 工具名），
      失败/取消、以及结论为 needs_human_review 的落**全文**（入参 + 结果）。
      理由：一次调查最多 40 步、单步 4 KB，重跑多的候选会把库撑大；而排查价值集中在
      "失败 / 拿不准"这两类——成功那次结论与证据都在 report 里。
    - **always**：一律落全文（需要完整轨迹留档时用）。
    - **never**：一律只落骨架（最省空间，但失败排查也会缺入参/结果）。

    非法值按 auto 处理：日志级别的开关不值得让请求失败关闭。
    """
    v = _get("DD_RUN_LOG_FULL_STEPS", default="auto").strip().lower()
    return v if v in ("auto", "always", "never") else "auto"


# ---------------------------------------------------------------- 原始日志正文读取
# `rawlog://<event_id>` 的正文存放位置：**目录**与**对象存储**二选一（互斥，由
# `evidence/rawlog.py` 与 `data-dog config check` 双向把关）。两者都不配 = 失败关闭，
# 不会退回到任何 mock 数据。


def rawlog_base_dir() -> str:
    """原始日志正文的**受控目录**（DD_RAWLOG_BASE_DIR，默认空 = 不启用本地读取）。

    目录内的相对键由 `telemetry_events.raw_log_ref` 给出；键在目录内解析（拒绝绝对路径、
    `..`、符号链接逃逸）。假定该目录**不可被非受信方写入**（本地 TOCTOU 见 rawlog.py 边界）。
    """
    return _get("DD_RAWLOG_BASE_DIR", default="").strip()


def rawlog_base_url() -> str:
    """原始日志正文的 http(s) 对象存储前缀（DD_RAWLOG_BASE_URL，默认空）。

    取数走 `data_plane.fetch.safe_request`（逐跳复验重定向 + 连接钉在已校验 IP 上），
    默认**不允许私网地址**——要指内网对象存储得显式开 DD_RAWLOG_ALLOW_PRIVATE=1。
    """
    return _get("DD_RAWLOG_BASE_URL", default="").strip()


def rawlog_max_bytes() -> int:
    """单条原始日志正文的读取上限（DD_RAWLOG_MAX_BYTES，默认 64 KiB）。

    默认与取数层 `_MAX_BYTES` 同量级：http 模式下取数层自身读满即止，配得比它大也只是
    "可能被截断"的保守标注（见 rawlog.py）。超限**截断并显式标注**，不是静默丢弃。
    """
    return max(1, _get_int("DD_RAWLOG_MAX_BYTES", default=65536))


def rawlog_timeout_s() -> float:
    """原始日志正文取数超时（DD_RAWLOG_TIMEOUT_S，默认 5s）。"""
    return max(0.1, _get_float("DD_RAWLOG_TIMEOUT_S", default=5.0))


def rawlog_allow_private() -> bool:
    """是否允许 rawlog 取数指向私网/本机（DD_RAWLOG_ALLOW_PRIVATE，默认 False）。"""
    return _get_bool("DD_RAWLOG_ALLOW_PRIVATE", default=False)


# ---- 分区维护（A9）

def partition_auto_ensure() -> bool:
    """启动时是否自动补齐未来分区（DD_PARTITION_AUTO_ENSURE，默认 **False**）。

    默认关：分区是**结构变更（DDL）**，生产应由集群层面的定时任务做（helm 的
    `partition.enabled` CronJob），不该由应用副本在启动时偷偷改结构。开了只是多一层保险，
    且失败不阻止启动（由 `data_dog_partition_end_timestamp_seconds` + 告警兜底）。
    """
    return _get_bool("DD_PARTITION_AUTO_ENSURE", default=False)


def partition_ensure_months() -> int:
    """自动补齐时"本月起往后建几个月"（DD_PARTITION_ENSURE_MONTHS，默认 3）。"""
    return max(1, _get_int("DD_PARTITION_ENSURE_MONTHS", default=3))


def investigate_workers() -> int:
    """异步调查 worker 池大小（进程内线程数）。"""
    return max(1, _get_int("DD_INVESTIGATE_WORKERS", default=4))


def investigate_queue_poll_ms() -> int:
    """worker 空转时查共享队列的间隔（毫秒）。"""
    return max(20, _get_int("DD_INVESTIGATE_QUEUE_POLL_MS", default=200))

def write_behind() -> bool:
    """是否把**业务落库**移出请求路径（write-behind 队列 + 后台写入线程）。

    默认 **False**：保持既有同步语义（响应返回前落库完成）。置 1 后
    `POST /v1/telemetry` 只把落库任务入队就返回——**HTTP 200 不再代表已落库**。

    ⚠️ 语义代价见 `docs/write_path_batching.md` §10：队列满 → 503 失败关闭；
    后台写失败/进程被杀 → **已返回的 200 无法追回**（数据丢失）。它是"用持久性
    换延迟"的显式取舍，只在可容忍少量丢失的部署里开，并须对
    `data_dog_write_behind_total{result=~"failed|rejected_full"}` 告警。
    """
    return _get_bool("DD_WRITE_BEHIND", default=False)


def write_behind_queue_size() -> int:
    """write-behind 队列容量（条数）。满了就 503 失败关闭，不静默丢。"""
    return max(1, _get_int("DD_WRITE_BEHIND_QUEUE", default=1000))


def visual_evidence_dir() -> str:
    """无损视觉记忆的本地目录（VISTA-1 式）。空 = 不启用（默认，零回归）。

    设了就启用视觉证据通道：Agent 可检索 / 调取 / 放大候选窗口内设备相机的原始帧。
    ⚠️ 裁剪需要 Pillow（可选依赖 `.[vision]`），缺失时视觉工具不暴露；而且要**多模态
    provider**才真能"看到"图像——纯文本 provider 不要开（本参数只决定证据是否可得，
    不判断模型能力）。
    """
    return _get("DD_VISUAL_EVIDENCE_DIR", default="")


def investigate_recover_interval_s() -> float:
    """异步 job 的**卡死恢复扫描**间隔（秒）。默认 60s。

    为什么需要它（两个以前只写在文档里的窗口）
    - **取件与领取之间崩溃**：worker 把 job 从共享队列取走后、还没条件领取就崩，
      该 job 会永远停在 `pending`（没人再投它）。
    - **完成写回时崩溃**：报告落库与 job 终态现在是**一个事务**（不会再出现
      "job=succeeded 但报告缺失"），但"事务提交前进程被杀"仍会留下 `running`。
    扫描会：`pending` 且过旧 → **重新入队**（幂等：条件领取保证只执行一次）；
    `running` 且过旧 → 置 `failed`（`error_code=abandoned`），**不自动重跑**。
    """
    return max(1.0, _get_float("DD_INVESTIGATE_RECOVER_INTERVAL_S", default=60.0))


def investigate_stale_pending_s() -> float:
    """`pending` 多久算卡住（秒）→ 重新入队。默认 300s。

    取值要大于"正常排队等待"：队列里排队的 job 也是 `pending`，反复重投只会让队列
    涨水（虽然条件领取保证不会重复执行）。300s 远大于 LLM 单次调查量级。
    """
    return max(1.0, _get_float("DD_INVESTIGATE_STALE_PENDING_S", default=300.0))


def investigate_stale_running_s() -> float:
    """`running` 多久算僵死（秒）→ 标 `failed`。默认 900s。

    必须显著大于"最坏情况下一次调查的耗时"（`DD_INVESTIGATE_TIMEOUT_S` × 步数），
    否则会把**慢但还活着**的 worker 判死——那会让它的产物写不进去（`finish_job`
    只接受 `running`），等于白跑一次。故取 15 分钟：90s 超时 × 8 步仍有充裕余量。
    """
    return max(10.0, _get_float("DD_INVESTIGATE_STALE_RUNNING_S", default=900.0))


def investigate_recover_batch() -> int:
    """每轮恢复扫描最多处理多少个 job（防一次扫太多把库压住）。默认 100。"""
    return max(1, _get_int("DD_INVESTIGATE_RECOVER_BATCH", default=100))


# ---------------------------------------------------------------- 事件时间（G2）

def allowed_lateness_s() -> float:
    """合并窗口允许的迟到容忍（秒）。事件早于 `watermark`（=最大事件时间-本值）即拒。

    同时也是恢复的 RPO 无关量：纯在线窗口参数。默认 30s——太小丢迟到数据（漏合并），
    太大增内存/延迟，按真实到达分布调。
    """
    return max(0.0, _get_float("DD_ALLOWED_LATENESS_S", default=30.0))


def watermark_enabled() -> bool:
    """是否启用事件时间水位线（有界迟到合并 + 窗口按事件时间有序）。

    默认 **关**：既有多数调用点未传事件时间，开启会改变合并边界；开启后窗口本就按
    事件时间排序（纯增益），合并从"无界 abs"收紧为"有向 + 水位线"。
    """
    return _get_bool("DD_WATERMARK_ENABLED", default=False)


# ---------------------------------------------------------------- 变更管理源（G7）

def change_source() -> str:
    """变更来源：mock（默认，内置表）/ jsonl（文件）/ webhook（CMDB API）。"""
    return _get("DD_CHANGE_SOURCE", default="mock").lower()


def change_jsonl_path() -> str:
    """jsonl 来源的变更记录文件路径。"""
    return _get("DD_CHANGE_JSONL_PATH")


def change_webhook_url() -> str:
    """webhook 来源的 CMDB API 地址（请求前过 SSRF 校验）。"""
    return _get("DD_CHANGE_WEBHOOK_URL")


def change_fallback_demo() -> bool:
    """变更源**不可用**时是否回退内置样例变更表（默认关；只给本地演示用）。

    为什么默认关：内置表是样例数据。把"查不到"悄悄变成"一份样例变更列表"，会让
    Agent 的"近期无（相关）变更"结论建立在假数据上——这与本仓"拿不准就失败关闭"
    的口径冲突。默认行为改为向 Agent 返回 `unavailable` 错误（不含 changes 字段），
    要演示"降级"这条路径必须显式打开本开关，且回退结果会带 `degraded=True` 标注。
    """
    return _get_bool("DD_CHANGE_FALLBACK_DEMO", default=False)


# ---------------------------------------------------------------- 因果时序（G6）

def causal_max_lag_s() -> float:
    """疑似因果链允许的最大滞后（秒）：因必须早于果且滞后在窗内。

    默认 5s——电气/热传播时标不同，按机型调；太小漏关联、太大引入伪因果。
    """
    return max(0.0, _get_float("DD_CAUSAL_MAX_LAG_S", default=5.0))


def causal_enabled() -> bool:
    """是否在证据桥启用因果链查询（默认开：查询只读、零副作用）。"""
    return _get_bool("DD_CAUSAL_ENABLED", default=True)


# ---------------------------------------------------------------- 降级队列（G4）

def degradation_queue_backend() -> str:
    """降级队列后端：memory（默认，进程内）/ state（共享状态，重启可续 + 多副本共享）。

    `state` 要真跨进程持久/共享，`DD_STATE_BACKEND` 须落在 postgres/redis——
    memory 状态后端只在同进程内共享。
    """
    return _get("DD_DEGRADATION_QUEUE", default="memory").lower()


# ---------------------------------------------------------------- 算子沙箱（P4-1）

def sandbox_kind() -> str:
    """算子执行器类型：inline（默认，单进程+预算）/ subprocess（进程隔离+资源上限）。

    wasm 为预留档位（未实现）。热路径保持 inline；对**不可信/待审**算子或隔离有
    硬要求的部署改 subprocess。见 `docs/p4/01_sandbox.md`。
    """
    return _get("DD_SANDBOX_KIND", default="inline").lower()


def sandbox_wall_s() -> float:
    """单算子墙钟上限（秒）。默认 50ms，对齐 slo.md §4（热路径须极短）。"""
    return _get_float("DD_SANDBOX_WALL_S", default=0.05)


def sandbox_max_steps() -> int:
    """解释器步数预算（Tier 0）。默认 10 万，防超长表达式。"""
    return _get_int("DD_SANDBOX_MAX_STEPS", default=100_000)


def sandbox_mem_mb() -> int:
    """子进程地址空间增量上限（MB，Tier 1）。默认 64。"""
    return _get_int("DD_SANDBOX_MEM_MB", default=64)


def sandbox_cpu_s() -> float:
    """子进程 CPU 时间上限（秒，Tier 1 rlimit）。默认 0.1。"""
    return _get_float("DD_SANDBOX_CPU_S", default=0.1)


def sandbox_policy():
    """组装 `SandboxPolicy`（延迟导入避免 settings 依赖 operator_factory）。"""
    from .operator_factory.sandbox import SandboxPolicy
    return SandboxPolicy(
        wall_s=sandbox_wall_s(),
        max_steps=sandbox_max_steps(),
        mem_mb=sandbox_mem_mb(),
        cpu_s=sandbox_cpu_s(),
        kind=sandbox_kind(),
    )


# ---------------------------------------------------------------- 算子控制面（P4-2）

def operator_signing_key() -> str:
    """算子制品签名密钥（HMAC 对称密钥）。**不进仓库**，生产环境变量注入。

    空 = 未配置：注册端点将拒绝带签名的注册（失败关闭）。本项提供签名机制，
    **不提供 KMS/HSM**——密钥托管是运营职责（见 docs/p4/02_control_plane.md §6）。
    """
    return _get("DD_OPERATOR_SIGNING_KEY")


def operator_pubkey() -> str:
    """Ed25519 公钥（hex）。与签名密钥二选一：配了它则用非对称验签（验签方无需私钥）。"""
    return _get("DD_OPERATOR_PUBKEY")


def operator_sign_alg() -> str:
    """签名算法：hmac-sha256（默认，零依赖）/ ed25519（需可选依赖 cryptography）。"""
    return _get("DD_OPERATOR_SIGN_ALG", default="hmac-sha256").lower()


def canary_min_samples() -> int:
    """看门狗最小样本数：低于此值**不自动判定**（冷启动保守，见 P4-2 §6）。"""
    return max(1, _get_int("DD_CANARY_MIN_SAMPLES", default=50))


def canary_max_crash_rate() -> float:
    return _get_float("DD_CANARY_MAX_CRASH_RATE", default=0.02)


def canary_max_parse_drop() -> float:
    return _get_float("DD_CANARY_MAX_PARSE_DROP", default=0.05)


def canary_max_candidate_multiple() -> float:
    return _get_float("DD_CANARY_MAX_CANDIDATE_MULTIPLE", default=3.0)


# ---------------------------------------------------------------- 退化趋势预测（P4-7）

def forecast_horizon_h() -> float:
    """预测视界（小时）：ETA 超过它不给数——外推越远越不可信。默认 24h。"""
    return max(0.0, _get_float("DD_FORECAST_HORIZON_H", default=24.0))


def forecast_min_conf() -> float:
    """方向一致性门槛（0..1）：低于它不预测（宁缺勿猜）。默认 0.5。"""
    return _get_float("DD_FORECAST_MIN_CONF", default=0.5)


def forecast_min_samples() -> int:
    """最少样本数：不足不预测。默认 6。"""
    return max(1, _get_int("DD_FORECAST_MIN_SAMPLES", default=6))


def forecast_thresholds() -> dict[str, float]:
    """各信号的目标阈值，形如 `temperature:95,current:12`（逗号分隔的 `信号:阈值`）。

    预测「到达该阈值」的 ETA；无阈值的信号不预测（没有目标就无从谈到达）。默认给
    温度一个保守上限示例——真实阈值应随机型标定。
    """
    raw = _get("DD_FORECAST_THRESHOLDS", default="temperature:95")
    out: dict[str, float] = {}
    for part in raw.split(","):
        if ":" not in part:
            continue
        k, _, v = part.partition(":")
        k = k.strip()
        try:
            out[k] = float(v.strip())
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------- 通知 / 值班 / 升级（P4-3）

def notify_enabled() -> bool:
    """是否在调查产事件后**主动投递通知**（默认关）。

    默认关：既有部署的告警流止于 SaaS adapter（落库审计）；开启后才会按值班表
    派发到 webhook/邮件/IM。开启即意味着"会真的往外发消息"，故默认不干扰。
    """
    return _get_bool("DD_NOTIFY_ENABLED", default=False)


def notify_ack_sla_s() -> float:
    """一线（level=1）确认 SLA（秒）：超此未 ack → 升级到下一级。默认 300s。"""
    return max(1.0, _get_float("DD_NOTIFY_ACK_SLA_S", default=300.0))


def notify_sla_l2_s() -> float:
    """二线（level=2）SLA（秒）。默认 600s。"""
    return max(1.0, _get_float("DD_NOTIFY_SLA_L2_S", default=600.0))


def notify_sla_l3_s() -> float:
    """三线/专家（level=3）SLA（秒）。默认 900s。"""
    return max(1.0, _get_float("DD_NOTIFY_SLA_L3_S", default=900.0))


def notify_max_level() -> int:
    """升级链最高级（含）。到顶仍无 ack → 该投递链记为 failed（不无限升级）。默认 3。"""
    return max(1, _get_int("DD_NOTIFY_MAX_LEVEL", default=3))


def notify_sla_for_level(level: int) -> float:
    """按级取 SLA；>3 级退回 level-1 的默认档（stay bounded）。"""
    table = {1: notify_ack_sla_s(), 2: notify_sla_l2_s(), 3: notify_sla_l3_s()}
    return table.get(level, notify_sla_l3_s())


def notify_sweep_interval_s() -> float:
    """升级 sweeper 的扫描间隔（秒）。默认 15s。"""
    return max(1.0, _get_float("DD_NOTIFY_SWEEP_INTERVAL_S", default=15.0))


def notify_sweep_lease_ttl_s() -> float:
    """升级 sweeper 的**单持有者租约** TTL（秒）。默认 60s。

    为什么要租约：sweeper 是进程内线程。多副本时每个副本都会扫到同一批"到期未 ack"的
    投递行，于是**同一个值班人被重复通知**——台账按 `delivery_id`（=hash(event_id, level)）
    去重，但**通知在去重之前就已经发出去了**（`_deliver` 是"先 send 再 upsert 台账"）。
    租约保证同一时刻只有一个副本在扫（见 `state/lease.py` 与 `EscalationSweeper`）。

    取值约束：**必须显著大于扫描间隔**，否则租约会在两轮之间过期、别的副本插进来扫，
    又变成重复升级。故实际取值 = `max(配置值, 3 × 扫描间隔)`。
    """
    configured = max(1.0, _get_float("DD_NOTIFY_SWEEP_LEASE_TTL_S", default=60.0))
    return max(configured, 3.0 * notify_sweep_interval_s())


def notify_http_timeout_s() -> float:
    """webhook/IM 通知的 HTTP 超时（秒）。默认 5s。"""
    return max(0.1, _get_float("DD_NOTIFY_HTTP_TIMEOUT_S", default=5.0))


def smtp_timeout_s() -> float:
    """SMTP 通知超时（秒）。默认 5s（与 HTTP 通知同量级，但**可各自调**）。

    为什么要单独一项：此前 `EmailNotifier` 直接复用 `DD_NOTIFY_HTTP_TIMEOUT_S`，
    于是"把邮件超时调长"会连带放长 webhook 超时（反之亦然）。两类下游的特性完全不同
    （SMTP 要 TLS 握手 + 认证 + 中继排队，webhook 是一次 HTTP POST），必须能分开设。
    默认值与旧行为一致（5s），故属于"解耦"而非"改行为"。
    """
    return max(0.1, _get_float("DD_SMTP_TIMEOUT_S", default=5.0))


def notify_allow_private_targets() -> bool:
    """是否允许通知目标指向内网/回环地址。

    默认 **False**（失败关闭）：webhook/IM 目标过 `data_plane.fetch.validate_url`
    的 SSRF 校验，内网 URL 被拒——**别让"通知"变成 SSRF 入口**。仅本地测试/受信
    内网部署才置 1。邮件（SMTP）目标不是 URL，不受此开关约束。
    """
    return _get_bool("DD_NOTIFY_ALLOW_PRIVATE", default=False)


def smtp_host() -> str:
    """SMTP 服务器（EmailNotifier 用）。空 = 未配（邮件通道不可用）。"""
    return _get("DD_SMTP_HOST")


def smtp_port() -> int:
    return _get_int("DD_SMTP_PORT", default=587)


def smtp_user() -> str:
    """SMTP 用户名/发件地址。**凭据走环境变量，不进仓库**。"""
    return _get("DD_SMTP_USER")


def smtp_password() -> str:
    return _get("DD_SMTP_PASSWORD")


def smtp_from() -> str:
    """发件人（From）。空则退回 `smtp_user`。"""
    return _get("DD_SMTP_FROM") or smtp_user()


def im_webhook_url() -> str:
    """IM 群机器人（飞书/钉钉/企微）webhook URL。空 = IM 通道不可用。

    走 SSRF 校验（与 webhook 同）。凭据在 URL 里，**不进仓库**，环境变量注入。
    """
    return _get("DD_IM_WEBHOOK_URL")


# ---------------------------------------------------------------- 路径

def project_root() -> str | None:
    return _get("DD_PROJECT_ROOT") or None


def vendor_docs_dir() -> str | None:
    return _get("DD_VENDOR_DOCS") or None


def operators_dir() -> str | None:
    return _get("DD_OPERATORS_DIR") or None


def data_dir() -> str | None:
    return _get("DD_DATA_DIR") or None


# ---------------------------------------------------------------- 知识库 / MCP

def kb_collection() -> str:
    return _get("DD_KB_COLLECTION", default="unitree_go2")


def kb_chroma() -> str:
    return _get("DD_KB_CHROMA")


def chroma_mcp_exe() -> str:
    return _get("DD_CHROMA_MCP_EXE")


def ctx_mcp() -> str:
    return _get("DD_CTX_MCP")


def ctx_mcp_server() -> str:
    return _get("DD_CTX_MCP_SERVER")
