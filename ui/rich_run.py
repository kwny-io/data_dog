# -*- coding: utf-8 -*-
"""精美 CLI：用 rich 渲染 ReAct 轨迹 + InvestigationReport。

不替换脚本逻辑，只负责"好看的输出"。AgentLoop 的 trace_cb 把每步
工具调用抛过来，这里实时渲染成彩色滚动列表；最后把报告渲染成卡片。
"""
from __future__ import annotations

import json
from typing import Callable, List

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()

# 每个工具对应的图标和颜色
_TOOL_STYLE = {
    "query_candidate_evidence": ("🌡️ ", "yellow"),
    "query_vendor_knowledge": ("📚 ", "cyan"),
    "query_neighbor_events": ("🔗 ", "magenta"),
    "query_recent_changes": ("🕐 ", "green"),
}

_DISPOSITION_STYLE = {
    "confirmed": "green",
    "rejected": "blue",
    "inconclusive": "yellow",
    "needs_human_review": "red",
}


def make_trace_cb() -> (List[dict], Callable[[dict], None]):
    """返回 (轨迹列表, 回调)。回调收到每步时立刻渲染到控制台。"""
    steps: List[dict] = []

    def _on_step(step: dict) -> None:
        steps.append(step)
        _render_step(step)

    return steps, _on_step


def _render_step(step: dict) -> None:
    name = step.get("name", "?")
    icon, color = _TOOL_STYLE.get(name, ("🔧 ", "white"))
    grid = Table.grid(padding=(0, 1))
    grid.add_column(justify="right", min_width=6)
    grid.add_column()
    # 参数
    args = json.dumps(step.get("args", {}), ensure_ascii=False)[:130]
    result = step.get("result", {})
    # 若 result 里有 detection，额外标出来更直观
    det = result.get("detection") if isinstance(result, dict) else None
    det_txt = ""
    if det:
        det_txt = "  ⚠️ " + ", ".join(f"{d['rule_id']}({d['severity_hint']})" for d in det)
    grid.add_row(
        f"[bold]第{step.get('step', '?')}步[/]",
        f"{icon}[bold {color}]{name}[/bold {color}]({args}){det_txt}",
    )
    # 返回值摘要
    grid.add_row("", Text.from_markup(f"← {json.dumps(result, ensure_ascii=False)[:300]}"))
    console.print(grid)


def render_report(report) -> None:
    console.rule("[bold]InvestigationReport[/bold]")
    d = report.to_dict()
    # 头部：裁决 + 置信度
    disp = d.get("disposition", "?")
    disp_color = _DISPOSITION_STYLE.get(disp, "white")
    header = Text()
    header.append("裁决: ", style="bold")
    header.append(disp.upper(), style=f"bold {disp_color}")
    header.append(f"    置信度: {d.get('confidence', 0.0):.2f}", style="dim")
    console.print(header)

    # 摘要
    console.print(Panel(
        d.get("summary", ""),
        title="摘要",
        border_style="cyan",
        expand=False,
    ))

    # 假设
    hyps = d.get("hypotheses", [])
    if hyps:
        hyps_panel = "\n".join(
            f"  [bold]·[/] {h.get('cause')}  [{('yellow' if h.get('status')=='supported' else 'dim')}]{h.get('status')}[/]"
            for h in hyps)
        console.print(Panel(hyps_panel, title="假设", border_style="blue", expand=False))

    # 缺失证据、风险窗口、建议
    if d.get("missing_evidence"):
        console.print("[bold]缺失证据:[/] " + "、".join(d["missing_evidence"]))
    if d.get("risk_window"):
        console.print(f"[bold]风险窗口:[/] {d['risk_window']}")
    acts = d.get("recommended_actions", [])
    if acts:
        console.print("[bold]建议动作:[/]")
        for a in acts:
            console.print(f"  · {a}")
    console.rule()
