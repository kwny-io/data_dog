#!/usr/bin/env bash
# 唯一的推送入口：推当前分支 → 快进 main → 双向读回核实。
#
# 为什么需要这个脚本（真实事故）：我手工拼过
#   bash tools/push_retry.sh && bash tools/push_main.sh; git rev-parse --short HEAD origin/main
# 前两步都成功、服务器侧 main 已核实为那个 sha，但最后那条装饰性的 rev-parse 报了
# fatal: Needed a single revision，整条命令 exit=128 —— 后台任务于是发来一条
# "推送失败"的通知。**推送是成功的，失败通知是假的。**
# 假失败通知会让人重推一次或误判 CI 没跑；所以入口只留一个，且它只在
# "服务器侧读回 sha 与本地一致"时才打印成功哨兵。
#
# 用法：
#   bash tools/push_all.sh              # 用当前分支
#   bash tools/push_all.sh <branch>
#
# 输出契约（机器可读）：
#   PUSH_ALL=OK sha=<sha> branch=<branch> main=synced         退出码 0
#   PUSH_ALL=FAIL reason=<...>                                退出码非 0
set -uo pipefail

cd "$(git rev-parse --show-toplevel)" || exit 1
HERE="$(cd "$(dirname "$0")" && pwd)"

BRANCH="${1:-$(git rev-parse --abbrev-ref HEAD)}"

if ! bash "$HERE/push_retry.sh" "$BRANCH"; then
  echo "PUSH_ALL=FAIL reason=push"
  exit 1
fi
if ! bash "$HERE/push_main.sh" "$BRANCH"; then
  echo "PUSH_ALL=FAIL reason=main_sync"
  exit 1
fi

LOCAL_SHA="$(git rev-parse "$BRANCH")"
REMOTE_SHA="$(git ls-remote origin "refs/heads/$BRANCH" 2>/dev/null | cut -f1)"
MAIN_SHA="$(git ls-remote origin refs/heads/main 2>/dev/null | cut -f1)"

if [ "$LOCAL_SHA" != "$REMOTE_SHA" ]; then
  echo "PUSH_ALL=FAIL reason=remote_branch_mismatch local=$LOCAL_SHA remote=${REMOTE_SHA:-none}"
  exit 1
fi
if [ "$LOCAL_SHA" != "$MAIN_SHA" ]; then
  echo "PUSH_ALL=OK sha=$LOCAL_SHA branch=$BRANCH main=diverged($MAIN_SHA)"
  echo "（分支已推，但 main 不指向同一提交 —— 有意为之则正常，否则查 push_main.sh 日志）"
  exit 0
fi

echo "PUSH_ALL=OK sha=$LOCAL_SHA branch=$BRANCH main=synced"
