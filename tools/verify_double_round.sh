#!/usr/bin/env bash
# 终版证据：同一 commit 的 9 腿双轮（root + 非 root 65534）。
# 为什么要跑两轮：root 会绕过 RLIMIT_NPROC，许多并发/进程限制类问题在 root 下不出现，
# 只跑 root 等于放过 CI（非 root/沙箱 runner）会遇到的失败模式。
# 用法： bash tools/verify_double_round.sh [commit说明]
set -u
cd "$(git rev-parse --show-toplevel)" || exit 1

if [ -n "$(git status --porcelain | grep -v '^?? artifacts/')" ]; then
  echo "工作区不干净——双轮证据会打到\"当前工作区\"而不是某个提交，先提交再跑："
  git status --short | head -10
  exit 2
fi

echo "=== 被验证的提交：$(git rev-parse --short HEAD) ==="
echo "=== 第 1 轮：root ==="
bash scripts/verify_ci_offline.sh all || { echo "ROOT 轮失败"; exit 1; }
echo "=== 第 2 轮：非 root（uid 65534） ==="
DD_CI_RUN_USER=65534 bash scripts/verify_ci_offline.sh all || { echo "NONROOT 轮失败"; exit 1; }
echo "=== 双轮均通过：$(git rev-parse --short HEAD) ==="
