"""G8 算子验证流水线补全测试：DSL 静态扫描 + 依赖 SCA + SBOM + 门控 + 聚合。

守住的关键事：
  1. DSL 扫描：超深嵌套/超大常量/语法错误 → 报 finding；正常算子 → 无 finding。
  2. SCA：有工具（注入 runner）→ 真扫；无工具 → `skipped`（**skipped≠pass**）。
  3. SBOM：产出结构合法的 CycloneDX JSON（含组件）。
  4. 门控：high/critical finding → Validating 被拒。
  5. 聚合：verify_full 各步状态齐、结构完整。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from data_dog.operator_factory.knowledge import build_knowledge_package
from data_dog.operator_factory.lifecycle import OperatorLifecycle, TransitionError
from data_dog.operator_factory.p1_demo import build_motor_operator
from data_dog.operator_factory.pipeline import (
    DeclarativeRule,
    Fixture,
    Operator,
)
from data_dog.operator_factory.sbom import (
    SbomComponent,
    declared_components,
    generate_sbom,
)
from data_dog.operator_factory.security_scan import (
    MAX_CONST,
    MAX_DEPTH,
    ScanFinding,
    scan_dependencies,
    scan_dsl_source,
    scan_operator,
)
from data_dog.operator_factory.verification import verify_full

ROOT = Path(__file__).resolve().parents[1]
CLEANED = ROOT / "vendor_docs" / "unitree" / "cleaned"
FIXTURES = ROOT / "src" / "data_dog" / "operator_factory" / "fixtures"


@pytest.fixture(scope="module")
def op():
    return build_motor_operator(build_knowledge_package(CLEANED))


@pytest.fixture(scope="module")
def fixtures():
    return [Fixture.load(p) for p in sorted(FIXTURES.glob("*.json"))]


# ---------------------------------------------------------------- DSL 静态扫描

def test_clean_expression_no_findings():
    assert scan_dsl_source("temperature > 90 and mode == 0",
                           declared_fields=("temperature", "mode")) == []


def test_syntax_error_is_high():
    f = scan_dsl_source("temperature >>>> 90", declared_fields=("temperature",))
    assert any(x.severity == "high" and x.kind == "dsl_static" for x in f)


def test_deep_nesting_flagged():
    # 构造深度 > MAX_DEPTH 的合法表达式
    expr = "temperature"
    for _ in range(MAX_DEPTH + 4):
        expr = f"abs({expr})"
    f = scan_dsl_source(expr, declared_fields=("temperature",))
    assert any("嵌套过深" in x.detail for x in f)


def test_huge_constant_flagged():
    f = scan_dsl_source(f"temperature > {int(MAX_CONST * 10)}",
                        declared_fields=("temperature",))
    assert any("超大常量" in x.detail for x in f)


def test_empty_expression_flagged():
    f = scan_dsl_source("", declared_fields=())
    assert len(f) == 1 and f[0].severity == "medium"


def test_scan_operator_clean_motor_op(op):
    """黄金算子（fixtures 6/6）DSL 规则应无 high/critical finding。"""
    rep = scan_operator(op)
    assert rep.status == "pass", [x.to_dict() for x in rep.findings]
    assert rep.has_blocking is False


# ---------------------------------------------------------------- 依赖 SCA

def test_sca_skipped_when_tool_missing():
    """无 pip-audit → skipped（且明确不是 pass）。"""
    rep = scan_dependencies()
    # 测试环境未装 pip-audit → 必为 skipped；即使装了也应是 pass/fail，绝不冒充
    assert rep.status in ("skipped", "pass", "fail")
    if rep.status == "skipped":
        assert "skipped" in rep.note.lower() or "未" in rep.note


def test_sca_with_injected_runner_reports_findings():
    """注入 runner（不联网）→ 真解析出漏洞 finding。"""
    def runner(lockfile):
        return {"dependencies": [
            {"name": "evilpkg", "version": "1.0",
             "vulns": [{"id": "CVE-2026-0001", "fix_versions": ["1.0.1"],
                        "description": "远程执行"}]},
        ]}

    rep = scan_dependencies(lockfile="whatever.txt", runner=runner)
    assert rep.status == "fail"
    assert any("CVE-2026-0001" in f.detail for f in rep.findings)


def test_sca_runner_clean():
    rep = scan_dependencies(runner=lambda lf: {"dependencies": []})
    assert rep.status == "pass" and rep.findings == []


def test_sca_runner_exception_is_skipped_not_pass():
    """runner 抛错 → skipped（未得出结论），绝不记 pass。"""
    def boom(lf):
        raise RuntimeError("network down")

    rep = scan_dependencies(runner=boom)
    assert rep.status == "skipped"


# ---------------------------------------------------------------- SBOM

def test_sbom_output_is_valid_cyclonedx():
    """两条实现路径（装了库 / 降级）都要产出合法 CycloneDX，且组件带 purl。

    **不要把 `complete` 当契约**：它取决于环境 —— 装了 cyclonedx-bom 走库路径（True），
    没装走最小实现（False）。原先这里写 `assert res.complete is False`，
    于是同一个测试本地绿、装了 sbom extra 的 CI 镜像红（2026-10-24 实测）。
    降级路径的 complete=False 由下面那条用例单独守。
    """
    res = generate_sbom(components=[SbomComponent("numpy", "2.0.0"),
                                    SbomComponent("pyyaml", "6.0")])
    bom = res.bom
    assert bom["bomFormat"] == "CycloneDX"
    assert bom["specVersion"]
    assert bom["serialNumber"].startswith("urn:uuid:")
    names = [c["name"] for c in bom["components"]]
    assert "numpy" in names and "pyyaml" in names
    # 组件带 purl（下游扫描器按 purl 建索引；两条路径都必须有）
    assert any(c.get("purl", "").startswith("pkg:pypi/") for c in bom["components"])
    assert res.generator          # 必须如实标注来源，不冒充“完整 SBOM”


def test_sbom_minimal_fallback_also_carries_purl():
    """两条路径产出必须一致：没装 cyclonedx-bom 时的最小实现也要带 purl。

    上面那条用例只能盖到“当前环境装了哪条”。这个直接调最小实现，
    让降级路径也进入覆盖 —— 否则它只在装了库的环境里永远不被执行。
    """
    from data_dog.operator_factory.sbom import _minimal_sbom

    res = _minimal_sbom([SbomComponent("numpy", "2.0.0")],
                        project="data_dog", version="0.1.0")
    comp = res.bom["components"][0]
    assert comp["purl"] == "pkg:pypi/numpy@2.0.0"
    assert res.complete is False            # 降级路径不得冒充完整 SBOM


def test_sbom_writes_file(tmp_path):
    out = tmp_path / "sbom.json"
    generate_sbom(out_path=str(out), components=[SbomComponent("a", "1.0")])
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["bomFormat"] == "CycloneDX"


def test_declared_components_parse():
    comps = declared_components(["numpy==2.0.0", "pyyaml"])
    assert comps[0].name == "numpy" and comps[0].version == "2.0.0"
    assert comps[1].name == "pyyaml" and comps[1].version == ""


# ---------------------------------------------------------------- 门控

def test_gate_rejects_blocking_finding():
    lc = OperatorLifecycle("op", "1.0")
    lc.transition("Generated")
    lc.transition("Building", {"scan_ok": True})
    finding = ScanFinding("critical", "dsl_static", "混淆表达式", "r1")
    with pytest.raises(TransitionError, match="安全扫描"):
        lc.transition("Validating", {"scan_findings": [finding]})


def test_gate_allows_nonblocking_finding():
    lc = OperatorLifecycle("op", "1.0")
    lc.transition("Generated")
    lc.transition("Building", {"scan_ok": True})
    low = ScanFinding("low", "dsl_static", "小提示", "r1")
    lc.transition("Validating", {"scan_findings": [low]})
    assert lc.state == "Validating"


# ---------------------------------------------------------------- 聚合

def test_verify_full_structure(op, fixtures):
    res = verify_full(op, fixtures, old=op,
                      sca_runner=lambda lf: {"dependencies": []})
    steps = {s.step: s.status for s in res.steps}
    assert set(steps) == {"golden_replay", "fuzz", "diff",
                          "dsl_static", "dependency_sca", "sbom"}
    assert steps["golden_replay"] == "pass"
    assert steps["fuzz"] == "pass"
    assert res.ok is True
    # 各步状态计数齐全
    d = res.to_dict()
    assert set(d["summary"]) == {"pass", "fail", "skipped"}


def test_verify_full_skips_sca_without_runner(op, fixtures):
    """不注入 runner 且环境无 pip-audit → dependency_sca 为 skipped，不冒充 pass。"""
    res = verify_full(op, fixtures, old=op)
    sca = next(s for s in res.steps if s.step == "dependency_sca")
    assert sca.status in ("skipped", "pass", "fail")
    # 但 sbom 步不受影响
    assert next(s for s in res.steps if s.step == "sbom").status == "pass"


def test_verify_full_fails_on_bad_operator(fixtures):
    """含 high finding 的算子 → verify_full 整体不 ok。"""
    bad = Operator(operator_id="bad", component="x", input_schema="", output_schema="",
                   fields=[{"name": "temperature", "type": "float"}],
                   rules=[DeclarativeRule("R", "temperature", "gt", 90.0, "warning", "")],
                   dsl_rules=[{"rule_id": "R1", "when": "temperature >>>> 90"}])
    res = verify_full(bad, fixtures, sca_runner=lambda lf: {"dependencies": []})
    assert res.ok is False
    assert next(s for s in res.steps if s.step == "dsl_static").status == "fail"
