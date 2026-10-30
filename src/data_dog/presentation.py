"""展示层派生：把「候选 + 统计 + 遥测」凝练成一句人话结论（verdict）。

为什么要抽出来
--------------
`scenarios.ScenarioRun.verdict` / `verdict_detail` 原本是内联在演示场景里的逻辑，
只有 Streamlit 演示台在用。阶段 D 的 HTTP 端点（`GET /v1/candidates/{id}`）也要给
前端同样的「一句话结论」——若各写一份，两边迟早分叉：演示台说"过温"，前端说
"无异常"。这里把判定逻辑收敛到**唯一实现**，`scenarios` 改为**委托**调用它
（因此 `ui/app.py` 的 `run.verdict` 行为不变、零改动）。

输入约定
--------
- `candidates`：至少含一个 dict，键有 `severity_hint` / `component_ref` /
  `rule_matches`（与 `CandidateEvent.to_dict()` 一致）。
- `stats`：数据面统计（`rejected` / `rejected_by_reason` 等），可为空。
- `telemetry`：部件 → `{"metrics": {...}}`，用于从证据里取温度。
"""
from __future__ import annotations

from typing import Any

from .go2_sim.semantic import COMPONENT_KIND_NAME, COMPONENT_PART_NAME

_SEVERITY_CN = {"critical": "严重", "warning": "警告"}


def component_cn(component_ref: str) -> str:
    """部件显示名：能翻就翻，翻不了**原样返回**（不编造名字）。

    这里原来有一份 6 条的 `_COMPONENT_CN` 硬编码表，只覆盖左前髋/左前膝等
    少数部件，且与 `go2_sim/semantic.py` 的表**各自演进**——同一部件在两个
    页面上叫法不同，没登记的部件则直接漏英文。

    现在改为拼 `semantic.py` 的两张表（部件类型 + 部件实例），覆盖全部 12 个
    关节和 5 类部件。**保留"翻不了就原样返回"这个语义**：调用方（演示台的
    一句话结论、HTTP 调查端点）需要的是"宁可露出原串，也不要编一个中文名"，
    所以不做 `semantic.component_name` 那种拆词重组。
    """
    if not component_ref:
        return component_ref
    kind_raw, sep, part_raw = component_ref.partition(".")
    if not sep:
        return COMPONENT_KIND_NAME.get(kind_raw, component_ref)
    kind = COMPONENT_KIND_NAME.get(kind_raw)
    part = COMPONENT_PART_NAME.get(part_raw)
    if kind is None or part is None:
        return component_ref          # 任一半没登记 → 原样，不拼半个中文
    return f"{part}{kind}"


def _first_temperature(telemetry: dict[str, dict[str, Any]]) -> float | None:
    for ev in telemetry.values():
        t = (ev or {}).get("metrics", {}).get("temperature_c")
        if t is not None:
            return float(t)
    return None


def derive_verdict(
    candidates: list[dict[str, Any]],
    stats: dict[str, Any] | None,
    telemetry: dict[str, dict[str, Any]] | None,
) -> tuple[str, str]:
    """返回 `(verdict, verdict_detail)`：一句话结论 + 一句人话解释。

    规则（与 `ScenarioRun` 原实现一致）：
      1. 有候选 → 按最高严重度给"检测到 N 条 X 异常"。
      2. 无候选但数据面拒绝过 → "日志被拒绝（失败关闭）"。
      3. 无候选且解析不出指标 → 说明"字段与算子对不上"。
      4. 其余 → "没有检测到异常"。
    """
    stats = stats or {}
    telemetry = telemetry or {}

    if candidates:
        c = candidates[0]
        sev = _SEVERITY_CN.get(c.get("severity_hint", ""), c.get("severity_hint", ""))
        verdict = f"⚠️ 检测到 {len(candidates)} 条{sev}异常"
        name = component_cn(c.get("component_ref", ""))
        temp = _first_temperature(telemetry)
        if "MOTOR_TEMP_PROTECT" in (c.get("rule_matches") or []) and temp is not None:
            detail = f"{name}温度 {temp:.0f}°C，超过厂商文档写明的 90°C 安全保护线。"
        else:
            detail = f"{name}命中了检测规则：{', '.join(c.get('rule_matches') or [])}。"
        return verdict, detail

    if stats.get("rejected_by_reason") or stats.get("rejected"):
        return ("⛔ 日志被拒绝（失败关闭）",
                "设备信息与已知兼容范围对不上，数据面拒绝处理而不是猜测。")

    has_metrics = any((ev or {}).get("metrics") for ev in telemetry.values())
    if not has_metrics:
        return ("✅ 没有检测到异常",
                "这条日志的字段和当前检测算子对不上（本算子只覆盖关节电机），"
                "解析不出可用指标，所以没有可判定的内容。")
    return ("✅ 没有检测到异常",
            "各项读数都在厂商文档给出的规则范围内，没有触发任何检测规则。")
