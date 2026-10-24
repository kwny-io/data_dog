# helm/schemas

`kubeconform` 用的 JSON Schema，**随仓库版本化**，让 chart 校验离线可复现。

## 为什么放在仓库里

`kubeconform` 默认从 `raw.githubusercontent.com` 拉 schema。它的 `-ignore-missing-schemas`
只容忍 404，**不容忍连接失败**：没有网络时会给 `failed downloading schema ... EOF`
→ `Errors: 6` → helm 腿变红，而 chart 本身完全正确。这类"环境决定结论"的红，
是验证台最不该有的东西（详见 `docs/ci-offline.md`）。

## 命名规则（有坑）

文件名必须**全小写**：`kubeconform` 解析 `{{.ResourceKind}}{{.KindSuffix}}` 后会转小写再读文件。
写成 `ServiceAccount-v1.json` 会导致它**跳过**该资源且退出码仍为 0 —— 零校验的"绿"。
`scripts/assert_kc_summary.py` 会把 `Skipped` 数当断言读，专门挡这个。

## 上游与许可

来源：<https://github.com/yannh/kubernetes-json-schema>（`master-standalone-strict`），
许可 Apache-2.0，与 Kubernetes 上游 schema（同样 Apache-2.0）一致。
仅作离线校验输入，未修改内容。

两个 CRD（`PrometheusRule`、`ServiceMonitor`）上游没有 schema，用 `-skip` 显式排除，
其正确性由 `scripts/verify_chart.py` 的不变量断言覆盖（键名、数量、开关行为）。

## 更新方式

```bash
cd helm/schemas
curl -fsSLO "https://raw.githubusercontent.com/yannh/kubernetes-json-schema/master/master-standalone-strict/<kind>-<group>-<ver>.json"
python ../../scripts/verify_schemas.py < <(helm template dd ../data-dog)
```
