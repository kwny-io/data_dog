#!/usr/bin/env bash
# 端到端验证 compose 的 app + web（profile web）。
#
# 判据不是"镜像能构建""容器能起"，而是四条**浏览器路径上的 HTTP 事实**：
#   1. 首页 200（且不是空的骨架）
#   2. 同源代理 /api/dd/healthz 200（证明 web→app 服务端代理通，密钥不下发浏览器）
#   3. POST /api/dd/v1/telemetry 200 且 ingested>0（正文是 {"events":[...]}，身份走 X-Device-Sn 头）
#   4. 用**本次投递的 device_sn** 过滤候选，必须真的查到它（否则"页面能开、数据全空"）
#
# 2026-10-25 教训（第一版脚本假绿）：临时文件写在 Git-Bash 的 /tmp，Windows 的 python
# 解析 /tmp 成 C:\tmp → 断言段直接崩，而失败没被传播，脚本照样打印"端到端可用 ✓"。
# 现在：临时目录由脚本显式创建并传同一路径给 python；任何一步非 200 或断言失败都置 fail。
set -u
cd /d/data_dog || exit 1
fail=0
TMP=artifacts/verify_compose
mkdir -p "$TMP"
PY=.venv/Scripts/python.exe
TENANT=${DD_TENANT:-tenant-demo}
SN="E2E-COMPOSE-$$"

step() { printf '\n=== %s ===\n' "$1"; }
bad()  { echo "✗ $*"; fail=1; }

step "1. 拉起 app + web（等 healthy）"
if docker compose --profile web up -d --wait --wait-timeout 300; then
  docker compose ps --format '{{.Service}}\t{{.Status}}'
else
  bad "compose up 失败"
fi

step "2. 浏览器路径：控制台首页"
code=$(curl -s -o "$TMP/index.html" -w '%{http_code}' --max-time 20 http://127.0.0.1:3000/ || echo 000)
bytes=$(wc -c < "$TMP/index.html" | tr -d ' ')
echo "GET / → $code（$bytes 字节）"
[ "$code" = "200" ] || bad "首页非 200"
[ "$bytes" -gt 2000 ] || bad "首页过小（$bytes 字节），疑似空白骨架"

step "3. 同源代理：浏览器 → /api/dd → app"
code=$(curl -s -o "$TMP/healthz.json" -w '%{http_code}' --max-time 20 \
  http://127.0.0.1:3000/api/dd/healthz || echo 000)
echo "GET /api/dd/healthz → $code $(head -c 160 "$TMP/healthz.json")"
[ "$code" = "200" ] || bad "同源代理不通（浏览器拿不到任何数据）"

step "4. 真数据路径：投一条遥测（device=$SN）"
# 事件正文直接取**文档里被守卫的那份示例**（docs/data_plane.md §3.4）——
# 单一真源：文档改坏了，tests/test_doc_ingest_example.py 先红；这里也不会再“猜字段”。
if ! "$PY" tools/_doc_sample.py docs/data_plane.md > "$TMP/event.json"; then
  bad "从文档取示例失败（种子坏了）"
fi
code=$(curl -s -o "$TMP/ingest.json" -w '%{http_code}' --max-time 30 \
  -X POST "http://127.0.0.1:3000/api/dd/v1/telemetry" \
  -H 'content-type: application/json' \
  -H "X-Tenant-Id: $TENANT" -H "X-Device-Sn: $SN" \
  -d "{\"events\":[$(cat "$TMP/event.json")]}" || echo 000)
echo "POST /api/dd/v1/telemetry → $code $(head -c 240 "$TMP/ingest.json")"
[ "$code" = "200" ] || bad "投递失败（浏览器侧根本发不进去）"

step "5. 决定性断言：用本次 device_sn 过滤，必须查到它"
code=$(curl -s -o "$TMP/cand.json" -w '%{http_code}' --max-time 20 \
  "http://127.0.0.1:3000/api/dd/v1/candidates?device_sn=$SN&limit=5" \
  -H "X-Tenant-Id: $TENANT" || echo 000)
echo "GET 候选?device_sn=$SN → $code"
[ "$code" = "200" ] || bad "候选查询失败"
if ! "$PY" tools/_assert_candidate.py "$TMP/cand.json" "$SN"; then
  bad "断言未通过：候选里没有本次投递的设备（典型症状：页面能开、数据全空）"
fi

step "6. 收尾"
docker compose --profile web down -v --remove-orphans >/dev/null 2>&1 && echo "已 down -v（含卷）"
rm -rf "$TMP"

if [ "$fail" -eq 0 ]; then
  echo
  echo "结论：compose app+web 端到端可用 ✓（首页/代理/投递/查询 四条实证）"
else
  echo
  echo "结论：有失败项 ✗"
fi
exit "$fail"
