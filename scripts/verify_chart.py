"""校验 Helm chart 渲染结果（P4-4 验收 1）。

为什么自建而不是只靠 `helm lint`
--------------------------------
`helm lint` 只查 chart 语法与惯例；**它不会**发现这类"rendered 出来但语义错误"的问题：
  - API 的 envFrom 里混进了 `DD_STREAM_ENABLED=1` → API 也起消费者 → 重复消费（真踩过）；
  - 迁移 Job 少了 hook 注解 → 部署时不会在应用前跑 → 首启连不上表；
  - HPA 开了但 Deployment 还写死 `replicas` → HPA 与实际副本数打架；
  - file 源配多副本 **没有** 被拒绝 → 静默重复消费。
本脚本把模板渲染出来，再用断言钉住这些**不变量**。渲染本身有错（含 `fail` 拦下的
误配）会以非零码返回——那正是"失败关闭"该有的样子。

用法：
    python scripts/verify_chart.py            # 校验默认值 + 生产覆盖值
需要 PATH 中有 `helm`（CI 里由 azure/setup-helm 提供；本地装一份）。

退出码：0=全过；1=有不变量被破坏；2=helm 不可用。
"""
from __future__ import annotations

import shutil
import subprocess
import sys

import yaml

# Windows 控制台默认 GBK，中文输出会乱码、个别符号（如 ✅）会直接抛 UnicodeEncodeError。
for _stream in (sys.stdout, sys.stderr):
    _reconf = getattr(_stream, "reconfigure", None)
    if _reconf is not None:
        _reconf(encoding="utf-8")

CHART = "./helm/data-dog"
RELEASE = "dd"


def _helm() -> str:
    exe = shutil.which("helm")
    if not exe:
        print("[verify_chart] 未找到 helm（请安装后重试）", file=sys.stderr)
        sys.exit(2)
    return exe


def render(helm: str, extra: list[str] | None = None) -> tuple[int, str, str]:
    cmd = [helm, "template", RELEASE, CHART, *(extra or [])]
    # 显式 utf-8：Windows 控制台默认 GBK，helm 输出的多字节字符会解码报错
    p = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return p.returncode, p.stdout or "", p.stderr or ""


def docs(text: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]


def _by_kind(ds: list[dict], kind: str, component: str | None = None) -> list[dict]:
    out = []
    for d in ds:
        if d.get("kind") != kind:
            continue
        if component is not None:
            labels = d.get("spec", {}).get("template", {}).get(
                "metadata", {}).get("labels", {})
            labels = labels or d.get("metadata", {}).get("labels", {})
            if labels.get("app.kubernetes.io/component") != component:
                continue
        out.append(d)
    return out


_FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  [OK]   {msg}")
    else:
        print(f"  [FAIL] {msg}")
        _FAILS.append(msg)


def _env_names(container: dict) -> set[str]:
    return {e.get("name", "") for e in container.get("env", []) if isinstance(e, dict)}


def _envfrom_names(deployment: dict) -> set[str]:
    """envFrom 引用的 ConfigMap/Secret 名（按名字近似判断——渲染出的名字稳定）。"""
    names = set()
    for c in deployment["spec"]["template"]["spec"]["containers"]:
        for src in c.get("envFrom", []) or []:
            for key in ("configMapRef", "secretRef"):
                ref = src.get(key) if isinstance(src, dict) else None
                if ref:
                    names.add(ref.get("name", ""))
    return names


