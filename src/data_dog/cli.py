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
):
    """用已配置的模型调查一个候选异常，输出 InvestigationReport。"""
    cfg = load_config()
    vid = vendor or cfg["vendor_id"]
    key = cfg["api_key"]
    mdl = model or cfg["model"] or None

    if not key:
        typer.echo("尚未配置 API Key，请先运行: data-dog init", err=True)
        raise typer.Exit(1)

    from .providers.client import Provider
    provider = Provider.from_config(vid, key)

    from .evidence.store import EvidenceStore
    from .tools.investigation import InvestigationTools
    from .agent.loop import AgentLoop

    candidate = _load_candidate(fixture)
    tools = InvestigationTools(EvidenceStore())
    agent = AgentLoop(provider, tools, model=mdl)

    typer.echo(f"调查候选异常: {candidate.candidate_id} ({candidate.signal})")
    report = agent.investigate(candidate)

    typer.echo("\n===== InvestigationReport =====")
    typer.echo(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))


def _load_candidate(path: Path) -> CandidateEvent:
    if not path.exists():
        typer.echo(f"找不到候选异常文件: {path}", err=True)
        raise typer.Exit(1)
    data = json.loads(path.read_text(encoding="utf-8"))
    return CandidateEvent(**data)


if __name__ == "__main__":
    app()
