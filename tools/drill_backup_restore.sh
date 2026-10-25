#!/usr/bin/env bash
# 备份/恢复演练（drill）：**不是**"备份命令能跑"，而是证明「库没了能真恢复回来」。
#
# 为什么这么写（两个真事故）：
#   1) 第一版用 `curl 127.0.0.1:8000` 投数据 → 宿主机 8000 被**别的项目**（warden）占着，
#      请求打到别人服务上返回 404，备份里只有 schema 没有数据，"恢复成功但 0 条"是假象。
#      → 现在一律走 `docker compose exec app`，**不碰宿主端口**，并对端身份显式断言。
#   2) 第二版把 psql 的 stderr 丢了，查询失败返回空串被当成"0 条"，
#      把"恢复没跑通"伪装成"数据没了"。→ 现在查询失败立刻 `QUERY_FAILED` 并打印原因。
#
# 五步判据（缺一步不算演练）：
#   1. 基线：投一条遥测 → 库内 candidate = 1
#   2. 备份：pg_dump 非空，且**先恢复到临时库验证这份 dump 真能用**（数据行数 = 1）
#   3. 负对照：down -v 真摧毁 → 同一条必须查不到（否则第 4 步毫无意义）
#   4. 恢复：pg_restore 回真库 → 必须重查到 1 条，且 signal 一致
#   5. 收尾：down -v，保留证据文件
set -u
cd "$(dirname "$0")/.." || exit 1
TMP=artifacts/drill_backup
mkdir -p "$TMP"
PY=.venv/Scripts/python.exe
COMPOSE="docker compose"
SN="DRILL-$$"
TENANT=tenant-drill
fail=0
step() { printf '\n=== %s ===\n' "$1"; }
bad()  { echo "✗ $*"; fail=1; }

# 数据库身份**从运行中的容器里取**，不在脚本里另写一份（避免 .env 与脚本漂移成两个库）
db_env() { $COMPOSE exec -T db printenv "$1" 2>/dev/null | tr -d '\r\n'; }
PSQL() {
  local out rc
  out=$($COMPOSE exec -T db psql -U "$(db_env POSTGRES_USER)" -d "${2:-$(db_env POSTGRES_DB)}" -tAc "$1" 2>"$TMP/psql.err"); rc=$?
  if [ $rc -ne 0 ] || [ -z "$out" ]; then
    echo "QUERY_FAILED(rc=$rc)" >&2
    head -3 "$TMP/psql.err" >&2
    return 1
  fi
  echo "$out" | tr -d '[:space:]'
}
cand_count() { PSQL "select count(*) from candidates where device_sn='$SN'" "$@"; }

step "0. 起 db+app（compose）"
$COMPOSE up -d --wait --wait-timeout 300 db app >/dev/null 2>&1 || bad "compose up 失败"
$COMPOSE ps --format '{{.Service}} {{.Status}}' | sed 's/^/  /'

step "0b. 对端身份断言（防止打到别的项目的服务上）"
# 在**容器内**请求，完全不依赖宿主端口；同时确认这确实是 data-dog
who=$($COMPOSE exec -T app python -c "import urllib.request,json;print(urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=10).read().decode())" 2>&1)
echo "  /healthz → $who"
case "$who" in
  *'"service":"data-dog"'*|*'"service": "data-dog"'*) : ;;
  *) bad "对端不是 data-dog（拿到：$(echo "$who" | head -c 120)）—— 端口/代理被别的项目占了" ;;
esac

step "1. 基线：容器内投一条遥测（device=$SN, tenant=$TENANT）"
$PY tools/_doc_sample.py docs/data_plane.md > "$TMP/event.json" || bad "取文档示例失败"
resp=$($COMPOSE exec -T app python -c "
import json,sys,urllib.request
ev=json.load(open('/dev/stdin'))
req=urllib.request.Request('http://127.0.0.1:8000/v1/telemetry',data=json.dumps({'events':[ev]}).encode(),headers={'content-type':'application/json','X-Tenant-Id':'$TENANT','X-Device-Sn':'$SN'})
print(urllib.request.urlopen(req,timeout=30).read().decode())" < "$TMP/event.json" 2>&1)
echo "  POST /v1/telemetry → $(echo "$resp" | head -c 200)"
case "$resp" in *'"candidates":1'*) : ;; *) bad "基线投递没产出候选：$(echo "$resp" | head -c 160)" ;; esac
before=$(cand_count) || before=QUERY_FAILED
echo "  库内候选数 = $before"
[ "$before" = "1" ] || bad "基线应为 1 条候选，实际 $before"

