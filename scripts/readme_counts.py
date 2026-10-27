"""README 规模数字的**单一事实源**：重新数出来并回填 README。

为什么需要它
------------
README 用精确数字做承诺（源码多少文件多少行、测试多少文件多少行、前端多少文件、
多少个 make 命令）。手写这些数字一定会烂：加一个文件就漂一位，而且**没人会发现**
——直到有人真的去数一遍。与其靠人肉记性，不如让脚本能一键重算，
再由 `tests/test_project_meta.py` 强制"README 里写的 == 真实的"。

"真实的"以什么为准：**git 跟踪的文件**（不是磁盘上的文件）
--------------------------------------------------------
README 的规模数字描述的是**仓库**，不是某个工作树里此刻恰好存在的文件。按磁盘数会有
两个具体毛病：① 未跟踪的草稿/临时文件会污染声明；② 并行开发或多工作树时，"提交子集"
会让 README 计数与提交内容不一致（这份文件数了 53 个测试，而那次提交只带进去 50 个），
于是每个提交都可能不自洽。故：**能问 git 就问 git**（`git ls-files`），
非 git 环境（sdist / tarball / 无 git）自动退回磁盘扫描。

用法
----
    python scripts/readme_counts.py            # 回填 README（默认）
    python scripts/readme_counts.py --check    # 只校验，漂移则 exit 1（测试/CI 用）
    # 运行时数字（跑一次才知道）：先 `pytest -q` 看结果，再同步
    python scripts/readme_counts.py --tests-passed 800 --tests-skipped 9

口径（与 README 的表述一一对应，改口径必须同时改这里和测试）
- 源码：`src/**/*.py` 的文件数与总行数
- 测试：`tests/test_*.py` 的文件数与总行数（不含 conftest.py / __init__.py）
- 前端：`web/**/*.ts` + `*.tsx`，**排除** node_modules 与 .next*
- make：Makefile 里的 target 数**减去 help**
- 运行时测试数（`**N 项测试**` / `` `P passed, S skipped` ``）：**只能由人跑一次才知道**，
  故用 `--tests-passed/--tests-skipped` 显式同步——测试无法验证它（静态文件里没有运行期信息），
  这一点如实写在这里，而不是假装它被自动守住了。
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
README = ROOT / "README.md"


def tracked_files() -> set[pathlib.Path] | None:
    """git 跟踪的文件（绝对路径）；非 git 环境返回 None → 调用方退回磁盘扫描。

    **局部提交（`git commit -- <paths>`）时不要用索引**：git 会把 `GIT_INDEX_FILE`
    指向一个临时索引，它只含 HEAD + 本次提交的路径，别的已暂存新增文件不在里面。
    于是 pre-commit 里算出来的规模比全树少（实测：新增两个 .ts 文件被漏算，
    README 写 76、钩子数到 74），而报错信息还说“README 与实际不一致”——
    真正不一致的是“索引与工作树”。这种误红比漏报更贵：它教人 `--no-verify`。

    所以：不退回磁盘扫描（磁盘会多算本地临时文件），而是**读真实索引**：
    把 `GIT_INDEX_FILE` 从子进程环境里抹掉，git 就会回去读 `.git/index`。
    全仓都被跟踪时两者等价，但后者不会被工作树里的 scratch 文件污染。
    """
    env = {k: v for k, v in os.environ.items() if k != "GIT_INDEX_FILE"}
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                             capture_output=True, check=True, env=env)
    except (OSError, subprocess.CalledProcessError):
        return None
    names = [n for n in out.stdout.decode("utf-8", "replace").split("\0") if n]
    return {ROOT / n for n in names}


def select(subdir: str, predicate) -> list[pathlib.Path]:
    """挑出 `subdir` 下满足 predicate 的文件；优先用 git 跟踪集，退化到磁盘扫描。"""
    pool = tracked_files()
    if pool is None:
        base = ROOT / subdir
        return sorted(p for p in base.rglob("*") if p.is_file() and predicate(p))
    base = ROOT / subdir
    return sorted(p for p in pool if p.is_file() and p.is_relative_to(base) and predicate(p))


def _lines(files: list[pathlib.Path]) -> int:
    return sum(len(p.read_text(encoding="utf-8", errors="replace").splitlines()) for p in files)


def count_src() -> tuple[int, int]:
    files = select("src", lambda p: p.suffix == ".py")
    return len(files), _lines(files)


def count_tests() -> tuple[int, int]:
    files = select("tests", lambda p: p.suffix == ".py" and p.name.startswith("test_"))
    return len(files), _lines(files)


def count_web() -> int:
    """前端源码文件数：排除 node_modules 与 Next.js 的生成物。

    注意 `.next` 之外还有 `.next-preview/`、以及生成文件 `next-env.d.ts`——
    它们体积与数量都不稳定（随构建变化），计进"项目规模"就是虚报。按 git 跟踪计数
    时它们本来就不在（被 .gitignore 忽略），但无 git 的退路模式必须自己排掉。
    """
    def is_source(p: pathlib.Path) -> bool:
        gen = {"node_modules"} | {part for part in p.parts if part.startswith(".next")}
        return (p.suffix in (".ts", ".tsx")
                and not (gen & set(p.parts))
                and p.name != "next-env.d.ts")
    return len(select("web", is_source))


def count_make_commands() -> int:
    """Makefile target 数减去 help（`help` 只打印用法，不算一条命令）。"""
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    targets = set(re.findall(r"^([A-Za-z][A-Za-z0-9_-]*):", text, re.M))
    return len(targets - {"help"})


def _repl(text: str, pattern: str, repl: str, what: str,
          optional: bool = False) -> tuple[str, list[str]]:
    """整体替换（count==1 才动，否则报错——README 结构变了就得改脚本）。

    optional=True 时，找不到就**跳过并提示**，而不是失败：用于“只能靠跑一次测试
    才知道”的数字。把它做成硬门禁的后果，是任何一次没有现成跑分记录的回填都会被
    卡住（本轮就卡在这里）；而它本来也守不住漂移——真变的只有源码/测试文件数，
    那些是硬的。
    """
    hits = re.findall(pattern, text)
    if len(hits) != 1:
        msg = (f"[readme_counts] 期望 README 里 {what} 恰好出现 1 次，实际 {len(hits)} 次；"
               f"请先修 README 或本脚本的正则：{pattern}")
        if optional:
            print(f"[readme_counts] 跳过：{msg}")
            return text, []
        raise SystemExit(msg)
    return re.sub(pattern, repl, text, count=1), [f"{what}: {hits[0]} -> {repl}"]


def build(tests_passed: int | None = None,
          tests_skipped: int | None = None) -> tuple[str, list[str]]:
    src_n, src_l = count_src()
    tst_n, tst_l = count_tests()
    web_n = count_web()
    mk_n = count_make_commands()

    text = README.read_text(encoding="utf-8")
    changes: list[str] = []
    text, ch = _repl(text, r"源码 \d+ 个 Python 文件 / \d+ 行",
                     f"源码 {src_n} 个 Python 文件 / {src_l} 行", "源码规模")
    changes += ch
    text, ch = _repl(text, r"测试 \d+ 个文件 / \d+ 行",
                     f"测试 {tst_n} 个文件 / {tst_l} 行", "测试规模")
    changes += ch
    text, ch = _repl(text, r"（\d+ 个 TS/TSX 文件）", f"（{web_n} 个 TS/TSX 文件）", "前端文件数")
    changes += ch
    text, ch = _repl(text, r"\d+ 个 `make` 命令", f"{mk_n} 个 `make` 命令", "make 命令数")
    changes += ch
    # 测试文件数在亮点表里也出现一次（"（48 个测试文件，…"），必须同源
    text, ch = _repl(text, r"（\d+ 个测试文件，", f"（{tst_n} 个测试文件，", "亮点表测试文件数")
    changes += ch
    if tests_passed is not None and tests_skipped is not None:
        # 这两个槽位是可选槽位：README 若已写明“条数以命令输出为准”，就没有它们。
        text, ch = _repl(text, r"\*\*\d+ 项测试，全离线确定性\*\*",
                         f"**{tests_passed + tests_skipped} 项测试，全离线确定性**", "测试总数",
                         optional=True)
        changes += ch
        text, ch = _repl(text, r"`\d+ passed, \d+ skipped`",
                         f"`{tests_passed} passed, {tests_skipped} skipped`", "运行时测试结果",
                         optional=True)
        changes += ch
    return text, changes


def main() -> int:
    ap = argparse.ArgumentParser(description="重算并回填 README 的规模数字")
    ap.add_argument("--check", action="store_true", help="只校验，不写文件（漂移则 exit 1）")
    ap.add_argument("--tests-passed", type=int, default=None,
                    help="pytest -q 的 passed 数（与 --tests-skipped 一起给出才生效）")
    ap.add_argument("--tests-skipped", type=int, default=None,
                    help="pytest -q 的 skipped 数")
    args = ap.parse_args()

    new_text, changes = build(args.tests_passed, args.tests_skipped)
    old_text = README.read_text(encoding="utf-8")
    if old_text == new_text:
        print("README 规模数字与实际一致（无需改动）")
        return 0
    if args.check:
        print("README 规模数字已漂移：")
        for c in changes:
            print("  " + c)
        print("修法：python scripts/readme_counts.py")
        return 1
    README.write_text(new_text, encoding="utf-8")
    print("已回填 README：")
    for c in changes:
        print("  " + c)
    return 0


if __name__ == "__main__":
    sys.exit(main())
