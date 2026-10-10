"""设备身份签名（⑦）：把"设备身份"从网关注入的声称抬到设备自己证明。

要守住的六件事
-------------
1. **默认零回归**：`DD_DEVICE_SIGNING` 未设/`off` 时，没带签名的请求照旧通过
   （与改动前逐字节一致）——这是"可选、向后兼容"的字面含义。
2. **签名覆盖数据**，不只是身份：换正文/换事件/换时间窗都必须验不过
   （只签设备号会被重放：录一条合法签名配任意伪造正文）。
3. **`optional` 档不是纸**：带了签名就必须对；**故意发垃圾签名等于绕过校验**，
   所以"签名不对"在任何档位都拒绝，没有"忽略"这个分支。
4. **`required` 档失败关闭**：缺签名、设备没配密钥，都拒。
5. **规范化无歧义**：`\n` 分隔，否则 `("a","bc")` 与 `("ab","c")` 会签出同一串
   （可构造碰撞）。
6. **配置写坏 = 失败关闭**：`DD_DEVICE_SIGNING` 拼错**不能**被当成 `off`
   （"我明明开了强制签名"变成"其实一直没验"是最危险的静默降级）。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from data_dog import settings
from data_dog.data_plane.device_signature import (
    canonical_message,
    check_device_signature,
    parse_header,
    payload_digest,
    signature_for,
)
from data_dog.data_plane.gateway import TelemetryGateway
from data_dog.state.memory import InMemoryStateStore
from data_dog.storage.db import init_db, reset_engine

SN = "go2-sn-001"
SECRET = "device-secret-001"
TENANT = "tenant-a"
PAYLOAD = json.dumps({"temperature": 96, "current": 12.0, "mode": 0})


def _raw(payload: str = PAYLOAD, event_id: str = "e1", sequence: int = 1,
         occurred_at: str = "2026-09-13T10:00:00Z") -> dict:
    return {
        "event_id": event_id, "occurred_at": occurred_at,
        "received_at": "2026-09-13T10:00:00.100Z", "sequence": sequence,
        "vendor": "unitree", "model": "go2", "firmware": "3.2.1",
        "protocol_version": "motor-log-v2",
        "component_hint": "joint_motor.front_left_hip",
        "payload_inline": payload,
    }


def _sig(secret: str = SECRET, raw: dict | None = None, tenant: str = TENANT,
         device: str = SN) -> str:
    r = raw or _raw()
    h = signature_for(secret, tenant_id=tenant, device_sn=device,
                      event_id=r["event_id"], occurred_at=r["occurred_at"],
                      sequence=r["sequence"], payload=r["payload_inline"])
    return f"sha256={h}"


def _check(mode: str, provided: str | None, *, raw: dict | None = None,
           keys: dict | None = None, tenant: str = TENANT, device: str = SN):
    return check_device_signature(
        mode=mode, keys={SN: SECRET} if keys is None else keys,
        tenant_id=tenant, device_sn=device, raw=raw or _raw(), provided=provided)


# ---------------------------------------------------------------- 规范化

def test_canonical_message_is_unambiguous():
    """`\\n` 分隔不是风格问题：拼接会让 ("a","bc") 与 ("ab","c") 撞成同一串。"""
    a = canonical_message(tenant_id="a", device_sn="bc", event_id="e",
                          occurred_at="t", sequence=1, payload="p")
    b = canonical_message(tenant_id="ab", device_sn="c", event_id="e",
                          occurred_at="t", sequence=1, payload="p")
    assert a != b


def test_payload_digest_is_stable_and_distinguishes_empty_from_none():
    """空载荷有确定摘要（不是"没有摘要"），且不同载荷摘要不同。"""
    assert payload_digest("") == payload_digest(None or "")
    assert payload_digest("") != payload_digest("x")


@pytest.mark.parametrize("value,expect", [
    ("sha256=deadbeef", "deadbeef"),
    ("SHA256=DEADBEEF", "deadbeef"),
    (" sha256=abc123 ", "abc123"),
    ("deadbeef", None),                 # 裸 hex：无法表达算法，换算法会变成隐式破坏性变更
    ("md5=deadbeef", None),
    ("sha256=", None),
    ("sha256=zzzz", None),
    ("", None),
    (None, None),
])
def test_parse_header(value, expect):
    assert parse_header(value) == expect


# ---------------------------------------------------------------- 三档矩阵

def test_off_mode_verifies_nothing():
    """默认档：连垃圾签名都放行——因为**完全不看**这个头（零回归）。"""
    assert _check("off", None)[0] is True
    assert _check("off", "sha256=deadbeef")[0] is True
    assert _check("off", "garbage", keys={})[0] is True


def test_optional_is_per_device():
    """`optional` 是**按设备**生效的：配了密钥 = 该设备已纳入强制签名。

    这样"开强制"这个动作就是"给设备配一把密钥"，不需要再维护第二份开关清单
    （两份清单必然漂移，然后就没人知道到底哪些设备在验）。
    """
    # 设备没配密钥 + 没带签名 → 放行（还没上签名的设备的正常态）
    ok, reason, _ = _check("optional", None, keys={})
    assert (ok, reason) == (True, "")

    # 设备**配了**密钥 + 没带签名 → 拒（该设备已纳入强制）
    ok, reason, _ = _check("optional", None)
    assert (ok, reason) == (False, "device_signature_missing")

    # 合法签名 → 放行
    assert _check("optional", _sig())[0] is True


def test_optional_rejects_bad_signature_for_everyone():
    """`optional` 档**不是纸**：签名不对一律拒。

    否则攻击者只要**故意发垃圾签名**就等于绕过了整个校验。
    """
    ok, reason, _ = _check("optional", "sha256=deadbeef")
    assert (ok, reason) == (False, "device_signature_invalid")

    ok, reason, _ = _check("optional", "not-a-signature")
    assert (ok, reason) == (False, "device_signature_invalid")

    ok, reason, _ = _check("optional", _sig(), keys={})
    assert (ok, reason) == (False, "device_signature_unknown"), (
        "设备没配密钥却带了签名 → 无法验证，必须拒")


def test_required_fails_closed_on_missing_signature_or_unknown_device():
    assert _check("required", _sig())[0] is True

    ok, reason, _ = _check("required", None)
    assert (ok, reason) == (False, "device_signature_missing")

    ok, reason, _ = _check("required", None, keys={})
    assert (ok, reason) == (False, "device_signature_unknown")


# ---------------------------------------------------------------- 签名覆盖数据

@pytest.mark.parametrize("mutate,label", [
    (lambda r: {**r, "payload_inline": json.dumps({"temperature": 20})}, "改正文"),
    (lambda r: {**r, "event_id": "e2"}, "改事件号"),
    (lambda r: {**r, "sequence": 2}, "改序号"),
    (lambda r: {**r, "occurred_at": "2026-09-13T10:00:05Z"}, "改时间"),
])
def test_signature_covers_the_data_not_just_identity(mutate, label):
    """只签设备号的签名可被重放：录一条合法签名 + 任意伪造正文即可通过。"""
    sig = _sig()
    tampered = mutate(_raw())
    ok, reason, _ = _check("required", sig, raw=tampered)
    assert (ok, reason) == (False, "device_signature_invalid"), f"{label}没有被签名覆盖"


def test_signature_binds_tenant_and_device():
    """换租户或换设备号（同一正文）也必须验不过——否则签的是"数据"不是"谁发的"。"""
    sig = _sig()
    assert _check("required", sig, tenant="tenant-b")[0] is False
    assert _check("required", sig, device="go2-sn-002")[0] is False


def test_signature_is_bound_to_the_secret():
    sig = _sig(secret="another-secret")
    assert _check("required", sig)[0] is False


# ---------------------------------------------------------------- 配置失败关闭

@pytest.mark.parametrize("bad", ["bogus", "ON", "true", "1"])
def test_invalid_signing_mode_is_not_treated_as_off(monkeypatch, bad):
    """拼错档位**绝不能**被当成 off——那会把"已强制签名"变成"其实没验"。"""
    monkeypatch.setenv("DD_DEVICE_SIGNING", bad)
    with pytest.raises(ValueError):
        settings.device_signing()


def test_signing_mode_defaults_to_off(monkeypatch):
    monkeypatch.delenv("DD_DEVICE_SIGNING", raising=False)
    assert settings.device_signing() == "off"


@pytest.mark.parametrize("bad", ["{not json", "[]", '{"": "k"}', '{"SN": ""}'])
def test_malformed_device_keys_fail_closed(monkeypatch, bad):
    monkeypatch.setenv("DD_DEVICE_KEYS", bad)
    with pytest.raises(ValueError):
        settings.device_keys()


# ---------------------------------------------------------------- 网关接线

def _gw() -> TelemetryGateway:
    return TelemetryGateway(state_store=InMemoryStateStore())


def _ingest(gw: TelemetryGateway, sig: str | None, raw: dict | None = None):
    auth = {"tenant_id": TENANT, "device_sn": SN, "device_signature": sig,
            "principal": "http-gateway"}
    return gw.ingest(raw or _raw(), auth)


def test_gateway_default_off_is_backward_compatible(monkeypatch):
    """默认（不配任何东西）：没带签名的请求照旧通过——向后兼容的硬要求。"""
    monkeypatch.delenv("DD_DEVICE_SIGNING", raising=False)
    monkeypatch.delenv("DD_DEVICE_KEYS", raising=False)
    ok, res = _ingest(_gw(), None)
    assert ok is True, res


def test_gateway_required_rejects_and_reports_reason(monkeypatch):
    monkeypatch.setenv("DD_DEVICE_SIGNING", "required")
    monkeypatch.setenv("DD_DEVICE_KEYS", json.dumps({SN: SECRET}))

    gw = _gw()
    ok, res = _ingest(gw, None)
    assert ok is False
    assert res["reason"] == "device_signature_missing"

    ok, res = _ingest(gw, _sig())
    assert ok is True, res

    ok, res = _ingest(gw, "sha256=deadbeef")
    assert ok is False and res["reason"] == "device_signature_invalid"


def test_gateway_rejects_when_config_is_broken(monkeypatch):
    """配置坏 → 拒绝且 reason 可分类（由服务边界转 503），绝不静默放行。"""
    monkeypatch.setenv("DD_DEVICE_SIGNING", "bogus")
    ok, res = _ingest(_gw(), None)
    assert ok is False
    assert res["reason"] == "device_signing_misconfigured"


def test_gateway_malformed_keys_reject(monkeypatch):
    monkeypatch.setenv("DD_DEVICE_SIGNING", "required")
    monkeypatch.setenv("DD_DEVICE_KEYS", "{not json")
    ok, res = _ingest(_gw(), None)
    assert ok is False and res["reason"] == "device_signing_misconfigured"


# ---------------------------------------------------------------- HTTP 端到端

@pytest.fixture()
def client(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'devsig.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", url)
    reset_engine()
    init_db(url)
    from data_dog.service.app import create_app
    with TestClient(create_app()) as c:
        yield c
    reset_engine()


def test_header_reaches_the_gateway_end_to_end(client, monkeypatch):
    """HTTP 层必须把 `X-Device-Signature` 真送到网关——否则整条链是摆设。

    这条专治"接了一半"：依赖里读了头、网关却没收到（或反之），
    单测全绿而线上等于没验。
    """
    monkeypatch.setenv("DD_DEVICE_SIGNING", "required")
    monkeypatch.setenv("DD_DEVICE_KEYS", json.dumps({SN: SECRET}))
    raw = _raw()
    hdr = {"X-Tenant-Id": TENANT, "X-Device-Sn": SN}

    r = client.post("/v1/telemetry", headers=hdr, json={"events": [raw]})
    assert r.status_code == 200
    body = r.json()
    assert body["stats"]["rejected"] == 1, body["stats"]
    assert "device_signature_missing" in json.dumps(body["stats"], ensure_ascii=False)

    r = client.post("/v1/telemetry",
                    headers={**hdr, "X-Device-Signature": _sig()},
                    json={"events": [raw]})
    assert r.status_code == 200
    body = r.json()
    assert body["stats"]["rejected"] == 0, body["stats"]
    # 最强的证据：这条事件**真的落库了**（不是"没报错但也没进去"）
    assert body["persisted"]["telemetry"] == 1, body["persisted"]
