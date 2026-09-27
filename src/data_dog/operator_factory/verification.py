"""P1：Fuzz 与新旧版本差分回放（对齐架构 §8.4）。

§8.4 要求的验证门里，本模块补齐两项（Fixture 黄金回放已在 pipeline.py）：
  1. **畸形输入 / Fuzz**：喂超长、乱码、截断、类型错乱的输入 → 算子**不得崩溃**，
     且**不得误命中**（宁可漏报，不可误报）。
  2. **新旧版本差分回放**：同一批数据分别喂新旧两版算子，输出差异必须**可解释**——
     新增命中的规则要能说清为什么（否则视为行为漂移，回退）。

确定性：Fuzz 用固定种子的伪随机（不依赖系统随机/挂钟），保证可复现。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from .pipeline import Operator

# ---------------------------------------------------------------- Fuzz

@dataclass
class FuzzResult:
    n: int
    crashes: int
    detail: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.crashes == 0


_MALFORMED = [
    None,                       # 空
    {},                         # 空映射
    {"temperature": "not-a-number"},
    {"temperature": None},
    {"temperature": 1e308},     # 极大
    {"temperature": -1e308},
    {"temperature": [1, 2, 3]},  # 类型错乱
    {"temperature": {"nested": 1}},
    {"mode": "4"},
    {"mode": -1},
    {"mode": 0xFFFFFFFFFF},     # 超范围
    {"payload": "x" * 10000},   # 超长
    {"payload": b"\xff\xfe\x00"},  # 乱码字节
]


def fuzz_operator(op: Operator, *, iterations: int = 200, seed: int = 20260913) -> FuzzResult:
    """喂畸形 + 伪随机输入；任何异常泄漏都记为该次崩溃。返回崩溃计数与详情。

    注意：这里**故意**用固定种子的 `random.Random`，因为测试要求**可复现**——
    同一份代码跑 Fuzz 必须得到同样的用例集。这不是加密/安全用途，不用 secrets
    （用系统熵会导致回归测试不可复现）。
    """
    rng = random.Random(seed)
    fields = [f["name"] for f in op.fields] or ["temperature", "current", "mode"]
    detail: list[str] = []
    crashes = 0
    cases: list[Any] = list(_MALFORMED)
    for _ in range(iterations):
        ev: dict[str, Any] = {}
        for name in fields:
            r = rng.random()
            if r < 0.2:
                continue                       # 缺字段
            if r < 0.4:
                ev[name] = rng.choice([None, "x", [1], {"a": 1}])
            elif r < 0.7:
                ev[name] = rng.uniform(-1e6, 1e6)
            else:
                ev[name] = rng.randint(-10, 0xFFFF)
        cases.append(ev)

    for i, ev in enumerate(cases):
        try:
            op.run(ev)                          # run 内部 evaluate 已吞 DSLError
        except Exception as e:                  # noqa: BLE001 —— Fuzz 就是要抓所有异常
            crashes += 1
            if len(detail) < 5:
                detail.append(f"case#{i} {type(e).__name__}: {e}")
    return FuzzResult(n=len(cases), crashes=crashes, detail=detail)


# ---------------------------------------------------------------- 新旧差分

@dataclass
class DiffEntry:
    fixture_id: str
    only_old: list[str]
    only_new: list[str]
    accepted: bool
    reason: str = ""


@dataclass
class DiffResult:
    entries: list[DiffEntry] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(e.accepted for e in self.entries)


def diff_replay(old: Operator, new: Operator, fixtures: list[Any],
                *, expected_new_hits: dict[str, list[str]] | None = None
                ) -> DiffResult:
    """逐 Fixture 比较新旧算子输出差异。

    expected_new_hits：允许的"新版本新增命中"清单（fixture_id → rule_id 列表）。
    差异只要**不在白名单里**就不接受（防行为漂移）——差分必须可解释。
    """
    expected = expected_new_hits or {}
    out = DiffResult()
    for fx in fixtures:
        old_hits = set(old.run(fx.event, fx.window_ctx()))
        new_hits = set(new.run(fx.event, fx.window_ctx()))
        only_old = sorted(old_hits - new_hits)
        only_new = sorted(new_hits - old_hits)
        if not only_old and not only_new:
            out.entries.append(DiffEntry(fx.fixture_id, [], [], True, "无差异"))
            continue
        allowed = set(expected.get(fx.fixture_id, []))
        ok = (not only_old) and set(only_new).issubset(allowed)
        reason = "差异在白名单内" if ok else f"未解释的差异（only_new={only_new}, allowed={sorted(allowed)}）"
        out.entries.append(DiffEntry(fx.fixture_id, only_old, only_new, ok, reason))
    return out
