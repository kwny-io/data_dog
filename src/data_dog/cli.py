"""data_dog 命令行入口。

用法：
  data-dog init   交互式选择供应商并接入你的 API key（小白式）
  data-dog run    用已配置的模型跑一次候选异常调查，输出 InvestigationReport
  data-dog test   交互窗：从候选清单挑一个调查（或导入自定义 JSON），可在一次会话里对比多个结论
"""
from __future__ import annotations

import json
from pathlib import Path

import typer

from .core.console import ensure_utf8_console
from .core.injection import unwrap
from .providers.config import load_config, save_config
from .providers.registry import BUILTIN_VENDORS, vendor_ids
from .schemas.events import CandidateEvent

app = typer.Typer(add_completion=False, help="data_dog：机器狗遥测故障调查 Agent")


@app.callback()
def _console_setup() -> None:
    """每个子命令执行前统一做一次控制台编码兜底。

    放在 callback 而不是 `if __name__ == "__main__"`：真实入口是 console_script
    `data-dog = data_dog.cli:app`，那条路径**不经过** `__main__`，写在那里等于没修。
    """
    ensure_utf8_console()


config_app = typer.Typer(add_completion=False, help="配置体检与诊断")


@config_app.command("check")
def config_check(
    json_out: bool = typer.Option(False, "--json", help="输出 JSON（供流水线采集）"),
    strict: bool = typer.Option(False, "--strict", help="警告也算失败（非零退出）"),
) -> None:
    """体检环境变量配置：未知键 / 非法取值 / 危险组合（不连库、不验密钥真假）。"""
    import json as _json

    from .config_check import check, render

    report = check()
    if json_out:
        print(_json.dumps(report, ensure_ascii=False))
    else:
        print(render(report))
    if report["errors"] or (strict and report["warnings"]):
        raise typer.Exit(1)


app.add_typer(config_app, name="config")


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
        raise typer.Exit(1) from e

    # 2) 填 key（默认读环境变量已有的，避免重复）
    import os
    existing = os.environ.get(spec.requires_env_key, "")
    # ⚠️ 两条都不能少：hide_input 防"输入被回显"；default="" + show_default=False 防
    # "把已有密钥当默认值打印在提示里"（那等于把 key 抄进终端 scrollback / 录屏）。
    key = typer.prompt(
        f"粘贴你的 {spec.label} API Key"
        + ("（检测到环境变量已有，直接回车沿用它）" if existing else ""),
        hide_input=True, default="", show_default=False,
    )
    if not key:
        # 回车：优先沿用环境变量，其次 .env（都不打印出来）
        from .providers.config import load_env
        env, _ = load_env()
        key = existing or env.get(spec.requires_env_key, "")
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
    provider = _make_provider(offline, vendor, model)
    cand = _load_candidate(fixture)

    from rich.console import Console
    console = Console()
    console.print(f"[bold]调查候选[/]: [cyan]{cand.candidate_id}[/] ({cand.signal})")
    console.print(f"[dim]来源[/]: {fixture}")

    evidence, tools, agent = _build_agent(provider, model)
    report = agent.investigate(cand)
    _render_report(report)


