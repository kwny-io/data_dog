"""撞 test_run_log 的间歇卡死，并在卡死时把所有线程的栈打出来。

为什么需要它：2026-10-25 隔离跑 200 次，第 190 次 pytest 进程写出 0 字节日志后
就再没动静，跑到 1 小时被外部超时杀掉——**不是断言失败，是死等**。
没有栈就无法定位，所以这次让子进程自带 faulthandler 定时器（90s 未结束即
dump 所有线程栈 + exit），把"卡在哪一行"变成证据。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

TEST = "tests/test_run_log.py::test_sync_investigation_writes_readable_run_log"
ROUNDS = int(os.environ.get('HUNT_ROUNDS', '240'))
HANG_AFTER_S = 90

CHILD = f"""
import faulthandler, sys
faulthandler.dump_traceback_later({HANG_AFTER_S}, exit=True)
import pytest
raise SystemExit(pytest.main([{TEST!r}, "-q", "-p", "no:randomly"]))
"""


def main() -> int:
    # tools/ 下这个文件：parents[0]=tools, parents[1]=仓库根
    repo = Path(__file__).resolve().parents[1]
    py = repo / ".venv" / "Scripts" / "python.exe"
    test_file = repo / TEST.split("::")[0]
    # 正对照：锚错目录时立刻报错，而不是“跑完 240 次全部通过”（假绿）
    for need, label in ((py, "解释器"), (test_file, "被测文件")):
        if not need.exists():
            print(f"锚点错误：找不到{label} {need}（repo={repo}）", file=sys.stderr)
            return 2

    done = 0
    started = time.time()
    for i in range(1, ROUNDS + 1):
        t0 = time.time()
        r = subprocess.run([str(py), "-c", CHILD], cwd=repo, capture_output=True, text=True)
        dur = time.time() - t0
        done += 1
        # faulthandler 的 exit=True 走的是 os._exit(1)，输出里一定有 Timeout 字样
        hung = "Timeout (" in r.stderr or dur >= HANG_AFTER_S
        if r.returncode != 0 or hung:
            print(f"!!! 第 {i} 次：rc={r.returncode} 耗时={dur:.1f}s hung={hung}")
            print("---- stderr ----")
            print(r.stderr[-8000:])
            print("---- stdout ----")
            print(r.stdout[-3000:])
            break
        if i % 20 == 0:
            print(f"… 已跑 {i} 次，本次 {dur:.1f}s，无异常", flush=True)
    else:
        print(f"跑完 {done} 次，全部通过（均 < {HANG_AFTER_S}s）。未复现卡死。")
    print(f"总耗时 {time.time() - started:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
