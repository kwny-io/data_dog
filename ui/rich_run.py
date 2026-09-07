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


def _summarize_result(name: str, result) -> str:
    """按工具类型精排返回值，避免泼一屏 JSON。

    遥测：显示温度/电流/转速 + 检测命中；
    变更：显示固件/算子变更要点；
    其他：只显示最关键的 few 个键。
    """
    if not isinstance(result, dict):
        return str(result)
    if result.get("ok") is False:
        return f"[red]错误: {result.get('error')}[/]"
    if name == "query_candidate_evidence":
        metrics = result.get("metrics", {})
        ref = result.get("reference_window_metrics", {})
        parts = []
        for k, v in metrics.items():
            rv = f" (参照{ref.get(k)})" if k in ref else ""
            parts.append(f"{k}={v}{rv}")
        # 检测命中
        det = result.get("detection", [])
        det_txt = ""
        if det:
            det_txt = "  ⚠️ " + ", ".join(
                f"{d['rule_id']}:{d.get('observed')}{'>=' if d.get('operator') in ('ge','gt') else '<='}{d.get('threshold')}"
                for d in det)
        return " | ".join(parts) + det_txt
    if name == "query_recent_changes":
        return "".join(
            f"\n    · {c.get('what')} ({c.get('applied_at')})" for c in result.get("changes", []))
    if name == "query_vendor_knowledge":
        return f"{len(result.get('items', []))} 条厂商知识"
    if name == "query_neighbor_events":
        return f"{len(result.get('events', []))} 个邻居事件"
    return json.dumps(result, ensure_ascii=False)[:200]


def _render_step(step: dict) -> None:
    name = step.get("name", "?")
    icon, color = _TOOL_STYLE.get(name, ("🔧 ", "white"))
    grid = Table.grid(padding=(0, 1))
    grid.add_column(justify="right", min_width=6)
    grid.add_column()
    # 参数
    args = json.dumps(step.get("args", {}), ensure_ascii=False)[:130]
    result = step.get("result", {})
    grid.add_row(
        f"[bold]第{step.get('step', '?')}步[/]",
        f"{icon}[bold {color}]{name}[/bold {color}]({args})",
    )
    # 精排后的返回值
    summary = _summarize_result(name, result)
    grid.add_row("", Text.from_markup(f"← {summary}"))
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

    # 缺失证据、风险窗口、建议（用 Panel 包裹，避免裸蜕文本）
    acts = d.get("recommended_actions", [])
    if d.get("missing_evidence") or d.get("risk_window") or acts:
        body = []
        if d.get("missing_evidence"):
            body.append("[bold]缺失证据:[/] " + "、".join(d["missing_evidence"]))
        if d.get("risk_window"):
            body.append(f"[bold]风险窗口:[/] {d['risk_window']}")
        if acts:
            body.append("[bold]建议动作:[/]")
            for a in acts:
                body.append(f"  · {a}")
        console.print(Panel("\n".join(body), title="待办 / 缺失", border_style="magenta", expand=False))
    console.rule()


def render_compare(results) -> None:
    """把一轮 test 会话里多次调查的结论排成对比表，突出不同候选 → 不同结论。"""
    t = Table(title="候选对比", box=None)
    t.add_column("候选")
    t.add_column("裁决", justify="center")
    t.add_column("置信度", justify="right")
    for label, source, report in results:
        d = report.to_dict()
        disp = d.get("disposition", "?")
        color = _DISPOSITION_STYLE.get(disp, "white")
        t.add_row(
            label,
            f"[{color}]{disp.upper()}[/{color}]",
            f"{d.get('confidence', 0.0):.2f}",
        )
    console.print(t)
