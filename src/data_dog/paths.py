# -*- coding: utf-8 -*-
"""项目资源路径解析（源码态 / 已安装态 / 容器态都能用）。

**为什么需要它**：以前各模块用 `Path(__file__).resolve().parents[3]` 推"仓库根"。
源码态下 `src/data_dog/x.py` 上溯 3 层确实是仓库根，是对的；但 `pip install` 之后
代码被装进 `site-packages/data_dog/`，上溯 3 层变成 `site-packages` 的上层——
于是找不到 `vendor_docs/`，知识包为空、算子规则缺失，服务静默交出错误结果。
（这个 bug 就是在 Docker 实跑时才暴露的，见 docs/delivery.md。）

解析顺序（先到先得）：
    1. 环境变量显式指定（容器/生产用这个）
    2. 从本文件所在目录上溯，找含 marker 的目录
    3. 从当前工作目录上溯
    4. 兜底返回当前工作目录
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

# 认定"项目根"的标志物：仓库里至少有一个
_ROOT_MARKERS = ("pyproject.toml", "vendor_docs")


def _walk_up_for(start: Path, marker: str, max_up: int = 8) -> Optional[Path]:
    cur = start
    for _ in range(max_up):
        if (cur / marker).exists():
            return cur
        if cur.parent == cur:          # 到盘符根，停
            break
        cur = cur.parent
    return None


def project_root() -> Path:
    """项目根目录（best-effort）。容器里建议用 DD_PROJECT_ROOT 显式指定。"""
    env = os.environ.get("DD_PROJECT_ROOT", "").strip()
    if env:
        return Path(env)

    from_file = Path(__file__).resolve().parent
    for marker in _ROOT_MARKERS:
        hit = _walk_up_for(from_file, marker)
        if hit:
            return hit
    for marker in _ROOT_MARKERS:
        hit = _walk_up_for(Path.cwd(), marker)
        if hit:
            return hit
    return Path.cwd()


def vendor_docs_dir() -> Path:
    """厂商文档根目录（`vendor_docs/`）。

    可用 DD_VENDOR_DOCS 直接指定，容器里指向挂载/拷贝进来的目录。
    """
    env = os.environ.get("DD_VENDOR_DOCS", "").strip()
    if env:
        return Path(env)
    return project_root() / "vendor_docs"


def operators_dir() -> Path:
    """算子产物目录（`operators/`）。"""
    env = os.environ.get("DD_OPERATORS_DIR", "").strip()
    if env:
        return Path(env)
    return project_root() / "operators"


def data_dir() -> Path:
    """运行期本地数据目录（默认 SQLite / 向量库等）。"""
    env = os.environ.get("DD_DATA_DIR", "").strip()
    if env:
        return Path(env)
    return project_root()
