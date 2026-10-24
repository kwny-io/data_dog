"""离线验证台（scripts/verify_ci_offline.sh + scripts/pack_ci_evidence.py）的自检。

为什么值得单独守：离线验证台是"没有 GitHub Actions 时唯一的 CI 真话"，它一旦与
`.github/workflows/ci.yml` 漂移，产出的证据就会**静默残缺**——9 条腿只跑 8 条、
证据包却照样写着 PASS。这类错误没人会在界面上看见，只能用断言挡。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
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



PYPROJECT = REPO / "pyproject.toml"
PRE_PUSH = REPO / "scripts" / "git-hooks" / "pre-push"


def _marker_gates() -> list[tuple[str, str]]:
    """从 pyproject 的 markers 声明里取 (marker, 门控环境变量) 对。

    markers 里写的是「redis: 需要真实 Redis 的集成测试（设 DD_TEST_REDIS_URL 才运行）」,
    所以 pyproject 本身就是"哪条腿靠哪个变量"的单一真源，测试不再抄一份。
    """
    text = PYPROJECT.read_text(encoding="utf-8")
    block = re.search(r"markers\s*=\s*\[(.*?)\]", text, re.S)
    assert block, "pyproject.toml 里找不到 pytest markers 声明"
    pairs = re.findall(r'"(\w+):[^"]*?设\s*(\w+)\s*才运行', block.group(1))
    assert pairs, "markers 声明里没有匹配到「设 XXX 才运行」的门控变量"
    return pairs


def _usable_bash() -> str | None:
    """找一个**真能执行**的 bash，而不是名字叫 bash 的东西。

    本机踩过的坑（2026-10-25）：`shutil.which("bash")` 会命中
    `C:\\Windows\\System32\\bash.EXE`，那是 **WSL 的 relay**，执行时直接
    `execvpe(/bin/bash) failed: No such file or directory` 返回 1。
    于是这些“行为级断言”钩子的用例会报一个**假的红**（不是钩子坏，是解释器选错），
    而这恰恰是本文件最恨的错误类型：守卫自己骗人。
    所以候选逐个**实跑 `-c echo`** 验证，全失败就 skip（skip ≠ pass，不会被当绿）。
    """
    candidates = [
        os.environ.get("DD_BASH", ""),
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        shutil.which("bash") or "",
    ]
    for cand in candidates:
        if not cand or not os.path.exists(cand):
            continue
        try:
            probe = subprocess.run([cand, "-c", "echo usok"], capture_output=True,
                                   encoding="utf-8", errors="replace", timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0 and "usok" in (probe.stdout or ""):
            return cand
    return None


def _skip_warning_output(**overrides: str) -> str:
    """**真的执行** pre-push 的提示出口，拿到它实际打印的文本。

    行为级而非文本级：断言"钩子文件里有 DD_TEST_REDIS_URL"是弱守卫——
    把分支改成 `if false` 断言照样绿（实测过）。执行出口才能证明提示可达。
    """
    bash = _usable_bash()
    if not bash:
        pytest.skip("本机没有可用的 bash（System32 那个是 WSL relay，跑不动），跳过行为断言")
    env = {k: v for k, v in os.environ.items()
           if k not in ("DD_TEST_PG_URL", "DD_TEST_REDIS_URL")}
    env.update(overrides)
    r = subprocess.run(
        [bash, str(PRE_PUSH), "--print-skip-warnings"],
        # 钩子输出是 UTF-8；Windows 默认按 GBK 解码会炸（cp936 解不开中文提示）。
        capture_output=True, encoding="utf-8", errors="replace", env=env,
        timeout=60, cwd=str(REPO),
    )
    assert r.returncode == 0, f"提示出口非零退出：{r.returncode} / {r.stderr}"
    return r.stdout


def test_pre_push_actually_warns_about_every_marker_gate():
    """每条需要真服务的腿，push 时都必须被**真的提示**出来（没跑 ≠ 通过）。"""
    out = _skip_warning_output()
    missing = [
        f"{marker}→{env}"
        for marker, env in _marker_gates()
        if env not in out or marker not in out
    ]
    assert not missing, (
        "pre-push 的跳过提示漏了这些腿/门控变量：" + ", ".join(missing)
        + "。实际输出：" + out
    )


def test_pre_push_does_not_conflate_marker_gates():
    """门控变量不能互相冒充：redis 的提示里不许出现 PG 的变量名（反之亦然）。"""
    out = _skip_warning_output()
    gates = dict(_marker_gates())
    pg_env, redis_env = gates.get("postgres"), gates.get("redis")
    if not (pg_env and redis_env):
        return  # marker 声明不完整时由上面的用例负责报错
    for line in out.splitlines():
        if not line.strip():
            continue
        assert not (pg_env in line and redis_env in line), (
            f"同一行同时出现两个门控变量（提示语在冒充）：{line}"
        )
    # 反向对照：两边的提示确实各出现过一次，否则上面的断言会空转
    assert any(pg_env in ln for ln in out.splitlines()), f"没有 {pg_env} 的提示"
    assert any(redis_env in ln for ln in out.splitlines()), f"没有 {redis_env} 的提示"


def test_each_gate_is_controlled_only_by_its_own_variable():
    """**环境矩阵**：每条腿的提示只由自己的门控变量决定。

    这条用例是为一个真实的 bug 写的：redis 的提示曾挂在 `DD_TEST_PG_URL` 名下，
    于是"设了 PG 变量、没设 redis"的人永远收不到 redis 被跳过的警告（静默跳过）。
    只看"两行提示都出现过"是抓不到的——必须按组合验。
    """
    gates = dict(_marker_gates())
    pg_env, redis_env = gates.get("postgres"), gates.get("redis")
    if not (pg_env and redis_env):
        pytest.skip("markers 声明不完整，无法按变量名做矩阵断言")
    pg_marker, redis_marker = "postgres", "redis"

    # 1) 只设 PG：redis 必须被提示，PG 不许被提示
    out = _skip_warning_output(**{pg_env: "postgresql+psycopg://x/y"})
    assert redis_env in out and redis_marker in out, f"只设 PG 时没提示 redis：{out}"
    assert "未设 " + pg_env not in out, f"设了 {pg_env} 却仍提示未设：{out}"

    # 2) 只设 redis：PG 必须被提示，redis 不许被提示
    out = _skip_warning_output(**{redis_env: "redis://127.0.0.1:6379/0"})
    assert pg_env in out and pg_marker in out, f"只设 redis 时没提示 PG：{out}"
    assert "未设 " + redis_env not in out, f"设了 {redis_env} 却仍提示未设：{out}"

    # 3) 两个都设：一条提示都不该有
    out = _skip_warning_output(**{pg_env: "postgresql+psycopg://x/y",
                                  redis_env: "redis://127.0.0.1:6379/0"})
    assert "未设 " not in out, f"两个门都设了却仍提示未设：{out}"