@app.command()
def test(
    vendor: str | None = typer.Option(None, help="指定供应商，覆盖配置"),
    model: str | None = typer.Option(None, help="指定模型，覆盖配置"),
    offline: bool = typer.Option(False, help="离线演示（用 fake provider，无需 key）"),
    run_all: bool = typer.Option(
        False, "--all", help="非交互：依次调查全部候选后汇总（CI / 管道 / 演示脚本用）"
    ),
):
    """交互窗：从候选清单挑一个调查，也可导入自定义 JSON；一轮可对比多个结论。"""
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    from .candidates import TEST_CASES

    console = Console()
    provider = _make_provider(offline, vendor, model, interactive=True)

    results = []  # [(label, source, report)]

    def _run_and_render(label: str, source: str, cand: CandidateEvent) -> None:
        console.rule(f"[bold]{label}[/]")
        console.print(Panel(
            f"[cyan]{cand.candidate_id}[/]  {cand.signal}\n"
            f"[dim]部件[/] {cand.component_ref}  [dim]窗口[/] {cand.window_start}~{cand.window_end}\n"
            f"[dim]来源[/] {source}",
            title="候选",
            border_style="cyan",
        ))
        _, tools, agent = _build_agent(provider, model)
        if isinstance(provider, _FakeProvider):
            provider.reset()
        report = agent.investigate(cand)
        results.append((label, source, report))
        _render_report(report)

    # 第一次：列出候选清单
    list_t = Table(title="候选异常清单", box=None)
    list_t.add_column("#", justify="right")
    list_t.add_column("名称")
    for i, c in enumerate(TEST_CASES, 1):
        list_t.add_row(str(i), c.label)
    console.print(list_t)

    added = 0
    history = []

    # 非交互入口：显式开关，不依赖 isatty——
    # Windows 上 `</dev/null` 重定向后 `sys.stdin.isatty()` 仍报 True（实测），
    # 靠它判断会把管道/CI 误判成交互终端。
    if run_all:
        console.print("[dim]--all：依次调查全部候选（无需输入）[/]")
        for case in TEST_CASES:
            _run_and_render(case.label, "候选清单", _make_candidate(case))
            history.append(case.label)
            added += 1
        console.rule("[bold]本轮对比汇总[/]")
        _ui_module("rich_run").render_compare(results)
        return
    while True:
        choice = typer.prompt(
            f"\n挑选一个编号调查 [1-{len(TEST_CASES)}]，"
            "n=临时新建异常，b=对比上一步，q=结束并汇总，i=导入自定义 JSON",
            default="1",
        ).strip().lower()

        if choice == "q":
            break
        if choice == "n":
            cand, label, source = _prompt_new_candidate(console)
            if cand is None:
                continue
            _run_and_render(label, source, cand)
            history.append(label)
            added += 1
            continue
        if choice == "i":
            path = typer.prompt("自定义候选 JSON 路径（输入回车取消）", default="").strip()
            if not path:
                console.print("[dim]已取消导入[/]")
                continue
            cand = _try_load_custom(path)
            if cand is None:
                console.print(f"[red]无法加载 {path}[/]")
                continue
            label = f"自定义: {cand.candidate_id}"
            _run_and_render(label, str(path), cand)
            history.append(label)
            added += 1
            continue
        if choice == "b":
            if history:
                console.print("[dim]最近的调查如上（可回看报告）[/]")
            else:
                console.print("[dim]还没有上一步调查[/]")
            continue

        try:
            idx = int(choice)
            case = TEST_CASES[idx - 1]
        except (ValueError, IndexError):
            console.print(f"[yellow]无效输入，请输入 1~{len(TEST_CASES)} 或 n/i/b/q[/]")
            continue

        cand = _make_candidate(case)
        _run_and_render(case.label, "候选清单", cand)
        history.append(case.label)
        added += 1

    console.rule("[bold]本轮对比汇总[/]")
    if results:
        _ui_module("rich_run").render_compare(results)
    else:
        console.print("[dim]本轮未做任何调查。[/]")


def _try_load_custom(path: str) -> CandidateEvent | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return _load_candidate(p)
    except Exception:
        return None


# 可选作为临时异常部件；白名单外会返回 denied（有意的权限演示）
_ALLOWED = (
    "joint_motor.front_left_hip",
    "joint_motor.front_right_hip",
    "joint_motor.front_left_knee",
    "battery.main",
    "controller.main",
)


