"""文档与前端契约的同源守卫：`docs/data_plane.md` §3.5 说的字段，TS 接口里必须真有。

为一条真实的漂移写的：后端 `POST /v1/telemetry` 在 #102 里加了 `partial`
（`src/data_dog/service/routes/telemetry.py`，三条返回路径都带），文档也要求调用方
"看 `persisted` 与 `partial`"，但 `web/lib/api/types.ts` 的 `IngestResult` 里根本没有这个字段——
于是本仓库自己的 TS 客户端**看不到**它，谁照着文档写前端谁就踩空。

为什么用文档表格而不是 OpenAPI：该路由没有 `response_model`（`@router.post("/v1/telemetry")`
只带 tags），FastAPI 生成不出响应 schema，所以文档表格是当前唯一的机读契约来源。
哪天补了 response_model，这个守卫应该改成读 OpenAPI——那时的断言只会更强。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DOC = REPO / "docs" / "data_plane.md"
TYPES = REPO / "web" / "lib" / "api" / "types.ts"

# 文档表格里以 `stats.` / `persisted.` 开头的行说的是子字段，顶层契约由它们的父对象代表。
_SUBFIELD_PARENTS = {"stats": "stats", "persisted": "persisted"}


def _documented_top_level_fields() -> set[str]:
    """从 §3.5 的响应语义表里取出顶层字段名（`ok` / `partial` / `stats` …）。"""
    text = DOC.read_text(encoding="utf-8")
    section = text.split("### 3.5", 1)
    assert len(section) == 2, "docs/data_plane.md 里找不到 §3.5（响应语义表）"
    body = section[1].split("\n### ", 1)[0]
    names: set[str] = set()
    for line in body.splitlines():
        m = re.match(r"^\|\s*`([^`]+)`", line.strip())
        if not m:
            continue
        field = m.group(1).strip()
        head = re.split(r"[.\s]", field, maxsplit=1)[0]
        names.add(head)
    assert {"ok", "partial"} <= names, f"§3.5 表格解析异常，取到：{sorted(names)}"
    return names


def _ts_interface_fields(interface: str) -> set[str]:
    """取 `types.ts` 里某个 interface 的**顶层**字段名（忽略嵌套对象的内容）。"""
    text = TYPES.read_text(encoding="utf-8")
    m = re.search(rf"export interface {interface} \{{(.*?)\n\}}", text, re.S)
    assert m, f"web/lib/api/types.ts 里找不到 interface {interface}"
    fields: set[str] = set()
    depth = 0
    for raw in m.group(1).splitlines():
        line = raw.split("//")[0].strip()
        if depth == 0:
            fm = re.match(r"^([A-Za-z_]\w*)\??\s*:", line)
            if fm:
                fields.add(fm.group(1))
        depth += line.count("{") + line.count("(") + line.count("[")
        depth -= line.count("}") + line.count(")") + line.count("]")
    return fields


def test_documented_ingest_response_fields_exist_in_ts_contract():
    """文档让调用方看的每个顶层字段，TS 的 `IngestResult` 里都必须有。"""
    documented = _documented_top_level_fields()
    typed = _ts_interface_fields("IngestResult")
    missing = sorted(documented - typed - set(_SUBFIELD_PARENTS))
    assert not missing, (
        "docs/data_plane.md §3.5 写了这些顶层字段，但 web/lib/api/types.ts 的 "
        f"IngestResult 里没有：{missing}（typed={sorted(typed)}）"
    )


def test_ingest_contract_carries_partial_flag():
    """`partial` 是 #102 的核心机读标记，TS 契约里缺它 = 修了个用户看不见的东西。"""
    assert "partial" in _ts_interface_fields("IngestResult"), (
        "IngestResult 缺 partial：200 + ok 会被当成“全部落库”，正是 #102 修的那个误读"
    )


# 注意：这里**故意不写**「后端源码里必须出现 "partial" 字样」这类反向断言。
# 它看起来很周全，实际是弱守卫——把 `"partial": partial` 改成 `"partial": True` 依旧绿（实测）。
# 后端是否真的按批内拒绝标 partial，已有**行为级**覆盖：
#   tests/test_service.py:121  （全部落库 → partial is False）
#   tests/test_service.py:1129 （批内被限流 → partial is True 且 normalized == 0）
# 同一个事实不要用两种强度写两遍：弱的那份只会让人误以为有保护。

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
