"""接入端点对着敌意输入必须给 4xx，不能给 5xx。

要守住的为什么是"不 500"而不是"返回某条具体错误"
----------------------------------------------
现场的接入方会先用错误格式的包来试接口（探活脚本、抓包重放、编码写错）。
这些包**本来就不该成功**，所以只要求 4xx/200-with-rejection。但如果它们能打出
5xx，就同时说明三件事：
1. 有一条**没人想过的分支**在跑（典型是 `TypeError: 'NoneType' is not iterable` 藏在
   某层 `.get()` 之后）——这是真 bug，只是还没人踩到；
2. 500 的响应体在多数部署里会带堆栈（信息泄露面）；
3. 对方会把它当成"服务不稳定"重试，把一次格式错误放大成流量事件。

这正是 `docs/data_plane.md` 承诺"批处理 + 逐事件拒绝 + `rejected_by_reason`"的**反面**：
承诺说每条坏事件都能被单独说明，那就不能有一类坏事件把整批打成 500。

刻意不测的东西
-------------
- 不测限流（`429` 是正常业务码，已有专门用例）；
- 不测鉴权（缺 header 的路径由 `test_device_signature.py` / 契约用例覆盖）；
- 不追求"具体是哪条 reason"，那属于 `gateway` 的单元用例。
本文件只守一条：**任何畸形输入都不许 5xx**。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

_AUTH = {"X-Tenant-Id": "tenant-hostile", "X-Device-Sn": "device-go2-hostile"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """真 app + 真 SQLite（不用 mock）：畸形输入要经过与生产同一套解析/校验链。"""
    url = f"sqlite:///{(tmp_path / 'hostile.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", url)
    monkeypatch.setenv("DD_INVESTIGATE_OFFLINE", "1")
    from data_dog.storage.db import init_db, reset_engine

    reset_engine()
    init_db(url)
    from data_dog.service.app import create_app

    with TestClient(create_app()) as c:
        yield c


def _ev(**over) -> dict:
    ev = {
        "event_id": "e1",
        "occurred_at": "2026-09-13T10:00:00Z",
        "received_at": "2026-09-13T10:00:00.100Z",
        "sequence": 1,
        "vendor": "unitree",
        "model": "go2",
        "firmware": "3.2.1",
        "protocol_version": "motor-log-v2",
        "component_hint": "joint_motor.front_left_hip",
        "payload_inline": json.dumps({"temperature": 70}),
    }
    ev.update(over)
    return ev


# (说明, 请求体) —— 每一条在现实里都有来源，不是凭空编的
_HOSTILE: list[tuple[str, object]] = [
    ("缺 events 键", {}),
    ("events 是 null", {"events": None}),
    ("events 是字符串", {"events": "not-a-list"}),
    ("events 是对象", {"events": {"0": _ev()}}),
    ("events 空数组", {"events": []}),
    ("单条事件是 null", {"events": [None]}),
    ("单条事件是字符串", {"events": ["hello"]}),
    ("事件全空", {"events": [{}]}),
    ("payload_inline 不是 JSON", {"events": [_ev(payload_inline="{bad")]}),
    ("payload_inline 是 null", {"events": [_ev(payload_inline=None)]}),
    ("payload_inline 是数字", {"events": [_ev(payload_inline=42)]}),
    ("payload 含 NaN/Infinity", {"events": [_ev(payload_inline='{"t": NaN, "u": Infinity}')]}),
    ("occurred_at 非时间", {"events": [_ev(occurred_at="not-a-date")]}),
    ("occurred_at 为空串", {"events": [_ev(occurred_at="")]}),
    ("occurred_at 是数字", {"events": [_ev(occurred_at=1234567890)]}),
    ("sequence 为负", {"events": [_ev(sequence=-5)]}),
    ("sequence 溢出 int64", {"events": [_ev(sequence=2**70)]}),
    ("sequence 是浮点", {"events": [_ev(sequence=1.5)]}),
    ("component_hint 空串", {"events": [_ev(component_hint="")]}),
    ("event_id 超长", {"events": [_ev(event_id="x" * 10000)]}),
    ("事件是嵌套深层结构", {"events": [_ev(payload_inline="[" * 200 + "]" * 200)]}),
    ("同批重复 event_id", {"events": [_ev(), _ev()]}),
    ("events 数量远超单批上限", {"events": [_ev(event_id=f"e{i}") for i in range(5000)]}),
]


@pytest.mark.parametrize("label,body", _HOSTILE, ids=[c[0] for c in _HOSTILE])
def test_hostile_body_never_returns_5xx(client, label, body):
    r = client.post("/v1/telemetry", headers=_AUTH, json=body)
    assert r.status_code < 500, (
        f"[{label}] 畸形输入打出了 {r.status_code}，说明有一条没被想过的分支在跑："
        f"{r.text[:400]}"
    )


@pytest.mark.parametrize(
    "raw,label",
    [
        (b"", "空 body"),
        (b"{", "半个 JSON"),
        (b'{"events": [}', "截断的 JSON"),
        (b"\xff\xfe\x00bad", "非法 UTF-8 字节"),
        (b'{"events":[{"event_id":"e"}]}', "缺几乎所有字段"),
    ],
    ids=lambda v: "" if isinstance(v, bytes) else v,
)
def test_hostile_raw_bytes_never_return_5xx(client, raw, label):
    """编码层写错（拼接/截断/非 UTF-8）在真实网关上很常见，同样不许 5xx。"""
    r = client.post("/v1/telemetry", headers={**_AUTH, "Content-Type": "application/json"},
                    content=raw)
    assert r.status_code < 500, (
        f"[{label}] 原始字节打出了 {r.status_code}：{r.text[:400]}"
    )
