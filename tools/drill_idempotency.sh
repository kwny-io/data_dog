#!/usr/bin/env bash
# 目的：证明 `make drill-restore` **可重复执行**（第二遍不能因为第一遍留下的状态而假绿或假红）。
#
# 判定只认**退出码**，不 grep 字符串——今晚已经因为 grep 中文/emoji 判据错了两回
# （一次把崩溃的取证路当成"未复现"，一次把通过当成失败）。
set -u
cd /d/data_dog || exit 1
BASH_BIN="C:/Program Files/Git/bin/bash.exe"
mkdir -p artifacts/drill_backup
rc=0
for i in 1 2; do
  log="artifacts/drill_backup/idempotency_$i.log"
  if "$BASH_BIN" tools/drill_backup_restore.sh >"$log" 2>&1; then
    echo "第 $i 遍：退出码 0（通过），日志 $log"
  else
    echo "第 $i 遍：退出码 $?（失败），日志 $log；最后 12 行："
    tail -12 "$log"
    rc=1
  fi
done
echo "---"
if [ "$rc" = "0" ]; then
  echo "两遍均退出码 0 → 演练可重复执行"
else
  echo "至少一遍失败 → 演练不可重复执行"
fi
exit "$rc"
