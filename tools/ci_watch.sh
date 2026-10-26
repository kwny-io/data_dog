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
# 参数顺序容错：文档写的是 `[branch] [sha]`，但实际调用时把 sha 写在前面很常见
# （我自己就传反过一次，结果脚本去查一个叫 7de8cad… 的分支，静默等满 90 分钟）。
# 判据只看形状：像 sha（≥7 位 hex）的那个是 sha，剩下一个是分支。
ARG1="${1:-main}"
ARG2="${2:-}"
is_sha() { case "$1" in (*[!0-9a-f]*) return 1;; esac; [ ${#1} -ge 7 ]; }
if is_sha "$ARG1"; then
  BRANCH="${ARG2:-main}"
  EXPECT_SHA="$ARG1"
else
  BRANCH="$ARG1"
  EXPECT_SHA="$ARG2"
fi
DEADLINE=$(( $(date +%s) + 5400 ))   # 最多等 90 分钟
# 同一个 commit 在仓库里可能有**多个工作流的 run**（CI / 软实时基准 / 文档……）。
# 只按 sha 匹配会绑到任一个，然后拿“1/1 success”报总绿 —— 这是假绿。
# 所以默认只认 CI 工作流，并要求 job 数不少于预期（CI 是 9 腿）。
WORKFLOW="${WORKFLOW:-CI}"
MIN_JOBS="${MIN_JOBS:-9}"

find_run() {
  gh run list --branch "$BRANCH" --workflow "$WORKFLOW" --limit 20 \
    --json databaseId,headSha,workflowName \
    --jq '.[] | "\(.databaseId) \(.headSha) \(.workflowName)"' 2>/dev/null | {
      while read -r id sha wf; do
        if [ -z "$EXPECT_SHA" ] || [ "${sha:0:7}" = "${EXPECT_SHA:0:7}" ]; then
          echo "$id $sha $wf"
          break
        fi
      done
    }
}

echo "=== 等 branch=$BRANCH 的 run（期望 commit: ${EXPECT_SHA:-HEAD}）==="
FOUND=""
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  FOUND=$(find_run)
  [ -n "$FOUND" ] && break
  sleep 20
done
if [ -z "$FOUND" ]; then
  echo "超时：没找到 workflow=$WORKFLOW 分支=$BRANCH 上对应 commit 的 run"
  echo "CI_RESULT=FAIL"
  exit 3
fi
RUN_ID=$(echo "$FOUND" | cut -d' ' -f1)
RUN_SHA=$(echo "$FOUND" | cut -d' ' -f2)
RUN_WF=$(echo "$FOUND" | cut -d' ' -f3-)
echo "run=$RUN_ID sha=$RUN_SHA workflow=$RUN_WF"

# watch 只用于等完（不依赖它的退出码，注释见 docs/delivery.md 关于"退出码兜底"）
gh run watch "$RUN_ID" --exit-status >/dev/null 2>&1
STATUS=$(gh run view "$RUN_ID" --json status,conclusion --jq '.status+" "+.conclusion' 2>/dev/null)
echo "run 状态: $STATUS"

# 逐 job 核对，不只看总绿
gh run view "$RUN_ID" --json jobs --jq '.jobs[] | "\(.conclusion)\t\(.name)"' 2>/dev/null

TOTAL=$(gh run view "$RUN_ID" --json jobs --jq '.jobs | length' 2>/dev/null)
OK=$(gh run view "$RUN_ID" --json jobs --jq '[.jobs[] | select(.conclusion=="success")] | length' 2>/dev/null)
BAD=$(gh run view "$RUN_ID" --json jobs --jq '[.jobs[] | select(.conclusion!="success")] | length' 2>/dev/null)
echo "job 统计：$OK/$TOTAL success，$BAD 非 success（要求 ≥$MIN_JOBS）"

if [ "${TOTAL:-0}" -lt "$MIN_JOBS" ]; then
  echo "绑定错工作流产？只看到 $TOTAL 个 job（要求 ≥$MIN_JOBS）—— 不算通过，CI_RESULT=FAIL"
  echo "CI_RESULT=FAIL"
  exit 1
fi

if [ "$BAD" = "0" ] && [ "$OK" = "$TOTAL" ] && [ "$TOTAL" != "0" ]; then
  echo "CI_RESULT=PASS"
  exit 0
fi
echo "CI_RESULT=FAIL"
exit 1
