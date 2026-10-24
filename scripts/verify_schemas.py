"""校验「渲染出的每个 kind 都有本地 JSON schema」。

为什么需要：`kubeconform` 默认从 raw.githubusercontent.com 拉 schema，
它的 `-ignore-missing-schemas` 只容忍 404，**不容忍连接失败**。
2026-10-24 实测：离线时它报 `failed downloading schema ... EOF` → `Errors: 6` →
helm 腿变红，而 chart 本身完全正确。于是 schema 被收进仓库 `helm/schemas/`（可版本化、可评审）。

本脚本是配套的**漂移守卫**：一旦 chart 新增一个类型（比如加了 HPA / Ingress），
这里立刻报出缺哪个文件，而不是等到某个离线环境上才变成假红。

用法：
    helm template dd helm/data-dog | python scripts/verify_schemas.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "helm" / "schemas"

# 有意跳过的 kind：CRD（Prometheus Operator 的）在 kubernetes-json-schema 上游**不存在**，
# 而 kubeconform 的 `-skip` 列表必须与本集合一致，否则同一次运行会两种口径。
# 它们的字段正确性由 `scripts/verify_chart.py` 的不变量断言覆盖（键名、数量、开关行为）。
SKIP_KINDS = {"PrometheusRule", "ServiceMonitor"}


def schema_name(kind: str, api_version: str) -> str:
    """kubeconform 的 `{{.ResourceKind}}{{.KindSuffix}}.json` 展开结果。

    KindSuffix 规则：`apps/v1` → `-apps-v1`；核心组 `v1` → `-v1`。
    """
    group, _, version = api_version.rpartition("/")
    name = f"{kind}-{group}-{version}" if group else f"{kind}-{version}"
    # kubeconform 解析 `{{.ResourceKind}}{{.KindSuffix}}` 后**会转小写**再读文件
    # （实测它要的是 serviceaccount-v1.json，给 ServiceAccount-v1.json 会“跳过”且退出码 0）。
    return name.lower() + ".json"


def missing_schemas(docs: list[dict]) -> list[str]:
    seen: dict[str, int] = {}
    for doc in docs:
        if not isinstance(doc, dict) or "kind" not in doc:
            continue
        name = schema_name(str(doc["kind"]), str(doc.get("apiVersion", "v1")))
        seen[name] = seen.get(name, 0) + 1
    return sorted(n for n in seen if not (SCHEMA_DIR / n).exists()
                  # n 已小写（对齐 kubeconform），SKIP_KINDS 是原始 Kind 名，比较前统一小写
                  and n.split("-")[0] not in {k.lower() for k in SKIP_KINDS})


def main() -> int:
    docs = [d for d in yaml.safe_load_all(sys.stdin.read()) if d]
    missing = missing_schemas(docs)
    if missing:
        print(f"[FAIL] helm/schemas 缺 schema（联网才能校验，离线必假红）：{missing}")
        print("       补法：curl -o helm/schemas/<Kind>-<group>-<ver>.json "
              "https://raw.githubusercontent.com/yannh/kubernetes-json-schema/"
              "master/master-standalone-strict/<kind>-<group>-<ver>.json")
        return 1
    print(f"[OK]   渲染出的 {len({d.get('kind') for d in docs if isinstance(d, dict)})} 种 kind 都有本地 schema"
          f"（CRD 显式跳过：{sorted(SKIP_KINDS)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
