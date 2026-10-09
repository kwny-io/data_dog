"""变更管理源适配器（G7）：接**真实**的固件/算子/配置变更记录。

为什么需要
----------
`query_recent_changes`（Agent 查"近期有无变更"）此前只读内置假表
`RECENT_CHANGES`，且 **`since` 参数被静默忽略**——Agent 以为按时间过滤了、
实际拿到很久前的变更，可能误导结论。这是**静默错误**，必须修。

本模块把变更来源抽象成 `ChangeSource`，并提供几种实现：

    MockChangeSource     包装内置表（离线确定性，CI 默认）
    JsonlChangeSource    从 jsonl 文件读（CI/发布流水线导出，最易落地）
    WebhookChangeSource  按需 POST 查 CMDB API（过 SSRF 校验）
    DbChangeSource       查本服务的 `change_records` 表（推送式落库后）

**所有实现都真正按 `since` / `until` 过滤**。切换用 `DD_CHANGE_SOURCE`。

诚实边界
--------
- **不绑定具体 CMDB 产品**：这里给适配器**骨架** + 一个可用的 mock/jsonl/webhook。
  对接 ServiceNow/Jira/自研发布系统需按实际 API 写一个适配器（几十行）。
- **变更 ≠ 故障**：记录的是"变了什么"，因果仍由 Agent 结合证据推断——不宣称
  "变更即根因"。
- **时效性**：变更记录可能滞后于实际（发布系统录入延迟），Agent 应容忍。
"""
from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol


def parse_dt(s: str) -> datetime | None:
    """宽松解析时间串（ISO / 'YYYY-MM-DD HH:MM' / 纯日期）；失败 → None。

    无法解析的**不过滤**（保留）——宁多勿漏：漏掉一条真实变更比多给一条更危险。
    """
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _in_window(applied_at: str, since: str, until: str) -> bool:
    """applied_at 是否落在 [since, until] 内。空 since/until = 不设该端。

    任一端无法解析（applied_at 或边界）→ 该端**不参与过滤**（保留），
    避免因格式不齐而静默丢弃记录。

    时区归一：来源常混用 aware（`...Z`）与 naive（`2026-09-01`）——直接比较会
    抛 `TypeError`。统一按 **UTC** 归一再比（naive 视为 UTC），保证不因格式差异
    崩掉或错判。
    """
    t = _as_utc(parse_dt(applied_at))
    if t is None:
        return True
    if since:
        lo = _as_utc(parse_dt(since))
        if lo is not None and t < lo:
            return False
    if until:
        hi = _as_utc(parse_dt(until))
        if hi is not None and t > hi:
            return False
    return True


def _as_utc(dt: datetime | None) -> datetime | None:
    """naive → 视为 UTC；aware 原样。便于安全比较。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass(frozen=True)
class ChangeRecord:
    """一条标准化变更记录。

    `detail` 承载来源特有的附加字段（如内置表的 `what`/`note`/`relevance`），
    `to_dict()` 会展开它们——保证对 Agent 的返回结构**向后兼容**（旧字段仍在）。
    """
    change_id: str
    kind: str                    # firmware / operator / config / hardware
    target: str                  # 部件 ref 或设备号
    applied_at: str              # 时间串（原样保留）
    from_version: str = ""
    to_version: str = ""
    actor: str = ""
    source: str = ""             # 来源系统标识
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转成给 Agent 的 dict：兼容旧形状（id/type/what/note/relevance…）。"""
        d: dict[str, Any] = {
            "id": self.change_id,
            "type": self.kind,
            "target": self.target,
            "applied_at": self.applied_at,
        }
        if self.from_version or self.to_version:
            d["from_version"] = self.from_version
            d["to_version"] = self.to_version
        if self.actor:
            d["actor"] = self.actor
        if self.source:
            d["source"] = self.source
        d.update(self.detail)          # 来源特有字段（what/note/…）展开
        return d


class ChangeSource(Protocol):
    """变更来源的最小接口。实现必须真正按 since/until 过滤。"""

    def query(self, *, device_sn: str, component: str = "",
              since: str = "", until: str = "") -> list[ChangeRecord]: ...


