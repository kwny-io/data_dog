#!/usr/bin/env bash
# 全新克隆复现测试：从 GitHub 拉到干净目录，按 README 的方式装，然后跑核心校验。
#
# 为什么必须做：本会话所有"验过"都在被改脏的工作区里做的——
# `data/` 被 .gitignore 排除那个缺陷就是这么漏掉的（本地能跑，克隆下来就断）。
#
# 用法： bash tools/clone_repro.sh                 # 克隆到 /d/dd_clone_test
#        DEST=/e/tmp/x bash tools/clone_repro.sh   # 换目录
#
# 判据（全部为硬失败，不是"看一眼"）：
#   1 克隆后 `git status --porcelain` 为空；
#   2 `pip install -e ".[dev]"` 退出码 0；
#   3 `data-dog --help` 退出码 0；
#   4 数据集能从零生成且产出文件非空（docs 承诺的路径）；
#   5 文档/守卫测试全绿；
#   6 离线 demo 三候选三结论（评委第一眼看到的东西）。
# 最后一行固定为 CLONE_REPRO=PASS 或 CLONE_REPRO=FAIL（机器读这一行）。
set -u
DEST="${DEST:-/d/dd_clone_test}"
REPO=https://github.com/kwny-io/data_dog.git
FAILED=0
note() { echo "❌ $1"; FAILED=$((FAILED + 1)); }

rm -rf "$DEST"
echo "=== 1/6 克隆 $REPO ==="
if ! git -c http.proxy= -c https.proxy= clone --depth 1 "$REPO" "$DEST" >/tmp/clone.log 2>&1; then
  echo "克隆失败，见 /tmp/clone.log"; echo "CLONE_REPRO=FAIL"; exit 1
fi
cd "$DEST" || { echo "CLONE_REPRO=FAIL"; exit 1; }
DIRTY=$(git status --porcelain | head -5)
if [ -n "$DIRTY" ]; then note "克隆后工作区不干净：$DIRTY"; else echo "✅ 克隆后工作区为空"; fi
echo "   提交：$(git log --oneline -1)"
echo "   被追踪的 data/ 文件数：$(git ls-files data/ | wc -l)（0 表示数据集必须现场生成）"

echo "=== 2/6 建 venv 并按 README 安装 ==="
python -m venv .venv || { echo "CLONE_REPRO=FAIL"; exit 1; }
PY=./.venv/Scripts/python.exe
[ -x "$PY" ] || PY=./.venv/bin/python
"$PY" -m pip install -q --upgrade pip >/tmp/pip_up.log 2>&1
if "$PY" -m pip install -q -e ".[dev,sim]" >/tmp/pip_install.log 2>&1; then
  echo "✅ pip install -e .[dev,sim]（含 sim，数据集生成必需；约 50MB）"
else
  note "pip install 失败，见 /tmp/pip_install.log"; tail -5 /tmp/pip_install.log
fi

echo "=== 3/6 CLI 可执行 ==="
BIN=./.venv/Scripts/data-dog.exe
[ -x "$BIN" ] || BIN=./.venv/bin/data-dog
if "$BIN" --help >/dev/null 2>&1; then echo "✅ data-dog --help"; else note "data-dog --help 失败"; fi

echo "=== 4/6 数据集能否从零生成（docs 承诺的路径）==="
if "$PY" -m data_dog.mujoco_sim.gen_dataset --trials 8 --duration 2 --workers 2 \
     --out "$DEST/data/mujoco_clone_smoke" >/tmp/gen.log 2>&1; then
  N=$(ls "$DEST"/data/mujoco_clone_smoke/*.jsonl 2>/dev/null | wc -l)
  if [ "$N" -gt 0 ]; then echo "✅ 生成 $N 个 manifest 分片"; else note "生成命令成功但没有产出 jsonl"; fi
else
  note "数据集生成失败，见 /tmp/gen.log"; tail -5 /tmp/gen.log
fi

echo "=== 5/6 文档/守卫测试（文档与代码同源性）==="
if "$PY" -m pytest tests/ -q -k "doc or readme or ci_job or timeout" 2>&1 | tail -4; then :; fi
"$PY" -m pytest tests/ -q -k "doc or readme or ci_job or timeout" >/tmp/guard.log 2>&1 \
  && echo "✅ 守卫测试全绿" || { note "守卫测试有失败，见 /tmp/guard.log"; tail -6 /tmp/guard.log; }

echo "=== 6/6 离线 demo（评委第一眼会看到的东西）==="
if "$BIN" test --offline --all >/tmp/demo.log 2>&1; then
  # 三候选三结论：两个 REJECTED + 一个 NEEDS_HUMAN_REVIEW
  R=$(grep -c "REJECTED" /tmp/demo.log || true)
  H=$(grep -c "NEEDS_HUMAN_REVIEW" /tmp/demo.log || true)
  if [ "$R" -ge 2 ] && [ "$H" -ge 1 ]; then
    echo "✅ 三候选三结论（REJECTED×$R, NEEDS_HUMAN_REVIEW×$H）"
  else
    note "demo 输出不符（REJECTED×$R, NEEDS_HUMAN_REVIEW×$H）"; tail -6 /tmp/demo.log
  fi
else
  note "离线 demo 失败，见 /tmp/demo.log"; tail -8 /tmp/demo.log
fi

echo "=== 结论 ==="
if [ "$FAILED" -eq 0 ]; then echo "CLONE_REPRO=PASS"; exit 0; fi
echo "$FAILED 项失败"; echo "CLONE_REPRO=FAIL"; exit 1
