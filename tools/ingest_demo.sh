#!/usr/bin/env bash
# 灌一条演示日志：96°C 过温，应产出 critical 候选（MOTOR_TEMP_PROTECT）。
#
# 为什么单独成文件：这段 curl 原本只写在 Makefile 的 `ingest` 目标里，
# 而 Windows 机器常常没有 make（本机实测 `make: command not found`），
# 提示语里写 `make ingest` 等于把人指进死胡同。
# 现在 Makefile 的 ingest 目标转调本脚本，两处不再是两份会漂移的副本。
#
#   bash tools/ingest_demo.sh          打默认端口/租户
#   PORT=8011 TENANT=xxx bash tools/ingest_demo.sh
#
# 租户必须和控制台的 DD_WEB_TENANT 一致，否则数据落库了但页面永远是空的——
# 看起来像坏了，其实是多租户隔离在正常工作（守卫见
# tests/test_demo_tenant_consistency.py）。
set -uo pipefail

PORT="${PORT:-8000}"
TENANT="${TENANT:-tenant-demo}"
DEVICE_SN="${DEVICE_SN:-device-demo-0001}"

curl -s -X POST "http://127.0.0.1:$PORT/v1/telemetry" \
  -H "Content-Type: application/json" \
  -H "X-Tenant-Id: $TENANT" \
  -H "X-Device-Sn: $DEVICE_SN" \
  -d '{"events":[{"event_id":"e1","occurred_at":"2026-09-13T10:00:00Z","received_at":"2026-09-13T10:00:00.1Z","sequence":1,"vendor":"unitree","model":"go2","firmware":"3.2.1","protocol_version":"motor-log-v2","component_hint":"joint_motor.front_left_hip","payload_inline":"{\"temperature\":96,\"current\":12.0,\"mode\":0}"}]}'
rc=$?
echo

if [ "$rc" -ne 0 ]; then
  echo "✗ 请求失败（exit=$rc）。后端起了吗？"
  echo "  自检：curl -s http://127.0.0.1:$PORT/healthz"
  exit "$rc"
fi

echo "→ 控台看这条：http://127.0.0.1:3000（租户 $TENANT）"
exit 0
