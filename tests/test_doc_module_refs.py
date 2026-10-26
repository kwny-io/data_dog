"""文档里引用的模块路径必须真实存在（防"文档写了、代码里没有"）。

## 为什么需要这条

2026-10-26 我在克隆复现脚本里写下 `python -m data_dog.mujoco.gen_dataset`，
真实路径是 `data_dog.mujoco_sim.gen_dataset`——`ModuleNotFoundError` 才发现。
同一类事故本仓已有先例：文档写 `pip install -e ".[mujoco]"`，而 pyproject 里没有这个
extra（被 CI 抓到）。共同点：**文档里的标识符和代码没有绑定**，只能靠人记得住。

## 判据

扫描文档中形如 `python -m data_dog.<a>.<b>`、`src/data_dog/<x>.py` 的引用，
用 `importlib.util.find_spec` 逐个确认可导入/文件存在。**任何一条解析不了就红。**

## 它防的是什么具体回归

- 重命名/搬移模块后文档不跟着改（人眼看不出来）；
- 凭空写出一个"看起来对"的模块路径（我刚犯的）；
- 文档里引用的 `src/...py` 文件被删除。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
DOCS = sorted(REPO.glob("docs/*.md")) + [REPO / "README.md"]

# `python -m data_dog.agent.degradation_demo` / `python -m data_dog.service`
MODULE_REF = re.compile(r"\bpython -m (data_dog(?:\.[A-Za-z_][\w]*)+)")
# `src/data_dog/agent/degradation.py`
PATH_REF = re.compile(r"\bsrc/(data_dog/[\w/]+\.py)")


def _doc_files() -> list[Path]:
    return [p for p in DOCS if p.is_file()]


@pytest.fixture(scope="module", autouse=True)
def _src_on_path() -> None:
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))


def _importable(mod: str) -> bool:
    """父包不存在时 find_spec 会抛 ModuleNotFoundError（而不是返回 None），一并当作不可导入。"""
    try:
        return importlib.util.find_spec(mod) is not None
    except (ModuleNotFoundError, ImportError, ValueError):
        return False


def test_documented_module_paths_are_importable() -> None:
    bad: list[str] = []
    seen = 0
    for doc in _doc_files():
        text = doc.read_text(encoding="utf-8")
        for mod in sorted(set(MODULE_REF.findall(text))):
            seen += 1
            if not _importable(mod):
                bad.append(f"{doc.relative_to(REPO)}: python -m {mod}")
    assert seen > 0, "没扫到任何 python -m data_dog.* 引用——正则或文档结构变了，本测试已失效"
    assert not bad, "文档引用了不存在的模块：\n" + "\n".join(bad)


def test_documented_source_file_paths_exist() -> None:
    bad: list[str] = []
    seen = 0
    for doc in _doc_files():
        text = doc.read_text(encoding="utf-8")
        for rel in sorted(set(PATH_REF.findall(text))):
            seen += 1
            if not (SRC / rel).is_file():
                bad.append(f"{doc.relative_to(REPO)}: src/{rel}")
    assert seen > 0, "没扫到任何 src/data_dog/*.py 引用——正则或文档结构变了"
    assert not bad, "文档引用了不存在的源文件：\n" + "\n".join(bad)
