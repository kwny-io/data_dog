#!/usr/bin/env bash
# 4 路并行取证：把 0.2% 的样本量拉到有统计意义的量级。
#
# 为什么需要它：单进程 240 轮只覆盖 0.5 个期望事件（0.2% × 240），
# "跑了没撞上"不能作为任何结论。4×375 = 1500 轮 ≈ 3 个期望事件，
# 若仍 0 命中，才能说"在该样本量下未复现"，而不是"没复现过"。
set -u
cd /d/data_dog || exit 1
R="${HUNT_ROUNDS:-375}"
pids=()
for s in 1 2 3 4; do
  PYTHONHASHSEED=$((s * 7)) HUNT_ROUNDS=$R \
    .venv/Scripts/python.exe tools/hunt_run_log_hang.py >"/tmp/hunt_$s.log" 2>&1 &
  pids+=($!)
done
echo "已启动 4 路 × $R 轮（期望命中 ≈ $((R * 4 * 2 / 1000)) 次，按 0.2% 估）"
for p in "${pids[@]}"; do wait "$p"; done
echo "=== 汇总 ==="
hit=0
for s in 1 2 3 4; do
  line=$(grep -E "全部通过|卡死|reproduced" "/tmp/hunt_$s.log" | tail -1)
  echo "路 $s: ${line:-（无结论行）}"
  if grep -q "卡死" "/tmp/hunt_$s.log"; then hit=1; fi
done
if [ "$hit" = "1" ]; then
  echo ">>> 命中卡死，dumb 栈见 /tmp/hunt_*.log"
  exit 1
fi
echo ">>> 1500 轮量级下未复现"
