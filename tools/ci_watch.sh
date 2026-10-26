#!/usr/bin/env bash
# 等某个分支最新一次真 CI 跑完，逐 job 核对结论。
#
# 为什么是脚本而不是内联命令：在 Windows 上 bg_run 的命令最终经 cmd.exe 解析，
# 内联里的 `|`、`$(...)`、嵌套引号会被 cmd 吃掉或重定向（已经踩了三次）。
#
# 用法：tools/ci_watch.sh [branch] [expected_sha]
#   输出最后一行固定为 CI_RESULT=PASS 或 CI_RESULT=FAIL（机器读这一行）。
#   退出码：0=全绿 / 1=有 job 失败 / 3=还没找到该 commit 的 run（超时）
set -u
BRANCH="${1:-main}"
EXPECT_SHA="${2:-}"
DEADLINE=$(( $(date +%s) + 5400 ))   # 最多等 90 分钟

find_run() {
  gh run list --branch "$BRANCH" --limit 20 --json databaseId,headSha \
    --jq '.[] | "\(.databaseId) \(.headSha)"' 2>/dev/null | {
      while read -r id sha; do
        if [ -z "$EXPECT_SHA" ] || [ "${sha:0:7}" = "${EXPECT_SHA:0:7}" ]; then
          echo "$id $sha"
          break
        fi
      done
    }
}

echo "=== 等 $BRANCH 的 run（期望 commit: ${EXPECT_SHA:-HEAD}）==="
FOUND=""
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  FOUND=$(find_run)
  [ -n "$FOUND" ] && break
  sleep 20
done
if [ -z "$FOUND" ]; then
  echo "超时：没找到对应 commit 的 run"
  echo "CI_RESULT=FAIL"
  exit 3
fi
RUN_ID=${FOUND%% *}
RUN_SHA=${FOUND##* }
echo "run=$RUN_ID sha=$RUN_SHA"

# watch 只用于等完（不依赖它的退出码，注释见 docs/delivery.md 关于"退出码兜底"）
gh run watch "$RUN_ID" --exit-status >/dev/null 2>&1
STATUS=$(gh run view "$RUN_ID" --json status,conclusion --jq '.status+" "+.conclusion' 2>/dev/null)
echo "run 状态: $STATUS"

# 逐 job 核对，不只看总绿
gh run view "$RUN_ID" --json jobs --jq '.jobs[] | "\(.conclusion)\t\(.name)"' 2>/dev/null

TOTAL=$(gh run view "$RUN_ID" --json jobs --jq '.jobs | length' 2>/dev/null)
OK=$(gh run view "$RUN_ID" --json jobs --jq '[.jobs[] | select(.conclusion=="success")] | length' 2>/dev/null)
BAD=$(gh run view "$RUN_ID" --json jobs --jq '[.jobs[] | select(.conclusion!="success")] | length' 2>/dev/null)
echo "job 统计：$OK/$TOTAL success，$BAD 非 success"

if [ "$BAD" = "0" ] && [ "$OK" = "$TOTAL" ] && [ "$TOTAL" != "0" ]; then
  echo "CI_RESULT=PASS"
  exit 0
fi
echo "CI_RESULT=FAIL"
exit 1