def _prompt_new_candidate(console):
    """问答式临时新建一个候选异常（n 进入），不用建文件。填完返回 (CandidateEvent, label, source)。"""
    from rich.panel import Panel
    console.print(Panel(
        "问答式临时新建候选异常。部件可用白名单（其他会返回 denied 演示权限拦截）：\n  "
        + "\n  ".join(_ALLOWED),
        title="临时新建 (n)",
        border_style="green",
    ))
    comp = typer.prompt("部件 (component_ref)", default="joint_motor.front_left_hip").strip()
    if not comp:
        console.print("[dim]已取消[/]")
        return None, None, None
    signal = typer.prompt("信号 (signal)", default="temperature_rising_under_normalized_load").strip() or "custom_signal"
    ws = typer.prompt("窗口开始 (window_start)", default="09:55").strip() or "09:55"
    we = typer.prompt("窗口结束 (window_end)", default="10:00").strip() or "10:00"
    sev = typer.prompt("严重度 (warning/critical)", default="warning").strip() or "warning"
    try:
        score = float(typer.prompt("异常分 (anomaly_score, 0~1)", default="0.5").strip() or "0.5")
    except ValueError:
        score = 0.5

    cand = CandidateEvent(
        candidate_id="manual-" + comp.replace(".", "_")[-24:],
        device_sn="device-abc123",
        component_ref=comp,
        signal=signal,
        severity_hint=sev,
        window_start=ws,
        window_end=we,
        sample_count=300,
        rule_matches=["MANUAL"],
        anomaly_score=score,
        detector_provenance="manual-entry",
    )
    return cand, f"临时: {comp}", "临时填写"


def _make_provider(offline: bool, vendor: str | None, model: str | None,
                   interactive: bool = False):
    """按 offline / 配置构造 provider；未配置 key 时给提示。"""
    from .providers.client import Provider

    if offline:
        return _FakeProvider()
    cfg = load_config()
    vid = vendor or cfg["vendor_id"]
    key = cfg["api_key"]
    if not key:
        hint = "交互窗里选个候选回车前，先 data-dog init 接入 key；或加 --offline 用离线条模型演示。" if interactive else "加 --offline 用离线条模型演示，或先 data-dog init"
        typer.echo(f"尚未配置 API Key。{hint}", err=True)
        raise typer.Exit(1)
    return Provider.from_config(vid, key)


def _build_agent(provider, model):
    from .agent.loop import AgentLoop
    from .evidence.store import EvidenceStore
    from .tools.investigation import InvestigationTools
    evidence = EvidenceStore()
    tools = InvestigationTools(evidence)
    trace_steps, trace_cb = _make_trace_cb()
    agent = AgentLoop(provider, tools, model=model, trace_cb=trace_cb)
    return evidence, tools, agent


def _make_candidate(case):
    """把 candidates.TestCase 转成 CandidateEvent（复用 candidates.make_candidate）。"""
    from .candidates import make_candidate
    return make_candidate(case)


def _ui_module(name: str):
    """从 ui/ 目录按路径加载模块（ui 不在包内，用路径导入）。"""
    import importlib.util
    from pathlib import Path
    here = Path(__file__).resolve()
    ui_path = here.parents[2] / "ui" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, ui_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 UI 模块 {name}（路径: {ui_path}）")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_trace_cb():
    return _ui_module("rich_run").make_trace_cb()


def _render_report(report):
    _ui_module("rich_run").render_report(report)


