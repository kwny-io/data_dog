"""G8：算子编译期安全扫描（DSL 静态 + 依赖 SCA）。

对齐架构 §8.4 验证流水线：在 `Validating` 阶段补两步——① **DSL 静态扫描**（自研，
零依赖）；② **依赖 SCA**（`pip-audit`，**可选依赖**，缺失时 `skipped`）。

诚实边界（关键）
----------------
- **DSL 静态扫描是启发式**：能找出可疑模式（超深嵌套/超长表达式/超大常量），
  **不能证明无漏洞**——它是纵深防御的一层，动态防线是 P4-1 沙箱。检出即报，
  不宣称"证明安全"。
- **`skipped ≠ pass`**：SCA 工具缺失时状态是 `skipped`（明确标注），**绝不**记
  `pass`——否则"没查"会被误读为"查了且没问题"。这是本模块的诚实核心。
- **不执行任何算子代码**：DSL 扫描只做词法/语法/AST 检查，不 run 算子。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .dsl import Binary, Call, DSLError, Num, Parser, Unary, tokenize
from .pipeline import Operator

# ---- 阈值（模块常量，测试据此构造用例）----
MAX_EXPR_LEN = 400          # 单条表达式最长字符数（超长疑混淆）
MAX_DEPTH = 16              # AST 最大嵌套深度（超深嵌套 = 资源风险）
MAX_NODES = 300             # AST 最大节点数（表达式过大）
MAX_CONST = 1e15            # 常量绝对值上限（超大常量）

# 判定为"必须拦下"的严重度
BLOCKING = ("high", "critical")


@dataclass(frozen=True)
class ScanFinding:
    severity: str               # low / medium / high / critical
    kind: str                   # dsl_static / dependency
    detail: str
    location: str = ""          # 规则 id / 包名等定位信息

    def to_dict(self) -> dict[str, Any]:
        return {"severity": self.severity, "kind": self.kind,
                "detail": self.detail, "location": self.location}


@dataclass
class ScanReport:
    """一步扫描的结果。`status` ∈ {pass, fail, skipped}——**skipped ≠ pass**。"""
    step: str
    status: str
    findings: list[ScanFinding] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"step": self.step, "status": self.status,
                "findings": [f.to_dict() for f in self.findings],
                "note": self.note}

    @property
    def has_blocking(self) -> bool:
        return any(f.severity in BLOCKING for f in self.findings)


# ---------------------------------------------------------------- AST 度量

def _depth(node: Any) -> int:
    if isinstance(node, Unary):
        return 1 + _depth(node.operand)
    if isinstance(node, Binary):
        return 1 + max(_depth(node.left), _depth(node.right))
    if isinstance(node, Call):
        return 1 + max((_depth(a) for a in node.args), default=0)
    return 1


def _count(node: Any) -> int:
    if isinstance(node, Unary):
        return 1 + _count(node.operand)
    if isinstance(node, Binary):
        return 1 + _count(node.left) + _count(node.right)
    if isinstance(node, Call):
        return 1 + sum(_count(a) for a in node.args)
    return 1


def _max_const(node: Any) -> float:
    if isinstance(node, Num):
        return abs(node.value)
    if isinstance(node, Unary):
        return _max_const(node.operand)
    if isinstance(node, Binary):
        return max(_max_const(node.left), _max_const(node.right))
    if isinstance(node, Call):
        return max((_max_const(a) for a in node.args), default=0.0)
    return 0.0


# ---------------------------------------------------------------- DSL 静态扫描

def scan_dsl_source(source: str, *, declared_fields: tuple[str, ...] = (),
                    rule_id: str = "") -> list[ScanFinding]:
    """对一条 DSL 表达式做静态检查（不执行）。

    检出：语法错误 / 超长表达式 / 超深嵌套 / 节点过载 / 超大常量。
    所有这些都只是**启发式风险信号**，不代表一定有问题。
    """
    loc = rule_id or source[:40]
    findings: list[ScanFinding] = []

    if not source or not source.strip():
        return [ScanFinding("medium", "dsl_static", "空表达式", loc)]

    if len(source) > MAX_EXPR_LEN:
        findings.append(ScanFinding(
            "medium", "dsl_static",
            f"表达式过长（{len(source)} > {MAX_EXPR_LEN} 字符），疑似混淆",
            loc))

    try:
        ast = Parser(tokenize(source)).parse(declared_fields)
    except DSLError as e:
        findings.append(ScanFinding("high", "dsl_static",
                                    f"DSL 语法/类型错误: {e}", loc))
        return findings

    d = _depth(ast)
    if d > MAX_DEPTH:
        findings.append(ScanFinding(
            "high", "dsl_static",
            f"嵌套过深（深度 {d} > {MAX_DEPTH}），疑资源耗尽风险", loc))

    n = _count(ast)
    if n > MAX_NODES:
        findings.append(ScanFinding(
            "medium", "dsl_static",
            f"表达式节点过多（{n} > {MAX_NODES}）", loc))

    mc = _max_const(ast)
    if mc > MAX_CONST:
        findings.append(ScanFinding(
            "medium", "dsl_static",
            f"超大常量（{mc:g} > {MAX_CONST:g}），疑溢出/混淆", loc))

    return findings


def scan_operator(op: Operator) -> ScanReport:
    """扫描一个算子的所有 DSL 规则。声明式规则无表达式，不在本步范围。"""
    declared = tuple(f["name"] for f in op.fields)
    findings: list[ScanFinding] = []
    for r in op.dsl_rules:
        src = str(r.get("when", ""))
        findings.extend(scan_dsl_source(src, declared_fields=declared,
                                        rule_id=str(r.get("rule_id", ""))))
    status = "fail" if any(f.severity in BLOCKING for f in findings) else "pass"
    return ScanReport("dsl_static", status, findings)


# ---------------------------------------------------------------- 依赖 SCA

def _parse_pip_audit(raw: Any) -> list[ScanFinding]:
    """解析 pip-audit JSON（{'dependencies': [{name,version,vulns:[...]}]}）。"""
    out: list[ScanFinding] = []
    deps = raw.get("dependencies", []) if isinstance(raw, dict) else []
    for d in deps:
        if not isinstance(d, dict):
            continue
        name = str(d.get("name", ""))
        ver = str(d.get("version", ""))
        for v in d.get("vulns", []) or []:
            if not isinstance(v, dict):
                continue
            vid = str(v.get("id", ""))
            fix = ",".join(str(x) for x in (v.get("fix_versions") or []))
            desc = str(v.get("description", ""))[:120]
            out.append(ScanFinding(
                "high", "dependency",
                f"{name}=={ver} 存在漏洞 {vid}"
                + (f"（修复版本 {fix}）" if fix else "")
                + (f": {desc}" if desc else ""),
                name))
    return out


def _default_pip_audit_runner(lockfile: str) -> Any:
    """默认 SCA 执行器：跑 `pip-audit -f json`（对 lockfile 或当前环境）。

    两个必须显式处理的边界（都实测过）：
    1. **编码**：`text=True` 默认 UTF-8，而 Windows 中文 locale 下工具输出可能是 GBK/CP936。
       解码失败会发生在 subprocess 的读取线程里 → `proc.stdout` 变 **None**，
       而旧代码 `json.loads(proc.stdout or "{}")` 会把它变成 `{}` → 无 finding → **status=pass**。
       即“扫描器崩了，报告说通过”。给 `encoding+errors` 让它变成可读的乱码而不是 None，
       再把空输出当失败抛出去（`scan_dependencies` 会转成 `skipped≠pass`）。
    2. **退出码/空输出**：工具非 0 退出或一行没输出时，`{}` 不是“干净”，是“没扫成”。
    """
    cmd = [sys.executable, "-m", "pip_audit", "-f", "json"]
    if lockfile:
        cmd += ["-r", lockfile]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False)
    out = (proc.stdout or "").strip()
    if not out:
        raise RuntimeError(
            f"pip-audit 未输出任何内容（rc={proc.returncode}），无法判定依赖安全状态"
            + ((proc.stderr or "").strip()[:200] and
               f": {(proc.stderr or '').strip()[:200]}"))
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"pip-audit 输出不是合法 JSON: {e}") from e


def scan_dependencies(*, lockfile: str = "",
                      runner: Callable[[str], Any] | None = None
                      ) -> ScanReport:
    """依赖 SCA。`pip-audit` 为**可选依赖**：缺失 → `skipped`（非静默通过）。

    `runner` 可注入（测试不联网、不装工具）。签名 `(lockfile) -> parsed_json`。
    """
    if runner is None:
        if importlib.util.find_spec("pip_audit") is None:
            return ScanReport(
                "dependency", "skipped", [],
                "未安装 pip-audit（可选依赖 `scan`），SCA 未执行——skipped≠pass")
        runner = _default_pip_audit_runner
    try:
        raw = runner(lockfile)
    except Exception as e:                          # noqa: BLE001
        return ScanReport("dependency", "skipped", [],
                          f"SCA 执行失败，未得出结果: {e}（skipped≠pass）")
    findings = _parse_pip_audit(raw)
    status = "fail" if any(f.severity in BLOCKING for f in findings) else "pass"
    return ScanReport("dependency", status, findings)


__all__ = [
    "BLOCKING",
    "MAX_CONST",
    "MAX_DEPTH",
    "MAX_EXPR_LEN",
    "MAX_NODES",
    "ScanFinding",
    "ScanReport",
    "scan_dependencies",
    "scan_dsl_source",
    "scan_operator",
]
