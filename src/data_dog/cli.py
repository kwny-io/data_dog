# -*- coding: utf-8 -*-
"""data_dog 命令行入口。

用法：
  data-dog init   交互式选择供应商并接入你的 API key（小白式）
  data-dog run   用已配置的模型跑一次候选异常调查，输出 InvestigationReport
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from .providers.config import load_config, save_config
from .providers.registry import BUILTIN_VENDORS, vendor_ids
from .schemas.events import CandidateEvent

app = typer.Typer(add_completion=False, help="data_dog：机器狗遥测故障调查 Agent")


@app.command()
def init(
    vendor: str = typer.Option(None, help="供应商 id，如 deepseek / openai / zhipu"),
):
    """交互式接入你的 LLM 供应商（选供应商 → 填 key → 校验 → 保存）。"""
    choices = vendor_ids()
    # 1) 选供应商
    if vendor is None:
        print("可选的 LLM 供应商：")
        for i, v in enumerate(BUILTIN_VENDORS, 1):
            print(f"  [{i}] {v.label}\n      {v.doc}")
        idx = typer.prompt("\n请输入编号选择（默认 1=DeepSeek）", default="1")
        try:
            vendor = choices[int(idx) - 1]
        except (ValueError, IndexError):
            typer.echo("无效编号，使用默认 DeepSeek")
            vendor = "deepseek"

    try:
        from .providers.registry import get_vendor
        spec = get_vendor(vendor)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)

    # 2) 填 key（默认读环境变量已有的，避免重复）
    import os
    existing = os.environ.get(spec.requires_env_key, "")
    key = typer.prompt(
        f"粘贴你的 {spec.label} API Key"
        + (f"（检测到环境变量已有，直接回车沿用）" if existing else ""),
        default=existing,
    )
    if not key or key == existing:
        # 若环境变量没有，则必须要求输入
        from .providers.config import load_env
        env, _ = load_env()
        key = env.get(spec.requires_env_key, key)
    if not key:
        typer.echo("未提供 API Key，无法完成接入。", err=True)
        raise typer.Exit(1)

    # 3) 可选：指定模型
    model = typer.prompt(
        f"使用模型（默认 {spec.default_model}，直接回车）",
        default=spec.default_model,
    )

    # 4) 校验连通
    typer.echo("正在校验连接…")
    from .providers.client import Provider
    ok = Provider.from_config(vendor, key).ping(model=model)
    if not ok:
        typer.echo("⚠️  连接校验失败（请检查 key 是否有效、是否有网络）。将仍尝试保存配置。", err=True)

    # 5) 保存
    path = save_config(vendor, key, model)
    typer.echo(f"✅ 已保存配置到 {path}")
    typer.echo("现在可以运行：data-dog run")


@app.command()
def run(
    fixture: Path = typer.Option(
        Path("evidence/candidate_temperature.json"),
        help="候选异常 JSON 文件路径",
    ),
    vendor: str | None = typer.Option(None, help="指定供应商，覆盖配置"),
    model: str | None = typer.Option(None, help="指定模型，覆盖配置"),
    offline: bool = typer.Option(False, help="离线演示（用 fake provider，无需 key）"),
):
    """调查一个候选异常，输出 InvestigationReport（rich 精美输出）。"""
    # reusable provider
    from .providers.client import Provider

    if offline:
        provider = _FakeProvider()
    else:
        cfg = load_config()
        vid = vendor or cfg["vendor_id"]
        key = cfg["api_key"]
        mdl = model or cfg["model"] or None
        if not key:
            typer.echo("尚未配置 API Key。加 --offline 用离线条模型演示，或先 data-dog init", err=True)
            raise typer.Exit(1)
        provider = Provider.from_config(vid, key)

    from .evidence.store import EvidenceStore
    from .tools.investigation import InvestigationTools
    from .agent.loop import AgentLoop

    candidate = _load_candidate(fixture)
    tools = InvestigationTools(EvidenceStore())
    trace_steps, trace_cb = _make_trace_cb()
    agent = AgentLoop(provider, tools, model=model, trace_cb=trace_cb)

    from rich.console import Console
    console = Console()
    console.print(f"[bold]调查候选[/]: [cyan]{candidate.candidate_id}[/] ({candidate.signal})")
    report = agent.investigate(candidate)
    _render_report(report)


def _ui_module(name: str):
    """从 ui/ 目录按路径加载模块（ui 不在包内，用路径导入）。"""
    import importlib.util
    from pathlib import Path
    here = Path(__file__).resolve()
    ui_path = here.parents[2] / "ui" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, ui_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_trace_cb():
    return _ui_module("rich_run").make_trace_cb()


def _render_report(report):
    _ui_module("rich_run").render_report(report)


class _FakeProvider:
    """无 key 离线演示用。"""
    def __init__(self):
        self.n = 0

    def chat(self, messages, tools=None, **kw):
        import json as _json
        self.n += 1
        if self.n == 1:
            return {"kind": "tool_calls", "tool_calls": [{
                "id": "m1", "name": "query_candidate_evidence",
                "arguments": _json.dumps({"device_sn": "device-abc123",
                                          "component": "joint_motor.front_left_hip",
                                          "window_start": "09:55", "window_end": "10:00"}),
                "content": ""}], "__raw_tool_calls": []}
        if self.n == 2:
            return {"kind": "tool_calls", "tool_calls": [{
                "id": "m2", "name": "query_recent_changes",
                "arguments": _json.dumps({"device_sn": "device-abc123",
                                          "component": "joint_motor.front_left_hip",
                                          "since": "2026-08-10"}),
                "content": ""}], "__raw_tool_calls": []}
        return {"kind": "text", "content": _json.dumps({
            "disposition": "needs_human_review", "severity": "warning", "confidence": 0.6,
            "summary": "温度78.2°C超过75°C阈值且电流上升，但固件3.2.1(8/12)变更冷却参数，需人工区分固件效应与真实退化。",
            "hypotheses": [{"cause": "固件3.2.1冷却变更导致", "status": "supported"},
                           {"cause": "轴承摩擦增大", "status": "supported"}],
            "missing_evidence": ["升级前基线"],
            "recommended_actions": ["复核固件基线", "检查轴承"],
        }, ensure_ascii=False)}


def _load_candidate(path: Path) -> CandidateEvent:
    if not path.exists():
        typer.echo(f"找不到候选异常文件: {path}", err=True)
        raise typer.Exit(1)
    data = json.loads(path.read_text(encoding="utf-8"))
    return CandidateEvent(**data)


if __name__ == "__main__":
    app()