def main() -> int:
    helm = _helm()

    # ---- 默认值（stream 关）----
    print("== 默认 values（stream.enabled=false）==")
    rc, out, err = render(helm)
    check(rc == 0, f"helm template 成功（rc={rc}）")
    if rc != 0:
        print(err, file=sys.stderr)
        return 1
    ds = docs(out)

    api = _by_kind(ds, "Deployment", "api")
    check(len(api) == 1, "有且仅有 1 个 API Deployment")
    check(not _by_kind(ds, "Deployment", "stream"),
          "stream 关闭时**不**渲染消费者 Deployment")

    migrate = _by_kind(ds, "Job", "migrate")
    check(len(migrate) == 1, "有且仅有 1 个迁移 Job")
    if migrate:
        ann = migrate[0]["metadata"].get("annotations", {})
        hook = ann.get("helm.sh/hook", "")
        check("pre-install" in hook and "pre-upgrade" in hook,
              "迁移 Job 是 pre-install+pre-upgrade hook（先迁移后发布）")
        cmd = migrate[0]["spec"]["template"]["spec"]["containers"][0]["command"]
        check("data_dog.storage.migrations" in " ".join(cmd),
              "迁移 Job 调用 python -m data_dog.storage.migrations")

    # API 不得自带流式消费（否则与独立消费者重复消费）
    if api:
        for c in api[0]["spec"]["template"]["spec"]["containers"]:
            names = _env_names(c)
            check("DD_STREAM_ENABLED" not in names,
                  "API 容器**不**直接注入 DD_STREAM_ENABLED")
            # 它引用的 ConfigMap 也不该把 STREAM 打开
        cm = _by_kind(ds, "ConfigMap")
        if cm:
            data = cm[0].get("data", {})
            check(data.get("DD_STREAM_ENABLED", "0") == "0",
                  "共享 ConfigMap 的 DD_STREAM_ENABLED=0（API 不误起消费者）")

    check(bool(_by_kind(ds, "PodDisruptionBudget")), "渲染了 PDB")

    # ---- 生产覆盖值（stream 开，kafka）----
    print("== values.prod.yaml（stream.enabled=true, kafka）==")
    rc, out, err = render(helm, ["-f", f"{CHART}/values.prod.yaml"])
    check(rc == 0, f"helm template 成功（rc={rc}）")
    if rc != 0:
        print(err, file=sys.stderr)
        return 1
    ds = docs(out)

    stream = _by_kind(ds, "Deployment", "stream")
    check(len(stream) == 1, "启用了 1 个消费者 Deployment")
    if stream:
        st = stream[0]["spec"]
        check(st.get("replicas") == 3, "消费者副本 = 3（kafka）")
        env = _env_names(st["template"]["spec"]["containers"][0])
        check({"DD_STREAM_ENABLED", "DD_STREAM_SOURCE", "DD_STREAM_SHARDS"} <= env,
              "消费者自带 DD_STREAM_* 环境变量（覆盖 envFrom）")
    hpa = _by_kind(ds, "HorizontalPodAutoscaler")
    check(len(hpa) == 1, "生产开了 HPA")
    api = _by_kind(ds, "Deployment", "api")
    if hpa and api:
        check("replicas" not in api[0]["spec"],
              "HPA 开启时 API Deployment **不**写死 replicas（避免两者打架）")

    # ---- 误配必须被拒（失败关闭）----
    print("== 失败关闭：file 源 + 多副本 ==")
    rc, out, err = render(helm, ["--set", "stream.enabled=true",
                                 "--set", "stream.source=file",
                                 "--set", "stream.replicas=3"])
    check(rc != 0, "file 源 + replicas=3 **被拒绝**（模板 fail，非静默渲染）")
    check("不支持" in (err + out) or "不支持" in err,
          "拒绝原因提到多副本不受支持")

    # ---- PrometheusRule（告警规则）----
    # 规则文件本身"引用的指标是否存在"由 tests/test_helm_alerts.py 钉（不需要 helm）；
    # 这里只钉**渲染行为**：默认不出、开启时恰好一个、且 stream 开关与 StreamDown 规则一致。
    print("== PrometheusRule（默认关，开启后生效）==")
    check(not _by_kind(ds, "PrometheusRule"),
          "默认 values **不**渲染 PrometheusRule（不是每个集群都装了 Operator）")

    rc, out, err = render(helm, ["--set", "prometheusRule.enabled=true"])
    check(rc == 0, f"prometheusRule.enabled=true 渲染成功（rc={rc}）")
    if rc == 0:
        rules = _by_kind(docs(out), "PrometheusRule")
        check(len(rules) == 1, "恰好 1 个 PrometheusRule")
        if rules:
            groups = rules[0].get("spec", {}).get("groups", [])
            alerts = {r.get("alert") for g in groups for r in g.get("rules", [])}
            # 两条"静默失败唯一可见窗口"必须真的在渲染结果里。
            must = {"DataDogAuditWriteFailed", "DataDogWriteBehindDataLoss",
                    "DataDogIngestLatencyP99High"}
            check(must <= alerts, f"关键告警齐全（缺：{sorted(must - alerts)}）")
            check("DataDogStreamDown" not in alerts,
                  "stream 关闭时**不**渲染 DataDogStreamDown（否则 API 的 stream_up=0 会永久误报）")

    rc, out, err = render(helm, ["-f", f"{CHART}/values.prod.yaml",
                                 "--set", "prometheusRule.enabled=true"])
    check(rc == 0, f"生产覆盖值 + 规则开启渲染成功（rc={rc}）")
    if rc == 0:
        dsp = docs(out)
        rules = _by_kind(dsp, "PrometheusRule")
        if rules:
            alerts = {r.get("alert")
                      for g in rules[0].get("spec", {}).get("groups", [])
                      for r in g.get("rules", [])}
            check("DataDogStreamDown" in alerts,
                  "stream 开启时渲染 DataDogStreamDown（消费者必须被看着）")

    print()
    if _FAILS:
        print(f"[verify_chart] {len(_FAILS)} 项不变量被破坏")
        return 1
    print("[verify_chart] 全部不变量通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
