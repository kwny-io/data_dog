"""守卫：`docs/data_plane.md` 里那份「最小可复制接入示例」必须**真的能跑出候选**。

为什么值得一条测试
------------------
接入文档最容易骗人：示例少一个必填字段、或 `payload_inline` 写成对象而不是字符串，
接口就返回 200 + `partial:true`，**既不报错也永远没有候选**——用户看到"接入成功、列表全空"，
只能去猜是产品坏了还是自己写错了。2026-10-25 我自己就踩了这个坑（七个必填字段只写了四个）。

所以文档里的示例不再靠人看，而是被这里**原样抽出来跑一遍**：
改坏文档 = 本测试红。文档与代码同源，不靠自觉。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from data_dog.storage.db import init_db, reset_engine

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "data_plane.md"
START = "<!-- dd-doc-sample: ingest-event -->"
END = "<!-- /dd-doc-sample: ingest-event -->"

AUTH = {"X-Tenant-Id": "tenant-doc", "X-Device-Sn": "device-doc-0001"}


def _doc_sample() -> dict[str, Any]:
    """从文档里抽出被标记包围的 json 代码块，解析成一个事件对象。"""
    text = DOC.read_text(encoding="utf-8")
    assert START in text and END in text, (
        f"docs/data_plane.md 缺少采样标记（{START} / {END}）——示例必须被标记，否则本守卫失效"
    )
    block = text.split(START, 1)[1].split(END, 1)[0]
    m = re.search(r"```json\n(.*?)```", block, re.S)
    assert m, "标记之间的示例不是 ```json 代码块"
    return json.loads(m.group(1))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'doc.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", url)
    monkeypatch.setenv("DD_INVESTIGATE_OFFLINE", "1")
    reset_engine()
    init_db(url)
    from data_dog.service.app import create_app
    with TestClient(create_app()) as c:
        yield c
    reset_engine()


def test_doc_sample_has_all_required_fields() -> None:
    """七个必填字段一个都不能少（少一个就是 200 + 0 候选的静默失败）。"""
    event = _doc_sample()
    required = ("event_id", "occurred_at", "received_at", "sequence",
                "vendor", "model", "firmware")
    missing = [f for f in required if event.get(f) in (None, "")]
    assert not missing, f"文档示例缺必填字段 {missing}，照抄会得 0 候选"

    assert isinstance(event.get("payload_inline"), str), (
        "payload_inline 必须是 JSON **字符串**（给成对象会被拒：payload_inline_not_string）"
    )
    json.loads(event["payload_inline"])  # 必须能被解析


def test_doc_sample_actually_produces_a_candidate(client: TestClient) -> None:
    """照抄文档示例 → 真产出候选。这条是"文档没骗人"的最终判据。"""
    event = _doc_sample()
    r = client.post("/v1/telemetry", headers=AUTH, json={"events": [event]})
    assert r.status_code == 200, f"接入失败：{r.status_code} {r.text[:400]}"

    body = r.json()
    stats = body.get("stats", {})
    assert stats.get("rejected") == 0, (
        f"文档示例被拒了：rejected_by_reason={stats.get('rejected_by_reason')}——"
        f"说明示例与代码已经不同源"
    )
    assert stats.get("ingested") == 1, f"received 条数不对：stats={stats}"
    assert body.get("candidates"), (
        f"文档示例跑不出候选（temperature=96 应超阈值）：resp={json.dumps(body, ensure_ascii=False)[:600]}"
    )

    # 候选要落在**请求头指定的**租户/设备上（身份不来自正文）
    cand = body["candidates"][0]
    assert cand.get("device_sn") == AUTH["X-Device-Sn"], (
        f"候选的 device_sn={cand.get('device_sn')!r} 与请求头不一致，身份注入被绕过"
    )
