"""分区/保留**定时任务**的不变量（不依赖 helm）。

为什么要这组测试（而不是只靠 helm lint）
--------------------------------------
1. helm 对**缺失的 values 键不报错**：会渲染成空字符串（`schedule: ""`、
   `startingDeadlineSeconds:` 甚至是非法 YAML），只有真部署时才炸；
2. **失败安全**属性必须钉住：定时任务里"默认删数据"是不可接受的默认值，
   一个手滑把 `retention.dryRun` 改成 false，CronJob 就会每天真删；
3. 参数名要与 CLI 的真实 flag 对齐（`--ensure-months` / `--dry-run`），
   否则任务每晚失败而没人知道——**分区到期后写入才会报错**，中间是静默的。

渲染层面的不变量另见 `scripts/verify_chart.py`（需要 helm）。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
TPL = ROOT / "helm" / "data-dog" / "templates"
VALUES_FILE = ROOT / "helm" / "data-dog" / "values.yaml"
PARTITION_TPL = TPL / "cronjob-partition.yaml"
RETENTION_TPL = TPL / "cronjob-retention.yaml"
RULE_FILE = TPL / "prometheusrule.yaml"
CLI = ROOT / "scripts" / "partition_telemetry.py"
RETENTION_SRC = ROOT / "src" / "data_dog" / "retention.py"

_VALUES_REF = re.compile(r"\.Values\.([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*)")


def _values() -> dict[str, Any]:
    return yaml.safe_load(VALUES_FILE.read_text(encoding="utf-8"))


def _has_path(values: dict[str, Any], path: str) -> bool:
    cur: Any = values
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True


def test_cronjobs_are_gated_off_by_default():
    """装 chart 不该自己开始跑 DDL / 删数据：两个开关默认都必须是 false。"""
    values = _values()
    assert values["partition"]["enabled"] is False
    assert values["retention"]["enabled"] is False


def test_every_values_reference_exists_in_values_yaml():
    """模板引用的 `.Values.*` 必须在 values.yaml 里真实存在。

    helm 缺键不报错、渲染成空字符串——那正是"看着部署成功、任务其实是坏配置"的来源。
    """
    values = _values()
    for path in (PARTITION_TPL, RETENTION_TPL):
        text = path.read_text(encoding="utf-8")
        refs = set(_VALUES_REF.findall(text))
        assert refs, f"{path.name} 里没有解析到任何 .Values 引用？"
        missing = sorted(r for r in refs if not _has_path(values, r))
        assert not missing, f"{path.name} 引用了 values.yaml 里不存在的键：{missing}"


def test_partition_job_args_match_the_real_cli_flag():
    """参数必须与 CLI 的 argparse 完全一致（改名就红，而不是每晚静默失败）。"""
    text = PARTITION_TPL.read_text(encoding="utf-8")
    cli = CLI.read_text(encoding="utf-8")
    assert '"--ensure-months"' in cli, "CLI 的 flag 改名了？同步模板与本测试"
    assert "--ensure-months" in text
    assert ".Values.partition.ensureMonths" in text
    assert "scripts/partition_telemetry.py" in text


def test_retention_job_defaults_to_dry_run_and_can_never_force():
    """**失败安全**：默认 dry-run；模板里不该出现任何"真删"的开关。"""
    values = _values()
    assert values["retention"]["dryRun"] is True, "定时任务默认删数据是不可接受的默认值"

    text = RETENTION_TPL.read_text(encoding="utf-8")
    assert "--dry-run" in text
    # 只有 dryRun=true 才带该参数；且不存在任何绕过 dry-run 的强制开关。
    assert "if .Values.retention.dryRun" in text
    assert "--force" not in text and "FORCE" not in text

    # CLI 侧必须真的有 --dry-run 这个开关（否则模板传了个不存在的参数）。
    retention = RETENTION_SRC.read_text(encoding="utf-8")
    assert '"--dry-run"' in retention


def test_partition_alert_group_is_gated_by_partition_enabled():
    """分区告警只在声明了分区维护的部署里渲染（否则 absent 那条会永久误报）。"""
    rules = RULE_FILE.read_text(encoding="utf-8")
    idx = rules.index("DataDogPartitionHorizonLow")
    before = rules[:idx]
    assert "{{- if .Values.partition.enabled }}" in before
    # 该组的 `{{- end }}` 必须在两个告警之后（别把后面的规则也关进条件里）。
    assert rules.index("DataDogPartitionHorizonUnknown") < rules.rindex("{{- end }}")
    assert ".Values.prometheusRule.partitionHorizonSeconds" in rules
