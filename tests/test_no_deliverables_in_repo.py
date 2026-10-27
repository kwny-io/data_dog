"""守卫：外发材料与桌面笔记不许进版本库。

失败模式（真实发生过）：桌面上的外发材料目录 `data_dog-PDF/` 就在仓库旁边，
`git add -A` 或 `git add .` 会把它整个吃进去。那些文件是**只在本地留存**的，
一旦进了提交历史，删掉也仍在历史里。

所以这里不是"检查一下有没有"，而是每次跑测试都拦一次：
- 已跟踪路径里出现 PDF / 外发材料 / 出稿目录 → FAIL
- 工作区出现这些未跟踪文件（说明 .gitignore 没盖住）→ FAIL

只白名单 `vendor_docs/` 下的厂商手册（那是产品依赖的输入文档，不是我们的产出）。

⚠️ 这个守卫第一版是**假的**，记在这里当教训：第一版直接 `"材料" in path` 匹配
`git ls-files` 的输出，而 git 默认 `core.quotepath=true`，非 ASCII 路径会输出成
`"data_dog-\\345\\217\\202\\350\\265\\233..."` 这种转义形式 —— 中文永远匹配不到，
守卫 100% 通过。当时用 `touch 材料.pdf` 做变异测试也没抓出来，因为那个
文件**根本不会被 git 看见**（未跟踪 ≠ 已跟踪），负对照本身是无效的。
正确做法：所有 git 调用带 `-c core.quotepath=false`（更稳的是用 `-z`），
并且变异测试必须让坏文件真的进索引（`git add -f`）才算数。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# 允许进仓库的 PDF：厂商原始手册（产品输入），不是我们的产出
ALLOWED_PDF_DIRS = ("vendor_docs/",)


def _git(*args: str) -> list[str]:
    """所有 git 调用必须关掉 quotepath，否则非 ASCII 路径会被转义成 \\345\\217\\202。

    这是本守卫第一版失效的真因：拿转义后的字符串去匹配中文，永远不命中。
    （更稳的写法是 `git ls-files -z` + NUL 分隔，本仓 `scripts/readme_counts.py`
    就是这么做的；这里保持按行读，但显式关掉转义。）
    """
    out = subprocess.run(
        ["git", "-C", str(REPO), "-c", "core.quotepath=false", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


def _allowed(path: str) -> bool:
    return any(path.startswith(d) for d in ALLOWED_PDF_DIRS)


def _is_forbidden(path: str) -> bool:
    return (
        path.lower().endswith(".pdf")
        or "材料" in path
        or path.startswith("data_dog-PDF/")
        or path.startswith("数据口径")
        or path.startswith("验证记录")
    ) and not _allowed(path)


def test_no_deliverable_tracked_in_repo() -> None:
    bad = [p for p in _git("ls-files") if _is_forbidden(p)]
    assert not bad, (
        "外发材料被跟踪进了版本库（应只在本地留存，进历史后删不掉）：\n  "
        + "\n  ".join(bad)
        + "\n→ 从索引移除：git rm --cached <path>；确认 .gitignore 已覆盖"
    )


def test_no_deliverable_untracked_in_worktree() -> None:
    """未跟踪也要拦：说明 .gitignore 没盖住，下一次 `git add -A` 就会进库。"""
    bad = [
        p for p in _git("ls-files", "--others", "--exclude-standard")
        if _is_forbidden(p)
    ]
    assert not bad, (
        "工作区有未被忽略的外发材料（下一次 git add -A 会进库）：\n  "
        + "\n  ".join(bad)
        + "\n→ 把这些路径加进 .gitignore"
    )


def test_quotepath_decoding_selfcheck() -> None:
    """自检：守卫赖以工作的前提——非 ASCII 路径能被原样读出来。

    没有这条，将来有人把 `-c core.quotepath=false` 去掉，另外两条测试会
    静默恢复成"永远通过"，而没有任何东西会报错。
    """

    name = "中文字符自检.tmp"
    probe = REPO / name
    try:
        probe.write_text("x", encoding="utf-8")
        subprocess.run(["git", "-C", str(REPO), "add", "-f", name], check=False,
                       capture_output=True)
        seen = _git("ls-files")
        assert name in seen, (
            f"守卫读不到非 ASCII 路径（读到的是转义形式）：{seen[:5]}\n"
            "→ git 调用缺 `-c core.quotepath=false`，两条断言已退化成永远通过"
        )
    finally:
        subprocess.run(["git", "-C", str(REPO), "reset", "-q", "--", name],
                       check=False, capture_output=True)
        probe.unlink(missing_ok=True)
