"""告警规则的不变量：**不能引用不存在的指标**，且规则与文档两侧对齐。

为什么需要它（而不是只靠 helm lint）
-----------------------------------
Prometheus 对"引用了不存在的时间序列"的规则**不报错**：表达式对空向量求值，规则**永远
不触发**。于是最该响的那几条（审计写入失败、write-behind 丢数据）如果指标名写错一个字母，
就会**静默变成死规则**——比没有规则更糟，因为文档会说"已告警"。
`helm lint` 也查不出来（它只看 chart 语法，不看指标是否存在）。

本测试**不依赖 helm**（本仓不内置 helm 二进制），做三件事：
1. 规则里出现的每个 `data_dog_*` 标识符，必须在 `core/metrics.py` 注册表里真实存在；
2. 每条告警都有 `severity` / `summary` / `expr` / `for`（缺级别的告警等于没人知道该不该起床）；
3. 规则引用的 `.Values.prometheusRule.*` 阈值必须在 `values.yaml` 里有定义，
   且每条告警名都在 `docs/slo.md` §9 的表里——**声明与事实双向对齐**。

渲染层面的不变量（默认不渲染、开启后恰好一个对象）另见 `scripts/verify_chart.py`
（那条需要 helm）。
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from data_dog.core import metrics

ROOT = Path(__file__).resolve().parents[1]
RULE_FILE = ROOT / "helm" / "data-dog" / "templates" / "prometheusrule.yaml"
VALUES_FILE = ROOT / "helm" / "data-dog" / "values.yaml"
SLO_FILE = ROOT / "docs" / "slo.md"

_METRIC = re.compile(r"\bdata_dog_[a-z0-9_]+\b")
_ALERT = re.compile(r"^\s*- alert:\s*([A-Za-z0-9_]+)", re.MULTILINE)
_VALUES_REF = re.compile(r"\.Values\.prometheusRule\.([A-Za-z0-9_]+)")
# metrics.py 里以字符串字面量声明的指标名（唯一真源）。
_DECLARED = re.compile(r'"(data_dog_[a-z0-9_]+)"')
# PromQL 能引用到的派生样本名：histogram 的 _bucket/_count/_sum、counter 的 _total 等。
_SAMPLE_SUFFIXES = ("", "_total", "_bucket", "_count", "_sum", "_created")

METRICS_SRC = ROOT / "src" / "data_dog" / "core" / "metrics.py"


def _rule_text() -> str:
    return RULE_FILE.read_text(encoding="utf-8")


def _registered_metric_names() -> set[str]:
    """可被 PromQL 引用的名字集合。

    不能只看 `registry.collect()`：**带标签的指标在还没有子对象时不产出任何样本**
    （例如 http_requests_total 在首个请求前 samples 为空），那样会误判"指标不存在"。
    故以 metrics.py 的声明为真源，再按 Prometheus 的派生样本名展开。
    """
    base = set(_DECLARED.findall(METRICS_SRC.read_text(encoding="utf-8")))
    assert base, "metrics.py 里没有解析到任何 data_dog_* 指标声明？"
    # 注册表里的对象名也并进来：防"声明在注释里、其实没建"的漏网。
    live = {m.name for m in metrics.registry.collect()}
    return {b + s for b in base for s in _SAMPLE_SUFFIXES} | live


def test_rules_reference_only_registered_metrics():
    """规则引用的指标必须真实存在——否则是"永不触发的死规则"。"""
    referenced = set(_METRIC.findall(_rule_text()))
    assert referenced, "规则文件里没有解析到任何 data_dog_* 指标？"

    known = _registered_metric_names()
    unknown = sorted(n for n in referenced if n not in known)
    assert not unknown, (
        "告警规则引用了未注册的指标（PromQL 会静默求值为空 → 规则永不触发）：\n  "
        + "\n  ".join(unknown)
        + "\n修法：改名对齐 core/metrics.py，或在 metrics.py 里补定义。")


def test_every_alert_has_severity_summary_expr_and_for():
    """每条告警四件套齐全：expr / for / severity / summary。"""
    text = _rule_text()
    blocks = re.split(r"^\s*- alert:\s*", text, flags=re.MULTILINE)[1:]
    assert blocks, "没有解析到任何告警"

    for block in blocks:
        name = block.splitlines()[0].strip()
        for field in ("expr:", "for:", "severity:", "summary:"):
            assert field in block, f"告警 {name} 缺少 {field}"
        # 级别只能是那两个之一：多出来的级别意味着"没有路由规则会接住它"。
        sev = re.search(r"severity:\s*(\w+)", block)
        assert sev and sev.group(1) in {"critical", "warning"}, \
            f"告警 {name} 的 severity 不是 critical/warning：{sev and sev.group(1)}"


def test_threshold_knobs_are_defined_in_values():
    """规则引用的 `.Values.prometheusRule.*` 必须在 values.yaml 里有定义。

    helm 对缺失的值**不报错**，会渲染成空字符串 → 表达式变成
    `data_dog_investigation_queue_depth > ` 这种非法 PromQL，只有真正部署时才炸。
    """
    refs = set(_VALUES_REF.findall(_rule_text()))
    assert refs, "规则里没有引用 prometheusRule.* 阈值？"

    values = yaml.safe_load(VALUES_FILE.read_text(encoding="utf-8"))
    section = values.get("prometheusRule") or {}
    missing = sorted(r for r in refs if r not in section)
    assert not missing, (
        "规则引用了 values.yaml 里没有的阈值键（helm 会渲染成空 → 非法 PromQL）：\n  "
        + "\n  ".join(missing))


def test_every_alert_is_documented_in_slo_table():
    """每条告警名都要出现在 docs/slo.md §9 的表里——文档与规则是同一件事的两面。"""
    names = _ALERT.findall(_rule_text())
    assert names, "没有解析到任何告警名"

    slo = SLO_FILE.read_text(encoding="utf-8")
    undocumented = [n for n in names if n not in slo]
    assert not undocumented, (
        "这些告警没写进 docs/slo.md §9 的表（改规则必须同步文档）：\n  "
        + "\n  ".join(sorted(undocumented)))


def test_rules_are_off_by_default():
    """默认**不渲染** PrometheusRule：不是每个集群都装了 Prometheus Operator。"""
    values = yaml.safe_load(VALUES_FILE.read_text(encoding="utf-8"))
    assert values["prometheusRule"]["enabled"] is False
    assert "{{- if .Values.prometheusRule.enabled }}" in _rule_text()