# ---------------------------------------------------------------- mock

def _records_from_builtin(builtin: dict[str, list[dict[str, Any]]]) -> list[ChangeRecord]:
    """把内置 `RECENT_CHANGES`（component → [dict]）转成 ChangeRecord 列表。"""
    out: list[ChangeRecord] = []
    for component, items in builtin.items():
        for it in items:
            detail = {k: v for k, v in it.items()
                      if k not in ("id", "type", "applied_at")}
            out.append(ChangeRecord(
                change_id=str(it.get("id", "")),
                kind=str(it.get("type", "")),
                target=component,
                applied_at=str(it.get("applied_at", "")),
                source="builtin",
                detail=detail,
            ))
    return out


class MockChangeSource:
    """包装内置变更表（离线确定性）。可注入自定义记录供测试。"""

    def __init__(self, records: list[ChangeRecord] | None = None):
        if records is None:
            from .store import RECENT_CHANGES
            records = _records_from_builtin(RECENT_CHANGES)
        self.records = list(records)

    def query(self, *, device_sn: str, component: str = "",
              since: str = "", until: str = "") -> list[ChangeRecord]:
        return [r for r in self.records
                if (not component or r.target == component)
                and _in_window(r.applied_at, since, until)]


# ---------------------------------------------------------------- jsonl

class JsonlChangeSource:
    """从 jsonl 文件读变更（每行一个 JSON 对象 → ChangeRecord）。

    字段名兼容 ChangeRecord 的字段；未识别的键进 `detail`。
    """

    _KNOWN = {"change_id", "kind", "target", "applied_at",
              "from_version", "to_version", "actor", "source"}

    def __init__(self, path: str):
        self.path = path
        self._cache: list[ChangeRecord] | None = None

    def _load(self) -> list[ChangeRecord]:
        if self._cache is not None:
            return self._cache
        out: list[ChangeRecord] = []
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except json.JSONDecodeError:
                        continue          # 跳坏行，不让一行毁了整份
                    if not isinstance(o, dict):
                        continue
                    detail = {k: v for k, v in o.items() if k not in self._KNOWN}
                    out.append(ChangeRecord(
                        change_id=str(o.get("change_id", o.get("id", ""))),
                        kind=str(o.get("kind", o.get("type", ""))),
                        target=str(o.get("target", "")),
                        applied_at=str(o.get("applied_at", "")),
                        from_version=str(o.get("from_version", "")),
                        to_version=str(o.get("to_version", "")),
                        actor=str(o.get("actor", "")),
                        source=str(o.get("source", "jsonl")),
                        detail=detail,
                    ))
        except FileNotFoundError:
            out = []
        self._cache = out
        return out

    def query(self, *, device_sn: str, component: str = "",
              since: str = "", until: str = "") -> list[ChangeRecord]:
        return [r for r in self._load()
                if (not component or r.target == component)
                and _in_window(r.applied_at, since, until)]


# ---------------------------------------------------------------- webhook

