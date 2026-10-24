"""把 kubeconform 的 summary 行当断言读，而不是当日志读。

为什么：`-ignore-missing-schemas` 会让 kubeconform 把“找不到 schema”的资源记成
**Skipped** 并返回 0。2026-10-24 实测过：schema 文件名大小写不对时，
`Summary: 6 resources found - Valid: 0, Skipped: 6` —— 退出码 0，肉眼看像通过，
实际上一个资源都没校验。这种"绿"比红更危险。

判据（两条同时成立才通过）：
1. `Errors: 0` 且 `Invalid: 0`；
2. `Skipped` 恰好等于**预期的 CRD 数**（这些 CRD 上游无 schema，由 verify_chart.py 覆盖）。

用法：
    helm template dd helm/data-dog | kubeconform ... | python scripts/assert_kc_summary.py 0
    helm template dd helm/data-dog -f .../values.prod.yaml | kubeconform ... | python scripts/assert_kc_summary.py 2
"""

from __future__ import annotations

import re
import sys


def parse_summary(text: str) -> dict[str, int]:
    m = re.search(r"Summary:\s*(?P<res>\d+)\s+resources.*?Valid:\s*(?P<valid>\d+),\s*"
                  r"Invalid:\s*(?P<invalid>\d+),\s*Errors:\s*(?P<errors>\d+),\s*"
                  r"Skipped:\s*(?P<skipped>\d+)", text, re.S)
    if not m:
        raise SystemExit("找不到 kubeconform 的 Summary 行——输出格式变了或被截断，不能当作通过")
    return {k: int(v) for k, v in m.groupdict().items()}


def main(argv: list[str]) -> int:
    expect_skipped = int(argv[1]) if len(argv) > 1 else 0
    s = parse_summary(sys.stdin.read())
    bad = []
    if s["invalid"] or s["errors"]:
        bad.append(f"Invalid={s['invalid']} Errors={s['errors']}（有资源真的不合法）")
    if s["skipped"] != expect_skipped:
        bad.append(f"Skipped={s['skipped']} 但预期 {expect_skipped}：说明有资源**没被校验**"
                   "（schema 路径/命名不匹配时 kubeconform 仍然返回 0）")
    if s["valid"] + s["skipped"] != s["res"]:
        bad.append(f"Valid+Skipped={s['valid'] + s['skipped']} != 资源数 {s['res']}（数字不自洽）")
    if bad:
        print("[FAIL] kubeconform summary 不合格：" + "；".join(bad))
        return 1
    print(f"[OK]   kubeconform：{s['res']} 个资源，{s['valid']} 个校验通过，"
          f"{s['skipped']} 个 CRD 跳过（上游无 schema）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
