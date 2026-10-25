#!/usr/bin/env bash
# 4 路并行取证：把 0.2% 的样本量拉到有统计意义的量级。
#
# 为什么需要它：单进程 240 轮只覆盖 0.5 个期望事件（0.2% × 240），
# "跑了没撞上"不能作为任何结论。4×375 = 1500 轮 ≈ 3 个期望事件，
# 若仍 0 命中，才能说"在该样本量下未复现"，而不是"没复现过"。
set -u
cd /d/data_dog || exit 1
R="${HUNT_ROUNDS:-375}"
ROUTES="${HUNT_ROUTES:-2}"   # 默认 2 路：4 路并行在满负载机器上会 MemoryError，反而拖垮取证
pids=()
for s in $(seq 1 "$ROUTES"); do
  PYTHONHASHSEED=$((s * 7)) HUNT_ROUNDS=$R \
    .venv/Scripts/python.exe tools/hunt_run_log_hang.py >"/tmp/hunt_$s.log" 2>&1 &
  pids+=($!)
done
echo "已启动 $ROUTES 路 × $R 轮（期望命中 ≈ $((R * ROUTES * 2 / 1000)) 次，按 0.2% 估）"
for p in "${pids[@]}"; do wait "$p"; done
echo "=== 汇总 ==="
clean=0
hang=0
broken=0
for s in $(seq 1 "$ROUTES"); do
  log="/tmp/hunt_$s.log"
  if grep -q "HUNT_HANG" "$log" 2>/dev/null; then
    echo "路 $s: 命中卡死"
    hang=$((hang + 1))
  elif grep -q "HUNT_ALL_CLEAN done=${R}" "$log" 2>/dev/null; then
    echo "路 $s: $R 轮干净"
    clean=$((clean + 1))
  else
    # 关键 1：**没跑完不算通过**（否则崩掉的路会被当成“未复现”，等于给出假阴性证据）
    # 关键 2：**只认 ASCII 哨兵**——之前 grep 中文在 GBK 日志上恒不命中，
    # 把“跑完 375 轮干净”误报成“无结论行”，同一份日志被读成了相反的结论。
    echo "路 $s: 未跑完（见 $log）"
    grep -oE "HUNT_PROGRESS i=[0-9]+" "$log" 2>/dev/null | tail -1
    grep -aE "MemoryError|Error" "$log" 2>/dev/null | head -2
    broken=$((broken + 1))
  fi
done
if [ "$hang" -gt 0 ]; then
  echo ">>> 命中卡死 $hang 路，dumb 栈见 /tmp/hunt_*.log"
  exit 1
fi
if [ "$broken" -gt 0 ]; then
  echo ">>> 不结论：$clean 路干净 / $broken 路崩掉，有效样本只有 $((clean * R)) 轮"
  exit 2
fi
echo ">>> $((clean * R)) 轮全部干净，未复现卡死（样本量按 0.2% 估可期 ~$((clean * R * 2 / 1000)) 次）"
