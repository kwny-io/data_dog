"""P4-3 值班表：按 (领域, 时刻) 查"谁在值班"，并解析成一条投递路由。

语义
----
值班表是**带时间窗的 (领域, 级别) → 值班人** 映射。查"当前谁值班"就是：在给定
时刻 `at`，找所有 `start_at <= at < end_at` 且领域匹配的条目，取**级别最低**（一线
优先）的那条。

- **领域匹配**：精确匹配 `domain`，或条目领域为 `"*"`（通配——全局值班）。
- **级别**：`level=1` 一线、`2` 二线、`3` 专家；数值小的优先。
- 同 (domain, level) 在同一时刻**多条并存**时，取 `start_at` 最晚者（更"新"的排班
  覆盖旧的）——冲突消解确定性，不随机。

诚实边界
--------
- 本模块只做**查询与解析**；**排班是人**的事（谁值哪班是运营决定，不是代码能生成）。
- 无值班人（表空 / 时段不覆盖）→ 返回 `None`，调用方据此**失败关闭**（不投递、
  或投到全局兜底），绝不假装有人值班。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from .notifiers import Route

WILDCARD = "*"


def make_rota_id(domain: str, level: int, start_at: float) -> str:
    """确定性 rota_id：同一 (领域, 级别, 起始时刻) 反复 UPSERT 收敛到同一行。"""
    raw = f"{domain}|{level}|{start_at:.0f}".encode()
    return "rota_" + hashlib.sha256(raw).hexdigest()[:16]


@dataclass(frozen=True)
class RotaEntry:
    """一条值班条目。`channel`/`target` 决定升级时用哪条路由投递。"""
    rota_id: str
    domain: str
    level: int
    assignee: str
    start_at: float
    end_at: float
    channel: str = "webhook"
    target: str = ""

    def is_active(self, at: float) -> bool:
        return self.start_at <= at < self.end_at

    def matches_domain(self, domain: str) -> bool:
        return self.domain == domain or self.domain == WILDCARD

    def to_dict(self) -> dict[str, object]:
        return {
            "rota_id": self.rota_id, "domain": self.domain, "level": self.level,
            "assignee": self.assignee, "start_at": self.start_at,
            "end_at": self.end_at, "channel": self.channel, "target": self.target,
        }

    def to_route(self) -> Route:
        return Route(channel=self.channel, target=self.target,
                     assignee=self.assignee, level=self.level)


def resolve_oncall(entries: list[RotaEntry], *, domain: str | None, at: float,
                   level: int | None = None) -> RotaEntry | None:
    """在 `at` 时刻解析值班条目。给 `level` 则只在该级找；否则取**级别最低**者。

    `domain=None` 表示**不限领域**（取任一时段内最低级条目）——给"总览当前谁值班"
    用；指定领域时按精确/通配匹配。纯函数、确定性；无匹配 → `None`（失败关闭）。
    """
    active = [e for e in entries if e.is_active(at)
              and (domain is None or e.matches_domain(domain))]
    if level is not None:
        active = [e for e in active if e.level == level]
    if not active:
        return None
    # 级别最小优先；同级别取 start_at 最晚（新排班覆盖旧）
    return min(active, key=lambda e: (e.level, -e.start_at))


def rows_to_entries(rows: list[dict[str, Any]]) -> list[RotaEntry]:
    """落库行 → RotaEntry 列表（缺字段容错，脏行跳过）。"""
    out: list[RotaEntry] = []
    for r in rows:
        try:
            out.append(RotaEntry(
                rota_id=str(r.get("rota_id", "")),
                domain=str(r.get("domain", WILDCARD)),
                level=int(r.get("level", 1) or 1),
                assignee=str(r.get("assignee", "")),
                start_at=float(r.get("start_at", 0.0) or 0.0),
                end_at=float(r.get("end_at", 0.0) or 0.0),
                channel=str(r.get("channel", "webhook") or "webhook"),
                target=str(r.get("target", "") or ""),
            ))
        except (TypeError, ValueError):
            continue
    return out


def current_oncall(entries: list[RotaEntry], *, domain: str | None, at: float,
                   level: int = 1) -> RotaEntry | None:
    """取某级当前值班人；该级无人 → 回退到更高级别（一线上不去就找二线）。

    回退只升不降：调用方要"一线"，若一线空则找 2→3；都空 → None。
    """
    for lv in range(max(1, level), 1 + 6):
        e = resolve_oncall(entries, domain=domain, at=at, level=lv)
        if e is not None:
            return e
    return None
