"""P1：算子装配 + Fixture 黄金回放验证。

对齐架构：
  §8.4 验证流水线 —— Fixture 黄金输入输出测试、历史回放。
  §10.3 执行语义 —— 同输入同输出（确定性）。

本模块把"知识包 → 检测算子（声明式 + DSL）"装配起来，并用 Fixture 回放验证：
  - 正例 Fixture：期望命中的规则必须命中；
  - 反例 Fixture：期望不命中的规则必须不命中（防"永远为真"的假算子）。
产物：可写回 operators/generated/ 的声明式 yaml（含 dsl_rules）。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .dsl import CompiledRule, DSLError, WindowContext, compile_rule, evaluate

# ---------------------------------------------------------------- 声明式检测（复用现有 engine 语义）

_DECL_OPS = {
    "gt": lambda a, b: a > b, "lt": lambda a, b: a < b,
    "ge": lambda a, b: a >= b, "le": lambda a, b: a <= b, "eq": lambda a, b: a == b,
}


@dataclass
class DeclarativeRule:
    rule_id: str
    field: str
    operator: str
    value: float
    severity_hint: str = "warning"
    message: str = ""


@dataclass
class Operator:
    """一个部件的检测算子（声明式 + DSL 双层），对应 operators/generated/*.yaml。"""
    operator_id: str
    component: str
    input_schema: str
    output_schema: str
    fields: list[dict[str, Any]] = field(default_factory=list)
    rules: list[DeclarativeRule] = field(default_factory=list)
    dsl_rules: list[dict[str, Any]] = field(default_factory=list)
    generator_provenance: str = ""
    why_not_lower_layer: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    # -- 编译 DSL 规则（暴露语法/白名单/类型错误）--
    def compile_dsl(self) -> list[CompiledRule]:
        declared = [f["name"] for f in self.fields]
        return [compile_rule(r, declared) for r in self.dsl_rules]

    # -- 执行：返回命中的 rule_id 列表 --
    def run(self, event: dict[str, Any],
            window: WindowContext | None = None,
            max_steps: int = 0, wall_s: float = 0.0) -> list[str]:
        """对一条（可能畸形的）输入执行算子。

        健壮性（架构 §10.3 / §8.4 Fuzz 要求）：**任何畸形输入都不得抛异常**——
        类型不符/缺失/超范围一律视为"该规则不命中"（宁可漏报，不可崩溃或误报）。

        `max_steps` / `wall_s`：Tier 0 沙箱预算（默认 0 = 不限，保持既有语义）。
        由 `sandbox.ExecutorPool` 传入；超步/超时抛 `SandboxViolation` 向上传播，
        **不**被本方法的"不命中"容错吞掉（安全事件不可伪装为不命中）。
        """
        hits: list[str] = []
        if not isinstance(event, dict):
            return hits
        for r in self.rules:
            if r.field not in event:
                continue
            fn = _DECL_OPS.get(r.operator)
            if fn is None:
                continue
            # 数值防御：非数值（None/str/list/dict/bool）→ 不命中，不比较
            val = event[r.field]
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                continue
            try:
                if fn(val, r.value):
                    hits.append(r.rule_id)
            except (TypeError, ValueError):
                continue
        for cr in self.compile_dsl():
            if evaluate(cr, event, window, max_steps=max_steps, wall_s=wall_s):
                hits.append(cr.rule_id)
        return hits


# ---------------------------------------------------------------- Fixture

@dataclass
class Fixture:
    """一个黄金测试用例：给定事件（+窗口），期望命中/不命中哪些规则。"""
    fixture_id: str
    component: str
    event: dict[str, Any]
    window: dict[str, Any] | None = None    # {"series": {...}, "baseline": {...}}
    expect_hit: list[str] = field(default_factory=list)
    expect_miss: list[str] = field(default_factory=list)

    @staticmethod
    def load(path: Path) -> Fixture:
        d = json.loads(path.read_text(encoding="utf-8"))
        return Fixture(
            fixture_id=d["fixture_id"], component=d["component"], event=d["event"],
            window=d.get("window"),
            expect_hit=d.get("expect_hit", []), expect_miss=d.get("expect_miss", []),
        )

    def window_ctx(self) -> WindowContext | None:
        if not self.window:
            return None
        return WindowContext(
            series=self.window.get("series", {}),
            baseline=self.window.get("baseline", {}),
        )


@dataclass
class ReplayResult:
    fixture_id: str
    passed: bool
    detail: str


def replay(operator: Operator, fixture: Fixture) -> ReplayResult:
    """用一个 Fixture 回放算子；返回是否通过（正例命中 + 反例不命中）。"""
    try:
        hits = set(operator.run(fixture.event, fixture.window_ctx()))
    except DSLError as e:
        return ReplayResult(fixture.fixture_id, False, f"DSL 错误: {e}")
    missed_expected = [r for r in fixture.expect_hit if r not in hits]
    unexpected = [r for r in fixture.expect_miss if r in hits]
    if missed_expected or unexpected:
        parts = []
        if missed_expected:
            parts.append(f"应命中未命中={missed_expected}")
        if unexpected:
            parts.append(f"应不命中却命中={unexpected}")
        return ReplayResult(fixture.fixture_id, False, "; ".join(parts))
    return ReplayResult(fixture.fixture_id, True, f"命中={sorted(hits)}")


def replay_all(operator: Operator, fixtures: list[Fixture]) -> dict[str, Any]:
    results = [replay(operator, f) for f in fixtures]
    n = len(results)
    hit = sum(1 for r in results if r.passed)
    return {
        "n": n, "passed": hit,
        "accuracy": round(hit / n, 3) if n else 0.0,
        "results": [asdict(r) for r in results],
    }
