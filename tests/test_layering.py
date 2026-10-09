"""分层依赖不变量：把"架构"变成可执行的断言。

为什么需要
----------
README 与 docs 都在讲分层（`service` 是唯一顶层；`core` / `schemas` / `storage`
是底部叶子；别的层只往下依赖）。但这类声明此前**没有任何机制守着**——底层模块里
出现一句 `from ..service import x`，分层就悄悄退化了（顺带埋下循环导入的隐患），
而且不会有任何测试变红。这里把方向钉住。

当前事实（拆分 `service/app.py` 后实测）
- 没有任何模块 import `service` → service 是唯一顶层；
- `core` / `schemas` / `storage` 不 import 任何其它 data_dog 子包 → 叶子层；
- 唯一的环是 `synth ↔ mujoco_sim`（mujoco_sim 产样本、synth 消费与评估），
  属有意的耦合，显式白名单；出现**新的**环即失败。

边界：只判"层与层"的方向，不判具体函数依赖（那属于设计评审，不是测试能兜的）。
"""
from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "data_dog"
LAYERS = {p.name for p in SRC.iterdir() if p.is_dir()}

TOP_LAYER = "service"
LEAF_LAYERS = {"core", "schemas", "storage"}
ALLOWED_CYCLES = [{"synth", "mujoco_sim"}]          # 有意耦合，见模块 docstring


def _layer_edges() -> set[tuple[str, str]]:
    """收集 (源层, 目标层) 依赖边；忽略同层内部依赖。"""
    edges: set[tuple[str, str]] = set()
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC)
        src = rel.parts[0] if len(rel.parts) > 1 else "data_dog"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            target: str | None = None
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.level:
                    # 相对导入：按层数回溯出目标包名
                    depth = node.level - 1
                    base = list(rel.parts[:-1])[:len(rel.parts) - 1 - depth]
                    parts = base + node.module.split(".")
                    target = parts[0] if parts else None
                elif node.module.startswith("data_dog."):
                    parts = node.module.split(".")
                    target = parts[1] if len(parts) > 1 else None
            if target and target in LAYERS and target != src:
                edges.add((src, target))
    return edges


def _cycles(edges: set[tuple[str, str]]) -> list[set[str]]:
    """Tarjan 求强连通分量（层数很少，递归安全），只返回大小 >1 的分量。"""
    graph: dict[str, set[str]] = {}
    for a, b in edges:
        graph.setdefault(a, set()).add(b)
        graph.setdefault(b, set())
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    counter = [0]
    out: list[set[str]] = []

    def strong(v: str) -> None:
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in graph[v]:
            if w not in index:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = set()
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.add(w)
                if w == v:
                    break
            if len(comp) > 1:
                out.append(comp)

    for v in list(graph):
        if v not in index:
            strong(v)
    return out


def test_service_is_the_only_top_layer():
    """没有任何模块 import service：顶层只能被 entrypoint（cli/service/scripts）依赖。"""
    offenders = sorted(a for a, b in _layer_edges() if b == TOP_LAYER)
    assert not offenders, f"这些层反向依赖了顶层 service：{offenders}"
    print("PASS test_service_is_the_only_top_layer")


def test_leaf_layers_import_nothing_internal():
    """core / schemas / storage 是叶子层：不得 import 任何其它 data_dog 子包。"""
    offenders = sorted((a, b) for a, b in _layer_edges() if a in LEAF_LAYERS)
    assert not offenders, f"叶子层出现了对外依赖：{offenders}"
    print("PASS test_leaf_layers_import_nothing_internal")


def test_no_new_dependency_cycles():
    """除白名单里的 synth ↔ mujoco_sim，不允许出现新的依赖环（环会带来循环导入）。"""
    found = _cycles(_layer_edges())
    unexpected = [sorted(c) for c in found
                  if not any(c == allowed for allowed in ALLOWED_CYCLES)]
    assert not unexpected, (
        f"出现新的层间依赖环：{unexpected}；"
        f"白名单（有意耦合）：{[sorted(c) for c in ALLOWED_CYCLES]}")
    print("PASS test_no_new_dependency_cycles")
