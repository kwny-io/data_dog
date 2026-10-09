"""从 pyproject.toml 生成 requirements.txt —— 依赖清单的**单一事实源**。

为什么需要它
------------
这个仓库同时维护两份依赖清单：`pyproject.toml` 与 `requirements.txt`。两份手抄的
清单必然漂移，而且漂移的后果很重：曾经 `core/metrics.py` 顶层 import
`prometheus_client`，pyproject 里声明了、requirements.txt 里没有——按
`pip install -r requirements.txt` 安装的人，服务直接 ImportError 起不来，
而本地 venv 恰好装过所以"测不出来"。这类缺口只能靠机器发现。

做法：**requirements.txt 由本脚本生成**，内容 = `[project].dependencies`
（服务运行必需）+ 一个"可选依赖"注释区（列出每个 extra，供人按需
`pip install -e ".[extra]"`）。CI 用 `--check` 校验仓库里的文件与生成结果一致，
漂移即红。

用法
----
    python scripts/gen_requirements.py            # 生成/覆盖 requirements.txt
    python scripts/gen_requirements.py --check    # 只校验（CI/测试用），漂移 exit 1

注意：`streamlit` 属于 `[ui]` extra（可选），因此生成结果里它在**注释区**。
早先的 requirements.txt 把它列为必需项，属于口径不一致——以 pyproject 为准。
"""
from __future__ import annotations

import argparse
import pathlib
import sys

try:                              # Python 3.10 无 tomllib（3.11 才进标准库）
    import tomllib
except ModuleNotFoundError:       # pragma: no cover - 仅 3.10 走这里
    import tomli as tomllib  # dev extra 里按 python_version < '3.11' 装了兜底

ROOT = pathlib.Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
REQUIREMENTS = ROOT / "requirements.txt"

HEADER = """\
# data_dog 运行依赖（`pip install -r requirements.txt`）
#
# ⚠ 本文件由 `scripts/gen_requirements.py` **自动生成，请勿手改**。
#   单一事实源是 pyproject.toml 的 [project.dependencies]（`pip install -e .` 会自动装齐）。
#   改了 pyproject 就重新生成：python scripts/gen_requirements.py
#   校验是否漂移：python scripts/gen_requirements.py --check
#
# 为什么要有这一份：给"只想跑服务、不装一整套 dev/ui 依赖"的场景用（容器瘦身、
# 现场排障机器）。所以它只列**运行必需**项，可选能力（UI / RAG / 沙箱 / 签名 /
# 各数据库与消息传输客户端）见文件末尾注释区，按需 `pip install -e ".[<extra>]"`。
"""


def generate() -> str:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = data["project"]
    deps: list[str] = list(project["dependencies"])
    extras: dict[str, list[str]] = project.get("optional-dependencies", {})

    lines = [HEADER, "# ---- 运行必需（来自 [project.dependencies]）----"]
    lines += deps
    lines.append("")
    lines.append("# ---- 可选依赖（不会被本文件安装）----")
    lines.append('# 按需：pip install -e ".[dev]" / ".[postgres]" / ".[ui]" / …')
    for name in sorted(extras):
        joined = ", ".join(extras[name])
        lines.append(f"# {name}: {joined}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="由 pyproject.toml 生成 requirements.txt")
    ap.add_argument("--check", action="store_true", help="只校验，不写文件（漂移则 exit 1）")
    args = ap.parse_args()

    want = generate()
    have = REQUIREMENTS.read_text(encoding="utf-8") if REQUIREMENTS.exists() else ""
    if want == have:
        print("requirements.txt 与 pyproject.toml 一致（无漂移）")
        return 0
    if args.check:
        print("requirements.txt 与 pyproject.toml 已漂移，差异如下：")
        import difflib
        for line in difflib.unified_diff(have.splitlines(), want.splitlines(),
                                         "requirements.txt(仓库里)", "requirements.txt(生成的)",
                                         lineterm="", n=2):
            print("  " + line)
        print("修法：python scripts/gen_requirements.py")
        return 1
    REQUIREMENTS.write_text(want, encoding="utf-8")
    print(f"已重新生成 {REQUIREMENTS.relative_to(ROOT)}（{len(want.splitlines())} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