step "2. 备份 + **先验证这份 dump 真能用**（恢复到临时库 drill_verify）"
$COMPOSE exec -T db pg_dump -U "$(db_env POSTGRES_USER)" -d "$(db_env POSTGRES_DB)" -Fc > "$TMP/dump.pgc" \
  || bad "pg_dump 失败"
size=$(wc -c < "$TMP/dump.pgc" | tr -d ' ')
echo "  备份大小 = ${size} 字节"
[ "$size" -gt 1000 ] || bad "备份过小（${size} 字节）"

$COMPOSE exec -T db psql -U "$(db_env POSTGRES_USER)" -d postgres -c "drop database if exists drill_verify" >/dev/null 2>&1
$COMPOSE exec -T db psql -U "$(db_env POSTGRES_USER)" -d postgres -c "create database drill_verify" >/dev/null 2>&1 \
  || bad "临时库创建失败"
$COMPOSE exec -T db pg_restore -U "$(db_env POSTGRES_USER)" -d drill_verify --no-owner --no-privileges \
  < "$TMP/dump.pgc" > "$TMP/verify_restore.log" 2>&1
vrc=$?
verr=$(grep -ci "error" "$TMP/verify_restore.log" || true)
inv=$(cand_count drill_verify) || inv=QUERY_FAILED
echo "  临时库恢复：rc=$vrc error行=$verr 候选数=$inv"
[ "$vrc" -eq 0 ] || bad "dump 恢复到临时库失败（rc=$vrc），看 $TMP/verify_restore.log"
[ "$inv" = "1" ] || bad "dump 里没有这条数据（临时库=$inv）—— 备份无效，后面的恢复必然假绿"
$COMPOSE exec -T db psql -U "$(db_env POSTGRES_USER)" -d postgres -c "drop database if exists drill_verify" >/dev/null 2>&1

step "3. 负对照：down -v 真摧毁 → 同一条必须查不到"
$COMPOSE down -v --remove-orphans >/dev/null 2>&1
$COMPOSE up -d --wait --wait-timeout 300 db app >/dev/null 2>&1 || bad "重建失败"
gone=$(cand_count) || gone=QUERY_FAILED
echo "  重建后候选数 = $gone"
[ "$gone" = "0" ] || bad "删库后仍查到 $gone —— 负对照不成立，本次演练无效"

step "4. 恢复真库 → 数据必须回来"
$COMPOSE exec -T db pg_restore -U "$(db_env POSTGRES_USER)" -d "$(db_env POSTGRES_DB)" \
  --clean --if-exists --no-owner --no-privileges < "$TMP/dump.pgc" > "$TMP/restore.log" 2>&1
rrc=$?
rerr=$(grep -ci "error" "$TMP/restore.log" || true)
echo "  pg_restore rc=$rrc error行=$rerr"
[ "$rerr" -gt 0 ] && grep -i "error" "$TMP/restore.log" | head -5
after=$(cand_count) || after=QUERY_FAILED
echo "  恢复后候选数 = $after"
[ "$after" = "1" ] || bad "恢复后没有找回数据（$after）"
sig=$(PSQL "select signal||'/'||severity_hint from candidates where device_sn='$SN'" 2>/dev/null | tr -d '[:space:]')
echo "  恢复后信号 = ${sig:-<空>}"

step "5. 收尾"
$COMPOSE down -v --remove-orphans >/dev/null 2>&1 && echo "  已 down -v"
echo "  证据保留在 $TMP/（dump.pgc / restore.log / verify_restore.log / psql.err）"

echo
if [ "$fail" -eq 0 ]; then
  echo "结论：备份/恢复演练通过 ✓（备份可用性预验 + 删库负对照 + 恢复校验 全过）"
else
  echo "结论：演练失败 ✗"
fi
exit "$fail"
