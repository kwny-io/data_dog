#!/usr/bin/env bash
# ============================================================================
# Windows 原生侧的等价验证（与 scripts/verify_ci_offline.sh 同口径、同产物格式）
#
# 为什么要两边都跑：容器证明“Linux 上绿”，原生证明“开发者的机器上绿”——
# 这两件事不是同一件事（#103 的 mypy 平台条件属性就是：Windows 绿、Linux 红）。
# 两边都跑，才能说“这个提交在两种平台上都验过”。
#
# 用法：bash scripts/verify_ci_native_windows.sh [leg ...]   默认全部
# 产物：$OUT/<leg>.log  rc.tsv  manifest.txt  SUMMARY.md（$OUT 默认 artifacts/ci-offline-native/<时间戳>/）
# ============================================================================
set -uo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$REPO" || exit 9
OUT=${OUT:-$REPO/artifacts/ci-offline-native/$(date +%Y%m%d-%H%M%S)}
export PATH="$HOME/bin:$PATH"          # helm.exe / kubeconform.exe 装在这里
export PYTHONIOENCODING=utf-8 PYTHONUTF8=1
PY=.venv/Scripts/python.exe
LEGS=${*:-lint test helm web web-e2e}
RC_FILE="$OUT/rc.tsv"
mkdir -p "$OUT"; : > "$RC_FILE"
log() { printf '%s\n' "$*" >&2; }

run_native() {
  local leg=$1 script=$2 workdir=${3:-$REPO}
  local logf="$OUT/$leg.log" start end rc
  start=$(date +%s)
  log ">>> [$leg] native"
  { echo "# leg=$leg host=windows-native workdir=$workdir $(date -Is)"; echo; } > "$logf"
  ( cd "$workdir" && bash -lc "$script" ) >>"$logf" 2>&1
  rc=$?
  end=$(date +%s)
  printf '%s\t%s\t%s\n' "$leg" "$rc" "$((end - start))" >> "$RC_FILE"
  if [ "$rc" -eq 0 ]; then log "<<< [$leg] OK ($((end - start))s)"; else log "<<< [$leg] FAIL rc=$rc ($((end - start))s)"; fi
}

for leg in $LEGS; do
  case "$leg" in
    lint) run_native lint "
      set -e
      echo '=== ruff check .';                    $PY -m ruff check .
      echo '=== mypy src（Windows 平台）';          $PY -m mypy src
      echo '=== mypy --platform linux src（CI 口径）'; $PY -m mypy --platform linux src
      echo '=== 依赖清单漂移';                       $PY scripts/gen_requirements.py --check
      echo '=== README 数字漂移';                    $PY scripts/readme_counts.py --check
      echo '=== 元数据一致性';                       $PY -m pytest -q tests/test_project_meta.py
    " ;;
    test) run_native test "
      set -e
      $PY -m pytest -q
    " ;;
    helm) run_native helm "
      set -e
      echo '=== helm lint'; helm lint helm/data-dog
      echo '=== 不变量'; $PY scripts/verify_chart.py
      # 与容器腿/CI 同口径：schema 用仓库内 helm/schemas/，-skip 两个 CRD，并断言 summary。
      echo '=== kubeconform 默认'
      helm template dd helm/data-dog | kubeconform -strict -summary -ignore-missing-schemas -skip PrometheusRule,ServiceMonitor -schema-location 'helm/schemas/{{.ResourceKind}}{{.KindSuffix}}.json' | tee /tmp/kc-default.txt
      $PY scripts/assert_kc_summary.py 0 < /tmp/kc-default.txt
      echo '=== kubeconform 生产'
      helm template dd helm/data-dog -f helm/data-dog/values.prod.yaml | kubeconform -strict -summary -ignore-missing-schemas -skip PrometheusRule,ServiceMonitor -schema-location 'helm/schemas/{{.ResourceKind}}{{.KindSuffix}}.json' | tee /tmp/kc-prod.txt
      $PY scripts/assert_kc_summary.py 2 < /tmp/kc-prod.txt
    " ;;
    web) run_native web "
      set -e
      echo '=== typecheck'; npm run typecheck
      echo '=== lint'; npm run lint
      echo '=== test'; npm test
    " "$REPO/web" ;;
    web-e2e) run_native web-e2e "
      set -e
      E2E_REAL_BACKEND=1 E2E_REAL_PYTHON=$REPO/.venv/Scripts/python.exe npm run e2e
    " "$REPO/web" ;;
    *) log "!! 未支持的原生腿：$leg"; printf '%s\t9\t0\n' "$leg" >> "$RC_FILE" ;;
  esac
done

{
  echo "# data_dog CI 等价验证快照（Windows 原生）"
  echo "生成时间: $(date -Is)"
  echo "主机: $(uname -srm)"
  echo "commit: $(git rev-parse HEAD)"
  echo "branch: $(git rev-parse --abbrev-ref HEAD)"
  echo "工作树改动: $(git status --porcelain | wc -l)"
  echo
  echo "## 工具版本"
  $PY -V
  $PY -m ruff --version
  $PY -m mypy --version
  helm version --short 2>/dev/null
  kubeconform -v 2>/dev/null
  node -v; npm -v
} > "$OUT/manifest.txt" 2>&1

{
  echo "# Windows 原生侧结论（$(date -Is)）"
  echo
  echo "commit \`$(git rev-parse --short HEAD)\`"
  echo
  echo "| 腿 | 退出码 | 耗时 | 结论 |"
  echo "|---|---|---|---|"
  fails=0
  while IFS=$'\t' read -r leg rc secs; do
    [ -z "${leg:-}" ] && continue
    if [ "$rc" = "0" ]; then mark="✅ 通过"; else mark="❌ 红（见 $leg.log）"; fails=$((fails + 1)); fi
    echo "| $leg | $rc | ${secs}s | $mark |"
  done < "$RC_FILE"
  echo
  [ "$fails" -eq 0 ] && echo "**全部 $(wc -l < "$RC_FILE") 条腿通过。**" || echo "**$fails 条腿红。**"
} > "$OUT/SUMMARY.md"

log ""; log "产物目录：$OUT"; cat "$OUT/SUMMARY.md" >&2
fails=$(awk -F'\t' '$2 != 0' "$RC_FILE" | wc -l)
[ "$fails" -eq 0 ] || exit 1
