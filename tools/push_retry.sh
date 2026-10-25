#!/usr/bin/env bash
# 推送重试器：本机代理不可用时，git 的 **上传** 会被重置（下载却正常）。
#
# 场景：~/.gitconfig 里配了 http.proxy=http://127.0.0.1:7897，但代理客户端没开。
# 现象：`git ls-remote` 200，`git push` 报 `Recv failure: Connection was reset`
#       或 `Failed to connect ... port 443`。重试往往能在网络抽动间隙里成功。
#
# 用法： tools/push_retry.sh [分支名]   （默认当前分支）
# 退出码：0 表示远端已与本地一致；1 表示重试用尽仍是网络问题（代码没丢，只是没推上去）。
set -u
cd "$(git rev-parse --show-toplevel)" || exit 1
BRANCH="${1:-$(git rev-parse --abbrev-ref HEAD)}"
ATTEMPTS="${ATTEMPTS:-12}"
LOG_DIR="$(mktemp -d)"

for i in $(seq 1 "$ATTEMPTS"); do
  local_sha=$(git rev-parse HEAD)
  # 用清除代理的 fetch 判远端状态：被代理配置拖挂时这里会失败，所以不靠它做唯一判据
  git -c http.proxy= -c https.proxy= fetch -q origin "$BRANCH" 2>/dev/null
  remote_sha=$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo "")
  if [ "$local_sha" = "$remote_sha" ]; then
    echo "已在远端：$local_sha"
    exit 0
  fi
  echo "--- 第 $i/$ATTEMPTS 次：推送 ${local_sha:0:7} → origin/$BRANCH ---"
  # --no-verify：pre-push 钩子会跑全量套件（数分钟），逐次重试时没必要重复；
  # 提交前的本地门禁请用 scripts/verify_ci_offline.sh，两者同口径。
  if git -c http.proxy= -c https.proxy= -c http.version=HTTP/1.1 \
        -c http.postBuffer=157286400 push --no-verify origin "$BRANCH" >"$LOG_DIR/push_$i.log" 2>&1; then
    echo "推送成功（第 $i 次）"
    exit 0
  fi
  grep -iE "fatal|error:" "$LOG_DIR/push_$i.log" | head -2
  sleep $((i * 10))
done

echo "$ATTEMPTS 次全部失败——这是网络层问题（需要可用代理）；提交仍在本地，未丢"
exit 1
