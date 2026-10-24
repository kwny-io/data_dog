"""离线验证台（scripts/verify_ci_offline.sh + scripts/pack_ci_evidence.py）的自检。

为什么值得单独守：离线验证台是"没有 GitHub Actions 时唯一的 CI 真话"，它一旦与
`.github/workflows/ci.yml` 漂移，产出的证据就会**静默残缺**——9 条腿只跑 8 条、
证据包却照样写着 PASS。这类错误没人会在界面上看见，只能用断言挡。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "verify_ci_offline.sh"
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"


def _offline_legs() -> list[str]:
    """取 `ALL_LEGS=` 里的腿名（单一真源在脚本里，不在本测试里抄一份）。"""
    text = SCRIPT.read_text(encoding="utf-8")
    m = re.search(r'^ALL_LEGS="([^"]+)"', text, re.M)
    assert m, "scripts/verify_ci_offline.sh 里找不到 ALL_LEGS=\"...\""
    return m.group(1).split()


def _ci_jobs() -> set[str]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return set(data["jobs"])


def test_offline_legs_match_ci_jobs():
    """离线 9 腿必须与 ci.yml 的 job 集合**逐个对齐**（不是"超集"也不是"子集"）。

    对齐的含义：本地跑 `all` 得到的证据，就是 CI 会跑的那 9 个 job。
    任一侧新增/改名而另一侧没跟上 → 这条断言红，逼你补腿或改证据话术。
    """
    legs, jobs = set(_offline_legs()), _ci_jobs()
    assert legs == jobs, (
        "离线验证腿与 CI job 不一致：\n"
        f"  只在离线脚本里：{sorted(legs - jobs) or '无'}\n"
        f"  只在 ci.yml 里：{sorted(jobs - legs) or '无'}\n"
        "改一边就改另一边，否则离线证据会静默缺腿。"
    )


def test_pack_reads_image_ids_from_run_manifest(tmp_path):
    """打包器要能自己从 manifest.txt 回收镜像名，不能等使用者记得传 --image。

    2026-10-24 实测：验证脚本把镜像摘要写进 manifest 了，打包时忘了传 `--image`，
    EVIDENCE.md 里就只剩"（未提供）"——已有证据被丢掉，而结论照样写 PASS。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "pack_ci_evidence", REPO / "scripts" / "pack_ci_evidence.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    run = tmp_path / "run"
    run.mkdir()
    (run / "manifest.txt").write_text(
        "# 快照\n"
        "docker server 29.4.0 linux/amd64\n"
        "dd-ci-python:3.12 -> sha256:cf38364ca2b2\n"
        "dd-ci-node:22 -> sha256:4f70530d2830\n"
        "postgres:16-alpine -> postgres@sha256:cf78e7\n",
        encoding="utf-8",
    )
    images = mod._images_from_run_manifest(run)
    assert images == ["dd-ci-python:3.12", "dd-ci-node:22", "postgres:16-alpine"]
    # manifest 缺失时安静回退到空列表（再由 --image 或"未提供"兜底）
    assert mod._images_from_run_manifest(tmp_path / "nope") == []


def _load(name: str, fname: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / fname)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_schema_names_match_kubeconform_resolution():
    """schema 文件名必须与 kubeconform 的解析结果一致（**全小写**）。

    2026-10-24 实测：给 `ServiceAccount-v1.json`，kubeconform 找不到 schema、
    记成 Skipped 并返回 0 —— 一次"零校验的绿"。这里把命名规则钉死。
    """
    vs = _load("verify_schemas", "verify_schemas.py")
    assert vs.schema_name("ServiceAccount", "v1") == "serviceaccount-v1.json"
    assert vs.schema_name("Deployment", "apps/v1") == "deployment-apps-v1.json"
    assert vs.schema_name("HorizontalPodAutoscaler", "autoscaling/v2") == \
        "horizontalpodautoscaler-autoscaling-v2.json"


def test_every_rendered_kind_has_vendored_schema():
    """chart 渲染出的 kind（除显式跳过的 CRD）都必须有本地 schema 文件。"""
    vs = _load("verify_schemas", "verify_schemas.py")
    docs = [
        {"kind": "Service", "apiVersion": "v1"},
        {"kind": "ServiceAccount", "apiVersion": "v1"},
        {"kind": "ConfigMap", "apiVersion": "v1"},
        {"kind": "Deployment", "apiVersion": "apps/v1"},
        {"kind": "Job", "apiVersion": "batch/v1"},
        {"kind": "PodDisruptionBudget", "apiVersion": "policy/v1"},
        {"kind": "HorizontalPodAutoscaler", "apiVersion": "autoscaling/v2"},
        {"kind": "PrometheusRule", "apiVersion": "monitoring.coreos.com/v1"},
        {"kind": "ServiceMonitor", "apiVersion": "monitoring.coreos.com/v1"},
    ]
    assert vs.missing_schemas(docs) == []
    # 新增一个未知 kind 必须被抓出来（否则只是静默降级到联网校验）
    assert vs.missing_schemas(docs + [{"kind": "Ingress", "apiVersion": "networking.k8s.io/v1"}]) == [
        "ingress-networking.k8s.io-v1.json"]


def test_kubeconform_summary_assertion_catches_zero_validation():
    """`Skipped: N` 却被当成通过 —— 假绿的核心判据。"""
    ak = _load("assert_kc_summary", "assert_kc_summary.py")
    ok = ak.parse_summary("Summary: 6 resources found parsing stdin - Valid: 6, Invalid: 0, Errors: 0, Skipped: 0")
    assert ok == {"res": 6, "valid": 6, "invalid": 0, "errors": 0, "skipped": 0}

    import io
    import sys

    def run(text: str, expect: int) -> int:
        old = sys.stdin
        sys.stdin = io.StringIO(text)
        try:
            return ak.main(["x", str(expect)])
        finally:
            sys.stdin = old

    assert run("Summary: 6 resources - Valid: 6, Invalid: 0, Errors: 0, Skipped: 0", 0) == 0
    # 全部被跳过（schema 没匹配上）→ 必须红
    assert run("Summary: 6 resources - Valid: 0, Invalid: 0, Errors: 0, Skipped: 6", 0) == 1
    # 生产值里 2 个 CRD 跳过是预期的
    assert run("Summary: 10 resources - Valid: 8, Invalid: 0, Errors: 0, Skipped: 2", 2) == 0

