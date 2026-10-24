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


# ------------------------------------------------ 被拒原因：代码 ↔ 文档 同源
#
# 为什么需要这条：`rejected_by_reason` 的键是**接入方唯一能拿到的诊断信息**。
# 代码里新增一个 reason 而文档没写，等于给了一个没人认识的错误码（值班人无法判断
# "该重试还是该找人"）。2026-10-24 修 payload 两个 500 时就新增了
# `payload_inline_not_string` / `payload_not_object`——必须有东西强迫它们进文档。

GATEWAY = REPO / "src" / "data_dog" / "data_plane" / "gateway.py"
PIPELINE = REPO / "src" / "data_dog" / "data_plane" / "pipeline.py"
SIGNATURE = REPO / "src" / "data_dog" / "data_plane" / "device_signature.py"


def _emitted_reject_reasons(*, gateway_text: str | None = None,
                           pipeline_text: str | None = None,
                           signature_text: str | None = None) -> set[str]:
    """从代码里枚举真正会被写进 `rejected_by_reason` 的键。

    四个来源缺一不可：
    - `gateway._reject("...")`：网关自身的拒绝；
    - `pipeline` 的 `reason="..."`（载荷错误）与 `stats.bump("...")`（绑定/解析器错误）；
    - 验签的**动态**原因：`gateway` 把 `check_device_signature()` 返回的第一项直接当
      reason 用，所以那三个字符串也得算在内——只看字面 `_reject("...")` 会漏掉它们。

    文本可注入（默认读真文件）：牙口自检靠传假文本，**绝不改写仓库里的源码**——
    一个会改源文件的测试在 CI 里被中断就是源码损坏。
    """
    gw = gateway_text if gateway_text is not None else GATEWAY.read_text(encoding="utf-8")
    pipe = pipeline_text if pipeline_text is not None else PIPELINE.read_text(encoding="utf-8")
    sig = signature_text if signature_text is not None else SIGNATURE.read_text(encoding="utf-8")
    reasons: set[str] = set()
    reasons |= set(re.findall(r'_reject\(\s*"([a-z_]+)"', gw))
    reasons |= set(re.findall(r'reason="([a-z_]+)"', pipe))
    reasons |= set(re.findall(r'stats\.bump\(\s*"([a-z_]+)"', pipe))
    reasons |= set(re.findall(r'return False,\s*"([a-z_]+)"', sig))
    return reasons


def _documented_reject_reasons() -> set[str]:
    """从 §3.6 的表格首列取 reason 名。"""
    text = DOC.read_text(encoding="utf-8")
    parts = text.split("### 3.6", 1)
    assert len(parts) == 2, "docs/data_plane.md 里找不到 §3.6（被拒原因全表）"
    body = parts[1].split("\n### ", 1)[0]
    return {m.group(1).strip() for line in body.splitlines()
            if (m := re.match(r"^\|\s*`([^`]+)`", line.strip()))}


def test_every_emitted_reject_reason_is_documented():
    emitted = _emitted_reject_reasons()
    documented = _documented_reject_reasons()
    assert emitted, "从代码里一个 reason 都没取到，正则失效了（不是代码没 reason）"
    missing = sorted(emitted - documented)
    assert not missing, (
        f"代码会发出这些 reason，但 docs/data_plane.md §3.6 没写：{missing}"
    )
    stale = sorted(documented - emitted)
    assert not stale, (
        f"§3.6 写了这些 reason，但代码里已经没有任何地方发出：{stale}（文档在骗人）"
    )


def test_reject_reason_doc_guard_has_teeth():
    """守卫自检：代码里加一个文档没写的 reason → 必须红。

    传假文本而不是改真文件：会改源文件的测试一旦被 Ctrl-C / CI kill，
    留下的就是损坏的仓库。
    """
    bogus = "not_a_real_reason_xyz"
    gw = GATEWAY.read_text(encoding="utf-8")
    mutated = gw.replace('_reject("auth"', f'_reject("{bogus}"', 1)
    assert mutated != gw, "无法在 gateway.py 里定位 _reject(\"auth\"，自检失效"

    emitted = _emitted_reject_reasons(gateway_text=mutated)
    assert bogus in emitted
    assert sorted(emitted - _documented_reject_reasons()) == [bogus]