class _FakeProvider:
    """无 key 离线演示用：按候选部件挑剧本，保证不同候选结论有对比。

    剧本：
    - 左髋电机（温度爬升）→ 查证据 + 查近期变更 → needs_human_review（疑固件基线偏移）
    - 右髋电机 / 电池（数据正常）→ 只查证据 → rejected（排除误报，SOC/温度在界内）
    """
    def __init__(self):
        self.n = 0

    def reset(self):
        self.n = 0

    @staticmethod
    def _candidate(messages):
        """从 user 消息里抽出候选 JSON（兼容 dict 或 LLMChatMessage）。"""
        for m in messages:
            if isinstance(m, dict):
                role = m.get("role")
                content = m.get("content", "")
            else:  # LLMChatMessage dataclass
                role = getattr(m, "role", None)
                content = getattr(m, "content", "") or ""
            if role == "user":
                # 候选正文也被包成「不可信数据块」(A3) → 先剥壳，再取**第一个** JSON 对象：
                # 块尾还有 <<<END_UNTRUSTED_DATA>>>，直接 json.loads 会因尾随内容失败，
                # 于是候选退化成默认值、整条离线基线跑偏。
                body = unwrap(content)
                idx = body.find("{")
                if idx >= 0:
                    try:
                        return json.JSONDecoder().raw_decode(body[idx:])[0]
                    except Exception:
                        return {}
        return {}

    def chat(self, messages, tools=None, **kw):
        cand = self._candidate(messages)
        comp = cand.get("component_ref", "joint_motor.front_left_hip")
        ws = cand.get("window_start", "09:55")
        we = cand.get("window_end", "10:00")
        sn = cand.get("device_sn", "device-abc123")
        self.n += 1
        if self.n == 1:
            return self._tool("m1", "query_candidate_evidence", {
                "device_sn": sn, "component": comp,
                "window_start": ws, "window_end": we,
            })
        # 正常剧本（右髋 / 电池）：只查一次证据就下结论，排除
        if comp in ("joint_motor.front_right_hip", "battery.main"):
            return {"kind": "text", "content": json.dumps(
                self._conclusion(comp), ensure_ascii=False)}
        # 故障剧本（左髋温度）：再查近期变更，再下"需人工"结论
        if self.n == 2:
            return self._tool("m2", "query_recent_changes", {
                "device_sn": sn, "component": comp, "since": "2026-08-10",
            })
        return {"kind": "text", "content": json.dumps(
            self._conclusion(comp), ensure_ascii=False)}

    @staticmethod
    def _tool(_id, name, args):
        return {
            "kind": "tool_calls",
            "tool_calls": [{"id": _id, "name": name,
                            "arguments": json.dumps(args, ensure_ascii=False),
                            "content": ""}],
            "__raw_tool_calls": [],
        }

    @staticmethod
    def _conclusion(comp: str):
        if comp == "joint_motor.front_left_hip":
            return {
                "disposition": "needs_human_review", "severity": "warning",
                "confidence": 0.6,
                "summary": "温度78.2°C超过75°C阈值且电流上升，但固件3.2.1(8/12)变更冷却参数，需人工区分固件效应与真实退化。",
                "hypotheses": [{"cause": "固件3.2.1冷却变更导致", "status": "supported"},
                               {"cause": "轴承摩擦增大", "status": "supported"}],
                "missing_evidence": ["升级前基线"],
                "recommended_actions": ["复核固件基线", "检查轴承"],
            }
        if comp == "battery.main":
            return {
                "disposition": "rejected", "severity": "info", "confidence": 0.8,
                "summary": "BMS 检查：SOC 0.72 远高于低压告警阈值 0.2（BATT_SOC_LOW），电压平台正常，判定非异常。",
                "hypotheses": [], "missing_evidence": [],
                "recommended_actions": ["继续例行巡检"],
            }
        # 右髋电机（正常数据）
        return {
            "disposition": "rejected", "severity": "info", "confidence": 0.85,
            "summary": "温度61°C / 电流6.2A 均在正常基线内，未触发任何告警规则，判定为误报并排除。",
            "hypotheses": [], "missing_evidence": [],
            "recommended_actions": ["无需处理"],
        }


def _load_candidate(path: Path) -> CandidateEvent:
    if not path.exists():
        typer.echo(f"找不到候选异常文件: {path}", err=True)
        raise typer.Exit(1)
    data = json.loads(path.read_text(encoding="utf-8"))
    return CandidateEvent(**data)


if __name__ == "__main__":
    app()