class WebhookChangeSource:
    """按需 POST 查询 CMDB API（拉取式）。

    **SSRF 防护**：请求前必须过 `data_plane.fetch.validate_url`——内网/环回/
    保留地址一律拒绝（与 P2 受控引用解析同一套校验）。

    `fetcher` 可注入（测试不联网）：签名 `(url, timeout) -> list[dict]`。
    默认使用标准库 urllib；**默认不自动联网**（生产按需）。若 goal 校验不过，
    `query` 返回空列表并记 reason（调用方/日志可见）。
    """

    def __init__(self, url: str, *, timeout: float = 5.0,
                 fetcher: Callable[[str, float], list[dict[str, Any]]] | None = None,
                 allowed: bool = True):
        self.url = url
        self.timeout = timeout
        self._fetcher = fetcher
        self._allowed = allowed          # 结果缓存：校验一次
        self.last_reason = ""
        self._validated = False

    # ---- 校验 ----
    def _check_url(self) -> bool:
        if self._validated:
            return self._allowed
        from ..data_plane.fetch import validate_url
        decision = validate_url(self.url)
        self._allowed = decision.allowed
        self.last_reason = decision.reason if not decision.allowed else ""
        self._validated = True
        return self._allowed

    def _post(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        if self._fetcher is not None:
            return self._fetcher(self.url, self.timeout)
        import urllib.request
        data = json.dumps(payload).encode("utf-8")
        # URL 已过 validate_url（仅 http/https、且拒绝环回/私有/保留地址），S310 不适用
        req = urllib.request.Request(       # noqa: S310
            self.url, data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
            body = json.loads(resp.read().decode("utf-8"))
        return body if isinstance(body, list) else body.get("changes", [])

    def query(self, *, device_sn: str, component: str = "",
              since: str = "", until: str = "") -> list[ChangeRecord]:
        if not self._check_url():
            return []                    # 校验不过 → 失败关闭，返回空
        raw = self._post({"device_sn": device_sn, "component": component,
                          "since": since, "until": until})
        out: list[ChangeRecord] = []
        for o in raw:
            if not isinstance(o, dict):
                continue
            out.append(ChangeRecord(
                change_id=str(o.get("change_id", o.get("id", ""))),
                kind=str(o.get("kind", o.get("type", ""))),
                target=str(o.get("target", component)),
                applied_at=str(o.get("applied_at", "")),
                from_version=str(o.get("from_version", "")),
                to_version=str(o.get("to_version", "")),
                actor=str(o.get("actor", "")),
                source=str(o.get("source", "webhook")),
                detail={k: v for k, v in o.items()
                        if k not in ("id", "change_id", "type", "kind", "target",
                                     "applied_at", "from_version", "to_version",
                                     "actor", "source")},
            ))
        # 远端可能不按 since 过滤 → 本地再保证一次（契约不靠对方）
        return [r for r in out
                if (not component or r.target == component)
                and _in_window(r.applied_at, since, until)]


# ---------------------------------------------------------------- db（推送式）

class DbChangeSource:
    """查本服务的 `change_records` 表（推送式：变更系统 POST 落库后）。

    `session_factory` 是返回 `AbstractContextManager[Session]` 的可调用（如
    `storage.db.session_scope`）；不在此模块直接依赖 db，便于测试注入假 session。

    `tenant_id` 是**必填**：`change_records` 按租户隔离（RLS + 仓储过滤），
    跨租户读不到——缺了它查询会零行可见（失败关闭），而非静默串租户。
    """

    def __init__(self, session_factory: Callable[[], AbstractContextManager[Any]],
                 *, tenant_id: str):
        self._session_factory = session_factory
        self.tenant_id = tenant_id

    def query(self, *, device_sn: str, component: str = "",
              since: str = "", until: str = "") -> list[ChangeRecord]:
        from ..storage import repository as repo
        with self._session_factory() as s:
            rows = repo.list_change_records(
                s, tenant_id=self.tenant_id, target=component or None)
        out: list[ChangeRecord] = []
        for r in rows:
            out.append(ChangeRecord(
                change_id=r.get("change_id", ""),
                kind=r.get("kind", ""),
                target=r.get("target", ""),
                applied_at=r.get("applied_at", ""),
                from_version=r.get("from_version", ""),
                to_version=r.get("to_version", ""),
                actor=r.get("actor", ""),
                source=r.get("source", "db"),
                detail=r.get("raw", {}) or {},
            ))
        return [r for r in out
                if (not component or r.target == component)
                and _in_window(r.applied_at, since, until)]


def default_change_source() -> ChangeSource:
    """按 `DD_CHANGE_SOURCE` 选默认来源（mock / jsonl / webhook；默认 mock）。"""
    from .. import settings
    kind = settings.change_source().lower()
    if kind == "jsonl":
        path = settings.change_jsonl_path()
        if path:
            return JsonlChangeSource(path)
        return MockChangeSource()        # 未配路径 → 退回 mock（不静默无数据）
    if kind == "webhook":
        url = settings.change_webhook_url()
        if url:
            return WebhookChangeSource(url)
        return MockChangeSource()
    return MockChangeSource()


__all__ = [
    "ChangeRecord",
    "ChangeSource",
    "DbChangeSource",
    "JsonlChangeSource",
    "MockChangeSource",
    "WebhookChangeSource",
    "default_change_source",
    "parse_dt",
]
